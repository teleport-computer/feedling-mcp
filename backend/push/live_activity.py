"""Live Activity / Dynamic Island push paths."""

import time
import uuid

from flask import jsonify

from core.store import UserStore
from push import apns
from push import tokens as push_tokens


# Injected by the assembly layer — identity sits above push in the
# dependency stack, so the identity-card loader is wired in at startup.
def load_identity(store) -> dict:
    return {}


def _live_activity_identity_context(store: UserStore) -> dict:
    identity = load_identity(store) or {}
    return {
        "aiStart": str(identity.get("relationship_started_at") or "").strip() or None,
    }


def _live_activity_content_state(store: UserStore, payload: dict, *, default_visual_state: str = "reply") -> dict:
    title = (payload.get("title") or "").strip()
    body = (payload.get("body") or payload.get("message") or payload.get("desc") or "").strip()
    subtitle = (payload.get("subtitle") or "").strip() or None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    identity_context = _live_activity_identity_context(store)
    visual_state = str(
        payload.get("visualState")
        or payload.get("visual_state")
        or ("reply" if body else default_visual_state)
    ).strip() or default_visual_state
    if visual_state not in {"default", "sharing", "reply"}:
        visual_state = default_visual_state
    name = (payload.get("name") or title or "IO")
    name = str(name).strip() or "IO"

    # Include both the post-animation schema (visualState/name/desc/aiStart)
    # and the earlier schema (title/subtitle/body/data/updatedAt). Swift
    # Codable ignores unknown keys, so this keeps production TestFlight builds
    # and the new animated widget build compatible during rollout.
    return {
        "visualState": visual_state,
        "name": name,
        "desc": body,
        "aiStart": payload.get("aiStart") or payload.get("ai_start") or identity_context.get("aiStart"),
        "title": title,
        "subtitle": subtitle,
        "body": body,
        "personaId": payload.get("personaId", "default"),
        "templateId": payload.get("templateId", "default"),
        "data": data,
        "updatedAt": time.time(),
    }


def _live_activity_body(payload: dict) -> str:
    return (payload.get("body") or payload.get("message") or payload.get("desc") or "").strip()


def _live_activity_top_app(payload: dict) -> str:
    return str(payload.get("topApp") or payload.get("top_app") or "")


def push_live_activity_inner(store: UserStore, payload: dict):
    activity_id = payload.get("activity_id")
    entry = push_tokens._select_token(store, push_tokens._is_live_activity_token, activity_id=activity_id, active_only=True)
    if not entry and activity_id:
        entry = push_tokens._select_token(store, push_tokens._is_live_activity_token, active_only=True)
    if not entry:
        print(f"[live-activity:{store.user_id}] no active token registered — logged: {payload}")
        return jsonify({
            "status": "logged",
            "activity_id": activity_id or f"la_{uuid.uuid4().hex[:8]}",
            "needs_refresh": True,
            "reason": "no_active_live_activity_token",
            "mode": "update",
        })

    body = _live_activity_body(payload)
    top_app = _live_activity_top_app(payload)
    alert_title = str(payload.get("alert_title") or payload.get("title") or "").strip()
    alert_body = str(payload.get("alert_body") or body or "").strip()

    suppress, reason = store.should_suppress_live_activity(message=body, top_app=top_app)
    if suppress:
        print(f"[live-activity:{store.user_id}] suppressed: {reason} body={body[:60]}")
        return jsonify({
            "status": "suppressed",
            "reason": reason,
            "activity_id": entry.get("activity_id"),
            "mode": "update",
        })

    apns_payload = {
        "aps": {
            "timestamp": int(time.time()),
            "event": payload.get("event", "update"),
            "content-state": _live_activity_content_state(store, payload, default_visual_state="reply"),
            # Non-empty alert text is what makes a remote Live Activity update
            # user-visible instead of only refreshing the lock-screen/Island
            # content state silently.
            "alert": {"title": alert_title or "IO", "body": alert_body[:240]},
        }
    }
    topic = f"{apns.BUNDLE_ID}.push-type.liveactivity"
    result = apns._send_apns_to_active_tokens(
        store,
        push_tokens._is_live_activity_token,
        apns_payload,
        push_type="liveactivity",
        topic=topic,
        activity_id=activity_id,
    )

    delivered = result.get("status") == "delivered"
    if delivered:
        store.record_successful_push()
        store.record_live_activity_sent(message=body, top_app=top_app)

    print(f"[live-activity:{store.user_id}] {result}")
    response = {
        "status": result.get("status", "error"),
        "activity_id": entry.get("activity_id") or activity_id,
        "mode": "update",
    }
    if result.get("code") is not None:
        response["error_code"] = result.get("code")
    if result.get("reason"):
        response["reason"] = result.get("reason")
    if result.get("errors"):
        response["errors"] = result.get("errors")
    if result.get("code") == 410 or apns._apns_token_should_expire(result):
        response["needs_refresh"] = True
    return jsonify(response)


def push_live_activity_end_inner(store: UserStore, payload: dict | None = None) -> dict:
    payload = payload or {}
    body = _live_activity_body(payload)
    top_app = _live_activity_top_app(payload)
    activity_id = payload.get("activity_id")
    apns_payload = {
        "aps": {
            "timestamp": int(time.time()),
            "event": "end",
            "content-state": _live_activity_content_state(store, payload, default_visual_state="default"),
            "dismissal-date": int(time.time()),
        }
    }
    topic = f"{apns.BUNDLE_ID}.push-type.liveactivity"
    result = apns._send_apns_to_active_tokens(
        store,
        push_tokens._is_live_activity_token,
        apns_payload,
        push_type="liveactivity",
        topic=topic,
        activity_id=activity_id,
    )
    if result.get("status") == "delivered":
        store.record_live_activity_sent(message=body, top_app=top_app)
    print(f"[live-end:{store.user_id}] {result}")
    return result


def push_live_start_inner(store: UserStore, payload: dict, *, end_existing: bool = False):
    entry = push_tokens._select_token(store, push_tokens._is_push_to_start_token, active_only=True)
    if not entry:
        print(f"[live-start:{store.user_id}] no push_to_start token — logged: {payload}")
        return jsonify({"status": "logged", "reason": "no_active_push_to_start_token", "mode": "start"})

    title = (payload.get("title") or "").strip()
    body_text = _live_activity_body(payload)
    top_app = _live_activity_top_app(payload)
    activity_id = str(payload.get("activity_id") or f"la_{uuid.uuid4().hex[:8]}")
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {"activityId": activity_id}
    attributes_type = str(
        payload.get("attributes-type")
        or payload.get("attributes_type")
        or "ScreenActivityAttributes"
    )
    end_result = None
    if end_existing and push_tokens._select_token(store, push_tokens._is_live_activity_token, active_only=True):
        end_result = push_live_activity_end_inner(store, payload)

    apns_payload = {
        "aps": {
            "timestamp": int(time.time()),
            "event": "start",
            "content-state": _live_activity_content_state(store, payload, default_visual_state="reply"),
            "attributes-type": attributes_type,
            "attributes": attributes,
            "alert": {
                "title": title or "OpenClaw",
                "body": body_text or "Live Activity started",
            },
        }
    }

    topic = f"{apns.BUNDLE_ID}.push-type.liveactivity"
    result = apns._send_apns(
        entry["token"],
        apns_payload,
        push_type="liveactivity",
        topic=topic,
        preferred_env=entry.get("apns_env"),
    )
    if result.get("status") == "delivered":
        push_tokens._mark_active_token_success(store, entry, apns_env=result.get("apns_env"))
        store.record_live_activity_started(message=body_text, top_app=top_app)
    else:
        reason_text = str(result.get("reason", ""))
        if apns._apns_token_should_expire(result):
            push_tokens._mark_expired_token(store, entry, reason_text)

    print(f"[live-start:{store.user_id}] {result}")
    response = {"status": result.get("status", "error"), "mode": "start"}
    if result.get("code") is not None:
        response["error_code"] = result.get("code")
    if result.get("reason"):
        response["reason"] = result.get("reason")
    if result.get("code") == 410 or apns._apns_token_should_expire(result):
        response["needs_refresh"] = True
    if end_result:
        response["end_status"] = end_result.get("status", "unknown")
        if end_result.get("reason"):
            response["end_reason"] = end_result.get("reason")
    return jsonify(response)


def push_live_activity_hybrid_inner(store: UserStore, payload: dict):
    should_start, start_reason = store.should_start_live_activity()
    if should_start and push_tokens._select_token(store, push_tokens._is_push_to_start_token, active_only=True):
        start_resp = push_live_start_inner(store, payload, end_existing=True)
        try:
            start_body = start_resp.get_json(silent=True) or {}
        except Exception:
            start_body = {}
        if start_body.get("status") == "delivered":
            start_body["mode"] = "start"
            start_body["start_reason"] = start_reason
            return jsonify(start_body)

        # If push-to-start is unavailable or rejected, fall back to the cheaper
        # update path so an already-visible activity can still refresh.
        update_resp = push_live_activity_inner(store, payload)
        try:
            update_body = update_resp.get_json(silent=True) or {}
        except Exception:
            update_body = {}
        update_body["mode"] = "start_fallback_update"
        update_body["start_status"] = start_body.get("status", "unknown")
        update_body["start_reason"] = start_body.get("reason") or start_reason
        return jsonify(update_body)

    update_resp = push_live_activity_inner(store, payload)
    try:
        update_body = update_resp.get_json(silent=True) or {}
    except Exception:
        update_body = {}
    update_body["mode"] = "update"
    update_body["start_reason"] = start_reason
    return jsonify(update_body)
