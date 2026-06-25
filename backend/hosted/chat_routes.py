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
from core import util as core_util
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
from model_api_runtime import memory_tools as hosted_memory_tools
from model_api_runtime.wake import (
    wake_turn_contract_message as model_api_wake_turn_contract_message,
    build_wake_event_message as build_model_api_wake_event_message,
    hosted_tick_trigger as model_api_hosted_tick_trigger,
    parse_wake_actions as parse_model_api_wake_actions,
)
from proactive.agent_protocol_v2 import parse_agent_response_v2, agent_tool_calls_v2
from proactive.tool_catalog_v2 import foreground_chat_tool_catalog_v2, foreground_chat_tool_context_v2
from proactive.tool_executor_v2 import (
    ToolBudgetV2,
    ToolCallV2,
    ToolExecutorV2,
    combined_runtime_adapters_v2,
)
from context_memory_selection import memory_relevance_details
from content_encryption import build_envelope
from memory_index_selector import select_memory_index_items

from accounts import auth
from push import service as push_service
import provider_client
from hosted import agent_runtime_cutover
from hosted import config_store as hosted_config_store
from hosted import context as hosted_context
from hosted import turn as hosted_turn


bp = Blueprint("hosted_chat_routes", __name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _model_api_memory_tools_enabled() -> bool:
    return _env_bool("MODEL_API_MEMORY_TOOLS_ENABLED", False)


def _model_api_auto_memory_context_enabled() -> bool:
    return _env_bool("MODEL_API_AUTO_MEMORY_CONTEXT_ENABLED", True)


HOSTED_CHAT_FULL_TOOL_LOOP_V2_FLAG = "hosted_chat_full_tool_loop_v2_enabled"
FOREGROUND_CHAT_TOOL_BUDGET_MODE_V2 = "foreground_chat_fast"


def _hosted_chat_full_tool_loop_v2_enabled(store: UserStore) -> bool:
    try:
        config = hosted_config_store._load_model_api_config(store)
        profile = hosted_config_store._ensure_model_api_runtime_profile(store, config) or {}
        if HOSTED_CHAT_FULL_TOOL_LOOP_V2_FLAG in profile:
            return bool(profile.get(HOSTED_CHAT_FULL_TOOL_LOOP_V2_FLAG))
        if HOSTED_CHAT_FULL_TOOL_LOOP_V2_FLAG in (config or {}):
            return bool((config or {}).get(HOSTED_CHAT_FULL_TOOL_LOOP_V2_FLAG))
        return core_util.runtime_v2_default_on()
    except Exception:
        return False


def _model_api_memory_tool_calls(raw_reply: str) -> list[tuple[str, dict]]:
    try:
        calls = agent_tool_calls_v2(parse_agent_response_v2(raw_reply))
    except Exception:
        return []
    return [
        (name, args)
        for name, args in calls
        if name in {hosted_memory_tools.MEMORY_INDEX_TOOL, hosted_memory_tools.MEMORY_FETCH_TOOL}
    ]


def _agent_tool_calls_from_reply(raw_reply: str) -> list[tuple[str, dict]]:
    try:
        return agent_tool_calls_v2(parse_agent_response_v2(raw_reply))
    except Exception:
        return []


def _model_api_chat_tool_calls(
    raw_reply: str,
    *,
    memory_tools_enabled: bool,
    perception_tools_enabled: bool,
) -> list[tuple[str, dict, str]]:
    allowed_perception = {tool["name"] for tool in foreground_chat_tool_context_v2()}
    calls: list[tuple[str, dict, str]] = []
    for name, args in _agent_tool_calls_from_reply(raw_reply):
        if memory_tools_enabled and name in {
            hosted_memory_tools.MEMORY_INDEX_TOOL,
            hosted_memory_tools.MEMORY_FETCH_TOOL,
        }:
            calls.append((name, args, "memory"))
        elif perception_tools_enabled:
            calls.append((name, args, "perception" if name in allowed_perception else "foreground_unavailable"))
    return calls


def _foreground_tool_unavailable_result(name: str, *, reason: str) -> dict:
    return {
        "ok": False,
        "name": name,
        "outcome": "unavailable",
        "result": {},
        "error": reason,
        "error_code": reason,
        "error_message": "This tool is not available in foreground chat.",
        "needs_background": False,
    }


def _run_model_api_memory_tool_loop(
    runtime,
    provider_messages: list[dict],
    *,
    store,
    api_key: str | None,
    max_tokens: int,
    temperature: float,
    memory_tools_enabled: bool = True,
    perception_tools_enabled: bool = False,
) -> tuple[dict, str, dict]:
    messages = list(provider_messages)
    memory_trace: dict = {
        "mode": "agent_tools",
        "index_called": False,
        "fetch_called": False,
        "tool_calls": [],
        "fetched_ids": [],
        "cumulative_fetch_limit": hosted_memory_tools.MEMORY_FETCH_CUMULATIVE_LIMIT,
    } if memory_tools_enabled else {}
    perception_trace: dict = {
        "mode": "additive_foreground_perception",
        "budget_mode": FOREGROUND_CHAT_TOOL_BUDGET_MODE_V2,
        "tool_calls": [],
        "available_tools": sorted(tool["name"] for tool in foreground_chat_tool_context_v2()),
    } if perception_tools_enabled else {}
    executor = ToolExecutorV2(
        catalog=foreground_chat_tool_catalog_v2(),
        adapters=combined_runtime_adapters_v2(api_key, store),
        budget=_foreground_chat_tool_budget_v2(),
    ) if perception_tools_enabled else None
    result: dict = {}
    raw_reply = ""
    usage_rounds: list[dict] = []
    for _ in range(4 if perception_tools_enabled else 3):
        result = provider_client.chat_completion(
            runtime,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=90.0,
            include_reasoning=hosted_turn.MODEL_API_PROVIDER_REASONING_ENABLED,
        )
        raw_reply = str(result.get("reply") or "").strip()
        usage_rounds.append(result.get("usage") or {})
        calls = _model_api_chat_tool_calls(
            raw_reply,
            memory_tools_enabled=memory_tools_enabled,
            perception_tools_enabled=perception_tools_enabled,
        )
        if not calls:
            break
        tool_results: list[dict] = []
        for name, args, kind in calls:
            if kind == "memory":
                try:
                    tool_results.append(
                        hosted_memory_tools.execute_memory_tool(
                            store,
                            api_key,
                            name,
                            args,
                            trace=memory_trace,
                        )
                    )
                except Exception as e:
                    memory_trace.setdefault("tool_calls", []).append({
                        "name": name,
                        "ok": False,
                        "error": f"{type(e).__name__}:{str(e)[:160]}",
                    })
                    tool_results.append({"ok": False, "name": name, "error": "memory_tool_failed"})
                continue
            if kind == "foreground_unavailable" or executor is None:
                result_doc = _foreground_tool_unavailable_result(name, reason="foreground_tool_unavailable")
                tool_results.append(result_doc)
                if perception_trace:
                    perception_trace["tool_calls"].append({
                        "name": name,
                        "ok": False,
                        "outcome": "unavailable",
                        "error_code": "foreground_tool_unavailable",
                    })
                continue
            res = executor.execute(ToolCallV2(name=name, args=dict(args or {}), user_id=store.user_id)).as_dict()
            if res.get("needs_background"):
                res = _foreground_tool_unavailable_result(name, reason="foreground_slow_tool_unavailable")
            tool_results.append(res)
            if perception_trace:
                perception_trace["tool_calls"].append({
                    "name": name,
                    "ok": bool(res.get("ok")),
                    "outcome": str(res.get("outcome") or ""),
                    "error_code": str(res.get("error_code") or ""),
                    "needs_background": bool(res.get("needs_background")),
                    "cost_class": ((res.get("trace") or {}) if isinstance(res.get("trace"), dict) else {}).get("cost_class", ""),
                })
        messages.append({"role": "assistant", "content": raw_reply[:4000]})
        messages.append({"role": "user", "content": hosted_memory_tools.render_memory_tool_results(tool_results)})
    if len(usage_rounds) > 1:
        result = {**result, "usage": {"memory_tool_loop": usage_rounds, "final": result.get("usage") or {}}}
    if perception_trace:
        memory_trace["foreground_perception_v2"] = perception_trace
    return result, raw_reply, memory_trace


def _model_api_foreground_perception_tool_instruction_message() -> dict:
    tools_json = json.dumps(
        foreground_chat_tool_context_v2(),
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "role": "system",
        "content": (
            "Additional fast foreground perception tools are available for the current chat turn. "
            "They are additive: keep using the normal foreground chat contract and any separate memory-tool instructions exactly as given. "
            "To gather current perception data, return "
            "{\"tool_calls\":[{\"name\":\"<tool.name>\",\"args\":{...}}]}; the runtime will return tool results. "
            "When finished, return the normal hosted chat final JSON required by the earlier turn contract, e.g. {\"reply\":\"...\"}. "
            "Only the listed fast tools are available here. Do not call memory.*, action tools, steps, sleep, workout, vitals, photo, screen, or long calendar windows. "
            "If a needed tool is not listed, answer from available context and do not promise a background follow-up. "
            "Do not include change_digest or proactive wake assumptions in foreground chat. "
            "Available tools JSON:\n" + tools_json
        ),
    }


def _foreground_chat_tool_budget_v2() -> ToolBudgetV2:
    return ToolBudgetV2(slow_inline_limit=0)


def _memory_fallback_instruction_message(
    fallback_source: str,
    fallback_memories: list,
    context_memory_trace: dict,
) -> dict:
    fallback_json = json.dumps({
        "source": fallback_source,
        "context_memories": fallback_memories[:8],
        "context_memory_trace": context_memory_trace or {},
    }, ensure_ascii=False)[:8000]
    return {
        "role": "system",
        "content": (
            "Memory fallback was triggered because the first answer did not call memory tools. "
            "The memory fallback JSON below is relevant fallback context for the latest user message. "
            "Priority ladder for conflict resolution: Safety/privacy boundaries >= the user's current explicit "
            "message or correction > directly relevant fallback memory > conflicting assistant draft from before "
            "this fallback. "
            "Do not use fallback memory to argue against the user's current correction. If the user now corrects "
            "or updates a fact, follow the current user message and treat older memory as possibly stale. "
            "If fallback memory directly answers the latest user message, use it instead of any conflicting "
            "assistant draft from before this fallback; do not say you are unsure or ask the user to tell you again. "
            "Judge fallback memories by whether their content directly answers the latest user message, not by "
            "weak/generic/approximate trace labels alone. If fallback memory is only tangentially related, do not "
            "make a hard factual claim from it; say you are not sure rather than over-asserting. "
            "Do not mention memory fallback, tools, traces, or JSON to the user.\n"
            "Memory fallback JSON:\n" + fallback_json
        ),
    }


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

    # P3 cutover: a flagged user is served by the out-of-process agent runtime
    # (agent-runner) instead of the inline LLM call below. The user message is
    # already in the chat store, so the agent-runner consumer picks it up and
    # posts a reply; we wait briefly and return it synchronously, else
    # "processing". Default 'legacy' leaves the inline path below unchanged, so
    # rollback is just flipping the flag back.
    # Image turns stay on the legacy multimodal path (the runtime is text-only).
    _arc_driver = agent_runtime_cutover.resolve_driver(hosted_config_store._load_model_api_config(store))
    if agent_runtime_cutover.should_route(_arc_driver, has_image=has_image):
        _arc_body, _arc_status = agent_runtime_cutover.handle_send(store, user_row, _arc_driver)
        return jsonify(_arc_body), _arc_status

    effects: list[dict] = []
    identity_action_results: list[dict] = []
    memory_action_results: list[dict] = []

    full_tool_loop_v2_enabled = _hosted_chat_full_tool_loop_v2_enabled(store)
    memory_tools_enabled = _model_api_memory_tools_enabled()
    auto_memory_context_enabled = _model_api_auto_memory_context_enabled()
    provider_messages, context_payload, screen_images = hosted_context._model_api_context_messages(
        store,
        api_key,
        message_for_context,
        include_screen_context=bool(payload.get("include_screen_context")),
        include_memory_context=not memory_tools_enabled,
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
    insert_at = 3
    if memory_tools_enabled:
        provider_messages.insert(insert_at, hosted_memory_tools.memory_tool_instruction_message())
        insert_at += 1
    if full_tool_loop_v2_enabled:
        provider_messages.insert(insert_at, _model_api_foreground_perception_tool_instruction_message())
    web_search: dict = {}
    memory_tools_trace: dict = {}
    full_tool_loop_trace: dict = {}
    try:
        if memory_tools_enabled or full_tool_loop_v2_enabled:
            result, raw_reply, memory_tools_trace = _run_model_api_memory_tool_loop(
                runtime,
                provider_messages,
                store=store,
                api_key=api_key,
                max_tokens=int(payload.get("max_tokens") or 2048),
                temperature=float(payload.get("temperature") or 0.7),
                memory_tools_enabled=memory_tools_enabled,
                perception_tools_enabled=full_tool_loop_v2_enabled,
            )
            full_tool_loop_trace = dict(memory_tools_trace.get("foreground_perception_v2") or {})
        else:
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
            raw_reply = str(result.get("reply") or "").strip()
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

    reply, thinking_summary, requested_web_search = hosted_turn._model_api_parse_turn_output(raw_reply)
    provider_reasoning = hosted_turn._sanitize_provider_reasoning_text(str(result.get("reasoning") or ""))
    if full_tool_loop_trace:
        context_payload["foreground_tool_loop_v2"] = dict(full_tool_loop_trace)
    if (
        memory_tools_enabled
        and auto_memory_context_enabled
        and not memory_tools_trace.get("index_called")
        and not memory_tools_trace.get("fetch_called")
    ):
        fallback_messages, fallback_context_payload, _fallback_images = hosted_context._model_api_context_messages(
            store,
            api_key,
            message_for_context,
            include_screen_context=False,
            include_memory_context=False,
        )
        fallback_memories = []
        fallback_source = "index_selector"
        fallback_tool_trace: dict = {}
        fallback_index_selection: dict = {}
        try:
            index_result = hosted_memory_tools.execute_memory_tool(
                store,
                api_key,
                hosted_memory_tools.MEMORY_INDEX_TOOL,
                {
                    "query": message_for_context,
                    "limit": 50,
                    "include_sensitive": False,
                },
                trace=fallback_tool_trace,
            )
            index_items = index_result.get("items") if isinstance(index_result.get("items"), list) else []
            fallback_index_selection = select_memory_index_items(
                message_for_context,
                index_items,
                cap=3,
                include_sensitive=False,
            )
            selected_ids = fallback_index_selection.get("selected_ids") if isinstance(fallback_index_selection.get("selected_ids"), list) else []
            if selected_ids:
                fetch_result = hosted_memory_tools.execute_memory_tool(
                    store,
                    api_key,
                    hosted_memory_tools.MEMORY_FETCH_TOOL,
                    {"ids": selected_ids[:3]},
                    trace=fallback_tool_trace,
                )
                fallback_memories = fetch_result.get("items") if isinstance(fetch_result.get("items"), list) else []
                fallback_context_payload["context_memories"] = fallback_memories[:8]
                fallback_context_payload["context_memory_trace"] = fallback_index_selection.get("trace") or {}
        except Exception as e:
            fallback_tool_trace.setdefault("tool_calls", []).append({
                "name": "memory_fallback_index_selector",
                "ok": False,
                "error": f"{type(e).__name__}:{str(e)[:160]}",
            })
        if not fallback_memories:
            auto_messages, auto_context_payload, _auto_images = hosted_context._model_api_context_messages(
                store,
                api_key,
                message_for_context,
                include_screen_context=False,
                include_memory_context=True,
            )
            auto_memories = auto_context_payload.get("context_memories") or []
            if auto_memories:
                fallback_messages = auto_messages
                fallback_context_payload = auto_context_payload
                fallback_memories = auto_memories
                fallback_source = "auto_readside"
        if fallback_memories:
            final_messages = list(fallback_messages)
            final_messages.insert(2, hosted_turn._model_api_turn_contract_message())
            final_messages.append(_memory_fallback_instruction_message(
                fallback_source,
                fallback_memories,
                fallback_context_payload.get("context_memory_trace") or {},
            ))
            final_messages.append({
                "role": "user",
                "content": (
                    "Answer my latest message again now. Use the fallback priority ladder above, and use fallback "
                    "memory only when its content directly answers that message. Return the normal Feedling chat "
                    "turn JSON only."
                ),
            })
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
                            "fallback": final_result.get("usage") or {},
                        },
                    }
                    context_payload["context_memories"] = fallback_memories[:8]
                    context_payload["context_memory_trace"] = fallback_context_payload.get("context_memory_trace") or {}
                    memory_tools_trace = {
                        "mode": "fallback",
                        "fallback_reason": "no_tool_call_backfilled",
                        "fallback_source": fallback_source,
                        "index_called": bool(fallback_tool_trace.get("index_called")),
                        "fetch_called": bool(fallback_tool_trace.get("fetch_called")),
                        "tool_calls": fallback_tool_trace.get("tool_calls") or [],
                        "fetched_ids": fallback_tool_trace.get("fetched_ids") or [],
                    }
            except provider_client.ProviderError:
                pass
    if memory_tools_enabled and memory_tools_trace:
        context_payload["memory_tools"] = {
            key: value
            for key, value in memory_tools_trace.items()
            if key not in {"cumulative_fetch_limit", "foreground_perception_v2"}
        }
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
            "memory": context_payload.get("memory_tools") or {},
            "runtime_v2": context_payload.get("foreground_tool_loop_v2") or {},
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
            "foreground_tool_loop_v2_enabled": bool(full_tool_loop_v2_enabled),
            "background_execution": background_execution,
        },
        "context": hosted_context._model_api_context_trace_for_action(
            context_payload,
            context_refs=context_refs,
            web_search=web_search,
        ),
    })
