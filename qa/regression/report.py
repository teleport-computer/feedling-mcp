"""Safe, deterministic reports for persona-memory baseline comparisons."""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from qa.regression import compare


_SPACE_RE = re.compile(r"\s+")
_SAFE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_FAILURE_CODES = frozenset(
    {
        "ASSISTANT_RESPONSE_MISSING",
        "BASELINE_EVIDENCE_INVALID",
        "BASELINE_METRIC_MISSING",
        "CANDIDATE_EVIDENCE_INVALID",
        "CANDIDATE_INFRA_ERROR",
        "CANDIDATE_METRIC_MISSING",
        "CANDIDATE_REGRESSION",
        "COMPARISON_CONTRACT_MISMATCH",
        "CONTRADICTION_RULES_MISSING",
        "EVALUATION_CONTRACT_MISMATCH",
        "EXPERIMENT_ID_MISSING",
        "INVALID_BOUNDARY_EVIDENCE",
        "INVALID_FAILURE_CODE",
        "INVALID_SCENARIO",
        "INVALID_SCENARIO_TRANSITION",
        "INVALID_SESSION_CREDENTIALS",
        "INVALID_TARGET_CAPABILITIES",
        "INVALID_TARGET_RESPONSE",
        "INVALID_TARGET_SESSION",
        "JUDGE_CONFIGURATION_INVALID",
        "JUDGE_EVIDENCE_INVALID",
        "JUDGE_EVIDENCE_MISMATCH",
        "JUDGE_ID_MISMATCH",
        "JUDGE_METADATA_INVALID",
        "JUDGE_NOT_CONFIGURED",
        "JUDGE_OUTPUT_INVALID",
        "JUDGE_REQUEST_INVALID",
        "JUDGE_RUBRIC_MISMATCH",
        "JUDGE_THRESHOLD_MISMATCH",
        "JUDGE_UNAVAILABLE",
        "MEMORY_CONTRADICTION",
        "MEMORY_EXPECTATION_MISSING",
        "MEMORY_PROBE_TURN_MISSING",
        "MEMORY_RECALL_MISSING",
        "METRIC_CONTRACT_AMBIGUOUS",
        "METRIC_EVIDENCE_DUPLICATE",
        "METRIC_EVIDENCE_INVALID",
        "METRIC_EVIDENCE_MISSING",
        "METRIC_THRESHOLD_NOT_MET",
        "METRIC_TRAJECTORY_BINDING_INVALID",
        "METRIC_TRAJECTORY_DUPLICATE",
        "METRIC_TRAJECTORY_EVIDENCE_MISSING",
        "PERSONA_CHARACTER_LIMIT_EXCEEDED",
        "PERSONA_CONSTRAINTS_MISSING",
        "PERSONA_FORBIDDEN_PHRASE",
        "PERSONA_IDENTITY_DRIFT",
        "PERSONA_QUESTION_LIMIT_EXCEEDED",
        "PERSONA_REQUIRED_MARKER_MISSING",
        "PERSONA_ROLE_DRIFT",
        "PERSONA_SIGNATURE_MISSING",
        "PRIVACY_CANARY_LEAK",
        "PRIVACY_CANARY_MISSING",
        "PRIVACY_PROBE_TURN_MISSING",
        "REPETITION_EVIDENCE_MISMATCH",
        "REPLY_CORRELATION_FAILED",
        "RESPONSE_CORRELATION_MISSING",
        "RUBRIC_BINDING_MISSING",
        "SAMPLE_COUNT_MISMATCH",
        "SCENARIO_FINGERPRINT_MISSING",
        "SCENARIO_MAX_TURNS_EXCEEDED",
        "SEMANTIC_CRITERION_NOT_MET",
        "SESSION_BOUNDARY_UNPROVEN",
        "SESSION_CLOSE_FAILED",
        "SESSION_ISOLATION_FAILED",
        "SESSION_OPEN_FAILED",
        "TARGET_REQUEST_FAILED",
        "TARGET_TIMEOUT",
        "TRAJECTORY_BINDING_INVALID",
        "TRAJECTORY_EVIDENCE_DUPLICATE",
        "TRAJECTORY_EVIDENCE_MISSING",
        "TRAJECTORY_INCOMPLETE",
        "TRAJECTORY_REPEAT_DUPLICATE",
        "TRAJECTORY_SCENARIO_BINDING_INVALID",
        "TRAJECTORY_STATUS_INVALID",
        "TRANSCRIPT_BOUNDARY_FAILED",
        "TRANSCRIPT_BOUNDARY_UNSUPPORTED",
        "UNEXPECTED_RUNNER_ERROR",
        "UNEXPECTED_WORKER_ERROR",
        "UNSUPPORTED_BOUNDARY",
        "UNSUPPORTED_SCENARIO_ROLE",
    }
)
_PUBLIC_CONTRACT_MISMATCHES = frozenset(
    {
        "coverage_contract",
        "evaluation_contract_sha256",
        "fixture_sha256",
        "judge.metadata",
        "persona_fixture_sha256",
        "rubric_sha256",
        "scenario_fingerprints",
        "scenario_set_sha256",
        "self_comparison.build_sha",
        "target.configuration",
        "target.model",
        "target.provider",
        "target.runtime_mode",
        "baseline_target.descriptor",
        "baseline_target.label",
        "baseline_target.build_sha",
        "baseline_target.runtime_mode",
        "baseline_target.provider",
        "baseline_target.model",
        "baseline_target.configuration",
        "account_batches.missing",
        "account_batches.overlap",
        "deployment_receipts.missing",
        "deployment_receipts.reused",
        "candidate_target.descriptor",
        "candidate_target.label",
        "candidate_target.build_sha",
        "candidate_target.runtime_mode",
        "candidate_target.provider",
        "candidate_target.model",
        "candidate_target.configuration",
    }
)


class ReportError(ValueError):
    """A comparison cannot be rendered or written safely."""


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _rows(comparison: Any) -> list[Any]:
    value = _get(comparison, "rows")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ReportError("comparison rows are missing")
    return list(value)


def _cell(value: Any) -> str:
    text = _SPACE_RE.sub(" ", str(value)).strip()
    return html.escape(text, quote=False).replace("|", "\\|")


def _rate(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.1%}"
    return "—"


def _delta(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):+.3f}"
    return "—"


def public_failure_codes(value: Any) -> list[str]:
    """Project failure codes onto the fixed public reporting allowlist.

    A syntactically valid but unknown code is deliberately collapsed so a
    model-created classifier cannot smuggle arbitrary text into artifacts.
    Invalid values use a separate fixed category.
    """

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return sorted(
        {
            (
                code
                if isinstance(code, str) and code in _PUBLIC_FAILURE_CODES
                else "UNRECOGNIZED_FAILURE_CODE"
                if isinstance(code, str) and _SAFE_CODE_RE.fullmatch(code)
                else "INVALID_FAILURE_CODE"
            )
            for code in value
        }
    )


def _safe_codes(value: Any) -> list[str]:
    """Backward-compatible private alias for existing report call sites."""

    return public_failure_codes(value)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _build_sha(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str)
        and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value)
        else None
    )


def _sha256(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None


def _public_aggregate(value: Any) -> dict[str, Any]:
    raw_status_counts = _get(value, "status_counts", {})
    status_counts = {
        status: _count(raw_status_counts.get(status, 0))
        for status in ("PASS", "FAIL", "INFRA_ERROR", "BLOCKED_EVIDENCE", "SKIP")
        if isinstance(raw_status_counts, Mapping) and raw_status_counts.get(status, 0)
    }
    raw_versions = _get(value, "metric_versions", [])
    versions = sorted(
        item
        for item in raw_versions
        if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,32}", item)
    ) if isinstance(raw_versions, Sequence) and not isinstance(raw_versions, (str, bytes, bytearray)) else []
    raw_evaluators = _get(value, "evaluator_types", [])
    evaluators = sorted(
        item for item in raw_evaluators if item in {"DETERMINISTIC", "LLM_JUDGE", "HUMAN"}
    ) if isinstance(raw_evaluators, Sequence) and not isinstance(raw_evaluators, (str, bytes, bytearray)) else []
    raw_rubrics = _get(value, "rubric_sha256", [])
    rubrics = sorted(
        item for item in raw_rubrics if isinstance(item, str) and _SHA256_RE.fullmatch(item)
    ) if isinstance(raw_rubrics, Sequence) and not isinstance(raw_rubrics, (str, bytes, bytearray)) else []
    result = {
        "sample_count": _count(_get(value, "sample_count")),
        "pass_count": _count(_get(value, "pass_count")),
        "pass_rate": _number(_get(value, "pass_rate")),
        "mean_score": _number(_get(value, "mean_score")),
        "min_score": _number(_get(value, "min_score")),
        "threshold": _number(_get(value, "threshold")),
        "hard_gate": _get(value, "hard_gate") is True,
        "status_counts": status_counts,
        "metric_versions": versions,
        "rubric_sha256": rubrics,
        "evaluator_types": evaluators,
    }
    result["failure_codes"] = _safe_codes(_get(value, "failure_codes", []))
    return result


def _public_projection(comparison: Any) -> dict[str, Any]:
    """Allowlist the public contract so accidental private fields stay private."""

    if hasattr(comparison, "to_dict"):
        comparison = comparison.to_dict()
    if not isinstance(comparison, Mapping):
        raise ReportError("comparison must be a mapping")
    projected_rows = []
    for row in _rows(comparison):
        projected_rows.append(
            {
                "scenario_id": _get(row, "scenario_id"),
                "metric_id": _get(row, "metric_id"),
                "hard_gate": _get(row, "hard_gate") is True,
                "baseline": _public_aggregate(_get(row, "baseline", {})),
                "candidate": _public_aggregate(_get(row, "candidate", {})),
                "score_delta": _get(row, "score_delta"),
                "outcome": _get(row, "outcome"),
                "failure_codes": _safe_codes(_get(row, "failure_codes", [])),
            }
        )
    raw_policy = _get(comparison, "policy", {})
    policy = {
        "required_pass_rate": _number(_get(raw_policy, "required_pass_rate")),
        "max_score_drop": _number(_get(raw_policy, "max_score_drop")),
    }
    raw_summary = _get(comparison, "summary", {})
    summary = {
        field: _count(_get(raw_summary, field))
        for field in (
            "metric_count",
            "parity",
            "regression",
            "soft_regression",
            "improvement",
            "existing_failure",
            "blocked_evidence",
            "infra_error",
        )
    }
    mismatches = _get(comparison, "contract_mismatches", [])
    safe_mismatches = sorted(
        {
            item if item in _PUBLIC_CONTRACT_MISMATCHES else "UNRECOGNIZED_CONTRACT_MISMATCH"
            for item in mismatches
            if isinstance(item, str)
        }
    ) if isinstance(mismatches, Sequence) and not isinstance(mismatches, (str, bytes, bytearray)) else []
    return {
        "schema_version": _get(comparison, "schema_version"),
        "comparison_type": _get(comparison, "comparison_type"),
        "baseline_target_id": _get(comparison, "baseline_target_id"),
        "candidate_target_id": _get(comparison, "candidate_target_id"),
        "baseline_build_sha": _build_sha(_get(comparison, "baseline_build_sha")),
        "candidate_build_sha": _build_sha(_get(comparison, "candidate_build_sha")),
        "baseline_deployment_receipt_sha256": _sha256(
            _get(comparison, "baseline_deployment_receipt_sha256")
        ),
        "candidate_deployment_receipt_sha256": _sha256(
            _get(comparison, "candidate_deployment_receipt_sha256")
        ),
        "baseline_experiment_id": _get(comparison, "baseline_experiment_id"),
        "candidate_experiment_id": _get(comparison, "candidate_experiment_id"),
        "status": _get(comparison, "status"),
        "policy": policy,
        "contract_mismatches": safe_mismatches,
        "summary": summary,
        "rows": projected_rows,
    }


def render_json(comparison: Any) -> str:
    """Return the canonical, stable JSON representation of a comparison."""

    comparison = _public_projection(comparison)
    try:
        return (
            json.dumps(
                comparison,
                allow_nan=False,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    except (TypeError, ValueError):
        raise ReportError("comparison is not JSON serializable") from None


def render_markdown_matrix(comparison_result: Any) -> str:
    """Render a concise parity matrix without prompts, responses, or evidence."""

    comparison_result = _public_projection(comparison_result)
    rows = _rows(comparison_result)
    lines = [
        "# Persona and memory regression matrix",
        "",
        f"- Status: `{_cell(_get(comparison_result, 'status', 'UNKNOWN'))}`",
        f"- Baseline: `{_cell(_get(comparison_result, 'baseline_target_id', 'unknown'))}`",
        f"- Candidate: `{_cell(_get(comparison_result, 'candidate_target_id', 'unknown'))}`",
        f"- Baseline build: `{_cell(_get(comparison_result, 'baseline_build_sha') or 'missing')}`",
        f"- Candidate build: `{_cell(_get(comparison_result, 'candidate_build_sha') or 'missing')}`",
        f"- Baseline deployment receipt: `{_cell(_get(comparison_result, 'baseline_deployment_receipt_sha256') or 'missing')}`",
        f"- Candidate deployment receipt: `{_cell(_get(comparison_result, 'candidate_deployment_receipt_sha256') or 'missing')}`",
        "- Canonical source: `run-result.json`",
        "",
    ]
    mismatches = _get(comparison_result, "contract_mismatches", [])
    if isinstance(mismatches, Sequence) and mismatches:
        lines.extend(
            [
                "## Contract mismatch",
                "",
                "Comparison is blocked because these locked inputs changed: "
                + ", ".join(f"`{_cell(value)}`" for value in mismatches),
                "",
            ]
        )

    header = (
        "Scenario",
        "Metric",
        "Gate",
        "Baseline pass",
        "Candidate pass",
        "Score Δ",
        "Outcome",
        "Failure codes",
    )
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows:
        baseline = _get(row, "baseline", {})
        candidate = _get(row, "candidate", {})
        codes = _safe_codes(_get(row, "failure_codes", []))
        code_text = ", ".join(_cell(code) for code in codes) if codes else "—"
        values = (
            _cell(_get(row, "scenario_id", "unknown-scenario")),
            _cell(_get(row, "metric_id", "unknown-metric")),
            "hard" if _get(row, "hard_gate") is True else "soft",
            _rate(_get(baseline, "pass_rate")),
            _rate(_get(candidate, "pass_rate")),
            _delta(_get(row, "score_delta")),
            _cell(_get(row, "outcome", "UNKNOWN")),
            code_text,
        )
        lines.append("| " + " | ".join(values) + " |")

    summary = _get(comparison_result, "summary", {})
    lines.extend(
        [
            "",
            "## Outcome counts",
            "",
            "| Outcome | Count |",
            "| --- | ---: |",
        ]
    )
    summary_fields = (
        ("PARITY", "parity"),
        ("REGRESSION", "regression"),
        ("SOFT_REGRESSION (warning)", "soft_regression"),
        ("IMPROVEMENT", "improvement"),
        ("EXISTING_FAILURE", "existing_failure"),
        ("BLOCKED_EVIDENCE", "blocked_evidence"),
        ("INFRA_ERROR", "infra_error"),
    )
    for label, field in summary_fields:
        value = _get(summary, field, 0)
        lines.append(f"| {label} | {value if type(value) is int else 0} |")
    return "\n".join(lines) + "\n"


def render_junit_xml(comparison_result: Any) -> str:
    """Render a regression-gate JUnit view.

    Only newly regressed hard-gate rows are failures.  Soft regressions remain
    visible as matrix warnings.  Invalid evidence and infrastructure failures
    are errors.  Existing baseline failures remain visible in the matrix but do
    not make a feature-parity gate fail.
    """

    comparison_result = _public_projection(comparison_result)
    rows = _rows(comparison_result)
    mismatches = _get(comparison_result, "contract_mismatches", [])
    contract_errors = 1 if isinstance(mismatches, Sequence) and mismatches else 0
    failures = sum(
        _get(row, "outcome") == compare.OUTCOME_REGRESSION
        and _get(row, "hard_gate") is True
        for row in rows
    )
    errors = sum(
        _get(row, "outcome")
        in (compare.OUTCOME_BLOCKED, compare.OUTCOME_INFRA_ERROR)
        for row in rows
    ) + contract_errors
    total = len(rows) + contract_errors

    root = ElementTree.Element(
        "testsuites",
        {
            "name": "feedling-persona-memory-regression",
            "tests": str(total),
            "failures": str(failures),
            "errors": str(errors),
        },
    )
    suite = ElementTree.SubElement(
        root,
        "testsuite",
        {
            "name": "feedling.persona-memory",
            "tests": str(total),
            "failures": str(failures),
            "errors": str(errors),
        },
    )
    properties = ElementTree.SubElement(suite, "properties")
    for name in (
        "baseline_build_sha",
        "candidate_build_sha",
        "baseline_deployment_receipt_sha256",
        "candidate_deployment_receipt_sha256",
    ):
        value = _get(comparison_result, name)
        ElementTree.SubElement(
            properties,
            "property",
            {"name": name, "value": str(value) if value is not None else "missing"},
        )

    if contract_errors:
        case = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": "feedling.persona-memory.contract",
                "name": "locked-evaluation-contract",
            },
        )
        ElementTree.SubElement(
            case,
            "error",
            {
                "type": compare.OUTCOME_BLOCKED,
                "message": "EVALUATION_CONTRACT_MISMATCH",
            },
        )

    for row in rows:
        scenario_id = str(_get(row, "scenario_id", "unknown-scenario"))
        metric_id = str(_get(row, "metric_id", "unknown-metric"))
        testcase = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": f"feedling.persona-memory.{scenario_id}",
                "name": metric_id,
            },
        )
        outcome = _get(row, "outcome")
        if (
            outcome == compare.OUTCOME_REGRESSION
            and _get(row, "hard_gate") is not True
        ):
            continue
        if outcome not in (
            compare.OUTCOME_REGRESSION,
            compare.OUTCOME_BLOCKED,
            compare.OUTCOME_INFRA_ERROR,
        ):
            continue
        safe_codes = _safe_codes(_get(row, "failure_codes", []))
        message = ",".join(safe_codes) if safe_codes else str(outcome)
        tag = "failure" if outcome == compare.OUTCOME_REGRESSION else "error"
        ElementTree.SubElement(
            testcase,
            tag,
            {"type": str(outcome), "message": message},
        )

    ElementTree.indent(root, space="  ")
    return (
        ElementTree.tostring(
            root,
            encoding="unicode",
            xml_declaration=True,
            short_empty_elements=True,
        )
        + "\n"
    )


def report_bundle(comparison_result: Any) -> dict[str, str]:
    """Build all CI artifacts without touching the filesystem."""

    return {
        "run-result.json": render_json(comparison_result),
        "matrix.md": render_markdown_matrix(comparison_result),
        "junit.xml": render_junit_xml(comparison_result),
    }


def _atomic_write(path: Path, content: str) -> None:
    temporary: Path | None = None
    descriptor = -1
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw_path)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise ReportError("regression artifacts could not be written") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def write_reports(comparison_result: Any, artifact_root: Path) -> None:
    """Atomically write ``run-result.json``, ``matrix.md``, and ``junit.xml``."""

    if artifact_root.is_symlink():
        raise ReportError("artifact root is unsafe")
    try:
        artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root = artifact_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ReportError("artifact root could not be created") from None
    if not root.is_dir():
        raise ReportError("artifact root is not a directory")
    for filename, content in report_bundle(comparison_result).items():
        destination = root / filename
        if destination.is_symlink():
            raise ReportError("artifact destination is unsafe")
        _atomic_write(destination, content)
