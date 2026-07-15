"""db.reconcile_unenqueued_v2_messages: the A7 reconciliation backstop.

chat_append_and_enqueue closes the crash window between "persist the user's
message" and "enqueue its chat job" going forward, but this sweeper is the
belt-and-suspenders check for anything that slips past it (a bug, a manual
data fix, or a pre-A7 message). It finds db_action_v2 users whose newest chat
message is an unanswered user message with no active chat job, and
single-flight enqueues a catch-up job for each.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store
from hosted import config_store as hosted_config_store
from core import store as core_store

from conftest import configure_model_api_route, seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 reconcile tests require the PostgreSQL test fixture",
)


@pytest.fixture(autouse=True)
def pg_clean():
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE chat_messages, agent_jobs, v2_runtime_state, user_blobs, "
            "model_api_routes, model_api_credentials CASCADE"
        )
    yield


def _mark_db_action_v2(uid: str) -> None:
    # set_hosted_runtime_mode requires an existing model_api config to persist
    # against (mirrors test_chat_send_v2_enqueue.py's _seed pattern).
    configure_model_api_route(uid, provider="anthropic", model="m", test_status="ok")
    store = core_store.get_store(uid)
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")


def _insert_user_message(uid: str, msg_id: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,%s,%s,%s)",
            (uid, msg_id, time.time(), db.Jsonb({"id": msg_id, "role": "user", "ts": time.time()})),
        )


def test_orphan_user_message_gets_a_catchup_job():
    uid = "u_reconcile_orphan"
    seed_user(uid)
    _mark_db_action_v2(uid)
    _insert_user_message(uid, "m-orphan-1")

    assert db.reconcile_unenqueued_v2_messages() == 1

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT lane, status, reason, trace_id FROM agent_jobs WHERE user_id=%s",
            (uid,),
        ).fetchall()
    assert len(rows) == 1
    lane, status, reason, trace_id = rows[0]
    assert lane == "chat"
    assert status == "pending"
    assert reason == "reconcile"
    assert trace_id == "m-orphan-1"


def test_terminal_reconcile_job_is_a_durable_per_message_stop_marker():
    uid = "u_reconcile_terminal"
    seed_user(uid)
    _mark_db_action_v2(uid)
    _insert_user_message(uid, "m-terminal-1")

    assert db.reconcile_unenqueued_v2_messages() == 1
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='failed',finished_at=now(),"
            "last_error='provider_error' WHERE user_id=%s",
            (uid,),
        )

    assert db.reconcile_unenqueued_v2_messages() == 0
    assert db.reconcile_unenqueued_v2_messages() == 0
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT status,reason,trace_id FROM agent_jobs WHERE user_id=%s",
            (uid,),
        ).fetchall()
    assert rows == [("failed", "reconcile", "m-terminal-1")]


def test_newer_message_gets_its_own_single_reconcile_attempt():
    uid = "u_reconcile_new_message"
    seed_user(uid)
    _mark_db_action_v2(uid)
    _insert_user_message(uid, "m-old")
    assert db.reconcile_unenqueued_v2_messages() == 1
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='expired',finished_at=now() "
            "WHERE user_id=%s",
            (uid,),
        )

    _insert_user_message(uid, "m-new")
    assert db.reconcile_unenqueued_v2_messages() == 1
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT trace_id,status FROM agent_jobs WHERE user_id=%s "
            "ORDER BY id",
            (uid,),
        ).fetchall()
    assert rows == [("m-old", "expired"), ("m-new", "pending")]


def test_superseded_cutover_race_does_not_consume_message_retry_marker():
    uid = "u_reconcile_superseded"
    seed_user(uid)
    _mark_db_action_v2(uid)
    _insert_user_message(uid, "m-cutover-race")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs "
            "(user_id,lane,status,reason,trace_id,last_error,finished_at) "
            "VALUES (%s,'chat','superseded','reconcile',%s,"
            "'stale_runtime_generation',now())",
            (uid, "m-cutover-race"),
        )

    # The earlier row never ran; after ownership returns to V2 this message is
    # still entitled to its one real catch-up attempt.
    assert db.reconcile_unenqueued_v2_messages() == 1
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT status,trace_id FROM agent_jobs WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
    assert rows == [
        ("superseded", "m-cutover-race"),
        ("pending", "m-cutover-race"),
    ]


def test_user_with_active_chat_job_is_not_double_enqueued():
    uid = "u_reconcile_active"
    seed_user(uid)
    _mark_db_action_v2(uid)
    _insert_user_message(uid, "m-active-1")
    jobs_store.enqueue_job(uid, "chat", reason="chat_send")

    assert db.reconcile_unenqueued_v2_messages() == 0

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT count(*) FROM agent_jobs WHERE user_id=%s", (uid,),
        ).fetchone()
    assert rows[0] == 1


def test_user_whose_newest_message_is_already_replied_is_not_orphan():
    uid = "u_reconcile_replied"
    seed_user(uid)
    _mark_db_action_v2(uid)
    _insert_user_message(uid, "m-user-1")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,%s,%s,%s)",
            (uid, "m-reply-1", time.time() + 1,
             db.Jsonb({"id": "m-reply-1", "role": "assistant", "ts": time.time() + 1})),
        )

    assert db.reconcile_unenqueued_v2_messages() == 0

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT count(*) FROM agent_jobs WHERE user_id=%s", (uid,),
        ).fetchone()
    assert rows[0] == 0


def test_resident_user_with_orphan_message_is_ignored():
    """Only db_action_v2 users are candidates — a resident_cli user's
    unanswered message is not this sweeper's concern (no v2 job queue exists
    for them)."""
    uid = "u_reconcile_resident"
    seed_user(uid)
    _insert_user_message(uid, "m-resident-1")

    assert db.reconcile_unenqueued_v2_messages() == 0

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT count(*) FROM agent_jobs WHERE user_id=%s", (uid,),
        ).fetchone()
    assert rows[0] == 0


def test_stale_blob_mode_cannot_override_authoritative_resident_state():
    uid = "u_reconcile_split_control"
    seed_user(uid)
    _mark_db_action_v2(uid)
    _insert_user_message(uid, "m-split-1")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET hosted_runtime_state='resident' "
            "WHERE user_id=%s",
            (uid,),
        )

    assert db.reconcile_unenqueued_v2_messages() == 0
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_jobs WHERE user_id=%s", (uid,),
        ).fetchone()[0] == 0
