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

from accounts import registry
import db
import debug_trace
from core.reqctx import request
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
from context_memory_selection import memory_relevance_details
from content_encryption import build_envelope

from screen import frames as screen_frames
from hosted import history_import as hosted_history_import
from hosted import turn as hosted_turn
import worldbook_readside_core


def _model_api_should_attach_screen(message: str, include_flag: bool) -> bool:
    if include_flag:
        return True
    text = (message or "").lower()
    cues = (
        "screen", "screenshot", "what am i looking at", "look at this",
        "current app", "current page", "屏幕", "截图", "现在这个", "帮我看",
    )
    return any(cue in text for cue in cues)


def _model_api_worldbook_context(
    store: UserStore,
    api_key: str | None,
    recent_messages: list[dict],
    user_message: str,
) -> dict:
    with store.world_books_lock:
        world_books = [dict(item) for item in getattr(store, "world_books", [])]
    if not world_books:
        return {"block": "", "matched_names": [], "rejected_over_cap": [], "unavailable_ids": []}
    messages = list(recent_messages or [])
    if user_message and not (
        messages
        and str(messages[-1].get("role") or "") == "user"
        and str(messages[-1].get("content") or "") == user_message
    ):
        messages.append({"role": "user", "content": user_message})
    runtime_token = request.headers.get("X-Feedling-Runtime-Token", "").strip() or None
    try:
        result = worldbook_readside_core.post_enclave_worldbook_match(
            api_key,
            world_books,
            messages,
            runtime_token=runtime_token,
        )
    except Exception as e:
        print(f"[worldbook:{store.user_id}] match failed: {type(e).__name__}: {e}")
        return {"block": "", "matched_names": [], "rejected_over_cap": [], "unavailable_ids": []}
    return {
        "block": str(result.get("block") or ""),
        "matched_names": result.get("matched_names") if isinstance(result.get("matched_names"), list) else [],
        "rejected_over_cap": result.get("rejected_over_cap") if isinstance(result.get("rejected_over_cap"), list) else [],
        "unavailable_ids": result.get("unavailable_ids") if isinstance(result.get("unavailable_ids"), list) else [],
    }


def _model_api_context_messages(
    store: UserStore,
    api_key: str | None,
    user_message: str,
    *,
    include_screen_context: bool,
    include_memory_context: bool = True,
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
    if not include_memory_context:
        context_memories = []
        context_memory_trace = {}

    identity = {}
    if isinstance(identity_data, dict) and isinstance(identity_data.get("identity"), dict):
        identity = identity_data["identity"]
    world_book = _model_api_worldbook_context(store, api_key, recent_messages, user_message)
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
        # Persona / voice layer. These profile fields are writable via
        # identity.profile_patch (see identity/actions.py _IDENTITY_PROFILE_FIELDS)
        # but were previously dropped from this summary, so the hosted chat
        # prompt never saw the agent's tone, role, or boundaries — a root cause
        # of model_api voice drift (the persona was write-only / dead). Surface
        # them so the foreground prompt and the memory-capture worker can
        # continue the established voice. Empty until distillation (P2) fills
        # them; empty values just render as blanks the model ignores.
        "agent_role": identity.get("agent_role", ""),
        "tone_style": identity.get("tone_style", ""),
        # User-authored override (D1 user layer). Highest-priority persona
        # directive — see the precedence instruction in
        # model_api_runtime/prompts.py build_foreground_chat_messages.
        "custom_persona_prompt": identity.get("custom_persona_prompt", ""),
        "user_preferred_name": identity.get("user_preferred_name", ""),
        "language_preference": identity.get("language_preference", ""),
        "boundaries": identity.get("boundaries", []),
        "do_not_say": identity.get("do_not_say", []),
        "stable_definitions": identity.get("stable_definitions", []),
    }
    perception_snapshot = _perception_wake_snapshot(store.user_id)
    context_payload = {
        "agent_profile": hosted_history_import._model_api_agent_profile_context(store, identity),
        "identity": identity_summary,
        "context_memories": context_memories[:8],
        "archive_language": registry._get_user_archive_language(store.user_id) or "",
        "locale": str(perception_snapshot.get("locale") or "") if isinstance(perception_snapshot, dict) else "",
        "context_memory_trace": context_memory_trace,
        "world_book": {
            "matched_names": world_book["matched_names"],
            "rejected_over_cap": world_book["rejected_over_cap"],
            "unavailable_ids": world_book["unavailable_ids"],
        },
        "screen_context": screen_context,
        "screen_image_attached": bool(screen_images),
        "pending_state_updates": pending_state_updates,
        # Extended Perception: coarse, permission-gated current state (place
        # label, motion, battery, user_state, …). null fields = unauthorized or
        # stale; agent must not infer from null. See backend/perception/.
        "perception": perception_snapshot,
        "context_errors": {
            "history": hist_err,
            "identity": identity_err,
        },
    }
    prompt_context_payload = dict(context_payload)
    prompt_context_payload.pop("context_memory_trace", None)
    if world_book["block"]:
        prompt_context_payload["world_book_block"] = world_book["block"]
        if debug_trace.is_enabled(store):
            debug_trace.trace_event(
                store,
                subsystem="worldbook",
                type="worldbook_injected",
                actor="host_agent_runtime",
                summary=f"worldbook injected {len(world_book['matched_names'])} entries",
                detail={"names": world_book["matched_names"]},
            )
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
        # Soft-recall index (P3 / D3): more of the user's cards by title/date so
        # the model can recall one even if it didn't lexically match. See
        # context_memory_selection.py strict branch + the instruction in
        # model_api_runtime/prompts.py.
        index_sample = context_memory_trace.get("index_sample") if isinstance(context_memory_trace.get("index_sample"), list) else []
        if index_sample:
            prompt_selection["memory_index"] = index_sample[:20]
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
    memory_tools = context_payload.get("memory_tools") if isinstance(context_payload.get("memory_tools"), dict) else {}
    if memory_tools:
        info["memory_tools"] = memory_tools
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
