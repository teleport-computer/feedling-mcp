"""Acceptance test (Hosted Runtime V2 PR B / Task 10) — the @sxysun acceptance
proof: "四类 provider 一次返两个 tool_calls 并按 call_id 收两结果" (all four
provider wires return two tool_calls in a single turn, and correctly receive
back two tool_results keyed by call_id). Proven at the codec boundary
(encode/decode functions in backend/provider_client.py), NOT via a live
network call — this test never makes an HTTP request. For the manual live
probe against real provider APIs see scripts/provider_probe/probe.py (NOT
run in CI).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from provider_types import ToolSpec, ToolResult

TOOLS = [
    ToolSpec("web_search", "search the web", {"type": "object",
             "properties": {"q": {"type": "string"}}, "required": ["q"]}),
    ToolSpec("get_time", "get current time", {"type": "object", "properties": {}}),
]


# --- canned 2-tool-call provider bodies, one per wire -----------------------

def _openai_chat_body():
    return {"choices": [{"message": {"tool_calls": [
        {"id": "call_a", "function": {"name": "web_search", "arguments": '{"q": "weather"}'}},
        {"id": "call_b", "function": {"name": "get_time", "arguments": "{}"}},
    ]}}]}


def _openai_responses_body():
    return {"output": [
        {"type": "function_call", "call_id": "fc_a", "name": "web_search",
         "arguments": '{"q": "weather"}'},
        {"type": "function_call", "call_id": "fc_b", "name": "get_time", "arguments": "{}"},
    ]}


def _anthropic_body():
    return {"content": [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "id": "toolu_a", "name": "web_search", "input": {"q": "weather"}},
        {"type": "tool_use", "id": "toolu_b", "name": "get_time", "input": {}},
    ]}


def _gemini_body():
    return {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "web_search", "args": {"q": "weather"}}},
        {"functionCall": {"name": "get_time", "args": {}}},
    ]}}]}


# --- per-wire tools-shape and results-shape assertions ---------------------

def _assert_openai_chat_tools_shape(encoded):
    assert [t["function"]["name"] for t in encoded] == ["web_search", "get_time"]
    assert all(t["type"] == "function" for t in encoded)


def _assert_openai_responses_tools_shape(encoded):
    assert [t["name"] for t in encoded] == ["web_search", "get_time"]
    assert all(t["type"] == "function" for t in encoded)


def _assert_anthropic_tools_shape(encoded):
    assert [t["name"] for t in encoded] == ["web_search", "get_time"]
    assert all("input_schema" in t for t in encoded)


def _assert_gemini_tools_shape(encoded):
    assert len(encoded) == 1
    decls = encoded[0]["functionDeclarations"]
    assert [d["name"] for d in decls] == ["web_search", "get_time"]


def _assert_openai_chat_results_shape(encoded, id_a, id_b):
    by_id = {r["tool_call_id"]: r["content"] for r in encoded}
    assert set(by_id) == {id_a, id_b}
    assert by_id[id_a] == "sunny" and by_id[id_b] == "12:00"


def _assert_openai_responses_results_shape(encoded, id_a, id_b):
    by_id = {r["call_id"]: r["output"] for r in encoded}
    assert set(by_id) == {id_a, id_b}
    assert by_id[id_a] == "sunny" and by_id[id_b] == "12:00"


def _assert_anthropic_results_shape(encoded, id_a, id_b):
    assert len(encoded) == 1
    blocks = encoded[0]["content"]
    by_id = {b["tool_use_id"]: b["content"] for b in blocks}
    assert set(by_id) == {id_a, id_b}
    assert by_id[id_a] == "sunny" and by_id[id_b] == "12:00"


def _assert_gemini_results_shape(encoded, id_a, id_b, id_to_name):
    assert len(encoded) == 1
    parts = encoded[0]["parts"]
    by_name = {p["functionResponse"]["name"]: p["functionResponse"]["response"]["content"]
               for p in parts}
    assert by_name.get(id_to_name[id_a]) == "sunny"
    assert by_name.get(id_to_name[id_b]) == "12:00"


WIRES = {
    "openai_chat": dict(
        encode_tools=pc._encode_tools_openai_chat,
        decode_calls=pc._decode_tool_calls_openai_chat,
        encode_results=lambda results, id_to_name: pc._encode_tool_results_openai_chat(results),
        body=_openai_chat_body,
        assert_tools_shape=_assert_openai_chat_tools_shape,
        assert_results_shape=lambda enc, a, b, m: _assert_openai_chat_results_shape(enc, a, b),
    ),
    "openai_responses": dict(
        encode_tools=pc._encode_tools_openai_responses,
        decode_calls=pc._decode_tool_calls_openai_responses,
        encode_results=lambda results, id_to_name: pc._encode_tool_results_openai_responses(results),
        body=_openai_responses_body,
        assert_tools_shape=_assert_openai_responses_tools_shape,
        assert_results_shape=lambda enc, a, b, m: _assert_openai_responses_results_shape(enc, a, b),
    ),
    "anthropic": dict(
        encode_tools=pc._encode_tools_anthropic,
        decode_calls=pc._decode_tool_calls_anthropic,
        encode_results=lambda results, id_to_name: pc._encode_tool_results_anthropic(results),
        body=_anthropic_body,
        assert_tools_shape=_assert_anthropic_tools_shape,
        assert_results_shape=lambda enc, a, b, m: _assert_anthropic_results_shape(enc, a, b),
    ),
    "gemini": dict(
        encode_tools=pc._encode_tools_gemini,
        decode_calls=pc._decode_tool_calls_gemini,
        encode_results=lambda results, id_to_name: pc._encode_tool_results_gemini(results, id_to_name),
        body=_gemini_body,
        assert_tools_shape=_assert_gemini_tools_shape,
        assert_results_shape=_assert_gemini_results_shape,
    ),
}


@pytest.mark.parametrize("wire", ["openai_chat", "openai_responses", "anthropic", "gemini"])
def test_two_tool_calls_round_trip_by_call_id(wire):
    spec = WIRES[wire]

    # (a) encode 2 ToolSpecs -> assert the wire tools shape.
    encoded_tools = spec["encode_tools"](TOOLS)
    spec["assert_tools_shape"](encoded_tools)

    # (b) decode a canned 2-tool-call response -> 2 ToolCalls with distinct ids.
    decoded = spec["decode_calls"](spec["body"]())
    assert len(decoded) == 2
    ids = [c["id"] for c in decoded]
    assert len(set(ids)) == 2, f"decoded tool_call ids must be distinct, got {ids}"
    names = {c["id"]: c["name"] for c in decoded}
    assert set(names.values()) == {"web_search", "get_time"}

    # (c) build id_to_name from the decoded calls.
    id_to_name = {c["id"]: c["name"] for c in decoded}
    id_a = next(i for i, n in id_to_name.items() if n == "web_search")
    id_b = next(i for i, n in id_to_name.items() if n == "get_time")

    # (d) encode 2 ToolResults (one per decoded id) -> assert BOTH present,
    # keyed by the right id/name in the wire shape.
    results = [ToolResult(id_a, "sunny"), ToolResult(id_b, "12:00")]
    encoded_results = spec["encode_results"](results, id_to_name)
    spec["assert_results_shape"](encoded_results, id_a, id_b, id_to_name)
