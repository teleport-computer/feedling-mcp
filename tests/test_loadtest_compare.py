"""Tests for scripts/loadtest/compare_tokens.py — the D4 Task 4 tokens/turn
vs frozen resident-baseline comparison. V2 must not regress against the
historical benchmark; the retired hosted runtime is not a rollback target.

Two pieces under test:
  1. ``compare_tokens_per_turn`` — pure math, no I/O.
  2. ``measure_v2_tokens_per_turn`` — drives the real
     ``model_api_runtime.v2.tool_loop.run_tool_loop`` path against a
     MockProvider (scripts/loadtest/mock_provider.py), including a native
     tool-call round and the reserved tools-disabled final reply.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.loadtest.compare_tokens import (
    compare_tokens_per_turn,
    measure_turn_tokens,
    measure_v2_tokens_per_turn,
)
from scripts.loadtest.mock_provider import MockProvider


# --- compare_tokens_per_turn (pure) -------------------------------------


def test_within_threshold_is_not_a_regression():
    result = compare_tokens_per_turn(105.0, 100.0, threshold=0.10)
    assert result["v2_mean"] == 105.0
    assert result["resident_baseline"] == 100.0
    assert result["threshold"] == 0.10
    assert abs(result["delta_ratio"] - 0.05) < 1e-9
    assert result["regression"] is False


def test_over_threshold_is_a_regression():
    result = compare_tokens_per_turn(120.0, 100.0, threshold=0.10)
    assert abs(result["delta_ratio"] - 0.20) < 1e-9
    assert result["regression"] is True


def test_exactly_at_threshold_is_not_a_regression():
    # delta_ratio == threshold exactly -> strict '>' means NOT a regression.
    result = compare_tokens_per_turn(110.0, 100.0, threshold=0.10)
    assert abs(result["delta_ratio"] - 0.10) < 1e-9
    assert result["regression"] is False


def test_default_threshold_is_ten_percent():
    within = compare_tokens_per_turn(109.0, 100.0)
    over = compare_tokens_per_turn(111.0, 100.0)
    assert within["regression"] is False
    assert over["regression"] is True


def test_zero_resident_baseline_guarded():
    with pytest.raises(ValueError, match="resident_baseline"):
        compare_tokens_per_turn(100.0, 0.0)


def test_negative_resident_baseline_guarded():
    with pytest.raises(ValueError, match="resident_baseline"):
        compare_tokens_per_turn(100.0, -5.0)


def test_negative_threshold_is_rejected():
    with pytest.raises(ValueError, match="threshold"):
        compare_tokens_per_turn(100.0, 100.0, threshold=-0.01)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_resident_baseline_is_rejected(value):
    with pytest.raises(ValueError, match="resident_baseline"):
        compare_tokens_per_turn(100.0, value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_threshold_is_rejected(value):
    with pytest.raises(ValueError, match="threshold"):
        compare_tokens_per_turn(100.0, 100.0, threshold=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_v2_mean_is_rejected(value):
    with pytest.raises(ValueError, match="v2_mean"):
        compare_tokens_per_turn(value, 100.0)


# --- measure_v2_tokens_per_turn (MockProvider + production native loop) ---


_FIXTURES = [
    {
        "summary": "",
        "tail": [{"role": "user", "content": "hello there"}],
    },
    {
        "summary": "- earlier chit chat",
        "tail": [
            {"role": "user", "content": "hi"},
            {"role": "openclaw", "content": "hey"},
            {"role": "user", "content": "how are you"},
        ],
    },
]

_TOOL_CALL = {
    "id": "loadtest-search-1",
    "name": "memory_search",
    "args": {"query": "hello"},
}


def test_measure_v2_tokens_per_turn_matches_mock_usage():
    with MockProvider(
        prompt_tokens=100,
        completion_tokens=20,
        tool_call=_TOOL_CALL,
    ) as provider:
        mean_tokens = measure_v2_tokens_per_turn(
            _FIXTURES, mock_base_url=provider.base_url
        )
    # Each turn is one tools-enabled function call + one tools-disabled final.
    assert mean_tokens == 240.0
    assert provider.request_count == 2 * len(_FIXTURES)
    for tool_request, final_request in zip(
        provider.request_payloads[::2], provider.request_payloads[1::2]
    ):
        assert tool_request["tools"]
        assert "tools" not in final_request
        # The final request replays the provider-native assistant call and its
        # call-id-matched tool observation, not a flattened planner digest.
        assert any(message.get("tool_calls") for message in final_request["messages"])
        assert any(message.get("role") == "tool" for message in final_request["messages"])


def test_measure_v2_tokens_per_turn_reflects_configured_usage():
    with MockProvider(
        prompt_tokens=50,
        completion_tokens=10,
        tool_call=_TOOL_CALL,
    ) as provider:
        mean_tokens = measure_v2_tokens_per_turn(
            _FIXTURES, mock_base_url=provider.base_url
        )
    assert mean_tokens == 120.0


# --- exit-code / regression-signal logic (no subprocess needed) --------


def test_regression_result_is_the_nonzero_exit_signal():
    # __main__ does `sys.exit(1 if result["regression"] else 0)` — exercise
    # the boolean the exit code is derived from directly, both ways.
    regressed = compare_tokens_per_turn(150.0, 100.0)
    ok = compare_tokens_per_turn(101.0, 100.0)
    assert regressed["regression"] is True
    assert ok["regression"] is False


# --- measure_turn_tokens (drives the production unified native loop) --------


def test_measure_turn_tokens_counts_one_shot_unified_loop():
    fixtures = [{"summary": "", "tail": [{"role": "user", "content": "hello"}]}]

    with MockProvider(reply="final", estimate_tokens=True) as p:
        report = measure_turn_tokens(fixtures, provider=p)

    # A weak model that does not call tools naturally degrades to one request.
    assert report["llm_calls_per_turn"] == 1.0
    assert report["tokens_per_turn"] > 0
    assert report["tokens_per_turn"] == (
        p.total_prompt_tokens + p.total_completion_tokens) / len(fixtures)


def test_measure_turn_tokens_grows_with_prompt_size():
    small = [{"summary": "", "tail": [{"role": "user", "content": "hi"}]}]
    large = [{"summary": "S" * 8000, "tail": [{"role": "user", "content": "hi"}]}]

    with MockProvider(reply="final", estimate_tokens=True) as p:
        small_report = measure_turn_tokens(small, provider=p)
    with MockProvider(reply="final", estimate_tokens=True) as p:
        large_report = measure_turn_tokens(large, provider=p)

    assert large_report["tokens_per_turn"] > small_report["tokens_per_turn"]


def test_multi_round_turn_costs_more_llm_calls_than_single_round():
    """The loop's cost is real and the instrument must see it. If this passes trivially
    (equal call counts), the mock is not driving the loop and the gate is blind."""
    fixtures = [{"summary": "", "tail": [{"role": "user", "content": "hello"}]}]

    with MockProvider(reply="final", estimate_tokens=True) as p:
        single = measure_turn_tokens(fixtures, provider=p)

    assert single["llm_calls_per_turn"] == 1.0
    assert single["tokens_per_turn"] > 0

    two_round = [{
        "summary": "",
        "tail": [{"role": "user", "content": "hello"}],
        "tool_call": _TOOL_CALL,
    }]
    with MockProvider(reply="final", estimate_tokens=True) as p:
        multi = measure_turn_tokens(two_round, provider=p)

    assert multi["llm_calls_per_turn"] == 2.0  # native tool call + final text
    assert multi["tokens_per_turn"] > single["tokens_per_turn"]
    assert p.request_payloads[0]["tools"]
    assert "tools" not in p.request_payloads[1]


def test_main_uses_whole_turn_measurement_and_reports_call_count(capsys):
    from scripts.loadtest import compare_tokens

    exit_code = compare_tokens.main(["--resident-baseline", "1000000"])
    report = __import__("json").loads(capsys.readouterr().out)

    assert exit_code == 0
    # The shared workload mixes two one-shot turns with one native-tool turn.
    assert report["llm_calls_per_turn"] > 1.0
    assert report["v2_mean"] > 0


def test_main_fails_closed_on_invalid_baseline(capsys):
    from scripts.loadtest import compare_tokens

    assert compare_tokens.main(["--resident-baseline", "0"]) == 2
    assert __import__("json").loads(capsys.readouterr().out)["error"] == "invalid_gate_input"


@pytest.mark.parametrize("argv", [
    ["--resident-baseline", "nan"],
    ["--resident-baseline", "inf"],
    ["--resident-baseline", "100", "--threshold", "nan"],
    ["--resident-baseline", "100", "--threshold", "inf"],
])
def test_main_fails_closed_on_nonfinite_inputs(capsys, argv):
    from scripts.loadtest import compare_tokens

    assert compare_tokens.main(argv) == 2
    assert __import__("json").loads(capsys.readouterr().out)["error"] == "invalid_gate_input"


def test_resident_and_v2_gate_use_same_prompts():
    from scripts.loadtest.fixtures import resident_prompts, v2_turn_fixtures

    assert resident_prompts() == [
        fixture["tail"][-1]["content"] for fixture in v2_turn_fixtures()
    ]
