"""Hosted setup HTTP surface: /v1/model_api/{setup,get,test,delete,runtime,memory/repair}, /v1/state/receipts, /v1/memory/capture_jobs."""

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
from core import envelope as core_envelope
from core import enclave as core_enclave
from flask import current_app
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

from accounts import auth
from accounts import onboarding as accounts_onboarding
from core import util as core_util
from memory import service as memory_service
import provider_client
from hosted import config_store as hosted_config_store
from hosted import turn as hosted_turn


bp = Blueprint("hosted_setup_routes", __name__)

@bp.route("/v1/model_api/setup", methods=["POST"])
def model_api_setup():
    store = auth.require_user()
    caller_api_key = auth._extract_api_key()
    payload = request.get_json(silent=True) or {}
    provider = str(payload.get("provider") or "")
    model = str(payload.get("model") or "")
    base_url = str(payload.get("base_url") or "")
    raw_key = str(payload.get("api_key") or "").strip()
    try:
        provider, model, base_url = validate_provider_config(provider, model, base_url)
    except provider_client.ProviderError as e:
        return jsonify({"error": str(e)}), 400

    existing = hosted_config_store._load_model_api_config(store) or {}
    existing_envelope = existing.get("api_key_envelope")
    if raw_key:
        provider_key = raw_key
        envelope, err = core_envelope._build_shared_envelope_for_store(
            store,
            raw_key.encode("utf-8"),
            item_id=f"model_api_key_{uuid.uuid4().hex}",
        )
        if envelope is None:
            return jsonify({
                "error": "cannot_encrypt_provider_key",
                "detail": err,
                "required": (
                    "The user must have a content public key and the enclave "
                    "attestation endpoint must be reachable before saving a provider key."
                ),
            }), 409
        api_key_hint = provider_client.mask_api_key(raw_key)
    else:
        if not isinstance(existing_envelope, dict):
            return jsonify({"error": "api_key required"}), 400
        try:
            provider_key = core_enclave._decrypt_envelope_via_enclave(
                existing_envelope,
                caller_api_key,
                purpose="model_api_provider_key",
            ).decode("utf-8")
        except Exception as e:
            return jsonify({
                "error": "model_api_key_decrypt_failed",
                "detail": str(e)[:220],
            }), 400
        envelope = existing_envelope
        api_key_hint = str(existing.get("api_key_hint") or "saved key")

    try:
        test = provider_client.test_provider_key(provider_client.ProviderConfig(provider, model, provider_key, base_url))
    except provider_client.ProviderError as e:
        # Log enough to triage a user-reported "key won't validate" without ever
        # logging the raw key: the failure detail (e.g. provider_http_404 for a
        # bad model name, or 401/429 for a bad/quota'd key) only lives here.
        print(
            f"[model_api:{store.user_id}] setup FAILED provider={provider} "
            f"model={model} status_code={e.status_code} detail={str(e)[:160]}"
        )
        return jsonify({
            "error": "provider_test_failed",
            "detail": str(e),
            "status_code": e.status_code,
        }), 400

    config = hosted_config_store._save_model_api_config(store, {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key_hint": api_key_hint,
        "api_key_envelope": envelope,
        "test_status": "ok",
        "last_test_at": core_util._now_iso(),
        "last_test_usage": test.get("usage") or {},
        "privacy_mode": "tdx_cvm_backend_runtime_option_a",
    })
    hosted_config_store._ensure_model_api_runtime_profile(store, config, touch=True)
    accounts_onboarding._save_onboarding_route(store, "model_api")
    print(f"[model_api:{store.user_id}] setup provider={provider} model={model}")
    return jsonify({"status": "configured", "config": hosted_config_store._public_model_api_config(config)})


@bp.route("/v1/model_api/get", methods=["GET"])
def model_api_get():
    store = auth.require_user()
    return jsonify({"config": hosted_config_store._public_model_api_config(hosted_config_store._load_model_api_config(store))})


@bp.route("/v1/model_api/test", methods=["POST"])
def model_api_test():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    config = hosted_config_store._load_model_api_config(store)
    if not config:
        return jsonify({"error": "model_api_not_configured"}), 404
    runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        _, err = runtime
        config["test_status"] = "failed"
        config["last_test_error"] = err.get("error", "unknown")
        hosted_config_store._save_model_api_config(store, config)
        return jsonify(err), 400
    try:
        test = provider_client.test_provider_key(runtime)
    except provider_client.ProviderError as e:
        config["test_status"] = "failed"
        config["last_test_error"] = str(e)[:240]
        hosted_config_store._save_model_api_config(store, config)
        return jsonify({
            "error": "provider_test_failed",
            "detail": str(e),
            "status_code": e.status_code,
        }), 400
    config["test_status"] = "ok"
    config["last_test_at"] = core_util._now_iso()
    config["last_test_error"] = ""
    config["last_test_usage"] = test.get("usage") or {}
    hosted_config_store._save_model_api_config(store, config)
    hosted_config_store._ensure_model_api_runtime_profile(store, config, touch=True)
    print(f"[model_api:{store.user_id}] test ok provider={config.get('provider')} model={config.get('model')}")
    return jsonify({"status": "ok", "config": hosted_config_store._public_model_api_config(config)})


@bp.route("/v1/model_api/delete", methods=["DELETE"])
def model_api_delete():
    store = auth.require_user()
    deleted = db.delete_blob(store.user_id, "model_api")
    db.delete_blob(store.user_id, hosted_config_store.MODEL_API_RUNTIME_BLOB)
    print(f"[model_api:{store.user_id}] deleted={deleted}")
    return jsonify({"deleted": deleted})


def _model_api_recap_status(store: UserStore) -> dict:
    latest = hosted_turn._model_api_latest_recap_job(store)
    with hosted_turn._model_api_recap_active_lock:
        active = store.user_id in hosted_turn._model_api_recap_active_users
    if not latest:
        return {"status": "idle", "active": active}
    status = str(latest.get("status") or "idle")
    if active and status not in {"failed", "completed", "skipped"}:
        status = "running"
    return {
        "status": status,
        "active": active,
        "job_id": latest.get("job_id", ""),
        "mode": latest.get("mode", ""),
        "progress": latest.get("progress", 0),
        "updated_at": latest.get("completed_at") or latest.get("created_at") or "",
    }


@bp.route("/v1/model_api/runtime", methods=["GET"])
def model_api_runtime_status():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    config = hosted_config_store._load_model_api_config(store)
    if not config:
        return jsonify({
            "configured": False,
            "runtime_mode": hosted_config_store.MODEL_API_RUNTIME_MODE,
            "runtime_version": hosted_config_store.MODEL_API_RUNTIME_VERSION,
            "recap_status": "idle",
            "memory_quality_warning": None,
        })
    profile = hosted_config_store._ensure_model_api_runtime_profile(store, config) or {}
    scan = hosted_turn._model_api_memory_quality_scan(store, api_key=api_key, max_cards=120, fast=True)
    warning = scan.get("warning")
    if warning != profile.get("memory_quality_warning"):
        profile = hosted_config_store._patch_model_api_runtime_profile(store, {"memory_quality_warning": warning}) or profile
    latest_trace = hosted_config_store._latest_model_api_action_trace(store)
    recap = _model_api_recap_status(store)
    return jsonify({
        "configured": True,
        "runtime_mode": profile.get("runtime_mode") or hosted_config_store.MODEL_API_RUNTIME_MODE,
        "runtime_version": int(profile.get("runtime_version") or hosted_config_store.MODEL_API_RUNTIME_VERSION),
        "tool_action_enabled": bool(profile.get("tool_action_enabled", True)),
        "provider": config.get("provider", ""),
        "model": config.get("model", ""),
        "recap_status": recap.get("status", "idle"),
        "recap": recap,
        "memory_quality_warning": warning,
        "memory_quality": {
            "scanned": scan.get("scanned", 0),
            "issue_count": scan.get("issue_count", 0),
            "noisy_count": scan.get("noisy_count", 0),
            "duplicate_count": scan.get("duplicate_count", 0),
        },
        "last_action_trace_id": profile.get("last_action_trace_id", ""),
        "last_action_trace_at": profile.get("last_action_trace_at", ""),
        "last_action_trace_status": (latest_trace or {}).get("status", ""),
        "last_runtime_error": profile.get("last_runtime_error", ""),
    })


@bp.route("/v1/model_api/memory/repair", methods=["POST"])
def model_api_memory_repair():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "dry_run").strip().lower()
    if mode not in {"dry_run", "apply"}:
        return jsonify({"error": "mode must be dry_run or apply"}), 400
    archive_old = bool(payload.get("archive_old", True))
    scan = hosted_turn._model_api_memory_quality_scan(store, api_key=api_key, max_cards=2000)
    noisy_count = int(scan.get("noisy_count") or 0)
    new_cards_planned = max(6, min(30, noisy_count)) if noisy_count else 0
    preview = {
        "old_cards_detected": noisy_count,
        "issue_count": int(scan.get("issue_count") or 0),
        "duplicate_count": int(scan.get("duplicate_count") or 0),
        "new_cards_planned": new_cards_planned,
        "noisy_ids": scan.get("noisy_ids", [])[:80],
        "issues": scan.get("issues", [])[:20],
    }
    hosted_config_store._patch_model_api_runtime_profile(store, {
        "memory_quality_warning": scan.get("warning"),
        "last_memory_quality_scan_at": core_util._now_iso(),
    })
    if mode == "dry_run":
        return jsonify({
            "status": "completed",
            "mode": "dry_run",
            "preview": preview,
            "memory_quality": scan,
        })
    if not preview["old_cards_detected"]:
        return jsonify({
            "status": "skipped",
            "mode": "apply",
            "reason": "no_noisy_memory_cards_detected",
            "preview": preview,
        })

    runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        _, err = runtime
        return jsonify(err), 400

    job = memory_service._append_memory_capture_job(store, {
        "mode": "repair",
        "status": "queued",
        "progress": 0,
        "old_cards_detected": preview["old_cards_detected"],
        "new_cards_planned": preview["new_cards_planned"],
        "repair_noisy_ids": preview["noisy_ids"],
        "archive_old": archive_old,
    })
    run_sync = bool(payload.get("synchronous") or payload.get("sync") or current_app.config.get("TESTING"))
    if run_sync:
        hosted_turn._run_model_api_memory_repair_job(
            store,
            api_key,
            runtime,
            job["job_id"],
            noisy_ids=preview["noisy_ids"],
            archive_old=archive_old,
        )
        jobs = db.log_read(store.user_id, "memory_capture_jobs", limit=20)
        latest = next((item for item in reversed(jobs) if item.get("job_id") == job["job_id"]), job)
        return jsonify({
            "status": latest.get("status", "completed"),
            "mode": "apply",
            "job_id": job["job_id"],
            "job": latest,
            "preview": preview,
        })

    thread = threading.Thread(
        target=hosted_turn._run_model_api_memory_repair_job,
        args=(store, api_key, runtime, job["job_id"]),
        kwargs={"noisy_ids": preview["noisy_ids"], "archive_old": archive_old},
        daemon=True,
    )
    thread.start()
    return jsonify({
        "status": "queued",
        "mode": "apply",
        "job_id": job["job_id"],
        "job": job,
        "preview": preview,
    }), 202


@bp.route("/v1/state/receipts", methods=["GET"])
def state_receipts():
    store = auth.require_user()
    try:
        limit = min(max(int(request.args.get("limit", 30)), 1), 100)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    return jsonify({
        "receipts": hosted_turn._load_state_receipts(store, limit=limit),
        "pending": [
            {
                "id": item.get("id", ""),
                "created_at": item.get("created_at", ""),
                "expires_at": item.get("expires_at", 0),
                "action": ((item.get("runtime_action") or {}).get("runtime_type") or ""),
                "confidence": (item.get("runtime_action") or {}).get("confidence", 0),
            }
            for item in hosted_turn._state_pending_items(store)
        ],
    })


@bp.route("/v1/memory/capture_jobs", methods=["GET"])
def memory_capture_jobs():
    store = auth.require_user()
    try:
        limit = min(max(int(request.args.get("limit", 30)), 1), 100)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    jobs = db.log_read(store.user_id, "memory_capture_jobs", limit=limit)
    jobs.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    with hosted_turn._model_api_recap_active_lock:
        active_recap = store.user_id in hosted_turn._model_api_recap_active_users
    return jsonify({
        "jobs": jobs,
        "active_recap": active_recap,
    })


