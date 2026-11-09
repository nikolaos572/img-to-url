"""Tests for the /upload endpoint (validation, size limits, storage)."""

import re
from pathlib import Path

import pytest
from starlette.requests import Request

from app import db

from .conftest import make_jpeg, make_png, upload


def _upload_ok(auth_client, name, content):
    response = upload(auth_client, name, content)
    assert response.status_code == 200, response.text
    return response.json()


class TestUploadValidation:
    def test_valid_png(self, auth_client):
        data = _upload_ok(auth_client, "photo.png", make_png(64))
        assert re.fullmatch(
            r"image-[0-9a-f]{16}-[0-9a-f]{16}\.png", data["filename"]
        )
        assert data["url"] == "https://images.example.com/" + data["filename"]
        assert data["size"] == len(make_png(64))

        settings = auth_client.app.state.settings
        assert (settings.images_dir / data["filename"]).is_file()
        row = db.get_image(settings.db_path, data["filename"])
        assert row is not None
        assert row["size"] == len(make_png(64))

    def test_valid_jpeg_and_webp_extensions(self, auth_client):
        for name, content in [
            ("pic.jpg", make_jpeg(10)),
            ("pic.jpeg", make_jpeg(10)),
            ("pic.webp", b"RIFF\x00\x00\x00\x00WEBP"),
            ("pic.gif", b"GIF89a" + b"\x00" * 8),
            ("pic.bmp", b"BM\x36\x00\x00\x00"),
            ("pic.avif", b"\x00\x00\x00\x18ftypavi f"),
            ("pic.ico", b"\x00\x00\x01\x00\x01\x00\x30\x30"),
        ]:
            data = _upload_ok(auth_client, name, content)
            assert data["filename"].endswith("." + name.rsplit(".", 1)[1])

    def test_uppercase_extension_normalized(self, auth_client):
        data = _upload_ok(auth_client, "PHOTO.PNG", make_png(8))
        assert data["filename"].endswith(".png")

    def test_rejects_non_image_extensions(self, auth_client):
        for name, content in [
            ("script.sh", b"#!/bin/sh\nrm -rf /\n"),
            ("script.py", b"print('hi')"),
            ("app.exe", b"MZ\x90\x00"),
            ("doc.pdf", b"%PDF-1.4"),
            ("vector.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
            ("noext", b"\x89PNG\r\n\x1a\n"),
        ]:
            response = upload(auth_client, name, content)
            assert response.status_code == 415, (name, response.status_code)
            assert "image files" in response.json()["detail"]

    def test_rejects_image_extension_with_fake_content(self, auth_client):
        response = upload(auth_client, "trojan.png", b"#!/bin/sh\necho pwned\n")
        assert response.status_code == 400
        assert "not a recognized image" in response.json()["detail"]

        response = upload(auth_client, "note.png", b"plain text pretending to be an image")
        assert response.status_code == 400

    def test_rejects_empty_file(self, auth_client):
        response = upload(auth_client, "empty.png", b"")
        assert response.status_code == 400

    def test_rejects_request_without_file_field(self, auth_client):
        response = auth_client.post(
            "/upload", files={"image": ("x.png", make_png(8))}, follow_redirects=False
        )
        assert response.status_code == 415
        assert response.json()["detail"] == "No image file was provided."


class TestSizeLimit:
    def test_rejects_oversized_file(self, auth_client):
        big = make_png(1024 * 1024 + 10)  # fixture limit is 1 MB
        response = upload(auth_client, "big.png", big)
        assert response.status_code == 413
        assert "maximum size" in response.json()["detail"]
        # Nothing must be stored for a rejected upload.
        assert list(auth_client.app.state.settings.images_dir.iterdir()) == []

    def test_accepts_file_just_under_limit(self, auth_client):
        ok = make_png(1024 * 1024 - 100)
        data = _upload_ok(auth_client, "edge.png", ok)
        assert data["size"] == len(ok)

    def test_custom_limit(self, tmp_path):
        from app.config import Settings
        from app.main import create_app
        from fastapi.testclient import TestClient

        settings = Settings(
            password="pw",
            base_url="http://localhost:8000",
            max_size_mb=0.01,  # ~10 KB
            expiration_hours=0,
            data_dir=tmp_path,
            port=8000,
            workers=1,
            session_secret="s",
        )
        app = create_app(settings)
        with TestClient(app) as raw_client:
            raw_client.post("/login", data={"username": "user", "password": "pw"})
            response = upload(raw_client, "small.png", make_png(5 * 1024))
            assert response.status_code == 200
            response = upload(raw_client, "huge.png", make_png(50 * 1024))
            assert response.status_code == 413


def _spy_on_form_parsing(monkeypatch) -> list:
    """Record every Request.form() call — i.e. every time a body is parsed
    (and a multipart upload buffered or spooled to disk)."""
    calls = []
    original = Request.form

    def spy(self, *args, **kwargs):
        calls.append(self.url.path)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Request, "form", spy)
    return calls


class TestRequestBodyLimits:
    """Bodies are refused before anything buffers or spools them."""

    def test_anonymous_upload_body_is_never_parsed(self, client, monkeypatch):
        calls = _spy_on_form_parsing(monkeypatch)
        response = upload(client, "x.png", make_png(64))
        assert response.status_code == 401
        assert calls == []

    def test_oversized_declared_length_rejected_up_front(self, auth_client, monkeypatch):
        calls = _spy_on_form_parsing(monkeypatch)
        response = auth_client.post(
            "/upload",
            content=b"--x--\r\n",
            headers={
                "content-type": "multipart/form-data; boundary=x",
                "content-length": str(50 * 1024 * 1024),  # fixture limit: 1 MB
            },
            follow_redirects=False,
        )
        assert response.status_code == 413
        assert "maximum size" in response.json()["detail"]
        assert calls == []

    def test_oversized_chunked_body_is_cut_off(self, auth_client):
        # Chunked transfer has no Content-Length to check up front, so the
        # running byte count has to stop it.
        head = (
            b"--x\r\n"
            b'Content-Disposition: form-data; name="file"; filename="big.png"\r\n'
            b"Content-Type: image/png\r\n\r\n"
        )
        chunks = [head, make_png()] + [b"\0" * 65536] * 40  # ~2.5 MB
        response = auth_client.post(
            "/upload",
            content=iter(chunks),
            headers={"content-type": "multipart/form-data; boundary=x"},
            follow_redirects=False,
        )
        assert response.status_code == 413
        assert list(auth_client.app.state.settings.images_dir.iterdir()) == []

    def test_login_body_is_capped(self, client):
        response = client.post(
            "/login",
            data={"username": "user", "password": "x" * (100 * 1024)},
            follow_redirects=False,
        )
        assert response.status_code == 413
        assert "session=" not in response.headers.get("set-cookie", "")


class TestUploadStorage:
    def test_two_uploads_get_distinct_names(self, auth_client):
        first = _upload_ok(auth_client, "a.png", make_png(1))
        second = _upload_ok(auth_client, "a.png", make_png(2))
        assert first["filename"] != second["filename"]

    def test_upload_counted_in_db(self, auth_client):
        _upload_ok(auth_client, "a.png", make_png(3))
        _upload_ok(auth_client, "b.png", make_png(4))
        settings = auth_client.app.state.settings
        assert db.count_images(settings.db_path) == 2
        assert db.total_size(settings.db_path) == len(make_png(3)) + len(make_png(4))

    def test_failed_write_leaves_nothing_behind(self, auth_client, monkeypatch):
        def disk_full(path, data):
            with path.open("wb") as fh:
                fh.write(data[:4])  # partially written when the disk fills up
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(Path, "write_bytes", disk_full)
        with pytest.raises(OSError):
            upload(auth_client, "a.png", make_png(100))

        # No orphan file (it would be publicly served) and no dead DB row.
        settings = auth_client.app.state.settings
        assert list(settings.images_dir.iterdir()) == []
        assert db.count_images(settings.db_path) == 0
