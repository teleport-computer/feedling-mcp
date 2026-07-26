"""Generation-fenced effect outbox: enqueue idempotency + fenced apply (spec A4)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import effect_outbox, effect_id, jobs_store

from conftest import seed_user
from incident_guard_reference import legacy_wake_should_publish

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 effect outbox tests require the PostgreSQL test fixture",
)


def _set_v2_owner(user_id: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET hosted_runtime_state='v2' "
            "WHERE user_id=%s",
            (user_id,),
        )


def _seed_active_route(
    user_id: str,
    *,
    error: str = "",
    error_class: str = "",
) -> None:
    credential_id = str(uuid.uuid4())
    route_id = str(uuid.uuid4())
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO model_api_credentials "
            "(id,user_id,provider,label,base_url,api_key_envelope) "
            "VALUES (%s,%s,'anthropic','test','','{}'::jsonb)",
            (credential_id, user_id),
        )
        conn.execute(
            "INSERT INTO model_api_routes "
            "(id,user_id,credential_id,model,is_active,test_status,"
            "last_runtime_error,last_runtime_error_class) "
            "VALUES (%s,%s,%s,'claude-test',true,'ok',%s,%s)",
            (route_id, user_id, credential_id, error, error_class),
        )


@pytest.fixture
def pg_clean():
    """Truncate the tables this module's tests touch so rows from one test
    (or another module sharing the session-scoped DB) never leak into the
    next: a leftover v2_runtime_state row would let a later test's
    db.get_runtime_generation lazy-init see a stale generation instead of
    starting fresh at 1, and a leftover v2_effect_outbox row would pollute
    effect_pending() for a reused user_id."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_effect_outbox, v2_runtime_state, agent_jobs, user_blobs CASCADE"
        )
    yield


def test_enqueue_is_idempotent_on_effect_id(pg_clean):
    seed_user("u_ob1")
    eid = effect_id.derive(job_id=1, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(eid, "u_ob1", 1, "reply", 1, {"text": "hi"}) is True
    assert db.effect_enqueue(eid, "u_ob1", 1, "reply", 1, {"text": "DUP"}) is False
    pend = db.effect_pending("u_ob1")
    assert len(pend) == 1 and pend[0]["payload"]["text"] == "hi"


def test_apply_dispatches_when_generation_matches(pg_clean):
    seed_user("u_ob2")
    db.get_runtime_generation("u_ob2")  # init at 1
    _set_v2_owner("u_ob2")
    eid = effect_id.derive(job_id=2, effect_type="reply", ordinal=0)
    db.effect_enqueue(eid, "u_ob2", 2, "reply", 1, {"text": "keep"})
    seen = []
    res = effect_outbox.apply_pending_effects("u_ob2", dispatch=lambda t, p: seen.append((t, p)))
    assert res == {"applied": 1, "discarded": 0}
    # Subset assertion (not full-dict equality): the applier annotates the
    # dispatched payload with the row's effect_id (Task 6 / spec A6) so sinks
    # can claim it for exactly-once — that key is additive, not part of the
    # caller's original payload.
    assert len(seen) == 1
    etype, payload = seen[0]
    assert etype == "reply"
    assert payload["text"] == "keep"
    assert payload["effect_id"] == eid
    assert db.effect_pending("u_ob2") == []


def _running_chat_job(uid: str, generation: int, *, owner: str = "owner") -> int:
    job_id, coalesced = jobs_store.enqueue_job(
        uid, "chat", expected_generation=generation)
    assert coalesced is False
    job = jobs_store.claim_next_job(owner)
    assert job is not None and int(job["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=owner) is True
    return job_id


def _running_wake_job(
    uid: str,
    generation: int,
    *,
    lane: str = "heartbeat",
    owner: str = "owner",
) -> int:
    job_id, coalesced = jobs_store.enqueue_job(
        uid,
        lane,
        expected_generation=generation,
    )
    assert coalesced is False
    job = jobs_store.claim_next_job(owner)
    assert job is not None and int(job["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=owner) is True
    return job_id


def _wake_intermediate_payload(*, lane: str = "heartbeat") -> dict:
    return {
        "text": "proactive candidate",
        "wake_kind": lane,
        effect_outbox.REPLY_SOURCE_FENCE_KEY: {"claimed_by": "owner"},
    }


def _wake_terminal_payload(*, observed_user_seq: int, lane: str = "heartbeat") -> dict:
    return {
        "text": "proactive candidate",
        "wake_kind": lane,
        effect_outbox.FINAL_REPLY_FENCE_KEY: {
            "claimed_by": "owner",
            "input_generation": 0,
            "observed_user_seq": observed_user_seq,
        },
    }


def _final_fence_payload(
    *, input_generation: int, through_seq: int, claimed_by: str = "owner"
) -> dict:
    return {
        "text": "candidate final",
        "reply_through_seq": through_seq,
        effect_outbox.FINAL_REPLY_FENCE_KEY: {
            "claimed_by": claimed_by,
            "input_generation": input_generation,
            "through_seq": through_seq,
        },
    }


def test_fenced_reply_is_hidden_from_old_pending_reader_but_current_drain_sees_it(
    pg_clean,
):
    """A pre-0041 sweeper must not split the new compound reply boundary."""
    uid = "u_ob_mixed_version_fenced_pending"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    job_id = _running_chat_job(uid, generation)
    effect_type = effect_outbox.INTERMEDIATE_REPLY_EFFECT_TYPE
    eid = effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type=effect_type,
        ordinal=0,
        expected_generation=generation,
        payload={
            "text": "still working",
            effect_outbox.REPLY_SOURCE_FENCE_KEY: {"claimed_by": "owner"},
        },
    )

    with db.get_pool().connection() as conn:
        status = conn.execute(
            "SELECT status FROM v2_effect_outbox WHERE effect_id=%s",
            (eid,),
        ).fetchone()[0]
        # This is the exact predicate used by the origin/pre applier and its
        # fleet-wide 60-second sweeper.
        old_reader = conn.execute(
            "SELECT effect_id FROM v2_effect_outbox "
            "WHERE user_id=%s AND status='pending' ORDER BY enqueue_seq",
            (uid,),
        ).fetchall()
    assert status == "pending_fenced_v1"
    assert old_reader == []
    assert [row["effect_id"] for row in db.effect_pending(uid)] == [eid]
    assert uid in db.effect_pending_users()
    assert db.effect_outbox_health()["pending"] == 1

    seen = []
    result = effect_outbox.apply_pending_effects(
        uid,
        dispatch=lambda kind, payload: seen.append((kind, payload["text"])),
    )

    assert result == {"applied": 1, "discarded": 0}
    assert seen == [("reply", "still working")]
    assert db.effect_pending(uid) == []


def test_wake_reply_is_suppressed_when_user_arrives_during_model_turn(
    pg_clean,
    monkeypatch,
):
    """The user row lands after the wake job starts but before publication.
    V2 must sleep at post time, not fold that row and make another provider
    call. This is the same conclusion as V1's post-time collision guard."""
    uid = "u_ob_wake_collision_during_turn"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    job_id = _running_wake_job(uid, generation)
    now = time.time()
    message = {
        "id": "user-during-wake",
        "role": "user",
        "source": "chat",
        "body_ct": "ciphertext",
        "ts": now,
    }
    assert legacy_wake_should_publish(
        monkeypatch,
        lane="heartbeat",
        message=message,
        now=now,
    ) is False
    db.chat_append_strict(
        uid,
        "user-during-wake",
        now,
        message,
        5000,
    )
    observed_user_seq = int(
        db.chat_seq_for_msg_id(uid, "user-during-wake")
    )
    eid = effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type=effect_outbox.TERMINAL_REPLY_EFFECT_TYPE,
        ordinal=0,
        expected_generation=generation,
        payload=_wake_terminal_payload(observed_user_seq=observed_user_seq),
    )
    seen = []

    result = effect_outbox.apply_pending_effects(
        uid,
        dispatch=lambda *args: seen.append(args),
    )

    assert result["applied"] == 0
    assert result["discarded"] == 1
    assert seen == []
    assert effect_outbox.get_effect_disposition(
        eid,
        user_id=uid,
        job_id=job_id,
        effect_type=effect_outbox.TERMINAL_REPLY_EFFECT_TYPE,
    ) == {
        "status": "discarded",
        "last_error": effect_outbox.WAKE_REPLY_CHAT_COLLISION,
    }


@pytest.mark.parametrize(
    ("lane", "age_sec", "should_publish"),
    [
        ("heartbeat", 91.0, True),
        ("scheduled", 1.0, True),
    ],
)
def test_wake_collision_window_and_scheduled_exemption_match_v1(
    pg_clean,
    monkeypatch,
    lane,
    age_sec,
    should_publish,
):
    uid = f"u_ob_wake_collision_{lane}_{int(age_sec)}"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    job_id = _running_wake_job(uid, generation, lane=lane)
    now = time.time()
    message = {
        "id": "nearby-user",
        "role": "user",
        "source": "chat",
        "body_ct": "ciphertext",
        "ts": now - age_sec,
    }
    assert legacy_wake_should_publish(
        monkeypatch,
        lane=lane,
        message=message,
        now=now,
    ) is should_publish
    db.chat_append_strict(
        uid,
        "nearby-user",
        now - age_sec,
        message,
        5000,
    )
    eid = effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type=effect_outbox.INTERMEDIATE_REPLY_EFFECT_TYPE,
        ordinal=0,
        expected_generation=generation,
        payload=_wake_intermediate_payload(lane=lane),
    )
    seen = []

    result = effect_outbox.apply_pending_effects(
        uid,
        dispatch=lambda effect_type, payload: seen.append(
            (effect_type, payload["text"])
        ),
    )

    assert result == {"applied": 1, "discarded": 0}
    assert seen == [("reply", "proactive candidate")]
    assert effect_outbox.get_effect_disposition(
        eid,
        user_id=uid,
        job_id=job_id,
        effect_type=effect_outbox.INTERMEDIATE_REPLY_EFFECT_TYPE,
    )["status"] == "applied"


def test_valid_final_reply_atomically_completes_job_and_clears_route_error(
    pg_clean,
):
    uid = "u_ob_final_atomic_completion"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    _seed_active_route(
        uid,
        error="previous_runtime_failure",
        error_class="provider",
    )
    job_id = _running_chat_job(uid, generation)
    eid = effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid,
        uid,
        job_id,
        "reply",
        generation,
        _final_fence_payload(input_generation=0, through_seq=0),
    )
    seen = []

    result = effect_outbox.apply_pending_effects(
        uid, dispatch=lambda *args: seen.append(args))

    assert result == {
        "applied": 1,
        "discarded": 0,
        effect_outbox.FINALIZED_JOB_IDS_KEY: [job_id],
    }
    assert len(seen) == 1
    with db.get_pool().connection() as conn:
        job = conn.execute(
            "SELECT status,finished_at IS NOT NULL FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
        effect = conn.execute(
            "SELECT status,applied_at IS NOT NULL FROM v2_effect_outbox "
            "WHERE effect_id=%s",
            (eid,),
        ).fetchone()
        route = conn.execute(
            "SELECT last_runtime_error,last_runtime_error_class "
            "FROM model_api_routes WHERE user_id=%s AND is_active",
            (uid,),
        ).fetchone()
    assert job == ("completed", True)
    assert effect == ("applied", True)
    assert route == ("", "")


def test_final_reply_send_first_is_discarded_before_dispatch(pg_clean):
    """If B commits while the provider is in flight, its generation bump wins
    before final-effect apply and the stale A-only answer never reaches a sink."""
    uid = "u_ob_late_final_send_first"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    job_id = _running_chat_job(uid, generation)
    eid = effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid,
        uid,
        job_id,
        "reply",
        generation,
        _final_fence_payload(input_generation=0, through_seq=0),
    )

    same_id, coalesced = jobs_store.enqueue_job(
        uid, "chat", expected_generation=generation)
    assert (same_id, coalesced) == (job_id, True)
    seen = []

    result = effect_outbox.apply_pending_effects(
        uid, dispatch=lambda *args: seen.append(args))

    assert result == {
        "applied": 0,
        "discarded": 1,
        effect_outbox.FINAL_REPLY_SUPERSEDED_EFFECT_IDS_KEY: [eid],
    }
    assert seen == []
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status,last_error FROM v2_effect_outbox WHERE effect_id=%s",
            (eid,),
        ).fetchone()
    assert row == ("discarded", "input_generation_advanced")
    assert effect_outbox.get_effect_disposition(
        eid, user_id=uid, job_id=job_id
    ) == {
        "status": "discarded",
        "last_error": effect_outbox.FINAL_REPLY_INPUT_ADVANCED,
    }
    # A producer drain that runs after an independent sweeper sees no changed
    # rows; the authoritative disposition above remains durable and queryable.
    assert effect_outbox.apply_pending_effects(
        uid, dispatch=lambda *_args: pytest.fail("stale reply dispatched")
    ) == {"applied": 0, "discarded": 0}


def test_final_reply_apply_first_serializes_send_after_dispatch(pg_clean):
    """If final-effect apply locks first, a concurrent send waits until the
    reply sink has finished. This is the other legal linearization ordering."""
    uid = "u_ob_late_final_apply_first"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    job_id = _running_chat_job(uid, generation)
    eid = effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid,
        uid,
        job_id,
        "reply",
        generation,
        _final_fence_payload(input_generation=0, through_seq=0),
    )

    dispatch_entered = threading.Event()
    release_dispatch = threading.Event()

    def blocking_dispatch(_effect_type, payload):
        assert effect_outbox.FINAL_REPLY_FENCE_KEY not in payload
        dispatch_entered.set()
        assert release_dispatch.wait(timeout=3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        apply_future = pool.submit(
            effect_outbox.apply_pending_effects,
            uid,
            dispatch=blocking_dispatch,
        )
        assert dispatch_entered.wait(timeout=3)
        send_future = pool.submit(
            db.chat_append_and_enqueue,
            uid,
            "B",
            20.0,
            {"id": "B", "role": "user", "body_ct": "ciphertext-B"},
            5000,
            "chat",
            expected_generation=generation,
        )
        try:
            # Final publication owns the runtime/source-job transaction through
            # dispatch, so B cannot persist or choose an active job yet.
            with pytest.raises(FutureTimeoutError):
                send_future.result(timeout=0.25)
        finally:
            release_dispatch.set()

        assert apply_future.result(timeout=3) == {
            "applied": 1,
            "discarded": 0,
            effect_outbox.FINALIZED_JOB_IDS_KEY: [job_id],
        }
        b_seq, fresh_job_id = send_future.result(timeout=3)

    assert b_seq == db.chat_seq_for_msg_id(uid, "B")
    assert fresh_job_id != job_id
    with db.get_pool().connection() as conn:
        jobs = conn.execute(
            "SELECT id,status,input_generation,expected_runtime_generation "
            "FROM agent_jobs WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
        message = conn.execute(
            "SELECT doc->>'role',doc->>'body_ct' FROM chat_messages "
            "WHERE user_id=%s AND msg_id='B'",
            (uid,),
        ).fetchone()
    assert jobs == [
        (job_id, "completed", 0, generation),
        (fresh_job_id, "pending", 0, generation),
    ]
    assert message == ("user", "ciphertext-B")


def test_exact_disposition_waits_for_concurrent_applier_commit(pg_clean):
    """A producer querying its exact effect must not observe the old pending
    snapshot while an independent sweeper is already publishing that row."""
    uid = "u_ob_exact_disposition_waits"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    job_id = _running_chat_job(uid, generation)
    eid = effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid,
        uid,
        job_id,
        "reply",
        generation,
        _final_fence_payload(input_generation=0, through_seq=0),
    )
    dispatch_entered = threading.Event()
    release_dispatch = threading.Event()

    def blocking_dispatch(_effect_type, _payload):
        dispatch_entered.set()
        assert release_dispatch.wait(timeout=3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        apply_future = pool.submit(
            effect_outbox.apply_pending_effects,
            uid,
            dispatch=blocking_dispatch,
        )
        assert dispatch_entered.wait(timeout=3)
        disposition_future = pool.submit(
            effect_outbox.get_effect_disposition,
            eid,
            user_id=uid,
            job_id=job_id,
        )
        try:
            with pytest.raises(FutureTimeoutError):
                disposition_future.result(timeout=0.25)
        finally:
            release_dispatch.set()

        assert apply_future.result(timeout=3) == {
            "applied": 1,
            "discarded": 0,
            effect_outbox.FINALIZED_JOB_IDS_KEY: [job_id],
        }
        assert disposition_future.result(timeout=3) == {
            "status": "applied",
            "last_error": "",
        }


def test_consumed_seq_frontier_independently_discards_newer_user_row(pg_clean):
    uid = "u_ob_late_final_seq_belt"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    job_id = _running_chat_job(uid, generation)
    # Simulate a future/broken admission producer that persisted B without the
    # generation bump. The consumed-seq belt must still refuse the stale final.
    db.chat_append_strict(
        uid,
        "late-user-row",
        1.0,
        {"id": "late-user-row", "role": "user", "body_ct": "ciphertext"},
        5000,
    )
    eid = effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid,
        uid,
        job_id,
        "reply",
        generation,
        _final_fence_payload(input_generation=0, through_seq=0),
    )

    result = effect_outbox.apply_pending_effects(
        uid, dispatch=lambda *_args: pytest.fail("stale reply dispatched"))
    assert result[effect_outbox.FINAL_REPLY_SUPERSEDED_EFFECT_IDS_KEY] == [eid]


def test_final_reply_from_terminalized_source_job_is_discarded(pg_clean):
    """A reaper/lifecycle transition that wins after the worker's last lease
    renewal must fence the not-yet-published final reply at the effect row."""
    uid = "u_ob_final_source_job_terminal"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    job_id = _running_chat_job(uid, generation)
    eid = effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid,
        uid,
        job_id,
        "reply",
        generation,
        _final_fence_payload(input_generation=0, through_seq=0),
    )
    assert jobs_store.mark_failed(
        job_id, "lease_lost_test", claimed_by="owner") is True
    seen = []

    result = effect_outbox.apply_pending_effects(
        uid, dispatch=lambda *args: seen.append(args))

    assert result == {
        "applied": 0,
        "discarded": 1,
        effect_outbox.FINAL_REPLY_SUPERSEDED_EFFECT_IDS_KEY: [eid],
    }
    assert seen == []
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status,last_error FROM v2_effect_outbox WHERE effect_id=%s",
            (eid,),
        ).fetchone()
    assert row == ("discarded", "source_job_not_active")


def test_intermediate_reply_is_not_late_input_fenced(pg_clean):
    uid = "u_ob_intermediate_unfenced"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    job_id = _running_chat_job(uid, generation)
    jobs_store.enqueue_job(uid, "chat", expected_generation=generation)
    eid = effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid, uid, job_id, "reply", generation, {"text": "I am checking"})
    seen = []

    assert effect_outbox.apply_pending_effects(
        uid,
        dispatch=lambda effect_type, payload: seen.append((effect_type, payload)),
    ) == {"applied": 1, "discarded": 0}
    assert seen[0][0] == "reply"
    assert seen[0][1]["text"] == "I am checking"
    with db.get_pool().connection() as conn:
        job = conn.execute(
            "SELECT status,finished_at FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert job == ("running", None)


@pytest.mark.parametrize("failure_mode", ["owner_mismatch", "dispatch_failure"])
def test_final_reply_completion_failure_rolls_back_and_fails_closed(
    pg_clean, monkeypatch, failure_mode,
):
    uid = f"u_ob_final_completion_rollback_{failure_mode}"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    _seed_active_route(
        uid,
        error="keep_previous_failure",
        error_class="provider",
    )
    job_id = _running_chat_job(uid, generation)
    eid = effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid,
        uid,
        job_id,
        "reply",
        generation,
        _final_fence_payload(input_generation=0, through_seq=0),
    )
    seen = []

    if failure_mode == "owner_mismatch":
        real_complete = effect_outbox._complete_final_reply_job_on_cursor

        def lose_owner(cur, **kwargs):
            kwargs["claimed_by"] = "not-the-owner"
            return real_complete(cur, **kwargs)

        monkeypatch.setattr(
            effect_outbox, "_complete_final_reply_job_on_cursor", lose_owner)

        def dispatch(*args):
            seen.append(args)
    else:
        def dispatch(*args):
            seen.append(args)
            raise RuntimeError("reply sink failed")

    with pytest.raises(RuntimeError):
        effect_outbox.apply_pending_effects(uid, dispatch=dispatch)

    # Owner loss is detected before the sink. A sink failure happens after the
    # tentative completion, whose savepoint must roll back job + route changes.
    assert len(seen) == (0 if failure_mode == "owner_mismatch" else 1)
    with db.get_pool().connection() as conn:
        job = conn.execute(
            "SELECT status,finished_at FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
        effect = conn.execute(
            "SELECT status,attempt_count,last_error FROM v2_effect_outbox "
            "WHERE effect_id=%s",
            (eid,),
        ).fetchone()
        route = conn.execute(
            "SELECT last_runtime_error,last_runtime_error_class "
            "FROM model_api_routes WHERE user_id=%s AND is_active",
            (uid,),
        ).fetchone()
    assert job == ("running", None)
    assert effect == ("pending", 1, "dispatch_failed:RuntimeError")
    assert route == ("keep_previous_failure", "provider")


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "null",
        "fractional_generation",
        "string_generation",
        "boolean_generation",
        "fractional_seq",
        "nonstr_owner",
        "frontier_mismatch",
        "frontier_beyond_input",
    ],
)
def test_malformed_final_reply_fence_is_terminally_discarded(pg_clean, case):
    uid = f"u_ob_invalid_final_fence_{case}"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    job_id = _running_chat_job(uid, generation)
    payload = _final_fence_payload(input_generation=0, through_seq=0)
    fence = payload[effect_outbox.FINAL_REPLY_FENCE_KEY]
    if case == "missing":
        payload.pop(effect_outbox.FINAL_REPLY_FENCE_KEY)
    elif case == "null":
        payload[effect_outbox.FINAL_REPLY_FENCE_KEY] = None
    elif case == "fractional_generation":
        fence["input_generation"] = 0.9
    elif case == "string_generation":
        fence["input_generation"] = "0"
    elif case == "boolean_generation":
        fence["input_generation"] = False
    elif case == "fractional_seq":
        fence["through_seq"] = 0.9
    elif case == "nonstr_owner":
        fence["claimed_by"] = 123
    elif case == "frontier_mismatch":
        fence["through_seq"] = 1
    elif case == "frontier_beyond_input":
        fence["through_seq"] = 10
        payload["reply_through_seq"] = 10
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    eid = effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid, uid, job_id, "reply", generation, payload)
    seen = []

    result = effect_outbox.apply_pending_effects(
        uid, dispatch=lambda *args: seen.append(args))

    assert result == {
        "applied": 0,
        "discarded": 1,
        effect_outbox.FINAL_REPLY_SUPERSEDED_EFFECT_IDS_KEY: [eid],
    }
    assert seen == []
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status,last_error,attempt_count FROM v2_effect_outbox "
            "WHERE effect_id=%s",
            (eid,),
        ).fetchone()
    assert row == ("discarded", effect_outbox.FINAL_REPLY_INVALID_FENCE, 0)


def test_final_reply_blocked_past_lease_expiry_is_not_dispatched(
    pg_clean, monkeypatch,
):
    uid = "u_ob_final_lease_expires_while_blocked"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    job_id = _running_chat_job(uid, generation)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET lease_expires_at="
            "clock_timestamp() + interval '0.25 seconds' WHERE id=%s",
            (job_id,),
        )
    eid = effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid,
        uid,
        job_id,
        "reply",
        generation,
        _final_fence_payload(input_generation=0, through_seq=0),
    )

    apply_entered = threading.Event()
    real_fence = db._lock_chat_user_fence_on_cursor

    def observed_fence(cur, user_id, *, exclusive=False):
        real_fence(cur, user_id, exclusive=exclusive)
        if user_id == uid and not exclusive:
            apply_entered.set()

    monkeypatch.setattr(db, "_lock_chat_user_fence_on_cursor", observed_fence)
    seen = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.get_pool().connection() as blocker:
            with blocker.transaction():
                blocker.execute(
                    "SELECT 1 FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                    (uid,),
                )
                future = pool.submit(
                    effect_outbox.apply_pending_effects,
                    uid,
                    dispatch=lambda *args: seen.append(args),
                )
                assert apply_entered.wait(timeout=3)
                time.sleep(0.4)
        result = future.result(timeout=3)

    assert seen == []
    assert result == {
        "applied": 0,
        "discarded": 1,
        effect_outbox.FINAL_REPLY_SUPERSEDED_EFFECT_IDS_KEY: [eid],
    }
    assert effect_outbox.get_effect_disposition(
        eid, user_id=uid, job_id=job_id
    ) == {
        "status": "discarded",
        "last_error": effect_outbox.FINAL_REPLY_SOURCE_JOB_INACTIVE,
    }


def test_apply_discards_stale_generation_without_dispatch(pg_clean):
    seed_user("u_ob3")
    db.get_runtime_generation("u_ob3")  # 1
    eid = effect_id.derive(job_id=3, effect_type="memory", ordinal=0)
    db.effect_enqueue(eid, "u_ob3", 3, "memory", 1, {"card": "x"})
    # cut over -> generation 3; the pinned-at-1 effect must be discarded, NOT dispatched
    db.advance_runtime_state("u_ob3", from_state="resident", to_state="draining")
    db.advance_runtime_state("u_ob3", from_state="draining", to_state="v2")
    seen = []
    res = effect_outbox.apply_pending_effects("u_ob3", dispatch=lambda t, p: seen.append((t, p)))
    assert res == {"applied": 0, "discarded": 1}
    assert seen == []
    with db.get_pool().connection() as conn:
        payload = conn.execute(
            "SELECT payload FROM v2_effect_outbox WHERE effect_id=%s",
            (eid,),
        ).fetchone()[0]
    assert payload == {"legacy_payload_scrubbed": True}


def test_apply_discards_matching_generation_when_runtime_not_v2(pg_clean):
    uid = "u_ob_resident_owner"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    eid = effect_id.derive(job_id=30, effect_type="reply", ordinal=0)
    db.effect_enqueue(eid, uid, 30, "reply", generation, {"text": "must not send"})
    seen = []

    result = effect_outbox.apply_pending_effects(
        uid, dispatch=lambda *args: seen.append(args))

    assert result == {"applied": 0, "discarded": 1}
    assert seen == []


def test_applied_legacy_sensitive_payload_is_scrubbed(pg_clean):
    uid = "u_ob_legacy_scrub"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    eid = effect_id.derive(job_id=31, effect_type="identity", ordinal=0)
    assert db.effect_enqueue(
        eid,
        uid,
        31,
        "identity",
        generation,
        {"signature": "private legacy signature"},
    )
    seen = []
    assert effect_outbox.apply_pending_effects(
        uid,
        dispatch=lambda effect_type, payload: seen.append((effect_type, payload)),
    ) == {"applied": 1, "discarded": 0}
    assert seen[0][1]["signature"] == "private legacy signature"

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, payload::text FROM v2_effect_outbox WHERE effect_id=%s",
            (eid,),
        ).fetchone()
    assert row[0] == "applied"
    assert "private legacy signature" not in row[1]
    assert "legacy_payload_scrubbed" in row[1]


def test_apply_is_rerunnable_after_partial(pg_clean):
    # A second apply pass over already-applied rows is a no-op (idempotent applier).
    seed_user("u_ob4")
    db.get_runtime_generation("u_ob4")
    _set_v2_owner("u_ob4")
    eid = effect_id.derive(job_id=4, effect_type="status", ordinal=0)
    db.effect_enqueue(eid, "u_ob4", 4, "status", 1, {"k": "v"})
    n = []
    effect_outbox.apply_pending_effects("u_ob4", dispatch=lambda t, p: n.append(1))
    effect_outbox.apply_pending_effects("u_ob4", dispatch=lambda t, p: n.append(1))
    assert n == [1]  # dispatched exactly once


def test_malformed_payload_stays_pending_and_records_sanitized_error(pg_clean):
    uid = "u_ob_malformed_payload"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    eid = effect_id.derive(job_id=41, effect_type="memory", ordinal=0)
    assert db.effect_enqueue(eid, uid, 41, "memory", generation, ["not", "an", "object"])

    with pytest.raises(RuntimeError, match="effect payload must be an object"):
        effect_outbox.apply_pending_effects(
            uid,
            dispatch=lambda *_args: pytest.fail("malformed payload must not dispatch"),
        )

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, attempt_count, last_error FROM v2_effect_outbox "
            "WHERE effect_id=%s",
            (eid,),
        ).fetchone()
    assert row == ("pending", 1, "dispatch_failed:RuntimeError")
    assert uid not in db.effect_pending_users()

    # Turn start/mid-turn/end-turn all call the drain. The failed ordered head
    # must honor its durable backoff instead of burning through the retry
    # ceiling during one turn.
    assert effect_outbox.apply_pending_effects(
        uid,
        dispatch=lambda *_args: pytest.fail("deferred head must not dispatch"),
    ) == {"applied": 0, "discarded": 0}
    with db.get_pool().connection() as conn:
        retry_row = conn.execute(
            "SELECT status,attempt_count FROM v2_effect_outbox WHERE effect_id=%s",
            (eid,),
        ).fetchone()
    assert retry_row == ("pending", 1)


def test_poison_payload_hits_manual_reconciliation_ceiling(pg_clean):
    uid = "u_ob_poison_ceiling"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    eid = effect_id.derive(job_id=42, effect_type="memory", ordinal=0)
    assert db.effect_enqueue(eid, uid, 42, "memory", generation, ["bad"])

    for attempt in range(8):
        with pytest.raises(RuntimeError, match="effect payload must be an object"):
            effect_outbox.apply_pending_effects(uid, dispatch=lambda *_args: None)
        if attempt < 7:
            # Exercise the ceiling without sleeping through exponential retry
            # delays; immediate drains are intentionally no-ops (covered above).
            with db.get_pool().connection() as conn:
                conn.execute(
                    "UPDATE v2_effect_outbox SET next_attempt_at=now() "
                    "WHERE effect_id=%s AND status='pending'",
                    (eid,),
                )

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, attempt_count, last_error FROM v2_effect_outbox "
            "WHERE effect_id=%s",
            (eid,),
        ).fetchone()
    assert row == (
        "needs_reconciliation",
        8,
        "dispatch_failed:RuntimeError",
    )
    assert db.effect_pending(uid) == []


def test_concurrent_applier_observes_atomic_ambiguous_failure_backoff(
    pg_clean, monkeypatch,
):
    """A waiter that snapshotted a due effect must see A's terminal marker.

    Recording the ambiguous failure after releasing the effect row lock leaves
    a gap where B can deliver the same logical effect again. This forces B to
    snapshot before A commits and proves the marker/backoff is committed while
    A still owns the lock.
    """
    uid = "u_ob_atomic_ambiguous_failure"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    eid = effect_id.derive(job_id=43, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid, uid, 43, "reply", generation, {"text": "maybe delivered"}
    )

    first_dispatch_entered = threading.Event()
    release_first_dispatch = threading.Event()
    second_snapshot_ready = threading.Event()
    second_dispatched = threading.Event()
    pending_calls = 0
    pending_calls_lock = threading.Lock()
    real_effect_pending = db.effect_pending

    def observed_pending(*args, **kwargs):
        nonlocal pending_calls
        rows = real_effect_pending(*args, **kwargs)
        with pending_calls_lock:
            pending_calls += 1
            if pending_calls == 2:
                second_snapshot_ready.set()
        return rows

    monkeypatch.setattr(db, "effect_pending", observed_pending)

    def ambiguous_dispatch(_effect_type, _payload):
        first_dispatch_entered.set()
        assert release_first_dispatch.wait(timeout=3)
        raise db.EffectDeliveryUncertainError("sink claim outcome unknown")

    def forbidden_second_dispatch(_effect_type, _payload):
        second_dispatched.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            effect_outbox.apply_pending_effects,
            uid,
            dispatch=ambiguous_dispatch,
        )
        assert first_dispatch_entered.wait(timeout=3)
        second = pool.submit(
            effect_outbox.apply_pending_effects,
            uid,
            dispatch=forbidden_second_dispatch,
        )
        assert second_snapshot_ready.wait(timeout=3)
        release_first_dispatch.set()

        with pytest.raises(db.EffectDeliveryUncertainError):
            first.result(timeout=3)
        assert second.result(timeout=3) == {"applied": 0, "discarded": 0}

    assert not second_dispatched.is_set()
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status,attempt_count,last_error "
            "FROM v2_effect_outbox WHERE effect_id=%s",
            (eid,),
        ).fetchone()
    assert row == (
        "needs_reconciliation",
        1,
        "delivery_uncertain: unresolved sink claim requires reconciliation",
    )


def test_cutover_waits_for_generation_fenced_effect_transaction(pg_clean):
    """The generation lock remains held through dispatch and terminal status."""
    uid = "u_ob_cutover_serializes"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    eid = effect_id.derive(job_id=5, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(eid, uid, 5, "reply", generation, {"text": "keep"})

    dispatch_entered = threading.Event()
    release_dispatch = threading.Event()
    cutover_started = threading.Event()

    def blocking_dispatch(_effect_type, _payload):
        dispatch_entered.set()
        assert release_dispatch.wait(timeout=3), "test did not release dispatch"

    def advance_generation():
        cutover_started.set()
        return db.advance_runtime_state(
            uid, from_state="v2", to_state="draining"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        apply_future = pool.submit(
            effect_outbox.apply_pending_effects, uid, dispatch=blocking_dispatch
        )
        assert dispatch_entered.wait(timeout=3), "effect never reached dispatch"
        cutover_future = pool.submit(advance_generation)
        assert cutover_started.wait(timeout=3), "cutover task never started"

        try:
            with pytest.raises(FutureTimeoutError):
                cutover_future.result(timeout=0.25)
        finally:
            release_dispatch.set()

        assert apply_future.result(timeout=3) == {"applied": 1, "discarded": 0}
        assert cutover_future.result(timeout=3) == generation + 1

    assert db.effect_pending(uid) == []


def test_delete_waiter_does_not_deadlock_nested_reply_sink(
    pg_clean, monkeypatch,
):
    """An account delete queued behind the outer effect fence must not block
    the nested reply connection from joining that same shared fence.

    This deliberately queues the exclusive delete before dispatch opens the
    reply sink connection.  A finite lock timeout converts any PostgreSQL lock
    queue inversion into a deterministic test failure instead of hanging the
    suite forever.
    """
    uid = "u_ob_delete_nested_reply"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    eid = effect_id.derive(job_id=6, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(
        eid, uid, 6, "reply", generation, {"text": "keep"}
    )

    dispatch_entered = threading.Event()
    exclusive_called = threading.Event()
    exclusive_waiting = threading.Event()
    nested_committed = threading.Event()
    delete_backend_pid: list[int] = []
    real_fence = db._lock_chat_user_fence_on_cursor

    def observed_fence(cur, user_id, *, exclusive=False):
        if user_id == uid and exclusive:
            delete_backend_pid.append(
                int(cur.execute("SELECT pg_backend_pid()").fetchone()[0])
            )
            exclusive_called.set()
        elif user_id == uid and not exclusive and exclusive_waiting.is_set():
            # If PostgreSQL queues this compatible shared request behind the
            # already-waiting exclusive request, fail promptly and release the
            # outer transaction so the worker threads can shut down cleanly.
            cur.execute("SET LOCAL lock_timeout = '1s'")
        return real_fence(cur, user_id, exclusive=exclusive)

    monkeypatch.setattr(db, "_lock_chat_user_fence_on_cursor", observed_fence)

    def nested_reply_dispatch(_effect_type, _payload):
        dispatch_entered.set()
        assert exclusive_called.wait(timeout=3), "delete never requested fence"

        # Wait until PostgreSQL itself reports the delete backend sleeping on
        # the advisory lock.  This makes the ordering deterministic rather than
        # relying on a scheduling sleep.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with db.get_pool().connection() as conn:
                waiting = conn.execute(
                    "SELECT EXISTS ("
                    " SELECT 1 FROM pg_locks"
                    " WHERE pid=%s AND locktype='advisory' AND NOT granted"
                    ")",
                    (delete_backend_pid[0],),
                ).fetchone()[0]
            if waiting:
                exclusive_waiting.set()
                break
            time.sleep(0.01)
        assert exclusive_waiting.is_set(), "exclusive delete never queued"

        db.chat_append_effect_with_cursor(
            uid,
            f"reply-{eid}",
            time.time(),
            {
                "id": f"reply-{eid}",
                "role": "assistant",
                "source": "model_api",
                "content_type": "text",
            },
            100,
            0,
        )
        nested_committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        apply_future = pool.submit(
            effect_outbox.apply_pending_effects,
            uid,
            dispatch=nested_reply_dispatch,
        )
        assert dispatch_entered.wait(timeout=3), "effect never reached dispatch"
        delete_future = pool.submit(db.delete_user, uid)

        assert apply_future.result(timeout=5) == {"applied": 1, "discarded": 0}
        assert nested_committed.is_set()
        delete_future.result(timeout=5)

    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM users WHERE user_id=%s", (uid,)
        ).fetchone() is None
