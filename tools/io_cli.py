#!/usr/bin/env python3
"""io_cli — thin Feedling tool client for resident (VPS) agents.

A resident autonomous agent (OpenClaw / Hermes / Claude Code) registers this as
a NATIVE tool so it can pull Feedling perception during chat (true agentic pull),
instead of the prompt-"emit tool_calls JSON" hack that does not work with
autonomous agents. See docs/PERCEPTION_CLI_DESIGN.md.

Design notes:
  - Stdlib only (urllib) — runs in any agent venv, no httpx/requests/psycopg.
  - Output is JSON on stdout (the agent parses it). Errors are JSON too.
  - Two-head routing:
      perception.*   -> main backend (FEEDLING_API_URL)   [coarse, no decrypt]
      photo/memory   -> enclave (FEEDLING_ENCLAVE_URL)     [decrypt; phase 2]
  - Auth: X-API-Key = FEEDLING_API_KEY, or (zero-roster host-all) the Stage-D
    runtime token from FEEDLING_RUNTIME_TOKEN_FILE as X-Feedling-Runtime-Token.
    Both backend and enclave accept either.

Config via env (same as the resident consumer): FEEDLING_API_URL,
FEEDLING_API_KEY (or FEEDLING_RUNTIME_TOKEN_FILE), FEEDLING_ENCLAVE_URL.

MVP = `perception`. send / wait-for-wake / schedule-wake / photo are phase 2 and
currently return a clean "not implemented" JSON so the agent degrades gracefully.
"""
import argparse
import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
try:
    from identity import card_policy as _card_policy  # single source, pure stdlib
except Exception:
    _card_policy = None

FAST_SIGNALS = ("now", "location", "weather", "motion", "calendar")
SLOW_SIGNALS = (
    "steps", "sleep", "workout", "vitals",
    "activity", "body", "metabolic", "cycle", "mood", "reminders",
)
# pull-only context signals (focus = are-you-in-a-focus-mode, audio_route =
# headphones/car). Valid + pullable, but kept out of the default fast set.
EXTRA_SIGNALS = ("focus", "audio_route", "app")
PERCEPTION_SIGNALS = FAST_SIGNALS + SLOW_SIGNALS + EXTRA_SIGNALS

# Native model handles these as agent OUTPUT actions, not pull tools — kept as
# graceful no-op stubs so an agent that tries to call them degrades cleanly.
PHASE2_VERBS = ("send", "wait-for-wake")


def _emit(obj, code=0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def _materialize_decrypted_image(prefix, body):
    """Turn a decrypt response's inline base64 image into a FILE the agent can Read.

    A vision CLI agent (claude via its Read tool; codex via native file open) sees
    an image only from a local file — an ``image_b64`` blob printed on stdout is
    just useless (undecodable) text and bloats the tool output. So when a
    ``*/decrypt?include_image=true`` body carries pixels, write them into
    ``IMAGE_TEMP_DIR`` (the same dir the consumer decrypts chat images to, so the
    claude command's ``--add-dir`` / ``Read(//…/images/**)`` grant already covers
    it) and return a copy of ``body`` with ``image_b64`` swapped for an
    ``image_file`` path + a Read hint. Non-dict / no image / write failure → return
    the body unchanged so the tool still degrades to caption/OCR gracefully."""
    if not isinstance(body, dict):
        return body
    b64 = body.get("image_b64")
    if not isinstance(b64, str) or not b64.strip():
        return body
    raw_b64 = b64.split(",", 1)[1] if b64.startswith("data:") else b64
    mime = str(body.get("image_mime") or "image/jpeg")
    ext = ".png" if "png" in mime.lower() else ".jpg"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(prefix))[:96] or "image"
    image_dir = os.environ.get("IMAGE_TEMP_DIR", "/tmp/feedling_chat_images")
    out = dict(body)
    try:
        os.makedirs(image_dir, exist_ok=True)
        path = os.path.join(image_dir, f"{safe}{ext}")
        with open(path, "wb") as f:
            f.write(base64.b64decode(raw_b64))
    except Exception as e:  # pragma: no cover - defensive
        out["image_error"] = f"could not save decrypted image: {e}"
        return out
    out.pop("image_b64", None)
    out["image_file"] = path
    out["image_hint"] = "Use the Read tool on image_file to view the pixels."
    return out


def _env(name):
    return os.environ.get(name, "").strip()


def _trace_id():
    return _env("FEEDLING_TRACE_ID") or _env("FEEDLING_DEBUG_TRACE_ID")


def _auth_headers():
    """Auth header for backend/enclave calls. Prefer ``FEEDLING_API_KEY``; in
    zero-roster host-all mode it is absent, so fall back to the Stage-D runtime
    token written to ``FEEDLING_RUNTIME_TOKEN_FILE`` (both backend and enclave
    accept ``X-Feedling-Runtime-Token``). Empty dict when neither is available."""
    api_key = _env("FEEDLING_API_KEY")
    if api_key:
        return {"X-API-Key": api_key}
    token_file = _env("FEEDLING_RUNTIME_TOKEN_FILE")
    if token_file:
        try:
            tok = open(token_file).read().strip()
        except Exception:
            tok = ""
        if tok:
            return {"X-Feedling-Runtime-Token": tok}
    return {}


def _http_json(method, url, auth, *, payload=None, insecure=False, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {**auth, "Accept": "application/json"}
    trace_id = _trace_id()
    if trace_id:
        headers["X-Feedling-Trace-Id"] = trace_id
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    # insecure: the enclave presents a dstack-gateway TEE cert the local httpx
    # client does not verify today (consumer uses verify=False); mirror that for
    # enclave calls only. Backend calls use normal TLS verification.
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:
            detail = {"error": "http_error"}
        return e.code, detail
    except Exception as e:  # noqa: BLE001 — return a JSON error, never crash the agent
        return -1, {"error": f"{type(e).__name__}: {e}"}


_REDACTED_ARG_KEYS = {"query", "self_introduction", "signature", "reason"}


def _clip_arg(s, limit=80):
    s = str(s or "")
    return s if len(s) <= limit else s[:limit] + "...(truncated)"


def _summarize_arg_value(key, value):
    if callable(value):
        return None
    if value is None:
        return None
    if isinstance(value, bool):
        return value if value else None
    if isinstance(value, (int, float)):
        return value
    if key in _REDACTED_ARG_KEYS:
        if isinstance(value, (list, tuple)):
            chars = sum(len(str(v)) for v in value)
            return f"<redacted items={len(value)} chars={chars}>"
        text = str(value)
        return f"<redacted chars={len(text)}>" if text else None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        sample = ", ".join(_clip_arg(v, 24) for v in list(value)[:3])
        suffix = ", ..." if len(value) > 3 else ""
        return f"{len(value)} item(s): {sample}{suffix}"
    text = str(value)
    return _clip_arg(text) if text else None


def _redacted_tool_args(args):
    out = {}
    for key, value in vars(args).items():
        if key in {"func", "verb"}:
            continue
        summary = _summarize_arg_value(key, value)
        if summary is not None:
            out[key] = summary
    return out


def _emit_tool_trace(args, exit_code, dur_ms):
    """Best-effort per-tool trace. Never let observability affect tool output."""
    try:
        trace_id = _trace_id()
        api_url = _env("FEEDLING_API_URL")
        auth = _auth_headers()
        if not trace_id or not api_url or not auth:
            return
        tool = str(getattr(args, "verb", "") or "")
        result_status = "ok" if int(exit_code or 0) == 0 else "err"
        rounded_ms = round(float(dur_ms), 1)
        detail = {
            "tool": tool,
            "args": _redacted_tool_args(args),
            "result_status": result_status,
            "dur_ms": rounded_ms,
        }
        _http_json(
            "POST",
            f"{api_url.rstrip('/')}/v1/debug/trace/event",
            auth,
            payload={"event": {
                "subsystem": "agent",
                "type": "agent.tool.call",
                "status": "ok" if result_status == "ok" else "error",
                "summary": f"io_cli {tool} {result_status}",
                "explain": f"io_cli tool {tool} finished {result_status} in {int(rounded_ms)}ms",
                "detail": detail,
                "trace_id": trace_id,
                "turn_id": trace_id,
                "actor": "vps_resident",
                "dur_ms": rounded_ms,
            }},
            timeout=1.0,
        )
    except Exception:
        pass


def cmd_perception(args):
    api_url = _env("FEEDLING_API_URL")
    auth = _auth_headers()
    if not api_url or not auth:
        _emit({"ok": False, "error": "missing FEEDLING_API_URL / auth (FEEDLING_API_KEY or runtime token) in env"}, 2)
    signals = list(args.signals) or list(FAST_SIGNALS)
    unknown = [s for s in signals if s not in PERCEPTION_SIGNALS]
    if unknown:
        _emit({"ok": False, "error": f"unknown signals: {unknown}",
               "available": list(PERCEPTION_SIGNALS)}, 2)
    qs = urllib.parse.urlencode({"signals": ",".join(signals)})
    url = f"{api_url.rstrip('/')}/v1/agent/perception?{qs}"
    status, body = _http_json("GET", url, auth)
    if status == 200:
        _emit({"ok": True, **body})
    # Surface the backend's shape verbatim so the agent (and we, during
    # acceptance) can see disabled/switch_off/not_permitted reasons + 404 before
    # the backend verb ships.
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def cmd_perception_recent_apps(args):
    api_url = _env("FEEDLING_API_URL")
    auth = _auth_headers()
    if not api_url or not auth:
        _emit({"ok": False, "error": "missing FEEDLING_API_URL / auth (FEEDLING_API_KEY or runtime token) in env"}, 2)
    params = {"limit": str(args.limit)}
    if args.hours:
        params["hours"] = str(args.hours)
    url = f"{api_url.rstrip('/')}/v1/agent/perception/recent_apps?{urllib.parse.urlencode(params)}"
    status, body = _http_json("GET", url, auth)
    if status == 200:
        _emit(body)
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def cmd_perception_trend(args):
    api_url = _env("FEEDLING_API_URL")
    auth = _auth_headers()
    if not api_url or not auth:
        _emit({"ok": False, "error": "missing FEEDLING_API_URL / auth (FEEDLING_API_KEY or runtime token) in env"}, 2)
    params = {"signal": args.signal, "days": str(args.days)}
    if args.field:
        params["field"] = args.field
    url = f"{api_url.rstrip('/')}/v1/agent/perception/trend?{urllib.parse.urlencode(params)}"
    status, body = _http_json("GET", url, auth)
    if status == 200:
        _emit(body)
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def cmd_perception_history(args):
    api_url = _env("FEEDLING_API_URL")
    auth = _auth_headers()
    if not api_url or not auth:
        _emit({"ok": False, "error": "missing FEEDLING_API_URL / auth (FEEDLING_API_KEY or runtime token) in env"}, 2)
    params = {"signal": args.signal, "days": str(args.days)}
    url = f"{api_url.rstrip('/')}/v1/agent/perception/history?{urllib.parse.urlencode(params)}"
    status, body = _http_json("GET", url, auth)
    if status == 200:
        _emit(body)
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def _require_backend():
    """Resolve (api_url, auth_headers). auth uses _auth_headers() so memory/screen
    work in both api-key and host-all runtime-token modes (mirrors perception)."""
    api_url = _env("FEEDLING_API_URL")
    auth = _auth_headers()
    if not api_url or not auth:
        _emit({"ok": False, "error": "missing FEEDLING_API_URL / auth (FEEDLING_API_KEY or runtime token) in env"}, 2)
    return api_url.rstrip("/"), auth


def cmd_memory_index(args):
    """Compact memory index (plaintext-safe readside). POST /v1/memory/index."""
    api_url, auth = _require_backend()
    payload = {"limit": args.limit}
    if args.bucket:
        payload["bucket"] = args.bucket
    if args.thread:
        payload["thread"] = args.thread
    if args.query:
        payload["query"] = args.query
    if args.ambient:
        payload["ambient"] = True
    if args.include_sensitive:
        payload["include_sensitive"] = True
    status, body = _http_json("POST", f"{api_url}/v1/memory/index", auth, payload=payload)
    if status == 200:
        _emit({"ok": True, **body})
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def cmd_schedule_wake(args):
    """Ask to be woken at a later time (native self-wake). POST /v1/proactive/scheduled/actions."""
    api_url, auth = _require_backend()
    at = (args.at or "").strip()
    if not at:
        _emit({"ok": False, "error": "schedule-wake needs --at <time> (ISO like 2026-06-29T18:00, or a relative spec)"}, 2)
    action = {"type": "schedule_wake", "at": at}
    if args.tz:
        action["tz"] = args.tz
    if args.reason:
        action["reason"] = args.reason
    status, body = _http_json("POST", f"{api_url}/v1/proactive/scheduled/actions", auth, payload={"actions": [action]})
    if status == 200:
        _emit({"ok": True, **body})
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def cmd_cancel_wake(args):
    """Cancel a previously scheduled self-wake. POST /v1/proactive/scheduled/actions."""
    api_url, auth = _require_backend()
    wid = (args.wake_id or "").strip()
    if not wid:
        _emit({"ok": False, "error": "cancel-wake needs --wake-id <id>"}, 2)
    action = {"type": "cancel_wake", "wake_id": wid}
    if args.reason:
        action["reason"] = args.reason
    status, body = _http_json("POST", f"{api_url}/v1/proactive/scheduled/actions", auth, payload={"actions": [action]})
    if status == 200:
        _emit({"ok": True, **body})
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def cmd_memory_fetch(args):
    """Verbatim decrypted memory cards by id (plaintext-safe). POST /v1/memory/fetch."""
    api_url, auth = _require_backend()
    ids = list(args.ids)
    if not ids:
        _emit({"ok": False, "error": "memory-fetch needs at least one id"}, 2)
    payload = {"ids": ids, "limit": args.limit}
    if args.include_archived:
        payload["include_archived"] = True
    if args.include_superseded:
        payload["include_superseded"] = True
    status, body = _http_json("POST", f"{api_url}/v1/memory/fetch", auth, payload=payload)
    if status == 200:
        _emit({"ok": True, **body})
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def cmd_screen_recent(args):
    """Recent screen frame metadata (no pixels). GET /v1/screen/frames."""
    api_url, auth = _require_backend()
    qs = urllib.parse.urlencode({"limit": args.limit})
    status, body = _http_json("GET", f"{api_url}/v1/screen/frames?{qs}", auth)
    if status == 200:
        _emit({"ok": True, **body})
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def cmd_screen_read(args):
    """Decrypted screen frame (caption/ocr; pixels gated off by default).

    GET /v1/screen/frames/<id>/decrypt (backend proxies to the enclave). When no
    --frame-id is given, resolve the latest frame first.
    """
    api_url, auth = _require_backend()
    frame_id = args.frame_id
    if not frame_id:
        status, body = _http_json("GET", f"{api_url}/v1/screen/frames/latest", auth)
        if status != 200:
            _emit({"ok": False, "http_status": status, "error": body}, 1)
        frame_id = body.get("frame_id") or body.get("id") or (body.get("filename") or "").split(".")[0]
        if not frame_id:
            _emit({"ok": False, "error": "could not resolve latest frame_id", "latest": body}, 1)
    include_image = "true" if args.include_image else "false"
    qs = urllib.parse.urlencode({"include_image": include_image})
    status, body = _http_json("GET", f"{api_url}/v1/screen/frames/{frame_id}/decrypt?{qs}", auth)
    if status == 200:
        if isinstance(body, dict):
            # Save pixels to a file the agent can Read instead of dumping base64
            # text it can't see (see _materialize_decrypted_image).
            body = _materialize_decrypted_image(f"screen_{frame_id}", body)
        _emit({"ok": True, "frame_id": frame_id, **(body if isinstance(body, dict) else {"data": body})})
    _emit({"ok": False, "http_status": status, "frame_id": frame_id, "error": body}, 1)


def cmd_photo_recent(args):
    """Recent photo metadata (scene/time; no raw pixels). GET /v1/perception/photos.

    Plaintext-safe readside, parallel to screen-recent. Raw image content
    (/photo/<id>/content) is intentionally not exposed here — the agent uses
    scene/metadata, not bytes."""
    api_url, auth = _require_backend()
    qs = urllib.parse.urlencode({"limit": args.limit})
    status, body = _http_json("GET", f"{api_url}/v1/perception/photos?{qs}", auth)
    if status == 200:
        _emit({"ok": True, **body})
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def cmd_photo_read(args):
    """One specific photo's details by id (metadata + optional decrypted image).

    GET /v1/perception/photo/<id>/content returns metadata + frame_id; with
    --include-image, the pixels are decrypted via the enclave's
    /v1/screen/frames/<frame_id>/decrypt path (same as screen-read). Pass an id
    from photo-recent. Lets the agent actually look at a photo it cares about,
    not just the recent-list metadata."""
    api_url, auth = _require_backend()
    pid = (args.photo_id or "").strip()
    if not pid:
        _emit({"ok": False, "error": "photo-read needs --id <photo_id> (from photo-recent)"}, 2)
    status, body = _http_json("GET", f"{api_url}/v1/perception/photo/{pid}/content", auth)
    if status != 200:
        _emit({"ok": False, "http_status": status, "photo_id": pid, "error": body}, 1)
    out = {"ok": True, "photo_id": pid, **(body if isinstance(body, dict) else {"data": body})}
    if args.include_image:
        frame_id = (body.get("frame_id") if isinstance(body, dict) else "") or ""
        if frame_id:
            qs = urllib.parse.urlencode({"include_image": "true"})
            istatus, ibody = _http_json("GET", f"{api_url}/v1/screen/frames/{frame_id}/decrypt?{qs}", auth)
            if istatus == 200:
                # Save pixels to a Read-able file rather than emitting base64 the
                # vision model can't decode (see _materialize_decrypted_image).
                out["image"] = (
                    _materialize_decrypted_image(f"photo_{pid}", ibody)
                    if isinstance(ibody, dict) else ibody
                )
            else:
                out["image"] = {"error": ibody, "http_status": istatus}
        else:
            out["image"] = {"error": "no frame_id on photo content"}
    _emit(out)


def cmd_chat_image(args):
    """Pull ONE past chat message's decrypted image by id, saved as a Read-able file.

    Chat-history images are NOT reachable via ``photo-read`` (that command hits the
    perception photo library, not the chat feed). The recent-chat transcript that
    gets injected into a turn shows historical image messages only as an
    ``[image] … io_cli chat-image --id <id>`` placeholder — the pixels are never in
    the transcript. This command lazily fetches the pixels of a specific past chat
    image WHEN the agent actually needs them, instead of eagerly decrypting every
    history image on every turn.

    Decrypt source is the enclave's ``GET /v1/chat/history`` (same source the
    resident consumer uses). It presents a dstack-gateway TEE cert the stdlib
    client does not verify, so the call is made insecure=True (mirrors the
    consumer's verify=False)."""
    enclave_url = _env("FEEDLING_ENCLAVE_URL")
    auth = _auth_headers()
    mid = (args.message_id or "").strip()
    if not enclave_url or not auth:
        _emit({"ok": False, "error": "missing FEEDLING_ENCLAVE_URL / auth (FEEDLING_API_KEY or runtime token) in env"}, 2)
    if not mid:
        _emit({"ok": False, "error": "chat-image needs --id <message_id> (from the [image] placeholder in the recent-chat transcript)"}, 2)
    qs = urllib.parse.urlencode({"since": 0, "limit": args.limit})
    status, body = _http_json("GET", f"{enclave_url}/v1/chat/history?{qs}", auth, insecure=True)
    if status != 200:
        _emit({"ok": False, "http_status": status, "message_id": mid, "error": body}, 1)
    messages = (body.get("messages") or body.get("history") or []) if isinstance(body, dict) else []
    msg = next((m for m in messages if isinstance(m, dict) and str(m.get("id") or "") == mid), None)
    if not msg:
        _emit({
            "ok": False,
            "message_id": mid,
            "error": "message not found in recent history",
            "hint": f"only the {args.limit} most recent messages are searched; raise --limit if the image is older",
        }, 1)
    if not msg.get("image_b64"):
        _emit({
            "ok": True,
            "message_id": mid,
            "role": msg.get("role"),
            "content": msg.get("content"),
            "note": "this message has no image (text-only turn)",
        })
    # Save pixels to a Read-able file rather than emitting base64 the vision model
    # can't decode (see _materialize_decrypted_image).
    out = _materialize_decrypted_image(f"chat_{mid}", msg)
    _emit({"ok": True, "message_id": mid, **(out if isinstance(out, dict) else {"data": out})})


def cmd_identity_read(args):
    """Read the CURRENT identity card (decrypted) so a rewrite builds ON it, not over it.

    Call this BEFORE writing/re-deriving identity from material a user hands you:
    keep the fields the new material doesn't address (部分补全), only change what it
    does. Decrypt source is the enclave's ``GET /v1/identity/get`` (TEE cert the
    stdlib client doesn't verify → insecure=True, mirrors chat-image), falling back
    to the backend when no enclave is configured."""
    auth = _auth_headers()
    if not auth:
        _emit({"ok": False, "error": "missing auth (FEEDLING_API_KEY or runtime token) in env"}, 2)
    enclave_url = _env("FEEDLING_ENCLAVE_URL")
    status, body = -1, {}
    if enclave_url:
        status, body = _http_json("GET", f"{enclave_url.rstrip('/')}/v1/identity/get", auth, insecure=True)
    if status != 200 or not (isinstance(body, dict) and isinstance(body.get("identity"), dict)):
        api_url = _env("FEEDLING_API_URL")
        if api_url:
            status, body = _http_json("GET", f"{api_url.rstrip('/')}/v1/identity/get", auth)
    if status == 200 and isinstance(body, dict):
        _emit({"ok": True, **body})
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def _identity_write_payload(self_introduction, signature):
    """Build the /v1/identity/actions body for a profile_patch. Pure (testable).

    Returns None when there's nothing to write. signature is a list of short strings.
    """
    patch = {}
    if self_introduction is not None:
        patch["self_introduction"] = self_introduction
    if signature:
        patch["signature"] = list(signature)
    if not patch:
        return None
    return {"action": {"type": "identity.profile_patch", "patch": patch}}


def cmd_identity_write(args):
    """Patch the agent's display identity card (self_introduction / signature).

    POST /v1/identity/actions (identity.profile_patch). The server decrypts the
    existing card, merges, and re-encrypts (no client crypto). Used by post-respawn
    7.D so the agent (now itself) writes its own intro + signature in-voice.
    """
    api_url, auth = _require_backend()
    payload = _identity_write_payload(args.self_introduction, args.signature)
    if payload is None:
        _emit({"ok": False, "error": "nothing_to_write: need --self-introduction and/or --signature"}, 2)
    status, body = _http_json("POST", f"{api_url}/v1/identity/actions", auth, payload=payload)
    if status == 200:
        _emit({"ok": True, **body})
    _emit({"ok": False, "http_status": status, "error": body}, 1)


_FRESH_START_EVIDENCE = "user-confirmed fresh start"


def _identity_init_payload(*, agent_name, self_introduction, dimensions,
                           days_with_user, anchor, fresh_start):
    """Build the /v1/identity/init body. Sanitize the card (clamp/dedup/truncate)
    so structure is valid; fresh_start fills days=0 + standard anchor evidence."""
    card = {
        "agent_name": str(agent_name or ""),
        "self_introduction": str(self_introduction or ""),
        "dimensions": dimensions if isinstance(dimensions, list) else [],
    }
    if _card_policy is not None:
        card = _card_policy.sanitize_identity_card(card)
    if fresh_start:
        days = 0
        anchor = _FRESH_START_EVIDENCE
    else:
        days = int(days_with_user) if days_with_user is not None else None
    return {"identity": card, "days_with_user": days,
            "relationship_anchor_evidence": anchor or ""}


def cmd_identity_init(args):
    """Create the agent's identity card (POST /v1/identity/init).

    Local pre-check only catches the STRONG checks sanitize can't fix (runtime-label
    name, missing days/anchor) — everything else (out-of-range values, dupes,
    unnamed dims) is auto-corrected by sanitize before this even runs. Contract:
    走 io_cli 尽量不失败、多拿内容."""
    api_url, auth = _require_backend()
    dims = json.loads(args.dimensions) if args.dimensions else []
    body = _identity_init_payload(
        agent_name=args.agent_name, self_introduction=args.self_introduction,
        dimensions=dims, days_with_user=args.days_with_user,
        anchor=args.relationship_anchor_evidence, fresh_start=args.fresh_start)
    # 强校验本地预检:只在 sanitize 修不了的 4 条上提示(runtime 名字 / days 缺锚点)
    if _card_policy is not None:
        ok, err = _card_policy.validate_full_identity_card(body["identity"])
        if not ok:
            _emit({"ok": False, "error": err,
                   "hint": "非空名字不能是 runtime 标签(Claude 等);其余结构已自动修正"}, 2)
    if body["days_with_user"] is None:
        _emit({"ok": False, "error": "days_with_user_required",
               "hint": "给 --days-with-user + --relationship-anchor-evidence,或用 --fresh-start"}, 2)
    if not args.fresh_start and len(body["relationship_anchor_evidence"]) < 8:
        _emit({"ok": False, "error": "relationship_anchor_evidence_required",
               "hint": "给 --relationship-anchor-evidence(≥8字符)或用 --fresh-start"}, 2)
    status, resp = _http_json("POST", f"{api_url}/v1/identity/init", auth, payload=body)
    if status in (200, 201):
        _emit({"ok": True, **(resp if isinstance(resp, dict) else {"result": resp})})
    _emit({"ok": False, "http_status": status, "error": resp}, 1)


def _memory_write_payload(*, summary, content, bucket, threads, importance, pulse, mem_type, source):
    """Build the /v1/memory/actions body for a single plaintext memory.add. Pure (testable).

    Plaintext action — the SERVER builds & encrypts the envelope (same path running capture
    uses), so no client crypto. Returns None when there's nothing to write. This is the
    on-demand counterpart to the consumer's automatic capture: the agent, having locally
    distilled a fact from a handed-in file, pushes ONE finished card."""
    summary = str(summary or "").strip()
    content = str(content or "").strip()
    if not summary and not content:
        return None
    memory = {
        "type": (mem_type or "fact").strip().lower(),
        "summary": summary or content[:180],
        "title": summary or content[:180],
        "content": content or summary,
        "description": content or summary,
        "source": (source or "resident_absorb").strip()[:80],
    }
    if bucket:
        memory["bucket"] = str(bucket).strip()
    if threads:
        memory["threads"] = [str(t).strip() for t in threads if str(t or "").strip()]
    if importance is not None:
        memory["importance"] = float(importance)
    if pulse is not None:
        memory["pulse"] = float(pulse)
    return {"actions": [{
        "type": "memory.add",
        "memory": memory,
        "reason": "Absorbed from a file/text the user handed me.",
    }]}


def cmd_memory_write(args):
    """Write ONE memory card the agent already distilled locally (handed-in file → fact).

    POST /v1/memory/actions (memory.add, plaintext — the server encrypts). This is the
    on-demand write path for the resident agent; the consumer's running capture is the
    automatic one. Both hit the same endpoint."""
    api_url, auth = _require_backend()
    payload = _memory_write_payload(
        summary=args.summary, content=args.content, bucket=args.bucket, threads=args.threads,
        importance=args.importance, pulse=args.pulse, mem_type=args.type, source=args.source,
    )
    if payload is None:
        _emit({"ok": False, "error": "nothing_to_write: need --summary and/or --content"}, 2)
    status, body = _http_json("POST", f"{api_url}/v1/memory/actions", auth, payload=payload)
    if status in (200, 201):
        _emit({"ok": True, **(body if isinstance(body, dict) else {"result": body})})
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def _memory_patch_payload(*, memory_id, summary, content, bucket, threads, importance, pulse, mem_type, source, reason):
    """Build the /v1/memory/actions body for a single plaintext memory.supersede. Pure (testable).

    「Patch」an existing card by superseding it with a NEW plaintext card — the SERVER
    builds & encrypts the envelope (same path memory.add uses) and inherits bucket/threads/
    importance/pulse from the old card when omitted here. Returns None when there's no new
    content to write (nothing to patch)."""
    memory_id = str(memory_id or "").strip()
    if not memory_id:
        return None
    summary = str(summary or "").strip()
    content = str(content or "").strip()
    if not summary and not content:
        return None
    memory = {
        "type": (mem_type or "fact").strip().lower(),
        "summary": summary or content[:180],
        "title": summary or content[:180],
        "content": content or summary,
        "description": content or summary,
        "source": (source or "resident_patch").strip()[:80],
    }
    if bucket:
        memory["bucket"] = str(bucket).strip()
    if threads:
        memory["threads"] = [str(t).strip() for t in threads if str(t or "").strip()]
    if importance is not None:
        memory["importance"] = float(importance)
    if pulse is not None:
        memory["pulse"] = float(pulse)
    return {"actions": [{
        "type": "memory.supersede",
        "supersedes": memory_id,
        "memory": memory,
        "reason": (str(reason or "").strip() or "Memory corrected/updated from chat."),
    }]}


def cmd_memory_delete(args):
    """Delete ONE memory card by id (hard delete — same as the user tapping delete in Garden).

    POST /v1/memory/actions (memory.delete). The card is removed from the user's garden."""
    api_url, auth = _require_backend()
    memory_id = str(args.id or "").strip()
    if not memory_id:
        _emit({"ok": False, "error": "memory-delete needs --id <memory_id>"}, 2)
    action = {"type": "memory.delete", "id": memory_id}
    if args.reason:
        action["reason"] = str(args.reason).strip()[:500]
    status, body = _http_json("POST", f"{api_url}/v1/memory/actions", auth, payload={"actions": [action]})
    if status in (200, 201):
        _emit({"ok": True, **(body if isinstance(body, dict) else {"result": body})})
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def cmd_memory_patch(args):
    """Modify ONE existing memory card by superseding it with corrected content.

    POST /v1/memory/actions (memory.supersede, plaintext — the server encrypts). The old card
    is retired and a new card takes its place; bucket/threads/importance/pulse inherit from the
    old card unless overridden here. This is the on-demand 'correct a card in chat' path."""
    api_url, auth = _require_backend()
    payload = _memory_patch_payload(
        memory_id=args.id, summary=args.summary, content=args.content, bucket=args.bucket,
        threads=args.threads, importance=args.importance, pulse=args.pulse,
        mem_type=args.type, source=args.source, reason=args.reason,
    )
    if payload is None:
        _emit({"ok": False, "error": "nothing_to_patch: need --id and at least one of --summary/--content"}, 2)
    status, body = _http_json("POST", f"{api_url}/v1/memory/actions", auth, payload=payload)
    if status in (200, 201):
        _emit({"ok": True, **(body if isinstance(body, dict) else {"result": body})})
    _emit({"ok": False, "http_status": status, "error": body}, 1)
def cmd_onboarding_validate(args):
    """Server-computed onboarding acceptance snapshot. GET /v1/onboarding/validate.

    Always 200 on the backend (an artifact-based readout, never a hard error), so
    ``ok`` here just tracks the HTTP round-trip; the real signal is the body's
    ``next_action`` (and whatever other fields the payload carries), surfaced
    verbatim so the caller can decide what onboarding step is still pending."""
    api_url, auth = _require_backend()
    status, body = _http_json("GET", f"{api_url}/v1/onboarding/validate", auth)
    _emit({"ok": status == 200, "http_status": status,
           **(body if isinstance(body, dict) else {})}, 0 if status == 200 else 1)


def cmd_chat_verify_loop(args):
    """Liveness probe for the resident-consumer reply pipeline. POST /v1/chat/verify_loop.

    The backend posts a hidden synthetic ping and blocks waiting for an
    agent-role reply (client request timeout hardcoded to 40s below); the
    response's ``passing`` bool is the real signal (``loop_alive`` mirrors it).
    Both ping and any matching reply are scrubbed from the visible transcript
    regardless of outcome, so this never pollutes IO Chat."""
    api_url, auth = _require_backend()
    status, body = _http_json("POST", f"{api_url}/v1/chat/verify_loop", auth,
                               payload={}, timeout=40)
    _emit({"ok": bool(isinstance(body, dict) and body.get("passing")), "http_status": status,
           **(body if isinstance(body, dict) else {})}, 0 if status == 200 else 1)


def _next_onboarding_step(status):
    """Pure: derive the current onboarding step + the next io_cli command from a
    ``/v1/bootstrap/status`` snapshot. identity -> live_loop -> greet -> complete.

    The greet step's ``chat-greet`` io_cli verb does not exist (posting a chat
    message needs client-side crypto, so it goes through the resident consumer,
    not io_cli) — ``next_cmd`` for that step is a plain instruction, not a
    runnable io_cli command.
    """
    s = status if isinstance(status, dict) else {}
    if not s.get("identity_written"):
        return {"step": "identity", "done": False,
                "next_cmd": "io_cli identity-init --agent-name <name> --dimensions <json> --fresh-start"}
    if not s.get("chat_loop_verified"):
        return {"step": "live_loop", "done": False, "next_cmd": "io_cli chat-verify-loop"}
    if int(s.get("agent_messages_count") or 0) < 1:
        return {"step": "greet", "done": False,
                "next_cmd": "send your greeting now (the resident consumer delivers it; no io_cli verb for this)"}
    return {"step": "complete", "done": True, "next_cmd": ""}


def cmd_onboard(args):
    """Next-step onboarding guide. GET /v1/bootstrap/status -> _next_onboarding_step."""
    api_url, auth = _require_backend()
    status, body = _http_json("GET", f"{api_url}/v1/bootstrap/status", auth)
    nxt = _next_onboarding_step(body if isinstance(body, dict) else {})
    _emit({"ok": status == 200, "http_status": status, "status": body, **nxt},
          0 if status == 200 else 1)


def _onboard_start_payload():
    """Pure: the /v1/track/event body for the onboard-start signal.

    The backend reads ``event_type`` (falling back to ``type``), not ``event``
    — using the wrong key silently records the event as type="unknown"."""
    return {"event_type": "resident_onboarding_started"}


def cmd_onboard_start(args):
    """Signal onboarding began (idempotent-ish). POST /v1/track/event."""
    api_url, auth = _require_backend()
    status, body = _http_json("POST", f"{api_url}/v1/track/event", auth,
                               payload=_onboard_start_payload())
    _emit({"ok": status in (200, 201), "http_status": status}, 0 if status in (200, 201) else 1)


def _doctor_summary(checks):
    """Pure: fold a {check_name: bool} map into {ok, checks, failed[]}."""
    failed = [k for k, v in (checks or {}).items() if not v]
    return {"ok": not failed, "checks": checks, "failed": failed}


def cmd_doctor(args):
    """Five-probe environment health check, read-only. GET/POST against api/enclave.

    Purpose: surface environment failures early (sandbox no-net, bad key, enclave
    down) rather than let onboarding fail opaquely deep in a later step. Every
    probe is a pure read (no chat message sent, no card written).

    Fresh-account judgment (no identity/memory/chat yet): checked against the
    actual handlers — GET /v1/identity/get (backend identity_core.get_identity,
    and its enclave decrypt-and-serve proxy) both return 200 with
    ``identity: None`` when nothing has been written yet, never 404; POST
    /v1/memory/index returns 200 with an empty items list on a fresh account;
    GET /v1/chat/history returns 200 with an empty list. So a plain 2xx check
    already treats "reachable but nothing there yet" as a pass — no fresh-account
    special-casing needed. A genuine connection failure / 401 / 5xx is what
    fails a check.
    """
    auth = _auth_headers()
    api_url = _env("FEEDLING_API_URL")
    enclave_url = _env("FEEDLING_ENCLAVE_URL")

    def _ok(method, url, insecure=False, payload=None):
        try:
            s, _ = _http_json(method, url, auth, payload=payload, insecure=insecure, timeout=10)
            return 200 <= s < 300
        except Exception:
            return False

    checks = {
        "api": bool(api_url) and _ok("GET", f"{api_url.rstrip('/')}/v1/users/whoami"),
        "enclave": bool(enclave_url) and _ok("GET", f"{enclave_url.rstrip('/')}/v1/identity/get", insecure=True),
        "identity": bool(api_url) and _ok("GET", f"{api_url.rstrip('/')}/v1/identity/get"),
        # /v1/memory/index is POST-only (a readside query, not a mutation) — a
        # GET here would 405 every time, always failing the check.
        "memory": bool(api_url) and _ok("POST", f"{api_url.rstrip('/')}/v1/memory/index", payload={"limit": 1}),
        "chat": bool(api_url) and _ok("GET", f"{api_url.rstrip('/')}/v1/chat/history?limit=1"),
    }
    out = _doctor_summary(checks)
    _emit(out, 0 if out["ok"] else 1)


def cmd_phase2(args):
    # send / sleep / schedule-wake / cancel-wake are NOT pull tools in the native
    # model — the agent emits them as output actions (JSON messages/actions) which
    # the resident consumer parses and executes. They are intentionally not CLI
    # verbs; calling them here is a no-op stub.
    _emit({"ok": False,
           "error": f"'{args.verb}' is not an io_cli tool — emit it as an agent output action "
                    f"(messages/send_message/sleep/schedule_wake), not a tool call.",
           "see": "docs/PROACTIVE_PERCEPTION_SPEC_V2.md"}, 3)


def main():
    p = argparse.ArgumentParser(
        prog="io_cli",
        description="Feedling resident-agent tool client. Outputs JSON.",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    pp = sub.add_parser("perception", help="Pull current coarse perception signals (JSON).")
    pp.add_argument(
        "signals", nargs="*",
        help="one or more of: " + ", ".join(PERCEPTION_SIGNALS) + " (default: fast set)",
    )
    pp.set_defaults(func=cmd_perception)

    pra = sub.add_parser(
        "perception-recent-apps",
        help="Which apps the user opened recently (newest first). Use this for "
             "'what have I been doing/using' — `perception app` only knows the "
             "last 15 minutes.",
    )
    pra.add_argument("--limit", type=int, default=20)
    pra.add_argument("--hours", type=float, default=0, help="only opens within the last N hours")
    pra.set_defaults(func=cmd_perception_recent_apps)

    pt = sub.add_parser("perception-trend",
                        help="Rolling baseline + delta for one numeric field (sense change vs norm).")
    pt.add_argument("signal", help="e.g. vitals/steps/sleep/weather/activity/metabolic/body")
    pt.add_argument("--field", default="", help="numeric field, e.g. resting_heart_rate / step_count / asleep_minutes")
    pt.add_argument("--days", type=int, default=30)
    pt.set_defaults(func=cmd_perception_trend)

    ph = sub.add_parser("perception-history",
                        help="Raw per-day rollup docs for a signal over N days.")
    ph.add_argument("signal", help="e.g. vitals/sleep/motion/location/calendar/reminders/mood")
    ph.add_argument("--days", type=int, default=14)
    ph.set_defaults(func=cmd_perception_history)

    mi = sub.add_parser("memory-index", help="Compact memory index (readside, plaintext-safe).")
    mi.add_argument("--limit", type=int, default=50)
    mi.add_argument("--bucket", default="", help="filter by bucket name")
    mi.add_argument("--thread", default="", help="filter by thread/dimension tag")
    mi.add_argument("--query", default="", help="free-text relevance query")
    mi.add_argument("--ambient", action="store_true", help="ambient (background) selection mode")
    mi.add_argument("--include-sensitive", dest="include_sensitive", action="store_true")
    mi.set_defaults(func=cmd_memory_index)

    mf = sub.add_parser("memory-fetch", help="Verbatim decrypted memory cards by id.")
    mf.add_argument("ids", nargs="+", help="one or more memory card ids")
    mf.add_argument("--limit", type=int, default=20)
    mf.add_argument("--include-archived", dest="include_archived", action="store_true")
    mf.add_argument("--include-superseded", dest="include_superseded", action="store_true")
    mf.set_defaults(func=cmd_memory_fetch)

    sr = sub.add_parser("screen-recent", help="Recent screen frame metadata (no pixels).")
    sr.add_argument("--limit", type=int, default=10)
    sr.set_defaults(func=cmd_screen_recent)

    sd = sub.add_parser("screen-read", help="Decrypted screen frame caption/ocr (pixels off by default).")
    sd.add_argument("--frame-id", dest="frame_id", default="", help="frame id; default = latest")
    sd.add_argument("--include-image", dest="include_image", action="store_true", help="save decrypted frame to a file; returns image_file path to Read")
    sd.set_defaults(func=cmd_screen_read)

    pr = sub.add_parser("photo-recent", help="Recent photo metadata (scene/time; no raw pixels).")
    pr.add_argument("--limit", type=int, default=10)
    pr.set_defaults(func=cmd_photo_recent)

    pd = sub.add_parser("photo-read", help="One specific photo's details by id (metadata + optional image).")
    pd.add_argument("--id", dest="photo_id", required=True, help="photo id (from photo-recent)")
    pd.add_argument("--include-image", dest="include_image", action="store_true", help="save decrypted photo to a file; returns image_file path to Read")
    pd.set_defaults(func=cmd_photo_read)

    ci = sub.add_parser("chat-image", help="Pull one PAST chat message's image by id (saves a file to Read).")
    ci.add_argument("--id", dest="message_id", required=True, help="chat message id (from the [image] placeholder in the transcript)")
    ci.add_argument("--limit", type=int, default=20, help="how many recent messages to search for the id")
    ci.set_defaults(func=cmd_chat_image)

    sw = sub.add_parser("schedule-wake", help="Ask to be woken at a later time (native self-wake).")
    sw.add_argument("--at", required=True, help="When to wake: ISO time (e.g. 2026-06-29T18:00) or a relative spec.")
    sw.add_argument("--tz", default="", help="IANA timezone (optional; defaults to the user's).")
    sw.add_argument("--reason", default="", help="Why you're scheduling it (optional).")
    sw.set_defaults(func=cmd_schedule_wake)

    cw = sub.add_parser("cancel-wake", help="Cancel a previously scheduled self-wake.")
    cw.add_argument("--wake-id", dest="wake_id", required=True, help="The scheduled wake/timer id to cancel.")
    cw.add_argument("--reason", default="", help="Why (optional).")
    cw.set_defaults(func=cmd_cancel_wake)

    ir = sub.add_parser("identity-read",
                        help="Read the CURRENT identity card (decrypted) — call before rewriting so you build on it (部分补全).")
    ir.set_defaults(func=cmd_identity_read)

    iw = sub.add_parser("identity-write",
                        help="Patch the agent's identity card (self_introduction / signature).")
    iw.add_argument("--self-introduction", dest="self_introduction", default=None)
    iw.add_argument("--signature", action="append", default=[],
                    help="repeatable short string(s) for the signature")
    iw.set_defaults(func=cmd_identity_write)

    ii = sub.add_parser("identity-init", help="Create the identity card (sanitizes + fresh-start).")
    ii.add_argument("--agent-name", default="")
    ii.add_argument("--self-introduction", default="")
    ii.add_argument("--dimensions", default="", help="JSON list of {name,value,description}")
    ii.add_argument("--days-with-user", type=int, default=None)
    ii.add_argument("--relationship-anchor-evidence", default="")
    ii.add_argument("--fresh-start", action="store_true", help="days=0 + standard anchor")
    ii.set_defaults(func=cmd_identity_init)

    mw = sub.add_parser("memory-write",
                        help="Write ONE memory card you distilled locally (plaintext; server encrypts).")
    mw.add_argument("--summary", default=None, help="one-line summary (index)")
    mw.add_argument("--content", default=None, help="card body (记忆/上下文/使用提示)")
    mw.add_argument("--bucket", default=None, help="single main bucket (reuse existing via memory-index)")
    mw.add_argument("--threads", action="append", default=[], help="repeatable cross-cutting thread(s)")
    mw.add_argument("--importance", type=float, default=None, help="0-1")
    mw.add_argument("--pulse", type=float, default=None, help="0-1")
    mw.add_argument("--type", default="fact", help="fact|event|quote|moment")
    mw.add_argument("--source", default="resident_absorb")
    mw.set_defaults(func=cmd_memory_write)

    md = sub.add_parser("memory-delete",
                        help="Delete ONE memory card by id (hard delete, like Garden's delete).")
    md.add_argument("--id", required=True, help="memory_id (from memory-index)")
    md.add_argument("--reason", default=None, help="why (optional, audit trail)")
    md.set_defaults(func=cmd_memory_delete)

    mp = sub.add_parser("memory-patch",
                        help="Modify ONE card by id (supersede w/ corrected content; server encrypts).")
    mp.add_argument("--id", required=True, help="memory_id to correct (from memory-index)")
    mp.add_argument("--summary", default=None, help="new one-line summary (index)")
    mp.add_argument("--content", default=None, help="new card body (记忆/上下文/使用提示)")
    mp.add_argument("--bucket", default=None, help="override bucket (else inherits old card's)")
    mp.add_argument("--threads", action="append", default=[], help="override thread(s) (else inherits)")
    mp.add_argument("--importance", type=float, default=None, help="0-1 (else inherits)")
    mp.add_argument("--pulse", type=float, default=None, help="0-1 (else inherits)")
    mp.add_argument("--type", default="fact", help="fact|event|quote|moment")
    mp.add_argument("--source", default="resident_patch")
    mp.add_argument("--reason", default=None, help="why (optional, audit trail)")
    mp.set_defaults(func=cmd_memory_patch)
    ov = sub.add_parser("onboarding-validate",
                        help="Server-computed onboarding acceptance snapshot (next_action etc.).")
    ov.set_defaults(func=cmd_onboarding_validate)

    cvl = sub.add_parser("chat-verify-loop",
                         help="Liveness probe: ping the resident-consumer reply pipeline and wait for a reply.")
    cvl.set_defaults(func=cmd_chat_verify_loop)

    ob = sub.add_parser("onboard",
                        help="Next-step onboarding guide (bootstrap status + what to run next).")
    ob.set_defaults(func=cmd_onboard)

    obs = sub.add_parser("onboard-start",
                         help="Signal that onboarding has started (track event).")
    obs.set_defaults(func=cmd_onboard_start)

    dr = sub.add_parser("doctor",
                        help="Five-probe environment health check (api/enclave/identity/memory/chat, read-only).")
    dr.set_defaults(func=cmd_doctor)

    for verb in PHASE2_VERBS:
        sp = sub.add_parser(verb, help="(phase 2 — not implemented yet)")
        sp.add_argument("rest", nargs="*")
        sp.set_defaults(func=cmd_phase2)

    args = p.parse_args()
    started = time.monotonic()
    exit_code = 0
    try:
        args.func(args)
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        raise
    except Exception:
        exit_code = 1
        raise
    finally:
        _emit_tool_trace(args, exit_code, (time.monotonic() - started) * 1000)


if __name__ == "__main__":
    main()
