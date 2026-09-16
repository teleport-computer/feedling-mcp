"""Selector for Dream ledgers advanced by a false "no cards" completion.

Incident (prod 2026-09-10 and 2026-09-13, roughly 18:00-20:00Z): resident
consumers answered a timed-out Memory Garden card read with ``completed`` +
``dream_no_cards_available``. The backend recorded that as a real Dream, so the
ledger (``user_blobs`` kind ``dream_state``) now carries the signature of a
garden that was never consolidated, and the scheduler answers
``already_dreamed`` / ``not_enough_new_cards`` until enough NEW cards arrive.
Fixing the code does not un-stick them: ``failed`` never rewinds the ledger and
``force`` does not bypass ``already_dreamed``.

This module only reads. It is shared by the operator CLI
(``tools/audit_dream_false_no_cards.py``) and the admin audit/repair endpoints
(``admin/dream_ledger_repair.py``, which owns the single write). It imports
nothing from the backend so the CLI can load it with only ``psycopg``
installed. Output is content-free: user/job ids, timestamps, counts, garden
signatures (a hash of card ids/metadata) and a ledger fingerprint — no card or
chat text.

A candidate must satisfy ALL of:

1. a resident ``memory_dream`` job (``user_logs`` stream ``proactive_jobs``)
   completed inside one of the windows (the completion matched in 5. is the
   latest such false completion from then on: a later night that failed the
   same way re-stamped the ledger);
2. with reason ``dream_no_cards_available`` and no ``dream_result.cards_read``
   marker (consumers since the strict read only send it after a read that
   reported zero live cards);
3. whose enqueue-time ``dream_stats.card_count`` was > 0 (the scheduler only
   enqueues Dream for a non-empty garden, so a real "no cards" is implausible);
4. no later verified Dream for that user: no later completed resident job that
   is not itself an unverified no-cards completion, and no later completed
   (non-skipped) Runtime V2 ``agent_jobs`` dream;
5. the current ledger still points at that completion: same signature, and
   ``last_dream_completed_at`` within ``ledger_tolerance_sec`` of it.

A user whose ledger already equals the rewind target (a repair already ran and
no Dream has completed since) is reported as ``already_repaired``. A job the
repair reclassified (``REPAIR_MARKER_KEY``) is still read as the false
completion it was, so verdicts do not change because of the repair itself.

Limits: resident jobs are trimmed to the newest ``FEEDLING_PROACTIVE_JOB_MAX``
(500) per user, so a user whose incident job was trimmed is not found; naive
``completed_at`` values (written with the server clock, UTC in the CVMs) are
read as UTC. Runtime V2 empty-read no-ops do not carry this reason and are not
covered. Without ``user_ids`` (global discovery) the candidate prefilter only
considers jobs enqueued (indexed ``ts`` column) within ``max_job_age_days``
before the earliest window start; a Dream job that sat pending longer than that
before completing is not found, so such a report carries ``partial: true`` and
the bound under ``scan_bound``. With ``user_ids`` there is no enqueue-time bound
(each user's job log is already capped at 500 rows) and ``partial`` is false.
Every statement runs under a transaction-local ``statement_timeout``.

A garden that is empty NOW is not decided here (this module cannot read
cards): the repair re-checks it and leaves such a user alone, because an old
consumer's "no cards" was then possibly true.

REPAIR DESIGN (implemented in ``admin/dream_ledger_repair.py``)
---------------------------------------------------------------
1. Rewind only the ledger fields (``LEDGER_FIELDS``) to the latest verified
   completion before the suspicious one — or to the never-dreamed zero state —
   leaving pending/backoff/skip/trace fields alone. The write is a
   compare-and-set on ``ledger_fingerprint`` of the current ledger under a row
   lock, so a Dream that completed after the audit is never rewound. It is
   refused while the user has a queued/running Dream job or no live cards.
2. Reclassify the rewound false completions (``rewound_job_ids``) the way the
   fixed backend classifies them at report time: ``failed`` +
   ``dream_context_unavailable``, plus ``REPAIR_MARKER_KEY`` recording the
   original status. Without this the rewind alone does not un-stick a user whose
   garden and chat turn count are unchanged since the incident: the scheduler
   recomputes the incident job's idempotency key and the enqueue answers
   ``duplicate_dream_key``, because a completed key is never retried while a
   failed one is.

The next scheduler tick then re-evaluates the real garden with the kernel's own
``needs_dream`` and enqueues normally; no special job type or
``already_dreamed`` bypass is needed.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

LEGACY_NO_CARDS_REASON = "dream_no_cards_available"
# Global discovery only (no user_id given): scan bound on a job's enqueue time,
# before the earliest window start. Picked in 4759417c when Codex review asked
# for the fleet-wide prefilter to use the ``ts`` partial index instead of every
# proactive_jobs row; it is not derived from data. A self-hosted consumer that
# was offline longer can complete an old pending Dream inside a window, so a
# bounded report says ``partial: true`` and naming the user removes the bound.
DEFAULT_MAX_JOB_AGE_DAYS = 30.0
DEFAULT_STATEMENT_TIMEOUT_SEC = 60.0
DEFAULT_LEDGER_TOLERANCE_SEC = 300.0
DREAM_JOB_KIND = "memory_dream"
DREAM_STATE_KIND = "dream_state"
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
_LEDGER_STRING_FIELDS = frozenset({"last_dream_signature", "last_dreamed_until"})
#: Job-doc key the repair writes when it reclassifies a false completion.
REPAIR_MARKER_KEY = "dream_ledger_repair"


class InvalidWindow(ValueError):
    """A window is not ``START/END`` ISO instants with END after START."""


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
        raise InvalidWindow(
            f"window must be START/END ISO instants with END after START: {value!r}"
        )
    return start, end


def format_instant(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


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


def repair_marker(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    """The repair's reclassification record, or ``{}`` for an untouched job."""
    marker = _mapping(doc.get(REPAIR_MARKER_KEY))
    return marker if str(marker.get("original_status") or "") == "completed" else {}


def completed_at(doc: Mapping[str, Any]) -> datetime | None:
    """When the job completed; a repaired job keeps its original completion."""
    marker = repair_marker(doc)
    if marker:
        return parse_instant(marker.get("original_completed_at"))
    if str(doc.get("status") or "").strip().lower() != "completed":
        return None
    return parse_instant(doc.get("completed_at"))


def is_unverified_no_cards(doc: Mapping[str, Any]) -> bool:
    """The completion shape the incident produced (whatever the card count)."""
    if repair_marker(doc):
        return True
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


def _canonical_ledger_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key in _LEDGER_STRING_FIELDS:
        return str(value)
    if isinstance(value, bool):
        return f"raw:{value!r}"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"raw:{str(value)[:240]}"
    if not math.isfinite(number):
        return f"raw:{str(value)[:240]}"
    if key == "last_dream_completed_at":
        # JSON round trips through jsonb keep the float, but a millisecond
        # grain keeps the fingerprint stable against representation noise.
        return round(number, 3)
    return int(number) if number == int(number) else number


def canonical_ledger(doc: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ledger fields of a ``dream_state`` document, normalised for comparison.

    A missing key stays ``None`` (distinct from ``0``/``""``) so a fingerprint
    taken from the raw row cannot collide with a rewound ledger.
    """
    source = doc if isinstance(doc, Mapping) else {}
    return {key: _canonical_ledger_value(key, source.get(key)) for key in LEDGER_FIELDS}


def ledger_fingerprint(doc: Mapping[str, Any] | None) -> str:
    """sha256 over the canonical ledger fields — the repair's compare-and-set token."""
    payload = json.dumps(canonical_ledger(doc), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _in_windows(instant: datetime, windows: Iterable[tuple[datetime, datetime]]) -> bool:
    return any(start <= instant < end for start, end in windows)


def select_user(
    user_id: str,
    jobs: list[Mapping[str, Any]],
    ledger: Mapping[str, Any] | None,
    *,
    windows: list[tuple[datetime, datetime]],
    v2_last_completed: datetime | None = None,
    ledger_tolerance_sec: float = DEFAULT_LEDGER_TOLERANCE_SEC,
) -> tuple[str, dict[str, Any] | None]:
    """Return ``(verdict, row)`` for one user.

    ``verdict`` is ``candidate``, ``already_repaired``, or the first exclusion
    that applied: ``no_incident_completion``, ``later_verified_dream``,
    ``ledger_missing``, ``ledger_moved``. ``row`` is set for ``candidate`` and
    ``already_repaired`` only.
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
    previous = [
        doc for at, doc in completions
        if at < suspect_at and not is_unverified_no_cards(doc)
    ]
    restore_from = previous[-1] if previous else None
    restore_ledger = ledger_after(restore_from)
    restore_at = completed_at(restore_from) if restore_from is not None else None
    rewound = [
        doc for at, doc in unverified if restore_at is None or at > restore_at
    ]
    row = {
        "user_id": user_id,
        "job_id": str(suspect.get("job_id") or ""),
        "completed_at": format_instant(suspect_at),
        "enqueue_card_count": enqueue_card_count(suspect),
        "incident_completions_in_window": len(incident),
        "expected_ledger": {key: ledger.get(key) for key in LEDGER_FIELDS},
        "ledger_fingerprint": ledger_fingerprint(ledger),
        "restore_from_job_id": str((restore_from or {}).get("job_id") or ""),
        "restore_ledger": restore_ledger,
        "rewound_job_ids": [str(doc.get("job_id") or "") for doc in rewound],
        "unreclassified_job_ids": [
            str(doc.get("job_id") or "") for doc in rewound if not repair_marker(doc)
        ],
    }
    if canonical_ledger(ledger) == canonical_ledger(restore_ledger):
        return "already_repaired", row
    if (
        str(ledger.get("last_dream_signature") or "") != job_signature(suspect)
        or abs(_num(ledger.get("last_dream_completed_at")) - suspect_at.timestamp())
        > float(ledger_tolerance_sec)
    ):
        return "ledger_moved", None
    return "candidate", row


def build_report(
    jobs_by_user: Mapping[str, list[Mapping[str, Any]]],
    ledgers: Mapping[str, Mapping[str, Any]],
    *,
    windows: list[tuple[datetime, datetime]],
    v2_last_completed: Mapping[str, datetime] | None = None,
    ledger_tolerance_sec: float = DEFAULT_LEDGER_TOLERANCE_SEC,
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    already_repaired: list[dict[str, Any]] = []
    for user_id in sorted(jobs_by_user):
        verdict, row = select_user(
            user_id,
            list(jobs_by_user[user_id]),
            ledgers.get(user_id),
            windows=windows,
            v2_last_completed=(v2_last_completed or {}).get(user_id),
            ledger_tolerance_sec=ledger_tolerance_sec,
        )
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict == "candidate":
            candidates.append(row)
        elif verdict == "already_repaired":
            already_repaired.append({
                "user_id": user_id,
                "job_id": row["job_id"],
                "unreclassified_job_ids": row["unreclassified_job_ids"],
            })
    return {
        "mode": "dry-run",
        "windows": [[format_instant(start), format_instant(end)] for start, end in windows],
        "users_scanned": len(jobs_by_user),
        "verdicts": dict(sorted(counts.items())),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "already_repaired": already_repaired,
    }


# --------------------------------------------------------------------------- #
# read-only database collection
# --------------------------------------------------------------------------- #

def validate_bounds(
    *,
    windows,
    max_job_age_days: float,
    statement_timeout_sec: float,
) -> None:
    if not windows:
        raise ValueError("at least one window is required")
    if not float(max_job_age_days) > 0:
        raise ValueError("max_job_age_days must be positive")
    if not float(statement_timeout_sec) > 0:
        raise ValueError("statement_timeout_sec must be positive")


def collect_inputs(
    conn,
    *,
    windows,
    user_ids=None,
    max_job_age_days: float = DEFAULT_MAX_JOB_AGE_DAYS,
    statement_timeout_sec: float = DEFAULT_STATEMENT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Read everything ``select_user`` needs; the caller owns the transaction.

    Must run as the first statements of a transaction (``SET TRANSACTION READ
    ONLY``). Only users with a completed resident dream job whose
    ``completed_at`` string falls on a UTC date a window touches are loaded in
    full (cheap prefilter; the exact window test is ``select_user``'s).

    Global discovery (no ``user_ids``) also bounds that scan on the indexed
    enqueue time ``ts`` (partial index ``ix_user_logs_proactive_jobs_ts``): a
    job completing inside a window was enqueued before the window ended and, by
    assumption, at most ``max_job_age_days`` before it started; rows without
    ``ts`` are kept. The assumption can hide rows, so the result says
    ``partial: True``. Named users are scanned without that bound (their logs
    are capped per user), ``partial: False``.
    """
    validate_bounds(
        windows=windows,
        max_job_age_days=max_job_age_days,
        statement_timeout_sec=statement_timeout_sec,
    )
    conn.execute("SET TRANSACTION READ ONLY")
    # Transaction-local: never outlives this read.
    conn.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (f"{int(float(statement_timeout_sec) * 1000)}ms",),
    )
    days = window_utc_dates(windows)
    params: list[Any] = [
        DREAM_JOB_KIND,
        DREAM_JOB_KIND,
        REPAIR_MARKER_KEY,
        [f"{day}%" for day in days],
    ]
    if user_ids:
        scan_bound = None
        scan_filter = "AND user_id = ANY(%s) "
        params.insert(0, list(user_ids))
    else:
        created_after = min(start for start, _end in windows) - timedelta(
            days=float(max_job_age_days)
        )
        created_before = max(end for _start, end in windows)
        scan_bound = {
            "enqueued_after": format_instant(created_after),
            "enqueued_before": format_instant(created_before),
            "max_job_age_days": float(max_job_age_days),
        }
        scan_filter = "AND (ts IS NULL OR (ts >= %s AND ts < %s)) "
        params[0:0] = [created_after.timestamp(), created_before.timestamp()]
    affected = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT user_id FROM user_logs "
            "WHERE stream = 'proactive_jobs' "
            + scan_filter +
            "AND (doc->>'job_kind' = %s OR doc->>'source' = %s) "
            # A repaired job is ``failed`` but keeps completed_at + the marker.
            "AND (doc->>'status' = 'completed' OR doc ? %s) "
            "AND doc->>'completed_at' LIKE ANY(%s)",
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
            "SELECT user_id, doc FROM user_blobs WHERE kind = %s "
            "AND user_id = ANY(%s)",
            (DREAM_STATE_KIND, affected),
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
    return {
        "jobs_by_user": jobs_by_user,
        "ledgers": ledgers,
        "v2_last_completed": v2_last,
        "prefilter": {
            "completed_on_utc_dates": days,
            "statement_timeout_sec": float(statement_timeout_sec),
        },
        "scan_bound": scan_bound,
        "partial": scan_bound is not None,
    }


def collect(
    conn,
    *,
    windows,
    user_ids=None,
    ledger_tolerance_sec=DEFAULT_LEDGER_TOLERANCE_SEC,
    max_job_age_days: float = DEFAULT_MAX_JOB_AGE_DAYS,
    statement_timeout_sec: float = DEFAULT_STATEMENT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Read and select in one read-only transaction (owned by the caller)."""
    inputs = collect_inputs(
        conn,
        windows=windows,
        user_ids=user_ids,
        max_job_age_days=max_job_age_days,
        statement_timeout_sec=statement_timeout_sec,
    )
    report = build_report(
        inputs["jobs_by_user"],
        inputs["ledgers"],
        windows=windows,
        v2_last_completed=inputs["v2_last_completed"],
        ledger_tolerance_sec=ledger_tolerance_sec,
    )
    report["prefilter"] = inputs["prefilter"]
    report["scan_bound"] = inputs["scan_bound"]
    report["partial"] = inputs["partial"]
    return report


def active_dream_job_ids(
    conn,
    user_id: str,
    *,
    resident_statuses: Iterable[str],
    v2_statuses: Iterable[str],
) -> list[str]:
    """Ids of this user's Dream jobs that are still queued or running, both runtimes.

    Resident ``memory_dream`` rows (a missing status reads as ``pending``, like
    ``capture_jobs``) and Runtime V2 ``agent_jobs`` in the ``dream`` lane. The
    status vocabularies come from the caller so they cannot drift from the job
    modules that own them. Ids only.
    """
    rows = conn.execute(
        "SELECT doc->>'job_id' FROM user_logs WHERE stream = 'proactive_jobs' "
        "AND user_id = %s AND (doc->>'job_kind' = %s OR doc->>'source' = %s) "
        "AND lower(COALESCE(NULLIF(btrim(doc->>'status'), ''), 'pending')) = ANY(%s) "
        "UNION ALL "
        "SELECT 'v2:' || id::text FROM agent_jobs "
        "WHERE user_id = %s AND lane = 'dream' AND status = ANY(%s)",
        (
            user_id, DREAM_JOB_KIND, DREAM_JOB_KIND, sorted(resident_statuses),
            user_id, sorted(v2_statuses),
        ),
    ).fetchall()
    return sorted(str(row[0] or "") for row in rows)
