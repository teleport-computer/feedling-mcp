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
