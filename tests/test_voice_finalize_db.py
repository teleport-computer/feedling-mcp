"""DB-backed tests for hangup cleanup: a finished call's per-turn chat rows are
selected by voice_call_id and deleted, leaving unrelated chat and the transcript card
row untouched. (The summary write itself needs user key material + a live
model; that half is covered by the local e2e.)"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
import uuid
from types import SimpleNamespace

import db
import pytest
from voice import routes_asgi
from voice import cleanup as summary


_STUBBED_ARCHIVED_CALLS: set[tuple[str, str]] = set()


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


def test_resident_voice_reply_is_correlated_and_cancelled_reply_is_suppressed():
    uid = _seed_user()
    active_call = "vcall_" + uuid.uuid4().hex[:10]
    db.voice_call_create_active(uid, active_call)
    active_parent = _append(uid, {
        "role": "user",
        "source": "chat",
        "voice_call_id": active_call,
        "voice_turn_id": "1",
    })
    reply_doc = {
        "id": "resident-" + uuid.uuid4().hex,
        "role": "openclaw",
        "source": "chat",
        "body_ct": "ciphertext",
    }
    finalized = db.chat_finalize_reply_once(
        uid,
        active_parent,
        reply_doc["id"],
        time.time() + 1,
        reply_doc,
        {
            "reply_status": "replied",
            "reply_message_id": reply_doc["id"],
        },
    )
    assert finalized is not None
    _parent, persisted = finalized
    assert persisted["reply_to_message_id"] == active_parent
    assert persisted["voice_call_id"] == active_call
    assert persisted["voice_turn_id"] == "1"

    cancelled_call = "vcall_" + uuid.uuid4().hex[:10]
    db.voice_call_create_active(uid, cancelled_call)
    cancelled_parent = _append(uid, {
        "role": "user",
        "source": "chat",
        "voice_call_id": cancelled_call,
        "voice_turn_id": "2",
    })
    assert db.voice_call_cancel(uid, cancelled_call, "user_hangup")["status"] == (
        "cancelled"
    )
    late_reply_id = "resident-" + uuid.uuid4().hex
    with pytest.raises(db.VoiceCallReplySuppressed, match="voice_call_cancelled"):
        db.chat_finalize_reply_once(
            uid,
            cancelled_parent,
            late_reply_id,
            time.time() + 1,
            {
                "id": late_reply_id,
                "role": "openclaw",
                "source": "chat",
                "body_ct": "late",
            },
            {
                "reply_status": "replied",
                "reply_message_id": late_reply_id,
            },
        )
    assert db.chat_get_strict(uid, late_reply_id) is None


def test_finalized_voice_call_cannot_be_downgraded_by_cancel():
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    db.voice_call_create_active(uid, call_id)

    assert db.voice_call_mark_finalized(uid, call_id) == {
        "status": "finalized",
        "replayed": False,
    }
    assert db.voice_call_cancel(uid, call_id, "user_hangup") == {
        "status": "finalized",
        "replayed": True,
    }
    assert db.voice_call_status(uid, call_id) == "finalized"


def test_cancel_and_finalize_claim_one_lifecycle_winner_before_archive():
    uid = _seed_user()

    cancel_wins = "vcall_" + uuid.uuid4().hex[:10]
    db.voice_call_create_active(uid, cancel_wins)
    assert db.voice_call_cancel(uid, cancel_wins, "connect_failed") == {
        "status": "cancelled",
        "replayed": False,
    }
    assert db.voice_call_cancel(uid, cancel_wins, "connect_failed") == {
        "status": "cancelled",
        "replayed": True,
    }
    assert db.voice_call_begin_finalize(uid, cancel_wins)["status"] == "cancelled"

    finalize_wins = "vcall_" + uuid.uuid4().hex[:10]
    db.voice_call_create_active(uid, finalize_wins)
    assert db.voice_call_begin_finalize(uid, finalize_wins) == {
        "status": "finalizing",
        "replayed": False,
    }
    assert db.voice_call_cancel(uid, finalize_wins, "user_hangup") == {
        "status": "finalizing",
        "replayed": True,
    }
    assert db.voice_call_mark_finalized(uid, finalize_wins)["status"] == (
        "finalized"
    )
    assert db.voice_call_status(uid, finalize_wins) == "finalized"


def test_cancel_serializes_with_inflight_resident_reply_before_cleanup():
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    db.voice_call_create_active(uid, call_id)
    parent_id = _append(uid, {
        "role": "user",
        "source": "chat",
        "voice_call_id": call_id,
        "voice_turn_id": "1",
    })
    reply_id = "resident-" + uuid.uuid4().hex
    barrier = threading.Barrier(2)

    def post_reply():
        barrier.wait()
        try:
            return db.chat_finalize_reply_once(
                uid,
                parent_id,
                reply_id,
                time.time() + 1,
                {
                    "id": reply_id,
                    "role": "openclaw",
                    "source": "chat",
                    "body_ct": "answer",
                },
                {"reply_status": "replied", "reply_message_id": reply_id},
            )
        except db.VoiceCallReplySuppressed:
            return None

    def cancel():
        barrier.wait()
        return db.voice_call_cancel(uid, call_id, "user_hangup")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        reply_future = pool.submit(post_reply)
        cancel_future = pool.submit(cancel)
        reply_result = reply_future.result(timeout=5)
        cancel_result = cancel_future.result(timeout=5)

    assert cancel_result["status"] == "cancelled"
    cleanup = summary.delete_call_messages(uid, call_id)
    assert cleanup["remaining"] == 0
    assert db.chat_get_strict(uid, parent_id) is None
    assert db.chat_get_strict(uid, reply_id) is None
    # Either reply committed before cancel took the call lock and cleanup saw
    # it, or cancel won and the reply woke up suppressed. There is no third
    # ordering where a reply appears after cleanup.
    assert reply_result is None or reply_result[1]["voice_call_id"] == call_id


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


def test_delete_never_touches_the_transcript_card_and_is_idempotent():
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })
    # Simulate the durable transcript card row: same call_id metadata, but its msg_id is
    # the deterministic card id, which delete must skip.
    smid = summary.transcript_card_message_id(call_id)
    db.chat_append_strict(uid, smid, time.time(), {
        "id": smid, "role": "openclaw", "source": "voice_call_transcript",
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


def test_uncovered_batch_mirrors_deletes_and_repairs_absent_primary_row(
    monkeypatch,
):
    """Mirror only safe uncovered ids, including an already-absent target.

    The absent row models a competing cleanup whose best-effort mirror was
    missed. The candidate seq snapshot retains the old ``chat_delete``
    self-heal property without allowing the covered row onto the shadow DELETE.
    Removing the helper's mirror block leaves ``mirrored`` empty and fails.
    """
    from tee_shadow import mirror

    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    covered_id = _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "covered",
    })
    absent_id = _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "absent",
    })
    current_id = _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "current",
    })
    targets = summary.call_message_rows(uid, call_id)
    seq_by_id = dict(targets)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_conversation_summary (user_id,watermark_seq) "
            "VALUES (%s,%s)",
            (uid, seq_by_id[covered_id]),
        )
        # Simulate another cleanup winning primary deletion but missing its
        # best-effort shadow mirror before this snapshot-bearing retry runs.
        conn.execute(
            "DELETE FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, absent_id),
        )

    mirrored = []
    monkeypatch.setattr(
        mirror, "execute_many", lambda statements: mirrored.append(statements)
    )
    result = db.chat_delete_uncovered_many(uid, targets)

    assert result == {"deleted": 1, "retained_covered": 1, "remaining": 0}
    assert db.chat_get_strict(uid, covered_id) is not None
    assert db.chat_get_strict(uid, absent_id) is None
    assert db.chat_get_strict(uid, current_id) is None
    assert len(mirrored) == 1
    assert len(mirrored[0]) == 2
    for _sql, params in mirrored[0]:
        assert params[0] == uid
        assert set(params[1]) == {absent_id, current_id}
        assert covered_id not in params[1]


def test_voice_cleanup_serializes_coverage_check_with_compaction_leaf(
    monkeypatch,
):
    """A leaf committing after candidate discovery must protect its rows.

    The compactor holds the ordinary shared chat-user fence while it commits
    the immutable leaf. Voice cleanup must wait on the exclusive form, then
    re-read the advanced watermark before deleting anything. Removing that
    exclusive fence makes the cleanup future finish early and this test fail;
    reading coverage before the fence makes the final retained count fail.
    """
    from model_api_runtime.v2 import jobs_store
    from model_api_runtime.v2 import serve_worker

    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    first = _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })
    second = _append(uid, {
        "role": "openclaw", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT msg_id,seq FROM chat_messages WHERE user_id=%s "
            "AND msg_id=ANY(%s) ORDER BY seq",
            (uid, [first, second]),
        ).fetchall()
    start_seq, end_seq = int(rows[0][1]), int(rows[-1][1])

    helper_entered = threading.Event()
    real_delete = db.chat_delete_uncovered_many

    def observed_delete(user_id, msg_ids):
        helper_entered.set()
        return real_delete(user_id, msg_ids)

    monkeypatch.setattr(db, "chat_delete_uncovered_many", observed_delete)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        with db.get_pool().connection() as compaction_conn:
            with compaction_conn.transaction():
                with compaction_conn.cursor() as cur:
                    db._lock_chat_user_fence_on_cursor(cur, uid)
                    cleanup_future = pool.submit(
                        summary.delete_call_messages, uid, call_id
                    )
                    assert helper_entered.wait(timeout=2)

                    # Prove cleanup reached the guard and is waiting behind
                    # this shared compaction transaction, rather than relying
                    # on thread timing alone.
                    deadline = time.monotonic() + 2
                    waiting = 0
                    while time.monotonic() < deadline and not cleanup_future.done():
                        waiting = int(cur.execute(
                            "SELECT COUNT(*) FROM pg_locks "
                            "WHERE locktype='advisory' "
                            "AND mode='ExclusiveLock' AND NOT granted"
                        ).fetchone()[0])
                        if waiting:
                            break
                        time.sleep(0.01)
                    assert waiting >= 1
                    assert not cleanup_future.done()

                    cur.execute(
                        "INSERT INTO v2_conversation_summary_segments "
                        "(user_id,format_version,coverage_kind,level,start_seq,"
                        "end_seq,source_message_count,legacy_opaque_through_seq,"
                        "child_segment_ids,summary_envelope) "
                        "VALUES (%s,1,'exact',0,%s,%s,2,0,'{}'::BIGINT[],%s) "
                        "RETURNING segment_id",
                        (
                            uid,
                            start_seq,
                            end_seq,
                            db.Jsonb({"plaintext": "covered voice rows"}),
                        ),
                    )
                    segment_id = int(cur.fetchone()[0])
                    cur.execute(
                        "INSERT INTO v2_conversation_summary "
                        "(user_id,summary_envelope,watermark_ts,version,"
                        "watermark_seq,materialized_segment_ids) "
                        "VALUES (%s,%s,%s,1,%s,%s)",
                        (
                            uid,
                            db.Jsonb({"plaintext": "covered voice rows"}),
                            time.time(),
                            end_seq,
                            [segment_id],
                        ),
                    )
        # The transaction commit releases the shared fence, so cleanup can now
        # re-read the new head and make its delete/retain decision.
        result = cleanup_future.result(timeout=2)
    finally:
        pool.shutdown(wait=True)
    assert result == {"deleted": 0, "retained_covered": 2, "remaining": 0}
    assert db.chat_get_strict(uid, first) is not None
    assert db.chat_get_strict(uid, second) is not None
    state = jobs_store.get_summary_frontier_state(uid)
    assert state is not None
    assert len(serve_worker._summary_metadata_frontier(state)) == 1


# --------------------------------------------------------------------------- #
# finalize route: archive -> bounded card -> cleanup -> capture nudge
# --------------------------------------------------------------------------- #

def _run_finalize(monkeypatch, uid: str, call_id: str):
    """Drive the real finalize route against the DB with only the archive write
    and the capture nudge stubbed (both need key material / a live runtime).
    Returns (status, body, calls)."""
    calls = {"archived": 0, "nudge": 0}
    payload = {
        "call_id": call_id,
        "turns": [
            {"role": "user", "text": "这周都在加班赶项目"},
            {"role": "assistant", "text": "记得照顾好自己"},
        ],
        "duration_sec": 240,
    }

    async def _read_json(_request):
        return payload

    monkeypatch.setattr(routes_asgi.asgi_http, "read_json_silent", _read_json)
    from voice import transcript_store

    monkeypatch.setattr(
        transcript_store,
        "exists",
        lambda archived_uid, archived_call_id: (
            str(archived_uid), str(archived_call_id)
        ) in _STUBBED_ARCHIVED_CALLS,
    )

    def _persist(_store, cid, text, *, turn_count, duration_sec, chat_message_id=""):
        calls["archived"] += 1
        _STUBBED_ARCHIVED_CALLS.add((str(uid), str(cid)))
        return {"call_id": cid, "turn_count": turn_count,
                "duration_sec": duration_sec, "char_count": len(text)}

    monkeypatch.setattr(transcript_store, "persist", _persist)

    def _card(_store, preview, mid, cid, *, turn_count=0, duration_sec=0):
        if db.chat_get_strict(uid, mid) is None:
            db.chat_append_strict(uid, mid, time.time(), {
                "id": mid, "role": "openclaw",
                "source": "voice_call_transcript", "voice_call_id": cid,
            }, 200)
        return True

    monkeypatch.setattr(summary, "persist_transcript_card", _card)

    from proactive import proactive_core

    monkeypatch.setattr(
        proactive_core, "capture_force",
        lambda _store: calls.__setitem__("nudge", calls["nudge"] + 1),
    )
    response = asyncio.run(
        routes_asgi.finalize_voice_call(
            SimpleNamespace(), SimpleNamespace(user_id=uid)
        )
    )
    return response.status_code, json.loads(response.body), calls


def test_fresh_finalize_archives_once_and_nudges_capture(monkeypatch):
    """The archive must happen exactly once, and Capture must be kicked so the
    call's memory does not wait out the 20-minute quiet window."""
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })

    status, body, calls = _run_finalize(monkeypatch, uid, call_id)
    assert (status, body["status"], body["replayed"]) == (200, "finalized", False)
    assert calls == {"archived": 1, "nudge": 1}
    # Old clients read summary_message_id; it is dual-written for one release.
    assert body["transcript_message_id"] == body["summary_message_id"]


def test_replayed_finalize_does_not_archive_again(monkeypatch):
    """A retry (card already durable) must not re-archive — the archive insert
    is ON CONFLICT DO NOTHING anyway, but re-running the model-free path twice
    would still be a lie in the logs. Capture may be nudged again: it is
    cursor-driven, so a second nudge cannot double-distil."""
    uid = _seed_user()
    call_id = "vcall_" + uuid.uuid4().hex[:10]
    _append(uid, {
        "role": "user", "source": "model_api",
        "voice_call_id": call_id, "voice_turn_id": "t1",
    })

    _run_finalize(monkeypatch, uid, call_id)
    status, body, calls = _run_finalize(monkeypatch, uid, call_id)
    assert (status, body["replayed"]) == (200, True)
    assert calls["archived"] == 0
