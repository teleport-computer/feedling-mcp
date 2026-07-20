"""Live Activity / Dynamic Island push paths."""

import time
import uuid

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


def build_content_state(payload: dict, *, default_visual_state: str = "reply",
                        ai_start: str | None = None) -> dict:
    """Pure Live Activity content-state builder — no UserStore dependency.

    ``ai_start`` is the fallback when the payload carries no aiStart of its own;
    the store-backed wrapper below passes the identity card's value, the notify
    relay passes whatever the self-hosted caller supplied (the official side has
    no identity for relay users).
    """
    title = (payload.get("title") or "").strip()
    body = (payload.get("body") or payload.get("message") or payload.get("desc") or "").strip()
    subtitle = (payload.get("subtitle") or "").strip() or None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
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
        "aiStart": payload.get("aiStart") or payload.get("ai_start") or ai_start,
        "title": title,
        "subtitle": subtitle,
        "body": body,
        "personaId": payload.get("personaId", "default"),
        "templateId": payload.get("templateId", "default"),
        "data": data,
        "updatedAt": time.time(),
    }


def build_live_activity_aps(content_state: dict, *, event: str, alert: dict | None = None,
                            attributes_type: str | None = None,
                            attributes: dict | None = None,
                            dismissal_date: int | None = None,
                            stale_date: int | None = None) -> dict:
    """Pure APNs ``aps`` envelope for a Live Activity push.

    Shared by the store-backed ``push_live_*`` paths AND the notify relay so the
    wire shape — the exact keys iOS's ActivityKit Codable reads — can't drift
    between the two call sites (they previously reimplemented this each). Only
    fallback strings (e.g. the alert title) legitimately differ per caller and
    stay in the caller's ``alert``/``attributes`` args."""
    aps: dict = {"timestamp": int(time.time()), "event": event, "content-state": content_state}
    if alert is not None:
        aps["alert"] = alert
    if attributes_type is not None:
        aps["attributes-type"] = attributes_type
        aps["attributes"] = attributes if isinstance(attributes, dict) else {}
    if dismissal_date is not None:
        aps["dismissal-date"] = dismissal_date
    if stale_date is not None:
        aps["stale-date"] = stale_date
    return {"aps": aps}


def _live_activity_content_state(store: UserStore, payload: dict, *, default_visual_state: str = "reply") -> dict:
    identity_context = _live_activity_identity_context(store)
    return build_content_state(
        payload,
        default_visual_state=default_visual_state,
        ai_start=identity_context.get("aiStart"),
    )


def _live_activity_body(payload: dict) -> str:
    return (payload.get("body") or payload.get("message") or payload.get("desc") or "").strip()


def _live_activity_top_app(payload: dict) -> str:
    return str(payload.get("topApp") or payload.get("top_app") or "")


def push_live_activity_dict(store: UserStore, payload: dict) -> dict:
    activity_id = payload.get("activity_id")
    entry = push_tokens._select_token(store, push_tokens._is_live_activity_token, activity_id=activity_id, active_only=True)
    if not entry and activity_id:
        entry = push_tokens._select_token(store, push_tokens._is_live_activity_token, active_only=True)
    if not entry:
        print(f"[live-activity:{store.user_id}] no active token registered — logged: {payload}")
        return {
            "status": "logged",
            "activity_id": activity_id or f"la_{uuid.uuid4().hex[:8]}",
            "needs_refresh": True,
            "reason": "no_active_live_activity_token",
            "mode": "update",
        }

    body = _live_activity_body(payload)
    top_app = _live_activity_top_app(payload)
    alert_title = str(payload.get("alert_title") or payload.get("title") or "").strip()
    alert_body = str(payload.get("alert_body") or body or "").strip()

    suppress, reason = store.should_suppress_live_activity(message=body, top_app=top_app)
    if suppress:
        print(f"[live-activity:{store.user_id}] suppressed: {reason} body={body[:60]}")
        return {
            "status": "suppressed",
            "reason": reason,
            "activity_id": entry.get("activity_id"),
            "mode": "update",
        }

    # Non-empty alert text is what makes a remote Live Activity update
    # user-visible instead of only refreshing the lock-screen/Island content
    # state silently.
    apns_payload = build_live_activity_aps(
        _live_activity_content_state(store, payload, default_visual_state="reply"),
        event=payload.get("event", "update"),
        alert={"title": alert_title or "IO", "body": alert_body[:240]},
    )
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
    return response


def push_live_activity_end_inner(store: UserStore, payload: dict | None = None) -> dict:
    payload = payload or {}
    body = _live_activity_body(payload)
    top_app = _live_activity_top_app(payload)
    activity_id = payload.get("activity_id")
    apns_payload = build_live_activity_aps(
        _live_activity_content_state(store, payload, default_visual_state="default"),
        event="end",
        dismissal_date=int(time.time()),
    )
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


def push_live_start_dict(
    store: UserStore,
    payload: dict,
    *,
    end_existing: bool = False,
    allow_existing_restart: bool = False,
) -> dict:
    existing_live_entry = push_tokens._select_token(store, push_tokens._is_live_activity_token, active_only=True)
    update_result = None
    if existing_live_entry and not (allow_existing_restart or payload.get("force_start")):
        update_result = push_live_activity_dict(store, payload)
        update_result["mode"] = "update_existing"
        update_result["start_reason"] = "active_live_activity_token_present"
        if not (update_result.get("needs_refresh") or update_result.get("error_code") == 410):
            return update_result

    entry = push_tokens._select_token(store, push_tokens._is_push_to_start_token, active_only=True)
    if not entry:
        if update_result:
            update_result["start_status"] = "logged"
            update_result["start_reason"] = update_result.get("reason") or "no_active_push_to_start_token"
            return update_result
        print(f"[live-start:{store.user_id}] no push_to_start token — logged: {payload}")
        return {"status": "logged", "reason": "no_active_push_to_start_token", "mode": "start"}

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
    if end_existing and existing_live_entry and not update_result:
        end_result = push_live_activity_end_inner(store, payload)

    apns_payload = build_live_activity_aps(
        _live_activity_content_state(store, payload, default_visual_state="reply"),
        event="start",
        alert={"title": title or "OpenClaw", "body": body_text or "Live Activity started"},
        attributes_type=attributes_type,
        attributes=attributes,
    )

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
    if update_result:
        response["mode"] = "start_after_update_refresh"
        response["update_status"] = update_result.get("status", "unknown")
        if update_result.get("reason"):
            response["update_reason"] = update_result.get("reason")
        if update_result.get("error_code") is not None:
            response["update_error_code"] = update_result.get("error_code")
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
    return response


def push_live_activity_hybrid_dict(store: UserStore, payload: dict) -> dict:
    """Framework-neutral hybrid start/update decision — returns a plain dict.

    Shared by the Flask ``_inner`` wrapper and the AI-push delivery path
    (``service._deliver_ai_message_push_if_background``), which runs off the
    event loop in an ASGI worker thread with no Flask app context. Building the
    body via the neutral ``*_dict`` helpers (not ``jsonify``) keeps it usable in
    both worlds."""
    should_start, start_reason = store.should_start_live_activity()
    if should_start and push_tokens._select_token(store, push_tokens._is_push_to_start_token, active_only=True):
        start_body = push_live_start_dict(store, payload, end_existing=True, allow_existing_restart=True)
        if start_body.get("status") == "delivered":
            start_body["mode"] = "start"
            start_body["start_reason"] = start_reason
            return start_body

        # If push-to-start is unavailable or rejected, fall back to the cheaper
        # update path so an already-visible activity can still refresh.
        update_body = push_live_activity_dict(store, payload)
        update_body["mode"] = "start_fallback_update"
        update_body["start_status"] = start_body.get("status", "unknown")
        update_body["start_reason"] = start_body.get("reason") or start_reason
        return update_body

    update_body = push_live_activity_dict(store, payload)
    update_body["mode"] = "update"
    update_body["start_reason"] = start_reason
    return update_body
