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
        debug_trace._stats_totals.clear()
        debug_trace._stats_flushed.clear()
        debug_trace._stats_first_seen.clear()


def _event(*, lane: str | None = "chat", ts: float = 1_800_000_000.0) -> dict:
    detail = {} if lane is None else {"lane": lane}
    return {
        "ts": ts,
        "subsystem": "agent",
        "type": "agent.model.call.done",
        "detail": detail,
        "status": "ok",
    }


def test_stats_retry_uses_same_absolute_total_and_unknown_lane(monkeypatch):
    _reset_stats(monkeypatch)
    attempts: list[list[tuple]] = []

    def flaky(rows):
        attempts.append(rows)
        if len(attempts) == 1:
            raise RuntimeError("commit outcome unknown")

    monkeypatch.setattr(debug_trace.db, "upsert_trace_write_stats", flaky)
    event = _event(lane=None)
    debug_trace._record_trace_stats([event], outcome="persisted")
    debug_trace._flush_trace_stats()
    debug_trace._flush_trace_stats()

    assert len(attempts) == 2
    assert attempts[0] == attempts[1], "retry must replay an absolute total"
    row = attempts[-1][0]
    assert row[1].startswith("4242:")
    assert row[2:5] == ("agent", "agent.model.call.done", "unknown")
    assert row[5] == 1
    assert row[6] == debug_trace._stats_event_bytes(event)
    assert row[7:11] == (0, 0, 0, 0)


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

    report = db.trace_write_stats_measurement(days=7, now_epoch=now)

    assert report["measurement_ready"] is True
    assert report["capacity_basis"] == "daily_peak_not_average"
    assert report["peak"]["day"] == days[1]
    assert report["peak"]["persisted_bytes"] == 2500
    assert report["peak"]["conservative_bytes"] == 3400
    assert "average" not in report
    assert len(report["breakdown"]) == 2
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM trace_write_stats WHERE day=ANY(%s)", (list(days),))


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
