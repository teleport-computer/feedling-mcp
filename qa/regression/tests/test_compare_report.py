from __future__ import annotations

import json
import stat
from xml.etree import ElementTree

import pytest

from qa.regression import compare, report


RUBRIC_SHA = "d" * 64
SCENARIO_SHA = "e" * 64
EVALUATION_CONTRACT_SHA = "f" * 64


def _metric(
    *,
    target: str,
    scenario: str = "persona-pressure",
    metric: str = "persona.hard_constraints",
    status: str = "PASS",
    score: float | None = 1.0,
    repeat: int = 0,
    hard_gate: bool = True,
    version: str = "1",
    rubric: str | None = RUBRIC_SHA,
    evaluator_type: str = "DETERMINISTIC",
    metadata=None,
    evidence=None,
    failure_codes=(),
):
    if evidence is None:
        evidence = [{"turn_ids": ["private-turn"], "detail": "private"}]
    return {
        "metric_id": metric,
        "metric_version": version,
        "experiment_id": "exp-1",
        "target_id": target,
        "trajectory_id": f"{target}-{scenario}-{repeat}",
        "_test_repeat_index": repeat,
        "scenario_id": scenario,
        "evaluator_type": evaluator_type,
        "status": status,
        "passed": status == "PASS" if status in ("PASS", "FAIL") else None,
        "score": score,
        "threshold": 0.8,
        "hard_gate": hard_gate,
        "failure_codes": list(failure_codes),
        "evidence": evidence,
        "rubric_sha256": rubric,
        "metadata": dict(metadata or {}),
    }


def _experiment(
    target: str,
    rows: list[dict],
    *,
    fixture_hash: str = "a" * 64,
    build_sha: str | None = None,
    label: str | None = None,
    runtime_mode: str = "deployed_current",
    provider: str = "openai",
    model: str = "gpt-test",
    configuration=None,
    scenario_ids=None,
    metadata=None,
):
    trajectories = {}
    for row in rows:
        trajectory_id = row["trajectory_id"]
        trajectories.setdefault(
            trajectory_id,
            {
                "trajectory_id": trajectory_id,
                "experiment_id": row["experiment_id"],
                "target_id": target,
                "scenario_id": row["scenario_id"],
                "scenario_sha256": SCENARIO_SHA,
                "repeat_index": row["_test_repeat_index"],
                "status": "COMPLETED",
                "failure_code": "NONE",
            },
        )
    selected_scenarios = set(scenario_ids or (row["scenario_id"] for row in rows))
    return {
        "experiment_id": "exp-1",
        "fixture_sha256": fixture_hash,
        "scenario_set_sha256": "b" * 64,
        "rubric_sha256": RUBRIC_SHA,
        "scenario_fingerprints": {
            scenario_id: SCENARIO_SHA for scenario_id in sorted(selected_scenarios)
        },
        "targets": [
            {
                "target_id": target,
                "label": label or ("baseline" if target == "baseline" else "candidate"),
                "base_url": f"https://{target}.example.test",
                "build_sha": build_sha
                or (("b" if target == "baseline" else "c") * 40),
                "runtime_mode": runtime_mode,
                "provider": provider,
                "model": model,
                "configuration": dict(
                    {"reasoning_effort": "low"}
                    if configuration is None
                    else configuration
                ),
            }
        ],
        "trajectories": list(trajectories.values()),
        "metric_results": rows,
        "metadata": {
            "evaluation_contract_sha256": EVALUATION_CONTRACT_SHA,
            **dict(metadata or {}),
        },
    }


def test_compare_classifies_parity_regression_improvement_and_existing_failure():
    baseline_rows = [
        _metric(target="baseline", scenario="parity", score=0.9),
        _metric(target="baseline", scenario="regressed", score=1.0),
        _metric(target="baseline", scenario="improved", status="FAIL", score=0.5),
        _metric(target="baseline", scenario="known", status="FAIL", score=0.5),
    ]
    candidate_rows = [
        _metric(target="candidate", scenario="parity", score=0.9),
        _metric(
            target="candidate",
            scenario="regressed",
            status="FAIL",
            score=0.5,
            failure_codes=["PERSONA_IDENTITY_DRIFT"],
        ),
        _metric(target="candidate", scenario="improved", score=1.0),
        _metric(target="candidate", scenario="known", status="FAIL", score=0.6),
    ]

    comparison = compare.compare_results(
        _experiment("baseline", baseline_rows),
        _experiment("candidate", candidate_rows),
    )

    assert comparison["status"] == "FAIL"
    assert {row["scenario_id"]: row["outcome"] for row in comparison["rows"]} == {
        "improved": "IMPROVEMENT",
        "known": "EXISTING_FAILURE",
        "parity": "PARITY",
        "regressed": "REGRESSION",
    }
    assert comparison["summary"] == {
        "metric_count": 4,
        "parity": 1,
        "regression": 1,
        "soft_regression": 0,
        "improvement": 1,
        "existing_failure": 1,
        "blocked_evidence": 0,
        "infra_error": 0,
    }


def test_hard_gate_requires_every_repeat_even_with_relaxed_global_pass_rate():
    baseline = [_metric(target="baseline", repeat=index) for index in range(3)]
    candidate = [
        _metric(
            target="candidate",
            repeat=index,
            status="FAIL" if index == 2 else "PASS",
            score=0.0 if index == 2 else 1.0,
        )
        for index in range(3)
    ]

    comparison = compare.compare_results(
        _experiment("baseline", baseline),
        _experiment("candidate", candidate),
        required_pass_rate=2 / 3,
        max_score_drop=1.0,
    )

    assert comparison["rows"][0]["outcome"] == "REGRESSION"
    assert comparison["rows"][0]["candidate"]["pass_rate"] == pytest.approx(2 / 3)


def test_soft_regression_is_a_warning_not_a_release_or_junit_failure():
    comparison = compare.compare_results(
        _experiment(
            "baseline",
            [_metric(target="baseline", score=1.0, hard_gate=False)],
        ),
        _experiment(
            "candidate",
            [_metric(target="candidate", score=0.9, hard_gate=False)],
        ),
    )

    assert comparison["status"] == "PASS"
    assert comparison["rows"][0]["outcome"] == "REGRESSION"
    assert comparison["rows"][0]["hard_gate"] is False
    assert comparison["summary"]["regression"] == 1
    assert comparison["summary"]["soft_regression"] == 1

    bundle = report.report_bundle(comparison)
    assert "SOFT_REGRESSION (warning)" in bundle["matrix.md"]
    root = ElementTree.fromstring(bundle["junit.xml"])
    assert root.attrib["tests"] == "1"
    assert root.attrib["failures"] == "0"
    assert root.attrib["errors"] == "0"
    assert root.findall(".//failure") == []
    assert root.findall(".//error") == []


@pytest.mark.parametrize(
    ("candidate_row", "outcome", "code"),
    [
        (
            _metric(target="candidate", status="INFRA_ERROR", score=None),
            "INFRA_ERROR",
            "CANDIDATE_INFRA_ERROR",
        ),
        (
            _metric(target="candidate", status="BLOCKED_EVIDENCE", score=None),
            "BLOCKED_EVIDENCE",
            "CANDIDATE_EVIDENCE_INVALID",
        ),
        (
            _metric(target="candidate", version="2"),
            "BLOCKED_EVIDENCE",
            "EVALUATION_CONTRACT_MISMATCH",
        ),
    ],
)
def test_compare_separates_product_regression_from_invalid_evidence(
    candidate_row, outcome, code
):
    comparison = compare.compare_results(
        _experiment("baseline", [_metric(target="baseline")]),
        _experiment("candidate", [candidate_row]),
    )

    assert comparison["rows"][0]["outcome"] == outcome
    assert code in comparison["rows"][0]["failure_codes"]


def test_compare_blocks_missing_metrics_repetition_mismatch_and_fixture_change():
    missing = compare.compare_results(
        _experiment(
            "baseline",
            [_metric(target="baseline", scenario="one")],
            scenario_ids={"one", "two"},
        ),
        _experiment(
            "candidate",
            [_metric(target="candidate", scenario="two")],
            scenario_ids={"one", "two"},
        ),
    )
    sample_mismatch = compare.compare_results(
        _experiment(
            "baseline",
            [_metric(target="baseline", repeat=0), _metric(target="baseline", repeat=1)],
        ),
        _experiment("candidate", [_metric(target="candidate", repeat=0)]),
    )
    hash_mismatch = compare.compare_results(
        _experiment("baseline", [_metric(target="baseline")], fixture_hash="a" * 64),
        _experiment("candidate", [_metric(target="candidate")], fixture_hash="c" * 64),
    )

    assert missing["status"] == "BLOCKED_EVIDENCE"
    assert {row["failure_codes"][0] for row in missing["rows"]} == {
        "BASELINE_METRIC_MISSING",
        "CANDIDATE_METRIC_MISSING",
    }
    assert "REPETITION_EVIDENCE_MISMATCH" in sample_mismatch["rows"][0][
        "failure_codes"
    ]
    assert hash_mismatch["contract_mismatches"] == ["fixture_sha256"]
    assert hash_mismatch["status"] == "BLOCKED_EVIDENCE"


def test_compare_experiment_selects_two_targets_from_one_result():
    baseline = _experiment("baseline", [_metric(target="baseline")])
    candidate = _experiment("candidate", [_metric(target="candidate")])
    experiment = dict(baseline)
    experiment["targets"] = baseline["targets"] + candidate["targets"]
    experiment["trajectories"] = baseline["trajectories"] + candidate["trajectories"]
    experiment["metric_results"] = baseline["metric_results"] + candidate[
        "metric_results"
    ]

    comparison = compare.compare_experiment(
        experiment,
        baseline_target_id="baseline",
        candidate_target_id="candidate",
    )

    assert comparison["status"] == "PASS"
    assert comparison["rows"][0]["outcome"] == "PARITY"


def test_real_coverage_results_require_disjoint_account_batches():
    coverage = {"repetitions": 1, "scenarios": {"persona-pressure": {}}}
    baseline = _experiment(
        "baseline",
        [_metric(target="baseline")],
        metadata={
            "coverage_contract": coverage,
            "account_fingerprints": ["d" * 64],
            "deployment_receipt_sha256": "1" * 64,
        },
    )
    candidate = _experiment(
        "candidate",
        [_metric(target="candidate")],
        metadata={
            "coverage_contract": coverage,
            "account_fingerprints": ["d" * 64],
            "deployment_receipt_sha256": "2" * 64,
        },
    )

    comparison = compare.compare_results(baseline, candidate)

    assert comparison["status"] == "BLOCKED_EVIDENCE"
    assert "account_batches.overlap" in comparison["contract_mismatches"]
    assert comparison["rows"][0]["outcome"] == "BLOCKED_EVIDENCE"


@pytest.mark.parametrize(
    ("baseline_receipt", "candidate_receipt", "mismatch"),
    [
        (None, "2" * 64, "deployment_receipts.missing"),
        ("1" * 64, None, "deployment_receipts.missing"),
        ("1" * 64, "1" * 64, "deployment_receipts.reused"),
    ],
)
def test_real_coverage_results_require_distinct_deployment_receipts(
    baseline_receipt, candidate_receipt, mismatch
):
    coverage = {"repetitions": 1, "scenarios": {"persona-pressure": {}}}
    common = {
        "coverage_contract": coverage,
        "account_fingerprints": ["d" * 64],
    }
    baseline_metadata = dict(common)
    candidate_metadata = {
        **common,
        "account_fingerprints": ["e" * 64],
    }
    if baseline_receipt is not None:
        baseline_metadata["deployment_receipt_sha256"] = baseline_receipt
    if candidate_receipt is not None:
        candidate_metadata["deployment_receipt_sha256"] = candidate_receipt

    comparison = compare.compare_results(
        _experiment(
            "baseline",
            [_metric(target="baseline")],
            metadata=baseline_metadata,
        ),
        _experiment(
            "candidate",
            [_metric(target="candidate")],
            metadata=candidate_metadata,
        ),
    )

    assert comparison["status"] == "BLOCKED_EVIDENCE"
    assert mismatch in comparison["contract_mismatches"]


def test_compare_rejects_same_target_and_blocks_same_build_self_comparison():
    with pytest.raises(compare.ComparisonError, match="must be distinct"):
        compare.compare_results(
            _experiment("baseline", [_metric(target="baseline")]),
            _experiment(
                "baseline",
                [_metric(target="baseline")],
                label="candidate",
                build_sha="candidate-build",
            ),
        )

    comparison = compare.compare_results(
        _experiment(
            "baseline", [_metric(target="baseline")], build_sha="a" * 40
        ),
        _experiment(
            "candidate", [_metric(target="candidate")], build_sha="a" * 40
        ),
    )

    assert comparison["status"] == "BLOCKED_EVIDENCE"
    assert comparison["contract_mismatches"] == ["self_comparison.build_sha"]
    assert comparison["rows"][0]["outcome"] == "BLOCKED_EVIDENCE"
    assert "COMPARISON_CONTRACT_MISMATCH" in comparison["rows"][0][
        "failure_codes"
    ]


def test_raw_mapping_with_noncanonical_build_identity_is_blocked():
    baseline = _experiment(
        "baseline",
        [_metric(target="baseline")],
        build_sha="baseline-secret",
    )
    candidate = _experiment(
        "candidate",
        [_metric(target="candidate")],
        build_sha="candidate-secret",
    )

    comparison = compare.compare_results(baseline, candidate)

    assert comparison["status"] == "BLOCKED_EVIDENCE"
    assert {
        "baseline_target.build_sha",
        "candidate_target.build_sha",
    }.issubset(comparison["contract_mismatches"])
    markdown = report.render_markdown_matrix(comparison)
    assert "baseline-secret" not in markdown
    assert "candidate-secret" not in markdown
    assert "Baseline build: `missing`" in markdown


@pytest.mark.parametrize(
    ("candidate_kwargs", "mismatch"),
    [
        ({"label": "baseline"}, "candidate_target.label"),
        ({"runtime_mode": "different"}, "target.runtime_mode"),
        ({"provider": "different"}, "target.provider"),
        ({"model": "different"}, "target.model"),
        ({"configuration": {"reasoning_effort": "high"}}, "target.configuration"),
    ],
)
def test_target_descriptor_changes_block_instead_of_reporting_parity(
    candidate_kwargs, mismatch
):
    comparison = compare.compare_results(
        _experiment("baseline", [_metric(target="baseline")]),
        _experiment("candidate", [_metric(target="candidate")], **candidate_kwargs),
    )

    assert mismatch in comparison["contract_mismatches"]
    assert comparison["status"] == "BLOCKED_EVIDENCE"
    assert comparison["rows"][0]["outcome"] == "BLOCKED_EVIDENCE"


def test_missing_selected_target_descriptor_blocks_comparison():
    candidate = _experiment("candidate", [_metric(target="candidate")])
    candidate["targets"] = []

    comparison = compare.compare_results(
        _experiment("baseline", [_metric(target="baseline")]), candidate
    )

    assert comparison["contract_mismatches"] == ["candidate_target.descriptor"]
    assert comparison["rows"][0]["outcome"] == "BLOCKED_EVIDENCE"


def _judge_metric(*, target: str, judge_id: str, temperature: float = 0.0):
    configuration_sha256 = ("2" if temperature == 0.0 else "3") * 64
    return _metric(
        target=target,
        evaluator_type="LLM_JUDGE",
        metadata={
            "judge_id": judge_id,
            "judge_configuration_sha256": configuration_sha256,
        },
        evidence=[
            {
                "judge_id": judge_id,
                "judge_configuration_sha256": configuration_sha256,
                "evidence_sha256": "1" * 64,
                "evidence_turn_ids": ["private-turn"],
            }
        ],
    )


def test_judge_identity_and_complete_config_metadata_are_comparison_bound():
    judge_change = compare.compare_results(
        _experiment(
            "baseline", [_judge_metric(target="baseline", judge_id="judge-v1")]
        ),
        _experiment(
            "candidate", [_judge_metric(target="candidate", judge_id="judge-v2")]
        ),
    )
    config_change = compare.compare_results(
        _experiment(
            "baseline", [_judge_metric(target="baseline", judge_id="judge-v1")]
        ),
        _experiment(
            "candidate",
            [
                _judge_metric(
                    target="candidate", judge_id="judge-v1", temperature=0.2
                )
            ],
        ),
    )

    for comparison in (judge_change, config_change):
        assert comparison["status"] == "BLOCKED_EVIDENCE"
        assert comparison["rows"][0]["outcome"] == "BLOCKED_EVIDENCE"
        assert "EVALUATION_CONTRACT_MISMATCH" in comparison["rows"][0][
            "failure_codes"
        ]


def test_judge_config_digest_is_required_and_must_match_judge_evidence():
    missing_config = _judge_metric(target="candidate", judge_id="judge-v1")
    missing_config["metadata"].pop("judge_configuration_sha256")
    mismatched_evidence = _judge_metric(target="candidate", judge_id="judge-v1")
    mismatched_evidence["evidence"][0]["judge_configuration_sha256"] = "9" * 64

    for candidate_row, code in (
        (missing_config, "JUDGE_METADATA_INVALID"),
        (mismatched_evidence, "JUDGE_EVIDENCE_INVALID"),
    ):
        comparison = compare.compare_results(
            _experiment(
                "baseline", [_judge_metric(target="baseline", judge_id="judge-v1")]
            ),
            _experiment("candidate", [candidate_row]),
        )
        assert comparison["status"] == "BLOCKED_EVIDENCE"
        assert code in comparison["rows"][0]["failure_codes"]


def test_result_level_judge_metadata_is_bound_without_publishing_its_value():
    baseline = _experiment(
        "baseline",
        [_metric(target="baseline")],
        metadata={"judge_configuration": {"model": "judge-v1", "temperature": 0}},
    )
    candidate = _experiment(
        "candidate",
        [_metric(target="candidate")],
        metadata={"judge_configuration": {"model": "judge-v2", "temperature": 0}},
    )

    comparison = compare.compare_results(baseline, candidate)

    assert comparison["contract_mismatches"] == ["judge.metadata"]
    assert "judge-v1" not in json.dumps(comparison)
    assert "judge-v2" not in json.dumps(comparison)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing", "METRIC_EVIDENCE_MISSING"),
        ("duplicate", "METRIC_EVIDENCE_DUPLICATE"),
        ("trajectory", "METRIC_TRAJECTORY_BINDING_INVALID"),
    ],
)
def test_missing_duplicate_or_unbound_metric_evidence_fails_closed(mutation, code):
    candidate = _experiment("candidate", [_metric(target="candidate")])
    if mutation == "missing":
        candidate["metric_results"][0]["evidence"] = []
    elif mutation == "duplicate":
        evidence = candidate["metric_results"][0]["evidence"][0]
        candidate["metric_results"][0]["evidence"] = [evidence, dict(evidence)]
    else:
        candidate["metric_results"][0]["trajectory_id"] = "missing-trajectory"

    comparison = compare.compare_results(
        _experiment("baseline", [_metric(target="baseline")]), candidate
    )

    assert comparison["status"] == "BLOCKED_EVIDENCE"
    assert code in comparison["rows"][0]["failure_codes"]
    assert comparison["rows"][0]["outcome"] == "BLOCKED_EVIDENCE"


def test_duplicate_metric_for_one_trajectory_cannot_impersonate_two_repeats():
    candidate_rows = [
        _metric(target="candidate", repeat=0),
        _metric(target="candidate", repeat=1),
    ]
    candidate_rows[1]["trajectory_id"] = candidate_rows[0]["trajectory_id"]
    comparison = compare.compare_results(
        _experiment(
            "baseline",
            [_metric(target="baseline", repeat=0), _metric(target="baseline", repeat=1)],
        ),
        _experiment("candidate", candidate_rows),
    )

    assert "METRIC_TRAJECTORY_DUPLICATE" in comparison["rows"][0][
        "failure_codes"
    ]
    assert comparison["rows"][0]["outcome"] == "BLOCKED_EVIDENCE"


def test_equal_sample_counts_still_require_the_exact_repeat_set():
    comparison = compare.compare_results(
        _experiment(
            "baseline",
            [_metric(target="baseline", repeat=0), _metric(target="baseline", repeat=1)],
        ),
        _experiment(
            "candidate",
            [_metric(target="candidate", repeat=0), _metric(target="candidate", repeat=2)],
        ),
    )

    assert comparison["rows"][0]["failure_codes"] == [
        "REPETITION_EVIDENCE_MISMATCH"
    ]
    assert comparison["rows"][0]["outcome"] == "BLOCKED_EVIDENCE"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("scenario_sha256", "9" * 64, "TRAJECTORY_SCENARIO_BINDING_INVALID"),
        ("status", "INFRA_ERROR", "METRIC_TRAJECTORY_BINDING_INVALID"),
        ("failure_code", "PRODUCT_FAILURE", "TRAJECTORY_STATUS_INVALID"),
    ],
)
def test_metric_cannot_claim_pass_on_unbound_or_failed_trajectory(field, value, code):
    candidate = _experiment("candidate", [_metric(target="candidate")])
    candidate["trajectories"][0][field] = value

    comparison = compare.compare_results(
        _experiment("baseline", [_metric(target="baseline")]), candidate
    )

    assert comparison["status"] == "BLOCKED_EVIDENCE"
    assert code in comparison["rows"][0]["failure_codes"]


def test_report_bundle_is_deterministic_and_does_not_publish_evidence():
    canary = "QA-PRIVATE-CANARY-SECRET"
    baseline = [_metric(target="baseline")]
    candidate = [
        _metric(
            target="candidate",
            status="FAIL",
            score=0.0,
            failure_codes=["PRIVACY_CANARY_LEAK"],
        )
    ]
    candidate[0]["evidence"] = [{"detail": canary, "turn_ids": ["turn-private"]}]
    comparison = compare.compare_results(
        _experiment("baseline", baseline),
        _experiment("candidate", candidate),
    )
    comparison["private_debug"] = canary

    bundle = report.report_bundle(comparison)

    assert set(bundle) == {"run-result.json", "matrix.md", "junit.xml"}
    expected_public = dict(comparison)
    expected_public.pop("private_debug")
    assert json.loads(bundle["run-result.json"]) == expected_public
    assert canary not in "".join(bundle.values())
    assert "turn-private" not in "".join(bundle.values())
    assert "PRIVACY_CANARY_LEAK" in bundle["matrix.md"]
    assert "REGRESSION" in bundle["matrix.md"]

    root = ElementTree.fromstring(bundle["junit.xml"])
    assert root.attrib == {
        "name": "feedling-persona-memory-regression",
        "tests": "1",
        "failures": "1",
        "errors": "0",
    }
    failure = root.find(".//failure")
    assert failure is not None
    assert failure.attrib == {
        "type": "REGRESSION",
        "message": "CANDIDATE_REGRESSION,PRIVACY_CANARY_LEAK",
    }
    assert failure.text is None
    assert root.findall(".//system-out") == []
    assert root.findall(".//system-err") == []


def test_all_public_reports_include_validated_build_and_deployment_identity():
    comparison = compare.compare_results(
        _experiment(
            "baseline",
            [_metric(target="baseline")],
            metadata={"deployment_receipt_sha256": "1" * 64},
        ),
        _experiment(
            "candidate",
            [_metric(target="candidate")],
            metadata={"deployment_receipt_sha256": "2" * 64},
        ),
    )

    bundle = report.report_bundle(comparison)
    for expected in ("b" * 40, "c" * 40, "1" * 64, "2" * 64):
        assert all(expected in artifact for artifact in bundle.values())


def test_every_public_report_projection_redacts_nested_private_values_and_codes():
    canary = "QA-NESTED-PRIVATE-CANARY"
    comparison = compare.compare_results(
        _experiment("baseline", [_metric(target="baseline")]),
        _experiment("candidate", [_metric(target="candidate")]),
    )
    comparison["policy"]["private"] = {"turn_id": canary}
    comparison["summary"]["private"] = [{"response_id": canary}]
    comparison["rows"][0]["candidate"]["metadata"] = {
        "session_id": canary,
    }
    comparison["rows"][0]["failure_codes"] = ["PRIVATE_CANARY_ENCODED"]

    bundle = report.report_bundle(comparison)
    rendered = "".join(bundle.values())

    assert canary not in rendered
    assert "PRIVATE_CANARY_ENCODED" not in rendered
    assert "UNRECOGNIZED_FAILURE_CODE" in rendered
    assert set(json.loads(bundle["run-result.json"])["policy"]) == {
        "required_pass_rate",
        "max_score_drop",
    }


def test_junit_marks_evidence_failures_as_errors_and_contract_mismatch_as_case():
    comparison = compare.compare_results(
        _experiment("baseline", [_metric(target="baseline")], fixture_hash="a" * 64),
        _experiment(
            "candidate",
            [
                _metric(
                    target="candidate", status="BLOCKED_EVIDENCE", score=None
                )
            ],
            fixture_hash="c" * 64,
        ),
    )

    root = ElementTree.fromstring(report.render_junit_xml(comparison))

    assert root.attrib["tests"] == "2"
    assert root.attrib["failures"] == "0"
    assert root.attrib["errors"] == "2"
    assert len(root.findall(".//error")) == 2


def test_write_reports_uses_private_regular_files(tmp_path):
    comparison = compare.compare_results(
        _experiment("baseline", [_metric(target="baseline")]),
        _experiment("candidate", [_metric(target="candidate")]),
    )
    artifact_root = tmp_path / "regression"

    report.write_reports(comparison, artifact_root)

    assert {path.name for path in artifact_root.iterdir()} == {
        "run-result.json",
        "matrix.md",
        "junit.xml",
    }
    for path in artifact_root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads((artifact_root / "run-result.json").read_text()) == comparison


def test_invalid_comparison_policy_is_rejected():
    with pytest.raises(compare.ComparisonError):
        compare.compare_results(
            [_metric(target="baseline")],
            _experiment("candidate", [_metric(target="candidate")]),
            required_pass_rate=1.1,
        )
