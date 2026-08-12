"""Adapters from the legacy proactive job shape into V2 wake events."""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Mapping

from proactive.runtime_v2 import WakeEventV2


def _legacy_trigger_is_manual_v2(trigger: str) -> bool:
    normalized = str(trigger or "").strip().lower()
    return normalized == "manual_wake" or normalized.startswith("manual_")


def source_for_legacy_trigger_v2(trigger: str, *, manual: bool) -> str:
    normalized = str(trigger or "").strip().lower()
    if manual or _legacy_trigger_is_manual_v2(normalized):
        return "user_message"
    if normalized.startswith("heartbeat"):
        return "heartbeat"
    if normalized.startswith("perception_"):
        return "perception_event"
    if normalized in {
        "scene_change", "screen_tick", "screen_watch",
        "broadcast_opened", "broadcast_closed", "heartbeat_broadcast_on",
    }:
        return "scene_change"
    if normalized.startswith("scheduled"):
        return "scheduled_wake"
    if normalized == "background_result":
        return "background_result"
    return "perception_event"


def wake_event_v2_from_legacy_job(
    user_id: str,
    job: Mapping[str, Any],
    *,
    now: float | None = None,
) -> WakeEventV2:
    """Convert one current proactive job into the new wake envelope.

    This deliberately preserves the legacy job as payload. The adapter is a
    migration boundary; new runtime code should not reach back into
    proactive_jobs for strategy.
    """
    now = time.time() if now is None else float(now)
    trigger = str(job.get("trigger") or job.get("wake_kind") or "legacy_job")
    manual = bool(job.get("manual")) or _legacy_trigger_is_manual_v2(trigger)
    source = source_for_legacy_trigger_v2(trigger, manual=manual)
    return WakeEventV2(
        user_id=user_id,
        wake_id=str(job.get("wake_id") or job.get("job_id") or ""),
        source=source,
        trigger=trigger,
        created_at=float(job.get("ts") or now),
        latency_sensitive=manual,
        manual=manual,
        change_digest=str(job.get("change_digest") or job.get("context_hint") or ""),
        presence_hints=dict(job.get("presence_hints") or {}),
        timezone=str(job.get("timezone") or ""),
        scheduled_note=str(job.get("scheduled_note") or ""),
        origin_refs=tuple(str(x) for x in (job.get("origin_refs") or []) if str(x)),
        background_payload=job.get("background_payload") if isinstance(job.get("background_payload"), dict) else {},
        payload={"legacy_proactive_job": dict(job)},
    )


def legacy_job_from_wake_event_v2(event: WakeEventV2) -> dict[str, Any]:
    """Project a V2 wake back to the temporary legacy job queue.

    This is an output compatibility boundary for hosted/resident cutover only.
    The V2 runtime must continue to reason on WakeEventV2 and turn records, not
    on this shape.
    """
    now = float(getattr(event, "created_at", 0.0) or time.time())
    source = str(getattr(event, "source", "") or "scheduled_wake")
    raw_trigger = str(getattr(event, "trigger", "") or source)
    trigger = "background_result" if source == "background_result" else raw_trigger
    scheduled_note = str(getattr(event, "scheduled_note", "") or "")
    change_digest = str(getattr(event, "change_digest", "") or scheduled_note)
    return {
        "job_id": "pj_" + uuid.uuid4().hex[:16],
        "wake_id": str(getattr(event, "wake_id", "") or ""),
        "ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(),
        "source": "agent_initiated_proactive",
        "status": "pending",
        "intent_label": raw_trigger[:120],
        "trigger": trigger[:120],
        "wake_kind": source[:120],
        "context_hint": change_digest[:2000],
        "change_digest": change_digest[:2000],
        "presence_hints": dict(getattr(event, "presence_hints", {}) or {}),
        "timezone": str(getattr(event, "timezone", "") or ""),
        "scheduled_note": scheduled_note[:2000],
        "origin_refs": list(getattr(event, "origin_refs", ()) or ()),
        "background_payload": dict(getattr(event, "background_payload", {}) or {}),
        "connections": [],
        "connection": {},
        "frame_ids": [],
        "device_event_ids": [],
        "current_app": "",
        "payload": {"v2_wake": dict(getattr(event, "payload", {}) or {})},
    }
