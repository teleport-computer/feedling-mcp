"""Offline Phase-4 cutover preflight and TEE-primary bridge preparation.

Run only after backend/worker/runner writers are stopped.  The command never
changes ``DATABASE_URL`` or deploys an application; it validates the drain
gates, copies the client-compatible frame-envelope bridge from RDS to TEE, and
advances every TEE identity sequence to its copied maximum.

Dry-run (default) performs no writes.  Mutation requires both explicit flags::

    python -m admin.phase4_cutover
    python -m admin.phase4_cutover --apply --confirm-writes-frozen

Environment: ``DATABASE_URL`` is the frozen RDS source and
``TEE_DATABASE_URL`` is the TEE app-role destination.  Apply additionally
requires ``TEE_MIGRATION_DATABASE_URL`` for the same database's owner role so
the cutover-only R2/immutability triggers can be enabled after copying stops.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable

import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from psycopg.types.json import Jsonb

import db


_DRAIN_GATES = {
    "genesis_import_chunks": "SELECT count(*) FROM genesis_import_chunks",
    "active_genesis_jobs": (
        "SELECT count(*) FROM genesis_import_jobs "
        "WHERE status NOT IN ('done','failed')"
    ),
    "active_voice_results": (
        "SELECT count(*) FROM voice_turn_results WHERE expires_at > now()"
    ),
    "active_voice_streams": (
        "SELECT count(*) FROM voice_turn_streams WHERE expires_at > now()"
    ),
    "active_agent_jobs": (
        "SELECT count(*) FROM agent_jobs "
        "WHERE status IN ('pending','claimed','running')"
    ),
    "agent_action_queue": "SELECT count(*) FROM agent_action_queue",
    "v2_effect_outbox": "SELECT count(*) FROM v2_effect_outbox",
    "v2_terminal_failure_outbox": "SELECT count(*) FROM v2_terminal_failure_outbox",
}

_PRIMARY_TRIGGER_TABLES = {
    "chat_messages_retire_r2_body": "chat_messages",
    "chat_message_archive_retire_r2_body": "chat_message_archive",
    "chat_message_archive_immutable": "chat_message_archive",
}


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _fingerprint(conn: psycopg.Connection) -> tuple[str, str, int]:
    row = conn.execute(
        "SELECT current_database(), COALESCE(inet_server_addr()::text, ''), "
        "inet_server_port()"
    ).fetchone()
    return str(row[0]), str(row[1]), int(row[2])


def _expected_tee_heads() -> set[str]:
    here = Path(__file__).resolve().parents[1]
    cfg = Config(str(here / "alembic_tee" / "alembic.ini"))
    cfg.set_main_option("script_location", str(here / "alembic_tee"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def _actual_tee_heads(conn: psycopg.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT version_num FROM alembic_tee_version"
        ).fetchall()
    }


def _drain_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {
        name: int(conn.execute(query).fetchone()[0])
        for name, query in _DRAIN_GATES.items()
    }


def _canonical_frame(row: tuple) -> bytes:
    return json.dumps(
        row,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _frame_digest(rows: Iterable[tuple]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(_canonical_frame(row))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


_FRAME_SELECT = (
    "SELECT user_id, frame_id, ts, doc, env_meta, body_key "
    "FROM frame_envelopes ORDER BY user_id, frame_id"
)

_CHAT_GENERATION_SELECT = (
    "SELECT user_id, msg_id, storage_generation "
    "FROM chat_messages ORDER BY user_id, msg_id"
)


def _copy_chat_storage_generations(source: psycopg.Connection,
                                   destination: psycopg.Connection) -> dict[str, object]:
    """Carry the R2 lifecycle fence omitted by the old shadow row shape."""
    source_hash = hashlib.sha256()
    source_count = 0
    with destination.transaction():
        with source.cursor(name="phase4_chat_generation_source") as source_cursor:
            source_cursor.execute(_CHAT_GENERATION_SELECT)
            while rows := source_cursor.fetchmany(1000):
                for row in rows:
                    source_hash.update(_canonical_frame(row))
                    source_hash.update(b"\n")
                    source_count += 1
                with destination.cursor() as destination_cursor:
                    destination_cursor.executemany(
                        "UPDATE chat_messages SET storage_generation=%s "
                        "WHERE user_id=%s AND msg_id=%s",
                        [
                            (storage_generation, user_id, msg_id)
                            for user_id, msg_id, storage_generation in rows
                        ],
                    )
    source_digest = source_hash.hexdigest()
    destination_count, destination_digest = _frame_digest(
        destination.execute(_CHAT_GENERATION_SELECT)
    )
    if (destination_count, destination_digest) != (source_count, source_digest):
        raise RuntimeError(
            "chat storage-generation verification failed: "
            f"source=({source_count},{source_digest}) "
            f"destination=({destination_count},{destination_digest})"
        )
    return {"rows": source_count, "sha256": source_digest}


def _copy_frame_bridge(source: psycopg.Connection,
                       destination: psycopg.Connection) -> dict[str, object]:
    """Replace the destination bridge inside one transaction and verify it."""
    source_hash = hashlib.sha256()
    source_count = 0
    with destination.transaction():
        destination.execute("DELETE FROM frame_envelopes")
        with source.cursor(name="phase4_frame_bridge_source") as cursor:
            cursor.execute(_FRAME_SELECT)
            while rows := cursor.fetchmany(500):
                for row in rows:
                    source_hash.update(_canonical_frame(row))
                    source_hash.update(b"\n")
                    source_count += 1
                with destination.cursor() as destination_cursor:
                    destination_cursor.executemany(
                        "INSERT INTO frame_envelopes "
                        "(user_id, frame_id, ts, doc, env_meta, body_key) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        [
                            (
                                user_id,
                                frame_id,
                                ts,
                                Jsonb(doc) if doc is not None else None,
                                Jsonb(env_meta) if env_meta is not None else None,
                                body_key,
                            )
                            for user_id, frame_id, ts, doc, env_meta, body_key in rows
                        ],
                    )

    source_digest = source_hash.hexdigest()
    destination_count, destination_digest = _frame_digest(
        destination.execute(_FRAME_SELECT)
    )
    if (destination_count, destination_digest) != (source_count, source_digest):
        raise RuntimeError(
            "frame bridge verification failed: "
            f"source=({source_count},{source_digest}) "
            f"destination=({destination_count},{destination_digest})"
        )
    return {"rows": source_count, "sha256": source_digest}


def _align_identity_sequences(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT c.relname, a.attname,
               pg_get_serial_sequence(format('%I.%I', n.nspname, c.relname), a.attname)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'
          AND a.attidentity IN ('a', 'd')
          AND NOT a.attisdropped
        ORDER BY c.relname, a.attname
        """
    ).fetchall()
    aligned: dict[str, int] = {}
    with conn.transaction():
        for table, column, sequence in rows:
            maximum = int(
                conn.execute(
                    sql.SQL("SELECT COALESCE(MAX({}), 0) FROM {}").format(
                        sql.Identifier(column), sql.Identifier(table)
                    )
                ).fetchone()[0]
            )
            conn.execute(
                "SELECT setval(%s, %s, %s)",
                (sequence, maximum if maximum > 0 else 1, maximum > 0),
            )
            aligned[f"{table}.{column}"] = maximum
    return aligned


def _enabled_primary_triggers(conn: psycopg.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT tgname FROM pg_trigger "
            "WHERE NOT tgisinternal AND tgenabled = 'O' AND tgname = ANY(%s)",
            (list(_PRIMARY_TRIGGER_TABLES),),
        ).fetchall()
    }


def _activate_primary_contract(owner: psycopg.Connection,
                               app: psycopg.Connection,
                               report: dict[str, object],
                               tee_heads: set[str]) -> dict[str, object]:
    """Owner-enable cutover-only triggers, then publish the startup marker."""
    with owner.transaction():
        for trigger, table in _PRIMARY_TRIGGER_TABLES.items():
            owner.execute(
                sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(
                    sql.Identifier(table), sql.Identifier(trigger)
                )
            )
    enabled = _enabled_primary_triggers(app)
    if enabled != set(_PRIMARY_TRIGGER_TABLES):
        raise RuntimeError(
            "failed to enable TEE-primary triggers: "
            f"expected={sorted(_PRIMARY_TRIGGER_TABLES)} actual={sorted(enabled)}"
        )

    marker = {
        "prepared": True,
        "tee_heads": sorted(tee_heads),
        "frame_sha256": report["frame_bridge"]["sha256"],
        "chat_generation_sha256": report["chat_storage_generations"]["sha256"],
    }
    app.execute(
        "INSERT INTO server_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        (db._TEE_PRIMARY_PREPARED_KEY, json.dumps(marker, sort_keys=True).encode("utf-8")),
    )
    return {"marker": marker, "enabled_triggers": sorted(enabled)}


def run(*, apply: bool, writes_frozen: bool) -> dict[str, object]:
    source_url = _required_env("DATABASE_URL")
    destination_url = _required_env("TEE_DATABASE_URL")
    if apply and not writes_frozen:
        raise RuntimeError("--apply requires --confirm-writes-frozen")

    with (
        psycopg.connect(source_url, autocommit=True) as source,
        psycopg.connect(destination_url, autocommit=True) as destination,
    ):
        source_fingerprint = _fingerprint(source)
        destination_fingerprint = _fingerprint(destination)
        if source_fingerprint == destination_fingerprint:
            raise RuntimeError("DATABASE_URL and TEE_DATABASE_URL resolve to the same database")

        expected_heads = _expected_tee_heads()
        actual_heads = _actual_tee_heads(destination)
        if actual_heads != expected_heads:
            raise RuntimeError(
                "TEE schema is not at head: "
                f"expected={sorted(expected_heads)} actual={sorted(actual_heads)}"
            )

        drain = _drain_counts(source)
        drain["tee_pending_device_migration"] = int(
            destination.execute(
                "SELECT count(*) FROM tee_pending_device_migration"
            ).fetchone()[0]
        )
        blockers = {name: count for name, count in drain.items() if count}
        result: dict[str, object] = {
            "ok": not blockers,
            "mode": "apply" if apply else "dry-run",
            "source": source_fingerprint,
            "destination": destination_fingerprint,
            "tee_heads": sorted(actual_heads),
            "drain": drain,
            "blockers": blockers,
        }
        if blockers:
            return result
        if not apply:
            result["frame_bridge_source_rows"] = int(
                source.execute("SELECT count(*) FROM frame_envelopes").fetchone()[0]
            )
            return result

        # The operator confirmation is the freeze boundary.  Keep the RDS view
        # stable for the bridge digest/copy while the destination is replaced.
        with source.transaction():
            source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            result["frame_bridge"] = _copy_frame_bridge(source, destination)
            result["chat_storage_generations"] = _copy_chat_storage_generations(
                source, destination
            )
        result["identity_sequences"] = _align_identity_sequences(destination)
        owner_url = _required_env("TEE_MIGRATION_DATABASE_URL")
        with psycopg.connect(owner_url, autocommit=True) as owner:
            if _fingerprint(owner) != destination_fingerprint:
                raise RuntimeError(
                    "TEE_MIGRATION_DATABASE_URL does not resolve to TEE_DATABASE_URL"
                )
            result["primary_contract"] = _activate_primary_contract(
                owner, destination, result, actual_heads
            )
        result["ok"] = True
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the TEE bridge and sequences")
    parser.add_argument(
        "--confirm-writes-frozen",
        action="store_true",
        help="confirm backend, worker, runner and consumers are stopped",
    )
    args = parser.parse_args()
    report = run(apply=args.apply, writes_frozen=args.confirm_writes_frozen)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not report.get("ok"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
