"""Prompt-level memory tools for hosted foreground chat."""

from __future__ import annotations

from typing import Any

from memory.prompts_v1 import MEMORY_CONTEXT_FRAMING_V1
import memory_readside_core


MEMORY_INDEX_TOOL = "memory_index"
MEMORY_FETCH_TOOL = "memory_fetch"
MEMORY_FETCH_PER_CALL_LIMIT = 5
MEMORY_FETCH_CUMULATIVE_LIMIT = 8


def memory_tool_instruction_message() -> dict:
    return {
        "role": "system",
        "content": (
            "Memory tools are available through JSON tool_calls.\n"
            "When long-term memory may matter, first call "
            "{\"tool_calls\":[{\"name\":\"memory_index\",\"args\":{\"query\":\"...\",\"bucket\":\"optional\",\"thread\":\"optional\",\"include_sensitive\":false}}]}.\n"
            "memory_index returns safe summaries only. Read those summaries, then fetch only directly relevant ids with "
            "{\"tool_calls\":[{\"name\":\"memory_fetch\",\"args\":{\"ids\":[\"mem_id\"]}}]}.\n"
            "Usually fetch 1-3 memories, never every memory. Set include_sensitive=true only when the user explicitly asks "
            "about a sensitive/private topic. If no index item is clearly relevant, answer without inventing.\n\n"
            + MEMORY_CONTEXT_FRAMING_V1
        ),
    }


def _trace(trace: dict | None) -> dict:
    if trace is None:
        return {}
    trace.setdefault("mode", "agent_tools")
    trace.setdefault("index_called", False)
    trace.setdefault("fetch_called", False)
    trace.setdefault("tool_calls", [])
    trace.setdefault("fetched_ids", [])
    trace.setdefault("cumulative_fetch_limit", MEMORY_FETCH_CUMULATIVE_LIMIT)
    return trace


def _clean_id_list(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        memory_id = str(item or "").strip()
        if not memory_id or memory_id in seen:
            continue
        seen.add(memory_id)
        out.append(memory_id)
    return out


def _result_for_error(name: str, error: str, trace: dict | None = None) -> dict:
    tr = _trace(trace)
    call = {"name": name, "ok": False, "error": error}
    if tr is not None:
        tr["tool_calls"].append(call)
    return {"ok": False, "name": name, "error": error}


def execute_memory_tool(store, api_key: str | None, name: str, args: dict | None, *, trace: dict | None = None) -> dict:
    args = dict(args or {})
    tr = _trace(trace)
    if name == MEMORY_INDEX_TOOL:
        try:
            limit = memory_readside_core.effective_readside_limit(args.get("limit"))
        except ValueError:
            return _result_for_error(name, "invalid_limit", trace=tr)
        payload = {
            "query": str(args.get("query") or args.get("q") or "")[:500],
            "limit": limit,
            "include_sensitive": bool(args.get("include_sensitive", False)),
        }
        for key in ("bucket", "thread"):
            value = str(args.get(key) or "").strip()
            if value:
                payload[key] = value[:120]
        if args.get("ambient") is not None:
            payload["ambient"] = bool(args.get("ambient"))
        if args.get("ambient_top_n") is not None:
            payload["ambient_top_n"] = args.get("ambient_top_n")
        body = memory_readside_core.memory_index_core(store, api_key, payload)
        items = body.get("items") if isinstance(body.get("items"), list) else []
        tr["index_called"] = True
        tr["user_card_count"] = int(body.get("user_card_count") or len(items))
        tr["tool_calls"].append({
            "name": name,
            "ok": True,
            "item_count": len(items),
            "user_card_count": tr["user_card_count"],
        })
        return {
            "ok": True,
            "name": name,
            "items": items,
            "limit": int(body.get("limit") or payload["limit"]),
            "user_card_count": tr["user_card_count"],
        }
    if name == MEMORY_FETCH_TOOL:
        requested_ids = _clean_id_list(args.get("ids") if "ids" in args else args.get("id"))
        if not requested_ids:
            return _result_for_error(name, "ids_required", trace=tr)
        already = set(str(mid) for mid in (tr.get("fetched_ids") or []))
        remaining_budget = max(0, int(tr.get("cumulative_fetch_limit") or MEMORY_FETCH_CUMULATIVE_LIMIT) - len(already))
        allowed = [mid for mid in requested_ids if mid not in already][: min(MEMORY_FETCH_PER_CALL_LIMIT, remaining_budget)]
        capped = len(allowed) < len([mid for mid in requested_ids if mid not in already])
        body = memory_readside_core.memory_fetch_core(
            store,
            api_key,
            {
                "ids": allowed,
                "include_archived": bool(args.get("include_archived", False)),
                "include_superseded": bool(args.get("include_superseded", False)),
            },
        ) if allowed else {"items": [], "missing_ids": [], "unavailable_ids": []}
        items = body.get("items") if isinstance(body.get("items"), list) else []
        fetched_ids = list(tr.get("fetched_ids") or [])
        for memory_id in allowed:
            if memory_id not in fetched_ids:
                fetched_ids.append(memory_id)
        tr["fetched_ids"] = fetched_ids
        tr["fetch_called"] = True
        tr["tool_calls"].append({
            "name": name,
            "ok": True,
            "ids": allowed,
            "item_count": len(items),
            "capped": capped,
        })
        return {
            "ok": True,
            "name": name,
            "items": items,
            "missing_ids": body.get("missing_ids") or [],
            "unavailable_ids": body.get("unavailable_ids") or [],
            "capped": capped,
        }
    return _result_for_error(name, "unknown_memory_tool", trace=tr)
