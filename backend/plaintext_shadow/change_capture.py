"""Install and audit content-free dirty-key triggers on the TEE primary."""
from __future__ import annotations

from dataclasses import dataclass

from psycopg import sql

from tee_shadow import table_registry


_TRIGGER_PREFIX = "plaintext_shadow_capture_"


@dataclass(frozen=True)
class InstallReport:
    installed: tuple[str, ...]
    missing_tables: tuple[str, ...]


@dataclass(frozen=True)
class AuditReport:
    ok: bool
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]
    disabled: tuple[str, ...]
    mismatched: tuple[str, ...]


def _entries():
    return {
        name: entry
        for name, entry in table_registry.REGISTRY.items()
        if entry.lane not in (table_registry.SKIP, table_registry.LOGICAL)
    }


def _trigger_name(table: str) -> str:
    name = f"{_TRIGGER_PREFIX}{table}"
    if len(name.encode()) > 63:
        raise RuntimeError(f"plaintext shadow trigger name is too long for table {table}")
    return name


def _primary_key(conn, table: str) -> tuple[str, ...] | None:
    row = conn.execute(
        """
        SELECT ARRAY(
            SELECT att.attname
            FROM unnest(idx.indkey) WITH ORDINALITY AS key(attnum, ord)
            JOIN pg_attribute AS att
              ON att.attrelid=idx.indrelid AND att.attnum=key.attnum
            WHERE key.ord <= idx.indnkeyatts
            ORDER BY key.ord
        )
        FROM pg_index AS idx
        JOIN pg_class AS cls ON cls.oid=idx.indrelid
        JOIN pg_namespace AS ns ON ns.oid=cls.relnamespace
        WHERE ns.nspname='public' AND cls.relname=%s AND idx.indisprimary
        """,
        (table,),
    ).fetchone()
    return None if row is None else tuple(row[0])


def _control_plane_ready(conn) -> bool:
    row = conn.execute(
        "SELECT to_regclass('public.plaintext_shadow_dirty_keys') IS NOT NULL, "
        "EXISTS (SELECT 1 FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid=p.pronamespace "
        "WHERE n.nspname='public' AND p.proname='feedling_capture_plaintext_shadow_change')"
    ).fetchone()
    return bool(row and row[0] and row[1])


def install(conn) -> InstallReport:
    """Install the exact registry trigger set, or make no changes on drift."""
    entries = _entries()
    if not _control_plane_ready(conn):
        raise RuntimeError("plaintext shadow control-plane migration is not installed")

    missing = tuple(
        sorted(
            table
            for table, entry in entries.items()
            if _primary_key(conn, table) != entry.key_columns
        )
    )
    if missing:
        return InstallReport(installed=(), missing_tables=missing)

    with conn.transaction():
        for table, entry in sorted(entries.items()):
            trigger = _trigger_name(table)
            conn.execute(
                sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(
                    sql.Identifier(trigger), sql.Identifier(table)
                )
            )
            args = sql.SQL(", ").join(
                sql.Literal(column) for column in (entry.capture_key_columns or ())
            )
            conn.execute(
                sql.SQL(
                    "CREATE TRIGGER {} AFTER INSERT OR UPDATE OR DELETE ON {} "
                    "FOR EACH ROW EXECUTE FUNCTION "
                    "feedling_capture_plaintext_shadow_change({})"
                ).format(
                    sql.Identifier(trigger),
                    sql.Identifier(table),
                    args,
                )
            )
    return InstallReport(installed=tuple(sorted(entries)), missing_tables=())


def _decode_trigger_args(value) -> tuple[str, ...]:
    raw = bytes(value or b"")
    return tuple(part.decode("utf-8") for part in raw.split(b"\0") if part)


def audit(conn) -> AuditReport:
    """Compare the complete live trigger semantics with the registry."""
    entries = _entries()
    rows = conn.execute(
        "SELECT cls.relname, trg.tgname, trg.tgenabled, trg.tgargs, trg.tgtype, "
        "proc.proname, proc_ns.nspname "
        "FROM pg_trigger AS trg "
        "JOIN pg_class AS cls ON cls.oid=trg.tgrelid "
        "JOIN pg_namespace AS ns ON ns.oid=cls.relnamespace "
        "JOIN pg_proc AS proc ON proc.oid=trg.tgfoid "
        "JOIN pg_namespace AS proc_ns ON proc_ns.oid=proc.pronamespace "
        "WHERE NOT trg.tgisinternal AND ns.nspname='public' "
        "AND trg.tgname LIKE 'plaintext_shadow_capture_%%'"
    ).fetchall()
    by_table: dict[str, list[tuple]] = {}
    for row in rows:
        by_table.setdefault(row[0], []).append(row[1:])

    missing = tuple(
        sorted(
            table
            for table in entries
            if not any(row[0] == _trigger_name(table) for row in by_table.get(table, ()))
        )
    )
    unexpected = tuple(
        sorted(
            row[1]
            for row in rows
            if row[0] not in entries or row[1] != _trigger_name(row[0])
        )
    )
    disabled = tuple(
        sorted(
            table
            for table in entries
            for name, enabled, *_rest in by_table.get(table, ())
            if name == _trigger_name(table) and enabled != "O"
        )
    )
    mismatched = tuple(
        sorted(
            table
            for table, entry in entries.items()
            if table in by_table
            and (
                len(by_table[table]) != 1
                or by_table[table][0][0] != _trigger_name(table)
                or _decode_trigger_args(by_table[table][0][2])
                != (entry.capture_key_columns or ())
                # PostgreSQL tgtype bits: ROW=1, INSERT=4, DELETE=8,
                # UPDATE=16. Exact AFTER ROW INSERT|UPDATE|DELETE is 29.
                or by_table[table][0][3] != 29
                or by_table[table][0][4]
                != "feedling_capture_plaintext_shadow_change"
                or by_table[table][0][5] != "public"
                or _primary_key(conn, table) != entry.key_columns
            )
        )
    )
    return AuditReport(
        ok=not (missing or unexpected or disabled or mismatched),
        missing=missing,
        unexpected=unexpected,
        disabled=disabled,
        mismatched=mismatched,
    )


def remove(conn) -> None:
    """Remove every managed or stray prefixed capture trigger."""
    rows = conn.execute(
        "SELECT cls.relname, trg.tgname FROM pg_trigger AS trg "
        "JOIN pg_class AS cls ON cls.oid=trg.tgrelid "
        "JOIN pg_namespace AS ns ON ns.oid=cls.relnamespace "
        "WHERE NOT trg.tgisinternal AND ns.nspname='public' "
        "AND trg.tgname LIKE 'plaintext_shadow_capture_%%'"
    ).fetchall()
    with conn.transaction():
        for table, trigger in rows:
            conn.execute(
                sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(
                    sql.Identifier(trigger), sql.Identifier(table)
                )
            )
