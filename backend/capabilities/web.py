"""Web capability — keyless facade over backend/model_api_runtime/tools.py.

Legacy runtime web access is a keyless DuckDuckGo HTML scrape (no provider,
no API key) already implemented in model_api_runtime/tools.py
(`web_search_duckduckgo`, `sanitize_web_query`, `query_has_sensitive_data`,
`_strip_html_text`). This facade exposes it as V2 capabilities so the
planner/executor gain web access parity with the legacy runtime
(merge-review condition 4b) — no reimplementation, just the uniform
CapabilityResult shape + input guards + redaction/size caps for untrusted
external content.
"""
from __future__ import annotations

from urllib.parse import urljoin, urlparse

import httpx

from core import net_safety
from model_api_runtime import tools

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err

_DEFAULT_SEARCH_LIMIT = 5
_MAX_SEARCH_LIMIT = 10
_SEARCH_TIMEOUT_SEC = 8.0

_FETCH_TIMEOUT_SEC = 8.0
_FETCH_MAX_BODY_BYTES = 40_000  # cap raw HTML before stripping — untrusted external body
_FETCH_USER_AGENT = "Mozilla/5.0 (compatible; FeedlingIO/1.0; +https://feedling.app)"
_FETCH_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _resolve_ips(host: str) -> list[str]:
    return net_safety.resolve_ips(host)


def _blocked_url_kind(url: str) -> str | None:
    return net_safety.blocked_url_kind(url, resolve=_resolve_ips)


def _stream_get(url: str, *, timeout: float, follow_redirects: bool, headers: dict):
    return httpx.stream(
        "GET", url, timeout=timeout, follow_redirects=follow_redirects, headers=headers)


def _read_capped_body(resp) -> str | None:
    """Read at most the configured raw-body cap; ``None`` means oversized."""
    raw_length = str(resp.headers.get("content-length") or "").strip()
    if raw_length:
        try:
            if int(raw_length) > _FETCH_MAX_BODY_BYTES:
                return None
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > _FETCH_MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    encoding = getattr(resp, "encoding", None) or "utf-8"
    return b"".join(chunks).decode(encoding, errors="replace")


def search(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    """params: {"query": str, "limit": int?}. Keyless DuckDuckGo HTML scrape."""
    params = params or {}
    raw_query = str(params.get("query") or "").strip()
    if not raw_query:
        return err(errors.INVALID, "query is required", retryable=False)

    # Refuse before ever touching the network — sensitive-looking queries (emails,
    # API keys, phone numbers) must not leave the process as a search term.
    if tools.query_has_sensitive_data(raw_query):
        return err(errors.INVALID,
                   "query refused: appears to contain sensitive data", retryable=False)

    query = tools.sanitize_web_query(raw_query)
    if not query:
        return err(errors.INVALID, "query is invalid", retryable=False)

    limit = params.get("limit")
    try:
        limit = int(limit) if limit is not None else _DEFAULT_SEARCH_LIMIT
    except (TypeError, ValueError):
        limit = _DEFAULT_SEARCH_LIMIT
    limit = max(1, min(limit, _MAX_SEARCH_LIMIT))

    try:
        results = tools.web_search_duckduckgo(query, limit=limit, timeout_sec=_SEARCH_TIMEOUT_SEC)
    except Exception as e:
        return err(errors.UPSTREAM,
                   f"web search failed: {type(e).__name__}: {e}", retryable=True)

    capped = errors.cap_list(results)
    return ok(data={"query": query, "results": [errors.cap_data(r) for r in capped]})


def fetch(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    """params: {"url": str}. Fetches + strips HTML to text; size-capped, no API key."""
    params = params or {}
    url = str(params.get("url") or "").strip()
    if not url:
        return err(errors.INVALID, "url is required", retryable=False)

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return err(errors.INVALID, "url must be an absolute http(s) url", retryable=False)

    current_url = url
    status_code = 0
    response_headers: dict = {}
    body: str | None = ""
    for redirect_count in range(_FETCH_MAX_REDIRECTS + 1):
        blocked = _blocked_url_kind(current_url)
        if blocked == "blocked_url":
            return err(errors.INVALID, "url is not permitted", retryable=False)
        if blocked == "dns":
            return err(errors.UPSTREAM, "url host could not be resolved", retryable=True)
        try:
            with _stream_get(
                current_url, timeout=_FETCH_TIMEOUT_SEC, follow_redirects=False,
                headers={"User-Agent": _FETCH_USER_AGENT},
            ) as resp:
                status_code = resp.status_code
                response_headers = dict(resp.headers)
                body = _read_capped_body(resp) if 200 <= status_code < 300 else ""
        except Exception as e:
            return err(errors.UPSTREAM,
                       f"web fetch failed: {type(e).__name__}: {e}", retryable=True)
        if status_code not in _REDIRECT_STATUSES:
            break
        if redirect_count >= _FETCH_MAX_REDIRECTS:
            return err(errors.UPSTREAM, "web fetch exceeded redirect limit", retryable=False)
        location = str(response_headers.get("location") or "").strip()
        if not location:
            return err(errors.UPSTREAM, "web fetch redirect missing location", retryable=False)
        current_url = urljoin(current_url, location)

    if not (200 <= status_code < 300):
        return err(errors.code_for_status(status_code),
                   f"fetch failed with status {status_code}",
                   retryable=errors.retryable_for_status(status_code))
    if body is None:
        return err(errors.UPSTREAM, "web fetch body exceeded size limit", retryable=False)

    text = tools._strip_html_text(body)
    return ok(data={"url": current_url, "text": errors.cap_text(text)})
