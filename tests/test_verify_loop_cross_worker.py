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

This test pins the fix: verify_loop reloads inside its poll loop, so an ack a
second store instance (a different worker) wrote straight to the DB — never
touching this store's cache — is found before timeout.
"""

from __future__ import annotations

import base64
import itertools
import sys
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
    """verify_loop's poll loop must reload, so an ack persisted by another
    worker (a separate store instance, DB-only) is found within the timeout."""
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
    monkeypatch.setattr(chat_core.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(store, "notify_chat_waiters", lambda *a, **k: None)
    monkeypatch.setattr(
        chat_core, "_maybe_enqueue_resident_introduction", lambda *a, **k: None
    )

    real_reload = store.reload
    fired = {"acked": False}

    def reload_as_if_worker_b():
        # First poll-loop reload: play "worker B" answering the probe. Persist
        # the hidden ack through a SEPARATE store instance, which writes the DB
        # but never touches worker A's cache — exactly the cross-worker split.
        if not fired["acked"]:
            ping = next(
                (
                    m for m in store.chat_messages
                    if m.get("source") == "verify_ping" and m.get("role") == "user"
                ),
                None,
            )
            if ping is not None:
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
        real_reload()

    monkeypatch.setattr(store, "reload", reload_as_if_worker_b)

    body, status = chat_core.verify_loop(store, {"timeout_sec": 10})

    assert status == 200, body
    # If verify_loop never reloaded (the pre-fix behavior), worker B's ack was
    # never injected and would be invisible anyway → passing would be false.
    assert fired["acked"], "verify_loop did not reload inside its poll loop"
    assert body["loop_alive"] is True, body
    assert body["passing"] is True, body
    assert body["response_time_sec"] is not None, body
