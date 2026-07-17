from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from qa import diagnostic_results
from qa.orchestration_contract import PROFILE_IDS


def _profile_schema() -> dict:
    document = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/codex-run-result.schema.json").read_text()
    )
    schema = dict(document["$defs"]["profileResult"])
    schema["$defs"] = document["$defs"]
    return schema


def _release_profile_schema() -> dict:
    document = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/run-result.schema.json").read_text()
    )
    schema = dict(document["$defs"]["profileResult"])
    schema["$defs"] = document["$defs"]
    return schema


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_agent_error_profile_is_schema_valid_and_evidence_negative(profile_id):
    result = diagnostic_results.agent_error_profile(
        {
            "profile_id": profile_id,
            "configured_model": "model-safe",
            "user_id": "synthetic-user-123",
            "runtime_mode": "hosted_resident",
            "trace_enabled": True,
            "api_key": "provider-secret-must-not-escape",
            "secret_key_b64": "content-secret-must-not-escape",
        },
        profile_id=profile_id,
        expected_runtime="hosted_resident",
    )

    assert list(Draft202012Validator(_profile_schema()).iter_errors(result)) == []
    assert result["status"] == "AGENT_ERROR"
    assert result["scenarios"] == []
    assert result["turns"] == []
    assert result["diagnostic_codes"] == [
        "AGENT_EXECUTION_ERROR",
        "TRACE_PARTIAL",
        "STAGE_TIMING_UNAVAILABLE",
    ]
    assert result["reasoning"]["effective_effort"] == "unknown"
    assert result["reasoning"]["raw_private_reasoning_stored"] is False
    assert result["trace"]["correlated_event_count"] == 0
    assert result["cleanup"]["attempted"] is False
    serialized = json.dumps(result, sort_keys=True)
    assert "provider-secret-must-not-escape" not in serialized
    assert "content-secret-must-not-escape" not in serialized


def test_blocked_provision_profile_cannot_validate_as_release_evidence():
    result = diagnostic_results.blocked_provision_profile(
        {
            "profile_id": "relay-kongbeiqie",
            "configured_model": "model-safe",
            "user_id": "synthetic-user-123",
            "provision_status": "blocked",
            "provision_failure_code": "VALID_KEY_REJECTED",
            "api_key": "provider-secret-must-not-escape",
            "secret_key_b64": "content-secret-must-not-escape",
        },
        profile_id="relay-kongbeiqie",
        expected_runtime="hosted_resident",
        provisioning_failure_code="VALID_KEY_REJECTED",
    )

    assert list(Draft202012Validator(_profile_schema()).iter_errors(result)) == []
    release_errors = list(
        Draft202012Validator(_release_profile_schema()).iter_errors(result)
    )
    assert release_errors
    assert any(
        list(error.absolute_path)[-1:] == ["attempts"]
        and "less than the minimum" in error.message
        for error in release_errors
    )
    serialized = json.dumps(result, sort_keys=True)
    assert "provider-secret-must-not-escape" not in serialized
    assert "content-secret-must-not-escape" not in serialized


@pytest.mark.parametrize(
    "provisioning_failure_code",
    ("NONE", "PROVISION_INCOMPLETE", "PROVISIONING_REQUEST_FAILED", ""),
)
def test_blocked_provision_profile_rejects_other_provisioning_failures(
    provisioning_failure_code,
):
    with pytest.raises(
        diagnostic_results.DiagnosticResultError,
        match="requires VALID_KEY_REJECTED",
    ):
        diagnostic_results.blocked_provision_profile(
            {"profile_id": "relay-kongbeiqie"},
            profile_id="relay-kongbeiqie",
            expected_runtime="hosted_resident",
            provisioning_failure_code=provisioning_failure_code,
        )


@pytest.mark.parametrize(
    "provision_status,manifest_failure_code",
    (("ready", "VALID_KEY_REJECTED"), ("blocked", "NONE"), (None, None)),
)
def test_blocked_provision_profile_rejects_manifest_classification_mismatch(
    provision_status,
    manifest_failure_code,
):
    with pytest.raises(
        diagnostic_results.DiagnosticResultError,
        match="requires VALID_KEY_REJECTED",
    ):
        diagnostic_results.blocked_provision_profile(
            {
                "profile_id": "relay-kongbeiqie",
                "provision_status": provision_status,
                "provision_failure_code": manifest_failure_code,
            },
            profile_id="relay-kongbeiqie",
            expected_runtime="hosted_resident",
            provisioning_failure_code="VALID_KEY_REJECTED",
        )


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_blocked_provision_profile_is_complete_schema_valid_and_not_run(profile_id):
    result = diagnostic_results.blocked_provision_profile(
        {
            "profile_id": profile_id,
            "configured_model": "model-safe",
            "user_id": "synthetic-user-123",
            "provision_status": "blocked",
            "provision_failure_code": "VALID_KEY_REJECTED",
            "runtime_mode": "hosted_resident",
            "runtime_version": 2,
            "trace_enabled": True,
            "api_key": "provider-secret-must-not-escape",
            "secret_key_b64": "content-secret-must-not-escape",
        },
        profile_id=profile_id,
        expected_runtime="hosted_resident",
        provisioning_failure_code="VALID_KEY_REJECTED",
    )

    assert list(Draft202012Validator(_profile_schema()).iter_errors(result)) == []
    assert result["status"] == "BLOCKED_CREDENTIAL"
    assert result["observed_runtime"] is None
    assert result["observed_runtime_version"] is None
    assert result["turns"] == []
    assert [row["scenario_id"] for row in result["scenarios"]] == [
        f"P0-{index:02d}" for index in range(1, 14)
    ]
    assert all(
        row["status"] == "BLOCKED_CREDENTIAL"
        and row["attempts"] == 0
        and row["attempt_results"] == []
        and row["request_ids"] == []
        and row["turn_ids"] == []
        and row["trace_ids"] == []
        and row["persona_finalizer"] is None
        and row["evidence_codes"] == []
        and set(row["assertions"].values()) == {False}
        for row in result["scenarios"]
    )
    assert result["latency"]["sample_count"] == 0
    assert result["trace"]["enabled"] is False
    assert result["trace"]["deploy_enabled"] is False
    assert result["reasoning"]["reasoning_event_count"] == 0
    assert result["cleanup"]["attempted"] is False
    serialized = json.dumps(result, sort_keys=True)
    assert "provider-secret-must-not-escape" not in serialized
    assert "content-secret-must-not-escape" not in serialized


def test_agent_error_profile_replaces_unsafe_optional_labels():
    result = diagnostic_results.agent_error_profile(
        {
            "profile_id": "official-gemini",
            "configured_model": "unsafe model\nsecret",
            "user_id": "unsafe user id",
            "runtime_mode": "unexpected runtime\n",
            "trace_enabled": False,
        },
        profile_id="official-gemini",
        expected_runtime="hosted_resident",
    )

    assert result["model"] == "unavailable"
    assert result["user_id"] is None
    assert result["observed_runtime"] is None
    assert result["trace"]["enabled"] is False
    assert result["trace"]["deploy_enabled"] is False


def test_agent_error_profile_preserves_bounded_unicode_relay_model():
    result = diagnostic_results.agent_error_profile(
        {
            "profile_id": "relay-kongbeiqie",
            "configured_model": "[特价纯血]claude-opus-4-6",
            "user_id": "synthetic-user-123",
            "runtime_mode": "hosted_resident",
            "trace_enabled": True,
        },
        profile_id="relay-kongbeiqie",
        expected_runtime="hosted_resident",
    )

    assert result["model"] == "[特价纯血]claude-opus-4-6"


@pytest.mark.parametrize(
    "manifest_profile,profile_id,expected_runtime",
    (
        ({"profile_id": "unknown"}, "unknown", "hosted_resident"),
        (
            {"profile_id": "official-gemini"},
            "official-openai",
            "hosted_resident",
        ),
        ({"profile_id": "official-gemini"}, "official-gemini", "resident_cli"),
    ),
)
def test_agent_error_profile_rejects_unlocked_contracts(
    manifest_profile, profile_id, expected_runtime
):
    with pytest.raises(
        diagnostic_results.DiagnosticResultError, match="diagnostic fallback"
    ):
        diagnostic_results.agent_error_profile(
            manifest_profile,
            profile_id=profile_id,
            expected_runtime=expected_runtime,
        )
