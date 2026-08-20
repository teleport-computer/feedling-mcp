"""T184/C: irreversible legacy trace cleanup stays scoped and guarded."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import trace_events_cleanup as cleanup  # noqa: E402


def _uid() -> str:
    return "usr_t184_cleanup_" + uuid.uuid4().hex[:12]


def _seed(conn: psycopg.Connection, uid: str) -> None:
    conn.execute(
        "INSERT INTO users(user_id,created_at,doc) VALUES (%s,now(),%s)",
        (uid, Jsonb({})),
    )
    conn.execute(
        "INSERT INTO user_blobs(user_id,kind,doc) VALUES "
        "(%s,%s,%s),(%s,%s,%s),(%s,%s,%s)",
        (
            uid,
            cleanup.LEGACY_TRACE_BLOB_KIND,
            Jsonb({"events": [{"type": "legacy"}]}),
            uid,
            "v1_flow_trace_enabled",
            Jsonb({"enabled": False}),
            uid,
            "unrelated_test_blob",
            Jsonb({"keep": True}),
        ),
    )


def _delete_seed(conn: psycopg.Connection, uid: str) -> None:
    conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_dry_run_inventories_without_deleting(tee_primary):
    import db

    uid = _uid()
    with db.get_pool().connection() as conn:
        _seed(conn, uid)
        try:
            report = cleanup.retire_legacy_trace_blobs(
                conn,
                environment="test",
            )
            assert report["mode"] == "dry-run"
            assert len(report["database_fingerprint"]) == 16
            assert report["before"]["rows"] >= 1
            assert conn.execute(
                "SELECT count(*) FROM user_blobs WHERE user_id=%s AND kind=%s",
                (uid, cleanup.LEGACY_TRACE_BLOB_KIND),
            ).fetchone() == (1,)
        finally:
            _delete_seed(conn, uid)


def test_execute_deletes_only_legacy_event_blob(tee_primary):
    import db

    uid = _uid()
    with db.get_pool().connection() as conn:
        _seed(conn, uid)
        try:
            preview = cleanup.retire_legacy_trace_blobs(
                conn,
                environment="test",
            )
            report = cleanup.retire_legacy_trace_blobs(
                conn,
                environment="test",
                execute=True,
                rollback_window_closed=True,
                validated_merge_sha="a" * 40,
                multi_tenant_ci_success=True,
                confirmation="DELETE LEGACY TRACE BLOBS FROM TEST",
                expected_database_fingerprint=preview["database_fingerprint"],
                expected_rows=preview["before"]["rows"],
                expected_document_bytes=preview["before"]["document_bytes"],
            )
            assert report["deleted"]["rows"] >= 1
            assert report["after"]["rows"] == 0
            assert report["validated_merge_sha"] == "a" * 40
            assert report["validated_ci_job"] == "test_api.py (multi-tenant)"
            assert conn.execute(
                "SELECT kind FROM user_blobs WHERE user_id=%s ORDER BY kind",
                (uid,),
            ).fetchall() == [
                ("unrelated_test_blob",),
                ("v1_flow_trace_enabled",),
            ]
        finally:
            _delete_seed(conn, uid)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {
                "environment": "test",
                "execute": True,
                "confirmation": "DELETE LEGACY TRACE BLOBS FROM TEST",
            },
            "rollback window",
        ),
        (
            {
                "environment": "test",
                "execute": True,
                "rollback_window_closed": True,
                "validated_merge_sha": "short",
                "multi_tenant_ci_success": True,
                "confirmation": "DELETE LEGACY TRACE BLOBS FROM TEST",
            },
            "merge SHA",
        ),
        (
            {
                "environment": "test",
                "execute": True,
                "rollback_window_closed": True,
                "validated_merge_sha": "a" * 40,
                "multi_tenant_ci_success": False,
                "confirmation": "DELETE LEGACY TRACE BLOBS FROM TEST",
            },
            "multi-tenant CI success",
        ),
        (
            {
                "environment": "test",
                "execute": True,
                "rollback_window_closed": True,
                "confirmation": "wrong target",
            },
            "confirmation must equal",
        ),
        (
            {
                "environment": "prod",
                "execute": True,
                "rollback_window_closed": True,
                "confirmation": "DELETE LEGACY TRACE BLOBS FROM PROD",
            },
            "Seven approval reference",
        ),
        (
            {
                "environment": "test",
                "execute": True,
                "rollback_window_closed": True,
                "confirmation": "DELETE LEGACY TRACE BLOBS FROM TEST",
                "expected_database_fingerprint": "wrong-target",
            },
            "fingerprint does not match",
        ),
        (
            {
                "environment": "test",
                "execute": True,
                "rollback_window_closed": True,
                "confirmation": "DELETE LEGACY TRACE BLOBS FROM TEST",
                "expected_rows": -1,
            },
            "inventory changed",
        ),
    ],
)
def test_execute_guards_fail_before_delete(tee_primary, kwargs, message):
    import db

    uid = _uid()
    with db.get_pool().connection() as conn:
        _seed(conn, uid)
        try:
            preview = cleanup.retire_legacy_trace_blobs(
                conn,
                environment=kwargs["environment"],
            )
            guarded_kwargs = {
                "validated_merge_sha": "a" * 40,
                "multi_tenant_ci_success": True,
                **kwargs,
            }
            if "expected_database_fingerprint" not in guarded_kwargs:
                guarded_kwargs = {
                    **guarded_kwargs,
                    "expected_database_fingerprint": preview[
                        "database_fingerprint"
                    ],
                }
            guarded_kwargs.setdefault("expected_rows", preview["before"]["rows"])
            guarded_kwargs.setdefault(
                "expected_document_bytes",
                preview["before"]["document_bytes"],
            )
            with pytest.raises(RuntimeError, match=message):
                cleanup.retire_legacy_trace_blobs(conn, **guarded_kwargs)
            assert conn.execute(
                "SELECT count(*) FROM user_blobs WHERE user_id=%s AND kind=%s",
                (uid, cleanup.LEGACY_TRACE_BLOB_KIND),
            ).fetchone() == (1,)
        finally:
            _delete_seed(conn, uid)
