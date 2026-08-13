"""db.chat_append_and_enqueue: atomic message-persist + job-enqueue (spec A7).

A crash between "write the user's chat message" and "enqueue its chat job"
today orphans the message (persisted, never processed -> no reply). This
module verifies both writes happen inside ONE transaction: a failure in the
job half rolls back the message half too, and a normal call persists both and
threads the generation through to the new job row.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from core import wake_bus
from model_api_runtime.v2 import jobs_store

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 send+enqueue atomicity tests require the PostgreSQL test fixture",
)


@pytest.fixture(autouse=True)
def pg_clean():
    """Truncate the tables this module's tests touch so rows from one test
    (or another module sharing the session-scoped DB) never leak into the
    next — mirrors test_v2_effect_outbox.py's pg_clean fixture."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_conversation_summary, chat_messages, "
            "agent_jobs, v2_runtime_state CASCADE"
        )
    yield


def _msg_doc(msg_id: str) -> dict:
    return {
        "id": msg_id,
        "role": "user",
        "ts": time.time(),
        "source": "model_api",
        "body_ct": "c", "nonce": "n", "K_user": "k",
        "content_type": "text",
    }


def _running_job(user_id: str, lane: str, worker_id: str) -> int:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'v2',1) ON CONFLICT (user_id) DO UPDATE SET "
            "hosted_runtime_state='v2'",
            (user_id,),
        )
    job_id, _ = jobs_store.enqueue_job(user_id, lane, reason=f"test_{lane}")
    claimed = jobs_store.claim_next_job(worker_id, lanes={lane})
    assert claimed is not None and int(claimed["id"]) == job_id
    assert jobs_store.mark_running(job_id, claimed_by=worker_id)
    return job_id


def test_chat_atomically_preempts_same_user_profile_and_keeps_return_tuple(monkeypatch):
    uid = "u_atomic_preempt_profile"
    seed_user(uid)
    profile_id = _running_job(uid, "profile", "heavy-0:g1")
    msg_id = uuid.uuid4().hex

    notifications = []

    def _notify_after_commit(event):
        with db.get_pool().connection() as conn:
            status = conn.execute(
                "SELECT status FROM agent_jobs WHERE id=%s", (event.job_id,)
            ).fetchone()[0]
        notifications.append((event, status))

    monkeypatch.setattr(wake_bus, "notify_job_cancel", _notify_after_commit)
    result = db.chat_append_and_enqueue(
        uid,
        msg_id,
        time.time(),
        _msg_doc(msg_id),
        5000,
        "chat",
        reason="chat_send",
        trace_id=msg_id,
        expected_generation=db.get_runtime_generation(uid),
    )

    assert isinstance(result, tuple) and len(result) == 2
    seq, chat_id = result
    assert isinstance(seq, int) and isinstance(chat_id, int)
    with db.get_pool().connection() as conn:
        profile = conn.execute(
            "SELECT status,last_error,claimed_by,claimed_at,started_at,"
            "lease_expires_at FROM agent_jobs WHERE id=%s",
            (profile_id,),
        ).fetchone()
        chat = conn.execute(
            "SELECT status,lane FROM agent_jobs WHERE id=%s", (chat_id,)
        ).fetchone()
    assert profile == ("superseded", "foreground_chat_preempted", None, None, None, None)
    assert chat == ("pending", "chat")
    assert notifications == [
        (
            wake_bus.JobCancellation(
                profile_id, "heavy-0:g1", "foreground_chat_preempted"
            ),
            "superseded",
        )
    ]


@pytest.mark.parametrize("lane", ["scheduled", "capture"])
def test_chat_requeues_recoverable_same_user_background_without_duplication(lane):
    uid = f"u_atomic_preempt_{lane}"
    seed_user(uid)
    background_id = _running_job(uid, lane, f"worker-{lane}")
    msg_id = uuid.uuid4().hex

    db.chat_append_and_enqueue(
        uid,
        msg_id,
        time.time(),
        _msg_doc(msg_id),
        5000,
        "chat",
        expected_generation=db.get_runtime_generation(uid),
    )

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status,claimed_by,last_error FROM agent_jobs "
            "WHERE user_id=%s AND lane=%s",
            (uid, lane),
        ).fetchall()
    assert rows == [(background_id, "pending", None, "foreground_chat_preempted")]


def test_chat_does_not_preempt_existing_chat_or_another_users_background():
    uid = "u_atomic_preempt_scope"
    other_uid = "u_atomic_preempt_other"
    seed_user(uid)
    seed_user(other_uid)
    existing_chat_id = _running_job(uid, "chat", "foreground-0:g1")
    other_profile_id = _running_job(other_uid, "profile", "heavy-0:g2")
    msg_id = uuid.uuid4().hex

    _seq, returned_chat_id = db.chat_append_and_enqueue(
        uid,
        msg_id,
        time.time(),
        _msg_doc(msg_id),
        5000,
        "chat",
        expected_generation=db.get_runtime_generation(uid),
    )

    assert returned_chat_id == existing_chat_id
    with db.get_pool().connection() as conn:
        existing_chat = conn.execute(
            "SELECT status,claimed_by FROM agent_jobs WHERE id=%s",
            (existing_chat_id,),
        ).fetchone()
        other_profile = conn.execute(
            "SELECT status,claimed_by FROM agent_jobs WHERE id=%s",
            (other_profile_id,),
        ).fetchone()
    assert existing_chat == ("running", "foreground-0:g1")
    assert other_profile == ("running", "heavy-0:g2")


def test_chat_insert_failure_rolls_back_background_preemption(monkeypatch):
    uid = "u_atomic_preempt_rollback"
    seed_user(uid)
    profile_id = _running_job(uid, "profile", "heavy-0:g1")
    msg_id = uuid.uuid4().hex

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated chat insert failure")

    notifications = []
    monkeypatch.setattr(jobs_store, "coalesce_or_insert_on_cursor", _boom)
    monkeypatch.setattr(wake_bus, "notify_job_cancel", notifications.append)
    with pytest.raises(RuntimeError, match="simulated chat insert failure"):
        db.chat_append_and_enqueue(
            uid,
            msg_id,
            time.time(),
            _msg_doc(msg_id),
            5000,
            "chat",
            expected_generation=db.get_runtime_generation(uid),
        )

    with db.get_pool().connection() as conn:
        profile = conn.execute(
            "SELECT status,claimed_by FROM agent_jobs WHERE id=%s", (profile_id,)
        ).fetchone()
        message = conn.execute(
            "SELECT 1 FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, msg_id),
        ).fetchone()
    assert profile == ("running", "heavy-0:g1")
    assert message is None
    assert notifications == []


def test_rollback_on_job_failure_leaves_no_orphan_message(monkeypatch):
    uid = "u_atomic_rollback"
    seed_user(uid)
    msg_id = uuid.uuid4().hex

    def _boom(cur, user_id, lane, **kw):
        raise RuntimeError("simulated job insert failure")

    monkeypatch.setattr(jobs_store, "coalesce_or_insert_on_cursor", _boom)

    with pytest.raises(RuntimeError, match="simulated job insert failure"):
        db.chat_append_and_enqueue(
            uid, msg_id, time.time(), _msg_doc(msg_id), 5000, "chat",
            reason="chat_send", trace_id=msg_id, expected_generation=1,
        )

    with db.get_pool().connection() as conn:
        msg_rows = conn.execute(
            "SELECT 1 FROM chat_messages WHERE user_id=%s AND msg_id=%s", (uid, msg_id),
        ).fetchall()
        job_rows = conn.execute(
            "SELECT 1 FROM agent_jobs WHERE user_id=%s", (uid,),
        ).fetchall()
    assert msg_rows == []
    assert job_rows == []


def test_success_persists_message_and_one_job_with_generation(monkeypatch):
    uid = "u_atomic_success"
    seed_user(uid)
    msg_id = uuid.uuid4().hex
    gen = db.get_runtime_generation(uid)  # lazily inits to 1

    seq, job_id = db.chat_append_and_enqueue(
        uid, msg_id, time.time(), _msg_doc(msg_id), 5000, "chat",
        reason="chat_send", trace_id=msg_id, expected_generation=gen,
    )
    assert isinstance(seq, int) and seq > 0
    assert isinstance(job_id, int) and job_id > 0

    with db.get_pool().connection() as conn:
        msg_row = conn.execute(
            "SELECT seq FROM chat_messages WHERE user_id=%s AND msg_id=%s", (uid, msg_id),
        ).fetchone()
        job_rows = conn.execute(
            "SELECT id, lane, status, expected_runtime_generation FROM agent_jobs WHERE user_id=%s",
            (uid,),
        ).fetchall()
    assert msg_row is not None and int(msg_row[0]) == seq
    assert len(job_rows) == 1
    job_row_id, lane, status, expected_gen = job_rows[0]
    assert job_row_id == job_id
    assert lane == "chat"
    assert status == "pending"
    assert int(expected_gen) == gen


def test_client_retry_recovers_original_message_without_second_job():
    """A lost iOS response must not duplicate the message or execute twice."""
    uid = "u_atomic_client_retry"
    seed_user(uid)
    gen = db.get_runtime_generation(uid)
    client_msg_id = str(uuid.uuid4())
    first_id = uuid.uuid4().hex
    first_doc = {**_msg_doc(first_id), "client_msg_id": client_msg_id}

    first_seq, first_job_id = db.chat_append_and_enqueue(
        uid,
        first_id,
        time.time(),
        first_doc,
        5000,
        "chat",
        reason="chat_send",
        trace_id=first_id,
        expected_generation=gen,
        client_msg_id=client_msg_id,
        idempotency_window_sec=600,
    )

    retry_id = uuid.uuid4().hex
    retry_doc = {**_msg_doc(retry_id), "client_msg_id": client_msg_id}
    retry_seq, retry_job_id = db.chat_append_and_enqueue(
        uid,
        retry_id,
        time.time(),
        retry_doc,
        5000,
        "chat",
        reason="chat_send",
        trace_id=retry_id,
        expected_generation=gen,
        client_msg_id=client_msg_id,
        idempotency_window_sec=600,
    )

    assert retry_seq == first_seq
    assert retry_job_id is None
    assert isinstance(first_job_id, int) and first_job_id > 0
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT msg_id,doc->>'client_msg_id' FROM chat_messages "
            "WHERE user_id=%s",
            (uid,),
        ).fetchall()
        jobs = conn.execute(
            "SELECT id FROM agent_jobs WHERE user_id=%s", (uid,)
        ).fetchall()
    assert rows == [(first_id, client_msg_id)]
    assert jobs == [(first_job_id,)]


def test_concurrent_client_retries_have_one_message_and_one_job():
    """The advisory lock makes simultaneous multi-worker retries single-flight."""
    uid = "u_atomic_concurrent_client_retry"
    seed_user(uid)
    generation = db.get_runtime_generation(uid)
    client_msg_id = str(uuid.uuid4())

    def send(index: int):
        msg_id = f"retry-{index}-{uuid.uuid4().hex}"
        return db.chat_append_and_enqueue(
            uid,
            msg_id,
            time.time(),
            {**_msg_doc(msg_id), "client_msg_id": client_msg_id},
            5000,
            "chat",
            reason="chat_send",
            trace_id=msg_id,
            expected_generation=generation,
            client_msg_id=client_msg_id,
            idempotency_window_sec=600,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(send, (1, 2)))

    assert len({seq for seq, _job_id in results}) == 1
    assert sum(job_id is not None for _seq, job_id in results) == 1
    with db.get_pool().connection() as conn:
        message_count = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE user_id=%s", (uid,)
        ).fetchone()[0]
        job_count = conn.execute(
            "SELECT COUNT(*) FROM agent_jobs WHERE user_id=%s", (uid,)
        ).fetchone()[0]
    assert (message_count, job_count) == (1, 1)


def test_atomic_send_retains_every_source_row_without_tee_eviction(monkeypatch):
    """The 5,000-row value is a hot-cache cap, never durable history GC."""
    uid = "u_atomic_send_tee_trim"
    seed_user(uid)
    gen = db.get_runtime_generation(uid)
    for index in range(1, 4):
        db.chat_append(
            uid,
            f"old-{index}",
            float(index),
            _msg_doc(f"old-{index}"),
            0,
        )
    old_two_seq = db.chat_seq_for_msg_id(uid, "old-2")
    assert old_two_seq is not None
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

    new_id = "new-message"
    db.chat_append_and_enqueue(
        uid,
        new_id,
        4.0,
        _msg_doc(new_id),
        2,
        "chat",
        reason="chat_send",
        trace_id=new_id,
        expected_generation=gen,
    )

    with db.get_pool().connection() as conn:
        remaining = {
            row[0]
            for row in conn.execute(
                "SELECT msg_id FROM chat_messages WHERE user_id=%s", (uid,)
            ).fetchall()
        }
    assert remaining == {"old-1", "old-2", "old-3", new_id}
    assert mirrored == []


def test_singleflight_race_retries_and_preserves_message(monkeypatch):
    """Simulates the concurrent same-user/same-lane race: the FIRST coalesce
    attempt hits the unique-index UniqueViolation a real racer would produce
    (both transactions passed the "no active job" check, both tried to
    INSERT). chat_append_and_enqueue must retry the WHOLE transaction rather
    than let the UniqueViolation roll back the message half too."""
    uid = "u_atomic_singleflight_retry"
    seed_user(uid)
    msg_id = uuid.uuid4().hex
    gen = db.get_runtime_generation(uid)

    original = jobs_store.coalesce_or_insert_on_cursor
    calls = {"n": 0}

    def _flaky(cur, user_id, lane, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.errors.UniqueViolation("simulated single-flight race")
        return original(cur, user_id, lane, **kw)

    monkeypatch.setattr(jobs_store, "coalesce_or_insert_on_cursor", _flaky)

    seq, job_id = db.chat_append_and_enqueue(
        uid, msg_id, time.time(), _msg_doc(msg_id), 5000, "chat",
        reason="chat_send", trace_id=msg_id, expected_generation=gen,
    )

    assert calls["n"] == 2  # first attempt raised, retry succeeded
    assert isinstance(seq, int) and seq > 0
    assert isinstance(job_id, int) and job_id > 0

    with db.get_pool().connection() as conn:
        msg_row = conn.execute(
            "SELECT seq FROM chat_messages WHERE user_id=%s AND msg_id=%s", (uid, msg_id),
        ).fetchone()
        job_rows = conn.execute(
            "SELECT id, status FROM agent_jobs WHERE user_id=%s AND lane='chat' "
            "AND status IN ('pending','claimed','running')",
            (uid,),
        ).fetchall()
    assert msg_row is not None and int(msg_row[0]) == seq
    assert len(job_rows) == 1
    assert int(job_rows[0][0]) == job_id


def test_unknown_lane_raises_before_any_write():
    uid = "u_atomic_bad_lane"
    seed_user(uid)
    msg_id = uuid.uuid4().hex
    with pytest.raises(ValueError):
        db.chat_append_and_enqueue(
            uid, msg_id, time.time(), _msg_doc(msg_id), 5000, "not_a_real_lane",
        )
    with db.get_pool().connection() as conn:
        msg_rows = conn.execute(
            "SELECT 1 FROM chat_messages WHERE user_id=%s AND msg_id=%s", (uid, msg_id),
        ).fetchall()
    assert msg_rows == []


def test_runtime_control_cas_rolls_back_send_after_observed_tuple_changes():
    uid = "u_atomic_runtime_cas_loss"
    seed_user(uid)
    msg_id = uuid.uuid4().hex
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO user_blobs (user_id,kind,doc) "
            "VALUES (%s,'model_api_runtime',%s) "
            "ON CONFLICT (user_id,kind) DO UPDATE SET doc=EXCLUDED.doc",
            (uid, db.Jsonb({"hosted_runtime_mode": "db_action_v2"})),
        )
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'v2',9) ON CONFLICT (user_id) DO UPDATE SET "
            "hosted_runtime_state='v2',runtime_generation=9",
            (uid,),
        )
    observed = db.get_hosted_runtime_control_strict(uid)
    assert observed == ("db_action_v2", "v2", 9)

    # Simulate a cutover winning after chat/send's read but before the atomic
    # append. The CAS must reject both halves of the write.
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET hosted_runtime_state='resident', "
            "runtime_generation=10 WHERE user_id=%s",
            (uid,),
        )
        conn.execute(
            "UPDATE user_blobs SET doc=doc || %s WHERE user_id=%s "
            "AND kind='model_api_runtime'",
            (db.Jsonb({"hosted_runtime_mode": "resident_cli"}), uid),
        )

    with pytest.raises(db.RuntimeControlChangedError):
        db.chat_append_and_enqueue(
            uid,
            msg_id,
            time.time(),
            _msg_doc(msg_id),
            5000,
            "chat",
            reason="chat_send",
            trace_id=msg_id,
            expected_generation=observed[2],
            expected_runtime_state=observed[1],
            expected_runtime_mode=observed[0],
        )

    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, msg_id),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM agent_jobs WHERE user_id=%s",
            (uid,),
        ).fetchone() is None


def test_runtime_control_cas_accepts_unchanged_v2_tuple():
    uid = "u_atomic_runtime_cas_success"
    seed_user(uid)
    msg_id = uuid.uuid4().hex
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO user_blobs (user_id,kind,doc) "
            "VALUES (%s,'model_api_runtime',%s) "
            "ON CONFLICT (user_id,kind) DO UPDATE SET doc=EXCLUDED.doc",
            (uid, db.Jsonb({"hosted_runtime_mode": "db_action_v2"})),
        )
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'v2',4) ON CONFLICT (user_id) DO UPDATE SET "
            "hosted_runtime_state='v2',runtime_generation=4",
            (uid,),
        )

    seq, job_id = db.chat_append_and_enqueue(
        uid,
        msg_id,
        time.time(),
        _msg_doc(msg_id),
        5000,
        "chat",
        expected_generation=4,
        expected_runtime_state="v2",
        expected_runtime_mode="db_action_v2",
    )
    assert seq > 0 and job_id > 0


def test_fenced_send_and_claim_share_runtime_then_job_lock_order(monkeypatch):
    """Deterministically exercise the old ABBA shape. The send pauses while
    holding runtime_state immediately before it locks/coalesces the job; a
    concurrent claim must wait on runtime_state rather than lock the job first.
    Releasing send therefore lets both operations finish without deadlock."""
    import concurrent.futures
    import threading

    uid = "u_atomic_send_claim_lock_order"
    seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO user_blobs (user_id,kind,doc) "
            "VALUES (%s,'model_api_runtime',%s) "
            "ON CONFLICT (user_id,kind) DO UPDATE SET doc=EXCLUDED.doc",
            (uid, db.Jsonb({"hosted_runtime_mode": "db_action_v2"})),
        )
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'v2',5) ON CONFLICT (user_id) DO UPDATE SET "
            "hosted_runtime_state='v2',runtime_generation=5",
            (uid,),
        )
    jobs_store.enqueue_job(uid, "chat", expected_generation=5)

    reached_job_lock = threading.Event()
    release_send = threading.Event()
    original_coalesce = jobs_store.coalesce_or_insert_on_cursor

    def _paused_coalesce(*args, **kwargs):
        reached_job_lock.set()
        assert release_send.wait(timeout=5)
        return original_coalesce(*args, **kwargs)

    monkeypatch.setattr(
        jobs_store, "coalesce_or_insert_on_cursor", _paused_coalesce)
    msg_id = uuid.uuid4().hex

    def _send():
        return db.chat_append_and_enqueue(
            uid,
            msg_id,
            time.time(),
            _msg_doc(msg_id),
            5000,
            "chat",
            expected_generation=5,
            expected_runtime_state="v2",
            expected_runtime_mode="db_action_v2",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        send_future = pool.submit(_send)
        assert reached_job_lock.wait(timeout=5)
        claim_future = pool.submit(
            jobs_store.claim_next_job, "w-lock-order")
        # Give claim a chance to reach (and block on) the runtime row.
        threading.Event().wait(0.1)
        release_send.set()
        _seq, sent_job_id = send_future.result(timeout=5)
        claimed = claim_future.result(timeout=5)

    assert claimed is not None
    assert claimed["id"] == sent_job_id
