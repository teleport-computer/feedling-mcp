import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from provider_types import ToolSpec


TOOLS = [
    ToolSpec("web_search", "search the web", {"type": "object",
             "properties": {"q": {"type": "string"}}, "required": ["q"]}),
    ToolSpec("get_time", "get current time", {"type": "object", "properties": {}}),
]


def test_encode_tools_openai_chat():
    enc = pc._encode_tools_openai_chat(TOOLS)
    assert enc[0] == {"type": "function", "function": {
        "name": "web_search", "description": "search the web",
        "parameters": TOOLS[0].parameters}}
    assert enc[1]["function"]["name"] == "get_time"


def test_decode_two_tool_calls_openai_chat():
    body = {"choices": [{"message": {"tool_calls": [
        {"id": "call_a", "function": {"name": "web_search",
         "arguments": json.dumps({"q": "weather"})}},
        {"id": "call_b", "function": {"name": "get_time", "arguments": "{}"}},
    ]}}]}
    calls = pc._decode_tool_calls_openai_chat(body)
    assert [c["id"] for c in calls] == ["call_a", "call_b"]
    assert calls[0]["name"] == "web_search" and calls[0]["args"] == {"q": "weather"}
    assert calls[0]["args_ok"] is True


def test_decode_bad_args_marks_not_ok():
    body = {"choices": [{"message": {"tool_calls": [
        {"id": "call_x", "function": {"name": "web_search", "arguments": "{not json"}}]}}]}
    call = pc._decode_tool_calls_openai_chat(body)[0]
    assert call["args_ok"] is False and call["args_raw"] == "{not json" and call["args"] == {}


def test_encode_tools_openai_responses_and_decode():
    enc = pc._encode_tools_openai_responses(TOOLS)
    assert enc[0] == {"type": "function", "name": "web_search",
                      "description": "search the web", "parameters": TOOLS[0].parameters}
    body = {"output": [
        {"type": "function_call", "call_id": "fc_a", "name": "web_search",
         "arguments": json.dumps({"q": "x"})},
        {"type": "function_call", "call_id": "fc_b", "name": "get_time", "arguments": "{}"},
    ]}
    calls = pc._decode_tool_calls_openai_responses(body)
    assert [c["id"] for c in calls] == ["fc_a", "fc_b"]
    assert calls[0]["args"] == {"q": "x"}
