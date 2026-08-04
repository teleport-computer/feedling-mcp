"""V2 perception ingress helpers.

This module is the mechanical bridge between parsed perception observations and
the proactive V2 wake envelope. It does not call models and does not decide
whether content is important; it only lets PerceptionDifferV2 emit discrete
events from observed signal changes.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Iterable, Mapping

from perception.differ_v2 import DifferEventV2, DifferResultV2, PerceptionDifferV2
from proactive.runtime_v2 import WakeEventV2


DEFAULT_DIFFER_V2 = PerceptionDifferV2()


@dataclass(frozen=True)
class IngressObservationV2:
    signal: str
    value: Any
    origin_refs: tuple[str, ...] = ()
    source_event_id: str | None = None
    allow_first_event: bool = False


@dataclass(frozen=True)
class IngressObserveResultV2:
    result: DifferResultV2
    wake_events: tuple[WakeEventV2, ...]


def wake_event_from_differ_event_v2(
    user_id: str,
    event: DifferEventV2,
    *,
    ts: float,
    origin_refs: Iterable[str] = (),
) -> WakeEventV2:
    return WakeEventV2(
        user_id=user_id,
        source=event.source,
        trigger=event.trigger,
        created_at=float(ts),
        change_digest=event.change_digest,
        presence_hints=dict(event.presence_hints or {}),
        origin_refs=tuple(str(ref) for ref in origin_refs if str(ref)),
        payload=dict(event.payload or {}),
    )


def observe_signal_v2(
    user_id: str,
    signal: str,
    value: Any,
    *,
    ts: float | None = None,
    origin_refs: Iterable[str] = (),
    source_event_id: str | None = None,
    allow_first_event: bool = False,
    differ: PerceptionDifferV2 | None = None,
    submit_wake: Callable[[WakeEventV2], None] | None = None,
) -> IngressObserveResultV2:
    ts = time.time() if ts is None else float(ts)
    active_differ = differ or DEFAULT_DIFFER_V2
    result = active_differ.observe(
        user_id,
        signal,
        value,
        ts=ts,
        source_event_id=source_event_id,
        allow_first_event=allow_first_event,
    )
    wake_events = tuple(
        wake_event_from_differ_event_v2(
            user_id,
            event,
            ts=ts,
            origin_refs=origin_refs,
        )
        for event in result.events
    )
    if submit_wake is not None:
        for event in wake_events:
            submit_wake(event)
    return IngressObserveResultV2(result=result, wake_events=wake_events)


def operation_observations_v2(key: str, data: Any) -> tuple[IngressObservationV2, ...]:
    if data is None:
        return ()
    if key == "time":
        return (IngressObservationV2("time", data, ("ios_report:time",)),)
    if key == "battery":
        return (IngressObservationV2("battery", data, ("ios_report:battery",)),)
    if key == "broadcast":
        value = data.get("state") if isinstance(data, Mapping) else data
        return (IngressObservationV2("broadcast_state", value, ("ios_report:broadcast",)),)
    return ()


def device_event_observations_v2(event: Mapping[str, Any]) -> tuple[IngressObservationV2, ...]:
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    event_id = str(event.get("event_id") or "")
    event_type = str(event.get("type") or "").strip().lower()
    origin = (f"device_event:{event_id}",) if event_id else ("device_event",)
    out: list[IngressObservationV2] = []

    wake_trigger = str(payload.get("wake_trigger") or "").strip().lower()
    if event_type == "unlock_after_absence" or wake_trigger == "unlock_after_absence":
        out.append(IngressObservationV2(
            "unlock_after_absence",
            True,
            origin,
            source_event_id=event_id or None,
            allow_first_event=True,
        ))

    phash = payload.get("safe_screen_phash") or payload.get("screen_phash")
    broadcast_state = str(payload.get("broadcast_state") or "").strip().lower()
    if phash and broadcast_state in {"on", "broadcasting"}:
        out.append(IngressObservationV2(
            "screen_phash",
            str(phash),
            origin,
            source_event_id=event_id or None,
            allow_first_event=True,
        ))
    return tuple(out)
