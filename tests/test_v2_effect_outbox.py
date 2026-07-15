"""Generation-fenced effect outbox: enqueue idempotency + fenced apply (spec A4)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import effect_outbox, effect_id

from conftest import seed_user

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
