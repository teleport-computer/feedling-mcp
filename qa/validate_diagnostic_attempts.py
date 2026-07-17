#!/usr/bin/env python3
"""Reject agent-authored diagnostic results that short-circuit the live SOP."""

from __future__ import annotations

from typing import Any, Mapping


SCENARIO_IDS = tuple(f"P0-{index:02d}" for index in range(1, 14))
LIVE_SCENARIO_IDS = SCENARIO_IDS[1:12]
PARENT_CLEANUP_DEFERRED_STATUS = "BLOCKED_EVIDENCE"
PARENT_CLEANUP_DEFERRED_FAILURE = {
    "category": PARENT_CLEANUP_DEFERRED_STATUS,
    "stage_code": "CLEANUP",
    "failure_code": "PRECONDITION_MISSING",
    "reproducible": True,
}


class DiagnosticAttemptError(RuntimeError):
    """The profile result does not prove that every live scenario was attempted."""


def _failure_code(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    code = value.get("failure_code")
    return str(code) if isinstance(code, str) else ""


def parent_cleanup_is_deferred(profile_result: Mapping[str, Any]) -> bool:
    """Recognize the one allowed agent-side result before parent cleanup.

    The local diagnostic worker must leave the synthetic account alive so the
    deterministic parent can perform and verify the sole account reset.  This
    predicate is intentionally exact: it cannot turn an arbitrary non-passing
    P0-13 result into a diagnostic success.
    """

    scenarios = profile_result.get("scenarios")
    cleanup = profile_result.get("cleanup")
    diagnostic_codes = profile_result.get("diagnostic_codes")
    if (
        not isinstance(scenarios, list)
        or len(scenarios) != len(SCENARIO_IDS)
        or not isinstance(cleanup, Mapping)
        or not isinstance(diagnostic_codes, list)
        or "CLEANUP_FALLBACK_USED" not in diagnostic_codes
    ):
        return False

    scenario = scenarios[-1]
    if not isinstance(scenario, Mapping) or scenario.get("scenario_id") != "P0-13":
        return False
    assertions = scenario.get("assertions")
    attempts = scenario.get("attempt_results")
    evidence_codes = scenario.get("evidence_codes")
    if (
        scenario.get("status") != PARENT_CLEANUP_DEFERRED_STATUS
        or scenario.get("attempts") != 1
        or not isinstance(assertions, Mapping)
        or set(assertions)
        != {
            "trace_stages_complete",
            "trace_correlation_confirmed",
            "latency_attributed",
            "cleanup_confirmed",
        }
        or any(type(value) is not bool for value in assertions.values())
        or assertions.get("trace_stages_complete")
        is not assertions.get("latency_attributed")
        or assertions.get("cleanup_confirmed") is not False
        or scenario.get("failure") != PARENT_CLEANUP_DEFERRED_FAILURE
        or not isinstance(attempts, list)
        or len(attempts) != 1
        or not isinstance(attempts[0], Mapping)
        or attempts[0].get("attempt") != 1
        or attempts[0].get("status") != PARENT_CLEANUP_DEFERRED_STATUS
        or attempts[0].get("failure") != PARENT_CLEANUP_DEFERRED_FAILURE
        or not isinstance(evidence_codes, list)
        or evidence_codes
        != [
            code
            for assertion, code in (
                ("trace_correlation_confirmed", "TRACE_CORRELATION_CONFIRMED"),
                ("latency_attributed", "LATENCY_ATTRIBUTED"),
            )
            if assertions.get(assertion) is True
        ]
    ):
        return False

    return (
        cleanup.get("attempted") is False
        and cleanup.get("provider_config_deleted") is False
        and cleanup.get("account_reset") is False
        and cleanup.get("old_credential_rejected") is False
        and cleanup.get("status") == PARENT_CLEANUP_DEFERRED_STATUS
    )


def validate_live_attempts(profile_result: Mapping[str, Any]) -> None:
    """Require ordered per-scenario attempts, not propagated preflight blockers.

    P0-01 may lack protected deployment evidence in a local diagnostic, and P0-13
    deliberately defers account reset to the deterministic parent. P0-02 through
    P0-12 must still contain a real outcome for their own live operation. An agent
    that copies ``PRECONDITION_MISSING`` through the remaining matrix has not run
    the requested SOP and is rejected as invalid worker evidence.
    """

    scenarios = profile_result.get("scenarios")
    if not isinstance(scenarios, list):
        raise DiagnosticAttemptError("diagnostic scenario attempts are missing")
    scenario_ids = [
        row.get("scenario_id") if isinstance(row, Mapping) else None
        for row in scenarios
    ]
    if scenario_ids != list(SCENARIO_IDS):
        raise DiagnosticAttemptError("diagnostic scenario matrix is incomplete")

    for row in scenarios[1:12]:
        if not isinstance(row, Mapping):
            raise DiagnosticAttemptError("diagnostic scenario attempt is invalid")
        scenario_id = str(row.get("scenario_id") or "")
        attempts = row.get("attempts")
        attempt_results = row.get("attempt_results")
        if (
            not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts not in (1, 2)
            or not isinstance(attempt_results, list)
            or len(attempt_results) != attempts
        ):
            raise DiagnosticAttemptError(
                f"diagnostic scenario {scenario_id} was not attempted"
            )
        for index, attempt in enumerate(attempt_results, start=1):
            if (
                not isinstance(attempt, Mapping)
                or attempt.get("attempt") != index
                or attempt.get("status") not in (
                    "PASS",
                    "PRODUCT_FAIL",
                    "BLOCKED_CREDENTIAL",
                    "BLOCKED_EVIDENCE",
                    "BLOCKED_DEPLOYMENT",
                    "AGENT_ERROR",
                    "SECURITY_FAIL",
                )
                or _failure_code(attempt.get("failure")) == "PRECONDITION_MISSING"
            ):
                raise DiagnosticAttemptError(
                    f"diagnostic scenario {scenario_id} did not execute its live operation"
                )
        if _failure_code(row.get("failure")) == "PRECONDITION_MISSING":
            raise DiagnosticAttemptError(
                f"diagnostic scenario {scenario_id} propagated a preflight blocker"
            )

    if not parent_cleanup_is_deferred(profile_result):
        raise DiagnosticAttemptError(
            "diagnostic scenario P0-13 is not the fixed parent-cleanup deferral"
        )
