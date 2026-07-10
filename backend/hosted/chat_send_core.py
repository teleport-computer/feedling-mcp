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
from core import wake_bus as core_wake_bus

import debug_trace
from chat import service as chat_service
from hosted import agent_runtime_cutover
from hosted import config_store as hosted_config_store
from hosted import context as hosted_context
from hosted import turn as hosted_turn
from model_api_runtime.v2 import admission
from model_api_runtime.v2 import jobs_store


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
    file_parse, file_err = hosted_turn._model_api_file_payload(payload)
    if file_err:
        return file_err  # (body, status) already shaped
    # An image sent through the file picker re-pipes into the image path so it
    # gets vision — reuse the exact image envelope/append below.
    if file_parse is not None and file_parse["kind"] == "image":
        image_bytes = file_parse["bytes"]
        image_mime = file_parse["mime"]
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        has_image = True
        file_parse = None
    has_file = file_parse is not None
    message = str(payload.get("message") or payload.get("content") or "").strip()
    message_for_context = message or (
        "User sent an image." if has_image else ("User sent a file." if has_file else "")
    )
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

    if has_image:
        user_plaintext = image_bytes
    elif has_file:
        user_plaintext = file_parse["bytes"]
    else:
        user_plaintext = message.encode("utf-8")
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

    # Hosted Runtime V2 灰度：db_action_v2 用户由独立 worker 池应答，不依赖 resident
    # supervisor，故跳过下面的 resident wedge guard（否则会被 resident 心跳错误 503）。
    _v2_mode = hosted_config_store.get_hosted_runtime_mode(store) == "db_action_v2"

    # V2 liveness guard: skipping the resident wedge guard above left db_action_v2
    # users with NO replacement — if every serve_worker process is dead (crashed,
    # not yet deployed, scaled to zero), enqueue_job below would still succeed
    # and the message would queue in agent_jobs forever with no error, no reply,
    # no visible failure. jobs_store.workers_alive() checks the v2_worker_heartbeats
    # table each live serve_worker process UPSERTs into every ~10s. Distinct
    # code/summary ("workers_unavailable") from the resident wedge guard's
    # "hosting_runtime_unavailable"/"supervisor_unavailable" — the two failure
    # modes are unrelated (dead worker pool vs. dead resident supervisor) and
    # must stay distinguishable to callers/on-call. Placed BEFORE append_chat so
    # nothing is persisted on refusal (same principle as the wedge guard above).
    if _v2_mode and not jobs_store.workers_alive():
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided", actor="host_agent_runtime",
            status="gated", summary="workers_unavailable",
            detail={"mode": "blocked", "reason": "workers_unavailable"},
        )
        return {"error": "workers_unavailable", "reason": "no_live_v2_worker_heartbeat"}, 503

    # §6 admission ceiling：存活闸已保证 ≥1 活 worker；再估排队等待，超 SLA 就在
    # persist 之前回独立 busy（区别于 workers_unavailable=供给死）。任何计算异常
    # fail-open（放行）——此闸绝不能自身变成故障源。
    if _v2_mode:
        _inflight = _workers = 0
        try:
            _workers = jobs_store.live_worker_count(within_sec=30)
            _inflight = jobs_store.inflight_job_count()
            _mean = jobs_store.recent_mean_service_sec(lane="chat", limit=admission.SERVICE_SAMPLE_N)
            _est = admission.estimate_wait_sec(
                inflight=_inflight, workers=_workers,
                mean_service_sec=_mean, default_service_sec=admission.DEFAULT_SERVICE_SEC,
            )
            _admit = admission.should_admit(_est, sla_sec=admission.SLA_SEC)
        except Exception as exc:  # fail-open
            debug_trace.trace_event(
                store, subsystem="route", type="route.decided", actor="host_agent_runtime",
                status="ok", summary="admission_failopen",
                detail={"mode": "admit", "error": str(exc)[:120]},
            )
            _admit = True
            _est = 0.0
        if not _admit:
            debug_trace.trace_event(
                store, subsystem="route", type="route.decided", actor="host_agent_runtime",
                status="gated", summary="admission_over_sla",
                detail={"mode": "blocked", "reason": "queue_over_sla",
                        "est_wait_sec": int(_est), "inflight": _inflight, "workers": _workers},
            )
            return {"error": "busy", "reason": "queue_over_sla", "est_wait_sec": int(_est)}, 503

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
    if not _v2_mode and not live:
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
    if has_file:
        extra["file_name"] = file_parse["name"]
        extra["file_mime"] = file_parse["mime"]
        if message:
            cap_env, cap_err = core_envelope._build_shared_envelope_for_store(
                store, message.encode("utf-8")
            )
            if cap_env:
                extra.update(chat_service._chat_caption_extra_from_envelope(cap_env))
            else:
                print(f"[model_api:{store.user_id}] file caption_envelope_failed detail={cap_err}")
    # Carry user-selected memory references (Garden「talk in chat」) onto the
    # turn so the enclave can expand them into the agent's context. Only ids are
    # stored (plaintext, non-sensitive); the enclave decrypts the memory body
    # itself on read. Covers both hosted and VPS resident replies — they share
    # the same consumer + enclave history path.
    quoted_memory_ids = [
        str(ref.get("id") or "").strip()
        for ref in context_refs
        if ref.get("type") == "memory" and str(ref.get("id") or "").strip()
    ]
    if quoted_memory_ids:
        extra["quoted_memory_ids"] = ",".join(quoted_memory_ids[:8])
    user_row = store.append_chat(
        "user",
        "model_api",
        user_env,
        content_type="image" if has_image else ("file" if has_file else "text"),
        extra=extra or None,
    )
    store.notify_chat_waiters()

    # image turn 不再被挡在 legacy；consumer 已能处理图片 envelope。
    _turn_id = str(user_row.get("id") or "") if isinstance(user_row, dict) else ""
    debug_trace.trace_event(
        store, subsystem="route", type="route.decided", actor="host_agent_runtime",
        turn_id=_turn_id, summary="agent_runtime",
        detail={"mode": "agent_runtime", "has_image": bool(has_image), "has_file": bool(has_file)},
    )
    if _v2_mode:
        # 落加密用户消息已完成（上方 append_chat）；入队/合并 chat job，唤醒 worker 池，
        # 快速返回 202 processing（不写 filler；客户端经 chat poll 取加密回复）。
        jobs_store.enqueue_job(
            store.user_id, "chat", reason="chat_send",
            trace_id=str(user_row.get("id") or "") if isinstance(user_row, dict) else None,
        )
        core_wake_bus.notify("v2_jobs", store.user_id)
        body, status = agent_runtime_cutover.build_processing_response(user_row, driver=driver)
        return body, status

    body, status = agent_runtime_cutover.handle_send(store, user_row, driver)
    return body, status
