"""Atomic reply finalization milestones A through C.

The tests pin the single-statement PostgreSQL CAS, its primary-key access path,
the chat_core wiring, winner-only side effects, row-shape parity, R2 behavior,
the parent-only TEE mirror trust boundary, INSERT-failure rollback, and the
remaining response-ingress routing.

Run: python3 -m pytest tests/test_chat_response_finalize_cas.py -q
"""
from __future__ import annotations

import base64
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import object_storage  # noqa: E402
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


def test_chat_core_two_workers_one_reply_and_winner_only_side_effects(
    store, monkeypatch
):
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "parent_wired_race")
    )
    both_entered_finalize = threading.Barrier(2)
    real_finalize = db.chat_finalize_reply_once

    def synchronized_finalize(*args, **kwargs):
        both_entered_finalize.wait(timeout=5)
        return real_finalize(*args, **kwargs)

    wakes = []
    captures = []
    first_chat_marks = []
    pushes = []
    traces = []
    metadata_updates = []
    real_metadata_update = db.chat_update_metadata
    monkeypatch.setattr(db, "chat_finalize_reply_once", synchronized_finalize)
    monkeypatch.setattr(
        chat_core.chat_consumer, "_record_consumer_event", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        chat_core.debug_trace,
        "trace_event",
        lambda *args, **kwargs: traces.append(kwargs),
    )
    monkeypatch.setattr(
        chat_core,
        "_maybe_mark_first_chat_ok",
        lambda *args: first_chat_marks.append(args),
    )
    monkeypatch.setattr(
        core_store.wake_bus, "notify", lambda *args: wakes.append(args)
    )
    from proactive import capture_scheduler

    monkeypatch.setattr(
        capture_scheduler,
        "record_chat_append",
        lambda *args: captures.append(args) or {},
    )
    monkeypatch.setattr(
        chat_core.push_service,
        "_deliver_ai_message_push_if_background",
        lambda *args, **kwargs: pushes.append((args, kwargs))
        or {"push_decision": "sent"},
    )

    def spy_metadata_update(user_id, msg_id, fields):
        metadata_updates.append((user_id, msg_id, fields))
        return real_metadata_update(user_id, msg_id, fields)

    monkeypatch.setattr(db, "chat_update_metadata", spy_metadata_update)

    def post(reply_id: str):
        return chat_core.write_response(
            store,
            {
                "envelope": _envelope(store.user_id, reply_id),
                "reply_to_message_id": parent["id"],
                "push_body": "winner only",
            },
            consumer_id=f"consumer-{reply_id}",
            consumer_info={},
            allow_verify_reply=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(post, ("wired_reply_a", "wired_reply_b")))

    assert sorted(status for _body, status in results) == [200, 409]
    loser_body = next(body for body, status in results if status == 409)
    assert loser_body == {"error": "already_answered", "reply_status": "replied"}
    replies = [
        msg for msg in db.chat_load(store.user_id)
        if msg.get("id") in {"wired_reply_a", "wired_reply_b"}
    ]
    assert len(replies) == 1
    winner_id = replies[0]["id"]
    persisted_parent = next(
        msg for msg in db.chat_load(store.user_id) if msg.get("id") == parent["id"]
    )
    assert persisted_parent["reply_status"] == "replied"
    assert persisted_parent["reply_message_id"] == winner_id

    cached_parent = next(msg for msg in store.chat_messages if msg["id"] == parent["id"])
    cached_replies = [
        msg for msg in store.chat_messages
        if msg.get("id") in {"wired_reply_a", "wired_reply_b"}
    ]
    assert cached_parent["reply_message_id"] == winner_id
    assert [msg["id"] for msg in cached_replies] == [winner_id]
    assert len(wakes) == 1
    assert len(captures) == 1
    assert len(first_chat_marks) == 1
    assert len(pushes) == 1
    assert len(traces) == 1
    # Delivery metadata may update the winning reply.  The parent itself was
    # reconciled in memory and must never take a second DB UPDATE.
    assert [call[1] for call in metadata_updates] == [winner_id]
    assert all(call[1] != parent["id"] for call in metadata_updates)


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


def test_finalize_mirrors_parent_metadata_only_for_winner(store, monkeypatch):
    from tee_shadow import mirror

    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "parent_tee_boundary")
    )
    candidate = store._build_chat_message(
        "openclaw", "chat", _envelope(store.user_id, "reply_tee_boundary")
    )
    replied_fields = {
        "reply_status": "replied",
        "reply_message_id": candidate["id"],
        "replied_by": "consumer-tee",
        "replied_at": f"{candidate['ts']:.3f}",
    }
    mirror_calls = []
    monkeypatch.setattr(
        mirror, "execute", lambda sql, params=(): mirror_calls.append((sql, params))
    )

    won = db.chat_finalize_reply_once(
        store.user_id,
        parent["id"],
        candidate["id"],
        candidate["ts"],
        candidate,
        replied_fields,
    )
    lost = db.chat_finalize_reply_once(
        store.user_id,
        parent["id"],
        "reply_tee_loser",
        candidate["ts"] + 1,
        {**candidate, "id": "reply_tee_loser"},
        {**replied_fields, "reply_message_id": "reply_tee_loser"},
    )

    assert won is not None
    assert lost is None
    assert len(mirror_calls) == 1
    sql, params = mirror_calls[0]
    assert sql == db._CHAT_FINALIZE_REPLY_PARENT_MIRROR_SQL
    assert "INSERT" not in sql
    assert params[0].obj == replied_fields
    assert not ({"body_ct", "nonce", "K_user", "K_enclave"} & params[0].obj.keys())
    assert params[1:] == (store.user_id, parent["id"])


def test_finalize_insert_collision_rolls_back_parent_and_retry_is_safe(store):
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "parent_insert_failure")
    )
    candidate = store._build_chat_message(
        "openclaw", "chat", _envelope(store.user_id, "reply_insert_collision")
    )
    replied_fields = {
        "reply_status": "replied",
        "reply_message_id": candidate["id"],
        "replied_by": "collision-consumer",
        "replied_at": f"{candidate['ts']:.3f}",
    }

    # Pre-existing same-PK row makes the CTE's INSERT fail after `won` has
    # tentatively updated the parent.  PostgreSQL must roll that UPDATE back.
    db.chat_append(
        store.user_id,
        candidate["id"],
        candidate["ts"] - 1,
        {**candidate, "body_ct": "pre-existing collision"},
        5000,
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.chat_finalize_reply_once(
            store.user_id,
            parent["id"],
            candidate["id"],
            candidate["ts"],
            candidate,
            replied_fields,
        )

    with db.get_pool().connection() as conn:
        parent_after_failure = conn.execute(
            "SELECT doc FROM chat_messages WHERE user_id = %s AND msg_id = %s",
            (store.user_id, parent["id"]),
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM chat_messages WHERE user_id = %s AND msg_id = %s",
            (store.user_id, candidate["id"]),
        )
    assert parent_after_failure.get("reply_status") != "replied"
    assert not parent_after_failure.get("reply_message_id")

    retried = db.chat_finalize_reply_once(
        store.user_id,
        parent["id"],
        candidate["id"],
        candidate["ts"],
        candidate,
        replied_fields,
    )
    assert retried is not None
    retried_parent, retried_reply = retried
    assert retried_parent["reply_status"] == "replied"
    assert retried_parent["reply_message_id"] == candidate["id"]
    assert retried_reply == candidate


def test_finalize_reply_row_shape_matches_append_chat(store, monkeypatch):
    fixed_now = 1_725_000_123.456
    monkeypatch.setattr(core_store.time, "time", lambda: fixed_now)
    shared_body = _b64(b"same ciphertext")
    extra = {
        "gate_decision_id": "gate-1",
        "proactive_job_id": "job-1",
        "thinking_kind": "native",
        "thinking_source": "provider",
        "thinking_model": "model-1",
        "thinking_native": True,
    }

    control_env = _envelope(store.user_id, "shape_append")
    control_env["body_ct"] = shared_body
    control = store.append_chat(
        "openclaw", "chat", control_env, content_type="text", extra=extra
    )

    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "shape_parent")
    )
    atomic_env = _envelope(store.user_id, "shape_atomic")
    atomic_env["body_ct"] = shared_body
    candidate = store._build_chat_message(
        "openclaw", "chat", atomic_env, content_type="text", extra=extra
    )
    finalized = db.chat_finalize_reply_once(
        store.user_id,
        parent["id"],
        candidate["id"],
        candidate["ts"],
        candidate,
        {
            "reply_status": "replied",
            "reply_message_id": candidate["id"],
            "replied_by": "shape-consumer",
            "replied_at": f"{candidate['ts']:.3f}",
        },
    )
    assert finalized is not None
    _parent_doc, atomic = finalized

    normalized_control = {**control, "id": "normalized"}
    normalized_atomic = {**atomic, "id": "normalized"}
    assert normalized_atomic == normalized_control
    assert atomic["ts"] == fixed_now
    assert atomic["source"] == "chat"
    assert atomic["content_type"] == "text"
    for key, value in extra.items():
        assert atomic[key] == value


def test_finalize_reply_image_matches_append_r2_pointer_semantics(
    store, monkeypatch
):
    monkeypatch.setattr(object_storage, "chat_files_enabled", lambda: True)
    uploads = []

    def put_chat_body(user_id, msg_id, body_ct, content_type, **kwargs):
        uploads.append((user_id, msg_id, body_ct, content_type))
        return f"chatfiles/{user_id}/{msg_id}"

    monkeypatch.setattr(object_storage, "put_chat_body", put_chat_body)
    monkeypatch.setattr(object_storage, "delete_chat_body", lambda *args: None)
    shared_body = _b64(b"same image ciphertext")

    control_env = _envelope(store.user_id, "r2_append")
    control_env["body_ct"] = shared_body
    control = store.append_chat(
        "openclaw", "chat", control_env, content_type="image"
    )
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "r2_parent")
    )
    atomic_env = _envelope(store.user_id, "r2_atomic")
    atomic_env["body_ct"] = shared_body
    candidate = store._build_chat_message(
        "openclaw", "chat", atomic_env, content_type="image"
    )
    finalized = db.chat_finalize_reply_once(
        store.user_id,
        parent["id"],
        candidate["id"],
        candidate["ts"],
        candidate,
        {
            "reply_status": "replied",
            "reply_message_id": candidate["id"],
        },
    )
    assert finalized is not None
    db.chat_finalize_reply_post_commit(store.user_id, candidate, 5000)

    def raw_doc(msg_id):
        with db.get_pool().connection() as conn:
            row = conn.execute(
                "SELECT doc FROM chat_messages WHERE user_id = %s AND msg_id = %s",
                (store.user_id, msg_id),
            ).fetchone()
        return row[0]

    control_raw = raw_doc(control["id"])
    atomic_raw = raw_doc(candidate["id"])
    assert "body_ct" not in control_raw
    assert "body_ct" not in atomic_raw
    assert control_raw["body_ct_len"] == atomic_raw["body_ct_len"] == len(shared_body)
    normalized_control = {
        **control_raw,
        "id": "normalized",
        "body_key": "normalized",
        "ts": 0,
    }
    normalized_atomic = {
        **atomic_raw,
        "id": "normalized",
        "body_key": "normalized",
        "ts": 0,
    }
    assert normalized_atomic == normalized_control
    assert [upload[1] for upload in uploads] == [control["id"], candidate["id"]]


def test_system_reply_target_bypasses_finalize_cas(store, monkeypatch):
    parent = store.append_chat(
        "user", "chat", _envelope(store.user_id, "system_bypass_parent")
    )
    monkeypatch.setattr(
        db,
        "chat_finalize_reply_once",
        lambda *args, **kwargs: pytest.fail("system role entered reply CAS"),
    )
    monkeypatch.setattr(
        chat_core.chat_consumer, "_record_consumer_event", lambda *args, **kwargs: None
    )
    body, status = chat_core.write_response(
        store,
        {
            "envelope": _envelope(store.user_id, "system_bypass_notice"),
            "role": "system",
            "notice_kind": "upstream_error",
            "reply_to_message_id": parent["id"],
        },
        consumer_id="system-consumer",
        consumer_info={},
        allow_verify_reply=False,
    )
    assert status == 200, body
    cached_parent = next(msg for msg in store.chat_messages if msg["id"] == parent["id"])
    assert cached_parent.get("reply_status") != "replied"
    assert not cached_parent.get("reply_message_id")


@pytest.mark.parametrize(
    ("source", "parent_source", "allow_verify_reply", "uses_reply_cas"),
    (
        ("verify_ping", "verify_ping", True, True),
        ("resident_maintenance", "resident_maintenance", False, True),
        (chat_core.proactive_service.PROACTIVE_JOB_SOURCE, "", False, False),
    ),
)
def test_verify_resident_and_proactive_response_ingress_remain_valid(
    store,
    monkeypatch,
    source,
    parent_source,
    allow_verify_reply,
    uses_reply_cas,
):
    parent = None
    if parent_source:
        parent = store.append_chat(
            "user",
            parent_source,
            _envelope(store.user_id, f"{source}_parent"),
            extra={"content": "maintenance"} if source == "resident_maintenance" else None,
        )

    finalize_calls = []
    real_finalize = store.finalize_chat_reply_once

    def spy_finalize(*args, **kwargs):
        finalize_calls.append((args, kwargs))
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(store, "finalize_chat_reply_once", spy_finalize)
    monkeypatch.setattr(
        chat_core.chat_consumer, "_record_consumer_event", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        chat_core.push_service,
        "_deliver_ai_message_push_if_background",
        lambda *args, **kwargs: pytest.fail(
            "verify/maintenance suppression or no-push proactive path regressed"
        ),
    )
    payload = {
        "envelope": _envelope(store.user_id, f"{source}_reply"),
        "source": source,
    }
    if parent is not None:
        payload.update({
            "reply_to_message_id": parent["id"],
            "push_body": "must stay suppressed",
            "push_live_activity": True,
        })
    if source == chat_core.proactive_service.PROACTIVE_JOB_SOURCE:
        payload["proactive_job_id"] = "proactive_ingress_job"

    body, status = chat_core.write_response(
        store,
        payload,
        consumer_id=f"consumer-{source}",
        consumer_info={},
        allow_verify_reply=allow_verify_reply,
    )
    assert status == 200, body
    assert len(finalize_calls) == int(uses_reply_cas)

    stored_reply = next(msg for msg in store.chat_messages if msg["id"] == body["id"])
    assert stored_reply["source"] == source
    if parent is not None:
        stored_parent = next(
            msg for msg in store.chat_messages if msg["id"] == parent["id"]
        )
        assert stored_parent["reply_status"] == "replied"
        assert stored_parent["reply_message_id"] == stored_reply["id"]
    else:
        assert stored_reply["proactive_job_id"] == "proactive_ingress_job"


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
