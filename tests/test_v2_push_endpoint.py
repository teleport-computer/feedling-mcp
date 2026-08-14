"""POST /v1/internal/push/ai_reply —— V2 回复推送的 backend 入口。

V2 的 serve-worker 容器没有 APNs 私钥（只注入 backend），所以推送由它把明文正文
交给这个端点、再走 V1 那条完全相同的投递链（presence gate -> APNs alert ->
delivery metadata 回写）。这里断言的是契约与 gate，APNs 出网被 monkeypatch 掉。
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
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402
from push import routes_asgi as push_asgi  # noqa: E402
from push import service as push_service  # noqa: E402

_SECRET = b"test-runtime-token-secret"


@pytest.fixture()
def app_obj():
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
        json={
            "public_key": base64.b64encode(b"\x11" * 32).decode("ascii"),
            "archive_language": "en",
        },
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["user_id"]


def _post(app, path, json_body, headers):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(path, json=json_body, headers=headers)
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, resp.text

    return asyncio.run(go())


def _token(user_id, scope):
    return runtime_token.mint(
        _SECRET, user_id=user_id, runtime_instance_id="v2-worker",
        scope=scope, ttl=60.0,
    )


def test_wrong_scope_is_forbidden(app_obj, user):
    status, _ = _post(
        app_obj,
        "/v1/internal/push/ai_reply",
        {"msg_id": "m1", "body": "hi"},
        {"X-Feedling-Runtime-Token": _token(user, ["envelope_decrypt"])},
    )
    assert status == 403


def test_delivers_and_writes_back_metadata(app_obj, user, monkeypatch):
    seen = {}

    def _fake_deliver(store, *, body, title="", data=None, visual_state="reply"):
        seen.update(user_id=store.user_id, body=body, title=title)
        return {"push_decision": "send", "push_reason": "no_app_presence",
                "alert_status": "delivered", "alert_reason": ""}

    monkeypatch.setattr(
        push_service, "_deliver_ai_message_push_if_background", _fake_deliver)
    written = {}
    monkeypatch.setattr(
        core_store.UserStore, "update_chat_message_metadata",
        lambda self, msg_id, fields: written.update(msg_id=msg_id, fields=fields))

    status, body = _post(
        app_obj,
        "/v1/internal/push/ai_reply",
        {"msg_id": "msg-abc", "body": "回复正文", "is_wake": False},
        {"X-Feedling-Runtime-Token": _token(user, ["chat_push"])},
    )

    assert status == 200
    assert body["status"] == "delivered"
    assert body["apns_alert_sent"] is True
    assert seen["body"] == "回复正文"
    assert seen["title"] == "IO"
    assert written["msg_id"] == "msg-abc"
    assert written["fields"]["alert_status"] == "delivered"


def test_wake_respects_reminders_delivery_off(app_obj, user, monkeypatch):
    from proactive import controls_v2

    monkeypatch.setattr(
        controls_v2, "load_settings_v2_for_store",
        lambda store: controls_v2.resolve_settings_v2({"reminders_delivery": False}))
    called = {"n": 0}
    monkeypatch.setattr(
        push_service, "_deliver_ai_message_push_if_background",
        lambda *a, **k: called.update(n=called["n"] + 1) or {})
    written = {}
    monkeypatch.setattr(
        core_store.UserStore, "update_chat_message_metadata",
        lambda self, msg_id, fields: written.update(fields=fields))

    status, body = _post(
        app_obj,
        "/v1/internal/push/ai_reply",
        {"msg_id": "msg-wake", "body": "主动消息", "is_wake": True},
        {"X-Feedling-Runtime-Token": _token(user, ["chat_push"])},
    )

    assert status == 200
    assert body["status"] == "suppressed"
    assert body["apns_alert_sent"] is False
    assert called["n"] == 0
    assert written["fields"]["alert_status"] == "suppressed"


def test_manual_wake_bypasses_reminders_delivery_off(app_obj, user, monkeypatch):
    """V1 parity (review Minor #1): manual wakes always deliver, even with the
    proactive-reminders toggle off — see `evaluate_delivery_v2`'s
    `manual_bypass` branch and V1's `_proactive_delivery_decision_v2`, which
    derives `manual` from the wake job rather than hardcoding it False. The V2
    worker signals this over the wire with `lane="manual_wake"` (the only V2
    wake lane that is manual); anything else must NOT bypass the gate."""
    from proactive import controls_v2

    monkeypatch.setattr(
        controls_v2, "load_settings_v2_for_store",
        lambda store: controls_v2.resolve_settings_v2({"reminders_delivery": False}))
    seen = {}

    def _fake_deliver(store, *, body, title="", data=None, visual_state="reply"):
        seen.update(body=body, title=title)
        return {"push_decision": "send", "push_reason": "manual_bypass",
                "alert_status": "delivered", "alert_reason": ""}

    monkeypatch.setattr(
        push_service, "_deliver_ai_message_push_if_background", _fake_deliver)
    written = {}
    monkeypatch.setattr(
        core_store.UserStore, "update_chat_message_metadata",
        lambda self, msg_id, fields: written.update(fields=fields))

    status, body = _post(
        app_obj,
        "/v1/internal/push/ai_reply",
        {"msg_id": "msg-manual", "body": "手动唤醒消息", "is_wake": True,
         "lane": "manual_wake"},
        {"X-Feedling-Runtime-Token": _token(user, ["chat_push"])},
    )

    assert status == 200
    assert body["status"] == "delivered"
    assert body["apns_alert_sent"] is True
    assert seen["body"] == "手动唤醒消息"
    assert written["fields"]["alert_status"] == "delivered"


def test_non_manual_wake_lane_still_respects_reminders_delivery_off(
    app_obj, user, monkeypatch
):
    """Sibling to the manual-bypass test above: a non-manual wake lane
    (heartbeat) must still be suppressed when `reminders_delivery` is off —
    guards against a fix that accidentally makes every wake manual."""
    from proactive import controls_v2

    monkeypatch.setattr(
        controls_v2, "load_settings_v2_for_store",
        lambda store: controls_v2.resolve_settings_v2({"reminders_delivery": False}))
    called = {"n": 0}
    monkeypatch.setattr(
        push_service, "_deliver_ai_message_push_if_background",
        lambda *a, **k: called.update(n=called["n"] + 1) or {})
    written = {}
    monkeypatch.setattr(
        core_store.UserStore, "update_chat_message_metadata",
        lambda self, msg_id, fields: written.update(fields=fields))

    status, body = _post(
        app_obj,
        "/v1/internal/push/ai_reply",
        {"msg_id": "msg-heartbeat", "body": "主动消息", "is_wake": True,
         "lane": "heartbeat"},
        {"X-Feedling-Runtime-Token": _token(user, ["chat_push"])},
    )

    assert status == 200
    assert body["status"] == "suppressed"
    assert body["apns_alert_sent"] is False
    assert called["n"] == 0
    assert written["fields"]["alert_status"] == "suppressed"


def test_empty_body_is_skipped(app_obj, user, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        push_service, "_deliver_ai_message_push_if_background",
        lambda *a, **k: called.update(n=called["n"] + 1) or {})

    status, body = _post(
        app_obj,
        "/v1/internal/push/ai_reply",
        {"msg_id": "m1", "body": "   "},
        {"X-Feedling-Runtime-Token": _token(user, ["chat_push"])},
    )

    assert status == 200
    assert body["status"] == "skipped"
    assert body["apns_alert_sent"] is False
    assert called["n"] == 0
