"""Explicit measurement versions used to make old/new evals comparable."""

from __future__ import annotations


HARNESS_CONTRACT_VERSION = "1.0.0"
RUNNER_EVIDENCE_VERSION = "1.0.0"

# Bump one entry whenever its normalization, matching, prompt, or scoring
# semantics change.  Baseline comparison fails closed across version changes.
METRIC_VERSIONS = {
    "memory.contradiction": "2",
    "memory.recall": "1",
    "persona.hard_constraints": "2",
    "privacy.canary": "1",
}
SEMANTIC_METRIC_VERSION = "1"


def metric_version(metric_id: str, evaluator_type: str) -> str:
    if evaluator_type == "DETERMINISTIC":
        return METRIC_VERSIONS.get(metric_id, "UNVERSIONED")
    if evaluator_type == "LLM_JUDGE":
        return SEMANTIC_METRIC_VERSION
    return "UNVERSIONED"


def evaluation_versions() -> dict[str, object]:
    return {
        "harness_contract_version": HARNESS_CONTRACT_VERSION,
        "runner_evidence_version": RUNNER_EVIDENCE_VERSION,
        "deterministic_metric_versions": dict(sorted(METRIC_VERSIONS.items())),
        "semantic_metric_version": SEMANTIC_METRIC_VERSION,
    }
