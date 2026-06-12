"""Push HTTP surface: /v1/push/*."""

import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from accounts import auth
from core import store as core_store
from push import apns, live_activity
from push import tokens as push_tokens

bp = Blueprint("push", __name__)

@bp.route("/v1/push/dynamic-island", methods=["POST"])
def push_dynamic_island():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    return live_activity.push_live_activity_inner(store, payload)


@bp.route("/v1/push/live-activity", methods=["POST"])
def push_live_activity():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    return live_activity.push_live_activity_inner(store, payload)


@bp.route("/v1/push/live-start", methods=["POST"])
def push_live_start():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    return live_activity.push_live_start_inner(store, payload)


@bp.route("/v1/push/notification", methods=["POST"])
def push_notification():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    if not push_tokens._select_token(store, push_tokens._is_device_token, active_only=True):
        print(f"[notification:{store.user_id}] no device token — logged: {payload}")
        return jsonify({"status": "logged", "message_id": f"msg_{uuid.uuid4().hex[:8]}"})

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
    return jsonify({"status": result["status"], "message_id": f"msg_{uuid.uuid4().hex[:8]}"})


@bp.route("/v1/push/register-token", methods=["POST"])
def register_token():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
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
    return jsonify({"status": "registered", "type": token_type})


@bp.route("/v1/push/tokens", methods=["GET"])
def list_tokens():
    store = auth.require_user()
    active_only = request.args.get("active_only", "false").lower() == "true"
    tokens = [core_store._normalize_token_entry(t) for t in store.tokens]
    if active_only:
        tokens = [t for t in tokens if push_tokens._entry_is_active(t)]
    return jsonify({"tokens": tokens})
