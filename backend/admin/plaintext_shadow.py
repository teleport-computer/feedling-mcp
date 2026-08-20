"""Operator gates for the TEE-primary decrypted plaintext shadow."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any

import db
from plaintext_shadow import change_capture, config, outbox
from psycopg import sql
from psycopg.types.json import Jsonb
from tee_replicator import transforms
from tee_replicator import worker
from tee_shadow import mirror, reconciler, snapshot, table_registry, verify


_SCHEMA_HEAD = "0026_plaintext_shadow_control"
_INSERT_SHAPE = re.compile(
    r"INSERT\s+INTO\s+([a-zA-Z0-9_]+)\s*\((.*?)\)\s*"
    r"(?:OVERRIDING\s+SYSTEM\s+VALUE\s*)?VALUES",
    re.IGNORECASE | re.DOTALL,
)
def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _endpoint_fingerprint(dsn: str) -> str:
    host, port, database = config._database_identity(dsn)
    return _fingerprint(f"{host}:{port}/{database}")


def _head(conn) -> str | None:
    row = conn.execute(
        "SELECT to_regclass('public.alembic_tee_version')"
    ).fetchone()
    if not row or row[0] is None:
        return None
    version = conn.execute("SELECT version_num FROM alembic_tee_version").fetchone()
    return None if version is None else str(version[0])


def _restore_evidence(
    conn,
    *,
    target_fingerprint: str | None = None,
    minimum_capacity_bytes: int = 0,
    target_connection_limit: int = 0,
) -> bool:
    clauses = [
        "expires_at > now()",
        "source_backup_at >= now() - interval '24 hours'",
        "schema_head=%s",
        "ha_verified",
    ]
    params: list[Any] = [_SCHEMA_HEAD]
    if target_fingerprint is not None:
        clauses.append("target_fingerprint=%s")
        params.append(target_fingerprint)
    if minimum_capacity_bytes:
        clauses.append("target_capacity_bytes >= %s")
        params.append(minimum_capacity_bytes)
    if target_connection_limit:
        clauses.append("target_connection_limit=%s")
        params.append(target_connection_limit)
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM plaintext_shadow_restore_evidence "
        f"WHERE {' AND '.join(clauses)})",
        tuple(params),
    ).fetchone()
    return bool(row and row[0])


def preflight() -> dict:
    policy = config.require_target()
    failures: list[str] = []
    primary_dsn = os.environ.get("DATABASE_URL", "")
    result: dict[str, Any] = {
        "primary_fingerprint": _endpoint_fingerprint(primary_dsn),
        "target_fingerprint": _endpoint_fingerprint(policy.dsn),
        "schema_head": _SCHEMA_HEAD,
    }

    with db.get_pool().connection() as primary, mirror.get_target_pool(
        policy
    ).connection() as target:
        try:
            config.validate_live_topology(primary, target)
        except RuntimeError:
            failures.append("primary_shadow_alias")
        primary_head = _head(primary)
        target_head = _head(target)
        result["primary_head"] = primary_head
        result["target_head"] = target_head
        if primary_head != _SCHEMA_HEAD:
            failures.append("primary_migration_mismatch")
        if target_head != _SCHEMA_HEAD:
            failures.append("target_migration_mismatch")

        version = int(target.execute("SHOW server_version_num").fetchone()[0])
        result["target_server_major"] = version // 10000
        if version < 170000:
            failures.append("target_postgres_version")
        read_only = target.execute("SHOW transaction_read_only").fetchone()[0]
        writable = read_only == "off" and bool(
            target.execute(
                "SELECT has_table_privilege(current_user, 'server_config', "
                "'INSERT,UPDATE,DELETE')"
            ).fetchone()[0]
        )
        result["target_writable"] = writable
        if not writable:
            failures.append("target_not_writable")
        ssl_row = target.execute(
            "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
        ).fetchone()
        tls = bool(ssl_row and ssl_row[0])
        result["target_tls"] = tls
        if not tls:
            failures.append("target_tls_required")

        target_bytes = int(
            target.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        )
        primary_bytes = int(
            primary.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        )
        result["primary_bytes"] = primary_bytes
        result["target_bytes"] = target_bytes
        max_connections = int(target.execute("SHOW max_connections").fetchone()[0])
        active_connections = int(
            target.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
            ).fetchone()[0]
        )
        connection_headroom = max_connections - active_connections
        result["target_max_connections"] = max_connections
        result["target_connection_headroom"] = connection_headroom
        if connection_headroom < 10:
            failures.append("target_connection_headroom")

        trigger_report = change_capture.audit(primary)
        result["trigger_audit_ok"] = trigger_report.ok
        if not trigger_report.ok:
            failures.append("trigger_drift")
        evidence_ok = _restore_evidence(
            primary,
            target_fingerprint=result["target_fingerprint"],
            minimum_capacity_bytes=max(primary_bytes * 2, target_bytes),
            target_connection_limit=max_connections,
        )
        result["restore_evidence_ok"] = evidence_ok
        if not evidence_ok:
            failures.append("restore_evidence_missing")

    result["failure_slugs"] = sorted(set(failures))
    result["ok"] = not result["failure_slugs"]
    return result


def record_restore_evidence(args) -> dict:
    policy = config.require_target()
    target_fingerprint = _endpoint_fingerprint(policy.dsn)
    with db.get_pool().connection() as primary, mirror.get_target_pool(
        policy
    ).connection() as target:
        config.validate_live_topology(primary, target)
        target_connection_limit = int(
            target.execute("SHOW max_connections").fetchone()[0]
        )
    values = (
        _parse_time(args.restored_at),
        _parse_time(args.source_backup_at),
        args.schema_head,
        args.verifier_digest,
        args.backup_artifact_digest,
        target_fingerprint,
        int(args.target_capacity_bytes),
        target_connection_limit,
        args.ha_verified == "true",
        args.operator_id,
        _parse_time(args.expires_at),
    )
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO plaintext_shadow_restore_evidence "
            "(restored_at, source_backup_at, schema_head, verifier_digest, "
            "backup_artifact_digest, target_fingerprint, target_capacity_bytes, "
            "target_connection_limit, ha_verified, operator_id, expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            values,
        ).fetchone()
    return {"ok": True, "evidence_id": int(row[0])}


def _parse_time(raw: str) -> datetime:
    value = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def install_triggers() -> dict:
    config.require_target()
    with db.get_pool().connection() as conn:
        report = change_capture.install(conn)
    return {"ok": not report.missing_tables, **asdict(report)}


def remove_triggers() -> dict:
    with db.get_pool().connection() as conn:
        change_capture.remove(conn)
    return {"ok": True}


def _capture_high_water() -> int:
    with db.get_pool().connection() as conn:
        return int(
            conn.execute(
                "SELECT COALESCE(max(generation), 0) FROM plaintext_shadow_dirty_keys"
            ).fetchone()[0]
        )


def _backfill_mirror(policy) -> list[dict]:
    return [
        reconciler.reconcile_table(table, target_policy=policy)
        for table in reconciler.TABLES
    ]


def _backfill_ciphertext(policy) -> list[dict]:
    return [
        worker.run_table(table, target_policy=policy)
        for table in sorted(worker._TABLES)
    ]


def _backfill_snapshot(policy) -> list[dict]:
    return [
        snapshot.snapshot_table(table, target_policy=policy)
        for table in snapshot.snapshot_order()
    ]


def _drain_to_high_water(high_water: int, *, limit: int = 500) -> dict:
    totals = {"claimed": 0, "applied": 0, "deleted": 0, "pending": 0,
              "retried": 0, "quarantined": 0}
    while True:
        report = outbox.drain_once(limit=limit)
        for key in totals:
            totals[key] += int(getattr(report, key))
        if report.claimed == 0:
            break
    with db.get_pool().connection() as conn:
        pending_through = int(
            conn.execute(
                "SELECT count(*) FROM plaintext_shadow_dirty_keys WHERE generation <= %s",
                (high_water,),
            ).fetchone()[0]
        )
        pending_total = int(
            conn.execute("SELECT count(*) FROM plaintext_shadow_dirty_keys").fetchone()[0]
        )
    return {**totals, "pending_through_high_water": pending_through,
            "pending_total": pending_total}


def _require_trigger_audit() -> None:
    with db.get_pool().connection() as conn:
        if not change_capture.audit(conn).ok:
            raise RuntimeError("plaintext shadow trigger audit is not green")


def _require_live_topology(policy) -> None:
    with db.get_pool().connection() as primary, mirror.get_target_pool(
        policy
    ).connection() as target:
        config.validate_live_topology(primary, target)


def backfill() -> dict:
    policy = config.require_target()
    _require_live_topology(policy)
    _require_trigger_audit()
    high_water = _capture_high_water()
    mirror_reports = _backfill_mirror(policy)
    ciphertext_reports = _backfill_ciphertext(policy)
    snapshot_reports = _backfill_snapshot(policy)
    drain_report = _drain_to_high_water(high_water)
    snapshot_failures = sum(not report.get("ok", True) for report in snapshot_reports)
    ciphertext_failures = sum(
        int(report.get(field, 0))
        for report in ciphertext_reports
        for field in ("pending", "errors", "skipped", "quarantined")
    )
    return {
        "ok": snapshot_failures == 0
        and ciphertext_failures == 0
        and drain_report["pending_through_high_water"] == 0,
        "high_water_generation": high_water,
        "mirror_tables": len(mirror_reports),
        "ciphertext_tables": len(ciphertext_reports),
        "snapshot_tables": len(snapshot_reports),
        "snapshot_failures": snapshot_failures,
        "ciphertext_failures": ciphertext_failures,
        "drain": drain_report,
    }


def drain(limit: int = 500) -> dict:
    policy = config.require_target()
    _require_live_topology(policy)
    return asdict(outbox.drain_once(limit=limit))


def _unexpected_ciphertext_count(policy) -> int:
    count = 0
    target_tables = sorted(
        {
            entry.destination_table
            for entry in table_registry.REGISTRY.values()
            if entry.lane == table_registry.CIPHERTEXT
        }
        | {"user_blobs"}
    )
    with mirror.get_target_pool(policy).connection() as conn:
        for table in target_tables:
            columns = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s AND data_type='jsonb'",
                (table,),
            ).fetchall()
            for (column,) in columns:
                column_id = sql.Identifier(column)
                query = sql.SQL(
                    "SELECT count(*) FROM {} WHERE "
                    "{} ? 'K_enclave' OR {} ? 'K_user' OR "
                    "({} ? 'body_ct' AND {} ? 'nonce') OR "
                    "({} ? 'ciphertext' AND {} ? 'nonce')"
                ).format(
                    sql.Identifier(table),
                    column_id,
                    column_id,
                    column_id,
                    column_id,
                    column_id,
                    column_id,
                )
                count += int(conn.execute(query).fetchone()[0])
    return count


def _upsert_shape(cfg) -> tuple[str, tuple[str, ...]]:
    match = _INSERT_SHAPE.search(cfg.upsert_sql)
    if match is None:
        raise RuntimeError("ciphertext upsert shape is not inspectable")
    return match.group(1), tuple(
        column.strip() for column in match.group(2).split(",")
    )


def _normal(value):
    if isinstance(value, Jsonb):
        return _normal(value.obj)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, dict):
        return {key: _normal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_normal(item) for item in value)
    return value


def _strict_extended_content(policy) -> tuple[int, int]:
    """Compare every current decryptable row, not a legacy random sample."""
    extended = set(verify._CIPHERTEXT_TABLES)
    mismatches = decrypt_failures = 0
    with db.get_pool().connection() as source, mirror.get_target_pool(
        policy
    ).connection() as target:
        for table in sorted(extended):
            cfg = worker._TABLES[table]
            verify_cfg = verify._CIPHERTEXT_TABLES[table]
            source_table = verify_cfg["rds_table"]
            source_where = (
                sql.SQL(" WHERE ") + sql.SQL(verify_cfg["rds_where"])
                if verify_cfg.get("rds_where")
                else sql.SQL("")
            )
            if table == "identity":
                key_columns = ("user_id",)
            else:
                key_columns = table_registry.REGISTRY[table].key_columns
            key_rows = source.execute(
                sql.SQL("SELECT {} FROM {}{}").format(
                    sql.SQL(", ").join(sql.Identifier(key) for key in key_columns),
                    sql.Identifier(source_table),
                    source_where,
                )
            ).fetchall()
            target_table, columns = _upsert_shape(cfg)
            target_keys = key_columns
            target_where = sql.SQL(" AND ").join(
                sql.SQL("{}=%s").format(sql.Identifier(key)) for key in target_keys
            )
            if verify_cfg.get("tee_where"):
                target_where = target_where + sql.SQL(" AND ") + sql.SQL(
                    verify_cfg["tee_where"]
                )
            target_select = sql.SQL("SELECT {} FROM {} WHERE {}").format(
                sql.SQL(", ").join(sql.Identifier(column) for column in columns),
                sql.Identifier(target_table),
                target_where,
            )
            for key_values in key_rows:
                dirty_key = dict(zip(key_columns, key_values))
                source_row = source.execute(
                    cfg.key_fetch_sql, cfg.key_params(dirty_key)
                ).fetchone()
                if source_row is None:
                    continue
                user_id, item_id, sort_value, doc = cfg.unpack(source_row)
                try:
                    expected = worker._produce_write(
                        cfg,
                        user_id,
                        item_id,
                        sort_value,
                        copy.deepcopy(doc),
                        False,
                        policy,
                    )
                except transforms.PendingDeviceMigration:
                    continue
                except Exception:  # noqa: BLE001 - expose only a scalar count
                    decrypt_failures += 1
                    continue
                if expected is None:
                    mismatches += 1
                    continue
                actual = target.execute(target_select, key_values).fetchone()
                if actual is None or _normal(expected) != _normal(actual):
                    mismatches += 1
    return mismatches, decrypt_failures


def _strict_snapshot_mismatch_count(policy, *, tables=None) -> int:
    """Compare complete SNAPSHOT rows without returning keys or row content."""
    mismatches = 0
    with db.get_pool().connection() as source, mirror.get_target_pool(
        policy
    ).connection() as target:
        for table in tables or snapshot.snapshot_order():
            keys = table_registry.REGISTRY[table].key_columns
            source_columns = snapshot._columns(source, table)
            target_columns = set(snapshot._columns(target, table))
            common = [column for column in source_columns if column in target_columns]
            if not common or any(key not in common for key in keys):
                mismatches += 1
                continue
            key_select = sql.SQL(", ").join(sql.Identifier(key) for key in keys)
            order_by = sql.SQL(", ").join(sql.Identifier(key) for key in keys)
            digest_values = sql.SQL(", ").join(
                sql.Identifier(column) for column in common
            )
            query = sql.SQL(
                "SELECT {}, md5(jsonb_build_array({})::text) "
                "FROM {} ORDER BY {}"
            ).format(key_select, digest_values, sql.Identifier(table), order_by)
            source_rows = source.execute(query).fetchall()
            target_rows = target.execute(query).fetchall()
            if source_rows != target_rows:
                mismatches += max(len(source_rows), len(target_rows), 1)
    return mismatches


def _target_pending_counts(policy) -> tuple[int, int, int]:
    """Return total, terminal, and requeue legacy target markers."""
    with mirror.get_target_pool(policy).connection() as target:
        row = target.execute(
            "SELECT count(*), "
            "count(*) FILTER (WHERE reason IS NULL OR reason NOT LIKE 'requeue%'), "
            "count(*) FILTER (WHERE reason LIKE 'requeue%') "
            "FROM tee_pending_device_migration"
        ).fetchone()
    return tuple(int(value) for value in row)


def strict_report() -> dict:
    policy = config.require_target()
    failures: list[str] = []
    gate = preflight()
    failures.extend(gate["failure_slugs"])
    with db.get_pool().connection() as conn:
        pending, quarantined = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE quarantined_at IS NOT NULL) "
            "FROM plaintext_shadow_dirty_keys"
        ).fetchone()
    if pending:
        failures.append("pending_keys")
    if quarantined:
        failures.append("quarantined_keys")
    target_pending, target_terminal, target_requeue = _target_pending_counts(policy)
    if target_pending:
        failures.append("target_pending_rows")

    consistency = verify.run(sample_rate=1.0)
    if not consistency.get("strict_ok", consistency.get("ok", False)):
        failures.append("table_count_mismatch")
    mismatch_count = len(consistency.get("mismatches", ()))
    if mismatch_count:
        failures.append("content_mismatch")
    ciphertext_count = _unexpected_ciphertext_count(policy)
    if ciphertext_count:
        failures.append("unexpected_ciphertext_shape")
    extended_mismatches, decrypt_failures = _strict_extended_content(policy)
    if extended_mismatches:
        failures.append("extended_content_mismatch")
    if decrypt_failures:
        failures.append("decrypt_failure")
    snapshot_mismatches = _strict_snapshot_mismatch_count(policy)
    if snapshot_mismatches:
        failures.append("snapshot_content_mismatch")

    return {
        "ok": not failures,
        "failure_slugs": sorted(set(failures)),
        "pending_keys": int(pending),
        "quarantined_keys": int(quarantined),
        "target_pending_rows": target_pending,
        "target_terminal_rows": target_terminal,
        "target_requeue_rows": target_requeue,
        "content_mismatches": mismatch_count,
        "unexpected_ciphertext_rows": ciphertext_count,
        "extended_content_mismatches": extended_mismatches,
        "decrypt_failures": decrypt_failures,
        "snapshot_content_mismatches": snapshot_mismatches,
    }


def status() -> dict:
    target = config.load_target()
    if target is None:
        return {
            "enabled": False,
            "schema_head": _SCHEMA_HEAD,
            "target_ok": False,
            "trigger_audit_ok": None,
            "pending_keys": 0,
            "quarantined_keys": 0,
            "oldest_pending_seconds": None,
            "latest_run": None,
            "latest_restore_evidence": None,
            "failure_slugs": [],
        }

    failures: list[str] = []
    pending = quarantined = 0
    oldest = None
    audit_ok = False
    evidence = None
    latest_run = None
    try:
        config.validate_startup()
        with db.get_pool().connection() as conn:
            pending, quarantined, oldest = conn.execute(
                "SELECT count(*), "
                "count(*) FILTER (WHERE quarantined_at IS NOT NULL), "
                "EXTRACT(epoch FROM now() - min(created_at)) "
                "FROM plaintext_shadow_dirty_keys"
            ).fetchone()
            audit_ok = change_capture.audit(conn).ok
            row = conn.execute(
                "SELECT restored_at, source_backup_at, expires_at, schema_head "
                "FROM plaintext_shadow_restore_evidence "
                "ORDER BY recorded_at DESC LIMIT 1"
            ).fetchone()
            if row:
                evidence = {
                    "restored_at": row[0].isoformat(),
                    "source_backup_at": row[1].isoformat(),
                    "expires_at": row[2].isoformat(),
                    "schema_head": row[3],
                    "fresh": _restore_evidence(conn),
                }
        runs = db.recent_plaintext_shadow_sync_runs(limit=1)
        latest_run = runs[0] if runs else None
    except Exception:  # noqa: BLE001 - status is fixed-shape and redacted
        failures.append("primary_status_unavailable")
    if not audit_ok:
        failures.append("trigger_drift")

    started = time.monotonic()
    try:
        with mirror.get_target_pool(target).connection() as conn:
            target_ok = conn.execute("SELECT 1").fetchone() == (1,)
    except Exception:  # noqa: BLE001
        target_ok = False
    target_probe_ms = round((time.monotonic() - started) * 1000, 2)
    if not target_ok:
        failures.append("target_unavailable")
    return {
        "enabled": True,
        "schema_head": _SCHEMA_HEAD,
        "target_ok": target_ok,
        "target_probe_ms": target_probe_ms,
        "trigger_audit_ok": audit_ok,
        "pending_keys": int(pending),
        "quarantined_keys": int(quarantined),
        "oldest_pending_seconds": None if oldest is None else float(oldest),
        "latest_run": latest_run,
        "latest_restore_evidence": evidence,
        "failure_slugs": sorted(set(failures)),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    evidence = commands.add_parser("record-restore-evidence")
    for name in (
        "restored-at", "source-backup-at", "schema-head", "verifier-digest",
        "backup-artifact-digest", "target-capacity-bytes", "operator-id",
        "expires-at",
    ):
        evidence.add_argument(f"--{name}", required=True)
    evidence.add_argument("--ha-verified", required=True, choices=("true", "false"))
    commands.add_parser("install-triggers")
    commands.add_parser("backfill")
    drain_cmd = commands.add_parser("drain")
    drain_cmd.add_argument("--limit", type=int, default=500)
    verify_cmd = commands.add_parser("verify")
    verify_cmd.add_argument("--require-green", action="store_true")
    commands.add_parser("status")
    commands.add_parser("remove-triggers")
    return parser


def _emit(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "preflight":
            payload = preflight()
        elif args.command == "record-restore-evidence":
            payload = record_restore_evidence(args)
        elif args.command == "install-triggers":
            payload = install_triggers()
        elif args.command == "backfill":
            payload = backfill()
        elif args.command == "drain":
            payload = drain(args.limit)
        elif args.command == "verify":
            payload = strict_report()
        elif args.command == "status":
            payload = status()
        else:
            payload = remove_triggers()
    except Exception:  # noqa: BLE001 - CLI output must never echo conninfo or content
        payload = {"ok": False, "failure_slugs": ["operator_command_failed"]}
        _emit(payload)
        return 2
    _emit(payload)
    if args.command == "verify" and args.require_green and not payload.get("ok"):
        return 2
    if args.command in {"preflight", "backfill", "install-triggers"} and not payload.get("ok"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
