from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from qa import run_codex_profile_workers as launcher
from qa import request_live_scenario_probe as live_request
from qa import validate_cot_receipt as cot_receipts
from qa import validate_diagnostic_attempts as diagnostic_attempts
from qa import verify_codex_orchestration as verifier
from qa import validate_live_scenario_receipts as live_receipts
from qa import write_codex_config as writer
from qa.orchestration_contract import PROFILE_AGENT_TYPES


def _private(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _qualification_runtime(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "qualification-runtime"
    binary_dir = runtime / "bin"
    binary_dir.mkdir(parents=True, mode=0o755)
    runtime.chmod(0o755)
    binary_dir.chmod(0o755)
    executable = binary_dir / "python3"
    executable.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n"
    )
    executable.chmod(0o700)
    return runtime, executable


def _setup(
    tmp_path: Path, qualification_mode: str = "release"
) -> dict[str, Any]:
    source = tmp_path / "checkout"
    artifacts = source / "artifacts" / "run"
    artifacts.mkdir(parents=True)
    private = _private(tmp_path / "private")
    runtime_root, worker_python = _qualification_runtime(tmp_path)
    paths = {
        "source": source,
        "artifacts": artifacts,
        "private": private,
        "codex_home": _private(private / "codex-home"),
        "manifests": _private(private / "manifests"),
        "worker_root": _private(private / "workers"),
        "raw": _private(private / "raw"),
        "aggregation": _private(private / "aggregation"),
        "supervisor_home": _private(private / "supervisor-home"),
        "supervisor_tmp": _private(private / "supervisor-tmp"),
        "supervisor_work": _private(private / "supervisor-work"),
        "receipt": private / "receipt.json",
        "full_manifest": private / "full-manifest.json",
        "schema": Path(__file__).resolve().parents[1]
        / "schemas"
        / "codex-run-result.schema.json",
        "codex_bin": Path("/usr/bin/true"),
        "runtime_root": runtime_root,
        "worker_python": worker_python,
    }
    for _, agent_type in PROFILE_AGENT_TYPES:
        agent_root = _private(paths["worker_root"] / agent_type)
        for leaf in ("home", "tmp", "work"):
            _private(agent_root / leaf)
    config_values = {
        "output": paths["codex_home"] / "config.toml",
        "source_root": source,
        "artifact_root": artifacts,
        "full_manifest": paths["full_manifest"],
        "profile_manifest_dir": paths["manifests"],
        "supervisor_home": paths["supervisor_home"],
        "supervisor_tmp": paths["supervisor_tmp"],
        "supervisor_work": paths["supervisor_work"],
        "worker_root": paths["worker_root"],
        "worker_output_root": paths["raw"],
        "aggregation_input_root": paths["aggregation"],
        "orchestration_receipt": paths["receipt"],
        "codex_model": "gpt-5.4",
        "allowed_host": "test-api.feedling.app",
        "worker_python": paths["worker_python"],
        "qualification_mode": qualification_mode,
        "runtime_read_roots": (paths["runtime_root"],),
    }
    writer.write_bundle(
        config_values["output"], writer.build_config_bundle(**config_values)
    )
    for profile_id, _ in PROFILE_AGENT_TYPES:
        manifest = paths["manifests"] / f"{profile_id}.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profiles": [{"profile_id": profile_id}],
                }
            )
            + "\n"
        )
        manifest.chmod(0o600)
    return paths


def _instance(node: dict[str, Any], definitions: dict[str, Any]) -> Any:
    if "$ref" in node:
        return _instance(
            definitions[node["$ref"].removeprefix("#/$defs/")], definitions
        )
    if "enum" in node:
        return node["enum"][0]
    if "anyOf" in node:
        return _instance(node["anyOf"][0], definitions)
    node_type = node.get("type")
    if isinstance(node_type, list):
        node_type = next(value for value in node_type if value != "null")
    if node_type == "object":
        return {
            name: _instance(child, definitions)
            for name, child in node.get("properties", {}).items()
        }
    if node_type == "array":
        return []
    if node_type == "string":
        return "safe"
    if node_type in ("integer", "number"):
        return 0
    if node_type == "boolean":
        return True
    if node_type == "null":
        return None
    raise AssertionError(node)


def _apply_diagnostic_parent_cleanup_deferral(result: dict[str, Any]) -> None:
    """Make a populated fake worker honest before deterministic cleanup."""

    failure = dict(diagnostic_attempts.PARENT_CLEANUP_DEFERRED_FAILURE)
    scenario = result["scenarios"][-1]
    scenario.update(
        {
            "status": diagnostic_attempts.PARENT_CLEANUP_DEFERRED_STATUS,
            "attempts": 1,
            "attempt_results": [
                {
                    "attempt": 1,
                    "status": diagnostic_attempts.PARENT_CLEANUP_DEFERRED_STATUS,
                    "failure": dict(failure),
                }
            ],
            "assertions": {
                "trace_stages_complete": True,
                "trace_correlation_confirmed": True,
                "latency_attributed": True,
                "cleanup_confirmed": False,
            },
            "evidence_codes": [
                "TRACE_CORRELATION_CONFIRMED",
                "LATENCY_ATTRIBUTED",
            ],
            "failure": failure,
        }
    )
    result["status"] = diagnostic_attempts.PARENT_CLEANUP_DEFERRED_STATUS
    result["cleanup"] = {
        "attempted": False,
        "provider_config_deleted": False,
        "account_reset": False,
        "old_credential_rejected": False,
        "status": diagnostic_attempts.PARENT_CLEANUP_DEFERRED_STATUS,
    }
    result["diagnostic_codes"] = ["CLEANUP_FALLBACK_USED"]


def _passing_cot_receipt(profile_id: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "profile_id": profile_id,
        "request_id": "request-1",
        "turn_id": "request-1",
        "trace_id": "request-1",
        "reply_message_id": "reply-1",
        "status": "PASS",
        "failure_code": "NONE",
        "requested_effort": "medium",
        "configured_effort": "medium",
        "configured_effort_attested": True,
        "configured_route_matches_manifest": True,
        "effective_effort": "unknown",
        "effective_effort_attested": False,
        "release_qualified": False,
        "delivery_qualified": True,
        "final_answer_correct": True,
        "ack_latency_ms": 25.0,
        "reply_latency_ms": 800.0,
        "model_duration_ms": 700.0,
        "provider_api_duration_ms": 650.0,
        "trace_dropped": False,
        "model_call_count": 1,
        "agent_reply_count": 1,
        "chat_response_count": 1,
        "chat_response_match_count": 1,
        "model_thinking_present": True,
        "model_thinking_len": 42,
        "reasoning_event_count": 1,
        "model_thinking_source": "pi_thinking",
        "agent_reply_thinking_kind": "provider_reasoning_summary",
        "delivered_thinking_present": True,
        "delivered_thinking_len": 24,
        "delivered_thinking_kind": "provider_reasoning_summary",
        "delivered_thinking_source": "pi_thinking",
        "delivered_thinking_model": "model-safe",
        "delivered_thinking_native": True,
        "metadata_present": True,
        "user_visible_disclosure_present": True,
        "token_metadata_status": "PRESENT",
        "reasoning_token_count": 42,
        "raw_reply_stored": False,
        "raw_thinking_stored": False,
        "raw_trace_stored": False,
    }


def _request_failed_cot_receipt(
    profile_id: str, *, after_ack: bool = False
) -> dict[str, Any]:
    receipt = _passing_cot_receipt(profile_id)
    receipt.update(
        {
            "request_id": "request-1" if after_ack else "",
            "turn_id": "request-1" if after_ack else "",
            "trace_id": "request-1" if after_ack else "",
            "reply_message_id": "",
            "status": "UNVERIFIED",
            "failure_code": "CHAT_REQUEST_FAILED",
            "delivery_qualified": False,
            "final_answer_correct": False,
            "ack_latency_ms": 25.0 if after_ack else None,
            "reply_latency_ms": None,
            "model_duration_ms": None,
            "provider_api_duration_ms": None,
            "trace_dropped": False,
            "model_call_count": 0,
            "agent_reply_count": 0,
            "chat_response_count": 0,
            "chat_response_match_count": 0,
            "model_thinking_present": False,
            "model_thinking_len": 0,
            "reasoning_event_count": 0,
            "model_thinking_source": "",
            "agent_reply_thinking_kind": "",
            "delivered_thinking_present": False,
            "delivered_thinking_len": 0,
            "delivered_thinking_kind": "",
            "delivered_thinking_source": "",
            "delivered_thinking_model": "",
            "delivered_thinking_native": None,
            "metadata_present": False,
            "user_visible_disclosure_present": False,
            "token_metadata_status": "UNVERIFIED",
            "reasoning_token_count": None,
        }
    )
    return receipt


def _cot_scenario_projection(receipt: dict[str, Any]) -> tuple[str, str | None]:
    status = receipt["status"]
    code = receipt["failure_code"]
    if (
        receipt["requested_effort"] != "medium"
        or receipt["configured_effort_attested"] is not True
    ):
        return "BLOCKED_EVIDENCE", "PRECONDITION_MISSING"
    if receipt["configured_route_matches_manifest"] is not True:
        return "PRODUCT_FAIL", "MODEL_ROUTE_MISMATCH"
    if receipt["configured_effort"] != "medium":
        return "PRODUCT_FAIL", "REASONING_EFFORT_CLAMPED"
    if status == "PASS" and receipt["token_metadata_status"] != "PRESENT":
        return "BLOCKED_EVIDENCE", "REASONING_TOKENS_MISSING"
    if status == "PASS":
        return "PASS", None
    if status == "FAIL":
        return "PRODUCT_FAIL", {
            "FINAL_ANSWER_WRONG": "CONTENT_ASSERTION_FAILED",
            "DOWNSTREAM_PARSE_DROPPED_REASONING": "REASONING_METADATA_MISSING",
            "THINKING_ENVELOPE_NOT_DELIVERED": "DISCLOSURE_MISSING",
            "THINKING_ENVELOPE_UNREADABLE": "DISCLOSURE_MISSING",
            "THINKING_METADATA_INVALID": "REASONING_METADATA_MISSING",
        }[code]
    return "BLOCKED_EVIDENCE", {
        "CHAT_TIMEOUT": "CHAT_TIMEOUT",
        "CHAT_REQUEST_FAILED": "TRACE_UNAVAILABLE",
        "MODEL_REASONING_NOT_OBSERVED": "TRACE_INCOMPLETE",
        "TRACE_AMBIGUOUS": "TRACE_INCOMPLETE",
        "TRACE_UNAVAILABLE": "TRACE_UNAVAILABLE",
    }[code]


def _bound_cot_profile(receipt: dict[str, Any]) -> dict[str, Any]:
    status, failure_code = _cot_scenario_projection(receipt)
    failure = (
        None
        if failure_code is None
        else {
            "category": status,
            "stage_code": "REASONING",
            "failure_code": failure_code,
            "reproducible": True,
        }
    )
    token_present = (
        receipt["token_metadata_status"] == "PRESENT"
        and isinstance(receipt["reasoning_token_count"], int)
        and receipt["reasoning_token_count"] > 0
    )
    capability_enabled = receipt["reasoning_event_count"] == 1
    return {
        "status": status,
        "reasoning": {
            "expected": True,
            "capability_enabled": capability_enabled,
            "requested_effort": receipt["requested_effort"],
            "configured_effort": receipt["configured_effort"],
            "effective_effort": receipt["effective_effort"],
            "request_id": receipt["request_id"],
            "turn_id": receipt["turn_id"],
            "trace_id": receipt["trace_id"],
            "reasoning_event_count": receipt["reasoning_event_count"],
            "metadata_present": receipt["metadata_present"],
            "token_metadata_present": token_present,
            "user_visible_disclosure_present": receipt[
                "user_visible_disclosure_present"
            ],
            "reasoning_token_count": receipt["reasoning_token_count"],
            "disclosure_length": receipt["delivered_thinking_len"],
            "kind": receipt["delivered_thinking_kind"],
            "source": receipt["delivered_thinking_source"],
            "model": receipt["delivered_thinking_model"],
            "raw_private_reasoning_stored": False,
        },
        "scenarios": [
            {
                "scenario_id": "P0-12",
                "status": status,
                "attempts": 1,
                "attempt_results": [
                    {"attempt": 1, "status": status, "failure": failure}
                ],
                "failure": failure,
                "request_ids": [receipt["request_id"]],
                "turn_ids": [receipt["turn_id"]],
                "trace_ids": [receipt["trace_id"]],
                "assertions": {
                    "objective_answer_correct": receipt["final_answer_correct"],
                    "reasoning_capability_enabled": capability_enabled,
                    "reasoning_requested_effort_medium": (
                        receipt["requested_effort"] == "medium"
                    ),
                    "reasoning_configured_effort_medium": (
                        receipt["configured_effort"] == "medium"
                        and receipt["configured_effort_attested"] is True
                    ),
                    "reasoning_effective_effort_not_attested": (
                        receipt["effective_effort"] == "unknown"
                        and receipt["effective_effort_attested"] is False
                    ),
                    "reasoning_event_observed": capability_enabled,
                    "reasoning_metadata_present": receipt["metadata_present"],
                    "reasoning_tokens_present": token_present,
                    "user_disclosure_present": receipt[
                        "user_visible_disclosure_present"
                    ],
                    "raw_private_reasoning_omitted": True,
                },
            }
        ],
    }


@pytest.mark.parametrize(
    ("configuration", "expected_status", "expected_failure_code"),
    (
        ({}, "PASS", None),
        (
            {"configured_effort": "off"},
            "PRODUCT_FAIL",
            "REASONING_EFFORT_CLAMPED",
        ),
        (
            {"configured_route_matches_manifest": False},
            "PRODUCT_FAIL",
            "MODEL_ROUTE_MISMATCH",
        ),
        (
            {
                "configured_effort": "unknown",
                "configured_effort_attested": False,
                "configured_route_matches_manifest": False,
            },
            "BLOCKED_EVIDENCE",
            "PRECONDITION_MISSING",
        ),
    ),
)
def test_cot_binding_projects_configuration_context_without_losing_delivery(
    configuration, expected_status, expected_failure_code
):
    receipt = _passing_cot_receipt("official-deepseek")
    receipt.update(configuration)
    profile = _bound_cot_profile(receipt)

    assert receipt["status"] == "PASS"
    assert profile["scenarios"][0]["status"] == expected_status
    failure = profile["scenarios"][0]["failure"]
    assert (failure or {}).get("failure_code") == expected_failure_code
    launcher._validate_cot_result_binding(profile, receipt)


def _request_passing_cot_probe(
    spec: launcher.WorkerSpec, receipt: dict[str, Any] | None = None
) -> None:
    """Bind the agent projection, then signal the trusted parent probe."""

    receipt = receipt or _passing_cot_receipt(spec.profile_id)

    try:
        result = json.loads(spec.result_path.read_text())
        schema = json.loads(spec.schema_path.read_text())
    except (OSError, json.JSONDecodeError):
        result = None
        schema = None
    if isinstance(result, dict):
        reasoning = result.get("reasoning")
        if isinstance(reasoning, dict):
            reasoning.update(
                {
                    "expected": True,
                    "capability_enabled": receipt["reasoning_event_count"] == 1,
                    "requested_effort": receipt["requested_effort"],
                    "configured_effort": receipt["configured_effort"],
                    "effective_effort": receipt["effective_effort"],
                    "request_id": receipt["request_id"],
                    "turn_id": receipt["turn_id"],
                    "trace_id": receipt["trace_id"],
                    "reasoning_event_count": receipt["reasoning_event_count"],
                    "metadata_present": receipt["metadata_present"],
                    "token_metadata_present": (
                        receipt["token_metadata_status"] == "PRESENT"
                    ),
                    "user_visible_disclosure_present": receipt[
                        "user_visible_disclosure_present"
                    ],
                    "reasoning_token_count": receipt["reasoning_token_count"],
                    "disclosure_length": receipt["delivered_thinking_len"],
                    "kind": receipt["delivered_thinking_kind"] or None,
                    "source": receipt["delivered_thinking_source"] or None,
                    "model": receipt["delivered_thinking_model"] or None,
                    "raw_private_reasoning_stored": False,
                }
            )
        scenarios = result.get("scenarios")
        if isinstance(scenarios, list) and isinstance(schema, dict):
            definitions = schema.get("$defs")
            if isinstance(definitions, dict):
                scenario_schema = definitions.get("scenarioResult")
                if isinstance(scenario_schema, dict):
                    existing = {
                        row.get("scenario_id"): row
                        for row in scenarios
                        if isinstance(row, dict)
                    }
                    ordered = []
                    for index in range(1, 14):
                        scenario_id = f"P0-{index:02d}"
                        scenario = existing.get(scenario_id)
                        if not isinstance(scenario, dict):
                            scenario = _instance(scenario_schema, definitions)
                        scenario["scenario_id"] = scenario_id
                        scenario["attempts"] = 1
                        scenario["attempt_results"] = [
                            {"attempt": 1, "status": "PASS", "failure": None}
                        ]
                        ordered.append(scenario)
                    scenarios[:] = ordered
                    assertion_variants = scenario_schema["properties"][
                        "assertions"
                    ]["anyOf"]
                    reasoning_assertions = next(
                        variant
                        for variant in assertion_variants
                        if "objective_answer_correct" in variant.get("required", [])
                    )
                    scenarios[11]["assertions"] = _instance(
                        reasoning_assertions, definitions
                    )
            for scenario in scenarios:
                if isinstance(scenario, dict) and scenario.get("scenario_id") == "P0-12":
                    scenario["request_ids"] = [receipt["request_id"]]
                    scenario["turn_ids"] = [receipt["turn_id"]]
                    scenario["trace_ids"] = [receipt["trace_id"]]
                    assertions = scenario.get("assertions")
                    if isinstance(assertions, dict):
                        assertions.update(
                            {
                                "objective_answer_correct": receipt[
                                    "final_answer_correct"
                                ],
                                "reasoning_capability_enabled": (
                                    receipt["reasoning_event_count"] == 1
                                ),
                                "reasoning_requested_effort_medium": (
                                    receipt["requested_effort"] == "medium"
                                ),
                                "reasoning_configured_effort_medium": (
                                    receipt["configured_effort"] == "medium"
                                    and receipt["configured_effort_attested"] is True
                                ),
                                "reasoning_effective_effort_not_attested": (
                                    receipt["effective_effort"] == "unknown"
                                    and receipt["effective_effort_attested"] is False
                                ),
                                "reasoning_event_observed": (
                                    receipt["reasoning_event_count"] == 1
                                ),
                                "reasoning_metadata_present": receipt[
                                    "metadata_present"
                                ],
                                "reasoning_tokens_present": (
                                    receipt["token_metadata_status"] == "PRESENT"
                                ),
                                "user_disclosure_present": receipt[
                                    "user_visible_disclosure_present"
                                ],
                                "raw_private_reasoning_omitted": True,
                            }
                        )
                    scenario_status, failure_code = _cot_scenario_projection(receipt)
                    failure = (
                        None
                        if failure_code is None
                        else {
                            "category": scenario_status,
                            "stage_code": "REASONING",
                            "failure_code": failure_code,
                            "reproducible": True,
                        }
                    )
                    scenario["status"] = scenario_status
                    scenario["failure"] = failure
                    scenario["attempts"] = 1
                    scenario["attempt_results"] = [
                        {"attempt": 1, "status": scenario_status, "failure": failure}
                    ]
                    if scenario_status != "PASS":
                        result["status"] = scenario_status
                    break
        spec.result_path.write_text(json.dumps(result) + "\n")

    spec.cot_request_path.write_text(f"{spec.profile_id}\n")
    spec.cot_request_path.chmod(0o600)


def _cot_probe_runner(mode: str = "valid") -> launcher.CotProbeRunner:
    def run(spec: launcher.WorkerSpec) -> dict[str, Any]:
        receipt = _passing_cot_receipt(spec.profile_id)
        if mode == "failed":
            receipt.update(
                status="FAIL",
                failure_code="FINAL_ANSWER_WRONG",
                delivery_qualified=False,
                final_answer_correct=False,
            )
        elif mode in {"request-failed", "request-failed-after-ack"}:
            receipt = _request_failed_cot_receipt(
                spec.profile_id, after_ack=mode == "request-failed-after-ack"
            )
        if mode == "missing":
            return receipt
        if mode == "malformed":
            spec.cot_receipt_path.write_text("{}\n")
            spec.cot_receipt_path.chmod(0o600)
            return receipt
        if mode not in {
            "valid",
            "failed",
            "request-failed",
            "request-failed-after-ack",
        }:
            raise AssertionError(f"unknown trusted COT fixture mode: {mode}")
        spec.cot_receipt_path.write_text(
            json.dumps(receipt, sort_keys=True) + "\n"
        )
        spec.cot_receipt_path.chmod(0o600)
        return receipt

    return run


def _passing_live_probe(
    spec: launcher.WorkerSpec,
    scenario_id: str,
    attempt: int,
    nonce: str,
    prior_receipts: tuple[Mapping[str, Any], ...],
) -> tuple[dict[str, Any], dict[str, Any]]:
    turn_count = {
        "P0-02": 0,
        "P0-03": 0,
        "P0-04": 0,
        "P0-05": 0,
        "P0-06": 0,
        "P0-07": 0,
        "P0-08": 1,
        "P0-09": 10,
        "P0-10": 2,
        "P0-11": 1,
        "P0-13": 15,
    }[scenario_id]
    semantic = scenario_id in {"P0-10", "P0-11"}
    stage_latency = {
        "routing": 1.0,
        "queue": 2.0,
        "provider": 3.0,
        "persistence": 4.0,
        "delivery": 5.0,
    }
    if scenario_id == "P0-13":
        turns = []
        for receipt in prior_receipts:
            if receipt.get("scenario_id") not in {
                "P0-08",
                "P0-09",
                "P0-10",
                "P0-11",
            }:
                continue
            for prior_turn in receipt.get("turns", []):
                turns.append(
                    {
                        key: prior_turn[key]
                        for key in (
                            "request_id",
                            "turn_id",
                            "trace_id",
                            "ack_latency_ms",
                            "reply_latency_ms",
                            "reply_count",
                            "content_assertion_passed",
                            "fallback_detected",
                            "duplicate_detected",
                            "out_of_order_detected",
                        )
                    }
                )
        try:
            cot, _ = cot_receipts.validate_cot_receipt(
                spec.cot_receipt_path, spec.profile_id
            )
        except (cot_receipts.CotReceiptError, OSError):
            cot = None
        cot_ids = (
            tuple(cot[field] for field in ("request_id", "turn_id", "trace_id"))
            if cot is not None
            else ()
        )
        if cot is not None and all(cot_ids):
            turns.append(
                {
                    "request_id": cot["request_id"],
                    "turn_id": cot["turn_id"],
                    "trace_id": cot["trace_id"],
                    "ack_latency_ms": cot["ack_latency_ms"],
                    "reply_latency_ms": cot["reply_latency_ms"],
                    "reply_count": cot["chat_response_match_count"],
                    "content_assertion_passed": cot["final_answer_correct"],
                    "fallback_detected": False,
                    "duplicate_detected": cot["chat_response_count"] != 1,
                    "out_of_order_detected": (
                        cot["chat_response_match_count"] != 1
                    ),
                }
            )
        assert len(turns) <= turn_count
        turns = [
            {
                "turn_index": index,
                **turn,
                "stage_latency_ms": dict(stage_latency),
            }
            for index, turn in enumerate(turns, start=1)
        ]
    else:
        turns = [
            {
                "turn_index": index,
                "request_id": f"req-{scenario_id.lower()}-{attempt}-{index}",
                "turn_id": f"req-{scenario_id.lower()}-{attempt}-{index}",
                "trace_id": f"req-{scenario_id.lower()}-{attempt}-{index}",
                "ack_latency_ms": float(index * 10),
                "reply_latency_ms": float(index * 100),
                "stage_latency_ms": dict(stage_latency),
                "reply_count": 1,
                "content_assertion_passed": None if semantic else True,
                "fallback_detected": False,
                "duplicate_detected": False,
                "out_of_order_detected": False,
            }
            for index in range(1, turn_count + 1)
        ]
    diagnostic_cleanup = (
        scenario_id == "P0-13"
        and spec.environment["QA_QUALIFICATION_MODE"] == "diagnostic"
    )
    incomplete_trace = scenario_id == "P0-13" and len(turns) != turn_count
    if diagnostic_cleanup:
        status, failure_code = "BLOCKED_EVIDENCE", "PRECONDITION_MISSING"
    elif incomplete_trace:
        status, failure_code = "BLOCKED_EVIDENCE", "TRACE_UNAVAILABLE"
    else:
        status, failure_code = "PASS", "NONE"
    assertions = {
        key: True
        for key in live_receipts.DETERMINISTIC_ASSERTIONS[scenario_id]
    }
    if diagnostic_cleanup:
        assertions["cleanup_confirmed"] = False
        assertions["trace_correlation_confirmed"] = len(turns) == turn_count
    elif incomplete_trace:
        assertions["trace_correlation_confirmed"] = False
    projection: dict[str, Any] | None = None
    if scenario_id == "P0-06":
        projection = {
            "kind": "persona_capture",
            "evidence_sha256": "1" * 64,
            "job_id": "persona-job-1",
            "archive_upload_count": 4,
            "archive_receipts_verified": True,
            "genesis_upload_metadata_verified": True,
        }
    elif scenario_id == "P0-13":
        projection = {
            "kind": "trace_cleanup",
            "latency": live_receipts.latency_projection(turns),
            "trace": {
                "enabled": True,
                "deploy_enabled": True,
                "correlated_event_count": len(turns) * 5,
                "observed_event_types": [
                    "routing",
                    "queue",
                    "provider",
                    "persistence",
                    "delivery",
                ],
                "missing_required_event_types": [],
                "raw_trace_stored": False,
            },
            "cleanup": {
                "attempted": not diagnostic_cleanup,
                "provider_config_deleted": not diagnostic_cleanup,
                "account_reset": not diagnostic_cleanup,
                "old_credential_rejected": not diagnostic_cleanup,
                "status": "BLOCKED_EVIDENCE" if diagnostic_cleanup else "PASS",
            },
        }
    private_facts = {
        "schema_version": 1,
        "run_id": spec.environment["QA_RUN_ID"],
        "profile_id": spec.profile_id,
        "scenario_id": scenario_id,
        "attempt": attempt,
        "observations": {
            "reply_texts": ["private semantic fixture"] if semantic else []
        },
    }
    ids = [turn["request_id"] for turn in turns]
    receipt = {
        "schema_version": 1,
        "kind": "live_scenario_probe",
        "run_id": spec.environment["QA_RUN_ID"],
        "profile_id": spec.profile_id,
        "scenario_id": scenario_id,
        "attempt": attempt,
        "nonce": nonce,
        "started_at": "2026-01-01T00:00:00.000000Z",
        "finished_at": "2026-01-01T00:00:01.000000Z",
        "status": status,
        "failure_code": failure_code,
        "assertions": assertions,
        "semantic_assertions": list(
            live_receipts.SEMANTIC_ASSERTIONS[scenario_id]
        ),
        "request_ids": (
            [f"probe-{scenario_id.lower()}-{attempt}"]
            if scenario_id == "P0-13"
            else ids or [f"probe-{scenario_id.lower()}-{attempt}"]
        ),
        "turn_ids": ids,
        "trace_ids": ids,
        "turns": turns,
        "result_projection": projection,
        "private_facts_sha256": live_receipts.canonical_json_sha256(private_facts),
        "raw_content_stored": False,
    }
    return receipt, private_facts


def _live_probe_runner(
    spec: launcher.WorkerSpec,
    scenario_id: str,
    attempt: int,
    nonce: str,
    prior_receipts: tuple[Mapping[str, Any], ...],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    return _passing_live_probe(
        spec, scenario_id, attempt, nonce, prior_receipts
    )


def _persona_finalize_runner(
    _spec: launcher.WorkerSpec, capture_receipt: Mapping[str, Any]
) -> Mapping[str, Any]:
    capture = capture_receipt["result_projection"]
    return {
        "kind": "persona_finalizer",
        "semantic_assertions": {
            "persona_acceptance_passed": True,
            "privacy_canary_absent": True,
        },
        "persona_finalizer": {
            "fixture_id": "persona-import-v1",
            "evidence_sha256": capture["evidence_sha256"],
            "request_id": capture_receipt["request_ids"][0],
            "job_id": capture["job_id"],
            "semantic_judgment_bound": True,
            "finalizer_ok": True,
            "private_evidence_deleted": True,
            "archive_upload_count": capture["archive_upload_count"],
            "archive_receipts_verified": capture[
                "archive_receipts_verified"
            ],
            "genesis_upload_metadata_verified": capture[
                "genesis_upload_metadata_verified"
            ],
            "privacy_violation_count": 0,
        },
    }


def _request_passing_live_probes(spec: launcher.WorkerSpec) -> None:
    receipts: list[dict[str, Any]] = []
    for scenario_id in live_request.LIVE_SCENARIO_IDS:
        request_path = live_request.request_path(spec.work, scenario_id, 1)
        facts_path = live_request.facts_path(spec.work, scenario_id, 1)
        facts = live_request.request_and_wait(
            scenario_id=scenario_id,
            attempt=1,
            request=request_path,
            facts=facts_path,
            environment=spec.environment,
            # The launcher and three fake Codex processes are intentionally
            # concurrent; leave headroom for heavily loaded full-suite runners.
            wait_seconds=15,
        )
        assert facts["receipt_sha256"] == live_receipts.canonical_json_sha256(
            facts["receipt"]
        )
        receipts.append(facts["receipt"])
        if scenario_id == "P0-06" and facts["receipt"]["status"] == "PASS":
            # The production CAPTURE probe publishes a worker review copy and
            # the agent's FINALIZE command deletes it after authoring this
            # judgment.  Fake probes omit plaintext by design, so represent
            # only that completed, owner-only offline phase boundary.
            judgment = spec.work / "p0-06-semantic-judgment.json"
            judgment.write_text("{}\n", encoding="utf-8")
            judgment.chmod(0o600)

    result = json.loads(spec.result_path.read_text())
    scenarios = {
        row["scenario_id"]: row
        for row in result["scenarios"]
        if isinstance(row, dict)
    }
    bound_turns: list[dict[str, Any]] = []
    for receipt in receipts:
        scenario = scenarios[receipt["scenario_id"]]
        scenario_status = receipt["status"]
        canonical_failure_code = {
            "LIVE_PROBE_ERROR": "TRACE_UNAVAILABLE",
            "ASSERTION_FAILED": "UNCLASSIFIED_PRODUCT_FAILURE",
        }.get(receipt["failure_code"], receipt["failure_code"])
        scenario_failure = (
            None
            if scenario_status == "PASS"
            else {
                "category": scenario_status,
                "stage_code": (
                    "CLEANUP"
                    if receipt["failure_code"] == "PRECONDITION_MISSING"
                    else "TRACE_LATENCY_CLEANUP"
                ),
                "failure_code": canonical_failure_code,
                "reproducible": True,
            }
        )
        scenario.update(
            {
                "status": scenario_status,
                "started_at": receipt["started_at"],
                "finished_at": receipt["finished_at"],
                "attempts": 1,
                "attempt_results": [
                    {
                        "attempt": 1,
                        "status": scenario_status,
                        "failure": scenario_failure,
                    }
                ],
                "request_ids": receipt["request_ids"],
                "turn_ids": receipt["turn_ids"],
                "trace_ids": receipt["trace_ids"],
                "failure": scenario_failure,
            }
        )
        for key, value in receipt["assertions"].items():
            if key in scenario["assertions"]:
                scenario["assertions"][key] = value
        if receipt["scenario_id"] == "P0-06":
            projection = receipt["result_projection"]
            if projection is None:
                failed = live_receipts.failed_persona_result_projection(receipt)
                scenario.update(
                    {
                        **failed,
                        "attempt_results": [
                            {
                                "attempt": 1,
                                "status": failed["status"],
                                "failure": failed["failure"],
                            }
                        ],
                    }
                )
            else:
                scenario["assertions"] = {
                    **receipt["assertions"],
                    "persona_acceptance_passed": True,
                    "privacy_canary_absent": True,
                }
                scenario["evidence_codes"] = [
                    "PERSONA_FILES_ARCHIVED",
                    "PERSONA_SOURCE_METADATA_VERIFIED",
                    "PERSONA_IMPORT_DONE",
                    "PERSONA_ACCEPTANCE_PASSED",
                    "PRIVACY_CANARY_ABSENT",
                ]
                scenario["persona_finalizer"] = {
                    "fixture_id": "persona-import-v1",
                    "evidence_sha256": projection["evidence_sha256"],
                    "request_id": receipt["request_ids"][0],
                    "job_id": projection["job_id"],
                    "semantic_judgment_bound": True,
                    "finalizer_ok": True,
                    "private_evidence_deleted": True,
                    "archive_upload_count": projection["archive_upload_count"],
                    "archive_receipts_verified": projection[
                        "archive_receipts_verified"
                    ],
                    "genesis_upload_metadata_verified": projection[
                        "genesis_upload_metadata_verified"
                    ],
                    "privacy_violation_count": 0,
                }
        if receipt["scenario_id"] == "P0-13":
            scenario["assertions"] = dict(receipt["assertions"])
            scenario["evidence_codes"] = [
                code
                for assertion, code in (
                    ("trace_correlation_confirmed", "TRACE_CORRELATION_CONFIRMED"),
                    ("latency_attributed", "LATENCY_ATTRIBUTED"),
                    ("cleanup_confirmed", "CLEANUP_CONFIRMED"),
                )
                if receipt["assertions"][assertion]
            ]
            continue
        for turn in receipt["turns"]:
            bound_turns.append(
                {
                    "scenario_id": receipt["scenario_id"],
                    "turn_index": turn["turn_index"],
                    "request_id": turn["request_id"],
                    "turn_id": turn["turn_id"],
                    "trace_id": turn["trace_id"],
                    "ack_latency_ms": turn["ack_latency_ms"],
                    "reply_latency_ms": turn["reply_latency_ms"],
                    "stage_latency_ms": turn["stage_latency_ms"],
                    "reply_count": turn["reply_count"],
                    "content_assertion_passed": (
                        True
                        if turn["content_assertion_passed"] is None
                        else turn["content_assertion_passed"]
                    ),
                    "fallback_detected": turn["fallback_detected"],
                    "duplicate_detected": turn["duplicate_detected"],
                    "out_of_order_detected": turn["out_of_order_detected"],
                }
            )
    trace_cleanup_receipt = next(
        receipt for receipt in receipts if receipt["scenario_id"] == "P0-13"
    )
    trace_cleanup_projection = trace_cleanup_receipt["result_projection"]
    if trace_cleanup_projection is None:
        bound_turns = [
            {
                **turn,
                "stage_latency_ms": {
                    stage: None
                    for stage in (
                        "routing",
                        "queue",
                        "provider",
                        "persistence",
                        "delivery",
                    )
                },
            }
            for turn in bound_turns
        ]
        result["turns"] = bound_turns
        result["latency"] = live_receipts.latency_projection(bound_turns)
        result["trace"] = {
            "enabled": False,
            "deploy_enabled": False,
            "correlated_event_count": 0,
            "observed_event_types": [],
            "missing_required_event_types": [
                "routing",
                "queue",
                "provider",
                "persistence",
                "delivery",
            ],
            "raw_trace_stored": False,
        }
        result["cleanup"] = {
            "attempted": False,
            "provider_config_deleted": False,
            "account_reset": False,
            "old_credential_rejected": False,
            "status": "BLOCKED_EVIDENCE",
        }
        result["status"] = "BLOCKED_EVIDENCE"
        result["diagnostic_codes"] = [
            "CLEANUP_FALLBACK_USED",
            "STAGE_TIMING_UNAVAILABLE",
            "TRACE_PARTIAL",
        ]
        spec.result_path.write_text(json.dumps(result) + "\n")
        return
    projected_by_trace = {
        turn["trace_id"]: turn for turn in trace_cleanup_receipt["turns"]
    }
    for turn in bound_turns:
        turn["stage_latency_ms"] = projected_by_trace[turn["trace_id"]][
            "stage_latency_ms"
        ]
    existing_trace_ids = {turn["trace_id"] for turn in bound_turns}
    cot_turns = [
        turn
        for turn in trace_cleanup_receipt["turns"]
        if turn["trace_id"] not in existing_trace_ids
    ]
    assert len(cot_turns) <= 1
    if cot_turns:
        cot_turn = cot_turns[0]
        bound_turns.append(
            {
                "scenario_id": "P0-12",
                **cot_turn,
                "content_assertion_passed": bool(
                    cot_turn["content_assertion_passed"]
                ),
            }
        )
    result["turns"] = bound_turns
    result["latency"] = trace_cleanup_projection["latency"]
    result["trace"] = trace_cleanup_projection["trace"]
    result["cleanup"] = trace_cleanup_projection["cleanup"]
    if spec.environment["QA_QUALIFICATION_MODE"] == "diagnostic":
        _apply_diagnostic_parent_cleanup_deferral(result)
    spec.result_path.write_text(json.dumps(result) + "\n")


def _wrapped_codex_command(payload: str) -> str:
    return f"/bin/zsh -c {shlex.quote(payload)}"


def _scenario_command_rows() -> list[dict[str, Any]]:
    commands = {
        "P0-06": (
            "QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=CAPTURE "
            '"$QA_PYTHON_BIN" '
            '"$QA_SOURCE_ROOT/qa/request_live_scenario_probe.py" '
            "--scenario P0-06 --attempt 1 "
            '--request "$QA_WORK_ROOT/.live-probe-P0-06-1.request" '
            '--facts "$QA_WORK_ROOT/live-probe-P0-06-1.facts.json"',
            "QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=REVIEW "
            '"$QA_PYTHON_BIN" -I -B -c '
            f"{shlex.quote(verifier.P0_06_REVIEW_PROGRAM)} "
            '"$QA_WORK_ROOT/p0-06-private-evidence.json" '
            '"$QA_WORK_ROOT/p0-06-semantic-judgment.json"',
            "QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=FINALIZE "
            '"$QA_PYTHON_BIN" -I -B '
            '"$QA_SOURCE_ROOT/qa/finalize_persona_review.py" '
            '--fixture "$QA_SOURCE_ROOT/qa/fixtures/persona-import-v1.json" '
            '--private-evidence "$QA_WORK_ROOT/p0-06-private-evidence.json" '
            '--semantic-judgment "$QA_WORK_ROOT/p0-06-semantic-judgment.json" '
            '--artifact-dir "$QA_ARTIFACT_DIR"',
        )
    }
    return [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": _wrapped_codex_command(command),
                "status": "completed",
                "exit_code": 0,
            },
        }
        for scenario_id in verifier.AGENT_LIVE_SCENARIO_IDS
        for command in (
            commands[scenario_id]
            if scenario_id in commands
            else (verifier.cot_request_command(),)
            if scenario_id == "P0-12"
            else (verifier.live_request_command(scenario_id, 1),)
        )
    ]


def _successful_runner(
    captured: list[launcher.WorkerSpec],
    *,
    duplicate_thread: bool = False,
    invalid_result: bool = False,
    extra_file: bool = False,
    omit_command: bool = False,
    omit_scenario_commands: bool = False,
    omit_persona_review_commands: bool = False,
    generic_persona_markers: bool = False,
    extra_persona_marker: bool = False,
    unsafe_extra_persona_marker: bool = False,
    failed_scenario_command: str | None = None,
    wrong_sop_read: bool = False,
    cot_mode: str = "valid",
    synchronize: bool = True,
) -> launcher.ProcessRunner:
    lock = threading.Lock()
    cap = verifier.MAX_CONFIGURED_CONCURRENCY
    barriers = {
        offset // cap: threading.Barrier(min(cap, len(PROFILE_AGENT_TYPES) - offset))
        for offset in range(0, len(PROFILE_AGENT_TYPES), cap)
    }

    def run(spec: launcher.WorkerSpec, timeout: int) -> int:
        assert timeout == 600
        with lock:
            captured.append(spec)
            index = PROFILE_AGENT_TYPES.index((spec.profile_id, spec.agent_type))
        barrier = barriers[index // cap]
        # Nested fake-worker threads can take more than five seconds to be
        # scheduled on a loaded GitHub runner.  Keep this finite so a real
        # launcher concurrency regression still fails instead of hanging.
        if synchronize:
            barrier.wait(timeout=30)
        schema = json.loads(spec.schema_path.read_text())
        result = _instance(schema, schema["$defs"])
        if invalid_result and index == 0:
            result.pop("profile_id")
        spec.result_path.write_text(json.dumps(result) + "\n")
        thread_index = 0 if duplicate_thread else index
        thread_id = f"30000000-0000-4000-8000-{thread_index:012d}"
        rows = [
            {
                "type": "thread.started",
                "thread_id": thread_id,
                "session_id": f"40000000-0000-4000-8000-{thread_index:012d}",
            },
            {"type": "turn.started"},
        ]
        if not omit_command:
            sop_command = (
                "sed -n '1,999p' qa/SOP.md"
                if wrong_sop_read
                else verifier.MANDATORY_SOP_READ_COMMAND
            )
            sop_command = _wrapped_codex_command(sop_command)
            rows.extend(
                (
                    {
                        "type": "item.started",
                        "item": {
                            "type": "command_execution",
                            "command": sop_command,
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": sop_command,
                            "status": "completed",
                            "exit_code": 0,
                        },
                    },
                )
            )
            if not omit_scenario_commands:
                scenario_rows = _scenario_command_rows()
                if generic_persona_markers:
                    scenario_rows = [
                        row
                        for row in scenario_rows
                        if "QA_SCENARIO_ID=P0-06" not in row["item"]["command"]
                    ]
                    scenario_rows.extend(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "command_execution",
                                "command": _wrapped_codex_command(
                                    "QA_SCENARIO_ID=P0-06 "
                                    '"$QA_PYTHON_BIN" -c true'
                                ),
                                "status": "completed",
                                "exit_code": 0,
                            },
                        }
                        for _ in range(3)
                    )
                if extra_persona_marker:
                    scenario_rows.append(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "command_execution",
                                "command": _wrapped_codex_command(
                                    "QA_SCENARIO_ID=P0-06 "
                                    '"$QA_PYTHON_BIN" -c true'
                                ),
                                "status": "completed",
                                "exit_code": 0,
                            },
                        }
                    )
                if unsafe_extra_persona_marker:
                    scenario_rows.append(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "command_execution",
                                "command": _wrapped_codex_command(
                                    "QA_SCENARIO_ID=P0-06 "
                                    '"$QA_PYTHON_BIN" -c true; '
                                    "echo hidden-extra"
                                ),
                                "status": "completed",
                                "exit_code": 0,
                            },
                        }
                    )
                if omit_persona_review_commands:
                    kept_p06 = False
                    filtered = []
                    for row in scenario_rows:
                        command = row["item"]["command"]
                        if "QA_SCENARIO_ID=P0-06" in command:
                            if kept_p06:
                                continue
                            kept_p06 = True
                        filtered.append(row)
                    scenario_rows = filtered
                if failed_scenario_command:
                    for row in scenario_rows:
                        if (
                            f"QA_SCENARIO_ID={failed_scenario_command}"
                            in row["item"]["command"]
                        ):
                            row["item"]["exit_code"] = 1
                            break
                rows.extend(scenario_rows)
        rows.append({"type": "turn.completed", "usage": {}})
        # The production process runner streams parent-owned Codex events while
        # the worker is still running.  This fixture separately exercises final
        # event validation modes, so first publish an exact gate transcript,
        # then replace it with the requested final transcript after all probe
        # helpers have reached terminal exit.
        gate_rows = [rows[0], rows[1], *_scenario_command_rows()]
        staged_events = spec.events_path.with_name(".events-fixture.tmp")
        staged_events.write_text(
            "".join(json.dumps(row) + "\n" for row in gate_rows)
        )
        staged_events.chmod(0o600)
        os.replace(staged_events, spec.events_path)
        expected_receipt = _passing_cot_receipt(spec.profile_id)
        if cot_mode in {"failed", "failed-status-mismatch"}:
            expected_receipt.update(
                status="FAIL",
                failure_code="FINAL_ANSWER_WRONG",
                delivery_qualified=False,
                final_answer_correct=False,
            )
        elif cot_mode in {"request-failed", "request-failed-after-ack"}:
            expected_receipt = _request_failed_cot_receipt(
                spec.profile_id,
                after_ack=cot_mode == "request-failed-after-ack",
            )
        _request_passing_cot_probe(spec, expected_receipt)
        _request_passing_live_probes(spec)
        if cot_mode == "binding-mismatch":
            result = json.loads(spec.result_path.read_text())
            result["reasoning"]["disclosure_length"] += 1
            spec.result_path.write_text(json.dumps(result) + "\n")
        elif cot_mode == "failed-status-mismatch":
            result = json.loads(spec.result_path.read_text())
            result["status"] = "PASS"
            cot_scenario = next(
                row for row in result["scenarios"] if row["scenario_id"] == "P0-12"
            )
            cot_scenario["status"] = "PASS"
            cot_scenario["failure"] = None
            cot_scenario["attempt_results"] = [
                {"attempt": 1, "status": "PASS", "failure": None}
            ]
            spec.result_path.write_text(json.dumps(result) + "\n")
        elif cot_mode not in {
            "valid",
            "missing",
            "malformed",
            "failed",
            "request-failed",
            "request-failed-after-ack",
        }:
            raise AssertionError(f"unknown COT fixture mode: {cot_mode}")
        staged_final_events = spec.events_path.with_name(
            ".events-final-fixture.tmp"
        )
        staged_final_events.write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )
        staged_final_events.chmod(0o600)
        os.replace(staged_final_events, spec.events_path)
        if extra_file and index == 0:
            extra = spec.output_dir / "extra.txt"
            extra.write_text("unexpected\n")
            extra.chmod(0o600)
        return 0

    return run


def _launch(
    paths: dict[str, Any],
    runner: launcher.ProcessRunner,
    *,
    cot_probe_runner: launcher.CotProbeRunner | None = None,
    live_probe_runner: launcher.LiveProbeRunner | None = None,
    persona_finalize_runner: launcher.PersonaFinalizeRunner | None = None,
) -> dict[str, Any]:
    def traced_runner(spec: launcher.WorkerSpec, timeout: int) -> int:
        try:
            return runner(spec, timeout)
        except Exception as exc:
            print(
                f"fake worker {spec.profile_id} failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            raise

    return launcher.launch(
        codex_bin=paths["codex_bin"],
        codex_home=paths["codex_home"],
        source_root=paths["source"],
        artifact_root=paths["artifacts"],
        profile_manifest_dir=paths["manifests"],
        worker_root=paths["worker_root"],
        worker_output_root=paths["raw"],
        aggregation_input_root=paths["aggregation"],
        authoring_schema_path=paths["schema"],
        receipt_path=paths["receipt"],
        run_id="run-123",
        base_url="https://test-api.feedling.app",
        expected_sha="a" * 40,
        timeout_seconds=600,
        process_runner=traced_runner,
        cot_probe_runner=cot_probe_runner or _cot_probe_runner(),
        live_probe_runner=live_probe_runner or _live_probe_runner,
        persona_finalize_runner=(
            persona_finalize_runner or _persona_finalize_runner
        ),
        worker_python=paths["worker_python"],
    )


def _launch_diagnostic(
    paths: dict[str, Any],
    runner: launcher.ProcessRunner,
    *,
    profile_ids: tuple[str, ...] = ("official-gemini",),
    cot_probe_runner: launcher.CotProbeRunner | None = None,
    live_probe_runner: launcher.LiveProbeRunner | None = None,
    persona_finalize_runner: launcher.PersonaFinalizeRunner | None = None,
) -> dict[str, Any]:
    return launcher.launch(
        codex_bin=paths["codex_bin"],
        codex_home=paths["codex_home"],
        source_root=paths["source"],
        artifact_root=paths["artifacts"],
        profile_manifest_dir=paths["manifests"],
        worker_root=paths["worker_root"],
        worker_output_root=paths["raw"],
        aggregation_input_root=paths["aggregation"],
        authoring_schema_path=paths["schema"],
        receipt_path=paths["receipt"],
        run_id="diagnostic-123",
        base_url="https://test-api.feedling.app",
        expected_sha="b" * 40,
        timeout_seconds=600,
        process_runner=runner,
        cot_probe_runner=cot_probe_runner or _cot_probe_runner(),
        live_probe_runner=live_probe_runner or _live_probe_runner,
        persona_finalize_runner=(
            persona_finalize_runner or _persona_finalize_runner
        ),
        diagnostic=True,
        profile_ids=profile_ids,
        expected_runtime="hosted_resident",
        worker_python=paths["worker_python"],
    )


def test_generated_profile_prompt_requires_one_live_command_until_terminal_exit(
    tmp_path,
):
    paths = _setup(tmp_path)
    specs = launcher._prepare_specs(
        codex_bin=paths["codex_bin"],
        codex_home=paths["codex_home"],
        source_root=paths["source"],
        artifact_root=paths["artifacts"],
        profile_manifest_dir=paths["manifests"],
        worker_root=paths["worker_root"],
        worker_output_root=paths["raw"],
        authoring_schema=launcher._load_authoring_schema(paths["schema"]),
        worker_python=paths["worker_python"],
        run_id="run-serial-command-contract",
        base_url="https://test-api.feedling.app",
        expected_sha="a" * 40,
        assignments=(PROFILE_AGENT_TYPES[0],),
    )

    prompt = specs[0].prompt
    required_contract = (
        "Exactly one live-scenario command may be active at a time.",
        "A yielded tool call that reports a running cell or session is not complete",
        "poll or wait on that same execution identifier until it reaches terminal exit",
        "do not start another tool call, issue another scenario request, or read any facts file",
        "Only after terminal exit may you read that command's facts",
        "reviewed_fact_ids",
        "persona_identity_consistent",
        "ground_truth_facts_supported",
        "contradictions_absent",
        "do not write `expected_fact_ids`, `consistency`, `support`, or `contradiction`",
    )
    for instruction in required_contract:
        assert instruction in " ".join(prompt.split())


def test_parent_probe_deadlines_finish_before_agent_and_profile_deadlines():
    longest_live_probe = max(
        launcher._DEFAULT_LIVE_PROBE_TIMEOUT_SECONDS,
        *launcher._LIVE_PROBE_TIMEOUT_SECONDS.values(),
    )

    assert launcher._COT_PROBE_TIMEOUT_SECONDS < 360
    assert longest_live_probe < live_request.FACTS_WAIT_SECONDS < 3600


def test_launcher_runs_exact_matrix_at_peak_three_without_secrets(
    tmp_path, monkeypatch
):
    assert PROFILE_AGENT_TYPES == (
        ("official-deepseek", "profile_official_deepseek"),
        ("official-anthropic", "profile_official_anthropic"),
        ("official-openai", "profile_official_openai"),
        ("official-gemini", "profile_official_gemini"),
        ("openrouter-claude", "profile_openrouter_claude"),
        ("openrouter-openai", "profile_openrouter_openai"),
        ("openrouter-glm", "profile_openrouter_glm"),
        ("relay-kongbeiqie", "profile_relay_kongbeiqie"),
    )
    paths = _setup(tmp_path)
    for name in (
        "IO_E2E_ADMIN_TOKEN",
        "FEEDLING_ADMIN_TOKEN",
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
        "QA_GEMINI_MODEL",
        "QA_KONGBEIQIE_MODEL",
        "QA_KONGBEIQIE_BASE_URL",
        "QA_CODEX_AUTH_JSON_B64",
    ):
        monkeypatch.setenv(name, "must-not-cross-boundary")
    captured: list[launcher.WorkerSpec] = []
    receipt = _launch(paths, _successful_runner(captured))

    assert receipt["schema_version"] == 4
    assert receipt["launch_attempts"] == len(PROFILE_AGENT_TYPES)
    assert receipt["max_configured_profile_concurrency"] == 3
    assert receipt["max_observed_profile_concurrency"] == 3
    assert [
        (row["profile_id"], row["agent_type"]) for row in receipt["workers"]
    ] == list(PROFILE_AGENT_TYPES)
    assert len({row["thread_id"] for row in receipt["workers"]}) == len(
        PROFILE_AGENT_TYPES
    )
    assert [row["permission_profile"] for row in receipt["workers"]] == [
        f"io-e2e-agent-driven-test-{profile_id}" for profile_id, _ in PROFILE_AGENT_TYPES
    ]
    assert all(len(row["cot_receipt_sha256"]) == 64 for row in receipt["workers"])
    assert all(len(row["live_receipt_sha256"]) == 64 for row in receipt["workers"])
    assert {row["cot_delivery_status"] for row in receipt["workers"]} == {"PASS"}
    assert {row["cot_failure_code"] for row in receipt["workers"]} == {"NONE"}
    assert (
        verifier.verify(paths["receipt"], paths["raw"], paths["aggregation"]) == receipt
    )
    assert {path.name for path in paths["aggregation"].iterdir()} == {
        f"{profile_id}.json" for profile_id, _ in PROFILE_AGENT_TYPES
    }
    assert len(captured) == len(PROFILE_AGENT_TYPES)
    for spec in captured:
        assert spec.command[1:6] == (
            "exec",
            "-p",
            spec.agent_type,
            "-c",
            f'default_permissions="io-e2e-agent-driven-test-{spec.profile_id}"',
        )
        assert spec.command[8:10] == ("--disable", "network_proxy")
        assert "--ephemeral" not in spec.command
        assert "spawn_agent" not in spec.prompt
        assert "first response action MUST be a shell command execution" in spec.prompt
        assert "MUST NOT short-circuit P0-02 through" in spec.prompt
        assert "QA_SCENARIO_ID=P0-XX" in spec.prompt
        assert "request_live_scenario_probe.py" in spec.prompt
        assert "This is a live qualification run, not a source-code review" in (
            spec.prompt
        )
        assert "Do not inspect QA implementation files or tests" in spec.prompt
        assert "Never invoke qa/cot_delivery_probe.py" in spec.prompt
        assert spec.environment["QA_PRIVATE_MANIFEST"].endswith(
            f"/{spec.profile_id}.json"
        )
        assert spec.environment["HOME"].endswith(f"/{spec.agent_type}/home")
        assert spec.environment["TMPDIR"].endswith(f"/{spec.agent_type}/tmp")
        assert spec.environment["QA_WORK_ROOT"].endswith(f"/{spec.agent_type}/work")
        assert spec.environment["QA_ARTIFACT_DIR"] == str(paths["artifacts"].resolve())
        assert spec.environment["QA_PYTHON_BIN"] == str(
            paths["worker_python"].resolve()
        )
        assert spec.environment["QA_QUALIFICATION_MODE"] == "release"
        assert spec.cot_receipt_path.parent == spec.output_dir
        assert spec.work not in spec.cot_receipt_path.parents
        assert spec.cot_request_path.parent == spec.work
        assert spec.cot_facts_path.parent == spec.work
        assert spec.cot_receipt_path.exists()
        assert spec.live_receipt_path.exists()
        live_set, live_sha = live_receipts.validate_live_scenario_receipts(
            spec.live_receipt_path,
            run_id="run-123",
            profile_id=spec.profile_id,
        )
        assert len(live_set["receipts"]) == len(live_request.LIVE_SCENARIO_IDS)
        receipts_by_scenario = {
            row["scenario_id"]: row for row in live_set["receipts"]
        }
        assert receipts_by_scenario["P0-06"]["result_projection"]["kind"] == (
            "persona_capture"
        )
        assert receipts_by_scenario["P0-13"]["result_projection"]["kind"] == (
            "trace_cleanup"
        )
        assert len(receipts_by_scenario["P0-13"]["turns"]) == 15
        assert live_set["persona_finalizer"] == _persona_finalize_runner(
            spec, receipts_by_scenario["P0-06"]
        )
        assert len(live_sha) == 64
        facts = json.loads(spec.cot_facts_path.read_text())
        assert facts["profile_id"] == spec.profile_id
        assert len(facts["receipt_sha256"]) == 64
        assert facts["receipt"]["status"] == "PASS"
        assert not (spec.work / "cot-delivery-receipt.json").exists()
        assert not any(
            name in spec.environment
            for name in (
                "IO_E2E_ADMIN_TOKEN",
                "FEEDLING_ADMIN_TOKEN",
                "QA_DEEPSEEK_API_KEY",
                "QA_ANTHROPIC_API_KEY",
                "QA_OPENAI_PROVIDER_API_KEY",
                "QA_OPENROUTER_API_KEY",
                "QA_GEMINI_API_KEY",
                "QA_KONGBEIQIE_API_KEY",
                "QA_GEMINI_MODEL",
                "QA_KONGBEIQIE_MODEL",
                "QA_KONGBEIQIE_BASE_URL",
                "QA_CODEX_AUTH_JSON_B64",
            )
        )


def test_fake_worker_completion_is_not_coupled_to_slowest_peer(tmp_path):
    """A slow parent probe must not break an unrelated fake worker barrier."""

    paths = _setup(tmp_path)
    delayed = False

    def slow_probe(spec, scenario_id, attempt, nonce, prior_receipts):
        nonlocal delayed
        if spec.profile_id == "official-gemini" and not delayed:
            delayed = True
            # The old completion barrier expired at five seconds even though
            # every individual request was still inside its valid deadline.
            time.sleep(5.1)
        return _live_probe_runner(
            spec, scenario_id, attempt, nonce, prior_receipts
        )

    receipt = _launch(
        paths,
        _successful_runner([]),
        live_probe_runner=slow_probe,
    )

    assert delayed is True
    assert len(receipt["workers"]) == len(PROFILE_AGENT_TYPES)


def test_live_request_loader_retries_transient_atomic_publication(
    tmp_path, monkeypatch
):
    marker = tmp_path / ".live-probe-P0-02-1.request"
    expected = {"scenario_id": "P0-02"}
    calls = 0

    def load(_path, **_identity):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise live_request.LiveProbeRequestError(
                "live probe request marker is unsafe"
            )
        return expected

    monkeypatch.setattr(launcher, "load_request_marker", load)

    assert launcher._load_ready_live_request(
        marker,
        run_id="run-123",
        profile_id="official-deepseek",
        scenario_id="P0-02",
        attempt=1,
        previous_receipt_sha256=None,
        cot_terminal_sha256=None,
    ) == expected
    assert calls == 2


def test_parent_event_snapshot_ignores_only_trailing_partial_json(tmp_path):
    path = tmp_path.resolve() / "events.jsonl"
    row = _scenario_command_rows()[0]
    path.write_bytes(json.dumps(row).encode("utf-8") + b'\n{"type":')
    path.chmod(0o600)

    assert verifier.completed_scenario_command_sequence_snapshot(path) == (
        ("P0-02", 1),
    )

    with path.open("ab") as handle:
        handle.write(b"\n")
    with pytest.raises(verifier.OrchestrationError, match="event stream is invalid"):
        verifier.completed_scenario_command_sequence_snapshot(path)


def test_parent_rejects_forged_p0_13_cot_terminal_binding(
    tmp_path, monkeypatch
):
    marker = tmp_path.resolve() / ".live-probe-P0-13-1.request"
    live_request.write_request_marker(
        marker,
        run_id="run-123",
        profile_id="official-deepseek",
        scenario_id="P0-13",
        attempt=1,
        previous_receipt_sha256="1" * 64,
        cot_terminal_sha256="2" * 64,
    )
    monkeypatch.setattr(launcher, "_REQUEST_PUBLICATION_GRACE_SECONDS", 0.0)

    with pytest.raises(live_request.LiveProbeRequestError, match="marker is invalid"):
        launcher._load_ready_live_request(
            marker,
            run_id="run-123",
            profile_id="official-deepseek",
            scenario_id="P0-13",
            attempt=1,
            previous_receipt_sha256="1" * 64,
            cot_terminal_sha256="3" * 64,
        )


@pytest.mark.skipif(os.name != "posix", reason="qualification runner is POSIX")
def test_parent_gate_ignores_forged_persona_files_and_replaced_worker_lock(
    tmp_path, monkeypatch
):
    paths = _setup(tmp_path)
    spec = launcher._prepare_specs(
        codex_bin=paths["codex_bin"],
        codex_home=paths["codex_home"],
        source_root=paths["source"],
        artifact_root=paths["artifacts"],
        profile_manifest_dir=paths["manifests"],
        worker_root=paths["worker_root"],
        worker_output_root=paths["raw"],
        authoring_schema=launcher._load_authoring_schema(paths["schema"]),
        worker_python=paths["worker_python"],
        run_id="run-adversarial-parent-gate",
        base_url="https://test-api.feedling.app",
        expected_sha="a" * 40,
        assignments=(PROFILE_AGENT_TYPES[0],),
    )[0]
    monkeypatch.setattr(launcher, "_PARENT_COMMAND_ACK_GRACE_SECONDS", 0.05)
    invoked: list[str] = []
    adversary_was_alive = False

    def live_probe_runner(
        received_spec,
        scenario_id,
        attempt,
        nonce,
        prior_receipts,
    ):
        invoked.append(scenario_id)
        return _passing_live_probe(
            received_spec,
            scenario_id,
            attempt,
            nonce,
            prior_receipts,
        )

    def process_runner(received_spec: launcher.WorkerSpec, timeout: int) -> int:
        nonlocal adversary_was_alive
        assert received_spec is spec
        assert timeout == 600
        rows: list[dict[str, Any]] = []
        for row in _scenario_command_rows():
            command = row["item"]["command"]
            if "QA_SCENARIO_ID=P0-07" in command:
                break
            if (
                "QA_SCENARIO_ID=P0-06" not in command
                or "QA_SCENARIO_PHASE=FINALIZE" not in command
            ):
                rows.append(row)
        staged = spec.events_path.with_name(".adversarial-events.tmp")
        staged.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        staged.chmod(0o600)
        os.replace(staged, spec.events_path)

        previous_sha256: str | None = None
        for scenario_id in ("P0-02", "P0-03", "P0-04", "P0-05", "P0-06"):
            facts = live_request.request_and_wait(
                scenario_id=scenario_id,
                attempt=1,
                request=live_request.request_path(spec.work, scenario_id, 1),
                facts=live_request.facts_path(spec.work, scenario_id, 1),
                environment=spec.environment,
                wait_seconds=5.0,
                sequence_wait_seconds=5.0,
            )
            previous_sha256 = facts["receipt_sha256"]

        assert previous_sha256 is not None
        adversary_program = r'''
import fcntl
import json
import os
import sys

work, run_id, profile_id, previous_sha256 = sys.argv[1:]
gate = os.path.join(work, ".live-probe-sequence.lock")
try:
    os.unlink(gate)
except FileNotFoundError:
    pass
descriptor = os.open(gate, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
os.write(descriptor, b'{"forged_worker_state":true}\n')
os.fsync(descriptor)
fcntl.flock(descriptor, fcntl.LOCK_EX)
judgment = os.path.join(work, "p0-06-semantic-judgment.json")
with open(judgment, "x", encoding="utf-8") as handle:
    json.dump({"forged": True}, handle)
    handle.write("\n")
os.chmod(judgment, 0o600)
evidence = os.path.join(work, "p0-06-private-evidence.json")
try:
    os.unlink(evidence)
except FileNotFoundError:
    pass
marker = os.path.join(work, ".live-probe-P0-07-1.request")
payload = {
    "schema_version": 2,
    "run_id": run_id,
    "profile_id": profile_id,
    "scenario_id": "P0-07",
    "attempt": 1,
    "previous_receipt_sha256": previous_sha256,
    "cot_terminal_sha256": None,
}
marker_fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(marker_fd, (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
os.close(marker_fd)
print("ready", flush=True)
sys.stdin.buffer.read(1)
'''
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                adversary_program,
                str(spec.work),
                spec.environment["QA_RUN_ID"],
                spec.profile_id,
                previous_sha256,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline() == "ready\n"
            p0_07_facts = live_request.facts_path(spec.work, "P0-07", 1)
            deadline = time.monotonic() + 3.0
            while not p0_07_facts.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert p0_07_facts.exists()
            adversary_was_alive = child.poll() is None
            payload = json.loads(p0_07_facts.read_text(encoding="utf-8"))
            assert payload["failure_code"] == "LIVE_REQUEST_PROTOCOL_VIOLATION"
        finally:
            if child.stdin is not None:
                child.stdin.write("x")
                child.stdin.flush()
            child.wait(timeout=3.0)
            assert child.returncode == 0, (
                child.stderr.read() if child.stderr is not None else ""
            )
        return 0

    assert launcher._run_process_with_trusted_cot(
        spec,
        600,
        process_runner,
        _cot_probe_runner(),
        live_probe_runner,
        _persona_finalize_runner,
    ) == 0
    assert adversary_was_alive is True
    assert invoked == ["P0-02", "P0-03", "P0-04", "P0-05", "P0-06"]


def test_cot_request_loader_retries_transient_shell_publication(monkeypatch):
    spec = object()
    calls = 0

    def validate(received):
        nonlocal calls
        assert received is spec
        calls += 1
        if calls == 1:
            raise launcher.WorkerLaunchError("COT probe request marker is invalid")

    monkeypatch.setattr(launcher, "_validate_cot_request", validate)

    launcher._validate_ready_cot_request(spec)
    assert calls == 2


def test_transient_request_publication_invokes_each_trusted_probe_once(
    tmp_path, monkeypatch
):
    paths = _setup(tmp_path)
    original = launcher.load_request_marker
    first_reads: set[Path] = set()
    invocations: dict[tuple[str, str, int], int] = {}

    def transient_load(path, **identity):
        if path not in first_reads:
            first_reads.add(path)
            raise live_request.LiveProbeRequestError(
                "live probe request marker is unsafe"
            )
        return original(path, **identity)

    def counting_probe(spec, scenario_id, attempt, nonce, prior_receipts):
        key = (spec.profile_id, scenario_id, attempt)
        invocations[key] = invocations.get(key, 0) + 1
        return _live_probe_runner(
            spec, scenario_id, attempt, nonce, prior_receipts
        )

    monkeypatch.setattr(launcher, "load_request_marker", transient_load)
    receipt = _launch(
        paths,
        _successful_runner([]),
        live_probe_runner=counting_probe,
    )

    assert len(receipt["workers"]) == len(PROFILE_AGENT_TYPES)
    assert len(first_reads) == len(PROFILE_AGENT_TYPES) * len(
        live_request.LIVE_SCENARIO_IDS
    )
    assert len(invocations) == len(first_reads)
    assert set(invocations.values()) == {1}


def test_overlapping_live_requests_fail_closed_without_future_probe(tmp_path):
    paths = _setup(tmp_path, qualification_mode="diagnostic")
    probe_started = threading.Event()
    release_probe = threading.Event()
    captured: list[launcher.WorkerSpec] = []
    invocations: list[tuple[str, int]] = []

    def blocking_probe(spec, scenario_id, attempt, nonce, prior_receipts):
        invocations.append((scenario_id, attempt))
        assert scenario_id == "P0-02"
        probe_started.set()
        assert release_probe.wait(timeout=5)
        return _live_probe_runner(
            spec, scenario_id, attempt, nonce, prior_receipts
        )

    def overlapping_runner(spec: launcher.WorkerSpec, _timeout: int) -> int:
        captured.append(spec)
        live_request.write_request_marker(
            live_request.request_path(spec.work, "P0-02", 1),
            run_id=spec.environment["QA_RUN_ID"],
            profile_id=spec.profile_id,
            scenario_id="P0-02",
            attempt=1,
            previous_receipt_sha256=None,
        )
        assert probe_started.wait(timeout=5)
        live_request.write_request_marker(
            live_request.request_path(spec.work, "P0-03", 1),
            run_id=spec.environment["QA_RUN_ID"],
            profile_id=spec.profile_id,
            scenario_id="P0-03",
            attempt=1,
            previous_receipt_sha256="0" * 64,
        )
        release_probe.set()
        facts = (
            live_request.facts_path(spec.work, "P0-02", 1),
            live_request.facts_path(spec.work, "P0-03", 1),
        )
        deadline = time.monotonic() + 5
        while not all(path.exists() for path in facts):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        return 0

    receipt = _launch_diagnostic(
        paths,
        overlapping_runner,
        live_probe_runner=blocking_probe,
    )

    assert invocations == [("P0-02", 1)]
    assert len(captured) == 1
    spec = captured[0]
    for scenario_id in ("P0-02", "P0-03"):
        facts = json.loads(
            live_request.facts_path(spec.work, scenario_id, 1).read_text()
        )
        assert facts == {
            "schema_version": 1,
            "profile_id": spec.profile_id,
            "scenario_id": scenario_id,
            "attempt": 1,
            "receipt_sha256": None,
            "receipt": None,
            "private_facts": None,
            "status": "UNAVAILABLE",
            "failure_code": "LIVE_REQUEST_PROTOCOL_VIOLATION",
        }
    aggregate = json.loads(spec.live_receipt_path.read_text())
    assert aggregate["receipts"] == []
    assert receipt["workers"][0]["result_source"] == "deterministic_fallback"


@pytest.mark.parametrize("scenario_id", ("P0-08", "P0-09", "P0-10", "P0-11"))
@pytest.mark.parametrize("failure_code", ("CHAT_TIMEOUT", "MISSING_REPLY"))
def test_retry_admission_requires_locked_transient_failure_code(
    scenario_id, failure_code
):
    receipts = [
        {
            "scenario_id": scenario_id,
            "attempt": 1,
            "status": "AGENT_ERROR",
            "failure_code": failure_code,
        }
    ]

    assert launcher._live_request_is_next(receipts, scenario_id, 2) is True


@pytest.mark.parametrize(
    "failure_code", ("LIVE_PROBE_ERROR", "ASSERTION_FAILED", None)
)
def test_retry_admission_rejects_nontransient_agent_error(failure_code):
    receipts = [
        {
            "scenario_id": "P0-08",
            "attempt": 1,
            "status": "AGENT_ERROR",
            "failure_code": failure_code,
        }
    ]

    assert launcher._live_request_is_next(receipts, "P0-08", 2) is False


def test_transient_cot_publication_invokes_each_trusted_probe_once(
    tmp_path, monkeypatch
):
    paths = _setup(tmp_path)
    original_validate = launcher._validate_cot_request
    passing_probe = _cot_probe_runner()
    first_reads: set[str] = set()
    invocations: dict[str, int] = {}

    def transient_validate(spec):
        if spec.profile_id not in first_reads:
            first_reads.add(spec.profile_id)
            raise launcher.WorkerLaunchError("COT probe request marker is invalid")
        original_validate(spec)

    def counting_probe(spec):
        invocations[spec.profile_id] = invocations.get(spec.profile_id, 0) + 1
        return passing_probe(spec)

    monkeypatch.setattr(launcher, "_validate_cot_request", transient_validate)
    receipt = _launch(
        paths,
        _successful_runner([]),
        cot_probe_runner=counting_probe,
    )

    expected_profiles = {profile_id for profile_id, _ in PROFILE_AGENT_TYPES}
    assert len(receipt["workers"]) == len(PROFILE_AGENT_TYPES)
    assert first_reads == expected_profiles
    assert set(invocations) == expected_profiles
    assert set(invocations.values()) == {1}


@pytest.mark.parametrize(
    ("name", "replacement"),
    (
        ("QA_QUALIFICATION_MODE", "release"),
        ("QA_PYTHON_BIN", "/untrusted/python"),
        ("QA_PROFILE_ID", "another-profile"),
    ),
)
def test_launcher_rejects_profile_shell_binding_tampering(
    tmp_path, name, replacement
):
    paths = _setup(tmp_path, qualification_mode="diagnostic")
    profile_path = paths["codex_home"] / "profile_official_gemini.config.toml"
    original = {
        "QA_QUALIFICATION_MODE": "diagnostic",
        "QA_PYTHON_BIN": str(paths["worker_python"]),
        "QA_PROFILE_ID": "official-gemini",
    }[name]
    content = profile_path.read_text()
    old_line = f"{name} = {json.dumps(original)}"
    new_line = f"{name} = {json.dumps(replacement)}"
    assert old_line in content
    profile_path.write_text(content.replace(old_line, new_line, 1))

    invoked = False

    def runner(_spec: launcher.WorkerSpec, _timeout: int) -> int:
        nonlocal invoked
        invoked = True
        return 0

    with pytest.raises(launcher.WorkerLaunchError, match="profile binding"):
        _launch_diagnostic(paths, runner)
    assert invoked is False


def test_nonzero_exit_attempts_all_eight_once_and_writes_no_receipt(tmp_path):
    paths = _setup(tmp_path)
    captured: list[launcher.WorkerSpec] = []
    successful = _successful_runner(captured)

    def runner(spec: launcher.WorkerSpec, timeout: int) -> int:
        code = successful(spec, timeout)
        return 1 if spec.profile_id == PROFILE_AGENT_TYPES[0][0] else code

    with pytest.raises(launcher.WorkerLaunchError, match="workers failed"):
        _launch(paths, runner)
    assert len(captured) == len(PROFILE_AGENT_TYPES)
    assert len({spec.profile_id for spec in captured}) == len(PROFILE_AGENT_TYPES)
    assert not paths["receipt"].exists()
    assert list(paths["aggregation"].iterdir()) == []


def test_release_fails_closed_when_schema_valid_workers_use_no_tools(tmp_path):
    paths = _setup(tmp_path)
    captured: list[launcher.WorkerSpec] = []

    with pytest.raises(launcher.WorkerToolUseError, match="qualification tools"):
        _launch(paths, _successful_runner(captured, omit_command=True))

    assert len(captured) == len(PROFILE_AGENT_TYPES)
    assert not paths["receipt"].exists()


def test_release_rejects_one_command_plus_agent_authored_matrix(tmp_path):
    paths = _setup(tmp_path)

    with pytest.raises(
        launcher.WorkerScenarioToolUseError,
        match="live-scenario command evidence",
    ):
        _launch(paths, _successful_runner([], omit_scenario_commands=True))

    assert not paths["receipt"].exists()


def test_release_rejects_markers_without_exact_first_sop_read(tmp_path):
    paths = _setup(tmp_path)

    with pytest.raises(
        launcher.WorkerScenarioToolUseError,
        match="live-scenario command evidence",
    ):
        _launch(paths, _successful_runner([], wrong_sop_read=True))

    assert not paths["receipt"].exists()


def test_release_rejects_persona_judgment_without_three_tool_phases(tmp_path):
    paths = _setup(tmp_path)

    with pytest.raises(
        launcher.WorkerScenarioToolUseError,
        match="live-scenario command evidence",
    ):
        _launch(paths, _successful_runner([], omit_persona_review_commands=True))

    assert not paths["receipt"].exists()


def test_release_rejects_three_generic_persona_markers(tmp_path):
    paths = _setup(tmp_path)

    with pytest.raises(
        launcher.WorkerScenarioToolUseError,
        match="live-scenario command evidence",
    ):
        _launch(paths, _successful_runner([], generic_persona_markers=True))

    assert not paths["receipt"].exists()


def test_release_rejects_valid_persona_phases_plus_extra_marker(tmp_path):
    paths = _setup(tmp_path)

    with pytest.raises(
        launcher.WorkerScenarioToolUseError,
        match="live-scenario command evidence",
    ):
        _launch(paths, _successful_runner([], extra_persona_marker=True))

    assert not paths["receipt"].exists()


def test_release_rejects_valid_persona_phases_plus_unsafe_extra_marker(tmp_path):
    paths = _setup(tmp_path)

    with pytest.raises(
        launcher.WorkerScenarioToolUseError,
        match="live-scenario command evidence",
    ):
        _launch(
            paths,
            _successful_runner([], unsafe_extra_persona_marker=True),
        )

    assert not paths["receipt"].exists()


def test_release_rejects_nonzero_scenario_probe(tmp_path):
    paths = _setup(tmp_path)

    with pytest.raises(
        launcher.WorkerScenarioToolUseError,
        match="live-scenario command evidence",
    ):
        _launch(
            paths,
            _successful_runner([], failed_scenario_command="P0-02"),
        )

    assert not paths["receipt"].exists()


def test_release_rejects_parent_persona_finalizer_not_bound_to_capture(
    tmp_path,
):
    paths = _setup(tmp_path)

    def tampered_finalizer(spec, capture_receipt):
        projection = json.loads(
            json.dumps(_persona_finalize_runner(spec, capture_receipt))
        )
        projection["persona_finalizer"]["evidence_sha256"] = "0" * 64
        return projection

    with pytest.raises(
        launcher.WorkerLaunchError,
        match="live scenario receipt does not match worker result",
    ):
        _launch(
            paths,
            _successful_runner([]),
            persona_finalize_runner=tampered_finalizer,
        )

    assert not paths["receipt"].exists()


@pytest.mark.parametrize(
    ("cot_mode", "message"),
    (
        ("missing", "COT receipt is missing"),
        ("malformed", "COT receipt is invalid"),
        ("binding-mismatch", "COT receipt is invalid"),
    ),
)
def test_release_fails_closed_on_untrusted_cot_evidence(
    tmp_path, cot_mode, message
):
    paths = _setup(tmp_path)

    with pytest.raises(launcher.WorkerLaunchError, match=message):
        _launch(
            paths,
            _successful_runner([], cot_mode=cot_mode),
            cot_probe_runner=_cot_probe_runner(
                cot_mode if cot_mode in {"missing", "malformed"} else "valid"
            ),
        )

    assert not paths["receipt"].exists()


def test_release_preserves_validated_failed_cot_lifecycle_for_final_gate(tmp_path):
    paths = _setup(tmp_path)

    receipt = _launch(
        paths,
        _successful_runner([], cot_mode="failed"),
        cot_probe_runner=_cot_probe_runner("failed"),
    )

    assert paths["receipt"].exists()
    assert {row["cot_delivery_status"] for row in receipt["workers"]} == {"FAIL"}
    assert {row["cot_failure_code"] for row in receipt["workers"]} == {
        "FINAL_ANSWER_WRONG"
    }
    assert verifier.verify(
        paths["receipt"], paths["raw"], paths["aggregation"]
    ) == receipt


def test_release_rejects_agent_pass_status_for_trusted_failed_cot(tmp_path):
    paths = _setup(tmp_path)

    with pytest.raises(launcher.WorkerLaunchError, match="COT receipt is invalid"):
        _launch(
            paths,
            _successful_runner([], cot_mode="failed-status-mismatch"),
            cot_probe_runner=_cot_probe_runner("failed"),
        )

    assert not paths["receipt"].exists()


def test_diagnostic_no_tool_verdict_uses_specific_fallback(tmp_path):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def runner(spec: launcher.WorkerSpec, _timeout: int) -> int:
        schema = json.loads(spec.schema_path.read_text())
        result = _instance(schema, schema["$defs"])
        spec.result_path.write_text(json.dumps(result) + "\n")
        spec.events_path.write_text(
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "30000000-0000-4000-8000-000000000009",
                    "session_id": "40000000-0000-4000-8000-000000000009",
                }
            )
            + "\n"
            + json.dumps({"type": "turn.started"})
            + "\n"
            + json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "command_execution"},
                }
            )
            + "\n"
            + json.dumps({"type": "turn.completed", "usage": {}})
            + "\n"
        )
        _request_passing_cot_probe(spec)
        return 0

    receipt = _launch_diagnostic(paths, runner)
    worker = receipt["workers"][0]

    assert worker["result_source"] == "deterministic_fallback"
    assert worker["fallback_reason"] == "AGENT_TOOL_USE_MISSING"
    assert worker["failure_stage"] == "SCENARIO_COMMAND_EVIDENCE"
    assert worker["failure_code"] == "AGENT_TOOL_USE_MISSING"
    assert worker["completed_command_execution_count"] == 0
    assert worker["thread_id"] is None


def test_diagnostic_short_circuited_live_matrix_becomes_invalid_worker(tmp_path):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def runner(spec: launcher.WorkerSpec, _timeout: int) -> int:
        schema = json.loads(spec.schema_path.read_text())
        result = _instance(schema, schema["$defs"])
        spec.result_path.write_text(json.dumps(result) + "\n")
        _request_passing_cot_probe(spec)
        result = json.loads(spec.result_path.read_text())
        scenario = result["scenarios"][1]
        scenario["status"] = "BLOCKED_EVIDENCE"
        scenario["failure"] = {
            "stage": "PRECONDITION",
            "failure_code": "PRECONDITION_MISSING",
            "retryable": False,
            "sanitized_message": "not attempted",
        }
        scenario["attempt_results"][0].update(
            status="BLOCKED_EVIDENCE",
            failure=scenario["failure"],
        )
        spec.result_path.write_text(json.dumps(result) + "\n")
        spec.events_path.write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in (
                    {
                        "type": "thread.started",
                        "thread_id": "30000000-0000-4000-8000-000000000010",
                    },
                    {"type": "turn.started"},
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "status": "completed",
                        },
                    },
                    {"type": "turn.completed", "usage": {}},
                )
            )
        )
        return 0

    receipt = _launch_diagnostic(paths, runner)

    assert receipt["workers"][0]["result_source"] == "deterministic_fallback"
    assert receipt["workers"][0]["fallback_reason"] == "WORKER_RESULT_INVALID"
    assert receipt["workers"][0]["failure_stage"] == "STRUCTURED_RESULT"
    assert receipt["workers"][0]["failure_code"] == "STRUCTURED_RESULT_INVALID"


def test_diagnostic_nonzero_preserves_sanitized_agent_error_row(tmp_path):
    paths = _setup(tmp_path, qualification_mode="diagnostic")
    manifest_path = paths["manifests"] / "official-gemini.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["profiles"][0].update(
        {
            "configured_model": "gemini-2.5-flash",
            "user_id": "synthetic-user-123",
            "runtime_mode": "hosted_resident",
            "trace_enabled": True,
            "api_key": "feedling-secret-must-not-escape",
            "secret_key_b64": "content-secret-must-not-escape",
        }
    )
    manifest_path.write_text(json.dumps(manifest) + "\n")

    receipt = _launch_diagnostic(paths, lambda _spec, _timeout: 124)
    canonical = paths["aggregation"] / "official-gemini.json"
    result = json.loads(canonical.read_text())

    assert receipt["qualification_mode"] == "diagnostic"
    assert receipt["release_qualified"] is False
    assert receipt["requested_profile_ids"] == ["official-gemini"]
    assert len(receipt["workers"]) == 1
    worker = receipt["workers"][0]
    assert worker["process_exit_code"] == 124
    assert worker["worker_id"] is None
    assert worker["thread_id"] is None
    assert worker["session_id"] is None
    assert worker["exec_events_sha256"] is None
    assert worker["result_source"] == "deterministic_fallback"
    assert worker["fallback_reason"] == "PROCESS_EXIT_NONZERO"
    assert worker["failure_stage"] == "PROCESS_EXIT"
    assert worker["failure_code"] == "PROCESS_EXIT_NONZERO"
    assert result["status"] == "AGENT_ERROR"
    assert result["diagnostic_codes"] == [
        "AGENT_EXECUTION_ERROR",
        "TRACE_PARTIAL",
        "STAGE_TIMING_UNAVAILABLE",
    ]
    assert result["user_id"] == "synthetic-user-123"
    assert result["observed_runtime"] == "hosted_resident"
    assert result["trace"]["enabled"] is True
    serialized = canonical.read_text() + paths["receipt"].read_text()
    assert "feedling-secret-must-not-escape" not in serialized
    assert "content-secret-must-not-escape" not in serialized


@pytest.mark.parametrize(
    ("failure_mode", "expected_stage", "expected_code"),
    (
        ("malformed", "STRUCTURED_RESULT", "STRUCTURED_RESULT_INVALID"),
        ("missing", "OUTPUT_FILE_SET", "OUTPUT_FILE_SET_INVALID"),
    ),
)
def test_diagnostic_malformed_or_missing_result_becomes_fallback(
    tmp_path, failure_mode, expected_stage, expected_code
):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def runner(spec: launcher.WorkerSpec, _timeout: int) -> int:
        if failure_mode == "malformed":
            spec.result_path.write_text("{}\n")
        else:
            spec.result_path.unlink()
        spec.events_path.write_text(
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "30000000-0000-4000-8000-000000000001",
                }
            )
            + "\n"
            + json.dumps({"type": "turn.started"})
            + "\n"
            + json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "command_execution"},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "status": "completed",
                    },
                }
            )
            + "\n"
            + json.dumps(
                next(
                    row
                    for row in _scenario_command_rows()
                    if "QA_SCENARIO_ID=P0-11" in row["item"]["command"]
                )
            )
            + "\n"
            + json.dumps({"type": "turn.completed", "usage": {}})
            + "\n"
        )
        _request_passing_cot_probe(spec)
        return 0

    receipt = _launch_diagnostic(paths, runner)
    worker = receipt["workers"][0]
    result = json.loads(
        (paths["aggregation"] / "official-gemini.json").read_text()
    )

    assert worker["result_source"] == "deterministic_fallback"
    assert worker["fallback_reason"] == "WORKER_RESULT_INVALID"
    assert worker["failure_stage"] == expected_stage
    assert worker["failure_code"] == expected_code
    assert worker["thread_id"] is None
    assert len(worker["cot_receipt_sha256"]) == 64
    assert worker["cot_delivery_status"] == "PASS"
    assert worker["cot_failure_code"] == "NONE"
    assert result["status"] == "AGENT_ERROR"


def test_diagnostic_keeps_valid_worker_when_peer_uses_fallback(tmp_path):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def runner(spec: launcher.WorkerSpec, _timeout: int) -> int:
        if spec.profile_id == "openrouter-glm":
            return 1
        schema = json.loads(spec.schema_path.read_text())
        result = _instance(schema, schema["$defs"])
        spec.result_path.write_text(json.dumps(result) + "\n")
        rows = [
            {
                "type": "thread.started",
                "thread_id": "30000000-0000-4000-8000-000000000002",
                "session_id": "40000000-0000-4000-8000-000000000002",
            },
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": verifier.MANDATORY_SOP_READ_COMMAND,
                    "status": "completed",
                    "exit_code": 0,
                },
            },
            *_scenario_command_rows(),
            {"type": "turn.completed", "usage": {}},
        ]
        spec.events_path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )
        _request_passing_cot_probe(spec)
        _request_passing_live_probes(spec)
        return 0

    receipt = _launch_diagnostic(
        paths, runner, profile_ids=("official-gemini", "openrouter-glm")
    )
    workers = {row["profile_id"]: row for row in receipt["workers"]}
    results = {
        path.stem: json.loads(path.read_text())
        for path in paths["aggregation"].iterdir()
    }

    assert set(results) == {"official-gemini", "openrouter-glm"}
    assert workers["official-gemini"]["result_source"] == "codex_worker"
    assert workers["official-gemini"]["fallback_reason"] is None
    assert workers["official-gemini"]["failure_stage"] is None
    assert workers["official-gemini"]["failure_code"] is None
    assert workers["official-gemini"]["cot_evidence_failure"] is None
    assert workers["official-gemini"]["completed_command_execution_count"] == 15
    assert workers["official-gemini"]["completed_scenario_command_ids"] == list(
        verifier.AGENT_LIVE_SCENARIO_IDS
    )
    assert workers["official-gemini"]["completed_scenario_command_counts"] == (
        verifier.MIN_SCENARIO_COMMAND_COUNTS
    )
    assert workers["official-gemini"]["cot_delivery_status"] == "PASS"
    assert workers["official-gemini"]["cot_failure_code"] == "NONE"
    assert len(workers["official-gemini"]["cot_receipt_sha256"]) == 64
    assert workers["openrouter-glm"]["result_source"] == "deterministic_fallback"
    assert workers["openrouter-glm"]["failure_stage"] == "PROCESS_EXIT"
    assert workers["openrouter-glm"]["failure_code"] == "PROCESS_EXIT_NONZERO"
    assert results["official-gemini"]["status"] == "BLOCKED_EVIDENCE"
    assert results["openrouter-glm"]["status"] == "AGENT_ERROR"


@pytest.mark.parametrize(
    ("cot_mode", "expected_reason"),
    (
        ("missing", "COT_RECEIPT_MISSING"),
        ("malformed", "COT_RECEIPT_INVALID"),
    ),
)
def test_diagnostic_preserves_valid_result_when_cot_evidence_fails(
    tmp_path, cot_mode, expected_reason
):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def runner(spec: launcher.WorkerSpec, _timeout: int) -> int:
        schema = json.loads(spec.schema_path.read_text())
        result = _instance(schema, schema["$defs"])
        spec.result_path.write_text(json.dumps(result) + "\n")
        rows = [
            {
                "type": "thread.started",
                "thread_id": "30000000-0000-4000-8000-000000000003",
                "session_id": "40000000-0000-4000-8000-000000000003",
            },
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": verifier.MANDATORY_SOP_READ_COMMAND,
                    "status": "completed",
                    "exit_code": 0,
                },
            },
            *_scenario_command_rows(),
            {"type": "turn.completed", "usage": {}},
        ]
        spec.events_path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )
        _request_passing_cot_probe(spec)
        _request_passing_live_probes(spec)
        return 0

    receipt = _launch_diagnostic(
        paths,
        runner,
        cot_probe_runner=_cot_probe_runner(
            cot_mode
        ),
    )
    worker = receipt["workers"][0]
    result = json.loads(
        (paths["aggregation"] / "official-gemini.json").read_text()
    )

    assert worker["result_source"] == "codex_worker"
    assert worker["fallback_reason"] is None
    assert worker["cot_evidence_failure"] == expected_reason
    assert worker["failure_stage"] == "COT_RECEIPT_LOAD"
    assert worker["failure_code"] == expected_reason
    assert worker["thread_id"] is not None
    assert worker["session_id"] is not None
    assert len(worker["exec_events_sha256"]) == 64
    assert worker["completed_command_execution_count"] == 15
    assert worker["completed_scenario_command_ids"] == list(
        verifier.AGENT_LIVE_SCENARIO_IDS
    )
    assert worker["completed_scenario_command_counts"] == (
        verifier.MIN_SCENARIO_COMMAND_COUNTS
    )
    assert result["profile_id"] == "official-gemini"
    assert result["status"] == "BLOCKED_EVIDENCE"
    assert len(result["turns"]) == 14
    assert all(turn["scenario_id"] != "P0-12" for turn in result["turns"])
    assert result["latency"]["sample_count"] == 14
    assert worker["cot_receipt_sha256"] is None
    assert worker["cot_delivery_status"] is None


@pytest.mark.parametrize(
    ("cot_mode", "expected_turn_count", "expected_cot_turn_count"),
    (
        ("request-failed", 14, 0),
        ("request-failed-after-ack", 15, 1),
    ),
)
def test_diagnostic_projects_negative_cot_before_holistic_attempt_validation(
    tmp_path, cot_mode, expected_turn_count, expected_cot_turn_count
):
    paths = _setup(tmp_path, qualification_mode="diagnostic")
    successful = _successful_runner(
        [], cot_mode=cot_mode, synchronize=False
    )

    def runner(spec: launcher.WorkerSpec, timeout: int) -> int:
        code = successful(spec, timeout)
        result = json.loads(spec.result_path.read_text())
        scenario = next(
            row for row in result["scenarios"] if row["scenario_id"] == "P0-12"
        )
        stale_failure = {
            "category": "BLOCKED_EVIDENCE",
            "stage_code": "REASONING",
            "failure_code": "PRECONDITION_MISSING",
            "reproducible": True,
        }
        scenario.update(
            {
                "status": "BLOCKED_EVIDENCE",
                "attempt_results": [
                    {
                        "attempt": 1,
                        "status": "BLOCKED_EVIDENCE",
                        "failure": dict(stale_failure),
                    }
                ],
                "failure": stale_failure,
            }
        )
        spec.result_path.write_text(json.dumps(result) + "\n")
        return code

    receipt = _launch_diagnostic(
        paths,
        runner,
        cot_probe_runner=_cot_probe_runner(cot_mode),
    )
    worker = receipt["workers"][0]
    result = json.loads(
        (paths["aggregation"] / "official-gemini.json").read_text()
    )
    assert worker["result_source"] == "codex_worker", worker
    assert worker["fallback_reason"] is None
    scenario = next(
        row for row in result["scenarios"] if row["scenario_id"] == "P0-12"
    )
    cot_turns = [
        turn for turn in result["turns"] if turn["scenario_id"] == "P0-12"
    ]

    assert worker["cot_evidence_failure"] is None
    assert worker["cot_delivery_status"] == "UNVERIFIED"
    assert worker["cot_failure_code"] == "CHAT_REQUEST_FAILED"
    assert scenario["status"] == "BLOCKED_EVIDENCE"
    assert scenario["failure"] == {
        "category": "BLOCKED_EVIDENCE",
        "stage_code": "REASONING",
        "failure_code": "TRACE_UNAVAILABLE",
        "reproducible": True,
    }
    assert scenario["attempt_results"][0]["failure"] == scenario["failure"]
    assert len(result["turns"]) == expected_turn_count
    assert len(cot_turns) == expected_cot_turn_count
    assert result["latency"]["sample_count"] == expected_turn_count
    if cot_turns:
        assert cot_turns[0]["request_id"] == "request-1"
        assert cot_turns[0]["duplicate_detected"] is True
        assert cot_turns[0]["out_of_order_detected"] is True


@pytest.mark.parametrize(
    ("cot_mode", "tamper_mode"),
    (
        ("request-failed", "unexpected-extra"),
        ("request-failed-after-ack", "missing-required"),
    ),
)
def test_diagnostic_rejects_p0_13_cot_trace_cardinality_tampering(
    tmp_path, cot_mode, tamper_mode
):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def tampered_probe(spec, scenario_id, attempt, nonce, prior_receipts):
        receipt, facts = _passing_live_probe(
            spec, scenario_id, attempt, nonce, prior_receipts
        )
        if scenario_id != "P0-13":
            return receipt, facts
        turns = receipt["turns"]
        if tamper_mode == "unexpected-extra":
            forged = json.loads(json.dumps(turns[-1]))
            forged.update(
                {
                    "turn_index": len(turns) + 1,
                    "request_id": "forged-cot-request",
                    "turn_id": "forged-cot-turn",
                    "trace_id": "forged-cot-trace",
                }
            )
            turns.append(forged)
        else:
            turns.pop()
        receipt["turn_ids"] = [turn["turn_id"] for turn in turns]
        receipt["trace_ids"] = [turn["trace_id"] for turn in turns]
        projection = receipt["result_projection"]
        projection["latency"] = live_receipts.latency_projection(turns)
        projection["trace"]["correlated_event_count"] = len(turns) * 5
        receipt["assertions"].update(
            {
                "trace_stages_complete": True,
                "trace_correlation_confirmed": (
                    len(turns) == 15
                    and len({turn["trace_id"] for turn in turns}) == 15
                ),
                "latency_attributed": True,
                "cleanup_confirmed": False,
            }
        )
        return receipt, facts

    receipt = _launch_diagnostic(
        paths,
        _successful_runner([], cot_mode=cot_mode, synchronize=False),
        cot_probe_runner=_cot_probe_runner(cot_mode),
        live_probe_runner=tampered_probe,
    )
    worker = receipt["workers"][0]

    assert worker["result_source"] == "deterministic_fallback"
    assert worker["fallback_reason"] == "WORKER_RESULT_INVALID"
    assert worker["failure_stage"] == "LIVE_RECEIPT_PROJECTION"
    assert worker["failure_code"] == "LIVE_RECEIPT_PROJECTION_INVALID"


def test_diagnostic_canonicalizes_parent_owned_live_and_cot_evidence(tmp_path):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def runner(spec: launcher.WorkerSpec, _timeout: int) -> int:
        schema = json.loads(spec.schema_path.read_text())
        result = _instance(schema, schema["$defs"])
        spec.result_path.write_text(json.dumps(result) + "\n")
        spec.events_path.write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in (
                    {
                        "type": "thread.started",
                        "thread_id": "30000000-0000-4000-8000-000000000004",
                        "session_id": "40000000-0000-4000-8000-000000000004",
                    },
                    {"type": "turn.started"},
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": verifier.MANDATORY_SOP_READ_COMMAND,
                            "status": "completed",
                            "exit_code": 0,
                        },
                    },
                    *_scenario_command_rows(),
                    {"type": "turn.completed", "usage": {}},
                )
            )
        )
        _request_passing_cot_probe(spec)
        _request_passing_live_probes(spec)

        # Reproduce the canary's harmless model transcription/staleness errors.
        result = json.loads(spec.result_path.read_text())
        scenarios = {row["scenario_id"]: row for row in result["scenarios"]}
        scenarios["P0-02"]["request_ids"] = ["probe-p0-02-typo"]
        scenarios["P0-06"]["persona_finalizer"]["finalizer_ok"] = False
        result["reasoning"].update(
            {
                "disclosure_length": 999,
                "source": "agent-transcription",
                "model": None,
            }
        )
        for turn in result["turns"]:
            turn["stage_latency_ms"] = {
                "routing": None,
                "queue": None,
                "provider": None,
                "persistence": None,
                "delivery": None,
            }
        spec.result_path.write_text(json.dumps(result) + "\n")
        return 0

    receipt = _launch_diagnostic(paths, runner)
    worker = receipt["workers"][0]
    result = json.loads(
        (paths["aggregation"] / "official-gemini.json").read_text()
    )
    scenarios = {row["scenario_id"]: row for row in result["scenarios"]}

    assert worker["result_source"] == "codex_worker"
    assert worker["fallback_reason"] is None
    assert worker["cot_evidence_failure"] is None
    assert scenarios["P0-02"]["request_ids"] == ["probe-p0-02-1"]
    assert scenarios["P0-06"]["persona_finalizer"]["finalizer_ok"] is True
    assert all(
        all(value is not None for value in turn["stage_latency_ms"].values())
        for turn in result["turns"]
    )
    assert result["reasoning"]["disclosure_length"] == 24
    assert result["reasoning"]["source"] == "pi_thinking"
    assert result["reasoning"]["model"] == "model-safe"
    assert next(
        turn for turn in result["turns"] if turn["scenario_id"] == "P0-12"
    )["turn_index"] == 1


def test_diagnostic_preserves_parent_owned_negative_persona_verdict(tmp_path):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def runner(spec: launcher.WorkerSpec, _timeout: int) -> int:
        schema = json.loads(spec.schema_path.read_text())
        spec.result_path.write_text(
            json.dumps(_instance(schema, schema["$defs"])) + "\n"
        )
        spec.events_path.write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in (
                    {
                        "type": "thread.started",
                        "thread_id": "30000000-0000-4000-8000-000000000005",
                        "session_id": "40000000-0000-4000-8000-000000000005",
                    },
                    {"type": "turn.started"},
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": verifier.MANDATORY_SOP_READ_COMMAND,
                            "status": "completed",
                            "exit_code": 0,
                        },
                    },
                    *_scenario_command_rows(),
                    {"type": "turn.completed", "usage": {}},
                )
            )
        )
        _request_passing_cot_probe(spec)
        _request_passing_live_probes(spec)
        return 0

    def negative_persona(spec, capture_receipt):
        projection = _persona_finalize_runner(spec, capture_receipt)
        projection["semantic_assertions"]["persona_acceptance_passed"] = False
        projection["persona_finalizer"]["finalizer_ok"] = False
        return projection

    receipt = _launch_diagnostic(
        paths,
        runner,
        persona_finalize_runner=negative_persona,
    )
    worker = receipt["workers"][0]
    result = json.loads(
        (paths["aggregation"] / "official-gemini.json").read_text()
    )
    persona = result["scenarios"][5]

    assert worker["result_source"] == "codex_worker"
    assert worker["fallback_reason"] is None
    assert persona["status"] == "PRODUCT_FAIL"
    assert persona["assertions"]["persona_acceptance_passed"] is False
    assert persona["assertions"]["privacy_canary_absent"] is True
    assert persona["persona_finalizer"]["finalizer_ok"] is False
    assert persona["failure"]["failure_code"] == "PERSONA_ACCEPTANCE_FAILED"


def test_diagnostic_retains_matrix_when_parent_persona_review_fails(tmp_path):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def failed_persona_review(_spec, _capture_receipt):
        raise launcher.PersonaFinalizeError("SEMANTIC_JUDGMENT_INVALID")

    receipt = _launch_diagnostic(
        paths,
        _successful_runner([]),
        persona_finalize_runner=failed_persona_review,
        profile_ids=tuple(profile_id for profile_id, _ in PROFILE_AGENT_TYPES),
    )
    worker = next(
        row
        for row in receipt["workers"]
        if row["profile_id"] == "official-gemini"
    )
    result = json.loads(
        (paths["aggregation"] / "official-gemini.json").read_text()
    )
    scenarios = {row["scenario_id"]: row for row in result["scenarios"]}
    live_set, _ = live_receipts.validate_live_scenario_receipts(
        paths["raw"] / "official-gemini" / "live-scenario-receipts.json",
        run_id="diagnostic-123",
        profile_id="official-gemini",
        allow_failed_persona=True,
    )

    assert worker["result_source"] == "codex_worker"
    assert worker["fallback_reason"] is None
    assert live_set["persona_finalizer"] == {
        "kind": "persona_finalizer_failure",
        "failure_code": "SEMANTIC_JUDGMENT_INVALID",
    }
    assert scenarios["P0-06"]["status"] == "AGENT_ERROR"
    assert scenarios["P0-06"]["failure"]["failure_code"] == "MALFORMED_EVIDENCE"
    assert scenarios["P0-06"]["persona_finalizer"] is None
    assert scenarios["P0-07"]["status"] == "PASS"
    assert scenarios["P0-13"]["scenario_id"] == "P0-13"


def test_diagnostic_persona_capture_failure_continues_later_scenarios(tmp_path):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def runner(spec: launcher.WorkerSpec, _timeout: int) -> int:
        schema = json.loads(spec.schema_path.read_text())
        spec.result_path.write_text(
            json.dumps(_instance(schema, schema["$defs"])) + "\n"
        )
        commands = [
            row
            for row in _scenario_command_rows()
            if not (
                "QA_SCENARIO_ID=P0-06" in row["item"]["command"]
                and any(
                    phase in row["item"]["command"]
                    for phase in (
                        "QA_SCENARIO_PHASE=REVIEW",
                        "QA_SCENARIO_PHASE=FINALIZE",
                    )
                )
            )
        ]
        spec.events_path.write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in (
                    {
                        "type": "thread.started",
                        "thread_id": "30000000-0000-4000-8000-000000000006",
                        "session_id": "40000000-0000-4000-8000-000000000006",
                    },
                    {"type": "turn.started"},
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": verifier.MANDATORY_SOP_READ_COMMAND,
                            "status": "completed",
                            "exit_code": 0,
                        },
                    },
                    *commands,
                    {"type": "turn.completed", "usage": {}},
                )
            )
        )
        _request_passing_cot_probe(spec)
        _request_passing_live_probes(spec)
        return 0

    def failed_persona_probe(spec, scenario_id, attempt, nonce, prior_receipts):
        receipt, facts = _passing_live_probe(
            spec, scenario_id, attempt, nonce, prior_receipts
        )
        if scenario_id == "P0-06":
            receipt.update(
                {
                    "status": "BLOCKED_EVIDENCE",
                    "failure_code": "LIVE_PROBE_ERROR",
                    "result_projection": None,
                }
            )
            receipt["assertions"] = {
                key: False
                for key in live_receipts.DETERMINISTIC_ASSERTIONS["P0-06"]
            }
        return receipt, facts

    def unexpected_finalizer(_spec, _capture):
        raise AssertionError("failed persona capture must not be finalized")

    receipt = _launch_diagnostic(
        paths,
        runner,
        live_probe_runner=failed_persona_probe,
        persona_finalize_runner=unexpected_finalizer,
    )
    worker = receipt["workers"][0]
    result = json.loads(
        (paths["aggregation"] / "official-gemini.json").read_text()
    )
    live_set, _ = live_receipts.validate_live_scenario_receipts(
        paths["raw"] / "official-gemini" / "live-scenario-receipts.json",
        run_id="diagnostic-123",
        profile_id="official-gemini",
        allow_failed_persona=True,
    )

    assert worker["result_source"] == "codex_worker"
    assert worker["fallback_reason"] is None
    assert worker["completed_scenario_command_ids"] == list(
        verifier.AGENT_LIVE_SCENARIO_IDS
    )
    assert worker["p0_06_command_phases"] == ["CAPTURE"]
    assert [row["scenario_id"] for row in live_set["receipts"]] == list(
        live_request.LIVE_SCENARIO_IDS
    )
    assert live_set["persona_finalizer"] is None
    assert result["scenarios"][5]["status"] == "BLOCKED_EVIDENCE"
    assert result["scenarios"][5]["persona_finalizer"] is None
    assert result["scenarios"][6]["scenario_id"] == "P0-07"
    assert result["scenarios"][12]["scenario_id"] == "P0-13"


def test_diagnostic_trusted_probe_error_is_receipted_and_sequence_continues(
    tmp_path,
):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def runner(spec: launcher.WorkerSpec, _timeout: int) -> int:
        schema = json.loads(spec.schema_path.read_text())
        spec.result_path.write_text(
            json.dumps(_instance(schema, schema["$defs"])) + "\n"
        )
        spec.events_path.write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in (
                    {
                        "type": "thread.started",
                        "thread_id": "30000000-0000-4000-8000-000000000007",
                        "session_id": "40000000-0000-4000-8000-000000000007",
                    },
                    {"type": "turn.started"},
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": verifier.MANDATORY_SOP_READ_COMMAND,
                            "status": "completed",
                            "exit_code": 0,
                        },
                    },
                    *_scenario_command_rows(),
                    {"type": "turn.completed", "usage": {}},
                )
            )
        )
        _request_passing_cot_probe(spec)
        _request_passing_live_probes(spec)
        result = json.loads(spec.result_path.read_text())
        scenario = next(
            row for row in result["scenarios"] if row["scenario_id"] == "P0-09"
        )
        stale_failure = {
            "category": "BLOCKED_EVIDENCE",
            "stage_code": "RELIABILITY_CHAT",
            "failure_code": "PRECONDITION_MISSING",
            "reproducible": True,
        }
        scenario["failure"] = stale_failure
        scenario["attempt_results"][0]["failure"] = dict(stale_failure)
        spec.result_path.write_text(json.dumps(result) + "\n")
        return 0

    def crashing_probe(spec, scenario_id, attempt, nonce, prior_receipts):
        if scenario_id == "P0-09":
            raise launcher.WorkerLaunchError("simulated trusted probe crash")
        return _passing_live_probe(
            spec, scenario_id, attempt, nonce, prior_receipts
        )

    receipt = _launch_diagnostic(
        paths,
        runner,
        live_probe_runner=crashing_probe,
    )
    worker = receipt["workers"][0]
    result = json.loads(
        (paths["aggregation"] / "official-gemini.json").read_text()
    )
    live_set, _ = live_receipts.validate_live_scenario_receipts(
        paths["raw"] / "official-gemini" / "live-scenario-receipts.json",
        run_id="diagnostic-123",
        profile_id="official-gemini",
        allow_failed_persona=True,
    )
    by_id = {row["scenario_id"]: row for row in live_set["receipts"]}

    assert worker["result_source"] == "codex_worker"
    assert worker["fallback_reason"] is None
    assert by_id["P0-09"]["status"] == "BLOCKED_EVIDENCE"
    assert by_id["P0-09"]["failure_code"] == "LIVE_PROBE_ERROR"
    assert by_id["P0-09"]["turns"] == []
    assert by_id["P0-10"]["scenario_id"] == "P0-10"
    assert by_id["P0-11"]["scenario_id"] == "P0-11"
    assert by_id["P0-13"]["scenario_id"] == "P0-13"
    assert result["scenarios"][8]["status"] == "BLOCKED_EVIDENCE"
    assert result["scenarios"][8]["failure"] == {
        "category": "BLOCKED_EVIDENCE",
        "stage_code": "RELIABILITY_CHAT",
        "failure_code": "TRACE_UNAVAILABLE",
        "reproducible": True,
    }
    assert (
        result["scenarios"][8]["attempt_results"][0]["failure"]
        == result["scenarios"][8]["failure"]
    )


@pytest.mark.parametrize(
    (
        "target_scenario",
        "trusted_status",
        "trusted_code",
        "expected_stage",
        "expected_code",
    ),
    (
        (
            "P0-07",
            "PRODUCT_FAIL",
            "ASSERTION_FAILED",
            "ACTIVATION",
            "UNCLASSIFIED_PRODUCT_FAILURE",
        ),
        (
            "P0-10",
            "BLOCKED_EVIDENCE",
            "LIVE_PROBE_ERROR",
            "MEMORY_PERSONA",
            "TRACE_UNAVAILABLE",
        ),
    ),
)
def test_diagnostic_replaces_stale_failure_with_trusted_live_failure(
    tmp_path,
    target_scenario,
    trusted_status,
    trusted_code,
    expected_stage,
    expected_code,
):
    paths = _setup(tmp_path, qualification_mode="diagnostic")
    successful = _successful_runner([], synchronize=False)

    def runner(spec: launcher.WorkerSpec, timeout: int) -> int:
        code = successful(spec, timeout)
        result = json.loads(spec.result_path.read_text())
        scenario = next(
            row
            for row in result["scenarios"]
            if row["scenario_id"] == target_scenario
        )
        stale_failure = {
            "category": "BLOCKED_EVIDENCE",
            "stage_code": expected_stage,
            "failure_code": "PRECONDITION_MISSING",
            "reproducible": True,
        }
        scenario["failure"] = stale_failure
        scenario["attempt_results"][0]["failure"] = dict(stale_failure)
        semantic_assertions = live_receipts.SEMANTIC_ASSERTIONS[target_scenario]
        if semantic_assertions:
            scenario["assertions"] = {
                assertion: True for assertion in semantic_assertions
            }
        spec.result_path.write_text(json.dumps(result) + "\n")
        return code

    def failed_probe(spec, scenario_id, attempt, nonce, prior_receipts):
        receipt, facts = _passing_live_probe(
            spec, scenario_id, attempt, nonce, prior_receipts
        )
        if scenario_id == target_scenario:
            receipt.update(
                {
                    "status": trusted_status,
                    "failure_code": trusted_code,
                    "request_ids": [],
                    "turn_ids": [],
                    "trace_ids": [],
                    "turns": [],
                }
            )
            receipt["assertions"] = {
                assertion: False
                for assertion in live_receipts.DETERMINISTIC_ASSERTIONS[scenario_id]
            }
        return receipt, facts

    receipt = _launch_diagnostic(
        paths,
        runner,
        live_probe_runner=failed_probe,
    )
    worker = receipt["workers"][0]
    result = json.loads(
        (paths["aggregation"] / "official-gemini.json").read_text()
    )
    assert worker["result_source"] == "codex_worker", worker
    assert worker["fallback_reason"] is None
    scenario = next(
        row
        for row in result["scenarios"]
        if row["scenario_id"] == target_scenario
    )
    expected_failure = {
        "category": trusted_status,
        "stage_code": expected_stage,
        "failure_code": expected_code,
        "reproducible": True,
    }

    assert scenario["status"] == trusted_status
    assert scenario["failure"] == expected_failure
    assert scenario["attempt_results"][0]["failure"] == expected_failure
    assert all(
        scenario["assertions"][assertion] is False
        for assertion in live_receipts.SEMANTIC_ASSERTIONS[target_scenario]
    )


def test_diagnostic_p0_13_probe_error_preserves_agent_result(tmp_path):
    paths = _setup(tmp_path, qualification_mode="diagnostic")

    def runner(spec: launcher.WorkerSpec, _timeout: int) -> int:
        schema = json.loads(spec.schema_path.read_text())
        spec.result_path.write_text(
            json.dumps(_instance(schema, schema["$defs"])) + "\n"
        )
        spec.events_path.write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in (
                    {
                        "type": "thread.started",
                        "thread_id": "30000000-0000-4000-8000-000000000008",
                        "session_id": "40000000-0000-4000-8000-000000000008",
                    },
                    {"type": "turn.started"},
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": verifier.MANDATORY_SOP_READ_COMMAND,
                            "status": "completed",
                            "exit_code": 0,
                        },
                    },
                    *_scenario_command_rows(),
                    {"type": "turn.completed", "usage": {}},
                )
            )
        )
        _request_passing_cot_probe(spec)
        _request_passing_live_probes(spec)
        return 0

    def crashing_probe(spec, scenario_id, attempt, nonce, prior_receipts):
        if scenario_id == "P0-13":
            raise launcher.WorkerLaunchError("simulated trace-cleanup probe crash")
        return _passing_live_probe(
            spec, scenario_id, attempt, nonce, prior_receipts
        )

    receipt = _launch_diagnostic(
        paths,
        runner,
        live_probe_runner=crashing_probe,
    )
    worker = receipt["workers"][0]
    result = json.loads(
        (paths["aggregation"] / "official-gemini.json").read_text()
    )
    live_set, _ = live_receipts.validate_live_scenario_receipts(
        paths["raw"] / "official-gemini" / "live-scenario-receipts.json",
        run_id="diagnostic-123",
        profile_id="official-gemini",
        allow_failed_persona=True,
    )
    by_id = {row["scenario_id"]: row for row in live_set["receipts"]}

    assert worker["result_source"] == "codex_worker"
    assert worker["fallback_reason"] is None
    assert worker["completed_scenario_command_ids"] == list(
        verifier.AGENT_LIVE_SCENARIO_IDS
    )
    assert worker["completed_scenario_command_counts"] == (
        verifier.MIN_SCENARIO_COMMAND_COUNTS
    )
    assert [row["scenario_id"] for row in live_set["receipts"]] == list(
        live_request.LIVE_SCENARIO_IDS
    )
    assert by_id["P0-13"]["status"] == "BLOCKED_EVIDENCE"
    assert by_id["P0-13"]["failure_code"] == "LIVE_PROBE_ERROR"
    assert by_id["P0-13"]["result_projection"] is None
    assert len(result["scenarios"]) == 13
    assert result["scenarios"][12]["failure"]["failure_code"] == (
        "TRACE_UNAVAILABLE"
    )
    assert result["reasoning"]["request_id"] == "request-1"
    assert result["scenarios"][11]["request_ids"] == ["request-1"]
    assert len(result["turns"]) == 14
    assert all(row["scenario_id"] != "P0-12" for row in result["turns"])
    assert all(
        value is None
        for row in result["turns"]
        for value in row["stage_latency_ms"].values()
    )
    assert result["latency"]["sample_count"] == 14
    assert result["latency"]["missing_stages"] == [
        "routing",
        "queue",
        "provider",
        "persistence",
        "delivery",
    ]
    assert result["trace"] == {
        "enabled": False,
        "deploy_enabled": False,
        "correlated_event_count": 0,
        "observed_event_types": [],
        "missing_required_event_types": [
            "routing",
            "queue",
            "provider",
            "persistence",
            "delivery",
        ],
        "raw_trace_stored": False,
    }
    assert result["cleanup"] == {
        "attempted": False,
        "provider_config_deleted": False,
        "account_reset": False,
        "old_credential_rejected": False,
        "status": "BLOCKED_EVIDENCE",
    }


def test_launcher_rejects_ambient_readable_private_roots(tmp_path, monkeypatch):
    paths = _setup(tmp_path)
    monkeypatch.setattr(launcher, "_AMBIENT_READ_ROOTS", (tmp_path.resolve(),))
    invoked = False

    def runner(spec: launcher.WorkerSpec, timeout: int) -> int:
        nonlocal invoked
        invoked = True
        return 0

    with pytest.raises(launcher.WorkerLaunchError, match="ambient-readable"):
        _launch(paths, runner)
    assert invoked is False
    assert not paths["receipt"].exists()


@pytest.mark.parametrize(
    "runner_kwargs",
    (
        {"duplicate_thread": True},
        {"invalid_result": True},
        {"extra_file": True},
    ),
)
def test_launcher_fails_closed_on_invalid_worker_evidence(tmp_path, runner_kwargs):
    paths = _setup(tmp_path)
    captured: list[launcher.WorkerSpec] = []
    with pytest.raises(launcher.WorkerLaunchError):
        _launch(paths, _successful_runner(captured, **runner_kwargs))
    assert len(captured) == len(PROFILE_AGENT_TYPES)
    assert not paths["receipt"].exists()


def test_verifier_rejects_tampered_canonical_input(tmp_path):
    paths = _setup(tmp_path)
    _launch(paths, _successful_runner([]))
    canonical = paths["aggregation"] / f"{PROFILE_AGENT_TYPES[0][0]}.json"
    canonical.write_text("{}\n")
    with pytest.raises(verifier.OrchestrationError, match="canonical aggregation"):
        verifier.verify(paths["receipt"], paths["raw"], paths["aggregation"])


def test_verifier_rejects_tampered_cot_lifecycle_binding(tmp_path):
    paths = _setup(tmp_path)
    _launch(paths, _successful_runner([]))
    receipt = json.loads(paths["receipt"].read_text())
    receipt["workers"][0]["cot_receipt_sha256"] = "0" * 64
    paths["receipt"].write_text(json.dumps(receipt) + "\n")
    paths["receipt"].chmod(0o600)

    with pytest.raises(verifier.OrchestrationError, match="COT evidence"):
        verifier.verify(paths["receipt"], paths["raw"], paths["aggregation"])


def test_agent_writable_receipt_is_not_authoritative(tmp_path):
    paths = _setup(tmp_path)
    successful = _successful_runner([])

    def runner(spec: launcher.WorkerSpec, timeout: int) -> int:
        code = successful(spec, timeout)
        untrusted = spec.work / "cot-delivery-receipt.json"
        untrusted.write_text('{"status":"PASS","forged":true}\n')
        untrusted.chmod(0o600)
        return code

    # The authoritative receipt still lives outside the agent-writable work
    # root and is the only one hashed into lifecycle data.
    receipt = _launch(paths, runner)
    worker = receipt["workers"][0]

    assert len(worker["cot_receipt_sha256"]) == 64
    assert worker["cot_delivery_status"] == "PASS"
    assert json.loads(
        (paths["raw"] / "official-gemini" / "cot-delivery-receipt.json").read_text()
    )["profile_id"] == "official-gemini"


def test_parse_events_rejects_nested_agents(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {
                    "type": "item.started",
                    "item": {"type": "collab_tool_call", "tool": "spawn_agent"},
                },
                {"type": "turn.completed"},
            )
        )
    )
    path.chmod(0o600)
    with pytest.raises(verifier.OrchestrationError, match="nested orchestration"):
        verifier.parse_exec_events(path.resolve())


@pytest.mark.parametrize(
    ("row", "expected"),
    (
        (
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "status": "completed"},
            },
            1,
        ),
        (
            {
                "type": "item.started",
                "item": {"type": "command_execution", "status": "in_progress"},
            },
            0,
        ),
        (
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "status": "failed"},
            },
            0,
        ),
        (
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "status": "completed"},
            },
            0,
        ),
    ),
)
def test_tool_gate_counts_only_completed_command_events(tmp_path, row, expected):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(row) + "\n")
    path.chmod(0o600)

    assert launcher._completed_command_execution_count(path.resolve()) == expected


def test_scenario_command_evidence_is_anchored_and_one_marker_per_command(tmp_path):
    path = tmp_path / "events.jsonl"
    rows = [
        *_scenario_command_rows(),
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "echo QA_SCENARIO_ID=P0-02 QA_SCENARIO_ID=P0-03",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "QA_SCENARIO_ID=P0-12 echo parent-owned",
                "status": "completed",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    (
        count,
        scenario_ids,
        sop_read_first,
        scenario_counts,
        p0_06_phases,
    ) = verifier.completed_command_evidence(path.resolve())

    assert count == sum(verifier.MIN_SCENARIO_COMMAND_COUNTS.values()) + 2
    assert scenario_ids == ()
    assert sop_read_first is False
    assert scenario_counts["P0-12"] == 2
    assert p0_06_phases == verifier.P0_06_COMMAND_PHASES
    assert (
        verifier.scenario_command_contract_satisfied(
            scenario_counts, p0_06_phases
        )
        is False
    )


def test_completed_command_evidence_unwraps_real_codex_shell_commands(tmp_path):
    path = tmp_path / "events.jsonl"
    rows = [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": _wrapped_codex_command(
                    verifier.MANDATORY_SOP_READ_COMMAND
                ),
                "status": "completed",
                "exit_code": 0,
            },
        },
        *_scenario_command_rows(),
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    (
        count,
        scenario_ids,
        sop_read_first,
        scenario_counts,
        p0_06_phases,
    ) = verifier.completed_command_evidence(path.resolve())

    assert count == 15
    assert scenario_ids == verifier.AGENT_LIVE_SCENARIO_IDS
    assert sop_read_first is True
    assert scenario_counts == verifier.MIN_SCENARIO_COMMAND_COUNTS
    assert p0_06_phases == verifier.P0_06_COMMAND_PHASES


def test_p0_12_command_contract_accepts_exact_helper_once_in_order(tmp_path):
    rows = _scenario_command_rows()
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    _, scenario_ids, _, counts, phases = verifier.completed_command_evidence(
        path.resolve()
    )

    assert scenario_ids == verifier.AGENT_LIVE_SCENARIO_IDS
    assert counts["P0-12"] == 1
    assert verifier.scenario_command_contract_satisfied(counts, phases)


def test_p0_12_command_contract_rejects_missing_helper(tmp_path):
    rows = [
        row
        for row in _scenario_command_rows()
        if "QA_SCENARIO_ID=P0-12" not in row["item"]["command"]
    ]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    _, scenario_ids, _, counts, phases = verifier.completed_command_evidence(
        path.resolve()
    )

    assert "P0-12" not in scenario_ids
    assert counts["P0-12"] == 0
    assert not verifier.scenario_command_contract_satisfied(counts, phases)


def test_p0_12_command_contract_rejects_duplicate_helper(tmp_path):
    rows = _scenario_command_rows()
    p0_12 = next(
        index
        for index, row in enumerate(rows)
        if "QA_SCENARIO_ID=P0-12" in row["item"]["command"]
    )
    rows.insert(p0_12 + 1, json.loads(json.dumps(rows[p0_12])))
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    _, scenario_ids, _, counts, phases = verifier.completed_command_evidence(
        path.resolve()
    )

    assert scenario_ids == ()
    assert counts["P0-12"] == 2
    assert not verifier.scenario_command_contract_satisfied(counts, phases)


def test_p0_12_command_contract_rejects_out_of_order_helper(tmp_path):
    rows = _scenario_command_rows()
    p0_11 = next(
        index
        for index, row in enumerate(rows)
        if "QA_SCENARIO_ID=P0-11" in row["item"]["command"]
    )
    p0_12 = next(
        index
        for index, row in enumerate(rows)
        if "QA_SCENARIO_ID=P0-12" in row["item"]["command"]
    )
    rows[p0_11], rows[p0_12] = rows[p0_12], rows[p0_11]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    _, scenario_ids, _, counts, _ = verifier.completed_command_evidence(
        path.resolve()
    )

    assert scenario_ids == ()
    assert counts["P0-12"] == 1


@pytest.mark.parametrize(
    "command",
    (
        'QA_SCENARIO_ID=P0-02 "$QA_PYTHON_BIN" -c pass',
        (
            'QA_SCENARIO_ID=P0-02 /usr/bin/python3 '
            '"$QA_SOURCE_ROOT/qa/request_live_scenario_probe.py" '
            '--scenario P0-02 --attempt 1 '
            '--request "$QA_WORK_ROOT/.live-probe-P0-02-1.request" '
            '--facts "$QA_WORK_ROOT/live-probe-P0-02-1.facts.json"'
        ),
        verifier.live_request_command("P0-02", 1) + "; echo chained",
        verifier.live_request_command("P0-02", 1).replace(
            "live-probe-P0-02-1.facts.json",
            "live-probe-P0-03-1.facts.json",
        ),
    ),
)
def test_live_scenario_evidence_rejects_generic_wrong_or_chained_helper(
    tmp_path, command
):
    rows = _scenario_command_rows()
    target = next(
        row
        for row in rows
        if "QA_SCENARIO_ID=P0-02" in row["item"]["command"]
    )
    target["item"]["command"] = _wrapped_codex_command(command)
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    _, scenario_ids, _, scenario_counts, _ = verifier.completed_command_evidence(
        path.resolve()
    )

    assert scenario_ids != verifier.AGENT_LIVE_SCENARIO_IDS
    assert "P0-02" not in scenario_ids
    assert scenario_counts["P0-02"] == 1


def test_live_scenario_evidence_rejects_extra_marker_even_beside_valid_helper(
    tmp_path,
):
    rows = _scenario_command_rows()
    target_index = next(
        index
        for index, row in enumerate(rows)
        if "QA_SCENARIO_ID=P0-02" in row["item"]["command"]
    )
    rows.insert(
        target_index + 1,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": _wrapped_codex_command(
                    'QA_SCENARIO_ID=P0-02 "$QA_PYTHON_BIN" -c pass'
                ),
                "status": "completed",
                "exit_code": 0,
            },
        },
    )
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    _, scenario_ids, _, scenario_counts, phases = (
        verifier.completed_command_evidence(path.resolve())
    )

    assert scenario_ids == ()
    assert scenario_counts["P0-02"] == 2
    assert not verifier.scenario_command_contract_satisfied(
        scenario_counts, phases
    )


def test_live_scenario_evidence_rejects_out_of_order_exact_requests(tmp_path):
    rows = _scenario_command_rows()
    p0_02 = next(
        index
        for index, row in enumerate(rows)
        if "QA_SCENARIO_ID=P0-02" in row["item"]["command"]
    )
    p0_03 = next(
        index
        for index, row in enumerate(rows)
        if "QA_SCENARIO_ID=P0-03" in row["item"]["command"]
    )
    rows[p0_02], rows[p0_03] = rows[p0_03], rows[p0_02]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    _, scenario_ids, _, _, _ = verifier.completed_command_evidence(path.resolve())

    assert scenario_ids == ()


def test_live_scenario_evidence_accepts_exact_retry_order_only_for_chat_scenarios(
    tmp_path,
):
    rows = _scenario_command_rows()
    first_attempt = next(
        index
        for index, row in enumerate(rows)
        if "QA_SCENARIO_ID=P0-08" in row["item"]["command"]
    )
    rows.insert(
        first_attempt + 1,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": _wrapped_codex_command(
                    verifier.live_request_command("P0-08", 2)
                ),
                "status": "completed",
                "exit_code": 0,
            },
        },
    )
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    _, scenario_ids, _, scenario_counts, phases = (
        verifier.completed_command_evidence(path.resolve())
    )

    assert scenario_ids == verifier.AGENT_LIVE_SCENARIO_IDS
    assert scenario_counts["P0-08"] == 2
    assert verifier.scenario_command_contract_satisfied(scenario_counts, phases)
    with pytest.raises(ValueError, match="unsupported live request"):
        verifier.live_request_command("P0-02", 2)


def test_persona_phase_contract_rejects_out_of_order_phases():
    assert (
        verifier.scenario_command_contract_satisfied(
            verifier.MIN_SCENARIO_COMMAND_COUNTS,
            ("CAPTURE", "FINALIZE", "REVIEW"),
        )
        is False
    )


def test_terminal_persona_capture_command_contract_is_diagnostic_only(tmp_path):
    rows = [
        row
        for row in _scenario_command_rows()
        if not (
            "QA_SCENARIO_ID=P0-06" in row["item"]["command"]
            and any(
                phase in row["item"]["command"]
                for phase in (
                    "QA_SCENARIO_PHASE=REVIEW",
                    "QA_SCENARIO_PHASE=FINALIZE",
                )
            )
        )
    ]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    _, strict_ids, _, strict_counts, strict_phases = (
        verifier.completed_command_evidence(path.resolve())
    )
    assert strict_ids == ()
    assert not verifier.scenario_command_contract_satisfied(
        strict_counts, strict_phases
    )

    _, diagnostic_ids, _, diagnostic_counts, diagnostic_phases = (
        verifier.completed_command_evidence(
            path.resolve(), allow_failed_persona_capture=True
        )
    )
    assert diagnostic_ids == verifier.AGENT_LIVE_SCENARIO_IDS
    assert diagnostic_phases == ("CAPTURE",)
    assert verifier.scenario_command_contract_satisfied(
        diagnostic_counts,
        diagnostic_phases,
        allow_failed_persona_capture=True,
    )


def test_persona_review_program_rejects_prefilled_judgment(tmp_path):
    evidence = tmp_path / "evidence.json"
    judgment = tmp_path / "judgment.json"
    evidence.write_text('{"safe":"review-me"}\n', encoding="utf-8")
    judgment.write_text('{"prefilled":true}\n', encoding="utf-8")

    prefilled = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            verifier.P0_06_REVIEW_PROGRAM,
            str(evidence),
            str(judgment),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    judgment.unlink()
    clean = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            verifier.P0_06_REVIEW_PROGRAM,
            str(evidence),
            str(judgment),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert prefilled.returncode == 17
    assert prefilled.stdout == ""
    assert clean.returncode == 0
    evidence_sha256 = hashlib.sha256(evidence.read_bytes()).hexdigest()
    assert json.loads(clean.stdout) == {
        "safe": "review-me",
        "evidence_sha256": evidence_sha256,
    }


def test_persona_review_plaintext_is_redacted_from_codex_events(tmp_path):
    path = tmp_path / "events.jsonl"
    command = _wrapped_codex_command(
        "QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=REVIEW "
        '"$QA_PYTHON_BIN" -I -B -c '
        f"{shlex.quote(verifier.P0_06_REVIEW_PROGRAM)} "
        '"$QA_WORK_ROOT/p0-06-private-evidence.json" '
        '"$QA_WORK_ROOT/p0-06-semantic-judgment.json"'
    )
    rows = [
        {
            "type": "item.started",
            "item": {
                "type": "command_execution",
                "id": "review-command",
                "command": command,
                "aggregated_output": "decrypted-persona-canary",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "review-command",
                "command": command,
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": "decrypted-persona-canary",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "other-command",
                "command": "pwd",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": "safe-output",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    launcher._redact_persona_review_event_output(path.resolve())

    raw = path.read_text(encoding="utf-8")
    assert "decrypted-persona-canary" not in raw
    assert "safe-output" in raw
    redacted = [json.loads(line) for line in raw.splitlines()]
    review_rows = [
        row
        for row in redacted
        if row.get("item", {}).get("id") == "review-command"
    ]
    assert len(review_rows) == 2
    assert all(row["item"]["output_redacted"] is True for row in review_rows)
    assert review_rows[-1]["item"]["exit_code"] == 0


def test_schema_valid_negative_persona_verdict_counts_as_completed_phase(tmp_path):
    path = tmp_path / "events.jsonl"
    rows = _scenario_command_rows()
    finalize = next(
        row
        for row in rows
        if "QA_SCENARIO_PHASE=FINALIZE" in row["item"]["command"]
    )
    finalize["item"]["aggregated_output"] = '{"ok":false}'
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)

    (
        _count,
        _ids,
        _sop_first,
        scenario_counts,
        p0_06_phases,
    ) = verifier.completed_command_evidence(path.resolve())

    assert scenario_counts["P0-06"] == 3
    assert p0_06_phases == verifier.P0_06_COMMAND_PHASES


@pytest.mark.parametrize(
    "command",
    (
        (
            "QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=REVIEW "
            '"$QA_PYTHON_BIN" -I -B -c pass '
            '"$QA_WORK_ROOT/p0-06-private-evidence.json"'
        ),
        (
            "QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=FINALIZE "
            '"$QA_PYTHON_BIN" -c \'print(1)\' '
            '"$QA_SOURCE_ROOT/qa/finalize_persona_review.py" '
            "--private-evidence "
            '"$QA_WORK_ROOT/p0-06-private-evidence.json"'
        ),
        (
            "QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=CAPTURE "
            '"$QA_PYTHON_BIN" "$QA_SOURCE_ROOT/tools/genesis_e2e.py" '
            "distill-existing-session --api-url \"$IO_E2E_BASE_URL\" "
            '--session-manifest "$QA_PRIVATE_MANIFEST" '
            '--profile-id "$QA_PROFILE_ID" '
            '--fixture "$QA_SOURCE_ROOT/qa/fixtures/persona-import-v1.json" '
            '--private-evidence "$QA_WORK_ROOT/p0-06-private-evidence.json" '
            '--artifact-dir "$QA_ARTIFACT_DIR"; echo cheated'
        ),
    ),
)
def test_persona_phase_parser_rejects_noop_fake_or_chained_commands(command):
    tokens = verifier._command_tokens(_wrapped_codex_command(command))

    assert verifier._p0_06_phase(tokens) is None


def test_sop_read_requires_zero_exit_code(tmp_path):
    path = tmp_path / "events.jsonl"
    row = {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": _wrapped_codex_command(
                verifier.MANDATORY_SOP_READ_COMMAND
            ),
            "status": "completed",
            "exit_code": 1,
        },
    }
    path.write_text(json.dumps(row) + "\n")
    path.chmod(0o600)

    _, _, sop_read_first, _, _ = verifier.completed_command_evidence(
        path.resolve()
    )

    assert sop_read_first is False


def test_trusted_parent_probe_uses_fixed_interpreter_and_private_output(
    tmp_path, monkeypatch
):
    source = _private(tmp_path / "source")
    work = _private(tmp_path / "work")
    output = _private(tmp_path / "output")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n")
    manifest.chmod(0o600)
    spec = launcher.WorkerSpec(
        profile_id="official-gemini",
        agent_type="profile_official_gemini",
        command=("/trusted/codex", "exec"),
        environment={
            "QA_RUN_ID": "run-123",
            "QA_SOURCE_ROOT": str(source),
            "QA_PYTHON_BIN": "/trusted/runtime/bin/python3",
            "QA_PRIVATE_MANIFEST": str(manifest),
        },
        work=work,
        output_dir=output,
        schema_path=output / "schema.json",
        result_path=output / "result.json",
        events_path=output / "events.jsonl",
        stderr_path=output / "stderr.log",
        cot_receipt_path=output / "cot-delivery-receipt.json",
        cot_request_path=work / ".cot-probe-request",
        cot_facts_path=work / "cot-delivery-facts.json",
        live_receipt_path=output / "live-scenario-receipts.json",
        prompt="test",
    )
    seen: dict[str, Any] = {}

    def fake_run(command, **kwargs):
        seen["command"] = tuple(command)
        seen["kwargs"] = kwargs
        spec.cot_receipt_path.write_text(
            json.dumps(_passing_cot_receipt(spec.profile_id)) + "\n"
        )
        spec.cot_receipt_path.chmod(0o600)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    receipt = launcher._run_trusted_cot_probe(spec)

    assert seen["command"][:4] == (
        "/trusted/runtime/bin/python3",
        "-I",
        "-B",
        str(source / "qa" / "cot_delivery_probe.py"),
    )
    assert seen["kwargs"]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["check"] is False
    assert spec.cot_receipt_path.parent == output
    assert work not in spec.cot_receipt_path.parents
    assert receipt["status"] == "PASS"


@pytest.mark.skipif(os.name != "posix", reason="qualification runner is POSIX")
def test_timeout_kills_the_entire_codex_process_group(tmp_path, monkeypatch):
    events = tmp_path / "events.jsonl"
    stderr = tmp_path / "stderr.log"
    events.write_bytes(b"")
    stderr.write_bytes(b"")
    spec = launcher.WorkerSpec(
        profile_id="official-deepseek",
        agent_type="profile_official_deepseek",
        command=("/trusted/codex", "exec"),
        environment={},
        work=tmp_path,
        output_dir=tmp_path,
        schema_path=tmp_path / "schema.json",
        result_path=tmp_path / "result.json",
        events_path=events,
        stderr_path=stderr,
        cot_receipt_path=tmp_path / "cot-delivery-receipt.json",
        cot_request_path=tmp_path / ".cot-probe-request",
        cot_facts_path=tmp_path / "cot-delivery-facts.json",
        live_receipt_path=tmp_path / "live-scenario-receipts.json",
        prompt="test",
    )

    class TimedOutProcess:
        pid = 4321
        returncode = None

        def __init__(self):
            self.communications = 0
            self.parent_killed = False

        def communicate(self, _input=None, timeout=None):
            self.communications += 1
            if self.communications == 1:
                raise subprocess.TimeoutExpired("codex", timeout)
            self.returncode = -signal.SIGKILL
            return (None, None)

        def kill(self):
            self.parent_killed = True

    process = TimedOutProcess()
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: process)
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        launcher.os,
        "killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    assert launcher._run_process(spec, 60) == 124
    assert killed == [(process.pid, signal.SIGKILL)]
    assert process.parent_killed is False
    assert process.communications == 2
