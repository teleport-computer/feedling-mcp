#!/usr/bin/env python3
"""Read-only audit: users whose Dream ledger was advanced by a false "no cards".

Incident (prod 2026-09-10 and 2026-09-13, roughly 18:00-20:00Z): resident
consumers answered a timed-out Memory Garden card read with ``completed`` +
``dream_no_cards_available``. The backend recorded that as a real Dream, so the
ledger (``user_blobs`` kind ``dream_state``) now carries the signature of a
garden that was never consolidated, and the scheduler answers
``already_dreamed`` / ``not_enough_new_cards`` until enough NEW cards arrive.
Fixing the code does not un-stick them: ``failed`` never rewinds the ledger and
``force`` does not bypass ``already_dreamed``.

This tool never writes. It selects candidates and prints, per candidate, the
ledger rewind a repair would apply (see ``REPAIR DESIGN`` below). Output is
content-free: user/job ids, timestamps, counts and garden signatures (a hash of
card ids/metadata, no card text).

A candidate must satisfy ALL of:

1. a resident ``memory_dream`` job (``user_logs`` stream ``proactive_jobs``)
   completed inside one of the ``--window`` ranges (the completion matched in
   5. is the latest such false completion from then on: a later night that
   failed the same way re-stamped the ledger);
2. with reason ``dream_no_cards_available`` and no ``dream_result.cards_read``
   marker (consumers since the strict read only send it after a read that
   reported zero live cards);
3. whose enqueue-time ``dream_stats.card_count`` was > 0 (the scheduler only
   enqueues Dream for a non-empty garden, so a real "no cards" is implausible);
4. no later verified Dream for that user: no later completed resident job that
   is not itself an unverified no-cards completion, and no later completed
   (non-skipped) Runtime V2 ``agent_jobs`` dream;
5. the current ledger still points at that completion: same signature, and
   ``last_dream_completed_at`` within ``--ledger-tolerance-sec`` of it.

Limits: resident jobs are trimmed to the newest ``FEEDLING_PROACTIVE_JOB_MAX``
(500) per user, so a user whose incident job was trimmed is not found; naive
``completed_at`` values (written with the server clock, UTC in the CVMs) are
read as UTC. Runtime V2 empty-read no-ops do not carry this reason and are not
covered. The candidate prefilter only considers jobs enqueued (indexed ``ts``
column) within ``--max-job-age-days`` before the earliest window start; a Dream
job that sat pending longer than that before completing is not found (the
report echoes the bound under ``prefilter``). Every statement runs under
``--statement-timeout-sec``.

REPAIR DESIGN (not implemented as a write path here, on purpose)
---------------------------------------------------------------
Rewind only the ledger fields (``LEDGER_FIELDS``) to the latest verified
completion before the suspicious one — or to the never-dreamed zero state —
leaving pending/backoff/skip/trace fields alone. The next nightly tick then
re-evaluates the real garden with the kernel's own ``needs_dream`` and
enqueues normally (idempotency key and ``min_interval`` unchanged), so no
special job type or ``already_dreamed`` bypass is needed. The write must go
through the backend's ``db.patch_blob_strict`` (atomic top-level merge that
also maintains the TEE shadow mirror), not raw SQL from this tool, and must be
a compare-and-set on the ledger still matching ``expected_ledger`` so a Dream
that ran in between is never rewound. Run per user, dry-run first, from inside
the backend environment.

Examples::

    python tools/audit_dream_false_no_cards.py --env prod \
      --window 2026-09-10T18:00:00Z/2026-09-10T20:00:00Z \
      --window 2026-09-13T18:00:00Z/2026-09-13T20:00:00Z
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
LEGACY_NO_CARDS_REASON = "dream_no_cards_available"
# Scan bound on a job's enqueue time, before the earliest window start. An
# assumption, not a measured limit: a self-hosted consumer that was offline can
# complete an old pending Dream late. Widen with --max-job-age-days when in doubt.
DEFAULT_MAX_JOB_AGE_DAYS = 30.0
DEFAULT_STATEMENT_TIMEOUT_SEC = 60.0
DREAM_JOB_KIND = "memory_dream"
# The dream_state keys ``dream_scheduler.record_dream_job_status`` sets on a
# completion. A repair rewinds exactly these and nothing else.
LEDGER_FIELDS = (
    "last_dream_completed_at",
    "last_dream_organized_count",
    "last_dream_merged_count",
    "last_dreamed_card_count",
    "last_dreamed_seed_card_count",
    "last_dreamed_turn_count",
    "last_dream_signature",
    "last_dreamed_until",
)


# --------------------------------------------------------------------------- #
# pure selection (tested without a database)
# --------------------------------------------------------------------------- #

def parse_instant(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_window(value: str) -> tuple[datetime, datetime]:
    start_raw, sep, end_raw = str(value or "").partition("/")
    start, end = parse_instant(start_raw), parse_instant(end_raw)
    if not sep or start is None or end is None or end <= start:
        raise argparse.ArgumentTypeError(
            f"window must be START/END ISO instants with END after START: {value!r}"
        )
    return start, end


def window_utc_dates(windows: Iterable[tuple[datetime, datetime]]) -> list[str]:
    """Every UTC calendar date a window touches, as ``YYYY-MM-DD``.

    A multi-day window covers its middle days too; loading only the start and
    end dates silently skipped completions in between.
    """
    days: set[str] = set()
    for start, end in windows:
        day = start.astimezone(timezone.utc).date()
        last = end.astimezone(timezone.utc).date()
        while day <= last:
            days.add(day.isoformat())
            day += timedelta(days=1)
    return sorted(days)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def is_dream_job(doc: Mapping[str, Any]) -> bool:
    return (
        str(doc.get("job_kind") or "").strip() == DREAM_JOB_KIND
        or str(doc.get("source") or "").strip() == DREAM_JOB_KIND
    )


def completed_at(doc: Mapping[str, Any]) -> datetime | None:
    if str(doc.get("status") or "").strip().lower() != "completed":
        return None
    return parse_instant(doc.get("completed_at"))


def is_unverified_no_cards(doc: Mapping[str, Any]) -> bool:
    """The completion shape the incident produced (whatever the card count)."""
    result = _mapping(doc.get("dream_result"))
    reasons = {
        str(doc.get("status_reason") or ""),
        str(doc.get("noop_reason") or ""),
        str(result.get("reason") or ""),
    }
    return LEGACY_NO_CARDS_REASON in reasons and result.get("cards_read") != "empty"


def enqueue_card_count(doc: Mapping[str, Any]) -> int:
    return int(_num(_mapping(doc.get("dream_stats")).get("card_count"), 0))


def job_signature(doc: Mapping[str, Any]) -> str:
    stats, until = _mapping(doc.get("dream_stats")), _mapping(doc.get("dream_until"))
    return str(stats.get("signature") or until.get("signature") or "")[:240]


def ledger_after(doc: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ledger fields a completion of ``doc`` writes (``None`` = never dreamed).

    Mirrors ``dream_scheduler.record_dream_job_status``; the completion time is
    the job's recorded ``completed_at`` instead of the recorder's clock.
    """
    if doc is None:
        return {
            "last_dream_completed_at": 0.0,
            "last_dream_organized_count": 0,
            "last_dream_merged_count": 0,
            "last_dreamed_card_count": 0,
            "last_dreamed_seed_card_count": 0,
            "last_dreamed_turn_count": 0,
            "last_dream_signature": "",
            "last_dreamed_until": "",
        }
    stats, until = _mapping(doc.get("dream_stats")), _mapping(doc.get("dream_until"))
    result = _mapping(doc.get("dream_result"))
    finished = completed_at(doc)
    card_count = max(0, int(_num(stats.get("card_count"), 0)))
    return {
        "last_dream_completed_at": finished.timestamp() if finished else 0.0,
        "last_dream_organized_count": max(0, int(_num(
            doc.get("organized_count") or result.get("organized_count")
            or doc.get("cards_superseded"), 0))),
        "last_dream_merged_count": max(0, int(_num(
            doc.get("merged_count") or result.get("merged_count")
            or doc.get("cards_merged"), 0))),
        "last_dreamed_card_count": card_count,
        "last_dreamed_seed_card_count": max(
            0, int(_num(stats.get("seed_card_count"), card_count))
        ),
        "last_dreamed_turn_count": max(0, int(_num(stats.get("turn_count"), 0))),
        "last_dream_signature": job_signature(doc),
        "last_dreamed_until": str(until.get("last_until") or "")[:240],
    }


def _in_windows(instant: datetime, windows: Iterable[tuple[datetime, datetime]]) -> bool:
    return any(start <= instant < end for start, end in windows)


def select_user(
    user_id: str,
    jobs: list[Mapping[str, Any]],
    ledger: Mapping[str, Any] | None,
    *,
    windows: list[tuple[datetime, datetime]],
    v2_last_completed: datetime | None = None,
    ledger_tolerance_sec: float = 300.0,
) -> tuple[str, dict[str, Any] | None]:
    """Return ``(verdict, candidate)`` for one user.

    ``verdict`` is ``candidate`` or the first exclusion that applied:
    ``no_incident_completion``, ``later_verified_dream``, ``ledger_missing``,
    ``ledger_moved``.
    """
    completions = sorted(
        (
            (completed_at(doc), doc)
            for doc in jobs
            if is_dream_job(doc) and completed_at(doc) is not None
        ),
        key=lambda pair: pair[0],
    )
    unverified = [
        (at, doc)
        for at, doc in completions
        if is_unverified_no_cards(doc) and enqueue_card_count(doc) > 0
    ]
    incident = [(at, doc) for at, doc in unverified if _in_windows(at, windows)]
    if not incident:
        return "no_incident_completion", None
    # The ledger is advanced by the LATEST such completion: a later night whose
    # read failed the same way re-stamped it, and that is the one to match.
    suspect_at, suspect = [pair for pair in unverified if pair[0] >= incident[0][0]][-1]
    later_verified = any(
        at > suspect_at and not is_unverified_no_cards(doc)
        for at, doc in completions
    )
    if later_verified or (v2_last_completed is not None and v2_last_completed > suspect_at):
        return "later_verified_dream", None
    if not isinstance(ledger, Mapping) or not ledger:
        return "ledger_missing", None
    if (
        str(ledger.get("last_dream_signature") or "") != job_signature(suspect)
        or abs(_num(ledger.get("last_dream_completed_at")) - suspect_at.timestamp())
        > float(ledger_tolerance_sec)
    ):
        return "ledger_moved", None
    previous = [
        doc for at, doc in completions
        if at < suspect_at and not is_unverified_no_cards(doc)
    ]
    restore_from = previous[-1] if previous else None
    return "candidate", {
        "user_id": user_id,
        "job_id": str(suspect.get("job_id") or ""),
        "completed_at": suspect_at.isoformat().replace("+00:00", "Z"),
        "enqueue_card_count": enqueue_card_count(suspect),
        "incident_completions_in_window": len(incident),
        "expected_ledger": {key: ledger.get(key) for key in LEDGER_FIELDS},
        "restore_from_job_id": str((restore_from or {}).get("job_id") or ""),
        "restore_ledger": ledger_after(restore_from),
    }


def build_report(
    jobs_by_user: Mapping[str, list[Mapping[str, Any]]],
    ledgers: Mapping[str, Mapping[str, Any]],
    *,
    windows: list[tuple[datetime, datetime]],
    v2_last_completed: Mapping[str, datetime] | None = None,
    ledger_tolerance_sec: float = 300.0,
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    for user_id in sorted(jobs_by_user):
        verdict, candidate = select_user(
            user_id,
            list(jobs_by_user[user_id]),
            ledgers.get(user_id),
            windows=windows,
            v2_last_completed=(v2_last_completed or {}).get(user_id),
            ledger_tolerance_sec=ledger_tolerance_sec,
        )
        counts[verdict] = counts.get(verdict, 0) + 1
        if candidate is not None:
            candidates.append(candidate)
    return {
        "mode": "dry-run",
        "windows": [
            [start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")]
            for start, end in windows
        ],
        "users_scanned": len(jobs_by_user),
        "verdicts": dict(sorted(counts.items())),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


# --------------------------------------------------------------------------- #
# read-only database collection
# --------------------------------------------------------------------------- #

def collect(
    conn,
    *,
    windows,
    user_ids=None,
    ledger_tolerance_sec=300.0,
    max_job_age_days: float = DEFAULT_MAX_JOB_AGE_DAYS,
    statement_timeout_sec: float = DEFAULT_STATEMENT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Read everything ``build_report`` needs in one read-only transaction.

    Only users with a completed resident dream job whose ``completed_at``
    string falls on a UTC date a window touches are loaded in full (cheap
    prefilter; the exact window test is ``select_user``'s). That scan is also
    bounded on the indexed enqueue time ``ts`` (partial index
    ``ix_user_logs_proactive_jobs_ts``): a job completing inside a window was
    enqueued before the window ended and, by assumption, at most
    ``max_job_age_days`` before it started. Rows without ``ts`` are kept.
    """
    if not windows:
        raise ValueError("at least one window is required")
    if not float(max_job_age_days) > 0:
        raise ValueError("max_job_age_days must be positive")
    if not float(statement_timeout_sec) > 0:
        raise ValueError("statement_timeout_sec must be positive")
    conn.execute("SET TRANSACTION READ ONLY")
    # Transaction-local: never outlives this read.
    conn.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (f"{int(float(statement_timeout_sec) * 1000)}ms",),
    )
    days = window_utc_dates(windows)
    created_after = min(start for start, _end in windows) - timedelta(
        days=float(max_job_age_days)
    )
    created_before = max(end for _start, end in windows)
    params: list[Any] = [
        created_after.timestamp(),
        created_before.timestamp(),
        DREAM_JOB_KIND,
        DREAM_JOB_KIND,
        [f"{day}%" for day in days],
    ]
    user_filter = ""
    if user_ids:
        user_filter = " AND user_id = ANY(%s)"
        params.append(list(user_ids))
    affected = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT user_id FROM user_logs "
            "WHERE stream = 'proactive_jobs' "
            "AND (ts IS NULL OR (ts >= %s AND ts < %s)) "
            "AND (doc->>'job_kind' = %s OR doc->>'source' = %s) "
            "AND doc->>'status' = 'completed' "
            "AND doc->>'completed_at' LIKE ANY(%s)" + user_filter,
            params,
        ).fetchall()
    ]
    jobs_by_user: dict[str, list[dict]] = {user_id: [] for user_id in affected}
    ledgers: dict[str, dict] = {}
    v2_last: dict[str, datetime] = {}
    if affected:
        for user_id, doc in conn.execute(
            "SELECT user_id, doc FROM user_logs WHERE stream = 'proactive_jobs' "
            "AND user_id = ANY(%s) AND (doc->>'job_kind' = %s OR doc->>'source' = %s) "
            "ORDER BY user_id, seq",
            (affected, DREAM_JOB_KIND, DREAM_JOB_KIND),
        ).fetchall():
            jobs_by_user[user_id].append(dict(doc or {}))
        for user_id, doc in conn.execute(
            "SELECT user_id, doc FROM user_blobs WHERE kind = 'dream_state' "
            "AND user_id = ANY(%s)",
            (affected,),
        ).fetchall():
            ledgers[user_id] = dict(doc or {})
        for user_id, finished in conn.execute(
            "SELECT user_id, max(finished_at) FROM agent_jobs "
            "WHERE lane = 'dream' AND status = 'completed' "
            "AND COALESCE(wake_result, '') <> 'skipped' AND user_id = ANY(%s) "
            "GROUP BY user_id",
            (affected,),
        ).fetchall():
            if finished is not None:
                v2_last[user_id] = (
                    finished if finished.tzinfo else finished.replace(tzinfo=timezone.utc)
                ).astimezone(timezone.utc)
    report = build_report(
        jobs_by_user,
        ledgers,
        windows=windows,
        v2_last_completed=v2_last,
        ledger_tolerance_sec=ledger_tolerance_sec,
    )
    report["prefilter"] = {
        "completed_on_utc_dates": days,
        "enqueued_after": created_after.isoformat().replace("+00:00", "Z"),
        "enqueued_before": created_before.isoformat().replace("+00:00", "Z"),
        "statement_timeout_sec": float(statement_timeout_sec),
    }
    return report


def _dsn(env: str) -> str:
    key = f"{env.upper()}_DATABASE_URL"
    if os.environ.get(key, "").strip():
        return os.environ[key].strip()
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} is required (environment or {env_file})")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", choices=("test", "pre", "prod"), required=True)
    parser.add_argument(
        "--window", type=parse_window, action="append", required=True,
        help="START/END ISO instants of an incident window (repeatable).",
    )
    parser.add_argument("--user-id", action="append", default=[],
                        help="Restrict to these users (repeatable).")
    parser.add_argument("--ledger-tolerance-sec", type=float, default=300.0)
    parser.add_argument(
        "--max-job-age-days", type=float, default=DEFAULT_MAX_JOB_AGE_DAYS,
        help="Only consider Dream jobs enqueued at most this long before the "
             "earliest window start (index-friendly scan bound).",
    )
    parser.add_argument(
        "--statement-timeout-sec", type=float, default=DEFAULT_STATEMENT_TIMEOUT_SEC,
        help="Postgres statement_timeout for every read (transaction-local).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        import psycopg
    except ImportError:  # pragma: no cover - operator environment
        raise SystemExit("psycopg is required: pip install 'psycopg[binary]'")
    with psycopg.connect(_dsn(args.env)) as conn:
        with conn.transaction():
            report = collect(
                conn,
                windows=args.window,
                user_ids=args.user_id or None,
                ledger_tolerance_sec=args.ledger_tolerance_sec,
                max_job_age_days=args.max_job_age_days,
                statement_timeout_sec=args.statement_timeout_sec,
            )
    report["environment"] = args.env
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
