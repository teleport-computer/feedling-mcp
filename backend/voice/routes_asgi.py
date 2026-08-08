"""Per-user ElevenLabs Custom LLM gateway."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
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
from core import wake_bus
from hosted import chat_send_core
from hosted import config_store as hosted_config_store
from notices import catalog as notices_catalog
from voice import results
from voice.message_filter import is_meaningful_voice_message

router = APIRouter()
log = logging.getLogger("feedling.voice.gateway")

_VOICE_NAMESPACE = uuid.UUID("c1673607-3107-4554-87a1-5a8f55b70023")
_VOICE_BUFFER_TEXT = "... "
# 「这一轮不说话」也必须是一个**带正文**的 completion。
#
# 2026-08-08 线上事故:噪音轮/生命周期已结束/本轮被更新的 ASR 取代这三条路径都
# 返回了一个零 content 的 SSE 流(只有 role 块 + finish 块)。ElevenLabs 的
# Custom LLM 拿到没有任何正文的 completion 会判协议错误
# —— `1002 custom_llm_error: LLM Cascade Error` —— 然后**杀掉整通电话**,
# 用户侧看到的是「暂时无法通话」。客户端日志里的前一行正是
# `ignored control-only agent response`。
#
# 一个空格在 TTS 里不发声,语义正好是「这一轮没有话要说」,同时协议合法。
# 绝不要在这里放真实文案:那等于替伴侣说了它没说过的话。
_VOICE_SILENT_TURN_TEXT = " "


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


def _positive_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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


def _voice_revision_turn_id(
    call_id: str,
    logical_turn_id: str,
    message: str,
    *,
    secret_key: bytes,
) -> str:
    """Stable opaque delivery id for one exact ASR revision.

    ElevenLabs can issue another Custom LLM request for the same logical user
    turn after a short pause.  The logical id groups those revisions; this
    content-bound id isolates their reply streams and transport idempotency.
    HMAC keeps short spoken phrases out of plaintext routing metadata.
    """
    label = f"{call_id}\n{logical_turn_id}\n{message}".encode("utf-8")
    digest = hmac.new(secret_key, label, hashlib.sha256).hexdigest()[:20]
    return f"{logical_turn_id}.{digest}"


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
    logical_turn_id: str,
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
        extra={
            "voice_call_id": call_id,
            "voice_turn_id": turn_id,
            "voice_logical_turn_id": logical_turn_id,
            "voice_turn_status": "current",
        },
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
    # 空文本会退化成一个**零 content 块**的流,ElevenLabs 判协议错误并拆掉整通
    # 电话(见 _VOICE_SILENT_TURN_TEXT)。保证放在这里而不是各调用点:调用点是
    # 开集(噪音轮/生命周期已结束/以后还会有别的"这一轮不说话"),漏一个就是
    # 一次线上事故。主流程那个生成器不受影响 —— 它开头无条件发 "... " 缓冲块。
    body = text if text else _VOICE_SILENT_TURN_TEXT

    async def stream():
        yield _sse_chunk(request_id, role="assistant")
        for offset in range(0, len(body), 18):
            yield _sse_chunk(request_id, content=body[offset : offset + 18])
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
    try:
        import db as _db

        await threadpool.run_db(
            _db.voice_call_create_active, auth.user_id, call_id
        )
    except Exception as exc:  # noqa: BLE001 — no token without its tombstone row
        log.warning(
            "[voice.session] lifecycle create failed user=%s type=%s",
            auth.user_id[:12],
            type(exc).__name__,
        )
        return JSONResponse({"error": "voice_session_unavailable"}, status_code=503)
    return {
        "call_id": call_id,
        "token": token,
        "gateway_url": gateway_url,
        "expires_at": expires_at,
    }


@router.post("/v1/voice/cancel")
async def cancel_voice_call(
    request: Request, auth: AuthResult = Depends(require_auth)
):
    """Idempotently end an unarchived call and suppress every late reply."""
    from core import store as core_store
    import db as _db
    from voice import cleanup as voice_cleanup
    from voice import transcript_store

    payload = (await asgi_http.read_json_silent(request)) or {}
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    call_id = str(payload.get("call_id") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if not call_id.startswith("vcall_") or len(call_id) > 96:
        return JSONResponse({"error": "call_id_required"}, status_code=400)
    if (
        not reason
        or len(reason) > 64
        or any(not char.isprintable() for char in reason)
    ):
        return JSONResponse({"error": "cancel_reason_required"}, status_code=400)

    user_id = auth.user_id

    def _cancel() -> tuple[dict, int]:
        lifecycle = _db.voice_call_cancel(user_id, call_id, reason)
        if lifecycle["status"] in {"finalizing", "finalized"}:
            return {
                "status": lifecycle["status"],
                "call_id": call_id,
                "replayed": True,
                "deleted": 0,
                "retained_covered": 0,
                "remaining": 0,
            }, 200
        # 守卫:客户端只在**它自己看到的** SDK 转写列表为空时才发 cancel
        # (VoiceCallTerminationPolicy: turns.isEmpty ? .cancel : .finalize)。
        # 那份列表落定晚于服务端落行:说完立刻挂断/切后台时,服务端明明已有真实
        # 逐轮行和伴侣的回复,客户端仍会判成"空通话"。此时无条件删行 =
        # **整通电话永久消失**,而且墓碑之后会一直 409 挡住任何补救性 finalize。
        # 所以:有行、又没归档过 → 只写墓碑(止住迟到回复),行留着等 finalize。
        existing_rows = voice_cleanup.call_message_rows(user_id, call_id)
        if existing_rows and not transcript_store.exists(user_id, call_id):
            log.warning(
                "[voice.cancel] kept %d row(s) user=%s call=%s — client reported "
                "an empty call but the server has real turns and no archive",
                len(existing_rows), user_id[:12], call_id[:16],
            )
            return {
                "status": lifecycle["status"],
                "call_id": call_id,
                "replayed": False,
                "deleted": 0,
                "retained_covered": 0,
                "remaining": len(existing_rows),
                "rows_kept_for_finalize": True,
            }, 200
        handoff = results.delete_call_state(user_id, call_id)
        cleanup = voice_cleanup.delete_call_messages(user_id, call_id)
        store = core_store.get_store(user_id)
        # Reload from the authoritative rows: older assistant messages may be
        # discoverable only through reply_to_message_id and carry no call id in
        # this worker's stale cache.
        store.reload()
        store.notify_chat_waiters()
        wake_bus.notify("chat", user_id)
        if cleanup["remaining"] > 0:
            return {
                "error": "voice_cleanup_incomplete",
                "call_id": call_id,
                **cleanup,
                **handoff,
            }, 502
        log.info(
            "[voice.cancel] ended user=%s call=%s reason=%s replay=%s",
            user_id[:12], call_id[:24], reason, lifecycle["replayed"],
        )
        return {
            "status": "cancelled",
            "call_id": call_id,
            "replayed": bool(lifecycle["replayed"]),
            **cleanup,
            **handoff,
        }, 200

    body, status = await asyncio.to_thread(_cancel)
    return JSONResponse(body, status_code=status)


@router.post("/v1/voice/finalize")
async def finalize_voice_call(
    request: Request, auth: AuthResult = Depends(require_auth)
):
    """Hangup: archive the full transcript, leave ONE small card, run Capture.

    The live call is untouched (per-turn voice rides the normal chat lane: same
    identity, memory and tools). At hangup the client sends the call's plaintext
    transcript (chat rows are ciphertext the server cannot read) and three
    things happen, in this order:

    1. the FULL transcript is archived to ``voice_transcripts`` — permanent,
       E2E-sealed, readable from Settings and by the agent's transcript tools;
    2. one bounded preview card (``voice_call_transcript``) replaces the call's
       per-turn rows in the chat stream, so neither the prompt tail nor the
       history carries a whole call;
    3. Capture is forced. It renders the archived FULL text into its window in
       place of this card, so memory is distilled from everything that was said
       — through the ordinary capture pipeline, not a parallel one.

    There is deliberately no summary: a 1-3 sentence model-written précis was
    both a second model call and a lossy memory input. The card's preview is
    mechanical (head + tail), the archive holds the rest.

    Idempotent: the card's message id derives from ``call_id``; a retried
    finalize finds the row and re-runs only the (idempotent) cleanup and
    capture nudge. On any archive/card failure the per-turn rows are KEPT
    (nothing is lost) and the client may retry.
    """
    from core import store as core_store
    from proactive import proactive_core
    from voice import cleanup as voice_cleanup
    from voice import transcript_store

    payload = (await asgi_http.read_json_silent(request)) or {}
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    call_id = str(payload.get("call_id") or "").strip()
    turns = payload.get("turns")
    if not call_id or not call_id.startswith("vcall_") or len(call_id) > 96:
        return JSONResponse({"error": "call_id_required"}, status_code=400)
    if not isinstance(turns, list) or not any(
        isinstance(t, dict) and str(t.get("text") or "").strip() for t in turns
    ):
        return JSONResponse({"error": "turns_required"}, status_code=400)
    if len(json.dumps(turns)) > 120_000:
        return JSONResponse({"error": "transcript_too_long"}, status_code=413)
    user_id = auth.user_id
    duration_sec = _positive_int(payload.get("duration_sec"))
    mid = voice_cleanup.transcript_card_message_id(call_id)

    def _finalize() -> tuple[dict, int]:
        import db as _db

        lifecycle = _db.voice_call_begin_finalize(user_id, call_id)
        if lifecycle["status"] == "cancelled":
            return {"error": "voice_call_cancelled"}, 409
        store = core_store.get_store(user_id)

        # Idempotent replay. The judge is the ARCHIVE, not the chat card: the
        # card id reuses the summary era's uuid5 namespace, so a row written by
        # an older build would otherwise read as "already handled" and this
        # request would delete the per-turn rows without ever storing the
        # transcript the client just re-sent — losing the call for good.
        archived = transcript_store.exists(user_id, call_id)
        already = archived and _db.chat_get_strict(user_id, mid) is not None
        if not archived:
            # 真名优先:这份记录用户会亲眼读,Capture 也拿它当输入,两处都该看到
            # TA 给伴侣起的名字而不是中性标签。取不到才退回既有兜底。
            speaker_user, speaker_ai = transcript_store.resolve_speaker_names(store)
            text = transcript_store.render_transcript(
                turns, user_name=speaker_user, ai_name=speaker_ai)
            if not text:
                return {"error": "turns_required"}, 400
            try:
                # Archive FIRST. The per-turn rows below are the only other copy
                # of this call, so nothing may delete them until the archive is
                # durable. Unlike a summary this cannot degrade — it either
                # stores every word or fails the request.
                stats = transcript_store.persist(
                    store, call_id, text,
                    turn_count=len(turns), duration_sec=duration_sec,
                    chat_message_id=mid,
                )
            except Exception as exc:  # noqa: BLE001 — keep rows, let client retry
                log.warning(
                    "[voice.finalize] archive failed user=%s call=%s: %s",
                    user_id[:12], call_id[:24], str(exc)[:160],
                )
                return {"error": "voice_transcript_not_archived"}, 502
            if _db.chat_get_strict(user_id, mid) is not None:
                # 旧版本留下的摘要行占着这个 id。归档刚补上了,但那行的 source
                # 仍是 voice_call_summary —— 它读得出来、也不会被 capture 展开
                # (source 不在白名单里)。保留它而不是覆写:改写用户可见的历史
                # 比留一条旧卡更糟,而这通电话的记忆本来也早就蒸过了。
                log.info("[voice.finalize] legacy summary row kept user=%s call=%s",
                         user_id[:12], call_id[:24])
            elif not voice_cleanup.persist_transcript_card(
                store, transcript_store.build_preview(text), mid, call_id,
                turn_count=stats["turn_count"], duration_sec=stats["duration_sec"],
            ):
                return {"error": "voice_transcript_card_not_persisted"}, 502
        lifecycle = _db.voice_call_mark_finalized(user_id, call_id)
        if lifecycle["status"] == "cancelled":
            return {"error": "voice_call_cancelled"}, 409
        cleanup = voice_cleanup.delete_call_messages(user_id, call_id)
        if cleanup["remaining"] > 0:
            # Deletable rows survived (a DB blip inside chat_delete). The
            # summary is durable, so a client retry re-enters via the
            # idempotent replay path and re-runs only this cleanup.
            log.warning(
                "[voice.finalize] cleanup incomplete user=%s call=%s remaining=%d",
                user_id[:12], call_id[:24], cleanup["remaining"],
            )
            return {"error": "voice_cleanup_incomplete"}, 502
        # Prune this worker's hot cache and tell every other worker (same
        # three steps clear_history performs after deleting rows). The
        # transcript card itself must survive — it is what stands in for
        # the call from here on.
        with store.chat_lock:
            store.chat_messages = [
                m for m in store.chat_messages
                if str(m.get("voice_call_id") or "") != call_id
                or str(m.get("id") or "") == mid
            ]
        store.notify_chat_waiters()
        wake_bus.notify("chat", user_id)
        # Memory: one ordinary Capture round over the window that now contains
        # this call's card. The capture handler swaps that card for the archived
        # FULL transcript, so memory sees everything that was said — same
        # prompt, parser, consent gate and card writer as every other capture.
        # Safe to re-run on replay: capture is cursor-driven, so once the
        # frontier has passed this card it cannot be distilled twice.
        try:
            proactive_core.capture_force(store)
        except Exception as exc:  # noqa: BLE001 — a nudge, never the HTTP result
            log.warning(
                "[voice.finalize] capture nudge failed user=%s call=%s: %s",
                user_id[:12], call_id[:24], str(exc)[:160],
            )
        log.info(
            "[voice.finalize] archived user=%s call=%s turns=%d replay=%s",
            user_id[:12], call_id[:24], len(turns), already,
        )
        return {
            "status": "finalized",
            "transcript_message_id": mid,
            # Deprecated alias: shipped clients read summary_message_id. Dual
            # written for one release so an older build keeps working; drop it
            # once the transcript-card build is the floor.
            "summary_message_id": mid,
            "deleted_turns": cleanup["deleted"],
            "retained_covered": cleanup["retained_covered"],
            "replayed": already,
        }, 200

    body, status = await asyncio.to_thread(_finalize)
    return JSONResponse(body, status_code=status)


@router.get("/v1/voice/transcripts")
async def list_voice_transcripts(
    request: Request, auth: AuthResult = Depends(require_auth)
):
    """Newest-first list of this user's archived calls, WITHOUT the bodies.

    Settings renders this; the agent's ``voice_transcript_list`` tool reads the
    same shape. Metadata only — pulling a body is an explicit second call so a
    listing can never blow anyone's context.
    """
    from voice import transcript_store

    limit = _positive_int(request.query_params.get("limit")) or 50
    items = await threadpool.run_db(
        transcript_store.list_metadata, auth.user_id, limit=limit
    )
    return JSONResponse({"items": items, "count": len(items)})


@router.get("/v1/voice/transcripts/{call_id}")
async def get_voice_transcript(
    call_id: str, auth: AuthResult = Depends(require_auth)
):
    """One archived call as its raw v1 envelope — the client decrypts locally.

    Same posture as ``/v1/memory/list``: the server hands back ciphertext it
    cannot read. Server-side readers (Capture, the agent tools) go through
    ``transcript_store.load_plaintext`` and the enclave instead.
    """
    from voice import transcript_store

    call_id = str(call_id or "").strip()
    if not call_id.startswith("vcall_") or len(call_id) > 96:
        return JSONResponse({"error": "call_id_required"}, status_code=400)
    row = await threadpool.run_db(
        transcript_store.get_envelope, auth.user_id, call_id
    )
    if row is None:
        return JSONResponse({"error": "voice_transcript_not_found"}, status_code=404)
    return JSONResponse(row)


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

    logical_turn_id = _voice_turn_id(user_turn_index)
    turn_id = _voice_revision_turn_id(
        call_id,
        logical_turn_id,
        message,
        secret_key=results.secret(),
    )
    client_msg_id = str(uuid.uuid5(_VOICE_NAMESPACE, f"{call_id}:{turn_id}"))
    user_id = str(claims["user_id"])
    request_id = "chatcmpl-" + hashlib.sha256(
        f"{call_id}:{turn_id}".encode("utf-8")
    ).hexdigest()[:24]
    if not is_meaningful_voice_message(message):
        log.info(
            "[voice.gateway] turn ignored user=%s reason=non_speech",
            user_id[:12],
        )
        return _streaming_text_response(request_id, _VOICE_SILENT_TURN_TEXT)

    import db as _db

    if await threadpool.run_db(_db.voice_call_status, user_id, call_id) in {
        "finalizing",
        "cancelled",
        "finalized",
    }:
        return _streaming_text_response(request_id, _VOICE_SILENT_TURN_TEXT)

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
                logical_turn_id=logical_turn_id,
            )
        else:
            body, status = await threadpool.run_db(
                chat_send_core.model_api_chat_send_core,
                store,
                api_key=None,
                runtime_tok=results.mint_enclave_token(user_id),
                payload={"message": message, "client_msg_id": client_msg_id},
                voice_context={
                    "call_id": call_id,
                    "turn_id": turn_id,
                    "logical_turn_id": logical_turn_id,
                },
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
        next_lifecycle_check = time.monotonic()
        next_revision_check = time.monotonic()
        reply = None
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_lifecycle_check:
                status = await threadpool.run_db(
                    _db.voice_call_status, user_id, call_id
                )
                if status in {"finalizing", "cancelled", "finalized"}:
                    log.info(
                        "[voice.gateway] stream stopped user=%s status=%s",
                        user_id[:12], status,
                    )
                    break
                next_lifecycle_check = now + 0.6
            if now >= next_revision_check:
                current = await threadpool.run_db(
                    results.is_current_voice_turn,
                    user_id,
                    parent_message_id=message_id,
                )
                if not current:
                    yield _sse_chunk(request_id, finish="stop")
                    yield "data: [DONE]\n\n"
                    log.info(
                        "[voice.gateway] stream superseded user=%s",
                        user_id[:12],
                    )
                    return
                next_revision_check = now + 0.5
            snapshots = await threadpool.run_db(
                results.load_stream_texts,
                user_id,
                call_id=call_id,
                turn_id=turn_id,
            )
            has_new_stream_text = any(
                _incremental_suffix(
                    streamed_by_segment.get(int(snapshot.get("segment") or 0), ""),
                    str(snapshot.get("text") or ""),
                )
                for snapshot in snapshots
            )
            if has_new_stream_text:
                current = await threadpool.run_db(
                    results.is_current_voice_turn,
                    user_id,
                    parent_message_id=message_id,
                )
                if not current:
                    yield _sse_chunk(request_id, finish="stop")
                    yield "data: [DONE]\n\n"
                    log.info(
                        "[voice.gateway] stream superseded before content user=%s",
                        user_id[:12],
                    )
                    return
                next_revision_check = time.monotonic() + 0.5
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
        lifecycle_status = await threadpool.run_db(
            _db.voice_call_status, user_id, call_id
        )
        if reply is None and lifecycle_status not in {
            "finalizing",
            "cancelled",
            "finalized",
        }:
            reply = {
                "message_id": "",
                "text": notices_catalog.user_text_for("turn_timeout"),
            }
        if lifecycle_status not in {"finalizing", "cancelled", "finalized"}:
            current = await threadpool.run_db(
                results.is_current_voice_turn,
                user_id,
                parent_message_id=message_id,
            )
            if not current:
                yield _sse_chunk(request_id, finish="stop")
                yield "data: [DONE]\n\n"
                log.info(
                    "[voice.gateway] stream superseded before final user=%s",
                    user_id[:12],
                )
                return
        text = str((reply or {}).get("text") or "")
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
