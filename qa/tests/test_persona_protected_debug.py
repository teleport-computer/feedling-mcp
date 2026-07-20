from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from qa import persona_protected_debug as persona_debug
from qa import publish_persona_memory_summary as publisher
from qa.regression.contracts import ExperimentResult, canonical_json_sha256
from qa.regression.tests.test_publish_persona_memory_summary import (
    PRIVATE_PROMPT,
    PRIVATE_RATIONALE,
    PRIVATE_REPLY,
    _arm as source_arm,
    _result as source_result,
)


RUN_ID = "unit-api-key-run"
ACCOUNT_ID = "synthetic-persona-account-1"
SECOND_ACCOUNT_ID = "synthetic-persona-account-2"
PIPELINE_OUTCOMES = {
    "prepare": "success",
    "live": "success",
    "cleanup": "success",
    "finalize": "failure",
}


def _fingerprint(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def _bound_private_result(
    *, include_unobserved_declared_account: bool = False
) -> tuple[dict, dict]:
    document = deepcopy(source_result().to_dict())
    experiment_id = f"{RUN_ID}-persona-memory"
    document["experiment_id"] = experiment_id
    document["targets"][0].update(
        build_sha="b" * 40,
        runtime_mode="hosted_resident",
        provider="openai",
    )

    account_ids = [ACCOUNT_ID]
    if include_unobserved_declared_account:
        account_ids.append(SECOND_ACCOUNT_ID)
    account_fingerprints = sorted(_fingerprint(user_id) for user_id in account_ids)
    observed_fingerprint = _fingerprint(ACCOUNT_ID)

    trajectory = document["trajectories"][0]
    trajectory["experiment_id"] = experiment_id
    trajectory["metadata"] = {
        "protected_debug_account_fingerprints": account_fingerprints,
        "protected_debug_identifiers": {
            "request_ids": ["failed-request-after-boundary"],
            "response_ids": ["failed-response-after-boundary"],
            "trace_ids": ["failed-trace-after-boundary"],
        },
        "private_note": PRIVATE_PROMPT,
    }
    trajectory["turns"][0]["metadata"] = {
        "protected_debug": {"account_fingerprint": observed_fingerprint}
    }
    trajectory["boundary_evidence"] = [
        {
            "kind": "runtime_rotation",
            "evidence": {
                "protected_debug_identifiers": {
                    "capture_job_ids": ["capture-job-1"],
                    "request_ids": ["boundary-request-1"],
                    "response_ids": ["boundary-response-1"],
                    "trace_ids": ["boundary-trace-1"],
                    "runtime_session_ids": ["runtime-session-before", "runtime-session-after"],
                },
                "private_trace_body": PRIVATE_REPLY,
            },
        }
    ]
    for metric in document["metric_results"]:
        metric["experiment_id"] = experiment_id

    document["metadata"].update(
        account_fingerprints=account_fingerprints,
        account_pool_manifest_sha256="3" * 64,
        protected_debug_accounts=[
            {
                "account_fingerprint": _fingerprint(user_id),
                "user_id": user_id,
            }
            for user_id in account_ids
        ],
    )
    result = ExperimentResult.from_dict(document)
    arm = source_arm(result)
    arm["account_fingerprints"] = account_fingerprints
    result_sha256 = canonical_json_sha256(document)
    summary = publisher.build_public_summary(
        result,
        arm,
        result_sha256=result_sha256,
        arm_receipt_sha256="b" * 64,
    )
    summary["pipeline_outcomes"] = dict(PIPELINE_OUTCOMES)
    return document, summary


def test_projects_only_exact_failure_correlation_ids_and_fixed_codes():
    document, summary = _bound_private_result()

    failures = persona_debug.build_persona_failures(
        document,
        summary,
        canonical_run_id=RUN_ID,
        expected_runtime="hosted_resident",
        expected_deployment_sha="b" * 40,
    )

    assert len(failures) == 1
    failure = failures[0]
    assert set(failure) == persona_debug.PERSONA_FAILURE_FIELDS
    assert failure["scenario_id"] == "persona-pressure"
    assert failure["status"] == "FAIL"
    trajectory = failure["trajectories"][0]
    assert set(trajectory) == persona_debug.PERSONA_TRAJECTORY_FIELDS
    assert trajectory["account_ids"] == [ACCOUNT_ID]
    assert trajectory["session_ids"] == ["private-session-id"]
    assert trajectory["request_ids"] == [
        "private-request-id",
        "boundary-request-1",
        "failed-request-after-boundary",
    ]
    assert trajectory["response_ids"] == [
        "private-response-id",
        "boundary-response-1",
        "failed-response-after-boundary",
    ]
    assert trajectory["turn_ids"] == ["private-turn-id"]
    assert trajectory["trace_ids"] == [
        "private-trace-id",
        "boundary-trace-1",
        "failed-trace-after-boundary",
    ]
    assert trajectory["capture_job_ids"] == ["capture-job-1"]
    assert trajectory["runtime_session_ids"] == [
        "runtime-session-before",
        "runtime-session-after",
    ]
    assert "PERSONA_IDENTITY_DRIFT" in trajectory["failure_codes"]

    rendered = json.dumps(failures, sort_keys=True)
    for forbidden in (
        PRIVATE_PROMPT,
        PRIVATE_REPLY,
        PRIVATE_RATIONALE,
        "private_trace_body",
        "prompt",
        "rationale",
        "token",
    ):
        assert forbidden not in rendered
    persona_debug.validate_persona_failures(failures)


def test_declared_account_without_observed_turn_binding_fails_closed():
    document, summary = _bound_private_result(
        include_unobserved_declared_account=True
    )

    with pytest.raises(
        persona_debug.PersonaDebugError,
        match="trajectory account binding is ambiguous",
    ):
        persona_debug.build_persona_failures(
            document,
            summary,
            canonical_run_id=RUN_ID,
            expected_runtime="hosted_resident",
            expected_deployment_sha="b" * 40,
        )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda summary: summary["hashes"].update(result_sha256="0" * 64),
        lambda summary: summary["target"].update(build_sha="a" * 40),
        lambda summary: summary["scenarios"][0].update(status="PASS"),
    ),
)
def test_public_summary_must_bind_exactly_to_private_result(mutate):
    document, summary = _bound_private_result()
    mutate(summary)

    with pytest.raises(persona_debug.PersonaDebugError, match="summary binding"):
        persona_debug.build_persona_failures(
            document,
            summary,
            canonical_run_id=RUN_ID,
            expected_runtime="hosted_resident",
            expected_deployment_sha="b" * 40,
        )


def test_decrypted_schema_rejects_content_or_unknown_identifier_fields():
    document, summary = _bound_private_result()
    failures = persona_debug.build_persona_failures(
        document,
        summary,
        canonical_run_id=RUN_ID,
        expected_runtime="hosted_resident",
        expected_deployment_sha="b" * 40,
    )
    failures[0]["trajectories"][0]["raw_response"] = PRIVATE_REPLY

    with pytest.raises(persona_debug.PersonaDebugError, match="trajectory is invalid"):
        persona_debug.validate_persona_failures(failures)
