"""Focused contract tests for provider prompt-cache request adapters.

Prompt caching is an optimization, never a correctness dependency.  These
tests pin both halves of that contract: supported providers receive their
native cache/session hints, while a relay that rejects those optional fields
gets one bounded cache-off retry without losing tools or other request state.
"""
from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client as pc  # noqa: E402
from provider_types import ToolSpec  # noqa: E402


MESSAGES = [{"role": "user", "content": "Find the latest result."}]
TOOLS = [
    ToolSpec(
        name="web_search",
        description="Search the web",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    ),
]
CACHE_KEY = "feedling-v2-deadbeef"
CACHE_FIELDS = {"prompt_cache_key", "prompt_cache_options", "cache_control", "session_id"}


def _compat_payload(provider: str, model: str) -> dict:
    return pc._build_openai_compat_payload(
        provider=provider,
        model=model,
        messages=MESSAGES,
        temperature=0.1,
        max_tokens=256,
        response_format=None,
        extra_body=None,
        include_reasoning=False,
        tools=TOOLS,
        prompt_cache_key=CACHE_KEY,
    )


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request("POST", "https://provider.example/v1/generate"),
    )


def _openai_chat_success() -> httpx.Response:
    return _response(
        200,
        {
            "id": "completion-1",
            "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 1},
        },
    )


def _responses_success() -> httpx.Response:
    return _response(
        200,
        {
            "id": "response-1",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            ],
            "usage": {"input_tokens": 12, "output_tokens": 1},
        },
    )


def _anthropic_success() -> httpx.Response:
    return _response(
        200,
        {
            "id": "message-1",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 1},
        },
    )


def test_openai_chat_sends_only_opaque_prompt_cache_key() -> None:
    payload = _compat_payload("openai", "gpt-4.1")

    assert payload["prompt_cache_key"] == CACHE_KEY
    assert "session_id" not in payload
    assert "cache_control" not in payload


def test_openai_responses_sends_opaque_prompt_cache_key() -> None:
    payload, _, _ = pc._build_openai_responses_payload(
        model="gpt-5",
        base_url="https://api.openai.com/v1",
        key="sk-test",
        messages=MESSAGES,
        max_tokens=256,
        response_format=None,
        tools=TOOLS,
        prompt_cache_key=CACHE_KEY,
    )

    assert payload["prompt_cache_key"] == CACHE_KEY


def test_openrouter_sends_sticky_cache_fields_and_anthropic_cache_control() -> None:
    payload = _compat_payload("openrouter", "anthropic/claude-sonnet-4")

    assert payload["prompt_cache_key"] == CACHE_KEY
    assert payload["session_id"] == CACHE_KEY
    assert payload["cache_control"] == {"type": "ephemeral"}


def test_direct_anthropic_uses_cache_control_without_disclosing_affinity_key() -> None:
    payload, _, _ = pc._build_anthropic_payload(
        model="claude-sonnet-4-5",
        base_url="https://api.anthropic.com/v1",
        key="sk-test",
        messages=MESSAGES,
        max_tokens=256,
        temperature=0.1,
        response_format=None,
        tools=TOOLS,
        prompt_cache_key=CACHE_KEY,
    )

    assert payload["cache_control"] == {"type": "ephemeral"}
    assert "prompt_cache_key" not in payload
    assert "session_id" not in payload


def test_gemini_relies_on_implicit_cache_without_non_native_fields() -> None:
    payload, _, _ = pc._build_gemini_payload(
        model="gemini-2.5-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        key="test-key",
        messages=MESSAGES,
        max_tokens=256,
        temperature=0.1,
        response_format=None,
        tools=TOOLS,
    )

    assert CACHE_FIELDS.isdisjoint(payload)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("deepseek", "deepseek-v4-flash"),
        ("openai_compatible", "custom-model"),
    ],
)
def test_other_openai_compatible_wires_do_not_receive_cache_fields(
    provider: str, model: str,
) -> None:
    assert CACHE_FIELDS.isdisjoint(_compat_payload(provider, model))


def test_sync_responses_cache_rejection_retries_without_cache_but_keeps_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    responses = [
        _response(400, {"error": {"message": "unknown field prompt_cache_key"}}),
        _responses_success(),
    ]

    class Client:
        def post(self, *args, json=None, **kwargs):
            seen.append(copy.deepcopy(json))
            return responses.pop(0)

    monkeypatch.setattr(pc, "_http_client", lambda: Client())
    config = pc.ProviderConfig(
        provider="openai",
        model="gpt-5",
        api_key="sk-test",
        prompt_cache_key=CACHE_KEY,
    )

    result = pc.chat_completion(config, MESSAGES, tools=TOOLS)

    assert result["reply"] == "done"
    assert len(seen) == 2
    assert seen[0]["prompt_cache_key"] == CACHE_KEY
    assert "prompt_cache_key" not in seen[1]
    assert seen[1]["tools"] == seen[0]["tools"]


def test_async_anthropic_cache_rejection_retries_without_cache_but_keeps_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    responses = [
        _response(422, {"error": {"message": "extra field cache_control"}}),
        _anthropic_success(),
    ]

    class AsyncClient:
        async def post(self, *args, json=None, **kwargs):
            seen.append(copy.deepcopy(json))
            return responses.pop(0)

    monkeypatch.setattr(pc, "_async_http_client", lambda: AsyncClient())
    config = pc.ProviderConfig(
        provider="anthropic",
        model="claude-sonnet-4-5",
        api_key="sk-test",
        prompt_cache_key=CACHE_KEY,
    )

    result = asyncio.run(pc.chat_completion_async(config, MESSAGES, tools=TOOLS))

    assert result["reply"] == "done"
    assert len(seen) == 2
    assert seen[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in seen[1]
    assert seen[1]["tools"] == seen[0]["tools"]


def test_openrouter_walks_cache_reasoning_temperature_fallbacks_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    responses = [
        _response(400, {"error": {"message": "unknown prompt_cache_key"}}),
        _response(400, {"error": {"message": "reasoning is unsupported"}}),
        _response(400, {"error": {"message": "temperature is deprecated"}}),
        _openai_chat_success(),
    ]

    class Client:
        def post(self, *args, json=None, **kwargs):
            seen.append(copy.deepcopy(json))
            return responses.pop(0)

    monkeypatch.setattr(pc, "_http_client", lambda: Client())
    config = pc.ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        api_key="sk-test",
        prompt_cache_key=CACHE_KEY,
    )

    result = pc.chat_completion(
        config,
        MESSAGES,
        temperature=0.1,
        include_reasoning=True,
        tools=TOOLS,
    )

    assert result["reply"] == "done"
    assert len(seen) == 4
    assert not CACHE_FIELDS.isdisjoint(seen[0])
    assert CACHE_FIELDS.isdisjoint(seen[1])
    assert "reasoning" in seen[1] and "temperature" in seen[1]
    assert "reasoning" not in seen[2] and "temperature" in seen[2]
    assert "reasoning" not in seen[3] and "temperature" not in seen[3]
    assert all(payload["tools"] == seen[0]["tools"] for payload in seen)


def test_openrouter_reasoning_rejection_preserves_supported_cache_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    responses = [
        _response(400, {"error": {"message": "unknown field reasoning"}}),
        _openai_chat_success(),
    ]

    class Client:
        def post(self, *args, json=None, **kwargs):
            seen.append(copy.deepcopy(json))
            return responses.pop(0)

    monkeypatch.setattr(pc, "_http_client", lambda: Client())
    config = pc.ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        api_key="sk-test",
        prompt_cache_key=CACHE_KEY,
    )

    result = pc.chat_completion(
        config,
        MESSAGES,
        temperature=0.1,
        include_reasoning=True,
        tools=TOOLS,
    )

    assert result["reply"] == "done"
    assert len(seen) == 2
    assert "reasoning" in seen[0] and "reasoning" not in seen[1]
    assert seen[1]["session_id"] == CACHE_KEY
    assert seen[1]["prompt_cache_key"] == CACHE_KEY
    assert seen[1]["cache_control"] == {"type": "ephemeral"}
    assert seen[1]["tools"] == seen[0]["tools"]


@pytest.mark.parametrize(
    ("provider", "raw", "expected"),
    [
        (
            "openai",
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "total_tokens": 107,
                "cache_read_tokens": 80,
                "cache_write_tokens": None,
                "cache_miss_tokens": 20,
            },
        ),
        (
            "openai",
            {
                "input_tokens": 100,
                "output_tokens": 7,
                "input_tokens_details": {"cached_tokens": 80},
            },
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "total_tokens": 107,
                "cache_read_tokens": 80,
                "cache_write_tokens": None,
                "cache_miss_tokens": 20,
            },
        ),
        (
            "anthropic",
            {
                "input_tokens": 20,
                "output_tokens": 7,
                "cache_read_input_tokens": 70,
                "cache_creation_input_tokens": 10,
            },
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "total_tokens": 107,
                "cache_read_tokens": 70,
                "cache_write_tokens": 10,
                "cache_miss_tokens": 30,
            },
        ),
        (
            "gemini",
            {
                "promptTokenCount": 100,
                "candidatesTokenCount": 7,
                "cachedContentTokenCount": 80,
            },
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "total_tokens": 107,
                "cache_read_tokens": 80,
                "cache_write_tokens": None,
                "cache_miss_tokens": 20,
            },
        ),
        (
            "deepseek",
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
            },
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "total_tokens": 107,
                "cache_read_tokens": 80,
                "cache_write_tokens": None,
                "cache_miss_tokens": 20,
            },
        ),
    ],
)
def test_usage_normalization_reports_effective_input_and_cache_tokens(
    provider: str, raw: dict, expected: dict,
) -> None:
    assert pc._normalize_usage(provider, raw) == expected


@pytest.mark.parametrize(
    ("provider", "raw"),
    [
        ("openai", {"prompt_tokens": 12, "completion_tokens": 1}),
        ("anthropic", {"input_tokens": 12, "output_tokens": 1}),
        ("gemini", {"promptTokenCount": 12, "candidatesTokenCount": 1}),
    ],
)
def test_usage_normalization_keeps_unreported_cache_metrics_unknown(
    provider: str, raw: dict,
) -> None:
    usage = pc._normalize_usage(provider, raw)

    assert usage["cache_read_tokens"] is None
    assert usage["cache_write_tokens"] is None
    assert usage["cache_miss_tokens"] is None
