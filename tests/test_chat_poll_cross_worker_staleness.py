"""/v1/chat/poll must not answer "nothing pending" from a stale worker cache.

2026-07-22 prod: a user message was committed to Postgres at 23:16:28 and the
resident consumer long-polled with the correct cursor every ~33s — four
consecutive polls returned empty (no enclave decrypt fetch in between, so the
polls genuinely carried nothing) and the message was only served at 23:19:30,
three minutes later. The message row existed the whole time; what the poll read
was this worker's in-memory ``store.chat_messages``, which had not been
refreshed. Cross-worker freshness rides on LISTEN/NOTIFY → ``_evict_store``;
when that wake is lost (listener reconnect window, or a notify emitted while
this worker had no live LISTEN), the cache stays stale until the 900s TTL and
every poll in between truthfully reports "nothing" about a message that exists.

The invariant under test: a poll that is about to answer *empty* must confirm
that against the database, not only against the cache. The message here is
written straight through ``db.chat_append`` — exactly the state another worker's
write leaves this worker's cache in.

Run:  python -m pytest tests/test_chat_poll_cross_worker_staleness.py -q
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry as accounts_registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    accounts_registry._users[:] = []
    accounts_registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x33" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _write_user_message_bypassing_cache(user_id: str, msg_id: str, ts: float) -> None:
    """Commit a user message the way ANOTHER worker would: the row lands in
    Postgres, this process's cached store never hears about it."""
    doc = {
        "id": msg_id,
        "role": "user",
        "source": "chat",
        "ts": ts,
        "v": 1,
        "content_type": "text",
        "visibility": "shared",
        "owner_user_id": user_id,
        "body_ct": "x",
        "nonce": "x",
        "K_user": "x",
    }
    db.chat_append(user_id, msg_id, ts, doc, core_store.MAX_CHAT_MESSAGES)


def test_poll_serves_message_committed_outside_this_workers_cache(user):
    uid, api_key = user
    client = make_client()

    # Warm this worker's cache, exactly as an idle poll cycle does.
    warm = client.get("/v1/chat/poll?since=0&timeout=0", headers={"X-API-Key": api_key})
    assert warm.status_code == 200, warm.get_data(as_text=True)
    assert warm.get_json()["messages"] == []

    ts = time.time()
    _write_user_message_bypassing_cache(uid, "m_other_worker", ts)
    # Precondition: this is genuinely the stale-cache state — the row is in the
    # DB and absent from the cache. (If this ever fails the test is no longer
    # reproducing the prod condition.)
    assert all(m.get("id") != "m_other_worker" for m in core_store.get_store(uid).chat_messages)
    assert any(m.get("id") == "m_other_worker" for m in db.chat_load(uid))

    res = client.get("/v1/chat/poll?since=0&timeout=0", headers={"X-API-Key": api_key})
    assert res.status_code == 200, res.get_data(as_text=True)
    delivered = [m.get("id") for m in res.get_json()["messages"]]
    assert "m_other_worker" in delivered, (
        "poll answered 'nothing pending' from a stale cache while the message "
        "was committed in Postgres — this is the 3-minute silent gap"
    )


def test_poll_serves_a_newer_db_only_message_alongside_a_cached_one(user):
    """Partial staleness: the cache yields *something*, so the gate that only
    probes on an EMPTY result never fires — and the newer row keeps waiting.

    Same cross-worker cause as above, one message later: this worker saw A but
    missed the NOTIFY for B. A poll that answers with A alone is not "nothing
    pending", so the empty-only probe stays silent and B waits out the TTL.
    """
    uid, api_key = user
    client = make_client()

    store = core_store.get_store(uid)
    now = time.time()
    # A: this worker knows about it (DB + cache), unclaimed.
    a_doc = {
        "id": "m_cached", "role": "user", "source": "chat", "ts": now - 60, "v": 1,
        "content_type": "text", "visibility": "shared", "owner_user_id": uid,
        "body_ct": "x", "nonce": "x", "K_user": "x",
    }
    db.chat_append(uid, "m_cached", a_doc["ts"], a_doc, core_store.MAX_CHAT_MESSAGES)
    with store.chat_lock:
        store.chat_messages.append(a_doc)
    # B: committed by another worker, newer, absent from this cache.
    _write_user_message_bypassing_cache(uid, "m_db_only", now - 30)

    res = client.get("/v1/chat/poll?since=0&timeout=0", headers={"X-API-Key": api_key})
    assert res.status_code == 200, res.get_data(as_text=True)
    delivered = [m.get("id") for m in res.get_json()["messages"]]
    assert "m_cached" in delivered
    assert "m_db_only" in delivered, (
        "a non-empty poll skipped the staleness probe, so the newer message "
        "committed by another worker was left waiting"
    )


def test_selfheal_recheck_does_not_swallow_a_redelivered_message(user):
    """A redelivered message handed out by the first pass must survive the
    self-heal re-check.

    Redelivery (``ts <= since``) claims are deliberately strict: ``db.
    chat_try_claim_reply(redelivery=True)`` drops the same-consumer clause, so a
    LIVE claim blocks redelivery *even to its own claimer* (re-handing a message
    to a consumer whose turn is still running would double-burn). That makes the
    re-check non-idempotent for this class: pass 1 delivers X and stamps a 600s
    claim, pass 2 is then refused it. If the re-check REPLACES the pending list,
    X is claimed-but-never-delivered — the exact silent-drop shape this whole
    file exists to kill, reintroduced by the fix for it.
    """
    uid, api_key = user
    client = make_client()

    store = core_store.get_store(uid)
    now = time.time()
    # X: an unanswered turn the consumer's cursor already passed — the
    # redelivery backstop's job. Known to this worker (DB + cache).
    x_doc = {
        "id": "m_redelivered", "role": "user", "source": "chat", "ts": now - 120, "v": 1,
        "content_type": "text", "visibility": "shared", "owner_user_id": uid,
        "body_ct": "x", "nonce": "x", "K_user": "x",
    }
    db.chat_append(uid, "m_redelivered", x_doc["ts"], x_doc, core_store.MAX_CHAT_MESSAGES)
    with store.chat_lock:
        store.chat_messages.append(x_doc)
    # Y: committed by another worker after the cursor — makes the cache stale,
    # so the self-heal probe fires and the re-check runs.
    _write_user_message_bypassing_cache(uid, "m_db_only_newer", now - 30)

    res = client.get(
        f"/v1/chat/poll?since={now - 60:.3f}&timeout=0", headers={"X-API-Key": api_key}
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    delivered = [m.get("id") for m in res.get_json()["messages"]]
    assert "m_db_only_newer" in delivered
    assert "m_redelivered" in delivered, (
        "the self-heal re-check dropped a message the first pass had already "
        "claimed — claimed and never delivered, silently"
    )


def test_history_since_returns_a_newer_db_only_message_alongside_a_cached_one(user):
    """Same partial staleness on the read path the app uses.

    /v1/chat/history has probed since 2026-07-15, but only when its since-window
    came back EMPTY. One cached row is enough to silence the probe, so the app
    renders a partial tail and the newer message stays invisible until the TTL —
    "灵动岛弹了但进 App 没有", one message later.
    """
    uid, api_key = user
    client = make_client()

    store = core_store.get_store(uid)
    now = time.time()
    a_doc = {
        "id": "h_cached", "role": "user", "source": "chat", "ts": now - 60, "v": 1,
        "content_type": "text", "visibility": "shared", "owner_user_id": uid,
        "body_ct": "x", "nonce": "x", "K_user": "x",
    }
    db.chat_append(uid, "h_cached", a_doc["ts"], a_doc, core_store.MAX_CHAT_MESSAGES)
    with store.chat_lock:
        store.chat_messages.append(a_doc)
    _write_user_message_bypassing_cache(uid, "h_db_only", now - 30)

    res = client.get(
        f"/v1/chat/history?since={now - 90:.3f}&limit=40", headers={"X-API-Key": api_key}
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    ids = [m.get("id") for m in res.get_json()["messages"]]
    assert "h_cached" in ids
    assert "h_db_only" in ids, (
        "a non-empty since-window skipped the staleness probe — the app cannot "
        "see a message another worker committed"
    )
