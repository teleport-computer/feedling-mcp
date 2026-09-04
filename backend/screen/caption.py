"""Backend side of the screen.read caption tool.

Encrypted frames are captioned in the enclave. Plaintext frames never cross
that boundary and currently return an explicit unsupported error. Captions
are cached per frame_id so repeated reads do not re-bill the VLM.
"""
from __future__ import annotations

import os

import httpx

import db
from core import envelope as core_envelope
from core.reqctx import request
from perception import store as perception_store
from screen import frames as screen_frames

_CACHE_KIND = "screen_caption"
_CAPTION_TIMEOUT = 50.0


def _enclave_auth_headers(api_key: str | None, runtime_token: str | None = None) -> dict | None:
    """Auth header for an enclave call, preferring the Stage-D runtime token.

    Runtime V2 workers have no per-user api_key — their proactive screen
    captions are triggered by a request carrying X-Feedling-Runtime-Token. Pick it
    up from the neutral request context (core.reqctx) when no explicit token/api_key
    is supplied, so these reads stop returning api_key_unavailable. Returns None when
    no credential is available (true non-request background callers behave as before).
    """
    rt = runtime_token
    if not rt:
        rt = request.headers.get("X-Feedling-Runtime-Token", "").strip() or None
    if rt:
        return {"X-Feedling-Runtime-Token": rt}
    if api_key:
        return {"X-API-Key": api_key}
    return None


def _enclave_get(url: str, headers=None, params=None):
    with httpx.Client(timeout=_CAPTION_TIMEOUT, verify=False) as client:
        return client.get(url, headers=headers or {}, params=params or {})


def _cached_caption(user_id: str, frame_id: str) -> str | None:
    doc = perception_store.item_get(user_id, _CACHE_KIND, frame_id)
    if doc and doc.get("caption"):
        return str(doc["caption"])
    return None


def _frame_id_from_entry(entry: dict) -> str:
    """Derive frame_id from a frame_list_meta entry.

    Real entries have a ``filename`` field like ``"<frame_id>.env.json"`` and
    also an ``"id"`` field. We derive from ``filename`` (controller-mandated
    canonical source) and fall back to ``id`` for robustness.
    """
    filename = str(entry.get("filename") or "")
    if filename:
        # frame_ids are dotless hex strings, so split(".")[0] is safe here
        return filename.removesuffix(".env.json").split(".")[0]
    return str(entry.get("id") or "")


def _latest_frame_id(user_id: str) -> str | None:
    meta = db.frame_list_meta(user_id, source="screen")  # sorted by ts ascending
    if not meta:
        return None
    return _frame_id_from_entry(meta[-1]) or None


def caption_frame(user_id: str, api_key: str, frame_id: str | None,
                  mode: str = "caption") -> dict:
    fid = str(frame_id or "").strip() or (_latest_frame_id(user_id) or "")
    if not fid:
        return {"error": "no_recent_frame"}
    frame = db.frame_get(user_id, fid)
    if frame is None:
        return {"frame_id": fid, "error": "no_recent_frame"}

    shape = core_envelope.classify_envelope_shape(frame)
    if shape == "plaintext_binary":
        if screen_frames._read_plaintext_frame(frame) is not None:
            return {"frame_id": fid, "error": "frame_plaintext_caption_unsupported"}
        return {"frame_id": fid, "error": "frame_plaintext_invalid"}
    if shape != "sealed":
        return {"frame_id": fid, "error": "frame_plaintext_invalid"}

    # full mode is intentionally NOT cached: it returns richer output (ocr_text,
    # decrypt_status, etc.) that the caption-only cache key can't represent, so
    # each full read re-invokes the VLM by design.
    if mode != "full":
        hit = _cached_caption(user_id, fid)
        if hit is not None:
            return {"frame_id": fid, "caption": hit, "mode": mode, "cached": True}

    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return {"frame_id": fid, "error": "enclave_unavailable"}
    auth = _enclave_auth_headers(api_key)
    if auth is None:
        return {"frame_id": fid, "error": "api_key_unavailable"}

    try:
        resp = _enclave_get(
            f"{enclave_url}/v1/screen/frames/{fid}/caption",
            headers=auth,
            params={"mode": mode},
        )
    except Exception as e:  # connect/timeout — fail-closed, no pixels involved
        return {"frame_id": fid, "error": f"caption_error:{type(e).__name__}"}
    if resp.status_code >= 400:
        return {"frame_id": fid, "error": f"caption_http_{resp.status_code}",
                "body": resp.text[:200]}

    try:
        raw = resp.json()
    except Exception:
        return {"frame_id": fid, "error": "caption_error:JSONDecodeError"}
    data = raw if isinstance(raw, dict) else {}
    caption = str(data.get("caption") or "").strip()
    out = {"frame_id": fid, "caption": caption, "model": data.get("model"),
           "mode": mode, "cached": False}
    if data.get("ocr_text"):
        out["ocr_text"] = data["ocr_text"]
    if caption:
        # ts=0.0: the enclave /caption response never returns ts; ordering and
        # expiry are handled by frame_list_meta / expires_at=None respectively.
        perception_store.item_upsert(
            user_id, _CACHE_KIND, fid, 0.0,
            {"caption": caption}, expires_at=None,
        )
    return out


def recent_frames(user_id: str, limit: int = 10) -> dict:
    meta = db.frame_list_meta(user_id, source="screen")[-max(1, int(limit)):]
    frames = []
    for m in reversed(meta):  # newest first
        fid = _frame_id_from_entry(m)
        entry = {"frame_id": fid, "ts": m.get("ts"), "app": m.get("app")}
        cached = _cached_caption(user_id, fid)
        if cached:
            entry["caption"] = cached
        frames.append(entry)
    return {"frames": frames}
