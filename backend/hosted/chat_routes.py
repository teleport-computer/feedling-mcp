"""Hosted chat: /v1/model_api/chat/send."""

import base64
import copy
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from flask import Blueprint, Response, jsonify, request, g

import db
from chat import service as chat_service
from core import envelope as core_envelope
from flask import current_app
from core.store import UserStore

from hosted_runtime import (
    ACTION_RESPONSE_FORMAT as HOSTED_RUNTIME_ACTION_RESPONSE_FORMAT,
    ACTION_METHOD as HOSTED_RUNTIME_ACTION_METHOD,
    BACKGROUND_METHOD as HOSTED_RUNTIME_BACKGROUND_METHOD,
    BACKGROUND_NOT_STARTED_METHOD as HOSTED_RUNTIME_BACKGROUND_NOT_STARTED_METHOD,
    NOOP_METHOD as HOSTED_RUNTIME_NOOP_METHOD,
    PENDING_CONFIRM_METHOD as HOSTED_RUNTIME_PENDING_CONFIRM_METHOD,
    PENDING_REJECT_METHOD as HOSTED_RUNTIME_PENDING_REJECT_METHOD,
    RUNTIME_ENGINE_NATIVE as HOSTED_RUNTIME_ENGINE_NATIVE,
    build_background_execution_messages as build_hosted_runtime_background_execution_messages,
    background_execution_trace as hosted_runtime_background_trace,
    companion_turn_contract_message as hosted_runtime_companion_turn_contract_message,
    coerce_pending_decision as coerce_hosted_runtime_pending_decision,
    coerce_runtime_action as coerce_hosted_runtime_action,
)
from model_api_runtime.prompts import (
    build_foreground_chat_messages as build_model_api_foreground_chat_messages,
    build_memory_capture_messages as build_model_api_memory_capture_messages,
    build_pending_confirmation_messages as build_model_api_pending_confirmation_messages,
    build_web_search_results_message as build_model_api_web_search_results_message,
    web_search_followup_message as model_api_web_search_followup_message,
)
from model_api_runtime.tools import (
    extract_web_search_requests as extract_model_api_web_search_requests,
    run_web_searches as run_model_api_web_searches,
    web_search_trace as model_api_web_search_trace,
)
from model_api_runtime.wake import (
    wake_turn_contract_message as model_api_wake_turn_contract_message,
    build_wake_event_message as build_model_api_wake_event_message,
    hosted_tick_trigger as model_api_hosted_tick_trigger,
    parse_wake_actions as parse_model_api_wake_actions,
)
from context_memory_selection import memory_relevance_details
from content_encryption import build_envelope

from accounts import auth
from push import service as push_service
import provider_client
from hosted import config_store as hosted_config_store
from hosted import context as hosted_context
from hosted import turn as hosted_turn


bp = Blueprint("hosted_chat_routes", __name__)

@bp.route("/v1/model_api/chat/send", methods=["POST"])
def model_api_chat_send():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    trace_start = time.time()
    payload = request.get_json(silent=True) or {}
    image_bytes, image_mime, image_err = hosted_turn._model_api_image_payload(payload)
    if image_err:
        return jsonify({"error": "invalid_image", "detail": image_err}), 400
    image_b64 = base64.b64encode(image_bytes).decode("ascii") if image_bytes else ""
    has_image = image_bytes is not None
    message = str(payload.get("message") or payload.get("content") or "").strip()
    message_for_context = message or ("User sent an image." if has_image else "")
    context_refs = hosted_context._context_refs_from_payload(payload)
    if not message_for_context:
        return jsonify({"error": "message required"}), 400
    if len(message) > 12000:
        return jsonify({"error": "message too long", "max_chars": 12000}), 413

    runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        _, err = runtime
        hosted_config_store._append_model_api_action_trace(store, {
            "status": "failed",
            "error": err.get("error", "runtime_load_failed"),
            "context": {"stage": "load_runtime"},
            "duration_ms": int((time.time() - trace_start) * 1000),
        })
        return jsonify(err), 400
    hosted_config_store._ensure_model_api_runtime_profile(store, hosted_config_store._load_model_api_config(store), touch=True)

    user_plaintext = image_bytes if image_bytes is not None else message.encode("utf-8")
    user_env, env_err = core_envelope._build_shared_envelope_for_store(store, user_plaintext)
    if user_env is None:
        return jsonify({"error": "user_message_envelope_failed", "detail": env_err}), 409
    user_row = store.append_chat(
        "user",
        "model_api",
        user_env,
        content_type="image" if has_image else "text",
    )
    store.notify_chat_waiters()

    effects: list[dict] = []
    identity_action_results: list[dict] = []
    memory_action_results: list[dict] = []

    provider_messages, context_payload, screen_images = hosted_context._model_api_context_messages(
        store,
        api_key,
        message_for_context,
        include_screen_context=bool(payload.get("include_screen_context")),
    )
    provider_images = list(screen_images)
    if has_image:
        provider_images.append({"mime": image_mime, "b64": image_b64, "label": "user_upload"})
    if provider_images:
        user_content = hosted_turn._model_api_user_content(message_for_context, provider_images)
        for idx in range(len(provider_messages) - 1, -1, -1):
            if provider_messages[idx].get("role") == "user":
                provider_messages[idx]["content"] = user_content
                break
        else:
            provider_messages.append({"role": "user", "content": user_content})
    if context_refs:
        context_payload["context_refs"] = context_refs
        provider_messages.insert(2, {
            "role": "system",
            "content": "User-selected context refs JSON:\n" + json.dumps(context_refs, ensure_ascii=False)[:3000],
        })
    provider_messages.insert(2, hosted_turn._model_api_turn_contract_message())
    web_search: dict = {}
    try:
        result = provider_client.chat_completion(
            runtime,
            provider_messages,
            # Thinking/reasoning models share this budget between reasoning and
            # output tokens, so keep it generous; non-thinking models stop early
            # on their own and don't pay for the headroom.
            max_tokens=int(payload.get("max_tokens") or 2048),
            temperature=float(payload.get("temperature") or 0.7),
            timeout=90.0,
            include_reasoning=hosted_turn.MODEL_API_PROVIDER_REASONING_ENABLED,
        )
    except provider_client.ProviderError as e:
        background_execution = hosted_runtime_background_trace(
            status="not_started",
            method=HOSTED_RUNTIME_BACKGROUND_NOT_STARTED_METHOD,
        )
        trace = hosted_config_store._append_model_api_action_trace(store, {
            "status": "failed",
            "provider": runtime.provider,
            "model": runtime.model,
            "user_message_id": user_row["id"],
            "background_execution": background_execution,
            "effects": effects,
            "identity_actions": identity_action_results,
            "memory_actions": memory_action_results,
            "context": hosted_context._model_api_context_trace_for_action(
                context_payload,
                context_refs=context_refs,
                web_search=web_search,
            ),
            "error": f"provider_chat_failed:{str(e)[:220]}",
            "duration_ms": int((time.time() - trace_start) * 1000),
        })
        return jsonify({
            "error": "provider_chat_failed",
            "detail": str(e),
            "status_code": e.status_code,
            "user_message_id": user_row["id"],
            "action_trace_id": trace.get("trace_id", ""),
        }), 502

    raw_reply = str(result.get("reply") or "").strip()
    reply, thinking_summary, requested_web_search = hosted_turn._model_api_parse_turn_output(raw_reply)
    provider_reasoning = hosted_turn._sanitize_provider_reasoning_text(str(result.get("reasoning") or ""))
    if requested_web_search and (not web_search or not reply):
        if not web_search:
            web_search = hosted_turn._run_model_api_web_searches(requested_web_search)
            context_payload["web_search"] = hosted_turn._model_api_web_search_trace(web_search)
        final_messages = list(provider_messages)
        final_messages.append({"role": "assistant", "content": raw_reply[:4000]})
        final_messages.append(hosted_turn._model_api_web_search_results_message(web_search))
        final_messages.append(model_api_web_search_followup_message())
        try:
            final_result = provider_client.chat_completion(
                runtime,
                final_messages,
                max_tokens=int(payload.get("max_tokens") or 2048),
                temperature=float(payload.get("temperature") or 0.7),
                timeout=90.0,
                include_reasoning=hosted_turn.MODEL_API_PROVIDER_REASONING_ENABLED,
            )
            final_raw_reply = str(final_result.get("reply") or "").strip()
            final_reply, final_thinking, _ = hosted_turn._model_api_parse_turn_output(final_raw_reply)
            if final_reply:
                reply = final_reply
                thinking_summary = final_thinking or thinking_summary
                provider_reasoning = hosted_turn._sanitize_provider_reasoning_text(str(final_result.get("reasoning") or "")) or provider_reasoning
                result = {
                    **final_result,
                    "usage": {
                        "initial": result.get("usage") or {},
                        "final": final_result.get("usage") or {},
                    },
                }
        except provider_client.ProviderError as e:
            if not reply:
                background_execution = hosted_runtime_background_trace(
                    status="not_started",
                    method=HOSTED_RUNTIME_BACKGROUND_NOT_STARTED_METHOD,
                )
                trace = hosted_config_store._append_model_api_action_trace(store, {
                    "status": "failed",
                    "provider": runtime.provider,
                    "model": runtime.model,
                    "user_message_id": user_row["id"],
                    "background_execution": background_execution,
                    "effects": effects,
                    "identity_actions": identity_action_results,
                    "memory_actions": memory_action_results,
                    "context": hosted_context._model_api_context_trace_for_action(
                        context_payload,
                        context_refs=context_refs,
                        web_search=web_search,
                    ),
                    "error": f"provider_chat_after_web_search_failed:{str(e)[:220]}",
                    "duration_ms": int((time.time() - trace_start) * 1000),
                })
                return jsonify({
                    "error": "provider_chat_after_web_search_failed",
                    "detail": str(e),
                    "status_code": e.status_code,
                    "user_message_id": user_row["id"],
                    "action_trace_id": trace.get("trace_id", ""),
                    "tools": {"web_search": hosted_turn._model_api_web_search_trace(web_search)},
                }), 502
    if not reply:
        background_execution = hosted_runtime_background_trace(
            status="not_started",
            method=HOSTED_RUNTIME_BACKGROUND_NOT_STARTED_METHOD,
        )
        trace = hosted_config_store._append_model_api_action_trace(store, {
            "status": "failed",
            "provider": runtime.provider,
            "model": runtime.model,
            "user_message_id": user_row["id"],
            "background_execution": background_execution,
            "context": hosted_context._model_api_context_trace_for_action(
                context_payload,
                context_refs=context_refs,
                web_search=web_search,
            ),
            "error": "provider_empty_reply",
            "duration_ms": int((time.time() - trace_start) * 1000),
        })
        return jsonify({"error": "provider_empty_reply", "user_message_id": user_row["id"], "action_trace_id": trace.get("trace_id", "")}), 502
    assistant_env, env_err = core_envelope._build_shared_envelope_for_store(store, reply.encode("utf-8"))
    if assistant_env is None:
        return jsonify({"error": "assistant_envelope_failed", "detail": env_err}), 409
    assistant_extra: dict = {}
    display_thinking = provider_reasoning or thinking_summary
    if display_thinking:
        thinking_env, thinking_err = core_envelope._build_shared_envelope_for_store(store, display_thinking.encode("utf-8"))
        if thinking_env is not None:
            assistant_extra.update(chat_service._chat_thinking_extra_from_envelope(thinking_env))
            assistant_extra["thinking_kind"] = "provider_reasoning" if provider_reasoning else "context_summary"
        else:
            print(f"[model_api_chat:{store.user_id}] thinking_envelope_failed detail={thinking_err}")
    assistant_row = store.append_chat("openclaw", "model_api", assistant_env, extra=assistant_extra)
    store.notify_chat_waiters()
    delivery_fields = push_service._deliver_ai_message_push_if_background(
        store,
        body=reply,
        title="IO",
        data={"source": "model_api"},
        visual_state="reply",
    )
    updated = store.update_chat_message_metadata(assistant_row["id"], delivery_fields)
    if updated:
        assistant_row = updated
    capture_job = hosted_turn._model_api_maybe_run_memory_capture(
        store,
        api_key,
        runtime,
        user_message=message_for_context,
        assistant_reply=reply,
        user_message_id=user_row["id"],
        assistant_message_id=assistant_row["id"],
        context_payload=context_payload,
        effects=effects,
        run_sync=bool(current_app.config.get("TESTING") and payload.get("capture_sync")),
    )
    state_job: dict = {
        "status": "skipped",
        "reason": "testing_background_execution_disabled" if current_app.config.get("TESTING") else "background_execution_disabled",
        "actions_written": 0,
    }
    if (not current_app.config.get("TESTING")) or payload.get("state_sync") or payload.get("state_async"):
        state_job = hosted_turn._start_model_api_state_action_job(
            store,
            api_key,
            runtime,
            user_message=message_for_context,
            user_message_id=user_row["id"],
            assistant_message_id=assistant_row["id"],
            context_refs=context_refs,
            run_sync=bool(current_app.config.get("TESTING") and payload.get("state_sync")),
        )
    print(
        f"[model_api_chat:{store.user_id}] user_msg={user_row['id']} "
        f"reply={assistant_row['id']} provider={runtime.provider} model={runtime.model}"
    )
    background_execution = hosted_runtime_background_trace(
        status=str(state_job.get("status") or ""),
        method=HOSTED_RUNTIME_BACKGROUND_METHOD,
        trace_id=str(state_job.get("trace_id") or ""),
        error=str(state_job.get("error") or ""),
    )
    trace = hosted_config_store._append_model_api_action_trace(store, {
        "status": "ok",
        "provider": runtime.provider,
        "model": runtime.model,
        "user_message_id": user_row["id"],
        "assistant_message_id": assistant_row["id"],
        "background_execution": background_execution,
        "effects": effects,
        "identity_actions": identity_action_results,
        "memory_actions": memory_action_results,
        "capture": {
            "job_id": capture_job.get("job_id", ""),
            "status": capture_job.get("status", ""),
            "reason": capture_job.get("reason", ""),
            "turn_count": capture_job.get("turn_count", 0),
            "actions_written": capture_job.get("actions_written", 0),
            "warnings": capture_job.get("warnings", []),
            "recap_job_id": capture_job.get("recap_job_id", ""),
            "recap_status": capture_job.get("recap_status", ""),
        },
        "context": hosted_context._model_api_context_trace_for_action(
            context_payload,
            context_refs=context_refs,
            web_search=web_search,
            provider_reasoning=provider_reasoning,
        ),
        "usage": result.get("usage") or {},
        "duration_ms": int((time.time() - trace_start) * 1000),
    })
    return jsonify({
        "status": "ok",
        "reply": reply,
        "context_summary": thinking_summary,
        "thinking_summary": display_thinking,
        "thinking_kind": "provider_reasoning" if provider_reasoning else ("context_summary" if thinking_summary else ""),
        "provider_reasoning": provider_reasoning,
        "user_message": {"id": user_row["id"], "ts": user_row["ts"]},
        "assistant_message": {"id": assistant_row["id"], "ts": assistant_row["ts"]},
        "user_content_type": "image" if has_image else "text",
        "provider": runtime.provider,
        "model": runtime.model,
        "usage": result.get("usage") or {},
        "tools": {
            "web_search": hosted_turn._model_api_web_search_trace(web_search),
        },
        "effects": effects,
        "identity_actions": identity_action_results,
        "memory_actions": memory_action_results,
        "capture": {
            "job_id": capture_job.get("job_id", ""),
            "status": capture_job.get("status", ""),
            "reason": capture_job.get("reason", ""),
            "turn_count": capture_job.get("turn_count", 0),
            "actions_written": capture_job.get("actions_written", 0),
            "warnings": capture_job.get("warnings", []),
            "recap_job_id": capture_job.get("recap_job_id", ""),
            "recap_status": capture_job.get("recap_status", ""),
        },
        "state": {
            "action_trace_id": trace.get("trace_id", ""),
            "background_trace_id": state_job.get("trace_id", ""),
            "receipt": {},
            "pending": [
                hosted_turn._state_pending_public_summary(item)
                for item in hosted_turn._state_pending_items(store)[:5]
            ],
            "background_execution": background_execution,
        },
        "runtime": {
            "engine": HOSTED_RUNTIME_ENGINE_NATIVE,
            "mode": hosted_config_store.MODEL_API_RUNTIME_MODE,
            "version": hosted_config_store.MODEL_API_RUNTIME_VERSION,
            "background_execution": background_execution,
        },
        "context": hosted_context._model_api_context_trace_for_action(
            context_payload,
            context_refs=context_refs,
            web_search=web_search,
        ),
    })

