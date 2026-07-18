"""Shared agent-perception authorization judgment.

Lifted out of ``agent.perception_core`` so EVERY read path enforces the same
rule. The signal path used to be the only caller, which meant a capability the
user had switched off was still readable through any other entry point (the
dedicated recent_apps route and the V2 executor adapter both bypassed it).

Pure functions only — no store/service imports — so ``perception.service`` can
depend on this without an import cycle.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# Agent signal name -> the permission keys that may gate it. A signal absent
# here is gated by its own name.
SIGNAL_PERMISSION_KEYS: dict[str, tuple[str, ...]] = {
    "now": ("now", "time", "device", "battery", "broadcast"),
    "location": ("location", "location_signal"),
    "weather": ("weather",),
    "motion": ("motion", "motion_state"),
    "calendar": ("calendar", "calendar_next_event"),
    "focus": ("focus",),
    "audio_route": ("audio_route",),
    "steps": ("steps", "health", "health_vitals"),
    "sleep": ("sleep", "health", "health_sleep"),
    "workout": ("workout", "health", "health_workout"),
    "vitals": ("vitals", "health", "health_vitals"),
    "activity": ("activity", "health", "health_activity"),
    "body": ("body", "health", "health_body"),
    "metabolic": ("metabolic", "health", "health_metabolic"),
    "cycle": ("cycle", "health", "health_cycle"),
    "mood": ("mood", "health", "health_mood"),
    "reminders": ("reminders",),
    # App-open HISTORY rides the same capability as the current-app field: one
    # switch, both resolutions. Turning `app` off must stop the trajectory read
    # too, not just the current value.
    "recent_apps": ("app",),
}

OFF_VALUES = {"0", "false", "off", "disabled", "switch_off", "switch-off", "no"}
DENIED_VALUES = {
    "denied",
    "not_permitted",
    "not-permitted",
    "not_allowed",
    "not-allowed",
    "not_authorized",
    "not-authorized",
    "unauthorized",
    "restricted",
    "permission_denied",
}
ALLOW_VALUES = {"1", "true", "on", "enabled", "allowed", "authorized", "granted", "yes"}


def boolish_doc_reason(value: Any) -> str:
    if isinstance(value, bool):
        return "" if value else "switch_off"
    if isinstance(value, (int, float)):
        return "" if bool(value) else "switch_off"
    normalized = str(value or "").strip().lower()
    if not normalized or normalized in ALLOW_VALUES:
        return ""
    if normalized in OFF_VALUES:
        return "switch_off"
    if normalized in DENIED_VALUES:
        return "not_permitted"
    return ""


def permission_state_reason(value: Any) -> str:
    if isinstance(value, Mapping):
        explicit_reason = str(value.get("reason") or "").strip().lower()
        for key in ("enabled", "allowed", "authorized", "granted", "permitted"):
            if key in value:
                reason = boolish_doc_reason(value.get(key))
                if reason:
                    return "not_permitted" if explicit_reason in DENIED_VALUES else reason
                return ""
        for key in ("state", "status", "permission", "value"):
            if key in value:
                reason = boolish_doc_reason(value.get(key))
                if reason:
                    return reason
        return boolish_doc_reason(explicit_reason)
    return boolish_doc_reason(value)


def permission_states_reason(settings: Mapping[str, Any], signal: str) -> str:
    """"" when the signal is readable, else a reason ("switch_off" /
    "not_permitted"). Absent permission_states means "never configured" ->
    readable, matching the implicit-authorization design."""
    states = settings.get("permission_states") if isinstance(settings, Mapping) else {}
    if not isinstance(states, Mapping):
        return ""
    for key in SIGNAL_PERMISSION_KEYS.get(signal, (signal,)):
        if key not in states:
            continue
        reason = permission_state_reason(states.get(key))
        if reason:
            return reason
    return ""
