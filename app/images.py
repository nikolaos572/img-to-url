"""Image file validation, naming and serving helpers.

SVG is intentionally NOT in the allowlist: serving uploaded SVG inline lets a
crafted file run scripts on the public image URL. Every other common web image
format is accepted.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

ALLOWED_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "avif", "ico"})

EXTENSION_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "avif": "image/avif",
    "ico": "image/x-icon",
}

# Served/deleted filenames must match this exact shape, e.g.
# image-0123456789abcdef-fedcba9876543210.png
FILENAME_RE = re.compile(
    r"^image-[0-9a-f]{16}-[0-9a-f]{16}\.(?:"
    + "|".join(sorted(ALLOWED_EXTENSIONS))
    + r")$"
)

_RANDOM_PART = secrets.token_hex


def file_extension(filename: str | None) -> str | None:
    """Return the lowercased extension of a filename, or None if it has none."""
    if not filename:
        return None
    _, separator, ext = filename.rpartition(".")
    if not separator:
        return None  # no dot anywhere in the name -> no extension
    if not ext or not ext.isalnum() or len(ext) > 5:
        return None
    return ext.lower()


def looks_like_image(data: bytes) -> bool:
    """Check magic bytes against known image signatures (defense in depth)."""
    if not data:
        return False
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith(b"\xff\xd8\xff"):
        return True  # JPEG
    if data.startswith((b"GIF87a", b"GIF89a")):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data.startswith(b"BM"):
        return True  # BMP
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12].startswith(b"avi"):
        return True  # AVIF
    if data.startswith(b"\x00\x00\x01\x00"):
        return True  # ICO
    return False


def random_filename(ext: str) -> str:
    """Generate a long random filename like image-XXXX(16)-XXXX(16).ext."""
    return f"image-{_RANDOM_PART(8)}-{_RANDOM_PART(8)}.{ext}"


def is_valid_filename(filename: str) -> bool:
    # fullmatch, not match: "$" also matches just before a trailing newline.
    return bool(FILENAME_RE.fullmatch(filename))


def media_type_for(filename: str) -> str:
    ext = file_extension(filename)
    return EXTENSION_MEDIA_TYPES.get(ext or "", "application/octet-stream")


def image_url(base_url: str, filename: str) -> str:
    return f"{base_url.rstrip('/')}/{filename}"


def human_size(size_bytes: int) -> str:
    """Format a byte count for display, e.g. 1234567 -> '1.2 MB'."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_timestamp(ts: int) -> str:
    """Render a unix timestamp as a readable UTC date."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
