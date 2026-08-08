import base64
import io

import pytest

from generated_image import (
    MAX_GENERATED_IMAGE_EDGE_PX,
    decode_base64_image,
    normalize_generated_image,
)


PIL = pytest.importorskip("PIL")
from PIL import Image


def _png(size=(32, 24), color=(40, 80, 120)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def test_decode_base64_image_accepts_plain_and_data_url():
    data = _png()
    encoded = base64.b64encode(data).decode("ascii")
    assert decode_base64_image(encoded) == data
    assert decode_base64_image(f"data:image/png;base64,{encoded}") == data


def test_decode_base64_image_rejects_remote_or_malformed_values():
    with pytest.raises(ValueError, match="base64_invalid"):
        decode_base64_image("https://example.com/result.png")
    with pytest.raises(ValueError, match="data_url_invalid"):
        decode_base64_image("data:image/png,not-base64")


def test_normalize_generated_image_validates_mime_and_safe_name():
    normalized = normalize_generated_image(
        _png(), declared_mime="image/png", name="../picture.jpeg"
    )
    assert normalized.mime_type == "image/png"
    assert normalized.name == "picture.png"
    with pytest.raises(ValueError, match="mime_mismatch"):
        normalize_generated_image(_png(), declared_mime="image/jpeg")


def test_normalize_generated_image_bounds_long_edge():
    normalized = normalize_generated_image(
        _png(size=(MAX_GENERATED_IMAGE_EDGE_PX + 400, 1000)),
        declared_mime="image/png",
    )
    with Image.open(io.BytesIO(normalized.data)) as image:
        assert max(image.size) == MAX_GENERATED_IMAGE_EDGE_PX


def test_normalize_generated_image_rejects_non_raster_payload():
    with pytest.raises(ValueError, match="generated_image_invalid"):
        normalize_generated_image(b"<svg><script /></svg>", declared_mime="image/svg+xml")
