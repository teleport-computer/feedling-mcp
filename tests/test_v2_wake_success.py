"""jobs_store.wake_success_stats — V2 唤醒成功率的 lane 级独立计量（D3 Task 8）。

地雷1（同 legacy daily-report 曾踩的坑）：wake job 的 `completed` 终态本身就是
成功——即使这一轮判断"这次不用发消息"（silence 是合法结果），也要计入成功，不能
因为没发消息就当失败。V2 唤醒（heartbeat/scheduled/manual_wake）走 agent_jobs，
不写 legacy proactive_jobs 流，所以这是一条独立的、只读 agent_jobs 的新查询——不是
去修 legacy daily-report 那条口径。
"""
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
    reason="DB-backed V2 wake-success tests require the PostgreSQL test fixture",
)


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    """Truncate before each test so the lane-scoped wake stats only ever see the
    rows a given test inserts itself (mirrors _clean_agent_jobs_table in
    test_v2_jobs_store.py — wake_success_stats aggregates across ALL users, so a
    row left behind by another test would silently pollute the count)."""
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _insert_job(user_id, lane, status, *, finished_hours_ago=1.0):
    # make_interval's `hours` arg is INT, so pass elapsed time via `secs`
    # (double precision) instead of casting a fractional hour count to hours.
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, finished_at) "
            "VALUES (%s, %s, %s, now() - make_interval(secs => %s))",
            (user_id, lane, status, float(finished_hours_ago) * 3600.0),
        )


def test_wake_success_stats_counts_completed_as_success_and_failed_as_failure():
    seed_user("u_wk_1")
    _insert_job("u_wk_1", "heartbeat", "completed")
    _insert_job("u_wk_1", "heartbeat", "completed")
    _insert_job("u_wk_1", "heartbeat", "completed")
    _insert_job("u_wk_1", "scheduled", "failed")
    _insert_job("u_wk_1", "manual_wake", "completed")

    stats = jobs_store.wake_success_stats()

    assert stats["completed"] == 4
    assert stats["failed"] == 1
    assert stats["success_rate"] == pytest.approx(0.8)
    assert stats["by_lane"]["heartbeat"]["completed"] == 3
    assert stats["by_lane"]["scheduled"]["failed"] == 1
    assert stats["by_lane"]["manual_wake"]["completed"] == 1


def test_wake_success_stats_ignores_non_wake_lanes():
    seed_user("u_wk_2")
    _insert_job("u_wk_2", "chat", "completed")
    _insert_job("u_wk_2", "maintenance", "failed")
    _insert_job("u_wk_2", "capture", "completed")

    stats = jobs_store.wake_success_stats()

    assert stats["completed"] == 0
    assert stats["failed"] == 0
    assert stats["success_rate"] is None
    assert stats["by_lane"] == {}


def test_wake_success_stats_excludes_jobs_outside_the_window():
    seed_user("u_wk_3")
    _insert_job("u_wk_3", "heartbeat", "completed", finished_hours_ago=1.0)
    _insert_job("u_wk_3", "heartbeat", "failed", finished_hours_ago=30.0)  # outside 24h window

    stats = jobs_store.wake_success_stats(within_hours=24)

    assert stats["completed"] == 1
    assert stats["failed"] == 0
    assert stats["success_rate"] == pytest.approx(1.0)


def test_wake_success_stats_no_wake_jobs_is_none_rate():
    stats = jobs_store.wake_success_stats()

    assert stats["completed"] == 0
    assert stats["failed"] == 0
    assert stats["expired"] == 0
    assert stats["success_rate"] is None
    assert stats["by_lane"] == {}
