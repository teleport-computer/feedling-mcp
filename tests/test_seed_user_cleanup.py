"""Focused guards for conftest.seed_user's function-scope isolation."""

from __future__ import annotations

import copy
import uuid

import db
from accounts import registry
from conftest import _SeedUserTracker


def _uid(label: str) -> str:
    return f"seed_cleanup_{label}_{uuid.uuid4().hex[:12]}"


def _delete_user_state(user_id: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_wake_schedule WHERE user_id=%s", (user_id,))
        conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))
    with registry._users_lock:
        registry._users[:] = [
            row for row in registry._users if row.get("user_id") != user_id
        ]


def test_tracker_removes_new_user_and_wake_rows():
    user_id = _uid("db")
    tracker = _SeedUserTracker("focused-new-db")
    tracker.capture_before(user_id)

    db.upsert_user({"user_id": user_id, "created_at": "2026-08-14T00:00:00+00:00"})
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_wake_schedule (user_id, next_heartbeat_at) "
            "VALUES (%s, '2026-08-14T01:00:00+00:00')",
            (user_id,),
        )

    tracker.restore_before_images()

    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM users WHERE user_id=%s", (user_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM v2_wake_schedule WHERE user_id=%s", (user_id,)
        ).fetchone()[0] == 0


def test_tracker_removes_new_registry_entry():
    user_id = _uid("registry")
    tracker = _SeedUserTracker("focused-new-registry")
    tracker.capture_before(user_id)
    with registry._users_lock:
        registry._users.append({"user_id": user_id, "marker": "new"})

    tracker.restore_before_images()

    with registry._users_lock:
        assert not any(row.get("user_id") == user_id for row in registry._users)


def test_tracker_restores_existing_user_wake_and_registry_before_images():
    user_id = _uid("existing")
    original = {
        "user_id": user_id,
        "created_at": "2026-08-13T00:00:00+00:00",
        "marker": "before",
        "nested": {"value": 1},
    }
    changed = {
        "user_id": user_id,
        "created_at": "2026-08-14T00:00:00+00:00",
        "marker": "after",
        "nested": {"value": 2},
    }

    try:
        db.upsert_user(original)
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_wake_schedule (user_id, next_heartbeat_at) "
                "VALUES (%s, '2026-08-13T01:00:00+00:00')",
                (user_id,),
            )
        with registry._users_lock:
            registry._users.insert(0, copy.deepcopy(original))

        tracker = _SeedUserTracker("focused-existing")
        tracker.capture_before(user_id)

        db.upsert_user(changed)
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE v2_wake_schedule "
                "SET next_heartbeat_at='2026-08-14T02:00:00+00:00' "
                "WHERE user_id=%s",
                (user_id,),
            )
        with registry._users_lock:
            row = next(row for row in registry._users if row.get("user_id") == user_id)
            row.clear()
            row.update(copy.deepcopy(changed))

        tracker.restore_before_images()

        with db.get_pool().connection() as conn:
            user_row = conn.execute(
                "SELECT created_at, doc FROM users WHERE user_id=%s", (user_id,)
            ).fetchone()
            wake_row = conn.execute(
                "SELECT next_heartbeat_at = "
                "'2026-08-13T01:00:00+00:00'::timestamptz "
                "FROM v2_wake_schedule WHERE user_id=%s",
                (user_id,),
            ).fetchone()
        assert user_row == (original["created_at"], original)
        assert wake_row == (True,)
        with registry._users_lock:
            restored = next(
                row for row in registry._users if row.get("user_id") == user_id
            )
            assert restored == original
            assert registry._users.index(restored) == 0
    finally:
        _delete_user_state(user_id)
