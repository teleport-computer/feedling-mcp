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


def _add_memory_payload(text, filename, as_kind, client_job_id):
    """Shape the plaintext-genesis body for a VPS-side re-distill (pure).

    Mirrors iOS uploadGenesisPlaintext + backend/genesis/plaintext.py field names:
    memory   -> mode=add_memory,      memory_summary_content
    identity -> mode=update_identity, ai_persona_content + character_content
    """
    payload = {
        "format": "auto",
        "content": "",
        "fresh_start": False,
        "client_job_id": client_job_id,
    }
    name = (filename or "").strip()
    if as_kind == "identity":
        payload["mode"] = "update_identity"
        payload["ai_persona_content"] = text
        payload["character_content"] = text
        if name:
            payload["ai_persona_filename"] = name
            payload["character_filename"] = name
    else:  # memory (default)
        payload["mode"] = "add_memory"
        payload["memory_summary_content"] = text
        if name:
            payload["memory_summary_filename"] = name
    return payload


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


def cmd_phase2(args):
    # send / sleep / schedule-wake / cancel-wake are NOT pull tools in the native
    # model — the agent emits them as output actions (JSON messages/actions) which
    # the resident consumer parses and executes. They are intentionally not CLI
    # verbs; calling them here is a no-op stub.
    _emit({"ok": False,
           "error": f"'{args.verb}' is not an io_cli tool — emit it as an agent output action "
                    f"(messages/send_message/sleep/schedule_wake), not a tool call.",
           "see": "docs/AFULL_PLAN.md"}, 3)


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

    iw = sub.add_parser("identity-write",
                        help="Patch the agent's identity card (self_introduction / signature).")
    iw.add_argument("--self-introduction", dest="self_introduction", default=None)
    iw.add_argument("--signature", action="append", default=[],
                    help="repeatable short string(s) for the signature")
    iw.set_defaults(func=cmd_identity_write)

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
