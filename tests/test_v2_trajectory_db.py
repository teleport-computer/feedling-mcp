from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import threading
import time

import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from conftest import seed_user, set_v2_runtime_owner
import db
from model_api_runtime.v2 import jobs_store


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="trajectory DB tests require PostgreSQL",
)


def _envelope(user_id: str, item_id: str, plaintext: str) -> dict:
    return {
        "v": 1,
        "id": item_id,
        "owner_user_id": user_id,
        "visibility": "shared",
        "body_ct": base64.b64encode(plaintext.encode()).decode(),
        "nonce": "nonce",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
    }


@pytest.fixture(autouse=True)
def clean_tables(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED", "1")
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE", "64")
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_trajectory_reviews,v2_trajectory_events,"
            "v2_trajectory_streams,agent_jobs,v2_runtime_state,users CASCADE"
        )
    yield


def _source_job(user_id: str) -> tuple[int, dict]:
    seed_user(user_id)
    set_v2_runtime_owner(user_id)
    job_id, _ = jobs_store.enqueue_job(user_id, "chat")
    job = jobs_store.claim_next_job("worker-source", lanes={"chat"})
    assert job is not None and int(job["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by="worker-source")
    return job_id, job


def test_encrypted_events_are_ordered_idempotent_and_immutable():
    uid = "u_trajectory_store"
    job_id, _job = _source_job(uid)
    secret = "exact private conversation text"
    first = jobs_store.append_trajectory_event(
        job_id,
        uid,
        event_kind="provider_request",
        idempotency_key="0000_provider_request",
        payload_envelope=_envelope(uid, "event-1", secret),
        payload_bytes=100,
    )
    duplicate = jobs_store.append_trajectory_event(
        job_id,
        uid,
        event_kind="provider_request",
        idempotency_key="0000_provider_request",
        payload_envelope=_envelope(uid, "event-other", "replacement must not land"),
        payload_bytes=120,
    )
    second = jobs_store.append_trajectory_event(
        job_id,
        uid,
        event_kind="provider_response",
        idempotency_key="0001_provider_response",
        payload_envelope=_envelope(uid, "event-2", "response"),
        payload_bytes=80,
    )
    assert (first, duplicate, second) == (0, 0, 1)
    rows = jobs_store.list_trajectory_events(job_id, uid)
    assert [row["event_index"] for row in rows] == [0, 1]
    assert rows[0]["payload_envelope"]["id"] == "event-1"
    capture_state = jobs_store.get_trajectory_capture_state(job_id, uid)
    assert capture_state["capture_status"] == "open"
    assert capture_state["terminal_event_index"] == -1
    leaky_envelope = _envelope(uid, "event-leaky", "ciphertext-placeholder")
    leaky_envelope["plaintext"] = secret
    with pytest.raises(ValueError, match="unsupported fields"):
        jobs_store.append_trajectory_event(
            job_id,
            uid,
            event_kind="turn_exception",
            idempotency_key="0002_turn_exception",
            payload_envelope=leaky_envelope,
            payload_bytes=90,
        )
    with db.get_pool().connection() as conn:
        stored = conn.execute(
            "SELECT payload_envelope::text FROM v2_trajectory_events "
            "WHERE job_id=%s ORDER BY event_index",
            (job_id,),
        ).fetchall()
        assert secret not in "".join(row[0] for row in stored)
        wrong_owner = _envelope("u_wrong_owner", "event-direct", "ciphertext")
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO v2_trajectory_events "
                "(job_id,user_id,event_index,event_kind,idempotency_key,"
                "payload_envelope,payload_bytes,truncated) "
                "VALUES (%s,%s,2,'turn_exception','direct_wrong_owner',%s,50,false)",
                (job_id, uid, Jsonb(wrong_owner)),
            )
        with pytest.raises(Exception):
            conn.execute(
                "UPDATE v2_trajectory_events SET event_kind='turn_terminal' "
                "WHERE job_id=%s AND event_index=0",
                (job_id,),
            )


def test_review_default_off_preserves_trajectory_without_review_runner(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED", raising=False)
    uid = "u_trajectory_review_default_off"
    source_job_id, _job = _source_job(uid)
    jobs_store.append_trajectory_event(
        source_job_id,
        uid,
        event_kind="turn_exception",
        idempotency_key="0000_turn_exception",
        payload_envelope=_envelope(uid, "disabled-event", "captured failure"),
        payload_bytes=90,
    )

    assert jobs_store.mark_failed(
        source_job_id,
        "turn_failed:providererror",
        claimed_by="worker-source",
    )
    assert len(jobs_store.list_trajectory_events(source_job_id, uid)) == 1
    assert jobs_store.get_failure_review(source_job_id, uid) is None
    with db.get_pool().connection() as conn:
        runners = conn.execute(
            "SELECT COUNT(*) FROM agent_jobs WHERE lane='trajectory_review'"
        ).fetchone()[0]
    assert runners == 0


def test_review_global_admission_cap_keeps_overflow_trajectory_only(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE", "1")
    first_uid = "u_trajectory_review_cap_first"
    second_uid = "u_trajectory_review_cap_second"
    first_job_id, _job = _source_job(first_uid)
    second_job_id, _job = _source_job(second_uid)
    sources = ((first_uid, first_job_id), (second_uid, second_job_id))
    for uid, job_id in sources:
        jobs_store.append_trajectory_event(
            job_id,
            uid,
            event_kind="turn_exception",
            idempotency_key="0000_turn_exception",
            payload_envelope=_envelope(uid, f"cap-event-{job_id}", "captured failure"),
            payload_bytes=90,
        )
    barrier = threading.Barrier(2)

    def fail(job_id):
        barrier.wait()
        return jobs_store.mark_failed(
            job_id,
            "turn_failed:providererror",
            claimed_by="worker-source",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(fail, (first_job_id, second_job_id)))

    reviews = {
        first_uid: jobs_store.get_failure_review(first_job_id, first_uid),
        second_uid: jobs_store.get_failure_review(second_job_id, second_uid),
    }
    admitted = [uid for uid, review in reviews.items() if review is not None]
    assert len(admitted) == 1
    assert len(jobs_store.list_trajectory_events(first_job_id, first_uid)) == 1
    assert len(jobs_store.list_trajectory_events(second_job_id, second_uid)) == 1
    with db.get_pool().connection() as conn:
        active_reviews = conn.execute(
            "SELECT COUNT(*) FROM v2_trajectory_reviews "
            "WHERE status IN ('pending','running')"
        ).fetchone()[0]
        runners = conn.execute(
            "SELECT user_id FROM agent_jobs WHERE lane='trajectory_review'"
        ).fetchall()
    assert active_reviews == 1
    assert runners == [(admitted[0],)]


def test_terminal_failure_queues_review_in_same_lifecycle_and_recovers_crash():
    uid = "u_trajectory_review_recovery"
    source_job_id, _job = _source_job(uid)
    assert jobs_store.mark_failed(
        source_job_id,
        "turn_failed:timeouterror",
        claimed_by="worker-source",
    )
    review = jobs_store.get_failure_review(source_job_id, uid)
    assert review is not None and review["status"] == "pending"

    runner = jobs_store.claim_next_job("worker-review", lanes={"trajectory_review"})
    assert runner is not None and runner["lane"] == "trajectory_review"
    assert jobs_store.mark_running(runner["id"], claimed_by="worker-review")
    claimed = jobs_store.claim_failure_review(
        uid,
        runner_job_id=runner["id"],
        claimed_by="worker-review",
    )
    assert claimed is not None and claimed["attempt_count"] == 1

    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET lease_expires_at=now()-interval '1 second' "
            "WHERE id=%s",
            (runner["id"],),
        )
    expired = jobs_store.reap_stuck_job_rows(now=time.time())
    assert runner["id"] in {row["id"] for row in expired}
    recovered = jobs_store.get_failure_review(source_job_id, uid)
    assert recovered is not None and recovered["status"] == "pending"
    with db.get_pool().connection() as conn:
        successor = conn.execute(
            "SELECT id,status FROM agent_jobs WHERE user_id=%s AND lane='trajectory_review' "
            "AND status='pending'",
            (uid,),
        ).fetchone()
    assert successor is not None and successor[1] == "pending"

    next_runner = jobs_store.claim_next_job(
        "worker-review-2",
        lanes={"trajectory_review"},
    )
    assert next_runner is not None and next_runner["id"] == successor[0]
    assert jobs_store.mark_running(next_runner["id"], claimed_by="worker-review-2")
    claimed_again = jobs_store.claim_failure_review(
        uid,
        runner_job_id=next_runner["id"],
        claimed_by="worker-review-2",
    )
    assert claimed_again is not None and claimed_again["attempt_count"] == 2
    review_secret = "private offline root-cause analysis"
    captured_frontier = jobs_store.get_trajectory_capture_state(
        source_job_id,
        uid,
    )
    assert captured_frontier["capture_status"] == "partial"
    settled = jobs_store.finish_failure_review(
        runner_job_id=next_runner["id"],
        source_job_id=source_job_id,
        user_id=uid,
        claimed_by="worker-review-2",
        review_envelope=_envelope(uid, "review-output", review_secret),
        captured_next_event_index=captured_frontier["next_event_index"],
    )
    assert settled == {
        "settled": True,
        "review_status": "completed",
        "frontier_advanced": False,
    }
    completed = jobs_store.get_failure_review(source_job_id, uid)
    assert completed is not None and completed["status"] == "completed"
    with db.get_pool().connection() as conn:
        stored_review = conn.execute(
            "SELECT review_envelope::text FROM v2_trajectory_reviews "
            "WHERE source_job_id=%s",
            (source_job_id,),
        ).fetchone()[0]
    assert review_secret not in stored_review


def test_review_frontier_reopens_when_a_late_event_lands():
    uid = "u_trajectory_late_frontier"
    source_job_id, _job = _source_job(uid)
    assert jobs_store.mark_failed(
        source_job_id,
        "turn_failed:providererror",
        claimed_by="worker-source",
    )
    captured_frontier = jobs_store.get_trajectory_capture_state(
        source_job_id,
        uid,
    )
    assert captured_frontier["capture_status"] == "partial"
    runner = jobs_store.claim_next_job("worker-review", lanes={"trajectory_review"})
    assert runner is not None
    assert jobs_store.mark_running(runner["id"], claimed_by="worker-review")
    assert jobs_store.claim_failure_review(
        uid,
        runner_job_id=runner["id"],
        claimed_by="worker-review",
    )

    jobs_store.append_trajectory_event(
        source_job_id,
        uid,
        event_kind="turn_terminal",
        idempotency_key="0000_turn_terminal",
        payload_envelope=_envelope(uid, "late-terminal", "late terminal"),
        payload_bytes=90,
    )
    settled = jobs_store.finish_failure_review(
        runner_job_id=runner["id"],
        source_job_id=source_job_id,
        user_id=uid,
        claimed_by="worker-review",
        review_envelope=_envelope(uid, "stale-review", "stale analysis"),
        captured_next_event_index=captured_frontier["next_event_index"],
    )
    assert settled == {
        "settled": True,
        "review_status": "pending",
        "frontier_advanced": True,
    }
    review = jobs_store.get_failure_review(source_job_id, uid)
    assert review is not None
    assert review["status"] == "pending"
    assert review["attempt_count"] == 0
    assert review["review_envelope"] is None
    complete_state = jobs_store.get_trajectory_capture_state(source_job_id, uid)
    assert complete_state["capture_status"] == "complete"
    assert complete_state["terminal_event_index"] == 0
