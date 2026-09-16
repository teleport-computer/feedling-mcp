from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import profile_retry, profile_store


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
    [(0, 300.0), (1, 600.0), (2, 1200.0), (7, 21600.0)],
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


# ── T607: transient backoff is bounded; a relay answering a dead route with 5xx
# must not keep a profile job alive forever. Past the cap the job parks as
# provider_config (auto re-armed by the next successful foreground chat), never
# terminal (operator-only).

@pytest.mark.parametrize(
    ("previous_attempts", "disposition"),
    [
        (profile_retry.TRANSIENT_MAX_RETRY_ATTEMPTS - 1, "scheduled"),   # attempt == cap: last retry
        (profile_retry.TRANSIENT_MAX_RETRY_ATTEMPTS, "provider_config"),  # attempt == cap + 1: park
        (10, "provider_config"),                                          # prod shape: attempt 11
    ],
)
def test_transient_retry_parks_as_provider_config_after_cap(previous_attempts, disposition):
    decision = profile_retry.decide_profile_retry(
        error_class="transient_exhausted",
        reject_code="profile_generation_failed:providererror",
        previous_retry_family="transient",
        previous_retry_attempts=previous_attempts,
        now=1000.0,
    )
    assert decision.disposition == disposition
    assert decision.retry_family == "transient"
    assert decision.retry_attempts == previous_attempts + 1
    assert decision.reason == "profile_generation_failed:providererror"
    if disposition == "provider_config":
        assert decision.retry_not_before == 0.0
        assert decision.disposition in profile_store.PROFILE_PROVIDER_SUCCESS_RECOVERABLE_DISPOSITIONS
        assert decision.disposition != "terminal"


def test_transient_cap_counts_only_consecutive_transient_attempts():
    # A family switch resets the counter: the cap never "remembers" a different
    # failure family's attempts.
    decision = profile_retry.decide_profile_retry(
        error_class="transient_exhausted",
        reject_code="profile_generation_failed:providererror",
        previous_retry_family="shape",
        previous_retry_attempts=profile_retry.TRANSIENT_MAX_RETRY_ATTEMPTS + 5,
        now=1000.0,
    )
    assert decision.disposition == "scheduled" and decision.retry_attempts == 1
