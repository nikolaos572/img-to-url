"""Remove all metadata from uploaded images by re-encoding them.

Photos often carry far more than the picture: EXIF with the GPS position,
phone or camera model, serial numbers and the time it was taken, plus XMP and
IPTC blocks, embedded thumbnails, comments, PNG text chunks and ICC profiles
that name the device. Rather than hunting for each such block in eight
formats, every upload is decoded and written out as a new file of the same
format holding only what is needed to show the same picture:

- EXIF orientation is applied to the pixels, so photos stay upright;
- colors tagged with an ICC profile are converted to sRGB, which browsers
  assume for untagged images, so they still look right;
- transparency, and for animations every frame, its duration and the loop
  count.

Everything else is dropped, bytes appended after the image data included.
JPEGs are re-encoded with their own quantization tables and chroma
subsampling, so quality and size stay close to the original. PNG, GIF, BMP,
ICO and lossless WebP stay lossless.
"""

from __future__ import annotations

import asyncio
import io
import warnings
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageCms, ImageOps, ImageSequence, JpegImagePlugin

#: The only decoders Pillow may use. The JPEG one also opens JPEGs with
#: extra images attached (a phone's HDR gain map or depth map, say), which
#: Pillow then reports as format "MPO".
_DECODERS = ("PNG", "JPEG", "GIF", "WEBP", "BMP", "ICO", "AVIF")

#: The format each allowed extension stands for.
EXTENSION_FORMATS = {
    "png": "PNG",
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "gif": "GIF",
    "webp": "WEBP",
    "bmp": "BMP",
    "ico": "ICO",
    "avif": "AVIF",
}

#: The extension to store an image under when its name's doesn't match it.
FORMAT_EXTENSIONS = {
    "PNG": "png",
    "JPEG": "jpg",
    "GIF": "gif",
    "WEBP": "webp",
    "BMP": "bmp",
    "ICO": "ico",
    "AVIF": "avif",
}

#: Most pixels an upload may decode to, all animation frames together. A
#: decoded pixel takes 3-4 bytes, held a few times over while re-encoding,
#: so this keeps one upload to a few hundred MB while a 48-megapixel photo
#: still fits. Checked from the file header, before anything is decoded.
MAX_PIXELS = 50_000_000

#: Re-encodes run on their own threads, this many per worker at most; more
#: parallel uploads wait their turn. That bounds the memory decoded pictures
#: take, and keeps it from piling up: the C allocator keeps freed memory per
#: thread, so a decode on every thread of a general pool would leave each
#: one holding a whole decoded photo's worth.
MAX_PARALLEL = 2

#: Quality for re-encoding lossy WebP and AVIF, whose original setting can't
#: be read back from the file: high, so the second encode adds no visible loss.
LOSSY_QUALITY = 90

_SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
_executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL, thread_name_prefix="img-to-url-sanitize")

# Pillow's own decompression-bomb check warns from about 89 million pixels
# per frame and fails from twice that. MAX_PIXELS is stricter and counts
# every frame, so the warning is only noise in the log; the error is still
# caught below and reported as "too large".
warnings.simplefilter("ignore", Image.DecompressionBombWarning)


class InvalidImage(ValueError):
    """The upload can't be stored as an image. The message is for the user."""


async def strip_metadata_async(data: bytes, ext: str) -> tuple[bytes, str]:
    """strip_metadata() on the re-encoding threads, without blocking the event loop."""
    return await asyncio.get_running_loop().run_in_executor(_executor, strip_metadata, data, ext)


def strip_metadata(data: bytes, ext: str) -> tuple[bytes, str]:
    """Re-encode an uploaded image so the file holds only the picture.

    Returns the new file's bytes and the extension to store it under:
    ``ext`` when it matches the content, otherwise the content's own (a PNG
    named .jpg becomes .png), so the link's extension and Content-Type always
    fit the bytes. Raises InvalidImage when ``data`` isn't a readable image
    in one of the allowed formats, or has more than MAX_PIXELS pixels.
    """
    try:
        with Image.open(io.BytesIO(data), formats=_DECODERS) as source:
            fmt = "JPEG" if source.format == "MPO" else source.format
            frames, options = _decode(source, fmt, data)
    except InvalidImage:
        raise
    except Image.DecompressionBombError as exc:
        raise _too_large() from exc
    except Exception as exc:  # Pillow raises many types for damaged files
        raise InvalidImage("The file could not be read as an image. It may be damaged.") from exc

    first, *rest = frames
    if rest:
        options.update(save_all=True, append_images=rest)
    out = io.BytesIO()
    first.save(out, format=fmt, **options)

    stored_ext = ext if EXTENSION_FORMATS.get(ext) == fmt else FORMAT_EXTENSIONS[fmt]
    return out.getvalue(), stored_ext


def _decode(source: Image.Image, fmt: str, data: bytes) -> tuple[list[Image.Image], dict]:
    """The frames to write, as clean copies, and the options to save them with."""
    # A JPEG's extra MPO images (previews, depth or gain maps) aren't the
    # picture itself; only the main image is kept.
    count = 1 if fmt == "JPEG" else getattr(source, "n_frames", 1)
    if source.width * source.height * count > MAX_PIXELS:
        raise _too_large()

    options: dict = {}
    if count == 1:
        frames = [_clean(_to_srgb(ImageOps.exif_transpose(source)))]
    else:
        if "loop" in source.info:  # absent in a GIF means "play once"
            options["loop"] = source.info["loop"]
        frames, durations = [], []
        # Pillow hands out every frame fully composited, so each is written
        # as a complete picture and the encoder works out the differences.
        for frame in ImageSequence.Iterator(source):
            frame.load()  # WebP and AVIF only know a frame's duration once loaded
            durations.append(frame.info.get("duration", 0))
            frames.append(_clean(_to_srgb(frame).convert("RGBA")))
        options["duration"] = durations
        if fmt == "GIF" and any(frame.getextrema()[3][0] < 255 for frame in frames):
            # GIF frames are drawn over the previous one unless it's cleared
            # first; without that, pixels that turn transparent would keep
            # showing the old frame.
            options["disposal"] = 2

    if fmt == "JPEG":
        options["qtables"] = source.quantization
        subsampling = JpegImagePlugin.get_sampling(source)
        if subsampling != -1:
            options["subsampling"] = subsampling
    elif fmt == "WEBP":
        if _webp_is_lossless(data):
            options["lossless"] = True
        else:
            options["quality"] = LOSSY_QUALITY
    elif fmt == "AVIF":
        options["quality"] = LOSSY_QUALITY
    elif fmt == "ICO":
        options["sizes"] = sorted(source.info["sizes"])
    return frames, options


def _too_large() -> InvalidImage:
    return InvalidImage(
        f"The image is too large: the limit is {MAX_PIXELS // 1_000_000} million "
        "pixels, counting every frame of an animation."
    )


def _to_srgb(image: Image.Image) -> Image.Image:
    """Convert colors described by an embedded ICC profile to plain sRGB.

    CMYK becomes RGB, which every format here can store. An unusable profile
    is ignored rather than failing the upload. Grayscale and palette images
    keep their pixels as they are; the profile goes either way.
    """
    icc = image.info.get("icc_profile")
    if icc and image.mode in ("RGB", "RGBA", "CMYK"):
        try:
            return ImageCms.profileToProfile(
                image,
                ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                _SRGB,
                outputMode="RGBA" if image.mode == "RGBA" else "RGB",
            )
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass
    return image.convert("RGB") if image.mode == "CMYK" else image


def _clean(image: Image.Image) -> Image.Image:
    """Forget everything read from the file except which color is transparent.

    ``image`` is always a new image made by the caller, never the source, so
    clearing its info leaves nothing behind for the encoder to copy: no EXIF,
    XMP, ICC profile, comment, text chunk or DPI.
    """
    image.info = (
        {"transparency": image.info["transparency"]} if "transparency" in image.info else {}
    )
    return image


def _webp_is_lossless(data: bytes) -> bool:
    """Whether every image in a WebP file is lossless (VP8L), not lossy (VP8)."""
    kinds = set()
    for fourcc, start, end in _riff_chunks(data, 12, len(data)):
        if fourcc == b"ANMF":  # animation frame: 16 header bytes, then its chunks
            kinds.update(inner for inner, _, _ in _riff_chunks(data, start + 16, end))
        else:
            kinds.add(fourcc)
    return b"VP8L" in kinds and b"VP8 " not in kinds


def _riff_chunks(data: bytes, start: int, end: int):
    """Yield (fourcc, payload start, payload end) for the RIFF chunks in a range."""
    while start + 8 <= end:
        size = int.from_bytes(data[start + 4 : start + 8], "little")
        yield data[start : start + 4], start + 8, min(start + 8 + size, end)
        start += 8 + size + (size & 1)  # payloads are padded to an even length
