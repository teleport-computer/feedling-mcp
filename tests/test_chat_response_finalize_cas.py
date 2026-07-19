"""Milestone-A proof for atomic reply finalization.

The first test deliberately characterizes the old chat_core
check -> append -> mark race.  The remaining tests pin the replacement's
single-statement PostgreSQL CAS and its primary-key access path.

Run: python3 -m pytest tests/test_chat_response_finalize_cas.py -q
"""
from __future__ import annotations

import base64
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from chat import chat_core  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _envelope(user_id: str, msg_id: str) -> dict:
    return {
        "v": 1,
        "id": msg_id,
        "body_ct": _b64(f"ciphertext:{msg_id}".encode()),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 32),
        "K_enclave": _b64(b"\x02" * 32),
        "visibility": "shared",
        "owner_user_id": user_id,
    }


@pytest.fixture()
def store(backend_env):
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return core_store.get_store(res.get_json()["user_id"])


def test_old_chat_core_check_append_mark_reproduces_two_replies(
    store, monkeypatch
):
    """Force both old-path callers to inspect the same unanswered snapshot.

    Returning independent snapshots is important: it models two workers, whose
    caches cannot observe each other's in-place parent mutation.  Once both
    reads have crossed the barrier, the legacy code has no database CAS, so
    both requests append successfully and only later overwrite parent metadata.
    """
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "parent_old_race")
    )
    real_lookup = chat_core._chat_message_by_id
    both_read_parent = threading.Barrier(2)
    counter_lock = threading.Lock()
    parent_reads = 0

    def synchronized_parent_snapshot(target_store, msg_id):
        nonlocal parent_reads
        snapshot = real_lookup(target_store, msg_id)
        if msg_id != parent["id"]:
            return snapshot
        with counter_lock:
            should_gate = parent_reads < 2
            parent_reads += 1
        if should_gate:
            both_read_parent.wait(timeout=5)
        return dict(snapshot) if snapshot is not None else None

    monkeypatch.setattr(chat_core, "_chat_message_by_id", synchronized_parent_snapshot)
    monkeypatch.setattr(
        chat_core.chat_consumer, "_record_consumer_event", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(chat_core.debug_trace, "trace_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(store, "mark_first_chat_ok", lambda *args, **kwargs: {})
    monkeypatch.setattr(core_store.wake_bus, "notify", lambda *args, **kwargs: None)

    def post(reply_id: str):
        return chat_core.write_response(
            store,
            {
                "envelope": _envelope(store.user_id, reply_id),
                "reply_to_message_id": parent["id"],
            },
            consumer_id=f"consumer-{reply_id}",
            consumer_info={},
            allow_verify_reply=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(post, ("old_reply_a", "old_reply_b")))

    assert sorted(status for _body, status in results) == [200, 200]
    replies = [
        msg for msg in db.chat_load(store.user_id)
        if msg.get("id") in {"old_reply_a", "old_reply_b"}
    ]
    assert {msg["id"] for msg in replies} == {"old_reply_a", "old_reply_b"}
    persisted_parent = next(
        msg for msg in db.chat_load(store.user_id) if msg.get("id") == parent["id"]
    )
    assert persisted_parent["reply_status"] == "replied"
    assert persisted_parent["reply_message_id"] in {"old_reply_a", "old_reply_b"}


def test_finalize_reply_once_two_workers_exactly_one_wins(store):
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "parent_atomic_race")
    )
    start = threading.Barrier(2)

    def finalize(reply_id: str):
        candidate = store._build_chat_message(
            "openclaw", "chat", _envelope(store.user_id, reply_id)
        )
        replied_fields = {
            "reply_status": "replied",
            "reply_message_id": reply_id,
            "replied_by": f"consumer-{reply_id}",
            "replied_at": f"{candidate['ts']:.3f}",
        }
        start.wait(timeout=5)
        result = db.chat_finalize_reply_once(
            store.user_id,
            parent["id"],
            reply_id,
            float(candidate["ts"]),
            candidate,
            replied_fields,
        )
        return reply_id, result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(finalize, ("atomic_reply_a", "atomic_reply_b")))

    winners = [(reply_id, result) for reply_id, result in results if result is not None]
    losers = [(reply_id, result) for reply_id, result in results if result is None]
    assert len(winners) == 1
    assert len(losers) == 1

    winner_id, (parent_doc, reply_doc) = winners[0]
    assert parent_doc["reply_status"] == "replied"
    assert parent_doc["reply_message_id"] == winner_id
    assert reply_doc["id"] == winner_id

    rows = db.chat_load(store.user_id)
    persisted_parent = next(msg for msg in rows if msg.get("id") == parent["id"])
    persisted_replies = [
        msg for msg in rows
        if msg.get("id") in {"atomic_reply_a", "atomic_reply_b"}
    ]
    assert persisted_parent["reply_message_id"] == winner_id
    assert [msg["id"] for msg in persisted_replies] == [winner_id]


def test_finalize_reply_once_explain_uses_parent_primary_key(store):
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "parent_explain")
    )
    candidate = store._build_chat_message(
        "openclaw", "chat", _envelope(store.user_id, "reply_explain")
    )

    # Make a sequential scan unattractive even in the small throwaway test DB.
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id, msg_id, ts, doc) "
            "SELECT %s, 'explain_filler_' || n::text, n::float8, "
            "       jsonb_build_object('id', 'explain_filler_' || n::text) "
            "FROM generate_series(1, 500) AS n",
            (store.user_id,),
        )
        plan_rows = conn.execute(
            "EXPLAIN (FORMAT TEXT) " + db._CHAT_FINALIZE_REPLY_ONCE_SQL,
            (
                db.Jsonb({
                    "reply_status": "replied",
                    "reply_message_id": candidate["id"],
                }),
                store.user_id,
                parent["id"],
                store.user_id,
                candidate["id"],
                float(candidate["ts"]),
                db.Jsonb(candidate),
            ),
        ).fetchall()

    plan = "\n".join(row[0] for row in plan_rows)
    assert "Index Scan using chat_messages_pkey on chat_messages" in plan
    assert "Seq Scan on chat_messages" not in plan
