"""Perception Differ V2 event renderer.

The existing perception service stores current coarse state. The v2 differ is
the layer between durable raw-signal decisions and wake creation: it emits only
discrete wake events and prepares cheap digests/presence hints for the runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable, Mapping

from perception import signal_state_v2
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


_DURABLE_WAKE_SIGNALS = frozenset({
    "connectivity_anchor",
    "wifi_anchor",
    "bluetooth_anchor",
    "unlock_after_absence",
    "screen_phash",
    "photo_added",
})


def _format_value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, dict):
        for key in ("anchor_id", "label", "state", "value"):
            if value.get(key) is not None:
                return str(value.get(key))
    return str(value)


class PerceptionDifferV2:
    """Render durable signal decisions into wake events and presence hints."""

    def __init__(
        self,
        *,
        metrics_sink: MetricsSinkV2 | None = None,
        observe_state: (
            Callable[..., signal_state_v2.SignalObservationDecision] | None
        ) = None,
    ) -> None:
        self.metrics_sink = metrics_sink
        self._observe_state = observe_state or signal_state_v2.observe_signal_state

    def observe(
        self,
        user_id: str,
        signal: str,
        value: Any,
        *,
        ts: float | None = None,
        source_event_id: str | None = None,
        allow_first_event: bool = False,
    ) -> DifferResultV2:
        ts = time.time() if ts is None else float(ts)
        if signal in _DURABLE_WAKE_SIGNALS:
            decision = self._observe_state(
                user_id,
                signal,
                value,
                observed_at=ts,
                source_event_id=source_event_id,
                allow_first_event=allow_first_event,
            )
            changed = decision.changed
            last_seen_ts = (
                decision.last_seen_at.timestamp()
                if decision.last_seen_at is not None
                else ts
            )
            last_changed_ts = (
                decision.last_changed_at.timestamp()
                if decision.last_changed_at is not None
                else last_seen_ts
            )
            outcome = decision.outcome
        else:
            changed = False
            last_seen_ts = ts
            last_changed_ts = ts
            outcome = "unchanged"
        state = SignalStateV2(
            value=value,
            last_seen_ts=last_seen_ts,
            last_changed_ts=last_changed_ts,
        )
        digest = f"{signal}: {outcome} ({_format_value(value)})"
        events = self._events_for(signal, value, None, digest, changed)
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
