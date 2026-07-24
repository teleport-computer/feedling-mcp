"""Format-aware image downscaling for chat ingestion.

Phone photos are often 3-12MB, but a vision model only uses ~1568px on the long
edge (Anthropic downscales larger images to that internally), and re-sending
multi-MB base64 every turn is wasted payload. So at ingestion we bound an
oversized image to a long edge + re-encode, keeping the result small enough to
store and inline without loss the model would actually notice.

Principles:
- **Only touch oversized images.** An image already under the byte trigger passes
  through byte-identical — zero loss, zero CPU for the common small case.
- **Format-aware.** PNG (usually screenshots with sharp text/UI) stays PNG so
  text edges don't pick up JPEG ringing; JPEG/WebP re-encode at a high quality.
  A PNG *photo* that stays too big after resize falls back to JPEG.
- **Animated GIFs are left intact** (re-encoding would flatten them).
- **Best-effort.** Any Pillow failure returns the original bytes unchanged — a
  slightly-too-large image is better than a rejected send.

Runs in the backend (main CVM) at chat/send ingestion, before the plaintext is
encrypted into the message envelope.
"""
from __future__ import annotations

import io
import logging
import os

log = logging.getLogger("feedling.hosted.image_resize")

# Only images larger than this get opened + re-encoded. Defaults to the stored
# image cap so anything that already fits is never touched.
_TRIGGER_BYTES = int(os.environ.get("FEEDLING_IMAGE_DOWNSCALE_TRIGGER_BYTES", "2000000"))
# Long-edge target. 1568px is Anthropic's internal vision target — larger buys the
# model nothing. Env-tunable if a future model wants more detail.
_MAX_EDGE_PX = int(os.environ.get("FEEDLING_IMAGE_MAX_EDGE_PX", "1568"))
# Lossy re-encode quality for JPEG/WebP. 85 is visually near-lossless for photos.
_QUALITY = int(os.environ.get("FEEDLING_IMAGE_JPEG_QUALITY", "85"))
# A lossless PNG re-encode that stays over this falls back to JPEG (PNG compresses
# photos poorly). Defaults to the stored image cap.
_PNG_FALLBACK_BYTES = int(os.environ.get("FEEDLING_IMAGE_PNG_FALLBACK_BYTES", "2000000"))


def _encode(im, prefer_fmt: str) -> tuple[bytes, str]:
    out = io.BytesIO()
    if prefer_fmt == "PNG":
        im.save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png"
    if prefer_fmt == "WEBP":
        im.save(out, format="WEBP", quality=_QUALITY, method=4)
        return out.getvalue(), "image/webp"
    # JPEG (default, and the fallback for everything else)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.save(out, format="JPEG", quality=_QUALITY, optimize=True)
    return out.getvalue(), "image/jpeg"


def downscale_image_if_needed(data: bytes, mime: str) -> tuple[bytes, str]:
    """Return (possibly smaller) image bytes + mime. Never raises.

    Small images (<= trigger) return unchanged. Oversized images are resized to a
    bounded long edge and re-encoded in a format-appropriate codec; the smaller of
    (re-encoded, original) wins so a re-encode can never inflate a send.
    """
    if not data or len(data) <= _TRIGGER_BYTES:
        return data, mime
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:  # noqa: BLE001 — corrupt/exotic image: don't block the send
        log.warning("image open failed, using original (%d bytes): %s", len(data), e)
        return data, mime

    fmt = (im.format or "").upper()
    # Animated GIF: re-encoding would flatten to one frame — leave it.
    if fmt == "GIF" and getattr(im, "is_animated", False):
        return data, mime

    try:
        w, h = im.size
        long_edge = max(w, h)
        if long_edge > _MAX_EDGE_PX:
            scale = _MAX_EDGE_PX / long_edge
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                           Image.LANCZOS)
        encoded, out_mime = _encode(im, fmt if fmt in ("PNG", "WEBP") else "JPEG")
        # A PNG photo can stay large even at 1568px — fall back to JPEG.
        if out_mime == "image/png" and len(encoded) > _PNG_FALLBACK_BYTES:
            encoded, out_mime = _encode(im, "JPEG")
    except Exception as e:  # noqa: BLE001
        log.warning("image re-encode failed, using original: %s", e)
        return data, mime

    if len(encoded) < len(data):
        return encoded, out_mime
    return data, mime
