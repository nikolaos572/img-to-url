"""Tests for public serving of images via their direct URLs."""

from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import make_jpeg, make_png, stored_bytes, upload

# The exact page shown for deleted / non-existent links (templates/404.html).
NOT_FOUND_PAGE = (
    "<html><head><title>404 Not Found</title></head><body><center><h1>404 Not Found"
    "</h1></center><hr><center>The requested URL cannot be found on this server."
    "</center></body></html>"
)


def _upload_one(auth_client, name="x.png", content=None):
    """Upload an image; returns its filename and the bytes stored for it."""
    content = content if content is not None else make_png(32)
    filename = upload(auth_client, name, content).json()["filename"]
    return filename, stored_bytes(auth_client, filename)


class TestPublicServing:
    def test_serves_image_without_login(self, client, auth_client):
        filename, content = _upload_one(auth_client)

        response = client.get("/" + filename, follow_redirects=False)
        assert response.status_code == 200
        assert response.content == content
        assert response.headers["content-type"] == "image/png"
        # IMG_CACHE_MINUTES is unset in the fixture: no caching at all.
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_cache_time_is_configurable(self, settings):
        app = create_app(replace(settings, image_cache_minutes=30))
        client = TestClient(app, base_url="https://testserver")
        client.post("/login", data={"username": "user", "password": settings.password})
        filename, _ = _upload_one(client)

        assert client.get("/" + filename).headers["cache-control"] == "public, max-age=1800"
        assert client.head("/" + filename).headers["cache-control"] == "public, max-age=1800"

    def test_content_type_matches_extension(self, auth_client, client):
        filename, _ = _upload_one(auth_client, "y.jpg", make_jpeg(16))
        response = client.get("/" + filename)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"

    def test_unknown_image_url_404s(self, client):
        response = client.get("/image-" + "0" * 16 + "-" + "0" * 16 + ".png")
        assert response.status_code == 404

    def test_deleted_image_404s(self, client, auth_client):
        filename, _ = _upload_one(auth_client)
        auth_client.post("/admin/delete", data={"filename": filename}, follow_redirects=False)
        assert client.get("/" + filename).status_code == 404

    def test_head_request(self, client, auth_client):
        # Link-preview bots and uptime monitors often probe with HEAD.
        filename, content = _upload_one(auth_client)
        response = client.head("/" + filename)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-length"] == str(len(content))
        assert response.content == b""

        missing = "/image-" + "0" * 16 + "-" + "0" * 16 + ".png"
        assert client.head(missing).status_code == 404


class TestNotFoundPage:
    """Deleted and non-existent links get the plain HTML 404 page, not JSON."""

    @staticmethod
    def _assert_not_found_page(response):
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")
        assert response.text.strip() == NOT_FOUND_PAGE

    def test_never_existing_image_link(self, client):
        self._assert_not_found_page(client.get("/image-" + "0" * 16 + "-" + "0" * 16 + ".png"))

    def test_deleted_image_link(self, auth_client):
        filename, _ = _upload_one(auth_client)
        assert auth_client.get("/" + filename).status_code == 200
        auth_client.post("/admin/delete", data={"filename": filename}, follow_redirects=False)
        self._assert_not_found_page(auth_client.get("/" + filename))

    def test_any_other_unknown_url(self, client):
        for path in ["/nonexistent", "/favicon.ico", "/a/b/c", "/static/missing.css"]:
            self._assert_not_found_page(client.get(path))

    def test_bare_hostname_for_anonymous_visitors(self, client):
        # No redirect to /login: strangers see the same page as a dead link.
        self._assert_not_found_page(client.get("/", follow_redirects=False))

    def test_bare_hostname_after_logout(self, auth_client):
        auth_client.post("/logout", follow_redirects=False)
        self._assert_not_found_page(auth_client.get("/", follow_redirects=False))


class TestNotFoundAndTraversal:
    def test_random_path_is_404(self, client):
        for path in [
            "/favicon.ico",
            "/robots.txt",
            "/nonexistent",
            "/docs",
            "/openapi.json",
            "/etc/passwd",
        ]:
            response = client.get(path)
            assert response.status_code == 404, path

    def test_traversal_attempts_are_404(self, client):
        for path in [
            "/image-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb%2e%2e%2f../../etc/passwd",
            "/..%2f..%2fetc%2fpasswd.png",
            "/image-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb.png/../../etc/passwd",
            "/image-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb.png%00.svg",
        ]:
            response = client.get(path)
            assert response.status_code == 404, path

    def test_static_css_served(self, client):
        response = client.get("/static/style.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]

    def test_static_files_are_revalidated(self, client):
        # no-cache + ETag: a cheap 304 per load, and never stale CSS/JS
        # after the container is upgraded.
        response = client.get("/static/upload.js")
        assert response.headers["cache-control"] == "no-cache"
        assert "etag" in response.headers

    def test_healthz(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert client.head("/healthz").status_code == 200


class TestCspAndExternalScripts:
    """The strict Content-Security-Policy forbids inline scripts and inline
    event handlers; these tests keep the templates compliant with it."""

    def test_html_pages_have_csp_header(self, auth_client):
        for path in ["/", "/admin"]:
            response = auth_client.get(path)
            csp = response.headers["content-security-policy"]
            assert "default-src 'self'" in csp, path
            assert "img-src 'self' data: blob:" in csp, path
            assert "frame-ancestors 'none'" in csp, path
            # Neither falls back to default-src, so both must be explicit.
            assert "base-uri 'none'" in csp, path
            assert "form-action 'self'" in csp, path

    def test_login_page_has_csp_header(self, client):
        # Anonymous GET /login renders the page (200) — unlike /admin, which
        # would redirect and carry no content-type at all.
        csp = client.get("/login").headers["content-security-policy"]
        assert "default-src 'self'" in csp

    def test_anonymous_root_404_has_csp_header(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 404
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "DENY"

    def test_static_files_and_json_have_no_csp(self, auth_client):
        # CSP applies to documents; the site's own files and JSON carry none.
        for path in ["/static/style.css", "/healthz"]:
            response = auth_client.get(path)
            assert response.status_code == 200, path
            assert "content-security-policy" not in response.headers, path

    def test_served_images_are_sandboxed(self, client, auth_client):
        # Opened directly, even a crafted file may neither run nor load
        # anything. Embedding the image elsewhere is unaffected.
        filename, _ = _upload_one(auth_client)
        for response in (client.get("/" + filename), client.head("/" + filename)):
            assert response.status_code == 200
            assert response.headers["content-security-policy"] == (
                "default-src 'none'; style-src 'unsafe-inline'; sandbox"
            )
            assert response.headers["x-content-type-options"] == "nosniff"

    def test_upload_page_uses_external_script_only(self, auth_client):
        html = auth_client.get("/").text
        assert 'src="/static/upload.js"' in html
        # The upload config must flow through data-* attributes.
        assert 'data-max-bytes="1048576"' in html  # 1 MB fixture limit
        assert 'data-allowed="avif,bmp,gif,ico,jpeg,jpg,png,webp"' in html
        # No inline scripts anywhere (CSP would block them anyway).
        assert "<script>" not in html

    def test_upload_js_served_as_javascript(self, client):
        response = client.get("/static/upload.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        assert "uploadFile" in response.text

    def test_admin_uses_data_confirm_not_inline_handlers(self, auth_client):
        html = auth_client.get("/admin").text
        # Confirmation prompts must live in data attributes (external admin.js
        # wires them up), not inline onsubmit= handlers.
        assert 'data-confirm="Delete ALL images? This cannot be undone."' in html
        assert "onsubmit" not in html
        assert 'src="/static/admin.js"' in html
        assert "<script>" not in html

    def test_admin_js_served_as_javascript(self, client):
        response = client.get("/static/admin.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        assert "data-confirm" in response.text
        assert "data-copy" in response.text  # the per-row Copy buttons


class TestDarkMode:
    """Dark mode follows the browser / OS setting; there is no toggle."""

    def test_pages_declare_light_and_dark(self, client):
        # Lets the browser pick the scheme, and paint the first frame in it,
        # before the stylesheet has even loaded.
        html = client.get("/login").text
        assert '<meta name="color-scheme" content="light dark">' in html

    def test_stylesheet_has_a_dark_palette(self, client):
        css = client.get("/static/style.css").text
        assert "@media (prefers-color-scheme: dark)" in css
        dark = css.split("@media (prefers-color-scheme: dark)", 1)[1]
        for token in ("--bg:", "--surface:", "--text:", "--text-muted:", "--border:", "--link:"):
            assert token in dark, token
