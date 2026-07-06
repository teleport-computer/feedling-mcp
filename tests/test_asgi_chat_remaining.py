"""Native ASGI parity + wake for the remaining chat routes (plan §9.1).

Covers the six routes migrated onto the chat ASGI router — /v1/chat/message,
/response, GET+DELETE /history, /messages/<id>/body, /verify_loop — asserting:

- **parity** with the Flask oracle (status + body; envelopes stay opaque
  ciphertext, never decrypted);
- **auth failure** is the fixed-body 401;
- **validation** errors match Flask byte-for-byte;
- the **/response bootstrap gate** produces the same 409 body under ASGI as
  Flask (proves the throwaway-app jsonify bridge in chat_core does not drift);
- a native /v1/chat/poll parked waiter is **woken by a native /v1/chat/message
  write on the same worker** (the whole migration payoff — reuses the hook
  injection from test_asgi_poll_native);
- the **/response reply-claim** marks the answered user message replied so a
  later claiming poll never re-delivers it (no double-deliver CAS).
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
from accounts import registry as accounts_registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from bootstrap import gates as boot_gates  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from runtime.waiters import registry  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _hk(api_key: str) -> dict:
    return {"X-API-Key": api_key}


def _env(user_id: str, marker: str, *, visibility: str = "shared") -> dict:
    env = {
        "v": 1,
        "id": marker,
        "body_ct": _b64(f"{user_id}:{marker}".encode()),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 32),
        "visibility": visibility,
        "owner_user_id": user_id,
    }
    if visibility == "shared":
        env["K_enclave"] = _b64(b"\x02" * 32)
    return env


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    accounts_registry._users[:] = []
    accounts_registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    # Inject the async wake hook the way the lifespan would (ASGITransport does
    # not run the lifespan) so a same-worker write wakes a parked async poll.
    core_store.set_async_wake_hook(registry.wake)
    yield body["user_id"], body["api_key"]
    core_store.set_async_wake_hook(None)


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app.app), base_url="http://t"
    )


def _flask_client():
    return make_client()


async def _wait_until_parked(expected: int, timeout: float = 2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if registry.active_count() >= expected:
            return True
        await asyncio.sleep(0.005)
    return False


def _asgi(method: str, path: str, *, headers=None, json=None, params=None):
    async def go():
        async with _client() as c:
            resp = await c.request(method, path, headers=headers or {}, json=json, params=params)
            return resp.status_code, resp.json()

    return asyncio.run(go())


# --------------------------------------------------------------------------- #
# auth failure (fixed-body 401 on every route)
# --------------------------------------------------------------------------- #

def test_all_routes_bad_auth_is_fixed_401(user):
    bad = {"X-API-Key": "nope"}
    for method, path, body in [
        ("POST", "/v1/chat/message", {"envelope": {}}),
        ("POST", "/v1/chat/response", {}),
        ("GET", "/v1/chat/history", None),
        ("DELETE", "/v1/chat/history", {"confirm": "clear-chat-history"}),
        ("GET", "/v1/chat/messages/whatever/body", None),
        ("POST", "/v1/chat/verify_loop", {"timeout_sec": 0}),
    ]:
        status, resp = _asgi(method, path, headers=bad, json=body)
        assert status == 401, (path, resp)
        assert resp == {"error": "unauthorized"}, (path, resp)


# --------------------------------------------------------------------------- #
# /v1/chat/message: validation parity + success shape
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("payload", [
    {},
    {"envelope": {}},
    {"envelope": {"body_ct": "x", "nonce": "n", "K_user": "k", "visibility": "shared", "owner_user_id": "u"}},
    {"envelope": {"body_ct": "x", "nonce": "n", "K_user": "k", "visibility": "weird", "owner_user_id": "u", "K_enclave": "e"}},
    {"envelope": {"body_ct": "x", "nonce": "n", "K_user": "k", "visibility": "shared", "owner_user_id": "u", "K_enclave": "e"}, "content_type": "video"},
])
def test_message_validation_parity(user, payload):
    _uid, api_key = user
    a_status, a_body = _asgi("POST", "/v1/chat/message", headers=_hk(api_key), json=payload)
    f = _flask_client().post("/v1/chat/message", headers=_hk(api_key), json=payload)
    assert (a_status, a_body) == (f.status_code, f.get_json())
    assert a_status == 400


def test_message_success_and_opaque_envelope(user):
    uid, api_key = user
    env = _env(uid, "m1")
    a_status, a_body = _asgi("POST", "/v1/chat/message", headers=_hk(api_key), json={"envelope": env})
    assert a_status == 200
    assert a_body["id"] == "m1" and a_body["v"] == 1 and a_body["ts"] > 0
    # the server stored the ciphertext verbatim (never decrypted)
    store = core_store.get_store(uid)
    with store.chat_lock:
        stored = [m for m in store.chat_messages if m["id"] == "m1"][0]
    assert stored["body_ct"] == env["body_ct"]
    assert stored["role"] == "user"


# --------------------------------------------------------------------------- #
# GET /v1/chat/history + /messages/<id>/body: parity (read same store)
# --------------------------------------------------------------------------- #

def test_history_and_message_body_parity(user):
    uid, api_key = user
    # Seed two messages through the native ASGI write path.
    for marker in ("h1", "h2"):
        s, _ = _asgi("POST", "/v1/chat/message", headers=_hk(api_key), json={"envelope": _env(uid, marker)})
        assert s == 200

    a_status, a_body = _asgi("GET", "/v1/chat/history", headers=_hk(api_key))
    f = _flask_client().get("/v1/chat/history", headers=_hk(api_key))
    assert a_status == f.status_code == 200
    assert a_body == f.get_json()  # identical bodies over the same store
    assert a_body["total"] == 2
    ids = [m["id"] for m in a_body["messages"]]
    assert ids == ["h1", "h2"]
    # opaque envelope surfaced verbatim
    assert a_body["messages"][0]["body_ct"] == _env(uid, "h1")["body_ct"]

    a_status, a_body = _asgi("GET", "/v1/chat/messages/h1/body", headers=_hk(api_key))
    f = _flask_client().get("/v1/chat/messages/h1/body", headers=_hk(api_key))
    assert a_status == f.status_code == 200
    assert a_body == f.get_json()
    assert a_body["message"]["body_ct"] == _env(uid, "h1")["body_ct"]


def test_history_invalid_limit_parity(user):
    _uid, api_key = user
    a_status, a_body = _asgi("GET", "/v1/chat/history", headers=_hk(api_key), params={"limit": "abc"})
    f = _flask_client().get("/v1/chat/history?limit=abc", headers=_hk(api_key))
    assert (a_status, a_body) == (f.status_code, f.get_json()) == (400, {"error": "invalid limit"})


def test_message_body_not_found_parity(user):
    _uid, api_key = user
    a_status, a_body = _asgi("GET", "/v1/chat/messages/missing/body", headers=_hk(api_key))
    f = _flask_client().get("/v1/chat/messages/missing/body", headers=_hk(api_key))
    assert (a_status, a_body) == (f.status_code, f.get_json()) == (404, {"error": "message_not_found"})


# --------------------------------------------------------------------------- #
# DELETE /v1/chat/history: confirm-gate parity + actual clear
# --------------------------------------------------------------------------- #

def test_history_clear_confirmation_parity(user):
    _uid, api_key = user
    a_status, a_body = _asgi("DELETE", "/v1/chat/history", headers=_hk(api_key), json={})
    f = _flask_client().delete("/v1/chat/history", headers=_hk(api_key), json={})
    assert (a_status, a_body) == (f.status_code, f.get_json())
    assert a_status == 400 and a_body["error"] == "confirmation_required"


def test_history_clear_success(user):
    uid, api_key = user
    _asgi("POST", "/v1/chat/message", headers=_hk(api_key), json={"envelope": _env(uid, "c1")})
    a_status, a_body = _asgi(
        "DELETE", "/v1/chat/history", headers=_hk(api_key), json={"confirm": "clear-chat-history"}
    )
    assert a_status == 200 and a_body["cleared"] is True
    store = core_store.get_store(uid)
    with store.chat_lock:
        assert store.chat_messages == []


# --------------------------------------------------------------------------- #
# POST /v1/chat/response: bootstrap-gate parity (fresh user has no identity)
# --------------------------------------------------------------------------- #

def test_response_gate_parity_needs_identity(user):
    _uid, api_key = user
    a_status, a_body = _asgi("POST", "/v1/chat/response", headers=_hk(api_key), json={})
    f = _flask_client().post("/v1/chat/response", headers=_hk(api_key), json={})
    assert a_status == f.status_code == 409
    assert a_body == f.get_json()
    assert a_body["stage"] == "needs_identity"
    assert a_body["error"] == "bootstrap_incomplete"


def test_response_validation_parity_gate_bypassed(user, monkeypatch):
    # Bypass the bootstrap gate (as the proactive endpoint tests do) so the
    # envelope validation path is reached identically under both frameworks.
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    _uid, api_key = user
    payload = {"envelope": {"body_ct": "x", "nonce": "n", "K_user": "k", "visibility": "shared", "owner_user_id": "u"}}
    a_status, a_body = _asgi("POST", "/v1/chat/response", headers=_hk(api_key), json=payload)
    f = _flask_client().post("/v1/chat/response", headers=_hk(api_key), json=payload)
    assert (a_status, a_body) == (f.status_code, f.get_json())
    assert a_status == 400


def test_response_success_gate_bypassed(user, monkeypatch):
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    uid, api_key = user
    a_status, a_body = _asgi(
        "POST", "/v1/chat/response", headers=_hk(api_key),
        json={"envelope": _env(uid, "r1"), "source": "chat"},
    )
    assert a_status == 200
    assert a_body["id"] == "r1" and a_body["v"] == 1
    store = core_store.get_store(uid)
    with store.chat_lock:
        stored = [m for m in store.chat_messages if m["id"] == "r1"][0]
    assert stored["role"] == "openclaw"
    assert stored["body_ct"] == _env(uid, "r1")["body_ct"]


# --------------------------------------------------------------------------- #
# POST /v1/chat/verify_loop: parity (fast, timeout_sec=0 → no reply)
# --------------------------------------------------------------------------- #

def test_verify_loop_parity(user):
    _uid, api_key = user
    a_status, a_body = _asgi("POST", "/v1/chat/verify_loop", headers=_hk(api_key), json={"timeout_sec": 0})
    f = _flask_client().post("/v1/chat/verify_loop", headers=_hk(api_key), json={"timeout_sec": 0})
    assert a_status == f.status_code == 200
    # ping_id is a random uuid; blank it before comparing the rest.
    a_norm = {**a_body, "ping_id": "<id>"}
    f_norm = {**f.get_json(), "ping_id": "<id>"}
    assert a_norm == f_norm
    assert a_body["loop_alive"] is False and a_body["passing"] is False
    # The synthetic ping is GC'd — it must not linger in the transcript.
    store = core_store.get_store(_uid)
    with store.chat_lock:
        assert not any(m.get("source") == "verify_ping" for m in store.chat_messages)


# --------------------------------------------------------------------------- #
# WAKE: native /message write wakes a native parked /poll (same worker)
# --------------------------------------------------------------------------- #

def test_native_message_write_wakes_parked_poll(user):
    uid, api_key = user

    async def go():
        async with _client() as c:
            poll = asyncio.create_task(
                c.get("/v1/chat/poll", params={"timeout": 30}, headers=_hk(api_key))
            )
            assert await _wait_until_parked(1), "poll did not park an asyncio waiter"
            assert registry.active_count() == 1  # a future, not a thread

            # Native ASGI /message write — its store.notify_chat_waiters() (fired
            # from the run_db worker thread) must wake the parked poll.
            resp = await c.post(
                "/v1/chat/message", json={"envelope": _env(uid, "m_wake")}, headers=_hk(api_key)
            )
            assert resp.status_code == 200

            polled = await asyncio.wait_for(poll, timeout=2.0)
            assert polled.status_code == 200
            body = polled.json()
            assert body["timed_out"] is False
            assert "m_wake" in [m["id"] for m in body["messages"]]
        assert registry.active_count() == 0

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# /response reply-claim: an answered user message is not re-delivered (CAS)
# --------------------------------------------------------------------------- #

def test_response_reply_marks_message_no_double_deliver(user, monkeypatch):
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    uid, api_key = user

    async def go():
        async with _client() as c:
            # user message u1
            s, _ = await _post(c, "/v1/chat/message", api_key, {"envelope": _env(uid, "u1")})
            assert s == 200
            # consumer A claims u1
            r1 = await c.get(
                "/v1/chat/poll",
                params={"since": 0, "timeout": 0, "consumer_id": "cA"},
                headers=_hk(api_key),
            )
            assert "u1" in [m["id"] for m in r1.json()["messages"]]
            # agent replies to u1 (marks it reply_status=replied)
            rr = await c.post(
                "/v1/chat/response",
                json={"envelope": _env(uid, "a1"), "source": "chat", "reply_to_message_id": "u1"},
                headers=_hk(api_key),
            )
            assert rr.status_code == 200
            # a SECOND reply to the already-replied u1 is dropped with 409 — the
            # reply-exclusivity guard prevents a duplicate reply + double model-key
            # burn (e.g. a stale consumer finishing after the lease failed over).
            rr2 = await c.post(
                "/v1/chat/response",
                json={"envelope": _env(uid, "a2"), "source": "chat", "reply_to_message_id": "u1"},
                headers=_hk(api_key),
            )
            assert rr2.status_code == 409
            # a fresh consumer's claiming poll must NOT re-deliver the replied u1
            r2 = await c.get(
                "/v1/chat/poll",
                params={"since": 0, "timeout": 0, "consumer_id": "cB"},
                headers=_hk(api_key),
            )
            assert "u1" not in [m["id"] for m in r2.json()["messages"]]

    asyncio.run(go())


async def _post(c, path, api_key, body):
    resp = await c.post(path, json=body, headers=_hk(api_key))
    return resp.status_code, resp.json()
