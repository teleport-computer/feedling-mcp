"""Unified provider-native tool loop (spec C2, plan Task 5).

Locks the P0 loop-behavior contract with a fake `provider_client.chat_completion_async`
(monkeypatched) plus recording injected callables — no real provider/DB/hosted access.
Dependency-clean per test_v2_dependency_direction.py (tool_loop.py must not import
hosted/agent_runtime/db).

Style: sync test functions driving `asyncio.run()` (matches tests/test_v2_worker.py),
not the pytest-asyncio marker, to avoid a plugin-config dependency.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import provider_client
from provider_types import ToolResult
from model_api_runtime.v2 import tool_loop


class _ScriptedProvider:
    """Records every chat_completion_async call and returns the next scripted dict."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []  # list of {"config":..., "messages":..., "tools":...}

    async def __call__(self, config, messages, *, tools=None):
        self.calls.append({"config": config, "messages": messages, "tools": tools})
        if not self.responses:
            raise AssertionError("provider called more times than scripted")
        return self.responses.pop(0)


class _RecordingReply:
    """`on_reply` is an ASYNC callable (BUG #1 fix, PR C final review): production
    callers enqueue a reply effect then `await asyncio.to_thread(apply_pending_effects,
    ...)` to offload the enclave-bound encrypted write off the event loop thread, so
    `run_tool_loop` awaits `on_reply` every time — mirror that contract here with an
    `async def __call__` rather than a plain sync callable."""

    def __init__(self):
        self.calls = []  # list of (text, final)

    async def __call__(self, text, *, final):
        self.calls.append((text, final))


class _RecordingDispatch:
    def __init__(self, result_text="tool-observation"):
        self.calls = []  # list of tool_calls lists
        self.result_text = result_text

    async def __call__(self, tool_calls):
        self.calls.append(list(tool_calls))
        return [ToolResult(call_id=tc.id, content=self.result_text) for tc in tool_calls]


class _RecordingFold:
    """`fold_new_messages` is an ASYNC callable (BUG-2 fix): it wraps an enclave-bound
    decrypt read, so `run_tool_loop` awaits it every round after the first — mirror that
    contract here with an `async def __call__` rather than a plain sync callable."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.call_count = 0

    async def __call__(self):
        self.call_count += 1
        if self.batches:
            return self.batches.pop(0)
        return []


class _RecordingBuildMessages:
    def __init__(self):
        self.calls = []  # list of (folded_inputs, prior_tool_results)

    def __call__(self, folded_inputs, prior_tool_results):
        self.calls.append((list(folded_inputs), list(prior_tool_results)))
        return [{"role": "user", "content": "turn"}]


def _noop_add_usage(usage):
    pass


def test_empty_tool_calls_is_final_reply_no_dispatch(monkeypatch):
    """P0: weak model 1 call 1 bubble — plain text with no tool_calls IS the final
    reply; dispatch_tools is never invoked; no forced second responder round-trip."""
    provider = _ScriptedProvider([
        {"reply": "hello", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    on_reply = _RecordingReply()
    dispatch = _RecordingDispatch()
    build_messages = _RecordingBuildMessages()
    fold = _RecordingFold([])

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=object(),
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=on_reply,
        fold_new_messages=fold,
        add_usage=_noop_add_usage,
        max_calls=5,
    ))

    assert on_reply.calls == [("hello", True)]
    assert dispatch.calls == []
    assert outcome.final_text == "hello"
    assert outcome.rounds == 1
    assert outcome.stop_reason == "final_text"
    assert outcome.replied_intermediate is False


def test_preamble_text_with_tool_calls_is_not_a_bubble(monkeypatch):
    """Text accompanying tool_calls is preamble/thinking, not a user bubble — closes
    the '我去查查' preamble-leaked-as-reply bug. Only the terminal no-tool-call
    text produces a bubble."""
    provider = _ScriptedProvider([
        {
            "reply": "let me look",
            "tool_calls": [{"id": "1", "name": "memory_search", "args": {"query": "x"}}],
            "usage": {},
        },
        {"reply": "final answer", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    on_reply = _RecordingReply()
    dispatch = _RecordingDispatch()
    build_messages = _RecordingBuildMessages()
    fold = _RecordingFold([])

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=object(),
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=on_reply,
        fold_new_messages=fold,
        add_usage=_noop_add_usage,
        max_calls=5,
    ))

    # "let me look" never reached on_reply.
    assert on_reply.calls == [("final answer", True)]
    # dispatch_tools WAS called, with the non-reply tool_call from round 1.
    assert len(dispatch.calls) == 1
    assert [tc.name for tc in dispatch.calls[0]] == ["memory_search"]
    assert outcome.final_text == "final answer"
    assert outcome.rounds == 2


def test_reply_special_tool_is_immediate_and_continues(monkeypatch):
    """reply{text} tool_call -> on_reply(text, final=False) fires immediately and the
    loop continues to a later terminal round."""
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{"id": "1", "name": "reply", "args": {"text": "我看看哈"}}],
            "usage": {},
        },
        {"reply": "done", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    on_reply = _RecordingReply()
    dispatch = _RecordingDispatch()
    build_messages = _RecordingBuildMessages()
    fold = _RecordingFold([])

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=object(),
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=on_reply,
        fold_new_messages=fold,
        add_usage=_noop_add_usage,
        max_calls=5,
    ))

    assert on_reply.calls == [("我看看哈", False), ("done", True)]
    # the reply tool_call is handled specially, not routed through dispatch_tools.
    assert dispatch.calls == []
    assert outcome.replied_intermediate is True
    assert outcome.final_text == "done"
    assert outcome.rounds == 2


def test_budget_bound_last_call_omits_tools_and_terminates(monkeypatch):
    """If every round returns a tool_call, the loop's LAST provider call
    (call_idx == max_calls-1) is made with tools=None so it always terminates with
    model text — never a filler bubble, never exceeding max_calls provider calls."""
    provider = _ScriptedProvider([
        {
            "reply": "looking 1",
            "tool_calls": [{"id": "1", "name": "memory_search", "args": {"query": "a"}}],
            "usage": {},
        },
        {
            "reply": "looking 2",
            "tool_calls": [{"id": "2", "name": "memory_search", "args": {"query": "b"}}],
            "usage": {},
        },
        # last call: tools=None -> model cannot return tool_calls, must terminate.
        {"reply": "final terminal text", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    on_reply = _RecordingReply()
    dispatch = _RecordingDispatch()
    build_messages = _RecordingBuildMessages()
    fold = _RecordingFold([])

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=object(),
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=on_reply,
        fold_new_messages=fold,
        add_usage=_noop_add_usage,
        max_calls=3,
    ))

    assert len(provider.calls) == 3
    assert provider.calls[0]["tools"] is not None
    assert provider.calls[1]["tools"] is not None
    assert provider.calls[2]["tools"] is None  # last call: tools omitted
    assert outcome.final_text == "final terminal text"
    assert outcome.rounds == 3
    assert outcome.stop_reason == "final_text"
    # never a filler: the terminal text is exactly what the model said.
    assert on_reply.calls[-1] == ("final terminal text", True)


def test_fold_before_each_call_after_first_feeds_next_build_messages(monkeypatch):
    """fold_new_messages() is invoked before each provider call AFTER the first; its
    returned messages appear (accumulated) in the next build_messages(folded_inputs, ...)."""
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{"id": "1", "name": "memory_search", "args": {"query": "a"}}],
            "usage": {},
        },
        {"reply": "final", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    on_reply = _RecordingReply()
    dispatch = _RecordingDispatch()
    build_messages = _RecordingBuildMessages()
    new_msg = {"role": "user", "content": "a new message that arrived mid-turn"}
    fold = _RecordingFold([[new_msg]])

    asyncio.run(tool_loop.run_tool_loop(
        provider_config=object(),
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=on_reply,
        fold_new_messages=fold,
        add_usage=_noop_add_usage,
        max_calls=5,
    ))

    # round 0 (call_idx==0): no fold yet.
    assert build_messages.calls[0][0] == []
    # round 1 (call_idx==1, after the first call): fold_new_messages was invoked once
    # and its result is folded into the next build_messages() folded_inputs.
    assert fold.call_count == 1
    assert build_messages.calls[1][0] == [new_msg]


def test_provider_call_exception_still_counts_a_model_call(monkeypatch):
    """BUG #3 (minor, metric): `TurnMetrics`'s docstring promises failed provider
    calls ARE counted (model_calls bumped, no token usage). If
    `provider_client.chat_completion_async` raises on the very first round,
    `add_usage(None)` must still fire exactly once, before the exception
    propagates out of `run_tool_loop` — otherwise a turn failing on its first
    provider call would flush model_calls=0."""

    class _RaisingProvider:
        async def __call__(self, config, messages, *, tools=None):
            raise RuntimeError("boom: provider unreachable")

    monkeypatch.setattr(provider_client, "chat_completion_async", _RaisingProvider())

    on_reply = _RecordingReply()
    dispatch = _RecordingDispatch()
    build_messages = _RecordingBuildMessages()
    fold = _RecordingFold([])

    usage_calls = []

    def _recording_add_usage(usage):
        usage_calls.append(usage)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=object(),
            build_messages=build_messages,
            dispatch_tools=dispatch,
            on_reply=on_reply,
            fold_new_messages=fold,
            add_usage=_recording_add_usage,
            max_calls=5,
        ))

    # add_usage(None) fired once (model_calls bumped, no usage) — the exception
    # still propagated (not swallowed), and on_reply was never reached.
    assert usage_calls == [None]
    assert on_reply.calls == []


def test_budget_exhausted_with_zero_max_calls_produces_no_reply(monkeypatch):
    """The `max_calls == 0` degenerate case: the loop body never runs (the `for
    call_idx in range(0)` is empty), so the provider is never called, `on_reply`
    is never invoked, and the loop falls through to the `budget_exhausted`
    LoopOutcome — the only way to reach that return line (see the comment above
    it in tool_loop.py). This is exactly the no-reply-produced shape worker.py's
    chat path (BUG #2 fix) must catch and turn into a failed turn rather than
    silently completing."""
    provider = _ScriptedProvider([])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    on_reply = _RecordingReply()
    dispatch = _RecordingDispatch()
    build_messages = _RecordingBuildMessages()
    fold = _RecordingFold([])

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=object(),
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=on_reply,
        fold_new_messages=fold,
        add_usage=_noop_add_usage,
        max_calls=0,
    ))

    assert provider.calls == []
    assert on_reply.calls == []
    assert outcome.final_text == ""
    assert outcome.rounds == 0
    assert outcome.stop_reason == "budget_exhausted"
    assert outcome.replied_intermediate is False
    # This is exactly the shape worker.py's chat-path BUG-2 guard checks:
    assert not outcome.replied_intermediate and not (outcome.final_text or "").strip()
