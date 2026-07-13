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
