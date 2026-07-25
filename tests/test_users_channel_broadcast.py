"""Cross-worker `users` channel coverage (Codex review follow-ups).

Under -w N a registry edit on one worker must broadcast so the others reload —
otherwise a worker keeps serving a stale _users / _key_to_user snapshot and a
new user/key 401s, or a public-key / preference / access edit is invisible. The
full-rewrite path (_save_users) and the single-row db.upsert_user paths must
both fire wake_bus.notify("users").

Run:  python -m pytest tests/test_users_channel_broadcast.py -q
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import store as core_store  # noqa: E402
from core import wake_bus  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def captured(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(wake_bus, "notify", lambda ch, uid="": calls.append((ch, uid)))
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()  # not broadcast: default
    return calls


def _register(calls) -> str:
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(os.urandom(32)), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["user_id"]


def test_register_broadcasts_users(captured):
    _register(captured)
    assert ("users", "") in captured  # _save_users(broadcast=True) on register


def test_set_public_key_broadcasts_users(captured):
    uid = _register(captured)
    captured.clear()
    assert registry._set_user_public_key(uid, _b64(os.urandom(32))) is True
    assert ("users", "") in captured  # single-row db.upsert_user path now broadcasts


def test_no_op_public_key_update_does_not_broadcast(captured):
    _register(captured)
    captured.clear()
    # Unknown user -> no write -> no broadcast.
    assert registry._set_user_public_key("usr_does_not_exist", "x") is False
    assert ("users", "") not in captured


def test_load_users_is_lock_guarded_and_reloads(captured):
    uid = _register(captured)
    # Reload (as the wake-bus listener would) must run under _users_lock without
    # deadlock and rebuild the key cache so the user still resolves.
    registry.load_users()
    assert any(u.get("user_id") == uid for u in registry._users)


def test_load_users_normalization_cas_preserves_concurrent_registration(
    captured, monkeypatch
):
    """A reconnect reload must not delete an account absent from its snapshot.

    Force a registration between the registry SELECT and legacy normalization
    persistence. The old full-snapshot save deleted that new row; per-user CAS
    may update only the legacy row it actually read.
    """
    legacy_id = "usr_00000000000000a1"
    concurrent_id = "usr_00000000000000a2"
    legacy = {
        "user_id": legacy_id,
        "api_key_hash": "legacy-hash",
        "created_at": "2026-01-01T00:00:00",
    }
    concurrent = {
        "user_id": concurrent_id,
        "principal_id": "prn_concurrent",
        "api_key_hash": "concurrent-hash",
        "api_keys": [],
        "access_bindings": [],
        "created_at": "2026-01-02T00:00:00",
    }
    registry.db.upsert_user(legacy)
    real_load = registry.db.load_all_users

    def stale_load_then_register(*args, **kwargs):
        snapshot = real_load(*args, **kwargs)
        registry.db.upsert_user(concurrent)
        return snapshot

    monkeypatch.setattr(registry.db, "load_all_users", stale_load_then_register)
    registry.load_users()

    persisted = {row["user_id"]: row for row in real_load()}
    assert concurrent_id in persisted
    assert persisted[concurrent_id] == concurrent
    assert persisted[legacy_id].get("principal_id")


def test_load_users_normalization_cas_does_not_overwrite_same_user_edit(
    captured, monkeypatch
):
    """A stale normalizer must lose to a newer edit of the same account row."""
    user_id = "usr_00000000000000b1"
    legacy = {
        "user_id": user_id,
        "api_key_hash": "legacy-same-user-hash",
        "created_at": "2026-01-01T00:00:00",
    }
    concurrent = {
        "user_id": user_id,
        "principal_id": "prn_newer_edit",
        "api_key_hash": "newer-same-user-hash",
        "api_keys": [],
        "access_bindings": [],
        "archive_language": "zh-Hans",
        "created_at": "2026-01-01T00:00:00",
    }
    registry.db.upsert_user(legacy)
    real_load = registry.db.load_all_users

    def stale_load_then_edit_same_user(*args, **kwargs):
        snapshot = real_load(*args, **kwargs)
        registry.db.upsert_user(concurrent)
        return snapshot

    monkeypatch.setattr(
        registry.db, "load_all_users", stale_load_then_edit_same_user
    )
    registry.load_users()

    persisted = {row["user_id"]: row for row in real_load()}
    assert persisted[user_id] == concurrent
    cached = next(row for row in registry._users if row["user_id"] == user_id)
    assert cached == concurrent
    assert registry._key_to_user["newer-same-user-hash"] == user_id
    assert "legacy-same-user-hash" not in registry._key_to_user


def test_load_users_normalization_db_failure_serves_original_row(
    captured, monkeypatch
):
    """Never expose process-local generated IDs when normalization cannot write."""
    user_id = "usr_00000000000000c1"
    legacy = {
        "user_id": user_id,
        "api_key_hash": "legacy-db-failure-hash",
        "created_at": "2026-01-01T00:00:00",
    }
    registry.db.upsert_user(legacy)
    monkeypatch.setattr(
        registry.db,
        "normalize_user_cas",
        lambda *_args, **_kwargs: (False, None),
    )

    registry.load_users()

    cached = next(row for row in registry._users if row["user_id"] == user_id)
    assert cached == legacy
    assert registry._key_to_user["legacy-db-failure-hash"] == user_id


def test_register_handler_is_idempotent():
    channel = "test_users_handler_idempotence"

    def handler(_user_id: str) -> None:
        return None

    try:
        wake_bus.register_handler(channel, handler)
        wake_bus.register_handler(channel, handler)
        assert wake_bus._extra_handlers[channel] == [handler]
    finally:
        wake_bus._extra_handlers.pop(channel, None)
