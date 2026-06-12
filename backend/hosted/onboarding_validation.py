"""Onboarding validation payloads + /v1/onboarding/validate."""

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
from accounts import onboarding as accounts_onboarding
from bootstrap import gates as boot_gates
from chat import consumer as chat_consumer
from identity import service as identity_service
from hosted import config_store as hosted_config_store


bp = Blueprint("hosted_onboarding_validation", __name__)

def _visible_agent_message_count(store) -> int:
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    return sum(
        1 for m in chat_msgs
        if isinstance(m, dict)
        and m.get("role") in ("agent", "openclaw")
        and m.get("source") != "verify_ping"
    )


def _real_user_agent_exchange_verified(store) -> bool:
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    sorted_msgs = sorted(
        chat_msgs,
        key=lambda m: float(m.get("ts") or m.get("timestamp") or 0),
    )
    seen_user = False
    for m in sorted_msgs:
        role = m.get("role")
        if role == "user" and m.get("source") != "verify_ping":
            seen_user = True
        elif role in ("agent", "openclaw") and seen_user:
            return True
    return False


def _model_api_hosted_chat_verified(store) -> bool:
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    sorted_msgs = sorted(
        chat_msgs,
        key=lambda m: float(m.get("ts") or m.get("timestamp") or 0),
    )
    seen_model_user = False
    for m in sorted_msgs:
        if m.get("source") != "model_api":
            continue
        role = m.get("role")
        if role == "user":
            seen_model_user = True
        elif role in ("agent", "openclaw"):
            if m.get("model_api_kind") == "onboarding_greeting":
                return True
            if seen_model_user:
                return True
    return False


def _latest_history_import_job(store: UserStore) -> dict | None:
    jobs = db.list_blobs(store.user_id, "history_import_job:")
    if not jobs:
        return None
    jobs.sort(key=lambda j: str(j.get("updated_at") or j.get("created_at") or ""))
    return jobs[-1]


def _model_api_onboarding_validation_payload(store: UserStore) -> dict:
    bootstrap_st = boot_gates._bootstrap_state(store)
    identity = identity_service._load_identity(store)
    identity_written = identity is not None
    relationship_anchored = bool(identity and identity.get("relationship_started_at"))
    relationship_evidence = str((identity or {}).get("relationship_anchor_evidence") or "").strip()
    relationship_ok = relationship_anchored and bool(relationship_evidence)
    config = hosted_config_store._load_model_api_config(store)
    runtime_profile = hosted_config_store._ensure_model_api_runtime_profile(store, config) if config else None
    runtime_ready = bool(
        runtime_profile
        and runtime_profile.get("runtime_mode") == hosted_config_store.MODEL_API_RUNTIME_MODE
        and int(runtime_profile.get("runtime_version") or 0) >= hosted_config_store.MODEL_API_RUNTIME_VERSION
        and runtime_profile.get("tool_action_enabled") is True
    )
    latest_job = _latest_history_import_job(store)
    chat_ready = bool(latest_job and latest_job.get("chat_ready"))
    history_ok = bool(latest_job and (latest_job.get("status") == "completed" or chat_ready))
    counts = bootstrap_st["counts"]
    memory_ok = history_ok and counts.get("story", 0) >= 1 and counts.get("about_me", 0) >= 1
    hosted_chat_ok = _model_api_hosted_chat_verified(store)

    steps = [
        {
            "id": "model_api_config",
            "label": "Model API Config",
            "passing": bool(config),
            "provider": (config or {}).get("provider", ""),
            "model": (config or {}).get("model", ""),
            "required": "Call /v1/model_api/setup with provider, model, and api_key." if not config else "",
        },
        {
            "id": "model_api_test",
            "label": "Model API Test",
            "passing": bool(config and config.get("test_status") == "ok"),
            "test_status": (config or {}).get("test_status", ""),
            "required": "Call /v1/model_api/test until test_status is ok." if not (config and config.get("test_status") == "ok") else "",
        },
        {
            "id": "hosted_runtime",
            "label": "Hosted Runtime",
            "passing": runtime_ready,
            "runtime_mode": (runtime_profile or {}).get("runtime_mode", ""),
            "runtime_version": (runtime_profile or {}).get("runtime_version", 0),
            "tool_action_enabled": bool((runtime_profile or {}).get("tool_action_enabled")),
            "required": "Open /v1/model_api/runtime or send one API chat message to initialize hosted runtime." if not runtime_ready else "",
        },
        {
            "id": "history_import",
            "label": "Onboarding Materials",
            "passing": history_ok,
            "job_id": (latest_job or {}).get("job_id", ""),
            "job_status": (latest_job or {}).get("status", ""),
            "phase": (latest_job or {}).get("phase", ""),
            "phase_label": (latest_job or {}).get("phase_label", ""),
            "progress": (latest_job or {}).get("progress", 0),
            "messages_parsed": (latest_job or {}).get("messages_parsed", 0),
            "support_materials": (latest_job or {}).get("support_materials", 0),
            "source_stats": (latest_job or {}).get("source_stats", {}),
            "ai_persona_chars": (latest_job or {}).get("ai_persona_chars", 0),
            "user_profile_chars": (latest_job or {}).get("user_profile_chars", (latest_job or {}).get("persona_chars", 0)),
            "memory_summary_chars": (latest_job or {}).get("memory_summary_chars", 0),
            "memories_created": (latest_job or {}).get("memories_created", 0),
            "history_tier": (latest_job or {}).get("history_tier", ""),
            "timeline_span_days": (latest_job or {}).get("timeline_span_days", 0),
            "candidate_windows_done": (latest_job or {}).get("candidate_windows_done", 0),
            "candidate_windows_total": (latest_job or {}).get("candidate_windows_total", 0),
            "candidates_extracted": (latest_job or {}).get("candidates_extracted", 0),
            "candidates_merged": (latest_job or {}).get("candidates_merged", 0),
            "chat_ready": chat_ready,
            "background_status": (latest_job or {}).get("background_status", ""),
            "background_windows_done": (latest_job or {}).get("background_windows_done", 0),
            "background_windows_total": (latest_job or {}).get("background_windows_total", 0),
            "required": (
                "Start onboarding with AI persona materials, user profile, memory summary, chat history, or confirmed fresh start."
                if not history_ok else ""
            ),
        },
        {
            "id": "memory_garden",
            "label": "Memory Garden",
            "passing": memory_ok,
            "counts": bootstrap_st["counts"],
            "floors": bootstrap_st["floors"],
            "missing_tabs": bootstrap_st["missing_tabs"],
            "required": "History import must write at least one Story card and one About-me card." if not memory_ok else "",
        },
        {
            "id": "identity_card",
            "label": "Identity Card",
            "passing": identity_written,
            "written": identity_written,
            "required": "History import must derive and write Identity Card." if not identity_written else "",
        },
        {
            "id": "relationship_anchor",
            "label": "Relationship Anchor",
            "passing": relationship_ok,
            "relationship_anchored": relationship_anchored,
            "relationship_anchor_source": (identity or {}).get("relationship_anchor_source", ""),
            "relationship_anchor_evidence": relationship_evidence,
            "days_with_user": identity_service._live_days_with_user(identity, store=store) if identity else None,
            "required": "History import must include relationship_started_at or fresh_start=true." if not relationship_ok else "",
        },
        {
            "id": "hosted_chat",
            "label": "Hosted Chat",
            "passing": hosted_chat_ok,
            "required": "Send one test message through /v1/model_api/chat/send." if not hosted_chat_ok else "",
        },
    ]
    next_step = next((step for step in steps if not step["passing"]), None)
    return {
        "passing": next_step is None,
        "stage": "complete" if next_step is None else next_step["id"],
        "route": "model_api",
        "next_action": "" if next_step is None else next_step["required"],
        "steps": steps,
        "skill_url": "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill-api.md",
    }


def _official_import_onboarding_validation_payload(store: UserStore) -> dict:
    bootstrap_st = boot_gates._bootstrap_state(store)
    memory_ok = not bootstrap_st["missing_tabs"]
    identity = identity_service._load_identity(store)
    identity_written = identity is not None
    relationship_evidence = str((identity or {}).get("relationship_anchor_evidence") or "").strip()
    relationship_ok = bool(identity and identity.get("relationship_started_at") and relationship_evidence)
    steps = [
        {
            "id": "memory_garden",
            "label": "Memory Garden",
            "passing": memory_ok,
            "counts": bootstrap_st["counts"],
            "floors": bootstrap_st["floors"],
            "missing_tabs": bootstrap_st["missing_tabs"],
            "required": boot_gates._gate_required_for_missing_tabs(bootstrap_st) if not memory_ok else "",
        },
        {
            "id": "identity_card",
            "label": "Identity Card",
            "passing": identity_written,
            "written": identity_written,
            "required": "Use the official app/tool client to import memory and identity." if not identity_written else "",
        },
        {
            "id": "relationship_anchor",
            "label": "Relationship Anchor",
            "passing": relationship_ok,
            "relationship_anchor_evidence": relationship_evidence,
            "days_with_user": identity_service._live_days_with_user(identity, store=store) if identity else None,
            "required": "Set relationship anchor during import." if not relationship_ok else "",
        },
    ]
    next_step = next((step for step in steps if not step["passing"]), None)
    return {
        "passing": next_step is None,
        "stage": "import_ready" if next_step is None else next_step["id"],
        "route": "official_import",
        "realtime_chat_supported": False,
        "next_action": "" if next_step is None else next_step["required"],
        "steps": steps,
        "skill_url": "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill-chat-client.md",
    }


def _onboarding_validation_payload(store: UserStore) -> dict:
    route = accounts_onboarding._load_onboarding_route(store)
    if route == "model_api":
        return _model_api_onboarding_validation_payload(store)
    if route == "official_import":
        return _official_import_onboarding_validation_payload(store)

    bootstrap_st = boot_gates._bootstrap_state(store)
    memory_ok = not bootstrap_st["missing_tabs"]
    identity = identity_service._load_identity(store)
    identity_written = identity is not None
    relationship_anchored = bool(identity and identity.get("relationship_started_at"))
    relationship_evidence = str((identity or {}).get("relationship_anchor_evidence") or "").strip()
    relationship_ok = relationship_anchored and bool(relationship_evidence)
    resident = chat_consumer._consumer_validation_state(store)
    chat_loop_ok = boot_gates._chat_loop_verified_by_server(store)
    first_greeting_count = _visible_agent_message_count(store)
    first_greeting_ok = first_greeting_count > 0
    real_exchange_ok = _real_user_agent_exchange_verified(store)

    steps = [
        {
            "id": "memory_garden",
            "label": "Memory Garden",
            "passing": memory_ok,
            "counts": bootstrap_st["counts"],
            "floors": bootstrap_st["floors"],
            "missing_tabs": bootstrap_st["missing_tabs"],
            "required": boot_gates._gate_required_for_missing_tabs(bootstrap_st) if not memory_ok else "",
        },
        {
            "id": "identity_card",
            "label": "Identity Card",
            "passing": identity_written,
            "written": identity_written,
            "required": (
                "Call feedling_identity_init after memory verification passes."
                if not identity_written else ""
            ),
        },
        {
            "id": "relationship_anchor",
            "label": "Relationship Anchor",
            "passing": relationship_ok,
            "relationship_anchored": relationship_anchored,
            "relationship_anchor_source": (identity or {}).get("relationship_anchor_source", ""),
            "relationship_anchor_evidence": relationship_evidence,
            "days_with_user": identity_service._live_days_with_user(identity, store=store) if identity else None,
            "required": (
                "Re-run identity init with relationship_anchor_evidence and a "
                "days_with_user value that matches the earliest memory date."
                if identity_written and not relationship_ok else ""
            ),
        },
        {
            "id": "resident_consumer",
            "label": "Resident Consumer",
            "passing": resident["passing"],
            "official": resident["official"],
            "consumer_name": resident["consumer_name"],
            "consumer_id": resident["consumer_id"],
            "last_poll_at": resident["last_poll_at"],
            "age_sec": resident["age_sec"],
            "required": resident["required"] if not resident["passing"] else "",
        },
        {
            "id": "live_loop",
            "label": "Live Connection",
            "passing": chat_loop_ok,
            "required": (
                "Call feedling_chat_verify_loop after the standard resident "
                "consumer is polling. Only passing=true opens visible chat."
                if not chat_loop_ok else ""
            ),
        },
        {
            "id": "first_greeting",
            "label": "First Greeting",
            "passing": first_greeting_ok,
            "visible_agent_messages": first_greeting_count,
            "required": (
                "After Live Connection passes, send the first greeting via "
                "feedling_chat_post_message."
                if not first_greeting_ok else ""
            ),
        },
        {
            "id": "real_chat_acceptance",
            "label": "Real Chat Acceptance",
            "passing": real_exchange_ok,
            "required": (
                "Ask the user to send one ordinary IO Chat message and confirm "
                "the resident consumer replies naturally."
                if not real_exchange_ok else ""
            ),
        },
    ]

    next_step = next((step for step in steps if not step["passing"]), None)
    return {
        "passing": next_step is None,
        "stage": "complete" if next_step is None else next_step["id"],
        "route": "resident",
        "next_action": "" if next_step is None else next_step["required"],
        "steps": steps,
        "skill_url": boot_gates._SKILL_URL,
    }


@bp.route("/v1/onboarding/validate", methods=["GET"])
def onboarding_validate():
    """Authoritative onboarding acceptance check.

    This is deliberately server-side and artifact-based: agents can report
    anything, but the validator only passes a step when Feedling can see the
    corresponding write, resident-consumer heartbeat, verify-loop event, or
    real user→agent exchange.
    """
    store = auth.require_user()
    return jsonify(_onboarding_validation_payload(store))

