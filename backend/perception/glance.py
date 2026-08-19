"""Pure, number-free projections for Runtime V2 proactive perception."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import sys
from typing import Any, Callable

_HEALTH_SIGNALS = ("steps", "sleep", "workout", "vitals", "activity", "body", "metabolic", "cycle")
_HEALTH_HISTORY = frozenset({
    "health_vitals", "health_sleep", "health_workout", "health_activity",
    "health_body", "health_metabolic", "health_cycle",
})
_EXPIRED_MARKERS = frozenset({"expired", "stale", "is_expired", "is_stale"})
_EVENT_FIELDS = {
    "unlock_after_absence": {"trigger": "unlock_after_absence", "returned_after_absence": True},
    "arrived_at_anchor": {"trigger": "arrived_at_anchor", "anchor_changed": True},
    "photo_added": {"trigger": "photo_added", "new_photo": True},
    "scene_change": {"trigger": "scene_change"},
    "broadcast_opened": {"trigger": "broadcast_opened", "screen_share_started": True},
    "broadcast_closed": {"trigger": "broadcast_closed", "screen_share_ended": True},
}
V1_PRESENCE_HINT_FIELDS = (
    "place_label",
    "motion_state",
    "now_playing",
    "locale",
    "broadcast_state",
    "broadcast_active",
)


def _finite_number(value: Any) -> bool:
    if type(value) is int:
        # Preserve the projector's historical "finite float" input range
        # without coercing an arbitrary-precision int through float(), which
        # raises OverflowError for malformed giant values.
        return -sys.float_info.max <= value <= sys.float_info.max
    if type(value) is float:
        return math.isfinite(value)
    return False


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _items(value: Any, predicate: Callable[[Any], bool]) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and any(predicate(item) for item in value)
    )


def _event(value: Any) -> bool:
    return isinstance(value, Mapping) and any((
        _text(value.get("title")),
        _text(value.get("start_time")),
        _finite_number(value.get("starts_in_min")),
        _finite_number(value.get("minutes_until_start")),
    ))


def _reminder(value: Any) -> bool:
    return isinstance(value, Mapping) and any((
        _text(value.get("title")),
        _text(value.get("due_time")),
        _text(value.get("due_date")),
        type(value.get("overdue")) is bool,
    ))


def _alert(value: Any) -> bool:
    return isinstance(value, Mapping) and any(
        _text(value.get(field)) for field in ("title", "summary", "headline", "description", "severity")
    )


def _doc(signals: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = signals.get(name)
    if (
        not isinstance(value, Mapping)
        or value.get("disabled") is True
        or value.get("fresh") is False
        or any(value.get(marker) is True for marker in _EXPIRED_MARKERS)
    ):
        return {}
    return value


def _available(
    doc: Mapping[str, Any],
    *,
    number_fields: Sequence[str] = (),
    text_fields: Sequence[str] = (),
    bool_fields: Sequence[str] = (),
    event_fields: Sequence[str] = (),
) -> bool:
    return any(_finite_number(doc.get(field)) for field in number_fields) or any(
        _text(doc.get(field)) for field in text_fields
    ) or any(type(doc.get(field)) is bool for field in bool_fields) or any(
        _event(doc.get(field)) for field in event_fields
    )


def _positive_count(value: Any) -> bool:
    return _finite_number(value) and value > 0


def build_perception_glance(
    signals: Mapping[str, Mapping[str, Any]],
    *,
    notable_changes: Sequence[Mapping[str, Any]] = (),
) -> dict[str, dict[str, bool]]:
    safe_signals = signals if isinstance(signals, Mapping) else {}
    changed = {
        str(item.get("signal") or "")
        for item in notable_changes
        if isinstance(item, Mapping)
    }
    out: dict[str, dict[str, bool]] = {}
    location = _doc(safe_signals, "location")
    if _available(location, text_fields=("place_label", "wifi_label", "country", "locality", "wifi_anchor_id")):
        out["location"] = {"available": True, "notable_change": "location_signal" in changed}
    now = _doc(safe_signals, "now")
    playing = now.get("now_playing")
    if isinstance(playing, Mapping) and _available(playing, text_fields=("title", "artist", "album", "playback_state")):
        out["media"] = {"available": True, "active": True, "notable_change": "playback" in changed}
    app = _doc(safe_signals, "app")
    if _available(app, text_fields=("app_name", "app_category", "app_state")):
        out["app"] = {"available": True, "recent_activity": True}
    health_docs = [_doc(safe_signals, name) for name in _HEALTH_SIGNALS]
    if any((
        _available(health_docs[0], number_fields=("step_count",)),
        _available(health_docs[1], number_fields=("asleep_minutes", "core_minutes", "deep_minutes", "rem_minutes")),
        _available(health_docs[2], number_fields=("duration_min", "count_today"), text_fields=("workout_type",)),
        _available(health_docs[3], number_fields=("resting_heart_rate", "step_count", "current_heart_rate", "hrv_sdnn_ms", "respiratory_rate", "oxygen_saturation_pct", "vo2_max")),
        _available(health_docs[4], number_fields=("active_energy_kcal", "exercise_minutes", "stand_minutes", "mindful_minutes")),
        _available(health_docs[5], number_fields=("weight_kg", "bmi", "body_fat_pct", "height_cm")),
        _available(health_docs[6], number_fields=("blood_glucose_mmol_l", "blood_pressure_systolic", "blood_pressure_diastolic")),
        _available(health_docs[7], text_fields=("flow_level",), bool_fields=("is_active_period",)),
    )):
        out["health"] = {"available": True, "notable_change": bool(changed & _HEALTH_HISTORY)}
    weather = _doc(safe_signals, "weather")
    if _available(
        weather,
        number_fields=("temperature", "apparent_temperature", "humidity", "precipitation_chance", "uv_index"),
        text_fields=("condition",),
        bool_fields=("is_daylight",),
    ) or _items(weather.get("alerts"), _alert):
        out["weather"] = {"available": True, "notable_change": "weather" in changed}
    mood = _doc(safe_signals, "mood")
    if _available(mood, number_fields=("valence", "label_count"), text_fields=("valence_classification", "kind"), bool_fields=("recorded_today",)):
        out["mood"] = {"available": True, "recorded": mood.get("recorded_today") is True}
    reminders = _doc(safe_signals, "reminders")
    if _available(
        reminders,
        number_fields=("overdue_count", "due_today_count"),
        text_fields=("next_reminder",),
        bool_fields=("reminders_truncated",),
    ) or _items(reminders.get("reminders"), _reminder):
        out["reminders"] = {
            "available": True,
            "has_due": _positive_count(reminders.get("due_today_count")),
            "has_overdue": _positive_count(reminders.get("overdue_count")),
        }
    calendar = _doc(safe_signals, "calendar")
    if _available(calendar, event_fields=("calendar_next_event",)) or _items(calendar.get("calendar_events"), _event):
        out["calendar"] = {
            "available": True,
            "has_upcoming": _event(calendar.get("calendar_next_event")) or _items(calendar.get("calendar_events"), _event),
        }
    return out


def project_perception_wake_events(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project the producer-bounded wake facts, excluding internal cursor data.

    ``serve_worker._read_perception_wake_context`` is the trust boundary that
    limits every string/list/scalar.  The old projection discarded those facts
    and retained only a trigger-derived constant, leaving the provider unable to
    tell what actually changed.  Copy only the closed field set consumed by the
    prompt; never pass through arbitrary keys from storage.
    """
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        trigger = str(item.get("trigger") or "")
        if trigger not in _EVENT_FIELDS:
            continue
        projected: dict[str, Any] = dict(_EVENT_FIELDS[trigger])
        for field, cap in (
            ("wake_id", 160),
            ("source", 120),
            ("change_digest", 2000),
        ):
            value = str(item.get(field) or "")[:cap]
            if value:
                projected[field] = value
        origin_refs = [
            str(ref)[:200] for ref in list(item.get("origin_refs") or [])[:10]
        ]
        if origin_refs:
            projected["origin_refs"] = origin_refs
        raw_hints = item.get("presence_hints")
        hints: dict[str, bool | int | float | str] = {}
        if isinstance(raw_hints, Mapping):
            for key in V1_PRESENCE_HINT_FIELDS:
                value = raw_hints.get(key)
                safe_key = str(key)[:80]
                if isinstance(value, bool):
                    hints[safe_key] = value
                elif isinstance(value, int):
                    hints[safe_key] = value
                elif isinstance(value, float) and math.isfinite(value):
                    hints[safe_key] = value
                elif isinstance(value, str):
                    hints[safe_key] = value[:200]
        if hints:
            projected["presence_hints"] = hints
        if trigger == "photo_added":
            for field, cap in (("photo_id", 160), ("scene", 200), ("time_of_day", 80)):
                value = str(item.get(field) or "")[:cap]
                if value:
                    projected[field] = value
        out.append(projected)
    return out


def perception_glance_fingerprint(glance: Mapping[str, Any]) -> str:
    canonical = json.dumps(glance, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
