"""Framework-neutral body of ``POST /v1/model_api/chat/send`` (ASGI-migration).

The route logic — image parse, V2 ownership/liveness/admission checks, encrypted
message append + atomic job enqueue, and wake notification — with no framework
request/response object. Hosted model-API accounts have no resident fallback.

Every collaborator is referenced via its module (``agent_runtime_cutover.X``,
``hosted_config_store.X``, ``core_envelope.X`` …) so the existing tests that
monkeypatch those module attributes keep working unchanged.
"""

from __future__ import annotations

from core import envelope as core_envelope
from core import wake_bus as core_wake_bus

import db
import debug_trace
from chat import idempotency as chat_idempotency
from chat import service as chat_service
from hosted import agent_runtime_cutover
from hosted import config_store as hosted_config_store
from hosted import context as hosted_context
from hosted import turn as hosted_turn
from model_api_runtime.v2 import admission
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import kill_switch


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
    client_msg_id, client_msg_id_err = chat_idempotency.parse_client_msg_id(payload)
    if client_msg_id_err is not None:
        return client_msg_id_err
    image_bytes, image_mime, image_err = hosted_turn._model_api_image_payload(payload)
    if image_err:
        return {"error": "invalid_image", "detail": image_err}, 400
    has_image = image_bytes is not None
    file_parse, file_err = hosted_turn._model_api_file_payload(payload)
    if file_err:
        return file_err  # (body, status) already shaped
    # An image sent through the file picker re-pipes into the image path so it
    # gets vision — reuse the exact image envelope/append below.
    if file_parse is not None and file_parse["kind"] == "image":
        image_bytes = file_parse["bytes"]
        image_mime = file_parse["mime"]
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

    # ---- Hosted Runtime V2 gates: no send-time provider decrypt -----------------
    # V2 claims immediately after cheap durable control/capacity checks. The worker
    # resolves and decrypts BYOK at turn start, where any failure becomes a durable
    # terminal job error. Repeating that uncached enclave round-trip here delayed
    # enqueue/notify and could reject the message before the no-wedge job machinery
    # owned it. Resident remains unchanged and validates its provider before send.
    try:
        _runtime_mode, _runtime_state, _generation = (
            hosted_config_store.get_hosted_runtime_control_strict(store)
        )
        hosted_config_store.hosted_runtime_policy()
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503
    # Startup/setup materialize V2 ownership. A request never repairs it: doing
    # so can race credential deletion. Any dormant, stale resident, or split
    # tuple fails closed before the user message is persisted.
    if (
        _runtime_mode
        != hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
        or _runtime_state != "v2"
    ):
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided",
            actor="host_agent_runtime", status="gated",
            summary="runtime_policy_not_ready",
            detail={"mode": "blocked", "reason": "runtime_policy_not_ready"},
        )
        return {"error": "runtime_policy_not_ready"}, 503

    # V2 liveness guard: if every serve_worker
    # process is dead (crashed, not yet deployed, scaled to zero), enqueue_job
    # would still succeed and the message would queue in agent_jobs forever with
    # no error, no reply, no visible failure. jobs_store.workers_alive() reads the
    # v2_worker_heartbeats table each live serve_worker UPSERTs every ~10s.
    # ``workers_unavailable`` is the sole managed-host liveness error; no
    # resident supervisor is consulted or offered as a fallback.
    if not jobs_store.workers_alive():
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided", actor="host_agent_runtime",
            status="gated", summary="workers_unavailable",
            detail={"mode": "blocked", "reason": "workers_unavailable"},
        )
        return {"error": "workers_unavailable", "reason": "no_live_v2_worker_heartbeat"}, 503

    # D4 live kill switch: fail-CLOSED (default_on_error=True) — unlike workers_alive
    # above (which answers "is anyone home"), this answers "has an operator asked the
    # pool to stop", and a control-plane read failure must NOT be treated as "no, keep
    # admitting" into a pool that may in fact be halted. Live-flippable without redeploy
    # via kill_switch.set_turns_halted; Genesis is a separate table/thread and never
    # consults this gate.
    if kill_switch.turns_halted(default_on_error=True):
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided", actor="host_agent_runtime",
            status="gated", summary="turns_halted",
            detail={"mode": "blocked", "reason": "turns_halted"},
        )
        return {"error": "turns_halted"}, 503

    # §6 admission ceiling：存活闸已保证 ≥1 活 worker；再估排队等待，超 SLA 就在
    # persist 之前回独立 busy（区别于 workers_unavailable=供给死）。任何计算异常
    # fail-open（放行）——此闸绝不能自身变成故障源。
    _inflight = _workers = 0
    try:
        _workers = jobs_store.live_worker_capacity(within_sec=30)
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
    # ---- end V2 gates ---------------------------------------------------------

    config = hosted_config_store._load_model_api_config(store)

    if has_image:
        user_plaintext = image_bytes
    elif has_file:
        user_plaintext = file_parse["bytes"]
    else:
        user_plaintext = message.encode("utf-8")
    user_env, env_err = core_envelope._build_shared_envelope_for_store(store, user_plaintext)
    if user_env is None:
        return {"error": "user_message_envelope_failed", "detail": env_err}, 409
    # A supported provider enters the unified V2 loop; reject before persistence
    # when no provider mapping exists.
    # 先校验 driver 再入 store，避免未配置时写入孤儿用户消息。
    try:
        driver = agent_runtime_cutover.resolve_driver(config)
    except agent_runtime_cutover.UnsupportedProviderError:
        return {"error": "provider_not_configured"}, 409

    extra: dict = {}
    if client_msg_id is not None:
        # Plain routing metadata only; the message body remains ciphertext.
        # The database uses this UUID to serialize iOS transport retries across
        # every backend process and both chat-send endpoints.
        extra["client_msg_id"] = client_msg_id
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
    # Message INSERT and chat-job enqueue/coalesce land in one transaction.
    _envelope_id = user_env.get("id")
    _trace_id = str(_envelope_id) if isinstance(_envelope_id, str) and _envelope_id else None
    try:
        user_row = store.append_chat(
            "user",
            "model_api",
            user_env,
            content_type="image" if has_image else ("file" if has_file else "text"),
            extra=extra or None,
            strict=True,
            enqueue={
                "lane": "chat",
                "reason": "chat_send",
                "trace_id": _trace_id,
                "expected_generation": _generation,
                "expected_runtime_state": "v2",
                "expected_runtime_mode": (
                    hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
                ),
                "client_msg_id": client_msg_id,
                "idempotency_window_sec": (
                    chat_idempotency.CLIENT_MSG_ID_WINDOW_SEC
                    if client_msg_id is not None
                    else None
                ),
            },
        )
    except db.RuntimeControlChangedError:
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided",
            actor="host_agent_runtime", status="gated",
            summary="runtime_control_changed",
            detail={"mode": "blocked", "reason": "runtime_control_changed"},
        )
        return {"error": "runtime_control_changed"}, 503
    inserted = not bool(user_row.pop("_client_msg_replayed", False))
    if inserted:
        store.notify_chat_waiters()

    # image turn 不再被挡在 legacy；consumer 已能处理图片 envelope。
    _turn_id = str(user_row.get("id") or "") if isinstance(user_row, dict) else ""
    if inserted:
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided", actor="host_agent_runtime",
            turn_id=_turn_id, summary="agent_runtime",
            detail={"mode": "agent_runtime", "has_image": bool(has_image), "has_file": bool(has_file)},
        )
    else:
        # The assistant reply may have been posted through another worker since
        # this store's last wake refresh. Reload before the existing response
        # builder checks for it; this does not notify or re-run the turn.
        store.reload()
    if inserted:
        core_wake_bus.notify("v2_jobs", store.user_id)
    body, status = agent_runtime_cutover.build_processing_response(user_row, driver=driver)
    return body, status
