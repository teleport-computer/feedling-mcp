"""Content-free lifecycle vocabulary for resident and hosted Dream runs.

Dream reads and rewrites private Memory Garden cards.  Its diagnostics may
describe control flow and integer result counts, but must never carry card ids,
prompt/reply text, summaries, buckets, threads, or raw exception messages.
Keeping the vocabulary here gives both runtimes and the admin projection one
closed contract instead of three copied allowlists.
"""
from __future__ import annotations

from typing import Mapping


DREAM_TRACE_TYPES = frozenset({
    "memory.dream.start",
    "memory.dream.model.start",
    "memory.dream.model.done",
    "memory.dream.model.error",
    "memory.dream.done",
    "memory.dream.error",
})
CONTEXT_TRACE_TYPE = "memory.extraction.context.error"

RUNTIMES = frozenset({"resident_v1", "hosted_v2", "unknown"})
LANES = frozenset({"dream"})
OUTCOMES = frozenset({
    "started",
    "accepted",
    "no_proposals",
    "noop",
    "applied",
    "partial",
    "context_unavailable",
    "provider_failed",
    "empty_reply",
    "output_truncated",
    "parse_rejected",
    "mapping_rejected",
    "guard_rejected",
    "write_failed",
    "write_rejected",
    "failed",
})
CONTEXT_COMPONENTS = frozenset({"cards", "memory_context"})
CONTEXT_OUTCOMES = frozenset({"unavailable", "truncated"})

COUNT_KEYS = frozenset({
    "active_cards",
    "model_attempts",
    "proposals",
    "actions",
    "applied",
    "skipped",
    "failed",
    "organized",
    "merged",
})


def _count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def detail(
    *,
    runtime: str,
    outcome: str,
    degraded_context: bool = False,
    counts: Mapping[str, object] | None = None,
) -> dict:
    """Build the exact closed detail shape for a Dream lifecycle event."""
    raw_counts = counts if isinstance(counts, Mapping) else {}
    return {
        "runtime": runtime if runtime in RUNTIMES else "unknown",
        "lane": "dream",
        "outcome": outcome if outcome in OUTCOMES else "failed",
        "degraded_context": bool(degraded_context),
        "counts": {key: _count(raw_counts.get(key)) for key in sorted(COUNT_KEYS)},
    }


def context_detail(*, runtime: str, component: str, outcome: str) -> dict:
    """Build the exact closed shape for a degraded Dream input observation."""
    return {
        "runtime": runtime if runtime in RUNTIMES else "unknown",
        "lane": "dream",
        "component": (
            component if component in CONTEXT_COMPONENTS else "memory_context"
        ),
        "outcome": outcome if outcome in CONTEXT_OUTCOMES else "unavailable",
    }


def reason_outcome(reason: object) -> str:
    """Collapse arbitrary parser/provider reasons into a content-free enum."""
    code = str(reason or "").strip()
    if code.startswith("provider_call_failed:"):
        return "provider_failed"
    if code == "empty_reply":
        return "empty_reply"
    if code == "output_truncated":
        return "output_truncated"
    return "parse_rejected"


def valid_detail(value: object) -> bool:
    """Validate the exact public lifecycle shape, including nested counts."""
    if not isinstance(value, dict) or set(value) != {
        "runtime", "lane", "outcome", "degraded_context", "counts",
    }:
        return False
    counts = value.get("counts")
    return (
        value.get("runtime") in RUNTIMES
        and value.get("lane") in LANES
        and value.get("outcome") in OUTCOMES
        and type(value.get("degraded_context")) is bool
        and isinstance(counts, dict)
        and set(counts) == COUNT_KEYS
        and all(type(item) is int and item >= 0 for item in counts.values())
    )


def valid_context_detail(value: object) -> bool:
    """Validate the exact public degraded-context shape."""
    return (
        isinstance(value, dict)
        and set(value) == {"runtime", "lane", "component", "outcome"}
        and value.get("runtime") in RUNTIMES
        and value.get("lane") in LANES
        and value.get("component") in CONTEXT_COMPONENTS
        and value.get("outcome") in CONTEXT_OUTCOMES
    )
