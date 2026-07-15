"""Evaluation orchestration for collected persona/memory trajectories."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import re
from typing import Any

from qa.regression import evaluators
from qa.regression.contracts import (
    ExperimentResult,
    ExperimentTarget,
    GoldenPersona,
    MetricResult,
    Scenario,
    Trajectory,
)
from qa.regression.judge import (
    JudgeError,
    JudgeMetricSpec,
    StructuredJudge,
    build_judge_request,
    evaluate_with_judge,
    judge_configuration_sha256,
)
from qa.regression.runner import COMPLETED, RegressionRunner
from qa.regression.scenario_loader import RegressionSuite
from qa.regression.versions import evaluation_versions, metric_version


DETERMINISTIC = "DETERMINISTIC"
LLM_JUDGE = "LLM_JUDGE"
_DETERMINISTIC_METRICS = frozenset(
    {
        "persona.hard_constraints",
        "memory.recall",
        "memory.contradiction",
        "privacy.canary",
    }
)
_SUPPORTED_REQUIREMENTS = frozenset(
    {"persistent_memory_transcript_boundary", "persistent_memory_strong"}
)


class EvaluationError(ValueError):
    """An evaluation configuration cannot be applied safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _metric_configs(scenario: Scenario) -> dict[str, dict[str, Any]]:
    raw = scenario.rubric.get("metrics")
    if not isinstance(raw, Mapping) or set(raw) != set(scenario.metric_ids):
        raise EvaluationError("scenario metric configuration is incomplete")
    result: dict[str, dict[str, Any]] = {}
    for metric_id in scenario.metric_ids:
        config = raw.get(metric_id)
        if not isinstance(config, Mapping):
            raise EvaluationError(f"metric configuration is invalid: {metric_id}")
        evaluator_type = config.get("evaluator_type")
        threshold = config.get("threshold")
        hard_gate = config.get("hard_gate")
        if evaluator_type not in {DETERMINISTIC, LLM_JUDGE}:
            raise EvaluationError(f"metric evaluator type is invalid: {metric_id}")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not 0.0 <= float(threshold) <= 1.0
            or type(hard_gate) is not bool
        ):
            raise EvaluationError(f"metric threshold or gate is invalid: {metric_id}")
        result[metric_id] = dict(config)
    return result


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def _merge_lists(left: Any, right: Any) -> list[Any]:
    result: list[Any] = []
    for value in [*_list(left), *_list(right)]:
        if value not in result:
            result.append(value)
    return result


def _persona_constraints(
    persona: GoldenPersona, metric_config: Mapping[str, Any]
) -> dict[str, Any]:
    base = persona.rubric.get("hard_constraints", {})
    result = dict(base) if isinstance(base, Mapping) else {}
    result["do_not_say"] = _merge_lists(persona.do_not_say, result.get("do_not_say"))
    result["signature"] = _merge_lists(
        persona.signature_phrases, result.get("signature")
    )
    configured = metric_config.get("config", {})
    if not isinstance(configured, Mapping):
        raise EvaluationError("persona hard-constraint config must be an object")
    list_fields = {
        "required_markers",
        "must_include_all",
        "must_include_any",
        "forbidden_phrases",
        "must_not_include",
        "do_not_say",
        "forbidden_identity_names",
        "forbidden_role_claims",
        "signature",
    }
    for key, value in configured.items():
        result[key] = _merge_lists(result.get(key), value) if key in list_fields else value
    return result


def _scenario_facts(scenario: Scenario) -> list[dict[str, Any]]:
    value = scenario.rubric.get("facts", [])
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise EvaluationError("scenario facts must be a list of objects")
    return [dict(item) for item in value]


def _facts_for_metric(
    persona: GoldenPersona,
    scenario: Scenario,
    metric_config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    all_facts = [dict(item) for item in persona.memory_facts]
    all_facts.extend(_scenario_facts(scenario))
    by_id = {str(item.get("id") or ""): item for item in all_facts}
    configured = metric_config.get("config", {})
    if not isinstance(configured, Mapping):
        raise EvaluationError("memory metric config must be an object")
    selected_ids = configured.get("fact_ids")
    if selected_ids is None:
        selected = list(all_facts)
    elif (
        not isinstance(selected_ids, list)
        or any(not isinstance(item, str) for item in selected_ids)
        or len(selected_ids) != len(set(selected_ids))
        or any(item not in by_id for item in selected_ids)
    ):
        raise EvaluationError("memory metric fact_ids are invalid")
    else:
        selected = [dict(by_id[item]) for item in selected_ids]
    probe_ids = configured.get("probe_turn_ids")
    if probe_ids is not None:
        if not isinstance(probe_ids, list) or any(not isinstance(item, str) for item in probe_ids):
            raise EvaluationError("memory metric probe_turn_ids are invalid")
        for fact in selected:
            fact["probe_turn_ids"] = list(probe_ids)
    return selected


def _privacy_config(
    persona: GoldenPersona, metric_config: Mapping[str, Any]
) -> dict[str, Any]:
    raw = persona.rubric.get("privacy", {})
    result = dict(raw) if isinstance(raw, Mapping) else {}
    configured = metric_config.get("config", {})
    if not isinstance(configured, Mapping):
        raise EvaluationError("privacy metric config must be an object")
    result.update(configured)
    return result


def validate_suite_contract(suite: RegressionSuite) -> None:
    """Compile the suite before any live account or target is mutated."""

    for scenario in suite.scenarios:
        turn_ids = {turn.turn_id for turn in scenario.turns}
        ordered_ids = [turn.turn_id for turn in scenario.turns]
        if any(turn.role != "user" for turn in scenario.turns):
            raise EvaluationError(
                f"release runner accepts only user turns: {scenario.scenario_id}"
            )
        requirements = set(scenario.requirements)
        requirements.update(
            requirement for turn in scenario.turns for requirement in turn.requirements
        )
        if requirements - _SUPPORTED_REQUIREMENTS:
            raise EvaluationError(
                f"scenario requirements are unsupported: {scenario.scenario_id}"
            )
        if scenario.metadata.get("lane") not in {"release", "nightly"}:
            raise EvaluationError(f"scenario lane is invalid: {scenario.scenario_id}")

        edges: dict[str, set[str]] = {turn_id: set() for turn_id in turn_ids}
        for index, turn in enumerate(scenario.turns):
            always_indexes = [
                position
                for position, transition in enumerate(turn.transitions)
                if transition.condition == "always"
            ]
            if always_indexes and always_indexes[-1] != len(turn.transitions) - 1:
                raise EvaluationError(
                    f"always transition must be last: {scenario.scenario_id}/{turn.turn_id}"
                )
            if turn.transitions:
                edges[turn.turn_id].update(
                    transition.target_turn_id
                    for transition in turn.transitions
                    if transition.target_turn_id is not None
                )
            elif turn.next_turn_id is not None:
                edges[turn.turn_id].add(turn.next_turn_id)
            elif turn.metadata.get("terminal") is not True and index + 1 < len(ordered_ids):
                edges[turn.turn_id].add(ordered_ids[index + 1])
        reachable: set[str] = set()
        pending = [str(scenario.entry_turn_id)]
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            pending.extend(sorted(edges[current] - reachable))
        if reachable != turn_ids:
            raise EvaluationError(f"scenario contains unreachable turns: {scenario.scenario_id}")

        configs = _metric_configs(scenario)
        for metric_id, config in configs.items():
            evaluator_type = str(config["evaluator_type"])
            if metric_version(metric_id, evaluator_type) == "UNVERSIONED":
                raise EvaluationError(f"metric implementation is unversioned: {metric_id}")
            if evaluator_type == DETERMINISTIC and metric_id not in _DETERMINISTIC_METRICS:
                raise EvaluationError(f"deterministic metric is unsupported: {metric_id}")
            if evaluator_type == LLM_JUDGE and not isinstance(
                config.get("description"), str
            ):
                raise EvaluationError(f"judge metric description is missing: {metric_id}")
            probe_ids: set[str] = set()
            configured = config.get("config", {})
            if isinstance(configured, Mapping):
                raw_probe_ids = configured.get("probe_turn_ids", [])
                if isinstance(raw_probe_ids, list):
                    probe_ids.update(
                        item for item in raw_probe_ids if isinstance(item, str)
                    )
            if metric_id in {"memory.recall", "memory.contradiction"}:
                facts = _facts_for_metric(suite.persona, scenario, config)
                for fact in facts:
                    raw_probe_ids = fact.get("probe_turn_ids", [])
                    if isinstance(raw_probe_ids, list):
                        probe_ids.update(
                            item for item in raw_probe_ids if isinstance(item, str)
                        )
            elif metric_id == "persona.hard_constraints":
                _persona_constraints(suite.persona, config)
            elif metric_id == "privacy.canary":
                privacy = _privacy_config(suite.persona, config)
                raw_probe_ids = privacy.get("probe_turn_ids", [])
                if isinstance(raw_probe_ids, list):
                    probe_ids.update(
                        item for item in raw_probe_ids if isinstance(item, str)
                    )
            if probe_ids - reachable:
                raise EvaluationError(
                    f"metric probe turns are missing or unreachable: {scenario.scenario_id}/{metric_id}"
                )


def _metric_from_row(
    row: Mapping[str, Any],
    *,
    scenario: Scenario,
    config: Mapping[str, Any],
) -> MetricResult:
    payload = dict(row)
    payload.update(
        {
            "kind": "metric_result",
            "schema_version": 1,
            "metric_id": str(payload.get("metric_id")),
            "metric_version": metric_version(
                str(payload.get("metric_id")), str(config["evaluator_type"])
            ),
            "evaluator_type": str(config["evaluator_type"]),
            "threshold": float(config["threshold"]),
            "hard_gate": config["hard_gate"] is True,
            "rubric_sha256": scenario.rubric_sha256,
        }
    )
    score = payload.get("score")
    status = payload.get("status")
    if status in {"PASS", "FAIL"} and score is not None:
        passed = float(score) >= float(payload["threshold"])
        payload["passed"] = passed
        payload["status"] = "PASS" if passed else "FAIL"
        if not passed and not payload.get("failure_codes"):
            payload["failure_codes"] = ["METRIC_THRESHOLD_NOT_MET"]
    return MetricResult.from_dict(payload)


def _blocked_metric(
    trajectory: Trajectory,
    scenario: Scenario,
    metric_id: str,
    config: Mapping[str, Any],
    failure_code: str,
    *,
    status: str = "BLOCKED_EVIDENCE",
) -> MetricResult:
    return MetricResult.from_dict(
        {
            "kind": "metric_result",
            "schema_version": 1,
            "metric_id": metric_id,
            "metric_version": metric_version(metric_id, str(config["evaluator_type"])),
            "experiment_id": trajectory.experiment_id,
            "target_id": trajectory.target_id,
            "trajectory_id": trajectory.trajectory_id,
            "scenario_id": trajectory.scenario_id,
            "evaluator_type": str(config["evaluator_type"]),
            "status": status,
            "passed": None,
            "score": None,
            "threshold": float(config["threshold"]),
            "hard_gate": config["hard_gate"] is True,
            "failure_codes": [failure_code],
            "evidence": [],
            "summary": "evaluation evidence is unavailable",
            "rubric_sha256": scenario.rubric_sha256,
            "metadata": {},
        }
    )


def _evaluate_deterministic(
    trajectory: Trajectory,
    persona: GoldenPersona,
    scenario: Scenario,
    metric_id: str,
    config: Mapping[str, Any],
) -> MetricResult:
    if metric_id == "persona.hard_constraints":
        row = evaluators.evaluate_persona_hard_constraints(
            trajectory, _persona_constraints(persona, config), metric_id=metric_id
        )
    elif metric_id == "memory.recall":
        row = evaluators.evaluate_memory_recall(
            trajectory,
            _facts_for_metric(persona, scenario, config),
            metric_id=metric_id,
        )
    elif metric_id == "memory.contradiction":
        row = evaluators.evaluate_memory_contradictions(
            trajectory,
            _facts_for_metric(persona, scenario, config),
            metric_id=metric_id,
        )
    elif metric_id == "privacy.canary":
        row = evaluators.evaluate_privacy_canaries(
            trajectory, _privacy_config(persona, config), metric_id=metric_id
        )
    else:
        raise EvaluationError(f"unknown deterministic metric: {metric_id}")
    return _metric_from_row(row, scenario=scenario, config=config)


def _judge_metrics(
    trajectory: Trajectory,
    persona: GoldenPersona,
    scenario: Scenario,
    configs: Mapping[str, Mapping[str, Any]],
    judge: StructuredJudge | None,
) -> list[MetricResult]:
    metric_ids = [
        metric_id
        for metric_id in scenario.metric_ids
        if configs[metric_id]["evaluator_type"] == LLM_JUDGE
    ]
    if not metric_ids:
        return []
    if judge is None:
        return [
            _blocked_metric(
                trajectory, scenario, metric_id, configs[metric_id], "JUDGE_NOT_CONFIGURED"
            )
            for metric_id in metric_ids
        ]
    specs: list[JudgeMetricSpec] = []
    for metric_id in metric_ids:
        config = configs[metric_id]
        description = config.get("description")
        if not isinstance(description, str) or not description:
            raise EvaluationError(f"judge metric description is missing: {metric_id}")
        specs.append(
            JudgeMetricSpec(
                metric_id=metric_id,
                description=description,
                threshold=float(config["threshold"]),
                hard_gate=config["hard_gate"] is True,
            )
        )
    request = build_judge_request(
        persona=persona,
        scenario=scenario,
        trajectory=trajectory,
        rubric_sha256=scenario.rubric_sha256,
        metrics=specs,
    )
    try:
        judge_config_sha256 = judge_configuration_sha256(judge)
        result = evaluate_with_judge(judge, request)
    except JudgeError as exc:
        code = (
            exc.code
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", exc.code or "")
            else "JUDGE_EVIDENCE_INVALID"
        )
        return [
            _blocked_metric(trajectory, scenario, metric_id, configs[metric_id], code)
            for metric_id in metric_ids
        ]
    by_id = {metric.metric_id: metric for metric in result.metrics}
    rows: list[MetricResult] = []
    for metric_id in metric_ids:
        metric = by_id[metric_id]
        config = configs[metric_id]
        rows.append(
            MetricResult.from_dict(
                {
                    "kind": "metric_result",
                    "schema_version": 1,
                    "metric_id": metric_id,
                    "metric_version": metric_version(metric_id, LLM_JUDGE),
                    "experiment_id": trajectory.experiment_id,
                    "target_id": trajectory.target_id,
                    "trajectory_id": trajectory.trajectory_id,
                    "scenario_id": trajectory.scenario_id,
                    "evaluator_type": LLM_JUDGE,
                    "status": "PASS" if metric.passed else "FAIL",
                    "passed": metric.passed,
                    "score": metric.score,
                    "threshold": float(config["threshold"]),
                    "hard_gate": config["hard_gate"] is True,
                    "failure_codes": list(metric.failure_codes),
                    "evidence": [
                        {
                            "evidence_turn_ids": list(metric.evidence_turn_ids),
                            "rationale": metric.rationale,
                            "judge_id": result.judge_id,
                            "judge_configuration_sha256": judge_config_sha256,
                            "evidence_sha256": result.evidence_sha256,
                        }
                    ],
                    "summary": "structured semantic judgment completed",
                    "rubric_sha256": scenario.rubric_sha256,
                    "metadata": {
                        "judge_id": result.judge_id,
                        "judge_configuration_sha256": judge_config_sha256,
                    },
                }
            )
        )
    return rows


def evaluate_trajectory(
    trajectory: Trajectory,
    *,
    persona: GoldenPersona,
    scenario: Scenario,
    judge: StructuredJudge | None = None,
) -> list[MetricResult]:
    if trajectory.scenario_id != scenario.scenario_id:
        raise EvaluationError("trajectory scenario does not match evaluator scenario")
    if trajectory.scenario_sha256 != scenario.fingerprint_sha256():
        raise EvaluationError("trajectory scenario fingerprint does not match")
    configs = _metric_configs(scenario)
    if trajectory.status != COMPLETED:
        status = "INFRA_ERROR" if trajectory.status == "INFRA_ERROR" else "BLOCKED_EVIDENCE"
        code = trajectory.failure_code if trajectory.failure_code != "NONE" else "TRAJECTORY_INCOMPLETE"
        return [
            _blocked_metric(
                trajectory, scenario, metric_id, configs[metric_id], code, status=status
            )
            for metric_id in scenario.metric_ids
        ]
    rows = [
        _evaluate_deterministic(
            trajectory, persona, scenario, metric_id, configs[metric_id]
        )
        for metric_id in scenario.metric_ids
        if configs[metric_id]["evaluator_type"] == DETERMINISTIC
    ]
    rows.extend(_judge_metrics(trajectory, persona, scenario, configs, judge))
    order = {metric_id: index for index, metric_id in enumerate(scenario.metric_ids)}
    return sorted(rows, key=lambda item: order[item.metric_id])


def build_experiment_result(
    *,
    suite: RegressionSuite,
    target: ExperimentTarget,
    experiment_id: str,
    trajectories: Sequence[Trajectory],
    metric_results: Sequence[MetricResult],
    started_at: str,
    repetitions: int,
    finished_at: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExperimentResult:
    trajectory_rows = tuple(trajectories)
    metric_rows = tuple(metric_results)
    if any(item.experiment_id != experiment_id for item in trajectory_rows) or any(
        item.experiment_id != experiment_id for item in metric_rows
    ):
        raise EvaluationError("experiment child ids do not match")
    if any(item.target_id != target.target_id for item in [*trajectory_rows, *metric_rows]):
        raise EvaluationError("experiment target ids do not match")
    statuses = Counter(item.status for item in metric_rows)
    trajectory_statuses = Counter(item.status for item in trajectory_rows)
    if statuses["INFRA_ERROR"] or trajectory_statuses["INFRA_ERROR"]:
        status = "INFRA_ERROR"
    elif statuses["BLOCKED_EVIDENCE"] or trajectory_statuses["BLOCKED_EVIDENCE"]:
        status = "BLOCKED_EVIDENCE"
    elif any(item.status == "FAIL" and item.hard_gate for item in metric_rows):
        status = "FAIL"
    else:
        status = "PASS"
    summary = {
        "trajectory_count": len(trajectory_rows),
        "metric_count": len(metric_rows),
        "trajectory_status_counts": dict(sorted(trajectory_statuses.items())),
        "metric_status_counts": dict(sorted(statuses.items())),
        "hard_gate_failure_count": sum(
            item.status == "FAIL" and item.hard_gate for item in metric_rows
        ),
    }
    result_metadata = dict(metadata or {})
    result_metadata["evaluation_contract_sha256"] = suite.evaluation_contract_sha256
    result_metadata["evaluation_versions"] = evaluation_versions()
    result_metadata["coverage_contract"] = {
        "repetitions": repetitions,
        "scenarios": {
            scenario.scenario_id: {
                "fingerprint_sha256": scenario.fingerprint_sha256(),
                "metric_ids": list(scenario.metric_ids),
            }
            for scenario in suite.scenarios
        },
    }
    return ExperimentResult.from_dict(
        {
            "kind": "experiment_result",
            "schema_version": 1,
            "experiment_id": experiment_id,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at or _now(),
            "persona_fixture_sha256": suite.persona.fixture_sha256,
            "rubric_sha256": suite.rubric_sha256,
            "scenario_fingerprints": suite.scenario_fingerprints,
            "targets": [target.to_dict()],
            "trajectories": [item.to_dict() for item in trajectory_rows],
            "metric_results": [item.to_dict() for item in metric_rows],
            "summary": summary,
            "metadata": result_metadata,
        }
    )


def run_experiment(
    *,
    suite: RegressionSuite,
    target_adapter: Any,
    target: ExperimentTarget,
    experiment_id: str,
    repetitions: int = 3,
    concurrency: int = 3,
    judge: StructuredJudge | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExperimentResult:
    validate_suite_contract(suite)
    if str(getattr(target_adapter, "target_id", "")) != target.target_id:
        raise EvaluationError("target adapter identity does not match target contract")
    started_at = _now()
    trajectories = RegressionRunner(
        target_adapter, max_concurrency=concurrency
    ).run(suite.scenarios, repeats=repetitions, experiment_id=experiment_id)
    scenarios = {scenario.scenario_id: scenario for scenario in suite.scenarios}
    metrics: list[MetricResult] = []
    for trajectory in trajectories:
        metrics.extend(
            evaluate_trajectory(
                trajectory,
                persona=suite.persona,
                scenario=scenarios[trajectory.scenario_id],
                judge=judge,
            )
        )
    return build_experiment_result(
        suite=suite,
        target=target,
        experiment_id=experiment_id,
        trajectories=trajectories,
        metric_results=metrics,
        started_at=started_at,
        repetitions=repetitions,
        metadata=metadata,
    )
