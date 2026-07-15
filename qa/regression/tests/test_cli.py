from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from qa import run_persona_memory_regression as cli


BUILD_SHA = "a" * 40


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("PASS", 0),
        ("FAIL", 1),
        ("BLOCKED_EVIDENCE", 2),
        ("INFRA_ERROR", 2),
    ],
)
def test_gate_exit_codes_distinguish_product_failure_from_invalid_evidence(
    status, expected
):
    assert cli._gate_exit_code(status) == expected


def test_gate_exit_code_rejects_unknown_status():
    with pytest.raises(cli.CommandError, match="unknown status"):
        cli._gate_exit_code("MAYBE")


def _receipt(path: Path, *, verified_at: datetime | None = None) -> Path:
    payload = {
        "schema_version": 1,
        "environment": "test",
        "base_url": cli.LOCKED_BASE_URL,
        "expected_runtime": "deployed_current",
        "expected_deployment_sha": BUILD_SHA,
        "observed_backend_sha": BUILD_SHA,
        "observed_deployment_sha": BUILD_SHA,
        "observed_worker_sha": None,
        "live_worker_count": None,
        "runtime_evidence_source": "deployed_runtime_readback",
        "liveness_verified": True,
        "deployment_identity_verified": True,
        "verified_at": (verified_at or datetime.now(timezone.utc)).isoformat(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o400)
    return path


def test_validate_command_compiles_and_fingerprints_release_suite(capsys):
    assert cli.main(["validate"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["persona_id"] == "mira"
    assert len(output["scenario_fingerprints"]) == 7
    assert "learned-memory-after-rotation" not in output["scenario_fingerprints"]
    assert len(output["evaluation_contract_sha256"]) == 64


def test_manifest_spec_requires_an_absolute_profile_mapping(tmp_path):
    profile_id, path = cli._manifest_spec(f"eval-001={tmp_path / 'one.json'}")
    assert profile_id == "eval-001"
    assert path.is_absolute()

    with pytest.raises(argparse.ArgumentTypeError):
        cli._manifest_spec("eval-001=relative.json")
    with pytest.raises(argparse.ArgumentTypeError):
        cli._manifest_spec("missing-separator")


def test_deployment_receipt_binds_fresh_full_build_identity(tmp_path):
    path = _receipt(tmp_path / "deployment.json")

    receipt, digest = cli._deployment_receipt(
        path, expected_sha=BUILD_SHA, expected_runtime="deployed_current"
    )

    assert receipt["observed_backend_sha"] == BUILD_SHA
    assert len(digest) == 64

    stale = _receipt(
        tmp_path / "stale.json",
        verified_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    with pytest.raises(cli.CommandError, match="does not prove"):
        cli._deployment_receipt(
            stale, expected_sha=BUILD_SHA, expected_runtime="deployed_current"
        )


@pytest.mark.parametrize(
    ("expected_runtime", "runtime_evidence_source"),
    [
        (
            "hosted_resident",
            "per_profile_runtime_readback_and_live_scenarios",
        ),
        ("deployed_current", "deployed_runtime_readback"),
    ],
)
def test_deployment_receipt_binds_null_worker_fields_and_runtime_evidence_source(
    tmp_path, expected_runtime, runtime_evidence_source
):
    path = _receipt(tmp_path / f"{expected_runtime}.json")
    payload = json.loads(path.read_text())
    payload["expected_runtime"] = expected_runtime
    payload["runtime_evidence_source"] = runtime_evidence_source
    path.chmod(0o600)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o400)

    receipt, _digest = cli._deployment_receipt(
        path,
        expected_sha=BUILD_SHA,
        expected_runtime=expected_runtime,
    )
    assert receipt["observed_worker_sha"] is None
    assert receipt["live_worker_count"] is None

    for mutation in (
        {"observed_worker_sha": BUILD_SHA},
        {"live_worker_count": 1},
        {
            "runtime_evidence_source": (
                "deployed_runtime_readback"
                if expected_runtime == "hosted_resident"
                else "per_profile_runtime_readback_and_live_scenarios"
            )
        },
    ):
        mutated = dict(payload)
        mutated.update(mutation)
        invalid = tmp_path / f"invalid-{expected_runtime}-{len(mutation)}-{next(iter(mutation))}.json"
        invalid.write_text(json.dumps(mutated), encoding="utf-8")
        invalid.chmod(0o400)
        with pytest.raises(cli.CommandError, match="does not prove"):
            cli._deployment_receipt(
                invalid,
                expected_sha=BUILD_SHA,
                expected_runtime=expected_runtime,
            )


def test_codex_oauth_judge_cli_configuration_is_validated_before_live_mutation(
    monkeypatch, tmp_path
):
    observed = {}

    class FakeCodexJudge:
        def __init__(self, **kwargs):
            observed.update(kwargs)

    monkeypatch.setattr(cli, "CodexExecJudge", FakeCodexJudge)
    args = argparse.Namespace(
        codex_bin=tmp_path / "codex",
        codex_home=tmp_path / "codex-home",
        judge_work_root=tmp_path / "judge-work",
        allow_private_judge_egress=True,
        judge_id="persona-memory-judge-v1",
        judge_configuration_id="prompt-rubric-v1",
        judge_model="judge-model-v1",
        judge_timeout=30.0,
        judge_codex_profile="persona_memory_judge",
        judge_permission_profile="feedling-e2e-persona-memory-judge",
        judge_reasoning_effort="medium",
    )

    judge = cli._judge(args)

    assert isinstance(judge, FakeCodexJudge)
    assert observed == {
        "judge_id": "persona-memory-judge-v1",
        "codex_bin": tmp_path / "codex",
        "codex_home": tmp_path / "codex-home",
        "work_root": tmp_path / "judge-work",
        "model": "judge-model-v1",
        "reasoning_effort": "medium",
        "timeout_seconds": 30.0,
        "codex_profile": "persona_memory_judge",
        "permission_profile": "feedling-e2e-persona-memory-judge",
        "configuration_id": "prompt-rubric-v1",
    }

    args.judge_configuration_id = ""
    with pytest.raises(cli.CommandError, match="configuration id"):
        cli._judge(args)


def test_live_setup_errors_are_structured_and_do_not_traceback(capsys, tmp_path):
    missing_manifest = tmp_path / "missing.json"
    receipt = _receipt(tmp_path / "deployment.json")
    code = cli.main(
        [
            "run-live",
            "--manifest-profile",
            f"eval-001={missing_manifest}",
            "--target-id",
            "candidate-a",
            "--target-label",
            "candidate",
            "--build-sha",
            BUILD_SHA,
            "--deployment-receipt",
            str(receipt),
            "--output",
            str(tmp_path / "result.json"),
            "--accounts-ready",
            "--cleanup-account",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    error = json.loads(captured.err)
    assert error["ok"] is False
    assert "Traceback" not in captured.err


def test_live_cli_refuses_unimplemented_strong_rotation_before_accounts(capsys, tmp_path):
    receipt = _receipt(tmp_path / "deployment.json")
    code = cli.main(
        [
            "run-live",
            "--include-nightly",
            "--manifest-profile",
            f"eval-001={tmp_path / 'missing.json'}",
            "--target-id",
            "candidate-a",
            "--target-label",
            "candidate",
            "--build-sha",
            BUILD_SHA,
            "--deployment-receipt",
            str(receipt),
            "--output",
            str(tmp_path / "result.json"),
            "--accounts-ready",
            "--cleanup-account",
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert code == 2
    assert "runtime session rotator" in error["detail"]


def test_strict_account_pool_requires_machine_readiness_and_batch_cleanup(
    capsys, tmp_path
):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    receipt = _receipt(private / "deployment.json")

    code = cli.main(
        [
            "run-live",
            "--account-pool",
            str(private / "missing-pool.json"),
            "--target-id",
            "candidate-a",
            "--target-label",
            "candidate",
            "--build-sha",
            BUILD_SHA,
            "--deployment-receipt",
            str(receipt),
            "--output",
            str(private / "result.json"),
            "--external-cleanup-guaranteed",
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert code == 2
    assert "readiness-receipt" in error["detail"]


def test_formal_account_pool_rejects_backend_only_runtime_before_live_mutation(
    capsys, tmp_path, monkeypatch
):
    private = tmp_path / "private-runtime"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    monkeypatch.setattr(
        cli,
        "load_account_pool",
        lambda path: SimpleNamespace(
            deployment_runtime="deployed_current",
            rows=(),
        ),
    )

    code = cli.main(
        [
            "run-live",
            "--account-pool",
            str(private / "pool.json"),
            "--readiness-receipt",
            str(private / "readiness.json"),
            "--target-id",
            "candidate-a",
            "--target-label",
            "candidate",
            "--build-sha",
            BUILD_SHA,
            "--deployment-receipt",
            str(private / "deployment.json"),
            "--output",
            str(private / "result.json"),
            "--external-cleanup-guaranteed",
        ]
    )

    assert code == 2
    assert "per-account Hosted Runtime V2 readback" in json.loads(
        capsys.readouterr().err
    )["detail"]
