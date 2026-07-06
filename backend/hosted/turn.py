"""Hosted turn parsing + background jobs: state actions, memory capture, recap, repair."""

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
from core import envelope as core_envelope
from core import enclave as core_enclave
from chat import service as chat_service
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

from accounts import registry
from core import util as core_util
from identity import actions as identity_actions_mod
from identity import service as identity_service
from memory import actions as memory_actions_mod
from memory import service as memory_service
from push import service as push_service
import provider_client
from hosted import config_store as hosted_config_store
from hosted import history_import as hosted_history_import


MODEL_API_STATE_DIRECT_CONFIDENCE = float(os.environ.get("FEEDLING_MODEL_API_STATE_DIRECT_CONFIDENCE", "0.85"))
MODEL_API_STATE_CONFIRM_CONFIDENCE = float(os.environ.get("FEEDLING_MODEL_API_STATE_CONFIRM_CONFIDENCE", "0.55"))
MODEL_API_CAPTURE_TURN_INTERVAL = max(12, int(os.environ.get("FEEDLING_MODEL_API_CAPTURE_TURN_INTERVAL", "24")))
MODEL_API_CONSOLIDATE_TURN_INTERVAL = max(60, int(os.environ.get("FEEDLING_MODEL_API_CONSOLIDATE_TURN_INTERVAL", "80")))
MODEL_API_CONSOLIDATE_MIN_INTERVAL_SEC = max(
    3600,
    int(os.environ.get("FEEDLING_MODEL_API_CONSOLIDATE_MIN_INTERVAL_SEC", str(12 * 3600))),
)
MODEL_API_WEB_SEARCH_ENABLED = os.environ.get("FEEDLING_MODEL_API_WEB_SEARCH_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
MODEL_API_WEB_SEARCH_MAX_QUERIES = max(1, min(3, int(os.environ.get("FEEDLING_MODEL_API_WEB_SEARCH_MAX_QUERIES", "2"))))
MODEL_API_WEB_SEARCH_MAX_RESULTS = max(1, min(8, int(os.environ.get("FEEDLING_MODEL_API_WEB_SEARCH_MAX_RESULTS", "5"))))
MODEL_API_WEB_SEARCH_TIMEOUT_SEC = max(2.0, min(20.0, float(os.environ.get("FEEDLING_MODEL_API_WEB_SEARCH_TIMEOUT_SEC", "8"))))
MODEL_API_PROVIDER_REASONING_ENABLED = os.environ.get("FEEDLING_MODEL_API_PROVIDER_REASONING_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
MODEL_API_PROVIDER_REASONING_MAX_CHARS = chat_service.MODEL_API_PROVIDER_REASONING_MAX_CHARS
# State receipts: one append per chat turn. Read views cap at 100; keep a
# generous tail so the stream can't grow without bound across a user's history.
STATE_RECEIPT_MAX = int(os.environ.get("FEEDLING_STATE_RECEIPT_MAX", 1000))
_sanitize_visible_thinking_summary = chat_service._sanitize_visible_thinking_summary
_sanitize_provider_reasoning_text = chat_service._sanitize_provider_reasoning_text

_STATE_PENDING_BLOB = "model_api_state_pending"
_model_api_recap_active_users: set[str] = set()
_model_api_recap_active_lock = threading.Lock()
_model_api_state_active_users: set[str] = set()
_model_api_state_active_lock = threading.Lock()
# Per-user guard for the running memory-capture job. State actions and recap
# already have one each; capture did not — overlapping capture windows (the
# turn-24 job still running when turn-48 fires) could double-write cards. Mirror
# the recap pattern: add on start, discard in the runner's single exit (finish).
_model_api_capture_active_users: set[str] = set()
_model_api_capture_active_lock = threading.Lock()


def _model_api_turn_count(store: UserStore) -> int:
    with store.chat_lock:
        return sum(
            1
            for msg in store.chat_messages
            if isinstance(msg, dict)
            and msg.get("role") == "user"
            and msg.get("source") == "model_api"
        )


def _state_lang_zh(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _state_pending_items(store: UserStore) -> list[dict]:
    data = db.get_blob(store.user_id, _STATE_PENDING_BLOB)
    items = data.get("items") if isinstance(data, dict) else []
    now = time.time()
    clean = [
        item for item in items
        if isinstance(item, dict) and float(item.get("expires_at") or 0) > now
    ]
    if clean != items:
        db.set_blob(store.user_id, _STATE_PENDING_BLOB, {"items": clean})
    return clean


def _state_save_pending_items(store: UserStore, items: list[dict]) -> None:
    db.set_blob(store.user_id, _STATE_PENDING_BLOB, {"items": items[:10], "updated_at": core_util._now_iso()})


def _state_add_pending(store: UserStore, runtime_actions: list[dict], *, user_message_id: str, prompt: str) -> list[dict]:
    existing = _state_pending_items(store)
    now = time.time()
    pending: list[dict] = []
    for runtime_action in runtime_actions[:5]:
        item = {
            "id": f"rta_{uuid.uuid4().hex[:12]}",
            "created_at": core_util._now_iso(),
            "expires_at": now + 86400,
            "source": "model_api_chat",
            "user_message_id": user_message_id,
            "prompt": prompt[:1000],
            "runtime_action": runtime_action,
        }
        pending.append(item)
    _state_save_pending_items(store, pending + existing)
    return pending


def _state_clear_pending(store: UserStore, pending_ids: set[str]) -> None:
    if not pending_ids:
        return
    items = [item for item in _state_pending_items(store) if str(item.get("id") or "") not in pending_ids]
    _state_save_pending_items(store, items)


def _append_state_receipt(store: UserStore, receipt: dict) -> dict:
    record = {
        "id": receipt.get("id") or f"sr_{uuid.uuid4().hex[:14]}",
        "ts": time.time(),
        "created_at": core_util._now_iso(),
        "source": "model_api_chat",
        "status": str(receipt.get("status") or "ok")[:80],
    }
    for key in (
        "background_execution", "results", "effects", "pending", "error",
        "user_message_id", "assistant_message_id", "summary",
    ):
        if key in receipt:
            record[key] = receipt[key]
    db.log_append(store.user_id, "state_receipts", record, ts=record["ts"], item_key=record["id"])
    db.log_trim(store.user_id, "state_receipts", STATE_RECEIPT_MAX)
    return record


def _load_state_receipts(store: UserStore, limit: int = 30) -> list[dict]:
    entries = db.log_read(store.user_id, "state_receipts", limit=max(1, min(limit, 100)))
    entries.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    return entries


def _state_memory_candidate_from_moment(moment: dict, inner: dict, score: float) -> dict:
    return {
        "id": str(moment.get("id") or ""),
        "type": str(moment.get("type") or inner.get("type") or "fact"),
        "title": str(inner.get("title") or "")[:220],
        "description": str(inner.get("description") or "")[:1000],
        "her_quote": str(inner.get("her_quote") or "")[:500],
        "context": str(inner.get("context") or "")[:500],
        "occurred_at": str(moment.get("occurred_at") or ""),
        "created_at": str(moment.get("created_at") or ""),
        "updated_at": str(moment.get("updated_at") or ""),
        "source": str(moment.get("source") or ""),
        "score": round(float(score), 4),
    }


def _model_api_state_memory_candidates(
    store: UserStore,
    api_key: str | None,
    message: str,
    context_refs: list[dict],
    *,
    limit: int = 12,
) -> list[dict]:
    ref_ids = {
        str(ref.get("id") or "")
        for ref in context_refs
        if ref.get("type") == "memory" and ref.get("id")
    }
    candidates: list[dict] = []
    for moment in memory_service._active_memory_moments(memory_service._load_moments(store)):
        if not isinstance(moment, dict):
            continue
        inner, err = memory_actions_mod._memory_plain_from_envelope(moment, api_key)
        if inner is None:
            continue
        merged = {
            **inner,
            "id": moment.get("id", ""),
            "type": moment.get("type") or inner.get("type"),
            "source": moment.get("source", ""),
            "occurred_at": moment.get("occurred_at", ""),
            "created_at": moment.get("created_at", ""),
        }
        if moment.get("id") in ref_ids:
            details = {
                "score": 1.0,
                "confidence": "strong",
                "reason": "user_selected_context_ref",
                "matched_units": [],
            }
        else:
            details = memory_relevance_details(message, merged)
        score = float(details.get("score") or 0.0)
        confidence = str(details.get("confidence") or "none")
        if moment.get("id") in ref_ids or (confidence in {"strong", "medium"} and score >= 0.35):
            candidate = _state_memory_candidate_from_moment(moment, inner, score)
            candidate["confidence"] = confidence
            candidate["reason"] = str(details.get("reason") or "")[:120]
            candidate["matched_units"] = list(details.get("matched_units") or [])[:8]
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item.get("score", 0), item.get("occurred_at", "")), reverse=True)
    return candidates[:limit]


def _model_api_plan_state_actions(
    store: UserStore,
    api_key: str | None,
    runtime: provider_client.ProviderConfig,
    message: str,
    context_refs: list[dict],
    identity: dict,
) -> dict:
    pending_items = _state_pending_items(store)
    memory_candidates = _model_api_state_memory_candidates(store, api_key, message, context_refs)
    memory_terms = _model_api_existing_memory_terms(store, api_key)
    raw_actions: list[dict] = []
    runtime_error = ""
    parsed: dict = {}
    try:
        result = provider_client.chat_completion(
            runtime,
            build_hosted_runtime_background_execution_messages(
                user_message=message,
                identity=identity,
                memory_candidates=memory_candidates,
                context_refs=context_refs,
                pending_items=pending_items,
                memory_terms=memory_terms,
            ),
            max_tokens=900,
            temperature=0.0,
            timeout=35.0,
            response_format=HOSTED_RUNTIME_ACTION_RESPONSE_FORMAT,
        )
        parsed_json = core_util._json_from_model_text(str(result.get("reply") or ""))
        parsed = parsed_json if isinstance(parsed_json, dict) else {}
        if isinstance(parsed.get("actions"), list):
            raw_actions = [a for a in parsed.get("actions") if isinstance(a, dict)]
    except Exception as e:
        runtime_error = f"{type(e).__name__}:{str(e)[:240]}"

    pending_decision, pending_ids = coerce_hosted_runtime_pending_decision(parsed, pending_items)
    if pending_decision:
        id_set = set(pending_ids)
        if pending_decision == "reject":
            _state_clear_pending(store, id_set)
            return {
                "triggered": True,
                "method": HOSTED_RUNTIME_PENDING_REJECT_METHOD,
                "actions": [],
                "memory_candidates": memory_candidates,
                "pending_ids": pending_ids,
                "error": runtime_error,
            }
        confirmed: list[dict] = []
        for item in pending_items:
            if str(item.get("id") or "") not in id_set:
                continue
            runtime_action = item.get("runtime_action") if isinstance(item.get("runtime_action"), dict) else {}
            runtime_action = dict(runtime_action)
            runtime_action["requires_confirmation"] = False
            runtime_action["confirmed_pending_id"] = str(item.get("id") or "")
            confirmed.append(runtime_action)
        _state_clear_pending(store, id_set)
        return {
            "triggered": True,
            "method": HOSTED_RUNTIME_PENDING_CONFIRM_METHOD,
            "actions": confirmed,
            "memory_candidates": memory_candidates,
            "pending_ids": pending_ids,
            "error": runtime_error,
        }

    planned: list[dict] = []
    seen = set()
    for raw in raw_actions[:12]:
        coerced = coerce_hosted_runtime_action(
            raw,
            memory_candidates,
            direct_confidence=MODEL_API_STATE_DIRECT_CONFIDENCE,
        )
        if not coerced:
            continue
        key = json.dumps({
            "domain": coerced.get("domain"),
            "executor": coerced.get("executor_action"),
        }, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        if coerced.get("confidence", 0.0) < MODEL_API_STATE_CONFIRM_CONFIDENCE:
            continue
        planned.append(coerced)

    return {
        "triggered": bool(planned or raw_actions or pending_items or runtime_error),
        "method": HOSTED_RUNTIME_ACTION_METHOD if raw_actions else HOSTED_RUNTIME_NOOP_METHOD,
        "actions": planned[:8],
        "memory_candidates": memory_candidates,
        "error": runtime_error,
    }


def _execute_model_api_state_plan(
    store: UserStore,
    api_key: str | None,
    plan: dict,
    *,
    user_message_id: str,
    user_message: str,
) -> tuple[list[dict], list[dict], list[dict], dict | None]:
    runtime_actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    if not runtime_actions and not plan.get("pending_ids"):
        return [], [], [], None

    direct = [a for a in runtime_actions if not a.get("requires_confirmation")]
    needs_confirm = [a for a in runtime_actions if a.get("requires_confirmation")]
    pending = _state_add_pending(
        store,
        needs_confirm,
        user_message_id=user_message_id,
        prompt=user_message,
    ) if needs_confirm else []

    identity_actions: list[dict] = []
    memory_actions: list[dict] = []
    for planned in direct:
        executor_action = planned.get("executor_action") if isinstance(planned.get("executor_action"), dict) else {}
        if not executor_action:
            continue
        executor_action = dict(executor_action)
        executor_action.setdefault("state_action_id", planned.get("action_id", ""))
        if planned.get("domain") == "identity":
            identity_actions.append(executor_action)
        elif planned.get("domain") == "memory":
            executor_action.setdefault("source_chat_message_ids", [user_message_id])
            memory_actions.append(executor_action)

    effects: list[dict] = []
    identity_results: list[dict] = []
    memory_results: list[dict] = []
    status = "ok"
    error = ""
    if identity_actions:
        body, action_status = identity_actions_mod._execute_identity_actions(store, api_key, identity_actions)
        identity_results = body.get("results") or []
        effects.extend(body.get("effects") or [])
        if action_status >= 400:
            status = "failed"
            error = body.get("error", "identity_action_failed")
    if status == "ok" and memory_actions:
        body, action_status = memory_actions_mod._execute_memory_actions(store, api_key, memory_actions)
        memory_results = body.get("results") or []
        effects.extend(body.get("effects") or [])
        if action_status >= 400:
            status = "failed"
            error = body.get("error", "memory_action_failed")

    background_execution = hosted_runtime_background_trace(
        status=status,
        method=str(plan.get("method") or ""),
        triggered=bool(plan.get("triggered")),
        error=str(plan.get("error") or ""),
    )
    receipt = _append_state_receipt(store, {
        "status": status,
        "background_execution": {
            **background_execution,
            "pending_decision": plan.get("method", "") if "pending_" in str(plan.get("method", "")) else "",
        },
        "results": {
            "identity": identity_results,
            "memory": memory_results,
        },
        "effects": effects,
        "pending": [
            _state_pending_public_summary(item)
            for item in pending
        ],
        "error": error,
        "user_message_id": user_message_id,
    })
    return effects, identity_results, memory_results, receipt


def _state_receipt_prompt_payload(receipt: dict | None) -> dict:
    if not receipt:
        return {}
    return {
        "status": receipt.get("status", ""),
        "results": receipt.get("results", {}),
        "effects": receipt.get("effects", []),
        "pending": receipt.get("pending", []),
        "error": receipt.get("error", ""),
    }


def _state_preview_value(value, max_chars: int = 260):
    if isinstance(value, list):
        out = []
        for item in value[:5]:
            text = str(item or "").strip()
            if text:
                out.append(text[:max_chars])
        return out
    if isinstance(value, dict):
        return {
            str(k): _state_preview_value(v, max_chars)
            for k, v in list(value.items())[:8]
            if str(v or "").strip()
        }
    text = str(value or "").strip()
    return text[:max_chars]


def _state_pending_public_summary(item: dict) -> dict:
    runtime_action = item.get("runtime_action") if isinstance(item.get("runtime_action"), dict) else {}
    executor = runtime_action.get("executor_action") if isinstance(runtime_action.get("executor_action"), dict) else {}
    preview = runtime_action.get("target_preview") if isinstance(runtime_action.get("target_preview"), dict) else {}
    runtime_target = runtime_action.get("target") if isinstance(runtime_action.get("target"), dict) else {}
    action = str(runtime_action.get("runtime_type") or executor.get("type") or "")
    result = {
        "id": str(item.get("id") or ""),
        "action": action,
        "confidence": runtime_action.get("confidence", 0),
        "reason": str(runtime_action.get("reason") or executor.get("reason") or "")[:300],
    }
    if action.startswith("identity"):
        result["domain"] = "identity"
        result["target"] = "Identity"
        if executor.get("type") == "identity.dimension_nudge":
            result["changes"] = [{
                "field": "dimension",
                "dimension": str(executor.get("dimension") or ""),
                "delta": executor.get("delta"),
            }]
        else:
            patch = executor.get("patch") if isinstance(executor.get("patch"), dict) else {}
            result["changes"] = [
                {"field": str(k), "to": _state_preview_value(v)}
                for k, v in patch.items()
            ][:8]
        return result

    result["domain"] = "memory"
    if executor.get("type") in {"memory.add", "memory.add_correction"}:
        memory = executor.get("memory") if isinstance(executor.get("memory"), dict) else {}
        result["target"] = str(memory.get("title") or "New Memory Garden card")[:180]
        result["new_memory"] = {
            "type": str(memory.get("type") or "")[:80],
            "title": str(memory.get("title") or "")[:180],
            "description": str(memory.get("description") or "")[:600],
        }
        return result

    memory_id = str(executor.get("memory_id") or runtime_target.get("memory_id") or "")
    result["memory_id"] = memory_id
    result["target"] = str(preview.get("title") or memory_id or "Memory Garden card")[:180]
    if preview:
        result["current_memory"] = {
            "title": str(preview.get("title") or "")[:180],
            "description": str(preview.get("description") or "")[:600],
            "type": str(preview.get("type") or "")[:80],
            "occurred_at": str(preview.get("occurred_at") or "")[:80],
        }
    if executor.get("type") == "memory.delete":
        result["changes"] = [{"field": "delete", "to": "remove this card"}]
        return result
    patch = executor.get("patch") if isinstance(executor.get("patch"), dict) else {}
    result["changes"] = [
        {"field": str(k), "to": _state_preview_value(v, 600)}
        for k, v in patch.items()
    ][:8]
    return result


def _pending_state_confirmation_messages(
    user_message: str,
    pending: list[dict],
    identity: dict,
) -> list[dict]:
    payload = {
        "latest_user_message": user_message[:2000],
        "identity": {
            "agent_name": str(identity.get("agent_name") or ""),
            "tone_style": str(identity.get("tone_style") or ""),
            "signature": identity.get("signature") if isinstance(identity.get("signature"), list) else [],
            "language_preference": str(identity.get("language_preference") or ""),
        },
        "pending_updates": [_state_pending_public_summary(item) for item in pending[:5]],
        "allowed_user_replies": ["确认", "取消", "confirm", "cancel", "or a natural correction"],
    }
    return build_model_api_pending_confirmation_messages(payload)


def _model_api_pending_confirmation_reply(
    runtime: provider_client.ProviderConfig,
    user_message: str,
    pending: list[dict],
    identity: dict,
) -> tuple[str, str]:
    if not pending:
        return "", ""
    try:
        result = provider_client.chat_completion(
            runtime,
            _pending_state_confirmation_messages(user_message, pending, identity),
            max_tokens=700,
            temperature=0.7,
            timeout=45.0,
            response_format=HOSTED_RUNTIME_ACTION_RESPONSE_FORMAT,
        )
        reply, thinking = _model_api_parse_turn_reply(str(result.get("reply") or ""))
        reply = reply.strip()
        if not reply:
            return "", ""
        banned = (
            "我找到了可能要修改",
            "可能要修改的身份或记忆",
            "需要你确认",
            "I found a likely identity or memory update",
            "I found a possible identity or memory change",
        )
        if any(phrase in reply for phrase in banned):
            return "", ""
        return reply, thinking
    except Exception:
        return "", ""


def _append_model_api_runtime_followup_message(
    store: UserStore,
    *,
    reply: str,
    thinking_summary: str = "",
    push_data: dict | None = None,
) -> dict | None:
    text = str(reply or "").strip()
    if not text:
        return None
    assistant_env, env_err = core_envelope._build_shared_envelope_for_store(store, text.encode("utf-8"))
    if assistant_env is None:
        print(f"[model_api_state:{store.user_id}] followup_envelope_failed detail={env_err}")
        return None
    extra: dict = {}
    thinking = str(thinking_summary or "").strip()
    if thinking:
        thinking_env, thinking_err = core_envelope._build_shared_envelope_for_store(store, thinking.encode("utf-8"))
        if thinking_env is not None:
            extra.update(chat_service._chat_thinking_extra_from_envelope(thinking_env))
        else:
            print(f"[model_api_state:{store.user_id}] followup_thinking_envelope_failed detail={thinking_err}")
    row = store.append_chat("openclaw", "model_api", assistant_env, extra=extra)
    store.notify_chat_waiters()
    delivery_fields = push_service._deliver_ai_message_push_if_background(
        store,
        body=text,
        title="IO",
        data=push_data or {"source": "model_api_state"},
        visual_state="reply",
    )
    updated = store.update_chat_message_metadata(row["id"], delivery_fields)
    return updated or row


def _run_model_api_state_action_job(
    store: UserStore,
    api_key: str | None,
    runtime: provider_client.ProviderConfig,
    trace_id: str,
    *,
    user_message: str,
    user_message_id: str,
    assistant_message_id: str,
    context_refs: list[dict],
) -> None:
    started = time.time()
    try:
        hosted_config_store._patch_model_api_action_trace(store, trace_id, {
            "status": "processing",
            "progress": 20,
        })
        identity_for_plan = {}
        identity_plan_data, _ = core_enclave._enclave_get_json_for_gate("/v1/identity/get", api_key)
        if isinstance(identity_plan_data, dict) and isinstance(identity_plan_data.get("identity"), dict):
            identity_for_plan = identity_plan_data["identity"]
        state_plan = _model_api_plan_state_actions(
            store,
            api_key,
            runtime,
            user_message,
            context_refs,
            identity_for_plan,
        )
        effects, identity_results, memory_results, state_receipt = _execute_model_api_state_plan(
            store,
            api_key,
            state_plan,
            user_message_id=user_message_id,
            user_message=user_message,
        )
        pending_items = _state_pending_items(store)
        just_pending = bool(state_receipt and (state_receipt.get("pending") or []) and not effects)
        followup_row = None
        if just_pending:
            reply, thinking_summary = _model_api_pending_confirmation_reply(
                runtime,
                user_message,
                pending_items,
                identity_for_plan,
            )
            if reply:
                followup_row = _append_model_api_runtime_followup_message(
                    store,
                    reply=reply,
                    thinking_summary=thinking_summary,
                    push_data={"source": "model_api_state", "kind": "pending_confirmation"},
                )
        if state_receipt and state_receipt.get("status") == "failed":
            background_execution = hosted_runtime_background_trace(
                status="failed",
                method=str(state_plan.get("method") or ""),
                triggered=bool(state_plan.get("triggered")),
                error=str(state_plan.get("error") or state_receipt.get("error") or ""),
            )
            hosted_config_store._patch_model_api_action_trace(store, trace_id, {
                "status": "failed",
                "progress": 100,
                "provider": runtime.provider,
                "model": runtime.model,
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
                "state_receipt_id": state_receipt.get("id", ""),
                "background_execution": background_execution,
                "effects": effects,
                "identity_actions": identity_results,
                "memory_actions": memory_results,
                "error": state_receipt.get("error", "state_action_failed"),
                "duration_ms": int((time.time() - started) * 1000),
            })
            return
        if not effects and not just_pending and not state_plan.get("pending_ids"):
            status = "skipped"
            reason = "no_state_action"
        elif just_pending:
            status = "pending_confirmation"
            reason = "needs_user_confirmation"
        else:
            status = "completed"
            reason = "state_actions_applied"
        background_execution = hosted_runtime_background_trace(
            status=status,
            method=str(state_plan.get("method") or ""),
            triggered=bool(state_plan.get("triggered")),
            error=str(state_plan.get("error") or ""),
        )
        hosted_config_store._patch_model_api_action_trace(store, trace_id, {
            "status": status,
            "progress": 100,
            "provider": runtime.provider,
            "model": runtime.model,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "state_receipt_id": (state_receipt or {}).get("id", ""),
            "background_execution": background_execution,
            "effects": effects,
            "identity_actions": identity_results,
            "memory_actions": memory_results,
            "context": {"context_refs": len(context_refs)},
            "reason": reason,
            "duration_ms": int((time.time() - started) * 1000),
        })
        if followup_row:
            hosted_config_store._patch_model_api_action_trace(store, trace_id, {
                "assistant_message_id": followup_row.get("id", assistant_message_id),
            })
    except Exception as e:
        hosted_config_store._patch_model_api_action_trace(store, trace_id, {
            "status": "failed",
            "progress": 100,
            "provider": runtime.provider,
            "model": runtime.model,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "error": f"{type(e).__name__}:{str(e)[:300]}",
            "duration_ms": int((time.time() - started) * 1000),
        })
    finally:
        with _model_api_state_active_lock:
            _model_api_state_active_users.discard(store.user_id)


def _start_model_api_state_action_job(
    store: UserStore,
    api_key: str | None,
    runtime: provider_client.ProviderConfig,
    *,
    user_message: str,
    user_message_id: str,
    assistant_message_id: str,
    context_refs: list[dict],
    run_sync: bool = False,
) -> dict:
    with _model_api_state_active_lock:
        if store.user_id in _model_api_state_active_users:
            return {
                "status": "skipped",
                "reason": "background_execution_already_running",
                "actions_written": 0,
            }
        _model_api_state_active_users.add(store.user_id)
    trace = hosted_config_store._append_model_api_action_trace(store, {
        "status": "queued",
        "provider": runtime.provider,
        "model": runtime.model,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "background_execution": hosted_runtime_background_trace(
            status="queued",
            method=HOSTED_RUNTIME_BACKGROUND_METHOD,
        ),
        "context": {"context_refs": len(context_refs)},
        "reason": "queued_after_foreground_reply",
        "progress": 0,
    })
    args = (store, api_key, runtime, trace["trace_id"])
    kwargs = {
        "user_message": user_message,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "context_refs": context_refs,
    }
    if run_sync:
        _run_model_api_state_action_job(*args, **kwargs)
        latest = db.log_patch_item(
            store.user_id,
            hosted_config_store.MODEL_API_ACTION_TRACE_STREAM,
            trace["trace_id"],
            {},
        )
        return latest or trace
    thread = threading.Thread(
        target=_run_model_api_state_action_job,
        args=args,
        kwargs=kwargs,
        daemon=True,
    )
    thread.start()
    return trace


def _model_api_turn_contract_message() -> dict:
    return hosted_runtime_companion_turn_contract_message()



def _model_api_extract_web_search_requests(parsed: Any) -> list[dict]:
    return extract_model_api_web_search_requests(
        parsed,
        enabled=MODEL_API_WEB_SEARCH_ENABLED,
        max_queries=MODEL_API_WEB_SEARCH_MAX_QUERIES,
    )


def _model_api_parse_turn_output(raw_reply: str) -> tuple[str, str, list[dict]]:
    raw = str(raw_reply or "").strip()
    if not raw:
        return "", "", []
    try:
        parsed = core_util._json_from_model_text(raw)
    except Exception:
        return raw, "", []

    def text_from(value) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("reply", "response", "content", "text", "result", "message", "answer"):
                nested = text_from(value.get(key))
                if nested:
                    return nested
        if isinstance(value, list):
            parts = [text_from(item) for item in value]
            return "\n".join(part for part in parts if part).strip()
        return ""

    if isinstance(parsed, dict):
        reply = ""
        for key in ("reply", "response", "content", "text", "result", "message", "answer"):
            reply = text_from(parsed.get(key))
            if reply:
                break
        thinking = ""
        for key in (
            "context_summary",
            "runtime_summary",
            "action_summary",
            "thinking_summary",
            "reasoning_summary",
            "thought_summary",
            "visible_reasoning",
        ):
            thinking = _sanitize_visible_thinking_summary(str(parsed.get(key) or ""))
            if thinking and _model_api_is_generic_context_summary(thinking):
                thinking = ""
            if thinking:
                break
        return reply or "", thinking, _model_api_extract_web_search_requests(parsed)
    if isinstance(parsed, list):
        reply = text_from(parsed)
        return reply or raw, "", []
    return raw, "", []


def _model_api_parse_turn_reply(raw_reply: str) -> tuple[str, str]:
    reply, thinking, _ = _model_api_parse_turn_output(raw_reply)
    return reply, thinking


def _model_api_is_generic_context_summary(text: str) -> bool:
    lines = [
        line.strip(" 。.").strip()
        for line in str(text or "").splitlines()
        if line.strip()
    ]
    if not lines:
        return True
    generic_patterns = [
        r"^参考了\s*\d+\s*条相关记忆$",
        r"^对齐了当前\s*Identity\s*设定$",
        r"^整理了这轮消息后回复$",
        r"^Checked\s+\d+\s+relevant memories$",
        r"^Used the current identity card$",
        r"^Read this turn and composed the reply$",
    ]
    return all(
        any(re.search(pattern, line, re.IGNORECASE) for pattern in generic_patterns)
        for line in lines
    )


def _model_api_capture_prompt(user_message: str, assistant_reply: str, context_payload: dict) -> list[dict]:
    return build_model_api_memory_capture_messages(
        user_message=user_message,
        assistant_reply=assistant_reply,
        context_payload=context_payload,
    )


def _model_api_existing_memory_terms(store: UserStore, api_key: str | None) -> dict[str, list[str]]:
    try:
        from memory import memory_core
        buckets, threads = memory_core.existing_terms_via_api_key(store, api_key)
    except Exception:
        buckets, threads = [], []
    return {
        "buckets": [str(item) for item in buckets[:80] if str(item or "").strip()],
        "threads": [str(item) for item in threads[:80] if str(item or "").strip()],
    }


def _model_api_context_with_memory_terms(
    store: UserStore,
    api_key: str | None,
    context_payload: dict,
) -> dict:
    out = dict(context_payload or {})
    out.setdefault("existing_memory_terms", _model_api_existing_memory_terms(store, api_key))
    return out


def _model_api_run_memory_capture(
    store: UserStore,
    api_key: str | None,
    runtime: provider_client.ProviderConfig,
    *,
    user_message: str,
    assistant_reply: str,
    user_message_id: str,
    assistant_message_id: str,
    context_payload: dict,
    job_id: str | None = None,
) -> dict:
    job_base = {
        "mode": "running",
        "source_chat_message_ids": [user_message_id, assistant_message_id],
        "message_chars": len(user_message),
        "reply_chars": len(assistant_reply),
    }

    def finish(entry: dict) -> dict:
        # Single exit for every return path — release the per-user capture guard
        # here so it is cleared no matter which branch returns. Idempotent.
        with _model_api_capture_active_lock:
            _model_api_capture_active_users.discard(store.user_id)
        if job_id:
            patched = _model_api_patch_recap_job(store, job_id, {**job_base, **entry})
            return patched or {**job_base, **entry, "job_id": job_id}
        return memory_service._append_memory_capture_job(store, {**job_base, **entry})

    if os.environ.get("FEEDLING_MODEL_API_MEMORY_CAPTURE", "1").strip().lower() in {"0", "false", "off", "no"}:
        return finish({"status": "skipped", "error": "disabled"})
    try:
        capture_context = _model_api_context_with_memory_terms(store, api_key, context_payload)
        result = provider_client.chat_completion(
            runtime,
            _model_api_capture_prompt(user_message, assistant_reply, capture_context),
            max_tokens=900,
            temperature=0.1,
            timeout=30.0,
        )
        raw = core_util._json_from_model_text(str(result.get("reply") or ""))
        memories = raw.get("memories") if isinstance(raw, dict) else []
        if not isinstance(memories, list):
            memories = []
        actions: list[dict] = []
        seen_titles = set()
        for item in memories[:4]:
            if not isinstance(item, dict):
                continue
            summary = memory_actions_mod._memory_action_text(
                item.get("summary") or item.get("title") or item.get("description"),
                180,
            )
            content = str(item.get("content") or item.get("description") or summary).strip()[:2000]
            if len(summary) < 4 or not content:
                continue
            key = summary.lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            actions.append({
                "type": "memory.add",
                "memory": {
                    "summary": summary,
                    "content": content,
                    "bucket": memory_actions_mod._memory_action_text(item.get("bucket") or "未分类", 120),
                    "threads": item.get("threads") if isinstance(item.get("threads"), list) else [],
                    "importance": item.get("importance", 0.5),
                    "pulse": item.get("pulse", 0.3),
                    "occurred_at": memory_actions_mod._memory_action_text(item.get("occurred_at") or date.today().isoformat(), 80),
                    "source": str(item.get("source") or "model_api_capture")[:80],
                },
                "reason": "Captured from recent chat.",
                "capture_mode": "running",
                "source_chat_message_ids": [user_message_id, assistant_message_id],
            })
        if not actions:
            return finish({"status": "completed", "actions_planned": 0, "actions_written": 0})
        body, status = memory_actions_mod._execute_memory_actions(store, api_key, actions)
        if status >= 400:
            return finish({
                "status": "failed",
                "actions_planned": len(actions),
                "actions_written": len(body.get("effects") or []),
                "effects": body.get("effects") or [],
                "error": body.get("error", "memory_action_failed"),
            })
        return finish({
            "status": "completed",
            "actions_planned": len(actions),
            "actions_written": len(body.get("effects") or []),
            "effects": body.get("effects") or [],
        })
    except Exception as e:
        return finish({
            "status": "failed",
            "error": f"{type(e).__name__}:{str(e)[:300]}",
        })


def _start_model_api_memory_capture_job(
    store: UserStore,
    api_key: str | None,
    runtime: provider_client.ProviderConfig,
    *,
    user_message: str,
    assistant_reply: str,
    user_message_id: str,
    assistant_message_id: str,
    context_payload: dict,
    turn_count: int,
    run_sync: bool = False,
) -> dict:
    # Per-user guard: skip if a capture for this user is already in flight, so
    # overlapping cadence windows can't double-write cards. Released in the
    # runner's finish(). (Mirrors _start_model_api_recap_job.)
    with _model_api_capture_active_lock:
        if store.user_id in _model_api_capture_active_users:
            return {
                "status": "skipped",
                "mode": "running",
                "reason": "capture_already_running",
                "turn_count": turn_count,
                "actions_written": 0,
            }
        _model_api_capture_active_users.add(store.user_id)
    # From here the guard is held; the runner's finish() releases it on every
    # return path. But if we fail to hand off to the runner (job append or thread
    # start raises), finish() never runs — release here so the user isn't wedged
    # out of all future captures.
    try:
        job = memory_service._append_memory_capture_job(store, {
            "mode": "running",
            "status": "queued",
            "reason": f"cadence:{MODEL_API_CAPTURE_TURN_INTERVAL}",
            "turn_count": turn_count,
            "progress": 0,
            "source_chat_message_ids": [user_message_id, assistant_message_id],
            "message_chars": len(user_message),
            "reply_chars": len(assistant_reply),
        })
        kwargs = {
            "user_message": user_message,
            "assistant_reply": assistant_reply,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "context_payload": context_payload,
            "job_id": job["job_id"],
        }
        if run_sync:
            return _model_api_run_memory_capture(store, api_key, runtime, **kwargs)
        thread = threading.Thread(
            target=_model_api_run_memory_capture,
            args=(store, api_key, runtime),
            kwargs=kwargs,
            daemon=True,
        )
        thread.start()
        return job
    except Exception:
        with _model_api_capture_active_lock:
            _model_api_capture_active_users.discard(store.user_id)
        raise


def _model_api_latest_recap_job(store: UserStore) -> dict | None:
    jobs = [
        job for job in db.log_read(store.user_id, "memory_capture_jobs", limit=60)
        if isinstance(job, dict) and str(job.get("mode") or "") == "recap"
    ]
    if not jobs:
        return None
    jobs.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    return jobs[0]


def _model_api_recent_recap_chat(store: UserStore, api_key: str | None, limit: int = 160) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    hist, hist_err = core_enclave._enclave_get_json_for_gate(
        "/v1/chat/history",
        api_key,
        {
            "limit": str(max(20, min(limit, 200))),
            "include_image_body": "false",
            "context_mode": "model_api",
        },
    )
    if hist_err:
        warnings.append(f"history_read:{hist_err[:180]}")
    raw_messages = hist.get("messages") if isinstance(hist, dict) and isinstance(hist.get("messages"), list) else []
    messages: list[dict] = []
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        content = str(msg.get("content") or "").strip()
        if not content or hosted_history_import._looks_like_import_artifact(content):
            continue
        role = "user" if msg.get("role") == "user" else "assistant"
        source = str(msg.get("source") or "")
        if source and source != "model_api":
            continue
        messages.append({
            "id": str(msg.get("id") or ""),
            "role": role,
            "content": content[:4000],
            "ts": msg.get("ts"),
            "source": "model_api_live",
        })
    return messages, warnings


def _model_api_plain_memory_cards(store: UserStore, api_key: str | None) -> list[dict]:
    cards: list[dict] = []
    for moment in memory_service._active_memory_moments(memory_service._load_moments(store)):
        if not isinstance(moment, dict):
            continue
        inner, _ = memory_actions_mod._memory_plain_from_envelope(moment, api_key)
        if inner is None:
            continue
        card = {
            "id": str(moment.get("id") or ""),
            "type": str(moment.get("type") or inner.get("type") or "fact"),
            "title": str(inner.get("title") or "")[:180],
            "description": str(inner.get("description") or "")[:2000],
            "occurred_at": str(moment.get("occurred_at") or ""),
            "created_at": str(moment.get("created_at") or ""),
            "source": str(moment.get("source") or ""),
        }
        for key in ("her_quote", "context", "linked_dimension"):
            value = str(inner.get(key) or "").strip()
            if value:
                card[key] = value
        cards.append(card)
    return cards


def _memory_quality_card_issues(
    moment: dict,
    inner: dict,
    *,
    archive_language: str = "",
) -> list[str]:
    title = str(inner.get("title") or "").strip()
    desc = str(inner.get("description") or "").strip()
    context = str(inner.get("context") or "").strip()
    mem_type = str(moment.get("type") or inner.get("type") or "fact")
    issues: list[str] = []
    if hosted_history_import._GENERIC_IMPORT_TITLE_RE.match(title):
        issues.append("generic_import_title")
    if any(hosted_history_import._looks_like_import_artifact(value) for value in (title, desc, context) if value):
        issues.append("raw_import_artifact")
    if hosted_history_import._looks_like_low_value_import_card(title, desc, mem_type):
        issues.append("low_value_content")
    if str(archive_language).lower().startswith("zh") and desc and hosted_history_import._english_only_for_zh(title + "\n" + desc):
        issues.append("language_mismatch")
    return list(dict.fromkeys(issues))


def _model_api_memory_quality_scan(
    store: UserStore,
    *,
    api_key: str | None,
    max_cards: int = 1000,
    fast: bool = False,
) -> dict:
    archive_language = registry._get_user_archive_language(store.user_id) or ""
    moments = memory_service._active_memory_moments(memory_service._load_moments(store))
    scanned: list[dict] = []
    noisy: list[dict] = []
    decrypt_errors = 0
    for moment in moments[:max(1, max_cards)]:
        if not isinstance(moment, dict):
            continue
        inner, err = memory_actions_mod._memory_plain_from_envelope(moment, api_key)
        if inner is None:
            decrypt_errors += 1
            continue
        issues = _memory_quality_card_issues(moment, inner, archive_language=archive_language)
        card = {
            "id": str(moment.get("id") or ""),
            "type": str(moment.get("type") or inner.get("type") or ""),
            "title": str(inner.get("title") or "")[:180],
            "description": str(inner.get("description") or "")[:1200],
            "occurred_at": str(moment.get("occurred_at") or ""),
            "created_at": str(moment.get("created_at") or ""),
            "source": str(moment.get("source") or ""),
            "issues": issues,
        }
        scanned.append(card)
        if issues:
            noisy.append(card)

    duplicate_ids: set[str] = set()
    seen: list[tuple[str, set[str], str]] = []
    for card in scanned:
        desc = str(card.get("description") or "")
        tokens = hosted_history_import._memory_similarity_tokens(desc)
        norm = hosted_history_import._normalize_card_similarity_text(desc)
        if not norm:
            continue
        for prev_norm, prev_tokens, prev_id in seen[-250 if fast else -800:]:
            if norm == prev_norm or norm[:120] == prev_norm[:120] or hosted_history_import._token_jaccard(tokens, prev_tokens) >= 0.72:
                duplicate_ids.add(str(card.get("id") or ""))
                break
        seen.append((norm, tokens, str(card.get("id") or "")))

    by_id = {str(item.get("id") or ""): item for item in noisy}
    for card in scanned:
        cid = str(card.get("id") or "")
        if cid in duplicate_ids and cid not in by_id:
            copy_card = dict(card)
            copy_card["issues"] = list(dict.fromkeys((copy_card.get("issues") or []) + ["duplicate_or_near_duplicate"]))
            by_id[cid] = copy_card
    noisy = list(by_id.values())
    noisy.sort(key=lambda item: (len(item.get("issues") or []), item.get("created_at") or ""), reverse=True)
    issue_count = sum(len(item.get("issues") or []) for item in noisy)
    warning = None
    if noisy:
        warning = "memory_quality_warning"
    return {
        "scanned": len(scanned),
        "total_active": len(moments),
        "decrypt_errors": decrypt_errors,
        "issue_count": issue_count,
        "noisy_count": len(noisy),
        "duplicate_count": len(duplicate_ids),
        "warning": warning,
        "noisy_ids": [str(item.get("id") or "") for item in noisy if item.get("id")],
        "issues": noisy[:40],
    }


def _archive_model_api_memory_cards(
    store: UserStore,
    memory_ids: list[str],
    *,
    reason: str,
    job_id: str,
) -> int:
    target_ids = {str(mid) for mid in memory_ids if str(mid).strip()}
    if not target_ids:
        return 0
    # Hold memory_lock across load→archive→save (this background repair job is a
    # concurrent writer vs foreground memory.add — the exact collision the plain
    # Lock masked under Flask -w1). RLock lets _append_memory_change re-enter.
    archived = 0
    now = core_util._now_iso()
    with memory_service.mutation_lock(store):
        moments = memory_service._load_moments(store)
        for idx, moment in enumerate(moments):
            if not isinstance(moment, dict) or str(moment.get("id") or "") not in target_ids:
                continue
            if memory_service._memory_is_archived(moment):
                continue
            updated = dict(moment)
            updated["is_archived"] = True
            updated["archived_at"] = now
            updated["archive_reason"] = reason[:300]
            updated["archived_by_repair_job"] = job_id
            updated["updated_at"] = now
            moments[idx] = updated
            archived += 1
            memory_service._append_memory_change(store, {
                "action": "archive",
                "memory_id": str(moment.get("id") or ""),
                "type": str(moment.get("type") or ""),
                "reason": reason,
            })
        if archived:
            memory_service._save_moments(store, moments)
    return archived


def _model_api_repair_material_messages(
    store: UserStore,
    api_key: str | None,
    *,
    noisy_ids: set[str],
) -> tuple[list[dict], list[dict], list[str]]:
    messages, warnings = _model_api_recent_recap_chat(store, api_key, limit=200)
    good_cards = [
        card for card in _model_api_plain_memory_cards(store, api_key)
        if str(card.get("id") or "") not in noisy_ids
        and not _memory_quality_card_issues(
            {"type": card.get("type", ""), "source": card.get("source", "")},
            card,
            archive_language=registry._get_user_archive_language(store.user_id) or "",
        )
    ]
    support_messages: list[dict] = []
    for card in good_cards[:80]:
        support_messages.append({
            "role": "user",
            "content": (
                f"Existing readable Memory Garden card ({card.get('type')}): "
                f"{card.get('title')}\n{card.get('description')}"
            ),
            "ts": None,
            "source": "persona_import",
        })
    return support_messages + messages, good_cards, warnings


def _run_model_api_memory_repair_job(
    store: UserStore,
    api_key: str | None,
    runtime: provider_client.ProviderConfig,
    job_id: str,
    *,
    noisy_ids: list[str],
    archive_old: bool,
) -> None:
    warnings: list[str] = []
    try:
        _model_api_patch_recap_job(store, job_id, {"status": "processing", "progress": 8})
        noisy_set = {str(mid) for mid in noisy_ids if str(mid).strip()}
        messages, good_cards, material_warnings = _model_api_repair_material_messages(
            store,
            api_key,
            noisy_ids=noisy_set,
        )
        warnings.extend(material_warnings)
        if len(messages) < 4 and len(good_cards) < 2:
            _model_api_patch_recap_job(store, job_id, {
                "status": "failed",
                "progress": 100,
                "error": "not_enough_repair_material",
                "warnings": warnings,
            })
            return

        language = hosted_history_import._import_language_for_store(store, messages)
        days = identity_service._relationship_age_days(store)
        relationship_start = date.today() - timedelta(days=max(0, days))
        windows = hosted_history_import._build_transcript_windows(messages, max_chars=14000, max_windows=8)
        target_total = max(6, min(30, len(noisy_set) or 12))
        story_target = max(2, min(10, target_total // 3))
        about_target = max(3, target_total - story_target)

        def progress(done: int, total: int, candidate_count: int) -> None:
            _model_api_patch_recap_job(store, job_id, {
                "status": "processing",
                "progress": 12 + int(50 * done / max(total, 1)),
                "candidate_windows_done": done,
                "candidate_windows_total": total,
                "candidates_extracted": candidate_count,
                "messages_reviewed": len(messages),
            })

        candidates, provider_warnings = hosted_history_import._extract_memory_candidates_with_provider(
            runtime,
            windows,
            relationship_start,
            per_window_target=4,
            language=language,
            on_progress=progress,
        )
        warnings.extend(provider_warnings)
        cards = hosted_history_import._render_candidates_to_memory_cards(
            candidates,
            relationship_start,
            {
                "story": story_target,
                "about_me": about_target,
                "ta_thinking": 0,
                "total": target_total,
            },
            language=language,
            max_cards=target_total,
        )
        cards = [
            card for card in cards
            if str(card.get("type") or "") in {"moment", "quote", "fact", "event"}
        ]
        new_cards = hosted_history_import._new_cards_only(good_cards, cards)[:target_total]
        actions = [
            {
                "type": "memory.add",
                "memory": {
                    **card,
                    "source": "model_api_repair",
                    "context": (
                        str(card.get("context") or "").strip()
                        or f"re-distilled during hosted API memory repair job {job_id}"
                    )[:1000],
                },
                "reason": "Memory repair re-distilled readable cards from existing chat/material.",
                "capture_mode": "repair",
            }
            for card in new_cards
        ]
        _model_api_patch_recap_job(store, job_id, {
            "status": "processing",
            "progress": 72,
            "candidate_cluster_count": len(hosted_history_import._merge_import_candidates(candidates)),
            "new_cards_planned": len(actions),
            "memories_planned": len(actions),
            "warnings": warnings,
        })
        if not actions:
            _model_api_patch_recap_job(store, job_id, {
                "status": "failed",
                "progress": 100,
                "error": "no_replacement_cards_generated",
                "warnings": warnings,
            })
            return

        body, status = memory_actions_mod._execute_memory_actions(store, api_key, actions)
        effects = body.get("effects") or []
        if status >= 400:
            _model_api_patch_recap_job(store, job_id, {
                "status": "failed",
                "progress": 100,
                "error": body.get("error", "memory_action_failed"),
                "effects": effects,
                "warnings": warnings,
            })
            return
        archived = 0
        if archive_old:
            archived = _archive_model_api_memory_cards(
                store,
                list(noisy_set),
                reason="Archived by hosted API memory repair after replacement cards were written.",
                job_id=job_id,
            )
        _model_api_patch_recap_job(store, job_id, {
            "status": "completed",
            "progress": 100,
            "actions_planned": len(actions),
            "actions_written": len(effects),
            "new_cards_created": len(effects),
            "memories_created": len(effects),
            "old_cards_archived": archived,
            "effects": effects,
            "warnings": warnings,
        })
        hosted_config_store._patch_model_api_runtime_profile(store, {
            "last_repair_at": core_util._now_iso(),
            "memory_quality_warning": None if archived else "memory_quality_warning",
        })
    except Exception as e:
        _model_api_patch_recap_job(store, job_id, {
            "status": "failed",
            "progress": 100,
            "error": f"{type(e).__name__}:{str(e)[:300]}",
            "warnings": warnings,
        })


def _model_api_recap_due(store: UserStore, turn_count: int) -> tuple[bool, str]:
    if turn_count <= 0 or turn_count % MODEL_API_CONSOLIDATE_TURN_INTERVAL != 0:
        return False, f"cadence:{MODEL_API_CONSOLIDATE_TURN_INTERVAL}"
    if not _model_api_capture_enabled(store):
        return False, "capture_disabled"
    with _model_api_recap_active_lock:
        if store.user_id in _model_api_recap_active_users:
            return False, "recap_already_running"
    latest = _model_api_latest_recap_job(store)
    if latest:
        status = str(latest.get("status") or "")
        if status in {"queued", "processing"}:
            return False, "recap_already_running"
        elapsed = time.time() - float(latest.get("ts") or 0)
        if elapsed < MODEL_API_CONSOLIDATE_MIN_INTERVAL_SEC:
            return False, f"min_interval:{MODEL_API_CONSOLIDATE_MIN_INTERVAL_SEC}"
    return True, "recap_due"


def _model_api_capture_enabled(store: UserStore) -> bool:
    try:
        return bool(store.load_proactive_settings().get("capture_enabled", True))
    except Exception:
        return True


def _model_api_patch_recap_job(store: UserStore, job_id: str, patch: dict, *, only_if_status: str | None = None) -> dict | None:
    merged = dict(patch)
    if patch.get("status") in {"completed", "failed", "skipped"}:
        merged.setdefault("completed_at", core_util._now_iso())
    return db.log_patch_item(store.user_id, "memory_capture_jobs", job_id, merged, only_if_status=only_if_status)


def _run_model_api_recap_job(
    store: UserStore,
    api_key: str | None,
    runtime: provider_client.ProviderConfig,
    job_id: str,
    turn_count: int,
) -> None:
    try:
        _model_api_patch_recap_job(store, job_id, {
            "status": "processing",
            "progress": 8,
        })
        messages, warnings = _model_api_recent_recap_chat(store, api_key)
        if len(messages) < 12:
            _model_api_patch_recap_job(store, job_id, {
                "status": "skipped",
                "reason": "not_enough_recent_chat",
                "messages_reviewed": len(messages),
                "warnings": warnings,
            })
            return

        language = hosted_history_import._import_language_for_store(store, messages)
        days = identity_service._relationship_age_days(store)
        relationship_start = date.today() - timedelta(days=max(0, days))
        windows = hosted_history_import._build_transcript_windows(messages, max_chars=14000, max_windows=8)
        if not windows:
            _model_api_patch_recap_job(store, job_id, {
                "status": "skipped",
                "reason": "empty_windows",
                "messages_reviewed": len(messages),
                "warnings": warnings,
            })
            return

        def progress(done: int, total: int, candidate_count: int) -> None:
            _model_api_patch_recap_job(store, job_id, {
                "status": "processing",
                "progress": 10 + int(55 * done / max(total, 1)),
                "candidate_windows_done": done,
                "candidate_windows_total": total,
                "candidates_extracted": candidate_count,
                "messages_reviewed": len(messages),
            })

        candidates, provider_warnings = hosted_history_import._extract_memory_candidates_with_provider(
            runtime,
            windows,
            relationship_start,
            per_window_target=4,
            language=language,
            on_progress=progress,
        )
        warnings.extend(provider_warnings)
        cards = hosted_history_import._render_candidates_to_memory_cards(
            candidates,
            relationship_start,
            {
                "story": 3,
                "about_me": 5,
                "ta_thinking": 0,
                "total": 8,
            },
            language=language,
            max_cards=8,
        )
        cards = [card for card in cards if str(card.get("type") or "") in {"moment", "quote", "fact", "event"}]
        existing_cards = _model_api_plain_memory_cards(store, api_key)
        new_cards = hosted_history_import._new_cards_only(existing_cards, cards)[:8]
        source_ids = [str(msg.get("id") or "") for msg in messages if msg.get("id")]
        actions = [
            {
                "type": "memory.add",
                "memory": {
                    **card,
                    "source": "model_api_recap",
                    "context": (
                        str(card.get("context") or "").strip()
                        or f"distilled from recent hosted API chat recap at turn {turn_count}"
                    )[:1000],
                },
                "reason": "Periodic recap distilled this from recent hosted API chat.",
                "capture_mode": "recap",
                "source_chat_message_ids": source_ids[-80:],
            }
            for card in new_cards
        ]
        _model_api_patch_recap_job(store, job_id, {
            "status": "processing",
            "progress": 76,
            "candidate_cluster_count": len(hosted_history_import._merge_import_candidates(candidates)),
            "memories_planned": len(actions),
            "warnings": warnings,
        })
        if not actions:
            _model_api_patch_recap_job(store, job_id, {
                "status": "completed",
                "progress": 100,
                "actions_planned": 0,
                "actions_written": 0,
                "memories_created": 0,
                "messages_reviewed": len(messages),
                "warnings": warnings,
            })
            hosted_config_store._patch_model_api_runtime_profile(store, {"last_recap_at": core_util._now_iso()})
            return

        body, status = memory_actions_mod._execute_memory_actions(store, api_key, actions)
        effects = body.get("effects") or []
        if status >= 400:
            _model_api_patch_recap_job(store, job_id, {
                "status": "failed",
                "progress": 100,
                "actions_planned": len(actions),
                "actions_written": len(effects),
                "effects": effects,
                "error": body.get("error", "memory_action_failed"),
                "warnings": warnings,
            })
            return
        _model_api_patch_recap_job(store, job_id, {
            "status": "completed",
            "progress": 100,
            "actions_planned": len(actions),
            "actions_written": len(effects),
            "memories_created": len(effects),
            "effects": effects,
            "messages_reviewed": len(messages),
            "first_message_ts": messages[0].get("ts"),
            "latest_message_ts": messages[-1].get("ts"),
            "warnings": warnings,
        })
        hosted_config_store._patch_model_api_runtime_profile(store, {"last_recap_at": core_util._now_iso()})
    except Exception as e:
        _model_api_patch_recap_job(store, job_id, {
            "status": "failed",
            "progress": 100,
            "error": f"{type(e).__name__}:{str(e)[:300]}",
        })
    finally:
        with _model_api_recap_active_lock:
            _model_api_recap_active_users.discard(store.user_id)


def _start_model_api_recap_job(
    store: UserStore,
    api_key: str | None,
    runtime: provider_client.ProviderConfig,
    *,
    turn_count: int,
) -> dict:
    with _model_api_recap_active_lock:
        if store.user_id in _model_api_recap_active_users:
            return {
                "status": "skipped",
                "mode": "recap",
                "reason": "recap_already_running",
                "turn_count": turn_count,
                "actions_written": 0,
            }
        _model_api_recap_active_users.add(store.user_id)
    job = memory_service._append_memory_capture_job(store, {
        "mode": "recap",
        "status": "queued",
        "reason": f"cadence:{MODEL_API_CONSOLIDATE_TURN_INTERVAL}",
        "turn_count": turn_count,
        "progress": 0,
        "actions_written": 0,
    })
    thread = threading.Thread(
        target=_run_model_api_recap_job,
        args=(store, api_key, runtime, job["job_id"], turn_count),
        daemon=True,
    )
    thread.start()
    return job


def _model_api_maybe_run_memory_capture(
    store: UserStore,
    api_key: str | None,
    runtime: provider_client.ProviderConfig,
    *,
    user_message: str,
    assistant_reply: str,
    user_message_id: str,
    assistant_message_id: str,
    context_payload: dict,
    effects: list[dict],
    run_sync: bool = False,
) -> dict:
    turn_count = _model_api_turn_count(store)
    if not _model_api_capture_enabled(store):
        return {
            "status": "skipped",
            "reason": "capture_disabled",
            "turn_count": turn_count,
            "actions_written": 0,
        }
    has_state_write = any(
        str(effect.get("action") or "").startswith(("identity.", "memory."))
        or str(effect.get("type") or "").startswith(("identity_", "memory_"))
        for effect in effects
    )
    if has_state_write:
        return {
            "status": "skipped",
            "reason": "state_action_already_written",
            "turn_count": turn_count,
            "actions_written": 0,
        }
    cadence = turn_count > 0 and turn_count % MODEL_API_CAPTURE_TURN_INTERVAL == 0
    if not cadence:
        return {
            "status": "skipped",
            "reason": f"cadence:{MODEL_API_CAPTURE_TURN_INTERVAL}",
            "turn_count": turn_count,
            "actions_written": 0,
        }
    job = _start_model_api_memory_capture_job(
        store,
        api_key,
        runtime,
        user_message=user_message,
        assistant_reply=assistant_reply,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        context_payload=context_payload,
        turn_count=turn_count,
        run_sync=run_sync,
    )
    recap_due, recap_reason = _model_api_recap_due(store, turn_count)
    if recap_due:
        recap_job = _start_model_api_recap_job(store, api_key, runtime, turn_count=turn_count)
        job.setdefault("warnings", [])
        if isinstance(job["warnings"], list):
            job["warnings"].append("recap_queued")
        if isinstance(recap_job, dict):
            job["recap_job_id"] = recap_job.get("job_id", "")
            job["recap_status"] = recap_job.get("status", "")
    elif recap_reason not in {f"cadence:{MODEL_API_CONSOLIDATE_TURN_INTERVAL}", ""}:
        job.setdefault("warnings", [])
        if isinstance(job["warnings"], list):
            job["warnings"].append(recap_reason)
    return job


MODEL_API_MAX_IMAGE_BYTES = 2_000_000


def _model_api_image_payload(payload: dict) -> tuple[bytes | None, str, str | None]:
    raw = str(payload.get("image_b64") or payload.get("image_base64") or "").strip()
    if not raw:
        return None, "", None
    mime = str(payload.get("image_mime") or "image/jpeg").strip() or "image/jpeg"
    if not re.match(r"^image/(jpeg|jpg|png|webp)$", mime, re.I):
        return None, "", "image_mime must be image/jpeg, image/png, or image/webp"
    if raw.startswith("data:"):
        head, _, rest = raw.partition(",")
        raw = rest.strip()
        if head.startswith("data:image/") and ";" in head:
            mime = head.removeprefix("data:").split(";", 1)[0] or mime
    try:
        image_bytes = base64.b64decode(raw, validate=True)
    except Exception:
        return None, "", "image_b64 must be valid base64"
    if not image_bytes:
        return None, "", "image_b64 must not be empty"
    if len(image_bytes) > MODEL_API_MAX_IMAGE_BYTES:
        return None, "", f"image too large; max {MODEL_API_MAX_IMAGE_BYTES} bytes"
    return image_bytes, mime, None


def _model_api_user_content(message: str, images: list[dict[str, str]]) -> Any:
    text = message.strip() or "User sent an image."
    if not images:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for image in images:
        image_b64 = str(image.get("b64") or "").strip()
        if not image_b64:
            continue
        image_mime = str(image.get("mime") or "image/jpeg")
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_b64}"},
        })
    return parts


def _run_model_api_web_searches(requests_in: list[dict]) -> dict:
    return run_model_api_web_searches(
        requests_in,
        enabled=MODEL_API_WEB_SEARCH_ENABLED,
        max_queries=MODEL_API_WEB_SEARCH_MAX_QUERIES,
        max_results=MODEL_API_WEB_SEARCH_MAX_RESULTS,
        timeout_sec=MODEL_API_WEB_SEARCH_TIMEOUT_SEC,
    )


def _model_api_web_search_results_message(web_search: dict) -> dict:
    return build_model_api_web_search_results_message(web_search)


def _model_api_web_search_trace(web_search: dict) -> dict:
    return model_api_web_search_trace(
        web_search,
        max_queries=MODEL_API_WEB_SEARCH_MAX_QUERIES,
    )
