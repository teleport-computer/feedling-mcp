"""Content-free exception observations, separate from terminal outcome semantics.

No message, repr, traceback, URL, class name, or provider body is serialized.
Trace delivery is best effort and gated by existing trace policy; silence is
not proof of success. An observation can precede a retry or a lost-lease CAS.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection

import httpx
from psycopg_pool import PoolTimeout as DatabasePoolTimeout

import provider_client
from notices import catalog

log = logging.getLogger(__name__)
_STAGES = frozenset({"wake", "chat", "setup", "compaction", "trajectory_review", "extraction_provider"})
_LANES = frozenset(
    {
        "chat",
        "heartbeat",
        "scheduled",
        "manual_wake",
        "screen_watch",
        "profile",
        "capture",
        "dream",
        "maintenance",
        "trajectory_review",
    }
)


def _family(exc: BaseException | None) -> str:
    # Concrete types, never exception class-name strings supplied by a caller.
    for cls, name in (
        (DatabasePoolTimeout, "database_pool_timeout"),
        (provider_client.ProviderError, "provider_error"),
        (httpx.PoolTimeout, "http_pool_timeout"),
        (httpx.TimeoutException, "http_timeout"),
        (httpx.TransportError, "http_transport_error"),
        (TimeoutError, "timeout"),
        (ConnectionError, "connection_error"),
        (RuntimeError, "runtime_error"),
        (ValueError, "value_error"),
        (KeyError, "key_error"),
    ):
        if isinstance(exc, cls):
            return name
    return "none" if exc is None else "unknown"


def _status(exc: BaseException | None) -> int | None:
    # An arbitrary exception's status_code attribute is not HTTP provenance.
    value = exc.status_code if isinstance(exc, provider_client.ProviderError) else None
    return value if type(value) is int and 100 <= value <= 599 else None


def build_detail(
    exc: BaseException,
    *,
    stage: str,
    lane: str,
    code: str,
    error_class: str,
    public_codes: Collection[str],
) -> dict:
    cause = exc.__cause__
    # Only explicit "raise ... from" establishes a causal link. Implicit
    # __context__ can describe a different error during exception handling.
    return {
        "driver": "v2",
        "stage": stage if stage in _STAGES else "unknown",
        "lane": lane if lane in _LANES else "other",
        "observation_only": True,
        "error_code": code if code in public_codes else "unknown",
        "error_class": (
            error_class if error_class in catalog.ERROR_CLASSES else "unknown"
        ),
        "exception_family": _family(exc),
        "http_status": _status(exc),
        "explicit_cause_present": cause is not None,
        "cause_family": _family(cause),
        "cause_http_status": _status(cause),
    }


async def observe(
    emit: Callable | None,
    *,
    user_id: str,
    job_id,
    trace_id: str,
    exc: BaseException,
    stage: str,
    lane: str,
    code: str,
    classify: Callable[[], str],
    public_codes: Collection[str],
) -> None:
    if emit is None:
        return
    try:
        detail = build_detail(
            exc,
            stage=stage,
            lane=lane,
            code=code,
            error_class=classify(),
            public_codes=public_codes,
        )
        await asyncio.to_thread(
            emit,
            user_id,
            "v2.failure.observed",
            status="error",
            job_id=str(job_id),
            trace_id=trace_id,
            summary="V2 exception observation",
            detail=detail,
        )
    except Exception:
        # Never print emitter exceptions: even diagnostic failures may contain
        # secrets. Existing queue transition/retry must continue unchanged.
        log.warning("V2 failure observation unavailable")
