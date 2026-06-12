"""Proactive V2 wake decision (mechanical gating only — judgment is agent-owned)."""

import re
import time
from datetime import datetime

from core import store as core_store
from core import util
from core.store import UserStore
from proactive import service
from screen import frames as screen_frames

def _proactive_trigger(payload: dict, *, manual: bool, frames: list[dict]) -> str:
    raw = (
        payload.get("trigger")
        or payload.get("wake_trigger")
        or payload.get("event_type")
        or payload.get("type")
        or ""
    )
    trigger = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(raw or "").strip().lower()).strip("_.:-")
    if trigger:
        return trigger[:120]
    if manual:
        return "manual_wake"
    return "screen_tick" if frames else "heartbeat_no_frame"


def _proactive_v2_auto_wake_block_reason(trigger: str, *, broadcast_state: str, frame_ids: list[str]) -> str:
    """Mechanical suppression for automatic V2 wakes that lack a current signal."""
    normalized_trigger = str(trigger or "").strip().lower()
    normalized_broadcast = str(broadcast_state or "").strip().lower()
    has_frames = bool(frame_ids)

    if normalized_trigger in {"heartbeat_unknown", "heartbeat_no_frame"}:
        return "no_recent_frames"
    if normalized_trigger in {"heartbeat_broadcast_off", "heartbeat_broadcast_paused"}:
        return ""
    if normalized_trigger == "heartbeat_broadcast_on" and not has_frames:
        return "no_recent_frames"
    if normalized_trigger == "broadcast_opened" and not has_frames:
        return "no_recent_frames"
    if (
        normalized_broadcast in {"off", "paused"}
        and normalized_trigger.startswith("heartbeat")
        and normalized_trigger not in {"heartbeat_broadcast_off", "heartbeat_broadcast_paused"}
    ):
        return f"broadcast_{normalized_broadcast}"
    return ""


def _proactive_v2_wake_kind(trigger: str, *, frame_ids: list[str]) -> str:
    if frame_ids:
        return "screen"
    normalized_trigger = str(trigger or "").strip().lower()
    if normalized_trigger in {"broadcast_opened", "heartbeat_broadcast_on", "screen_tick"}:
        return "screen"
    return "presence"


def _latest_payload_state_from_events(store: UserStore, key: str, allowed: set[str]) -> str:
    for event in reversed(store.list_device_events(since_epoch=max(0.0, time.time() - 86400), limit=200)):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        state = str(payload.get(key) or "").strip().lower()
        if state in allowed:
            return state
    return ""


def _effective_broadcast_state(store: UserStore, settings: dict) -> str:
    """Broadcast state as the wake decision sees it: the latest device event
    (24h window) wins; the persisted settings value is only the fallback.
    Tick-trigger derivation MUST use this same source — deriving the trigger
    from stale settings while the decision uses the live event state lets a
    heartbeat_broadcast_off trigger bypass the no-frame suppression."""
    return (
        _latest_payload_state_from_events(store, "broadcast_state", core_store.PROACTIVE_BROADCAST_STATES)
        or service._normalize_proactive_state(settings.get("broadcast_state"), core_store.PROACTIVE_BROADCAST_STATES, "unknown")
    )


def _build_proactive_v2_wake_decision(store: UserStore, payload: dict, api_key: str | None = None) -> dict:
    """Create a V2 wake event without doing platform-side semantic judgment.

    The platform may decide whether a wake is mechanically allowed, but it does
    not decrypt frames, require a memory connection, call a platform model, or infer
    whether the agent should speak. That judgment belongs to the authorized
    companion agent when the resident realizes this job.
    """
    now = time.time()
    payload = payload if isinstance(payload, dict) else {}
    settings = store.load_proactive_settings()
    force = service._proactive_bool(payload, "force", "force_response")
    manual = force or service._proactive_bool(payload, "manual", "manual_wake", "user_initiated") or bool(
        str(payload.get("context_hint") or "").strip()
    )

    payload_frames = payload.get("frames")
    if isinstance(payload_frames, list) and payload_frames:
        frames = [
            dict(f) for f in payload_frames
            if isinstance(f, dict) and str(f.get("id") or f.get("frame_id") or "").strip()
        ]
        for f in frames:
            if not f.get("id") and f.get("frame_id"):
                f["id"] = f.get("frame_id")
    else:
        frames = screen_frames._recent_frame_meta(
            store,
            now,
            service._payload_float(payload, "frame_window_sec", 300.0, 30.0, 3600.0),
        )
    selected_frames = screen_frames._sample_frames_for_wake(frames, max_frames=service.PROACTIVE_WAKE_MAX_FRAMES)
    frame_ids = screen_frames._frame_ids(selected_frames)
    device_events = service._recent_device_events_for_wake(
        store,
        now,
        service._payload_float(payload, "device_event_window_sec", 900.0, 30.0, 86400.0),
    )

    user_state = service._normalize_proactive_state(
        payload.get("user_state"),
        core_store.PROACTIVE_USER_STATES,
        service._normalize_proactive_state(settings.get("user_state"), core_store.PROACTIVE_USER_STATES, "default"),
    )
    ai_state = service._normalize_proactive_state(
        payload.get("ai_state"),
        core_store.PROACTIVE_AI_STATES,
        service._normalize_proactive_state(settings.get("ai_state"), core_store.PROACTIVE_AI_STATES, "present"),
    )
    broadcast_state = service._normalize_proactive_state(
        payload.get("broadcast_state"),
        core_store.PROACTIVE_BROADCAST_STATES,
        _effective_broadcast_state(store, settings),
    )
    trigger = _proactive_trigger(payload, manual=manual, frames=selected_frames)

    block_reason = ""
    if not settings.get("enabled", True) and not force:
        block_reason = "proactive_disabled"
    elif settings.get("dnd", False) and not manual:
        block_reason = "dnd_enabled"
    elif user_state == "away" and not manual:
        block_reason = "user_away"
    elif not manual:
        block_reason = _proactive_v2_auto_wake_block_reason(
            trigger,
            broadcast_state=broadcast_state,
            frame_ids=frame_ids,
        )

    current_app = str(payload.get("current_app") or "").strip()
    if not current_app:
        current_app = screen_frames._current_app_from_frames([], selected_frames)
    ocr = str(payload.get("ocr_summary") or "").strip() or screen_frames._ocr_summary(selected_frames)
    should_wake_agent = not bool(block_reason)
    wake_kind = _proactive_v2_wake_kind(trigger, frame_ids=frame_ids)
    decision_id = util._new_public_id("gd")
    wake_id = util._new_public_id("wake")
    reason = "wake_created" if should_wake_agent else block_reason
    expires_at = datetime.fromtimestamp(now + service.PROACTIVE_V2_WAKE_TTL_SEC).isoformat()

    return {
        "decision_id": decision_id,
        "wake_id": wake_id,
        "schema_version": 2,
        "decision_type": "wake_event",
        "ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(),
        "expires_at": expires_at,
        "gate_model": "proactive_v2:wake",
        "should_reach_out": should_wake_agent,
        "should_wake_agent": should_wake_agent,
        "should_garden_passive": False,
        "abstention_reason": "" if should_wake_agent else reason,
        "reason": reason,
        "intent_label": "",
        "context_hint": "",
        "connections": [],
        "connection": {},
        "frame_ids": frame_ids,
        "device_event_ids": [str(e.get("event_id")) for e in device_events if e.get("event_id")][:10],
        "current_app": current_app,
        "trigger": trigger,
        "wake_kind": wake_kind,
        "screen_context_available": bool(frame_ids),
        "manual": manual,
        "forced": force,
        "user_state": user_state,
        "ai_state": ai_state,
        "broadcast_state": broadcast_state,
        "semantic": {
            "reference": "agent_owned_v2",
            "llm_confidence": None,
            "llm_usage": {},
        },
        "gate_input": {
            "v2": True,
            "judgment": "agent_owned",
            "ocr_chars": len(ocr),
            "sampled_frame_count": len(selected_frames),
            "decrypt_ok": False,
            "image_count": 0,
            "decrypt_errors": [],
            "llm_called": False,
            "llm_error": "",
            "mechanical_block": block_reason,
            "memory_context": {
                "identity_loaded": False,
                "memory_count": 0,
                "passive_observation_count": 0,
                "recent_fire_count": 0,
                "connection_candidate_count": 0,
                "context_errors": {},
            },
        },
        "api_key_present": bool(api_key),
    }


def _proactive_job_from_decision(decision: dict) -> dict:
    now = time.time()
    return {
        "job_id": util._new_public_id("pj"),
        "schema_version": int(decision.get("schema_version") or 1),
        "ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(),
        "expires_at": decision.get("expires_at", ""),
        "source": service.PROACTIVE_JOB_SOURCE,
        "gate_decision_id": decision.get("decision_id", ""),
        "wake_id": decision.get("wake_id", decision.get("decision_id", "")),
        "status": "pending",
        "intent_label": decision.get("intent_label", ""),
        "context_hint": decision.get("context_hint", ""),
        "connections": decision.get("connections", []),
        "connection": decision.get("connection", {}),
        "frame_ids": decision.get("frame_ids", []),
        "device_event_ids": decision.get("device_event_ids", []),
        "current_app": decision.get("current_app", ""),
        "trigger": decision.get("trigger", ""),
        "manual": bool(decision.get("manual", False)),
        "forced": bool(decision.get("forced", False)),
        "user_state": decision.get("user_state", ""),
        "ai_state": decision.get("ai_state", ""),
        "broadcast_state": decision.get("broadcast_state", ""),
        "wake_kind": decision.get("wake_kind", ""),
        "screen_context_available": bool(decision.get("screen_context_available", False)),
        "agent_action": "",
        "agent_action_status": "",
    }
