"""v2_chat_tail_anchor 的读写（需要 PostgreSQL fixture）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed tail-anchor tests require the PostgreSQL test fixture",
)


def test_absent_anchor_reads_as_none():
    seed_user("u_anchor_1")
    assert jobs_store.get_chat_tail_anchor("u_anchor_1") is None


def test_set_then_get_roundtrip():
    seed_user("u_anchor_2")
    jobs_store.set_chat_tail_anchor("u_anchor_2", 4242)
    assert jobs_store.get_chat_tail_anchor("u_anchor_2") == 4242


def test_anchor_is_monotonic_never_regresses():
    """并发回合可能带着旧值回写；存储层必须自己保证只增不减，
    否则 tail 会突然变长、前缀重排，正是本次优化要消灭的现象。"""
    seed_user("u_anchor_3")
    jobs_store.set_chat_tail_anchor("u_anchor_3", 9000)
    jobs_store.set_chat_tail_anchor("u_anchor_3", 8000)
    assert jobs_store.get_chat_tail_anchor("u_anchor_3") == 9000


def test_anchor_advances_forward():
    seed_user("u_anchor_4")
    jobs_store.set_chat_tail_anchor("u_anchor_4", 100)
    jobs_store.set_chat_tail_anchor("u_anchor_4", 500)
    assert jobs_store.get_chat_tail_anchor("u_anchor_4") == 500


def test_anchor_row_is_deleted_with_the_user():
    """FK ON DELETE CASCADE：删号不留孤儿（v2_user_allowlist 曾因缺 FK 留过孤儿）。"""
    seed_user("u_anchor_5")
    jobs_store.set_chat_tail_anchor("u_anchor_5", 777)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id=%s", ("u_anchor_5",))
        row = conn.execute(
            "SELECT count(*) FROM v2_chat_tail_anchor WHERE user_id=%s",
            ("u_anchor_5",),
        ).fetchone()
    assert row[0] == 0
