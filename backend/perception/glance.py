"""Pure, number-free projections for Runtime V2 proactive perception."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from typing import Any

_HEALTH_SIGNALS = ("steps", "sleep", "workout", "vitals", "activity", "body", "metabolic", "cycle")
_HEALTH_HISTORY = frozenset({
    "health_vitals", "health_sleep", "health_workout", "health_activity",
    "health_body", "health_metabolic", "health_cycle",
})
_EVENT_FIELDS = {
    "unlock_after_absence": {"trigger": "unlock_after_absence", "returned_after_absence": True},
    "arrived_at_anchor": {"trigger": "arrived_at_anchor", "anchor_changed": True},
    "photo_added": {"trigger": "photo_added", "new_photo": True},
    "scene_change": {"trigger": "scene_change"},
}


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_present(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_present(item) for item in value)
    return False


def _doc(signals: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = signals.get(name)
    if not isinstance(value, Mapping) or value.get("disabled") is True:
        return {}
    return value


def _available(doc: Mapping[str, Any]) -> bool:
    return any(_present(value) for key, value in doc.items() if key not in {"disabled", "reason"})


def _positive_count(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(float(value)) and float(value) > 0


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
    if _available(location):
        out["location"] = {"available": True, "notable_change": "location_signal" in changed}
    now = _doc(safe_signals, "now")
    playing = now.get("now_playing")
    if _present(playing):
        out["media"] = {"available": True, "active": True, "notable_change": "playback" in changed}
    app = _doc(safe_signals, "app")
    if _available(app):
        out["app"] = {"available": True, "recent_activity": True}
    health_docs = [_doc(safe_signals, name) for name in _HEALTH_SIGNALS]
    if any(_available(value) for value in health_docs):
        out["health"] = {"available": True, "notable_change": bool(changed & _HEALTH_HISTORY)}
    weather = _doc(safe_signals, "weather")
    if _available(weather):
        out["weather"] = {"available": True, "notable_change": "weather" in changed}
    mood = _doc(safe_signals, "mood")
    if _available(mood):
        out["mood"] = {"available": True, "recorded": mood.get("recorded_today") is True}
    reminders = _doc(safe_signals, "reminders")
    if _available(reminders):
        out["reminders"] = {
            "available": True,
            "has_due": _positive_count(reminders.get("due_today_count")),
            "has_overdue": _positive_count(reminders.get("overdue_count")),
        }
    calendar = _doc(safe_signals, "calendar")
    if _available(calendar):
        out["calendar"] = {
            "available": True,
            "has_upcoming": _present(calendar.get("calendar_next_event")) or _present(calendar.get("calendar_events")),
        }
    return out


def project_perception_wake_events(items: Sequence[Mapping[str, Any]]) -> list[dict[str, bool | str]]:
    return [dict(_EVENT_FIELDS[trigger]) for item in items if isinstance(item, Mapping)
            if (trigger := str(item.get("trigger") or "")) in _EVENT_FIELDS]


def perception_glance_fingerprint(glance: Mapping[str, Any]) -> str:
    canonical = json.dumps(glance, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
