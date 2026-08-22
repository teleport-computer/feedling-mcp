"""Regression: /v1/chat/verify_loop must see a hidden verify ack that a
DIFFERENT worker persisted (cross-worker visibility).

Resident-route bug (self-hosted report 2026-08-01):

  /v1/chat/response returns 200 for the hidden verify ack, decrypt health=ok,
  yet verify_loop returns loop_alive=false / response_time_sec=null. A retry
  passes. Intermittent, self-heals.

Root cause: the response-acceptance path
(routes_asgi._allow_verify_reply_with_fresh_pending_check) reloads the store
before its negative decision, so the ack posted through another worker is
persisted + accepted (200). But verify_loop's wait loop polled
``store.chat_messages`` WITHOUT a reload — it only saw the sending worker's
cached list, so a cross-worker ack stayed invisible until a LISTEN/NOTIFY
eviction that may never land inside the ≤60s window. The two paths must agree.

This test pins the optimized fix: verify_loop uses the exact durable ping/ack
lookup, so an ack a second store instance wrote is found without reloading the
worker's full resident state.
"""

from __future__ import annotations

import base64
import itertools
import sys
import threading
import uuid
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi_test_client import make_client  # noqa: E402
from chat import chat_core  # noqa: E402
from chat import consumer as chat_consumer  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from core.store import UserStore  # noqa: E402
from accounts import registry  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(raw), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _env(msg_id: str, user_id: str) -> dict:
    return {
        "id": msg_id,
        "v": 1,
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def test_verify_loop_finds_cross_worker_ack(client, monkeypatch):
    """A DB-only cross-worker ack is found without a full store reload."""
    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)  # "worker A" (cached instance)

    # Resident-route account keeps the synthetic-ping protocol; make the
    # resident decrypt-health gate non-blocking so passing hinges on the ack.
    monkeypatch.setattr(
        chat_consumer, "_consumer_validation_state",
        lambda *a, **k: {"decrypt_health": {"status": "ok"}},
    )
    monkeypatch.setattr(
        chat_consumer, "_decrypt_health_enforcement_state",
        lambda *a, **k: {"blocks_verify": False},
    )
    # Deterministic: no real 2s waits, no wake-bus / introduction side effects.
    monkeypatch.setattr(store, "notify_chat_waiters", lambda *a, **k: None)
    monkeypatch.setattr(
        chat_core, "_maybe_enqueue_resident_introduction", lambda *a, **k: None
    )

    real_verify_reply = chat_core.db.chat_verify_reply_strict
    fired = {"acked": False, "started": False}
    responder_lock = threading.Lock()

    def point_read_as_if_worker_b(read_user_id, ping_id, ping_ts):
        # First exact read: play "worker B" answering the probe. Persist
        # the hidden ack through a SEPARATE store instance, which writes the DB
        # but never touches worker A's cache — exactly the cross-worker split.
        should_answer = False
        with responder_lock:
            ping = chat_core.db.chat_get_strict(read_user_id, ping_id)
            if ping is not None and not fired["started"]:
                fired["started"] = True
                should_answer = True
        if should_answer:
            worker_b = UserStore(user_id)
            # resident_reply_to is the real ack linkage: it persists via
            # db.chat_append_resident_reply, sets reply_to_message_id, and
            # CAS-marks the ping — exactly what /v1/chat/response does.
            worker_b.append_chat(
                "openclaw",
                "verify_ping",
                _env("wb_ack_" + uuid.uuid4().hex[:8], user_id),
                resident_reply_to=ping["id"],
            )
            fired["acked"] = True
        return real_verify_reply(read_user_id, ping_id, ping_ts)

    monkeypatch.setattr(
        store,
        "reload",
        lambda: (_ for _ in ()).throw(AssertionError("full reload is forbidden")),
    )
    monkeypatch.setattr(chat_core.db, "chat_verify_reply_strict", point_read_as_if_worker_b)

    body, status = chat_core.verify_loop(store, {"timeout_sec": 10})

    assert status == 200, body
    assert fired["acked"], "verify_loop did not perform the exact durable lookup"
    assert body["loop_alive"] is True, body
    assert body["passing"] is True, body
    assert body["response_time_sec"] is not None, body
