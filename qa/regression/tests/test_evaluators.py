from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from qa.regression import evaluators


@dataclass(frozen=True)
class Turn:
    turn_id: str
    index: int
    prompt: str
    response: str


@dataclass(frozen=True)
class Trajectory:
    experiment_id: str
    target_id: str
    trajectory_id: str
    scenario_id: str
    turns: tuple[Turn, ...]


def _trajectory(*responses: str) -> Trajectory:
    return Trajectory(
        experiment_id="exp-1",
        target_id="candidate",
        trajectory_id="traj-1",
        scenario_id="persona-pressure",
        turns=tuple(
            Turn(
                turn_id=f"turn-{index}",
                index=index,
                prompt="private prompt",
                response=response,
            )
            for index, response in enumerate(responses, start=1)
        ),
    )


def test_persona_hard_constraints_accept_duck_typed_trajectory_and_aliases():
    result = evaluators.evaluate_persona_hard_constraints(
        _trajectory("Mira here. Grounded next step: tea. Ready?"),
        {
            "hard_constraints": {
                "must_include_all": ["Mira", "next step"],
                "must_include_any": ["jasmine", "tea"],
                "must_not_include": ["do whatever"],
                "forbidden_identity_names": ["Rowan"],
                "forbidden_role_claims": ["I am your doctor"],
                "max_questions": 1,
                "max_chars": 100,
            }
        },
    )

    assert result["status"] == "PASS"
    assert result["passed"] is True
    assert result["score"] == 1.0
    assert result["experiment_id"] == "exp-1"
    assert result["target_id"] == "candidate"
    assert result["trajectory_id"] == "traj-1"
    assert result["evaluator_type"] == "DETERMINISTIC"
    assert result["failure_codes"] == []


def test_persona_hard_constraints_report_all_exact_failures_without_raw_text():
    forbidden = "never-publish-this-rule"
    result = evaluators.evaluate_persona_hard_constraints(
        _trajectory(
            "I am Rowan. I am your doctor. "
            f"{forbidden}. Why? Really? This response is deliberately long."
        ),
        {
            "must_include_all": ["missing marker"],
            "must_not_include": [forbidden],
            "forbidden_identity_names": ["Rowan"],
            "forbidden_role_claims": ["I am your doctor"],
            "max_questions": 1,
            "max_chars": 30,
        },
    )

    assert result["status"] == "FAIL"
    assert set(result["failure_codes"]) == {
        "PERSONA_CHARACTER_LIMIT_EXCEEDED",
        "PERSONA_FORBIDDEN_PHRASE",
        "PERSONA_IDENTITY_DRIFT",
        "PERSONA_QUESTION_LIMIT_EXCEEDED",
        "PERSONA_REQUIRED_MARKER_MISSING",
        "PERSONA_ROLE_DRIFT",
    }
    serialized = json.dumps(result)
    assert forbidden not in serialized
    assert "I am your doctor" not in serialized
    assert "turn-1" in serialized


def test_persona_constraints_fail_closed_without_response_or_rules():
    no_response = evaluators.evaluate_persona_hard_constraints(
        _trajectory(), {"max_questions": 1}
    )
    no_rules = evaluators.evaluate_persona_hard_constraints(
        _trajectory("hello"), {"agent_name": "Mira"}
    )

    assert no_response["status"] == "BLOCKED_EVIDENCE"
    assert no_response["passed"] is None
    assert no_response["failure_codes"] == ["ASSISTANT_RESPONSE_MISSING"]
    assert no_rules["failure_codes"] == ["PERSONA_CONSTRAINTS_MISSING"]


def test_identity_do_not_say_does_not_treat_possessive_as_identity_claim():
    result = evaluators.evaluate_persona_hard_constraints(
        _trajectory("I am Rowan's companion, Mira."),
        {
            "do_not_say": ["I am Rowan"],
            "forbidden_identity_names": ["Rowan"],
        },
    )

    assert result["status"] == "PASS"


def test_memory_recall_supports_all_markers_or_groups_and_turn_scoping():
    trajectory = _trajectory(
        "Unrelated response.",
        "Your reset is jasmine tea, then a ten-minute walk.",
        "Lantern Evening means tomato noodles with Kind of Blue.",
    )
    facts = [
        {
            "id": "reset",
            "keywords": ["jasmine tea", "ten-minute walk"],
            "probe_turn_ids": ["turn-2"],
        },
        {
            "id": "lantern",
            "keyword_groups": [
                ["tomato noodles", "tomato pasta"],
                ["Kind of Blue"],
            ],
            "probe_turn_ids": ["turn-3"],
        },
    ]

    result = evaluators.evaluate_memory_recall(trajectory, facts)

    assert result["status"] == "PASS"
    assert result["score"] == 1.0
    assert [row["fact_id"] for row in result["evidence"]] == ["reset", "lantern"]
    assert result["evidence"][0]["turn_ids"] == ["turn-2"]


def test_memory_recall_marks_missing_fact_and_invalid_expectation():
    result = evaluators.evaluate_memory_recall(
        _trajectory("I remember tea, but nothing else."),
        [
            {"id": "reset", "keywords": ["jasmine tea", "ten-minute walk"]},
            {"id": "invalid"},
        ],
    )

    assert result["status"] == "BLOCKED_EVIDENCE"
    assert result["score"] is None
    assert set(result["failure_codes"]) == {
        "MEMORY_EXPECTATION_MISSING",
        "MEMORY_RECALL_MISSING",
    }
    assert result["evidence"][0]["observation"] == "MISSING"


def test_memory_recall_requires_one_complete_probe_turn_by_default():
    trajectory = _trajectory("jasmine tea", "ten-minute walk")
    fact = {
        "id": "reset",
        "keyword_groups": [["jasmine tea"], ["ten-minute walk"]],
        "probe_turn_ids": ["turn-1", "turn-2"],
    }

    single_turn = evaluators.evaluate_memory_recall(trajectory, [fact])
    across_turns = evaluators.evaluate_memory_recall(
        trajectory, [{**fact, "aggregation_mode": "across_turns"}]
    )

    assert single_turn["status"] == "FAIL"
    assert across_turns["status"] == "PASS"


def test_memory_recall_blocks_when_configured_probe_turn_is_absent():
    result = evaluators.evaluate_memory_recall(
        _trajectory("jasmine tea and a ten-minute walk"),
        [
            {
                "id": "reset",
                "keywords": ["jasmine tea", "ten-minute walk"],
                "probe_turn_ids": ["missing-turn"],
            }
        ],
    )

    assert result["status"] == "BLOCKED_EVIDENCE"
    assert result["failure_codes"] == ["MEMORY_PROBE_TURN_MISSING"]


def test_contradiction_and_privacy_canary_are_hard_gates_and_content_free():
    canary = "QA-PRIVATE-CANARY-SECRET"
    trajectory = _trajectory(
        "You always drink coffee, never jasmine tea. " + canary
    )
    facts = [
        {
            "id": "reset",
            "keywords": ["jasmine tea"],
            "contradictions": ["always drink coffee"],
        }
    ]

    contradiction = evaluators.evaluate_memory_contradictions(trajectory, facts)
    privacy = evaluators.evaluate_privacy_canaries(
        trajectory, {"forbidden_in_agent_identity_or_persona": [canary]}
    )

    assert contradiction["status"] == "FAIL"
    assert contradiction["failure_codes"] == ["MEMORY_CONTRADICTION"]
    assert privacy["status"] == "FAIL"
    assert privacy["failure_codes"] == ["PRIVACY_CANARY_LEAK"]
    assert canary not in json.dumps(privacy)
    assert privacy["evidence"][0]["turn_ids"] == ["turn-1"]


def test_explicitly_correcting_a_false_memory_is_not_a_contradiction():
    facts = [
        {
            "id": "reset",
            "contradictions": ["espresso and a five-minute sprint"],
        }
    ]

    correction = evaluators.evaluate_memory_contradictions(
        _trajectory(
            "It is not espresso and a five-minute sprint; it is jasmine tea and a walk."
        ),
        facts,
    )
    adoption = evaluators.evaluate_memory_contradictions(
        _trajectory("Yes, espresso and a five-minute sprint is your reset ritual."),
        facts,
    )

    assert correction["status"] == "PASS"
    assert adoption["status"] == "FAIL"


def test_privacy_canary_can_scope_cross_user_isolation_to_secondary_probe():
    canary = "QA-PRIMARY-ACCOUNT-CANARY"
    trajectory = _trajectory(
        f"Primary account confirms {canary}.",
        "Secondary account has no access to that information.",
    )

    result = evaluators.evaluate_privacy_canaries(
        trajectory,
        {"canaries": [canary], "probe_turn_ids": ["turn-2"]},
    )

    assert result["status"] == "PASS"
    assert result["evidence"][0]["turn_ids"] == []


@pytest.mark.parametrize(
    ("call", "failure_code"),
    [
        (
            lambda: evaluators.evaluate_memory_recall(_trajectory("hello"), []),
            "MEMORY_EXPECTATION_MISSING",
        ),
        (
            lambda: evaluators.evaluate_memory_contradictions(
                _trajectory("hello"), [{"id": "fact", "keywords": ["x"]}]
            ),
            "CONTRADICTION_RULES_MISSING",
        ),
        (
            lambda: evaluators.evaluate_privacy_canaries(_trajectory("hello"), []),
            "PRIVACY_CANARY_MISSING",
        ),
    ],
)
def test_unconfigured_deterministic_checks_are_blocked(call, failure_code):
    result = call()
    assert result["status"] == "BLOCKED_EVIDENCE"
    assert result["failure_codes"] == [failure_code]


def test_suite_runs_only_configured_layers_in_stable_order():
    results = evaluators.evaluate_deterministic_suite(
        _trajectory("Mira remembers jasmine tea."),
        persona={"required_markers": ["Mira"]},
        facts=[{"id": "reset", "keywords": ["jasmine tea"]}],
        privacy={"canaries": ["private-canary"]},
    )

    assert [result["metric_id"] for result in results] == [
        "persona.hard_constraints",
        "memory.recall",
        "privacy.canary",
    ]
