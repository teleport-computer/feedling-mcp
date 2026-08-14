"""Strict, bounded parent/slot progress protocol for Runtime V2."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


_MAX_ID = 200
_MAX_STAGE = 120
_LANES = frozenset(
    {
        "chat",
        "manual_wake",
        "heartbeat",
        "scheduled",
        "screen_watch",
        "profile",
        "dream",
        "capture",
        "maintenance",
        "trajectory_review",
    }
)


def _bounded_text(value: Any, *, field: str, maximum: int = _MAX_ID) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"invalid {field}")
    return value


def _finite_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {field}")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"invalid {field}")
    return result


@dataclass(frozen=True)
class ActiveJobIdentity:
    job_id: int
    lane: str
    claimed_by: str

    def __post_init__(self) -> None:
        if type(self.job_id) is not int or self.job_id <= 0:
            raise ValueError("invalid job_id")
        if self.lane not in _LANES:
            raise ValueError("invalid lane")
        _bounded_text(self.claimed_by, field="claimed_by")


@dataclass(frozen=True)
class SlotProgress:
    slot_id: str
    slot_generation: str
    monotonic_at: float
    turn_start: float | None
    stage: str
    active_job: ActiveJobIdentity | None

    def __post_init__(self) -> None:
        _bounded_text(self.slot_id, field="slot_id")
        _bounded_text(self.slot_generation, field="slot_generation")
        _finite_number(self.monotonic_at, field="monotonic_at")
        if self.turn_start is not None:
            _finite_number(self.turn_start, field="turn_start")
        _bounded_text(self.stage, field="stage", maximum=_MAX_STAGE)
        if (self.turn_start is None) != (self.active_job is None):
            raise ValueError("turn_start and active_job must be set or cleared together")


@dataclass(frozen=True)
class LoopHeartbeat:
    slot_generation: str
    monotonic_at: float

    def __post_init__(self) -> None:
        _bounded_text(self.slot_generation, field="slot_generation")
        _finite_number(self.monotonic_at, field="monotonic_at")


SlotMessage = SlotProgress | LoopHeartbeat


def encode_message(message: SlotMessage) -> dict[str, object]:
    if isinstance(message, LoopHeartbeat):
        return {"v": 1, "t": "h", "g": message.slot_generation, "m": message.monotonic_at}
    if not isinstance(message, SlotProgress):
        raise TypeError("unsupported slot message")
    active = message.active_job
    return {
        "v": 1,
        "t": "p",
        "s": message.slot_id,
        "g": message.slot_generation,
        "m": message.monotonic_at,
        "a": message.turn_start,
        "x": message.stage,
        "j": (
            None
            if active is None
            else {"i": active.job_id, "l": active.lane, "b": active.claimed_by}
        ),
    }


def decode_message(payload: Mapping[str, Any]) -> SlotMessage:
    if not isinstance(payload, Mapping):
        raise ValueError("slot message must be an object")
    kind = payload.get("t")
    if kind == "h":
        if set(payload) != {"v", "t", "g", "m"} or payload.get("v") != 1:
            raise ValueError("invalid loop heartbeat schema")
        return LoopHeartbeat(
            slot_generation=payload["g"], monotonic_at=payload["m"]
        )
    if kind != "p" or set(payload) != {"v", "t", "s", "g", "m", "a", "x", "j"}:
        raise ValueError("invalid slot progress schema")
    if payload.get("v") != 1:
        raise ValueError("unsupported slot protocol version")
    raw_job = payload["j"]
    active_job = None
    if raw_job is not None:
        if not isinstance(raw_job, Mapping) or set(raw_job) != {"i", "l", "b"}:
            raise ValueError("invalid active job schema")
        active_job = ActiveJobIdentity(
            job_id=raw_job["i"], lane=raw_job["l"], claimed_by=raw_job["b"]
        )
    return SlotProgress(
        slot_id=payload["s"],
        slot_generation=payload["g"],
        monotonic_at=payload["m"],
        turn_start=payload["a"],
        stage=payload["x"],
        active_job=active_job,
    )
