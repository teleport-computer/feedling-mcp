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
    result = compare_tokens_per_turn(100.0, 0.0)
    assert result["regression"] is False
    assert result["delta_ratio"] is None
    assert "reason" in result
    assert result["resident_baseline"] == 0.0


def test_negative_resident_baseline_guarded():
    result = compare_tokens_per_turn(100.0, -5.0)
    assert result["regression"] is False
    assert result["delta_ratio"] is None
    assert "reason" in result


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
