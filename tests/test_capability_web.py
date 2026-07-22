"""Web capability facade — keyless facade over model_api_runtime/tools.py's
DuckDuckGo HTML scrape (§merge-review condition 4b: V2 planner/executor need web
access parity with the legacy runtime). NO live network — everything monkeypatched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))  # noqa: E402

from model_api_runtime import tools  # noqa: E402
from capabilities import web as cap_web  # noqa: E402
from capabilities import registry as cap_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    monkeypatch.setattr(cap_web, "_resolve_ips", lambda host: ["93.184.216.34"])


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
    def __init__(self, *, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = dict(headers or {})
        self.encoding = "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_bytes(self):
        yield self.text.encode(self.encoding)


def test_fetch_happy_path_strips_html_and_caps_size(monkeypatch):
    html_body = "<html><head><style>.x{}</style></head><body><p>Hello  World</p></body></html>"

    def fake_get(url, *, resolved_ip, timeout, follow_redirects, headers):
        assert follow_redirects is False
        assert "User-Agent" in headers
        assert resolved_ip == "93.184.216.34"
        return _FakeResponse(status_code=200, text=html_body)

    monkeypatch.setattr(cap_web, "_stream_get", fake_get)
    r = cap_web.fetch("STORE", params={"url": "https://example.com/page"})
    assert r.ok is True
    assert r.data["url"] == "https://example.com/page"
    assert "Hello World" in r.data["text"]
    assert "<" not in r.data["text"]


def test_stream_get_pins_ip_preserves_host_and_disables_env_proxy(monkeypatch):
    captured = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, method, url, **kwargs):
            captured.update(method=method, url=url, request_kwargs=kwargs)
            return _FakeResponse(status_code=200, text="ok")

    monkeypatch.setattr(cap_web.httpx, "Client", _FakeClient)
    with cap_web._stream_get(
        "https://example.com:8443/path?q=1",
        resolved_ip="93.184.216.34",
        timeout=8.0,
        follow_redirects=False,
        headers={"User-Agent": "test"},
    ) as response:
        assert response.status_code == 200

    assert captured["client_kwargs"] == {"trust_env": False}
    assert captured["url"] == "https://93.184.216.34:8443/path?q=1"
    assert captured["request_kwargs"]["headers"]["Host"] == "example.com:8443"
    assert captured["request_kwargs"]["extensions"] == {"sni_hostname": "example.com"}


def test_fetch_uses_the_same_dns_answer_it_validated(monkeypatch):
    resolutions = {"n": 0}

    def resolve(_host):
        resolutions["n"] += 1
        # A rebinding resolver would return private on a second lookup. The
        # fetch path must resolve exactly once and pass the validated public IP
        # into the direct connection helper.
        return ["93.184.216.34"] if resolutions["n"] == 1 else ["127.0.0.1"]

    connected = []

    def fake_get(url, *, resolved_ip, **_kwargs):
        connected.append((url, resolved_ip))
        return _FakeResponse(status_code=200, text="safe")

    monkeypatch.setattr(cap_web, "_resolve_ips", resolve)
    monkeypatch.setattr(cap_web, "_stream_get", fake_get)

    result = cap_web.fetch("STORE", params={"url": "https://example.com/page"})

    assert result.ok is True
    assert resolutions["n"] == 1
    assert connected == [("https://example.com/page", "93.184.216.34")]


def test_fetch_missing_url_is_invalid():
    r = cap_web.fetch("STORE", params={"url": ""})
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"


def test_fetch_invalid_url_scheme_is_invalid():
    r = cap_web.fetch("STORE", params={"url": "not-a-url"})
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://10.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
])
def test_fetch_blocks_non_global_literal_addresses(url, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(cap_web, "_stream_get", lambda *a, **k: called.update(n=1))
    r = cap_web.fetch("STORE", params={"url": url})
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"
    assert called["n"] == 0


def test_fetch_blocks_mixed_public_private_dns(monkeypatch):
    monkeypatch.setattr(
        cap_web, "_resolve_ips", lambda host: ["93.184.216.34", "10.0.0.8"]
    )
    r = cap_web.fetch("STORE", params={"url": "https://example.com/"})
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"


def test_fetch_revalidates_public_to_private_redirect(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse(status_code=302, headers={"location": "http://127.0.0.1/admin"})

    monkeypatch.setattr(cap_web, "_stream_get", fake_get)
    r = cap_web.fetch("STORE", params={"url": "https://example.com/start"})
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"
    assert calls == ["https://example.com/start"]


def test_fetch_caps_redirect_count(monkeypatch):
    monkeypatch.setattr(
        cap_web,
        "_stream_get",
        lambda url, **kwargs: _FakeResponse(status_code=302, headers={"location": "/again"}),
    )
    r = cap_web.fetch("STORE", params={"url": "https://example.com/start"})
    assert r.ok is False
    assert "redirect" in r.error["message"]


def test_fetch_non_2xx_status_is_err(monkeypatch):
    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _FakeResponse(status_code=404, text="nope"))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/missing"})
    assert r.ok is False
    assert r.error["code"] == "capability_not_found"


def test_fetch_5xx_status_is_retryable(monkeypatch):
    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _FakeResponse(status_code=503, text="down"))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/down"})
    assert r.ok is False
    assert r.error["retryable"] is True


def test_fetch_exception_maps_to_retryable_err(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("dns failure")

    monkeypatch.setattr(cap_web, "_stream_get", boom)
    r = cap_web.fetch("STORE", params={"url": "https://example.com/"})
    assert r.ok is False
    assert r.error["retryable"] is True


def test_fetch_truncates_an_oversized_body_instead_of_discarding_it(monkeypatch):
    """The regression this pins: an over-cap page used to come back as an error.

    That made the tool useless on the real web — Wikipedia is 360 KB, a weather
    page 86 KB — and the model, seeing only failures, would tell the user it has
    no web access at all. Reading the first N bytes bounds what we pull from an
    untrusted host just as well, and actually answers the question.
    """
    lead = "<p>the part that matters</p>"
    huge = lead + "<p>" + ("a" * 400_000) + "</p>"
    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _FakeResponse(status_code=200, text=huge))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/huge"})
    assert r.ok is True
    assert "the part that matters" in r.data["text"]
    assert r.data["truncated"] is True


def test_fetch_reports_a_whole_page_as_not_truncated(monkeypatch):
    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _FakeResponse(status_code=200, text="<p>hi</p>"))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/small"})
    assert r.ok is True
    assert r.data["truncated"] is False


def test_fetch_stops_pulling_chunks_once_the_cap_is_reached(monkeypatch):
    """Deliberately NOT "never reads past the cap".

    httpx yields a whole decoded chunk before we can slice it, and a compressed
    response can expand inside its decoder first, so this is a bound on what we
    retain and on how far we keep iterating — not a hard bandwidth guarantee.
    Naming it the stronger thing would be a claim the code cannot back.
    """
    seen = {"chunks": 0}

    class _Endless(_FakeResponse):
        def iter_bytes(self):
            for _ in range(1000):
                seen["chunks"] += 1
                yield b"x" * 10_000

    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _Endless(status_code=200, text=""))
    cap_web.fetch("STORE", params={"url": "https://example.com/endless"})
    assert seen["chunks"] <= cap_web._FETCH_MAX_BODY_BYTES // 10_000 + 1


def test_a_single_chunk_larger_than_the_cap_is_sliced(monkeypatch):
    class _OneBigChunk(_FakeResponse):
        def iter_bytes(self):
            yield b"<p>lead</p>" + b"x" * (cap_web._FETCH_MAX_BODY_BYTES * 3)

    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _OneBigChunk(status_code=200, text=""))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/onebig"})
    assert r.ok is True
    assert r.data["truncated"] is True


def test_a_body_that_exactly_fills_the_cap_is_not_reported_as_truncated(monkeypatch):
    """Off-by-one: nothing was dropped, so claiming truncation would send the
    model looking for a rest of the page that does not exist."""
    class _Exact(_FakeResponse):
        def iter_bytes(self):
            yield b"<p>all of it</p>".ljust(cap_web._FETCH_MAX_BODY_BYTES, b" ")

    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _Exact(status_code=200, text=""))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/exact"})
    assert r.ok is True
    assert r.data["truncated"] is False


def test_truncation_inside_a_script_does_not_leak_javascript_as_text(monkeypatch):
    """The cut lands mid-script on plenty of real pages. `_strip_html_text`
    needs a closing tag to remove the block, so without this the whole minified
    JS body would be handed to the model as page content."""
    js = "var a='" + ("JUNK" * 40_000) + "';"
    html = "<p>the article body</p><script>" + js

    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _FakeResponse(status_code=200, text=html))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/cut-in-script"})
    assert r.ok is True
    assert "the article body" in r.data["text"]
    assert "JUNK" not in r.data["text"]
    assert "var a=" not in r.data["text"]


def test_truncated_is_true_when_only_the_text_cap_bit(monkeypatch):
    """The body fit; the stripped text did not. Content still went missing, so
    reporting `truncated: false` here would be a lie to the model."""
    html = "<p>" + ("word " * 5_000) + "</p>"
    assert len(html.encode()) < cap_web._FETCH_MAX_BODY_BYTES
    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _FakeResponse(status_code=200, text=html))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/long-text"})
    assert r.ok is True
    assert r.data["truncated"] is True


def test_a_lying_content_length_does_not_skip_the_page(monkeypatch):
    """Content-Length is attacker-controlled and often wrong; it used to be
    enough on its own to refuse a page that was in fact small."""
    response = _FakeResponse(
        status_code=200,
        text="<p>small after all</p>",
        headers={"content-length": str(cap_web._FETCH_MAX_BODY_BYTES * 10)},
    )
    monkeypatch.setattr(cap_web, "_stream_get", lambda *a, **k: response)
    r = cap_web.fetch("STORE", params={"url": "https://example.com/liar"})
    assert r.ok is True
    assert "small after all" in r.data["text"]


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


def test_truncated_is_delivered_before_the_text_so_it_survives_the_result_cap(monkeypatch):
    """Key order is load-bearing, not cosmetic.

    The dict is json-dumped and hard-cut at executor._RESULT_CHAR_CAP (2000)
    before it reaches the model. With `truncated` last it was cut off every
    single time on exactly the pages where it mattered — the long ones.
    """
    import json as _json

    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _FakeResponse(
                            status_code=200, text="<p>" + ("word " * 5_000) + "</p>"))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/long"})
    assert list(r.data)[0] == "truncated"
    assert "truncated" in _json.dumps(r.data, ensure_ascii=False)[:2000]
