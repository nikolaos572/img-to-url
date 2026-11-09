"""End-to-end user journey plus concurrent-upload stress test."""

import asyncio
import re

import httpx
from fastapi.testclient import TestClient

from app import db

from .conftest import make_png


class TestUserJourney:
    def test_full_journey(self, tmp_path):
        from app.config import Settings
        from app.main import create_app

        settings = Settings(
            password="hunter2",
            base_url="https://images.example.com",
            max_size_mb=2,
            expiration_hours=0,
            data_dir=tmp_path,
            port=8000,
            workers=1,
            session_secret="integration-secret",
        )
        app = create_app(settings)
        # https base URL -> Secure session cookie; the client must match.
        with TestClient(app, base_url="https://testserver") as client:
            # Anonymous visitor gets a plain 404 at /; /admin goes to login.
            response = client.get("/", follow_redirects=False)
            assert response.status_code == 404
            assert "<h1>404 Not Found</h1>" in response.text
            assert client.get("/admin", follow_redirects=False).status_code == 303

            # Bad password, then a successful login.
            assert (
                client.post("/login", data={"username": "user", "password": "wrong"}).status_code
                == 401
            )
            response = client.post(
                "/login",
                data={"username": "user", "password": "hunter2"},
                follow_redirects=False,
            )
            assert response.status_code == 303

            # Upload an image; URL must use the configured base URL.
            response = client.post(
                "/upload", files={"file": ("photo.png", make_png(100))}
            )
            assert response.status_code == 200
            data = response.json()
            assert data["url"].startswith("https://images.example.com/image-")
            assert re.fullmatch(
                r"image-[0-9a-f]{16}-[0-9a-f]{16}\.png", data["filename"]
            )

            # Anyone (even without the session) can open the direct link.
            public = TestClient(app)
            response = public.get("/" + data["filename"])
            assert response.status_code == 200
            assert response.content == make_png(100)
            assert response.headers["content-type"] == "image/png"

            # The image shows up in the admin panel and can be deleted there.
            response = client.get("/admin")
            assert data["url"] in response.text
            response = client.post(
                "/admin/delete", data={"filename": data["filename"]}, follow_redirects=False
            )
            assert response.status_code == 303
            assert public.get("/" + data["filename"]).status_code == 404

            # Logging out locks the session out again.
            response = client.post("/logout", follow_redirects=False)
            assert response.status_code == 303
            assert client.get("/", follow_redirects=False).status_code == 404


class TestConcurrentUploads:
    def test_parallel_uploads_all_succeed(self, app):
        """Requirement: multiple parallel submissions must not freeze or fail.

        Uses a real async client against the ASGI app, like uvicorn would.
        """

        async def run():
            transport = httpx.ASGITransport(app=app)  # app with lifespan off
            async with httpx.AsyncClient(
                transport=transport, base_url="https://test"
            ) as async_client:
                await async_client.post(
                    "/login", data={"username": "user", "password": app.state.settings.password}
                )
                responses = await asyncio.gather(
                    *[
                        async_client.post(
                            "/upload", files={"file": (f"p{i}.png", make_png(50 + i))}
                        )
                        for i in range(10)
                    ]
                )
                return responses

        responses = asyncio.run(run())
        filenames = set()
        for i, response in enumerate(responses):
            assert response.status_code == 200, (i, response.text)
            data = response.json()
            filenames.add(data["filename"])
            assert data["size"] == 50 + i + 8  # PNG magic + payload

        assert len(filenames) == 10
        settings = app.state.settings
        assert db.count_images(settings.db_path) == 10
        for filename in filenames:
            assert (settings.images_dir / filename).is_file()
