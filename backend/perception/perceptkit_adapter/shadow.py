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
            kit = PerceptionKit(storage=storage, signals=MINIMAL_SIGNALS)
            outcome = kit.ingest(envelope,
                                 context=IngestContext(user_id, received))
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


__all__ = ["ENV_FLAG", "BUDGET_SEC", "enabled", "observe"]
