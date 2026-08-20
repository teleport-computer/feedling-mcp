"""Phase-4 offline prepare integration tests against isolated PostgreSQL DBs."""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb


def _url_for(base: str, database: str) -> str:
    prefix, _, _ = base.rpartition("/")
    return f"{prefix}/{database}"


def test_phase4_prepare_copies_frame_bridge_and_aligns_sequences(monkeypatch):
    import db
    from admin import phase4_cutover
    from alembic_tee import upgrade_head
    from tee_replicator import terminal_preservation as preservation

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
            source.execute(
                "INSERT INTO chat_messages (user_id,msg_id,ts,doc) "
                "VALUES (%s,'preserved-chat',2,%s)",
                (uid, Jsonb({"id": "preserved-chat", "body_ct": "raw-chat"})),
            )
            source.execute(
                "INSERT INTO frame_envelopes "
                "(user_id,frame_id,ts,doc,env_meta,body_key) "
                "VALUES (%s,'preserved-frame',2,%s,%s,'raw/body-key')",
                (
                    uid,
                    Jsonb({"id": "preserved-frame", "body_ct": "raw-frame"}),
                    Jsonb({"ciphertext": True}),
                ),
            )
            preserved_chat = tuple(
                source.execute(
                    preservation.CONTRACTS["chat_messages"].source_fetch_sql,
                    (uid, "preserved-chat"),
                ).fetchone()
            )
            preserved_frame = tuple(
                source.execute(
                    preservation.CONTRACTS["frame_envelopes"].source_fetch_sql,
                    (uid, "preserved-frame"),
                ).fetchone()
            )
            job_id = source.execute(
                "INSERT INTO agent_jobs (user_id, lane, status) "
                "VALUES (%s, 'chat', 'completed') RETURNING id",
                (uid,),
            ).fetchone()[0]
            for suffix, status in (("applied", "applied"), ("discarded", "discarded")):
                source.execute(
                    "INSERT INTO v2_effect_outbox "
                    "(effect_id, user_id, job_id, effect_type, "
                    " expected_generation, payload, status) "
                    "VALUES (%s, %s, %s, 'reply', 1, '{}'::jsonb, %s)",
                    (f"phase4-{suffix}", uid, job_id, status),
                )
            source.execute(
                "INSERT INTO v2_terminal_failure_outbox "
                "(job_id, user_id, error_code, status_delivered_at, "
                " runtime_error_delivered_at, reply_parent_message_id, "
                " reply_delivered_at) "
                "VALUES (%s, %s, 'historical', now(), now(), 'parent-1', now())",
                (job_id, uid),
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
            destination.execute(
                preservation.CONTRACTS["chat_messages"].insert_sql,
                preservation.CONTRACTS["chat_messages"].insert_args(preserved_chat),
            )
            destination.execute(
                preservation.CONTRACTS["frame_envelopes"].insert_sql,
                preservation.CONTRACTS["frame_envelopes"].insert_args(preserved_frame),
            )
            destination.execute(
                "INSERT INTO tee_pending_device_migration "
                "(user_id,table_name,item_id,reason) VALUES "
                "(%s,'chat_messages','preserved-chat',%s),"
                "(%s,'frame_envelopes','preserved-frame',%s)",
                (
                    uid,
                    preservation.encode_preserved_reason(
                        preservation.canonical_row_sha256(
                            "chat_messages", preserved_chat
                        ),
                        "decrypt_failed:historical",
                    ),
                    uid,
                    preservation.encode_preserved_reason(
                        preservation.canonical_row_sha256(
                            "frame_envelopes", preserved_frame
                        ),
                        "pdm:no_k_enclave",
                    ),
                ),
            )

        report = phase4_cutover.run(apply=True, writes_frozen=True)
        assert report["ok"] is True
        assert not any(report["drain"].values())
        assert report["tee_pending_device_migration_blocking"] == 0
        assert report["tee_terminal_ciphertext_preserved"] == 2
        assert report["preserved_plan_sha256"]
        assert report["frame_bridge"]["rows"] == 2
        assert report["chat_storage_generations"]["rows"] == 2
        assert report["primary_contract"]["marker"][
            "tee_terminal_ciphertext_preserved"
        ] == 2
        assert report["primary_contract"]["marker"][
            "preserved_plan_sha256"
        ] == report["preserved_plan_sha256"]
        assert report["voice_session_smoke"] == {
            "ok": True,
            "cancel_winner": "cancelled",
            "finalize_winner": "finalized",
        }
        assert set(report["primary_contract"]["enabled_triggers"]) == {
            "chat_messages_retire_r2_body",
            "chat_message_archive_retire_r2_body",
            "chat_message_archive_immutable",
        }

        # The same app role now passes startup's read-only head + prepared
        # marker + trigger assertion; no RDS Alembic table is created.
        monkeypatch.setenv("DATABASE_URL", destination_url)
        monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
        db.init_schema()

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
            assert destination.execute(
                "SELECT count(*) FROM tee_pending_device_migration "
                "WHERE reason LIKE 'preserved_ciphertext:v1:%'"
            ).fetchone()[0] == 2
            next_seq = destination.execute(
                "INSERT INTO user_logs (user_id, stream, doc) "
                "VALUES (%s, 'test', '{}'::jsonb) RETURNING seq",
                (uid,),
            ).fetchone()[0]
            assert next_seq == 43
            assert destination.execute(
                "SELECT to_regclass('public.alembic_version')"
            ).fetchone()[0] is None
            assert destination.execute(
                "SELECT count(*) FROM users "
                "WHERE user_id LIKE 'usr_phase4_voice_%'"
            ).fetchone()[0] == 0
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
def test_phase4_voice_session_smoke_fails_closed_when_table_is_missing(backend_env):
    """A green schema head cannot hide a missing runtime-critical voice table."""
    from admin import phase4_cutover

    with psycopg.connect(os.environ["TEE_DATABASE_URL"]) as tee:
        with tee.transaction(force_rollback=True):
            tee.execute(
                "ALTER TABLE voice_call_sessions "
                "RENAME TO voice_call_sessions_missing_probe"
            )
            with pytest.raises(psycopg.errors.UndefinedTable):
                phase4_cutover._voice_session_smoke(tee)


def test_phase4_pending_gate_blocks_every_unaudited_reason(backend_env):
    import db
    from admin import phase4_cutover
    from tee_replicator import terminal_preservation as preservation

    uid = f"usr_phase4_gate_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as tee:
        tee.execute("DELETE FROM tee_pending_device_migration")
        tee.execute(
            "INSERT INTO users (user_id,created_at,doc) VALUES (%s,'',%s)",
            (uid, Jsonb({"user_id": uid})),
        )
        tee.execute(
            "INSERT INTO tee_pending_device_migration "
            "(user_id,table_name,item_id,reason) VALUES "
            "(%s,'chat_messages','terminal','pdm:no_k_enclave'),"
            "(%s,'chat_messages','requeue','requeue:source_updated'),"
            "(%s,'chat_messages','malformed','preserved_ciphertext:v1:bad'),"
            "(%s,'chat_messages','missing-source',%s)",
            (
                uid,
                uid,
                uid,
                uid,
                preservation.encode_preserved_reason(
                    "a" * 64, "decrypt_failed:historical"
                ),
            ),
        )
        with db.get_pool().connection() as source:
            gate = phase4_cutover._pending_gate(source, tee)

        assert gate["tee_pending_device_migration_blocking"] == 4
        assert gate["tee_terminal_ciphertext_preserved"] == 0
        assert gate["preserved_mismatches"] == [
            "missing_source:chat_messages:1"
        ]

        tee.execute("DELETE FROM tee_pending_device_migration")
        tee.execute("DELETE FROM users WHERE user_id=%s", (uid,))
