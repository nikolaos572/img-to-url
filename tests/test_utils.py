"""Tests for pure helper functions in app.images."""

import re

import pytest

from app import images


class TestFileExtension:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("photo.JPG", "jpg"),
            ("a.b.png", "png"),
            ("noext", None),
            ("", None),
            (None, None),
        ],
    )
    def test_parsing(self, name, expected):
        assert images.file_extension(name) == expected

    @pytest.mark.parametrize("name", ["evil.sh", "evil.py", "evil.exe", "evil.svg"])
    def test_dangerous_extensions_not_allowed(self, name):
        ext = images.file_extension(name)
        assert ext not in images.ALLOWED_EXTENSIONS


class TestMagicBytes:
    @pytest.mark.parametrize(
        "data",
        [
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff\xe1rest",
            b"GIF89a...",
            b"BM\x00\x00",
            b"RIFF\x00\x00\x00\x00WEBP",
            b"\x00\x00\x00\x18ftypavi f",
            b"\x00\x00\x01\x00\x01\x00",
        ],
    )
    def test_accepts(self, data):
        assert images.looks_like_image(data) is True

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"#!/bin/sh\necho hi",
            b"print('hello')",
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            b"\x00\x00\x00\x18ftypisom",  # mp4 video, not a still image
            b"RIFF____AUDI",
        ],
    )
    def test_rejects(self, data):
        assert images.looks_like_image(data) is False


class TestFilenames:
    def test_random_filename_shape(self):
        name = images.random_filename("png")
        assert re.fullmatch(
            r"image-[0-9a-f]{16}-[0-9a-f]{16}\.png", name
        )
        assert images.is_valid_filename(name)

    def test_random_filename_uniqueness(self):
        names = {images.random_filename("jpg") for _ in range(200)}
        assert len(names) == 200

    @pytest.mark.parametrize(
        "name,valid",
        [
            ("image-" + "a" * 16 + "-" + "b" * 16 + ".png", True),
            ("image-" + "a" * 16 + "-" + "b" * 16 + ".jpeg", True),
            ("image-" + "a" * 16 + "-" + "b" * 16 + ".svg", False),
            ("image-" + "a" * 16 + "-" + "b" * 16 + ".sh", False),
            ("image-" + "a" * 15 + "-" + "b" * 16 + ".png", False),
            ("image-" + "a" * 16 + ".png", False),
            ("../../etc/passwd", False),
            ("", False),
            ("Image-" + "a" * 16 + "-" + "b" * 16 + ".png", False),
            # A regex "$" would also match before a trailing newline.
            ("image-" + "a" * 16 + "-" + "b" * 16 + ".png\n", False),
        ],
    )
    def test_is_valid_filename(self, name, valid):
        assert images.is_valid_filename(name) is valid


class TestFormatting:
    @pytest.mark.parametrize(
        "size,expected",
        [
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.0 KB"),
            (1234567, "1.2 MB"),
            (1024 * 1024 * 1024 * 3, "3.0 GB"),
        ],
    )
    def test_human_size(self, size, expected):
        assert images.human_size(size) == expected

    def test_image_url_joins_base_and_filename(self):
        url = images.image_url("https://images.example.com/", "image-x.png")
        assert url == "https://images.example.com/image-x.png"

    def test_media_type(self):
        assert images.media_type_for("image-a-b.png") == "image/png"
        assert images.media_type_for("image-a-b.jpg") == "image/jpeg"
        assert images.media_type_for("image-a-b.webp") == "image/webp"

    def test_format_timestamp_is_utc(self):
        assert images.format_timestamp(0) == "1970-01-01 00:00 UTC"
