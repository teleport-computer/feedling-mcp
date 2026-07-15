from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qa import publish_persona_memory_summary as publisher
from qa.regression.contracts import ExperimentResult


PRIVATE_PROMPT = "PRIVATE PROMPT MUST NEVER PUBLISH"
PRIVATE_REPLY = "PRIVATE REPLY MUST NEVER PUBLISH"
PRIVATE_RATIONALE = "PRIVATE JUDGE RATIONALE MUST NEVER PUBLISH"
PRIVATE_ACCOUNT_FINGERPRINT = "9" * 64


def _result() -> ExperimentResult:
    now = datetime.now(timezone.utc).isoformat()
    scenario_sha = "c" * 64
    rubric_sha = "e" * 64
    trajectory_id = "trajectory-private"
    common_metric = {
        "schema_version": 1,
        "kind": "metric_result",
        "metric_version": "v1",
        "experiment_id": "experiment-candidate",
        "target_id": "current-test-build",
        "trajectory_id": trajectory_id,
        "scenario_id": "persona-pressure",
        "threshold": 0.8,
        "hard_gate": True,
        "rubric_sha256": rubric_sha,
    }
    return ExperimentResult.from_dict(
        {
            "schema_version": 1,
            "kind": "experiment_result",
            "experiment_id": "experiment-candidate",
            "status": "FAIL",
            "started_at": now,
            "finished_at": now,
            "persona_fixture_sha256": "d" * 64,
            "rubric_sha256": rubric_sha,
            "scenario_fingerprints": {"persona-pressure": scenario_sha},
            "targets": [
                {
                    "schema_version": 1,
                    "kind": "experiment_target",
                    "target_id": "current-test-build",
                    "label": "candidate",
                    "base_url": "https://test-api.feedling.app",
                    "build_sha": "b" * 40,
                    "runtime_mode": "hosted_resident",
                    "provider": "openai",
                    "model": "gpt-5.6-terra",
                    "configuration": {
                        "reasoning_effort": "medium",
                        "private_configuration_note": PRIVATE_PROMPT,
                    },
                }
            ],
            "trajectories": [
                {
                    "schema_version": 1,
                    "kind": "trajectory",
                    "trajectory_id": trajectory_id,
                    "experiment_id": "experiment-candidate",
                    "target_id": "current-test-build",
                    "scenario_id": "persona-pressure",
                    "scenario_version": "v1",
                    "scenario_sha256": scenario_sha,
                    "repeat_index": 0,
                    "status": "COMPLETED",
                    "failure_code": "NONE",
                    "started_at": now,
                    "finished_at": now,
                    "turns": [
                        {
                            "schema_version": 1,
                            "kind": "turn_evidence",
                            "turn_id": "private-turn-id",
                            "turn_index": 1,
                            "role": "assistant",
                            "prompt": PRIVATE_PROMPT,
                            "response": PRIVATE_REPLY,
                            "session_key": "default",
                            "session_id": "private-session-id",
                            "session_generation": 0,
                            "boundary_before": "none",
                            "request_id": "private-request-id",
                            "response_id": "private-response-id",
                            "trace_id": "private-trace-id",
                            "latency_ms": 12.0,
                            "next_turn_id": None,
                            "metadata": {"private": PRIVATE_REPLY},
                        }
                    ],
                    "boundary_evidence": [],
                    "metadata": {"private": PRIVATE_PROMPT},
                }
            ],
            "metric_results": [
                {
                    **common_metric,
                    "metric_id": "persona.hard_constraints",
                    "evaluator_type": "DETERMINISTIC",
                    "status": "PASS",
                    "passed": True,
                    "score": 1.0,
                    "failure_codes": [],
                    "evidence": [{"private_turn_id": "private-turn-id"}],
                    "summary": PRIVATE_REPLY,
                    "metadata": {"private": PRIVATE_PROMPT},
                },
                {
                    **common_metric,
                    "metric_id": "persona.tone_consistency",
                    "evaluator_type": "LLM_JUDGE",
                    "status": "FAIL",
                    "passed": False,
                    "score": 0.4,
                    "failure_codes": [
                        "PERSONA_IDENTITY_DRIFT",
                        "MODEL_INVENTED_PRIVATE_CLASSIFIER",
                    ],
                    "evidence": [
                        {
                            "evidence_turn_ids": ["private-turn-id"],
                            "rationale": PRIVATE_RATIONALE,
                        }
                    ],
                    "summary": PRIVATE_RATIONALE,
                    "metadata": {
                        "judge_id": "codex-oauth-persona-memory-v1",
                        "judge_configuration_sha256": "4" * 64,
                    },
                },
            ],
            "summary": {"private_summary": PRIVATE_REPLY},
            "metadata": {
                "evaluation_contract_sha256": "f" * 64,
                "coverage_contract": {
                    "repetitions": 1,
                    "scenarios": {
                        "persona-pressure": {
                            "fingerprint_sha256": scenario_sha,
                            "metric_ids": [
                                "persona.hard_constraints",
                                "persona.tone_consistency",
                            ],
                        }
                    },
                },
                "account_fingerprints": [PRIVATE_ACCOUNT_FINGERPRINT],
                "private_metadata": PRIVATE_PROMPT,
            },
        }
    )


def _arm(result: ExperimentResult) -> dict:
    return {
        "result_status": result.status,
        "source_bundle_sha256": "1" * 64,
        "import_fixture_sha256": "2" * 64,
        "pool_manifest_sha256": "3" * 64,
        "route_sha256": "5" * 64,
        "readiness_receipt_sha256": "6" * 64,
        "cleanup_receipt_sha256": "7" * 64,
        "deployment_receipt_pre_sha256": "8" * 64,
        "deployment_receipt_post_sha256": "a" * 64,
        "account_fingerprints": [PRIVATE_ACCOUNT_FINGERPRINT],
    }


def test_public_projection_contains_metrics_but_no_private_evidence():
    result = _result()
    summary = publisher.build_public_summary(
        result,
        _arm(result),
        result_sha256="0" * 64,
        arm_receipt_sha256="b" * 64,
    )
    rendered = json.dumps(summary, sort_keys=True)
    markdown = publisher.render_markdown(summary)

    for forbidden in (
        PRIVATE_PROMPT,
        PRIVATE_REPLY,
        PRIVATE_RATIONALE,
        PRIVATE_ACCOUNT_FINGERPRINT,
        "private-turn-id",
        "private-session-id",
        "private-request-id",
        "private-response-id",
        "private-trace-id",
        "account_fingerprints",
        "prompt",
        "response",
        "rationale",
        "evidence_turn_ids",
    ):
        assert forbidden not in rendered
        assert forbidden not in markdown

    assert summary["status"] == "FAIL"
    assert summary["target"] == {
        "label": "candidate",
        "build_sha": "b" * 40,
        "runtime_mode": "hosted_resident",
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
    }
    assert summary["coverage"] == {
        "repetitions": 1,
        "scenario_count": 1,
        "trajectory_count": 1,
        "metric_result_count": 2,
    }
    assert summary["judge"] == {
        "judge_id": "codex-oauth-persona-memory-v1",
        "configuration_sha256": "4" * 64,
    }
    metrics = summary["scenarios"][0]["metrics"]
    assert [row["metric_id"] for row in metrics] == [
        "persona.hard_constraints",
        "persona.tone_consistency",
    ]
    assert metrics[0]["pass_count"] == 1
    assert metrics[0]["pass_rate"] == 1.0
    assert metrics[1]["failure_codes"] == [
        "PERSONA_IDENTITY_DRIFT",
        "UNRECOGNIZED_FAILURE_CODE",
    ]
    assert "MODEL_INVENTED_PRIVATE_CLASSIFIER" not in rendered
    assert "MODEL_INVENTED_PRIVATE_CLASSIFIER" not in markdown
    assert "| persona-pressure | persona.tone_consistency | LLM_JUDGE |" in markdown


def test_public_failure_code_projection_maps_invalid_values_to_fixed_code():
    assert publisher.public_failure_codes(
        ["PERSONA_IDENTITY_DRIFT", "bad private text", 42, None]
    ) == ["INVALID_FAILURE_CODE", "PERSONA_IDENTITY_DRIFT"]


def test_publish_writes_new_owner_only_files_and_refuses_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    result = _result()
    arm = _arm(result)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    artifacts.chmod(0o700)
    monkeypatch.setattr(
        publisher,
        "_load_private_result",
        lambda _path: (result, "0" * 64),
    )
    monkeypatch.setattr(
        publisher,
        "_verify_arm_receipt",
        lambda _path, _result, _digest: (arm, "b" * 64),
    )

    summary = publisher.publish(
        tmp_path / "private-result.json",
        tmp_path / "private-arm.json",
        artifacts,
    )

    assert summary["status"] == "FAIL"
    for filename in (publisher.JSON_FILENAME, publisher.MARKDOWN_FILENAME):
        path = artifacts / filename
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(publisher.SummaryPublishError, match="could not be created"):
        publisher.publish(
            tmp_path / "private-result.json",
            tmp_path / "private-arm.json",
            artifacts,
        )


def test_artifact_directory_must_be_owner_only(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o755)
    artifacts.chmod(0o755)

    with pytest.raises(publisher.SummaryPublishError, match="owner-only"):
        publisher.publish(
            tmp_path / "private-result.json",
            tmp_path / "private-arm.json",
            artifacts,
        )


def test_nonformal_skip_is_explicit_and_contains_no_claimed_coverage(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    artifacts.chmod(0o700)

    summary = publisher.publish_nonformal_skip(
        artifacts,
        build_sha="a" * 40,
        repetitions=3,
    )

    assert summary["status"] == "NOT_FORMALLY_QUALIFIED"
    assert summary["skip_reason"] == "STRICT_V2_USER_PATH_NOT_REQUIRED"
    assert summary["coverage"] == {
        "repetitions": 3,
        "scenario_count": 0,
        "trajectory_count": 0,
        "metric_result_count": 0,
    }
    assert summary["scenarios"] == []
    assert summary["judge"] is None
    assert "Not formally" not in (
        artifacts / publisher.MARKDOWN_FILENAME
    ).read_text()
    markdown = (artifacts / publisher.MARKDOWN_FILENAME).read_text()
    assert "NOT_FORMALLY_QUALIFIED" in markdown
    assert "user-path mode and version evidence" in markdown
    assert "worker identity" not in markdown
