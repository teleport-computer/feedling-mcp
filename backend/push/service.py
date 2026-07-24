"""Push decision + chat-alert delivery (presence-aware)."""

import os
import time

import db
from accounts import registry
from core.store import UserStore
from push import apns, live_activity
from push import tokens as push_tokens

APP_FOREGROUND_FRESH_SEC = int(os.environ.get("FEEDLING_APP_FOREGROUND_FRESH_SEC", 90))


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Received AI messages (chat replies + proactive) no longer expand the Dynamic
# Island — they deliver only the system alert, which stacks in Notification
# Center instead of flashing away. The Live Activity module and its explicit
# /v1/push/{dynamic-island,live-activity,live-start} routes are untouched and stay
# available for other/future uses; only this AI-message trigger is gated. Set
# FEEDLING_AI_MSG_LIVE_ACTIVITY=1 to restore the old dual (Live Activity + alert)
# behavior.
AI_MSG_LIVE_ACTIVITY = _env_flag("FEEDLING_AI_MSG_LIVE_ACTIVITY", False)


def _latest_app_presence(store: UserStore, now: float | None = None) -> dict | None:
    now = now or time.time()
    for event in reversed(store.list_device_events(since_epoch=max(0.0, now - 86400), limit=300)):
        event_type = str(event.get("type") or "").strip().lower()
        if event_type not in {"app_presence", "app_state", "app_lifecycle"}:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        phase = str(
            payload.get("scene_phase")
            or payload.get("phase")
            or payload.get("app_state")
            or ""
        ).strip().lower()
        try:
            ts = float(event.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        age = max(0.0, now - ts) if ts > 0 else float("inf")
        is_foreground = payload.get("is_foreground")
        if isinstance(is_foreground, str):
            is_foreground = is_foreground.lower() in {"1", "true", "yes", "active", "foreground"}
        elif not isinstance(is_foreground, bool):
            is_foreground = phase in {"active", "foreground"}
        is_chat_visible = payload.get("is_chat_visible")
        if isinstance(is_chat_visible, str):
            is_chat_visible = is_chat_visible.lower() in {"1", "true", "yes"}
        elif not isinstance(is_chat_visible, bool):
            is_chat_visible = False
        return {
            "event_id": event.get("event_id", ""),
            "phase": phase or "unknown",
            "age_sec": age,
            "is_foreground": bool(is_foreground),
            "is_chat_visible": bool(is_chat_visible),
            "selected_tab": str(payload.get("selected_tab") or payload.get("tab") or "")[:80],
        }
    return None


def _ai_push_decision(store: UserStore) -> dict:
    now = time.time()
    presence = _latest_app_presence(store, now)
    if presence is None:
        return {
            "should_push": True,
            "reason": "no_app_presence",
            "phase": "unknown",
            "age_sec": "",
        }

    age = float(presence.get("age_sec") or 0.0)
    phase = str(presence.get("phase") or "unknown")
    is_fresh = age <= APP_FOREGROUND_FRESH_SEC
    if presence.get("is_foreground") and is_fresh:
        reason = "app_foreground_chat_visible" if presence.get("is_chat_visible") else "app_foreground"
        return {
            "should_push": False,
            "reason": reason,
            "phase": phase,
            "age_sec": str(int(age)),
        }

    if presence.get("is_foreground") and not is_fresh:
        reason = "foreground_presence_stale"
    elif phase in {"background", "inactive"}:
        reason = f"app_{phase}"
    else:
        reason = "app_not_foreground"
    return {
        "should_push": True,
        "reason": reason,
        "phase": phase,
        "age_sec": str(int(age)) if age != float("inf") else "",
    }


def _send_chat_alert(store: UserStore, alert_body: str, alert_title: str = ""):
    """Fire an APNs alert push for an agent chat message. Best-effort:
    failure here never blocks the chat write. The MCP layer (which has
    the plaintext at envelope-build time) passes alert_body in here so
    Flask doesn't have to decrypt anything. Apple's APNs gateway sees
    this string — same posture as Live Activity already has.

    Body is truncated to ~80 chars so long replies render as "...".
    Tap on the notification opens the app (iOS handles routing).
    """
    # Two-tier: free in-memory negative first (covers already-propagated deletes,
    # no DB call), then an authoritative DB check (closes the sub-second window
    # where THIS worker's registry is stale after another worker committed the
    # delete). `or` short-circuits so a None snapshot skips without touching the DB.
    if registry._user_entry_snapshot(store.user_id) is None or not db.user_exists(store.user_id):
        print(f"[chat-alert:{store.user_id}] account gone — skip push")
        return {"status": "skipped", "reason": "account_gone"}
    if not alert_body:
        return {"status": "skipped", "reason": "empty_body"}
    # Match iOS-registered token type: LiveActivityManager registers
    # the standard APNs push token as type="device". Older dev builds used
    # type="apns", so accept both but choose the newest active token.
    if not push_tokens._select_token(store, push_tokens._is_device_token, active_only=True):
        print(f"[chat-alert:{store.user_id}] no device token — skip push")
        return {"status": "skipped", "reason": "no_device_token"}

    # Truncate at 80 chars; iOS shows the rest after tapping into chat.
    body = alert_body.strip()
    if len(body) > 80:
        body = body[:79] + "…"

    apns_payload = {
        "aps": {
            "alert": {"title": alert_title or "", "body": body},
            "sound": "default",
        },
        "feedling": {"type": "chat_reply"},
    }
    try:
        result = apns._send_apns_to_active_tokens(
            store,
            push_tokens._is_device_token,
            apns_payload,
            push_type="alert",
            topic=apns.BUNDLE_ID,
        )
        print(f"[chat-alert:{store.user_id}] {result.get('status')}")
        return result
    except Exception as e:
        print(f"[chat-alert:{store.user_id}] failed: {e}")
        return {"status": "error", "reason": str(e)}


def _deliver_ai_message_push_if_background(
    store: UserStore,
    *,
    body: str,
    title: str = "",
    data: dict | None = None,
    visual_state: str = "reply",
) -> dict:
    # Two-tier gate (see _send_chat_alert): free in-memory negative, then
    # authoritative DB check to close the stale-worker race after a cross-worker
    # delete. Short-circuits so a None snapshot skips without a DB call.
    if registry._user_entry_snapshot(store.user_id) is None or not db.user_exists(store.user_id):
        print(f"[ai-push:{store.user_id}] account gone — skip push")
        return {"push_decision": "skip", "push_reason": "account_gone",
                "live_activity_status": "skipped", "live_activity_reason": "account_gone",
                "alert_status": "skipped", "alert_reason": "account_gone"}
    visible_body = (body or "").strip()
    decision = _ai_push_decision(store)
    fields: dict = {
        "push_decision": "send" if decision.get("should_push") else "suppress",
        "push_reason": str(decision.get("reason") or "")[:120],
        "app_presence_phase": str(decision.get("phase") or "")[:40],
    }
    if decision.get("age_sec") not in ("", None):
        fields["app_presence_age_sec"] = str(decision.get("age_sec"))[:20]

    if not visible_body:
        fields.update({
            "push_decision": "skip",
            "push_reason": "empty_body",
            "live_activity_status": "skipped",
            "live_activity_reason": "empty_body",
            "alert_status": "skipped",
            "alert_reason": "empty_body",
        })
        return fields

    if not decision.get("should_push"):
        reason = str(decision.get("reason") or "app_foreground")[:120]
        fields.update({
            "live_activity_status": "suppressed",
            "live_activity_reason": reason,
            "alert_status": "suppressed",
            "alert_reason": reason,
        })
        print(f"[ai-push:{store.user_id}] suppressed reason={reason}")
        return fields

    push_payload = {
        "title": title or "IO",
        "body": visible_body[:240],
        "alert_body": visible_body[:240],
        "data": data or {},
        "visualState": visual_state or "reply",
    }
    # AI messages deliver via the system alert only; the Dynamic Island expansion
    # is gated off (FEEDLING_AI_MSG_LIVE_ACTIVITY). The Live Activity module + its
    # explicit push routes remain for other uses — this only skips the AI-message
    # trigger. Neutral dict path (no Flask jsonify): runs off the event loop in an
    # ASGI worker thread with no Flask app context.
    if AI_MSG_LIVE_ACTIVITY:
        live_body = live_activity.push_live_activity_hybrid_dict(store, push_payload)
        fields["live_activity_status"] = live_body.get("status", "unknown")
        fields["live_activity_reason"] = live_body.get("reason", "")
        fields["live_activity_activity_id"] = live_body.get("activity_id", "")
        fields["live_activity_mode"] = live_body.get("mode", "")
    else:
        fields["live_activity_status"] = "disabled"
        fields["live_activity_reason"] = "ai_msg_live_activity_off"
        fields["live_activity_activity_id"] = ""
        fields["live_activity_mode"] = "disabled"

    alert_result = _send_chat_alert(store, visible_body, alert_title=title or "")
    fields["alert_status"] = (alert_result or {}).get("status", "unknown")
    fields["alert_reason"] = (alert_result or {}).get("reason", "")
    print(
        f"[ai-push:{store.user_id}] live={fields['live_activity_status']} "
        f"alert={fields['alert_status']} reason={fields.get('push_reason', '')}"
    )
    return fields
