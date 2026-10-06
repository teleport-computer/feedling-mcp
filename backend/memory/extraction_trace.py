"""Closed refusal observations shared by hosted Capture and Dream.

Separate from Dream lifecycle outcomes: a refused provider attempt can retry
and recover, and must not relabel the job's existing failure/health semantics.
"""
from __future__ import annotations

import asyncio
import logging

import provider_refusal

log = logging.getLogger(__name__)
REFUSAL_TRACE_TYPE = "memory.extraction.refusal"
LANES = frozenset({"capture", "dream"})


def refusal_detail(value: object, lane: str) -> dict | None:
    shape = provider_refusal.project(value)
    if shape is None or not isinstance(lane, str) or lane not in LANES:
        return None
    attempt = value.get("attempt")
    if type(attempt) is not int or attempt < 1:
        return None
    return {
        **shape, "runtime": "hosted_v2", "lane": lane,
        "attempt": attempt,
    }


def valid_refusal_detail(value: object) -> bool:
    return (
        isinstance(value, dict)
        and refusal_detail(value, value.get("lane")) == value
    )


def refusal_observer(emit, user_id: str, *, lane: str, job_id, trace_id: str):
    """Connect only hosted extraction, independently of encrypted trajectory."""
    async def observe(value: dict) -> None:
        detail = refusal_detail(value, lane)
        if detail is None or emit is None:
            return
        try:
            await asyncio.to_thread(
                emit, user_id, REFUSAL_TRACE_TYPE,
                status="warning", summary="", explain="",
                trace_id=str(trace_id or job_id),
                turn_id=str(trace_id or job_id), job_id=str(job_id),
                detail=detail,
            )
        except Exception:
            log.warning("extraction refusal trace failed")
    return observe
