import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from provider_types import ToolSpec, ToolResult

TOOLS = [ToolSpec("web_search", "search", {"type": "object", "properties": {"q": {"type": "string"}}}),
         ToolSpec("get_time", "time", {"type": "object", "properties": {}})]


def test_encode_tools_anthropic():
    enc = pc._encode_tools_anthropic(TOOLS)
    assert enc[0] == {"name": "web_search", "description": "search",
                      "input_schema": TOOLS[0].parameters}


def test_decode_two_tool_uses_anthropic():
    body = {"content": [
        {"type": "text", "text": "let me look"},
        {"type": "tool_use", "id": "toolu_a", "name": "web_search", "input": {"q": "x"}},
        {"type": "tool_use", "id": "toolu_b", "name": "get_time", "input": {}},
    ]}
    calls = pc._decode_tool_calls_anthropic(body)
    assert [c["id"] for c in calls] == ["toolu_a", "toolu_b"]
    assert calls[0]["name"] == "web_search" and calls[0]["args"] == {"q": "x"}
    assert calls[0]["args_ok"] is True


def test_encode_tool_results_anthropic():
    enc = pc._encode_tool_results_anthropic([ToolResult("toolu_a", "sunny"),
                                             ToolResult("toolu_b", "12:00")])
    assert enc == [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_a", "content": "sunny"},
        {"type": "tool_result", "tool_use_id": "toolu_b", "content": "12:00"}]}]
