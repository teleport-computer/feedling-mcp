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


def _insert_job(user_id, lane, status, *, finished_hours_ago=1.0, last_error=None):
    # make_interval's `hours` arg is INT, so pass elapsed time via `secs`
    # (double precision) instead of casting a fractional hour count to hours.
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, finished_at, last_error) "
            "VALUES (%s, %s, %s, now() - make_interval(secs => %s), %s)",
            (user_id, lane, status, float(finished_hours_ago) * 3600.0, last_error),
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


# ---------------------------------------------------------------------------
# memory_lane_health — capture / dream 的舰队级健康度
#
# 补这条的由来（2026-07-31）：用户报"切到 V2 后晚上不整理记忆了"，而当时
# /v1/admin/v2-metrics 里没有任何记忆车道的数字，只能逐个用户比对 bootstrap_events
# 才看出来。没有用户报障的话，记忆整理全线停摆我们发现不了——它坏掉的样子就是
# "什么都没发生"。
# ---------------------------------------------------------------------------


def test_memory_lane_health_counts_capture_and_dream():
    seed_user("u_mem_1")
    _insert_job("u_mem_1", "capture", "completed")
    _insert_job("u_mem_1", "capture", "completed")
    _insert_job("u_mem_1", "capture", "failed")
    _insert_job("u_mem_1", "dream", "completed")
    _insert_job("u_mem_1", "dream", "expired")

    stats = jobs_store.memory_lane_health()

    assert stats["completed"] == 3
    assert stats["failed"] == 1
    assert stats["expired"] == 1
    assert stats["success_rate"] == pytest.approx(3 / 5)
    assert stats["by_lane"]["capture"] == {"completed": 2, "failed": 1}
    assert stats["by_lane"]["dream"] == {"completed": 1, "expired": 1}


def test_memory_lane_health_counts_a_zero_card_run_as_success():
    """capture 跑完发现没什么值得记，是合法结果，不是失败。

    与 wake 的"silence 也算成功"同一条判据。把 noop 当失败会让成功率长期偏低，
    真正的故障反而淹没在噪声里。终态是 completed 就是成功——这里 job 层面看不到
    卡数，也不该看。
    """
    seed_user("u_mem_noop")
    _insert_job("u_mem_noop", "capture", "completed")

    stats = jobs_store.memory_lane_health()

    assert stats["success_rate"] == 1.0
    assert stats["failed"] == 0


def test_memory_lane_health_and_wake_do_not_contaminate_each_other():
    """两个指标必须互不含对方的车道。

    这是这条指标单独存在的全部理由：把 capture/dream 折进 wake，会让"做梦大面积
    失败"表现成"唤醒成功率下降"，排查的人就去查唤醒了（TESTING §2-N 的口径漂移）。
    """
    seed_user("u_mix")
    _insert_job("u_mix", "heartbeat", "completed")
    _insert_job("u_mix", "capture", "failed")
    _insert_job("u_mix", "dream", "failed")

    wake = jobs_store.wake_success_stats()
    memory = jobs_store.memory_lane_health()

    # 两条记忆车道的失败，一条都不许算进唤醒
    assert wake["failed"] == 0
    assert wake["success_rate"] == 1.0
    assert set(wake["by_lane"]) == {"heartbeat"}

    # 反向同理：唤醒的成功不许被记忆车道借去充数
    assert memory["completed"] == 0
    assert memory["failed"] == 2
    assert set(memory["by_lane"]) == {"capture", "dream"}


def test_memory_lane_health_breaks_down_failure_reasons():
    """「保险丝在熔断」和「provider 在挂」必须分开看(2026-08-05 dream 阀门重构)。

    出口闸从「按提案静默丢」改成「明显不对就让整个 job 失败」之后,失败侧不细分
    的话这两种情况在成功率上长得一模一样,阀门就没有刻度。"""
    seed_user("u_mem_reasons")
    _insert_job("u_mem_reasons", "dream", "failed",
                last_error="extraction_failed:dream_blast_radius_exceeded")
    _insert_job("u_mem_reasons", "dream", "failed",
                last_error="extraction_failed:upstream_unavailable")
    _insert_job("u_mem_reasons", "capture", "failed", last_error=None)
    _insert_job("u_mem_reasons", "dream", "completed")

    stats = jobs_store.memory_lane_health()

    assert stats["failed_reasons"]["dream"] == {
        "extraction_failed:dream_blast_radius_exceeded": 1,
        "extraction_failed:upstream_unavailable": 1,
    }
    assert stats["failed_reasons"]["capture"] == {"unknown": 1}


def test_memory_lane_health_is_explicit_when_nothing_ran():
    """没有历史时 success_rate 是 None，不是 0。

    0 会被读成"全挂了"，None 才是"无从判断"。同 wake_success_stats 的约定。
    """
    stats = jobs_store.memory_lane_health()

    assert stats == {
        "completed": 0, "failed": 0, "expired": 0,
        "success_rate": None, "by_lane": {}, "failed_reasons": {},
    }
