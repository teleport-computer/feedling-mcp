"""Proactive HTTP surface: /v1/proactive/*, /v1/device/events, /debug/proactive."""

import threading
import time
from datetime import datetime

from flask import Blueprint, Response, jsonify, request

from accounts import auth
from core import store as core_store
from core import util
from proactive import dashboard, gate, service

bp = Blueprint("proactive", __name__)

@bp.route("/v1/proactive/settings", methods=["GET", "POST"])
def proactive_settings():
    store = auth.require_user()
    if request.method == "GET":
        return jsonify(store.load_proactive_settings())
    payload = request.get_json(silent=True) or {}
    settings = store.save_proactive_settings(payload)
    return jsonify(settings)


@bp.route("/v1/proactive/state", methods=["GET", "POST"])
def proactive_state():
    store = auth.require_user()
    if request.method == "GET":
        settings = store.load_proactive_settings()
        return jsonify({
            "version": settings.get("version", 2),
            "enabled": bool(settings.get("enabled", True)),
            "dnd": bool(settings.get("dnd", False)),
            "user_state": settings.get("user_state", "default"),
            "manual_user_state": settings.get("manual_user_state", settings.get("user_state", "default")),
            "ai_state": settings.get("ai_state", "present"),
            "broadcast_state": settings.get("broadcast_state", "unknown"),
            "updated_at": settings.get("updated_at", ""),
        })
    payload = request.get_json(silent=True) or {}
    settings = store.save_proactive_settings({
        key: payload.get(key)
        for key in ("user_state", "manual_user_state", "ai_state", "broadcast_state", "enabled", "dnd")
        if key in payload
    })
    return jsonify({
        "version": settings.get("version", 2),
        "enabled": bool(settings.get("enabled", True)),
        "dnd": bool(settings.get("dnd", False)),
        "user_state": settings.get("user_state", "default"),
        "manual_user_state": settings.get("manual_user_state", settings.get("user_state", "default")),
        "ai_state": settings.get("ai_state", "present"),
        "broadcast_state": settings.get("broadcast_state", "unknown"),
        "updated_at": settings.get("updated_at", ""),
    })


@bp.route("/v1/device/events", methods=["GET", "POST"])
def device_events():
    store = auth.require_user()
    if request.method == "GET":
        try:
            since = float(request.args.get("since", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid since"}), 400
        try:
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid limit"}), 400
        limit = max(1, min(limit, 200))
        return jsonify({"events": store.list_device_events(since_epoch=since, limit=limit)})

    payload = request.get_json(silent=True) or {}
    event = service._make_device_event(
        source=str(payload.get("source") or "ios"),
        event_type=str(payload.get("type") or payload.get("event_type") or "unknown"),
        payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
    )
    store.append_device_event(event)
    return jsonify(event)


@bp.route("/v1/proactive/tick", methods=["POST"])
def proactive_tick():
    """Create a proactive wake job.

    V2 is agent-owned: the server may mechanically suppress disabled/away
    automatic wakes, but it no longer decrypts frames, calls a platform LLM, or
    requires a memory connection.
    """
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    decision = gate._build_proactive_v2_wake_decision(store, payload, api_key=auth._extract_api_key())
    store.append_gate_decision(decision)

    job = None
    if decision.get("should_wake_agent", decision.get("should_reach_out")):
        job = store.append_proactive_job(gate._proactive_job_from_decision(decision))

    return jsonify({
        "decision": decision,
        "job": job,
        "enqueued": job is not None,
    })


def _job_status_patch(payload: dict, *, default_status: str = "") -> dict:
    status = str(payload.get("status") or default_status).strip().lower()
    reason = str(payload.get("reason") or payload.get("status_reason") or "").strip()
    consumer_id = str(payload.get("consumer_id") or "").strip()
    now_iso = datetime.now().isoformat()
    patch: dict = {}
    if status:
        patch["status"] = status[:80]
        if status == "claimed":
            patch["claimed_at"] = now_iso
        elif status == "realizing":
            patch["realizing_at"] = now_iso
        elif status in {"posted", "delivered"}:
            patch["posted_at"] = now_iso
        elif status == "completed":
            patch["completed_at"] = now_iso
        elif status in {"failed", "skipped"}:
            patch["failed_at"] = now_iso
    if reason:
        patch["status_reason"] = reason[:500]
    if consumer_id:
        patch["consumer_id"] = consumer_id[:160]
    if payload.get("chat_message_id"):
        patch["chat_message_id"] = str(payload.get("chat_message_id"))[:160]
    if payload.get("agent_action"):
        patch["agent_action"] = str(payload.get("agent_action"))[:120]
    if payload.get("agent_action_status"):
        patch["agent_action_status"] = str(payload.get("agent_action_status"))[:240]
    if isinstance(payload.get("agent_actions"), list):
        safe_actions = []
        for action in payload.get("agent_actions", [])[:10]:
            if not isinstance(action, dict):
                continue
            safe_action = {
                str(k)[:80]: (v if isinstance(v, (bool, int, float)) or v is None else str(v)[:500])
                for k, v in action.items()
                if str(k)
            }
            safe_actions.append(safe_action)
        patch["agent_actions"] = safe_actions
    if payload.get("wake_result"):
        patch["wake_result"] = str(payload.get("wake_result"))[:120]
    if payload.get("ai_state"):
        ai_state = service._normalize_proactive_state(payload.get("ai_state"), core_store.PROACTIVE_AI_STATES, "")
        if ai_state:
            patch["ai_state"] = ai_state
    if payload.get("broadcast_state"):
        broadcast_state = service._normalize_proactive_state(payload.get("broadcast_state"), core_store.PROACTIVE_BROADCAST_STATES, "")
        if broadcast_state:
            patch["broadcast_state"] = broadcast_state
    if isinstance(payload.get("request_broadcast"), dict):
        req = payload.get("request_broadcast") or {}
        try:
            duration_sec = int(req.get("duration_sec") or 0)
        except (TypeError, ValueError):
            duration_sec = 0
        patch["request_broadcast"] = {
            "reason": str(req.get("reason") or "")[:500],
            "duration_sec": max(0, min(duration_sec, 3600)),
            "copy": str(req.get("copy") or req.get("message") or "")[:500],
        }
    return patch


@bp.route("/v1/proactive/jobs/<job_id>/claim", methods=["POST"])
def proactive_job_claim(job_id):
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    patch = _job_status_patch(payload, default_status="claimed")
    job = store.update_proactive_job(job_id, patch, only_if_status="pending")
    if job is None:
        current = None
        for row in store.list_proactive_jobs(since_epoch=0, limit=0):
            if str(row.get("job_id") or "") == str(job_id):
                current = row
                break
        return jsonify({"claimed": False, "job": current, "reason": "not_pending_or_missing"})
    return jsonify({"claimed": True, "job": job})


@bp.route("/v1/proactive/jobs/<job_id>/status", methods=["POST"])
def proactive_job_status(job_id):
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    patch = _job_status_patch(payload)
    if not patch:
        return jsonify({"error": "empty_status_patch"}), 400
    job = store.update_proactive_job(job_id, patch)
    if job is None:
        return jsonify({"error": "job_not_found"}), 404
    return jsonify({"job": job})


@bp.route("/v1/proactive/decisions", methods=["GET"])
def proactive_decisions():
    store = auth.require_user()
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 200))
    return jsonify({"decisions": store.list_gate_decisions(since_epoch=since, limit=limit)})


@bp.route("/v1/proactive/decisions/<decision_id>/review", methods=["POST"])
def proactive_decision_review(decision_id):
    store = auth.require_user()
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form.to_dict(flat=True)

    decision_id = str(decision_id or "").strip()
    if not decision_id:
        return jsonify({"error": "decision_id_required"}), 400

    decision = None
    for row in store.list_gate_decisions(since_epoch=0, limit=0):
        if str(row.get("decision_id") or "") == decision_id:
            decision = row
            break
    if decision is None:
        return jsonify({"error": "decision_not_found"}), 404

    label = str(payload.get("label") or "").strip()
    allowed_labels = {
        "correct_true",
        "correct_false",
        "missed_opportunity",
        "spam",
        "weak_connection",
        "repeated",
        "privacy_bad",
        "great_companion_moment",
    }
    if label not in allowed_labels:
        return jsonify({"error": "invalid_label", "allowed": sorted(allowed_labels)}), 400

    review = {
        "review_id": util._new_public_id("gr"),
        "decision_id": decision_id,
        "ts": time.time(),
        "created_at": datetime.now().isoformat(),
        "label": label,
        "notes": str(payload.get("notes") or "")[:500],
        "reviewer": str(payload.get("reviewer") or "human")[:80],
        "expected_should_reach_out": payload.get("expected_should_reach_out"),
        "correct_connection_source_id": str(payload.get("correct_connection_source_id") or "")[:160],
        "decision_should_reach_out": bool(decision.get("should_reach_out")),
        "decision_reason": str(decision.get("reason") or decision.get("abstention_reason") or "")[:240],
        "decision_intent_label": str(decision.get("intent_label") or "")[:120],
        "decision_connection": decision.get("connection") or {},
        "frame_ids": decision.get("frame_ids") or [],
    }
    store.append_gate_review(review)
    accept = request.headers.get("Accept", "")
    if not request.is_json and "text/html" in accept:
        return Response(
            "<html><body><p>Review saved.</p><script>history.back()</script></body></html>",
            mimetype="text/html",
        )
    return jsonify({"review": review})


@bp.route("/v1/proactive/reviews", methods=["GET"])
def proactive_reviews():
    store = auth.require_user()
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 500))
    return jsonify({"reviews": store.list_gate_reviews(since_epoch=since, limit=limit)})


@bp.route("/v1/proactive/debug", methods=["GET"])
def proactive_debug_json():
    store = auth.require_user()
    return jsonify(dashboard._proactive_debug_snapshot(store))


@bp.route("/debug/proactive", methods=["GET"])
def proactive_debug_page():
    store = auth.require_user()
    html_body = dashboard._render_proactive_dashboard(dashboard._proactive_debug_snapshot(store))
    return Response(html_body, mimetype="text/html")


@bp.route("/v1/proactive/jobs/poll", methods=["GET"])
def proactive_jobs_poll():
    store = auth.require_user()
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        timeout = max(0.0, min(float(request.args.get("timeout", 30)), 60))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid timeout"}), 400
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 100))

    pending = [
        j for j in store.list_proactive_jobs(since_epoch=since, limit=limit)
        if str(j.get("status") or "pending") == "pending"
    ]
    if pending:
        return jsonify({"jobs": pending, "timed_out": False})

    ev = threading.Event()
    with store.proactive_job_waiters_lock:
        store.proactive_job_waiters.append(ev)

    notified = ev.wait(timeout=timeout)

    with store.proactive_job_waiters_lock:
        try:
            store.proactive_job_waiters.remove(ev)
        except ValueError:
            pass

    if notified:
        pending = [
            j for j in store.list_proactive_jobs(since_epoch=since, limit=limit)
            if str(j.get("status") or "pending") == "pending"
        ]
        return jsonify({"jobs": pending, "timed_out": False})
    return jsonify({"jobs": [], "timed_out": True})
