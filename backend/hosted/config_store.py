"""Model API config / runtime profile / action traces (hosted line)."""

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
from provider_client import public_config as public_provider_config
from provider_client import validate_config as validate_provider_config
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

from core import util as core_util
import provider_client


def _load_model_api_config(store: UserStore) -> dict | None:
    data = db.get_blob(store.user_id, "model_api")
    if not data:
        return None
    if data.get("route") != "model_api":
        data["route"] = "model_api"
    return data


def _save_model_api_config(store: UserStore, config: dict) -> dict:
    data = dict(config)
    data["route"] = "model_api"
    data["updated_at"] = core_util._now_iso()
    if not data.get("created_at"):
        data["created_at"] = data["updated_at"]
    db.set_blob(store.user_id, "model_api", data)
    return data


def _public_model_api_config(config: dict | None) -> dict:
    if not config:
        return {"configured": False}
    safe = public_provider_config(config)
    safe["configured"] = True
    safe["privacy_mode"] = "tdx_cvm_backend_runtime_option_a"
    return safe


MODEL_API_RUNTIME_BLOB = "model_api_runtime"
MODEL_API_RUNTIME_VERSION = 2
MODEL_API_RUNTIME_MODE = "hosted_resident"
MODEL_API_ACTION_TRACE_STREAM = "model_api_action_traces"


def _load_model_api_runtime_profile(store: UserStore) -> dict | None:
    data = db.get_blob(store.user_id, MODEL_API_RUNTIME_BLOB)
    return data if isinstance(data, dict) else None


def _save_model_api_runtime_profile(store: UserStore, profile: dict) -> dict:
    data = dict(profile)
    data["runtime_mode"] = MODEL_API_RUNTIME_MODE
    data["runtime_version"] = MODEL_API_RUNTIME_VERSION
    data["updated_at"] = core_util._now_iso()
    if not data.get("created_at"):
        data["created_at"] = data["updated_at"]
    db.set_blob(store.user_id, MODEL_API_RUNTIME_BLOB, data)
    return data


def _ensure_model_api_runtime_profile(
    store: UserStore,
    config: dict | None = None,
    *,
    touch: bool = False,
) -> dict | None:
    """Lazily migrate model_api users into the hosted resident runtime.

    This is intentionally metadata-only: provider key envelopes, chat,
    identity, and memory cards remain untouched.
    """
    config = config if isinstance(config, dict) else _load_model_api_config(store)
    if not config:
        return None
    existing = _load_model_api_runtime_profile(store) or {}
    profile = dict(existing)
    changed = touch

    defaults = {
        "runtime_mode": MODEL_API_RUNTIME_MODE,
        "runtime_version": MODEL_API_RUNTIME_VERSION,
        "tool_action_enabled": True,
        "recap_cursor": None,
        "last_recap_at": None,
        "last_action_trace_id": None,
        "memory_quality_warning": None,
        "provider": str(config.get("provider") or ""),
        "model": str(config.get("model") or ""),
    }
    for key, value in defaults.items():
        if profile.get(key) != value and (
            key in {"runtime_mode", "runtime_version", "tool_action_enabled"}
            or key not in profile
            or key in {"provider", "model"}
        ):
            profile[key] = value
            changed = True
    if changed or not existing:
        profile = _save_model_api_runtime_profile(store, profile)
    return profile


def _patch_model_api_runtime_profile(store: UserStore, patch: dict) -> dict | None:
    profile = _ensure_model_api_runtime_profile(store) or {}
    if not profile:
        return None
    merged = dict(profile)
    merged.update({k: v for k, v in patch.items() if v is not None})
    return _save_model_api_runtime_profile(store, merged)


def _append_model_api_action_trace(store: UserStore, entry: dict) -> dict:
    record = {
        "trace_id": entry.get("trace_id") or f"mat_{uuid.uuid4().hex[:16]}",
        "ts": time.time(),
        "created_at": core_util._now_iso(),
        "runtime_mode": MODEL_API_RUNTIME_MODE,
        "runtime_version": MODEL_API_RUNTIME_VERSION,
        "status": str(entry.get("status") or "ok")[:80],
    }
    for key in (
        "provider", "model", "user_message_id", "assistant_message_id",
        "state_receipt_id", "background_execution", "runtime", "effects", "identity_actions",
        "memory_actions", "capture", "context", "error", "duration_ms",
        "usage", "reason", "progress",
    ):
        if key in entry:
            record[key] = entry[key]
    db.log_append(
        store.user_id,
        MODEL_API_ACTION_TRACE_STREAM,
        record,
        ts=record["ts"],
        item_key=record["trace_id"],
    )
    patch = {
        "last_action_trace_id": record["trace_id"],
        "last_action_trace_at": record["created_at"],
    }
    if record["status"] == "ok":
        patch["last_runtime_error"] = ""
    elif record.get("error"):
        patch["last_runtime_error"] = str(record.get("error"))[:300]
    _patch_model_api_runtime_profile(store, patch)
    return record


def _patch_model_api_action_trace(store: UserStore, trace_id: str, patch: dict) -> dict | None:
    merged = dict(patch)
    if patch.get("status") in {"completed", "failed", "skipped"}:
        merged.setdefault("completed_at", core_util._now_iso())
    record = db.log_patch_item(store.user_id, MODEL_API_ACTION_TRACE_STREAM, trace_id, merged)
    profile_patch: dict = {
        "last_action_trace_id": trace_id,
        "last_action_trace_at": core_util._now_iso(),
    }
    if patch.get("status") in {"completed", "skipped", "ok"}:
        profile_patch["last_runtime_error"] = ""
    elif patch.get("error"):
        profile_patch["last_runtime_error"] = str(patch.get("error"))[:300]
    _patch_model_api_runtime_profile(store, profile_patch)
    return record


def _latest_model_api_action_trace(store: UserStore) -> dict | None:
    traces = db.log_read(store.user_id, MODEL_API_ACTION_TRACE_STREAM, limit=1)
    return traces[-1] if traces else None




def _provider_config_from_plain(config: dict, api_key: str) -> provider_client.ProviderConfig:
    provider, model, base_url = validate_provider_config(
        str(config.get("provider") or ""),
        str(config.get("model") or ""),
        str(config.get("base_url") or ""),
    )
    return provider_client.ProviderConfig(provider=provider, model=model, api_key=api_key, base_url=base_url)


def _load_runtime_provider_config(store: UserStore, api_key: str | None) -> provider_client.ProviderConfig | tuple[None, dict]:
    config = _load_model_api_config(store)
    if not config:
        return None, {"error": "model_api_not_configured"}
    if config.get("test_status") != "ok":
        return None, {"error": "model_api_not_tested", "test_status": config.get("test_status", "")}
    envelope = config.get("api_key_envelope")
    if not isinstance(envelope, dict):
        return None, {"error": "model_api_key_envelope_missing"}
    try:
        provider_key = core_enclave._decrypt_envelope_via_enclave(
            envelope,
            api_key,
            purpose="model_api_provider_key",
        ).decode("utf-8")
    except Exception as e:
        return None, {"error": "model_api_key_decrypt_failed", "detail": str(e)[:220]}
    try:
        return _provider_config_from_plain(config, provider_key)
    except provider_client.ProviderError as e:
        return None, {"error": "model_api_config_invalid", "detail": str(e)}
