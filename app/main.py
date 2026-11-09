"""FastAPI application for img-to-url."""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import Headers, UploadFile
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import auth, db, images
from app.cleanup import start_cleanup_thread
from app.config import Settings

log = logging.getLogger("img-to-url")

APP_DIR = Path(__file__).parent

PAGE_SIZE = 10
UPLOAD_CHUNK = 1024 * 1024  # 1 MiB
MAX_FILENAME_ATTEMPTS = 5
MAX_FORM_BODY = 64 * 1024  # login / admin forms are a few hundred bytes
MULTIPART_SLACK = 64 * 1024  # boundaries + part headers around an uploaded file

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class BodySizeLimitMiddleware:
    """Cap request body sizes before any endpoint parses them (pure ASGI).

    Starlette buffers form fields in memory and spools uploaded files to
    disk with no overall limit, so without this even an anonymous POST to
    /login could be made arbitrarily large. A too-large Content-Length is
    refused up front; chunked bodies are cut off once they cross the limit.
    """

    def __init__(self, app: ASGIApp, limits: Callable[[str], tuple[int, str]]) -> None:
        self.app = app
        self.limits = limits  # request path -> (max body bytes, 413 detail)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit, detail = self.limits(scope["path"])
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Raised inside the endpoint's body parsing, so it turns
                    # into an ordinary 413 response.
                    raise HTTPException(status_code=413, detail=detail)
            return message

        await self.app(scope, limited_receive, send)


class RevalidatedStaticFiles(StaticFiles):
    """Static files that browsers must revalidate before reusing.

    They carry ETags, so this only costs a 304 per page load — and after an
    upgrade, new pages are never paired with stale cached CSS/JS.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.images_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(settings.db_path)
    if not settings.session_secret:
        # A random key created on first start and shared by every worker
        # through the database, mixed with the password.
        install_key = db.get_or_create_secret(
            settings.db_path, "session_key", secrets.token_hex(32)
        )
        settings = replace(
            settings,
            session_secret=auth.derive_session_secret(install_key, settings.password),
        )

    # One template environment per app instance: site_name/brand_mark are
    # injected as globals so base.html renders them on every page.
    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
    templates.env.globals["site_name"] = settings.site_name
    templates.env.globals["brand_mark"] = settings.brand_mark

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop = start_cleanup_thread(settings)
        yield
        stop.set()

    app = FastAPI(
        title=settings.site_name,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    too_large = f"File exceeds the maximum size of {settings.max_size_mb:g} MB."

    def body_limit(path: str) -> tuple[int, str]:
        if path == "/upload":
            return settings.max_size_bytes + MULTIPART_SLACK, too_large
        return MAX_FORM_BODY, "Request body is too large."

    # Added before security_headers so that one stays the outer middleware
    # and its headers land on these 413 responses too.
    app.add_middleware(BodySizeLimitMiddleware, limits=body_limit)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # Stop browsers from MIME-sniffing served files as something active,
        # and stop other sites from embedding the logged-in pages in a frame.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("X-Frame-Options", "DENY")
            # Scripts and styles only from this site's own files — no inline
            # scripts or event handlers anywhere. data: is for the favicon and
            # blob: for the upload preview (an object URL of the local file);
            # images themselves come from 'self'. base-uri and form-action
            # don't fall back to default-src, so they are pinned explicitly.
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            )
        return response

    # ------------------------------------------------------------------ pages

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        if not auth.is_authenticated(request, settings):
            # Anonymous visitors to the bare hostname get the same plain 404
            # page as any unknown URL rather than a redirect to /login.
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "max_size_mb": settings.max_size_mb,
                "max_size_bytes": settings.max_size_bytes,
                "allowed_extensions": sorted(images.ALLOWED_EXTENSIONS),
                "expiration_hours": settings.expiration_hours,
            },
        )

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if auth.is_authenticated(request, settings):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", include_in_schema=False)
    def login(request: Request, username: str = Form(""), password: str = Form("")):
        if not auth.check_login(username, password, settings):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid username or password."},
                status_code=401,
            )
        response = RedirectResponse("/", status_code=303)
        auth.set_session_cookie(response, settings)
        return response

    @app.post("/logout", include_in_schema=False)
    def logout():
        response = RedirectResponse("/login", status_code=303)
        auth.clear_session_cookie(response)
        return response

    # ----------------------------------------------------------------- upload

    @app.post("/upload", include_in_schema=False)
    async def upload(request: Request):
        # Check the session before touching the body. Declaring the file as
        # an endpoint parameter would make FastAPI receive and spool the
        # whole upload first — for anonymous clients too.
        if not auth.is_authenticated(request, settings):
            return JSONResponse({"detail": "Not authenticated."}, status_code=401)

        async with request.form() as form:
            file = form.get("file")
            if not isinstance(file, UploadFile):
                raise HTTPException(
                    status_code=415, detail="No image file was provided."
                )

            ext = images.file_extension(file.filename)
            if ext not in images.ALLOWED_EXTENSIONS:
                allowed = ", ".join(f".{e}" for e in sorted(images.ALLOWED_EXTENSIONS))
                raise HTTPException(
                    status_code=415,
                    detail=f"Only image files are allowed ({allowed}).",
                )

            max_bytes = settings.max_size_bytes
            chunks: list[bytes] = []
            total = 0
            while chunk := await file.read(UPLOAD_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail=too_large)
                chunks.append(chunk)
            data = b"".join(chunks)

        if not images.looks_like_image(data):
            raise HTTPException(
                status_code=400,
                detail="File content is not a recognized image format.",
            )

        for _ in range(MAX_FILENAME_ATTEMPTS):
            filename = images.random_filename(ext)
            try:
                await asyncio.to_thread(_save_image, settings, filename, data)
                return {
                    "url": images.image_url(settings.base_url, filename),
                    "filename": filename,
                    "size": total,
                }
            except sqlite3.IntegrityError:
                # Astronomically unlikely, but regenerate on a collision.
                pass
        raise HTTPException(status_code=500, detail="Could not generate a unique filename.")

    # ----------------------------------------------------------------- admin

    @app.get("/admin", include_in_schema=False)
    def admin(request: Request, page: str = "1"):
        if not auth.is_authenticated(request, settings):
            return RedirectResponse("/login", status_code=303)

        try:
            current_page = max(1, int(page))
        except ValueError:
            current_page = 1

        count = db.count_images(settings.db_path)
        max_page = max(1, -(-count // PAGE_SIZE))
        page = min(current_page, max_page)

        rows = db.list_images(settings.db_path, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        items = [
            {
                "url": images.image_url(settings.base_url, row["filename"]),
                "filename": row["filename"],
                "size": images.human_size(row["size"]),
                "created": images.format_timestamp(row["created_at"]),
            }
            for row in rows
        ]
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "items": items,
                "total_images": count,
                "total_size": images.human_size(db.total_size(settings.db_path)),
                "page": page,
                "max_page": max_page,
            },
        )

    @app.post("/admin/delete", include_in_schema=False)
    def admin_delete(request: Request, filename: str = Form(""), page: int = Form(1)):
        if not auth.is_authenticated(request, settings):
            return RedirectResponse("/login", status_code=303)
        if not images.is_valid_filename(filename):
            raise HTTPException(status_code=404, detail="Image not found.")
        _remove_image(settings, filename)
        return RedirectResponse(f"/admin?page={page}", status_code=303)

    @app.post("/admin/clear-all", include_in_schema=False)
    def admin_clear_all(request: Request):
        if not auth.is_authenticated(request, settings):
            return RedirectResponse("/login", status_code=303)
        db.delete_all_images(
            settings.db_path,
            lambda filename: images.delete_file(settings.images_dir, filename),
        )
        return RedirectResponse("/admin", status_code=303)

    # ----------------------------------------------------------------- misc

    # GET and HEAD: unlike plain Starlette, FastAPI doesn't add HEAD to GET
    # routes, and uptime monitors and link-preview bots often probe with it.
    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    app.mount(
        "/static", RevalidatedStaticFiles(directory=str(APP_DIR / "static")), name="static"
    )

    # Registered last so it never shadows the routes above. Only filenames
    # matching the exact random pattern are served; anything else 404s.
    @app.api_route("/{filename}", methods=["GET", "HEAD"], include_in_schema=False)
    def serve_image(filename: str):
        if not images.is_valid_filename(filename):
            raise HTTPException(status_code=404, detail="Not Found")
        path = settings.images_dir / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(
            path,
            media_type=images.media_type_for(filename),
            # "no-store" unless IMG_CACHE_MINUTES allows browsers to cache.
            headers={"Cache-Control": settings.image_cache_control},
        )

    # Deleted or never-existing direct links (and any other unknown URL) get
    # the plain templates/404.html page instead of FastAPI's JSON error.
    @app.exception_handler(404)
    async def not_found(request: Request, _exc: Exception):
        return templates.TemplateResponse(request, "404.html", status_code=404)

    log.info(
        "img-to-url ready (base_url=%s, max_size=%sMB, expiration=%sh, "
        "image_cache=%r, workers_env=%s)",
        settings.base_url,
        settings.max_size_mb,
        settings.expiration_hours,
        settings.image_cache_control,
        settings.workers,
    )
    return app


def _save_image(settings: Settings, filename: str, data: bytes) -> None:
    """Record and write a new image (runs in a worker thread).

    The row is inserted first: the unique constraint on filename claims the
    name, so the astronomically unlikely case of two workers picking the
    same random filename fails here, before any file is touched. The file
    is then written while that insert still holds the database's write
    lock, which Clear all takes too, so Clear all sees a new image with
    both its row and its file or with neither. If the write fails, the row
    is rolled back and any partial file removed: a partial file would still
    be publicly served, yet invisible to the admin panel and to Clear all.
    """
    path = settings.images_dir / filename
    with db.adding_image(settings.db_path, filename, len(data), int(time.time())):
        try:
            path.write_bytes(data)
        except Exception:
            path.unlink(missing_ok=True)
            raise


def _remove_image(settings: Settings, filename: str) -> bool:
    (settings.images_dir / filename).unlink(missing_ok=True)
    return db.delete_image(settings.db_path, filename)


app = create_app()  # module-level app for `uvicorn app.main:app`
