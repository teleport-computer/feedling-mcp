from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
import time

import conftest
import db
import psycopg
import provider_client
import pytest
from capabilities import registry as cap_registry
from core import store as core_store
from model_api_runtime.v2 import cursor
from model_api_runtime.v2 import effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker
from provider_types import ToolExchange


_TEST_PROVIDER_CONFIG = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-sonnet-4-test",
    api_key="test-key",
)


def _envelope(item_id: str, *, body: str = "ciphertext") -> dict:
    return {
        "v": 1,
        "id": item_id,
        "owner_user_id": "ignored-by-store",
        "visibility": "shared",
        "body_ct": body,
        "nonce": "nonce",
        "K_user": "sealed-user-key",
        "K_enclave": "sealed-enclave-key",
    }


def _reset(uid: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_sink_applied")
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM user_blobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
        # These tests exercise the V2 sink directly. Production reaches it only
        # after the control plane has completed the resident -> draining -> V2
        # transition; seed that authoritative state explicitly so a late sink
        # is now allowed to fail closed after rollback.
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'v2',1)",
            (uid,),
        )


def test_deterministic_reply_row_and_cursor_commit_are_idempotent():
    uid = "u_atomic_reply_cursor"
    conftest.seed_user(uid)
    _reset(uid)
    store = core_store.get_store(uid)
    envelope = _envelope("a" * 32)

    first = store.append_chat(
        "openclaw", "model_api", envelope,
        strict=True, reply_through_seq=41,
    )
    second = store.append_chat(
        "openclaw", "model_api", envelope,
        strict=True, reply_through_seq=39,
    )

    with db.get_pool().connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, envelope["id"]),
        ).fetchone()[0]
    assert count == 1
    assert first["seq"] == second["seq"]
    assert cursor.load_seq(store) == 41  # replay cannot regress the cursor


def test_final_v2_reply_marks_consumed_user_rows_answered_for_resident_rollback():
    uid = "u_atomic_reply_resident_bridge"
    conftest.seed_user(uid)
    _reset(uid)
    for index in (1, 2):
        db.chat_append(
            uid,
            f"user-{index}",
            float(index),
            {"id": f"user-{index}", "role": "user", "body_ct": f"ct-{index}"},
            0,
        )
    through_seq = db.chat_seq_for_msg_id(uid, "user-2")
    assert through_seq is not None

    db.chat_append_effect_with_cursor(
        uid,
        "v2-reply",
        3.0,
        {"id": "v2-reply", "role": "openclaw", "source": "model_api", "body_ct": "reply"},
        0,
        through_seq,
    )

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT msg_id,doc FROM chat_messages "
            "WHERE user_id=%s AND doc->>'role'='user' ORDER BY seq",
            (uid,),
        ).fetchall()
    assert [row[0] for row in rows] == ["user-1", "user-2"]
    for _msg_id, doc in rows:
        assert doc["reply_status"] == "replied"
        assert doc["reply_message_id"] == "v2-reply"
        assert doc["replied_by"] == "hosted_runtime_v2"

    # Resident's DB-level delivery CAS must refuse both rows even if its
    # process-local chat cache predates the V2 metadata update.
    for msg_id in ("user-1", "user-2"):
        assert db.chat_try_claim_reply(
            uid,
            msg_id,
            "resident-consumer",
            10.0,
            {"reply_claimed_by": "resident-consumer", "reply_claim_expires_at": "20"},
            redelivery=True,
        ) is None


def test_v2_final_reply_aborts_if_resident_won_after_prompt_snapshot():
    uid = "u_atomic_reply_resident_race"
    conftest.seed_user(uid)
    _reset(uid)
    db.chat_append(
        uid,
        "raced-user",
        1.0,
        {"id": "raced-user", "role": "user", "body_ct": "ct"},
        0,
    )
    through_seq = db.chat_seq_for_msg_id(uid, "raced-user")
    assert through_seq is not None
    db.chat_update_metadata(
        uid,
        "raced-user",
        {"reply_status": "replied", "reply_message_id": "resident-reply"},
    )

    with pytest.raises(RuntimeError, match="already answered"):
        db.chat_append_effect_with_cursor(
            uid,
            "v2-late-reply",
            2.0,
            {"id": "v2-late-reply", "role": "openclaw", "source": "model_api", "body_ct": "late"},
            0,
            through_seq,
        )

    assert db.chat_seq_for_msg_id(uid, "v2-late-reply") is None


def test_v2_effect_fence_discards_late_write_after_resident_rollback():
    uid = "u_atomic_reply_late_v2_cutover"
    conftest.seed_user(uid)
    _reset(uid)
    db.effect_enqueue(
        "late-v2-effect",
        uid,
        901,
        "reply",
        1,
        {"envelope": _envelope("late-v2-reply"), "reply_through_seq": 0},
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET hosted_runtime_state='resident', "
            "runtime_generation=runtime_generation+1 "
            "WHERE user_id=%s",
            (uid,),
        )

    dispatched = []
    result = effect_outbox.apply_pending_effects(
        uid,
        dispatch=lambda effect_type, payload: dispatched.append(
            (effect_type, payload)),
    )

    assert result == {"applied": 0, "discarded": 1}
    assert dispatched == []
    assert db.chat_seq_for_msg_id(uid, "late-v2-reply") is None


def test_resident_reply_commit_is_atomic_and_bridged_before_v2_cutover():
    uid = "u_atomic_resident_reply_cutover"
    conftest.seed_user(uid)
    _reset(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET hosted_runtime_state='resident' "
            "WHERE user_id=%s",
            (uid,),
        )
    db.chat_append(
        uid,
        "resident-parent",
        1.0,
        {"id": "resident-parent", "role": "user", "body_ct": "question"},
        0,
    )
    parent_seq = db.chat_seq_for_msg_id(uid, "resident-parent")
    assert parent_seq is not None

    seq, inserted, parent, persisted_reply = db.chat_append_resident_reply(
        uid,
        "resident-reply",
        2.0,
        {"id": "resident-reply", "role": "openclaw", "source": "chat", "body_ct": "answer"},
        0,
        parent_msg_id="resident-parent",
        replied_by="resident-a",
    )
    assert seq > parent_seq
    assert inserted is True
    assert persisted_reply["id"] == "resident-reply"
    assert parent["reply_status"] == "replied"
    assert parent["reply_message_id"] == "resident-reply"

    profile = db.patch_blob_strict(
        uid,
        "model_api_runtime",
        {"hosted_runtime_mode": "db_action_v2"},
        runtime_state_target="v2",
    )
    assert profile["v2_reply_cursor_seq"] == parent_seq

    db.chat_append(
        uid,
        "late-resident-parent",
        3.0,
        {"id": "late-resident-parent", "role": "user", "body_ct": "late"},
        0,
    )
    with pytest.raises(db.ResidentReplyRejected, match="runtime_not_resident"):
        db.chat_append_resident_reply(
            uid,
            "late-resident-reply",
            4.0,
            {"id": "late-resident-reply", "role": "openclaw", "source": "chat", "body_ct": "late"},
            0,
            parent_msg_id="late-resident-parent",
            replied_by="resident-a",
        )
    assert db.chat_seq_for_msg_id(uid, "late-resident-reply") is None


def test_inflight_resident_final_reply_serializes_before_cutover():
    """A provider call finishing during cutover cannot create two replies.

    Hold the parent row so the resident transaction is observably parked after
    taking the runtime fence. The mode flip must then park behind that fence;
    once released, the resident reply commits first and cutover bridges its
    exact parent sequence before V2 becomes visible.
    """
    uid = "u_atomic_resident_inflight_cutover"
    conftest.seed_user(uid)
    _reset(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET hosted_runtime_state='resident' "
            "WHERE user_id=%s",
            (uid,),
        )
    db.chat_append(
        uid,
        "blocked-parent",
        1.0,
        {"id": "blocked-parent", "role": "user", "body_ct": "question"},
        0,
    )
    parent_seq = db.chat_seq_for_msg_id(uid, "blocked-parent")
    assert parent_seq is not None

    def _wait_for_lock(query_fragment: str, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with db.get_pool().connection() as probe:
                count = probe.execute(
                    "SELECT COUNT(*) FROM pg_stat_activity "
                    "WHERE datname=current_database() "
                    "  AND pid<>pg_backend_pid() "
                    "  AND wait_event_type='Lock' "
                    "  AND query LIKE %s",
                    (f"%{query_fragment}%",),
                ).fetchone()[0]
            if count:
                return
            time.sleep(0.01)
        raise AssertionError(f"query never blocked on lock: {query_fragment}")

    blocker = db.get_pool().getconn()
    tx = blocker.transaction()
    tx.__enter__()
    blocker.execute(
        "SELECT doc FROM chat_messages "
        "WHERE user_id=%s AND msg_id=%s FOR UPDATE",
        (uid, "blocked-parent"),
    )
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        resident_future = pool.submit(
            db.chat_append_resident_reply,
            uid,
            "resident-inflight-reply",
            2.0,
            {
                "id": "resident-inflight-reply",
                "role": "openclaw",
                "source": "chat",
                "body_ct": "answer",
            },
            0,
            parent_msg_id="blocked-parent",
            replied_by="resident-a",
        )
        _wait_for_lock("SELECT doc FROM chat_messages")

        cutover_future = pool.submit(
            db.patch_blob_strict,
            uid,
            "model_api_runtime",
            {"hosted_runtime_mode": "db_action_v2"},
            runtime_state_target="v2",
        )
        _wait_for_lock("SELECT hosted_runtime_state FROM v2_runtime_state")
        assert not cutover_future.done()

        tx.__exit__(None, None, None)
        tx = None
        _seq, inserted, parent_doc, _reply_doc = resident_future.result(timeout=3)
        profile = cutover_future.result(timeout=3)
    finally:
        if tx is not None:
            tx.__exit__(None, None, None)
        pool.shutdown(wait=True, cancel_futures=True)
        db.get_pool().putconn(blocker)

    assert inserted is True
    assert parent_doc["reply_message_id"] == "resident-inflight-reply"
    assert profile["v2_reply_cursor_seq"] == parent_seq
    with db.get_pool().connection() as conn:
        state = conn.execute(
            "SELECT hosted_runtime_state FROM v2_runtime_state WHERE user_id=%s",
            (uid,),
        ).fetchone()[0]
    assert state == "v2"


def test_unlinked_resident_output_is_fenced_after_v2_cutover():
    uid = "u_atomic_unlinked_resident_cutover"
    conftest.seed_user(uid)
    _reset(uid)  # authoritative state is V2

    with pytest.raises(db.ResidentReplyRejected, match="runtime_not_resident"):
        db.chat_append_resident_message(
            uid,
            "late-proactive-resident-message",
            1.0,
            {
                "id": "late-proactive-resident-message",
                "role": "openclaw",
                "source": "proactive_job",
                "body_ct": "late",
            },
            0,
        )

    assert db.chat_seq_for_msg_id(uid, "late-proactive-resident-message") is None


def test_strict_runtime_cursor_writes_mirror_exact_committed_document(monkeypatch):
    uid = "u_atomic_runtime_blob_mirror"
    conftest.seed_user(uid)
    _reset(uid)
    db.set_blob_strict(uid, "model_api_runtime", {"sibling": "kept"})

    from tee_shadow import mirror

    calls: list[tuple[str, tuple]] = []
    monkeypatch.setattr(mirror, "execute", lambda sql, params=(): calls.append((sql, params)))

    persisted = db.advance_blob_int_strict(
        uid, "model_api_runtime", cursor.CURSOR_KEY, 17)

    assert persisted == {
        "sibling": "kept",
        cursor.CURSOR_KEY: 17,
        db._BLOB_REVISION_KEY: 1,
    }
    assert len(calls) == 1
    assert "INSERT INTO user_blobs" in calls[0][0]
    assert calls[0][1][0:2] == (uid, "model_api_runtime")
    assert calls[0][1][2].obj == persisted


def test_reordered_runtime_blob_mirrors_cannot_restore_an_older_cursor(
    backend_env, monkeypatch,
):
    """A slow old post-commit mirror must lose to the newer document revision."""
    uid = "u_atomic_runtime_blob_mirror_reordered"
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    conftest.seed_user(uid)
    _reset(uid)
    db.set_blob_strict(uid, "model_api_runtime", {"sibling": "kept"})

    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO user_blobs (user_id,kind,doc) VALUES (%s,%s,%s) "
            "ON CONFLICT (user_id,kind) DO UPDATE SET doc=EXCLUDED.doc",
            (uid, "model_api_runtime", psycopg.types.json.Jsonb({"sibling": "kept"})),
        )

    from tee_shadow import mirror

    original_execute = mirror.execute
    old_mirror_started = threading.Event()
    release_old_mirror = threading.Event()

    def delayed_execute(sql, params=()):
        doc = params[2].obj if len(params) >= 3 and hasattr(params[2], "obj") else {}
        if doc.get(cursor.CURSOR_KEY) == 10:
            old_mirror_started.set()
            assert release_old_mirror.wait(timeout=3)
        return original_execute(sql, params)

    monkeypatch.setattr(mirror, "execute", delayed_execute)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        old = pool.submit(
            db.advance_blob_int_strict,
            uid,
            "model_api_runtime",
            cursor.CURSOR_KEY,
            10,
        )
        assert old_mirror_started.wait(timeout=3)
        new = pool.submit(
            db.advance_blob_int_strict,
            uid,
            "model_api_runtime",
            cursor.CURSOR_KEY,
            20,
        )
        new.result(timeout=3)
        release_old_mirror.set()
        old.result(timeout=3)
    finally:
        release_old_mirror.set()
        pool.shutdown(wait=True, cancel_futures=True)

    primary = db.get_blob_strict(uid, "model_api_runtime")
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        shadow = conn.execute(
            "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s",
            (uid, "model_api_runtime"),
        ).fetchone()[0]
    assert primary[cursor.CURSOR_KEY] == 20
    assert primary[db._BLOB_REVISION_KEY] == 2
    assert shadow == primary


def test_colliding_user_row_cannot_advance_reply_cursor():
    uid = "u_atomic_reply_collision"
    conftest.seed_user(uid)
    _reset(uid)
    collision_id = "c" * 32
    db.chat_append_strict(
        uid,
        collision_id,
        1.0,
        {
            "id": collision_id,
            "role": "user",
            "source": "model_api",
            "body_ct": "attacker-ciphertext",
            "nonce": "attacker-nonce",
            "K_user": "attacker-key",
        },
        5000,
    )

    with pytest.raises(RuntimeError, match="reply id collision"):
        db.chat_append_effect_with_cursor(
            uid,
            collision_id,
            2.0,
            {
                "id": collision_id,
                "role": "openclaw",
                "source": "model_api",
                "v": 1,
                "body_ct": "real-reply-ciphertext",
                "nonce": "real-reply-nonce",
                "K_user": "real-reply-key",
                "K_enclave": "real-enclave-key",
                "enclave_pk_fpr": "",
                "visibility": "shared",
                "owner_user_id": uid,
                "content_type": "text",
            },
            5000,
            99,
        )

    assert cursor.load_seq(core_store.get_store(uid)) == 0
    with db.get_pool().connection() as conn:
        role = conn.execute(
            "SELECT doc->>'role' FROM chat_messages "
            "WHERE user_id=%s AND msg_id=%s",
            (uid, collision_id),
        ).fetchone()[0]
    assert role == "user"


def test_atomic_reply_retains_source_history_without_tee_eviction(monkeypatch):
    """Reply+cursor commit must never turn prompt coverage into history GC."""
    uid = "u_atomic_reply_tee_trim"
    conftest.seed_user(uid)
    _reset(uid)

    for index in range(1, 4):
        db.chat_append(
            uid,
            f"old-{index}",
            float(index),
            {"id": f"old-{index}", "role": "user", "content": str(index)},
            0,
        )
    old_two_seq = db.chat_seq_for_msg_id(uid, "old-2")
    newest_input_seq = db.chat_seq_for_msg_id(uid, "old-3")
    assert old_two_seq is not None and newest_input_seq is not None
    assert jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"body_ct": "summary"},
        watermark_ts=2.0,
        watermark_seq=old_two_seq,
        expected_version=0,
    )

    from tee_shadow import mirror

    mirrored: list[list[tuple[str, tuple]]] = []
    monkeypatch.setattr(mirror, "execute_many", lambda statements: mirrored.append(statements))

    db.chat_append_effect_with_cursor(
        uid,
        "reply-row",
        4.0,
        {"id": "reply-row", "role": "openclaw", "body_ct": "ciphertext"},
        2,
        newest_input_seq,
    )

    with db.get_pool().connection() as conn:
        remaining = {
            row[0]
            for row in conn.execute(
                "SELECT msg_id FROM chat_messages WHERE user_id=%s", (uid,)
            ).fetchall()
        }
    assert remaining == {"old-1", "old-2", "old-3", "reply-row"}
    assert mirrored == []


def test_reply_effect_payload_contains_ciphertext_not_model_text(monkeypatch):
    uid = "u_atomic_reply_privacy"
    conftest.seed_user(uid)
    _reset(uid)
    store = core_store.get_store(uid)

    def _build(_store, plaintext, *, item_id=None):
        assert plaintext == b"plaintext-secret-marker"
        return _envelope(str(item_id), body="opaque-ciphertext"), ""

    monkeypatch.setattr(worker.core_envelope, "_build_shared_envelope_for_store", _build)
    payload = worker._build_encrypted_reply_effect_payload(
        store,
        "plaintext-secret-marker",
        effect_id="job7:reply:0",
        reply_through_seq=9,
    )

    serialized = json.dumps(payload, sort_keys=True)
    assert "plaintext-secret-marker" not in serialized
    assert "opaque-ciphertext" in serialized
    assert len(payload["envelope"]["id"]) == 32
    assert payload["reply_through_seq"] == 9


def test_pending_final_reply_recovers_before_any_new_provider_call(monkeypatch):
    uid = "u_atomic_reply_recovery"
    conftest.seed_user(uid)
    _reset(uid)

    db.chat_append_strict(
        uid, "user-message", 100.0,
        {"id": "user-message", "role": "user", "ts": 100.0,
         "body_ct": "u", "nonce": "n", "K_user": "k", "K_enclave": "e"},
        5000,
    )
    input_seq = db.chat_messages_after_seq(uid, 0, limit=None)[0]["seq"]
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("recovery-worker")
    generation = db.get_runtime_generation(uid)
    effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type="reply",
        ordinal=0,
        expected_generation=generation,
        payload={
            "envelope": _envelope("b" * 32, body="recovered-ciphertext"),
            "reply_through_seq": input_seq,
            effect_outbox.FINAL_REPLY_FENCE_KEY: {
                "claimed_by": "recovery-worker",
                "input_generation": 0,
                "through_seq": input_seq,
            },
        },
    )

    provider_calls = []

    async def _provider(*args, **kwargs):
        provider_calls.append((args, kwargs))
        raise AssertionError("recovery must drain the old final reply before model work")

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)

    def _read_after_seq(_uid: str, after_seq: int):
        if after_seq >= input_seq:
            return []
        return [{"id": "user-message", "seq": input_seq, "ts": 100.0,
                 "role": "user", "content": "hello"}]

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        read_messages_after_seq=_read_after_seq,
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "rt",
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
    )

    status = asyncio.run(worker.process_job(
        job,
        deps,
        provider_config=_TEST_PROVIDER_CONFIG,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    assert provider_calls == []
    assert cursor.load_seq(core_store.get_store(uid)) == input_seq
    with db.get_pool().connection() as conn:
        replies = conn.execute(
            "SELECT COUNT(*) FROM chat_messages "
            "WHERE user_id=%s AND doc->>'role'='openclaw'",
            (uid,),
        ).fetchone()[0]
        effect_status = conn.execute(
            "SELECT status FROM v2_effect_outbox "
            "WHERE user_id=%s AND effect_type='reply'",
            (uid,),
        ).fetchone()[0]
    assert replies == 1
    assert effect_status == "applied"


def test_final_reply_effect_surfaces_sealed_thinking(monkeypatch):
    """A reply effect whose payload carries a sealed ``thinking`` sub-envelope
    lands its provider chain-of-thought on the same chat row (thinking_body_ct +
    thinking_kind), through the real production seq-native reply sink."""
    uid = "u_atomic_reply_thinking"
    conftest.seed_user(uid)
    _reset(uid)

    db.chat_append_strict(
        uid, "user-message", 100.0,
        {"id": "user-message", "role": "user", "ts": 100.0,
         "body_ct": "u", "nonce": "n", "K_user": "k", "K_enclave": "e"},
        5000,
    )
    input_seq = db.chat_messages_after_seq(uid, 0, limit=None)[0]["seq"]
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("thinking-worker")
    generation = db.get_runtime_generation(uid)
    effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type="reply",
        ordinal=0,
        expected_generation=generation,
        payload={
            "envelope": _envelope("b" * 32, body="reply-ciphertext"),
            "reply_through_seq": input_seq,
            "thinking": {
                "envelope": _envelope("c" * 32, body="thinking-ciphertext"),
                "metadata": {
                    "thinking_kind": "provider_reasoning",
                    "thinking_source": "v2.deepseek",
                    "thinking_model": "deepseek-v4-pro",
                    "thinking_native": True,
                },
            },
            effect_outbox.FINAL_REPLY_FENCE_KEY: {
                "claimed_by": "thinking-worker",
                "input_generation": 0,
                "through_seq": input_seq,
            },
        },
    )

    async def _provider(*args, **kwargs):
        raise AssertionError("recovery must drain the pending reply before model work")

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)

    def _read_after_seq(_uid: str, after_seq: int):
        if after_seq >= input_seq:
            return []
        return [{"id": "user-message", "seq": input_seq, "ts": 100.0,
                 "role": "user", "content": "hello"}]

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        read_messages_after_seq=_read_after_seq,
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "rt",
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_TEST_PROVIDER_CONFIG, api_key=None, runtime_token="rt",
    ))

    assert status == "completed"
    store = core_store.get_store(uid)
    store.reload()
    replies = [m for m in store.chat_messages if m.get("role") == "openclaw"]
    assert len(replies) == 1
    reply = replies[0]
    assert reply["body_ct"] == "reply-ciphertext"
    assert reply.get("thinking_body_ct") == "thinking-ciphertext"
    assert reply.get("thinking_kind") == "provider_reasoning"
    assert reply.get("thinking_model") == "deepseek-v4-pro"
    assert reply.get("thinking_native") is True


@pytest.mark.parametrize("advance_cursor_after_snapshot", [False, True])
def test_wake_yields_snapshot_race_input_to_chat_without_duplicate_reply(
    monkeypatch,
    advance_cursor_after_snapshot,
):
    """A send that lands after wake claim but before its tail snapshot is chat work.

    A concurrent compaction watermark can put the row only in the encrypted
    summary, so tail membership is not enough.  The durable reply cursor still
    proves it was unanswered: wake retires without calling the model and the
    pending chat lane consumes the row exactly once.
    """
    uid = (
        "u_atomic_wake_presnapshot_yield_"
        f"{int(bool(advance_cursor_after_snapshot))}"
    )
    conftest.seed_user(uid)
    _reset(uid)

    wake_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    wake_job = jobs_store.claim_next_job("wake-snapshot-worker")
    assert wake_job is not None and wake_job["id"] == wake_id

    plaintext: dict[str, str] = {}
    input_seq_box = {"value": 0}

    def _append_racing_send() -> None:
        if input_seq_box["value"]:
            return
        message_id = "wake-presnapshot-user"
        plaintext[message_id] = "answer this in the chat lane"
        db.chat_append_strict(
            uid,
            message_id,
            100.0,
            {
                "id": message_id,
                "role": "user",
                "ts": 100.0,
                "body_ct": "cipher-user",
                "nonce": "n",
                "K_user": "k",
                "K_enclave": "e",
            },
            5000,
        )
        input_seq_box["value"] = int(db.chat_seq_for_msg_id(uid, message_id))
        # Production chat/send persists the row and this job atomically.  The
        # test hook performs both synchronously at the same race boundary.
        jobs_store.enqueue_job(uid, "chat")

    def _summary_with_send_race(_uid: str):
        _append_racing_send()
        return (
            "- user: answer this in the chat lane",
            100.0,
            1,
            input_seq_box["value"],
        )

    def _plain_rows(after_seq: int, *, limit=None, oldest_first=True, through_seq=None):
        rows = db.chat_messages_after_seq(
            uid,
            after_seq,
            limit=limit,
            oldest_first=oldest_first,
            through_seq=through_seq,
        )
        return [
            {
                "id": row["id"],
                "seq": row["seq"],
                "ts": row["ts"],
                "role": row.get("role", "user"),
                "content": plaintext.get(row["id"], "[assistant reply]"),
            }
            for row in rows
        ]

    def _messages_after(_uid: str, after_seq: int):
        return [
            row
            for row in _plain_rows(after_seq)
            if row["role"] in {"user", "human"}
        ]

    def _tail_after(_uid: str, after_seq: int, limit: int, *, through_seq=None):
        return _plain_rows(
            after_seq,
            limit=limit,
            oldest_first=False,
            through_seq=through_seq,
        )

    provider_calls: list[list[dict]] = []

    async def _provider(_config, messages, *, tools=None):
        provider_calls.append(messages)
        return {"reply": "chat answered once", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    monkeypatch.setattr(
        worker,
        "_perception_grounding_results",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=None),
    )
    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _text, *, item_id=None: (_envelope(str(item_id)), ""),
    )
    if advance_cursor_after_snapshot:
        original_assert = worker._assert_prompt_tail_exact
        advanced = {"done": False}

        async def _assert_then_finish_concurrent_chat(*args, **kwargs):
            await original_assert(*args, **kwargs)
            if advanced["done"]:
                return
            advanced["done"] = True
            # Model a final-effect recovery that commits immediately after the
            # wake froze its prompt.  Wake must retain its earlier cursor read;
            # reloading the advanced cursor here would let it reply from a
            # snapshot that omitted this assistant row.
            db.chat_append_strict(
                uid,
                "concurrent-chat-reply",
                101.0,
                {
                    "id": "concurrent-chat-reply",
                    "role": "openclaw",
                    "ts": 101.0,
                    "body_ct": "cipher-reply",
                    "nonce": "n",
                    "K_user": "k",
                    "K_enclave": "e",
                },
                5000,
            )
            db.patch_blob(
                uid,
                "model_api_runtime",
                {"v2_reply_cursor_seq": input_seq_box["value"]},
            )

        monkeypatch.setattr(
            worker,
            "_assert_prompt_tail_exact",
            _assert_then_finish_concurrent_chat,
        )

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        read_messages_after_seq=_messages_after,
        read_tail_after_seq=_tail_after,
        read_compaction_tail_after_seq=_tail_after,
        read_summary_with_seq=_summary_with_send_race,
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "rt",
        write_summary=lambda *_args: True,
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
    )

    assert asyncio.run(
        worker.process_job(
            wake_job,
            deps,
            provider_config=_TEST_PROVIDER_CONFIG,
            api_key=None,
            runtime_token="rt",
        )
    ) == "completed"
    assert provider_calls == []
    assert cursor.load_seq(core_store.get_store(uid)) == (
        input_seq_box["value"] if advance_cursor_after_snapshot else 0
    )
    assert jobs_store.get_job_status(
        wake_id,
        user_id=uid,
        claimed_by=str(wake_job["claimed_by"]),
    ) == "completed"

    chat_job = jobs_store.claim_next_job("chat-after-wake-yield")
    assert chat_job is not None and chat_job["lane"] == "chat"
    assert asyncio.run(
        worker.process_job(
            chat_job,
            deps,
            provider_config=_TEST_PROVIDER_CONFIG,
            api_key=None,
            runtime_token="rt",
        )
    ) == "completed"

    input_seq = input_seq_box["value"]
    assert input_seq > 0
    assert cursor.load_seq(core_store.get_store(uid)) == input_seq
    if advance_cursor_after_snapshot:
        assert provider_calls == []
    else:
        assert len(provider_calls) == 1
        assert sum(
            "answer this in the chat lane" in str(message.get("content") or "")
            for message in provider_calls[0]
            if isinstance(message, dict)
        ) == 1
    with db.get_pool().connection() as conn:
        reply_count = conn.execute(
            "SELECT COUNT(*) FROM chat_messages "
            "WHERE user_id=%s AND doc->>'role'='openclaw'",
            (uid,),
        ).fetchone()[0]
    assert reply_count == 1


def test_same_timestamp_initial_midturn_and_successor_inputs_are_consumed_once(monkeypatch):
    uid = "u_atomic_same_timestamp"
    conftest.seed_user(uid)
    _reset(uid)

    plaintext: dict[str, str] = {}

    def _append_user(mid: str, text: str) -> int:
        plaintext[mid] = text
        db.chat_append_strict(
            uid, mid, 777.0,
            {"id": mid, "role": "user", "ts": 777.0,
             "body_ct": f"cipher-{mid}", "nonce": "n", "K_user": "k",
             "K_enclave": "e"},
            5000,
        )
        return db.chat_seq_for_msg_id(uid, mid)

    seq1 = _append_user("m1", "first")
    seq2 = _append_user("m2", "second")

    def _plain_rows(after_seq: int, *, limit=None, oldest_first=True, through_seq=None):
        rows = db.chat_messages_after_seq(
            uid, after_seq, limit=limit, oldest_first=oldest_first,
            through_seq=through_seq,
        )
        return [{
            "id": row["id"], "seq": row["seq"], "ts": row["ts"],
            "role": row.get("role", "user"),
            "content": plaintext.get(row["id"], "[assistant reply]"),
        } for row in rows]

    def _messages_after(_uid: str, after_seq: int):
        return [row for row in _plain_rows(after_seq) if row["role"] == "user"]

    def _tail_after(_uid: str, after_seq: int, limit: int, *, through_seq=None):
        return _plain_rows(
            after_seq, limit=limit, oldest_first=False, through_seq=through_seq)

    def _compact_after(_uid: str, after_seq: int, limit: int, *, through_seq=None):
        return _plain_rows(
            after_seq, limit=limit, oldest_first=True, through_seq=through_seq)

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _text, *, item_id=None: (_envelope(str(item_id)), ""),
    )

    class _CapResult:
        def to_dict(self):
            return {"ok": True, "data": {"found": True}, "error": None,
                    "trace": {}, "warnings": []}

    seq3_box = {"value": 0}
    seq4_box = {"value": 0}

    def _summary_with_snapshot_race(_uid: str):
        # Arrives after the initial coalesce but before chat_max_seq/tail are
        # snapped. It belongs in the base tail and must not be folded a second
        # time at the first provider boundary.
        if not seq3_box["value"]:
            seq3_box["value"] = _append_user("m3", "third-snapshot-race")
        return "", 0.0, 0, 0

    def _capability(*args, **kwargs):
        if not seq4_box["value"]:
            seq4_box["value"] = _append_user("m4", "fourth-midturn")
        return _CapResult()

    monkeypatch.setattr(cap_registry, "run_capability", _capability)

    provider_calls = []
    scripted = iter([
        {"reply": "", "tool_calls": [{"id": "c1", "name": "memory_search",
                                         "args": {"query": "third"}}], "usage": {}},
        {"reply": "answered-three", "tool_calls": [], "usage": {}},
    ])

    async def _provider(config, messages, *, tools=None):
        provider_calls.append(messages)
        return next(scripted)

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)

    def _apply(user_id: str):
        return serve_worker._apply_pending_effects_for_user(user_id)

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        read_messages_after_seq=_messages_after,
        read_tail_after_seq=_tail_after,
        read_compaction_tail_after_seq=_compact_after,
        read_summary_with_seq=_summary_with_snapshot_race,
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "rt",
        write_summary=lambda *_args: True,
        apply_pending_effects=_apply,
    )

    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("same-ts-worker")
    assert asyncio.run(worker.process_job(
        job, deps, provider_config=_TEST_PROVIDER_CONFIG,
        api_key=None, runtime_token="rt",
    )) == "completed"

    seq3 = seq3_box["value"]
    seq4 = seq4_box["value"]
    assert seq1 < seq2 < seq3 < seq4
    assert cursor.load_seq(core_store.get_store(uid)) == seq4
    first_text = [
        m.get("content") for m in provider_calls[0]
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert first_text.count("third-snapshot-race") == 1
    second_transcript = [m for m in provider_calls[1] if isinstance(m, dict)]
    assert sum(m.get("content") == "fourth-midturn" for m in second_transcript) == 1
    assert any(isinstance(m, ToolExchange) for m in provider_calls[1])

    # A successor with no new seq is empty and never calls the provider.
    async def _unexpected_provider(*args, **kwargs):
        raise AssertionError("already-consumed same-ts inputs must not replay")

    monkeypatch.setattr(provider_client, "chat_completion_async", _unexpected_provider)
    jobs_store.enqueue_job(uid, "chat")
    empty_job = jobs_store.claim_next_job("same-ts-empty")
    assert asyncio.run(worker.process_job(
        empty_job, deps, provider_config=_TEST_PROVIDER_CONFIG,
        api_key=None, runtime_token="rt",
    )) == "completed"

    # A fourth message with the identical timestamp is still picked up by seq.
    seq5 = _append_user("m5", "fifth-same-ts")
    calls_after = []

    async def _fourth_provider(config, messages, *, tools=None):
        calls_after.append(messages)
        return {"reply": "answered-fourth", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(provider_client, "chat_completion_async", _fourth_provider)
    jobs_store.enqueue_job(uid, "chat")
    fourth_job = jobs_store.claim_next_job("same-ts-fourth")
    assert asyncio.run(worker.process_job(
        fourth_job, deps, provider_config=_TEST_PROVIDER_CONFIG,
        api_key=None, runtime_token="rt",
    )) == "completed"
    assert calls_after
    assert cursor.load_seq(core_store.get_store(uid)) == seq5


def test_recovery_drained_reply_is_not_pushed(monkeypatch):
    """上个进程崩溃遗留的 effect 由回合开头的 recovery drain 落库 —— 它不经
    `_on_reply`，没有明文也不写槽位，所以不推送。消息照常落库。"""
    uid = "u_atomic_reply_push_recovery"
    conftest.seed_user(uid)
    _reset(uid)

    db.chat_append_strict(
        uid, "user-message", 100.0,
        {"id": "user-message", "role": "user", "ts": 100.0,
         "body_ct": "u", "nonce": "n", "K_user": "k", "K_enclave": "e"},
        5000,
    )
    input_seq = db.chat_messages_after_seq(uid, 0, limit=None)[0]["seq"]
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("push-recovery-worker")
    generation = db.get_runtime_generation(uid)
    effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type="reply",
        ordinal=0,
        expected_generation=generation,
        payload={
            "envelope": _envelope("b" * 32, body="reply-ciphertext"),
            "reply_through_seq": input_seq,
            effect_outbox.FINAL_REPLY_FENCE_KEY: {
                "claimed_by": "push-recovery-worker",
                "input_generation": 0,
                "through_seq": input_seq,
            },
        },
    )

    async def _provider(*args, **kwargs):
        raise AssertionError("recovery must drain the pending reply before model work")

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)

    def _read_after_seq(_uid: str, after_seq: int):
        if after_seq >= input_seq:
            return []
        return [{"id": "user-message", "seq": input_seq, "ts": 100.0,
                 "role": "user", "content": "hello"}]

    pushes = []
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        read_messages_after_seq=_read_after_seq,
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "rt",
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
        send_reply_push=lambda uid, **kw: pushes.append((uid, kw)),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_TEST_PROVIDER_CONFIG,
        api_key=None, runtime_token="rt",
    ))

    assert status == "completed"
    assert pushes == []
    store = core_store.get_store(uid)
    store.reload()
    replies = [m for m in store.chat_messages if m.get("role") == "openclaw"]
    assert len(replies) == 1


def test_push_transport_failure_does_not_fail_the_turn(monkeypatch):
    """推送实现抛异常 —— 回合仍然 completed，回复仍然在库里，且推送确实被调用过。

    走的是正常投递路径（provider 真跑一轮 -> `_on_reply` 的 `status=="applied"`
    分支写 `push_slot` -> `finally` 里真的调用 `deps.send_reply_push`），不是
    recovery-drain：后者在到达 `_on_reply` 之前就已经 `return "completed"`，
    `push_slot` 永远是 `None`，`_boom` 根本不会被调用（见本文件同名测试上一版
    的复核发现——即使把 `finally` 里保护推送的 try/except 整段删掉也依然全绿，
    没有判别力）。
    """
    uid = "u_atomic_reply_push_boom"
    conftest.seed_user(uid)
    _reset(uid)

    db.chat_append_strict(
        uid, "user-message", 100.0,
        {"id": "user-message", "role": "user", "ts": 100.0,
         "body_ct": "u", "nonce": "n", "K_user": "k", "K_enclave": "e"},
        5000,
    )
    input_seq = db.chat_messages_after_seq(uid, 0, limit=None)[0]["seq"]

    # Real production sink (serve_worker._apply_pending_effects_for_user) needs
    # a real-shaped envelope with an "id" out of the enclave round-trip; stub
    # only that boundary (same technique as
    # test_same_timestamp_initial_midturn_and_successor_inputs_are_consumed_once
    # above), so the reply effect's payload actually carries "envelope"/"id"
    # and _on_reply's push_slot write hits the real code path, not a mock gap.
    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _text, *, item_id=None: (_envelope(str(item_id)), ""),
    )

    # Deliberately longer than the push body's 240-char cap (see `_on_reply`'s
    # `text[:240]` truncation), so the body assertion below can't pass on a
    # copy-paste bug that pushes the full untruncated text.
    reply_text = "answered-boom-" + ("x" * 300)
    assert len(reply_text) > 240

    async def _provider(config, messages, *, tools=None):
        return {"reply": reply_text, "tool_calls": [], "usage": {}}

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)

    def _read_after_seq(_uid: str, after_seq: int):
        if after_seq >= input_seq:
            return []
        return [{"id": "user-message", "seq": input_seq, "ts": 100.0,
                 "role": "user", "content": "hello"}]

    boom_calls = []

    def _boom(_uid, **kw):
        boom_calls.append((_uid, kw))
        raise RuntimeError("apns down")

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        read_messages_after_seq=_read_after_seq,
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "rt",
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
        send_reply_push=_boom,
    )

    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("push-boom-worker")

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_TEST_PROVIDER_CONFIG,
        api_key=None, runtime_token="rt",
    ))

    assert status == "completed"
    assert boom_calls, "send_reply_push must be invoked on the normal delivery path"
    store = core_store.get_store(uid)
    store.reload()
    replies = [m for m in store.chat_messages if m.get("role") == "openclaw"]
    assert replies

    # Parameter assertions (review Minor #2): the earlier version of this test
    # only checked that `send_reply_push` was *called*, which would not have
    # caught a copy-paste bug (e.g. the wake-lane block's `is_wake` literal
    # being copied as `False`) — it must also carry the right msg_id/body/
    # is_wake for the chat lane.
    _, kw = boom_calls[0]
    assert kw["msg_id"] == replies[0]["id"], (
        "pushed msg_id must be the envelope id of the row that was actually "
        "persisted, not some other identifier"
    )
    assert kw["body"] == reply_text[:240]
    assert len(kw["body"]) == 240
    assert kw["is_wake"] is False
    assert kw["lane"] == "", "chat lane must never claim a wake lane name"
