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
from core import envelope as core_envelope
from core import store as core_store
from core import wake_bus
from model_api_runtime.v2 import cursor as v2_cursor
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


def _append_user_message(uid: str, msg_id: str = "parent-user") -> int:
    store = core_store.get_store(uid)
    store.append_chat(
        "user",
        "chat",
        {
            "v": 1,
            "id": msg_id,
            "body_ct": "encrypted-user-body",
            "nonce": "user-nonce",
            "K_user": "wrapped-user-key",
            "visibility": "shared",
            "owner_user_id": uid,
        },
        strict=True,
    )
    seq = db.chat_seq_for_msg_id(uid, msg_id)
    assert seq is not None
    return seq


def _fake_failure_envelope(store, plaintext: bytes, *, item_id: str | None = None):
    return {
        "v": 1,
        "id": item_id or "failure-envelope",
        "body_ct": "encrypted-failure-body",
        "nonce": "failure-nonce",
        "K_user": "wrapped-failure-key",
        "K_enclave": "wrapped-enclave-key",
        "visibility": "shared",
        "owner_user_id": store.user_id,
        "enclave_pk_fpr": "test",
    }, ""


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table(monkeypatch):
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
    # Legacy lifecycle assertions in this module intentionally exercise the
    # opt-in review lane. Production defaults remain fail-closed/off.
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED", "1")
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE", "64")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
        conn.execute("DELETE FROM v2_runtime_state")
    yield


def test_enqueue_returns_job_id_and_not_coalesced_first_time():
    seed_user("u_js_1")
    _reset("u_js_1")
    job_id, coalesced = jobs_store.enqueue_job("u_js_1", "chat", reason="hi")
    assert isinstance(job_id, int) and job_id > 0
    assert coalesced is False


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "nope"])
def test_positive_job_ttl_settings_fail_closed(monkeypatch, raw):
    monkeypatch.setenv("TEST_V2_POSITIVE_FLOAT", raw)
    with pytest.raises(RuntimeError, match="finite and > 0"):
        jobs_store._positive_float_env("TEST_V2_POSITIVE_FLOAT", "1")


def test_enqueue_same_user_lane_coalesces_to_existing_pending():
    seed_user("u_js_2")
    _reset("u_js_2")
    first_id, first_c = jobs_store.enqueue_job("u_js_2", "chat")
    second_id, second_c = jobs_store.enqueue_job("u_js_2", "chat")
    assert second_id == first_id
    assert first_c is False and second_c is True


def test_enqueue_replaces_overdue_pending_row_instead_of_coalescing():
    seed_user("u_js_stale_pending")
    _reset("u_js_stale_pending")
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
            "SELECT id,lane,status,last_error FROM agent_jobs "
            "WHERE user_id=%s ORDER BY id",
            ("u_js_stale_pending",),
        ).fetchall()
        marker = conn.execute(
            "SELECT error_code FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (old_id,),
        ).fetchone()
    assert len(rows) == 3
    assert rows[0] == (old_id, "chat", "expired", "queue_timeout")
    assert rows[1][1:] == ("trajectory_review", "pending", None)
    assert rows[2] == (new_id, "chat", "pending", None)
    assert marker == ("queue_timeout",)


def test_enqueue_replaces_expired_active_lease_and_fences_old_owner():
    seed_user("u_js_stale_active")
    _reset("u_js_stale_active")
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
            "SELECT id,lane,status,last_error FROM agent_jobs "
            "WHERE user_id=%s ORDER BY id",
            ("u_js_stale_active",),
        ).fetchall()
    assert len(rows) == 3
    assert rows[0] == (old_id, "chat", "expired", "lease_timeout")
    assert rows[1][1:] == ("trajectory_review", "pending", None)
    assert rows[2] == (new_id, "chat", "pending", None)


def test_chat_enqueue_sets_pending_deadline_and_coalesce_advances_generation():
    seed_user("u_js_deadline")
    _reset("u_js_deadline")
    job_id, _ = jobs_store.enqueue_job("u_js_deadline", "chat")
    with db.get_pool().connection() as conn:
        before = conn.execute(
            "SELECT queue_deadline_at,deadline_at,input_generation "
            "FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert before[0] is not None
    assert (
        before[1] is None
    )  # old workers mint their own full execution deadline at claim
    assert before[2] == 0

    same_id, coalesced = jobs_store.enqueue_job("u_js_deadline", "chat")
    with db.get_pool().connection() as conn:
        after = conn.execute(
            "SELECT queue_deadline_at,deadline_at,input_generation "
            "FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert (same_id, coalesced) == (job_id, True)
    assert (
        after[0] == before[0]
    )  # coalescing must not postpone the oldest input forever
    assert after[1] is None
    assert after[2] == 1


def test_enqueue_rejects_unknown_lane():
    seed_user("u_js_2b")
    _reset("u_js_2b")
    with pytest.raises(ValueError):
        jobs_store.enqueue_job("u_js_2b", "not_a_lane")


def test_claim_moves_pending_to_claimed_and_returns_row():
    seed_user("u_js_3")
    _reset("u_js_3")
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
    seed_user("u_js_3b")
    _reset("u_js_3b")
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
    seed_user("u_js_4")
    _reset("u_js_4")
    jobs_store.enqueue_job("u_js_4", "chat")
    first = jobs_store.claim_next_job("w1")
    second = jobs_store.claim_next_job("w2")
    assert first is not None
    assert second is None


def test_lifecycle_running_completed_frees_singleflight_slot():
    seed_user("u_js_5")
    _reset("u_js_5")
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
    seed_user("u_js_6")
    _reset("u_js_6")
    job_id, _ = jobs_store.enqueue_job("u_js_6", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_failed(job_id, "boom", claimed_by="w")
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, attempt_count, last_error FROM agent_jobs WHERE id=%s",
            (job_id,),
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

    assert (
        jobs_store.mark_failed(job_id, "turn_failed:runtimeerror", claimed_by="w")
        is True
    )

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
        "reply_delivered": 1,
    }
    assert second == {
        "examined": 0,
        "status_delivered": 0,
        "runtime_error_delivered": 0,
        "reply_delivered": 0,
    }
    assert recorded == [(uid, "turn_failed:runtimeerror")]
    errors = [
        event
        for event in jobs_store.list_status_events(uid, after_id=0)
        if event["kind"] == "error" and event["job_id"] == job_id
    ]
    assert len(errors) == 1


def test_terminal_failure_reply_is_encrypted_linked_classified_and_idempotent(
    monkeypatch,
):
    uid = "u_js_terminal_reply"
    seed_user(uid)
    _reset(uid)
    parent_seq = _append_user_message(uid)
    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        _fake_failure_envelope,
    )
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w")
    assert jobs_store.mark_failed(
        job_id,
        "turn_failed:providererror",
        claimed_by="w",
        error_class="quota_insufficient",
    )

    first = jobs_store.reconcile_terminal_failure_outbox(job_id=job_id)
    second = jobs_store.reconcile_terminal_failure_outbox(job_id=job_id)

    assert first["reply_delivered"] == 1
    assert second["reply_delivered"] == 0
    messages = db.chat_load_strict(uid)
    failures = [
        row for row in messages
        if str(row.get("terminal_failure_job_id") or "") == str(job_id)
    ]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["body_ct"] == "encrypted-failure-body"
    assert failure["reply_to_message_id"] == "parent-user"
    assert failure["turn_failure_error_class"] == "quota_insufficient"
    assert failure["turn_failure_blame"] == "user_provider"
    assert "额度不足" in failure["turn_failure_user_text"]
    assert "额度不足" not in failure["body_ct"]

    parent = db.chat_get_strict(uid, "parent-user")
    assert parent["reply_status"] == "replied"
    assert parent["reply_message_id"] == failure["id"]
    assert parent["reply_error_class"] == "quota_insufficient"
    assert v2_cursor.load_seq(core_store.get_store(uid)) == parent_seq


def test_terminal_vision_required_reply_uses_user_archive_language(monkeypatch):
    uid = "u_js_terminal_vision_en"
    seed_user(uid, archive_language="en-US")
    _reset(uid)
    _seed_active_route(uid)
    _append_user_message(uid)
    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        _fake_failure_envelope,
    )
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w")
    assert jobs_store.mark_failed(
        job_id,
        "turn_failed:providererror",
        claimed_by="w",
        error_class="vision_model_required",
    )

    result = jobs_store.reconcile_terminal_failure_outbox(job_id=job_id)

    assert result["reply_delivered"] == 1
    failure = next(
        row for row in db.chat_load_strict(uid)
        if str(row.get("terminal_failure_job_id") or "") == str(job_id)
    )
    assert failure["turn_failure_error_class"] == "vision_model_required"
    assert failure["turn_failure_user_text"] == (
        "Your current model can't process images, so it didn't receive this "
        "picture. Switch models, or add a dedicated vision model in Settings."
    )
    with db.get_pool().connection() as conn:
        learned = conn.execute(
            "SELECT vision_test_status,last_vision_test_error,"
            "last_vision_test_at IS NOT NULL "
            "FROM model_api_routes WHERE user_id=%s AND is_active",
            (uid,),
        ).fetchone()
    assert learned == ("unsupported", "vision_model_required", True)


def test_terminal_failure_reply_retry_adopts_committed_bubble_after_ack_crash(
    monkeypatch,
):
    uid = "u_js_terminal_reply_ack_crash"
    seed_user(uid)
    _reset(uid)
    _append_user_message(uid)
    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        _fake_failure_envelope,
    )
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w")
    assert jobs_store.mark_failed(job_id, "lease_timeout", claimed_by="w")

    real_ack = jobs_store._ack_terminal_failure_reply
    monkeypatch.setattr(
        jobs_store,
        "_ack_terminal_failure_reply",
        lambda _job_id: False,
    )
    first = jobs_store.reconcile_terminal_failure_outbox(job_id=job_id)
    monkeypatch.setattr(jobs_store, "_ack_terminal_failure_reply", real_ack)
    second = jobs_store.reconcile_terminal_failure_outbox(job_id=job_id)

    assert first["reply_delivered"] == 0
    assert second["reply_delivered"] == 1
    messages = db.chat_load_strict(uid)
    assert sum(
        str(row.get("terminal_failure_job_id") or "") == str(job_id)
        for row in messages
    ) == 1


def test_terminal_failure_reply_is_suppressed_after_newer_cursor_success(
    monkeypatch,
):
    uid = "u_js_terminal_reply_stale"
    seed_user(uid)
    _reset(uid)
    parent_seq = _append_user_message(uid)
    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        _fake_failure_envelope,
    )
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w")
    assert jobs_store.mark_failed(job_id, "lease_timeout", claimed_by="w")

    store = core_store.get_store(uid)
    success = store._build_chat_message(
        "openclaw",
        "model_api",
        {
            "v": 1,
            "id": "newer-success",
            "body_ct": "encrypted-success",
            "nonce": "success-nonce",
            "K_user": "success-key",
            "visibility": "shared",
            "owner_user_id": uid,
        },
    )
    db.chat_append_effect_with_cursor(
        uid,
        "newer-success",
        float(success["ts"]),
        success,
        core_store.MAX_CHAT_MESSAGES,
        parent_seq,
    )

    result = jobs_store.reconcile_terminal_failure_outbox(job_id=job_id)

    assert result["reply_delivered"] == 1
    assert not any(
        row.get("terminal_failure_job_id")
        for row in db.chat_load_strict(uid)
    )


def test_terminal_visibility_retries_each_fail_once_sink_without_duplicates(
    monkeypatch,
):
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
        jobs_store, "_deliver_terminal_failure_status", _status_fails_once
    )

    base_now = time.time()
    first = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=_runtime_fails_once, job_id=job_id, now=base_now
    )
    second = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=_runtime_fails_once, job_id=job_id, now=base_now + 2
    )
    third = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=_runtime_fails_once, job_id=job_id, now=base_now + 4
    )

    assert first["status_delivered"] == 0
    assert first["runtime_error_delivered"] == 0
    assert second["status_delivered"] == 1
    assert second["runtime_error_delivered"] == 1
    assert third["examined"] == 0
    assert status_attempts["n"] == 2
    assert runtime_attempts["n"] == 2
    assert recorded == [(uid, "provider_unavailable")]
    errors = [
        event
        for event in jobs_store.list_status_events(uid, after_id=0)
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
        job_id, "raw provider secret sk-do-not-leak", claimed_by="w"
    )

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
        jobs_store, "_deliver_terminal_failure_status", _poison_old_status
    )
    base_now = time.time()
    jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=_poison_old_runtime, limit=1, now=base_now
    )
    second = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=_poison_old_runtime, limit=1, now=base_now + 2
    )

    assert second["status_delivered"] == 1
    assert second["runtime_error_delivered"] == 1
    assert delivered_runtime == [(uid, "lease_timeout")]
    errors = [
        event
        for event in jobs_store.list_status_events(uid, after_id=0)
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
        failed_id, "provider_unavailable", claimed_by="w-fail"
    )

    real_runtime_sink = jobs_store._deliver_terminal_failure_runtime_error
    attempts = {"n": 0}

    def _runtime_fails_once(job_id):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient route write")
        return real_runtime_sink(job_id)

    monkeypatch.setattr(
        jobs_store, "_deliver_terminal_failure_runtime_error", _runtime_fails_once
    )
    base_now = time.time()
    jobs_store.reconcile_terminal_failure_outbox(job_id=failed_id, now=base_now)

    success_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w-success")
    assert jobs_store.mark_running(success_id, claimed_by="w-success")
    completed, _successor = jobs_store.finish_chat_job(
        success_id, claimed_by="w-success", observed_generation=0
    )
    assert completed

    retried = jobs_store.reconcile_terminal_failure_outbox(
        job_id=failed_id, now=base_now + 2
    )
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
    assert jobs_store.mark_failed(
        failed_id,
        "turn_failed:providererror",
        claimed_by="w",
        error_class="vision_model_required",
    )

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
        jobs_store, "_deliver_terminal_failure_runtime_error", real_runtime_sink
    )
    jobs_store.reconcile_terminal_failure_outbox(job_id=failed_id, now=base_now + 2)
    with db.get_pool().connection() as conn:
        errors = conn.execute(
            "SELECT id::text,last_runtime_error,vision_test_status "
            "FROM model_api_routes "
            "WHERE id IN (%s,%s) ORDER BY id",
            (old_route_id, new_route_id),
        ).fetchall()
    assert all(
        error == "" and vision_status == "untested"
        for _route_id, error, vision_status in errors
    )


def test_enqueue_after_failed_job_also_coalesces_free(monkeypatch=None):
    """Partial-index crux: a job in a TERMINAL status ('failed') must not block
    a fresh enqueue for the same (user, lane) — only 'pending'/'claimed'/'running'
    rows are covered by ux_agent_jobs_singleflight. A full (non-partial) unique
    index would wrongly reject/coalesce this new INSERT.
    """
    seed_user("u_js_7")
    _reset("u_js_7")
    job_id, _ = jobs_store.enqueue_job("u_js_7", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_failed(job_id, "boom", claimed_by="w")
    new_id, coalesced = jobs_store.enqueue_job("u_js_7", "chat")
    assert new_id != job_id
    assert coalesced is False


def test_reap_expires_stuck_claimed_job_by_deadline():
    seed_user("u_js_7b")
    _reset("u_js_7b")
    job_id, _ = jobs_store.enqueue_job("u_js_7b", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id, claimed_by="w")
    # reap with a "now" far in the future → deadline is in the past relative to it.
    import time

    reaped = jobs_store.reap_stuck_jobs(
        now=time.time() + jobs_store.RUNNING_TTL_SEC + 10
    )
    assert reaped == 1
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        marker = conn.execute(
            "SELECT error_code FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert row[0] == "expired"
    assert marker == ("lease_timeout",)


def test_reap_leaves_fresh_running_job_alone():
    seed_user("u_js_8")
    _reset("u_js_8")
    job_id, _ = jobs_store.enqueue_job("u_js_8", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id, claimed_by="w")
    reaped = jobs_store.reap_stuck_jobs()  # now=None → now(); deadline is in the future
    assert reaped == 0


def test_reap_expires_overdue_pending_chat_job():
    seed_user("u_js_pending_timeout")
    _reset("u_js_pending_timeout")
    job_id, _ = jobs_store.enqueue_job("u_js_pending_timeout", "chat")
    reaped = jobs_store.reap_stuck_job_rows(
        now=time.time() + jobs_store.PENDING_CHAT_TTL_SEC + 10
    )
    assert [(row["id"], row["last_error"]) for row in reaped] == [
        (job_id, "queue_timeout")
    ]


def test_reap_expires_legacy_pending_chat_without_queue_deadline():
    seed_user("u_js_legacy_pending")
    _reset("u_js_legacy_pending")
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
    seed_user("u_js_legacy_active")
    _reset("u_js_legacy_active")
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
    seed_user("u_js_successor")
    _reset("u_js_successor")
    job_id, _ = jobs_store.enqueue_job("u_js_successor", "chat")
    jobs_store.claim_next_job("owner-a")
    assert jobs_store.mark_running(job_id, claimed_by="owner-a") is True
    assert jobs_store.mark_completed(job_id, claimed_by="owner-b") is False
    assert (
        jobs_store.get_job_status(
            job_id,
            user_id="u_js_successor",
            claimed_by="owner-a",
        )
        == "running"
    )
    assert (
        jobs_store.get_job_status(
            job_id,
            user_id="u_js_successor",
            claimed_by="owner-b",
        )
        is None
    )

    observed = jobs_store.get_input_generation(job_id, claimed_by="owner-a")
    assert observed == 0
    same_id, coalesced = jobs_store.enqueue_job("u_js_successor", "chat")
    assert (same_id, coalesced) == (job_id, True)

    completed, successor_id = jobs_store.finish_chat_job(
        job_id, claimed_by="owner-a", observed_generation=observed
    )
    assert completed is True
    assert successor_id is not None
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status FROM agent_jobs WHERE user_id=%s ORDER BY id",
            ("u_js_successor",),
        ).fetchall()
    assert rows == [(job_id, "completed"), (successor_id, "pending")]
    assert (
        jobs_store.get_job_status(
            job_id,
            user_id="u_js_successor",
            claimed_by="owner-a",
        )
        == "completed"
    )


def test_forced_successor_is_generation_pinned_singleflight_and_preserves_error():
    uid = "u_js_forced_successor"
    seed_user(uid)
    _reset(uid)
    _seed_active_route(uid, error="previous_runtime_failure")
    generation = db.get_runtime_generation(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat", expected_generation=generation)
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
    state,
    generation,
):
    uid = f"u_js_forced_successor_fenced_{state}_{generation}"
    seed_user(uid)
    _reset(uid)
    original_generation = db.get_runtime_generation(uid)
    job_id, _ = jobs_store.enqueue_job(
        uid, "chat", expected_generation=original_generation
    )
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
    job_id, _ = jobs_store.enqueue_job(uid, "chat", expected_generation=generation)
    claimed = jobs_store.claim_next_job("expiry-owner")
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by="expiry-owner")
    # Lease must outlast the finisher's park latency (~0.5s) so the finisher's
    # scan begins while the lease is still valid — that pre-expiry scan is what
    # a buggy inline `lease > clock_timestamp()` predicate would sample, and is
    # the whole premise that lets this test distinguish it from the correct
    # post-lock re-check. 4s gives comfortable margin; the row is otherwise left
    # UNCHANGED so real wall-clock time (not a row update) drives expiry — an
    # update would trigger EvalPlanQual re-evaluation and mask the bug.
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET lease_expires_at="
            "clock_timestamp() + interval '4 second' WHERE id=%s",
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

                # Prove the finisher parked on the held job-row lock before the
                # lease expired; this makes the regression distinguish
                # transaction-start now() from clock_timestamp().
                #
                # Detect via pg_blocking_pids: a backend is "waiting" iff this
                # blocker's backend is in its blocking set. This is robust to
                # query-text drift and to which statement snapshot pg_stat_activity
                # happens to show — it reads the server's lock wait graph directly,
                # and is scoped to THIS blocker so a full-suite run's unrelated
                # lock waits can't false-positive it. Poll gently (100ms): a tight
                # busy-loop in this process starves the finisher thread of the GIL
                # and prevents it from ever issuing its blocking statement (the
                # original 10ms loop was the observer effect that made this flaky).
                deadline = time.monotonic() + 15
                waiting = False
                while time.monotonic() < deadline:
                    waiting = bool(blocker.execute(
                        "SELECT EXISTS ("
                        " SELECT 1 FROM pg_stat_activity "
                        " WHERE pid <> pg_backend_pid() "
                        " AND pg_backend_pid() = ANY(pg_blocking_pids(pid))"
                        ")"
                    ).fetchone()[0])
                    if waiting:
                        break
                    time.sleep(0.1)
                assert waiting, "finisher never reached the blocked lease check"
                remaining = float(blocker.execute(
                    "SELECT EXTRACT(EPOCH FROM "
                    "(lease_expires_at-clock_timestamp())) "
                    "FROM agent_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()[0])
                # The finisher parked while the lease was still valid (its scan
                # ran pre-expiry) — the precondition for discrimination.
                assert remaining > 0.1, (
                    "finisher parked too late; lease already expired at scan time"
                )
                time.sleep(remaining + 0.1)
        result = future.result(timeout=10)

    assert result == (False, None)
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status FROM agent_jobs WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
    assert rows == [(job_id, "running")]


def test_claim_serializes_all_lanes_per_user():
    seed_user("u_js_lane_lock")
    _reset("u_js_lane_lock")
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
    monkeypatch.setattr(
        wake_bus, "notify", lambda channel, user_id="": calls.append((channel, user_id))
    )
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


def test_finish_wake_job_persists_glance_before_successor_handoff():
    """Completion, state merge, and late-input successor share one commit."""
    uid = "u_js_glance_successor_atomic"
    seed_user(uid)
    _reset(uid)
    jobs_store.upsert_runtime_state(uid, {"preserved": "state"})
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w-glance")
    assert job is not None and job["id"] == job_id
    assert jobs_store.mark_running(job_id, claimed_by="w-glance")
    same_id, coalesced = jobs_store.enqueue_job(uid, "heartbeat")
    assert (same_id, coalesced) == (job_id, True)

    completed, successor_id = jobs_store.finish_wake_job(
        job_id,
        claimed_by="w-glance",
        observed_generation=0,
        context_stream="v2_perception_wake_context",
        consumed_context_seq=0,
        completed_perception_glance_fingerprint="b" * 64,
    )

    assert completed is True
    assert successor_id is not None
    assert jobs_store.get_runtime_state(uid) == {
        "preserved": "state",
        "last_completed_perception_glance_fingerprint": "b" * 64,
        "last_completed_perception_glance_source_job_id": job_id,
    }
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT status FROM agent_jobs WHERE id=%s", (successor_id,)
        ).fetchone()[0] == "pending"


def test_completed_wake_retry_cannot_overwrite_newer_glance_source():
    """An exact-source retry is idempotent and ordered by source job id."""
    uid = "u_js_glance_source_order"
    seed_user(uid)
    _reset(uid)

    old_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    old_job = jobs_store.claim_next_job("w-old")
    assert old_job is not None and old_job["id"] == old_id
    assert jobs_store.mark_running(old_id, claimed_by="w-old")
    assert jobs_store.finish_wake_job(
        old_id,
        claimed_by="w-old",
        observed_generation=0,
        context_stream="v2_perception_wake_context",
        consumed_context_seq=0,
        completed_perception_glance_fingerprint="1" * 64,
    ) == (True, None)

    new_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    new_job = jobs_store.claim_next_job("w-new")
    assert new_job is not None and new_job["id"] == new_id
    assert jobs_store.mark_running(new_id, claimed_by="w-new")
    assert jobs_store.finish_wake_job(
        new_id,
        claimed_by="w-new",
        observed_generation=0,
        context_stream="v2_perception_wake_context",
        consumed_context_seq=0,
        completed_perception_glance_fingerprint="2" * 64,
    ) == (True, None)

    assert jobs_store.finish_wake_job(
        old_id,
        claimed_by="w-old",
        observed_generation=0,
        context_stream="v2_perception_wake_context",
        consumed_context_seq=0,
        completed_perception_glance_fingerprint="1" * 64,
    ) == (True, None)
    assert jobs_store.get_runtime_state(uid) == {
        "last_completed_perception_glance_fingerprint": "2" * 64,
        "last_completed_perception_glance_source_job_id": new_id,
    }


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
    seed_user("u_js_11")
    _reset("u_js_11")
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
    jobs_store.record_worker_heartbeat("w1")  # default kind='turn'
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
        "v2-worker-new-deadbeef1234:genesis", kind="genesis", capacity=0
    )
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
    assert jobs_store.recent_worker_heartbeat_count(within_sec=300) == 2
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
    job_id, _ = jobs_store.enqueue_job(uid, "chat", expected_generation=generation)

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
    assert jobs_store.mark_running(job_id, claimed_by="w-running-aba") is True
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET runtime_generation=3 "
            "WHERE user_id=%s AND hosted_runtime_state='v2'",
            (uid,),
        )

    assert jobs_store.renew_job_lease(job_id, "w-running-aba") is False


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


# --- backlog scanner --------------------------------------------------------
#
# Every maintenance enqueue point hangs off a turn (post-reply, degraded
# coverage, self-chain, CAS retry). A user who stops talking therefore stops
# folding, and a large backlog cut over to V2 has nobody to kick off the first
# fold. This scanner is the only path that does not need the user to speak.


def _scan_rows(uid: str, count: int, *, source: str = "chat", start_ts: float = 1000.0):
    with db.get_pool().connection() as conn:
        for index in range(count):
            conn.execute(
                "INSERT INTO chat_messages (user_id, msg_id, ts, doc) "
                "VALUES (%s,%s,%s,%s::jsonb)",
                (
                    uid,
                    f"{uid}-scan-{index}-{uuid.uuid4().hex[:8]}",
                    start_ts + index,
                    '{"source":"%s","role":"user","body_ct":"x"}' % source,
                ),
            )


def _set_watermark(uid: str, watermark_seq: int):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_conversation_summary (user_id, watermark_seq) "
            "VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET watermark_seq=%s",
            (uid, watermark_seq, watermark_seq),
        )


def _set_runtime(uid: str, state: str):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) VALUES (%s,%s,1) "
            "ON CONFLICT (user_id) DO UPDATE SET hosted_runtime_state=%s",
            (uid, state, state),
        )


def test_backlog_scanner_finds_a_v2_user_who_stopped_talking():
    uid = f"usr_scan_idle_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    _reset(uid)
    _scan_rows(uid, 12)

    due = dict(jobs_store.due_compaction_users(min_backlog=10, limit=50))

    assert uid in due
    assert due[uid] >= 10


def test_backlog_scanner_ignores_a_user_below_the_threshold():
    uid = f"usr_scan_small_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    _reset(uid)
    _scan_rows(uid, 3)

    due = dict(jobs_store.due_compaction_users(min_backlog=10, limit=50))

    assert uid not in due


def test_backlog_scanner_never_touches_a_user_rolled_back_to_v1():
    """Enqueueing maintenance for a resident user would resurrect V2 work."""
    uid = f"usr_scan_resident_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    _reset(uid)
    _scan_rows(uid, 30)
    _set_runtime(uid, "resident")

    due = dict(jobs_store.due_compaction_users(min_backlog=10, limit=50))

    assert uid not in due


def test_backlog_scanner_excludes_gc_able_synthetic_rows():
    """Same exclusion set as the fold and both frontier witnesses.

    Counting a verify_ping as backlog would schedule a fold over a row that
    verify_loop is about to delete — the permanent frontier corruption this
    exclusion exists to prevent.
    """
    uid = f"usr_scan_synth_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    _reset(uid)
    _scan_rows(uid, 30, source="verify_ping")

    due = dict(jobs_store.due_compaction_users(min_backlog=10, limit=50))

    assert uid not in due


def test_backlog_scanner_counts_only_rows_past_the_watermark():
    uid = f"usr_scan_watermark_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    _reset(uid)
    _scan_rows(uid, 20)
    with db.get_pool().connection() as conn:
        top = conn.execute(
            "SELECT max(seq) FROM chat_messages WHERE user_id=%s", (uid,)
        ).fetchone()[0]
    _set_watermark(uid, int(top))

    due = dict(jobs_store.due_compaction_users(min_backlog=1, limit=50))

    assert uid not in due


def test_backlog_scanner_skips_a_user_already_being_folded():
    """A pending maintenance job means the work is already scheduled."""
    uid = f"usr_scan_busy_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    _reset(uid)
    _scan_rows(uid, 30)
    jobs_store.enqueue_job(uid, "maintenance", reason="already_queued")

    due = dict(jobs_store.due_compaction_users(min_backlog=10, limit=50))

    assert uid not in due


def test_backlog_scanner_returns_the_worst_backlog_first():
    small = f"usr_scan_rank_small_{uuid.uuid4().hex[:8]}"
    large = f"usr_scan_rank_large_{uuid.uuid4().hex[:8]}"
    for uid, count in ((small, 12), (large, 40)):
        seed_user(uid)
        _reset(uid)
        _scan_rows(uid, count)

    ordered = [
        uid
        for uid, _ in jobs_store.due_compaction_users(min_backlog=10, limit=50)
        if uid in {small, large}
    ]

    assert ordered == [large, small]


# --- persisted effective batch cap ------------------------------------------
#
# Both folds shrink their batch on refusal, but the shrunk value used to be a
# local: the next job started at the full batch again and burned one
# guaranteed-to-fail model call rediscovering the same limit.


def test_effective_batch_cap_is_unset_for_a_new_conversation():
    uid = f"usr_cap_new_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    assert db.v2_effective_batch_cap(uid) is None


def test_effective_batch_cap_round_trips():
    uid = f"usr_cap_rt_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    _set_watermark(uid, 0)  # the row a first fold would have written
    db.v2_set_effective_batch_cap(uid, 12)
    assert db.v2_effective_batch_cap(uid) == 12
    db.v2_set_effective_batch_cap(uid, 37)
    assert db.v2_effective_batch_cap(uid) == 37


def test_writing_the_cap_never_fabricates_a_summary_row():
    """Inserting here would wedge the very fold this is meant to help.

    A fabricated row carries version=0. The fold reads "no summary", computes
    its write against that absence, and its CAS then collides with the row the
    bookkeeping invented — failing the whole job with summary_cas_lost. So a
    conversation with no summary keeps no memory until its first fold lands.
    """
    uid = f"usr_cap_nosummary_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))

    db.v2_set_effective_batch_cap(uid, 8)

    assert db.v2_effective_batch_cap(uid) is None
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT count(*) FROM v2_conversation_summary WHERE user_id=%s", (uid,)
        ).fetchone()[0]
    assert rows == 0, "bookkeeping must not create a summary row"


def test_effective_batch_cap_never_persists_a_useless_value():
    """Zero or negative would wedge the fold at an empty batch forever."""
    uid = f"usr_cap_floor_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    _set_watermark(uid, 0)
    db.v2_set_effective_batch_cap(uid, 0)
    assert db.v2_effective_batch_cap(uid) == 1
    db.v2_set_effective_batch_cap(uid, -5)
    assert db.v2_effective_batch_cap(uid) == 1


def test_writing_the_cap_does_not_disturb_the_watermark():
    """The cap is bookkeeping; it must never touch fold coverage or its CAS."""
    uid = f"usr_cap_isolation_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_conversation_summary (user_id, watermark_seq, version) "
            "VALUES (%s, 4242, 7) ON CONFLICT (user_id) DO UPDATE "
            "SET watermark_seq=4242, version=7",
            (uid,),
        )
    db.v2_set_effective_batch_cap(uid, 6)
    with db.get_pool().connection() as conn:
        watermark, version = conn.execute(
            "SELECT watermark_seq, version FROM v2_conversation_summary "
            "WHERE user_id=%s",
            (uid,),
        ).fetchone()
    assert (watermark, version) == (4242, 7)
