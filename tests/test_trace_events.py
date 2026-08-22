"""T184/A: additive TEE trace table, retention machinery, and read contract."""

from __future__ import annotations

import logging
import os
import sys
import time as pytime
import types
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
import debug_trace  # noqa: E402
import asgi.lifespan as lifespan_mod  # noqa: E402
from accounts import registry  # noqa: E402
from admin import admin_core, data_track  # noqa: E402
from admin import trace_events_monitor as monitor  # noqa: E402
from admin import trace_events_partitions as partitions  # noqa: E402
from core import leader as core_leader  # noqa: E402
from core.reqctx import bind  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


_ZONE = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def tee_primary(monkeypatch):
    """Point the process-local pool at the migrated TEE test database."""
    original_url = os.environ["DATABASE_URL"]
    db.close_pool()
    monkeypatch.setenv("DATABASE_URL", os.environ["TEE_DATABASE_URL"])
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    yield
    db.close_pool()
    monkeypatch.setenv("DATABASE_URL", original_url)
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")


def _uid() -> str:
    return "usr_t184_" + uuid.uuid4().hex[:16]


def _event(*, ts: float, trace_id: str = "trace-t184") -> dict:
    return {
        "ts": ts,
        "subsystem": "agent",
        "type": "agent.test",
        "status": "ok",
        "actor": "backend",
        "lane": "heartbeat",
        "trace_id": trace_id,
        "turn_id": "turn-t184",
        "job_id": "job-t184",
        "provider": "test-provider",
        "model": "test-model",
        "enqueue_source": "heartbeat",
        "summary": "summary",
        "explain": "explain",
        "detail": {"safe": True},
        "content_excerpt": {"kind": "text", "chars": 3},
        "dur_ms": 12.5,
    }


def test_migration_has_beijing_bounds_no_fk_and_stable_indexes():
    today = datetime.now(_ZONE).date()
    name = f"trace_events_p{today:%Y%m%d}"
    with psycopg.connect(os.environ["TEE_DATABASE_URL"]) as conn:
        conn.execute("SET LOCAL TIME ZONE 'Asia/Shanghai'")
        constraints = conn.execute(
            "SELECT contype,pg_get_constraintdef(oid,true) FROM pg_constraint "
            "WHERE conrelid='trace_events'::regclass ORDER BY contype,conname"
        ).fetchall()
        indexes = [
            row[0]
            for row in conn.execute(
                "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                "WHERE indrelid='trace_events'::regclass ORDER BY indexrelid"
            ).fetchall()
        ]
        bound = conn.execute(
            "SELECT pg_get_expr(child.relpartbound,child.oid) "
            "FROM pg_class child WHERE child.relname=%s",
            (name,),
        ).fetchone()[0]
        outcome_column = conn.execute(
            "SELECT is_nullable,column_default FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='trace_events' "
            "AND column_name='outcome_class'"
        ).fetchone()

    assert not any(kind == "f" for kind, _ in constraints)
    assert any(kind == "p" and "PRIMARY KEY (id, ts)" in definition
               for kind, definition in constraints)
    assert any("(user_id, ts DESC, id DESC)" in definition for definition in indexes)
    assert any("(ts DESC, id DESC)" in definition for definition in indexes)
    assert any("(trace_id, ts DESC, id DESC)" in definition for definition in indexes)
    assert f"{today.isoformat()} 00:00:00+08" in bound
    assert outcome_column == (
        "NO", f"'{db.TRACE_OUTCOME_DEFAULT}'::text"
    )
    migration = (
        Path(__file__).parents[1]
        / "backend/alembic_tee/versions/0033_trace_events.py"
    ).read_text()
    assert "AT TIME ZONE 'Asia/Shanghai'" in migration


def test_strict_insert_and_query_use_ts_id_order(tee_primary):
    uid = _uid()
    now = datetime.now(_ZONE).timestamp()
    try:
        assert db.insert_trace_events_strict(
            uid,
            [_event(ts=now, trace_id="first"), _event(ts=now, trace_id="second")],
        ) == 2
        rows = db.query_trace_events(user_id=uid)
        assert [row["trace_id"] for row in rows] == ["second", "first"]
        assert rows[0]["detail"] == {"safe": True}
        assert rows[0]["content_excerpt"] == {"kind": "text", "chars": 3}
        assert rows[0]["outcome_class"] == db.TRACE_OUTCOME_DEFAULT
    finally:
        db.delete_trace_events_for_user(uid)


def test_trace_outcome_vocabulary_matches_runtime_dashboard():
    assert debug_trace.TRACE_OUTCOME_CLASSES == data_track.RUNTIME_OUTCOME_CLASSES
    assert debug_trace.TRACE_OUTCOME_DEFAULT == data_track.RUNTIME_OUTCOME_DEFAULT
    assert debug_trace.TRACE_OUTCOME_DEFAULT in debug_trace.TRACE_OUTCOME_CLASSES


@pytest.mark.parametrize("lane,outcome,expect_status,expect_class", [
    ("heartbeat", "completed", "ok", None),
    ("heartbeat", "failed", "error", "operational_failure"),
    ("scheduled", "rescheduled", "warning", None),
    ("manual_wake", "failed", "error", "control"),
])
def test_terminal_trace_reuses_the_job_trace_id(
    lane, outcome, expect_status, expect_class, monkeypatch
):
    """Both halves must be joinable, not merely both present.

    Two events that each exist but carry different ids satisfy "we emit at both
    ends" while still answering nothing -- which is the failure this whole item
    exists to remove.
    """
    import asyncio as _asyncio
    from model_api_runtime.v2 import worker, jobs_store

    captured = []

    def _emit(user_id, event_type, **kwargs):
        captured.append((user_id, event_type, kwargs))

    # Attribution comes from the durable row, never the claim snapshot.
    durable_error = "manual_wake_disabled" if expect_class == "control" else "boom"
    # The durable vocabulary is the row's, not the body's: a rescheduled turn
    # leaves the row pending, which is why the emitter checks it rather than
    # trusting the returned outcome string.
    durable_status = {"rescheduled": "pending"}.get(outcome, outcome)
    monkeypatch.setattr(
        jobs_store, "get_terminal_snapshot",
        lambda job_id, *, user_id: {"status": durable_status, "last_error": durable_error},
    )
    job = {
        "id": 4242, "user_id": "usr_term", "lane": lane,
        "trace_id": "trace-abc-123",
    }
    deps = types.SimpleNamespace(emit_debug_trace=_emit)
    _asyncio.run(worker._emit_job_terminal_trace(
        deps, job, outcome, dur_ms=12.5,
    ))

    assert len(captured) == 1
    user_id, event_type, kwargs = captured[0]
    assert event_type == "agent.job.terminal"
    assert kwargs["trace_id"] == job["trace_id"]      # the load-bearing one
    assert kwargs["turn_id"] == job["trace_id"]
    assert kwargs["job_id"] == "4242"
    assert kwargs["dur_ms"] == 12.5
    assert kwargs["status"] == expect_status
    assert kwargs["detail"]["lane"] == lane
    if expect_class is None:
        # outcome_class has no success member, so an ok/warning terminal must
        # not claim a failure classification.
        assert "outcome_class" not in kwargs
    else:
        assert kwargs["outcome_class"] == expect_class


def test_run_turn_measures_terminal_duration(monkeypatch):
    """The wrapper owns whole-turn elapsed time for every return path."""
    import asyncio as _asyncio
    from model_api_runtime.v2 import worker

    ticks = iter((1_000_000_000, 1_012_500_000))
    captured = []

    async def _completed(_job, _deps, *, enclave_sem=None):
        return "completed"

    async def _terminal(_deps, _job, _outcome, *, dur_ms):
        captured.append(dur_ms)

    monkeypatch.setattr(worker.time, "monotonic_ns", lambda: next(ticks))
    monkeypatch.setattr(worker, "_run_turn_body", _completed)
    monkeypatch.setattr(worker, "_emit_job_terminal_trace", _terminal)

    assert _asyncio.run(worker._run_turn(
        {"id": 1, "user_id": "u", "lane": "heartbeat"}, object(),
    )) == "completed"
    assert captured == [12.5]


def test_drained_turn_does_not_emit_an_invented_failure(monkeypatch):
    """A cancelled turn is drained, not failed.

    An earlier version used `finally`, which also runs for CancelledError and
    published outcome="failed" for a turn that never reached a terminal state --
    manufacturing a failure that did not happen, in the exact table people would
    later use to count failures.
    """
    import asyncio as _asyncio
    from model_api_runtime.v2 import worker

    captured = []
    monkeypatch.setattr(
        worker, "_emit_job_terminal_trace",
        lambda deps, job, outcome, *, dur_ms: captured.append(outcome) or _noop(),
    )

    async def _noop():
        return None

    async def _cancelled(job, deps, *, enclave_sem=None):
        raise _asyncio.CancelledError()

    monkeypatch.setattr(worker, "_run_turn_body", _cancelled)
    with pytest.raises(_asyncio.CancelledError):
        _asyncio.run(worker._run_turn({"id": 1, "user_id": "u", "lane": "heartbeat"}, None))
    assert captured == []


@pytest.mark.parametrize("durable_status", ["running", "completed", "claimed"])
def test_body_failure_without_a_terminal_row_emits_nothing(durable_status, monkeypatch):
    """A body that stopped is not a job that is terminal.

    On LostJobLease the winning lifecycle owns terminal visibility and the row
    may already be completed; trajectory review also returns "failed" when
    mark_running loses.  Emitting on the body's word alone would publish a
    failure for a job that actually succeeded.
    """
    import asyncio as _asyncio
    from model_api_runtime.v2 import worker, jobs_store

    captured = []
    monkeypatch.setattr(
        jobs_store, "get_terminal_snapshot",
        lambda job_id, *, user_id: {"status": durable_status, "last_error": ""},
    )
    deps = types.SimpleNamespace(
        emit_debug_trace=lambda uid, et, **kw: captured.append(kw))
    _asyncio.run(worker._emit_job_terminal_trace(
        deps,
        {"id": 5, "user_id": "u", "lane": "heartbeat", "trace_id": "t"},
        "failed",
        dur_ms=1.0,
    ))
    assert captured == []


def test_enqueue_rollback_leaves_no_job_and_fires_no_hook(tee_primary, monkeypatch):
    """The hook must sit outside the transaction.

    A trace claiming a job exists, for a job that rolled back, is worse than no
    trace at all -- so this drives a real INSERT and then fails the transaction.
    """
    from model_api_runtime.v2 import jobs_store

    uid = _uid()
    db.upsert_user({"user_id": uid, "created_at": datetime.now(_ZONE).isoformat()})
    calls = []
    real = jobs_store.coalesce_or_insert_on_cursor

    def _insert_then_fail(cur, user_id, lane, **kwargs):
        real(cur, user_id, lane, **kwargs)     # the row really is written...
        raise RuntimeError("transaction dies after insert")

    monkeypatch.setattr(jobs_store, "coalesce_or_insert_on_cursor", _insert_then_fail)
    monkeypatch.setattr(jobs_store, "on_job_enqueued",
                        lambda *a, **k: calls.append(a))
    try:
        with pytest.raises(RuntimeError):
            jobs_store.enqueue_job(uid, "heartbeat", reason="tick", trace_id="t-roll")
        assert calls == []                      # no event for a job that vanished
        with db.get_pool().connection() as conn:
            left = conn.execute(
                "SELECT count(*) FROM agent_jobs WHERE user_id=%s", (uid,)
            ).fetchone()[0]
        assert left == 0                        # ...and the insert really rolled back
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


@pytest.mark.parametrize("lane", sorted(jobs_store.LANES - {"chat"}))
def test_non_chat_enqueue_mints_joinable_trace_id_when_producer_omits_it(
    tee_primary, monkeypatch, lane,
):
    """Every real background enqueue must have one id shared by row and hook."""
    uid = _uid()
    db.upsert_user({"user_id": uid, "created_at": datetime.now(_ZONE).isoformat()})
    calls = []
    monkeypatch.setattr(
        jobs_store,
        "on_job_enqueued",
        lambda user_id, event_lane, **kwargs: calls.append(
            (user_id, event_lane, kwargs)
        ),
    )
    try:
        job_id, coalesced = jobs_store.enqueue_job(
            uid, lane, reason=f"{lane}_due", trace_id=None,
        )
        assert coalesced is False
        with db.get_pool().connection() as conn:
            row_trace_id = conn.execute(
                "SELECT trace_id FROM agent_jobs WHERE id=%s AND user_id=%s",
                (job_id, uid),
            ).fetchone()[0]

        assert isinstance(row_trace_id, str) and row_trace_id
        assert calls == [(
            uid,
            lane,
            {"reason": f"{lane}_due", "trace_id": row_trace_id},
        )]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_non_chat_enqueue_preserves_semantic_producer_trace_id(
    tee_primary, monkeypatch,
):
    """A wake/message id supplied by a producer must never be replaced."""
    from model_api_runtime.v2 import jobs_store

    uid = _uid()
    db.upsert_user({"user_id": uid, "created_at": datetime.now(_ZONE).isoformat()})
    calls = []
    monkeypatch.setattr(
        jobs_store,
        "on_job_enqueued",
        lambda _uid, _lane, **kwargs: calls.append(kwargs),
    )
    try:
        job_id, coalesced = jobs_store.enqueue_job(
            uid,
            "scheduled",
            reason="scheduled_wake",
            trace_id="wake-semantic-123",
        )
        assert coalesced is False
        with db.get_pool().connection() as conn:
            row_trace_id = conn.execute(
                "SELECT trace_id FROM agent_jobs WHERE id=%s AND user_id=%s",
                (job_id, uid),
            ).fetchone()[0]
        assert row_trace_id == "wake-semantic-123"
        assert calls == [{
            "reason": "scheduled_wake",
            "trace_id": "wake-semantic-123",
        }]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_chat_enqueue_does_not_invent_a_nonsemantic_turn_id(
    tee_primary, monkeypatch,
):
    """Chat correlation must remain a real message id, never a random token."""
    from model_api_runtime.v2 import jobs_store

    uid = _uid()
    db.upsert_user({"user_id": uid, "created_at": datetime.now(_ZONE).isoformat()})
    calls = []
    monkeypatch.setattr(
        jobs_store,
        "on_job_enqueued",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    try:
        job_id, coalesced = jobs_store.enqueue_job(
            uid, "chat", reason="ordered_followup", trace_id=None,
        )
        assert coalesced is False
        with db.get_pool().connection() as conn:
            row_trace_id = conn.execute(
                "SELECT trace_id FROM agent_jobs WHERE id=%s AND user_id=%s",
                (job_id, uid),
            ).fetchone()[0]
        assert row_trace_id is None
        assert calls == []
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_coalesced_enqueue_is_not_reported_as_an_enqueue():
    """Coalescing folds input into an existing row and keeps that row's
    trace_id, so emitting here would both name a non-event and publish an id the
    terminal event will never carry -- breaking the join it exists to provide."""
    from model_api_runtime.v2 import jobs_store

    calls = []
    original = jobs_store.on_job_enqueued
    jobs_store.on_job_enqueued = lambda uid, lane, **kw: calls.append(kw)
    try:
        jobs_store._notify_job_enqueued(
            "u", "scheduled", reason="r", trace_id="new-id", result=(7, True))
        assert calls == []                      # coalesced: nothing was enqueued
        jobs_store._notify_job_enqueued(
            "u", "scheduled", reason="r", trace_id="new-id", result=(8, False))
        assert calls == [{"reason": "r", "trace_id": "new-id"}]
    finally:
        jobs_store.on_job_enqueued = original


def test_terminal_attribution_reads_the_durable_row_not_the_claim_snapshot(monkeypatch):
    """mark_failed writes the row, never the worker's dict.

    Trusting the snapshot gives a fresh job an empty code and a retried job the
    PREVIOUS attempt's error -- a wrong attribution, which is worse than none.
    """
    import asyncio as _asyncio
    from model_api_runtime.v2 import worker, jobs_store

    captured = []
    monkeypatch.setattr(
        jobs_store, "get_terminal_snapshot",
        lambda job_id, *, user_id: {"status": "failed", "last_error": "turns_halted"},
    )
    deps = types.SimpleNamespace(
        emit_debug_trace=lambda uid, et, **kw: captured.append(kw))
    stale = {"id": 9, "user_id": "u", "lane": "heartbeat", "trace_id": "t",
             "last_error": "previous_attempt_boom"}
    _asyncio.run(worker._emit_job_terminal_trace(
        deps, stale, "failed", dur_ms=1.0,
    ))

    assert captured[0]["detail"]["error_code"] == "turns_halted"
    assert captured[0]["outcome_class"] == "control"   # not operational_failure


def test_terminal_trace_skips_chat_and_survives_a_broken_sink():
    import asyncio as _asyncio
    from model_api_runtime.v2 import worker

    seen = []
    deps = types.SimpleNamespace(emit_debug_trace=lambda *a, **k: seen.append(a))
    _asyncio.run(worker._emit_job_terminal_trace(
        deps,
        {"id": 1, "user_id": "u", "lane": "chat", "trace_id": "t"},
        "failed",
        dur_ms=1.0,
    ))
    assert seen == []  # chat is traced on its own send path

    def _boom(*a, **k):
        raise RuntimeError("sink down")

    # A turn must not fail because its observability failed.
    _asyncio.run(worker._emit_job_terminal_trace(
        types.SimpleNamespace(emit_debug_trace=_boom),
        {"id": 2, "user_id": "u", "lane": "heartbeat", "trace_id": "t"},
        "failed",
        dur_ms=1.0,
    ))


def test_enqueue_hook_fires_after_commit_and_skips_chat():
    """A trace claiming a job exists, for a job that rolled back, is worse than
    no trace -- so the hook must sit outside the transaction."""
    from model_api_runtime.v2 import jobs_store

    calls = []
    original = jobs_store.on_job_enqueued
    jobs_store.on_job_enqueued = lambda uid, lane, **kw: calls.append((uid, lane, kw))
    try:
        jobs_store._notify_job_enqueued("u1", "chat", reason="r", trace_id="t")
        assert calls == []  # highest-volume lane, already traced elsewhere
        jobs_store._notify_job_enqueued("u1", "heartbeat", reason="tick", trace_id="t9")
        assert calls == [("u1", "heartbeat", {"reason": "tick", "trace_id": "t9"})]

        def _boom(*a, **k):
            raise RuntimeError("hook down")

        jobs_store.on_job_enqueued = _boom
        jobs_store._notify_job_enqueued("u1", "heartbeat", reason="r", trace_id="t")
    finally:
        jobs_store.on_job_enqueued = original


def test_outcome_classifier_is_shared_with_the_ops_dashboard():
    """The dashboard and the trace layer must not classify the same job apart.

    Classification used to live only inside the dashboard query, so nothing
    stopped a second reader from deriving its own answer and calling a
    deliberate suppression a real failure on one screen but not the other.
    """
    from model_api_runtime.v2 import jobs_store

    for code in jobs_store.CONTROL_OUTCOME_CODES:
        assert jobs_store.terminal_outcome_class(code) == "control"
    for code in jobs_store.USER_UNAVAILABLE_OUTCOME_CODES:
        assert jobs_store.terminal_outcome_class(code) == "user_unavailable"
    for code in jobs_store.SAFETY_SUPPRESSION_CODES:
        assert jobs_store.terminal_outcome_class(code) == "operational_failure"
    for code in jobs_store.TIMEOUT_OUTCOME_CODES:
        assert jobs_store.terminal_outcome_class(code) == "timeout"
    # An unclassified code stays operational rather than being guessed into a
    # bucket that would quietly excuse it from the failure rate.
    assert jobs_store.terminal_outcome_class("something_new") == "operational_failure"
    assert jobs_store.terminal_outcome_class("") == "operational_failure"
    # Whatever it returns must be a member of the one shared vocabulary.
    every_code = (
        jobs_store.CONTROL_OUTCOME_CODES
        | jobs_store.USER_UNAVAILABLE_OUTCOME_CODES
        | jobs_store.SAFETY_SUPPRESSION_CODES
        | jobs_store.TIMEOUT_OUTCOME_CODES
        | {"anything_else"}
    )
    assert {jobs_store.terminal_outcome_class(c) for c in every_code} <= debug_trace.TRACE_OUTCOME_CLASSES


def test_detail_list_caps_cannot_drift_past_the_silent_ceiling():
    """A caller cap above the ceiling makes its own truncated flag lie."""
    from model_api_runtime.v2 import serve_worker

    assert serve_worker._MCP_CATALOG_MAX_TOOLS <= debug_trace._DETAIL_MAX_LIST

    names = [f"tool_{i}" for i in range(debug_trace._DETAIL_MAX_LIST + 14)]
    bounded = debug_trace.bounded_names("collapsed_names", names)
    # Survives _safe_detail without losing anything it did not admit to losing.
    safe = debug_trace._safe_detail(bounded)
    assert safe["collapsed_names"] == bounded["collapsed_names"]
    assert safe["collapsed_names_truncated"] is True
    assert safe["collapsed_names_total"] == len(names)
    # A caller asking for more than the ceiling is clamped, not silently obeyed.
    assert len(debug_trace.bounded_names("k", names, cap=999)["k"]) <= debug_trace._DETAIL_MAX_LIST


def test_safe_detail_preserves_json_null_instead_of_inventing_none_string():
    safe = debug_trace._safe_detail({
        "reason": None,
        "nested": {"reason": None},
        "items": [None, "real"],
    })

    assert safe == {
        "reason": None,
        "nested": {"reason": None},
        "items": [None, "real"],
    }


def test_emit_payload_forwards_job_id_and_outcome_class(tee_primary, monkeypatch):
    """Both columns existed and were written, but nobody forwarded them.

    A green suite proved only that nothing regressed; it could not tell us the
    taxonomy was never populated.  This pins the forwarding itself, so removing
    it goes red instead of silently returning every row to the default.
    """
    from diagnostics import diagnostics_core

    class _Store:
        def __init__(self, uid): self.user_id = uid

    uid = _uid()
    db.upsert_user({"user_id": uid, "created_at": datetime.now(_ZONE).isoformat()})
    store = _Store(uid)
    try:
        debug_trace.set_enabled(store, True)
        diagnostics_core.emit_trace_event_payload(store, {"event": {
            "subsystem": "agent", "type": "agent.job.terminal", "status": "error",
            "job_id": "job-4491", "outcome_class": "safety_suppression",
        }})
        # An unknown class must degrade to the default rather than reach the
        # column, because this path also accepts untrusted resident payloads.
        diagnostics_core.emit_trace_event_payload(store, {"event": {
            "subsystem": "agent", "type": "agent.job.terminal", "status": "error",
            "job_id": "job-4492", "outcome_class": "not-a-real-class",
        }})
        # read_trace flushes the pending queue for this user before reading.
        debug_trace.read_trace(store)

        rows = {r["job_id"]: r for r in db.query_trace_events(user_id=uid)}
        assert rows["job-4491"]["outcome_class"] == "safety_suppression"
        assert rows["job-4492"]["outcome_class"] == debug_trace.TRACE_OUTCOME_DEFAULT
    finally:
        db.delete_trace_events_for_user(uid)
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_trace_survives_account_delete_and_remains_queryable_by_uid(tee_primary):
    uid = _uid()
    db.upsert_user({"user_id": uid, "created_at": datetime.now(_ZONE).isoformat()})
    try:
        db.insert_trace_events_strict(uid, [_event(ts=datetime.now(_ZONE).timestamp())])
        assert db.delete_user(uid) is True
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT count(*) FROM users WHERE user_id=%s", (uid,)
            ).fetchone() == (0,)
        rows = db.query_trace_events(user_id=uid)
        assert len(rows) == 1
        assert rows[0]["user_id"] == uid
        with registry._users_lock:
            registry._users[:] = [
                user for user in registry._users if user.get("user_id") != uid
            ]
        with bind(f"view=debug&mode=flat&user_id={uid}"):
            admin_payload = data_track._data_track_debug_payload()
        assert [event["user_id"] for event in admin_payload["events"]] == [uid]
        assert len(admin_payload["users"]) == 1
        deleted_user = admin_payload["users"][0]
        assert deleted_user["user_id"] == uid
        assert deleted_user["account_present"] is False
        assert deleted_user["events"] == 1
        assert deleted_user["last_ts"] == rows[0]["ts"]
        html = admin_core.page_html(f"view=debug&mode=flat&user_id={uid}")
        assert uid in html
        assert "agent.test" in html
    finally:
        db.delete_trace_events_for_user(uid)
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_clear_tombstone_rejects_delayed_batch_and_preserves_toggle(tee_primary):
    uid = _uid()
    cutoff = pytime.time()
    db.upsert_user({"user_id": uid, "created_at": datetime.now(_ZONE).isoformat()})
    try:
        db.patch_blob_strict(
            uid,
            "v1_flow_trace_enabled",
            {"enabled": True},
        )
        assert db.insert_trace_events_strict(
            uid, [_event(ts=cutoff - 10, trace_id="before")]
        ) == 1
        assert db.clear_trace_events_strict(
            uid,
            flag_kind="v1_flow_trace_enabled",
            cleared_at=cutoff,
            enabled_if_missing=True,
        ) == 1
        assert db.insert_trace_events_strict(
            uid, [_event(ts=cutoff - 1, trace_id="delayed-before")]
        ) == 0
        assert db.insert_trace_events_strict(
            uid, [_event(ts=cutoff + 1, trace_id="after")]
        ) == 1
        assert [row["trace_id"] for row in db.query_trace_events(user_id=uid)] == [
            "after"
        ]
        flag = db.get_blob_strict(uid, "v1_flow_trace_enabled")
        assert flag["enabled"] is True
        assert flag["cleared_at"] == cutoff
    finally:
        db.delete_trace_events_for_user(uid)
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_clear_tombstone_preserves_disabled_default_for_missing_flag(
    tee_primary, monkeypatch
):
    uid = _uid()
    cutoff = pytime.time()
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE_DEFAULT", "0")
    db.upsert_user({"user_id": uid, "created_at": datetime.now(_ZONE).isoformat()})
    try:
        assert db.clear_trace_events_strict(
            uid,
            flag_kind="v1_flow_trace_enabled",
            cleared_at=cutoff,
            enabled_if_missing=False,
        ) == 0
        assert db.get_blob_strict(uid, "v1_flow_trace_enabled") == {
            "enabled": False,
            "cleared_at": cutoff,
        }
    finally:
        db.delete_trace_events_for_user(uid)
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_default_is_red_then_owner_maintenance_recovers_and_expires(tee_primary, caplog):
    uid = _uid()
    today = datetime.now(_ZONE).date()
    stranded_day = today + timedelta(days=400)
    stale_day = today - timedelta(days=40)
    stranded_ts = datetime.combine(stranded_day, time(hour=12), tzinfo=_ZONE).timestamp()
    stale_ts = datetime.combine(stale_day, time(hour=12), tzinfo=_ZONE).timestamp()
    stranded_partition = f"trace_events_p{stranded_day:%Y%m%d}"
    try:
        db.insert_trace_events_strict(uid, [
            _event(ts=stranded_ts, trace_id="stranded"),
            _event(ts=stale_ts, trace_id="expired"),
        ])
        report = db.trace_events_partition_health()
        assert report["ok"] is False
        assert "default_partition_nonempty" in report["issues"]
        assert report["default_rows"] >= 2

        with caplog.at_level(logging.ERROR, logger="feedling.trace_events"):
            monitor._tick()
        assert "default_partition_nonempty" in caplog.text

        with psycopg.connect(os.environ["TEE_DATABASE_URL"]) as owner:
            fixed = partitions.maintain(owner, today=today)
        assert fixed["default_rows_before"] >= 2
        assert fixed["default_rows_after"] == 0
        assert fixed["moved_rows"] >= 1
        assert fixed["expired_default_rows"] >= 1
        with db.get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT trace_id,tableoid::regclass::text FROM trace_events "
                "WHERE user_id=%s ORDER BY trace_id",
                (uid,),
            ).fetchall()
        assert rows == [("stranded", stranded_partition)]

        # A repaired far-future outlier must not mask a hole in the near-term
        # rolling window: health measures consecutive coverage from today.
        missing_day = today + timedelta(days=2)
        missing_partition = f"trace_events_p{missing_day:%Y%m%d}"
        with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP TABLE {}").format(sql.Identifier(missing_partition))
            )
        hole = db.trace_events_partition_health()
        assert "partition_horizon_low" in hole["issues"]
        with psycopg.connect(os.environ["TEE_DATABASE_URL"]) as owner:
            partitions.maintain(owner, today=today)
    finally:
        db.delete_trace_events_for_user(uid)
        with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(
                    sql.Identifier(stranded_partition)
                )
            )


def test_monitor_has_its_own_singleton_leader(monkeypatch):
    calls = []
    monkeypatch.setattr(
        core_leader,
        "run_singleton",
        lambda name, start_fn: calls.append((name, start_fn)),
    )
    lifespan_mod._start_trace_events_monitor_leader()
    assert calls == [("trace-events-monitor", monitor.start)]


def test_capacity_budget_is_an_independent_red_signal(tee_primary, monkeypatch):
    monkeypatch.setenv("FEEDLING_TRACE_EVENTS_STORAGE_BUDGET_BYTES", "1")
    report = db.trace_events_partition_health()
    assert "storage_budget_exceeded" in report["issues"]


def test_durable_at_risk_counter_is_red_within_monitor_cadence(
    tee_primary, monkeypatch,
):
    today = datetime.now(_ZONE).date().isoformat()
    writer = "t184-at-risk-" + uuid.uuid4().hex
    try:
        db.upsert_trace_write_stats([(
            today, writer, "agent", "agent.test", "heartbeat",
            0, 0, 0, 0, 2, 200, pytime.time(),
        )])
        report = db.trace_events_partition_health()
        assert "trace_write_at_risk" in report["issues"]
        assert report["at_risk_events_today"] >= 2
        monkeypatch.delenv("FEEDLING_TRACE_EVENTS_MONITOR_INTERVAL_SEC", raising=False)
        assert monitor._interval() == 60.0
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM trace_write_stats WHERE writer_id=%s", (writer,))


def test_monitor_start_spawns_daemon_thread(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, *, target, daemon, name):
            started.update(target=target, daemon=daemon, name=name)

        def start(self):
            started["started"] = True

    monkeypatch.setattr(monitor.threading, "Thread", FakeThread)
    monitor.start()
    assert started == {
        "target": monitor._loop,
        "daemon": True,
        "name": "trace-events-monitor",
        "started": True,
    }
