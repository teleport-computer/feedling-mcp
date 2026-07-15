#!/usr/bin/env python3
"""Publish an allowlisted persona-memory arm summary after verified cleanup.

The private experiment result contains prompts, replies, judge rationales,
trace identifiers, and account evidence.  This publisher loads it only through
the finalized arm verifier, then emits aggregate metrics from a fixed allowlist.
No private text or identity field is copied into the public artifact boundary.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from qa.regression.contracts import (
    ContractError,
    ExperimentResult,
    canonical_json_sha256,
)
from qa.regression.report import public_failure_codes
from qa.run_persona_memory_regression import (
    CommandError,
    _load_private_result,
    _verify_arm_receipt,
)


JSON_FILENAME = "persona-memory-summary.json"
MARKDOWN_FILENAME = "persona-memory-matrix.md"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_RESULT_STATUSES = frozenset(("PASS", "FAIL", "BLOCKED_EVIDENCE", "INFRA_ERROR"))
_METRIC_STATUSES = frozenset(("PASS", "FAIL", "BLOCKED_EVIDENCE", "INFRA_ERROR", "SKIP"))
_EVALUATORS = frozenset(("DETERMINISTIC", "LLM_JUDGE"))
_REASONING_EFFORTS = frozenset(("low", "medium", "high", "xhigh"))


class SummaryPublishError(RuntimeError):
    """Fixed-category public summary error with no private values."""


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SummaryPublishError(f"{label} hash is invalid")
    return value


def _status_from_counts(counts: Mapping[str, int]) -> str:
    if counts.get("INFRA_ERROR", 0):
        return "INFRA_ERROR"
    if counts.get("BLOCKED_EVIDENCE", 0) or counts.get("SKIP", 0):
        return "BLOCKED_EVIDENCE"
    if counts.get("FAIL", 0):
        return "FAIL"
    return "PASS"


def _judge_contract(result: ExperimentResult) -> dict[str, str] | None:
    contracts: set[tuple[str, str]] = set()
    for metric in result.metric_results:
        if metric.evaluator_type != "LLM_JUDGE":
            continue
        judge_id = metric.metadata.get("judge_id")
        configuration = metric.metadata.get("judge_configuration_sha256")
        if (
            not isinstance(judge_id, str)
            or _TOKEN_RE.fullmatch(judge_id) is None
            or not isinstance(configuration, str)
            or _SHA256_RE.fullmatch(configuration) is None
        ):
            raise SummaryPublishError("semantic judge contract is invalid")
        contracts.add((judge_id, configuration))
    if not contracts:
        return None
    if len(contracts) != 1:
        raise SummaryPublishError("semantic judge contract is inconsistent")
    judge_id, configuration = next(iter(contracts))
    return {
        "judge_id": judge_id,
        "configuration_sha256": configuration,
    }


def _metric_rows(result: ExperimentResult) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for metric in result.metric_results:
        if metric.status not in _METRIC_STATUSES:
            raise SummaryPublishError("metric status is invalid")
        if metric.evaluator_type not in _EVALUATORS:
            raise SummaryPublishError("metric evaluator is invalid")
        grouped[(metric.scenario_id, metric.metric_id)].append(metric)

    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (scenario_id, metric_id), metrics in sorted(grouped.items()):
        contracts = {
            (
                row.metric_version,
                row.evaluator_type,
                row.hard_gate,
                row.threshold,
                row.rubric_sha256,
            )
            for row in metrics
        }
        if len(contracts) != 1:
            raise SummaryPublishError("metric aggregation contract is inconsistent")
        metric_version, evaluator_type, hard_gate, threshold, rubric_sha256 = next(
            iter(contracts)
        )
        statuses = Counter(row.status for row in metrics)
        passed = sum(row.status == "PASS" and row.passed is True for row in metrics)
        scores = [float(row.score) for row in metrics if row.score is not None]
        if any(not math.isfinite(value) for value in scores):
            raise SummaryPublishError("metric score is invalid")
        total = len(metrics)
        failure_codes = public_failure_codes(
            [code for row in metrics for code in row.failure_codes]
        )
        by_scenario[scenario_id].append(
            {
                "metric_id": metric_id,
                "metric_version": metric_version,
                "evaluator_type": evaluator_type,
                "hard_gate": hard_gate,
                "status": _status_from_counts(statuses),
                "result_count": total,
                "pass_count": passed,
                "fail_count": statuses.get("FAIL", 0),
                "blocked_count": statuses.get("BLOCKED_EVIDENCE", 0)
                + statuses.get("SKIP", 0),
                "infra_error_count": statuses.get("INFRA_ERROR", 0),
                "pass_rate": round(passed / total, 6),
                "average_score": (
                    round(sum(scores) / len(scores), 6) if scores else None
                ),
                "threshold": threshold,
                "failure_codes": failure_codes,
                "rubric_sha256": _sha(rubric_sha256, "metric rubric"),
            }
        )
    return by_scenario


def build_public_summary(
    result: ExperimentResult,
    arm_receipt: Mapping[str, Any],
    *,
    result_sha256: str,
    arm_receipt_sha256: str,
) -> dict[str, Any]:
    """Return the sole public projection of one finalized private arm."""

    if len(result.targets) != 1 or result.status not in _RESULT_STATUSES:
        raise SummaryPublishError("persona-memory result target is invalid")
    target = result.targets[0]
    if arm_receipt.get("result_status") != result.status:
        raise SummaryPublishError("arm receipt status does not match result")
    coverage = result.metadata.get("coverage_contract")
    if not isinstance(coverage, Mapping):
        raise SummaryPublishError("coverage contract is missing")
    repetitions = coverage.get("repetitions")
    if type(repetitions) is not int or repetitions not in {1, 3}:
        raise SummaryPublishError("coverage repetition count is invalid")

    metric_rows = _metric_rows(result)
    scenarios: list[dict[str, Any]] = []
    for scenario_id, fingerprint in sorted(result.scenario_fingerprints.items()):
        trajectories = [
            row for row in result.trajectories if row.scenario_id == scenario_id
        ]
        versions = {row.scenario_version for row in trajectories}
        if len(versions) != 1 or not trajectories:
            raise SummaryPublishError("scenario version coverage is invalid")
        metrics = metric_rows.get(scenario_id)
        if not metrics:
            raise SummaryPublishError("scenario metric coverage is invalid")
        metric_statuses = Counter(row["status"] for row in metrics)
        trajectory_statuses = Counter(row.status for row in trajectories)
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "scenario_version": next(iter(versions)),
                "scenario_sha256": _sha(fingerprint, "scenario"),
                "status": _status_from_counts(metric_statuses),
                "trajectory_count": len(trajectories),
                "trajectory_status_counts": dict(sorted(trajectory_statuses.items())),
                "metrics": metrics,
            }
        )
    if set(metric_rows) != set(result.scenario_fingerprints):
        raise SummaryPublishError("scenario metric projection is incomplete")

    reasoning = target.configuration.get("reasoning_effort")
    reasoning_effort = reasoning if reasoning in _REASONING_EFFORTS else "unknown"
    hashes = {
        "result_sha256": _sha(result_sha256, "result"),
        "arm_receipt_sha256": _sha(arm_receipt_sha256, "arm receipt"),
        "persona_fixture_sha256": _sha(
            result.persona_fixture_sha256, "persona fixture"
        ),
        "rubric_sha256": _sha(result.rubric_sha256, "rubric"),
        "target_configuration_sha256": canonical_json_sha256(target.configuration),
        "evaluation_contract_sha256": _sha(
            result.metadata.get("evaluation_contract_sha256"),
            "evaluation contract",
        ),
        "source_bundle_sha256": _sha(
            arm_receipt.get("source_bundle_sha256"), "source bundle"
        ),
        "import_fixture_sha256": _sha(
            arm_receipt.get("import_fixture_sha256"), "import fixture"
        ),
        "pool_manifest_sha256": _sha(
            arm_receipt.get("pool_manifest_sha256"), "pool manifest"
        ),
        "route_sha256": _sha(arm_receipt.get("route_sha256"), "route"),
        "readiness_receipt_sha256": _sha(
            arm_receipt.get("readiness_receipt_sha256"), "readiness receipt"
        ),
        "cleanup_receipt_sha256": _sha(
            arm_receipt.get("cleanup_receipt_sha256"), "cleanup receipt"
        ),
        "deployment_receipt_pre_sha256": _sha(
            arm_receipt.get("deployment_receipt_pre_sha256"),
            "pre-deployment receipt",
        ),
        "deployment_receipt_post_sha256": _sha(
            arm_receipt.get("deployment_receipt_post_sha256"),
            "post-deployment receipt",
        ),
    }
    return {
        "schema_version": 1,
        "kind": "persona_memory_qualification_summary",
        "status": result.status,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "target": {
            "label": target.label,
            "build_sha": target.build_sha,
            "runtime_mode": target.runtime_mode,
            "provider": target.provider,
            "model": target.model,
            "reasoning_effort": reasoning_effort,
        },
        "coverage": {
            "repetitions": repetitions,
            "scenario_count": len(scenarios),
            "trajectory_count": len(result.trajectories),
            "metric_result_count": len(result.metric_results),
        },
        "judge": _judge_contract(result),
        "hashes": hashes,
        "scenarios": scenarios,
    }


def _cell(value: Any) -> str:
    return (
        str(value)
        .replace("|", "\\|")
        .replace("`", "'")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def render_markdown(summary: Mapping[str, Any]) -> str:
    target = summary["target"]
    coverage = summary["coverage"]
    lines = [
        "# Persona and memory qualification matrix",
        "",
        f"- Status: `{_cell(summary['status'])}`",
        f"- Build: `{_cell(target['build_sha'])}`",
        f"- Runtime: `{_cell(target['runtime_mode'])}`",
        f"- Provider/model: `{_cell(target['provider'])}` / `{_cell(target['model'])}`",
        f"- Repetitions: `{coverage['repetitions']}`",
        "- Canonical source: `persona-memory-summary.json`",
        "",
        "| Scenario | Metric | Evaluator | Gate | Status | Passed | Rate | Avg score | Threshold | Failure codes |",
        "|---|---|---|---:|---|---:|---:|---:|---:|---|",
    ]
    for scenario in summary["scenarios"]:
        for metric in scenario["metrics"]:
            codes = ", ".join(metric["failure_codes"]) or "—"
            average = (
                "—" if metric["average_score"] is None else metric["average_score"]
            )
            lines.append(
                "| "
                + " | ".join(
                    _cell(value)
                    for value in (
                        scenario["scenario_id"],
                        metric["metric_id"],
                        metric["evaluator_type"],
                        "hard" if metric["hard_gate"] else "soft",
                        metric["status"],
                        f"{metric['pass_count']}/{metric['result_count']}",
                        metric["pass_rate"],
                        average,
                        metric["threshold"],
                        codes,
                    )
                )
                + " |"
            )
    return "\n".join(lines) + "\n"


def _artifact_directory(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise SummaryPublishError("artifact directory is unsafe")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = candidate.lstat()
    except (OSError, RuntimeError):
        raise SummaryPublishError("artifact directory is unavailable") from None
    if (
        resolved != candidate
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise SummaryPublishError("artifact directory must be owner-only")
    return resolved


def _write_new(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise SummaryPublishError("public persona summary could not be created") from None


def publish(result_path: Path, arm_path: Path, artifact_dir: Path) -> dict[str, Any]:
    root = _artifact_directory(artifact_dir)
    try:
        result, result_sha256 = _load_private_result(result_path)
        arm_receipt, arm_sha256 = _verify_arm_receipt(
            arm_path, result, result_sha256
        )
    except (CommandError, ContractError):
        raise SummaryPublishError("finalized private persona arm is invalid") from None
    summary = build_public_summary(
        result,
        arm_receipt,
        result_sha256=result_sha256,
        arm_receipt_sha256=arm_sha256,
    )
    json_content = json.dumps(
        summary,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ) + "\n"
    json_path = root / JSON_FILENAME
    markdown_path = root / MARKDOWN_FILENAME
    _write_new(json_path, json_content)
    try:
        _write_new(markdown_path, render_markdown(summary))
    except Exception:
        try:
            json_path.unlink()
        except OSError:
            pass
        raise
    return summary


def publish_nonformal_skip(
    artifact_dir: Path, *, build_sha: str, repetitions: int
) -> dict[str, Any]:
    """Publish an explicit diagnostic skip when strict V2 proof is not required."""

    if (
        not isinstance(build_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", build_sha) is None
        or repetitions not in {1, 3}
    ):
        raise SummaryPublishError("non-formal skip inputs are invalid")
    root = _artifact_directory(artifact_dir)
    summary = {
        "schema_version": 1,
        "kind": "persona_memory_qualification_summary",
        "status": "NOT_FORMALLY_QUALIFIED",
        "target": {
            "label": "candidate",
            "build_sha": build_sha,
            "runtime_mode": "deployed_current",
            "provider": None,
            "model": None,
            "reasoning_effort": "unknown",
        },
        "coverage": {
            "repetitions": repetitions,
            "scenario_count": 0,
            "trajectory_count": 0,
            "metric_result_count": 0,
        },
        "judge": None,
        "hashes": {},
        "scenarios": [],
        "skip_reason": "STRICT_V2_USER_PATH_NOT_REQUIRED",
    }
    json_content = json.dumps(
        summary,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ) + "\n"
    markdown = (
        "# Persona and memory qualification matrix\n\n"
        "- Status: `NOT_FORMALLY_QUALIFIED`\n"
        f"- Build: `{build_sha}`\n"
        "- Runtime: `deployed_current`\n"
        f"- Requested repetitions: `{repetitions}`\n"
        "- Reason: `deployed_current` does not require exact Hosted Runtime V2 "
        "user-path mode and version evidence.\n"
        "- Canonical source: `persona-memory-summary.json`\n"
    )
    json_path = root / JSON_FILENAME
    markdown_path = root / MARKDOWN_FILENAME
    _write_new(json_path, json_content)
    try:
        _write_new(markdown_path, markdown)
    except Exception:
        try:
            json_path.unlink()
        except OSError:
            pass
        raise
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--arm-receipt", type=Path)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--nonformal-skip", action="store_true")
    parser.add_argument("--build-sha", default="")
    parser.add_argument("--repetitions", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.nonformal_skip:
            if args.result is not None or args.arm_receipt is not None:
                raise SummaryPublishError("skip publication cannot include private arm inputs")
            summary = publish_nonformal_skip(
                args.artifact_dir,
                build_sha=args.build_sha,
                repetitions=args.repetitions,
            )
        else:
            if (
                args.result is None
                or args.arm_receipt is None
                or args.build_sha
                or args.repetitions is not None
            ):
                raise SummaryPublishError("finalized arm publication inputs are incomplete")
            summary = publish(args.result, args.arm_receipt, args.artifact_dir)
    except SummaryPublishError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("ERROR: persona-memory summary publication failed", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "status": summary["status"],
                "files": [JSON_FILENAME, MARKDOWN_FILENAME],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
