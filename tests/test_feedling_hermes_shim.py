from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


SHIM_PATH = Path(__file__).parent.parent / "tools" / "feedling_hermes_shim.py"


@pytest.fixture()
def shim(monkeypatch):
    fake_cli = ModuleType("cli")
    fake_cli.HermesCLI = object
    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    spec = importlib.util.spec_from_file_location("feedling_hermes_shim_under_test", SHIM_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extracts_latest_current_turn_codex_summary_only(shim):
    history = [
        {"role": "user", "content": "old"},
        {
            "role": "assistant",
            "content": "old answer",
            "codex_reasoning_items": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "old summary"}]}
            ],
        },
        {"role": "user", "content": "current"},
        {
            "role": "assistant",
            "content": None,
            "reasoning": "raw fallback must not be used",
            "reasoning_content": "private scratchpad must not be used",
            "codex_reasoning_items": [
                {
                    "type": "reasoning",
                    "encrypted_content": "opaque-secret",
                    "summary": [
                        {"type": "summary_text", "text": "Compared the existing protocol with compatibility requirements."},
                        {"type": "summary_text", "text": "Chose the encrypted summary sub-envelope."},
                    ],
                }
            ],
        },
        {"role": "tool", "content": "tool result"},
        {"role": "assistant", "content": "final answer", "reasoning": "another raw fallback"},
    ]

    summary = shim._display_reasoning_summary_from_history(history)

    assert summary == (
        "Compared the existing protocol with compatibility requirements.\n"
        "Chose the encrypted summary sub-envelope."
    )
    assert "old summary" not in summary
    assert "scratchpad" not in summary
    assert "opaque-secret" not in summary


def test_extracts_openrouter_summary_detail_but_not_raw_reasoning(shim):
    history = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "answer",
            "reasoning": "raw chain of thought",
            "reasoning_content": "raw provider scratchpad",
            "reasoning_details": [
                {"type": "reasoning.summary", "summary": "Weighed freshness against compatibility."},
                {"type": "reasoning.text", "text": "private detail"},
            ],
        },
    ]

    assert shim._display_reasoning_summary_from_history(history) == (
        "Weighed freshness against compatibility."
    )


def test_build_payload_keeps_summary_separate_and_omits_empty_field(shim):
    with_summary = shim._resident_response_payload(
        "正文不应包含摘要。",
        reasoning_summary="判断依据单独传输。",
        model="gpt-5.6-sol",
        conversation_id="conversation-1",
        turn_id="turn-1",
        summary_source_id="reasoning-1",
    )
    without_summary = shim._resident_response_payload(
        "普通旧式回复。",
        reasoning_summary="  ",
        model="gpt-5.6-sol",
    )

    assert with_summary["messages"] == ["正文不应包含摘要。"]
    assert with_summary["reasoning_summary"] == "判断依据单独传输。"
    assert with_summary["reasoning_kind"] == "provider_reasoning_summary"
    assert with_summary["reasoning_source"] == "hermes_provider_summary"
    assert with_summary["reasoning_native"] is True
    assert with_summary["reasoning_conversation_id"] == "conversation-1"
    assert with_summary["reasoning_turn_id"] == "turn-1"
    assert with_summary["reasoning_source_id"] == "reasoning-1"
    assert "判断依据" not in with_summary["messages"][0]
    assert without_summary == {"messages": ["普通旧式回复。"]}


@pytest.mark.parametrize(
    ("provider_summary", "expected"),
    [
        (
            "Refining playful admission phrasing",
            "结合语气和关系语境判断你的意思，再调整回应方式。",
        ),
        (
            "Confirming wake time retention",
            "核对了时间、行程和关键安排，确认回复里的执行步骤。",
        ),
        (
            "Compared the existing protocol with compatibility requirements.\n"
            "Chose the encrypted summary sub-envelope.",
            "核对了现有实现与兼容要求，并选择了更安全的处理方式。",
        ),
    ],
)
def test_localizes_english_provider_summary_for_chinese_ui(
    shim, provider_summary, expected
):
    summary, native = shim._localize_display_summary(provider_summary)

    assert summary == expected
    assert native is False


def test_keeps_chinese_provider_summary_native(shim):
    summary, native = shim._localize_display_summary("核对预约时间并调整出发安排。")

    assert summary == "核对预约时间并调整出发安排。"
    assert native is True


def test_payload_marks_localized_summary_as_non_native(shim):
    payload = shim._resident_response_payload(
        "正文。",
        reasoning_summary="先理解你的意思和语境，再整理成更清楚、自然的回应。",
        model="gpt-5.6-sol",
        reasoning_source="hermes_localized_provider_summary",
        reasoning_native=False,
        conversation_id="conversation-1",
        turn_id="turn-1",
        summary_source_id="reasoning-1",
    )

    assert payload["reasoning_source"] == "hermes_localized_provider_summary"
    assert payload["reasoning_native"] is False


def test_history_summary_contract_localizes_before_payload(shim):
    history = [
        {"role": "user", "content": "nickname?"},
        {
            "role": "assistant",
            "content": "answer",
            "codex_reasoning_items": [
                {
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "Interpreting nickname meaning"}
                    ],
                }
            ],
        },
    ]

    summary, source, native = shim._localized_display_summary_from_history(history)

    assert summary == "结合语气和关系语境判断你的意思，再调整回应方式。"
    assert source == "hermes_localized_provider_summary"
    assert native is False


def test_bound_summary_uses_only_the_explicit_current_turn(shim):
    stale_history = [
        {
            "role": "assistant",
            "content": "older answer",
            "codex_reasoning_items": [
                {
                    "type": "reasoning",
                    "id": "rs_old",
                    "summary": [
                        {
                            "type": "summary_text",
                            "text": "Compared the existing protocol with compatibility requirements.",
                        }
                    ],
                }
            ],
        },
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "current answer"},
    ]

    binding = shim._bound_localized_summary_from_turn(
        stale_history,
        conversation_id="session-current",
        turn_id="user-message-current",
    )

    assert binding is None


def test_bound_summary_carries_conversation_turn_and_provider_source_ids(shim):
    current_turn = [
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": "current answer",
            "codex_reasoning_items": [
                {
                    "type": "reasoning",
                    "id": "rs_current",
                    "summary": [
                        {"type": "summary_text", "text": "Interpreting nickname meaning"}
                    ],
                }
            ],
        },
    ]

    binding = shim._bound_localized_summary_from_turn(
        current_turn,
        conversation_id="session-current",
        turn_id="user-message-current",
    )

    assert binding is not None
    assert binding.summary == "结合语气和关系语境判断你的意思，再调整回应方式。"
    assert binding.conversation_id == "session-current"
    assert binding.turn_id == "user-message-current"
    assert binding.summary_source_id == "rs_current"


@pytest.mark.parametrize(
    ("conversation_id", "turn_id"),
    [("", "turn-1"), ("conversation-1", "")],
)
def test_bound_summary_is_omitted_without_complete_correlation_ids(
    shim, conversation_id, turn_id
):
    current_turn = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "answer",
            "codex_reasoning_items": [
                {
                    "type": "reasoning",
                    "id": "rs_current",
                    "summary": [
                        {"type": "summary_text", "text": "Interpreting nickname meaning"}
                    ],
                }
            ],
        },
    ]

    assert shim._bound_localized_summary_from_turn(
        current_turn,
        conversation_id=conversation_id,
        turn_id=turn_id,
    ) is None