"""Validation and bounded normalization for model-generated chat images."""
from __future__ import annotations

import base64
import binascii
import io
from dataclasses import dataclass
from pathlib import PurePath


MAX_GENERATED_IMAGE_SOURCE_BYTES = 25_000_000
MAX_GENERATED_IMAGE_STORED_BYTES = 2_000_000
MAX_GENERATED_IMAGE_EDGE_PX = 1568
MAX_GENERATED_IMAGE_PIXELS = 40_000_000
MAX_GENERATED_IMAGES_PER_REPLY = 4

_MIME_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@dataclass(frozen=True)
class NormalizedGeneratedImage:
    data: bytes
    mime_type: str
    name: str


def decode_base64_image(value: str) -> bytes:
    """Decode one provider image result without accepting remote URLs."""
    encoded = str(value or "").strip()
    if encoded.startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or ";base64" not in header.lower():
            raise ValueError("generated_image_data_url_invalid")
    if not encoded:
        raise ValueError("generated_image_empty")
    max_encoded = ((MAX_GENERATED_IMAGE_SOURCE_BYTES + 2) // 3) * 4 + 16
    if len(encoded) > max_encoded:
        raise ValueError("generated_image_too_large")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("generated_image_base64_invalid") from exc
    if not data:
        raise ValueError("generated_image_empty")
    if len(data) > MAX_GENERATED_IMAGE_SOURCE_BYTES:
        raise ValueError("generated_image_too_large")
    return data


def _safe_name(raw: str, *, mime_type: str, index: int) -> str:
    base = PurePath(str(raw or "").replace("\\", "/")).name
    cleaned = "".join(
        char
        for char in base
        if char.isprintable() and char not in {"\n", "\r", "\t", "/", "\\"}
    ).strip().strip(".")
    stem = PurePath(cleaned).stem[:80] if cleaned else f"generated-image-{index}"
    return (stem or f"generated-image-{index}") + _EXT_BY_MIME[mime_type]


def _encode(im, mime_type: str, *, quality: int = 85) -> bytes:
    out = io.BytesIO()
    if mime_type == "image/png":
        im.save(out, format="PNG", optimize=True)
    elif mime_type == "image/webp":
        im.save(out, format="WEBP", quality=quality, method=4)
    else:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def normalize_generated_image(
    data: bytes,
    *,
    declared_mime: str = "",
    name: str = "",
    index: int = 1,
) -> NormalizedGeneratedImage:
    """Validate pixels and return a bounded PNG/JPEG/WebP chat payload."""
    raw = bytes(data or b"")
    if not raw:
        raise ValueError("generated_image_empty")
    if len(raw) > MAX_GENERATED_IMAGE_SOURCE_BYTES:
        raise ValueError("generated_image_too_large")

    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as probe:
            image_format = str(probe.format or "").upper()
            width, height = probe.size
            if image_format not in _MIME_BY_FORMAT:
                raise ValueError("generated_image_format_unsupported")
            if width <= 0 or height <= 0 or width * height > MAX_GENERATED_IMAGE_PIXELS:
                raise ValueError("generated_image_dimensions_invalid")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            image = opened.copy()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow exposes format-specific errors
        raise ValueError("generated_image_invalid") from exc

    actual_mime = _MIME_BY_FORMAT[image_format]
    claimed = str(declared_mime or "").strip().lower()
    if claimed and claimed not in {actual_mime, "image/jpg"}:
        raise ValueError("generated_image_mime_mismatch")

    long_edge = max(image.size)
    if long_edge > MAX_GENERATED_IMAGE_EDGE_PX:
        scale = MAX_GENERATED_IMAGE_EDGE_PX / long_edge
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )

    output_mime = actual_mime
    output = raw
    if image.size != (width, height) or len(raw) > MAX_GENERATED_IMAGE_STORED_BYTES:
        output = _encode(image, output_mime)
    if output_mime == "image/png" and len(output) > MAX_GENERATED_IMAGE_STORED_BYTES:
        output_mime = "image/jpeg"
        output = _encode(image, output_mime)
    if len(output) > MAX_GENERATED_IMAGE_STORED_BYTES:
        output_mime = "image/jpeg"
        for quality in (80, 70, 60):
            output = _encode(image, output_mime, quality=quality)
            if len(output) <= MAX_GENERATED_IMAGE_STORED_BYTES:
                break
    if not output or len(output) > MAX_GENERATED_IMAGE_STORED_BYTES:
        raise ValueError("generated_image_normalized_too_large")

    return NormalizedGeneratedImage(
        data=output,
        mime_type=output_mime,
        name=_safe_name(name, mime_type=output_mime, index=max(1, int(index))),
    )
