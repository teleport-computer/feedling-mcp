"""Per-user ElevenLabs Custom LLM gateway."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import time
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_auth, require_scope
from chat import idempotency as chat_idempotency
from core import envelope as core_envelope
from core import voice_token
from hosted import chat_send_core
from hosted import config_store as hosted_config_store
from notices import catalog as notices_catalog
from voice import results

router = APIRouter()
log = logging.getLogger("feedling.voice.gateway")

_VOICE_NAMESPACE = uuid.UUID("c1673607-3107-4554-87a1-5a8f55b70023")
_VOICE_BUFFER_TEXT = "... "


def _gateway_url(request: Request) -> str | None:
    configured = (os.environ.get("FEEDLING_VOICE_GATEWAY_PUBLIC_URL") or "").strip()
    base = configured or str(request.base_url).rstrip("/")
    if not base:
        return None
    if base.endswith("/v1/voice/chat/completions"):
        url = base.removesuffix("/chat/completions")
    elif base.endswith("/v1/voice"):
        url = base
    else:
        url = base.rstrip("/") + "/v1/voice"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    allow_private = (
        os.environ.get("FEEDLING_VOICE_ALLOW_PRIVATE_GATEWAY") or ""
    ).strip().lower() in {"1", "true", "yes"}
    try:
        private_host = ipaddress.ip_address(parsed.hostname).is_private
    except ValueError:
        private_host = parsed.hostname in {"localhost"}
    if private_host and not allow_private:
        return None
    if parsed.scheme != "https" and not allow_private:
        return None
    return url


def _content_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") not in {"text", "input_text"}:
            continue
        value = str(item.get("text") or "").strip()
        if value:
            parts.append(value)
    return "\n".join(parts).strip()


def _last_user_turn(payload: dict) -> tuple[str, int] | None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    user_messages = [
        item for item in messages
        if isinstance(item, dict) and str(item.get("role") or "") == "user"
    ]
    if not user_messages:
        return None
    text = _content_text(user_messages[-1].get("content"))
    if not text:
        return None
    return text, len(user_messages)


def _voice_turn_id(user_turn_index: int) -> str:
    """Keep ASR revisions of the same utterance in one logical voice turn."""
    return str(max(1, user_turn_index))


def _voice_session_context(payload: dict) -> tuple[str, str, str, list[str]]:
    """Read per-call fields from ElevenLabs' custom LLM envelope."""
    extra = payload.get("elevenlabs_extra_body")
    if not isinstance(extra, dict):
        extra = {}
    token = str(
        extra.get("io_voice_token") or payload.get("io_voice_token") or ""
    ).strip()
    call_id = str(
        extra.get("io_call_id") or payload.get("io_call_id") or ""
    ).strip()
    tts_model = str(
        extra.get("io_tts_model") or payload.get("io_tts_model") or "v3"
    ).strip().lower()
    if tts_model not in {"flash", "v3"}:
        tts_model = "v3"
    safe_keys = sorted(
        str(key)[:48]
        for key in extra.keys()
        if isinstance(key, str)
    )[:24]
    return token, call_id, tts_model, safe_keys


def _sse_chunk(request_id: str, *, content: str = "", role: str = "", finish=None) -> str:
    delta: dict[str, str] = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content
    payload = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "io-current",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def _incremental_suffix(previous: str, current: str) -> str:
    """Return only newly appended model text; never replay a rewritten prefix."""
    if not previous:
        return current
    if current.startswith(previous):
        return current[len(previous) :]
    return ""


def _final_suffix(streamed: str, final_text: str) -> str:
    if not streamed:
        return final_text
    if final_text.startswith(streamed):
        return final_text[len(streamed) :]
    if streamed.startswith(final_text):
        return ""
    common = 0
    for left, right in zip(streamed, final_text):
        if left != right:
            break
        common += 1
    return final_text[common:]


def _failed_turn_text(user_id: str, message_id: str) -> str | None:
    """Return the normal chat failure copy once this voice turn has settled."""
    import db

    row = db.chat_get_strict(user_id, message_id)
    if not isinstance(row, dict) or str(row.get("reply_status") or "") != "failed":
        return None
    return notices_catalog.user_text_for(str(row.get("reply_failure_code") or "unknown"))


def _resident_voice_send_core(
    store,
    *,
    message: str,
    client_msg_id: str,
    call_id: str,
    turn_id: str,
) -> tuple[dict, int]:
    """Put a voice transcript through the same lane as resident text chat."""
    envelope, envelope_error = core_envelope._build_shared_envelope_for_store(
        store,
        message.encode("utf-8"),
        item_id=client_msg_id,
    )
    if envelope is None:
        return {
            "error": "user_message_envelope_failed",
            "detail": envelope_error,
        }, 409
    row, inserted = store.append_chat_idempotent(
        "user",
        "chat",
        envelope,
        client_msg_id=client_msg_id,
        window_sec=chat_idempotency.CLIENT_MSG_ID_WINDOW_SEC,
        extra={"voice_call_id": call_id, "voice_turn_id": turn_id},
    )
    if inserted:
        store.notify_chat_waiters()
    return {
        "status": "processing",
        "reply_ready": False,
        "user_message": {"id": row.get("id"), "ts": row.get("ts")},
        "runtime": {"mode": "resident"},
    }, 202


def _is_resident_voice_runtime(store) -> bool:
    if hosted_config_store.hosted_runtime_policy() != (
        hosted_config_store.HOSTED_RUNTIME_POLICY_DUAL
    ):
        return False
    mode, state, _generation = (
        hosted_config_store.get_hosted_runtime_control_strict(store)
    )
    return (
        mode == hosted_config_store.HOSTED_RUNTIME_MODE_RESIDENT
        and state == "resident"
    )


def _voice_error_text(body: dict) -> str:
    code = str(body.get("error") or "unknown").strip() or "unknown"
    return notices_catalog.user_text_for(code)


def _streaming_text_response(request_id: str, text: str) -> StreamingResponse:
    async def stream():
        yield _sse_chunk(request_id, role="assistant")
        for offset in range(0, len(text), 18):
            yield _sse_chunk(request_id, content=text[offset : offset + 18])
            await asyncio.sleep(0)
        yield _sse_chunk(request_id, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/v1/voice/sessions")
async def create_voice_session(
    request: Request, auth: AuthResult = Depends(require_auth)
):
    gateway_url = _gateway_url(request)
    if gateway_url is None:
        return JSONResponse(
            {
                "error": "voice_gateway_not_configured",
                "detail": "a public HTTPS voice gateway URL is required",
            },
            status_code=503,
        )
    call_id = "vcall_" + secrets.token_urlsafe(18)
    try:
        token, expires_at = voice_token.mint(
            results.secret(),
            user_id=auth.user_id,
            call_id=call_id,
            ttl=600.0,
        )
    except RuntimeError:
        return JSONResponse({"error": "voice_gateway_not_configured"}, status_code=503)
    return {
        "call_id": call_id,
        "token": token,
        "gateway_url": gateway_url,
        "expires_at": expires_at,
    }


@router.post("/v1/voice/chat/completions")
async def voice_chat_completions(request: Request):
    payload = (await asgi_http.read_json_silent(request)) or {}
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    token, call_id, _tts_model, extra_keys = _voice_session_context(payload)
    try:
        claims = voice_token.verify(results.secret(), token)
    except (voice_token.VoiceTokenError, RuntimeError) as exc:
        payload_keys = sorted(
            str(key)[:48] for key in payload.keys()
            if isinstance(key, str)
        )[:24]
        log.warning(
            "[voice.gateway] session rejected reason=%s token_present=%s "
            "token_len=%d call_present=%s call_len=%d payload_keys=%s extra_keys=%s",
            str(exc)[:80],
            bool(token),
            len(token),
            bool(call_id),
            len(call_id),
            payload_keys,
            extra_keys,
        )
        return JSONResponse({"error": "voice_session_unauthorized"}, status_code=401)
    if call_id != str(claims.get("call_id") or ""):
        return JSONResponse({"error": "voice_session_mismatch"}, status_code=401)
    turn = _last_user_turn(payload)
    if turn is None:
        return JSONResponse({"error": "voice_user_message_required"}, status_code=400)
    message, user_turn_index = turn
    if len(message) > 12000:
        return JSONResponse({"error": "message_too_long"}, status_code=413)

    turn_id = _voice_turn_id(user_turn_index)
    client_msg_id = str(uuid.uuid5(_VOICE_NAMESPACE, f"{call_id}:{turn_id}"))
    user_id = str(claims["user_id"])
    request_id = "chatcmpl-" + hashlib.sha256(
        f"{call_id}:{turn_id}".encode("utf-8")
    ).hexdigest()[:24]

    from core import store as core_store

    store = await threadpool.run_db(core_store.get_store, user_id)
    try:
        resident_runtime = await threadpool.run_db(
            _is_resident_voice_runtime, store
        )
    except Exception as exc:
        log.warning(
            "[voice.gateway] runtime read failed user=%s type=%s",
            user_id[:12],
            type(exc).__name__,
        )
        body, status = {"error": "runtime_control_unavailable"}, 503
    else:
        if resident_runtime:
            body, status = await threadpool.run_db(
                _resident_voice_send_core,
                store,
                message=message,
                client_msg_id=client_msg_id,
                call_id=call_id,
                turn_id=turn_id,
            )
        else:
            body, status = await threadpool.run_db(
                chat_send_core.model_api_chat_send_core,
                store,
                api_key=None,
                runtime_tok=results.mint_enclave_token(user_id),
                payload={"message": message, "client_msg_id": client_msg_id},
                voice_context={"call_id": call_id, "turn_id": turn_id},
            )
    if status >= 400:
        code = str(body.get("error") or "unknown")
        log.warning(
            "[voice.gateway] turn rejected user=%s status=%d code=%s",
            user_id[:12],
            status,
            code[:80],
        )
        # Authentication and malformed ElevenLabs requests still fail as HTTP.
        # Once a valid user transcript reached IO, however, returning JSON 4xx/5xx
        # makes ElevenLabs tear down the whole call as custom_llm_error. Keep the
        # call alive and speak IO's normal user-facing failure copy instead.
        return _streaming_text_response(request_id, _voice_error_text(body))
    message_id = str((body.get("user_message") or {}).get("id") or "").strip()
    if not message_id:
        return JSONResponse({"error": "voice_turn_not_accepted"}, status_code=502)
    log.info(
        "[voice.gateway] turn accepted user=%s runtime=%s",
        user_id[:12],
        "resident" if resident_runtime else "v2",
    )

    async def stream():
        yield _sse_chunk(request_id, role="assistant")
        # ElevenLabs documents an ellipsis-plus-space chunk as the compatibility
        # buffer for slow Custom LLMs. It buys the real IO model time without
        # speaking a canned filler sentence.
        yield _sse_chunk(request_id, content=_VOICE_BUFFER_TEXT)
        started_at = time.monotonic()
        deadline = time.monotonic() + 115.0
        streamed_by_segment: dict[int, str] = {}
        first_real_content_at: float | None = None
        buffer_count = 1
        next_keepalive = time.monotonic() + 3.5
        reply = None
        while time.monotonic() < deadline:
            snapshots = await threadpool.run_db(
                results.load_stream_texts,
                user_id,
                call_id=call_id,
                turn_id=turn_id,
            )
            for snapshot in snapshots:
                segment = int(snapshot.get("segment") or 0)
                text = str(snapshot.get("text") or "")
                previous = streamed_by_segment.get(segment, "")
                suffix = _incremental_suffix(previous, text)
                if suffix:
                    if first_real_content_at is None:
                        first_real_content_at = time.monotonic()
                        log.info(
                            "[voice.gateway] first content user=%s ttft_ms=%d buffers=%d",
                            user_id[:12],
                            int((first_real_content_at - started_at) * 1000),
                            buffer_count,
                        )
                    yield _sse_chunk(request_id, content=suffix)
                if text.startswith(previous):
                    streamed_by_segment[segment] = text
            completed_segments = [
                int(snapshot.get("segment") or 0)
                for snapshot in snapshots
                if snapshot.get("is_final")
            ]
            if completed_segments and streamed_by_segment:
                latest_segment = max(streamed_by_segment)
                reply = {
                    "message_id": "",
                    "text": streamed_by_segment.get(latest_segment, ""),
                }
                break
            reply = await threadpool.run_db(
                results.load_reply,
                user_id,
                call_id=call_id,
                turn_id=turn_id,
            )
            if reply is not None:
                break
            failure_text = await threadpool.run_db(
                _failed_turn_text,
                user_id,
                message_id,
            )
            if failure_text:
                reply = {"message_id": "", "text": failure_text}
                break
            now = time.monotonic()
            if first_real_content_at is None and now >= next_keepalive:
                # SSE comments do not count as model output to ElevenLabs. Repeat
                # its documented ellipsis buffer until the first real token so a
                # slow IO model is not treated as a dead Custom LLM.
                yield _sse_chunk(request_id, content=_VOICE_BUFFER_TEXT)
                buffer_count += 1
                next_keepalive = now + 3.5
            await asyncio.sleep(0.15)
        if reply is None:
            reply = {
                "message_id": "",
                "text": notices_catalog.user_text_for("turn_timeout"),
            }
        text = str(reply.get("text") or "")
        latest_segment = max(streamed_by_segment, default=-1)
        streamed_final = streamed_by_segment.get(latest_segment, "")
        remaining = _final_suffix(streamed_final, text)
        for offset in range(0, len(remaining), 18):
            yield _sse_chunk(request_id, content=remaining[offset : offset + 18])
            await asyncio.sleep(0)
        yield _sse_chunk(request_id, finish="stop")
        yield "data: [DONE]\n\n"
        log.info(
            "[voice.gateway] stream complete user=%s dur_ms=%d buffers=%d real=%s",
            user_id[:12],
            int((time.monotonic() - started_at) * 1000),
            buffer_count,
            first_real_content_at is not None,
        )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/v1/internal/voice/reply")
async def internal_voice_reply(
    request: Request, auth: AuthResult = Depends(require_scope("voice_reply"))
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    stored = await threadpool.run_db(
        results.store_reply,
        auth.user_id,
        call_id=str(payload.get("call_id") or ""),
        turn_id=str(payload.get("turn_id") or ""),
        message_id=str(payload.get("message_id") or ""),
        text=str(payload.get("text") or ""),
    )
    return {"status": "stored" if stored else "ignored"}


@router.post("/v1/internal/voice/delta")
async def internal_voice_delta(
    request: Request, auth: AuthResult = Depends(require_scope("voice_reply"))
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    stored = await threadpool.run_db(
        results.store_stream_text_for_parent,
        auth.user_id,
        parent_message_id=str(payload.get("parent_message_id") or ""),
        segment=payload.get("segment"),
        text=str(payload.get("text") or ""),
        is_final=bool(payload.get("final")),
    )
    return {"status": "stored" if stored else "ignored"}


def register_asgi(app) -> None:
    app.include_router(router)
