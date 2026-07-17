"""run_tool_loop accepts per-turn user-MCP tool specs: they are offered to the
provider, dispatched (not rejected as malformed despite no PARAMS entry), and —
per the trusted-MCP decision — do NOT lock durable writes for later rounds."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client
from provider_types import ToolResult, ToolSpec
from capabilities import registry as cap_registry
from model_api_runtime.v2 import tool_loop

MCP_SPEC = ToolSpec(name="mcp__weather__search", description="find weather",
                    parameters={"type": "object", "properties": {"q": {"type": "string"}}})


async def _on_reply_collect(store):
    async def _on_reply(text, *, final):
        store.append((text, final))
    return _on_reply


def _run(responses, dispatch, *, extra_tool_specs, provider_tools):
    async def _provider(_config, _messages, *, tools=None):
        provider_tools.append(tools)
        return next(responses)

    replies = []

    async def _on_reply(text, *, final):
        replies.append((text, final))

    async def _fold():
        return []

    import pytest  # noqa: F401
    orig = provider_client.chat_completion_async
    provider_client.chat_completion_async = _provider
    try:
        outcome = asyncio.run(tool_loop.run_tool_loop(
            provider_config=object(),
            build_messages=lambda _t: [{"role": "user", "content": "hi"}],
            dispatch_tools=dispatch,
            on_reply=_on_reply,
            fold_new_messages=_fold,
            add_usage=lambda _u: None,
            max_calls=4,
            extra_tool_specs=extra_tool_specs,
        ))
    finally:
        provider_client.chat_completion_async = orig
    return outcome, replies


def test_mcp_tool_is_offered_and_dispatched():
    responses = iter([
        {"reply": "", "usage": {}, "tool_calls": [
            {"id": "m1", "name": "mcp__weather__search", "args": {"q": "SF"}}]},
        {"reply": "it's sunny", "tool_calls": [], "usage": {}},
    ])
    dispatched = []

    async def _dispatch(calls):
        dispatched.extend(calls)
        return [ToolResult(call_id=tc.id, content="sunny 25C") for tc in calls]

    provider_tools = []
    outcome, replies = _run(responses, _dispatch,
                            extra_tool_specs=[MCP_SPEC], provider_tools=provider_tools)
    # offered to the provider in round 1
    assert "mcp__weather__search" in {s.name for s in provider_tools[0]}
    # dispatched (NOT rejected as malformed despite having no platform PARAMS entry)
    assert [tc.name for tc in dispatched] == ["mcp__weather__search"]
    assert outcome.final_text == "it's sunny"
    assert replies == [("it's sunny", True)]


def test_writes_stay_available_after_an_mcp_call():
    """Trusted-MCP decision: an MCP tool result does NOT revoke durable writes for
    the rest of the turn (contrast with web results, which do)."""
    responses = iter([
        {"reply": "", "usage": {}, "tool_calls": [
            {"id": "m1", "name": "mcp__weather__search", "args": {"q": "SF"}}]},
        {"reply": "done", "tool_calls": [], "usage": {}},
    ])

    async def _dispatch(calls):
        return [ToolResult(call_id=tc.id, content="external-ish text from user's own server")
                for tc in calls]

    provider_tools = []
    _run(responses, _dispatch, extra_tool_specs=[MCP_SPEC], provider_tools=provider_tools)
    second_names = {s.name for s in provider_tools[1]}
    assert cap_registry.WRITE_ACTIONS <= second_names           # writes NOT locked
    assert "mcp__weather__search" in second_names               # mcp tool still offered
    assert tool_loop.provenance.EXTERNAL_READS <= second_names  # web tools still offered too


def test_unknown_non_mcp_tool_still_rejected():
    """The mcp_names bypass must not blanket-accept arbitrary names — a name that
    is neither a platform tool nor an injected MCP tool is still malformed."""
    responses = iter([
        {"reply": "", "usage": {}, "tool_calls": [
            {"id": "x", "name": "totally_made_up_tool", "args": {}}]},
        {"reply": "fallback", "tool_calls": [], "usage": {}},
    ])

    async def _dispatch(_calls):
        raise AssertionError("unknown tool must never dispatch")

    provider_tools = []
    outcome, replies = _run(responses, _dispatch,
                            extra_tool_specs=[MCP_SPEC], provider_tools=provider_tools)
    assert provider_tools[-1] is None            # forced one tools-disabled fallback
    assert outcome.final_text == "fallback"


def test_no_extra_specs_is_unchanged_behavior():
    """extra_tool_specs=None keeps the plain platform catalog (no regression)."""
    responses = iter([{"reply": "just text", "tool_calls": [], "usage": {}}])

    async def _dispatch(_calls):
        return []

    provider_tools = []
    outcome, _ = _run(responses, _dispatch, extra_tool_specs=None,
                      provider_tools=provider_tools)
    names = {s.name for s in provider_tools[0]}
    assert not any(n.startswith("mcp__") for n in names)
    assert outcome.final_text == "just text"
