from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client as pc  # noqa: E402
from model_api_runtime.v2 import context as v2_context  # noqa: E402
from provider_types import (  # noqa: E402
    NativeAssistantTurn,
    ToolCall,
    ToolExchange,
    ToolResult,
    ToolSpec,
)


TOOLS = [
    ToolSpec(
        "web_search",
        "Search the web",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    ),
]
MODEL = "us.anthropic.claude-sonnet-4-6"
BASE_URL = "https://bedrock-runtime.us-east-1.amazonaws.com"


def _success(content: list[dict], *, stop_reason: str = "end_turn") -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {
            "inputTokens": 12,
            "outputTokens": 3,
            "totalTokens": 15,
            "cacheReadInputTokens": 100,
            "cacheWriteInputTokens": 20,
        },
    }


def test_bedrock_config_alias_and_default_endpoint() -> None:
    assert pc.validate_config("aws-bedrock", MODEL) == (
        "bedrock",
        MODEL,
        BASE_URL,
    )


def test_bedrock_payload_has_native_tools_images_and_stable_cache_points() -> None:
    messages = v2_context.build_turn_messages(
        system_prompt=v2_context.CHAT_SYSTEM_PROMPT,
        agent_memory="- user likes tea",
        tail=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ],
        action_context=v2_context.action_context_str(
            {
                "perception_snapshot": [{"ok": True, "data": {"now": "dynamic"}}],
            }
        ),
    )

    payload, url, headers = pc._build_bedrock_payload(
        model=MODEL,
        base_url=BASE_URL,
        key="bedrock-key",
        messages=messages,
        max_tokens=700,
        temperature=None,
        response_format=None,
        tools=TOOLS,
        prompt_cache_key="feedling-v2-cache",
    )

    assert url.endswith("/model/us.anthropic.claude-sonnet-4-6/converse")
    assert headers["Authorization"] == "Bearer bedrock-key"
    assert payload["toolConfig"]["tools"][0]["toolSpec"]["name"] == "web_search"
    assert payload["toolConfig"]["tools"][-1] == {"cachePoint": {"type": "default"}}
    assert payload["system"][-1] == {"cachePoint": {"type": "default"}}
    assert any(
        "image" in block
        for message in payload["messages"]
        for block in message["content"]
    )
    runtime_message = payload["messages"][-1]
    assert pc._is_runtime_context_message(runtime_message)
    assert not any("cachePoint" in block for block in runtime_message["content"])
    cache_points = [
        block for block in pc._cache_control_blocks(payload) if "cachePoint" in block
    ]
    assert 2 <= len(cache_points) <= 4


def test_bedrock_cache_fallback_removes_union_blocks_but_preserves_tools() -> None:
    payload, _, _ = pc._build_bedrock_payload(
        model=MODEL,
        base_url=BASE_URL,
        key="k",
        messages=[
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "hello"},
        ],
        max_tokens=100,
        temperature=None,
        response_format=None,
        tools=TOOLS,
        prompt_cache_key="cache",
    )

    fallback = pc._without_provider_cache_control(payload)

    assert pc._cache_fields_present(payload) == ("cache_control",)
    assert pc._cache_fields_present(fallback) == ()
    assert fallback["toolConfig"]["tools"] == pc._encode_tools_bedrock(TOOLS)
    assert fallback["system"] == [{"text": "stable"}]
    assert fallback["messages"] == [
        {"role": "user", "content": [{"text": "hello"}]},
    ]


def test_bedrock_decodes_parallel_tool_calls_and_preserves_native_turn() -> None:
    body = _success(
        [
            {
                "toolUse": {
                    "toolUseId": "tool-a",
                    "name": "web_search",
                    "input": {"query": "one"},
                },
            },
            {
                "toolUse": {
                    "toolUseId": "tool-b",
                    "name": "web_search",
                    "input": {"query": "two"},
                },
            },
        ],
        stop_reason="tool_use",
    )

    result = pc._parse_bedrock_body(body, model=MODEL, require_reply=True)

    assert [call["id"] for call in result["tool_calls"]] == ["tool-a", "tool-b"]
    assert result["reply"] == ""
    assert result["assistant_turn"] == {
        "wire": "bedrock",
        "payload": body["output"]["message"]["content"],
    }
    assert result["usage"] == {
        "prompt_tokens": 132,
        "completion_tokens": 3,
        "total_tokens": 135,
        "cache_read_tokens": 100,
        "cache_write_tokens": 20,
        "cache_miss_tokens": 32,
    }


def test_bedrock_encodes_native_tool_exchange_and_results() -> None:
    native_content = [
        {
            "toolUse": {
                "toolUseId": "tool-a",
                "name": "web_search",
                "input": {"query": "one"},
            },
        }
    ]
    exchange = ToolExchange(
        calls=(ToolCall("tool-a", "web_search", {"query": "one"}),),
        results=(ToolResult("tool-a", "result text"),),
        assistant_turn=NativeAssistantTurn("bedrock", native_content),
    )

    system, messages = pc._split_system_messages_bedrock(
        [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "search"},
            exchange,
        ]
    )

    assert system == ["stable"]
    assert messages[-2] == {"role": "assistant", "content": native_content}
    assert messages[-1] == {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": "tool-a",
                    "content": [{"text": "result text"}],
                    "status": "success",
                },
            }
        ],
    }


def test_bedrock_sync_and_async_use_bearer_converse_wire(monkeypatch) -> None:
    captured: list[tuple[str, dict, dict]] = []
    body = _success([{"text": "done"}])

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return body

    class SyncClient:
        def post(self, url, *, headers, json, timeout):
            captured.append((url, headers, json))
            return Response()

    class AsyncClient:
        async def post(self, url, *, headers, json, timeout):
            captured.append((url, headers, json))
            return Response()

    monkeypatch.setattr(pc, "_http_client", lambda: SyncClient())
    config = pc.ProviderConfig(
        "bedrock",
        MODEL,
        "bearer",
        BASE_URL,
        prompt_cache_key="cache",
    )
    sync = pc.chat_completion(
        config, [{"role": "user", "content": "hello"}], tools=TOOLS
    )

    monkeypatch.setattr(pc, "_async_http_client", lambda: AsyncClient())
    async_result = asyncio.run(
        pc.chat_completion_async(
            config, [{"role": "user", "content": "hello"}], tools=TOOLS
        )
    )

    assert sync["reply"] == async_result["reply"] == "done"
    assert sync["provider"] == async_result["provider"] == "bedrock"
    assert all(
        headers["Authorization"] == "Bearer bearer" for _, headers, _ in captured
    )
    assert all(url.endswith("/converse") for url, _, _ in captured)


def test_bedrock_cachepoint_rejection_retries_without_cache(monkeypatch) -> None:
    requests: list[dict] = []

    class Response:
        text = ""

        def __init__(self, status_code: int, body: dict):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    class SyncClient:
        def post(self, url, *, headers, json, timeout):
            requests.append(json)
            if len(requests) == 1:
                return Response(
                    400,
                    {
                        "message": "cachePoint is not supported for this model",
                    },
                )
            return Response(200, _success([{"text": "ok"}]))

    monkeypatch.setattr(pc, "_http_client", lambda: SyncClient())
    result = pc.chat_completion(
        pc.ProviderConfig("bedrock", MODEL, "k", BASE_URL, prompt_cache_key="cache"),
        [{"role": "system", "content": "stable"}, {"role": "user", "content": "hello"}],
        tools=TOOLS,
    )

    assert result["reply"] == "ok"
    assert len(requests) == 2
    assert pc._cache_fields_present(requests[0]) == ("cache_control",)
    assert pc._cache_fields_present(requests[1]) == ()
    assert result["usage"]["compatibility_fallbacks"] == [
        "cache_rejected:cache_control"
    ]


def test_bedrock_http_shape_is_json_serializable() -> None:
    payload, _, _ = pc._build_bedrock_payload(
        model=MODEL,
        base_url=BASE_URL,
        key="k",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=100,
        temperature=None,
        response_format=None,
        tools=TOOLS,
        prompt_cache_key="cache",
    )

    assert json.loads(json.dumps(payload)) == payload
