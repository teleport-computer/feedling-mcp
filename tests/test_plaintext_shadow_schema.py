from __future__ import annotations

import builtins
import os
from pathlib import Path

import psycopg
import pytest

import alembic_tee
from tee_shadow import table_registry
from admin import plaintext_shadow


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
    assert rows == [(alembic_tee.current_head(),)]


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
        "backup_artifact_digest",
        "target_fingerprint",
        "target_capacity_bytes",
        "target_connection_limit",
        "ha_verified",
        "attestation_key_fingerprint",
        "attestation_signature_digest",
        "attestation_payload",
        "attestation_signature",
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


def test_restore_evidence_writer_is_security_definer_and_not_public() -> None:
    rows = _fetchall(
        os.environ["TEE_DATABASE_URL"],
        "SELECT prosecdef, EXISTS ("
        "SELECT 1 FROM aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) "
        "WHERE grantee=0 AND privilege_type='EXECUTE') FROM pg_proc AS p "
        "JOIN pg_namespace AS n ON n.oid=p.pronamespace "
        "WHERE n.nspname='public' "
        "AND p.proname='feedling_record_plaintext_shadow_restore_evidence'",
    )
    assert len(rows) == 1
    security_definer, public_can_execute = rows[0]
    assert security_definer is True
    assert public_can_execute is False


def test_restore_evidence_migration_revokes_direct_writer_roles() -> None:
    migration = (
        Path(__file__).parents[1]
        / "backend/alembic_tee/versions/0027_plaintext_shadow_gates.py"
    ).read_text()
    assert "ARRAY['app', 'tee_replicator']" in migration
    assert "REVOKE INSERT, UPDATE, DELETE, TRUNCATE" in migration


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

    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
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


def test_tee_primary_migration_uses_app_database_url(monkeypatch) -> None:
    from alembic_tee.connection import migration_database_url

    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setenv("DATABASE_URL", "postgresql://app/primary")
    monkeypatch.setenv("TEE_MIGRATION_DATABASE_URL", "postgresql://owner/legacy")

    assert migration_database_url() == "postgresql+psycopg://app/primary"


def test_keyword_dsn_is_converted_to_sqlalchemy_url(monkeypatch) -> None:
    """Prod supplies libpq keyword form; boot migration must accept it.

    Regression guard for the 2026-09-02 outage: create_engine() got the
    raw keyword string and crash-looped backend + serve-worker.
    """
    from alembic_tee.connection import migration_database_url

    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
    for var in (
        "PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL",
        "TEE_MIGRATION_DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(
        "TEE_DATABASE_URL",
        "user=app password='p@ss w:rd' dbname=feedling "
        "host=example.dstack.host port=443 sslmode=verify-full "
        "sslnegotiation=direct sslrootcert=/certs/ca.crt",
    )
    url = migration_database_url()
    assert url == (
        "postgresql+psycopg://app:p%40ss%20w%3Ard@example.dstack.host:443/"
        "feedling?sslmode=verify-full&sslnegotiation=direct"
        "&sslrootcert=%2Fcerts%2Fca.crt"
    )
    # And SQLAlchemy itself must accept the result — that is the property
    # the outage violated, so assert it directly, not just the string shape.
    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    assert parsed.password == "p@ss w:rd"
    assert parsed.host == "example.dstack.host"


def test_url_form_dsns_pass_through_unchanged_by_keyword_branch(monkeypatch) -> None:
    from alembic_tee.connection import migration_database_url

    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
    for var in (
        "PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL",
        "TEE_MIGRATION_DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(
        "TEE_DATABASE_URL",
        "postgresql://u:p@h:5432/d?sslmode=require",
    )
    assert migration_database_url() == (
        "postgresql+psycopg://u:p@h:5432/d?sslmode=require"
    )
    monkeypatch.setenv(
        "TEE_DATABASE_URL", "postgresql+psycopg://u:p@h/d"
    )
    assert migration_database_url() == "postgresql+psycopg://u:p@h/d"


def test_unparseable_keyword_dsn_fails_closed_with_named_error(monkeypatch) -> None:
    from alembic_tee.connection import migration_database_url

    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
    for var in (
        "PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL",
        "TEE_MIGRATION_DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TEE_DATABASE_URL", "host=h port=not a pair =broken")
    with pytest.raises(RuntimeError, match="keyword form"):
        migration_database_url()


def test_non_dsn_text_fails_closed_with_named_error() -> None:
    from alembic_tee.connection import (
        InvalidPostgresDSNError,
        _normalize_postgres_url,
    )

    with pytest.raises(
        InvalidPostgresDSNError,
        match="PostgreSQL URL or libpq keyword/value DSN",
    ):
        _normalize_postgres_url("not-a-dsn")


def test_empty_dsn_fails_closed_with_named_error() -> None:
    from alembic_tee.connection import (
        InvalidPostgresDSNError,
        _normalize_postgres_url,
    )

    with pytest.raises(InvalidPostgresDSNError, match="must not be empty"):
        _normalize_postgres_url("")


def test_missing_conninfo_parser_has_clear_named_error(monkeypatch) -> None:
    from alembic_tee.connection import (
        InvalidPostgresDSNError,
        _keyword_dsn_to_url,
    )

    real_import = builtins.__import__

    def reject_conninfo_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "psycopg.conninfo":
            raise ImportError("simulated missing conninfo parser")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_conninfo_import)
    with pytest.raises(
        InvalidPostgresDSNError,
        match="requires psycopg's conninfo parser",
    ):
        _keyword_dsn_to_url("host=db.example dbname=feedling")


@pytest.mark.parametrize(
    ("reserved", "encoded"),
    [
        ("@", "%40"),
        (":", "%3A"),
        ("/", "%2F"),
        ("?", "%3F"),
        ("#", "%23"),
        ("&", "%26"),
    ],
)
def test_keyword_dsn_password_preserves_reserved_characters(
    reserved: str,
    encoded: str,
) -> None:
    from alembic_tee.connection import _keyword_dsn_to_url
    from sqlalchemy.engine.url import make_url

    password = f"before{reserved}after"
    url = _keyword_dsn_to_url(
        f"user=app password='{password}' host=db.example dbname=feedling"
    )

    assert encoded in url
    assert make_url(url).password == password


def test_keyword_dsn_with_url_like_password_still_converts(monkeypatch) -> None:
    """'://' inside a quoted value must not suppress conversion (QA finding)."""
    from alembic_tee.connection import migration_database_url
    from sqlalchemy.engine.url import make_url

    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
    for var in (
        "PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL",
        "TEE_MIGRATION_DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(
        "TEE_DATABASE_URL",
        "user=app password='p://x' host=db.example dbname=feedling",
    )
    url = migration_database_url()
    parsed = make_url(url)
    assert parsed.password == "p://x"
    assert parsed.host == "db.example"
    assert parsed.database == "feedling"


@pytest.mark.parametrize(
    "dsn",
    [
        "host=/var/run/postgresql dbname=feedling",
        "postgresql:///feedling?host=/var/run/postgresql",
    ],
)
def test_unix_socket_host_is_explicitly_refused(monkeypatch, dsn: str) -> None:
    """No configured migration DSN uses sockets, so refuse rather than guess."""
    from alembic_tee.connection import (
        InvalidPostgresDSNError,
        migration_database_url,
    )

    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
    for var in (
        "PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL",
        "TEE_MIGRATION_DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("TEE_DATABASE_URL", dsn)
    with pytest.raises(
        InvalidPostgresDSNError,
        match="Unix-socket hosts are not supported",
    ):
        migration_database_url()


def test_keyword_dsn_password_without_user_is_refused(monkeypatch) -> None:
    from alembic_tee.connection import migration_database_url

    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
    for var in (
        "PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL",
        "TEE_MIGRATION_DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(
        "TEE_DATABASE_URL", "password=secret host=h dbname=d"
    )
    with pytest.raises(RuntimeError, match="losslessly"):
        migration_database_url()
