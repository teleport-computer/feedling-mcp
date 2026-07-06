"""Framework-neutral body of ``POST /v1/model_api/chat/send`` (ASGI-migration).

The route logic — image parse, runtime-provider load, user-message envelope
build, driver resolve, supervisor wedge guard, chat append + wake, debug traces,
and the delegation to ``agent_runtime_cutover.handle_send`` — with no framework
request/response object. The ASGI route (``hosted.chat_routes_asgi``) calls this
and wraps the returned ``(body, status)`` in its response type, preserving the
202 contract, every debug/action trace, the 402/409/503/400/413 error branches,
and the single (non-double) append.

Every collaborator is referenced via its module (``agent_runtime_cutover.X``,
``hosted_config_store.X``, ``core_envelope.X`` …) so the existing tests that
monkeypatch those module attributes keep working unchanged.
"""

from __future__ import annotations

import base64
import time

from core import envelope as core_envelope

import debug_trace
from chat import service as chat_service
from hosted import agent_runtime_cutover
from hosted import config_store as hosted_config_store
from hosted import context as hosted_context
from hosted import turn as hosted_turn


def model_api_chat_send_core(
    store,
    *,
    api_key: str | None,
    runtime_tok: str,
    payload: dict,
) -> tuple[dict, int]:
    """Run a hosted chat send. Returns ``(body, status)``; the caller renders it.

    ``store`` is the resolved UserStore (Flask ``auth.require_user()`` / ASGI
    ``auth.store``). ``api_key`` mirrors Flask ``auth._extract_api_key()`` (None
    on the runtime-token path). ``runtime_tok`` mirrors the Flask forward: the
    verified runtime token when no api_key is present, else "". ``payload`` is the
    JSON body (``request.get_json(silent=True) or {}`` / ``read_json_silent``).
    """
    trace_start = time.time()
    image_bytes, image_mime, image_err = hosted_turn._model_api_image_payload(payload)
    if image_err:
        return {"error": "invalid_image", "detail": image_err}, 400
    image_b64 = base64.b64encode(image_bytes).decode("ascii") if image_bytes else ""
    has_image = image_bytes is not None
    message = str(payload.get("message") or payload.get("content") or "").strip()
    message_for_context = message or ("User sent an image." if has_image else "")
    context_refs = hosted_context._context_refs_from_payload(payload)
    if not message_for_context:
        return {"error": "message required"}, 400
    if len(message) > 12000:
        return {"error": "message too long", "max_chars": 12000}, 413

    runtime = hosted_config_store._load_runtime_provider_config(store, api_key, runtime_token=runtime_tok)
    if isinstance(runtime, tuple):
        _, err = runtime
        hosted_config_store._append_model_api_action_trace(store, {
            "status": "failed",
            "error": err.get("error", "runtime_load_failed"),
            "context": {"stage": "load_runtime"},
            "duration_ms": int((time.time() - trace_start) * 1000),
        })
        return err, 400
    hosted_config_store._ensure_model_api_runtime_profile(store, hosted_config_store._load_model_api_config(store), touch=True)

    user_plaintext = image_bytes if image_bytes is not None else message.encode("utf-8")
    user_env, env_err = core_envelope._build_shared_envelope_for_store(store, user_plaintext)
    if user_env is None:
        return {"error": "user_message_envelope_failed", "detail": env_err}, 409
    # 收口：配了 fit provider 即托管到 agent-runner，否则 409。
    # 先校验 driver 再入 store，避免未配置时写入孤儿用户消息。
    config = hosted_config_store._load_model_api_config(store)
    try:
        driver = agent_runtime_cutover.resolve_driver(config)
    except agent_runtime_cutover.UnsupportedProviderError:
        return {"error": "provider_not_configured"}, 409

    # Wedge guard: routing to the agent-runner only works if a supervisor is
    # actually hosting. assert_hosting_ready validated THIS process's env at
    # startup, but the consumer lives in a separate service — if its heartbeat is
    # missing/stale or its host-all/gateway flags are off, this turn would park in
    # "processing" forever. Surface a clear 503 instead, BEFORE writing the user
    # message (so no orphan turn is left unanswered). Fail-open on a DB hiccup.
    #
    # Only gate on gateway if this provider actually routes through the in-CVM
    # LiteLLM gateway. anthropic/deepseek (claude driver) and openai (codex-native)
    # bypass the gateway entirely and must not be blocked by a gateway-off heartbeat.
    _provider = str((config or {}).get("provider") or "")
    _require_gateway = agent_runtime_cutover.codex_transport(_provider) == "gateway"
    live, reason = agent_runtime_cutover.check_supervisor_live(require_gateway=_require_gateway)
    if not live:
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided", actor="host_agent_runtime",
            status="gated", summary="supervisor_unavailable",
            detail={"mode": "blocked", "reason": "supervisor_unavailable", "live_reason": str(reason or "")[:80]},
        )
        return {"error": "hosting_runtime_unavailable", "reason": reason}, 503

    extra: dict = {}
    if has_image and image_mime:
        extra["image_mime"] = image_mime
    if has_image and message:
        # 带文字说明的图片：独立加密 caption，enclave history 解后填 content。
        caption_env, caption_err = core_envelope._build_shared_envelope_for_store(
            store, message.encode("utf-8")
        )
        if caption_env:
            extra.update(chat_service._chat_caption_extra_from_envelope(caption_env))
        else:
            print(f"[model_api:{store.user_id}] caption_envelope_failed detail={caption_err}")
    user_row = store.append_chat(
        "user",
        "model_api",
        user_env,
        content_type="image" if has_image else "text",
        extra=extra or None,
    )
    store.notify_chat_waiters()

    # image turn 不再被挡在 legacy；consumer 已能处理图片 envelope。
    _turn_id = str(user_row.get("id") or "") if isinstance(user_row, dict) else ""
    debug_trace.trace_event(
        store, subsystem="route", type="route.decided", actor="host_agent_runtime",
        turn_id=_turn_id, summary="agent_runtime",
        detail={"mode": "agent_runtime", "has_image": bool(has_image)},
    )
    body, status = agent_runtime_cutover.handle_send(store, user_row, driver)
    return body, status
