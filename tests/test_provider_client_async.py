# tests/test_provider_client_async.py
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx  # noqa: E402
import pytest  # noqa: E402

import provider_client  # noqa: E402


def _mock_async_client(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(provider_client, "_shared_async_client", client)
    return client


def test_openrouter_wire_async(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "gen-1",
            "choices": [{"message": {"content": "a caption"},
                         "finish_reason": "stop"}],
            "usage": {"total_tokens": 10},
        })

    _mock_async_client(monkeypatch, handler)
    cfg = provider_client.ProviderConfig(
        provider="openrouter", model="qwen/qwen3-vl-8b-instruct",
        api_key="or-key", base_url="https://openrouter.ai/api/v1")
    out = asyncio.run(provider_client.chat_completion_async(
        cfg, [{"role": "user", "content": "hi"}], max_tokens=160, timeout=45.0))
    assert out["reply"] == "a caption"
    assert out["provider"] == "openrouter"
    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["max_tokens"] == 160
    assert seen["body"]["stream"] is False


def test_provider_error_on_http_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)
    _mock_async_client(monkeypatch, handler)
    cfg = provider_client.ProviderConfig(
        provider="openrouter", model="m", api_key="k",
        base_url="https://openrouter.ai/api/v1")
    with pytest.raises(provider_client.ProviderError):
        asyncio.run(provider_client.chat_completion_async(
            cfg, [{"role": "user", "content": "hi"}]))


def test_missing_key_raises():
    cfg = provider_client.ProviderConfig(provider="openrouter", model="m", api_key="")
    with pytest.raises(provider_client.ProviderError):
        asyncio.run(provider_client.chat_completion_async(
            cfg, [{"role": "user", "content": "hi"}]))


def test_non_openai_wire_native_async(monkeypatch):
    """PR B Task 7 (B3): anthropic/gemini/openai-responses no longer bridge to
    the sync `chat_completion` via anyio.to_thread — they POST natively async
    through the shared _async_http_client, same as the openai-compat wire."""
    def boom(*a, **k):
        raise AssertionError("must not call sync chat_completion (thread bridge removed)")

    monkeypatch.setattr(provider_client, "chat_completion", boom)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "msg_1",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        })

    _mock_async_client(monkeypatch, handler)
    cfg = provider_client.ProviderConfig(
        provider="anthropic", model="claude-sonnet-5", api_key="k")
    out = asyncio.run(provider_client.chat_completion_async(
        cfg, [{"role": "user", "content": "hi"}]))
    assert out["reply"] == "hi"
    assert out["provider"] == "anthropic"
    assert out["tool_calls"] == []


def test_openai_compatible_returns_remapped_model_async(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "gen-2",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        })

    _mock_async_client(monkeypatch, handler)
    cfg = provider_client.ProviderConfig(
        provider="openrouter", model="deepseek/deepseek-chat",
        api_key="or-key", base_url="https://openrouter.ai/api/v1")
    out = asyncio.run(provider_client.chat_completion_async(
        cfg, [{"role": "user", "content": "hi"}]))
    assert out["model"] != cfg.model
    assert out["model"] == provider_client._runtime_model(cfg.provider, cfg.model)[0]


def test_aclose_async_http_client(monkeypatch):
    client = _mock_async_client(monkeypatch, lambda r: httpx.Response(200))
    asyncio.run(provider_client.aclose_async_http_client())
    assert provider_client._shared_async_client is None
    assert client.is_closed


def test_async_retries_without_temperature_on_temperature_400(monkeypatch):
    """The async wire must downgrade identically to the sync one — this module keeps a
    SINGLE openai-compat codec precisely so the two can't drift. The enclave's caption
    path is async, so a temperature-deprecating model would 400 there too."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            assert body["temperature"] == 0.1  # first attempt keeps determinism
            return httpx.Response(400, json={
                "error": {"message": "`temperature` is deprecated for this model."}})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 3},
        })

    _mock_async_client(monkeypatch, handler)
    cfg = provider_client.ProviderConfig(
        provider="openai_compatible", model="claude-sonnet-5",
        api_key="sk-x", base_url="https://relay.example/v1")
    out = asyncio.run(provider_client.chat_completion_async(
        cfg, [{"role": "user", "content": "hi"}], temperature=0.1))

    assert out["reply"] == "ok"
    assert len(seen) == 2
    assert "temperature" not in seen[1]  # retry dropped it
