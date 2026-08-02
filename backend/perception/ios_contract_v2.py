"""iOS perception payload contract for the Round 3 runtime.

This module is intentionally parse/classify only. PR7 can route these parsed
signals into storage, decryption, and PerceptionDifferV2; PR6b only locks the
current iOS producer shape so the cutover does not guess at client payloads.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping


OPERATION_SIGNAL_KEYS_V2 = frozenset({"time", "battery", "broadcast", "focus"})
ENCRYPTED_SIGNAL_KEYS_V2 = frozenset({
    "location_signal",
    "motion_state",
    "calendar_next_event",
    "playback",
    "audio_route",
    "weather",
    "reminders",
    "health_sleep",
    "health_workout",
    "health_vitals",
    "health_activity",
    "health_body",
    "health_metabolic",
    "health_cycle",
    "health_mood",
})
IGNORED_SIGNAL_KEYS_V2 = frozenset({"unsupported"})

EXPECTED_REPORT_KEYS_V2 = (
    "time",
    "battery",
    "broadcast",
    "location_signal",
    "motion_state",
    "focus",
    "calendar_next_event",
    "playback",
    "audio_route",
    "weather",
    "reminders",
    "health_sleep",
    "health_workout",
    "health_vitals",
    "health_activity",
    "health_body",
    "health_metabolic",
    "health_cycle",
    "health_mood",
    "unsupported",
)

DIFFER_INPUTS_BY_IOS_KEY_V2 = {
    "time": ("time",),
    "battery": ("battery",),
    "broadcast": ("broadcast_state",),
    # Focus is pull/presence context only in V2. It must not revive the old
    # user_state/away gate.
    "focus": (),
    # Encrypted values require enclave decrypt before they can become differ
    # observations. The mapping here says where decrypted values will go.
    "location_signal": ("connectivity_anchor", "wifi_anchor", "place_label"),
    "motion_state": ("motion_state",),
    "calendar_next_event": ("calendar_presence", "calendar_next_event"),
    "playback": ("now_playing",),
    "audio_route": ("audio_route",),
    "weather": ("weather",),
    "reminders": ("reminders",),
    "health_sleep": ("health_sleep",),
    "health_workout": ("health_workout",),
    "health_vitals": ("health_vitals",),
    "health_activity": ("health_activity",),
    "health_body": ("health_body",),
    "health_metabolic": ("health_metabolic",),
    "health_cycle": ("health_cycle",),
    "health_mood": ("health_mood",),
}

WAKE_POLICY_BY_IOS_KEY_V2 = {
    "time": "pull_only",
    "battery": "pull_only",
    "broadcast": "runtime_context_only",
    "focus": "pull_only",
    "location_signal": "differ_after_decrypt",
    "motion_state": "pull_only_after_decrypt",
    "calendar_next_event": "presence_hint_after_decrypt",
    "playback": "pull_only_after_decrypt",
    "audio_route": "pull_only_after_decrypt",
    "weather": "pull_only_after_decrypt",
    "reminders": "pull_only_after_decrypt",
    "health_sleep": "pull_only_after_decrypt",
    "health_workout": "pull_only_after_decrypt",
    "health_vitals": "pull_only_after_decrypt",
    "health_activity": "pull_only_after_decrypt",
    "health_body": "pull_only_after_decrypt",
    "health_metabolic": "pull_only_after_decrypt",
    "health_cycle": "pull_only_after_decrypt",
    "health_mood": "pull_only_after_decrypt",
    "unsupported": "ignored",
}


@dataclass(frozen=True)
class IOSSignalContractV2:
    key: str
    status: str
    wake_policy: str
    differ_inputs: tuple[str, ...] = ()
    data: Any = None
    message: str = ""
    encrypted: bool = False
    requires_decrypt: bool = False
    changed: bool | None = None
    envelope_id: str | None = None


def parse_data_value_v2(data: Any) -> Any:
    """Parse the iOS `data` field.

    Operation signals are JSON strings when present, NSNull/null when missing
    permission, and "" when authorized but currently unavailable.
    """
    if data is None:
        return None
    if isinstance(data, str):
        value = data.strip()
        if not value or value.lower() == "null":
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return data


def classify_item_v2(item: Mapping[str, Any]) -> IOSSignalContractV2:
    key = str(item.get("key") or "").strip()
    message = str(item.get("message") or "")

    if not key:
        return IOSSignalContractV2(
            key="",
            status="missing_key",
            wake_policy="error",
        )

    if key in IGNORED_SIGNAL_KEYS_V2:
        return IOSSignalContractV2(
            key=key,
            status="ignored",
            wake_policy=WAKE_POLICY_BY_IOS_KEY_V2[key],
        )

    if key in OPERATION_SIGNAL_KEYS_V2:
        parsed = parse_data_value_v2(item.get("data"))
        return IOSSignalContractV2(
            key=key,
            status="accepted" if parsed is not None else "unavailable",
            wake_policy=WAKE_POLICY_BY_IOS_KEY_V2[key],
            differ_inputs=DIFFER_INPUTS_BY_IOS_KEY_V2[key],
            data=parsed,
            message=message,
        )

    if key in ENCRYPTED_SIGNAL_KEYS_V2:
        envelope = item.get("envelope")
        if isinstance(envelope, Mapping):
            encrypted = bool(envelope.get("body_ct"))
            raw_changed = item.get("changed")
            if not isinstance(raw_changed, bool):
                return IOSSignalContractV2(
                    key=key,
                    status="invalid_changed_flag",
                    wake_policy="error",
                    encrypted=encrypted,
                    requires_decrypt=encrypted,
                    envelope_id=str(envelope.get("id") or ""),
                )
            return IOSSignalContractV2(
                key=key,
                status="changed" if raw_changed else "unchanged",
                wake_policy=WAKE_POLICY_BY_IOS_KEY_V2[key],
                differ_inputs=DIFFER_INPUTS_BY_IOS_KEY_V2[key],
                encrypted=encrypted,
                requires_decrypt=encrypted,
                changed=raw_changed,
                envelope_id=str(envelope.get("id") or ""),
            )

        parsed = parse_data_value_v2(item.get("data"))
        if parsed is None:
            return IOSSignalContractV2(
                key=key,
                status="unavailable",
                wake_policy="no_wake",
                message=message,
            )
        return IOSSignalContractV2(
            key=key,
            status="invalid_plaintext_sensitive_signal",
            wake_policy="error",
            data=parsed,
            message=message,
        )

    return IOSSignalContractV2(
        key=key,
        status="unknown_signal",
        wake_policy="error",
        data=parse_data_value_v2(item.get("data")),
        message=message,
    )


def classify_report_v2(payload: Mapping[str, Any]) -> tuple[IOSSignalContractV2, ...]:
    items = payload.get("context_snapshot")
    if not isinstance(items, list):
        return ()
    return tuple(classify_item_v2(item) for item in items if isinstance(item, Mapping))


def keys_in_report_v2(payload: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(signal.key for signal in classify_report_v2(payload) if signal.key)


def missing_expected_keys_v2(payload: Mapping[str, Any]) -> tuple[str, ...]:
    present = set(keys_in_report_v2(payload))
    return tuple(key for key in EXPECTED_REPORT_KEYS_V2 if key not in present)
