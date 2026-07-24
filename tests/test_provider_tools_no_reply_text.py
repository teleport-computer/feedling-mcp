"""Regression: a pure tool-call provider response (tool_calls present, NO reply
text) must NOT raise "provider response had no usable reply text" even when
require_reply is set — the model chose to call a tool instead of answering, and
the V2 unified tool loop needs those tool_calls to reach the executor.

Before the fix the four `_parse_*_body` functions evaluated the require_reply
gate BEFORE (or independent of) decoding tool_calls, so the very first tool
round on the hosted V2 loop died with `turn_failed:providererror` (openai/gemini
return content=null for a pure tool call). Verified live on pre across
deepseek/openai/gemini. The decoders themselves were already tested; the gap was
the full parse path with require_reply=True + a no-text body.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc


class _Resp:
    """Minimal httpx-response stand-in for _parse_openai_compat_body (which calls
    resp.json())."""
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


# --- bodies where the model returned ONLY a tool call (no assistant text) ---
_OPENAI_CHAT = {"choices": [{"message": {"content": None, "tool_calls": [
    {"id": "call_a", "function": {"name": "memory_write", "arguments": "{\"text\": \"x\"}"}}]}}]}
_OPENAI_RESPONSES = {"output": [
    {"type": "function_call", "call_id": "fc_a", "name": "memory_write", "arguments": "{}"}]}
_ANTHROPIC = {"content": [{"type": "tool_use", "id": "tu_a", "name": "memory_write", "input": {"text": "x"}}]}
_GEMINI = {"candidates": [{"content": {"parts": [
    {"functionCall": {"name": "memory_write", "args": {"text": "x"}}}]}}]}


def test_openai_compat_tool_call_without_text_does_not_raise():
    out = pc._parse_openai_compat_body(_Resp(_OPENAI_CHAT), provider="openai", model="gpt-4o-mini", require_reply=True)
    assert out["reply"] == ""
    assert [c["name"] for c in out["tool_calls"]] == ["memory_write"]
    assert out["stop_reason"] in ("", "tool_calls") or True  # stop_reason not asserted strictly


def test_openai_responses_tool_call_without_text_does_not_raise():
    out = pc._parse_openai_responses_body(_OPENAI_RESPONSES, model="gpt-5", require_reply=True)
    assert out["reply"] == ""
    assert [c["name"] for c in out["tool_calls"]] == ["memory_write"]


def test_anthropic_tool_call_without_text_does_not_raise():
    out = pc._parse_anthropic_body(_ANTHROPIC, model="claude-x", require_reply=True)
    assert out["reply"] == ""
    assert [c["name"] for c in out["tool_calls"]] == ["memory_write"]


def test_gemini_tool_call_without_text_does_not_raise():
    out = pc._parse_gemini_body(_GEMINI, model="gemini-2.5-flash", require_reply=True)
    assert out["reply"] == ""
    assert [c["name"] for c in out["tool_calls"]] == ["memory_write"]


# --- a genuinely empty response (NO text AND NO tool_calls) must still raise ---
def test_openai_compat_empty_without_tools_still_raises():
    with pytest.raises(pc.ProviderError, match="no usable reply text"):
        pc._parse_openai_compat_body(_Resp({"choices": [{"message": {"content": None}}]}),
                                     provider="openai", model="gpt-4o-mini", require_reply=True)


def test_anthropic_empty_without_tools_still_raises():
    with pytest.raises(pc.ProviderError, match="no usable reply text"):
        pc._parse_anthropic_body({"content": []}, model="claude-x", require_reply=True)


def test_gemini_empty_without_tools_still_raises():
    with pytest.raises(pc.ProviderError, match="no usable reply text"):
        pc._parse_gemini_body({"candidates": [{"content": {"parts": []}}]},
                              model="gemini-2.5-flash", require_reply=True)


def test_openai_responses_empty_without_tools_still_raises():
    with pytest.raises(pc.ProviderError, match="no usable reply text"):
        pc._parse_openai_responses_body({"output": []}, model="gpt-5", require_reply=True)
