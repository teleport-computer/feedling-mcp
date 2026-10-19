"""Task-local Dream budget anchored to the existing claimed-turn clock."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import math
import time

RESERVE_SEC = 120.0
EXHAUSTED = "dream_turn_budget_exhausted"
LEASE_LOST = "lease_lost"


@dataclass(frozen=True)
class TurnBudget:
    start: float
    hard_deadline: float
    provider_deadline: float

    def exhausted(self) -> bool:
        return time.monotonic() >= self.provider_deadline


def validate_slot_budget(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= RESERVE_SEC:
        raise ValueError("Dream slot budget must be finite and exceed reserve")
    return value


def from_start(start: float, budget: float) -> TurnBudget:
    hard = start + validate_slot_budget(budget)
    return TurnBudget(start, hard, hard - RESERVE_SEC)


current: ContextVar[TurnBudget | None] = ContextVar("dream_turn_budget", default=None)


def exhausted() -> bool:
    budget = current.get()
    return budget is not None and budget.exhausted()
