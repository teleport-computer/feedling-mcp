"""db.chat_append_and_enqueue: atomic message-persist + job-enqueue (spec A7).

A crash between "write the user's chat message" and "enqueue its chat job"
today orphans the message (persisted, never processed -> no reply). This
module verifies both writes happen inside ONE transaction: a failure in the
job half rolls back the message half too, and a normal call persists both and
threads the generation through to the new job row.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
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
        conn.execute("TRUNCATE chat_messages, agent_jobs, v2_runtime_state CASCADE")
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
