"""Web capability facade — keyless facade over model_api_runtime/tools.py's
DuckDuckGo HTML scrape (§merge-review condition 4b: V2 planner/executor need web
access parity with the legacy runtime). NO live network — everything monkeypatched.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))  # noqa: E402

from model_api_runtime import tools  # noqa: E402
from capabilities import web as cap_web  # noqa: E402
from capabilities import registry as cap_registry  # noqa: E402
from model_api_runtime.v2 import pool_config  # noqa: E402


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
    assert r.data["source_truncated"] is True


def test_fetch_reports_a_whole_page_as_not_truncated(monkeypatch):
    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _FakeResponse(status_code=200, text="<p>hi</p>"))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/small"})
    assert r.ok is True
    assert r.data["source_truncated"] is False


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
    assert r.data["source_truncated"] is True


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
    assert r.data["source_truncated"] is False


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


def test_long_extracted_text_reports_paging_separately_from_source_cut(monkeypatch):
    """The body fit, so source_truncated stays false; paging metadata separately
    reports that the current result contains only the first retained slice."""
    html = "<p>" + ("word " * 5_000) + "</p>"
    assert len(html.encode()) < cap_web._FETCH_MAX_BODY_BYTES
    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _FakeResponse(status_code=200, text=html))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/long-text"})
    assert r.ok is True
    assert r.data["source_truncated"] is False
    assert r.data["has_more"] is True
    assert r.data["next_offset"] == r.data["returned_chars"]


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


def test_paged_payload_structurally_fits_the_literal_atomic_result_cap(monkeypatch):
    """Independent 8000 anchor: production constants cannot rise while this
    test follows them and turns a real prompt-budget regression falsely green."""
    import json as _json

    monkeypatch.setattr(cap_web, "_stream_get",
                        lambda *a, **k: _FakeResponse(
                            status_code=200, text="<p>" + ("word " * 5_000) + "</p>"))
    r = cap_web.fetch("STORE", params={"url": "https://example.com/long"})
    rendered = _json.dumps(r.data, ensure_ascii=False)
    assert 7000 < len(rendered) <= 8000
    assert r.data["has_more"] is True
    assert r.data["returned_chars"] == len(r.data["text"])


def test_same_attempt_two_offsets_make_one_outbound_request(monkeypatch):
    calls = {"n": 0}
    body = "<p>" + ("0123456789" * 2500) + "</p>"

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        return _FakeResponse(status_code=200, text=body)

    monkeypatch.setattr(cap_web, "_stream_get", fake_get)
    session = cap_web.WebFetchSession()
    first = cap_web.fetch(
        "STORE", params={"url": "https://example.com/paged"},
        web_fetch_session=session,
    )
    second = cap_web.fetch(
        "STORE",
        params={
            "url": "https://example.com/paged",
            "offset": first.data["next_offset"],
        },
        web_fetch_session=session,
    )

    assert first.ok is second.ok is True
    assert calls["n"] == 1
    assert second.data["offset"] == first.data["next_offset"]
    assert second.data["text"] != first.data["text"]
    assert second.data["total_chars"] == first.data["total_chars"]


def test_offset_cache_miss_is_stable_and_never_refetches(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(
        cap_web,
        "_stream_get",
        lambda *args, **kwargs: calls.__setitem__("n", calls["n"] + 1),
    )
    session = cap_web.WebFetchSession()

    first = cap_web.fetch(
        "STORE",
        params={"url": "https://example.com/miss", "offset": 10},
        web_fetch_session=session,
    )
    second = cap_web.fetch(
        "STORE",
        params={"url": "https://example.com/miss", "offset": 10},
        web_fetch_session=session,
    )

    assert first.to_dict() == second.to_dict()
    assert first.error["code"] == "capability_invalid_input"
    assert calls["n"] == 0


@pytest.mark.parametrize("offset", [-1, 1.5, "10", True, None])
def test_invalid_offset_shape_is_rejected_before_network(monkeypatch, offset):
    calls = {"n": 0}
    monkeypatch.setattr(
        cap_web,
        "_stream_get",
        lambda *args, **kwargs: calls.__setitem__("n", calls["n"] + 1),
    )

    result = cap_web.fetch(
        "STORE",
        params={"url": "https://example.com/invalid-offset", "offset": offset},
    )

    assert result.error["code"] == "capability_invalid_input"
    assert calls["n"] == 0


def test_stateless_fetch_rejects_continuation_without_refetching(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(
        cap_web,
        "_stream_get",
        lambda *args, **kwargs: calls.__setitem__("n", calls["n"] + 1),
    )

    result = cap_web.fetch(
        "STORE",
        params={"url": "https://example.com/stateless", "offset": 1},
    )

    assert result.error["code"] == "capability_invalid_input"
    assert calls["n"] == 0


def test_offset_beyond_retained_text_is_invalid_without_refetch(monkeypatch):
    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        return _FakeResponse(status_code=200, text="<p>short</p>")

    monkeypatch.setattr(cap_web, "_stream_get", fake_get)
    session = cap_web.WebFetchSession()
    first = cap_web.fetch(
        "STORE",
        params={"url": "https://example.com/short"},
        web_fetch_session=session,
    )
    result = cap_web.fetch(
        "STORE",
        params={
            "url": "https://example.com/short",
            "offset": first.data["total_chars"] + 1,
        },
        web_fetch_session=session,
    )

    assert result.error["code"] == "capability_invalid_input"
    assert calls["n"] == 1


def test_separate_attempt_session_refetches_the_same_url(monkeypatch):
    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        return _FakeResponse(status_code=200, text="<p>fresh each attempt</p>")

    monkeypatch.setattr(cap_web, "_stream_get", fake_get)
    for _ in range(2):
        result = cap_web.fetch(
            "STORE",
            params={"url": "https://example.com/fresh"},
            web_fetch_session=cap_web.WebFetchSession(),
        )
        assert result.ok is True
    assert calls["n"] == 2


def test_continuation_waits_only_after_zero_offset_loader_has_started(monkeypatch):
    calls = {"n": 0}
    url = "https://example.com/concurrent"
    loader_started = threading.Event()
    release_loader = threading.Event()

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        loader_started.set()
        assert release_loader.wait(timeout=2)
        return _FakeResponse(status_code=200, text="<p>" + ("abcdef" * 3000) + "</p>")

    monkeypatch.setattr(cap_web, "_stream_get", fake_get)
    session = cap_web.WebFetchSession()
    session.prepare_batch([
        SimpleNamespace(name="web_fetch", args={"url": url, "offset": 0}),
        SimpleNamespace(name="web_fetch", args={"url": url, "offset": 100}),
    ])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            cap_web.fetch,
            "STORE",
            params={"url": url},
            web_fetch_session=session,
        )
        assert loader_started.wait(timeout=2)
        later = pool.submit(
            cap_web.fetch,
            "STORE",
            params={"url": url, "offset": 100},
            web_fetch_session=session,
        )
        release_loader.set()
        first_result = first.result(timeout=2)
        later_result = later.result(timeout=2)

    assert first_result.ok is later_result.ok is True
    assert calls["n"] == 1
    assert later_result.data["offset"] == 100


def test_planned_continuation_does_not_wait_for_an_owner_that_has_not_started(
    monkeypatch,
):
    url = "https://example.com/planned"
    session = cap_web.WebFetchSession()
    session.prepare_batch([
        SimpleNamespace(name="web_fetch", args={"url": url, "offset": 0}),
    ])
    monkeypatch.setattr(
        session._condition,
        "wait",
        lambda **_kwargs: pytest.fail("planned continuation entered Condition.wait"),
    )

    started_at = time.monotonic()
    result = session.document(
        url,
        offset=10,
        loader=lambda: pytest.fail("a continuation must never become the loader"),
    )
    elapsed = time.monotonic() - started_at

    assert result.error["code"] == "capability_upstream_error"
    assert result.error["retryable"] is True
    assert elapsed < 1.0


def test_session_wait_timeout_is_below_every_shared_broker_stall_budget():
    runtime = pool_config.RuntimePoolConfig.from_env()
    smallest_stall_budget = min(slot.stall_budget_sec for slot in runtime.slots)

    assert 0 < cap_web._FETCH_SESSION_WAIT_TIMEOUT_SEC < smallest_stall_budget


@pytest.mark.parametrize("offset", [-1, 1.5, "10", True, None])
def test_session_document_rejects_invalid_offset_without_trusting_fetch(offset):
    result = cap_web.WebFetchSession().document(
        "https://example.com/direct",
        offset=offset,
        loader=lambda: pytest.fail("invalid offset reached the loader"),
    )

    assert result.error["code"] == "capability_invalid_input"
    assert result.error["retryable"] is False


def test_session_wait_deadline_returns_retryable_error(monkeypatch):
    url = "https://example.com/slow"
    loader_started = threading.Event()
    release_loader = threading.Event()
    session = cap_web.WebFetchSession()
    monkeypatch.setattr(cap_web, "_FETCH_SESSION_WAIT_TIMEOUT_SEC", 0.01)

    def _loader():
        loader_started.set()
        assert release_loader.wait(timeout=2)
        return cap_web._FetchDocument(url=url, text="complete", source_truncated=False)

    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(session.document, url, offset=0, loader=_loader)
        assert loader_started.wait(timeout=2)
        timed_out = session.document(
            url,
            offset=1,
            loader=lambda: pytest.fail("continuation became owner"),
        )
        release_loader.set()
        assert owner.result(timeout=2).text == "complete"

    assert timed_out.error["code"] == "capability_upstream_error"
    assert timed_out.error["retryable"] is True


def test_redirect_final_url_alias_reuses_retained_document(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/start"):
            return _FakeResponse(status_code=302, headers={"location": "/final"})
        return _FakeResponse(status_code=200, text="<p>" + ("alias" * 3000) + "</p>")

    monkeypatch.setattr(cap_web, "_stream_get", fake_get)
    session = cap_web.WebFetchSession()
    first = cap_web.fetch(
        "STORE", params={"url": "https://example.com/start"},
        web_fetch_session=session,
    )
    continued = cap_web.fetch(
        "STORE",
        params={"url": "https://example.com/final", "offset": 100},
        web_fetch_session=session,
    )

    assert first.ok is continued.ok is True
    assert calls == ["https://example.com/start", "https://example.com/final"]


def test_session_capacity_rejects_new_url_without_evicting_old(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse(status_code=200, text="<p>" + (url[-1] * 200) + "</p>")

    monkeypatch.setattr(cap_web, "_stream_get", fake_get)
    session = cap_web.WebFetchSession(max_urls=1, max_chars=10_000)
    first = cap_web.fetch(
        "STORE", params={"url": "https://example.com/a"},
        web_fetch_session=session,
    )
    rejected = cap_web.fetch(
        "STORE", params={"url": "https://example.com/b"},
        web_fetch_session=session,
    )
    continued = cap_web.fetch(
        "STORE", params={"url": "https://example.com/a", "offset": 10},
        web_fetch_session=session,
    )

    assert first.ok is continued.ok is True
    assert rejected.error["code"] == "capability_unavailable"
    assert calls == ["https://example.com/a"]


def test_session_total_character_cap_rejects_retention_honestly(monkeypatch):
    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        return _FakeResponse(status_code=200, text="<p>too large to retain</p>")

    monkeypatch.setattr(cap_web, "_stream_get", fake_get)
    result = cap_web.fetch(
        "STORE",
        params={"url": "https://example.com/large"},
        web_fetch_session=cap_web.WebFetchSession(max_urls=8, max_chars=5),
    )

    assert result.ok is False
    assert result.error["code"] == "capability_unavailable"
    assert result.error["retryable"] is False
    assert calls["n"] == 1
