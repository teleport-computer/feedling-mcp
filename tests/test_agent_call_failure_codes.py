"""Resident memory-lane agent-call failures become content-free codes (Bug 20).

Before: the resident consumer wrote
``dream_agent_call_failed:RuntimeError: <provider error text>``; every
content-free surface replaced the whole value with ``runtime_failed``, so ~135
weekly V1 dream failures that were users' own account problems (revoked key,
empty balance, retired model) were indistinguishable from Feedling bugs.

After: the backend classifies the tail with the producer-owned error registry
at write time (status endpoint) and, for rows written before that, at rollup
freeze time. Raw provider text never reaches the stored code.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

import db  # noqa: E402
from notices import agent_call_failure  # noqa: E402
from notices import catalog as notices_catalog  # noqa: E402
from notices import error_contract  # noqa: E402
from notices import status_reason  # noqa: E402
from proactive import proactive_core  # noqa: E402

SECRET = "PRIVATE-PROMPT-ECHO-7f3a"


@pytest.mark.parametrize(
    ("raw_tail", "expected"),
    [
        (f"RuntimeError: pi agent produced no reply: 401 Unauthorized {SECRET}", "auth_invalid"),
        (f"RuntimeError: cli agent exited 1: invalid api key {SECRET}", "auth_invalid"),
        # Relays answer an empty balance with 401; balance evidence must win.
        (f"RuntimeError: HTTP 401: Insufficient balance {SECRET}", "quota_insufficient"),
        (f'RuntimeError: 403 {{"error":{{"message":"insufficient_user_quota"}}}} {SECRET}', "quota_insufficient"),
        (f"RuntimeError: API Error: 402 Payment Required {SECRET}", "quota_insufficient"),
        (f"RuntimeError: Error code: 404 - model_not_found: gpt-9 {SECRET}", "model_not_found"),
        (f"RuntimeError: 429 Too Many Requests {SECRET}", "rate_limited"),
        (f"RuntimeError: status 503 Service Unavailable {SECRET}", "upstream_unavailable"),
        (f"RuntimeError: provider_http_502 {SECRET}", "upstream_unavailable"),
        (f"TimeoutExpired: Command timed out after 600 seconds {SECRET}", "upstream_unavailable"),
        (f"RuntimeError: Connection refused {SECRET}", "upstream_unavailable"),
        (f"RuntimeError: Not logged in · Please run /login {SECRET}", "resident_agent_cli_logged_out"),
        # A bare 5xx-looking number is not upstream evidence.
        (f"RuntimeError: wrote 500 tokens then gave up {SECRET}", "unknown"),
        (f"ValueError: something odd {SECRET}", "unknown"),
        ("RuntimeError", "unknown"),
    ],
)
def test_memory_lane_reasons_classify_into_content_free_codes(raw_tail, expected):
    for prefix in sorted(agent_call_failure.AGENT_CALL_FAILED_PREFIXES):
        code = agent_call_failure.normalize_reason(f"{prefix}:{raw_tail}")
        assert code == f"{prefix}:{expected}"
        assert SECRET not in code
        assert db._LANE_ROLLUP_CODE_RE.fullmatch(code)
        # Display redaction keeps the whole classified code.
        assert status_reason.sanitize_status_reason(code) == code


def test_normalization_is_idempotent_and_leaves_other_reasons_alone():
    code = "dream_agent_call_failed:auth_invalid"
    assert agent_call_failure.normalize_reason(code) == code
    assert agent_call_failure.normalize_reason("dream_agent_call_failed") == (
        "dream_agent_call_failed"
    )
    for untouched in (
        "json_decode_error",
        "dream_nothing_to_consolidate",
        f"agent_call_failed: 401 {SECRET}",  # chat lane: out of scope, unchanged
        "",
    ):
        assert agent_call_failure.normalize_reason(untouched) == untouched


def test_every_class_is_a_registered_displayable_error_class():
    registered = {spec.code for spec in error_contract.public_specs()}
    assert agent_call_failure.AGENT_CALL_FAILURE_CLASSES <= registered
    assert {
        "auth_invalid", "quota_insufficient", "model_not_found",
        "rate_limited", "upstream_unavailable", "unknown",
    } <= agent_call_failure.AGENT_CALL_FAILURE_CLASSES


def test_registry_reads_insufficient_balance_as_quota_for_every_lane():
    """The shared matcher change also fixes Chat's notice for 401+balance."""
    assert notices_catalog.classify_upstream("401 Insufficient Balance") == (
        "quota_insufficient"
    )
    assert notices_catalog.classify_upstream("401 Unauthorized") == "auth_invalid"


def test_status_patch_stores_only_the_code_in_every_reason_field():
    raw = f"dream_agent_call_failed:RuntimeError: 401 invalid api key {SECRET}"
    patch = proactive_core._job_status_patch({
        "status": "failed",
        "reason": raw,
        "dream_result": {"status": "failed", "reason": raw, "job_kind": "memory_dream"},
        "capture_result": {"status": "failed", "reason": raw},
        "noop_reason": raw,
    })
    expected = "dream_agent_call_failed:auth_invalid"
    assert patch["status_reason"] == expected
    assert patch["noop_reason"] == expected
    assert patch["dream_result"] == {
        "status": "failed", "reason": expected, "job_kind": "memory_dream",
    }
    assert patch["capture_result"]["reason"] == expected
    assert SECRET not in repr(patch)


def test_status_patch_keeps_non_agent_call_reasons_byte_for_byte():
    patch = proactive_core._job_status_patch({
        "status": "failed",
        "reason": "json_decode_error",
        "dream_result": {"status": "failed", "reason": "json_decode_error"},
        "noop_reason": "json_decode_error",
    })
    assert patch["status_reason"] == "json_decode_error"
    assert patch["noop_reason"] == "json_decode_error"
    assert patch["dream_result"] == {"status": "failed", "reason": "json_decode_error"}


def test_rollup_code_keeps_class_for_legacy_rows_and_still_logs_raw(caplog):
    raw = f"dream_agent_call_failed:RuntimeError: HTTP 401: Insufficient balance {SECRET}"
    with caplog.at_level("WARNING", logger=db.log.name):
        code = db.content_free_failure_code(
            raw, source="lane_rollup_v1", user_id="usr_x", lane="dream",
            day="2030-06-01",
        )
    assert code == "dream_agent_call_failed:quota_insufficient"
    assert "replacement=dream_agent_call_failed:quota_insufficient" in caplog.text
    # Other free text still collapses exactly as before.
    assert db.content_free_failure_code(
        f"Provider Error {SECRET}", source="lane_rollup_v1", user_id="usr_x",
        lane="heartbeat", day="2030-06-01",
    ) == "runtime_failed"
