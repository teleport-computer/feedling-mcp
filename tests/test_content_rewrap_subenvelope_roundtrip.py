"""Real-crypto round trip for the thinking sub-envelope.

The unit tests stub the enclave, so they can't catch an AAD mismatch. Here the
enclave really decrypts (``enclave.envelope.decrypt_envelope``) and the device
really unseals afterwards, using the same construction iOS uses:

    aad = thinking_owner_user_id | thinking_v | (thinking_id ?? id)

If rewrap ever changes any leg of that triple, the final unseal raises and this
test fails — which is exactly what would happen on the user's phone.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import nacl.public
import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from accounts import registry  # noqa: E402
from content_encryption import build_envelope  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import store as core_store  # noqa: E402
from enclave import envelope as enclave_envelope  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


THINKING_TEXT = "内部推理：先查记忆，再回答。"
BODY_TEXT = "hello from the past"


@pytest.fixture()
def crypto(tmp_path, monkeypatch):
    enclave_sk = nacl.public.PrivateKey.generate()
    enclave_pk = bytes(enclave_sk.public_key)
    old_user_sk = nacl.public.PrivateKey.generate()
    new_user_sk = nacl.public.PrivateKey.generate()

    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    monkeypatch.setattr(
        core_enclave,
        "_get_enclave_info",
        lambda: {"content_pk_hex": enclave_pk.hex(), "compose_hash": "test"},
    )

    # The enclave decrypts for real, with its real private key.
    def real_decrypt(env, key, purpose, caller_user_id):
        return enclave_envelope.decrypt_envelope(env, env["owner_user_id"], enclave_sk)

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", real_decrypt)

    with make_client() as c:
        yield c, enclave_pk, old_user_sk, new_user_sk


def _device_unseal(env: dict, user_sk: nacl.public.PrivateKey) -> bytes:
    """Exactly what iOS does: unseal K_user, then AEAD-open with owner|v|id."""
    K = enclave_envelope.box_seal_open_hkdf(base64.b64decode(env["K_user"]), bytes(user_sk))
    import nacl.bindings

    aad = enclave_envelope.build_aead_aad(env["owner_user_id"], int(env.get("v", 1)), env.get("id", ""))
    return nacl.bindings.crypto_aead_chacha20poly1305_ietf_decrypt(
        base64.b64decode(env["body_ct"]), aad, base64.b64decode(env["nonce"]), K
    )


def test_thinking_sub_envelope_survives_rewrap_and_opens_on_the_device(crypto):
    client, enclave_pk, old_user_sk, new_user_sk = crypto
    old_user_pk = bytes(old_user_sk.public_key)
    new_user_pk = bytes(new_user_sk.public_key)

    res = client.post("/v1/users/register", json={"public_key": _b64(old_user_pk)})
    assert res.status_code == 201
    user_id = res.get_json()["user_id"]
    api_key = res.get_json()["api_key"]

    main = build_envelope(
        plaintext=BODY_TEXT.encode(), owner_user_id=user_id,
        user_pk_bytes=old_user_pk, enclave_pk_bytes=enclave_pk, item_id="chat1",
    )
    thinking = build_envelope(
        plaintext=THINKING_TEXT.encode(), owner_user_id=user_id,
        user_pk_bytes=old_user_pk, enclave_pk_bytes=enclave_pk, item_id="think1",
    )
    msg = {
        **main, "role": "openclaw", "source": "test",
        "ts": time.time(), "content_type": "text",
        "thinking_v": thinking["v"], "thinking_id": thinking["id"],
        "thinking_owner_user_id": thinking["owner_user_id"],
        "thinking_body_ct": thinking["body_ct"], "thinking_nonce": thinking["nonce"],
        "thinking_K_user": thinking["K_user"], "thinking_K_enclave": thinking["K_enclave"],
        "thinking_visibility": "shared",
    }
    store = core_store.get_store(user_id)
    with store.chat_lock:
        store.chat_messages = [msg]
        db.chat_append(user_id, msg["id"], msg["ts"], msg, core_store.MAX_CHAT_MESSAGES)

    # Sanity: the NEW device key must not open anything yet.
    with pytest.raises(Exception):
        _device_unseal(main, new_user_sk)

    res = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": _b64(new_user_pk)},
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json()["summary"]["total_errors"] == 0

    stored = core_store.get_store(user_id).chat_messages[0]

    assert _device_unseal(stored, new_user_sk) == BODY_TEXT.encode()

    thinking_env = {
        "v": stored["thinking_v"], "id": stored["thinking_id"],
        "owner_user_id": stored["thinking_owner_user_id"],
        "body_ct": stored["thinking_body_ct"], "nonce": stored["thinking_nonce"],
        "K_user": stored["thinking_K_user"],
    }
    assert _device_unseal(thinking_env, new_user_sk) == THINKING_TEXT.encode()


def test_thinking_without_its_own_id_opens_under_the_record_id(crypto):
    """iOS falls back to `id`; the AAD must fall back the same way."""
    client, enclave_pk, old_user_sk, new_user_sk = crypto
    old_user_pk = bytes(old_user_sk.public_key)
    new_user_pk = bytes(new_user_sk.public_key)

    res = client.post("/v1/users/register", json={"public_key": _b64(old_user_pk)})
    user_id, api_key = res.get_json()["user_id"], res.get_json()["api_key"]

    main = build_envelope(
        plaintext=BODY_TEXT.encode(), owner_user_id=user_id,
        user_pk_bytes=old_user_pk, enclave_pk_bytes=enclave_pk, item_id="chatX",
    )
    # Legacy row: the thinking envelope was sealed under the RECORD's id.
    thinking = build_envelope(
        plaintext=THINKING_TEXT.encode(), owner_user_id=user_id,
        user_pk_bytes=old_user_pk, enclave_pk_bytes=enclave_pk, item_id="chatX",
    )
    msg = {
        **main, "role": "openclaw", "source": "test",
        "ts": time.time(), "content_type": "text",
        "thinking_v": thinking["v"],  # no thinking_id
        "thinking_owner_user_id": thinking["owner_user_id"],
        "thinking_body_ct": thinking["body_ct"], "thinking_nonce": thinking["nonce"],
        "thinking_K_user": thinking["K_user"], "thinking_K_enclave": thinking["K_enclave"],
        "thinking_visibility": "shared",
    }
    store = core_store.get_store(user_id)
    with store.chat_lock:
        store.chat_messages = [msg]
        db.chat_append(user_id, msg["id"], msg["ts"], msg, core_store.MAX_CHAT_MESSAGES)

    res = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": _b64(new_user_pk)},
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json()["summary"]["total_errors"] == 0

    stored = core_store.get_store(user_id).chat_messages[0]
    thinking_env = {
        "v": stored["thinking_v"], "id": stored["thinking_id"],
        "owner_user_id": stored["thinking_owner_user_id"],
        "body_ct": stored["thinking_body_ct"], "nonce": stored["thinking_nonce"],
        "K_user": stored["thinking_K_user"],
    }
    assert stored["thinking_id"] == "chatX"
    assert _device_unseal(thinking_env, new_user_sk) == THINKING_TEXT.encode()
