"""Guarded full-table recovery for ciphertext-lane replication.

The ordinary worker deliberately resumes from a durable high-water mark.  This
module scans from the table's true lower bound without rewinding that cursor,
so rows written under an older transform policy can be rebuilt idempotently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import db
from tee_replicator import transforms, worker
from tee_shadow import ciphertext_prune, mirror

log = logging.getLogger("feedling.tee_reflow")


@dataclass(frozen=True)
class _RecoveryContract:
    fetch_sql: str
    delete_sql: str
    by_user_only: bool = False

    def args(self, user_id: str, item_id: str) -> tuple:
        return (user_id,) if self.by_user_only else (user_id, item_id)


_RECOVERY_CONTRACTS = {
    "chat_messages": _RecoveryContract(
        "SELECT user_id,msg_id,ts,doc,seq,storage_generation FROM chat_messages "
        "WHERE user_id=%s AND msg_id=%s",
        "DELETE FROM chat_messages WHERE user_id=%s AND msg_id=%s",
    ),
    "memory_moments": _RecoveryContract(
        "SELECT user_id,moment_id,occurred_at,doc FROM memory_moments "
        "WHERE user_id=%s AND moment_id=%s",
        "DELETE FROM memory_moments WHERE user_id=%s AND moment_id=%s",
    ),
    "world_book_entries": _RecoveryContract(
        "SELECT user_id,entry_id,updated_at,doc FROM world_book_entries "
        "WHERE user_id=%s AND entry_id=%s",
        "DELETE FROM world_book_entries WHERE user_id=%s AND entry_id=%s",
    ),
    "identity": _RecoveryContract(
        "SELECT user_id,doc FROM user_blobs WHERE kind='identity' AND user_id=%s",
        "DELETE FROM user_blobs WHERE user_id=%s AND kind='identity'",
        by_user_only=True,
    ),
    "voice_transcripts": _RecoveryContract(
        "SELECT user_id,call_id,chat_message_id,turn_count,duration_sec,char_count,"
        "created_at,transcript_envelope FROM voice_transcripts "
        "WHERE user_id=%s AND call_id=%s",
        "DELETE FROM voice_transcripts WHERE user_id=%s AND call_id=%s",
    ),
}

SUPPORTED_TABLES = tuple(sorted(_RECOVERY_CONTRACTS))


def _existing_pending(table: str) -> set[tuple[str, str]]:
    with mirror.get_tee_pool().connection() as conn:
        return {
            (str(user_id), str(item_id))
            for user_id, item_id in conn.execute(
                "SELECT user_id,item_id FROM tee_pending_device_migration "
                "WHERE table_name=%s",
                (table,),
            )
        }


def _orphan_pending(contract: _RecoveryContract, table: str) -> list[tuple[str, str]]:
    with mirror.get_tee_pool().connection() as dst:
        pending = [
            (str(user_id), str(item_id))
            for user_id, item_id in dst.execute(
                "SELECT user_id,item_id FROM tee_pending_device_migration "
                "WHERE table_name=%s",
                (table,),
            )
        ]
    missing: list[tuple[str, str]] = []
    with db.get_pool().connection() as src:
        for user_id, item_id in pending:
            if src.execute(contract.fetch_sql, contract.args(user_id, item_id)).fetchone() is None:
                missing.append((user_id, item_id))
    return missing


def _delete_orphan_pending(contract, table: str, rows: list[tuple[str, str]]) -> int:
    deleted = 0
    with mirror.get_tee_pool().connection() as dst, dst.transaction():
        for user_id, item_id in rows:
            dst.execute(contract.delete_sql, contract.args(user_id, item_id))
            deleted += dst.execute(
                worker._PENDING_DELETE, (user_id, table, item_id)
            ).rowcount
    return deleted


def _write_batch(cfg, contract, table: str, writes, terminal) -> int:
    cleared = 0
    with mirror.get_tee_pool().connection() as dst, dst.transaction():
        for args, user_id, item_id in writes:
            dst.execute(cfg.upsert_sql, args)
            cleared += dst.execute(
                worker._PENDING_DELETE, (user_id, table, item_id)
            ).rowcount
        for user_id, item_id, reason in terminal:
            dst.execute(contract.delete_sql, contract.args(user_id, item_id))
            dst.execute(worker._PENDING_UPSERT, (user_id, table, item_id, reason))
    return cleared


def reflow_table(
    table: str,
    *,
    qps: float = 2.0,
    dry_run: bool = True,
    expected_stale: int | None = None,
) -> dict:
    """Replay one complete source table and converge stale rows/pending markers.

    ``expected_stale`` is an exact compare-and-delete guard.  It only overrides
    the normal percentage/absolute prune threshold when the live stale count is
    exactly the value previously observed in dry-run output.
    """
    contract = _RECOVERY_CONTRACTS[table]
    cfg = worker._TABLES[table]
    wm_ts, wm_id = 0.0, ""
    existing_pending = _existing_pending(table)
    successful_keys: set[tuple[str, str]] = set()
    scanned = would_copy = copied = pending = quarantined = errors = 0
    pending_cleared = 0

    with db.get_pool().connection() as src:
        first_page = True
        while True:
            if first_page and cfg.cursor_kind == "numeric":
                cursor_args = (float("-inf"), "")
            else:
                cursor_args = worker._decode_cursor(cfg, wm_ts, wm_id)
            rows = src.execute(
                cfg.select_sql,
                (*cursor_args, worker.BATCH),
            ).fetchall()
            first_page = False
            if not rows:
                break
            writes = []
            terminal = []
            for row in rows:
                user_id, item_id, sort_val, doc = cfg.unpack(row)
                scanned += 1
                try:
                    args = worker._produce_write(
                        cfg, user_id, item_id, sort_val, doc, dry_run
                    )
                except transforms.PendingDeviceMigration as exc:
                    pending += 1
                    terminal.append((user_id, item_id, worker._pdm_reason(exc)))
                except transforms.PermanentDecryptFailure as exc:
                    quarantined += 1
                    terminal.append(
                        (user_id, item_id, worker._decrypt_failed_reason(exc))
                    )
                except Exception as exc:  # noqa: BLE001 - report and leave reflow resumable
                    errors += 1
                    if not dry_run:
                        worker._log_row_error(table, user_id, item_id, exc)
                    else:
                        log.warning("[tee-reflow] dry-run %s/%s: %s", table, item_id, exc)
                else:
                    would_copy += 1
                    successful_keys.add((str(user_id), str(item_id)))
                    if args is not None:
                        writes.append((args, user_id, item_id))
                wm_ts, wm_id = worker._encode_cursor(cfg, sort_val, item_id)

            if not dry_run:
                try:
                    pending_cleared += _write_batch(cfg, contract, table, writes, terminal)
                    copied += len(writes)
                except Exception as exc:  # noqa: BLE001 - prior batches are idempotent
                    errors += 1
                    log.warning("[tee-reflow] %s batch write failed: %s", table, exc)
                    break
            if qps and qps > 0:
                worker._sleep(len(rows) / qps)
            if len(rows) < worker.BATCH:
                break

    orphan_rows = _orphan_pending(contract, table)
    orphan_deleted = 0
    prune = {
        "stale": 0,
        "deleted": 0,
        "refused": None,
        "error": None,
    }
    if errors == 0:
        if not dry_run:
            orphan_deleted = _delete_orphan_pending(contract, table, orphan_rows)
        if cfg.prune_rds_keys_sql and cfg.prune_tee_keys_sql:
            prune = ciphertext_prune.prune_table(
                table,
                dry_run=dry_run,
                expected_stale=expected_stale,
            )

    if not dry_run and errors == 0:
        worker.finalize_identity_sequence(table)
    failures = errors + int(bool(prune.get("error"))) + int(bool(prune.get("refused")))
    report = {
        "table": table,
        "scanned": scanned,
        "would_copy": would_copy,
        "copied": copied,
        "pending": pending,
        "quarantined": quarantined,
        "errors": errors,
        "pending_cleared": (
            len(existing_pending & successful_keys) if dry_run else pending_cleared
        ),
        "orphan_pending": len(orphan_rows),
        "orphan_pending_deleted": orphan_deleted,
        "stale": prune.get("stale", 0),
        "deleted": prune.get("deleted", 0),
        "refused": prune.get("refused"),
        "prune_error": prune.get("error"),
        "failures": failures,
        "ok": failures == 0,
        "watermark_ts": wm_ts,
        "watermark_id": wm_id,
    }
    log.info("[tee-reflow] %s", report)
    return report
