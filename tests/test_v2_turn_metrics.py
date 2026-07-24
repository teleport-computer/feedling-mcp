"""v2_turn_metrics：record_turn_metric 写入 + recent_mean_tokens_per_turn/pending_job_count 读取。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 turn-metrics tests require the PostgreSQL test fixture",
)


@pytest.fixture(autouse=True)
def _clean_turn_metrics_table():
    """Truncate before each test so lane-scoped averages only see the rows a
    given test sets up itself (mirrors _clean_agent_jobs_table in
    test_v2_jobs_store.py)."""
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_turn_metrics")
    yield


def _reset_jobs(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))


def test_record_turn_metric_feeds_recent_mean_tokens_per_turn():
    seed_user("u_tm_1")
    jobs_store.record_turn_metric(
        job_id=None, user_id="u_tm_1", lane="chat",
        prompt_tokens=10, completion_tokens=20, latency_ms=500,
    )
    jobs_store.record_turn_metric(
        job_id=None, user_id="u_tm_1", lane="chat",
        prompt_tokens=20, completion_tokens=30, latency_ms=700,
    )
    mean = jobs_store.recent_mean_tokens_per_turn(lane="chat")
    assert mean == pytest.approx(40.0)  # (30+50)/2


def test_null_token_row_is_ignored_by_average():
    seed_user("u_tm_2")
    jobs_store.record_turn_metric(
        job_id=None, user_id="u_tm_2", lane="chat",
        prompt_tokens=10, completion_tokens=20, latency_ms=500,
    )
    jobs_store.record_turn_metric(
        job_id=None, user_id="u_tm_2", lane="chat",
        prompt_tokens=20, completion_tokens=30, latency_ms=700,
    )
    before = jobs_store.recent_mean_tokens_per_turn(lane="chat")
    # NULL-token row (e.g. provider gave no usage data) must not skew the average.
    jobs_store.record_turn_metric(
        job_id=None, user_id="u_tm_2", lane="chat",
        prompt_tokens=None, completion_tokens=None, latency_ms=123,
    )
    after = jobs_store.recent_mean_tokens_per_turn(lane="chat")
    assert after == before == pytest.approx(40.0)


def test_recent_mean_tokens_per_turn_none_without_history():
    assert jobs_store.recent_mean_tokens_per_turn(lane="no-such-lane") is None


def test_recent_token_usage_summary_keeps_totals_and_coverage_honest():
    seed_user("u_tm_rollup_a")
    seed_user("u_tm_rollup_b")
    jobs_store.record_whole_turn_metric(
        98001, "u_tm_rollup_a", "chat",
        prompt_tokens=1_000,
        completion_tokens=120,
        cache_read_tokens=600,
        cache_write_tokens=100,
        cache_miss_tokens=400,
        usage_reported_calls=2,
        cache_reported_calls=2,
        provider="anthropic",
        model="claude-test",
        cache_route_fingerprint="feedling-v2-route-a",
        latency_ms=500,
        model_calls=2,
        retries=0,
        failed=False,
        status="ok",
    )
    jobs_store.record_whole_turn_metric(
        98002, "u_tm_rollup_b", "chat",
        prompt_tokens=None,
        completion_tokens=None,
        latency_ms=250,
        model_calls=1,
        retries=0,
        failed=True,
        status="provider_error",
    )

    usage = jobs_store.recent_token_usage_summary(lane="chat", within_days=30)

    assert usage == {
        "window_days": 30,
        "sampled_turns": 2,
        "users": 2,
        "model_calls": 3,
        "usage_reported_calls": 2,
        "cache_reported_calls": 2,
        "usage_telemetry_coverage": pytest.approx(2 / 3),
        "cache_telemetry_coverage": pytest.approx(2 / 3),
        "prompt_tokens": 1_000,
        "completion_tokens": 120,
        "total_tokens": 1_120,
        "cache_read_tokens": 600,
        "cache_write_tokens": 100,
        "cache_miss_tokens": 400,
    }


def test_recent_token_usage_summary_preserves_unknown_token_totals():
    usage = jobs_store.recent_token_usage_summary(lane="no-such-lane")

    assert usage["sampled_turns"] == 0
    assert usage["users"] == 0
    assert usage["total_tokens"] is None
    assert usage["usage_telemetry_coverage"] is None


def test_recent_prompt_cache_stats_preserve_unknowns_and_report_coverage():
    seed_user("u_tm_cache")
    jobs_store.record_whole_turn_metric(
        99001, "u_tm_cache", "chat",
        prompt_tokens=1000,
        completion_tokens=100,
        cache_read_tokens=600,
        cache_write_tokens=100,
        cache_miss_tokens=400,
        usage_reported_calls=2,
        cache_reported_calls=2,
        provider="anthropic",
        model="claude-test",
        cache_route_fingerprint="feedling-v2-route-a",
        latency_ms=500,
        model_calls=2,
        retries=0,
        failed=False,
        status="ok",
    )
    jobs_store.record_whole_turn_metric(
        99002, "u_tm_cache", "chat",
        prompt_tokens=None,
        completion_tokens=None,
        latency_ms=250,
        model_calls=1,
        retries=0,
        failed=True,
        status="provider_error",
    )

    stats = jobs_store.recent_prompt_cache_stats(lane="chat")

    assert stats == {
        "sampled_turns": 2,
        "model_calls": 3,
        "usage_reported_calls": 2,
        "cache_reported_calls": 2,
        "usage_telemetry_coverage": pytest.approx(2 / 3),
        "cache_telemetry_coverage": pytest.approx(2 / 3),
        "route_identity_coverage": pytest.approx(1 / 2),
        "route_fingerprint_count": 1,
        "route_fingerprint": "feedling-v2-route-a",
        "prompt_tokens": 1000,
        "cache_read_tokens": 600,
        "cache_write_tokens": 100,
        "cache_miss_tokens": 400,
        "effective_input_tokens": 1000,
        "hit_ratio": pytest.approx(0.6),
    }


def test_recent_prompt_cache_stats_does_not_invent_zero_tokens():
    seed_user("u_tm_cache_unknown")
    jobs_store.record_whole_turn_metric(
        99003, "u_tm_cache_unknown", "unknown-cache-lane",
        prompt_tokens=None,
        completion_tokens=None,
        latency_ms=250,
        model_calls=1,
        retries=0,
        failed=True,
        status="provider_error",
    )

    stats = jobs_store.recent_prompt_cache_stats(lane="unknown-cache-lane")

    assert stats["model_calls"] == 1
    assert stats["usage_reported_calls"] == 0
    assert stats["cache_reported_calls"] == 0
    assert stats["route_identity_coverage"] == 0.0
    assert stats["route_fingerprint_count"] == 0
    assert stats["route_fingerprint"] is None
    assert stats["prompt_tokens"] is None
    assert stats["cache_read_tokens"] is None
    assert stats["cache_write_tokens"] is None
    assert stats["cache_miss_tokens"] is None
    assert stats["effective_input_tokens"] is None
    assert stats["hit_ratio"] is None


def test_recent_prompt_cache_stats_isolates_provider_model_and_window():
    seed_user("u_tm_cache_routes")
    seed_user("u_tm_cache_other_user")
    common = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cache_write_tokens": 0,
        "usage_reported_calls": 1,
        "cache_reported_calls": 1,
        "latency_ms": 100,
        "model_calls": 1,
        "retries": 0,
        "failed": False,
        "status": "ok",
    }
    jobs_store.record_whole_turn_metric(
        99004, "u_tm_cache_routes", "cache-route-test",
        cache_read_tokens=0, cache_miss_tokens=100,
        provider="openai", model="gpt-cache",
        cache_route_fingerprint="feedling-v2-route-a", **common)
    jobs_store.record_whole_turn_metric(
        99005, "u_tm_cache_routes", "cache-route-test",
        cache_read_tokens=0, cache_miss_tokens=100,
        provider="openai", model="gpt-cache",
        cache_route_fingerprint="feedling-v2-route-b", **common)
    jobs_store.record_whole_turn_metric(
        99006, "u_tm_cache_other_user", "cache-route-test",
        cache_read_tokens=0, cache_miss_tokens=100,
        provider="openai", model="gpt-cache",
        cache_route_fingerprint="feedling-v2-route-a", **common)
    jobs_store.record_whole_turn_metric(
        99007, "u_tm_cache_routes", "cache-route-test",
        cache_read_tokens=80, cache_miss_tokens=20,
        provider="openai", model="gpt-cache",
        cache_route_fingerprint="feedling-v2-route-a", **common)

    aggregate = jobs_store.recent_prompt_cache_stats(
        lane="cache-route-test", provider="openai", model="gpt-cache")
    canary = jobs_store.recent_prompt_cache_stats(
        lane="cache-route-test", provider="openai", model="gpt-cache",
        cache_route_fingerprint="feedling-v2-route-a",
        user_id="u_tm_cache_routes",
        since_ts=time.time() - 60,
        until_ts=time.time() + 60,
        include_turns=True)
    other_user = jobs_store.recent_prompt_cache_stats(
        lane="cache-route-test", provider="openai", model="gpt-cache",
        cache_route_fingerprint="feedling-v2-route-a",
        user_id="u_tm_cache_other_user")
    future = jobs_store.recent_prompt_cache_stats(
        lane="cache-route-test", provider="openai", model="gpt-cache",
        since_ts=time.time() + 60)

    assert aggregate["sampled_turns"] == 4
    assert aggregate["route_fingerprint_count"] == 2
    assert aggregate["route_fingerprint"] is None
    assert canary["sampled_turns"] == 2
    assert canary["hit_ratio"] == pytest.approx(0.4)
    assert canary["route_identity_coverage"] == 1.0
    assert canary["route_fingerprint_count"] == 1
    assert canary["route_fingerprint"] == "feedling-v2-route-a"
    assert canary["filter"] == {
        "provider": "openai",
        "model": "gpt-cache",
        "cache_route_fingerprint": "feedling-v2-route-a",
        "user_id": "u_tm_cache_routes",
        "since_ts": canary["filter"]["since_ts"],
        "until_ts": canary["filter"]["until_ts"],
        "include_turns": True,
    }
    assert [turn["job_id"] for turn in canary["turns"]] == [99004, 99007]
    assert [turn["model_calls"] for turn in canary["turns"]] == [1, 1]
    assert canary["turns"][0]["cache_read_tokens"] == 0
    assert canary["turns"][1]["cache_read_tokens"] == 80
    assert other_user["sampled_turns"] == 1
    assert other_user["hit_ratio"] == 0.0
    assert future["sampled_turns"] == 0
    assert future["filter"]["since_ts"] is not None


def test_pending_job_count_counts_pending_agent_jobs():
    seed_user("u_tm_3")
    _reset_jobs("u_tm_3")
    before = jobs_store.pending_job_count()
    jobs_store.enqueue_job("u_tm_3", "chat", reason="t")
    assert jobs_store.pending_job_count() == before + 1
