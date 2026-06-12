"""Hosted chat context assembly (history + identity + memories + screen)."""

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
from core import enclave as core_enclave
from perception import snapshot_for_wake as _perception_wake_snapshot
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

from screen import frames as screen_frames
from hosted import history_import as hosted_history_import
from hosted import turn as hosted_turn


def _model_api_should_attach_screen(message: str, include_flag: bool) -> bool:
    if include_flag:
        return True
    text = (message or "").lower()
    cues = (
        "screen", "screenshot", "what am i looking at", "look at this",
        "current app", "current page", "屏幕", "截图", "现在这个", "帮我看",
    )
    return any(cue in text for cue in cues)


def _model_api_context_messages(
    store: UserStore,
    api_key: str | None,
    user_message: str,
    *,
    include_screen_context: bool,
) -> tuple[list[dict], dict, list[dict[str, str]]]:
    hist, hist_err = core_enclave._enclave_get_json_for_gate(
        "/v1/chat/history",
        api_key,
        {"limit": "30", "context_mode": "model_api", "context_trace": "1"},
    )
    identity_data, identity_err = core_enclave._enclave_get_json_for_gate("/v1/identity/get", api_key)
    context_memories = []
    context_memory_trace = {}
    recent_messages = []
    if isinstance(hist, dict):
        context_memories = hist.get("context_memories") if isinstance(hist.get("context_memories"), list) else []
        context_memory_trace = hist.get("context_memory_trace") if isinstance(hist.get("context_memory_trace"), dict) else {}
        recent_messages = hist.get("messages") if isinstance(hist.get("messages"), list) else []

    identity = {}
    if isinstance(identity_data, dict) and isinstance(identity_data.get("identity"), dict):
        identity = identity_data["identity"]
    pending_state_updates = [
        hosted_turn._state_pending_public_summary(item)
        for item in hosted_turn._state_pending_items(store)[:5]
    ]

    screen_context = ""
    screen_images: list[dict[str, str]] = []
    if _model_api_should_attach_screen(user_message, include_screen_context):
        with store.frames_lock:
            latest = store.frames_meta[-1].copy() if store.frames_meta else None
        if latest and latest.get("id"):
            frame = screen_frames._decrypt_frame_metadata_for_gate(
                store,
                str(latest.get("id")),
                api_key,
                include_image=True,
            )
            if not frame.get("error"):
                screen_context = (
                    f"Current screen app: {frame.get('app') or 'unknown'}\n"
                    f"Current screen OCR: {str(frame.get('ocr_text') or '')[:1600]}"
                )
                image_b64 = str(frame.get("image_b64") or "").strip()
                if image_b64:
                    screen_images.append({
                        "mime": str(frame.get("image_mime") or "image/jpeg"),
                        "b64": image_b64,
                        "label": "current_screen",
                    })

    identity_summary = {
        "agent_name": identity.get("agent_name", ""),
        "self_introduction": identity.get("self_introduction", ""),
        "days_with_user": identity.get("days_with_user"),
        "category": identity.get("category", ""),
        "signature": identity.get("signature", []),
        "dimensions": identity.get("dimensions", []),
    }
    context_payload = {
        "agent_profile": hosted_history_import._model_api_agent_profile_context(store, identity),
        "identity": identity_summary,
        "context_memories": context_memories[:8],
        "context_memory_trace": context_memory_trace,
        "screen_context": screen_context,
        "screen_image_attached": bool(screen_images),
        "pending_state_updates": pending_state_updates,
        # Extended Perception: coarse, permission-gated current state (place
        # label, motion, battery, user_state, …). null fields = unauthorized or
        # stale; agent must not infer from null. See backend/perception/.
        "perception": _perception_wake_snapshot(store.user_id),
        "context_errors": {
            "history": hist_err,
            "identity": identity_err,
        },
    }
    prompt_context_payload = dict(context_payload)
    prompt_context_payload.pop("context_memory_trace", None)
    if context_memory_trace:
        prompt_selection = {}
        selected_trace = context_memory_trace.get("selected") if isinstance(context_memory_trace.get("selected"), list) else []
        query_units = context_memory_trace.get("query_units") if isinstance(context_memory_trace.get("query_units"), list) else []
        query_strong = context_memory_trace.get("query_strong_phrases") if isinstance(context_memory_trace.get("query_strong_phrases"), list) else []
        if selected_trace:
            prompt_selection["selected"] = selected_trace[:8]
        if query_units:
            prompt_selection["query_units"] = query_units[:20]
        if query_strong:
            prompt_selection["query_strong_phrases"] = query_strong[:20]
        if prompt_selection:
            prompt_context_payload["context_memory_selection"] = prompt_selection

    messages = build_model_api_foreground_chat_messages(
        context_payload=prompt_context_payload,
        recent_messages=recent_messages,
        user_message=user_message,
    )
    return messages, context_payload, screen_images


def _model_api_context_trace_summary(context_payload: dict) -> dict:
    trace = context_payload.get("context_memory_trace") if isinstance(context_payload, dict) else {}
    if not isinstance(trace, dict):
        return {}
    selected = trace.get("selected") if isinstance(trace.get("selected"), list) else []
    rejected = trace.get("rejected_sample") if isinstance(trace.get("rejected_sample"), list) else []
    summary: dict = {}
    if selected:
        summary["selected"] = selected[:8]
    if rejected:
        summary["rejected_sample"] = rejected[:8]
    query_units = trace.get("query_units") if isinstance(trace.get("query_units"), list) else []
    query_strong = trace.get("query_strong_phrases") if isinstance(trace.get("query_strong_phrases"), list) else []
    if query_units:
        summary["query_units"] = query_units[:20]
    if query_strong:
        summary["query_strong_phrases"] = query_strong[:20]
    if trace.get("mode"):
        summary["mode"] = str(trace.get("mode") or "")[:40]
    return summary


def _model_api_context_trace_for_action(
    context_payload: dict,
    *,
    context_refs: list[dict],
    web_search: dict,
    provider_reasoning: str | None = None,
) -> dict:
    info = {
        "memories": len(context_payload.get("context_memories") or []),
        "identity_loaded": bool((context_payload.get("identity") or {}).get("agent_name")),
        "screen_context": bool(context_payload.get("screen_context")),
        "context_refs": len(context_refs),
        "web_search": hosted_turn._model_api_web_search_trace(web_search),
    }
    memory_selection = _model_api_context_trace_summary(context_payload)
    if memory_selection:
        info["memory_selection"] = memory_selection
    if provider_reasoning is not None:
        info["provider_reasoning"] = {
            "enabled": hosted_turn.MODEL_API_PROVIDER_REASONING_ENABLED,
            "present": bool(provider_reasoning),
            "chars": len(provider_reasoning),
        }
    return info


def _context_refs_from_payload(payload: dict) -> list[dict]:
    refs = payload.get("context_refs") or payload.get("contextRefs") or []
    if not isinstance(refs, list):
        return []
    out: list[dict] = []
    for ref in refs[:8]:
        if not isinstance(ref, dict):
            continue
        ref_type = str(ref.get("type") or "").strip()
        ref_id = str(ref.get("id") or "").strip()
        if not ref_type or not ref_id:
            continue
        clean = {"type": ref_type[:40], "id": ref_id[:160]}
        if ref.get("title"):
            clean["title"] = str(ref.get("title") or "")[:240]
        out.append(clean)
    return out
