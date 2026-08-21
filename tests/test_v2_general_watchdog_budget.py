"""Bounded watchdog replay for Dream and the other general Runtime V2 lanes."""

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
    reason="general watchdog budget tests require PostgreSQL",
)


@pytest.fixture(autouse=True)
def _clean_runtime_jobs():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_job_recovery_events")
        conn.execute("DELETE FROM agent_jobs")
        conn.execute("DELETE FROM v2_runtime_state")


def _enqueue_running(user_id: str, lane: str, *, claimed_by: str) -> int:
    seed_user(user_id)
    set_v2_runtime_owner(user_id)
    job_id, coalesced = jobs_store.enqueue_job(user_id, lane)
    assert coalesced is False
    claimed = jobs_store.claim_next_job(claimed_by, lanes={lane})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=claimed_by)
    return job_id


def _reclaim_running(job_id: int, lane: str, *, claimed_by: str) -> None:
    claimed = jobs_store.claim_next_job(claimed_by, lanes={lane})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=claimed_by)


def _job_state(job_id: int):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT status,attempt_count,last_error FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()


def test_general_watchdog_requeues_three_times_then_terminalizes_and_counts():
    """The fourth kill stops replay and remains visible in the admin metric."""

    assert jobs_store.GENERAL_LEASE_REQUEUE_MAX_ATTEMPTS == 3
    before = jobs_store.recent_watchdog_recovery_counts(within_hours=24)
    job_id = _enqueue_running(
        "u_t187_dream_watchdog_budget",
        "dream",
        claimed_by="t187-dream",
    )

    outcomes: list[str] = []
    for kill_number in range(1, 5):
        recovered = jobs_store.recover_killed_job(
            job_id=job_id,
            claimed_by="t187-dream",
            reason="slot_watchdog_timeout",
        )
        assert recovered is not None
        outcomes.append(str(recovered["recovery"]))
        status, attempt_count, last_error = _job_state(job_id)
        assert attempt_count == kill_number
        if kill_number <= 3:
            assert status == "pending"
            assert last_error == "slot_watchdog_timeout"
            _reclaim_running(job_id, "dream", claimed_by="t187-dream")
        else:
            assert status == "expired"
            assert last_error == jobs_store.GENERAL_WATCHDOG_REQUEUE_EXHAUSTED

    assert outcomes == ["requeued", "requeued", "requeued", "terminal"]
    with db.get_pool().connection() as conn:
        events = conn.execute(
            "SELECT job_attempt_count,recovery,reason "
            "FROM v2_job_recovery_events WHERE job_id=%s "
            "ORDER BY job_attempt_count",
            (job_id,),
        ).fetchall()
    assert events == [
        (1, "requeued", "slot_watchdog_timeout"),
        (2, "requeued", "slot_watchdog_timeout"),
        (3, "requeued", "slot_watchdog_timeout"),
        (4, "terminal", "slot_watchdog_timeout"),
    ]

    after = jobs_store.recent_watchdog_recovery_counts(within_hours=24)
    assert after.get("dream:requeued", 0) - before.get("dream:requeued", 0) == 3
    assert after.get("dream:terminal", 0) - before.get("dream:terminal", 0) == 1


def test_general_watchdog_budget_config_is_fail_closed(monkeypatch):
    for bad in ("0", "-1", "abc", ""):
        monkeypatch.setenv("FEEDLING_V2_GENERAL_REQUEUE_MAX_ATTEMPTS", bad)
        with pytest.raises(RuntimeError):
            jobs_store._positive_int_env(
                "FEEDLING_V2_GENERAL_REQUEUE_MAX_ATTEMPTS",
                "3",
            )
    monkeypatch.setenv("FEEDLING_V2_GENERAL_REQUEUE_MAX_ATTEMPTS", "5")
    assert (
        jobs_store._positive_int_env(
            "FEEDLING_V2_GENERAL_REQUEUE_MAX_ATTEMPTS",
            "3",
        )
        == 5
    )


def test_trajectory_review_runner_keeps_its_existing_review_budget(monkeypatch):
    """The general job cap must not replace trajectory_review's own state machine."""

    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED", "1")
    job_id = _enqueue_running(
        "u_t187_trajectory_budget_guard",
        "trajectory_review",
        claimed_by="t187-review",
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET attempt_count=%s WHERE id=%s",
            (jobs_store.GENERAL_LEASE_REQUEUE_MAX_ATTEMPTS, job_id),
        )

    recovered = jobs_store.recover_killed_job(
        job_id=job_id,
        claimed_by="t187-review",
        reason="slot_watchdog_timeout",
    )

    assert recovered is not None and recovered["recovery"] == "requeued"
    assert _job_state(job_id) == (
        "pending",
        jobs_store.GENERAL_LEASE_REQUEUE_MAX_ATTEMPTS + 1,
        "slot_watchdog_timeout",
    )
