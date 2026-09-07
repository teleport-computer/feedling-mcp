"""Per-loop recall accounting; content-free and side effects injected.

Count actual dispatches, not provider proposals, cached discovery reuse, or
background reads outside this loop. A failed read is not an empty search.
"""
from __future__ import annotations

from functools import wraps
import inspect
import logging


log = logging.getLogger(__name__)


def traced(loop):
    """One terminal callback across every return/exception/cancellation path."""
    @wraps(loop)
    async def run(*args, on_memory_recall_completed=None, **kwargs):
        if on_memory_recall_completed is None:
            return await loop(*args, **kwargs)
        counts = {"injected": 0, "selected": None, "index_calls": 0,
                  "search_calls": 0, "empty_searches": 0, "fetch_cards": 0}
        dispatch = kwargs["dispatch_tools"]

        async def counted_dispatch(calls):
            for call in calls:
                if call.name == "memory_index":
                    counts["index_calls"] += 1
                elif call.name == "memory_search":
                    counts["search_calls"] += 1
            try:
                results = list(await dispatch(calls))
            except BaseException:
                # A cancelled/failed batch may have partially executed reads.
                if any(call.name == "memory_fetch" for call in calls):
                    counts["fetch_cards"] = None
                if any(call.name == "memory_search" for call in calls):
                    counts["empty_searches"] = None
                raise
            by_id = {r.call_id: r for r in results}
            for call in calls:
                if call.name not in {"memory_search", "memory_fetch"}:
                    continue
                result = by_id.get(call.id)
                if result is not None and str(result.content).startswith("error:"):
                    continue
                metadata = (getattr(result, "metadata", None) or {})
                field = "empty_searches" if call.name == "memory_search" else "fetch_cards"
                key = "memory_matched" if call.name == "memory_search" else "memory_count"
                value = metadata.get(key)
                if type(value) is not int or value < 0:
                    counts[field] = None
                elif counts[field] is not None:
                    counts[field] += int(value == 0) if call.name == "memory_search" else value
            return results

        outcome = None
        try:
            outcome = await loop(*args, **{**kwargs, "dispatch_tools": counted_dispatch})
            return outcome
        finally:
            detail = {
                "runtime": "v2", "driver": "v2", "source": "tool_loop",
                "counts": counts, "quoted_memories": None,
                "unknown": [key for key, value in counts.items() if value is None] + ["quoted_memories"],
                "outcome": getattr(outcome, "stop_reason", "interrupted"),
            }
            try:
                result = on_memory_recall_completed(detail)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # telemetry must never change turn outcome
                log.warning("recall trace callback failed: %s", type(exc).__name__)
    return run


def summary(counts: dict) -> str:
    def n(key):
        value = counts.get(key)
        return "?" if value is None else str(value)
    return (f"召回 注入{n('injected')} · 已选{n('selected')} · 索引{n('index_calls')} · "
            f"搜索{n('search_calls')}(空{n('empty_searches')}) · 取卡{n('fetch_cards')}")
