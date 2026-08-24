"""Single source of truth for agent-facing perception projection.

Both the CLI-tools path (`/v1/agent/perception`, used by io_cli + the OpenClaw
native plugin) and the hosted proactive tool executor path (`tool_executor_v2`)
project perception_state through THIS map, so the proactive agent sees exactly
the same signals/fields as chat -- no second, stale catalog. (A-lite
unification 2026-06-26; see project_proactive_tool_unification.)

Add a new agent-pullable signal/field here ONCE and both paths pick it up.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FAST_AGENT_PERCEPTION_SIGNALS = ("now", "location", "weather", "motion", "calendar")
SLOW_AGENT_PERCEPTION_SIGNALS = (
    "steps", "sleep", "workout", "vitals",
    "activity", "body", "metabolic", "cycle", "mood", "reminders",
)
# `app` = the last app event we observed, within its TTL. `app_state` says which
# kind it was: "foreground" (the user opened it) or "closed" (the user left it).
# Both come from iOS Shortcut automations the user configures per app, so a user
# who only wired the open automation never produces "closed" — treat a missing
# app_state as unknown, not as "still in the app".
# App-event HISTORY is deliberately not a signal: it returns a list, takes
# limit/hours, and doesn't fit project_signal's state-field projection. It's a
# query tool -- perception.recent_apps / io_cli perception-recent-apps.
PULL_ONLY_AGENT_PERCEPTION_SIGNALS = ("focus", "audio_route", "app")
AGENT_PERCEPTION_SIGNALS = (
    FAST_AGENT_PERCEPTION_SIGNALS
    + SLOW_AGENT_PERCEPTION_SIGNALS
    + PULL_ONLY_AGENT_PERCEPTION_SIGNALS
)

AGENT_SIGNAL_FIELDS: dict[str, tuple[str, ...]] = {
    "now": (
        "local_time", "timezone", "locale", "battery_level", "charging",
        "place_label", "motion_state", "now_playing", "broadcast_state", "broadcast_active",
    ),
    "location": ("place_label", "wifi_label", "country", "locality", "wifi_anchor_id"),
    "weather": (
        "condition", "temperature", "apparent_temperature", "humidity",
        "precipitation_chance", "uv_index", "is_daylight", "alerts",
    ),
    "motion": ("motion_state",),
    "calendar": ("calendar_next_event", "calendar_events", "calendar_events_truncated"),
    "focus": ("focus_authorization_status", "in_focus"),
    "audio_route": ("output_type", "is_bluetooth", "device_name"),
    "app": ("app_name", "app_category", "app_state"),
    "steps": ("step_count",),
    "sleep": ("asleep_minutes", "core_minutes", "deep_minutes", "rem_minutes"),
    "workout": ("workout_type", "duration_min", "count_today"),
    "vitals": (
        "resting_heart_rate", "step_count", "current_heart_rate", "hrv_sdnn_ms",
        "respiratory_rate", "oxygen_saturation_pct", "vo2_max",
    ),
    "activity": ("active_energy_kcal", "exercise_minutes", "stand_minutes", "mindful_minutes"),
    "body": ("weight_kg", "bmi", "body_fat_pct", "height_cm"),
    "metabolic": ("blood_glucose_mmol_l", "blood_pressure_systolic", "blood_pressure_diastolic"),
    "cycle": ("flow_level", "is_active_period"),
    "mood": ("valence", "valence_classification", "kind", "label_count", "recorded_today"),
    "reminders": ("next_reminder", "reminders", "overdue_count", "due_today_count", "reminders_truncated"),
}


def project_signal(
    signal: str,
    snapshot: Mapping[str, Any],
    pull_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one agent signal's fields from the right source.

    `now` and shortcut-reported `app` are cheap snapshot fields; everything else
    comes from the pull snapshot.
    """
    source = snapshot if signal in {"now", "app"} else pull_snapshot
    out = {field: source.get(field) for field in AGENT_SIGNAL_FIELDS.get(signal, ())}
    if signal == "now":
        out["time"] = source.get("local_time")
        if "user_state" in source:
            out["user_state"] = source.get("user_state")
    return out


# --- Permission judgment ----------------------------------------------------
#
# Shared agent-perception authorization judgment. Lifted out of
# ``agent.perception_core`` so EVERY read path enforces the same rule. The
# signal path used to be the only caller, which meant a capability the user
# had switched off was still readable through any other entry point (the
# dedicated recent_apps route and the V2 executor adapter both bypassed it).
#
# Merged into this module (2026-08-19 perception-kernel extraction) because
# permission judgment is used only by projection (above) and glance — keeping
# it in a separate file bought nothing.

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
