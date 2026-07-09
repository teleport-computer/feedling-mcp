"""Web capability facade — keyless facade over model_api_runtime/tools.py's
DuckDuckGo HTML scrape (§merge-review condition 4b: V2 planner/executor need web
access parity with the legacy runtime). NO live network — everything monkeypatched.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))  # noqa: E402

from model_api_runtime import tools  # noqa: E402
from capabilities import web as cap_web  # noqa: E402
from capabilities import registry as cap_registry  # noqa: E402


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------

def test_search_happy_path_wraps_results(monkeypatch):
    captured = {}

    def fake_search(query, *, limit, timeout_sec):
        captured["query"] = query
        captured["limit"] = limit
        return [
            {"title": "Result One", "url": "https://example.com/1", "snippet": "one"},
            {"title": "Result Two", "url": "https://example.com/2", "snippet": "two"},
        ]

    monkeypatch.setattr(tools, "web_search_duckduckgo", fake_search)
    r = cap_web.search("STORE", params={"query": "feedling io"})
    assert r.ok is True
    assert captured["query"] == "feedling io"
    assert r.data["results"] == [
        {"title": "Result One", "url": "https://example.com/1", "snippet": "one"},
        {"title": "Result Two", "url": "https://example.com/2", "snippet": "two"},
    ]


def test_search_missing_query_is_invalid():
    r = cap_web.search("STORE", params={"query": "  "})
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"


def test_search_no_params_is_invalid():
    r = cap_web.search("STORE", params=None)
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"


def test_search_sensitive_query_is_refused_without_calling_underlying_search(monkeypatch):
    called = {"hit": False}

    def fake_search(*a, **k):
        called["hit"] = True
        return []

    monkeypatch.setattr(tools, "web_search_duckduckgo", fake_search)
    r = cap_web.search("STORE", params={"query": "my email is bob@example.com"})
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"
    assert called["hit"] is False


def test_search_sensitive_query_flagged_via_query_has_sensitive_data(monkeypatch):
    called = {"hit": False}
    monkeypatch.setattr(tools, "query_has_sensitive_data", lambda q: True)
    monkeypatch.setattr(tools, "web_search_duckduckgo",
                        lambda *a, **k: called.__setitem__("hit", True) or [])
    r = cap_web.search("STORE", params={"query": "totally benign query"})
    assert r.ok is False
    assert called["hit"] is False


def test_search_upstream_exception_maps_to_retryable_err(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(tools, "web_search_duckduckgo", boom)
    r = cap_web.search("STORE", params={"query": "feedling io"})
    assert r.ok is False
    assert r.error["retryable"] is True


def test_search_caps_oversized_result_list(monkeypatch):
    monkeypatch.setattr(
        tools, "web_search_duckduckgo",
        lambda *a, **k: [{"title": f"t{i}", "url": f"https://x/{i}", "snippet": "s"}
                          for i in range(1000)],
    )
    r = cap_web.search("STORE", params={"query": "feedling io"})
    assert r.ok is True
    assert len(r.data["results"]) <= 50


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, *, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def test_fetch_happy_path_strips_html_and_caps_size(monkeypatch):
    html_body = "<html><head><style>.x{}</style></head><body><p>Hello  World</p></body></html>"

    def fake_get(url, *, timeout, follow_redirects, headers):
        assert follow_redirects is True
        assert "User-Agent" in headers
        return _FakeResponse(status_code=200, text=html_body)

    monkeypatch.setattr(cap_web.httpx, "get", fake_get)
    r = cap_web.fetch("STORE", params={"url": "https://example.com/page"})
    assert r.ok is True
    assert r.data["url"] == "https://example.com/page"
    assert "Hello World" in r.data["text"]
    assert "<" not in r.data["text"]


def test_fetch_missing_url_is_invalid():
    r = cap_web.fetch("STORE", params={"url": ""})
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"


def test_fetch_invalid_url_scheme_is_invalid():
    r = cap_web.fetch("STORE", params={"url": "not-a-url"})
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"


def test_fetch_non_2xx_status_is_err(monkeypatch):
    monkeypatch.setattr(cap_web.httpx, "get",
                        lambda *a, **k: _FakeResponse(status_code=404, text="nope"))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/missing"})
    assert r.ok is False
    assert r.error["code"] == "capability_not_found"


def test_fetch_5xx_status_is_retryable(monkeypatch):
    monkeypatch.setattr(cap_web.httpx, "get",
                        lambda *a, **k: _FakeResponse(status_code=503, text="down"))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/down"})
    assert r.ok is False
    assert r.error["retryable"] is True


def test_fetch_exception_maps_to_retryable_err(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("dns failure")

    monkeypatch.setattr(cap_web.httpx, "get", boom)
    r = cap_web.fetch("STORE", params={"url": "https://example.com/"})
    assert r.ok is False
    assert r.error["retryable"] is True


def test_fetch_caps_response_body_before_stripping(monkeypatch):
    huge = "<p>" + ("a" * 200_000) + "</p>"
    monkeypatch.setattr(cap_web.httpx, "get",
                        lambda *a, **k: _FakeResponse(status_code=200, text=huge))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/huge"})
    assert r.ok is True
    # errors.cap_text further caps to MAX_TEXT (2000) — either way, nowhere near 200k.
    assert len(r.data["text"]) < 3000


# ---------------------------------------------------------------------------
# registry dispatch
# ---------------------------------------------------------------------------

def test_registry_dispatches_web_search(monkeypatch):
    from capabilities.types import ok
    monkeypatch.setattr(cap_web, "search", lambda store, **kw: ok({"results": []}))
    r = cap_registry.run_capability("web_search", "STORE", params={"query": "x"})
    assert r.ok is True and r.data == {"results": []}


def test_registry_dispatches_web_fetch(monkeypatch):
    from capabilities.types import ok
    monkeypatch.setattr(cap_web, "fetch", lambda store, **kw: ok({"url": "u", "text": "t"}))
    r = cap_registry.run_capability("web_fetch", "STORE", params={"url": "https://x"})
    assert r.ok is True and r.data == {"url": "u", "text": "t"}


def test_web_actions_are_read_actions():
    assert "web_search" in cap_registry.READ_ACTIONS
    assert "web_fetch" in cap_registry.READ_ACTIONS
    assert "web_search" not in cap_registry.WRITE_ACTIONS
    assert "web_fetch" not in cap_registry.WRITE_ACTIONS
