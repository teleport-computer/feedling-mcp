"""Frame storage and shape-aware read plumbing."""

import json
import os
import time
import uuid

import httpx

import db
from core import envelope as core_envelope
from core import store as core_store
from core.frame_ids import is_supported_frame_id
from core.reqctx import request
from core.store import UserStore

PROACTIVE_WAKE_FRAME_CANDIDATE_MAX = int(os.environ.get("FEEDLING_PROACTIVE_WAKE_FRAME_CANDIDATE_MAX", "60"))


def _enclave_auth_headers(api_key: str | None, runtime_token: str | None = None) -> dict | None:
    """Auth header for an enclave call, preferring the Stage-D runtime token.

    Runtime V2 workers have no per-user api_key — their turns (model_api
    context build, proactive screen reads) are triggered by a request carrying
    X-Feedling-Runtime-Token. Pick it up from the current request context when no
    explicit token/api_key is supplied, so these server-side screen reads stop
    returning api_key_unavailable. Returns None when no credential is available
    (true non-request background callers behave exactly as before).
    """
    rt = runtime_token
    if not rt:
        rt = request.headers.get("X-Feedling-Runtime-Token", "").strip() or None
    if rt:
        return {"X-Feedling-Runtime-Token": rt}
    if api_key:
        return {"X-API-Key": api_key}
    return None


def _frame_url(store: UserStore, filename: str) -> str:
    # Absolute frame URLs come from FEEDLING_PUBLIC_BASE_URL. There is no
    # request-host fallback under ASGI (the old flask.request.host_url path had
    # no context off the event loop and always yielded ""), so an unset base
    # simply produces a root-relative URL.
    base = os.environ.get("FEEDLING_PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/v1/screen/frames/{filename}?user={store.user_id}"


def _save_frame(store: UserStore, payload: dict):
    """Save a v1 frame envelope. See docs/DESIGN_E2E.md §3.2.

    Wire format:
      {"type":"frame","ts":..., "envelope":{
          "v":1,"id":...,"body_ct":...,"nonce":...,
          "K_user":...,"K_enclave":...,
          "visibility":"shared","owner_user_id":...}}

    The JPEG + OCR are inside `body_ct` (ChaCha20-Poly1305 AEAD bound to
    owner|v|id). Server never decrypts — it stores the envelope in a
    frame_envelopes row and appends the item to frames_meta with
    `encrypted=True` so the UI + enclave path can find it.
    """
    env = payload.get("envelope")
    error = core_envelope.validate_uploaded_chat_envelope(
        env, user_id=store.user_id, content_type="image",
        max_binary_bytes=12 * 1024 * 1024,
    )
    if error is not None:
        print(f"[ingest:{store.user_id}] rejecting frame: {error.get('error')}")
        return
    _save_frame_envelope(store, payload, env)


def _save_frame_envelope(store: UserStore, payload: dict, env: dict):
    """Persist a v1 frame envelope. The ciphertext blob is big (>150KB for
    typical screen frames) so it lives in its own frame_envelopes row instead
    of being inlined into the frames_meta index blob. frames_meta gets a
    lightweight index entry with `encrypted=True`.
    """
    item_id = env.get("id") or uuid.uuid4().hex
    ts = payload.get("ts") or time.time()
    if db.frame_upsert(store.user_id, item_id, ts, env) is False:
        return

    encrypted = bool(env.get("body_ct"))
    inner = _read_plaintext_frame(env) if not encrypted else None
    meta = {
        "filename": f"{item_id}.env.json",
        "ts": ts,
        "app": (inner or {}).get("app"),
        "ocr_text": str((inner or {}).get("ocr_text") or "")[:1200],
        "w": (inner or {}).get("w") or payload.get("w", 0),
        "h": (inner or {}).get("h") or payload.get("h", 0),
        "encrypted": encrypted,
        "id": item_id,
        "v": env.get("v", 1),
        "owner_user_id": env.get("owner_user_id"),
    }

    with store.frames_lock:
        store.frames_meta.append(meta)
        if len(store.frames_meta) > core_store.MAX_FRAMES:
            removed = store.frames_meta.pop(0)
            db.frame_delete(store.user_id, removed.get("id") or removed["filename"].split(".")[0])
        store._persist_frames_meta()  # also broadcasts the "frames" wake cross-worker

    body_field = "body_ct" if encrypted else "body_b64"
    body_len = len(env.get(body_field) or "")
    print(f"[ingest:{store.user_id}] saved frame id={item_id} shape={body_field} body_len={body_len}")


def _read_plaintext_frame(env: dict) -> dict | None:
    """Decode a plaintext-binary frame wire body without involving the enclave."""
    if not isinstance(env, dict) or env.get("body_b64") is None:
        return None
    try:
        inner = json.loads(core_envelope.read_envelope_body(
            env, None, purpose="screen_frame_plaintext_read").decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return inner if isinstance(inner, dict) else None


def _recent_frame_meta(store: UserStore, now: float, window_sec: float) -> list[dict]:
    with store.frames_lock:
        frames = [f for f in store.frames_meta if now - float(f.get("ts", 0) or 0) <= window_sec]
    return frames[-PROACTIVE_WAKE_FRAME_CANDIDATE_MAX:]


def _sample_frames_for_wake(frames: list[dict], max_frames: int = 5) -> list[dict]:
    """Metadata sampler for proactive wake frames."""
    clean = [f for f in frames if isinstance(f, dict) and str(f.get("id") or "").strip()]
    clean.sort(key=lambda f: float(f.get("ts", 0) or 0))
    if len(clean) <= max_frames:
        return clean
    if max_frames <= 2:
        return [clean[-1]]
    picks = [clean[0], clean[-1]]
    middle = clean[1:-1]
    slots = max_frames - len(picks)
    if middle and slots > 0:
        if slots == 1:
            picks.append(middle[len(middle) // 2])
        else:
            for i in range(slots):
                idx = round(i * (len(middle) - 1) / max(1, slots - 1))
                picks.append(middle[idx])
    by_id: dict[str, dict] = {}
    for frame in picks:
        by_id[str(frame.get("id"))] = frame
    return sorted(by_id.values(), key=lambda f: float(f.get("ts", 0) or 0))


def _frame_ids(frames: list[dict]) -> list[str]:
    out: list[str] = []
    for frame in frames:
        frame_id = str(frame.get("id") or frame.get("frame_id") or "").strip()
        if frame_id:
            out.append(frame_id)
    return out


def _decrypt_frame_metadata_for_gate(
    store: UserStore,
    frame_id: str,
    api_key: str | None,
    include_image: bool = False,
) -> dict:
    """Best-effort decrypt of a frame for request-scoped routes.

    The Flask backend does not store raw API keys, so only request-scoped
    callers (iOS / resident consumer / manual curl) can run this path.
    """
    fid = str(frame_id or "").strip()
    if not fid:
        return {"frame_id": "", "error": "missing_frame_id"}
    if not _frame_exists(store, fid):
        return {"frame_id": fid, "error": "frame_not_found"}
    env = db.frame_get(store.user_id, fid)
    inner = _read_plaintext_frame(env or {})
    if inner is not None:
        result = {
            "frame_id": fid, "id": fid, "ts": inner.get("ts"),
            "app": inner.get("app") or "unknown",
            "ocr_text": str(inner.get("ocr_text") or "")[:1200],
            "w": inner.get("w"), "h": inner.get("h"),
            "image_mime": inner.get("image_mime") or "image/jpeg",
        }
        if include_image and inner.get("image"):
            result["image_b64"] = str(inner.get("image") or "")
        return result
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return {"frame_id": fid, "error": "enclave_unavailable"}
    auth = _enclave_auth_headers(api_key)
    if auth is None:
        return {"frame_id": fid, "error": "api_key_unavailable"}
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.get(
                f"{enclave_url}/v1/screen/frames/{fid}/decrypt",
                headers=auth,
                params={"include_image": "true" if include_image else "false"},
            )
        if resp.status_code >= 400:
            return {
                "frame_id": fid,
                "error": f"decrypt_http_{resp.status_code}",
                "body": resp.text[:240],
            }
        data = resp.json()
        if not isinstance(data, dict):
            return {"frame_id": fid, "error": "decrypt_non_object"}
        result = {
            "frame_id": fid,
            "id": fid,
            "ts": data.get("ts"),
            "app": data.get("app") or "unknown",
            "ocr_text": str(data.get("ocr_text") or "")[:1200],
            "w": data.get("w"),
            "h": data.get("h"),
            "image_mime": data.get("image_mime") or "image/jpeg",
        }
        if include_image and data.get("image_b64"):
            result["image_b64"] = str(data.get("image_b64") or "")
        return result
    except Exception as e:
        return {"frame_id": fid, "error": f"decrypt_error:{type(e).__name__}:{str(e)[:160]}"}


def _current_app_from_frames(frame_contexts: list[dict], fallback_frames: list[dict]) -> str:
    for frame in reversed(frame_contexts):
        app_name = str(frame.get("app") or "").strip()
        if app_name and app_name != "unknown":
            return app_name[:120]
    for frame in reversed(fallback_frames):
        app_name = str(frame.get("app") or "").strip()
        if app_name:
            return app_name[:120]
    return "unknown"


def _ocr_summary(frames: list[dict]) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for frame in reversed(frames):
        text = (frame.get("ocr_text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text[:240])
        if len(parts) >= 3:
            break
    return " | ".join(reversed(parts))[:700]


# --- Frame decrypt plumbing -------------------------------------------------
# Frames are v1 envelopes just like chat/memory/identity: the broadcast
# extension runs VNRecognizeText on-device, stuffs `image` + `ocr_text`
# into the same JSON payload, and ChaCha20-seals the whole thing. The
# server sees only `body_ct` — that's why frames_meta.ocr_text is always
# "" and screen.analyze.ocr_summary is empty.
#
# Two endpoints open the decrypt path to agents + API clients:
#   GET /v1/screen/frames/<id>/envelope — opaque envelope JSON.
#                                         Used by the enclave to pull
#                                         the ciphertext back for
#                                         in-enclave decryption.
#   GET /v1/screen/frames/<id>/decrypt  — proxies to the enclave's
#                                         /v1/screen/frames/<id>/decrypt
#                                         and returns the plaintext:
#                                         image_b64, ocr_text, app, w, h.
#                                         This is the API-path parity
#                                         clients ask for when they want
#                                         everything curl-reachable too.


def _load_envelope(store, frame_id: str) -> dict | None:
    """Load a frame's stored v1 envelope doc by id, or None if absent/invalid."""
    if not is_supported_frame_id(frame_id):
        return None
    env = db.frame_get(store.user_id, frame_id)
    return env if isinstance(env, dict) else None


def _frame_exists(store, frame_id: str) -> bool:
    """Existence guard for the proxy endpoints (no heavy body_ct fetch)."""
    if not is_supported_frame_id(frame_id):
        return False
    return db.frame_exists(store.user_id, frame_id)
