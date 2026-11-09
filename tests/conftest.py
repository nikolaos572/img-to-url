"""Shared pytest fixtures.

IMPORTANT: these environment variables are set at import time, BEFORE any
test imports ``app.main`` (whose module import builds the default app from
the environment). This keeps tests hermetic and avoids touching ./data.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile

_TEST_PASSWORD = "correct horse battery staple"

os.environ["APP_PASSWORD"] = _TEST_PASSWORD
_DATA_TMP = tempfile.mkdtemp(prefix="img2url-tests-")
os.environ["DATA_DIR"] = _DATA_TMP

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.sanitize import EXTENSION_FORMATS  # noqa: E402


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

def make_image(ext: str = "png", size: tuple[int, int] = (16, 16)) -> bytes:
    """A small, real, metadata-free image in the format of an allowed extension.

    Uploads are decoded and re-encoded (app/sanitize.py), so test uploads
    must be real images.
    """
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 60)).save(buffer, EXTENSION_FORMATS[ext])
    return buffer.getvalue()


_PNG = make_image("png")
_JPEG = make_image("jpg")


def make_png(padding: int = 0) -> bytes:
    """A real PNG followed by ``padding`` extra bytes, to set an exact upload size.

    Decoders ignore bytes after the image data, and uploads drop them.
    """
    return _PNG + b"\x00" * padding


def make_jpeg(padding: int = 0) -> bytes:
    """A real JPEG followed by ``padding`` extra bytes (see make_png)."""
    return _JPEG + b"\x00" * padding


def stored_bytes(client, filename: str) -> bytes:
    """The file the app stored (and serves) for an uploaded image."""
    return (client.app.state.settings.images_dir / filename).read_bytes()


def upload(client, filename: str, content: bytes):
    """POST a file to /upload without following redirects."""
    return client.post(
        "/upload", files={"file": (filename, content)}, follow_redirects=False
    )
