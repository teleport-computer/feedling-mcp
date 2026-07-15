from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from qa.regression.contracts import ExperimentTarget
from qa.regression.engine import build_experiment_result, evaluate_trajectory
from qa.regression.judge import CallbackJudge
from qa.regression.runner import run_scenario
from qa.regression.scenario_loader import RegressionSuite, load_suite_directory
from qa.regression.target import (
    BoundaryResult,
    TargetCapabilities,
    TargetContext,
    TargetResponse,
    TargetSession,
)


ROOT = Path(__file__).resolve().parents[3]


class ScriptedTarget:
    target_id = "candidate"

    def __init__(self, responses, *, runtime_rotation=False):
        self.responses = dict(responses)
        self.sent = []
        self._capabilities = TargetCapabilities(
            clear_history=True, runtime_session_rotation=runtime_rotation
        )

    @property
    def capabilities(self):
        return self._capabilities

    def open_session(self, context: TargetContext):
        return TargetSession(
            target_id=self.target_id,
            session_key=context.session_key,
            session_id=f"session-{context.session_key}",
        )

    def send(self, session, *, turn_id, prompt, timeout_seconds):
        self.sent.append(turn_id)
        return TargetResponse(
            text=self.responses[turn_id],
            request_id=f"request-{turn_id}",
            response_id=f"response-{turn_id}",
            trace_id=f"trace-{turn_id}",
            latency_ms=1.0,
        )

    def apply_boundary(self, session, *, action):
        if action == "clear_history":
            return BoundaryResult(
                session=replace(session, generation=session.generation + 1),
                action=action,
                boundary_kind="transcript",
                runtime_session_rotated=False,
                evidence={"transcript_cleared": True},
            )
        raise AssertionError(action)

    def close_session(self, session):
        return None


def _suite():
    return load_suite_directory(
        ROOT / "qa/regression/fixtures/golden-persona-mira-v1.json",
        ROOT / "qa/regression/scenarios",
    )


def test_deterministic_memory_checks_pass_after_transcript_clear_and_judge_is_blocked():
    suite = _suite()
    scenario = next(
        item for item in suite.scenarios if item.scenario_id == "imported-memory-after-clear"
    )
    target = ScriptedTarget(
        {
            "recall-imported-facts": (
                "Your reset is jasmine tea and a ten-minute walk. Lantern Evening "
                "is tomato noodles with Kind of Blue."
            )
        }
    )
    trajectory = run_scenario(
        scenario, target, experiment_id="experiment-1", repeat_index=0
    )

    rows = evaluate_trajectory(
        trajectory, persona=suite.persona, scenario=scenario, judge=None
    )

    assert trajectory.status == "COMPLETED"
    assert [item.metric_id for item in rows] == list(scenario.metric_ids)
    assert {item.metric_id: item.status for item in rows} == {
        "memory.recall": "PASS",
        "memory.contradiction": "PASS",
        "privacy.canary": "PASS",
        "memory.natural_recall": "BLOCKED_EVIDENCE",
    }
    assert rows[-1].failure_codes == ("JUDGE_NOT_CONFIGURED",)


def test_strong_memory_scenario_blocks_before_mutation_without_rotation_proof():
    suite = _suite()
    scenario = next(
        item for item in suite.scenarios if item.scenario_id == "learned-memory-after-rotation"
    )
    target = ScriptedTarget({})

    trajectory = run_scenario(scenario, target, experiment_id="experiment-1")
    rows = evaluate_trajectory(
        trajectory, persona=suite.persona, scenario=scenario, judge=None
    )

    assert trajectory.status == "BLOCKED_EVIDENCE"
    assert trajectory.failure_code == "SESSION_BOUNDARY_UNPROVEN"
    assert target.sent == []
    assert {item.status for item in rows} == {"BLOCKED_EVIDENCE"}
    assert all(
        item.failure_codes == ("SESSION_BOUNDARY_UNPROVEN",) for item in rows
    )


def test_structured_judge_metrics_are_hash_bound_and_build_experiment_result():
    suite = _suite()
    scenario = next(
        item for item in suite.scenarios if item.scenario_id == "persona-stability"
    )
    target = ScriptedTarget(
        {
            "identity": "I am Mira, your steady companion.",
            "vulnerable-support": "That sounds draining. Try one slow cup of tea.",
            "persona-pressure": "I am still Mira, not Rowan.",
            "verbosity-pressure": "I will keep it brief: take one small pause.",
        }
    )
    trajectory = run_scenario(scenario, target, experiment_id="experiment-1")

    def judge_callback(request):
        return {
            "kind": "persona_memory_judge_result",
            "schema_version": 1,
            "judge_id": "stable-judge-v1",
            "evidence_sha256": request["evidence_sha256"],
            "rubric_sha256": request["rubric_sha256"],
            "status": "PASS",
            "metrics": [
                {
                    "metric_id": spec["metric_id"],
                    "score": 0.95,
                    "passed": True,
                    "failure_codes": [],
                    "evidence_turn_ids": ["identity", "persona-pressure"],
                    "rationale": "The cited turns preserve the locked persona.",
                }
                for spec in request["metrics"]
            ],
            "metadata": {"temperature": 0},
        }

    rows = evaluate_trajectory(
        trajectory,
        persona=suite.persona,
        scenario=scenario,
        judge=CallbackJudge(
            judge_id="stable-judge-v1",
            configuration_id="engine-test-v1",
            callback=judge_callback,
        ),
    )
    target_contract = ExperimentTarget(
        target_id="candidate",
        label="candidate",
        base_url="https://test-api.feedling.app",
        build_sha="a" * 40,
        runtime_mode="deployed_current",
        provider="openai",
        model="test-model",
    )
    selected_suite = RegressionSuite(persona=suite.persona, scenarios=(scenario,))
    result = build_experiment_result(
        suite=selected_suite,
        target=target_contract,
        experiment_id="experiment-1",
        trajectories=[trajectory],
        metric_results=rows,
        started_at=trajectory.started_at,
        repetitions=1,
        finished_at=trajectory.finished_at,
    )

    assert {item.status for item in rows} == {"PASS"}
    assert all(
        len(item.metadata["judge_configuration_sha256"]) == 64
        for item in rows
        if item.evaluator_type == "LLM_JUDGE"
    )
    assert result.status == "PASS"
    assert result.persona_fixture_sha256 == suite.persona.fixture_sha256
    assert result.metadata["evaluation_contract_sha256"] == (
        selected_suite.evaluation_contract_sha256
    )
