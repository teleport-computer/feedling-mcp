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


def test_valid_active_claims_matches_job_and_owner_as_one_fence():
    _seed_v2("u_claim_snapshot")
    job_id, _ = jobs_store.enqueue_job("u_claim_snapshot", "chat")
    claimed = jobs_store.claim_next_job("worker:foreground:0:g7")
    assert claimed is not None

    assert jobs_store.valid_active_claims(
        [
            (job_id, "worker:foreground:0:g7"),
            (job_id, "worker:foreground:0:g8"),
            (job_id + 1, "worker:foreground:0:g7"),
        ]
    ) == {(job_id, "worker:foreground:0:g7")}

    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='cancelled', finished_at=now() WHERE id=%s",
            (job_id,),
        )

    assert jobs_store.valid_active_claims(
        [(job_id, "worker:foreground:0:g7")]
    ) == set()


def test_valid_reconcile_claims_accepts_owned_completion_but_not_cancellation():
    """A worker-owned terminal commit precedes its final pipe message.

    Reconciliation must tolerate that bounded unwind window, while an external
    cancellation remains an invalid claim that requires slot cancellation.
    """
    _seed_v2("u_reconcile_terminal_owner")
    completed_id, _ = jobs_store.enqueue_job(
        "u_reconcile_terminal_owner", "chat"
    )
    completed = jobs_store.claim_next_job("worker:foreground:0:g7")
    assert completed is not None and completed["id"] == completed_id
    assert jobs_store.mark_running(
        completed_id, claimed_by="worker:foreground:0:g7"
    ) is True
    assert jobs_store.mark_completed(
        completed_id, claimed_by="worker:foreground:0:g7"
    ) is True

    failed_id, _ = jobs_store.enqueue_job(
        "u_reconcile_terminal_owner", "profile"
    )
    failed = jobs_store.claim_next_job(
        "worker:heavy:0:g9", lanes={"profile"}
    )
    assert failed is not None and failed["id"] == failed_id
    assert jobs_store.mark_running(
        failed_id, claimed_by="worker:heavy:0:g9"
    ) is True
    assert jobs_store.mark_failed(
        failed_id, "provider_failed", claimed_by="worker:heavy:0:g9"
    ) is True

    cancelled_id, _ = jobs_store.enqueue_job(
        "u_reconcile_terminal_owner", "chat"
    )
    cancelled = jobs_store.claim_next_job("worker:foreground:1:g8")
    assert cancelled is not None and cancelled["id"] == cancelled_id
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='cancelled',finished_at=now() "
            "WHERE id=%s",
            (cancelled_id,),
        )

    assert jobs_store.valid_reconcile_claims(
        [
            (completed_id, "worker:foreground:0:g7"),
            (completed_id, "worker:foreground:0:wrong"),
            (failed_id, "worker:heavy:0:g9"),
            (cancelled_id, "worker:foreground:1:g8"),
        ]
    ) == {
        (completed_id, "worker:foreground:0:g7"),
        (failed_id, "worker:heavy:0:g9"),
    }
