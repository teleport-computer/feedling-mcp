"""db.reconcile_unenqueued_v2_messages: the A7 reconciliation backstop.

chat_append_and_enqueue closes the crash window between "persist the user's
message" and "enqueue its chat job" going forward, but this sweeper is the
belt-and-suspenders check for anything that slips past it (a bug, a manual
data fix, or a pre-A7 message). It finds db_action_v2 users whose newest chat
message is an unanswered user message with no active chat job, and
single-flight enqueues a catch-up job for each.
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
import provider_client
from hosted import config_store as hosted_config_store
from core import store as core_store
from model_api_runtime.v2 import cursor as v2_cursor
from model_api_runtime.v2 import jobs_store, serve_worker, worker

from conftest import configure_model_api_route, seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 reconcile tests require the PostgreSQL test fixture",
)


@pytest.fixture(autouse=True)
def pg_clean(monkeypatch):
    # These exact catch-up queue shapes specify the profile-off contract.
    monkeypatch.setenv("FEEDLING_V2_PROFILE_ENABLED", "0")
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


def _insert_human_message(uid: str, msg_id: str) -> int:
    ts = time.time()
    db.chat_append_strict(
        uid,
        msg_id,
        ts,
        {
            "id": msg_id,
            "role": "human",
            "ts": ts,
            "v": 1,
            "body_ct": "cipher-human-message",
            "nonce": "nonce-human-message",
            "K_user": "wrapped-user-key",
            "K_enclave": "wrapped-enclave-key",
            "owner_user_id": uid,
            "content_type": "text",
        },
        core_store.MAX_CHAT_MESSAGES,
    )
    seq = db.chat_seq_for_msg_id(uid, msg_id)
    assert seq is not None
    return seq


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


def test_eager_cutover_recovery_accepts_legacy_human_role():
    uid = "u_reconcile_eager_human"
    seed_user(uid)
    _mark_db_action_v2(uid)
    _insert_human_message(uid, "m-eager-human")

    assert db.reconcile_unenqueued_v2_message_for_user(uid) is True

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT lane,status,reason,trace_id FROM agent_jobs WHERE user_id=%s",
            (uid,),
        ).fetchone()
    assert row == (
        "chat",
        "pending",
        "runtime_cutover_recovery",
        "m-eager-human",
    )


def test_reconciled_human_message_is_replied_and_advances_seq_cursor(monkeypatch):
    """Production-shaped regression for the legacy iOS ``role=human`` form.

    Exercise the real orphan reconciler, strict DB seq reader, worker process,
    fenced reply outbox, and transactional reply+cursor sink. Only enclave and
    provider boundaries are faked, as they are external to this test process.
    """
    uid = "u_reconcile_human"
    seed_user(uid)
    _mark_db_action_v2(uid)
    human_seq = _insert_human_message(uid, "m-human-1")

    assert db.reconcile_unenqueued_v2_messages() == 1
    job = jobs_store.claim_next_job("human-reconcile-worker")
    assert job is not None
    assert job["reason"] == "reconcile"
    assert job["trace_id"] == "m-human-1"

    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")
    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, *args, **kwargs: (
            b"hello from legacy human role"
            if envelope.get("id") == "m-human-1"
            else (_ for _ in ()).throw(
                AssertionError(f"unexpected decrypt: {envelope.get('id')}")
            )
        ),
    )

    provider_calls = []

    async def _provider(config, messages, *, tools=None):
        provider_calls.append({"messages": messages, "tools": tools})
        return {
            "reply": "reply to human",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)

    # Keep the production encrypted/fenced outbox shape without requiring a
    # live client public key in this hermetic test. The real transactional sink
    # below consumes this envelope and commits reply + cursor together.
    def _reply_payload(
        store,
        text,
        *,
        effect_id,
        reply_through_seq=None,
    ):
        assert store.user_id == uid
        assert text == "reply to human"
        payload = {
            "envelope": {
                "id": "reply-human-1",
                "v": 1,
                "body_ct": "cipher-reply-to-human",
                "nonce": "nonce-reply-to-human",
                "K_user": "wrapped-user-key",
                "K_enclave": "wrapped-enclave-key",
                "owner_user_id": uid,
                "visibility": "shared",
            }
        }
        if reply_through_seq is not None:
            payload["reply_through_seq"] = int(reply_through_seq)
        return payload

    monkeypatch.setattr(worker, "_build_encrypted_reply_effect_payload", _reply_payload)

    config = provider_client.ProviderConfig(
        provider="anthropic",
        model="claude-sonnet-4-test",
        api_key="sk-test",
        context_window_tokens=200_000,
    )
    deps = worker.TurnDeps(
        read_messages=serve_worker._read_messages,
        read_messages_after_seq=serve_worker._read_messages_after_seq,
        resolve_provider=lambda _uid: (config, {}),
        mint_enclave_token=lambda _uid: "rt",
        read_tail_after_seq=serve_worker._read_tail_after_seq,
        read_summary_with_seq=serve_worker._read_summary_with_seq,
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=config,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert len(provider_calls) == 1
    # Prompt assembly may place coalesced input inside a structured context
    # block rather than retain it as a top-level provider ``user`` message.
    assert "hello from legacy human role" in str(provider_calls[0]["messages"])
    assert v2_cursor.load_seq(core_store.get_store(uid)) == human_seq
    with db.get_pool().connection() as conn:
        reply = conn.execute(
            "SELECT doc FROM chat_messages "
            "WHERE user_id=%s AND doc->>'role'='openclaw'",
            (uid,),
        ).fetchone()
        consumed_human = conn.execute(
            "SELECT doc FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, "m-human-1"),
        ).fetchone()
    assert reply is not None
    assert reply[0]["body_ct"] == "cipher-reply-to-human"
    assert consumed_human is not None
    assert consumed_human[0]["reply_status"] == "replied"
    assert consumed_human[0]["reply_message_id"] == "reply-human-1"


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
    user_seq = db.chat_seq_for_msg_id(uid, "m-user-1")
    assert user_seq is not None
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,%s,%s,%s)",
            (uid, "m-reply-1", time.time() + 1,
             db.Jsonb({"id": "m-reply-1", "role": "assistant", "ts": time.time() + 1})),
        )
        conn.execute(
            "UPDATE user_blobs SET doc=jsonb_set(doc,'{v2_reply_cursor_seq}',%s) "
            "WHERE user_id=%s AND kind='model_api_runtime'",
            (db.Jsonb(user_seq), uid),
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


def _chat_message_envelope(user_id: str, marker: str) -> dict:
    """Minimal valid v1 shared envelope for chat_core.write_message."""
    def _b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return {
        "v": 1, "id": marker,
        "body_ct": _b64(f"{user_id}:{marker}".encode()),
        "nonce": _b64(b"\x00" * 12), "K_user": _b64(b"\x01" * 32),
        "K_enclave": _b64(b"\x02" * 32),
        "visibility": "shared", "owner_user_id": user_id,
    }


def test_chat_message_from_v2_user_enqueues_immediately_without_sweep():
    """A db_action_v2 user whose message arrives on /v1/chat/message (an
    onboarding-incomplete / post-reset client that has not switched to
    /v1/model_api/chat/send) must get its V2 chat job eagerly at write time,
    not wait up to one 60s fleet reconcile tick. No db.reconcile_* call here:
    the job must already exist right after write_message returns."""
    from chat import chat_core

    uid = "u_eager_chatmsg_v2"
    seed_user(uid)
    _mark_db_action_v2(uid)
    store = core_store.get_store(uid)

    body, status = chat_core.write_message(
        store, {"envelope": _chat_message_envelope(uid, "m-eager-chatmsg")}
    )
    assert status == 200, body

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT lane,status,trace_id FROM agent_jobs WHERE user_id=%s",
            (uid,),
        ).fetchone()
    assert row is not None, "write_message did not eagerly enqueue a V2 chat job"
    lane, status_j, trace_id = row
    assert lane == "chat"
    assert status_j == "pending"
    assert trace_id == "m-eager-chatmsg"


def test_chat_message_from_resident_user_enqueues_no_v2_job():
    """A resident (non-V2) user's /v1/chat/message must not touch the V2 chat
    lane — the eager hook is a no-op for anyone whose fence isn't
    db_action_v2. This keeps the resident/self-hosted hot path clean."""
    from chat import chat_core

    uid = "u_eager_chatmsg_resident"
    seed_user(uid)
    configure_model_api_route(uid, provider="anthropic", model="m", test_status="ok")
    # deliberately NOT marked db_action_v2 -> stays resident
    store = core_store.get_store(uid)

    body, status = chat_core.write_message(
        store, {"envelope": _chat_message_envelope(uid, "m-resident")}
    )
    assert status == 200, body

    with db.get_pool().connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM agent_jobs WHERE user_id=%s", (uid,)
        ).fetchone()[0]
    assert n == 0
