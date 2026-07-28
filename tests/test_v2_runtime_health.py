"""recent_runtime_health：全 lane 运行时健康聚合。"""
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
    reason="DB-backed runtime-health tests require the PostgreSQL test fixture",
)

_TERMINAL = {"completed", "failed", "expired", "superseded"}


@pytest.fixture(autouse=True)
def _clean_tables():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_turn_metrics")
        conn.execute("DELETE FROM agent_jobs")
    yield


def _add_job(
    user_id: str,
    lane: str,
    status: str,
    *,
    age_hours: int = 0,
    last_error: str | None = None,
) -> int:
    """一个 job 一个用户——agent_jobs 有单飞唯一索引，同用户同 lane 不能并存两条在飞行中。"""
    seed_user(user_id)
    set_v2_runtime_owner(user_id)
    job_id, _ = jobs_store.enqueue_job(user_id, lane)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status=%s,"
            "created_at=clock_timestamp()-make_interval(hours => %s),"
            "finished_at=CASE WHEN %s THEN "
            "clock_timestamp()-make_interval(hours => %s) ELSE NULL END,"
            "last_error=%s WHERE id=%s",
            (status, age_hours, status in _TERMINAL, age_hours, last_error, job_id),
        )
    return job_id


def test_recent_runtime_health_groups_outcomes_by_lane():
    _add_job("u_rh_chat_ok", "chat", "completed")
    _add_job("u_rh_chat_ok2", "chat", "completed")
    _add_job("u_rh_chat_bad", "chat", "failed")
    _add_job("u_rh_hb_ok", "heartbeat", "completed")

    health = jobs_store.recent_runtime_health(within_hours=24)

    assert health["window_hours"] == 24
    lanes = {row["lane"]: row for row in health["lanes"]}
    assert lanes["chat"]["completed"] == 2
    assert lanes["chat"]["failed"] == 1
    assert lanes["chat"]["sampled_jobs"] == 3
    assert lanes["chat"]["failure_rate"] == pytest.approx(1 / 3)
    assert lanes["heartbeat"]["completed"] == 1
    assert lanes["heartbeat"]["failure_rate"] == pytest.approx(0.0)
    # lanes 按样本量降序：chat(3) 在 heartbeat(1) 前
    assert [row["lane"] for row in health["lanes"]] == ["chat", "heartbeat"]


def test_recent_runtime_health_excludes_superseded_from_failure_rate():
    # 运行时代际切换不是故障：superseded 单列，既不进分子也不进分母。
    _add_job("u_rh_sup_ok", "chat", "completed")
    _add_job("u_rh_sup_1", "chat", "superseded")
    _add_job("u_rh_sup_2", "chat", "superseded")

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    assert lanes["chat"]["superseded"] == 2
    assert lanes["chat"]["completed"] == 1
    assert lanes["chat"]["failure_rate"] == pytest.approx(0.0)


def test_recent_runtime_health_splits_expiry_reasons():
    _add_job("u_rh_q", "chat", "expired", last_error="queue_timeout")
    _add_job("u_rh_l", "chat", "expired", last_error="lease_timeout")

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    assert lanes["chat"]["expired"] == 2
    assert lanes["chat"]["queue_expired"] == 1
    assert lanes["chat"]["lease_expired"] == 1
    assert lanes["chat"]["failure_rate"] == pytest.approx(1.0)


def test_recent_runtime_health_respects_window():
    _add_job("u_rh_recent", "chat", "completed")
    _add_job("u_rh_old", "chat", "failed", age_hours=48)

    lanes_24 = {r["lane"]: r for r in jobs_store.recent_runtime_health(within_hours=24)["lanes"]}
    lanes_168 = {r["lane"]: r for r in jobs_store.recent_runtime_health(within_hours=168)["lanes"]}

    assert lanes_24["chat"]["sampled_jobs"] == 1
    assert lanes_24["chat"]["failed"] == 0
    assert lanes_168["chat"]["sampled_jobs"] == 2
    assert lanes_168["chat"]["failed"] == 1


def test_recent_runtime_health_zero_sample_rate_is_none():
    # 零样本（全是 superseded）与零失败（有 completed）必须可区分。
    # 该 lane 出现在结果里（不是被整个过滤掉），但 sampled_jobs=0 故 failure_rate=None。
    _add_job("u_rh_sup_only_1", "chat", "superseded")
    _add_job("u_rh_sup_only_2", "chat", "superseded")

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    assert lanes["chat"]["superseded"] == 2
    assert lanes["chat"]["sampled_jobs"] == 0
    assert lanes["chat"]["failure_rate"] is None  # 零样本，不是零失败


def test_recent_runtime_health_is_empty_without_history():
    health = jobs_store.recent_runtime_health()
    assert health["lanes"] == []
    assert health["window_hours"] == 24


def test_recent_runtime_health_latency_ignores_failed_turns():
    # 只算成功回合：一批失败超时回合不得把 p95 拉高，否则一个故障会同时点亮
    # 「失败率」和「延迟」两盏灯，值班时看起来像两个独立故障。
    ok_job = _add_job("u_rh_lat_ok", "chat", "completed")
    bad_job = _add_job("u_rh_lat_bad", "chat", "failed")
    jobs_store.record_whole_turn_metric(
        ok_job, "u_rh_lat_ok", "chat",
        prompt_tokens=10, completion_tokens=5, latency_ms=20_000,
        model_calls=1, retries=0, failed=False, status="ok",
    )
    jobs_store.record_whole_turn_metric(
        bad_job, "u_rh_lat_bad", "chat",
        prompt_tokens=None, completion_tokens=None, latency_ms=550_000,
        model_calls=1, retries=0, failed=True,
        status="turn_failed:providererror",
    )

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    assert lanes["chat"]["p95_ok_ms"] == pytest.approx(20_000)
    assert lanes["chat"]["p50_ok_ms"] == pytest.approx(20_000)


def test_recent_runtime_health_latency_is_none_without_successful_turns():
    bad_job = _add_job("u_rh_lat_none", "chat", "failed")
    jobs_store.record_whole_turn_metric(
        bad_job, "u_rh_lat_none", "chat",
        prompt_tokens=None, completion_tokens=None, latency_ms=99_000,
        model_calls=1, retries=0, failed=True,
        status="turn_failed:responder_error",
    )

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    # 无成功样本 → None（页面显 N/A），绝不能拿失败回合的延迟冒充
    assert lanes["chat"]["p95_ok_ms"] is None
    assert lanes["chat"]["p50_ok_ms"] is None


def test_recent_runtime_health_counts_missing_capture_from_jobs():
    # 有终态 job 但没有 trajectory 流 → missing，必须从 agent_jobs 起算才看得见
    _add_job("u_rh_cap_missing", "chat", "completed")

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    assert lanes["chat"]["capture"]["missing"] == 1
    assert lanes["chat"]["capture"]["complete"] == 0


def test_recent_runtime_health_top_failures_are_enumerated_codes():
    _add_job("u_rh_tf_1", "chat", "failed", last_error="turn_failed:providererror")
    _add_job("u_rh_tf_2", "chat", "failed", last_error="turn_failed:providererror")
    _add_job("u_rh_tf_3", "chat", "failed", last_error="turn_failed:responder_error")

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}
    top = lanes["chat"]["top_failures"]

    assert top[0] == {"code": "turn_failed:providererror", "count": 2}
    assert {"code": "turn_failed:responder_error", "count": 1} in top


def test_recent_runtime_health_reports_pool_and_pending_age():
    _add_job("u_rh_pool_pending", "chat", "pending", age_hours=1)

    pool = jobs_store.recent_runtime_health()["pool"]

    assert pool["pending"] == 1
    assert pool["oldest_pending_age_sec"] >= 3_500  # ~1h
    assert pool["inflight"] >= 1
    assert pool["live_workers"] >= 0
    assert pool["capacity"] >= 0


def test_recent_runtime_health_pool_pending_age_is_none_when_idle():
    _add_job("u_rh_pool_idle", "chat", "completed")

    pool = jobs_store.recent_runtime_health()["pool"]

    assert pool["pending"] == 0
    assert pool["oldest_pending_age_sec"] is None


def test_recent_runtime_health_includes_nonterminal_only_lanes():
    # 回归测试：lane 在窗口内全部 job 都未终态（pending/running）时，
    # 不应从健康视图消失。这是 worker 卡死时最需要看到的症状。
    seed_user("u_rh_nonterminal_only")
    set_v2_runtime_owner("u_rh_nonterminal_only")
    # 直接创建 pending job，不经过 _add_job（它会把 job 标记为终态）
    job_id, _ = jobs_store.enqueue_job("u_rh_nonterminal_only", "capture")

    health = jobs_store.recent_runtime_health()
    lanes = {r["lane"]: r for r in health["lanes"]}

    # capture lane 只有 pending job，无任何终态，但必须出现
    assert "capture" in lanes
    # sampled_jobs = 0（无终态 job）
    assert lanes["capture"]["sampled_jobs"] == 0
    # failure_rate = None（零样本，不是零失败）
    assert lanes["capture"]["failure_rate"] is None
    # capture["open"] >= 1（证明 capture 数据被保住了）
    assert lanes["capture"]["capture"]["open"] >= 1
