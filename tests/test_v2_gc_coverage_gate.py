"""Durable chat retention: append limits never delete source rows.

The historical filename is retained so downstream test selectors keep working,
but the old "coverage gate" is intentionally gone. A summary is a derived
prompt index, not proof that the encrypted source transcript may be destroyed.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402

from conftest import seed_user  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed durable chat retention tests require PostgreSQL",
)


@pytest.fixture(autouse=True)
def pg_clean():
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_conversation_summary, agent_jobs, chat_messages CASCADE"
        )
    yield


def _ids(uid: str) -> list[str]:
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT msg_id FROM chat_messages WHERE user_id=%s ORDER BY seq ASC",
            (uid,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _doc(msg_id: str, *, role: str = "user") -> dict:
    return {"id": msg_id, "role": role, "body_ct": f"cipher-{msg_id}"}


def test_legacy_append_limit_only_bounds_hot_cache_not_durable_rows():
    uid = "retention-legacy"
    seed_user(uid)

    for index in range(10):
        msg_id = f"message-{index}"
        db.chat_append(uid, msg_id, float(index), _doc(msg_id), max_messages=3)

    assert _ids(uid) == [f"message-{index}" for index in range(10)]
    assert [row["id"] for row in db.chat_load_recent_strict(uid, 3)] == [
        "message-7", "message-8", "message-9",
    ]


def test_v2_strict_append_limit_never_authorizes_delete():
    uid = "retention-v2-strict"
    seed_user(uid)
    for index in range(6):
        msg_id = f"message-{index}"
        db.chat_append_strict(uid, msg_id, float(index), _doc(msg_id), 0)

    for index in range(6, 12):
        msg_id = f"message-{index}"
        db.chat_append_strict(uid, msg_id, float(index), _doc(msg_id), 3)

    assert _ids(uid) == [f"message-{index}" for index in range(12)]


def test_atomic_v2_send_and_reply_paths_preserve_older_source_rows():
    uid = "retention-v2-atomic"
    seed_user(uid)
    for index in range(3):
        msg_id = f"seed-{index}"
        db.chat_append_strict(uid, msg_id, float(index), _doc(msg_id), 1)

    db.chat_append_and_enqueue(
        uid,
        "atomic-user",
        10.0,
        _doc("atomic-user"),
        1,
        "chat",
    )
    db.chat_append_effect_with_cursor(
        uid,
        "atomic-reply",
        11.0,
        _doc("atomic-reply", role="openclaw"),
        1,
        None,
    )

    assert _ids(uid) == [
        "seed-0", "seed-1", "seed-2", "atomic-user", "atomic-reply",
    ]


def test_resident_append_limit_cannot_delete_history_during_rollback_window():
    uid = "retention-resident"
    seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_runtime_state (user_id,hosted_runtime_state) "
            "VALUES (%s,'resident') ON CONFLICT (user_id) DO UPDATE "
            "SET hosted_runtime_state='resident'",
            (uid,),
        )
    db.chat_append(uid, "old", 1.0, _doc("old"), 1)
    db.chat_append_resident_message(
        uid, "resident-new", 2.0,
        _doc("resident-new", role="openclaw"), 1,
    )

    assert _ids(uid) == ["old", "resident-new"]


def test_idempotent_client_send_preserves_rows_beyond_limit():
    uid = "retention-idempotent"
    seed_user(uid)
    for index in range(4):
        msg_id = f"client-{index}"
        key = f"00000000-0000-4000-8000-{index:012d}"
        doc = {
            **_doc(msg_id),
            "client_msg_id": key,
            "ts": time.time() + index,
        }
        _winner, inserted = db.chat_append_idempotent(
            uid, msg_id, doc["ts"], doc, 1,
            client_msg_id=key, window_sec=600,
        )
        assert inserted is True

    assert _ids(uid) == [f"client-{index}" for index in range(4)]
