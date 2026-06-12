"""Screen HTTP surface: /v1/screen/*, /v1/sources."""

import json
import os
import time
from datetime import datetime

import httpx
from flask import Blueprint, Response, jsonify, request

import db
from accounts import auth
from screen import frames
from screen import summary as summary_mod
from semantic_analysis import analyze as _semantic_analysis

bp = Blueprint("screen", __name__)

@bp.route("/v1/screen/ios", methods=["GET"])
def get_ios():
    store = auth.require_user()
    try:
        window_sec = max(300.0, min(172800.0, float(request.args.get("window_sec", 86400))))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid window_sec"}), 400
    return jsonify(summary_mod._build_ios_data(store, window_sec=window_sec))


@bp.route("/v1/screen/mac", methods=["GET"])
def get_mac():
    auth.require_user()
    return jsonify(summary_mod.MAC_DATA)


@bp.route("/v1/screen/summary", methods=["GET"])
def get_summary():
    store = auth.require_user()
    ios_data = summary_mod._build_ios_data(store, window_sec=86400)
    top_app = ios_data["apps"][0]["name"] if ios_data.get("apps") else "Unknown"
    categories = ios_data.get("categories") or {}
    top_category = max(categories, key=categories.get) if categories else "Other"

    summary = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ios": {
            "total_screen_time_minutes": ios_data.get("total_screen_time_minutes", 0),
            "top_app": top_app,
            "top_category": top_category,
            "pickups": ios_data.get("pickups", 0),
            "data_source": ios_data.get("data_source", "unknown"),
            "frame_count": ios_data.get("frame_count", 0),
        },
        "mac": {
            "total_active_minutes": summary_mod.MAC_DATA["total_active_minutes"],
            "deep_work_minutes": summary_mod.MAC_DATA["deep_work_minutes"],
            "focus_score": summary_mod.MAC_DATA["focus_score"],
            "top_app": summary_mod.MAC_DATA["apps"][0]["name"],
            "context_switches": summary_mod.MAC_DATA["context_switches"],
        },
        "combined": {
            "total_screen_minutes": ios_data.get("total_screen_time_minutes", 0) + summary_mod.MAC_DATA["total_active_minutes"],
            "insight": "Phone side now comes from real frame aggregation; Mac remains mocked.",
        },
    }
    return jsonify(summary)


@bp.route("/v1/sources", methods=["GET"])
def get_sources():
    auth.require_user()
    return jsonify(summary_mod.SOURCES_DATA)


@bp.route("/v1/screen/frames", methods=["GET"])
def list_frames():
    store = auth.require_user()
    limit = min(int(request.args.get("limit", 20)), 100)
    with store.frames_lock:
        recent = [f.copy() for f in reversed(store.frames_meta)][:limit]
    for f in recent:
        f["url"] = frames._frame_url(store, f["filename"])
    return jsonify({"frames": recent, "total": len(store.frames_meta)})


@bp.route("/v1/screen/frames/latest", methods=["GET"])
def latest_frame():
    store = auth.require_user()
    with store.frames_lock:
        if not store.frames_meta:
            return jsonify({"error": "no frames yet"}), 404
        meta = store.frames_meta[-1].copy()
    # image_base64 used to be included here, but every frame is a v1
    # envelope now — the file bytes are opaque ciphertext and only
    # waste ~900KB per call. Callers wanting pixels should hit
    # /v1/screen/frames/<id>/decrypt (or the decrypt_frame MCP tool).
    meta["url"] = frames._frame_url(store, meta["filename"])
    return jsonify(meta)


@bp.route("/v1/screen/frames/<filename>", methods=["GET"])
def serve_frame(filename):
    store = auth.require_user()
    # Reject path traversal
    if "/" in filename or ".." in filename:
        return jsonify({"error": "bad filename"}), 400
    # Filenames are `<frame_id>.env.json`; map back to the frame_id and serve
    # the stored envelope JSON bytes (frames are always v1 ciphertext now).
    frame_id = filename.split(".")[0]
    env = db.frame_get(store.user_id, frame_id)
    if env is None:
        return jsonify({"error": "not found"}), 404
    return Response(json.dumps(env), mimetype="application/json")


@bp.route("/v1/screen/frames/<frame_id>/envelope", methods=["GET"])
def frame_envelope(frame_id):
    """Return the raw v1 envelope JSON for a single frame.

    Callers needing plaintext should hit /v1/screen/frames/<id>/decrypt
    instead — this endpoint exists primarily so the enclave can pull the
    ciphertext back for in-enclave decryption.
    """
    store = auth.require_user()
    env = frames._load_envelope(store, frame_id)
    if env is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(env)


@bp.route("/v1/screen/frames/<frame_id>/decrypt", methods=["GET"])
def frame_decrypt(frame_id):
    """Proxy to the enclave's decrypt endpoint so API-only clients get
    plaintext without needing the MCP transport.

    Query params are forwarded untouched; the enclave honors
    `include_image=true|false` to gate the base64 JPEG payload (large).
    """
    store = auth.require_user()
    if not frames._frame_exists(store, frame_id):
        return jsonify({"error": "not found"}), 404

    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return jsonify({"error": "enclave unreachable — FEEDLING_ENCLAVE_URL not set"}), 503

    # Forward the caller's api_key + any include_image flag.
    api_key = auth._extract_api_key()
    headers = {"X-API-Key": api_key} if api_key else {}
    params = {"include_image": request.args.get("include_image", "true")}
    try:
        with httpx.Client(timeout=30, verify=False) as client:
            r = client.get(
                f"{enclave_url}/v1/screen/frames/{frame_id}/decrypt",
                headers=headers,
                params=params,
            )
        return (r.content, r.status_code, {"Content-Type": r.headers.get("Content-Type", "application/json")})
    except httpx.HTTPError as e:
        return jsonify({"error": f"enclave_error: {e}"}), 502


@bp.route("/v1/screen/frames/<frame_id>/image", methods=["GET"])
def frame_image(frame_id):
    """Proxy to the enclave's raw-JPEG endpoint, passing Range through.

    Returns Content-Type image/jpeg with Accept-Ranges: bytes. Clients
    can issue parallel Range GETs to bypass the per-TCP-connection
    throttle on dstack-gateway (~1 Mbps/stream, ~3-4 Mbps aggregate).
    """
    store = auth.require_user()
    if not frames._frame_exists(store, frame_id):
        return jsonify({"error": "not found"}), 404

    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return jsonify({"error": "enclave unreachable — FEEDLING_ENCLAVE_URL not set"}), 503

    api_key = auth._extract_api_key()
    # Forward api_key + Range (if present) so the enclave's send_file
    # can respond 206 Partial Content with the requested slice.
    fwd_headers = {"X-API-Key": api_key} if api_key else {}
    if request.headers.get("Range"):
        fwd_headers["Range"] = request.headers["Range"]
    try:
        with httpx.Client(timeout=30, verify=False) as client:
            r = client.get(
                f"{enclave_url}/v1/screen/frames/{frame_id}/image",
                headers=fwd_headers,
            )
    except httpx.HTTPError as e:
        return jsonify({"error": f"enclave_error: {e}"}), 502

    resp_headers = {}
    for h in ("Content-Type", "Content-Length", "Content-Range",
              "Accept-Ranges", "ETag", "Last-Modified"):
        if r.headers.get(h):
            resp_headers[h] = r.headers[h]
    return (r.content, r.status_code, resp_headers)


@bp.route("/v1/screen/analyze", methods=["GET"])
def analyze_screen():
    store = auth.require_user()
    now = time.time()
    window_sec = max(30.0, min(3600.0, float(request.args.get("window_sec", 300))))
    min_continuous_min = max(1.0, min(120.0, float(request.args.get("min_continuous_min", 3))))

    with store.frames_lock:
        recent = [f for f in store.frames_meta if now - f["ts"] <= window_sec]

    if not recent:
        return jsonify({
            "active": False,
            "rate_limit_ok": False,
            "reason": "No frames in window — phone screen may be off or recording stopped.",
            "current_app": None,
            "continuous_minutes": 0,
            "ocr_summary": "",
            "cooldown_remaining_seconds": round(store.cooldown_remaining_seconds()),
            "latest_ts": None,
            "latest_frame_filename": None,
            "latest_frame_url": None,
            "frame_count_in_window": 0,
        })

    latest = recent[-1]
    current_app = latest.get("app") or "unknown"

    MAX_GAP_SECONDS = 8
    MAX_JITTER_FRAMES = 2

    continuous_start_ts = latest["ts"]
    jitter_count = 0
    prev_ts = latest["ts"]

    for frame in reversed(recent[:-1]):
        if prev_ts - frame["ts"] > MAX_GAP_SECONDS:
            break
        fapp = frame.get("app") or "unknown"
        if fapp == current_app:
            continuous_start_ts = frame["ts"]
            jitter_count = 0
        else:
            jitter_count += 1
            if jitter_count > MAX_JITTER_FRAMES:
                break
        prev_ts = frame["ts"]

    continuous_minutes = round((latest["ts"] - continuous_start_ts) / 60, 1)

    seen_ocr: set[str] = set()
    ocr_parts: list[str] = []
    for f in reversed(recent):
        text = (f.get("ocr_text") or "").strip()
        if text and text not in seen_ocr:
            seen_ocr.add(text)
            ocr_parts.append(text[:200])
            if len(ocr_parts) >= 3:
                break
    ocr_summary = " | ".join(reversed(ocr_parts))[:500]

    cooldown_remaining = store.cooldown_remaining_seconds()
    rate_limit_ok = cooldown_remaining == 0
    semantic = _semantic_analysis(current_app=current_app, ocr_summary=ocr_summary)
    semantic_strength = semantic.get("semantic_strength", "weak")

    exploratory_allowed = (
        semantic_strength == "weak"
        and len(ocr_summary) >= 20
        and continuous_minutes >= 1.0
    )

    if semantic_strength == "strong":
        trigger_basis = "semantic_strong"
        reason = f"semantic:{semantic.get('semantic_scene', 'unknown')}"
    elif exploratory_allowed:
        trigger_basis = "curiosity_exploratory"
        reason = "ambiguous_context_but_conversation_worth_starting"
    elif continuous_minutes >= min_continuous_min:
        trigger_basis = "legacy_time_fallback"
        reason = f"continuous_minutes {continuous_minutes} >= min_continuous_min {min_continuous_min}"
    else:
        trigger_basis = "insufficient_signal"
        reason = "no_semantic_trigger_and_not_enough_context"

    return jsonify({
        "active": True,
        "current_app": current_app,
        "continuous_minutes": continuous_minutes,
        "ocr_summary": ocr_summary,
        "rate_limit_ok": rate_limit_ok,
        "cooldown_remaining_seconds": round(cooldown_remaining),
        "reason": reason,
        "trigger_policy": "semantic_first",
        "trigger_basis": trigger_basis,
        "semantic_scene": semantic.get("semantic_scene"),
        "task_intent": semantic.get("task_intent"),
        "friction_point": semantic.get("friction_point"),
        "semantic_confidence": semantic.get("confidence", 0.0),
        "suggested_openers": semantic.get("suggested_openers", [])[:2],
        "latest_ts": latest["ts"],
        "latest_frame_filename": latest.get("filename"),
        "latest_frame_url": frames._frame_url(store, latest.get("filename")) if latest.get("filename") else None,
        "frame_count_in_window": len(recent),
    })
