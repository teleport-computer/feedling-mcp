"""Content-free domain values for canonical provider-attempt accounting.

This module intentionally contains no recorder, queue, or database I/O.  It
defines the stable identity and the small allowlisted payload that later hot
path instrumentation may hand to a fail-open recorder.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
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


class AttemptRetryKind(str, Enum):
    INITIAL = "initial"
    OUTER_RETRY = "outer_retry"
    COMPATIBILITY_RETRY = "compatibility_retry"
    FAILOVER = "failover"


_CALL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
_PROVIDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}\Z")
_TRANSPORT = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_RUNTIME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_INSTALLATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PROVIDER_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
_SENSITIVE_PREFIXES = (
    "authorization:", "x-api-key:", "cookie:", "host:", "bearer",
    "basic", "sk-", "rk-", "pk-",
)
_SENSITIVE_MARKERS = ("api_key", "apikey", "token=", "secret=", "password=")


def _safe_identifier(
    name: str,
    value: str | None,
    pattern: re.Pattern[str],
    *,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid_{name}")
    folded = value.casefold()
    if (
        "://" in value
        or folded.startswith(_SENSITIVE_PREFIXES)
        or any(marker in folded for marker in _SENSITIVE_MARKERS)
    ):
        raise ValueError(f"unsafe_{name}")
    return value


def stable_attempt_id(call_id: str, outer_ordinal: int, inner_ordinal: int) -> str:
    """Return the replay-stable ID for one actual provider dispatch."""
    _safe_identifier("call_id", call_id, _CALL_ID)
    if (
        not isinstance(outer_ordinal, int)
        or isinstance(outer_ordinal, bool)
        or outer_ordinal < 0
    ):
        raise ValueError("outer_ordinal_must_be_nonnegative")
    if (
        not isinstance(inner_ordinal, int)
        or isinstance(inner_ordinal, bool)
        or inner_ordinal < 0
    ):
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
    installation_id: str | None
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
    retry_kind: AttemptRetryKind = AttemptRetryKind.INITIAL
    provider_request_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        user_id: str,
        call_id: str,
        outer_attempt_ordinal: int,
        inner_attempt_ordinal: int,
        installation_id: str | None = None,
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
        retry_kind: AttemptRetryKind = AttemptRetryKind.INITIAL,
        provider_request_id: str | None = None,
    ) -> "ProviderAttemptEvent":
        _safe_identifier("user_id", user_id, _INSTALLATION_ID)
        for value, enum_type in (
            (source, AttemptSource),
            (lane, AttemptLane),
            (state, AttemptState),
            (outcome, AttemptOutcome),
            (completeness, AttemptCompleteness),
            (error_class, AttemptErrorClass),
            (retry_kind, AttemptRetryKind),
        ):
            if not isinstance(value, enum_type):
                raise TypeError("provider_attempt_enums_required")
        _safe_identifier("requested_provider", requested_provider, _PROVIDER)
        _safe_identifier("requested_model", requested_model, _MODEL)
        _safe_identifier("resolved_provider", resolved_provider, _PROVIDER)
        _safe_identifier("resolved_model", resolved_model, _MODEL)
        _safe_identifier("transport", transport, _TRANSPORT)
        _safe_identifier("installation_id", installation_id, _INSTALLATION_ID, optional=True)
        _safe_identifier("runtime", runtime, _RUNTIME, optional=True)
        _safe_identifier("turn_id", turn_id, _CALL_ID, optional=True)
        _safe_identifier("round_id", round_id, _CALL_ID, optional=True)
        _safe_identifier(
            "provider_request_id", provider_request_id, _PROVIDER_REQUEST_ID, optional=True,
        )
        if job_id is not None and (
            not isinstance(job_id, int) or isinstance(job_id, bool) or job_id < 0
        ):
            raise ValueError("job_id_must_be_nonnegative")
        return cls(
            attempt_id=stable_attempt_id(
                call_id, outer_attempt_ordinal, inner_attempt_ordinal,
            ),
            user_id=user_id,
            call_id=call_id,
            outer_attempt_ordinal=outer_attempt_ordinal,
            inner_attempt_ordinal=inner_attempt_ordinal,
            installation_id=installation_id,
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
            "installation_id": self.installation_id,
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
            "retry_kind": self.retry_kind.value,
            "provider_request_id": self.provider_request_id,
        }
