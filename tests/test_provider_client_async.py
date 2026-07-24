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
    assert provider_client.runtime_provider_attempt_trace(out) is None


def test_provider_error_on_http_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)
    _mock_async_client(monkeypatch, handler)
    cfg = provider_client.ProviderConfig(
        provider="openrouter", model="m", api_key="k",
        base_url="https://openrouter.ai/api/v1", capture_attempt_trace=True)
    with pytest.raises(provider_client.ProviderError) as raised:
        asyncio.run(provider_client.chat_completion_async(
            cfg, [{"role": "user", "content": "hi"}]))
    trace = raised.value.feedling_provider_attempt_trace
    assert trace["version"] == 1
    assert len(trace["attempts"]) == 1
    assert trace["attempts"][0]["status"] is None
    assert trace["attempts"][0]["error_class"] == "transient"
    assert trace["attempts"][0]["wire"]["payload"]["messages"] == [
        {"role": "user", "content": "hi"}
    ]


def test_runtime_trace_marks_transport_200_invalid_json_as_postprocess_error(
    monkeypatch,
):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    _mock_async_client(monkeypatch, handler)
    cfg = provider_client.ProviderConfig(
        provider="openrouter",
        model="m",
        api_key="k",
        base_url="https://openrouter.ai/api/v1",
        capture_attempt_trace=True,
    )
    with pytest.raises(provider_client.ProviderError) as raised:
        asyncio.run(
            provider_client.chat_completion_async(
                cfg,
                [{"role": "user", "content": "hi"}],
            )
        )
    attempt = raised.value.feedling_provider_attempt_trace["attempts"][0]
    assert attempt["status"] == 200
    assert attempt["error_class"] == "transient"
    assert attempt["outcome"] == "postprocess_error"
    assert attempt["postprocess_stage"] == "response_decode_or_validation"


def test_runtime_attempt_trace_can_be_released_after_durable_capture():
    trace = {"version": 1, "attempts": [{"wire": {"payload": {"large": "x"}}}]}
    result = {
        "reply": "ok",
        provider_client._RUNTIME_PROVIDER_ATTEMPT_TRACE_FIELD: trace,
    }
    stripped = provider_client.without_runtime_provider_attempt_trace(result)
    assert stripped == {"reply": "ok"}
    assert provider_client.runtime_provider_attempt_trace(stripped) is None
    assert provider_client.runtime_provider_attempt_trace(result) == trace


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
        api_key="sk-x", base_url="https://relay.example/v1",
        capture_attempt_trace=True)
    out = asyncio.run(provider_client.chat_completion_async(
        cfg, [{"role": "user", "content": "hi"}], temperature=0.1))

    assert out["reply"] == "ok"
    assert len(seen) == 2
    assert "temperature" not in seen[1]  # retry dropped it
    trace = out[provider_client._RUNTIME_PROVIDER_ATTEMPT_TRACE_FIELD]
    assert trace["version"] == 1
    assert [item["ordinal"] for item in trace["attempts"]] == [1, 2]
    assert [item["inner_attempt"] for item in trace["attempts"]] == [1, 2]
    assert [item["status"] for item in trace["attempts"]] == [400, 200]
    assert trace["attempts"][0]["error_class"] == "provider_config"
    assert trace["attempts"][0]["compatibility_fallback"] == (
        "temperature_rejected"
    )
    assert trace["attempts"][1]["compatibility_fallback"] is None
    assert [item["wire"]["payload"] for item in trace["attempts"]] == seen
    assert all(item["duration_ms"] >= 0 for item in trace["attempts"])
    serialized_trace = json.dumps(trace)
    assert "sk-x" not in serialized_trace
    assert "https://relay.example/v1" not in serialized_trace


def test_reliable_async_merges_inner_http_and_outer_retry_attempts(monkeypatch):
    responses = [
        httpx.Response(503, json={"error": {"message": "try again"}}),
        httpx.Response(200, json={
            "choices": [{
                "message": {"content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"total_tokens": 3},
        }),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    _mock_async_client(monkeypatch, handler)
    cfg = provider_client.ProviderConfig(
        provider="openrouter",
        model="openai/gpt-4o-mini",
        api_key="secret-key",
        base_url="https://relay.example/v1",
        capture_attempt_trace=True,
    )
    out = asyncio.run(provider_client.reliable_chat_completion_async(
        cfg,
        [{"role": "user", "content": "hi"}],
        max_attempts=2,
        base_delay_sec=0.0,
    ))

    trace = out[provider_client._RUNTIME_PROVIDER_ATTEMPT_TRACE_FIELD]
    attempts = trace["attempts"]
    assert [item["ordinal"] for item in attempts] == [1, 2, 3, 4]
    assert [item["kind"] for item in attempts] == [
        "http_attempt",
        "outer_attempt",
        "http_attempt",
        "outer_attempt",
    ]
    assert [item["outer_attempt"] for item in attempts] == [1, 1, 2, 2]
    assert attempts[0]["status"] == 503
    assert attempts[0]["error_class"] == "transient"
    assert attempts[1]["outcome"] == "retry"
    assert attempts[1]["wire"]["ordinals"] == [1]
    assert attempts[2]["status"] == 200
    assert attempts[3]["outcome"] == "success"
    assert attempts[3]["wire"]["ordinals"] == [3]
    assert out["usage"]["provider_retry_count"] == 1
    serialized_trace = json.dumps(trace)
    assert "secret-key" not in serialized_trace
    assert "https://relay.example/v1" not in serialized_trace


def test_reliable_async_attaches_complete_trace_to_terminal_exception(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, json={"error": {"message": "still unavailable"}}
        )

    _mock_async_client(monkeypatch, handler)
    cfg = provider_client.ProviderConfig(
        provider="openrouter",
        model="openai/gpt-4o-mini",
        api_key="secret-key",
        base_url="https://relay.example/v1",
        capture_attempt_trace=True,
    )
    with pytest.raises(provider_client.ProviderError) as raised:
        asyncio.run(provider_client.reliable_chat_completion_async(
            cfg,
            [{"role": "user", "content": "hi"}],
            max_attempts=2,
            base_delay_sec=0.0,
        ))

    assert raised.value.feedling_error_class == "transient_exhausted"
    trace = raised.value.feedling_provider_attempt_trace
    attempts = trace["attempts"]
    assert [item["kind"] for item in attempts] == [
        "http_attempt",
        "outer_attempt",
        "http_attempt",
        "outer_attempt",
    ]
    assert [item["status"] for item in attempts] == [503, 503, 503, 503]
    assert attempts[1]["outcome"] == "retry"
    assert attempts[3]["outcome"] == "terminal_error"
    assert all(item["error_class"] == "transient" for item in attempts)


def test_shared_async_client_never_replays_cookies_across_users():
    # Async twin of the sync cross-user cookie-bleed guard: the shared
    # AsyncClient (enclave caption + openai-wire relays) must not persist a
    # relay's Set-Cookie from one user's response onto another user's request
    # to the same host.
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(
            200, headers=[("set-cookie", "sid=userA; Path=/")], json={"ok": True})

    async def _run() -> httpx.AsyncClient:
        client = provider_client._build_shared_async_client(
            transport=httpx.MockTransport(handler))
        try:
            await client.post("https://relay.example/v1/chat/completions", json={})
            await client.post("https://relay.example/v1/chat/completions", json={})
        finally:
            await client.aclose()
        return client

    client = asyncio.run(_run())
    assert seen == [None, None], f"cookie replayed across calls: {seen!r}"
    assert len(list(client.cookies.jar)) == 0
