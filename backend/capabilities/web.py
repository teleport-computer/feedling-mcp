"""Web capability — keyless facade over backend/model_api_runtime/tools.py.

Legacy runtime web access is a keyless DuckDuckGo HTML scrape (no provider,
no API key) already implemented in model_api_runtime/tools.py
(`web_search_duckduckgo`, `sanitize_web_query`, `query_has_sensitive_data`,
`_strip_html_text`). This facade exposes it as V2 capabilities so the
provider-native tool loop gains web access parity with the legacy runtime
(merge-review condition 4b) — no reimplementation, just the uniform
CapabilityResult shape + input guards + redaction/size caps for untrusted
external content.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import re
import threading
import time
from urllib.parse import urljoin, urlparse

import httpx

from core import net_safety
from model_api_runtime import tools

from capabilities import errors
from capabilities import html_extract
from capabilities import result_budget
from capabilities.types import CapabilityResult, ok, err

_DEFAULT_SEARCH_LIMIT = 5
_MAX_SEARCH_LIMIT = 10
_SEARCH_TIMEOUT_SEC = 8.0

_FETCH_TIMEOUT_SEC = 8.0
# Cap on raw HTML retained from an untrusted host before stripping. 40 KB was
# the old value and no longer buys a whole page anywhere — Wikipedia is 360 KB,
# a weather page 86 KB — so a fetch saw only the <head> and navigation.
#
# This bounds what we KEEP, not strictly what crosses the wire: httpx hands over
# a whole decoded chunk before we slice it, and a compressed response can expand
# inside its decoder first. It is a retention cap, not a bandwidth guarantee.
#
# It is deliberately NOT the bound that protects the prompt. Two later stages do
# that, and they are the real limit on what reaches the model: web_fetch first
# shrinks structurally to its shared 8000-character atomic JSON policy, then
# the executor and tool loop preserve that object without string-slicing it.
# Raising a limit here alone would only increase attempt-local retained memory.
_FETCH_MAX_BODY_BYTES = 300_000

# One tool-loop attempt may retain extracted pages so the model can continue a
# large page with ``offset`` without repeating the outbound request. These are
# lifetime-memory ceilings, not output budgets: redirect aliases point at the
# same document and do not count twice.
_FETCH_SESSION_MAX_URLS = 8
_FETCH_SESSION_MAX_CHARS = 600_000
_FETCH_OUTPUT_URL_CHARS = 1000
# A waiter can sit below an instance-wide enclave permit. Keep that hold below
# every pool's stall budget; the shared broker also serves 120-second heavy slots.
# A normal fetch may spend up to 48 seconds across six independently timed hops.
_FETCH_SESSION_WAIT_TIMEOUT_SEC = 60.0

# Kill switch for article extraction. Default ON: a flag that ships OFF is how
# you get "the code deployed but the feature never did", which this workspace
# has been bitten by. Flipping it to "0" returns web_fetch to the plain
# tag-strip — the behaviour every page had before extraction existed.
# NOTE: read at import; changing it requires restarting the runner. It is a
# rollback lever, not a live control-plane switch.
_EXTRACT_ARTICLE = os.environ.get("FEEDLING_WEB_EXTRACT_ARTICLE", "1").strip() != "0"

# Only these get sent to an *article* extractor. web_fetch is also how the model
# reads a JSON API, a raw .py file, an RSS feed or a plain-text changelog, and
# running those through a "find the prose" heuristic silently drops fields —
# with a result long enough that no length-based guard would catch it.
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

# Below this, an extraction is treated as "it found a fragment", not "it found
# the article", and the plain strip is appended after it. Chosen because real
# short pages exist (a status page, a term definition, a weather summary) and
# discarding a correct short answer would re-bury it under the navigation the
# extractor had just removed.
_ARTICLE_MIN_CHARS = 200
_FETCH_USER_AGENT = "Mozilla/5.0 (compatible; FeedlingIO/1.0; +https://feedling.app)"
_FETCH_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _resolve_ips(host: str) -> list[str]:
    return net_safety.resolve_ips(host)


def _blocked_url_kind(url: str) -> str | None:
    return net_safety.blocked_url_kind(url, resolve=_resolve_ips)


def _validated_pinned_ip(url: str) -> tuple[str | None, str | None]:
    """Validate one hop and pin it to a checked address (see net_safety)."""
    return net_safety.validated_pinned_ip(url, resolve=_resolve_ips)


def _pinned_url(url: str, resolved_ip: str) -> tuple[str, str, str]:
    return net_safety.pinned_url(url, resolved_ip)


@contextmanager
def _stream_get(
    url: str,
    *,
    resolved_ip: str,
    timeout: float,
    follow_redirects: bool,
    headers: dict,
):
    """Connect to the validated IP while preserving HTTP Host and TLS SNI."""
    pinned, host_header, sni_hostname = _pinned_url(url, resolved_ip)
    outbound_headers = dict(headers)
    outbound_headers["Host"] = host_header
    # Ignore HTTP(S)_PROXY/ALL_PROXY from the process environment: a proxy
    # would bypass the pinned direct connection and re-resolve the hostname.
    with httpx.Client(trust_env=False) as client:
        with client.stream(
            "GET",
            pinned,
            timeout=timeout,
            follow_redirects=follow_redirects,
            headers=outbound_headers,
            extensions={"sni_hostname": sni_hostname},
        ) as response:
            yield response


def _read_capped_body(resp) -> tuple[str, bool]:
    """Read up to the raw-body cap. Returns ``(text, was_truncated)``.

    This used to discard the whole response once it crossed the cap, which made
    the tool useless in practice: 40 KB no longer buys a whole page anywhere,
    so every real site (Wikipedia 360 KB, a weather page 86 KB, even the Python
    docs at 41.8 KB) came back as an upstream error and the model concluded it
    had no web access at all.

    Reading the first N bytes and stopping is both the useful behaviour and the
    safe one — the point of the cap is to bound what we pull from an untrusted
    host, and that is satisfied by not reading past it. Content-Length is only a
    hint about what is coming, never a reason to skip a page: it is attacker-
    controlled, often absent, and often wrong.
    """
    chunks: list[bytes] = []
    total = 0
    truncated = False
    for chunk in resp.iter_bytes():
        if total >= _FETCH_MAX_BODY_BYTES:
            truncated = True  # there was more after a body that exactly filled it
            break
        room = _FETCH_MAX_BODY_BYTES - total
        if len(chunk) > room:
            chunks.append(chunk[:room])
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    encoding = getattr(resp, "encoding", None) or "utf-8"
    # errors="replace": a hard byte cut lands mid-character on any multi-byte
    # page, which is most of them.
    return b"".join(chunks).decode(encoding, errors="replace"), truncated


_UNCLOSED_TAGS = ("script", "style", "noscript")


def _drop_unterminated_tail(html: str) -> str:
    """Cut a trailing ``<script>``/``<style>`` block that has no closing tag.

    ``tools._strip_html_text`` removes those blocks by matching a closing tag.
    When the byte cut lands inside a big inline script — or the page just ships
    malformed markup — the closing tag is not there, the regex matches nothing,
    and the whole JavaScript body survives into the text handed to the model.
    Dropping from the last unmatched opening tag onwards costs a little real
    content in the worst case and keeps minified JS out of the answer.
    """
    cut = len(html)
    lowered = html.lower()
    for tag in _UNCLOSED_TAGS:
        open_at = lowered.rfind(f"<{tag}")
        if open_at == -1:
            continue
        if lowered.find(f"</{tag}", open_at) == -1:
            cut = min(cut, open_at)
    return html[:cut]


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class _FetchDocument:
    url: str
    text: str
    source_truncated: bool


@dataclass
class _FetchPending:
    """Singleflight state for one request URL inside one attempt."""

    loading: bool = False
    done: bool = False
    document: _FetchDocument | None = None
    error: CapabilityResult | None = None


class WebFetchSession:
    """Attempt-scoped retained-page cache for ``web_fetch`` paging.

    The worker creates a fresh instance for each chat, wake, or child tool-loop
    attempt. There is deliberately no module-global fallback: V1 requests and
    later attempts must not inherit another turn's page or another user's data.
    """

    def __init__(
        self,
        *,
        max_urls: int = _FETCH_SESSION_MAX_URLS,
        max_chars: int = _FETCH_SESSION_MAX_CHARS,
    ) -> None:
        self.max_urls = max(1, int(max_urls))
        self.max_chars = max(1, int(max_chars))
        self._condition = threading.Condition()
        self._cache: dict[str, _FetchDocument] = {}
        self._pending: dict[str, _FetchPending] = {}
        self._document_count = 0
        self._total_chars = 0

    def prepare_batch(self, tool_calls) -> None:
        """Pre-register offset-zero owners before concurrent dispatch starts.

        This distinguishes a planned same-batch owner from a genuine cache miss.
        Worker admission ordering lets the owner finish first; defensive callers
        that bypass it receive a retryable error instead of waiting indefinitely.
        Planning creates no network or retained data.
        """
        with self._condition:
            for call in tool_calls or ():
                if str(getattr(call, "name", "")) != "web_fetch":
                    continue
                args = getattr(call, "args", None)
                if not isinstance(args, dict):
                    continue
                raw_offset = args.get("offset", 0)
                if type(raw_offset) is not int or raw_offset != 0:
                    continue
                url = str(args.get("url") or "").strip()
                if url and url not in self._cache:
                    self._pending.setdefault(url, _FetchPending())

    def document(
        self,
        url: str,
        *,
        offset: int,
        loader,
    ) -> _FetchDocument | CapabilityResult:
        """Return a retained document or run exactly one offset-zero loader."""
        if type(offset) is not int or offset < 0:
            return err(
                errors.INVALID,
                "offset must be a non-negative integer",
                retryable=False,
            )
        owner = False
        with self._condition:
            cached = self._cache.get(url)
            if cached is not None:
                return cached
            pending = self._pending.get(url)
            if offset > 0 and pending is None:
                return err(
                    errors.INVALID,
                    "offset requires an earlier web_fetch for this URL in the same turn",
                    retryable=False,
                )
            if pending is None:
                pending = self._pending.setdefault(url, _FetchPending())
            if offset > 0 and not pending.loading and not pending.done:
                # prepare_batch only proves an offset-zero sibling was planned;
                # it does not prove that sibling has acquired an execution slot.
                # Waiting here would let enough continuations occupy every outer
                # enclave permit and prevent their owner from ever starting.
                return err(
                    errors.UPSTREAM,
                    "earlier web_fetch for this URL has not started; retry",
                    retryable=True,
                )
            if offset == 0 and not pending.loading and not pending.done:
                if self._document_count >= self.max_urls:
                    pending.error = err(
                        errors.UNAVAILABLE,
                        "web fetch turn cache capacity exceeded; use a different turn",
                        retryable=False,
                    )
                    pending.done = True
                    self._condition.notify_all()
                    return pending.error
                pending.loading = True
                owner = True
            wait_deadline = time.monotonic() + _FETCH_SESSION_WAIT_TIMEOUT_SEC
            while not owner and not pending.done:
                remaining = wait_deadline - time.monotonic()
                if remaining <= 0:
                    return err(
                        errors.UPSTREAM,
                        "timed out waiting for earlier web_fetch; retry",
                        retryable=True,
                    )
                self._condition.wait(timeout=remaining)
            if not owner:
                return pending.document or pending.error or err(
                    errors.UPSTREAM,
                    "web fetch session did not retain a result",
                    retryable=True,
                )

        try:
            loaded = loader()
        except Exception as exc:  # keep every singleflight waiter releasable
            loaded = err(
                errors.UPSTREAM,
                f"web fetch failed: {type(exc).__name__}: {exc}",
                retryable=True,
            )
        with self._condition:
            if isinstance(loaded, CapabilityResult):
                pending.error = loaded
            else:
                new_chars = len(loaded.text)
                if (
                    self._document_count >= self.max_urls
                    or self._total_chars + new_chars > self.max_chars
                ):
                    pending.error = err(
                        errors.UNAVAILABLE,
                        "web fetch turn cache capacity exceeded; use a different turn",
                        retryable=False,
                    )
                else:
                    self._document_count += 1
                    self._total_chars += new_chars
                    pending.document = loaded
                    self._cache[url] = loaded
                    self._cache[loaded.url] = loaded
            pending.loading = False
            pending.done = True
            self._condition.notify_all()
            return pending.document or pending.error or err(
                errors.UPSTREAM,
                "web fetch session did not retain a result",
                retryable=True,
            )


def _page_title(html: str) -> str:
    """The document title, or "".

    The extractor's output does not carry it, and without it a weather page
    reads "中雨 24℃ 晴转多云" with no clue which city that is. Cheap to take
    from the markup and worth its ~40 characters of the budget.
    """
    m = _TITLE_RE.search(html)
    if not m:
        return ""
    return tools._strip_html_text(m.group(1))[:120]


def _looks_like_html(content_type: str, body: str) -> bool:
    """HTML by declaration, or by sniff when nothing was declared.

    A wrong or absent Content-Type is common; a *misleading* one is not worth
    defending against here, because the cost of guessing wrong is only that we
    fall back to the same tag-strip we would have used anyway.
    """
    declared = content_type.split(";", 1)[0].strip().lower()
    if declared:
        return declared in _HTML_CONTENT_TYPES
    head = body[:1000].lstrip().lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or "<body" in head


def _readable_text(html: str, *, content_type: str) -> str:
    """Article text when we can get it, plain stripped text otherwise.

    Three outcomes, and the middle one is the reason this is not a boolean:

    - **article** — extraction returned enough to stand on its own.
    - **hybrid** — it returned something short. Real pages are legitimately
      short, so the fragment is kept *first* (it is the part most likely to
      answer the question) and the plain strip follows to fill the budget.
      Throwing the fragment away would re-bury a correct short answer.
    - **fallback** — nothing usable, or this is not HTML at all.
    """
    plain = tools._strip_html_text(_drop_unterminated_tail(html))
    if not _EXTRACT_ARTICLE or not _looks_like_html(content_type, html):
        return plain

    article = html_extract.extract_article(html)
    if not article:
        return plain

    title = _page_title(html)
    # Skip the title when the extractor already opens with it, which it does on
    # most articles — paying twice for it out of 2000 characters is a real cost.
    if title and title[:40] not in article[:200]:
        article = f"{title}\n\n{article}"
    if len(article) >= _ARTICLE_MIN_CHARS:
        return article
    return f"{article}\n\n{plain}"


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


def _fetch_document(url: str) -> _FetchDocument | CapabilityResult:
    """Perform one validated outbound fetch and retain its extracted text."""
    current_url = url
    status_code = 0
    response_headers: dict = {}
    body = ""
    truncated = False
    for redirect_count in range(_FETCH_MAX_REDIRECTS + 1):
        blocked, resolved_ip = _validated_pinned_ip(current_url)
        if blocked == "blocked_url":
            return err(errors.INVALID, "url is not permitted", retryable=False)
        if blocked == "dns":
            return err(errors.UPSTREAM, "url host could not be resolved", retryable=True)
        try:
            with _stream_get(
                current_url, resolved_ip=str(resolved_ip),
                timeout=_FETCH_TIMEOUT_SEC, follow_redirects=False,
                headers={"User-Agent": _FETCH_USER_AGENT},
            ) as resp:
                status_code = resp.status_code
                response_headers = dict(resp.headers)
                if 200 <= status_code < 300:
                    body, truncated = _read_capped_body(resp)
                else:
                    body, truncated = "", False
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
    text = _readable_text(body, content_type=str(response_headers.get("content-type") or ""))
    return _FetchDocument(
        url=current_url[:_FETCH_OUTPUT_URL_CHARS],
        text=text,
        source_truncated=truncated,
    )


def _paged_fetch_payload(document: _FetchDocument, offset: int) -> dict:
    """Structurally shrink one page to the shared atomic JSON result cap."""
    total_chars = len(document.text)
    if offset > total_chars:
        raise ValueError("offset exceeds retained page text")
    policy = result_budget.for_tool("web_fetch")
    max_chars = policy.result_cap if policy is not None else 8000

    def payload_for(candidate_end: int) -> dict:
        page_text = document.text[offset:candidate_end]
        has_more = candidate_end < total_chars
        return {
            "url": document.url,
            "total_chars": total_chars,
            "offset": offset,
            "returned_chars": len(page_text),
            "next_offset": candidate_end if has_more else None,
            "has_more": has_more,
            "source_truncated": document.source_truncated,
            "text": page_text,
        }

    # Binary-search the largest whole text slice whose actual JSON rendering
    # fits. The downstream atomic policy then preserves it byte-for-byte.
    low = offset
    high = min(total_chars, offset + max_chars)
    while low < high:
        candidate = (low + high + 1) // 2
        if len(json.dumps(payload_for(candidate), ensure_ascii=False)) <= max_chars:
            low = candidate
        else:
            high = candidate - 1
    payload = payload_for(low)
    if len(json.dumps(payload, ensure_ascii=False)) > max_chars:
        raise RuntimeError("web_fetch result cap cannot hold its metadata")
    return payload


def fetch(
    store,
    *,
    api_key=None,
    runtime_token=None,
    params=None,
    web_fetch_session: WebFetchSession | None = None,
) -> CapabilityResult:
    """Fetch and page readable text; a continuation never repeats the network."""
    params = params or {}
    url = str(params.get("url") or "").strip()
    if not url:
        return err(errors.INVALID, "url is required", retryable=False)

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return err(errors.INVALID, "url must be an absolute http(s) url", retryable=False)
    raw_offset = params.get("offset", 0)
    if type(raw_offset) is not int or raw_offset < 0:
        return err(errors.INVALID, "offset must be a non-negative integer", retryable=False)
    offset = raw_offset

    if web_fetch_session is None:
        if offset > 0:
            return err(
                errors.INVALID,
                "offset requires an earlier web_fetch for this URL in the same turn",
                retryable=False,
            )
        document = _fetch_document(url)
    else:
        document = web_fetch_session.document(
            url,
            offset=offset,
            loader=lambda: _fetch_document(url),
        )
    if isinstance(document, CapabilityResult):
        return document
    try:
        return ok(data=_paged_fetch_payload(document, offset))
    except ValueError as exc:
        return err(errors.INVALID, str(exc), retryable=False)
