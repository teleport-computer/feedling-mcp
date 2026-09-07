"""Per-loop recall accounting; content-free and side effects injected.

Count actual dispatches, not provider proposals, cached discovery reuse, or
background reads outside this loop. A failed read is not an empty search.
"""
from __future__ import annotations

from functools import wraps
import inspect
import json
import logging
import re


log = logging.getLogger(__name__)


def traced(loop):
    """One terminal callback across every return/exception/cancellation path."""
    @wraps(loop)
    async def run(*args, on_memory_recall_completed=None,
                  memory_context_observation=None, **kwargs):
        if on_memory_recall_completed is None:
            return await loop(*args, **kwargs)
        counts = {"injected": 0, "selected": None, "index_calls": 0,
                  "search_calls": 0, "empty_searches": 0, "fetch_cards": 0}
        dispatch = kwargs["dispatch_tools"]
        trajectory = kwargs.get("on_trajectory_event")
        dispatched_ids = set()
        tool_results = []
        prompt_observations = []
        injected_ids = set()

        async def observed_trajectory(kind, payload):
            if kind == "provider_request":
                from model_api_runtime.v2 import context
                view = memory_context_observation or {}
                messages = [m for m in payload.get("messages", []) if isinstance(m, dict)]
                block = view.get("block")
                reached = bool(block and any(
                    m.get("role") != "system" and m.get("content") == block for m in messages
                ))
                ids = list(view.get("ids") or []) if reached else []
                injected_ids.update(ids)
                counts["injected"] = len(injected_ids)
                counts["selected"] = view.get("selected")
                systems = "\n".join(str(m.get("content") or "") for m in messages
                                    if m.get("role") == "system")
                observation = {
                    "runtime": "v2", "driver": "v2", "driver_request": "prepared",
                    "round": payload.get("round"),
                    "injected_ids": ids, "injected_chars": len(block) if reached else 0,
                    "block_header": "# 相关记忆" if reached else None,
                    "profile_used": context.AGENT_MEMORY_HEADER + "\n" in systems
                                    or context.USER_PROFILE_HEADER + "\n" in systems,
                }
                prompt_observations.append(observation)
                # The encrypted trajectory retains the actual messages; this
                # additional content-free projection is measured on those same
                # final messages, after prompt-frontier planning and fallback.
                payload = {**payload, "memory_context": observation}
            elif kind == "tool_batch_result":
                by_id = {r.call_id: r for r in payload.get("results", [])}
                for call in payload.get("calls", []):
                    if call.id not in dispatched_ids or call.name not in {
                        "memory_index", "memory_search", "memory_fetch"
                    }:
                        continue
                    result = by_id.get(call.id)
                    if result is not None:
                        tool_results.append(result_observation(call, result))
            if trajectory is not None:
                await trajectory(kind, payload)

        async def counted_dispatch(calls):
            dispatched_ids.update(call.id for call in calls)
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
            outcome = await loop(*args, **{**kwargs, "dispatch_tools": counted_dispatch,
                                           "on_trajectory_event": observed_trajectory})
            return outcome
        finally:
            detail = {
                "runtime": "v2", "driver": "v2", "source": "tool_loop",
                "counts": counts, "quoted_memories": None,
                "unknown": [key for key, value in counts.items() if value is None] + ["quoted_memories"],
                "outcome": getattr(outcome, "stop_reason", "interrupted"),
                "injected_ids": sorted(injected_ids),
                "injected_chars": max((o["injected_chars"] for o in prompt_observations), default=0),
                "profile_used": any(o["profile_used"] for o in prompt_observations)
                                if prompt_observations else None,
                # The worker emits these as separate flat events, not nested
                # lists (the durable trace sanitizer stringifies nested data).
                "prompt_observations": prompt_observations,
                "tool_results": tool_results,
            }
            try:
                result = on_memory_recall_completed(detail)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # telemetry must never change turn outcome
                log.warning("recall trace callback failed: %s", type(exc).__name__)
    return run


def result_observation(call, result) -> dict:
    """Measure final normalized JSON, not the producer's pre-budget metadata."""
    content = str(result.content)
    observation = {"call_id": call.id, "tool": call.name,
                   "result_chars": len(content), "json_complete": False,
                   "returned": None, "omitted": None}
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        observation["json_complete"] = True
        for field in ("returned", "omitted"):
            value = data.get(field)
            if type(value) is int and value >= 0:
                observation[field] = value
        observation["source_truncated"] = data.get("source_truncated")
    if call.name == "memory_fetch":
        raw_ids = call.args.get("ids") or []
        raw_ids = raw_ids if isinstance(raw_ids, list) else [raw_ids]
        def safe_ids(values):
            return [v for v in values if isinstance(v, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", v)][:20]
        observation["requested_ids"] = safe_ids(raw_ids)
        items = data.get("items") if isinstance(data, dict) else None
        observation["returned_ids"] = safe_ids(
            [item.get("id") for item in items if isinstance(item, dict)]
        ) if isinstance(items, list) else None
    return observation


def summary(counts: dict) -> str:
    def n(key):
        value = counts.get(key)
        return "?" if value is None else str(value)
    return (f"召回 注入{n('injected')} · 已选{n('selected')} · 索引{n('index_calls')} · "
            f"搜索{n('search_calls')}(空{n('empty_searches')}) · 取卡{n('fetch_cards')}")
