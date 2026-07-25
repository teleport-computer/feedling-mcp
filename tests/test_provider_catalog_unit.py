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
