"""Provider-native multi-round tool transcript encoding.

The loop stores a provider-neutral ``ToolExchange`` between calls.  These tests
prove each production payload builder replays the exact native assistant turn
before attaching every result in that provider's required call-id/name shape.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client as pc
from provider_types import NativeAssistantTurn, ToolCall, ToolExchange, ToolResult


CALLS = (
    ToolCall("call_a", "web_search", {"q": "weather"}),
    ToolCall("call_b", "get_time", {}),
)
RESULTS = (
    ToolResult("call_a", "sunny"),
    ToolResult("call_b", "12:00"),
)


def _exchange(wire, payload):
    return ToolExchange(
        calls=CALLS,
        results=RESULTS,
        assistant_text="let me check",
        assistant_turn=NativeAssistantTurn(wire, payload),
    )


def test_openai_chat_replays_exact_assistant_turn_then_results():
    native = {
        "role": "assistant",
        "content": "let me check",
        "reasoning_content": "provider-only-field",
        "tool_calls": [
            {"id": "call_a", "type": "function", "function": {
                "name": "web_search", "arguments": '{"q":"weather"}'}},
            {"id": "call_b", "type": "function", "function": {
                "name": "get_time", "arguments": "{}"}},
        ],
    }
    payload = pc._build_openai_compat_payload(
        provider="deepseek",
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "hi"}, _exchange("openai_chat", native)],
        temperature=0.7,
        max_tokens=700,
        response_format=None,
        extra_body=None,
        include_reasoning=False,
    )

    assert payload["messages"][1] == native
    assert payload["messages"][2:] == [
        {"role": "tool", "tool_call_id": "call_a", "content": "sunny"},
        {"role": "tool", "tool_call_id": "call_b", "content": "12:00"},
    ]


def test_openai_responses_replays_exact_output_items_then_results():
    native = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
        {"type": "function_call", "id": "fc_item_a", "call_id": "call_a",
         "name": "web_search", "arguments": '{"q":"weather"}'},
        {"type": "function_call", "id": "fc_item_b", "call_id": "call_b",
         "name": "get_time", "arguments": "{}"},
    ]
    payload, _url, _headers = pc._build_openai_responses_payload(
        model="gpt-5",
        base_url="https://api.openai.com/v1",
        key="k",
        messages=[{"role": "user", "content": "hi"},
                  _exchange("openai_responses", native)],
        max_tokens=700,
        response_format=None,
    )

    assert payload["input"][1:4] == native
    assert payload["input"][4:] == [
        {"type": "function_call_output", "call_id": "call_a", "output": "sunny"},
        {"type": "function_call_output", "call_id": "call_b", "output": "12:00"},
    ]


def test_anthropic_replays_thinking_signature_and_tool_results():
    native = [
        {"type": "thinking", "thinking": "opaque", "signature": "sig-keep"},
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "id": "call_a", "name": "web_search",
         "input": {"q": "weather"}},
        {"type": "tool_use", "id": "call_b", "name": "get_time", "input": {}},
    ]
    payload, _url, _headers = pc._build_anthropic_payload(
        model="claude-sonnet-4",
        base_url="https://api.anthropic.com/v1",
        key="k",
        messages=[{"role": "user", "content": "hi"}, _exchange("anthropic", native)],
        max_tokens=700,
        temperature=0.7,
        response_format=None,
    )

    assert payload["messages"][1] == {"role": "assistant", "content": native}
    assert payload["messages"][2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_a", "content": "sunny"},
        {"type": "tool_result", "tool_use_id": "call_b", "content": "12:00"},
    ]}


def test_gemini_replays_thought_signature_and_results_by_name():
    native = {"role": "model", "parts": [
        {"text": "let me check", "thoughtSignature": "sig-keep"},
        {"functionCall": {"name": "web_search", "args": {"q": "weather"}}},
        {"functionCall": {"name": "get_time", "args": {}}},
    ]}
    payload, _url, _headers = pc._build_gemini_payload(
        model="gemini-2.5-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        key="k",
        messages=[{"role": "user", "content": "hi"}, _exchange("gemini", native)],
        max_tokens=700,
        temperature=0.7,
        response_format=None,
    )

    assert payload["contents"][1] == native
    assert payload["contents"][2] == {"role": "user", "parts": [
        {"functionResponse": {"name": "web_search", "response": {"content": "sunny"}}},
        {"functionResponse": {"name": "get_time", "response": {"content": "12:00"}}},
    ]}


def test_exchange_rejects_wire_mismatch_and_incomplete_results():
    mismatch = _exchange("anthropic", [])
    with pytest.raises(pc.ProviderError, match="wire mismatch"):
        pc._encode_messages_openai_chat([mismatch])

    incomplete = ToolExchange(calls=CALLS, results=RESULTS[:1])
    with pytest.raises(pc.ProviderError, match="must match"):
        pc._encode_messages_openai_chat([incomplete])


def test_normalized_exchange_has_deterministic_openai_chat_fallback():
    exchange = ToolExchange(calls=CALLS, results=RESULTS, assistant_text="let me check")
    encoded = pc._encode_messages_openai_chat([exchange])

    assert encoded[0]["role"] == "assistant"
    assert [call["id"] for call in encoded[0]["tool_calls"]] == ["call_a", "call_b"]
    assert [message["tool_call_id"] for message in encoded[1:]] == ["call_a", "call_b"]
