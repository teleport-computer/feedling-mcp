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

import db
from core import util as core_util
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

from accounts import onboarding as accounts_onboarding
from bootstrap import gates as boot_gates
from chat import consumer as chat_consumer
from identity import service as identity_service
from genesis import service as genesis_service
from hosted import config_store as hosted_config_store
from hosted import onboarding_validation_core



_GENESIS_ACTIVE_STATUSES = {"created", "uploading", "uploaded", "processing"}
_GENESIS_TERMINAL_STATUSES = {"done", "failed"}
_GENESIS_BACKFILL_SOURCE_KIND = "companion_persona_backfill"


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


def _latest_onboarding_genesis_job(store: UserStore) -> dict | None:
    try:
        jobs = db.genesis_list_jobs(store.user_id, limit=20)
    except Exception:
        return None
    for job in jobs:
        source_kind = str((job or {}).get("source_kind") or "").strip()
        if source_kind == _GENESIS_BACKFILL_SOURCE_KIND:
            continue
        status = str((job or {}).get("status") or "").strip().lower()
        if status in _GENESIS_ACTIVE_STATUSES or status in _GENESIS_TERMINAL_STATUSES:
            return job
    return None


def _genesis_job_metadata(job: dict | None) -> dict:
    metadata = (job or {}).get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _genesis_job_output(job: dict | None) -> dict:
    output = (job or {}).get("output")
    return output if isinstance(output, dict) else {}


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return default


def _identity_card_complete(identity: dict | None, genesis_job: dict | None = None) -> bool:
    """Identity content is never an onboarding completion gate.

    A written card counts even when nameless/empty. A completed Genesis job also
    counts when no card was written because the supplied material contained no
    identity signal. The overall history step still anchors onboarding on the
    Genesis ``done`` status.
    """
    genesis_done = str((genesis_job or {}).get("status") or "").strip().lower() == "done"
    return isinstance(identity, dict) or genesis_done


def _model_api_steps_with_genesis(
    *,
    base_steps: list[dict],
    genesis_job: dict,
    bootstrap_st: dict,
    identity: dict | None,
    identity_written: bool,
    relationship_anchored: bool,
    relationship_evidence: str,
    relationship_ok: bool,
    store: UserStore,
) -> list[dict]:
    status = str(genesis_job.get("status") or "").strip().lower()
    done = status == "done"
    failed = status == "failed"
    metadata = _genesis_job_metadata(genesis_job)
    output = _genesis_job_output(genesis_job)
    stage = str(output.get("stage") or ("completed" if done else status or "genesis")).strip()
    memory_action_count = _int_value(genesis_job.get("memory_action_count"))
    identity_status = str(genesis_job.get("identity_status") or "")
    persona_ref = str(genesis_job.get("persona_ref") or "")
    identity_complete = _identity_card_complete(identity, genesis_job)
    # Per-artifact live signals so the checklist ticks INCREMENTALLY (each step passes as
    # its own artifact lands), matching the base/legacy steps — instead of every step
    # being gated on the single job `done`, which made them all flip at once. The
    # identity-first foreground writes memories -> identity -> relationship -> greeting in
    # sequence, so these light up one by one as the user watches.
    # Memory Garden is informational in A': zero cards is a valid onboarding state.
    # Keep this aligned with the base model_api/resident/official validators so the
    # iOS checklist does not briefly show Memory Garden as done and then regress to
    # waiting when the genesis overlay refreshes.
    # NOTE (Codex): hosted_chat leaning on `done` is only sound because the
    # foreground-ready contract writes the greeting BEFORE done (identity may
    # legitimately remain absent). If `done` ever fires without a greeting again,
    # this mis-lights hosted_chat — keep that invariant.
    hosted_chat_ok = done or _model_api_hosted_chat_verified(store)
    history_windows_total = _int_value(metadata.get("history_windows_total") or output.get("history_windows_total") or 0)
    history_windows_failed = _int_value(metadata.get("history_windows_failed") or output.get("history_windows_failed") or 0)

    history_step = {
        "id": "history_import",
        "label": "Onboarding Materials",
        "passing": done,
        "job_id": genesis_job.get("job_id", ""),
        "job_status": status,
        # client-facing phase: v2-internal stage -> legacy phase the old iOS maps
        "phase": genesis_service.public_stage(stage),
        "phase_label": "Genesis complete" if done else ("Genesis failed" if failed else "Genesis processing"),
        "progress": 100 if done else (100 if failed else 24),
        "messages_parsed": _int_value(metadata.get("history_count")),
        "support_materials": _int_value(metadata.get("support_count")),
        "source_stats": {},
        "ai_persona_chars": 0,
        "user_profile_chars": 0,
        "memory_summary_chars": 0,
        "memories_created": memory_action_count,
        "history_tier": str(metadata.get("history_tier") or ""),
        "timeline_span_days": _int_value(metadata.get("timeline_span_days")),
        "candidate_windows_done": 0,
        "candidate_windows_total": _int_value(metadata.get("window_count") or genesis_job.get("total_chunks")),
        "candidates_extracted": 0,
        "candidates_merged": 0,
        "chat_ready": done,
        "background_status": "",
        "background_windows_done": 0,
        "background_windows_total": 0,
        "genesis": True,
        "memory_action_count": memory_action_count,
        "identity_status": identity_status,
        "persona_ref": persona_ref,
        "history_windows_total": history_windows_total,
        "history_windows_failed": history_windows_failed,
        "degraded": bool(history_windows_failed > 0),
        "support_inputs_present": bool(_int_value(metadata.get("support_count")) > 0),
        "required": (
            "Genesis import failed. Start onboarding again with the latest app build."
            if failed else (
                "Wait for Genesis to finish distilling the onboarding materials."
                if not done else ""
            )
        ),
    }
    memory_step = {
        "id": "memory_garden",
        "label": "Memory Garden",
        "passing": True,
        "blocking": False,
        "memory_count": bootstrap_st["memory_count"],
        "counts": bootstrap_st["counts"],
        "floors": bootstrap_st["floors"],
        "missing_tabs": bootstrap_st["missing_tabs"],
        "genesis": True,
        "memory_action_count": memory_action_count,
        "required": "",
    }
    identity_step = {
        "id": "identity_card",
        "label": "Identity Card",
        "passing": identity_complete,
        "written": identity_written,
        "complete": identity_complete,
        "genesis": True,
        "identity_status": identity_status,
        "required": "" if identity_complete else "Wait for Genesis to finish onboarding.",
    }
    relationship_step = {
        "id": "relationship_anchor",
        "label": "Relationship Anchor",
        "passing": relationship_ok,
        "relationship_anchored": relationship_anchored,
        "relationship_anchor_source": (identity or {}).get("relationship_anchor_source", ""),
        "relationship_anchor_evidence": relationship_evidence,
        "days_with_user": identity_service._live_days_with_user(identity, store=store) if identity else None,
        "genesis": True,
        "required": "" if relationship_ok else "Wait for Genesis to write the relationship anchor.",
    }
    hosted_chat_step = {
        "id": "hosted_chat",
        "label": "Hosted Chat",
        "passing": hosted_chat_ok,
        "genesis": True,
        "required": "" if hosted_chat_ok else "Wait for Genesis to finish before opening hosted chat.",
    }

    replacements = {
        "history_import": history_step,
        "memory_garden": memory_step,
        "identity_card": identity_step,
        "relationship_anchor": relationship_step,
        "hosted_chat": hosted_chat_step,
    }
    return [replacements.get(str(step.get("id") or ""), step) for step in base_steps]


def _model_api_onboarding_validation_payload(store: UserStore) -> dict:
    bootstrap_st = boot_gates._bootstrap_state(store)
    identity = identity_service._load_identity(store)
    identity_written = identity is not None
    genesis_job = _latest_onboarding_genesis_job(store)
    identity_complete = _identity_card_complete(identity, genesis_job)
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
            "last_test_error": (config or {}).get("last_test_error", ""),
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
            "history_windows_total": _int_value((latest_job or {}).get("history_windows_total")),
            "history_windows_failed": _int_value((latest_job or {}).get("history_windows_failed")),
            "degraded": bool(_int_value((latest_job or {}).get("history_windows_failed")) > 0),
            "support_inputs_present": bool(_int_value((latest_job or {}).get("support_materials")) > 0),
            "required": (
                "Start onboarding with AI persona materials, user profile, memory summary, chat history, or confirmed fresh start."
                if not history_ok else ""
            ),
        },
        {
            # A' (2026-06): Memory Garden is informational, not an onboarding gate.
            "id": "memory_garden",
            "label": "Memory Garden",
            "passing": True,
            "blocking": False,
            "memory_count": bootstrap_st["memory_count"],
            "counts": bootstrap_st["counts"],
            "floors": bootstrap_st["floors"],
            "missing_tabs": bootstrap_st["missing_tabs"],
            "required": "",
        },
        {
            "id": "identity_card",
            "label": "Identity Card",
            "passing": identity_complete,
            "written": identity_written,
            "complete": identity_complete,
            "required": "Wait for onboarding to finish." if not identity_complete else "",
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
    if genesis_job:
        steps = _model_api_steps_with_genesis(
            base_steps=steps,
            genesis_job=genesis_job,
            bootstrap_st=bootstrap_st,
            identity=identity,
            identity_written=identity_written,
            relationship_anchored=relationship_anchored,
            relationship_evidence=relationship_evidence,
            relationship_ok=relationship_ok,
            store=store,
        )
    next_step = next((step for step in steps if not step["passing"]), None)
    return {
        "passing": next_step is None,
        "stage": "complete" if next_step is None else next_step["id"],
        "route": "model_api",
        "next_action": "" if next_step is None else next_step["required"],
        "steps": steps,
        "skill_url": core_util.io_onboarding_skill_url("skill-api.md"),
    }


def _official_import_onboarding_validation_payload(store: UserStore) -> dict:
    bootstrap_st = boot_gates._bootstrap_state(store)
    identity = identity_service._load_identity(store)
    identity_written = identity is not None
    genesis_job = _latest_onboarding_genesis_job(store)
    identity_complete = _identity_card_complete(identity, genesis_job)
    relationship_evidence = str((identity or {}).get("relationship_anchor_evidence") or "").strip()
    relationship_ok = bool(identity and identity.get("relationship_started_at") and relationship_evidence)
    steps = [
        {
            # A' (2026-06): Memory Garden is informational, not an onboarding gate.
            "id": "memory_garden",
            "label": "Memory Garden",
            "passing": True,
            "blocking": False,
            "memory_count": bootstrap_st["memory_count"],
            "counts": bootstrap_st["counts"],
            "floors": bootstrap_st["floors"],
            "missing_tabs": bootstrap_st["missing_tabs"],
            "required": "",
        },
        {
            "id": "identity_card",
            "label": "Identity Card",
            "passing": identity_complete,
            "written": identity_written,
            "complete": identity_complete,
            "required": "Wait for onboarding to finish." if not identity_complete else "",
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
        "skill_url": core_util.io_onboarding_skill_url("skill-chat-client.md"),
    }


def _onboarding_validation_payload(store: UserStore) -> dict:
    route = accounts_onboarding._load_onboarding_route(store)
    if route == "model_api":
        return _model_api_onboarding_validation_payload(store)
    if route == "official_import":
        return _official_import_onboarding_validation_payload(store)

    bootstrap_st = boot_gates._bootstrap_state(store)
    identity = identity_service._load_identity(store)
    identity_written = identity is not None
    genesis_job = _latest_onboarding_genesis_job(store)
    identity_complete = _identity_card_complete(identity, genesis_job)
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
            # A' (2026-06): Memory Garden is informational, not an onboarding gate.
            "id": "memory_garden",
            "label": "Memory Garden",
            "passing": True,
            "blocking": False,
            "memory_count": bootstrap_st["memory_count"],
            "counts": bootstrap_st["counts"],
            "floors": bootstrap_st["floors"],
            "missing_tabs": bootstrap_st["missing_tabs"],
            "required": "",
        },
        {
            "id": "identity_card",
            "label": "Identity Card",
            "passing": identity_complete,
            "written": identity_written,
            "complete": identity_complete,
            "required": "Wait for onboarding to finish." if not identity_complete else "",
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
