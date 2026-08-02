"""Content-free domain values for canonical provider-attempt accounting.

This module intentionally contains no recorder, queue, or database I/O.  It
defines the stable identity and the small allowlisted payload that later hot
path instrumentation may hand to a fail-open recorder.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import NAMESPACE_URL, uuid5


class AttemptSource(str, Enum):
    RUNTIME_RECORDER = "runtime_recorder"
    LEGACY_BEST_EFFORT = "legacy_best_effort"


class AttemptLane(str, Enum):
    CHAT = "chat"
    HEARTBEAT = "heartbeat"
    SCHEDULED = "scheduled"
    MANUAL_WAKE = "manual_wake"
    SCREEN_WATCH = "screen_watch"
    MAINTENANCE = "maintenance"
    CAPTURE = "capture"
    DREAM = "dream"
    TRAJECTORY_REVIEW = "trajectory_review"
    UNKNOWN = "unknown"


class AttemptState(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"


class AttemptOutcome(str, Enum):
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class AttemptErrorClass(str, Enum):
    NONE = "none"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    PROVIDER = "provider"
    PROTOCOL = "protocol"
    VALIDATION = "validation"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class AttemptCompleteness(str, Enum):
    STARTED_ONLY = "started_only"
    COMPLETE = "complete"
    USAGE_UNKNOWN = "usage_unknown"
    LEGACY_BEST_EFFORT = "legacy_best_effort"


def stable_attempt_id(call_id: str, outer_ordinal: int, inner_ordinal: int) -> str:
    """Return the replay-stable ID for one actual provider dispatch."""
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("call_id_required")
    if not isinstance(outer_ordinal, int) or outer_ordinal < 0:
        raise ValueError("outer_ordinal_must_be_nonnegative")
    if not isinstance(inner_ordinal, int) or inner_ordinal < 0:
        raise ValueError("inner_ordinal_must_be_nonnegative")
    return str(uuid5(
        NAMESPACE_URL,
        f"feedling/provider-attempt/{call_id}/{outer_ordinal}/{inner_ordinal}",
    ))


@dataclass(frozen=True, slots=True)
class ProviderAttemptEvent:
    """Allowlisted metadata for a provider attempt; it cannot carry content."""

    attempt_id: str
    user_id: str
    call_id: str
    outer_attempt_ordinal: int
    inner_attempt_ordinal: int
    source: AttemptSource
    lane: AttemptLane
    state: AttemptState
    outcome: AttemptOutcome
    completeness: AttemptCompleteness
    requested_provider: str
    requested_model: str
    resolved_provider: str
    resolved_model: str
    transport: str
    error_class: AttemptErrorClass = AttemptErrorClass.NONE
    runtime: str | None = None
    job_id: int | None = None
    turn_id: str | None = None
    round_id: str | None = None
    retry_kind: str = "initial"
    provider_request_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        user_id: str,
        call_id: str,
        outer_attempt_ordinal: int,
        inner_attempt_ordinal: int,
        source: AttemptSource,
        lane: AttemptLane,
        state: AttemptState,
        outcome: AttemptOutcome,
        completeness: AttemptCompleteness,
        requested_provider: str,
        requested_model: str,
        resolved_provider: str,
        resolved_model: str,
        transport: str,
        error_class: AttemptErrorClass = AttemptErrorClass.NONE,
        runtime: str | None = None,
        job_id: int | None = None,
        turn_id: str | None = None,
        round_id: str | None = None,
        retry_kind: str = "initial",
        provider_request_id: str | None = None,
    ) -> "ProviderAttemptEvent":
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user_id_required")
        for value, enum_type in (
            (source, AttemptSource),
            (lane, AttemptLane),
            (state, AttemptState),
            (outcome, AttemptOutcome),
            (completeness, AttemptCompleteness),
            (error_class, AttemptErrorClass),
        ):
            if not isinstance(value, enum_type):
                raise TypeError("provider_attempt_enums_required")
        for value in (
            requested_provider, requested_model, resolved_provider,
            resolved_model, transport,
        ):
            if not isinstance(value, str) or not value:
                raise ValueError("provider_attempt_identity_required")
        return cls(
            attempt_id=stable_attempt_id(
                call_id, outer_attempt_ordinal, inner_attempt_ordinal,
            ),
            user_id=user_id,
            call_id=call_id,
            outer_attempt_ordinal=outer_attempt_ordinal,
            inner_attempt_ordinal=inner_attempt_ordinal,
            source=source,
            lane=lane,
            state=state,
            outcome=outcome,
            completeness=completeness,
            requested_provider=requested_provider,
            requested_model=requested_model,
            resolved_provider=resolved_provider,
            resolved_model=resolved_model,
            transport=transport,
            error_class=error_class,
            runtime=runtime,
            job_id=job_id,
            turn_id=turn_id,
            round_id=round_id,
            retry_kind=retry_kind,
            provider_request_id=provider_request_id,
        )

    def as_row(self) -> dict[str, object]:
        """Return the RDS-column-shaped, content-free row payload."""
        return {
            "attempt_id": self.attempt_id,
            "user_id": self.user_id,
            "call_id": self.call_id,
            "outer_attempt_ordinal": self.outer_attempt_ordinal,
            "inner_attempt_ordinal": self.inner_attempt_ordinal,
            "source": self.source.value,
            "lane": self.lane.value,
            "state": self.state.value,
            "outcome": self.outcome.value,
            "completeness": self.completeness.value,
            "requested_provider": self.requested_provider,
            "requested_model": self.requested_model,
            "resolved_provider": self.resolved_provider,
            "resolved_model": self.resolved_model,
            "transport": self.transport,
            "error_class": self.error_class.value,
            "runtime": self.runtime,
            "job_id": self.job_id,
            "turn_id": self.turn_id,
            "round_id": self.round_id,
            "retry_kind": self.retry_kind,
            "provider_request_id": self.provider_request_id,
        }
