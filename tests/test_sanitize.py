"""Uploaded images are re-encoded so the file holds only the picture."""

import io

import pytest
from PIL import (
    Image,
    ImageChops,
    ImageCms,
    ImageOps,
    ImageSequence,
    JpegImagePlugin,
    PngImagePlugin,
)

from app import images, sanitize
from app.sanitize import InvalidImage, strip_metadata

# Planted in the metadata of the test files; none may survive.
SECRETS = (b"PhoneMaker", b"SecretModel", b"JaneDoe", b"SecretComment", b"SecretText")
METADATA_KEYS = {"exif", "xmp", "XML:com.adobe.xmp", "icc_profile", "comment", "Author"}
XMP = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
    b'xmlns:dc="http://purl.org/dc/elements/1.1/" dc:creator="JaneDoe"/>'
    b"</rdf:RDF></x:xmpmeta>"
)
SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _exif(orientation=None):
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make
    exif[0x0110] = "SecretModel"  # Model
    exif.get_ifd(0x8825).update({1: "N", 2: (40.0, 18.0, 0.0), 3: "E", 4: (21.0, 47.0, 0.0)})
    if orientation:
        exif[0x0112] = orientation
    return exif


def _picture(mode="RGB", size=(48, 32)):
    """A picture with some structure, so changed pixels would show."""
    ramp = Image.linear_gradient("L").resize(size)
    picture = Image.merge(
        "RGB", (ramp, ramp.transpose(Image.Transpose.FLIP_LEFT_RIGHT), Image.new("L", size, 90))
    )
    if mode == "RGBA":
        picture.putalpha(ramp.transpose(Image.Transpose.ROTATE_180))
    elif mode == "P":
        picture = picture.quantize(16)
    elif mode == "L":
        picture = ramp
    return picture


def _encode(image, fmt, **params):
    buffer = io.BytesIO()
    image.save(buffer, fmt, **params)
    return buffer.getvalue()


def _open(data):
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def _difference(a, b):
    """Largest difference of any channel of any pixel, alpha included."""
    a, b = a.convert("RGBA"), b.convert("RGBA")
    assert a.size == b.size
    return max(high for _, high in ImageChops.difference(a, b).getextrema())


def _read_frames(data):
    """Every frame fully composited, its duration, and the loop count."""
    with Image.open(io.BytesIO(data)) as image:
        frames = []
        for frame in ImageSequence.Iterator(image):
            frame.load()
            frames.append((frame.convert("RGBA"), frame.info.get("duration")))
        return frames, image.info.get("loop")


class TestMetadataIsRemoved:
    # Every kind of metadata Pillow can write into each format.
    WITH_METADATA = {
        "jpg": lambda: _encode(
            _picture(), "JPEG", exif=_exif(), xmp=XMP, comment=b"SecretComment",
            icc_profile=SRGB_ICC,
        ),
        "png": lambda: _encode(
            _picture("RGBA"), "PNG", exif=_exif(), icc_profile=SRGB_ICC, pnginfo=_png_text(),
        ),
        "webp": lambda: _encode(
            _picture("RGBA"), "WEBP", exif=_exif(), xmp=XMP, icc_profile=SRGB_ICC,
        ),
        "gif": lambda: _encode(_picture("P"), "GIF", comment=b"SecretComment"),
        "avif": lambda: _encode(_picture(), "AVIF", exif=_exif(), xmp=XMP, icc_profile=SRGB_ICC),
    }

    @pytest.mark.parametrize("ext", sorted(WITH_METADATA))
    def test_nothing_but_the_picture_is_left(self, ext):
        original = self.WITH_METADATA[ext]()
        assert any(secret in original for secret in SECRETS)

        clean, stored_ext = strip_metadata(original, ext)

        assert stored_ext == ext
        for secret in SECRETS:
            assert secret not in clean
        image = _open(clean)
        assert not METADATA_KEYS & set(image.info), image.info.keys()
        assert not image.getexif()

    def test_bytes_after_the_image_are_dropped(self):
        payload = b"<html><script>alert(1)</script></html>"
        clean, _ = strip_metadata(_encode(_picture(), "PNG") + payload, "png")
        assert payload not in clean


def _png_text():
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "SecretText")
    info.add_itxt("XML:com.adobe.xmp", XMP.decode())
    return info


class TestThePictureIsKept:
    @pytest.mark.parametrize(
        "ext,original",
        [
            ("png", lambda: _encode(_picture("RGBA"), "PNG")),
            ("png", lambda: _encode(_picture("P"), "PNG", transparency=3)),
            ("png", lambda: _encode(_picture("L"), "PNG")),
            ("gif", lambda: _encode(_picture("P"), "GIF", transparency=3)),
            ("webp", lambda: _encode(_picture("RGBA"), "WEBP", lossless=True)),
            ("bmp", lambda: _encode(_picture(), "BMP")),
            ("bmp", lambda: _encode(_picture("RGBA"), "BMP")),
            ("ico", lambda: _encode(_picture("RGBA", (32, 32)), "ICO")),
        ],
    )
    def test_lossless_formats_keep_every_pixel(self, ext, original):
        source = original()
        clean, _ = strip_metadata(source, ext)
        before, after = _open(source), _open(clean)
        assert after.mode == before.mode  # an alpha channel or palette stays
        assert after.info.get("transparency") == before.info.get("transparency")
        assert _difference(before, after) == 0

    @pytest.mark.parametrize(
        "ext,original,tolerance",
        [
            ("jpg", lambda: _encode(_picture(), "JPEG", quality=90), 8),
            ("webp", lambda: _encode(_picture("RGBA"), "WEBP", quality=80), 12),
            ("avif", lambda: _encode(_picture(), "AVIF", quality=80), 12),
        ],
    )
    def test_lossy_formats_stay_close(self, ext, original, tolerance):
        source = original()
        clean, _ = strip_metadata(source, ext)
        assert _difference(_open(source), _open(clean)) <= tolerance

    def test_jpeg_keeps_its_quantization_tables_and_subsampling(self):
        source = _encode(_picture(), "JPEG", quality=70, subsampling="4:4:4")
        clean, _ = strip_metadata(source, "jpg")
        before, after = _open(source), _open(clean)
        assert after.quantization == before.quantization
        assert JpegImagePlugin.get_sampling(after) == 0  # 4:4:4

    def test_webp_stays_lossless_or_lossy(self):
        lossless, _ = strip_metadata(_encode(_picture(), "WEBP", lossless=True), "webp")
        lossy, _ = strip_metadata(_encode(_picture(), "WEBP", quality=80), "webp")
        assert b"VP8L" in lossless and b"VP8 " not in lossless
        assert b"VP8 " in lossy and b"VP8L" not in lossy

    def test_exif_orientation_is_applied_to_the_pixels(self):
        picture = _picture(size=(40, 20))
        source = _encode(picture, "JPEG", quality=95, exif=_exif(orientation=6))

        clean, _ = strip_metadata(source, "jpg")

        after = _open(clean)
        assert after.size == (20, 40)  # turned upright, and no tag left to turn it again
        assert not after.getexif()
        assert _difference(ImageOps.exif_transpose(_open(source)), after) <= 8

    def test_icc_colors_are_converted_to_srgb(self):
        # In this profile, stored pure red means sRGB's green.
        source = _encode(Image.new("RGB", (8, 8), (255, 0, 0)), "PNG",
                         icc_profile=_swapped_primaries_profile())
        clean, _ = strip_metadata(source, "png")
        after = _open(clean)
        assert "icc_profile" not in after.info
        red, green, blue = after.getpixel((4, 4))
        assert red <= 2 and green >= 253 and blue <= 2

    def test_cmyk_jpeg_becomes_rgb(self):
        source = _encode(Image.new("CMYK", (8, 8), (0, 255, 255, 0)), "JPEG")
        clean, _ = strip_metadata(source, "jpg")
        after = _open(clean)
        assert after.mode == "RGB"
        red, green, blue = after.getpixel((4, 4))
        assert red >= 240 and green <= 15 and blue <= 15

    def test_unusable_icc_profile_is_ignored(self):
        source = _encode(_picture(), "PNG", icc_profile=b"not really a profile")
        clean, _ = strip_metadata(source, "png")
        after = _open(clean)
        assert "icc_profile" not in after.info
        assert _difference(_open(source), after) == 0


def _swapped_primaries_profile():
    """An RGB ICC profile like sRGB but with the red and green primaries swapped."""
    profile = bytearray(SRGB_ICC)
    entries = {}
    for index in range(int.from_bytes(profile[128:132], "big")):
        at = 132 + 12 * index  # tag table entry: signature, offset, size
        entries[bytes(profile[at : at + 4])] = at
    red, green = entries[b"rXYZ"], entries[b"gXYZ"]
    profile[red + 4 : red + 12], profile[green + 4 : green + 12] = (
        profile[green + 4 : green + 12],
        profile[red + 4 : red + 12],
    )
    return bytes(profile)


def _moving_square():
    """Three frames: a square moves on, and the pixels it leaves turn transparent."""
    frames = []
    for index, color in enumerate([(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)]):
        frame = Image.new("RGBA", (60, 40), (0, 0, 0, 0))
        frame.paste(Image.new("RGBA", (20, 20), color), (index * 20, 10))
        frames.append(frame)
    return frames


def _opaque_frames():
    frames = [Image.new("RGBA", (60, 40), color) for color in ("red", "green", "blue")]
    for index, frame in enumerate(frames):
        frame.paste(Image.new("RGBA", (10, 10), "black"), (index * 20, 5))
    return frames


def _animation(frames, fmt, **params):
    first, *rest = frames
    return _encode(first, fmt, save_all=True, append_images=rest, duration=[100, 200, 300], **params)


class TestAnimations:
    @pytest.mark.parametrize(
        "ext,original",
        [
            ("gif", lambda: _animation(_moving_square(), "GIF", loop=0, disposal=2)),
            ("gif", lambda: _animation(_moving_square(), "GIF", disposal=2)),  # plays once
            ("gif", lambda: _animation(_opaque_frames(), "GIF", loop=0)),
            ("png", lambda: _animation(_moving_square(), "PNG", loop=2)),  # APNG
            ("webp", lambda: _animation(_moving_square(), "WEBP", loop=3, lossless=True)),
        ],
    )
    def test_frames_durations_and_loop_are_kept(self, ext, original):
        source = original()
        clean, _ = strip_metadata(source, ext)

        (before, loop_before), (after, loop_after) = _read_frames(source), _read_frames(clean)
        assert len(after) == len(before) == 3
        assert [duration for _, duration in after] == [duration for _, duration in before]
        assert loop_after == loop_before
        for (frame_before, _), (frame_after, _) in zip(before, after):
            assert _difference(frame_before, frame_after) == 0

    @pytest.mark.parametrize(
        "ext,original",
        [
            ("webp", lambda: _animation(_opaque_frames(), "WEBP", quality=80)),
            ("avif", lambda: _animation(_opaque_frames(), "AVIF", quality=90)),
        ],
    )
    def test_lossy_animations_keep_frames_and_durations(self, ext, original):
        source = original()
        clean, _ = strip_metadata(source, ext)

        (before, _), (after, _) = _read_frames(source), _read_frames(clean)
        assert [duration for _, duration in after] == [duration for _, duration in before]
        for (frame_before, _), (frame_after, _) in zip(before, after):
            assert _difference(frame_before, frame_after) <= 40


class TestFormatAndExtension:
    def test_every_allowed_extension_has_a_format(self):
        assert set(sanitize.EXTENSION_FORMATS) == images.ALLOWED_EXTENSIONS
        assert set(sanitize.FORMAT_EXTENSIONS.values()) <= images.ALLOWED_EXTENSIONS

    def test_content_decides_the_format(self):
        clean, ext = strip_metadata(_encode(_picture(), "PNG"), "jpg")
        assert ext == "png"
        assert _open(clean).format == "PNG"

    def test_jpeg_spelling_of_the_upload_is_kept(self):
        assert strip_metadata(_encode(_picture(), "JPEG"), "jpeg")[1] == "jpeg"
        assert strip_metadata(_encode(_picture(), "JPEG"), "jpg")[1] == "jpg"

    def test_only_the_main_image_of_a_multi_picture_jpeg_is_kept(self):
        # Phones attach extra images to JPEGs (HDR gain maps, depth maps).
        main, extra = _picture(), _picture().rotate(90)
        source = _encode(main, "MPO", save_all=True, append_images=[extra])
        assert _open(source).format == "MPO"

        clean, ext = strip_metadata(source, "jpg")

        after = _open(clean)
        assert (ext, after.format, getattr(after, "n_frames", 1)) == ("jpg", "JPEG", 1)
        assert _difference(_open(source), after) <= 8


class TestRejected:
    @pytest.mark.parametrize(
        "data",
        [
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,  # PNG signature, no image
            _encode(_picture(), "JPEG")[:200],  # cut off
            b"BM<html><script>alert(1)</script></html>",  # passes a BMP magic check
            b'<svg xmlns="http://www.w3.org/2000/svg"/>',
        ],
    )
    def test_unreadable_files(self, data):
        with pytest.raises(InvalidImage, match="could not be read as an image"):
            strip_metadata(data, "png")

    def test_too_many_pixels(self, monkeypatch):
        monkeypatch.setattr(sanitize, "MAX_PIXELS", 1000)
        strip_metadata(_encode(_picture(size=(30, 30)), "PNG"), "png")  # 900: fine
        with pytest.raises(InvalidImage, match="too large"):
            strip_metadata(_encode(_picture(size=(40, 40)), "PNG"), "png")

    def test_too_many_pixels_over_all_frames(self, monkeypatch):
        monkeypatch.setattr(sanitize, "MAX_PIXELS", 3000)
        strip_metadata(_encode(_opaque_frames()[0], "GIF"), "gif")  # one frame, 2400: fine
        with pytest.raises(InvalidImage, match="counting every frame"):
            strip_metadata(_animation(_opaque_frames(), "GIF"), "gif")  # 3 x 2400

    def test_a_decompression_bomb_is_too_large_not_damaged(self):
        # About 20 KB on disk, 180 million pixels: past Pillow's own hard limit.
        bomb = _encode(Image.new("1", (20_000, 9_000)), "PNG")
        with pytest.raises(InvalidImage, match="too large"):
            strip_metadata(bomb, "png")
