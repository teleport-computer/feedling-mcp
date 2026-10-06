"""Runtime V2's unconditional three-pool, one-process-per-slot topology."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Literal


from model_api_runtime.v2 import extraction


PoolName = Literal["foreground", "wake", "heavy"]

_FOREGROUND_LANES = frozenset({"chat", "manual_wake"})
_WAKE_POOL_LANES = frozenset({"heartbeat", "scheduled", "screen_watch"})
_HEAVY_LANES = frozenset(
    {"dream", "capture", "maintenance", "trajectory_review"}
)


@dataclass(frozen=True)
class SlotSpec:
    pool: PoolName
    index: int
    lanes: frozenset[str]
    stall_budget_sec: float
    absolute_budget_sec: float

    @property
    def slot_id(self) -> str:
        return f"{self.pool}-{self.index}"


@dataclass(frozen=True)
class RuntimePoolConfig:
    slots: tuple[SlotSpec, ...]
    profile_instance_concurrency: int
    enclave_instance_concurrency: int

    @classmethod
    def from_env(cls) -> "RuntimePoolConfig":
        foreground_slots = _positive_slots(
            "FEEDLING_V2_FOREGROUND_SLOTS", "4", "foreground"
        )
        wake_slots = _positive_slots("FEEDLING_V2_WAKE_SLOTS", "2", "wake")
        heavy_slots = _positive_slots("FEEDLING_V2_HEAVY_SLOTS", "2", "heavy")
        profile_concurrency = _integer_env(
            "FEEDLING_V2_PROFILE_INSTANCE_CONCURRENCY", "1"
        )
        if profile_concurrency != 1:
            raise ValueError("profile instance concurrency must equal 1")
        enclave_concurrency = _integer_env(
            "FEEDLING_V2_ENCLAVE_INSTANCE_CONCURRENCY", "4"
        )
        if enclave_concurrency < 4:
            raise ValueError("enclave instance concurrency must be at least 4")

        slots: list[SlotSpec] = []
        slots.extend(
            SlotSpec("foreground", index, _FOREGROUND_LANES, 240.0, 1500.0)
            for index in range(foreground_slots)
        )
        slots.extend(
            SlotSpec("wake", index, _WAKE_POOL_LANES, 240.0, 900.0)
            for index in range(wake_slots)
        )
        # Every HTTP wire (including compatibility fallback) reports progress;
        # one longest wire plus 30s bounds the silence, not two wires. Round the
        # existing provider envelope up to a minute for the absolute allowance.
        heavy_stall = extraction.max_wire_deadline_sec() + 30.0
        heavy_absolute = 60.0 * math.ceil(extraction.nominal_provider_envelope_sec() / 60.0)
        for index in range(heavy_slots):
            lanes = _HEAVY_LANES | ({"profile"} if index == 0 else set())
            slots.append(SlotSpec("heavy", index, frozenset(lanes), heavy_stall, heavy_absolute))

        return cls(
            slots=tuple(slots),
            profile_instance_concurrency=profile_concurrency,
            enclave_instance_concurrency=enclave_concurrency,
        )


def _integer_env(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _positive_slots(name: str, default: str, pool: str) -> int:
    value = _integer_env(name, default)
    if value <= 0:
        raise ValueError(f"{pool} slots must be positive")
    return value
