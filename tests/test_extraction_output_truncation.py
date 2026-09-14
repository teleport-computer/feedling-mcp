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


# --------------------------------------------------------------------------- #
# Heavy-pool watchdog: slow wires and compatibility fallbacks inside one attempt
# --------------------------------------------------------------------------- #

def test_slow_compatibility_fallback_wires_never_starve_the_heavy_pool_stall_clock(
    monkeypatch,
):
    """Dream runs in a Heavy-pool slot whose stall budget (120s) is shorter than
    two extraction wires (2 x 90s). One reliable attempt can hold several wires
    (a compatibility fallback re-sends without ``temperature``), and the retry
    wrapper used to report progress only around the whole attempt, so a slow but
    healthy request was killed and requeued (duplicate spend).

    Fault injection: every wire takes just under the 90s wire timeout on a fake
    clock; the first wire of each attempt is a compatibility 400. The real
    transport, parser, retry wrapper and ``extract`` run; the stall ages the
    watchdog would observe are computed from the reported progress timestamps
    and fed to the real ``should_kill`` with the real Heavy-pool budgets.
    """
    from model_api_runtime.v2 import pool_config, watchdog

    clock = {"now": 0.0}
    progress: list[tuple[float, str]] = []
    wires: list[dict] = []
    budget = extraction.max_output_tokens_for_lane("dream")
    retry_budget = extraction.truncation_retry_max_output_tokens_for_lane("dream")
    answers = [
        (400, {"error": {"message": "`temperature` is deprecated for this model."}}),
        (200, _deepseek_reasoning_spent_budget(budget)),
        (400, {"error": {"message": "`temperature` is deprecated for this model."}}),
        (200, _valid_dream_reply()),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        wires.append(json.loads(request.content or b"{}"))
        clock["now"] += extraction._TIMEOUT_SEC - 1.0  # slow, but inside the wire timeout
        status, body = answers[len(wires) - 1]
        return httpx.Response(status, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(pc, "_shared_async_client", client)
    monkeypatch.setattr(pc, "_validate_egress_url", lambda _url: None, raising=False)

    items, reason = asyncio.run(extraction.extract(
        provider_config=_WIRES["deepseek"][0],
        prompt="P",
        parse=_dream_parse,
        parse_retry=_dream_parse_retry(),
        max_tokens=budget,
        truncation_retry_max_tokens=retry_budget,
        progress_cb=lambda stage, attempt: progress.append((clock["now"], stage)),
    ))
    finished_at = clock["now"]

    assert (items, reason) == ([], None)
    assert len(wires) == 4
    assert "temperature" in wires[0] and "temperature" not in wires[1]
    assert [stage for _at, stage in progress].count("wire_start") == 4

    heavy_dream_slots = [
        slot for slot in pool_config.RuntimePoolConfig.from_env().slots
        if "dream" in slot.lanes
    ]
    assert heavy_dream_slots and {slot.pool for slot in heavy_dream_slots} == {"heavy"}
    boundaries = [0.0, *(at for at, _stage in progress), finished_at]
    longest_silence = max(b - a for a, b in zip(boundaries, boundaries[1:]))
    for slot in heavy_dream_slots:
        assert not watchdog.should_kill(
            {
                "alive": True,
                "event_loop_heartbeat_age_sec": 1.0,
                "last_slot_progress_age_sec": 1.0,
                "active_turn_count": 1,
                "current_turn_age_sec": finished_at,
                "current_turn_stall_age_sec": longest_silence,
            },
            child_liveness_timeout_sec=45.0,
            jobs_claimable=True,
            turn_stall_timeout_sec=slot.stall_budget_sec,
            turn_absolute_timeout_sec=slot.absolute_budget_sec,
        ), (slot.slot_id, longest_silence)


# --------------------------------------------------------------------------- #
# Escalated truncation retry rejected as "max_tokens too large"
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "status, message",
    [
        (400, "max_tokens is too large: 24000. This model supports at most 16384 completion tokens"),
        (400, "max_tokens: 24000 > 16000, which is the maximum allowed number of output tokens"),
        (400, "Invalid max_tokens value, the valid range of max_tokens is [1, 8192]"),
        (400, "The maximum tokens you requested exceeds the model limit of 8192"),
        (422, "maxOutputTokens must be less than or equal to 8192"),
    ],
)
def test_output_budget_rejection_shapes_are_recognized(status, message):
    exc = pc.ProviderError(
        f"provider_http_{status}: {message}", status_code=status,
        raw_response_body=json.dumps({"error": {"message": message}}),
    )
    assert pc.is_output_budget_rejection(exc) is True


@pytest.mark.parametrize(
    "status, message",
    [
        (400, "`temperature` is deprecated for this model."),
        (400, "messages: at most 100 messages are allowed"),
        (401, "max_tokens is too large"),  # not a request-shape rejection
        (429, "output tokens per minute limit exceeded"),
        (400, "prompt is too long: 250000 tokens > 200000 maximum"),
        (400, "Unsupported parameter: 'max_tokens' is not supported with this model. "
              "Use 'max_completion_tokens' instead."),
    ],
)
def test_other_provider_errors_are_not_output_budget_rejections(status, message):
    exc = pc.ProviderError(
        f"provider_http_{status}: {message}", status_code=status,
        raw_response_body=json.dumps({"error": {"message": message}}),
    )
    assert pc.is_output_budget_rejection(exc) is False


def _serve_by_budget(monkeypatch, *, accepted_max, first, fallback):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        seen.append(payload)
        if payload["max_tokens"] > accepted_max:
            return httpx.Response(400, json={"error": {"message": (
                f"max_tokens is too large: {payload['max_tokens']}. This model "
                f"supports at most {accepted_max} completion tokens."
            )}})
        return httpx.Response(200, json=first() if len(seen) == 1 else fallback())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(pc, "_shared_async_client", client)
    monkeypatch.setattr(pc, "_validate_egress_url", lambda _url: None, raising=False)
    return seen


def test_rejected_escalated_budget_falls_back_to_the_accepted_budget(monkeypatch):
    budget = extraction.max_output_tokens_for_lane("dream")
    retry_budget = extraction.truncation_retry_max_output_tokens_for_lane("dream")
    seen = _serve_by_budget(
        monkeypatch, accepted_max=budget,
        first=lambda: _deepseek_reasoning_spent_budget(budget),
        fallback=_valid_dream_reply,
    )
    events = []

    async def _trajectory(kind, payload):
        events.append((kind, payload))

    items, reason = asyncio.run(extraction.extract(
        provider_config=_WIRES["deepseek"][0],
        prompt="P",
        parse=_dream_parse,
        parse_retry=_dream_parse_retry(),
        max_tokens=budget,
        truncation_retry_max_tokens=retry_budget,
        trajectory_out=_trajectory,
    ))

    assert (items, reason) == ([], None)
    assert [row["max_tokens"] for row in seen] == [budget, retry_budget, budget]
    assert seen[1]["messages"] == seen[2]["messages"]
    assert seen[2]["messages"][-1]["content"].endswith("(be concise)")
    assert ("extraction_output_budget_fallback",
            {"rejected_max_tokens": retry_budget, "max_tokens": budget}) in events


def test_rejected_escalated_budget_whose_fallback_truncates_is_output_truncated(monkeypatch):
    budget = extraction.max_output_tokens_for_lane("dream")
    seen = _serve_by_budget(
        monkeypatch, accepted_max=budget,
        first=lambda: _deepseek_reasoning_spent_budget(budget),
        fallback=lambda: _deepseek_reasoning_spent_budget(budget),
    )

    items, reason = asyncio.run(extraction.extract(
        provider_config=_WIRES["deepseek"][0],
        prompt="P",
        parse=_dream_parse,
        parse_retry=_dream_parse_retry(),
        max_tokens=budget,
        truncation_retry_max_tokens=extraction.truncation_retry_max_output_tokens_for_lane("dream"),
    ))

    assert (items, reason) == (None, "output_truncated")
    assert len(seen) == 3


def test_a_first_call_budget_rejection_is_still_a_provider_config_failure(monkeypatch):
    """The fallback only exists for the escalated retry: rejecting the lane's own
    budget is a real configuration problem, not something to paper over."""
    budget = extraction.max_output_tokens_for_lane("dream")
    seen = _serve_by_budget(
        monkeypatch, accepted_max=budget - 1,
        first=_valid_dream_reply, fallback=_valid_dream_reply,
    )

    items, reason = asyncio.run(extraction.extract(
        provider_config=_WIRES["deepseek"][0],
        prompt="P",
        parse=_dream_parse,
        parse_retry=_dream_parse_retry(),
        max_tokens=budget,
        truncation_retry_max_tokens=extraction.truncation_retry_max_output_tokens_for_lane("dream"),
    ))

    assert items is None
    assert reason == "provider_call_failed:provider_config"
    assert [row["max_tokens"] for row in seen] == [budget]
