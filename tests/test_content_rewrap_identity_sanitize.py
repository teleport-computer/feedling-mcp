"""Regression: /v1/content/rewrap-to-current-key deep-sanitizes the identity card.

Root cause of the usr_f13f loop: the poisoned plaintext (float dimension
values) lives INSIDE an envelope already sealed to the CURRENT key, so the
fpr guard skipped it (`rewrapped=0 skipped=38`) and the client retried
forever. The fix: for the identity record the rewrap path decrypts even when
the fpr is current, runs card_policy.sanitize on the plaintext, and re-seals
iff sanitize changed anything — the failing client's own retry loop becomes
the recovery vehicle (no ops action, no app update).
"""
from __future__ import annotations

import base64
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from content import content_core  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from identity import service as identity_service  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


USER_PK = _b64(b"\x11" * 32)


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
        json={"public_key": USER_PK, "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _seed_identity(user_id: str, *, content_pk_fpr: str) -> dict:
    """Seed an encrypted identity record; fpr controls current-key vs old-key."""
    store = core_store.get_store(user_id)
    now = datetime.now().isoformat()
    identity = {
        "v": 1,
        "id": "identity1",
        "body_ct": _b64(b"opaque-ciphertext"),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 48),
        "K_enclave": _b64(b"\x02" * 48),
        "visibility": "shared",
        "owner_user_id": user_id,
        "enclave_pk_fpr": "old",
        "content_pk_fpr": content_pk_fpr,
        "created_at": now,
        "updated_at": now,
    }
    identity_service._save_identity(store, identity)
    return identity


POISONED_CARD = {
    "agent_name": "言",
    "self_introduction": "hi",
    "dimensions": [
        {"name": "锐利", "value": 0.95, "description": "d1"},
        {"name": "温情", "value": 0.6, "description": "d2"},
        {"name": "已是整数", "value": 88, "description": "d3"},
    ],
}

CLEAN_CARD = {
    "agent_name": "言",
    "self_introduction": "hi",
    "dimensions": [
        {"name": "锐利", "value": 95, "description": "d1"},
        {"name": "温情", "value": 60, "description": "d2"},
    ],
}


def _current_fpr() -> str:
    return core_envelope._content_public_key_fingerprint(USER_PK)


def _mock_decrypt(monkeypatch, card: dict):
    def fake_decrypt(envelope, key, *, purpose, **kwargs):
        return json.dumps(card, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)


def _spy_build_envelope(monkeypatch):
    captured: list[bytes] = []
    real = content_core.build_envelope

    def spy(*args, **kwargs):
        captured.append(kwargs.get("plaintext") or args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(content_core, "build_envelope", spy)
    return captured


def _rewrap(client, api_key: str):
    res = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": USER_PK},
        headers=_headers(api_key),
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


def _identity_result(body: dict) -> dict:
    return next(r for r in body["results"] if r["type"] == "identity")


def test_current_key_poisoned_identity_is_sanitized_and_resealed(client, monkeypatch):
    # usr_f13f 的精确形态:信封已封在当前钥(fpr 匹配),毒在明文里。
    user_id, api_key = _register(client)
    seeded = _seed_identity(user_id, content_pk_fpr=_current_fpr())
    _mock_decrypt(monkeypatch, POISONED_CARD)
    captured = _spy_build_envelope(monkeypatch)

    body = _rewrap(client, api_key)

    r = _identity_result(body)
    assert r["status"] == "rewrapped"
    assert "identity_plaintext_normalized" in r.get("reason", "")
    assert body["summary"]["identity"]["rewrapped"] == 1

    # 落盘:重封后的信封换了 K_user,fpr 仍是当前钥
    store = core_store.get_store(user_id)
    saved = identity_service._load_identity(store)
    assert saved["K_user"] != seeded["K_user"]
    assert saved["content_pk_fpr"] == _current_fpr()

    # 重封进信封的明文分值已是整数
    card = json.loads(captured[0].decode("utf-8"))
    values = {d["name"]: d["value"] for d in card["dimensions"]}
    assert values == {"锐利": 95, "温情": 60, "已是整数": 88}
    for d in card["dimensions"]:
        assert type(d["value"]) is int


def test_current_key_clean_identity_still_skips(client, monkeypatch):
    # 干净卡不能白白重封:解开看过没毒 → 维持 skipped_already_current 语义。
    user_id, api_key = _register(client)
    seeded = _seed_identity(user_id, content_pk_fpr=_current_fpr())
    _mock_decrypt(monkeypatch, CLEAN_CARD)

    body = _rewrap(client, api_key)

    r = _identity_result(body)
    assert r["status"] == "skipped_already_current"
    assert body["summary"]["identity"]["rewrapped"] == 0
    store = core_store.get_store(user_id)
    assert identity_service._load_identity(store)["K_user"] == seeded["K_user"]


def test_current_key_decrypt_failure_downgrades_to_skip(client, monkeypatch):
    # fpr 已是当前钥时,探测性解密失败不能变成 error/pending —— 否则客户端会
    # 对一条本来就"已是当前钥"的记录无限重试。
    user_id, api_key = _register(client)
    _seed_identity(user_id, content_pk_fpr=_current_fpr())

    def boom(envelope, key, *, purpose, **kwargs):
        raise RuntimeError("enclave_error:ReadTimeout")

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", boom)

    body = _rewrap(client, api_key)

    r = _identity_result(body)
    assert r["status"].startswith("skipped")
    assert body["summary"]["identity"]["errors"] == 0
    assert body["pending"] == []


def test_old_key_poisoned_identity_rewraps_and_sanitizes_in_one_pass(client, monkeypatch):
    # 真钥漂移 + 毒明文同时发生:一次 rewrap 同时换钥和归一化。
    user_id, api_key = _register(client)
    _seed_identity(user_id, content_pk_fpr="stale-fpr")
    _mock_decrypt(monkeypatch, POISONED_CARD)
    captured = _spy_build_envelope(monkeypatch)

    body = _rewrap(client, api_key)

    r = _identity_result(body)
    assert r["status"] == "rewrapped"
    card = json.loads(captured[0].decode("utf-8"))
    assert all(type(d["value"]) is int for d in card["dimensions"])


def test_dry_run_reports_would_sanitize_without_saving(client, monkeypatch):
    user_id, api_key = _register(client)
    seeded = _seed_identity(user_id, content_pk_fpr=_current_fpr())
    _mock_decrypt(monkeypatch, POISONED_CARD)

    res = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": USER_PK, "dry_run": True},
        headers=_headers(api_key),
    )
    assert res.status_code == 200
    body = res.get_json()
    assert _identity_result(body)["status"] == "rewrapped"

    store = core_store.get_store(user_id)
    assert identity_service._load_identity(store)["K_user"] == seeded["K_user"]
