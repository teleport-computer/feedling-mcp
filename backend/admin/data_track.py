"""Admin data-track: per-user stats, DAU, HTML pages, store evict."""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

import hmac
from urllib.parse import quote

from flask import Blueprint, Response, abort, jsonify, request

import db
from core.store import UserStore
from urllib.parse import urlencode
import html

from accounts import onboarding
from accounts import onboarding as accounts_onboarding
from accounts import registry
from chat import consumer as chat_consumer
from memory import service as memory_service
from proactive import service as proactive_service
from bootstrap import gates as boot_gates
from core import store as core_store
from core import util as core_util
from identity import service as identity_service

bp = Blueprint("admin", __name__)


# Injected by the assembly layer — these live with the hosted/onboarding
# validation code that has not been extracted from app.py yet.
def _latest_history_import_job(store):
    return None


def _onboarding_validation_payload(store):
    return {}


def _extract_admin_token() -> str:
    key = request.headers.get("X-Admin-Token", "").strip()
    if key:
        return key
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.args.get("admin_key", "").strip()


def require_admin() -> None:
    configured = os.environ.get("FEEDLING_ADMIN_TOKEN", "").strip()
    if not configured:
        abort(503)
    supplied = _extract_admin_token()
    if not supplied or not hmac.compare_digest(supplied, configured):
        abort(401)


def _admin_qs() -> str:
    token = request.args.get("admin_key", "").strip()
    return urlencode({"admin_key": token}) if token else ""


def _data_track_qs(**updates) -> str:
    params: dict[str, str] = {}
    for key in ("admin_key", "since", "registered_since", "q", "limit", "offset", "sort", "dir", "view", "days"):
        value = request.args.get(key, "").strip()
        if value:
            params[key] = value
    for key, value in updates.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = str(value)
    return urlencode(params)


def _latest_epoch(*values) -> float:
    epochs = [core_util._to_epoch(v) for v in values]
    return max(epochs) if epochs else 0.0


def _count_rows(rows: list[dict], key: str) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        val = str(row.get(key) or "unknown").strip() or "unknown"
        counts[val] = counts.get(val, 0) + 1
    return counts


def _safe_onboarding_validation(raw: dict) -> dict:
    def scrub_step(step: dict) -> dict:
        blocked = {"relationship_anchor_evidence"}
        safe: dict = {}
        for key, value in (step or {}).items():
            if key in blocked:
                safe["has_relationship_anchor_evidence"] = bool(str(value or "").strip())
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value
            elif isinstance(value, (list, dict)):
                safe[key] = value
        return safe

    return {
        "passing": bool((raw or {}).get("passing")),
        "stage": str((raw or {}).get("stage") or ""),
        "route": str((raw or {}).get("route") or ""),
        "next_action": str((raw or {}).get("next_action") or ""),
        "steps": [scrub_step(s) for s in (raw or {}).get("steps", []) if isinstance(s, dict)],
        "skill_url": str((raw or {}).get("skill_url") or ""),
    }


def _chat_stats(store: UserStore) -> dict:
    with store.chat_lock:
        messages = list(store.chat_messages)
    by_role = _count_rows(messages, "role")
    by_source = _count_rows(messages, "source")
    by_content_type = _count_rows(messages, "content_type")
    epochs = [core_util._to_epoch(m.get("ts") or m.get("timestamp")) for m in messages if isinstance(m, dict)]
    user_epochs = [
        core_util._to_epoch(m.get("ts") or m.get("timestamp"))
        for m in messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    agent_epochs = [
        core_util._to_epoch(m.get("ts") or m.get("timestamp"))
        for m in messages
        if isinstance(m, dict) and m.get("role") in ("agent", "openclaw")
    ]
    return {
        "total": len(messages),
        "by_role": by_role,
        "by_source": by_source,
        "by_content_type": by_content_type,
        "user_messages": by_role.get("user", 0),
        "agent_messages": by_role.get("agent", 0) + by_role.get("openclaw", 0),
        "image_messages": by_content_type.get("image", 0),
        "proactive_messages": by_source.get(proactive_service.PROACTIVE_JOB_SOURCE, 0),
        "first_at": core_util._epoch_to_iso(min(epochs)) if epochs else "",
        "last_at": core_util._epoch_to_iso(max(epochs)) if epochs else "",
        "last_user_at": core_util._epoch_to_iso(max(user_epochs)) if user_epochs else "",
        "last_agent_at": core_util._epoch_to_iso(max(agent_epochs)) if agent_epochs else "",
    }


def _memory_stats(store: UserStore) -> dict:
    moments = memory_service._load_moments(store)
    changes = db.log_read_all(store.user_id, "memory_changes")
    capture_jobs = db.log_read_all(store.user_id, "memory_capture_jobs")
    by_type = {typ: 0 for typ in memory_service.MEMORY_TYPES}
    by_tab = {"story": 0, "about_me": 0, "ta_thinking": 0}
    by_source: dict[str, int] = {}
    created_epochs = []
    occurred_epochs = []
    for m in moments if isinstance(moments, list) else []:
        if not isinstance(m, dict):
            continue
        mem_type = str(m.get("type") or "unknown")
        by_type[mem_type] = by_type.get(mem_type, 0) + 1
        tab = memory_service.TAB_FOR_TYPE.get(mem_type, "unknown")
        by_tab[tab] = by_tab.get(tab, 0) + 1
        source = str(m.get("source") or "unknown")
        by_source[source] = by_source.get(source, 0) + 1
        created_epochs.append(core_util._to_epoch(m.get("created_at")))
        occurred_epochs.append(core_util._to_epoch(m.get("occurred_at")))
    counts = memory_service._count_by_tab(moments)
    capture_epochs = [
        core_util._to_epoch(j.get("ts") or j.get("created_at"))
        for j in capture_jobs
        if isinstance(j, dict)
    ]
    actions_written = 0
    for job in capture_jobs:
        if not isinstance(job, dict):
            continue
        try:
            actions_written += int(job.get("actions_written") or 0)
        except (TypeError, ValueError):
            continue
    return {
        "total": counts["total"],
        "by_tab": by_tab,
        "by_type": by_type,
        "by_source": by_source,
        "changes": len(changes),
        "changes_by_action": _count_rows(changes, "action"),
        "changes_by_capture_mode": _count_rows(changes, "capture_mode"),
        "capture_jobs": len(capture_jobs),
        "capture_jobs_by_status": _count_rows(capture_jobs, "status"),
        "capture_jobs_by_mode": _count_rows(capture_jobs, "mode"),
        "capture_actions_written": actions_written,
        "last_capture_at": core_util._epoch_to_iso(max(capture_epochs, default=0)),
        "first_created_at": core_util._epoch_to_iso(min([e for e in created_epochs if e], default=0)),
        "last_created_at": core_util._epoch_to_iso(max(created_epochs, default=0)),
        "earliest_occurred_at": core_util._epoch_to_iso(min([e for e in occurred_epochs if e], default=0)),
        "latest_occurred_at": core_util._epoch_to_iso(max(occurred_epochs, default=0)),
    }


def _proactive_stats(store: UserStore) -> dict:
    decisions = store.list_gate_decisions(limit=0)
    jobs = store.list_proactive_jobs(limit=0)
    device_events = store.list_device_events(limit=0)
    with store.chat_lock:
        proactive_messages = [
            m for m in store.chat_messages
            if isinstance(m, dict) and (
                m.get("source") == proactive_service.PROACTIVE_JOB_SOURCE or str(m.get("proactive_job_id") or "")
            )
        ]
    decision_true = sum(1 for d in decisions if bool(d.get("should_reach_out")))
    status_counts = _count_rows(jobs, "status")
    live_status_counts = _count_rows(proactive_messages, "live_activity_status")
    alert_status_counts = _count_rows(proactive_messages, "alert_status")
    job_epochs = [core_util._to_epoch(j.get("ts") or j.get("created_at") or j.get("updated_at")) for j in jobs]
    msg_epochs = [core_util._to_epoch(m.get("ts")) for m in proactive_messages]
    decision_epochs = [core_util._to_epoch(d.get("ts") or d.get("created_at")) for d in decisions]
    delivered = (
        live_status_counts.get("delivered", 0)
        + alert_status_counts.get("delivered", 0)
        + alert_status_counts.get("logged_only", 0)
    )
    failed = sum(status_counts.get(s, 0) for s in ("failed", "skipped"))
    failed += sum(live_status_counts.get(s, 0) for s in ("failed", "error"))
    failed += sum(alert_status_counts.get(s, 0) for s in ("failed", "error"))
    return {
        "decisions": len(decisions),
        "decision_true": decision_true,
        "decision_false": max(0, len(decisions) - decision_true),
        "jobs": len(jobs),
        "jobs_by_status": status_counts,
        "pending_jobs": status_counts.get("pending", 0),
        "posted_jobs": status_counts.get("posted", 0) + status_counts.get("delivered", 0),
        "failed_jobs": failed,
        "proactive_messages": len(proactive_messages),
        "delivery_signals": delivered,
        "live_activity_status": live_status_counts,
        "alert_status": alert_status_counts,
        "device_events": len(device_events),
        "last_at": core_util._epoch_to_iso(max(job_epochs + msg_epochs + decision_epochs, default=0)),
    }


def _push_stats(store: UserStore) -> dict:
    tokens = [t for t in (store.tokens or []) if isinstance(t, dict)]
    statuses = _count_rows(tokens, "status")
    updated_epochs = [core_util._to_epoch(t.get("updated_at") or t.get("registered_at")) for t in tokens]
    return {
        "tokens": len(tokens),
        "active_tokens": statuses.get("active", 0),
        "by_status": statuses,
        "last_token_at": core_util._epoch_to_iso(max(updated_epochs, default=0)),
    }


def _tracking_stats(store: UserStore, *, include_events: bool = False) -> dict:
    events = store.list_tracking_events(limit=0)
    by_type = _count_rows(events, "type")
    epochs = [core_util._to_epoch(e.get("ts") or e.get("created_at")) for e in events]
    latest = sorted(events, key=lambda e: core_util._to_epoch(e.get("ts") or e.get("created_at")), reverse=True)[:50]
    out = {
        "events": len(events),
        "by_type": by_type,
        "last_at": core_util._epoch_to_iso(max(epochs, default=0)),
    }
    if include_events:
        out["latest"] = [
            {
                "event_id": e.get("event_id", ""),
                "type": e.get("type", ""),
                "created_at": e.get("created_at", ""),
                "source": e.get("source", ""),
                "route": e.get("route", ""),
                "app_version": e.get("app_version", ""),
                "build": e.get("build", ""),
                "payload": e.get("payload", {}),
            }
            for e in latest
        ]
    return out


def _history_import_stats(store: UserStore) -> dict:
    latest = _latest_history_import_job(store)
    if not latest:
        return {"has_job": False}
    return {
        "has_job": True,
        "job_id": latest.get("job_id", ""),
        "status": latest.get("status", ""),
        "phase": latest.get("phase", ""),
        "phase_label": latest.get("phase_label", ""),
        "progress": latest.get("progress", 0),
        "created_at": latest.get("created_at", ""),
        "started_at": latest.get("started_at", ""),
        "updated_at": latest.get("updated_at", ""),
        "completed_at": latest.get("completed_at", ""),
        "failed_at": latest.get("failed_at", ""),
        "error": latest.get("error", ""),
        "messages_parsed": latest.get("messages_parsed", 0),
        "support_materials": latest.get("support_materials", 0),
        "source_stats": latest.get("source_stats", {}),
        "ai_persona_chars": latest.get("ai_persona_chars", 0),
        "user_profile_chars": latest.get("user_profile_chars", latest.get("persona_chars", 0)),
        "memory_summary_chars": latest.get("memory_summary_chars", 0),
        "chat_messages_imported": latest.get("chat_messages_imported", 0),
        "memories_created": latest.get("memories_created", 0),
        "identity_written": bool(latest.get("identity_written")),
    }


def _data_track_iso(value) -> str:
    if isinstance(value, (int, float)):
        return core_util._epoch_to_iso(value)
    return str(value or "")


def _data_track_count_dict(raw: dict | None) -> dict:
    out: dict[str, int] = {}
    for key, value in (raw or {}).items():
        try:
            out[str(key or "unknown")] = int(value or 0)
        except Exception:
            out[str(key or "unknown")] = 0
    return out


def _data_track_days_since(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.replace(tzinfo=None)
        return max(0, (datetime.now().date() - dt.date()).days)
    except Exception:
        return None


def _data_track_memory_from_snapshot(snap: dict) -> dict:
    memory = dict(snap.get("memory") or {})
    extra = dict(snap.get("memory_extra") or {})
    log_counts = dict(snap.get("log_counts") or {})
    by_type = {typ: 0 for typ in memory_service.MEMORY_TYPES}
    by_type.update(_data_track_count_dict(memory.get("by_type")))
    by_tab = {"story": 0, "about_me": 0, "ta_thinking": 0}
    for mem_type, count in by_type.items():
        tab = memory_service.TAB_FOR_TYPE.get(mem_type, "unknown")
        by_tab[tab] = by_tab.get(tab, 0) + int(count or 0)
    return {
        "total": int(memory.get("total") or 0),
        "by_tab": by_tab,
        "by_type": by_type,
        "by_source": _data_track_count_dict(memory.get("by_source")),
        "changes": int((snap.get("logs") or {}).get("memory_changes", {}).get("count") or 0),
        "changes_by_action": _data_track_count_dict(log_counts.get("changes_by_action")),
        "changes_by_capture_mode": _data_track_count_dict(log_counts.get("changes_by_capture_mode")),
        "capture_jobs": int(extra.get("capture_jobs") or 0),
        "capture_jobs_by_status": _data_track_count_dict(log_counts.get("capture_jobs_by_status")),
        "capture_jobs_by_mode": _data_track_count_dict(log_counts.get("capture_jobs_by_mode")),
        "capture_actions_written": int(extra.get("capture_actions_written") or 0),
        "last_capture_at": _data_track_iso(extra.get("last_capture_ts")),
        "first_created_at": _data_track_iso(memory.get("first_created_at")),
        "last_created_at": _data_track_iso(memory.get("last_created_at")),
        "earliest_occurred_at": _data_track_iso(memory.get("earliest_occurred_at")),
        "latest_occurred_at": _data_track_iso(memory.get("latest_occurred_at")),
    }


def _data_track_chat_from_snapshot(snap: dict) -> dict:
    chat = dict(snap.get("chat") or {})
    by_role = _data_track_count_dict(chat.get("by_role"))
    by_source = _data_track_count_dict(chat.get("by_source"))
    by_content_type = _data_track_count_dict(chat.get("by_content_type"))
    user_messages = int(chat.get("user_messages") or by_role.get("user", 0))
    agent_messages = int(
        chat.get("agent_messages")
        or by_role.get("agent", 0)
        + by_role.get("openclaw", 0)
    )
    return {
        "total": int(chat.get("total") or 0),
        "by_role": by_role,
        "by_source": by_source,
        "by_content_type": by_content_type,
        "user_messages": user_messages,
        "agent_messages": agent_messages,
        "image_messages": int(chat.get("image_messages") or by_content_type.get("image", 0)),
        "proactive_messages": int(chat.get("proactive_messages") or by_source.get(proactive_service.PROACTIVE_JOB_SOURCE, 0)),
        "model_api_user_messages": int(chat.get("model_api_user_messages") or 0),
        "model_api_agent_messages": int(chat.get("model_api_agent_messages") or 0),
        "model_api_greetings": int(chat.get("model_api_greetings") or 0),
        "first_at": _data_track_iso(chat.get("first_ts")),
        "last_at": _data_track_iso(chat.get("last_ts")),
        "last_user_at": _data_track_iso(chat.get("last_user_ts")),
        "last_agent_at": _data_track_iso(chat.get("last_agent_ts")),
        "proactive_last_at": _data_track_iso(chat.get("proactive_last_ts")),
    }


def _data_track_proactive_from_snapshot(snap: dict, chat: dict) -> dict:
    logs = dict(snap.get("logs") or {})
    extra = dict(snap.get("proactive_extra") or {})
    status_counts = _data_track_count_dict(extra.get("jobs_by_status"))
    live_status_counts = _data_track_count_dict(extra.get("live_activity_status"))
    alert_status_counts = _data_track_count_dict(extra.get("alert_status"))
    decisions = int(extra.get("decisions") or logs.get("gate_decisions", {}).get("count") or 0)
    decision_true = int(extra.get("decision_true") or 0)
    delivered = (
        live_status_counts.get("delivered", 0)
        + alert_status_counts.get("delivered", 0)
        + alert_status_counts.get("logged_only", 0)
    )
    failed = sum(status_counts.get(s, 0) for s in ("failed", "skipped"))
    failed += sum(live_status_counts.get(s, 0) for s in ("failed", "error"))
    failed += sum(alert_status_counts.get(s, 0) for s in ("failed", "error"))
    last_at = _latest_epoch(
        logs.get("proactive_jobs", {}).get("last_ts"),
        logs.get("gate_decisions", {}).get("last_ts"),
        chat.get("proactive_last_at"),
    )
    return {
        "decisions": decisions,
        "decision_true": decision_true,
        "decision_false": max(0, decisions - decision_true),
        "jobs": int(logs.get("proactive_jobs", {}).get("count") or 0),
        "jobs_by_status": status_counts,
        "pending_jobs": status_counts.get("pending", 0),
        "posted_jobs": status_counts.get("posted", 0) + status_counts.get("delivered", 0),
        "failed_jobs": failed,
        "proactive_messages": int(chat.get("proactive_messages") or 0),
        "delivery_signals": delivered,
        "live_activity_status": live_status_counts,
        "alert_status": alert_status_counts,
        "device_events": int(logs.get("device_events", {}).get("count") or 0),
        "last_at": core_util._epoch_to_iso(last_at),
    }


def _data_track_tracking_from_snapshot(snap: dict) -> dict:
    logs = dict(snap.get("logs") or {})
    counts = dict(snap.get("log_counts") or {})
    tracking = logs.get("tracking_events", {}) or {}
    return {
        "events": int(tracking.get("count") or 0),
        "by_type": _data_track_count_dict(counts.get("tracking_by_type")),
        "last_at": _data_track_iso(tracking.get("last_ts")),
    }


def _data_track_bootstrap_from_snapshot(snap: dict) -> dict:
    logs = dict(snap.get("logs") or {})
    counts = dict(snap.get("log_counts") or {})
    bootstrap = logs.get("bootstrap_events", {}) or {}
    return {
        "events": int(bootstrap.get("count") or 0),
        "by_type": _data_track_count_dict(counts.get("bootstrap_by_type")),
        "last_at": _data_track_iso(bootstrap.get("last_ts")),
    }


def _data_track_history_import_from_snapshot(snap: dict) -> dict:
    latest = snap.get("history_import")
    if not isinstance(latest, dict):
        return {"has_job": False}
    return {
        "has_job": True,
        "job_id": latest.get("job_id", ""),
        "status": latest.get("status", ""),
        "phase": latest.get("phase", ""),
        "phase_label": latest.get("phase_label", ""),
        "progress": latest.get("progress", 0),
        "created_at": latest.get("created_at", ""),
        "started_at": latest.get("started_at", ""),
        "updated_at": latest.get("updated_at", ""),
        "completed_at": latest.get("completed_at", ""),
        "failed_at": latest.get("failed_at", ""),
        "error": latest.get("error", ""),
        "messages_parsed": latest.get("messages_parsed", 0),
        "support_materials": latest.get("support_materials", 0),
        "source_stats": latest.get("source_stats", {}),
        "ai_persona_chars": latest.get("ai_persona_chars", 0),
        "user_profile_chars": latest.get("user_profile_chars", latest.get("persona_chars", 0)),
        "memory_summary_chars": latest.get("memory_summary_chars", 0),
        "chat_messages_imported": latest.get("chat_messages_imported", 0),
        "memories_created": latest.get("memories_created", 0),
        "identity_written": bool(latest.get("identity_written")),
        "chat_ready": bool(latest.get("chat_ready")),
    }


def _data_track_relationship_days(identity: dict | None, memory: dict) -> int:
    if identity and identity.get("relationship_started_at"):
        days = _data_track_days_since(identity.get("relationship_started_at"))
        if days is not None:
            return days
    days = _data_track_days_since(memory.get("earliest_occurred_at"))
    return days if days is not None else 0


def _data_track_fast_validation(
    *,
    route: str,
    chat: dict,
    memory: dict,
    identity: dict | None,
    history_import: dict,
    model_api_config: dict | None,
    consumer_state: dict | None,
    bootstrap_events: dict,
) -> dict:
    relationship_days = _data_track_relationship_days(identity, memory)
    floors = memory_service._per_tab_floors_for_days(relationship_days)
    counts = memory.get("by_tab") or {}
    missing_tabs = []
    if int(counts.get("story") or 0) < floors["story"]:
        missing_tabs.append("story")
    if int(counts.get("about_me") or 0) < floors["about_me"]:
        missing_tabs.append("about_me")
    identity_written = identity is not None
    relationship_evidence = str((identity or {}).get("relationship_anchor_evidence") or "").strip()
    relationship_ok = bool(identity and identity.get("relationship_started_at") and relationship_evidence)

    def step(step_id: str, label: str, passing: bool, required: str = "", **extra) -> dict:
        out = {"id": step_id, "label": label, "passing": bool(passing), "required": "" if passing else required}
        out.update(extra)
        return out

    if route == "model_api":
        history_ok = bool(history_import.get("has_job") and (
            history_import.get("status") == "completed" or history_import.get("chat_ready")
        ))
        memory_ok = history_ok and int(counts.get("story") or 0) >= 1 and int(counts.get("about_me") or 0) >= 1
        hosted_chat_ok = bool(
            chat.get("model_api_greetings")
            or (chat.get("model_api_user_messages") and chat.get("model_api_agent_messages"))
        )
        steps = [
            step("model_api_config", "Model API Config", bool(model_api_config), "Call /v1/model_api/setup."),
            step(
                "model_api_test",
                "Model API Test",
                bool(model_api_config and model_api_config.get("test_status") == "ok"),
                "Call /v1/model_api/test until test_status is ok.",
                test_status=(model_api_config or {}).get("test_status", ""),
            ),
            step(
                "history_import",
                "Onboarding Materials",
                history_ok,
                "Start onboarding with AI persona materials, user profile, memory summary, chat history, or confirmed fresh start.",
                job_status=history_import.get("status", ""),
                phase=history_import.get("phase", ""),
                progress=history_import.get("progress", 0),
                chat_ready=history_import.get("chat_ready", False),
            ),
            step(
                "memory_garden",
                "Memory Garden",
                memory_ok,
                "History import must write at least one Story card and one About-me card.",
                counts={"story": counts.get("story", 0), "about_me": counts.get("about_me", 0), "ta_thinking": counts.get("ta_thinking", 0), "total": memory.get("total", 0)},
            ),
            step("identity_card", "Identity Card", identity_written, "History import must derive and write Identity Card."),
            step("relationship_anchor", "Relationship Anchor", relationship_ok, "History import must include relationship_started_at or fresh_start=true."),
            step("hosted_chat", "Hosted Chat", hosted_chat_ok, "Send one test message through /v1/model_api/chat/send."),
        ]
        next_step = next((s for s in steps if not s["passing"]), None)
        return {
            "passing": next_step is None,
            "stage": "complete" if next_step is None else next_step["id"],
            "route": "model_api",
            "next_action": "" if next_step is None else next_step["required"],
            "steps": steps,
        }

    memory_ok = not missing_tabs
    if route == "official_import":
        steps = [
            step("memory_garden", "Memory Garden", memory_ok, "Memory import must fill required Story and About-me cards."),
            step("identity_card", "Identity Card", identity_written, "Use the official app/tool client to import memory and identity."),
            step("relationship_anchor", "Relationship Anchor", relationship_ok, "Set relationship anchor during import."),
        ]
        next_step = next((s for s in steps if not s["passing"]), None)
        return {
            "passing": next_step is None,
            "stage": "import_ready" if next_step is None else next_step["id"],
            "route": "official_import",
            "next_action": "" if next_step is None else next_step["required"],
            "steps": steps,
        }

    consumer = consumer_state or {}
    try:
        age_sec = time.time() - float(consumer.get("last_poll_epoch") or 0)
    except Exception:
        age_sec = None
    consumer_ok = bool(consumer.get("official")) and age_sec is not None and age_sec <= chat_consumer._CONSUMER_RECENT_SEC
    bootstrap_types = bootstrap_events.get("by_type") or {}
    chat_loop_ok = bool(bootstrap_types.get("chat_loop_verified")) or bool(chat.get("user_messages") and chat.get("agent_messages"))
    first_greeting_ok = int(chat.get("agent_messages") or 0) > 0
    real_exchange_ok = bool(chat.get("user_messages") and chat.get("agent_messages"))
    steps = [
        step("memory_garden", "Memory Garden", memory_ok, "Memory Garden is below required Story/About-me floors."),
        step("identity_card", "Identity Card", identity_written, "Call feedling_identity_init after memory verification passes."),
        step("relationship_anchor", "Relationship Anchor", relationship_ok, "Re-run identity init with relationship_anchor_evidence."),
        step("resident_consumer", "Resident Consumer", consumer_ok, "Run the standard feedling-chat-resident / IO resident consumer."),
        step("live_loop", "Live Connection", chat_loop_ok, "Call feedling_chat_verify_loop after resident consumer is polling."),
        step("first_greeting", "First Greeting", first_greeting_ok, "Send first visible greeting via feedling_chat_post_message."),
        step("real_chat_acceptance", "Real Chat Acceptance", real_exchange_ok, "Ask the user to send one ordinary IO Chat message and confirm a reply."),
    ]
    next_step = next((s for s in steps if not s["passing"]), None)
    return {
        "passing": next_step is None,
        "stage": "complete" if next_step is None else next_step["id"],
        "route": "resident",
        "next_action": "" if next_step is None else next_step["required"],
        "steps": steps,
    }


def _build_data_track_user_fast(user_entry: dict, snap: dict) -> dict:
    user_id = str(user_entry.get("user_id") or "")
    blobs = dict(snap.get("blobs") or {})
    route_data = blobs.get("onboarding_route") or {}
    route = accounts_onboarding._normalize_onboarding_route(str((route_data or {}).get("route") or "resident"))
    route = route if route in accounts_onboarding.MODEL_API_ROUTES else "resident"
    access_modes = registry._public_access_mode_state(dict(user_entry), route)
    access_connected = [
        mode["access_mode"]
        for mode in access_modes
        if mode.get("connected")
    ]
    api_keys_count = sum(
        1
        for key_entry in user_entry.get("api_keys") or []
        if isinstance(key_entry, dict) and not key_entry.get("revoked_at")
    )
    chat = _data_track_chat_from_snapshot(snap)
    memory = _data_track_memory_from_snapshot(snap)
    proactive = _data_track_proactive_from_snapshot(snap, chat)
    tracking = _data_track_tracking_from_snapshot(snap)
    bootstrap_events = _data_track_bootstrap_from_snapshot(snap)
    history_import = _data_track_history_import_from_snapshot(snap)
    identity = blobs.get("identity") if isinstance(blobs.get("identity"), dict) else None
    validation = _data_track_fast_validation(
        route=route,
        chat=chat,
        memory=memory,
        identity=identity,
        history_import=history_import,
        model_api_config=blobs.get("model_api") if isinstance(blobs.get("model_api"), dict) else None,
        consumer_state=blobs.get("consumer_state") if isinstance(blobs.get("consumer_state"), dict) else None,
        bootstrap_events=bootstrap_events,
    )
    steps = validation.get("steps", [])
    steps_total = len(steps)
    steps_done = sum(1 for s in steps if bool(s.get("passing")))
    registered_at = str(user_entry.get("created_at") or "")
    identity_updated_at = (identity or {}).get("updated_at", "")
    latest_epoch = _latest_epoch(
        registered_at,
        route_data.get("selected_at"),
        chat.get("last_at"),
        memory.get("last_created_at"),
        proactive.get("last_at"),
        tracking.get("last_at"),
        bootstrap_events.get("last_at"),
        identity_updated_at,
        history_import.get("updated_at"),
        history_import.get("completed_at"),
    )
    passing = bool(validation.get("passing"))
    stuck_for_sec = 0 if passing else int(max(0, time.time() - latest_epoch)) if latest_epoch else None
    return {
        "user_id": user_id,
        "principal_id": user_entry.get("principal_id") or "",
        "registered_at": registered_at,
        "archive_language": user_entry.get("archive_language") or "",
        "public_key_present": bool(str(user_entry.get("public_key") or "").strip()),
        "route": route,
        "route_selected_at": route_data.get("selected_at", ""),
        "access": {
            "principal_id": user_entry.get("principal_id") or "",
            "active_route": route,
            "connected_modes": access_connected,
            "modes": access_modes,
            "api_keys_count": api_keys_count,
        },
        "onboarding": {
            "passing": passing,
            "stage": "complete" if passing else validation.get("stage") or "unknown",
            "steps_done": steps_done,
            "steps_total": steps_total,
            "next_action": validation.get("next_action", ""),
            "steps": [],
            "stuck_for_sec": stuck_for_sec,
        },
        "last_activity_at": core_util._epoch_to_iso(latest_epoch),
        "chat": chat,
        "memory": memory,
        "proactive": proactive,
        "push": _push_stats_from_user_entry(user_entry),
        "tracking": tracking,
        "bootstrap_events": bootstrap_events,
        "history_import": history_import,
    }


def _push_stats_from_user_entry(user_entry: dict) -> dict:
    tokens = [t for t in (user_entry.get("tokens") or []) if isinstance(t, dict)]
    statuses = _count_rows(tokens, "status")
    updated_epochs = [core_util._to_epoch(t.get("updated_at") or t.get("registered_at")) for t in tokens]
    return {
        "tokens": len(tokens),
        "active_tokens": statuses.get("active", 0),
        "by_status": statuses,
        "last_token_at": core_util._epoch_to_iso(max(updated_epochs, default=0)),
    }


def _bootstrap_event_stats(store: UserStore, *, include_events: bool = False) -> dict:
    events = boot_gates._load_bootstrap_events(store)
    by_type = _count_rows(events, "event_type")
    epochs = [core_util._to_epoch(e.get("timestamp") or e.get("ts")) for e in events]
    out = {
        "events": len(events),
        "by_type": by_type,
        "last_at": core_util._epoch_to_iso(max(epochs, default=0)),
    }
    if include_events:
        out["latest"] = [
            {
                "event_type": e.get("event_type", ""),
                "success": bool(e.get("success")),
                "timestamp": e.get("timestamp", ""),
                "has_error": bool(str(e.get("error_message") or "").strip()),
            }
            for e in events[-50:]
        ]
    return out


def _build_data_track_user(user_entry: dict, *, include_detail: bool = False) -> dict:
    user_id = str(user_entry.get("user_id") or "")
    store = core_store.get_store(user_id)
    route_data = db.get_blob(store.user_id, "onboarding_route") or {}
    route = onboarding._load_onboarding_route(store)
    access_modes = registry._public_access_mode_state(dict(user_entry), route)
    access_connected = [
        mode["access_mode"]
        for mode in access_modes
        if mode.get("connected")
    ]
    api_keys_count = sum(
        1
        for key_entry in user_entry.get("api_keys") or []
        if isinstance(key_entry, dict) and not key_entry.get("revoked_at")
    )
    validation = _safe_onboarding_validation(_onboarding_validation_payload(store))
    steps = validation.get("steps", [])
    steps_total = len(steps)
    steps_done = sum(1 for s in steps if bool(s.get("passing")))
    chat = _chat_stats(store)
    memory = _memory_stats(store)
    proactive = _proactive_stats(store)
    push = _push_stats(store)
    tracking = _tracking_stats(store, include_events=include_detail)
    bootstrap_events = _bootstrap_event_stats(store, include_events=include_detail)
    history_import = _history_import_stats(store)
    identity = identity_service._load_identity(store)
    identity_updated_at = (identity or {}).get("updated_at", "")
    registered_at = str(user_entry.get("created_at") or "")
    latest_epoch = _latest_epoch(
        registered_at,
        route_data.get("selected_at"),
        chat.get("last_at"),
        memory.get("last_created_at"),
        proactive.get("last_at"),
        tracking.get("last_at"),
        bootstrap_events.get("last_at"),
        identity_updated_at,
        history_import.get("updated_at"),
        history_import.get("completed_at"),
    )
    now = time.time()
    stage = validation.get("stage") or "unknown"
    passing = bool(validation.get("passing"))
    stuck_for_sec = 0 if passing else int(max(0, now - latest_epoch)) if latest_epoch else None
    row = {
        "user_id": user_id,
        "principal_id": user_entry.get("principal_id") or "",
        "registered_at": registered_at,
        "archive_language": user_entry.get("archive_language") or "",
        "public_key_present": bool(str(user_entry.get("public_key") or "").strip()),
        "route": route,
        "route_selected_at": route_data.get("selected_at", ""),
        "access": {
            "principal_id": user_entry.get("principal_id") or "",
            "active_route": route,
            "connected_modes": access_connected,
            "modes": access_modes,
            "api_keys_count": api_keys_count,
        },
        "onboarding": {
            "passing": passing,
            "stage": "complete" if passing else stage,
            "steps_done": steps_done,
            "steps_total": steps_total,
            "next_action": validation.get("next_action", ""),
            "steps": steps if include_detail else [],
            "stuck_for_sec": stuck_for_sec,
        },
        "last_activity_at": core_util._epoch_to_iso(latest_epoch),
        "chat": chat,
        "memory": memory,
        "proactive": proactive,
        "push": push,
        "tracking": tracking,
        "bootstrap_events": bootstrap_events,
        "history_import": history_import,
    }
    if include_detail:
        row["identity"] = {
            "written": identity is not None,
            "updated_at": identity_updated_at,
            "relationship_started_at": (identity or {}).get("relationship_started_at", ""),
            "relationship_anchor_source": (identity or {}).get("relationship_anchor_source", ""),
            "has_relationship_anchor_evidence": bool(
                str((identity or {}).get("relationship_anchor_evidence") or "").strip()
            ),
        }
    return row


def _data_track_request_filters() -> dict:
    raw_since = (
        request.args.get("since")
        or request.args.get("registered_since")
        or ""
    ).strip()
    raw_q = (request.args.get("q") or "").strip().lower()
    raw_sort = (request.args.get("sort") or "").strip().lower()
    if raw_sort not in {"chat", "memory", "proactive"}:
        raw_sort = ""
    raw_dir = (request.args.get("dir") or "desc").strip().lower()
    if raw_dir not in {"asc", "desc"}:
        raw_dir = "desc"
    raw_view = (request.args.get("view") or "users").strip().lower()
    if raw_view not in {"users", "dau"}:
        raw_view = "users"

    def read_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(request.args.get(name, default))
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    return {
        "since": raw_since,
        "since_epoch": core_util._to_epoch(raw_since),
        "q": raw_q,
        "sort": raw_sort,
        "dir": raw_dir,
        "limit": read_int("limit", 100, 1, 500),
        "offset": read_int("offset", 0, 0, 1_000_000),
        "view": raw_view,
        "days": read_int("days", 30, 1, 366),
    }


def _data_track_filter_users(users: list[dict], filters: dict) -> list[dict]:
    since_epoch = float(filters.get("since_epoch") or 0)
    if not since_epoch:
        return users
    return [
        u for u in users
        if core_util._to_epoch(u.get("created_at")) >= since_epoch
    ]


def _data_track_apply_text_filter(rows: list[dict], q: str) -> list[dict]:
    needle = (q or "").strip().lower()
    if not needle:
        return rows
    out = []
    for row in rows:
        hay = " ".join([
            str(row.get("user_id") or ""),
            str(row.get("principal_id") or ""),
            str(row.get("route") or ""),
            str(row.get("archive_language") or ""),
            str(row.get("onboarding", {}).get("stage") or ""),
            " ".join(row.get("access", {}).get("connected_modes") or []),
        ]).lower()
        if needle in hay:
            out.append(row)
    return out


def _data_track_sort_rows(rows: list[dict], sort_key: str, direction: str) -> None:
    def intval(value) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    def metrics(row: dict) -> tuple[int, ...]:
        if sort_key == "chat":
            chat = row.get("chat") or {}
            return (
                intval(chat.get("total")),
                intval(chat.get("user_messages")),
                intval(chat.get("agent_messages")),
            )
        if sort_key == "memory":
            memory = row.get("memory") or {}
            by_tab = memory.get("by_tab") or {}
            return (
                intval(memory.get("total")),
                intval(by_tab.get("story")),
                intval(by_tab.get("about_me")),
                intval(by_tab.get("ta_thinking")),
            )
        if sort_key == "proactive":
            proactive = row.get("proactive") or {}
            return (
                intval(proactive.get("proactive_messages")),
                intval(proactive.get("jobs")),
                intval(proactive.get("decisions")),
                intval(proactive.get("delivery_signals")),
            )
        return (0,)

    if not sort_key:
        rows.sort(key=lambda r: (core_util._to_epoch(r.get("registered_at")), str(r.get("user_id") or "")), reverse=True)
        return

    desc = direction != "asc"

    def sort_tuple(row: dict) -> tuple:
        values = metrics(row)
        if desc:
            values = tuple(-v for v in values)
        return (*values, -core_util._to_epoch(row.get("registered_at")), str(row.get("user_id") or ""))

    rows.sort(key=sort_tuple)


def _data_track_payload(*, include_users: bool = True, include_detail_user: str = "") -> dict:
    filters = _data_track_request_filters()
    with registry._users_lock:
        if registry._normalize_all_users():
            registry._save_users()
        users = [dict(u) for u in registry._users if u.get("user_id")]
    users = _data_track_filter_users(users, filters)
    snapshot = db.admin_data_track_snapshot([str(u.get("user_id") or "") for u in users])
    rows = []
    for u in users:
        uid = str(u.get("user_id") or "")
        if include_detail_user and include_detail_user == uid:
            rows.append(_build_data_track_user(u, include_detail=True))
        else:
            rows.append(_build_data_track_user_fast(u, snapshot.get(uid, {})))
    rows = _data_track_apply_text_filter(rows, str(filters.get("q") or ""))
    _data_track_sort_rows(rows, str(filters.get("sort") or ""), str(filters.get("dir") or "desc"))
    completed = sum(1 for r in rows if r["onboarding"]["passing"])
    incomplete = max(0, len(rows) - completed)
    stage_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    access_mode_counts: dict[str, int] = {}
    chat_total = 0
    memory_total = 0
    proactive_jobs = 0
    proactive_messages = 0
    proactive_failed = 0
    for row in rows:
        stage = row["onboarding"]["stage"]
        route = row["route"]
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        route_counts[route] = route_counts.get(route, 0) + 1
        for mode in row.get("access", {}).get("connected_modes", []):
            access_mode_counts[mode] = access_mode_counts.get(mode, 0) + 1
        chat_total += row["chat"]["total"]
        memory_total += row["memory"]["total"]
        proactive_jobs += row["proactive"]["jobs"]
        proactive_messages += row["proactive"]["proactive_messages"]
        proactive_failed += row["proactive"]["failed_jobs"]
    summary = {
        "generated_at": datetime.now().isoformat(),
        "users_total": len(rows),
        "onboarding_completed": completed,
        "onboarding_incomplete": incomplete,
        "completion_rate": (completed / len(rows)) if rows else 0,
        "stage_counts": stage_counts,
        "route_counts": route_counts,
        "access_mode_counts": access_mode_counts,
        "principals_total": len(set(r.get("principal_id") or r.get("user_id") for r in rows)),
        "chat_messages_total": chat_total,
        "memory_total": memory_total,
        "memory_avg_per_user": (memory_total / len(rows)) if rows else 0,
        "proactive_jobs_total": proactive_jobs,
        "proactive_messages_total": proactive_messages,
        "proactive_failed_total": proactive_failed,
    }
    payload = {
        "summary": summary,
        "filters": {
            "since": filters.get("since", ""),
            "q": filters.get("q", ""),
            "sort": filters.get("sort", ""),
            "dir": filters.get("dir", "desc"),
        },
    }
    if include_users:
        offset = int(filters.get("offset") or 0)
        limit = int(filters.get("limit") or 100)
        payload["users"] = rows[offset:offset + limit]
        payload["pagination"] = {
            "limit": limit,
            "offset": offset,
            "returned": len(payload["users"]),
            "total": len(rows),
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "prev_offset": max(0, offset - limit) if offset > 0 else None,
        }
    return payload


def _data_track_dau_payload() -> dict:
    filters = _data_track_request_filters()
    days = int(filters.get("days") or 30)
    rows = db.admin_data_track_dau(
        since_epoch=float(filters.get("since_epoch") or 0),
        days=days,
        tz="Asia/Shanghai",
    )
    dau_values = [int(row.get("dau") or 0) for row in rows]
    latest = rows[0] if rows else {}
    summary = {
        "generated_at": datetime.now().isoformat(),
        "timezone": "Asia/Shanghai",
        "days_returned": len(rows),
        "latest_day": latest.get("day", ""),
        "latest_dau": int(latest.get("dau") or 0),
        "max_dau": max(dau_values, default=0),
        "avg_dau": (sum(dau_values) / len(dau_values)) if dau_values else 0,
        "user_messages": sum(int(row.get("user_messages") or 0) for row in rows),
        "tracking_events": sum(int(row.get("tracking_events") or 0) for row in rows),
        "active_events": sum(int(row.get("active_events") or 0) for row in rows),
    }
    return {
        "summary": summary,
        "filters": {
            "since": filters.get("since", ""),
            "days": days,
            "view": "dau",
        },
        "rows": [
            {
                **row,
                "first_at": core_util._epoch_to_iso(row.get("first_ts")),
                "last_at": core_util._epoch_to_iso(row.get("last_ts")),
            }
            for row in rows
        ],
        "definition": {
            "dau": "Distinct users with at least one user chat message or tracking event on the Beijing day.",
            "excluded": "Agent/openclaw messages, proactive writes, and verify_ping synthetic messages are excluded.",
            "timezone": "Asia/Shanghai",
        },
    }


def _format_duration(seconds) -> str:
    if seconds is None:
        return "n/a"
    try:
        sec = int(seconds)
    except Exception:
        return "n/a"
    if sec < 60:
        return f"{sec}s"
    minutes = sec // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _render_metric(label: str, value) -> str:
    return (
        "<div class='metric'>"
        f"<div class='metric-value'>{html.escape(str(value))}</div>"
        f"<div class='metric-label'>{html.escape(label)}</div>"
        "</div>"
    )


def _data_track_page_href(**updates) -> str:
    qs_inner = _data_track_qs(**updates)
    return f"/admin/data-track?{qs_inner}" if qs_inner else "/admin/data-track"


def _render_data_track_view_nav(active: str) -> str:
    def nav_item(view: str, label: str) -> str:
        cls = "sort-button active" if active == view else "sort-button"
        href = _data_track_page_href(view=None if view == "users" else view, offset=0)
        return f"<a class='{cls}' href='{html.escape(href, quote=True)}'>{html.escape(label)}</a>"

    return (
        "<div class='viewbar'>"
        f"{nav_item('users', 'Users')}"
        f"{nav_item('dau', 'DAU')}"
        "</div>"
    )


def _render_data_track_page(payload: dict) -> str:
    summary = payload["summary"]
    users = payload.get("users", [])
    pagination = payload.get("pagination", {})
    filters = payload.get("filters", {})
    qs = _data_track_qs()
    qs_suffix = f"?{qs}" if qs else ""
    current_sort = str(filters.get("sort") or "")
    current_dir = str(filters.get("dir") or "desc")

    def sort_button(metric: str, direction: str, label: str) -> str:
        active = current_sort == metric and current_dir == direction
        cls = "sort-button active" if active else "sort-button"
        href = _data_track_page_href(sort=metric, dir=direction, offset=0, view=None)
        return f"<a class='{cls}' href='{html.escape(href, quote=True)}'>{html.escape(label)}</a>"

    sort_controls = "".join([
        sort_button("chat", "desc", "Chat desc"),
        sort_button("chat", "asc", "Chat asc"),
        sort_button("memory", "desc", "Memory desc"),
        sort_button("memory", "asc", "Memory asc"),
        sort_button("proactive", "desc", "Proactive desc"),
        sort_button("proactive", "asc", "Proactive asc"),
    ])
    pager = ""
    if pagination:
        pager_links = []
        prev_offset = pagination.get("prev_offset")
        next_offset = pagination.get("next_offset")
        if prev_offset is not None:
            pager_links.append(f"<a class='sort-button' href='{html.escape(_data_track_page_href(offset=prev_offset, view=None), quote=True)}'>Prev</a>")
        if next_offset is not None:
            pager_links.append(f"<a class='sort-button' href='{html.escape(_data_track_page_href(offset=next_offset, view=None), quote=True)}'>Next</a>")
        if pager_links:
            pager = f"<div class='pager'>{''.join(pager_links)}</div>"
    rows_html = []
    for row in users:
        onboarding = row["onboarding"]
        stage = onboarding["stage"]
        complete = onboarding["passing"]
        status_class = "ok" if complete else "warn"
        user_url = f"/admin/data-track/users/{quote(row['user_id'])}{qs_suffix}"
        access = row.get("access", {})
        principal = str(access.get("principal_id") or row.get("principal_id") or "")
        principal_short = f"{principal[:12]}…" if len(principal) > 12 else principal
        connected_modes = ", ".join(access.get("connected_modes") or []) or "none"
        rows_html.append(
            "<tr>"
            f"<td><a href='{html.escape(user_url)}'>{html.escape(row['user_id'])}</a>"
            f"<br><span class='muted'>{html.escape(principal_short)} · keys {access.get('api_keys_count', 0)}</span></td>"
            f"<td>{html.escape(row['route'])}</td>"
            f"<td>{html.escape(connected_modes)}</td>"
            f"<td><span class='pill {status_class}'>{html.escape(stage)}</span></td>"
            f"<td>{onboarding['steps_done']}/{onboarding['steps_total']}</td>"
            f"<td>{html.escape(_format_duration(onboarding['stuck_for_sec']))}</td>"
            f"<td>{row['chat']['total']} <span class='muted'>u{row['chat']['user_messages']} / a{row['chat']['agent_messages']}</span></td>"
            f"<td>{row['memory']['total']} <span class='muted'>S{row['memory']['by_tab'].get('story', 0)} / A{row['memory']['by_tab'].get('about_me', 0)} / T{row['memory']['by_tab'].get('ta_thinking', 0)}</span>"
            f"<br><span class='muted'>cap {row['memory'].get('capture_actions_written', 0)} / edit {row['memory'].get('changes', 0)}</span></td>"
            f"<td>{row['proactive']['proactive_messages']} <span class='muted'>jobs {row['proactive']['jobs']} / fail {row['proactive']['failed_jobs']}</span></td>"
            f"<td>{html.escape(row.get('last_activity_at') or '')}</td>"
            "</tr>"
        )
    metrics = "".join([
        _render_metric("users", summary["users_total"]),
        _render_metric("onboarding done", summary["onboarding_completed"]),
        _render_metric("completion", f"{summary['completion_rate'] * 100:.0f}%"),
        _render_metric("chat messages", summary["chat_messages_total"]),
        _render_metric("memories", summary["memory_total"]),
    _render_metric("proactive jobs", summary["proactive_jobs_total"]),
    _render_metric("principals", summary.get("principals_total", summary["users_total"])),
    ])
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedling Beta Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; --ok:#1d7a4d; --warn:#a05a00; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:1280px; margin:0 auto; padding:28px 24px 48px; }}
    h1 {{ font-size:26px; margin:0 0 4px; }}
    h2 {{ font-size:16px; margin:28px 0 12px; }}
    .muted {{ color:var(--muted); }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:22px 0; }}
    .metric {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .metric-value {{ font-size:24px; font-weight:700; }}
    .metric-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
	    .toolbar {{ display:flex; gap:10px; align-items:center; margin:18px 0; }}
	    .viewbar,.sortbar,.pager {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:10px 0 18px; }}
	    .sort-button {{ display:inline-flex; align-items:center; border:1px solid var(--line); border-radius:6px; padding:7px 10px; background:var(--card); color:var(--fg); font-size:13px; }}
	    .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#fff1ed; }}
	    input {{ width:320px; max-width:100%; border:1px solid var(--line); border-radius:6px; padding:9px 10px; background:white; color:var(--fg); }}
    table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
    th {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; background:#f4ece5; }}
    tr:last-child td {{ border-bottom:0; }}
    a {{ color:var(--accent); text-decoration:none; }}
    .pill {{ display:inline-flex; border-radius:999px; padding:2px 8px; font-size:12px; background:#efe7df; color:var(--muted); }}
    .pill.ok {{ color:var(--ok); background:#e7f3ed; }}
    .pill.warn {{ color:var(--warn); background:#fff1db; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
  </style>
</head>
<body>
<main>
	  <h1>Feedling Beta Data Track</h1>
	  <div class="muted">Generated {html.escape(summary["generated_at"])}. Metadata only; encrypted content is not read or rendered.</div>
	  <div class="muted">Showing {html.escape(str(pagination.get("returned", len(users))))} of {html.escape(str(pagination.get("total", summary["users_total"])))} filtered users. Since {html.escape(str(filters.get("since") or "all time"))}.</div>
	  {_render_data_track_view_nav("users")}
		  <section class="metrics">{metrics}</section>
	  <h2>Beta users</h2>
	  <div class="toolbar"><input id="q" placeholder="Filter user, route, stage"></div>
	  <div class="sortbar">{sort_controls}</div>
	  {pager}
	  <table id="users">
    <thead><tr><th>User</th><th>Route</th><th>Access</th><th>Onboarding</th><th>Steps</th><th>Stuck</th><th>Chat</th><th>Memory</th><th>Proactive</th><th>Last activity</th></tr></thead>
    <tbody>{''.join(rows_html) if rows_html else "<tr><td colspan='10' class='muted'>No users yet.</td></tr>"}</tbody>
  </table>
</main>
<script>
const q = document.getElementById('q');
q.addEventListener('input', () => {{
  const needle = q.value.toLowerCase();
  for (const tr of document.querySelectorAll('#users tbody tr')) {{
    tr.style.display = tr.textContent.toLowerCase().includes(needle) ? '' : 'none';
  }}
}});
</script>
	</body>
	</html>"""


def _render_data_track_dau_page(payload: dict) -> str:
    summary = payload["summary"]
    filters = payload.get("filters", {})
    rows = payload.get("rows", [])
    definition = payload.get("definition", {})
    api_qs = _data_track_qs(view=None, q=None, limit=None, offset=None, sort=None, dir=None)
    api_url = f"/v1/admin/data-track/dau?{api_qs}" if api_qs else "/v1/admin/data-track/dau"
    rows_html = []
    for row in rows:
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('day') or ''))}</td>"
            f"<td>{int(row.get('dau') or 0)}</td>"
            f"<td>{int(row.get('chat_dau') or 0)}</td>"
            f"<td>{int(row.get('tracking_dau') or 0)}</td>"
            f"<td>{int(row.get('active_events') or 0)}</td>"
            f"<td>{int(row.get('user_messages') or 0)}</td>"
            f"<td>{int(row.get('tracking_events') or 0)}</td>"
            f"<td>{html.escape(str(row.get('last_at') or ''))}</td>"
            "</tr>"
        )
    metrics = "".join([
        _render_metric("latest DAU", summary["latest_dau"]),
        _render_metric("latest day", summary.get("latest_day") or "n/a"),
        _render_metric("max DAU", summary["max_dau"]),
        _render_metric("avg DAU", f"{summary['avg_dau']:.1f}"),
        _render_metric("user messages", summary["user_messages"]),
        _render_metric("tracking events", summary["tracking_events"]),
    ])
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedling DAU · Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:1280px; margin:0 auto; padding:28px 24px 48px; }}
    h1 {{ font-size:26px; margin:0 0 4px; }}
    h2 {{ font-size:16px; margin:28px 0 12px; }}
    .muted {{ color:var(--muted); }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:22px 0; }}
    .metric {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .metric-value {{ font-size:24px; font-weight:700; }}
    .metric-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
    .viewbar,.toolbar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:14px 0 18px; }}
    .sort-button {{ display:inline-flex; align-items:center; border:1px solid var(--line); border-radius:6px; padding:7px 10px; background:var(--card); color:var(--fg); font-size:13px; }}
    .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#fff1ed; }}
    table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
    th {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; background:#f4ece5; }}
    tr:last-child td {{ border-bottom:0; }}
    a {{ color:var(--accent); text-decoration:none; }}
  </style>
</head>
<body>
<main>
  <h1>Feedling Beta Data Track</h1>
  <div class="muted">Generated {html.escape(summary["generated_at"])}. DAU timezone: {html.escape(summary["timezone"])}.</div>
  <div class="muted">Showing {html.escape(str(summary["days_returned"]))} active days. Since {html.escape(str(filters.get("since") or "all time"))}; days limit {html.escape(str(filters.get("days") or 30))}.</div>
  {_render_data_track_view_nav("dau")}
  <section class="metrics">{metrics}</section>
  <h2>Daily Active Users</h2>
  <div class="muted">{html.escape(definition.get("dau") or "")} {html.escape(definition.get("excluded") or "")}</div>
  <div class="toolbar"><a class="sort-button" href="{html.escape(api_url, quote=True)}">JSON</a></div>
  <table>
    <thead><tr><th>Beijing day</th><th>DAU</th><th>Chat DAU</th><th>Tracking DAU</th><th>Active events</th><th>User messages</th><th>Tracking events</th><th>Last active</th></tr></thead>
    <tbody>{''.join(rows_html) if rows_html else "<tr><td colspan='8' class='muted'>No DAU activity in this range.</td></tr>"}</tbody>
  </table>
</main>
</body>
</html>"""


def _render_user_detail_page(user: dict) -> str:
    qs = _data_track_qs()
    back = f"/admin/data-track?{qs}" if qs else "/admin/data-track"
    safe_json = json.dumps(user, ensure_ascii=False, indent=2)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(user['user_id'])} · Feedling Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:1040px; margin:0 auto; padding:28px 24px 48px; }}
    a {{ color:var(--accent); text-decoration:none; }}
    h1 {{ font-size:24px; margin:14px 0 4px; }}
    .muted {{ color:var(--muted); }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin:20px 0; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .value {{ font-size:22px; font-weight:700; }}
    .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
  </style>
</head>
<body>
<main>
  <a href="{html.escape(back)}">Back to data track</a>
  <h1>{html.escape(user['user_id'])}</h1>
  <div class="muted">Principal {html.escape(user.get('principal_id') or '')}; route {html.escape(user['route'])}; stage {html.escape(user['onboarding']['stage'])}; metadata only.</div>
  <section class="grid">
    <div class="card"><div class="value">{user['onboarding']['steps_done']}/{user['onboarding']['steps_total']}</div><div class="label">onboarding steps</div></div>
    <div class="card"><div class="value">{html.escape(_format_duration(user['onboarding']['stuck_for_sec']))}</div><div class="label">stuck for</div></div>
    <div class="card"><div class="value">{user['chat']['total']}</div><div class="label">chat messages</div></div>
    <div class="card"><div class="value">{user['memory']['total']}</div><div class="label">memories</div></div>
    <div class="card"><div class="value">{user['proactive']['proactive_messages']}</div><div class="label">proactive writes</div></div>
  </section>
  <pre>{html.escape(safe_json)}</pre>
</main>
</body>
</html>"""


@bp.route("/v1/admin/data-track/summary", methods=["GET"])
def admin_data_track_summary():
    require_admin()
    return jsonify(_data_track_payload(include_users=False))


@bp.route("/v1/admin/data-track/users", methods=["GET"])
def admin_data_track_users():
    require_admin()
    return jsonify(_data_track_payload(include_users=True))


@bp.route("/v1/admin/data-track/dau", methods=["GET"])
def admin_data_track_dau():
    require_admin()
    return jsonify(_data_track_dau_payload())


@bp.route("/v1/admin/data-track/users/<user_id>", methods=["GET"])
def admin_data_track_user(user_id: str):
    require_admin()
    with registry._users_lock:
        entry = next((dict(u) for u in registry._users if u.get("user_id") == user_id), None)
    if not entry:
        return jsonify({"error": "user_not_found"}), 404
    return jsonify({"user": _build_data_track_user(entry, include_detail=True)})


@bp.route("/admin/data-track", methods=["GET"])
def admin_data_track_page():
    require_admin()
    if (request.args.get("view") or "").strip().lower() == "dau":
        return Response(_render_data_track_dau_page(_data_track_dau_payload()), mimetype="text/html")
    return Response(_render_data_track_page(_data_track_payload(include_users=True)), mimetype="text/html")


@bp.route("/admin/data-track/users/<user_id>", methods=["GET"])
def admin_data_track_user_page(user_id: str):
    require_admin()
    with registry._users_lock:
        entry = next((dict(u) for u in registry._users if u.get("user_id") == user_id), None)
    if not entry:
        return Response("user not found", status=404, mimetype="text/plain")
    return Response(_render_user_detail_page(_build_data_track_user(entry, include_detail=True)), mimetype="text/html")


@bp.route("/v1/admin/store/evict", methods=["POST"])
def admin_store_evict():
    """Drop a user's cached in-process store so its next access rebuilds from
    PostgreSQL. Use after an out-of-band DB write (e.g. the orphan-account
    recovery tool) to surface the change immediately, instead of waiting for the
    cache TTL or a backend redeploy."""
    require_admin()
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("user_id") or request.args.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    evicted = core_store._evict_store(user_id)
    print(f"[admin:store/evict] user_id={user_id} evicted={evicted}")
    return jsonify({"evicted": evicted, "user_id": user_id})


# Synthetic chat-loop ping — server posts a marker user message,
# posts a synthetic ping, waits for an agent-role reply, reports back.
# This proves that some reply pipeline is alive. It cannot, by itself,
# prove that a one-shot CLI is resident; a bridge/fallback may answer.
