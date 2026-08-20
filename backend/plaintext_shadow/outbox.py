"""Durable dirty-key drain for the decrypted plaintext shadow."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import db
from plaintext_shadow import config as plaintext_shadow_config
from psycopg.types.json import Jsonb
from tee_replicator import worker
from tee_shadow import reconciler, snapshot, table_registry


_CLAIM_LEASE = timedelta(minutes=5)
_QUARANTINE_AFTER = 20
_ERROR_SLUG = "shadow_apply_failed"


@dataclass(frozen=True)
class DirtyKey:
    table_name: str
    key_json: dict[str, Any]
    operation: str
    generation: int
    attempts: int


@dataclass
class DrainReport:
    claimed: int = 0
    applied: int = 0
    deleted: int = 0
    pending: int = 0
    retried: int = 0
    quarantined: int = 0


def retry_delay(attempts: int) -> timedelta:
    """Bounded 30s exponential retry delay for a one-based attempt count."""
    exponent = max(0, int(attempts) - 1)
    return timedelta(seconds=min(300, 30 * (2**exponent)))


def _claim(limit: int) -> list[DirtyKey]:
    if limit <= 0:
        return []
    with db.get_pool().connection() as conn, conn.transaction():
        rows = conn.execute(
            "SELECT table_name, key_json, operation, generation, attempts "
            "FROM plaintext_shadow_dirty_keys "
            "WHERE quarantined_at IS NULL AND next_attempt_at <= now() "
            "ORDER BY generation FOR UPDATE SKIP LOCKED LIMIT %s",
            (limit,),
        ).fetchall()
        claimed = [DirtyKey(*row) for row in rows]
        for row in claimed:
            conn.execute(
                "UPDATE plaintext_shadow_dirty_keys "
                "SET next_attempt_at=now() + %s "
                "WHERE table_name=%s AND key_json=%s AND generation=%s",
                (_CLAIM_LEASE, row.table_name, Jsonb(row.key_json), row.generation),
            )
    return claimed


def _ack(row: DirtyKey) -> bool:
    with db.get_pool().connection() as conn:
        result = conn.execute(
            "DELETE FROM plaintext_shadow_dirty_keys "
            "WHERE table_name=%s AND key_json=%s AND generation=%s",
            (row.table_name, Jsonb(row.key_json), row.generation),
        )
        return result.rowcount == 1


def _fail(row: DirtyKey) -> bool:
    attempts = row.attempts + 1
    quarantine = attempts >= _QUARANTINE_AFTER
    with db.get_pool().connection() as conn:
        result = conn.execute(
            "UPDATE plaintext_shadow_dirty_keys SET "
            "attempts=%s, next_attempt_at=now() + %s, last_error_slug=%s, "
            "quarantined_at=CASE WHEN %s THEN now() ELSE NULL END "
            "WHERE table_name=%s AND key_json=%s AND generation=%s",
            (
                attempts,
                retry_delay(attempts),
                _ERROR_SLUG,
                quarantine,
                row.table_name,
                Jsonb(row.key_json),
                row.generation,
            ),
        )
    return bool(quarantine and result.rowcount == 1)


def _quarantine(row: DirtyKey, slug: str) -> bool:
    """Keep a terminal, content-free gap visible until an operator repairs it."""
    with db.get_pool().connection() as conn:
        result = conn.execute(
            "UPDATE plaintext_shadow_dirty_keys SET attempts=attempts + 1, "
            "last_error_slug=%s, quarantined_at=now() "
            "WHERE table_name=%s AND key_json=%s AND generation=%s",
            (slug, row.table_name, Jsonb(row.key_json), row.generation),
        )
    return result.rowcount == 1


def apply_key(row: DirtyKey, *, target_policy=None) -> dict:
    """Apply current authoritative state; the captured operation is advisory."""
    entry = table_registry.REGISTRY.get(row.table_name)
    if entry is None or entry.lane in (table_registry.SKIP, table_registry.LOGICAL):
        raise RuntimeError("plaintext shadow registry lane is not replayable")
    if row.table_name == "user_blobs" and row.key_json.get("kind") == "identity":
        return worker.run_keys("identity", [row.key_json], target_policy=target_policy)
    if entry.lane == table_registry.CIPHERTEXT:
        return worker.run_keys(
            row.table_name, [row.key_json], target_policy=target_policy
        )
    if entry.lane == table_registry.MIRROR:
        return reconciler.reconcile_keys(
            row.table_name, [row.key_json], target_policy=target_policy
        )
    if entry.lane == table_registry.SNAPSHOT:
        return snapshot.snapshot_table(row.table_name, target_policy=target_policy)
    raise RuntimeError("plaintext shadow registry lane is unsupported")


def _fold(report: DrainReport, result: dict | None) -> None:
    result = result or {}
    report.applied += int(result.get("applied", result.get("copied", 0)) or 0)
    report.deleted += int(result.get("deleted", result.get("pruned", 0)) or 0)
    report.pending += int(result.get("pending", 0) or 0)


def drain_once(*, limit: int = 500) -> DrainReport:
    """Claim ready keys briefly, then perform remote work without a source lock."""
    rows = _claim(limit)
    report = DrainReport(claimed=len(rows))
    target_policy = plaintext_shadow_config.load_target()

    # A snapshot is already table-wide.  Apply it once per claimed table, but
    # generation-safe acknowledge every marker represented by that run.
    snapshot_results: dict[str, dict] = {}
    folded_snapshots: set[str] = set()
    for row in rows:
        try:
            entry = table_registry.REGISTRY.get(row.table_name)
            if entry is not None and entry.lane == table_registry.SNAPSHOT:
                if row.table_name not in snapshot_results:
                    snapshot_results[row.table_name] = apply_key(
                        row, target_policy=target_policy
                    )
                result = snapshot_results[row.table_name]
                if not result.get("ok", True):
                    raise RuntimeError("plaintext shadow snapshot failed")
            else:
                result = apply_key(row, target_policy=target_policy)
            if entry is None or entry.lane != table_registry.SNAPSHOT:
                _fold(report, result)
            elif row.table_name not in folded_snapshots:
                _fold(report, result)
                folded_snapshots.add(row.table_name)
            if int((result or {}).get("pending", 0) or 0):
                if _quarantine(row, "pending_device_migration"):
                    report.quarantined += 1
                continue
            _ack(row)
        except Exception:  # noqa: BLE001 - persist only a fixed, content-free slug
            if _fail(row):
                report.quarantined += 1
            else:
                report.retried += 1
    return report
