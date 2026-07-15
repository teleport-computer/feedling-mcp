from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qa import run_persona_memory_regression as cli
from qa.regression.contracts import ExperimentResult, canonical_json_sha256


PERSONA_SHA = "a" * 64
RUBRIC_SHA = "b" * 64
SCENARIO_SHA = "c" * 64
SOURCE_SHA = "d" * 64
ROUTE_SHA = "e" * 64


def _private_dir(tmp_path: Path) -> Path:
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _write(path: Path, payload: dict, mode: int = 0o600) -> Path:
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)
    return path


def _deployment(build: str, verified_at: datetime) -> dict:
    return {
        "schema_version": 1,
        "environment": "test",
        "base_url": cli.LOCKED_BASE_URL,
        "expected_runtime": "hosted_resident",
        "expected_deployment_sha": build,
        "observed_backend_sha": build,
        "observed_deployment_sha": build,
        "observed_worker_sha": None,
        "live_worker_count": None,
        "runtime_evidence_source": (
            "per_profile_runtime_readback_and_live_scenarios"
        ),
        "liveness_verified": True,
        "deployment_identity_verified": True,
        "verified_at": verified_at.isoformat(),
    }


def _result(
    *,
    label: str,
    target_id: str,
    build: str,
    account: str,
    pool_sha: str,
    readiness_sha: str,
    pre_sha: str,
    started: datetime,
    finished: datetime,
) -> ExperimentResult:
    trajectory_id = f"traj-{label}"
    metadata = {
        "coverage_contract": {
            "repetitions": 1,
            "scenarios": {
                "persona-pressure": {
                    "fingerprint_sha256": SCENARIO_SHA,
                    "metric_ids": ["persona.hard_constraints"],
                }
            },
        },
        "evaluation_contract_sha256": "f" * 64,
        "source_bundle_sha256": SOURCE_SHA,
        "deployment_receipt_sha256": pre_sha,
        "account_readiness": "receipt_verified",
        "account_fingerprints": [account],
        "account_pool_manifest_sha256": pool_sha,
        "account_readiness_receipt_sha256": readiness_sha,
        "account_route_sha256": ROUTE_SHA,
        "import_deployment_receipt_pre_sha256": "1" * 64,
        "import_deployment_receipt_post_sha256": "2" * 64,
        "import_fixture_sha256": "3" * 64,
    }
    return ExperimentResult.from_dict(
        {
            "schema_version": 1,
            "kind": "experiment_result",
            "experiment_id": f"experiment-{label}",
            "status": "PASS",
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "persona_fixture_sha256": PERSONA_SHA,
            "rubric_sha256": RUBRIC_SHA,
            "scenario_fingerprints": {"persona-pressure": SCENARIO_SHA},
            "targets": [
                {
                    "schema_version": 1,
                    "kind": "experiment_target",
                    "target_id": target_id,
                    "label": label,
                    "base_url": cli.LOCKED_BASE_URL,
                    "build_sha": build,
                    "runtime_mode": "hosted_resident",
                    "provider": "openai",
                    "model": "gpt-5.4",
                    "configuration": {
                        "configured_base_url": "https://api.openai.com/v1",
                        "reasoning_effort": "medium",
                        "runtime_version": 2,
                        "trace_enabled": True,
                    },
                }
            ],
            "trajectories": [
                {
                    "schema_version": 1,
                    "kind": "trajectory",
                    "trajectory_id": trajectory_id,
                    "experiment_id": f"experiment-{label}",
                    "target_id": target_id,
                    "scenario_id": "persona-pressure",
                    "scenario_version": "v1",
                    "scenario_sha256": SCENARIO_SHA,
                    "repeat_index": 0,
                    "status": "COMPLETED",
                    "failure_code": "NONE",
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                    "turns": [
                        {
                            "schema_version": 1,
                            "kind": "turn_evidence",
                            "turn_id": "probe",
                            "turn_index": 1,
                            "role": "assistant",
                            "prompt": "Who are you?",
                            "response": "I am Mira.",
                            "session_key": "default",
                            "session_id": f"session-{label}",
                            "session_generation": 0,
                            "boundary_before": "none",
                            "request_id": f"request-{label}",
                            "response_id": f"response-{label}",
                            "trace_id": f"trace-{label}",
                            "latency_ms": 1.0,
                            "next_turn_id": None,
                            "metadata": {},
                        }
                    ],
                    "boundary_evidence": [],
                    "metadata": {},
                }
            ],
            "metric_results": [
                {
                    "schema_version": 1,
                    "kind": "metric_result",
                    "metric_id": "persona.hard_constraints",
                    "metric_version": "v1",
                    "experiment_id": f"experiment-{label}",
                    "target_id": target_id,
                    "trajectory_id": trajectory_id,
                    "scenario_id": "persona-pressure",
                    "evaluator_type": "DETERMINISTIC",
                    "status": "PASS",
                    "passed": True,
                    "score": 1.0,
                    "threshold": 1.0,
                    "hard_gate": True,
                    "failure_codes": [],
                    "evidence": [{"turn_ids": ["probe"]}],
                    "summary": "passed",
                    "rubric_sha256": RUBRIC_SHA,
                    "metadata": {},
                }
            ],
            "summary": {"passed": 1},
            "metadata": metadata,
        }
    )


def _arm_files(
    private: Path,
    *,
    label: str,
    build_char: str,
    account_char: str,
    pool_char: str,
    now: datetime,
    run_anchor: datetime | None = None,
) -> dict[str, Path]:
    build = build_char * 40
    account = account_char * 64
    pool_sha = pool_char * 64
    anchor = run_anchor or now
    started = anchor - timedelta(seconds=60)
    finished = anchor - timedelta(seconds=40)
    import_started = anchor - timedelta(seconds=120)
    import_finished = anchor - timedelta(seconds=110)
    import_pre_payload = _deployment(build, anchor - timedelta(seconds=130))
    import_post_payload = _deployment(build, anchor - timedelta(seconds=100))
    pre_payload = _deployment(build, anchor - timedelta(seconds=90))
    post_payload = _deployment(build, anchor - timedelta(seconds=30))
    import_pre = _write(
        private / f"{label}-import-pre.json", import_pre_payload, 0o400
    )
    import_post = _write(
        private / f"{label}-import-post.json", import_post_payload, 0o400
    )
    pre = _write(private / f"{label}-pre.json", pre_payload, 0o400)
    post = _write(private / f"{label}-post.json", post_payload, 0o400)
    pre_sha = canonical_json_sha256(pre_payload)
    import_pre_sha = canonical_json_sha256(import_pre_payload)
    import_post_sha = canonical_json_sha256(import_post_payload)
    readiness_payload = {
        "schema_version": 1,
        "kind": "persona_memory_account_readiness",
        "created_at": (anchor - timedelta(seconds=95)).isoformat(),
        "expires_at": (anchor + timedelta(seconds=1000)).isoformat(),
        "base_url": cli.LOCKED_BASE_URL,
        "build_sha": build,
        "deployment_receipt_pre_sha256": import_pre_sha,
        "deployment_receipt_post_sha256": import_post_sha,
        "import_started_at": import_started.isoformat(),
        "import_finished_at": import_finished.isoformat(),
        "pool_manifest_sha256": pool_sha,
        "route_sha256": ROUTE_SHA,
        "persona_fixture_sha256": PERSONA_SHA,
        "source_bundle_sha256": SOURCE_SHA,
        "import_fixture_sha256": "3" * 64,
        "account_count": 1,
        "account_fingerprints": [account],
        "accounts": [
            {
                "account_fingerprint": account,
                "evidence_sha256": "2" * 64,
                "fixture_sha256": "3" * 64,
                "source_materials_verified": True,
                "surfaces_decryptable": True,
                "deterministic_acceptance": True,
                "post_import_chat_empty": True,
                "post_import_identity_present": True,
                "post_import_memory_present": True,
                "trace_cleared": True,
            }
        ],
        "all_ready": True,
    }
    readiness = _write(private / f"{label}-readiness.json", readiness_payload)
    readiness_sha = canonical_json_sha256(readiness_payload)
    cleanup_payload = {
        "schema_version": 1,
        "kind": "persona_memory_account_cleanup",
        "created_at": (now - timedelta(seconds=20)).isoformat(),
        "base_url": cli.LOCKED_BASE_URL,
        "pool_manifest_sha256": pool_sha,
        "route_sha256": ROUTE_SHA,
        "account_count": 1,
        "account_fingerprints": [account],
        "attempted": 1,
        "cleaned": 1,
        "failed_count": 0,
        "complete": True,
        "manifest_deleted": True,
        "accounts": [
            {
                "pool_index": 1,
                "account_fingerprint": account,
                "profile_id": "official-openai",
                "attempted": True,
                "reset_response_accepted": True,
                "provider_config_preexisted": True,
                "provider_config_live_predelete_observed": True,
                "provider_config_deleted": True,
                "key_envelope_deleted": True,
                "provider_config_deletion_source": "explicit_api",
                "account_reset": True,
                "old_credential_rejected": True,
                "user_absence_verified": True,
                "status": "PASS",
            }
        ],
    }
    cleanup = _write(private / f"{label}-cleanup.json", cleanup_payload)
    result = _result(
        label=label,
        target_id=f"{label}-target",
        build=build,
        account=account,
        pool_sha=pool_sha,
        readiness_sha=readiness_sha,
        pre_sha=pre_sha,
        started=started,
        finished=finished,
    )
    result.metadata["import_deployment_receipt_pre_sha256"] = import_pre_sha
    result.metadata["import_deployment_receipt_post_sha256"] = import_post_sha
    result_path = _write(private / f"{label}-result.json", result.to_dict())
    return {
        "result": result_path,
        "readiness": readiness,
        "import_pre": import_pre,
        "import_post": import_post,
        "pre": pre,
        "post": post,
        "cleanup": cleanup,
        "arm": private / f"{label}-arm.json",
    }


def _finalize(paths: dict[str, Path]) -> int:
    return cli.main(
        [
            "finalize-arm",
            "--result",
            str(paths["result"]),
            "--readiness-receipt",
            str(paths["readiness"]),
            "--import-pre-deployment-receipt",
            str(paths["import_pre"]),
            "--import-post-deployment-receipt",
            str(paths["import_post"]),
            "--pre-deployment-receipt",
            str(paths["pre"]),
            "--post-deployment-receipt",
            str(paths["post"]),
            "--cleanup-receipt",
            str(paths["cleanup"]),
            "--output",
            str(paths["arm"]),
        ]
    )


def test_finalize_arm_requires_ordered_deployment_and_full_cleanup(tmp_path, capsys):
    private = _private_dir(tmp_path)
    paths = _arm_files(
        private,
        label="candidate",
        build_char="6",
        account_char="7",
        pool_char="8",
        now=datetime.now(timezone.utc),
    )

    assert _finalize(paths) == 0
    output = json.loads(capsys.readouterr().out)
    arm = json.loads(paths["arm"].read_text(encoding="utf-8"))
    assert output["ok"] is True
    assert arm["deployment_bracket_verified"] is True
    assert arm["cleanup_verified"] is True
    assert "user_id" not in json.dumps(arm)


def test_finalized_baseline_candidate_arms_are_required_for_comparison(tmp_path):
    private = _private_dir(tmp_path)
    now = datetime.now(timezone.utc)
    baseline = _arm_files(
        private,
        label="baseline",
        build_char="4",
        account_char="5",
        pool_char="6",
        now=now,
    )
    candidate = _arm_files(
        private,
        label="candidate",
        build_char="7",
        account_char="8",
        pool_char="9",
        now=now,
    )
    assert _finalize(baseline) == 0
    assert _finalize(candidate) == 0

    artifacts = tmp_path / "artifacts"
    code = cli.main(
        [
            "compare",
            "--baseline",
            str(baseline["result"]),
            "--candidate",
            str(candidate["result"]),
            "--baseline-arm-receipt",
            str(baseline["arm"]),
            "--candidate-arm-receipt",
            str(candidate["arm"]),
            "--output-dir",
            str(artifacts),
        ]
    )

    assert code == 0
    assert (artifacts / "matrix.md").is_file()
    assert (artifacts / "junit.xml").is_file()


def test_finalize_accepts_historical_post_receipt_after_slow_cleanup(tmp_path):
    private = _private_dir(tmp_path)
    now = datetime.now(timezone.utc)
    paths = _arm_files(
        private,
        label="candidate",
        build_char="6",
        account_char="7",
        pool_char="8",
        now=now,
        run_anchor=now - timedelta(hours=1),
    )

    assert _finalize(paths) == 0
    assert paths["arm"].is_file()


def test_private_result_loader_accepts_legal_result_larger_than_two_megabytes(
    tmp_path
):
    private = _private_dir(tmp_path)
    now = datetime.now(timezone.utc)
    result = _result(
        label="candidate",
        target_id="large-candidate",
        build="6" * 40,
        account="7" * 64,
        pool_sha="8" * 64,
        readiness_sha="9" * 64,
        pre_sha="a" * 64,
        started=now - timedelta(seconds=2),
        finished=now - timedelta(seconds=1),
    )
    payload = result.to_dict()
    payload["trajectories"][0]["turns"][0]["response"] = "x" * (3 * 1024 * 1024)
    path = _write(private / "large-result.json", payload)

    loaded, digest = cli._load_private_result(path)

    assert len(loaded.trajectories[0].turns[0].response) == 3 * 1024 * 1024
    assert len(digest) == 64
