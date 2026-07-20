"""Strict private projection for encrypted persona-memory failure debugging.

The formal persona experiment result is owner-only and contains behavioral
content.  This module accepts that result only through its versioned contract,
binds it byte-for-byte to the public persona summary, and projects a tiny set of
exact synthetic correlation identifiers for non-passing trajectories.  It never
copies prompts, replies, persona or memory content, judge rationales, trace
bodies, COT, credentials, or tokens.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from qa import publish_persona_memory_summary as persona_publisher
from qa.regression.contracts import (
    ContractError,
    ExperimentResult,
    Trajectory,
    canonical_json_sha256,
)
from qa.regression.report import public_failure_codes


LOCKED_BASE_URL = "https://test-api.feedling.app"
MAX_PERSONA_FAILURES = 8
MAX_TRAJECTORIES_PER_FAILURE = 3
MAX_IDS_PER_KIND = 64

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,159}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BUILD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_TRAJECTORY_STATUSES = frozenset({"FAIL", "BLOCKED_EVIDENCE", "INFRA_ERROR"})
_DEBUG_ID_FIELDS = frozenset(
    {
        "capture_job_ids",
        "request_ids",
        "response_ids",
        "trace_ids",
        "runtime_session_ids",
    }
)
PERSONA_FAILURE_FIELDS = frozenset(
    {
        "scenario_id",
        "scenario_version",
        "status",
        "failure_codes",
        "trajectories",
    }
)
PERSONA_TRAJECTORY_FIELDS = frozenset(
    {
        "repeat_index",
        "status",
        "failure_codes",
        "account_ids",
        "session_ids",
        "request_ids",
        "response_ids",
        "turn_ids",
        "trace_ids",
        "capture_job_ids",
        "runtime_session_ids",
    }
)


class PersonaDebugError(RuntimeError):
    """A fixed, content-free private projection failure."""


def _fail(message: str) -> PersonaDebugError:
    return PersonaDebugError(message)


def _safe_id(value: Any) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise _fail("persona debug identifier is invalid")
    return value


def _safe_ids(value: Any) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > MAX_IDS_PER_KIND
    ):
        raise _fail("persona debug identifier list is invalid")
    result = [_safe_id(item) for item in value]
    if len(result) != len(set(result)):
        raise _fail("persona debug identifier list is ambiguous")
    return result


def _debug_ids(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) - _DEBUG_ID_FIELDS:
        raise _fail("persona protected debug sidecar is invalid")
    result: dict[str, list[str]] = {}
    for field in sorted(_DEBUG_ID_FIELDS):
        identifiers = _safe_ids(value.get(field, []))
        if identifiers:
            result[field] = identifiers
    return result


def _append_unique(target: list[str], values: Sequence[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _account_map(result: ExperimentResult) -> dict[str, str]:
    fingerprints = result.metadata.get("account_fingerprints")
    rows = result.metadata.get("protected_debug_accounts")
    if (
        not isinstance(fingerprints, list)
        or not fingerprints
        or fingerprints != sorted(fingerprints)
        or len(fingerprints) != len(set(fingerprints))
        or any(
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
            for value in fingerprints
        )
        or not isinstance(rows, list)
        or len(rows) != len(fingerprints)
    ):
        raise _fail("persona debug account binding is invalid")
    result_map: dict[str, str] = {}
    seen_users: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
            "account_fingerprint",
            "user_id",
        }:
            raise _fail("persona debug account binding is invalid")
        fingerprint = row.get("account_fingerprint")
        user_id = _safe_id(row.get("user_id"))
        if (
            not isinstance(fingerprint, str)
            or _SHA256_RE.fullmatch(fingerprint) is None
            or hashlib.sha256(user_id.encode("utf-8")).hexdigest() != fingerprint
            or fingerprint in result_map
            or user_id in seen_users
        ):
            raise _fail("persona debug account binding is invalid")
        result_map[fingerprint] = user_id
        seen_users.add(user_id)
    if sorted(result_map) != fingerprints:
        raise _fail("persona debug account binding is invalid")
    return result_map


def _summary_binding(
    result: ExperimentResult,
    summary: Mapping[str, Any],
    *,
    result_sha256: str,
    canonical_run_id: str,
    expected_runtime: str,
    expected_deployment_sha: str,
) -> None:
    if (
        result.experiment_id != f"{canonical_run_id}-persona-memory"
        or len(result.targets) != 1
        or result.targets[0].label != "candidate"
        or result.targets[0].base_url != LOCKED_BASE_URL
        or result.targets[0].build_sha != expected_deployment_sha
        or result.targets[0].runtime_mode != expected_runtime
        or result.targets[0].provider != "openai"
        or _BUILD_SHA_RE.fullmatch(expected_deployment_sha) is None
    ):
        raise _fail("persona debug run or deployment binding is invalid")
    hashes = summary.get("hashes")
    pipeline = summary.get("pipeline_outcomes")
    if (
        not isinstance(hashes, Mapping)
        or hashes.get("result_sha256") != result_sha256
        or not isinstance(pipeline, Mapping)
    ):
        raise _fail("persona debug summary binding is invalid")
    required_hashes = {
        "arm_receipt_sha256",
        "source_bundle_sha256",
        "import_fixture_sha256",
        "pool_manifest_sha256",
        "route_sha256",
        "readiness_receipt_sha256",
        "cleanup_receipt_sha256",
        "deployment_receipt_pre_sha256",
        "deployment_receipt_post_sha256",
    }
    if any(
        not isinstance(hashes.get(name), str)
        or _SHA256_RE.fullmatch(hashes[name]) is None
        for name in required_hashes
    ):
        raise _fail("persona debug summary binding is invalid")
    if result.metadata.get("account_pool_manifest_sha256") != hashes.get(
        "pool_manifest_sha256"
    ):
        raise _fail("persona debug pool binding is invalid")
    arm = {
        "result_status": result.status,
        "source_bundle_sha256": hashes["source_bundle_sha256"],
        "import_fixture_sha256": hashes["import_fixture_sha256"],
        "pool_manifest_sha256": hashes["pool_manifest_sha256"],
        "route_sha256": hashes["route_sha256"],
        "readiness_receipt_sha256": hashes["readiness_receipt_sha256"],
        "cleanup_receipt_sha256": hashes["cleanup_receipt_sha256"],
        "deployment_receipt_pre_sha256": hashes[
            "deployment_receipt_pre_sha256"
        ],
        "deployment_receipt_post_sha256": hashes[
            "deployment_receipt_post_sha256"
        ],
        "account_fingerprints": list(result.metadata["account_fingerprints"]),
    }
    try:
        expected = persona_publisher.build_public_summary(
            result,
            arm,
            result_sha256=result_sha256,
            arm_receipt_sha256=hashes["arm_receipt_sha256"],
        )
    except (KeyError, TypeError, ValueError, persona_publisher.SummaryPublishError):
        raise _fail("persona debug summary binding is invalid") from None
    expected["pipeline_outcomes"] = dict(pipeline)
    if dict(summary) != expected:
        raise _fail("persona debug summary binding is invalid")


def _trajectory_status(
    trajectory: Trajectory, metrics: Sequence[Any]
) -> tuple[str, list[str]]:
    if trajectory.status == "INFRA_ERROR":
        status = "INFRA_ERROR"
    elif trajectory.status == "BLOCKED_EVIDENCE":
        status = "BLOCKED_EVIDENCE"
    elif any(metric.status == "INFRA_ERROR" for metric in metrics):
        status = "INFRA_ERROR"
    elif any(metric.status in {"BLOCKED_EVIDENCE", "SKIP"} for metric in metrics):
        status = "BLOCKED_EVIDENCE"
    elif any(metric.status == "FAIL" for metric in metrics):
        status = "FAIL"
    else:
        return "PASS", []
    raw_codes = []
    if trajectory.failure_code != "NONE":
        raw_codes.append(trajectory.failure_code)
    raw_codes.extend(
        code
        for metric in metrics
        if metric.status != "PASS"
        for code in metric.failure_codes
    )
    codes = public_failure_codes(raw_codes)
    if not codes:
        codes = ["PERSONA_METRIC_NONPASS"]
    return status, codes


def _trajectory_projection(
    trajectory: Trajectory,
    metrics: Sequence[Any],
    accounts: Mapping[str, str],
) -> dict[str, Any] | None:
    status, failure_codes = _trajectory_status(trajectory, metrics)
    if status == "PASS":
        return None
    identifiers: dict[str, list[str]] = {
        "session_ids": [],
        "request_ids": [],
        "response_ids": [],
        "turn_ids": [],
        "trace_ids": [],
        "capture_job_ids": [],
        "runtime_session_ids": [],
    }
    fingerprints = _safe_ids(
        trajectory.metadata.get("protected_debug_account_fingerprints", [])
    )
    if any(_SHA256_RE.fullmatch(value) is None for value in fingerprints):
        raise _fail("persona trajectory account binding is invalid")
    observed_fingerprints: set[str] = set()
    session_accounts: dict[str, str] = {}
    for turn in trajectory.turns:
        metadata = turn.metadata.get("protected_debug")
        if not isinstance(metadata, Mapping) or set(metadata) - {
            "account_fingerprint"
        }:
            raise _fail("persona turn account binding is invalid")
        fingerprint = metadata.get("account_fingerprint")
        if not isinstance(fingerprint, str) or _SHA256_RE.fullmatch(fingerprint) is None:
            raise _fail("persona turn account binding is invalid")
        observed_fingerprints.add(fingerprint)
        previous = session_accounts.setdefault(turn.session_id, fingerprint)
        if previous != fingerprint:
            raise _fail("persona session account binding is ambiguous")
        _append_unique(identifiers["session_ids"], [_safe_id(turn.session_id)])
        _append_unique(identifiers["request_ids"], [_safe_id(turn.request_id)])
        _append_unique(identifiers["response_ids"], [_safe_id(turn.response_id)])
        _append_unique(identifiers["turn_ids"], [_safe_id(turn.turn_id)])
        if turn.trace_id:
            _append_unique(identifiers["trace_ids"], [_safe_id(turn.trace_id)])
    if trajectory.turns and observed_fingerprints != set(fingerprints):
        raise _fail("persona trajectory account binding is ambiguous")
    if trajectory.turns and not fingerprints:
        raise _fail("persona trajectory account binding is missing")
    for boundary in trajectory.boundary_evidence:
        if not isinstance(boundary, Mapping):
            raise _fail("persona boundary debug evidence is invalid")
        evidence = boundary.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        debug = _debug_ids(evidence.get("protected_debug_identifiers"))
        for field, values in debug.items():
            _append_unique(identifiers[field], values)
    failure_debug = _debug_ids(
        trajectory.metadata.get("protected_debug_identifiers")
    )
    for field, values in failure_debug.items():
        _append_unique(identifiers[field], values)
    if any(len(values) > MAX_IDS_PER_KIND for values in identifiers.values()):
        raise _fail("persona trajectory debug identifier count is invalid")
    try:
        account_ids = [accounts[fingerprint] for fingerprint in fingerprints]
    except KeyError:
        raise _fail("persona trajectory account binding is missing") from None
    return {
        "repeat_index": trajectory.repeat_index,
        "status": status,
        "failure_codes": failure_codes,
        "account_ids": account_ids,
        **identifiers,
    }


def build_persona_failures(
    value: Any,
    summary: Mapping[str, Any],
    *,
    canonical_run_id: str,
    expected_runtime: str,
    expected_deployment_sha: str,
) -> list[dict[str, Any]]:
    """Validate one private result and project only non-passing exact IDs."""

    if not isinstance(value, Mapping):
        raise _fail("private persona result is invalid")
    try:
        result = ExperimentResult.from_dict(value)
        result_sha256 = canonical_json_sha256(value)
    except (ContractError, TypeError, ValueError):
        raise _fail("private persona result is invalid") from None
    accounts = _account_map(result)
    _summary_binding(
        result,
        summary,
        result_sha256=result_sha256,
        canonical_run_id=canonical_run_id,
        expected_runtime=expected_runtime,
        expected_deployment_sha=expected_deployment_sha,
    )
    metrics_by_trajectory: dict[str, list[Any]] = defaultdict(list)
    for metric in result.metric_results:
        metrics_by_trajectory[metric.trajectory_id].append(metric)
    scenarios = {row["scenario_id"]: row for row in summary["scenarios"]}
    failing_scenarios = {
        scenario_id for scenario_id, row in scenarios.items() if row["status"] != "PASS"
    }
    failures: list[dict[str, Any]] = []
    for scenario_id in sorted(failing_scenarios):
        scenario_summary = scenarios[scenario_id]
        trajectories = []
        versions: set[str] = set()
        for trajectory in sorted(
            (
                row for row in result.trajectories if row.scenario_id == scenario_id
            ),
            key=lambda row: row.repeat_index,
        ):
            versions.add(trajectory.scenario_version)
            projected = _trajectory_projection(
                trajectory,
                metrics_by_trajectory[trajectory.trajectory_id],
                accounts,
            )
            if projected is not None:
                trajectories.append(projected)
        if (
            len(versions) != 1
            or next(iter(versions), None) != scenario_summary["scenario_version"]
            or not trajectories
            or len(trajectories) > MAX_TRAJECTORIES_PER_FAILURE
        ):
            raise _fail("persona failure trajectory binding is invalid")
        failures.append(
            {
                "scenario_id": _safe_id(scenario_id),
                "scenario_version": _safe_id(scenario_summary["scenario_version"]),
                "status": scenario_summary["status"],
                "failure_codes": ["PERSONA_METRIC_NONPASS"],
                "trajectories": trajectories,
            }
        )
    if len(failures) > MAX_PERSONA_FAILURES:
        raise _fail("persona protected debug failure count is invalid")
    return failures


def validate_persona_failures(value: Any) -> None:
    """Validate the decrypted persona projection without private source files."""

    if not isinstance(value, list) or len(value) > MAX_PERSONA_FAILURES:
        raise _fail("persona protected debug failures are invalid")
    seen_scenarios: set[str] = set()
    for failure in value:
        if not isinstance(failure, Mapping) or set(failure) != PERSONA_FAILURE_FIELDS:
            raise _fail("persona protected debug failure is invalid")
        scenario_id = _safe_id(failure.get("scenario_id"))
        _safe_id(failure.get("scenario_version"))
        codes = failure.get("failure_codes")
        trajectories = failure.get("trajectories")
        if (
            failure.get("status") not in _TRAJECTORY_STATUSES
            or not isinstance(codes, list)
            or not codes
            or any(not isinstance(code, str) or _CODE_RE.fullmatch(code) is None for code in codes)
            or len(codes) != len(set(codes))
            or not isinstance(trajectories, list)
            or not 1 <= len(trajectories) <= MAX_TRAJECTORIES_PER_FAILURE
            or scenario_id in seen_scenarios
        ):
            raise _fail("persona protected debug failure is invalid")
        seen_scenarios.add(scenario_id)
        seen_repeats: set[int] = set()
        for trajectory in trajectories:
            if (
                not isinstance(trajectory, Mapping)
                or set(trajectory) != PERSONA_TRAJECTORY_FIELDS
                or type(trajectory.get("repeat_index")) is not int
                or not 0 <= trajectory["repeat_index"] < MAX_TRAJECTORIES_PER_FAILURE
                or trajectory.get("status") not in _TRAJECTORY_STATUSES
                or trajectory["repeat_index"] in seen_repeats
            ):
                raise _fail("persona protected debug trajectory is invalid")
            seen_repeats.add(trajectory["repeat_index"])
            trajectory_codes = trajectory.get("failure_codes")
            if (
                not isinstance(trajectory_codes, list)
                or not trajectory_codes
                or any(
                    not isinstance(code, str) or _CODE_RE.fullmatch(code) is None
                    for code in trajectory_codes
                )
                or len(trajectory_codes) != len(set(trajectory_codes))
            ):
                raise _fail("persona protected debug trajectory is invalid")
            for field in PERSONA_TRAJECTORY_FIELDS - {
                "repeat_index",
                "status",
                "failure_codes",
            }:
                _safe_ids(trajectory[field])


__all__ = [
    "PERSONA_FAILURE_FIELDS",
    "PERSONA_TRAJECTORY_FIELDS",
    "PersonaDebugError",
    "build_persona_failures",
    "validate_persona_failures",
]
