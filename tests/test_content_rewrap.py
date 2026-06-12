from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    appmod._users[:] = []
    appmod._key_to_user.clear()
    appmod._stores.clear()
    appmod._save_users()
    monkeypatch.setattr(
        core_enclave,
        "_get_enclave_info",
        lambda: {"content_pk_hex": ("22" * 32), "compose_hash": "test"},
    )
    appmod.app.config.update(TESTING=True)
    with appmod.app.test_client() as c:
        yield c


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _old_env(user_id: str, item_id: str) -> dict:
    return {
        "v": 1,
        "id": item_id,
        "body_ct": _b64(f"old-body:{item_id}".encode()),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 48),
        "K_enclave": _b64(b"\x02" * 48),
        "visibility": "shared",
        "owner_user_id": user_id,
        "enclave_pk_fpr": "old",
    }


def _seed_encrypted_content(user_id: str) -> dict[str, str]:
    store = appmod.get_store(user_id)
    now = appmod.datetime.now().isoformat()

    identity = {
        **_old_env(user_id, "identity1"),
        "created_at": now,
        "updated_at": now,
        "relationship_started_at": "2026-06-01",
    }
    appmod._save_identity(store, identity)

    memory = {
        **_old_env(user_id, "memory1"),
        "type": "fact",
        "occurred_at": "2026-06-01",
        "created_at": now,
        "source": "test",
    }
    appmod._save_moments(store, [memory])

    chat = {
        **_old_env(user_id, "chat1"),
        "role": "openclaw",
        "source": "test",
        "ts": time.time(),
        "content_type": "text",
    }
    with store.chat_lock:
        store.chat_messages = [chat]
        appmod.db.chat_append(user_id, chat["id"], chat["ts"], chat, appmod.MAX_CHAT_MESSAGES)
    return {
        "identity_K_user": identity["K_user"],
        "memory_K_user": memory["K_user"],
        "chat_K_user": chat["K_user"],
    }


def test_public_key_rotation_requires_rewrap_when_content_exists(client):
    user_id, api_key = _register(client)
    _seed_encrypted_content(user_id)

    res = client.post(
        "/v1/users/public-key",
        json={"public_key": _b64(b"\x33" * 32)},
        headers=_headers(api_key),
    )

    assert res.status_code == 409, res.get_data(as_text=True)
    body = res.get_json()
    assert body["error"] == "public_key_rotation_requires_rewrap"
    assert body["encrypted_content"] == {"identity": 1, "memory": 1, "chat": 1, "total": 3}
    assert body["recovery_endpoint"] == "/v1/content/rewrap-to-current-key"
    assert appmod._get_user_public_key(user_id) == _b64(b"\x11" * 32)


def test_content_rewrap_to_current_key_rewraps_all_shared_content(client, monkeypatch):
    user_id, api_key = _register(client)
    old_keys = _seed_encrypted_content(user_id)
    new_public_key = _b64(b"\x33" * 32)

    def fake_decrypt(envelope, key, purpose):
        assert key == api_key
        return f"plaintext:{purpose}:{envelope.get('id')}".encode()

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    dry = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": new_public_key, "dry_run": True},
        headers=_headers(api_key),
    )
    assert dry.status_code == 200, dry.get_data(as_text=True)
    assert dry.get_json()["summary"]["total_rewrapped"] == 3
    assert appmod._get_user_public_key(user_id) == _b64(b"\x11" * 32)

    res = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": new_public_key},
        headers=_headers(api_key),
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["summary"]["total_rewrapped"] == 3
    assert body["summary"]["total_errors"] == 0
    assert appmod._get_user_public_key(user_id) == new_public_key

    store = appmod.get_store(user_id)
    identity = appmod._load_identity(store)
    moments = appmod._load_moments(store)
    with store.chat_lock:
        chat = list(store.chat_messages)

    assert identity["id"] == "identity1"
    assert moments[0]["id"] == "memory1"
    assert chat[0]["id"] == "chat1"
    assert identity["K_user"] != old_keys["identity_K_user"]
    assert moments[0]["K_user"] != old_keys["memory_K_user"]
    assert chat[0]["K_user"] != old_keys["chat_K_user"]
    assert "K_enclave" in identity and "K_enclave" in moments[0] and "K_enclave" in chat[0]
