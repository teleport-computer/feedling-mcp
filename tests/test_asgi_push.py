"""Native /v1/push/* parity + auth (ASGI-migration plan §5.3 / §9).

Asserts the six FastAPI push routes return the same status + body as the Flask
oracle (both delegate to ``push.push_core`` / ``push.live_activity`` dict
producers), that every route requires auth (fixed-body 401 — the same gate as the
Flask ``auth.require_user()`` call; none of these routes enforce a scope), and
that the APNs error path (410 -> ``needs_refresh``) is reproduced identically.

APNs is made deterministic and offline by monkeypatching ``push.apns`` module
functions (``_send_apns_to_active_tokens`` / ``_send_apns``) — Flask and the ASGI
route share the same ``apns`` module object, so a single patch covers both and no
test ever opens a socket to Apple. Routes are exercised on their no-token /
patched-send branches so the two sequential backend calls never diverge on
live-activity dedupe state.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from core.store import UserStore  # noqa: E402
from push import apns  # noqa: E402
from push import routes_asgi as push_asgi  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def asgi_app_obj():
    """Minimal FastAPI app mirroring asgi_app.py's assembly (exception handlers +
    our router). Built here because the orchestrator wires push.routes_asgi into
    asgi_app.py separately (out of this task's scope)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    push_asgi.register_asgi(app)
    return app


@pytest.fixture()
def user(tmp_path, monkeypatch):
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
    body = res.get_json()
    return body["user_id"], body["api_key"]


# --------------------------------------------------------------------------- #
# transport helpers
# --------------------------------------------------------------------------- #

def _asgi_get(app, path: str, headers: dict | None = None):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get(path, headers=headers or {})
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, resp.text

    return asyncio.run(go())


def _asgi_post(app, path: str, json_body=None, headers: dict | None = None):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(path, json=json_body, headers=headers or {})
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, resp.text

    return asyncio.run(go())


def _flask_get(path: str, headers: dict | None = None):
    res = make_client().get(path, headers=headers or {})
    return res.status_code, res.get_json()


def _flask_post(path: str, json_body=None, headers: dict | None = None):
    res = make_client().post(path, json=json_body, headers=headers or {})
    return res.status_code, res.get_json()


def _register(api_key: str, payload: dict):
    """Seed a token through the real Flask register-token route (persists to the
    shared store both backends read)."""
    status, body = _flask_post("/v1/push/register-token", payload, {"X-API-Key": api_key})
    assert status == 200, body
    return body


def _norm_msg(body: dict) -> dict:
    """message_id is a fresh uuid per call; blank it before comparing."""
    out = dict(body)
    if "message_id" in out:
        out["message_id"] = "<msg>"
    return out


# --------------------------------------------------------------------------- #
# GET /v1/push/tokens
# --------------------------------------------------------------------------- #

def test_list_tokens_parity(asgi_app_obj, user):
    uid, api_key = user
    _register(api_key, {"type": "device", "token": "devtok123456789abc"})
    fs, fb = _flask_get("/v1/push/tokens", {"X-API-Key": api_key})
    as_, ab = _asgi_get(asgi_app_obj, "/v1/push/tokens", {"X-API-Key": api_key})
    assert fs == as_ == 200
    assert ab == fb
    assert len(fb["tokens"]) == 1 and fb["tokens"][0]["type"] == "device"


def test_list_tokens_active_only_parity(asgi_app_obj, user):
    uid, api_key = user
    _register(api_key, {"type": "device", "token": "devtok123456789abc"})
    fs, fb = _flask_get("/v1/push/tokens?active_only=true", {"X-API-Key": api_key})
    as_, ab = _asgi_get(asgi_app_obj, "/v1/push/tokens?active_only=true", {"X-API-Key": api_key})
    assert fs == as_ == 200
    assert ab == fb


def test_list_tokens_requires_auth(asgi_app_obj, user):
    fs, _fb = _flask_get("/v1/push/tokens")
    as_, ab = _asgi_get(asgi_app_obj, "/v1/push/tokens")
    assert fs == as_ == 401
    assert ab == {"error": "unauthorized"}


# --------------------------------------------------------------------------- #
# POST /v1/push/register-token
# --------------------------------------------------------------------------- #

def test_register_token_parity(asgi_app_obj, user):
    uid, api_key = user
    payload = {"type": "device", "token": "devtok123456789abc", "apns_env": "sandbox"}
    fs, fb = _flask_post("/v1/push/register-token", payload, {"X-API-Key": api_key})
    as_, ab = _asgi_post(asgi_app_obj, "/v1/push/register-token", payload, {"X-API-Key": api_key})
    assert fs == as_ == 200
    assert ab == fb == {"status": "registered", "type": "device"}


def test_register_token_requires_auth(asgi_app_obj, user):
    fs, _fb = _flask_post("/v1/push/register-token", {"type": "device", "token": "x"})
    as_, ab = _asgi_post(asgi_app_obj, "/v1/push/register-token", {"type": "device", "token": "x"})
    assert fs == as_ == 401
    assert ab == {"error": "unauthorized"}


# --------------------------------------------------------------------------- #
# POST /v1/push/notification
# --------------------------------------------------------------------------- #

def test_notification_no_token_logged_parity(asgi_app_obj, user):
    """No device token registered -> 'logged', no APNs call (offline branch)."""
    uid, api_key = user
    fs, fb = _flask_post("/v1/push/notification", {"title": "hi", "body": "yo"}, {"X-API-Key": api_key})
    as_, ab = _asgi_post(asgi_app_obj, "/v1/push/notification", {"title": "hi", "body": "yo"}, {"X-API-Key": api_key})
    assert fs == as_ == 200
    assert _norm_msg(ab) == _norm_msg(fb) == {"status": "logged", "message_id": "<msg>"}


def test_notification_delivered_parity(asgi_app_obj, user, monkeypatch):
    """With a device token + patched APNs send, both backends report the send
    status identically (and never touch the network)."""
    uid, api_key = user
    _register(api_key, {"type": "device", "token": "devtok123456789abc"})
    monkeypatch.setattr(apns, "_send_apns_to_active_tokens", lambda *a, **k: {"status": "delivered"})
    fs, fb = _flask_post("/v1/push/notification", {"title": "hi", "body": "yo"}, {"X-API-Key": api_key})
    as_, ab = _asgi_post(asgi_app_obj, "/v1/push/notification", {"title": "hi", "body": "yo"}, {"X-API-Key": api_key})
    assert fs == as_ == 200
    assert _norm_msg(ab) == _norm_msg(fb) == {"status": "delivered", "message_id": "<msg>"}


# --------------------------------------------------------------------------- #
# POST /v1/push/dynamic-island  &  /v1/push/live-activity
# --------------------------------------------------------------------------- #

def test_dynamic_island_no_token_parity(asgi_app_obj, user):
    uid, api_key = user
    payload = {"activity_id": "la_test", "body": "hello"}
    fs, fb = _flask_post("/v1/push/dynamic-island", payload, {"X-API-Key": api_key})
    as_, ab = _asgi_post(asgi_app_obj, "/v1/push/dynamic-island", payload, {"X-API-Key": api_key})
    assert fs == as_ == 200
    assert ab == fb == {
        "status": "logged",
        "activity_id": "la_test",
        "needs_refresh": True,
        "reason": "no_active_live_activity_token",
        "mode": "update",
    }


def test_live_activity_no_token_parity(asgi_app_obj, user):
    uid, api_key = user
    payload = {"activity_id": "la_test", "body": "hello"}
    fs, fb = _flask_post("/v1/push/live-activity", payload, {"X-API-Key": api_key})
    as_, ab = _asgi_post(asgi_app_obj, "/v1/push/live-activity", payload, {"X-API-Key": api_key})
    assert fs == as_ == 200
    assert ab == fb


def test_live_activity_error_path_parity(asgi_app_obj, user, monkeypatch):
    """APNs 410 (token gone) -> needs_refresh. Patched send + no-suppress so both
    sequential backend calls hit the same error branch deterministically."""
    uid, api_key = user
    _register(api_key, {"type": "live-activity", "token": "latok123456", "activity_id": "la_test"})
    monkeypatch.setattr(
        apns, "_send_apns_to_active_tokens",
        lambda *a, **k: {"status": "error", "code": 410, "reason": "Unregistered"},
    )
    monkeypatch.setattr(UserStore, "should_suppress_live_activity", lambda self, message, top_app: (False, "ok"))
    payload = {"activity_id": "la_test", "body": "hello"}
    fs, fb = _flask_post("/v1/push/live-activity", payload, {"X-API-Key": api_key})
    as_, ab = _asgi_post(asgi_app_obj, "/v1/push/live-activity", payload, {"X-API-Key": api_key})
    assert fs == as_ == 200
    assert ab == fb
    assert fb["status"] == "error" and fb["error_code"] == 410 and fb["needs_refresh"] is True


def test_live_activity_requires_auth(asgi_app_obj, user):
    fs, _fb = _flask_post("/v1/push/live-activity", {"body": "x"})
    as_, ab = _asgi_post(asgi_app_obj, "/v1/push/live-activity", {"body": "x"})
    assert fs == as_ == 401
    assert ab == {"error": "unauthorized"}


# --------------------------------------------------------------------------- #
# POST /v1/push/live-start
# --------------------------------------------------------------------------- #

def test_live_start_no_token_parity(asgi_app_obj, user):
    uid, api_key = user
    payload = {"title": "hi", "body": "hello"}
    fs, fb = _flask_post("/v1/push/live-start", payload, {"X-API-Key": api_key})
    as_, ab = _asgi_post(asgi_app_obj, "/v1/push/live-start", payload, {"X-API-Key": api_key})
    assert fs == as_ == 200
    assert ab == fb == {"status": "logged", "reason": "no_active_push_to_start_token", "mode": "start"}


def test_live_start_requires_auth(asgi_app_obj, user):
    fs, _fb = _flask_post("/v1/push/live-start", {"body": "x"})
    as_, ab = _asgi_post(asgi_app_obj, "/v1/push/live-start", {"body": "x"})
    assert fs == as_ == 401
    assert ab == {"error": "unauthorized"}
