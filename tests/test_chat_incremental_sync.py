"""Versioned UserStore chat-cache reconciliation state machine."""

from __future__ import annotations

import logging
import threading
import sys
import os
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from core import store as core_store
import db
from conftest import seed_user


def test_snapshot_fallback_telemetry_is_info_enabled_in_backend_runtime():
    assert core_store.log.isEnabledFor(logging.INFO)


def _row(msg_id: str, seq: int, **extra) -> dict:
    return {"id": msg_id, "seq": seq, "ts": float(seq), **extra}


def _bare_store(rows=(), *, version: int = 0, limit: int = 256):
    store = core_store.UserStore.__new__(core_store.UserStore)
    store.user_id = "usr_incremental"
    store.chat_messages = [dict(row) for row in rows]
    store.chat_lock = threading.Lock()
    store.chat_waiters = []
    store.chat_waiters_lock = threading.Lock()
    store.chat_sync_lock = threading.Lock()
    store.chat_hot_cache_limit = limit
    store.chat_version = version
    store.chat_max_seq = max(
        (int(row.get("seq") or 0) for row in store.chat_messages), default=0
    )
    store._chat_messages_by_id = {
        str(row["id"]): row for row in store.chat_messages
    }
    store._chat_last_version_check_mono = 0.0
    return store


def test_chat_hot_cache_limit_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("FEEDLING_CHAT_HOT_CACHE_LIMIT", raising=False)
    assert core_store._chat_hot_cache_limit() == 5000
    monkeypatch.setenv("FEEDLING_CHAT_HOT_CACHE_LIMIT", "1")
    assert core_store._chat_hot_cache_limit() == 64
    monkeypatch.setenv("FEEDLING_CHAT_HOT_CACHE_LIMIT", "99999")
    assert core_store._chat_hot_cache_limit() == 5000
    monkeypatch.setenv("FEEDLING_CHAT_HOT_CACHE_LIMIT", "broken")
    assert core_store._chat_hot_cache_limit() == 5000


def test_continuous_events_apply_upsert_update_delete_and_wake_once(monkeypatch):
    store = _bare_store([_row("a", 1, value="old")], version=1)
    wakes = []
    monkeypatch.setattr(core_store.db, "chat_change_version", lambda _uid: 4)
    monkeypatch.setattr(
        core_store.db,
        "chat_change_events_after",
        lambda _uid, after, limit: [
            {"version": 2, "operation": "upsert", "message_ids": ["b"]},
            {"version": 3, "operation": "upsert", "message_ids": ["a"]},
            {"version": 4, "operation": "delete", "message_ids": ["b"]},
        ],
    )

    def get_many(_uid, ids):
        assert not store.chat_lock.locked()
        assert ids == ["b", "a"]
        return [_row("a", 1, value="new"), _row("b", 2)]

    monkeypatch.setattr(core_store.db, "chat_get_many_strict", get_many)
    monkeypatch.setattr(store, "notify_chat_waiters", lambda: wakes.append("wake"))

    assert store.ensure_chat_fresh(force=True) is True
    assert store.chat_version == 4
    assert store.chat_messages == [_row("a", 1, value="new")]
    assert store._chat_messages_by_id == {"a": store.chat_messages[0]}
    assert wakes == ["wake"]


@pytest.mark.parametrize(
    ("current", "events", "expected_reason"),
    [
        (3, [{"version": 3, "operation": "upsert", "message_ids": ["c"]}], "gap"),
        (2, [{"version": 2, "operation": "reset", "message_ids": []}], "reset"),
        (300, [], "overflow"),
        (2, [], "gap"),
    ],
    ids=["gap", "reset", "overflow", "expired-history"],
)
def test_gap_reset_overflow_and_expired_history_reload_snapshot(
    monkeypatch, caplog, current, events, expected_reason,
):
    store = _bare_store([_row("a", 1)], version=1)
    reloads = []
    monkeypatch.setattr(core_store.db, "chat_change_version", lambda _uid: current)
    monkeypatch.setattr(
        core_store.db,
        "chat_change_events_after",
        lambda _uid, after, limit: events,
    )

    def reload_hot():
        reloads.append(True)
        with store.chat_lock:
            store._replace_chat_rows_locked(
                [_row("snapshot", 99)], version=current
            )
        return list(store.chat_messages)

    monkeypatch.setattr(store, "reload_chat_hot_strict", reload_hot)
    monkeypatch.setattr(store, "notify_chat_waiters", lambda: None)
    caplog.set_level("INFO", logger="feedling.chat_sync")

    assert store.ensure_chat_fresh(force=True) is True
    assert reloads == [True]
    assert store.chat_version == current
    assert [row["id"] for row in store.chat_messages] == ["snapshot"]
    assert f"reason={expected_reason}" in caplog.text
    assert "user_hash=" in caplog.text
    assert store.user_id not in caplog.text
    assert "message_ids" not in caplog.text


def test_missing_upsert_row_logs_snapshot_fallback_reason(monkeypatch, caplog):
    store = _bare_store([_row("a", 1)], version=1)
    monkeypatch.setattr(core_store.db, "chat_change_version", lambda _uid: 2)
    monkeypatch.setattr(
        core_store.db,
        "chat_change_events_after",
        lambda *_args: [
            {"version": 2, "operation": "upsert", "message_ids": ["missing"]}
        ],
    )
    monkeypatch.setattr(core_store.db, "chat_get_many_strict", lambda *_args: [])
    monkeypatch.setattr(store, "reload_chat_hot_strict", lambda: [])
    monkeypatch.setattr(store, "notify_chat_waiters", lambda: None)
    caplog.set_level("INFO", logger="feedling.chat_sync")

    assert store.ensure_chat_fresh(force=True) is True

    assert "reason=missing_row" in caplog.text
    assert store.user_id not in caplog.text


def test_generation_conflict_logs_snapshot_fallback_reason(monkeypatch, caplog):
    store = _bare_store([_row("a", 1)], version=1)
    monkeypatch.setattr(core_store.db, "chat_change_version", lambda _uid: 2)
    monkeypatch.setattr(
        core_store.db,
        "chat_change_events_after",
        lambda *_args: [
            {"version": 2, "operation": "upsert", "message_ids": ["b"]}
        ],
    )

    def get_many(*_args):
        store.apply_committed_chat_rows([_row("local", 3)])
        return [_row("b", 2)]

    monkeypatch.setattr(core_store.db, "chat_get_many_strict", get_many)
    monkeypatch.setattr(store, "reload_chat_hot_strict", lambda: [])
    monkeypatch.setattr(store, "notify_chat_waiters", lambda: None)
    caplog.set_level("INFO", logger="feedling.chat_sync")

    assert store.ensure_chat_fresh(force=True) is True

    assert "reason=generation_conflict" in caplog.text
    assert store.user_id not in caplog.text


def test_snapshot_fallback_telemetry_rejects_unknown_reason():
    with pytest.raises(ValueError, match="fallback reason"):
        core_store._chat_snapshot_fallback_telemetry(
            user_id="private-user", reason="unknown", hot_rows=1
        )


def test_duplicate_target_and_coalesced_check_do_no_work(monkeypatch):
    store = _bare_store([_row("a", 1)], version=5)
    calls = []
    monkeypatch.setattr(
        core_store.db,
        "chat_change_version",
        lambda _uid: calls.append("version") or 5,
    )
    monkeypatch.setattr(core_store.time, "monotonic", lambda: 100.0)

    assert store.ensure_chat_fresh(force=True, target_version=5) is True
    assert calls == []
    assert store.ensure_chat_fresh() is True
    assert store.ensure_chat_fresh() is True
    assert calls == ["version"]


def test_apply_committed_rows_orders_deduplicates_and_trims():
    store = _bare_store([_row("a", 1), _row("b", 2)], limit=3)

    store.apply_committed_chat_rows([
        _row("d", 4),
        _row("b", 2, changed=True),
        _row("c", 3),
        _row("d", 4, changed=True),
    ], version=7)

    assert [row["id"] for row in store.chat_messages] == ["b", "c", "d"]
    assert store.chat_messages[0]["changed"] is True
    assert store.chat_messages[-1]["changed"] is True
    assert store.chat_version == 7
    assert store.chat_max_seq == 4
    assert set(store._chat_messages_by_id) == {"b", "c", "d"}


def test_failed_ordinary_load_is_open_but_strict_reload_preserves_state(monkeypatch):
    store = _bare_store([_row("kept", 1)], version=3)

    def fail(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(core_store.db, "chat_load_hot_snapshot_strict", fail)
    store._load_chat()
    assert [row["id"] for row in store.chat_messages] == ["kept"]
    assert store.chat_version == 3

    with pytest.raises(RuntimeError, match="database unavailable"):
        store.reload_chat_hot_strict()
    assert [row["id"] for row in store.chat_messages] == ["kept"]
    assert store.chat_version == 3


def test_strict_snapshot_retries_if_local_commit_lands_during_db_read(monkeypatch):
    store = _bare_store([_row("old", 1)], version=1)
    calls = []

    def snapshot(_uid, _limit):
        calls.append(True)
        if len(calls) == 1:
            store.apply_committed_chat_rows([_row("local", 2)])
            return 1, [_row("old", 1)]
        return 2, [_row("old", 1), _row("local", 2)]

    monkeypatch.setattr(
        core_store.db, "chat_load_hot_snapshot_strict", snapshot
    )

    rows = store.reload_chat_hot_strict()
    assert len(calls) == 2
    assert [row["id"] for row in rows] == ["old", "local"]
    assert store.chat_version == 2


def test_strict_snapshot_serializes_final_attempt_after_repeated_local_commits(
    monkeypatch,
):
    store = _bare_store([_row("old", 1)], version=1)
    calls = []

    def snapshot(_uid, _limit):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            assert store.chat_lock.locked() is False
            store.apply_committed_chat_rows([_row("local-a", 2)])
            return 1, [_row("old", 1)]
        if len(calls) == 2:
            assert store.chat_lock.locked() is False
            store.apply_committed_chat_rows([_row("local-b", 3)])
            return 2, [_row("old", 1), _row("local-a", 2)]
        assert store.chat_lock.locked() is True
        return 3, [
            _row("old", 1),
            _row("local-a", 2),
            _row("local-b", 3),
        ]

    monkeypatch.setattr(
        core_store.db, "chat_load_hot_snapshot_strict", snapshot
    )

    rows = store.reload_chat_hot_strict()

    assert calls == [1, 2, 3]
    assert [row["id"] for row in rows] == ["old", "local-a", "local-b"]
    assert store.chat_version == 3
    assert store.chat_max_seq == 3
    assert set(store._chat_messages_by_id) == {"old", "local-a", "local-b"}


def test_strict_snapshot_locked_fallback_failure_preserves_last_good_cache(
    monkeypatch,
):
    store = _bare_store([_row("kept", 1)], version=1)
    calls = []

    def snapshot(_uid, _limit):
        calls.append(len(calls) + 1)
        if len(calls) <= 2:
            store.apply_committed_chat_rows([
                _row(f"local-{len(calls)}", len(calls) + 1)
            ])
            return 1, [_row("kept", 1)]
        assert store.chat_lock.locked() is True
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        core_store.db, "chat_load_hot_snapshot_strict", snapshot
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        store.reload_chat_hot_strict()

    assert calls == [1, 2, 3]
    assert [row["id"] for row in store.chat_messages] == [
        "kept",
        "local-1",
        "local-2",
    ]
    assert store.chat_version == 1


def test_failed_incremental_sync_does_not_wake_or_mutate(monkeypatch):
    store = _bare_store([_row("kept", 1)], version=1)
    wakes = []
    monkeypatch.setattr(core_store.db, "chat_change_version", lambda _uid: 2)
    monkeypatch.setattr(
        core_store.db,
        "chat_change_events_after",
        lambda *_args, **_kwargs: [
            {"version": 2, "operation": "upsert", "message_ids": ["new"]}
        ],
    )
    monkeypatch.setattr(
        core_store.db,
        "chat_get_many_strict",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("database unavailable")
        ),
    )
    monkeypatch.setattr(store, "notify_chat_waiters", lambda: wakes.append(True))

    assert store.ensure_chat_fresh(force=True) is False
    assert store.chat_version == 1
    assert [row["id"] for row in store.chat_messages] == ["kept"]
    assert wakes == []


def test_strict_append_applies_durable_seq_to_local_id_index(monkeypatch):
    store = _bare_store()
    monkeypatch.setattr(
        core_store.db, "chat_append_strict", lambda *_args, **_kwargs: 42
    )
    monkeypatch.setattr(core_store.wake_bus, "notify", lambda *_args: None)
    from proactive import capture_scheduler
    monkeypatch.setattr(
        capture_scheduler, "record_chat_append", lambda *_args, **_kwargs: None
    )

    message = store.append_chat(
        "agent",
        "model_api",
        {
            "id": "reply",
            "v": 1,
            "body_ct": "ciphertext",
            "nonce": "nonce",
            "K_user": "wrapped-key",
            "owner_user_id": store.user_id,
        },
        strict=True,
    )

    assert "seq" not in message
    assert store.chat_messages[0]["seq"] == 42
    assert store._chat_messages_by_id == {
        "reply": store.chat_messages[0]
    }
    assert store.chat_max_seq == 42


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires PostgreSQL"
)
def test_real_postgres_events_reconcile_cross_worker_cache(monkeypatch):
    uid = f"usr_incremental_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    store = _bare_store()
    store.user_id = uid
    monkeypatch.setattr(store, "notify_chat_waiters", lambda: None)
    try:
        db.chat_append_strict(uid, "a", 1.0, {"id": "a", "value": "old"}, 5000)
        db.chat_append_strict(uid, "b", 2.0, {"id": "b"}, 5000)

        assert store.ensure_chat_fresh(force=True) is True
        assert store.chat_version == 2
        assert [row["id"] for row in store.chat_messages] == ["a", "b"]

        db.chat_update_metadata(uid, "a", {"value": "new"})
        db.chat_delete(uid, "b")
        assert store.ensure_chat_fresh(force=True) is True
        assert store.chat_version == 4
        assert store.chat_messages[0]["value"] == "new"
        assert set(store._chat_messages_by_id) == {"a"}
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))
