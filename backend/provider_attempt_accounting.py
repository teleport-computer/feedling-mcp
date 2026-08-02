"""Content-free provider-attempt facts and their fail-open RDS recorder.

The only request-path operation is a bounded ``Queue.put_nowait``.  All pool
work, serialization, SQL, retries, and stale-start reconciliation run on a
daemon worker and are deliberately allowed to lose telemetry rather than alter
provider-call behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
import math
import queue
import re
import threading
import time
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

import db


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


class AttemptUsageUnknownReason(str, Enum):
    """Content-free lifecycle codes for usage that cannot be established."""

    PROVIDER_OMITTED = "provider_omitted"
    REQUEST_FAILED = "request_failed"
    TIMEOUT = "timeout"
    PARSE_ERROR = "parse_error"
    RECORDER_GAP = "recorder_gap"
    STARTED_ONLY = "started_only"


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


log = logging.getLogger("feedling.provider_attempt_accounting")

_EVENT_COLUMNS = (
    "attempt_id", "user_id", "call_id", "outer_attempt_ordinal",
    "inner_attempt_ordinal", "installation_id", "source", "lane", "state",
    "outcome", "completeness", "requested_provider", "requested_model",
    "resolved_provider", "resolved_model", "transport", "error_class",
    "runtime", "job_id", "turn_id", "round_id", "retry_kind",
    "provider_request_id", "usage_unknown_reason",
    "started_at", "finished_at", "input_tokens", "output_tokens",
    "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
    "cache_miss_tokens", "usage_known", "possibly_billed", "latency_ms",
    "ttft_ms",
)
_UPSERT_SQL = """
INSERT INTO llm_provider_attempts (
  attempt_id, user_id, call_id, outer_attempt_ordinal, inner_attempt_ordinal,
  installation_id, source, lane, state, outcome, completeness,
  requested_provider, requested_model, resolved_provider, resolved_model,
  transport, error_class, runtime, job_id, turn_id, round_id, retry_kind,
  provider_request_id, usage_unknown_reason, started_at, finished_at,
  input_tokens, output_tokens, reasoning_tokens, cache_read_tokens,
  cache_write_tokens, cache_miss_tokens, usage_known, possibly_billed,
  latency_ms, ttft_ms
) VALUES (
  %(attempt_id)s, %(user_id)s, %(call_id)s, %(outer_attempt_ordinal)s,
  %(inner_attempt_ordinal)s, %(installation_id)s, %(source)s, %(lane)s,
  %(state)s, %(outcome)s, %(completeness)s, %(requested_provider)s,
  %(requested_model)s, %(resolved_provider)s, %(resolved_model)s,
  %(transport)s, %(error_class)s, %(runtime)s, %(job_id)s, %(turn_id)s,
  %(round_id)s, %(retry_kind)s, %(provider_request_id)s,
  %(usage_unknown_reason)s, %(started_at)s, %(finished_at)s,
  %(input_tokens)s, %(output_tokens)s, %(reasoning_tokens)s,
  %(cache_read_tokens)s, %(cache_write_tokens)s, %(cache_miss_tokens)s,
  %(usage_known)s, %(possibly_billed)s, %(latency_ms)s, %(ttft_ms)s
)
ON CONFLICT (attempt_id) DO UPDATE SET
  installation_id = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.installation_id
    ELSE EXCLUDED.installation_id END,
  source = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.source ELSE EXCLUDED.source END,
  lane = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.lane ELSE EXCLUDED.lane END,
  state = CASE WHEN llm_provider_attempts.state = 'completed' THEN 'completed'
    ELSE EXCLUDED.state END,
  outcome = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.outcome ELSE EXCLUDED.outcome END,
  completeness = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.completeness ELSE EXCLUDED.completeness END,
  requested_provider = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.requested_provider ELSE EXCLUDED.requested_provider END,
  requested_model = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.requested_model ELSE EXCLUDED.requested_model END,
  resolved_provider = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.resolved_provider ELSE EXCLUDED.resolved_provider END,
  resolved_model = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.resolved_model ELSE EXCLUDED.resolved_model END,
  transport = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.transport ELSE EXCLUDED.transport END,
  error_class = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.error_class ELSE EXCLUDED.error_class END,
  runtime = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.runtime ELSE EXCLUDED.runtime END,
  job_id = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.job_id ELSE EXCLUDED.job_id END,
  turn_id = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.turn_id ELSE EXCLUDED.turn_id END,
  round_id = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.round_id ELSE EXCLUDED.round_id END,
  retry_kind = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.retry_kind ELSE EXCLUDED.retry_kind END,
  provider_request_id = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.provider_request_id ELSE EXCLUDED.provider_request_id END,
  usage_unknown_reason = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.usage_unknown_reason ELSE EXCLUDED.usage_unknown_reason END,
  started_at = LEAST(llm_provider_attempts.started_at, EXCLUDED.started_at),
  input_tokens = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.input_tokens ELSE EXCLUDED.input_tokens END,
  output_tokens = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.output_tokens ELSE EXCLUDED.output_tokens END,
  reasoning_tokens = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.reasoning_tokens ELSE EXCLUDED.reasoning_tokens END,
  cache_read_tokens = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.cache_read_tokens ELSE EXCLUDED.cache_read_tokens END,
  cache_write_tokens = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.cache_write_tokens ELSE EXCLUDED.cache_write_tokens END,
  cache_miss_tokens = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.cache_miss_tokens ELSE EXCLUDED.cache_miss_tokens END,
  usage_known = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.usage_known ELSE EXCLUDED.usage_known END,
  latency_ms = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.latency_ms ELSE EXCLUDED.latency_ms END,
  ttft_ms = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.ttft_ms ELSE EXCLUDED.ttft_ms END,
  finished_at = CASE WHEN llm_provider_attempts.state = 'completed'
    THEN llm_provider_attempts.finished_at WHEN EXCLUDED.state = 'completed'
    THEN EXCLUDED.finished_at ELSE NULL END,
  possibly_billed = CASE WHEN llm_provider_attempts.state = 'completed'
    AND EXCLUDED.state = 'started' THEN llm_provider_attempts.possibly_billed
    ELSE EXCLUDED.possibly_billed END,
  updated_at = now()
"""
_RECONCILE_STALE_SQL = """
UPDATE llm_provider_attempts
SET possibly_billed = TRUE, updated_at = now()
WHERE state = 'started'
  AND finished_at IS NULL
  AND possibly_billed = FALSE
  AND started_at < now() - (%s * interval '1 second')
"""


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
class ProviderAttemptContext:
    """Immutable Hosted V2 identity propagated with one logical provider call."""

    user_id: str
    lane: AttemptLane
    job_id: int
    call_id: str
    installation_id: str | None = None
    runtime: str = "hosted_v2"
    turn_id: str | None = None
    round_id: str | None = None

    def __post_init__(self) -> None:
        _safe_identifier("user_id", self.user_id, _INSTALLATION_ID)
        if not isinstance(self.lane, AttemptLane):
            raise TypeError("provider_attempt_lane_must_be_typed")
        if (
            not isinstance(self.job_id, int)
            or isinstance(self.job_id, bool)
            or self.job_id < 0
        ):
            raise ValueError("job_id_must_be_nonnegative")
        _safe_identifier("call_id", self.call_id, _CALL_ID)
        _safe_identifier(
            "installation_id", self.installation_id, _INSTALLATION_ID, optional=True,
        )
        _safe_identifier("runtime", self.runtime, _RUNTIME, optional=True)
        _safe_identifier("turn_id", self.turn_id, _CALL_ID, optional=True)
        _safe_identifier("round_id", self.round_id, _CALL_ID, optional=True)


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
    usage_unknown_reason: AttemptUsageUnknownReason | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_miss_tokens: int | None = None
    usage_known: bool = False
    possibly_billed: bool = False
    latency_ms: float | None = None
    ttft_ms: float | None = None

    def __post_init__(self) -> None:
        """Keep direct dataclass construction inside the content-free contract."""
        self.validate()

    def validate(self) -> None:
        """Revalidate before persistence in case a frozen object was forged."""
        _safe_identifier("user_id", self.user_id, _INSTALLATION_ID)
        for value, enum_type in (
            (self.source, AttemptSource),
            (self.lane, AttemptLane),
            (self.state, AttemptState),
            (self.outcome, AttemptOutcome),
            (self.completeness, AttemptCompleteness),
            (self.error_class, AttemptErrorClass),
            (self.retry_kind, AttemptRetryKind),
        ):
            if not isinstance(value, enum_type):
                raise TypeError("provider_attempt_enums_required")
        if self.usage_unknown_reason is not None and not isinstance(
            self.usage_unknown_reason, AttemptUsageUnknownReason,
        ):
            raise TypeError("provider_attempt_usage_unknown_reason_must_be_typed")
        _safe_identifier("requested_provider", self.requested_provider, _PROVIDER)
        _safe_identifier("requested_model", self.requested_model, _MODEL)
        _safe_identifier("resolved_provider", self.resolved_provider, _PROVIDER)
        _safe_identifier("resolved_model", self.resolved_model, _MODEL)
        _safe_identifier("transport", self.transport, _TRANSPORT)
        _safe_identifier(
            "installation_id", self.installation_id, _INSTALLATION_ID, optional=True,
        )
        _safe_identifier("runtime", self.runtime, _RUNTIME, optional=True)
        _safe_identifier("turn_id", self.turn_id, _CALL_ID, optional=True)
        _safe_identifier("round_id", self.round_id, _CALL_ID, optional=True)
        _safe_identifier(
            "provider_request_id", self.provider_request_id, _PROVIDER_REQUEST_ID,
            optional=True,
        )
        if self.job_id is not None and (
            not isinstance(self.job_id, int)
            or isinstance(self.job_id, bool)
            or self.job_id < 0
        ):
            raise ValueError("job_id_must_be_nonnegative")
        for name in (
            "input_tokens", "output_tokens", "reasoning_tokens",
            "cache_read_tokens", "cache_write_tokens", "cache_miss_tokens",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name}_must_be_nonnegative_integer")
        for name in ("usage_known", "possibly_billed"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name}_must_be_boolean")
        for name in ("latency_ms", "ttft_ms"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name}_must_be_nonnegative_finite")
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, datetime) or value.utcoffset() is None
            ):
                raise TypeError(f"{name}_must_be_timezone_aware_datetime")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at_before_started_at")
        if self.attempt_id != stable_attempt_id(
            self.call_id, self.outer_attempt_ordinal, self.inner_attempt_ordinal,
        ):
            raise ValueError("attempt_id_must_match_stable_identity")

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
        usage_unknown_reason: AttemptUsageUnknownReason | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        cache_miss_tokens: int | None = None,
        usage_known: bool = False,
        possibly_billed: bool = False,
        latency_ms: float | None = None,
        ttft_ms: float | None = None,
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
        if usage_unknown_reason is not None and not isinstance(
            usage_unknown_reason, AttemptUsageUnknownReason,
        ):
            raise TypeError("provider_attempt_usage_unknown_reason_must_be_typed")
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
        actual_started_at = started_at or datetime.now(timezone.utc)
        actual_finished_at = finished_at
        if state is AttemptState.COMPLETED and actual_finished_at is None:
            actual_finished_at = datetime.now(timezone.utc)
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
            usage_unknown_reason=usage_unknown_reason,
            started_at=actual_started_at,
            finished_at=actual_finished_at,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_miss_tokens=cache_miss_tokens,
            usage_known=usage_known,
            possibly_billed=possibly_billed,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
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
            "usage_unknown_reason": (
                None if self.usage_unknown_reason is None
                else self.usage_unknown_reason.value
            ),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "usage_known": self.usage_known,
            "possibly_billed": self.possibly_billed,
            "latency_ms": self.latency_ms,
            "ttft_ms": self.ttft_ms,
        }


class ProviderAttemptRecorder:
    """A bounded, process-local, deliberately lossy event recorder.

    ``record`` is safe to call from provider code: it never waits for a pool,
    a database, a worker, or a queue slot, and it always returns ``None``.
    """

    def __init__(
        self,
        *,
        queue_capacity: int = 1024,
        batch_size: int = 64,
        flush_interval: float = 0.05,
        max_retries: int = 2,
        retry_backoff: float = 0.05,
        stale_after_seconds: float = 900,
        reconcile_interval: float = 60,
        pool_factory: Callable[[], Any] | None = None,
        thread_factory: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if queue_capacity < 1 or batch_size < 1:
            raise ValueError("provider_attempt_recorder_capacity_must_be_positive")
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_capacity)
        self._batch_size = batch_size
        self._flush_interval = max(0.001, float(flush_interval))
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff = max(0.0, float(retry_backoff))
        self._stale_after_seconds = max(0.0, float(stale_after_seconds))
        self._reconcile_interval = max(0.0, float(reconcile_interval))
        self._pool_factory = pool_factory or db.get_pool
        self._thread_factory = thread_factory or threading.Thread
        self._sleeper = sleeper or time.sleep
        self._stop = threading.Event()
        self._lifecycle = threading.Condition()
        self._start_phase = "idle"
        self._thread: Any | None = None
        self._dropped_count = 0
        self._last_diagnostic = 0.0
        self._last_reconcile = time.monotonic()

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def queue_size(self) -> int:
        try:
            return self._queue.qsize()
        except Exception:  # noqa: BLE001 - diagnostics cannot affect callers
            return 0

    def record(self, event: ProviderAttemptEvent) -> None:
        """Enqueue a fact with one non-blocking operation; never raise."""
        try:
            if self._stop.is_set():
                self._drop("shutdown")
                return None
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._drop("queue_full")
            except Exception:  # noqa: BLE001 - custom queue/runtime failures are telemetry-only
                self._drop("queue_failure")
            return None
        except Exception:  # noqa: BLE001 - no telemetry path may alter a provider result
            return None

    def shutdown(self, timeout: float = 1.0) -> bool:
        """Request bounded drain/exit and never propagate shutdown failures."""
        self._stop.set()
        try:
            deadline = time.monotonic() + max(0.0, float(timeout))
            with self._lifecycle:
                while self._start_phase != "idle":
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._lifecycle.wait(remaining)
                thread = self._thread
            if thread is None:
                return True
            thread.join(max(0.0, deadline - time.monotonic()))
            return not thread.is_alive()
        except Exception:  # noqa: BLE001 - process shutdown must remain safe
            return False

    def start(self) -> bool:
        """Start the daemon from process bootstrap, never from a provider call."""
        with self._lifecycle:
            if self._stop.is_set():
                return False
            thread = self._thread
            try:
                if thread is not None and thread.is_alive():
                    return True
            except Exception:  # noqa: BLE001
                pass
            if self._start_phase != "idle":
                return False
            self._start_phase = "factory"
        try:
            worker = self._thread_factory(
                target=self._run,
                name="provider-attempt-recorder",
                daemon=True,
            )
        except Exception:  # noqa: BLE001 - daemon startup is non-essential telemetry
            with self._lifecycle:
                self._start_phase = "idle"
                self._lifecycle.notify_all()
            self._drop("worker_start_failure")
            return False
        with self._lifecycle:
            if self._stop.is_set():
                self._start_phase = "idle"
                self._lifecycle.notify_all()
                return False
            self._start_phase = "thread_start"
        try:
            worker.start()
        except Exception:  # noqa: BLE001 - daemon startup is non-essential telemetry
            with self._lifecycle:
                self._start_phase = "idle"
                self._lifecycle.notify_all()
            self._drop("worker_start_failure")
            return False
        with self._lifecycle:
            self._thread = worker
            stopped = self._stop.is_set()
            self._start_phase = "idle"
            self._lifecycle.notify_all()
            return not stopped

    def _ensure_worker(self) -> bool:
        """Compatibility shim for explicit off-hot-path bootstrap callers."""
        return self.start()

    def _run(self) -> None:
        try:
            while True:
                batch = self._next_batch()
                if batch:
                    self._write_batch(batch)
                self._reconcile_if_due()
                if self._stop.is_set() and not batch and self.queue_size == 0:
                    return
        except Exception:  # noqa: BLE001 - a recorder crash must not cross its thread boundary
            self._diagnostic("worker_crash")

    def _next_batch(self) -> list[Any]:
        try:
            first = self._queue.get(timeout=self._flush_interval)
        except queue.Empty:
            return []
        except Exception:  # noqa: BLE001
            self._drop("queue_read_failure")
            return []
        batch = [first]
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
            except Exception:  # noqa: BLE001
                self._diagnostic("queue_read_failure")
                break
        return batch

    def _write_batch(self, events: list[Any]) -> None:
        rows: list[dict[str, object]] = []
        for event in events:
            try:
                if type(event) is not ProviderAttemptEvent:
                    raise TypeError("provider_attempt_event_required")
                event.validate()
                row = event.as_row()
                if tuple(row) != _EVENT_COLUMNS:
                    raise ValueError("provider_attempt_row_shape")
                rows.append(row)
            except Exception:  # noqa: BLE001 - malformed telemetry is dropped off-path
                self._drop("serialization_failure")
        if not rows:
            return
        for attempt in range(self._max_retries + 1):
            try:
                pool = self._pool_factory()
                with pool.connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.executemany(_UPSERT_SQL, rows)
                return
            except Exception:  # noqa: BLE001 - RDS failures are intentionally fail-open
                if attempt == self._max_retries:
                    self._drop("database_failure", len(rows))
                    return
                self._sleeper(self._retry_backoff * (2 ** attempt))

    def _reconcile_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_reconcile < self._reconcile_interval:
            return
        self._last_reconcile = now
        try:
            pool = self._pool_factory()
            with pool.connection() as connection:
                connection.execute(_RECONCILE_STALE_SQL, (self._stale_after_seconds,))
        except Exception:  # noqa: BLE001 - background cleanup never impacts event recording
            self._diagnostic("reconcile_failure")

    def _drop(self, reason: str, count: int = 1) -> None:
        try:
            self._dropped_count += count
        except Exception:  # noqa: BLE001 - counters are only best-effort diagnostics
            pass
        try:
            self._diagnostic(reason)
        except Exception:  # noqa: BLE001 - diagnostics must not escape any caller
            pass

    def _diagnostic(self, reason: str) -> None:
        try:
            now = time.monotonic()
            if now - self._last_diagnostic < 60:
                return
            self._last_diagnostic = now
            log.warning("provider attempt recorder event dropped or deferred: %s", reason)
        except Exception:  # noqa: BLE001 - logging failures cannot break telemetry
            return


_recorder: ProviderAttemptRecorder | None = None
_recorder_lock = threading.Lock()


def record_provider_attempt(event: ProviderAttemptEvent) -> None:
    """Record one fact through the process singleton without changing callers."""
    global _recorder
    recorder = _recorder
    if recorder is None:
        if not _recorder_lock.acquire(blocking=False):
            return None
        try:
            if _recorder is None:
                _recorder = ProviderAttemptRecorder()
            recorder = _recorder
        except Exception:  # noqa: BLE001 - singleton construction is optional telemetry
            return None
        finally:
            _recorder_lock.release()
    try:
        recorder.record(event)
    except Exception:  # noqa: BLE001 - preserve provider call behaviour
        return None
    return None


def start_provider_attempt_recorder() -> bool:
    """Explicit off-hot-path bootstrap for the process singleton's daemon."""
    global _recorder
    with _recorder_lock:
        if _recorder is None:
            _recorder = ProviderAttemptRecorder()
        recorder = _recorder
    return recorder.start()


def shutdown_provider_attempt_recorder(timeout: float = 1.0) -> bool:
    """Stop the singleton during controlled process shutdown."""
    recorder = _recorder
    return True if recorder is None else recorder.shutdown(timeout)
