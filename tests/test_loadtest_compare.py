"""Tests for scripts/loadtest/compare_tokens.py — the D4 Task 4 tokens/turn
vs resident-baseline comparison (the ROLLBACK gate: V2 tokens/turn must not
regress vs the resident runtime on identical fixtures).

Two pieces under test:
  1. ``compare_tokens_per_turn`` — pure math, no I/O.
  2. ``measure_v2_tokens_per_turn`` — drives the real
     ``model_api_runtime.v2.responder.respond`` path against a MockProvider
     (scripts/loadtest/mock_provider.py) and returns mean tokens/turn.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.loadtest.compare_tokens import (
    compare_tokens_per_turn,
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


# --- measure_v2_tokens_per_turn (against MockProvider, real responder) ---


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


def test_measure_v2_tokens_per_turn_matches_mock_usage():
    with MockProvider(prompt_tokens=100, completion_tokens=20) as provider:
        mean_tokens = measure_v2_tokens_per_turn(
            _FIXTURES, mock_base_url=provider.base_url
        )
    assert mean_tokens == 120.0


def test_measure_v2_tokens_per_turn_reflects_configured_usage():
    with MockProvider(prompt_tokens=50, completion_tokens=10) as provider:
        mean_tokens = measure_v2_tokens_per_turn(
            _FIXTURES, mock_base_url=provider.base_url
        )
    assert mean_tokens == 60.0


# --- exit-code / rollback-signal logic (no subprocess needed) -----------


def test_regression_result_is_the_nonzero_exit_signal():
    # __main__ does `sys.exit(1 if result["regression"] else 0)` — exercise
    # the boolean the exit code is derived from directly, both ways.
    regressed = compare_tokens_per_turn(150.0, 100.0)
    ok = compare_tokens_per_turn(101.0, 100.0)
    assert regressed["regression"] is True
    assert ok["regression"] is False


# --- measure_turn_tokens (drives planner + responder, whole-turn baseline) --


def test_measure_turn_tokens_counts_planner_and_responder():
    from scripts.loadtest.compare_tokens import measure_turn_tokens
    from scripts.loadtest.mock_provider import MockProvider

    # The mock must answer the planner with parseable JSON; responder gets the
    # same string back, which is fine — we are counting tokens, not reading prose.
    plan_json = '{"plan":[{"type":"final_response","payload":{}}],"reason":"t"}'
    fixtures = [{"summary": "", "tail": [{"role": "user", "content": "hello"}]}]

    with MockProvider(reply=plan_json, estimate_tokens=True) as p:
        report = measure_turn_tokens(fixtures, provider=p)

    # One planner call + one responder call per turn, today (pre-loop).
    assert report["llm_calls_per_turn"] == 2.0
    assert report["tokens_per_turn"] > 0
    assert report["tokens_per_turn"] == (
        p.total_prompt_tokens + p.total_completion_tokens) / len(fixtures)


def test_measure_turn_tokens_grows_with_prompt_size():
    from scripts.loadtest.compare_tokens import measure_turn_tokens
    from scripts.loadtest.mock_provider import MockProvider

    plan_json = '{"plan":[{"type":"final_response","payload":{}}],"reason":"t"}'
    small = [{"summary": "", "tail": [{"role": "user", "content": "hi"}]}]
    large = [{"summary": "S" * 8000, "tail": [{"role": "user", "content": "hi"}]}]

    with MockProvider(reply=plan_json, estimate_tokens=True) as p:
        small_report = measure_turn_tokens(small, provider=p)
    with MockProvider(reply=plan_json, estimate_tokens=True) as p:
        large_report = measure_turn_tokens(large, provider=p)

    assert large_report["tokens_per_turn"] > small_report["tokens_per_turn"]


def test_multi_round_turn_costs_more_llm_calls_than_single_round():
    """The loop's cost is real and the instrument must see it. If this passes trivially
    (equal call counts), the mock is not driving the loop and the gate is blind."""
    from scripts.loadtest.compare_tokens import measure_turn_tokens
    from scripts.loadtest.mock_provider import MockProvider

    one_shot = '{"plan":[{"type":"final_response","payload":{}}],"reason":"t"}'
    fixtures = [{"summary": "", "tail": [{"role": "user", "content": "hello"}]}]

    with MockProvider(reply=one_shot, estimate_tokens=True) as p:
        single = measure_turn_tokens(fixtures, provider=p)

    assert single["llm_calls_per_turn"] == 2.0            # planner + responder
    assert single["tokens_per_turn"] > 0

    two_round = [{
        "summary": "",
        "tail": [{"role": "user", "content": "hello"}],
        "planner_replies": [
            '{"plan":[{"type":"memory_search","payload":{"query":"hello"}}]}',
            one_shot,
        ],
    }]
    with MockProvider(reply=one_shot, estimate_tokens=True) as p:
        multi = measure_turn_tokens(two_round, provider=p)

    assert multi["llm_calls_per_turn"] == 3.0  # two planner rounds + responder
    assert multi["tokens_per_turn"] > single["tokens_per_turn"]


def test_main_uses_whole_turn_measurement_and_reports_call_count(capsys):
    from scripts.loadtest import compare_tokens

    exit_code = compare_tokens.main(["--resident-baseline", "1000000"])
    report = __import__("json").loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["llm_calls_per_turn"] > 2.0
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
