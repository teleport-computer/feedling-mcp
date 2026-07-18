"""Focused contract tests for provider prompt-cache request adapters.

Prompt caching is an optimization, never a correctness dependency.  These
tests pin both halves of that contract: supported providers receive their
native cache/session hints, while a relay that rejects those optional fields
gets one bounded cache-off retry without losing tools or other request state.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import httpx
import pytest

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


def _nested_cache_controls(value) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        control = value.get("cache_control")
        if isinstance(control, dict):
            found.append(control)
        for child in value.values():
            found.extend(_nested_cache_controls(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_nested_cache_controls(child))
    return found


def _without_cache_metadata(value):
    if isinstance(value, dict):
        return {
            key: _without_cache_metadata(child)
            for key, child in value.items()
            if key != "cache_control"
        }
    if isinstance(value, list):
        return [_without_cache_metadata(child) for child in value]
    return value


def _runtime_turn_messages() -> tuple[list[dict], list[dict], list[dict]]:
    """Build two growing turns plus the same first turn without live data."""
    first_tail = [
        {"role": "user", "content": "first stable request"},
    ]
    first = v2_context.build_turn_messages(
        system_prompt=v2_context.CHAT_SYSTEM_PROMPT,
        summary="",
        tail=first_tail,
        action_context=v2_context.action_context_str({
            "perception_snapshot": [{
                "ok": True,
                "data": {"now": "2026-07-18T10:00:01Z"},
            }],
        }),
    )
    second = v2_context.build_turn_messages(
        system_prompt=v2_context.CHAT_SYSTEM_PROMPT,
        summary="",
        tail=[
            *first_tail,
            {"role": "assistant", "content": "first stable response"},
            {"role": "user", "content": "second stable request"},
        ],
        action_context=v2_context.action_context_str({
            "perception_snapshot": [{
                "ok": True,
                "data": {"now": "2026-07-18T10:00:02Z"},
            }],
        }),
    )
    without_runtime_data = v2_context.build_turn_messages(
        system_prompt=v2_context.CHAT_SYSTEM_PROMPT,
        summary="",
        tail=first_tail,
    )
    return first, second, without_runtime_data


def _multimodal_runtime_messages() -> list[dict]:
    return v2_context.build_turn_messages(
        system_prompt=v2_context.CHAT_SYSTEM_PROMPT,
        summary="- user asked for visual debugging help",
        tail=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "What is wrong in this screenshot?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }],
        action_context=v2_context.action_context_str({
            "perception_snapshot": [{
                "ok": True,
                "data": {"now": "2026-07-18T10:00:01Z"},
            }],
        }),
    )


def _wire_message_text(message: dict) -> str:
    if "parts" in message:
        return pc._content_text(message.get("parts"))
    return pc._content_text(message.get("content"))


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

    assert payload["session_id"] == CACHE_KEY
    assert "prompt_cache_key" not in payload
    assert "cache_control" not in payload
    assert _nested_cache_controls(payload["messages"]) == [{"type": "ephemeral"}]


def test_v2_cache_breakpoints_exclude_dynamic_perception_grounding() -> None:
    messages = v2_context.build_turn_messages(
        system_prompt=v2_context.CHAT_SYSTEM_PROMPT,
        summary="- user likes tea",
        tail=[{"role": "user", "content": "What should I drink?"}],
        action_context="live perception: now=2026-07-18T10:00:00Z",
    )

    openrouter = pc._build_openai_compat_payload(
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        messages=messages,
        temperature=None,
        max_tokens=256,
        response_format=None,
        extra_body=None,
        include_reasoning=False,
        tools=TOOLS,
        prompt_cache_key=CACHE_KEY,
    )
    controls_by_message = [
        _nested_cache_controls(message.get("content"))
        for message in openrouter["messages"]
    ]

    assert controls_by_message[0] == [{"type": "ephemeral"}]
    assert controls_by_message[1] == [{"type": "ephemeral"}]
    assert controls_by_message[-2] == [{"type": "ephemeral"}]
    assert controls_by_message[-1] == []

    anthropic, _, _ = pc._build_anthropic_payload(
        model="claude-sonnet-4-5",
        base_url="https://api.anthropic.com/v1",
        key="sk-test",
        messages=messages,
        max_tokens=256,
        temperature=None,
        response_format=None,
        tools=TOOLS,
        prompt_cache_key=CACHE_KEY,
    )
    assert _nested_cache_controls(anthropic["system"]) == [{"type": "ephemeral"}]
    assert "live perception" not in str(anthropic["system"])
    assert "live perception" in str(anthropic["messages"][-1]["content"])
    assert _nested_cache_controls(anthropic["messages"][-1]["content"]) == []


def test_stable_skills_and_working_memory_precede_dynamic_cache_frontier() -> None:
    messages = v2_context.build_turn_messages(
        system_prompt=v2_context.CHAT_SYSTEM_PROMPT,
        trusted_system_blocks=("<skill>stable skill</skill>",),
        working_memory="- project alpha is active",
        summary="- prior discussion",
        tail=[{"role": "user", "content": "continue"}],
        action_context="now=dynamic",
    )

    openrouter = pc._build_openai_compat_payload(
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        messages=messages,
        temperature=None,
        max_tokens=256,
        response_format=None,
        extra_body=None,
        include_reasoning=False,
        tools=TOOLS,
        prompt_cache_key=CACHE_KEY,
    )
    working = next(
        message
        for message in openrouter["messages"]
        if v2_context.WORKING_MEMORY_HEADER in str(message.get("content"))
    )
    assert _nested_cache_controls(working["content"]) == [
        {"type": "ephemeral"}
    ]

    anthropic, _, _ = pc._build_anthropic_payload(
        model="claude-sonnet-4-5",
        base_url="https://api.anthropic.com/v1",
        key="sk-test",
        messages=messages,
        max_tokens=256,
        temperature=None,
        response_format=None,
        tools=TOOLS,
        prompt_cache_key=CACHE_KEY,
    )
    assert "<skill>stable skill</skill>" in str(anthropic["system"])
    assert _nested_cache_controls(anthropic["system"]) == [
        {"type": "ephemeral"}
    ]

    bedrock, _, _ = pc._build_bedrock_payload(
        model="us.anthropic.claude-sonnet-4-6",
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        key="bedrock-key",
        messages=messages,
        max_tokens=256,
        temperature=None,
        response_format=None,
        tools=TOOLS,
        prompt_cache_key=CACHE_KEY,
    )
    merged = bedrock["messages"][0]["content"]
    working_index = next(
        index
        for index, block in enumerate(merged)
        if v2_context.WORKING_MEMORY_HEADER in str(block.get("text") or "")
    )
    runtime_index = next(
        index
        for index, block in enumerate(merged)
        if v2_context.RUNTIME_CONTEXT_HEADER in str(block.get("text") or "")
    )
    assert merged[working_index + 1] == {"cachePoint": {"type": "default"}}
    assert working_index < working_index + 1 < runtime_index
    assert not any("cachePoint" in block for block in merged[runtime_index + 1 :])


def test_direct_anthropic_two_turn_runtime_data_preserves_cached_prefix() -> None:
    first_messages, second_messages, without_data_messages = (
        _runtime_turn_messages()
    )

    def build(messages: list[dict]) -> dict:
        payload, _, _ = pc._build_anthropic_payload(
            model="claude-sonnet-4-5",
            base_url="https://api.anthropic.com/v1",
            key="sk-test",
            messages=messages,
            max_tokens=256,
            temperature=None,
            response_format=None,
            tools=TOOLS,
            prompt_cache_key=CACHE_KEY,
        )
        return payload

    first = build(first_messages)
    second = build(second_messages)
    without_data = build(without_data_messages)

    assert first["system"] == second["system"] == without_data["system"]
    assert "2026-07-18T10:00" not in str(first["system"])
    assert first["messages"][:-1] == second["messages"][:1]
    assert first["messages"][-1]["role"] == "user"
    assert _wire_message_text(first["messages"][-1]).startswith(
        v2_context.RUNTIME_CONTEXT_HEADER
    )
    assert _nested_cache_controls(first["messages"][-1]["content"]) == []
    assert first["tools"] == second["tools"] == without_data["tools"]


def test_direct_gemini_two_turn_runtime_data_preserves_implicit_cache_prefix() -> None:
    first_messages, second_messages, without_data_messages = (
        _runtime_turn_messages()
    )

    def build(messages: list[dict]) -> dict:
        payload, _, _ = pc._build_gemini_payload(
            model="gemini-2.5-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            key="test-key",
            messages=messages,
            max_tokens=256,
            temperature=None,
            response_format=None,
            tools=TOOLS,
        )
        return payload

    first = build(first_messages)
    second = build(second_messages)
    without_data = build(without_data_messages)

    assert (
        first["systemInstruction"]
        == second["systemInstruction"]
        == without_data["systemInstruction"]
    )
    assert "2026-07-18T10:00" not in str(first["systemInstruction"])
    assert first["contents"][:-1] == second["contents"][:1]
    assert first["contents"][-1]["role"] == "user"
    assert _wire_message_text(first["contents"][-1]).startswith(
        v2_context.RUNTIME_CONTEXT_HEADER
    )
    assert first["tools"] == second["tools"] == without_data["tools"]


def test_openai_responses_two_turn_runtime_data_preserves_cached_prefix() -> None:
    first_messages, second_messages, without_data_messages = (
        _runtime_turn_messages()
    )

    def build(messages: list[dict]) -> dict:
        payload, _, _ = pc._build_openai_responses_payload(
            model="gpt-5",
            base_url="https://api.openai.com/v1",
            key="sk-test",
            messages=messages,
            max_tokens=256,
            response_format=None,
            tools=TOOLS,
            prompt_cache_key=CACHE_KEY,
        )
        return payload

    first = build(first_messages)
    second = build(second_messages)
    without_data = build(without_data_messages)

    assert (
        first["instructions"]
        == second["instructions"]
        == without_data["instructions"]
    )
    assert "2026-07-18T10:00" not in first["instructions"]
    assert first["input"][:-1] == second["input"][:1]
    assert first["input"][-1]["role"] == "user"
    assert _wire_message_text(first["input"][-1]).startswith(
        v2_context.RUNTIME_CONTEXT_HEADER
    )
    assert first["prompt_cache_key"] == second["prompt_cache_key"] == CACHE_KEY
    assert first["tools"] == second["tools"] == without_data["tools"]


def test_openrouter_two_turn_runtime_data_keeps_existing_cache_boundaries() -> None:
    first_messages, second_messages, without_data_messages = (
        _runtime_turn_messages()
    )

    def build(messages: list[dict]) -> dict:
        return pc._build_openai_compat_payload(
            provider="openrouter",
            model="anthropic/claude-sonnet-4",
            messages=messages,
            temperature=None,
            max_tokens=256,
            response_format=None,
            extra_body=None,
            include_reasoning=False,
            tools=TOOLS,
            prompt_cache_key=CACHE_KEY,
        )

    first = build(first_messages)
    second = build(second_messages)
    without_data = build(without_data_messages)

    assert first["session_id"] == second["session_id"] == CACHE_KEY
    assert "prompt_cache_key" not in first
    assert first["messages"][0] == second["messages"][0]
    assert first["messages"][0] == without_data["messages"][0]
    assert "2026-07-18T10:00" not in str(first["messages"][0])
    assert first["messages"][:-1] == second["messages"][:2]
    assert _wire_message_text(first["messages"][-1]).startswith(
        v2_context.RUNTIME_CONTEXT_HEADER
    )
    assert _nested_cache_controls(first["messages"][-1]["content"]) == []
    assert first["tools"] == second["tools"] == without_data["tools"]


def test_openai_chat_two_turn_runtime_data_keeps_existing_cache_contract() -> None:
    first_messages, second_messages, without_data_messages = (
        _runtime_turn_messages()
    )

    def build(messages: list[dict]) -> dict:
        return pc._build_openai_compat_payload(
            provider="openai",
            model="gpt-4.1",
            messages=messages,
            temperature=None,
            max_tokens=256,
            response_format=None,
            extra_body=None,
            include_reasoning=False,
            tools=TOOLS,
            prompt_cache_key=CACHE_KEY,
        )

    first = build(first_messages)
    second = build(second_messages)
    without_data = build(without_data_messages)

    assert first["prompt_cache_key"] == second["prompt_cache_key"] == CACHE_KEY
    assert "session_id" not in first
    assert first["messages"][0] == second["messages"][0]
    assert first["messages"][0] == without_data["messages"][0]
    assert "2026-07-18T10:00" not in str(first["messages"][0])
    assert first["messages"][:-1] == second["messages"][:2]
    assert _wire_message_text(first["messages"][-1]).startswith(
        v2_context.RUNTIME_CONTEXT_HEADER
    )
    assert _nested_cache_controls(first["messages"]) == []
    assert first["tools"] == second["tools"] == without_data["tools"]


def test_direct_anthropic_native_tool_round_keeps_canonical_cached_prefix() -> None:
    base_messages = _multimodal_runtime_messages()
    call = ToolCall("call-1", "web_search", {"query": "traceback"})
    native = [
        {"type": "thinking", "thinking": "opaque", "signature": "sig-keep"},
        {"type": "text", "text": "I will inspect it."},
        {
            "type": "tool_use",
            "id": call.id,
            "name": call.name,
            "input": call.args,
        },
    ]
    native_original = copy.deepcopy(native)
    exchange = ToolExchange(
        calls=(call,),
        results=(ToolResult(call.id, "search result"),),
        assistant_text="I will inspect it.",
        assistant_turn=NativeAssistantTurn("anthropic", native),
    )

    def build(messages: list) -> dict:
        payload, _, _ = pc._build_anthropic_payload(
            model="claude-sonnet-4-5",
            base_url="https://api.anthropic.com/v1",
            key="sk-test",
            messages=messages,
            max_tokens=256,
            temperature=None,
            response_format=None,
            tools=TOOLS,
            prompt_cache_key=CACHE_KEY,
        )
        return payload

    first = build(base_messages)
    second = build([*base_messages, exchange])

    assert native == native_original
    # The advancing marker set deliberately displaces the old summary marker.
    assert _nested_cache_controls(first["messages"][0])
    assert not _nested_cache_controls(second["messages"][0])
    # Cache metadata may move; the serialized semantic prefix must not.
    assert _without_cache_metadata(first["system"]) == _without_cache_metadata(
        second["system"]
    )
    assert _without_cache_metadata(first["messages"]) == _without_cache_metadata(
        second["messages"][:len(first["messages"])]
    )
    assert all(
        isinstance(message.get("content"), list)
        for message in first["messages"]
    )
    first_without_cache = _without_cache_metadata(first["messages"])
    assert any(
        block.get("type") == "image"
        for message in first_without_cache
        for block in message.get("content", [])
        if isinstance(block, dict)
    )
    second_without_cache = _without_cache_metadata(second["messages"])
    assert second_without_cache[len(first["messages"])] == {
        "role": "assistant",
        "content": native,
    }
    assert second_without_cache[len(first["messages"]) + 1] == {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": "search result",
        }],
    }


def test_openrouter_native_tool_round_keeps_canonical_cached_prefix() -> None:
    base_messages = _multimodal_runtime_messages()
    call = ToolCall("call-1", "web_search", {"query": "traceback"})
    native = {
        "role": "assistant",
        "content": "I will inspect it.",
        "reasoning_content": "opaque-provider-state",
        "tool_calls": [{
            "id": call.id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": '{"query":"traceback"}',
            },
        }],
    }
    native_original = copy.deepcopy(native)
    exchange = ToolExchange(
        calls=(call,),
        results=(ToolResult(call.id, "search result"),),
        assistant_text="I will inspect it.",
        assistant_turn=NativeAssistantTurn("openai_chat", native),
    )

    def build(messages: list) -> dict:
        return pc._build_openai_compat_payload(
            provider="openrouter",
            model="anthropic/claude-sonnet-4",
            messages=messages,
            temperature=None,
            max_tokens=256,
            response_format=None,
            extra_body=None,
            include_reasoning=False,
            tools=TOOLS,
            prompt_cache_key=CACHE_KEY,
        )

    first = build(base_messages)
    second = build([
        *base_messages,
        exchange,
        {"role": "user", "content": "One more detail arrived."},
    ])

    assert native == native_original
    # A folded user boundary plus the native tool transcript advances all four
    # OpenRouter markers and deliberately displaces the old summary marker.
    assert _nested_cache_controls(first["messages"][1])
    assert not _nested_cache_controls(second["messages"][1])
    assert _without_cache_metadata(first["messages"]) == _without_cache_metadata(
        second["messages"][:len(first["messages"])]
    )
    assert all(
        isinstance(message.get("content"), list)
        for message in first["messages"]
    )
    second_without_cache = _without_cache_metadata(second["messages"])
    assistant = second_without_cache[len(first["messages"])]
    assert assistant["content"] == [{"type": "text", "text": native["content"]}]
    assert assistant["reasoning_content"] == native["reasoning_content"]
    assert assistant["tool_calls"] == native["tool_calls"]
    assert second_without_cache[len(first["messages"]) + 1] == {
        "role": "tool",
        "tool_call_id": call.id,
        "content": [{"type": "text", "text": "search result"}],
    }


def test_parallel_tool_batch_cannot_displace_user_cache_boundary() -> None:
    messages = [
        {"role": "system", "content": "stable persona"},
        {"role": "user", "content": "current user request"},
        {
            "role": "assistant",
            "content": "calling two tools",
            "tool_calls": [{"id": "one"}, {"id": "two"}],
        },
        {"role": "tool", "content": "first result", "tool_call_id": "one"},
        {"role": "tool", "content": "second result", "tool_call_id": "two"},
        {"role": "system", "content": "dynamic perception"},
    ]

    marked = pc._mark_openai_chat_cache_breakpoint(messages)

    assert _nested_cache_controls(marked[0]["content"])
    assert _nested_cache_controls(marked[1]["content"])
    assert _nested_cache_controls(marked[3]["content"])
    assert _nested_cache_controls(marked[4]["content"])
    assert not _nested_cache_controls(marked[5]["content"])


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

    assert "cache_control" not in payload
    assert _nested_cache_controls(payload["messages"]) == [{"type": "ephemeral"}]
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
    assert _nested_cache_controls(seen[0]["messages"]) == [{"type": "ephemeral"}]
    assert _nested_cache_controls(seen[1]["messages"]) == []
    assert _without_cache_metadata(seen[0]["messages"]) == seen[1]["messages"]
    assert isinstance(seen[1]["messages"][0]["content"], list)
    assert seen[1]["tools"] == seen[0]["tools"]
    assert result["usage"]["provider_retry_count"] == 1
    assert result["usage"]["cache_hint_sent_on_success"] is False
    assert result["usage"]["compatibility_fallbacks"] == [
        "cache_rejected:cache_control",
    ]


def test_openrouter_walks_cache_reasoning_temperature_fallbacks_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    responses = [
        _response(400, {"error": {"message": "unknown field cache_control"}}),
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
    assert "prompt_cache_key" not in seen[0]
    assert seen[1]["session_id"] == CACHE_KEY
    assert _nested_cache_controls(seen[1]["messages"]) == []
    assert "reasoning" in seen[1] and "temperature" in seen[1]
    assert "reasoning" not in seen[2] and "temperature" in seen[2]
    assert "reasoning" not in seen[3] and "temperature" not in seen[3]
    assert all(payload["tools"] == seen[0]["tools"] for payload in seen)
    assert result["usage"]["provider_retry_count"] == 3
    assert result["usage"]["cache_hint_sent_on_success"] is True
    assert result["usage"]["compatibility_fallbacks"] == [
        "cache_rejected:cache_control",
        "reasoning_rejected",
        "temperature_rejected",
    ]


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
    assert "prompt_cache_key" not in seen[1]
    assert _nested_cache_controls(seen[1]["messages"]) == [{"type": "ephemeral"}]
    assert seen[1]["tools"] == seen[0]["tools"]


def test_async_runtime_default_omits_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []

    class AsyncClient:
        async def post(self, *args, json=None, **kwargs):
            seen.append(copy.deepcopy(json))
            return _openai_chat_success()

    monkeypatch.setattr(pc, "_async_http_client", lambda: AsyncClient())
    config = pc.ProviderConfig(
        provider="openrouter",
        model="openai/gpt-4o-mini",
        api_key="sk-test",
        prompt_cache_key=CACHE_KEY,
    )

    result = asyncio.run(pc.chat_completion_async(config, MESSAGES, tools=TOOLS))

    assert result["reply"] == "done"
    assert len(seen) == 1
    assert "temperature" not in seen[0]
    assert result["usage"]["provider_retry_count"] == 0


def test_openrouter_removes_only_each_named_rejected_cache_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    responses = [
        _response(400, {"error": {"message": "unknown field cache_control"}}),
        _response(400, {"error": {"message": "unknown field session_id"}}),
        _openai_chat_success(),
    ]

    class AsyncClient:
        async def post(self, *args, json=None, **kwargs):
            seen.append(copy.deepcopy(json))
            return responses.pop(0)

    monkeypatch.setattr(pc, "_async_http_client", lambda: AsyncClient())
    config = pc.ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        api_key="sk-test",
        prompt_cache_key=CACHE_KEY,
    )

    result = asyncio.run(
        pc.chat_completion_async(config, MESSAGES, temperature=None, tools=TOOLS)
    )

    assert len(seen) == 3
    assert _nested_cache_controls(seen[0]["messages"]) == [{"type": "ephemeral"}]
    assert _nested_cache_controls(seen[1]["messages"]) == []
    assert "prompt_cache_key" not in seen[1]
    assert seen[1]["session_id"] == CACHE_KEY
    assert "prompt_cache_key" not in seen[2]
    assert "session_id" not in seen[2]
    assert result["usage"]["cache_hint_sent_on_success"] is False
    assert result["usage"]["compatibility_fallbacks"] == [
        "cache_rejected:cache_control",
        "cache_rejected:session_id",
    ]


def test_openrouter_reads_wrapped_upstream_cache_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    responses = [
        _response(400, {
            "error": {
                "message": "Provider returned error",
                "code": 400,
                "metadata": {
                    "raw": json.dumps({
                        "type": "error",
                        "error": {
                            "message": "unknown field session_id",
                        },
                    }),
                },
            },
        }),
        _openai_chat_success(),
    ]

    class AsyncClient:
        async def post(self, *args, json=None, **kwargs):
            seen.append(copy.deepcopy(json))
            return responses.pop(0)

    monkeypatch.setattr(pc, "_async_http_client", lambda: AsyncClient())
    config = pc.ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        api_key="sk-test",
        prompt_cache_key=CACHE_KEY,
    )

    result = asyncio.run(pc.chat_completion_async(config, MESSAGES, tools=TOOLS))

    assert len(seen) == 2
    assert seen[0]["session_id"] == CACHE_KEY
    assert "prompt_cache_key" not in seen[0]
    assert "prompt_cache_key" not in seen[1]
    assert "session_id" not in seen[1]
    assert _nested_cache_controls(seen[1]["messages"]) == [{"type": "ephemeral"}]
    assert result["usage"]["compatibility_fallbacks"] == [
        "cache_rejected:session_id",
    ]


def test_unrelated_bad_request_is_not_retried_without_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    responses = [
        _response(400, {"error": {"message": "model does not exist"}}),
    ]

    class AsyncClient:
        async def post(self, *args, json=None, **kwargs):
            seen.append(copy.deepcopy(json))
            return responses.pop(0)

    monkeypatch.setattr(pc, "_async_http_client", lambda: AsyncClient())
    config = pc.ProviderConfig(
        provider="openrouter",
        model="anthropic/not-a-model",
        api_key="sk-test",
        prompt_cache_key=CACHE_KEY,
    )

    with pytest.raises(pc.ProviderError, match="model does not exist"):
        asyncio.run(
            pc.chat_completion_async(config, MESSAGES, temperature=None, tools=TOOLS)
        )

    assert len(seen) == 1


def test_unrelated_named_tool_schema_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    responses = [
        _response(400, {"error": {"message": "unknown field tools"}}),
    ]

    class AsyncClient:
        async def post(self, *args, json=None, **kwargs):
            seen.append(copy.deepcopy(json))
            return responses.pop(0)

    monkeypatch.setattr(pc, "_async_http_client", lambda: AsyncClient())
    config = pc.ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        api_key="sk-test",
        prompt_cache_key=CACHE_KEY,
    )

    with pytest.raises(pc.ProviderError, match="unknown field tools"):
        asyncio.run(pc.chat_completion_async(config, MESSAGES, tools=TOOLS))

    assert len(seen) == 1


def test_cache_fallback_never_deletes_tool_parameter_named_cache_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_tool = ToolSpec(
        name="inspect_cache",
        description="Inspect a caller-supplied cache object",
        parameters={
            "type": "object",
            "properties": {"cache_control": {"type": "string"}},
        },
    )
    seen: list[dict] = []
    responses = [
        _response(400, {"error": {"message": "unknown field cache_control"}}),
        _openai_chat_success(),
    ]

    class AsyncClient:
        async def post(self, *args, json=None, **kwargs):
            seen.append(copy.deepcopy(json))
            return responses.pop(0)

    monkeypatch.setattr(pc, "_async_http_client", lambda: AsyncClient())
    config = pc.ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        api_key="sk-test",
        prompt_cache_key=CACHE_KEY,
    )

    asyncio.run(pc.chat_completion_async(config, MESSAGES, tools=[schema_tool]))

    assert len(seen) == 2
    assert _nested_cache_controls(seen[1]["messages"]) == []
    assert (
        seen[1]["tools"][0]["function"]["parameters"]["properties"]
        ["cache_control"]
        == {"type": "string"}
    )


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
