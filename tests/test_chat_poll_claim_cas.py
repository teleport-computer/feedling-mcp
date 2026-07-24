"""Cross-worker safety of the chat-poll reply claim (chat.service +
db.chat_try_claim_reply).

Before: claimability was read from the in-process cache, then written through.
Two workers polling the same AI reply would each read "unclaimed" and both
deliver it. Now the claim is a conditional UPDATE in the DB, so exactly one
caller (consumer/worker) can win. These tests pin that invariant; the two
callers here stand in for two workers' pollers sharing one Postgres.

Run:  python -m pytest tests/test_chat_poll_claim_cas.py -q
"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import store as core_store  # noqa: E402
from core import config as core_config  # noqa: E402
from chat import service as chat_service  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    uid = res.get_json()["user_id"]
    return core_store.get_store(uid)


def _append_reply(store, msg_id: str) -> None:
    """Append a pending user message through the store (cache + DB). A poll
    "claims the reply" to such a message — only role==user, not-yet-replied
    messages are claimable (see chat.service._chat_message_claimable)."""
    store.append_chat(
        role="user",
        source="chat",
        envelope={"id": msg_id, "v": 1, "body_ct": "x", "nonce": "x", "K_user": "x"},
    )


def _poll(store, consumer_id: str, *, claim: bool = True):
    return chat_service._pending_chat_messages_for_poll(
        store, since=0.0, consumer_id=consumer_id, claim=claim
    )


def test_two_consumers_only_one_claims(store):
    _append_reply(store, "m_reply_1")
    a = [m["id"] for m in _poll(store, "consumer-A")]
    b = [m["id"] for m in _poll(store, "consumer-B")]
    assert "m_reply_1" in a
    assert "m_reply_1" not in b  # B lost the claim — no double delivery


def test_same_consumer_can_reclaim(store):
    _append_reply(store, "m_reply_2")
    first = [m["id"] for m in _poll(store, "consumer-A")]
    again = [m["id"] for m in _poll(store, "consumer-A")]
    assert "m_reply_2" in first
    assert "m_reply_2" in again  # idempotent re-claim by the same consumer


def test_claim_releases_after_expiry(store, monkeypatch):
    _append_reply(store, "m_reply_3")
    assert "m_reply_3" in [m["id"] for m in _poll(store, "consumer-A")]
    # Fast-forward past the claim TTL: a different consumer can now take it.
    monkeypatch.setattr(
        chat_service, "CHAT_POLL_CLAIM_TTL_SEC", -10_000, raising=False
    )
    # The prior claim's expiry was stamped with the old TTL; advance wall clock
    # by polling with a time far in the future via monkeypatched time.
    real_time = time.time
    monkeypatch.setattr(chat_service.time, "time", lambda: real_time() + 10_000)
    assert "m_reply_3" in [m["id"] for m in _poll(store, "consumer-B")]


def test_claim_cas_rejects_already_replied_in_db(store):
    # Simulate a cross-worker race: another worker has already posted the reply
    # (reply_status='replied' persisted to the DB) but this worker's cache is
    # stale. The DB CAS itself must refuse the claim — not rely on the cache
    # pre-gate — so the answered message isn't handled twice.
    import db
    _append_reply(store, "m_reply_5")
    db.chat_update_metadata(store.user_id, "m_reply_5", {"reply_status": "replied"})
    now = time.time()
    won = db.chat_try_claim_reply(
        store.user_id, "m_reply_5", "consumer-A", now,
        {"reply_claimed_by": "consumer-A", "reply_claimed_at": f"{now:.3f}",
         "reply_claim_expires_at": f"{now + 120:.3f}"},
    )
    assert won is None  # replied row is not claimable at the DB level

    # Same via reply_message_id (the other replied marker).
    _append_reply(store, "m_reply_6")
    db.chat_update_metadata(store.user_id, "m_reply_6", {"reply_message_id": "resp_1"})
    won2 = db.chat_try_claim_reply(
        store.user_id, "m_reply_6", "consumer-A", now,
        {"reply_claimed_by": "consumer-A"},
    )
    assert won2 is None


def test_peek_does_not_claim(store):
    _append_reply(store, "m_reply_4")
    # claim=False is a read-only peek: both callers see it, neither locks it.
    a = [m["id"] for m in _poll(store, "consumer-A", claim=False)]
    b = [m["id"] for m in _poll(store, "consumer-B", claim=False)]
    assert "m_reply_4" in a and "m_reply_4" in b
    # A real claim afterwards still succeeds (the peek didn't lock anything).
    assert "m_reply_4" in [m["id"] for m in _poll(store, "consumer-A")]


# ---- Resident↔V2 flip-window guard (chat.service claim gate) ----

def test_db_action_v2_claiming_poll_gated_but_readonly_still_delivers(store):
    """A db_action_v2 user is served ONLY by the V2 worker pool, not the resident
    consumer. The resident's CLAIMING poll must return nothing for such a user
    (so a still-running resident consumer in the ~15s flip window can't claim +
    reply alongside the V2 worker, which reads independently by seq cursor and
    ignores reply_claimed_by — that would double-reply). A read-only poll (the
    client fetching the V2 reply) is unaffected."""
    import db  # noqa: E402
    db.patch_blob_strict(
        store.user_id,
        "model_api_runtime",
        {"hosted_runtime_mode": "db_action_v2"},
        runtime_state_target="v2",
    )
    _append_reply(store, "m_v2_1")

    # resident consumer's claiming poll: gated → nothing claimed
    assert _poll(store, "agent-runner:x", claim=True) == []
    # read-only poll still sees the pending message (client reads the V2 reply path)
    assert [m["id"] for m in _poll(store, "client", claim=False)] == ["m_v2_1"]
    # and because the claiming poll was gated, the row is still unclaimed, so a
    # subsequent read-only poll continues to deliver it (not stuck behind a claim)
    assert [m["id"] for m in _poll(store, "client", claim=False)] == ["m_v2_1"]


def test_resident_cli_claiming_poll_unaffected_by_guard(store):
    """The default (resident_cli) user's claiming poll is unchanged — the guard
    only fires for explicit db_action_v2 users."""
    _append_reply(store, "m_res_1")
    assert [m["id"] for m in _poll(store, "agent-runner:x", claim=True)] == ["m_res_1"]


def test_runtime_control_read_failure_never_grants_resident_claim(store, monkeypatch):
    from hosted import config_store as hosted_config_store  # noqa: E402

    _append_reply(store, "m_control_down")
    monkeypatch.setattr(
        hosted_config_store,
        "get_hosted_runtime_control_strict",
        lambda _store: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    assert _poll(store, "agent-runner:x", claim=True) == []
    assert [m["id"] for m in _poll(store, "client", claim=False)] == ["m_control_down"]
