"""Format-aware ingestion image downscaling (backend/hosted/image_resize.py)."""
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import image_resize

PIL = pytest.importorskip("PIL")
from PIL import Image


def _noise(w, h):
    """A noise image — doesn't compress, so it reliably exceeds the byte trigger."""
    import os
    return Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))


def _jpeg(w, h, q=95):
    buf = io.BytesIO()
    _noise(w, h).save(buf, format="JPEG", quality=q)
    return buf.getvalue()


def _png(im):
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _dims(data):
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def test_oversized_jpeg_is_downscaled_to_max_edge_and_shrinks():
    big = _jpeg(4000, 3000)                      # ~large noise JPEG
    assert len(big) > image_resize._TRIGGER_BYTES
    out, mime = image_resize.downscale_image_if_needed(big, "image/jpeg")
    assert mime == "image/jpeg"
    w, h = _dims(out)
    assert max(w, h) == image_resize._MAX_EDGE_PX      # long edge bounded to 1568
    assert abs((w / h) - (4000 / 3000)) < 0.02         # aspect ratio preserved
    assert len(out) < len(big)                          # actually smaller


def test_small_image_passes_through_byte_identical():
    small = _jpeg(400, 300, q=80)
    assert len(small) <= image_resize._TRIGGER_BYTES
    out, mime = image_resize.downscale_image_if_needed(small, "image/jpeg")
    assert out == small and mime == "image/jpeg"        # untouched, zero loss


def test_png_screenshot_that_fits_after_resize_stays_png(monkeypatch):
    # Screenshot-like PNG (compressible): once resized it stays under the PNG
    # fallback threshold, so the lossless format is preserved (no JPEG ringing on
    # text). Force the downscale path and a generous PNG budget deterministically.
    monkeypatch.setattr(image_resize, "_TRIGGER_BYTES", 1000)
    monkeypatch.setattr(image_resize, "_PNG_FALLBACK_BYTES", 50_000_000)
    im = Image.new("RGB", (3000, 2000), (255, 255, 255))
    for x in range(0, 3000, 6):                          # thin vertical rules ~ UI/text
        for y in range(2000):
            im.putpixel((x, y), (0, 0, 0))
    data = _png(im)
    assert len(data) > 1000
    out, mime = image_resize.downscale_image_if_needed(data, "image/png")
    assert mime == "image/png"                            # stayed lossless PNG
    assert max(_dims(out)) == image_resize._MAX_EDGE_PX


def test_png_photo_too_big_falls_back_to_jpeg():
    # noise saved as PNG at 1568px is still multi-MB → must fall back to JPEG
    photo_png = _png(_noise(3000, 3000))
    assert len(photo_png) > image_resize._TRIGGER_BYTES
    out, mime = image_resize.downscale_image_if_needed(photo_png, "image/png")
    assert mime == "image/jpeg"                          # PNG photo → JPEG fallback
    assert max(_dims(out)) == image_resize._MAX_EDGE_PX
    assert len(out) < len(photo_png)


def test_corrupt_bytes_returns_original_and_never_raises():
    junk = b"\xff\xd8\xff" + b"not a real image" * 200000   # >trigger, invalid
    assert len(junk) > image_resize._TRIGGER_BYTES
    out, mime = image_resize.downscale_image_if_needed(junk, "image/jpeg")
    assert out == junk and mime == "image/jpeg"


def test_animated_gif_is_left_intact(monkeypatch):
    # Re-encoding would flatten the animation, so an animated GIF is never touched.
    monkeypatch.setattr(image_resize, "_TRIGGER_BYTES", 1000)   # force the path
    frames = [_noise(300, 300) for _ in range(3)]               # noise → over 1000 bytes
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=100, loop=0)
    gif = buf.getvalue()
    assert len(gif) > 1000
    out, mime = image_resize.downscale_image_if_needed(gif, "image/gif")
    assert out == gif                                    # animation preserved untouched


def test_empty_input_is_a_noop():
    assert image_resize.downscale_image_if_needed(b"", "image/jpeg") == (b"", "image/jpeg")
