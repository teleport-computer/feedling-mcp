"""V2 proactive control semantics.

This module owns the Round 3 wake/delivery/autonomy switch split. It is
deliberately independent from legacy `enabled`, `dnd`, `user_state`, and
`ai_state` behavior; those fields are accepted only as compatibility input.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SWITCH_AMBIENT = "ambient"
SWITCH_SCHEDULED = "scheduled"
SWITCH_REMINDERS_DELIVERY = "reminders_delivery"
SWITCH_DREAM_ENABLED = "dream_enabled"
SWITCH_CAPTURE_ENABLED = "capture_enabled"
SWITCH_SCREEN_WATCH_ENABLED = "screen_watch_enabled"
SWITCH_PHOTO_WAKE_ENABLED = "photo_wake_enabled"
SWITCH_ARRIVAL_WAKE_ENABLED = "arrival_wake_enabled"
SWITCH_UNLOCK_WAKE_ENABLED = "unlock_wake_enabled"
SWITCH_KEYS_V2 = (
    SWITCH_AMBIENT,
    SWITCH_SCHEDULED,
    SWITCH_REMINDERS_DELIVERY,
    SWITCH_DREAM_ENABLED,
    SWITCH_CAPTURE_ENABLED,
    SWITCH_SCREEN_WATCH_ENABLED,
    SWITCH_PHOTO_WAKE_ENABLED,
    SWITCH_ARRIVAL_WAKE_ENABLED,
    SWITCH_UNLOCK_WAKE_ENABLED,
)

DEFAULT_TIMEZONE_V2 = os.environ.get("FEEDLING_DEFAULT_TIMEZONE", "Asia/Shanghai").strip() or "UTC"
SETTINGS_KIND_V2 = "proactive_settings_v2"
SETTINGS_SCHEMA_VERSION_V2 = 2

SCHEDULED_WAKE_SOURCE_V2 = "scheduled_wake"
USER_MESSAGE_SOURCE_V2 = "user_message"
BACKGROUND_RESULT_SOURCE_V2 = "background_result"


def default_switches_v2() -> dict[str, bool]:
    return {
        SWITCH_AMBIENT: True,
        SWITCH_SCHEDULED: True,
        SWITCH_REMINDERS_DELIVERY: True,
        SWITCH_DREAM_ENABLED: True,
        SWITCH_CAPTURE_ENABLED: True,
        SWITCH_SCREEN_WATCH_ENABLED: True,
        SWITCH_PHOTO_WAKE_ENABLED: True,
        SWITCH_ARRIVAL_WAKE_ENABLED: True,
        SWITCH_UNLOCK_WAKE_ENABLED: True,
    }


def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "disabled"}:
            return False
    return default


def _valid_timezone(value: Any, default: str = DEFAULT_TIMEZONE_V2) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return default
    try:
        ZoneInfo(candidate)
        return candidate
    except ZoneInfoNotFoundError:
        return default


def _explicit_switches(raw: Mapping[str, Any]) -> dict[str, bool]:
    switches: dict[str, bool] = {}
    nested = raw.get("switches")
    if isinstance(nested, Mapping):
        for key in SWITCH_KEYS_V2:
            if key in nested:
                switches[key] = _bool_value(nested.get(key), True)
    for key in SWITCH_KEYS_V2:
        if key in raw:
            switches[key] = _bool_value(raw.get(key), True)
    return switches


def compatibility_switches_v2(raw: Mapping[str, Any]) -> dict[str, bool]:
    """Map old app fields to V2 switches at the compatibility boundary only.

    `enabled=false` becomes Ambient off. `dnd=true` becomes Delivery off.
    Old `user_state` and `ai_state` do not become V2 gates; D6 removes
    `ai_state`, and V2 must not resurrect `away` as a second wake gate.
    """
    switches: dict[str, bool] = {}
    explicit = _explicit_switches(raw)
    if SWITCH_AMBIENT not in explicit and "enabled" in raw:
        switches[SWITCH_AMBIENT] = _bool_value(raw.get("enabled"), True)
    if SWITCH_REMINDERS_DELIVERY not in explicit and "dnd" in raw:
        switches[SWITCH_REMINDERS_DELIVERY] = not _bool_value(raw.get("dnd"), False)
    return switches


@dataclass(frozen=True)
class ProactiveSettingsV2:
    ambient: bool = True
    scheduled: bool = True
    reminders_delivery: bool = True
    dream_enabled: bool = True
    capture_enabled: bool = True
    screen_watch_enabled: bool = True
    photo_wake_enabled: bool = True
    arrival_wake_enabled: bool = True
    unlock_wake_enabled: bool = True
    timezone: str = DEFAULT_TIMEZONE_V2
    updated_at: str = ""

    def switches(self) -> dict[str, bool]:
        return {
            SWITCH_AMBIENT: bool(self.ambient),
            SWITCH_SCHEDULED: bool(self.scheduled),
            SWITCH_REMINDERS_DELIVERY: bool(self.reminders_delivery),
            SWITCH_DREAM_ENABLED: bool(self.dream_enabled),
            SWITCH_CAPTURE_ENABLED: bool(self.capture_enabled),
            SWITCH_SCREEN_WATCH_ENABLED: bool(self.screen_watch_enabled),
            SWITCH_PHOTO_WAKE_ENABLED: bool(self.photo_wake_enabled),
            SWITCH_ARRIVAL_WAKE_ENABLED: bool(self.arrival_wake_enabled),
            SWITCH_UNLOCK_WAKE_ENABLED: bool(self.unlock_wake_enabled),
        }


def resolve_settings_v2(raw: ProactiveSettingsV2 | Mapping[str, Any] | None = None) -> ProactiveSettingsV2:
    if isinstance(raw, ProactiveSettingsV2):
        return raw
    doc = raw if isinstance(raw, Mapping) else {}
    switches = default_switches_v2()
    switches.update(compatibility_switches_v2(doc))
    switches.update(_explicit_switches(doc))
    return ProactiveSettingsV2(
        ambient=switches[SWITCH_AMBIENT],
        scheduled=switches[SWITCH_SCHEDULED],
        reminders_delivery=switches[SWITCH_REMINDERS_DELIVERY],
        dream_enabled=switches[SWITCH_DREAM_ENABLED],
        capture_enabled=switches[SWITCH_CAPTURE_ENABLED],
        screen_watch_enabled=switches[SWITCH_SCREEN_WATCH_ENABLED],
        photo_wake_enabled=switches[SWITCH_PHOTO_WAKE_ENABLED],
        arrival_wake_enabled=switches[SWITCH_ARRIVAL_WAKE_ENABLED],
        unlock_wake_enabled=switches[SWITCH_UNLOCK_WAKE_ENABLED],
        timezone=_valid_timezone(doc.get("timezone"), DEFAULT_TIMEZONE_V2),
        updated_at=str(doc.get("updated_at") or ""),
    )


def settings_v2_to_doc(settings: ProactiveSettingsV2 | Mapping[str, Any] | None = None) -> dict[str, Any]:
    resolved = resolve_settings_v2(settings)
    return {
        "kind": SETTINGS_KIND_V2,
        "schema_version": SETTINGS_SCHEMA_VERSION_V2,
        "switches": resolved.switches(),
        "timezone": resolved.timezone,
        "updated_at": resolved.updated_at,
    }


def merge_settings_patch_v2(
    current: ProactiveSettingsV2 | Mapping[str, Any] | None,
    patch: Mapping[str, Any] | None,
    *,
    updated_at: str | None = None,
) -> ProactiveSettingsV2:
    base = resolve_settings_v2(current)
    patch_doc = patch if isinstance(patch, Mapping) else {}
    switches = base.switches()
    switches.update(compatibility_switches_v2(patch_doc))
    switches.update(_explicit_switches(patch_doc))
    timezone = base.timezone
    if "timezone" in patch_doc:
        timezone = _valid_timezone(patch_doc.get("timezone"), base.timezone)
    return ProactiveSettingsV2(
        ambient=switches[SWITCH_AMBIENT],
        scheduled=switches[SWITCH_SCHEDULED],
        reminders_delivery=switches[SWITCH_REMINDERS_DELIVERY],
        dream_enabled=switches[SWITCH_DREAM_ENABLED],
        capture_enabled=switches[SWITCH_CAPTURE_ENABLED],
        screen_watch_enabled=switches[SWITCH_SCREEN_WATCH_ENABLED],
        photo_wake_enabled=switches[SWITCH_PHOTO_WAKE_ENABLED],
        arrival_wake_enabled=switches[SWITCH_ARRIVAL_WAKE_ENABLED],
        unlock_wake_enabled=switches[SWITCH_UNLOCK_WAKE_ENABLED],
        timezone=timezone,
        updated_at=updated_at if updated_at is not None else str(patch_doc.get("updated_at") or base.updated_at),
    )


@dataclass(frozen=True)
class WakeControlDecisionV2:
    accepted: bool
    reason: str
    settings: ProactiveSettingsV2
    transparency_required: bool = False

    @property
    def switches(self) -> dict[str, bool]:
        return self.settings.switches()


@dataclass(frozen=True)
class DeliveryDecisionV2:
    allow_chat_write: bool
    allow_visible_delivery: bool
    reason: str
    settings: ProactiveSettingsV2

    @property
    def allow_push(self) -> bool:
        return self.allow_visible_delivery

    @property
    def switches(self) -> dict[str, bool]:
        return self.settings.switches()


@dataclass(frozen=True)
class ScheduledActionDecisionV2:
    accepted: bool
    reason: str
    settings: ProactiveSettingsV2
    transparency_required: bool = False

    @property
    def switches(self) -> dict[str, bool]:
        return self.settings.switches()


def evaluate_wake_control_v2(
    source: str,
    *,
    trigger: str | None = None,
    manual: bool = False,
    settings: ProactiveSettingsV2 | Mapping[str, Any] | None = None,
) -> WakeControlDecisionV2:
    resolved = resolve_settings_v2(settings)
    normalized = str(source or "").strip()
    normalized_trigger = str(trigger or "").strip().lower()
    if manual:
        return WakeControlDecisionV2(True, "manual_bypass", resolved)
    if normalized == USER_MESSAGE_SOURCE_V2:
        return WakeControlDecisionV2(True, "user_message_bypass", resolved)
    if (normalized == "heartbeat" or normalized_trigger.startswith("heartbeat")) and not resolved.ambient:
        return WakeControlDecisionV2(False, "ambient_disabled", resolved)
    if normalized_trigger == "photo_added" and not resolved.photo_wake_enabled:
        return WakeControlDecisionV2(False, "photo_wake_disabled", resolved)
    if normalized_trigger == "arrived_at_anchor" and not resolved.arrival_wake_enabled:
        return WakeControlDecisionV2(False, "arrival_wake_disabled", resolved)
    if normalized_trigger == "unlock_after_absence" and not resolved.unlock_wake_enabled:
        return WakeControlDecisionV2(False, "unlock_wake_disabled", resolved)
    if (
        normalized == "scene_change"
        or normalized_trigger in {"scene_change", "screen_watch", "screen_tick", "broadcast_opened"}
    ) and not resolved.screen_watch_enabled:
        return WakeControlDecisionV2(False, "screen_watch_disabled", resolved)
    if normalized == SCHEDULED_WAKE_SOURCE_V2 and not resolved.scheduled:
        return WakeControlDecisionV2(False, "scheduled_disabled", resolved, transparency_required=True)
    return WakeControlDecisionV2(True, "allowed", resolved)


def evaluate_delivery_v2(
    settings: ProactiveSettingsV2 | Mapping[str, Any] | None = None,
    *,
    source: str = "",
    manual: bool = False,
) -> DeliveryDecisionV2:
    resolved = resolve_settings_v2(settings)
    normalized = str(source or "").strip()
    if manual:
        return DeliveryDecisionV2(True, True, "manual_bypass", resolved)
    if normalized == USER_MESSAGE_SOURCE_V2:
        return DeliveryDecisionV2(True, True, "user_message_bypass", resolved)
    if not resolved.reminders_delivery:
        return DeliveryDecisionV2(True, False, "reminders_delivery_disabled", resolved)
    return DeliveryDecisionV2(True, True, "allowed", resolved)


def evaluate_scheduled_action_v2(
    action: str | Mapping[str, Any],
    settings: ProactiveSettingsV2 | Mapping[str, Any] | None = None,
    *,
    manual: bool = False,
) -> ScheduledActionDecisionV2:
    resolved = resolve_settings_v2(settings)
    action_type = str(action.get("type") if isinstance(action, Mapping) else action or "").strip()
    if action_type in {"schedule_wake", "cancel_wake"} and not resolved.scheduled:
        return ScheduledActionDecisionV2(False, "scheduled_disabled", resolved, transparency_required=True)
    if manual:
        return ScheduledActionDecisionV2(True, "manual_bypass", resolved)
    return ScheduledActionDecisionV2(True, "allowed", resolved)


def now_settings_updated_at_v2() -> str:
    return datetime.now().isoformat()
