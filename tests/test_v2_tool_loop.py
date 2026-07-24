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
from provider_types import ToolExchange, ToolResult
from model_api_runtime.v2 import tool_loop


_TEST_PROVIDER_CONFIG = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-sonnet-4-test",
    api_key="test-key",
)


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

    async def __call__(self, text, *, final, reasoning=""):
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
        self.calls = []  # chronological transcript snapshots

    def __call__(self, transcript):
        self.calls.append(list(transcript))
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
    progress = []

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=on_reply,
        fold_new_messages=fold,
        add_usage=_noop_add_usage,
        max_calls=5,
        on_progress=progress.append,
    ))

    assert on_reply.calls == [("hello", True)]
    assert dispatch.calls == []
    assert outcome.final_text == "hello"
    assert outcome.rounds == 1
    assert outcome.stop_reason == "final_text"
    assert outcome.replied_intermediate is False
    assert progress == ["round_boundary", "provider_start", "provider_complete"]


def test_superseded_final_folds_new_input_and_retries_without_stale_transcript(
    monkeypatch,
):
    """A final candidate rejected at the durable publish CAS is not visible and
    is not fed back as an assistant turn. The next boundary folds B and asks the
    provider for one revised answer over the actual conversation."""
    provider = _ScriptedProvider([
        {"reply": "stale answer to A", "tool_calls": [], "usage": {}},
        {"reply": "revised answer to A and B", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    class _AtomicReply:
        def __init__(self):
            self.attempts = []
            self.visible = []

        async def __call__(self, text, *, final, reasoning=""):
            self.attempts.append((text, final))
            if text == "stale answer to A":
                raise tool_loop.FinalReplySuperseded()
            self.visible.append((text, final))

    reply = _AtomicReply()
    build_messages = _RecordingBuildMessages()
    late = {"id": "B", "role": "user", "content": "new input B"}
    fold = _RecordingFold([[], [late]])

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=build_messages,
        dispatch_tools=_RecordingDispatch(),
        on_reply=reply,
        fold_new_messages=fold,
        add_usage=_noop_add_usage,
        max_calls=3,
        fold_before_first=True,
    ))

    assert len(provider.calls) == 2
    assert reply.visible == [("revised answer to A and B", True)]
    assert reply.attempts == [
        ("stale answer to A", True),
        ("revised answer to A and B", True),
    ]
    assert build_messages.calls[0] == []
    assert build_messages.calls[1] == [late]
    assert all(
        "stale answer to A" not in str(item)
        for item in build_messages.calls[1]
    )
    assert outcome.final_text == "revised answer to A and B"
    assert outcome.stop_reason == "final_text"


def test_superseded_final_at_budget_returns_clean_handoff_outcome(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "stale terminal", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    async def reject_final(_text, *, final, reasoning=""):
        assert final is True
        raise tool_loop.FinalReplySuperseded()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=reject_final,
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=1,
    ))

    assert len(provider.calls) == 1
    assert outcome.final_text == ""
    assert outcome.rounds == 1
    assert outcome.stop_reason == "input_advanced"
    assert outcome.replied_intermediate is False


def test_child_loop_can_remove_reply_from_the_offered_catalog(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "child result", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=2,
        allow_reply_tool=False,
    ))

    offered = {spec.name for spec in provider.calls[0]["tools"]}
    assert "reply" not in offered
    assert outcome.final_text == "child result"


def test_task_result_is_external_and_removes_later_writes_and_recursion(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{
                "id": "task-1",
                "name": "task",
                "args": {"prompt": "inspect external evidence"},
            }],
            "usage": {},
        },
        {"reply": "done", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch(
        '{"status":"completed","summary":"untrusted child output"}'
    )

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=3,
    ))

    first = {spec.name for spec in provider.calls[0]["tools"]}
    second = {spec.name for spec in provider.calls[1]["tools"]}
    assert "task" in first
    assert "task" not in second
    assert "memory_write" not in second
    assert "workspace_write" not in second
    assert "web_search" not in second
    assert outcome.final_text == "done"


def test_intermediate_reply_remains_visible_when_later_final_is_superseded(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": "r1", "name": "reply", "args": {"text": "I am checking"}}
            ],
            "usage": {},
        },
        {"reply": "stale final", "tool_calls": [], "usage": {}},
        {"reply": "fresh final with B", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    visible = []

    async def publish(text, *, final, reasoning=""):
        if final and text == "stale final":
            raise tool_loop.FinalReplySuperseded()
        visible.append((text, final))

    late = {"id": "B", "role": "user", "content": "one more thing"}
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=publish,
        fold_new_messages=_RecordingFold([[], [late]]),
        add_usage=_noop_add_usage,
        max_calls=4,
    ))

    assert visible == [
        ("I am checking", False),
        ("fresh final with B", True),
    ]
    assert outcome.replied_intermediate is True
    assert outcome.final_text == "fresh final with B"


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
        provider_config=_TEST_PROVIDER_CONFIG,
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
        provider_config=_TEST_PROVIDER_CONFIG,
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
        provider_config=_TEST_PROVIDER_CONFIG,
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


def test_fold_at_every_round_boundary_feeds_chronological_transcript(monkeypatch):
    """The first boundary closes prompt-assembly races; later boundaries append
    newly arrived user messages after the preceding native tool exchange."""
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
    fold = _RecordingFold([[], [new_msg]])

    asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=on_reply,
        fold_new_messages=fold,
        add_usage=_noop_add_usage,
        max_calls=5,
        fold_before_first=True,
    ))

    # Round 0 performed an immediate empty fold at the seeded cursor.
    assert build_messages.calls[0] == []
    # Round 1 appends B after round 0's native exchange, without restart/debounce.
    assert fold.call_count == 2
    assert isinstance(build_messages.calls[1][0], ToolExchange)
    assert build_messages.calls[1][1] == new_msg


def test_native_tool_exchange_keeps_calls_results_and_midturn_user_order(monkeypatch):
    native = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{"id": "c1", "name": "memory_search", "args": {"query": "x"}}],
            "assistant_turn": {"wire": "openai_chat", "payload": native},
            "usage": {},
        },
        {"reply": "final", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    build_messages = _RecordingBuildMessages()
    new_msg = {"id": "m2", "role": "user", "content": "also check y"}

    asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=build_messages,
        dispatch_tools=_RecordingDispatch("found x"),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[], [new_msg]]),
        add_usage=_noop_add_usage,
        max_calls=5,
        fold_before_first=True,
    ))

    exchange = build_messages.calls[1][0]
    assert isinstance(exchange, ToolExchange)
    assert exchange.assistant_turn.payload is native
    assert [c.id for c in exchange.calls] == ["c1"]
    assert [(r.call_id, r.content) for r in exchange.results] == [("c1", "found x")]
    assert build_messages.calls[1][1] == new_msg


def test_reply_tool_also_gets_call_id_matched_result(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{"id": "r1", "name": "reply", "args": {"text": "one sec"}}],
            "usage": {},
        },
        {"reply": "done", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    build_messages = _RecordingBuildMessages()

    asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG, build_messages=build_messages,
        dispatch_tools=_RecordingDispatch(), on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]), add_usage=_noop_add_usage,
        max_calls=5,
    ))

    exchange = build_messages.calls[1][0]
    assert [r.call_id for r in exchange.results] == ["r1"]
    assert exchange.results[0].content.startswith("ok:")


def test_malformed_args_gets_one_tools_disabled_fallback_without_dispatch(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "", "tool_calls": [{"id": "c1", "name": "web_search",
                                             "args": {}, "args_raw": "{", "args_ok": False}],
            "usage": {},
        },
        {"reply": "I could not use tools, but here is the answer.", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch()
    reply = _RecordingReply()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG, build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch, on_reply=reply,
        fold_new_messages=_RecordingFold([[]]), add_usage=_noop_add_usage,
        max_calls=5,
    ))

    assert dispatch.calls == []
    assert [call["tools"] is None for call in provider.calls] == [False, True]
    assert reply.calls == [("I could not use tools, but here is the answer.", True)]
    assert outcome.rounds == 2


def test_duplicate_call_ids_fall_back_before_any_side_effect(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": "dup", "name": "memory_write", "args": {"actions": []}},
                {"id": "dup", "name": "memory_write", "args": {"actions": []}},
            ],
            "usage": {},
        },
        {"reply": "safe fallback", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch()
    reply = _RecordingReply()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG, build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch, on_reply=reply,
        fold_new_messages=_RecordingFold([[]]), add_usage=_noop_add_usage,
        max_calls=5,
    ))

    assert dispatch.calls == []
    assert [call["tools"] is None for call in provider.calls] == [False, True]
    assert reply.calls == [("safe fallback", True)]
    assert outcome.rounds == 2


def test_per_round_tool_call_ceiling_is_all_or_nothing(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": f"c{i}", "name": "memory_index", "args": {}}
                for i in range(3)
            ],
            "usage": {},
        },
        {"reply": "bounded fallback", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG, build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch, on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]), add_usage=_noop_add_usage,
        max_calls=5, max_tool_calls_per_round=2,
    ))

    assert dispatch.calls == []
    assert [call["tools"] is None for call in provider.calls] == [False, True]
    assert outcome.final_text == "bounded fallback"


def test_per_turn_tool_call_ceiling_rejects_only_the_overflow_batch(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "", "tool_calls": [
            {"id": "c1", "name": "memory_index", "args": {}}], "usage": {}},
        {"reply": "", "tool_calls": [
            {"id": "c2", "name": "memory_index", "args": {}}], "usage": {}},
        {"reply": "", "tool_calls": [
            {"id": "c3", "name": "memory_index", "args": {}}], "usage": {}},
        {"reply": "turn fallback", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG, build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch, on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[], [], []]), add_usage=_noop_add_usage,
        max_calls=5, max_tool_calls_per_turn=2,
    ))

    assert [[tc.id for tc in batch] for batch in dispatch.calls] == [["c1"], ["c2"]]
    assert [call["tools"] is None for call in provider.calls] == [False, False, False, True]
    assert outcome.final_text == "turn fallback"


@pytest.mark.parametrize("oversized_part", ["args", "native_turn", "assistant_text"])
def test_oversized_tool_exchange_falls_back_before_dispatch(
    monkeypatch,
    oversized_part,
):
    tool_call = {"id": "c1", "name": "memory_index", "args": {}}
    first = {
        "reply": "",
        "tool_calls": [tool_call],
        "usage": {},
    }
    kwargs = {
        "max_tool_args_chars": 32,
        "max_tool_batch_args_chars": 64,
        "max_native_assistant_turn_chars": 64,
        "max_assistant_tool_text_chars": 32,
    }
    if oversized_part == "args":
        tool_call["args"] = {"query": "x" * 100}
    elif oversized_part == "native_turn":
        first["assistant_turn"] = {
            "wire": "openai_chat",
            "payload": {"role": "assistant", "opaque": "x" * 100},
        }
    else:
        first["reply"] = "x" * 100

    provider = _ScriptedProvider([
        first,
        {"reply": "bounded fallback", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]),
        add_usage=_noop_add_usage,
        max_calls=5,
        **kwargs,
    ))

    assert dispatch.calls == []
    assert [call["tools"] is None for call in provider.calls] == [False, True]
    assert outcome.final_text == "bounded fallback"


def test_all_tool_results_share_per_call_and_aggregate_prompt_budgets(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": f"c{i}", "name": "memory_index", "args": {}}
                for i in range(5)
            ],
            "usage": {},
        },
        {"reply": "done", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    build_messages = _RecordingBuildMessages()

    async def _dispatch(calls):
        return [
            ToolResult(call_id=calls[0].id, content="error: denied"),
            *[
                ToolResult(call_id=tc.id, content=str(index) * 3000)
                for index, tc in enumerate(calls[1:], start=1)
            ],
        ]

    asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG, build_messages=build_messages,
        dispatch_tools=_dispatch, on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]), add_usage=_noop_add_usage,
        max_calls=5,
    ))

    exchange = build_messages.calls[1][0]
    assert isinstance(exchange, ToolExchange)
    assert [result.call_id for result in exchange.results] == [f"c{i}" for i in range(5)]
    assert exchange.results[0].content == "error: denied"
    assert all(len(result.content) <= 2000 for result in exchange.results)
    assert sum(len(result.content) for result in exchange.results) <= 8000
    assert all(result.content.endswith("...[truncated]") for result in exchange.results[1:])


@pytest.mark.parametrize("status_code", [400, 422])
def test_tool_schema_rejection_gets_exactly_one_tools_disabled_fallback(monkeypatch, status_code):
    class _RejectThenReply:
        def __init__(self):
            self.calls = []

        async def __call__(self, config, messages, *, tools=None):
            self.calls.append(tools)
            if len(self.calls) == 1:
                raise provider_client.ProviderError("tools rejected", status_code=status_code)
            return {"reply": "fallback answer", "tool_calls": [], "usage": {}}

    provider = _RejectThenReply()
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    usage = []
    reply = _RecordingReply()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG, build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(), on_reply=reply,
        fold_new_messages=_RecordingFold([[]]), add_usage=usage.append,
        max_calls=5,
    ))

    assert provider.calls[0] is not None
    assert provider.calls[1] is None
    assert len(provider.calls) == 2
    assert usage == [None, {}]
    assert reply.calls == [("fallback answer", True)]
    assert outcome.rounds == 2


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
            provider_config=_TEST_PROVIDER_CONFIG,
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
        provider_config=_TEST_PROVIDER_CONFIG,
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
