"""T138 block-0 persistent trace-rate ruler."""
from __future__ import annotations

import queue
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
import debug_trace
import conftest


def _reset_stats(monkeypatch, *, pid: int = 4242) -> None:
    monkeypatch.setattr(debug_trace.os, "getpid", lambda: pid)
    with debug_trace._stats_lock:
        debug_trace._stats_pid = -1
        debug_trace._stats_writer_id = ""
        debug_trace._stats_process_started_at = 0.0
        debug_trace._stats_totals.clear()
        debug_trace._stats_flushed.clear()
        debug_trace._stats_first_seen.clear()
        debug_trace._stats_flush_failures_total = 0
        debug_trace._stats_flush_consecutive_failures = 0
        debug_trace._stats_last_flush_failure_at = 0.0
        debug_trace._stats_last_flush_success_at = 0.0
        debug_trace._stats_last_flush_error = ""
        debug_trace._stats_last_failure_warning_at = 0.0


def _event(*, lane: str | None = "chat", ts: float = 1_800_000_000.0) -> dict:
    detail = {} if lane is None else {"lane": lane}
    return {
        "ts": ts,
        "subsystem": "agent",
        "type": "agent.model.call.done",
        "detail": detail,
        "status": "ok",
    }


def _insert_health(
    writer_id: str,
    *,
    started_at: float,
    last_success_at: float,
    last_failure_at: float | None = None,
    failures_total: int = 0,
    max_consecutive_failures: int = 0,
    dirty_rows: int = 0,
    stopped_at: float | None = None,
) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO trace_write_stats_health "
            "(writer_id,process_started_at,last_success_at,last_failure_at,"
            "failures_total,max_consecutive_failures,dirty_rows,stopped_at) "
            "VALUES (%s,to_timestamp(%s),to_timestamp(%s),to_timestamp(%s),"
            "%s,%s,%s,to_timestamp(%s)) "
            "ON CONFLICT (writer_id) DO UPDATE SET "
            "process_started_at=EXCLUDED.process_started_at,"
            "last_success_at=EXCLUDED.last_success_at,"
            "last_failure_at=EXCLUDED.last_failure_at,"
            "failures_total=EXCLUDED.failures_total,"
            "max_consecutive_failures=EXCLUDED.max_consecutive_failures,"
            "dirty_rows=EXCLUDED.dirty_rows,stopped_at=EXCLUDED.stopped_at",
            (
                writer_id,
                started_at,
                last_success_at,
                last_failure_at,
                failures_total,
                max_consecutive_failures,
                dirty_rows,
                stopped_at,
            ),
        )


def _delete_health(*writer_ids: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM trace_write_stats_health WHERE writer_id=ANY(%s)",
            (list(writer_ids),),
        )


def test_stats_retry_uses_same_absolute_total_and_unknown_lane(monkeypatch):
    _reset_stats(monkeypatch)
    attempts: list[tuple[list[tuple], tuple | None]] = []

    def flaky(rows, *, writer_health=None):
        attempts.append((rows, writer_health))
        if len(attempts) == 1:
            raise RuntimeError("commit outcome unknown")

    monkeypatch.setattr(debug_trace.db, "upsert_trace_write_stats", flaky)
    event = _event(lane=None)
    debug_trace._record_trace_stats([event], outcome="persisted")
    debug_trace._flush_trace_stats()
    debug_trace._flush_trace_stats()

    assert len(attempts) == 2
    assert attempts[0][0] == attempts[1][0], "retry must replay an absolute total"
    row = attempts[-1][0][0]
    assert row[1].startswith("4242:")
    assert row[2:5] == ("agent", "agent.model.call.done", "unknown")
    assert row[5] == 1
    assert row[6] == debug_trace._stats_event_bytes(event)
    assert row[7:11] == (0, 0, 0, 0)
    assert attempts[1][1][2:5] == (
        debug_trace._stats_last_flush_failure_at,
        1,
        1,
    )
    health = debug_trace.trace_stats_health()
    assert health["dirty_rows"] == 0
    assert health["failures_total"] == 1
    assert health["consecutive_failures"] == 0
    assert health["last_success_at"] is not None


def test_idle_writer_heartbeats_without_rewriting_more_frequently_than_interval(
    monkeypatch,
):
    _reset_stats(monkeypatch)
    now = [1_800_000_000.0]
    monkeypatch.setattr(debug_trace.time, "time", lambda: now[0])
    calls = []
    monkeypatch.setattr(
        debug_trace.db,
        "upsert_trace_write_stats",
        lambda rows, *, writer_health=None: calls.append((rows, writer_health)),
    )

    debug_trace._flush_trace_stats()
    debug_trace._flush_trace_stats()
    now[0] += debug_trace._STATS_HEARTBEAT_SEC
    debug_trace._flush_trace_stats()

    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == []
    assert calls[0][1][0].startswith("4242:")


def test_writer_and_report_share_one_heartbeat_interval():
    assert (
        debug_trace._STATS_HEARTBEAT_SEC
        == db.TRACE_WRITE_STATS_HEARTBEAT_SEC
    )


def test_graceful_stop_tombstones_only_an_existing_writer(monkeypatch):
    _reset_stats(monkeypatch)
    stopped = []
    monkeypatch.setattr(
        debug_trace.db,
        "upsert_trace_write_stats",
        lambda _rows, *, writer_health=None: None,
    )
    monkeypatch.setattr(
        debug_trace.db,
        "stop_trace_write_stats_writer",
        lambda writer_id: stopped.append(writer_id),
    )

    debug_trace.stop_trace_stats_writer()
    assert stopped == []

    debug_trace._record_trace_stats([_event()], outcome="persisted")
    debug_trace.stop_trace_stats_writer()
    assert len(stopped) == 1
    assert stopped[0].startswith("4242:")


def test_failed_final_flush_must_not_publish_a_graceful_tombstone(monkeypatch):
    _reset_stats(monkeypatch)
    stopped = []
    monkeypatch.setattr(
        debug_trace.db,
        "upsert_trace_write_stats",
        lambda _rows, *, writer_health=None: (_ for _ in ()).throw(
            RuntimeError("health row rejected")
        ),
    )
    monkeypatch.setattr(
        debug_trace.db,
        "stop_trace_write_stats_writer",
        lambda writer_id: stopped.append(writer_id),
    )
    debug_trace._record_trace_stats([_event()], outcome="persisted")

    debug_trace.stop_trace_stats_writer()

    assert stopped == []
    health = debug_trace.trace_stats_health()
    assert health["dirty_rows"] == 1
    assert health["consecutive_failures"] == 1


def test_queue_full_is_the_only_known_drop_source(monkeypatch):
    _reset_stats(monkeypatch)
    captured: list[tuple[list[dict], str]] = []

    class FullQueue:
        def put_nowait(self, _item):
            raise queue.Full

    monkeypatch.setattr(debug_trace, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(debug_trace, "_event_queue", FullQueue())
    monkeypatch.setattr(debug_trace, "_pending_by_uid", {})
    monkeypatch.setattr(debug_trace, "_dropped_by_uid", {})
    monkeypatch.setattr(
        debug_trace,
        "_record_trace_stats",
        lambda events, *, outcome: captured.append((events, outcome)),
    )

    event = _event(lane="wake")
    debug_trace._enqueue("usr_full", event)

    assert captured == [([event], "known_drop")]
    assert debug_trace._pending_by_uid == {}
    assert debug_trace._take_dropped("usr_full") == 1


def test_flush_exception_is_at_risk_and_restores_only_queue_marker(monkeypatch):
    monkeypatch.setattr(debug_trace, "is_enabled", lambda _store: True)
    monkeypatch.setattr(debug_trace, "verbose_enabled", lambda _store: False)
    monkeypatch.setattr(debug_trace, "_dropped_by_uid", {"usr_risk": 2})
    monkeypatch.setattr(debug_trace, "_at_risk_by_uid", {})
    captured: list[tuple[list[dict], str]] = []
    monkeypatch.setattr(
        debug_trace,
        "_record_trace_stats",
        lambda events, *, outcome: captured.append((events, outcome)),
    )
    monkeypatch.setattr(
        debug_trace.db,
        "append_blob_events_strict",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    debug_trace._append_events("usr_risk", [_event()])

    assert len(captured) == 1
    events, outcome = captured[0]
    assert outcome == "at_risk"
    assert len(events) == 2  # queue-full marker plus the attempted event
    assert events[0]["type"] == "debug_trace.dropped"
    assert debug_trace._take_dropped("usr_risk") == 2
    assert debug_trace._take_at_risk_marker("usr_risk") == 2


def test_next_success_emits_at_risk_marker_without_calling_it_a_drop(monkeypatch):
    monkeypatch.setattr(debug_trace, "is_enabled", lambda _store: True)
    monkeypatch.setattr(debug_trace, "verbose_enabled", lambda _store: False)
    monkeypatch.setattr(debug_trace, "_dropped_by_uid", {})
    monkeypatch.setattr(debug_trace, "_at_risk_by_uid", {"usr_risk": 3})
    written: list[dict] = []
    monkeypatch.setattr(
        debug_trace.db,
        "append_blob_events_strict",
        lambda _uid, _kind, events, **_kwargs: written.extend(events),
    )
    monkeypatch.setattr(debug_trace, "_record_trace_stats", lambda *_args, **_kwargs: None)

    debug_trace._append_events("usr_risk", [_event()])

    assert written[0]["type"] == "debug_trace.at_risk"
    assert written[0]["detail"] == {"at_risk": 3}
    assert "may or may not have committed" in written[0]["explain"]
    assert all(event["type"] != "debug_trace.dropped" for event in written)


def test_absolute_db_upsert_is_restart_safe_and_retry_idempotent(backend_env):
    day = "2027-01-15"
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_write_stats WHERE day=%s", (day,))
    first_seen = 1_800_000_000.0
    base = (
        day, "writer-a", "agent", "call.done", "chat",
        3, 300, 1, 100, 2, 200, first_seen,
    )
    db.upsert_trace_write_stats([base])
    db.upsert_trace_write_stats([base])
    db.upsert_trace_write_stats([(
        day, "writer-a", "agent", "call.done", "chat",
        5, 500, 1, 100, 2, 200, first_seen,
    )])
    # A restarted process owns a new writer id and is additive at report time.
    db.upsert_trace_write_stats([(
        day, "writer-b", "agent", "call.done", "chat",
        7, 700, 0, 0, 0, 0, first_seen + 1,
    )])

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT writer_id,persisted_events,persisted_bytes,known_drop_events,"
            "at_risk_events FROM trace_write_stats WHERE day=%s ORDER BY writer_id",
            (day,),
        ).fetchall()
        conn.execute("DELETE FROM trace_write_stats WHERE day=%s", (day,))
    assert rows == [
        ("writer-a", 5, 500, 1, 2),
        ("writer-b", 7, 700, 0, 0),
    ]


def test_health_upsert_recovers_failure_history_and_graceful_stop(backend_env):
    writer = "writer-health-upsert"
    _delete_health(writer)
    started_at = 1_800_000_000.0
    failed_at = started_at + 10

    db.upsert_trace_write_stats([], writer_health=(
        writer, started_at, None, 0, 0, 0,
    ))
    db.upsert_trace_write_stats([], writer_health=(
        writer, started_at, failed_at, 4, 4, 0,
    ))
    db.upsert_trace_write_stats([], writer_health=(
        writer, started_at, None, 4, 0, 3,
    ))
    db.stop_trace_write_stats_writer(writer)

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT extract(epoch FROM process_started_at),"
            "extract(epoch FROM last_failure_at),failures_total,"
            "max_consecutive_failures,dirty_rows,stopped_at IS NOT NULL "
            "FROM trace_write_stats_health WHERE writer_id=%s",
            (writer,),
        ).fetchone()
    assert row == (started_at, failed_at, 4, 4, 3, False)

    db.upsert_trace_write_stats([], writer_health=(
        writer, started_at, None, 4, 0, 0,
    ))
    db.stop_trace_write_stats_writer(writer)
    with db.get_pool().connection() as conn:
        stopped = conn.execute(
            "SELECT stopped_at IS NOT NULL FROM trace_write_stats_health "
            "WHERE writer_id=%s",
            (writer,),
        ).fetchone()[0]
    assert stopped is True
    _delete_health(writer)


def test_real_ring_append_reaches_persistent_rate_counter(backend_env, monkeypatch):
    uid = "usr_trace_rate_plumbing"
    conftest.seed_user(uid)
    _reset_stats(monkeypatch, pid=5252)
    monkeypatch.setattr(debug_trace, "is_enabled", lambda _store: True)
    monkeypatch.setattr(debug_trace, "verbose_enabled", lambda _store: False)
    event = _event(lane="wake")

    debug_trace._append_events(uid, [event])
    debug_trace._flush_trace_stats()

    with debug_trace._stats_lock:
        writer_id = debug_trace._stats_writer_id
    day = datetime.fromtimestamp(event["ts"], ZoneInfo("Asia/Shanghai")).date().isoformat()
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT persisted_events,persisted_bytes,known_drop_events,at_risk_events "
            "FROM trace_write_stats WHERE day=%s AND writer_id=%s "
            "AND subsystem='agent' AND event_type='agent.model.call.done' "
            "AND lane='wake'",
            (day, writer_id),
        ).fetchone()
        conn.execute(
            "DELETE FROM trace_write_stats WHERE writer_id=%s", (writer_id,)
        )
    assert row == (1, debug_trace._stats_event_bytes(event), 0, 0)


def test_measurement_reports_peak_not_average_and_three_precision_classes(
    backend_env,
):
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2027, 1, 15, 12, tzinfo=zone).timestamp()
    first_seen = now - 8 * 86400
    days = ("2027-01-14", "2027-01-15")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_write_stats WHERE day=ANY(%s)", (list(days),))
    db.upsert_trace_write_stats([
        (days[0], "writer-a", "agent", "a", "chat", 10, 1000, 1, 100, 2, 200, first_seen),
        (days[0], "writer-b", "agent", "a", "chat", 5, 500, 0, 0, 0, 0, first_seen),
        (days[1], "writer-a", "route", "b", "wake", 20, 2500, 0, 0, 1, 900, first_seen),
    ])
    _delete_health("writer-a", "writer-b")
    uncovered = db.trace_write_stats_measurement(days=7, now_epoch=now)
    assert uncovered["measurement_ready"] is False
    assert uncovered["writer_health"]["unregistered_writer_ids"] == [
        "writer-a",
        "writer-b",
    ]
    _insert_health(
        "writer-a", started_at=first_seen, last_success_at=now,
    )

    # A mixed fleet is the production shape that makes the unregistered guard
    # carry weight: one healthy writer must not hide another writer whose
    # counter rows exist without a health registration.
    partial = db.trace_write_stats_measurement(days=7, now_epoch=now)
    assert partial["writer_health"]["unregistered_writer_ids"] == ["writer-b"]
    assert partial["writer_health"]["complete"] is False
    assert partial["measurement_ready"] is False

    _insert_health(
        "writer-b", started_at=first_seen, last_success_at=now,
    )

    report = db.trace_write_stats_measurement(days=7, now_epoch=now)

    assert report["measurement_ready"] is True
    assert report["capacity_basis"] == "daily_peak_not_average"
    assert report["peak"]["day"] == days[1]
    assert report["peak"]["persisted_bytes"] == 2500
    assert report["peak"]["conservative_bytes"] == 3400
    assert "average" not in report
    assert len(report["breakdown"]) == 2
    assert report["writer_health"]["complete"] is True
    assert report["writer_health"]["writers"][0]["dirty_rows"] == 0
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_write_stats WHERE day=ANY(%s)", (list(days),))
    _delete_health("writer-a", "writer-b")


def test_short_display_window_cannot_bypass_seven_day_readiness_gate(
    backend_env,
):
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2027, 2, 15, 12, tzinfo=zone).timestamp()
    first_seen = now - 25 * 3600
    day = "2027-02-15"
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_write_stats WHERE day=%s", (day,))
    db.upsert_trace_write_stats([(
        day, "writer-gate", "agent", "gate", "chat",
        1, 100, 0, 0, 0, 0, first_seen,
    )])
    _insert_health(
        "writer-gate", started_at=first_seen, last_success_at=now,
    )

    one_day = db.trace_write_stats_measurement(days=1, now_epoch=now)
    seven_days = db.trace_write_stats_measurement(days=7, now_epoch=now)

    for report in (one_day, seven_days):
        assert report["measurement_elapsed_hours"] == 25.0
        assert report["minimum_measurement_hours"] == 168
        assert report["measurement_ready"] is False
    assert one_day["window_days"] == 1
    assert seven_days["window_days"] == 7
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_write_stats WHERE day=%s", (day,))
    _delete_health("writer-gate")


def test_stale_success_heartbeat_blocks_readiness_and_hides_old_dirty_zero(
    backend_env,
):
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2027, 3, 15, 12, tzinfo=zone).timestamp()
    first_seen = now - 8 * 86400
    day = "2027-03-15"
    writer = "writer-stale"
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_write_stats WHERE day=%s", (day,))
    _delete_health(writer)
    db.upsert_trace_write_stats([(
        day, writer, "agent", "stale", "chat",
        1, 100, 0, 0, 0, 0, first_seen,
    )])
    _insert_health(
        writer,
        started_at=first_seen,
        last_success_at=now - 3 * 3600,
        last_failure_at=now - 3 * 3600,
        failures_total=7,
        max_consecutive_failures=7,
        dirty_rows=0,
    )

    report = db.trace_write_stats_measurement(days=7, now_epoch=now)

    assert report["measurement_elapsed_hours"] == 192.0
    assert report["measurement_ready"] is False
    assert report["writer_health"]["stale_writer_ids"] == [writer]
    health = report["writer_health"]["writers"][0]
    assert health["status"] == "stale"
    assert health["dirty_rows"] is None
    assert health["dirty_rows_status"] == "unknown_stale"
    assert health["missed_heartbeat_intervals"] == 180
    assert health["failures_total"] == 7

    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_write_stats WHERE day=%s", (day,))
    _delete_health(writer)


def test_gracefully_stopped_writer_does_not_become_stale(backend_env):
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2027, 4, 15, 12, tzinfo=zone).timestamp()
    first_seen = now - 8 * 86400
    day = "2027-04-15"
    writer = "writer-stopped"
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_write_stats WHERE day=%s", (day,))
    _delete_health(writer)
    db.upsert_trace_write_stats([(
        day, writer, "route", "stopped", "wake",
        1, 100, 0, 0, 0, 0, first_seen,
    )])
    _insert_health(
        writer,
        started_at=first_seen,
        last_success_at=now - 3600,
        stopped_at=now - 3599,
    )

    report = db.trace_write_stats_measurement(days=7, now_epoch=now)

    assert report["measurement_ready"] is True
    assert report["writer_health"]["writers"][0]["status"] == "stopped"
    assert report["writer_health"]["stale_writer_ids"] == []

    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_write_stats WHERE day=%s", (day,))
    _delete_health(writer)


def test_reader_clock_behind_the_database_is_not_a_missing_registration(
    backend_env,
):
    """A healthy writer must survive the health window's upper bound.

    ``last_success_at`` carries the DATABASE clock; the report's ``now`` carries
    the reader's.  When the reader lags, a heartbeat written moments ago sits in
    the reader's future.  Excluding it would delete the writer's health row from
    the report and surface it as ``unregistered`` — the gate would then claim a
    registration failure that never happened, on the healthiest writer there is.
    """
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2027, 5, 15, 12, tzinfo=zone).timestamp()
    first_seen = now - 8 * 86400
    day = "2027-05-15"
    writer = "writer-ahead-of-reader"
    try:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM trace_write_stats WHERE day=%s", (day,))
        _delete_health(writer)
        db.upsert_trace_write_stats([(
            day, writer, "agent", "skew", "chat",
            1, 100, 0, 0, 0, 0, first_seen,
        )])
        # The database stamped this heartbeat half a second "after" the reader's
        # clock — the ordinary consequence of two hosts, not a fault.
        _insert_health(
            writer, started_at=first_seen, last_success_at=now + 0.5,
        )

        report = db.trace_write_stats_measurement(days=7, now_epoch=now)

        assert report["writer_health"]["unregistered_writer_ids"] == []
        assert [w["writer_id"] for w in report["writer_health"]["writers"]] == [
            writer,
        ]
        assert report["writer_health"]["writers"][0]["status"] == "healthy"
        assert report["writer_health"]["complete"] is True
        assert report["measurement_ready"] is True
    finally:
        # Cleanup belongs in ``finally``: a failure that leaves health rows
        # behind cascades into every later fixed-``now`` case in this file and
        # buries the one assertion that actually broke.
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM trace_write_stats WHERE day=%s", (day,))
        _delete_health(writer)
