from __future__ import annotations

import os

import psycopg
import pytest

from tee_shadow import table_registry


def _fetchall(url: str, sql: str, params: tuple | None = None) -> list[tuple]:
    with psycopg.connect(url) as conn:
        cursor = conn.execute(sql) if params is None else conn.execute(sql, params)
        return cursor.fetchall()


def _columns(table: str) -> set[str]:
    rows = _fetchall(
        os.environ["TEE_DATABASE_URL"],
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    return {row[0] for row in rows}


def _primary_keys(url: str) -> dict[str, tuple[str, ...]]:
    rows = _fetchall(
        url,
        """
        SELECT cls.relname,
               ARRAY(
                   SELECT att.attname
                   FROM unnest(idx.indkey) WITH ORDINALITY AS key(attnum, ord)
                   JOIN pg_attribute AS att
                     ON att.attrelid = idx.indrelid AND att.attnum = key.attnum
                   WHERE key.ord <= idx.indnkeyatts
                   ORDER BY key.ord
               )
        FROM pg_index AS idx
        JOIN pg_class AS cls ON cls.oid = idx.indrelid
        JOIN pg_namespace AS ns ON ns.oid = cls.relnamespace
        WHERE ns.nspname='public' AND idx.indisprimary
        ORDER BY cls.relname
        """,
    )
    return {table: tuple(columns) for table, columns in rows}


def test_plaintext_shadow_control_revision_is_head() -> None:
    rows = _fetchall(
        os.environ["TEE_DATABASE_URL"],
        "SELECT version_num FROM alembic_tee_version",
    )
    assert rows == [("0026_plaintext_shadow_control",)]


def test_dirty_key_control_plane_contains_no_content_columns() -> None:
    assert _columns("plaintext_shadow_dirty_keys") == {
        "table_name",
        "key_json",
        "operation",
        "generation",
        "created_at",
        "attempts",
        "next_attempt_at",
        "last_error_slug",
        "quarantined_at",
    }


def test_sync_and_restore_evidence_are_scalar_only() -> None:
    assert _columns("plaintext_shadow_sync_runs") == {
        "id",
        "ran_at",
        "duration_ms",
        "applied",
        "deleted",
        "retried",
        "quarantined",
        "pending",
        "oldest_pending_seconds",
        "target_ok",
        "target_probe_ms",
        "verify_ok",
        "table_metrics",
    }
    assert _columns("plaintext_shadow_restore_evidence") == {
        "id",
        "restored_at",
        "source_backup_at",
        "schema_head",
        "verifier_digest",
        "operator_id",
        "expires_at",
        "recorded_at",
    }


def test_capture_function_exists_but_migration_installs_no_table_triggers() -> None:
    function_rows = _fetchall(
        os.environ["TEE_DATABASE_URL"],
        "SELECT count(*) FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid=p.pronamespace "
        "WHERE n.nspname='public' AND p.proname='feedling_capture_plaintext_shadow_change'",
    )
    trigger_rows = _fetchall(
        os.environ["TEE_DATABASE_URL"],
        "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
        "AND tgname LIKE 'plaintext_shadow_capture_%'",
    )
    assert function_rows == [(1,)]
    assert trigger_rows == [(0,)]


def test_every_synced_table_declares_its_real_primary_key() -> None:
    actual = _primary_keys(os.environ["DATABASE_URL"])
    mismatches = {
        table: {"declared": getattr(entry, "key_columns", ()), "actual": actual.get(table)}
        for table, entry in table_registry.REGISTRY.items()
        if entry.lane not in (table_registry.SKIP, table_registry.LOGICAL)
        and getattr(entry, "key_columns", ()) != actual.get(table)
    }
    assert mismatches == {}


def test_frame_envelopes_declares_its_different_destination_table() -> None:
    assert table_registry.REGISTRY["frame_envelopes"].destination_table == "frames"
    assert table_registry.REGISTRY["chat_messages"].destination_table == "chat_messages"


def test_plaintext_migration_dsn_takes_priority_without_database_url_fallback(
    monkeypatch,
) -> None:
    from alembic_tee.connection import migration_database_url

    monkeypatch.setenv("DATABASE_URL", "postgresql://wrong/primary")
    monkeypatch.setenv("TEE_DATABASE_URL", "postgresql://legacy/target")
    monkeypatch.setenv("TEE_MIGRATION_DATABASE_URL", "postgresql://legacy/owner")
    monkeypatch.setenv(
        "PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL",
        "postgresql://plaintext/owner",
    )
    assert migration_database_url() == "postgresql+psycopg://plaintext/owner"

    monkeypatch.delenv("PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL")
    monkeypatch.delenv("TEE_MIGRATION_DATABASE_URL")
    monkeypatch.delenv("TEE_DATABASE_URL")
    with pytest.raises(RuntimeError, match="migration database URL is not set"):
        migration_database_url()
