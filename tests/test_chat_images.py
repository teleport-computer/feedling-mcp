from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from asgi_test_client import make_client  # noqa: E402
from core import chat_images  # noqa: E402
from core import store as core_store  # noqa: E402
from tee_replicator import transforms  # noqa: E402


def test_image_bundle_round_trip_and_strict_tail_rejection():
    source = [(b"jpeg-one", "image/jpeg"), (b"png-two", "image/png")]
    encoded = chat_images.encode_image_bundle(source)
    assert chat_images.decode_image_bundle(encoded) == source
    with pytest.raises(ValueError, match="trailing"):
        chat_images.decode_image_bundle(encoded + b"x")


# Fixed contract values, independent of the producer's projection-field tuple.
_IMAGE_METADATA = {
    "image_bundle_version": 1,
    "image_count": 2,
    "image_mimes": ["image/jpeg", "image/webp"],
}


def _image_envelope(user_id, marker):
    return {
        "v": 1, "id": marker,
        "body_ct": base64.b64encode(b"opaque-image-bundle").decode("ascii"),
        "nonce": base64.b64encode(b"\x00" * 12).decode("ascii"),
        "K_user": base64.b64encode(b"\x01" * 32).decode("ascii"),
        "K_enclave": base64.b64encode(b"\x02" * 32).decode("ascii"),
        "visibility": "shared", "owner_user_id": user_id,
    }


def _assert_image_metadata(doc):
    assert doc["content_type"] == "image"
    assert {key: doc[key] for key in _IMAGE_METADATA} == _IMAGE_METADATA


def test_build_chat_message_preserves_multi_image_metadata():
    # This path is separate from append_chat and has its own projection list.
    store = core_store.UserStore("image-projection-unit")
    msg = store._build_chat_message(
        "user", "chat", _image_envelope(store.user_id, "built-images"),
        content_type="image", extra=dict(_IMAGE_METADATA),
    )
    _assert_image_metadata(msg)


def test_append_chat_persists_multi_image_metadata_on_fresh_reload(backend_env):
    response = make_client().post(
        "/v1/users/register",
        json={
            "public_key": base64.b64encode(b"\x11" * 32).decode("ascii"),
            "archive_language": "en",
        },
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    user_id = response.get_json()["user_id"]
    store = core_store.get_store(user_id)
    msg = store.append_chat(
        "user", "chat", _image_envelope(user_id, "persisted-images"),
        content_type="image", extra=dict(_IMAGE_METADATA), strict=True,
    )
    _assert_image_metadata(msg)
    # A new instance cannot satisfy this assertion from the writer's cache.
    reloaded = core_store.UserStore(user_id)
    rows = reloaded.reload_chat_hot_strict()
    saved = next(row for row in rows if row["id"] == "persisted-images")
    _assert_image_metadata(saved)


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
    _assert_image_metadata(out)
    assert not {"body", "body_ct", "nonce", "K_user", "K_enclave"} & out.keys()


def test_numbered_observations_have_one_total_budget():
    combined = chat_images.combine_numbered_observations(["a" * 9000, "b" * 9000])
    assert combined.startswith("Image 1:\n")
    assert "Image 2:\n" in combined
    assert len(combined) <= chat_images.MAX_CHAT_IMAGE_OBSERVATION_CHARS
