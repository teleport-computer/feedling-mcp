"""Baseline/candidate comparison for persona-memory regression metrics."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any


OUTCOME_PARITY = "PARITY"
OUTCOME_REGRESSION = "REGRESSION"
OUTCOME_IMPROVEMENT = "IMPROVEMENT"
OUTCOME_EXISTING_FAILURE = "EXISTING_FAILURE"
OUTCOME_BLOCKED = "BLOCKED_EVIDENCE"
OUTCOME_INFRA_ERROR = "INFRA_ERROR"

_STATUS_PASS = "PASS"
_STATUS_FAIL = "FAIL"
_STATUS_SKIP = "SKIP"
_STATUS_BLOCKED = "BLOCKED_EVIDENCE"
_STATUS_INFRA = "INFRA_ERROR"
_SAFE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BUILD_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_KNOWN_STATUSES = frozenset(
    {_STATUS_PASS, _STATUS_FAIL, _STATUS_SKIP, _STATUS_BLOCKED, _STATUS_INFRA}
)
_KNOWN_EVALUATORS = frozenset({"DETERMINISTIC", "LLM_JUDGE", "HUMAN"})
_MISSING = object()


class ComparisonError(ValueError):
    """The supplied experiment result cannot be compared safely."""


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _items(value: Any) -> list[Any]:
    if value is None or isinstance(value, (str, bytes, bytearray)):
        return []
    if isinstance(value, Sequence):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return []


def _string(value: Any, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    enum_value = getattr(value, "value", None)
    return enum_value if isinstance(enum_value, str) and enum_value else default


def _canonical_json(value: Any) -> str | None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError):
        return None


def _status(row: Any) -> str:
    return _string(_get(row, "status"), _STATUS_BLOCKED)


def _score(row: Any) -> float | None:
    value = _get(row, "score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


def _threshold(row: Any) -> float:
    value = _get(row, "threshold", 1.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1.0
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else 1.0


def _failure_codes(row: Any) -> list[str]:
    result: list[str] = []
    for value in _items(_get(row, "failure_codes", [])):
        if isinstance(value, str) and _SAFE_CODE_RE.fullmatch(value):
            result.append(value)
        elif value is not None:
            result.append("INVALID_FAILURE_CODE")
    return result


def _metric_results(result: Any) -> list[Any]:
    nested = _get(result, "metric_results", None)
    if nested is not None:
        return _items(nested)
    nested = _get(result, "evaluations", None)
    if nested is not None:
        return _items(nested)
    if isinstance(result, Sequence) and not isinstance(
        result, (str, bytes, bytearray)
    ):
        return list(result)
    if _get(result, "metric_id") is not None:
        return [result]
    raise ComparisonError("result does not contain metric results")


def _target_ids(rows: Sequence[Any]) -> set[str]:
    return {
        _string(_get(row, "target_id"), "unknown-target")
        for row in rows
    }


def _select_target(
    rows: Sequence[Any], requested: str | None, *, label: str
) -> tuple[str, list[Any]]:
    target_ids = _target_ids(rows)
    if requested is None:
        if len(target_ids) != 1:
            raise ComparisonError(f"{label} result must identify exactly one target")
        requested = next(iter(target_ids))
    selected = [
        row
        for row in rows
        if _string(_get(row, "target_id"), "unknown-target") == requested
    ]
    if not selected:
        raise ComparisonError(f"{label} target has no metric results")
    return requested, selected


def _target_descriptor(
    result: Any,
    target_id: str,
    *,
    expected_label: str,
    side: str,
) -> tuple[dict[str, str] | None, list[str]]:
    """Select and normalize the descriptor which owns the selected rows."""

    raw_targets = _get(result, "targets", _MISSING)
    if raw_targets is _MISSING:
        return None, [f"{side}_target.descriptor"]
    targets = _items(raw_targets)
    matches = [
        item
        for item in targets
        if isinstance(_get(item, "target_id"), str)
        and _get(item, "target_id") == target_id
    ]
    if len(matches) != 1:
        return None, [f"{side}_target.descriptor"]

    item = matches[0]
    issues: list[str] = []
    descriptor: dict[str, str] = {"target_id": target_id}
    label = _get(item, "label")
    if label != expected_label:
        issues.append(f"{side}_target.label")
    elif isinstance(label, str):
        descriptor["label"] = label
    build_sha = _get(item, "build_sha")
    if not isinstance(build_sha, str) or _BUILD_SHA_RE.fullmatch(build_sha) is None:
        issues.append(f"{side}_target.build_sha")
    else:
        descriptor["build_sha"] = build_sha
    for field in ("runtime_mode", "provider", "model"):
        value = _get(item, field)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"{side}_target.{field}")
        else:
            descriptor[field] = value
    configuration = _get(item, "configuration", _MISSING)
    if not isinstance(configuration, Mapping):
        issues.append(f"{side}_target.configuration")
    else:
        canonical = _canonical_json(configuration)
        if canonical is None:
            issues.append(f"{side}_target.configuration")
        else:
            descriptor["configuration"] = canonical
    return descriptor, issues


def _target_contract_mismatches(
    baseline: Any,
    candidate: Any,
    *,
    baseline_id: str,
    candidate_id: str,
) -> list[str]:
    before, issues = _target_descriptor(
        baseline,
        baseline_id,
        expected_label="baseline",
        side="baseline",
    )
    after, candidate_issues = _target_descriptor(
        candidate,
        candidate_id,
        expected_label="candidate",
        side="candidate",
    )
    issues.extend(candidate_issues)
    if before is None or after is None:
        return sorted(set(issues))

    # A release comparison needs two actual builds.  Other route properties
    # are controlled variables and therefore must remain identical.
    if before.get("build_sha") == after.get("build_sha"):
        issues.append("self_comparison.build_sha")
    for field in ("runtime_mode", "provider", "model", "configuration"):
        if before.get(field) != after.get(field):
            issues.append(f"target.{field}")
    return sorted(set(issues))


def _comparison_identity(
    result: Any, target_id: str, *, expected_label: str
) -> dict[str, str | None]:
    descriptor, _issues = _target_descriptor(
        result,
        target_id,
        expected_label=expected_label,
        side=expected_label,
    )
    metadata = _get(result, "metadata", {})
    receipt = (
        metadata.get("deployment_receipt_sha256")
        if isinstance(metadata, Mapping)
        else None
    )
    return {
        "build_sha": descriptor.get("build_sha") if descriptor else None,
        "deployment_receipt_sha256": receipt
        if isinstance(receipt, str) and _SHA256_RE.fullmatch(receipt)
        else None,
    }


def _judge_result_metadata(result: Any) -> str | None:
    metadata = _get(result, "metadata", {})
    if not isinstance(metadata, Mapping):
        return "INVALID"
    selected = {
        key: value
        for key, value in metadata.items()
        if isinstance(key, str) and (key == "judge" or key.startswith("judge_"))
    }
    if not selected:
        return None
    return _canonical_json(selected) or "INVALID"


def _trajectory_context(result: Any, target_id: str) -> dict[str, Any]:
    """Index private trajectory evidence without copying it into the report."""

    raw = _get(result, "trajectories", _MISSING)
    issues: set[str] = set()
    by_id: dict[str, Any] = {}
    by_scenario: dict[str, set[str]] = {}
    repeat_by_id: dict[str, int] = {}
    repeat_keys: set[tuple[str, int]] = set()
    scenario_fingerprints = _get(result, "scenario_fingerprints", _MISSING)
    if not isinstance(scenario_fingerprints, Mapping):
        issues.add("SCENARIO_FINGERPRINT_MISSING")
        scenario_fingerprints = {}
    experiment_id = _get(result, "experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id:
        issues.add("EXPERIMENT_ID_MISSING")
        experiment_id = None
    if raw is _MISSING:
        issues.add("TRAJECTORY_EVIDENCE_MISSING")
        trajectories: list[Any] = []
    else:
        trajectories = _items(raw)
        if not trajectories:
            issues.add("TRAJECTORY_EVIDENCE_MISSING")

    selected = [item for item in trajectories if _get(item, "target_id") == target_id]
    if not selected:
        issues.add("TRAJECTORY_EVIDENCE_MISSING")
    for item in selected:
        trajectory_id = _get(item, "trajectory_id")
        scenario_id = _get(item, "scenario_id")
        repeat_index = _get(item, "repeat_index")
        if not isinstance(trajectory_id, str) or not trajectory_id:
            issues.add("TRAJECTORY_BINDING_INVALID")
            continue
        if trajectory_id in by_id:
            issues.add("TRAJECTORY_EVIDENCE_DUPLICATE")
            continue
        if not isinstance(scenario_id, str) or not scenario_id:
            issues.add("TRAJECTORY_BINDING_INVALID")
            continue
        if type(repeat_index) is not int or repeat_index < 0:
            issues.add("TRAJECTORY_BINDING_INVALID")
            continue
        if experiment_id is not None and _get(item, "experiment_id") != experiment_id:
            issues.add("TRAJECTORY_BINDING_INVALID")
        scenario_sha256 = _get(item, "scenario_sha256")
        if (
            not isinstance(scenario_sha256, str)
            or _SHA256_RE.fullmatch(scenario_sha256) is None
            or scenario_fingerprints.get(scenario_id) != scenario_sha256
        ):
            issues.add("TRAJECTORY_SCENARIO_BINDING_INVALID")
        trajectory_status = _get(item, "status")
        failure_code = _get(item, "failure_code")
        if trajectory_status not in {
            "COMPLETED",
            "PASS",
            "FAIL",
            "PRODUCT_FAIL",
            _STATUS_INFRA,
            _STATUS_BLOCKED,
        }:
            issues.add("TRAJECTORY_STATUS_INVALID")
        if (
            not isinstance(failure_code, str)
            or _SAFE_CODE_RE.fullmatch(failure_code) is None
            or (trajectory_status == "COMPLETED" and failure_code != "NONE")
        ):
            issues.add("TRAJECTORY_STATUS_INVALID")
        repeat_key = (scenario_id, repeat_index)
        if repeat_key in repeat_keys:
            issues.add("TRAJECTORY_REPEAT_DUPLICATE")
        repeat_keys.add(repeat_key)
        by_id[trajectory_id] = item
        repeat_by_id[trajectory_id] = repeat_index
        by_scenario.setdefault(scenario_id, set()).add(trajectory_id)
    return {
        "experiment_id": experiment_id,
        "issues": issues,
        "by_id": by_id,
        "by_scenario": by_scenario,
        "repeat_by_id": repeat_by_id,
    }


def _metric_key(row: Any) -> tuple[str, str]:
    scenario_id = _string(_get(row, "scenario_id"), "unknown-scenario")
    metric_id = _string(_get(row, "metric_id"), "unknown-metric")
    return scenario_id, metric_id


def _group(rows: Sequence[Any]) -> dict[tuple[str, str], list[Any]]:
    result: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        result.setdefault(_metric_key(row), []).append(row)
    return result


def _version_set(rows: Sequence[Any]) -> list[str]:
    return sorted(
        {
            _string(_get(row, "metric_version"), "unknown")
            for row in rows
        }
    )


def _rubric_set(rows: Sequence[Any]) -> list[str]:
    return sorted(
        {
            value
            for row in rows
            if isinstance((value := _get(row, "rubric_sha256")), str) and value
        }
    )


def _row_evidence_integrity(row: Any) -> tuple[set[str], str | None]:
    issues: set[str] = set()
    status = _status(row)
    evidence = _get(row, "evidence", _MISSING)
    if evidence is _MISSING or isinstance(evidence, (str, bytes, bytearray)):
        return {"METRIC_EVIDENCE_MISSING"}, None
    evidence_rows = _items(evidence)
    if status in {_STATUS_PASS, _STATUS_FAIL} and not evidence_rows:
        issues.add("METRIC_EVIDENCE_MISSING")
    canonical_rows: list[str] = []
    for item in evidence_rows:
        if not isinstance(item, Mapping):
            issues.add("METRIC_EVIDENCE_INVALID")
            continue
        canonical = _canonical_json(item)
        if canonical is None:
            issues.add("METRIC_EVIDENCE_INVALID")
            continue
        canonical_rows.append(canonical)
    if len(canonical_rows) != len(set(canonical_rows)):
        issues.add("METRIC_EVIDENCE_DUPLICATE")

    judge_contract: str | None = None
    if _string(_get(row, "evaluator_type"), "unknown") == "LLM_JUDGE" and status in {
        _STATUS_PASS,
        _STATUS_FAIL,
    }:
        metadata = _get(row, "metadata", _MISSING)
        judge_id = _get(metadata, "judge_id") if isinstance(metadata, Mapping) else None
        judge_configuration_sha256 = (
            _get(metadata, "judge_configuration_sha256")
            if isinstance(metadata, Mapping)
            else None
        )
        if (
            not isinstance(judge_id, str)
            or not judge_id
            or not isinstance(judge_configuration_sha256, str)
            or _SHA256_RE.fullmatch(judge_configuration_sha256) is None
        ):
            issues.add("JUDGE_METADATA_INVALID")
        else:
            judge_contract = _canonical_json(metadata)
            if judge_contract is None:
                issues.add("JUDGE_METADATA_INVALID")
            if len(evidence_rows) != 1:
                issues.add("JUDGE_EVIDENCE_INVALID")
            else:
                judge_evidence = evidence_rows[0]
                digest = _get(judge_evidence, "evidence_sha256")
                if (
                    not isinstance(judge_evidence, Mapping)
                    or _get(judge_evidence, "judge_id") != judge_id
                    or _get(
                        judge_evidence, "judge_configuration_sha256"
                    )
                    != judge_configuration_sha256
                    or not isinstance(digest, str)
                    or _SHA256_RE.fullmatch(digest) is None
                ):
                    issues.add("JUDGE_EVIDENCE_INVALID")
    return issues, judge_contract


def _aggregate(
    rows: Sequence[Any],
    *,
    context: Mapping[str, Any],
    target_id: str,
    scenario_id: str,
) -> dict[str, Any]:
    statuses = Counter(_status(row) for row in rows)
    scores = [score for row in rows if (score := _score(row)) is not None]
    codes = sorted({code for row in rows for code in _failure_codes(row)})
    thresholds = [_threshold(row) for row in rows]
    sample_count = len(rows)
    pass_count = statuses[_STATUS_PASS]
    integrity_codes = set(context["issues"])
    trajectory_ids: list[str] = []
    repeat_indices: list[int] = []
    judge_contracts: set[str] = set()
    for row in rows:
        if _get(row, "target_id") != target_id or _get(row, "scenario_id") != scenario_id:
            integrity_codes.add("METRIC_TRAJECTORY_BINDING_INVALID")
        experiment_id = context["experiment_id"]
        if experiment_id is not None and _get(row, "experiment_id") != experiment_id:
            integrity_codes.add("METRIC_TRAJECTORY_BINDING_INVALID")
        trajectory_id = _get(row, "trajectory_id")
        if not isinstance(trajectory_id, str) or trajectory_id not in context["by_id"]:
            integrity_codes.add("METRIC_TRAJECTORY_BINDING_INVALID")
        else:
            trajectory = context["by_id"][trajectory_id]
            if (
                _get(trajectory, "target_id") != target_id
                or _get(trajectory, "scenario_id") != scenario_id
                or (
                    experiment_id is not None
                    and _get(trajectory, "experiment_id") != experiment_id
                )
            ):
                integrity_codes.add("METRIC_TRAJECTORY_BINDING_INVALID")
            if (
                _status(row) in {_STATUS_PASS, _STATUS_FAIL}
                and _get(trajectory, "status") != "COMPLETED"
            ):
                integrity_codes.add("METRIC_TRAJECTORY_BINDING_INVALID")
            trajectory_ids.append(trajectory_id)
            repeat_indices.append(context["repeat_by_id"][trajectory_id])
        evidence_issues, judge_contract = _row_evidence_integrity(row)
        integrity_codes.update(evidence_issues)
        if judge_contract is not None:
            judge_contracts.add(judge_contract)
        status = _status(row)
        if status not in _KNOWN_STATUSES:
            integrity_codes.add("METRIC_EVIDENCE_INVALID")
        raw_score = _get(row, "score")
        if status in {_STATUS_PASS, _STATUS_FAIL} and (
            isinstance(raw_score, bool)
            or not isinstance(raw_score, (int, float))
            or not math.isfinite(raw_score)
            or not 0.0 <= raw_score <= 1.0
        ):
            integrity_codes.add("METRIC_EVIDENCE_INVALID")
        expected_passed = (
            True
            if status == _STATUS_PASS
            else False
            if status == _STATUS_FAIL
            else None
        )
        if _get(row, "passed") is not expected_passed:
            integrity_codes.add("METRIC_EVIDENCE_INVALID")
        raw_threshold = _get(row, "threshold")
        if (
            isinstance(raw_threshold, bool)
            or not isinstance(raw_threshold, (int, float))
            or not math.isfinite(raw_threshold)
            or not 0.0 <= raw_threshold <= 1.0
            or type(_get(row, "hard_gate")) is not bool
        ):
            integrity_codes.add("METRIC_CONTRACT_AMBIGUOUS")
        evaluator = _string(_get(row, "evaluator_type"), "unknown")
        if evaluator not in _KNOWN_EVALUATORS:
            integrity_codes.add("METRIC_EVIDENCE_INVALID")
        rubric = _get(row, "rubric_sha256")
        if not isinstance(rubric, str) or _SHA256_RE.fullmatch(rubric) is None:
            integrity_codes.add("RUBRIC_BINDING_MISSING")
        if "INVALID_FAILURE_CODE" in _failure_codes(row):
            integrity_codes.add("METRIC_EVIDENCE_INVALID")
    if len(trajectory_ids) != len(set(trajectory_ids)):
        integrity_codes.add("METRIC_TRAJECTORY_DUPLICATE")
    expected_ids = context["by_scenario"].get(scenario_id, set())
    if not expected_ids or set(trajectory_ids) != expected_ids:
        integrity_codes.add("METRIC_TRAJECTORY_EVIDENCE_MISSING")
    if len(judge_contracts) > 1:
        integrity_codes.add("JUDGE_METADATA_INVALID")
    if rows:
        if len(_version_set(rows)) != 1 or len(_rubric_set(rows)) != 1:
            integrity_codes.add("METRIC_CONTRACT_AMBIGUOUS")
        if len({_threshold(row) for row in rows}) != 1:
            integrity_codes.add("METRIC_CONTRACT_AMBIGUOUS")
        hard_gate_values = {
            _canonical_json(_get(row, "hard_gate")) for row in rows
        }
        if None in hard_gate_values or len(hard_gate_values) != 1:
            integrity_codes.add("METRIC_CONTRACT_AMBIGUOUS")
    return {
        "sample_count": sample_count,
        "pass_count": pass_count,
        "pass_rate": pass_count / sample_count if sample_count else None,
        "mean_score": sum(scores) / len(scores) if scores else None,
        "min_score": min(scores) if scores else None,
        "threshold": max(thresholds) if thresholds else 1.0,
        "hard_gate": any(_get(row, "hard_gate") is True for row in rows),
        "status_counts": dict(sorted(statuses.items())),
        "failure_codes": codes,
        "metric_versions": _version_set(rows),
        "rubric_sha256": _rubric_set(rows),
        "evaluator_types": sorted(
            {
                _string(_get(row, "evaluator_type"), "unknown")
                for row in rows
            }
        ),
        "integrity_codes": sorted(integrity_codes),
        "repeat_indices": sorted(set(repeat_indices)),
        "judge_contracts": sorted(judge_contracts),
    }


def _reported_aggregate(aggregate: Mapping[str, Any]) -> dict[str, Any]:
    """Remove comparison-only bindings from the safe report projection."""

    return {
        key: value
        for key, value in aggregate.items()
        if key not in {"integrity_codes", "repeat_indices", "judge_contracts"}
    }


def _has_infra(aggregate: Mapping[str, Any]) -> bool:
    return aggregate["status_counts"].get(_STATUS_INFRA, 0) > 0


def _has_blocked(aggregate: Mapping[str, Any]) -> bool:
    counts = aggregate["status_counts"]
    known = {_STATUS_PASS, _STATUS_FAIL, _STATUS_INFRA}
    return (
        counts.get(_STATUS_BLOCKED, 0) > 0
        or counts.get(_STATUS_SKIP, 0) > 0
        or any(count > 0 for status, count in counts.items() if status not in known)
    )


def _passes(
    aggregate: Mapping[str, Any], *, required_pass_rate: float
) -> bool:
    pass_rate = aggregate["pass_rate"]
    score = aggregate["mean_score"]
    effective_pass_rate = 1.0 if aggregate["hard_gate"] else required_pass_rate
    return (
        isinstance(pass_rate, float)
        and pass_rate >= effective_pass_rate
        and (score is None or score >= aggregate["threshold"])
        and not _has_infra(aggregate)
        and not _has_blocked(aggregate)
    )


def _score_delta(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> float | None:
    before = baseline["mean_score"]
    after = candidate["mean_score"]
    if isinstance(before, float) and isinstance(after, float):
        return after - before
    return None


def _contract_hashes(result: Any) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for key in (
        "fixture_sha256",
        "persona_fixture_sha256",
        "scenario_set_sha256",
        "rubric_sha256",
        "evaluation_contract_sha256",
    ):
        value = _get(result, key)
        if isinstance(value, str) and value:
            hashes[key] = value
    nested = _get(result, "contract_hashes", {})
    if isinstance(nested, Mapping):
        for key, value in nested.items():
            if isinstance(key, str) and isinstance(value, str) and value:
                hashes[key] = value
    metadata = _get(result, "metadata", {})
    if isinstance(metadata, Mapping):
        evaluation_contract = metadata.get("evaluation_contract_sha256")
        if isinstance(evaluation_contract, str) and evaluation_contract:
            hashes["evaluation_contract_sha256"] = evaluation_contract
        coverage_contract = metadata.get("coverage_contract")
        if coverage_contract is not None:
            hashes["coverage_contract"] = (
                _canonical_json(coverage_contract) or "INVALID"
            )
    fingerprints = _get(result, "scenario_fingerprints")
    if fingerprints is not None:
        if hasattr(fingerprints, "to_dict"):
            fingerprints = fingerprints.to_dict()
        try:
            hashes["scenario_fingerprints"] = json.dumps(
                fingerprints,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError):
            hashes["scenario_fingerprints"] = "INVALID"
    return dict(sorted(hashes.items()))


def _top_level_mismatches(baseline: Any, candidate: Any) -> list[str]:
    before = _contract_hashes(baseline)
    after = _contract_hashes(candidate)
    mismatches = {
        key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
    }
    baseline_judge = _judge_result_metadata(baseline)
    candidate_judge = _judge_result_metadata(candidate)
    if (
        baseline_judge == "INVALID"
        or candidate_judge == "INVALID"
        or baseline_judge != candidate_judge
    ):
        mismatches.add("judge.metadata")
    baseline_metadata = _get(baseline, "metadata", {})
    candidate_metadata = _get(candidate, "metadata", {})
    baseline_coverage = (
        baseline_metadata.get("coverage_contract")
        if isinstance(baseline_metadata, Mapping)
        else None
    )
    candidate_coverage = (
        candidate_metadata.get("coverage_contract")
        if isinstance(candidate_metadata, Mapping)
        else None
    )
    if baseline_coverage is not None or candidate_coverage is not None:
        baseline_receipt = (
            baseline_metadata.get("deployment_receipt_sha256")
            if isinstance(baseline_metadata, Mapping)
            else None
        )
        candidate_receipt = (
            candidate_metadata.get("deployment_receipt_sha256")
            if isinstance(candidate_metadata, Mapping)
            else None
        )
        valid_baseline_receipt = (
            isinstance(baseline_receipt, str)
            and _SHA256_RE.fullmatch(baseline_receipt) is not None
        )
        valid_candidate_receipt = (
            isinstance(candidate_receipt, str)
            and _SHA256_RE.fullmatch(candidate_receipt) is not None
        )
        if not valid_baseline_receipt or not valid_candidate_receipt:
            mismatches.add("deployment_receipts.missing")
        elif baseline_receipt == candidate_receipt:
            mismatches.add("deployment_receipts.reused")
        baseline_accounts = (
            baseline_metadata.get("account_fingerprints")
            if isinstance(baseline_metadata, Mapping)
            else None
        )
        candidate_accounts = (
            candidate_metadata.get("account_fingerprints")
            if isinstance(candidate_metadata, Mapping)
            else None
        )
        valid_baseline = (
            isinstance(baseline_accounts, list)
            and bool(baseline_accounts)
            and len(baseline_accounts) == len(set(baseline_accounts))
            and all(isinstance(item, str) and _SHA256_RE.fullmatch(item) for item in baseline_accounts)
        )
        valid_candidate = (
            isinstance(candidate_accounts, list)
            and bool(candidate_accounts)
            and len(candidate_accounts) == len(set(candidate_accounts))
            and all(isinstance(item, str) and _SHA256_RE.fullmatch(item) for item in candidate_accounts)
        )
        if not valid_baseline or not valid_candidate:
            mismatches.add("account_batches.missing")
        elif set(baseline_accounts) & set(candidate_accounts):
            mismatches.add("account_batches.overlap")
    return sorted(mismatches)


def _row_outcome(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    required_pass_rate: float,
    max_score_drop: float,
) -> tuple[str, list[str]]:
    codes = set(candidate["failure_codes"])
    baseline_integrity = set(baseline["integrity_codes"])
    candidate_integrity = set(candidate["integrity_codes"])
    if baseline_integrity or candidate_integrity:
        if baseline_integrity:
            codes.update(baseline_integrity)
            codes.add("BASELINE_EVIDENCE_INVALID")
        if candidate_integrity:
            codes.update(candidate_integrity)
            codes.add("CANDIDATE_EVIDENCE_INVALID")
        return OUTCOME_BLOCKED, sorted(codes)
    repetition_codes: set[str] = set()
    if baseline["repeat_indices"] != candidate["repeat_indices"]:
        repetition_codes.add("REPETITION_EVIDENCE_MISMATCH")
    if baseline["sample_count"] != candidate["sample_count"]:
        repetition_codes.add("SAMPLE_COUNT_MISMATCH")
    if repetition_codes:
        return OUTCOME_BLOCKED, sorted(codes | repetition_codes)
    if (
        baseline["metric_versions"] != candidate["metric_versions"]
        or baseline["rubric_sha256"] != candidate["rubric_sha256"]
        or baseline["evaluator_types"] != candidate["evaluator_types"]
        or baseline["judge_contracts"] != candidate["judge_contracts"]
        or baseline["threshold"] != candidate["threshold"]
        or baseline["hard_gate"] != candidate["hard_gate"]
        or "unknown" in baseline["metric_versions"]
        or "unknown" in candidate["metric_versions"]
        or "unknown" in baseline["evaluator_types"]
        or "unknown" in candidate["evaluator_types"]
    ):
        return OUTCOME_BLOCKED, sorted(
            codes | {"EVALUATION_CONTRACT_MISMATCH"}
        )
    if "INVALID_FAILURE_CODE" in codes:
        return OUTCOME_BLOCKED, sorted(codes | {"CANDIDATE_EVIDENCE_INVALID"})
    if _has_infra(candidate):
        return OUTCOME_INFRA_ERROR, sorted(codes | {"CANDIDATE_INFRA_ERROR"})
    if _has_infra(baseline) or _has_blocked(baseline):
        return OUTCOME_BLOCKED, sorted(codes | {"BASELINE_EVIDENCE_INVALID"})
    if _has_blocked(candidate):
        return OUTCOME_BLOCKED, sorted(codes | {"CANDIDATE_EVIDENCE_INVALID"})

    baseline_passes = _passes(baseline, required_pass_rate=required_pass_rate)
    candidate_passes = _passes(candidate, required_pass_rate=required_pass_rate)
    delta = _score_delta(baseline, candidate)
    score_regressed = delta is not None and delta < -max_score_drop
    pass_rate_regressed = candidate["pass_rate"] < baseline["pass_rate"]

    if baseline_passes and (not candidate_passes or score_regressed):
        return OUTCOME_REGRESSION, sorted(codes | {"CANDIDATE_REGRESSION"})
    if not baseline_passes and candidate_passes:
        return OUTCOME_IMPROVEMENT, sorted(codes)
    if not baseline_passes and not candidate_passes:
        if pass_rate_regressed or score_regressed:
            return OUTCOME_REGRESSION, sorted(codes | {"CANDIDATE_REGRESSION"})
        return OUTCOME_EXISTING_FAILURE, sorted(codes)
    return OUTCOME_PARITY, sorted(codes)


def compare_results(
    baseline: Any,
    candidate: Any,
    *,
    baseline_target_id: str | None = None,
    candidate_target_id: str | None = None,
    required_pass_rate: float = 1.0,
    max_score_drop: float = 0.0,
) -> dict[str, Any]:
    """Compare two result sets using matched scenario/metric repetitions.

    Contract hashes, metric versions, rubric hashes, and repetition counts must
    match.  A mismatch is evidence-blocking rather than a false product failure.
    Existing baseline failures do not fail the parity gate unless the candidate
    makes them worse.
    """

    if not 0.0 <= required_pass_rate <= 1.0:
        raise ComparisonError("required pass rate must be between zero and one")
    if not 0.0 <= max_score_drop <= 1.0:
        raise ComparisonError("maximum score drop must be between zero and one")

    baseline_id, baseline_rows = _select_target(
        _metric_results(baseline), baseline_target_id, label="baseline"
    )
    candidate_id, candidate_rows = _select_target(
        _metric_results(candidate), candidate_target_id, label="candidate"
    )
    if baseline_id == candidate_id:
        raise ComparisonError("baseline and candidate target ids must be distinct")

    baseline_context = _trajectory_context(baseline, baseline_id)
    candidate_context = _trajectory_context(candidate, candidate_id)
    before = _group(baseline_rows)
    after = _group(candidate_rows)
    keys = sorted(before.keys() | after.keys())
    rows: list[dict[str, Any]] = []
    for scenario_id, metric_id in keys:
        baseline_group = before.get((scenario_id, metric_id), [])
        candidate_group = after.get((scenario_id, metric_id), [])
        baseline_aggregate = _aggregate(
            baseline_group,
            context=baseline_context,
            target_id=baseline_id,
            scenario_id=scenario_id,
        )
        candidate_aggregate = _aggregate(
            candidate_group,
            context=candidate_context,
            target_id=candidate_id,
            scenario_id=scenario_id,
        )
        if not baseline_group:
            outcome = OUTCOME_BLOCKED
            codes = ["BASELINE_METRIC_MISSING"]
        elif not candidate_group:
            outcome = OUTCOME_BLOCKED
            codes = ["CANDIDATE_METRIC_MISSING"]
        else:
            outcome, codes = _row_outcome(
                baseline_aggregate,
                candidate_aggregate,
                required_pass_rate=required_pass_rate,
                max_score_drop=max_score_drop,
            )
        rows.append(
            {
                "scenario_id": scenario_id,
                "metric_id": metric_id,
                "hard_gate": baseline_aggregate["hard_gate"]
                or candidate_aggregate["hard_gate"],
                "baseline": _reported_aggregate(baseline_aggregate),
                "candidate": _reported_aggregate(candidate_aggregate),
                "score_delta": _score_delta(
                    baseline_aggregate, candidate_aggregate
                ),
                "outcome": outcome,
                "failure_codes": codes,
            }
        )

    contract_mismatches = sorted(
        set(_top_level_mismatches(baseline, candidate))
        | set(
            _target_contract_mismatches(
                baseline,
                candidate,
                baseline_id=baseline_id,
                candidate_id=candidate_id,
            )
        )
    )
    baseline_identity = _comparison_identity(
        baseline, baseline_id, expected_label="baseline"
    )
    candidate_identity = _comparison_identity(
        candidate, candidate_id, expected_label="candidate"
    )
    if contract_mismatches:
        for row in rows:
            row["outcome"] = OUTCOME_BLOCKED
            row["failure_codes"] = sorted(
                set(row["failure_codes"]) | {"COMPARISON_CONTRACT_MISMATCH"}
            )
    counts = Counter(row["outcome"] for row in rows)
    hard_regressions = sum(
        row["outcome"] == OUTCOME_REGRESSION and row["hard_gate"] is True
        for row in rows
    )
    soft_regressions = counts[OUTCOME_REGRESSION] - hard_regressions
    if contract_mismatches:
        overall_status = OUTCOME_BLOCKED
    elif counts[OUTCOME_INFRA_ERROR]:
        overall_status = OUTCOME_INFRA_ERROR
    elif counts[OUTCOME_BLOCKED]:
        overall_status = OUTCOME_BLOCKED
    elif hard_regressions:
        overall_status = _STATUS_FAIL
    else:
        overall_status = _STATUS_PASS

    return {
        "schema_version": 1,
        "comparison_type": "BASELINE_CANDIDATE",
        "baseline_target_id": baseline_id,
        "candidate_target_id": candidate_id,
        "baseline_build_sha": baseline_identity["build_sha"],
        "candidate_build_sha": candidate_identity["build_sha"],
        "baseline_deployment_receipt_sha256": baseline_identity[
            "deployment_receipt_sha256"
        ],
        "candidate_deployment_receipt_sha256": candidate_identity[
            "deployment_receipt_sha256"
        ],
        "baseline_experiment_id": _string(
            _get(baseline, "experiment_id"), "unknown-experiment"
        ),
        "candidate_experiment_id": _string(
            _get(candidate, "experiment_id"), "unknown-experiment"
        ),
        "status": overall_status,
        "policy": {
            "required_pass_rate": required_pass_rate,
            "max_score_drop": max_score_drop,
        },
        "contract_mismatches": contract_mismatches,
        "summary": {
            "metric_count": len(rows),
            "parity": counts[OUTCOME_PARITY],
            "regression": counts[OUTCOME_REGRESSION],
            "soft_regression": soft_regressions,
            "improvement": counts[OUTCOME_IMPROVEMENT],
            "existing_failure": counts[OUTCOME_EXISTING_FAILURE],
            "blocked_evidence": counts[OUTCOME_BLOCKED]
            + (1 if contract_mismatches else 0),
            "infra_error": counts[OUTCOME_INFRA_ERROR],
        },
        "rows": rows,
    }


def compare_experiment(
    experiment_result: Any,
    *,
    baseline_target_id: str,
    candidate_target_id: str,
    required_pass_rate: float = 1.0,
    max_score_drop: float = 0.0,
) -> dict[str, Any]:
    """Compare two targets contained in one ``ExperimentResult``."""

    return compare_results(
        experiment_result,
        experiment_result,
        baseline_target_id=baseline_target_id,
        candidate_target_id=candidate_target_id,
        required_pass_rate=required_pass_rate,
        max_score_drop=max_score_drop,
    )
