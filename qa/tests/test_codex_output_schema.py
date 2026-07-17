from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from qa import codex_output_schema as compat
from qa.tests.test_validate_run import _valid_result


QA_DIR = Path(__file__).resolve().parents[1]
GATE_SCHEMA_PATH = QA_DIR / "schemas" / "run-result.schema.json"
AUTHORING_SCHEMA_PATH = QA_DIR / "schemas" / "codex-run-result.schema.json"
COVERAGE_PATH = QA_DIR / "coverage-lock.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_checked_in_authoring_schema_is_exact_compatible_projection():
    gate_schema = _load(GATE_SCHEMA_PATH)
    coverage = _load(COVERAGE_PATH)
    authoring_schema = _load(AUTHORING_SCHEMA_PATH)

    assert authoring_schema == compat.build_authoring_schema(gate_schema, coverage)
    assert compat.validate_authoring_schema(authoring_schema) == []


def test_gate_valid_result_is_valid_authoring_output_offline():
    result = _valid_result()
    gate_errors = list(
        Draft202012Validator(_load(GATE_SCHEMA_PATH)).iter_errors(result)
    )
    authoring_errors = list(
        Draft202012Validator(_load(AUTHORING_SCHEMA_PATH)).iter_errors(result)
    )

    assert gate_errors == []
    assert authoring_errors == []


def test_authoring_schema_locks_eight_profiles_and_unicode_model_shape():
    schema = _load(AUTHORING_SCHEMA_PATH)
    profile = schema["$defs"]["profileResult"]
    reasoning = schema["$defs"]["reasoningEvidence"]

    assert schema["properties"]["profiles_expected"]["enum"] == [8]
    assert profile["properties"]["profile_id"]["enum"] == [
        "official-deepseek",
        "official-anthropic",
        "official-openai",
        "official-gemini",
        "openrouter-claude",
        "openrouter-openai",
        "openrouter-glm",
        "relay-kongbeiqie",
    ]
    assert profile["properties"]["model"] == {"$ref": "#/$defs/modelLabel"}
    assert reasoning["properties"]["model"] == {"$ref": "#/$defs/nullableModelLabel"}


def test_gate_only_condition_remains_authoritative_after_authoring():
    result = _valid_result()
    result["profiles"][0]["scenarios"][0]["failure"] = {
        "category": "PRODUCT_FAIL",
        "stage_code": "PREFLIGHT",
        "failure_code": "PRECONDITION_MISSING",
        "reproducible": True,
    }

    assert (
        list(Draft202012Validator(_load(AUTHORING_SCHEMA_PATH)).iter_errors(result))
        == []
    )
    assert list(Draft202012Validator(_load(GATE_SCHEMA_PATH)).iter_errors(result))


def test_assertion_map_projection_uses_all_locked_exact_key_sets():
    coverage = _load(COVERAGE_PATH)
    schema = _load(AUTHORING_SCHEMA_PATH)
    alternatives = schema["$defs"]["scenarioResult"]["properties"]["assertions"][
        "anyOf"
    ]

    expected = [
        coverage["scenario_contracts"][scenario_id]["required_assertions"]
        for scenario_id in coverage["required_scenarios"]
    ]
    assert [item["required"] for item in alternatives] == expected
    for item in alternatives:
        assert item["additionalProperties"] is False
        assert list(item["properties"]) == item["required"]
        assert all(
            value == {"type": "boolean"} for value in item["properties"].values()
        )


def test_authoring_schema_requires_bound_persona_and_reasoning_evidence():
    schema = _load(AUTHORING_SCHEMA_PATH)
    scenario = schema["$defs"]["scenarioResult"]
    reasoning = schema["$defs"]["reasoningEvidence"]
    finalizer = schema["$defs"]["personaFinalizerEvidence"]

    assert "persona_finalizer" in scenario["required"]
    assert scenario["properties"]["persona_finalizer"] == {
        "anyOf": [
            {"$ref": "#/$defs/personaFinalizerEvidence"},
            {"type": "null"},
        ]
    }
    assert all(
        field in reasoning["required"]
        for field in (
            "capability_enabled",
            "requested_effort",
            "configured_effort",
            "effective_effort",
            "reasoning_event_count",
            "request_id",
            "turn_id",
            "trace_id",
        )
    )
    assert reasoning["properties"]["capability_enabled"] == {"type": "boolean"}
    for field in ("requested_effort", "configured_effort", "effective_effort"):
        assert reasoning["properties"][field]["enum"] == [
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "unsupported",
            "unknown",
        ]
    assert reasoning["properties"]["reasoning_event_count"] == {"type": "integer"}
    assert reasoning["properties"]["raw_private_reasoning_stored"] == {
        "type": "boolean"
    }
    assert finalizer["properties"]["semantic_judgment_bound"]["enum"] == [True]
    # The authoring/diagnostic schema must retain a bounded negative semantic
    # verdict.  The release validator still requires this field to be true.
    assert finalizer["properties"]["finalizer_ok"] == {"type": "boolean"}
    assert finalizer["properties"]["private_evidence_deleted"]["enum"] == [True]
    assert finalizer["properties"]["privacy_violation_count"] == {
        "type": "integer"
    }


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda schema: schema.update(allOf=[{"type": "object"}]),
            "unsupported",
        ),
        (
            lambda schema: schema["properties"]["run_id"].update(
                oneOf=[{"type": "string"}]
            ),
            "unsupported",
        ),
        (
            lambda schema: schema.pop("additionalProperties"),
            "additionalProperties",
        ),
        (
            lambda schema: schema["required"].remove("run_id"),
            "required",
        ),
        (
            lambda schema: schema["properties"].update(run_id={}),
            "type, $ref, or anyOf",
        ),
    ],
)
def test_compatibility_checker_rejects_known_structured_output_blockers(
    mutate, expected
):
    schema = deepcopy(_load(AUTHORING_SCHEMA_PATH))
    mutate(schema)

    assert any(expected in error for error in compat.validate_authoring_schema(schema))


def test_projection_rejects_new_unmodeled_gate_conditionals():
    gate_schema = _load(GATE_SCHEMA_PATH)
    gate_schema["$defs"]["target"]["allOf"] = [{"type": "object"}]

    with pytest.raises(compat.AuthoringSchemaError, match="explicit projection"):
        compat.build_authoring_schema(gate_schema, _load(COVERAGE_PATH))
