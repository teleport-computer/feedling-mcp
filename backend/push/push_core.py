"""Framework-neutral /v1/push/* payloads (ASGI-migration plan §7 / §9).

The APNs / Live-Activity push route bodies, lifted out of the Flask routes so the
native ASGI routes (``push.routes_asgi``) reuse the exact same logic and return a
byte-for-byte identical body. No Flask/FastAPI request object here — the caller
resolves the store, parses the query/JSON body, and passes the decoded values in.

These are NOT E2E-encrypted user content: they register APNs device / Live
Activity tokens and fire lock-screen pushes. Several functions do blocking DB
writes (``store._save_tokens``) and outbound APNs HTTP (``apns._send_apns*``), so
ASGI callers must run them on the threadpool, not the event loop (plan §5.2).

The Live Activity update / start payloads live in ``push.live_activity`` (shared
with ``push_live_activity_hybrid_inner`` / ``push.service``); this module reuses
the dict producers there rather than duplicating that logic.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from core import store as core_store
from core.store import UserStore
from push import apns
from push import live_activity
from push import tokens as push_tokens


def list_tokens(store: UserStore, *, active_only: bool) -> dict:
    tokens = [core_store._normalize_token_entry(t) for t in store.tokens]
    if active_only:
        tokens = [t for t in tokens if push_tokens._entry_is_active(t)]
    return {"tokens": tokens}


def register_token(store: UserStore, *, payload: dict) -> dict:
    token_type = payload.get("type", "unknown")
    token = payload.get("token", "")
    activity_id = payload.get("activity_id")

    now_iso = datetime.now().isoformat()
    entry = {
        "type": token_type,
        "token": token,
        "registered_at": now_iso,
        "status": "active",
        "last_error": "",
        "last_success_at": "",
        "expired_at": "",
        "updated_at": now_iso,
    }
    if activity_id:
        entry["activity_id"] = activity_id
    apns_env = str(payload.get("apns_env") or payload.get("environment") or "").strip().lower()
    if apns_env in {"sandbox", "production"}:
        entry["apns_env"] = apns_env
    for meta_key in (
        "bundle_id",
        "app_version",
        "app_build",
        "build_configuration",
        "device_model",
        "system_version",
    ):
        meta_value = payload.get(meta_key)
        if meta_value is not None:
            entry[meta_key] = str(meta_value)[:160]

    store.tokens[:] = [
        core_store._normalize_token_entry(t)
        for t in store.tokens
        if not (
            t.get("token") == token
            or (
                t.get("type") == token_type
                and (not activity_id or t.get("activity_id") == activity_id)
            )
        )
    ]
    store.tokens.append(entry)
    store._save_tokens()

    print(f"[register-token:{store.user_id}] {token_type}: {token[:16]}…")
    return {"status": "registered", "type": token_type}


def notification(store: UserStore, *, payload: dict) -> dict:
    if not push_tokens._select_token(store, push_tokens._is_device_token, active_only=True):
        print(f"[notification:{store.user_id}] no device token — logged: {payload}")
        return {"status": "logged", "message_id": f"msg_{uuid.uuid4().hex[:8]}"}

    apns_payload = {
        "aps": {
            "alert": {"title": payload.get("title", ""), "body": payload.get("body", "")},
            "sound": "default",
        }
    }
    result = apns._send_apns_to_active_tokens(
        store,
        push_tokens._is_device_token,
        apns_payload,
        push_type="alert",
        topic=apns.BUNDLE_ID,
    )
    print(f"[notification:{store.user_id}] {result}")
    return {"status": result["status"], "message_id": f"msg_{uuid.uuid4().hex[:8]}"}


def dynamic_island(store: UserStore, *, payload: dict) -> dict:
    return live_activity.push_live_activity_dict(store, payload)


def live_activity_update(store: UserStore, *, payload: dict) -> dict:
    return live_activity.push_live_activity_dict(store, payload)


def live_start(store: UserStore, *, payload: dict) -> dict:
    return live_activity.push_live_start_dict(store, payload)


def ai_reply_push(store: UserStore, *, payload: dict) -> dict:
    """V2 回复的推送入口 —— 与 V1 ``chat_core.response`` 走同一条投递链。

    V2 的 serve-worker 没有 APNs 私钥（只注入 backend），所以它把已落库回复的
    明文正文交到这里。正文只经过内存：不写库、不进日志正文。

    ``is_wake`` 为真表示这是 agent 主动发起的消息，额外受用户的
    ``reminders_delivery`` 开关管辖；用户发消息后的应答不受该开关影响。
    """
    from proactive.controls_v2 import evaluate_delivery_v2, load_settings_v2_for_store
    from push import service as push_service

    msg_id = str(payload.get("msg_id") or "").strip()
    body = str(payload.get("body") or "").strip()
    is_wake = bool(payload.get("is_wake"))
    if not msg_id:
        return {"status": "skipped", "reason": "missing_msg_id", "apns_alert_sent": False}
    if not body:
        return {"status": "skipped", "reason": "empty_body", "apns_alert_sent": False}

    if is_wake:
        # ``lane`` is the V2 lane name (heartbeat/scheduled/manual_wake/
        # screen_watch) the serve-worker sent this wake reply's push under.
        # Mirrors V1's ``_proactive_delivery_decision_v2`` (chat/chat_core.py),
        # which derives ``source``/``manual`` from the wake job rather than
        # hardcoding them -- manual==True routes through
        # ``evaluate_delivery_v2``'s ``manual_bypass`` and is delivered even
        # when the user has turned ``reminders_delivery`` off. Back-compat: a
        # caller that predates this field (payload has no "lane" key at all)
        # falls back to the pre-fix constants instead of erroring.
        if "lane" in payload:
            source = str(payload.get("lane") or "").strip() or "heartbeat"
            manual = source == "manual_wake"
        else:
            source = "heartbeat"
            manual = False
        decision = evaluate_delivery_v2(
            load_settings_v2_for_store(store), source=source, manual=manual)
        if not decision.allow_visible_delivery:
            fields = {
                "push_decision": "suppressed",
                "push_reason": decision.reason,
                "alert_status": "suppressed",
                "alert_reason": decision.reason,
                "live_activity_status": "suppressed",
                "live_activity_reason": decision.reason,
            }
            store.update_chat_message_metadata(msg_id, fields)
            return {
                "status": "suppressed",
                "reason": decision.reason,
                "apns_alert_sent": False,
            }

    fields = push_service._deliver_ai_message_push_if_background(
        store, body=body[:240], title="IO", data={}, visual_state="reply")
    store.update_chat_message_metadata(msg_id, fields)
    alert_status = str(fields.get("alert_status") or "unknown")
    return {
        "status": alert_status,
        "reason": str(fields.get("push_reason") or ""),
        # This is an upper bound: APNs accepted an alert. Device Focus, mute,
        # notification settings, and summary delivery remain invisible here.
        "apns_alert_sent": alert_status == "delivered",
    }
