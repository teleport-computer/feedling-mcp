from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


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


def _strip_html_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", str(value or ""))
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _duckduckgo_result_url(raw_href: str) -> str:
    href = html.unescape(str(raw_href or "")).strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg") or []
        if uddg:
            return str(uddg[0])
    return href


def web_search_duckduckgo(query: str, *, limit: int, timeout_sec: float) -> list[dict]:
    resp = httpx.get(
        "https://duckduckgo.com/html/",
        params={"q": query, "kl": "us-en"},
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; FeedlingIO/1.0; +https://feedling.app)",
            "Accept": "text/html,application/xhtml+xml",
        },
        follow_redirects=True,
        timeout=timeout_sec,
    )
    resp.raise_for_status()
    body = resp.text
    results: list[dict] = []
    seen_urls: set[str] = set()
    anchor_re = re.compile(
        r'<a[^>]+class="[^"]*\bresult__a\b[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    snippet_re = re.compile(
        r'class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(.*?)</(?:a|div)>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in anchor_re.finditer(body):
        url = _duckduckgo_result_url(match.group(1))
        title = _strip_html_text(match.group(2))
        if not url or not title or url in seen_urls:
            continue
        window = body[match.end(): match.end() + 2600]
        snippet_match = snippet_re.search(window)
        snippet = _strip_html_text(snippet_match.group(1)) if snippet_match else ""
        seen_urls.add(url)
        results.append({
            "title": title[:240],
            "url": url[:600],
            "snippet": snippet[:700],
        })
        if len(results) >= limit:
            break
    return results


def run_web_searches(
    requests_in: list[dict],
    *,
    enabled: bool,
    max_queries: int,
    max_results: int,
    timeout_sec: float,
) -> dict:
    clean_requests: list[dict] = []
    seen: set[str] = set()
    for item in requests_in[:max_queries]:
        if not isinstance(item, dict):
            continue
        query = sanitize_web_query(str(item.get("query") or ""))
        if not query:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        clean_requests.append({
            "tool": "web_search",
            "query": query,
            "reason": str(item.get("reason") or "")[:240],
            "source": str(item.get("source") or "model_request")[:80],
        })
    if not clean_requests:
        return {
            "enabled": enabled,
            "status": "skipped",
            "requests": [],
            "results": [],
            "result_count": 0,
            "errors": [],
        }

    all_results: list[dict] = []
    errors: list[dict] = []
    for item in clean_requests:
        query = item["query"]
        try:
            results = web_search_duckduckgo(
                query,
                limit=max_results,
                timeout_sec=timeout_sec,
            )
            all_results.append({
                "query": query,
                "status": "ok" if results else "empty",
                "results": results,
            })
        except Exception as e:
            error = f"{type(e).__name__}:{str(e)[:220]}"
            errors.append({"query": query, "error": error})
            all_results.append({
                "query": query,
                "status": "failed",
                "results": [],
                "error": error,
            })
    result_count = sum(len(item.get("results") or []) for item in all_results)
    return {
        "enabled": enabled,
        "status": "ok" if result_count else ("failed" if errors else "empty"),
        "requests": clean_requests,
        "results": all_results,
        "result_count": result_count,
        "errors": errors,
    }


def web_search_trace(web_search: dict, *, max_queries: int) -> dict:
    if not web_search:
        return {"status": "skipped", "requests": 0, "results": 0}
    return {
        "status": str(web_search.get("status") or ""),
        "requests": len(web_search.get("requests") or []),
        "results": int(web_search.get("result_count") or 0),
        "queries": [
            str(item.get("query") or "")[:160]
            for item in (web_search.get("requests") or [])[:max_queries]
            if isinstance(item, dict)
        ],
        "errors": web_search.get("errors") or [],
    }
