"""recent_runtime_health：全 lane 运行时健康聚合。"""
from __future__ import annotations

import base64
import inspect
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
        # v2_effect_outbox.job_id 没有 FK（只有 user_id 有），所以删 agent_jobs
        # 不会把它带走——必须显式清，否则上一个测试的积压会漏进下一个测试的
        # 交付断言。另两张表虽然 CASCADE 得掉，一并显式列出以免下次谁改了 FK。
        conn.execute("DELETE FROM v2_effect_outbox")
        conn.execute("DELETE FROM v2_mcp_mutation_attempts")
        conn.execute("DELETE FROM v2_terminal_failure_outbox")
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
    assert lanes["chat"]["capture"]["terminal_seen_no_gap"] == 0


def _trajectory_envelope(user_id: str, item_id: str) -> dict:
    return {
        "v": 1,
        "id": item_id,
        "owner_user_id": user_id,
        "visibility": "shared",
        "body_ct": base64.b64encode(b"metadata-only").decode(),
        "nonce": "nonce",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
    }


def _add_trajectory_events(job_id: int, user_id: str, *event_kinds: str) -> None:
    for index, event_kind in enumerate(event_kinds):
        jobs_store.append_trajectory_event(
            job_id,
            user_id,
            event_kind=event_kind,
            idempotency_key=f"rh_{job_id}_{index}_{event_kind}",
            payload_envelope=_trajectory_envelope(
                user_id, f"rh-{job_id}-{index}"
            ),
            payload_bytes=64,
        )


def test_recent_runtime_health_capture_bucket_is_named_for_what_it_proves():
    # 这个桶只证明「见到 turn_terminal 且无 capture_gap」。它此前叫 complete，
    # 而 complete 会被读成"轨迹可完整回放"——那是它证明不了的事（prompt /
    # provider 往返 / tool call / 最终回复都可能缺）。名字必须收窄到证据本身。
    job_id = _add_job("u_rh_cap_terminal", "chat", "completed")
    _add_trajectory_events(job_id, "u_rh_cap_terminal", "turn_terminal")

    capture = {
        r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]
    }["chat"]["capture"]

    assert capture["terminal_seen_no_gap"] == 1
    assert capture["partial"] == 0
    # 旧名不得残留：留着它，下一个读者仍会按"完整"去理解这个数。
    assert "complete" not in capture


def test_recent_runtime_health_capture_gap_lands_in_partial_not_terminal_bucket():
    # 有 capture_gap 的回合即使已见 turn_terminal 也算 partial：取证已经损失。
    job_id = _add_job("u_rh_cap_gap", "chat", "completed")
    _add_trajectory_events(
        job_id, "u_rh_cap_gap", "capture_gap", "turn_terminal"
    )

    capture = {
        r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]
    }["chat"]["capture"]

    assert capture["partial"] == 1
    assert capture["terminal_seen_no_gap"] == 0


def test_recent_runtime_health_counts_full_window_without_sampling_cap():
    # 回归测试（2026-07-30 审计）：四条子查询此前各带 LIMIT 1000，于是 24h 档
    # 写着"24 小时"、实际是"最近 1000 个 job"，而同页 token 列是窗口内全量——
    # 两列不是同一批样本，长窗口下无法对账，且故障总量被**静默少报**。
    # 1200 > 旧上界 1000：加回上界这个测试立刻变红。
    seed_user("u_rh_full_window")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id,lane,status,created_at,finished_at) "
            "SELECT %s,'chat','completed',clock_timestamp(),clock_timestamp() "
            "FROM generate_series(1,1200)",
            ("u_rh_full_window",),
        )

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    assert lanes["chat"]["sampled_jobs"] == 1200
    assert lanes["chat"]["completed"] == 1200


def test_recent_runtime_health_takes_no_limit_argument():
    # 采样上界是正确性缺陷而不是性能旋钮，别让它悄悄回来：签名里不该再有 limit。
    assert "limit" not in inspect.signature(jobs_store.recent_runtime_health).parameters


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
    model_calls: int = 1,
    retries: int = 0,
    failed: bool = False,
    usage_reported: int = 1,
    cache_read: int | None = None,
    cache_write: int | None = None,
    cache_miss: int | None = None,
    cache_reported: int = 0,
    provider: str | None = None,
    model: str | None = None,
    route: str | None = None,
    age_hours: int = 0,
) -> None:
    """直接写一行 v2_turn_metrics。job_id 传 None——该列的唯一索引允许多个 NULL。"""
    seed_user(user_id)
    jobs_store.record_whole_turn_metric(
        None, user_id, lane,
        prompt_tokens=prompt, completion_tokens=completion, latency_ms=1000,
        model_calls=model_calls, retries=retries, failed=failed,
        status="turn_failed:providererror" if failed else "ok",
        cache_read_tokens=cache_read, cache_write_tokens=cache_write,
        cache_miss_tokens=cache_miss,
        usage_reported_calls=usage_reported, cache_reported_calls=cache_reported,
        provider=provider, model=model, cache_route_fingerprint=route,
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


def test_token_usage_by_lane_reports_cache_coverage_separately():
    """cache coverage 与 usage coverage 是两个不同的东西，必须分别可得。

    2026-07-30 审计指出：页面把 `cache_hit_ratio · usage_coverage` 挤在一列、标签
    写成「缓存命中 · 上报」，读者会把那个「上报」理解成 cache 上报，而它其实是
    token usage 上报。`cache_reported_calls` 一直在写入路径里采集、聚合查询却从没
    取过它——真正的 cache coverage 此前不可得。
    """
    # 2 次调用：1 次报了 cache 指标、1 次没报；usage 则两次都报了
    _add_metric("u_cov_a", "chat", prompt=1000, completion=100,
                usage_reported=1, cache_reported=1,
                cache_read=600, cache_miss=400)
    _add_metric("u_cov_b", "chat", prompt=500, completion=50,
                usage_reported=1, cache_reported=0)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["model_calls"] == 2
    assert chat["usage_reported_calls"] == 2
    assert chat["usage_coverage"] == pytest.approx(1.0)      # usage 全报了
    assert chat["cache_reported_calls"] == 1
    assert chat["cache_coverage"] == pytest.approx(0.5)      # cache 只报了一半
    # 两个 coverage 不是同一个数——这正是要拆开的理由
    assert chat["cache_coverage"] != chat["usage_coverage"]


def test_token_usage_by_lane_cache_coverage_is_none_without_calls():
    _add_metric("u_cov_none", "chat", prompt=None, completion=None,
                model_calls=0, usage_reported=0, cache_reported=0)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["model_calls"] == 0
    assert chat["cache_coverage"] is None


def test_runtime_user_report_groups_each_users_models_and_lanes():
    _add_metric(
        "u_report_a", "chat", prompt=100, completion=10,
        cache_read=60, cache_write=5, cache_miss=40,
        usage_reported=1, cache_reported=1,
        provider="anthropic", model="claude-a", route="route-a", retries=2,
    )
    _add_metric(
        "u_report_a", "heartbeat", prompt=200, completion=20,
        cache_read=100, cache_write=10, cache_miss=100,
        usage_reported=1, cache_reported=1,
        provider="anthropic", model="claude-a", route="route-a", retries=1,
    )
    _add_metric(
        "u_report_a", "chat", prompt=50, completion=5,
        provider="openai", model="gpt-b", route="route-b",
    )

    report = jobs_store.recent_runtime_user_report(within_hours=24)
    user = next(row for row in report["users"] if row["user_id"] == "u_report_a")

    assert user["known_total_tokens"] == 385
    assert user["model_calls"] == 3
    assert [(m["provider"], m["model"], m["route"]) for m in user["models"]] == [
        ("anthropic", "claude-a", "route-a"),
        ("openai", "gpt-b", "route-b"),
    ]
    model = user["models"][0]
    assert model["lanes"] == ["chat", "heartbeat"]
    assert model["turns"] == 2
    assert model["model_calls"] == 2
    assert model["retries"] == 3
    assert model["prompt_tokens"] == 300
    assert model["completion_tokens"] == 30
    assert model["cache_read_tokens"] == 160
    assert model["cache_write_tokens"] == 15
    assert model["cache_miss_tokens"] == 140
    assert model["cache_hit_ratio"] == pytest.approx(160 / 300)
    assert model["usage_coverage"] == pytest.approx(1.0)
    assert model["cache_coverage"] == pytest.approx(1.0)


def test_runtime_user_report_keeps_unknown_usage_and_identity():
    _add_metric(
        "u_report_unknown", "maintenance",
        prompt=None, completion=None, model_calls=1,
        usage_reported=0, cache_reported=0,
    )
    user = jobs_store.recent_runtime_user_report()["users"][0]
    model = user["models"][0]
    assert (model["provider"], model["model"], model["route"]) == (
        "unknown", "unknown", "unknown",
    )
    assert user["known_total_tokens"] is None
    assert model["total_tokens"] is None
    assert model["cache_hit_ratio"] is None
    assert model["usage_coverage"] == pytest.approx(0.0)
    assert model["cache_coverage"] == pytest.approx(0.0)


def test_runtime_user_report_respects_window_and_orders_known_before_unknown():
    _add_metric("u_report_small", "chat", prompt=10, completion=1)
    _add_metric("u_report_big", "chat", prompt=100, completion=10)
    _add_metric(
        "u_report_unknown", "chat", prompt=None, completion=None,
        usage_reported=0,
    )
    _add_metric(
        "u_report_old", "chat", prompt=1000, completion=100,
        age_hours=48,
    )
    users = jobs_store.recent_runtime_user_report(within_hours=24)["users"]
    assert [u["user_id"] for u in users] == [
        "u_report_big", "u_report_small", "u_report_unknown",
    ]


# ---------------------------------------------------------------------------
# recent_delivery_health：端到端交付（job 判 completed ≠ 产物到达用户）
# ---------------------------------------------------------------------------


def _add_pending_effect(user_id: str, effect_id: str, *, age_sec: int = 0) -> None:
    seed_user(user_id)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_effect_outbox "
            "(effect_id,user_id,effect_type,expected_generation,payload,status,"
            " created_at) VALUES (%s,%s,'reply',1,'{}'::jsonb,'pending',"
            " clock_timestamp()-make_interval(secs => %s))",
            (effect_id, user_id, age_sec),
        )


def _add_failure_outbox_row(
    user_id: str,
    *,
    status_delivered: bool,
    runtime_error_delivered: bool,
    age_sec: int = 0,
) -> None:
    job_id = _add_job(user_id, "chat", "failed")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_terminal_failure_outbox "
            "(job_id,user_id,error_code,created_at,status_delivered_at,"
            " runtime_error_delivered_at) VALUES (%s,%s,'turn_failed:x',"
            " clock_timestamp()-make_interval(secs => %s),"
            " CASE WHEN %s THEN clock_timestamp() END,"
            " CASE WHEN %s THEN clock_timestamp() END)",
            (
                job_id,
                user_id,
                age_sec,
                status_delivered,
                runtime_error_delivered,
            ),
        )


def _add_mutation_attempt(
    user_id: str,
    *,
    outcome: str | None,
    resolved: bool,
    age_hours: int = 0,
) -> None:
    job_id = _add_job(user_id, "chat", "completed")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_mcp_mutation_attempts "
            "(job_id,user_id,input_frontier_seq,call_key,tool_fingerprint,"
            " started_at,resolved_at,outcome) VALUES (%s,%s,0,repeat('a',64),"
            " repeat('b',64),clock_timestamp()-make_interval(hours => %s),"
            " CASE WHEN %s THEN clock_timestamp() END,%s)",
            (job_id, user_id, age_hours, resolved, outcome),
        )


def test_delivery_health_reports_effect_backlog_and_age():
    _add_pending_effect("u_dh_effect", "eff_dh_1", age_sec=7200)

    effect = jobs_store.recent_delivery_health()["effect_outbox"]

    assert effect["pending"] == 1
    assert effect["oldest_pending_age_sec"] >= 7_100


def test_delivery_health_effect_age_is_none_when_drained():
    # 空队列必须是 pending=0 + age=None。age 给个 0 会被页面渲染成"0s"，读起来
    # 像"刚刚堵上了"，而真相是压根没有积压。
    delivery = jobs_store.recent_delivery_health()

    assert delivery["effect_outbox"]["pending"] == 0
    assert delivery["effect_outbox"]["oldest_pending_age_sec"] is None


def test_delivery_health_ignores_applied_effects():
    _add_pending_effect("u_dh_applied", "eff_dh_applied")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_effect_outbox SET status='applied',"
            "applied_at=clock_timestamp() WHERE effect_id='eff_dh_applied'"
        )

    effect = jobs_store.recent_delivery_health()["effect_outbox"]

    assert effect["pending"] == 0
    assert effect["oldest_pending_age_sec"] is None


def test_delivery_health_counts_two_failure_delivery_duties_separately():
    # 两种投递义务各自独立退避、独立标记完成：status 已投递但 runtime_error 还没，
    # 是真实存在的中间态，合并成一个数会把它藏起来。
    _add_failure_outbox_row(
        "u_dh_fo_partial",
        status_delivered=True,
        runtime_error_delivered=False,
        age_sec=1800,
    )

    outbox = jobs_store.recent_delivery_health()["terminal_failure_outbox"]

    assert outbox["status_undelivered"] == 0
    assert outbox["runtime_error_undelivered"] == 1
    assert outbox["oldest_undelivered_age_sec"] >= 1_700


def test_delivery_health_fully_delivered_failure_is_not_backlog():
    _add_failure_outbox_row(
        "u_dh_fo_done",
        status_delivered=True,
        runtime_error_delivered=True,
        age_sec=9999,
    )

    outbox = jobs_store.recent_delivery_health()["terminal_failure_outbox"]

    assert outbox["status_undelivered"] == 0
    assert outbox["runtime_error_undelivered"] == 0
    assert outbox["oldest_undelivered_age_sec"] is None


def test_delivery_health_separates_unknown_from_unresolved_mutations():
    # unknown = 已判定"结果不可知"（远端可能已经改了数据）；unresolved = 进程死在
    # 判定之前的悬空记录。都要人看，但含义不同，合并计数会掩盖后者。
    _add_mutation_attempt("u_dh_mut_unknown", outcome="unknown", resolved=True)
    _add_mutation_attempt("u_dh_mut_dangling", outcome=None, resolved=False)
    _add_mutation_attempt("u_dh_mut_known", outcome="known", resolved=True)

    mutation = jobs_store.recent_delivery_health()["mcp_mutation"]

    assert mutation["unknown"] == 1
    assert mutation["unresolved"] == 1


def test_delivery_health_mutation_respects_window_but_outbox_does_not():
    # 刻意不同的窗口语义：mutation 是窗口内计数（一次性事件，过去某天出过一次不该
    # 永久点亮值班台）；outbox 是当前积压状态（三天前就该 apply 的 effect 还堵着，
    # 那是**现在**的故障，不该因为窗口切到 24h 就消失）。
    _add_mutation_attempt(
        "u_dh_mut_old", outcome="unknown", resolved=True, age_hours=48
    )
    _add_pending_effect("u_dh_effect_old", "eff_dh_old", age_sec=48 * 3600)

    in_24h = jobs_store.recent_delivery_health(within_hours=24)
    in_168h = jobs_store.recent_delivery_health(within_hours=168)

    assert in_24h["mcp_mutation"]["unknown"] == 0
    assert in_168h["mcp_mutation"]["unknown"] == 1
    # 同一条积压在两个窗口下都必须看得见
    assert in_24h["effect_outbox"]["pending"] == 1
    assert in_168h["effect_outbox"]["pending"] == 1


def test_full_window_queries_have_their_supporting_indexes():
    # 去掉采样上界后，这三条查询的扫描量全靠 0071 的索引兜着（24h 档实测：
    # 无索引 Seq Scan 4226 buffer → 有索引 Index Scan 1454 buffer）。索引若被
    # 谁的迁移顺手删掉，症状是值班台在长窗口下越来越慢——那是最难归因的一类
    # 退化，所以在这里钉死。
    with db.get_pool().connection() as conn:
        present = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE indexname = ANY(%s)",
                (
                    [
                        "ix_agent_jobs_terminal_finished_at",
                        "ix_agent_jobs_created_at",
                        "ix_v2_turn_metrics_created_at",
                    ],
                ),
            ).fetchall()
        }

    assert present == {
        "ix_agent_jobs_terminal_finished_at",
        "ix_agent_jobs_created_at",
        "ix_v2_turn_metrics_created_at",
    }
