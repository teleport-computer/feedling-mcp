"""Transactional chat change capture for cross-worker incremental caches.

Each test exercises the real PostgreSQL triggers.  The break these tests catch
is a committed chat mutation that fails to advance a durable per-user version,
or an account-delete cascade that is blocked by the capture machinery.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb

import db


ROOT = Path(__file__).parent.parent


def _uid(label: str) -> str:
    return f"usr_chat_change_{label}_{uuid.uuid4().hex[:10]}"


def _seed_user(conn, user_id: str) -> None:
    conn.execute(
        "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb)",
        (user_id,),
    )


def _events(conn, user_id: str) -> list[tuple]:
    return conn.execute(
        "SELECT version, operation, message_ids "
        "FROM chat_change_events WHERE user_id=%s ORDER BY version",
        (user_id,),
    ).fetchall()


def test_chat_change_schema_and_statement_triggers_are_installed():
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT to_regclass('public.chat_change_state'), "
            "to_regclass('public.chat_change_events')"
        ).fetchone() == ("chat_change_state", "chat_change_events")
        triggers = conn.execute(
            "SELECT tgname, tgenabled, tgtype "
            "FROM pg_trigger "
            "WHERE tgrelid='public.chat_messages'::regclass "
            "AND tgname LIKE 'chat_change_capture_%' "
            "ORDER BY tgname"
        ).fetchall()

    assert triggers == [
        ("chat_change_capture_delete", "O", 8),
        ("chat_change_capture_insert", "O", 4),
        ("chat_change_capture_update", "O", 16),
    ]


def test_chat_change_events_group_each_user_once_per_statement():
    first = _uid("multi_a")
    second = _uid("multi_b")
    try:
        with db.get_pool().connection() as conn:
            _seed_user(conn, first)
            _seed_user(conn, second)
            conn.execute(
                "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES "
                "(%s,'b',1,%s),(%s,'a',2,%s),(%s,'z',3,%s)",
                (
                    first,
                    Jsonb({"id": "b"}),
                    first,
                    Jsonb({"id": "a"}),
                    second,
                    Jsonb({"id": "z"}),
                ),
            )
            assert _events(conn, first) == [(1, "upsert", ["a", "b"])]
            assert _events(conn, second) == [(1, "upsert", ["z"])]

            conn.execute(
                "UPDATE chat_messages SET doc=doc || '{\"changed\":true}'::jsonb "
                "WHERE user_id=%s",
                (first,),
            )
            assert _events(conn, first)[-1] == (2, "upsert", ["a", "b"])

            conn.execute(
                "DELETE FROM chat_messages WHERE user_id=%s AND msg_id='a'",
                (first,),
            )
            assert _events(conn, first)[-1] == (3, "delete", ["a"])
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=ANY(%s)", ([first, second],))


def test_chat_change_large_delete_emits_one_reset_without_message_ids():
    user_id = _uid("reset")
    try:
        with db.get_pool().connection() as conn:
            _seed_user(conn, user_id)
            conn.execute(
                "INSERT INTO chat_messages (user_id,msg_id,ts,doc) "
                "SELECT %s, 'm' || g::text, g, jsonb_build_object('id','m' || g::text) "
                "FROM generate_series(1,65) AS g",
                (user_id,),
            )
            conn.execute("DELETE FROM chat_change_events WHERE user_id=%s", (user_id,))
            conn.execute("DELETE FROM chat_change_state WHERE user_id=%s", (user_id,))

            conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (user_id,))

            assert _events(conn, user_id) == [(1, "reset", [])]
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_chat_change_rolls_back_event_and_notify_with_business_write():
    user_id = _uid("rollback")
    listener = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    listener.execute("LISTEN feedling_wake")
    try:
        with db.get_pool().connection() as conn:
            _seed_user(conn, user_id)
            try:
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO chat_messages (user_id,msg_id,ts,doc) "
                        "VALUES (%s,'rolled-back',1,%s)",
                        (user_id, Jsonb({"id": "rolled-back"})),
                    )
                    raise RuntimeError("force rollback")
            except RuntimeError as exc:
                assert str(exc) == "force rollback"

            assert conn.execute(
                "SELECT count(*) FROM chat_messages WHERE user_id=%s",
                (user_id,),
            ).fetchone()[0] == 0
            assert _events(conn, user_id) == []

        assert list(listener.notifies(timeout=0.2, stop_after=1)) == []

        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES (%s,'ok',2,%s)",
                (user_id, Jsonb({"id": "ok"})),
            )

        notices = list(listener.notifies(timeout=1.0, stop_after=1))
        assert len(notices) == 1
        assert json.loads(notices[0].payload) == {
            "v": 2,
            "c": "chat",
            "u": user_id,
            "r": 1,
        }
    finally:
        listener.close()
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))


def test_account_delete_cascade_does_not_recreate_chat_change_rows():
    user_id = _uid("account_delete")
    with db.get_pool().connection() as conn:
        _seed_user(conn, user_id)
        conn.execute(
            "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES (%s,'m1',1,%s)",
            (user_id, Jsonb({"id": "m1"})),
        )
        assert _events(conn, user_id) == [(1, "upsert", ["m1"])]

        conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))

        assert conn.execute(
            "SELECT count(*) FROM chat_messages WHERE user_id=%s", (user_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM chat_change_state WHERE user_id=%s", (user_id,)
        ).fetchone()[0] == 0
        assert _events(conn, user_id) == []


def test_0098_downgrade_and_upgrade_are_repeatable():
    cfg = Config(str(ROOT / "backend" / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend" / "alembic"))

    try:
        command.downgrade(cfg, "0097_v2_job_recovery_events")
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT to_regclass('chat_change_state'), "
                "to_regclass('chat_change_events'), "
                "to_regclass('ix_chat_messages_user_ts_seq')"
            ).fetchone() == (None, None, None)
            assert conn.execute(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgrelid='chat_messages'::regclass "
                "AND tgname LIKE 'chat_change_capture_%'"
            ).fetchone() == (0,)

        command.upgrade(cfg, "head")
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchall() == [("0101_chat_change_events",)]
            assert conn.execute(
                "SELECT to_regclass('chat_change_state'), "
                "to_regclass('chat_change_events'), "
                "to_regclass('ix_chat_messages_user_ts_seq')"
            ).fetchone() == (
                "chat_change_state",
                "chat_change_events",
                "ix_chat_messages_user_ts_seq",
            )
    finally:
        command.upgrade(cfg, "head")
