"""Mutation tests for report-only CI execution evidence.

These tests exercise the failure modes that the old workflow-text grep could
not distinguish: a filename in a comment, an all-skipped file, non-verbose
pytest output, and evidence from the wrong checkout SHA.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import ci_execution_evidence as evidence


HEAD_SHA = "a" * 40
PRODUCERS = ["python-tests=python-tests"]
CI_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _fixture_repo(tmp_path: Path, workflow_text: str, *test_names: str):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for name in test_names:
        (tests_dir / name).write_text("def test_placeholder(): pass\n", encoding="utf-8")
    workflow = tmp_path / "ci.yml"
    workflow.write_text(workflow_text, encoding="utf-8")
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    return workflow, tests_dir, baseline, evidence_dir


def _write_pytest_manifest(
    directory: Path,
    *,
    filename: str,
    head_sha: str = HEAD_SHA,
    job: str = "python-tests",
    **outcomes: int,
) -> None:
    counts = {key: 0 for key in evidence.OUTCOME_KEYS}
    counts.update(outcomes)
    evidence._atomic_write_json(
        directory / f"{filename.replace('/', '-')}.json",
        {
            "schema_version": evidence.SCHEMA_VERSION,
            "kind": "pytest",
            "head_sha": head_sha,
            "job": job,
            "session_exitstatus": 0,
            "files": {filename: counts},
        },
    )


def _report(paths, **kwargs):
    workflow, tests_dir, baseline, evidence_dir = paths
    return evidence.build_report(
        workflow=workflow,
        tests_dir=tests_dir,
        baseline=baseline,
        evidence_dir=evidence_dir,
        expected_head_sha=HEAD_SHA,
        producer_values=PRODUCERS,
        **kwargs,
    )


def _files_by_path(report: dict) -> dict[str, dict]:
    return {item["path"]: item for item in report["files"]}


def test_job_env_does_not_use_runner_context_before_a_runner_exists():
    """GitHub rejects the whole workflow before creating jobs in this shape."""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    invalid = []
    for job_name, job in workflow["jobs"].items():
        for env_name, value in (job.get("env") or {}).items():
            if "${{ runner." in str(value):
                invalid.append(f"{job_name}.env.{env_name}")

    assert invalid == [], (
        "runner context is unavailable in jobs.<job_id>.env; bind it inside a "
        f"step instead: {invalid}"
    )


def test_comment_only_filename_is_configured_but_unassigned_unknown(tmp_path):
    paths = _fixture_repo(
        tmp_path,
        """jobs:
  python-tests:
    steps:
      # historical mention: tests/test_comment_only.py
      - run: python -m pytest tests/test_real.py -q
""",
        "test_real.py",
        "test_comment_only.py",
    )
    _write_pytest_manifest(paths[3], filename="tests/test_real.py", passed=1)

    files = _files_by_path(_report(paths))

    assert files["tests/test_real.py"]["status"] == "executed"
    assert files["tests/test_comment_only.py"] == {
        "path": "tests/test_comment_only.py",
        "status": "unknown",
        "reason": "no_expected_producer_job",
        "expected_jobs": [],
        "observed_jobs": {},
    }


def test_all_skipped_has_distinct_report_only_status(tmp_path):
    paths = _fixture_repo(
        tmp_path,
        """jobs:
  python-tests:
    steps:
      - run: python -m pytest tests/test_skipped.py -q
""",
        "test_skipped.py",
    )
    _write_pytest_manifest(paths[3], filename="tests/test_skipped.py", skipped=3)

    report = _report(paths)

    assert report["summary"] == {
        "configured": 1,
        "executed": 0,
        "all_skipped": 1,
        "unknown": 0,
        "invalid_evidence": 0,
    }
    assert _files_by_path(report)["tests/test_skipped.py"]["status"] == "all_skipped"


def test_missing_one_expected_profile_is_unknown_not_all_skipped(tmp_path):
    paths = _fixture_repo(
        tmp_path,
        """jobs:
  runtime-v2-coverage:
    steps:
      - run: python -m pytest tests/test_matrix.py -q
""",
        "test_matrix.py",
    )
    _write_pytest_manifest(
        paths[3],
        filename="tests/test_matrix.py",
        job="runtime-v2-coverage/profile=0",
        skipped=1,
    )

    report = evidence.build_report(
        workflow=paths[0],
        tests_dir=paths[1],
        baseline=paths[2],
        evidence_dir=paths[3],
        expected_head_sha=HEAD_SHA,
        producer_values=[
            "runtime-v2-coverage=runtime-v2-coverage/profile=0",
            "runtime-v2-coverage=runtime-v2-coverage/profile=1",
        ],
    )

    item = _files_by_path(report)["tests/test_matrix.py"]
    assert item["status"] == "unknown"
    assert item["reason"] == "no_positive_evidence_in_expected_job"


def test_nonverbose_and_verbose_pytest_write_the_same_positive_manifest(tmp_path):
    (tmp_path / "conftest.py").write_text(
        "from tools.ci_execution_evidence import "
        "pytest_runtest_logreport, pytest_sessionfinish\n",
        encoding="utf-8",
    )
    (tmp_path / "test_sample.py").write_text(
        "def test_positive_call():\n    assert 2 + 2 == 4\n", encoding="utf-8"
    )
    repo_root = Path(__file__).resolve().parents[1]
    observed = []
    for verbosity in ("-q", "-v"):
        output_dir = tmp_path / verbosity.removeprefix("-")
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(repo_root),
                evidence.EVIDENCE_DIR_ENV: str(output_dir),
                evidence.HEAD_SHA_ENV: HEAD_SHA,
                evidence.JOB_ENV: "python-tests",
            }
        )
        subprocess.run(
            [sys.executable, "-m", "pytest", "test_sample.py", verbosity],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        manifests = list(output_dir.glob("*.json"))
        assert len(manifests) == 1
        observed.append(json.loads(manifests[0].read_text(encoding="utf-8")))

    assert observed[0]["files"] == observed[1]["files"]
    assert observed[0]["files"]["test_sample.py"]["passed"] == 1


def test_repo_test_collects_from_external_cwd_without_pythonpath(tmp_path):
    """The report-only observer must never become a conftest import gate."""
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(repo_root / "tests" / "test_pytest_coverage_ratchet.py"),
            "--collect-only",
            "-q",
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_the_exemption_list_only_ever_shrinks" in result.stdout


def test_observer_module_failure_cannot_break_collection(tmp_path):
    """Prove a reached non-ImportError mutation in the observer stays fail-open."""
    repo_root = Path(__file__).resolve().parents[1]
    shadow = tmp_path / "shadow"
    shadow_tools = shadow / "tools"
    shadow_tools.mkdir(parents=True)
    (shadow_tools / "__init__.py").write_text("", encoding="utf-8")
    mutation_marker = tmp_path / "observer-mutation-reached"
    (shadow_tools / "ci_execution_evidence.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(mutation_marker)!r}).touch()\n"
        "raise RuntimeError('injected observer module failure')\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(shadow)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(repo_root / "tests" / "test_pytest_coverage_ratchet.py"),
            "--collect-only",
            "-q",
        ],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert mutation_marker.exists(), "shadow observer mutation was not imported"
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_the_exemption_list_only_ever_shrinks" in result.stdout


def test_wrong_sha_is_invalid_and_gap_remains_unknown(tmp_path):
    paths = _fixture_repo(
        tmp_path,
        """jobs:
  python-tests:
    steps:
      - run: python -m pytest tests/test_wrong_sha.py -q
""",
        "test_wrong_sha.py",
    )
    _write_pytest_manifest(
        paths[3], filename="tests/test_wrong_sha.py", head_sha="b" * 40, passed=1
    )

    report = _report(paths)

    assert report["summary"]["invalid_evidence"] == 1
    item = _files_by_path(report)["tests/test_wrong_sha.py"]
    assert item["status"] == "unknown"
    assert item["reason"] == "no_positive_evidence_in_expected_job"


def test_positive_evidence_from_wrong_job_does_not_count(tmp_path):
    paths = _fixture_repo(
        tmp_path,
        """jobs:
  python-tests:
    steps:
      - run: python -m pytest tests/test_owned.py -q
""",
        "test_owned.py",
    )
    _write_pytest_manifest(
        paths[3], filename="tests/test_owned.py", job="some-other-job", passed=1
    )

    item = _files_by_path(_report(paths))["tests/test_owned.py"]

    assert item["status"] == "unknown"
    assert item["reason"] == "positive_evidence_only_from_unexpected_job"


def test_custom_runner_requires_explicit_attestation(tmp_path):
    paths = _fixture_repo(
        tmp_path,
        """jobs:
  python-tests:
    steps:
      - run: python tests/test_api.py --multi-tenant
""",
        "test_api.py",
    )
    evidence.write_attestation(
        output=paths[3] / "custom-runner.json",
        head_sha=HEAD_SHA,
        job="python-tests",
        runner="tests/test_api.py",
    )

    item = _files_by_path(_report(paths))["tests/test_api.py"]

    assert item["status"] == "executed"
    assert item["observed_jobs"]["python-tests"]["passed"] == 1
