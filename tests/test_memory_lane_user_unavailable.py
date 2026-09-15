"""Memory-lane failures that are the user's own account problem leave our failure numerator.

Before (2026-09-15): a capture/dream/migrate job that failed because the user's
key was revoked, balance empty, model retired, or V2 model API never
configured counted as a Feedling operational failure — only
chat-lane codes were in Seven's user-unavailable sets.

After: hx approved adding the memory-lane codes for exactly those proven
account classes (additions only, pending Seven's review). Provider outages,
rate limits, timeouts, content filtering, unknown and Feedling-side codes stay
operational. So does ``resident_agent_cli_logged_out``: on hosted V1 runners a
platform key-injection bug prints the same "Not logged in" text (review 09-15),
matching Seven's chat set which also leaves it out.

DB-backed rollup/health coverage lives in tests/test_lane_rollup.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from memory import capture_failure  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402
from model_api_runtime.v2 import worker  # noqa: E402
from notices import agent_call_failure  # noqa: E402
from notices import catalog  # noqa: E402
from notices import status_reason  # noqa: E402
from proactive import proactive_core  # noqa: E402

# The product decision, spelled out once: proven user-account classes.
USER_ACCOUNT_CLASSES = frozenset({
    "auth_invalid",
    "quota_insufficient",
    "model_not_found",
    "provider_account_expired",
})
NOT_USER_CLASSES = frozenset({
    "resident_agent_cli_logged_out",
    "turn_timeout",
    "upstream_unavailable",
    "rate_limited",
    "content_filtered",
    "context_overflow",
    "provider_incompatible",
    "cli_config_invalid",
    "unknown",
})

SEVEN_V1 = frozenset({
    "quota_insufficient",
    "extraction_failed:quota_insufficient",
    "image_generation_quota_insufficient",
    "provider_account_expired",
    "auth_invalid",
    "image_generation_auth_invalid",
    "model_not_found",
    "image_generation_model_not_found",
})
SEVEN_V2 = frozenset({
    "turn_failed:quota_insufficient",
    "extraction_failed:quota_insufficient",
    "turn_failed:image_generation_quota_insufficient",
    "turn_failed:provider_account_expired",
    "turn_failed:auth_invalid",
    "turn_failed:image_generation_auth_invalid",
    "turn_failed:model_not_found",
    "turn_failed:image_generation_model_not_found",
})


def test_additions_are_exactly_the_approved_block_and_seven_entries_are_untouched():
    assert catalog.MEMORY_LANE_USER_UNAVAILABLE_V1_REASONS == frozenset(
        f"{prefix}:{cls}"
        for prefix in ("capture_agent_call_failed", "dream_agent_call_failed",
                       "migrate_agent_call_failed")
        for cls in USER_ACCOUNT_CLASSES
    )
    assert catalog.MEMORY_LANE_USER_UNAVAILABLE_V2_OUTCOME_CODES == frozenset({
        "extraction_failed:auth_invalid",
        "extraction_failed:model_not_found",
        "provider_setup:model_api_not_configured",
        "provider_setup:model_api_not_tested",
        "provider_setup:model_api_key_envelope_missing",
        "provider_setup:model_api_config_invalid",
    })
    assert catalog.USER_UNAVAILABLE_V1_REASONS == (
        SEVEN_V1 | catalog.MEMORY_LANE_USER_UNAVAILABLE_V1_REASONS
    )
    assert catalog.USER_UNAVAILABLE_V2_OUTCOME_CODES == (
        SEVEN_V2 | catalog.MEMORY_LANE_USER_UNAVAILABLE_V2_OUTCOME_CODES
    )


def test_every_addition_is_a_value_its_producer_actually_writes():
    # V1: the status endpoint's normalizer owns this vocabulary and the lanes.
    assert {
        prefix for prefix in agent_call_failure.AGENT_CALL_FAILED_PREFIXES
    } == {"capture_agent_call_failed", "dream_agent_call_failed",
          "migrate_agent_call_failed"}
    assert USER_ACCOUNT_CLASSES <= agent_call_failure.AGENT_CALL_FAILURE_CLASSES
    assert (catalog.MEMORY_LANE_USER_UNAVAILABLE_V1_REASONS
            <= agent_call_failure.PUBLIC_REASON_CODES)
    # V2: registered producer codes; provider_setup is the capture resolver's closed set.
    assert catalog.MEMORY_LANE_USER_UNAVAILABLE_V2_OUTCOME_CODES <= worker.PUBLIC_FAILURE_CODES
    assert {
        code for code in catalog.MEMORY_LANE_USER_UNAVAILABLE_V2_OUTCOME_CODES
        if code.startswith("provider_setup:")
    } == {
        f"{capture_failure.PROVIDER_SETUP_ACCOUNT_CODE}:{slug}"
        for slug in capture_failure.PROVIDER_SETUP_USER_ERRORS
    }


def test_cli_logged_out_still_waits_in_the_escape_valve_but_is_not_excused():
    """Not excused from the failure rate, yet still an account class for the
    capture escape valve (wait 7 days with the login notice, never skip at 6)."""
    for prefix in sorted(agent_call_failure.AGENT_CALL_FAILED_PREFIXES):
        code = f"{prefix}:resident_agent_cli_logged_out"
        assert code not in catalog.USER_UNAVAILABLE_V1_REASONS
        assert catalog.v1_proactive_outcome_class("failed", code) == "operational_failure"
        assert capture_failure.failure_class(code) == "account"
        assert capture_failure.account_error_code(code) == "resident_agent_cli_logged_out"


def test_excluded_codes_are_a_subset_of_what_the_escape_valve_treats_as_account():
    """One classifier: nothing is excused from the failure rate that the capture
    escape valve does not also treat as an account/provider-setup problem."""
    for code in (catalog.MEMORY_LANE_USER_UNAVAILABLE_V1_REASONS
                 | catalog.MEMORY_LANE_USER_UNAVAILABLE_V2_OUTCOME_CODES):
        assert capture_failure.failure_class(code) == "account", code


@pytest.mark.parametrize("prefix", sorted(agent_call_failure.AGENT_CALL_FAILED_PREFIXES))
def test_v1_classifier_splits_account_classes_from_our_failures(prefix):
    for cls in USER_ACCOUNT_CLASSES:
        assert catalog.v1_proactive_outcome_class(
            "failed", f"{prefix}:{cls}") == "user_unavailable"
        # skipped remains a control outcome whatever the reason says
        assert catalog.v1_proactive_outcome_class(
            "skipped", f"{prefix}:{cls}") == "control"
    for cls in NOT_USER_CLASSES:
        assert catalog.v1_proactive_outcome_class(
            "failed", f"{prefix}:{cls}") == "operational_failure"
    # A raw tail (written before status-endpoint normalization) is not guessed at.
    assert catalog.v1_proactive_outcome_class(
        "failed", f"{prefix}:RuntimeError: HTTP 401 invalid api key"
    ) == "operational_failure"
    # V2-shaped codes never leak into the V1 keyspace.
    assert catalog.v1_proactive_outcome_class(
        "failed", "provider_setup:model_api_not_configured") == "operational_failure"


@pytest.mark.parametrize(("raw", "expected"), [
    ('capture_agent_call_failed:RuntimeError: cli agent exited 1: Failed to authenticate. '
     'API Error: 401 {"error":"Insufficient balance"} (api_status=401)', "user_unavailable"),
    # CLI logged out: may be a hosted platform key-injection bug — stays ours.
    ("dream_agent_call_failed:RuntimeError: Failed to authenticate: OAuth session expired "
     "and could not be refreshed", "operational_failure"),
    ("capture_agent_call_failed:RuntimeError: Not logged in · Please run /login",
     "operational_failure"),
    ("capture_agent_call_failed:TimeoutExpired: Command '['claude', '-p']' timed out after 300 "
     "seconds", "operational_failure"),
    ("migrate_agent_call_failed:RuntimeError: 403 Your request was blocked", "user_unavailable"),
    ("capture_agent_call_failed:RuntimeError: 错误码 401：API 密钥无效", "user_unavailable"),
    ("capture_agent_call_failed:RuntimeError: provider_http_403: Request failed. "
     "Please try again later.", "operational_failure"),
    ("dream_agent_call_failed:RuntimeError: 429 Too Many Requests", "operational_failure"),
    ("capture_agent_call_failed:RuntimeError: prompt text says insufficient balance",
     "operational_failure"),
    ("dream_agent_call_failed:RuntimeError: stream disconnected before completion",
     "operational_failure"),
])
def test_v1_stored_reason_from_real_consumer_text_is_classified(raw, expected):
    stored = proactive_core._job_status_patch(
        {"status": "failed", "reason": raw})["status_reason"]
    assert catalog.v1_proactive_outcome_class("failed", stored) == expected
    # Admin surfaces show the stored code intact (no <redacted> bucket).
    assert status_reason.sanitize_status_reason(stored) == stored


def test_v2_terminal_outcome_class_for_memory_lane_codes():
    for code in catalog.MEMORY_LANE_USER_UNAVAILABLE_V2_OUTCOME_CODES:
        assert jobs_store.terminal_outcome_class(code) == "user_unavailable", code
    assert jobs_store.terminal_outcome_class(
        "extraction_failed:quota_insufficient") == "user_unavailable"
    for code in (
        "extraction_failed:upstream_unavailable",
        "extraction_failed:rate_limited",
        "extraction_failed:provider_config",
        "extraction_failed:content_filtered",
        "extraction_failed:unknown",
        "extraction_failed:database_pool_timeout",
        "extraction_failed:json_decode_error",
        "provider_unavailable",
        "slot_watchdog_timeout",
    ):
        assert jobs_store.terminal_outcome_class(code) == "operational_failure", code
    assert jobs_store.terminal_outcome_class("lease_timeout") == "timeout"
