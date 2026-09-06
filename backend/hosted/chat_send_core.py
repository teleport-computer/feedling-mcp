"""Framework-neutral body of ``POST /v1/model_api/chat/send`` (ASGI-migration).

The route logic — image parse, V2 ownership/liveness/admission checks, encrypted
message append + atomic job enqueue, and wake notification — with no framework
request/response object. Hosted model-API accounts have no resident fallback.

Every collaborator is referenced via its module (``agent_runtime_cutover.X``,
``hosted_config_store.X``, ``core_envelope.X`` …) so the existing tests that
monkeypatch those module attributes keep working unchanged.
"""

from __future__ import annotations

import sys
import time

from core import envelope as core_envelope
from core import chat_images
from core import wake_bus as core_wake_bus

import db
import debug_trace
from chat import idempotency as chat_idempotency
from chat import service as chat_service
from hosted import agent_runtime_cutover
from hosted import config_store as hosted_config_store
from hosted import context as hosted_context
from hosted import turn as hosted_turn
from hosted import vision_routing
from model_api_runtime.v2 import admission
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import kill_switch
from proactive import capture_scheduler


_CAPTION_ENVELOPE_FAILURE_EVENT = "chat.caption_envelope_failed"
_CAPTION_ENVELOPE_FAILURE_LOG_TYPE = "caption_envelope_failed"
_CAPTION_ENVELOPE_FAILURE_CODES = frozenset({
    "plaintext_body_not_utf8",
    "user_content_public_key_missing",
    "user_content_public_key_invalid_base64",
    "user_content_public_key_invalid_length",
    "enclave_info_unavailable",
    "enclave_content_public_key_invalid_hex",
    "enclave_content_public_key_invalid_length",
    "envelope_build_failed",
})


def _caption_failure_code(error: object) -> str:
    """Keep only the stable, content-free prefix of an envelope error."""
    code = str(error or "").strip().partition(":")[0]
    return code if code in _CAPTION_ENVELOPE_FAILURE_CODES else "unknown"


def _current_v2_chat_job_id(user_id: str) -> str:
    """Resolve the single-flight chat job that accepted the new turn."""
    try:
        rows = jobs_store.recent_jobs_for_user(
            user_id,
            within_hours=1,
            limit=20,
        ).get("jobs") or []
    except Exception:  # noqa: BLE001 - failure telemetry must stay fail-open
        return ""
    active = {"pending", "claimed", "running"}
    chat_rows = [row for row in rows if str(row.get("lane") or "") == "chat"]
    selected = next(
        (row for row in chat_rows if str(row.get("status") or "") in active),
        chat_rows[0] if chat_rows else None,
    )
    return str((selected or {}).get("job_id") or "")


def _emit_caption_envelope_failure(
    store,
    *,
    turn_id: str,
    job_id: str,
    error: object,
) -> None:
    """Expose a dropped image caption without blocking the image send.

    The trace and user-log writes are independent best-effort signals. Stderr
    is unconditional so a failure in both signal paths cannot make the turn
    indistinguishable from a caption that was preserved.
    """
    error_code = _caption_failure_code(error)
    signal_failures: list[str] = []
    try:
        debug_trace.trace_event(
            store,
            subsystem="route",
            type=_CAPTION_ENVELOPE_FAILURE_EVENT,
            actor="host_agent_runtime",
            status="error",
            outcome_class="operational_failure",
            summary="image caption envelope construction failed",
            explain=(
                "The image was accepted fail-open, but its accompanying caption "
                "was not persisted."
            ),
            trace_id=turn_id,
            turn_id=turn_id,
            job_id=job_id,
            detail={
                "attachment_kind": "image",
                "error_code": error_code,
                "caption_persisted": False,
            },
        )
    except Exception:  # noqa: BLE001 - stderr below is the independent fallback
        signal_failures.append("trace_events")

    now = time.time()
    tracking_event = {
        "event_id": f"caption-envelope-failed:{turn_id}",
        "type": _CAPTION_ENVELOPE_FAILURE_LOG_TYPE,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "source": "backend",
        "route": "/v1/model_api/chat/send",
        "payload": {
            "turn_id": turn_id,
            "job_id": job_id,
            "attachment_kind": "image",
            "outcome_class": "operational_failure",
            "error_code": error_code,
            "caption_persisted": False,
        },
    }
    try:
        stored = db.log_append(
            store.user_id,
            "tracking_events",
            tracking_event,
            ts=now,
            item_key=tracking_event["event_id"],
        )
    except Exception:  # noqa: BLE001 - stderr below is the independent fallback
        stored = False
    if not stored:
        signal_failures.append("user_logs")

    print(
        f"[model_api:{store.user_id}] caption_envelope_failed "
        f"turn_id={turn_id or '-'} job_id={job_id or '-'} "
        f"error_code={error_code} "
        f"signal_failures={','.join(signal_failures) or 'none'}",
        file=sys.stderr,
        flush=True,
    )


def _voice_metadata(voice_context: dict | None) -> dict[str, str]:
    if not isinstance(voice_context, dict):
        return {}
    call_id = str(voice_context.get("call_id") or "").strip()
    turn_id = str(voice_context.get("turn_id") or "").strip()
    if not call_id or not turn_id or len(call_id) > 96 or len(turn_id) > 128:
        return {}
    metadata = {"voice_call_id": call_id, "voice_turn_id": turn_id}
    logical_turn_id = str(
        voice_context.get("logical_turn_id") or ""
    ).strip()
    if logical_turn_id and len(logical_turn_id) <= 128:
        metadata.update({
            "voice_logical_turn_id": logical_turn_id,
            "voice_turn_status": "current",
        })
    return metadata


def model_api_chat_send_core(
    store,
    *,
    api_key: str | None,
    runtime_tok: str,
    payload: dict,
    voice_context: dict | None = None,
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
    parsed_images, images_err = hosted_turn._model_api_images_payload(payload)
    if images_err:
        return images_err, 400
    image_bytes, image_mime, image_err = hosted_turn._model_api_image_payload(payload)
    if image_err:
        return {"error": "invalid_image", "detail": image_err}, 400
    image_items: list[tuple[bytes, str]] = []
    if parsed_images is not None:
        image_items = parsed_images
        image_bytes, image_mime = parsed_images[0]
    elif image_bytes is not None:
        image_items = [(image_bytes, image_mime)]
    has_image = image_bytes is not None
    file_parse, file_err = hosted_turn._model_api_file_payload(payload)
    if file_err:
        return file_err  # (body, status) already shaped
    # An image sent through the file picker re-pipes into the image path so it
    # gets vision — reuse the exact image envelope/append below.
    if file_parse is not None and file_parse["kind"] == "image":
        image_bytes = file_parse["bytes"]
        image_mime = file_parse["mime"]
        image_items = [(image_bytes, image_mime)]
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
        _policy = hosted_config_store.hosted_runtime_policy()
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503

    # Three-state per-user dispatch. Startup/setup materialize ownership; a
    # request never repairs it (that could race credential deletion). The
    # authoritative (mode, state) tuple selects the runtime:
    #   * db_action_v2 + v2       -> the V2 worker-pool path (below)
    #   * resident_cli + resident -> the restored resident consumer path
    #   * *, draining             -> mid-switch, refuse cleanly
    #   * anything else           -> a split/illegal tuple, fail closed
    # Under the ``v2_only`` policy only the exact V2 tuple is honored (the
    # retirement-era contract, P7); everything else is runtime_policy_not_ready.
    _v2_tuple = (
        _runtime_mode == hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
        and _runtime_state == "v2"
    )
    _resident_tuple = (
        _runtime_mode == hosted_config_store.HOSTED_RUNTIME_MODE_RESIDENT
        and _runtime_state == "resident"
    )

    if _policy == hosted_config_store.HOSTED_RUNTIME_POLICY_V2_ONLY:
        if not _v2_tuple:
            # Any dormant, stale resident, or split tuple fails closed before the
            # user message is persisted; startup reconciliation repairs the row.
            debug_trace.trace_event(
                store, subsystem="route", type="route.decided",
                actor="host_agent_runtime", status="gated",
                summary="runtime_policy_not_ready",
                detail={"mode": "blocked", "reason": "runtime_policy_not_ready"},
            )
            return {"error": "runtime_policy_not_ready"}, 503
        # else: fall through to the V2 path below (unchanged behavior).
    else:  # dual
        # External readers rarely observe "draining" — the resident/v2 <->
        # draining <-> target transition commits atomically (patch_blob_strict),
        # so this window is narrow by construction. The guard below is
        # defensive fail-closed cover for that narrow window, not dead code.
        if _runtime_state == "draining":
            debug_trace.trace_event(
                store, subsystem="route", type="route.decided",
                actor="host_agent_runtime", status="gated",
                summary="runtime_switching",
                detail={"mode": "blocked", "reason": "runtime_switching"},
            )
            return {"error": "runtime_switching"}, 503
        if _v2_tuple:
            pass  # fall through to the V2 path below.
        elif _resident_tuple:
            return _send_resident(
                store,
                api_key=api_key,
                runtime_tok=runtime_tok,
                message=message,
                has_image=has_image,
                image_bytes=image_bytes,
                image_mime=image_mime,
                image_items=image_items,
                has_file=has_file,
                file_parse=file_parse,
                context_refs=context_refs,
                client_msg_id=client_msg_id,
                voice_context=voice_context,
            )
        else:
            debug_trace.trace_event(
                store, subsystem="route", type="route.decided",
                actor="host_agent_runtime", status="gated",
                summary="runtime_control_invalid",
                detail={"mode": "blocked", "reason": "runtime_control_invalid"},
            )
            return {"error": "runtime_control_invalid"}, 503

    include_reasoning = payload.get("include_reasoning", False)
    if type(include_reasoning) is not bool:
        return {
            "error": "invalid_include_reasoning",
            "detail": "include_reasoning must be a boolean",
        }, 400

    # Resolve and pin V2 image routing before persistence so a later Settings
    # change cannot redirect already-accepted pixels.
    vision_route_id = ""
    if has_image:
        vision_route, vision_error = vision_routing.dedicated_route_for_send(store)
        if vision_error is not None:
            return vision_error
        if vision_route is not None:
            vision_route_id = str(vision_route.get("id") or "")

    # V2 liveness guard: if every serve_worker
    # process is dead (crashed, not yet deployed, scaled to zero), enqueue_job
    # would still succeed and the message would queue in agent_jobs forever with
    # no error, no reply, no visible failure. jobs_store.workers_alive() reads the
    # v2_worker_heartbeats table each live serve_worker UPSERTs every ~10s.
    # ``workers_unavailable`` is the sole managed-host liveness error; no
    # resident supervisor is consulted or offered as a fallback.
    if not jobs_store.workers_alive(pool="foreground"):
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

    # §6 admission telemetry：存活闸已保证 ≥1 活 worker；继续估算排队等待，
    # 但容量过载不能拒绝用户输入。V2 的原子 append+enqueue 已经能持久化并合并
    # 同一用户的新输入；在它之前返回 busy 会让消息只留在客户端输入框里，使
    # 后端的 chat-priority / wake-preemption 闸完全看不见这次用户发言。
    #
    # 控制面、worker liveness、kill switch 等 fail-closed 闸仍在上方；这里只把
    # live-but-overloaded 从 admission decision 降为可观测信号。任何估算异常也继续
    # fail-open（放行）——telemetry 绝不能自身变成故障源。
    _inflight = _workers = 0
    try:
        _workers = jobs_store.live_worker_capacity(
            within_sec=30, pool="foreground"
        )
        _inflight = jobs_store.inflight_job_count(
            lanes={"chat", "manual_wake"}
        )
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
            detail={"mode": "admit", "reason": "admission_unavailable",
                    "error_class": type(exc).__name__},
        )
        _admit = True
        _est = 0.0
    if not _admit:
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided", actor="host_agent_runtime",
            status="ok", summary="admission_over_sla",
            detail={"mode": "admit", "reason": "queue_over_sla",
                    "est_wait_sec": int(_est), "inflight": _inflight, "workers": _workers},
        )
    # ---- end V2 gates ---------------------------------------------------------

    config = hosted_config_store._load_model_api_config(store)

    if len(image_items) >= 2:
        image_items = [
            (body, "image/jpeg" if mime.lower() == "image/jpg" else mime.lower())
            for body, mime in image_items
        ]
        user_plaintext = chat_images.encode_image_bundle(image_items)
    elif has_image:
        user_plaintext = image_bytes
    elif has_file:
        user_plaintext = file_parse["bytes"]
    else:
        user_plaintext = message.encode("utf-8")
    user_env, env_err = core_envelope._build_shared_envelope_for_store(
        store,
        user_plaintext,
        content_kind="binary" if (has_image or has_file) else "text",
    )
    if user_env is None:
        return {"error": "user_message_envelope_failed", "detail": env_err}, 409
    # A supported provider enters the unified V2 loop; reject before persistence
    # when no provider mapping exists.
    # 先校验 driver 再入 store，避免未配置时写入孤儿用户消息。
    try:
        driver = agent_runtime_cutover.resolve_driver(config)
    except agent_runtime_cutover.UnsupportedProviderError:
        return {"error": "provider_not_configured"}, 409

    extra: dict = _voice_metadata(voice_context)
    # V2-only per-turn routing metadata. Resident/VPS paths never consume or
    # persist this field; old clients omit it and keep the historical false path.
    extra["include_reasoning"] = include_reasoning
    if client_msg_id is not None:
        # Plain routing metadata only; the message body remains ciphertext.
        # The database uses this UUID to serialize iOS transport retries across
        # every backend process and both chat-send endpoints.
        extra["client_msg_id"] = client_msg_id
    if has_image and image_mime:
        extra["image_mime"] = image_mime
    if len(image_items) >= 2:
        extra.update({
            "image_bundle_version": chat_images.CHAT_IMAGE_BUNDLE_VERSION,
            "image_count": len(image_items),
            "image_mimes": [mime for _body, mime in image_items],
            "image_byte_count": sum(len(body) for body, _mime in image_items),
        })
    if has_image and vision_route_id:
        extra["vision_route_id"] = vision_route_id
    caption_failure = ""
    if has_image and message:
        # 带文字说明的图片：独立加密 caption，enclave history 解后填 content。
        caption_env, caption_err = core_envelope._build_shared_envelope_for_store(
            store, message.encode("utf-8")
        )
        if caption_env:
            extra.update(chat_service._chat_caption_extra_from_envelope(caption_env))
        else:
            caption_failure = caption_err
    if has_file:
        extra["file_name"] = file_parse["name"]
        extra["file_mime"] = file_parse["mime"]
        for key in ("file_display_title", "file_display_subtitle"):
            if file_parse.get(key):
                extra[key] = file_parse[key]
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
    chat_append_started = time.monotonic()
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
    chat_append_duration_ms = round(
        (time.monotonic() - chat_append_started) * 1000.0, 1
    )
    inserted = not bool(user_row.pop("_client_msg_replayed", False))
    if inserted:
        store.notify_chat_waiters()

    # image turn 不再被挡在 legacy；consumer 已能处理图片 envelope。
    _turn_id = str(user_row.get("id") or "") if isinstance(user_row, dict) else ""
    if inserted and caption_failure:
        _emit_caption_envelope_failure(
            store,
            turn_id=_turn_id,
            job_id=_current_v2_chat_job_id(store.user_id),
            error=caption_failure,
        )
    if inserted:
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided", actor="host_agent_runtime",
            turn_id=_turn_id, summary="agent_runtime",
            detail={
                "mode": "agent_runtime",
                "has_image": bool(has_image),
                "has_file": bool(has_file),
                "chat_append_duration_ms": chat_append_duration_ms,
                "capture_append_refresh_mode": (
                    "deferred"
                    if capture_scheduler.append_refresh_deferred()
                    else "sync"
                ),
            },
        )
    else:
        # The assistant reply may have been posted through another worker since
        # this store's last wake refresh. Apply only its durable change events.
        store.ensure_chat_fresh(force=True)
    if inserted:
        core_wake_bus.notify("v2_jobs", store.user_id)
    body, status = agent_runtime_cutover.build_processing_response(user_row, driver=driver)
    return body, status


def _send_resident(
    store,
    *,
    api_key: str | None,
    runtime_tok: str,
    message: str,
    has_image: bool,
    image_bytes,
    image_mime,
    image_items,
    has_file: bool,
    file_parse,
    context_refs,
    client_msg_id,
    voice_context: dict | None = None,
) -> tuple[dict, int]:
    """Restored V1 resident send path (dual policy, ``resident_cli``/``resident``).

    Validates the runtime provider (400 + action trace on failure), builds the
    user-message envelope (409 on failure), resolves the wire-compat driver (409
    when unconfigured), then gates on the resident supervisor wedge — a dead/stale
    supervisor 503s ``hosting_runtime_unavailable`` BEFORE any append, so no orphan
    turn is left unanswered (fail-open on a DB hiccup inside ``check_supervisor_live``).
    On a live supervisor it appends the user message (client-msg-id idempotent when
    present), wakes chat waiters, and hands off to
    ``agent_runtime_cutover.handle_send`` for the 202 processing/ready reply.

    Restored verbatim from ``git show 2b294a1f^:backend/hosted/chat_send_core.py``
    (the ``not _v2_mode`` branch), including the historical wedge body
    ``{"error": "hosting_runtime_unavailable", "reason": reason}``; the debug-trace
    ``summary`` stays ``supervisor_unavailable`` (that is how the two were split
    historically).
    """
    vision_route_id = ""
    if has_image:
        vision_route, vision_error = vision_routing.dedicated_route_for_send(store)
        if vision_error is not None:
            return vision_error
        if vision_route is not None:
            vision_route_id = str(vision_route.get("id") or "")

    trace_start = time.time()
    config = hosted_config_store._load_model_api_config(store)
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
    hosted_config_store._ensure_model_api_runtime_profile(store, config, touch=True)

    # Hosted V1 learns visual capability only from the exact provider failure.
    # Capture the main route/version as inert metadata on this accepted image;
    # the terminal reply transaction later consumes it with a strict CAS. A
    # missing/racing binding never blocks or reroutes the send.
    main_vision_binding = None
    if has_image and not vision_route_id:
        candidate = db.model_api_active_route_version(store.user_id)
        if isinstance(candidate, dict):
            runtime_provider = str(getattr(runtime, "provider", "") or "")
            runtime_model = str(getattr(runtime, "model", "") or "")
            runtime_base_url = str(
                getattr(runtime, "base_url", "") or ""
            ).rstrip("/")
            candidate_base_url = str(
                candidate.get("base_url") or ""
            ).rstrip("/")
            if (
                str(candidate.get("provider") or "") == runtime_provider
                and (
                    not runtime_model
                    or str(candidate.get("model") or "") == runtime_model
                )
                and (
                    not runtime_base_url
                    or not candidate_base_url
                    or candidate_base_url == runtime_base_url
                )
            ):
                main_vision_binding = candidate

    if len(image_items) >= 2:
        image_items = [
            (body, "image/jpeg" if mime.lower() == "image/jpg" else mime.lower())
            for body, mime in image_items
        ]
        user_plaintext = chat_images.encode_image_bundle(image_items)
    elif has_image:
        user_plaintext = image_bytes
    elif has_file:
        user_plaintext = file_parse["bytes"]
    else:
        user_plaintext = message.encode("utf-8")
    user_env, env_err = core_envelope._build_shared_envelope_for_store(
        store,
        user_plaintext,
        content_kind="binary" if (has_image or has_file) else "text",
    )
    if user_env is None:
        return {"error": "user_message_envelope_failed", "detail": env_err}, 409
    # 收口：配了 fit provider 即托管到 agent-runner，否则 409。
    # 先校验 driver 再入 store，避免未配置时写入孤儿用户消息。
    try:
        driver = agent_runtime_cutover.resolve_driver(config)
    except agent_runtime_cutover.UnsupportedProviderError:
        return {"error": "provider_not_configured"}, 409

    # Wedge guard: routing to the agent-runner only works if a supervisor is
    # actually hosting. If its heartbeat is missing/stale or its host-all/pi flags
    # are off, this turn would park in "processing" forever. Surface a clear 503
    # instead, BEFORE writing the user message (so no orphan turn is left
    # unanswered). Fail-open on a DB hiccup. Only gate on pi if this provider
    # actually routes through the pi driver.
    _provider = str((config or {}).get("provider") or "")
    _require_pi = agent_runtime_cutover.driver_for_provider(_provider) == "pi"
    live, reason = agent_runtime_cutover.check_supervisor_live(require_pi=_require_pi)
    if not live:
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided", actor="host_agent_runtime",
            status="gated", summary="supervisor_unavailable",
            detail={"mode": "blocked", "reason": "supervisor_unavailable",
                    "live_reason": str(reason or "")[:80]},
        )
        return {"error": "hosting_runtime_unavailable", "reason": reason}, 503

    extra: dict = _voice_metadata(voice_context)
    if has_image and image_mime:
        extra["image_mime"] = image_mime
    if len(image_items) >= 2:
        extra.update({
            "image_bundle_version": chat_images.CHAT_IMAGE_BUNDLE_VERSION,
            "image_count": len(image_items),
            "image_mimes": [mime for _body, mime in image_items],
            "image_byte_count": sum(len(body) for body, _mime in image_items),
        })
    if has_image and vision_route_id:
        extra["vision_route_id"] = vision_route_id
    if has_image and main_vision_binding is not None:
        extra["vision_main_route_id"] = str(
            main_vision_binding.get("route_id") or ""
        )
        extra["vision_main_route_updated_at"] = str(
            main_vision_binding.get("updated_at_token") or ""
        )
    caption_failure = ""
    if has_image and message:
        # 带文字说明的图片：独立加密 caption，enclave history 解后填 content。
        caption_env, caption_err = core_envelope._build_shared_envelope_for_store(
            store, message.encode("utf-8")
        )
        if caption_env:
            extra.update(chat_service._chat_caption_extra_from_envelope(caption_env))
        else:
            caption_failure = caption_err
    if has_file:
        extra["file_name"] = file_parse["name"]
        extra["file_mime"] = file_parse["mime"]
        for key in ("file_display_title", "file_display_subtitle"):
            if file_parse.get(key):
                extra[key] = file_parse[key]
        if message:
            cap_env, cap_err = core_envelope._build_shared_envelope_for_store(
                store, message.encode("utf-8")
            )
            if cap_env:
                extra.update(chat_service._chat_caption_extra_from_envelope(cap_env))
            else:
                print(f"[model_api:{store.user_id}] file caption_envelope_failed detail={cap_err}")
    # Carry user-selected memory references onto the turn so the enclave can
    # expand them into the agent's context (ids only; the enclave decrypts the
    # memory body itself on read).
    quoted_memory_ids = [
        str(ref.get("id") or "").strip()
        for ref in context_refs
        if ref.get("type") == "memory" and str(ref.get("id") or "").strip()
    ]
    if quoted_memory_ids:
        extra["quoted_memory_ids"] = ",".join(quoted_memory_ids[:8])

    # Resident send with client-msg-id dedup: a re-sent client_msg_id recovers
    # the original row instead of double-inserting.
    inserted = True
    chat_append_started = time.monotonic()
    if client_msg_id is not None:
        user_row, inserted = store.append_chat_idempotent(
            "user",
            "model_api",
            user_env,
            client_msg_id=client_msg_id,
            window_sec=chat_idempotency.CLIENT_MSG_ID_WINDOW_SEC,
            content_type="image" if has_image else ("file" if has_file else "text"),
            extra=extra or None,
        )
    else:
        user_row = store.append_chat(
            "user",
            "model_api",
            user_env,
            content_type="image" if has_image else ("file" if has_file else "text"),
            extra=extra or None,
        )
    chat_append_duration_ms = round(
        (time.monotonic() - chat_append_started) * 1000.0, 1
    )
    if inserted:
        store.notify_chat_waiters()

    _turn_id = str(user_row.get("id") or "") if isinstance(user_row, dict) else ""
    if inserted and caption_failure:
        _emit_caption_envelope_failure(
            store,
            turn_id=_turn_id,
            job_id="",
            error=caption_failure,
        )
    if inserted:
        debug_trace.trace_event(
            store, subsystem="route", type="route.decided", actor="host_agent_runtime",
            turn_id=_turn_id, summary="agent_runtime",
            detail={
                "mode": "agent_runtime",
                "has_image": bool(has_image),
                "has_file": bool(has_file),
                "chat_append_duration_ms": chat_append_duration_ms,
                "capture_append_refresh_mode": (
                    "deferred"
                    if capture_scheduler.append_refresh_deferred()
                    else "sync"
                ),
            },
        )
    else:
        # The assistant reply may have been posted through another worker since
        # this store's last wake refresh. Apply only its durable change events.
        store.ensure_chat_fresh(force=True)

    body, status = agent_runtime_cutover.handle_send(store, user_row, driver)
    return body, status
