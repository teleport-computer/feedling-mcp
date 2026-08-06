"""DB-backed tests for hangup cleanup: a finished call's per-turn chat rows are
selected by voice_call_id and deleted, leaving unrelated chat and the summary
row untouched. (The summary write itself needs user key material + a live
model; that half is covered by the local e2e.)"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import db
from voice import routes_asgi
from voice import summary
from voice import transcript_memory


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

    found = {msg_id for msg_id, _seq in summary.call_message_rows(uid, call_id)}
    assert found == {turn_user, turn_reply}

    result = summary.delete_call_messages(uid, call_id)
    assert result == {"deleted": 2, "retained_covered": 0, "remaining": 0}
    assert db.chat_get_strict(uid, turn_user) is None
    assert db.chat_get_strict(uid, turn_reply) is None
    # unrelated chat and other calls stay
    assert db.chat_get_strict(uid, normal) is not None
    assert db.chat_get_strict(uid, other_call) is not None


def test_untagged_reply_rows_are_deleted_via_reply_to_parent():
    # Live-verified shape: only the spoken USER row carries voice_call_id; the
    # assistant reply carries only reply_to_message_id. The reply must still be
    # cleaned up, and the roll-call recheck must count IT, not re-query by the
    # (now deleted) parent tag.
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    turn = _append(uid, {
        "role": "user", "source": "chat",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })
    reply = _append(uid, {
        "role": "openclaw", "source": "chat",
        "reply_to_message_id": turn,   # no voice_call_id — the real shape
    })
    unrelated_reply = _append(uid, {
        "role": "openclaw", "source": "chat",
        "reply_to_message_id": "someone-else",
    })

    result = summary.delete_call_messages(uid, call_id)
    assert result == {"deleted": 2, "retained_covered": 0, "remaining": 0}
    assert db.chat_get_strict(uid, turn) is None
    assert db.chat_get_strict(uid, reply) is None
    assert db.chat_get_strict(uid, unrelated_reply) is not None


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

    first = summary.delete_call_messages(uid, call_id)
    assert (first["deleted"], first["remaining"]) == (1, 0)
    assert db.chat_get_strict(uid, smid) is not None
    # replayed cleanup deletes nothing further and keeps the summary
    replay = summary.delete_call_messages(uid, call_id)
    assert (replay["deleted"], replay["remaining"]) == (0, 0)
    assert db.chat_get_strict(uid, smid) is not None


def test_rows_folded_by_compaction_are_retained_not_deleted():
    # C1 guard: a voice row already covered by the V2 summary watermark is part
    # of compaction's frozen ledger — deleting it would corrupt the frontier.
    # It must be RETAINED (and not counted as a cleanup failure).
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    early = _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })
    late = _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t2",
    })
    with db.get_pool().connection() as conn:
        early_seq = conn.execute(
            "SELECT seq FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, early),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO v2_conversation_summary (user_id, watermark_seq) "
            "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE "
            "SET watermark_seq = EXCLUDED.watermark_seq",
            (uid, int(early_seq)),
        )

    result = summary.delete_call_messages(uid, call_id)
    assert result == {"deleted": 1, "retained_covered": 1, "remaining": 0}
    assert db.chat_get_strict(uid, early) is not None   # folded row kept
    assert db.chat_get_strict(uid, late) is None        # unfolded row deleted


# --------------------------------------------------------------------------- #
# finalize route: memory step routing (transcript capture vs summary nudge)
# --------------------------------------------------------------------------- #

def _run_finalize(monkeypatch, uid: str, call_id: str, *, capture=None):
    """Drive the real finalize route against the DB with the memory/model seams
    stubbed. ``capture`` is the stub for capture_from_transcript (None keeps a
    guard that fails the test if it is reached). Returns (status, body, calls)."""
    calls = {"capture": 0, "nudge": 0}
    payload = {
        "call_id": call_id,
        "turns": [
            {"role": "user", "text": "这周都在加班赶项目"},
            {"role": "assistant", "text": "记得照顾好自己"},
        ],
    }

    async def _read_json(_request):
        return payload

    monkeypatch.setattr(routes_asgi.asgi_http, "read_json_silent", _read_json)
    monkeypatch.setattr(routes_asgi.wake_bus, "notify", lambda *_a, **_k: None)
    monkeypatch.setattr(summary, "generate_summary", lambda _s, _t: "总结一下")

    def _persist(_store, _text, mid, cid):
        if db.chat_get_strict(uid, mid) is None:
            db.chat_append_strict(uid, mid, time.time(), {
                "id": mid, "role": "openclaw",
                "source": "voice_call_summary", "voice_call_id": cid,
            }, 200)
        return True

    monkeypatch.setattr(summary, "persist_summary", _persist)

    def _capture(_store, _turns, *, call_id=""):
        calls["capture"] += 1
        if capture is None:
            raise AssertionError("capture_from_transcript must not run here")
        return capture()

    monkeypatch.setattr(transcript_memory, "capture_from_transcript", _capture)
    monkeypatch.setattr(
        summary,
        "nudge_capture",
        lambda _store: calls.__setitem__("nudge", calls["nudge"] + 1),
    )
    response = asyncio.run(
        routes_asgi.finalize_voice_call(
            SimpleNamespace(), SimpleNamespace(user_id=uid)
        )
    )
    return response.status_code, json.loads(response.body), calls


def test_fresh_finalize_runs_transcript_capture_once_and_skips_nudge(monkeypatch):
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })

    status, body, calls = _run_finalize(
        monkeypatch, uid, call_id, capture=lambda: True
    )
    assert (status, body["status"], body["replayed"]) == (200, "finalized", False)
    assert calls == {"capture": 1, "nudge": 0}


def test_fresh_finalize_falls_back_to_nudge_when_capture_declines(monkeypatch):
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })

    status, body, calls = _run_finalize(
        monkeypatch, uid, call_id, capture=lambda: False
    )
    assert (status, body["status"]) == (200, "finalized")
    assert calls == {"capture": 1, "nudge": 1}


def test_fresh_finalize_falls_back_to_nudge_when_capture_raises(monkeypatch):
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })

    def _boom():
        raise RuntimeError("provider down")

    status, body, calls = _run_finalize(monkeypatch, uid, call_id, capture=_boom)
    # Memory errors never fail the request: summary/cleanup define the result.
    assert (status, body["status"]) == (200, "finalized")
    assert calls == {"capture": 1, "nudge": 1}


def test_replay_finalize_nudges_instead_of_transcript_capture(monkeypatch):
    # Replay: the summary row is already durable (first attempt persisted it but
    # e.g. 502'd on cleanup). Transcript capture must NOT re-run — the
    # summary-window nudge is the memory backstop on this path.
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })
    smid = summary.summary_message_id(call_id)
    db.chat_append_strict(uid, smid, time.time(), {
        "id": smid, "role": "openclaw",
        "source": "voice_call_summary", "voice_call_id": call_id,
    }, 200)

    status, body, calls = _run_finalize(monkeypatch, uid, call_id, capture=None)
    assert (status, body["status"], body["replayed"]) == (200, "finalized", True)
    assert calls == {"capture": 0, "nudge": 1}
