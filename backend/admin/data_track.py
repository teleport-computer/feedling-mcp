"""Admin data-track: per-user stats, DAU, HTML pages, store evict."""

import json
import hashlib
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from urllib.parse import parse_qs, quote

from core.reqctx import request

import db
import debug_trace
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


_PROVIDER_ATTEMPT_STREAM = "provider_attempts"
_PROVIDER_ATTEMPT_DETAIL_LIMIT = 200


# Injected by the assembly layer (asgi_app.py) — the real implementations live
# in hosted/onboarding_validation.py; admin sits below hosted, so the stub is
# declared here and assembly wires it.
def _latest_history_import_job(store):
    return None


def _onboarding_validation_payload(store):
    return {}


def _runtime_token_usage_summary(*, lane: str = "chat", within_days: int = 30) -> dict:
    """Injected by ``asgi_app`` to keep admin below Runtime V2."""
    return {
        "window_days": within_days,
        "sampled_turns": 0,
        "users": 0,
        "model_calls": 0,
        "usage_reported_calls": 0,
        "cache_reported_calls": 0,
        "usage_telemetry_coverage": None,
        "cache_telemetry_coverage": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cache_miss_tokens": None,
    }


def _data_track_qs(**updates) -> str:
    params: dict[str, str] = {}
    for key in (
        "admin_key", "since", "registered_since", "q", "limit", "offset", "sort",
        "dir", "view", "days", "user_id", "subsystem", "status", "trace_id",
        "mode", "reveal", "page", "event", "day",
    ):
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
    failed_reasons: dict[str, int] = {}
    kind_lanes = {"heartbeat": 0, "screen": 0, "other": 0}
    fail_lanes = {"heartbeat": 0, "screen": 0, "other": 0}
    for j in jobs:
        if not isinstance(j, dict):
            continue
        raw_kind = j.get("job_kind") or j.get("wake_kind") or j.get("trigger") or ""
        lane = _classify_proactive_kind(raw_kind)
        kind_lanes[lane] += 1
        if str(j.get("status") or "") in ("failed", "skipped"):
            fail_lanes[lane] += 1
            reason = str(j.get("status_reason") or "").strip() or "unknown"
            failed_reasons[reason] = failed_reasons.get(reason, 0) + 1
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
        "heartbeat_jobs": kind_lanes["heartbeat"],
        "screen_jobs": kind_lanes["screen"],
        "other_jobs": kind_lanes["other"],
        "heartbeat_failed": fail_lanes["heartbeat"],
        "screen_failed": fail_lanes["screen"],
        "pending_jobs": status_counts.get("pending", 0),
        "posted_jobs": status_counts.get("posted", 0) + status_counts.get("delivered", 0),
        "failed_jobs": failed,
        # Matches the existing failure-lane lifecycle: skipped is terminal and
        # grouped with failed, even though jobs_by_status keeps them distinct.
        "job_failed_reasons": failed_reasons,
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


def _safe_genesis_metadata(raw: dict | None) -> dict:
    metadata = raw if isinstance(raw, dict) else {}
    allowed = {
        "mode",
        "ingest",
        "history_tier",
        "window_count",
        "history_count",
        "timeline_span_days",
        "support_count",
        "warning_count",
        "content_bytes",
    }
    out: dict = {}
    for key in allowed:
        val = metadata.get(key)
        if isinstance(val, (str, int, float, bool)) or val is None:
            out[key] = val
    return out


def _safe_genesis_job(job: dict | None) -> dict:
    raw = job if isinstance(job, dict) else {}
    output = raw.get("output") if isinstance(raw.get("output"), dict) else {}
    return {
        "job_id": str(raw.get("job_id") or ""),
        "status": str(raw.get("status") or ""),
        "source_kind": str(raw.get("source_kind") or ""),
        "total_chunks": int(raw.get("total_chunks") or 0),
        "processed_chunks": int(raw.get("processed_chunks") or 0),
        "total_bytes": int(raw.get("total_bytes") or 0),
        "memory_action_count": int(raw.get("memory_action_count") or 0),
        "identity_status": str(raw.get("identity_status") or ""),
        "persona_ref_present": bool(str(raw.get("persona_ref") or "").strip()),
        "error": str(raw.get("error") or "")[:240],
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
        "completed_at": str(raw.get("completed_at") or ""),
        "metadata": _safe_genesis_metadata(raw.get("metadata")),
        "stage": str(output.get("stage") or "")[:80],
    }


def _genesis_stats(store: UserStore, *, include_jobs: bool = False) -> dict:
    state = db.get_blob(store.user_id, "genesis_state")
    state_doc = state if isinstance(state, dict) else {}
    try:
        jobs_raw = db.genesis_list_jobs(store.user_id, limit=5 if include_jobs else 1)
    except Exception:
        jobs_raw = []
    jobs = [_safe_genesis_job(j) for j in jobs_raw if isinstance(j, dict)]
    latest = jobs[0] if jobs else {}
    return {
        "has_state": bool(state_doc),
        "status": str(state_doc.get("status") or latest.get("status") or ""),
        "job_status": str(state_doc.get("job_status") or latest.get("status") or ""),
        "job_id": str(state_doc.get("job_id") or latest.get("job_id") or ""),
        "source_kind": str(state_doc.get("source_kind") or latest.get("source_kind") or ""),
        "updated_at": str(state_doc.get("updated_at") or latest.get("updated_at") or ""),
        "completed_at": str(state_doc.get("completed_at") or latest.get("completed_at") or ""),
        "memory_action_count": int(state_doc.get("memory_action_count") or latest.get("memory_action_count") or 0),
        "identity_status": str(state_doc.get("identity_status") or latest.get("identity_status") or ""),
        "persona_ref_present": bool(str(state_doc.get("persona_ref") or "").strip() or latest.get("persona_ref_present")),
        "error": str(state_doc.get("error") or latest.get("error") or "")[:240],
        "job_count": len(jobs),
        "latest_job": latest,
        "jobs": jobs if include_jobs else [],
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


_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_DAU_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidDauDay(ValueError):
    """The admin DAU histogram received an invalid YYYY-MM-DD selector."""


def _validated_dau_day(value: str) -> str:
    raw = str(value or "").strip()
    if not _DAU_DAY_RE.fullmatch(raw):
        raise InvalidDauDay("invalid_day")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidDauDay("invalid_day") from exc
    if parsed.isoformat() != raw:
        raise InvalidDauDay("invalid_day")
    return raw


def _default_usage_histogram_day(rows: list[dict]) -> str:
    """Latest completed Beijing day present in the DAU table, or yesterday."""
    today = datetime.now(_SHANGHAI_TZ).date()
    for row in rows:
        raw = str(row.get("day") or "").strip()
        if not _DAU_DAY_RE.fullmatch(raw):
            continue
        try:
            candidate = date.fromisoformat(raw)
        except ValueError:
            continue
        if candidate < today:
            return candidate.isoformat()
    return (today - timedelta(days=1)).isoformat()


def _bj_iso(value) -> str:
    """Display a UTC epoch or UTC-ISO string in Beijing time (UTC+8).
    Display-only — storage and the JSON API stay UTC. Empty in -> empty out."""
    if value is None or value == "" or value == 0:
        return ""
    if isinstance(value, (int, float)):
        epoch = float(value)
    else:
        s = str(value).strip()
        try:
            epoch = float(s)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)  # stored ISO is UTC
                epoch = dt.timestamp()
            except ValueError:
                return str(value)
    if not epoch:
        return ""
    # fail-soft: a NaN / out-of-range epoch must never 500 the whole admin page.
    try:
        return datetime.fromtimestamp(epoch, _SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return str(value)


_ISO_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _bj_deep(obj):
    """Recursively convert every ISO-8601 datetime STRING in a payload clone to
    Beijing (UTC+8) for HTML display. Display-only — the JSON API is unchanged.
    Non-timestamp strings and all other types pass through untouched."""
    if isinstance(obj, str):
        return _bj_iso(obj) if _ISO_DT_RE.match(obj) else obj
    if isinstance(obj, dict):
        return {k: _bj_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bj_deep(v) for v in obj]
    return obj


def _data_track_app_usage_from_snapshot(snap: dict) -> dict:
    """Per-user app usage from db snapshot's ``app_usage`` (iOS app_session_end
    aggregation). ``last_at`` is a server ingest epoch; keep it raw for the
    Shanghai-day DAU roll-up and also expose an ISO string for display."""
    au = dict(snap.get("app_usage") or {})
    try:
        last_epoch = float(au.get("last_at") or 0) or 0.0
    except (TypeError, ValueError):
        last_epoch = 0.0
    return {
        "foreground_sec": int(au.get("foreground_sec") or 0),
        "sessions": int(au.get("sessions") or 0),
        "last_at_epoch": last_epoch,
        "last_at": _data_track_iso(last_epoch) if last_epoch else "",
    }


def _epoch_is_today_shanghai(epoch: float, now_epoch: float) -> bool:
    """True when ``epoch`` falls on the same Asia/Shanghai calendar day as
    ``now_epoch``. Explicit ZoneInfo — never the host's local TZ."""
    if not epoch:
        return False
    try:
        d1 = datetime.fromtimestamp(float(epoch), _SHANGHAI_TZ).date()
        d2 = datetime.fromtimestamp(float(now_epoch), _SHANGHAI_TZ).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return False
    return d1 == d2


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


_SCREEN_PROACTIVE_KINDS = frozenset({
    "screen_watch", "scene_change", "screen_tick", "broadcast_opened",
    "heartbeat_broadcast_on",
})


def _classify_proactive_kind(kind: str) -> str:
    """Bucket a raw proactive job kind/trigger into the two product lanes the
    current runtime has: ``heartbeat`` (the main self-initiated tick) vs
    ``screen`` (screen-share / broadcast driven). Anything unrecognized falls
    to ``other`` so the split never silently drops jobs."""
    norm = str(kind or "").strip().lower()
    if not norm or norm == "unknown":
        return "other"
    if norm in _SCREEN_PROACTIVE_KINDS:
        return "screen"
    # 现网自发 tick 的 kind 是 presence；heartbeat* 为历史 kind
    # (heartbeat, heartbeat_no_frame, heartbeat_unknown, heartbeat_broadcast_off …)。
    if norm == "presence" or norm.startswith("heartbeat"):
        return "heartbeat"
    return "other"


def _bucket_proactive_kinds(by_kind: dict) -> dict:
    """{raw_kind: count} → {heartbeat, screen, other} lane totals."""
    out = {"heartbeat": 0, "screen": 0, "other": 0}
    for kind, count in (by_kind or {}).items():
        out[_classify_proactive_kind(kind)] += int(count or 0)
    return out


_CONN_STALE_H = 6.0  # resident consumer considered offline after this many hours silent


def _connection_health(route: str, access_modes: list, chat: dict) -> dict:
    """Live-connection state per user from EXISTING signals (no new埋点):
    resident → the consumer's poll heartbeat (access-mode ``last_seen_at``);
    model_api → whether recent user messages are getting AI replies back;
    official_import → n/a (import only, no live loop)."""
    now = time.time()

    def age_h(iso):
        e = core_util._to_epoch(iso)
        return None if not e else (now - e) / 3600.0

    if route == "model_api":
        lu = age_h(chat.get("last_user_at"))
        la = age_h(chat.get("last_agent_at"))
        agent_n = int(chat.get("model_api_agent_messages") or chat.get("agent_messages") or 0)
        if lu is not None and lu <= 72 and (agent_n == 0 or (la is not None and la - lu > 24)):
            return {"status": "stalled", "label": "有去无回", "last_seen_at": chat.get("last_agent_at") or "", "stale_h": lu}
        return {"status": "ok", "label": "在线", "last_seen_at": chat.get("last_agent_at") or "", "stale_h": la}
    if route == "official_import":
        return {"status": "na", "label": "导入", "last_seen_at": "", "stale_h": None}
    modes = {m.get("access_mode"): m for m in (access_modes or [])}
    rm = modes.get("resident", {})
    seen = rm.get("last_seen_at") or ""
    sh = age_h(seen)
    if rm.get("connected") and (sh is None or sh > _CONN_STALE_H):
        return {"status": "offline", "label": "掉线", "last_seen_at": seen, "stale_h": sh}
    if rm.get("connected"):
        return {"status": "ok", "label": "在线", "last_seen_at": seen, "stale_h": sh}
    return {"status": "idle", "label": "未连接", "last_seen_at": seen, "stale_h": sh}


def _data_track_proactive_from_snapshot(snap: dict, chat: dict) -> dict:
    logs = dict(snap.get("logs") or {})
    extra = dict(snap.get("proactive_extra") or {})
    status_counts = _data_track_count_dict(extra.get("jobs_by_status"))
    kind_lanes = _bucket_proactive_kinds(_data_track_count_dict(extra.get("jobs_by_kind")))
    fail_lanes = _bucket_proactive_kinds(_data_track_count_dict(extra.get("jobs_failed_by_kind")))
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
        "heartbeat_jobs": kind_lanes["heartbeat"],
        "screen_jobs": kind_lanes["screen"],
        "other_jobs": kind_lanes["other"],
        "heartbeat_failed": fail_lanes["heartbeat"],
        "screen_failed": fail_lanes["screen"],
        "pending_jobs": status_counts.get("pending", 0),
        "posted_jobs": status_counts.get("posted", 0) + status_counts.get("delivered", 0),
        "failed_jobs": failed,
        # Includes failed + skipped proactive jobs; delivery failures are not
        # job reasons and remain in live_activity_status / alert_status.
        "job_failed_reasons": _data_track_count_dict(extra.get("jobs_failed_by_reason")),
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
    # Current-reality onboarding check (2026-07 redo). The old 7-step validator
    # asserted removed MCP tools (feedling_identity_init / _chat_verify_loop /
    # _chat_post_message) and retired story/about_me/ta_thinking memory tabs, so
    # every Genesis/Runtime V2 user looked permanently stuck. The live flow has
    # exactly four ground-truth milestones — identity written, memory distilled,
    # a live connection, and the first AI-visible message — evaluated the same
    # way the authoritative hosted validator does (memory_count > 0, etc.).
    memory_total = int(memory.get("total") or 0)
    has_memories = memory_total > 0
    identity_written = identity is not None

    if route == "model_api":
        # Hosted: "connection" = the backend actually produced a hosted reply.
        connected = bool(
            chat.get("model_api_greetings")
            or (chat.get("model_api_user_messages") and chat.get("model_api_agent_messages"))
        )
        agent_spoke = int(chat.get("model_api_agent_messages") or chat.get("model_api_greetings") or 0) > 0
        conn_hint = "托管聊天尚未产生 AI 回复（多为 provider key 解密为空 / 网关未注册）。"
        norm_route = "model_api"
    elif route == "official_import":
        # Import route has no live consumer; "connection" = the import landed
        # both identity and memory.
        connected = has_memories and identity_written
        agent_spoke = int(chat.get("agent_messages") or 0) > 0
        conn_hint = "官方导入尚未同时写入身份与记忆。"
        norm_route = "official_import"
    else:  # resident / self-host
        consumer = consumer_state or {}
        try:
            age_sec = time.time() - float(consumer.get("last_poll_epoch") or 0)
        except Exception:
            age_sec = None
        consumer_ok = (
            bool(consumer.get("official"))
            and age_sec is not None
            and age_sec <= chat_consumer._CONSUMER_RECENT_SEC
        )
        connected = consumer_ok or bool(chat.get("user_messages") and chat.get("agent_messages"))
        agent_spoke = int(chat.get("agent_messages") or 0) > 0
        conn_hint = "常驻 consumer 未在轮询（resident 未上线或已掉线）。"
        norm_route = "resident"

    def step(step_id: str, label: str, passing: bool, required: str = "") -> dict:
        return {"id": step_id, "label": label, "passing": bool(passing), "required": "" if passing else required}

    steps = [
        step("identity", "身份 Identity", identity_written, "尚未写入 Identity Card。"),
        step("memory", "记忆 Memory", has_memories, "蒸馏尚未写入任何记忆卡。"),
        step("connection", "连接 Connection", connected, conn_hint),
        step("first_message", "首消息 First Message", agent_spoke, "IO 尚未发出第一条可见消息。"),
    ]
    next_step = next((s for s in steps if not s["passing"]), None)
    return {
        "passing": next_step is None,
        "stage": "complete" if next_step is None else next_step["id"],
        "route": norm_route,
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
        "connection": _connection_health(route, access_modes, chat),
        "push": _push_stats_from_user_entry(user_entry),
        "tracking": tracking,
        "app_usage": _data_track_app_usage_from_snapshot(snap),
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


def _runtime_summary(store: UserStore) -> dict:
    """Non-secret hosting-runtime facts (detail view): which driver/provider/
    transport serves this user. Closes the blind spot behind 'agent can't read
    memory' — a codex-driven user whose Bash io_cli reads would break in-CVM
    unless the default sandbox-bypass command is used, or a gateway-routed
    provider. Metadata only — no api_key / base_url is read."""
    out = {"provider": "", "model": "", "test_status": "",
           "driver": "", "codex_transport": "", "cli_cmd_custom": False,
           "reasoning_effort": ""}
    try:
        from hosted import config_store as _cfg_store
        cfg = _cfg_store._load_model_api_config(store) or {}
    except Exception:
        return out
    provider = str(cfg.get("provider") or "")
    out.update({
        "provider": provider,
        "model": str(cfg.get("model") or ""),
        "test_status": str(cfg.get("test_status") or ""),
        "reasoning_effort": str(cfg.get("reasoning_effort") or ""),
        # A custom cli_cmd drops the default codex --dangerously-bypass command,
        # which is what makes io_cli memory reads work in-CVM. True → suspicious.
        "cli_cmd_custom": bool(str(cfg.get("cli_cmd") or "").strip()),
    })
    if provider:
        try:
            from hosted import agent_runtime_cutover as _cutover
            out["driver"] = _cutover.driver_for_provider(provider)
            out["codex_transport"] = _cutover.codex_transport(provider)
        except Exception:
            pass
    return out


def _provider_attempts_detail(store: UserStore) -> dict:
    rows = db.log_read(
        store.user_id,
        _PROVIDER_ATTEMPT_STREAM,
        limit=_PROVIDER_ATTEMPT_DETAIL_LIMIT + 1,
    )
    return {
        "coverage": "chat_turns_only",
        "attempts": rows[-_PROVIDER_ATTEMPT_DETAIL_LIMIT:],
        "has_more": len(rows) > _PROVIDER_ATTEMPT_DETAIL_LIMIT,
    }


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
    genesis = _genesis_stats(store, include_jobs=include_detail)
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
        genesis.get("updated_at"),
        genesis.get("completed_at"),
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
        "connection": _connection_health(route, access_modes, chat),
        "push": push,
        "tracking": tracking,
        "bootstrap_events": bootstrap_events,
        "history_import": history_import,
        "genesis": genesis,
    }
    if include_detail:
        row["runtime"] = _runtime_summary(store)
        row["provider_attempt_ledger"] = _provider_attempts_detail(store)
        _ps = store.load_proactive_settings()
        row["perception_permissions"] = {
            # what the device reports it granted (free-form; keys are app-defined,
            # e.g. photos / screen / location / health / motion / calendar / audio)
            "permission_states": dict(_ps.get("permission_states") or {}),
            # per-user autonomy switches (all default on)
            "switches": {
                "ambient_陪伴": bool(_ps.get("enabled", True)),
                "dnd_勿扰": bool(_ps.get("dnd", False)),
                "scheduled_定时": bool(_ps.get("scheduled", True)),
                "dream_做梦": bool(_ps.get("dream_enabled", True)),
                "capture_记忆整理": bool(_ps.get("capture_enabled", True)),
                "screen_watch_屏幕观察": bool(_ps.get("screen_watch_enabled", True)),
                "photo_wake_照片唤醒": bool(_ps.get("photo_wake_enabled", True)),
                "arrival_wake_到达唤醒": bool(_ps.get("arrival_wake_enabled", True)),
                "unlock_wake_解锁唤醒": bool(_ps.get("unlock_wake_enabled", True)),
            },
            # User-authored natural language is private content. Data-track may
            # expose whether it is configured, never the directive itself.
            "wake_directive_configured": bool(str(_ps.get("wake_directive") or "").strip()),
            "wake_interval_sec": int(_ps.get("wake_interval_sec") or 0),
            "user_state": _ps.get("user_state"),
            "ai_state": _ps.get("ai_state"),
            "broadcast_state": _ps.get("broadcast_state"),
        }
        # Per-field last-report freshness (timestamps only, no values) so support
        # can tell "device stopped feeding X" from a backend read gap — the two
        # blind spots that cost us in usr_7f30 / usr_5d3d triage.
        try:
            from perception import service as _perception_service
            row["perception_freshness"] = _perception_service.admin_perception_freshness(
                str(user_entry.get("user_id") or "")
            )
        except Exception as e:  # noqa: BLE001 — observability must never 500 the page
            row["perception_freshness"] = {"error": f"{type(e).__name__}:{str(e)[:120]}"}
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
    if raw_view not in {"users", "dau", "proactive", "debug", "events"}:
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
            # Retired story/about_me/ta_thinking tabs are always 0 under genesis;
            # tie-break on real signal (change-log volume) instead.
            return (
                intval(memory.get("total")),
                intval(memory.get("changes")),
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
    # Read-only snapshot: do NOT normalize+persist here. load_users() already
    # normalizes on boot and on every cross-worker reload, so an admin GET must
    # not trigger a full-table rewrite (db.save_all_users) — that turned every
    # dashboard refresh into an O(N) scan + write on the hot path (2026-07 perf).
    with registry._users_lock:
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
    activated_route_counts: dict[str, int] = {}
    access_mode_counts: dict[str, int] = {}
    chat_total = 0
    memory_total = 0
    proactive_jobs = 0
    proactive_messages = 0
    proactive_failed = 0
    # Ground-truth activation funnel — derived from REAL behaviour (memory
    # written / messages sent / replies received / recency), independent of the
    # onboarding-stage label. This is the trustworthy usage view: a user who has
    # received an agent reply is genuinely onboarded-and-working, regardless of
    # what the stage validator says.
    now_epoch = time.time()
    fn_has_memory = fn_sent = fn_chatting = fn_active_3d = fn_active_1d = 0
    # "human active" = a real user message landed recently. Distinct from
    # active_1d/3d, which key off last_activity_at and therefore also fire on
    # pure background work (heartbeat/proactive jobs, memory writes) — a churned
    # user whose heartbeat still ticks looks "active" there but not here.
    human_active_1d = human_active_3d = 0
    activated = 0
    conn_offline = conn_stalled = 0
    # App usage-duration roll-up (iOS app_session_end). foreground-kill undercount
    # is expected (see analytics-app-session-end.md); this is a slight lower bound.
    au_fg_total = au_sessions_total = au_users_active = au_dau_today = 0
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
        if int(row["memory"].get("total") or 0) > 0:
            fn_has_memory += 1
        if int(row["chat"].get("user_messages") or 0) > 0:
            fn_sent += 1
        # "Activated" = did anything real: has a memory card OR sent a message.
        # Separates genuine humans from abandoned/duplicate registration rows.
        is_activated = int(row["memory"].get("total") or 0) > 0 or int(row["chat"].get("user_messages") or 0) > 0
        if is_activated:
            activated += 1
            activated_route_counts[route] = activated_route_counts.get(route, 0) + 1
        # Connection health only meaningful for users who actually use it.
        if is_activated:
            cstatus = (row.get("connection") or {}).get("status")
            if cstatus == "offline":
                conn_offline += 1
            elif cstatus == "stalled":
                conn_stalled += 1
        if int(row["chat"].get("agent_messages") or 0) > 0:
            fn_chatting += 1
        act_epoch = core_util._to_epoch(row.get("last_activity_at"))
        if act_epoch and (now_epoch - act_epoch) <= 3 * 86400:
            fn_active_3d += 1
        if act_epoch and (now_epoch - act_epoch) <= 86400:
            fn_active_1d += 1
        human_epoch = core_util._to_epoch(row["chat"].get("last_user_at"))
        if human_epoch and (now_epoch - human_epoch) <= 3 * 86400:
            human_active_3d += 1
        if human_epoch and (now_epoch - human_epoch) <= 86400:
            human_active_1d += 1
        au = row.get("app_usage") or {}
        au_sess = int(au.get("sessions") or 0)
        au_fg_total += int(au.get("foreground_sec") or 0)
        au_sessions_total += au_sess
        if au_sess >= 1:
            au_users_active += 1
            if _epoch_is_today_shanghai(au.get("last_at_epoch") or 0, now_epoch):
                au_dau_today += 1
    # Duplicate registration: one real person (principal_id) can hold several
    # user_id rows because re-onboarding never deletes the old row (only the
    # explicit reset endpoint does). Count principals carrying >1 row and the
    # number of surplus rows — this is most of the gap between 原始行 and 真人.
    principal_rows: dict[str, int] = {}
    for row in rows:
        pid = str(row.get("principal_id") or "")
        if pid:
            principal_rows[pid] = principal_rows.get(pid, 0) + 1
    dup_principals = sum(1 for c in principal_rows.values() if c > 1)
    dup_surplus_rows = sum(c - 1 for c in principal_rows.values() if c > 1)
    runtime_token_usage = _runtime_token_usage_summary(
        lane="chat",
        within_days=int(filters.get("days") or 30),
    )
    summary = {
        "generated_at": datetime.now().isoformat(),
        "users_total": len(rows),
        "activated_total": activated,
        "human_active_1d": human_active_1d,
        "human_active_3d": human_active_3d,
        "conn_offline": conn_offline,
        "conn_stalled": conn_stalled,
        "dup_principals": dup_principals,
        "dup_surplus_rows": dup_surplus_rows,
        "onboarding_completed": completed,
        "onboarding_incomplete": incomplete,
        "completion_rate": (completed / len(rows)) if rows else 0,
        "activation_funnel": {
            "registered": len(rows),
            "has_memory": fn_has_memory,
            "sent_first_message": fn_sent,
            "chatting": fn_chatting,
            "active_3d": fn_active_3d,
            "active_1d": fn_active_1d,
        },
        "stage_counts": stage_counts,
        "route_counts": route_counts,
        "activated_route_counts": activated_route_counts,
        "access_mode_counts": access_mode_counts,
        "principals_total": len(set(r.get("principal_id") or r.get("user_id") for r in rows)),
        "chat_messages_total": chat_total,
        "memory_total": memory_total,
        "memory_avg_per_user": (memory_total / len(rows)) if rows else 0,
        "proactive_jobs_total": proactive_jobs,
        "proactive_messages_total": proactive_messages,
        "proactive_failed_total": proactive_failed,
        "app_usage": {
            "foreground_sec_total": au_fg_total,
            "sessions_total": au_sessions_total,
            "avg_session_sec": (au_fg_total / au_sessions_total) if au_sessions_total else 0,
            "users_active": au_users_active,
            "dau_today": au_dau_today,
        },
        "runtime_token_usage": runtime_token_usage,
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


def _debug_trace_events_from_blobs(
    user_id: str,
    enabled_raw,
    raw,
) -> tuple[bool, list[dict]]:
    if debug_trace._hard_disabled():
        enabled = False
    elif isinstance(enabled_raw, dict) and "enabled" in enabled_raw:
        enabled = bool(enabled_raw.get("enabled"))
    elif enabled_raw is None:
        enabled = debug_trace._default_enabled()
    else:
        enabled = bool(enabled_raw)
    raw = raw or {}
    events = raw.get("events") if isinstance(raw, dict) and isinstance(raw.get("events"), list) else []
    out = []
    for e in events:
        if not isinstance(e, dict):
            continue
        ev = dict(e)
        ev["user_id"] = user_id
        try:
            ev["ts"] = float(ev.get("ts") or 0)
        except (TypeError, ValueError):
            ev["ts"] = 0.0
        out.append(ev)
    return enabled, out


def _debug_trace_stem(event_type: str) -> str:
    typ = str(event_type or "")
    for suffix in (".start", ".done", ".error"):
        if typ.endswith(suffix):
            return typ[:-len(suffix)]
    return typ


def _debug_trace_detect_stall(events: list[dict]) -> bool:
    open_stems: set[str] = set()
    for ev in sorted(events, key=lambda e: float(e.get("ts") or 0)):
        typ = str(ev.get("type") or "")
        stem = _debug_trace_stem(typ)
        if typ.endswith(".start"):
            open_stems.add(stem)
        elif typ.endswith(".done") or typ.endswith(".error"):
            open_stems.discard(stem)
    return bool(open_stems)


def _debug_trace_group_turns(events: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str], list[dict]] = {}
    for ev in events:
        trace_id = str(ev.get("trace_id") or "ungrouped")
        user_id = str(ev.get("user_id") or "")
        buckets.setdefault((user_id, trace_id), []).append(ev)
    turns = []
    for (user_id, trace_id), rows in buckets.items():
        ordered = sorted(rows, key=lambda e: float(e.get("ts") or 0))
        stalled = _debug_trace_detect_stall(ordered)
        any_error = any(str(e.get("status") or "") in {"error", "failed"} for e in ordered)
        any_blocked = any(str(e.get("status") or "") == "blocked" for e in ordered)
        terminal = "stalled" if stalled else ("error" if any_error else ("blocked" if any_blocked else "ok"))
        total = 0.0
        for ev in ordered:
            try:
                total += float(ev.get("dur_ms") or 0)
            except (TypeError, ValueError):
                continue
        title = (
            next((str(e.get("explain") or "") for e in ordered if str(e.get("explain") or "")), "")
            or next((str(e.get("summary") or "") for e in ordered if str(e.get("summary") or "")), "")
            or trace_id
        )
        turns.append({
            "user_id": user_id,
            "trace_id": trace_id,
            "rows": ordered,
            "title": title,
            "first_ts": ordered[0].get("ts") if ordered else 0,
            "last_ts": ordered[-1].get("ts") if ordered else 0,
            "total_dur_ms": round(total, 1),
            "terminal_status": terminal,
            "is_stalled": stalled,
        })
    turns.sort(key=lambda t: float(t.get("last_ts") or 0), reverse=True)
    return turns


def _debug_trace_search_text(ev: dict) -> str:
    parts = [
        ev.get("user_id"),
        ev.get("trace_id"),
        ev.get("subsystem"),
        ev.get("type"),
        ev.get("status"),
        ev.get("summary"),
        ev.get("explain"),
        ev.get("detail"),
        ev.get("content_excerpt"),
    ]
    return " ".join(str(p or "") for p in parts).lower()


def _debug_event_key(ev: dict) -> str:
    try:
        ts = f"{float(ev.get('ts') or 0):.6f}"
    except (TypeError, ValueError):
        ts = str(ev.get("ts") or "")
    raw = "|".join([
        str(ev.get("user_id") or ""),
        str(ev.get("trace_id") or ""),
        ts,
        str(ev.get("type") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _debug_redact_value(value, *, key: str = ""):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        safe_string_keys = {"model", "provider", "subsystem", "type", "status", "route", "stage", "actor"}
        if key in safe_string_keys:
            return value
        return f"<redacted string len={len(value)}>"
    if isinstance(value, list):
        return [_debug_redact_value(v) for v in value[:20]] + ([f"<redacted {len(value) - 20} more items>"] if len(value) > 20 else [])
    if isinstance(value, dict):
        return {str(k): _debug_redact_value(v, key=str(k)) for k, v in value.items()}
    return f"<redacted {type(value).__name__}>"


def _debug_content_summary(value) -> dict:
    if not isinstance(value, dict):
        return {"has_plaintext": bool(value), "shape": type(value).__name__}
    out = {}
    for key, item in value.items():
        if isinstance(item, str):
            out[str(key)] = {"redacted": True, "chars": len(item)}
        elif isinstance(item, (list, dict)):
            out[str(key)] = {"redacted": True, "shape": type(item).__name__, "items": len(item)}
        else:
            out[str(key)] = _debug_redact_value(item, key=str(key))
    return out


def _debug_event_public_json(ev: dict) -> dict:
    return {
        "ts": ev.get("ts"),
        "user_id": ev.get("user_id"),
        "trace_id": ev.get("trace_id"),
        "subsystem": ev.get("subsystem"),
        "type": ev.get("type"),
        "status": ev.get("status"),
        "dur_ms": ev.get("dur_ms"),
        "summary": ev.get("summary"),
        "explain": ev.get("explain"),
        "detail": _debug_redact_value(ev.get("detail") or {}),
        "content_excerpt": _debug_content_summary(ev.get("content_excerpt") or {}),
    }


def _debug_filter_options(events: list[dict]) -> dict:
    """Build stable filter choices from the current ring-buffer sample."""
    subsystems = sorted({str(e.get("subsystem") or "").strip() for e in events if str(e.get("subsystem") or "").strip()})
    statuses = sorted({str(e.get("status") or "").strip().lower() for e in events if str(e.get("status") or "").strip()})
    preferred_subsystems = ["route", "context", "agent", "memory", "genesis", "debug_trace"]
    ordered_subsystems = [s for s in preferred_subsystems if s in subsystems]
    ordered_subsystems.extend([s for s in subsystems if s not in ordered_subsystems])
    preferred_statuses = ["ok", "error", "failed", "blocked", "stalled"]
    ordered_statuses = [s for s in preferred_statuses if s in statuses]
    ordered_statuses.extend([s for s in statuses if s not in ordered_statuses])
    return {
        "subsystems": ordered_subsystems,
        "statuses": ordered_statuses,
    }


def _data_track_debug_payload() -> dict:
    filters = _data_track_request_filters()
    limit = int(filters.get("limit") or 100)
    offset = int(filters.get("offset") or 0)
    try:
        page = int((request.args.get("page") or "").strip() or 0)
    except (TypeError, ValueError):
        page = 0
    if page > 0:
        offset = (page - 1) * limit
    user_filter = (request.args.get("user_id") or "").strip()
    subsystem_filter = (request.args.get("subsystem") or "").strip()
    status_filter = (request.args.get("status") or "").strip().lower()
    trace_filter = (request.args.get("trace_id") or "").strip()
    reveal_key = (request.args.get("reveal") or "").strip()
    # Default to the readable per-turn timeline when drilling into one user (or a
    # single trace); the flat event table stays the default for the firehose view.
    raw_mode = (request.args.get("mode") or "").strip().lower()
    if raw_mode in {"flat", "timeline"}:
        mode = raw_mode
    elif user_filter or trace_filter:
        mode = "timeline"
    else:
        mode = "flat"
    q = str(filters.get("q") or "").strip().lower()
    since_epoch = float(filters.get("since_epoch") or 0)

    with registry._users_lock:
        users = [dict(u) for u in registry._users if u.get("user_id")]
    if user_filter:
        users = [u for u in users if str(u.get("user_id") or "") == user_filter]

    user_ids = [str(user.get("user_id") or "") for user in users]
    trace_blobs = db.get_blobs_for_users(
        user_ids,
        ["v1_flow_trace_enabled", "v1_flow_trace"],
    )

    all_events_raw: list[dict] = []
    all_events: list[dict] = []
    user_rows: dict[str, dict] = {}
    for user in users:
        uid = str(user.get("user_id") or "")
        enabled, events = _debug_trace_events_from_blobs(
            uid,
            trace_blobs.get((uid, "v1_flow_trace_enabled")),
            trace_blobs.get((uid, "v1_flow_trace")),
        )
        all_events_raw.extend(events)
        matching = []
        for ev in events:
            if since_epoch and float(ev.get("ts") or 0) < since_epoch:
                continue
            if subsystem_filter and str(ev.get("subsystem") or "") != subsystem_filter:
                continue
            if trace_filter and trace_filter not in str(ev.get("trace_id") or ""):
                continue
            if q and q not in _debug_trace_search_text(ev):
                continue
            matching.append(ev)
        if matching or enabled:
            latest = max((float(e.get("ts") or 0) for e in matching), default=0)
            user_rows[uid] = {
                "user_id": uid,
                "principal_id": user.get("principal_id") or "",
                "enabled": enabled,
                "events": len(matching),
                "last_ts": latest,
                "last_at": core_util._epoch_to_iso(latest),
            }
        all_events.extend(matching)

    all_events = sorted(all_events, key=lambda e: float(e.get("ts") or 0), reverse=True)
    turns = _debug_trace_group_turns(all_events)
    if status_filter and status_filter != "all":
        turns = [t for t in turns if t.get("terminal_status") == status_filter]
        allowed = {(t["user_id"], t["trace_id"]) for t in turns}
        all_events = [
            e for e in all_events
            if (str(e.get("user_id") or ""), str(e.get("trace_id") or "ungrouped")) in allowed
        ]
        allowed_users = {t["user_id"] for t in turns}
        user_rows = {uid: row for uid, row in user_rows.items() if uid in allowed_users}

    if mode == "timeline":
        total = len(turns)
        turns_out = turns[offset:offset + limit]
        page_ids = {(t["user_id"], t["trace_id"]) for t in turns_out}
        events_out = [
            e for e in all_events
            if (str(e.get("user_id") or ""), str(e.get("trace_id") or "ungrouped")) in page_ids
        ]
    else:
        total = len(all_events)
        events_out = all_events[offset:offset + limit]
        page_ids = {(str(e.get("user_id") or ""), str(e.get("trace_id") or "ungrouped")) for e in events_out}
        turns_out = [t for t in turns if (t["user_id"], t["trace_id"]) in page_ids]
    pagination = {
        "limit": limit,
        "offset": offset,
        "total": total,
        "returned": len(turns_out) if mode == "timeline" else len(events_out),
        "next_offset": offset + limit if offset + limit < total else None,
        "prev_offset": max(0, offset - limit) if offset > 0 else None,
        "current_page": (offset // limit) + 1 if limit else 1,
        "total_pages": ((total + limit - 1) // limit) if limit else 1,
    }

    users_out = list(user_rows.values())
    users_out.sort(key=lambda u: float(u.get("last_ts") or 0), reverse=True)
    users_out = [u for u in users_out if not user_filter or u["user_id"] == user_filter]

    return {
        "summary": {
            "generated_at": datetime.now().isoformat(),
            "users_scanned": len(users),
            "users_with_events": sum(1 for u in users_out if int(u.get("events") or 0) > 0),
            "events_total": len(all_events),
            "turns_total": len(turns),
            "events_returned": len(events_out),
            "turns_returned": len(turns_out),
            "stalled_turns": sum(1 for t in turns if t.get("terminal_status") == "stalled"),
            "error_turns": sum(1 for t in turns if t.get("terminal_status") == "error"),
        },
        "filters": {
            "since": filters.get("since", ""),
            "q": q,
            "user_id": user_filter,
            "subsystem": subsystem_filter,
            "status": status_filter,
            "trace_id": trace_filter,
            "mode": mode,
            "reveal": reveal_key,
            "view": "debug",
            "page": str(page or ""),
        },
        "options": _debug_filter_options(all_events_raw),
        "pagination": pagination,
        "users": users_out,
        "turns": turns_out,
        "events": events_out,
    }


def _data_track_dau_payload() -> dict:
    filters = _data_track_request_filters()
    days = int(filters.get("days") or 30)
    raw_day = str(request.args.get("day") or "").strip()
    requested_day = _validated_dau_day(raw_day) if raw_day else ""
    snapshot = db.admin_dau_snapshot_bounds()
    rows = db.admin_data_track_dau(
        since_epoch=float(filters.get("since_epoch") or 0),
        days=days,
        tz="Asia/Shanghai",
    )
    histogram_day = requested_day or _default_usage_histogram_day(rows)
    usage_histogram = db.admin_data_track_usage_histogram(
        day=histogram_day,
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
        "snapshot_first_day": snapshot.get("first_day", ""),
        "snapshot_last_day": snapshot.get("last_day", ""),
        "snapshot_days": int(snapshot.get("days") or 0),
    }
    return {
        "summary": summary,
        "filters": {
            "since": filters.get("since", ""),
            "days": days,
            "day": histogram_day,
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
        "usage_histogram": usage_histogram,
        "definition": {
            "dau": "Distinct users with at least one user chat message or tracking event on the Beijing day.",
            "excluded": "Agent/openclaw messages, proactive writes, and verify_ping synthetic messages are excluded.",
            "timezone": "Asia/Shanghai",
        },
    }


def _data_track_proactive_daily_payload() -> dict:
    filters = _data_track_request_filters()
    days = int(filters.get("days") or 30)
    since_epoch = float(filters.get("since_epoch") or 0)
    rows = db.admin_data_track_proactive_daily(
        since_epoch=since_epoch, days=days, tz="Asia/Shanghai",
    )
    # 全量 kind 分桶 + 超速哨兵(2026-07 心跳暴增排查的教训:心跳/屏幕两列外
    # 全是大杂烩、人均超物理上限没人看见)。查询失败各自降级为空,不碍主表。
    kinds_by_day = db.admin_data_track_proactive_kinds(
        since_epoch=since_epoch, days=days, tz="Asia/Shanghai",
    )
    overspeed_by_day = db.admin_proactive_heartbeat_overspeed(
        since_epoch=since_epoch, days=min(days, 14), tz="Asia/Shanghai",
    )
    # Runtime V2 心跳走 agent_jobs(lane='heartbeat'),从不写 legacy 流——
    # 单独拉一列并入日报,dual 共存下两个 runtime 的心跳量并排可见
    # (2026-07-24 Seven 定补的 V2 观测盲区;超速哨兵在 db 层已 UNION 两源)。
    v2_hb_by_day = db.admin_v2_heartbeat_daily(
        since_epoch=since_epoch, days=days, tz="Asia/Shanghai",
    )
    out_rows = []
    v2_days_seen = set()
    for r in rows:
        delivered = int(r.get("delivered") or 0)
        completed = int(r.get("completed") or 0)
        failed = int(r.get("failed") or 0)
        # success rate over RESOLVED jobs (exclude still-pending) — a fairer
        # "did it work" denominator than raw jobs. completed（sleep/纯动作，
        # 醒了但决定不说话）算成功：口径衡量「系统是否健康」。
        ok = delivered + completed
        resolved = ok + failed
        day = str(r.get("day") or "")
        v2 = v2_hb_by_day.get(day) or {}
        v2_days_seen.add(day)
        out_rows.append({
            **r,
            "success_rate": (ok / resolved) if resolved else 0.0,
            "fail_rate": (failed / resolved) if resolved else 0.0,
            "kinds": kinds_by_day.get(day, {}),
            "overspeed_users": overspeed_by_day.get(day, []),
            "v2_heartbeat": int(v2.get("jobs") or 0),
            "v2_heartbeat_failed": int(v2.get("failed") or 0) + int(v2.get("expired") or 0),
        })
    # 某天只有 V2 心跳、legacy 流全空(V2 全量后的将来态)也要有行,不静默丢。
    for day, v2 in sorted(v2_hb_by_day.items(), reverse=True):
        if day in v2_days_seen:
            continue
        out_rows.append({
            "day": day, "jobs": 0, "delivered": 0, "completed": 0, "failed": 0,
            "skipped": 0, "pending": 0, "maintenance": 0, "maintenance_failed": 0,
            "screen": 0, "heartbeat": 0, "heartbeat_throttled": 0,
            "success_rate": 0.0, "fail_rate": 0.0,
            "kinds": kinds_by_day.get(day, {}),
            "overspeed_users": overspeed_by_day.get(day, []),
            "v2_heartbeat": int(v2.get("jobs") or 0),
            "v2_heartbeat_failed": int(v2.get("failed") or 0) + int(v2.get("expired") or 0),
        })
    out_rows.sort(key=lambda r: str(r.get("day") or ""), reverse=True)
    tot_jobs = sum(int(r.get("jobs") or 0) for r in rows)
    tot_deliv = sum(int(r.get("delivered") or 0) for r in rows)
    tot_completed = sum(int(r.get("completed") or 0) for r in rows)
    tot_fail = sum(int(r.get("failed") or 0) for r in rows)
    tot_maint = sum(int(r.get("maintenance") or 0) for r in rows)
    tot_maint_fail = sum(int(r.get("maintenance_failed") or 0) for r in rows)
    tot_resolved = tot_deliv + tot_completed + tot_fail
    latest = out_rows[0] if out_rows else {}
    summary = {
        "generated_at": datetime.now().isoformat(),
        "timezone": "Asia/Shanghai",
        "days_returned": len(out_rows),
        "latest_day": latest.get("day", ""),
        "latest_success_rate": latest.get("success_rate", 0.0),
        # 最新天可能是 V2-only 合成行(legacy 分母为 0)——顶部 metric 用这个
        # flag 显示 N/A 而不是假 0%(codex review (b))。
        "latest_has_legacy": bool(
            int(latest.get("delivered") or 0) + int(latest.get("completed") or 0)
            + int(latest.get("failed") or 0)
        ),
        "total_jobs": tot_jobs,
        "total_delivered": tot_deliv,
        "total_completed": tot_completed,
        "total_failed": tot_fail,
        "total_maintenance": tot_maint,
        "total_maintenance_failed": tot_maint_fail,
        "total_v2_heartbeat": sum(int(r.get("v2_heartbeat") or 0) for r in out_rows),
        "total_v2_heartbeat_failed": sum(int(r.get("v2_heartbeat_failed") or 0) for r in out_rows),
        "overall_success_rate": ((tot_deliv + tot_completed) / tot_resolved) if tot_resolved else 0.0,
        "overall_has_legacy": bool(tot_resolved),
    }
    return {
        "summary": summary,
        "filters": {"since": filters.get("since", ""), "days": days, "view": "proactive"},
        "rows": out_rows,
        "definition": {
            "success_rate": "wake-lane only: (delivered + completed) / (delivered + completed + failed). "
                            "completed = woke, decided, just didn't post (sleep / action-only) — counts as success. "
                            "memory-maintenance jobs, gate-skipped wakes and still-pending "
                            "jobs are all excluded from the denominator.",
            "lanes": "heartbeat = the main self-initiated tick (kind=presence); "
                     "screen = screen-share / broadcast driven; "
                     "maintenance = memory capture/dream/migrate (never user-facing).",
            "kinds": "per-day raw kind counts (job_kind → wake_kind → trigger fallback) — "
                     "every wake source visible, nothing lumped into a bucket.",
            "overspeed": "users whose daily heartbeat jobs exceed 86400/wake_interval_sec + 1 "
                         "(their own setting, default 7200 → cap 12/day). Any entry here means "
                         "the frequency gate is broken or bypassed — investigate, don't wait.",
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


def _fmt_duration_sec(value) -> str:
    """Seconds -> compact human duration (e.g. 137 -> '2m17s', 5400 -> '1h30m').
    None / non-numeric -> '—' (unknown), distinct from a real 0 -> '0s'."""
    if value is None:
        return "—"
    try:
        s = int(round(float(value)))
    except (TypeError, ValueError):
        return "—"
    if s <= 0:
        return "0s"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m}m" if m else f"{h}h"
    if m:
        return f"{m}m{sec}s" if sec else f"{m}m"
    return f"{sec}s"


def _fmt_count(value) -> str:
    """Compact dashboard count while preserving unknown-vs-zero semantics."""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_ratio(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _render_admin_login_page(error: bool = False, next_url: str = "/admin/data-track") -> str:
    """Password-gate login page for the admin dashboard. Posts to /admin/login,
    which validates FEEDLING_ADMIN_PASSWORD and sets the signed admin_session
    cookie (see admin.routes_asgi). Styled to match the data-track dashboard."""
    err_html = (
        "<div class='err'>密码不对，再试一次。</div>" if error else ""
    )
    safe_next = html.escape(next_url or "/admin/data-track", quote=True)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedling Data Track · 登录</title>
  <style>
    :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; display:flex; min-height:100vh; align-items:center; justify-content:center; }}
    .box {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:28px 26px; width:320px; box-shadow:0 2px 14px rgba(0,0,0,.05); }}
    h1 {{ font-size:19px; margin:0 0 4px; }}
    .sub {{ color:var(--muted); font-size:13px; margin:0 0 18px; }}
    input[type=password] {{ width:100%; box-sizing:border-box; padding:11px 12px; font-size:15px; border:1px solid var(--line); border-radius:8px; background:#fff; }}
    button {{ width:100%; margin-top:12px; padding:11px; font-size:15px; font-weight:600; color:#fff; background:var(--accent); border:0; border-radius:8px; cursor:pointer; }}
    .err {{ color:var(--accent); font-size:13px; margin-bottom:10px; }}
  </style>
</head>
<body>
  <form class="box" method="post" action="/admin/login">
    <h1>Feedling Data Track</h1>
    <p class="sub">团队内部面板 · 输入密码进入</p>
    {err_html}
    <input type="hidden" name="next" value="{safe_next}">
    <input type="password" name="password" placeholder="密码" autofocus autocomplete="current-password">
    <button type="submit">进入</button>
  </form>
</body>
</html>"""


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
        f"{nav_item('growth', '增长 & 留存')}"
        f"{nav_item('proactive', 'Proactive 日报')}"
        f"{nav_item('events', '事件健康度')}"
        f"{nav_item('debug', 'Debug')}"
        "</div>"
    )


def _memory_source_split(by_source: dict) -> tuple[int, int]:
    """(onboarding_written, live_written) from a memory by_source map. Under the
    current runtime memory is written almost entirely at onboarding (genesis /
    history import); the ``live`` bucket (background capture etc.) should be ~0,
    which is exactly what this surfaces. Unknown source strings default to live
    so a new write path can't hide inside the onboarding count."""
    onb = live = 0
    for src, count in (by_source or {}).items():
        s = str(src or "").lower()
        n = int(count or 0)
        if "genesis" in s or "import" in s or "onboard" in s:
            onb += n
        else:
            live += n
    return onb, live


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
        onb_mem, live_mem = _memory_source_split(row["memory"].get("by_source"))
        pro = row["proactive"]
        conn = row.get("connection") or {}
        conn_status = conn.get("status", "")
        conn_cls = "warn" if conn_status in ("offline", "stalled") else ("ok" if conn_status == "ok" else "muted")
        conn_age = conn.get("stale_h")
        conn_sub = f"{conn_age:.0f}h" if isinstance(conn_age, (int, float)) else ""
        rows_html.append(
            "<tr>"
            f"<td><a href='{html.escape(user_url)}'>{html.escape(row['user_id'])}</a>"
            f"<br><span class='muted'>{html.escape(principal_short)} · keys {access.get('api_keys_count', 0)}</span></td>"
            f"<td>{html.escape(row['route'])}</td>"
            f"<td><span class='pill {conn_cls}'>{html.escape(conn.get('label', '—'))}</span>"
            f"<br><span class='muted'>{html.escape(conn_sub)}</span></td>"
            f"<td><span class='pill {status_class}'>{html.escape(stage)}</span></td>"
            f"<td>{onboarding['steps_done']}/{onboarding['steps_total']}</td>"
            f"<td>{row['chat']['total']} <span class='muted'>u{row['chat']['user_messages']} / a{row['chat']['agent_messages']}</span></td>"
            f"<td>{row['memory']['total']} <span class='muted'>cards</span>"
            f"<br><span class='muted'>onb {onb_mem} / live {live_mem}</span></td>"
            f"<td>{pro['proactive_messages']} <span class='muted'>sent</span>"
            f"<br><span class='muted'>心跳 {pro.get('heartbeat_jobs', 0)}(f{pro.get('heartbeat_failed', 0)}) / "
            f"屏幕 {pro.get('screen_jobs', 0)}(f{pro.get('screen_failed', 0)})</span></td>"
            f"<td>{html.escape(_bj_iso(row.get('last_activity_at')))}</td>"
            "</tr>"
        )
    fn = summary.get("activation_funnel", {})
    reg = max(1, int(fn.get("registered") or summary["users_total"] or 1))
    def _pct(n): return f"{(int(n or 0) / reg) * 100:.0f}%"
    activated = int(summary.get("activated_total") or 0)
    raw_rows = int(summary["users_total"])
    off = int(summary.get("conn_offline") or 0)
    stalled = int(summary.get("conn_stalled") or 0)
    # 头条用「激活用户」(真正用起来的人),不用「注册」——注册行含重装/换机产生的孤儿号,
    # 每次静默重注册都新建一行且旧行不删,所以不是人数。principals_total 在 prod 恒等于
    # 注册行(每次重装新密钥=新 principal),去重无意义,不再展示。
    metrics = "".join([
        _render_metric("激活用户（真正用起来的人）", activated),
        _render_metric("累计注册行（含重装孤儿·非人数）", raw_rows),
        _render_metric("真人活跃 1d/3d（有人发消息）", f"{summary.get('human_active_1d', 0)} / {summary.get('human_active_3d', 0)}"),
        _render_metric("系统活跃 1d/3d（含后台任务）", f"{fn.get('active_1d', 0)} / {fn.get('active_3d', 0)}"),
        _render_metric("连接异常 掉线/有去无回", f"{off} / {stalled}"),
        _render_metric("在聊（收到过AI回复）", f"{fn.get('chatting', 0)} · {_pct(fn.get('chatting'))}"),
        _render_metric("onboarding 完成", summary["onboarding_completed"]),
        _render_metric("聊天消息总数", summary["chat_messages_total"]),
        _render_metric("记忆总数", summary["memory_total"]),
        _render_metric("主动任务数", summary["proactive_jobs_total"]),
    ])
    # Ground-truth activation funnel (behaviour-based, not stage-label-based).
    funnel_steps = [
        ("registered", "注册行（含重装孤儿·非人数）"),
        ("has_memory", "有记忆"),
        ("sent_first_message", "发过消息"),
        ("chatting", "收到AI回复(真在聊)"),
        ("active_3d", "近3天活跃"),
        ("active_1d", "近1天活跃"),
    ]
    funnel_html = "".join(
        f"<div class='fn-step'><div class='fn-num'>{int(fn.get(k) or 0)}</div>"
        f"<div class='fn-bar'><span style='width:{(int(fn.get(k) or 0)/reg)*100:.0f}%'></span></div>"
        f"<div class='fn-label'>{html.escape(label)} · {_pct(fn.get(k))}</div></div>"
        for k, label in funnel_steps
    )
    funnel_section = (
        "<h2>Activation funnel（真实使用漏斗 · 基于行为,不看 stage 标签）</h2>"
        f"<section class='funnel'>{funnel_html}</section>"
    )
    # One operator-facing telemetry surface: Runtime V2 model usage, deployment
    # route coverage, and iOS foreground duration. The three clocks are labelled
    # explicitly because model latency is not product engagement time.
    rt = summary.get("runtime_token_usage") or {}
    route_counts = summary.get("activated_route_counts") or {}
    cache_read = rt.get("cache_read_tokens")
    cache_miss = rt.get("cache_miss_tokens")
    cache_denominator = (
        int(cache_read) + int(cache_miss)
        if cache_read is not None and cache_miss is not None
        else 0
    )
    cache_hit_ratio = (
        int(cache_read) / cache_denominator if cache_denominator else None
    )
    token_window = int(rt.get("window_days") or 30)
    telemetry_metrics = "".join([
        _render_metric(f"全站 V2 token 总量（近 {token_window} 天）", _fmt_count(rt.get("total_tokens"))),
        _render_metric(f"全站 V2 输入 / 输出 token（近 {token_window} 天）", f"{_fmt_count(rt.get('prompt_tokens'))} / {_fmt_count(rt.get('completion_tokens'))}"),
        _render_metric(f"Prompt cache 读取 token（近 {token_window} 天）", _fmt_count(cache_read)),
        _render_metric(f"Prompt cache token 命中率（近 {token_window} 天）", _fmt_ratio(cache_hit_ratio)),
        _render_metric(f"Token 上报覆盖率（近 {token_window} 天）", _fmt_ratio(rt.get("usage_telemetry_coverage"))),
        _render_metric(f"全站 V2 使用账号 / turns（近 {token_window} 天）", f"{_fmt_count(rt.get('users'))} / {_fmt_count(rt.get('sampled_turns'))}"),
        _render_metric("托管激活账号（当前筛选）", _fmt_count(route_counts.get("model_api", 0))),
        _render_metric("自托管激活账号（当前筛选）", _fmt_count(route_counts.get("resident", 0))),
    ])
    telemetry_section = (
        "<h2>运营 Telemetry</h2>"
        f"<section class='metrics'>{telemetry_metrics}</section>"
        "<div class='muted'>Token 来自 provider usage，缺报会降低覆盖率而不会伪装成 0；"
        "prompt token 已包含 cache read/write，不与 cache 列重复相加。"
        "Token 是全站近期开销，不随用户搜索条件收窄；托管/自托管按当前页面筛选后、当前 route 的已激活账号行统计。"
        "从未联系官方 backend 的离线自托管实例不可观测，"
        "重装产生的旧账号也可能重复，因此它是可观测账号覆盖数，不是假装精确的真人/实例数。</div>"
    )
    # App 使用时长(iOS app_session_end 事件聚合 · summary['app_usage'] 由 db 层填充)。
    au = summary.get("app_usage") or {}
    if au.get("sessions_total"):
        usage_metrics = "".join([
            _render_metric("总前台时长", _fmt_duration_sec(au.get("foreground_sec_total"))),
            _render_metric("会话数（结束事件）", au.get("sessions_total", 0)),
            _render_metric("平均单次时长", _fmt_duration_sec(au.get("avg_session_sec"))),
            _render_metric("有使用时长的用户", au.get("users_active", 0)),
            _render_metric("今日活跃（按会话）", au.get("dau_today", 0)),
        ])
        app_usage_section = (
            "<h2>App 使用时长（iOS 前台时间 · 前台被杀会漏报，略偏低估）</h2>"
            f"<section class='metrics'>{usage_metrics}</section>"
        )
    else:
        app_usage_section = (
            "<h2>App 使用时长</h2>"
            "<div class='muted'>暂无 app_session_end 事件（iOS 上报后此处出现聚合）。</div>"
        )
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
    .funnel {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:8px 0 22px; }}
    .fn-step {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .fn-num {{ font-size:22px; font-weight:700; }}
    .fn-bar {{ height:6px; background:#eee3d9; border-radius:4px; margin:6px 0; overflow:hidden; }}
    .fn-bar span {{ display:block; height:100%; background:var(--accent); }}
    .fn-label {{ color:var(--muted); font-size:12px; }}
    .note-box {{ background:#fff8ef; border:1px solid #e8d8be; border-radius:8px; padding:12px 14px; margin:16px 0 4px; font-size:13px; line-height:1.6; color:#5a4d3c; }}
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
	  <div class="muted">Generated {html.escape(_bj_iso(summary["generated_at"]))}. Metadata only; encrypted content is not read or rendered.</div>
	  <div class="muted">Showing {html.escape(str(pagination.get("returned", len(users))))} of {html.escape(str(pagination.get("total", summary["users_total"])))} filtered users. Since {html.escape(str(filters.get("since") or "all time"))}.</div>
	  <div class="note-box">
	    <b>怎么读这些数：</b>
	    「<b>激活用户</b>」= 写过记忆或发过消息的人，<b>最接近真实用户数</b>。
	    「<b>累计注册行</b>」= 装过 app、生成过密钥的账户行累计；同一个人重装 / 换机 / 抹机后
	    app 会静默注册一个新号、旧号变孤儿不删，所以它<b>不是人数</b>、会远大于发出的邀请/邮件数。
	    删除账户是硬删（行直接消失），因此没有、也无法有「已删除账户数」这个指标。
	  </div>
	  {_render_data_track_view_nav("users")}
		  <section class="metrics">{metrics}</section>
		  {funnel_section}
		  {telemetry_section}
		  {app_usage_section}
	  <h2>Beta users</h2>
	  <div class="toolbar"><input id="q" placeholder="Filter user, route, stage"></div>
	  <div class="sortbar">{sort_controls}</div>
	  {pager}
	  <table id="users">
    <thead><tr><th>User</th><th>Route</th><th>连接</th><th>Onboarding</th><th>Steps</th><th>Stuck</th><th>Chat</th><th>Memory</th><th>Proactive 心跳/屏幕(fail)</th><th>Last activity</th></tr></thead>
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
    histogram = payload.get("usage_histogram") or {}
    histogram_day = str(
        histogram.get("day") or filters.get("day") or ""
    ).strip()
    histogram_buckets = list(histogram.get("buckets") or [])
    histogram_total = int(histogram.get("total_users") or 0)
    definition = payload.get("definition", {})
    api_qs = _data_track_qs(
        view=None,
        q=None,
        limit=None,
        offset=None,
        sort=None,
        dir=None,
        day=histogram_day,
    )
    api_url = f"/v1/admin/data-track/dau?{api_qs}" if api_qs else "/v1/admin/data-track/dau"
    _snap_first = str(summary.get("snapshot_first_day") or "")
    _cutover_html = (
        f"首个冻结日是 <b>{html.escape(_snap_first)}</b>。状态列以当天实际标记为准："
        "<b>已冻结</b>的数据不再变化；<b>今天</b>仍是实时数据，日结束后自动冻结。"
        f"<b>{html.escape(_snap_first)} 之前</b>的日期仍是实时重算，会随删号下降、偏少、仅供参考。"
        if _snap_first else
        "每日快照即将生效；生效后当天真实数据会冻结、不再随删号变化。"
    )
    rows_html = []
    for row in rows:
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('day') or ''))}</td>"
            + ("<td><span style='color:#1d7a4d;font-size:12px'>🔒 已冻结</span></td>"
               if row.get("frozen")
               else "<td><span style='color:#a05a00;font-size:12px'>⏱ 实时</span></td>")
            + f"<td>{int(row.get('dau') or 0)}</td>"
            f"<td>{int(row.get('chat_dau') or 0)}</td>"
            f"<td>{int(row.get('tracking_dau') or 0)}</td>"
            f"<td>{int(row.get('active_events') or 0)}</td>"
            f"<td>{int(row.get('user_messages') or 0)}</td>"
            f"<td>{int(row.get('tracking_events') or 0)}</td>"
            f"<td>{int(row.get('session_dau') or 0)}</td>"
            # 人均日使用时长 = 当天总前台时长 / 使用DAU(每个活跃用户当天实际用了多久)。
            # 之前是 avg_session_sec(每次会话均长),被大量前后台切换的微会话拉低,误导。
            f"<td><b>{_fmt_duration_sec((row.get('foreground_sec') or 0) / (row.get('session_dau') or 1))}</b></td>"
            # 中位数日使用时长 = 每用户当天前台总时长的中位数(典型用户),不被少数
            # 重度用户拉高。冻结前的历史天没存 per-user 分布,显示 "-"。
            + (f"<td>{_fmt_duration_sec(row.get('median_user_sec'))}</td>"
               if (row.get('median_user_sec') or 0) > 0
               else "<td class='muted'>-</td>")
            + f"<td>{int(row.get('session_count') or 0)}</td>"
            + f"<td>{html.escape(_bj_iso(row.get('last_at')))}</td>"
            + "</tr>"
        )
    histogram_days = []
    for row in rows:
        row_day = str(row.get("day") or "").strip()
        if not _DAU_DAY_RE.fullmatch(row_day):
            continue
        cls = "sort-button active" if row_day == histogram_day else "sort-button"
        href = _data_track_page_href(view="dau", day=row_day, offset=0)
        histogram_days.append(
            f"<a class='{cls}' href='{html.escape(href, quote=True)}'>"
            f"{html.escape(row_day)}</a>"
        )
    max_bucket_users = max(
        (int(bucket.get("users") or 0) for bucket in histogram_buckets),
        default=0,
    )
    histogram_rows = []
    for bucket in histogram_buckets:
        users = int(bucket.get("users") or 0)
        percent = (users * 100.0 / histogram_total) if histogram_total else 0.0
        width = (users * 100.0 / max_bucket_users) if max_bucket_users else 0.0
        histogram_rows.append(
            "<div class='hist-row'>"
            f"<div class='hist-label'>{html.escape(str(bucket.get('label') or ''))}</div>"
            "<div class='hist-track'>"
            f"<span style='width:{width:.2f}%'></span>"
            "</div>"
            f"<div class='hist-value'>{users} 人 · {percent:.1f}%</div>"
            "</div>"
        )
    histogram_summary = (
        f"样本 {histogram_total} 人 · "
        f"中位数 {_fmt_duration_sec(histogram.get('median_sec'))} · "
        f"均值 {_fmt_duration_sec(histogram.get('mean_sec'))} · "
        f"P90 {_fmt_duration_sec(histogram.get('p90_sec'))} · "
        f"最大值 {_fmt_duration_sec(histogram.get('max_sec'))}"
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
    .funnel {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:8px 0 22px; }}
    .fn-step {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .fn-num {{ font-size:22px; font-weight:700; }}
    .fn-bar {{ height:6px; background:#eee3d9; border-radius:4px; margin:6px 0; overflow:hidden; }}
    .fn-bar span {{ display:block; height:100%; background:var(--accent); }}
    .fn-label {{ color:var(--muted); font-size:12px; }}
    .viewbar,.toolbar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:14px 0 18px; }}
    .sort-button {{ display:inline-flex; align-items:center; border:1px solid var(--line); border-radius:6px; padding:7px 10px; background:var(--card); color:var(--fg); font-size:13px; }}
    .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#fff1ed; }}
    .histogram {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:16px; }}
    .hist-row {{ display:grid; grid-template-columns:72px minmax(120px,1fr) 110px; gap:10px; align-items:center; margin:9px 0; }}
    .hist-label {{ color:var(--muted); font-size:12px; text-align:right; }}
    .hist-track {{ height:18px; background:#f0e6dd; border-radius:4px; overflow:hidden; }}
    .hist-track span {{ display:block; height:100%; background:var(--accent); border-radius:4px; min-width:0; }}
    .hist-value {{ font-variant-numeric:tabular-nums; font-size:12px; }}
    .hist-summary {{ color:var(--muted); font-size:12px; margin-top:12px; }}
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
  <div class="muted">Generated {html.escape(_bj_iso(summary["generated_at"]))}. DAU timezone: {html.escape(summary["timezone"])}.</div>
  <div class="muted">Showing {html.escape(str(summary["days_returned"]))} active days. Since {html.escape(str(filters.get("since") or "all time"))}; days limit {html.escape(str(filters.get("days") or 30))}.</div>
  {_render_data_track_view_nav("dau")}
  <section class="metrics">{metrics}</section>
  <h2>使用时长分布 · {html.escape(histogram_day or "n/a")}</h2>
  <div class="toolbar">{''.join(histogram_days)}</div>
  <section class="histogram">
    {''.join(histogram_rows) if histogram_rows else "<div class='muted'>这一天没有 app_session_end 上报。</div>"}
    <div class="hist-summary">{html.escape(histogram_summary)}</div>
  </section>
  <h2>Daily Active Users</h2>
  <div style="background:#fff8ef;border:1px solid #e8d8be;border-radius:8px;padding:12px 14px;margin:10px 0;font-size:13px;line-height:1.7;color:#5a4d3c">
    <b>⚠️ 历史数据偏少 · 已知问题</b><br>
    实时重算的历史数据，是<b>每次打开页面时从当前还存在的数据算</b>的，没有冻结快照。用户<b>删除/重置账户后其消息会被级联删除</b>，会<b>追溯性地</b>减少他活跃过的每一天——所以那些天的 DAU 会随时间下降、<b>偏少、仅供参考</b>。<br>
    {_cutover_html}
  </div>
  <div class="muted">{html.escape(definition.get("dau") or "")} {html.escape(definition.get("excluded") or "")}</div>
  <div class="muted">使用DAU=当天有 app 后台上报（app_session_end 事件）的用户数；人均日使用时长=当天总前台时长÷使用DAU（均值，会被少数重度用户拉高）；中位数日使用时长=每个用户当天前台总时长的中位数（典型用户实际用了多久，不被少数重度用户拉高，和均值并看更可信）——冻结快照只存聚合值，改动前已冻结的历史天显示"-"；会话数=当天 app_session_end 事件数（含大量前后台切换的微会话）。前台被强杀会漏报，略偏低估。均按北京日。</div>
  <div class="toolbar"><a class="sort-button" href="{html.escape(api_url, quote=True)}">JSON</a></div>
  <table>
    <thead><tr><th>Beijing day</th><th>状态</th><th>DAU</th><th>Chat DAU</th><th>Tracking DAU</th><th>Active events</th><th>User messages</th><th>Tracking events</th><th>使用DAU</th><th>人均日使用时长</th><th>中位数日使用时长</th><th>会话数</th><th>Last active</th></tr></thead>
    <tbody>{''.join(rows_html) if rows_html else "<tr><td colspan='13' class='muted'>No DAU activity in this range.</td></tr>"}</tbody>
  </table>
  <div class="muted" style="margin-top:12px">使用时长分布口径：先汇总每位用户在所选北京日的全部 app_session_end duration_sec，再按固定右开区间分桶。样本=当天有上报的 {histogram_total} 位用户；没打开 App 或没有 app_session_end 上报的用户不计入，也不会补 0。</div>
</main>
</body>
</html>"""


def _data_track_growth_payload() -> dict:
    filters = _data_track_request_filters()
    days = int(filters.get("days") or 60)
    growth = db.admin_data_track_growth(days=days, tz="Asia/Shanghai")
    retention = db.admin_data_track_retention(tz="Asia/Shanghai")
    g_bounds = db.admin_growth_snapshot_bounds()
    latest = growth[-1] if growth else {}
    summary = {
        "generated_at": datetime.now().isoformat(),
        "timezone": "Asia/Shanghai",
        "days_returned": len(growth),
        "total_users": int(latest.get("cumulative") or 0),
        "latest_day": latest.get("day", ""),
        "latest_new": int(latest.get("new_users") or 0),
        "snapshot_first_day": g_bounds.get("first_day", ""),
        "snapshot_days": int(g_bounds.get("days") or 0),
        "cohort_count": len(retention.get("cohorts") or []),
    }
    return {
        "summary": summary,
        "filters": {"days": days, "view": "growth"},
        "growth": growth,
        "retention": retention,
    }


_GROWTH_STYLE = """
    :root { color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; }
    body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    main { max-width:1280px; margin:0 auto; padding:28px 24px 48px; }
    h1 { font-size:26px; margin:0 0 4px; }
    h2 { font-size:16px; margin:28px 0 12px; }
    .muted { color:var(--muted); font-size:13px; }
    .metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:22px 0; }
    .metric { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }
    .metric-value { font-size:24px; font-weight:700; }
    .metric-label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
    .viewbar,.toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:14px 0 18px; }
    .sort-button { display:inline-flex; align-items:center; border:1px solid var(--line); border-radius:6px; padding:7px 10px; background:var(--card); color:var(--fg); font-size:13px; }
    .sort-button.active { border-color:var(--accent); color:var(--accent); background:#fff1ed; }
    table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
    th,td { text-align:left; padding:9px 11px; border-bottom:1px solid var(--line); vertical-align:top; }
    th { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; background:#f4ece5; }
    tr:last-child td { border-bottom:0; }
    a { color:var(--accent); text-decoration:none; }
    .bar { display:inline-block; height:8px; background:var(--accent); border-radius:3px; vertical-align:middle; }
"""


def _retention_cell_bg(pct: float) -> str:
    # Heat: 0% pale, 100% saturated warm.
    frac = max(0.0, min(1.0, pct / 100.0))
    # blend from #fbf1ea (low) toward #b7352b (high) via alpha on accent.
    alpha = 0.10 + 0.75 * frac
    return f"background:rgba(183,53,43,{alpha:.2f})"


def _render_data_track_growth_page(payload: dict) -> str:
    summary = payload["summary"]
    growth = payload.get("growth", [])
    retention = payload.get("retention", {})
    api_qs = _data_track_qs(view=None, q=None, limit=None, offset=None, sort=None, dir=None)
    api_url = f"/v1/admin/data-track/growth?{api_qs}" if api_qs else "/v1/admin/data-track/growth"

    max_cum = max((int(r.get("cumulative") or 0) for r in growth), default=0) or 1
    growth_rows = []
    for row in reversed(growth):  # newest first
        cum = int(row.get("cumulative") or 0)
        width = int(round(120 * cum / max_cum))
        state = ("<span style='color:#1d7a4d;font-size:12px'>🔒 已冻结</span>"
                 if row.get("frozen")
                 else "<span style='color:#a05a00;font-size:12px'>⏱ 实时</span>")
        growth_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('day') or ''))}</td>"
            f"<td>{state}</td>"
            f"<td><b>{int(row.get('new_users') or 0)}</b></td>"
            f"<td>{cum} <span class='bar' style='width:{width}px'></span></td>"
            "</tr>"
        )

    cohorts = retention.get("cohorts", [])
    max_period = int(retention.get("max_period") or 0)
    head_cells = "".join(f"<th>W{p}</th>" for p in range(max_period + 1))
    ret_rows = []
    for c in sorted(cohorts, key=lambda x: x["cohort_week"], reverse=True):
        cells_html = []
        for p in range(max_period + 1):
            cell = c["cells"].get(p)
            if cell is None:
                cells_html.append("<td class='muted'></td>")
                continue
            mark = "" if cell["frozen"] else "*"
            cells_html.append(
                f"<td style='{_retention_cell_bg(cell['pct'])}'>"
                f"{cell['pct']:.0f}%<br><span class='muted'>{cell['active']}{mark}</span></td>"
            )
        ret_rows.append(
            "<tr>"
            f"<td><b>{html.escape(c['cohort_week'])}</b></td>"
            f"<td>{int(c['cohort_size'])}</td>"
            + "".join(cells_html)
            + "</tr>"
        )

    metrics = "".join([
        _render_metric("累计用户", summary["total_users"]),
        _render_metric("最新一天新增", summary["latest_new"]),
        _render_metric("cohort 数", summary["cohort_count"]),
        _render_metric("已冻结天数", summary["snapshot_days"]),
    ])
    snap_first = html.escape(str(summary.get("snapshot_first_day") or ""))
    boundary = (f"首个冻结日 <b>{snap_first}</b> 起,新增/留存不再随删号变化;之前的历史仍实时重算、会随删号偏少,仅供参考。"
                if snap_first else "每日快照即将生效;生效后完成日的新增/留存会冻结、不再随删号变化。")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedling 增长 & 留存 · Data Track</title>
  <style>{_GROWTH_STYLE}</style>
</head>
<body>
<main>
  <h1>增长 & 留存</h1>
  <div class="muted">Generated {html.escape(_bj_iso(summary["generated_at"]))} · {html.escape(summary["timezone"])}</div>
  {_render_data_track_view_nav("growth")}
  <section class="metrics">{metrics}</section>
  <div class="muted" style="background:#fff8ef;border:1px solid #e8d8be;border-radius:8px;padding:12px 14px;margin:10px 0;line-height:1.7">
    <b>⚠️ 口径与已知偏差</b><br>
    {boundary}<br>
    留存为<b>周 cohort</b>(北京周,行=注册周,列 W0=注册周自身…Wk=第 k 周);格子=该周仍活跃占比,小字=活跃人数(<b>*</b>=当前进行中的实时周,未冻结)。cohort_size 分母在首次冻结时钉死,删号不会缩小分母假抬留存。<br>
    <b>用户基数极小</b> → 曲线抖动大,当方向性参考,非统计显著。<b>③ 完全自部署</b>用户不在此表(不在我们后端注册)。"活跃"=当天有用户消息或 app tracking 事件,口径同 DAU。
  </div>
  <div class="toolbar"><a class="sort-button" href="{html.escape(api_url, quote=True)}">JSON</a></div>
  <h2>用户增长(新增 + 累计)</h2>
  <table>
    <thead><tr><th>Beijing day</th><th>状态</th><th>新增</th><th>累计</th></tr></thead>
    <tbody>{''.join(growth_rows) if growth_rows else "<tr><td colspan='4' class='muted'>暂无注册数据</td></tr>"}</tbody>
  </table>
  <h2>周 cohort 留存</h2>
  <table>
    <thead><tr><th>注册周</th><th>人数</th>{head_cells}</tr></thead>
    <tbody>{''.join(ret_rows) if ret_rows else f"<tr><td colspan='{max_period + 3}' class='muted'>暂无 cohort 数据</td></tr>"}</tbody>
  </table>
</main>
</body>
</html>"""


def _render_proactive_daily_page(payload: dict) -> str:
    summary = payload["summary"]
    rows = payload.get("rows", [])
    definition = payload.get("definition", {})
    api_qs = _data_track_qs(view=None, q=None, limit=None, offset=None, sort=None, dir=None)
    api_url = f"/v1/admin/data-track/proactive-daily?{api_qs}" if api_qs else "/v1/admin/data-track/proactive-daily"
    rows_html = []
    for row in rows:
        sr = float(row.get("success_rate") or 0.0)
        sr_cls = "ok" if sr >= 0.7 else ("warn" if sr >= 0.4 else "bad")
        # legacy wake lane 当天无已结 job(V2-only 天 / 空天)→ 成功率无分母,
        # 显示 — 而不是红 0%:那不是故障,是"这条口径当天没有样本"
        # (codex review (b):合成 V2-only 行曾被渲染成假红色告警)。
        legacy_resolved = (int(row.get("delivered") or 0)
                           + int(row.get("completed") or 0)
                           + int(row.get("failed") or 0))
        if legacy_resolved:
            sr_cell = (
                f"<td><b class='{sr_cls}'>{sr*100:.0f}%</b>"
                f"<div class='fn-bar'><span class='{sr_cls}' style='width:{sr*100:.0f}%'></span></div></td>"
            )
        else:
            sr_cell = "<td><span class='muted'>—</span></td>"
        overspeed = row.get("overspeed_users") or []
        # 超速哨兵:任何用户当天心跳 > 其 wake_interval 物理上限 → 标红。
        # 出现即频率闸失效的直接信号,别等人肉挖(2026-07-22 教训)。
        if overspeed:
            top = overspeed[0]
            overspeed_cell = (
                f"<b class='bad'>{len(overspeed)}人</b>"
                f"<div class='muted' style='font-size:11px'>最凶 "
                f"{html.escape(str(top.get('user_id') or '')[:16])}… "
                f"{int(top.get('heartbeats') or 0)}/{int(top.get('cap') or 0)}上限</div>"
            )
        else:
            overspeed_cell = "<span class='muted'>—</span>"
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('day') or ''))}</td>"
            f"<td>{int(row.get('jobs') or 0)}</td>"
            f"<td>{int(row.get('delivered') or 0)}</td>"
            f"<td>{int(row.get('completed') or 0)}</td>"
            f"<td>{int(row.get('failed') or 0)}</td>"
            f"<td>{int(row.get('skipped') or 0)}</td>"
            f"<td>{int(row.get('pending') or 0)}</td>"
            + sr_cell +
            f"<td>{int(row.get('maintenance') or 0)}(f{int(row.get('maintenance_failed') or 0)})</td>"
            f"<td>{int(row.get('heartbeat') or 0)}</td>"
            f"<td>{int(row.get('heartbeat_throttled') or 0)}</td>"
            f"<td>{int(row.get('v2_heartbeat') or 0)}"
            + (f"<span class='bad'>(f{int(row.get('v2_heartbeat_failed') or 0)})</span>"
               if int(row.get('v2_heartbeat_failed') or 0) else "")
            + "</td>"
            f"<td>{int(row.get('screen') or 0)}</td>"
            f"<td>{overspeed_cell}</td>"
            "</tr>"
        )
    # 全量 kind 分桶矩阵(最近 14 天):行=kind,列=天。哪个唤醒源在异常一眼可见;
    # 新 kind 自动出现,不需要改代码。
    kind_days = [str(r.get("day") or "") for r in rows[:14]]
    kind_totals: dict[str, int] = {}
    for r in rows[:14]:
        for k, n in (r.get("kinds") or {}).items():
            kind_totals[k] = kind_totals.get(k, 0) + int(n)
    kind_rows_html = []
    for kind in sorted(kind_totals, key=lambda k: -kind_totals[k]):
        cells = "".join(
            f"<td>{int((r.get('kinds') or {}).get(kind) or 0) or ''}</td>"
            for r in rows[:14]
        )
        kind_rows_html.append(
            f"<tr><td><b>{html.escape(kind)}</b></td>"
            f"<td>{kind_totals[kind]}</td>{cells}</tr>"
        )
    kinds_table = (
        "<h2>唤醒源分桶(全量 kind × 最近 14 天)</h2>"
        "<div class='muted'>kind = job_kind → wake_kind → trigger 回退;现网出现过什么就列什么,"
        "新唤醒源自动出现。某行突然膨胀 = 那个源在超发。</div>"
        "<table><thead><tr><th>kind</th><th>Σ14天</th>"
        + "".join(f"<th>{html.escape(d[5:])}</th>" for d in kind_days)
        + "</tr></thead><tbody>"
        + ("".join(kind_rows_html) or "<tr><td colspan='16' class='muted'>无数据。</td></tr>")
        + "</tbody></table>"
    ) if rows else ""
    # 成功率口径只覆盖 V1 legacy wake lane;分母为 0(V2-only 窗口/空窗口)时
    # 显示 N/A 而非假 0%。V2 心跳单独一格(它有自己的 completed/failed 口径)。
    overall_sr = (
        f"{summary['overall_success_rate']*100:.0f}%"
        if summary.get("overall_has_legacy", True) else "N/A"
    )
    latest_sr = (
        f"{summary['latest_success_rate']*100:.0f}%"
        if summary.get("latest_has_legacy", True) else "N/A"
    )
    metrics = "".join([
        _render_metric("整体成功率 (V1 wake 投递+完成/已结)", overall_sr),
        _render_metric("最近一天成功率 (V1)", latest_sr),
        _render_metric("最近一天", summary.get("latest_day") or "n/a"),
        _render_metric("总 jobs (V1)", summary["total_jobs"]),
        _render_metric("投递+完成 / 失败 (V1)", f"{summary['total_delivered']}+{summary.get('total_completed', 0)} / {summary['total_failed']}"),
        _render_metric("维护 / 失败", f"{summary.get('total_maintenance', 0)} / {summary.get('total_maintenance_failed', 0)}"),
        _render_metric("V2 心跳 / 失败+过期", f"{summary.get('total_v2_heartbeat', 0)} / {summary.get('total_v2_heartbeat_failed', 0)}"),
    ])
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedling Proactive 日报 · Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:1280px; margin:0 auto; padding:28px 24px 48px; }}
    h1 {{ font-size:26px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 12px; }}
    .muted {{ color:var(--muted); }} .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:22px 0; }}
    .metric {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .metric-value {{ font-size:24px; font-weight:700; }}
    .metric-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
    .fn-bar {{ height:6px; background:#eee3d9; border-radius:4px; margin:5px 0 0; overflow:hidden; width:120px; }}
    .fn-bar span {{ display:block; height:100%; background:var(--accent); }}
    .fn-bar span.ok {{ background:var(--ok); }} .fn-bar span.warn {{ background:var(--warn); }} .fn-bar span.bad {{ background:var(--bad); }}
    .viewbar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:14px 0 18px; }}
    .toolbar {{ display:flex; gap:8px; margin:14px 0; }}
    .sort-button {{ display:inline-flex; align-items:center; border:1px solid var(--line); border-radius:6px; padding:7px 10px; background:var(--card); color:var(--fg); font-size:13px; }}
    .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#fff1ed; }}
    table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
    th {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; background:#f4ece5; }}
    tr:last-child td {{ border-bottom:0; }} a {{ color:var(--accent); text-decoration:none; }}
  </style>
</head>
<body>
<main>
  <h1>Feedling Proactive 日报</h1>
  <div class="muted">Generated {html.escape(_bj_iso(summary["generated_at"]))}. 时区 {html.escape(summary["timezone"])}. 最近 {html.escape(str(summary["days_returned"]))} 天。</div>
  {_render_data_track_view_nav("proactive")}
  <section class="metrics">{metrics}</section>
  <h2>每日主动消息成功率(仅 wake lane)</h2>
  <div class="muted">{html.escape(definition.get("success_rate") or "")} {html.escape(definition.get("lanes") or "")}</div>
  <div class="muted">限频拦截 = 服务端心跳闸(reason=heartbeat_throttled)拦下的 tick,闸上线前恒 0;V2心跳 = Runtime V2 pooled worker 的心跳(agent_jobs lane=heartbeat,被 gate 拦的不落行,故全为放行量;(fN)=失败+过期);超速用户 = 当天心跳 job 数(V1+V2 两源合计)超过其 wake_interval 物理上限(86400/interval+1,默认 2h → 12/天)的用户——<b>出现任何一个都说明频率闸失效,立刻查,别等</b>。</div>
  <div class="toolbar"><a class="sort-button" href="{html.escape(api_url, quote=True)}">JSON</a></div>
  <table>
    <thead><tr><th>北京日</th><th>Jobs</th><th>投递</th><th>完成</th><th>失败</th><th>Skipped</th><th>Pending</th><th>成功率</th><th>维护(失败)</th><th>心跳</th><th>限频拦截</th><th>V2心跳</th><th>屏幕</th><th>超速用户</th></tr></thead>
    <tbody>{''.join(rows_html) if rows_html else "<tr><td colspan='14' class='muted'>此区间无 proactive job。</td></tr>"}</tbody>
  </table>
  {kinds_table}
</main>
</body>
</html>"""


def _debug_json(obj) -> str:
    if not obj:
        return ""
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _debug_ms(ms) -> str:
    try:
        value = float(ms or 0)
    except (TypeError, ValueError):
        return ""
    return f"{value:.0f}ms" if value else ""


def _debug_time(ts) -> str:
    # Beijing time (UTC+8) for display; storage stays UTC. Include the date so a
    # firehose event can be placed on a day, not just an hour.
    try:
        value = float(ts or 0)
    except (TypeError, ValueError):
        value = 0
    if not value:
        return "—"
    try:
        return datetime.fromtimestamp(value, _SHANGHAI_TZ).strftime("%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return "—"


# type → (icon, 友好中文步骤名). Makes a turn read as a narrative instead of
# raw event-type strings. Unknown types fall back to a subsystem-based label.
_DEBUG_STEP_LABELS = {
    "route.decided": ("🧭", "路由决策"),
    "context.build": ("📎", "组装上下文"),
    "agent.model.call.start": ("🧠", "调用模型 · 开始"),
    "agent.model.call.done": ("🧠", "调用模型 · 完成"),
    "agent.tool.call": ("🔧", "调用工具"),
    "agent.reasoning": ("💭", "思考 / reasoning"),
    "agent.reply": ("💬", "AI 回复"),
    "chat.response": ("📤", "写入回复"),
    "chat.poll.delivered": ("✅", "投递到客户端"),
    "enclave.call.start": ("🔐", "飞地调用 · 开始"),
    "enclave.call.done": ("🔐", "飞地调用 · 完成"),
    "memory.capture.queued": ("🧩", "记忆抓取 · 入队"),
    "memory.capture.done": ("🧩", "记忆抓取 · 完成"),
}
_DEBUG_SUBSYSTEM_FALLBACK = {
    "route": ("🧭", "路由"), "context": ("📎", "上下文"), "agent": ("🤖", "Agent"),
    "memory": ("🧩", "记忆"), "genesis": ("🌱", "入驻蒸馏"), "enclave": ("🔐", "飞地"),
    "chat": ("💬", "聊天"), "debug_trace": ("🔎", "trace"),
}


def _debug_friendly_step(ev: dict) -> tuple[str, str]:
    typ = str(ev.get("type") or "")
    # tool-call carries the tool name in detail.tool → surface it (before the map,
    # which only has the generic "调用工具" label).
    if typ == "agent.tool.call":
        tool = str((ev.get("detail") or {}).get("tool") or "")
        return ("🔧", f"调用工具 · {tool}" if tool else "调用工具")
    if typ in _DEBUG_STEP_LABELS:
        return _DEBUG_STEP_LABELS[typ]
    icon, base = _DEBUG_SUBSYSTEM_FALLBACK.get(str(ev.get("subsystem") or ""), ("•", ""))
    # for an unknown type, show the last dotted segment as a hint, e.g. foo.bar.baz→baz
    tail = typ.split(".")[-1] if typ else ""
    label = f"{base} · {tail}" if base and tail else (base or tail or "事件")
    return (icon, label)


def _render_data_track_debug_page(payload: dict) -> str:
    summary = payload["summary"]
    filters = payload.get("filters", {})
    options = payload.get("options", {})
    users = payload.get("users", [])
    turns = payload.get("turns", [])
    events = payload.get("events", [])
    pagination = payload.get("pagination", {})
    mode = str(filters.get("mode") or "flat")
    reveal_key = str(filters.get("reveal") or "")

    def input_value(name: str) -> str:
        return html.escape(str(filters.get(name) or ""), quote=True)

    def is_selected(name: str, value: str) -> str:
        return "selected" if str(filters.get(name) or "") == value else ""

    def mode_href(next_mode: str) -> str:
        return _data_track_page_href(view="debug", mode=next_mode, offset=0, reveal=None)

    def hint(text: str) -> str:
        return f"<span class='hint' title='{html.escape(text, quote=True)}'>?</span>"

    def module_badge(value: str) -> str:
        label = html.escape(value or "unknown")
        return f"<span class='module module-{html.escape((value or 'unknown').replace('.', '-'))}'>{label}</span>"

    def copy_button(label: str, value) -> str:
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        return (
            f"<button type='button' class='mini-button' data-copy='{html.escape(raw, quote=True)}'>"
            f"{html.escape(label)}</button>"
        )

    def hidden_inputs(qs: str) -> str:
        parts = []
        for key, values in parse_qs(qs, keep_blank_values=False).items():
            for value in values:
                parts.append(
                    f'<input type="hidden" name="{html.escape(key, quote=True)}" '
                    f'value="{html.escape(value, quote=True)}">'
                )
        return "".join(parts)

    def event_anchor(ev: dict) -> str:
        return f"event-{_debug_event_key(ev)}"

    def turn_anchor(value: str) -> str:
        return f"turn-{hashlib.sha1(str(value or 'ungrouped').encode('utf-8')).hexdigest()[:12]}"

    def reveal_href(ev: dict) -> str:
        href = _data_track_page_href(
            view="debug",
            mode=mode,
            user_id=ev.get("user_id") or "",
            trace_id=ev.get("trace_id") or "",
            offset=0,
            reveal=_debug_event_key(ev),
        )
        return f"{href}#{event_anchor(ev)}"

    def event_actions(ev: dict, *, include_open_turn: bool) -> str:
        buttons = [
            copy_button("copy user", ev.get("user_id") or ""),
            copy_button("copy trace", ev.get("trace_id") or ""),
            copy_button("copy type", ev.get("type") or ""),
            copy_button("copy JSON", _debug_event_public_json(ev)),
        ]
        if include_open_turn:
            href = _data_track_page_href(view="debug", mode="timeline", user_id=ev.get("user_id") or "", trace_id=ev.get("trace_id") or "", offset=0, reveal=None)
            href = f"{href}#{turn_anchor(str(ev.get('trace_id') or 'ungrouped'))}"
            buttons.append(f"<a class='mini-button' href='{html.escape(href, quote=True)}'>open turn</a>")
        buttons.append(
            f"<a class='mini-button reveal-button' data-reveal='1' href='{html.escape(reveal_href(ev), quote=True)}'>Reveal plaintext</a>"
        )
        return "<div class='actions'>" + "".join(buttons) + "</div>"

    def event_detail_block(ev: dict) -> str:
        revealed = bool(reveal_key and reveal_key == _debug_event_key(ev))
        detail = _debug_json(ev.get("detail") if revealed else _debug_redact_value(ev.get("detail") or {}))
        excerpt = _debug_json(ev.get("content_excerpt") if revealed else _debug_content_summary(ev.get("content_excerpt") or {}))
        if not detail and not excerpt and not revealed:
            return ""
        label = "plaintext detail" if revealed else "redacted detail"
        warning = (
            "<div class='reveal-note'>Plaintext revealed for this event only. Avoid screenshots or sharing user content.</div>"
            if revealed else
            "<div class='redacted-note'>Plaintext is not rendered by default. Use Reveal plaintext for a single event when debugging needs it.</div>"
        )
        return (
            f"<details class='event-detail' {'open' if revealed else ''}><summary>{label}</summary>{warning}"
            f"{'<h4>detail</h4><pre>' + html.escape(detail) + '</pre>' if detail else ''}"
            f"{'<h4>content_excerpt</h4><pre>' + html.escape(excerpt) + '</pre>' if excerpt else ''}"
            "</details>"
        )

    user_rows = []
    for row in users:
        href = _data_track_page_href(view="debug", user_id=row["user_id"], offset=0, reveal=None)
        enabled_cls = "ok" if row.get("enabled") else "muted"
        user_rows.append(
            "<tr>"
            f"<td><a href='{html.escape(href, quote=True)}'>{html.escape(row['user_id'])}</a>"
            f"<br><span class='muted'>{html.escape(str(row.get('principal_id') or ''))}</span></td>"
            f"<td>{int(row.get('events') or 0)}</td>"
            f"<td><span class='pill {enabled_cls}'>{'enabled' if row.get('enabled') else 'off'}</span></td>"
            f"<td>{html.escape(_bj_iso(row.get('last_at')))}</td>"
            "</tr>"
        )

    flat_rows = []
    for ev in events:
        ev_status = str(ev.get("status") or "ok").lower()
        ev_cls = "bad" if ev_status in {"error", "failed"} else ("warn" if ev_status == "blocked" else "ok")
        trace_href = _data_track_page_href(view="debug", mode="timeline", user_id=ev.get("user_id") or "", trace_id=ev.get("trace_id") or "", offset=0, reveal=None)
        trace_href = f"{trace_href}#{turn_anchor(str(ev.get('trace_id') or 'ungrouped'))}"
        user_href = _data_track_page_href(view="debug", mode=mode, user_id=ev.get("user_id") or "", offset=0, reveal=None)
        flat_rows.append(
            f"<tr id='{html.escape(event_anchor(ev), quote=True)}'>"
            f"<td><span class='mono'>{html.escape(_debug_time(ev.get('ts')))}</span></td>"
            f"<td><a class='mono' href='{html.escape(user_href, quote=True)}'>{html.escape(str(ev.get('user_id') or ''))}</a></td>"
            f"<td><a class='mono trace-link' href='{html.escape(trace_href, quote=True)}'>{html.escape(str(ev.get('trace_id') or 'ungrouped'))}</a></td>"
            f"<td>{module_badge(str(ev.get('subsystem') or ''))}</td>"
            f"<td><span class='mono'>{html.escape(str(ev.get('type') or ''))}</span></td>"
            f"<td><span class='pill {ev_cls}'>{html.escape(ev_status)}</span></td>"
            f"<td>{html.escape(_debug_ms(ev.get('dur_ms')))}</td>"
            f"<td>{html.escape(str(ev.get('explain') or ev.get('summary') or ''))}{event_actions(ev, include_open_turn=True)}{event_detail_block(ev)}</td>"
            "</tr>"
        )

    turn_cards = []
    for turn in turns:
        status = str(turn.get("terminal_status") or "ok")
        mark = "⏳" if status == "stalled" else ("✗" if status == "error" else ("!" if status == "blocked" else "✓"))
        status_cls = "warn" if status in {"stalled", "blocked"} else ("bad" if status == "error" else "ok")
        rows_html = []
        for ev in turn.get("rows") or []:
            ev_status = str(ev.get("status") or "")
            ev_cls = "bad" if ev_status in {"error", "failed"} else ("warn" if ev_status == "blocked" else "ok")
            icon, step_label = _debug_friendly_step(ev)
            rows_html.append(
                f"<div class='event-row' id='{html.escape(event_anchor(ev), quote=True)}'>"
                f"<div class='step-head'><span class='step-icon'>{icon}</span>"
                f"<span class='step-label'>{html.escape(step_label)}</span>"
                f"<span class='pill {ev_cls}'>{html.escape(ev_status or 'ok')}</span>"
                f"<span class='muted mono step-time'>{html.escape(_debug_time(ev.get('ts')))}</span>"
                f"<span class='muted'>{html.escape(_debug_ms(ev.get('dur_ms')))}</span>"
                f"<span class='muted mono step-rawtype'>{html.escape(str(ev.get('type') or ''))}</span></div>"
                f"<div class='step-explain'>{html.escape(str(ev.get('explain') or ev.get('summary') or ''))}</div>"
                f"{event_actions(ev, include_open_turn=False)}"
                f"{event_detail_block(ev)}"
                "</div>"
            )
        stalled_note = (
            "<div class='stall'>⏳ 有步骤只 start 没 done/error，可能卡在模型返回或后续写回。</div>"
            if turn.get("is_stalled") else ""
        )
        turn_cards.append(
            f"<section class='turn' id='{html.escape(turn_anchor(str(turn.get('trace_id') or 'ungrouped')), quote=True)}'>"
            f"<h3><span>{mark}</span> <span>{html.escape(str(turn.get('title') or ''))}</span>"
            f"<span class='pill {status_cls}'>{html.escape(status)}</span>"
            f"<span class='muted mono'>{html.escape(str(turn.get('user_id') or ''))} · {html.escape(str(turn.get('trace_id') or ''))}</span>"
            f"<span class='muted'>{html.escape(_debug_ms(turn.get('total_dur_ms')))}</span></h3>"
            f"{''.join(rows_html)}{stalled_note}"
            "</section>"
        )

    subsystem_options = ['<option value="">all modules</option>']
    for subsystem in options.get("subsystems") or ["route", "context", "agent", "memory", "genesis", "debug_trace"]:
        subsystem_options.append(
            f'<option value="{html.escape(subsystem, quote=True)}" {is_selected("subsystem", subsystem)}>{html.escape(subsystem)}</option>'
        )
    limit_options = []
    current_limit = str(pagination.get("limit") or filters.get("limit") or 100)
    for value in ("50", "100", "200", "500"):
        selected = "selected" if current_limit == value else ""
        limit_options.append(f'<option value="{value}" {selected}>{value}</option>')

    metrics = "".join([
        _render_metric("users with events", summary["users_with_events"]),
        _render_metric("events", summary["events_total"]),
        _render_metric("turns", summary["turns_total"]),
        _render_metric("stalled / error", f"{summary['stalled_turns']} / {summary['error_turns']}"),
    ])
    refresh_meta = "" if reveal_key else '<meta http-equiv="refresh" content="30">'

    page_unit = "turns" if mode == "timeline" else "events"
    total = int(pagination.get("total") or 0)
    returned = int(pagination.get("returned") or 0)
    offset = int(pagination.get("offset") or 0)
    page_size = int(pagination.get("limit") or filters.get("limit") or 100)
    current_page = int(pagination.get("current_page") or 1)
    total_pages = max(1, int(pagination.get("total_pages") or 1))
    start = offset + 1 if total and returned else 0
    end = min(offset + returned, total) if total and returned else 0
    pager_links = []
    prev_offset = pagination.get("prev_offset")
    next_offset = pagination.get("next_offset")
    if total_pages > 1:
        pager_links.append(
            f"<a class='sort-button' href='{html.escape(_data_track_page_href(view='debug', mode=mode, offset=0, page=None, reveal=None), quote=True)}'>First</a>"
        )
        if prev_offset is not None:
            pager_links.append(
                f"<a class='sort-button' href='{html.escape(_data_track_page_href(view='debug', mode=mode, offset=prev_offset, page=None, reveal=None), quote=True)}'>Prev</a>"
            )
        else:
            pager_links.append("<span class='sort-button disabled'>Prev</span>")
        if next_offset is not None:
            pager_links.append(
                f"<a class='sort-button' href='{html.escape(_data_track_page_href(view='debug', mode=mode, offset=next_offset, page=None, reveal=None), quote=True)}'>Next</a>"
            )
        else:
            pager_links.append("<span class='sort-button disabled'>Next</span>")
        last_offset = max(0, (total_pages - 1) * page_size)
        pager_links.append(
            f"<a class='sort-button' href='{html.escape(_data_track_page_href(view='debug', mode=mode, offset=last_offset, page=None, reveal=None), quote=True)}'>Last</a>"
        )
    page_form_qs = _data_track_qs(view="debug", mode=mode, offset=None, page=None, reveal=None)
    pager_html = (
        "<div class='pager'>"
        f"<span class='muted'>Showing {start}-{end} of {total} {page_unit}. "
        f"Page size {html.escape(str(page_size))}.</span>"
        f"<form class='page-jump' method='get' action='/admin/data-track'>"
        f"{hidden_inputs(page_form_qs)}"
        f"<span>Page</span><input name='page' value='{html.escape(str(current_page), quote=True)}' inputmode='numeric'>"
        f"<span>/ {html.escape(str(total_pages))}</span><button type='submit'>Go</button></form>"
        f"<div class='pager-links'>{''.join(pager_links)}</div>"
        "</div>"
    )

    flat_table = (
        "<table class='log-table'>"
        "<thead><tr>"
        f"<th>Time {hint('事件发生时间，北京时间(UTC+8)，格式 月-日 时:分:秒。列表默认最新在最上方。')}</th>"
        f"<th>User {hint('真实 user_id。点它会只看这个用户的 debug 事件。')}</th>"
        f"<th>Trace {hint('一次聊天或一次任务链路的 trace_id。点它会切到 timeline 看完整步骤。')}</th>"
        f"<th>Module {hint('事件来源模块：route 是入口，context 是上下文，agent 是模型调用/回复，memory 是记忆写入。')}</th>"
        f"<th>Type {hint('具体事件类型，比如 agent.model.call.start/done 或 agent.reply。')}</th>"
        f"<th>Status {hint('单条事件自己的状态；turn 顶部的 stalled/error 是整轮链路诊断结果。')}</th>"
        f"<th>Cost {hint('这一步上报的耗时。不是每个事件都有。')}</th>"
        f"<th>Explain / detail {hint('explain 是人读摘要；展开 detail / 明文 可以看 prompt、reply、tokens 等 excerpt。')}</th>"
        "</tr></thead>"
        "<tbody>"
        + ("".join(flat_rows) if flat_rows else "<tr><td colspan='8' class='muted'>No events match the current filter.</td></tr>")
        + "</tbody>"
        "</table>"
    )

    timeline_view = (
        "".join(turn_cards) if turn_cards else "<div class='muted'>No turns match the current filter.</div>"
    )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>Feedling Debug · Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; --ink:#263238; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:1280px; margin:0 auto; padding:28px 24px 48px; }}
    h1 {{ font-size:26px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 12px; }} h3 {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; font-size:15px; margin:0 0 10px; }}
    a {{ color:var(--accent); text-decoration:none; }} .muted {{ color:var(--muted); }} .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:22px 0; }}
    .metric,.turn {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .metric-value {{ font-size:24px; font-weight:700; }} .metric-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
    .modebar,.viewbar,.toolbar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:14px 0 18px; }}
    .modebar {{ justify-content:space-between; background:#f4ece5; border:1px solid var(--line); border-radius:8px; padding:8px; }}
    .mode-left {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
    .pager {{ display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:10px; margin:12px 0; }}
    .pager-links {{ display:flex; flex-wrap:wrap; gap:8px; }}
    .page-jump {{ display:flex; align-items:center; gap:6px; margin:0; color:var(--muted); }} .page-jump input {{ width:58px; padding:6px 7px; }}
    .sort-button.disabled {{ color:#b5aaa2; background:#f6efe8; pointer-events:none; }}
    .sort-button,button {{ display:inline-flex; align-items:center; border:1px solid var(--line); border-radius:6px; padding:7px 10px; background:var(--card); color:var(--fg); font-size:13px; }}
    .mini-button {{ display:inline-flex; align-items:center; border:1px solid var(--line); border-radius:5px; padding:3px 7px; background:#fffdfa; color:var(--fg); font-size:12px; margin:4px 5px 0 0; cursor:pointer; }}
    .reveal-button {{ border-color:#d9b28c; color:#8a4a00; background:#fff8ed; }}
    .actions {{ display:flex; flex-wrap:wrap; gap:0; margin-top:5px; }}
    .event-row {{ border-left:3px solid var(--line); padding:8px 0 8px 12px; margin:2px 0; }}
    .step-head {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
    .step-icon {{ font-size:15px; }}
    .step-label {{ font-weight:600; font-size:14px; }}
    .step-time {{ font-size:12px; }} .step-rawtype {{ font-size:11px; color:#b5aaa2; }}
    .step-explain {{ color:var(--ink); margin:3px 0 0; font-size:13px; }}
    .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#fff1ed; }}
    input,select {{ border:1px solid var(--line); border-radius:6px; padding:8px 9px; background:white; color:var(--fg); }}
    .field {{ display:flex; flex-direction:column; gap:4px; min-width:150px; }} .field label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
    table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }} th {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; background:#f4ece5; }}
    .pill {{ display:inline-flex; border-radius:999px; padding:2px 8px; font-size:12px; background:#efe7df; color:var(--muted); }} .pill.ok,.ok {{ color:var(--ok); background:#e7f3ed; }} .pill.warn,.warn {{ color:var(--warn); background:#fff1db; }} .pill.bad,.bad {{ color:var(--bad); background:#fff1ed; }}
    .hint {{ display:inline-flex; align-items:center; justify-content:center; width:16px; height:16px; border-radius:50%; margin-left:4px; background:#eadfd4; color:var(--muted); font-size:11px; font-weight:700; cursor:help; text-transform:none; letter-spacing:0; }}
    .module {{ display:inline-flex; border-radius:5px; padding:2px 7px; font-size:12px; background:#edf0ef; color:var(--ink); }} .module-route {{ background:#e8f1ff; color:#24538a; }} .module-context {{ background:#edf7e8; color:#3b6b2d; }} .module-agent {{ background:#fff0e3; color:#9a4d00; }} .module-memory {{ background:#f0eaff; color:#5b3a91; }} .module-genesis {{ background:#e8f6f4; color:#21625a; }} .module-debug_trace {{ background:#eef0f2; color:#52616b; }}
    .log-table td:nth-child(8) {{ min-width:280px; }} .trace-link {{ color:#6b3fb0; }}
    .turn {{ margin:10px 0; }} .event-row {{ border-top:1px solid var(--line); padding:9px 0; }} .event-row:first-of-type {{ border-top:0; }}
    .event-meta {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:4px; }} .event-detail summary {{ color:var(--accent); cursor:pointer; margin-top:5px; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:#fff7f0; border:1px solid var(--line); border-radius:6px; padding:10px; max-height:360px; overflow:auto; }}
    .redacted-note,.reveal-note {{ border-radius:6px; padding:8px; margin:7px 0; }} .redacted-note {{ color:var(--muted); background:#f6efe8; border:1px solid var(--line); }} .reveal-note {{ color:#8a4a00; background:#fff8ed; border:1px solid #e8c59d; }}
    .stall {{ color:var(--warn); background:#fff8e8; border:1px solid #f0d7a5; border-radius:6px; padding:8px; margin-top:8px; }}
  </style>
</head>
<body>
<main>
  <h1>Feedling Debug Logs</h1>
  <div class="muted">Admin-only beta debug view. Reads existing per-user v1_flow_trace ring buffers; no instrumentation writes here. Generated {html.escape(_bj_iso(summary["generated_at"]))}.</div>
  {_render_data_track_view_nav("debug")}
  <section class="metrics">{metrics}</section>
  <div class="modebar">
    <div class="mode-left">
      <a class="sort-button {'active' if mode == 'flat' else ''}" href="{html.escape(mode_href('flat'), quote=True)}">Flat logs</a>
      <a class="sort-button {'active' if mode == 'timeline' else ''}" href="{html.escape(mode_href('timeline'), quote=True)}">Timeline</a>
      <span class="muted">Flat 用来扫全局异常；Timeline 用来看一次聊天怎么走完。</span>
    </div>
    <span class="muted">{hint('页面每 30 秒自动刷新。所有数据来自已有 v1_flow_trace ring buffer，不会写新埋点。')} auto refresh</span>
  </div>
  <h2>Filter debug logs {hint('先按 user 或 status 收窄，再用 q 搜 prompt/reply/tokens/explain；点 trace 可进入单轮链路。')}</h2>
  <form class="toolbar" method="get" action="/admin/data-track">
    <input type="hidden" name="view" value="debug">
    <input type="hidden" name="mode" value="{html.escape(mode, quote=True)}">
    <input name="admin_key" type="hidden" value="{html.escape(request.args.get('admin_key', ''), quote=True)}">
    <div class="field"><label>User {hint('留空表示扫所有用户；填 user_id 时可查任意真实用户。')}</label><input name="user_id" placeholder="usr_..." value="{input_value('user_id')}"></div>
    <div class="field"><label>Trace {hint('一次聊天/任务的链路 id。已知 trace_id 时可直接定位。')}</label><input name="trace_id" placeholder="trace_id" value="{input_value('trace_id')}"></div>
    <div class="field"><label>Module {hint('按模块收窄：route=入口，context=上下文，agent=模型/回复，memory=记忆，genesis=蒸馏/初始化。')}</label><select name="subsystem">{''.join(subsystem_options)}</select></div>
    <div class="field"><label>Page size {hint('每页渲染多少条。全局 debug 默认分页，避免一次打开全量日志。')}</label><select name="limit">{''.join(limit_options)}</select></div>
    <div class="field"><label>Status {hint('这里按整轮 turn 状态筛选：stalled 表示 start 后没有 done/error。')}</label><select name="status">
      <option value="" {is_selected("status", "")}>all status</option>
      <option value="ok" {is_selected("status", "ok")}>ok</option>
      <option value="error" {is_selected("status", "error")}>error</option>
      <option value="blocked" {is_selected("status", "blocked")}>blocked</option>
      <option value="stalled" {is_selected("status", "stalled")}>stalled</option>
    </select></div>
    <div class="field"><label>Since {hint('支持 ISO 时间或 epoch；空着就是 ring buffer 里全部。')}</label><input name="since" placeholder="2026-07-04T00:00:00" value="{input_value('since')}"></div>
    <div class="field"><label>Search {hint('会搜 type/explain/detail/content_excerpt，能查明文 excerpt。')}</label><input name="q" placeholder="prompt / reply / token / error" value="{input_value('q')}"></div>
    <button type="submit">Search</button>
  </form>
  <h2>Users with debug events {hint('当前筛选条件下，有 debug 事件或开启 trace 的用户。')}</h2>
  <table>
    <thead><tr><th>User</th><th>Events</th><th>Trace</th><th>Last event</th></tr></thead>
    <tbody>{''.join(user_rows) if user_rows else "<tr><td colspan='4' class='muted'>No debug events in the current filter.</td></tr>"}</tbody>
  </table>
  <h2>{'Flat logs' if mode == 'flat' else 'Turns'} {hint('Flat 是时间倒序事件流；Timeline 是按 trace_id 分组后的完整链路。')}</h2>
  {pager_html}
  {flat_table if mode == 'flat' else timeline_view}
  {pager_html}
</main>
<script>
  document.addEventListener('click', async (event) => {{
    const copyTarget = event.target.closest('[data-copy]');
    if (copyTarget) {{
      event.preventDefault();
      const value = copyTarget.getAttribute('data-copy') || '';
      try {{
        await navigator.clipboard.writeText(value);
        const oldText = copyTarget.textContent;
        copyTarget.textContent = 'copied';
        setTimeout(() => {{ copyTarget.textContent = oldText; }}, 900);
      }} catch (err) {{
        window.prompt('Copy value', value);
      }}
      return;
    }}
    const revealTarget = event.target.closest('[data-reveal]');
    if (revealTarget) {{
      const ok = window.confirm('Reveal plaintext for this single debug event? This may expose private user content.');
      if (!ok) event.preventDefault();
    }}
  }});
</script>
</body>
</html>"""


def _events_route_bucket(route) -> str:
    """VPS/API split: model_api → API; resident/official_import/other → VPS."""
    return "api" if str(route or "") == "model_api" else "vps"


# Ordered category catalog for the event-health board.
_EVENT_CATEGORIES = [
    ("onboarding", "Onboarding（漏斗）"),
    ("distill_first", "一次蒸馏"),
    ("distill_second", "二次蒸馏"),
    ("memory_org", "主动记忆整理"),
    ("reply", "回复消息"),
    ("heartbeat", "心跳"),
    ("trigger", "主动触发"),
    ("screen", "屏幕共享"),
    ("other", "其他"),
]

# Plain-language: what each row actually is. Shown under every event label so the
# page is self-explanatory (no tribal knowledge needed to read it).
_EVENT_DESCRIPTIONS = {
    "onboarding": "从注册 → 配置上线 → 内容就绪 → 第一次收到真回复的转化漏斗。点开看各段转化率+耗时。",
    "distill_first": "首次蒸馏：onboarding 时 AI 把上传材料/初始对话提炼成身份卡+记忆（genesis job，mode=onboarding）。VPS 和 API 都有。",
    "distill_second": "增量蒸馏：onboarding 之后 AI 追加记忆或改写身份卡（genesis 的 add_memory / update_identity，含用户从 IO 手动改身份卡）。",
    "memory_org": "AI 主动整理/沉淀记忆的 capture 任务。",
    "reply": "用户发消息后 AI 是否真的回上了。成功率=真回复÷用户消息数；下面一行=兜底回复占比·回复延迟中位。",
    "heartbeat": "陪伴心跳：空闲时按间隔唤醒 AI，判断要不要主动说话。",
    "trigger": "事件触发的主动：到达/解锁/照片等场景唤醒 AI。",
    "screen": "屏幕观察类的主动任务。",
    "other": "其他/未归类的主动任务。",
}


def _data_track_events_payload() -> dict:
    raw = db.admin_events_overview()

    def blank():
        return {"vps": _blank_evt_stat(), "api": _blank_evt_stat()}

    cats = {key: {"label": label, **blank()} for key, label in _EVENT_CATEGORIES}

    def add(key, route, total=0, success=0, failed=0, pending=0, median=None):
        b = cats[key][_events_route_bucket(route)]
        b["total"] += int(total or 0)
        b["success"] += int(success or 0)
        b["failed"] += int(failed or 0)
        b["pending"] += int(pending or 0)
        if median is not None:
            b["median_dur"] = median  # db already medians per (route, category)

    lane_key = {"heartbeat": "heartbeat", "trigger": "trigger", "screen": "screen", "other": "other"}
    for row in raw.get("proactive", []):
        add(lane_key.get(row.get("lane"), "other"), row.get("route"),
            row.get("total"), row.get("success"), row.get("failed"), row.get("pending"), row.get("median_dur"))
    for row in raw.get("capture", []):
        add("memory_org", row.get("route"), row.get("total"), row.get("success"), row.get("failed"), 0, row.get("median_dur"))
    for row in raw.get("genesis", []):
        key = "distill_first" if row.get("distill") == "first" else "distill_second"
        add(key, row.get("route"), row.get("total"), row.get("success"), row.get("failed"))
    # reply is special: success = real replies / user messages; fallback rate tracked apart
    reply = {"vps": {"user_msgs": 0, "real_replies": 0, "fallback_replies": 0, "median_latency": None},
             "api": {"user_msgs": 0, "real_replies": 0, "fallback_replies": 0, "median_latency": None}}
    for row in raw.get("reply", []):
        b = reply[_events_route_bucket(row.get("route"))]
        b["user_msgs"] += int(row.get("user_msgs") or 0)
        b["real_replies"] += int(row.get("real_replies") or 0)
        b["fallback_replies"] += int(row.get("fallback_replies") or 0)
        if row.get("median_latency") is not None:
            b["median_latency"] = row["median_latency"]  # one row per route from db
    for bucket in ("vps", "api"):
        rb = reply[bucket]
        cats["reply"][bucket].update({
            "total": rb["user_msgs"], "success": rb["real_replies"],
            "failed": max(0, rb["user_msgs"] - rb["real_replies"]),
            "fallback": rb["fallback_replies"],
            "fallback_base": rb["real_replies"] + rb["fallback_replies"],
            "median_latency": rb["median_latency"],
        })
    return {
        "generated_at": datetime.now().isoformat(),
        "categories": [{"key": k, **cats[k]} for k, _ in _EVENT_CATEGORIES],
        "note": "Onboarding 漏斗 + 回复延迟为下一阶段；本页先给 成功率/次数/中位耗时(job类)/兜底率，VPS·API 分列。",
    }


def _blank_evt_stat() -> dict:
    return {"total": 0, "success": 0, "failed": 0, "pending": 0, "median_dur": None}


def _evt_rate(b: dict) -> str:
    resolved = int(b.get("success") or 0) + int(b.get("failed") or 0)
    if resolved <= 0:
        return "—"
    return f"{(b['success'] / resolved) * 100:.0f}%"


def _evt_dur(b: dict) -> str:
    d = b.get("median_dur")
    if d is None:
        return "—"
    d = float(d)
    return f"{d:.1f}s" if d < 60 else f"{d/60:.1f}m"


def _render_events_page(payload: dict) -> str:
    cats = payload.get("categories", [])

    def cell(b: dict, *, is_reply: bool) -> str:
        total = int(b.get("total") or 0)
        if total <= 0:
            return "<td class='muted'>—</td>"
        rate = _evt_rate(b)
        rate_cls = ""
        try:
            rv = int(rate.rstrip("%"))
            rate_cls = "ok" if rv >= 80 else ("warn" if rv >= 50 else "bad")
        except ValueError:
            rate_cls = ""
        extra = ""
        if is_reply:
            fb = int(b.get("fallback") or 0)
            base = int(b.get("fallback_base") or 0)
            fb_pct = f"{(fb/base)*100:.0f}%" if base else "—"
            lat = _evt_dur({"median_dur": b.get("median_latency")})
            extra = f"<br><span class='muted'>兜底 {fb_pct} · 延迟 {lat}</span>"
        else:
            extra = f"<br><span class='muted'>中位耗时 {_evt_dur(b)}</span>"
        return (f"<td><b class='{rate_cls}'>{rate}</b> <span class='muted'>成功率 · {total} 次</span>{extra}</td>")

    def _desc(key: str) -> str:
        d = _EVENT_DESCRIPTIONS.get(key, "")
        return f"<div class='evt-desc'>{html.escape(d)}</div>" if d else ""

    rows = []
    for c in cats:
        is_reply = c["key"] == "reply"
        is_onb = c["key"] == "onboarding"
        if is_onb:
            onb_href = _data_track_page_href(view="events", event="onboarding", offset=0)
            rows.append(f"<tr><td><a href='{html.escape(onb_href, quote=True)}'>{html.escape(c['label'])}</a>{_desc(c['key'])}</td>"
                        f"<td colspan='2'><a href='{html.escape(onb_href, quote=True)}'>打开漏斗(VPS/API 转化率+耗时) →</a></td></tr>")
            continue
        drill = _data_track_page_href(view="events", event=c["key"], offset=0)
        rows.append(
            f"<tr><td><a href='{html.escape(drill, quote=True)}'>{html.escape(c['label'])}</a>{_desc(c['key'])}</td>"
            f"{cell(c['vps'], is_reply=is_reply)}{cell(c['api'], is_reply=is_reply)}</tr>"
        )
    body = "".join(rows)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feedling 事件健康度 · Data Track</title>
<style>
  :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  main {{ max-width:920px; margin:0 auto; padding:28px 24px 48px; }}
  h1 {{ font-size:24px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:24px 0 10px; }}
  .muted {{ color:var(--muted); }} .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
  .viewbar {{ display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 18px; }}
  .sort-button {{ display:inline-flex; border:1px solid var(--line); border-radius:6px; padding:7px 10px; background:var(--card); color:var(--fg); font-size:13px; }}
  .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#fff1ed; }}
  a {{ color:var(--accent); text-decoration:none; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
  th,td {{ text-align:left; padding:11px 14px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; background:#f4ece5; }}
  tr:last-child td {{ border-bottom:0; }} b {{ font-size:15px; }}
  .evt-desc {{ font-size:12px; color:var(--muted); line-height:1.5; margin-top:3px; max-width:520px; font-weight:400; text-transform:none; letter-spacing:0; }}
  .note-box {{ background:#fff8ef; border:1px solid #e8d8be; border-radius:8px; padding:12px 14px; margin:14px 0; font-size:13px; line-height:1.65; color:#5a4d3c; }}
</style></head><body><main>
  <h1>事件健康度</h1>
  <div class="muted">VPS=resident 自托管；API=model_api 托管。Generated {html.escape(_bj_iso(payload.get('generated_at')))}.</div>
  {_render_data_track_view_nav("events")}
  <div class="note-box">
    <b>每一格怎么读：</b>
    <b>成功率</b> = 完成 ÷（完成 + 失败）（回复类 = 真回复 ÷ 用户消息数，越高越健康）；
    <b>· N 次</b> = 这类事件/任务在统计窗口内的总条数（不是百分比！）；
    <b>中位耗时</b> = 任务从开始到结束的中位时长（一次/二次蒸馏暂未上报耗时，显示 —）；
    回复类第二行 = <b>兜底回复占比 · 回复延迟中位</b>。每行下方灰字说明这个事件到底是什么。
  </div>
  <table>
    <thead><tr><th>事件</th><th>VPS(自托管)</th><th>API(托管)</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</main></body></html>"""


_FUNNEL_MILESTONES = [("reg", "注册"), ("m1", "配置/上线"), ("m2", "内容就绪"), ("m3", "首次真回复")]
_FUNNEL_SEGMENTS = [("s1", "注册 → 配置/上线"), ("s2", "配置 → 内容就绪"), ("s3", "内容 → 首回复")]


def _funnel_median(vals: list):
    xs = sorted(v for v in vals if v is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _funnel_dur(sec) -> str:
    if sec is None:
        return "—"
    sec = float(sec)
    if sec < 90:
        return f"{sec:.0f}s"
    if sec < 5400:
        return f"{sec/60:.1f}m"
    if sec < 172800:
        return f"{sec/3600:.1f}h"
    return f"{sec/86400:.1f}d"


def _data_track_onboarding_funnel_payload() -> dict:
    def blank():
        return {"reg": 0, "m1": 0, "m2": 0, "m3": 0, "s1": [], "s2": [], "s3": [], "total": []}
    buckets = {"vps": blank(), "api": blank()}
    for r in db.admin_onboarding_funnel():
        t0, t1, t2, t3 = r.get("t0"), r.get("t1"), r.get("t2"), r.get("t3")
        if t0 is None:
            continue
        b = buckets["api" if str(r.get("route")) == "model_api" else "vps"]
        b["reg"] += 1
        if t1 is not None:
            b["m1"] += 1
        if t2 is not None:
            b["m2"] += 1
        if t3 is not None:
            b["m3"] += 1
        if t1 is not None and t1 >= t0:
            b["s1"].append(t1 - t0)
        if t1 is not None and t2 is not None and t2 >= t1:
            b["s2"].append(t2 - t1)
        if t2 is not None and t3 is not None and t3 >= t2:
            b["s3"].append(t3 - t2)
        if t3 is not None and t3 >= t0:
            b["total"].append(t3 - t0)

    def route_funnel(b):
        reg = max(1, b["reg"])
        return {
            "registered": b["reg"],
            "steps": [{"key": k, "label": lbl, "count": b[k], "pct": b[k] / reg} for k, lbl in _FUNNEL_MILESTONES],
            "segments": [{"key": k, "label": lbl, "median": _funnel_median(b[k])} for k, lbl in _FUNNEL_SEGMENTS],
            "total_median": _funnel_median(b["total"]),
        }
    return {
        "generated_at": datetime.now().isoformat(),
        "vps": route_funnel(buckets["vps"]),
        "api": route_funnel(buckets["api"]),
        "api_key": db.admin_api_key_stats(),
        "note": "里程碑:注册→配置/上线(API=开始入驻·有蒸馏job;VPS=consumer首活动[B])→内容就绪(API=一次蒸馏完成·VPS=首条记忆)→首次真回复[A]。转化率=到达÷注册(恒≤100%);「↓ 时间」=该段耗时中位。里程碑是相互独立的信号,所以「首次真回复」可能比「内容就绪」还多(用户没写记忆卡就先收到了回复),后段不严格单调。样本太少时段耗时显示「—」(test 环境用户少常见,prod 有数据)。API Key 验证单独统计(下)。",
    }


def _render_onboarding_funnel_page(payload: dict) -> str:
    back = _data_track_page_href(view="events", event=None, offset=0)
    ak = payload.get("api_key") or {}
    ak_total = int(ak.get("total") or 0)
    ak_passed = int(ak.get("passed") or 0)
    ak_stuck = int(ak.get("stuck") or 0)
    ak_pct = f"{(ak_passed/ak_total)*100:.0f}%" if ak_total else "—"
    ak_cls = "ok" if (ak_total and ak_passed/ak_total >= 0.8) else ("warn" if ak_total else "muted")
    apikey_html = (
        "<section class='funnel-col' style='margin-top:16px'>"
        "<h2>API Key 验证（服务端 test_status，非客户端埋点）</h2>"
        f"<div class='fn-line'>通过率 <b class='{ak_cls}'>{ak_pct}</b> · "
        f"通过 <b class='ok'>{ak_passed}</b> / 卡住 <b class='bad'>{ak_stuck}</b> / 共 {ak_total} 个配了 model_api 的用户</div>"
        "<div class='muted' style='margin-top:4px'>「卡住」= 填了 provider/key 但 test_status 还不是 ok（key 没验通）。</div>"
        "</section>"
    )

    def col(title: str, f: dict) -> str:
        reg = int(f.get("registered") or 0)
        steps = f.get("steps", [])
        seg = {s["key"]: s for s in f.get("segments", [])}
        seg_order = ["", "s1", "s2", "s3"]
        rows = []
        for i, st in enumerate(steps):
            pct = st["pct"] * 100
            cls = "ok" if pct >= 70 else ("warn" if pct >= 40 else "bad")
            segk = seg_order[i]
            seg_html = f"<div class='seg'>↓ {html.escape(_funnel_dur(seg[segk]['median']))}</div>" if segk and segk in seg else ""
            rows.append(
                f"{seg_html}<div class='step'><div class='fn-bar'><span class='{cls}' style='width:{pct:.0f}%'></span></div>"
                f"<div class='fn-line'><b>{html.escape(st['label'])}</b> <span class='{cls}'>{st['count']} · {pct:.0f}%</span></div></div>"
            )
        total = _funnel_dur(f.get("total_median"))
        return (f"<section class='funnel-col'><h2>{html.escape(title)} · 注册 {reg}</h2>{''.join(rows)}"
                f"<div class='total'>注册→首回复 中位:<b>{html.escape(total)}</b></div></section>")

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Onboarding 漏斗 · Data Track</title>
<style>
  :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  main {{ max-width:900px; margin:0 auto; padding:28px 24px 48px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:0 0 14px; }}
  a {{ color:var(--accent); text-decoration:none; }} .muted {{ color:var(--muted); }} .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
  .cols {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-top:16px; }}
  .funnel-col {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:16px; }}
  .step {{ margin:0 0 4px; }} .fn-bar {{ height:8px; background:#eee3d9; border-radius:4px; overflow:hidden; }}
  .fn-bar span {{ display:block; height:100%; background:var(--accent); }}
  .fn-bar span.ok {{ background:var(--ok); }} .fn-bar span.warn {{ background:var(--warn); }} .fn-bar span.bad {{ background:var(--bad); }}
  .fn-line {{ font-size:13px; margin:3px 0 0; }} .seg {{ color:var(--muted); font-size:12px; margin:6px 0 6px 4px; }}
  .total {{ margin-top:14px; padding-top:10px; border-top:1px solid var(--line); font-size:13px; color:var(--muted); }}
</style></head><body><main>
  <div><a href="{html.escape(back, quote=True)}">← 返回事件健康度</a></div>
  <h1>Onboarding 漏斗</h1>
  <div class="muted">{html.escape(str(payload.get('note') or ''))}</div>
  <div class="cols">{col('VPS（自托管）', payload.get('vps', {}))}{col('API（托管）', payload.get('api', {}))}</div>
  {apikey_html}
  <div class="muted" style="margin-top:14px">Generated {html.escape(_bj_iso(payload.get('generated_at')))}.</div>
</main></body></html>"""


def _data_track_event_users_payload(category: str) -> dict:
    label = dict(_EVENT_CATEGORIES).get(category, category)
    is_reply = category == "reply"
    users = []
    for r in db.admin_events_by_user(category):
        total = int(r.get("total") or 0)
        if total <= 0:
            continue
        resolved = int(r.get("success") or 0) + int(r.get("failed") or 0)
        rate = (int(r.get("success") or 0) / resolved) if resolved else 0.0
        users.append({
            "user_id": r.get("user_id"),
            "route": "API" if str(r.get("route")) == "model_api" else "VPS",
            "total": total, "success": int(r.get("success") or 0), "failed": int(r.get("failed") or 0),
            "rate": rate, "fallback": r.get("fallback"), "fallback_base": r.get("fallback_base"),
            "median_dur": r.get("median_dur"),
            "last_at": core_util._epoch_to_iso(r.get("last_ts")) if r.get("last_ts") else "",
        })
    users.sort(key=lambda u: (u["rate"], -u["total"]))  # worst success-rate first
    return {"generated_at": datetime.now().isoformat(), "category": category,
            "label": label, "is_reply": is_reply, "users": users[:400]}


def _render_event_users_page(payload: dict) -> str:
    label = payload.get("label", "")
    is_reply = payload.get("is_reply")
    users = payload.get("users", [])
    back = _data_track_page_href(view="events", event=None, offset=0)
    rows = []
    for u in users:
        rate = f"{u['rate']*100:.0f}%"
        cls = "ok" if u["rate"] >= 0.8 else ("warn" if u["rate"] >= 0.5 else "bad")
        d = u.get("median_dur")
        dur_s = "—" if d is None else (f"{float(d):.1f}s" if float(d) < 60 else f"{float(d)/60:.1f}m")
        if is_reply:
            fb = int(u.get("fallback") or 0)
            base = int(u.get("fallback_base") or 0)
            fb_pct = f"{(fb/base)*100:.0f}%" if base else "—"
            extra = f"兜底 {fb_pct} · 延迟 {dur_s}"
        else:
            extra = dur_s
        uhref = f"/admin/data-track/users/{quote(str(u['user_id']))}"
        rows.append(
            "<tr>"
            f"<td><a href='{html.escape(uhref, quote=True)}'>{html.escape(str(u['user_id']))}</a></td>"
            f"<td>{html.escape(u['route'])}</td>"
            f"<td><b class='{cls}'>{rate}</b></td>"
            f"<td>{u['total']} <span class='muted'>·{u['success']}✓/{u['failed']}✗</span></td>"
            f"<td>{html.escape(extra)}</td>"
            f"<td class='muted'>{html.escape(_bj_iso(u.get('last_at')))}</td>"
            "</tr>"
        )
    body = "".join(rows) if rows else "<tr><td colspan='6' class='muted'>此事件暂无用户数据。</td></tr>"
    metric3 = "兜底率·延迟" if is_reply else "中位耗时"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(label)} · 按用户 · Data Track</title>
<style>
  :root {{ color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  main {{ max-width:1000px; margin:0 auto; padding:28px 24px 48px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }} .muted {{ color:var(--muted); }} .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
  a {{ color:var(--accent); text-decoration:none; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
  th,td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); }}
  th {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; background:#f4ece5; }}
  tr:last-child td {{ border-bottom:0; }}
</style></head><body><main>
  <div><a href="{html.escape(back, quote=True)}">← 返回事件健康度</a></div>
  <h1>{html.escape(label)} · 按用户（最差成功率在前）</h1>
  <div class="muted">点用户 id 看单用户详情。Generated {html.escape(_bj_iso(payload.get('generated_at')))}.</div>
  <table>
    <thead><tr><th>用户</th><th>类型</th><th>成功率</th><th>次数(✓/✗)</th><th>{metric3}</th><th>最近一次</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</main></body></html>"""


def _render_perception_permissions(user: dict) -> str:
    """Readable 感知授权 & 主动开关 block for the user detail page — so 'can't use
    album/screen' can be answered on sight (granted vs not vs unknown)."""
    pp = user.get("perception_permissions")
    if not isinstance(pp, dict):
        return ""

    def _perm_pill(label, state):
        s = str(state).strip().lower()
        if s in ("authorized", "granted", "true", "on", "1", "yes", "allowed", "full", "limited"):
            cls, txt = "ppok", ("已授权" if s != "limited" else "部分授权")
        elif s in ("denied", "restricted", "false", "off", "0", "no", "blocked"):
            cls, txt = "ppbad", "未授权"
        else:
            cls, txt = "ppmuted", (str(state) or "未知")
        return f"<span class='pp-item'>{html.escape(str(label))} <b class='{cls}'>{html.escape(txt)}</b></span>"

    perm = pp.get("permission_states") if isinstance(pp.get("permission_states"), dict) else {}
    perm_html = (
        "".join(_perm_pill(k, v) for k, v in perm.items())
        or "<span class='ppmuted'>permission_states 为空——设备没上报任何感知授权（可能未授权，也可能这版 app 没上报此字段）</span>"
    )
    sw = pp.get("switches") if isinstance(pp.get("switches"), dict) else {}
    sw_html = "".join(
        f"<span class='pp-item'>{html.escape(str(k))} <b class='{'ppok' if v else 'ppmuted'}'>{'开' if v else '关'}</b></span>"
        for k, v in sw.items()
    )
    directive_configured = bool(pp.get("wake_directive_configured"))
    directive_html = (
        "<div class='ppmuted' style='margin-top:6px'>"
        f"自定义 wake 指令：{'已配置' if directive_configured else '未配置'}"
        f" · 间隔：{html.escape(str(pp.get('wake_interval_sec') or 0))} 秒"
        "</div>"
    )
    return (
        "<h2 style='font-size:15px;margin:22px 0 6px'>感知授权 &amp; 主动开关</h2>"
        "<div class='ppmuted' style='font-size:12px;margin-bottom:6px'>感知授权=设备上报的各感知权限（相册/屏幕/位置/健康…）；开关=用户自己的主动/自主开关。</div>"
        f"<div class='pp-box'><b>感知授权</b><br>{perm_html}</div>"
        f"<div class='pp-box'><b>主动开关</b><br>{sw_html}{directive_html}</div>"
    )


def _render_perception_freshness(user: dict) -> str:
    """Per-field last-report freshness — so '感知读不到' can be split into
    'device stopped feeding' vs 'backend read gap' on sight (usr_7f30/usr_5d3d)."""
    pf = user.get("perception_freshness")
    if not isinstance(pf, dict) or pf.get("error"):
        return ""
    fields = pf.get("fields") or []
    if not fields:
        return ""
    fresh_n = sum(1 for f in fields if f.get("fresh"))
    stale_n = sum(1 for f in fields if f.get("reported") and not f.get("fresh"))
    never_n = sum(1 for f in fields if not f.get("reported"))

    def _fmt_ts(ts):
        return html.escape(_bj_iso(core_util._epoch_to_iso(ts))) if ts else "—"

    rows = []
    # reported-and-stale first (what triage cares about), then fresh, then never.
    for f in sorted(fields, key=lambda r: (not r.get("reported"), bool(r.get("fresh")), r.get("capability"))):
        if not f.get("reported"):
            status, last, age = "<span class='ppmuted'>— 从未上报</span>", "—", "—"
        elif f.get("fresh"):
            status = "<b class='ppok'>✓ 新鲜</b>"
            last, age = _fmt_ts(f.get("last_report_ts")), _fmt_duration_sec(f.get("age_sec"))
        else:
            status = "<b style='color:#a05a00'>⚠️ 过期</b>"
            last, age = _fmt_ts(f.get("last_report_ts")), _fmt_duration_sec(f.get("age_sec"))
        ttl = f.get("ttl_sec")
        ttl_txt = _fmt_duration_sec(ttl) if ttl else "常驻"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(f.get('field')))}</td>"
            f"<td class='ppmuted'>{html.escape(str(f.get('capability')))}</td>"
            f"<td>{last}</td><td>{age}</td><td>{ttl_txt}</td><td>{status}</td>"
            "</tr>"
        )
    app_ts = pf.get("recent_app_open_ts")
    app_line = (
        f"<div class='ppmuted' style='margin-top:6px'>最近 app_open 上报：{_fmt_ts(app_ts)}"
        f"（{_fmt_duration_sec(pf.get('recent_app_open_age_sec'))} 前）</div>"
        if app_ts else
        "<div class='ppmuted' style='margin-top:6px'>最近 app_open 上报：无（iOS 快捷指令可能已停）</div>"
    )
    return (
        "<h2 style='font-size:15px;margin:22px 0 6px'>感知上报新鲜度</h2>"
        "<div class='ppmuted' style='font-size:12px;margin-bottom:6px'>"
        "每个感知字段的最后上报时间（只看时间戳，不含内容）。过期=已超该字段 TTL，agent 现在读到 null；"
        "用来分辨『客户端停止上报』vs『后端读取问题』。共 "
        f"{len(fields)} 字段：<b class='ppok'>{fresh_n} 新鲜</b> · "
        f"<b style='color:#a05a00'>{stale_n} 过期</b> · {never_n} 从未上报。</div>"
        "<table style='font-size:12px'><thead><tr>"
        "<th>字段</th><th>能力</th><th>最后上报</th><th>距今</th><th>TTL</th><th>状态</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>" + app_line
    )


def _render_user_detail_page(user: dict) -> str:
    qs = _data_track_qs()
    back = f"/admin/data-track?{qs}" if qs else "/admin/data-track"
    safe_json = json.dumps(_bj_deep(user), ensure_ascii=False, indent=2)
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
    .pp-box {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; margin:8px 0; font-size:13px; line-height:2; }}
    .pp-item {{ display:inline-block; margin:0 14px 4px 0; }}
    .ppok {{ color:#1d7a4d; }} .ppbad {{ color:#b7352b; }} .ppmuted {{ color:var(--muted); }}
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
    <div class="card"><div class="value">{html.escape(user.get('genesis', {}).get('status') or 'none')}</div><div class="label">genesis distill</div></div>
    <div class="card"><div class="value">{user['proactive']['proactive_messages']}</div><div class="label">proactive writes</div></div>
  </section>
  {_render_perception_permissions(user)}
  {_render_perception_freshness(user)}
  <div class="muted" style="margin-top:14px">以下所有时间已转北京时间(UTC+8) · 原始存储为 UTC。</div>
  <pre>{html.escape(safe_json)}</pre>
</main>
</body>
</html>"""


# Synthetic chat-loop ping — server posts a marker user message,
# posts a synthetic ping, waits for an agent-role reply, reports back.
# This proves that some reply pipeline is alive. It cannot, by itself,
# prove that a one-shot CLI is resident; a bridge/fallback may answer.
