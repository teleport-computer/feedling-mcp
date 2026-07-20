from __future__ import annotations

import csv
import json
import os
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from qa import build_team_report as report
from qa import validate_cleanup_receipt as cleanup_validator
from qa.tests.test_validate_run import (
    SHA,
    _memory_receipt,
    _valid_result,
    _write_orchestration_receipt,
    _write_provisioning_manifest,
    _write_receipt,
)


QA_ROOT = Path(__file__).resolve().parents[1]
COVERAGE = QA_ROOT / "coverage-lock.json"
SCHEMA = QA_ROOT / "schemas" / "run-result.schema.json"
SENTINEL = "FORBIDDEN-PRIVATE-SENTINEL-93f7"


def _cleanup_row(
    profile_id: str,
    *,
    provider_config_preexisted: bool = True,
    provider_config_live_predelete_observed: bool = True,
    provider_config_deletion_source: str = "explicit_api",
) -> dict:
    return {
        "profile_id": profile_id,
        "attempted": True,
        "reset_response_accepted": True,
        "provider_config_preexisted": provider_config_preexisted,
        "provider_config_live_predelete_observed": provider_config_live_predelete_observed,
        "provider_config_deleted": True,
        "key_envelope_deleted": True,
        "provider_config_deletion_source": provider_config_deletion_source,
        "account_reset": True,
        "old_credential_rejected": True,
        "user_absence_verified": True,
        "status": "PASS",
    }


def _cleanup_receipt(run_id: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "deterministic_cleanup_receipt",
        "run_id": run_id,
        "generated_at": "2026-07-19T12:00:00+00:00",
        "attempted": len(cleanup_validator.PROFILE_IDS) + 1,
        "cleaned": len(cleanup_validator.PROFILE_IDS) + 1,
        "failed_profile_ids": [],
        "manifest_deleted": False,
        "manifest_retained_for_scan": True,
        "profiles": [_cleanup_row(item) for item in cleanup_validator.PROFILE_IDS],
        "auxiliary_accounts": [
            _cleanup_row(
                cleanup_validator.MEMORY_CONTRACT_PROFILE_ID,
                provider_config_preexisted=False,
                provider_config_live_predelete_observed=False,
                provider_config_deletion_source="not_applicable",
            )
        ],
    }


def _persona_summary() -> dict:
    scenarios = [
        {
            "scenario_id": scenario_id,
            "scenario_version": report.PERSONA_SCENARIO_VERSION,
            "scenario_sha256": "a" * 63 + f"{index + 1:x}",
            "status": "PASS",
            "trajectory_count": 1,
            "trajectory_status_counts": {"COMPLETED": 1},
            "metrics": [
                {
                    "metric_id": "persona.contract",
                    "metric_version": "v1",
                    "evaluator_type": "DETERMINISTIC",
                    "hard_gate": True,
                    "status": "PASS",
                    "result_count": 1,
                    "pass_count": 1,
                    "fail_count": 0,
                    "blocked_count": 0,
                    "infra_error_count": 0,
                    "pass_rate": 1.0,
                    "average_score": 1.0,
                    "threshold": 0.8,
                    "failure_codes": [],
                    "rubric_sha256": "b" * 63 + f"{index + 1:x}",
                }
            ],
        }
        for index, scenario_id in enumerate(report.PERSONA_SCENARIO_IDS)
    ]
    return {
        "schema_version": 1,
        "kind": "persona_memory_qualification_summary",
        "status": "PASS",
        "started_at": "2026-07-19T11:00:00+00:00",
        "finished_at": "2026-07-19T11:01:00+00:00",
        "target": {
            "label": "candidate",
            "build_sha": "a" * 40,
            "runtime_mode": "hosted_resident",
            "provider": None,
            "model": None,
            "reasoning_effort": "unknown",
        },
        "coverage": {
            "repetitions": 1,
            "scenario_count": len(scenarios),
            "trajectory_count": len(scenarios),
            "metric_result_count": len(scenarios),
        },
        "judge": None,
        "hashes": {},
        "scenarios": scenarios,
    }


def _inputs(tmp_path: Path, result: dict | None = None) -> dict[str, Path]:
    source = tmp_path / "source"
    source.mkdir()
    document = deepcopy(_valid_result() if result is None else result)
    result_path = source / "run-result.json"
    result_path.write_text(json.dumps(document), encoding="utf-8")
    cleanup_path = source / "cleanup-receipt.json"
    cleanup_path.write_text(
        json.dumps(_cleanup_receipt(document["run_id"])), encoding="utf-8"
    )
    (source / "memory-contract.json").write_text(
        json.dumps(_memory_receipt()), encoding="utf-8"
    )
    (source / "persona-memory-summary.json").write_text(
        json.dumps(_persona_summary()), encoding="utf-8"
    )
    (source / "persona-memory-matrix.md").write_text(
        f"untrusted source markdown {SENTINEL}\n", encoding="utf-8"
    )
    manifest_path = _write_provisioning_manifest(tmp_path)
    manifest = _read_json(manifest_path)
    for entry, profile in zip(manifest["profiles"], document["profiles"], strict=True):
        entry["label"] = f"agent-e2e-{document['run_id']}-{profile['profile_id']}"
        entry["user_id"] = profile["user_id"]
        entry["configured_model"] = profile["model"]
        entry["valid_key_receipt"]["model"] = profile["model"]
    manifest["auxiliary_accounts"][0]["label"] = (
        f"agent-e2e-{document['run_id']}-memory-contract"
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    orchestration_path = _write_orchestration_receipt(tmp_path, document)
    deployment_path = _write_receipt(tmp_path, "deployment-receipt.json")
    post_deployment_path = _write_receipt(tmp_path, "post-deployment-receipt.json")
    return {
        "source": source,
        "result": result_path,
        "cleanup": cleanup_path,
        "manifest": manifest_path,
        "orchestration": orchestration_path,
        "deployment": deployment_path,
        "post_deployment": post_deployment_path,
        "output": tmp_path / "team-report",
    }


def _build(paths: dict[str, Path], *, output: Path | None = None) -> Path:
    destination = paths["output"] if output is None else output
    report.build_team_report(
        coverage_path=COVERAGE,
        schema_path=SCHEMA,
        result_path=paths["result"],
        provisioning_manifest_path=paths["manifest"],
        orchestration_receipt_path=paths["orchestration"],
        deployment_receipt_path=paths["deployment"],
        post_deployment_receipt_path=paths["post_deployment"],
        cleanup_receipt_path=paths["cleanup"],
        source_artifacts=paths["source"],
        output_path=destination,
        actor="qa-engineer",
        persona_repetitions=1,
        persona_pipeline_outcomes={
            "prepare": "success",
            "live": "success",
            "cleanup": "success",
            "finalize": "success",
        },
        expected_runtime="hosted_resident",
        expected_sha=SHA,
    )
    return destination


def _set_failure(
    result: dict,
    *,
    profile_index: int,
    scenario_id: str,
    status: str,
    stage_code: str,
    failure_code: str,
    failed_assertions: tuple[str, ...],
    reported_reproducible: bool = True,
) -> dict:
    profile = result["profiles"][profile_index]
    scenario = next(
        row for row in profile["scenarios"] if row["scenario_id"] == scenario_id
    )
    failure = {
        "category": status,
        "stage_code": stage_code,
        "failure_code": failure_code,
        "reproducible": reported_reproducible,
    }
    scenario.update(
        status=status,
        failure=failure,
        attempts=1,
        attempt_results=[{"attempt": 1, "status": status, "failure": failure}],
    )
    for assertion in failed_assertions:
        scenario["assertions"][assertion] = False
    profile["status"] = status
    result["overall_status"] = status
    result["summary"] = {
        "pass": 8,
        "product_fail": int(status == "PRODUCT_FAIL"),
        "blocked_credential": int(status == "BLOCKED_CREDENTIAL"),
        "blocked_evidence": int(status == "BLOCKED_EVIDENCE"),
        "blocked_deployment": int(status == "BLOCKED_DEPLOYMENT"),
        "agent_error": int(status == "AGENT_ERROR"),
        "security_fail": int(status == "SECURITY_FAIL"),
    }
    return scenario


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_all_pass_report_has_exact_owner_only_outputs(tmp_path):
    paths = _inputs(tmp_path)
    output = _build(paths)

    assert {path.name for path in output.iterdir()} == report.OUTPUT_FILES
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())

    run_index = _read_json(output / "run-index.json")
    failure_index = _read_json(output / "failure-index.json")
    assert run_index["duration_ms"] == 0
    assert run_index["trigger_actor"] == "qa-engineer"
    assert run_index["cleanup"]["status"] == "PASS"
    assert len(run_index["matrix"]) == 9
    assert failure_index["failure_count"] == 0
    assert failure_index["api_key_failure_count"] == 0
    assert failure_index["persona_memory_failure_count"] == 0
    assert failure_index["exact_id_failure_count"] == 0
    assert (
        "All locked API-key scenarios passed."
        in (output / "team-summary.md").read_text()
    )
    assert (
        "All formal persona-memory scenarios passed."
        in (output / "team-summary.md").read_text()
    )
    assert SENTINEL not in (output / "persona-memory-matrix.md").read_text()


def test_formal_persona_failure_is_detailed_and_marks_private_exact_ids(tmp_path):
    paths = _inputs(tmp_path)
    persona_path = paths["source"] / "persona-memory-summary.json"
    persona = _read_json(persona_path)
    persona.update(status="FAIL")
    scenario = next(
        row for row in persona["scenarios"] if row["scenario_id"] == "persona-stability"
    )
    scenario["status"] = "FAIL"
    scenario["metrics"][0].update(
        metric_id="persona.tone_consistency",
        evaluator_type="LLM_JUDGE",
        status="FAIL",
        pass_count=0,
        fail_count=1,
        pass_rate=0.0,
        average_score=0.4,
        failure_codes=["PERSONA_IDENTITY_DRIFT"],
    )
    persona_path.write_text(json.dumps(persona), encoding="utf-8")

    output = _build(paths)
    failure_index = _read_json(output / "failure-index.json")
    assert failure_index["failure_count"] == 1
    assert failure_index["api_key_failure_count"] == 0
    assert failure_index["persona_memory_failure_count"] == 1
    assert failure_index["exact_id_failure_count"] == 1
    failure = failure_index["failures"][0]
    assert failure["source"] == "persona_memory"
    assert failure["scenario_id"] == "persona-stability"
    assert failure["exact_id_debug_available"] is True
    assert failure["metrics"][0]["failure_codes"] == ["PERSONA_IDENTITY_DRIFT"]
    run_index = _read_json(output / "run-index.json")
    assert run_index["overall_status"] == "PRODUCT_FAIL"
    assert run_index["api_key_overall_status"] == "PASS"
    rendered = (output / "team-summary.md").read_text(encoding="utf-8")
    assert "Overall: `PRODUCT_FAIL`" in rendered
    assert "API-key matrix: `PASS`" in rendered
    assert "## Persona-memory aggregate failures" in rendered
    assert "persona.tone_consistency" in rendered
    assert "PERSONA_IDENTITY_DRIFT" in rendered
    assert "recipient-encrypted bundle eligible" in rendered
    assert not any(
        forbidden in json.dumps(failure)
        for forbidden in ("user_id", "request_id", "turn_id", "trace_id", "job_id")
    )


@pytest.mark.parametrize(
    ("persona_status", "metric_counts", "expected_overall"),
    (
        (
            "BLOCKED_EVIDENCE",
            {"fail_count": 0, "blocked_count": 1, "infra_error_count": 0},
            "BLOCKED_EVIDENCE",
        ),
        (
            "INFRA_ERROR",
            {"fail_count": 0, "blocked_count": 0, "infra_error_count": 1},
            "AGENT_ERROR",
        ),
    ),
)
def test_persona_nonpass_status_maps_into_combined_headline(
    tmp_path, persona_status, metric_counts, expected_overall
):
    paths = _inputs(tmp_path)
    persona_path = paths["source"] / "persona-memory-summary.json"
    persona = _read_json(persona_path)
    persona["status"] = persona_status
    scenario = persona["scenarios"][0]
    scenario["status"] = persona_status
    scenario["metrics"][0].update(
        status=persona_status,
        pass_count=0,
        pass_rate=0.0,
        average_score=None,
        failure_codes=["JUDGE_NOT_CONFIGURED"],
        **metric_counts,
    )
    persona_path.write_text(json.dumps(persona), encoding="utf-8")

    output = _build(paths)
    run_index = _read_json(output / "run-index.json")
    assert run_index["api_key_overall_status"] == "PASS"
    assert run_index["overall_status"] == expected_overall
    assert f"Overall: `{expected_overall}`" in (output / "team-summary.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda summary: summary["scenarios"].pop(),
        lambda summary: summary["scenarios"].reverse(),
        lambda summary: summary["scenarios"][0].update(scenario_version="v1"),
        lambda summary: summary["scenarios"][0].update(trajectory_count=0),
        lambda summary: summary["coverage"].update(scenario_count=7),
        lambda summary: summary["coverage"].update(trajectory_count=7),
    ),
)
def test_formal_persona_summary_requires_exact_locked_coverage(tmp_path, mutate):
    paths = _inputs(tmp_path)
    persona_path = paths["source"] / "persona-memory-summary.json"
    persona = _read_json(persona_path)
    mutate(persona)
    persona_path.write_text(json.dumps(persona), encoding="utf-8")

    with pytest.raises(report.TeamReportError, match="persona-memory"):
        _build(paths)
    assert not paths["output"].exists()


def test_report_is_deterministic_across_fresh_output_roots(tmp_path):
    paths = _inputs(tmp_path)
    first = _build(paths, output=tmp_path / "first")
    second = _build(paths, output=tmp_path / "second")

    assert {name: (first / name).read_bytes() for name in report.OUTPUT_FILES} == {
        name: (second / name).read_bytes() for name in report.OUTPUT_FILES
    }


def test_security_child_cannot_be_hidden_by_parent_profile_status(tmp_path):
    result = _valid_result()
    scenario = _set_failure(
        result,
        profile_index=0,
        scenario_id="P0-06",
        status="SECURITY_FAIL",
        stage_code="PERSONA_IMPORT",
        failure_code="REDACTION_ASSERTION_FAILED",
        failed_assertions=("privacy_canary_absent",),
    )
    result["profiles"][0]["status"] = "BLOCKED_EVIDENCE"
    scenario["persona_finalizer"].update(finalizer_ok=False, privacy_violation_count=1)
    paths = _inputs(tmp_path, result)

    output = _build(paths)
    run_index = _read_json(output / "run-index.json")
    first = run_index["matrix"][0]
    assert first["status"] == "SECURITY_FAIL"
    assert first["reported_status"] == "BLOCKED_EVIDENCE"
    assert first["worst_child_status"] == "SECURITY_FAIL"
    assert "PROFILE_STATUS_MASKS_WORSE_CHILD" in {
        row["code"] for row in run_index["report_inconsistencies"]
    }


def test_headline_and_summary_are_derived_from_child_scenarios(tmp_path):
    result = _valid_result()
    _set_failure(
        result,
        profile_index=0,
        scenario_id="P0-08",
        status="PRODUCT_FAIL",
        stage_code="BASIC_CHAT",
        failure_code="CHAT_TIMEOUT",
        failed_assertions=("exact_reply_correlated",),
    )
    result["profiles"][0]["status"] = "PASS"
    result["overall_status"] = "PASS"
    result["summary"] = {
        "pass": 9,
        "product_fail": 0,
        "blocked_credential": 0,
        "blocked_evidence": 0,
        "blocked_deployment": 0,
        "agent_error": 0,
        "security_fail": 0,
    }

    output = _build(_inputs(tmp_path, result))
    run_index = _read_json(output / "run-index.json")
    assert run_index["overall_status"] == "PRODUCT_FAIL"
    assert run_index["reported_overall_status"] == "PASS"
    assert run_index["summary"]["pass"] == 8
    assert run_index["summary"]["product_fail"] == 1
    assert "Overall: `PRODUCT_FAIL`" in (output / "team-summary.md").read_text()
    assert (
        "API-key overall status: `PRODUCT_FAIL`" in (output / "matrix.md").read_text()
    )
    assert {
        "RUN_STATUS_DIFFERS_FROM_PROFILES",
        "RUN_SUMMARY_DIFFERS_FROM_PROFILES",
        "PROFILE_STATUS_MASKS_WORSE_CHILD",
    }.issubset({row["code"] for row in run_index["report_inconsistencies"]})


def test_one_attempt_is_observed_once_and_reported_reproducibility_is_warned(tmp_path):
    result = _valid_result()
    _set_failure(
        result,
        profile_index=0,
        scenario_id="P0-08",
        status="PRODUCT_FAIL",
        stage_code="BASIC_CHAT",
        failure_code="CHAT_TIMEOUT",
        failed_assertions=("exact_reply_correlated",),
        reported_reproducible=True,
    )
    output = _build(_inputs(tmp_path, result))

    failure = _read_json(output / "failure-index.json")["failures"][0]
    assert failure["reproduction_state"] == "OBSERVED_ONCE"
    assert "REPRODUCIBILITY_FLAG_UNATTESTED" in failure["report_inconsistencies"]
    assert "Reproduction: `OBSERVED_ONCE`" in (output / "team-summary.md").read_text()


def test_positive_evidence_for_false_assertion_is_surfaced(tmp_path):
    result = _valid_result()
    _set_failure(
        result,
        profile_index=0,
        scenario_id="P0-12",
        status="PRODUCT_FAIL",
        stage_code="REASONING",
        failure_code="REASONING_METADATA_MISSING",
        failed_assertions=("reasoning_metadata_present",),
    )
    output = _build(_inputs(tmp_path, result))

    warnings = _read_json(output / "run-index.json")["report_inconsistencies"]
    contradiction = next(
        row
        for row in warnings
        if row["code"] == "POSITIVE_EVIDENCE_FOR_FAILED_ASSERTION"
    )
    assert contradiction["assertion"] == "reasoning_metadata_present"
    assert contradiction["evidence_code"] == "REASONING_METADATA_CONFIRMED"


@pytest.mark.parametrize(
    "mutation",
    ("false_assertion", "missing_evidence", "invalid_attempt", "turn_binding"),
)
def test_false_green_pass_contract_fails_closed_without_output(tmp_path, mutation):
    result = _valid_result()
    profile = result["profiles"][0]
    scenario = next(
        row for row in profile["scenarios"] if row["scenario_id"] == "P0-08"
    )
    if mutation == "false_assertion":
        scenario["assertions"]["exact_reply_correlated"] = False
    elif mutation == "missing_evidence":
        scenario["evidence_codes"].remove("EXACT_REPLY_CORRELATED")
    elif mutation == "invalid_attempt":
        scenario["attempt_results"][0]["attempt"] = 2
    else:
        scenario["request_ids"] = ["request-that-does-not-bind-to-the-turn"]
    paths = _inputs(tmp_path, result)

    with pytest.raises(report.TeamReportError, match="canonical PASS evidence"):
        _build(paths)
    assert not paths["output"].exists()


@pytest.mark.parametrize(
    "mutation",
    ("turn", "persona", "reasoning", "trace"),
)
def test_false_green_backing_evidence_fails_closed_without_output(tmp_path, mutation):
    result = _valid_result()
    profile = result["profiles"][0]
    if mutation == "turn":
        next(turn for turn in profile["turns"] if turn["scenario_id"] == "P0-08")[
            "content_assertion_passed"
        ] = False
    elif mutation == "persona":
        next(row for row in profile["scenarios"] if row["scenario_id"] == "P0-06")[
            "persona_finalizer"
        ]["privacy_violation_count"] = 1
    elif mutation == "reasoning":
        profile["reasoning"]["token_metadata_present"] = False
    else:
        profile["trace"]["correlated_event_count"] = 0
    paths = _inputs(tmp_path, result)

    with pytest.raises(report.TeamReportError, match="canonical PASS evidence"):
        _build(paths)
    assert not paths["output"].exists()


@pytest.mark.parametrize(
    "mutation",
    ("unsafe_profile_redaction", "duplicate_persona_job", "reply_timeout"),
)
def test_false_green_global_semantics_fail_closed_without_output(tmp_path, mutation):
    result = _valid_result()
    if mutation == "unsafe_profile_redaction":
        result["profiles"][0]["redaction"]["prompt_injection_detected"] = True
    elif mutation == "duplicate_persona_job":
        first = next(
            row
            for row in result["profiles"][0]["scenarios"]
            if row["scenario_id"] == "P0-06"
        )["persona_finalizer"]
        second = next(
            row
            for row in result["profiles"][1]["scenarios"]
            if row["scenario_id"] == "P0-06"
        )["persona_finalizer"]
        second["job_id"] = first["job_id"]
    else:
        turn = next(
            row
            for row in result["profiles"][0]["turns"]
            if row["scenario_id"] == "P0-08"
        )
        turn["reply_latency_ms"] = 120_001
        result["profiles"][0]["latency"]["reply_p95_ms"] = 120_001
    paths = _inputs(tmp_path, result)

    with pytest.raises(
        report.TeamReportError,
        match="redaction evidence|identities are not unique|canonical PASS evidence",
    ):
        _build(paths)
    assert not paths["output"].exists()


@pytest.mark.parametrize("mutation", ("runtime", "route", "reasoning_effort"))
def test_mixed_run_cannot_weaken_another_pass_profile_contract(tmp_path, mutation):
    result = _valid_result()
    _set_failure(
        result,
        profile_index=0,
        scenario_id="P0-08",
        status="PRODUCT_FAIL",
        stage_code="BASIC_CHAT",
        failure_code="CHAT_TIMEOUT",
        failed_assertions=("exact_reply_correlated",),
    )
    passing_profile = result["profiles"][1]
    if mutation == "runtime":
        passing_profile["observed_runtime"] = "deployed_current"
        passing_profile["observed_runtime_version"] = 1
    elif mutation == "route":
        passing_profile["route_family"] = "openrouter"
    else:
        passing_profile["reasoning_effort"] = "high"
    paths = _inputs(tmp_path, result)

    with pytest.raises(
        report.TeamReportError,
        match="canonical PASS profile|canonical run result is invalid",
    ):
        _build(paths)
    assert not paths["output"].exists()


def test_failure_details_are_safe_for_persona_reasoning_and_cleanup(tmp_path):
    result = _valid_result()
    persona = _set_failure(
        result,
        profile_index=0,
        scenario_id="P0-06",
        status="SECURITY_FAIL",
        stage_code="PERSONA_IMPORT",
        failure_code="REDACTION_ASSERTION_FAILED",
        failed_assertions=("privacy_canary_absent",),
    )
    persona["persona_finalizer"].update(
        request_id=SENTINEL + "-persona-request",
        job_id=SENTINEL + "-persona-job",
        finalizer_ok=False,
        privacy_violation_count=1,
    )
    reasoning = _set_failure(
        result,
        profile_index=1,
        scenario_id="P0-12",
        status="PRODUCT_FAIL",
        stage_code="REASONING",
        failure_code="REASONING_METADATA_MISSING",
        failed_assertions=("reasoning_metadata_present", "reasoning_tokens_present"),
    )
    reasoning["request_ids"] = [SENTINEL + "-request"]
    reasoning["turn_ids"] = [SENTINEL + "-turn"]
    reasoning["trace_ids"] = [SENTINEL + "-trace"]
    result["profiles"][1]["reasoning"].update(
        request_id=SENTINEL + "-reasoning-request",
        turn_id=SENTINEL + "-reasoning-turn",
        trace_id=SENTINEL + "-reasoning-trace",
        metadata_present=False,
        token_metadata_present=False,
    )
    cleanup = _set_failure(
        result,
        profile_index=2,
        scenario_id="P0-13",
        status="BLOCKED_EVIDENCE",
        stage_code="CLEANUP",
        failure_code="PRECONDITION_MISSING",
        failed_assertions=("cleanup_confirmed",),
    )
    cleanup["request_ids"] = [SENTINEL + "-cleanup-request"]
    result["profiles"][0]["user_id"] = SENTINEL + "-user"
    output = _build(_inputs(tmp_path, result))

    aggregate = b"\n".join(path.read_bytes() for path in output.iterdir())
    assert SENTINEL.encode() not in aggregate
    failures = _read_json(output / "failure-index.json")["failures"]
    by_scenario = {row["scenario_id"]: row for row in failures}
    assert by_scenario["P0-06"]["persona"] == {
        "archive_receipts_verified": True,
        "archive_upload_count": 4,
        "evidence_sha256": persona["persona_finalizer"]["evidence_sha256"],
        "finalizer_ok": False,
        "fixture_id": "persona-import-v1",
        "genesis_upload_metadata_verified": True,
        "privacy_violation_count": 1,
        "private_evidence_deleted": True,
        "semantic_judgment_bound": True,
    }
    assert by_scenario["P0-12"]["reasoning"]["metadata_present"] is False
    assert by_scenario["P0-12"]["reasoning"]["token_metadata_present"] is False
    assert by_scenario["P0-13"]["cleanup"]["status"] == "PASS"


def test_latency_uses_full_run_scoped_handles_and_omits_raw_ids(tmp_path):
    result = _valid_result()
    raw_ids = {
        value
        for profile in result["profiles"]
        for turn in profile["turns"]
        for value in (turn["request_id"], turn["turn_id"], turn["trace_id"])
    }
    output = _build(_inputs(tmp_path, result))

    latency_text = (output / "latency.csv").read_text(encoding="utf-8")
    assert not any(value in latency_text for value in raw_ids)
    with (output / "latency.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {"request_handle", "turn_handle", "trace_handle"}.issubset(rows[0])
    turn = next(row for row in rows if row["record_type"] == "turn")
    assert all(
        len(turn[field]) == 64
        for field in ("request_handle", "turn_handle", "trace_handle")
    )
    assert all(
        set(turn[field]) <= set("0123456789abcdef")
        for field in ("request_handle", "turn_handle", "trace_handle")
    )


def test_forbidden_persona_source_field_fails_without_output(tmp_path):
    paths = _inputs(tmp_path)
    persona_path = paths["source"] / "persona-memory-summary.json"
    persona = _read_json(persona_path)
    persona["prompt"] = SENTINEL
    persona_path.write_text(json.dumps(persona), encoding="utf-8")

    with pytest.raises(report.TeamReportError, match="invalid"):
        _build(paths)
    assert not paths["output"].exists()


def test_unknown_persona_value_is_not_republished(tmp_path):
    paths = _inputs(tmp_path)
    persona_path = paths["source"] / "persona-memory-summary.json"
    persona = _read_json(persona_path)
    persona["notes"] = SENTINEL
    persona_path.write_text(json.dumps(persona), encoding="utf-8")

    with pytest.raises(report.TeamReportError, match="invalid"):
        _build(paths)
    assert not paths["output"].exists()


def test_missing_persona_summary_yields_fixed_infra_projection(tmp_path):
    paths = _inputs(tmp_path)
    (paths["source"] / "persona-memory-summary.json").unlink()
    (paths["source"] / "persona-memory-matrix.md").unlink()

    output = _build(paths)
    summary = _read_json(output / "persona-memory-summary.json")
    assert summary["status"] == "INFRA_ERROR"
    assert summary["failure_code"] == "PERSONA_SUMMARY_UNAVAILABLE"
    assert summary["pipeline_outcomes"] == {
        "prepare": "success",
        "live": "success",
        "cleanup": "success",
        "finalize": "success",
    }


def test_malformed_input_fails_closed_without_output(tmp_path):
    paths = _inputs(tmp_path)
    paths["result"].write_text("{not-json", encoding="utf-8")

    with pytest.raises(report.TeamReportError, match="canonical run result is invalid"):
        _build(paths)
    assert not paths["output"].exists()


@pytest.mark.parametrize("target", ("result", "cleanup"))
def test_symlink_input_fails_closed_without_output(tmp_path, target):
    paths = _inputs(tmp_path)
    path = paths[target]
    backing = path.with_suffix(path.suffix + ".backing")
    path.rename(backing)
    path.symlink_to(backing)

    with pytest.raises(report.TeamReportError, match="unsafe|invalid"):
        _build(paths)
    assert not paths["output"].exists()


def test_occupied_output_is_never_modified_or_removed(tmp_path):
    paths = _inputs(tmp_path)
    paths["output"].mkdir()
    marker = paths["output"] / "owned-by-user"
    marker.write_text(SENTINEL, encoding="utf-8")

    with pytest.raises(report.TeamReportError, match="occupied"):
        _build(paths)
    assert marker.read_text(encoding="utf-8") == SENTINEL


@pytest.mark.parametrize("field", ("user_id", "configured_model"))
def test_trusted_provisioning_mismatch_fails_closed_without_output(tmp_path, field):
    paths = _inputs(tmp_path)
    manifest = _read_json(paths["manifest"])
    manifest["profiles"][0][field] = f"mismatched-{field}"
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    paths["manifest"].chmod(0o600)

    with pytest.raises(report.TeamReportError, match="trusted run evidence"):
        _build(paths)
    assert not paths["output"].exists()


def test_trusted_deployment_mismatch_fails_closed_without_output(tmp_path):
    paths = _inputs(tmp_path)
    receipt = _read_json(paths["post_deployment"])
    receipt["observed_backend_sha"] = "b" * 40
    paths["post_deployment"].write_text(json.dumps(receipt), encoding="utf-8")
    paths["post_deployment"].chmod(0o600)

    with pytest.raises(report.TeamReportError, match="trusted run evidence"):
        _build(paths)
    assert not paths["output"].exists()


@pytest.mark.parametrize(
    ("pre_verified_at", "post_verified_at"),
    (
        ("2026-07-13T12:01:00Z", "2026-07-13T11:59:00Z"),
        ("2026-07-13T12:01:00Z", "2026-07-13T12:02:00Z"),
    ),
    ids=("reversed", "does-not-bracket-start"),
)
def test_trusted_deployment_receipts_must_bracket_run_without_output(
    tmp_path, pre_verified_at, post_verified_at
):
    paths = _inputs(tmp_path)
    pre_receipt = _read_json(paths["deployment"])
    post_receipt = _read_json(paths["post_deployment"])
    pre_receipt["verified_at"] = pre_verified_at
    post_receipt["verified_at"] = post_verified_at
    paths["deployment"].write_text(json.dumps(pre_receipt), encoding="utf-8")
    paths["post_deployment"].write_text(json.dumps(post_receipt), encoding="utf-8")

    with pytest.raises(report.TeamReportError, match="trusted run evidence"):
        _build(paths)
    assert not paths["output"].exists()


def test_trusted_deployment_receipt_identity_drift_fails_without_output(tmp_path):
    paths = _inputs(tmp_path)
    receipt = _read_json(paths["post_deployment"])
    receipt["observed_deployment_sha"] = SHA.upper()
    paths["post_deployment"].write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(report.TeamReportError, match="trusted run evidence"):
        _build(paths)
    assert not paths["output"].exists()


def test_output_symlink_is_rejected_without_touching_target(tmp_path):
    paths = _inputs(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "marker"
    marker.write_text(SENTINEL, encoding="utf-8")
    paths["output"].symlink_to(target, target_is_directory=True)

    with pytest.raises(report.TeamReportError, match="occupied"):
        _build(paths)
    assert marker.read_text(encoding="utf-8") == SENTINEL


def test_cli_returns_sanitized_error_without_echoing_input(tmp_path, capsys):
    paths = _inputs(tmp_path)
    paths["result"].write_text(SENTINEL, encoding="utf-8")

    rc = report.main(
        [
            "--coverage",
            str(COVERAGE),
            "--schema",
            str(SCHEMA),
            "--result",
            str(paths["result"]),
            "--provisioning-manifest",
            str(paths["manifest"]),
            "--orchestration-receipt",
            str(paths["orchestration"]),
            "--deployment-receipt",
            str(paths["deployment"]),
            "--post-deployment-receipt",
            str(paths["post_deployment"]),
            "--cleanup-receipt",
            str(paths["cleanup"]),
            "--source-artifacts",
            str(paths["source"]),
            "--output",
            str(paths["output"]),
            "--actor",
            "qa-engineer",
            "--expected-runtime",
            "hosted_resident",
            "--expected-sha",
            SHA,
            "--persona-repetitions",
            "1",
            "--persona-prepare-outcome",
            "success",
            "--persona-live-outcome",
            "success",
            "--persona-cleanup-outcome",
            "success",
            "--persona-finalize-outcome",
            "success",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert SENTINEL not in captured.out + captured.err
    assert not paths["output"].exists()


def test_output_file_set_has_no_directories_or_symlinks(tmp_path):
    output = _build(_inputs(tmp_path))

    assert all(path.is_file() for path in output.iterdir())
    assert not any(path.is_symlink() for path in output.iterdir())
    assert len((output / "team-summary.md").read_bytes()) <= report.MAX_SUMMARY_BYTES
    assert os.listdir(output) and set(os.listdir(output)) == report.OUTPUT_FILES


def test_built_report_passes_the_real_secret_and_identifier_scanner(tmp_path):
    from qa.tests.test_scan_artifacts import _write_inputs as scan_inputs

    builder_root = tmp_path / "builder"
    scanner_root = tmp_path / "scanner"
    builder_root.mkdir()
    scanner_root.mkdir()
    paths = _inputs(builder_root)
    scan = scan_inputs(scanner_root)
    scan.write_canonical(_read_json(paths["result"]))
    for child in scan.artifacts.iterdir():
        child.unlink()
    scan.artifacts.rmdir()

    _build(paths, output=scan.artifacts)

    assert scan.scan() == []
