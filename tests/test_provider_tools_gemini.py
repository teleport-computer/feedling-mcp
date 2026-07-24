import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from provider_types import ToolSpec, ToolResult

TOOLS = [ToolSpec("web_search", "search", {"type": "object", "properties": {"q": {"type": "string"}}}),
         ToolSpec("get_time", "time", {"type": "object", "properties": {}})]


def test_encode_tools_gemini():
    enc = pc._encode_tools_gemini(TOOLS)
    assert enc == [{"functionDeclarations": [
        {"name": "web_search", "description": "search", "parameters": TOOLS[0].parameters},
        {"name": "get_time", "description": "time", "parameters": TOOLS[1].parameters}]}]


def test_decode_two_function_calls_gemini_synthesizes_ids():
    body = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "web_search", "args": {"q": "x"}}},
        {"functionCall": {"name": "get_time", "args": {}}}]}}]}
    calls = pc._decode_tool_calls_gemini(body)
    assert [c["id"] for c in calls] == ["call_0_web_search", "call_1_get_time"]
    assert calls[0]["name"] == "web_search" and calls[0]["args"] == {"q": "x"}


def test_same_tool_twice_disambiguated_by_index():
    body = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "web_search", "args": {"q": "a"}}},
        {"functionCall": {"name": "web_search", "args": {"q": "b"}}}]}}]}
    calls = pc._decode_tool_calls_gemini(body)
    assert [c["id"] for c in calls] == ["call_0_web_search", "call_1_web_search"]


def test_encode_tool_results_gemini_by_name_from_map():
    id_to_name = {"call_0_web_search": "web_search", "call_1_get_time": "get_time"}
    enc = pc._encode_tool_results_gemini(
        [ToolResult("call_0_web_search", "sunny"), ToolResult("call_1_get_time", "12:00")], id_to_name)
    assert enc == [{"role": "user", "parts": [
        {"functionResponse": {"name": "web_search", "response": {"content": "sunny"}}},
        {"functionResponse": {"name": "get_time", "response": {"content": "12:00"}}}]}]
