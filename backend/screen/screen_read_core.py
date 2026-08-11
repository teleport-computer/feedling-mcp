"""Framework-neutral screen read operations (ASGI-migration plan §5.3).

A pure relocation of the Flask ``/v1/screen/*`` + ``/v1/sources`` route bodies so
both the Flask adapter (``screen.routes``) and the native FastAPI router
(``screen.routes_asgi``) share one implementation and return byte-identical
responses. No ``flask.request`` here — every route reads already-parsed params +
the resolved store (and, for the enclave proxies, the caller's credential) as
explicit arguments.

Content boundary: encrypted-tier frames remain v1 E2E envelopes; plaintext-tier
frames are strict ``body_b64`` envelopes and are decoded locally.

  - ``serve_frame`` / ``frame_envelope`` return the stored shape verbatim.
  - ``frame_decrypt`` / ``frame_image`` are pure PROXIES to the enclave: they
    forward the caller's credential (runtime token OR api key, runtime token
    winning — mirroring the old ``_enclave_forward_auth``) to the enclave's own
    ``/decrypt`` / ``/image`` endpoint and stream its bytes/status/headers back.
    Encrypted decryption happens INSIDE the enclave; plaintext-tier reads bypass
    that proxy and decode inside the managed service boundary.

All store / DB / enclave work is blocking, so ASGI callers run these through
``threadpool.run_db`` off the event loop (plan §5.2).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx

import db
from core import enclave as core_enclave
from screen import frames
from screen import summary as summary_mod
from semantic_analysis import analyze as _semantic_analysis

log = logging.getLogger(__name__)


@dataclass
class ScreenResult:
    """Framework-neutral description of a screen route's response.

    Either a JSON body (``json_body`` set → rendered as jsonify / JSONResponse)
    or an opaque raw body (``raw_body`` set → rendered as a raw Response carrying
    ``media_type`` and/or ``headers`` verbatim). The two are mutually exclusive;
    ``raw_body is not None`` selects the raw path.
    """

    status: int
    json_body: Optional[Any] = None
    raw_body: Optional[bytes | str] = None
    media_type: Optional[str] = None
    headers: dict = field(default_factory=dict)


def enclave_forward_headers(*, api_key: str | None, runtime_token: str | None) -> dict:
    """Auth header to forward to the enclave, preferring the Stage-D runtime token.

    Mirrors the old Flask ``_enclave_forward_auth``: Runtime V2 workers
    have NO per-user api_key, so the runtime token must win. Empty dict only when
    neither credential is present.
    """
    if runtime_token:
        return {"X-Feedling-Runtime-Token": runtime_token}
    return {"X-API-Key": api_key} if api_key else {}


SCREEN_SHARE_LIVE_SEC = 90.0
SCREEN_SHARE_STALLED_SEC = 5 * 60.0
SCREEN_BROADCAST_FUTURE_SKEW_SEC = 2 * 60.0
_BROADCAST_ACTIVE_STATES = frozenset({"on", "active", "broadcasting", "true", "1"})
_BROADCAST_INACTIVE_STATES = frozenset({"off", "inactive", "stopped", "false", "0"})


def broadcast_report_is_recently_active(
    report: dict, *, now: float
) -> bool:
    cells: list[tuple[float, bool]] = []
    for value, timestamp_key in (
        (report.get("broadcast_state"), "broadcast_state_ts"),
        (report.get("broadcast_active"), "broadcast_active_ts"),
    ):
        normalized = str(value if value is not None else "").strip().lower()
        if normalized not in _BROADCAST_ACTIVE_STATES | _BROADCAST_INACTIVE_STATES:
            continue
        try:
            timestamp = float(report.get(timestamp_key))
        except (TypeError, ValueError):
            continue
        cells.append((timestamp, normalized in _BROADCAST_ACTIVE_STATES))
    if not cells:
        return False
    newest_ts = max(timestamp for timestamp, _is_active in cells)
    report_age = now - newest_ts
    if report_age < -SCREEN_BROADCAST_FUTURE_SKEW_SEC:
        log.debug(
            "broadcast report ignored because device timestamp is %.1fs ahead",
            -report_age,
        )
        return False
    if report_age > SCREEN_SHARE_STALLED_SEC:
        return False
    newest_values = [is_active for ts, is_active in cells if ts == newest_ts]
    return any(newest_values) and all(newest_values)


def screen_share_grounding(
    user_id: str, *, latest_frame_ts: float | None = None
) -> dict:
    """Return live, stalled, or absent state from content-free metadata.

    Foreground grounding and the ``screen_read`` default must never disagree
    about whether sharing is active. Both therefore use this one metadata-only
    predicate. A frame no more than 90 seconds old is live. A fresh device
    broadcast-on report paired with a frame older than five minutes is stalled
    and carries a restart action. Frame ids and content never leave this helper.
    """
    if latest_frame_ts is None:
        try:
            meta = db.frame_list_meta(user_id)
        except Exception:
            return {}
        if not meta:
            return {}
        latest_frame_ts = meta[-1].get("ts")
    now = time.time()
    try:
        age = now - float(latest_frame_ts)
    except (TypeError, ValueError):
        return {}
    if age < 0:
        return {}
    if age <= SCREEN_SHARE_LIVE_SEC:
        return {"active": True, "latest_frame_age_sec": int(age)}
    # Give a transient disconnect time to self-heal before telling the user to
    # restart sharing. The iOS reconnect backoff is capped well below 5 minutes.
    if age <= SCREEN_SHARE_STALLED_SEC:
        return {}
    try:
        broadcast = db.perception_broadcast_meta(user_id)
    except Exception:
        return {}
    if not broadcast_report_is_recently_active(broadcast, now=now):
        return {}
    return {
        "active": False,
        "stalled": True,
        "status": "broadcast_on_without_recent_frames",
        "latest_frame_age_sec": int(age),
        "suggested_action": "Ask the user to stop and restart screen sharing.",
    }


def _trace_enclave_proxy(
    store,
    event_type: str,
    *,
    path: str,
    purpose: str,
    status: str = "ok",
    summary: str = "",
    detail: dict | None = None,
    dur_ms: float | None = None,
) -> None:
    core_enclave._trace_enclave(
        store,
        event_type,
        path=path,
        purpose=purpose,
        status=status,
        summary=summary,
        explain=(
            "Screen route proxied a request to the enclave; only metadata is "
            "recorded."
        ),
        detail=detail,
        dur_ms=dur_ms,
    )


# --------------------------------------------------------------------------- #
# Aggregated screen-time reads (JSON only)
# --------------------------------------------------------------------------- #

def ios_data(store, window_sec_raw) -> ScreenResult:
    try:
        window_sec = max(300.0, min(172800.0, float(window_sec_raw if window_sec_raw is not None else 86400)))
    except (TypeError, ValueError):
        return ScreenResult(400, json_body={"error": "invalid window_sec"})
    return ScreenResult(200, json_body=summary_mod._build_ios_data(store, window_sec=window_sec))


def mac_data(store) -> ScreenResult:
    return ScreenResult(200, json_body=summary_mod.MAC_DATA)


def summary_data(store) -> ScreenResult:
    ios = summary_mod._build_ios_data(store, window_sec=86400)
    top_app = ios["apps"][0]["name"] if ios.get("apps") else "Unknown"
    categories = ios.get("categories") or {}
    top_category = max(categories, key=categories.get) if categories else "Other"

    summary = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ios": {
            "total_screen_time_minutes": ios.get("total_screen_time_minutes", 0),
            "top_app": top_app,
            "top_category": top_category,
            "pickups": ios.get("pickups", 0),
            "data_source": ios.get("data_source", "unknown"),
            "frame_count": ios.get("frame_count", 0),
        },
        "mac": {
            "total_active_minutes": summary_mod.MAC_DATA["total_active_minutes"],
            "deep_work_minutes": summary_mod.MAC_DATA["deep_work_minutes"],
            "focus_score": summary_mod.MAC_DATA["focus_score"],
            "top_app": summary_mod.MAC_DATA["apps"][0]["name"],
            "context_switches": summary_mod.MAC_DATA["context_switches"],
        },
        "combined": {
            "total_screen_minutes": ios.get("total_screen_time_minutes", 0) + summary_mod.MAC_DATA["total_active_minutes"],
            "insight": "Phone side now comes from real frame aggregation; Mac remains mocked.",
        },
    }
    return ScreenResult(200, json_body=summary)


def sources_data(store) -> ScreenResult:
    return ScreenResult(200, json_body=summary_mod.SOURCES_DATA)


# --------------------------------------------------------------------------- #
# Frame index reads (JSON only)
# --------------------------------------------------------------------------- #

def list_frames(store, limit_raw) -> ScreenResult:
    limit = min(int(limit_raw if limit_raw is not None else 20), 100)
    with store.frames_lock:
        recent = [f.copy() for f in reversed(store.frames_meta)][:limit]
        latest_ts = store.frames_meta[-1].get("ts") if store.frames_meta else None
        total = len(store.frames_meta)
    for f in recent:
        f["url"] = frames._frame_url(store, f["filename"])
    share_state = (
        screen_share_grounding(store.user_id, latest_frame_ts=latest_ts)
        if latest_ts is not None
        else {}
    )
    return ScreenResult(
        200,
        json_body={
            "frames": recent,
            "total": total,
            "screen_share": share_state,
        },
    )


def latest_frame(store) -> ScreenResult:
    with store.frames_lock:
        if not store.frames_meta:
            return ScreenResult(404, json_body={"error": "no frames yet"})
        meta = store.frames_meta[-1].copy()
    # image_base64 is intentionally omitted — every frame is a v1 envelope now,
    # the file bytes are opaque ciphertext. Callers wanting pixels hit /decrypt.
    meta["url"] = frames._frame_url(store, meta["filename"])
    return ScreenResult(200, json_body=meta)


# --------------------------------------------------------------------------- #
# Envelope reads (opaque ciphertext — NEVER decrypted server-side)
# --------------------------------------------------------------------------- #

def serve_frame(store, filename: str) -> ScreenResult:
    # Reject path traversal
    if "/" in filename or ".." in filename:
        return ScreenResult(400, json_body={"error": "bad filename"})
    # Filenames are `<frame_id>.env.json`; map back to the frame_id and serve the
    # stored envelope JSON bytes (frames are always v1 ciphertext now).
    frame_id = filename.split(".")[0]
    env = db.frame_get(store.user_id, frame_id)
    if env is None:
        return ScreenResult(404, json_body={"error": "not found"})
    # Byte-identical to Flask ``Response(json.dumps(env), mimetype="application/json")``.
    return ScreenResult(200, raw_body=json.dumps(env), media_type="application/json")


def frame_envelope(store, frame_id: str) -> ScreenResult:
    """Return the raw v1 envelope JSON for a single frame (opaque ciphertext)."""
    env = frames._load_envelope(store, frame_id)
    if env is None:
        return ScreenResult(404, json_body={"error": "not found"})
    return ScreenResult(200, json_body=env)


# --------------------------------------------------------------------------- #
# Enclave decrypt proxies (decryption happens INSIDE the enclave, not here)
# --------------------------------------------------------------------------- #

def frame_decrypt(
    store, frame_id: str, *, include_image: str, api_key: str | None, runtime_token: str | None
) -> ScreenResult:
    """Proxy to the enclave's decrypt endpoint so API-only clients get plaintext.

    The enclave owns decryption. This process only forwards the caller's
    credential + the ``include_image`` flag and relays the enclave's response
    bytes/status/content-type. No plaintext is produced here.
    """
    env = db.frame_get(store.user_id, frame_id)
    if env is None:
        return ScreenResult(404, json_body={"error": "not found"})
    inner = frames._read_plaintext_frame(env)
    if inner is not None:
        include = str(include_image).lower() != "false"
        result = {
            "id": frame_id, "ts": inner.get("ts") or env.get("ts"),
            "app": inner.get("app"), "bundle": inner.get("bundle"),
            "ocr_text": inner.get("ocr_text", ""), "urls": inner.get("urls", []),
            "w": inner.get("w", 0), "h": inner.get("h", 0),
            "tier_hint": inner.get("tier_hint"), "v": int(env.get("v", 1)),
            "owner_user_id": store.user_id, "decrypt_status": "plaintext",
        }
        if include:
            result["image_b64"] = inner.get("image", "")
            result["image_mime"] = inner.get("image_mime") or "image/jpeg"
        else:
            result["image_b64"] = None
            result["image_bytes_omitted"] = True
        return ScreenResult(200, json_body=result)

    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return ScreenResult(503, json_body={"error": "enclave unreachable — FEEDLING_ENCLAVE_URL not set"})

    headers = enclave_forward_headers(api_key=api_key, runtime_token=runtime_token)
    params = {"include_image": include_image}
    path = f"/v1/screen/frames/{frame_id}/decrypt"
    started_at = time.time()
    _trace_enclave_proxy(
        store,
        "enclave.call.start",
        path=path,
        purpose="screen_frame_decrypt",
        summary="screen frame enclave decrypt started",
    )
    try:
        with httpx.Client(timeout=30, verify=False) as client:
            r = client.get(
                f"{enclave_url}/v1/screen/frames/{frame_id}/decrypt",
                headers=headers,
                params=params,
            )
        _trace_enclave_proxy(
            store,
            "enclave.call.done" if r.status_code < 400 else "enclave.call.error",
            path=path,
            purpose="screen_frame_decrypt",
            status="ok" if r.status_code < 400 else "error",
            summary="screen frame enclave decrypt returned",
            detail={"status_code": r.status_code, "include_image": include_image},
            dur_ms=(time.time() - started_at) * 1000,
        )
        return ScreenResult(
            r.status_code,
            raw_body=r.content,
            media_type=r.headers.get("Content-Type", "application/json"),
        )
    except httpx.HTTPError as e:
        _trace_enclave_proxy(
            store,
            "enclave.call.timeout" if isinstance(e, httpx.TimeoutException) else "enclave.call.error",
            path=path,
            purpose="screen_frame_decrypt",
            status="error",
            summary="screen frame enclave decrypt failed",
            detail={"error_class": type(e).__name__},
            dur_ms=(time.time() - started_at) * 1000,
        )
        return ScreenResult(502, json_body={"error": f"enclave_error: {e}"})


def frame_image(
    store, frame_id: str, *, range_header: str | None, api_key: str | None, runtime_token: str | None
) -> ScreenResult:
    """Proxy to the enclave's raw-JPEG endpoint, passing Range through.

    Returns the enclave's Content-Type image/jpeg with Accept-Ranges: bytes (and
    Content-Range on 206). The enclave owns decryption; this only relays bytes.
    """
    env = db.frame_get(store.user_id, frame_id)
    if env is None:
        return ScreenResult(404, json_body={"error": "not found"})
    inner = frames._read_plaintext_frame(env)
    if inner is not None:
        try:
            image = base64.b64decode(str(inner.get("image") or ""), validate=True)
        except Exception:
            return ScreenResult(502, json_body={"error": "image_b64_decode"})
        return ScreenResult(200, raw_body=image,
                            media_type=str(inner.get("image_mime") or "image/jpeg"),
                            headers={"Accept-Ranges": "bytes", "Content-Length": str(len(image))})

    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return ScreenResult(503, json_body={"error": "enclave unreachable — FEEDLING_ENCLAVE_URL not set"})

    fwd_headers = enclave_forward_headers(api_key=api_key, runtime_token=runtime_token)
    if range_header:
        fwd_headers["Range"] = range_header
    path = f"/v1/screen/frames/{frame_id}/image"
    started_at = time.time()
    _trace_enclave_proxy(
        store,
        "enclave.call.start",
        path=path,
        purpose="screen_frame_image",
        summary="screen frame enclave image started",
    )
    try:
        with httpx.Client(timeout=30, verify=False) as client:
            r = client.get(
                f"{enclave_url}/v1/screen/frames/{frame_id}/image",
                headers=fwd_headers,
            )
    except httpx.HTTPError as e:
        _trace_enclave_proxy(
            store,
            "enclave.call.timeout" if isinstance(e, httpx.TimeoutException) else "enclave.call.error",
            path=path,
            purpose="screen_frame_image",
            status="error",
            summary="screen frame enclave image failed",
            detail={"error_class": type(e).__name__, "has_range": bool(range_header)},
            dur_ms=(time.time() - started_at) * 1000,
        )
        return ScreenResult(502, json_body={"error": f"enclave_error: {e}"})
    _trace_enclave_proxy(
        store,
        "enclave.call.done" if r.status_code < 400 else "enclave.call.error",
        path=path,
        purpose="screen_frame_image",
        status="ok" if r.status_code < 400 else "error",
        summary="screen frame enclave image returned",
        detail={"status_code": r.status_code, "has_range": bool(range_header)},
        dur_ms=(time.time() - started_at) * 1000,
    )

    resp_headers: dict = {}
    for h in ("Content-Type", "Content-Length", "Content-Range",
              "Accept-Ranges", "ETag", "Last-Modified"):
        if r.headers.get(h):
            resp_headers[h] = r.headers[h]
    return ScreenResult(r.status_code, raw_body=r.content, headers=resp_headers)


# --------------------------------------------------------------------------- #
# Semantic trigger analysis (JSON only)
# --------------------------------------------------------------------------- #

def analyze(store, window_sec_raw, min_continuous_min_raw) -> ScreenResult:
    now = time.time()
    window_sec = max(30.0, min(3600.0, float(window_sec_raw if window_sec_raw is not None else 300)))
    min_continuous_min = max(1.0, min(120.0, float(min_continuous_min_raw if min_continuous_min_raw is not None else 3)))

    with store.frames_lock:
        recent = [f for f in store.frames_meta if now - f["ts"] <= window_sec]

    if not recent:
        return ScreenResult(200, json_body={
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

    return ScreenResult(200, json_body={
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
