"""Admin data-track: per-user stats, DAU, HTML pages, store evict."""

import json
import hashlib
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from admin import usage as admin_usage
from chat import consumer as chat_consumer
from memory import service as memory_service
from notices import core as notices_core
from proactive import service as proactive_service
from bootstrap import gates as boot_gates
from core import store as core_store
from core import util as core_util
from identity import service as identity_service


_PROVIDER_ATTEMPT_STREAM = "provider_attempts"
_PROVIDER_ATTEMPT_DETAIL_LIMIT = 200
_NOTICE_SUMMARY_LIMIT = 20
_DATA_TRACK_USER_ID_RE = re.compile(r"^usr_[0-9a-f]{16}$")


class InvalidDataTrackUserId(ValueError):
    pass


def _normalized_data_track_user_id(raw_user_id: str) -> str:
    user_id = str(raw_user_id or "").strip()
    if not _DATA_TRACK_USER_ID_RE.fullmatch(user_id):
        raise InvalidDataTrackUserId("invalid_user_id")
    return user_id


def _data_track_query_int(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


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
        "mode", "reveal", "page", "event", "day", "events_limit", "hours",
        "runtime_state",
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


def _data_track_capture_doc(value, *, max_items: int = 20):
    if isinstance(value, dict):
        return {
            str(key)[:80]: _data_track_capture_doc(item, max_items=max_items)
            for key, item in list(value.items())[:max_items]
        }
    if isinstance(value, list):
        return [
            _data_track_capture_doc(item, max_items=max_items)
            for item in value[:max_items]
        ]
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:500]


def _memory_capture_validation_detail(store: UserStore, *, limit: int = 50) -> dict:
    """Expose bounded capture write decisions on the per-user Data Track page."""
    jobs = [
        job
        for job in store.list_proactive_jobs(limit=0)
        if isinstance(job, dict)
        and isinstance(job.get("capture_result"), dict)
        and (
            str(job.get("job_kind") or "") == "memory_capture"
            or str((job.get("capture_result") or {}).get("job_kind") or "")
            == "memory_capture"
        )
    ]
    jobs.sort(
        key=lambda job: core_util._to_epoch(
            job.get("updated_at") or job.get("ts") or job.get("timestamp")
        ),
        reverse=True,
    )
    skipped: dict[str, int] = {}
    applied = {"added": 0, "superseded": 0}
    rows: list[dict] = []
    for job in jobs:
        result = job.get("capture_result") or {}
        for reason, count in (result.get("skipped") or {}).items():
            try:
                skipped[str(reason)] = skipped.get(str(reason), 0) + max(
                    0, int(count or 0)
                )
            except (TypeError, ValueError):
                continue
        for action in applied:
            try:
                applied[action] += max(
                    0, int((result.get("applied") or {}).get(action) or 0)
                )
            except (TypeError, ValueError):
                continue
        if len(rows) < limit:
            rows.append({
                "job_id": str(job.get("job_id") or ""),
                "status": str(job.get("status") or ""),
                "status_reason": str(job.get("status_reason") or ""),
                "updated_at": str(
                    job.get("updated_at")
                    or job.get("ts")
                    or job.get("timestamp")
                    or ""
                ),
                "capture_result": _data_track_capture_doc(result, max_items=20),
                "memory_action_status": _data_track_capture_doc(
                    job.get("memory_action_status") or {}, max_items=20
                ),
            })
    return {
        "jobs_total": len(jobs),
        "applied": applied,
        "skipped": skipped,
        "jobs": rows,
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


def _tracking_stats(
    store: UserStore,
    *,
    include_events: bool = False,
    events_limit: int = 50,
) -> dict:
    try:
        detail_limit = max(1, min(int(events_limit or 50), 500))
    except (TypeError, ValueError):
        detail_limit = 50
    events = store.list_tracking_events(limit=0)
    by_type = _count_rows(events, "type")
    epochs = [core_util._to_epoch(e.get("ts") or e.get("created_at")) for e in events]
    latest = sorted(
        events,
        key=lambda e: core_util._to_epoch(e.get("ts") or e.get("created_at")),
        reverse=True,
    )[:detail_limit]
    out = {
        "events": len(events),
        "by_type": by_type,
        "last_at": core_util._epoch_to_iso(max(epochs, default=0)),
        "events_limit": detail_limit,
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
        "distill_model",
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
    metadata = _safe_genesis_metadata(raw.get("metadata"))
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
        "distill_model": str(metadata.get("distill_model") or "")[:160],
        "metadata": metadata,
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


def _data_track_user_mcp_from_snapshot(snap: dict) -> dict:
    """Whether this user has MCP servers saved, and how many are switched ON.

    Metadata only — the db aggregate counts in SQL so no encrypted envelope
    (each server's url + auth headers) ever enters this process. ``configured``
    answers a question the app cannot: its connection test is a control-plane
    probe that dials the server directly and passes without saving anything, so
    a user reporting "the test was green" is not evidence that anything was
    stored. ``enabled_count`` separates the two failure shapes that look
    identical from outside — a saved-but-switched-off server still advertises a
    NON-empty fingerprint and materializes cleanly, yet reaches the agent as
    zero servers, exactly like a broken apply chain would.
    """
    mcp = dict(snap.get("user_mcp") or {})
    configured_count = int(mcp.get("configured_count") or 0)
    return {
        "configured": configured_count > 0,
        "configured_count": configured_count,
        "enabled_count": int(mcp.get("enabled_count") or 0),
        "fingerprint": str(mcp.get("fingerprint") or ""),
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
    # Liveness is `last_seen_at`, never the `connected` flag. That flag only
    # means "a binding row exists", and bindings are append-only — the backend
    # upserts one for the active route on every whoami and never clears the old
    # ones — so it stays true forever once a user has merely *selected* a route.
    # Trusting it labelled 103 of 278 prod resident users 掉线 ("it broke, go
    # debug their consumer") when the truth was 未连接 ("they never ran one").
    # Those are two different next actions for whoever picks up the ticket.
    if not seen:
        return {"status": "idle", "label": "未连接", "last_seen_at": "", "stale_h": None}
    if sh is None or sh > _CONN_STALE_H:
        return {"status": "offline", "label": "掉线", "last_seen_at": seen, "stale_h": sh}
    return {"status": "ok", "label": "在线", "last_seen_at": seen, "stale_h": sh}


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


def _effective_responder(
    *,
    route: str,
    consumer_state: dict | None,
    runtime: dict | None,
    now_epoch: float | None = None,
) -> dict:
    """Classify who can answer, keeping current facts separate from samples.

    A live V1 lease and V2 ownership are current control-plane facts. Poll rows
    are recent observations only; they never prove that a process is still
    running. This distinction is surfaced verbatim in the payload/UI because a
    single last-writer-wins ``consumer_id`` caused the original misdiagnosis.
    """
    now = time.time() if now_epoch is None else float(now_epoch)
    state = consumer_state if isinstance(consumer_state, dict) else {}
    runtime_data = runtime if isinstance(runtime, dict) else {}
    lease = runtime_data.get("runner_lease")
    lease = lease if isinstance(lease, dict) else {}
    model_route = runtime_data.get("model_api_route")
    model_route = model_route if isinstance(model_route, dict) else {}
    runtime_state = str(
        runtime_data.get("hosted_runtime_state") or "resident"
    ).strip().lower()

    raw_pollers = state.get("poll_consumers")
    pollers = raw_pollers if isinstance(raw_pollers, dict) else {}
    # Backward compatibility for states written before poll_consumers existed.
    if not pollers and state.get("last_poll_epoch"):
        consumer_id = str(state.get("consumer_id") or "")
        responder = (
            "hosted_v1"
            if consumer_id.startswith("agent-runner:")
            else "hosted_v2"
            if consumer_id == "hosted_runtime_v2"
            else "resident"
        )
        pollers = {
            consumer_id or str(state.get("consumer_name") or "unknown"): {
                "consumer_id": consumer_id,
                "consumer_name": str(state.get("consumer_name") or ""),
                "responder": responder,
                "last_poll_at": str(state.get("last_poll_at") or ""),
                "last_poll_epoch": state.get("last_poll_epoch"),
            }
        }

    poll_observations = []
    for identity, value in pollers.items():
        if not isinstance(value, dict):
            continue
        try:
            last_epoch = float(value.get("last_poll_epoch") or 0)
        except (TypeError, ValueError):
            last_epoch = 0.0
        age_sec = max(0.0, now - last_epoch) if last_epoch > 0 else None
        responder = str(value.get("responder") or "")
        if responder not in {"hosted_v1", "hosted_v2", "resident"}:
            consumer_id = str(value.get("consumer_id") or "")
            responder = (
                "hosted_v1"
                if consumer_id.startswith("agent-runner:")
                else "hosted_v2"
                if consumer_id == "hosted_runtime_v2"
                else "resident"
            )
        poll_observations.append(
            {
                "identity": str(identity),
                "responder": responder,
                "last_poll_at": str(value.get("last_poll_at") or ""),
                "last_poll_epoch": last_epoch,
                "age_sec": int(age_sec) if age_sec is not None else None,
                "recent": bool(
                    age_sec is not None
                    and age_sec <= chat_consumer._CONSUMER_RECENT_SEC
                ),
            }
        )
    poll_observations.sort(
        key=lambda item: item["last_poll_epoch"],
        reverse=True,
    )
    recent_polls = [item for item in poll_observations if item["recent"]]

    v2_owned = runtime_state in {"v2", "draining"}
    v1_lease_active = bool(lease.get("active"))
    current = []
    if v1_lease_active:
        current.append("hosted_v1")
    if v2_owned:
        current.append("hosted_v2")
    detected = set(current)
    detected.update(item["responder"] for item in recent_polls)

    if v2_owned:
        effective = "hosted_v2"
        basis = "v2_runtime_ownership"
    elif v1_lease_active:
        effective = "hosted_v1"
        basis = "live_agent_runtime_lease"
    elif recent_polls:
        effective = recent_polls[0]["responder"]
        basis = "most_recent_poll_observation"
    else:
        effective = "none"
        basis = "no_current_or_recent_evidence"

    mismatch_reasons = []
    hosted_detected = bool(detected & {"hosted_v1", "hosted_v2"})
    if route != "model_api" and hosted_detected:
        mismatch_reasons.append("non_model_api_route_with_hosted_responder")
    if route == "official_import" and "resident" in detected:
        mismatch_reasons.append("official_import_route_with_resident_poll")
    if route == "model_api" and "resident" in detected:
        mismatch_reasons.append("model_api_route_with_resident_poll")
    if len(detected) > 1:
        mismatch_reasons.append("multiple_responder_classes_detected")
    if route != "model_api" and bool(model_route.get("is_active")):
        mismatch_reasons.append("non_model_api_route_with_active_model_route")

    return {
        "effective_responder": effective,
        "runtime_state": runtime_state,
        "basis": basis,
        "mismatch": bool(mismatch_reasons),
        "mismatch_reasons": mismatch_reasons,
        "current_control_plane": sorted(current),
        "poll_observations": poll_observations,
        "recent_poll_observations": recent_polls,
        "criteria": (
            "current: live agent_runtime_instances lease => hosted_v1; "
            "v2_runtime_state v2/draining => hosted_v2. "
            "observed: each consumer identity's poll within the recent window; "
            "poll evidence is not proof the process is still running."
        ),
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
        "user_mcp": _data_track_user_mcp_from_snapshot(snap),
    }
    row["responder"] = _effective_responder(
        route=route,
        consumer_state=(
            blobs.get("consumer_state")
            if isinstance(blobs.get("consumer_state"), dict)
            else None
        ),
        runtime=snap.get("responder_runtime"),
    )
    return row


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


def _model_api_route_summaries(user_id: str) -> list[dict]:
    """Return support-safe route state without credentials or endpoint URLs."""
    out = []
    for route in db.model_api_routes_list(user_id):
        purposes = []
        if route.get("is_active"):
            purposes.append("chat")
        if route.get("is_vision"):
            purposes.append("vision")
        # image_generation was missing here while the column existed, so an
        # image-gen route rendered as `purpose: []` — indistinguishable from a
        # route with no job at all. Diagnosing usr_7001b1df80e2024d's "生图没引导
        # 我加模型" (2026-08-10) from admin was impossible: the one fact that
        # decides the whole branch (is there a dedicated image route?) was the
        # one fact not projected. Read a projection as a projection.
        if route.get("is_image_generation"):
            purposes.append("image_generation")
        out.append({
            "purpose": purposes,
            "provider": str(route.get("provider") or "")[:80],
            "model": str(route.get("model") or "")[:160],
            "vision_test_status": str(
                route.get("vision_test_status") or "untested"
            )[:40],
            "image_generation_test_status": str(
                route.get("image_generation_test_status") or "untested"
            )[:40],
            "last_image_generation_test_error": str(
                route.get("last_image_generation_test_error") or ""
            )[:300],
            "last_vision_test_error": str(
                route.get("last_vision_test_error") or ""
            )[:300],
            "last_vision_test_at": str(
                route.get("last_vision_test_at") or ""
            )[:40],
            "last_runtime_error_class": str(
                route.get("last_runtime_error_class") or ""
            )[:160],
            "last_runtime_error": str(route.get("last_runtime_error") or "")[:300],
            "updated_at": str(route.get("updated_at") or "")[:40],
        })
    return out


def _notice_summaries(user_id: str, *, limit: int = _NOTICE_SUMMARY_LIMIT) -> list[dict]:
    """Return recent notice metadata using an explicit content-free allowlist."""
    def _count(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def _last_ts(row: dict) -> float:
        return float(core_util._to_epoch(row.get("last_ts")) or 0)

    rows = db.log_read_all(user_id, notices_core.NOTICES_STREAM)
    rows = sorted(
        rows,
        key=_last_ts,
        reverse=True,
    )[: max(0, int(limit))]
    return [
        {
            "error_class": str(row.get("error_class") or "")[:160],
            "blame": str(row.get("blame") or "")[:80],
            "severity": str(row.get("severity") or "")[:40],
            "occurrences": _count(row.get("occurrences")),
            "last_ts": _last_ts(row),
        }
        for row in rows
    ]


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


def _v2_chat_failures_detail(user_id: str) -> dict:
    """Why this user's V2 chat turns failed.

    Complements ``provider_attempt_ledger``, which is V1-only
    (``coverage: chat_turns_only`` is written by the runner, and the V2
    package contains no ledger writes at all). Reading the two together is
    the whole point: a V2 user's ledger goes silent at the moment of the
    cutover, so silence there means "switched", not "no provider call".
    """
    try:
        from model_api_runtime.v2 import jobs_store as _v2_jobs_store
        return _v2_jobs_store.recent_chat_failures_for_user(user_id)
    except Exception as e:  # noqa: BLE001 — observability must never 500 the page
        return {"error": f"{type(e).__name__}:{str(e)[:120]}"}


def _v2_profile_detail(user_id: str) -> dict:
    """Return the Runtime V2 profile's content-free support metadata only.

    The encrypted ``memory`` / ``user`` envelope bodies are deliberately not
    copied, validated, or decrypted here.  Keep this as an explicit allowlist:
    a future field added to the stored profile must not automatically become
    visible through the admin detail endpoint.
    """
    try:
        from model_api_runtime.v2 import profile_store as _v2_profile_store

        document = db.get_blob_strict(
            str(user_id),
            _v2_profile_store.PROFILE_BLOB_KIND,
        )
    except Exception:  # noqa: BLE001 — observability must never 500 the page
        return {"state": "read_error"}

    if document is None:
        return {"state": "missing"}
    if not isinstance(document, dict):
        return {"state": "read_error"}

    state = str(document.get("state") or "")
    if state not in {"ok", "pending", "degraded", "empty"}:
        return {"state": "read_error"}

    def _count(value) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _retry_at(value) -> float:
        try:
            parsed = float(value or 0)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0

    source = document.get("source")
    if not isinstance(source, dict):
        source = {}
    last_attempt = document.get("last_attempt")
    if not isinstance(last_attempt, dict):
        last_attempt = {}
    memory = document.get("memory")
    if not isinstance(memory, dict):
        memory = {}
    user = document.get("user")
    if not isinstance(user, dict):
        user = {}

    return {
        "state": state,
        "memory_chars": _count(memory.get("chars")),
        "user_chars": _count(user.get("chars")),
        "source": {
            "card_count": _count(source.get("card_count")),
            "max_updated_at": str(source.get("max_updated_at") or ""),
            "generated_at": str(source.get("generated_at") or ""),
        },
        "last_attempt": {
            "reject_code": str(last_attempt.get("reject_code") or "")[:160],
            "attempts": _count(last_attempt.get("attempts")),
            "retry_not_before": _retry_at(last_attempt.get("retry_not_before")),
        },
        "disabled": document.get("disabled") is True,
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
    events_limit = _data_track_query_int(
        "events_limit",
        default=50,
        minimum=1,
        maximum=500,
    )
    tracking = _tracking_stats(
        store,
        include_events=include_detail,
        events_limit=events_limit,
    )
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
        daily_days = _data_track_query_int(
            "days",
            default=14,
            minimum=1,
            maximum=90,
        )
        detail_snapshot = db.admin_data_track_snapshot([user_id]).get(user_id, {})
        row["app_usage"] = _data_track_app_usage_from_snapshot(detail_snapshot)
        # Same key the list rows carry (_build_data_track_user_fast), so a
        # client reading one shape can read the other.
        row["user_mcp"] = _data_track_user_mcp_from_snapshot(detail_snapshot)
        detail_blobs = detail_snapshot.get("blobs") or {}
        row["responder"] = _effective_responder(
            route=route,
            consumer_state=(
                detail_blobs.get("consumer_state")
                if isinstance(detail_blobs.get("consumer_state"), dict)
                else None
            ),
            runtime=detail_snapshot.get("responder_runtime"),
        )
        row["daily_usage"] = db.admin_data_track_user_daily_usage(
            user_id=user_id,
            days=daily_days,
            tz="Asia/Shanghai",
        )
        row["daily_usage_days"] = daily_days
        row["runtime"] = _runtime_summary(store)
        row["model_api_routes"] = _model_api_route_summaries(user_id)
        row["notice_summaries"] = _notice_summaries(user_id)
        row["provider_attempt_ledger"] = _provider_attempts_detail(store)
        row["v2_chat_failures"] = _v2_chat_failures_detail(user_id)
        row["v2_profile"] = _v2_profile_detail(user_id)
        row["memory_capture_validation"] = _memory_capture_validation_detail(store)
        _ps = store.load_proactive_settings()
        row["perception_permissions"] = {
            # what the device reports it granted (free-form; keys are app-defined,
            # e.g. photos / screen / location / health / motion / calendar / audio)
            "permission_states": dict(_ps.get("permission_states") or {}),
            # per-user autonomy switches (all default on)
            "switches": {
                "ambient_心跳": bool(_ps.get("enabled", True)),
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
    if raw_view not in {
        "users", "dau", "growth", "proactive", "debug", "events", "health",
        "overview", "imports", "chat", "latency", "runtime", "usage",
    }:
        raw_view = "users"
    raw_runtime_state = (request.args.get("runtime_state") or "").strip().lower()
    if raw_runtime_state not in {"", "v2", "draining", "resident"}:
        raw_runtime_state = ""

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
        "runtime_state": raw_runtime_state,
        "days": read_int("days", 30, 1, 1000),
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
            str(row.get("responder", {}).get("runtime_state") or ""),
            str(row.get("responder", {}).get("effective_responder") or ""),
            " ".join(row.get("access", {}).get("connected_modes") or []),
        ]).lower()
        if needle in hay:
            out.append(row)
    return out


def _data_track_apply_runtime_filter(rows: list[dict], runtime_state: str) -> list[dict]:
    selected = str(runtime_state or "").strip().lower()
    if not selected:
        return rows
    return [
        row for row in rows
        if str((row.get("responder") or {}).get("runtime_state") or "resident")
        .strip().lower() == selected
    ]


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
            row = _build_data_track_user(u, include_detail=True)
        else:
            row = _build_data_track_user_fast(u, snapshot.get(uid, {}))
        health = dict(snapshot.get(uid, {}).get("provider_health") or {})
        row.update(
            {
                "provider_state": str(
                    health.get("provider_state") or "ok"
                ),
                "last_provider_success_at": str(
                    health.get("last_provider_success_at") or ""
                ),
                "last_provider_failure_at": str(
                    health.get("last_provider_failure_at") or ""
                ),
                "last_provider_error_class": str(
                    health.get("last_provider_error_class") or ""
                ),
            }
        )
        rows.append(row)
    rows = _data_track_apply_runtime_filter(
        rows, str(filters.get("runtime_state") or "")
    )
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
    provider_needs_user_action = 0
    runtime_state_counts: dict[str, int] = {}
    activated_runtime_state_counts: dict[str, int] = {}
    for row in rows:
        stage = row["onboarding"]["stage"]
        route = row["route"]
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        route_counts[route] = route_counts.get(route, 0) + 1
        runtime_state = str(
            (row.get("responder") or {}).get("runtime_state") or "resident"
        ).strip().lower()
        runtime_state_counts[runtime_state] = (
            runtime_state_counts.get(runtime_state, 0) + 1
        )
        for mode in row.get("access", {}).get("connected_modes", []):
            access_mode_counts[mode] = access_mode_counts.get(mode, 0) + 1
        chat_total += row["chat"]["total"]
        memory_total += row["memory"]["total"]
        proactive_jobs += row["proactive"]["jobs"]
        proactive_messages += row["proactive"]["proactive_messages"]
        proactive_failed += row["proactive"]["failed_jobs"]
        if row.get("provider_state") == "needs_user_action":
            provider_needs_user_action += 1
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
            activated_runtime_state_counts[runtime_state] = (
                activated_runtime_state_counts.get(runtime_state, 0) + 1
            )
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
        "runtime_state_counts": runtime_state_counts,
        "activated_runtime_state_counts": activated_runtime_state_counts,
        "activated_route_counts": activated_route_counts,
        "access_mode_counts": access_mode_counts,
        "principals_total": len(set(r.get("principal_id") or r.get("user_id") for r in rows)),
        "chat_messages_total": chat_total,
        "memory_total": memory_total,
        "memory_avg_per_user": (memory_total / len(rows)) if rows else 0,
        "proactive_jobs_total": proactive_jobs,
        "proactive_messages_total": proactive_messages,
        "proactive_failed_total": proactive_failed,
        "provider_needs_user_action": provider_needs_user_action,
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
            "runtime_state": filters.get("runtime_state", ""),
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
    # DAU now = 使用 DAU (app_session_end / genuinely opened the app), not the
    # broad chat∪tracking count. session_dau is frozen in the snapshot too, so
    # historical frozen days stay consistent.
    dau_values = [int(row.get("session_dau") or 0) for row in rows]
    latest = rows[0] if rows else {}
    summary = {
        "generated_at": datetime.now().isoformat(),
        "timezone": "Asia/Shanghai",
        "days_returned": len(rows),
        "latest_day": latest.get("day", ""),
        "latest_dau": int(latest.get("session_dau") or 0),
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
            "dau": "DAU = 使用 DAU: distinct users with an app_session_end (foreground app open) on the Beijing day. Chat DAU / Tracking DAU are looser breakdowns.",
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


def _render_metric(label: str, value, *, hint: str = "", delta: str = "") -> str:
    """``hint`` 是一行口径定义，渲染成 label 旁的 '?'（title 悬浮显示）；
    ``delta`` 是**已渲染好的 HTML**，只允许来自 _render_delta——其余入参照旧
    走 html.escape。两个新参数都有默认值，既有 2 参调用点不用改。"""
    hint_html = (
        f"<span class='hint' title='{html.escape(hint, quote=True)}'>?</span>"
        if hint else ""
    )
    return (
        "<div class='metric'>"
        f"<div class='metric-value'>{html.escape(str(value))}{delta}</div>"
        f"<div class='metric-label'>{html.escape(label)}{hint_html}</div>"
        "</div>"
    )


def _render_delta(current, previous, *, good_when: str = "up") -> str:
    """环比上一窗口的小箭头。previous 为 None/0/非数值时返回空串——0 做分母
    没有意义，而"没有上一窗口数据"不该渲染成 0% 假环比。good_when 指定方向
    语义：'up' 涨是好事、'down' 跌是好事、'neutral' 无好坏（如 token 总量，
    烧多烧少取决于业务，不预设立场）。"""
    try:
        prev = float(previous)
        cur = float(current)
    except (TypeError, ValueError):
        return ""
    if prev == 0:
        return ""
    pct = (cur - prev) / abs(prev) * 100
    # 极端环比显示成倍数上限：上一窗口基数很小时，「▲ +7999900%」没人能
    # 读，也会把整行挤爆。10 倍以上 / 0.1 倍以下只标方向和量级，中间照旧。
    # 只对正基数做倍数换算——prev < 0 时 ratio 的正负号语义会翻转。
    ratio = cur / prev if prev > 0 else None
    if ratio is not None and ratio >= 10:
        arrow = "▲ "
        magnitude = "≥10×"
    elif ratio is not None and ratio <= 0.1:
        arrow = "▼ "
        magnitude = "≤0.1×"
    elif pct > 0:
        arrow = "▲ +"
        magnitude = f"{pct:.0f}%" if pct >= 9.95 else f"{pct:.1f}%"
    elif pct < 0:
        arrow = "▼ −"
        magnitude = f"{-pct:.0f}%" if -pct >= 9.95 else f"{-pct:.1f}%"
    else:
        arrow = "±"
        magnitude = "0%"
    # 小样本不染色：新注册 17→4 渲染成红色 −76% 是统计噪声吓人（prod
    # 2026-08-07 实景）。两侧都 < 20 时箭头和数值照显，但颜色中性——数字是
    # 真的，好坏判断在这个样本量下不是。任一侧 ≥ 20 说明量级足够，照常染色。
    small_base = abs(prev) < 20 and abs(cur) < 20
    if good_when == "neutral" or pct == 0 or small_base:
        cls = "neutral"
    elif good_when == "down":
        cls = "good" if pct < 0 else "bad"
    else:
        cls = "good" if pct > 0 else "bad"
    title = "对比上一个同长度窗口" + ("；样本量小（两侧均 <20），方向仅供参考" if small_base else "")
    return (
        f"<span class='delta {cls}' title='{title}'>"
        f"{arrow}{magnitude}</span>"
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


def _fmt_tokens_compact(value) -> str:
    """Token 计数的紧凑写法。lane 健康表已有 10 列，千分位会把列宽撑爆。

    先除后 `.1f` 会在四舍五入进位时把数字撑回到下一档的整千——例如
    999_950 若按 `abs(n) >= 1_000_000` 才升到 M 档，会先算出 999.95k、
    格式化成 "1000.0k"（视觉上像 100 万却挂着 k 后缀，且违反"紧凑格式
    只显 3 位数"的设计意图）。真实的进位边界是 999_950，不是天真地猜
    999_500——`.1f` 在小数点后第二位 >= 5 时进位，999_950 / 1000 == 999.95
    正好是那个边界值本身。M→B 是同一个 bug 的更高一档（999_950_000 会被
    格式化成 "1000.0M"），按同样的边界收紧。
    """
    if value is None:
        return "—"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "—"
    abs_n = abs(n)
    if abs_n >= 999_950_000:
        return f"{n / 1_000_000_000:.1f}B"
    if abs_n >= 999_950:
        return f"{n / 1_000_000:.1f}M"
    if abs_n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _spark(values: list, *, good_when: str = "neutral") -> str:
    """64×20 内联 SVG 折线 sparkline，纯静态无 JS。

    None 是「缺数据」不是 0：缺的点断成缺口，绝不画成落零——和
    _fmt_count 的未知≠0 语义一致。全缺时渲染灰 — 占位。描边一律
    currentColor，颜色只由 wrapper class 给（.spark / .good / .bad），
    嵌进什么底色的页面都不用改 SVG 本身。good_when 只决定首尾趋势的
    好坏着色：'up' 涨绿跌红、'down' 反之、'neutral' 一律灰（如 token
    总量，烧多烧少不预设立场）。"""
    width, height, pad = 64.0, 20.0, 2.5
    pts: list[float | None] = []
    for v in values or []:
        if v is None:
            pts.append(None)
            continue
        try:
            pts.append(float(v))
        except (TypeError, ValueError):
            pts.append(None)
    known = [p for p in pts if p is not None]
    if not known:
        return "<span class='spark spark-empty' title='暂无样本'>—</span>"
    lo, hi = min(known), max(known)
    span = hi - lo
    n = len(pts)

    def px(i: int) -> float:
        if n <= 1:
            return width / 2
        return pad + (width - 2 * pad) * (i / (n - 1))

    def py(v: float) -> float:
        if span <= 0:
            return height / 2
        return pad + (height - 2 * pad) * (1 - (v - lo) / span)

    segments: list[list[tuple[float, float]]] = []
    run: list[tuple[float, float]] = []
    for i, p in enumerate(pts):
        if p is None:
            if run:
                segments.append(run)
                run = []
            continue
        run.append((px(i), py(p)))
    if run:
        segments.append(run)
    parts: list[str] = []
    for seg in segments:
        if len(seg) == 1:
            # 缺口两侧的孤点画成小圆点，否则单点段在 polyline 里不可见。
            cx, cy = seg[0]
            parts.append(
                f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='1.6' fill='currentColor'/>"
            )
        else:
            attr = " ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
            parts.append(
                f"<polyline points='{attr}' fill='none' stroke='currentColor'"
                " stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/>"
            )
    cls = ""
    if good_when in ("up", "down"):
        first = next(p for p in pts if p is not None)
        last = next(p for p in reversed(pts) if p is not None)
        if last != first:
            rising = last > first
            good = rising if good_when == "up" else not rising
            cls = " good" if good else " bad"
    return (
        f"<span class='spark{cls}' aria-hidden='true'>"
        f"<svg viewBox='0 0 64 20' width='64' height='20'>{''.join(parts)}</svg>"
        "</span>"
    )


def _render_funnel(funnel: dict | None, *, compact: bool) -> str:
    """水平激活漏斗（admin_funnel_snapshot 的渲染面）。

    条宽一律按「本阶段 ÷ 第一阶段」算，阶段间标注逐级转化率与流失数——
    这是单调漏斗（同一 28 天注册 cohort 的里程碑子集），和 users 页旧的
    「行为口径独立占比」不同，后者各分母互不包含、数字可以不单调。
    count 为 None（如 W1 窗口尚无人走完）显 —，不画成 0 宽的假流失；
    第一阶段为 0 时所有比例显「无样本」，不做除零。compact=True 给首页
    （不叠上一窗口）；False 给用户页，每阶段追加上一 28 天窗口的灰色
    对照数。funnel 为 None（builder 失败/未接线）整段坍缩成暂不可用。"""
    if funnel is None:
        return "<div class='muted'>漏斗暂不可用。页面不会把未知渲染成 0。</div>"
    stages = [s for s in (funnel.get("stages") or []) if isinstance(s, dict)]
    if not stages:
        return "<div class='muted'>漏斗暂不可用。页面不会把未知渲染成 0。</div>"
    window_days = int(funnel.get("window_days") or 0)
    prev_by_id: dict[str, dict] = {}
    if not compact:
        for s in ((funnel.get("prev") or {}).get("stages") or []):
            if isinstance(s, dict):
                prev_by_id[str(s.get("id") or "")] = s

    def _count(stage: dict):
        raw = stage.get("count")
        if raw is None:
            return None
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return None

    base = _count(stages[0])
    rows: list[str] = []
    prev_count = None
    for idx, stage in enumerate(stages):
        count = _count(stage)
        label = html.escape(str(stage.get("label") or stage.get("id") or ""))
        if idx > 0:
            # 阶段间的逐级转化行：上一阶段无样本/未知就老实说，不编百分比。
            # 带 eligible 的阶段（W1）分母必须用「窗口已走完的人」——拿 t3
            # 总数当分母会把注册不满 14 天的人算成流失（prod 实景：t3=124
            # 大半未到期，却标成 ↓40%·流失 75）。
            eligible_raw = stage.get("eligible")
            try:
                eligible = int(eligible_raw) if eligible_raw is not None else None
            except (TypeError, ValueError):
                eligible = None
            if prev_count is None:
                conv = "<span class='muted'>—</span>"
            elif prev_count == 0:
                conv = "<span class='muted'>无样本</span>"
            elif count is None:
                conv = "<span class='muted'>暂不可判</span>"
            elif eligible is not None:
                if eligible <= 0:
                    conv = "<span class='muted'>暂不可判（无人走完 W1 窗）</span>"
                else:
                    pct = count / eligible * 100
                    drop = max(0, eligible - count)
                    immature = max(0, prev_count - eligible)
                    imm_note = (
                        f"；{immature:,} 人窗口未到期不计" if immature else ""
                    )
                    conv = (
                        f"↓ {pct:.0f}%<span class='muted'>"
                        f" · 已走完 W1 窗的 {eligible:,} 人中流失 {drop:,}"
                        f"{imm_note}</span>"
                    )
            else:
                pct = count / prev_count * 100
                drop = prev_count - count
                conv = f"↓ {pct:.0f}%<span class='muted'> · 流失 {drop:,}</span>"
            rows.append(f"<div class='hfunnel-conv'>{conv}</div>")
        if count is None:
            width_pct = 0.0
            num = "<span class='muted' title='窗口尚未走完或查询失败，判不了'>—</span>"
        elif base is None or base <= 0:
            width_pct = 0.0
            num = f"{count:,}"
        else:
            width_pct = max(0.0, min(100.0, count / base * 100))
            num = f"{count:,}"
        prev_html = ""
        if not compact:
            prev_stage = prev_by_id.get(str(stage.get("id") or ""))
            prev_val = _count(prev_stage) if prev_stage else None
            prev_html = (
                f"<span class='hfunnel-prev muted' title='上一个 {window_days} 天窗口的同阶段'>"
                f"上期 {prev_val:,}</span>"
                if prev_val is not None else
                "<span class='hfunnel-prev muted' title='上一个窗口取不到'>上期 —</span>"
            )
        rows.append(
            "<div class='hfunnel-stage'>"
            f"<div class='hfunnel-label'>{label}</div>"
            f"<div class='hfunnel-bar'><span style='width:{width_pct:.1f}%'></span></div>"
            f"<div class='hfunnel-num'>{num}{prev_html}</div>"
            "</div>"
        )
        prev_count = count
    caption = (
        f"<div class='hfunnel-caption muted'>近 {window_days} 个完整北京日注册 cohort"
        "（同一批人逐级往下，单调）；W1 活跃证据来自可被裁剪/删号级联的 live 埋点，"
        "计数为已知下限</div>"
        if window_days else ""
    )
    return f"<div class='hfunnel'>{caption}{''.join(rows)}</div>"


def _home_rel_time(epoch) -> str:
    """epoch → 「3h前」相对时间；None/非数值 → —（未知≠刚刚）。"""
    try:
        e = float(epoch)
    except (TypeError, ValueError):
        return "—"
    if e <= 0:
        return "—"
    delta = time.time() - e
    if delta < 60:
        return "刚刚"
    return f"{_format_duration(int(delta))}前"


# ---- Runtime 健康值班台 ----------------------------------------------------
# 阈值集中在此，便于以后一处调整。定阈依据（2026-07-27 三环境实测）：
#   失败率 —— prod 07-26 为 0%、07-27 为 100%，两端都能正确点亮
#   p95    —— pre 健康态 chat p95 = 38.1s，留约 1.5× 余量
#   pending 年龄 —— 对应 claim lag 退化；健康时该值为空
_RUNTIME_HEALTH_WINDOWS = (24, 168, 720)
# 本页只按 hours 取窗口，这几个共享参数它一概读不到。`_data_track_qs` 的保留列表
# 是全视图共用的，所以从别的视图带过来的 day/limit/offset/page 会一路跟着 URL 走、
# 看着生效实则被无视。2026-07-30 审计实证了它的代价：有人拿
# `?view=runtime&day=2026-07-25` 的截图当成"7 月 25 日的数据"，而页面渲染的其实是
# 生成时刻向前 24 小时——参数被静默忽略比参数报错危险，因为页顶还写着「窗口 24
# 小时」，读者不会怀疑自己看错了日期。本页自己生成的链接一律把它们清掉。
_RUNTIME_IGNORED_PARAMS = {
    "day": None,
    "limit": None,
    "offset": None,
    "page": None,
    "runtime_state": None,
}
_RUNTIME_HEALTH_FAILURE_WARN = 0.05
_RUNTIME_HEALTH_FAILURE_BAD = 0.15
_RUNTIME_HEALTH_P95_WARN_MS = 60_000
_RUNTIME_HEALTH_P95_BAD_MS = 120_000
_RUNTIME_HEALTH_PENDING_WARN_SEC = 60
_RUNTIME_HEALTH_PENDING_BAD_SEC = 180
# 交付积压阈值刻意比上面几档宽得多（1 小时 / 6 小时）。effect outbox 与 terminal
# failure outbox 的**稳态积压年龄我们还没有实测基线**，而值班台最不能犯的错是长期
# 挂着一条误报的红——那会训练出"这页的红不用看"。宁可先漏报边缘情形，也要保证亮
# 起来时一定是真堵塞（一条 effect 堵满一小时，无论如何都该有人看）。等 test/pre
# 跑出稳态分布后再收紧，收紧前不要把它当灵敏的告警用。
_RUNTIME_DELIVERY_AGE_WARN_SEC = 3600
_RUNTIME_DELIVERY_AGE_BAD_SEC = 21600
_RUNTIME_FAILURE_CODE_MAX = 64
# 按形状放行，不按枚举前缀放行。agent_jobs.last_error 的真实写入点远不止
# turn_failed:/queue_timeout/lease_timeout 三种（wake_failed:*、
# extraction_failed:*、compaction_failed:*、mcp_mutation_outcome_unknown、
# runtime_expired 都是 mark_failed/mark_expired 落库的合法码），枚举前缀白名单
# 会把 chat 之外每条 lane 的失败原因塌成 other。所有已知写入点产出的码都是
# "scope:kind" 或裸 "snake_case"，且只含小写字母/数字/下划线——照此收紧，而不
# 是照 jobs_store._TERMINAL_ERROR_CODE_RE（那个更宽松的 `:`/`-` 全字符集是给
# outbox 用的，admin 层不 import model_api_runtime，故在此单独定义）。
_RUNTIME_FAILURE_CODE_RE = re.compile(r"^[a-z0-9_]+(:[a-z0-9_]+)?$")
_RUNTIME_ERROR_CLASS_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def _runtime_operational_rate(lane: dict):
    """Hard runtime/provider/timeout rate, with backwards-compatible fallback.

    Old/self-host payload producers do not yet emit ``operational_failure_rate``;
    for those, retaining the raw failure rate is safer than silently turning the
    dashboard green.
    """
    if "operational_failure_rate" in lane:
        return lane.get("operational_failure_rate")
    return lane.get("failure_rate")


def _runtime_failure_code(raw) -> str:
    """失败码白名单化：只放行已知形状（scope:kind 或裸 snake_case），其余归入
    other。

    形状校验而非前缀校验：`turn_failed:` 前缀曾经无条件放行 + 截断，哪怕冒号
    后是含空格/中文的自由文本也会被截断显示——这是本函数要堵住的口子。
    """
    code = str(raw or "").strip()
    if not code or not _RUNTIME_FAILURE_CODE_RE.fullmatch(code):
        return "other"
    return code[:_RUNTIME_FAILURE_CODE_MAX]


def _runtime_health_level(
    payload: dict, delivery: dict | None = None
) -> tuple[str, list[str]]:
    """(总体档位, 中文原因列表)。档位取所有指标里最差的一档。

    分母为 0 的指标一律跳过、不参与判定：零样本不是故障。commit 2795537a 的
    re-review 教训——V2-only 合成行的 legacy 分母为 0 曾被渲染成红 0%，3 条健康
    心跳看起来像全挂。

    ``delivery`` 可选（取不到时为 None，见 admin_core 的三个独立失败域）。它必须
    参与判定而不是只做展示：job 层面全绿但回复/副作用堵在 outbox，正是审计点出的
    "页面报正常、用户没收到"那类故障，而这种故障在 lane 表里没有任何投影。
    """
    rank = {"ok": 0, "warn": 1, "bad": 2}
    worst = "ok"
    reasons: list[str] = []

    def escalate(level: str, reason: str) -> None:
        nonlocal worst
        if rank[level] > rank[worst]:
            worst = level
        if level != "ok":
            reasons.append(reason)

    for lane in payload.get("lanes") or []:
        name = str(lane.get("lane") or "unknown")
        sampled = int(lane.get("sampled_jobs") or 0)
        rate = _runtime_operational_rate(lane)
        if rate is not None and sampled > 0:
            if rate >= _RUNTIME_HEALTH_FAILURE_BAD:
                escalate("bad", f"{name} 系统故障率 {rate * 100:.0f}%")
            elif rate >= _RUNTIME_HEALTH_FAILURE_WARN:
                escalate("warn", f"{name} 系统故障率 {rate * 100:.0f}%")

        # Chat is the foreground user contract: every failed/expired terminal
        # outcome matters even when the cause is an operator route change.  Keep
        # that user-impact signal next to (not folded into) system attribution.
        raw_rate = lane.get("failure_rate")
        if name == "chat" and raw_rate is not None and sampled > 0:
            if raw_rate >= _RUNTIME_HEALTH_FAILURE_BAD:
                escalate("bad", f"chat 回复失败率（终态未成功）{raw_rate * 100:.0f}%")
            elif raw_rate >= _RUNTIME_HEALTH_FAILURE_WARN:
                escalate("warn", f"chat 回复失败率（终态未成功）{raw_rate * 100:.0f}%")

        suppressed = int(lane.get("safety_suppressions") or 0)
        if suppressed:
            escalate("warn", f"{name} 安全抑制 {suppressed} 次（模型输出质量）")

        p95 = lane.get("p95_ok_ms")
        if p95 is not None:
            if p95 >= _RUNTIME_HEALTH_P95_BAD_MS:
                escalate("bad", f"{name} 成功回合 p95 {p95 / 1000:.0f}s")
            elif p95 >= _RUNTIME_HEALTH_P95_WARN_MS:
                escalate("warn", f"{name} 成功回合 p95 {p95 / 1000:.0f}s")

        missing = int((lane.get("capture") or {}).get("missing") or 0)
        if missing > 0:
            escalate("bad", f"{name} trajectory capture missing {missing} 条")

        # partial 此前完全不参与判定：只有 missing 才降级，于是一个带 capture_gap
        # 的回合（轨迹有洞、无法完整回放）在页面上既显示了 partial 计数、综合告警档位
        # 又写「正常」。2026-07-30 审计实证了这个组合的危害——截图里明明有 1 个
        # partial，读者看到的结论是绿的。缺口是真实的取证损失，至少 warn。
        partial = int((lane.get("capture") or {}).get("partial") or 0)
        if partial > 0:
            escalate("warn", f"{name} trajectory 有缺口 {partial} 条（无法完整回放）")

        # 卡死形态（claimed/running，无 pending，worker 心跳还活着）：这条 lane
        # 的 job 全部还没到终态，rate/p95/missing 全部为空/0，池层面也看不出
        # 异常——除了这一件事本身就是矛盾态。sampled_jobs==0 却 capture.open>0
        # 意味着窗口内有回合在飞但一个都没走到终态，必须至少 warn，否则值班台
        # 会在卡死时告诉人「没事」（本分支专门修过一次"卡死 lane 不消失"，但判
        # 定层和文案层此前没跟上）。
        open_count = int((lane.get("capture") or {}).get("open") or 0)
        if sampled == 0 and open_count > 0:
            escalate(
                "warn",
                f"{name} 有 {open_count} 个回合在飞但无一终态（可能卡死）",
            )

    pool = payload.get("pool") or {}
    if int(pool.get("live_workers") or 0) <= 0:
        escalate("bad", "无存活 worker")

    inflight = int(pool.get("inflight") or 0)
    capacity = int(pool.get("capacity") or 0)
    if inflight > capacity:
        escalate("bad", f"在飞 {inflight} 超过容量 {capacity}（矛盾态）")

    age = pool.get("oldest_pending_age_sec")
    if age is not None:
        if age >= _RUNTIME_HEALTH_PENDING_BAD_SEC:
            escalate("bad", f"最老 pending 已排队 {age / 60:.0f} 分钟")
        elif age >= _RUNTIME_HEALTH_PENDING_WARN_SEC:
            escalate("warn", f"最老 pending 已排队 {age:.0f} 秒")

    if delivery:
        def _delivery_age(label: str, age_sec) -> None:
            if age_sec is None:
                return
            if age_sec >= _RUNTIME_DELIVERY_AGE_BAD_SEC:
                escalate("bad", f"{label}已堵 {age_sec / 3600:.1f} 小时")
            elif age_sec >= _RUNTIME_DELIVERY_AGE_WARN_SEC:
                escalate("warn", f"{label}已堵 {age_sec / 60:.0f} 分钟")

        effect = delivery.get("effect_outbox") or {}
        # 只按年龄判定、不按条数：积压 500 条但秒级排空是健康的高吞吐，积压 1 条
        # 卡了两小时才是故障。条数仍然展示（给人看规模），但不进档位。
        _delivery_age("副作用 outbox", effect.get("oldest_pending_age_sec"))

        failure_outbox = delivery.get("terminal_failure_outbox") or {}
        _delivery_age(
            "终态失败投递", failure_outbox.get("oldest_undelivered_age_sec")
        )

        mutation = delivery.get("mcp_mutation") or {}
        # 远端 mutation 结果未知 = 我们可能已经改了用户在第三方的数据却无从确认。
        # 它稀有且不可自愈，出现即需人看，所以不设阈值、见一条就 warn。
        unknown = int(mutation.get("unknown") or 0)
        if unknown > 0:
            escalate("warn", f"MCP 远端改动结果未知 {unknown} 次")
        unresolved = int(mutation.get("unresolved") or 0)
        if unresolved > 0:
            escalate("warn", f"MCP 远端改动悬空未判定 {unresolved} 次")

    return worst, reasons


def _runtime_split_level() -> tuple[dict[str, int], list[str], str]:
    """Small shared state for the three operator-facing Runtime dimensions."""
    return {"ok": 0, "warn": 1, "bad": 2}, [], "ok"


def _runtime_service_level(
    payload: dict, delivery: dict | None = None
) -> tuple[str, list[str]]:
    """Current ability to claim and deliver work, independent of history window."""
    rank, reasons, worst = _runtime_split_level()

    def escalate(level: str, reason: str) -> None:
        nonlocal worst
        if rank[level] > rank[worst]:
            worst = level
        if level != "ok":
            reasons.append(reason)

    pool = payload.get("pool") or {}
    live_workers = int(pool.get("live_workers") or 0)
    capacity = int(pool.get("capacity") or 0)
    if live_workers <= 0:
        escalate("bad", "无存活 worker")
    elif capacity <= 0:
        escalate("bad", "无可执行槽位")
    inflight = int(pool.get("inflight") or 0)
    if inflight > capacity:
        escalate("bad", f"在飞 {inflight} 超过容量 {capacity}")
    pending_age = pool.get("oldest_pending_age_sec")
    if pending_age is not None:
        if pending_age >= _RUNTIME_HEALTH_PENDING_BAD_SEC:
            escalate("bad", f"最老 pending {pending_age / 60:.0f} 分钟")
        elif pending_age >= _RUNTIME_HEALTH_PENDING_WARN_SEC:
            escalate("warn", f"最老 pending {pending_age:.0f} 秒")

    if delivery:
        def delivery_age(label: str, value) -> None:
            if value is None:
                return
            if value >= _RUNTIME_DELIVERY_AGE_BAD_SEC:
                escalate("bad", f"{label}堵塞 {value / 3600:.1f} 小时")
            elif value >= _RUNTIME_DELIVERY_AGE_WARN_SEC:
                escalate("warn", f"{label}堵塞 {value / 60:.0f} 分钟")

        effect = delivery.get("effect_outbox") or {}
        delivery_age("副作用 outbox", effect.get("oldest_pending_age_sec"))
        failure = delivery.get("terminal_failure_outbox") or {}
        delivery_age(
            "终态失败投递", failure.get("oldest_undelivered_age_sec")
        )
    return worst, reasons


def _runtime_execution_level(
    payload: dict, delivery: dict | None = None
) -> tuple[str, list[str]]:
    """Historical success and latency quality for the selected window."""
    rank, reasons, worst = _runtime_split_level()

    def escalate(level: str, reason: str) -> None:
        nonlocal worst
        if rank[level] > rank[worst]:
            worst = level
        if level != "ok":
            reasons.append(reason)

    for lane in payload.get("lanes") or []:
        name = str(lane.get("lane") or "unknown")
        sampled = int(lane.get("sampled_jobs") or 0)
        rate = _runtime_operational_rate(lane)
        if rate is not None and sampled > 0:
            if rate >= _RUNTIME_HEALTH_FAILURE_BAD:
                escalate("bad", f"{name} 系统故障率 {rate * 100:.0f}%")
            elif rate >= _RUNTIME_HEALTH_FAILURE_WARN:
                escalate("warn", f"{name} 系统故障率 {rate * 100:.0f}%")
        raw_rate = lane.get("failure_rate")
        if name == "chat" and raw_rate is not None and sampled > 0:
            if raw_rate >= _RUNTIME_HEALTH_FAILURE_BAD:
                escalate("bad", f"chat 回复失败率（终态未成功）{raw_rate * 100:.0f}%")
            elif raw_rate >= _RUNTIME_HEALTH_FAILURE_WARN:
                escalate("warn", f"chat 回复失败率（终态未成功）{raw_rate * 100:.0f}%")
        suppressed = int(lane.get("safety_suppressions") or 0)
        if suppressed:
            escalate("warn", f"{name} 安全抑制 {suppressed} 次（模型输出质量）")
        p95 = lane.get("p95_ok_ms")
        if p95 is not None:
            if p95 >= _RUNTIME_HEALTH_P95_BAD_MS:
                escalate("bad", f"{name} p95 {p95 / 1000:.0f}s")
            elif p95 >= _RUNTIME_HEALTH_P95_WARN_MS:
                escalate("warn", f"{name} p95 {p95 / 1000:.0f}s")
        open_count = int((lane.get("capture") or {}).get("open") or 0)
        if sampled == 0 and open_count > 0:
            escalate("warn", f"{name} {open_count} 个在飞回合无终态")
    mutation = (delivery or {}).get("mcp_mutation") or {}
    unknown = int(mutation.get("unknown") or 0)
    unresolved = int(mutation.get("unresolved") or 0)
    if unknown:
        escalate("warn", f"MCP 结果未知 {unknown} 次")
    if unresolved:
        escalate("warn", f"MCP 悬空 {unresolved} 次")
    return worst, reasons


def _runtime_trajectory_level(payload: dict) -> tuple[str, list[str]]:
    """Historical replay/forensics completeness for the selected window."""
    rank, reasons, worst = _runtime_split_level()

    def escalate(level: str, reason: str) -> None:
        nonlocal worst
        if rank[level] > rank[worst]:
            worst = level
        if level != "ok":
            reasons.append(reason)

    for lane in payload.get("lanes") or []:
        name = str(lane.get("lane") or "unknown")
        capture = lane.get("capture") or {}
        missing = int(capture.get("missing") or 0)
        partial = int(capture.get("partial") or 0)
        if missing:
            escalate("bad", f"{name} 漏写 {missing} 条")
        if partial:
            escalate("warn", f"{name} 有缺口 {partial} 条")
    return worst, reasons


def _runtime_health_window_hours() -> int:
    """窗口枚举白名单（照 view 参数的写法），非法值一律回落 24。"""
    try:
        value = int(request.args.get("hours", 24))
    except (TypeError, ValueError):
        return 24
    return value if value in _RUNTIME_HEALTH_WINDOWS else 24


# Injected by the assembly layer (asgi_app.py); the real implementation is
# model_api_runtime.v2.jobs_store.recent_runtime_health.
def _runtime_health_summary(*, within_hours: int = 24) -> dict:
    return {
        "window_hours": within_hours,
        "generated_at": 0.0,
        "lanes": [],
        "pool": {
            "inflight": 0, "pending": 0, "live_workers": 0,
            "capacity": 0, "oldest_pending_age_sec": None,
        },
    }


# Injected by the assembly layer (asgi_app.py); the real implementation is
# model_api_runtime.v2.jobs_store.recent_token_usage_by_lane.
def _runtime_token_by_lane(*, within_hours: int = 24) -> dict:
    return {"window_hours": within_hours, "lanes": {}}


# Injected by the assembly layer (asgi_app.py); the real implementation is
# model_api_runtime.v2.jobs_store.recent_delivery_health.
def _runtime_delivery_health(*, within_hours: int = 24) -> dict:
    return {
        "window_hours": within_hours,
        "effect_outbox": {"pending": 0, "oldest_pending_age_sec": None},
        "terminal_failure_outbox": {
            "status_undelivered": 0,
            "runtime_error_undelivered": 0,
            "oldest_undelivered_age_sec": None,
        },
        "mcp_mutation": {"unknown": 0, "unresolved": 0},
    }


# Injected by the assembly layer (asgi_app.py); the real implementation is
# model_api_runtime.v2.jobs_store.recent_runtime_user_delivery_report. Keep this
# stub content-free so Admin can render safely before that binding is installed.
def _runtime_user_report(*, within_hours: int = 24) -> dict:
    return {"window_hours": within_hours, "users": []}


# Injected by the assembly layer (asgi_app.py); the real implementation is
# model_api_runtime.v2.jobs_store.usage_report_snapshot.  The stub keeps Admin
# importable in isolation without reversing the admin -> Runtime V2 dependency.
def _usage_report(query: admin_usage.UsageQuery) -> dict:
    return {
        "overview": {}, "averages": {}, "daily": [], "users": [],
        "models": [], "filters": {}, "coverage": {},
    }


def _runtime_user_delivery_level(delivery: dict) -> str:
    """Return the per-user delivery severity without treating fresh volume as bad.

    A current backlog can be healthy under load; only reconciliation work or an
    old unfinished item makes a user's reliability row degrade.
    """
    reconciliation = int(
        ((delivery or {}).get("all_effects") or {}).get("needs_reconciliation")
        or 0
    )
    if reconciliation > 0:
        return "bad"
    age = (delivery or {}).get("oldest_unfinished_age_sec")
    if age is not None and float(age) >= _RUNTIME_DELIVERY_AGE_BAD_SEC:
        return "bad"
    if age is not None and float(age) >= _RUNTIME_DELIVERY_AGE_WARN_SEC:
        return "warn"
    return "ok"


def _render_runtime_user_report(user_report: dict | None) -> str:
    """Render content-free per-user delivery reliability for Runtime Health."""
    if user_report is None:
        return (
            "<section><h2>用户交付可靠性</h2>"
            "<div class='note-box'><b>用户交付可靠性暂时取不到。</b>"
            "其余 Runtime 健康区块不受影响。</div></section>"
        )

    window_hours = _fmt_count(user_report.get("window_hours"))
    users = user_report.get("users") or []
    if not users:
        empty = (
            f"所选 {window_hours} 小时窗口没有用户指标或当前待交付项。"
        )
        delivery_rows = f"<tr><td colspan='7' class='muted'>{empty}</td></tr>"
    else:
        delivery_rows_list: list[str] = []

        def _user_cell(raw_user_id) -> str:
            user_id = str(raw_user_id or "unknown")
            escaped_user_id = f"<code>{html.escape(user_id)}</code>"
            if user_id == "unknown":
                return escaped_user_id
            qs = _data_track_qs()
            qs_suffix = f"?{qs}" if qs else ""
            href = f"/admin/data-track/users/{quote(user_id, safe='')}{qs_suffix}"
            return (
                f"<a href='{html.escape(href, quote=True)}'>"
                f"{escaped_user_id}</a>"
            )

        def _effect_summary(effect: dict, *, include_discarded: bool) -> str:
            effect = effect or {}
            parts = [
                f"applied {_fmt_count(effect.get('applied_in_window'))}",
            ]
            if include_discarded:
                parts.append(f"discarded {_fmt_count(effect.get('discarded_in_window'))}")
            parts.extend([
                f"pending {_fmt_count(effect.get('pending'))}",
                "needs_reconciliation "
                f"{_fmt_count(effect.get('needs_reconciliation'))}",
            ])
            return " · ".join(parts)

        def _failure_summary(failure: dict) -> str:
            failure = failure or {}
            return " · ".join([
                "reply "
                f"{_fmt_count(failure.get('reply_delivered_in_window'))}"
                f" / {_fmt_count(failure.get('reply_undelivered'))}",
                "status "
                f"{_fmt_count(failure.get('status_delivered_in_window'))}"
                f" / {_fmt_count(failure.get('status_undelivered'))}",
                "error "
                f"{_fmt_count(failure.get('runtime_error_delivered_in_window'))}"
                f" / {_fmt_count(failure.get('runtime_error_undelivered'))}",
            ])

        for user in users:
            user_cell = _user_cell(user.get("user_id"))
            delivery = user.get("delivery") or {}
            level = _runtime_user_delivery_level(delivery)
            level_text = {"ok": "正常", "warn": "注意", "bad": "异常"}[level]
            delivery_rows_list.append(
                "<tr>"
                f"<td>{user_cell}</td>"
                f"<td><span class='pill {level}'>{level_text}</span></td>"
                f"<td>{_effect_summary(delivery.get('reply_effects') or {}, include_discarded=False)}</td>"
                f"<td>{_effect_summary(delivery.get('status_effects') or {}, include_discarded=False)}</td>"
                f"<td>{_effect_summary(delivery.get('all_effects') or {}, include_discarded=True)}</td>"
                f"<td>{_failure_summary(delivery.get('terminal_failure') or {})}</td>"
                f"<td>{_fmt_duration_sec(delivery.get('oldest_unfinished_age_sec'))}</td>"
                "</tr>"
            )

        delivery_rows = "".join(delivery_rows_list)

    return f"""<section>
  <h2>用户交付可靠性</h2>
  <div class='note-box'>
    <b>口径：</b>按 user_id 统计，不按真人/principal 合并；重新注册可能显示多行。
    交付「ok 不代表客户端已读」：它只表示
    服务端已完成可观测的 effect / failure 投递义务。当前 outstanding delivery
    <b>不受所选时间窗口限制</b>，因此旧积压也会显示；applied/discarded 与 delivered
    计数才跟随所选窗口。所有内容、prompt、reply 与 outbox payload 均不渲染。
    Token / model 分析已移到独立的 <b>Usage / 模型用量</b> 页。
  </div>
  <div class="table-wrap"><table class="runtime-user-delivery">
    <thead><tr><th>User</th><th>Reliability</th><th>Reply effects</th><th>Status effects</th><th>All effects</th><th>Failure reply/status/error</th><th>Oldest unfinished</th></tr></thead>
    <tbody>{delivery_rows}</tbody>
  </table></div>
</section>"""


# 视图切换 nav 的分组样式。nav 由 _render_data_track_view_nav 渲染进所有
# 视图页，但各页 <style> 各自独立，所以这两条规则必须出现在**每个**渲染
# nav 的页面里——只放进 _RUNTIME_PAGE_CSS 会让 users / dau / growth /
# proactive / events / debug 的分组间距和标签样式整体失效。
# 普通字符串（非 f-string）；嵌进 f-string 模板时作为值插入，花括号安全。
_NAV_GROUP_CSS = """
    .nav-group { display:inline-flex; flex-wrap:wrap; align-items:center; gap:8px; margin-right:6px; }
    .nav-group-label { color:var(--muted); font-size:11px; font-weight:700; letter-spacing:.06em; }
    .viewbar-diag { margin-top:-8px; padding:8px 10px; border:1px dashed var(--line); border-radius:8px; }
    .viewbar-diag .sort-button { min-height:34px; padding:5px 10px; font-size:12px; }
"""


# Runtime 视图共用这一份样式；其余视图仍保留各自布局，但统一使用同一组
# sage / paper 基础色，避免红色同时表示“当前选中”和“故障”。
# 普通字符串（非 f-string），花括号无需转义。
_RUNTIME_PAGE_CSS = _NAV_GROUP_CSS + """
    :root { color-scheme: light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; }
    body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    main { max-width:1280px; margin:0 auto; padding:28px 24px 48px; }
    h1 { font-size:26px; margin:0 0 4px; }
    h2 { font-size:16px; margin:28px 0 12px; }
    .muted { color:var(--muted); }
    .ok { color:var(--ok); }
    .warn { color:var(--warn); }
    .bad { color:var(--bad); }
    .metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:22px 0; }
    .metric { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }
    .metric-value { font-size:24px; font-weight:700; }
    .metric-label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
    .viewbar,.sortbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:10px 0 18px; }
    .sort-button { display:inline-flex; min-height:44px; box-sizing:border-box; align-items:center; justify-content:center; border:1px solid var(--line); border-radius:6px; padding:9px 12px; background:var(--card); color:var(--fg); font-size:13px; }
    .sort-button.active { border-color:var(--accent); color:var(--accent); background:#e7eee9; }
    .note-box { background:#fff8ef; border:1px solid #e8d8be; border-radius:8px; padding:12px 14px; margin:16px 0 4px; font-size:13px; line-height:1.6; color:#5a4d3c; }
    table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; margin-bottom:18px; }
    th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
    th { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; background:#f4ece5; }
    tr:last-child td { border-bottom:0; }
    a { color:var(--accent); text-decoration:none; }
    code { font-size:12px; }
    .pill { display:inline-flex; border-radius:999px; padding:2px 8px; font-size:12px; background:#efe7df; color:var(--muted); }
    .pill.ok { color:var(--ok); background:#e7f3ed; }
    .pill.warn { color:var(--warn); background:#fff1db; }
    .pill.bad { color:var(--bad); background:#fff1ed; }
    .pill.unknown { color:var(--muted); background:#ecebe3; border:1px solid var(--line); }
    .hint { display:inline-block; margin-left:5px; width:14px; height:14px; line-height:14px; text-align:center; border:1px solid var(--line); border-radius:999px; background:var(--card); color:var(--muted); font-size:10px; text-transform:none; letter-spacing:0; cursor:help; }
    .delta { margin-left:7px; font-size:12px; font-weight:700; letter-spacing:0; vertical-align:2px; white-space:nowrap; }
    .delta.good { color:var(--ok); }
    .delta.bad { color:var(--bad); }
    .delta.neutral { color:var(--muted); }
    .h2-sub { color:var(--muted); font-size:12px; font-weight:400; margin-left:6px; }
    details.note-box summary { cursor:pointer; font-weight:700; color:#7a6a52; }
    details.note-box[open] summary { margin-bottom:8px; }
    .health-strip { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin:18px 0 8px; }
    .health-dimension { min-width:0; padding:15px; background:var(--card); border:1px solid var(--line); border-radius:8px; }
    .health-dimension.warn { background:#fff9ed; border-color:#ead7ad; }
    .health-dimension.bad { background:#fff3f0; border-color:#e7c4bd; }
    .dimension-top { display:flex; justify-content:space-between; align-items:center; gap:12px; font-weight:750; }
    .dimension-detail { min-height:38px; margin-top:10px; font-size:13px; line-height:1.45; }
    .dimension-scope { margin-top:8px; color:var(--muted); font-size:11px; line-height:1.4; }
    .overall-summary { margin:12px 0 3px; color:var(--muted); font-size:13px; font-weight:650; }
    .table-wrap { max-width:100%; overflow-x:auto; }
    .ops-kicker { color:var(--accent); font-size:11px; font-weight:800; letter-spacing:.11em; text-transform:uppercase; }
    .ops-window { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin:18px 0; }
    .ops-window-label { margin-right:3px; color:var(--muted); font-size:12px; }
    .ops-questions { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:18px 0 8px; }
    .question-card { min-width:0; background:var(--card); border:1px solid var(--line); border-top:4px solid var(--line); border-radius:9px; padding:15px; }
    .question-card.ok { border-top-color:var(--ok); background:#f7fbf8; }
    .question-card.warn { border-top-color:var(--warn); background:#fffaf1; }
    .question-card.bad { border-top-color:var(--bad); background:#fff5f2; }
    .question-card.unknown { border-top-color:#c9c7bb; background:#f6f5f0; }
    .question-card .question-link { display:block; color:inherit; text-decoration:none; }
    .question-top { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
    .question-title { margin:0; color:var(--fg); font-size:14px; font-weight:780; }
    .question-value { margin:16px 0 4px; color:var(--fg); font-size:28px; line-height:1; font-weight:780; font-variant-numeric:tabular-nums; }
    .question-fraction { margin:0 0 6px; color:var(--muted); font-size:12px; line-height:1.5; font-variant-numeric:tabular-nums; }
    .question-evidence { min-height:38px; color:var(--muted); font-size:12px; line-height:1.5; }
    .question-drill { margin-top:10px; color:var(--accent); font-size:12px; font-weight:650; }
    .question-card .question-link:hover .question-drill { text-decoration:underline; }
    .funnel-line { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:1px; overflow:hidden; margin:15px 0; border:1px solid var(--line); border-radius:9px; background:var(--line); }
    .funnel-step { background:var(--card); padding:14px; }
    .funnel-step b { display:block; margin:4px 0; font-size:24px; font-variant-numeric:tabular-nums; }
    .funnel-step span,.funnel-step small { display:block; color:var(--muted); }
    .definition-line { max-width:82ch; margin:8px 0 18px; color:var(--muted); font-size:12px; }
    .evidence-ok { color:var(--ok); font-weight:700; }
    .evidence-warn { color:var(--warn); font-weight:700; }
    .evidence-bad { color:var(--bad); font-weight:700; }
    @media (max-width:940px) { .ops-questions { grid-template-columns:1fr 1fr; } }
    @media (max-width:760px) { .health-strip,.ops-questions,.funnel-line { grid-template-columns:1fr; } }
"""


# 首页/诊断枢纽/用户页新增组件（sparkline、水平漏斗、队列、事件流、脉搏卡、
# 诊断卡）的共享样式。users 页的 <style> 是独立 f-string 且 :root 没定义
# --bad，所以这里所有语义色都带字面量兜底——共享块必须在**每个**嵌它的页面
# 里都渲染正确，而不是只在 _RUNTIME_PAGE_CSS 家族里正确。
# 普通字符串（非 f-string）；嵌进 f-string 模板时作为值插入，花括号安全。
_HOME_WIDGET_CSS = """
    .h2-sub { color:var(--muted); font-size:12px; font-weight:400; margin-left:6px; }
    .spark { display:inline-block; margin-left:8px; color:var(--muted); vertical-align:middle; }
    .spark.good { color:var(--ok,#1d7a4d); }
    .spark.bad { color:var(--bad,#b7352b); }
    .spark svg { display:block; }
    .spark-empty { color:var(--muted); font-size:12px; margin-left:8px; }
    .hfunnel { background:var(--card); border:1px solid var(--line); border-radius:9px; padding:14px 16px; margin:10px 0 18px; }
    .hfunnel-caption { font-size:12px; margin-bottom:10px; }
    .hfunnel-stage { display:grid; grid-template-columns:150px minmax(0,1fr) minmax(90px,auto); gap:10px; align-items:center; }
    .hfunnel-label { font-size:13px; }
    .hfunnel-bar { height:14px; background:#eee3d9; border-radius:4px; overflow:hidden; }
    .hfunnel-bar span { display:block; height:100%; background:var(--accent); }
    .hfunnel-num { font-size:14px; font-weight:700; font-variant-numeric:tabular-nums; white-space:nowrap; text-align:right; }
    .hfunnel-prev { font-weight:400; font-size:12px; margin-left:6px; }
    .hfunnel-conv { margin:4px 0 4px 160px; color:var(--fg); font-size:12px; font-variant-numeric:tabular-nums; }
    .verdict-line { margin:16px 0 6px; font-size:15px; }
    .verdict-line b { font-size:22px; font-variant-numeric:tabular-nums; }
    .queue-table td .pill { margin-right:6px; }
    .queue-empty { padding:14px 16px; background:var(--card); border:1px solid var(--line); border-radius:9px; color:var(--ok,#1d7a4d); font-weight:650; margin:10px 0; }
    .feed-list { list-style:none; margin:10px 0 18px; padding:0; background:var(--card); border:1px solid var(--line); border-radius:9px; overflow:hidden; }
    .feed-list li { display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:9px 14px; border-bottom:1px solid var(--line); font-size:13px; }
    .feed-list li:last-child { border-bottom:0; }
    .feed-time { min-width:52px; color:var(--muted); font-variant-numeric:tabular-nums; }
    .pulse-cards { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin:10px 0 18px; }
    .pulse-card { display:block; background:var(--card); border:1px solid var(--line); border-radius:9px; padding:15px; color:inherit; text-decoration:none; }
    .pulse-card:hover { border-color:var(--accent); }
    .pulse-value { font-size:26px; font-weight:780; font-variant-numeric:tabular-nums; }
    .pulse-sub { margin-top:6px; color:var(--muted); font-size:12px; }
    .diag-cards { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin:16px 0; }
    .diag-card { display:block; background:var(--card); border:1px solid var(--line); border-radius:9px; padding:15px; color:inherit; text-decoration:none; }
    .diag-card:hover { border-color:var(--accent); }
    .diag-card h3 { margin:0 0 6px; font-size:14px; }
    .diag-card p { margin:0; color:var(--muted); font-size:12px; line-height:1.5; }
    .diag-card .diag-when { color:var(--accent); font-weight:700; }
    .cost-line { display:flex; flex-wrap:wrap; gap:18px; align-items:center; background:var(--card); border:1px solid var(--line); border-radius:9px; padding:14px 16px; margin:10px 0 18px; }
    .cost-item { font-size:12px; color:var(--muted); }
    .cost-item b { display:block; color:var(--fg); font-size:18px; font-variant-numeric:tabular-nums; }
    @media (max-width:760px) {
      .pulse-cards,.diag-cards { grid-template-columns:1fr; }
      .hfunnel-stage { grid-template-columns:90px minmax(0,1fr) minmax(70px,auto); }
      .hfunnel-conv { margin-left:100px; }
    }
"""

_HOME_PAGE_CSS = _RUNTIME_PAGE_CSS + _HOME_WIDGET_CSS


def _render_runtime_health_page(
    payload: dict,
    tokens: dict | None = None,
    delivery: dict | None = None,
    user_report: dict | None = None,
) -> str:
    """Runtime V2 全 lane 运行时健康值班台（?view=runtime）。

    本页 = 运行时视角（job 生命周期，窗口可切）；「Proactive 日报」= 产品视角
    （日报送达率，按天）。heartbeat lane 两页都出现但口径不同，故本页该行给出
    指向日报页的链接。

    ``tokens`` / ``delivery`` / ``user_report`` 都可能是 None（各自独立的失败域，
    见 admin_core）：取不到时对应区块显「暂不可用」，不得把 None 渲染成 0——0
    是"确认过是零"。
    """
    service_level, service_reasons = _runtime_service_level(payload, delivery)
    if delivery is None and service_level == "ok":
        service_level = "warn"
        service_reasons = ["交付数据暂不可用"]
    execution_level, execution_reasons = _runtime_execution_level(
        payload, delivery
    )
    trajectory_level, trajectory_reasons = _runtime_trajectory_level(payload)
    split_rank = {"ok": 0, "warn": 1, "bad": 2}
    level = max(
        (service_level, execution_level, trajectory_level),
        key=split_rank.__getitem__,
    )
    reasons = service_reasons + execution_reasons + trajectory_reasons
    window_hours = int(payload.get("window_hours") or 24)
    lanes = payload.get("lanes") or []
    pool = payload.get("pool") or {}

    # I-2：recent_runtime_health 的每条子查询共享 LIMIT 1000 的配额，
    # recent_token_usage_by_lane 是窗口内全量、无 LIMIT。只遍历
    # payload["lanes"] 会让"窗口内有 token 开销、但 job 没挤进最近 1000 条"
    # 的 lane 不显示也不报错——而消灭这类非 chat lane 的开销盲区正是本功能
    # 存在的理由。取 tokens["lanes"] 里健康侧看不到的 lane 名，补成健康列
    # 全部为 None（走既有的"无数据"渲染逻辑显 —，不是 0——0 意味着"确认过
    # 是零"，这里是"压根没被健康查询看见"，两者不能混）的合成行；不参与
    # 上面已经算完的 _runtime_health_level 判定（那是纯 job 结局层面的判断，
    # token-only 的 lane 没有任何 job 结局信息可供判定）。跟 jobs_store 里
    # recent_runtime_health 自己的 all_lanes 是同一个哲学，只是补在渲染层。
    token_lanes = (tokens or {}).get("lanes") or {}
    known_lane_names = {str(lane.get("lane") or "unknown") for lane in lanes}
    token_only_lane_names = sorted(
        name for name in token_lanes if name not in known_lane_names
    )
    if token_only_lane_names:
        lanes = list(lanes) + [
            {
                "lane": name,
                "sampled_jobs": None,
                "completed": None,
                "failed": None,
                "expired": None,
                "superseded": None,
                "operational_failures": None,
                "control_outcomes": None,
                "safety_suppressions": None,
                "failure_rate": None,
                "operational_failure_rate": None,
                "p50_ok_ms": None,
                "p95_ok_ms": None,
                "capture": None,
                "top_failures": [],
            }
            for name in token_only_lane_names
        ]

    level_text = {"ok": "正常", "warn": "注意", "bad": "异常"}[level]
    level_cls = {"ok": "ok", "warn": "warn", "bad": "bad"}[level]
    reason_text = ("：" + "；".join(html.escape(r) for r in reasons)) if reasons else ""

    def split_card(title: str, split_level: str, split_reasons: list[str], scope: str) -> str:
        label = {"ok": "正常", "warn": "注意", "bad": "异常"}[split_level]
        detail = "；".join(split_reasons[:3]) if split_reasons else "未发现触发阈值的信号"
        if len(split_reasons) > 3:
            detail += f"；另有 {len(split_reasons) - 3} 项"
        return (
            f"<section class='health-dimension {split_level}'>"
            f"<div class='dimension-top'><span>{html.escape(title)}</span>"
            f"<b class='pill {split_level}'>{html.escape(label)}</b></div>"
            f"<div class='dimension-detail'>{html.escape(detail)}</div>"
            f"<div class='dimension-scope'>{html.escape(scope)}</div>"
            "</section>"
        )

    split_health = "<div class='health-strip'>" + "".join([
        split_card(
            "当前可服务",
            service_level,
            service_reasons,
            "此刻的 worker、pending 与交付积压，不随窗口变化",
        ),
        split_card(
            f"近 {window_hours}h 运行质量",
            execution_level,
            execution_reasons,
            "所选窗口的失败率、成功 p95、无终态回合与 MCP 结果歧义",
        ),
        split_card(
            f"近 {window_hours}h 轨迹可观测性",
            trajectory_level,
            trajectory_reasons,
            "所选窗口的 trajectory 漏写与缺口",
        ),
    ]) + "</div>"

    window_labels = {24: "24 小时", 168: "7 天", 720: "30 天"}
    window_links = "".join(
        f"<a class='sort-button{' active' if hours == window_hours else ''}' "
        f"href='{html.escape(_data_track_page_href(view='runtime', hours=hours, **_RUNTIME_IGNORED_PARAMS), quote=True)}'>"
        f"{html.escape(window_labels[hours])}</a>"
        for hours in _RUNTIME_HEALTH_WINDOWS
    )

    pool_age = pool.get("oldest_pending_age_sec")
    pool_metrics = "".join([
        _render_metric("在飞 job", _fmt_count(pool.get("inflight"))),
        _render_metric("排队 pending", _fmt_count(pool.get("pending"))),
        _render_metric("存活 worker", _fmt_count(pool.get("live_workers"))),
        _render_metric("可执行槽位", _fmt_count(pool.get("capacity"))),
        _render_metric(
            "最老 pending 年龄",
            _fmt_duration_sec(pool_age),
        ),
    ])

    # 交付区块：唯一能看出「job 判成功但产物没到用户」的地方。取不到时明说取不到，
    # 不拿 0 顶替——这个区块显示 0 的含义是"队列是空的、一切送达了"，与"数据取不到"
    # 是相反的结论。
    if delivery is None:
        delivery_section = (
            "<div class='note-box'><b>端到端交付数据暂时取不到。</b>"
            "本页其余部分不受影响；这一项本身也是值得查的信号。</div>"
        )
    else:
        effect = delivery.get("effect_outbox") or {}
        failure_outbox = delivery.get("terminal_failure_outbox") or {}
        mutation = delivery.get("mcp_mutation") or {}
        delivery_section = "<section class='metrics'>" + "".join([
            _render_metric("副作用积压", _fmt_count(effect.get("pending"))),
            _render_metric(
                "最老未 apply",
                _fmt_duration_sec(effect.get("oldest_pending_age_sec")),
            ),
            _render_metric(
                "失败待投递 status/error",
                f"{_fmt_count(failure_outbox.get('status_undelivered'))}"
                f" / {_fmt_count(failure_outbox.get('runtime_error_undelivered'))}",
            ),
            _render_metric(
                "最老未投递",
                _fmt_duration_sec(failure_outbox.get("oldest_undelivered_age_sec")),
            ),
            _render_metric(
                "MCP 未知/悬空",
                f"{_fmt_count(mutation.get('unknown'))}"
                f" / {_fmt_count(mutation.get('unresolved'))}",
            ),
        ]) + "</section>"

    def _ms_cell(value) -> str:
        if value is None:
            return "<td class='muted'>—</td>"
        return f"<td>{value / 1000:.1f}s</td>"

    lane_rows = []
    for lane in lanes:
        name = str(lane.get("lane") or "unknown")
        raw_rate = lane.get("failure_rate")
        operational_rate = _runtime_operational_rate(lane)

        def _rate_cell(rate) -> str:
            if rate is None:
                return "<td class='muted'>—</td>"
            if rate >= _RUNTIME_HEALTH_FAILURE_BAD:
                cls = "bad"
            elif rate >= _RUNTIME_HEALTH_FAILURE_WARN:
                cls = "warn"
            else:
                cls = "ok"
            return f"<td><span class='pill {cls}'>{rate * 100:.0f}%</span></td>"

        raw_rate_cell = _rate_cell(raw_rate)
        operational_rate_cell = _rate_cell(operational_rate)
        capture = lane.get("capture")
        if capture is None:
            # token-only 的合成行（I-2）：健康侧压根没见过这条 lane，捕获状态
            # 未知，不是"0 个漏写"——显 —，不显 0。
            capture_cell = "<td class='muted'>—</td>"
        else:
            missing = int(capture.get("missing") or 0)
            open_count = int(capture.get("open") or 0)
            partial = int(capture.get("partial") or 0)
            capture_cell = (
                f"<td>{int(capture.get('terminal_seen_no_gap') or 0)} / "
                f"<b class='{'warn' if partial else ''}'>{partial}</b> / "
                f"<b class='{'bad' if missing else ''}'>{missing}</b> / "
                f"{open_count}</td>"
            )
        # 某 lane 有 job 但无 turn metric 行时（例如全部回合都还没终态），
        # tokens["lanes"] 里没有这个键——两列显 —，不得 KeyError、也不得显 0。
        lane_tokens = ((tokens or {}).get("lanes") or {}).get(name) or {}
        prompt_tok = lane_tokens.get("input_tokens")
        if prompt_tok is None:
            prompt_tok = lane_tokens.get("prompt_tokens")
        completion_tok = lane_tokens.get("completion_tokens")
        if prompt_tok is None and completion_tok is None:
            token_cell = "<td class='muted'>—</td>"
        else:
            token_cell = (
                f"<td>{_fmt_tokens_compact(prompt_tok)} / "
                f"{_fmt_tokens_compact(completion_tok)}</td>"
            )
        # 缓存命中率与两种上报覆盖率分列。此前它们挤在一列、标签写「缓存命中 ·
        # 上报」，那个"上报"指的是 token usage 上报，读者却会当成 cache 上报
        # （2026-07-30 审计）。cache coverage 与 usage coverage 是两个独立的量。
        hit_ratio = lane_tokens.get("cache_hit_ratio")
        cache_cell = (
            "<td class='muted'>—</td>" if hit_ratio is None
            else f"<td>{_fmt_ratio(hit_ratio)}</td>"
        )
        usage_cov = lane_tokens.get("usage_coverage")
        cache_cov = lane_tokens.get("cache_coverage")
        if usage_cov is None and cache_cov is None:
            coverage_cell = "<td class='muted'>—</td>"
        else:
            coverage_cell = (
                f"<td>{_fmt_ratio(usage_cov)} / {_fmt_ratio(cache_cov)}</td>"
            )
        if name != "screen_watch" or not lane_tokens:
            visible_reply_cell = "<td class='muted'>—</td>"
        else:
            visible_turns = int(lane_tokens.get("visible_reply_turns") or 0)
            measured_turns = int(lane_tokens.get("turns") or 0)
            visible_rate = lane_tokens.get("visible_reply_rate")
            visible_reply_cell = (
                f"<td>{visible_turns} / {measured_turns}"
                + (
                    f" ({_fmt_ratio(visible_rate)})"
                    if visible_rate is not None
                    else ""
                )
                + "</td>"
            )
        lane_label = html.escape(name)
        if name == "heartbeat":
            # 同样清掉本页忽略的参数。目标页（Proactive 日报）只读
            # since/registered_since/days（**复数**），从不读单数 day；limit/offset
            # 在它的 payload 里也没用——不清的话那些参数会跟着跳过去，在新页面上
            # 照样是"看着生效实则被无视"，只是换了一跳、可见度更低。
            hb_href = _data_track_page_href(
                view="proactive", hours=None, **_RUNTIME_IGNORED_PARAMS
            )
            lane_label += (
                f" <a class='muted' style='font-size:12px' "
                f"href='{html.escape(hb_href, quote=True)}'>（日报口径）</a>"
            )
        lane_rows.append(
            "<tr>"
            f"<td><b>{lane_label}</b></td>"
            f"<td>{_fmt_count(lane.get('sampled_jobs'))}</td>"
            f"<td>{_fmt_count(lane.get('completed'))}</td>"
            f"<td>{_fmt_count(lane.get('failed'))}</td>"
            f"<td>{_fmt_count(lane.get('expired'))}</td>"
            f"<td>{_fmt_count(lane.get('operational_failures'))}</td>"
            f"<td class='muted'>{_fmt_count(lane.get('control_outcomes'))}</td>"
            f"<td class='muted'>{_fmt_count(lane.get('safety_suppressions'))}</td>"
            + raw_rate_cell
            + operational_rate_cell
            + _ms_cell(lane.get("p50_ok_ms"))
            + _ms_cell(lane.get("p95_ok_ms"))
            + capture_cell
            + visible_reply_cell
            + token_cell
            + cache_cell
            + coverage_cell
            + "</tr>"
        )

    failure_rows = []
    for lane in lanes:
        name = html.escape(str(lane.get("lane") or "unknown"))
        # 清洗发生在渲染层且不重新聚合：同一 lane 的两个不同原始码若都被清洗成
        # 同一个桶（most commonly "other"），必须在渲染前按清洗后的码合并计数，
        # 否则会渲染成两行都叫 other（reviewer 实证：('other',3) 和
        # ('other',2) 两行，看起来像两个不同故障）。
        merged: dict[tuple[str, str, str], int] = {}
        for item in lane.get("top_failures") or []:
            code = _runtime_failure_code(item.get("code"))
            outcome_class = str(item.get("outcome_class") or "operational_failure")
            if outcome_class not in {
                "operational_failure", "timeout", "control", "safety_suppression"
            }:
                outcome_class = "operational_failure"
            error_class = str(item.get("error_class") or "")
            if not _RUNTIME_ERROR_CLASS_RE.fullmatch(error_class):
                error_class = ""
            key = (code, outcome_class, error_class)
            merged[key] = merged.get(key, 0) + int(item.get("count") or 0)
        class_labels = {
            "operational_failure": "执行故障",
            "timeout": "超时 / 失活",
            "control": "控制切流",
            "safety_suppression": "安全抑制",
        }
        for (code, outcome_class, error_class), count in sorted(
            merged.items(), key=lambda kv: kv[1], reverse=True
        ):
            error_class_html = (
                html.escape(error_class)
                if error_class else "<span class='muted'>—</span>"
            )
            failure_rows.append(
                "<tr>"
                f"<td>{name}</td>"
                f"<td>{class_labels[outcome_class]}</td>"
                f"<td><code>{html.escape(code)}</code></td>"
                f"<td>{error_class_html}</td>"
                f"<td>{_fmt_count(count)}</td>"
                "</tr>"
            )

    # "这不是故障" 只在真的什么都没发生时才说得出口：没有终态样本、没有在飞的
    # 回合（capture.open）、池里也没有 inflight/pending。若窗口内无终态 job 但
    # 有回合在飞，那不是"没数据"，是卡死的形状——I-3 的教训：之前这两种情形共
    # 用同一句"这不是故障"，把卡死误报成正常的空窗口。
    no_terminal_samples = not any(
        int(lane.get("sampled_jobs") or 0) for lane in lanes
    )
    total_open = sum(
        int((lane.get("capture") or {}).get("open") or 0) for lane in lanes
    )
    pool_inflight = int(pool.get("inflight") or 0)
    pool_pending = int(pool.get("pending") or 0)
    if not no_terminal_samples:
        empty_note = ""
    elif total_open == 0 and pool_inflight == 0 and pool_pending == 0:
        empty_note = (
            "<div class='muted'>当前窗口无样本——这不是故障，是这条口径当天没有数据。"
            "可切到 7 天或 30 天。</div>"
        )
    else:
        empty_note = (
            "<div class='muted bad'>窗口内无终态 job，但有 "
            f"{total_open} 个回合在飞（在飞 {pool_inflight} / 排队 {pool_pending}）"
            "——可能是卡死，不是「当天没数据」。</div>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Runtime 健康 · Feedling Data Track</title>
  <style>{_RUNTIME_PAGE_CSS}</style>
</head>
<body>
<main>
  <h1>Runtime 健康</h1>
  <div class="muted">Generated {html.escape(_bj_iso(payload.get("generated_at")))}. Metadata only; encrypted content is not read or rendered.</div>
  {_render_data_track_view_nav("runtime")}
  <div class="sortbar">{window_links}</div>
  <h2>状态拆分</h2>
  {split_health}
  <div class="overall-summary">综合告警档位（取三项最差）：<span class="{level_cls}">{html.escape(level_text)}</span></div>
  <div class="muted">用于兼容告警；当前能否服务请以第一张“当前可服务”卡为准。窗口 {window_hours} 小时{reason_text}</div>
  <div class="note-box">
    <b>口径分工：</b>本页是<b>运行时视角</b>——按 job 生命周期统计，窗口可切。
    「<b>Proactive 日报</b>」是产品视角——按天统计日报送达率。
    heartbeat lane 两页都会出现，但口径不同，别直接对数。
    分母一律从 agent_jobs 起算，因此「完全没写 metrics」的回合不会从统计里消失。
    延迟只算成功回合：失败超时会把 p95 拉高，混在一起会让一个故障看起来像两个。
    token 含<b>失败回合</b>——失败也烧钱，与上方失败率不是同一批样本的筛选口径。
    健康列与 token 列<b>都是窗口内全量</b>，无采样上界——两者覆盖同一批样本，
    可以放在一行对账。
    prompt token 已包含 cache read/write，<b>不要与缓存列相加</b>，否则重复计数。
    本页 token 跟随上方窗口；「Token 与模型」页默认近 30 天，
    两处数字不一致是<b>窗口不同</b>，不是 bug——切到 30 天时应当一致。
    「上报 usage/cache」是两种<b>不同</b>的覆盖率：前者指有多少次调用回报了 token
    usage、后者指有多少次回报了缓存指标，不要当成同一个数。
    捕获列的第一格是「<b>见终态·无缺口</b>」，它<b>不等于</b>轨迹完整：只表示找到了
    turn_terminal 事件且没有 capture_gap，不保证 prompt / provider 往返 / tool call
    / 最终回复这些 artifact 都在。有缺口（第二格）意味着<b>取证已经损失</b>，会把
    综合告警档位压到「注意」。
    <b>覆盖范围：</b>本页只统计<b>本实例托管</b>的 Runtime V2 回合。self-host
    consumer 只会 best-effort 上报部分 provider-attempt 元数据，离线实例完全不可见
    ——所以本页的 token 与失败率<b>不是全体用户的总量</b>，不能当作全量用量账。
    <b>本页只按 hours 取窗口</b>（24 / 168 / 720），URL 里的 day / limit / offset
    在这一页<b>不生效</b>——想看某一天请用 DAU 或 Proactive 日报页。</div>
  {empty_note}
  <h2>Worker 池</h2>
  <section class="metrics">{pool_metrics}</section>
  <h2>端到端交付</h2>
  <div class="muted">job 判 completed 只证明回合跑完了，不证明产物到达用户。
  这里是副作用与失败回包的投递积压——<b>积压条数不参与总体档位，只有"堵了多久"参与</b>
  （高吞吐下瞬时积压是正常的）。两个 outbox 是<b>当前积压状态</b>，不随上方窗口变化；
  MCP 两列才是窗口内计数。阈值目前刻意保守（1 小时注意 / 6 小时异常），稳态基线实测
  后再收紧。</div>
  {delivery_section}
  <h2>各 lane 健康</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>Lane</th><th>样本</th><th>成功</th><th>原始失败</th><th>过期</th><th>系统故障<br><span class='muted'>含过期</span></th><th>控制切流</th><th>安全抑制</th><th>终态未成功率</th><th>系统故障率</th><th>p50(成功)</th><th>p95(成功)</th><th>捕获 见终态·无缺口/有缺口/漏写/在飞</th><th>开口回合/回合<br><span class='muted'>仅 screen_watch</span></th><th>token 入/出</th><th>缓存命中</th><th>上报 usage/cache</th></tr></thead>
    <tbody>{''.join(lane_rows) if lane_rows else "<tr><td colspan='17' class='muted'>当前窗口无 job。</td></tr>"}</tbody>
  </table></div>
  {_render_runtime_user_report(user_report)}
  <h2>未成功原因 Top</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>Lane</th><th>归类</th><th>失败码</th><th>上游安全归因</th><th>次数</th></tr></thead>
    <tbody>{''.join(failure_rows) if failure_rows else "<tr><td colspan='5' class='muted'>当前窗口无未成功终态。</td></tr>"}</tbody>
  </table></div>
  <div class="muted">“终态未成功率”保留所有 failed / expired，回答“这轮有没有正常完成”；
  “系统故障率”只统计 provider、Runtime、排队和 lease 故障。控制切流和安全抑制仍保留原始计数，
  但不再冒充基础设施故障。上游安全归因来自终态 outbox 的 metadata；<b>上游原始错</b>和聊天正文
  均不在本页读取，如确需查看只能走 default-off、全审计的 break-glass trajectory inspector。</div>
</main>
</body>
</html>"""


def _render_runtime_health_error_page() -> str:
    """数据取不到时的降级页：保留 nav，不外泄异常细节。"""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Runtime 健康 · Feedling Data Track</title>
  <style>{_RUNTIME_PAGE_CSS}</style>
</head>
<body>
<main>
  <h1>Runtime 健康</h1>
  {_render_data_track_view_nav("runtime")}
  <div class="note-box">
    <b>Runtime 健康数据暂时取不到。</b>
    多半是数据库连接池或 V2 表访问出了问题——这本身就是一个值得查的信号。
    其他视图不受影响，可继续使用上面的导航。具体异常见后端日志。
  </div>
</main>
</body>
</html>"""


# ---- Operations cockpit: Overview / Imports / Chat / Latency --------------

def _ops_window_hours() -> int:
    """The operations views share one rolling-window contract."""
    return _runtime_health_window_hours()


def _ops_window_links(active: str, hours: int) -> str:
    labels = {24: "24 小时", 168: "7 天", 720: "30 天"}
    links = []
    for value in _RUNTIME_HEALTH_WINDOWS:
        cls = "sort-button active" if value == hours else "sort-button"
        href = _data_track_page_href(
            view=active,
            hours=value,
            day=None,
            limit=None,
            offset=None,
            page=None,
            runtime_state=None,
            user_id=None,
        )
        current = " aria-current='true'" if value == hours else ""
        links.append(
            f"<a class='{cls}' href='{html.escape(href, quote=True)}'"
            f"{current}>{labels[value]}</a>"
        )
    return (
        "<div class='ops-window'><span class='ops-window-label'>统计窗口</span>"
        + "".join(links)
        + "</div>"
    )


def _ops_status(level: str) -> str:
    # unknown 是一等档位：纯"没证据"（查询失败、窗口无样本、缺 p95）显灰，
    # 与"有证据且测出问题"的黄/红分开——否则读者无法区分"该去修"和"该去补数据"。
    labels = {"ok": "正常", "warn": "注意", "bad": "异常", "unknown": "证据不足"}
    normalized = level if level in labels else "warn"
    return f"<span class='pill {normalized}'>{labels[normalized]}</span>"


def _ops_import_level(report: dict | None) -> tuple[str, list[str]]:
    # 纯无证据（查询失败 / 窗口无样本）→ unknown；测得的边缘值才是 warn。
    if report is None:
        return "unknown", ["导入统计暂不可用"]
    started = int(report.get("started") or 0)
    completed = int(report.get("completed") or 0)
    failed = int(report.get("failed") or 0)
    stuck = int(report.get("stuck_over_15m") or 0)
    unverified = int(report.get("completed_unverified") or 0)
    if started == 0:
        return "unknown", ["窗口内没有导入样本"]
    reasons: list[str] = []
    level = "ok"
    terminal = completed + failed
    failure_rate = float(failed) / float(terminal) if terminal else None
    if stuck:
        level = "bad"
        reasons.append(f"{stuck} 个任务超过 15 分钟未更新")
    if failure_rate is not None and failure_rate >= 0.15:
        level = "bad"
        reasons.append(f"终态失败率 {failure_rate * 100:.1f}%")
    elif failure_rate is not None and failure_rate >= 0.05:
        if level == "ok":
            level = "warn"
        reasons.append(f"终态失败率 {failure_rate * 100:.1f}%")
    if unverified:
        if level == "ok":
            level = "warn"
        reasons.append(f"{unverified} 个 completed 缺少完整 artifact 证据")
    return level, reasons


def _ops_chat_level(report: dict | None) -> tuple[str, list[str]]:
    if report is None:
        return "unknown", ["聊天统计暂不可用"]
    outcomes = report.get("outcomes") or {}
    delivery = report.get("reply_delivery") or {}
    settled = int(report.get("settled_jobs") or 0)
    failed = int(outcomes.get("failed") or 0) + int(outcomes.get("expired") or 0)
    reasons: list[str] = []
    # 有样本时基线是 warn 而非 unknown：客户端 ACK 结构性缺失，证据永远不闭环，
    # 这是"测了但测不全"，不是"没测"。Never claim green.
    level = "warn"
    if settled == 0:
        level = "unknown"
        reasons.append("Hosted V2 chat 通道窗口内无已结算样本（V1/BYOK 聊天不经此通道，不代表没人聊天）")
    else:
        failure_rate = float(failed) / float(settled)
        if failure_rate >= 0.15:
            level = "bad"
        if failure_rate >= 0.05:
            reasons.append(f"chat 终态未成功率 {failure_rate * 100:.1f}%")
    missing = int(delivery.get("completed_without_final_applied") or 0)
    reconciliation = int(delivery.get("final_reconciliation_jobs") or 0)
    if missing:
        level = "bad"
        reasons.append(f"{missing} 个 completed 没有 final reply server-applied 证据")
    if reconciliation:
        level = "bad"
        reasons.append(f"{reconciliation} 个 final reply 需要人工 reconcile")
    if not reasons:
        reasons.append("服务端交付可观测；客户端接收 ACK 尚未采集")
    return level, reasons


def _ops_latency_level(report: dict | None) -> tuple[str, list[str]]:
    if report is None:
        return "unknown", ["延迟统计暂不可用"]
    value = (report.get("latency") or {}).get("server_applied_p95_sec")
    if value is None:
        return "unknown", ["Hosted V2 通道缺少 reply applied 的 p95 样本（V1/BYOK 不经此通道）"]
    seconds = float(value)
    if seconds >= 120:
        return "bad", [f"服务端交付 p95 {seconds:.0f}s"]
    if seconds >= 60:
        return "warn", [f"服务端交付 p95 {seconds:.0f}s"]
    return "ok", []


def _ops_page(
    *,
    active: str,
    title: str,
    subtitle: str,
    hours: int,
    body: str,
) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · Feedling Data Track</title>
<style>{_RUNTIME_PAGE_CSS}</style></head><body><main>
  <span class="ops-kicker">Operations / metadata only</span>
  <h1>{html.escape(title)}</h1>
  <div class="muted">{html.escape(subtitle)}</div>
  {_render_data_track_view_nav(active)}
  {_ops_window_links(active, hours)}
  {body}
</main></body></html>"""


def _render_ops_overview_page(
    imports: dict | None,
    chat: dict | None,
    runtime: dict | None,
    product: dict | None = None,
    usage: dict | None = None,
    *,
    prev_product: dict | None = None,
    prev_usage: dict | None = None,
    within_hours: int,
) -> str:
    # prev_product / prev_usage 是**紧邻的上一个同长度窗口**的同构 payload，
    # 只用来算环比箭头；admin_core 未传（旧签名）或取不到时为 None，
    # _render_delta 对 None 返回空串，页面照常渲染。
    import_level, import_reasons = _ops_import_level(imports)
    chat_level, chat_reasons = _ops_chat_level(chat)
    latency_level, latency_reasons = _ops_latency_level(chat)

    started = int((imports or {}).get("started") or 0)
    completed = int((imports or {}).get("completed") or 0)
    import_failed = int((imports or {}).get("failed") or 0)
    verified = int((imports or {}).get("artifact_verified") or 0)
    admitted = int(((chat or {}).get("outcomes") or {}).get("admitted") or 0)
    applied = int(((chat or {}).get("reply_delivery") or {}).get("final_applied_jobs") or 0)
    p95 = ((chat or {}).get("latency") or {}).get("server_applied_p95_sec")
    pool = (runtime or {}).get("pool") or {}
    live_workers = pool.get("live_workers") if runtime is not None else None
    capacity = pool.get("capacity") if runtime is not None else None
    duplicate_effects = int(
        ((chat or {}).get("reply_delivery") or {}).get("duplicate_final_effect_jobs") or 0
    )
    onboarding = (product or {}).get("onboarding") or {}
    onboarding_covered = bool(onboarding.get("coverage_complete"))
    onboarding_completed = (
        onboarding.get("first_genuine_reply") if onboarding_covered else None
    )
    onboarding_cohort = (
        onboarding.get("cohort_accounts") if onboarding_covered else None
    )
    onboarding_rate = onboarding.get("completion_rate") if onboarding_covered else None
    if onboarding_covered:
        onboarding_value = (
            f"{_fmt_count(onboarding_completed)} / {_fmt_count(onboarding_cohort)}"
        )
        onboarding_label = (
            "窗口注册 cohort → 首次真回复"
            f"（{_fmt_ratio(onboarding_rate)}）"
        )
    else:
        onboarding_value = "未知"
        onboarding_label = "注册 cohort 事件覆盖不完整，未计算完成率"

    usage_total = (usage or {}).get("total")
    usage_known = usage_total is not None
    model_calls = usage_total.get("model_calls") if usage_known else None
    retries = usage_total.get("retries") if usage_known else None
    retry_rate = (
        float(retries) / float(model_calls)
        if retries is not None and model_calls
        else None
    )
    model_active_users = (
        usage_total.get("model_active_users") if usage_known else None
    )
    known_tokens = usage_total.get("total_tokens") if usage_known else None
    active_user_days = usage_total.get("active_user_days") if usage_known else None
    tokens_per_active_user_day = (
        usage_total.get("tokens_per_active_user_day") if usage_known else None
    )

    prev_prod = prev_product or {}
    prev_total = (prev_usage or {}).get("total") or {}
    prev_model_calls = prev_total.get("model_calls")
    prev_retries = prev_total.get("retries")
    prev_retry_rate = (
        float(prev_retries) / float(prev_model_calls)
        if prev_retries is not None and prev_model_calls
        else None
    )

    def evidence(items: list[str]) -> str:
        return "；".join(html.escape(item) for item in items) or "当前证据通过"

    def card(
        *,
        level: str,
        title: str,
        view: str,
        value: str,
        fraction_html: str,
        evidence_html: str,
    ) -> str:
        # 整卡是一个到明细页的链接；href 走既有 helper，admin_key / hours
        # 自动随 query 传播，本页不消费的共享参数照 _ops_window_links 清掉。
        # fraction_html / evidence_html 是本函数内拼好的受信 HTML（其中的
        # 动态值都已经过 _fmt_* / evidence() 的转义）。
        href = _data_track_page_href(
            view=view, hours=within_hours, user_id=None, **_RUNTIME_IGNORED_PARAMS
        )
        return (
            f"<article class='question-card {level}'>"
            f"<a class='question-link' href='{html.escape(href, quote=True)}'>"
            f"<div class='question-top'><h2 class='question-title'>{html.escape(title)}</h2>{_ops_status(level)}</div>"
            f"<div class='question-value'>{html.escape(value)}</div>"
            f"<div class='question-fraction'>{fraction_html}</div>"
            f"<div class='question-evidence'>{evidence_html}</div>"
            "<div class='question-drill'>查看明细 →</div>"
            "</a></article>"
        )

    # 大字标率、分数降级为小字：读者不用心算 41/69 是多少。分母为 0 时显
    # 「无样本」而不是 0%——0% 是测出来的坏消息，无样本是没消息。
    # 导入卡的样本门槛跟 _ops_import_level 用同一把尺：started == 0 才是
    # 「无样本」。
    # 分母必须是**终态**（completed + failed）：失败的导入到不了 completed，
    # 用 verified/completed 会让红卡顶着 90% 的大数字（prod 2026-08-07 实景：
    # 9/10=90% 配 23.1% 终态失败率）——回答「导入成功了吗」的分母是所有
    # 有结局的导入，不是所有善终的导入。
    import_terminal = completed + import_failed
    # started>0 且 terminal==0（全部还在跑/卡住）：率不可算，显「—」交给
    # evidence 行的卡住理由说话——0.0% 是测出来的坏消息，这里还没有终态。
    import_rate = (
        float(verified) / float(import_terminal) if import_terminal else None
    )
    chat_rate = float(applied) / float(admitted) if admitted else None
    cards = "".join([
        card(
            level=import_level,
            title="用户导入成功了吗？",
            view="imports",
            value=(
                (_fmt_ratio(import_rate) if import_terminal else "—")
                if started else "无样本"
            ),
            fraction_html=(
                f"{_fmt_count(verified)} / {_fmt_count(import_terminal)}"
                " · artifact 证据通过 / 终态（completed+failed）"
            ),
            evidence_html=evidence(import_reasons),
        ),
        card(
            level=chat_level,
            title="用户真的收到回复了吗？",
            view="chat",
            value=_fmt_ratio(chat_rate) if admitted else "无样本",
            fraction_html=(
                f"{_fmt_count(applied)} / {_fmt_count(admitted)}"
                " · 服务端 final reply applied / admitted Runtime turns"
            ),
            evidence_html=(
                "客户端接收或已读 ACK：<b>不可用</b>。" + evidence(chat_reasons)
            ),
        ),
        card(
            level=latency_level,
            title="回复有多慢？",
            view="latency",
            value=_fmt_duration_sec(p95),
            fraction_html="发送到 final reply 服务端 applied 的 p95",
            evidence_html=evidence(latency_reasons),
        ),
        card(
            # 测到重复候选就是证据，必须转黄并把数字放进大字——灰色「不可
            # 判定」只留给真的一个候选都没测到的窗口；provider 侧是否真扣
            # 了两次费仍不可判定，这句诚实话保留在证据行里。
            level="warn" if duplicate_effects else "unknown",
            title="有重复调用或重复费用吗？",
            view="chat",
            value=(
                f"重复候选 {_fmt_count(duplicate_effects)}"
                if duplicate_effects else "不可判定"
            ),
            fraction_html=(
                f"final reply effect 重复候选 {_fmt_count(duplicate_effects)} 个 job"
                + (" · provider 扣费不可判定" if duplicate_effects else "")
            ),
            evidence_html=(
                "这<b>不等于</b> provider 重复扣费。真实 provider attempt / "
                "possibly-billed / authoritative cost 要等 P0-B 账本。"
            ),
        ),
    ])

    body = f"""
    <section class="ops-questions" aria-label="运营核心问题">{cards}</section>
    <div class="definition-line">四张卡的颜色：<b class="ok">绿</b>=证据闭环且正常；<b class="warn">黄</b>=有证据且需注意，或证据结构不完整（如缺客户端 ACK）；<b class="muted">灰</b>=证据不足、不可判定；<b class="bad">红</b>=异常。当前客户端 ACK 与 provider 请求账本尚未闭环，所以不会用 0 或绿色冒充“确认没有问题”。</div>
    <h2>产品还有人用吗？ <span class="h2-sub">产品活跃与 Onboarding</span></h2>
    <section class="metrics">
      {_render_metric('窗口内 App 活跃账号', _fmt_count((product or {}).get('window_app_users')), hint='窗口内至少上报一次 app_session_end 的去重账号，保守下限', delta=_render_delta((product or {}).get('window_app_users'), prev_prod.get('window_app_users')))}
      {_render_metric('App sessions', _fmt_count((product or {}).get('app_sessions')), hint='窗口内 app_session_end 事件条数', delta=_render_delta((product or {}).get('app_sessions'), prev_prod.get('app_sessions')))}
      {_render_metric('窗口新注册账号', _fmt_count((product or {}).get('new_registered_accounts')), hint='窗口内注册且注册时间可安全解析的账号数', delta=_render_delta((product or {}).get('new_registered_accounts'), prev_prod.get('new_registered_accounts')))}
      {_render_metric(onboarding_label, onboarding_value, hint='窗口注册 cohort 截至本页生成时已产生首次非 fallback 真回复的比例；不同窗口的 cohort 不可比，不做环比')}
    </section>
    <details class="note-box"><summary>口径说明</summary><b>产品口径：</b>“窗口内 App 活跃账号”是所选滚动窗口内至少上报一次 <code>app_session_end</code> 的去重账号。iOS 前台 session 可能被系统杀掉而漏报，所以这是保守下限；24 小时档也不是北京自然日 DAU，7 天和 30 天更不是把每日 DAU 相加。自然日趋势看「DAU」。Onboarding 分母只取同一窗口内注册且时间可安全解析的账号，完成定义为截至本页生成时已产生首次非 fallback 真回复。{'<b>当前 cohort 事件覆盖不完整，因此完成率显示未知。</b>' if not onboarding_covered else ''}</details>
    <h2>Hosted V2 在烧多少 Token？ <span class="h2-sub">Hosted V2 模型用量</span></h2>
    <section class="metrics">
      {_render_metric('V2 模型活跃账号', _fmt_count(model_active_users), hint='v2_turn_metrics 中实际发生过模型调用的去重 user_id', delta=_render_delta(model_active_users, prev_total.get('model_active_users')))}
      {_render_metric('V2 模型活跃用户日', _fmt_count(active_user_days), hint='按北京时间把 user_id × 日期去重', delta=_render_delta(active_user_days, prev_total.get('active_user_days')))}
      {_render_metric('V2 turns', _fmt_count(usage_total.get('turns') if usage_known else None), hint='窗口内 Hosted V2 会话回合数', delta=_render_delta(usage_total.get('turns') if usage_known else None, prev_total.get('turns')))}
      {_render_metric('模型调用', _fmt_count(model_calls), hint='provider 模型调用次数，含重试', delta=_render_delta(model_calls, prev_model_calls, good_when='neutral'))}
      {_render_metric('重试 / 调用', f"{_fmt_count(retries)} / {_fmt_ratio(retry_rate)}", hint='重试次数与重试占调用比；重试同样烧钱', delta=_render_delta(retry_rate, prev_retry_rate, good_when='down'))}
      {_render_metric('窗口 Token 总量（已知下限）', _fmt_tokens_compact(known_tokens), hint='provider 已上报 usage 的 token 合计，缺报只降覆盖率、不伪装成 0', delta=_render_delta(known_tokens, prev_total.get('total_tokens'), good_when='neutral'))}
      {_render_metric('平均每个 V2 活跃用户日 Token', _fmt_tokens_compact(round(tokens_per_active_user_day) if tokens_per_active_user_day is not None else None), hint='已知 Token ÷ V2 活跃用户日，不是全产品人均', delta=_render_delta(tokens_per_active_user_day, prev_total.get('tokens_per_active_user_day'), good_when='neutral'))}
      {_render_metric('Token usage 覆盖率', _fmt_ratio(usage_total.get('usage_coverage') if usage_known else None), hint='有 usage 上报的模型调用占比')}
    </section>
    <details class="note-box"><summary>口径说明</summary><b>V2 口径：</b>模型活跃账号来自本实例 <code>v2_turn_metrics</code> 中实际发生过模型调用的去重 user_id；活跃用户日按北京时间把 user_id × 日期去重，和上面的产品 App 活跃账号不是同一分母。Token 是 provider 已上报 usage 的已知下限，缺报会降低覆盖率，不会被伪装成 0；“平均每个活跃用户日 Token”是 Hosted V2 模型活跃口径，不能冒充全产品 App DAU 的人均用量。离线 self-host 与非 V2 路径不在这组数里。</details>
    <h2>当前承载扛得住吗？ <span class="h2-sub">当前承载</span></h2>
    <section class="metrics">
      {_render_metric('存活 worker', _fmt_count(live_workers), hint='当前心跳存活的 runtime worker 数；此刻状态，不随窗口变化')}
      {_render_metric('可执行容量', _fmt_count(capacity), hint='worker 并发可执行槽位合计')}
      {_render_metric('在飞 job', _fmt_count(pool.get('inflight') if runtime is not None else None), hint='此刻正在执行的 job 数')}
      {_render_metric('排队 pending', _fmt_count(pool.get('pending') if runtime is not None else None), hint='此刻排队待执行的 job 数')}
      {_render_metric('最老 pending', _fmt_duration_sec(pool.get('oldest_pending_age_sec') if runtime is not None else None), hint='最老排队 job 已等待的时长；健康时为空')}
    </section>
    <details class="note-box"><summary>下一步看哪里</summary><b>下一步看哪里：</b>导入证据与卡住任务看「记忆导入」；消息→Runtime→reply effect 看「聊天可靠性」；排队/执行/服务端交付分段看「延迟」；Token、provider、model 与重试看「Token 与模型」。本总览只覆盖本实例 Hosted Runtime V2 与 Genesis ledger，不代表离线 self-host 全量。</details>
    """
    return _ops_page(
        active="overview",
        title="运营总览",
        subtitle=f"最近 {within_hours} 小时 · 四个问题，一眼看证据是否闭环。",
        hours=within_hours,
        body=body,
    )


def _ops_time(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _render_imports_page(report: dict | None, *, within_hours: int) -> str:
    if report is None:
        body = "<div class='note-box'><b>导入统计暂时取不到。</b>页面不会把未知渲染成 0；其他 Admin 视图不受影响。</div>"
        return _ops_page(active="imports", title="记忆导入", subtitle="Genesis import ledger", hours=within_hours, body=body)

    level, reasons = _ops_import_level(report)
    recent_rows = []
    status_labels = {
        "done": "完成", "failed": "失败", "processing": "处理中",
        "uploaded": "待处理", "awaiting_resident": "等待 resident",
        "created": "已创建",
    }
    for row in report.get("recent_jobs") or []:
        user_id = str(row.get("user_id") or "unknown")
        qs = _data_track_qs(view=None, hours=None, offset=None, user_id=None)
        suffix = f"?{qs}" if qs else ""
        user_href = f"/admin/data-track/users/{quote(user_id, safe='')}{suffix}"
        status = str(row.get("status") or "unknown")
        is_stuck = (
            status not in {"done", "failed"}
            and float(row.get("age_since_update_sec") or 0) >= 900
        )
        if status == "done" and row.get("artifact_evidence_complete"):
            evidence_text = "通过"
            evidence_cls = "evidence-ok"
        elif status == "done":
            evidence_text = "终态完成，证据不足"
            evidence_cls = "evidence-warn"
        elif status == "failed":
            evidence_text = "失败"
            evidence_cls = "evidence-bad"
        else:
            evidence_text = "处理中"
            evidence_cls = "evidence-warn" if is_stuck else ""
        recent_rows.append(
            "<tr>"
            f"<td><a href='{html.escape(user_href, quote=True)}'><code>{html.escape(user_id)}</code></a></td>"
            f"<td><code>{html.escape(str(row.get('job_id') or ''))}</code></td>"
            f"<td>{html.escape(str(row.get('import_mode') or row.get('source_kind') or 'unknown'))}</td>"
            f"<td>{html.escape(status_labels.get(status, status))}{' · 超过15m未更新' if is_stuck else ''}</td>"
            f"<td class='{evidence_cls}'>{evidence_text}</td>"
            f"<td>{_fmt_count(row.get('memory_action_count'))} / {'有' if row.get('has_identity_evidence') else '无'}</td>"
            f"<td><code>{html.escape(str(row.get('error_code') or '—'))}</code></td>"
            f"<td>{html.escape(_ops_time(row.get('created_at')))}</td>"
            f"<td>{html.escape(_ops_time(row.get('updated_at')))}</td>"
            "</tr>"
        )
    failure_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(str(row.get('error_code') or 'other'))}</code></td>"
        f"<td>{_fmt_count(row.get('count'))}</td></tr>"
        for row in report.get("failure_reasons") or []
    )
    reason_text = "；".join(html.escape(item) for item in reasons) or "当前证据通过"
    body = f"""
    <div class="overall-summary">结论：{_ops_status(level)} · {reason_text}</div>
    <section class="metrics">
      {_render_metric('开始导入', _fmt_count(report.get('started')))}
      {_render_metric('Terminal done', _fmt_count(report.get('completed')))}
      {_render_metric('Artifact 证据通过', _fmt_count(report.get('artifact_verified')))}
      {_render_metric('Done 但证据不足', _fmt_count(report.get('completed_unverified')))}
      {_render_metric('失败', _fmt_count(report.get('failed')))}
      {_render_metric('仍在处理中', _fmt_count(report.get('processing')))}
      {_render_metric('卡住 >15m', _fmt_count(report.get('stuck_over_15m')))}
      {_render_metric('Terminal 成功率', _fmt_ratio(report.get('terminal_success_rate')))}
      {_render_metric('Artifact 证据通过率', _fmt_ratio(report.get('artifact_verified_rate')))}
      {_render_metric('完成 p50', _fmt_duration_sec(report.get('p50_complete_sec')))}
      {_render_metric('完成 p95', _fmt_duration_sec(report.get('p95_complete_sec')))}
    </section>
    <div class="note-box"><b>两种成功不能混：</b>Terminal done 只表示 reducer 把 job 置为完成；Artifact 证据通过则按任务模式检查 durable ledger 中的 identity / memory 写入证据。当前仍不是独立的 artifact-attempt 账本：合法的 nameless / fresh-start 完成可能被保守标为“证据不足”，不会假绿。页面不读取导入正文、模型输出或任意 exception 尾部。</div>
    <h2>最近任务</h2>
    <div class="table-wrap"><table><thead><tr><th>User</th><th>Job</th><th>模式</th><th>状态</th><th>Artifact 证据</th><th>Memory / Identity</th><th>安全失败码</th><th>创建 UTC</th><th>更新 UTC</th></tr></thead>
    <tbody>{''.join(recent_rows) if recent_rows else "<tr><td colspan='9' class='muted'>窗口内无任务。</td></tr>"}</tbody></table></div>
    <h2>失败原因 Top</h2>
    <div class="table-wrap"><table><thead><tr><th>安全失败码</th><th>次数</th></tr></thead><tbody>{failure_rows or "<tr><td colspan='2' class='muted'>窗口内无失败。</td></tr>"}</tbody></table></div>
    """
    return _ops_page(active="imports", title="记忆导入", subtitle="任务终态与 artifact 证据分开看", hours=within_hours, body=body)


def _render_chat_reliability_page(report: dict | None, *, within_hours: int) -> str:
    if report is None:
        body = "<div class='note-box'><b>聊天可靠性统计暂时取不到。</b>未知不会显示成 0。</div>"
        return _ops_page(active="chat", title="聊天可靠性", subtitle="Hosted Runtime V2 server-side evidence", hours=within_hours, body=body)
    level, reasons = _ops_chat_level(report)
    outcomes = report.get("outcomes") or {}
    delivery = report.get("reply_delivery") or {}
    failure_delivery = report.get("failure_delivery") or {}
    failure_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(str(row.get('code') or 'runtime_failed'))}</code></td>"
        f"<td>{_fmt_count(row.get('count'))}</td></tr>"
        for row in report.get("failure_reasons") or []
    )
    recent_rows = []
    for row in report.get("recent_jobs") or []:
        user_id = str(row.get("user_id") or "unknown")
        qs = _data_track_qs(view=None, hours=None, offset=None, user_id=None)
        suffix = f"?{qs}" if qs else ""
        href = f"/admin/data-track/users/{quote(user_id, safe='')}{suffix}"
        recent_rows.append(
            "<tr>"
            f"<td><a href='{html.escape(href, quote=True)}'><code>{html.escape(user_id)}</code></a></td>"
            f"<td>{_fmt_count(row.get('job_id'))}</td>"
            f"<td>{html.escape(str(row.get('status') or 'unknown'))}</td>"
            f"<td>{html.escape(str(row.get('final_effect_status') or 'missing'))}</td>"
            f"<td>{html.escape(str(row.get('provider') or '—'))} / {html.escape(str(row.get('model') or '—'))}</td>"
            f"<td>{_fmt_count(row.get('model_calls'))} / {_fmt_count(row.get('retries'))}</td>"
            f"<td><code>{html.escape(str(row.get('last_error') or '—'))}</code></td>"
            f"<td>{html.escape(_ops_time(row.get('created_at')))}</td>"
            "</tr>"
        )
    reason_text = "；".join(html.escape(item) for item in reasons)
    body = f"""
    <div class="overall-summary">结论：{_ops_status(level)} · {reason_text}</div>
    <div class="funnel-line" role="list" aria-label="聊天生命周期">
      <div class="funnel-step"><span>消息进入 Runtime</span><b>{_fmt_count(outcomes.get('admitted'))}</b><small>chat agent_jobs admitted</small></div>
      <div class="funnel-step"><span>开始处理</span><b>{_fmt_count(outcomes.get('started'))}</b><small>claimed_at / started_at 有记录</small></div>
      <div class="funnel-step"><span>生成 final effect</span><b>{_fmt_count(delivery.get('final_effect_jobs'))}</b><small>服务端 durable effect</small></div>
      <div class="funnel-step"><span>服务端 applied</span><b>{_fmt_count(delivery.get('final_applied_jobs'))}</b><small>不是客户端 ACK</small></div>
    </div>
    <section class="metrics">
      {_render_metric('已结算 chat', _fmt_count(report.get('settled_jobs')))}
      {_render_metric('Admitted → final reply 服务端 applied', _fmt_ratio(report.get('server_final_reply_applied_rate')))}
      {_render_metric('终态完成率', _fmt_ratio(report.get('terminal_completion_rate')))}
      {_render_metric('Completed', _fmt_count(outcomes.get('completed')))}
      {_render_metric('Failed', _fmt_count(outcomes.get('failed')))}
      {_render_metric('Expired', _fmt_count(outcomes.get('expired')))}
      {_render_metric('仍在飞', _fmt_count(outcomes.get('in_flight')))}
      {_render_metric('Final pending', _fmt_count(delivery.get('final_pending_jobs')))}
      {_render_metric('需 reconcile', _fmt_count(delivery.get('final_reconciliation_jobs')))}
      {_render_metric('Completed 无 applied', _fmt_count(delivery.get('completed_without_final_applied')))}
      {_render_metric('失败 fallback 已投递', _fmt_count(failure_delivery.get('fallback_reply_delivered')))}
      {_render_metric('失败 fallback 待投递', _fmt_count(failure_delivery.get('fallback_reply_pending')))}
    </section>
    <div class="note-box"><b>两条率不能混：</b>“Admitted → final reply 服务端 applied”以窗口内全部 chat agent_jobs 为同一 cohort，回答进入 Runtime 的 turn 有多少已经落地最终回复；“终态完成率”只在 completed / failed / expired 终态中计算 completed。最终回复只认明确 final effect，或带 <code>reply_through_seq</code> 的 legacy reply；普通中间 reply 不计。服务端 applied 比“模型 API 200”更接近用户结果，但仍<b>不等于设备收到或用户已读</b>；客户端 delivery ACK 当前不可用，因此页面不会给“用户真的收到”绿灯。同一用户在单飞回合中追加的多条消息会折入一个 job，所以 admitted 是 turns，不是原始消息条数。</div>
    <h2>失败原因 Top</h2><div class="table-wrap"><table><thead><tr><th>失败码</th><th>次数</th></tr></thead><tbody>{failure_rows or "<tr><td colspan='2' class='muted'>窗口内无失败。</td></tr>"}</tbody></table></div>
    <h2>最近 chat jobs</h2><div class="table-wrap"><table><thead><tr><th>User</th><th>Job</th><th>Job 状态</th><th>Final effect</th><th>Provider / Model</th><th>Calls / retries</th><th>失败码</th><th>创建 UTC</th></tr></thead><tbody>{''.join(recent_rows) if recent_rows else "<tr><td colspan='8' class='muted'>窗口内无 chat。</td></tr>"}</tbody></table></div>
    """
    return _ops_page(active="chat", title="聊天可靠性", subtitle="消息进入、处理、生成、服务端交付四段证据", hours=within_hours, body=body)


def _render_latency_page(report: dict | None, *, within_hours: int) -> str:
    if report is None:
        body = "<div class='note-box'><b>延迟统计暂时取不到。</b>未知不会显示成 0。</div>"
        return _ops_page(active="latency", title="回复延迟", subtitle="Queue → processing → server applied", hours=within_hours, body=body)
    level, reasons = _ops_latency_level(report)
    latency = report.get("latency") or {}

    def stage(label: str, prefix: str, note: str) -> str:
        return (
            "<div class='funnel-step'>"
            f"<span>{html.escape(label)}</span>"
            f"<b>{_fmt_duration_sec(latency.get(prefix + '_p95_sec'))}</b>"
            f"<small>p50 {_fmt_duration_sec(latency.get(prefix + '_p50_sec'))} · "
            f"p99 {_fmt_duration_sec(latency.get(prefix + '_p99_sec'))}<br>{html.escape(note)}</small>"
            "</div>"
        )

    model_rows = []
    for row in report.get("model_breakdown") or []:
        turns = int(row.get("turns") or 0)
        failed = int(row.get("failed_turns") or 0)
        model_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('provider') or 'unknown'))}</td>"
            f"<td>{html.escape(str(row.get('model') or 'unknown'))}</td>"
            f"<td>{_fmt_count(turns)}</td><td>{_fmt_count(row.get('model_calls'))}</td>"
            f"<td>{_fmt_count(row.get('retries'))}</td>"
            f"<td>{_fmt_count(failed)} / {_fmt_ratio(float(failed) / float(turns) if turns else None)}</td>"
            f"<td>{_fmt_duration_sec(float(row.get('p50_ms')) / 1000 if row.get('p50_ms') is not None else None)}</td>"
            f"<td>{_fmt_duration_sec(float(row.get('p95_ms')) / 1000 if row.get('p95_ms') is not None else None)}</td>"
            f"<td>{_fmt_duration_sec(float(row.get('p99_ms')) / 1000 if row.get('p99_ms') is not None else None)}</td>"
            "</tr>"
        )
    reason_text = "；".join(html.escape(item) for item in reasons) or "当前 p95 在阈值内"
    body = f"""
    <div class="overall-summary">结论：{_ops_status(level)} · {reason_text}</div>
    <div class="funnel-line">
      {stage('排队', 'queue', 'created → claimed/started')}
      {stage('模型与工具处理', 'processing', 'started → job terminal')}
      {stage('整轮 job', 'turn', 'created → job terminal')}
      {stage('用户侧最接近值', 'server_applied', 'created → final reply server applied')}
    </div>
    <div class="note-box"><b>延迟口径：</b>p50 / p95 / p99 都来自窗口内有对应时间戳的样本。最后一段是目前最接近“用户等待”的服务端证据，但仍不包含设备网络与客户端渲染时间；没有 client ACK 时不能称为真正 send-to-receive。</div>
    <h2>Provider / Model whole-turn</h2>
    <div class="muted">下表 latency_ms 来自 whole-turn metrics；它不是分段延迟，也不是 provider 单次请求延迟。用于比较 provider / model 趋势。</div>
    <div class="table-wrap"><table><thead><tr><th>Provider</th><th>Model</th><th>Turns</th><th>Calls</th><th>Retries</th><th>Failed</th><th>p50</th><th>p95</th><th>p99</th></tr></thead><tbody>{''.join(model_rows) if model_rows else "<tr><td colspan='9' class='muted'>窗口内无 whole-turn metrics。</td></tr>"}</tbody></table></div>
    """
    return _ops_page(active="latency", title="回复延迟", subtitle="把排队、处理、整轮和服务端交付拆开", hours=within_hours, body=body)


def _render_admin_login_page(error: bool = False, next_url: str = "/admin/data-track") -> str:
    """Password-gate login page for the admin dashboard. Posts to /admin/login,
    which validates FEEDLING_ADMIN_PASSWORD and sets the signed admin_session
    cookie (see admin.routes_asgi). Styled to match the data-track dashboard."""
    err_html = (
        "<div class='err'>密码不对，再试一次。</div>" if error else ""
    )
    safe_next = html.escape(next_url or "/admin/data-track", quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedling Data Track · 登录</title>
  <style>
    :root {{ color-scheme: light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; display:flex; min-height:100vh; align-items:center; justify-content:center; }}
    .box {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:28px 26px; width:320px; box-shadow:0 2px 14px rgba(0,0,0,.05); }}
    h1 {{ font-size:19px; margin:0 0 4px; }}
    .sub {{ color:var(--muted); font-size:13px; margin:0 0 18px; }}
    input[type=password] {{ width:100%; box-sizing:border-box; padding:11px 12px; font-size:15px; border:1px solid var(--line); border-radius:8px; background:#fff; }}
    button {{ width:100%; margin-top:12px; padding:11px; font-size:15px; font-weight:600; color:#fff; background:var(--accent); border:0; border-radius:8px; cursor:pointer; }}
    .err {{ color:#b7352b; font-size:13px; margin-bottom:10px; }}
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


def _usage_page_href(
    query: admin_usage.UsageQuery | None = None,
    *,
    now_utc: datetime | None = None,
    **updates,
) -> str:
    """Build a Usage link from its normalized whitelist, never raw args."""

    if query is None:
        query = admin_usage.parse_usage_query(request.args, now_utc=now_utc)
    params = {
        "preset": query.preset,
        "timezone": query.timezone,
        "completeness": query.completeness,
    }
    if query.start_date is not None:
        params["start_date"] = query.start_date
    if query.end_date is not None:
        params["end_date"] = query.end_date
    for key in ("user_id", "lane", "provider", "model"):
        value = getattr(query, key)
        if value is not None:
            params[key] = value

    allowed = {
        "preset", "start_date", "end_date", "timezone", "user_id", "lane",
        "provider", "model", "completeness",
    }
    for key, value in updates.items():
        if key not in allowed:
            continue
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = str(value)

    normalized = admin_usage.parse_usage_query(
        params,
        now_utc=query.end_at_utc,
    )
    canonical = {
        "view": "usage",
        "preset": normalized.preset,
        "timezone": normalized.timezone,
        "completeness": normalized.completeness,
    }
    if normalized.start_date is not None:
        canonical["start_date"] = normalized.start_date
    if normalized.end_date is not None:
        canonical["end_date"] = normalized.end_date
    for key in ("user_id", "lane", "provider", "model"):
        value = getattr(normalized, key)
        if value is not None:
            canonical[key] = value

    try:
        offset = int(updates.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    if offset > 0:
        canonical["offset"] = str(min(offset, 1_000_000))
    sort = str(updates.get("sort") or "").strip().lower()
    if sort in {"tokens", "calls", "retries", "recent"}:
        canonical["sort"] = sort
        direction = str(updates.get("dir") or "desc").strip().lower()
        canonical["dir"] = direction if direction in {"asc", "desc"} else "desc"
    admin_key = str(request.args.get("admin_key") or "").strip()
    if admin_key:
        canonical["admin_key"] = admin_key
    return f"/admin/data-track?{urlencode(canonical)}"


def _usage_float(value, *, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _usage_timestamp(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _usage_filter_options(values, selected: str | None) -> str:
    normalized = [str(value) for value in (values or [])]
    if selected and selected not in normalized:
        normalized.insert(0, selected)
    options = ["<option value=''>All</option>"]
    for value in normalized:
        marker = " selected" if value == selected else ""
        escaped = html.escape(value, quote=True)
        options.append(f"<option value='{escaped}'{marker}>{escaped}</option>")
    return "".join(options)


def _usage_user_href(
    query: admin_usage.UsageQuery,
    user_id: str,
    **updates,
) -> str:
    canonical = _usage_page_href(query, user_id=user_id, **updates)
    _, _, query_string = canonical.partition("?")
    path = f"/admin/data-track/users/{quote(user_id, safe='')}"
    return f"{path}?{query_string}" if query_string else path


def _usage_sorted_users(report: dict) -> tuple[list[dict], int, int]:
    source_rows = report.get("users")
    rows = list(source_rows or [])
    sort = str(request.args.get("sort") or "tokens").strip().lower()
    if sort not in {"tokens", "calls", "retries", "recent"}:
        sort = "tokens"
    direction = str(request.args.get("dir") or "desc").strip().lower()
    if direction not in {"asc", "desc"}:
        direction = "desc"

    def sortable(row: dict):
        if sort == "recent":
            raw = row.get("last_model_call_at")
            if isinstance(raw, datetime):
                if raw.tzinfo is None:
                    raw = raw.replace(tzinfo=timezone.utc)
                return raw.timestamp()
            return None
        key = {"tokens": "total_tokens", "calls": "model_calls", "retries": "retries"}[sort]
        value = row.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    # Sort the deterministic user_id tie-break first, then stably sort only the
    # known metric rows.  ``reverse=True`` on a compound key would reverse the
    # user_id tie-break as well; sentinel numbers would also put unknown values
    # first for ascending order.  Unknown is always last in either direction.
    rows.sort(key=lambda row: str(row.get("user_id") or ""))
    known = [row for row in rows if sortable(row) is not None]
    unknown = [row for row in rows if sortable(row) is None]
    known.sort(key=sortable, reverse=direction == "desc")
    rows = known + unknown
    try:
        offset = max(int(request.args.get("offset") or 0), 0)
    except (TypeError, ValueError):
        offset = 0
    if not rows:
        offset = 0
    elif offset >= len(rows):
        offset = ((len(rows) - 1) // 100) * 100
    return rows[offset:offset + 100], offset, len(rows)


def _usage_focus_day(
    report: dict,
    query: admin_usage.UsageQuery,
) -> tuple[dict | None, str, bool]:
    """Return the selected window's most relevant local-day row.

    Rolling presets focus the display timezone's current partial day. Custom
    ranges focus their inclusive end date. A missing target falls back to the
    latest returned day so partial report degradation remains useful instead
    of turning the entire executive summary into dashes.
    """
    rows = sorted(
        (row for row in (report.get("daily") or []) if row.get("local_day")),
        key=lambda row: str(row.get("local_day")),
    )
    if query.preset == "custom" and query.end_date:
        target_day = query.end_date
        partial = False
    else:
        try:
            display_tz = ZoneInfo(query.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            display_tz = _SHANGHAI_TZ
        target_day = query.end_at_utc.astimezone(display_tz).date().isoformat()
        partial = True
    for row in rows:
        if str(row.get("local_day")) == target_day:
            return row, target_day, partial
    if rows:
        latest = rows[-1]
        return latest, str(latest.get("local_day")), False
    return None, target_day, partial


def _usage_rate_count(value) -> str:
    """Human-scale whole-token rate for operator-facing equations."""
    if value is None:
        return "—"
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "—"


def _render_usage_trend(rows: list[dict], *, focus_day: str) -> str:
    """Compact, dependency-free daily ledger for the latest fourteen rows."""
    ordered = sorted(
        (row for row in rows if row.get("local_day")),
        key=lambda row: str(row.get("local_day")),
    )[-14:]
    if not ordered:
        return "<div class='usage-empty'>所选窗口没有每日用量。</div>"
    max_tokens = max((int(row.get("total_tokens") or 0) for row in ordered), default=0)
    rendered = []
    for row in ordered:
        day = str(row.get("local_day") or "")
        total = row.get("total_tokens")
        numeric_total = int(total or 0)
        width = (numeric_total / max_tokens * 100) if max_tokens else 0
        active = int(row.get("model_active_users") or 0)
        per_dau = row.get("tokens_per_active_user_day")
        cls = " trend-row focus" if day == focus_day else " trend-row"
        rendered.append(
            f"<div class='{cls.strip()}' role='row'>"
            f"<time role='cell' datetime='{html.escape(day, quote=True)}'>{html.escape(day[5:] or day)}</time>"
            "<div class='trend-track' role='cell'>"
            f"<span style='width:{width:.1f}%'></span></div>"
            f"<b role='cell'>{_fmt_tokens_compact(total)}</b>"
            f"<span role='cell'>DAU {active:,}</span>"
            f"<span role='cell'>{_usage_rate_count(per_dau)} 已知 / DAU</span>"
            "</div>"
        )
    return (
        "<div class='trend-ledger' role='table' aria-label='每日 V2 Token 与模型 DAU'>"
        "<div class='trend-head' role='row'><span role='columnheader'>日期</span>"
        "<span role='columnheader'>相对 Token</span><span role='columnheader'>已知 Token</span>"
        "<span role='columnheader'>V2 模型 DAU</span><span role='columnheader'>已知 Token / DAU</span></div>"
        + "".join(rendered)
        + "</div>"
    )


def _render_usage_page(
    report: dict,
    query: admin_usage.UsageQuery,
    *,
    drilldown_user_id: str | None = None,
) -> str:
    overview = report.get("overview") or {}
    averages = report.get("averages") or {}
    coverage = report.get("coverage") or {}
    filters_available = report.get("filters") is not None
    filters = report.get("filters") or {}
    dimension_filtered = bool(
        query.lane
        or query.provider
        or query.model
        or query.completeness != "all"
    )
    daily_available = report.get("daily") is not None
    daily = list(report.get("daily") or [])
    focus, focus_day, focus_is_partial = _usage_focus_day(report, query)
    if focus is None:
        # An empty, successfully-read daily section is a confirmed zero.  A
        # missing section is unknown and must stay a dash rather than quietly
        # presenting a synthetic DAU of zero.
        focus_tokens = 0 if daily_available else None
        focus_dau = 0 if daily_available else None
        focus_per_dau = None
        focus_coverage = None
    else:
        focus_tokens = focus.get("total_tokens")
        focus_dau = int(focus.get("model_active_users") or 0)
        focus_per_dau = focus.get("tokens_per_active_user_day")
        focus_coverage = focus.get("usage_coverage")
    # DAU is a local-calendar-day metric.  Do not divide active user-days by
    # elapsed seconds / 86400: rolling windows can span N+1 local date buckets,
    # and DST custom days can be 23 or 25 hours.  The report returns every local
    # day (including zero rows), so the arithmetic mean of those DAU buckets is
    # the honest operator-facing "average daily" value.
    window_avg_dau = (
        sum(int(row.get("model_active_users") or 0) for row in daily)
        / len(daily)
        if daily_available and daily
        else (0.0 if daily_available else None)
    )
    focus_label = (
        f"今天 {focus_day} · 进行中"
        if focus_is_partial
        else f"数据日 {focus_day}"
    )
    active_dimensions = [
        f"{name}={value}"
        for name, value in (
            ("lane", query.lane),
            ("provider", query.provider),
            ("model", query.model),
            ("completeness", query.completeness if query.completeness != "all" else None),
        )
        if value
    ]
    filter_scope = (
        " · 当前筛选：" + " · ".join(active_dimensions)
        if active_dimensions
        else ""
    )
    pulse_chips = [
        "Hosted V2 only",
        "已知 Token 下界",
        query.timezone,
        f"上报覆盖 {_fmt_ratio(focus_coverage)}",
        *active_dimensions,
    ]
    pulse_chip_html = "".join(
        f"<span>{html.escape(str(label))}</span>" for label in pulse_chips
    )
    usage_pulse = f"""
  <section class='usage-pulse' aria-labelledby='usage-pulse-title'>
    <div class='pulse-heading'>
      <div><span class='eyebrow'>{html.escape(focus_label)}</span>
      <h2 id='usage-pulse-title'>每位 V2 模型 DAU 的已知 Token 用量</h2></div>
      <div class='scope-chips'>{pulse_chip_html}</div>
    </div>
    <div class='pulse-equation' aria-label='{html.escape(f"{focus_label} 已知 Token 除以 V2 模型 DAU", quote=True)}'>
      <div class='equation-term'><small>已知 V2 Token</small><strong>{_fmt_count(focus_tokens)}</strong></div>
      <span class='equation-sign'>÷</span>
      <div class='equation-term'><small>V2 模型 DAU</small><strong>{_fmt_count(focus_dau)}</strong></div>
      <span class='equation-sign'>=</span>
      <div class='equation-result'><strong>{_usage_rate_count(focus_per_dau)}</strong><small>已知 Token / DAU（下界）</small></div>
    </div>
    <div class='pulse-foot'>
      <span><b>V2 模型 DAU</b> = 当天至少有一次 model call、按 user_id 去重的 Hosted V2 账号；重装旧号不会合并。</span>
      <span>它不是“打开 App”的产品 DAU，产品 DAU 请看 <a href='{html.escape(_data_track_page_href(view="dau", user_id=None, offset=0), quote=True)}'>日活与时长</a>。{html.escape(filter_scope)}</span>
    </div>
  </section>
  <section class='window-ledger' aria-label='所选窗口加权平均'>
    <div><span>窗口已知 Token</span><b>{_fmt_count(overview.get('total_tokens'))}</b></div>
    <div><span>窗口活跃用户日</span><b>{_fmt_count(overview.get('active_user_days'))}</b></div>
    <div><span>窗口自然日平均 V2 DAU</span><b>{_usage_float(window_avg_dau)}</b></div>
    <div><span>加权已知 Token / DAU</span><b>{_usage_rate_count(averages.get('tokens_per_active_user_day'))}</b></div>
  </section>"""

    current_app_users = overview.get("current_app_users")
    current_hosted_v2_users = overview.get("current_hosted_v2_users")
    current_v2_coverage = overview.get("current_hosted_v2_coverage")
    lane_source = report.get("lanes")
    lanes_available = lane_source is not None
    lanes = list(lane_source or [])

    def summed_lane_tokens(rows: list[dict]) -> int | None:
        if not lanes_available:
            return None
        if not rows:
            return 0
        values = [row.get("total_tokens") for row in rows]
        if any(value is None for value in values):
            return None
        return sum(int(value) for value in values)

    chat_tokens = summed_lane_tokens([
        row for row in lanes if str(row.get("lane") or "unknown") == "chat"
    ])
    background_tokens = summed_lane_tokens([
        row for row in lanes if str(row.get("lane") or "unknown") != "chat"
    ])
    split_total = (
        chat_tokens + background_tokens
        if chat_tokens is not None and background_tokens is not None
        else None
    )
    background_share = (
        float(background_tokens) / split_total
        if split_total
        else None
    )

    def coverage_node(label: str, value: str, detail: str) -> str:
        return (
            "<div class='coverage-node'>"
            f"<div class='coverage-label'>{html.escape(label)}</div>"
            f"<div class='coverage-value'>{html.escape(value)}</div>"
            f"<div class='coverage-detail'>{html.escape(detail)}</div>"
            "</div>"
        )

    coverage_groups = (
        "<div class='coverage-groups'>"
        "<section class='coverage-group'>"
        "<div class='coverage-group-title'>当前账号覆盖</div>"
        "<div class='coverage-pair'>"
        + coverage_node(
            "全部 App 账号行",
            _fmt_count(current_app_users),
            "当前 users rows · 全 runtime mode · 可能含重装旧号",
        )
        + coverage_node(
            "当前 Hosted V2 账号行",
            _fmt_count(current_hosted_v2_users),
            f"占全部 App 账号行 {_fmt_ratio(current_v2_coverage)}",
        )
        + "</div></section>"
        "<section class='coverage-group'>"
        "<div class='coverage-group-title'>所选窗口活动 · 跟随筛选</div>"
        "<div class='coverage-pair'>"
        + coverage_node(
            "窗口 V2 用量用户",
            _fmt_count(overview.get("model_active_users")),
            "所选窗口至少一次 model call",
        )
        + coverage_node(
            "Token 可计量用户",
            _fmt_count(overview.get("token_users")),
            "窗口内 input / output 两列各至少有一次已知值",
        )
        + "</div></section></div>"
    )

    def page_href(**updates) -> str:
        if drilldown_user_id:
            return _usage_user_href(query, drilldown_user_id, **updates)
        return _usage_page_href(query, **updates)

    preset_links = []
    for preset in ("24h", "7d", "30d", "90d"):
        cls = "sort-button active" if query.preset == preset else "sort-button"
        href = page_href(preset=preset, start_date=None, end_date=None, offset=0)
        preset_links.append(
            f"<a class='{cls}' href='{html.escape(href, quote=True)}'>{preset}</a>"
        )
    admin_key = html.escape(str(request.args.get("admin_key") or ""), quote=True)
    custom_start = html.escape(str(query.start_date or ""), quote=True)
    custom_end = html.escape(str(query.end_date or ""), quote=True)
    timezone_name = html.escape(query.timezone, quote=True)
    user_id_value = html.escape(str(query.user_id or ""), quote=True)
    form_action = "/admin/data-track"
    user_filter = f"<label>User ID<input name='user_id' value='{user_id_value}'></label>"
    if drilldown_user_id:
        form_action = f"/admin/data-track/users/{quote(drilldown_user_id, safe='')}"
        user_filter = (
            "<input type='hidden' name='user_id' "
            f"value='{html.escape(drilldown_user_id, quote=True)}'>"
        )
    form_action = html.escape(form_action, quote=True)
    preset_options = "".join(
        f"<option value='{preset}'{' selected' if query.preset == preset else ''}>{preset}</option>"
        for preset in ("24h", "7d", "30d", "90d", "custom")
    )
    filter_form = f"""
  <form class='usage-filters' method='get' action='{form_action}'>
    <input type='hidden' name='view' value='usage'>
    <input type='hidden' name='admin_key' value='{admin_key}'>
    <label>Window<select name='preset'>{preset_options}</select></label>
    <label>Start date<input name='start_date' type='date' value='{custom_start}'></label>
    <label>End date<input name='end_date' type='date' value='{custom_end}'></label>
    <label>Timezone<input name='timezone' value='{timezone_name}'></label>
    {user_filter}
    <label>Lane<select name='lane'>{_usage_filter_options(filters.get('lanes'), query.lane)}</select></label>
    <label>Provider<select name='provider'>{_usage_filter_options(filters.get('providers'), query.provider)}</select></label>
    <label>Model<select name='model'>{_usage_filter_options(filters.get('models'), query.model)}</select></label>
    <label>Completeness<select name='completeness'>
      <option value='all'{" selected" if query.completeness == "all" else ""}>All</option>
      <option value='metered'{" selected" if query.completeness == "metered" else ""}>Metered</option>
      <option value='unknown'{" selected" if query.completeness == "unknown" else ""}>Unknown</option>
    </select></label>
    <button type='submit'>Apply range / filters</button>
  </form>"""

    reference_metrics = "".join([
        _render_metric("Range-end registered", _fmt_count(overview.get("registered_accounts"))),
        _render_metric("Range-end activated", _fmt_count(overview.get("activated_users"))),
        _render_metric("Installations", "unavailable until self-host phase"),
    ])
    usage_metrics = "".join([
        _render_metric("窗口 V2 用量用户", _fmt_count(overview.get("model_active_users"))),
        _render_metric("Token 可计量用户", _fmt_count(overview.get("token_users"))),
        _render_metric("有 usage 回报的用户", _fmt_count(overview.get("metered_users"))),
        _render_metric("V2 模型活跃用户日", _fmt_count(overview.get("active_user_days"))),
        _render_metric("Turns", _fmt_count(overview.get("turns"))),
        _render_metric("Model calls", _fmt_count(overview.get("model_calls"))),
        _render_metric("Retries", _fmt_count(overview.get("retries"))),
        _render_metric("Failed turns", _fmt_count(overview.get("failed_turns"))),
    ])
    token_metrics = "".join([
        _render_metric("Prompt tokens", _fmt_count(overview.get("prompt_tokens"))),
        _render_metric("Completion tokens", _fmt_count(overview.get("completion_tokens"))),
        _render_metric("已知 Token 总量（下界）", _fmt_count(overview.get("total_tokens"))),
        _render_metric("可计量用户 Token", _fmt_count(overview.get("token_user_tokens"))),
        _render_metric("每位 Token 可计量用户", _usage_float(averages.get("tokens_per_token_user"))),
        _render_metric("Chat known tokens", _fmt_count(chat_tokens)),
        _render_metric("Background known tokens", _fmt_count(background_tokens)),
        _render_metric("Background share", _fmt_ratio(background_share)),
        _render_metric("Cache read / write / miss", " / ".join([
            _fmt_count(overview.get("cache_read_tokens")),
            _fmt_count(overview.get("cache_write_tokens")),
            _fmt_count(overview.get("cache_miss_tokens")),
        ])),
        _render_metric("Unknown usage calls", _fmt_count(overview.get("unknown_usage_calls"))),
        _render_metric("Usage coverage", _fmt_ratio(coverage.get("usage_coverage"))),
        _render_metric("Cache coverage", _fmt_ratio(coverage.get("cache_coverage"))),
    ])
    activated_average = averages.get("tokens_per_activated_user_day")
    activated_average_text = (
        "not applicable for filtered cohort"
        if activated_average is None and dimension_filtered
        else _usage_float(activated_average)
    )
    distribution_available = averages.get("user_day_tokens") is not None
    distribution = averages.get("user_day_tokens") or {}
    average_metrics = "".join([
        _render_metric("每日已知 Token（窗口均值）", _usage_float(averages.get("tokens_per_calendar_day"))),
        _render_metric("每位 V2 模型 DAU 已知 Token（加权）", _usage_float(averages.get("tokens_per_active_user_day"))),
        _render_metric("每个当前激活账号·日（参考）", activated_average_text),
        _render_metric("每个可计量 turn", _usage_float(averages.get("tokens_per_metered_turn"))),
        _render_metric("User-day p50 / p75", f"{_usage_float(distribution.get('p50'))} / {_usage_float(distribution.get('p75'))}"),
        _render_metric("User-day p90 / p95 / max", f"{_usage_float(distribution.get('p90'))} / {_usage_float(distribution.get('p95'))} / {_usage_float(distribution.get('max'))}"),
        _render_metric("Model calls / turn", _usage_float(averages.get("model_calls_per_turn"), digits=2)),
        _render_metric("Retries / turn", _usage_float(averages.get("retries_per_turn"), digits=2)),
    ])

    daily_rows = []
    max_daily_tokens = max(
        (int(row.get("total_tokens") or 0) for row in daily), default=0
    )
    for row in daily:
        total = row.get("total_tokens")
        width = (float(total) * 100 / max_daily_tokens) if total is not None and max_daily_tokens else 0
        daily_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('local_day') or ''))}</td>"
            f"<td>{_fmt_count(total)}<div class='usage-bar'><span style='width:{width:.1f}%'></span></div></td>"
            f"<td>{_fmt_count(row.get('model_active_users'))}</td>"
            f"<td>{_fmt_count(row.get('token_users'))}</td>"
            f"<td>{_usage_float(row.get('tokens_per_token_user'))}</td>"
            f"<td>{_usage_float(row.get('tokens_per_active_user_day'))}</td>"
            f"<td>{_usage_float(row.get('tokens_per_metered_turn'))}</td>"
            f"<td>{_fmt_count(row.get('model_calls'))}</td>"
            f"<td>{_fmt_count(row.get('retries'))}</td>"
            f"<td>{_fmt_count(row.get('failed_turns'))}</td>"
            f"<td>{_fmt_ratio(row.get('usage_coverage'))} / {_fmt_ratio(row.get('cache_coverage'))}</td>"
            "</tr>"
        )

    users_available = report.get("users") is not None
    user_rows, user_offset, user_total = _usage_sorted_users(report)
    rendered_users = []
    for row in user_rows:
        raw_user_id = str(row.get("user_id") or "unknown")
        user_label = f"<code>{html.escape(raw_user_id)}</code>"
        if raw_user_id != "unknown":
            href = _usage_user_href(query, raw_user_id)
            user_label = f"<a href='{html.escape(href, quote=True)}'>{user_label}</a>"
        rendered_users.append(
            "<tr>"
            f"<td>{user_label}</td>"
            f"<td>{html.escape(_usage_timestamp(row.get('last_model_call_at')))}</td>"
            f"<td>{_fmt_count(row.get('active_days'))}</td>"
            f"<td>{_fmt_count(row.get('turns'))} / {_fmt_count(row.get('model_calls'))} / {_fmt_count(row.get('retries'))} / {_fmt_count(row.get('failed_turns'))}</td>"
            f"<td>{_fmt_count(row.get('prompt_tokens'))} / {_fmt_count(row.get('completion_tokens'))} / {_fmt_count(row.get('total_tokens'))}</td>"
            f"<td>{_fmt_count(row.get('cache_read_tokens'))} / {_fmt_count(row.get('cache_write_tokens'))} / {_fmt_count(row.get('cache_miss_tokens'))}</td>"
            f"<td>{_usage_float(row.get('tokens_per_calendar_day'))} / {_usage_float(row.get('tokens_per_active_day'))}</td>"
            f"<td>{_usage_float(row.get('daily_p50'))} / {_usage_float(row.get('daily_p95'))}</td>"
            f"<td>{_usage_float(row.get('tokens_per_metered_turn'))}</td>"
            f"<td>{html.escape(str(row.get('primary_provider') or 'unknown'))} / {html.escape(str(row.get('primary_model') or 'unknown'))}</td>"
            f"<td>{_fmt_ratio(row.get('usage_coverage'))} / {_fmt_ratio(row.get('cache_coverage'))} / {_fmt_count(row.get('unknown_usage_calls'))}</td>"
            f"<td>{_fmt_ratio(row.get('known_token_share'))}</td>"
            "</tr>"
        )
    sort_links = []
    active_sort = str(request.args.get("sort") or "tokens").strip().lower()
    active_dir = str(request.args.get("dir") or "desc").strip().lower()
    for key, label in (("tokens", "Known token"), ("calls", "Calls"), ("retries", "Retries"), ("recent", "Recent")):
        next_dir = "asc" if key == active_sort and active_dir == "desc" else "desc"
        href = page_href(sort=key, dir=next_dir, offset=0)
        cls = "sort-button active" if key == active_sort else "sort-button"
        sort_links.append(f"<a class='{cls}' href='{html.escape(href, quote=True)}'>{label}</a>")
    pager = []
    if user_offset:
        href = page_href(sort=active_sort, dir=active_dir, offset=max(0, user_offset - 100))
        pager.append(f"<a class='sort-button' href='{html.escape(href, quote=True)}'>Prev</a>")
    if user_offset + len(user_rows) < user_total:
        href = page_href(sort=active_sort, dir=active_dir, offset=user_offset + 100)
        pager.append(f"<a class='sort-button' href='{html.escape(href, quote=True)}'>Next</a>")

    models_available = report.get("models") is not None
    model_rows = []
    for row in report.get("models") or []:
        model_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('provider') or 'unknown'))}</td>"
            f"<td>{html.escape(str(row.get('model') or 'unknown'))}</td>"
            f"<td>{_fmt_count(row.get('users'))}</td>"
            f"<td>{_fmt_count(row.get('turns'))} / {_fmt_count(row.get('model_calls'))} / {_fmt_count(row.get('retries'))}</td>"
            f"<td>{_fmt_count(row.get('failed_turns'))} / {_fmt_ratio(row.get('failure_rate'))} / {_fmt_ratio(row.get('retry_rate'))}</td>"
            f"<td>{_fmt_count(row.get('prompt_tokens'))} / {_fmt_count(row.get('completion_tokens'))} / {_fmt_count(row.get('total_tokens'))}</td>"
            f"<td>{_fmt_count(row.get('cache_read_tokens'))} / {_fmt_count(row.get('cache_write_tokens'))} / {_fmt_count(row.get('cache_miss_tokens'))}</td>"
            f"<td>{_usage_float(row.get('tokens_per_call'))}</td>"
            f"<td>{_fmt_duration_sec((row.get('latency_ms_p50') or 0) / 1000 if row.get('latency_ms_p50') is not None else None)} / {_fmt_duration_sec((row.get('latency_ms_p95') or 0) / 1000 if row.get('latency_ms_p95') is not None else None)}</td>"
            f"<td>{_fmt_ratio(row.get('usage_coverage'))} / {_fmt_ratio(row.get('cache_coverage'))}</td>"
            "</tr>"
        )

    lane_rows = []
    for row in lanes:
        lane_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('lane') or 'unknown'))}</td>"
            f"<td>{_fmt_count(row.get('users'))}</td>"
            f"<td>{_fmt_count(row.get('turns'))} / {_fmt_count(row.get('model_calls'))} / {_fmt_count(row.get('retries'))}</td>"
            f"<td>{_fmt_count(row.get('failed_turns'))} / {_fmt_ratio(row.get('failure_rate'))} / {_fmt_ratio(row.get('retry_rate'))}</td>"
            f"<td>{_fmt_count(row.get('prompt_tokens'))} / {_fmt_count(row.get('completion_tokens'))} / {_fmt_count(row.get('total_tokens'))}</td>"
            f"<td>{_fmt_count(row.get('cache_read_tokens'))} / {_fmt_count(row.get('cache_write_tokens'))} / {_fmt_count(row.get('cache_miss_tokens'))}</td>"
            f"<td>{_fmt_ratio(row.get('usage_coverage'))} / {_fmt_ratio(row.get('cache_coverage'))}</td>"
            "</tr>"
        )

    cohort = coverage.get("reference_cohort") or {}
    rollup = coverage.get("rollup")
    if rollup:
        lag = rollup.get("source_lag_seconds")
        lag_text = "unknown" if lag is None else _fmt_duration_sec(float(lag))
        error = rollup.get("last_error")
        error_text = (
            f" · last error {html.escape(str(error))} at "
            f"{html.escape(str(rollup.get('last_error_at') or 'unknown'))}"
            if error
            else ""
        )
        rollup_freshness = (
            "<div class='note-box'><b>Rollup freshness:</b> "
            f"mode {html.escape(str(rollup.get('mode') or 'unknown'))} · "
            f"refreshed {html.escape(str(rollup.get('refreshed_at') or 'unknown'))} · "
            f"processed ({html.escape(str(rollup.get('processed_updated_at') or 'unknown'))}, "
            f"{html.escape(str(rollup.get('processed_id') if rollup.get('processed_id') is not None else 'unknown'))}) · "
            f"source observed {html.escape(str(rollup.get('source_observed_updated_at') or 'unknown'))} · "
            f"lag {lag_text} · raw days {_fmt_count(len(rollup.get('raw_days') or []))} · "
            f"rollup days {_fmt_count(len(rollup.get('rollup_days') or []))}"
            f"{error_text}</div>"
        )
    else:
        rollup_freshness = (
            "<div class='note-box'><b>Rollup freshness:</b> unavailable; "
            "freshness and lag are unknown, not zero.</div>"
        )
    drilldown = ""
    if drilldown_user_id:
        back_href = _usage_page_href(query, user_id=None, offset=0)
        drilldown = (
            "<div class='note-box'><b>单用户钻取：</b>"
            f"<code>{html.escape(drilldown_user_id)}</code> · "
            f"<a href='{html.escape(back_href, quote=True)}'>返回全体用户</a></div>"
        )
    daily_body = (
        "".join(daily_rows)
        if daily_rows
        else (
            "<tr><td colspan='11' class='muted'>窗口内无日期。</td></tr>"
            if daily_available
            else "<tr><td colspan='11' class='muted'>每日趋势暂时取不到；其他区块仍可用。</td></tr>"
        )
    )
    user_body = (
        "".join(rendered_users)
        if rendered_users
        else (
            "<tr><td colspan='12' class='muted'>当前 cohort 无用户用量。</td></tr>"
            if users_available
            else "<tr><td colspan='12' class='muted'>Per-user Usage 暂时取不到；其他区块仍可用。</td></tr>"
        )
    )
    model_body = (
        "".join(model_rows)
        if model_rows
        else (
            "<tr><td colspan='10' class='muted'>当前 cohort 无 provider/model 数据。</td></tr>"
            if models_available
            else "<tr><td colspan='10' class='muted'>Provider / Model 暂时取不到；其他区块仍可用。</td></tr>"
        )
    )
    lane_body = (
        "".join(lane_rows)
        if lane_rows
        else (
            "<tr><td colspan='7' class='muted'>当前 cohort 无 lane 数据。</td></tr>"
            if lanes_available
            else "<tr><td colspan='7' class='muted'>Lane breakdown 暂时取不到；其他区块仍可用。</td></tr>"
        )
    )
    lane_section = f"""
  <h2>Lane breakdown · 成本拆分</h2>
  <div class='muted'>chat 单列；background 是所有非 chat lane 的合计。下表保留逐 lane 明细，便于定位 dream / profile / heartbeat / maintenance / capture 的成本。</div>
  <div class='table-wrap'><table><thead><tr><th>Lane</th><th>Users</th><th>Turns / calls / retries</th><th>Failed / failure / retry rate</th><th>Prompt / completion / total</th><th>Cache R / W / M</th><th>Usage / cache coverage</th></tr></thead>
  <tbody>{lane_body}</tbody></table></div>"""
    optional_note_parts = []
    if not filters_available:
        optional_note_parts.append("筛选候选项暂时取不到，当前已选筛选仍然生效。")
    if not distribution_available:
        optional_note_parts.append("User-day 分位数暂时取不到。")
    optional_note = (
        f"<div class='note-box'>{html.escape(' '.join(optional_note_parts))}</div>"
        if optional_note_parts
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Token 与模型 · Feedling Data Track</title>
  <style>{_RUNTIME_PAGE_CSS}
    :root {{ --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; --accent-soft:#e7eee9; --data:#b16a3f; }}
    main {{ max-width:1440px; padding-top:24px; }}
    h1 {{ font-size:30px; letter-spacing:-.025em; }}
    h2 {{ font-size:17px; letter-spacing:-.01em; }}
    .usage-header {{ display:flex; justify-content:space-between; align-items:flex-end; gap:24px; margin-bottom:14px; }}
    .usage-kicker,.eyebrow {{ display:block; color:var(--accent); font-size:11px; font-weight:750; letter-spacing:.1em; text-transform:uppercase; }}
    .usage-range {{ max-width:72ch; margin-top:6px; }}
    .range-row {{ display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin:12px 0; }}
    .range-label {{ color:var(--muted); font-size:12px; font-weight:700; }}
    .usage-filters {{ display:flex; flex-wrap:wrap; gap:10px; align-items:end; margin:12px 0 2px; }}
    .usage-filters label {{ display:flex; flex-direction:column; gap:4px; color:var(--muted); font-size:12px; }}
    .usage-filters input,.usage-filters select,.usage-filters button {{ min-height:44px; box-sizing:border-box; border:1px solid var(--line); border-radius:6px; background:var(--card); color:var(--fg); padding:9px 11px; }}
    .usage-filters button {{ color:#f7faf7; background:var(--accent); border-color:var(--accent); cursor:pointer; font-weight:700; }}
    .advanced-filters,.technical-block {{ border-top:1px solid var(--line); border-bottom:1px solid var(--line); margin:12px 0 20px; padding:10px 0; }}
    details > summary {{ cursor:pointer; color:var(--fg); font-weight:700; list-style-position:outside; }}
    details > summary::marker {{ color:var(--accent); }}
    .usage-pulse {{ margin:28px 0 0; padding:22px 24px 20px; background:var(--accent-soft); border:1px solid #cddbd1; border-radius:12px; }}
    .pulse-heading {{ display:flex; justify-content:space-between; align-items:flex-start; gap:20px; }}
    .pulse-heading h2 {{ margin:4px 0 0; font-size:20px; }}
    .scope-chips {{ display:flex; flex-wrap:wrap; justify-content:flex-end; gap:6px; }}
    .scope-chips span {{ padding:4px 8px; border:1px solid #c7d4cb; border-radius:999px; color:#365846; background:#f5f8f5; font-size:11px; font-weight:650; }}
    .pulse-equation {{ display:grid; grid-template-columns:minmax(130px,1fr) auto minmax(130px,1fr) auto minmax(180px,1.25fr); gap:18px; align-items:end; margin:26px 0 18px; font-variant-numeric:tabular-nums; }}
    .equation-term,.equation-result {{ display:flex; flex-direction:column; gap:3px; }}
    .equation-term small,.equation-result small {{ color:var(--muted); font-size:12px; }}
    .equation-term strong {{ font-size:30px; line-height:1; }}
    .equation-result {{ padding:12px 14px; background:#315744; color:#f4f7f4; border-radius:8px; }}
    .equation-result strong {{ font-size:38px; line-height:.95; }}
    .equation-result small {{ color:#dce8e0; }}
    .equation-sign {{ align-self:center; color:#718077; font-size:24px; }}
    .pulse-foot {{ display:flex; justify-content:space-between; gap:18px; padding-top:13px; border-top:1px solid #c7d4cb; color:var(--muted); font-size:12px; }}
    .window-ledger {{ display:grid; grid-template-columns:repeat(4,1fr); border-bottom:1px solid var(--line); margin:0 0 26px; font-variant-numeric:tabular-nums; }}
    .window-ledger > div {{ padding:14px 18px 15px; border-right:1px solid var(--line); }}
    .window-ledger > div:last-child {{ border-right:0; }}
    .window-ledger span {{ display:block; color:var(--muted); font-size:11px; }}
    .window-ledger b {{ display:block; margin-top:3px; font-size:21px; }}
    .trend-ledger {{ margin:12px 0 20px; font-variant-numeric:tabular-nums; }}
    .trend-head,.trend-row {{ display:grid; grid-template-columns:64px minmax(160px,1fr) 78px 88px 104px; gap:12px; align-items:center; min-height:34px; padding:0 10px; }}
    .trend-head {{ color:var(--muted); font-size:10px; font-weight:750; letter-spacing:.04em; text-transform:uppercase; border-bottom:1px solid var(--line); }}
    .trend-row {{ color:var(--muted); font-size:12px; border-bottom:1px solid #e8e6df; }}
    .trend-row b {{ color:var(--fg); text-align:right; }}
    .trend-row.focus {{ background:#f0eee7; color:var(--fg); }}
    .trend-track {{ height:7px; background:#e7e3d9; overflow:hidden; border-radius:999px; }}
    .trend-track span {{ display:block; height:100%; background:var(--data); }}
    .usage-empty {{ padding:22px; color:var(--muted); background:var(--card); border:1px dashed var(--line); }}
    .usage-bar {{ width:90px; height:4px; margin-top:4px; background:#e7e3d9; border-radius:2px; overflow:hidden; }}
    .usage-bar span {{ display:block; height:100%; background:var(--data); }}
    .table-wrap {{ overflow-x:auto; margin-top:10px; }}
    table {{ font-variant-numeric:tabular-nums; }}
    th {{ position:sticky; top:0; z-index:1; }}
    .coverage-groups {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:12px 0 8px; }}
    .coverage-group {{ min-width:0; padding:13px; border:1px solid var(--line); border-radius:9px; background:#f0eee8; }}
    .coverage-group-title {{ margin:0 0 9px 2px; color:var(--muted); font-size:11px; font-weight:750; letter-spacing:.06em; text-transform:uppercase; }}
    .coverage-pair {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; align-items:stretch; }}
    .coverage-node {{ height:100%; box-sizing:border-box; background:var(--card); border:1px solid var(--line); border-radius:7px; padding:13px; }}
    .coverage-label {{ color:var(--muted); font-size:12px; }}
    .coverage-value {{ margin:3px 0 2px; font-size:27px; line-height:1.15; font-weight:750; font-variant-numeric:tabular-nums; }}
    .coverage-detail {{ color:var(--muted); font-size:11px; }}
    .section-intro {{ max-width:78ch; margin-top:-7px; }}
    .diagnostic-stack {{ display:grid; gap:10px; margin-top:26px; }}
    .diagnostic-stack > details {{ margin:0; padding:12px 0; }}
    a:focus-visible,summary:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible {{ outline:3px solid #9bb7a7; outline-offset:2px; }}
    @media (max-width:860px) {{
      .pulse-heading,.pulse-foot,.usage-header {{ align-items:flex-start; flex-direction:column; }}
      .scope-chips {{ justify-content:flex-start; }}
      .pulse-equation {{ grid-template-columns:1fr auto 1fr; }}
      .pulse-equation .equation-result {{ grid-column:1 / -1; }}
      .pulse-equation .equation-sign:nth-of-type(2) {{ display:none; }}
      .window-ledger {{ grid-template-columns:1fr 1fr; }}
      .window-ledger > div:nth-child(2) {{ border-right:0; }}
      .coverage-groups {{ grid-template-columns:1fr; }}
    }}
    @media (max-width:640px) {{
      main {{ padding:18px 14px 36px; }}
      .usage-pulse {{ padding:18px 16px; }}
      .pulse-equation {{ gap:9px; }}
      .equation-term strong {{ font-size:25px; }}
      .trend-head {{ display:none; }}
      .trend-row {{ grid-template-columns:50px minmax(80px,1fr) 62px; gap:8px; padding:6px 4px; }}
      .trend-row span:nth-last-child(-n+2) {{ grid-column:2 / -1; }}
      .coverage-pair {{ grid-template-columns:1fr; }}
    }}
  </style>
</head>
<body><main>
  <header class='usage-header'>
    <div><span class='usage-kicker'>Usage / 模型用量</span><h1>Token 与模型</h1>
    <div class='muted usage-range'>Hosted Runtime V2 whole-turn telemetry · UTC
      {html.escape(query.start_at_utc.isoformat())} 至 {html.escape(query.end_at_utc.isoformat())} · 展示时区 {timezone_name}</div></div>
  </header>
  {_render_data_track_view_nav('usage', query, drilldown_user_id)}
  <div class='range-row'><span class='range-label'>时间范围</span><div class='sortbar'>{''.join(preset_links)}</div></div>
  <details class='advanced-filters'{" open" if dimension_filtered or query.preset == "custom" else ""}>
    <summary>高级筛选与自定义日期</summary>{filter_form}
  </details>
  {drilldown}
  {usage_pulse}
  <h2>每日趋势</h2>
  <div class='muted section-intro'>下方只展示最近 14 个返回日；窗口平均基于窗口内全部自然日行。滚动窗口首尾可能只是部分自然日。Token / DAU 一律使用已知 Token 下界。</div>
  {_render_usage_trend(daily, focus_day=focus_day)}
  <details class='technical-block'>
    <summary>查看完整每日数据</summary>
    <div class='table-wrap'><table><thead><tr><th>Local day</th><th>Known tokens</th><th>V2 模型 DAU</th><th>Token 可计量用户</th><th>Token / 可计量用户</th><th>Token / V2 模型 DAU</th><th>Token / 可计量 turn</th><th>Calls</th><th>Retries</th><th>Failed</th><th>Usage / cache coverage</th></tr></thead>
    <tbody>{daily_body}</tbody></table></div>
  </details>
  <h2>Token 用量与成本结构 · 跟随当前筛选</h2>
  <section class='metrics'>{token_metrics}</section>
  <div class='muted section-intro'>Known total 会保留 partial-only 用户已知的 token；旧名 Complete-cohort tokens 的“可计量用户 Token”与其人均值，只统计窗口内 input / output 两列各至少有一次已知值的用户。Chat / Background 仅拆分当前 cohort；筛选 lane=chat 时 Background 为 0，不代表全站后台用量为 0。</div>
  <h2>用户覆盖与窗口活动</h2>
  {coverage_groups}
  <details class='technical-block'><summary>查看覆盖口径</summary>
    <div class='note-box'><b>先看清口径：</b>“当前账号覆盖”是此刻的全站账号行状态；“所选窗口活动”是 Hosted V2 telemetry 的历史窗口数据，并跟随筛选。两组不是漏斗，不能直接相减。当前 Hosted V2 覆盖率 = Hosted V2 account rows / 全部 App account rows；它不是 usage 上报覆盖率，也不是去重真人比例。</div>
  </details>
  <h2>Per-user Usage</h2>
  <div class='muted section-intro'>按用户查看窗口内已知用量；点击 UID 进入同一筛选口径的单用户钻取。</div>
  <div class='sortbar'>{''.join(sort_links)} {''.join(pager)}<span class='muted'>Showing {user_offset + 1 if user_rows else 0}–{user_offset + len(user_rows)} of {user_total}</span></div>
  <div class='table-wrap'><table><thead><tr><th>User</th><th>Last model call (UTC)</th><th>Active days</th><th>Turns / calls / retries / failed</th><th>Prompt / completion / total</th><th>Cache R / W / M</th><th>Calendar / active-day avg</th><th>Daily p50 / p95</th><th>Tokens / metered turn</th><th>Primary provider / model</th><th>Usage / cache / unknown</th><th>Known share</th></tr></thead>
  <tbody>{user_body}</tbody></table></div>
  <div class='diagnostic-stack'>
    <details class='technical-block'><summary>Provider / Model</summary>
      <h2>Provider / Model</h2>
      <div class='muted'>Identity 是 whole-turn resolved / best-known；requested 与 resolved 将在 P0-B attempt ledger 分开。</div>
      <div class='table-wrap'><table><thead><tr><th>Provider</th><th>Model</th><th>Users</th><th>Turns / calls / retries</th><th>Failed / failure / retry rate</th><th>Prompt / completion / total</th><th>Cache R / W / M</th><th>Tokens / call</th><th>Latency p50 / p95</th><th>Usage / cache coverage</th></tr></thead>
      <tbody>{model_body}</tbody></table></div>
    </details>
    <details class='technical-block'><summary>Lane breakdown · 成本拆分</summary>{lane_section}</details>
    <details class='technical-block'><summary>Fleet Overview 与窗口平均值</summary>
      <h2>Fleet Overview · filtered usage</h2><section class='metrics'>{usage_metrics}</section>
      <h2>平均值</h2><section class='metrics'>{average_metrics}</section>{optional_note}
    </details>
    <details class='technical-block'><summary>Reference cohort 与数据覆盖边界</summary>
      <h2>Reference cohort · 截止所选区间末端</h2><section class='metrics'>{reference_metrics}</section>
      <h2>数据覆盖与边界</h2>{rollup_freshness}
      <div class='note-box'><b>P0-A 边界：</b>Reasoning tokens、possibly-billed attempts 与 authoritative cost 均 <b>unavailable until P0-B</b>，不会显示成零。Prompt token 已包含 cache token，不能重复相加。</div>
      <div class='note-box'>Usage {_fmt_count(coverage.get('usage_reported_calls'))} / {_fmt_count(coverage.get('model_calls'))} ({_fmt_ratio(coverage.get('usage_coverage'))}) · Cache {_fmt_count(coverage.get('cache_reported_calls'))} / {_fmt_count(coverage.get('model_calls'))} ({_fmt_ratio(coverage.get('cache_coverage'))}) · Cache hit {_fmt_ratio(coverage.get('cache_hit_ratio'))}.<br>
      参考 cohort basis: <code>{html.escape(str(cohort.get('basis') or 'unknown'))}</code>；unparseable registrations {_fmt_count(cohort.get('unparseable_registered_rows'))}，legacy memory timestamps {_fmt_count(cohort.get('legacy_memory_rows_without_valid_created_at'))}。{html.escape(str(cohort.get('limitation') or ''))}<br>
      <b>范围：</b>全部 App 账号行与 range-end reference cards 来自 users，跨所有 runtime mode；其余 usage、每日趋势、用户/模型/lane 明细仅统计本实例 Hosted Runtime V2。Resident V1、Hosted V1 与 self-host token 均未计入，不能从本页数字外推。Unknown 是缺报，不是零。</div>
    </details>
  </div>
</main></body></html>"""


def _render_usage_error_page(
    query: admin_usage.UsageQuery,
    *,
    drilldown_user_id: str | None = None,
) -> str:
    drilldown = (
        f"<div class='muted'>Requested user: <code>{html.escape(drilldown_user_id)}</code></div>"
        if drilldown_user_id else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Usage / 模型用量 · Feedling Data Track</title><style>{_RUNTIME_PAGE_CSS}</style></head>
<body><main><h1>Usage / 模型用量</h1>{_render_data_track_view_nav('usage', query, drilldown_user_id)}
<div class='note-box'><b>Usage 数据暂时取不到。</b>仅本页降级；Runtime 健康和其他 Admin 视图不受影响。具体异常见后端日志。</div>
{drilldown}<div class='muted'>Requested UTC range {html.escape(query.start_at_utc.isoformat())} — {html.escape(query.end_at_utc.isoformat())}.</div>
</main></body></html>"""


# 诊断二级导航的 11 个遗留视图。顺序即渲染顺序；所有旧 URL 原样可用，
# 这里只是把它们从一级导航挪进「诊断」抽屉。
_DIAG_NAV_ITEMS = (
    ("overview", "总览"),
    ("imports", "记忆导入"),
    ("chat", "聊天可靠性"),
    ("latency", "延迟"),
    ("dau", "日活与时长"),
    ("growth", "增长 & 留存"),
    ("proactive", "主动任务"),
    ("events", "事件采集"),
    ("runtime", "运行状态"),
    ("usage", "Token 与模型"),
    ("debug", "调试"),
)
_DIAG_NAV_VIEW_IDS = frozenset(view for view, _ in _DIAG_NAV_ITEMS)


def _render_data_track_view_nav(
    active: str,
    usage_query: admin_usage.UsageQuery | None = None,
    usage_drilldown_user_id: str | None = None,
) -> str:
    # 一级导航只留 4 个值班入口：首页 / 产品健康 / 用户 / 诊断。首页是新
    # 默认视图（view 缺省即首页），所以 home 的链接不带 view 参数；users
    # 从默认位退下来后必须显式携带 view=users。11 个遗留诊断视图只在当前
    # 就处于诊断族（或诊断枢纽页）时展开成第二行，active 高亮不变。
    in_diag = active == "diag" or active in _DIAG_NAV_VIEW_IDS

    def nav_item(view: str, label: str) -> str:
        cls = "sort-button active" if active == view else "sort-button"
        if view == "usage":
            if usage_drilldown_user_id:
                href = _usage_user_href(
                    usage_query,
                    usage_drilldown_user_id,
                    offset=0,
                )
            else:
                href = _usage_page_href(usage_query, offset=0)
        else:
            href = _data_track_page_href(
                view=None if view == "home" else view,
                offset=0,
                # A Usage drill-down's user_id belongs to its analytics cohort.
                # Do not leak it into unrelated views where user_id has another
                # meaning (or is silently ignored).
                user_id=None if active == "usage" else request.args.get("user_id"),
                # Runtime population is a Users-only filter.  Carrying it into
                # DAU / Growth / Runtime produces a URL that looks filtered
                # even though those views do not consume it.
                runtime_state=(
                    request.args.get("runtime_state")
                    if view == "users"
                    else None
                ),
                # The four operations views and Runtime share the rolling
                # hours window.  Other pages (home/diag/health included) have
                # their own contracts; carrying hours there only makes URLs
                # look filtered when they are not.
                hours=(
                    request.args.get("hours")
                    if view in {"overview", "imports", "chat", "latency", "runtime"}
                    else None
                ),
                day=(
                    request.args.get("day")
                    if view in {"dau", "proactive"}
                    else None
                ),
            )
        current = " aria-current='page'" if active == view else ""
        return (
            f"<a class='{cls}' href='{html.escape(href, quote=True)}'"
            f"{current}>{html.escape(label)}</a>"
        )

    def diag_entry_item() -> str:
        # 「诊断」入口在整个诊断族里都保持点亮（否则从二级行进入某个遗留
        # 视图后一级导航看起来什么都没选中），aria-current 只给枢纽页本身。
        cls = "sort-button active" if in_diag else "sort-button"
        href = _data_track_page_href(
            view="diag",
            offset=0,
            user_id=None if active == "usage" else request.args.get("user_id"),
            runtime_state=None,
            hours=None,
            day=None,
        )
        current = " aria-current='page'" if active == "diag" else ""
        return (
            f"<a class='{cls}' href='{html.escape(href, quote=True)}'"
            f"{current}>诊断</a>"
        )

    primary = (
        "<nav class='viewbar' aria-label='Data Track views'>"
        + nav_item("home", "首页")
        # health 是固定周口径页，故意不进 hours/day 参数携带集合——带上
        # 只会让 URL 看起来被过滤而页面根本不消费。
        + nav_item("health", "产品健康")
        + nav_item("users", "用户")
        + diag_entry_item()
        + "</nav>"
    )
    if not in_diag:
        return primary
    second = (
        "<nav class='viewbar viewbar-diag' aria-label='诊断视图'>"
        "<span class='nav-group-label'>诊断视图</span>"
        + "".join(nav_item(view, label) for view, label in _DIAG_NAV_ITEMS)
        + "</nav>"
    )
    return primary + second


# 队列原因 → (pill 档位, 兜底文案)。stalled_no_reply 是「用户在等我们」，
# 直接 bad；其余是「流程卡住」级别的 warn。offline 在 v1 结构性缺席（见
# _render_home_page 的说明框），保留只为 db 契约里的 reason_code 全集。
_HOME_QUEUE_REASON_META = {
    "stalled_no_reply": ("bad", "等回复没等到"),
    "onboarding_stuck": ("warn", "onboarding 卡住"),
    "model_config_pending": ("warn", "模型配置待处理"),
    "offline": ("warn", "疑似掉线"),
}

_HOME_FEED_KIND_META = {
    "registration": ("注册", "ok"),
    "first_reply": ("首次真回复", "ok"),
    "import_failed": ("导入失败", "bad"),
}


def _home_status_card(
    title: str,
    verdict: dict | None,
    href: str,
    drill: str,
    *,
    card_id: str = "",
    max_reasons: int = 3,
) -> str:
    """首页状态灯（复用 question-card 视觉与 pill 档位，含灰 unknown）。"""
    level = str((verdict or {}).get("level") or "unknown").strip().lower()
    if level not in ("ok", "warn", "bad", "unknown"):
        level = "unknown"
    reasons = [str(r) for r in ((verdict or {}).get("reasons") or []) if str(r).strip()]
    if verdict is None:
        level = "unknown"
        reasons = ["统计暂不可用"]
    if not reasons:
        reasons = ["窗口内未见异常"] if level == "ok" else ["（无原因说明）"]
    evidence = "<br>".join(html.escape(r) for r in reasons[:max_reasons])
    if len(reasons) > max_reasons:
        evidence += f"<br><span class='muted'>…共 {len(reasons)} 条</span>"
    id_attr = f" id='{html.escape(card_id, quote=True)}'" if card_id else ""
    return (
        f"<article class='question-card {level}'{id_attr}>"
        f"<a class='question-link' href='{html.escape(href, quote=True)}'>"
        f"<div class='question-top'><h3 class='question-title'>{html.escape(title)}</h3>"
        f"{_ops_status(level)}</div>"
        f"<div class='question-evidence'>{evidence}</div>"
        f"<div class='question-drill'>{html.escape(drill)}</div>"
        "</a></article>"
    )


def _home_queue_section(queue: dict | None) -> str:
    # 用户详情链接沿用 users 页的 qs 透传模式（admin_key 等跟着走）；view
    # 参数属于当前页，不带进详情页。
    qs = _data_track_qs(view=None)
    qs_suffix = f"?{qs}" if qs else ""
    offline_note = (
        "<div class='note-box'><b>掉线检测未接入：</b>resident 轮询在线状态只存在于"
        "进程内存 registry、不落库，本队列查不到，所以「疑似掉线」这一类 v1 先空缺"
        "——列表里没有掉线项<b>不等于</b>没人掉线。单个用户的连接状态看"
        "「用户」页的连接列。</div>"
    )
    if queue is None:
        return (
            "<div class='note-box'><b>队列暂不可用。</b>查询失败按未知处理，"
            "不会渲染成「没有人卡住」。</div>" + offline_note
        )
    rows = [r for r in (queue.get("rows") or []) if isinstance(r, dict)]
    if not rows:
        return (
            "<div class='queue-empty'>没有人卡住。</div>" + offline_note
        )
    now = time.time()

    def queue_tr(row: dict) -> str:
        user_id = str(row.get("user_id") or "")
        reason_code = str(row.get("reason_code") or "")
        pill_cls, fallback_label = _HOME_QUEUE_REASON_META.get(
            reason_code, ("warn", reason_code or "未知原因")
        )
        reason_text = str(row.get("reason_text") or "").strip() or fallback_label
        since_epoch = row.get("since_epoch")
        try:
            stuck_for = (
                _format_duration(int(now - float(since_epoch)))
                if since_epoch is not None and float(since_epoch) > 0
                else "—"
            )
        except (TypeError, ValueError):
            stuck_for = "—"
        detail = str(row.get("detail") or "")
        # safe=''：user_id 里万一混进 '/'，默认 safe='/' 会让它逃出
        # /users/<id> 路径段——与 imports 页同一硬化（见 _render_import 链接）。
        user_url = f"/admin/data-track/users/{quote(user_id, safe='')}{qs_suffix}"
        return (
            "<tr>"
            f"<td><a href='{html.escape(user_url, quote=True)}'>{html.escape(user_id)}</a></td>"
            f"<td><span class='pill {pill_cls}'>{html.escape(reason_text)}</span></td>"
            f"<td>{html.escape(stuck_for)}</td>"
            f"<td class='muted'>{html.escape(detail)}</td>"
            "</tr>"
        )

    # 同因折叠：一个注册波卡在同一步会把队列刷屏（prod 首日 8+ 条同款
    # 「13d · t1 未达」把更急的 stalled 挤下屏）。每个原因先露前 3 条，
    # 其余收进可展开行——工单的价值在多样性，不在同因复读。
    by_reason: dict[str, list[dict]] = {}
    for row in rows:
        by_reason.setdefault(str(row.get("reason_code") or ""), []).append(row)
    body_rows: list[str] = []
    for reason_code, reason_rows in by_reason.items():
        for row in reason_rows[:3]:
            body_rows.append(queue_tr(row))
        rest = reason_rows[3:]
        if rest:
            _, fallback_label = _HOME_QUEUE_REASON_META.get(
                reason_code, ("warn", reason_code or "未知原因")
            )
            label = (
                str(rest[0].get("reason_text") or "").strip() or fallback_label
            )
            inner = "".join(queue_tr(row) for row in rest)
            body_rows.append(
                "<tr class='queue-more'><td colspan='4'>"
                f"<details><summary>还有 {len(rest)} 个「{html.escape(label)}」用户</summary>"
                f"<table class='queue-table'><tbody>{inner}</tbody></table>"
                "</details></td></tr>"
            )
    truncated_note = (
        "<div class='muted'>只显示最严重 / 最早的前 20 条。</div>"
        if queue.get("truncated") else ""
    )
    return (
        "<div class='table-wrap'><table class='queue-table'>"
        "<thead><tr><th>用户</th><th>原因</th><th>卡了多久</th><th>细节</th></tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table></div>"
        + truncated_note + offline_note
    )


def _home_pulse_section(pulse: dict | None) -> str:
    if pulse is None:
        return (
            "<div class='note-box'><b>产品脉搏暂不可用。</b>页面不会把未知渲染成 0；"
            "完整口径看「产品健康」页。</div>"
        )
    health_href = html.escape(_data_track_page_href(view="health"), quote=True)
    daily = [d for d in (pulse.get("daily_actives") or []) if isinstance(d, dict)]
    dau_values = [d.get("dau") for d in daily]
    wau = pulse.get("wau")
    prev_wau = pulse.get("prev_wau")
    latest_day = daily[-1] if daily else {}
    wau_sub = (
        f"最新完整日 {html.escape(str(latest_day.get('day') or ''))} · DAU {_fmt_count(latest_day.get('dau'))}"
        if daily else "暂无完整日样本"
    )
    w4 = pulse.get("latest_mature_w4")
    if isinstance(w4, dict):
        # db 契约里 pct 已是 0–100 百分数（retention 快照口径），不是 0–1
        # 比率——不能过 _fmt_ratio 再乘一次 100。
        try:
            w4_value = f"{float(w4.get('pct')):.1f}%"
        except (TypeError, ValueError):
            w4_value = "—"
        w4_sub = (
            f"注册周 {html.escape(str(w4.get('cohort_week') or ''))}"
            f" · n={_fmt_count(w4.get('n'))}"
        )
    else:
        w4_value = "—"
        w4_sub = "暂无已成熟的注册周 cohort"
    activation = [
        c for c in (pulse.get("activation_recent") or []) if isinstance(c, dict)
    ]
    # activation_recent[0] 永远是进行中的北京周（health builder 的 k=0）：
    # 周二看它 t3_rate 会因右删失塌向 0%，再随周走完「恢复」——进行中的周
    # 不渲染成定局（与 db 侧 growth 判定剔除 this_monday 同一把尺），头条
    # 只取已完整的周，折线上进行中的周留成缺口。
    this_monday = _health_bj_this_monday()
    act_headline = next(
        (c for c in activation
         if str(c.get("cohort_week")) != this_monday
         and c.get("t3_rate") is not None),
        None,
    )
    if act_headline is not None:
        act_value = _fmt_ratio(act_headline.get("t3_rate"))
        act_sub = (
            f"注册周 {html.escape(str(act_headline.get('cohort_week') or ''))}"
            f" · n={_fmt_count(act_headline.get('n'))}"
        )
    else:
        act_value = "—"
        act_sub = "暂无可判的完整注册周 cohort（右删失显 —，不当 0）"
    act_spark_values = [
        None if str(c.get("cohort_week")) == this_monday else c.get("t3_rate")
        for c in reversed(activation)
    ]

    def card(value_html: str, spark_html: str, delta_html: str, label: str, hint: str, sub: str) -> str:
        hint_html = f"<span class='hint' title='{html.escape(hint, quote=True)}'>?</span>"
        return (
            f"<a class='pulse-card' href='{health_href}'>"
            f"<div><span class='pulse-value'>{value_html}</span>{delta_html}{spark_html}</div>"
            f"<div class='metric-label'>{html.escape(label)}{hint_html}</div>"
            f"<div class='pulse-sub'>{sub}</div>"
            "</a>"
        )

    cards = "".join([
        card(
            _fmt_count(wau),
            _spark(dau_values, good_when="up"),
            _render_delta(wau, prev_wau),
            "7日活跃真人",
            "近 7 个已完整北京自然日的去重活跃账号（与 DAU 页同源）；环比对上一个 7 完整日窗口；折线是逐日 DAU",
            wau_sub,
        ),
        card(
            w4_value,
            "",
            "",
            "最新成熟 W4",
            "最新一个已满 4 周的注册周 cohort 第 4 周仍活跃占比（留存快照口径）；没有成熟 cohort 时显 —，不当 0",
            w4_sub,
        ),
        card(
            act_value,
            _spark(act_spark_values, good_when="up"),
            "",
            "激活率（t3）",
            "最新已完整注册周 cohort 中注册后产生首条非 fallback 真回复（t3，不限天数）的比例；进行中的周右删失不当定局；覆盖不完整的周是缺口不是 0；折线为近 4 个 cohort（进行中的周留空）",
            act_sub,
        ),
    ])
    return f"<div class='pulse-cards'>{cards}</div>"


def _home_feed_section(feed: dict | None) -> str:
    qs = _data_track_qs(view=None)
    qs_suffix = f"?{qs}" if qs else ""
    if feed is None:
        return "<div class='note-box'><b>事件流暂不可用。</b>查询失败按未知处理。</div>"
    events = [e for e in (feed.get("events") or []) if isinstance(e, dict)]
    if not events:
        return "<div class='muted'>过去 48 小时没有注册 / 首次真回复 / 导入失败事件。</div>"
    items: list[str] = []
    for ev in events:
        kind = str(ev.get("kind") or "")
        label, pill_cls = _HOME_FEED_KIND_META.get(kind, (kind or "事件", "warn"))
        user_id = str(ev.get("user_id") or "")
        user_url = f"/admin/data-track/users/{quote(user_id, safe='')}{qs_suffix}"
        text = str(ev.get("text") or "")
        items.append(
            "<li>"
            f"<span class='feed-time'>{html.escape(_home_rel_time(ev.get('epoch')))}</span>"
            f"<span class='pill {pill_cls}'>{html.escape(label)}</span>"
            f"<a href='{html.escape(user_url, quote=True)}'>{html.escape(user_id)}</a>"
            f"<span class='muted'>{html.escape(text)}</span>"
            "</li>"
        )
    return f"<ul class='feed-list'>{''.join(items)}</ul>"


def _home_cost_section(cost: dict | None) -> str:
    usage_href = html.escape(_usage_page_href(), quote=True)
    if cost is None:
        return (
            "<div class='note-box'><b>成本统计暂不可用。</b>Token 明细看"
            f" <a href='{usage_href}'>Token 与模型</a>。</div>"
        )
    daily = [d for d in (cost.get("daily_tokens") or []) if isinstance(d, dict)]
    token_values = [d.get("tokens") for d in daily]
    runaway = cost.get("runaway")
    if runaway is True:
        runaway_html = "<span class='pill bad'>放量异常</span>"
    elif runaway is False:
        runaway_html = "<span class='pill ok'>未见放量</span>"
    else:
        runaway_html = "<span class='pill unknown'>样本不足，判不了</span>"
    coverage = cost.get("coverage")
    coverage_note = ""
    try:
        if coverage is not None and float(coverage) < 0.8:
            coverage_note = (
                "<span class='muted'>usage 缺报较多，以上都是已知下限，不是全量。</span>"
            )
    except (TypeError, ValueError):
        pass
    per_active = cost.get("per_active_user_day")
    try:
        # OverflowError：round(float('inf')) 会炸——契约上游今天只产有限值，
        # 但本渲染面的承诺是任何一格坏都只降级成 —，绝不带崩整页（首页是
        # 裸 URL 默认页）。
        per_active_f = float(per_active)
        if not math.isfinite(per_active_f):
            raise ValueError("non-finite per_active_user_day")
        per_active_value = _fmt_tokens_compact(round(per_active_f))
    except (TypeError, ValueError, OverflowError):
        per_active_value = "—"
    hint = (
        "<span class='hint' title='v2_turn_metrics 近 8 个北京日；tokens 为 provider 已上报"
        " usage 的合计，缺报显 —（绝不伪装成 0）；放量判定=最新完整日 > 3×前 6 个完整日中位数，"
        "样本不足显「判不了」'>?</span>"
    )
    return (
        "<div class='cost-line'>"
        f"<div class='cost-item'>近 7 日 token 走势{hint}{_spark(token_values, good_when='neutral')}</div>"
        f"<div class='cost-item'>今日已用（进行中）<b>{_fmt_tokens_compact(cost.get('today_so_far'))}</b></div>"
        f"<div class='cost-item'>每活跃用户日<b>{per_active_value}</b></div>"
        f"<div class='cost-item'>放量判定<b>{runaway_html}</b></div>"
        f"<div class='cost-item'>usage 覆盖率<b>{_fmt_ratio(coverage)}</b></div>"
        f"<div class='cost-item'><a href='{usage_href}'>去 Token 与模型 →</a></div>"
        "</div>"
        + coverage_note
    )


def _render_home_page(
    system_verdict: dict | None,
    soft_verdicts: dict | None,
    queue: dict | None,
    pulse: dict | None,
    feed: dict | None,
    cost: dict | None,
    funnel: dict | None,
) -> str:
    """值班首页（新的默认视图）。七个入参各自独立失败：None → 对应板块
    渲染「暂不可用」，绝不 or 0、绝不装健康。system_verdict 由 admin_core
    用 _ops_import_level/_ops_chat_level/_ops_latency_level 合成后传入
    （db 层不 import 本模块，依赖方向不倒挂）；soft_verdicts 是 db 侧
    admin_home_soft_verdicts 的 growth/cost/evidence 三灯。"""
    soft = soft_verdicts if isinstance(soft_verdicts, dict) else None
    growth_verdict = (soft or {}).get("growth") if soft else None
    cost_verdict = (soft or {}).get("cost") if soft else None
    evidence_verdict = (soft or {}).get("evidence") if soft else None
    status_cards = "".join([
        _home_status_card(
            "系统",
            system_verdict,
            _data_track_page_href(view="overview"),
            "去运营总览 →",
        ),
        _home_status_card(
            "增长",
            growth_verdict,
            _data_track_page_href(view="health"),
            "去产品健康 →",
        ),
        _home_status_card(
            "成本",
            cost_verdict,
            _usage_page_href(),
            "去 Token 与模型 →",
        ),
        # 数据完整性灯的「链接」就是它自己的原因清单：缺口是长期结构性
        # 事实（灰，不会绿），没有更深的钻取页可去。
        _home_status_card(
            "数据完整性",
            evidence_verdict,
            "#evidence-reasons",
            "缺口清单（长期）",
            card_id="evidence-reasons",
            max_reasons=6,
        ),
    ])
    users_href = html.escape(
        _data_track_page_href(view="users", offset=0), quote=True
    )
    definitions = (
        "<details class='note-box'><summary>口径说明（本页全部数字）</summary>"
        "<b>状态灯：</b>系统=运营总览的导入/聊天/延迟三灯取最差档（24h 窗）；增长/成本/数据完整性来自"
        " DB 侧软判定。灰=没证据、黄=测得需关注或证据结构性不闭环、红=测得已坏、绿=证据闭环且健康——"
        "数据完整性在客户端已读 ACK 与 session 来源埋点补齐前恒为灰。"
        "<b>队列：</b>等回复没等到=近 72h 内用户末条消息（剔除 verify_ping/resident_maintenance）之后无"
        "真回复（fallback 与定时主动消息都不算「对用户的回复」）、且已超 30 分钟；"
        "onboarding 卡住=近 14 天注册、t0 后 24h 仍无 t3（t3 沿用漏斗口径，主动消息可点亮）；"
        "模型配置待处理=model_api 测试状态非 ok；同一用户取最重原因去重，最多 20 条。"
        "<b>脉搏：</b>7 日活跃与逐日折线取已完整北京自然日（与 DAU 页同源）；W4 来自留存快照；"
        "激活率取最新可判注册周 cohort，右删失显 — 不当 0。"
        "<b>漏斗：</b>近 28 个完整北京日注册 cohort 的单调里程碑，W1 需个人窗口走完才计；"
        "W1 活跃证据取自会被裁剪（per-user 上限 2000 条）与删号级联的 live 埋点流，是已知下限。"
        "<b>事件流：</b>近 48h 的注册 / 首次真回复 / 导入失败，服务端视角。"
        "<b>成本：</b>v2_turn_metrics 近 8 个北京日已知下限；缺报显 —；放量=最新完整日 > 3×前 6 完整日"
        "中位数；进行中的今天单独标注、不参与判定。"
        "</details>"
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>首页 · Feedling Data Track</title>
<style>{_HOME_PAGE_CSS}</style></head><body><main>
  <span class="ops-kicker">Operations / metadata only</span>
  <h1>Feedling 值班首页</h1>
  <div class="muted">先看四个灯，再看谁卡住；每块都能点进对应诊断页。生成于 {html.escape(_bj_iso(time.time()))}（北京时间）。</div>
  {_render_data_track_view_nav("home")}
  <section class="ops-questions">{status_cards}</section>
  <h2>需要你的用户</h2>
  {_home_queue_section(queue)}
  <h2>产品脉搏 <span class="h2-sub">点卡片进「产品健康」</span></h2>
  {_home_pulse_section(pulse)}
  <h2>激活漏斗 <span class="h2-sub"><a href='{users_href}'>去「用户」页看上一窗口对照 →</a></span></h2>
  {_render_funnel(funnel, compact=True)}
  <h2>今天发生了什么 <span class="h2-sub">近 48 小时</span></h2>
  {_home_feed_section(feed)}
  <h2>成本一行</h2>
  {_home_cost_section(cost)}
  {definitions}
</main></body></html>"""


# 诊断枢纽（?view=diag）：11 张卡各配一行「何时来看」。文案是分诊指南
# 而不是功能清单——值班员该凭症状选门诊。
_DIAG_HUB_CARDS = (
    ("overview", "总览", "值班第一眼：导入 / 聊天 / 延迟三个红绿灯加产品 KPI 环比，判断今天要不要深挖。"),
    ("imports", "记忆导入", "用户说「导入没反应 / 卡住了」：看终态成功率、artifact 证据链和卡住任务。"),
    ("chat", "聊天可靠性", "用户说「发了没回」：查 admitted → final reply 服务端 applied 的交付链断在哪。"),
    ("latency", "延迟", "用户说「回得慢」：排队 / 模型处理 / 整轮 / 服务端交付四段 p95、p99 定位瓶颈。"),
    ("dau", "日活与时长", "想看北京自然日 DAU 与前台使用时长的逐日趋势与冻结快照。"),
    ("growth", "增长 & 留存", "看周注册、留存热力图与增长核算总账（流失 / 回流 / 净变化）。"),
    ("proactive", "主动任务", "主动消息发没发出去：wake 成功率、各 lane 失败原因、超速用户。"),
    ("events", "事件采集", "怀疑客户端埋点断流：各事件类别的量级、成功率与当日走势。"),
    ("runtime", "运行状态", "Runtime V2 值班台：worker 池、lane 失败率、交付积压与用户交付可靠性。"),
    ("usage", "Token 与模型", "谁在烧 token：provider / model 拆分、重试率与缓存命中，成本异常先来这。"),
    ("debug", "调试", "单用户单轮 trace 级排查（含 reveal 明文开关），其他页都定位不了时的最后一站。"),
)


def _render_diag_hub_page() -> str:
    """诊断枢纽页。11 个遗留视图的 URL 原样不变，这里只是导航壳。"""
    cards: list[str] = []
    for view, label, when in _DIAG_HUB_CARDS:
        href = (
            _usage_page_href()
            if view == "usage"
            else _data_track_page_href(view=view, offset=0)
        )
        cards.append(
            f"<a class='diag-card' href='{html.escape(href, quote=True)}'>"
            f"<h3>{html.escape(label)}</h3>"
            f"<p><span class='diag-when'>何时来看：</span>{html.escape(when)}</p>"
            "</a>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>诊断 · Feedling Data Track</title>
<style>{_HOME_PAGE_CSS}</style></head><body><main>
  <span class="ops-kicker">Operations / metadata only</span>
  <h1>诊断</h1>
  <div class="muted">按症状选门诊；11 个诊断视图的旧链接全部原样可用。</div>
  {_render_data_track_view_nav("diag")}
  <div class="diag-cards">{''.join(cards)}</div>
</main></body></html>"""


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


def _render_data_track_page(payload: dict, funnel: dict | None = None) -> str:
    # funnel = db.admin_funnel_snapshot 的产物（admin_core 扇出后传入）；
    # 未传 / builder 失败 → None → _render_funnel 渲染「暂不可用」。
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
        # users 不再是默认视图（view 缺省=首页），本页自链必须显式 view=users。
        href = _data_track_page_href(sort=metric, dir=direction, offset=0, view="users")
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
            pager_links.append(f"<a class='sort-button' href='{html.escape(_data_track_page_href(offset=prev_offset, view='users'), quote=True)}'>Prev</a>")
        if next_offset is not None:
            pager_links.append(f"<a class='sort-button' href='{html.escape(_data_track_page_href(offset=next_offset, view='users'), quote=True)}'>Next</a>")
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
        responder = row.get("responder") or {}
        runtime_state = str(responder.get("runtime_state") or "resident")
        runtime_cls = (
            "ok" if runtime_state == "v2"
            else "warn" if runtime_state == "draining"
            else "muted"
        )
        runtime_label = {
            "v2": "Hosted V2",
            "draining": "Draining",
            "resident": "Resident / V1",
        }.get(runtime_state, runtime_state or "unknown")
        effective_responder = str(
            responder.get("effective_responder") or "none"
        )
        rows_html.append(
            "<tr>"
            f"<td><a href='{html.escape(user_url)}'>{html.escape(row['user_id'])}</a>"
            f"<br><span class='muted'>{html.escape(principal_short)} · keys {access.get('api_keys_count', 0)}</span></td>"
            f"<td>{html.escape(row['route'])}</td>"
            f"<td><span class='pill {runtime_cls}'>{html.escape(runtime_label)}</span>"
            f"<br><span class='muted'>{html.escape(effective_responder)}</span></td>"
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
    runtime_state_counts = summary.get("runtime_state_counts") or {}
    activated_runtime_state_counts = (
        summary.get("activated_runtime_state_counts") or {}
    )
    # 头条用「激活用户」(真正用起来的人),不用「注册」——注册行含重装/换机产生的孤儿号,
    # 每次静默重注册都新建一行且旧行不删,所以不是人数。principals_total 在 prod 恒等于
    # 注册行(每次重装新密钥=新 principal),去重无意义,不再展示。
    metrics = "".join([
        _render_metric("激活用户（真正用起来的人）", activated),
        _render_metric("Hosted V2 账号行（当前筛选）", _fmt_count(runtime_state_counts.get("v2", 0))),
        _render_metric("已激活 Hosted V2 账号行（当前筛选）", _fmt_count(activated_runtime_state_counts.get("v2", 0))),
        _render_metric("Draining 账号行（当前筛选）", _fmt_count(runtime_state_counts.get("draining", 0))),
        _render_metric("Resident / V1 账号行（当前筛选）", _fmt_count(runtime_state_counts.get("resident", 0))),
        _render_metric("累计注册行（含重装孤儿·非人数）", raw_rows),
        _render_metric("真人活跃 1d/3d（有人发消息）", f"{summary.get('human_active_1d', 0)} / {summary.get('human_active_3d', 0)}"),
        _render_metric("系统活跃 1d/3d（含后台任务）", f"{fn.get('active_1d', 0)} / {fn.get('active_3d', 0)}"),
        _render_metric("连接异常 掉线/有去无回", f"{off} / {stalled}"),
        _render_metric("在聊（收到过AI回复）", f"{fn.get('chatting', 0)} · {_pct(fn.get('chatting'))}"),
        _render_metric("onboarding 完成", summary["onboarding_completed"]),
        _render_metric("聊天消息总数", summary["chat_messages_total"]),
        _render_metric("记忆总数", summary["memory_total"]),
        _render_metric("主动任务数", summary["proactive_jobs_total"]),
        _render_metric(
            "模型配置待处理",
            summary.get("provider_needs_user_action", 0),
        ),
    ])
    # 头屏判决句：三个数都来自本 payload。「在队列」这里只能用 payload 已有
    # 的近似口径（掉线 + 有去无回 + 模型配置待处理）；首页队列是逐用户按消息
    # 表判定的独立 builder，两者近似但非同源，hint 里说清楚。
    queue_ish = off + stalled + int(summary.get("provider_needs_user_action") or 0)
    verdict_line = (
        "<div class='verdict-line'>"
        f"<b>{activated}</b> 个激活真人用户 · "
        f"<b>{int(summary.get('human_active_1d') or 0)}</b> 个近1日发过消息 · "
        f"<b>{queue_ish}</b> 个在队列"
        "<span class='hint' title='激活=写过记忆或发过消息的账号行；近1日发过消息=滚动 24h 内有真人消息（比「日活与时长」页的 打开过App DAU 严格，两者不同尺，无需对上）；"
        "在队列=连接掉线+有去无回+模型配置待处理（与首页「需要你的用户」口径近似但非同源，"
        "首页按消息表逐用户判定）'>?</span>"
        "</div>"
    )
    # 旧「Activation funnel」六格条形图退役：各分母互不包含（有记忆⊅发过
    # 消息），画成漏斗视觉上暗示单调递减，实际可以反超。真正的单调漏斗由
    # admin_funnel_snapshot（同一 28 天注册 cohort 的里程碑子集）供给，用
    # _render_funnel 渲染；这些独立占比仍有用，收进下方「人群与账号行记账」。
    behavior_steps = [
        ("registered", "注册行（含重装孤儿·非人数）"),
        ("has_memory", "有记忆"),
        ("sent_first_message", "发过消息"),
        ("chatting", "收到AI回复(真在聊)"),
        ("active_3d", "近3天活跃"),
        ("active_1d", "近1天活跃"),
    ]
    behavior_lines = "".join(
        f"<div>{html.escape(label)}：{int(fn.get(k) or 0)} · {_pct(fn.get(k))}</div>"
        for k, label in behavior_steps
    )
    accounting_details = (
        "<details class='note-box'><summary>人群与账号行记账（全部口径数字）</summary>"
        "<b>怎么读这些数：</b>"
        "「<b>激活用户</b>」= 写过记忆或发过消息的人，<b>最接近真实用户数</b>。"
        "「<b>累计注册行</b>」= 装过 app、生成过密钥的账户行累计；同一个人重装 / 换机 / 抹机后"
        " app 会静默注册一个新号、旧号变孤儿不删，所以它<b>不是人数</b>、会远大于发出的邀请/邮件数。"
        "删除账户是硬删（行直接消失），因此没有、也无法有「已删除账户数」这个指标。"
        f"<section class='metrics'>{metrics}</section>"
        "<b>行为口径独立占比</b>（各项分母=注册行；口径互不包含，<b>不构成漏斗</b>，数字可以不单调）："
        f"{behavior_lines}"
        "</details>"
    )
    runtime_filter_links = []
    selected_runtime_state = str(filters.get("runtime_state") or "")
    for state, label in (
        ("", "全部 runtime"),
        ("v2", "Hosted V2"),
        ("draining", "Draining"),
        ("resident", "Resident / V1"),
    ):
        cls = "sort-button active" if selected_runtime_state == state else "sort-button"
        href = _data_track_page_href(
            view="users", runtime_state=state or None, offset=0
        )
        runtime_filter_links.append(
            f"<a class='{cls}' href='{html.escape(href, quote=True)}'>{html.escape(label)}</a>"
        )
    runtime_population_section = (
        "<h2>Runtime 人群</h2>"
        f"<div class='sortbar'>{''.join(runtime_filter_links)}</div>"
        "<div class='muted'>这里按实际 <code>hosted_runtime_state</code> 筛选，"
        "不是 onboarding route。这里计的是 user/account rows，不是去重真人；重装旧号可能重复。"
        "“已激活”仍按有记忆或发过消息的行为口径。Token 与模型用量已移到独立页面，避免把只统计 chat lane 的旧 rollup 误称为全站用量。"
        f" <a href='{html.escape(_usage_page_href(), quote=True)}'>打开 Token 与模型</a>。</div>"
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
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedling Beta Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; --ok:#1d7a4d; --warn:#a05a00; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:1280px; margin:0 auto; padding:28px 24px 48px; }}
    h1 {{ font-size:26px; margin:0 0 4px; }}
    h2 {{ font-size:16px; margin:28px 0 12px; }}
    .muted {{ color:var(--muted); }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:22px 0; }}
    .metric {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .metric-value {{ font-size:24px; font-weight:700; }}
    .metric-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
    .note-box {{ background:#fff8ef; border:1px solid #e8d8be; border-radius:8px; padding:12px 14px; margin:16px 0 4px; font-size:13px; line-height:1.6; color:#5a4d3c; }}
	    .toolbar {{ display:flex; gap:10px; align-items:center; margin:18px 0; }}
	    .viewbar,.sortbar,.pager {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:10px 0 18px; }}
	    .sort-button {{ display:inline-flex; min-height:44px; box-sizing:border-box; align-items:center; justify-content:center; border:1px solid var(--line); border-radius:6px; padding:9px 12px; background:var(--card); color:var(--fg); font-size:13px; }}
	    .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#e7eee9; }}
	    input {{ width:320px; max-width:100%; border:1px solid var(--line); border-radius:6px; padding:9px 10px; background:white; color:var(--fg); }}
	    .toolbar button {{ border:0; border-radius:6px; padding:9px 13px; background:var(--accent); color:white; font-weight:600; cursor:pointer; }}
    table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
    th {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; background:#f4ece5; }}
    tr:last-child td {{ border-bottom:0; }}
    a {{ color:var(--accent); text-decoration:none; }}
    .pill {{ display:inline-flex; border-radius:999px; padding:2px 8px; font-size:12px; background:#efe7df; color:var(--muted); }}
    .pill.ok {{ color:var(--ok); background:#e7f3ed; }}
    .pill.warn {{ color:var(--warn); background:#fff1db; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .table-wrap {{ max-width:100%; overflow-x:auto; }}
    .hint {{ display:inline-block; margin-left:5px; width:14px; height:14px; line-height:14px; text-align:center; border:1px solid var(--line); border-radius:999px; background:var(--card); color:var(--muted); font-size:10px; text-transform:none; letter-spacing:0; cursor:help; vertical-align:3px; }}
    details.note-box summary {{ cursor:pointer; font-weight:700; color:#7a6a52; }}
    details.note-box[open] summary {{ margin-bottom:8px; }}
{_NAV_GROUP_CSS}
{_HOME_WIDGET_CSS}
  </style>
</head>
<body>
<main>
	  <h1>Feedling Beta Data Track</h1>
	  <div class="muted">Generated {html.escape(_bj_iso(summary["generated_at"]))}. Metadata only; encrypted content is not read or rendered.</div>
	  <div class="muted">Showing {html.escape(str(pagination.get("returned", len(users))))} of {html.escape(str(pagination.get("total", summary["users_total"])))} filtered users. Since {html.escape(str(filters.get("since") or "all time"))}.</div>
	  {_render_data_track_view_nav("users")}
	  {verdict_line}
	  <h2>激活漏斗 <span class="h2-sub">近 28 完整日注册 cohort · 含上一窗口对照</span></h2>
	  {_render_funnel(funnel, compact=False)}
	  {accounting_details}
		  {runtime_population_section}
		  {app_usage_section}
	  <h2>Beta users</h2>
	  <form class="toolbar" method="get" action="/admin/data-track/users">
	    <input name="admin_key" type="hidden" value="{html.escape(request.args.get('admin_key', ''), quote=True)}">
	    <input name="view" type="hidden" value="users">
	    <input name="uid" placeholder="输入 UID 直查（usr_…）" autocomplete="off">
	    <button type="submit">打开用户详情</button>
	  </form>
	  <div class="toolbar"><input id="q" placeholder="筛选 UID、route、runtime state、stage"></div>
	  <div class="sortbar">{sort_controls}</div>
	  {pager}
	  <div class="table-wrap"><table id="users">
    <thead><tr><th>User</th><th>Onboarding route</th><th>实际 Runtime</th><th>连接</th><th>Onboarding</th><th>Steps</th><th>Chat</th><th>Memory</th><th>Proactive 心跳/屏幕(fail)</th><th>Last activity</th></tr></thead>
    <tbody>{''.join(rows_html) if rows_html else "<tr><td colspan='10' class='muted'>No users yet.</td></tr>"}</tbody>
  </table></div>
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
    # 「加载更多日期」：本页服务端渲染，无 SPA。默认 days=30，点一次 +30，封顶
    # 366（一年，DB / filters 两侧的硬上限）。链接经 _data_track_qs 保住 admin_key
    # 与 since/day 等现有参数。到顶后换成说明文案。
    _current_days = int(filters.get("days") or 30)
    _DAU_DAYS_MAX = 366
    if _current_days < _DAU_DAYS_MAX:
        _next_days = min(_current_days + 30, _DAU_DAYS_MAX)
        _more_href = _data_track_page_href(
            view="dau", days=_next_days, day=histogram_day, offset=0,
        )
        _more_all_href = _data_track_page_href(
            view="dau", days=_DAU_DAYS_MAX, day=histogram_day, offset=0,
        )
        load_more_html = (
            "<div class='toolbar' style='margin-top:12px'>"
            f"<a class='sort-button' href='{html.escape(_more_href, quote=True)}'>"
            f"↓ 加载更多日期（{_current_days} → {_next_days} 天）</a>"
            f"<a class='sort-button' href='{html.escape(_more_all_href, quote=True)}'>"
            f"加载全部（最多 {_DAU_DAYS_MAX} 天 · 约一年）</a>"
            "</div>"
        )
    else:
        load_more_html = (
            "<div class='muted' style='margin-top:12px'>"
            f"已加载到最大范围 {_DAU_DAYS_MAX} 天（约一年）——更早的数据需提高后端上限。"
            "</div>"
        )
    rows_html = []
    for row in rows:
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('day') or ''))}</td>"
            + ("<td><span style='color:#1d7a4d;font-size:12px'>🔒 已冻结</span></td>"
               if row.get("frozen")
               else "<td><span style='color:#a05a00;font-size:12px'>⏱ 实时</span></td>")
            + f"<td><b>{int(row.get('session_dau') or 0)}</b></td>"
            f"<td>{int(row.get('chat_dau') or 0)}</td>"
            f"<td>{int(row.get('tracking_dau') or 0)}</td>"
            f"<td>{int(row.get('active_events') or 0)}</td>"
            f"<td>{int(row.get('user_messages') or 0)}</td>"
            f"<td>{int(row.get('tracking_events') or 0)}</td>"
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
        _render_metric("latest DAU · 打开App", summary["latest_dau"]),
        _render_metric("latest day", summary.get("latest_day") or "n/a"),
        _render_metric("max DAU · 打开App", summary["max_dau"]),
        _render_metric("avg DAU · 打开App", f"{summary['avg_dau']:.1f}"),
        _render_metric("user messages", summary["user_messages"]),
        _render_metric("tracking events", summary["tracking_events"]),
    ])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedling DAU · Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; }}
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
    .sort-button {{ display:inline-flex; min-height:44px; box-sizing:border-box; align-items:center; justify-content:center; border:1px solid var(--line); border-radius:6px; padding:9px 12px; background:var(--card); color:var(--fg); font-size:13px; }}
    .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#e7eee9; }}
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
{_NAV_GROUP_CSS}
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
  <div class="muted"><b>DAU = 使用 DAU</b>=当天<b>真正打开过 App</b> 的用户数（有 app_session_end 前台会话）；此列已冻结进快照，历史天一致。<b>Chat DAU</b>=当天发过用户消息的人；<b>Tracking DAU</b>=当天有任意 tracking 事件的人（含后台/proactive 遥测，口径最松、仅作拆分参考——旧的「广义 DAU」就等于它，已去掉）。人均日使用时长=当天总前台时长÷DAU；中位数=每用户当天前台总时长的中位数（典型用户，和均值并看更可信）；会话数=app_session_end 事件数（含大量前后台切换微会话）。前台被强杀会漏报，略偏低估。均按北京日。<b>留存/增长也改用此 DAU（打开 App）口径</b>。</div>
  <div class="toolbar"><a class="sort-button" href="{html.escape(api_url, quote=True)}">JSON</a></div>
  <table>
    <thead><tr><th>Beijing day</th><th>状态</th><th>DAU(打开App)</th><th>Chat DAU</th><th>Tracking DAU</th><th>Active events</th><th>User messages</th><th>Tracking events</th><th>人均日使用时长</th><th>中位数日使用时长</th><th>会话数</th><th>Last active</th></tr></thead>
    <tbody>{''.join(rows_html) if rows_html else "<tr><td colspan='13' class='muted'>No DAU activity in this range.</td></tr>"}</tbody>
  </table>
  {load_more_html}
  <div class="muted" style="margin-top:12px">使用时长分布口径：先汇总每位用户在所选北京日的全部 app_session_end duration_sec，再按固定右开区间分桶。样本=当天有上报的 {histogram_total} 位用户；没打开 App 或没有 app_session_end 上报的用户不计入，也不会补 0。</div>
</main>
</body>
</html>"""


def _data_track_growth_payload() -> dict:
    filters = _data_track_request_filters()
    days = int(filters.get("days") or 60)
    growth = db.admin_data_track_growth(days=days, tz="Asia/Shanghai")
    # Retention now uses classic day-N cohorts, restricted to signup days on/after
    # the freeze boundary (dau snapshot first_day). Pre-freeze days are excluded
    # entirely per the product decision — their live recompute drifts as accounts
    # delete, so they were never trustworthy for retention/growth.
    dau_bounds = db.admin_dau_snapshot_bounds()
    freeze_day = str(dau_bounds.get("first_day") or "")
    retention = db.admin_data_track_retention_daily(
        tz="Asia/Shanghai", since_day=freeze_day, granularity="day"
    )
    retention_week = db.admin_data_track_retention_daily(
        tz="Asia/Shanghai", since_day=freeze_day, granularity="week"
    )
    accounting = db.admin_data_track_growth_accounting(
        tz="Asia/Shanghai", since_day=freeze_day
    )
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
        "freeze_day": freeze_day,
        "cohort_count": len(retention.get("cohorts") or []),
    }
    return {
        "summary": summary,
        "filters": {"days": days, "view": "growth"},
        "growth": growth,
        "retention": retention,
        "retention_week": retention_week,
        "accounting": accounting,
    }


_GROWTH_STYLE = _NAV_GROUP_CSS + """
    :root { color-scheme: light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; }
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
    .sort-button { display:inline-flex; min-height:44px; box-sizing:border-box; align-items:center; justify-content:center; border:1px solid var(--line); border-radius:6px; padding:9px 12px; background:var(--card); color:var(--fg); font-size:13px; }
    .sort-button.active { border-color:var(--accent); color:var(--accent); background:#e7eee9; }
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


def _svg_retention_lines(retention: dict, *, width: int = 640, height: int = 240) -> str:
    """Multi-cohort retention curve: X = day offset (D1..D30), Y = %, one
    polyline per cohort (mature cells only). Inline SVG — no JS."""
    offsets = retention.get("offsets") or [1, 3, 7, 14, 30]
    cohorts = [c for c in retention.get("cohorts", [])
               if any(c["cells"].get(n) is not None for n in offsets)]
    if not cohorts:
        return "<div class='muted'>暂无足够成熟的 cohort 画留存曲线（都还不满 D1）。</div>"
    pad_l, pad_r, pad_t, pad_b = 34, 104, 12, 26
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    maxoff = max(offsets) or 1

    def px(n: int) -> float:
        return pad_l + pw * (n / maxoff)

    def py(v: float) -> float:
        return pad_t + ph * (1 - v / 100.0)

    parts = [f"<svg viewBox='0 0 {width} {height}' width='100%' "
             f"style='max-width:{width}px;background:var(--card);"
             f"border:1px solid var(--line);border-radius:8px'>"]
    for g in (0, 25, 50, 75, 100):
        yy = py(g)
        parts.append(f"<line x1='{pad_l}' y1='{yy:.1f}' x2='{pad_l+pw}' y2='{yy:.1f}' stroke='#eee'/>")
        parts.append(f"<text x='{pad_l-5}' y='{yy+3:.1f}' font-size='9' text-anchor='end' fill='#999'>{g}%</text>")
    for n in offsets:
        parts.append(f"<text x='{px(n):.1f}' y='{height-9}' font-size='9' text-anchor='middle' fill='#999'>D{n}</text>")
    palette = ['#b7352b', '#1d7a4d', '#2f6fb0', '#c07800', '#7a3fa0', '#496b2e', '#9c4f6b', '#37868a']
    for i, c in enumerate(cohorts[:8]):
        col = palette[i % len(palette)]
        pts = [f"{px(n):.1f},{py(v):.1f}" for n in offsets
               if (v := c["cells"].get(n)) is not None]
        if pts:
            parts.append(f"<polyline points='{' '.join(pts)}' fill='none' stroke='{col}' stroke-width='1.6'/>")
            for p in pts:
                xx, yy = p.split(",")
                parts.append(f"<circle cx='{xx}' cy='{yy}' r='2' fill='{col}'/>")
        parts.append(f"<text x='{pad_l+pw+8}' y='{pad_t+13*i+9}' font-size='9' fill='{col}'>{html.escape(str(c['cohort']))}</text>")
    parts.append("</svg>")
    return "".join(parts)


def _render_retention_table(retention: dict, *, unit_label: str) -> str:
    offsets = retention.get("offsets", [1, 3, 7, 14, 30])
    head = "".join(f"<th>D{n}</th>" for n in offsets)
    body = []
    for c in retention.get("cohorts", []):
        cells = []
        for n in offsets:
            pct = c["cells"].get(n)
            cells.append("<td class='muted'>—</td>" if pct is None
                         else f"<td style='{_retention_cell_bg(pct)}'>{pct:.0f}%</td>")
        body.append(f"<tr><td><b>{html.escape(str(c['cohort']))}</b></td>"
                    f"<td>{int(c['size'])}</td>{''.join(cells)}</tr>")
    empty = f"<tr><td colspan='{len(offsets)+2}' class='muted'>暂无冻结后 cohort 数据</td></tr>"
    return (f"<table><thead><tr><th>{unit_label}</th><th>新增数</th>{head}</tr></thead>"
            f"<tbody>{''.join(body) if body else empty}</tbody></table>")


def _svg_growth_accounting(accounting: dict, *, width: int = 640, height: int = 220) -> str:
    """Stacked growth-accounting bars: new + resurrected above the zero line,
    churned below it — one bar per day (the baseline first day is skipped)."""
    rows = [r for r in accounting.get("rows", []) if r.get("churned") is not None]
    if not rows:
        return "<div class='muted'>暂无足够天数做增长核算柱状图。</div>"
    pad_l, pad_r, pad_t, pad_b = 34, 10, 14, 22
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    up = [(r.get("new") or 0) + (r.get("resurrected") or 0) for r in rows]
    dn = [r.get("churned") or 0 for r in rows]
    scale = max(max(up, default=1), max(dn, default=1), 1)
    mid = pad_t + ph / 2
    n = len(rows)
    slot = pw / max(n, 1)
    bw = min(slot * 0.7, 26)

    def h(v: int) -> float:
        return (ph / 2) * (v / scale)

    parts = [f"<svg viewBox='0 0 {width} {height}' width='100%' "
             f"style='max-width:{width}px;background:var(--card);"
             f"border:1px solid var(--line);border-radius:8px'>"]
    parts.append(f"<line x1='{pad_l}' y1='{mid:.1f}' x2='{pad_l+pw}' y2='{mid:.1f}' stroke='#bbb'/>")
    parts.append(f"<text x='{pad_l-4}' y='{pad_t+8}' font-size='9' text-anchor='end' fill='#999'>+{scale}</text>")
    parts.append(f"<text x='{pad_l-4}' y='{pad_t+ph:.0f}' font-size='9' text-anchor='end' fill='#999'>-{scale}</text>")
    for i, r in enumerate(rows):
        x = pad_l + slot * i + (slot - bw) / 2
        newh, resh, chh = h(r.get("new") or 0), h(r.get("resurrected") or 0), h(r.get("churned") or 0)
        parts.append(f"<rect x='{x:.1f}' y='{mid-newh:.1f}' width='{bw:.1f}' height='{newh:.1f}' fill='#b7352b'/>")
        parts.append(f"<rect x='{x:.1f}' y='{mid-newh-resh:.1f}' width='{bw:.1f}' height='{resh:.1f}' fill='#1d7a4d'/>")
        parts.append(f"<rect x='{x:.1f}' y='{mid:.1f}' width='{bw:.1f}' height='{chh:.1f}' fill='#d9b48a'/>")
    step = max(1, n // 8)
    for i in range(0, n, step):
        x = pad_l + slot * i + slot / 2
        parts.append(f"<text x='{x:.1f}' y='{height-7}' font-size='8' text-anchor='middle' fill='#999'>{html.escape(str(rows[i]['day'])[5:])}</text>")
    parts.append("</svg>")
    return "".join(parts)


def _svg_growth_curve(growth: list, *, width: int = 640, height: int = 220) -> str:
    """Cumulative users on a LOG Y axis: a straight line ⇒ exponential growth,
    bending down ⇒ slowing (log-scale diagnostic from the spec)."""
    import math
    pts = [(str(r.get("day") or ""), int(r.get("cumulative") or 0)) for r in growth]
    pts = [(d, v) for d, v in pts if v > 0]
    if len(pts) < 2:
        return "<div class='muted'>累计数据不足画增长曲线。</div>"
    pad_l, pad_r, pad_t, pad_b = 44, 10, 12, 22
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    vals = [v for _, v in pts]
    vmax, vmin = max(vals), max(1, min(vals))
    lo = math.log10(vmin)
    hi = math.log10(vmax) if vmax > vmin else lo + 1
    n = len(pts)

    def px(i: int) -> float:
        return pad_l + pw * (i / (n - 1))

    def py(v: int) -> float:
        return pad_t + ph * (1 - (math.log10(max(1, v)) - lo) / (hi - lo))

    parts = [f"<svg viewBox='0 0 {width} {height}' width='100%' "
             f"style='max-width:{width}px;background:var(--card);"
             f"border:1px solid var(--line);border-radius:8px'>"]
    p = math.floor(lo)
    while 10 ** p <= vmax * 1.001:
        if 10 ** p >= vmin * 0.999:
            yy = py(10 ** p)
            parts.append(f"<line x1='{pad_l}' y1='{yy:.1f}' x2='{pad_l+pw}' y2='{yy:.1f}' stroke='#eee'/>")
            parts.append(f"<text x='{pad_l-5}' y='{yy+3:.1f}' font-size='9' text-anchor='end' fill='#999'>{10**p}</text>")
        p += 1
    poly = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, (_, v) in enumerate(pts))
    parts.append(f"<polyline points='{poly}' fill='none' stroke='#b7352b' stroke-width='1.8'/>")
    step = max(1, n // 8)
    for i in range(0, n, step):
        parts.append(f"<text x='{px(i):.1f}' y='{height-7}' font-size='8' text-anchor='middle' fill='#999'>{html.escape(pts[i][0][5:])}</text>")
    parts.append("</svg>")
    return "".join(parts)


def _render_data_track_growth_page(payload: dict) -> str:
    summary = payload["summary"]
    growth = payload.get("growth", [])
    retention = payload.get("retention", {})
    retention_week = payload.get("retention_week", {})
    accounting = payload.get("accounting", {})
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

    retention_chart = _svg_retention_lines(retention)
    accounting_chart = _svg_growth_accounting(accounting)
    growth_curve = _svg_growth_curve(growth)
    ret_day_table = _render_retention_table(retention, unit_label="注册日")
    ret_week_table = _render_retention_table(retention_week, unit_label="注册周(周一)")

    def _opt(v):
        return "—" if v is None else str(int(v))

    acct_rows = []
    for r in accounting.get("rows", []):
        qr = r.get("quick_ratio")
        churn = r.get("churned")
        churn_txt = "—" if churn is None else (f"-{int(churn)}" if churn else "0")
        acct_rows.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('day') or ''))}</td>"
            f"<td>{int(r.get('active') or 0)}</td>"
            f"<td><b>{int(r.get('new') or 0)}</b></td>"
            f"<td>{_opt(r.get('resurrected'))}</td>"
            f"<td>{_opt(r.get('retained'))}</td>"
            f"<td style='color:#a05a00'>{churn_txt}</td>"
            f"<td>{'—' if qr is None else f'{qr:.2f}'}</td>"
            "</tr>"
        )
    acct_rows.reverse()  # newest first

    metrics = "".join([
        _render_metric("累计用户", summary["total_users"]),
        _render_metric("最新一天新增", summary["latest_new"]),
        _render_metric("cohort 数", summary["cohort_count"]),
        _render_metric("已冻结天数", summary["snapshot_days"]),
    ])
    _freeze = html.escape(str(summary.get("freeze_day") or ""))
    boundary = (f"留存只统计<b>冻结边界 {_freeze} 起</b>注册的 cohort;此日之前的历史实时重算、会随删号偏少不准,已按产品决策<b>整体排除</b>,不进本页。"
                if _freeze else "每日快照尚未生效;生效后本页只统计冻结边界起注册的 cohort。")
    return f"""<!doctype html>
<html lang="zh-CN">
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
    留存为<b>日 cohort · 经典 Day-N</b>(行=注册北京日,列 D_N=注册后第 N 天仍活跃的占比;D0=100% 定义上省略);<b>—</b>=该 cohort 距今不足 N 天、还判不了,不算 0。分母=当天注册人数。<br>
    <b>用户基数极小</b> → 曲线抖动大,当方向性参考,非统计显著。<b>③ 完全自部署</b>用户不在此表(不在我们后端注册)。<b>"活跃"=使用 DAU=当天真打开过 App(app_session_end)</b>,不含只有后台/proactive 遥测的用户。
  </div>
  <div class="toolbar"><a class="sort-button" href="{html.escape(api_url, quote=True)}">JSON</a></div>
  <h2>用户增长(新增 + 累计)</h2>
  <table>
    <thead><tr><th>Beijing day</th><th>状态</th><th>新增</th><th>累计</th></tr></thead>
    <tbody>{''.join(growth_rows) if growth_rows else "<tr><td colspan='4' class='muted'>暂无注册数据</td></tr>"}</tbody>
  </table>
  <h2>累计用户 · log 轴增长曲线</h2>
  <div class="muted" style="margin-bottom:8px">Y 轴为对数刻度:<b>直线=指数增长</b>,<b>上弯=加速</b>,<b>下弯=放缓</b>。全量累计(注册即计,不受冻结边界限制)。</div>
  {growth_curve}
  <h2>Growth Accounting · 每日(新增/回流/留存/流失 + Quick Ratio)</h2>
  <div class="muted" style="margin-bottom:8px">活跃=使用 DAU=当天真打开过 App(app_session_end)。<b>新增</b>=当天注册;<b>回流</b>=今天活跃、之前注册过、但昨天没活跃;<b>留存</b>=今昨都活跃;<b>流失</b>=昨天活跃今天没(负);<b>Quick Ratio</b>=(新增+回流)/流失,&gt;1 才是净增长。首日为基线无环比。仅冻结边界 {_freeze} 起。</div>
  <table>
    <thead><tr><th>Beijing day</th><th>活跃</th><th>新增</th><th>回流</th><th>留存</th><th>流失</th><th>Quick Ratio</th></tr></thead>
    <tbody>{''.join(acct_rows) if acct_rows else "<tr><td colspan='7' class='muted'>暂无足够天数做增长核算</td></tr>"}</tbody>
  </table>
  <div class="muted" style="margin:6px 0">堆叠柱:<b style="color:#b7352b">■ 新增</b> + <b style="color:#1d7a4d">■ 回流</b> 向上,<b style="color:#c79a63">■ 流失</b> 向下;零线以上净增、以下净减。</div>
  {accounting_chart}
  <h2>留存曲线 · Day-N（{_freeze} 起）</h2>
  <div class="muted" style="margin-bottom:8px">每条线=一个注册日 cohort;X=注册后天数,Y=当日留存率(仅画已成熟档位)。基数小、当方向性参考。</div>
  {retention_chart}
  <h2>日 cohort 留存表 · Day-N（{_freeze} 起）</h2>
  {ret_day_table}
  <h2>周 cohort 留存表 · Day-N(按注册周聚合,周一标签)</h2>
  {ret_week_table}
</main>
</body>
</html>"""


# ---- 产品健康（?view=health）· 固定周口径，无 hours 窗口 --------------------

# _RUNTIME_PAGE_CSS 没有 h3 规则，浏览器默认 h3 比本页 16px 的 h2 还大，
# 层级会倒挂；只在本页补一条，不动共享样式。
_HEALTH_PAGE_CSS = _RUNTIME_PAGE_CSS + """
    h3 { font-size:14px; margin:20px 0 10px; }
"""

# 任一 builder 失败即整段坍缩成这一行——绝不把「查不到」渲染成 0。
_HEALTH_UNAVAILABLE = (
    "<div class='note-box'>统计暂不可用。页面不会把未知渲染成 0。</div>"
)


def _health_bj_this_monday() -> str:
    """当前北京自然周的周一（ISO）。进行中的周在多处需要右删失处理。"""
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return (today - timedelta(days=today.weekday())).isoformat()


def _health_t3_matured(cohort_week: str) -> bool:
    """注册周的 t3 激活窗是否已全部走完（注册周 7 天 + 3 天 t3 窗）。

    coverage_complete 只说明漏斗行数对得上，不代表窗口走完；没成熟的周
    t3 率是右删失下限，当定论展示就是把「还没来得及」渲染成 0%。
    """
    try:
        week = date.fromisoformat(str(cohort_week))
    except (TypeError, ValueError):
        return False
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return week + timedelta(days=10) <= today


def _health_signed(value) -> str:
    """净变化带符号显示；None（基线周判不了）→ —，与真实 0 区分。"""
    if value is None:
        return "—"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "—"
    return f"+{n:,}" if n > 0 else f"{n:,}"


def _health_cohort_table(retention: dict | None) -> str:
    if retention is None:
        return _HEALTH_UNAVAILABLE
    periods = [int(p) for p in (retention.get("periods") or [1, 2, 4, 8])]
    head = "".join(f"<th>W{p}</th>" for p in periods)
    rows = []
    for c in retention.get("cohorts", []):
        n = c.get("n")
        cells = []
        for p in periods:
            cell = (c.get("cells") or {}).get(p)
            pct = cell.get("pct") if isinstance(cell, dict) else None
            if pct is None:
                # 未成熟（cohort 距今不满 p 周）不是 0，判不了就是判不了。
                cells.append("<td class='muted' title='cohort 尚未满该周数，未成熟'>—</td>")
            else:
                pct = float(pct)
                cells.append(
                    f"<td style='{_retention_cell_bg(pct)}'"
                    f" title='{_fmt_count(cell.get('active'))}/{_fmt_count(n)} 活跃'>"
                    f"{pct:.0f}%</td>"
                )
        rows.append(
            f"<tr><td>{html.escape(str(c.get('cohort_week') or ''))}"
            f"（n={_fmt_count(n)}）</td>{''.join(cells)}</tr>"
        )
    if not rows:
        rows.append(
            f"<tr><td colspan='{1 + len(periods)}' class='muted'>暂无注册周 cohort</td></tr>"
        )
    return (
        "<div class='table-wrap'><table>"
        f"<thead><tr><th>注册周（周一）</th>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _health_l_table(stickiness: dict | None) -> str:
    if stickiness is None:
        return _HEALTH_UNAVAILABLE
    ld = stickiness.get("l_distribution") or {}
    head = "".join(f"<th>L{k}</th>" for k in range(1, 8))
    cells = "".join(f"<td>{_fmt_count(ld.get(k))}</td>" for k in range(1, 8))
    return (
        "<div class='table-wrap'><table>"
        f"<thead><tr><th>近 7 完整日活跃天数</th>{head}</tr></thead>"
        f"<tbody><tr><td class='muted'>用户数</td>{cells}</tr></tbody></table></div>"
    )


def _health_growth_table(growth: dict | None) -> str:
    if growth is None:
        return _HEALTH_UNAVAILABLE
    this_monday = _health_bj_this_monday()
    rows = []
    for r in growth.get("rows", []):
        week = str(r.get("week") or "")
        # 进行中的本周要标出来：builder 已把流失/回流/净变化发成 None（→ —），
        # 但不标注的话读者会把「—」误读成没数据而不是没走完。
        tag = (
            "<span class='muted' title='本周未走完，流失/回流/净变化不可判定'>"
            "（进行中）</span>"
            if week == this_monday else ""
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(week)}{tag}</td>"
            f"<td>{_fmt_count(r.get('new_registered'))}</td>"
            f"<td>{_fmt_count(r.get('newly_activated'))}</td>"
            f"<td>{_fmt_count(r.get('active'))}</td>"
            f"<td>{_fmt_count(r.get('churned'))}</td>"
            f"<td>{_fmt_count(r.get('resurrected'))}</td>"
            f"<td>{_health_signed(r.get('net_change'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='7' class='muted'>暂无足够周数做增长核算</td></tr>")
    return (
        "<div class='table-wrap'><table>"
        "<thead><tr><th>周（周一）</th><th>新注册</th><th>新激活</th><th>活跃</th>"
        "<th>流失</th><th>回流</th><th>净变化</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _health_activation_table(activation: dict | None) -> str:
    if activation is None:
        return _HEALTH_UNAVAILABLE

    def pct_cell(count, n) -> str:
        try:
            c, total = int(count), int(n)
        except (TypeError, ValueError):
            return "<td class='muted'>—</td>"
        if total <= 0:
            return "<td class='muted'>—</td>"
        return f"<td>{c / total * 100:.0f}%（{c:,}）</td>"

    unknown = "<td class='muted' title='cohort 事件覆盖不完整，判不了'>未知</td>"
    immature = (
        "<td class='muted' title='注册周 + 3 天 t3 窗尚未走完，右删失，判不了'>"
        "未成熟</td>"
    )
    rows = []
    for c in activation.get("cohorts", []):
        week = html.escape(str(c.get("cohort_week") or ""))
        n = _fmt_count(c.get("n"))
        if not c.get("coverage_complete"):
            # 覆盖不完整的周渲染「未知」而不是 0%——缺证据 ≠ 没激活。
            rows.append(f"<tr><td>{week}</td><td>{n}</td>{unknown * 4}</tr>")
            continue
        if not _health_t3_matured(c.get("cohort_week")):
            # 窗口没走完的周（本周/上周）渲染「未成熟」——注册 3 天内还没
            # 回复 ≠ 不会回复，确定性的 0% 是编出来的悲观。
            rows.append(f"<tr><td>{week}</td><td>{n}</td>{immature * 4}</tr>")
            continue
        median_h = c.get("median_t0_t3_hours")
        median = _funnel_dur(median_h * 3600 if median_h is not None else None)
        rows.append(
            f"<tr><td>{week}</td><td>{n}</td>"
            f"{pct_cell(c.get('t1'), c.get('n'))}"
            f"{pct_cell(c.get('t2'), c.get('n'))}"
            f"{pct_cell(c.get('t3'), c.get('n'))}"
            f"<td>{median}</td></tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6' class='muted'>暂无注册周 cohort</td></tr>")
    return (
        "<div class='table-wrap'><table>"
        "<thead><tr><th>注册周（周一）</th><th>n</th><th>t1 内激活</th>"
        "<th>t2 内激活</th><th>t3 内激活</th><th>中位 t0→t3</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _health_w4_split_table(w4_split: dict | None) -> str:
    if w4_split is None:
        return _HEALTH_UNAVAILABLE

    def rate_cell(rate, active) -> str:
        if rate is None:
            return "<td class='muted'>—</td>"
        return f"<td>{_fmt_ratio(rate)}（{_fmt_count(active)}）</td>"

    rows = []
    for c in w4_split.get("cohorts", []):
        ra = c.get("w4_rate_all")
        rb = c.get("w4_rate_activated")
        lift = (
            f"×{float(rb) / float(ra):.1f}"
            if ra is not None and rb is not None and float(ra) > 0
            else "—"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(c.get('cohort_week') or ''))}</td>"
            f"<td>{_fmt_count(c.get('n_all'))}</td>"
            f"<td>{_fmt_count(c.get('n_activated'))}</td>"
            f"{rate_cell(ra, c.get('w4_all'))}"
            f"{rate_cell(rb, c.get('w4_activated'))}"
            f"<td>{lift}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6' class='muted'>暂无可判 W4 的注册周 cohort</td></tr>")
    return (
        "<div class='table-wrap'><table>"
        "<thead><tr><th>注册周（周一）</th><th>n 全量</th><th>n 激活者</th>"
        "<th>W4 全量</th><th>W4 激活者</th><th>lift</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _health_power_tables(power: dict | None) -> str:
    if power is None:
        return _HEALTH_UNAVAILABLE
    weekly_rows = []
    # builder 拿 16 周只为判满 4 周连续；趋势表只展示最近 12 周。
    for r in (power.get("weekly") or [])[:12]:
        weekly_rows.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('week') or ''))}</td>"
            f"<td>{_fmt_count(r.get('qualifying_users'))}</td>"
            f"<td>{_fmt_count(r.get('power_users'))}</td>"
            "</tr>"
        )
    if not weekly_rows:
        weekly_rows.append("<tr><td colspan='3' class='muted'>暂无周样本</td></tr>")
    monthly_rows = []
    for r in power.get("monthly") or []:
        monthly_rows.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('month') or ''))}</td>"
            f"<td>{_fmt_count(r.get('power_users'))}</td>"
            "</tr>"
        )
    if not monthly_rows:
        monthly_rows.append("<tr><td colspan='2' class='muted'>暂无月度样本</td></tr>")
    return (
        "<div class='table-wrap'><table>"
        "<thead><tr><th>周（周一）</th><th>当周达标（≥5 job）</th>"
        "<th>铁杆（连续 4 周达标）</th></tr></thead>"
        f"<tbody>{''.join(weekly_rows)}</tbody></table></div>"
        "<div class='table-wrap'><table>"
        "<thead><tr><th>月份</th><th>铁杆用户</th></tr></thead>"
        f"<tbody>{''.join(monthly_rows)}</tbody></table></div>"
    )


def _health_reply_table(reply_rate: dict | None) -> str:
    if reply_rate is None:
        return _HEALTH_UNAVAILABLE
    rows = []
    for r in reply_rate.get("rows", []):
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('week') or ''))}</td>"
            f"<td>{_fmt_count(r.get('proactive_msgs'))}</td>"
            f"<td>{_fmt_count(r.get('replied_24h'))}</td>"
            f"<td>{_fmt_count(r.get('users'))}</td>"
            f"<td>{_fmt_ratio(r.get('reply_rate'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='5' class='muted'>暂无主动消息样本</td></tr>")
    return (
        "<div class='table-wrap'><table>"
        "<thead><tr><th>周（周一）</th><th>主动消息</th><th>24h 内有回复</th>"
        "<th>触达用户</th><th>回复率</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _render_product_health_page(
    retention: dict | None,
    activation: dict | None,
    w4_split: dict | None,
    stickiness: dict | None,
    concentration: dict | None,
    growth: dict | None,
    power: dict | None,
    reply_rate: dict | None,
) -> str:
    """产品健康（?view=health）。八个 builder 各自独立失败：None → 对应
    tile 显 — / 表格坍缩成「统计暂不可用」，绝不 or 0。固定自然周口径，
    没有 hours 窗口条。"""
    # ---- ① 用户留下来了吗 ----
    stick = stickiness or {}
    window = stick.get("window") or {}
    l5_plus = None
    if stickiness is not None:
        ld = stick.get("l_distribution") or {}
        vals = [ld.get(k) for k in (5, 6, 7)]
        if any(v is not None for v in vals):
            l5_plus = sum(int(v) for v in vals if v is not None)
    # W4 tile 用 w4_split 的使用口径（app_session_end），与宽口径 cohort 表
    # 是两把尺子；取最新一个 W4 已可判的注册周。
    w4_tile = "—"
    if w4_split is not None:
        for c in w4_split.get("cohorts", []):
            if c.get("w4_rate_all") is not None:
                w4_tile = (
                    f"{_fmt_ratio(c.get('w4_rate_all'))}"
                    f"（{str(c.get('cohort_week') or '')}）"
                )
                break
    window_line = (
        f"<div class='muted'>粘性窗口 {html.escape(str(window.get('start_day') or ''))}"
        f" ～ {html.escape(str(window.get('end_day') or ''))}（近 7 个已完整北京自然日）</div>"
        if stickiness is not None else ""
    )
    section1_tiles = "".join([
        _render_metric(
            "WAU·打开过 App（近 7 完整日）", _fmt_count(stick.get("wau")),
            hint="近 7 个已完整北京自然日内至少上报一次 app_session_end 的去重账号；前台被杀会漏报，是保守下限",
        ),
        _render_metric(
            "DAU/WAU 粘性", _fmt_ratio(stick.get("stickiness")),
            hint="同窗口平均 DAU ÷ WAU；越高说明周活用户来得越频繁",
        ),
        _render_metric(
            "最新成熟 cohort W4", w4_tile,
            hint="最新一个已满 4 周的注册周 cohort 的第 4 周仍活跃占比（app_session_end 使用口径）",
        ),
        _render_metric(
            "L5+ 重度用户", _fmt_count(l5_plus),
            hint="近 7 完整日内活跃 ≥5 天的用户数；来自下方 L1–L7 分布",
        ),
    ])
    section1 = f"""
  <h2>用户留下来了吗？</h2>
  {window_line}
  <section class="metrics">{section1_tiles}</section>
  <h3>周 cohort 留存（宽口径快照）</h3>
  {_health_cohort_table(retention)}
  <h3>L1–L7 活跃天数分布（近 7 完整日）</h3>
  {_health_l_table(stickiness)}
  <h3>每周增长核算（app_session_end 使用口径）</h3>
  {_health_growth_table(growth)}
  <details class="note-box"><summary>口径说明</summary>cohort 表的「活跃」是宽口径快照（聊天∪任何 tracking 事件、分母自首次冻结起防删除），而粘性/W4/增长核算用 app_session_end 使用口径——两把尺子不同，不可互读。增长核算最新一行是进行中的本周：流失/回流/净变化右删失显示 —（本周还没打开 ≠ 流失），新注册/新激活/活跃是到目前为止的下限；小样本仅方向性参考。</details>"""

    # ---- ② 新用户能激活吗 ----
    # coverage_complete 只保证漏斗行数对得上；「最新完整」tile 还要求 t3 窗
    # 已走完，否则 cohorts[0]（进行中的本周）会把右删失的 0% 当定论展示。
    latest_complete = next(
        (
            c for c in (activation or {}).get("cohorts", [])
            if c.get("coverage_complete")
            and _health_t3_matured(c.get("cohort_week"))
        ),
        None,
    )
    t3_tile = "—"
    median_tile = "—"
    if latest_complete is not None:
        t3_tile = (
            f"{_fmt_ratio(latest_complete.get('t3_rate'))}"
            f"（{str(latest_complete.get('cohort_week') or '')}）"
        )
        median_h = latest_complete.get("median_t0_t3_hours")
        median_tile = _funnel_dur(median_h * 3600 if median_h is not None else None)
    cum_activated = None
    if activation is not None:
        # 只数 coverage_complete 的周——契约里 t3 恒为 int（漏斗故障时全 0），
        # 不过滤的话漏斗断供会把 tile 渲染成自信的 0，且与 hint 的「覆盖不
        # 完整的周不计入」自相矛盾。一个完整周都没有 → None → —。
        complete = [
            c for c in activation.get("cohorts", []) if c.get("coverage_complete")
        ]
        if complete:
            cum_activated = sum(int(c.get("t3") or 0) for c in complete)
    section2_tiles = "".join([
        _render_metric(
            "最新完整 cohort t3 激活率", t3_tile,
            hint="最新一个事件覆盖完整、且 t3 窗（注册周+3 天）已走完的注册周 cohort 中，注册 3 天内产生首条非 fallback 真回复的比例；窗口没走完的周不当定论",
        ),
        _render_metric(
            "注册→首次真回复中位时长", median_tile,
            hint="同一 cohort 内注册到首条非 fallback 真回复的中位耗时",
        ),
        _render_metric(
            "近 10 周累计激活数", _fmt_count(cum_activated),
            hint="近 10 个注册周 cohort 中 3 天内激活的用户数合计；覆盖不完整的周不计入",
        ),
    ])
    section2 = f"""
  <h2>新用户能激活吗？</h2>
  <section class="metrics">{section2_tiles}</section>
  <h3>注册周激活漏斗</h3>
  {_health_activation_table(activation)}
  <h3>W4 留存 · 全量 vs 激活者</h3>
  {_health_w4_split_table(w4_split)}
  <details class="note-box"><summary>口径说明</summary>t3=首条非 fallback 真回复（可能是主动消息）；lift=激活者 W4 ÷ 全量 W4，「激活」只认 W4 窗口开始前的回复（激活须先于被预测的那周，成熟 cohort 数字永久冻结）。覆盖不完整的注册周渲染「未知」、t3 窗（注册周+3 天）没走完的渲染「未成熟」——都不算 0%。</details>"""

    # ---- ③ 强度是真的吗 ----
    conc_sessions = (concentration or {}).get("sessions") or {}
    conc_tokens = (concentration or {}).get("tokens") or {}
    sess_tile = "—"
    token_tile = "—"
    if concentration is not None:
        sess_tile = (
            f"{_fmt_ratio(conc_sessions.get('share'))}"
            f" · n={_fmt_count(conc_sessions.get('users'))}"
        )
        token_tile = (
            f"{_fmt_ratio(conc_tokens.get('share'))}"
            f" · n={_fmt_count(conc_tokens.get('users'))}"
        )
    reply_rows = (reply_rate or {}).get("rows") or []
    # builder 首行是进行中的本周（右删失半样本），tile 标称「最新完整周」
    # 就必须跳过本周，取第一个严格早于本北京周一的行。
    _bj_this_monday = _health_bj_this_monday()
    latest_reply = next(
        (r for r in reply_rows if str(r.get("week") or "") < _bj_this_monday),
        None,
    )
    reply_tile = _fmt_ratio(latest_reply.get("reply_rate")) if latest_reply else "—"
    # 铁杆 headline 同款跳过本周：进行中的周 ≥5 job 门槛周初必然没凑够，
    # current（=weekly[0]）每周一都假性归零——那是删失不是流失。
    latest_power = next(
        (
            r for r in (power or {}).get("weekly") or []
            if str(r.get("week") or "") < _bj_this_monday
        ),
        None,
    )
    power_tile = _fmt_count((latest_power or {}).get("power_users"))
    section3_tiles = "".join([
        _render_metric(
            "Top10% session 份额", sess_tile,
            hint="近 28 个完整北京日内 app session 数最多的前 10% 用户贡献的 session 占比；n=窗口内有 session 的用户数",
        ),
        _render_metric(
            "Top10% token 份额（Hosted V2 灰度）", token_tile,
            hint="与 session 侧同一 28 个完整北京日窗口；仅 Hosted V2 灰度用户的模型 token 可见，BYOK 自付不可观测，不代表全体",
        ),
        _render_metric(
            "最新完整周铁杆用户", power_tile,
            hint="最近一个已完整走完的周里，连续 4 周每周 ≥5 个 admitted 用户侧主动 job 的用户数（V1+V2 wake 合并；自主心跳/presence tick、维护类与限流 skip 不计——那些量的是 agent 在线，不是人）",
        ),
        _render_metric(
            "主动消息 24h 回复率（最新完整周）", reply_tile,
            hint="主动消息发出后 24 小时内用户有发言的比例；服务端可见下限，相关非归因",
        ),
    ])
    section3 = f"""
  <h2>强度是真的吗？</h2>
  <section class="metrics">{section3_tiles}</section>
  <h3>铁杆用户趋势（近 12 周 + 月度）</h3>
  {_health_power_tables(power)}
  <h3>主动消息 24h 回复率（近 10 周）</h3>
  {_health_reply_table(reply_rate)}
  <details class="note-box"><summary>口径说明</summary>铁杆=连续 4 周每周≥5 个 admitted 用户侧主动 job（V1+V2 wake 合并；自主心跳/presence tick 不计——默认 cadence 下心跳一周 ~84 个，计入会让门槛对任何 agent 在线的用户躺满；维护类与限流 skip 同样不计）；headline 取最近完整周，进行中的本周周初必然偏低；回复率仅服务端下限、相关非归因；session 与 token 集中度同用近 28 个完整北京日窗口，token 仅 V2 灰度、BYOK 自付不可见。</details>"""

    # ---- ④ 还缺什么证据 ----
    # 这个 note-box 故意常开不折叠：缺口清单就是这一节的正文。
    section4 = """
  <h2>还缺什么证据？</h2>
  <div class="note-box">
    · 客户端 session 来源标记（自然打开 vs 推送拉起）未埋点——阻塞「主动消息拉活占比」与「人发起 session 数」两个核心指标<br>
    · 客户端已读/点按 ACK 未埋点——「主动消息触达率」只能用本页 24h 回复率做下限<br>
    · BYOK 自付 token 不可观测（全员自带 key；Hosted V2 为灰度）——「用户自掏多少钱」需线下调研补证。
  </div>"""

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>产品健康 · Feedling Data Track</title>
<style>{_HEALTH_PAGE_CSS}</style></head><body><main>
  <span class="ops-kicker">Operations / metadata only</span>
  <h1>产品健康</h1>
  <div class="muted">固定口径：北京时间自然周（周一起算）· 近 10–12 周 · 只展示当前数据库可证实的数字。</div>
  {_render_data_track_view_nav("health")}
  {section1}
  {section2}
  {section3}
  {section4}
</main></body></html>"""


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
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedling Proactive 日报 · Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; }}
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
    .sort-button {{ display:inline-flex; min-height:44px; box-sizing:border-box; align-items:center; justify-content:center; border:1px solid var(--line); border-radius:6px; padding:9px 12px; background:var(--card); color:var(--fg); font-size:13px; }}
    .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#e7eee9; }}
    table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
    th {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; background:#f4ece5; }}
    tr:last-child td {{ border-bottom:0; }} a {{ color:var(--accent); text-decoration:none; }}
{_NAV_GROUP_CSS}
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
    "mcp.surface.resolved": ("🧩", "MCP 工具面"),
    "mcp.surface.missing": ("🧩", "MCP 工具面缺失"),
    "mcp.surface.wired": ("🧩", "MCP 已接线"),
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
    # Ring depth is debug_trace._MAX_EVENTS (2500); stopping the picker at 500
    # meant a full 48h trace could not be read out from the panel at all.
    for value in ("50", "100", "200", "500", "1000", "2500"):
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
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>Feedling Debug · Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; --ink:#263238; }}
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
    .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#e7eee9; }}
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
{_NAV_GROUP_CSS}
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
    """事件健康度看板。

    **按天统计**（北京时区）：`?day=YYYY-MM-DD`，缺省为今天。在 2026-08-04 之前
    这里是全量历史累计，而 URL 上的 `day=`/`hours=` 参数对它毫无作用——于是"今天
    200 次全挂"在一万次历史成功面前仍显示 98% 绿，分母只增不减，指标越用越钝。
    运营真正要看的是"今天健康吗"，所以口径改成单日。
    """
    raw_day = str(request.args.get("day") or "").strip()
    day = _validated_dau_day(raw_day) if raw_day else _events_today()
    raw = db.admin_events_overview(day=day)

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
        "day": day,
        "note": "Onboarding 漏斗 + 回复延迟为下一阶段；本页先给 成功率/次数/中位耗时(job类)/兜底率，VPS·API 分列。按北京时区单日统计。",
    }


def _events_today() -> str:
    """北京时间的今天。与 `admin_events_overview` 的分桶时区同源，
    否则页面默认打开的"今天"会和 SQL 认定的"今天"错开几小时。"""
    return datetime.now(_SHANGHAI_TZ).strftime("%Y-%m-%d")


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


def _render_events_day_nav(day: str) -> str:
    """前一天 / 后一天 / 直接选日期。

    这个看板的口径是单日，所以"看哪天"必须是页面上的一等控件——否则用户只能手改
    URL，而 URL 里那个 `day=` 参数在 2026-08-04 之前恰恰是不生效的装饰。
    """
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return ""
    prev = (d - timedelta(days=1)).isoformat()
    nxt = (d + timedelta(days=1)).isoformat()
    today = _events_today()
    # 不给"未来"入口：那天必然是空表，只会让人以为出故障了
    next_link = (f"<a class='sort-button' href='?view=events&day={nxt}'>后一天 ›</a>"
                 if nxt <= today else
                 "<span class='sort-button muted' aria-disabled='true'>后一天 ›</span>")
    return f"""<div class="viewbar">
    <a class="sort-button" href="?view=events&day={prev}">‹ 前一天</a>
    <form method="get" style="display:inline-flex; gap:8px; align-items:center;">
      <input type="hidden" name="view" value="events">
      <input class="sort-button" type="date" name="day" value="{html.escape(day)}" max="{today}">
      <button class="sort-button" type="submit">查看</button>
    </form>
    {next_link}
    {"" if day == today else f"<a class='sort-button' href='?view=events&day={today}'>回到今天</a>"}
  </div>"""


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
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feedling 事件健康度 · Data Track</title>
<style>
  :root {{ color-scheme: light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  main {{ max-width:920px; margin:0 auto; padding:28px 24px 48px; }}
  h1 {{ font-size:24px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:24px 0 10px; }}
  .muted {{ color:var(--muted); }} .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
  .viewbar {{ display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 18px; }}
  .sort-button {{ display:inline-flex; min-height:44px; box-sizing:border-box; align-items:center; justify-content:center; border:1px solid var(--line); border-radius:6px; padding:9px 12px; background:var(--card); color:var(--fg); font-size:13px; }}
  .sort-button.active {{ border-color:var(--accent); color:var(--accent); background:#e7eee9; }}
  a {{ color:var(--accent); text-decoration:none; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
  th,td {{ text-align:left; padding:11px 14px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; background:#f4ece5; }}
  tr:last-child td {{ border-bottom:0; }} b {{ font-size:15px; }}
  .evt-desc {{ font-size:12px; color:var(--muted); line-height:1.5; margin-top:3px; max-width:520px; font-weight:400; text-transform:none; letter-spacing:0; }}
  .note-box {{ background:#fff8ef; border:1px solid #e8d8be; border-radius:8px; padding:12px 14px; margin:14px 0; font-size:13px; line-height:1.65; color:#5a4d3c; }}
{_NAV_GROUP_CSS}
</style></head><body><main>
  <h1>事件健康度</h1>
  <div class="muted">VPS=resident 自托管；API=model_api 托管。统计口径 = <b>{html.escape(str(payload.get('day') or ''))}</b> 当天（北京时间）。Generated {html.escape(_bj_iso(payload.get('generated_at')))}.</div>
  {_render_data_track_view_nav("events")}
  {_render_events_day_nav(str(payload.get('day') or ''))}
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
    # 查询失败时 db 层返回 None（区别于「查到 0 行」的 []）；漏斗页对两者
    # 都渲染空桶，不在这里崩。
    for r in (db.admin_onboarding_funnel() or []):
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
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Onboarding 漏斗 · Data Track</title>
<style>
  :root {{ color-scheme: light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; }}
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
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(label)} · 按用户 · Data Track</title>
<style>
  :root {{ color-scheme: light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; }}
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
    def _app_event_line(event: str) -> str:
        ts = pf.get(f"recent_app_{event}_ts")
        if ts:
            return (
                f"<div class='ppmuted' style='margin-top:6px'>最近 app_{event} 上报：{_fmt_ts(ts)}"
                f"（{_fmt_duration_sec(pf.get(f'recent_app_{event}_age_sec'))} 前）</div>"
            )
        return (
            f"<div class='ppmuted' style='margin-top:6px'>最近 app_{event} 上报："
            "无（iOS 快捷指令可能已停）</div>"
        )

    app_lines = _app_event_line("open") + _app_event_line("close")
    return (
        "<h2 style='font-size:15px;margin:22px 0 6px'>感知上报新鲜度</h2>"
        "<div class='ppmuted' style='font-size:12px;margin-bottom:6px'>"
        "每个感知字段的最后上报时间（只看时间戳，不含内容）。过期=已超该字段 TTL，agent 现在读到 null；"
        "用来分辨『客户端停止上报』vs『后端读取问题』。共 "
        f"{len(fields)} 字段：<b class='ppok'>{fresh_n} 新鲜</b> · "
        f"<b style='color:#a05a00'>{stale_n} 过期</b> · {never_n} 从未上报。</div>"
        "<table style='font-size:12px'><thead><tr>"
        "<th>字段</th><th>能力</th><th>最后上报</th><th>距今</th><th>TTL</th><th>状态</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>" + app_lines
    )


def _render_user_daily_usage(user: dict) -> str:
    rows = list(user.get("daily_usage") or [])
    days = int(user.get("daily_usage_days") or len(rows) or 14)
    window_total = sum(int(row.get("foreground_sec") or 0) for row in rows)
    window_sessions = sum(int(row.get("sessions") or 0) for row in rows)
    all_time = user.get("app_usage") or {}
    all_time_total = int(all_time.get("foreground_sec") or 0)
    all_time_sessions = int(all_time.get("sessions") or 0)
    max_daily = max(
        (int(row.get("foreground_sec") or 0) for row in rows),
        default=0,
    )
    daily_rows = []
    for row in rows:
        foreground = int(row.get("foreground_sec") or 0)
        sessions = int(row.get("sessions") or 0)
        width = (foreground * 100.0 / max_daily) if max_daily else 0.0
        state = (
            f"{html.escape(_fmt_duration_sec(foreground))} · {sessions} 次会话"
            if sessions
            else "未打开"
        )
        daily_rows.append(
            "<div class='daily-row'>"
            f"<div class='daily-day'>{html.escape(str(row.get('day') or ''))}</div>"
            f"<div class='daily-track {'daily-zero' if sessions == 0 else ''}'>"
            f"<span style='width:{width:.2f}%'></span>"
            "</div>"
            f"<div class='daily-value'>{state}</div>"
            "</div>"
        )
    action = f"/admin/data-track/users/{quote(str(user.get('user_id') or ''))}"
    return (
        f"<h2 class='daily-heading'>最近 {days} 天使用时长</h2>"
        "<div class='daily-summary'>"
        f"<b>窗口合计 {html.escape(_fmt_duration_sec(window_total))}</b>"
        f" · {window_sessions} 次会话"
        f"；全时段合计 <b>{html.escape(_fmt_duration_sec(all_time_total))}</b>"
        f" · {all_time_sessions} 次会话"
        "</div>"
        f"<form class='daily-controls' method='get' action='{html.escape(action, quote=True)}'>"
        f"<input name='admin_key' type='hidden' value='{html.escape(request.args.get('admin_key', ''), quote=True)}'>"
        f"<input name='events_limit' type='hidden' value='{int((user.get('tracking') or {}).get('events_limit') or 50)}'>"
        f"<label>天数 <input name='days' type='number' min='1' max='90' value='{days}'></label>"
        "<button type='submit'>更新</button>"
        "</form>"
        "<section class='daily-usage'>"
        + (
            "".join(daily_rows)
            if daily_rows
            else "<div class='muted'>暂无使用时长数据。</div>"
        )
        + "</section>"
        "<div class='muted daily-note'>"
        "按北京日统计 app_session_end；没有上报的日期明确显示“未打开”。"
        "前台被强杀会漏报，因此时长略偏低估。"
        "</div>"
    )


def _render_invalid_data_track_user_page(raw_user_id: str) -> str:
    # 强制 view=users：UID 直查表单长在用户页，默认视图切成首页后，裸
    # qs 的返回链接会把人丢回首页、弄丢表单——回用户页永远是对的去处。
    back_qs = _data_track_qs(uid=None, view="users")
    back = f"/admin/data-track?{back_qs}" if back_qs else "/admin/data-track"
    supplied = str(raw_user_id or "").strip()
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>UID 格式错误 · Feedling Data Track</title>
<style>
  :root {{ color-scheme:light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  main {{ max-width:620px; margin:80px auto; padding:24px; }}
  .box {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:22px; }}
  h1 {{ margin:0 0 8px; font-size:20px; }} .muted {{ color:var(--muted); }}
  code {{ word-break:break-all; }} a {{ color:var(--accent); text-decoration:none; }}
</style></head><body><main><section class="box">
  <h1>UID 格式不正确</h1>
  <p>请输入形如 <code>usr_0123456789abcdef</code> 的 UID。</p>
  <p class="muted">收到：<code>{html.escape(supplied) if supplied else "（空）"}</code></p>
  <a href="{html.escape(back, quote=True)}">← 返回 Data Track</a>
</section></main></body></html>"""


def _render_user_detail_page(user: dict) -> str:
    qs = _data_track_qs()
    back = f"/admin/data-track?{qs}" if qs else "/admin/data-track"
    safe_json = json.dumps(_bj_deep(user), ensure_ascii=False, indent=2)
    responder = user.get("responder") if isinstance(user.get("responder"), dict) else {}
    responder_name = str(responder.get("effective_responder") or "none")
    mismatch = bool(responder.get("mismatch"))
    mismatch_reasons = ", ".join(
        str(reason) for reason in responder.get("mismatch_reasons") or []
    )
    responder_notice = (
        f'<div class="responder-alert"><strong>RESPONDER MISMATCH</strong>: '
        f'{html.escape(mismatch_reasons or "route/evidence conflict")}</div>'
        if mismatch
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(user['user_id'])} · Feedling Data Track</title>
  <style>
    :root {{ color-scheme: light; --fg:#1b201d; --muted:#68706a; --line:#dddcd4; --bg:#f5f4ef; --card:#fcfbf8; --accent:#416b56; }}
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
    .daily-heading {{ font-size:17px; margin:26px 0 6px; }}
    .daily-summary {{ background:#fff8ef; border:1px solid #e8d8be; border-radius:8px; padding:11px 13px; color:#5a4d3c; }}
    .daily-controls {{ display:flex; align-items:center; gap:8px; margin:10px 0; }}
    .daily-controls input[type=number] {{ width:72px; border:1px solid var(--line); border-radius:6px; padding:7px; }}
    .daily-controls button {{ border:0; border-radius:6px; padding:8px 12px; background:var(--accent); color:white; font-weight:600; cursor:pointer; }}
    .daily-usage {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:13px 15px; }}
    .daily-row {{ display:grid; grid-template-columns:90px minmax(130px,1fr) 150px; gap:10px; align-items:center; margin:8px 0; }}
    .daily-day,.daily-value {{ font-size:12px; font-variant-numeric:tabular-nums; }}
    .daily-track {{ height:16px; background:#f0e6dd; border-radius:4px; overflow:hidden; }}
    .daily-track span {{ display:block; height:100%; min-width:0; background:var(--accent); border-radius:4px; }}
    .daily-track.daily-zero {{ background:#e7e2de; border:1px dashed #c9beb6; box-sizing:border-box; }}
    .daily-note {{ margin-top:7px; font-size:12px; }}
    .responder-alert {{ margin:10px 0; padding:11px 13px; color:#8f1711; background:#fff0ee; border:2px solid #c62f25; border-radius:8px; }}
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
    <div class="card"><div class="value">{html.escape(user.get('provider_state') or 'ok')}</div><div class="label">provider state</div></div>
    <div class="card"><div class="value">{html.escape(_bj_iso(user.get('last_provider_success_at')) or 'never')}</div><div class="label">last provider success</div></div>
    <div class="card"><div class="value">{html.escape(user.get('last_provider_error_class') or 'none')}</div><div class="label">latest provider error</div></div>
    <div class="card"><div class="value">{html.escape(responder_name)}</div><div class="label">effective responder</div></div>
  </section>
  {responder_notice}
  <div class="muted">Responder 判据：{html.escape(str(responder.get('criteria') or 'unavailable'))}</div>
  {_render_user_daily_usage(user)}
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
