import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker


def test_caption_envelope_rebuilds_from_prefixed_keys():
    m = {
        "id": "msg1", "owner_user_id": "u1", "v": "1",
        "caption_id": "cap1", "caption_v": "1", "caption_body_ct": "CT",
        "caption_nonce": "N", "caption_K_enclave": "KE",
        "caption_owner_user_id": "u1",
    }
    env = serve_worker._caption_envelope(m)
    # AEAD AAD is owner_user_id||v||id -> MUST use the caption's own id, not the message's.
    assert env["id"] == "cap1"
    assert env["body_ct"] == "CT"
    assert env["K_enclave"] == "KE"
    assert env["owner_user_id"] == "u1"


def test_caption_envelope_none_without_ciphertext():
    assert serve_worker._caption_envelope({"id": "m1"}) is None
    assert serve_worker._caption_envelope({"id": "m1", "caption_body_ct": ""}) is None


def test_caption_envelope_falls_back_to_message_owner_and_id():
    env = serve_worker._caption_envelope(
        {"id": "m1", "owner_user_id": "u9", "v": "2", "caption_body_ct": "CT"})
    assert env["id"] == "m1"        # no caption_id -> message id
    assert env["owner_user_id"] == "u9"
    assert env["v"] == 2
