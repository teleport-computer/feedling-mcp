"""D3 Task 2：lane 优先级 + claim 的可选 lane-allowlist（预留槽位）。

预留槽位场景：一个 worker slot 只被允许抢 {"chat","manual_wake"}，即使有大量
heartbeat/capture 在排队，也绝不会去抢它们——保证聊天回复不被后台唤醒风暴饿死。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store

from conftest import seed_user, set_v2_runtime_owner

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 jobs_store tests require the PostgreSQL test fixture",
)


def _seed_v2(uid: str) -> None:
    seed_user(uid)
    set_v2_runtime_owner(uid)


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    """镜像 test_v2_jobs_store.py：claim_next_job 是全局队列 claim（不按
    user_id 过滤），上一测试遗留的 pending 行会污染 ORDER BY，必须每测试前清空。"""
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def test_reserved_slot_skips_background_lanes_until_chat_arrives():
    _seed_v2("u_res_1")
    _seed_v2("u_res_2")
    _seed_v2("u_res_3")
    jobs_store.enqueue_job("u_res_1", "heartbeat")
    jobs_store.enqueue_job("u_res_2", "capture")

    # Reserved slot only allowed to pull chat/manual_wake — nothing claimable yet.
    claimed = jobs_store.claim_next_job("w1", lanes={"chat", "manual_wake"})
    assert claimed is None

    chat_id, _ = jobs_store.enqueue_job("u_res_3", "chat")
    claimed = jobs_store.claim_next_job("w2", lanes={"chat", "manual_wake"})
    assert claimed is not None
    assert claimed["id"] == chat_id
    assert claimed["lane"] == "chat"


def test_general_slot_priority_order_chat_before_heartbeat():
    _seed_v2("u_gen_1")
    _seed_v2("u_gen_2")
    hb_id, _ = jobs_store.enqueue_job("u_gen_1", "heartbeat")
    chat_id, _ = jobs_store.enqueue_job("u_gen_2", "chat")  # enqueued later in time

    first = jobs_store.claim_next_job("w3", lanes=None)
    assert first is not None
    assert first["id"] == chat_id  # priority 100 > 50 wins despite later created_at

    second = jobs_store.claim_next_job("w4", lanes=None)
    assert second is not None
    assert second["id"] == hb_id

    third = jobs_store.claim_next_job("w5", lanes=None)
    assert third is None


def test_lane_derived_default_priority_and_explicit_override():
    seed_user("u_prio_1")
    seed_user("u_prio_2")
    seed_user("u_prio_3")

    chat_id, _ = jobs_store.enqueue_job("u_prio_1", "chat")
    capture_id, _ = jobs_store.enqueue_job("u_prio_2", "capture")
    hb_id, _ = jobs_store.enqueue_job("u_prio_3", "heartbeat", priority=7)

    with db.get_pool().connection() as conn:
        rows = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT id, priority FROM agent_jobs WHERE id = ANY(%s)",
                ([chat_id, capture_id, hb_id],),
            ).fetchall()
        }

    assert rows[chat_id] == 100
    assert rows[capture_id] == 10
    assert rows[hb_id] == 7  # explicit priority honored, not lane-derived
