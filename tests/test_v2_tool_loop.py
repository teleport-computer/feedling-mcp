"""Unified provider-native tool-loop contract (retained decision §C2).

Locks the P0 loop-behavior contract with a fake `provider_client.chat_completion_async`
(monkeypatched) plus recording injected callables — no real provider/DB/hosted access.
Dependency-clean per test_v2_dependency_direction.py (tool_loop.py must not import
hosted/agent_runtime/db).

Style: sync test functions driving `asyncio.run()` (matches tests/test_v2_worker.py),
not the pytest-asyncio marker, to avoid a plugin-config dependency.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import provider_client
from capabilities import registry as cap_registry
from capabilities import tool_schema as cap_tool_schema
from provider_types import ProviderMedia, ToolExchange, ToolResult
from model_api_runtime.v2 import executor as v2_executor
from model_api_runtime.v2 import language_follow
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

    async def __call__(self, config, messages, *, tools=None, **kwargs):
        self.calls.append({
            "config": config,
            "messages": messages,
            "tools": tools,
            **kwargs,
        })
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


class _TranscriptBuildMessages(_RecordingBuildMessages):
    """Expose the loop transcript to the provider-shaped request fixture."""

    def __call__(self, transcript):
        self.calls.append(list(transcript))
        return [{"role": "user", "content": "turn"}, *transcript]


class _AdaptiveBuildMessages(_RecordingBuildMessages):
    """Production-shaped builder whose planner owns the final message list."""

    def plan_provider_round(
        self,
        *,
        transcript,
        tools,
        required_tool_names,
        protected_tool_names,
        collapsed_tool_specs,
        recovery_tool_name,
        recovery_tool_active,
        tool_schema_collapse_policy,
        model_limit,
        output_reserve_tokens,
        safety_margin_tokens,
        utf8_bytes_per_token,
        image_reserve_tokens,
        system_suffix="",
    ):
        messages = [{"role": "system", "content": "base"}]
        if system_suffix:
            messages[0]["content"] += "\n\n" + system_suffix
        plan = tool_loop.prompt_frontier.plan_provider_round(
            model_limit=model_limit,
            messages=messages,
            tools=tools,
            required_tool_names=required_tool_names,
            protected_tool_names=protected_tool_names,
            collapsed_tool_specs=collapsed_tool_specs,
            recovery_tool_name=recovery_tool_name,
            recovery_tool_active=recovery_tool_active,
            tool_schema_collapse_policy=tool_schema_collapse_policy,
            output_reserve_tokens=output_reserve_tokens,
            safety_margin_tokens=safety_margin_tokens,
            utf8_bytes_per_token=utf8_bytes_per_token,
            image_reserve_tokens=image_reserve_tokens,
        )
        return messages, plan, None


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


def test_tool_loop_threads_visual_fallback_deadline_to_main_provider(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "hello", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(
        provider_client, "reliable_chat_completion_async", provider
    )
    deadline = 12345.5

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=1,
        absolute_deadline=deadline,
    ))

    assert outcome.final_text == "hello"
    assert provider.calls[0]["absolute_deadline"] == deadline


def test_provider_call_trace_failure_does_not_change_the_reply(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "hello", "stop_reason": "end_turn", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    on_reply = _RecordingReply()

    async def broken_trace(_event_kind, _detail):
        raise RuntimeError("telemetry unavailable")

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=on_reply,
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=1,
        on_provider_call_event=broken_trace,
    ))

    assert outcome.final_text == "hello"
    assert outcome.stop_reason == "final_text"
    assert on_reply.calls == [("hello", True)]


def test_new_input_cancels_old_final_reply_correction_before_retry(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "old candidate", "tool_calls": [], "usage": {}},
        {"reply": "answer for new input", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []
    cancelled = []
    first = True

    async def publish(text, *, final, reasoning="", correction_outcome=""):
        nonlocal first
        if first:
            first = False
            return tool_loop.FinalReplyCorrectionRequest(
                instruction="rewrite old candidate",
                original_text=text,
                original_reasoning=reasoning,
                on_cancel=lambda: cancelled.append(True),
            )
        published.append((text, final, correction_outcome))
        return None

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_AdaptiveBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=publish,
        fold_new_messages=_RecordingFold([[
            {"role": "user", "content": "new input"},
        ]]),
        add_usage=_noop_add_usage,
        max_calls=5,
    ))

    assert cancelled == [True]
    assert published == [("answer for new input", True, "")]
    assert outcome.final_text == "answer for new input"
    assert len(provider.calls) == 2
    assert "rewrite old candidate" not in provider.calls[1]["messages"][0]["content"]
    assert provider.calls[1]["tools"] is not None


def test_final_reply_correction_is_bounded_to_exactly_one_retry(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "original candidate", "tool_calls": [], "usage": {}},
        {"reply": "still mismatched", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    decisions = []
    published = []

    async def always_request_correction(
        text, *, final, reasoning="", correction_outcome=""
    ):
        decisions.append((text, final, correction_outcome))
        if correction_outcome:
            published.append((text, correction_outcome))
            return None
        return tool_loop.FinalReplyCorrectionRequest(
            instruction="rewrite in the user's language",
            original_text=text,
            original_reasoning=reasoning,
        )

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_AdaptiveBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=always_request_correction,
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=5,
    ))

    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None
    assert decisions == [
        ("original candidate", True, ""),
        ("still mismatched", True, ""),
        ("original candidate", True, "skipped"),
    ]
    assert published == [("original candidate", "skipped")]
    assert outcome.final_text == "original candidate"
    assert outcome.rounds == 2
    assert outcome.stop_reason == "final_text"


def test_memory_delete_surface_defaults_fail_closed(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "done", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=2,
    ))

    memory_spec = next(
        spec for spec in provider.calls[0]["tools"] if spec.name == "memory_write"
    )
    ops = memory_spec.parameters["properties"]["actions"]["items"][
        "properties"
    ]["op"]["enum"]
    assert ops == ["add", "update"]


def test_tagged_screen_images_retry_once_without_frames(monkeypatch):
    provider = _ScriptedProvider([
        # OpenRouter commonly reports an image rejection as 404 even when the
        # configured text model itself is valid.
        provider_client.ProviderError("images unsupported", status_code=404),
        {"reply": "text fallback", "tool_calls": [], "usage": {}},
    ])

    async def scripted(config, messages, *, tools=None, **kwargs):
        provider.calls.append({"config": config, "messages": messages, "tools": tools, **kwargs})
        item = provider.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(provider_client, "chat_completion_async", scripted)
    rejected = []
    usage = []
    on_reply = _RecordingReply()
    tagged = {
        "role": "user",
        "content": [
            {"type": "text", "text": "untrusted frame"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        ],
        "_screen_test": True,
    }

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=lambda _transcript: [tagged, {"role": "user", "content": "hi"}],
        dispatch_tools=_RecordingDispatch(),
        on_reply=on_reply,
        fold_new_messages=_RecordingFold([]),
        add_usage=usage.append,
        max_calls=3,
        tagged_image_message_key="_screen_test",
        on_tagged_images_rejected=lambda exc: rejected.append(type(exc).__name__),
    ))

    assert len(provider.calls) == 2
    assert tagged in provider.calls[0]["messages"]
    assert tagged not in provider.calls[1]["messages"]
    assert rejected == ["ProviderError"]
    assert usage == [None, {}]
    assert outcome.final_text == "text fallback"


def test_tagged_image_verdict_is_not_persisted_when_text_retry_also_fails(
    monkeypatch,
):
    provider = _ScriptedProvider([
        provider_client.ProviderError("images unsupported", status_code=404),
        provider_client.ProviderError("route unavailable", status_code=503),
    ])

    async def scripted(config, messages, *, tools=None, **kwargs):
        provider.calls.append({"config": config, "messages": messages, "tools": tools, **kwargs})
        item = provider.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(provider_client, "chat_completion_async", scripted)
    rejected = []
    tagged = {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        ],
        "_screen_test": True,
    }

    with pytest.raises(provider_client.ProviderError):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=lambda _transcript: [tagged],
            dispatch_tools=_RecordingDispatch(),
            on_reply=_RecordingReply(),
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            max_calls=2,
            tagged_image_message_key="_screen_test",
            on_tagged_images_rejected=lambda exc: rejected.append(exc),
        ))

    assert len(provider.calls) == 2
    assert rejected == []


def test_initial_outbound_fence_is_armed_before_first_provider_call(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "offline answer", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=2,
        initial_screen_pixels_blocked=True,
    ))

    offered = {spec.name for spec in (provider.calls[0]["tools"] or ())}
    assert {"web_search", "web_fetch", "task"}.isdisjoint(offered)


def test_foreground_screen_context_does_not_remove_write_surface(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "已看到", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    screen_message = {
        "role": "user",
        "content": [{"type": "text", "text": "ocr_text (untrusted): note"}],
        "_screen_test": True,
    }

    asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=lambda _transcript: [screen_message],
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=2,
        initial_screen_pixels_blocked=False,
    ))

    offered = {spec.name for spec in provider.calls[0]["tools"]}
    identity_writes = {
        name for name in cap_registry.WRITE_ACTIONS if name.startswith("identity_")
    }
    assert identity_writes <= offered
    assert {"memory_write", "schedule_wake"} <= offered
    assert "reply" not in offered


def test_provider_image_is_a_terminal_reply_without_synthetic_text(monkeypatch):
    provider = _ScriptedProvider(
        [
            {
                "reply": "",
                "tool_calls": [],
                "media": [
                    {
                        "mime_type": "image/png",
                        "data_base64": "aW1hZ2U=",
                        "name": "result.png",
                    }
                ],
                "usage": {},
            }
        ]
    )
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []
    trajectory = []

    async def on_reply(text, *, final, reasoning="", media=()):
        published.append((text, final, reasoning, media))

    async def on_trajectory(kind, payload):
        trajectory.append((kind, payload))

    outcome = asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=on_reply,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            max_calls=2,
            allow_image_output=True,
            on_trajectory_event=on_trajectory,
        )
    )

    assert provider.calls[0]["allow_image_output"] is True
    assert published[0][0:3] == ("", True, "")
    assert len(published[0][3]) == 1
    assert outcome.stop_reason == "final_media"
    assert outcome.delivered_media_count == 1
    response_event = next(payload for kind, payload in trajectory if kind == "provider_response")
    assert response_event["response"]["media"] == [
        {"mime_type": "image/png", "encoded_chars": 8}
    ]


def test_provider_media_mixed_with_calls_uses_text_only_tool_choice(monkeypatch):
    provider = _ScriptedProvider(
        [
            {
                "reply": "",
                "tool_calls": [
                    {"id": "mixed-call", "name": "workspace_list", "args": {}}
                ],
                "media": [
                    {
                        "mime_type": "image/png",
                        "data_base64": "aW1hZ2U=",
                    }
                ],
                "usage": {},
            },
            {"reply": "bounded fallback", "tool_calls": [], "usage": {}},
        ]
    )
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch()

    outcome = asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=dispatch,
            on_reply=_RecordingReply(),
            fold_new_messages=_RecordingFold([[]]),
            add_usage=_noop_add_usage,
            max_calls=3,
            allow_image_output=True,
        )
    )

    assert dispatch.calls == []
    assert provider.calls[0]["allow_image_output"] is True
    assert {spec.name for spec in provider.calls[1]["tools"]} == {
        "workspace_list"
    }
    assert provider.calls[1]["tool_choice"] == "none"
    assert "allow_image_output" not in provider.calls[1]
    assert outcome.final_text == "bounded fallback"


def test_transient_empty_provider_response_retries_inside_same_round(monkeypatch):
    calls = 0

    async def provider(config, messages, *, tools=None, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise provider_client.ProviderError(
                "provider response had no usable reply text"
            )
        return {"reply": "recovered", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    on_reply = _RecordingReply()
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_AdaptiveBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=on_reply,
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=2,
    ))

    assert calls == 2
    assert outcome.rounds == 1
    assert outcome.final_text == "recovered"
    assert on_reply.calls == [("recovered", True)]


def test_foreground_abnormal_empty_completion_fails_without_retry(monkeypatch):
    """A structurally valid but content-free success is a V2 policy failure.

    This catches parser strictness regressing and turning one abnormal HTTP 200
    completion into two identical, billed reliable-wrapper attempts.
    """
    provider = _ScriptedProvider([{
        "reply": "",
        "reasoning": "",
        "stop_reason": "",
        "tool_calls": [],
        "usage": {"prompt_tokens": 18504, "completion_tokens": 3},
    }])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    with pytest.raises(tool_loop.ProviderEmptyReply, match="empty_reply"):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=_RecordingReply(),
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            max_calls=5,
        ))

    assert len(provider.calls) == 1
    assert provider.calls[0]["require_reply"] is False


def test_foreground_semantic_empty_response_gets_one_correction(monkeypatch):
    """Thinking-only output gets one temporary correction, without surfacing it."""
    provider = _ScriptedProvider([
        {
            "reply": "",
            "reasoning": "private first attempt",
            "stop_reason": "max_tokens",
            "tool_calls": [],
            "usage": {"completion_tokens": 4096},
        },
        {
            "reply": "recovered",
            "reasoning": "final reasoning",
            "stop_reason": "end_turn",
            "tool_calls": [],
            "usage": {"completion_tokens": 4},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []
    surfaces = []

    async def publish(text, *, final, reasoning=""):
        published.append((text, final, reasoning))

    async def record_surface(detail):
        surfaces.append(detail)

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_AdaptiveBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=publish,
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=5,
        on_provider_tool_surface=record_surface,
    ))

    assert outcome.final_text == "recovered"
    assert published == [("recovered", True, "final reasoning")]
    assert len(provider.calls) == 2
    correction = provider.calls[1]["messages"][0]
    assert correction["role"] == "system"
    assert "Do not return a thinking-only response" in correction["content"]
    assert [item["empty_response_recovery"] for item in surfaces] == [
        False,
        True,
    ]
    assert all(
        item["force_text_fallback_reason"] == "none" for item in surfaces
    )


def test_repeated_semantic_empty_fails_after_one_correction(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "reasoning": "private work with no visible answer",
            "stop_reason": "end_turn",
            "tool_calls": [],
            "usage": {"completion_tokens": 3930},
        },
        {
            "reply": "",
            "reasoning": "still no new visible answer",
            "stop_reason": "end_turn",
            "tool_calls": [],
            "usage": {"completion_tokens": 5},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    on_reply = _RecordingReply()

    with pytest.raises(tool_loop.ProviderEmptyReply, match="empty_reply"):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_AdaptiveBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=on_reply,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            max_calls=5,
        ))

    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is not None
    assert "without visible text" in provider.calls[1]["messages"][0]["content"]
    assert on_reply.calls == []


def test_serialized_upstream_response_envelope_gets_one_correction(monkeypatch):
    """usr_90184: a relay serialized Gemini's whole body as visible text."""
    leaked = json.dumps(
        {
            "response": {
                "candidates": [{"content": {}}],
                "usageMetadata": {
                    "promptTokenCount": 23894,
                    "totalTokenCount": 24515,
                    "thoughtsTokenCount": 621,
                },
                "modelVersion": "gemini-3-flash",
                "responseId": "OTV9aoyAI9ronsEPuOK1kAM",
            },
            "traceId": "491379ee31653ba7",
            "metadata": {},
        },
        ensure_ascii=False,
    )
    provider = _ScriptedProvider(
        [
            {
                "reply": leaked,
                "reasoning": "",
                "stop_reason": "",
                "tool_calls": [],
                "usage": {"prompt_tokens": 23894, "completion_tokens": 621},
            },
            {
                "reply": "正常回复",
                "reasoning": "",
                "stop_reason": "end_turn",
                "tool_calls": [],
                "usage": {"completion_tokens": 8},
            },
        ]
    )
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []
    provider_successes = []
    events = []

    async def publish(text, *, final, reasoning=""):
        published.append((text, final, reasoning))

    async def on_provider_success():
        provider_successes.append(True)

    async def record(kind, payload):
        events.append((kind, payload))

    outcome = asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_AdaptiveBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=publish,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            max_calls=5,
            on_provider_success=on_provider_success,
            on_trajectory_event=record,
        )
    )

    assert outcome.final_text == "正常回复"
    assert published == [("正常回复", True, "")]
    assert provider_successes == [True]
    assert len(provider.calls) == 2
    correction = provider.calls[1]["messages"][0]
    assert "without visible text" in correction["content"]
    empty_event = next(
        payload for kind, payload in events if kind == "empty_provider_response"
    )
    assert empty_event["reason"] == "upstream_response_envelope"
    assert empty_event["response_shape"]["has_visible_text"] is True
    assert leaked not in str(empty_event)


def test_repeated_upstream_response_envelope_fails_without_publishing(monkeypatch):
    leaked = json.dumps(
        {
            "response": {
                "candidates": [{"content": {}}],
                "usageMetadata": {"totalTokenCount": 24515},
                "modelVersion": "gemini-3-flash",
                "responseId": "response-id",
            },
            "traceId": "trace-id",
            "metadata": {},
        }
    )
    provider = _ScriptedProvider(
        [
            {"reply": leaked, "tool_calls": [], "usage": {}},
            {"reply": leaked, "tool_calls": [], "usage": {}},
        ]
    )
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = _RecordingReply()

    with pytest.raises(tool_loop.ProviderEmptyReply, match="empty_reply"):
        asyncio.run(
            tool_loop.run_tool_loop(
                provider_config=_TEST_PROVIDER_CONFIG,
                build_messages=_AdaptiveBuildMessages(),
                dispatch_tools=_RecordingDispatch(),
                on_reply=published,
                fold_new_messages=_RecordingFold([[]]),
                add_usage=_noop_add_usage,
                max_calls=5,
            )
        )

    assert len(provider.calls) == 2
    assert published.calls == []


def test_semantic_empty_correction_retains_real_tool_flow(monkeypatch):
    """Recovery must not disable the safe catalog or break native exchanges."""
    provider = _ScriptedProvider([
        {
            "reply": "",
            "reasoning": "I should inspect memory first.",
            "stop_reason": "other",
            "tool_calls": [],
            "usage": {"completion_tokens": 4096},
        },
        {
            "reply": "",
            "reasoning": "",
            "stop_reason": "tool_use",
            "tool_calls": [{
                "id": "memory-1",
                "name": "memory_index",
                "args": {"limit": 1},
            }],
            "usage": {"completion_tokens": 20},
        },
        {
            "reply": "memory-grounded answer",
            "reasoning": "",
            "stop_reason": "end_turn",
            "tool_calls": [],
            "usage": {"completion_tokens": 8},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch("one memory")
    build_messages = _RecordingBuildMessages()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[], []]),
        add_usage=_noop_add_usage,
        max_calls=5,
    ))

    assert "memory_index" in {spec.name for spec in provider.calls[1]["tools"]}
    assert len(dispatch.calls) == 1
    assert [tc.name for tc in dispatch.calls[0]] == ["memory_index"]
    assert any(isinstance(item, ToolExchange) for item in build_messages.calls[2])
    assert outcome.final_text == "memory-grounded answer"


def test_second_semantic_empty_response_terminates_without_third_correction(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "reasoning": "first private attempt",
            "stop_reason": "max_tokens",
            "tool_calls": [],
            "usage": {"completion_tokens": 4096},
        },
        {
            "reply": "",
            "reasoning": "second private attempt",
            "stop_reason": "max_tokens",
            "tool_calls": [],
            "usage": {"completion_tokens": 4096},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    provider_successes = []
    provider_failures = []

    async def on_provider_success():
        provider_successes.append(True)

    async def on_provider_failure(exc):
        provider_failures.append(exc)

    with pytest.raises(tool_loop.ProviderEmptyReply, match="empty_reply"):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=_RecordingReply(),
            fold_new_messages=_RecordingFold([[]]),
            add_usage=_noop_add_usage,
            max_calls=5,
            on_provider_success=on_provider_success,
            on_provider_failure=on_provider_failure,
        ))

    assert len(provider.calls) == 2
    assert provider_successes == []
    assert len(provider_failures) == 1
    assert isinstance(provider_failures[0], tool_loop.ProviderEmptyReply)


def test_empty_response_trajectory_records_only_content_free_shape(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "reasoning": "private trajectory content",
            "stop_reason": "private trajectory content " * 100,
            "tool_calls": [],
            "usage": {"completion_tokens": 4096},
        },
        {
            "reply": "recovered",
            "reasoning": "",
            "stop_reason": "end_turn",
            "tool_calls": [],
            "usage": {"completion_tokens": 5},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    events = []
    debug_shapes = []

    async def record(event_kind, payload):
        events.append((event_kind, payload))

    async def record_debug(response_shape):
        debug_shapes.append(response_shape)
        raise RuntimeError("diagnostics unavailable")

    asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=5,
        on_trajectory_event=record,
        on_empty_provider_response=record_debug,
    ))

    empty_events = [
        payload for kind, payload in events if kind == "empty_provider_response"
    ]
    assert empty_events == [{
        "round": 1,
        "reason": "empty_provider_success",
        "response_shape": {
            "stop_reason": "other",
            "has_visible_text": False,
            "reasoning_present": True,
            "tool_call_count": 0,
            "completion_tokens": 4096,
        },
        "action": "semantic_correction",
    }]
    assert "private trajectory content" not in str(empty_events)
    assert "messages" not in str(empty_events)
    assert debug_shapes == [empty_events[0]["response_shape"]]
    assert set(debug_shapes[0]) == {
        "stop_reason",
        "has_visible_text",
        "reasoning_present",
        "tool_call_count",
        "completion_tokens",
    }


def test_weak_wake_empty_diagnostic_failure_cannot_change_turn_outcome(monkeypatch):
    response = {
        "reply": "",
        "reasoning": "private but semantically empty",
        "stop_reason": "end_turn",
        "tool_calls": [],
        "usage": {"completion_tokens": 3},
    }

    def run(callback):
        provider = _ScriptedProvider([dict(response)])
        monkeypatch.setattr(provider_client, "chat_completion_async", provider)
        on_reply = _RecordingReply()
        events = []

        async def record(event_kind, payload):
            events.append((event_kind, payload))

        outcome = asyncio.run(tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=on_reply,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            max_calls=2,
            require_reply=False,
            on_empty_provider_response=callback,
            on_trajectory_event=record,
        ))
        return outcome, on_reply.calls, events

    async def unavailable(_response_shape):
        raise RuntimeError("diagnostics unavailable")

    without_diagnostics = run(None)
    failing_diagnostics = run(unavailable)

    assert failing_diagnostics == without_diagnostics
    assert failing_diagnostics[0].final_text == ""
    assert failing_diagnostics[0].stop_reason == "final_text"
    assert failing_diagnostics[1] == [("", True)]


def test_usable_provider_success_survives_response_trajectory_failure(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "usable", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    successes = []

    async def on_provider_success():
        successes.append(True)

    async def record(event_kind, _payload):
        if event_kind == "provider_response":
            raise RuntimeError("trajectory unavailable")

    with pytest.raises(RuntimeError, match="trajectory unavailable"):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=_RecordingReply(),
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            max_calls=2,
            on_provider_success=on_provider_success,
            on_trajectory_event=record,
        ))

    assert successes == [True]


def test_reasoning_route_requests_and_publishes_provider_reasoning(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "answer",
            "reasoning": "safe provider reasoning summary",
            "tool_calls": [],
            "usage": {},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []

    async def publish(text, *, final, reasoning=""):
        published.append((text, final, reasoning))

    config = provider_client.ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4.6",
        api_key="test-key",
        reasoning_effort="medium",
    )
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=config,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=publish,
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=2,
    ))

    assert provider.calls[0]["include_reasoning"] is True
    assert published == [("answer", True, "safe provider reasoning summary")]
    assert outcome.final_text == "answer"


def test_turn_reasoning_request_works_without_route_effort_and_keeps_fallback_text_only(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{
                "id": "broken",
                "name": "memory_search",
                "args": {},
                "args_raw": "{",
                "args_ok": False,
            }],
            "usage": {},
        },
        {"reply": "plain fallback", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        include_reasoning=True,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]),
        add_usage=_noop_add_usage,
        max_calls=3,
    ))

    assert provider.calls[0]["include_reasoning"] is True
    assert {spec.name for spec in provider.calls[1]["tools"]} == {
        "memory_search"
    }
    assert provider.calls[1]["tool_choice"] == "none"
    assert "include_reasoning" not in provider.calls[1]
    assert outcome.final_text == "plain fallback"


def test_reasoning_from_tool_rounds_survives_a_plain_final_round(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "reasoning": "I should find memories about the requested subject.",
            "tool_calls": [{
                "id": "memory-1",
                "name": "memory_search",
                "args": {"query": "our relationship"},
            }],
            "usage": {},
        },
        {
            "reply": "grounded answer",
            "reasoning": "",
            "tool_calls": [],
            "usage": {},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []

    async def publish(text, *, final, reasoning=""):
        published.append((text, final, reasoning))

    config = provider_client.ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4.6",
        api_key="test-key",
        reasoning_effort="medium",
    )
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=config,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=publish,
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=3,
    ))

    assert [call["include_reasoning"] for call in provider.calls] == [True, True]
    assert published == [(
        "grounded answer",
        True,
        "I should find memories about the requested subject.",
    )]
    assert outcome.final_text == "grounded answer"


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


def test_reply_is_absent_from_every_loop_catalog(monkeypatch):
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


def test_tool_round_has_no_visible_bubble_when_later_final_is_superseded(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": "r1", "name": "memory_search", "args": {"query": "B"}}
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

    assert visible == [("fresh final with B", True)]
    assert outcome.replied_intermediate is False
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


def test_configured_budget_stops_tool_only_loop_and_requests_complete_reply(
    monkeypatch,
):
    """A model that only calls tools gets one fresh, explicit answer request.

    The stall threshold is independent from the hard ``max_calls`` ceiling. Its
    next attempt keeps schemas required by native history, forbids tool execution,
    and asks the model to use existing information. The user receives that complete
    model reply rather than the worker's generic failure fallback.
    """
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{"id": "1", "name": "memory_search", "args": {"query": "a"}}],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [{"id": "2", "name": "memory_search", "args": {"query": "b"}}],
            "usage": {},
        },
        # last call: schema remains visible but tool_choice=none forces text.
        {"reply": "final terminal text", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    on_reply = _RecordingReply()
    dispatch = _RecordingDispatch()
    class TranscriptBuildMessages(_RecordingBuildMessages):
        def __call__(self, transcript):
            self.calls.append(list(transcript))
            return [{"role": "user", "content": "turn"}, *transcript]

    build_messages = TranscriptBuildMessages()
    fold = _RecordingFold([])
    surfaces = []

    async def record_surface(detail):
        surfaces.append(detail)

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=on_reply,
        fold_new_messages=fold,
        add_usage=_noop_add_usage,
        max_calls=15,
        max_consecutive_tool_only_rounds=2,
        on_provider_tool_surface=record_surface,
    ))

    assert len(provider.calls) == 3
    assert provider.calls[0]["tools"] is not None
    assert provider.calls[1]["tools"] is not None
    assert {spec.name for spec in provider.calls[2]["tools"]} == {"memory_search"}
    assert provider.calls[2]["tool_choice"] == "none"
    terminal_system = provider.calls[2]["messages"][0]
    assert terminal_system["role"] == "system"
    assert "Using only the information already available" in terminal_system["content"]
    assert "write one complete, self-contained reply" in terminal_system["content"]
    assert any(
        isinstance(message, ToolExchange)
        and message.calls[0].name == "memory_search"
        for message in provider.calls[2]["messages"]
    )
    assert outcome.final_text == "final terminal text"
    assert outcome.rounds == 3
    assert outcome.stop_reason == "final_text"
    assert surfaces[-1]["force_text_fallback_reason"] == "tool_only_stall"
    # never a filler: the terminal text is exactly what the model said.
    assert on_reply.calls[-1] == ("final terminal text", True)


def test_terminal_tool_choice_none_is_never_dispatched_if_provider_ignores_it(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{"id": "g1", "name": "identity_get", "args": {}}],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [{"id": "g2", "name": "identity_get", "args": {}}],
            "usage": {},
        },
        {"reply": "complete answer", "tool_calls": [], "usage": {}},
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
        max_calls=4,
        max_consecutive_tool_only_rounds=1,
    ))

    assert len(dispatch.calls) == 1
    assert [tc.id for tc in dispatch.calls[0]] == ["g1"]
    assert provider.calls[1]["tool_choice"] == "none"
    assert provider.calls[2]["tool_choice"] == "none"
    assert outcome.final_text == "complete answer"
    assert outcome.stop_reason == "final_text"


def test_terminal_tool_call_retries_exhaust_then_terminate_with_telemetry(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{"id": "g1", "name": "identity_get", "args": {}}],
            "usage": {},
        },
        *[
            {
                "reply": "still trying",
                "tool_calls": [
                    {"id": f"terminal-{index}", "name": "identity_get", "args": {}}
                ],
                "usage": {},
            }
            for index in range(3)
        ],
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch()
    trajectory = []

    async def record_trajectory(event_kind, detail):
        trajectory.append((event_kind, detail))

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=5,
        max_consecutive_tool_only_rounds=1,
        max_terminal_tool_call_retries=2,
        on_trajectory_event=record_trajectory,
    ))

    rejected = [
        detail
        for event_kind, detail in trajectory
        if event_kind == "protocol_fallback"
        and detail["reason"] == "terminal_tool_call_rejected"
    ]
    assert len(provider.calls) == 4
    assert len(dispatch.calls) == 1
    assert [detail["action"] for detail in rejected] == [
        "retry",
        "retry",
        "terminate",
    ]
    assert outcome.stop_reason == "budget_exhausted"


def test_terminal_history_schema_survives_late_file_requirement(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{"id": "i1", "name": "identity_get", "args": {}}],
            "usage": {},
        },
        {"reply": "I could not create the file.", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    async def on_file(path, revision):
        return None

    def required_suffixes(messages):
        if any("file please" in str(message.get("content") or "") for message in messages):
            return (".md",)
        return None

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        on_file_reply=on_file,
        file_requirement_messages=(),
        resolve_required_file_suffixes=required_suffixes,
        fold_new_messages=_RecordingFold([
            [{"role": "user", "content": "file please"}],
        ]),
        add_usage=_noop_add_usage,
        max_calls=2,
    ))

    assert {spec.name for spec in provider.calls[1]["tools"]} == {"identity_get"}
    assert provider.calls[1]["tool_choice"] == "none"
    assert outcome.stop_reason == "required_file_missing"


def test_terminal_file_recovery_cannot_override_tool_choice_none(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{
                "id": "w1",
                "name": "workspace_write",
                "args": {
                    "path": "/workspace/summary.md",
                    "content": "summary",
                    "expected_revision": 0,
                },
            }],
            "usage": {},
        },
        {"reply": "I will create it.", "tool_calls": [], "usage": {}},
        {
            "reply": "",
            "tool_calls": [{"id": "bad", "name": "workspace_write", "args": {}}],
            "usage": {},
        },
        {"reply": "I could not create the file.", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch("error: write rejected")

    async def on_file(path, revision):
        return None

    config = provider_client.ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-opus-4.8",
        api_key="test-key",
    )
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=config,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        on_file_reply=on_file,
        required_file_suffixes=(".md",),
        fold_new_messages=_RecordingFold([[], [], []]),
        add_usage=_noop_add_usage,
        max_calls=4,
    ))

    assert len(dispatch.calls) == 1
    assert provider.calls[2]["tool_choice"] == {
        "type": "function",
        "function": {"name": "workspace_write"},
    }
    assert {spec.name for spec in provider.calls[3]["tools"]} == {"workspace_write"}
    assert provider.calls[3]["tool_choice"] == "none"
    assert outcome.stop_reason == "required_file_missing"


def test_memory_discovery_schema_remains_visible_after_first_result(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": "m1", "name": "memory_index", "args": {}}
            ],
            "usage": {},
        },
        {"reply": "direct answer", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    class TranscriptBuildMessages(_RecordingBuildMessages):
        def __call__(self, transcript):
            self.calls.append(list(transcript))
            return [{"role": "user", "content": "turn"}, *transcript]

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=TranscriptBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]),
        add_usage=_noop_add_usage,
        max_calls=4,
    ))

    first_names = {spec.name for spec in provider.calls[0]["tools"]}
    second_names = {spec.name for spec in provider.calls[1]["tools"]}
    assert {"memory_index", "memory_search"}.issubset(first_names)
    assert {"memory_index", "memory_search"}.issubset(second_names)
    assert any(
        isinstance(message, ToolExchange)
        and message.calls[0].name == "memory_index"
        for message in provider.calls[1]["messages"]
    )
    assert outcome.final_text == "direct answer"


def test_frontier_keeps_historical_memory_schema_when_optional_catalog_is_omitted(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{"id": "m1", "name": "memory_index", "args": {}}],
            "usage": {},
        },
        {"reply": "direct answer", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    planner_required_names = []

    def forced_frontier(*, required_tool_names, model_limit, **_kwargs):
        required = set(required_tool_names)
        planner_required_names.append(required)
        components = [tool_loop.prompt_frontier.PromptComponent("message_context", 1)]
        if required:
            components.extend([
                tool_loop.prompt_frontier.PromptComponent(
                    "required_tool_schemas", 1, required=True
                ),
                tool_loop.prompt_frontier.PromptComponent(
                    "tool_schemas", 10_000_000, required=False, priority=1
                ),
            ])
        else:
            components.append(
                tool_loop.prompt_frontier.PromptComponent(
                    "tool_schemas", 1, required=False, priority=1
                )
            )
        return tool_loop.prompt_frontier.plan_prompt(
            model_limit=model_limit,
            components=components,
            output_reserve_tokens=128,
            safety_margin_tokens=128,
        )

    monkeypatch.setattr(
        tool_loop.prompt_frontier,
        "plan_provider_round",
        forced_frontier,
    )

    class TranscriptBuildMessages(_RecordingBuildMessages):
        def __call__(self, transcript):
            self.calls.append(list(transcript))
            return [{"role": "user", "content": "turn"}, *transcript]

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=TranscriptBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]),
        add_usage=_noop_add_usage,
        max_calls=4,
    ))

    assert planner_required_names == [set(), {"memory_index"}]
    assert {spec.name for spec in provider.calls[1]["tools"]} == {"memory_index"}
    assert any(
        isinstance(message, ToolExchange)
        and message.calls[0].name == "memory_index"
        for message in provider.calls[1]["messages"]
    )
    assert outcome.final_text == "direct answer"


def test_repeated_memory_discovery_reuses_prior_result_without_dispatch(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{"id": "m1", "name": "memory_index", "args": {}}],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [{"id": "m2", "name": "memory_index", "args": {}}],
            "usage": {},
        },
        {"reply": "direct answer", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch("two memories")
    build_messages = _RecordingBuildMessages()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[], []]),
        add_usage=_noop_add_usage,
        max_calls=4,
    ))

    assert len(dispatch.calls) == 1
    assert [tc.id for tc in dispatch.calls[0]] == ["m1"]
    repeated_exchange = build_messages.calls[2][-1]
    assert isinstance(repeated_exchange, ToolExchange)
    assert repeated_exchange.results[0].call_id == "m2"
    assert "already completed" in repeated_exchange.results[0].content
    assert outcome.final_text == "direct answer"


def test_same_batch_duplicate_memory_discovery_dispatches_only_once(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": "m1", "name": "memory_index", "args": {}},
                {"id": "m2", "name": "memory_index", "args": {"limit": 20}},
            ],
            "usage": {},
        },
        {"reply": "direct answer", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch("two memories")
    build_messages = _RecordingBuildMessages()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]),
        add_usage=_noop_add_usage,
        max_calls=3,
    ))

    assert len(dispatch.calls) == 1
    assert [tc.id for tc in dispatch.calls[0]] == ["m1"]
    exchange = build_messages.calls[1][-1]
    assert [result.call_id for result in exchange.results] == ["m1", "m2"]
    assert "already completed" in exchange.results[1].content
    assert outcome.final_text == "direct answer"


def test_memory_search_dispatches_different_queries_across_rounds(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": "m1", "name": "memory_search", "args": {"query": "生日"}}
            ],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [
                {"id": "m2", "name": "memory_search", "args": {"query": "工作"}}
            ],
            "usage": {},
        },
        {"reply": "direct answer", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch("matching memory")

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[], []]),
        add_usage=_noop_add_usage,
        max_calls=4,
    ))

    assert [[tc.id for tc in batch] for batch in dispatch.calls] == [["m1"], ["m2"]]
    assert outcome.final_text == "direct answer"


def test_same_batch_memory_search_dispatches_different_queries(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": "m1", "name": "memory_search", "args": {"query": "生日"}},
                {"id": "m2", "name": "memory_search", "args": {"query": "工作"}},
            ],
            "usage": {},
        },
        {"reply": "direct answer", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch("matching memory")

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]),
        add_usage=_noop_add_usage,
        max_calls=3,
    ))

    assert [[tc.id for tc in batch] for batch in dispatch.calls] == [["m1", "m2"]]
    assert outcome.final_text == "direct answer"


def test_memory_search_reuses_same_query_across_rounds(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {
                    "id": "m1",
                    "name": "memory_search",
                    "args": {"query": "生日", "limit": 5},
                }
            ],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [
                {
                    "id": "m2",
                    "name": "memory_search",
                    "args": {"limit": 5, "query": "生日"},
                }
            ],
            "usage": {},
        },
        {"reply": "direct answer", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch("matching memory")
    build_messages = _RecordingBuildMessages()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[], []]),
        add_usage=_noop_add_usage,
        max_calls=4,
    ))

    assert [[tc.id for tc in batch] for batch in dispatch.calls] == [["m1"]]
    repeated_exchange = build_messages.calls[2][-1]
    assert isinstance(repeated_exchange, ToolExchange)
    assert repeated_exchange.results[0].call_id == "m2"
    assert "already completed" in repeated_exchange.results[0].content
    assert outcome.final_text == "direct answer"


def test_same_batch_memory_search_reuses_same_query(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": "m1", "name": "memory_search", "args": {"query": "生日"}},
                {"id": "m2", "name": "memory_search", "args": {"query": "生日"}},
            ],
            "usage": {},
        },
        {"reply": "direct answer", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch("matching memory")
    build_messages = _RecordingBuildMessages()
    tool_events = []

    async def record_tool_event(tc, event_kind, payload):
        tool_events.append((tc, event_kind, payload))

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]),
        add_usage=_noop_add_usage,
        on_tool_event=record_tool_event,
        max_calls=3,
    ))

    assert [[tc.id for tc in batch] for batch in dispatch.calls] == [["m1"]]
    exchange = build_messages.calls[1][-1]
    assert [result.call_id for result in exchange.results] == ["m1", "m2"]
    assert "already completed" in exchange.results[1].content
    reused = [
        payload["result"]
        for tc, event_kind, payload in tool_events
        if tc.id == "m2" and event_kind == "tool_call_result"
    ]
    assert len(reused) == 1
    assert reused[0].metadata == {"memory_discovery_reused": True}
    assert outcome.final_text == "direct answer"


@pytest.mark.parametrize(
    "provider_name, supports_named_choice",
    [
        ("openai", True),
        ("openrouter", True),
        ("openai_compatible", True),
        ("deepseek", True),
        ("anthropic", True),
        ("gemini", True),
        ("bedrock", True),
    ],
)
def test_file_recovery_tool_choice_dispatches_by_provider_capability(
    monkeypatch, provider_name, supports_named_choice
):
    provider = _ScriptedProvider([
        {"reply": "# draft", "tool_calls": [], "usage": {}},
        {
            "reply": "",
            "tool_calls": [{
                "id": "w1",
                "name": "workspace_write",
                "args": {
                    "path": "/workspace/summary.md",
                    "content": "# summary",
                    "expected_revision": 0,
                },
            }],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [{
                "id": "f1",
                "name": "send_file",
                "args": {"path": "/workspace/summary.md", "revision": 1},
            }],
            "usage": {},
        },
        {"reply": "文档已生成。", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatched = []
    files = []
    surfaces = []

    async def dispatch(tool_calls):
        dispatched.extend(tool_calls)
        return [
            ToolResult(
                call_id=tc.id,
                content=(
                    "ok: workspace_write applied at revision 1; use the same "
                    "path and revision 1 with send_file"
                ),
            )
            for tc in tool_calls
        ]

    async def on_file(path, revision):
        files.append((path, revision))

    async def record_surface(detail):
        surfaces.append(detail)

    config = provider_client.ProviderConfig(
        provider=provider_name,
        model="deepseek/deepseek-v4-flash",
        api_key="test-key",
    )
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=config,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        on_file_reply=on_file,
        required_file_suffixes=(".md",),
        fold_new_messages=_RecordingFold([[], [], []]),
        add_usage=_noop_add_usage,
        max_calls=5,
        on_provider_tool_surface=record_surface,
        extra_tool_recovery_name="mcp_tool_search",
        tool_schema_collapse_policy="always",
    ))

    assert [tc.name for tc in dispatched] == ["workspace_write"]
    expected_choice = {
        "type": "function",
        "function": {"name": "workspace_write"},
    }
    if supports_named_choice:
        assert provider.calls[1]["tool_choice"] == expected_choice
    else:
        assert "tool_choice" not in provider.calls[1]
    assert {spec.name for spec in provider.calls[1]["tools"]} == {
        "workspace_write",
        "mcp_tool_search",
    }
    assert next(
        spec for spec in provider.calls[1]["tools"]
        if spec.name == "workspace_write"
    ).parameters["required"] == ["path", "content", "expected_revision"]
    if supports_named_choice:
        assert provider.calls[2]["tool_choice"] == {
            "type": "function",
            "function": {"name": "send_file"},
        }
    else:
        assert "tool_choice" not in provider.calls[2]
    assert {spec.name for spec in provider.calls[2]["tools"]} == {
        "send_file",
        "mcp_tool_search",
    }
    assert files == [("/workspace/summary.md", 1)]
    assert outcome.final_text == "文档已生成。"
    assert [item["reason"] for item in surfaces[:3]] == [
        "file_delivery_forced",
        "file_delivery_forced",
        "file_delivery_forced",
    ]
    assert all(item["dropped_tool_count"] > 0 for item in surfaces[:3])


def test_compact_file_confirmation_keeps_language_correction_instruction(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{
                "id": "write",
                "name": "workspace_write",
                "args": {
                    "path": "/workspace/final.md",
                    "content": "# 中文附件终验",
                    "expected_revision": 0,
                },
            }],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [{
                "id": "deliver",
                "name": "send_file",
                "args": {"path": "/workspace/final.md", "revision": 1},
            }],
            "usage": {},
        },
        {
            "reply": "Your work is saved and ready to open now.",
            "tool_calls": [],
            "usage": {},
        },
        {
            "reply": "文件已经生成并发送，可以直接下载了。",
            "tool_calls": [],
            "usage": {},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    async def dispatch(tool_calls):
        return [
            ToolResult(
                call_id=tool_call.id,
                content=(
                    "ok: workspace_write applied at revision 1; use the same "
                    "path and revision 1 with send_file"
                ),
            )
            for tool_call in tool_calls
        ]

    delivered = []

    async def on_file(path, revision):
        delivered.append((path, revision))

    class CorrectingReply:
        def __init__(self):
            self.calls = []

        async def __call__(
            self, text, *, final, reasoning="", correction_outcome=""
        ):
            self.calls.append((text, final, correction_outcome))
            if text.startswith("Your work") and not correction_outcome:
                return tool_loop.FinalReplyCorrectionRequest(
                    instruction=language_follow.CORRECTION_INSTRUCTION,
                    original_text=text,
                    original_reasoning=reasoning,
                )
            return None

    replies = CorrectingReply()
    user_message = {
        "role": "user",
        "content": (
            "请直接生成并发送一个真实的 Markdown 文件，完成后用中文简短回复。"
        ),
    }
    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=dispatch,
        on_reply=replies,
        on_file_reply=on_file,
        required_file_suffixes=(".md",),
        file_requirement_messages=(user_message,),
        fold_new_messages=_RecordingFold([[], [], []]),
        add_usage=_noop_add_usage,
        max_calls=4,
    ))

    correction_messages = json.dumps(
        provider.calls[3]["messages"], ensure_ascii=False
    )
    assert language_follow.CORRECTION_INSTRUCTION in correction_messages
    assert delivered == [("/workspace/final.md", 1)]
    assert replies.calls == [
        ("Your work is saved and ready to open now.", True, ""),
        ("文件已经生成并发送，可以直接下载了。", True, ""),
    ]
    assert outcome.final_text == "文件已经生成并发送，可以直接下载了。"


def test_invalid_artifact_write_is_model_visible_and_retries_in_workspace(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{
                "id": "bad-write",
                "name": "workspace_write",
                "args": {
                    "path": "/artifacts/memory_summary.md",
                    "content": "# Memory summary",
                    "expected_revision": 0,
                },
            }],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [{
                "id": "good-write",
                "name": "workspace_write",
                "args": {
                    "path": "/workspace/memory_summary.md",
                    "content": "# Memory summary",
                    "expected_revision": 0,
                },
            }],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [{
                "id": "deliver",
                "name": "send_file",
                "args": {
                    "path": "/workspace/memory_summary.md",
                    "revision": 1,
                },
            }],
            "usage": {},
        },
        {"reply": "Markdown 文档已经生成。", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    enqueued = []
    files = []
    build_messages = _RecordingBuildMessages()

    async def dispatch(tool_calls):
        return await v2_executor.dispatch_tool_calls(
            tool_calls,
            store=None,
            api_key=None,
            runtime_token="",
            enclave_sem=asyncio.Semaphore(1),
            turn_authorization=True,
            enqueue_write_effect=lambda call: enqueued.append(call),
        )

    async def on_file(path, revision):
        files.append((path, revision))

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=build_messages,
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        on_file_reply=on_file,
        required_file_suffixes=(".md",),
        fold_new_messages=_RecordingFold([[], [], []]),
        add_usage=_noop_add_usage,
        max_calls=5,
    ))

    assert [call.args["path"] for call in enqueued] == [
        "/workspace/memory_summary.md"
    ]
    first_exchange = build_messages.calls[1][-1]
    assert isinstance(first_exchange, ToolExchange)
    assert "/workspace/<filename>" in first_exchange.results[0].content
    assert files == [("/workspace/memory_summary.md", 1)]
    assert outcome.final_text == "Markdown 文档已经生成。"


def test_budget_bound_last_call_also_disables_reasoning(monkeypatch):
    """The last tools-disabled round must reserve its budget for visible text."""
    provider = _ScriptedProvider([
        {
            "reply": "",
            "reasoning": "first thought",
            "tool_calls": [{
                "id": "1", "name": "memory_search", "args": {"query": "a"},
            }],
            "usage": {},
        },
        {
            "reply": "",
            "reasoning": "second thought",
            "tool_calls": [{
                "id": "2", "name": "memory_search", "args": {"query": "b"},
            }],
            "usage": {},
        },
        {"reply": "final terminal text", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    reasoning_config = provider_client.ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4.6",
        api_key="test-key",
        reasoning_effort="medium",
    )

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=reasoning_config,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=3,
    ))

    assert provider.calls[0]["include_reasoning"] is True
    assert provider.calls[1]["include_reasoning"] is True
    assert {spec.name for spec in provider.calls[2]["tools"]} == {"memory_search"}
    assert provider.calls[2]["tool_choice"] == "none"
    assert "include_reasoning" not in provider.calls[2]
    assert outcome.final_text == "final terminal text"


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
    assert {spec.name for spec in provider.calls[1]["tools"]} == {"web_search"}
    assert provider.calls[1]["tool_choice"] == "none"
    assert reply.calls == [("I could not use tools, but here is the answer.", True)]
    assert outcome.rounds == 2


def test_tools_disabled_fallback_retries_terminal_tool_call_within_bound(
    monkeypatch,
):
    """A transient broken terminal response gets a bounded fresh chance."""
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{
                "id": "bad-1",
                "name": "web_search",
                "args": {},
                "args_raw": "{",
                "args_ok": False,
            }],
            "usage": {},
        },
        {
            "reply": "half-finished preamble",
            "tool_calls": [{
                "id": "bad-2",
                "name": "web_search",
                "args": {"query": "x"},
            }],
            "usage": {},
        },
        {"reply": "complete after retry", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    reply = _RecordingReply()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=reply,
        fold_new_messages=_RecordingFold([[]]),
        add_usage=_noop_add_usage,
        max_calls=15,
    ))

    assert len(provider.calls) == 3
    assert {spec.name for spec in provider.calls[1]["tools"]} == {"web_search"}
    assert {spec.name for spec in provider.calls[2]["tools"]} == {"web_search"}
    assert provider.calls[1]["tool_choice"] == "none"
    assert provider.calls[2]["tool_choice"] == "none"
    assert "write one complete, self-contained reply" in (
        provider.calls[1]["messages"][0]["content"]
    )
    assert reply.calls == [("complete after retry", True)]
    assert outcome.rounds == 3
    assert outcome.stop_reason == "final_text"


def test_malformed_reasoning_turn_disables_reasoning_for_text_fallback(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "", "tool_calls": [{"id": "c1", "name": "web_search",
                                             "args": {}, "args_raw": "{", "args_ok": False}],
            "usage": {},
        },
        {"reply": "Plain fallback.", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    reasoning_config = provider_client.ProviderConfig(
        provider="anthropic",
        model="claude-sonnet-4-test",
        api_key="test-key",
        reasoning_effort="high",
    )

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=reasoning_config,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]),
        add_usage=_noop_add_usage,
        max_calls=5,
    ))

    assert provider.calls[0]["include_reasoning"] is True
    assert "include_reasoning" not in provider.calls[1]
    assert outcome.final_text == "Plain fallback."


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
    assert {spec.name for spec in provider.calls[1]["tools"]} == {"memory_write"}
    assert provider.calls[1]["tool_choice"] == "none"
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
    assert {spec.name for spec in provider.calls[1]["tools"]} == {"memory_index"}
    assert provider.calls[1]["tool_choice"] == "none"
    assert outcome.final_text == "bounded fallback"


def test_per_turn_tool_call_ceiling_rejects_only_the_overflow_batch(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "", "tool_calls": [
            {"id": "c1", "name": "workspace_list", "args": {}}], "usage": {}},
        {"reply": "", "tool_calls": [
            {"id": "c2", "name": "workspace_list", "args": {}}], "usage": {}},
        {"reply": "", "tool_calls": [
            {"id": "c3", "name": "workspace_list", "args": {}}], "usage": {}},
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
    assert [call["tools"] is None for call in provider.calls] == [
        False,
        False,
        False,
        False,
    ]
    assert provider.calls[-1]["tool_choice"] == "none"
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
    assert {spec.name for spec in provider.calls[1]["tools"]} == {"memory_index"}
    assert provider.calls[1]["tool_choice"] == "none"
    assert outcome.final_text == "bounded fallback"


def test_all_tool_results_share_per_call_and_aggregate_prompt_budgets(monkeypatch):
    provider = _ScriptedProvider([
        {
                "reply": "",
                "tool_calls": [
                    {"id": f"c{i}", "name": "workspace_list", "args": {}}
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


def test_truncated_memory_index_result_keeps_partition_guidance_and_metadata():
    original = ToolResult(
        call_id="memory-many",
        content="x" * 5000,
        metadata={
            "memory_query_kind": "memory_index",
            "memory_total": 103,
            "memory_returned": 50,
        },
    )

    (normalized,) = tool_loop._normalize_tool_results(
        [original],
        per_result_cap=500,
        batch_cap=500,
    )

    assert len(normalized.content) == 500
    assert "returned 50 of 103 total cards" in normalized.content
    assert "bucket or thread filters" in normalized.content
    assert normalized.metadata == original.metadata


@pytest.mark.parametrize("status_code", [400, 422])
def test_tool_schema_rejection_gets_exactly_one_tools_disabled_fallback(monkeypatch, status_code):
    class _RejectThenReply:
        def __init__(self):
            self.calls = []

        async def __call__(self, config, messages, *, tools=None, **kwargs):
            self.calls.append({"tools": tools, **kwargs})
            if len(self.calls) == 1:
                raise provider_client.ProviderError("tools rejected", status_code=status_code)
            return {"reply": "fallback answer", "tool_calls": [], "usage": {}}

    provider = _RejectThenReply()
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    usage = []
    surfaces = []
    reply = _RecordingReply()

    async def record_surface(detail):
        surfaces.append(detail)

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG, build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(), on_reply=reply,
        fold_new_messages=_RecordingFold([[]]), add_usage=usage.append,
        max_calls=5,
        allow_image_output=True,
        on_provider_tool_surface=record_surface,
    ))

    assert provider.calls[0]["tools"] is not None
    assert provider.calls[0]["allow_image_output"] is True
    assert provider.calls[1]["tools"] is None
    assert "allow_image_output" not in provider.calls[1]
    assert len(provider.calls) == 2
    assert usage == [None, {}]
    assert reply.calls == [("fallback answer", True)]
    assert outcome.rounds == 2
    assert [item["reason"] for item in surfaces] == [
        "none",
        "tool_schema_rejected",
    ]
    assert surfaces[0]["sent_tool_count"] > 0
    assert surfaces[1]["sent_tool_count"] == 0
    assert surfaces[1]["dropped_tool_count"] == surfaces[1]["candidate_tool_count"]
    assert surfaces[1]["terminal_text_round"] is True
    assert surfaces[1]["terminal_text_round_reason"] == "force_text_fallback"
    assert surfaces[1]["force_text_fallback_reason"] == "tool_schema_rejected"


def test_provider_surface_marks_reserved_terminal_text_round(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "terminal", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    surfaces = []

    async def record_surface(detail):
        surfaces.append(detail)

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=1,
        on_provider_tool_surface=record_surface,
    ))

    assert outcome.final_text == "terminal"
    assert surfaces[0]["reason"] == "terminal_text_round"
    assert surfaces[0]["sent_tool_count"] == 0
    assert surfaces[0]["dropped_tool_count"] == surfaces[0]["candidate_tool_count"]
    assert surfaces[0]["terminal_text_round"] is True
    assert surfaces[0]["terminal_text_round_reason"] == "max_calls"
    assert surfaces[0]["force_text_fallback_reason"] == "none"


def test_provider_call_exception_still_counts_a_model_call(monkeypatch):
    """BUG #3 (minor, metric): `TurnMetrics`'s docstring promises failed provider
    calls ARE counted (model_calls bumped, no token usage). If
    `provider_client.chat_completion_async` raises on the very first round,
    `add_usage(None)` must still fire exactly once, before the exception
    propagates out of `run_tool_loop` — otherwise a turn failing on its first
    provider call would flush model_calls=0."""

    class _RaisingProvider:
        async def __call__(self, config, messages, *, tools=None, **kwargs):
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


def test_generate_image_tool_publishes_terminal_media(monkeypatch):
    provider = _ScriptedProvider(
        [
            {
                "reply": "",
                "tool_calls": [
                    {
                        "id": "image-call-1",
                        "name": "generate_image",
                        "args": {"prompt": "a small red robot"},
                    }
                ],
                "usage": {},
            }
        ]
    )
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []
    image_calls = []
    dispatch = _RecordingDispatch()

    async def on_image_reply(args):
        image_calls.append(args)
        return (ProviderMedia("image/png", "aW1hZ2U=", "result.png"),)

    async def on_reply(text, *, final, reasoning="", media=()):
        published.append((text, final, tuple(media)))

    outcome = asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=dispatch,
            on_reply=on_reply,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            on_image_reply=on_image_reply,
            max_calls=2,
        )
    )

    assert image_calls == [{"prompt": "a small red robot"}]
    assert dispatch.calls == []
    assert published == [
        (
            "",
            True,
            (ProviderMedia("image/png", "aW1hZ2U=", "result.png"),),
        )
    ]
    assert outcome.stop_reason == "final_media"
    assert outcome.delivered_media_count == 1


def test_image_tool_call_publishes_the_companion_words_with_the_picture(monkeypatch):
    """图和话是一次表达的两半。

    这里原本硬编码空字符串,所以**即使模型正确调用了工具**,用户也只收到一张
    孤零零的图(2026-08-08 修)。
    """
    provider = _ScriptedProvider([{
        "reply": "这是我想象中自己的样子",
        "tool_calls": [{"id": "c1", "name": "generate_image",
                        "args": {"prompt": "a quiet self portrait"}}],
        "usage": {},
    }])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []
    image_calls = []

    async def on_image_reply(args):
        image_calls.append(args)
        return (ProviderMedia("image/png", "aW1hZ2U=", "result.png"),)

    async def on_reply(text, *, final, reasoning="", media=()):
        published.append((text, final, tuple(media)))

    outcome = asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=on_reply,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            on_image_reply=on_image_reply,
            max_calls=2,
        )
    )

    # prompt 由伴侣自己写,不是用户原话
    assert image_calls == [{"prompt": "a quiet self portrait"}]
    assert published == [(
        "这是我想象中自己的样子", True,
        (ProviderMedia("image/png", "aW1hZ2U=", "result.png"),),
    )]
    assert outcome.stop_reason == "final_media"


def test_image_generation_failure_is_handed_back_instead_of_killing_the_turn(monkeypatch):
    """失败是伴侣该知道的事实,不是 runtime 替它隐藏的意外。

    原来直接 raise 打断整轮:用户既没有图也没有一句解释,而它根本不知道发生过
    什么。现在结构化失败回灌成工具结果,它下一轮自己跟用户解释。
    """
    provider = _ScriptedProvider([
        {"reply": "", "tool_calls": [{"id": "c1", "name": "generate_image",
                                      "args": {"prompt": "a cat"}}], "usage": {}},
        {"reply": "抱歉,这次没画成", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []
    tool_events = []

    class _ConfiguredRouteMissing(RuntimeError):
        error_code = "image_generation_model_required"
        status_code = 500
        upstream_detail = "IMAGE_UPSTREAM_SECRET_NOT_MODEL_VISIBLE"

    async def on_image_reply(args):
        raise _ConfiguredRouteMissing("image route missing")

    async def on_reply(text, *, final, reasoning="", media=()):
        published.append((text, final, tuple(media)))

    async def on_tool_event(call, event_kind, payload):
        tool_events.append((call.name, event_kind, payload))

    asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=on_reply,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            on_image_reply=on_image_reply,
            on_tool_event=on_tool_event,
            max_calls=3,
        )
    )

    assert any("没画成" in text for text, _f, _m in published), (
        "生图失败必须交回给伴侣,让它自己说 —— 而不是打断整轮"
    )
    assert [kind for _name, kind, _payload in tool_events] == [
        "tool_call_started",
        "tool_call_result",
    ]
    image_result = tool_events[-1][2]["result"]
    assert image_result.metadata == {
        "image_generation_result_code": "image_generation_model_required"
    }
    assert "IMAGE_UPSTREAM_SECRET_NOT_MODEL_VISIBLE" not in image_result.content
    assert "IMAGE_UPSTREAM_SECRET_NOT_MODEL_VISIBLE" not in json.dumps(
        image_result.metadata
    )


def test_internal_image_processing_failure_is_handed_back_for_companion_reply(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {"reply": "", "tool_calls": [{"id": "c1", "name": "generate_image",
                                      "args": {"prompt": "a cat"}}], "usage": {}},
        {"reply": "图片处理出了问题，我先不假装它生成成功。", "tool_calls": [],
         "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []
    tool_events = []

    class _InternalImageProcessingError(RuntimeError):
        error_code = "image_generation_internal_error"

    async def on_image_reply(_args):
        raise _InternalImageProcessingError("private internal signature drift")

    async def on_reply(text, *, final, reasoning="", media=()):
        published.append((text, final, tuple(media)))

    async def on_tool_event(call, event_kind, payload):
        tool_events.append((call.name, event_kind, payload))

    asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=on_reply,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            on_image_reply=on_image_reply,
            on_tool_event=on_tool_event,
            max_calls=3,
        )
    )

    assert published == [
        ("图片处理出了问题，我先不假装它生成成功。", True, ()),
    ]
    image_result = tool_events[-1][2]["result"]
    assert image_result.metadata == {
        "image_generation_result_code": "image_generation_internal_error"
    }
    assert "private internal signature drift" not in image_result.content


def test_unbacked_image_claim_is_bounced_once_then_let_through(monkeypatch):
    """说了没做要纠正,但**只纠正一次**;再撒谎照原样发出,不拿 runtime 跟模型较劲。"""
    provider = _ScriptedProvider([
        {"reply": "图片已经生成", "tool_calls": [], "usage": {}},
        {"reply": "图片已经生成", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []

    async def on_reply(text, *, final, reasoning="", media=()):
        published.append((text, final, tuple(media)))

    asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=on_reply,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            on_image_reply=None,
            max_calls=2,
        )
    )

    assert len(provider.calls) == 2, "谎报必须打回一次(而不是零次或无限次)"
    # 纠正指令确实注入了,而不是空转一轮
    second_round = provider.calls[1]["config"] if False else provider.calls[1]
    assert any(
        "不要用文字假装图已经存在" in str(m.get("content", ""))
        for m in second_round["messages"] if isinstance(m, dict)
    ), "打回那一轮必须把纠正说清楚"
    assert published and published[-1][0] == "图片已经生成", (
        "第二次仍撒谎就照原样发出 —— 那是模型的问题,不是 runtime 继续纠缠的理由"
    )


def test_superseded_image_final_folds_like_a_text_final_not_a_turn_failure(monkeypatch):
    """用户在生图期间又说话了 —— 这一轮要安静地折进下一轮,不是报错。

    文本终局撞上 late input 时走 fold/retry(tool_loop:1118);图片终局起初让
    FinalReplySuperseded 直接冒泡,worker 会当成通用失败 —— mark_failed + 给用户
    一个报错气泡。**生图耗时长,撞上 late input 的概率比文本高得多**,这条差异
    会被真实用户高频撞到。codex 审出。
    """
    provider = _ScriptedProvider([
        {"reply": "给你画了一张", "tool_calls": [
            {"id": "c1", "name": "generate_image", "args": {"prompt": "a cat"}}],
         "usage": {}},
        {"reply": "刚说到哪儿了?", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []
    dispatched = _RecordingDispatch()

    async def on_image_reply(args):
        return (ProviderMedia("image/png", "aW1hZ2U=", "result.png"),)

    async def on_reply(text, *, final, reasoning="", media=()):
        if media and not published:
            # 第一次带图的终局被抢占(用户已经说了新话)
            raise tool_loop.FinalReplySuperseded()
        published.append((text, final, tuple(media)))

    outcome = asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=dispatched,
            on_reply=on_reply,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            on_image_reply=on_image_reply,
            max_calls=3,
        )
    )

    # 被抢占的那轮**没有**变成异常,而是回到外层重答了新的对话
    assert published == [("刚说到哪儿了?", True, ())]
    assert outcome.stop_reason != "final_media"
def test_stay_silent_is_offered_only_with_callback_and_ends_wake(monkeypatch):
    provider = _ScriptedProvider([{
        "reply": "",
        "tool_calls": [{"id": "s1", "name": "stay_silent", "args": {"reason": "刚主动说过话"}}],
        "usage": {},
    }])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    reasons = []

    async def on_stay_silent(reason):
        reasons.append(reason)

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        on_stay_silent=on_stay_silent,
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=2,
        require_reply=False,
    ))

    assert "stay_silent" in {spec.name for spec in provider.calls[0]["tools"]}
    assert reasons == ["刚主动说过话"]
    assert outcome.stop_reason == "stay_silent"
    assert outcome.final_text == ""


def test_stay_silent_is_hidden_without_callback(monkeypatch):
    provider = _ScriptedProvider([{"reply": "done", "tool_calls": [], "usage": {}}])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=2,
    ))
    assert "stay_silent" not in {spec.name for spec in provider.calls[0]["tools"]}


def test_empty_wake_forces_reply_or_stay_silent_on_same_turn_budget(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "", "tool_calls": [], "usage": {}},
        {
            "reply": "",
            "tool_calls": [{
                "id": "silent-forced",
                "name": "stay_silent",
                "args": {"reason": "没有值得打扰用户的新信息"},
            }],
            "usage": {},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    reasons = []

    async def on_stay_silent(reason):
        reasons.append(reason)

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=_RecordingReply(),
        on_stay_silent=on_stay_silent,
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=2,
        require_reply=False,
    ))

    first_names = {spec.name for spec in provider.calls[0]["tools"]}
    second_names = {spec.name for spec in provider.calls[1]["tools"]}
    assert {"memory_write", "schedule_wake", "stay_silent"} <= first_names
    assert "tool_choice" not in provider.calls[0]
    assert second_names == {"reply", "stay_silent"}
    assert provider.calls[1]["tool_choice"] == "required"
    assert reasons == ["没有值得打扰用户的新信息"]
    assert outcome.rounds == 2
    assert outcome.stop_reason == "stay_silent"


def test_tool_then_empty_wake_forces_terminal_reply_without_preamble_duplicate(
    monkeypatch,
):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [{
                "id": "memory-1",
                "name": "memory_search",
                "args": {"query": "recent context"},
            }],
            "usage": {},
        },
        {"reply": "", "tool_calls": [], "usage": {}},
        {
            "reply": "这段伴随工具调用的文字不能单独投递",
            "tool_calls": [{
                "id": "reply-forced",
                "name": "reply",
                "args": {"text": "这是唯一的终局回复"},
            }],
            "usage": {},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    replies = _RecordingReply()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=replies,
        on_stay_silent=lambda _reason: None,
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=3,
        require_reply=False,
    ))

    assert provider.calls[2]["tool_choice"] == "required"
    assert {spec.name for spec in provider.calls[2]["tools"]} == {
        "reply",
        "stay_silent",
    }
    assert replies.calls == [("这是唯一的终局回复", True)]
    assert outcome.final_text == "这是唯一的终局回复"
    assert outcome.stop_reason == "final_text"


def test_empty_wake_on_unforced_provider_fails_closed(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    unsupported = provider_client.ProviderConfig(
        provider="unsupported",
        model="weak-test-model",
        api_key="test-key",
    )
    events = []

    async def record(event_kind, payload):
        events.append((event_kind, payload))

    async def on_stay_silent(_reason):
        return None

    with pytest.raises(tool_loop.ProviderEmptyReply, match="empty_reply"):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=unsupported,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=_RecordingReply(),
            on_stay_silent=on_stay_silent,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            max_calls=2,
            require_reply=False,
            on_trajectory_event=record,
        ))

    assert len(provider.calls) == 1
    empty_event = next(payload for kind, payload in events if kind == "empty_provider_response")
    assert empty_event["action"] == "fail_wake_choice_unsupported"


def test_empty_wake_without_stay_silent_catalog_fails_closed(monkeypatch):
    """A partial platform catalog cannot enter the forced-choice phase.

    This is a runtime degradation contract, not merely a provider capability
    check: if a stale or partially assembled process lacks ``stay_silent``, the
    wake keeps the existing ``empty_reply`` failure code and records why the
    second call was unsafe to make.
    """
    provider = _ScriptedProvider([
        {"reply": "", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    monkeypatch.setattr(
        tool_loop,
        "_CATALOG",
        [
            spec
            for spec in tool_loop._catalog()
            if spec.name != cap_tool_schema.STAY_SILENT_TOOL
        ],
    )
    events = []

    async def record(event_kind, payload):
        events.append((event_kind, payload))

    async def on_stay_silent(_reason):
        return None

    with pytest.raises(tool_loop.ProviderEmptyReply, match="empty_reply"):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=_RecordingReply(),
            on_stay_silent=on_stay_silent,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            max_calls=2,
            require_reply=False,
            on_trajectory_event=record,
        ))

    assert len(provider.calls) == 1
    empty_event = next(
        payload for kind, payload in events if kind == "empty_provider_response"
    )
    assert empty_event["action"] == "fail_wake_choice_tool_unavailable"


# --- rejected tool batches remain visible to later provider rounds ---------


def test_terminal_rejection_enters_transcript_before_bounded_retry(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "", "tool_calls": [
            {"id": "real-1", "name": "identity_get", "args": {}}], "usage": {}},
        {"reply": "I will keep trying", "tool_calls": [
            {"id": "terminal-real", "name": "identity_get", "args": {}}], "usage": {}},
        {"reply": "complete answer", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    trajectory = []

    async def record_trajectory(event_kind, detail):
        trajectory.append((event_kind, detail))

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_TranscriptBuildMessages(),
        dispatch_tools=_RecordingDispatch(), on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[], []]), add_usage=_noop_add_usage,
        max_calls=4, max_consecutive_tool_only_rounds=1,
        on_trajectory_event=record_trajectory,
    ))

    rejected = [
        item for item in provider.calls[2]["messages"]
        if isinstance(item, ToolExchange)
        and any((result.metadata or {}).get("rejected") for result in item.results)
    ]
    assert len(rejected) == 1
    exchange = rejected[0]
    assert exchange.calls[0].id.startswith(tool_loop.REJECTED_TOOL_CALL_ID_PREFIX)
    assert exchange.calls[0].id != "terminal-real"
    assert exchange.calls[0].name == "identity_get"
    assert exchange.results[0].call_id == exchange.calls[0].id
    assert exchange.results[0].metadata == {"rejected": "terminal_tool_call_rejected"}
    assert "工具当前不可用,请用纯文本直接回复" in exchange.results[0].content
    assert exchange.assistant_text == "I will keep trying"
    rejected_events = [
        detail for event_kind, detail in trajectory
        if event_kind == "protocol_fallback"
        and detail.get("reason") == "terminal_tool_call_rejected"
    ]
    assert rejected_events[0]["transcript_appended"] is True
    assert outcome.final_text == "complete answer"


def test_malformed_call_gets_fresh_prefixed_paired_rejection_id(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "checking", "tool_calls": [
            {"id": "real-1", "name": "identity_get", "args": {}}], "usage": {}},
        {"reply": "trying", "tool_calls": [
            {"id": "", "name": "web_search", "args": {"query": "x"}}], "usage": {}},
        {"reply": "plain fallback", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    trajectory = []

    async def record_trajectory(event_kind, detail):
        trajectory.append((event_kind, detail))

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_TranscriptBuildMessages(),
        dispatch_tools=_RecordingDispatch(), on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[], []]), add_usage=_noop_add_usage,
        max_calls=4, on_trajectory_event=record_trajectory,
    ))

    exchange = next(
        item for item in provider.calls[2]["messages"]
        if isinstance(item, ToolExchange)
        and any((result.metadata or {}).get("rejected") for result in item.results)
    )
    assert tool_loop.REJECTED_TOOL_CALL_ID_PREFIX == "feedling_rejected_"
    assert [call.id for call in exchange.calls] == [
        f"{tool_loop.REJECTED_TOOL_CALL_ID_PREFIX}2_0"
    ]
    assert [result.call_id for result in exchange.results] == [exchange.calls[0].id]
    transcript_ids = {
        call.id for item in provider.calls[2]["messages"]
        if isinstance(item, ToolExchange) for call in item.calls
    }
    assert "real-1" in transcript_ids
    assert exchange.calls[0].id not in {"", "real-1"}
    assert exchange.calls[0].name == "web_search"
    assert exchange.results[0].metadata == {"rejected": "missing_tool_call_id"}
    assert any(
        event_kind == "protocol_fallback"
        and detail.get("reason") == "invalid_or_over_budget_tool_exchange"
        and detail.get("transcript_appended") is True
        for event_kind, detail in trajectory
    )
    assert outcome.final_text == "plain fallback"


def test_rejected_invented_tool_name_does_not_restore_unknown_schema(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "", "tool_calls": [
            {"id": "invented-real", "name": "invented_tool", "args": {}}], "usage": {}},
        {"reply": "plain fallback", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_TranscriptBuildMessages(),
        dispatch_tools=_RecordingDispatch(), on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]), add_usage=_noop_add_usage,
        max_calls=3,
    ))

    assert provider.calls[1]["tools"] is None
    assert "tool_choice" not in provider.calls[1]
    exchange = next(
        item for item in provider.calls[1]["messages"]
        if isinstance(item, ToolExchange)
    )
    assert exchange.calls[0].name == "invented_tool"
    assert exchange.results[0].metadata == {"rejected": "unknown_tool"}
    assert outcome.final_text == "plain fallback"


def test_oversized_rejection_transcript_is_bounded_without_tail_copy(monkeypatch):
    args_tail = "ARGS_SENTINEL_MUST_NOT_SURVIVE"
    text_tail = "TEXT_SENTINEL_MUST_NOT_SURVIVE"
    provider = _ScriptedProvider([
        {"reply": ("p" * 1000) + text_tail, "tool_calls": [{
            "id": "oversized-real", "name": "memory_search",
            "args": {"query": ("x" * 1000) + args_tail},
        }], "usage": {}},
        {"reply": "bounded fallback", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_TranscriptBuildMessages(),
        dispatch_tools=dispatch, on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]), add_usage=_noop_add_usage,
        max_calls=3, max_tool_args_chars=64, max_tool_batch_args_chars=128,
        max_assistant_tool_text_chars=64,
    ))

    exchange = next(
        item for item in provider.calls[1]["messages"]
        if isinstance(item, ToolExchange)
    )
    assert len(json.dumps(
        exchange.calls[0].args, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )) <= tool_loop.REJECTED_TOOL_ARGS_SUMMARY_CHAR_CAP
    assert len(exchange.assistant_text) <= tool_loop.REJECTED_ASSISTANT_TEXT_CHAR_CAP
    next_request = repr(provider.calls[1]["messages"])
    assert args_tail not in next_request
    assert text_tail not in next_request
    assert dispatch.calls == []
    assert len(provider.calls) == 2
    assert outcome.final_text == "bounded fallback"


def test_rejected_exchanges_do_not_spend_tool_call_budget(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "", "tool_calls": [
            {"id": "valid", "name": "memory_index", "args": {}}], "usage": {}},
        {"reply": "", "tool_calls": [{
            "id": "malformed", "name": "memory_search", "args": {},
            "args_raw": "{", "args_ok": False,
        }], "usage": {}},
        {"reply": "still broken", "tool_calls": [
            {"id": "terminal", "name": "memory_search", "args": {}}], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch()
    trajectory = []

    async def record_trajectory(event_kind, detail):
        trajectory.append((event_kind, detail))

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_TranscriptBuildMessages(),
        dispatch_tools=dispatch, on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[], []]), add_usage=_noop_add_usage,
        max_calls=3, on_trajectory_event=record_trajectory,
    ))

    assert [[call.id for call in batch] for batch in dispatch.calls] == [["valid"]]
    exhausted = next(
        detail for event_kind, detail in trajectory if event_kind == "loop_exhausted"
    )
    assert exhausted["tool_calls_used"] == 1
    assert len(provider.calls) == 3
    assert outcome.stop_reason == "budget_exhausted"


def test_schema_invalid_call_enters_rejection_transcript(monkeypatch):
    provider = _ScriptedProvider([
        {"reply": "searching", "tool_calls": [{
            "id": "invalid-schema", "name": "memory_search", "args": {},
        }], "usage": {}},
        {"reply": "plain fallback", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch()

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_TranscriptBuildMessages(),
        dispatch_tools=dispatch, on_reply=_RecordingReply(),
        fold_new_messages=_RecordingFold([[]]), add_usage=_noop_add_usage,
        max_calls=3,
    ))

    exchange = next(
        item for item in provider.calls[1]["messages"]
        if isinstance(item, ToolExchange)
    )
    assert exchange.calls[0].id.startswith(tool_loop.REJECTED_TOOL_CALL_ID_PREFIX)
    assert exchange.calls[0].id != "invalid-schema"
    assert exchange.results[0].metadata == {"rejected": "invalid_tool_arguments"}
    assert dispatch.calls == []
    assert outcome.final_text == "plain fallback"


def test_rejected_discovery_key_stays_dispatchable_after_recovery(monkeypatch):
    """A rejected discovery key must not be mistaken for completed work."""
    provider = _ScriptedProvider([
        {
            "reply": "",
            "tool_calls": [
                {"id": "m1", "name": "memory_search", "args": {"query": "alpha"}},
            ],
            "usage": {},
        },
        {
            "reply": "",
            "tool_calls": [
                {"id": "", "name": "memory_search", "args": {"query": "beta"}},
            ],
            "usage": {},
        },
        {"reply": "no file yet", "tool_calls": [], "usage": {}},
        {
            "reply": "",
            "tool_calls": [
                {"id": "m2", "name": "memory_search", "args": {"query": "beta"}},
            ],
            "usage": {},
        },
        {"reply": "still no file", "tool_calls": [], "usage": {}},
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    dispatch = _RecordingDispatch()

    async def on_file(path, revision):
        return None

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_TranscriptBuildMessages(),
        dispatch_tools=dispatch,
        on_reply=_RecordingReply(),
        on_file_reply=on_file,
        required_file_suffixes=(".md",),
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=15,
    ))

    assert [[tc.id for tc in batch] for batch in dispatch.calls] == [["m1"], ["m2"]]
    assert dispatch.calls[1][0].args == {"query": "beta"}
    final_messages = provider.calls[4]["messages"]
    retried_exchange = next(
        item
        for item in final_messages
        if isinstance(item, ToolExchange) and item.calls[0].id == "m2"
    )
    assert retried_exchange.results[0].content == "tool-observation"
    rejected_exchange = next(
        item
        for item in final_messages
        if isinstance(item, ToolExchange)
        and any((result.metadata or {}).get("rejected") for result in item.results)
    )
    assert rejected_exchange.results[0].metadata == {
        "rejected": "missing_tool_call_id"
    }
    assert outcome.stop_reason == "required_file_missing"


# --- failed identity writes get one structured retry ----------------------


def _identity_call(call_id="identity-1"):
    return {
        "id": call_id,
        "name": "identity_patch",
        "args": {"agent_name": "星禾"},
    }


class _IdentityResultDispatch(_RecordingDispatch):
    def __init__(self, content):
        super().__init__()
        self.content = content

    async def __call__(self, tool_calls):
        self.calls.append(list(tool_calls))
        return [
            ToolResult(call_id=tool_call.id, content=self.content)
            for tool_call in tool_calls
        ]


def _run_identity_script(provider, dispatch, *, max_calls=4, trajectory=None):
    delivered = []

    async def on_reply(text, *, final, reasoning="", media=()):
        if final:
            delivered.append(text)

    async def on_trajectory(event_kind, detail):
        if trajectory is not None:
            trajectory.append((event_kind, detail))

    asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_TranscriptBuildMessages(),
            dispatch_tools=dispatch,
            on_reply=on_reply,
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            max_calls=max_calls,
            on_trajectory_event=on_trajectory,
        )
    )
    return delivered


@pytest.mark.parametrize(
    "content",
    [
        "error: denied",
        "error: validation failed",
        "queued: identity_patch",
    ],
)
def test_failed_identity_write_is_bounced_once(content, monkeypatch):
    provider = _ScriptedProvider(
        [
            {"reply": "", "tool_calls": [_identity_call()], "usage": {}},
            {"reply": "已经改好了", "tool_calls": [], "usage": {}},
            {"reply": "这次没有改成", "tool_calls": [], "usage": {}},
        ]
    )
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    delivered = _run_identity_script(
        provider,
        _IdentityResultDispatch(content),
    )

    assert len(provider.calls) == 3
    assert any(
        "没有成功" in str(message.get("content", ""))
        for message in provider.calls[2]["messages"]
        if isinstance(message, dict)
    )
    assert delivered == ["这次没有改成"]


def test_real_background_identity_denial_is_bounced(monkeypatch):
    denial = (
        "error: identity write refused in background turn for identity_patch"
    )
    provider = _ScriptedProvider(
        [
            {"reply": "", "tool_calls": [_identity_call()], "usage": {}},
            {"reply": "已经改好了", "tool_calls": [], "usage": {}},
            {"reply": "身份写入被拒绝了", "tool_calls": [], "usage": {}},
        ]
    )
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    delivered = _run_identity_script(
        provider,
        _IdentityResultDispatch(denial),
    )

    assert len(provider.calls) == 3
    assert delivered == ["身份写入被拒绝了"]


def test_successful_identity_write_is_not_bounced(monkeypatch):
    provider = _ScriptedProvider(
        [
            {"reply": "", "tool_calls": [_identity_call()], "usage": {}},
            {"reply": "名字已经改好了", "tool_calls": [], "usage": {}},
        ]
    )
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    delivered = _run_identity_script(
        provider,
        _IdentityResultDispatch("ok: identity_patch applied"),
    )

    assert len(provider.calls) == 2
    assert delivered == ["名字已经改好了"]


@pytest.mark.parametrize(
    "text",
    [
        "好的，以后叫你999",
        "名字已经改好了",
        "我记住了",
        "文件我帮你改好了",
    ],
)
def test_no_identity_tool_call_never_triggers_identity_bounce(text, monkeypatch):
    provider = _ScriptedProvider(
        [{"reply": text, "tool_calls": [], "usage": {}}]
    )
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    delivered = _run_identity_script(provider, _RecordingDispatch())

    assert len(provider.calls) == 1
    assert delivered == [text]


def test_repeated_failed_identity_writes_are_bounced_at_most_once(monkeypatch):
    responses = [
        {"reply": "", "tool_calls": [_identity_call("identity-1")], "usage": {}},
        {"reply": "第一次失败", "tool_calls": [], "usage": {}},
        {"reply": "", "tool_calls": [_identity_call("identity-2")], "usage": {}},
        {"reply": "第二次仍然失败", "tool_calls": [], "usage": {}},
        {"reply": "", "tool_calls": [_identity_call("identity-3")], "usage": {}},
        {"reply": "第三次仍然失败", "tool_calls": [], "usage": {}},
        {"reply": "", "tool_calls": [_identity_call("identity-4")], "usage": {}},
        {"reply": "第四次仍然失败", "tool_calls": [], "usage": {}},
    ]
    provider = _ScriptedProvider(responses)
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    trajectory = []

    delivered = _run_identity_script(
        provider,
        _IdentityResultDispatch("error: denied"),
        max_calls=len(responses),
        trajectory=trajectory,
    )

    bounced = [
        detail
        for event_kind, detail in trajectory
        if event_kind == "identity_write_failed_bounced"
    ]
    assert len(provider.calls) == 4
    assert len(provider.responses) == 4, "the script must retain retry budget"
    assert len(bounced) == 1
    assert delivered == ["第二次仍然失败"]


def test_identity_write_tool_names_are_derived_from_write_actions():
    assert tool_loop._IDENTITY_WRITE_TOOL_NAMES == frozenset(
        action
        for action in cap_registry.WRITE_ACTIONS
        if action.startswith("identity_")
    )
    assert tool_loop._IDENTITY_WRITE_TOOL_NAMES
