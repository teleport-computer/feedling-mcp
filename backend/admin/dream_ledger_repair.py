"""Admin audit + compare-and-set repair for false "no cards" Dream ledgers.

Operators have the admin token but not database access, so the read-only
selector in ``proactive.dream_ledger_audit`` (also used by the CLI) is exposed
here, together with the repair it was designed for, per user:

1. rewind ONLY the Dream ledger fields to the last verified completion (or the
   never-dreamed zero state); pending/backoff/skip/trace fields are left alone;
2. reclassify the rewound false completions as ``failed`` +
   ``dream_context_unavailable`` (what the fixed backend records for them at
   report time), keeping ``completed_at`` and recording the original status
   under ``dream_ledger_repair``. A completed job's ``dream_key`` is never
   retried, so without this a user whose garden and turn count did not change
   since the incident stays blocked on ``duplicate_dream_key``.

The next scheduler tick then decides whether to enqueue a Dream with its normal
rules.

Safety properties, each covered by ``tests/test_admin_dream_false_no_cards.py``:

- no bulk mode: the repair only touches ``user_id``s named in the request, each
  paired with the ``ledger_fingerprint`` the audit reported for it;
- ``dry_run`` defaults to true and must be the JSON boolean ``false`` to write;
- the selector is re-run inside the request; a user that is no longer a
  candidate is skipped with its verdict, and ``already_repaired`` is a no-op;
- the ledger write is ``db.patch_blob_if_match_strict``: row lock, fingerprint
  compare, top-level merge, TEE shadow mirror of the committed document. A
  Dream that completed after the audit changes the fingerprint and is skipped
  (and then no job is reclassified either);
- job reclassification runs only after the ledger is at the rewind target, via
  ``db.log_patch_item`` guarded on ``status = 'completed'`` (mirrored to TEE
  only when the guard matched). An interrupted run is finished by re-running:
  the user then reads ``already_repaired`` with ``unreclassified_job_ids``;
- output and trace/log events carry ids, verdicts, timestamps, counts, hashes
  and ledger numbers only — never card or chat content.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

import db
import debug_trace
from proactive import dream_ledger_audit
from proactive import dream_scheduler

log = logging.getLogger("feedling.admin.dream_ledger_repair")

AUDIT_STATEMENT_TIMEOUT_SEC_DEFAULT = 20.0
AUDIT_STATEMENT_TIMEOUT_SEC_MAX = 50.0
REPAIR_READ_STATEMENT_TIMEOUT_SEC = 20.0
REPAIR_WRITE_STATEMENT_TIMEOUT_MS = 5000
# Stop starting new user writes once the request has run this long (read
# included), so the 55s HTTP deadline (``routes_asgi``) is not what ends a
# partially applied batch. Remaining users come back as ``not_attempted``.
REPAIR_WRITE_BUDGET_SEC = 35.0
POOL_ACQUIRE_TIMEOUT_SEC = 5.0
MAX_WINDOWS = 8
MAX_WINDOW_SPAN = timedelta(days=7)
MAX_JOB_AGE_DAYS_MAX = 90.0
LEDGER_TOLERANCE_SEC_MAX = 3600.0
MAX_AUDIT_USER_IDS = 500
MAX_REPAIR_USERS = 100

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_AUDIT_PARAMS = frozenset({
    "window", "user_id", "max_job_age_days", "ledger_tolerance_sec",
    "statement_timeout_sec",
    "admin_key",  # legacy auth channel _require_admin still accepts
})
_REPAIR_KEYS = frozenset({
    "windows", "users", "dry_run", "max_job_age_days", "ledger_tolerance_sec",
})
_REPAIR_USER_KEYS = frozenset({"user_id", "ledger_fingerprint"})


class BadRequest(ValueError):
    """Invalid audit/repair input; ``detail`` is a content-free reason code."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _now_iso() -> str:
    return dream_ledger_audit.format_instant(datetime.now(timezone.utc))


def _parse_windows(raw_windows: Iterable[Any]) -> list[tuple[datetime, datetime]]:
    values = list(raw_windows)
    if not values:
        raise BadRequest("window_required")
    if len(values) > MAX_WINDOWS:
        raise BadRequest("too_many_windows")
    windows = []
    for value in values:
        if not isinstance(value, str):
            raise BadRequest("invalid_window")
        try:
            start, end = dream_ledger_audit.parse_window(value)
        except dream_ledger_audit.InvalidWindow:
            raise BadRequest("invalid_window") from None
        if end - start > MAX_WINDOW_SPAN:
            raise BadRequest("window_too_long")
        windows.append((start, end))
    return windows


def _bounded_float(raw: Any, name: str, *, default: float, maximum: float,
                   allow_zero: bool = False) -> float:
    if raw is None or raw == "":
        return float(default)
    if isinstance(raw, bool):
        raise BadRequest(f"invalid_{name}")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"invalid_{name}") from None
    if not math.isfinite(value) or value > maximum or value < 0 or (
        value == 0 and not allow_zero
    ):
        raise BadRequest(f"invalid_{name}")
    return value


def _user_id(raw: Any) -> str:
    text = raw.strip() if isinstance(raw, str) else ""
    if not _ID_RE.match(text):
        raise BadRequest("invalid_user_id")
    return text


def parse_audit_query(items: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Validate ``GET`` query pairs (repeatable ``window`` / ``user_id``)."""
    pairs = list(items)
    unknown = sorted({key for key, _ in pairs} - _AUDIT_PARAMS)
    if unknown:
        raise BadRequest("unknown_query_params:" + ",".join(unknown)[:200])
    single: dict[str, str] = {}
    for key, value in pairs:
        if key in ("window", "user_id", "admin_key"):
            continue
        if key in single:
            raise BadRequest(f"duplicate_{key}")
        single[key] = value
    user_ids = sorted({_user_id(value) for key, value in pairs if key == "user_id"})
    if len(user_ids) > MAX_AUDIT_USER_IDS:
        raise BadRequest("too_many_user_ids")
    return {
        "windows": _parse_windows(value for key, value in pairs if key == "window"),
        "user_ids": user_ids,
        "max_job_age_days": _bounded_float(
            single.get("max_job_age_days"), "max_job_age_days",
            default=dream_ledger_audit.DEFAULT_MAX_JOB_AGE_DAYS,
            maximum=MAX_JOB_AGE_DAYS_MAX,
        ),
        "ledger_tolerance_sec": _bounded_float(
            single.get("ledger_tolerance_sec"), "ledger_tolerance_sec",
            default=dream_ledger_audit.DEFAULT_LEDGER_TOLERANCE_SEC,
            maximum=LEDGER_TOLERANCE_SEC_MAX, allow_zero=True,
        ),
        "statement_timeout_sec": _bounded_float(
            single.get("statement_timeout_sec"), "statement_timeout_sec",
            default=AUDIT_STATEMENT_TIMEOUT_SEC_DEFAULT,
            maximum=AUDIT_STATEMENT_TIMEOUT_SEC_MAX,
        ),
    }


def parse_repair_body(payload: Any) -> dict[str, Any]:
    """Validate the repair body. Only explicit ids; ``dry_run`` defaults true."""
    if not isinstance(payload, Mapping):
        raise BadRequest("body_must_be_object")
    unknown = sorted(set(payload) - _REPAIR_KEYS)
    if unknown:
        raise BadRequest("unknown_fields:" + ",".join(map(str, unknown))[:200])
    dry_run = payload.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise BadRequest("invalid_dry_run")
    raw_windows = payload.get("windows")
    if not isinstance(raw_windows, list):
        raise BadRequest("window_required")
    users = payload.get("users")
    if not isinstance(users, list) or not users:
        raise BadRequest("users_required")
    if len(users) > MAX_REPAIR_USERS:
        raise BadRequest("too_many_users")
    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in users:
        if not isinstance(entry, Mapping) or set(entry) != _REPAIR_USER_KEYS:
            raise BadRequest("invalid_user_entry")
        user_id = _user_id(entry.get("user_id"))
        fingerprint = entry.get("ledger_fingerprint")
        if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.match(fingerprint):
            raise BadRequest("invalid_ledger_fingerprint")
        if user_id in seen:
            raise BadRequest("duplicate_user_id")
        seen.add(user_id)
        targets.append({"user_id": user_id, "ledger_fingerprint": fingerprint})
    return {
        "dry_run": dry_run,
        "windows": _parse_windows(raw_windows),
        "targets": targets,
        "max_job_age_days": _bounded_float(
            payload.get("max_job_age_days"), "max_job_age_days",
            default=dream_ledger_audit.DEFAULT_MAX_JOB_AGE_DAYS,
            maximum=MAX_JOB_AGE_DAYS_MAX,
        ),
        "ledger_tolerance_sec": _bounded_float(
            payload.get("ledger_tolerance_sec"), "ledger_tolerance_sec",
            default=dream_ledger_audit.DEFAULT_LEDGER_TOLERANCE_SEC,
            maximum=LEDGER_TOLERANCE_SEC_MAX, allow_zero=True,
        ),
    }


def _read_inputs(*, windows, user_ids, max_job_age_days, statement_timeout_sec):
    with db.get_pool().connection(timeout=POOL_ACQUIRE_TIMEOUT_SEC) as conn:
        with conn.transaction():
            return dream_ledger_audit.collect_inputs(
                conn,
                windows=windows,
                user_ids=user_ids or None,
                max_job_age_days=max_job_age_days,
                statement_timeout_sec=statement_timeout_sec,
            )


def audit_payload(params: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only report: the CLI's report plus ``generated_at``."""
    inputs = _read_inputs(
        windows=params["windows"],
        user_ids=params["user_ids"],
        max_job_age_days=params["max_job_age_days"],
        statement_timeout_sec=params["statement_timeout_sec"],
    )
    report = dream_ledger_audit.build_report(
        inputs["jobs_by_user"],
        inputs["ledgers"],
        windows=params["windows"],
        v2_last_completed=inputs["v2_last_completed"],
        ledger_tolerance_sec=params["ledger_tolerance_sec"],
    )
    report["mode"] = "read_only"
    report["prefilter"] = inputs["prefilter"]
    report["user_filter_count"] = len(params["user_ids"])
    report["ledger_tolerance_sec"] = params["ledger_tolerance_sec"]
    report["generated_at"] = _now_iso()
    return report


def _ledger_changes(current: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    now_canon = dream_ledger_audit.canonical_ledger(current)
    target_canon = dream_ledger_audit.canonical_ledger(target)
    return {
        key: {"from": current.get(key), "to": target.get(key)}
        for key in dream_ledger_audit.LEDGER_FIELDS
        if now_canon[key] != target_canon[key]
    }


def _emit_repair_event(user_id: str, result: Mapping[str, Any]) -> None:
    """Content-free audit trail for an applied (non-dry-run) decision."""
    audit = {
        "event": "admin_dream_ledger_repair",
        "who": "admin",
        "user_id": user_id,
        "action": result.get("action"),
        "reason": result.get("reason"),
        "job_id": result.get("job_id") or "",
        "restore_from_job_id": result.get("restore_from_job_id") or "",
        "reclassified_job_ids": list(result.get("reclassified_job_ids") or []),
        "unreclassified_job_ids": list(result.get("unreclassified_job_ids") or []),
        "ts": _now_iso(),
    }
    log.warning("[admin:dream-ledger-repair] %s", json.dumps(audit, separators=(",", ":")))
    if result.get("action") != "rewound" and not result.get("reclassified_job_ids"):
        return
    debug_trace.trace_event(
        SimpleNamespace(user_id=user_id),
        subsystem="memory",
        type="memory.dream.ledger_rewound",
        actor="admin",
        status="warning",
        summary="管理员回退了被假「没有卡」推进的做梦账本",
        explain=(
            "09-10/09-13 读卡超时被记成做梦完成；只回退做梦账本字段，"
            "下一次调度照常判断要不要做梦。只记编号与计数，不含卡片内容。"
        ),
        detail={
            "reason": "false_no_cards_repair",
            "job_id": audit["job_id"],
            "restore_from_job_id": audit["restore_from_job_id"],
            "action": result.get("action"),
            "fields": sorted(result.get("changes") or {}),
            "reclassified_job_ids": audit["reclassified_job_ids"],
        },
        job_id=audit["job_id"],
    )


def _reclassify_jobs(user_id: str, jobs: Iterable[Mapping[str, Any]],
                     job_ids: Iterable[str]) -> list[str]:
    """Mark the rewound false completions failed; returns the ids that changed."""
    wanted = set(job_ids)
    reason = dream_scheduler.CONTEXT_UNAVAILABLE_REASON
    changed: list[str] = []
    repaired_at = _now_iso()
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        if job_id not in wanted or dream_ledger_audit.repair_marker(job):
            continue
        dream_result = job.get("dream_result") if isinstance(job.get("dream_result"), Mapping) else {}
        patch = {
            "status": "failed",
            "status_reason": reason,
            "noop_reason": reason,
            "failed_at": job.get("completed_at"),
            "dream_result": {**dict(dream_result), "status": "failed", "reason": reason},
            dream_ledger_audit.REPAIR_MARKER_KEY: {
                "original_status": "completed",
                "original_completed_at": job.get("completed_at"),
                "original_reason": str(job.get("status_reason") or "")[:120],
                "repaired_at": repaired_at,
                "by": "admin_dream_ledger_repair",
            },
            "updated_at": datetime.now().isoformat(),
        }
        if db.log_patch_item(
            user_id, "proactive_jobs", job_id, patch, only_if_status="completed",
        ) is not None:
            changed.append(job_id)
    return changed


def _result_row(user_id: str, action: str, reason: str,
                row: Mapping[str, Any] | None = None, **extra) -> dict[str, Any]:
    out: dict[str, Any] = {"user_id": user_id, "action": action, "reason": reason}
    if row is not None:
        out["job_id"] = row.get("job_id") or ""
        out["restore_from_job_id"] = row.get("restore_from_job_id") or ""
        out["rewound_job_ids"] = list(row.get("rewound_job_ids") or [])
        out["ledger_fingerprint_current"] = row.get("ledger_fingerprint") or ""
    out.update(extra)
    return out


def repair_payload(params: Mapping[str, Any], *, clock=time.monotonic) -> dict[str, Any]:
    """Re-select the named users, then (unless dry-run) CAS-rewind each ledger.

    ``action`` per user: ``would_rewind`` (dry run), ``rewound``,
    ``already_repaired`` (ledger already at the target; apply mode only finishes
    any ``unreclassified_job_ids``), ``skipped`` (``reason`` = the selector
    verdict, ``ledger_changed_since_audit``, or ``ledger_missing``) or
    ``not_attempted`` (write budget exhausted). Re-running is idempotent.
    """
    dry_run = bool(params["dry_run"])
    targets = list(params["targets"])
    windows = params["windows"]
    started = clock()
    inputs = _read_inputs(
        windows=windows,
        user_ids=[target["user_id"] for target in targets],
        max_job_age_days=params["max_job_age_days"],
        statement_timeout_sec=REPAIR_READ_STATEMENT_TIMEOUT_SEC,
    )
    results: list[dict[str, Any]] = []
    for target in targets:
        user_id = target["user_id"]
        expected = target["ledger_fingerprint"]
        if user_id not in inputs["jobs_by_user"]:
            results.append(_result_row(user_id, "skipped", "no_incident_completion"))
            continue
        verdict, row = dream_ledger_audit.select_user(
            user_id,
            inputs["jobs_by_user"][user_id],
            inputs["ledgers"].get(user_id),
            windows=windows,
            v2_last_completed=inputs["v2_last_completed"].get(user_id),
            ledger_tolerance_sec=params["ledger_tolerance_sec"],
        )
        if verdict == "already_repaired":
            pending = list(row["unreclassified_job_ids"])
            if dry_run or not pending:
                results.append(_result_row(
                    user_id, "already_repaired", verdict, row,
                    reclassified_job_ids=[], unreclassified_job_ids=pending,
                ))
                continue
            done = _reclassify_jobs(user_id, inputs["jobs_by_user"][user_id], pending)
            result = _result_row(
                user_id, "already_repaired", verdict, row,
                reclassified_job_ids=done,
                unreclassified_job_ids=[job_id for job_id in pending if job_id not in done],
            )
            results.append(result)
            _emit_repair_event(user_id, result)
            continue
        if verdict != "candidate":
            result = _result_row(user_id, "skipped", verdict)
            results.append(result)
            if not dry_run:
                _emit_repair_event(user_id, result)
            continue
        if row["ledger_fingerprint"] != expected:
            result = _result_row(user_id, "skipped", "ledger_changed_since_audit", row)
            results.append(result)
            if not dry_run:
                _emit_repair_event(user_id, result)
            continue
        changes = _ledger_changes(row["expected_ledger"], row["restore_ledger"])
        if dry_run:
            results.append(_result_row(
                user_id, "would_rewind", verdict, row, changes=changes,
                would_reclassify_job_ids=list(row["unreclassified_job_ids"]),
            ))
            continue
        if clock() - started > REPAIR_WRITE_BUDGET_SEC:
            results.append(_result_row(user_id, "not_attempted", "request_budget_exhausted", row))
            continue
        applied, persisted = db.patch_blob_if_match_strict(
            user_id,
            dream_ledger_audit.DREAM_STATE_KIND,
            dict(row["restore_ledger"]),
            precondition=lambda doc, fp=expected: (
                dream_ledger_audit.ledger_fingerprint(doc) == fp
            ),
            statement_timeout_ms=REPAIR_WRITE_STATEMENT_TIMEOUT_MS,
        )
        if applied:
            done = _reclassify_jobs(
                user_id, inputs["jobs_by_user"][user_id], row["unreclassified_job_ids"],
            )
            result = _result_row(
                user_id, "rewound", verdict, row, changes=changes,
                ledger_fingerprint_after=dream_ledger_audit.ledger_fingerprint(persisted),
                reclassified_job_ids=done,
                unreclassified_job_ids=[
                    job_id for job_id in row["unreclassified_job_ids"] if job_id not in done
                ],
            )
        else:
            result = _result_row(
                user_id, "skipped",
                "ledger_changed_since_audit" if persisted is not None else "ledger_missing",
                row,
            )
        results.append(result)
        _emit_repair_event(user_id, result)
    counts: dict[str, int] = {}
    for result in results:
        counts[result["action"]] = counts.get(result["action"], 0) + 1
    return {
        "mode": "dry_run" if dry_run else "apply",
        "windows": [
            [dream_ledger_audit.format_instant(start), dream_ledger_audit.format_instant(end)]
            for start, end in windows
        ],
        "requested": len(targets),
        "counts": dict(sorted(counts.items())),
        "results": results,
        "prefilter": inputs["prefilter"],
        "generated_at": _now_iso(),
    }
