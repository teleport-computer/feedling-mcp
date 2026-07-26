"""Display-safe projection of runtime-neutral chat activity.

The authoritative records are V2 ``agent_jobs`` / ``agent_status_events`` and
V1 ``chat_turn_activity_events``. This module only projects their fixed
metadata; it never accepts model prose, tool arguments, tool result bodies, or
chain-of-thought.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping


TOOL_ACTIVITY_KIND = "tool_activity"
MAX_ACTIVITY_EVENTS = 100
MEMORY_CATEGORY_KEYS = frozenset({
    "work", "growth", "family", "friends", "pets", "relationship", "feelings",
    "preferences", "values", "health", "interests", "money", "food", "travel",
})
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "discarded"})


def safe_token(value: Any, *, max_len: int = 128) -> str:
    """Return a bounded identifier token, never arbitrary content."""
    cleaned = _SAFE_TOKEN_RE.sub("_", str(value or "").strip())
    return cleaned[:max_len]


def safe_duration_ms(value: Any) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(duration) or duration < 0:
        return None
    return round(min(duration, 86_400_000.0), 3)


def result_code(result_content: Any, effect: Mapping[str, Any] | None = None) -> str:
    """Classify a tool result without retaining its body."""
    effect_status = safe_token((effect or {}).get("status"), max_len=40).lower()
    if effect_status:
        return effect_status
    text = str(result_content or "").strip()
    lowered = text.lower()
    if lowered.startswith("error:"):
        return "tool_error"
    if lowered.startswith("queued:"):
        return "queued"
    return "ok"


def event_state(event_kind: str, result_content: Any = "") -> str:
    if event_kind == "tool_call_started":
        return "running"
    if event_kind == "tool_call_error":
        return "failure"
    return "failure" if str(result_content or "").strip().lower().startswith("error:") else "success"


def safe_memory_metadata(value: Any) -> dict:
    """Keep only a confirmed count and complete canonical category breakdown."""
    if not isinstance(value, Mapping):
        return {}
    count = value.get("memory_count")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1000:
        return {}
    safe: dict[str, Any] = {"memory_count": count}
    categories = value.get("memory_categories")
    if not isinstance(categories, list) or not categories:
        return safe
    projected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in categories[: len(MEMORY_CATEGORY_KEYS)]:
        if not isinstance(item, Mapping):
            return safe
        key = str(item.get("key") or "")
        item_count = item.get("count")
        if (
            key not in MEMORY_CATEGORY_KEYS
            or key in seen
            or isinstance(item_count, bool)
            or not isinstance(item_count, int)
            or item_count <= 0
        ):
            return safe
        seen.add(key)
        projected.append({"key": key, "count": item_count})
    if sum(item["count"] for item in projected) == count:
        safe["memory_categories"] = projected
    return safe


def project_tool_events(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse start/result status rows into one event per confirmed invocation."""
    ordered: list[str] = []
    projected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("kind") or "") != TOOL_ACTIVITY_KIND:
            continue
        detail = row.get("detail_json")
        if not isinstance(detail, Mapping):
            continue
        activity_id = safe_token(detail.get("activity_id"), max_len=160)
        tool_name = safe_token(detail.get("tool_name"), max_len=120)
        state = safe_token(detail.get("state"), max_len=24).lower()
        if not activity_id or not tool_name or state not in {"running", "success", "failure"}:
            continue
        created_at = row.get("created_at")
        try:
            created = float(created_at)
        except (TypeError, ValueError, OverflowError):
            created = 0.0
        if activity_id not in projected:
            if len(ordered) >= MAX_ACTIVITY_EVENTS:
                continue
            ordered.append(activity_id)
            projected[activity_id] = {
                "id": activity_id,
                "kind": "tool",
                "name": tool_name,
                "status": state,
                "job_id": str(row.get("job_id") or ""),
                "call_id": safe_token(detail.get("call_id"), max_len=160),
                "started_at": created or None,
            }
        event = projected[activity_id]
        event["status"] = state
        if state != "running":
            event["finished_at"] = created or None
        duration = safe_duration_ms(detail.get("duration_ms"))
        if duration is not None:
            event["duration_ms"] = duration
        for source, target, limit in (
            ("effect_id", "effect_id", 160),
            ("effect_type", "effect_type", 80),
            ("effect_status", "effect_status", 40),
            ("result_code", "result_code", 64),
        ):
            token = safe_token(detail.get(source), max_len=limit)
            if token:
                event[target] = token
        event.update(safe_memory_metadata(detail))
    return [projected[event_id] for event_id in ordered]


def turn_response(turn_id: str, jobs: Iterable[Mapping[str, Any]], rows: Iterable[Mapping[str, Any]]) -> dict:
    job_list = [
        {
            "job_id": str(job.get("id") or ""),
            "status": safe_token(job.get("status"), max_len=32),
        }
        for job in jobs
    ]
    row_list = list(rows)
    phases = [str(row.get("kind") or "") for row in row_list if row.get("kind") != TOOL_ACTIVITY_KIND]
    statuses = {job["status"] for job in job_list if job["status"]}
    complete = bool(statuses) and statuses.issubset(_TERMINAL_JOB_STATUSES)
    return {
        "turn_id": turn_id,
        "runtime": "v2",
        "complete": complete,
        "phase": safe_token(phases[-1], max_len=40) if phases else "queued",
        "jobs": job_list,
        "events": project_tool_events(row_list),
    }


def resident_turn_response(turn_id: str, parent: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> dict:
    """Project a V1 resident turn from its durable parent and tool evidence."""
    complete = (
        str(parent.get("reply_status") or "") == "replied"
        or bool(str(parent.get("reply_message_id") or ""))
    )
    status = "completed" if complete else "running"
    return {
        "turn_id": turn_id,
        "runtime": "v1",
        "complete": complete,
        "phase": "done" if complete else "processing",
        "jobs": [{"job_id": f"v1:{turn_id}", "status": status}],
        "events": project_tool_events(rows),
    }
