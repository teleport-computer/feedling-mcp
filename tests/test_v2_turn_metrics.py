"""v2_turn_metrics：record_turn_metric 写入 + recent_mean_tokens_per_turn/pending_job_count 读取。"""
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


def test_pending_job_count_counts_pending_agent_jobs():
    seed_user("u_tm_3")
    _reset_jobs("u_tm_3")
    before = jobs_store.pending_job_count()
    jobs_store.enqueue_job("u_tm_3", "chat", reason="t")
    assert jobs_store.pending_job_count() == before + 1
