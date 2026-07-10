"""Admin-triggered TEE replication controls (spec §5 执行防护).

Thin wrapper around the CLI-level entry points already implemented and
tested by earlier tasks:

  - ``tee_shadow.reconciler.reconcile_table`` / ``reconcile_all`` — full-table
    backfill/compensation.
  - ``tee_replicator.worker.run_table`` — cursor-driven ciphertext→plaintext
    replication (hits the enclave for real decrypts).
  - ``tee_shadow.verify.run`` — read-only sampled consistency check.

This module owns no business logic of its own beyond the execution
guardrails (spec §5 四要素): a literal ``confirm == "MIGRATE"`` gate on any
non-dry-run write action, and a non-blocking module-level lock so at most one
run is in flight at a time (a second concurrent call gets rejected rather
than queued — prod user count is tiny, so running synchronously and refusing
overlap is simpler than a job queue). Note the cost of that choice: a real
(non-dry-run) replicate/reconcile occupies one anyio worker thread for the
whole run — minutes at the default qps=2 — acceptable at current scale;
revisit (background job + status polling) if user count grows.

``verify`` is read-only by construction (spec §5.1/§7): it never needs
``confirm`` regardless of the requested ``dry_run`` value.
"""

from __future__ import annotations

import os
import threading

from tee_replicator import worker as tee_worker
from tee_shadow import mirror
from tee_shadow import reconciler as tee_reconciler
from tee_shadow import verify as tee_verify

_ACTIONS = ("reconcile", "replicate", "verify")

# Non-blocking guard: two concurrent /run calls must not stomp on each other
# (reconcile/replicate both do multi-statement writes against the TEE pool).
# The second caller gets rejected (409) rather than blocked/queued.
_run_lock = threading.Lock()


class BadRequest(Exception):
    """Maps 1:1 to a 400 response; ``error`` is the machine-readable code."""

    def __init__(self, error: str):
        self.error = error
        super().__init__(error)


class AlreadyRunning(Exception):
    """Raised when another run holds ``_run_lock``; maps to 409."""


class Unconfigured(Exception):
    """Raised when ``TEE_DATABASE_URL`` is unset; maps to a clean 503.

    Without it, ``mirror.get_tee_pool()`` does ``os.environ["TEE_DATABASE_URL"]``
    and raises a bare KeyError → an ugly 500 traceback on every status/run call
    in an env where the shadow DB simply isn't provisioned yet (the whole point
    of the phased rollout)."""


def _require_tee_configured() -> None:
    if not os.environ.get("TEE_DATABASE_URL"):
        raise Unconfigured()


def _validate(action: str, table: str | None, dry_run) -> None:
    if action not in _ACTIONS:
        raise BadRequest("unknown_action")
    # Strict bool: JSON strings like "false" are truthy in Python, which would
    # silently invert the meaning of the confirm gate — reject anything non-bool.
    if not isinstance(dry_run, bool):
        raise BadRequest("invalid_dry_run")
    if action == "reconcile" and table is not None and table not in tee_reconciler.TABLES:
        raise BadRequest("unknown_table")
    if action == "replicate":
        if not table:
            raise BadRequest("table_required")
        if table not in tee_worker._TABLES:
            raise BadRequest("unknown_table")


def run_action(
    *,
    action: str,
    table: str | None = None,
    dry_run: bool = True,
    confirm: str | None = None,
    qps: float | None = None,
    sample_rate: float | None = None,
) -> dict:
    """Validate + dispatch one admin-triggered replication action.

    Raises ``BadRequest`` (→ 400) or ``AlreadyRunning`` (→ 409) on guardrail
    violations; otherwise returns the operation's report dict with
    ``action``/``dry_run`` echoed in.
    """
    _validate(action, table, dry_run)
    if action != "verify" and not dry_run and confirm != "MIGRATE":
        raise BadRequest("confirm_required")
    # A dry-run reconcile never touches the TEE pool (plan-only short-circuit),
    # but every other path (verify, replicate, non-dry reconcile) does — guard
    # them so a missing shadow DB yields a clean 503 instead of a KeyError 500.
    if not (action == "reconcile" and dry_run):
        _require_tee_configured()

    if not _run_lock.acquire(blocking=False):
        raise AlreadyRunning()
    try:
        if action == "reconcile":
            report = _run_reconcile(table=table, dry_run=dry_run)
        elif action == "replicate":
            report = _run_replicate(table=table, dry_run=dry_run, qps=qps)
        else:
            report = _run_verify(sample_rate=sample_rate)
    finally:
        _run_lock.release()

    report = dict(report)
    report["action"] = action
    report["dry_run"] = dry_run
    return report


def _run_reconcile(*, table: str | None, dry_run: bool) -> dict:
    if dry_run:
        # reconcile has no dry_run concept of its own (spec §5.4) — the admin
        # surface fakes one by returning the would-run table plan without
        # calling into the reconciler at all (zero reads/writes either side).
        return {"plan": [table] if table else list(tee_reconciler.TABLES)}
    if table:
        return {"tables": [tee_reconciler.reconcile_table(table)]}
    return {"tables": tee_reconciler.reconcile_all()}


def _run_replicate(*, table: str, dry_run: bool, qps: float | None) -> dict:
    kwargs: dict = {"dry_run": dry_run}
    if qps is not None:
        kwargs["qps"] = qps
    return tee_worker.run_table(table, **kwargs)


def _run_verify(*, sample_rate: float | None) -> dict:
    kwargs: dict = {}
    if sample_rate is not None:
        kwargs["sample_rate"] = sample_rate
    return tee_verify.run(**kwargs)


def status_payload() -> dict:
    """Read-only snapshot: replication cursors, pending-migration counts,
    mirror failure counter, dual-write flag, and whether a run is in flight."""
    _require_tee_configured()
    with mirror.get_tee_pool().connection() as conn:
        cursor_rows = conn.execute(
            "SELECT table_name, watermark_ts, watermark_id, updated_at "
            "FROM tee_replication_cursors ORDER BY table_name"
        ).fetchall()
        pending_rows = conn.execute(
            "SELECT table_name, count(*) FROM tee_pending_device_migration "
            "GROUP BY table_name ORDER BY table_name"
        ).fetchall()

    cursors = [
        {
            "table_name": row[0],
            "watermark_ts": row[1],
            "watermark_id": row[2],
            "updated_at": row[3].isoformat() if row[3] is not None else None,
        }
        for row in cursor_rows
    ]
    pending_by_table = {row[0]: row[1] for row in pending_rows}

    return {
        "cursors": cursors,
        "pending_count": sum(pending_by_table.values()),
        "pending_by_table": pending_by_table,
        "mirror_failures": mirror.failure_count(),
        "dual_write_enabled": mirror.enabled(),
        "running": _run_lock.locked(),
    }
