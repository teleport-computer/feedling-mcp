from __future__ import annotations

from xml.etree import ElementTree

import pytest

from qa import render_diagnostic_artifacts as renderer


PROFILE_ID = "official-gemini"


def _summary(cot_status: str) -> dict:
    return {
        "schema_version": 1,
        "qualification_mode": "diagnostic",
        "release_qualified": False,
        "run_id": "local-render-unit",
        "candidate_sha": "a" * 40,
        "qualification_harness": {
            "git_head": "b" * 40,
            "dirty": True,
            "source_sha256": "c" * 64,
            "worker_source_sha256": "d" * 64,
            "worker_snapshot_sha256": "d" * 64,
        },
        "status": "DIAGNOSTIC_FAIL" if cot_status != "PASS" else "DIAGNOSTIC_PASS",
        "preflight_only": False,
        "missing_strict_evidence": [],
        "provisioning": {
            "profile_statuses": {PROFILE_ID: "ready"},
            "failure_codes": {PROFILE_ID: "NONE"},
        },
        "cot_delivery": {
            PROFILE_ID: {
                "status": cot_status,
                "failure_code": "NONE" if cot_status == "PASS" else "TRACE_AMBIGUOUS",
                "delivery_qualified": cot_status == "PASS",
            }
        },
    }


def _agent_pass_profile() -> dict:
    return {
        "profile_id": PROFILE_ID,
        "status": "PASS",
        "scenarios": [
            {"scenario_id": scenario_id, "status": "PASS"}
            for scenario_id in renderer.SCENARIO_IDS
        ],
    }


def _blocked_profile() -> dict:
    return {
        "profile_id": PROFILE_ID,
        "status": "BLOCKED_CREDENTIAL",
        "scenarios": [
            {
                "scenario_id": scenario_id,
                "status": "BLOCKED_CREDENTIAL",
                "attempts": 0,
            }
            for scenario_id in renderer.SCENARIO_IDS
        ],
    }


@pytest.mark.parametrize("cot_status", ("FAIL", "UNVERIFIED", "NOT_RUN"))
def test_junit_fails_p012_when_trusted_cot_is_nonpassing(cot_status):
    root = ElementTree.fromstring(
        renderer.render_junit(
            _summary(cot_status),
            {PROFILE_ID: _agent_pass_profile()},
            (PROFILE_ID,),
        )
    )

    assert root.attrib["failures"] == "1"
    assert root.attrib["errors"] == "0"
    p012 = root.find(".//testcase[@name='P0-12']")
    assert p012 is not None
    failure = p012.find("failure")
    assert failure is not None
    assert failure.attrib == {
        "type": "COT_DELIVERY_FAIL",
        "message": f"trusted-cot-delivery:{cot_status}",
    }
    assert all(
        not list(testcase)
        for testcase in root.findall(".//testcase")
        if testcase.attrib["name"] != "P0-12"
    )


def test_junit_keeps_agent_p012_pass_when_trusted_cot_passes():
    root = ElementTree.fromstring(
        renderer.render_junit(
            _summary("PASS"),
            {PROFILE_ID: _agent_pass_profile()},
            (PROFILE_ID,),
        )
    )

    assert root.attrib["failures"] == "0"
    assert root.attrib["errors"] == "0"
    assert root.attrib["tests"] == "14"
    assert root.attrib["name"] == "io-local-api-key-diagnostic"
    provisioning = root.find(".//testcase[@name='PROVISIONING']")
    assert provisioning is not None
    assert list(provisioning) == []
    p012 = root.find(".//testcase[@name='P0-12']")
    assert p012 is not None
    assert list(p012) == []


def test_matrix_separates_cot_gate_failure_from_trusted_observation():
    summary = _summary("FAIL")
    summary["cot_delivery"][PROFILE_ID].update(
        {
            "failure_code": "COT_RESULT_BINDING_MISMATCH",
            "receipt_status": "UNVERIFIED",
            "receipt_failure_code": "CHAT_REQUEST_FAILED",
        }
    )

    matrix = renderer.render_matrix(
        summary,
        {PROFILE_ID: _agent_pass_profile()},
        (PROFILE_ID,),
    )

    assert "COT observation | COT observation code" in matrix
    assert "Agent P0-12" in matrix
    assert "Harness source SHA-256: `" + "c" * 64 + "`" in matrix
    assert "Worker snapshot SHA-256: `" + "d" * 64 + "`" in matrix
    assert (
        "FAIL | COT_RESULT_BINDING_MISMATCH | UNVERIFIED | CHAT_REQUEST_FAILED"
        in matrix
    )


def test_matrix_surfaces_fixed_worker_failure_observability():
    summary = _summary("FAIL")
    summary["orchestration"] = {
        "result_sources": {PROFILE_ID: "deterministic_fallback"},
        "process_exit_codes": {PROFILE_ID: 0},
        "failure_stages": {PROFILE_ID: "STRUCTURED_RESULT"},
        "failure_codes": {PROFILE_ID: "STRUCTURED_RESULT_INVALID"},
    }

    matrix = renderer.render_matrix(
        summary,
        {PROFILE_ID: _agent_pass_profile()},
        (PROFILE_ID,),
    )
    header_line, profile_line = (
        line
        for line in matrix.splitlines()
        if line.startswith("| Profile") or line.startswith(f"| {PROFILE_ID}")
    )
    headers = [cell.strip() for cell in header_line.strip("|").split("|")]
    values = [cell.strip() for cell in profile_line.strip("|").split("|")]
    rendered = dict(zip(headers, values, strict=True))

    assert rendered["Worker source"] == "deterministic_fallback"
    assert rendered["Worker exit"] == "0"
    assert rendered["Worker failure stage"] == "STRUCTURED_RESULT"
    assert rendered["Worker failure code"] == "STRUCTURED_RESULT_INVALID"


def test_matrix_rejects_contradictory_worker_observability():
    summary = _summary("FAIL")
    summary["orchestration"] = {
        "result_sources": {PROFILE_ID: "deterministic_fallback"},
        "process_exit_codes": {PROFILE_ID: 0},
        "failure_stages": {PROFILE_ID: "PROCESS_EXIT"},
        "failure_codes": {PROFILE_ID: "STRUCTURED_RESULT_INVALID"},
    }

    matrix = renderer.render_matrix(
        summary,
        {PROFILE_ID: _agent_pass_profile()},
        (PROFILE_ID,),
    )
    profile_line = next(
        line for line in matrix.splitlines() if line.startswith(f"| {PROFILE_ID}")
    )

    assert "deterministic_fallback" not in profile_line
    assert "STRUCTURED_RESULT_INVALID" not in profile_line
    assert profile_line.count("UNVERIFIED") >= 4


def test_provision_blocked_profile_renders_as_not_run_with_sanitized_code():
    summary = _summary("NOT_RUN")
    summary["provisioning"] = {
        "profile_statuses": {PROFILE_ID: "blocked"},
        "failure_codes": {PROFILE_ID: "VALID_KEY_REJECTED"},
    }
    summary["cot_delivery"][PROFILE_ID].update(
        {
            "failure_code": "VALID_KEY_REJECTED",
            "receipt_status": None,
            "receipt_failure_code": None,
        }
    )
    profiles = {PROFILE_ID: _blocked_profile()}

    matrix = renderer.render_matrix(summary, profiles, (PROFILE_ID,))
    header_line, profile_line = (
        line
        for line in matrix.splitlines()
        if line.startswith("| Profile") or line.startswith(f"| {PROFILE_ID}")
    )
    headers = [cell.strip() for cell in header_line.strip("|").split("|")]
    values = [cell.strip() for cell in profile_line.strip("|").split("|")]
    rendered = dict(zip(headers, values, strict=True))

    assert matrix.startswith("# io local API-key diagnostic\n")
    assert rendered["Provisioning"] == "BLOCKED"
    assert rendered["Provisioning code"] == "VALID_KEY_REJECTED"
    assert rendered["Disposition"] == "NOT_RUN"
    assert rendered["Runtime"] == "NOT_RUN"
    assert rendered["COT delivery"] == "NOT_RUN"
    assert rendered["COT code"] == "VALID_KEY_REJECTED"
    assert all(rendered[f"Agent {scenario_id}"] == "NOT_RUN" for scenario_id in renderer.SCENARIO_IDS)
    assert "COT_DELIVERY_FAIL" not in matrix
    assert "PRODUCT_FAIL" not in matrix

    root = ElementTree.fromstring(
        renderer.render_junit(summary, profiles, (PROFILE_ID,))
    )
    assert root.attrib == {
        "name": "io-local-api-key-diagnostic",
        "tests": "14",
        "failures": "0",
        "errors": "1",
        "skipped": "13",
        "release_qualified": "false",
    }
    suite = root.find("testsuite")
    assert suite is not None
    assert suite.attrib == {
        "name": f"io.diagnostic.{PROFILE_ID}",
        "tests": "14",
        "failures": "0",
        "errors": "1",
        "skipped": "13",
    }
    provisioning = suite.find("testcase[@name='PROVISIONING']")
    assert provisioning is not None
    provision_error = provisioning.find("error")
    assert provision_error is not None
    assert provision_error.attrib == {
        "type": "PROVISIONING_BLOCKED",
        "message": "provisioning:VALID_KEY_REJECTED",
    }
    for testcase in (
        testcase
        for testcase in root.findall(".//testcase")
        if testcase.attrib["name"] != "PROVISIONING"
    ):
        assert testcase.attrib["classname"] == f"io.diagnostic.{PROFILE_ID}"
        skipped = testcase.find("skipped")
        assert skipped is not None
        assert skipped.attrib == {
            "type": "NOT_RUN",
            "message": "provisioning:VALID_KEY_REJECTED",
        }
        assert testcase.find("failure") is None
        assert testcase.find("error") is None


@pytest.mark.parametrize(
    "provisioning",
    (
        None,
        {},
        {
            "profile_statuses": {PROFILE_ID: "blocked"},
            "failure_codes": {PROFILE_ID: "NONE"},
        },
        {
            "profile_statuses": {PROFILE_ID: "blocked"},
            "failure_codes": {PROFILE_ID: "VALID_KEY_REJECTED|unsafe"},
        },
        {
            "profile_statuses": {PROFILE_ID: "blocked"},
            "failure_codes": {PROFILE_ID: "TRACE_UNAVAILABLE"},
        },
        {
            "profile_statuses": {PROFILE_ID: "blocked"},
            "failure_codes": {PROFILE_ID: ["VALID_KEY_REJECTED"]},
        },
        {
            "profile_statuses": {
                PROFILE_ID: "blocked",
                "official-openai": "ready",
            },
            "failure_codes": {
                PROFILE_ID: "VALID_KEY_REJECTED",
                "official-openai": "NONE",
            },
        },
    ),
    ids=(
        "missing",
        "missing-maps",
        "invalid-status-code-pair",
        "unknown-code",
        "non-allowlisted-operational-code",
        "non-string-code",
        "unexpected-profile",
    ),
)
def test_malformed_provisioning_fails_closed_as_unverified_error(provisioning):
    summary = _summary("NOT_RUN")
    if provisioning is None:
        summary.pop("provisioning")
    else:
        summary["provisioning"] = provisioning
    profiles = {PROFILE_ID: _blocked_profile()}

    matrix = renderer.render_matrix(summary, profiles, (PROFILE_ID,))
    profile_line = next(
        line for line in matrix.splitlines() if line.startswith(f"| {PROFILE_ID}")
    )
    assert "| UNVERIFIED | UNVERIFIED | UNVERIFIED |" in profile_line
    assert "NOT_RUN" not in profile_line
    assert "VALID_KEY_REJECTED|unsafe" not in matrix

    root = ElementTree.fromstring(
        renderer.render_junit(summary, profiles, (PROFILE_ID,))
    )
    assert root.attrib["failures"] == "0"
    assert root.attrib["errors"] == "14"
    assert root.attrib["skipped"] == "0"
    assert root.findall(".//skipped") == []
    errors = root.findall(".//error")
    assert len(errors) == 14
    assert all(
        error.attrib
        == {
            "type": "UNVERIFIED",
            "message": "provisioning-metadata:UNVERIFIED",
        }
        for error in errors
    )


@pytest.mark.parametrize("identity", (None, {}), ids=("null", "empty"))
def test_absent_deployment_identity_fields_render_unavailable(identity):
    summary = _summary("PASS")
    summary["deployment_identity"] = identity

    matrix = renderer.render_matrix(
        summary,
        {PROFILE_ID: _agent_pass_profile()},
        (PROFILE_ID,),
    )

    assert "Deployment identity: `UNAVAILABLE`" in matrix
    assert "Identity evidence source: `UNAVAILABLE`" in matrix
    assert "Identity evidence gap: `UNAVAILABLE`" in matrix
    assert "Identity evidence source: `protected_build_identity`" not in matrix
    assert "Identity evidence gap: `NONE`" not in matrix
