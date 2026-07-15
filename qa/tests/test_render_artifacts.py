from __future__ import annotations

import csv
import json
import os
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree

import pytest

from qa import render_artifacts as renderer
from qa.tests.test_validate_run import _valid_result


QA_DIR = Path(__file__).resolve().parents[1]
SCHEMA = QA_DIR / "schemas" / "run-result.schema.json"


def _write_canonical(
    tmp_path: Path, result: dict | None = None
) -> tuple[Path, Path, dict]:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    document = _valid_result() if result is None else result
    result_path = artifacts / "run-result.json"
    result_path.write_text(json.dumps(document), encoding="utf-8")
    return artifacts, result_path, document


def _render(tmp_path: Path, result: dict | None = None) -> tuple[Path, Path, dict]:
    artifacts, result_path, document = _write_canonical(tmp_path, result)
    renderer.render_artifacts(
        schema_path=SCHEMA,
        result_path=result_path,
        artifact_root=artifacts,
    )
    return artifacts, result_path, document


def test_renders_deterministic_views_and_exact_profile_documents(tmp_path):
    artifacts, result_path, result = _render(tmp_path)
    canonical_before = result_path.read_bytes()

    expected_files = {
        "run-result.json",
        "matrix.md",
        "latency.csv",
        "junit.xml",
        *(f"profiles/{profile_id}.json" for profile_id in renderer.PROFILE_IDS),
    }
    actual_files = {
        path.relative_to(artifacts).as_posix()
        for path in artifacts.rglob("*")
        if path.is_file()
    }
    assert actual_files == expected_files

    profiles_by_id = {profile["profile_id"]: profile for profile in result["profiles"]}
    for profile_id in renderer.PROFILE_IDS:
        derived = json.loads(
            (artifacts / "profiles" / f"{profile_id}.json").read_text(encoding="utf-8")
        )
        assert derived == profiles_by_id[profile_id]

    first_render = {
        relative: (artifacts / relative).read_bytes()
        for relative in expected_files
        if relative != "run-result.json"
    }
    renderer.render_artifacts(
        schema_path=SCHEMA,
        result_path=result_path,
        artifact_root=artifacts,
    )
    assert result_path.read_bytes() == canonical_before
    assert {
        relative: (artifacts / relative).read_bytes()
        for relative in expected_files
        if relative != "run-result.json"
    } == first_render


def test_matrix_and_latency_are_fixed_structured_projections(tmp_path):
    result = _valid_result()
    result["profiles"][0]["scenarios"][7]["request_ids"] = ["PRIVATE_SENTINEL"]
    artifacts, _, _ = _render(tmp_path, result)

    matrix = (artifacts / "matrix.md").read_text(encoding="utf-8")
    assert "PRIVATE_SENTINEL" not in matrix
    assert matrix.count("\n| official-") == 4
    assert matrix.count("\n| openrouter-") == 3
    assert matrix.count("\n| relay-") == 1
    for scenario_id in renderer.SCENARIO_IDS:
        assert scenario_id in matrix

    with (artifacts / "latency.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert set(rows[0]) == {
        "record_type",
        "profile_id",
        "scenario_id",
        "turn_index",
        "request_id",
        "turn_id",
        "trace_id",
        "metric",
        "stage",
        "value_ms",
    }
    assert "PRIVATE_SENTINEL" not in (artifacts / "latency.csv").read_text(
        encoding="utf-8"
    )
    assert rows[0]["request_id"]
    assert rows[0]["turn_id"]
    assert rows[0]["trace_id"]
    assert all(float(row["value_ms"]) >= 0 for row in rows)
    turn_stage_rows = [row for row in rows if row["record_type"] == "turn_stage"]
    expected_turn_count = sum(len(profile["turns"]) for profile in result["profiles"])
    assert len(turn_stage_rows) == expected_turn_count * len(
        renderer.REQUIRED_TRACE_STAGES
    )
    assert {row["stage"] for row in turn_stage_rows} == set(
        renderer.REQUIRED_TRACE_STAGES
    )
    assert all(row["metric"] == "stage_latency_ms" for row in turn_stage_rows)


def test_profile_artifact_preserves_bounded_persona_and_reasoning_receipts(tmp_path):
    artifacts, _, result = _render(tmp_path)
    profile = result["profiles"][0]
    rendered = json.loads(
        (artifacts / "profiles" / "official-deepseek.json").read_text(encoding="utf-8")
    )

    assert (
        rendered["scenarios"][5]["persona_finalizer"]
        == profile["scenarios"][5]["persona_finalizer"]
    )
    assert {
        field: rendered["reasoning"][field]
        for field in (
            "capability_enabled",
            "requested_effort",
            "configured_effort",
            "effective_effort",
            "reasoning_event_count",
            "request_id",
            "turn_id",
            "trace_id",
        )
    } == {
        field: profile["reasoning"][field]
        for field in (
            "capability_enabled",
            "requested_effort",
            "configured_effort",
            "effective_effort",
            "reasoning_event_count",
            "request_id",
            "turn_id",
            "trace_id",
        )
    }


def test_reasoning_configuration_mismatch_is_schema_valid_and_renderable(tmp_path):
    result = _valid_result()
    profile = result["profiles"][0]
    scenario = next(
        row for row in profile["scenarios"] if row["scenario_id"] == "P0-12"
    )
    failure = {
        "category": "PRODUCT_FAIL",
        "stage_code": "REASONING",
        "failure_code": "MODEL_ROUTE_MISMATCH",
        "reproducible": True,
    }
    profile["reasoning"].update(
        capability_enabled=False,
        requested_effort="medium",
        configured_effort="off",
        effective_effort="unknown",
        reasoning_event_count=0,
        metadata_present=False,
        token_metadata_present=False,
        user_visible_disclosure_present=False,
        kind=None,
        source=None,
        reasoning_token_count=0,
        disclosure_length=0,
    )
    scenario["status"] = "PRODUCT_FAIL"
    scenario["assertions"].update(
        reasoning_capability_enabled=False,
        reasoning_configured_effort_medium=False,
        reasoning_effective_effort_not_attested=True,
        reasoning_event_observed=False,
        reasoning_metadata_present=False,
        reasoning_tokens_present=False,
        user_disclosure_present=False,
    )
    scenario["attempt_results"] = [
        {"attempt": 1, "status": "PRODUCT_FAIL", "failure": failure}
    ]
    scenario["failure"] = failure
    profile["status"] = "PRODUCT_FAIL"
    result["overall_status"] = "PRODUCT_FAIL"
    result["summary"]["pass"] = 7
    result["summary"]["product_fail"] = 1

    artifacts, _, _ = _render(tmp_path, result)
    rendered = json.loads(
        (artifacts / "profiles" / "official-deepseek.json").read_text(encoding="utf-8")
    )

    assert rendered["reasoning"] == profile["reasoning"]
    assert rendered["reasoning"]["capability_enabled"] is False
    assert rendered["reasoning"]["effective_effort"] == "unknown"
    assert rendered["reasoning"]["reasoning_event_count"] == 0
    assert "MODEL_ROUTE_MISMATCH" in (artifacts / "junit.xml").read_text(
        encoding="utf-8"
    )


def test_junit_contains_only_fixed_attributes_and_no_output_or_error_bodies(tmp_path):
    result = _valid_result()
    scenario = result["profiles"][0]["scenarios"][7]
    scenario["status"] = "PRODUCT_FAIL"
    scenario["failure"] = {
        "category": "PRODUCT_FAIL",
        "stage_code": "BASIC_CHAT",
        "failure_code": "CHAT_TIMEOUT",
        "reproducible": True,
    }
    artifacts, _, _ = _render(tmp_path, result)

    root = ElementTree.parse(artifacts / "junit.xml").getroot()
    assert root.attrib == {
        "name": "io-e2e-agent-driven-test-p0",
        "tests": "104",
        "failures": "1",
        "errors": "0",
    }
    assert root.findall(".//system-out") == []
    assert root.findall(".//system-err") == []
    failures = root.findall(".//failure")
    assert len(failures) == 1
    assert failures[0].attrib == {
        "type": "PRODUCT_FAIL",
        "message": "BASIC_CHAT:CHAT_TIMEOUT",
    }
    assert failures[0].text is None
    assert all(error.text is None for error in root.findall(".//error"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda result: result["profiles"][0]["scenarios"][0].update(
            evidence_codes=["raw chat: tell me your secret"]
        ),
        lambda result: result["profiles"][0].update(
            diagnostic_codes=["raw trace response body"]
        ),
        lambda result: result["profiles"][0]["scenarios"][0].update(
            status="PRODUCT_FAIL",
            failure={
                "category": "PRODUCT_FAIL",
                "stage_code": "PREFLIGHT",
                "failure_code": "raw failure response body",
                "reproducible": True,
            },
        ),
    ],
)
def test_schema_rejects_free_form_evidence_diagnostic_and_failure_text(
    tmp_path, mutate
):
    result = _valid_result()
    mutate(result)
    artifacts, result_path, _ = _write_canonical(tmp_path, result)

    with pytest.raises(renderer.RenderInputError, match="does not satisfy"):
        renderer.render_artifacts(
            schema_path=SCHEMA,
            result_path=result_path,
            artifact_root=artifacts,
        )
    assert not (artifacts / "matrix.md").exists()


def test_cli_failure_does_not_echo_untrusted_result_text(tmp_path, capsys):
    result = _valid_result()
    raw = "raw-chat-that-must-never-be-printed"
    result["profiles"][0]["scenarios"][0]["evidence_codes"] = [raw]
    artifacts, result_path, _ = _write_canonical(tmp_path, result)

    rc = renderer.main(
        [
            "--schema",
            str(SCHEMA),
            "--result",
            str(result_path),
            "--artifacts",
            str(artifacts),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert raw not in captured.out + captured.err
    assert "artifact render: FAIL" in captured.err


def test_duplicate_profile_cannot_overwrite_a_profile_artifact(tmp_path):
    result = _valid_result()
    result["profiles"][-1] = deepcopy(result["profiles"][0])
    artifacts, result_path, _ = _write_canonical(tmp_path, result)

    with pytest.raises(renderer.RenderInputError, match="profile set is not exact"):
        renderer.render_artifacts(
            schema_path=SCHEMA,
            result_path=result_path,
            artifact_root=artifacts,
        )


def test_profiles_directory_symlink_is_rejected(tmp_path):
    artifacts, result_path, _ = _write_canonical(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, artifacts / "profiles")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    with pytest.raises(renderer.RenderInputError, match="unsafe"):
        renderer.render_artifacts(
            schema_path=SCHEMA,
            result_path=result_path,
            artifact_root=artifacts,
        )
    assert list(outside.iterdir()) == []
