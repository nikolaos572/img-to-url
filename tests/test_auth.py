"""Tests for login, session cookies, and route protection."""

import base64
import hashlib
import hmac
import re

from fastapi.testclient import TestClient

from app import auth, db
from app.auth import SESSION_MAX_AGE
from app.config import Settings
from app.main import create_app


def _login(client, password, username="user", follow_redirects=False):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=follow_redirects,
    )


class TestLoginPage:
    def test_login_page_renders(self, client):
        response = client.get("/login")
        assert response.status_code == 200
        assert "Password" in response.text
        assert "Invalid" not in response.text
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]

    def test_login_page_redirects_when_authenticated(self, auth_client):
        response = auth_client.get("/login", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"

    def test_login_page_hides_app_navigation(self, client):
        # Logged-out visitors get no Upload/Admin links and no Log out button.
        html = client.get("/login").text
        assert 'action="/logout"' not in html
        assert 'href="/admin"' not in html
        # The brand must not point at /, which is a 404 for anonymous visitors.
        assert '<a class="brand" href="/login">' in html

    def test_login_form_username_is_empty(self, client):
        for response in (client.get("/login"), _login(client, "wrong")):
            username = re.search(r'<input[^>]*name="username"[^>]*>', response.text)
            assert username, response.status_code
            assert "value=" not in username.group(0)
            # Focus starts in the (now empty) username field.
            assert "autofocus" in username.group(0)

    def test_authenticated_pages_show_navigation(self, auth_client):
        for path in ["/", "/admin"]:
            html = auth_client.get(path).text
            assert 'action="/logout"' in html, path
            assert 'href="/admin"' in html, path
            assert '<a class="brand" href="/">' in html, path


class TestLoginFlow:
    def test_correct_login_sets_cookie_and_redirects(self, client):
        response = _login(client, client.app.state.settings.password)
        assert response.status_code == 303
        assert response.headers["location"] == "/"

        set_cookie = response.headers["set-cookie"]
        assert "session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "samesite=lax" in set_cookie.lower()
        assert f"Max-Age={SESSION_MAX_AGE}" in set_cookie
        assert "Secure" in set_cookie  # base URL is https in the fixture
        assert "Path=/" in set_cookie

    def test_wrong_password_rejected(self, client):
        response = _login(client, "nope")
        assert response.status_code == 401
        assert "Invalid username or password" in response.text
        assert "session=" not in response.headers.get("set-cookie", "")

    def test_wrong_username_rejected(self, client):
        response = _login(client, client.app.state.settings.password, username="admin")
        assert response.status_code == 401

    def test_empty_credentials_rejected(self, client):
        response = _login(client, "")
        assert response.status_code == 401

    def test_login_then_homepage(self, client):
        assert client.get("/", follow_redirects=False).status_code == 404
        _login(client, client.app.state.settings.password)
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 200
        assert "Ctrl" in response.text and "Choose image file" in response.text


class TestProtectedRoutes:
    def test_homepage_is_404_not_a_login_redirect(self, client):
        # The bare hostname must not reveal the login page to strangers.
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 404
        assert "location" not in response.headers
        assert "/login" not in response.text

    def test_admin_redirects_to_login(self, client):
        response = client.get("/admin", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_admin_post_redirects_to_login(self, client):
        response = client.post(
            "/admin/delete", data={"filename": "image-a-b.png"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_clear_all_redirects_to_login(self, client):
        response = client.post("/admin/clear-all", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_upload_unauthenticated_is_401_json(self, client):
        response = client.post(
            "/upload",
            files={"file": ("x.png", b"\x89PNG\r\n\x1a\n")},
            follow_redirects=False,
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Not authenticated."


class TestSessionTokens:
    def test_token_roundtrip_and_expiry(self):
        token = auth.make_token("secret", now=1000)
        session = auth.read_token("secret", token, now=1000 + 1)
        assert session is not None
        assert session.expires_at == 1000 + SESSION_MAX_AGE
        assert auth.read_token("secret", token, now=1000 + SESSION_MAX_AGE) is None

    def test_token_signed_with_other_secret_fails(self):
        token = auth.make_token("secret-a", now=1000)
        assert auth.read_token("secret-b", token, now=1001) is None

    def test_every_login_gets_its_own_session_id(self):
        first = auth.read_token("secret", auth.make_token("secret", now=1000), now=1001)
        second = auth.read_token("secret", auth.make_token("secret", now=1000), now=1001)
        assert first.session_id and second.session_id
        assert first.session_id != second.session_id

    def test_token_without_session_id_is_rejected(self):
        # The format used before logout could revoke sessions: a correctly
        # signed expiry alone. Such cookies can't be revoked, so they're refused.
        def encode(data):
            return base64.urlsafe_b64encode(data).decode().rstrip("=")

        payload = b"9999999999"
        signature = hmac.new(b"secret", payload, hashlib.sha256).digest()
        old_token = f"{encode(payload)}.{encode(signature)}"
        assert auth.read_token("secret", old_token, now=1000) is None

    def test_tampered_cookie_is_rejected(self, client):
        _login(client, client.app.state.settings.password)
        token = client.cookies.get("session")
        assert token

        # Flip characters in the payload; signature no longer matches.
        payload, signature = token.split(".")
        if payload[0] == "A":
            evil = "B" + payload[1:]
        else:
            evil = "A" + payload[1:]
        client.cookies.set("session", f"{evil}.{signature}")
        assert client.get("/", follow_redirects=False).status_code == 404

    def test_garbage_cookie_is_rejected(self, client):
        client.cookies.set("session", "totally-bogous")
        assert client.get("/", follow_redirects=False).status_code == 404
        assert client.get("/admin", follow_redirects=False).status_code == 303


class TestGeneratedSessionSecret:
    """Without SESSION_SECRET, cookies are signed with a random per-install
    key (kept in the database) mixed with the password — not with a key
    derived from the password alone, which would let anyone holding a
    cookie test password guesses offline."""

    @staticmethod
    def _app(data_dir, password="pw"):
        return create_app(
            Settings(
                password=password,
                base_url="https://images.example.com",
                max_size_mb=1,
                expiration_hours=0,
                data_dir=data_dir,
                port=8000,
                workers=1,
                session_secret="",
            )
        )

    def test_same_install_keeps_its_key(self, tmp_path):
        # Every worker and every restart on the same data dir agree.
        first = self._app(tmp_path).state.settings.session_secret
        second = self._app(tmp_path).state.settings.session_secret
        assert first
        assert first == second

    def test_key_differs_between_installs(self, tmp_path):
        # Same password, different installs: not derivable from the password.
        one = self._app(tmp_path / "one").state.settings.session_secret
        two = self._app(tmp_path / "two").state.settings.session_secret
        assert one != two

    def test_password_change_logs_everyone_out(self, tmp_path):
        before = self._app(tmp_path, password="old").state.settings.session_secret
        after = self._app(tmp_path, password="new").state.settings.session_secret
        token = auth.make_token(before)
        assert auth.read_token(before, token) is not None
        assert auth.read_token(after, token) is None

    def test_login_works_and_survives_restart(self, tmp_path):
        client = TestClient(self._app(tmp_path), base_url="https://testserver")
        response = client.post(
            "/login", data={"username": "user", "password": "pw"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 200
        token = client.cookies.get("session")

        # A second app on the same data dir: another worker, or a restart.
        restarted = TestClient(self._app(tmp_path), base_url="https://testserver")
        response = restarted.get(
            "/", headers={"Cookie": f"session={token}"}, follow_redirects=False
        )
        assert response.status_code == 200

    def test_explicit_secret_is_used_verbatim(self, app):
        assert app.state.settings.session_secret == "unit-test-secret"


class TestLogout:
    def test_logout_clears_cookie(self, auth_client):
        response = auth_client.post("/logout", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert "Max-Age=0" in response.headers["set-cookie"]

        assert auth_client.get("/", follow_redirects=False).status_code == 404

    def test_logout_ends_the_session_on_the_server(self, app, auth_client):
        # A copy of the cookie taken before logging out must stop working too.
        copied = auth_client.cookies.get("session")
        auth_client.post("/logout", follow_redirects=False)

        thief = TestClient(app, base_url="https://testserver")
        thief.cookies.set("session", copied)
        assert thief.get("/", follow_redirects=False).status_code == 404
        assert thief.get("/admin", follow_redirects=False).status_code == 303
        upload = thief.post("/upload", files={"file": ("a.png", b"x")})
        assert upload.status_code == 401
        assert thief.post("/admin/clear-all", follow_redirects=False).headers[
            "location"
        ] == "/login"

    def test_logout_keeps_other_sessions(self, app, auth_client):
        other = TestClient(app, base_url="https://testserver")
        assert _login(other, app.state.settings.password).status_code == 303

        auth_client.post("/logout", follow_redirects=False)
        assert other.get("/", follow_redirects=False).status_code == 200

    def test_logout_holds_across_workers_and_restarts(self, settings, auth_client):
        copied = auth_client.cookies.get("session")
        auth_client.post("/logout", follow_redirects=False)

        # Another app on the same database: a second worker, or a restart.
        other_worker = TestClient(create_app(settings), base_url="https://testserver")
        other_worker.cookies.set("session", copied)
        assert other_worker.get("/", follow_redirects=False).status_code == 404

    def test_logout_without_a_session_is_harmless(self, client):
        response = client.post("/logout", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        settings = client.app.state.settings
        with db.connect(settings.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM revoked_sessions").fetchone()[0] == 0
