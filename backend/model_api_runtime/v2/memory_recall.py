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


_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", str(text or "")).strip()


def _message_text(message: dict) -> str:
    """Text of one chat message whether ``content`` is a string or a parts list.

    Parts are concatenated **losslessly** (no separator): an adapter may split a
    single line anywhere, including inside a token such as an ``NP-4286`` code,
    and inserting a newline there would fabricate a boundary that makes the line
    look absent.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                for key in ("text", "content"):
                    if isinstance(part.get(key), str):
                        out.append(part[key])
                        break
        return "".join(out)
    return "" if content is None else str(content)


# Bounds for the embedded-JSON scan. This runs on the pre-provider path, so it
# must be linear and total-time bounded no matter what the text looks like: a
# flood of unmatched braces used to rescan to the end from every position
# (quadratic), and a deeply nested value used to raise RecursionError out of
# json.loads. Anything past a bound is simply not considered for structural
# matching — the verbatim path still applies.
_MAX_JSON_CANDIDATES = 512
_MAX_JSON_SCAN_CHARS = 200_000
_MAX_JSON_DEPTH = 40
_MAX_JSON_SPAN_CHARS = 20_000


def _json_objects(text: str) -> tuple[list[dict], bool]:
    """Every balanced JSON object embedded in ``text``, in one linear pass.

    Returns ``(objects, complete)``; ``complete`` is False when a bound stopped
    the scan short, so the caller can say "not verified" instead of "absent".

    Quote state is tracked **only inside a candidate object**: a lone quotation
    mark in ordinary prose ("他说\"好\"" before an entry) must not swallow the
    JSON that follows it.
    """
    complete = len(text) <= _MAX_JSON_SCAN_CHARS
    text = text[:_MAX_JSON_SCAN_CHARS]
    out: list[dict] = []
    stack: list[int] = []
    in_str = False
    esc = False
    for idx, ch in enumerate(text):
        if not stack:
            # Outside any object: only an opening brace matters.
            if ch == "{":
                stack.append(idx)
                in_str = False
                esc = False
            continue
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            # Past the depth cap we stop recording starts, but keep counting so
            # the matching close brace still unwinds correctly.
            if len(stack) >= _MAX_JSON_DEPTH:
                complete = False
                stack.append(-1)
            else:
                stack.append(idx)
        elif ch == "}":
            start = stack.pop()
            if start < 0:
                continue
            if idx - start >= _MAX_JSON_SPAN_CHARS:
                complete = False
                continue
            try:
                row = json.loads(text[start:idx + 1])
            except (ValueError, RecursionError):
                complete = False
                continue
            if isinstance(row, dict):
                out.append(row)
                if len(out) >= _MAX_JSON_CANDIDATES:
                    return out, False
    if stack or in_str:
        # Text ended inside an object or a string: everything after that point
        # was swallowed, so this scan proves nothing about what is not here.
        complete = False
    return out, complete


def _entries(block: str, ids) -> dict[str, str]:
    """``card_id -> rendered line`` for the lines of a rendered memory block."""
    wanted = {str(i) for i in (ids or [])}
    found: dict[str, str] = {}
    for line in str(block or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        mid = str(row.get("id") or "") if isinstance(row, dict) else ""
        if mid and mid in wanted and mid not in found:
            found[mid] = line
    return found


def arrival_ids(block: str, ids, messages) -> tuple[list[str], list[str], list[str]]:
    """Which rendered card entries are present in the final non-system messages.

    Returns ``(arrived, missing, unverified)`` in render order. A card counts as
    arrived when its rendered entry is found in the concatenated non-system text
    either verbatim (whitespace-normalized) or as an equal JSON object — equal
    field by field, so a re-formatted entry still counts and an edited one does
    not. **Only a complete scan may call a card missing**: when a bound stopped
    the structural scan short, unmatched cards are reported as unverified, so a
    capped scan never reads as "the memory was not injected". Ids named in
    ``ids`` but absent from the block are in none of the three lists.
    """
    entries = _entries(block, ids)
    if not entries:
        return [], [], []
    text = "".join(_message_text(m) for m in messages
                   if isinstance(m, dict) and m.get("role") != "system")
    haystack = _norm(text)
    rows, complete = _json_objects(text)
    # A card id may legitimately appear more than once (an earlier, stale copy in
    # the transcript and the current entry): any equal object counts as arrival,
    # not just the first one seen.
    structural: dict[str, list[dict]] = {}
    for row in rows:
        mid = str(row.get("id") or "")
        if mid:
            structural.setdefault(mid, []).append(row)
    arrived, unmatched = [], []
    for mid, line in entries.items():
        if _norm(line) in haystack:
            arrived.append(mid)
            continue
        try:
            rendered = json.loads(line)
        except ValueError:
            unmatched.append(mid)
            continue
        if any(row == rendered for row in structural.get(mid, ())):
            arrived.append(mid)
        else:
            unmatched.append(mid)
    if complete:
        return arrived, unmatched, []
    return arrived, [], unmatched


def arrived_chars(block: str, arrived) -> int:
    """Size of what actually arrived: the block with only the arrived card lines
    kept (frame lines — header, notice, footer — always kept), so a full arrival
    equals ``len(block)`` and a partial one is proportionally smaller. This is a
    normalized size of the rendered block, not the byte count of the provider
    payload."""
    keep = {str(i) for i in (arrived or [])}
    kept = []
    for line in str(block or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                mid = str(json.loads(stripped).get("id") or "")
            except (ValueError, AttributeError):
                mid = ""
            if mid not in keep:
                continue
        kept.append(line)
    return len("\n".join(kept)) if kept else 0


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
                # Arrival is proven per card line on the final messages, the way
                # the V1 consumer does it: whitespace-normalized containment in
                # any non-system message, whose content may be a string or a
                # list of parts (Anthropic/Gemini shapes). Whole-block equality
                # was the previous check and it reads "never arrived" as soon
                # as an adapter re-flows or splits the text (haoxuan, T529).
                # This callback runs before the provider request; an accounting
                # failure must degrade the observation, never the turn.
                arrival_error = ""
                try:
                    ids, missing, unverified = arrival_ids(block, view.get("ids"), messages)
                except Exception as exc:  # noqa: BLE001
                    # An accounting failure means we do not know what arrived —
                    # report the ids as unverified, never as absent.
                    ids, missing = [], []
                    unverified = [str(i) for i in (view.get("ids") or [])]
                    arrival_error = type(exc).__name__
                    log.warning("memory arrival accounting failed: %s", arrival_error)
                reached = bool(ids)
                injected_ids.update(ids)
                counts["injected"] = len(injected_ids)
                counts["selected"] = view.get("selected")
                systems = "\n".join(_message_text(m) for m in messages
                                    if m.get("role") == "system")
                observation = {
                    "runtime": "v2", "driver": "v2", "driver_request": "prepared",
                    "round": payload.get("round"),
                    "injected_ids": ids, "missing_ids": missing,
                    # Unverified ids are neither proven present nor proven
                    # absent: a bound stopped the scan, or accounting raised.
                    "unverified_ids": unverified,
                    "injected_is_lower_bound": bool(unverified),
                    "arrival_error": arrival_error or None,
                    "injected_chars": arrived_chars(block, ids) if reached else 0,
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
                # ``injected`` counts what we could prove; when any round could
                # not be verified it is a lower bound, not a measurement.
                "unverified_ids": sorted({i for o in prompt_observations
                                          for i in o["unverified_ids"]}),
                "injected_is_lower_bound": any(o["injected_is_lower_bound"]
                                               for o in prompt_observations),
                "arrival_errors": sorted({o["arrival_error"] for o in prompt_observations
                                          if o["arrival_error"]}),
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
