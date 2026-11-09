"""Password login and signed, stateless session cookies.

The cookie holds only a HMAC-SHA256-signed expiry timestamp, so any uvicorn
worker can validate it without shared state or a session table.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import time

from fastapi import Request, Response

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


def make_token(secret: str, now: float | None = None) -> str:
    """Create a session token that expires SESSION_DAYS from ``now``."""
    expires_at = int((time.time() if now is None else now)) + SESSION_MAX_AGE
    payload = str(expires_at).encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64encode(payload)}.{_b64encode(signature)}"


def verify_token(secret: str, token: str, now: float | None = None) -> bool:
    """Return True when the token has a valid signature and has not expired."""
    try:
        payload_part, signature_part = token.split(".")
        payload = _b64decode(payload_part)
        signature = _b64decode(signature_part)
    except (ValueError, TypeError):
        return False

    expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return False

    try:
        expires_at = int(payload.decode())
    except (ValueError, UnicodeDecodeError):
        return False

    current = int(time.time() if now is None else now)
    return current < expires_at


def check_login(username: str, password: str, settings: Settings) -> bool:
    if not username or not password:
        return False
    user_ok = hmac.compare_digest(username.encode(), USERNAME.encode())
    password_ok = hmac.compare_digest(password.encode(), settings.password.encode())
    return user_ok and password_ok


def is_authenticated(request: Request, settings: Settings) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return False
    return verify_token(settings.session_secret, token)


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


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
