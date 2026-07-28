import pathlib
import sys
import asyncio

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import prompt_frontier as frontier
from model_api_runtime.v2 import tool_loop
import provider_client
from provider_types import ToolResult, ToolSpec


def _model_limit(context_window_tokens: int = 2_048) -> frontier.ModelPromptLimit:
    return frontier.ModelPromptLimit(
        provider="test",
        model="test-model",
        context_window_tokens=context_window_tokens,
        source="deployment_override",
        override_key="test:test-model",
    )


def test_known_model_uses_audited_family_lower_bound():
    resolved = frontier.resolve_model_limit("open-ai", "GPT-4O-mini")

    assert resolved.provider == "openai"
    assert resolved.model == "gpt-4o-mini"
    assert resolved.context_window_tokens == 128_000
    assert resolved.source == "audited_family"
    assert resolved.family == "openai_modern"


def test_openrouter_known_family_is_resolved_separately():
    resolved = frontier.resolve_model_limit(
        "openrouter",
        "anthropic/claude-sonnet-4.5",
        base_url="https://openrouter.ai/api/v1/",
    )

    assert resolved.context_window_tokens == 128_000
    assert resolved.source == "audited_family"
    assert resolved.family == "openrouter_modern"


def test_bedrock_anthropic_family_uses_native_default_endpoint():
    resolved = frontier.resolve_model_limit(
        "aws-bedrock",
        "us.anthropic.claude-sonnet-4-6",
    )

    assert resolved.provider == "bedrock"
    assert resolved.context_window_tokens == 128_000
    assert resolved.source == "audited_family"
    assert resolved.family == "bedrock_anthropic_modern"


def test_deployment_override_wins_over_audited_family():
    resolved = frontier.resolve_model_limit(
        "openai",
        "gpt-4o-mini",
        deployment_overrides={"openai:gpt-4o-mini": 131_072},
    )

    assert resolved.context_window_tokens == 131_072
    assert resolved.source == "deployment_override"
    assert resolved.override_key == "openai:gpt-4o-mini"
    assert resolved.family is None


def test_deployment_override_precedence_is_most_specific_first():
    overrides = {
        "openai:gpt-4o-mini": 131_072,
        "openai:*": 65_536,
        "*:gpt-4o-mini": 16_384,
        "*:*": 8_192,
    }

    assert (
        frontier.resolve_model_limit(
            "openai", "gpt-4o-mini", deployment_overrides=overrides
        ).context_window_tokens
        == 131_072
    )
    assert (
        frontier.resolve_model_limit(
            "openai", "unlisted", deployment_overrides=overrides
        ).context_window_tokens
        == 65_536
    )
    assert (
        frontier.resolve_model_limit(
            "some-relay", "gpt-4o-mini", deployment_overrides=overrides
        ).context_window_tokens
        == 16_384
    )
    assert (
        frontier.resolve_model_limit(
            "some-relay", "unlisted", deployment_overrides=overrides
        ).context_window_tokens
        == 8_192
    )


def test_unaudited_route_gets_conservative_default(monkeypatch):
    """A custom relay / unknown model with nothing more specific configured now
    resolves to a conservative default instead of a hard rejection, so custom
    relays are usable without every client sending context_window_tokens."""
    monkeypatch.delenv(frontier._UNAUDITED_DEFAULT_ENV, raising=False)
    resolved = frontier.resolve_model_limit("some-relay", "mystery-model")
    assert resolved.source == "unaudited_default"
    assert resolved.context_window_tokens == 65536
    # A first-party provider NAME pointed at a custom base_url is still unaudited.
    custom = frontier.resolve_model_limit(
        "openai", "gpt-4o-mini", base_url="https://llm-proxy.example/v1"
    )
    assert custom.source == "unaudited_default"
    assert custom.context_window_tokens == 65536


def test_raised_default_provides_target_58163_input_budget(monkeypatch):
    """The 64K default reserves 4K output + 5% safety and leaves 58,163 input."""
    monkeypatch.delenv(frontier._UNAUDITED_DEFAULT_ENV, raising=False)
    persona = frontier.PromptComponent(
        name="target_history", estimated_tokens=58_163, required=True
    )
    limit = frontier.resolve_model_limit("some-relay", "mystery-model")
    plan = frontier.plan_prompt(
        model_limit=limit,
        components=[persona],
        output_reserve_tokens=4_096,
    )
    assert "target_history" in plan.included_components
    assert plan.budget.safety_margin_tokens == 3_277
    assert plan.budget.input_budget_tokens == 58_163

    # The old 32K default (opt back in via env) rejects the same required set.
    monkeypatch.setenv(frontier._UNAUDITED_DEFAULT_ENV, "32768")
    old_limit = frontier.resolve_model_limit("some-relay", "mystery-model")
    with pytest.raises(frontier.PromptFrontierExhausted):
        frontier.plan_prompt(
            model_limit=old_limit,
            components=[persona],
            output_reserve_tokens=4_096,
        )


def test_unaudited_default_is_env_tunable_and_zero_restores_fail_closed(monkeypatch):
    monkeypatch.setenv(frontier._UNAUDITED_DEFAULT_ENV, "16384")
    assert (
        frontier.resolve_model_limit("some-relay", "m").context_window_tokens == 16384
    )
    # 0 opts back into strict fail-closed rejection.
    monkeypatch.setenv(frontier._UNAUDITED_DEFAULT_ENV, "0")
    with pytest.raises(
        frontier.PromptContextLimitUnconfigured,
        match="^prompt_context_limit_unconfigured$",
    ) as unknown:
        frontier.resolve_model_limit("some-relay", "mystery-model")
    assert unknown.value.provider == "some_relay"


def test_unaudited_default_is_lowest_precedence(monkeypatch):
    """Override > caller-supplied > audited family all still win over the default,
    so enabling the default never masks a value someone stated explicitly."""
    monkeypatch.delenv(frontier._UNAUDITED_DEFAULT_ENV, raising=False)
    supplied = frontier.resolve_model_limit(
        "some-relay", "m", provider_context_window_tokens=4096
    )
    assert supplied.source == "provider_metadata"
    assert supplied.context_window_tokens == 4096
    override = frontier.resolve_model_limit(
        "some-relay", "m", deployment_overrides={"*:*": 12000}
    )
    assert override.source == "deployment_override"
    audited = frontier.resolve_model_limit("open-ai", "gpt-4o-mini")
    assert audited.source == "audited_family"


def test_unknown_route_override_is_explicit_and_applies_before_rejection():
    resolved = frontier.resolve_model_limit(
        "custom",
        "private-8k",
        base_url="https://relay.example/v1",
        deployment_overrides={"openai_compatible:private-8k": 8_192},
    )

    assert resolved.context_window_tokens == 8_192
    assert resolved.source == "deployment_override"
    assert resolved.override_key == "openai_compatible:private-8k"


def test_deployment_override_json_is_normalized_and_invalid_config_fails_closed():
    parsed = frontier.parse_deployment_overrides(
        '{"Open-AI:GPT-4O-MINI": 65536, "*:*": 4096}'
    )
    assert parsed == {"openai:gpt-4o-mini": 65_536, "*:*": 4_096}

    with pytest.raises(ValueError):
        frontier.parse_deployment_overrides("[]")
    with pytest.raises(ValueError):
        frontier.parse_deployment_overrides('{"openai:*": 1024}')
    with pytest.raises(ValueError, match="duplicate normalized"):
        frontier.parse_deployment_overrides(
            '{"open-ai:gpt-4o": 32768, "open_ai:GPT-4O": 65536}'
        )


def test_utf8_and_structural_estimation_is_conservative_and_deterministic():
    assert frontier.estimate_utf8_tokens("a") == 1
    assert frontier.estimate_utf8_tokens("é") == 2
    assert frontier.estimate_utf8_tokens("中") == 3

    first = {"b": 1, "a": "中"}
    second = {"a": "中", "b": 1}
    assert frontier.canonical_json(first) == '{"a":"中","b":1}'
    assert frontier.estimate_structured_tokens(
        first, structural_overhead_tokens=7
    ) == frontier.estimate_structured_tokens(second, structural_overhead_tokens=7)
    assert (
        frontier.estimate_structured_tokens(first, structural_overhead_tokens=7)
        == len(frontier.canonical_json(first).encode("utf-8")) + 7
    )


def test_messages_include_per_message_structural_overhead():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hello"},
    ]
    component = frontier.messages_component(messages)

    assert component.name == "messages"
    assert component.required is True
    assert component.estimated_tokens == (
        len(frontier.canonical_json(messages).encode("utf-8"))
        + 2 * frontier.MESSAGE_STRUCTURAL_OVERHEAD_TOKENS
    )


def test_complete_tool_schema_catalog_is_one_atomic_component():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "memory_search",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                },
            },
        },
    ]

    component = frontier.tool_schemas_component(tools)

    assert component == frontier.PromptComponent(
        name="tool_schemas",
        estimated_tokens=(
            len(frontier.canonical_json(tools).encode("utf-8"))
            + len(tools) * frontier.TOOL_SCHEMA_STRUCTURAL_OVERHEAD_TOKENS
        ),
        required=True,
        priority=0,
    )


def test_output_and_safety_reserves_are_explicit():
    default_budget = frontier.build_prompt_budget(32_768)
    assert default_budget.output_reserve_tokens == 4_096
    assert default_budget.safety_margin_tokens == 1_639
    assert default_budget.input_budget_tokens == 27_033

    custom_budget = frontier.build_prompt_budget(
        4_096,
        output_reserve_tokens=768,
        safety_margin_tokens=256,
    )
    assert custom_budget.input_budget_tokens == 3_072
    assert (
        custom_budget.input_budget_tokens
        + custom_budget.output_reserve_tokens
        + custom_budget.safety_margin_tokens
        == custom_budget.context_window_tokens
    )


def test_required_components_exact_fit_and_one_over_boundary():
    exact = frontier.PromptComponent("required_history", 1_792)
    plan = frontier.plan_prompt(
        model_limit=_model_limit(),
        components=[exact],
        output_reserve_tokens=128,
        safety_margin_tokens=128,
    )

    assert plan.status == "fits"
    assert plan.estimated_input_tokens == 1_792
    assert plan.remaining_input_tokens == 0
    assert plan.reserved_total_tokens == 2_048
    assert plan.included_components == ("required_history",)

    with pytest.raises(frontier.PromptFrontierExhausted) as caught:
        frontier.plan_prompt(
            model_limit=_model_limit(),
            components=[frontier.PromptComponent("required_history", 1_793)],
            output_reserve_tokens=128,
            safety_margin_tokens=128,
        )

    error = caught.value
    assert error.code == "prompt_frontier_exhausted"
    assert error.required_tokens == 1_793
    assert error.input_budget_tokens == 1_792
    assert error.required_components == ("required_history",)


def test_exhaustion_error_never_contains_prompt_content():
    secret = "PRIVATE CONVERSATION CONTENT"
    component = frontier.text_component("conversation_history", secret * 100)

    with pytest.raises(frontier.PromptFrontierExhausted) as caught:
        frontier.plan_prompt(
            model_limit=_model_limit(),
            components=[component],
            output_reserve_tokens=128,
            safety_margin_tokens=128,
        )

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_required_and_optional_components_have_explicit_deterministic_decisions():
    components = [
        frontier.PromptComponent("low_priority", 30, required=False, priority=1),
        frontier.PromptComponent("required", 1_700),
        frontier.PromptComponent("high_priority", 80, required=False, priority=10),
    ]

    plan = frontier.plan_prompt(
        model_limit=_model_limit(),
        components=components,
        output_reserve_tokens=128,
        safety_margin_tokens=128,
    )

    assert plan.status == "fits_optional_omitted"
    assert plan.estimated_input_tokens == 1_780
    assert plan.remaining_input_tokens == 12
    assert plan.included_components == ("required", "high_priority")
    assert plan.omitted_optional_components == ("low_priority",)
    assert [decision.name for decision in plan.decisions] == [
        "low_priority",
        "required",
        "high_priority",
    ]
    assert [decision.reason for decision in plan.decisions] == [
        "optional_over_budget",
        "required",
        "optional_fit",
    ]
    assert plan.reserved_total_tokens <= plan.budget.context_window_tokens


def test_tool_transcript_is_indivisible_instead_of_partially_admitted():
    exchanges = [
        {
            "assistant": {
                "tool_calls": [{"id": "call_1", "name": "web_fetch", "arguments": {}}]
            },
            "tool_results": [{"tool_call_id": "call_1", "content": "x" * 750}],
        },
        {
            "assistant": {
                "tool_calls": [
                    {"id": "call_2", "name": "memory_search", "arguments": {}}
                ]
            },
            "tool_results": [{"tool_call_id": "call_2", "content": "y" * 750}],
        },
    ]
    complete = frontier.tool_transcript_component(exchanges)
    first_only = frontier.tool_transcript_component(exchanges[:1])
    budget = frontier.build_prompt_budget(
        2_048,
        output_reserve_tokens=128,
        safety_margin_tokens=128,
    )

    assert first_only.estimated_tokens <= budget.input_budget_tokens
    assert complete.estimated_tokens > budget.input_budget_tokens
    with pytest.raises(frontier.PromptFrontierExhausted) as caught:
        frontier.plan_prompt(
            model_limit=_model_limit(),
            components=[complete],
            output_reserve_tokens=128,
            safety_margin_tokens=128,
        )
    assert caught.value.required_components == ("tool_transcript",)


def test_optional_tool_catalog_is_omitted_whole_and_reported():
    tools = [
        {
            "type": "function",
            "function": {"name": "large_tool", "description": "z" * 2_000},
        }
    ]
    plan = frontier.plan_prompt(
        model_limit=_model_limit(),
        components=[
            frontier.PromptComponent("messages", 100),
            frontier.tool_schemas_component(tools, required=False),
        ],
        output_reserve_tokens=128,
        safety_margin_tokens=128,
    )

    assert plan.included_components == ("messages",)
    assert plan.omitted_optional_components == ("tool_schemas",)
    assert plan.status == "fits_optional_omitted"


def test_duplicate_component_names_fail_instead_of_producing_ambiguous_metrics():
    with pytest.raises(ValueError, match="must be unique"):
        frontier.plan_prompt(
            model_limit=_model_limit(),
            components=[
                frontier.PromptComponent("messages", 10),
                frontier.PromptComponent("messages", 20),
            ],
            output_reserve_tokens=128,
            safety_margin_tokens=128,
        )


def test_provider_metadata_and_calibrated_estimator_are_supported():
    config = provider_client.ProviderConfig(
        "custom",
        "private-model",
        "key",
        "https://relay.example/v1",
        context_window_tokens=65_536,
    )
    resolved = frontier.resolve_model_limit_from_config(config)

    assert resolved.context_window_tokens == 65_536
    assert resolved.source == "provider_metadata"
    assert frontier.estimate_utf8_tokens("abcdefgh", utf8_bytes_per_token=4) == 2


def test_image_payload_uses_fixed_reserve_instead_of_base64_text_size():
    tiny = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                }
            ],
        }
    ]
    huge = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + "A" * 500_000},
                }
            ],
        }
    ]

    assert frontier.messages_component(tiny).estimated_tokens == (
        frontier.messages_component(huge).estimated_tokens
    )


def _run_loop(*, monkeypatch, provider, fold, build_messages, **kwargs):
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    async def dispatch(calls):
        return [ToolResult(call_id=call.id, content="x" * 65_000) for call in calls]

    async def on_reply(_text, *, final, reasoning=""):
        assert final is True

    return asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=kwargs.pop("provider_config"),
            build_messages=build_messages,
            dispatch_tools=dispatch,
            on_reply=on_reply,
            fold_new_messages=fold,
            add_usage=lambda _usage: None,
            max_calls=kwargs.pop("max_calls", 5),
            **kwargs,
        )
    )


def test_tool_loop_rejects_unconfigured_custom_limit_before_provider_io(monkeypatch):
    # With the unaudited default disabled (fail-closed mode), an unconfigured
    # custom route is still rejected BEFORE any provider I/O. (The default-on
    # behaviour — a conservative window instead of rejection — is covered by the
    # resolve_model_limit unit tests above.)
    monkeypatch.setenv(frontier._UNAUDITED_DEFAULT_ENV, "0")
    calls = []

    async def provider(_config, _messages, *, tools=None):
        calls.append(tools)
        return {"reply": "not reached", "tool_calls": [], "usage": {}}

    async def fold():
        return []

    with pytest.raises(
        frontier.PromptContextLimitUnconfigured,
        match="^prompt_context_limit_unconfigured$",
    ):
        _run_loop(
            monkeypatch=monkeypatch,
            provider=provider,
            fold=fold,
            build_messages=lambda transcript: [
                {"role": "user", "content": "hello"},
                *transcript,
            ],
            provider_config=provider_client.ProviderConfig(
                "custom",
                "unknown-model",
                "key",
                "https://relay.example/v1",
            ),
        )

    assert calls == []


def test_round_frontier_stops_aggregate_native_transcript_before_provider_call(
    monkeypatch,
):
    calls = []
    scripted = [
        {
            "reply": "",
            "tool_calls": [
                {
                    "id": f"c{index}",
                    "name": "memory_search",
                    "args": {"query": "q"},
                }
            ],
            "usage": {},
        }
        for index in range(1, 5)
    ]

    async def provider(_config, messages, *, tools=None):
        calls.append((list(messages), tools))
        return scripted.pop(0)

    async def fold():
        return []

    with pytest.raises(frontier.PromptFrontierExhausted) as caught:
        _run_loop(
            monkeypatch=monkeypatch,
            provider=provider,
            fold=fold,
            build_messages=lambda transcript: [
                {"role": "user", "content": "start"},
                *transcript,
            ],
            provider_config=provider_client.ProviderConfig(
                "custom",
                "test-150k",
                "key",
                context_window_tokens=150_000,
            ),
            fold_before_first=True,
            tool_result_char_cap=65_000,
            tool_batch_result_char_cap=65_000,
            prompt_output_reserve_tokens=4_096,
            prompt_safety_margin_tokens=0,
        )

    # Every 65K exchange is valid alone; only their aggregate crosses the
    # fourth round's single total frontier, which is checked pre-request.
    assert len(calls) == 3
    assert (
        len(
            [
                message
                for message in calls[-1][0]
                if message.__class__.__name__ == "ToolExchange"
            ]
        )
        == 2
    )
    assert "tool_transcript" in caught.value.required_components


def test_late_input_is_in_required_frontier_before_first_provider_call(monkeypatch):
    calls = []

    async def provider(_config, _messages, *, tools=None):
        calls.append(tools)
        return {"reply": "not reached", "tool_calls": [], "usage": {}}

    async def fold():
        return [{"role": "user", "content": "late" * 4_000}]

    with pytest.raises(frontier.PromptFrontierExhausted) as caught:
        _run_loop(
            monkeypatch=monkeypatch,
            provider=provider,
            fold=fold,
            build_messages=lambda transcript: [
                {"role": "system", "content": "stable prefix"},
                *transcript,
            ],
            provider_config=provider_client.ProviderConfig(
                "custom",
                "test-8k",
                "key",
                context_window_tokens=8_192,
            ),
            fold_before_first=True,
            prompt_output_reserve_tokens=1_024,
            prompt_safety_margin_tokens=0,
        )

    assert calls == []
    assert caught.value.required_components == ("message_context",)


def test_tool_catalog_is_counted_and_omitted_whole_when_only_it_does_not_fit(
    monkeypatch,
):
    calls = []

    async def provider(_config, messages, *, tools=None):
        calls.append((list(messages), tools))
        return {"reply": "text-only", "tool_calls": [], "usage": {}}

    async def fold():
        return []

    outcome = _run_loop(
        monkeypatch=monkeypatch,
        provider=provider,
        fold=fold,
        build_messages=lambda transcript: [
            {"role": "system", "content": "stable prefix"},
            *transcript,
        ],
        provider_config=provider_client.ProviderConfig(
            "custom",
            "test-20k",
            "key",
            context_window_tokens=20_000,
        ),
        extra_tool_specs=[
            ToolSpec(
                "large_read",
                "z" * 20_000,
                {"type": "object", "properties": {}},
            )
        ],
        prompt_output_reserve_tokens=2_000,
        prompt_safety_margin_tokens=0,
    )

    assert outcome.final_text == "text-only"
    assert len(calls) == 1
    assert calls[0][1] is None
    assert calls[0][0] == [{"role": "system", "content": "stable prefix"}]


def test_adaptive_round_reports_tail_window_to_metrics_and_trajectory(monkeypatch):
    calls = []
    metric_windows = []
    trajectory = []

    class AdaptiveBuilder:
        def __call__(self, transcript):
            return [{"role": "user", "content": "fallback"}, *transcript]

        def plan_provider_round(self, **kwargs):
            messages = [{"role": "user", "content": "adaptive"}]
            plan = frontier.plan_provider_round(
                model_limit=kwargs["model_limit"],
                messages=messages,
                tools=kwargs["tools"],
                output_reserve_tokens=kwargs["output_reserve_tokens"],
                safety_margin_tokens=kwargs["safety_margin_tokens"],
                utf8_bytes_per_token=kwargs["utf8_bytes_per_token"],
                image_reserve_tokens=kwargs["image_reserve_tokens"],
            )
            return messages, plan, {
                "lane": "chat",
                "target_turns": 40,
                "available_turns": 40,
                "effective_turns": 23,
                "fallback": True,
                "source_truncated": False,
            }

    async def provider(_config, messages, *, tools=None):
        calls.append((messages, tools))
        return {"reply": "ok", "tool_calls": [], "usage": {}}

    async def fold():
        return []

    async def record(kind, payload):
        trajectory.append((kind, payload))

    outcome = _run_loop(
        monkeypatch=monkeypatch,
        provider=provider,
        fold=fold,
        build_messages=AdaptiveBuilder(),
        provider_config=provider_client.ProviderConfig(
            "custom",
            "adaptive",
            "key",
            context_window_tokens=32_768,
        ),
        on_tail_window=lambda item: metric_windows.append(item),
        on_trajectory_event=record,
    )

    assert outcome.final_text == "ok"
    assert calls[0][0] == [{"role": "user", "content": "adaptive"}]
    assert metric_windows == [{
        "lane": "chat",
        "target_turns": 40,
        "available_turns": 40,
        "effective_turns": 23,
        "fallback": True,
        "source_truncated": False,
    }]
    request = next(payload for kind, payload in trajectory if kind == "provider_request")
    assert request["tail_window"] == metric_windows[0]


def test_adaptive_required_exhaustion_is_counted_and_still_raised(monkeypatch):
    counted = []
    provider_calls = []

    class ExhaustedBuilder:
        def __call__(self, transcript):
            return []

        def plan_provider_round(self, **kwargs):
            raise frontier.PromptFrontierExhausted(
                required_tokens=10_000,
                input_budget_tokens=8_000,
                context_window_tokens=12_000,
                required_components=("message_context",),
                limit_source="provider_metadata",
            )

    async def provider(_config, messages, *, tools=None):
        provider_calls.append(messages)
        return {"reply": "not reached", "tool_calls": [], "usage": {}}

    async def fold():
        return []

    with pytest.raises(frontier.PromptFrontierExhausted):
        _run_loop(
            monkeypatch=monkeypatch,
            provider=provider,
            fold=fold,
            build_messages=ExhaustedBuilder(),
            provider_config=provider_client.ProviderConfig(
                "custom",
                "adaptive",
                "key",
                context_window_tokens=12_000,
            ),
            on_prompt_frontier_exhaustion=lambda: counted.append(True),
        )

    assert counted == [True]
    assert provider_calls == []
