"""Tests for scripts/loadtest/collect.py — pure math helpers + DB samplers that
feed the D4 load-test driver/report. Pure math (mean/p95) needs no DB; the
samplers read agent_jobs / v2_turn_metrics via db.get_pool(), same scaffolding
as tests/test_v2_jobs_store.py (seed_user + autouse table-clean fixture)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent))

import db
from model_api_runtime.v2 import jobs_store

from conftest import seed_user

from scripts.loadtest import collect

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed load-test collect tests require the PostgreSQL test fixture",
)


@pytest.fixture(autouse=True)
def _clean_tables():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
        conn.execute("DELETE FROM v2_turn_metrics")
    yield


# --- pure math (no DB) -------------------------------------------------


def test_mean_of_two_values():
    assert collect.mean([2, 4]) == 3


def test_mean_of_empty_is_none():
    assert collect.mean([]) is None


def test_p95_of_one_to_hundred_is_in_sane_range():
    samples = list(range(1, 101))
    result = collect.p95(samples)
    assert result is not None
    assert 94 <= result <= 96


def test_p95_of_empty_is_none():
    assert collect.p95([]) is None


def test_p95_of_single_value_is_that_value():
    assert collect.p95([5]) == 5


# --- DB samplers ---------------------------------------------------------


def test_queue_wait_samples_reads_claimed_minus_created():
    seed_user("u_lt_1")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at, claimed_at) "
            "VALUES (%s, 'chat', 'claimed', now() - make_interval(secs=>10), "
            "now() - make_interval(secs=>7))",
            ("u_lt_1",),
        )
    samples = collect.queue_wait_samples(within_hours=24)
    assert len(samples) == 1
    assert 2.5 <= samples[0] <= 3.5  # ~3.0s


def test_queue_wait_samples_excludes_unclaimed_jobs():
    seed_user("u_lt_2")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at) "
            "VALUES (%s, 'chat', 'pending', now())",
            ("u_lt_2",),
        )
    samples = collect.queue_wait_samples(within_hours=24)
    assert samples == []


def test_queue_wait_samples_excludes_outside_window():
    seed_user("u_lt_3")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at, claimed_at) "
            "VALUES (%s, 'chat', 'claimed', now() - interval '48 hours', "
            "now() - interval '48 hours' + make_interval(secs=>3))",
            ("u_lt_3",),
        )
    samples = collect.queue_wait_samples(within_hours=24)
    assert samples == []


def test_turn_latency_ms_samples_reads_latency_ms():
    seed_user("u_lt_4")
    jobs_store.record_turn_metric(
        job_id=None, user_id="u_lt_4", lane="chat",
        prompt_tokens=10, completion_tokens=5, latency_ms=1234,
    )
    samples = collect.turn_latency_ms_samples(within_hours=24)
    assert samples == [1234.0]


def test_turn_latency_ms_samples_excludes_null_latency():
    seed_user("u_lt_5")
    jobs_store.record_turn_metric(
        job_id=None, user_id="u_lt_5", lane="chat",
        prompt_tokens=10, completion_tokens=5, latency_ms=None,
    )
    samples = collect.turn_latency_ms_samples(within_hours=24)
    assert samples == []


def test_tokens_per_turn_samples_sums_prompt_and_completion():
    seed_user("u_lt_6")
    jobs_store.record_turn_metric(
        job_id=None, user_id="u_lt_6", lane="chat",
        prompt_tokens=100, completion_tokens=20, latency_ms=500,
    )
    samples = collect.tokens_per_turn_samples(within_hours=24)
    assert samples == [120.0]


def test_tokens_per_turn_samples_excludes_rows_with_null_tokens():
    seed_user("u_lt_7")
    jobs_store.record_turn_metric(
        job_id=None, user_id="u_lt_7", lane="chat",
        prompt_tokens=None, completion_tokens=20, latency_ms=500,
    )
    samples = collect.tokens_per_turn_samples(within_hours=24)
    assert samples == []


def test_stuck_job_count_counts_expired_status():
    seed_user("u_lt_8")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at) "
            "VALUES (%s, 'chat', 'expired', now())",
            ("u_lt_8",),
        )
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at) "
            "VALUES (%s, 'chat', 'completed', now())",
            ("u_lt_8",),
        )
    assert collect.stuck_job_count(within_hours=24) == 1


def test_stuck_job_count_excludes_outside_window():
    seed_user("u_lt_9")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at) "
            "VALUES (%s, 'chat', 'expired', now() - interval '48 hours')",
            ("u_lt_9",),
        )
    assert collect.stuck_job_count(within_hours=24) == 0


def test_failed_turn_count_counts_only_failed_whole_turn_rows():
    seed_user("u_lt_10")
    jobs_store.record_whole_turn_metric(
        job_id=90001, user_id="u_lt_10", lane="chat",
        prompt_tokens=10, completion_tokens=5, latency_ms=100,
        model_calls=1, retries=0, failed=True, status="error",
    )
    jobs_store.record_whole_turn_metric(
        job_id=90002, user_id="u_lt_10", lane="chat",
        prompt_tokens=10, completion_tokens=5, latency_ms=100,
        model_calls=1, retries=0, failed=False, status="ok",
    )
    assert collect.failed_turn_count(within_hours=24) == 1


def test_failed_turn_count_excludes_outside_window():
    seed_user("u_lt_11")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics "
            "(job_id, user_id, lane, prompt_tokens, completion_tokens, latency_ms, "
            "model_calls, retries, failed, status, created_at) "
            "VALUES (90003, %s, 'chat', 10, 5, 100, 1, 0, true, 'error', "
            "now() - interval '48 hours')",
            ("u_lt_11",),
        )
    assert collect.failed_turn_count(within_hours=24) == 0


# --- peak_rss_kb ----------------------------------------------------------


def test_peak_rss_kb_of_current_process_is_none_or_positive_int():
    r = collect.peak_rss_kb(os.getpid())
    assert r is None or (isinstance(r, int) and r > 0)


def test_peak_rss_kb_of_bogus_pid_returns_none():
    assert collect.peak_rss_kb(999999999) is None
