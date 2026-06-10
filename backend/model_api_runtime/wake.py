"""Hosted proactive wake turn — pure logic.

The wake contract / wake event messages slot into the existing
_model_api_context_messages() output (persona + identity + memory + recent
chat come along for free); app.py only inserts wake_turn_contract_message()
at index 2 (same convention as the chat turn contract) and passes
build_wake_event_message()'s content as the turn's user message.

parse_wake_actions() validates the model's {"actions":[...]} reply.
Return value None means unparseable (caller marks the job failed and writes
NOTHING to chat — never leak raw model output to the user on a wake turn).
"""
from __future__ import annotations

import json
import re
from typing import Any

WAKE_AI_STATES = ("present", "watching", "thinking", "curious", "waiting")
MAX_WAKE_MESSAGES = 3
MAX_WAKE_MESSAGE_CHARS = 4000


def wake_turn_contract_message() -> dict[str, str]:
    return {
        "role": "system",
        "content": (
            "[Feedling proactive wake] This turn is a wake, not a user message. "
            "It is one opportunity to notice the user — not a request to speak. "
            "Speak only if you have a genuine, strong, self-motivated reason grounded "
            "in the wake event and your shared context; weak impulses, generic "
            "check-ins, or speaking just because you were woken should be sleep. "
            "Stay in your established companion voice; never say 'the system detected' "
            "or mention wakes, triggers, or jobs. Do not pretend to see the user's screen. "
            "Do not write raw screen or sensitive content into your message. "
            "Exception: if the wake event has manual=true, the USER summoned you "
            "(e.g. tapped the Dynamic Island) — do not sleep; respond with at least "
            "a brief, natural presence in your own voice. "
            "Return JSON only: {\"actions\":[...]} where each action is one of "
            "{\"type\":\"sleep\"} | "
            "{\"type\":\"send_message\",\"text\":\"...\"} "
            f"(at most {MAX_WAKE_MESSAGES} send_message actions, each a natural standalone bubble) | "
            "{\"type\":\"set_ai_state\",\"state\":\"" + "|".join(WAKE_AI_STATES) + "\"}. "
            "If nothing feels right, return {\"actions\":[{\"type\":\"sleep\"}]}."
        ),
    }


def build_wake_event_message(wake: dict[str, Any]) -> dict[str, str]:
    payload = {
        "kind": "proactive_wake",
        "trigger": str(wake.get("trigger") or ""),
        "context_hint": str(wake.get("context_hint") or "")[:2000],
        "user_state": str(wake.get("user_state") or ""),
        "ai_state": str(wake.get("ai_state") or ""),
        "broadcast_state": str(wake.get("broadcast_state") or ""),
        "created_at": str(wake.get("created_at") or ""),
        # 用户主动召唤的标志：契约要求 manual=true 时不许 sleep。
        "manual": bool(wake.get("manual")),
        "forced": bool(wake.get("forced")),
    }
    return {
        "role": "user",
        "content": "[Feedling proactive wake event]\n"
        + json.dumps(payload, ensure_ascii=False),
    }


def parse_wake_actions(raw_reply: str) -> list[dict[str, Any]] | None:
    """None = unparseable. A parseable reply always yields >=1 action; an
    actions list with nothing actionable coerces to a single sleep. Mixed
    replies (sleep alongside send_message/set_ai_state) are passed through
    as-is; the caller treats stray sleep entries as no-ops."""
    cleaned = (raw_reply or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("actions"), list):
        return None
    out: list[dict[str, Any]] = []
    message_count = 0
    for item in data["actions"]:
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("type") or "").strip().lower()
        if action_type == "sleep":
            out.append({"type": "sleep"})
        elif action_type == "send_message":
            text = str(item.get("text") or item.get("message") or "").strip()
            if text and message_count < MAX_WAKE_MESSAGES:
                out.append({"type": "send_message",
                            "text": text[:MAX_WAKE_MESSAGE_CHARS]})
                message_count += 1
        elif action_type == "set_ai_state":
            state = str(item.get("state") or "").strip().lower()
            if state in WAKE_AI_STATES:
                out.append({"type": "set_ai_state", "state": state})
        # 其他类型（含 request_broadcast）本期丢弃
    if not any(a["type"] in {"send_message", "set_ai_state"} for a in out):
        return [{"type": "sleep"}]
    return out


def hosted_tick_trigger(broadcast_state: str | None) -> str:
    """Trigger name by broadcast state, mirroring the resident consumer's
    mapping so _proactive_v2_auto_wake_block_reason treats hosted heartbeats
    identically (broadcast off/paused -> presence wake allowed; on without
    frames -> mechanically suppressed)."""
    state = str(broadcast_state or "").strip().lower()
    if state == "on":
        return "heartbeat_broadcast_on"
    if state == "paused":
        return "heartbeat_broadcast_paused"
    return "heartbeat_broadcast_off"
