import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from provider_types import (
    NativeAssistantTurn,
    ProviderResponse,
    ToolCall,
    ToolExchange,
    ToolResult,
    ToolSpec,
    Usage,
)


def test_types_construct():
    assert ToolSpec(
        name="web_search", description="search", parameters={"type": "object"}
    ).name == "web_search"
    tc = ToolCall(id="c1", name="web_search", args={"q": "hi"})
    assert tc.args_ok is True and tc.args_raw == ""
    assert ToolResult(call_id="c1", content="ok").call_id == "c1"
    assert Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15).total_tokens == 15
    native = NativeAssistantTurn("openai_chat", {"role": "assistant"})
    exchange = ToolExchange(
        calls=(tc,), results=(ToolResult("c1", "ok"),), assistant_turn=native)
    assert exchange.assistant_turn is native


def test_provider_response_from_result():
    result = {
        "reply": "hello",
        "tool_calls": [{"id": "c1", "name": "web_search", "args": {"q": "x"},
                        "args_raw": "", "args_ok": True}],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 4,
            "total_tokens": 7,
            "cache_read_tokens": 2,
            "cache_write_tokens": 1,
            "cache_miss_tokens": 0,
        },
        "assistant_turn": {
            "wire": "openai_chat",
            "payload": {"role": "assistant", "tool_calls": [{"id": "c1"}]},
        },
    }
    pr = ProviderResponse.from_result(result)
    assert pr.text == "hello"
    assert pr.tool_calls == [ToolCall(id="c1", name="web_search", args={"q": "x"})]
    assert pr.usage == Usage(
        prompt_tokens=3,
        completion_tokens=4,
        total_tokens=7,
        cache_read_tokens=2,
        cache_write_tokens=1,
        cache_miss_tokens=0,
    )
    assert pr.raw is result
    assert pr.assistant_turn == NativeAssistantTurn(
        "openai_chat", {"role": "assistant", "tool_calls": [{"id": "c1"}]})


def test_from_result_defaults_missing_tool_calls_and_usage():
    pr = ProviderResponse.from_result({"reply": "hi"})
    assert pr.tool_calls == [] and pr.text == "hi"
    assert pr.usage == Usage(prompt_tokens=None, completion_tokens=None, total_tokens=None)
