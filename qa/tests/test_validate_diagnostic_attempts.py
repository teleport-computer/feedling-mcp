from __future__ import annotations

from copy import deepcopy

import pytest

from qa import validate_diagnostic_attempts as validator


def _result() -> dict:
    scenarios = []
    for scenario_id in validator.SCENARIO_IDS:
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "status": "PRODUCT_FAIL",
                "attempts": 1,
                "attempt_results": [
                    {
                        "attempt": 1,
                        "status": "PRODUCT_FAIL",
                        "failure": {
                            "failure_code": "UNCLASSIFIED_PRODUCT_FAILURE"
                        },
                    }
                ],
                "failure": {"failure_code": "UNCLASSIFIED_PRODUCT_FAILURE"},
            }
        )
    deferred_failure = dict(validator.PARENT_CLEANUP_DEFERRED_FAILURE)
    scenarios[-1] = {
        "scenario_id": "P0-13",
        "status": validator.PARENT_CLEANUP_DEFERRED_STATUS,
        "attempts": 1,
        "attempt_results": [
            {
                "attempt": 1,
                "status": validator.PARENT_CLEANUP_DEFERRED_STATUS,
                "failure": dict(deferred_failure),
            }
        ],
        "assertions": {
            "trace_stages_complete": True,
            "trace_correlation_confirmed": True,
            "latency_attributed": True,
            "cleanup_confirmed": False,
        },
        "evidence_codes": [
            "TRACE_CORRELATION_CONFIRMED",
            "LATENCY_ATTRIBUTED",
        ],
        "failure": deferred_failure,
    }
    return {
        "status": validator.PARENT_CLEANUP_DEFERRED_STATUS,
        "scenarios": scenarios,
        "cleanup": {
            "attempted": False,
            "provider_config_deleted": False,
            "account_reset": False,
            "old_credential_rejected": False,
            "status": validator.PARENT_CLEANUP_DEFERRED_STATUS,
        },
        "diagnostic_codes": ["CLEANUP_FALLBACK_USED"],
    }


def test_accepts_complete_live_attempt_matrix():
    validator.validate_live_attempts(_result())


def test_parent_cleanup_deferral_preserves_missing_latency_evidence():
    result = _result()
    scenario = result["scenarios"][-1]
    scenario["assertions"].update(
        {
            "trace_stages_complete": False,
            "trace_correlation_confirmed": True,
            "latency_attributed": False,
        }
    )
    scenario["evidence_codes"] = ["TRACE_CORRELATION_CONFIRMED"]

    validator.validate_live_attempts(result)


@pytest.mark.parametrize("scenario_index", range(1, 12))
def test_rejects_preflight_block_propagation_for_each_live_scenario(scenario_index):
    result = _result()
    row = result["scenarios"][scenario_index]
    row["status"] = "BLOCKED_DEPLOYMENT"
    row["failure"] = {"failure_code": "PRECONDITION_MISSING"}
    row["attempt_results"][0] = {
        "attempt": 1,
        "status": "BLOCKED_DEPLOYMENT",
        "failure": {"failure_code": "PRECONDITION_MISSING"},
    }

    with pytest.raises(validator.DiagnosticAttemptError, match=row["scenario_id"]):
        validator.validate_live_attempts(result)


def test_allows_known_local_gaps_only_at_preflight_and_parent_cleanup():
    result = _result()
    row = result["scenarios"][0]
    row["status"] = "BLOCKED_DEPLOYMENT"
    row["failure"] = {"failure_code": "PRECONDITION_MISSING"}
    row["attempt_results"][0] = {
        "attempt": 1,
        "status": "BLOCKED_DEPLOYMENT",
        "failure": {"failure_code": "PRECONDITION_MISSING"},
    }

    validator.validate_live_attempts(result)


@pytest.mark.parametrize(
    ("mutation", "value"),
    (
        ("scenario_status", "PRODUCT_FAIL"),
        ("cleanup_confirmed", True),
        ("cleanup_attempted", True),
        ("failure_code", "CLEANUP_FAILED"),
        ("diagnostic_code", []),
    ),
)
def test_rejects_any_noncanonical_parent_cleanup_deferral(mutation, value):
    result = _result()
    scenario = result["scenarios"][-1]
    if mutation == "scenario_status":
        scenario["status"] = value
    elif mutation == "cleanup_confirmed":
        scenario["assertions"]["cleanup_confirmed"] = value
    elif mutation == "cleanup_attempted":
        result["cleanup"]["attempted"] = value
    elif mutation == "failure_code":
        scenario["failure"]["failure_code"] = value
        scenario["attempt_results"][0]["failure"]["failure_code"] = value
    else:
        result["diagnostic_codes"] = value

    with pytest.raises(validator.DiagnosticAttemptError, match="P0-13"):
        validator.validate_live_attempts(result)


def test_parent_cleanup_deferral_preserves_an_earlier_product_failure():
    result = _result()
    result["status"] = "PRODUCT_FAIL"
    result["scenarios"][7]["status"] = "PRODUCT_FAIL"

    validator.validate_live_attempts(result)


def test_rejects_missing_or_malformed_attempt_evidence():
    for mutation in ("missing_scenario", "zero_attempts", "skipped_attempt_number"):
        result = deepcopy(_result())
        if mutation == "missing_scenario":
            result["scenarios"].pop(5)
        elif mutation == "zero_attempts":
            result["scenarios"][5]["attempts"] = 0
            result["scenarios"][5]["attempt_results"] = []
        else:
            result["scenarios"][5]["attempt_results"][0]["attempt"] = 2
        with pytest.raises(validator.DiagnosticAttemptError):
            validator.validate_live_attempts(result)
