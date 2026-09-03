from __future__ import annotations

import base64
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from core import chat_images  # noqa: E402
from core import store as core_store  # noqa: E402
from tee_replicator import transforms  # noqa: E402


def test_image_bundle_round_trip_and_strict_tail_rejection():
    source = [(b"jpeg-one", "image/jpeg"), (b"png-two", "image/png")]
    encoded = chat_images.encode_image_bundle(source)
    assert chat_images.decode_image_bundle(encoded) == source
    with pytest.raises(ValueError, match="trailing"):
        chat_images.decode_image_bundle(encoded + b"x")


def test_multi_image_storage_fields_are_registered_in_every_projection():
    """The producer-owned field tuple derives every projection assertion."""
    store_source = inspect.getsource(core_store)
    transform_source = inspect.getsource(transforms.plaintext_chat_doc)
    for field in chat_images.MULTI_IMAGE_STORAGE_FIELDS:
        assert store_source.count(f'"{field}",') >= 2
        assert field in transform_source


def test_tee_multi_image_transform_exposes_each_body_without_crypto_fields():
    bundle = chat_images.encode_image_bundle([
        (b"one", "image/jpeg"),
        (b"two", "image/webp"),
    ])
    doc = {
        "id": "m-images",
        "role": "user",
        "content_type": "image",
        "visibility": "shared",
        "owner_user_id": "u",
        "body_ct": "cipher",
        "nonce": "n",
        "K_user": "ku",
        "K_enclave": "ke",
        "image_bundle_version": 1,
        "image_count": 2,
        "image_mimes": ["image/jpeg", "image/webp"],
    }
    out = transforms.plaintext_chat_doc(
        doc, lambda _env, purpose: bundle
    )
    assert out["images"] == [
        {"image_b64": base64.b64encode(b"one").decode(), "image_mime": "image/jpeg"},
        {"image_b64": base64.b64encode(b"two").decode(), "image_mime": "image/webp"},
    ]
    assert "body" not in out and "body_ct" not in out


def test_numbered_observations_have_one_total_budget():
    combined = chat_images.combine_numbered_observations(["a" * 9000, "b" * 9000])
    assert combined.startswith("Image 1:\n")
    assert "Image 2:\n" in combined
    assert len(combined) <= chat_images.MAX_CHAT_IMAGE_OBSERVATION_CHARS
