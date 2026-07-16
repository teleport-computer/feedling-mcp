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

import db
import debug_trace
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
        _forced_mode = hosted_config_store.forced_hosted_runtime_mode()
        _forced_state = (
            "v2"
            if _forced_mode
            == hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
            else "resident"
        )
        if _forced_mode is not None and (
            _runtime_mode != _forced_mode or _runtime_state != _forced_state
        ):
            # Startup and setup materialize the environment policy. A request
            # must never repair ownership itself: doing so can race the
            # credential-delete fence and resurrect V2 after credentials have
            # disappeared. Refuse before persisting the user message; startup
            # reconciliation or a successful setup/test will repair the row.
            return {"error": "runtime_policy_not_ready"}, 503
        _v2_mode = (
            _runtime_mode
            == hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
        )
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503
    # The blob is the routing selector while v2_runtime_state is the
    # authoritative ownership fence.  A split tuple must not fall through to
    # either runtime: resident would eventually lose its reply fence and V2
    # would run without generation ownership.
    _expected_state = "v2" if _v2_mode else "resident"
    if _runtime_state != _expected_state:
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided",
            actor="host_agent_runtime", status="gated",
            summary="runtime_control_inconsistent",
            detail={"mode": "blocked", "reason": "runtime_control_inconsistent"},
        )
        return {"error": "runtime_control_inconsistent"}, 503

    # V2 liveness guard: db_action_v2 users skip the resident wedge guard further
    # down, and without this they'd have NO replacement — if every serve_worker
    # process is dead (crashed, not yet deployed, scaled to zero), enqueue_job
    # would still succeed and the message would queue in agent_jobs forever with
    # no error, no reply, no visible failure. jobs_store.workers_alive() reads the
    # v2_worker_heartbeats table each live serve_worker UPSERTs every ~10s.
    # Distinct code/summary ("workers_unavailable") from the resident wedge guard's
    # "hosting_runtime_unavailable"/"supervisor_unavailable" — dead worker pool vs.
    # dead resident supervisor are unrelated and must stay distinguishable.
    if _v2_mode and not jobs_store.workers_alive():
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
    if _v2_mode and kill_switch.turns_halted(default_on_error=True):
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided", actor="host_agent_runtime",
            status="gated", summary="turns_halted",
            detail={"mode": "blocked", "reason": "turns_halted"},
        )
        return {"error": "turns_halted"}, 503

    # §6 admission ceiling：存活闸已保证 ≥1 活 worker；再估排队等待，超 SLA 就在
    # persist 之前回独立 busy（区别于 workers_unavailable=供给死）。任何计算异常
    # fail-open（放行）——此闸绝不能自身变成故障源。
    if _v2_mode:
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
    if not _v2_mode:
        runtime = hosted_config_store._load_runtime_provider_config(
            store, api_key, runtime_token=runtime_tok,
        )
        if isinstance(runtime, tuple):
            _, err = runtime
            hosted_config_store._append_model_api_action_trace(store, {
                "status": "failed",
                "error": err.get("error", "runtime_load_failed"),
                "context": {"stage": "load_runtime"},
                "duration_ms": int((time.time() - trace_start) * 1000),
            })
            return err, 400
        hosted_config_store._ensure_model_api_runtime_profile(
            store, config, touch=True,
        )

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
    try:
        driver = agent_runtime_cutover.resolve_driver(config)
    except agent_runtime_cutover.UnsupportedProviderError:
        return {"error": "provider_not_configured"}, 409

    # Wedge guard: routing to the agent-runner only works if a supervisor is
    # actually hosting. assert_hosting_ready validated THIS process's env at
    # startup, but the consumer lives in a separate service — if its heartbeat is
    # missing/stale or its host-all/pi flags are off, this turn would park in
    # "processing" forever. Surface a clear 503 instead, BEFORE writing the user
    # message (so no orphan turn is left unanswered). Fail-open on a DB hiccup.
    #
    # Only gate on pi if this provider actually routes through the pi driver
    # (the in-CVM LiteLLM gateway is retired). anthropic (claude driver) and
    # openai (codex-native) must not be blocked by a pi-off heartbeat.
    _provider = str((config or {}).get("provider") or "")
    _require_pi = agent_runtime_cutover.driver_for_provider(_provider) == "pi"
    live, reason = agent_runtime_cutover.check_supervisor_live(require_pi=_require_pi)
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
    # A7: for the v2 send path, the message INSERT and its chat-job
    # enqueue/coalesce must land in ONE DB transaction, so a crash between
    # them can never orphan a persisted-but-never-enqueued message. The
    # generation is read ONCE, before the append, and the trace_id is derived
    # from the envelope's own id (the same value append_chat will use as the
    # stored message's msg_id) — both must be known before the call since the
    # enqueue now happens INSIDE store.append_chat via db.chat_append_and_enqueue,
    # not as a separate call afterward.
    _envelope_id = user_env.get("id")
    _trace_id = str(_envelope_id) if isinstance(_envelope_id, str) and _envelope_id else None
    try:
        user_row = store.append_chat(
            "user",
            "model_api",
            user_env,
            content_type="image" if has_image else ("file" if has_file else "text"),
            extra=extra or None,
            strict=_v2_mode,
            enqueue=(
                {
                    "lane": "chat",
                    "reason": "chat_send",
                    "trace_id": _trace_id,
                    "expected_generation": _generation,
                    "expected_runtime_state": "v2",
                    "expected_runtime_mode": (
                        hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
                    ),
                }
                if _v2_mode else None
            ),
        )
    except db.RuntimeControlChangedError:
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided",
            actor="host_agent_runtime", status="gated",
            summary="runtime_control_changed",
            detail={"mode": "blocked", "reason": "runtime_control_changed"},
        )
        return {"error": "runtime_control_changed"}, 503
    store.notify_chat_waiters()

    # image turn 不再被挡在 legacy；consumer 已能处理图片 envelope。
    _turn_id = str(user_row.get("id") or "") if isinstance(user_row, dict) else ""
    debug_trace.trace_event(
        store, subsystem="route", type="route.decided", actor="host_agent_runtime",
        turn_id=_turn_id, summary="agent_runtime",
        detail={"mode": "agent_runtime", "has_image": bool(has_image), "has_file": bool(has_file)},
    )
    if _v2_mode:
        # 落加密用户消息 + 入队/合并 chat job 已经在上方 append_chat 内一个事务里原子
        # 完成（db.chat_append_and_enqueue，spec A7）；这里只需唤醒 worker 池，快速
        # 返回 202 processing（不写 filler；客户端经 chat poll 取加密回复）。
        core_wake_bus.notify("v2_jobs", store.user_id)
        body, status = agent_runtime_cutover.build_processing_response(user_row, driver=driver)
        return body, status

    body, status = agent_runtime_cutover.handle_send(store, user_row, driver)
    return body, status
