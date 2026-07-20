from __future__ import annotations

import base64
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from conftest import seed_user, set_v2_runtime_owner
from model_api_runtime.v2 import jobs_store


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="trajectory retention tests require PostgreSQL",
)


@pytest.fixture(autouse=True)
def _clean_queue_state():
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE agent_jobs,users CASCADE")
    yield


def _envelope(user_id: str, item_id: str) -> dict:
    return {
        "v": 1,
        "id": item_id,
        "owner_user_id": user_id,
        "visibility": "shared",
        "body_ct": base64.b64encode(b"private trajectory").decode(),
        "nonce": "nonce",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
    }


def _terminal_job(user_id: str, ordinal: int, *, age_days: int) -> int:
    worker_id = f"retention-worker-{ordinal}"
    job_id, coalesced = jobs_store.enqueue_job(
        user_id,
        "maintenance",
        expected_generation=db.get_runtime_generation(user_id),
    )
    assert coalesced is False
    claimed = jobs_store.claim_next_job(worker_id, lanes={"maintenance"})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=worker_id)
    jobs_store.append_trajectory_event(
        job_id,
        user_id,
        event_kind="provider_response",
        idempotency_key=f"attempt{ordinal}.response",
        payload_envelope=_envelope(user_id, f"trajectory-{ordinal}"),
        payload_bytes=100,
    )
    assert jobs_store.mark_completed(job_id, claimed_by=worker_id)
    jobs_store.record_turn_metric(
        job_id=job_id,
        user_id=user_id,
        lane="maintenance",
        prompt_tokens=10,
        completion_tokens=2,
        latency_ms=20,
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET finished_at="
            "clock_timestamp() - make_interval(days => %s) WHERE id=%s",
            (age_days, job_id),
        )
    return job_id


def test_retention_purges_only_expired_terminal_content_and_keeps_metrics():
    uid = "u_trajectory_retention_boundary"
    seed_user(uid)
    set_v2_runtime_owner(uid, generation=5)
    old_job = _terminal_job(uid, 1, age_days=30)
    recent_job = _terminal_job(uid, 2, age_days=1)
    active_review_job = _terminal_job(uid, 3, age_days=20)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_trajectory_reviews "
            "(source_job_id,user_id,status) VALUES (%s,%s,'pending')",
            (active_review_job, uid),
        )

    assert jobs_store.purge_expired_trajectories(
        retention_days=7,
        limit=100,
    ) == 2

    with db.get_pool().connection() as conn:
        job_rows = conn.execute(
            "SELECT id,trajectory_purged_at IS NOT NULL FROM agent_jobs "
            "WHERE id=ANY(%s::bigint[]) ORDER BY id",
            ([old_job, recent_job, active_review_job],),
        ).fetchall()
        event_jobs = {
            int(row[0])
            for row in conn.execute(
                "SELECT job_id FROM v2_trajectory_events WHERE user_id=%s",
                (uid,),
            ).fetchall()
        }
        review = conn.execute(
            "SELECT status FROM v2_trajectory_reviews WHERE source_job_id=%s",
            (active_review_job,),
        ).fetchone()
        metric_jobs = {
            int(row[0])
            for row in conn.execute(
                "SELECT job_id FROM v2_turn_metrics WHERE user_id=%s",
                (uid,),
            ).fetchall()
        }

    purged = {int(row[0]): bool(row[1]) for row in job_rows}
    assert purged == {
        old_job: True,
        recent_job: False,
        active_review_job: True,
    }
    assert event_jobs == {recent_job}
    assert review is None
    assert metric_jobs == {old_job, recent_job, active_review_job}

    # The tombstone is part of the same transaction as deletion: a delayed
    # recorder cannot recreate an expired stream after GC commits.
    with pytest.raises(ValueError, match="generation is stale"):
        jobs_store.append_trajectory_event(
            old_job,
            uid,
            event_kind="provider_response",
            idempotency_key="attempt1.late",
            payload_envelope=_envelope(uid, "late-trajectory"),
            payload_bytes=100,
        )

    assert jobs_store.get_trajectory_capture_state(active_review_job, uid)[
        "capture_status"
    ] == "missing"


def test_retention_batch_is_bounded_and_resumable():
    uid = "u_trajectory_retention_batch"
    seed_user(uid)
    set_v2_runtime_owner(uid)
    first = _terminal_job(uid, 1, age_days=30)
    second = _terminal_job(uid, 2, age_days=20)

    assert jobs_store.purge_expired_trajectories(
        retention_days=7,
        limit=1,
    ) == 1
    with db.get_pool().connection() as conn:
        first_pass = {
            int(row[0])
            for row in conn.execute(
                "SELECT id FROM agent_jobs WHERE trajectory_purged_at IS NOT NULL"
            ).fetchall()
        }
    assert first_pass == {first}

    assert jobs_store.purge_expired_trajectories(
        retention_days=7,
        limit=1,
    ) == 1
    with db.get_pool().connection() as conn:
        all_purged = {
            int(row[0])
            for row in conn.execute(
                "SELECT id FROM agent_jobs WHERE trajectory_purged_at IS NOT NULL"
            ).fetchall()
        }
    assert all_purged == {first, second}
    assert jobs_store.purge_expired_trajectories(
        retention_days=7,
        limit=1,
    ) == 0


def test_retention_cancels_a_running_review_runner_before_erasure():
    uid = "u_trajectory_retention_running_review"
    seed_user(uid)
    set_v2_runtime_owner(uid)
    source_job = _terminal_job(uid, 1, age_days=30)
    runner_job, coalesced = jobs_store.enqueue_job(uid, "trajectory_review")
    assert coalesced is False
    claimed = jobs_store.claim_next_job(
        "retention-review-worker",
        lanes={"trajectory_review"},
    )
    assert claimed is not None and int(claimed["id"]) == runner_job
    assert jobs_store.mark_running(
        runner_job,
        claimed_by="retention-review-worker",
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_trajectory_reviews "
            "(source_job_id,user_id,status,claimed_by_job_id,attempt_count) "
            "VALUES (%s,%s,'running',%s,1)",
            (source_job, uid, runner_job),
        )

    assert jobs_store.purge_expired_trajectories(
        retention_days=7,
        limit=100,
    ) == 1

    with db.get_pool().connection() as conn:
        runner = conn.execute(
            "SELECT status,last_error FROM agent_jobs WHERE id=%s",
            (runner_job,),
        ).fetchone()
        review = conn.execute(
            "SELECT 1 FROM v2_trajectory_reviews WHERE source_job_id=%s",
            (source_job,),
        ).fetchone()
    assert runner == ("superseded", "trajectory_retention_expired")
    assert review is None
