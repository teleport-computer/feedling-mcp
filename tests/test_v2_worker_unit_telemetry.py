"""Pure Runtime V2 telemetry/error-code tests (no PostgreSQL required)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import re

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from core import self_thinking  # noqa: E402
from model_api_runtime.v2 import tool_loop  # noqa: E402
from model_api_runtime.v2 import worker  # noqa: E402
from model_api_runtime.v2 import language_follow  # noqa: E402
from model_api_runtime.v2 import prompt_frontier  # noqa: E402
from model_api_runtime.v2 import summary_frontier  # noqa: E402


def test_thinking_extra_preserves_plaintext_body():
    extra = worker._thinking_extra({
        "envelope": {
            "id": "thinking-plain",
            "body": "private reasoning",
            "owner_user_id": "usr_plain",
            "visibility": "shared",
        },
        "metadata": {"thinking_kind": "reasoning"},
    })

    assert extra["thinking_body"] == "private reasoning"
    assert "thinking_body_ct" not in extra


@pytest.mark.parametrize(
    "reason",
    [
        "degenerate_reply_suppressed",
        "protocol_fragment_suppressed",
        "malformed_self_thinking_suppressed",
    ],
)
def test_wake_safety_suppressions_keep_distinct_stable_codes(reason):
    exc = worker.TurnError(reason)
    assert worker._safe_failure_code("wake_failed", exc) == f"wake_failed:{reason}"
    assert worker._turn_failure_error_class(exc) == "reply_parse_failed"


def test_provider_attempt_ledger_inherits_job_lane_when_event_omits_it(monkeypatch):
    captured = {}

    def _capture(user_id, event_kind, payload, **kwargs):
        captured.update(
            user_id=user_id,
            event_kind=event_kind,
            payload=payload,
            kwargs=kwargs,
        )

    monkeypatch.setattr(worker, "_note_provider_attempt", _capture)

    class _Recorder:
        user_id = "u_lane"
        job_id = 41
        _ledger_lane = "maintenance"
        _ledger_route = ("anthropic", "claude-test")

    original = {"error_class": "upstream_unavailable"}
    asyncio.run(
        worker._mirror_provider_attempt(_Recorder(), "provider_error", original)
    )

    assert original == {"error_class": "upstream_unavailable"}
    assert captured["payload"]["lane"] == "maintenance"
    assert captured["kwargs"]["provider"] == "anthropic"


def _minimal_deps():
    return worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "token",
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("这是一个用于测试的完整中文句子", "han"),
        ("This is a complete English sentence", "latin"),
        ("これはひらがなとカタカナのテストです", "kana"),
        ("한국어로 작성한 충분히 긴 문장입니다", "hangul"),
        ("Это достаточно длинное русское предложение", "cyrillic"),
        ("汉汉汉汉汉abcde", "mixed"),
        ("😀🎉 123 !!!", "indeterminate"),
        ("hello!", "indeterminate"),
        ("def compute_value(items): return items", "latin"),
    ],
)
def test_writing_system_classifier_uses_only_letters(text, expected):
    assert language_follow.classify_writing_system(text) == expected


def test_writing_system_classifier_requires_strictly_more_than_sixty_percent():
    assert language_follow.classify_writing_system("汉汉汉汉汉汉abcd") == "mixed"
    assert language_follow.classify_writing_system("汉汉汉汉汉汉汉abc") == "han"


def test_language_correction_instruction_is_pinned_verbatim():
    assert language_follow.CORRECTION_INSTRUCTION == (
        "你刚才这条回复,语言和这个人正在说的语言对不上。除非这个人要求过你用别的语言,"
        "否则用这个人的语言把同一条回复重说一遍:内容、语气、分寸都不变,只换语言。"
        "要是这个人确实要求过现在这种语言,就原样重复原回复。"
    )


def test_latest_user_writing_system_skips_short_newest_message():
    rows = [
        {
            "role": "user",
            "content": "这是前一条足够长而且有实质内容的中文消息",
        },
        {"role": "assistant", "content": "assistant text is ignored"},
        {"role": "user", "content": "ok"},
    ]

    assert worker._latest_user_writing_system(rows) == "han"


def test_language_follow_trace_is_closed_enum_only_and_admin_readable():
    from admin import data_track

    captured = {}

    def _emit(user_id, event_type, **fields):
        captured.update(user_id=user_id, type=event_type, **fields)

    asyncio.run(worker._emit_reply_language_follow_trace(
        _emit,
        "u_language_trace",
        user_rows=[{
            "role": "user",
            "content": "这是用户不会进入遥测的私密中文正文",
        }],
        visible_reply="This private reply body must not enter telemetry",
        lane="chat",
    ))

    assert captured["type"] == "reply.language_follow"
    assert captured["detail"] == {
        "user_script": "han",
        "reply_script": "latin",
        "outcome": "mismatch",
        "lane": "chat",
        "correction_attempted": False,
        "correction_outcome": "skipped",
    }
    assert set(captured["detail"]) == {
        "user_script", "reply_script", "outcome", "lane",
        "correction_attempted", "correction_outcome",
    }
    assert "私密中文正文" not in str(captured)
    assert "private reply body" not in str(captured)
    assert data_track._debug_event_public_json(captured)["detail"] == (
        captured["detail"]
    )

    malicious = dict(captured)
    malicious["detail"] = {
        **captured["detail"],
        "reply_script": "private reply body",
        "correction_outcome": "private correction detail",
        "body": "private user text",
    }
    redacted = data_track._debug_event_public_json(malicious)["detail"]
    assert redacted["reply_script"] == "<redacted string len=18>"
    assert redacted["correction_outcome"] == "<redacted string len=25>"
    assert redacted["body"] == "<redacted string len=17>"


def test_language_follow_trace_skips_without_anchor_and_never_raises():
    captured = {}

    def _emit(_user_id, _event_type, **fields):
        captured.update(fields)

    asyncio.run(worker._emit_reply_language_follow_trace(
        _emit,
        "u_language_skip",
        user_rows=[{"role": "user", "content": "ok"}],
        visible_reply="A sufficiently long visible English reply",
        lane="wake",
    ))
    assert captured["detail"] == {
        "user_script": "indeterminate",
        "reply_script": "latin",
        "outcome": "skip",
        "lane": "wake",
        "correction_attempted": False,
        "correction_outcome": "skipped",
    }

    asyncio.run(worker._emit_reply_language_follow_trace(
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        "u_language_fail_open",
        user_rows=[{"role": "user", "content": "A long English user message"}],
        visible_reply="A long English visible reply",
        lane="chat",
    ))


def test_language_follow_has_dedicated_admin_timeline_label():
    from admin import data_track

    assert data_track._debug_friendly_step({
        "type": "reply.language_follow",
        "subsystem": "agent",
    }) == ("🌐", "语言跟随")


def test_prompt_frontier_trace_reaches_final_sink_with_closed_content_free_shape():
    from admin import data_track

    sentinel = "PRIVATE_PROMPT_BODY_must_never_reach_trace"
    traces = []
    deps = _minimal_deps()
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append({
        "user_id": user_id,
        "type": event_type,
        **fields,
    })
    limit = prompt_frontier.ModelPromptLimit(
        provider="test",
        model="budget-test",
        context_window_tokens=2_048,
        source="caller",
    )
    plan = prompt_frontier.plan_provider_round(
        model_limit=limit,
        messages=[{"role": "system", "content": "x" * 750}],
        tools=None,
        output_reserve_tokens=512,
        safety_margin_tokens=512,
        message_component_bytes=(
            prompt_frontier.PromptByteComponent("system", 750),
        ),
    )
    sink = worker._ledger_tapped_sink(
        None,
        deps=deps,
        user_id="u_budget_trace",
        lane="chat",
    )
    asyncio.run(sink("provider_request", {
        "prompt_frontier": plan,
        "messages": [{"role": "user", "content": sentinel}],
    }))

    budget_trace = traces[-1]
    assert budget_trace["type"] == "v2.prompt_frontier.budget"
    assert budget_trace["status"] == "warning"
    assert budget_trace["detail"] == {
        "context_window_tokens": 2_048,
        "input_budget_tokens": 1_024,
        "required_tokens": plan.estimated_input_tokens,
        "estimated_input_tokens": plan.estimated_input_tokens,
        "overflow_tokens": 0,
        "output_reserve_tokens": 512,
        "safety_margin_tokens": 512,
        "utf8_bytes_per_token": 1.0,
        "limit_source": "caller",
        "lane": "chat",
        "required_components": ["message_context"],
        "components": [{"name": "system", "bytes": 750}],
    }
    assert sentinel not in repr(budget_trace)

    with pytest.raises(prompt_frontier.PromptFrontierExhausted) as caught:
        prompt_frontier.plan_provider_round(
            model_limit=limit,
            messages=[{"role": "system", "content": sentinel * 40}],
            tools=None,
            output_reserve_tokens=512,
            safety_margin_tokens=512,
            message_component_bytes=(
                prompt_frontier.PromptByteComponent("system", 1_700),
            ),
        )
    callback = worker._prompt_frontier_exhaustion_trace_callback(
        deps, "u_budget_trace", "heartbeat"
    )
    asyncio.run(callback(caught.value))

    exhausted = traces[-1]
    assert exhausted["type"] == "v2.prompt_frontier.exhausted"
    assert exhausted["status"] == "warning"
    assert exhausted["detail"]["overflow_tokens"] == (
        exhausted["detail"]["required_tokens"] - 1_024
    )
    assert exhausted["detail"]["limit_source"] == "caller"
    assert exhausted["detail"]["lane"] == "heartbeat"
    assert exhausted["detail"]["components"] == [
        {"name": "system", "bytes": 1_700},
    ]
    assert sentinel not in repr(exhausted)
    assert data_track._debug_event_public_json(exhausted)["detail"] == (
        exhausted["detail"]
    )


def test_prompt_frontier_trace_rejects_component_plaintext_before_emit():
    sentinel = "PRIVATE_COMPONENT_TEXT"
    traces = []
    deps = _minimal_deps()
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append({
        "user_id": user_id,
        "type": event_type,
        **fields,
    })
    error = prompt_frontier.PromptFrontierExhausted(
        required_tokens=2_000,
        input_budget_tokens=1_000,
        context_window_tokens=2_000,
        required_components=("message_context",),
        limit_source="caller",
        output_reserve_tokens=500,
        safety_margin_tokens=500,
        utf8_bytes_per_token=1.0,
        component_bytes=(
            prompt_frontier.PromptByteComponent("system", 2_000),
        ),
    )
    error.component_bytes = ({
        "name": "system",
        "bytes": 2_000,
        "content": sentinel,
    },)

    worker._emit_prompt_frontier_trace(
        deps,
        "u_budget_guard",
        error,
        lane="chat",
    )

    assert traces == []


def test_prompt_frontier_trace_rejects_plaintext_component_name_before_emit():
    traces = []
    deps = _minimal_deps()
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append({
        "user_id": user_id,
        "type": event_type,
        **fields,
    })
    error = prompt_frontier.PromptFrontierExhausted(
        required_tokens=2_000,
        input_budget_tokens=1_000,
        context_window_tokens=2_000,
        required_components=("message_context",),
        limit_source="caller",
        output_reserve_tokens=500,
        safety_margin_tokens=500,
        utf8_bytes_per_token=1.0,
        component_bytes=(
            prompt_frontier.PromptByteComponent("system", 2_000),
        ),
    )
    error.component_bytes = ({
        "name": "她说她今天很累",
        "bytes": 2_000,
    },)

    worker._emit_prompt_frontier_trace(
        deps,
        "u_budget_guard",
        error,
        lane="chat",
    )

    assert traces == []


def test_prompt_frontier_message_breakdown_uses_only_semantic_closed_names():
    messages = [
        {"role": "system", "content": "private system"},
        {
            "role": "user",
            "content": worker.context._SUMMARY_HEADER + "private summary",
        },
        {
            "role": "user",
            "content": (
                worker.context.WORLD_BOOK_CONTEXT_HEADER + "\nprivate world"
            ),
        },
        {
            "role": "user",
            "content": (
                worker.context.RUNTIME_CONTEXT_HEADER + "\nprivate runtime"
            ),
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": "private screen"}],
            worker.v2_screen_chat.MESSAGE_TAG: True,
        },
        {"role": "user", "content": "private tail"},
    ]

    breakdown = worker._prompt_frontier_message_component_bytes(messages)

    assert [item.name for item in breakdown] == [
        "system", "summary", "tail", "worldbook", "runtime_data", "screen",
    ]
    assert all(type(item.bytes) is int and item.bytes > 0 for item in breakdown)
    assert "private" not in repr(breakdown)


@pytest.mark.parametrize(
    ("self_on", "self_text", "failed", "provider_text", "expected"),
    [
        (True, "agent summary", False, "private native cot", "self"),
        (True, "", True, "private native cot", "marker"),
        (True, "", False, "private native cot", "none"),
        (False, "", False, "private native cot", "native_legacy"),
    ],
)
def test_thinking_surface_selector_has_four_explicit_branches(
    self_on, self_text, failed, provider_text, expected
):
    text, kind, source, native, branch = worker._select_thinking_surface(
        provider_text,
        self_thinking_on=self_on,
        self_thinking_text=self_text,
        self_thinking_failed=failed,
    )

    assert branch == expected
    if expected == "self":
        assert text == self_text
        assert (kind, source, native) == (
            "agent_summary",
            "self_thinking",
            False,
        )
    elif expected == "marker":
        assert text == worker.self_thinking.THINKING_FAILED_MARKER
    elif expected == "none":
        assert text == ""
    else:
        assert text == provider_text
        assert (kind, source, native) == ("provider_reasoning", None, True)


def test_self_thinking_internal_terms_are_derived_from_tool_specs():
    terms = worker._self_thinking_internal_terms()
    assert terms == {
        str(spec.name)
        for spec in worker.cap_tool_schema.build_tool_specs()
        if str(spec.name).strip()
    }
    assert worker._self_thinking_internal_term("我会调用 memory_write") == "memory_write"
    assert worker._self_thinking_internal_term("我会聊天") is None


def test_self_thinking_language_mismatch_is_closed_and_content_free():
    assert worker._self_thinking_language_mismatch(
        "Let me check this carefully", [{"role": "user", "content": "请帮我看看这个内容好吗"}]
    ) == ("han", "latin")
    assert worker._self_thinking_language_mismatch(
        "我来认真看看这个内容好吗", [{"role": "user", "content": "请帮我看看这个内容好吗"}]
    ) is None


def test_thinking_surface_trace_contains_metadata_only():
    captured = {}

    def _emit(user_id, event_type, **fields):
        captured.update(user_id=user_id, event_type=event_type, **fields)

    config = type("Provider", (), {"model": "model-safe-name"})()
    asyncio.run(
        worker._emit_thinking_surfaced_trace(
            _emit,
            "u_trace",
            config,
            lane="chat",
            branch="self",
            chars=17,
        )
    )

    assert captured["event_type"] == "thinking.surfaced"
    assert captured["detail"] == {
        "branch": "self",
        "chars": 17,
        "model": "model-safe-name",
        "lane": "chat",
    }
    assert set(captured["detail"]) == {"branch", "chars", "model", "lane"}


def test_thinking_surface_trace_bounds_user_configured_model_name():
    captured = {}

    def _emit(_user_id, _event_type, **fields):
        captured.update(fields)

    overlong_model = "relay-model-" * 20
    config = type("Provider", (), {"model": overlong_model})()
    asyncio.run(
        worker._emit_thinking_surfaced_trace(
            _emit,
            "u_trace",
            config,
            lane="wake",
            branch="none",
            chars=0,
        )
    )

    assert captured["detail"]["model"] == overlong_model[:96]
    assert len(captured["detail"]["model"]) < len(overlong_model)


def test_thinking_surface_has_dedicated_admin_timeline_label():
    from admin import data_track

    assert data_track._debug_friendly_step(
        {"type": "thinking.surfaced", "subsystem": "agent"}
    ) == ("💭", "思考展示 · 分支")


def test_empty_provider_response_trace_is_content_free_and_admin_readable():
    from admin import data_track

    captured = {}

    def _emit(user_id, event_type, **fields):
        captured.update(user_id=user_id, type=event_type, **fields)

    deps = _minimal_deps()
    deps.emit_debug_trace = _emit
    callback = worker._empty_provider_response_debug_callback(
        deps, "u_trace", "chat", "trace-empty-response"
    )
    assert callback is not None
    asyncio.run(callback({
        "stop_reason": "content_filter",
        "has_visible_text": False,
        "reasoning_present": True,
        "tool_call_count": 0,
        "completion_tokens": 41,
        "reply_excerpt": "MUST NEVER REACH DEBUG TRACE",
    }))

    assert captured["type"] == "provider.empty_response"
    assert captured["trace_id"] == "trace-empty-response"
    assert captured["detail"] == {
        "stop_reason": "content_filter",
        "has_visible_text": False,
        "reasoning_present": True,
        "tool_call_count": 0,
        "completion_tokens": 41,
        "lane": "chat",
    }
    assert set(captured["detail"]) == {
        "stop_reason",
        "has_visible_text",
        "reasoning_present",
        "tool_call_count",
        "completion_tokens",
        "lane",
    }
    public = data_track._debug_event_public_json(captured)
    assert public["trace_id"] == "trace-empty-response"
    assert public["detail"] == captured["detail"]
    assert "MUST NEVER REACH DEBUG TRACE" not in str(captured)

    malicious = dict(captured)
    malicious["detail"] = {
        **captured["detail"],
        "stop_reason": "short_private_text",
        "lane": "another_private_text",
    }
    redacted = data_track._debug_event_public_json(malicious)["detail"]
    assert redacted["stop_reason"] == "<redacted string len=18>"
    assert redacted["lane"] == "<redacted string len=20>"


def test_empty_provider_response_has_dedicated_admin_timeline_label():
    from admin import data_track

    assert data_track._debug_friendly_step({
        "type": "provider.empty_response",
        "subsystem": "agent",
    }) == ("🕳️", "空回复诊断")


def test_provider_roundtrip_trace_closed_enums_are_admin_readable():
    from admin import data_track

    assert "force_text_fallback" in (
        tool_loop._PROVIDER_TERMINAL_TEXT_ROUND_REASONS
    )
    assert "tool_schema_rejected" in (
        tool_loop._PROVIDER_FORCE_TEXT_FALLBACK_REASONS
    )
    captured = []
    deps = _minimal_deps()
    deps.emit_debug_trace = lambda user_id, event_type, **fields: captured.append(
        {"user_id": user_id, "type": event_type, **fields}
    )
    trace = worker._provider_tool_surface_callback(
        deps, "u_roundtrip_trace", "chat", "trace-roundtrip"
    )
    assert trace is not None
    asyncio.run(trace({
        "round": 2,
        "candidate_tool_count": 4,
        "sent_tool_count": 0,
        "dropped_tool_count": 4,
        "mcp_candidate_tool_count": 2,
        "mcp_sent_tool_count": 0,
        "mcp_dropped_tool_count": 2,
        "reason": "tool_schema_rejected",
        "terminal_text_round": True,
        "terminal_text_round_reason": "force_text_fallback",
        "force_text_fallback_reason": "tool_schema_rejected",
        "empty_response_recovery": False,
    }))
    asyncio.run(trace.emit_summary())

    roundtrip = next(
        event for event in captured if event["type"] == "mcp.roundtrip.provider"
    )
    assert roundtrip["trace_id"] == "trace-roundtrip"
    public = data_track._debug_event_public_json(roundtrip)
    assert public["detail"]["lane"] == "chat"
    assert public["detail"]["terminal_text_round_reason"] == (
        "force_text_fallback"
    )
    assert public["detail"]["force_text_fallback_reason"] == (
        "tool_schema_rejected"
    )

    surface = next(
        event for event in captured if event["type"] == "mcp.surface.provider"
    )
    assert surface["trace_id"] == "trace-roundtrip"
    surface_public = data_track._debug_event_public_json(surface)["detail"]
    assert surface_public["lane"].startswith("<redacted string")
    assert surface_public["terminal_text_round_reason"].startswith(
        "<redacted string"
    )


def test_provider_roundtrip_trace_normalizes_unknowns_and_admin_redacts_forgery():
    from admin import data_track

    private_lane = "private lane from caller"
    private_terminal = "private terminal reason"
    private_fallback = "private fallback reason"
    captured = []
    deps = _minimal_deps()
    deps.emit_debug_trace = lambda user_id, event_type, **fields: captured.append(
        {"user_id": user_id, "type": event_type, **fields}
    )
    trace = worker._provider_tool_surface_callback(
        deps, "u_roundtrip_unknown", private_lane
    )
    assert trace is not None
    asyncio.run(trace({
        "round": 1,
        "candidate_tool_count": 0,
        "sent_tool_count": 0,
        "dropped_tool_count": 0,
        "mcp_candidate_tool_count": 0,
        "mcp_sent_tool_count": 0,
        "mcp_dropped_tool_count": 0,
        "reason": "none",
        "terminal_text_round": True,
        "terminal_text_round_reason": private_terminal,
        "force_text_fallback_reason": private_fallback,
        "empty_response_recovery": False,
    }))
    asyncio.run(trace.emit_summary())

    roundtrip = next(
        event for event in captured if event["type"] == "mcp.roundtrip.provider"
    )
    assert roundtrip["detail"]["lane"] == "other"
    assert roundtrip["detail"]["wake_kind"] == "other"
    assert roundtrip["detail"]["terminal_text_round_reason"] == "other"
    assert roundtrip["detail"]["force_text_fallback_reason"] == "other"
    assert private_lane not in str(roundtrip)
    assert private_terminal not in str(roundtrip)
    assert private_fallback not in str(roundtrip)
    normalized_public = data_track._debug_event_public_json(roundtrip)["detail"]
    assert normalized_public["lane"] == "other"
    assert normalized_public["wake_kind"] == "other"
    assert normalized_public["terminal_text_round_reason"] == "other"
    assert normalized_public["force_text_fallback_reason"] == "other"

    forged = {
        **roundtrip,
        "detail": {
            **roundtrip["detail"],
            "lane": private_lane,
            "wake_kind": private_lane,
            "terminal_text_round_reason": private_terminal,
            "force_text_fallback_reason": private_fallback,
        },
    }
    forged_public = data_track._debug_event_public_json(forged)["detail"]
    for key in (
        "lane",
        "wake_kind",
        "terminal_text_round_reason",
        "force_text_fallback_reason",
    ):
        assert forged_public[key].startswith("<redacted string")


def test_combined_memory_worldbook_message_keeps_truncation_trace_visible():
    captured = {}

    def _emit(user_id, event_type, **fields):
        captured.update(user_id=user_id, type=event_type, **fields)

    deps = _minimal_deps()
    deps.emit_debug_trace = _emit
    combined = (
        worker.context.AGENT_MEMORY_HEADER
        + "\nremembered fact\n\n"
        + worker.context.USER_PROFILE_HEADER
        + "\npreferred voice\n\n"
        + worker.context.WORLD_BOOK_CONTEXT_HEADER
        + "\n<world_book>bounded setting</world_book>"
        + worker.context.WORLD_BOOK_TRUNCATION_MARKER
    )

    worker._emit_context_truncation_trace(
        deps,
        "u_combined_worldbook",
        {"messages": [{"role": "user", "content": combined}]},
    )

    assert captured == {
        "user_id": "u_combined_worldbook",
        "type": "context.truncation",
        "status": "warning",
        "summary": "",
        "explain": "",
        "detail": {
            "counts": {
                "profile_cards_truncated": 0,
                "worldbook_truncated": 1,
            }
        },
    }


def test_post_fold_checkpoint_exhaustion_is_content_free_degradation(monkeypatch):
    recorded = {}

    async def _exhausted(*_args, **_kwargs):
        raise summary_frontier.SummaryFrontierExhausted(
            "checkpoint_pass_budget_exhausted"
        )

    async def _record(_recorder, kind, payload, **_kwargs):
        recorded.update(kind=kind, payload=payload)
        return True

    monkeypatch.setattr(worker, "_rebalance_summary_frontier", _exhausted)
    monkeypatch.setattr(worker, "_record_trajectory", _record)

    landed = asyncio.run(
        worker._rebalance_summary_frontier_best_effort(
            "u_checkpoint",
            _minimal_deps(),
            lane="maintenance",
            phase="post_fold",
            enclave_sem=None,
        )
    )

    assert landed is False
    assert recorded["kind"] == "compaction_checkpoint_degraded"
    assert recorded["payload"]["detail"] == "checkpoint_pass_budget_exhausted"
    assert recorded["payload"]["phase"] == "post_fold"


def test_post_fold_frontier_integrity_error_is_still_fatal(monkeypatch):
    async def _corrupt(*_args, **_kwargs):
        raise summary_frontier.SummaryFrontierIntegrityError(
            "non_contiguous_exact_frontier"
        )

    monkeypatch.setattr(worker, "_rebalance_summary_frontier", _corrupt)

    with pytest.raises(summary_frontier.SummaryFrontierIntegrityError):
        asyncio.run(
            worker._rebalance_summary_frontier_best_effort(
                "u_checkpoint",
                _minimal_deps(),
                lane="maintenance",
                phase="post_fold",
                enclave_sem=None,
            )
        )


def test_post_fold_checkpoint_timeout_is_bounded_degradation(monkeypatch):
    recorded = {}

    async def _never_finishes(*_args, **_kwargs):
        await asyncio.Future()

    async def _record(_recorder, kind, payload, **_kwargs):
        recorded.update(kind=kind, payload=payload)
        return True

    monkeypatch.setattr(worker, "_rebalance_summary_frontier", _never_finishes)
    monkeypatch.setattr(worker, "_record_trajectory", _record)

    landed = asyncio.run(
        worker._rebalance_summary_frontier_best_effort(
            "u_checkpoint",
            _minimal_deps(),
            lane="maintenance",
            phase="post_fold",
            enclave_sem=None,
            timeout_sec=0.01,
        )
    )

    assert landed is False
    assert recorded["kind"] == "compaction_checkpoint_degraded"
    assert recorded["payload"]["detail"] == "checkpoint_timeout"
    assert "error_code" not in recorded["payload"]


def test_checkpoint_degradation_never_logs_arbitrary_detail_or_code(
    monkeypatch, caplog
):
    recorded = {}

    class SecretCheckpointError(RuntimeError):
        detail = "sk_live_customer_secret"
        code = "private_user_content"

    async def _fails(*_args, **_kwargs):
        raise SecretCheckpointError("raw-secret-message")

    async def _record(_recorder, kind, payload, **_kwargs):
        recorded.update(kind=kind, payload=payload)
        return True

    monkeypatch.setattr(worker, "_rebalance_summary_frontier", _fails)
    monkeypatch.setattr(worker, "_record_trajectory", _record)

    landed = asyncio.run(
        worker._rebalance_summary_frontier_best_effort(
            "u_checkpoint",
            _minimal_deps(),
            lane="maintenance",
            phase="post_fold",
            enclave_sem=None,
        )
    )

    assert landed is False
    assert recorded["payload"]["detail"] == "checkpoint_secretcheckpointerror"
    combined = str(recorded) + caplog.text
    assert "sk_live_customer_secret" not in combined
    assert "private_user_content" not in combined
    assert "raw-secret-message" not in combined


# ── B4-5 (#3):思考气泡的内部【字段名】黑名单 ─────────────────────────
# 词表共享(core.self_thinking),**宽度按道分开**:
#   V1 逐行丢弃 → 宽匹配;V2 整段替换成 marker → 窄匹配(只认泄漏形状)。


@pytest.mark.parametrize("ordinary", [
    "用户问 UUID 是什么",
    "讨论 system prompt 的设计",
    "学习 chain of thought prompting",
    "他在研究 chain - of thought",
    "他刚吃完脑花，撑得不行，我陪他消化一会儿",
])
def test_normal_technical_talk_is_not_suppressed(ordinary):
    """不误伤(codex2 实测挑出的三句真会发生的话)。

    V2 命中即把**整段**内心话换成「(思考没写完)」。用户完全可能跟伴侣聊
    prompt 设计、聊 UUID 是什么 —— 把这些吞掉,他只会看到一句占位符,
    而且不知道为什么。**误判的代价比漏判高。**
    """
    assert self_thinking.internal_field_leak(ordinary) is None


@pytest.mark.parametrize("leak", [
    "session_id: abc123",
    '"input_tokens": 12',
    "costUSD=0.02",
    "terminal_reason -> x",
    "permission_denials: []",
    '{"uuid":"x"}',
])
def test_real_field_leaks_are_caught(leak):
    """真泄漏必须命中:词后面紧跟分隔符/取值,概念提及不会长这样。"""
    assert self_thinking.internal_field_leak(leak) is not None


def test_shared_pattern_keeps_v1_wide_forms():
    """共享 pattern 必须保留 V1 原有的宽形态,否则 V1 会开始漏。"""
    pattern = self_thinking.internal_field_terms_pattern()
    for term in self_thinking.INTERNAL_FIELD_TERMS:
        assert re.escape(term) in pattern
    assert re.search(pattern, "chain - of thought", re.IGNORECASE), (
        "V1 原有的 chain[-\\s]*of[-\\s]*thought 宽形态丢了"
    )


def test_v2_guard_covers_tool_names_and_field_leaks():
    """V2 判据同时盖两类,且对概念提及放行。"""
    from model_api_runtime.v2 import worker as v2_worker

    assert v2_worker._self_thinking_internal_term("我调 memory_write 存一下")
    assert v2_worker._self_thinking_internal_term("session_id: 我看下这个")
    assert v2_worker._self_thinking_internal_term("讨论 system prompt 的设计") is None
