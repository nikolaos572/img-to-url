"""Tests for SITE_NAME branding across pages."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _make_client(tmp_path, site_name: str, *, login: bool = False) -> TestClient:
    """A client for a custom-branded instance; pass login=True for a
    logged-in session (GET /login then redirects to /, so tests that want
    the login page itself must stay anonymous)."""
    settings = Settings(
        password="pw",
        base_url="https://images.example.com",
        max_size_mb=1,
        expiration_hours=0,
        data_dir=tmp_path,
        port=8000,
        workers=1,
        session_secret="branding-secret",
        site_name=site_name,
    )
    app = create_app(settings)
    # https base URL -> Secure session cookie; the client must speak https.
    client = TestClient(app, base_url="https://testserver")
    if login:
        response = client.post(
            "/login",
            data={"username": "user", "password": "pw"},
            follow_redirects=False,
        )
        assert response.status_code == 303
    return client


class TestDefaultBranding:
    def test_login_page_defaults(self, client):
        html = client.get("/login").text
        assert "<title>Log in &middot; img-to-url</title>" in html
        assert '<span class="brand-mark">img</span>' in html
        assert "img-to-url &middot; paste an image, share the link" in html

    def test_authenticated_pages_default(self, auth_client):
        assert "<title>Upload &middot; img-to-url</title>" in auth_client.get("/").text
        assert "<title>Admin &middot; img-to-url</title>" in auth_client.get("/admin").text


class TestCustomBranding:
    def test_custom_name_on_public_page(self, tmp_path):
        client = _make_client(tmp_path, "My Pics")  # anonymous: login page
        response = client.get("/login")
        assert response.status_code == 200
        assert "<title>Log in &middot; My Pics</title>" in response.text
        assert '<span class="brand-mark">myp</span>' in response.text
        assert "My Pics &middot; paste an image, share the link" in response.text
        assert "img-to-url" not in response.text  # no hardcoded name left anywhere

    def test_custom_name_on_authenticated_pages(self, tmp_path):
        client = _make_client(tmp_path, "My Pics", login=True)
        assert "<title>Upload &middot; My Pics</title>" in client.get("/").text
        assert "<title>Admin &middot; My Pics</title>" in client.get("/admin").text

    def test_name_with_html_chars_is_escaped(self, tmp_path):
        # SITE_NAME is operator-controlled, but Jinja must still autoescape
        # it so a stray <script> can never end up in the page.
        client = _make_client(tmp_path, "<b>x</b> &amp; pics")  # anonymous
        response = client.get("/login")
        assert response.status_code == 200
        assert "<title>Log in &middot; &lt;b&gt;x&lt;/b&gt; &amp;amp; pics</title>" in response.text
        assert "<script>" not in response.text
