"""Chat-history clear is a generation-fenced live-context boundary.

It is intentionally narrower than account deletion: encrypted trajectory
telemetry remains available for audited debugging.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
from pathlib import Path
import sys
import threading

import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from conftest import seed_user, set_v2_runtime_owner
from model_api_runtime.v2 import effect_id, effect_outbox, jobs_store
from proactive import capture_scheduler


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="chat-clear fencing tests require PostgreSQL",
)


@pytest.fixture(autouse=True)
def _clean_v2_queue_state():
    # claim_next_job is fleet-global; isolate this concurrency module from its
    # own post-clear successor job and from earlier suite modules.
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_effect_sink_applied,v2_effect_outbox,agent_jobs,users CASCADE"
        )
    yield


def _envelope(user_id: str, item_id: str, body: str = "ciphertext") -> dict:
    return {
        "v": 1,
        "id": item_id,
        "owner_user_id": user_id,
        "visibility": "shared",
        "body_ct": base64.b64encode(body.encode()).decode(),
        "nonce": "nonce",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
    }


def _append_user_message(user_id: str, message_id: str, ts: float = 10.0) -> int:
    db.chat_append_strict(
        user_id,
        message_id,
        ts,
        {
            "id": message_id,
            "role": "user",
            "source": "chat",
            "ts": ts,
            "body_ct": "opaque-chat-ciphertext",
            "nonce": "nonce",
            "K_user": "wrapped-user-key",
            "K_enclave": "wrapped-enclave-key",
        },
        5000,
    )
    return int(db.chat_seq_for_msg_id(user_id, message_id))


def test_delayed_pre_clear_capture_refresh_cannot_recreate_state():
    uid = "u_clear_capture_refresh"
    seed_user(uid)
    _append_user_message(uid, "before-clear", ts=10.0)

    class StaleStore:
        user_id = uid
        chat_lock = threading.RLock()
        chat_messages = [
            {
                "id": "before-clear",
                "role": "user",
                "source": "chat",
                "ts": 10.0,
            }
        ]

    store = StaleStore()
    before = capture_scheduler.refresh_capture_state_from_chat(store, now=11.0)
    assert before["last_seen_message_id"] == "before-clear"
    assert db.chat_clear(uid) == 1
    assert db.get_blob_strict(uid, "capture_state") is None

    # This callback retained the pre-clear in-process message list. The shared
    # fence + source-row witness must observe that Clear already removed it and
    # leave capture_state absent in both primary and mirror ordering.
    after = capture_scheduler.refresh_capture_state_from_chat(store, now=12.0)
    assert after["last_seen_message_id"] == ""
    assert db.get_blob_strict(uid, "capture_state") is None


def _running_job(user_id: str, owner: str = "clear-test-worker") -> tuple[int, int]:
    generation = db.get_runtime_generation(user_id)
    job_id, coalesced = jobs_store.enqueue_job(
        user_id,
        "chat",
        expected_generation=generation,
    )
    assert coalesced is False
    claimed = jobs_store.claim_next_job(owner, lanes={"chat"})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=owner)
    return job_id, generation


def test_clear_atomically_removes_live_chat_context_but_retains_independent_state(
    monkeypatch,
):
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED", "1")
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE", "64")
    from tee_shadow import mirror

    mirrored_deletes = []
    original_execute_many = mirror.execute_many

    def _capture_mirror(statements):
        mirrored_deletes.extend(statements)
        return original_execute_many(statements)

    monkeypatch.setattr(mirror, "execute_many", _capture_mirror)
    uid = "u_v2_clear_atomic_context"
    seed_user(uid)
    set_v2_runtime_owner(uid, generation=7)
    seq = _append_user_message(uid, "pre-clear-message")
    job_id, old_generation = _running_job(uid)
    assert old_generation == 7

    assert jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"body_ct": "encrypted-summary"},
        watermark_ts=10.0,
        expected_version=0,
        watermark_seq=seq,
        require_source_row=True,
    )
    assert jobs_store.seed_legacy_summary_segment(
        uid,
        expected_version=1,
        translated_watermark_seq=seq,
    )
    artifact = jobs_store.put_workspace_entry_cas(
        uid,
        "/artifacts/pre-clear.txt",
        kind="artifact",
        content_envelope={"body_ct": "encrypted-artifact"},
        mime_type="text/plain",
        source_ref="pre-clear-message",
        expected_revision=0,
    )
    assert artifact is not None
    with db.get_pool().connection() as conn:
        for path, kind in (
            ("/workspace/keep.md", "workspace"),
            ("/memory/WORKING.md", "working_memory"),
            ("/skills/keep.md", "skill"),
        ):
            conn.execute(
                "INSERT INTO v2_workspace_entries "
                "(user_id,path,kind,content_envelope,mime_type,source_ref,revision) "
                "VALUES (%s,%s,%s,%s,'text/markdown','',1)",
                (uid, path, kind, Jsonb({"body_ct": f"encrypted:{path}"})),
            )

    jobs_store.append_status_event(uid, "processing", job_id=job_id)
    jobs_store.upsert_runtime_state(
        uid,
        {"last_replied_ts": 10.0, "action_digest": {"reply": {"ok": 1}}},
        source_job_id=job_id,
    )
    jobs_store.append_trajectory_event(
        job_id,
        uid,
        event_kind="provider_request",
        idempotency_key="attempt0.provider_request",
        payload_envelope=_envelope(uid, "trajectory-before-clear", "private turn"),
        payload_bytes=100,
    )
    # Live effect/action/recovery state is tied to this source job and must be
    # erased even though its historical job/trajectory rows survive.
    active_job_id = job_id
    active_generation = old_generation
    parent_effect_id = effect_id.derive(
        job_id=active_job_id,
        effect_type="workspace_batch_encrypted_v1",
        ordinal=0,
    )
    assert db.effect_enqueue(
        parent_effect_id,
        uid,
        active_job_id,
        "workspace_batch_encrypted_v1",
        active_generation,
        {"effect_envelope": {"body_ct": "encrypted-effect"}},
        input_frontier_seq=seq,
    )
    child_effect_id = effect_id.derive_batch_item(
        parent_effect_id=parent_effect_id,
        ordinal=0,
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_effect_sink_applied "
            "(effect_id,claim_state) VALUES (%s,'completed')",
            (parent_effect_id,),
        )
        conn.execute(
            "INSERT INTO v2_effect_sink_applied "
            "(effect_id,claim_state) VALUES (%s,'completed')",
            (child_effect_id,),
        )
        conn.execute(
            "INSERT INTO agent_action_queue "
            "(job_id,user_id,seq,type,payload_json,status) "
            "VALUES (%s,%s,0,'legacy','{}'::jsonb,'pending')",
            (active_job_id, uid),
        )
        conn.execute(
            "INSERT INTO v2_mcp_mutation_attempts "
            "(job_id,user_id,input_frontier_seq,call_key,tool_fingerprint) "
            "VALUES (%s,%s,%s,%s,%s)",
            (active_job_id, uid, seq, "a" * 64, "b" * 64),
        )

    # Queue a review to prove that encrypted historical state survives while
    # active review execution is stopped by clear.
    assert jobs_store.mark_failed(
        job_id,
        "turn_failed:providererror",
        claimed_by="clear-test-worker",
    )
    review = jobs_store.get_failure_review(job_id, uid)
    assert review is not None and review["status"] == "pending"

    review_runner = jobs_store.claim_next_job(
        "review-worker",
        lanes={"trajectory_review"},
    )
    assert review_runner is not None
    assert jobs_store.mark_running(
        review_runner["id"], claimed_by="review-worker"
    )
    assert jobs_store.claim_failure_review(
        uid,
        runner_job_id=review_runner["id"],
        claimed_by="review-worker",
    )

    db.memory_upsert(
        uid,
        "memory-keep",
        "2026-07-19T00:00:00Z",
        {"id": "memory-keep", "content": "independent encrypted memory"},
    )
    db.set_blob(uid, "identity", {"body_ct": "encrypted-identity"})
    db.set_blob(
        uid,
        "model_api_runtime",
        {
            "hosted_runtime_mode": "db_action_v2",
            "v2_reply_cursor_seq": seq,
            "provider_profile": "keep-this-config",
        },
    )
    jobs_store.upsert_wake_schedule(uid, next_heartbeat_at=2_000_000_000.0)
    metric_id = jobs_store.record_sandbox_acquisition(
        uid,
        provider="memory-test",
        purpose="materialize_artifact",
    )
    jobs_store.record_turn_metric(
        job_id=active_job_id,
        user_id=uid,
        lane="chat",
        prompt_tokens=10,
        completion_tokens=2,
        latency_ms=20,
    )
    capture_job_id, capture_coalesced = jobs_store.enqueue_job(uid, "capture")
    assert not capture_coalesced
    db.set_blob(
        uid,
        "capture_state",
        {
            "last_captured_until_seq": 0,
            "capture_seq_initialized": True,
            "pending_capture_key": "capture:pre-clear",
        },
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_capture_batches "
            "(user_id,runtime_generation,after_seq,through_seq,"
            "until_message_id,actions_json,action_count,prepared_by_job_id) "
            "VALUES (%s,%s,0,%s,'pre-clear-message',%s,1,%s)",
            (
                uid,
                old_generation,
                seq,
                Jsonb([{"envelope": {"body_ct": "encrypted-capture-card"}}]),
                capture_job_id,
            ),
        )

    assert db.chat_clear(uid) == 1
    assert db.get_runtime_generation(uid) == old_generation + 1

    with db.get_pool().connection() as conn:
        counts = {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id=%s", (uid,)
            ).fetchone()[0]
            for table in (
                "chat_messages",
                "v2_conversation_summary",
                "v2_conversation_summary_segments",
                "agent_status_events",
                "runtime_state",
                "v2_effect_outbox",
                "v2_mcp_mutation_attempts",
                "v2_capture_batches",
            )
        }
        counts["capture_state"] = conn.execute(
            "SELECT COUNT(*) FROM user_blobs "
            "WHERE user_id=%s AND kind='capture_state'",
            (uid,),
        ).fetchone()[0]
        archived_chat = conn.execute(
            "SELECT source_seq,msg_id,doc,clear_generation "
            "FROM chat_message_archive WHERE user_id=%s ORDER BY source_seq",
            (uid,),
        ).fetchall()
        workspace = conn.execute(
            "SELECT path,kind FROM v2_workspace_entries "
            "WHERE user_id=%s ORDER BY path",
            (uid,),
        ).fetchall()
        jobs = conn.execute(
            "SELECT id,status,last_error FROM agent_jobs "
            "WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
        trajectories = {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id=%s", (uid,)
            ).fetchone()[0]
            for table in (
                "v2_trajectory_streams",
                "v2_trajectory_events",
                "v2_trajectory_reviews",
            )
        }
        retained_review = conn.execute(
            "SELECT status,last_error FROM v2_trajectory_reviews "
            "WHERE source_job_id=%s",
            (job_id,),
        ).fetchone()
        sink_markers = conn.execute(
            "SELECT COUNT(*) FROM v2_effect_sink_applied "
            "WHERE effect_id IN (%s,%s)",
            (parent_effect_id, child_effect_id),
        ).fetchone()[0]
        action_rows = conn.execute(
            "SELECT COUNT(*) FROM agent_action_queue WHERE user_id=%s",
            (uid,),
        ).fetchone()[0]
        preserved = {
            "memory": conn.execute(
                "SELECT COUNT(*) FROM memory_moments WHERE user_id=%s", (uid,)
            ).fetchone()[0],
            "identity": conn.execute(
                "SELECT COUNT(*) FROM user_blobs "
                "WHERE user_id=%s AND kind='identity'",
                (uid,),
            ).fetchone()[0],
            "schedule": conn.execute(
                "SELECT COUNT(*) FROM v2_wake_schedule WHERE user_id=%s", (uid,)
            ).fetchone()[0],
            "sandbox": conn.execute(
                "SELECT COUNT(*) FROM v2_sandbox_usage_events "
                "WHERE user_id=%s AND id=%s",
                (uid, metric_id),
            ).fetchone()[0],
            "tokens": conn.execute(
                "SELECT COUNT(*) FROM v2_turn_metrics WHERE user_id=%s", (uid,)
            ).fetchone()[0],
        }

    assert counts == {name: 0 for name in counts}
    assert len(archived_chat) == 1
    assert archived_chat[0][0] == seq
    assert archived_chat[0][1] == "pre-clear-message"
    assert archived_chat[0][2]["body_ct"] == "opaque-chat-ciphertext"
    assert archived_chat[0][3] == old_generation + 1
    assert any(
        "kind = 'capture_state'" in sql for sql, _params in mirrored_deletes
    )
    assert workspace == [
        ("/memory/WORKING.md", "working_memory"),
        ("/skills/keep.md", "skill"),
        ("/workspace/keep.md", "workspace"),
    ]
    assert jobs and all(row[1] not in {"pending", "claimed", "running"} for row in jobs)
    assert trajectories == {
        "v2_trajectory_streams": 1,
        "v2_trajectory_events": 1,
        "v2_trajectory_reviews": 1,
    }
    assert retained_review == ("failed", "chat_history_cleared")
    assert sink_markers == 0
    assert action_rows == 0
    assert preserved == {key: 1 for key in preserved}
    runtime_config = db.get_blob_strict(uid, "model_api_runtime")
    assert runtime_config["hosted_runtime_mode"] == "db_action_v2"
    assert runtime_config["provider_profile"] == "keep-this-config"
    assert runtime_config["_rds_revision"] == 1
    assert "v2_reply_cursor_seq" not in runtime_config

    # A clean post-clear conversation starts on the new runtime generation and
    # is not blocked by any old single-flight/recovery state.
    new_generation = db.get_runtime_generation(uid)
    new_seq, new_job_id = db.chat_append_and_enqueue(
        uid,
        "post-clear-message",
        20.0,
        {
            "id": "post-clear-message",
            "role": "user",
            "ts": 20.0,
            "body_ct": "new-ciphertext",
            "nonce": "nonce",
            "K_user": "wrapped-user-key",
            "K_enclave": "wrapped-enclave-key",
        },
        5000,
        "chat",
        expected_generation=new_generation,
    )
    assert new_seq > seq and new_job_id is not None
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT expected_runtime_generation,status FROM agent_jobs WHERE id=%s",
            (new_job_id,),
        ).fetchone() == (new_generation, "pending")


def test_clear_first_rejects_paused_summary_artifact_trajectory_and_status_writers(
    monkeypatch,
):
    uid = "u_v2_clear_wins_writer_races"
    seed_user(uid)
    set_v2_runtime_owner(uid, generation=11)
    seq = _append_user_message(uid, "stale-source")
    job_id, _generation = _running_job(uid)

    original_lock = db._lock_chat_user_fence_on_cursor
    clear_has_exclusive = threading.Event()
    release_clear = threading.Event()

    def pausing_lock(cur, user_id: str, *, exclusive: bool = False):
        original_lock(cur, user_id, exclusive=exclusive)
        if exclusive and str(user_id) == uid:
            clear_has_exclusive.set()
            assert release_clear.wait(timeout=5)

    monkeypatch.setattr(db, "_lock_chat_user_fence_on_cursor", pausing_lock)

    with ThreadPoolExecutor(max_workers=7) as pool:
        clear_future = pool.submit(db.chat_clear, uid)
        assert clear_has_exclusive.wait(timeout=3)
        summary_future = pool.submit(
            jobs_store.upsert_summary_row_cas,
            uid,
            summary_envelope={"body_ct": "stale-summary"},
            watermark_ts=10.0,
            expected_version=0,
            watermark_seq=seq,
            require_source_row=True,
        )
        artifact_future = pool.submit(
            jobs_store.put_workspace_entry_cas,
            uid,
            "/artifacts/stale.txt",
            kind="artifact",
            content_envelope={"body_ct": "stale-artifact"},
            mime_type="text/plain",
            source_ref="stale-source",
            expected_revision=0,
        )
        trajectory_future = pool.submit(
            jobs_store.append_trajectory_event,
            job_id,
            uid,
            event_kind="provider_response",
            idempotency_key="attempt0.late_response",
            payload_envelope=_envelope(uid, "stale-trajectory"),
            payload_bytes=100,
        )
        status_future = pool.submit(
            jobs_store.append_status_event,
            uid,
            "writing_reply",
            job_id=job_id,
        )
        runtime_future = pool.submit(
            jobs_store.upsert_runtime_state,
            uid,
            {"last_replied_ts": 10.0},
            source_job_id=job_id,
        )
        failure_future = pool.submit(
            jobs_store.ensure_terminal_failure_outbox,
            job_id,
            uid,
            "turn_failed:providererror",
        )
        for future in (
            summary_future,
            artifact_future,
            trajectory_future,
            status_future,
            runtime_future,
            failure_future,
        ):
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.1)
        release_clear.set()
        assert clear_future.result(timeout=5) == 1
        assert summary_future.result(timeout=5) is False
        assert artifact_future.result(timeout=5) is None
        with pytest.raises(ValueError, match="trajectory source job generation is stale"):
            trajectory_future.result(timeout=5)
        with pytest.raises(ValueError, match="status source job generation is stale"):
            status_future.result(timeout=5)
        assert runtime_future.result(timeout=5) is None
        assert failure_future.result(timeout=5) is False

    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v2_conversation_summary WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM v2_workspace_entries WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM v2_trajectory_events WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_status_events WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_state WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM v2_terminal_failure_outbox WHERE user_id=%s",
            (uid,),
        ).fetchone()[0] == 0


def test_onboarding_greeting_writer_first_linearizes_before_clear(monkeypatch):
    """The immutable greeting writer must hold clear's shared chat fence."""
    uid = "u_v2_onboarding_writer_before_clear"
    seed_user(uid)
    msg_id = "onboarding-greeting"
    greeting = {
        "id": msg_id,
        "role": "openclaw",
        "ts": 10.0,
        "body_ct": "encrypted-greeting",
        "model_api_kind": "onboarding_greeting",
    }

    original_lock = db._lock_chat_user_fence_on_cursor
    writer_has_shared = threading.Event()
    release_writer = threading.Event()
    paused_once = threading.Event()

    def pausing_lock(cur, user_id: str, *, exclusive: bool = False):
        original_lock(cur, user_id, exclusive=exclusive)
        if (
            not exclusive
            and str(user_id) == uid
            and threading.current_thread().name.startswith("greeting-writer")
            and not paused_once.is_set()
        ):
            paused_once.set()
            writer_has_shared.set()
            assert release_writer.wait(timeout=5)

    monkeypatch.setattr(db, "_lock_chat_user_fence_on_cursor", pausing_lock)

    with ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="greeting-writer",
    ) as pool:
        writer_future = pool.submit(
            db.chat_insert_onboarding_greeting_once,
            uid,
            msg_id,
            greeting["ts"],
            greeting,
        )
        assert writer_has_shared.wait(timeout=3)
        clear_future = pool.submit(db.chat_clear, uid)
        with pytest.raises(FutureTimeoutError):
            clear_future.result(timeout=0.2)
        release_writer.set()
        winner, inserted = writer_future.result(timeout=5)
        assert inserted is True and winner == greeting
        assert clear_future.result(timeout=5) == 1

    assert db.chat_onboarding_greeting_row(uid) is None


def test_onboarding_greeting_after_clear_keeps_storage_retention_generation():
    uid = "u_v2_onboarding_after_clear_generation"
    seed_user(uid)
    assert db.chat_clear(uid) == 0
    greeting = {
        "id": "post-clear-greeting",
        "role": "openclaw",
        "ts": 20.0,
        "body_ct": "encrypted-post-clear-greeting",
        "model_api_kind": "onboarding_greeting",
    }

    winner, inserted = db.chat_insert_onboarding_greeting_once(
        uid,
        greeting["id"],
        greeting["ts"],
        greeting,
    )
    assert inserted is True and winner == greeting
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT message.storage_generation,lifecycle.generation "
            "FROM chat_messages AS message "
            "JOIN chat_r2_lifecycle AS lifecycle USING (user_id) "
            "WHERE message.user_id=%s AND message.msg_id=%s",
            (uid, greeting["id"]),
        ).fetchone()
    assert row == (0, 0)


def test_reply_writer_first_linearizes_before_clear_and_clear_removes_reply(
    monkeypatch,
):
    """A shared reply writer may finish first; clear then removes its bubble."""
    uid = "u_v2_writer_wins_then_clear"
    seed_user(uid)
    set_v2_runtime_owner(uid, generation=21)
    seq = _append_user_message(uid, "source-before-clear")
    _job_id, _generation = _running_job(uid)

    original_lock = db._lock_chat_user_fence_on_cursor
    writer_has_shared = threading.Event()
    release_writer = threading.Event()
    paused_once = threading.Event()

    def pausing_lock(cur, user_id: str, *, exclusive: bool = False):
        original_lock(cur, user_id, exclusive=exclusive)
        if (
            not exclusive
            and str(user_id) == uid
            and threading.current_thread().name.startswith("preclear-writer")
            and not paused_once.is_set()
        ):
            paused_once.set()
            writer_has_shared.set()
            assert release_writer.wait(timeout=5)

    monkeypatch.setattr(db, "_lock_chat_user_fence_on_cursor", pausing_lock)

    def write_reply() -> None:
        db.chat_append_effect_with_cursor(
            uid,
            "reply-before-clear",
            11.0,
            {
                "id": "reply-before-clear",
                "role": "openclaw",
                "ts": 11.0,
                "body_ct": "encrypted-reply",
                "nonce": "nonce",
                "K_user": "wrapped-user-key",
                "K_enclave": "wrapped-enclave-key",
            },
            5000,
            seq,
        )

    with ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="preclear-writer",
    ) as pool:
        writer_future = pool.submit(write_reply)
        assert writer_has_shared.wait(timeout=3)
        clear_future = pool.submit(db.chat_clear, uid)
        with pytest.raises(FutureTimeoutError):
            clear_future.result(timeout=0.2)
        release_writer.set()
        writer_future.result(timeout=5)
        assert clear_future.result(timeout=5) == 2

    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        archived = conn.execute(
            "SELECT msg_id,doc FROM chat_message_archive "
            "WHERE user_id=%s ORDER BY source_seq",
            (uid,),
        ).fetchall()
    assert [row[0] for row in archived] == [
        "source-before-clear",
        "reply-before-clear",
    ]
    assert all(row[1].get("body_ct") for row in archived)


def test_pending_reply_apply_cannot_publish_after_clear_generation_bump(
    monkeypatch,
):
    uid = "u_v2_clear_pending_reply"
    seed_user(uid)
    set_v2_runtime_owner(uid, generation=31)
    seq = _append_user_message(uid, "reply-source")
    job_id, generation = _running_job(uid, owner="reply-worker")
    reply_effect_id = effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type=effect_outbox.FINAL_REPLY_EFFECT_TYPE,
        ordinal=0,
        expected_generation=generation,
        payload={
            "envelope": _envelope(uid, "f" * 32, "stale reply"),
            "reply_through_seq": seq,
            effect_outbox.FINAL_REPLY_FENCE_KEY: {
                "claimed_by": "reply-worker",
                "input_generation": 0,
                "through_seq": seq,
            },
        },
    )

    original_lock = db._lock_chat_user_fence_on_cursor
    clear_has_exclusive = threading.Event()
    release_clear = threading.Event()

    def pausing_lock(cur, user_id: str, *, exclusive: bool = False):
        original_lock(cur, user_id, exclusive=exclusive)
        if exclusive and str(user_id) == uid:
            clear_has_exclusive.set()
            assert release_clear.wait(timeout=5)

    monkeypatch.setattr(db, "_lock_chat_user_fence_on_cursor", pausing_lock)
    dispatched: list[str] = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        clear_future = pool.submit(db.chat_clear, uid)
        assert clear_has_exclusive.wait(timeout=3)
        apply_future = pool.submit(
            effect_outbox.apply_pending_effects,
            uid,
            dispatch=lambda _kind, _payload: dispatched.append("reply"),
        )
        with pytest.raises(FutureTimeoutError):
            apply_future.result(timeout=0.1)
        release_clear.set()
        assert clear_future.result(timeout=5) == 1
        assert apply_future.result(timeout=5) == {"applied": 0, "discarded": 0}

    assert dispatched == []
    assert effect_outbox.get_effect_disposition(
        reply_effect_id,
        user_id=uid,
        job_id=job_id,
        effect_type=effect_outbox.FINAL_REPLY_EFFECT_TYPE,
    ) is None
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
