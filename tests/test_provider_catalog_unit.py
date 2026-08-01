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


def test_validate_catalog_target_rejects_loopback_prefix_forgery():
    # Real host is evil.example — a startswith("http://127.0.0.1") check accepts
    # both of these; urlsplit-based validation must reject them so the provider
    # key is never sent in plaintext to an attacker-controlled host.
    for bad in (
        "http://127.0.0.1.evil.example/v1",   # subdomain trick
        "http://127.0.0.1@evil.example/v1",   # userinfo trick
    ):
        with pytest.raises(pc.ProviderError):
            pc.validate_catalog_target("openai_compatible", bad)


def test_validate_catalog_target_allows_localhost_and_forbids_https_userinfo():
    _, base = pc.validate_catalog_target("openai_compatible", "http://localhost:1234/v1")
    assert base == "http://localhost:1234/v1"
    # Userinfo is forbidden regardless of scheme.
    with pytest.raises(pc.ProviderError):
        pc.validate_catalog_target("openai_compatible", "https://user:pw@api.example.com/v1")


# --------------------------------------------------------------------------- #
# Task 1: request construction + page parsing (pure)
# --------------------------------------------------------------------------- #

def test_catalog_request_openai_compatible_bearer():
    url, headers, params = pc._catalog_request(
        "openai_compatible", "sk-x", "https://api.example.com/v1", None)
    assert url == "https://api.example.com/v1/models"
    assert headers["Authorization"] == "Bearer sk-x"
    assert params == {}


def test_catalog_request_openrouter_uses_unmodified_user_catalog():
    url, headers, params = pc._catalog_request(
        "openrouter", "sk-or", "", None)
    assert url.endswith("/models/user")
    assert headers["Authorization"] == "Bearer sk-or"
    assert params == {}


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
    assert models == [{"id": "gpt-5.4", "display_name": "GPT-5.4",
                       "input_modalities": ["text", "image"]},
                      {"id": "o5", "display_name": "o5"}]
    assert nxt is None


def test_parse_catalog_page_openai_uses_official_vision_capability_table():
    body = {
        "data": [
            {"id": "gpt-4.1", "owned_by": "openai"},
            {"id": "gpt-4.1-mini-2025-04-14", "owned_by": "openai"},
            {"id": "gpt-4o", "owned_by": "openai"},
            {"id": "gpt-5-mini", "owned_by": "openai"},
        ]
    }
    models, _ = pc._parse_catalog_page("openai", body)
    assert [model["input_modalities"] for model in models] == [
        ["text", "image"],
        ["text", "image"],
        ["text", "image"],
        ["text", "image"],
    ]


def test_parse_catalog_page_openai_covers_current_reasoning_and_gpt_families():
    body = {
        "data": [
            {"id": "o1", "owned_by": "openai"},
            {"id": "o1-pro-2025-03-19", "owned_by": "openai"},
            {"id": "o3", "owned_by": "openai"},
            {"id": "o3-pro-2025-06-10", "owned_by": "openai"},
            {"id": "o4-mini", "owned_by": "openai"},
            {"id": "gpt-5.1-codex-max", "owned_by": "openai"},
            {"id": "gpt-5.2-pro", "owned_by": "openai"},
            {"id": "gpt-5.4-mini-2026-03-17", "owned_by": "openai"},
            {"id": "gpt-5.5-pro-2026-04-23", "owned_by": "openai"},
            {"id": "gpt-5.6-sol", "owned_by": "openai"},
            {"id": "chat-latest", "owned_by": "openai"},
        ]
    }

    models, _ = pc._parse_catalog_page("openai", body)

    assert all(model["input_modalities"] == ["text", "image"] for model in models)


def test_parse_catalog_page_openai_excludes_tool_only_visual_models():
    body = {
        "data": [
            {"id": "o3-deep-research", "owned_by": "openai"},
            {"id": "o4-mini-deep-research-2025-06-26", "owned_by": "openai"},
            {"id": "computer-use-preview", "owned_by": "openai"},
        ]
    }

    models, _ = pc._parse_catalog_page("openai", body)

    assert all(model["input_modalities"] == ["text"] for model in models)


def test_parse_catalog_page_openai_marks_known_text_models_and_keeps_unknown_safe():
    body = {
        "data": [
            {"id": "gpt-4-0613", "owned_by": "openai"},
            {"id": "gpt-3.5-turbo", "owned_by": "openai"},
            {"id": "ft:gpt-4.1:org:custom", "owned_by": "organization-owner"},
            {"id": "gpt-future-specialized", "owned_by": "openai"},
        ]
    }
    models, _ = pc._parse_catalog_page("openai", body)
    assert models[0]["input_modalities"] == ["text"]
    assert models[1]["input_modalities"] == ["text"]
    assert "input_modalities" not in models[2]
    assert "input_modalities" not in models[3]


def test_parse_catalog_page_openai_covers_legacy_and_specialized_text_models():
    body = {
        "data": [
            {"id": "gpt-3.5-turbo-16k", "owned_by": "openai"},
            {"id": "gpt-3.5-turbo-instruct-0914", "owned_by": "openai"},
            {"id": "babbage-002", "owned_by": "openai"},
            {"id": "davinci-002", "owned_by": "openai"},
            {"id": "o3-mini-2025-01-31", "owned_by": "openai"},
            {"id": "gpt-4o-search-preview-2025-03-11", "owned_by": "openai"},
        ]
    }

    models, _ = pc._parse_catalog_page("openai", body)

    assert all(model["input_modalities"] == ["text"] for model in models)


def test_parse_catalog_page_openai_marks_specialized_models_non_visual():
    body = {
        "data": [
            {"id": "whisper-1", "owned_by": "openai"},
            {"id": "gpt-4o-mini-transcribe", "owned_by": "openai"},
            {"id": "tts-1", "owned_by": "openai"},
            {"id": "text-embedding-3-large", "owned_by": "openai"},
            {"id": "gpt-image-1", "owned_by": "openai"},
            {"id": "omni-moderation-latest", "owned_by": "openai"},
        ]
    }

    models, _ = pc._parse_catalog_page("openai", body)

    assert [model["input_modalities"] for model in models] == [
        ["audio"],
        ["audio"],
        ["text"],
        ["text"],
        ["text"],
        ["text"],
    ]


def test_parse_catalog_page_gemini_strips_prefix_and_paginates():
    body = {"models": [{
                "name": "models/gemini-3.1-pro",
                "displayName": "Gemini 3.1 Pro",
                "supportedGenerationMethods": ["generateContent"],
            }],
            "nextPageToken": "tok2"}
    models, nxt = pc._parse_catalog_page("gemini", body)
    assert models == [{
        "id": "gemini-3.1-pro",
        "display_name": "Gemini 3.1 Pro",
        "input_modalities": ["text", "image"],
    }]
    assert nxt == "tok2"


def test_parse_catalog_page_gemini_excludes_non_observer_model_families():
    body = {"models": [
        {
            "name": "models/gemma-3-27b-it",
            "supportedGenerationMethods": ["generateContent"],
        },
        {
            "name": "models/gemini-2.5-flash-tts",
            "supportedGenerationMethods": ["generateContent"],
        },
        {
            "name": "models/gemini-embedding-2",
            "supportedGenerationMethods": ["embedContent"],
        },
        {"name": "models/gemini-future-without-methods"},
    ]}

    models, _ = pc._parse_catalog_page("gemini", body)

    assert models[0]["input_modalities"] == ["text"]
    assert models[1]["input_modalities"] == ["text"]
    assert models[2]["input_modalities"] == ["text"]
    assert "input_modalities" not in models[3]


def test_parse_catalog_page_anthropic_has_more():
    body = {"data": [{"id": "claude-opus-5", "display_name": "Claude Opus 5"}],
            "has_more": True, "last_id": "claude-opus-5"}
    models, nxt = pc._parse_catalog_page("anthropic", body)
    assert models == [{"id": "claude-opus-5", "display_name": "Claude Opus 5"}]
    assert nxt == "claude-opus-5"


def test_parse_catalog_page_openrouter_preserves_explicit_input_modalities():
    body = {"data": [{
        "id": "vendor/model",
        "architecture": {"input_modalities": ["text", "image"]},
    }]}
    models, _ = pc._parse_catalog_page("openrouter", body)
    assert models == [{
        "id": "vendor/model",
        "display_name": "vendor/model",
        "input_modalities": ["image", "text"],
    }]


def test_parse_catalog_page_anthropic_maps_image_capability_without_guessing():
    body = {"data": [
        {"id": "vision", "capabilities": {"image_input": {"supported": True}}},
        {"id": "text", "capabilities": {"image_input": {"supported": False}}},
        {"id": "unknown"},
    ]}
    models, _ = pc._parse_catalog_page("anthropic", body)
    assert models[0]["input_modalities"] == ["text", "image"]
    assert models[1]["input_modalities"] == ["text"]
    assert "input_modalities" not in models[2]


def test_parse_catalog_page_deepseek_marks_official_models_text_only():
    body = {"data": [
        {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
        {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
    ]}

    models, _ = pc._parse_catalog_page("deepseek", body)

    assert [model["input_modalities"] for model in models] == [["text"], ["text"]]


def test_parse_catalog_page_compatible_accepts_only_explicit_safe_modalities():
    body = {"data": [
        {"id": "explicit", "input_modalities": ["IMAGE", "text", "secret"]},
        {"id": "name-only"},
    ]}
    models, _ = pc._parse_catalog_page("openai_compatible", body)
    assert models[0]["input_modalities"] == ["image", "text"]
    assert "input_modalities" not in models[1]


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
    # /models/user is authenticated and filtered for this exact API key.
    calls = _install_fake_stream(monkeypatch, [
        (200, {"data": [{"id": "a"}, {"id": "b"}]}),
    ])
    res = pc.list_provider_models("openrouter", "k", "")
    assert res["catalog_supported"] is True and res["complete"] is True
    assert [m["id"] for m in res["models"]] == ["a", "b"]
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/models/user")
    assert calls[0]["params"] == {}


def test_list_models_openrouter_bogus_key_rejected_by_user_catalog(monkeypatch):
    # The key-filtered catalog itself is authenticated, so a bad key cannot
    # receive the public all-model list.
    calls = _install_fake_stream(monkeypatch, [
        (401, {"error": "invalid api key"}),
    ])
    with pytest.raises(pc.ProviderError) as ei:
        pc.list_provider_models("openrouter", "bogus", "")
    assert ei.value.status_code == 401
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/models/user")
    # And the route maps a bogus openrouter key to the auth-failed slug.
    assert pc.model_catalog_error_slug(ei.value) == "model_catalog_auth_failed"


def test_list_models_openai_issues_no_key_probe(monkeypatch):
    # Providers whose /models already authenticates must NOT get an extra /key
    # probe — the fix is scoped to public-catalog providers only.
    calls = _install_fake_stream(monkeypatch, [(200, {"data": [{"id": "a"}]})])
    res = pc.list_provider_models("openai", "k", "")
    assert res["catalog_supported"] is True
    assert [m["id"] for m in res["models"]] == ["a"]
    assert len(calls) == 1                               # single call: /models
    assert calls[0]["url"].endswith("/models")
    assert not any(c["url"].endswith("/key") for c in calls)


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


def test_list_models_exact_cap_no_more_is_complete(monkeypatch):
    # Exactly _CATALOG_MAX_MODELS on the last page with no next cursor and no
    # leftover members → genuinely complete, must NOT be flagged truncated.
    monkeypatch.setattr(pc, "_CATALOG_MAX_MODELS", 2)
    _install_fake_stream(monkeypatch, [(200, {"data": [{"id": "a"}, {"id": "b"}]})])
    res = pc.list_provider_models("openai", "k", "")
    assert [m["id"] for m in res["models"]] == ["a", "b"]
    assert res["complete"] is True
    assert res["warnings"] == []


def test_list_models_cap_with_next_cursor_is_truncated(monkeypatch):
    # Hitting the cap while a next cursor is still outstanding IS truncated.
    monkeypatch.setattr(pc, "_CATALOG_MAX_MODELS", 2)
    _install_fake_stream(monkeypatch, [
        (200, {"data": [{"id": "a"}, {"id": "b"}], "has_more": True, "last_id": "b"}),
    ])
    res = pc.list_provider_models("anthropic", "k", "")
    assert [m["id"] for m in res["models"]] == ["a", "b"]
    assert res["complete"] is False
    assert any("model cap reached" in w for w in res["warnings"])


def test_list_models_cap_with_leftover_members_is_truncated(monkeypatch):
    # Cap hit mid-page with more members left on the same page → truncated.
    monkeypatch.setattr(pc, "_CATALOG_MAX_MODELS", 2)
    _install_fake_stream(monkeypatch, [
        (200, {"data": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}),
    ])
    res = pc.list_provider_models("openai", "k", "")
    assert [m["id"] for m in res["models"]] == ["a", "b"]
    assert res["complete"] is False


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


def test_list_models_empty_first_page_then_failing_second_is_partial(monkeypatch):
    # A legit-empty first page WITH a next cursor, then a 503 on page 2. Keying
    # on model count would misfire the first-page `raise` (0 models collected);
    # keying on page index must treat this as partial success.
    _install_fake_stream(monkeypatch, [
        (200, {"data": [], "has_more": True, "last_id": "cur1"}),
        (503, {"error": "upstream"}),
    ])
    res = pc.list_provider_models("anthropic", "k", "")
    assert res["models"] == []
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
