"""Shared pytest fixtures.

IMPORTANT: these environment variables are set at import time, BEFORE any
test imports ``app.main`` (whose module import builds the default app from
the environment). This keeps tests hermetic and avoids touching ./data.
"""

from __future__ import annotations

import os
import shutil
import tempfile

_TEST_PASSWORD = "correct horse battery staple"

os.environ["APP_PASSWORD"] = _TEST_PASSWORD
_DATA_TMP = tempfile.mkdtemp(prefix="img2url-tests-")
os.environ["DATA_DIR"] = _DATA_TMP

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_DATA_TMP, ignore_errors=True)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    """Isolated settings: 1 MB limit, no expiration, https base URL."""
    return Settings(
        password=_TEST_PASSWORD,
        base_url="https://images.example.com",
        max_size_mb=1,
        expiration_hours=0,
        data_dir=tmp_path,
        port=8000,
        workers=1,
        session_secret="unit-test-secret",
    )


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def client(app) -> TestClient:
    # The test settings use an https:// base URL, which makes the session
    # cookie Secure — so the TestClient must also speak https, or the cookie
    # jar will (correctly) refuse to send the cookie back over plain http.
    return TestClient(app, base_url="https://testserver")


@pytest.fixture()
def auth_client(client) -> TestClient:
    """A client that is already logged in as 'user'."""
    response = client.post(
        "/login",
        data={"username": "user", "password": client.app.state.settings.password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


# ------------------------------------------------------------------ helpers

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff\xe0"


def make_png(padding: int = 0) -> bytes:
    """Minimal fake-but-magic-valid PNG of arbitrary size."""
    return PNG_MAGIC + b"\x00" * padding


def make_jpeg(padding: int = 0) -> bytes:
    return JPEG_MAGIC + b"\x00" * padding


def upload(client, filename: str, content: bytes):
    """POST a file to /upload without following redirects."""
    return client.post(
        "/upload", files={"file": (filename, content)}, follow_redirects=False
    )
