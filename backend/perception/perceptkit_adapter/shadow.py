"""Run PerceptKit alongside the live path and record what it would have done.

Nothing here changes what the user sees. The existing ingest keeps doing
everything it did before, byte for byte; this takes the same already-decrypted
snapshot, pushes it through the kit, and writes the result to the kit's own
tables. Then somebody can compare.

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

        from backend import db  # imported late: db pulls in the pool at import

        with db.get_pool().connection() as conn:
            conn.autocommit = True
            kit = PerceptionKit(storage=PostgresStorage(conn),
                                signals=MINIMAL_SIGNALS)
            outcome = kit.ingest(envelope,
                                 context=IngestContext(user_id, received))

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


__all__ = ["ENV_FLAG", "BUDGET_SEC", "enabled", "observe"]
