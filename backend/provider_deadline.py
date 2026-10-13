"""Opt-in absolute-deadline provenance; no provider or business dependencies."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import time
from typing import Any, Callable


# Shared only across this attempt's wait_for child task. The provider owns the
# private trace representation; this module only preserves its reference.
attempt_trace: ContextVar[list | None] = ContextVar("deadline_attempt_trace", default=None)


class ProviderAbsoluteDeadlineExceeded(TimeoutError):
    """Only the boundary owning a deadline may create this outcome."""

    def __init__(self, deadline: float, phase: str, *, result: Any = None):
        super().__init__("provider absolute deadline exceeded")
        self.deadline_at = deadline
        self.observed_at = time.monotonic()
        self.deadline_phase = phase
        self.late_result = result
        self.deadline_attempt_trace: list = []


@dataclass(frozen=True)
class _Outcome:
    value: Any
    error: Exception | None
    at: float


async def run_attempt(factory: Callable[[], Any], deadline: float) -> Any:
    """Separate a wire's TimeoutError from cancellation by our own timer.

    Ordinary exceptions cross wait_for as values. External cancellation remains
    BaseException and is never translated. A transport that suppresses cancel
    still cannot make a late result look on-time.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderAbsoluteDeadlineExceeded(deadline, "entry")

    trace: list = []

    def expired(phase: str, result: Any = None) -> ProviderAbsoluteDeadlineExceeded:
        error = ProviderAbsoluteDeadlineExceeded(deadline, phase, result=result)
        error.deadline_attempt_trace = trace
        return error

    async def capture_inner() -> _Outcome:
        try:
            value = await factory()
        except Exception as exc:
            return _Outcome(None, exc, time.monotonic())
        return _Outcome(value, None, time.monotonic())

    async def capture() -> _Outcome:
        token = attempt_trace.set(trace)
        try:
            return await capture_inner()
        finally:
            attempt_trace.reset(token)

    try:
        outcome = await asyncio.wait_for(capture(), timeout=remaining)
    except asyncio.TimeoutError as exc:
        raise expired("attempt") from exc
    if outcome.at >= deadline:
        raise expired(
            "attempt" if outcome.error else "late_success", outcome.value,
        ) from outcome.error
    if outcome.error is not None:
        raise outcome.error
    return outcome.value
