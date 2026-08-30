from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from agent_protocol_core import self_thinking


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client as pc  # noqa: E402
from provider_types import ToolSpec  # noqa: E402


PREFILL = pc.SELF_THINKING_ASSISTANT_PREFILL
MESSAGES = [{"role": "user", "content": "answer"}]


class _Response:
    status_code = 200

    def __init__(self, body: dict):
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


@pytest.mark.parametrize(
    ("provider", "model", "supported"),
    [
        ("anthropic", "claude-haiku-4-5-20251001", True),
        ("anthropic", "claude-sonnet-4-5-20250929", True),
        ("anthropic", "claude-opus-4-5-20251101", True),
        ("anthropic", "claude-sonnet-4-6", False),
        ("bedrock", "anthropic.claude-haiku-4-5", False),
        ("gemini", "gemini-2.5-flash", True),
        ("gemini", "gemini-3.1-pro-preview", True),
        ("gemini", "gemini-3.6-flash", False),
        ("openai", "gpt-4o-mini", False),
        ("openai", "gpt-5.2", False),
        ("deepseek", "deepseek-v4-flash", False),
        ("openai_compatible", "claude-haiku-4-5", False),
        ("openrouter", "anthropic/claude-haiku-4.5", True),
        ("openrouter", "anthropic/claude-sonnet-4.6", False),
        ("openrouter", "google/gemini-2.5-flash", True),
        ("openrouter", "google/gemini-3.1-pro-preview", True),
        ("openrouter", "openai/gpt-4o-mini", False),
    ],
)
def test_live_measured_assistant_prefill_capability_gate(
    provider, model, supported
):
    assert pc._model_supports_assistant_prefill(provider, model) is supported


def _openai_chat_payload(
    provider: str,
    model: str,
    prefill: str,
    *,
    tools=None,
    tool_choice=None,
    allow_image_output: bool = False,
) -> dict:
    effective_prefill = pc._effective_assistant_prefill(
        provider=provider,
        model=model,
        requested=prefill,
        tools=tools,
        tool_choice=tool_choice,
        allow_image_output=allow_image_output,
    )
    return pc._build_openai_compat_payload(
        provider=provider,
        model=model,
        messages=MESSAGES,
        temperature=None,
        max_tokens=64,
        response_format=None,
        extra_body=None,
        include_reasoning=False,
        tools=tools,
        tool_choice=tool_choice,
        allow_image_output=allow_image_output,
        assistant_prefill=effective_prefill,
    )


def test_supported_wires_append_the_native_assistant_prefill_shape():
    anthropic_prefill = pc._effective_assistant_prefill(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        requested=PREFILL,
    )
    anthropic, _, _ = pc._build_anthropic_payload(
        model="claude-haiku-4-5-20251001",
        base_url="https://api.anthropic.com/v1",
        key="k",
        messages=MESSAGES,
        max_tokens=64,
        temperature=None,
        response_format=None,
        assistant_prefill=anthropic_prefill,
    )
    gemini_prefill = pc._effective_assistant_prefill(
        provider="gemini",
        model="gemini-3.1-pro-preview",
        requested=PREFILL,
    )
    gemini, _, _ = pc._build_gemini_payload(
        model="gemini-3.1-pro-preview",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        key="k",
        messages=MESSAGES,
        max_tokens=64,
        temperature=None,
        response_format=None,
        assistant_prefill=gemini_prefill,
    )
    openrouter = _openai_chat_payload(
        "openrouter", "anthropic/claude-haiku-4.5", PREFILL
    )

    assert anthropic["messages"][-1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": PREFILL}],
    }
    assert gemini["contents"][-1] == {
        "role": "model",
        "parts": [{"text": PREFILL}],
    }
    assert openrouter["messages"][-1] == {
        "role": "assistant",
        "content": PREFILL,
    }


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-4o-mini"),
        ("deepseek", "deepseek-v4-flash"),
        ("openai_compatible", "arbitrary-custom-model"),
        ("openrouter", "openai/gpt-4o-mini"),
        ("openrouter", "anthropic/claude-sonnet-4.6"),
    ],
)
def test_unsupported_openai_chat_provider_payload_is_byte_for_byte_unchanged(
    provider, model
):
    assert _openai_chat_payload(provider, model, PREFILL) == _openai_chat_payload(
        provider, model, ""
    )


def test_other_model_gated_wire_payloads_are_byte_for_byte_unchanged():
    anthropic_prefill = pc._effective_assistant_prefill(
        provider="anthropic",
        model="claude-sonnet-4-6",
        requested=PREFILL,
    )
    anthropic_with = pc._build_anthropic_payload(
        model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com/v1",
        key="k",
        messages=MESSAGES,
        max_tokens=64,
        temperature=None,
        response_format=None,
        assistant_prefill=anthropic_prefill,
    )[0]
    anthropic_without = pc._build_anthropic_payload(
        model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com/v1",
        key="k",
        messages=MESSAGES,
        max_tokens=64,
        temperature=None,
        response_format=None,
    )[0]
    gemini_prefill = pc._effective_assistant_prefill(
        provider="gemini",
        model="gemini-3.6-flash",
        requested=PREFILL,
    )
    gemini_with = pc._build_gemini_payload(
        model="gemini-3.6-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        key="k",
        messages=MESSAGES,
        max_tokens=64,
        temperature=None,
        response_format=None,
        assistant_prefill=gemini_prefill,
    )[0]
    gemini_without = pc._build_gemini_payload(
        model="gemini-3.6-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        key="k",
        messages=MESSAGES,
        max_tokens=64,
        temperature=None,
        response_format=None,
    )[0]

    assert anthropic_with == anthropic_without
    assert gemini_with == gemini_without


def test_prefill_is_disabled_on_ordinary_tool_rounds():
    tool = ToolSpec("ping", "ping", {"type": "object", "properties": {}})
    effective_prefill = pc._effective_assistant_prefill(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        requested=PREFILL,
        tools=[tool],
        tool_choice="auto",
    )
    anthropic, _, _ = pc._build_anthropic_payload(
        model="claude-haiku-4-5-20251001",
        base_url="https://api.anthropic.com/v1",
        key="k",
        messages=MESSAGES,
        max_tokens=64,
        temperature=None,
        response_format=None,
        tools=[tool],
        tool_choice="auto",
        assistant_prefill=effective_prefill,
    )
    assert anthropic["messages"][-1]["role"] == "user"


def test_prefill_is_disabled_on_image_rounds():
    openrouter = _openai_chat_payload(
        "openrouter",
        "google/gemini-2.5-flash",
        PREFILL,
        allow_image_output=True,
    )

    assert openrouter["messages"][-1]["role"] == "user"


def test_tool_choice_none_can_keep_schemas_and_prefill_terminal_text():
    tool = ToolSpec("ping", "ping", {"type": "object", "properties": {}})
    effective_prefill = pc._effective_assistant_prefill(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        requested=PREFILL,
        tools=[tool],
        tool_choice="none",
    )
    payload, _, _ = pc._build_anthropic_payload(
        model="claude-haiku-4-5-20251001",
        base_url="https://api.anthropic.com/v1",
        key="k",
        messages=MESSAGES,
        max_tokens=64,
        temperature=None,
        response_format=None,
        tools=[tool],
        tool_choice="none",
        assistant_prefill=effective_prefill,
    )

    assert payload["tool_choice"] == {"type": "none"}
    assert payload["messages"][-1]["content"][0]["text"] == PREFILL


def test_gemini_tool_choice_none_does_not_claim_an_unencoded_wire_capability():
    tool = ToolSpec("ping", "ping", {"type": "object", "properties": {}})
    effective_prefill = pc._effective_assistant_prefill(
        provider="gemini",
        model="gemini-3.1-pro-preview",
        requested=PREFILL,
        tools=[tool],
        tool_choice="none",
    )
    payload, _, _ = pc._build_gemini_payload(
        model="gemini-3.1-pro-preview",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        key="k",
        messages=MESSAGES,
        max_tokens=64,
        temperature=None,
        response_format=None,
        tools=[tool],
        tool_choice="none",
        assistant_prefill=effective_prefill,
    )

    assert payload["contents"][-1]["role"] == "user"


def _parse_anthropic(
    text: str, prefill: str, stop_reason: str = "end_turn"
) -> str:
    return pc._parse_anthropic_body(
        {
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
        },
        model="claude-haiku-4-5-20251001",
        require_reply=True,
        assistant_prefill=prefill,
    )["reply"]


def _parse_gemini(text: str, prefill: str, stop_reason: str = "STOP") -> str:
    return pc._parse_gemini_body(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}]},
                    "finishReason": stop_reason,
                }
            ]
        },
        model="gemini-3.1-pro-preview",
        require_reply=True,
        assistant_prefill=prefill,
    )["reply"]


def _parse_openrouter(text: str, prefill: str, stop_reason: str = "stop") -> str:
    return pc._parse_openai_compat_body(
        _Response(
            {
                "choices": [
                    {
                        "message": {"content": text},
                        "finish_reason": stop_reason,
                    }
                ]
            }
        ),
        provider="openrouter",
        model="anthropic/claude-haiku-4.5",
        require_reply=True,
        assistant_prefill=prefill,
    )["reply"]


@pytest.mark.parametrize("parse", [_parse_anthropic, _parse_gemini, _parse_openrouter])
def test_supported_provider_reconstruction_makes_split_thinking_complete(parse):
    reply = parse("reason</think>visible", PREFILL)
    assert reply == "<think>reason</think>visible"
    assert self_thinking.split_thinking(reply) == (
        self_thinking.COMPLETE,
        "reason",
        "visible",
    )


def test_anthropic_raw_continuation_without_reconstruction_remains_absent():
    reply = _parse_anthropic("reason</think>visible", "")
    assert self_thinking.split_thinking(reply) == (
        self_thinking.ABSENT,
        "",
        "reason</think>visible",
    )


@pytest.mark.parametrize("parse", [_parse_anthropic, _parse_gemini, _parse_openrouter])
def test_provider_emitted_think_tag_is_not_doubled_by_reconstruction(parse):
    reply = parse("<think>provider reason</think>visible", PREFILL)
    assert reply.count("<think>") == 1
    assert self_thinking.split_thinking(reply) == (
        self_thinking.COMPLETE,
        "provider reason",
        "visible",
    )


def test_unsupported_provider_keeps_old_absent_distribution():
    reply = pc._reconstruct_assistant_prefill("visible", "")
    assert self_thinking.split_thinking(reply) == (
        self_thinking.ABSENT,
        "",
        "visible",
    )


def test_prefill_alone_does_not_turn_an_empty_provider_reply_into_output():
    assert pc._reconstruct_assistant_prefill("", PREFILL) == ""


@pytest.mark.parametrize("parse", [_parse_anthropic, _parse_gemini, _parse_openrouter])
def test_normal_stop_without_any_think_tag_preserves_provider_reply(parse):
    reply = parse("抱歉，这个我没法帮你。", PREFILL)
    assert reply == "抱歉，这个我没法帮你。"
    assert self_thinking.split_thinking(reply) == (
        self_thinking.ABSENT,
        "",
        reply,
    )


@pytest.mark.parametrize(
    ("parse", "stop_reason"),
    [
        (_parse_anthropic, "max_tokens"),
        (_parse_gemini, "MAX_TOKENS"),
        (_parse_openrouter, "length"),
    ],
)
def test_token_limit_without_close_tag_stays_fail_closed(parse, stop_reason):
    reply = parse("partial private reasoning", PREFILL, stop_reason)
    assert reply == "<think>partial private reasoning"
    assert self_thinking.split_thinking(reply) == (
        self_thinking.FAILED,
        "",
        "",
    )


def test_missing_stop_signal_without_close_tag_stays_fail_closed():
    reply = pc._reconstruct_assistant_prefill(
        "partial private reasoning", PREFILL, stop_reason=""
    )
    assert self_thinking.split_thinking(reply)[0] == self_thinking.FAILED


@pytest.mark.parametrize(
    ("config", "body", "payload_key", "expected_tail"),
    [
        (
            pc.ProviderConfig(
                "anthropic",
                "claude-haiku-4-5-20251001",
                "k",
                "https://api.anthropic.com/v1",
            ),
            {"content": [{"type": "text", "text": "reason</think>visible"}]},
            "messages",
            {"role": "assistant", "content": [{"type": "text", "text": PREFILL}]},
        ),
        (
            pc.ProviderConfig(
                "gemini",
                "gemini-3.1-pro-preview",
                "k",
                "https://generativelanguage.googleapis.com/v1beta",
            ),
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "reason</think>visible"}],
                        }
                    }
                ]
            },
            "contents",
            {"role": "model", "parts": [{"text": PREFILL}]},
        ),
        (
            pc.ProviderConfig(
                "openrouter",
                "anthropic/claude-haiku-4.5",
                "k",
                "https://openrouter.ai/api/v1",
            ),
            {"choices": [{"message": {"content": "reason</think>visible"}}]},
            "messages",
            {"role": "assistant", "content": PREFILL},
        ),
    ],
    ids=["anthropic", "gemini", "openrouter"],
)
def test_async_supported_provider_sends_and_reconstructs_prefill(
    monkeypatch, config, body, payload_key, expected_tail
):
    requests = []

    class _Client:
        async def post(self, _url, *, json, **_kwargs):
            requests.append(json)
            return _Response(body)

    monkeypatch.setattr(pc, "_async_http_client", lambda: _Client())
    result = asyncio.run(
        pc.chat_completion_async(
            config,
            MESSAGES,
            max_tokens=64,
            assistant_prefill=PREFILL,
        )
    )

    assert requests[0][payload_key][-1] == expected_tail
    assert result["reply"] == "<think>reason</think>visible"


@pytest.mark.parametrize(
    ("config", "body"),
    [
        (
            pc.ProviderConfig(
                "openai", "gpt-5.2", "k", "https://api.openai.com/v1"
            ),
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "visible"}],
                    }
                ],
            },
        ),
        (
            pc.ProviderConfig(
                "bedrock",
                "anthropic.claude-haiku-4-5",
                "k",
                "https://bedrock-runtime.us-east-1.amazonaws.com",
            ),
            {"output": {"message": {"content": [{"text": "visible"}]}}},
        ),
    ],
    ids=["openai-responses", "bedrock"],
)
def test_async_unsupported_wire_request_is_byte_for_byte_unchanged(
    monkeypatch, config, body
):
    requests = []

    class _Client:
        async def post(self, _url, *, json, **_kwargs):
            requests.append(json)
            return _Response(body)

    monkeypatch.setattr(pc, "_async_http_client", lambda: _Client())
    with_prefill = asyncio.run(
        pc.chat_completion_async(
            config,
            MESSAGES,
            max_tokens=64,
            assistant_prefill=PREFILL,
        )
    )
    without_prefill = asyncio.run(
        pc.chat_completion_async(config, MESSAGES, max_tokens=64)
    )

    assert requests[0] == requests[1]
    assert with_prefill["reply"] == without_prefill["reply"] == "visible"
