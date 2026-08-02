"""Capability catalog — the single source of truth for Extended Perception.

Every perceptual capability is declared here ONCE. This drives all the generic
machinery:
  - the report endpoint (which input fields are accepted, which permission gates
    them, how raw values resolve to labels, which state fields they produce)
  - the snapshot endpoint (which fields are cheap context, their freshness TTL)
  - wake triggering (which capabilities are wake sources + their debounce)
  - the transparency UI (label + tier + default-on per capability)

Adding a Tier 2 capability = adding rows here + (if it has query tools) a thin
MCP pass-through. No changes to service/routes logic.

Privacy: capabilities whose `resolver` is set accept RAW values (lat/lon, ssid,
bundle id) and resolve them to coarse labels via the user's perception_config.
The raw value is used transiently and never written to perception_state, so the
agent only ever sees labels.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    """One perceptual ability (always available; authorization is implicit in
    whether the client reports a value — see docs/superpowers/specs/
    2026-06-09-perception-permissions-implicit-design.md)."""
    key: str                 # canonical capability key
    label: str               # human copy for the transparency UI
    tier: int                # 1 = ships with V2, 2 = follow-up
    wake_source: bool = False
    debounce_sec: float = 0.0
    context_field: bool = False   # appears in cheap wake snapshot
    query_tool: bool = False      # agent pulls on demand


@dataclass(frozen=True)
class Signal:
    """One field the client may report, mapped to its capability + processing."""
    input: str                       # key inside report `signals`
    capability: str                  # permission key gating it
    outputs: tuple[str, ...]         # state field name(s) produced
    resolver: str | None = None      # name in resolve.RESOLVERS, or None (store as-is)
    ttl_sec: float = 600.0           # snapshot freshness; older -> null
    significant: bool = True         # value change can trigger a wake (if wake_source)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

CAPABILITIES: dict[str, Capability] = {c.key: c for c in [
    # --- always-on (no iOS permission) ---
    Capability("time", "本地时间", 1, context_field=True),
    Capability("device", "电量", 1, context_field=True),
    Capability("broadcast", "屏幕采集状态", 1, context_field=True),

    # --- permissioned ---
    Capability("location", "你大概在哪里（只看地点标签，不看具体地址）", 1,
               wake_source=True, debounce_sec=60.0, context_field=True, query_tool=True),
    # Motion remains pullable context, but is intentionally not a wake source.
    # It changes too frequently to justify interrupting the user, matching the
    # Runtime V2 differ's motion_state suppression.
    Capability("motion", "你在动还是静止", 1, context_field=True),
    Capability("calendar", "日历下一场日程", 1, context_field=True, query_tool=True),
    Capability("now_playing", "你在听的音乐", 1, context_field=True, query_tool=True),
    Capability("focus", "专注模式", 1, context_field=True),
    Capability("audio_route", "音频输出路由", 2, query_tool=True),
    # One switch covers BOTH resolutions (current field + the recent_apps
    # history tool), so the copy has to say so — otherwise the user thinks they
    # are allowing "which app now" and is actually allowing a usage trajectory.
    Capability("app", "你最近打开了哪些 app（通过 iOS 快捷指令上报，含最近使用记录）", 1,
               context_field=True, query_tool=True),
    Capability("weather", "粗天气", 2, query_tool=True),
    Capability("photos", "你拍的照片", 2, wake_source=True, query_tool=True),
    Capability("reminders", "提醒事项", 2, query_tool=True),
    Capability("health_sleep", "睡眠", 2, query_tool=True),
    Capability("health_workout", "运动", 2, query_tool=True),
    Capability("health_vitals", "身体趋势", 2, query_tool=True),
    Capability("health_activity", "活动量（能量/锻炼/站立/正念）", 2, query_tool=True),
    Capability("health_body", "身体测量（体重/BMI/体脂/身高）", 2, query_tool=True),
    Capability("health_metabolic", "代谢点值（血糖/血压）", 2, query_tool=True),
    Capability("health_cycle", "经期", 2, query_tool=True),
    Capability("health_mood", "心情 / State of Mind", 2, query_tool=True),
]}


# ---------------------------------------------------------------------------
# Signals (report inputs) — keyed by the iOS context_snapshot `key`.
# `data` (a JSON string) parses into the shapes in perception-report-fields.md;
# the resolver picks out the label/state fields and DISCARDS raw/precise fields
# (coordinates, BSSID, placemark address) so the agent only sees coarse state.
# ---------------------------------------------------------------------------

SIGNALS: dict[str, Signal] = {s.input: s for s in [
    # always-on
    Signal("time", "time", ("local_time", "timezone", "locale"),
           resolver="time", ttl_sec=300.0, significant=False),
    Signal("battery", "device", ("battery_level", "charging"),
           resolver="battery", ttl_sec=600.0, significant=False),
    Signal("broadcast", "broadcast", ("broadcast_state", "broadcast_active"),
           resolver="broadcast", ttl_sec=300.0, significant=False),
    Signal("focus", "focus", ("focus_authorization_status", "in_focus"),
           resolver="focus_presence", ttl_sec=300.0, significant=False),

    # permissioned
    Signal("location_signal", "location", ("place_label", "wifi_label", "country", "locality", "wifi_anchor_id"),
           resolver="location_signal", ttl_sec=900.0),
    Signal("motion_state", "motion", ("motion_state",), ttl_sec=300.0),
    Signal("calendar_next_event", "calendar", ("calendar_next_event", "calendar_events", "calendar_events_truncated"),
           ttl_sec=3600.0, significant=False),
    Signal("playback", "now_playing", ("now_playing",),
           ttl_sec=600.0, significant=False),
    Signal("audio_route", "audio_route", ("output_type", "is_bluetooth", "device_name"),
           resolver="audio_route", ttl_sec=600.0, significant=False),
    Signal("weather", "weather",
           ("condition", "temperature", "apparent_temperature", "humidity",
            "precipitation_chance", "uv_index", "is_daylight", "alerts"),
           resolver="weather", ttl_sec=1800.0, significant=False),
    Signal("reminders", "reminders",
           ("next_reminder", "reminders", "overdue_count", "due_today_count", "reminders_truncated"),
           ttl_sec=3600.0, significant=False),
    Signal("health_sleep", "health_sleep",
           ("asleep_minutes", "core_minutes", "deep_minutes", "rem_minutes"),
           resolver="health_sleep", ttl_sec=86400.0, significant=False),
    Signal("health_workout", "health_workout", ("workout_type", "duration_min", "count_today"),
           resolver="health_workout", ttl_sec=86400.0, significant=False),
    Signal("health_vitals", "health_vitals",
           ("resting_heart_rate", "step_count", "current_heart_rate", "hrv_sdnn_ms",
            "respiratory_rate", "oxygen_saturation_pct", "vo2_max"),
           resolver="health_vitals", ttl_sec=3600.0, significant=False),
    Signal("health_activity", "health_activity",
           ("active_energy_kcal", "exercise_minutes", "stand_minutes", "mindful_minutes"),
           ttl_sec=3600.0, significant=False),
    Signal("health_body", "health_body",
           ("weight_kg", "bmi", "body_fat_pct", "height_cm"),
           ttl_sec=86400.0, significant=False),
    Signal("health_metabolic", "health_metabolic",
           ("blood_glucose_mmol_l", "blood_pressure_systolic", "blood_pressure_diastolic"),
           ttl_sec=86400.0, significant=False),
    Signal("health_cycle", "health_cycle",
           ("flow_level", "is_active_period"),
           ttl_sec=86400.0, significant=False),
    Signal("health_mood", "health_mood",
           ("valence", "valence_classification", "kind", "label_count", "recorded_today"),
           ttl_sec=86400.0, significant=False),
    # `app` is reported via the GET /app_open + /app_close shortcut endpoints
    # (not /report); this entry exists so app_name/app_category/app_state appear
    # in the snapshot with a TTL. 900s, not the 300s this used to be: an app
    # session routinely outlives a 5-minute window, so a shorter TTL made the
    # agent see app_name=None for any turn that wasn't right after a launch.
    # `app_state` is what distinguishes "still in it" (foreground) from "just
    # left it" (closed); `recent_apps` covers anything older.
    #
    # NOTE: this `app_state` (values "foreground" / "closed" / null) is about
    # some OTHER app on the user's phone — the one the iOS Shortcut just
    # opened or closed. It is UNRELATED to the `app_state` field found in
    # device_events / proactive scene-phase code (backend/proactive/service.py,
    # backend/push/service.py, backend/proactive/capture_scheduler.py), which
    # tracks IO's OWN foreground/background/inactive phase. Same name,
    # different namespace (perception_state field vs. device_events payload),
    # no functional overlap.
    Signal("app", "app", ("app_name", "app_category", "app_state"),
           ttl_sec=900.0, significant=False),
]}


# Back-compat aliases: canonical capability names also map to the iOS key.
KEY_ALIASES = {
    "location": "location_signal",
    "motion": "motion_state",
    "now_playing": "playback",
    "calendar": "calendar_next_event",
}

# iOS keys that carry only null placeholders (frontmost_app/silent_mode/focus/
# precise_unlock — not obtainable on iOS). Accepted and silently ignored.
IGNORED_KEYS = {"unsupported"}

# Composite report keys whose `data` expands into several signals. (none now —
# battery is its own iOS key.)
COMPOSITE_KEYS: dict[str, list[str]] = {}


# perception_items kinds (collection-style data; see migration 0002) and the
# capability that gates the GENERIC /items endpoint for each kind.
# NOTE: "photo" is intentionally ABSENT from KIND_CAPABILITY — photos must go
# through the dedicated /photo/evaluate flow, which stores the encrypted
# envelope in the frame channel. Allowing kind=photo via /items would let a
# caller inject a confirmed photo doc without the envelope path.
# (calendar is reported via /report, not /items.)
KIND_CAPABILITY = {
    "workout": "health_workout",
    "sleep": "health_sleep",
    "vitals": "health_vitals",
}

# Burst de-dup backstop. Clustering is primarily done ON DEVICE (iOS collapses a
# 30s burst and uploads only the representative frame); this window is the
# server-side safety net so a client that uploads several still wakes once.
PHOTO_CLUSTER_SEC = 30.0

# scene_hint — the canonical enum shared with the iOS Vision classifier. The
# client MUST emit one of these strings; anything else is treated as "other".
# V2 removes the old platform hard block: sensitive hints are metadata for the
# companion's expression policy, not a perception gate.
SCENE_HINTS = (
    # non-sensitive — may reach the agent
    "landscape", "food", "people", "pet", "activity", "object", "art",
    "text_note", "other",
    # contextual
    "private", "receipt",
    # objectively sensitive
    "document", "id_card", "medical", "screenshot",
)
SENSITIVE_PHOTO_SCENES = {"private", "receipt", "document", "id_card", "medical", "screenshot"}

# Extended on-device metadata the iOS Vision pass may include. The runtime does
# not use these fields as a gate; they are passed through as context for the
# agent's own judgment and voice.
PHOTO_METADATA_FIELDS = (
    "has_faces", "face_count", "scene_hint", "scene_confidence",
    "time_of_day", "is_burst", "is_indoor", "has_text_block", "is_screenshot",
)
# "Back after long lock" wake threshold.
UNLOCK_BACK_THRESHOLD_SEC = 1800.0  # 30 min

# Legacy recent app-open rows surfaced in the snapshot (folds the standalone
# /app_usage read; capped to keep the wake-attached snapshot small). The merged
# TTL-bounded open/close trajectory is exposed separately as recent_app_events.
RECENT_APPS_LIMIT = 10

# Explicit `perception.recent_apps` pulls merge the existing open/close streams,
# but an agent asking "what have I been using today?" wants more than the wake
# budget.
RECENT_APPS_TOOL_LIMIT = 20
RECENT_APPS_TOOL_MAX = 100
