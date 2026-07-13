"""Task 6: `tools` threaded through chat_completion / chat_completion_async +
`tool_calls` always present in every wire's return dict (empty when no tools).

Covers all 4 wire handlers: anthropic, gemini, openai-compatible, openai-responses.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from provider_types import ToolSpec


TOOLS = [
    ToolSpec("web_search", "s", {"type": "object"}),
    ToolSpec("get_time", "t", {"type": "object"}),
]


class _Resp:
    status_code = 200

    def __init__(self, body):
        self._b = body

    def json(self):
        return self._b

    def raise_for_status(self):
        pass


def _fake_client(body):
    class C:
        def post(self, *a, **k):
            return _Resp(body)

    return C()


# --- anthropic ---------------------------------------------------------

def test_anthropic_returns_two_tool_calls(monkeypatch):
    body = {
        "content": [
            {"type": "tool_use", "id": "t_a", "name": "web_search", "input": {"q": "x"}},
            {"type": "tool_use", "id": "t_b", "name": "get_time", "input": {}},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "stop_reason": "tool_use",
    }
    monkeypatch.setattr(pc, "_http_client", lambda: _fake_client(body))
    cfg = pc.ProviderConfig("anthropic", "claude-x", "k", "https://api.anthropic.com/v1")
    res = pc.chat_completion(cfg, [{"role": "user", "content": "hi"}], tools=TOOLS, require_reply=False)
    assert [c["id"] for c in res["tool_calls"]] == ["t_a", "t_b"]


def test_anthropic_no_tools_empty_list(monkeypatch):
    body = {
        "content": [{"type": "text", "text": "hello"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    captured = {}

    class C:
        def post(self, url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _Resp(body)

    monkeypatch.setattr(pc, "_http_client", lambda: C())
    cfg = pc.ProviderConfig("anthropic", "claude-x", "k", "https://api.anthropic.com/v1")
    res = pc.chat_completion(cfg, [{"role": "user", "content": "hi"}])
    assert res["tool_calls"] == [] and res["reply"] == "hello"
    assert "tools" not in captured["payload"]


# --- gemini --------------------------------------------------------------

def test_gemini_returns_two_tool_calls(monkeypatch):
    body = {
        "candidates": [{"content": {"parts": [
            {"functionCall": {"name": "web_search", "args": {"q": "x"}}},
            {"functionCall": {"name": "get_time", "args": {}}},
        ]}}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2},
    }
    monkeypatch.setattr(pc, "_http_client", lambda: _fake_client(body))
    cfg = pc.ProviderConfig("gemini", "gemini-2.5-flash", "k", "https://generativelanguage.googleapis.com/v1beta")
    res = pc.chat_completion(cfg, [{"role": "user", "content": "hi"}], tools=TOOLS, require_reply=False)
    assert [c["name"] for c in res["tool_calls"]] == ["web_search", "get_time"]
    assert len(res["tool_calls"]) == 2


def test_gemini_no_tools_empty_list(monkeypatch):
    body = {
        "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
    }
    captured = {}

    class C:
        def post(self, url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _Resp(body)

    monkeypatch.setattr(pc, "_http_client", lambda: C())
    cfg = pc.ProviderConfig("gemini", "gemini-2.5-flash", "k", "https://generativelanguage.googleapis.com/v1beta")
    res = pc.chat_completion(cfg, [{"role": "user", "content": "hi"}])
    assert res["tool_calls"] == [] and res["reply"] == "hello"
    assert "tools" not in captured["payload"]


# --- openai-compatible -----------------------------------------------------

def test_openai_compatible_returns_two_tool_calls(monkeypatch):
    body = {
        "choices": [{"message": {"tool_calls": [
            {"id": "call_a", "function": {"name": "web_search", "arguments": json.dumps({"q": "x"})}},
            {"id": "call_b", "function": {"name": "get_time", "arguments": "{}"}},
        ]}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }
    monkeypatch.setattr(pc, "_http_client", lambda: _fake_client(body))
    cfg = pc.ProviderConfig("deepseek", "deepseek-v4-flash", "k", "https://api.deepseek.com")
    res = pc.chat_completion(cfg, [{"role": "user", "content": "hi"}], tools=TOOLS, require_reply=False)
    assert [c["id"] for c in res["tool_calls"]] == ["call_a", "call_b"]


def test_openai_compatible_no_tools_empty_list(monkeypatch):
    body = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    captured = {}

    class C:
        def post(self, url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _Resp(body)

    monkeypatch.setattr(pc, "_http_client", lambda: C())
    cfg = pc.ProviderConfig("deepseek", "deepseek-v4-flash", "k", "https://api.deepseek.com")
    res = pc.chat_completion(cfg, [{"role": "user", "content": "hi"}])
    assert res["tool_calls"] == [] and res["reply"] == "hello"
    assert "tools" not in captured["payload"]


# --- openai-responses -------------------------------------------------------

def test_openai_responses_returns_two_tool_calls(monkeypatch):
    body = {
        "output": [
            {"type": "function_call", "call_id": "call_a", "name": "web_search", "arguments": json.dumps({"q": "x"})},
            {"type": "function_call", "call_id": "call_b", "name": "get_time", "arguments": "{}"},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    monkeypatch.setattr(pc, "_http_client", lambda: _fake_client(body))
    cfg = pc.ProviderConfig("openai", "gpt-5", "k", "https://api.openai.com/v1")
    res = pc.chat_completion(cfg, [{"role": "user", "content": "hi"}], tools=TOOLS, require_reply=False)
    assert [c["id"] for c in res["tool_calls"]] == ["call_a", "call_b"]


def test_openai_responses_no_tools_empty_list(monkeypatch):
    body = {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    captured = {}

    class C:
        def post(self, url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _Resp(body)

    monkeypatch.setattr(pc, "_http_client", lambda: C())
    cfg = pc.ProviderConfig("openai", "gpt-5", "k", "https://api.openai.com/v1")
    res = pc.chat_completion(cfg, [{"role": "user", "content": "hi"}])
    assert res["tool_calls"] == [] and res["reply"] == "hello"
    assert "tools" not in captured["payload"]


# --- chat_completion_async: tools threaded through all 4 native-async paths
# (openai-compat / anthropic / gemini / responses — no thread-bridge, PR B
# Task 7) --------------------------------------------------------------

def test_chat_completion_async_openai_compatible_threads_tools(monkeypatch):
    import anyio

    body = {
        "choices": [{"message": {"tool_calls": [
            {"id": "call_a", "function": {"name": "web_search", "arguments": json.dumps({"q": "x"})}},
        ]}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    class AC:
        async def post(self, url, headers=None, json=None, timeout=None):
            return _Resp(body)

    monkeypatch.setattr(pc, "_async_http_client", lambda: AC())
    cfg = pc.ProviderConfig("deepseek", "deepseek-v4-flash", "k", "https://api.deepseek.com")

    async def run():
        return await pc.chat_completion_async(
            cfg, [{"role": "user", "content": "hi"}], tools=TOOLS, require_reply=False,
        )

    res = anyio.run(run)
    assert [c["id"] for c in res["tool_calls"]] == ["call_a"]


def test_chat_completion_async_anthropic_native_threads_tools(monkeypatch):
    import anyio

    body = {
        "content": [{"type": "tool_use", "id": "t_a", "name": "web_search", "input": {"q": "x"}}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "stop_reason": "tool_use",
    }

    class AC:
        async def post(self, url, headers=None, json=None, timeout=None):
            return _Resp(body)

    monkeypatch.setattr(pc, "_async_http_client", lambda: AC())
    cfg = pc.ProviderConfig("anthropic", "claude-x", "k", "https://api.anthropic.com/v1")

    async def run():
        return await pc.chat_completion_async(
            cfg, [{"role": "user", "content": "hi"}], tools=TOOLS, require_reply=False,
        )

    res = anyio.run(run)
    assert [c["id"] for c in res["tool_calls"]] == ["t_a"]
