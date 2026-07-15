"""Stable seq cursor: chat_messages_after_seq/chat_max_seq order by seq (never
ts), and cursor.advance_effect derives a proper outbox 'cursor' effect (spec A1).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import cursor

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 cursor tests require the PostgreSQL test fixture",
)


@pytest.fixture
def pg_clean():
    """Truncate chat_messages so a leftover row from another test/module
    sharing the session-scoped DB can't shift a reused user_id's seq
    ordering or max_seq."""
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE chat_messages CASCADE")
    yield


def test_after_seq_orders_by_seq_not_ts(pg_clean):
    # Two messages with IDENTICAL ts must come back in seq (insertion) order,
    # both present — a ts-based cursor could tie-break arbitrarily or drop one.
    seed_user("u_cur1")
    db.chat_append("u_cur1", "m1", 100.0, {"id": "m1", "ts": 100.0, "role": "user"}, 5000)
    db.chat_append("u_cur1", "m2", 100.0, {"id": "m2", "ts": 100.0, "role": "user"}, 5000)
    out = db.chat_messages_after_seq("u_cur1", 0, limit=10)
    assert [m["id"] for m in out] == ["m1", "m2"]
    assert db.chat_max_seq("u_cur1") == out[-1]["seq"]


def test_after_seq_respects_after_and_limit(pg_clean):
    seed_user("u_cur2")
    for i in range(5):
        db.chat_append("u_cur2", f"m{i}", float(i), {"id": f"m{i}", "ts": float(i), "role": "user"}, 5000)
    first_seq = db.chat_messages_after_seq("u_cur2", 0, limit=1)[0]["seq"]
    out = db.chat_messages_after_seq("u_cur2", first_seq, limit=2)
    assert len(out) == 2
    assert [m["id"] for m in out] == ["m1", "m2"]
    assert db.chat_max_seq("u_cur2") == first_seq + 4


def test_newest_seq_window_is_bounded_then_restored_to_ascending(pg_clean):
    seed_user("u_cur_newest")
    for i in range(5):
        db.chat_append(
            "u_cur_newest", f"m{i}", float(i),
            {"id": "stale", "ts": -1.0, "role": "user", "n": i}, 5000,
        )

    rows = db.chat_messages_after_seq(
        "u_cur_newest", 0, limit=2, oldest_first=False,
    )

    assert [row["id"] for row in rows] == ["m3", "m4"]
    assert [row["ts"] for row in rows] == [3.0, 4.0]
    assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
    assert db.chat_seqs_after_seq(
        "u_cur_newest", 0, limit=2, oldest_first=False,
    ) == [row["seq"] for row in rows]


def test_unbounded_seq_window_returns_every_row_after_cursor(pg_clean):
    seed_user("u_cur_unbounded")
    for i in range(4):
        db.chat_append(
            "u_cur_unbounded", f"m{i}", 100.0,
            {"role": "user", "n": i}, 5000,
        )
    first = db.chat_messages_after_seq("u_cur_unbounded", 0, limit=1)[0]["seq"]

    rows = db.chat_messages_after_seq("u_cur_unbounded", first, limit=None)

    assert [row["n"] for row in rows] == [1, 2, 3]
    assert db.chat_seqs_after_seq("u_cur_unbounded", first) == [
        row["seq"] for row in rows
    ]


def test_through_seq_freezes_both_row_and_identity_windows(pg_clean):
    uid = "u_cur_snapshot"
    seed_user(uid)
    for i in range(5):
        db.chat_append(
            uid, f"m{i}", 100.0, {"role": "user", "n": i}, 5000,
        )
    all_rows = db.chat_messages_after_seq(uid, 0)
    through_seq = all_rows[3]["seq"]

    rows = db.chat_messages_after_seq(
        uid, 0, limit=2, oldest_first=False, through_seq=through_seq,
    )

    assert [row["n"] for row in rows] == [2, 3]
    assert db.chat_seqs_after_seq(
        uid, 0, limit=2, oldest_first=False, through_seq=through_seq,
    ) == [row["seq"] for row in rows]
    assert all(row["seq"] <= through_seq for row in rows)


def test_max_seq_zero_for_unknown_user(pg_clean):
    assert db.chat_max_seq("u_cur_never_wrote") == 0
    assert db.chat_messages_after_seq("u_cur_never_wrote", 0, limit=10) == []


def test_cursor_advance_is_a_cursor_effect():
    eid, payload = cursor.advance_effect(job_id=7, ordinal=3, generation=2, new_seq=42)
    assert payload == {"new_seq": 42}
    assert eid == "job7:cursor:3"


def test_cursor_advance_is_deterministic_and_distinct():
    a = cursor.advance_effect(job_id=1, ordinal=0, generation=1, new_seq=10)
    b = cursor.advance_effect(job_id=1, ordinal=0, generation=1, new_seq=10)
    c = cursor.advance_effect(job_id=1, ordinal=1, generation=1, new_seq=10)
    assert a == b
    assert a[0] != c[0]


class _FakeStore:
    def __init__(self, user_id: str):
        self.user_id = user_id


def test_load_seq_defaults_to_zero_when_unset(pg_clean):
    seed_user("u_cur_loadseq")
    assert cursor.load_seq(_FakeStore("u_cur_loadseq")) == 0


def test_load_seq_reads_back_committed_value(pg_clean):
    seed_user("u_cur_loadseq2")
    db.set_blob("u_cur_loadseq2", "model_api_runtime", {cursor.CURSOR_KEY: 42})
    assert cursor.load_seq(_FakeStore("u_cur_loadseq2")) == 42


@pytest.mark.parametrize("bad", [-1, 1.5, True, "1.5", "broken", {}])
def test_load_seq_rejects_corrupt_cursor_instead_of_replaying_from_zero(pg_clean, bad):
    uid = f"u_cur_bad_{str(bad).replace(' ', '_')}"
    seed_user(uid)
    db.set_blob_strict(uid, "model_api_runtime", {cursor.CURSOR_KEY: bad})

    with pytest.raises(ValueError, match="non-negative integer"):
        cursor.load_seq(_FakeStore(uid))


def test_load_seq_propagates_strict_database_failure(monkeypatch):
    monkeypatch.setattr(
        db, "get_blob_strict",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    with pytest.raises(RuntimeError, match="db down"):
        cursor.load_seq(_FakeStore("u_cur_db_down"))


def test_atomic_integer_advance_is_monotonic_and_preserves_siblings(pg_clean):
    uid = "u_cur_atomic_max"
    seed_user(uid)
    db.set_blob_strict(
        uid, "model_api_runtime",
        {cursor.CURSOR_KEY: 20, "hosted_runtime_mode": "db_action_v2"},
    )

    older = db.advance_blob_int_strict(
        uid, "model_api_runtime", cursor.CURSOR_KEY, 10,
    )
    newer = db.advance_blob_int_strict(
        uid, "model_api_runtime", cursor.CURSOR_KEY, 30,
    )

    assert older[cursor.CURSOR_KEY] == 20
    assert newer[cursor.CURSOR_KEY] == 30
    assert newer["hosted_runtime_mode"] == "db_action_v2"
