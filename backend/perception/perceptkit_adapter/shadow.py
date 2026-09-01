"""Run PerceptKit alongside the live path and record what it would have done.

Nothing here changes what the user sees. The existing ingest keeps doing
everything it did before, byte for byte; this takes the same already-decrypted
snapshot, pushes it through the kit, writes the result to the kit's own
tables, and then compares the two conclusions field by field (see
``compare.py``) into a running tally.

That last step is the point. Running the kit beside the live path only proves
it does not crash on real data; the question worth asking is whether it
reaches the same conclusions, and where it does not.

## Why shadow first rather than switching

The kit has 500 tests and has never seen one real report. Tests are written
from what we believe the data looks like; a shadow run is the only way to find
out where that belief is wrong while being wrong is still free.

## Three rules this module lives by

**It can never break a report.** Every failure is caught and counted. A
perception report that returns 500 because a shadow comparison tripped would
be a self-inflicted outage over a diagnostic.

**It costs no extra enclave calls.** It is handed ``storage_items``, which the
live path has already decrypted. One report is roughly seven enclave decrypts
already; doubling that to watch ourselves would be its own incident.

**It is bounded.** Wall-clock is capped and the writes are capped. The lesson
from 2026-07-07 is on the record: a few extra seconds added to the shared
report path took cloud chat down with it, and that change was additive too.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)

#: Kill switch. Default ON, per the workspace rule that a flag is a rollback
#: lever and not a gate someone has to remember to open -- a default-off flag
#: is how code ships while the feature does not.
#:
#: Turn it off if perception reports slow down or the shadow starts filling
#: logs. Nothing user-visible changes either way.
ENV_FLAG = "FEEDLING_PERCEPTKIT_SHADOW"

#: Give up past this many seconds and count it. The live path has already
#: finished by the time we run, but it has not returned yet -- time spent here
#: is time the client waits.
BUDGET_SEC = 1.5


def enabled() -> bool:
    return (os.environ.get(ENV_FLAG, "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _report_id(user_id: str, items: Sequence[Mapping[str, Any]],
               client_ts: Any) -> str:
    from .ios_report import report_id_for
    return report_id_for({"context_snapshot": list(items),
                          "client_ts": client_ts})


def _live_state(user_id: str) -> dict:
    """The live path's current projection for this user.

    Read through the store rather than the read API: the read API layers
    freshness rules and presentation on top, and comparing the kit against a
    presented value would credit or blame it for decisions it never made.
    """
    from .. import store
    try:
        return store.get_state(user_id) or {}
    except Exception:                              # noqa: BLE001
        return {}


#: kit 负责唤醒。**默认开**，出问题设成 0 立刻回到老路判定 —— 和
#: `FEEDLING_PERCEPTKIT_PRIMARY` 一样是回滚闸，不是等人来开的门。
#:
#: 两条路**不能同时投递**：老路和 kit 会为同一件事各叫一次，用户被提醒两遍。
#: 所以这个开关是「谁来投」的单选，不是「多加一路」的加法。
WAKE_ENV_FLAG = "FEEDLING_PERCEPTKIT_WAKES"


def wakes_enabled() -> bool:
    """kit 是不是负责投递唤醒。

    额外一条：**store 被换成测试假实现时不投**。和快照读取那边同一个理由 ——
    唤醒是有副作用的（写队列、写事件流），一个自以为完全隔离的测试不该因为
    另一条路的真库写入而时红时绿。
    """
    if (os.environ.get(WAKE_ENV_FLAG, "1") or "1").strip().lower() in (
            "0", "false", "no", "off"):
        return False
    from .. import store
    if getattr(store, "__name__", "") != "perception.store":
        return False
    return enabled()


def _kit(storage):
    """带上规则和 WakePort 的 kit。

    投递走 ``dispatch=True`` 同步做。kit 的默认是留在发件箱、由 worker 去投，
    理由是别把 agent runtime 的延迟叠到上报接口上 —— io 这边不适用：我们投的
    是**排队**（写一条 job），不是等 agent 想完话，耗时毫秒级。事件仍然先落
    发件箱再投，崩溃了下次还能补投。
    """
    from perceptkit.kit import PerceptionKit
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS

    from .wake_port import FeedlingWakePort
    from .wake_rules import wake_definitions

    on = wakes_enabled()
    return PerceptionKit(
        storage=storage,
        signals=MINIMAL_SIGNALS,
        wake=FeedlingWakePort() if on else None,
        definitions=wake_definitions() if on else (),
    )


def _live_timezone(user_id: str) -> str | None:
    """The user's timezone as the live path last recorded it.

    The snapshot carries its own zone in the report; these other producers do
    not send one at all. Falling back to the UTC offset in a timestamp is not
    equivalent -- an offset is not a zone, and the day a DST transition happens
    gets attributed wrong, silently. The live state has the real zone id
    because a recent snapshot put it there.
    """
    return (_live_state(user_id).get("timezone") or {}).get("v") or None


def _tally(findings) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        out[f.verdict] = out.get(f.verdict, 0) + 1
    return out


def observe(
    user_id: str,
    storage_items: Sequence[Mapping[str, Any]],
    *,
    client_ts: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Push one already-decrypted snapshot through the kit. Never raises.

    Returns a small summary for logging and tests. The caller is expected to
    ignore it.
    """
    summary: dict[str, Any] = {"ran": False}
    if not enabled() or not storage_items:
        return summary

    started = time.monotonic()
    try:
        import psycopg
        from perceptkit.contracts import IngestContext
        from perceptkit.kit import PerceptionKit
        from perceptkit.manifest.minimal import MINIMAL_SIGNALS

        from .. import perceptkit_adapter as _pkg  # noqa: F401
        from .ios_report import to_envelope
        from .storage import PostgresStorage

        received = now or datetime.now(timezone.utc)
        envelope = to_envelope(
            {"context_snapshot": list(storage_items), "client_ts": client_ts},
            occurred_at=received.isoformat(),
        )
        if not envelope["observations"]:
            return summary

        # `import db`, not `from backend import db`: backend/ is on sys.path,
        # which is how every other module here reaches it. The wrong spelling
        # raises ImportError, gets swallowed by the caller's guard, and the
        # shadow silently never runs -- which is exactly what happened.
        import db

        from . import compare as _compare

        signals = {o["signal"] for o in envelope["observations"]}
        with db.get_pool().connection() as conn:
            conn.autocommit = True
            storage = PostgresStorage(conn)
            kit = _kit(storage)
            outcome = kit.ingest(envelope,
                                 context=IngestContext(user_id, received),
                                 dispatch=wakes_enabled())
            # The comparison, which is the reason the shadow exists. It runs
            # against the live state *after* the live path has written it --
            # the caller does that before handing control here -- so both
            # sides have seen exactly the same report.
            findings = _compare.compare(
                _live_state(user_id),
                storage.get_current(subject_id=user_id, signals=sorted(signals)),
                signals=signals,
            )
            _compare.record(conn, user_id, findings, now=received,
                            report_id=envelope["report_id"])

        summary = {
            "ran": True,
            "report_id": envelope["report_id"],
            "observations": len(envelope["observations"]),
            "applied": len(outcome.applied),
            "rejected": len(outcome.rejected),
            "duplicates": len(outcome.duplicates),
            "conflicts": len(outcome.conflicts),
            "events": len(outcome.events),
            # Warnings are where the interesting news is: unit mismatches,
            # missing timezones, fields the manifest never declared. A shadow
            # run that reports zero warnings on real data is more likely to be
            # broken than correct.
            "warnings": list(outcome.warnings)[:20],
            "rejections": [
                {"signal": envelope["observations"][i]["signal"],
                 "reasons": list(reasons)}
                for i, reasons in list(outcome.rejected)[:20]
            ],
            # The verdict tally for this report. `differ` and `only_live` are
            # the two that mean the kit would have told the user something
            # different from what the live path told them.
            "verdicts": _tally(findings),
            "ms": round((time.monotonic() - started) * 1000, 1),
        }
        if summary["ms"] > BUDGET_SEC * 1000:
            log.warning("perceptkit shadow over budget: %sms", summary["ms"])
        log.info("perceptkit shadow %s", summary)
    except Exception as exc:                      # noqa: BLE001 -- deliberate
        # A diagnostic must never be the reason a user's report fails.
        summary = {"ran": False, "error": f"{type(exc).__name__}: {exc}"}
        log.warning("perceptkit shadow failed (report unaffected): %s", exc)
    return summary


def _run(user_id: str, envelope: Mapping[str, Any], *,
         received: datetime, compare_signals: bool = True) -> dict[str, Any]:
    """Push one envelope through the kit and record the comparison.

    The single guarded path every entry point below goes through, so the three
    rules in the module docstring hold for all of them rather than for whichever
    one they were written on.
    """
    from perceptkit.contracts import IngestContext
    from perceptkit.kit import PerceptionKit
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS

    from . import compare as _compare
    from .storage import PostgresStorage

    import db

    signals = {o["signal"] for o in envelope["observations"]}
    with db.get_pool().connection() as conn:
        conn.autocommit = True
        storage = PostgresStorage(conn)
        kit = _kit(storage)
        outcome = kit.ingest(envelope, context=IngestContext(user_id, received),
                             dispatch=wakes_enabled())
        findings = []
        if compare_signals:
            findings = _compare.compare(
                _live_state(user_id),
                storage.get_current(subject_id=user_id, signals=sorted(signals)),
                signals=signals,
            )
            _compare.record(conn, user_id, findings, now=received,
                            report_id=envelope["report_id"])
    return {
        "ran": True,
        "report_id": envelope["report_id"],
        "producer": envelope["producer"],
        "observations": len(envelope["observations"]),
        "applied": len(outcome.applied),
        "rejected": len(outcome.rejected),
        "duplicates": len(outcome.duplicates),
        "events": len(outcome.events),
        "warnings": list(outcome.warnings)[:20],
        "rejections": [
            {"signal": envelope["observations"][i]["signal"], "reasons": list(r)}
            for i, r in list(outcome.rejected)[:20]
        ],
        "verdicts": _tally(findings),
    }


def _guarded(name: str, build, user_id: str) -> dict[str, Any]:
    """Run one entry point. Never raises; always reports why it did not run.

    The silent-failure lesson is on the record: the shadow spent a stretch not
    running at all because of an import spelling, and said nothing, because the
    caller's guard swallowed it. A shadow that quietly stops and a shadow that
    runs and finds nothing look identical from outside.
    """
    if not enabled():
        return {"ran": False, "reason": "disabled"}
    started = time.monotonic()
    try:
        summary = build()
        if summary is None:
            return {"ran": False, "reason": "nothing_to_observe"}
        summary["ms"] = round((time.monotonic() - started) * 1000, 1)
        if summary["ms"] > BUDGET_SEC * 1000:
            log.warning("perceptkit shadow (%s) over budget: %sms",
                        name, summary["ms"])
        log.info("perceptkit shadow %s", summary)
        return summary
    except Exception as exc:                       # noqa: BLE001 -- deliberate
        log.warning("perceptkit shadow (%s) failed (request unaffected): %s",
                    name, exc)
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"}


def observe_photo(user_id: str, photo_id: str, *,
                  occurred_at: Any, now: datetime | None = None) -> dict[str, Any]:
    """One confirmed photo. Called after the pixels are durably stored --
    a photo whose storage failed is not a photo the user added."""
    def build():
        from .events import photo_envelope
        received = now or datetime.now(timezone.utc)
        return _run(user_id,
                    photo_envelope(photo_id, occurred_at=occurred_at,
                                   timezone_id=_live_timezone(user_id)),
                    received=received)
    return _guarded("photo", build, user_id)


def observe_device_event(user_id: str, event: Mapping[str, Any], *,
                         occurred_at: Any, now: datetime | None = None) -> dict[str, Any]:
    """A screen change or an unlock after absence."""
    def build():
        from .events import device_event_envelope
        envelope = device_event_envelope(
            event, occurred_at=occurred_at, timezone_id=_live_timezone(user_id))
        if envelope is None:
            return None
        return _run(user_id, envelope, received=now or datetime.now(timezone.utc))
    return _guarded("device_event", build, user_id)


def observe_app_event(user_id: str, app: str, category: Any, *,
                      action: str, occurred_at: Any,
                      now: datetime | None = None) -> dict[str, Any]:
    """One app open or close from the iOS Shortcut automations."""
    def build():
        from .events import app_event_envelope
        return _run(user_id,
                    app_event_envelope(app, category, action=action,
                                       occurred_at=occurred_at,
                                       timezone_id=_live_timezone(user_id)),
                    received=now or datetime.now(timezone.utc))
    return _guarded("app_event", build, user_id)


def observe_location(user_id: str, values: Mapping[str, Any], *,
                     occurred_at: Any, now: datetime | None = None) -> dict[str, Any]:
    """The decrypted location signal -> city and Wi-Fi anchor.

    Handed the plaintext the live path already decrypted, so it costs no extra
    enclave call, and reading only the coarse labels -- see events.py.
    """
    def build():
        from .events import location_envelope
        envelope = location_envelope(
            values, occurred_at=occurred_at, timezone_id=_live_timezone(user_id))
        if envelope is None:
            return None
        return _run(user_id, envelope, received=now or datetime.now(timezone.utc))
    return _guarded("location", build, user_id)


def mirror_calendar(user_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Calendar and reminders take the source-mirror path, not the signal path.

    No comparison is recorded: the live path keeps a single "next event" cell
    while the mirror keeps the collection, so there is no field pair to compare
    -- which is why `compare.SHAPE_DIFFERS` would not help here either.
    """
    def build():
        from .events import calendar_rows
        from perceptkit.contracts.records import CalendarEventMirror
        rows = calendar_rows(payload)
        if not rows:
            return None
        received = datetime.now(timezone.utc)
        events = [CalendarEventMirror(subject_id=user_id, updated_at=received,
                                      **row) for row in rows]
        import db
        from .storage import PostgresStorage
        with db.get_pool().connection() as conn:
            conn.autocommit = True
            PostgresStorage(conn).upsert_calendar_events(
                subject_id=user_id, events=events)
        return {"ran": True, "producer": "ios_calendar_mirror",
                "report_id": "-", "observations": len(rows), "applied": len(rows),
                "rejected": 0, "duplicates": 0, "events": 0,
                "warnings": [], "rejections": [], "verdicts": {}}
    return _guarded("calendar_mirror", build, user_id)


def mirror_reminders(user_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """See ``mirror_calendar``."""
    def build():
        from .events import reminder_rows
        from perceptkit.contracts.records import ReminderItemMirror
        rows = reminder_rows(payload)
        if not rows:
            return None
        received = datetime.now(timezone.utc)
        items = [ReminderItemMirror(subject_id=user_id, updated_at=received,
                                    **row) for row in rows]
        import db
        from .storage import PostgresStorage
        with db.get_pool().connection() as conn:
            conn.autocommit = True
            PostgresStorage(conn).upsert_reminders(subject_id=user_id, items=items)
        return {"ran": True, "producer": "ios_reminder_mirror",
                "report_id": "-", "observations": len(rows), "applied": len(rows),
                "rejected": 0, "duplicates": 0, "events": 0,
                "warnings": [], "rejections": [], "verdicts": {}}
    return _guarded("reminder_mirror", build, user_id)


__all__ = [
    "ENV_FLAG", "WAKE_ENV_FLAG", "BUDGET_SEC", "enabled", "wakes_enabled", "observe",
    "observe_photo", "observe_device_event", "observe_app_event",
    "observe_location", "mirror_calendar", "mirror_reminders",
]
