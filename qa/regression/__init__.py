"""Lightweight persona and memory regression framework."""

from .contracts import (
    SCHEMA_VERSION,
    ContractError,
    Experiment,
    ExperimentResult,
    ExperimentTarget,
    GoldenPersona,
    MetricResult,
    Scenario,
    Trajectory,
    Transition,
    Turn,
    TurnEvidence,
    canonical_json_bytes,
    canonical_json_sha256,
)

__all__ = [
    "SCHEMA_VERSION", "ContractError", "canonical_json_bytes", "canonical_json_sha256",
    "Transition", "Turn", "GoldenPersona", "Scenario", "TurnEvidence", "Trajectory",
    "MetricResult", "ExperimentTarget", "Experiment", "ExperimentResult",
]
