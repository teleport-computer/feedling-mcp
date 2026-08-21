"""Owner-only retirement of the legacy per-user trace-event blob.

T184 deliberately did not migrate or union-read the old ring.  Once the
single-write deployment has passed its rollback window, this tool removes only
``user_blobs.kind='v1_flow_trace'`` from the TEE primary.  The independent
``v1_flow_trace_enabled`` flag is not touched.

The CLI is dry-run by default.  Execution requires an exact environment-bound
confirmation and an explicit assertion that the old-writer rollback window is
closed.  Production additionally requires the recorded Seven approval
reference; having the tool available is not itself approval to run it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from typing import Any

import psycopg


LEGACY_TRACE_BLOB_KIND = "v1_flow_trace"
_CONFIRMATIONS = {
    "test": "DELETE LEGACY TRACE BLOBS FROM TEST",
    "prod": "DELETE LEGACY TRACE BLOBS FROM PROD",
}


def _database_fingerprint(conn: psycopg.Connection) -> str:
    row = conn.execute(
        "SELECT current_database(),COALESCE(inet_server_addr()::text,'local'),"
        "COALESCE(inet_server_port(),0)"
    ).fetchone()
    identity = ":".join(str(value) for value in row)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _inventory(conn: psycopg.Connection) -> dict[str, int]:
    row = conn.execute(
        "SELECT count(*),count(DISTINCT user_id),"
        "COALESCE(sum(pg_column_size(doc)),0) "
        "FROM user_blobs WHERE kind=%s",
        (LEGACY_TRACE_BLOB_KIND,),
    ).fetchone()
    return {
        "rows": int(row[0] or 0),
        "users": int(row[1] or 0),
        "document_bytes": int(row[2] or 0),
    }


def retire_legacy_trace_blobs(
    conn: psycopg.Connection,
    *,
    environment: str,
    execute: bool = False,
    rollback_window_closed: bool = False,
    validated_merge_sha: str = "",
    multi_tenant_ci_success: bool = False,
    confirmation: str = "",
    expected_database_fingerprint: str = "",
    expected_rows: int | None = None,
    expected_document_bytes: int | None = None,
    seven_approval_reference: str = "",
) -> dict[str, Any]:
    """Inventory or delete the one legacy event-blob kind transactionally."""
    target = str(environment or "").strip().lower()
    if target not in _CONFIRMATIONS:
        raise ValueError("environment must be test or prod")

    before = _inventory(conn)
    database_fingerprint = _database_fingerprint(conn)
    report: dict[str, Any] = {
        "environment": target,
        "database_fingerprint": database_fingerprint,
        "mode": "execute" if execute else "dry-run",
        "kind": LEGACY_TRACE_BLOB_KIND,
        "before": before,
        "deleted": {"rows": 0, "users": 0, "document_bytes": 0},
        "after": before,
    }
    if not execute:
        return report
    if not rollback_window_closed:
        raise RuntimeError("legacy writer rollback window is not confirmed closed")
    merge_sha = str(validated_merge_sha or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", merge_sha) is None:
        raise RuntimeError("validated merge SHA must be exactly 40 hex characters")
    if not multi_tenant_ci_success:
        raise RuntimeError("multi-tenant CI success is not confirmed")
    if confirmation != _CONFIRMATIONS[target]:
        raise RuntimeError(f"confirmation must equal {_CONFIRMATIONS[target]!r}")
    if expected_database_fingerprint != database_fingerprint:
        raise RuntimeError(
            "expected database fingerprint does not match the dry-run target"
        )
    if (
        expected_rows != before["rows"]
        or expected_document_bytes != before["document_bytes"]
    ):
        raise RuntimeError("legacy trace blob inventory changed since dry-run")
    approval = str(seven_approval_reference or "").strip()
    if target == "prod" and not approval:
        raise RuntimeError("production deletion requires a Seven approval reference")

    with conn.transaction():
        deleted = conn.execute(
            "WITH gone AS ("
            " DELETE FROM user_blobs WHERE kind=%s RETURNING user_id,doc"
            ") SELECT count(*),count(DISTINCT user_id),"
            "COALESCE(sum(pg_column_size(doc)),0) FROM gone",
            (LEGACY_TRACE_BLOB_KIND,),
        ).fetchone()
        after = _inventory(conn)
        if after["rows"] != 0:
            raise RuntimeError("legacy trace blob cleanup did not converge to zero")

    report["deleted"] = {
        "rows": int(deleted[0] or 0),
        "users": int(deleted[1] or 0),
        "document_bytes": int(deleted[2] or 0),
    }
    report["after"] = after
    report["validated_merge_sha"] = merge_sha
    report["validated_ci_job"] = "test_api.py (multi-tenant)"
    if target == "prod":
        report["seven_approval_reference"] = approval
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", choices=tuple(_CONFIRMATIONS), required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--rollback-window-closed", action="store_true")
    parser.add_argument("--validated-merge-sha", default="")
    parser.add_argument("--multi-tenant-ci-success", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--expected-database-fingerprint", default="")
    parser.add_argument("--expected-rows", type=int)
    parser.add_argument("--expected-document-bytes", type=int)
    parser.add_argument("--seven-approval-reference", default="")
    args = parser.parse_args()

    dsn = os.environ.get("TEE_MIGRATION_DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("TEE_MIGRATION_DATABASE_URL is required")
    with psycopg.connect(dsn) as conn:
        report = retire_legacy_trace_blobs(
            conn,
            environment=args.environment,
            execute=args.execute,
            rollback_window_closed=args.rollback_window_closed,
            validated_merge_sha=args.validated_merge_sha,
            multi_tenant_ci_success=args.multi_tenant_ci_success,
            confirmation=args.confirm,
            expected_database_fingerprint=args.expected_database_fingerprint,
            expected_rows=args.expected_rows,
            expected_document_bytes=args.expected_document_bytes,
            seven_approval_reference=args.seven_approval_reference,
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
