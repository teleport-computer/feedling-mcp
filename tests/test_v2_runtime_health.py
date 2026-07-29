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


def _add_metric(
    user_id: str,
    lane: str,
    *,
    prompt: int | None,
    completion: int | None,
    failed: bool = False,
    model_calls: int = 1,
    usage_reported: int = 1,
    cache_read: int | None = None,
    cache_miss: int | None = None,
    age_hours: int = 0,
) -> None:
    """直接写一行 v2_turn_metrics。job_id 传 None——该列的唯一索引允许多个 NULL。"""
    seed_user(user_id)
    jobs_store.record_whole_turn_metric(
        None, user_id, lane,
        prompt_tokens=prompt, completion_tokens=completion, latency_ms=1000,
        model_calls=model_calls, retries=0, failed=failed,
        status="turn_failed:providererror" if failed else "ok",
        cache_read_tokens=cache_read, cache_miss_tokens=cache_miss,
        usage_reported_calls=usage_reported,
    )
    if age_hours:
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE v2_turn_metrics SET created_at=clock_timestamp()"
                "-make_interval(hours => %s) WHERE user_id=%s",
                (age_hours, user_id),
            )


def test_token_usage_by_lane_groups_by_lane():
    _add_metric("u_tok_chat_1", "chat", prompt=1000, completion=100)
    _add_metric("u_tok_chat_2", "chat", prompt=2000, completion=200)
    _add_metric("u_tok_hb", "heartbeat", prompt=500, completion=50)

    lanes = jobs_store.recent_token_usage_by_lane(within_hours=24)["lanes"]

    assert lanes["chat"]["prompt_tokens"] == 3000
    assert lanes["chat"]["completion_tokens"] == 300
    assert lanes["chat"]["total_tokens"] == 3300
    assert lanes["heartbeat"]["prompt_tokens"] == 500
    assert lanes["heartbeat"]["total_tokens"] == 550


def test_token_usage_by_lane_counts_failed_turns():
    # 失败回合照样烧 token（provider 已经算过钱了），必须计入——这是它与延迟
    # 分位数（只算成功回合）口径相反的地方。
    _add_metric("u_tok_ok", "chat", prompt=1000, completion=100, failed=False)
    _add_metric("u_tok_bad", "chat", prompt=3000, completion=0, failed=True)

    lanes = jobs_store.recent_token_usage_by_lane()["lanes"]

    assert lanes["chat"]["prompt_tokens"] == 4000     # 两条都算
    assert lanes["chat"]["model_calls"] == 2


def test_token_usage_by_lane_reports_none_not_zero_without_usage():
    # provider 没回 usage 时不得记成 0 token 混进总量假装正常
    _add_metric("u_tok_nousage", "chat", prompt=None, completion=None, usage_reported=0)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["prompt_tokens"] is None
    assert chat["completion_tokens"] is None
    assert chat["total_tokens"] is None
    assert chat["model_calls"] == 1
    assert chat["usage_reported_calls"] == 0
    assert chat["usage_coverage"] == pytest.approx(0.0)


def test_token_usage_by_lane_coverage_is_none_without_calls():
    # model_calls 为 0 → 覆盖率没有分母，必须是 None 而非 0.0
    _add_metric("u_tok_nocalls", "chat", prompt=None, completion=None,
                model_calls=0, usage_reported=0)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["model_calls"] == 0
    assert chat["usage_coverage"] is None


def test_token_usage_by_lane_cache_hit_ratio():
    _add_metric("u_tok_cache", "chat", prompt=1000, completion=100,
                cache_read=600, cache_miss=400)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["cache_read_tokens"] == 600
    assert chat["cache_miss_tokens"] == 400
    assert chat["cache_hit_ratio"] == pytest.approx(0.6)


def test_token_usage_by_lane_cache_ratio_is_none_without_cache_data():
    _add_metric("u_tok_nocache", "chat", prompt=1000, completion=100,
                cache_read=None, cache_miss=None)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["cache_hit_ratio"] is None


def test_token_usage_by_lane_cache_ratio_is_none_on_partial_report_miss_only():
    # I-4：cache_read=None、cache_miss=500——只有 miss 上报、read 没上报。
    # 必须是 None（"不知道"），不是 0.0（"零命中"，反而误导成缓存完全没生效）。
    # 对齐 users 页既有算法：任一为 None，ratio 就是 None。
    _add_metric("u_tok_partial_miss", "chat", prompt=1000, completion=100,
                cache_read=None, cache_miss=500)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["cache_miss_tokens"] == 500
    assert chat["cache_read_tokens"] is None
    assert chat["cache_hit_ratio"] is None


def test_token_usage_by_lane_cache_ratio_is_none_on_partial_report_read_only():
    # I-4 反向：cache_read=500、cache_miss=None——只有 read 上报。旧算法用
    # `or 0` 兜底会把分母算成 500、分子 500，显示 "100.0%"（缓存完美命中）,
    # 而真相是 miss 根本没上报，不知道真实命中率。reviewer 核过
    # provider_client.py:721-780，Anthropic 只有 cache write 无 cache read 的
    # 回合确实会产出这种组合，是真实路径。
    _add_metric("u_tok_partial_read", "chat", prompt=1000, completion=100,
                cache_read=500, cache_miss=None)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["cache_read_tokens"] == 500
    assert chat["cache_miss_tokens"] is None
    assert chat["cache_hit_ratio"] is None


def test_token_usage_by_lane_respects_window():
    _add_metric("u_tok_recent", "chat", prompt=1000, completion=100)
    _add_metric("u_tok_old", "chat", prompt=9000, completion=900, age_hours=48)

    lanes_24 = jobs_store.recent_token_usage_by_lane(within_hours=24)["lanes"]
    lanes_168 = jobs_store.recent_token_usage_by_lane(within_hours=168)["lanes"]

    assert lanes_24["chat"]["prompt_tokens"] == 1000
    assert lanes_168["chat"]["prompt_tokens"] == 10000


def test_token_usage_by_lane_is_empty_without_history():
    out = jobs_store.recent_token_usage_by_lane()
    assert out["lanes"] == {}
    assert out["window_hours"] == 24
