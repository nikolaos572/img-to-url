"""Password login and signed session cookies.

The cookie holds an expiry timestamp and a random session id, signed with
HMAC-SHA256, so any uvicorn worker can validate it without a session table.
Logging out records the session id in the database (revoked_sessions), which
every worker checks, so a logged-out cookie stops working everywhere.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import secrets
import time
from dataclasses import dataclass

from fastapi import Request, Response

from app import db
from app.config import SESSION_DAYS, Settings

SESSION_COOKIE = "session"
SESSION_MAX_AGE = SESSION_DAYS * 24 * 60 * 60  # 30 days, in seconds
USERNAME = "user"


def derive_session_secret(install_key: str, password: str) -> str:
    """Cookie-signing key used when SESSION_SECRET is not set.

    Combining the password with a random per-install key (kept server-side in
    the database) keeps the property that changing APP_PASSWORD logs every
    session out, without turning each cookie into an offline password-guessing
    oracle — which a key derived from the password alone would be.
    """
    return hmac.new(install_key.encode(), password.encode(), hashlib.sha256).hexdigest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


@dataclass(frozen=True)
class Session:
    """A session read from a token with a valid signature."""

    session_id: str
    expires_at: int


def make_token(secret: str, now: float | None = None) -> str:
    """Create a token for a new session that expires SESSION_DAYS from ``now``.

    The signed payload is "<expiry>:<session id>"; the random id is what
    logging out revokes.
    """
    expires_at = int((time.time() if now is None else now)) + SESSION_MAX_AGE
    payload = f"{expires_at}:{secrets.token_urlsafe(16)}".encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64encode(payload)}.{_b64encode(signature)}"


def read_token(secret: str, token: str, now: float | None = None) -> Session | None:
    """Return the token's session if its signature is valid and it hasn't expired.

    Revocation is not checked here; see current_session().
    """
    try:
        payload_part, signature_part = token.split(".")
        payload = _b64decode(payload_part)
        signature = _b64decode(signature_part)
    except (ValueError, TypeError):
        return None

    expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None

    try:
        # Tokens from before session ids existed hold only the expiry and
        # are rejected here: they could never be logged out.
        expires_part, session_id = payload.decode().split(":")
        expires_at = int(expires_part)
    except (ValueError, UnicodeDecodeError):
        return None
    if not session_id:
        return None

    current = int(time.time() if now is None else now)
    return Session(session_id, expires_at) if current < expires_at else None


def check_login(username: str, password: str, settings: Settings) -> bool:
    if not username or not password:
        return False
    user_ok = hmac.compare_digest(username.encode(), USERNAME.encode())
    password_ok = hmac.compare_digest(password.encode(), settings.password.encode())
    return user_ok and password_ok


def current_session(request: Request, settings: Settings) -> Session | None:
    """The request's session, if its cookie is valid and it wasn't logged out."""
    token = request.cookies.get(SESSION_COOKIE, "")
    session = read_token(settings.session_secret, token) if token else None
    if session is None or db.is_session_revoked(settings.db_path, session.session_id):
        return None
    return session


def is_authenticated(request: Request, settings: Settings) -> bool:
    return current_session(request, settings) is not None


def set_session_cookie(response: Response, settings: Settings) -> None:
    token = make_token(settings.session_secret)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def end_session(request: Request, response: Response, settings: Settings) -> None:
    """Log out: revoke the request's session on the server and drop its cookie.

    Revoking matters because deleting the cookie only affects this browser;
    a copy of the cookie taken earlier would otherwise stay valid until it
    expires.
    """
    session = current_session(request, settings)
    if session is not None:
        db.revoke_session(settings.db_path, session.session_id, session.expires_at)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
