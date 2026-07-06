"""Unanswered-turn redelivery backstop in chat.service._pending_chat_messages_for_poll.

Before: message visibility was solely `ts > since` — a consumer whose cursor
passed an unanswered user message (respawn re-seed, checkpoint advanced before
the reply, wedge skip) lost that turn forever; the claim CAS machinery never
saw it again. Now messages with `ts <= since` are still candidates when they
are (a) claimable, (b) within CHAT_REDELIVERY_WINDOW_SEC, and (c) on the
unanswered tail — no NEWER visible user message has already been replied
(conversation moved past them ⇒ superseded, never redelivered).

Run:  python -m pytest tests/test_chat_poll_redelivery.py -q
"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402
from core import store as core_store  # noqa: E402
from core import config as core_config  # noqa: E402
from chat import service as chat_service  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    appmod._users[:] = []
    appmod._key_to_user.clear()
    appmod._stores.clear()
    appmod._save_users()
    res = appmod.app.test_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    uid = res.get_json()["user_id"]
    return core_store.get_store(uid)


def _append_user_msg(store, msg_id: str, *, source: str = "chat") -> dict:
    return store.append_chat(
        role="user",
        source=source,
        envelope={"id": msg_id, "v": 1, "body_ct": "x", "nonce": "x", "K_user": "x"},
    )


def _mark_replied(store, msg_id: str) -> None:
    store.update_chat_message_metadata(
        msg_id, {"reply_status": "replied", "reply_message_id": f"resp_{msg_id}"}
    )


def _poll(store, consumer_id: str = "consumer-A", *, since: float, claim: bool = True):
    return chat_service._pending_chat_messages_for_poll(
        store, since=since, consumer_id=consumer_id, claim=claim
    )


def _cursor_past(msg: dict) -> float:
    """A consumer cursor that has already moved past `msg` (the loss scenario)."""
    return float(msg["ts"]) + 0.001


def test_unanswered_message_behind_cursor_is_redelivered(store):
    msg = _append_user_msg(store, "m_lost_1")
    got = [m["id"] for m in _poll(store, since=_cursor_past(msg))]
    assert "m_lost_1" in got


def test_replied_message_behind_cursor_is_not_redelivered(store):
    msg = _append_user_msg(store, "m_done_1")
    _mark_replied(store, "m_done_1")
    got = [m["id"] for m in _poll(store, since=_cursor_past(msg))]
    assert got == []


def test_newer_replied_user_message_supersedes_older_unanswered(store):
    _append_user_msg(store, "m_old_unanswered")
    newer = _append_user_msg(store, "m_new_replied")
    _mark_replied(store, "m_new_replied")
    got = [m["id"] for m in _poll(store, since=_cursor_past(newer))]
    assert got == []  # conversation moved past m_old_unanswered — never redeliver


def test_redelivery_outside_window_is_dropped(store, monkeypatch):
    msg = _append_user_msg(store, "m_ancient")
    since = _cursor_past(msg)
    # Move the service's wall clock past the window: the message is now stale.
    monkeypatch.setattr(chat_service, "CHAT_REDELIVERY_WINDOW_SEC", 100)
    real_time = time.time
    monkeypatch.setattr(chat_service.time, "time", lambda: real_time() + 101)
    got = [m["id"] for m in _poll(store, since=since)]
    assert got == []


def test_redelivery_disabled_when_window_zero(store, monkeypatch):
    msg = _append_user_msg(store, "m_disabled")
    monkeypatch.setattr(chat_service, "CHAT_REDELIVERY_WINDOW_SEC", 0)
    got = [m["id"] for m in _poll(store, since=_cursor_past(msg))]
    assert got == []


def test_verify_ping_row_is_not_redelivered(store):
    ping = _append_user_msg(store, "m_ping", source="verify_ping")
    got = [m["id"] for m in _poll(store, since=_cursor_past(ping))]
    assert got == []  # late ping redelivery would only 409 at chat/response


def test_verify_ping_reply_does_not_supersede(store):
    _append_user_msg(store, "m_real_lost")
    ping = _append_user_msg(store, "m_ping_2", source="verify_ping")
    _mark_replied(store, "m_ping_2")  # verify_loop answered its synthetic probe
    got = [m["id"] for m in _poll(store, since=_cursor_past(ping))]
    assert got == ["m_real_lost"]  # a hidden liveness probe is not conversation


def test_backstop_claim_not_rebypassed_by_same_consumer(store):
    # The `claimed_by == consumer_id` idempotent-reclaim bypass is for the
    # ts > since path (poll retry of a fresh delivery). On the backstop path it
    # would re-hand the same lost message to its claimer on EVERY poll while
    # the turn is still running — so a live claim blocks redelivery even to
    # its own consumer; retry only after TTL expiry.
    msg = _append_user_msg(store, "m_lost_2")
    since = _cursor_past(msg)
    assert "m_lost_2" in [m["id"] for m in _poll(store, "consumer-A", since=since)]
    assert [m["id"] for m in _poll(store, "consumer-A", since=since)] == []


def test_backstop_claim_releases_after_ttl(store, monkeypatch):
    msg = _append_user_msg(store, "m_lost_3")
    since = _cursor_past(msg)
    assert "m_lost_3" in [m["id"] for m in _poll(store, "consumer-A", since=since)]
    # Past the claim TTL (but still inside the redelivery window): retry allowed.
    real_time = time.time
    monkeypatch.setattr(chat_service, "CHAT_REDELIVERY_WINDOW_SEC", 100_000)
    monkeypatch.setattr(chat_service.time, "time", lambda: real_time() + 10_000)
    assert "m_lost_3" in [m["id"] for m in _poll(store, "consumer-B", since=since)]


def test_redelivered_and_new_messages_return_ts_ascending(store):
    old = _append_user_msg(store, "m_lost_4")
    since = _cursor_past(old)
    _append_user_msg(store, "m_fresh")
    got = [m["id"] for m in _poll(store, since=since)]
    assert got == ["m_lost_4", "m_fresh"]


def test_backstop_blocks_same_consumer_even_with_stale_cache(store):
    # Multi-worker: the live-claim pre-check reads THIS worker's cache, but the
    # claim may have been taken through another worker. Simulate that worker's
    # stale cache by wiping the claim fields from the in-memory copy while the
    # DB still holds them — the DB CAS must stay authoritative and refuse to
    # re-hand the in-flight message even to its own consumer_id.
    msg = _append_user_msg(store, "m_lost_5")
    since = _cursor_past(msg)
    assert "m_lost_5" in [m["id"] for m in _poll(store, "consumer-A", since=since)]
    with store.chat_lock:
        for m in store.chat_messages:
            if m.get("id") == "m_lost_5":
                m.pop("reply_claimed_by", None)
                m.pop("reply_claimed_at", None)
                m.pop("reply_claim_expires_at", None)
    assert [m["id"] for m in _poll(store, "consumer-A", since=since)] == []


def test_strict_claim_rejects_same_consumer_unexpired(store):
    # DB-level pin of the same invariant (mirrors
    # test_claim_cas_rejects_already_replied_in_db's style).
    import db

    _append_user_msg(store, "m_lost_6")
    now = time.time()
    fields = {
        "reply_claimed_by": "consumer-A",
        "reply_claimed_at": f"{now:.3f}",
        "reply_claim_expires_at": f"{now + 600:.3f}",
    }
    assert db.chat_try_claim_reply(store.user_id, "m_lost_6", "consumer-A", now, fields)
    refreshed = db.chat_try_claim_reply(
        store.user_id, "m_lost_6", "consumer-A", now, fields,
        redelivery=True,
    )
    assert refreshed is None
    # The normal (fresh-delivery) semantics keep the idempotent self-refresh.
    assert db.chat_try_claim_reply(store.user_id, "m_lost_6", "consumer-A", now, fields)


def test_supersede_blocks_redelivery_even_with_stale_cache(store):
    # Multi-worker: _redelivery_floor reads THIS worker's cache, but the parent
    # reply_status metadata update after a reply append is not broadcast — a
    # stale worker may not know the conversation already moved past the old
    # message. Simulate by wiping the newer message's replied markers from the
    # cache while the DB keeps them: the claim CAS itself must refuse to
    # redeliver (superseded is decided at claim time, authoritatively).
    _append_user_msg(store, "m_old_lost")
    newer = _append_user_msg(store, "m_new_done")
    _mark_replied(store, "m_new_done")
    with store.chat_lock:
        for m in store.chat_messages:
            if m.get("id") == "m_new_done":
                m.pop("reply_status", None)
                m.pop("reply_message_id", None)
    got = [m["id"] for m in _poll(store, since=_cursor_past(newer))]
    assert got == []


def test_redelivery_claim_rejects_when_newer_user_message_replied_db(store):
    # DB-level pin: a redelivery claim on an old message must fail when ANY
    # newer visible user message is already replied — regardless of caches.
    import db

    _append_user_msg(store, "m_old_lost_2")
    _append_user_msg(store, "m_new_done_2")
    _mark_replied(store, "m_new_done_2")
    now = time.time()
    fields = {
        "reply_claimed_by": "consumer-A",
        "reply_claimed_at": f"{now:.3f}",
        "reply_claim_expires_at": f"{now + 600:.3f}",
    }
    assert db.chat_try_claim_reply(
        store.user_id, "m_old_lost_2", "consumer-A", now, fields, redelivery=True,
    ) is None
    # A replied verify_ping probe is not conversation and must NOT supersede.
    _append_user_msg(store, "m_old_lost_3")
    _append_user_msg(store, "m_ping_done", source="verify_ping")
    _mark_replied(store, "m_ping_done")
    assert db.chat_try_claim_reply(
        store.user_id, "m_old_lost_3", "consumer-A", now, fields, redelivery=True,
    )


def test_redelivery_batch_is_capped_and_rolls(store, monkeypatch):
    # The consumer runs ONE agent turn per message (30-90s each): claiming a
    # big recovery batch in one poll would let the tail's claims expire before
    # they're processed (duplicate turns) and outgrow the decrypt fetch. So a
    # poll claims at most CHAT_REDELIVERY_BATCH_MAX backstop messages, oldest
    # first; the REST STAY UNCLAIMED and roll into the next poll immediately —
    # no TTL stall.
    monkeypatch.setattr(chat_service, "CHAT_REDELIVERY_BATCH_MAX", 2)
    for i in range(1, 6):
        _append_user_msg(store, f"m_batch_{i}")
    since = time.time() + 0.001
    assert [m["id"] for m in _poll(store, since=since)] == ["m_batch_1", "m_batch_2"]
    assert [m["id"] for m in _poll(store, since=since)] == ["m_batch_3", "m_batch_4"]
    assert [m["id"] for m in _poll(store, since=since)] == ["m_batch_5"]
    assert [m["id"] for m in _poll(store, since=since)] == []


def test_fresh_messages_not_capped_by_redelivery_budget(store, monkeypatch):
    # The budget bounds RECOVERY work only; the live conversation (ts > since)
    # must never be dropped by it.
    monkeypatch.setattr(chat_service, "CHAT_REDELIVERY_BATCH_MAX", 1)
    _append_user_msg(store, "m_lost_a")
    lost_b = _append_user_msg(store, "m_lost_b")
    since = float(lost_b["ts"])  # exactly at the cursor: lost <= since < fresh
    _append_user_msg(store, "m_fresh_1")
    _append_user_msg(store, "m_fresh_2")
    got = [m["id"] for m in _poll(store, since=since)]
    assert got == ["m_lost_a", "m_fresh_1", "m_fresh_2"]


def test_claim_ttl_default_covers_long_turns():
    # With redelivery live, an expired claim means a duplicate agent turn
    # (double provider burn; the 409 only blocks the double append). The TTL
    # must therefore sit above the longest normal turn, not at 120s.
    assert chat_service.CHAT_POLL_CLAIM_TTL_SEC == 600
