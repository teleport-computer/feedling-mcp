"""Targeted per-user `users` reload (resident-heartbeat reload-storm fix).

The resident poll heartbeat rewrites ONE user row (``access_bindings[resident]
.last_seen_at``) about once per minute per online resident. It used to pair that
per-row write with the untargeted ``users`` broadcast, so every subscribing
process re-read the WHOLE users table: measured on prod 2026-07-24 at 36 row
writes/min -> 38.7 users-NOTIFY/min -> ~26k row reads/min across 7 processes,
growing linearly with the number of online residents.

The heartbeat now broadcasts with its user_id and the wake-bus handler refreshes
only that row. Genuine registry edits (registration, key issue, public key,
preferences) keep the untargeted full reload — see test_users_channel_broadcast.

Run:  python -m pytest tests/test_users_reload_targeted.py -q
"""
from __future__ import annotations

import base64
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from core import wake_bus  # noqa: E402


_CONSUMER_HEADERS = {
    "X-Feedling-Consumer": "feedling-chat-resident",
    "X-Feedling-Consumer-Id": "resident-targeted-reload-test",
    "X-Feedling-Consumer-Version": "resident-v1",
}


@pytest.fixture()
def notifies(monkeypatch, tmp_path):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(wake_bus, "notify", lambda ch, uid="": calls.append((ch, uid)))
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    calls.clear()
    return calls


def _register(client=None) -> tuple[str, str]:
    res = (client or make_client()).post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(os.urandom(32)).decode("ascii"),
              "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _resident(entry: dict) -> dict | None:
    return next(
        (b for b in entry.get("access_bindings") or []
         if isinstance(b, dict) and b.get("access_mode") == "resident"),
        None,
    )


def test_resident_heartbeat_broadcasts_only_its_own_user(notifies):
    """The per-minute liveness write must not order a full-registry reload."""
    with make_client() as client:
        user_id, api_key = _register(client)
        stale = (datetime.now() - timedelta(hours=8)).isoformat()
        with registry._users_lock:
            entry = registry._find_user_entry_locked(user_id)
            binding = registry._upsert_access_binding_locked(entry, "resident")
            binding["last_seen_at"] = stale
            binding["updated_at"] = stale
            registry.persist_user(entry)
        notifies.clear()

        poll = client.get(
            "/v1/chat/poll?timeout=0",
            headers={"X-API-Key": api_key, **_CONSUMER_HEADERS},
        )
        assert poll.status_code == 200, poll.get_data(as_text=True)

    users_notifies = [uid for ch, uid in notifies if ch == "users"]
    assert users_notifies == [user_id], (
        "the resident heartbeat must broadcast its own user_id so other "
        f"processes reload one row, got {users_notifies!r}"
    )


def test_targeted_notify_refreshes_one_row_without_reading_the_table(notifies, monkeypatch):
    """A ``users`` notify carrying a user_id reloads that row only."""
    user_id, _api_key = _register()
    other_id, _other_key = _register()
    fresh = datetime.now().isoformat()

    # Another process persists the heartbeat; this process still holds the old row.
    persisted = next(u for u in db.load_all_users() if u["user_id"] == user_id)
    binding = registry._upsert_access_binding_locked(persisted, "resident")
    binding["last_seen_at"] = fresh
    binding["updated_at"] = fresh
    db.upsert_user(persisted)

    full_loads: list[int] = []
    monkeypatch.setattr(
        db, "load_all_users", lambda: full_loads.append(1) or []
    )
    registry.reload_users_after_notify(user_id)

    assert full_loads == [], "a targeted users notify must not re-read the whole table"
    with registry._users_lock:
        refreshed = registry._find_user_entry_locked(user_id)
        assert _resident(refreshed)["last_seen_at"] == fresh
        assert registry._find_user_entry_locked(other_id) is not None


def test_untargeted_notify_still_reloads_the_whole_registry(notifies, monkeypatch):
    user_id, _api_key = _register()
    real_load = db.load_all_users
    full_loads: list[int] = []

    def spy(*args, **kwargs):
        full_loads.append(1)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(db, "load_all_users", spy)
    registry.reload_users_after_notify("")

    assert full_loads == [1]
    assert any(u.get("user_id") == user_id for u in registry._users)


def _cached_uid(api_key: str) -> str | None:
    """The in-memory key cache only — never the DB miss-fallback."""
    return registry._key_to_user.get(registry._hash_api_key(api_key))


def test_targeted_reload_adds_an_unknown_user_and_keeps_its_key_resolvable(notifies):
    """A registration broadcast targeted at a user this process never saw."""
    user_id, api_key = _register()
    with registry._users_lock:
        registry._users[:] = [u for u in registry._users if u.get("user_id") != user_id]
        registry._rebuild_key_cache()
    assert _cached_uid(api_key) is None

    registry.reload_users_after_notify(user_id)

    assert any(u.get("user_id") == user_id for u in registry._users)
    assert _cached_uid(api_key) == user_id


def test_targeted_reload_of_an_unseen_user_clears_the_negative_key_cache(notifies):
    """An inserted row may hold a key an earlier lookup negative-cached."""
    user_id, api_key = _register()
    key_hash = registry._hash_api_key(api_key)
    with registry._users_lock:
        registry._users[:] = [u for u in registry._users if u.get("user_id") != user_id]
        registry._rebuild_key_cache()
    assert registry._resolve_user(api_key) == user_id  # DB fallback re-appends
    with registry._users_lock:
        registry._users[:] = [u for u in registry._users if u.get("user_id") != user_id]
        registry._rebuild_key_cache()
    with registry._resolve_neg_lock:
        registry._resolve_neg_cache[key_hash] = registry.time.monotonic() + 3600
    assert registry._neg_cached(key_hash) is True

    registry.reload_users_after_notify(user_id)

    assert registry._neg_cached(key_hash) is False
    assert _cached_uid(api_key) == user_id


def test_targeted_reload_drops_a_row_deleted_elsewhere(notifies):
    user_id, api_key = _register()
    db.delete_user(user_id)

    registry.reload_users_after_notify(user_id)

    assert not any(u.get("user_id") == user_id for u in registry._users)
    assert _cached_uid(api_key) is None


def test_targeted_reload_read_failure_keeps_the_existing_row(notifies, monkeypatch):
    """A transient read failure must never blank the registry (cf. the
    lifespan-missing-load_users incident: an empty _users 401s every request)."""
    user_id, _api_key = _register()

    def boom(_user_id):
        raise RuntimeError("transient users read failure")

    monkeypatch.setattr(db, "load_user", boom)
    monkeypatch.setattr(db, "load_all_users", lambda: [])
    registry.reload_users_after_notify(user_id)

    assert any(u.get("user_id") == user_id for u in registry._users)
