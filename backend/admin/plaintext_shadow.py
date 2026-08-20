"""Operator gates for the TEE-primary decrypted plaintext shadow."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

import db
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from plaintext_shadow import change_capture, config, outbox
from psycopg import sql
from psycopg.types.json import Jsonb
from tee_replicator import transforms
from tee_replicator import worker
from tee_shadow import mirror, reconciler, snapshot, table_registry, verify


_SCHEMA_HEAD = "0027_plaintext_shadow_gates"
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


def _live_database_fingerprint(conn) -> str:
    row = conn.execute(
        "SELECT current_database(), oid, COALESCE(inet_server_addr()::text, 'local'), "
        "COALESCE(inet_server_port(), 0) FROM pg_database "
        "WHERE datname=current_database()"
    ).fetchone()
    if row is None:
        raise RuntimeError("live database identity unavailable")
    return _fingerprint(":".join(str(value) for value in row))


def _infra_public_key() -> tuple[Ed25519PublicKey, str]:
    raw_value = os.environ.get(
        "FEEDLING_PLAINTEXT_SHADOW_INFRA_EVIDENCE_PUBLIC_KEY", ""
    ).strip()
    try:
        raw = base64.b64decode(raw_value, validate=True)
        key = Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:
        raise RuntimeError("trusted infrastructure evidence key is invalid") from exc
    return key, _fingerprint(raw.hex())


def _verified_infra_attestation_bytes(
    payload_bytes: bytes, signature: bytes
) -> tuple[dict, str, str]:
    key, key_fingerprint = _infra_public_key()
    try:
        key.verify(signature, payload_bytes)
        payload = json.loads(payload_bytes)
    except (ValueError, InvalidSignature, json.JSONDecodeError) as exc:
        raise RuntimeError("trusted infrastructure evidence signature is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("trusted infrastructure evidence payload is invalid")
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if payload_bytes != canonical:
        raise RuntimeError("trusted infrastructure evidence payload is not canonical JSON")
    return payload, key_fingerprint, "sha256:" + hashlib.sha256(signature).hexdigest()


def _verified_infra_attestation(
    payload_b64: str, signature_b64: str
) -> tuple[dict, str, str]:
    try:
        payload_bytes = base64.b64decode(payload_b64, validate=True)
        signature = base64.b64decode(signature_b64, validate=True)
    except ValueError as exc:
        raise RuntimeError("trusted infrastructure evidence signature is invalid") from exc
    return _verified_infra_attestation_bytes(payload_bytes, signature)


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
    attestation_key_fingerprint: str | None = None,
) -> bool:
    rows = conn.execute(
        "SELECT restored_at, source_backup_at, schema_head, verifier_digest, "
        "backup_artifact_digest, target_fingerprint, target_capacity_bytes, "
        "target_connection_limit, ha_verified, attestation_key_fingerprint, "
        "attestation_signature_digest, expires_at, attestation_payload, "
        "attestation_signature FROM plaintext_shadow_restore_evidence"
    ).fetchall()
    now = datetime.now(timezone.utc)
    for row in rows:
        payload_bytes, signature = row[12], row[13]
        if payload_bytes is None or signature is None:
            continue
        try:
            payload, key_fingerprint, signature_digest = (
                _verified_infra_attestation_bytes(bytes(payload_bytes), bytes(signature))
            )
            claims = _validated_restore_claims(
                payload,
                target_fingerprint=target_fingerprint,
                target_connection_limit=target_connection_limit,
            )
        except RuntimeError:
            continue
        if attestation_key_fingerprint is not None and (
            key_fingerprint != attestation_key_fingerprint
        ):
            continue
        if claims["target_capacity_bytes"] < minimum_capacity_bytes:
            continue
        if claims["expires_at"] <= now:
            continue
        if claims["source_backup_at"] < now - timedelta(hours=24):
            continue
        stored = (
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7],
            row[8], row[9], row[10], row[11],
        )
        expected = (
            claims["restored_at"], claims["source_backup_at"],
            claims["schema_head"], claims["verifier_digest"],
            claims["backup_artifact_digest"], claims["target_fingerprint"],
            claims["target_capacity_bytes"], claims["target_connection_limit"],
            claims["ha_verified"], key_fingerprint, signature_digest,
            claims["expires_at"],
        )
        if stored == expected:
            return True
    return False


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
        except config.DatabaseAliasError:
            failures.append("primary_shadow_alias")
        except config.LiveTopologyCheckError:
            failures.append("live_topology_check_failed")
        primary_live_fingerprint = _live_database_fingerprint(primary)
        target_live_fingerprint = _live_database_fingerprint(target)
        result["primary_live_fingerprint"] = primary_live_fingerprint
        result["target_live_fingerprint"] = target_live_fingerprint
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

        evidence_direct_dml = bool(
            primary.execute(
                "SELECT has_table_privilege(current_user, "
                "'plaintext_shadow_restore_evidence', "
                "'INSERT,UPDATE,DELETE,TRUNCATE')"
            ).fetchone()[0]
        )
        evidence_recorder_available = bool(
            primary.execute(
                "SELECT has_function_privilege(current_user, "
                "'feedling_record_plaintext_shadow_restore_evidence("
                "bytea,bytea,text,text,text)', 'EXECUTE')"
            ).fetchone()[0]
        )
        result["restore_evidence_direct_dml_blocked"] = not evidence_direct_dml
        result["restore_evidence_recorder_available"] = evidence_recorder_available
        if evidence_direct_dml:
            failures.append("restore_evidence_direct_dml_allowed")
        if not evidence_recorder_available:
            failures.append("restore_evidence_recorder_unavailable")

        trigger_report = change_capture.audit(primary)
        result["trigger_audit_ok"] = trigger_report.ok
        if not trigger_report.ok:
            failures.append("trigger_drift")
        try:
            _key, attestation_key_fingerprint = _infra_public_key()
        except RuntimeError:
            attestation_key_fingerprint = None
            failures.append("trusted_infrastructure_key_missing")
        evidence_ok = attestation_key_fingerprint is not None and _restore_evidence(
            primary,
            target_fingerprint=target_live_fingerprint,
            minimum_capacity_bytes=max(primary_bytes * 2, target_bytes),
            target_connection_limit=max_connections,
            attestation_key_fingerprint=attestation_key_fingerprint,
        )
        result["restore_evidence_ok"] = evidence_ok
        if not evidence_ok:
            failures.append("restore_evidence_missing")

    result["failure_slugs"] = sorted(set(failures))
    result["ok"] = not result["failure_slugs"]
    return result


def _positive_int_claim(payload: dict, name: str) -> int:
    value = payload.get(name)
    if type(value) is not int or value <= 0:
        raise RuntimeError(f"trusted infrastructure evidence {name} is invalid")
    return value


_RESTORE_CLAIM_KEYS = {
    "restored_at",
    "source_backup_at",
    "schema_head",
    "verifier_digest",
    "backup_artifact_digest",
    "target_fingerprint",
    "target_capacity_bytes",
    "target_connection_limit",
    "ha_verified",
    "expires_at",
}


def _validated_restore_claims(
    payload: dict,
    *,
    target_fingerprint: str | None,
    target_connection_limit: int,
) -> dict:
    if set(payload) != _RESTORE_CLAIM_KEYS:
        raise RuntimeError("trusted infrastructure evidence payload keys are invalid")
    if payload["schema_head"] != _SCHEMA_HEAD:
        raise RuntimeError("trusted infrastructure evidence schema head is stale")
    if target_fingerprint is not None and (
        payload["target_fingerprint"] != target_fingerprint
    ):
        raise RuntimeError("trusted infrastructure evidence target identity is stale")
    target_capacity_bytes = _positive_int_claim(payload, "target_capacity_bytes")
    attested_connection_limit = _positive_int_claim(
        payload, "target_connection_limit"
    )
    if target_connection_limit and (
        attested_connection_limit != target_connection_limit
    ):
        raise RuntimeError("trusted infrastructure evidence connection limit is stale")
    if payload["ha_verified"] is not True:
        raise RuntimeError("trusted infrastructure evidence does not verify HA")
    for digest_name in ("verifier_digest", "backup_artifact_digest"):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload[digest_name])):
            raise RuntimeError("trusted infrastructure evidence digest is invalid")
    restored_at = _parse_time(payload["restored_at"])
    source_backup_at = _parse_time(payload["source_backup_at"])
    expires_at = _parse_time(payload["expires_at"])
    if not source_backup_at <= restored_at < expires_at:
        raise RuntimeError("trusted infrastructure evidence time order is invalid")
    return {
        **payload,
        "restored_at": restored_at,
        "source_backup_at": source_backup_at,
        "expires_at": expires_at,
        "target_capacity_bytes": target_capacity_bytes,
        "target_connection_limit": attested_connection_limit,
    }


def record_restore_evidence(args) -> dict:
    policy = config.require_target()
    try:
        payload_bytes = base64.b64decode(args.attestation_payload_b64, validate=True)
        signature = base64.b64decode(args.attestation_signature_b64, validate=True)
    except ValueError as exc:
        raise RuntimeError("trusted infrastructure evidence signature is invalid") from exc
    payload, key_fingerprint, signature_digest = _verified_infra_attestation_bytes(
        payload_bytes, signature
    )
    with db.get_pool().connection() as primary, mirror.get_target_pool(
        policy
    ).connection() as target:
        config.validate_live_topology(primary, target)
        target_connection_limit = int(
            target.execute("SHOW max_connections").fetchone()[0]
        )
        target_fingerprint = _live_database_fingerprint(target)
    _validated_restore_claims(
        payload,
        target_fingerprint=target_fingerprint,
        target_connection_limit=target_connection_limit,
    )
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT feedling_record_plaintext_shadow_restore_evidence("
            "%s,%s,%s,%s,%s)",
            (
                payload_bytes, signature, key_fingerprint, signature_digest,
                args.operator_id,
            ),
        ).fetchone()
    return {"ok": True, "evidence_id": int(row[0])}


def _parse_time(raw: str) -> datetime:
    try:
        if not isinstance(raw, str):
            raise TypeError("timestamp must be a string")
        value = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return parsed
    except (TypeError, ValueError) as exc:
        raise RuntimeError("trusted infrastructure evidence timestamp is invalid") from exc


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
                if table == "identity":
                    # The INSERT column list includes ``kind``, but its VALUES
                    # clause uses the SQL literal 'identity' rather than a
                    # placeholder returned by upsert_args.
                    expected = (expected[0], "identity", expected[1])
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
                    # This is only the time/HA portion. The authoritative
                    # endpoint/capacity/connection binding is reported by
                    # preflight and must not be implied by status.
                    "time_fresh": _restore_evidence(conn),
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
    for name in ("attestation-payload-b64", "attestation-signature-b64", "operator-id"):
        evidence.add_argument(f"--{name}", required=True)
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
