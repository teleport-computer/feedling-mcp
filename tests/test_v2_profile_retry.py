from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import profile_retry


@pytest.mark.parametrize(
    (
        "error_class",
        "code",
        "previous_family",
        "previous_attempts",
        "disposition",
        "family",
        "attempts",
    ),
    [
        (
            "transient_exhausted",
            "profile_generation_failed:providererror",
            "",
            0,
            "scheduled",
            "transient",
            1,
        ),
        (
            "provider_config",
            "profile_generation_failed:providererror",
            "transient",
            8,
            "provider_config",
            "provider_config",
            1,
        ),
        ("", "reply_not_json", "", 0, "scheduled", "shape", 1),
        ("", "reply_not_json", "shape", 3, "terminal", "shape", 4),
        ("", "field_empty:memory", "shape", 1, "scheduled", "shape", 2),
        (
            "",
            "profile_source_exceeds_budget:120001",
            "shape",
            2,
            "source_change",
            "source",
            1,
        ),
        (
            "",
            "profile_cards_count_invalid",
            "",
            0,
            "source_change",
            "source",
            1,
        ),
        (
            "",
            "profile_generation_failed:runtimeerror",
            "",
            0,
            "terminal",
            "terminal",
            1,
        ),
    ],
)
def test_retry_policy_matrix(
    error_class,
    code,
    previous_family,
    previous_attempts,
    disposition,
    family,
    attempts,
):
    decision = profile_retry.decide_profile_retry(
        error_class=error_class,
        reject_code=code,
        previous_retry_family=previous_family,
        previous_retry_attempts=previous_attempts,
        now=1000.0,
    )

    assert decision.disposition == disposition
    assert decision.retry_family == family
    assert decision.retry_attempts == attempts
    assert decision.reason == code


@pytest.mark.parametrize(
    ("previous_attempts", "delay"),
    [(0, 300.0), (1, 600.0), (2, 1200.0), (99, 21600.0)],
)
def test_transient_retry_uses_bounded_exponential_delay(previous_attempts, delay):
    decision = profile_retry.decide_profile_retry(
        error_class="transient_exhausted",
        reject_code="profile_generation_failed:providererror",
        previous_retry_family="transient" if previous_attempts else "",
        previous_retry_attempts=previous_attempts,
        now=1000.0,
    )

    assert decision.retry_not_before == 1000.0 + delay


@pytest.mark.parametrize(
    "code",
    [
        "reply_not_text",
        "reply_empty",
        "reply_not_json",
        "missing_field:style",
        "field_empty:memory",
        "placeholder_detected:style",
        "memory_chars_over_budget:9001",
        "style_chars_over_budget:9001",
        # Legacy stored code remains retryable during natural redistillation.
        "user_chars_over_budget:9001",
    ],
)
def test_profile_shape_codes_are_retryable(code):
    decision = profile_retry.decide_profile_retry(
        error_class="",
        reject_code=code,
        previous_retry_family="",
        previous_retry_attempts=0,
        now=1000.0,
    )

    assert decision.disposition == "scheduled"
    assert decision.retry_family == "shape"


def test_shape_retry_budget_uses_retry_attempts_not_cumulative_profile_attempts():
    # A profile may have twenty historical generations. Only the current
    # consecutive shape-failure family participates in the three-retry bound.
    cumulative_profile_attempts = 20
    assert cumulative_profile_attempts > 3
    decision = profile_retry.decide_profile_retry(
        error_class="",
        reject_code="reply_not_json",
        previous_retry_family="shape",
        previous_retry_attempts=1,
        now=1000.0,
    )

    assert decision.disposition == "scheduled"
    assert decision.retry_attempts == 2


def test_unknown_code_cannot_be_promoted_by_partial_text_match():
    decision = profile_retry.decide_profile_retry(
        error_class="",
        reject_code="raw_provider_message_contains_reply_not_json",
        previous_retry_family="",
        previous_retry_attempts=0,
        now=1000.0,
    )

    assert decision.disposition == "terminal"
    assert decision.retry_not_before == 0.0
