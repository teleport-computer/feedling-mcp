"""Thinking models that spend the whole output budget on reasoning (Bug: V2 Dream
``extraction_failed:upstream_unavailable``).

Prod evidence (2026-09): ~61 of 85 V2 Dream failures were HTTP 200 replies with
empty content and a length stop — the model spent the budget on hidden
reasoning. ``provider_client`` raised its generic "no usable reply text" shape
error, the retry wrapper re-sent the same prompt at the same budget three times
("transient"), and extraction reported ``upstream_unavailable`` without ever
reaching its truncation retry with the larger Dream budget.

These tests drive the real wire parsers through the real async transport
(``httpx.MockTransport``) with realistic provider bodies, so a severed hand-off
between the parser tag, the retry wrapper and ``extract`` turns them red. No DB.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import provider_client as pc  # noqa: E402
from model_api_runtime.v2 import extraction  # noqa: E402

_MESSAGES = [{"role": "user", "content": "consolidate these cards"}]


def _deepseek_reasoning_spent_budget(max_tokens: int) -> dict:
    # DeepSeek thinking models (deepseek-v4-pro, relays' deepseek-flash):
    # reasoning_content filled, content empty, finish_reason=length.
    return {
        "id": "chatcmpl-x",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "", "reasoning_content": "Let me think..."},
            "finish_reason": "length",
        }],
        "usage": {
            "prompt_tokens": 9000,
            "completion_tokens": max_tokens,
            "total_tokens": 9000 + max_tokens,
            "completion_tokens_details": {"reasoning_tokens": max_tokens},
        },
    }


_WIRES = {
    "deepseek": (
        pc.ProviderConfig(provider="deepseek", model="deepseek-v4-pro", api_key="k"),
        lambda: _deepseek_reasoning_spent_budget(4000),
        "length",
    ),
    "openai_compatible_null_content": (
        pc.ProviderConfig(
            provider="openai_compatible", model="deepseek-flash", api_key="k",
            base_url="https://relay.example/v1",
        ),
        lambda: {
            "choices": [{"message": {"role": "assistant", "content": None,
                                     "reasoning": "thinking"},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4000},
        },
        "length",
    ),
    "anthropic": (
        pc.ProviderConfig(provider="anthropic", model="claude-sonnet-4-5", api_key="k"),
        lambda: {
            "id": "msg_1", "type": "message", "role": "assistant",
            "content": [{"type": "thinking", "thinking": "long", "signature": "s"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 10, "output_tokens": 4000},
        },
        "max_tokens",
    ),
    "gemini": (
        pc.ProviderConfig(provider="gemini", model="gemini-2.5-pro", api_key="k"),
        lambda: {
            "candidates": [{
                "finishReason": "MAX_TOKENS",
                "content": {"role": "model", "parts": [{"thought": True, "text": "hmm"}]},
            }],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 0,
                              "thoughtsTokenCount": 4000},
        },
        "max_tokens",
    ),
    "openai_responses": (
        pc.ProviderConfig(provider="openai", model="gpt-5", api_key="k"),
        lambda: {
            "id": "resp_1", "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning", "summary": []}],
            "usage": {"input_tokens": 10, "output_tokens": 4000},
        },
        "max_output_tokens",
    ),
}


def _serve(monkeypatch, bodies):
    """Answer every provider POST with the next body (the last one repeats)."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content or b"{}"))
        body = bodies[min(len(seen) - 1, len(bodies) - 1)]
        return httpx.Response(200, json=body() if callable(body) else body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(pc, "_shared_async_client", client)
    monkeypatch.setattr(pc, "_validate_egress_url", lambda _url: None, raising=False)
    return seen


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    async def _sleep(_delay):
        return None

    monkeypatch.setattr(pc.asyncio, "sleep", _sleep)


@pytest.mark.parametrize("wire", sorted(_WIRES))
def test_empty_reply_at_output_cap_is_tagged_as_truncation_on_every_wire(monkeypatch, wire):
    config, body, stop = _WIRES[wire]
    _serve(monkeypatch, [body])

    with pytest.raises(pc.ProviderError) as raised:
        asyncio.run(pc.chat_completion_async(config, _MESSAGES, max_tokens=4000))

    exc = raised.value
    assert pc.is_output_truncation_error(exc)
    assert exc.stop_reason == stop
    assert isinstance(exc.truncation_usage, dict)
    # Everything every other caller keys on is unchanged: same class, message,
    # status and coarse classification.
    assert type(exc) is pc.ProviderError
    assert str(exc) == "provider response had no usable reply text"
    assert exc.status_code is None
    assert pc.classify_provider_error(exc) == "transient"


def test_bedrock_empty_reply_at_output_cap_is_tagged(monkeypatch):
    body = {
        "output": {"message": {"role": "assistant", "content": [
            {"reasoningContent": {"reasoningText": {"text": "thinking", "signature": "s"}}},
        ]}},
        "stopReason": "max_tokens",
        "usage": {"inputTokens": 10, "outputTokens": 4000},
    }
    with pytest.raises(pc.ProviderError) as raised:
        pc._parse_bedrock_body(body, model="anthropic.claude", require_reply=True)
    assert pc.is_output_truncation_error(raised.value)
    assert raised.value.stop_reason == "max_tokens"


@pytest.mark.parametrize("finish_reason", ["stop", "", "content_filter"])
def test_empty_reply_without_a_length_stop_is_not_truncation(monkeypatch, finish_reason):
    config = _WIRES["deepseek"][0]
    body = _deepseek_reasoning_spent_budget(100)
    body["choices"][0]["finish_reason"] = finish_reason
    _serve(monkeypatch, [body])

    with pytest.raises(pc.ProviderError) as raised:
        asyncio.run(pc.chat_completion_async(config, _MESSAGES, max_tokens=4000))
    assert not pc.is_output_truncation_error(raised.value)


def test_require_reply_false_callers_still_get_the_empty_length_success(monkeypatch):
    """V2 Chat (tool loop) calls with require_reply=False and owns its own empty
    policy: its input for an empty+length reply must be byte-for-byte the same."""
    config = _WIRES["deepseek"][0]
    _serve(monkeypatch, [lambda: _deepseek_reasoning_spent_budget(4000)])

    result = asyncio.run(pc.chat_completion_async(
        config, _MESSAGES, max_tokens=4000, require_reply=False
    ))
    assert result["reply"] == ""
    assert result["stop_reason"] == "length"


def test_default_reliable_wrapper_keeps_retrying_empty_length_replies(monkeypatch):
    """Every caller that does not opt out keeps its retry count and labels."""
    config = _WIRES["deepseek"][0]
    seen = _serve(monkeypatch, [lambda: _deepseek_reasoning_spent_budget(4000)])

    with pytest.raises(pc.ProviderError) as raised:
        asyncio.run(pc.reliable_chat_completion_async(
            config, _MESSAGES, max_tokens=4000, base_delay_sec=0.0,
        ))
    assert len(seen) == 3
    assert raised.value.feedling_error_class == "transient_exhausted"


def test_opted_out_reliable_wrapper_does_not_resend_a_truncated_reply(monkeypatch):
    config = _WIRES["deepseek"][0]
    seen = _serve(monkeypatch, [lambda: _deepseek_reasoning_spent_budget(4000)])

    with pytest.raises(pc.ProviderError) as raised:
        asyncio.run(pc.reliable_chat_completion_async(
            config, _MESSAGES, max_tokens=4000, base_delay_sec=0.0,
            retry_output_truncation=False,
        ))
    assert len(seen) == 1
    assert raised.value.feedling_error_class == "output_truncated"


def test_opted_out_wrapper_still_retries_real_transient_failures(monkeypatch):
    config = _WIRES["deepseek"][0]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "overloaded"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        })

    monkeypatch.setattr(
        pc, "_shared_async_client", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    result = asyncio.run(pc.reliable_chat_completion_async(
        config, _MESSAGES, max_tokens=4000, base_delay_sec=0.0,
        retry_output_truncation=False,
    ))
    assert result["reply"] == "ok"
    assert len(calls) == 2


def _dream_parse(raw):
    data = json.loads(raw)
    return data["consolidations"], [], None


def _valid_dream_reply() -> dict:
    return {
        "choices": [{"message": {"content": '{"consolidations": []}'},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 9000, "completion_tokens": 30},
    }


def _dream_parse_retry():
    return extraction.ParseRetry(
        should_retry=lambda _err: False,
        build_prompt=lambda prompt, _err: prompt,
        parse=_dream_parse,
        build_truncation_prompt=lambda prompt: prompt + "\n(be concise)",
    )


def test_extract_routes_reasoning_spent_budget_into_the_escalated_truncation_retry(
    monkeypatch,
):
    config = _WIRES["deepseek"][0]
    budget = extraction.max_output_tokens_for_lane("dream")
    retry_budget = extraction.truncation_retry_max_output_tokens_for_lane("dream")
    seen = _serve(
        monkeypatch,
        [lambda: _deepseek_reasoning_spent_budget(budget), _valid_dream_reply],
    )
    usage, events = [], []

    async def _trajectory(kind, payload):
        events.append((kind, payload))

    items, reason = asyncio.run(extraction.extract(
        provider_config=config,
        prompt="P",
        parse=_dream_parse,
        parse_retry=_dream_parse_retry(),
        max_tokens=budget,
        truncation_retry_max_tokens=retry_budget,
        usage_out=usage.append,
        trajectory_out=_trajectory,
    ))

    assert (items, reason) == ([], None)
    # One call at the Dream budget, one truncation retry at the doubled budget —
    # not three identical re-sends reported as upstream_unavailable.
    assert [row["max_tokens"] for row in seen] == [budget, retry_budget]
    assert seen[1]["messages"][-1]["content"].endswith("(be concise)")
    assert usage[0]["completion_tokens"] == budget
    kinds = [kind for kind, _payload in events]
    assert "provider_error" not in kinds
    assert "extraction_output_truncated" in kinds


def test_extract_reports_output_truncated_when_the_retry_also_spends_its_budget(
    monkeypatch,
):
    config = _WIRES["deepseek"][0]
    budget = extraction.max_output_tokens_for_lane("dream")
    retry_budget = extraction.truncation_retry_max_output_tokens_for_lane("dream")
    seen = _serve(
        monkeypatch,
        [
            lambda: _deepseek_reasoning_spent_budget(budget),
            lambda: _deepseek_reasoning_spent_budget(retry_budget),
        ],
    )
    failure = {}

    items, reason = asyncio.run(extraction.extract(
        provider_config=config,
        prompt="P",
        parse=_dream_parse,
        parse_retry=_dream_parse_retry(),
        max_tokens=budget,
        truncation_retry_max_tokens=retry_budget,
        failure_detail_out=failure.update,
    ))

    assert (items, reason) == (None, "output_truncated")
    assert len(seen) == 2
    assert failure == {
        "stop_reason": "length",
        "completion_tokens": retry_budget,
        "max_tokens": retry_budget,
    }


def test_extract_still_reports_a_real_empty_reply_as_upstream_unavailable(monkeypatch):
    """A 200 with nothing in it and no length stop is still a relay blip."""
    config = _WIRES["deepseek"][0]
    body = _deepseek_reasoning_spent_budget(10)
    body["choices"][0]["finish_reason"] = "stop"
    seen = _serve(monkeypatch, [body])

    items, reason = asyncio.run(extraction.extract(
        provider_config=config, prompt="P", parse=_dream_parse,
        parse_retry=_dream_parse_retry(), max_tokens=4000,
    ))
    assert (items, reason) == (None, "provider_call_failed:upstream_unavailable")
    assert len(seen) == 3


def test_extract_treats_anthropic_max_tokens_on_a_partial_reply_as_truncation(monkeypatch):
    """Non-empty cut-off replies on non-OpenAI wires used to reach the JSON
    parser as a format error; every wire's cap marker now means truncation."""
    config = _WIRES["anthropic"][0]
    seen = _serve(monkeypatch, [
        {"content": [{"type": "text", "text": '{"consolidations": [{"op'}],
         "stop_reason": "max_tokens", "usage": {"output_tokens": 4000}},
        {"content": [{"type": "text", "text": '{"consolidations": []}'}],
         "stop_reason": "end_turn", "usage": {"output_tokens": 20}},
    ])

    items, reason = asyncio.run(extraction.extract(
        provider_config=config, prompt="P", parse=_dream_parse,
        parse_retry=_dream_parse_retry(), max_tokens=4000,
        truncation_retry_max_tokens=8000,
    ))
    assert (items, reason) == ([], None)
    assert [row["max_tokens"] for row in seen] == [4000, 8000]
