#!/usr/bin/env python3
"""Build a team-safe GitHub qualification report from trusted run evidence.

The canonical result contains synthetic user and correlation identifiers that
must not cross the broadly readable artifact boundary.  This module validates
the canonical result and locked coverage, binds it to deterministic cleanup,
and emits a fresh, owner-only directory containing only allowlisted structured
evidence.  Raw chat, persona, trace, reasoning, prompt, response, and rationale
content are never copied.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qa import render_artifacts as release_renderer  # noqa: E402
from qa import validate_cleanup_receipt as cleanup_validator  # noqa: E402
from qa import validate_run as release_gate  # noqa: E402


MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_SUMMARY_BYTES = 512 * 1024
OUTPUT_FILES = frozenset(
    {
        "run-index.json",
        "failure-index.json",
        "team-summary.md",
        "matrix.md",
        "latency.csv",
        "junit.xml",
        "cleanup-receipt.json",
        "memory-contract.json",
        "persona-memory-summary.json",
        "persona-memory-matrix.md",
    }
)
SOURCE_FILES = ("memory-contract.json",)
TRACE_STAGES = ("routing", "queue", "provider", "persistence", "delivery")
STATUS_SEVERITY = {
    "PASS": 0,
    "BLOCKED_EVIDENCE": 1,
    "BLOCKED_CREDENTIAL": 2,
    "BLOCKED_DEPLOYMENT": 3,
    "AGENT_ERROR": 4,
    "PRODUCT_FAIL": 5,
    "SECURITY_FAIL": 6,
}
SCENARIO_TITLES = {
    "P0-01": "Test target and credential preflight",
    "P0-02": "Fresh account onboarding",
    "P0-03": "Invalid provider-key rejection",
    "P0-04": "Valid provider-key recovery",
    "P0-05": "Runtime discovery and readiness",
    "P0-06": "Persona import and distillation",
    "P0-07": "Hosted activation and live loop",
    "P0-08": "Basic chat and acknowledgement",
    "P0-09": "Ten-turn delivery reliability",
    "P0-10": "Memory and persona continuity",
    "P0-11": "Model and agent identity",
    "P0-12": "Reasoning delivery",
    "P0-13": "Trace, latency, and cleanup",
}
TRIAGE_BY_STAGE = {
    "PREFLIGHT": ("qualification-control", "qa-platform", "CHECK_PREFLIGHT", "HIGH"),
    "PROVISIONER": (
        "account-provisioning",
        "qa-platform",
        "CHECK_PROVISIONING_RECEIPT",
        "HIGH",
    ),
    "SUPERVISOR": (
        "qualification-control",
        "qa-platform",
        "CHECK_SUPERVISOR_OUTPUT",
        "HIGH",
    ),
    "ONBOARDING": ("account-lifecycle", "backend-api", "RERUN_ONBOARDING", "HIGH"),
    "INVALID_KEY_VALIDATION": (
        "provider-configuration",
        "backend-api",
        "CHECK_INVALID_KEY_PATH",
        "HIGH",
    ),
    "VALID_KEY_RECOVERY": (
        "provider-configuration",
        "backend-api",
        "CHECK_VALID_KEY_PATH",
        "HIGH",
    ),
    "RUNTIME_SELECTION": (
        "runtime-control-plane",
        "runtime",
        "CHECK_RUNTIME_READBACK",
        "HIGH",
    ),
    "ACTIVATION": ("runtime-activation", "runtime", "CHECK_ACTIVATION_TRACE", "HIGH"),
    "PERSONA_IMPORT": (
        "persona-distillation",
        "genesis",
        "RERUN_PERSONA_IMPORT",
        "HIGH",
    ),
    "BASIC_CHAT": (
        "chat-delivery",
        "runtime-provider",
        "CHECK_TURN_TIMELINE",
        "MEDIUM",
    ),
    "RELIABILITY_CHAT": (
        "chat-delivery",
        "runtime-provider",
        "RERUN_RELIABILITY_CHAT",
        "MEDIUM",
    ),
    "MEMORY_PERSONA": (
        "memory-persona",
        "memory-runtime",
        "RERUN_MEMORY_PERSONA",
        "MEDIUM",
    ),
    "IDENTITY": (
        "provider-routing",
        "provider-adapter",
        "CHECK_ROUTE_IDENTITY",
        "HIGH",
    ),
    "REASONING": (
        "reasoning-delivery",
        "provider-adapter",
        "CHECK_REASONING_RECEIPT",
        "HIGH",
    ),
    "TRACE_LATENCY_CLEANUP": (
        "runtime-observability",
        "runtime",
        "CHECK_MISSING_TRACE_STAGES",
        "HIGH",
    ),
    "CLEANUP": ("synthetic-cleanup", "qa-platform", "CHECK_CLEANUP_RECEIPT", "HIGH"),
}

# Evidence that has an unambiguous assertion counterpart.  This is used only to
# diagnose contradictions in the report, never to change a scenario outcome.
ASSERTION_EVIDENCE = {
    "target_is_test": "TARGET_TEST_CONFIRMED",
    "deployed_endpoint_reachable": "DEPLOYED_ENDPOINT_REACHABLE",
    "provisioning_receipts_confirmed": "PROVISIONING_RECEIPTS_CONFIRMED",
    "agent_environment_sanitized": "AGENT_ENVIRONMENT_SANITIZED",
    "contract_inputs_readable": "CONTRACT_INPUTS_READABLE",
    "credentials_omitted": "CREDENTIAL_OMITTED",
    "synthetic_account_is_fresh": "SYNTHETIC_ACCOUNT_FRESH",
    "whoami_matches": "WHOAMI_MATCHED",
    "trace_cleared": "TRACE_CLEARED",
    "invalid_key_rejected": "INVALID_KEY_REJECTED",
    "invalid_key_not_echoed": "INVALID_KEY_NOT_ECHOED",
    "hosted_chat_not_started": "HOSTED_CHAT_NOT_STARTED",
    "valid_key_accepted": "VALID_KEY_ACCEPTED",
    "provider_config_matches": "PROVIDER_CONFIG_MATCHED",
    "credential_omitted": "CREDENTIAL_OMITTED",
    "runtime_status_readback_succeeds": "RUNTIME_STATUS_READBACK_SUCCEEDED",
    "runtime_configured": "RUNTIME_CONFIGURED",
    "runtime_metadata_recorded": "RUNTIME_METADATA_RECORDED",
    "persona_files_archived": "PERSONA_FILES_ARCHIVED",
    "persona_source_metadata_verified": "PERSONA_SOURCE_METADATA_VERIFIED",
    "persona_import_done": "PERSONA_IMPORT_DONE",
    "persona_acceptance_passed": "PERSONA_ACCEPTANCE_PASSED",
    "privacy_canary_absent": "PRIVACY_CANARY_ABSENT",
    "driver_enabled": "DRIVER_ENABLED",
    "chat_loop_verified": "CHAT_LOOP_VERIFIED",
    "no_orphan_turn": "NO_ORPHAN_TURN",
    "async_ack_received": "ASYNC_ACK_RECEIVED",
    "exact_reply_correlated": "EXACT_REPLY_CORRELATED",
    "latency_recorded": "LATENCY_ATTRIBUTED",
    "ten_turns_ordered": "TEN_TURNS_ORDERED",
    "exact_replies_correlated": "EXACT_REPLY_CORRELATED",
    "memory_recall_confirmed": "MEMORY_RECALL_CONFIRMED",
    "imported_memory_recalled": "MEMORY_RECALL_CONFIRMED",
    "persona_consistency_confirmed": "PERSONA_CONSISTENCY_CONFIRMED",
    "agent_identity_confirmed": "AGENT_IDENTITY_CONFIRMED",
    "model_route_confirmed": "MODEL_ROUTE_CONFIRMED",
    "trace_route_correlated": "TRACE_CORRELATION_CONFIRMED",
    "reasoning_capability_enabled": "REASONING_CAPABILITY_CONFIRMED",
    "reasoning_configured_effort_medium": "REASONING_CONFIGURATION_CONFIRMED",
    "reasoning_effective_effort_not_attested": "REASONING_EFFECTIVE_EFFORT_UNATTESTED",
    "reasoning_event_observed": "REASONING_EVENT_CONFIRMED",
    "reasoning_metadata_present": "REASONING_METADATA_CONFIRMED",
    "reasoning_tokens_present": "EXPLICIT_REASONING_TOKEN_COUNT_CONFIRMED",
    "user_disclosure_present": "DISCLOSURE_PRESENT",
    "trace_correlation_confirmed": "TRACE_CORRELATION_CONFIRMED",
    "latency_attributed": "LATENCY_ATTRIBUTED",
    "cleanup_confirmed": "CLEANUP_CONFIRMED",
}
ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DEPLOYMENT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}$")
PERSONA_PIPELINE_OUTCOMES = frozenset({"success", "failure", "skipped", "cancelled"})
PERSONA_STATUS_VALUES = frozenset(
    {
        "PASS",
        "FAIL",
        "BLOCKED_EVIDENCE",
        "INFRA_ERROR",
        "NOT_FORMALLY_QUALIFIED",
    }
)
PERSONA_METRIC_STATUS_VALUES = frozenset(
    {"PASS", "FAIL", "BLOCKED_EVIDENCE", "INFRA_ERROR", "SKIP"}
)
PERSONA_STATUS_SEVERITY = {
    "NOT_FORMALLY_QUALIFIED": 0,
    "PASS": 0,
    "FAIL": 1,
    "BLOCKED_EVIDENCE": 2,
    "INFRA_ERROR": 3,
}
PERSONA_TO_OVERALL_STATUS = {
    "NOT_FORMALLY_QUALIFIED": "PASS",
    "PASS": "PASS",
    "FAIL": "PRODUCT_FAIL",
    "BLOCKED_EVIDENCE": "BLOCKED_EVIDENCE",
    "INFRA_ERROR": "AGENT_ERROR",
}
PERSONA_SCENARIO_IDS = (
    "contradiction-resistance",
    "cross-user-memory-isolation",
    "imported-memory-after-clear",
    "learned-memory-after-rotation",
    "long-horizon-persona-memory",
    "persona-stability",
    "privacy-canary",
    "unknown-memory-honesty",
)
PERSONA_SCENARIO_VERSION = "1.0.0"


class TeamReportError(RuntimeError):
    """Fixed team-report failure safe to print in CI."""


def _read_regular(path: Path, label: str, *, max_bytes: int = MAX_INPUT_BYTES) -> bytes:
    try:
        metadata = path.lstat()
    except OSError:
        raise TeamReportError(f"{label} is missing or unsafe") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > max_bytes
    ):
        raise TeamReportError(f"{label} is missing or unsafe")
    try:
        return path.read_bytes()
    except OSError:
        raise TeamReportError(f"{label} is missing or unsafe") from None


def _read_json(path: Path, label: str, *, max_bytes: int = MAX_INPUT_BYTES) -> Any:
    try:
        return json.loads(
            _read_regular(path, label, max_bytes=max_bytes).decode("utf-8")
        )
    except TeamReportError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise TeamReportError(f"{label} is invalid") from None


def _source_root(path: Path) -> Path:
    if path.is_symlink():
        raise TeamReportError("source artifact root is unsafe")
    try:
        root = path.resolve(strict=True)
        metadata = path.lstat()
    except (OSError, RuntimeError):
        raise TeamReportError("source artifact root is unsafe") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise TeamReportError("source artifact root is unsafe")
    for name in SOURCE_FILES:
        _read_regular(root / name, "source artifact")
    return root


def _exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise TeamReportError(f"{label} is invalid")
    return value


def _safe_token(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or SAFE_TOKEN_RE.fullmatch(value) is None:
        raise TeamReportError(f"{label} is invalid")
    return value


def _safe_count(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise TeamReportError(f"{label} is invalid")
    return value


def _safe_number(
    value: Any, label: str, *, nullable: bool = False
) -> float | int | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TeamReportError(f"{label} is invalid")
    if not math.isfinite(float(value)):
        raise TeamReportError(f"{label} is invalid")
    return value


def _persona_metric(value: Any) -> dict[str, Any]:
    row = _exact_keys(
        value,
        {
            "metric_id",
            "metric_version",
            "evaluator_type",
            "hard_gate",
            "status",
            "result_count",
            "pass_count",
            "fail_count",
            "blocked_count",
            "infra_error_count",
            "pass_rate",
            "average_score",
            "threshold",
            "failure_codes",
            "rubric_sha256",
        },
        "persona-memory metric",
    )
    status = row["status"]
    failure_codes = row["failure_codes"]
    if (
        status not in PERSONA_METRIC_STATUS_VALUES
        or row["evaluator_type"] not in {"DETERMINISTIC", "LLM_JUDGE"}
        or type(row["hard_gate"]) is not bool
        or not isinstance(failure_codes, list)
        or any(
            not isinstance(code, str) or SAFE_TOKEN_RE.fullmatch(code) is None
            for code in failure_codes
        )
        or not isinstance(row["rubric_sha256"], str)
        or SHA_RE.fullmatch(row["rubric_sha256"]) is None
    ):
        raise TeamReportError("persona-memory metric is invalid")
    projected = {
        "metric_id": _safe_token(row["metric_id"], "persona-memory metric"),
        "metric_version": _safe_token(row["metric_version"], "persona-memory metric"),
        "evaluator_type": row["evaluator_type"],
        "hard_gate": row["hard_gate"],
        "status": status,
        "result_count": _safe_count(row["result_count"], "persona-memory metric"),
        "pass_count": _safe_count(row["pass_count"], "persona-memory metric"),
        "fail_count": _safe_count(row["fail_count"], "persona-memory metric"),
        "blocked_count": _safe_count(row["blocked_count"], "persona-memory metric"),
        "infra_error_count": _safe_count(
            row["infra_error_count"], "persona-memory metric"
        ),
        "pass_rate": _safe_number(row["pass_rate"], "persona-memory metric"),
        "average_score": _safe_number(
            row["average_score"], "persona-memory metric", nullable=True
        ),
        "threshold": _safe_number(row["threshold"], "persona-memory metric"),
        "failure_codes": list(failure_codes),
        "rubric_sha256": row["rubric_sha256"],
    }
    if (
        projected["result_count"] <= 0
        or projected["pass_count"]
        + projected["fail_count"]
        + projected["blocked_count"]
        + projected["infra_error_count"]
        != projected["result_count"]
        or not 0 <= float(projected["pass_rate"]) <= 1
        or not math.isclose(
            float(projected["pass_rate"]),
            projected["pass_count"] / projected["result_count"],
            abs_tol=1e-6,
        )
    ):
        raise TeamReportError("persona-memory metric is invalid")
    return projected


def _persona_scenario(value: Any) -> dict[str, Any]:
    row = _exact_keys(
        value,
        {
            "scenario_id",
            "scenario_version",
            "scenario_sha256",
            "status",
            "trajectory_count",
            "trajectory_status_counts",
            "metrics",
        },
        "persona-memory scenario",
    )
    status_counts = row["trajectory_status_counts"]
    metrics = row["metrics"]
    if (
        row["status"] not in PERSONA_STATUS_VALUES
        or not isinstance(row["scenario_sha256"], str)
        or SHA_RE.fullmatch(row["scenario_sha256"]) is None
        or not isinstance(status_counts, Mapping)
        or any(
            not isinstance(key, str)
            or SAFE_TOKEN_RE.fullmatch(key) is None
            or type(count) is not int
            or count < 0
            for key, count in status_counts.items()
        )
        or not isinstance(metrics, list)
        or not metrics
    ):
        raise TeamReportError("persona-memory scenario is invalid")
    projected_metrics = [_persona_metric(metric) for metric in metrics]
    if any(metric["status"] == "INFRA_ERROR" for metric in projected_metrics):
        derived_status = "INFRA_ERROR"
    elif any(
        metric["status"] in {"BLOCKED_EVIDENCE", "SKIP"} for metric in projected_metrics
    ):
        derived_status = "BLOCKED_EVIDENCE"
    elif any(metric["status"] == "FAIL" for metric in projected_metrics):
        derived_status = "FAIL"
    else:
        derived_status = "PASS"
    if (
        sum(status_counts.values()) != row["trajectory_count"]
        or row["status"] != derived_status
    ):
        raise TeamReportError("persona-memory scenario is invalid")
    return {
        "scenario_id": _safe_token(row["scenario_id"], "persona-memory scenario"),
        "scenario_version": _safe_token(
            row["scenario_version"], "persona-memory scenario"
        ),
        "scenario_sha256": row["scenario_sha256"],
        "status": row["status"],
        "trajectory_count": _safe_count(
            row["trajectory_count"], "persona-memory scenario"
        ),
        "trajectory_status_counts": dict(sorted(status_counts.items())),
        "metrics": projected_metrics,
    }


def _persona_pipeline(outcomes: Mapping[str, str]) -> dict[str, str]:
    expected = {"prepare", "live", "cleanup", "finalize"}
    if set(outcomes) != expected or any(
        value not in PERSONA_PIPELINE_OUTCOMES for value in outcomes.values()
    ):
        raise TeamReportError("persona pipeline outcomes are invalid")
    return {key: outcomes[key] for key in ("prepare", "live", "cleanup", "finalize")}


def _persona_unavailable_summary(
    result: Mapping[str, Any], repetitions: int, outcomes: Mapping[str, str]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "persona_memory_qualification_summary",
        "status": "INFRA_ERROR",
        "target": {
            "label": "candidate",
            "build_sha": result["target"]["expected_deployment_sha"],
            "runtime_mode": result["target"]["expected_runtime"],
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
        "failure_code": "PERSONA_SUMMARY_UNAVAILABLE",
        "pipeline_outcomes": _persona_pipeline(outcomes),
    }


def _persona_summary(
    source_root: Path,
    result: Mapping[str, Any],
    repetitions: int,
    outcomes: Mapping[str, str],
) -> dict[str, Any]:
    path = source_root / "persona-memory-summary.json"
    if not path.exists() and not path.is_symlink():
        return _persona_unavailable_summary(result, repetitions, outcomes)
    value = _read_json(path, "persona-memory summary")
    if not isinstance(value, Mapping):
        raise TeamReportError("persona-memory summary is invalid")
    status = value.get("status")
    formal = status != "NOT_FORMALLY_QUALIFIED"
    expected_top = (
        {
            "schema_version",
            "kind",
            "status",
            "started_at",
            "finished_at",
            "target",
            "coverage",
            "judge",
            "hashes",
            "scenarios",
        }
        if formal
        else {
            "schema_version",
            "kind",
            "status",
            "target",
            "coverage",
            "judge",
            "hashes",
            "scenarios",
            "skip_reason",
        }
    )
    row = _exact_keys(value, expected_top, "persona-memory summary")
    target = _exact_keys(
        row["target"],
        {"label", "build_sha", "runtime_mode", "provider", "model", "reasoning_effort"},
        "persona-memory target",
    )
    coverage = _exact_keys(
        row["coverage"],
        {"repetitions", "scenario_count", "trajectory_count", "metric_result_count"},
        "persona-memory coverage",
    )
    if (
        row["schema_version"] != 1
        or row["kind"] != "persona_memory_qualification_summary"
        or status not in PERSONA_STATUS_VALUES
        or (formal and result["target"]["expected_runtime"] != "hosted_resident")
        or (not formal and result["target"]["expected_runtime"] != "deployed_current")
        or target["build_sha"] != result["target"]["expected_deployment_sha"]
        or target["runtime_mode"] != result["target"]["expected_runtime"]
        or coverage["repetitions"] != repetitions
    ):
        raise TeamReportError("persona-memory summary is invalid")
    projected: dict[str, Any] = {
        "schema_version": 1,
        "kind": "persona_memory_qualification_summary",
        "status": status,
        "target": {
            "label": _safe_token(target["label"], "persona-memory target"),
            "build_sha": target["build_sha"],
            "runtime_mode": _safe_token(
                target["runtime_mode"], "persona-memory target"
            ),
            "provider": _safe_token(
                target["provider"], "persona-memory target", nullable=True
            ),
            "model": _safe_token(
                target["model"], "persona-memory target", nullable=True
            ),
            "reasoning_effort": _safe_token(
                target["reasoning_effort"], "persona-memory target"
            ),
        },
        "coverage": {
            key: _safe_count(coverage[key], "persona-memory coverage")
            for key in (
                "repetitions",
                "scenario_count",
                "trajectory_count",
                "metric_result_count",
            )
        },
        "judge": None,
        "hashes": {},
        "scenarios": [],
        "pipeline_outcomes": _persona_pipeline(outcomes),
    }
    if formal:
        _timestamp(row["started_at"])
        _timestamp(row["finished_at"])
        judge = row["judge"]
        if judge is not None:
            judge = _exact_keys(
                judge,
                {"judge_id", "configuration_sha256"},
                "persona-memory judge",
            )
            if (
                not isinstance(judge["configuration_sha256"], str)
                or SHA_RE.fullmatch(judge["configuration_sha256"]) is None
            ):
                raise TeamReportError("persona-memory judge is invalid")
            projected["judge"] = {
                "judge_id": _safe_token(judge["judge_id"], "persona-memory judge"),
                "configuration_sha256": judge["configuration_sha256"],
            }
        hashes = row["hashes"]
        if not isinstance(hashes, Mapping) or any(
            not isinstance(key, str)
            or SAFE_TOKEN_RE.fullmatch(key) is None
            or not isinstance(digest, str)
            or SHA_RE.fullmatch(digest) is None
            for key, digest in hashes.items()
        ):
            raise TeamReportError("persona-memory hashes are invalid")
        scenarios = row["scenarios"]
        if not isinstance(scenarios, list):
            raise TeamReportError("persona-memory scenarios are invalid")
        projected.update(
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            hashes=dict(sorted(hashes.items())),
            scenarios=[_persona_scenario(scenario) for scenario in scenarios],
        )
        if (
            [row["scenario_id"] for row in projected["scenarios"]]
            != list(PERSONA_SCENARIO_IDS)
            or any(
                row["scenario_version"] != PERSONA_SCENARIO_VERSION
                or row["trajectory_count"] != repetitions
                for row in projected["scenarios"]
            )
            or projected["coverage"]["scenario_count"] != len(PERSONA_SCENARIO_IDS)
            or projected["coverage"]["scenario_count"] != len(projected["scenarios"])
            or projected["coverage"]["trajectory_count"]
            != len(PERSONA_SCENARIO_IDS) * repetitions
            or projected["coverage"]["trajectory_count"]
            != sum(row["trajectory_count"] for row in projected["scenarios"])
            or projected["coverage"]["metric_result_count"]
            != sum(
                metric["result_count"]
                for scenario in projected["scenarios"]
                for metric in scenario["metrics"]
            )
        ):
            raise TeamReportError("persona-memory summary is invalid")
    else:
        if row["skip_reason"] != "STRICT_V2_USER_PATH_NOT_REQUIRED":
            raise TeamReportError("persona-memory summary is invalid")
        projected["skip_reason"] = row["skip_reason"]
    return projected


def _persona_markdown(summary: Mapping[str, Any]) -> str:
    target = summary.get("target")
    coverage = summary.get("coverage")
    target = target if isinstance(target, Mapping) else {}
    coverage = coverage if isinstance(coverage, Mapping) else {}
    pipeline = summary.get("pipeline_outcomes")
    pipeline = pipeline if isinstance(pipeline, Mapping) else {}
    failure_code = summary.get("failure_code")
    lines = [
        "# Persona and memory qualification matrix",
        "",
        f"- Status: `{_cell(summary.get('status'))}`",
        f"- Build: `{_cell(target.get('build_sha', 'UNAVAILABLE'))}`",
        f"- Runtime: `{_cell(target.get('runtime_mode', 'UNAVAILABLE'))}`",
        f"- Repetitions: `{_cell(coverage.get('repetitions', 'UNAVAILABLE'))}`",
        f"- Pipeline: prepare=`{_cell(pipeline.get('prepare', 'UNAVAILABLE'))}`, "
        f"live=`{_cell(pipeline.get('live', 'UNAVAILABLE'))}`, "
        f"cleanup=`{_cell(pipeline.get('cleanup', 'UNAVAILABLE'))}`, "
        f"finalize=`{_cell(pipeline.get('finalize', 'UNAVAILABLE'))}`",
        f"- Failure code: `{_cell(failure_code or 'NONE')}`",
        "- Canonical source: `persona-memory-summary.json`",
    ]
    scenarios = summary.get("scenarios")
    if isinstance(scenarios, list) and scenarios:
        lines.extend(
            [
                "",
                "| Scenario | Metric | Evaluator | Gate | Status | Passed | Failed | Blocked | Infra | Rate | Avg | Threshold | Failure codes |",
                "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for scenario in scenarios:
            for metric in scenario["metrics"]:
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
                            metric["fail_count"],
                            metric["blocked_count"],
                            metric["infra_error_count"],
                            metric["pass_rate"],
                            metric["average_score"]
                            if metric["average_score"] is not None
                            else "—",
                            metric["threshold"],
                            ", ".join(metric["failure_codes"]) or "—",
                        )
                    )
                    + " |"
                )
    return "\n".join(lines) + "\n"


def _persona_failure_rows(
    summary: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Project formal persona failures without inventing exact identifiers.

    The persona publisher deliberately removes trajectory/account/correlation IDs.
    These rows therefore carry only fixed codes and aggregate scenario/metric
    evidence.  ``exact_id_debug_available`` is always false so downstream bundle
    planning cannot imply that absent identifiers were retained.
    """

    reported_status = str(summary["status"])
    if reported_status == "NOT_FORMALLY_QUALIFIED":
        return reported_status, []
    scenarios = summary.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, list) else []
    failures: list[dict[str, Any]] = []
    for scenario in scenarios:
        if scenario["status"] == "PASS":
            continue
        metrics = [dict(metric) for metric in scenario["metrics"]]
        failures.append(
            {
                "source": "persona_memory",
                "profile_id": "persona-memory",
                "scenario_id": scenario["scenario_id"],
                "scenario_title": "Persona and memory regression",
                "status": scenario["status"],
                "failure": {
                    "category": scenario["status"],
                    "stage_code": "PERSONA_MEMORY",
                    "failure_code": "PERSONA_METRIC_NONPASS",
                },
                "trajectory_count": scenario["trajectory_count"],
                "trajectory_status_counts": dict(scenario["trajectory_status_counts"]),
                "metrics": metrics,
                "pipeline_outcomes": dict(summary["pipeline_outcomes"]),
                "exact_id_debug_available": False,
            }
        )

    if reported_status != "PASS" and not failures:
        failures.append(
            {
                "source": "persona_memory",
                "profile_id": "persona-memory",
                "scenario_id": "PERSONA_ARM",
                "scenario_title": "Persona and memory qualification arm",
                "status": reported_status,
                "failure": {
                    "category": reported_status,
                    "stage_code": "PERSONA_MEMORY",
                    "failure_code": summary.get("failure_code", "PERSONA_ARM_NONPASS"),
                },
                "trajectory_count": summary["coverage"]["trajectory_count"],
                "trajectory_status_counts": {},
                "metrics": [],
                "pipeline_outcomes": dict(summary["pipeline_outcomes"]),
                "exact_id_debug_available": False,
            }
        )

    effective_status = max(
        [reported_status, *(str(row["status"]) for row in failures)],
        key=PERSONA_STATUS_SEVERITY.__getitem__,
    )
    return effective_status, failures


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise TeamReportError("run timestamps are invalid") from None
    if parsed.tzinfo is None:
        raise TeamReportError("run timestamps are invalid")
    return parsed


def _duration_ms(result: Mapping[str, Any]) -> float:
    duration = (
        _timestamp(result["finished_at"]) - _timestamp(result["started_at"])
    ).total_seconds() * 1000
    if not math.isfinite(duration) or duration < 0:
        raise TeamReportError("run timestamps are invalid")
    return round(duration, 3)


def _handle(run_id: str, kind: str, value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(f"{run_id}\0{kind}\0{value}".encode("utf-8")).hexdigest()


def _handles(run_id: str, kind: str, values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [handle for value in values if (handle := _handle(run_id, kind, value))]


def _worst_status(scenarios: Sequence[Mapping[str, Any]]) -> str:
    return max(
        (str(row["status"]) for row in scenarios),
        key=lambda status: STATUS_SEVERITY[status],
    )


def _derived_summary(profile_statuses: Sequence[str]) -> dict[str, int]:
    return {
        field: sum(status == label for status in profile_statuses)
        for field, label in release_renderer.SUMMARY_FIELDS
    }


def _derived_overall(profile_statuses: Sequence[str]) -> str:
    return max(profile_statuses, key=lambda status: STATUS_SEVERITY[status])


def _failure_tuple(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, Mapping):
        return None
    fields = (value.get("category"), value.get("stage_code"), value.get("failure_code"))
    return (
        tuple(str(item) for item in fields)
        if all(isinstance(item, str) for item in fields)
        else None
    )


def _reproduction_state(scenario: Mapping[str, Any]) -> str:
    attempts = scenario.get("attempt_results")
    if not isinstance(attempts, list) or len(attempts) <= 1:
        return "OBSERVED_ONCE"
    if attempts[-1].get("status") == "PASS":
        return "RECOVERED_ON_RETRY"
    tuples = [
        _failure_tuple(row.get("failure"))
        for row in attempts
        if isinstance(row, Mapping)
    ]
    return (
        "REPRODUCED"
        if tuples and len(set(tuples)) == 1 and None not in tuples
        else "OBSERVED_MULTIPLE"
    )


def _attempt_projection(attempt: Mapping[str, Any]) -> dict[str, Any]:
    failure = attempt.get("failure")
    projected_failure = None
    if isinstance(failure, Mapping):
        projected_failure = {
            "category": failure["category"],
            "stage_code": failure["stage_code"],
            "failure_code": failure["failure_code"],
        }
    return {
        "attempt": attempt["attempt"],
        "status": attempt["status"],
        "failure": projected_failure,
    }


def _scenario_inconsistencies(
    profile_id: str,
    scenario: Mapping[str, Any],
) -> list[dict[str, str]]:
    scenario_id = str(scenario["scenario_id"])
    assertions = scenario["assertions"]
    evidence = set(scenario["evidence_codes"])
    rows: list[dict[str, str]] = []

    def add(code: str, *, assertion: str = "NONE", evidence_code: str = "NONE") -> None:
        rows.append(
            {
                "code": code,
                "profile_id": profile_id,
                "scenario_id": scenario_id,
                "assertion": assertion,
                "evidence_code": evidence_code,
            }
        )

    failed = [name for name, passed in assertions.items() if passed is False]
    if scenario["status"] == "PASS" and failed:
        add("PASS_WITH_FAILED_ASSERTION")
    if scenario["status"] != "PASS" and assertions and not failed:
        add("NONPASS_WITH_ALL_ASSERTIONS_TRUE")
    for assertion in failed:
        evidence_code = ASSERTION_EVIDENCE.get(assertion)
        if evidence_code in evidence:
            add(
                "POSITIVE_EVIDENCE_FOR_FAILED_ASSERTION",
                assertion=assertion,
                evidence_code=str(evidence_code),
            )
    failure = scenario.get("failure")
    if (
        isinstance(failure, Mapping)
        and failure.get("reproducible") is True
        and _reproduction_state(scenario) != "REPRODUCED"
    ):
        add("REPRODUCIBILITY_FLAG_UNATTESTED")
    return rows


def _validate_reportable_pass_contracts(
    profiles: Sequence[Mapping[str, Any]],
) -> None:
    """Reject a false-green PASS before any agent-authored bytes are published.

    Non-PASS rows are intentionally reportable so the team can debug product
    failures.  A PASS row, however, must satisfy the locked deterministic
    scenario contract: exact assertions/evidence, bounded attempt history, and
    exact turn/request/trace binding.  The authoritative release gate enforces
    the same contract for a green qualification; this local check prevents a
    contradictory PASS from becoming a green team panel or suppressing the
    protected failure bundle.
    """

    for profile in profiles:
        profile_id = str(profile["profile_id"])
        all_turns = profile["turns"]
        diagnostics = set(profile["diagnostic_codes"])
        for scenario in profile["scenarios"]:
            if scenario["status"] != "PASS":
                continue
            scenario_id = str(scenario["scenario_id"])
            scenario_turns = [
                turn for turn in all_turns if turn["scenario_id"] == scenario_id
            ]
            try:
                errors, _retried = release_gate._validate_scenario_contract(
                    profile_id,
                    scenario,
                    scenario_turns,
                    all_turns,
                    diagnostics,
                )
            except (KeyError, TypeError, ValueError):
                errors = ["invalid"]
            try:
                backing_evidence_valid = _pass_backing_evidence_valid(
                    profile, scenario_id, scenario_turns, all_turns
                )
            except (KeyError, TypeError, ValueError):
                backing_evidence_valid = False
            if not backing_evidence_valid:
                errors.append("invalid")
            if errors:
                raise TeamReportError(
                    "canonical PASS evidence does not satisfy the locked contract"
                )


def _validate_reportable_global_contract(
    result: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    *,
    expected_runtime: str,
    expected_sha: str,
) -> None:
    """Protect panel-wide identity, redaction, and false-green invariants."""

    redactions = [result["redaction"], *(profile["redaction"] for profile in profiles)]
    if any(
        any(
            redaction.get(field) is not True
            for field in release_gate._REDACTION_TRUE_FIELDS
        )
        or redaction.get("prompt_injection_detected") is not False
        for redaction in redactions
    ):
        raise TeamReportError("canonical report redaction evidence is unsafe")

    identity_groups: list[list[str]] = [
        [str(profile["user_id"]) for profile in profiles],
        [
            value
            for profile in profiles
            for scenario in profile["scenarios"]
            for value in scenario["request_ids"]
            if isinstance(value, str) and value
        ],
        [
            str(turn["turn_id"])
            for profile in profiles
            for turn in profile["turns"]
            if turn["turn_id"] is not None
        ],
        [
            str(turn["trace_id"])
            for profile in profiles
            for turn in profile["turns"]
            if turn["trace_id"] is not None
        ],
    ]
    persona_job_ids: list[str] = []
    persona_evidence_hashes: list[str] = []
    for profile in profiles:
        persona_scenario = next(
            row for row in profile["scenarios"] if row["scenario_id"] == "P0-06"
        )
        finalizer = persona_scenario.get("persona_finalizer")
        if isinstance(finalizer, Mapping):
            persona_job_ids.append(str(finalizer["job_id"]))
            persona_evidence_hashes.append(str(finalizer["evidence_sha256"]))
    identity_groups.extend((persona_job_ids, persona_evidence_hashes))
    if any(len(values) != len(set(values)) for values in identity_groups):
        raise TeamReportError("canonical report identities are not unique")

    for profile in profiles:
        if all(scenario["status"] == "PASS" for scenario in profile["scenarios"]):
            profile_id = str(profile["profile_id"])
            locked_metadata = release_gate._PROFILE_METADATA[profile_id]
            observed_runtime = profile["observed_runtime"]
            observed_version = profile["observed_runtime_version"]
            runtime_valid = (
                observed_runtime == expected_runtime and observed_version == 2
                if expected_runtime == release_gate.EXPECTED_RUNTIME
                else isinstance(observed_runtime, str)
                and bool(observed_runtime)
                and type(observed_version) is int
                and observed_version >= 1
            )
            if (
                profile["status"] != "PASS"
                or (
                    profile["route_family"],
                    profile["model_family"],
                    profile["provider"],
                )
                != locked_metadata
                or profile["expected_runtime"] != expected_runtime
                or not runtime_valid
                or profile["reasoning_effort"] != "medium"
            ):
                raise TeamReportError(
                    "canonical PASS profile does not satisfy locked metadata"
                )

    if all(
        scenario["status"] == "PASS"
        for profile in profiles
        for scenario in profile["scenarios"]
    ):
        try:
            semantic_errors = release_gate._validate_result_semantics(
                result, expected_runtime, expected_sha
            )
        except (KeyError, TypeError, ValueError):
            semantic_errors = ["invalid"]
        if semantic_errors:
            raise TeamReportError(
                "canonical PASS result does not satisfy release semantics"
            )


def _pass_turns_valid(turns: Sequence[Mapping[str, Any]]) -> bool:
    for turn in turns:
        ack_latency = turn["ack_latency_ms"]
        reply_latency = turn["reply_latency_ms"]
        if (
            turn["reply_count"] != 1
            or turn["content_assertion_passed"] is not True
            or turn["fallback_detected"] is not False
            or turn["duplicate_detected"] is not False
            or turn["out_of_order_detected"] is not False
            or not release_gate._is_nonnegative_number(ack_latency)
            or not release_gate._is_nonnegative_number(reply_latency)
            or ack_latency > reply_latency
            or reply_latency
            > release_gate._EXECUTION_CONTRACT["chat_reply_timeout_seconds"] * 1000
            or not release_gate._has_complete_stage_latency(turn["stage_latency_ms"])
        ):
            return False
    return True


def _pass_backing_evidence_valid(
    profile: Mapping[str, Any],
    scenario_id: str,
    scenario_turns: Sequence[Mapping[str, Any]],
    all_turns: Sequence[Mapping[str, Any]],
) -> bool:
    """Verify the deterministic evidence beneath an agent-authored PASS."""

    if not _pass_turns_valid(scenario_turns):
        return False
    if scenario_id == "P0-06":
        scenario = next(
            row for row in profile["scenarios"] if row["scenario_id"] == scenario_id
        )
        finalizer = scenario.get("persona_finalizer")
        return bool(
            isinstance(finalizer, Mapping)
            and finalizer.get("semantic_judgment_bound") is True
            and finalizer.get("finalizer_ok") is True
            and finalizer.get("private_evidence_deleted") is True
            and finalizer.get("archive_upload_count") == 4
            and finalizer.get("archive_receipts_verified") is True
            and finalizer.get("genesis_upload_metadata_verified") is True
            and finalizer.get("privacy_violation_count") == 0
            and scenario.get("request_ids") == [finalizer.get("request_id")]
        )
    if scenario_id == "P0-12":
        reasoning = profile["reasoning"]
        return bool(
            len(scenario_turns) == 1
            and reasoning["expected"] is True
            and reasoning["capability_enabled"] is True
            and reasoning["requested_effort"] == "medium"
            and reasoning["configured_effort"] == "medium"
            and reasoning["effective_effort"] == "unknown"
            and type(reasoning["reasoning_event_count"]) is int
            and reasoning["reasoning_event_count"] > 0
            and reasoning["metadata_present"] is True
            and reasoning["token_metadata_present"] is True
            and reasoning["user_visible_disclosure_present"] is True
            and bool(reasoning["kind"])
            and bool(reasoning["source"])
            and reasoning["model"] == profile["model"]
            and type(reasoning["reasoning_token_count"]) is int
            and reasoning["reasoning_token_count"] > 0
            and type(reasoning["disclosure_length"]) is int
            and reasoning["disclosure_length"] > 0
            and reasoning["raw_private_reasoning_stored"] is False
            and reasoning["request_id"] == scenario_turns[0]["request_id"]
            and reasoning["turn_id"] == scenario_turns[0]["turn_id"]
            and reasoning["trace_id"] == scenario_turns[0]["trace_id"]
        )
    if scenario_id == "P0-13":
        trace = profile["trace"]
        cleanup = profile["cleanup"]
        latency = profile["latency"]
        return bool(
            trace["enabled"] is True
            and trace["deploy_enabled"] is True
            and trace["correlated_event_count"] >= len(all_turns) * len(TRACE_STAGES)
            and set(trace["observed_event_types"]) == set(TRACE_STAGES)
            and trace["missing_required_event_types"] == []
            and trace["raw_trace_stored"] is False
            and cleanup["status"] == "PASS"
            and all(
                cleanup[field] is True
                for field in (
                    "attempted",
                    "provider_config_deleted",
                    "account_reset",
                    "old_credential_rejected",
                )
            )
            and latency["sample_count"] == len(all_turns)
            and release_gate._has_complete_stage_latency(latency["stage_p50_ms"])
            and latency["missing_stages"] == []
            and release_gate._latency_summary_matches_turns(latency, all_turns)
        )
    return True


def _matching_turns(
    profile: Mapping[str, Any], scenario: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    scenario_id = scenario["scenario_id"]
    raw_ids = {
        value
        for field in ("request_ids", "turn_ids", "trace_ids")
        for value in scenario.get(field, [])
        if isinstance(value, str)
    }
    rows = []
    for turn in profile["turns"]:
        identifiers = {
            turn.get("request_id"),
            turn.get("turn_id"),
            turn.get("trace_id"),
        }
        if turn.get("scenario_id") == scenario_id or raw_ids.intersection(identifiers):
            rows.append(turn)
    return rows


def _turn_projection(run_id: str, turn: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scenario_id": turn["scenario_id"],
        "turn_index": turn["turn_index"],
        "request_handle": _handle(run_id, "request", turn["request_id"]),
        "turn_handle": _handle(run_id, "turn", turn["turn_id"]),
        "trace_handle": _handle(run_id, "trace", turn["trace_id"]),
        "ack_latency_ms": turn["ack_latency_ms"],
        "reply_latency_ms": turn["reply_latency_ms"],
        "stage_latency_ms": dict(turn["stage_latency_ms"]),
        "reply_count": turn["reply_count"],
        "content_assertion_passed": turn["content_assertion_passed"],
        "fallback_detected": turn["fallback_detected"],
        "duplicate_detected": turn["duplicate_detected"],
        "out_of_order_detected": turn["out_of_order_detected"],
    }


def _reasoning_projection(
    profile: Mapping[str, Any], scenario_id: str
) -> dict[str, Any] | None:
    if scenario_id != "P0-12":
        return None
    reasoning = profile["reasoning"]
    return {
        field: reasoning[field]
        for field in (
            "expected",
            "capability_enabled",
            "requested_effort",
            "configured_effort",
            "effective_effort",
            "reasoning_event_count",
            "metadata_present",
            "token_metadata_present",
            "user_visible_disclosure_present",
            "kind",
            "source",
            "model",
            "reasoning_token_count",
            "disclosure_length",
            "raw_private_reasoning_stored",
        )
    }


def _persona_projection(scenario: Mapping[str, Any]) -> dict[str, Any] | None:
    if scenario["scenario_id"] != "P0-06" or not isinstance(
        scenario.get("persona_finalizer"), Mapping
    ):
        return None
    finalizer = scenario["persona_finalizer"]
    return {
        field: finalizer[field]
        for field in (
            "fixture_id",
            "evidence_sha256",
            "semantic_judgment_bound",
            "finalizer_ok",
            "private_evidence_deleted",
            "archive_upload_count",
            "archive_receipts_verified",
            "genesis_upload_metadata_verified",
            "privacy_violation_count",
        )
    }


def _trace_projection(profile: Mapping[str, Any]) -> dict[str, Any]:
    trace = profile["trace"]
    return {
        field: trace[field]
        for field in (
            "enabled",
            "deploy_enabled",
            "correlated_event_count",
            "observed_event_types",
            "missing_required_event_types",
            "raw_trace_stored",
        )
    }


def _triage(stage_code: str) -> dict[str, str]:
    layer, owner, next_probe, confidence = TRIAGE_BY_STAGE[stage_code]
    return {
        "suspected_layer": layer,
        "owner_code": owner,
        "next_probe": next_probe,
        "confidence": confidence,
    }


def _cleanup_projection(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "PASS",
        "generated_at": receipt["generated_at"],
        "attempted": receipt["attempted"],
        "cleaned": receipt["cleaned"],
        "failed_profile_ids": list(receipt["failed_profile_ids"]),
        "manifest_deleted": receipt["manifest_deleted"],
        "manifest_retained_for_scan": receipt["manifest_retained_for_scan"],
    }


def _build_indexes(
    result: Mapping[str, Any],
    coverage: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    cleanup_receipt: Mapping[str, Any],
    actor: str,
    persona_summary: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = str(result["run_id"])
    contracts = coverage["scenario_contracts"]
    inconsistencies: list[dict[str, str]] = []
    matrix: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    cleanup = _cleanup_projection(cleanup_receipt)
    derived_profile_statuses = [
        _worst_status(profile["scenarios"]) for profile in profiles
    ]
    derived_summary = _derived_summary(derived_profile_statuses)
    derived_overall = _derived_overall(derived_profile_statuses)

    if result["overall_status"] != derived_overall:
        inconsistencies.append(
            {
                "code": "RUN_STATUS_DIFFERS_FROM_PROFILES",
                "profile_id": "RUN",
                "scenario_id": "NONE",
                "assertion": "NONE",
                "evidence_code": "NONE",
            }
        )
    if result["summary"] != derived_summary:
        inconsistencies.append(
            {
                "code": "RUN_SUMMARY_DIFFERS_FROM_PROFILES",
                "profile_id": "RUN",
                "scenario_id": "NONE",
                "assertion": "NONE",
                "evidence_code": "NONE",
            }
        )
    if result["profiles_completed"] != len(profiles):
        inconsistencies.append(
            {
                "code": "RUN_COMPLETION_COUNT_INCONSISTENT",
                "profile_id": "RUN",
                "scenario_id": "NONE",
                "assertion": "NONE",
                "evidence_code": "NONE",
            }
        )

    for profile, worst in zip(profiles, derived_profile_statuses, strict=True):
        profile_id = str(profile["profile_id"])
        scenarios = profile["scenarios"]
        reported_status = str(profile["status"])
        if worst != reported_status:
            inconsistencies.append(
                {
                    "code": (
                        "PROFILE_STATUS_MASKS_WORSE_CHILD"
                        if STATUS_SEVERITY[worst] > STATUS_SEVERITY[reported_status]
                        else "PROFILE_STATUS_DIFFERS_FROM_CHILDREN"
                    ),
                    "profile_id": profile_id,
                    "scenario_id": "NONE",
                    "assertion": "NONE",
                    "evidence_code": "NONE",
                }
            )
        matrix.append(
            {
                "profile_id": profile_id,
                "route_family": profile["route_family"],
                "provider": profile["provider"],
                "model_family": profile["model_family"],
                "model": profile["model"],
                "expected_runtime": profile["expected_runtime"],
                "observed_runtime": profile["observed_runtime"],
                "observed_runtime_version": profile["observed_runtime_version"],
                "status": worst,
                "reported_status": reported_status,
                "worst_child_status": worst,
                "latency": dict(profile["latency"]),
                "scenarios": [
                    {"scenario_id": row["scenario_id"], "status": row["status"]}
                    for row in scenarios
                ],
            }
        )
        for scenario in scenarios:
            scenario_inconsistencies = _scenario_inconsistencies(profile_id, scenario)
            inconsistencies.extend(scenario_inconsistencies)
            if scenario["status"] == "PASS":
                continue
            scenario_id = str(scenario["scenario_id"])
            contract = contracts[scenario_id]
            observed_evidence = list(scenario["evidence_codes"])
            expected_evidence = list(contract["required_evidence_codes"])
            failure = scenario["failure"]
            matching_turns = _matching_turns(profile, scenario)
            failures.append(
                {
                    "source": "api_key_matrix",
                    "profile_id": profile_id,
                    "scenario_id": scenario_id,
                    "scenario_title": SCENARIO_TITLES[scenario_id],
                    "status": scenario["status"],
                    "started_at": scenario["started_at"],
                    "finished_at": scenario["finished_at"],
                    "failure": {
                        "category": failure["category"],
                        "stage_code": failure["stage_code"],
                        "failure_code": failure["failure_code"],
                    },
                    "attempts": [
                        _attempt_projection(row) for row in scenario["attempt_results"]
                    ],
                    "reproduction_state": _reproduction_state(scenario),
                    "failed_assertions": sorted(
                        name
                        for name, passed in scenario["assertions"].items()
                        if passed is False
                    ),
                    "observed_evidence_codes": observed_evidence,
                    "missing_evidence_codes": [
                        code
                        for code in expected_evidence
                        if code not in observed_evidence
                    ],
                    "diagnostic_codes": list(profile["diagnostic_codes"]),
                    "correlation_handles": {
                        "request_handles": _handles(
                            run_id, "request", scenario["request_ids"]
                        ),
                        "turn_handles": _handles(run_id, "turn", scenario["turn_ids"]),
                        "trace_handles": _handles(
                            run_id, "trace", scenario["trace_ids"]
                        ),
                    },
                    "turn_evidence": [
                        _turn_projection(run_id, turn) for turn in matching_turns
                    ],
                    "latency": dict(profile["latency"]),
                    "trace": _trace_projection(profile),
                    "reasoning": _reasoning_projection(profile, scenario_id),
                    "persona": _persona_projection(scenario),
                    "cleanup": cleanup if scenario_id == "P0-13" else None,
                    "triage": _triage(str(failure["stage_code"])),
                    "report_inconsistencies": [
                        row["code"] for row in scenario_inconsistencies
                    ],
                }
            )

    run_index = {
        "schema_version": 1,
        "kind": "io_e2e_team_run_index",
        "run_id": run_id,
        "suite_id": result["suite_id"],
        "overall_status": derived_overall,
        "reported_overall_status": result["overall_status"],
        "started_at": result["started_at"],
        "finished_at": result["finished_at"],
        "duration_ms": _duration_ms(result),
        "trigger_actor": actor,
        "target": dict(result["target"]),
        "profiles_expected": result["profiles_expected"],
        "profiles_completed": len(profiles),
        "reported_profiles_completed": result["profiles_completed"],
        "summary": derived_summary,
        "reported_summary": dict(result["summary"]),
        "cleanup": cleanup,
        "matrix": matrix,
        "report_inconsistencies": inconsistencies,
        "redaction": {
            "synthetic_users_only": True,
            "credentials_omitted": True,
            "user_identifiers_omitted": True,
            "raw_correlation_identifiers_omitted": True,
            "raw_chat_omitted": True,
            "raw_persona_omitted": True,
            "raw_trace_omitted": True,
            "raw_reasoning_omitted": True,
        },
    }
    api_key_failure_count = len(failures)
    persona_status = "NOT_FORMALLY_QUALIFIED"
    persona_failures: list[dict[str, Any]] = []
    if persona_summary is not None:
        persona_status, persona_failures = _persona_failure_rows(persona_summary)
        failures.extend(persona_failures)
    persona_overall = PERSONA_TO_OVERALL_STATUS[persona_status]
    run_index["api_key_overall_status"] = derived_overall
    run_index["overall_status"] = max(
        (derived_overall, persona_overall), key=STATUS_SEVERITY.__getitem__
    )
    failure_index = {
        "schema_version": 1,
        "kind": "io_e2e_team_failure_index",
        "run_id": run_id,
        "failure_count": len(failures),
        "api_key_failure_count": api_key_failure_count,
        "persona_memory_failure_count": len(persona_failures),
        "exact_id_failure_count": api_key_failure_count,
        "failures": failures,
        "redaction": dict(run_index["redaction"]),
    }
    run_index["persona_memory_effective_status"] = persona_status
    return run_index, failure_index


def _render_latency(run_id: str, profiles: Sequence[Mapping[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        (
            "record_type",
            "profile_id",
            "scenario_id",
            "turn_index",
            "request_handle",
            "turn_handle",
            "trace_handle",
            "metric",
            "stage",
            "value_ms",
        )
    )
    for profile in profiles:
        profile_id = profile["profile_id"]
        for turn in profile["turns"]:
            handles = (
                _handle(run_id, "request", turn["request_id"]),
                _handle(run_id, "turn", turn["turn_id"]),
                _handle(run_id, "trace", turn["trace_id"]),
            )
            for metric in ("ack_latency_ms", "reply_latency_ms"):
                value = turn[metric]
                if value is not None:
                    writer.writerow(
                        (
                            "turn",
                            profile_id,
                            turn["scenario_id"],
                            turn["turn_index"],
                            *handles,
                            metric,
                            "",
                            value,
                        )
                    )
            for stage in TRACE_STAGES:
                value = turn["stage_latency_ms"][stage]
                if value is not None:
                    writer.writerow(
                        (
                            "turn_stage",
                            profile_id,
                            turn["scenario_id"],
                            turn["turn_index"],
                            *handles,
                            "stage_latency_ms",
                            stage,
                            value,
                        )
                    )
        for metric in ("ack_p50_ms", "reply_p50_ms", "reply_p95_ms"):
            value = profile["latency"][metric]
            if value is not None:
                writer.writerow(
                    (
                        "profile_summary",
                        profile_id,
                        "",
                        "",
                        "",
                        "",
                        "",
                        metric,
                        "",
                        value,
                    )
                )
        for stage in TRACE_STAGES:
            value = profile["latency"]["stage_p50_ms"][stage]
            if value is not None:
                writer.writerow(
                    (
                        "stage_summary",
                        profile_id,
                        "",
                        "",
                        "",
                        "",
                        "",
                        "stage_p50_ms",
                        stage,
                        value,
                    )
                )
    return stream.getvalue()


def _cell(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("`", "'")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _milliseconds(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):.1f}"


def _matrix_markdown(run_index: Mapping[str, Any]) -> str:
    target = run_index["target"]
    lines = [
        "# IO API-key deployed-runtime qualification",
        "",
        f"- Run ID: `{_cell(run_index['run_id'])}`",
        f"- API-key overall status: `{_cell(run_index['api_key_overall_status'])}`",
        f"- Expected deployment: `{_cell(target['expected_deployment_sha'])}`",
        f"- Expected runtime: `{_cell(target['expected_runtime'])}`",
        "- Statuses are recomputed from the locked child-scenario matrix.",
        "",
    ]
    header = ["Profile", "Route", "Provider", "Model family", "Status"] + list(
        release_renderer.SCENARIO_IDS
    )
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for profile in run_index["matrix"]:
        statuses = {row["scenario_id"]: row["status"] for row in profile["scenarios"]}
        row = [
            profile["profile_id"],
            profile["route_family"],
            profile["provider"],
            profile["model_family"],
            profile["status"],
            *(statuses[scenario_id] for scenario_id in release_renderer.SCENARIO_IDS),
        ]
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    lines.extend(
        [
            "",
            "## Derived profile terminal-status counts",
            "",
            "| Status | Profiles |",
            "| --- | ---: |",
        ]
    )
    for field, status in release_renderer.SUMMARY_FIELDS:
        lines.append(f"| {status} | {run_index['summary'][field]} |")
    return "\n".join(lines) + "\n"


def _team_summary(
    run_index: Mapping[str, Any], failure_index: Mapping[str, Any]
) -> str:
    lines = [
        "# io agentic API-key qualification",
        "",
        f"- Overall: `{_cell(run_index['overall_status'])}`",
        f"- API-key matrix: `{_cell(run_index['api_key_overall_status'])}`",
        f"- Run: `{_cell(run_index['run_id'])}`",
        f"- Deployment: `{_cell(run_index['target']['expected_deployment_sha'])}`",
        f"- Runtime: `{_cell(run_index['target']['expected_runtime'])}`",
        f"- Duration: `{_milliseconds(run_index['duration_ms'])} ms`",
        f"- Triggered by: `@{_cell(run_index['trigger_actor'])}`",
        f"- Cleanup: `{_cell(run_index['cleanup']['status'])}` ({run_index['cleanup']['cleaned']}/{run_index['cleanup']['attempted']})",
        f"- Persona-memory arm: `{_cell(run_index['persona_memory']['status'])}`",
        f"- Failures/evidence gaps: `{failure_index['failure_count']}`",
        "",
        "## Coverage matrix",
        "",
    ]
    header = ["Profile", "Status", "Worst child", *release_renderer.SCENARIO_IDS]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for profile in run_index["matrix"]:
        scenario_statuses = {
            row["scenario_id"]: row["status"] for row in profile["scenarios"]
        }
        row = [
            profile["profile_id"],
            profile["status"],
            profile["worst_child_status"],
            *(scenario_statuses[item] for item in release_renderer.SCENARIO_IDS),
        ]
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")

    lines.extend(
        [
            "",
            "## Latency",
            "",
            "| Profile | Reply p50 ms | Reply p95 ms | Missing stages |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for profile in run_index["matrix"]:
        latency = profile["latency"]
        missing = ", ".join(latency["missing_stages"]) or "—"
        lines.append(
            f"| {_cell(profile['profile_id'])} | {_milliseconds(latency['reply_p50_ms'])} | "
            f"{_milliseconds(latency['reply_p95_ms'])} | {_cell(missing)} |"
        )

    api_key_failures = [
        row for row in failure_index["failures"] if row["source"] == "api_key_matrix"
    ]
    persona_failures = [
        row for row in failure_index["failures"] if row["source"] == "persona_memory"
    ]
    lines.extend(["", "## API-key failed and blocked scenarios", ""])
    if not api_key_failures:
        lines.append("All locked API-key scenarios passed.")
    for failure in api_key_failures:
        code = failure["failure"]["failure_code"]
        lines.extend(
            [
                f"<details><summary>{_cell(failure['status'])} — {_cell(failure['profile_id'])} / "
                f"{_cell(failure['scenario_id'])} — {_cell(code)}</summary>",
                "",
                f"- Scenario: {_cell(failure['scenario_title'])}",
                f"- Stage: `{_cell(failure['failure']['stage_code'])}`",
                f"- Reproduction: `{_cell(failure['reproduction_state'])}`",
                "- Attempts: "
                + _cell(
                    ", ".join(
                        f"{row['attempt']}={row['status']}"
                        for row in failure["attempts"]
                    )
                ),
                f"- Failed assertions: {_cell(', '.join(failure['failed_assertions']) or 'none')}",
                f"- Observed evidence: {_cell(', '.join(failure['observed_evidence_codes']) or 'none')}",
                f"- Missing evidence: {_cell(', '.join(failure['missing_evidence_codes']) or 'none')}",
                f"- Profile reply latency: p50=`{_milliseconds(failure['latency']['reply_p50_ms'])} ms`; "
                f"p95=`{_milliseconds(failure['latency']['reply_p95_ms'])} ms`",
                f"- Trace events: `{failure['trace']['correlated_event_count']}`; missing stages: "
                f"{_cell(', '.join(failure['trace']['missing_required_event_types']) or 'none')}",
                f"- Triage: `{_cell(failure['triage']['suspected_layer'])}` / "
                f"`{_cell(failure['triage']['owner_code'])}` / `{_cell(failure['triage']['next_probe'])}`",
            ]
        )
        if failure["persona"] is not None:
            persona = failure["persona"]
            lines.append(
                f"- Persona finalizer: `{'PASS' if persona['finalizer_ok'] else 'FAIL'}`; "
                f"privacy violations: `{persona['privacy_violation_count']}`"
            )
        if failure["reasoning"] is not None:
            reasoning = failure["reasoning"]
            lines.append(
                "- Reasoning evidence: "
                f"events=`{reasoning['reasoning_event_count']}`, "
                f"metadata=`{str(reasoning['metadata_present']).lower()}`, "
                f"tokens=`{str(reasoning['token_metadata_present']).lower()}`, "
                f"disclosure=`{str(reasoning['user_visible_disclosure_present']).lower()}`"
            )
        if failure["cleanup"] is not None:
            lines.append(
                f"- Deterministic cleanup: `{failure['cleanup']['status']}` "
                f"({failure['cleanup']['cleaned']}/{failure['cleanup']['attempted']})"
            )
        if failure["report_inconsistencies"]:
            lines.append(
                "- Report inconsistencies: "
                + _cell(", ".join(failure["report_inconsistencies"]))
            )
        turns = failure["turn_evidence"]
        if turns:
            lines.extend(
                [
                    f"- Matching turns retained: `{len(turns)}` (showing up to 5)",
                    "",
                    "| Turn | Ack ms | Reply ms | Replies | Content | Duplicate | Out of order | Fallback |",
                    "| ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
                ]
            )
            for turn in turns[:5]:
                lines.append(
                    "| "
                    + " | ".join(
                        _cell(value)
                        for value in (
                            turn["turn_index"],
                            _milliseconds(turn["ack_latency_ms"]),
                            _milliseconds(turn["reply_latency_ms"]),
                            turn["reply_count"],
                            turn["content_assertion_passed"],
                            turn["duplicate_detected"],
                            turn["out_of_order_detected"],
                            turn["fallback_detected"],
                        )
                    )
                    + " |"
                )
        lines.extend(["", "</details>", ""])

    lines.extend(["", "## Persona-memory aggregate failures", ""])
    if not persona_failures:
        if run_index["persona_memory"]["status"] == "NOT_FORMALLY_QUALIFIED":
            lines.append(
                "This runtime target does not run the formal persona-memory arm."
            )
        else:
            lines.append("All formal persona-memory scenarios passed.")
    for failure in persona_failures:
        lines.extend(
            [
                f"<details><summary>{_cell(failure['status'])} — persona-memory / "
                f"{_cell(failure['scenario_id'])} — "
                f"{_cell(failure['failure']['failure_code'])}</summary>",
                "",
                f"- Scenario: {_cell(failure['scenario_title'])}",
                f"- Trajectories: `{failure['trajectory_count']}`; statuses: "
                + _cell(
                    ", ".join(
                        f"{status}={count}"
                        for status, count in failure["trajectory_status_counts"].items()
                    )
                    or "none"
                ),
                "- Pipeline: "
                + _cell(
                    ", ".join(
                        f"{stage}={outcome}"
                        for stage, outcome in failure["pipeline_outcomes"].items()
                    )
                ),
                "- Exact-ID debug context: `unavailable by persona aggregate contract`; "
                "no account, request, turn, trace, or job IDs were published.",
            ]
        )
        metrics = failure["metrics"]
        if metrics:
            lines.extend(
                [
                    "",
                    "| Metric | Evaluator | Gate | Status | Passed | Failed | Blocked | Infra | Rate | Avg | Threshold | Failure codes |",
                    "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
                ]
            )
            for metric in metrics:
                lines.append(
                    "| "
                    + " | ".join(
                        _cell(value)
                        for value in (
                            metric["metric_id"],
                            metric["evaluator_type"],
                            "hard" if metric["hard_gate"] else "soft",
                            metric["status"],
                            f"{metric['pass_count']}/{metric['result_count']}",
                            metric["fail_count"],
                            metric["blocked_count"],
                            metric["infra_error_count"],
                            metric["pass_rate"],
                            metric["average_score"]
                            if metric["average_score"] is not None
                            else "—",
                            metric["threshold"],
                            ", ".join(metric["failure_codes"]) or "—",
                        )
                    )
                    + " |"
                )
        lines.extend(["", "</details>", ""])

    if run_index["report_inconsistencies"]:
        lines.extend(
            [
                "## Report consistency warnings",
                "",
                "These indicate evidence-authoring defects and must be resolved before treating the row as a product diagnosis.",
                "",
            ]
        )
        for item in run_index["report_inconsistencies"]:
            lines.append(
                f"- `{_cell(item['code'])}` — `{_cell(item['profile_id'])}` / `{_cell(item['scenario_id'])}`"
            )
    rendered = "\n".join(lines).rstrip() + "\n"
    if len(rendered.encode("utf-8")) > MAX_SUMMARY_BYTES:
        raise TeamReportError("team summary exceeds its size limit")
    return rendered


def _json_text(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, indent=2)
        + "\n"
    )


def _write_new(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise TeamReportError("team report output could not be created") from None


def _create_output_root(path: Path) -> Path:
    candidate = path.absolute()
    if candidate.exists() or candidate.is_symlink():
        raise TeamReportError("team report output path is occupied")
    try:
        parent = candidate.parent.resolve(strict=True)
        if not parent.is_dir():
            raise OSError
        candidate.mkdir(mode=0o700)
        os.chmod(candidate, 0o700)
    except OSError:
        raise TeamReportError("team report output root could not be created") from None
    return candidate


def build_team_report(
    *,
    coverage_path: Path,
    schema_path: Path,
    result_path: Path,
    provisioning_manifest_path: Path,
    orchestration_receipt_path: Path,
    deployment_receipt_path: Path,
    post_deployment_receipt_path: Path,
    cleanup_receipt_path: Path,
    source_artifacts: Path,
    output_path: Path,
    actor: str,
    persona_repetitions: int,
    persona_pipeline_outcomes: Mapping[str, str],
    expected_runtime: str,
    expected_sha: str,
) -> None:
    """Validate trusted inputs and write the exact team-safe artifact set."""

    if ACTOR_RE.fullmatch(actor) is None:
        raise TeamReportError("trigger actor is invalid")
    if persona_repetitions not in {1, 3}:
        raise TeamReportError("persona repetitions are invalid")
    if expected_runtime not in {"deployed_current", "hosted_resident"}:
        raise TeamReportError("expected runtime is invalid")
    if DEPLOYMENT_SHA_RE.fullmatch(expected_sha) is None:
        raise TeamReportError("expected deployment is invalid")
    persona_pipeline = _persona_pipeline(persona_pipeline_outcomes)
    coverage = _read_json(coverage_path, "coverage lock")
    schema = _read_json(schema_path, "result schema")
    result = _read_json(result_path, "canonical run result")
    if not isinstance(result, dict) or not isinstance(coverage, dict):
        raise TeamReportError("team report input shape is invalid")
    target = result.get("target")
    if (
        not isinstance(target, Mapping)
        or target.get("expected_runtime") != expected_runtime
        or target.get("expected_deployment_sha") != expected_sha
        or target.get("observed_backend_sha") != expected_sha
    ):
        raise TeamReportError("canonical result target binding is invalid")
    coverage_errors = release_gate._validate_coverage(
        coverage, str(result.get("target", {}).get("expected_runtime") or "")
    )
    if coverage_errors:
        raise TeamReportError("coverage lock does not match the locked qualification")
    try:
        release_renderer._validate_schema(schema, result)
        profiles = release_renderer._ordered_profiles(result)
    except release_renderer.RenderInputError:
        raise TeamReportError("canonical run result is invalid") from None

    expected_profiles = [row["id"] for row in coverage["profiles"]]
    if [row["profile_id"] for row in result["profiles"]] != expected_profiles:
        raise TeamReportError("canonical profile matrix is not in locked order")
    expected_scenarios = list(coverage["required_scenarios"])
    for profile in result["profiles"]:
        if [row["scenario_id"] for row in profile["scenarios"]] != expected_scenarios:
            raise TeamReportError("canonical scenario matrix is not in locked order")
        for scenario in profile["scenarios"]:
            contract = coverage["scenario_contracts"][scenario["scenario_id"]]
            if set(scenario["assertions"]) != set(contract["required_assertions"]):
                raise TeamReportError(
                    "canonical scenario assertions do not match coverage"
                )
    _validate_reportable_pass_contracts(profiles)
    _validate_reportable_global_contract(
        result,
        profiles,
        expected_runtime=expected_runtime,
        expected_sha=expected_sha,
    )

    provisioning_manifest = _read_json(
        provisioning_manifest_path, "trusted provisioning manifest"
    )
    orchestration_receipt = _read_json(
        orchestration_receipt_path, "trusted orchestration receipt"
    )
    deployment_receipt = _read_json(
        deployment_receipt_path, "trusted pre-run deployment receipt"
    )
    post_deployment_receipt = _read_json(
        post_deployment_receipt_path, "trusted post-run deployment receipt"
    )
    binding_errors = [
        *release_gate._validate_provisioning_manifest(
            provisioning_manifest, result, expected_runtime
        ),
        *release_gate._validate_orchestration_receipt(orchestration_receipt, result),
        *release_gate._validate_deployment_receipt(
            deployment_receipt,
            result,
            expected_sha,
            expected_runtime,
            "pre",
        ),
        *release_gate._validate_deployment_receipt(
            post_deployment_receipt,
            result,
            expected_sha,
            expected_runtime,
            "post",
        ),
        *release_gate._validate_deployment_receipt_pair(
            deployment_receipt, post_deployment_receipt, result
        ),
    ]
    if binding_errors:
        raise TeamReportError("trusted run evidence does not bind the team report")

    try:
        cleanup_validator.validate_cleanup_receipt(cleanup_receipt_path, result_path)
    except cleanup_validator.CleanupReceiptError:
        raise TeamReportError("deterministic cleanup receipt is invalid") from None
    cleanup_receipt = _read_json(
        cleanup_receipt_path, "cleanup receipt", max_bytes=2 * 1024 * 1024
    )
    source_root = _source_root(source_artifacts)
    try:
        memory_receipt = release_gate._read_memory_contract_receipt(source_root)
        memory_policy = coverage["deterministic_contracts"]["memory"][
            "migration_policy"
        ]
        memory_errors = release_gate._validate_memory_contract_receipt(
            memory_receipt, migration_policy=memory_policy
        )
    except release_gate.GateInputError:
        raise TeamReportError("memory contract receipt is invalid") from None
    if memory_errors:
        raise TeamReportError("memory contract receipt is invalid")
    persona_summary = _persona_summary(
        source_root, result, persona_repetitions, persona_pipeline
    )

    run_index, failure_index = _build_indexes(
        result, coverage, profiles, cleanup_receipt, actor, persona_summary
    )
    run_index["persona_memory"] = {
        "status": run_index.pop("persona_memory_effective_status"),
        "reported_status": persona_summary["status"],
        "failure_code": persona_summary.get("failure_code", "NONE"),
        "coverage": dict(persona_summary["coverage"]),
        "pipeline_outcomes": dict(persona_summary["pipeline_outcomes"]),
    }
    outputs = {
        "run-index.json": _json_text(run_index),
        "failure-index.json": _json_text(failure_index),
        "team-summary.md": _team_summary(run_index, failure_index),
        "matrix.md": _matrix_markdown(run_index),
        "latency.csv": _render_latency(str(result["run_id"]), profiles),
        "junit.xml": release_renderer._render_junit(profiles),
        "cleanup-receipt.json": _json_text(cleanup_receipt),
        "memory-contract.json": _json_text(memory_receipt),
        "persona-memory-summary.json": _json_text(persona_summary),
        "persona-memory-matrix.md": _persona_markdown(persona_summary),
    }
    if set(outputs) != OUTPUT_FILES:
        raise TeamReportError("team report output contract is incomplete")

    root: Path | None = None
    try:
        root = _create_output_root(output_path)
        for name in sorted(OUTPUT_FILES):
            _write_new(root / name, outputs[name])
        actual = {path.name for path in root.iterdir() if path.is_file()}
        if actual != OUTPUT_FILES or any(path.is_symlink() for path in root.iterdir()):
            raise TeamReportError("team report output contract is incomplete")
    except Exception:
        if root is not None and root.exists() and not root.is_symlink():
            shutil.rmtree(root)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--provisioning-manifest", type=Path, required=True)
    parser.add_argument("--orchestration-receipt", type=Path, required=True)
    parser.add_argument("--deployment-receipt", type=Path, required=True)
    parser.add_argument("--post-deployment-receipt", type=Path, required=True)
    parser.add_argument("--cleanup-receipt", type=Path, required=True)
    parser.add_argument("--source-artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument(
        "--expected-runtime",
        choices=("deployed_current", "hosted_resident"),
        required=True,
    )
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument(
        "--persona-repetitions", type=int, choices=(1, 3), required=True
    )
    for stage in ("prepare", "live", "cleanup", "finalize"):
        parser.add_argument(
            f"--persona-{stage}-outcome",
            choices=sorted(PERSONA_PIPELINE_OUTCOMES),
            required=True,
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        build_team_report(
            coverage_path=args.coverage,
            schema_path=args.schema,
            result_path=args.result,
            provisioning_manifest_path=args.provisioning_manifest,
            orchestration_receipt_path=args.orchestration_receipt,
            deployment_receipt_path=args.deployment_receipt,
            post_deployment_receipt_path=args.post_deployment_receipt,
            cleanup_receipt_path=args.cleanup_receipt,
            source_artifacts=args.source_artifacts,
            output_path=args.output,
            actor=args.actor,
            persona_repetitions=args.persona_repetitions,
            persona_pipeline_outcomes={
                stage: getattr(args, f"persona_{stage}_outcome")
                for stage in ("prepare", "live", "cleanup", "finalize")
            },
            expected_runtime=args.expected_runtime,
            expected_sha=args.expected_sha,
        )
    except TeamReportError as exc:
        print(f"team report: FAIL: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("team report: FAIL: internal error", file=sys.stderr)
        return 2
    print("team report: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
