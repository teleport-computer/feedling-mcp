from __future__ import annotations

import json
import re
from typing import Any


def query_has_sensitive_data(query: str) -> bool:
    text = str(query or "")
    if re.search(r"\b(sk-[A-Za-z0-9_\-]{12,}|AIza[0-9A-Za-z_\-]{20,}|[A-Fa-f0-9]{48,})\b", text):
        return True
    if re.search(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+", text):
        return True
    for match in re.finditer(r"\b(?:\+?\d[\d\s().-]{8,}\d)\b", text):
        if len(re.sub(r"\D", "", match.group(0))) >= 9:
            return True
    return False


def sanitize_web_query(query: str) -> str:
    clean = re.sub(r"\s+", " ", str(query or "").strip())
    if not clean:
        return ""
    clean = clean.strip("`\"'“”‘’")
    if len(clean) < 3 or query_has_sensitive_data(clean):
        return ""
    return clean[:220]


def extract_web_search_requests(parsed: Any, *, enabled: bool, max_queries: int) -> list[dict]:
    if not enabled:
        return []

    raw_requests: list[Any] = []
    if isinstance(parsed, dict):
        for key in ("tool_requests", "tool_calls", "tools"):
            value = parsed.get(key)
            if isinstance(value, list):
                raw_requests.extend(value)
        web_search = parsed.get("web_search")
        if isinstance(web_search, list):
            raw_requests.extend(web_search)
        elif isinstance(web_search, (dict, str)):
            raw_requests.append(web_search)
        for key in ("search_query", "web_search_query"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                raw_requests.append({"tool": "web_search", "query": value})

    requests_out: list[dict] = []
    seen: set[str] = set()
    for raw in raw_requests:
        tool_name = "web_search"
        query = ""
        reason = ""
        if isinstance(raw, str):
            query = raw
        elif isinstance(raw, dict):
            tool_name = str(raw.get("tool") or raw.get("name") or raw.get("type") or "web_search")
            args_raw: Any = raw.get("arguments")
            function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            if function:
                tool_name = str(function.get("name") or tool_name)
                args_raw = function.get("arguments", args_raw)
            if isinstance(args_raw, str):
                try:
                    loaded_args = json.loads(args_raw)
                    args = loaded_args if isinstance(loaded_args, dict) else {}
                except Exception:
                    args = {}
            else:
                args = args_raw if isinstance(args_raw, dict) else {}
            query = str(raw.get("query") or args.get("query") or args.get("q") or args.get("input") or "")
            reason = str(raw.get("reason") or args.get("reason") or "")
        else:
            continue
        if tool_name.lower().replace("-", "_") not in {"web_search", "search", "internet_search", "browser_search"}:
            continue
        clean_query = sanitize_web_query(query)
        if not clean_query:
            continue
        dedupe_key = clean_query.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        requests_out.append({
            "tool": "web_search",
            "query": clean_query,
            "reason": reason[:240],
            "source": "model_request",
        })
        if len(requests_out) >= max_queries:
            break
    return requests_out
