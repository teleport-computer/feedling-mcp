"""Pure-unit tests for the provider model-catalog wire helpers.

fake-httpx + pure functions only; NO DB, NO real network. Registered in
``tests/conftest.py`` ``_PURE_UNIT`` so it still collects without Postgres.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc  # noqa: E402


# --------------------------------------------------------------------------- #
# Task 1: request construction + page parsing (pure)
# --------------------------------------------------------------------------- #

def test_catalog_request_openai_compatible_bearer():
    url, headers, params = pc._catalog_request(
        "openai_compatible", "sk-x", "https://api.example.com/v1", None)
    assert url == "https://api.example.com/v1/models"
    assert headers["Authorization"] == "Bearer sk-x"
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


# --------------------------------------------------------------------------- #
# Task 2: list_provider_models network orchestration (fake httpx)
# --------------------------------------------------------------------------- #

class _FakeResp:
    def __init__(self, status, body, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else "{}"

    def json(self):
        return self._body


def _install_fake_get(monkeypatch, pages):
    """pages: list of (status, body) 依次返回。"""
    seq = list(pages)
    calls = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get(self, url, *, headers=None, params=None, timeout=None):
            calls.append({"url": url, "params": params or {}})
            page = seq.pop(0)
            status, body = page[0], page[1]
            text = page[2] if len(page) > 2 else None
            return _FakeResp(status, body, text)

    monkeypatch.setattr(pc.httpx, "Client", FakeClient)
    monkeypatch.setattr(pc, "_shared_client", None)
    return calls


def test_list_models_openrouter_single_page(monkeypatch):
    _install_fake_get(monkeypatch, [(200, {"data": [{"id": "a"}, {"id": "b"}]})])
    res = pc.list_provider_models("openrouter", "k", "")
    assert res["catalog_supported"] is True and res["complete"] is True
    assert [m["id"] for m in res["models"]] == ["a", "b"]


def test_list_models_anthropic_paginates_and_dedupes(monkeypatch):
    calls = _install_fake_get(monkeypatch, [
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
    _install_fake_get(monkeypatch, pages)
    res = pc.list_provider_models("anthropic", "k", "")
    assert res["complete"] is False
    assert any("truncated" in w or "不完整" in w for w in res["warnings"])


def test_list_models_upstream_401_raises(monkeypatch):
    _install_fake_get(monkeypatch, [(401, {"error": "bad key"}, "unauthorized")])
    with pytest.raises(pc.ProviderError) as ei:
        pc.list_provider_models("openai", "k", "")
    assert ei.value.status_code == 401
