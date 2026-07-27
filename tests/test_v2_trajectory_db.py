from __future__ import annotations

import asyncio
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
from model_api_runtime.v2 import trajectory


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


def test_encrypted_event_batch_is_atomic_ordered_and_idempotent():
    uid = "u_trajectory_batch"
    job_id, _job = _source_job(uid)
    events = [
        {
            "event_kind": "provider_request",
            "idempotency_key": "batch_chunk_0",
            "payload_envelope": _envelope(uid, "batch-0", "chunk zero"),
            "payload_bytes": 80,
            "truncated": False,
        },
        {
            "event_kind": "provider_request",
            "idempotency_key": "batch_chunk_1",
            "payload_envelope": _envelope(uid, "batch-1", "chunk one"),
            "payload_bytes": 81,
            "truncated": False,
        },
    ]
    first = jobs_store.append_trajectory_events_batch(job_id, uid, events=events)
    duplicate = jobs_store.append_trajectory_events_batch(
        job_id,
        uid,
        events=[
            {
                **events[0],
                "payload_envelope": _envelope(uid, "replacement-0", "must not land"),
            },
            {
                **events[1],
                "payload_envelope": _envelope(uid, "replacement-1", "must not land"),
            },
        ],
    )
    assert first == duplicate == [0, 1]
    rows = jobs_store.list_trajectory_events(job_id, uid)
    assert [row["event_index"] for row in rows] == [0, 1]
    assert [row["payload_envelope"]["id"] for row in rows] == ["batch-0", "batch-1"]
    state = jobs_store.get_trajectory_capture_state(job_id, uid)
    assert state["event_count"] == 2
    assert state["next_event_index"] == 2


def test_capture_gap_keeps_terminal_trajectory_explicitly_partial():
    uid = "u_trajectory_gap"
    job_id, _job = _source_job(uid)
    for index, event_kind in enumerate(("capture_gap", "turn_terminal")):
        jobs_store.append_trajectory_event(
            job_id,
            uid,
            event_kind=event_kind,
            idempotency_key=f"gap_{index}_{event_kind}",
            payload_envelope=_envelope(uid, f"gap-{index}", event_kind),
            payload_bytes=80,
        )
    state = jobs_store.get_trajectory_capture_state(job_id, uid)
    assert state["has_capture_gap"] is True
    assert state["terminal_event_index"] == 1
    assert state["capture_status"] == "partial"


def test_recent_chat_operational_health_counts_missing_capture_from_jobs():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_turn_metrics")

    terminal_statuses = {"completed", "failed", "expired", "superseded"}

    def add_job(
        user_id: str,
        status: str,
        *,
        age_hours: int = 0,
        last_error: str | None = None,
    ) -> int:
        seed_user(user_id)
        set_v2_runtime_owner(user_id)
        job_id, _ = jobs_store.enqueue_job(user_id, "chat")
        with db.get_pool().connection() as conn:
                conn.execute(
                    "UPDATE agent_jobs SET status=%s,"
                    "created_at=clock_timestamp()-make_interval(hours => %s),"
                    "finished_at=CASE WHEN %s THEN "
                    "clock_timestamp()-make_interval(hours => %s) ELSE NULL END "
                    ",last_error=%s "
                    "WHERE id=%s",
                    (
                        status,
                        age_hours,
                        status in terminal_statuses,
                        age_hours,
                        last_error,
                        job_id,
                    ),
                )
        return job_id

    complete_job = add_job("u_health_complete", "completed")
    gap_job = add_job("u_health_gap", "failed")
    partial_job = add_job(
        "u_health_partial", "expired", last_error="queue_timeout"
    )
    _missing_job = add_job("u_health_missing", "superseded")
    open_job = add_job("u_health_open", "pending")
    _active_missing_job = add_job("u_health_active_missing", "running")
    add_job("u_health_old_pending", "pending", age_hours=48)
    add_job("u_health_old_terminal", "completed", age_hours=48)
    seed_user("u_health_background")
    set_v2_runtime_owner("u_health_background")
    background_job, _ = jobs_store.enqueue_job("u_health_background", "heartbeat")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET created_at=clock_timestamp()-interval '72 hours' "
            "WHERE id=%s",
            (background_job,),
        )

    for job_id, event_kinds in (
        (complete_job, ("turn_terminal",)),
        (gap_job, ("capture_gap", "turn_terminal")),
        (partial_job, ("provider_request",)),
        (open_job, ("provider_request",)),
    ):
        user_id = {
            complete_job: "u_health_complete",
            gap_job: "u_health_gap",
            partial_job: "u_health_partial",
            open_job: "u_health_open",
        }[job_id]
        for event_index, event_kind in enumerate(event_kinds):
            jobs_store.append_trajectory_event(
                job_id,
                user_id,
                event_kind=event_kind,
                idempotency_key=f"health_{event_index}_{event_kind}",
                payload_envelope=_envelope(
                    user_id,
                    f"health-{job_id}-{event_index}",
                    event_kind,
                ),
                payload_bytes=80,
            )

    recent_jobs = (
        (complete_job, "u_health_complete"),
        (gap_job, "u_health_gap"),
        (partial_job, "u_health_partial"),
        (_missing_job, "u_health_missing"),
        (open_job, "u_health_open"),
        (_active_missing_job, "u_health_active_missing"),
    )
    with db.get_pool().connection() as conn:
        for latency_ms, (job_id, user_id) in enumerate(recent_jobs, start=1):
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,latency_ms) VALUES (%s,%s,'chat',%s)",
                (job_id, user_id, latency_ms * 100),
            )
        seed_user("u_health_old_metric")
        seed_user("u_health_background_metric")
        conn.execute(
            "INSERT INTO v2_turn_metrics (user_id,lane,latency_ms,created_at) "
            "VALUES ('u_health_old_metric','chat',999999,"
            "clock_timestamp()-interval '48 hours'),"
            "('u_health_background_metric','heartbeat',999999,clock_timestamp())"
        )

    health = jobs_store.recent_chat_operational_health()

    assert health["window_hours"] == 24
    assert health["sample_limit"] == 1000
    assert health["jobs"] == {
        "sampled_terminal_jobs": 4,
        "completed": 1,
        "failed": 1,
        "expired": 1,
        "queue_expired": 1,
        "lease_expired": 0,
        "superseded": 1,
        "failure_rate": pytest.approx(1 / 3),
        "expiry_rate": pytest.approx(1 / 3),
        "error_or_expiry_rate": pytest.approx(2 / 3),
        "pending": 2,
        "oldest_pending_age_sec": health["jobs"]["oldest_pending_age_sec"],
        # A failure count with no reason is a smoke alarm with no address:
        # the failed job carries no last_error and must still be visible as
        # "(empty)" rather than vanishing from the histogram, and the expired
        # one must report the reason the column already stored.
        "failure_reasons": [
            {"reason": "(empty)", "count": 1},
            {"reason": "queue_timeout", "count": 1},
        ],
    }
    assert 172790 <= health["jobs"]["oldest_pending_age_sec"] <= 172860
    assert health["latency"] == {
        "sampled_turns": 6,
        "p95_ms": pytest.approx(575.0),
    }
    assert health["trajectory"] == {
        "sampled_jobs": 6,
        "complete": 1,
        "partial": 2,
        "missing": 1,
        "open": 2,
        "capture_gap": 1,
        "complete_rate": pytest.approx(1 / 4),
    }


def test_failure_reasons_reconcile_with_the_counts_they_sit_next_to():
    """The histogram and the failure count must describe the SAME sample.

    Caught in review of 2b6fb852: the reasons were a second "most recent N
    failures" scan while the counts came from "most recent N terminal jobs".
    Once a window holds more than N terminal jobs those are different sets,
    so the panel would show `failed: 0` beside a list of failure reasons —
    two implementations of one number, which is the trap TESTING §2-N exists
    to catch.
    """
    def add_job(user_id: str, status: str, *, last_error: str, age_hours: int) -> None:
        seed_user(user_id)
        set_v2_runtime_owner(user_id)
        job_id, _ = jobs_store.enqueue_job(user_id, "chat")
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE agent_jobs SET status=%s,last_error=%s,"
                "finished_at=clock_timestamp()-make_interval(hours => %s) "
                "WHERE id=%s",
                (status, last_error, age_hours, job_id),
            )

    # Older failures, newer successes: a bounded sample must exclude both
    # the failures AND their reasons, together.
    add_job("u_recon_old_fail_a", "failed", last_error="evicted_reason", age_hours=5)
    add_job("u_recon_old_fail_b", "expired", last_error="evicted_reason", age_hours=4)
    for index in range(3):
        add_job(f"u_recon_ok_{index}", "completed", last_error="", age_hours=index)

    health = jobs_store.recent_chat_operational_health(limit=3)
    jobs = health["jobs"]

    assert jobs["failed"] + jobs["expired"] == sum(
        entry["count"] for entry in jobs["failure_reasons"]
    ), "reasons must sum to the failures reported beside them"
    assert jobs["failure_reasons"] == [], "evicted failures take their reasons with them"

    # Widen the sample and both sides must grow together.
    widened = jobs_store.recent_chat_operational_health(limit=1000)["jobs"]
    assert widened["failed"] + widened["expired"] == sum(
        entry["count"] for entry in widened["failure_reasons"]
    )
    assert {"reason": "evicted_reason", "count": 2} in widened["failure_reasons"]


def test_recent_chat_failures_for_user_answers_why_this_user_went_silent():
    """The fleet histogram says "V2 is unhealthy"; support needs "this person".

    Regression for 2026-07-27: two prod users sat silent for hours after a
    V1→V2 cutover and the provider-attempt ledger alone cannot distinguish a
    pre-provider runtime failure from a dead relay. The job's terminal code is
    the authoritative explanation for the failed turn.
    """
    def add_job(user_id: str, status: str, *, last_error: str, age_hours: int = 0) -> int:
        seed_user(user_id)
        set_v2_runtime_owner(user_id)
        job_id, _ = jobs_store.enqueue_job(user_id, "chat")
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE agent_jobs SET status=%s,last_error=%s,"
                "created_at=clock_timestamp()-make_interval(hours => %s),"
                "finished_at=clock_timestamp()-make_interval(hours => %s) "
                "WHERE id=%s",
                (status, last_error, age_hours, age_hours, job_id),
            )
        return job_id

    add_job("u_fail_subject", "failed", last_error="prompt_frontier_exhausted")
    newest = add_job("u_fail_subject", "expired", last_error="lease_timeout")
    add_job("u_fail_subject", "failed", last_error="ancient", age_hours=200)
    add_job("u_fail_other", "failed", last_error="someone_elses_problem")

    out = jobs_store.recent_chat_failures_for_user("u_fail_subject")

    assert [f["reason"] for f in out["failures"]] == [
        "lease_timeout",
        "prompt_frontier_exhausted",
    ], "newest first, and another user's failure must never leak in"
    assert out["failures"][0]["job_id"] == newest
    assert out["failures"][0]["status"] == "expired"
    assert out["failures"][0]["finished_at"], "admin needs a timestamp to correlate"
    assert out["has_more"] is False

    # An over-long relay error body must not be able to dominate the payload.
    add_job("u_fail_long", "failed", last_error="x" * 5000)
    long_reason = jobs_store.recent_chat_failures_for_user("u_fail_long")["failures"][0]
    assert len(long_reason["reason"]) == jobs_store._FAILURE_REASON_MAX_CHARS + 1
    assert long_reason["reason"].endswith("…")

    # A user with no failures is an explicit empty answer, not an error.
    seed_user("u_fail_none")
    assert jobs_store.recent_chat_failures_for_user("u_fail_none")["failures"] == []
    assert jobs_store.recent_chat_failures_for_user("")["failures"] == []


def test_recent_chat_operational_health_is_explicit_without_history():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_turn_metrics")

    health = jobs_store.recent_chat_operational_health()

    assert health["jobs"] == {
        "sampled_terminal_jobs": 0,
        "completed": 0,
        "failed": 0,
        "expired": 0,
        "queue_expired": 0,
        "lease_expired": 0,
        "superseded": 0,
        "failure_rate": None,
        "expiry_rate": None,
        "error_or_expiry_rate": None,
        "pending": 0,
        "oldest_pending_age_sec": None,
        "failure_reasons": [],
    }
    assert health["latency"] == {"sampled_turns": 0, "p95_ms": None}
    assert health["trajectory"] == {
        "sampled_jobs": 0,
        "complete": 0,
        "partial": 0,
        "missing": 0,
        "open": 0,
        "capture_gap": 0,
        "complete_rate": None,
    }


def test_superseded_rows_cannot_evict_real_outcomes_from_bounded_sample():
    for user_id in ("u_health_real_failure", "u_health_superseded"):
        seed_user(user_id)
        set_v2_runtime_owner(user_id)
    failed_job, _ = jobs_store.enqueue_job("u_health_real_failure", "chat")
    superseded_job, _ = jobs_store.enqueue_job("u_health_superseded", "chat")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='failed',finished_at=clock_timestamp()-"
            "interval '1 second',last_error='turn_failed:test' WHERE id=%s",
            (failed_job,),
        )
        conn.execute(
            "UPDATE agent_jobs SET status='superseded',finished_at=clock_timestamp() "
            "WHERE id=%s",
            (superseded_job,),
        )

    health = jobs_store.recent_chat_operational_health(limit=1)

    assert health["jobs"]["sampled_terminal_jobs"] == 2
    assert health["jobs"]["failed"] == 1
    assert health["jobs"]["superseded"] == 1
    assert health["jobs"]["failure_rate"] == 1.0


def test_required_append_failure_is_marked_partial_before_terminal():
    uid = "u_trajectory_required_gap"
    job_id, _job = _source_job(uid)

    def seal(user_id, plaintext, item_id):
        return _envelope(
            user_id,
            item_id,
            base64.b64encode(plaintext).decode("ascii"),
        )

    def append_batch(source_job_id, user_id, *, events):
        if events[0]["event_kind"] == "provider_response":
            raise RuntimeError("simulated required append failure")
        return jobs_store.append_trajectory_events_batch(
            source_job_id,
            user_id,
            events=events,
        )

    recorder = trajectory.TrajectoryRecorder(
        job_id=job_id,
        user_id=uid,
        seal=seal,
        append=jobs_store.append_trajectory_event,
        append_batch=append_batch,
    )
    with pytest.raises(RuntimeError, match="required append failure"):
        asyncio.run(recorder.record("provider_response", {"reply": "ok"}))
    asyncio.run(recorder.record("turn_exception", {"error_code": "capture_failed"}))
    asyncio.run(recorder.record("turn_terminal", {"outcome": "failed"}))

    rows = jobs_store.list_trajectory_events(job_id, uid)
    assert [row["event_kind"] for row in rows] == [
        "turn_exception",
        "capture_gap",
        "turn_terminal",
    ]
    state = jobs_store.get_trajectory_capture_state(job_id, uid)
    assert state["has_capture_gap"] is True
    assert state["capture_status"] == "partial"


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


def test_review_reconciler_recovers_pending_backlog_after_kill_switch_reenable(
    monkeypatch,
):
    uid = "u_trajectory_review_reenable_recovery"
    source_job_id, _job = _source_job(uid)
    assert jobs_store.mark_failed(
        source_job_id,
        "turn_failed:providererror",
        claimed_by="worker-source",
    )
    runner = jobs_store.claim_next_job("worker-review", lanes={"trajectory_review"})
    assert runner is not None
    assert jobs_store.mark_running(runner["id"], claimed_by="worker-review")
    claimed = jobs_store.claim_failure_review(
        uid,
        runner_job_id=runner["id"],
        claimed_by="worker-review",
    )
    assert claimed is not None and claimed["status"] == "running"

    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED", "0")
    assert jobs_store.mark_failed(
        runner["id"],
        "trajectory_review_disabled",
        claimed_by="worker-review",
    )
    recovered = jobs_store.get_failure_review(source_job_id, uid)
    assert recovered is not None and recovered["status"] == "pending"
    assert jobs_store.reconcile_failure_review_runners(limit=8) == 0
    with db.get_pool().connection() as conn:
        active_while_off = conn.execute(
            "SELECT COUNT(*) FROM agent_jobs WHERE user_id=%s "
            "AND lane='trajectory_review' "
            "AND status IN ('pending','claimed','running')",
            (uid,),
        ).fetchone()[0]
    assert active_while_off == 0

    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED", "1")
    barrier = threading.Barrier(2)

    def reconcile():
        barrier.wait()
        return jobs_store.reconcile_failure_review_runners(limit=8)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reconciled = list(pool.map(lambda _index: reconcile(), range(2)))
    assert sum(reconciled) == 1
    assert jobs_store.reconcile_failure_review_runners(limit=8) == 0
    with db.get_pool().connection() as conn:
        active_after_reenable = conn.execute(
            "SELECT id,status,reason FROM agent_jobs WHERE user_id=%s "
            "AND lane='trajectory_review' "
            "AND status IN ('pending','claimed','running')",
            (uid,),
        ).fetchall()
    assert len(active_after_reenable) == 1
    assert active_after_reenable[0][1:] == ("pending", "terminal_failure_review")


def test_review_reconciler_bounds_each_tick_by_pending_user(monkeypatch):
    users = ("u_review_reconcile_limit_a", "u_review_reconcile_limit_b")
    for uid in users:
        source_job_id, _job = _source_job(uid)
        assert jobs_store.mark_failed(
            source_job_id,
            "turn_failed:providererror",
            claimed_by="worker-source",
        )

    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED", "0")
    for index in range(2):
        runner = jobs_store.claim_next_job(
            f"review-limit-worker-{index}",
            lanes={"trajectory_review"},
        )
        assert runner is not None
        claimed_by = f"review-limit-worker-{index}"
        assert jobs_store.mark_running(runner["id"], claimed_by=claimed_by)
        assert jobs_store.mark_failed(
            runner["id"],
            "trajectory_review_disabled",
            claimed_by=claimed_by,
        )

    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED", "1")
    assert jobs_store.reconcile_failure_review_runners(limit=1) == 1
    with db.get_pool().connection() as conn:
        first_tick_active = conn.execute(
            "SELECT COUNT(*) FROM agent_jobs WHERE lane='trajectory_review' "
            "AND status IN ('pending','claimed','running')"
        ).fetchone()[0]
    assert first_tick_active == 1
    assert jobs_store.reconcile_failure_review_runners(limit=1) == 1
    assert jobs_store.reconcile_failure_review_runners(limit=1) == 0


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
