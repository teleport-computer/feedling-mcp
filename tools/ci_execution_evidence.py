"""Report-only CI evidence for which pytest files actually ran assertions.

Pytest calls the two hook functions through ``tests/conftest.py`` only when the
CI evidence environment is configured.  Each pytest process writes one atomic
JSON manifest.  A downstream job combines those manifests with an explicit
custom-runner attestation and compares them with the existing static coverage
set.  Missing or invalid evidence is reported as unknown; Phase A never turns
that absence into a hard failure.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid


SCHEMA_VERSION = 1
EVIDENCE_DIR_ENV = "FEEDLING_CI_EXECUTION_EVIDENCE_DIR"
HEAD_SHA_ENV = "FEEDLING_CI_EXECUTION_HEAD_SHA"
JOB_ENV = "FEEDLING_CI_EXECUTION_JOB"
TEST_PATH_RE = re.compile(r"tests/test_[A-Za-z0-9_]+\.py")
CONSUMER_RE = re.compile(
    r"tools\.chat_resident_consumer|chat_resident_consumer as|"
    r"import chat_resident_consumer|from tools import chat_resident_consumer|"
    r"chat_resident_consumer\.py"
)
OUTCOME_KEYS = ("passed", "failed", "skipped", "xfailed", "xpassed", "error")

_pytest_outcomes: dict[str, dict[str, int]] = {}


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        tmp_path = Path(handle.name)
    os.replace(tmp_path, path)


def _enabled_pytest_evidence() -> tuple[Path, str, str] | None:
    raw_dir = os.environ.get(EVIDENCE_DIR_ENV, "").strip()
    head_sha = os.environ.get(HEAD_SHA_ENV, "").strip()
    job = os.environ.get(JOB_ENV, "").strip()
    if not raw_dir or not head_sha or not job:
        return None
    return Path(raw_dir), head_sha, job


def _nodeid_file(nodeid: str) -> str | None:
    path = str(nodeid).split("::", 1)[0].replace("\\", "/")
    if not path.endswith(".py"):
        return None
    return path.removeprefix("./")


def pytest_runtest_logreport(report) -> None:
    """Collect call outcomes without depending on verbose terminal output."""
    if _enabled_pytest_evidence() is None:
        return
    path = _nodeid_file(report.nodeid)
    if path is None:
        return

    outcome = None
    was_xfail = bool(getattr(report, "wasxfail", False))
    if report.when == "call":
        if was_xfail and report.skipped:
            outcome = "xfailed"
        elif was_xfail and report.passed:
            outcome = "xpassed"
        elif report.passed:
            outcome = "passed"
        elif report.failed:
            outcome = "failed"
        elif report.skipped:
            outcome = "skipped"
    elif report.skipped:
        outcome = "skipped"
    elif report.failed:
        outcome = "error"

    if outcome is None:
        return
    counts = _pytest_outcomes.setdefault(path, {key: 0 for key in OUTCOME_KEYS})
    counts[outcome] += 1


def pytest_sessionfinish(session, exitstatus) -> None:
    """Write one fail-open manifest for this pytest process."""
    config = _enabled_pytest_evidence()
    if config is None:
        return
    directory, head_sha, job = config
    try:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": "pytest",
            "head_sha": head_sha,
            "job": job,
            "session_exitstatus": int(exitstatus),
            "files": dict(sorted(_pytest_outcomes.items())),
        }
        safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "-", job).strip("-") or "job"
        output = directory / f"pytest-{safe_job}-{uuid.uuid4().hex}.json"
        _atomic_write_json(output, payload)
    except Exception as exc:  # noqa: BLE001 - Phase A evidence must not fail pytest
        terminal = getattr(session.config, "pluginmanager", None)
        warning = f"CI execution evidence write failed: {type(exc).__name__}: {exc}"
        if terminal is not None:
            reporter = terminal.get_plugin("terminalreporter")
            if reporter is not None:
                reporter.write_line(warning, yellow=True)


def write_attestation(*, output: Path, head_sha: str, job: str, runner: str) -> None:
    _atomic_write_json(
        output,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "custom_runner",
            "head_sha": head_sha,
            "job": job,
            "runner": runner,
            "outcome": "passed",
        },
    )


def _read_baseline(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _consumer_tests(tests_dir: Path) -> set[str]:
    root = tests_dir.parent
    found = set()
    for path in tests_dir.glob("test_*.py"):
        if CONSUMER_RE.search(path.read_text(encoding="utf-8")):
            found.add(path.relative_to(root).as_posix())
    return found


def _workflow_job_block(workflow_text: str, job_name: str) -> str:
    lines = workflow_text.splitlines()
    start = None
    job_line = re.compile(rf"^  {re.escape(job_name)}:\s*$")
    next_job = re.compile(r"^  [A-Za-z0-9_-]+:\s*$")
    for index, line in enumerate(lines):
        if job_line.match(line):
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if next_job.match(lines[index]):
            end = index
            break
    executable_lines = []
    for line in lines[start:end]:
        # Current workflow shell snippets do not use literal # inside test
        # arguments.  Removing both YAML/shell full-line and inline comments is
        # what keeps a comment-only filename out of producer ownership.
        executable_lines.append(line.split("#", 1)[0])
    return "\n".join(executable_lines)


def _parse_producers(values: list[str]) -> dict[str, set[str]]:
    producers: dict[str, set[str]] = defaultdict(set)
    for value in values:
        workflow_job, separator, evidence_job = value.partition("=")
        if not separator or not workflow_job or not evidence_job:
            raise ValueError(f"invalid producer mapping: {value!r}")
        producers[workflow_job].add(evidence_job)
    return producers


def discover_configuration(
    *,
    workflow: Path,
    tests_dir: Path,
    baseline: Path,
    producer_mappings: dict[str, set[str]],
) -> tuple[set[str], dict[str, set[str]]]:
    workflow_text = workflow.read_text(encoding="utf-8")
    root = tests_dir.parent
    all_tests = {
        path.relative_to(root).as_posix() for path in tests_dir.glob("test_*.py")
    }
    consumers = _consumer_tests(tests_dir)
    configured = (set(TEST_PATH_RE.findall(workflow_text)) | consumers) & all_tests
    configured -= _read_baseline(baseline)

    owners: dict[str, set[str]] = defaultdict(set)
    for workflow_job, evidence_jobs in producer_mappings.items():
        paths = set(TEST_PATH_RE.findall(_workflow_job_block(workflow_text, workflow_job)))
        if workflow_job == "python-tests":
            paths |= consumers
        for path in paths & all_tests:
            owners[path].update(evidence_jobs)
    return configured, owners


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key in OUTCOME_KEYS}


def _load_evidence(
    evidence_dir: Path, expected_head_sha: str
) -> tuple[dict[str, dict[str, dict[str, int]]], list[dict]]:
    observed: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    invalid = []
    for path in sorted(evidence_dir.rglob("*.json")) if evidence_dir.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported_schema")
            if payload.get("head_sha") != expected_head_sha:
                raise ValueError("wrong_head_sha")
            job = str(payload.get("job") or "")
            if not job:
                raise ValueError("missing_job")
            kind = payload.get("kind")
            if kind == "pytest":
                for test_path, raw_counts in payload.get("files", {}).items():
                    counts = observed[test_path].setdefault(job, _empty_counts())
                    for key in OUTCOME_KEYS:
                        counts[key] += int(raw_counts.get(key, 0))
            elif kind == "custom_runner":
                if payload.get("outcome") != "passed":
                    raise ValueError("custom_runner_not_passed")
                runner = str(payload.get("runner") or "")
                if not runner:
                    raise ValueError("missing_runner")
                counts = observed[runner].setdefault(job, _empty_counts())
                counts["passed"] += 1
            else:
                raise ValueError("unsupported_kind")
        except Exception as exc:  # noqa: BLE001 - invalid evidence stays report-only
            invalid.append({"path": str(path), "reason": str(exc)})
    return observed, invalid


def build_report(
    *,
    workflow: Path,
    tests_dir: Path,
    baseline: Path,
    evidence_dir: Path,
    expected_head_sha: str,
    producer_values: list[str],
    producer_results: dict[str, str] | None = None,
) -> dict:
    producers = _parse_producers(producer_values)
    configured, owners = discover_configuration(
        workflow=workflow,
        tests_dir=tests_dir,
        baseline=baseline,
        producer_mappings=producers,
    )
    observed, invalid = _load_evidence(evidence_dir, expected_head_sha)
    files = []
    counts = defaultdict(int)
    for path in sorted(configured):
        expected_jobs = owners.get(path, set())
        by_job = observed.get(path, {})
        positive_jobs = {
            job
            for job, outcomes in by_job.items()
            if outcomes["passed"] + outcomes["failed"] > 0
        }
        expected_positive = positive_jobs & expected_jobs
        expected_observed = set(by_job) & expected_jobs
        if expected_positive:
            status = "executed"
            reason = "positive_passed_or_failed_in_expected_job"
        elif expected_jobs and expected_observed == expected_jobs and all(
            by_job[job]["skipped"] + by_job[job]["xfailed"] > 0
            and by_job[job]["error"] + by_job[job]["xpassed"] == 0
            for job in expected_observed
        ):
            status = "all_skipped"
            reason = "observed_only_skipped_or_xfailed_in_expected_job"
        else:
            status = "unknown"
            if not expected_jobs:
                reason = "no_expected_producer_job"
            elif positive_jobs:
                reason = "positive_evidence_only_from_unexpected_job"
            else:
                reason = "no_positive_evidence_in_expected_job"
        counts[status] += 1
        files.append(
            {
                "path": path,
                "status": status,
                "reason": reason,
                "expected_jobs": sorted(expected_jobs),
                "observed_jobs": {
                    job: by_job[job] for job in sorted(by_job)
                },
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "report_only",
        "expected_head_sha": expected_head_sha,
        "producer_results": producer_results or {},
        "summary": {
            "configured": len(configured),
            "executed": counts["executed"],
            "all_skipped": counts["all_skipped"],
            "unknown": counts["unknown"],
            "invalid_evidence": len(invalid),
        },
        "invalid_evidence": invalid,
        "files": files,
    }


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# CI execution evidence (report-only)",
        "",
        f"Exact checkout SHA: `{report['expected_head_sha']}`",
        "",
        "| configured | executed | all skipped | unknown | invalid evidence |",
        "|---:|---:|---:|---:|---:|",
        "| {configured} | {executed} | {all_skipped} | {unknown} | "
        "{invalid_evidence} |".format(**summary),
        "",
        "Missing evidence is **unknown**, not a CI failure. Positive evidence is",
        "accepted only from the file's expected producer job.",
    ]
    noteworthy = [item for item in report["files"] if item["status"] != "executed"]
    if noteworthy:
        lines.extend(["", "## Files needing review", ""])
        for item in noteworthy[:200]:
            expected = ", ".join(item["expected_jobs"]) or "unassigned"
            lines.append(
                f"- `{item['path']}` — **{item['status']}** "
                f"({item['reason']}; expected: {expected})"
            )
    if report["invalid_evidence"]:
        lines.extend(["", "## Invalid evidence", ""])
        for item in report["invalid_evidence"]:
            lines.append(f"- `{item['path']}` — {item['reason']}")
    return "\n".join(lines) + "\n"


def _producer_results(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        job, separator, outcome = value.partition("=")
        if not separator or not job or not outcome:
            raise ValueError(f"invalid producer result: {value!r}")
        result[job] = outcome
    return result


def _command_attest(args: argparse.Namespace) -> int:
    head_sha = args.head_sha or os.environ.get(HEAD_SHA_ENV, "")
    job = args.job or os.environ.get(JOB_ENV, "")
    if not head_sha or not job:
        raise ValueError("attestation requires exact head SHA and producer job")
    write_attestation(
        output=Path(args.output), head_sha=head_sha, job=job, runner=args.runner
    )
    return 0


def _command_report(args: argparse.Namespace) -> int:
    report = build_report(
        workflow=Path(args.workflow),
        tests_dir=Path(args.tests_dir),
        baseline=Path(args.baseline),
        evidence_dir=Path(args.evidence_dir),
        expected_head_sha=args.expected_head_sha,
        producer_values=args.producer,
        producer_results=_producer_results(args.producer_result),
    )
    _atomic_write_json(Path(args.output), report)
    markdown = render_markdown(report)
    Path(args.markdown).write_text(markdown, encoding="utf-8")
    print(markdown, end="")
    summary = report["summary"]
    if summary["all_skipped"] or summary["unknown"] or summary["invalid_evidence"]:
        print(
            "::warning title=CI execution evidence is report-only::"
            f"all_skipped={summary['all_skipped']} unknown={summary['unknown']} "
            f"invalid={summary['invalid_evidence']}"
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    attest = subparsers.add_parser("attest")
    attest.add_argument("--output", required=True)
    attest.add_argument("--runner", required=True)
    attest.add_argument("--head-sha")
    attest.add_argument("--job")
    attest.set_defaults(func=_command_attest)

    report = subparsers.add_parser("report")
    report.add_argument("--workflow", required=True)
    report.add_argument("--tests-dir", required=True)
    report.add_argument("--baseline", required=True)
    report.add_argument("--evidence-dir", required=True)
    report.add_argument("--expected-head-sha", required=True)
    report.add_argument("--producer", action="append", default=[], required=True)
    report.add_argument("--producer-result", action="append", default=[])
    report.add_argument("--output", required=True)
    report.add_argument("--markdown", required=True)
    report.set_defaults(func=_command_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
