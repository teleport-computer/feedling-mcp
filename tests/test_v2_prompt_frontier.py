import pathlib
import sys
import asyncio
import math

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import prompt_frontier as frontier
from model_api_runtime.v2 import tool_loop
import provider_client
from capabilities import tool_schema
from provider_types import ToolCall, ToolExchange, ToolResult, ToolSpec


def _model_limit(context_window_tokens: int = 2_048) -> frontier.ModelPromptLimit:
    return frontier.ModelPromptLimit(
        provider="test",
        model="test-model",
        context_window_tokens=context_window_tokens,
        source="deployment_override",
        override_key="test:test-model",
    )


_REAL_TOOL_COUNT = 69
_REAL_TOOL_CATALOG_BYTES = 32_684


def _real_sized_mixed_tool_catalog() -> tuple[list[ToolSpec], list[ToolSpec]]:
    """34-ish platform tools + user MCP, matching the captured production size.

    The fixture is derived from the real platform catalog rather than copying a
    toy schema list. ASCII description padding makes the combined canonical
    payload exactly 32,684 bytes while keeping 69 independently named tools.
    """
    platform = list(tool_schema.build_tool_specs())
    mcp_count = _REAL_TOOL_COUNT - len(platform)
    assert mcp_count > 0, "platform catalog outgrew the captured 69-tool surface"

    def mcp_specs(description_lengths: list[int]) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=f"mcp__boundary__search_{index:02d}",
                description="d" * description_length,
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            )
            for index, description_length in enumerate(description_lengths)
        ]

    empty_mcp = mcp_specs([0] * mcp_count)
    base_bytes = len(frontier.canonical_json(platform + empty_mcp).encode("utf-8"))
    padding = _REAL_TOOL_CATALOG_BYTES - base_bytes
    assert padding >= 0, "real platform schemas alone exceed captured fixture size"
    each, remainder = divmod(padding, mcp_count)
    mcp = mcp_specs([
        each + (1 if index < remainder else 0)
        for index in range(mcp_count)
    ])
    combined = platform + mcp
    assert len(combined) == _REAL_TOOL_COUNT
    assert (
        len(frontier.canonical_json(combined).encode("utf-8"))
        == _REAL_TOOL_CATALOG_BYTES
    )
    return platform, mcp


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
    rendered = frontier.canonical_json(tools).encode("utf-8")
    ascii_bytes = sum(byte < 0x80 for byte in rendered)
    non_ascii_bytes = len(rendered) - ascii_bytes

    assert component == frontier.PromptComponent(
        name="tool_schemas",
        estimated_tokens=(
            int(
                math.ceil(
                    ascii_bytes / frontier.TOOL_SCHEMA_UTF8_BYTES_PER_TOKEN
                )
            )
            + non_ascii_bytes
            + len(tools) * frontier.TOOL_SCHEMA_STRUCTURAL_OVERHEAD_TOKENS
        ),
        required=True,
        priority=0,
    )


def test_non_ascii_schema_bytes_never_inherit_ascii_calibration():
    ascii_tool = ToolSpec(
        "lookup",
        "calendar lookup",
        {"type": "object", "properties": {}},
    )
    multilingual_tool = ToolSpec(
        "lookup",
        "查询日历安排",
        {"type": "object", "properties": {}},
    )
    ascii_json = frontier.canonical_json([ascii_tool]).encode("utf-8")
    multilingual_json = frontier.canonical_json([multilingual_tool]).encode("utf-8")
    ascii_non_ascii_bytes = sum(byte >= 0x80 for byte in ascii_json)
    multilingual_non_ascii_bytes = sum(
        byte >= 0x80 for byte in multilingual_json
    )
    assert ascii_non_ascii_bytes == 0
    assert multilingual_non_ascii_bytes > 0

    ascii_cost = frontier.tool_schemas_component([ascii_tool]).estimated_tokens
    multilingual_cost = frontier.tool_schemas_component(
        [multilingual_tool]
    ).estimated_tokens
    assert multilingual_cost > ascii_cost
    multilingual_ascii_bytes = (
        len(multilingual_json) - multilingual_non_ascii_bytes
    )
    assert multilingual_cost == (
        math.ceil(
            multilingual_ascii_bytes
            / frontier.TOOL_SCHEMA_UTF8_BYTES_PER_TOKEN
        )
        + multilingual_non_ascii_bytes
        + frontier.TOOL_SCHEMA_STRUCTURAL_OVERHEAD_TOKENS
    )


def test_large_cjk_tool_catalog_keeps_every_non_ascii_byte_conservative():
    tools = [
        ToolSpec(
            f"mcp__calendar__lookup_{index}",
            "查询日历安排和提醒" * 40,
            {
                "type": "object",
                "properties": {"查询": {"type": "string"}},
            },
        )
        for index in range(69)
    ]
    rendered = frontier.canonical_json(tools).encode("utf-8")
    non_ascii_bytes = sum(byte >= 0x80 for byte in rendered)
    estimate = frontier.tool_schemas_component(tools).estimated_tokens
    wrapper_reserve = (
        len(tools) * frontier.TOOL_SCHEMA_STRUCTURAL_OVERHEAD_TOKENS
    )

    assert non_ascii_bytes > 0
    assert estimate - wrapper_reserve >= non_ascii_bytes


def test_schema_wrapper_reserve_covers_every_current_provider_wire_shape():
    platform, mcp = _real_sized_mixed_tool_catalog()
    tools = platform + mcp
    canonical_bytes = len(frontier.canonical_json(tools).encode("utf-8"))
    reserved_wrapper_bytes = math.floor(
        len(tools)
        * frontier.TOOL_SCHEMA_STRUCTURAL_OVERHEAD_TOKENS
        * frontier.TOOL_SCHEMA_UTF8_BYTES_PER_TOKEN
    )
    provider_shapes = {
        "openai_chat": provider_client._encode_tools_openai_chat(tools),
        "openai_responses": provider_client._encode_tools_openai_responses(tools),
        "anthropic": provider_client._encode_tools_anthropic(tools),
        "bedrock": provider_client._encode_tools_bedrock(tools),
        "gemini": provider_client._encode_tools_gemini(tools),
    }

    for provider, shape in provider_shapes.items():
        wire_bytes = len(frontier.canonical_json(shape).encode("utf-8"))
        assert wire_bytes <= canonical_bytes + reserved_wrapper_bytes, provider


def test_schema_calibration_keeps_ascii_catalog_inside_real_small_window():
    """The text estimator must not be reused for ASCII-heavy tool schemas.

    Grow the fixture from the frontier's own budget rather than copying a
    schema-size threshold into the test. The sample must straddle the old
    one-byte estimate and the calibrated estimate; changing the production
    ratio back to one therefore makes this guard fail before planning.
    """

    model_limit = _model_limit(16_384)
    budget = frontier.build_prompt_budget(model_limit.context_window_tokens)
    messages = [{"role": "user", "content": "hello"}]
    message_cost = frontier.messages_component(
        messages, name="message_context"
    ).estimated_tokens
    available_for_tools = budget.input_budget_tokens - message_cost
    tools = []
    calibrated = None
    legacy = None
    for index in range(1, 1_000):
        tools.append(ToolSpec(
            name=f"catalog_tool_{index}",
            description="lookup structured records " * 8,
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        ))
        calibrated = frontier.tool_schemas_component(
            tools, required=False
        ).estimated_tokens
        legacy = frontier.estimate_structured_tokens(
            tools,
            structural_overhead_tokens=(
                len(tools) * frontier.TOOL_SCHEMA_STRUCTURAL_OVERHEAD_TOKENS
            ),
            utf8_bytes_per_token=(
                frontier.DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN
            ),
        )
        if calibrated <= available_for_tools < legacy:
            break

    assert calibrated is not None and legacy is not None
    assert calibrated <= available_for_tools < legacy, (
        "fixture did not cross the legacy schema-estimator gate"
    )
    plan = frontier.plan_provider_round(
        model_limit=model_limit,
        messages=messages,
        tools=tools,
    )
    assert "tool_schemas" not in plan.omitted_optional_components


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
    budget = frontier.build_prompt_budget(
        _model_limit().context_window_tokens,
        output_reserve_tokens=128,
        safety_margin_tokens=128,
    )
    overflow_bytes = int(
        budget.input_budget_tokens * frontier.TOOL_SCHEMA_UTF8_BYTES_PER_TOKEN
    ) + 1_024
    tools = [
        {
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": "z" * overflow_bytes,
            },
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


def test_historical_tool_schema_is_required_when_optional_catalog_does_not_fit():
    messages = [
        {"role": "user", "content": "find my memories"},
        ToolExchange(
            calls=(ToolCall(id="m1", name="memory_index", args={}),),
            results=(ToolResult(call_id="m1", content="one memory"),),
        ),
    ]
    tools = [
        ToolSpec("memory_index", "list memories", {"type": "object"}),
        ToolSpec("large_optional", "z" * 15_000, {"type": "object"}),
    ]

    plan = frontier.plan_provider_round(
        model_limit=_model_limit(4_096),
        messages=messages,
        tools=tools,
        required_tool_names={"memory_index"},
        output_reserve_tokens=128,
        safety_margin_tokens=128,
    )

    assert "required_tool_schemas" in plan.included_components
    assert plan.included_tool_names == ("memory_index",)
    assert plan.offered_tool_names == ("memory_index", "large_optional")


def test_budget_pressure_keeps_mcp_memory_and_reply_floor_before_tail():
    messages = [{"role": "user", "content": "use my connected service"}]
    floor_tools = [
        ToolSpec("memory_index", "browse memory", {"type": "object"}),
        ToolSpec("memory_write", "write memory", {"type": "object"}),
        ToolSpec("reply", "reply now", {"type": "object"}),
        ToolSpec("mcp__calendar__events", "calendar", {"type": "object"}),
    ]
    tail_tools = [
        ToolSpec(
            f"optional_tail_{index}",
            "non-core capability " * 20,
            {"type": "object", "properties": {}},
        )
        for index in range(40)
    ]
    tools = [*tail_tools, *floor_tools]
    message_cost = frontier.messages_component(
        messages, name="message_context"
    ).estimated_tokens
    floor_cost = frontier.tool_schemas_component(floor_tools).estimated_tokens
    full_cost = frontier.tool_schemas_component(tools).estimated_tokens
    output_reserve = 128
    safety_margin = 128
    context_window = max(
        frontier.MIN_CONTEXT_WINDOW_TOKENS,
        message_cost + floor_cost + output_reserve + safety_margin + 128,
    )
    budget = frontier.build_prompt_budget(
        context_window,
        output_reserve_tokens=output_reserve,
        safety_margin_tokens=safety_margin,
    )
    assert message_cost + floor_cost <= budget.input_budget_tokens
    assert message_cost + full_cost > budget.input_budget_tokens, (
        "fixture did not cross the complete-catalog budget gate"
    )

    plan = frontier.plan_provider_round(
        model_limit=_model_limit(context_window),
        messages=messages,
        tools=tools,
        output_reserve_tokens=output_reserve,
        safety_margin_tokens=safety_margin,
    )

    included = set(plan.included_tool_names)
    assert {tool.name for tool in floor_tools}.issubset(included)
    assert len(plan.included_tool_names) < len(plan.offered_tool_names)


def test_historical_tool_schema_fails_closed_when_required_frontier_is_exhausted():
    messages = [
        {"role": "user", "content": "find my memories"},
        ToolExchange(
            calls=(ToolCall(id="m1", name="memory_index", args={}),),
            results=(ToolResult(call_id="m1", content="one memory"),),
        ),
    ]
    tools = [
        ToolSpec("memory_index", "z" * 15_000, {"type": "object"}),
    ]

    with pytest.raises(frontier.PromptFrontierExhausted) as caught:
        frontier.plan_provider_round(
            model_limit=_model_limit(4_096),
            messages=messages,
            tools=tools,
            required_tool_names={"memory_index"},
            output_reserve_tokens=128,
            safety_margin_tokens=128,
        )

    assert "required_tool_schemas" in caught.value.required_components


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

    async def provider(_config, _messages, *, tools=None, **kwargs):
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
                        "name": "identity_get",
                        "args": {},
                }
            ],
            "usage": {},
        }
        for index in range(1, 5)
    ]

    async def provider(_config, messages, *, tools=None, **kwargs):
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
                "test-160k",
                "key",
                # 160K, not a knife-edge window: round 3's prompt is two 65K
                # exchanges PLUS the real tool catalog. At 150K the catalog had
                # ~2.4K bytes of slack, so any capability addition flipped
                # round 3 into "omit atomic tool_schemas" (tools=None => an
                # early terminal round) instead of the round-4 transcript
                # exhaustion this test is about. The headroom keeps the tested
                # boundary the aggregate transcript, not the catalog's size.
                context_window_tokens=160_000,
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

    async def provider(_config, _messages, *, tools=None, **kwargs):
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


def test_fourteen_mcp_catalog_pressure_reaches_provider_with_memory_floor(
    monkeypatch,
):
    """The reported MCP cardinality must retain real provider memory tools.

    This intentionally crosses the planner/tool-loop seam: assertions inspect
    the ``tools`` array received by the fake provider, not the frontier plan.
    """
    calls = []
    surfaces = []
    mcp_tools = [
        ToolSpec(
            f"mcp__connected__tool_{index}",
            "user-selected connected capability",
            {"type": "object", "properties": {}},
        )
        for index in range(14)
    ]

    async def provider(_config, messages, *, tools=None, **kwargs):
        calls.append((list(messages), tools))
        return {"reply": "text-only", "tool_calls": [], "usage": {}}

    async def fold():
        return []

    async def record_surface(detail):
        surfaces.append(detail)

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
            *mcp_tools,
            ToolSpec(
                "large_read",
                "z" * 60_000,
                {"type": "object", "properties": {}},
            )
        ],
        prompt_output_reserve_tokens=2_000,
        prompt_safety_margin_tokens=0,
        on_provider_tool_surface=record_surface,
    )

    assert outcome.final_text == "text-only"
    assert len(calls) == 1
    sent_names = {spec.name for spec in calls[0][1]}
    assert {
        "memory_index",
        "memory_search",
        "memory_fetch",
        "memory_write",
        "memory_organize",
        "reply",
    }.issubset(sent_names)
    assert {tool.name for tool in mcp_tools}.issubset(sent_names)
    assert "large_read" not in sent_names
    assert len(sent_names) < len(tool_loop._catalog()) + len(mcp_tools) + 1
    assert len(surfaces) == 1
    surface = surfaces[0]
    assert surface["round"] == 1
    assert surface["sent_tool_count"] == len(sent_names)
    assert surface["candidate_tool_count"] > surface["sent_tool_count"]
    assert surface["dropped_tool_count"] == (
        surface["candidate_tool_count"] - surface["sent_tool_count"]
    )
    assert surface["mcp_candidate_tool_count"] == 15
    assert surface["mcp_sent_tool_count"] == 14
    assert surface["mcp_dropped_tool_count"] == 1
    assert surface["reason"] == "frontier_omitted"
    assert calls[0][0] == [{"role": "system", "content": "stable prefix"}]


@pytest.mark.parametrize(
    "context_window_tokens",
    [16_384, 32_768, 40_000, 128_000],
)
def test_real_sized_mixed_catalog_reaches_provider_at_common_window_boundaries(
    monkeypatch,
    context_window_tokens,
):
    """A production-sized catalog must not turn into a text-only request.

    The captured failure had 69 tools / 32,684 canonical UTF-8 bytes. Previous
    fixtures used a handful of tiny schemas, so the atomic omission branch was
    never reached. This assertion is at the final provider exit: accounting a
    catalog upstream does not count if ``tool_loop`` later sends ``tools=None``.
    """
    platform, mcp = _real_sized_mixed_tool_catalog()
    calls = []

    async def provider(_config, _messages, *, tools=None, **_kwargs):
        calls.append(tools)
        return {"reply": "ok", "tool_calls": [], "usage": {}}

    async def fold():
        return []

    async def on_file_reply(_path, _revision):
        return None

    async def on_image_reply(_args):
        return ()

    outcome = _run_loop(
        monkeypatch=monkeypatch,
        provider=provider,
        fold=fold,
        build_messages=lambda transcript: [
            {"role": "system", "content": "stable prefix"},
            {"role": "user", "content": "use my connected tools"},
            *transcript,
        ],
        provider_config=provider_client.ProviderConfig(
            "custom",
            f"boundary-{context_window_tokens}",
            "key",
            "https://relay.example/v1",
            context_window_tokens=context_window_tokens,
        ),
        extra_tool_specs=mcp,
        on_file_reply=on_file_reply,
        on_image_reply=on_image_reply,
    )

    assert outcome.final_text == "ok"
    assert len(calls) == 1
    offered = calls[0]
    assert offered is not None, (
        f"{context_window_tokens}-token route silently dropped the complete "
        f"{_REAL_TOOL_COUNT}-tool surface"
    )
    offered_names = {spec.name for spec in offered}
    assert len(offered) == _REAL_TOOL_COUNT
    assert {spec.name for spec in platform} <= offered_names
    assert {spec.name for spec in mcp} <= offered_names


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

    async def provider(_config, messages, *, tools=None, **kwargs):
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

    async def provider(_config, messages, *, tools=None, **kwargs):
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
