"""Pure-unit tests for the provider model-catalog wire helpers.

fake-httpx + pure functions only; NO DB, NO real network. Registered in
``tests/conftest.py`` ``_PURE_UNIT`` so it still collects without Postgres.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc  # noqa: E402


# --------------------------------------------------------------------------- #
# Task 2 (new): validate_catalog_target — provider allowlist + URL scheme
# --------------------------------------------------------------------------- #

def test_validate_catalog_target_fills_default_base():
    provider, base = pc.validate_catalog_target("anthropic", "")
    assert provider == "anthropic"
    assert base == "https://api.anthropic.com/v1"


def test_validate_catalog_target_unknown_provider_raises():
    with pytest.raises(pc.ProviderError):
        pc.validate_catalog_target("totally-not-a-provider", "")


def test_validate_catalog_target_requires_base_for_compatible():
    with pytest.raises(pc.ProviderError):
        pc.validate_catalog_target("openai_compatible", "")


def test_validate_catalog_target_rejects_bad_scheme():
    with pytest.raises(pc.ProviderError):
        pc.validate_catalog_target("openai_compatible", "ftp://evil.example.com")
    # localhost http is allowed; arbitrary http is not.
    with pytest.raises(pc.ProviderError):
        pc.validate_catalog_target("openai_compatible", "http://evil.example.com/v1")
    provider, base = pc.validate_catalog_target(
        "openai_compatible", "http://127.0.0.1:8080/v1")
    assert base == "http://127.0.0.1:8080/v1"


# --------------------------------------------------------------------------- #
# Task 1: request construction + page parsing (pure)
# --------------------------------------------------------------------------- #

def test_catalog_request_openai_compatible_bearer():
    url, headers, params = pc._catalog_request(
        "openai_compatible", "sk-x", "https://api.example.com/v1", None)
    assert url == "https://api.example.com/v1/models"
    assert headers["Authorization"] == "Bearer sk-x"
    assert params == {}


def test_catalog_request_openrouter_asks_all_modalities():
    url, headers, params = pc._catalog_request(
        "openrouter", "sk-or", "", None)
    assert url.endswith("/models")
    assert headers["Authorization"] == "Bearer sk-or"
    assert params.get("output_modalities") == "all"


def test_catalog_request_gemini_key_in_header_not_query():
    url, headers, params = pc._catalog_request(
        "gemini", "AIza-x", "https://generativelanguage.googleapis.com/v1beta", None)
    assert url.endswith("/models")
    assert headers["x-goog-api-key"] == "AIza-x"
    assert "AIza-x" not in url and "key" not in params  # 密钥不进 URL


def test_catalog_request_anthropic_headers_and_cursor():
    url, headers, params = pc._catalog_request(
        "anthropic", "sk-ant", "https://api.anthropic.com/v1", "msg_123")
    assert headers["x-api-key"] == "sk-ant"
    assert headers["anthropic-version"] == "2023-06-01"
    assert params.get("after_id") == "msg_123"


def test_parse_catalog_page_openai_data_shape():
    body = {"data": [{"id": "gpt-5.4", "name": "GPT-5.4"}, {"id": "o5"}]}
    models, nxt = pc._parse_catalog_page("openai", body)
    assert models == [{"id": "gpt-5.4", "display_name": "GPT-5.4"},
                      {"id": "o5", "display_name": "o5"}]
    assert nxt is None


def test_parse_catalog_page_gemini_strips_prefix_and_paginates():
    body = {"models": [{"name": "models/gemini-3.1-pro", "displayName": "Gemini 3.1 Pro"}],
            "nextPageToken": "tok2"}
    models, nxt = pc._parse_catalog_page("gemini", body)
    assert models == [{"id": "gemini-3.1-pro", "display_name": "Gemini 3.1 Pro"}]
    assert nxt == "tok2"


def test_parse_catalog_page_anthropic_has_more():
    body = {"data": [{"id": "claude-opus-5", "display_name": "Claude Opus 5"}],
            "has_more": True, "last_id": "claude-opus-5"}
    models, nxt = pc._parse_catalog_page("anthropic", body)
    assert models == [{"id": "claude-opus-5", "display_name": "Claude Opus 5"}]
    assert nxt == "claude-opus-5"


def test_catalog_request_bedrock_unsupported():
    with pytest.raises(pc.ProviderError) as ei:
        pc._catalog_request("bedrock", "k", "", None)
    assert "model_catalog_unsupported" in str(ei.value)


# --- strict parser: valid-empty vs invalid vs junk members ------------------ #

def test_parse_catalog_page_present_empty_array_is_legit():
    models, nxt = pc._parse_catalog_page("openai", {"data": []})
    assert models == [] and nxt is None


def test_parse_catalog_page_missing_array_is_invalid():
    for bad in ({}, {"error": {"message": "nope"}}, {"data": "not-a-list"}):
        with pytest.raises(pc.ProviderError) as ei:
            pc._parse_catalog_page("openai", bad)
        assert "model_catalog_invalid_response" in str(ei.value)


def test_parse_catalog_page_non_dict_root_is_invalid():
    with pytest.raises(pc.ProviderError):
        pc._parse_catalog_page("openai", ["not", "a", "dict"])


def test_parse_catalog_page_skips_non_object_and_bad_id_members():
    body = {"data": [
        "junk-string",
        {"id": 123},                 # non-str id
        {"id": ""},                  # empty id
        {"id": "x" * 200},           # over length cap
        {"id": "keep", "name": "Keep"},
    ]}
    models, nxt = pc._parse_catalog_page("openai", body)
    assert models == [{"id": "keep", "display_name": "Keep"}]
    assert nxt is None


def test_parse_catalog_page_anthropic_has_more_missing_last_id_invalid():
    body = {"data": [{"id": "a"}], "has_more": True}  # no last_id
    with pytest.raises(pc.ProviderError) as ei:
        pc._parse_catalog_page("anthropic", body)
    assert "model_catalog_invalid_response" in str(ei.value)


# --------------------------------------------------------------------------- #
# Task 2: list_provider_models network orchestration (fake streaming httpx)
# --------------------------------------------------------------------------- #

class _FakeStream:
    def __init__(self, status, raw: bytes):
        self.status_code = status
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self):
        # Chunk it so the byte-cap accounting is exercised across iterations.
        for i in range(0, len(self._raw), 4096):
            yield self._raw[i:i + 4096]


def _install_fake_stream(monkeypatch, pages):
    """pages: list of (status, body[, raw_bytes]) returned in order.

    Fakes ``httpx.Client.stream`` (a context manager exposing ``status_code``
    and ``iter_bytes``). ``_shared_client`` reset so ``_http_client()`` rebuilds
    the faked client.
    """
    seq = list(pages)
    calls = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def stream(self, method, url, *, headers=None, params=None, timeout=None):
            calls.append({"url": url, "params": params or {}, "timeout": timeout})
            page = seq.pop(0)
            status, body = page[0], page[1]
            raw = page[2] if len(page) > 2 else json.dumps(body).encode()
            return _FakeStream(status, raw)

    monkeypatch.setattr(pc.httpx, "Client", FakeClient)
    monkeypatch.setattr(pc, "_shared_client", None)
    return calls


def test_list_models_openrouter_single_page(monkeypatch):
    calls = _install_fake_stream(monkeypatch, [(200, {"data": [{"id": "a"}, {"id": "b"}]})])
    res = pc.list_provider_models("openrouter", "k", "")
    assert res["catalog_supported"] is True and res["complete"] is True
    assert [m["id"] for m in res["models"]] == ["a", "b"]
    assert calls[0]["params"].get("output_modalities") == "all"


def test_list_models_per_phase_timeouts_are_tight_and_bounded(monkeypatch):
    calls = _install_fake_stream(monkeypatch, [(200, {"data": [{"id": "a"}]})])
    pc.list_provider_models("openai", "k", "")
    # Per-phase httpx.Timeout, NOT a single remaining-budget float: connect/read
    # are each capped by their own tight constant (and by remaining budget), so a
    # slow connect/header phase can't itself outlast the wall-clock budget.
    t = calls[0]["timeout"]
    assert isinstance(t, pc.httpx.Timeout)
    assert 0 < t.connect <= pc._CATALOG_CONNECT_TIMEOUT
    assert 0 < t.read <= pc._CATALOG_READ_TIMEOUT


class _DripStream:
    """A stream that keeps emitting small chunks with real sleeps between them,
    for far longer than the wall-clock budget — the malicious slow-drip case."""

    def __init__(self, status, chunk, n, sleep):
        self.status_code = status
        self._chunk = chunk
        self._n = n
        self._sleep = sleep

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self):
        for _ in range(self._n):
            time.sleep(self._sleep)
            yield self._chunk


def test_list_models_slow_drip_past_budget_is_bounded_partial(monkeypatch):
    # Regression for the "total-time bound is not real" critical: page 1 returns
    # instantly with a model; page 2 slow-drips small chunks well past the budget.
    # The in-loop deadline check must stop reading around the budget and return
    # a PARTIAL result (models already collected) — never complete:true, never
    # run for the full drip duration.
    monkeypatch.setattr(pc, "_CATALOG_TOTAL_BUDGET", 0.15)

    page1 = json.dumps({"data": [{"id": "a"}], "has_more": True,
                        "last_id": "a"}).encode()
    seq = [(200, page1)]

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def stream(self, method, url, *, headers=None, params=None, timeout=None):
            if seq:
                status, raw = seq.pop(0)
                return _FakeStream(status, raw)
            # page 2+: drip ~1.5s worth of chunks, far past the 0.15s budget.
            return _DripStream(200, b'{"id":"x"},', n=50, sleep=0.03)

    monkeypatch.setattr(pc.httpx, "Client", FakeClient)
    monkeypatch.setattr(pc, "_shared_client", None)

    started = time.monotonic()
    res = pc.list_provider_models("anthropic", "k", "")
    elapsed = time.monotonic() - started

    assert res["complete"] is False              # NOT falsely complete
    assert res["catalog_supported"] is True
    assert [m["id"] for m in res["models"]] == ["a"]   # page-1 models kept
    # Bounded near the budget, not the full ~1.5s drip. Generous ceiling to stay
    # non-flaky on a loaded CI box while still proving we didn't drip to the end.
    assert elapsed < 1.0, elapsed


def test_list_models_anthropic_paginates_and_dedupes(monkeypatch):
    calls = _install_fake_stream(monkeypatch, [
        (200, {"data": [{"id": "x"}], "has_more": True, "last_id": "x"}),
        (200, {"data": [{"id": "x"}, {"id": "y"}], "has_more": False}),
    ])
    res = pc.list_provider_models("anthropic", "k", "")
    assert [m["id"] for m in res["models"]] == ["x", "y"]   # 去重、稳定序
    assert res["complete"] is True
    assert calls[1]["params"].get("after_id") == "x"        # 第二页带 cursor


def test_list_models_bedrock_unsupported(monkeypatch):
    res = pc.list_provider_models("bedrock", "k", "")
    assert res["catalog_supported"] is False and res["models"] == []


def test_list_models_page_cap_marks_incomplete(monkeypatch):
    # 永远 has_more 的死循环，应在 _CATALOG_MAX_PAGES 处停下并 complete=False
    monkeypatch.setattr(pc, "_CATALOG_MAX_PAGES", 3)
    pages = [(200, {"data": [{"id": f"m{i}"}], "has_more": True, "last_id": f"m{i}"})
             for i in range(10)]
    _install_fake_stream(monkeypatch, pages)
    res = pc.list_provider_models("anthropic", "k", "")
    assert res["complete"] is False
    assert any("truncated" in w for w in res["warnings"])


def test_list_models_repeated_cursor_stops(monkeypatch):
    # Same last_id every page → without cursor-dedup this loops; must stop early.
    pages = [(200, {"data": [{"id": f"m{i}"}], "has_more": True, "last_id": "SAME"})
             for i in range(5)]
    calls = _install_fake_stream(monkeypatch, pages)
    res = pc.list_provider_models("anthropic", "k", "")
    assert res["complete"] is False
    assert any("repeated pagination cursor" in w for w in res["warnings"])
    # page0 (no cursor) + page1 (cursor SAME) then repeat detected → 2 fetches.
    assert len(calls) == 2


def test_list_models_later_page_failure_is_partial(monkeypatch):
    _install_fake_stream(monkeypatch, [
        (200, {"data": [{"id": "a"}], "has_more": True, "last_id": "a"}),
        (503, {"error": "upstream"}),
    ])
    res = pc.list_provider_models("anthropic", "k", "")
    assert [m["id"] for m in res["models"]] == ["a"]   # already-collected kept
    assert res["complete"] is False
    assert res["catalog_supported"] is True


def test_list_models_first_page_failure_raises(monkeypatch):
    _install_fake_stream(monkeypatch, [(401, {"error": "bad key"})])
    with pytest.raises(pc.ProviderError) as ei:
        pc.list_provider_models("openai", "k", "")
    assert ei.value.status_code == 401


def test_list_models_compatible_404_is_unsupported_not_error(monkeypatch):
    _install_fake_stream(monkeypatch, [(404, {"error": "no such route"})])
    res = pc.list_provider_models("openai_compatible", "k", "https://x.example.com/v1")
    assert res["catalog_supported"] is False and res["models"] == []


def test_list_models_body_size_cap_first_page_raises(monkeypatch):
    huge = b'{"data":[' + b'{"id":"x"},' * 500000 + b'{"id":"y"}]}'
    assert len(huge) > pc._CATALOG_MAX_BODY_BYTES
    _install_fake_stream(monkeypatch, [(200, None, huge)])
    with pytest.raises(pc.ProviderError) as ei:
        pc.list_provider_models("openai", "k", "")
    assert "model_catalog_invalid_response" in str(ei.value)


def test_list_models_non_json_first_page_raises(monkeypatch):
    _install_fake_stream(monkeypatch, [(200, None, b"<html>not json</html>")])
    with pytest.raises(pc.ProviderError) as ei:
        pc.list_provider_models("openai", "k", "")
    assert "model_catalog_invalid_response" in str(ei.value)


def test_list_models_present_empty_array_is_success(monkeypatch):
    _install_fake_stream(monkeypatch, [(200, {"data": []})])
    res = pc.list_provider_models("openai", "k", "")
    assert res["models"] == [] and res["complete"] is True
    assert res["catalog_supported"] is True


# --------------------------------------------------------------------------- #
# Task 3: ProviderError -> slug classification
# --------------------------------------------------------------------------- #

def test_error_slug_mapping():
    def mk(msg, sc=None):
        return pc.ProviderError(msg, status_code=sc)
    assert pc.model_catalog_error_slug(mk("provider_http_401", 401)) == "model_catalog_auth_failed"
    assert pc.model_catalog_error_slug(mk("provider_http_403", 403)) == "model_catalog_access_denied"
    assert pc.model_catalog_error_slug(mk("provider_http_402", 402)) == "model_catalog_access_denied"
    assert pc.model_catalog_error_slug(mk("provider_http_451", 451)) == "model_catalog_access_denied"
    assert pc.model_catalog_error_slug(mk("provider_http_429", 429)) == "model_catalog_rate_limited"
    assert pc.model_catalog_error_slug(mk("x", 503)) == "model_catalog_temporarily_unavailable"
    assert pc.model_catalog_error_slug(mk("x", 500)) == "model_catalog_temporarily_unavailable"
    assert pc.model_catalog_error_slug(mk("x", 501)) == "model_catalog_temporarily_unavailable"
    assert pc.model_catalog_error_slug(mk("x", 408)) == "model_catalog_temporarily_unavailable"
    assert pc.model_catalog_error_slug(mk("x", 425)) == "model_catalog_temporarily_unavailable"
    assert pc.model_catalog_error_slug(mk("provider network error: ReadTimeout")) == "model_catalog_temporarily_unavailable"
    # remaining 4xx must NOT masquerade as auth failure
    assert pc.model_catalog_error_slug(mk("provider_http_400", 400)) == "model_catalog_invalid_response"
    assert pc.model_catalog_error_slug(mk("provider_http_404", 404)) == "model_catalog_invalid_response"
    assert pc.model_catalog_error_slug(mk("provider_http_422", 422)) == "model_catalog_invalid_response"
    assert pc.model_catalog_error_slug(mk("model_catalog_unsupported")) == "model_catalog_unsupported"
    assert pc.model_catalog_error_slug(mk("model_catalog_invalid_response")) == "model_catalog_invalid_response"
