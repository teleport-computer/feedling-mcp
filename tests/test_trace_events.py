"""T184/A: additive TEE trace table, retention machinery, and read contract."""

from __future__ import annotations

import logging
import os
import sys
import time as pytime
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


def test_outcome_classifier_is_shared_with_the_ops_dashboard():
    """The dashboard and the trace layer must not classify the same job apart.

    Classification used to live only inside the dashboard query, so nothing
    stopped a second reader from deriving its own answer and calling a
    deliberate suppression a real failure on one screen but not the other.
    """
    from model_api_runtime.v2 import jobs_store

    for code in jobs_store.CONTROL_OUTCOME_CODES:
        assert jobs_store.terminal_outcome_class(code) == "control"
    for code in jobs_store.SAFETY_SUPPRESSION_CODES:
        assert jobs_store.terminal_outcome_class(code) == "safety_suppression"
    for code in jobs_store.TIMEOUT_OUTCOME_CODES:
        assert jobs_store.terminal_outcome_class(code) == "timeout"
    # An unclassified code stays operational rather than being guessed into a
    # bucket that would quietly excuse it from the failure rate.
    assert jobs_store.terminal_outcome_class("something_new") == "operational_failure"
    assert jobs_store.terminal_outcome_class("") == "operational_failure"
    # Whatever it returns must be a member of the one shared vocabulary.
    every_code = (
        jobs_store.CONTROL_OUTCOME_CODES
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
