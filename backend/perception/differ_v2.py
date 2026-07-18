"""Perception Differ V2 skeleton.

The existing perception service stores current coarse state. The v2 differ is
the missing stateful layer between raw signals and wake creation: it tracks
last-seen vs last-changed, emits only discrete wake events, and prepares cheap
digests/presence hints for the runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Mapping

from proactive.observability_v2 import METRIC_PHASH_FRAME, MetricsSinkV2, record_metric_v2


@dataclass(frozen=True)
class SignalStateV2:
    value: Any
    last_seen_ts: float
    last_changed_ts: float


@dataclass(frozen=True)
class DifferEventV2:
    source: str
    trigger: str
    change_digest: str
    presence_hints: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DifferResultV2:
    signal: str
    state: SignalStateV2
    changed: bool
    change_digest: str
    presence_hints: Mapping[str, Any] = field(default_factory=dict)
    events: tuple[DifferEventV2, ...] = ()


def _format_value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, dict):
        for key in ("anchor_id", "label", "state", "value"):
            if value.get(key) is not None:
                return str(value.get(key))
    return str(value)


class PerceptionDifferV2:
    """In-memory differ implementation for the v2 contract.

    Production storage can later move this state to Postgres or to a resident
    local store. The signal semantics live here so hosted and resident can stay
    equivalent.
    """

    def __init__(self, *, metrics_sink: MetricsSinkV2 | None = None) -> None:
        self._state: dict[str, dict[str, SignalStateV2]] = {}
        self.metrics_sink = metrics_sink


    def observe(
        self,
        user_id: str,
        signal: str,
        value: Any,
        *,
        ts: float | None = None,
    ) -> DifferResultV2:
        ts = time.time() if ts is None else float(ts)
        signals = self._state.setdefault(user_id, {})
        prev = signals.get(signal)
        changed = prev is None or prev.value != value
        last_changed_ts = ts if changed else prev.last_changed_ts
        state = SignalStateV2(value=value, last_seen_ts=ts, last_changed_ts=last_changed_ts)
        signals[signal] = state

        digest = (
            f"{signal}: {_format_value(prev.value if prev else None)} -> {_format_value(value)}"
            if changed
            else f"{signal}: stable since {int(last_changed_ts)}"
        )
        events = self._events_for(signal, value, prev, digest, changed)
        hints = self._presence_hints_for(signal, value, changed)
        if signal == "screen_phash":
            record_metric_v2(
                self.metrics_sink,
                user_id=user_id,
                name=METRIC_PHASH_FRAME,
                tags={"outcome": "scene_change" if events else "deduped"},
                data={"changed": changed},
                ts=ts,
            )
        if hints:
            events = tuple(
                DifferEventV2(
                    source=event.source,
                    trigger=event.trigger,
                    change_digest=event.change_digest,
                    presence_hints={**dict(event.presence_hints or {}), **hints},
                    payload=event.payload,
                )
                for event in events
            )
        return DifferResultV2(
            signal=signal,
            state=state,
            changed=changed,
            change_digest=digest,
            presence_hints=hints,
            events=events,
        )

    def _events_for(
        self,
        signal: str,
        value: Any,
        prev: SignalStateV2 | None,
        digest: str,
        changed: bool,
    ) -> tuple[DifferEventV2, ...]:
        if not changed:
            return ()
        if signal in {"motion_state", "battery", "now_playing", "time", "place_label"}:
            return ()
        if signal in {"connectivity_anchor", "wifi_anchor", "bluetooth_anchor"}:
            if value is None:
                return ()
            return (
                DifferEventV2(
                    source="perception_event",
                    trigger="arrived_at_anchor",
                    change_digest=digest,
                    presence_hints={"anchor_changed": True},
                    payload={"anchor": value, "previous_anchor": prev.value if prev else None},
                ),
            )
        if signal == "unlock_after_absence":
            return (
                DifferEventV2(
                    source="perception_event",
                    trigger="unlock_after_absence",
                    change_digest=digest,
                ),
            )
        if signal == "screen_phash":
            return (
                DifferEventV2(
                    source="scene_change",
                    trigger="scene_change",
                    change_digest=digest,
                    payload={"phash": value},
                ),
            )
        if signal == "photo_added":
            return (
                DifferEventV2(
                    source="perception_event",
                    trigger="photo_added",
                    change_digest=digest,
                    payload={"photo": value},
                ),
            )
        return ()

    def _presence_hints_for(self, signal: str, value: Any, changed: bool) -> dict[str, Any]:
        if not changed:
            return {}
        if signal == "calendar_presence" and isinstance(value, dict):
            return {"in_meeting": bool(value.get("in_meeting"))}
        if signal in {"connectivity_anchor", "wifi_anchor", "bluetooth_anchor"}:
            label = value.get("label") if isinstance(value, dict) else value
            return {"entered_anchor": label}
        if signal == "screen_locked":
            return {"screen_locked": bool(value)}
        return {}
