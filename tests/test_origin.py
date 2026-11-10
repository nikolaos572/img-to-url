"""POSTs a browser sends from any origin other than BASE_URL's are refused.

The settings fixture's BASE_URL is https://images.example.com. The test
client sends no Origin header unless a test adds one, like curl or a script.
"""

import logging

import pytest

from app import db

from .conftest import make_png, upload

ORIGIN = "https://images.example.com"

OTHER_ORIGINS = [
    "https://evil.example",
    "https://blog.example.com",  # a sibling subdomain: same site for SameSite=Lax
    "http://images.example.com",  # same host, plain http
    "https://images.example.com:8443",  # same host, another port
    "https://images.example.com.evil.example",
    "null",  # sandboxed frames, some redirects
]


def _login(client, origin=None):
    headers = {"Origin": origin} if origin else {}
    return client.post(
        "/login",
        data={"username": "user", "password": client.app.state.settings.password},
        headers=headers,
        follow_redirects=False,
    )


class TestRefused:
    def test_login_from_another_address_explains_the_mismatch(self, client, caplog):
        with caplog.at_level(logging.WARNING, logger="img-to-url"):
            response = _login(client, origin="http://10.0.0.5:8000")

        assert response.status_code == 403
        assert response.headers["content-type"].startswith("text/html")
        assert "set-cookie" not in response.headers
        assert (
            "This site&#39;s address is set to https://images.example.com (BASE_URL), "
            "but this request came from http://10.0.0.5:8000. "
            "Open https://images.example.com/login, or set BASE_URL to the address you use."
        ) in response.text
        # The 403 page is a normal page: same security headers as the rest.
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert "Refused POST /login sent from 'http://10.0.0.5:8000'" in caplog.text

    @pytest.mark.parametrize("origin", OTHER_ORIGINS)
    def test_clear_all_from_another_origin_deletes_nothing(self, auth_client, origin):
        upload(auth_client, "a.png", make_png(10))

        response = auth_client.post(
            "/admin/clear-all", headers={"Origin": origin}, follow_redirects=False
        )
        assert response.status_code == 403
        assert "set to https://images.example.com" in response.json()["detail"]
        settings = auth_client.app.state.settings
        assert db.count_images(settings.db_path) == 1

    @pytest.mark.parametrize(
        "path", ["/login", "/logout", "/upload", "/admin/delete", "/admin/clear-all"]
    )
    def test_every_post_is_checked(self, auth_client, path):
        response = auth_client.post(
            path, headers={"Origin": "https://evil.example"}, follow_redirects=False
        )
        assert response.status_code == 403
        # Still logged in: /logout was refused too.
        assert auth_client.get("/", follow_redirects=False).status_code == 200

    def test_upload_is_refused_before_anything_is_stored(self, auth_client):
        response = auth_client.post(
            "/upload",
            files={"file": ("a.png", make_png(10))},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403
        assert "https://evil.example" in response.json()["detail"]  # shown by upload.js
        settings = auth_client.app.state.settings
        assert db.count_images(settings.db_path) == 0
        assert list(settings.images_dir.iterdir()) == []

    def test_checked_before_authentication(self, client):
        response = client.post(
            "/upload",
            files={"file": ("a.png", make_png(10))},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403


class TestAllowed:
    @pytest.mark.parametrize(
        "origin",
        [ORIGIN, "https://IMAGES.example.com", "https://images.example.com:443"],
    )
    def test_the_configured_origin_however_written(self, client, origin):
        assert _login(client, origin=origin).status_code == 303
        response = client.post(
            "/upload", files={"file": ("a.png", make_png(10))}, headers={"Origin": origin}
        )
        assert response.status_code == 200

    def test_requests_without_an_origin_header(self, client):
        # Not from a browser, so not forgeable by another site; the session
        # cookie is still required.
        assert _login(client).status_code == 303
        assert upload(client, "a.png", make_png(10)).status_code == 200

    def test_get_requests_are_not_checked(self, auth_client):
        response = auth_client.get("/admin", headers={"Origin": "https://evil.example"})
        assert response.status_code == 200
