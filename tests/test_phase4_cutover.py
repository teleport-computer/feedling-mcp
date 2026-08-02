"""Phase-4 offline prepare integration tests against isolated PostgreSQL DBs."""

from __future__ import annotations

import os
import uuid

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


def _url_for(base: str, database: str) -> str:
    prefix, _, _ = base.rpartition("/")
    return f"{prefix}/{database}"


def test_phase4_prepare_copies_frame_bridge_and_aligns_sequences(monkeypatch):
    import db
    from admin import phase4_cutover
    from alembic_tee import upgrade_head

    admin_url = os.environ.get(
        "FEEDLING_TEST_PG",
        "postgresql://postgres:test@127.0.0.1:55432/postgres",
    )
    source_name = f"phase4_source_{uuid.uuid4().hex[:10]}"
    destination_name = f"phase4_tee_{uuid.uuid4().hex[:10]}"
    source_url = _url_for(admin_url, source_name)
    destination_url = _url_for(admin_url, destination_name)

    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(source_name)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(destination_name)))
    try:
        monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
        monkeypatch.setenv("DATABASE_URL", source_url)
        db.init_schema()

        monkeypatch.setenv("TEE_DATABASE_URL", destination_url)
        monkeypatch.setenv("TEE_MIGRATION_DATABASE_URL", destination_url)
        upgrade_head()

        uid = f"usr_{uuid.uuid4().hex[:16]}"
        frame = {"v": 1, "owner_user_id": uid, "body_ct": "ciphertext"}
        user = {"user_id": uid, "api_key_hash": "phase4-test"}
        with psycopg.connect(source_url, autocommit=True) as source:
            source.execute(
                "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', %s)",
                (uid, Jsonb(user)),
            )
            source.execute(
                "INSERT INTO frame_envelopes (user_id, frame_id, ts, doc) "
                "VALUES (%s, 'frame-1', 1, %s)",
                (uid, Jsonb(frame)),
            )
            source.execute(
                "INSERT INTO chat_messages "
                "(user_id, msg_id, ts, doc, storage_generation) "
                "VALUES (%s, 'chat-1', 1, %s, 7)",
                (uid, Jsonb({"id": "chat-1", "body_ct": "ciphertext"})),
            )
        with psycopg.connect(destination_url, autocommit=True) as destination:
            destination.execute(
                "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', %s)",
                (uid, Jsonb(user)),
            )
            destination.execute(
                "INSERT INTO user_logs (user_id, stream, seq, doc) "
                "OVERRIDING SYSTEM VALUE VALUES (%s, 'test', 42, '{}'::jsonb)",
                (uid,),
            )
            destination.execute(
                "INSERT INTO chat_messages (user_id, msg_id, ts, doc) "
                "VALUES (%s, 'chat-1', 1, %s)",
                (uid, Jsonb({"id": "chat-1", "body": "plaintext"})),
            )

        report = phase4_cutover.run(apply=True, writes_frozen=True)
        assert report["ok"] is True
        assert report["frame_bridge"]["rows"] == 1
        assert report["chat_storage_generations"]["rows"] == 1

        with psycopg.connect(destination_url, autocommit=True) as destination:
            copied = destination.execute(
                "SELECT doc FROM frame_envelopes "
                "WHERE user_id=%s AND frame_id='frame-1'",
                (uid,),
            ).fetchone()
            assert copied[0] == frame
            assert destination.execute(
                "SELECT storage_generation FROM chat_messages "
                "WHERE user_id=%s AND msg_id='chat-1'",
                (uid,),
            ).fetchone()[0] == 7
            next_seq = destination.execute(
                "INSERT INTO user_logs (user_id, stream, doc) "
                "VALUES (%s, 'test', '{}'::jsonb) RETURNING seq",
                (uid,),
            ).fetchone()[0]
            assert next_seq == 43
            assert destination.execute(
                "SELECT to_regclass('public.alembic_version')"
            ).fetchone()[0] is None
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            for database in (source_name, destination_name):
                admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s",
                    (database,),
                )
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(database)
                    )
                )
