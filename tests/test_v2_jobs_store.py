"""jobs_store：single-flight coalesce、SKIP LOCKED 独占 claim、job 生命周期。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from core import wake_bus
from model_api_runtime.v2 import jobs_store

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 jobs_store tests require the PostgreSQL test fixture",
)


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'v2',1) ON CONFLICT (user_id) DO UPDATE SET "
            "hosted_runtime_state='v2',runtime_generation=1",
            (uid,),
        )


def _seed_active_route(uid: str, *, error: str = "") -> str:
    credential_id = str(uuid.uuid4())
    route_id = str(uuid.uuid4())
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO model_api_credentials "
            "(id,user_id,provider,label,base_url,api_key_envelope) "
            "VALUES (%s,%s,'anthropic','test','','{}'::jsonb)",
            (credential_id, uid),
        )
        conn.execute(
            "INSERT INTO model_api_routes "
            "(id,user_id,credential_id,model,is_active,test_status,last_runtime_error) "
            "VALUES (%s,%s,%s,'claude-test',true,'ok',%s)",
            (route_id, uid, credential_id, error),
        )
    return route_id


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    """claim_next_job() is a GLOBAL work-queue claim (by design it doesn't filter
    by user_id — any worker can pick up any user's pending job). That means a
    pending job left behind by one test (e.g. an enqueue test that never drains
    it) pollutes `ORDER BY priority DESC, created_at` for every later test in
    this module and gets claimed instead of the row the test just created.
    Truncate the whole table before each test so claim tests only ever see
    the row(s) they set up themselves.

    Also clears `v2_runtime_state` (Task 2's per-user cutover generation row):
    generation tests advance a user's generation via `db.advance_runtime_state`,
    and a leftover row from an earlier test would let a later test's
    `db.get_runtime_generation("u_...")` lazy-init see a stale generation
    instead of starting fresh at 1."""
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
        conn.execute("DELETE FROM v2_runtime_state")
    yield


def test_enqueue_returns_job_id_and_not_coalesced_first_time():
    seed_user("u_js_1"); _reset("u_js_1")
    job_id, coalesced = jobs_store.enqueue_job("u_js_1", "chat", reason="hi")
    assert isinstance(job_id, int) and job_id > 0
    assert coalesced is False


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "nope"])
def test_positive_job_ttl_settings_fail_closed(monkeypatch, raw):
    monkeypatch.setenv("TEST_V2_POSITIVE_FLOAT", raw)
    with pytest.raises(RuntimeError, match="finite and > 0"):
        jobs_store._positive_float_env("TEST_V2_POSITIVE_FLOAT", "1")


def test_enqueue_same_user_lane_coalesces_to_existing_pending():
    seed_user("u_js_2"); _reset("u_js_2")
    first_id, first_c = jobs_store.enqueue_job("u_js_2", "chat")
    second_id, second_c = jobs_store.enqueue_job("u_js_2", "chat")
    assert second_id == first_id
    assert first_c is False and second_c is True


def test_enqueue_replaces_overdue_pending_row_instead_of_coalescing():
    seed_user("u_js_stale_pending"); _reset("u_js_stale_pending")
    old_id, _ = jobs_store.enqueue_job("u_js_stale_pending", "chat")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET queue_deadline_at=now()-interval '1 second' "
            "WHERE id=%s",
            (old_id,),
        )

    new_id, coalesced = jobs_store.enqueue_job("u_js_stale_pending", "chat")

    assert coalesced is False and new_id != old_id
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status,last_error FROM agent_jobs "
            "WHERE user_id=%s ORDER BY id",
            ("u_js_stale_pending",),
        ).fetchall()
        marker = conn.execute(
            "SELECT error_code FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (old_id,),
        ).fetchone()
    assert rows == [
        (old_id, "expired", "queue_timeout"),
        (new_id, "pending", None),
    ]
    assert marker == ("queue_timeout",)


def test_enqueue_replaces_expired_active_lease_and_fences_old_owner():
    seed_user("u_js_stale_active"); _reset("u_js_stale_active")
    old_id, _ = jobs_store.enqueue_job("u_js_stale_active", "chat")
    jobs_store.claim_next_job("old-owner")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET lease_expires_at=now()-interval '1 second', "
            "deadline_at=now()-interval '1 second' WHERE id=%s",
            (old_id,),
        )

    new_id, coalesced = jobs_store.enqueue_job("u_js_stale_active", "chat")

    assert coalesced is False and new_id != old_id
    assert jobs_store.mark_completed(old_id, claimed_by="old-owner") is False
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status,last_error FROM agent_jobs "
            "WHERE user_id=%s ORDER BY id",
            ("u_js_stale_active",),
        ).fetchall()
    assert rows == [
        (old_id, "expired", "lease_timeout"),
        (new_id, "pending", None),
    ]


def test_chat_enqueue_sets_pending_deadline_and_coalesce_advances_generation():
    seed_user("u_js_deadline"); _reset("u_js_deadline")
    job_id, _ = jobs_store.enqueue_job("u_js_deadline", "chat")
    with db.get_pool().connection() as conn:
        before = conn.execute(
            "SELECT queue_deadline_at,deadline_at,input_generation "
            "FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
    assert before[0] is not None
    assert before[1] is None  # old workers mint their own full execution deadline at claim
    assert before[2] == 0

    same_id, coalesced = jobs_store.enqueue_job("u_js_deadline", "chat")
    with db.get_pool().connection() as conn:
        after = conn.execute(
            "SELECT queue_deadline_at,deadline_at,input_generation "
            "FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
    assert (same_id, coalesced) == (job_id, True)
    assert after[0] == before[0]  # coalescing must not postpone the oldest input forever
    assert after[1] is None
    assert after[2] == 1


def test_enqueue_rejects_unknown_lane():
    seed_user("u_js_2b"); _reset("u_js_2b")
    with pytest.raises(ValueError):
        jobs_store.enqueue_job("u_js_2b", "not_a_lane")


def test_claim_moves_pending_to_claimed_and_returns_row():
    seed_user("u_js_3"); _reset("u_js_3")
    job_id, _ = jobs_store.enqueue_job("u_js_3", "chat", trace_id="t1")
    row = jobs_store.claim_next_job("worker-A")
    assert row is not None
    assert row["id"] == job_id
    assert row["status"] == "claimed"
    assert row["claimed_by"] == "worker-A"
    assert row["trace_id"] == "t1"
    with db.get_pool().connection() as conn:
        protocol = conn.execute(
            "SELECT current_setting('feedling.v2_worker_protocol',true)"
        ).fetchone()[0]
    # Claim success proves the trigger observed 0041 inside the transaction;
    # the pooled connection must not retain that authority after commit.
    assert protocol != jobs_store._WORKER_CLAIM_PROTOCOL


def test_claim_converts_pending_deadline_to_active_lease():
    seed_user("u_js_3b"); _reset("u_js_3b")
    job_id, _ = jobs_store.enqueue_job("u_js_3b", "chat")  # no explicit deadline_at
    row = jobs_store.claim_next_job("worker-B")
    assert row is not None
    assert row["id"] == job_id
    assert row["status"] == "claimed"
    assert row["queue_deadline_at"] is not None
    assert row["deadline_at"] is not None  # rollback-compatible legacy lease mirror
    assert row["lease_expires_at"] is not None


def test_claim_is_exclusive_second_claim_skips():
    # single-flight means at most one active job per (user, lane); after one claim
    # of the only pending job, a second claim finds nothing.
    seed_user("u_js_4"); _reset("u_js_4")
    jobs_store.enqueue_job("u_js_4", "chat")
    first = jobs_store.claim_next_job("w1")
    second = jobs_store.claim_next_job("w2")
    assert first is not None
    assert second is None


def test_lifecycle_running_completed_frees_singleflight_slot():
    seed_user("u_js_5"); _reset("u_js_5")
    job_id, _ = jobs_store.enqueue_job("u_js_5", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id, claimed_by="w")
    jobs_store.mark_completed(job_id, claimed_by="w")
    # completed is terminal → the partial unique index no longer covers it →
    # a new job can be enqueued fresh (not coalesced).
    new_id, coalesced = jobs_store.enqueue_job("u_js_5", "chat")
    assert new_id != job_id
    assert coalesced is False


def test_mark_failed_increments_attempt_count():
    seed_user("u_js_6"); _reset("u_js_6")
    job_id, _ = jobs_store.enqueue_job("u_js_6", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_failed(job_id, "boom", claimed_by="w")
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, attempt_count, last_error FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
    assert row[0] == "failed"
    assert row[1] == 1
    assert row[2] == "boom"


def test_mark_expired_retained_helper_also_queues_chat_visibility():
    uid = "u_js_mark_expired"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")

    jobs_store.mark_expired(job_id, "queue_timeout")

    with db.get_pool().connection() as conn:
        job = conn.execute(
            "SELECT status,last_error FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        marker = conn.execute(
            "SELECT error_code FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert job == ("expired", "queue_timeout")
    assert marker == ("queue_timeout",)


def test_mark_failed_crash_window_has_durable_visibility_marker_and_replays_once():
    """Simulate death immediately after terminalization by doing no inline
    surfacing.  A later reconciler must find both obligations, and replaying it
    again must not duplicate either the status event or the idempotent callback.
    """
    uid = "u_js_terminal_crash"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w")

    assert jobs_store.mark_failed(
        job_id, "turn_failed:runtimeerror", claimed_by="w") is True

    with db.get_pool().connection() as conn:
        marker = conn.execute(
            "SELECT user_id,error_code,status_delivered_at,"
            "runtime_error_delivered_at FROM v2_terminal_failure_outbox "
            "WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert marker == (uid, "turn_failed:runtimeerror", None, None)

    recorded = []
    first = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=lambda user_id, code: recorded.append((user_id, code)),
        job_id=job_id,
    )
    second = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=lambda user_id, code: recorded.append((user_id, code)),
        job_id=job_id,
    )

    assert first == {
        "examined": 1,
        "status_delivered": 1,
        "runtime_error_delivered": 1,
    }
    assert second == {
        "examined": 0,
        "status_delivered": 0,
        "runtime_error_delivered": 0,
    }
    assert recorded == [(uid, "turn_failed:runtimeerror")]
    errors = [
        event for event in jobs_store.list_status_events(uid, after_id=0)
        if event["kind"] == "error" and event["job_id"] == job_id
    ]
    assert len(errors) == 1


def test_terminal_visibility_retries_each_fail_once_sink_without_duplicates(monkeypatch):
    uid = "u_js_terminal_retry"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w")
    assert jobs_store.mark_failed(job_id, "provider_unavailable", claimed_by="w")

    real_status_sink = jobs_store._deliver_terminal_failure_status
    status_attempts = {"n": 0}

    def _status_fails_once(*args, **kwargs):
        status_attempts["n"] += 1
        if status_attempts["n"] == 1:
            raise RuntimeError("transient status sink")
        return real_status_sink(*args, **kwargs)

    runtime_attempts = {"n": 0}
    recorded = []

    def _runtime_fails_once(user_id, code):
        runtime_attempts["n"] += 1
        if runtime_attempts["n"] == 1:
            raise RuntimeError("transient runtime sink")
        recorded.append((user_id, code))

    monkeypatch.setattr(
        jobs_store, "_deliver_terminal_failure_status", _status_fails_once)

    base_now = time.time()
    first = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=_runtime_fails_once, job_id=job_id, now=base_now)
    second = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=_runtime_fails_once, job_id=job_id, now=base_now + 2)
    third = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=_runtime_fails_once, job_id=job_id, now=base_now + 4)

    assert first["status_delivered"] == 0
    assert first["runtime_error_delivered"] == 0
    assert second["status_delivered"] == 1
    assert second["runtime_error_delivered"] == 1
    assert third["examined"] == 0
    assert status_attempts["n"] == 2
    assert runtime_attempts["n"] == 2
    assert recorded == [(uid, "provider_unavailable")]
    errors = [
        event for event in jobs_store.list_status_events(uid, after_id=0)
        if event["kind"] == "error" and event["job_id"] == job_id
    ]
    assert len(errors) == 1


def test_terminal_visibility_redacts_unstable_error_before_user_sinks():
    uid = "u_js_terminal_redact"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w")
    assert jobs_store.mark_failed(
        job_id, "raw provider secret sk-do-not-leak", claimed_by="w")

    recorded = []
    jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=lambda user_id, code: recorded.append((user_id, code)),
        job_id=job_id,
    )
    assert recorded == [(uid, "runtime_failed")]


def test_production_runtime_error_sink_updates_captured_active_route_atomically():
    uid = "u_js_terminal_route_delivery"
    seed_user(uid)
    _reset(uid)
    _seed_active_route(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w")
    assert jobs_store.mark_failed(job_id, "lease_timeout", claimed_by="w")

    result = jobs_store.reconcile_terminal_failure_outbox(job_id=job_id)

    assert result["runtime_error_delivered"] == 1
    with db.get_pool().connection() as conn:
        route_error = conn.execute(
            "SELECT last_runtime_error FROM model_api_routes "
            "WHERE user_id=%s AND is_active",
            (uid,),
        ).fetchone()[0]
        delivered = conn.execute(
            "SELECT runtime_error_delivered_at IS NOT NULL "
            "FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (job_id,),
        ).fetchone()[0]
    assert route_error == "lease_timeout"
    assert delivered is True


def test_poison_oldest_rotates_so_newer_marker_delivers_both_sinks(monkeypatch):
    uid = "u_js_terminal_fair"
    seed_user(uid)
    _reset(uid)
    old_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w-old")
    assert jobs_store.mark_failed(old_id, "queue_timeout", claimed_by="w-old")
    new_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w-new")
    assert jobs_store.mark_failed(new_id, "lease_timeout", claimed_by="w-new")

    real_status = jobs_store._deliver_terminal_failure_status

    def _poison_old_status(job_id, **kwargs):
        if job_id == old_id:
            raise RuntimeError("permanent old status poison")
        return real_status(job_id, **kwargs)

    delivered_runtime = []

    def _poison_old_runtime(user_id, code):
        if code == "queue_timeout":
            raise RuntimeError("permanent old runtime poison")
        delivered_runtime.append((user_id, code))

    monkeypatch.setattr(
        jobs_store, "_deliver_terminal_failure_status", _poison_old_status)
    base_now = time.time()
    jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=_poison_old_runtime, limit=1, now=base_now)
    second = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=_poison_old_runtime, limit=1, now=base_now + 2)

    assert second["status_delivered"] == 1
    assert second["runtime_error_delivered"] == 1
    assert delivered_runtime == [(uid, "lease_timeout")]
    errors = [
        event for event in jobs_store.list_status_events(uid, after_id=0)
        if event["kind"] == "error"
    ]
    assert [event["job_id"] for event in errors] == [new_id]


def test_delayed_failure_cannot_overwrite_newer_success(monkeypatch):
    uid = "u_js_terminal_newer_success"
    seed_user(uid)
    _reset(uid)
    _seed_active_route(uid)

    failed_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w-fail")
    assert jobs_store.mark_failed(
        failed_id, "provider_unavailable", claimed_by="w-fail")

    real_runtime_sink = jobs_store._deliver_terminal_failure_runtime_error
    attempts = {"n": 0}

    def _runtime_fails_once(job_id):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient route write")
        return real_runtime_sink(job_id)

    monkeypatch.setattr(
        jobs_store, "_deliver_terminal_failure_runtime_error", _runtime_fails_once)
    base_now = time.time()
    jobs_store.reconcile_terminal_failure_outbox(job_id=failed_id, now=base_now)

    success_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w-success")
    assert jobs_store.mark_running(success_id, claimed_by="w-success")
    completed, _successor = jobs_store.finish_chat_job(
        success_id, claimed_by="w-success", observed_generation=0)
    assert completed

    retried = jobs_store.reconcile_terminal_failure_outbox(
        job_id=failed_id, now=base_now + 2)
    assert retried["runtime_error_delivered"] == 1
    with db.get_pool().connection() as conn:
        route_error = conn.execute(
            "SELECT last_runtime_error FROM model_api_routes "
            "WHERE user_id=%s AND is_active",
            (uid,),
        ).fetchone()[0]
    assert route_error == ""


def test_delayed_failure_never_stamps_newly_active_route(monkeypatch):
    uid = "u_js_terminal_route_switch"
    seed_user(uid)
    _reset(uid)
    old_route_id = _seed_active_route(uid)
    failed_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w")
    assert jobs_store.mark_failed(failed_id, "lease_timeout", claimed_by="w")

    real_runtime_sink = jobs_store._deliver_terminal_failure_runtime_error
    monkeypatch.setattr(
        jobs_store,
        "_deliver_terminal_failure_runtime_error",
        lambda _job_id: (_ for _ in ()).throw(RuntimeError("transient route write")),
    )
    base_now = time.time()
    jobs_store.reconcile_terminal_failure_outbox(job_id=failed_id, now=base_now)

    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE model_api_routes SET is_active=false,updated_at=now() WHERE id=%s",
            (old_route_id,),
        )
    new_route_id = _seed_active_route(uid)
    monkeypatch.setattr(
        jobs_store, "_deliver_terminal_failure_runtime_error", real_runtime_sink)
    jobs_store.reconcile_terminal_failure_outbox(
        job_id=failed_id, now=base_now + 2)
    with db.get_pool().connection() as conn:
        errors = conn.execute(
            "SELECT id::text,last_runtime_error FROM model_api_routes "
            "WHERE id IN (%s,%s) ORDER BY id",
            (old_route_id, new_route_id),
        ).fetchall()
    assert all(error == "" for _route_id, error in errors)


def test_enqueue_after_failed_job_also_coalesces_free(monkeypatch=None):
    """Partial-index crux: a job in a TERMINAL status ('failed') must not block
    a fresh enqueue for the same (user, lane) — only 'pending'/'claimed'/'running'
    rows are covered by ux_agent_jobs_singleflight. A full (non-partial) unique
    index would wrongly reject/coalesce this new INSERT.
    """
    seed_user("u_js_7"); _reset("u_js_7")
    job_id, _ = jobs_store.enqueue_job("u_js_7", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_failed(job_id, "boom", claimed_by="w")
    new_id, coalesced = jobs_store.enqueue_job("u_js_7", "chat")
    assert new_id != job_id
    assert coalesced is False


def test_reap_expires_stuck_claimed_job_by_deadline():
    seed_user("u_js_7b"); _reset("u_js_7b")
    job_id, _ = jobs_store.enqueue_job("u_js_7b", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id, claimed_by="w")
    # reap with a "now" far in the future → deadline is in the past relative to it.
    import time
    reaped = jobs_store.reap_stuck_jobs(now=time.time() + jobs_store.RUNNING_TTL_SEC + 10)
    assert reaped == 1
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
        marker = conn.execute(
            "SELECT error_code FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert row[0] == "expired"
    assert marker == ("lease_timeout",)


def test_reap_leaves_fresh_running_job_alone():
    seed_user("u_js_8"); _reset("u_js_8")
    job_id, _ = jobs_store.enqueue_job("u_js_8", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id, claimed_by="w")
    reaped = jobs_store.reap_stuck_jobs()  # now=None → now(); deadline is in the future
    assert reaped == 0


def test_reap_expires_overdue_pending_chat_job():
    seed_user("u_js_pending_timeout"); _reset("u_js_pending_timeout")
    job_id, _ = jobs_store.enqueue_job("u_js_pending_timeout", "chat")
    reaped = jobs_store.reap_stuck_job_rows(
        now=time.time() + jobs_store.PENDING_CHAT_TTL_SEC + 10)
    assert [(row["id"], row["last_error"]) for row in reaped] == [
        (job_id, "queue_timeout")
    ]


def test_reap_expires_legacy_pending_chat_without_queue_deadline():
    seed_user("u_js_legacy_pending"); _reset("u_js_legacy_pending")
    with db.get_pool().connection() as conn:
        job_id = conn.execute(
            "INSERT INTO agent_jobs "
            "(user_id,lane,status,created_at,queue_deadline_at,deadline_at) "
            "VALUES (%s,'chat','pending',now() - interval '10 minutes',NULL,NULL) "
            "RETURNING id",
            ("u_js_legacy_pending",),
        ).fetchone()[0]

    reaped = jobs_store.reap_stuck_job_rows()

    assert [(row["id"], row["last_error"]) for row in reaped] == [
        (job_id, "queue_timeout")
    ]


def test_reap_expires_legacy_active_job_using_deadline_fallback():
    seed_user("u_js_legacy_active"); _reset("u_js_legacy_active")
    with db.get_pool().connection() as conn:
        job_id = conn.execute(
            "INSERT INTO agent_jobs "
            "(user_id,lane,status,claimed_by,claimed_at,deadline_at,lease_expires_at) "
            "VALUES (%s,'chat','claimed','old-worker',now() - interval '10 minutes',"
            "now() - interval '5 minutes',NULL) RETURNING id",
            ("u_js_legacy_active",),
        ).fetchone()[0]

    reaped = jobs_store.reap_stuck_job_rows()

    assert [(row["id"], row["last_error"]) for row in reaped] == [
        (job_id, "lease_timeout")
    ]


def test_owner_fence_and_late_input_successor():
    seed_user("u_js_successor"); _reset("u_js_successor")
    job_id, _ = jobs_store.enqueue_job("u_js_successor", "chat")
    jobs_store.claim_next_job("owner-a")
    assert jobs_store.mark_running(job_id, claimed_by="owner-a") is True
    assert jobs_store.mark_completed(job_id, claimed_by="owner-b") is False
    assert jobs_store.get_job_status(
        job_id,
        user_id="u_js_successor",
        claimed_by="owner-a",
    ) == "running"
    assert jobs_store.get_job_status(
        job_id,
        user_id="u_js_successor",
        claimed_by="owner-b",
    ) is None

    observed = jobs_store.get_input_generation(job_id, claimed_by="owner-a")
    assert observed == 0
    same_id, coalesced = jobs_store.enqueue_job("u_js_successor", "chat")
    assert (same_id, coalesced) == (job_id, True)

    completed, successor_id = jobs_store.finish_chat_job(
        job_id, claimed_by="owner-a", observed_generation=observed)
    assert completed is True
    assert successor_id is not None
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status FROM agent_jobs WHERE user_id=%s ORDER BY id",
            ("u_js_successor",),
        ).fetchall()
    assert rows == [(job_id, "completed"), (successor_id, "pending")]
    assert jobs_store.get_job_status(
        job_id,
        user_id="u_js_successor",
        claimed_by="owner-a",
    ) == "completed"


def test_forced_successor_is_generation_pinned_singleflight_and_preserves_error():
    uid = "u_js_forced_successor"
    seed_user(uid)
    _reset(uid)
    _seed_active_route(uid, error="previous_runtime_failure")
    generation = db.get_runtime_generation(uid)
    job_id, _ = jobs_store.enqueue_job(
        uid, "chat", expected_generation=generation)
    claimed = jobs_store.claim_next_job("force-owner")
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by="force-owner")

    start = threading.Barrier(8)

    def finish_once():
        start.wait(timeout=3)
        return jobs_store.finish_chat_job(
            job_id,
            claimed_by="force-owner",
            observed_generation=0,
            force_successor=True,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _i: finish_once(), range(8)))

    winners = [result for result in results if result[0]]
    assert len(winners) == 1
    successor_id = winners[0][1]
    assert successor_id is not None
    assert all(result == (False, None) for result in results if not result[0])
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status,reason,expected_runtime_generation "
            "FROM agent_jobs WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
        route_error = conn.execute(
            "SELECT last_runtime_error FROM model_api_routes "
            "WHERE user_id=%s AND is_active",
            (uid,),
        ).fetchone()[0]
    assert rows == [
        (job_id, "completed", None, generation),
        (successor_id, "pending", "coalesced_followup", generation),
    ]
    # A superseded candidate was not a successful user-visible turn, so it must
    # not erase the most recent diagnostic while handing input to the successor.
    assert route_error == "previous_runtime_failure"


@pytest.mark.parametrize(
    ("state", "generation"),
    [("draining", 1), ("v2", 2)],
)
def test_forced_successor_declines_after_runtime_ownership_changes(
    state, generation,
):
    uid = f"u_js_forced_successor_fenced_{state}_{generation}"
    seed_user(uid)
    _reset(uid)
    original_generation = db.get_runtime_generation(uid)
    job_id, _ = jobs_store.enqueue_job(
        uid, "chat", expected_generation=original_generation)
    claimed = jobs_store.claim_next_job("force-fenced-owner")
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by="force-fenced-owner")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET hosted_runtime_state=%s,"
            "runtime_generation=%s WHERE user_id=%s",
            (state, generation, uid),
        )

    assert jobs_store.finish_chat_job(
        job_id,
        claimed_by="force-fenced-owner",
        observed_generation=0,
        force_successor=True,
    ) == (False, None)
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status FROM agent_jobs WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
    assert rows == [(job_id, "running")]


def test_finish_chat_job_blocked_past_lease_expiry_fails_closed():
    uid = "u_js_finish_lease_expires_while_blocked"
    seed_user(uid)
    _reset(uid)
    generation = db.get_runtime_generation(uid)
    job_id, _ = jobs_store.enqueue_job(
        uid, "chat", expected_generation=generation)
    claimed = jobs_store.claim_next_job("expiry-owner")
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by="expiry-owner")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET lease_expires_at="
            "clock_timestamp() + interval '1 second' WHERE id=%s",
            (job_id,),
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.get_pool().connection() as blocker:
            with blocker.transaction():
                blocker.execute(
                    "SELECT 1 FROM agent_jobs WHERE id=%s FOR UPDATE",
                    (job_id,),
                )
                future = pool.submit(
                    jobs_store.finish_chat_job,
                    job_id,
                    claimed_by="expiry-owner",
                    observed_generation=0,
                    force_successor=True,
                )

                # Prove the finisher started before expiry and is specifically
                # sleeping on the held job-row lock; this makes the regression
                # distinguish transaction-start now() from clock_timestamp().
                deadline = time.monotonic() + 3
                waiting = False
                while time.monotonic() < deadline:
                    waiting = bool(blocker.execute(
                        "SELECT EXISTS ("
                        " SELECT 1 FROM pg_stat_activity "
                        " WHERE datname=current_database() "
                        " AND wait_event_type='Lock' "
                        " AND query LIKE '%%expected_runtime_generation%%' "
                        " AND query LIKE '%%FROM agent_jobs%%FOR UPDATE%%'"
                        ")"
                    ).fetchone()[0])
                    if waiting:
                        break
                    time.sleep(0.01)
                assert waiting, "finisher never reached the blocked lease check"
                remaining = float(blocker.execute(
                    "SELECT GREATEST(EXTRACT(EPOCH FROM "
                    "(lease_expires_at-clock_timestamp())),0) "
                    "FROM agent_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()[0])
                time.sleep(remaining + 0.1)
        result = future.result(timeout=3)

    assert result == (False, None)
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status FROM agent_jobs WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
    assert rows == [(job_id, "running")]


def test_claim_serializes_all_lanes_per_user():
    seed_user("u_js_lane_lock"); _reset("u_js_lane_lock")
    chat_id, _ = jobs_store.enqueue_job("u_js_lane_lock", "chat")
    wake_id, _ = jobs_store.enqueue_job("u_js_lane_lock", "heartbeat")

    first = jobs_store.claim_next_job("owner-a")
    second = jobs_store.claim_next_job("owner-b")

    assert first["id"] == chat_id
    assert second is None
    assert jobs_store.mark_failed(chat_id, "done", claimed_by="owner-a") is True
    assert jobs_store.claim_next_job("owner-b")["id"] == wake_id


def test_status_events_append_and_list_by_cursor():
    seed_user("u_js_9")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_status_events WHERE user_id='u_js_9'")
    id1 = jobs_store.append_status_event("u_js_9", "processing", label="starting")
    id2 = jobs_store.append_status_event(
        "u_js_9", "reading_memory", label="读取上下文", detail={"count": 3}
    )
    assert id2 > id1
    events = jobs_store.list_status_events("u_js_9", after_id=id1)
    assert [e["kind"] for e in events] == ["reading_memory"]
    assert events[0]["label"] == "读取上下文"
    assert events[0]["detail_json"] == {"count": 3}
    assert events[0]["id"] == id2


def test_append_status_event_fires_cross_process_chat_wake(monkeypatch):
    """FIX 2 (§9): the V2 worker writes status events from a separate process than
    the web tier holding the parked chat long-poll. append_status_event must fire
    a cross-process wake on the "chat" channel after the INSERT commits, so the
    parked poll sees intermediate status progressively instead of only at
    turn-end / on its ~30s timeout."""
    seed_user("u_js_9d")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_status_events WHERE user_id='u_js_9d'")
    calls = []
    monkeypatch.setattr(wake_bus, "notify", lambda channel, user_id="": calls.append((channel, user_id)))
    event_id = jobs_store.append_status_event("u_js_9d", "processing", label="starting")
    assert calls == [("chat", "u_js_9d")]
    # The notify is additive/best-effort — the status row itself must still land.
    events = jobs_store.list_status_events("u_js_9d", after_id=0)
    assert [e["id"] for e in events] == [event_id]
    assert events[0]["kind"] == "processing"


def test_list_status_events_delegates_to_db_primitive(monkeypatch):
    """Cross-plan amendment: jobs_store.list_status_events must not run its own SQL —
    it delegates to db.list_agent_status_events so Plan C's long-poll reads the same
    single source of truth."""
    seed_user("u_js_9b")
    calls = []

    def _fake(user_id, *, after_id=0, limit=50):
        calls.append((user_id, after_id, limit))
        return ["sentinel"]

    monkeypatch.setattr(db, "list_agent_status_events", _fake)
    result = jobs_store.list_status_events("u_js_9b", after_id=5, limit=10)
    assert result == ["sentinel"]
    assert calls == [("u_js_9b", 5, 10)]


def test_db_list_agent_status_events_primitive_reads_raw_rows():
    seed_user("u_js_9c")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_status_events WHERE user_id='u_js_9c'")
    id1 = jobs_store.append_status_event("u_js_9c", "processing", label="starting")
    id2 = jobs_store.append_status_event(
        "u_js_9c", "reading_memory", label="读取上下文", detail={"count": 3}
    )
    all_events = db.list_agent_status_events("u_js_9c")
    assert [e["id"] for e in all_events] == [id1, id2]
    after = db.list_agent_status_events("u_js_9c", after_id=id1)
    assert len(after) == 1
    assert after[0]["id"] == id2
    assert after[0]["kind"] == "reading_memory"
    assert after[0]["detail_json"] == {"count": 3}
    assert isinstance(after[0]["created_at"], float)
    limited = db.list_agent_status_events("u_js_9c", after_id=0, limit=1)
    assert [e["id"] for e in limited] == [id1]


def test_runtime_state_upsert_merges_patch():
    seed_user("u_js_10")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM runtime_state WHERE user_id='u_js_10'")
    assert jobs_store.get_runtime_state("u_js_10") == {}
    jobs_store.upsert_runtime_state("u_js_10", {"a": 1})
    merged = jobs_store.upsert_runtime_state("u_js_10", {"b": 2})
    assert merged == {"a": 1, "b": 2}
    assert jobs_store.get_runtime_state("u_js_10") == {"a": 1, "b": 2}


# --- §6 admission ceiling: 三个纯读查询 (live_worker_count / inflight_job_count /
# recent_mean_service_sec) ---------------------------------------------------


def test_live_worker_count_counts_only_recent():
    jobs_store.record_worker_heartbeat("w-fresh-1")
    jobs_store.record_worker_heartbeat("w-fresh-2")
    # 塞一个陈旧心跳（beat_at 在窗口外）
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_worker_heartbeats (worker_id, beat_at) "
            "VALUES (%s, now() - make_interval(secs => %s)) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at = EXCLUDED.beat_at",
            ("w-stale", 120),
        )
    assert jobs_store.live_worker_count(within_sec=30) >= 2
    # 陈旧的不计入
    n_wide = jobs_store.live_worker_count(within_sec=300)
    n_narrow = jobs_store.live_worker_count(within_sec=30)
    assert n_wide > n_narrow


def test_inflight_job_count_counts_active_states():
    seed_user("u_js_11"); _reset("u_js_11")
    before = jobs_store.inflight_job_count()
    jobs_store.enqueue_job("u_js_11", "chat", reason="t")
    assert jobs_store.inflight_job_count() == before + 1


def test_recent_mean_service_sec_none_without_history():
    # 全新 lane，无 completed job
    assert jobs_store.recent_mean_service_sec(lane="no-such-lane") is None


def test_recent_mean_service_sec_averages_completed():
    seed_user("u_js_12")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, started_at, finished_at) "
            "VALUES (%s,'svc-test','completed', now() - make_interval(secs=>10), now())",
            ("u_js_12",),
        )
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, started_at, finished_at) "
            "VALUES (%s,'svc-test','completed', now() - make_interval(secs=>20), now())",
            ("u_js_12",),
        )
    mean = jobs_store.recent_mean_service_sec(lane="svc-test", limit=50)
    assert mean is not None
    assert 14.0 <= mean <= 16.0  # (10+20)/2 = 15


# --- kind discriminator: genesis heartbeats must be invisible to the chat/send
# admission gate (workers_alive / live_worker_count read only kind='turn') ---


def _clear_heartbeats():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_worker_heartbeats")


def test_genesis_heartbeat_does_not_inflate_turn_worker_liveness():
    """A genesis heartbeat row must be invisible to the chat/send admission gate.

    live_worker_count() feeds admission.estimate_wait_sec(workers=...); counting a
    genesis row as a turn worker would halve the estimated queue wait for a
    single-process pool and over-admit onto turn slots that do not exist.
    """
    _clear_heartbeats()
    jobs_store.record_worker_heartbeat("w1")                      # default kind='turn'
    jobs_store.record_worker_heartbeat("w1:genesis", kind="genesis")

    assert jobs_store.live_worker_count() == 1
    assert jobs_store.workers_alive() is True
    assert jobs_store.genesis_worker_alive() is True


def test_genesis_heartbeat_alone_does_not_open_the_send_gate():
    """Genesis alive but every turn worker dead => send must still 503."""
    _clear_heartbeats()
    jobs_store.record_worker_heartbeat("only:genesis", kind="genesis")

    assert jobs_store.workers_alive() is False
    assert jobs_store.live_worker_count() == 0
    assert jobs_store.genesis_worker_alive() is True


def test_genesis_worker_alive_false_when_nothing_beats():
    _clear_heartbeats()
    assert jobs_store.genesis_worker_alive() is False


def test_recent_worker_heartbeats_returns_identity_kind_capacity_and_db_age():
    _clear_heartbeats()
    jobs_store.record_worker_heartbeat("v2-worker-new-deadbeef1234", capacity=4)
    jobs_store.record_worker_heartbeat(
        "v2-worker-new-deadbeef1234:genesis", kind="genesis", capacity=0)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_worker_heartbeats (worker_id, beat_at, kind, capacity) "
            "VALUES ('v2-worker-stale', now() - interval '10 minutes', 'turn', 4)"
        )

    rows = jobs_store.recent_worker_heartbeats(within_sec=300)

    assert {row["worker_id"] for row in rows} == {
        "v2-worker-new-deadbeef1234",
        "v2-worker-new-deadbeef1234:genesis",
    }
    turn = next(row for row in rows if row["kind"] == "turn")
    genesis = next(row for row in rows if row["kind"] == "genesis")
    assert turn["capacity"] == 4
    assert genesis["capacity"] == 0
    assert turn["age_sec"] >= 0
    assert isinstance(turn["beat_at_epoch"], float)


def test_enqueue_stamps_expected_generation():
    seed_user("u_jobgen")
    gen = db.get_runtime_generation("u_jobgen")  # 1
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET hosted_runtime_state='v2' WHERE user_id=%s",
            ("u_jobgen",),
        )
    jid, _created = jobs_store.enqueue_job("u_jobgen", "chat", expected_generation=gen)
    row = jobs_store.claim_next_job("w1")
    assert row["id"] == jid
    assert row["expected_runtime_generation"] == gen


def test_stale_generation_job_superseded_at_claim():
    seed_user("u_jobstale")
    jobs_store.enqueue_job("u_jobstale", "chat", expected_generation=1)
    # user cut over: generation moves to 3
    db.advance_runtime_state("u_jobstale", from_state="resident", to_state="draining")
    db.advance_runtime_state("u_jobstale", from_state="draining", to_state="v2")
    claimed = jobs_store.claim_next_job("w1")
    # stale job is not handed out for a turn; it is terminal 'superseded'
    assert claimed is None or claimed["status"] == "superseded"


def test_resident_owned_job_is_superseded_without_running():
    uid = "u_job_resident_owned"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    job_id, _ = jobs_store.enqueue_job(
        uid, "chat", expected_generation=generation)

    assert jobs_store.claim_next_job("w-resident-fence") is None
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status,last_error FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert row == ("superseded", "runtime_state_not_v2")


def test_v2_enqueue_auto_pins_authoritative_generation():
    uid = "u_job_auto_generation"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    with db.get_pool().connection() as conn:
        expected = conn.execute(
            "SELECT expected_runtime_generation FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()[0]
    assert expected == 1


def test_claim_pins_legacy_null_generation_to_authoritative_generation():
    """Pre-fence pending rows survive migration but become ABA-safe at claim."""
    uid = "u_job_claim_pins_legacy_generation"
    seed_user(uid)
    _reset(uid)
    with db.get_pool().connection() as conn:
        job_id = conn.execute(
            "INSERT INTO agent_jobs (user_id,lane,status,priority) "
            "VALUES (%s,'heartbeat','pending',50) RETURNING id",
            (uid,),
        ).fetchone()[0]

    claimed = jobs_store.claim_next_job("w-legacy-generation")

    assert claimed is not None and claimed["id"] == job_id
    assert claimed["expected_runtime_generation"] == 1


def test_generation_aba_between_claim_and_start_loses_ownership():
    uid = "u_job_claim_start_aba"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    claimed = jobs_store.claim_next_job("w-aba")
    assert claimed is not None and claimed["id"] == job_id
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET runtime_generation=3 "
            "WHERE user_id=%s AND hosted_runtime_state='v2'",
            (uid,),
        )

    assert jobs_store.mark_running(job_id, claimed_by="w-aba") is False


def test_generation_aba_during_turn_prevents_lease_renewal():
    uid = "u_job_running_aba"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    claimed = jobs_store.claim_next_job("w-running-aba")
    assert claimed is not None
    assert jobs_store.mark_running(
        job_id, claimed_by="w-running-aba") is True
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET runtime_generation=3 "
            "WHERE user_id=%s AND hosted_runtime_state='v2'",
            (uid,),
        )

    assert jobs_store.renew_job_lease(
        job_id, "w-running-aba") is False


def test_enqueue_after_generation_aba_replaces_old_pending_job():
    uid = "u_job_enqueue_aba_successor"
    seed_user(uid)
    _reset(uid)
    old_id, _ = jobs_store.enqueue_job(uid, "chat")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET runtime_generation=3 "
            "WHERE user_id=%s AND hosted_runtime_state='v2'",
            (uid,),
        )

    new_id, coalesced = jobs_store.enqueue_job(uid, "chat")

    assert coalesced is False
    assert new_id != old_id
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status,expected_runtime_generation FROM agent_jobs "
            "WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
        marker = conn.execute(
            "SELECT 1 FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (old_id,),
        ).fetchone()
    assert rows == [
        (old_id, "superseded", 1),
        (new_id, "pending", 3),
    ]
    assert marker is None
