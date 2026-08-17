"""jobs_store：single-flight coalesce、SKIP LOCKED 独占 claim、job 生命周期。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
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
        conn.execute("DELETE FROM v2_wake_shadow_decisions")
        conn.execute("DELETE FROM agent_jobs")
        conn.execute("DELETE FROM v2_runtime_state")
    yield


def test_enqueue_returns_job_id_and_not_coalesced_first_time():
    seed_user("u_js_1")
    _reset("u_js_1")
    job_id, coalesced = jobs_store.enqueue_job("u_js_1", "chat", reason="hi")
    assert isinstance(job_id, int) and job_id > 0
    assert coalesced is False


def test_wake_shadow_observations_are_idempotent_and_reportable():
    uid = "u_wake_shadow_report"
    seed_user(uid)
    _reset(uid)
    heartbeat_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    scheduled_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    screen_id, _ = jobs_store.enqueue_job(uid, "screen_watch")
    day = date(2026, 8, 14)
    decided_at = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)

    assert jobs_store.record_wake_shadow_decision(
        job_id=heartbeat_id,
        local_day=day,
        local_hour=23,
        local_minute=8,
        lane="heartbeat",
        decision_allowed=True,
        apns_alert_sent=True,
        decided_at=decided_at,
    ) is True
    assert jobs_store.record_wake_shadow_decision(
        job_id=scheduled_id,
        local_day=day,
        local_hour=2,
        local_minute=40,
        lane="scheduled",
        decision_allowed=True,
        apns_alert_sent=False,
        decided_at=decided_at,
    ) is True
    assert jobs_store.record_wake_shadow_decision(
        job_id=screen_id,
        local_day=day,
        local_hour=12,
        local_minute=0,
        lane="screen_watch",
        decision_allowed=False,
        apns_alert_sent=False,
        decided_at=decided_at,
    ) is True
    # Same source job cannot inflate the report if the best-effort observer is
    # replayed after a worker retry.
    assert jobs_store.record_wake_shadow_decision(
        job_id=heartbeat_id,
        local_day=day,
        local_hour=9,
        local_minute=9,
        lane="heartbeat",
        decision_allowed=False,
        apns_alert_sent=False,
        decided_at=decided_at,
    ) is False

    # Queue cleanup must not erase the observation window. job_id is only the
    # stable idempotency key in this table, not a foreign-key lifecycle tie.
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE id=%s", (heartbeat_id,))

    report = jobs_store.wake_shadow_report(
        days=2,
        bucket_start_hour=23,
        bucket_end_hour=7,
        through_day=day,
    )

    assert report["bucket"] == {
        "start_hour_inclusive": 23,
        "end_hour_exclusive": 7,
        "crosses_midnight": True,
        "purpose": "observation_only_not_product_policy",
    }
    assert {
        key: report[key]
        for key in (
            "total_decisions",
            "allowed",
            "suppressed",
            "apns_alert_sent",
            "bucket_allowed",
            "bucket_allowed_apns_alert_sent",
        )
    } == {
        "total_decisions": 3,
        "allowed": 2,
        "suppressed": 1,
        "apns_alert_sent": 1,
        "bucket_allowed": 2,
        "bucket_allowed_apns_alert_sent": 1,
    }
    assert report["by_lane"]["heartbeat"]["total_decisions"] == 1
    assert report["by_lane"]["scheduled"]["bucket_allowed"] == 1
    assert report["by_lane"]["screen_watch"]["suppressed"] == 1


def test_wake_shadow_prunes_its_own_90_day_retention_window():
    uid = "u_wake_shadow_retention"
    seed_user(uid)
    _reset(uid)
    old_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    fresh_id, _ = jobs_store.enqueue_job(uid, "scheduled")

    assert jobs_store.record_wake_shadow_decision(
        job_id=old_id,
        local_day=date.today(),
        local_hour=1,
        local_minute=0,
        lane="heartbeat",
        decision_allowed=False,
        apns_alert_sent=False,
        decided_at=time.time(),
    ) is True
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_wake_shadow_decisions "
            "SET recorded_at=now() - interval '91 days' WHERE job_id=%s",
            (old_id,),
        )

    assert jobs_store.record_wake_shadow_decision(
        job_id=fresh_id,
        local_day=date.today(),
        local_hour=2,
        local_minute=0,
        lane="scheduled",
        decision_allowed=True,
        apns_alert_sent=False,
        decided_at=time.time(),
    ) is True
    with db.get_pool().connection() as conn:
        ids = {
            int(row[0])
            for row in conn.execute(
                "SELECT job_id FROM v2_wake_shadow_decisions"
            ).fetchall()
        }
    assert ids == {fresh_id}


def test_wake_shadow_rejects_impossible_apns_alert_observation():
    with pytest.raises(ValueError, match="suppressed wake cannot send an APNs alert"):
        jobs_store.record_wake_shadow_decision(
            job_id=1,
            local_day="2026-08-14",
            local_hour=1,
            local_minute=2,
            lane="heartbeat",
            decision_allowed=False,
            apns_alert_sent=True,
            decided_at=0.0,
        )


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


def test_completed_wake_persists_auditable_sleep_reason():
    uid = "u_js_stay_silent"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id, claimed_by="w")

    assert jobs_store.mark_completed(
        job_id,
        claimed_by="w",
        wake_result="sleep",
        wake_result_reason="刚刚已经主动联系过",
    )
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status,wake_result,wake_result_reason FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert row == ("completed", "sleep", "刚刚已经主动联系过")
    activity = jobs_store.wake_lane_activity_for_user(uid)
    assert activity["recent_silences"] == [{
        "job_id": job_id,
        "lane": "manual_wake",
        "wake_result": "sleep",
        "reason": "刚刚已经主动联系过",
        "finished_at": activity["recent_silences"][0]["finished_at"],
    }]


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


def test_scheduled_failure_reply_is_standalone_visible_and_idempotent(monkeypatch):
    uid = "u_js_scheduled_terminal_reply"
    seed_user(uid)
    _reset(uid)
    _seed_active_route(uid)
    _append_user_message(uid)
    cursor_before = v2_cursor.load_seq(core_store.get_store(uid))
    encrypted_plaintexts: list[str] = []

    def capture_failure_envelope(store, plaintext, *, item_id=None):
        encrypted_plaintexts.append(plaintext.decode("utf-8"))
        return _fake_failure_envelope(store, plaintext, item_id=item_id)

    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        capture_failure_envelope,
    )
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    db.log_append(
        uid,
        jobs_store.SCHEDULED_WAKE_STREAM,
        {
            "status": "fired",
            "fired_job_id": job_id,
            "note": "提醒我喝水",
            "at": "2026-08-17T09:30:00",
            "timezone": "Asia/Shanghai",
            "due_at": 1_787_110_200.0,
        },
        item_key="timer-water",
    )
    jobs_store.claim_next_job("w")
    assert jobs_store.mark_failed(
        job_id,
        "wake_failed:empty_reply",
        claimed_by="w",
        error_class="provider_empty_reply",
    )

    with db.get_pool().connection() as conn:
        marker = conn.execute(
            "SELECT reply_frontier_seq,reply_parent_message_id "
            "FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert marker == (None, None)

    recorded = []
    first = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=lambda *args: recorded.append(args),
        job_id=job_id,
    )
    second = jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=lambda *args: recorded.append(args),
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
    assert recorded == [], "scheduled failure must not poison the active chat route"
    failures = [
        row for row in db.chat_load_strict(uid)
        if str(row.get("terminal_failure_job_id") or "") == str(job_id)
    ]
    assert len(failures) == 1
    failure = failures[0]
    assert failure.get("reply_to_message_id") in {None, ""}
    assert failure["wake_kind"] == "scheduled"
    assert failure["notice_kind"] == "scheduled_wake_failure"
    assert failure["turn_failure_error_class"] == "provider_empty_reply"
    assert encrypted_plaintexts == [
        "提醒没能送到\n"
        "「提醒我喝水」原定 2026年8月17日 09:30（Asia/Shanghai） 提醒你,"
        "试了几次都没成功。\n"
        "这条提醒不会自动补发,需要的话可以重新设一个。"
    ]
    parent = db.chat_get_strict(uid, "parent-user")
    assert not str(parent.get("reply_message_id") or "")
    assert v2_cursor.load_seq(core_store.get_store(uid)) == cursor_before
    with db.get_pool().connection() as conn:
        route_error = conn.execute(
            "SELECT last_runtime_error FROM model_api_routes "
            "WHERE user_id=%s AND is_active",
            (uid,),
        ).fetchone()[0]
    assert route_error == ""


def test_scheduled_quota_failure_uses_approved_copy_with_original_time():
    text = jobs_store._scheduled_failure_reply_text(
        "quota_insufficient",
        language="zh-CN",
        contexts=[{
            "note": "喝水",
            "at": "2026-08-17T09:30:00",
            "timezone": "Asia/Shanghai",
            "due_at": "",
        }],
    )

    assert text == (
        "提醒没能送到\n"
        "「喝水」原定 2026年8月17日 09:30（Asia/Shanghai） 提醒你,"
        "因为模型服务额度不足没能送出。\n"
        "充值后新的提醒就能正常工作;这一条不会自动补发。"
    )
    for internal_term in ("provider", "Runtime", "job", "retry", "空回复", "定时任务"):
        assert internal_term not in text


def test_terminal_image_generation_configuration_failure_never_uses_slow_fallback(
    monkeypatch,
):
    uid = "u_js_terminal_image_generation"
    seed_user(uid)
    _reset(uid)
    _append_user_message(uid)
    encrypted_plaintexts: list[str] = []

    def capture_failure_envelope(store, plaintext, *, item_id=None):
        encrypted_plaintexts.append(plaintext.decode("utf-8"))
        return _fake_failure_envelope(store, plaintext, item_id=item_id)

    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        capture_failure_envelope,
    )
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    jobs_store.claim_next_job("w")
    assert jobs_store.mark_failed(
        job_id,
        "turn_failed:image_generation_model_required",
        claimed_by="w",
        error_class="image_generation_model_required",
    )

    result = jobs_store.reconcile_terminal_failure_outbox(job_id=job_id)

    assert result["reply_delivered"] == 1
    assert encrypted_plaintexts == [
        "当前模型不能生成图片，请到设置里添加生图模型。"
    ]
    failure = next(
        row for row in db.chat_load_strict(uid)
        if str(row.get("terminal_failure_job_id") or "") == str(job_id)
    )
    assert failure["turn_failure_error_class"] == (
        "image_generation_model_required"
    )
    assert failure["turn_failure_blame"] == "user_provider"
    assert "我这会儿有点慢" not in failure["turn_failure_user_text"]


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
    jobs_store.record_worker_heartbeat("w-fresh-1", pool="foreground")
    jobs_store.record_worker_heartbeat("w-fresh-2", pool="foreground")
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


def test_inflight_job_count_filters_foreground_and_background_lanes():
    foreground = {"chat", "manual_wake"}
    background = {"profile", "dream"}
    before_all = jobs_store.inflight_job_count()
    before_foreground = jobs_store.inflight_job_count(lanes=foreground)
    before_background = jobs_store.inflight_job_count(lanes=background)

    seed_user("u_js_inflight_foreground")
    jobs_store.enqueue_job("u_js_inflight_foreground", "chat", reason="t")
    for index in range(7):
        user_id = f"u_js_inflight_background_{index}"
        lane = "profile" if index % 2 == 0 else "dream"
        seed_user(user_id)
        jobs_store.enqueue_job(user_id, lane, reason="t")

    assert jobs_store.inflight_job_count(lanes=foreground) == before_foreground + 1
    assert jobs_store.inflight_job_count(lanes=background) == before_background + 7
    assert jobs_store.inflight_job_count() == before_all + 8


def test_claim_skips_profile_job_until_available_at():
    uid = "u_delayed_claim"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "profile", reason="retry")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs "
            "SET available_at=clock_timestamp()+interval '1 hour' WHERE id=%s",
            (job_id,),
        )

    assert jobs_store.claim_next_job("heavy-delayed", lanes={"profile"}) is None

    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs "
            "SET available_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job_id,),
        )
    claimed = jobs_store.claim_next_job("heavy-ready", lanes={"profile"})
    assert claimed is not None
    assert int(claimed["id"]) == job_id


def test_future_job_with_missing_runtime_state_is_not_orphan_retired():
    uid = "u_delayed_orphan_probe"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "profile", reason="retry")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs "
            "SET available_at=clock_timestamp()+interval '1 hour' WHERE id=%s",
            (job_id,),
        )
        conn.execute("DELETE FROM v2_runtime_state WHERE user_id=%s", (uid,))

    assert jobs_store.claim_next_job("heavy-orphan", lanes={"profile"}) is None
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
    assert row == ("pending",)


def test_reschedule_owned_job_preserves_exact_profile_singleflight():
    uid = "u_profile_reschedule_owned"
    owner = "heavy-0:g3"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "profile", reason="retry")
    claimed = jobs_store.claim_next_job(owner, lanes={"profile"})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=owner)
    available_at = time.time() + 300

    assert jobs_store.reschedule_owned_job(
        job_id,
        claimed_by=owner,
        error="profile_generation_failed:providererror",
        available_at=available_at,
    )

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status,attempt_count,EXTRACT(EPOCH FROM available_at),"
            "claimed_by,claimed_at,started_at,finished_at,lease_expires_at,"
            "deadline_at FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert row[0:2] == ("pending", 1)
    assert abs(float(row[2]) - available_at) < 1
    assert row[3:] == (None, None, None, None, None, None)

    coalesced_id, coalesced = jobs_store.enqueue_job(
        uid, "profile", reason="postchat_due"
    )
    assert (coalesced_id, coalesced) == (job_id, True)


def test_reschedule_owned_job_rejects_wrong_owner_without_mutation():
    uid = "u_profile_reschedule_wrong_owner"
    owner = "heavy-1:g8"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "profile", reason="retry")
    claimed = jobs_store.claim_next_job(owner, lanes={"profile"})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=owner)
    with db.get_pool().connection() as conn:
        before = conn.execute(
            "SELECT status,attempt_count,available_at,claimed_by,lease_expires_at "
            "FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()

    assert not jobs_store.reschedule_owned_job(
        job_id,
        claimed_by="heavy-1:g9",
        error="profile_generation_failed:providererror",
        available_at=time.time() + 300,
    )

    with db.get_pool().connection() as conn:
        after = conn.execute(
            "SELECT status,attempt_count,available_at,claimed_by,lease_expires_at "
            "FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert after == before


def test_pristine_scheduled_failure_reschedule_rejects_any_mcp_attempt():
    uid = "u_scheduled_failure_mcp_fence"
    owner = "wake:g1"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed = jobs_store.claim_next_job(owner, lanes={"scheduled"})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=owner)
    assert jobs_store.start_mcp_mutation_attempt(
        job_id,
        user_id=uid,
        claimed_by=owner,
        call_id="calendar-write",
        tool_name="mcp__calendar__create",
        input_frontier_seq=0,
    )

    assert not jobs_store.reschedule_pristine_scheduled_failure(
        job_id,
        claimed_by=owner,
        error="scheduled_retry:wake_failed:providererror",
        available_at=time.time() + 30,
        expected_attempt_count=0,
        max_attempts=3,
    )
    assert _job_row(job_id)[0:2] == ("running", 0)


@pytest.mark.parametrize("effect_type", sorted(jobs_store.DURABLE_TOOL_EFFECT_TYPES))
def test_pristine_scheduled_failure_reschedule_rejects_durable_effect(
    effect_type,
):
    from model_api_runtime.v2 import effect_outbox

    uid = f"u_scheduled_failure_effect_{effect_type[:10]}"
    owner = "wake:g2"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed = jobs_store.claim_next_job(owner, lanes={"scheduled"})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=owner)
    effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type=effect_type,
        ordinal=0,
        expected_generation=1,
        payload={"ciphertext": "shell"},
        input_frontier_seq=0,
    )

    assert not jobs_store.reschedule_pristine_scheduled_failure(
        job_id,
        claimed_by=owner,
        error="scheduled_retry:wake_failed:providererror",
        available_at=time.time() + 30,
        expected_attempt_count=0,
        max_attempts=3,
    )
    assert _job_row(job_id)[0:2] == ("running", 0)


def test_make_pending_profile_job_ready_updates_only_delayed_row():
    uid = "u_profile_make_ready"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "profile", reason="retry")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs "
            "SET available_at=clock_timestamp()+interval '1 hour' WHERE id=%s",
            (job_id,),
        )

    assert jobs_store.make_pending_job_ready(uid, lane="profile")
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT available_at <= clock_timestamp() FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert row == (True,)
    assert not jobs_store.make_pending_job_ready(uid, lane="profile")


def test_make_pending_job_ready_rejects_owned_terminal_and_orphan_rows():
    claimed_uid = "u_profile_make_ready_claimed"
    seed_user(claimed_uid)
    _reset(claimed_uid)
    claimed_id, _ = jobs_store.enqueue_job(claimed_uid, "profile", reason="retry")
    claimed = jobs_store.claim_next_job("heavy-ready-owned", lanes={"profile"})
    assert claimed is not None and int(claimed["id"]) == claimed_id
    assert not jobs_store.make_pending_job_ready(claimed_uid, lane="profile")
    assert jobs_store.mark_failed(
        claimed_id, "terminal", claimed_by="heavy-ready-owned"
    )
    assert not jobs_store.make_pending_job_ready(claimed_uid, lane="profile")

    orphan_uid = "u_profile_make_ready_orphan"
    seed_user(orphan_uid)
    _reset(orphan_uid)
    orphan_id, _ = jobs_store.enqueue_job(orphan_uid, "profile", reason="retry")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs "
            "SET available_at=clock_timestamp()+interval '1 hour' WHERE id=%s",
            (orphan_id,),
        )
        conn.execute("DELETE FROM v2_runtime_state WHERE user_id=%s", (orphan_uid,))
    assert not jobs_store.make_pending_job_ready(orphan_uid, lane="profile")


def test_delayed_pending_is_not_claimable_and_is_split_in_queue_metrics():
    uid = "u_profile_delayed_metrics"
    seed_user(uid)
    _reset(uid)
    before_count = jobs_store.pending_job_count()
    before_pool = jobs_store.pool_queue_metrics()["heavy"]
    before_lane = jobs_store.job_counts_by_lane().get(
        "profile", {"pending": 0, "pending_ready": 0, "pending_delayed": 0}
    )
    job_id, _ = jobs_store.enqueue_job(uid, "profile", reason="retry")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs "
            "SET available_at=clock_timestamp()+interval '1 hour' WHERE id=%s",
            (job_id,),
        )

    assert jobs_store.pending_job_count() == before_count
    metrics = jobs_store.pool_queue_metrics()["heavy"]
    assert metrics["pending_ready"] == before_pool.get(
        "pending_ready", before_pool["pending"]
    )
    assert metrics["pending_delayed"] == before_pool.get("pending_delayed", 0) + 1
    assert metrics["pending"] == metrics["pending_ready"]
    if before_pool["oldest_pending_sec"] is None:
        assert metrics["oldest_pending_sec"] is None

    lane = jobs_store.job_counts_by_lane()["profile"]
    assert lane["pending_ready"] == before_lane.get(
        "pending_ready", before_lane["pending"]
    )
    assert lane["pending_delayed"] == before_lane.get("pending_delayed", 0) + 1
    assert lane["pending"] == lane["pending_ready"]


def test_recover_killed_chat_is_exact_and_queues_terminal_failure():
    uid = "u_js_exact_watchdog_chat"
    seed_user(uid)
    _reset(uid)
    chat_id, _ = jobs_store.enqueue_job(uid, "chat")
    other_id, _ = jobs_store.enqueue_job(uid, "profile")
    claimed = jobs_store.claim_next_job("foreground-0:g7", lanes={"chat"})
    assert claimed is not None and int(claimed["id"]) == chat_id
    assert jobs_store.mark_running(chat_id, claimed_by="foreground-0:g7")

    recovered = jobs_store.recover_killed_job(
        job_id=chat_id, claimed_by="foreground-0:g7"
    )

    assert recovered == {
        "job_id": chat_id,
        "user_id": uid,
        "lane": "chat",
        "recovery": "terminal",
    }
    with db.get_pool().connection() as conn:
        chat = conn.execute(
            "SELECT status,last_error,claimed_by FROM agent_jobs WHERE id=%s",
            (chat_id,),
        ).fetchone()
        other = conn.execute(
            "SELECT status FROM agent_jobs WHERE id=%s", (other_id,)
        ).fetchone()
        outbox = conn.execute(
            "SELECT error_code FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (chat_id,),
        ).fetchone()
    assert chat == ("expired", "slot_watchdog_timeout", None)
    assert other == ("pending",)
    assert outbox == ("slot_watchdog_timeout",)
    assert jobs_store.recover_killed_job(
        job_id=chat_id, claimed_by="foreground-0:g7"
    ) is None


@pytest.mark.parametrize("lane", ["profile", "scheduled", "capture"])
def test_recover_killed_background_requeues_exact_claim(lane):
    uid = f"u_js_exact_watchdog_{lane}"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    claimed = jobs_store.claim_next_job(f"{lane}-owner", lanes={lane})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=f"{lane}-owner")

    assert jobs_store.recover_killed_job(
        job_id=job_id, claimed_by="wrong-owner"
    ) is None
    recovered = jobs_store.recover_killed_job(
        job_id=job_id, claimed_by=f"{lane}-owner"
    )

    assert recovered["recovery"] == "requeued"
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status,last_error,claimed_by,lease_expires_at "
            "FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert row == ("pending", "slot_watchdog_timeout", None, None)


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
    jobs_store.record_worker_heartbeat("w1", pool="foreground")
    jobs_store.record_worker_heartbeat(
        "w1:genesis", pool="control", kind="genesis"
    )

    assert jobs_store.live_worker_count() == 1
    assert jobs_store.workers_alive() is True
    assert jobs_store.genesis_worker_alive() is True


def test_genesis_heartbeat_alone_does_not_open_the_send_gate():
    """Genesis alive but every turn worker dead => send must still 503."""
    _clear_heartbeats()
    jobs_store.record_worker_heartbeat(
        "only:genesis", pool="control", kind="genesis"
    )

    assert jobs_store.workers_alive() is False
    assert jobs_store.live_worker_count() == 0
    assert jobs_store.genesis_worker_alive() is True


def test_genesis_worker_alive_false_when_nothing_beats():
    _clear_heartbeats()
    assert jobs_store.genesis_worker_alive() is False


def test_recent_worker_heartbeats_returns_identity_kind_capacity_and_db_age():
    _clear_heartbeats()
    jobs_store.record_worker_heartbeat(
        "v2-worker-new-deadbeef1234",
        pool="foreground",
        capacity=4,
        runtime_state={"slot": "foreground-0", "job_id": "job-123"},
    )
    jobs_store.record_worker_heartbeat(
        "v2-worker-new-deadbeef1234:genesis",
        pool="control",
        kind="genesis",
        capacity=0,
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
    assert turn["pool"] == "foreground"
    assert turn["runtime_state"] == {
        "slot": "foreground-0",
        "job_id": "job-123",
    }
    assert genesis["capacity"] == 0
    assert genesis["pool"] == "control"
    assert genesis["runtime_state"] == {}
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


# ── B4-4 (#18):到期提醒卡死后必须重投,不能终结 ──────────────────────────
#
# scheduled 是**必须送达**的道:scheduled_wake_v2 在**入队时**就 mark_fired,
# 所以一次 worker 进程死亡如果按终结处理,这条提醒就永久消失且不重试。
# 同文件的前台抢占路径早已按道分流(scheduled/capture 重投、其余终结);
# 这几条锁住租约回收器上的同一语义。


def _stuck_scheduled(uid: str) -> int:
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id, claimed_by="w")
    return job_id


def _job_row(job_id: int):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT status, attempt_count, last_error FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()


def test_reap_requeues_stuck_scheduled_instead_of_expiring():
    """租约超时 = worker 死了、活儿没干 → 退回 pending 重投,不是丢掉。"""
    import time

    job_id = _stuck_scheduled("u_js_sched_requeue")
    jobs_store.reap_stuck_jobs(now=time.time() + jobs_store.RUNNING_TTL_SEC + 10)
    status, attempts, last_error = _job_row(job_id)
    assert status == "pending", "到期提醒卡死后被终结了 —— 这条提醒永久丢失"
    assert attempts == 1
    assert last_error == "scheduled_lease_timeout_requeued"
    with db.get_pool().connection() as conn:
        marker = conn.execute(
            "SELECT error_code FROM v2_terminal_failure_outbox WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert marker is None, "重投的任务不该写终结失败回执"


def test_reap_expires_scheduled_once_requeue_budget_is_spent():
    """有界:一个每次都失败的提醒不能变成永不停歇的唤醒循环。"""
    import time

    job_id = _stuck_scheduled("u_js_sched_budget")
    cap = jobs_store.SCHEDULED_LEASE_REQUEUE_MAX_ATTEMPTS
    assert cap >= 1
    for _ in range(cap):
        jobs_store.reap_stuck_jobs(now=time.time() + jobs_store.RUNNING_TTL_SEC + 10)
        assert _job_row(job_id)[0] == "pending"
        jobs_store.claim_next_job("w")
        jobs_store.mark_running(job_id, claimed_by="w")
    jobs_store.reap_stuck_jobs(now=time.time() + jobs_store.RUNNING_TTL_SEC + 10)
    assert _job_row(job_id)[0] == "expired", (
        f"重投预算({cap})用尽后仍未终结 —— 会无限重投"
    )


def test_reap_still_expires_stuck_chat_job():
    """不误伤:chat 卡死仍是终结,重投语义只给 scheduled。"""
    import time

    seed_user("u_js_sched_chat_guard")
    _reset("u_js_sched_chat_guard")
    job_id, _ = jobs_store.enqueue_job("u_js_sched_chat_guard", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id, claimed_by="w")
    jobs_store.reap_stuck_jobs(now=time.time() + jobs_store.RUNNING_TTL_SEC + 10)
    assert _job_row(job_id)[0] == "expired"


def test_reap_does_not_touch_scheduled_still_in_pending():
    """重投只认 lease_timeout(worker 死了、活儿没干)。

    pending 的 scheduled 不该被本条路径碰:它还没被认领,重投没有意义 ——
    只会把 attempt_count 白白烧掉,预算耗尽后反而变成终结。

    ⚠️ 断言的是「**我这条路径没动它**」(attempt_count 与 last_error 不变),
    不是「它会被终结」—— 没有 deadline 的 pending 非 chat 任务在既有终结扫描里
    本来就永不匹配(COALESCE(queue_deadline_at, deadline_at, chat-only TTL)
    求值为 NULL),那是**改动前就有的**行为,不属于本条守的范围。
    """
    import time

    seed_user("u_js_sched_pending")
    _reset("u_js_sched_pending")
    job_id, _ = jobs_store.enqueue_job("u_js_sched_pending", "scheduled")
    # 不 claim:停在 pending
    jobs_store.reap_stuck_jobs(now=time.time() + jobs_store.RUNNING_TTL_SEC + 10_000)
    status, attempts, last_error = _job_row(job_id)
    assert attempts == 0, "pending 的 scheduled 被重投路径烧掉了一次预算"
    assert last_error != "scheduled_lease_timeout_requeued"


def test_requeued_scheduled_survives_stale_deadlines_and_is_claimable():
    """终点守卫(codex2 审出:这两处清理是承重的,原实现没有测试钉住它)。

    一个**旧的、已过期的**入队/执行截止如果留在重投后的行上,同一轮 reaper
    会立刻把它再次终结 —— 重投等于没做。删掉 queue_deadline_at=NULL 或
    deadline_at=NULL 任意一处,本条必红。

    断言的是**终点状态**:reap 之后仍 pending、旧 owner 已被 fence、
    并且能被新 owner 重新认领(=这条提醒真的还会再送一次)。
    """
    import time

    uid = "u_js_sched_endstate"
    job_id = _stuck_scheduled(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET queue_deadline_at=now()-interval '1 hour', "
            "deadline_at=now()-interval '1 hour' WHERE id=%s",
            (job_id,),
        )
    future = time.time() + jobs_store.RUNNING_TTL_SEC + 10
    jobs_store.reap_stuck_jobs(now=future)
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, attempt_count, claimed_by, queue_deadline_at, deadline_at "
            "FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
    assert row[0] == "pending", "带着过期截止被重投 → 同一轮又被终结,提醒仍然丢"
    assert row[1] == 1
    assert row[2] is None, "旧 owner 没被 fence"
    assert row[3] is None and row[4] is None, "过期截止没清干净"
    assert jobs_store.claim_next_job("w2") is not None, "重投后无法被重新认领"


def test_scheduled_requeue_budget_config_is_fail_closed(monkeypatch):
    """配成 0/负数/非数字必须启动即炸,不能静默把修复关掉。"""
    import pytest as _pytest

    for bad in ("0", "-1", "abc", ""):
        monkeypatch.setenv("FEEDLING_V2_SCHEDULED_REQUEUE_MAX_ATTEMPTS", bad)
        with _pytest.raises(RuntimeError):
            jobs_store._positive_int_env(
                "FEEDLING_V2_SCHEDULED_REQUEUE_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("FEEDLING_V2_SCHEDULED_REQUEUE_MAX_ATTEMPTS", "5")
    assert jobs_store._positive_int_env(
        "FEEDLING_V2_SCHEDULED_REQUEUE_MAX_ATTEMPTS", "3") == 5


def test_scheduled_with_unresolved_mcp_mutation_is_not_requeued():
    """P0(codex2 交叉审计):未决/unknown 的 MCP 写**绝不能**被重投。

    一个超时的 MCP 写可能**远端已经成功、只是回执丢了**。终结 CTE 正是为此把
    这类任务判成 `mcp_mutation_outcome_unknown` 并终结。重投路径若不排除它,
    会重复执行一次已生效的远端副作用(重复建日程 / 重复发消息),
    而用户侧只看得到「提醒来了两次」,看不到真正发生了什么。

    ⚠️ **错误标签**仍由 terminal CTE 按 outcome 选择
    (mcp_mutation_outcome_unknown vs lease_timeout);但**是否重投**看的是
    「这一轮有没有 **任何** MCP attempt」,不看 outcome。

    这两件事一度被我混为一谈:第一版把「选标签的条件」抄成了「重投的闸」,
    结果 known-success 也会被重投 —— 而 barrier 契约的原话正是
    「a known success is exactly the case that must not be repeated」。
    别再把这个闸按 outcome 窄回去。
    """
    import time

    uid = "u_js_sched_mcp_unknown"
    job_id = _stuck_scheduled(uid)
    jobs_store.start_mcp_mutation_attempt(
        job_id,
        user_id=uid,
        claimed_by="w",
        call_id="lost-receipt",
        tool_name="mcp__calendar__create",
        input_frontier_seq=0,
    )
    rows = jobs_store.reap_stuck_job_rows(
        now=time.time() + jobs_store.RUNNING_TTL_SEC + 10)
    assert [(r["id"], r["last_error"]) for r in rows] == [
        (job_id, "mcp_mutation_outcome_unknown")
    ], "带未决 MCP 写的到期提醒被重投了 —— 可能重复执行远端副作用"
    status, _attempts, _err = _job_row(job_id)
    assert status == "expired"
    with db.get_pool().connection() as conn:
        outcome = conn.execute(
            "SELECT outcome FROM v2_mcp_mutation_attempts WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert outcome == ("unknown",)


def test_scheduled_with_known_mcp_mutation_is_also_not_requeued():
    """`known` 成功**尤其**不能重投 —— barrier 契约的原话就是这个。

    终结 CTE 对 known 也终结(只是 last_error=lease_timeout),原实现从未重跑整轮。
    重投复用同一 job_id,而 worker 的 mutation recovery barrier 只覆盖 lane='chat',
    保护不到 scheduled;模型重跑会生成新 call_id 再写一次。
    """
    import time

    uid = "u_js_sched_mcp_known"
    job_id = _stuck_scheduled(uid)
    jobs_store.start_mcp_mutation_attempt(
        job_id, user_id=uid, claimed_by="w", call_id="already-done",
        tool_name="mcp__calendar__create", input_frontier_seq=0,
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_mcp_mutation_attempts SET outcome='known', "
            "resolved_at=clock_timestamp() WHERE job_id=%s", (job_id,))
    jobs_store.reap_stuck_jobs(now=time.time() + jobs_store.RUNNING_TTL_SEC + 10)
    status, _a, _e = _job_row(job_id)
    assert status != "pending", (
        "已知成功的 MCP 写被重投了 —— 这正是 barrier 契约点名不能重复的那一种"
    )


@pytest.mark.parametrize("effect_type", sorted(jobs_store.DURABLE_TOOL_EFFECT_TYPES))
def test_scheduled_with_durable_effect_is_not_requeued(effect_type):
    """产生过持久平台写的这一轮,一律不重投(codex2 交叉审计第二层)。

    不只 MCP:`worker._write_tool_effect_payload` 是**所有道共享**的,
    scheduled 一样能落 memory/identity/schedule/workspace 写。重投复用同一
    job_id 并重跑整轮,而 mutation recovery barrier 只保护 lane='chat'。

    **参数化整个闭集**,而不是只测 memory —— 否则新增一种 effect 类型时
    (例如 workspace_batch)会静默漏掉,且不会有任何东西变红。

    用生产入口 `effect_outbox.enqueue_effect` 造数据,不手写 INSERT:
    手写会绑死当下的列结构,schema 一改测试就以错误的理由红。
    """
    import time
    from model_api_runtime.v2 import effect_outbox

    uid = f"u_js_eff_{effect_type[:12]}"
    job_id = _stuck_scheduled(uid)
    effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type=effect_type,
        ordinal=0,
        expected_generation=1,
        payload={"ciphertext": "shell"},
        input_frontier_seq=0,
    )
    jobs_store.reap_stuck_jobs(now=time.time() + jobs_store.RUNNING_TTL_SEC + 10)
    status, _a, _e = _job_row(job_id)
    assert status != "pending", (
        f"落过 {effect_type} 的到期提醒被重投 —— 会重复执行一次已生效的写"
    )


def test_durable_effect_closed_set_matches_worker():
    """漂移守卫:jobs_store 的闭集必须与 worker 的值域一致。

    jobs_store 是底层,不能反向 import worker(会成环),所以闭集在本地重写了一份。
    两份定义必然漂移 —— 除非有东西在漂移时变红。这条就是那个东西。

    ⚠️ 顺带记录:serve_worker.py 里还有**第三份**拷贝(_ENCRYPTED_TOOL_EFFECT_TYPES)。
    本条只钉住 jobs_store↔worker;那一份是既有状况,不在本批范围。
    """
    from model_api_runtime.v2 import worker as v2_worker

    assert jobs_store.DURABLE_TOOL_EFFECT_TYPES == frozenset(
        v2_worker.ENCRYPTED_TOOL_EFFECT_TYPES.values()
    )
