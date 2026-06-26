"""Chat HTTP surface: /v1/chat/* (resident line)."""

import json
import os
import re
import time
import uuid
from datetime import date, datetime

from flask import jsonify, request

import db
from core.store import UserStore
from core import wake_bus
from flask import Blueprint, Response
import threading

from accounts import auth
from bootstrap import gates as boot_gates
from chat import consumer as chat_consumer
from chat import service as chat_service
from memory import service as memory_service
from proactive import service as proactive_service
from push import service as push_service

bp = Blueprint("chat", __name__)


def _settings_v2_for_store(store: UserStore):
    try:
        from proactive.store_v2 import DBProactiveSettingsStoreV2

        return DBProactiveSettingsStoreV2().load(store.user_id)
    except Exception:
        return store.load_proactive_settings()


def _proactive_job_for_response(store: UserStore, job_id: str) -> dict | None:
    if not job_id:
        return None
    try:
        for job in store.list_proactive_jobs(since_epoch=0, limit=0):
            if str(job.get("job_id") or "") == str(job_id):
                return job
    except Exception:
        return None
    return None


def _proactive_delivery_decision_v2(store: UserStore, payload: dict):
    from proactive.controls_v2 import evaluate_delivery_v2

    source = "heartbeat"
    manual = False
    job = _proactive_job_for_response(store, str(payload.get("proactive_job_id") or ""))
    if job:
        try:
            from proactive.adapters_v2 import wake_event_v2_from_legacy_job

            event = wake_event_v2_from_legacy_job(store.user_id, job)
            source = event.source
            manual = event.manual
        except Exception:
            manual = bool(job.get("manual"))
    manual = manual or bool(payload.get("manual") or payload.get("manual_wake") or payload.get("user_initiated"))
    return evaluate_delivery_v2(_settings_v2_for_store(store), source=source, manual=manual)


@bp.route("/v1/chat/history", methods=["GET"])
def chat_history():
    store = auth.require_user()
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 200))

    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400

    before_raw = request.args.get("before", "")
    before = 0.0
    if before_raw not in ("", None):
        try:
            before = float(before_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid before"}), 400

    include_image_body = str(
        request.args.get("include_image_body", request.args.get("include_image_bodies", "true"))
    ).lower() not in {"0", "false", "no", "off"}

    with store.chat_lock:
        all_msgs = list(store.chat_messages)
        total = len(store.chat_messages)

    if before > 0:
        filtered = [m for m in all_msgs if float(m.get("ts", 0)) < before]
        msgs = filtered[-limit:]
        has_more_older = len(filtered) > len(msgs)
        has_more_newer = False
        page_mode = "before"
    elif since > 0:
        filtered = [m for m in all_msgs if float(m.get("ts", 0)) > since]
        msgs = filtered[:limit]
        has_more_older = bool(all_msgs and msgs and float(all_msgs[0].get("ts", 0)) < float(msgs[0].get("ts", 0)))
        has_more_newer = len(filtered) > len(msgs)
        page_mode = "since"
    else:
        msgs = all_msgs[-limit:]
        has_more_older = len(all_msgs) > len(msgs)
        has_more_newer = False
        page_mode = "latest"

    out = [chat_service._chat_history_item(m, include_image_body=include_image_body) for m in msgs]
    omitted_bodies = sum(1 for m in out if m.get("body_omitted"))
    omitted_image_bodies = sum(
        1
        for m in out
        if m.get("body_omitted") and m.get("content_type", "text") == "image"
    )
    oldest_ts = float(out[0].get("ts", 0)) if out else 0
    latest_ts = float(out[-1].get("ts", 0)) if out else 0

    ua = request.headers.get("User-Agent", "")
    print(
        f"[chat/history:{store.user_id}] ip={request.remote_addr} mode={page_mode} "
        f"since={since} before={before} limit={limit} returned={len(out)} total={total} "
        f"include_image_body={include_image_body} omitted_bodies={omitted_bodies} "
        f"omitted_images={omitted_image_bodies} ua={ua[:80]}"
    )

    return jsonify({
        "messages": out,
        "total": total,
        "oldest_ts": oldest_ts,
        "latest_ts": latest_ts,
        "has_more_older": has_more_older,
        "has_more_newer": has_more_newer,
        "bodies_omitted": omitted_bodies,
        "image_bodies_omitted": omitted_image_bodies,
        "body_omit_inline_max": chat_service.CHAT_HISTORY_INLINE_BODY_CT_MAX,
    })


@bp.route("/v1/chat/history", methods=["DELETE"])
def chat_history_clear():
    """Clear only the caller's chat transcript.

    This intentionally does not touch memory, identity, frames, API keys, or
    onboarding route state. The destructive account reset endpoint remains the
    only path that wipes the whole user record.
    """
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    confirm = (payload.get("confirm") or "").strip()
    if confirm != "clear-chat-history":
        return jsonify({
            "error": "confirmation_required",
            "detail": "DELETE body must include {\"confirm\": \"clear-chat-history\"}."
        }), 400

    deleted = db.chat_clear(store.user_id)
    if deleted is None:
        return jsonify({"error": "chat_clear_failed"}), 500

    with store.chat_lock:
        store.chat_messages = []

    store.notify_chat_waiters()
    # Cross-worker: other workers still hold the now-cleared messages in cache —
    # refresh them (a delete isn't a new-message append, so it won't route
    # through append_chat's notify).
    wake_bus.notify("chat", store.user_id)
    print(f"[chat/clear:{store.user_id}] deleted={deleted}")
    return jsonify({"cleared": True, "deleted": deleted})


@bp.route("/v1/chat/messages/<message_id>/body", methods=["GET"])
def chat_message_body(message_id):
    store = auth.require_user()
    with store.chat_lock:
        msg = next((m for m in store.chat_messages if str(m.get("id") or "") == str(message_id)), None)
    if not msg:
        return jsonify({"error": "message_not_found"}), 404
    return jsonify({"message": chat_service._chat_history_item(msg, include_image_body=True)})


@bp.route("/v1/chat/message", methods=["POST"])
def chat_message():
    """User sends a chat message as a v1 ciphertext envelope.

    See docs/DESIGN_E2E.md §3.2 for envelope field definitions. The
    server never decrypts the envelope — it is stored verbatim and
    later surfaced by the enclave's /v1/* handlers.
    """
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    if envelope is None:
        return jsonify({"error": "envelope required"}), 400
    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    content_type = payload.get("content_type", "text")
    if content_type not in ("text", "image"):
        return jsonify({"error": "content_type must be 'text' or 'image'"}), 400
    msg = store.append_chat("user", "chat", envelope, content_type=content_type)
    store.notify_chat_waiters()
    print(f"[chat:{store.user_id}] user(v1, visibility={envelope['visibility']}, type={content_type}) id={msg['id']}")
    return jsonify({"id": msg["id"], "ts": msg["ts"], "v": msg["v"]})


@bp.route("/v1/chat/response", methods=["POST"])
def chat_response():
    """Agent posts a reply as a v1 ciphertext envelope. Shape matches
    /v1/chat/message. When the caller supplies plaintext `alert_body` or
    `push_body`, the server applies app-state push policy: background/unknown
    app state gets APNs alert + Live Activity hybrid delivery; active foreground
    app state records a suppression. `push_body` / `alert_body` are plaintext
    metadata (user-visible in APNs surfaces) and are never stored in chat.

    Bootstrap gate (A', 2026-06): this endpoint 409s only if identity is not
    yet written, or the live chat loop (resident consumer + verified loop) is
    not yet wired. Memory is NO LONGER a gate — 0 memory cards is a valid state.
    See boot_gates._gate_bootstrap_for_chat.
    """
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    allow_verify_reply = boot_gates._reply_is_for_pending_verify_ping(store)
    gated = boot_gates._gate_bootstrap_for_chat(store, allow_verify_reply=allow_verify_reply)
    if gated is not None:
        return gated
    chat_consumer._record_consumer_event(store, "response")
    envelope = payload.get("envelope")
    if envelope is None:
        return jsonify({"error": "envelope required"}), 400
    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    content_type = payload.get("content_type", "text")
    if content_type not in ("text", "image"):
        return jsonify({"error": "content_type must be 'text' or 'image'"}), 400
    thinking_envelope = payload.get("thinking_envelope")
    thinking_extra: dict = {}
    if thinking_envelope is not None:
        if not isinstance(thinking_envelope, dict):
            return jsonify({"error": "thinking_envelope must be an object"}), 400
        missing = [f for f in required if not thinking_envelope.get(f)]
        if missing:
            return jsonify({"error": f"thinking_envelope missing fields: {missing}"}), 400
        if thinking_envelope["visibility"] not in ("shared", "local_only"):
            return jsonify({"error": "thinking_envelope.visibility must be 'shared' or 'local_only'"}), 400
        if thinking_envelope["visibility"] == "shared" and not thinking_envelope.get("K_enclave"):
            return jsonify({"error": "thinking_envelope with visibility=shared requires K_enclave"}), 400
        thinking_extra = {
            "thinking_v": str(thinking_envelope.get("v", 1)),
            "thinking_id": str(thinking_envelope.get("id") or ""),
            "thinking_body_ct": str(thinking_envelope["body_ct"]),
            "thinking_nonce": str(thinking_envelope["nonce"]),
            "thinking_K_user": str(thinking_envelope["K_user"]),
            "thinking_visibility": str(thinking_envelope["visibility"]),
            "thinking_owner_user_id": str(thinking_envelope["owner_user_id"]),
            "thinking_enclave_pk_fpr": str(thinking_envelope.get("enclave_pk_fpr") or ""),
        }
        if thinking_envelope.get("K_enclave"):
            thinking_extra["thinking_K_enclave"] = str(thinking_envelope["K_enclave"])
        thinking_extra.update(chat_service._chat_thinking_metadata_from_payload(payload))
    else:
        thinking_extra.update(chat_service._chat_plaintext_thinking_extra_for_store(store, payload))
    source = str(payload.get("source") or "chat").strip() or "chat"
    if source not in {"chat", "live_activity", "heartbeat", proactive_service.PROACTIVE_JOB_SOURCE}:
        return jsonify({"error": "invalid source"}), 400
    alert_body = str(payload.get("alert_body") or "")
    push_body = str(payload.get("push_body") or "")
    extra = {
        "gate_decision_id": str(payload.get("gate_decision_id") or ""),
        "proactive_job_id": str(payload.get("proactive_job_id") or ""),
        **thinking_extra,
    }
    if source == proactive_service.PROACTIVE_JOB_SOURCE:
        preview = (alert_body or push_body).strip()
        if preview:
            extra["alert_preview"] = preview[:240]
        if push_body.strip():
            extra["push_body_preview"] = push_body.strip()[:240]
        extra["push_live_activity_requested"] = bool(payload.get("push_live_activity"))
    msg = store.append_chat(
        "openclaw",
        source,
        envelope,
        content_type=content_type,
        extra=extra,
    )
    reply_to_message_id = str(
        payload.get("reply_to_message_id")
        or payload.get("reply_to_id")
        or payload.get("in_reply_to")
        or ""
    ).strip()
    if reply_to_message_id:
        store.update_chat_message_metadata(reply_to_message_id, {
            "reply_status": "replied",
            "reply_message_id": str(msg.get("id") or ""),
            "replied_by": chat_service._request_chat_consumer_id(),
            "replied_at": f"{time.time():.3f}",
        })
    delivery_fields: dict = {}
    visible_push_body = (push_body or alert_body).strip()
    # Any plaintext AI reply supplied by the caller enters the same app-state
    # policy: background/unknown app state gets Live Activity + APNs alert;
    # foreground app state records a suppression instead of interrupting.
    if visible_push_body or payload.get("push_live_activity"):
        delivery = None
        if source == proactive_service.PROACTIVE_JOB_SOURCE:
            delivery = _proactive_delivery_decision_v2(store, payload)
        if delivery is not None and not delivery.allow_visible_delivery:
            delivery_fields.update({
                "push_decision": "suppressed",
                "push_reason": delivery.reason,
                "alert_status": "suppressed",
                "alert_reason": delivery.reason,
                "live_activity_status": "suppressed",
                "live_activity_reason": delivery.reason,
            })
        else:
            delivery_fields.update(push_service._deliver_ai_message_push_if_background(
                store,
                body=visible_push_body,
                title=payload.get("title", "") or "IO",
                data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
                visual_state=payload.get("visualState") or payload.get("visual_state") or "reply",
            ))
    if delivery_fields:
        updated = store.update_chat_message_metadata(msg["id"], delivery_fields)
        if updated:
            msg = updated
    print(f"[chat:{store.user_id}] openclaw(v1, source={source}, type={content_type}) id={msg['id']}")
    return jsonify({"id": msg["id"], "ts": msg["ts"], "v": msg["v"]})


@bp.route("/v1/chat/poll", methods=["GET"])
def chat_poll():
    store = auth.require_user()
    chat_consumer._record_consumer_event(store, "poll")
    from proactive import resident_runtime_v2  # lazy: chat poll should not own proactive startup
    runtime_profile = resident_runtime_v2.resident_runtime_v2_public_profile(store)
    # Advertise the commit a self-hosted resident consumer should run, so it can
    # self-update to the commit this backend deploys (see chat_consumer).
    client_release = {"expected_consumer_commit": chat_consumer.expected_consumer_commit()}
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    timeout = min(float(request.args.get("timeout", 30)), 60)
    consumer_id = chat_service._request_chat_consumer_id()
    claim = chat_service._request_bool_arg("claim", default=True)

    pending = chat_service._pending_chat_messages_for_poll(
        store,
        since=since,
        consumer_id=consumer_id,
        claim=claim,
    )
    if pending:
        return jsonify({
            "messages": pending,
            "runtime_v2": runtime_profile,
            "client_release": client_release,
            "timed_out": False,
            "consumer_id": consumer_id,
            "claimed": claim,
        })

    ev = threading.Event()
    with store.chat_waiters_lock:
        store.chat_waiters.append(ev)

    notified = ev.wait(timeout=timeout)

    with store.chat_waiters_lock:
        try:
            store.chat_waiters.remove(ev)
        except ValueError:
            pass

    if notified:
        pending = chat_service._pending_chat_messages_for_poll(
            store,
            since=since,
            consumer_id=consumer_id,
            claim=claim,
        )
        return jsonify({
            "messages": pending,
            "runtime_v2": runtime_profile,
            "client_release": client_release,
            "timed_out": False,
            "consumer_id": consumer_id,
            "claimed": claim,
        })
    return jsonify({
        "messages": [],
        "runtime_v2": runtime_profile,
        "client_release": client_release,
        "timed_out": True,
        "consumer_id": consumer_id,
        "claimed": claim,
    })
