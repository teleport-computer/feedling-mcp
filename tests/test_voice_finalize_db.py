"""DB-backed tests for hangup cleanup: a finished call's per-turn chat rows are
selected by voice_call_id and deleted, leaving unrelated chat and the summary
row untouched. (The summary write itself needs user key material + a live
model; that half is covered by the local e2e.)"""

from __future__ import annotations

import time
import uuid

import db
from voice import summary


def _seed_user() -> str:
    uid = "u_" + uuid.uuid4().hex[:12]
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) "
            "VALUES (%s, '', '{}'::jsonb) ON CONFLICT DO NOTHING",
            (uid,),
        )
    return uid


def _append(uid: str, doc: dict) -> str:
    msg_id = uuid.uuid4().hex
    db.chat_append_strict(uid, msg_id, time.time(), dict(doc, id=msg_id), 200)
    return msg_id


def test_call_rows_selected_and_deleted_others_kept():
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    turn_user = _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })
    turn_reply = _append(uid, {
        "role": "openclaw", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })
    normal = _append(uid, {"role": "user", "source": "model_api"})
    other_call = _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": "vcall_other", "voice_turn_id": "t1",
    })

    found = set(summary.call_message_ids(uid, call_id))
    assert found == {turn_user, turn_reply}

    deleted = summary.delete_call_messages(uid, call_id)
    assert deleted == 2
    assert db.chat_get_strict(uid, turn_user) is None
    assert db.chat_get_strict(uid, turn_reply) is None
    # unrelated chat and other calls stay
    assert db.chat_get_strict(uid, normal) is not None
    assert db.chat_get_strict(uid, other_call) is not None


def test_delete_never_touches_the_summary_row_and_is_idempotent():
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })
    # Simulate the durable summary row: same call_id metadata, but its msg_id is
    # the deterministic summary id, which delete must skip.
    smid = summary.summary_message_id(call_id)
    db.chat_append_strict(uid, smid, time.time(), {
        "id": smid, "role": "openclaw", "source": "voice_call_summary",
        "voice_call_id": call_id,
    }, 200)

    assert summary.delete_call_messages(uid, call_id) == 1
    assert db.chat_get_strict(uid, smid) is not None
    # replayed cleanup deletes nothing further and keeps the summary
    assert summary.delete_call_messages(uid, call_id) == 0
    assert db.chat_get_strict(uid, smid) is not None
