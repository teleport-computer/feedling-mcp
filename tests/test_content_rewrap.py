from __future__ import annotations

import base64
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from identity import service as identity_service  # noqa: E402
from memory import service as memory_service  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    monkeypatch.setattr(
        core_enclave,
        "_get_enclave_info",
        lambda: {"content_pk_hex": ("22" * 32), "compose_hash": "test"},
    )
    with make_client() as c:
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
    store = core_store.get_store(user_id)
    now = datetime.now().isoformat()

    identity = {
        **_old_env(user_id, "identity1"),
        "created_at": now,
        "updated_at": now,
        "relationship_started_at": "2026-06-01",
    }
    identity_service._save_identity(store, identity)

    memory = {
        **_old_env(user_id, "memory1"),
        "type": "fact",
        "occurred_at": "2026-06-01",
        "created_at": now,
        "source": "test",
    }
    memory_service._save_moments(store, [memory])

    chat = {
        **_old_env(user_id, "chat1"),
        "role": "openclaw",
        "source": "test",
        "ts": time.time(),
        "content_type": "text",
    }
    with store.chat_lock:
        store.chat_messages = [chat]
        db.chat_append(user_id, chat["id"], chat["ts"], chat, core_store.MAX_CHAT_MESSAGES)
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
    assert registry._get_user_public_key(user_id) == _b64(b"\x11" * 32)


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
    assert registry._get_user_public_key(user_id) == _b64(b"\x11" * 32)

    res = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": new_public_key},
        headers=_headers(api_key),
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["summary"]["total_rewrapped"] == 3
    assert body["summary"]["total_errors"] == 0
    assert registry._get_user_public_key(user_id) == new_public_key

    store = core_store.get_store(user_id)
    identity = identity_service._load_identity(store)
    moments = memory_service._load_moments(store)
    with store.chat_lock:
        chat = list(store.chat_messages)

    assert identity["id"] == "identity1"
    assert moments[0]["id"] == "memory1"
    assert chat[0]["id"] == "chat1"
    assert identity["K_user"] != old_keys["identity_K_user"]
    assert moments[0]["K_user"] != old_keys["memory_K_user"]
    assert chat[0]["K_user"] != old_keys["chat_K_user"]
    assert "K_enclave" in identity and "K_enclave" in moments[0] and "K_enclave" in chat[0]


def test_rewrap_partial_failure_persists_successes_and_reports_pending(client, monkeypatch):
    user_id, api_key = _register(client)
    old_keys = _seed_encrypted_content(user_id)
    new_public_key = _b64(b"\x33" * 32)

    def fake_decrypt(envelope, key, purpose):
        if envelope.get("id") == "chat1":
            raise RuntimeError("enclave_error:ReadTimeout")
        return f"plaintext:{purpose}:{envelope.get('id')}".encode()

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    res = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": new_public_key},
        headers=_headers(api_key),
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "partial"
    assert body["summary"]["total_rewrapped"] == 2
    assert body["summary"]["total_errors"] == 1
    # pending 只含超时的 chat1
    assert [p["id"] for p in body["pending"]] == ["chat1"]
    # 成功条目已落盘(K_user 变了),失败条目保持旧值
    store = core_store.get_store(user_id)
    identity = identity_service._load_identity(store)
    moments = memory_service._load_moments(store)
    with store.chat_lock:
        chat = list(store.chat_messages)
    assert identity["K_user"] != old_keys["identity_K_user"]
    assert moments[0]["K_user"] != old_keys["memory_K_user"]
    assert chat[0]["K_user"] == old_keys["chat_K_user"]
    # 有进展 → 注册钥已推进到新钥
    assert registry._get_user_public_key(user_id) == new_public_key


def test_rewrap_converges_on_retry(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_encrypted_content(user_id)
    new_public_key = _b64(b"\x33" * 32)

    calls = {"n": 0}
    def fake_decrypt(envelope, key, purpose):
        # 第一轮 chat1 超时;之后全成。
        if envelope.get("id") == "chat1" and calls["n"] == 0:
            raise RuntimeError("enclave_error:ReadTimeout")
        return f"plaintext:{purpose}:{envelope.get('id')}".encode()

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    r1 = client.post("/v1/content/rewrap-to-current-key",
                     json={"public_key": new_public_key}, headers=_headers(api_key))
    assert r1.get_json()["status"] == "partial"
    calls["n"] = 1

    r2 = client.post("/v1/content/rewrap-to-current-key",
                     json={"public_key": new_public_key}, headers=_headers(api_key))
    b2 = r2.get_json()
    assert r2.status_code == 200
    assert b2["status"] == "ok"
    assert b2["pending"] == []
    assert registry._get_user_public_key(user_id) == new_public_key
    store = core_store.get_store(user_id)
    with store.chat_lock:
        chat = list(store.chat_messages)
    assert "K_enclave" in chat[0]


def test_rewrap_no_progress_returns_failed_and_keeps_key(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_encrypted_content(user_id)
    new_public_key = _b64(b"\x33" * 32)
    old_registered = registry._get_user_public_key(user_id)

    def fake_decrypt(envelope, key, purpose):
        raise RuntimeError("enclave_http_502:backend_error timed out")

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    res = client.post("/v1/content/rewrap-to-current-key",
                      json={"public_key": new_public_key}, headers=_headers(api_key))
    assert res.status_code == 409
    body = res.get_json()
    assert body["status"] == "failed"
    assert body["summary"]["total_rewrapped"] == 0
    assert len(body["pending"]) == 3
    # 零进展 → 注册钥保持旧值
    assert registry._get_user_public_key(user_id) == old_registered


def test_rewrap_skips_already_current_items_on_second_pass(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_encrypted_content(user_id)
    new_public_key = _b64(b"\x33" * 32)

    calls = {"n": 0}
    def fake_decrypt(envelope, key, purpose):
        calls["n"] += 1
        return f"plaintext:{purpose}:{envelope.get('id')}".encode()

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    r1 = client.post("/v1/content/rewrap-to-current-key",
                     json={"public_key": new_public_key}, headers=_headers(api_key))
    assert r1.get_json()["status"] == "ok"
    first_calls = calls["n"]
    assert first_calls == 3  # 首轮解了 3 条

    # 同钥再来一次:全部已是当前钥 → 一次 enclave 解密都不发生。
    r2 = client.post("/v1/content/rewrap-to-current-key",
                     json={"public_key": new_public_key}, headers=_headers(api_key))
    b2 = r2.get_json()
    assert r2.status_code == 200
    assert b2["status"] == "ok"
    assert calls["n"] == first_calls  # 未新增解密调用
    assert b2["summary"]["total_skipped"] >= 3
    assert b2["summary"]["total_rewrapped"] == 0


def test_client_swap_clears_stale_content_pk_fpr_no_false_skip(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_encrypted_content(user_id)
    key_a = _b64(b"\x33" * 32)

    calls = {"n": 0}
    def fake_decrypt(envelope, key, purpose):
        calls["n"] += 1
        return f"plaintext:{purpose}:{envelope.get('id')}".encode()
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    # First rewrap to key A stamps content_pk_fpr on chat1.
    r1 = client.post("/v1/content/rewrap-to-current-key",
                     json={"public_key": key_a}, headers=_headers(api_key))
    assert r1.get_json()["status"] == "ok"

    # Client swaps chat1 in place: new K_user + a FORGED stamp matching key A.
    fpr_a = core_envelope._content_public_key_fingerprint(base64.b64decode(key_a))
    swap_env = _old_env(user_id, "chat1")
    swap_env["K_user"] = _b64(b"\x09" * 48)
    swap_env["content_pk_fpr"] = fpr_a
    sres = client.post("/v1/content/swap",
                       json={"items": [{"type": "chat", "id": "chat1", "envelope": swap_env}]},
                       headers=_headers(api_key))
    assert sres.status_code == 200, sres.get_data(as_text=True)

    # Stored stamp must have been cleared (not the forged fpr_a).
    store = core_store.get_store(user_id)
    with store.chat_lock:
        chat = [m for m in store.chat_messages if m["id"] == "chat1"][0]
    assert chat.get("content_pk_fpr") != fpr_a

    # A subsequent rewrap to key A must NOT false-skip chat1: it must re-decrypt it.
    before = calls["n"]
    client.post("/v1/content/rewrap-to-current-key",
                json={"public_key": key_a}, headers=_headers(api_key))
    assert calls["n"] > before
