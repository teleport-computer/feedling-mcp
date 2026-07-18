#!/usr/bin/env python3
"""Render deterministic operator views for a local diagnostic qualification.

Unlike the protected release renderer, this renderer accepts a locked subset of
profiles and incomplete diagnostic evidence.  Missing evidence is rendered as
missing; it is never upgraded into a release-qualified success.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree

try:
    from qa.orchestration_contract import PROFILE_IDS
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from orchestration_contract import PROFILE_IDS


SCENARIO_IDS = tuple(f"P0-{index:02d}" for index in range(1, 14))
TRACE_STAGES = ("routing", "queue", "provider", "persistence", "delivery")
TERMINAL_STATUSES = frozenset(
    (
        "PASS",
        "PRODUCT_FAIL",
        "BLOCKED_CREDENTIAL",
        "BLOCKED_EVIDENCE",
        "BLOCKED_DEPLOYMENT",
        "AGENT_ERROR",
        "SECURITY_FAIL",
    )
)
FAILURE_STATUSES = frozenset(("PRODUCT_FAIL", "SECURITY_FAIL"))
COT_JUNIT_FAILURE = "COT_DELIVERY_FAIL"
COT_DELIVERY_STATUSES = frozenset(("PASS", "FAIL", "UNVERIFIED", "NOT_RUN"))
PROVISION_STATUS_READY = "ready"
PROVISION_STATUS_BLOCKED = "blocked"
PROVISION_FAILURE_NONE = "NONE"
TRUSTED_NOT_RUN_PROVISION_FAILURE_CODES = frozenset({"VALID_KEY_REJECTED"})
DEPLOYMENT_RECHECK_FAILURE_CODES = frozenset(
    {"DEPLOYMENT_CHANGED_DURING_RUN", "END_HEALTH_REATTESTATION_FAILED"}
)
WORKER_RESULT_SOURCES = frozenset(
    {"codex_worker", "deterministic_fallback", "provision_blocked"}
)
WORKER_FAILURE_CODES_BY_STAGE = {
    "INVOCATION": frozenset({"INVOCATION_FAILED"}),
    "PROCESS_EXIT": frozenset({"PROCESS_EXIT_NONZERO"}),
    "OUTPUT_FILE_SET": frozenset({"OUTPUT_FILE_SET_INVALID"}),
    "STRUCTURED_RESULT": frozenset({"STRUCTURED_RESULT_INVALID"}),
    "EVENT_IDENTITY_PARSE": frozenset(
        {
            "EVENT_IDENTITY_PARSE_INVALID",
            "EVENT_IDENTITY_DUPLICATED",
            "EVENT_STREAM_DIGEST_INVALID",
        }
    ),
    "COMMAND_EVIDENCE_PARSE": frozenset({"COMMAND_EVIDENCE_PARSE_INVALID"}),
    "SCENARIO_COMMAND_EVIDENCE": frozenset(
        {"AGENT_TOOL_USE_MISSING", "AGENT_SCENARIO_TOOL_USE_MISSING"}
    ),
    "LIVE_RECEIPT_LOAD": frozenset({"LIVE_RECEIPT_INVALID"}),
    "LIVE_RECEIPT_PROJECTION": frozenset({"LIVE_RECEIPT_PROJECTION_INVALID"}),
    "LIVE_RECEIPT_SHAPE": frozenset({"LIVE_RECEIPT_SHAPE_INVALID"}),
    "LIVE_RECEIPT_BINDING": frozenset({"LIVE_RECEIPT_BINDING_INVALID"}),
    "COT_RECEIPT_LOAD": frozenset({"COT_RECEIPT_MISSING", "COT_RECEIPT_INVALID"}),
    "COT_BINDING": frozenset({"COT_RESULT_BINDING_MISMATCH"}),
    "WORKER_EVIDENCE": frozenset({"WORKER_EVIDENCE_INVALID"}),
}


class DiagnosticRenderError(RuntimeError):
    """Fixed diagnostic safe to return to a local operator."""


def _status(value: Any, *, missing: str = "MISSING") -> str:
    return value if value in TERMINAL_STATUSES else missing


def _profile(
    profile_results: Mapping[str, Mapping[str, Any]], profile_id: str
) -> Mapping[str, Any] | None:
    value = profile_results.get(profile_id)
    return value if isinstance(value, Mapping) else None


def _deployment_recheck(summary: Mapping[str, Any]) -> tuple[str, str, bool]:
    value = summary.get("deployment_recheck")
    if not isinstance(value, Mapping):
        return "NOT_APPLICABLE", "NOT_APPLICABLE", False
    status = value.get("status")
    code = value.get("failure_code")
    if (
        value.get("required") is not True
        or (status == "PASS" and code != "NONE")
        or (
            status == "FAIL"
            and code not in DEPLOYMENT_RECHECK_FAILURE_CODES
        )
        or status not in {"PASS", "FAIL"}
    ):
        return "UNVERIFIED", "UNVERIFIED", True
    return str(status), str(code), True


def _provisioning_dispositions(
    summary: Mapping[str, Any], profile_ids: Sequence[str]
) -> dict[str, tuple[str, str, str]]:
    """Return fail-closed ``(status, code, disposition)`` rows.

    Provisioning is a parent-owned trust boundary.  A row is considered
    ``NOT_RUN`` only when the two summary maps are complete, bind exactly to
    the selected matrix, and contain one of the fixed operational codes.  Any
    malformed or incomplete metadata invalidates the whole projection so a
    forged partial map can never turn a failed agent result into a skip.
    """

    unverified = {
        profile_id: ("UNVERIFIED", "UNVERIFIED", "UNVERIFIED")
        for profile_id in profile_ids
    }
    provisioning = summary.get("provisioning")
    if not isinstance(provisioning, Mapping):
        return unverified
    statuses = provisioning.get("profile_statuses")
    failure_codes = provisioning.get("failure_codes")
    if not isinstance(statuses, Mapping) or not isinstance(failure_codes, Mapping):
        return unverified

    expected = set(profile_ids)
    if set(statuses) != expected or set(failure_codes) != expected:
        return unverified

    rows: dict[str, tuple[str, str, str]] = {}
    for profile_id in profile_ids:
        status = statuses.get(profile_id)
        code = failure_codes.get(profile_id)
        if status == PROVISION_STATUS_READY and code == PROVISION_FAILURE_NONE:
            rows[profile_id] = ("READY", PROVISION_FAILURE_NONE, "RUN")
        elif (
            status == PROVISION_STATUS_BLOCKED
            and isinstance(code, str)
            and code in TRUSTED_NOT_RUN_PROVISION_FAILURE_CODES
        ):
            rows[profile_id] = ("BLOCKED", str(code), "NOT_RUN")
        else:
            return unverified
    return rows


def _worker_observability(
    summary: Mapping[str, Any], profile_ids: Sequence[str]
) -> dict[str, tuple[str, str, str, str]]:
    """Return fixed public worker source/exit/stage/code fields or UNVERIFIED."""

    unverified = {
        profile_id: ("UNVERIFIED", "UNVERIFIED", "UNVERIFIED", "UNVERIFIED")
        for profile_id in profile_ids
    }
    orchestration = summary.get("orchestration")
    if not isinstance(orchestration, Mapping):
        return unverified
    sources = orchestration.get("result_sources")
    exits = orchestration.get("process_exit_codes")
    stages = orchestration.get("failure_stages")
    codes = orchestration.get("failure_codes")
    maps = (sources, exits, stages, codes)
    expected = set(profile_ids)
    if any(not isinstance(value, Mapping) or set(value) != expected for value in maps):
        return unverified

    rows: dict[str, tuple[str, str, str, str]] = {}
    for profile_id in profile_ids:
        source = sources[profile_id]
        exit_code = exits[profile_id]
        stage = stages[profile_id]
        code = codes[profile_id]
        allowed_codes = WORKER_FAILURE_CODES_BY_STAGE.get(stage)
        if source not in WORKER_RESULT_SOURCES:
            return unverified
        if exit_code is not None and (
            not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or not -255 <= exit_code <= 255
        ):
            return unverified
        if (stage is None) != (code is None) or (
            code is not None and (allowed_codes is None or code not in allowed_codes)
        ):
            return unverified
        if source == "provision_blocked":
            if exit_code is not None or stage is not None:
                return unverified
        elif exit_code is None:
            return unverified
        rows[profile_id] = (
            str(source),
            "NONE" if exit_code is None else str(exit_code),
            "NONE" if stage is None else str(stage),
            "NONE" if code is None else str(code),
        )
    return rows


def _scenario_statuses(profile: Mapping[str, Any] | None) -> dict[str, str]:
    statuses: dict[str, str] = {}
    rows = profile.get("scenarios") if profile else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            scenario_id = row.get("scenario_id")
            if scenario_id in SCENARIO_IDS and scenario_id not in statuses:
                statuses[str(scenario_id)] = _status(row.get("status"))
    return statuses


def _observed_runtime(profile: Mapping[str, Any] | None) -> str:
    if profile and profile.get("observed_runtime") in (
        "hosted_resident",
        "resident_cli",
    ):
        version = profile.get("observed_runtime_version")
        suffix = f"@v{version}" if type(version) is int and version >= 1 else "@v?"
        return f"{profile['observed_runtime']}{suffix}"
    return "UNVERIFIED"


def _boolean_evidence(reasoning: Any, field: str) -> str:
    if not isinstance(reasoning, Mapping) or not isinstance(
        reasoning.get(field), bool
    ):
        return "UNVERIFIED"
    return "PRESENT" if reasoning[field] else "ABSENT"


def _reasoning_event(reasoning: Any) -> str:
    if not isinstance(reasoning, Mapping):
        return "UNVERIFIED"
    count = reasoning.get("reasoning_event_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return "UNVERIFIED"
    return "OBSERVED" if count > 0 else "NOT_OBSERVED"


def _cot_row(summary: Mapping[str, Any], profile_id: str) -> Mapping[str, Any] | None:
    matrix = summary.get("cot_delivery")
    if not isinstance(matrix, Mapping):
        return None
    row = matrix.get(profile_id)
    return row if isinstance(row, Mapping) else None


def _cot_token_evidence(cot: Mapping[str, Any] | None) -> str:
    if cot is None:
        return "UNVERIFIED"
    return "PRESENT" if cot.get("token_metadata_status") == "PRESENT" else "UNVERIFIED"


def _parent_finalized_profile_status(
    summary: Mapping[str, Any], profile_id: str
) -> str:
    statuses = summary.get("diagnostic_profile_statuses")
    cleanup = summary.get("parent_cleanup_verification")
    if (
        isinstance(statuses, Mapping)
        and isinstance(cleanup, Mapping)
        and cleanup.get("status") == "PASS"
        and statuses.get(profile_id) == "PASS"
    ):
        return "PASS"
    return "FAIL"


def _junit_scenario_status(
    summary: Mapping[str, Any],
    profile_id: str,
    scenario_id: str,
    scenario_statuses: Mapping[str, str],
) -> str:
    if scenario_id == "P0-12":
        cot = _cot_row(summary, profile_id)
        if cot is not None and cot.get("status") != "PASS":
            return COT_JUNIT_FAILURE
    if (
        scenario_id == "P0-13"
        and _parent_finalized_profile_status(summary, profile_id) == "PASS"
    ):
        return "PASS"
    return scenario_statuses.get(scenario_id, "MISSING")


def _cot_junit_message(summary: Mapping[str, Any], profile_id: str) -> str:
    cot = _cot_row(summary, profile_id)
    status = cot.get("status") if cot is not None else None
    normalized = status if status in COT_DELIVERY_STATUSES else "INVALID"
    return f"trusted-cot-delivery:{normalized}"


def _safe_number(value: Any, *, integer: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if not math.isfinite(value) or value < 0 or (integer and not isinstance(value, int)):
        return ""
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def render_matrix(
    summary: Mapping[str, Any],
    profile_results: Mapping[str, Mapping[str, Any]],
    profile_ids: Sequence[str],
) -> str:
    """Return a fixed-field Markdown view of the selected diagnostic matrix."""

    provisioning = _provisioning_dispositions(summary, profile_ids)
    worker_observability = _worker_observability(summary, profile_ids)
    harness = summary.get("qualification_harness")
    if not isinstance(harness, Mapping):
        harness = {}
    parent_cleanup = summary.get("parent_cleanup_verification")
    parent_cleanup_status = (
        parent_cleanup.get("status")
        if isinstance(parent_cleanup, Mapping)
        else "UNAVAILABLE"
    )
    finalized_statuses = summary.get("diagnostic_profile_statuses")
    finalized_passes = (
        sum(value == "PASS" for value in finalized_statuses.values())
        if isinstance(finalized_statuses, Mapping)
        else 0
    )
    deployment_identity_value = summary.get("deployment_identity")
    identity_present = (
        isinstance(deployment_identity_value, Mapping)
        and bool(deployment_identity_value)
    )
    deployment_identity = deployment_identity_value if identity_present else {}
    if deployment_identity.get("identity_verified") is True:
        identity_status = "VERIFIED"
    elif deployment_identity.get("identity_observed") is True:
        identity_status = "OBSERVED_UNATTESTED"
    else:
        identity_status = "UNAVAILABLE"
    identity_source = (
        str(
            deployment_identity.get("identity_evidence_source")
            or "protected_build_identity"
        )
        if identity_present
        else "UNAVAILABLE"
    )
    identity_gap = (
        str(deployment_identity.get("identity_gap_code") or "NONE")
        if identity_present
        else "UNAVAILABLE"
    )
    recheck_status, recheck_code, _ = _deployment_recheck(summary)
    lines = [
        "# io local API-key diagnostic",
        "",
        f"- Run ID: `{summary['run_id']}`",
        f"- Target backend SHA: `{summary['candidate_sha']}`",
        f"- Deployment identity: `{identity_status}`",
        f"- Identity evidence source: `{identity_source}`",
        f"- Identity evidence gap: `{identity_gap}`",
        f"- End-of-run deployment recheck: `{recheck_status}`",
        f"- Deployment recheck code: `{recheck_code}`",
        f"- Harness Git HEAD: `{harness.get('git_head', 'UNAVAILABLE')}`",
        f"- Harness source SHA-256: `{harness.get('source_sha256', 'UNAVAILABLE')}`",
        f"- Worker snapshot SHA-256: `{harness.get('worker_snapshot_sha256', 'UNAVAILABLE')}`",
        f"- Harness dirty: `{str(harness.get('dirty', 'UNAVAILABLE')).lower()}`",
        f"- Diagnostic status: `{summary['status']}`",
        f"- Deterministic parent cleanup: `{parent_cleanup_status}`",
        f"- Parent-finalized profiles: `{finalized_passes}/{len(profile_ids)}`",
        "- Release qualified: `false`",
        f"- Strict evidence gaps: `{len(summary.get('missing_strict_evidence', []))}`",
        "",
    ]
    header = (
        "Profile",
        "Provisioning",
        "Provisioning code",
        "Disposition",
        "Worker source",
        "Worker exit",
        "Worker failure stage",
        "Worker failure code",
        "Runtime",
        "COT delivery",
        "COT code",
        "COT observation",
        "COT observation code",
        "Reasoning event",
        "Reasoning metadata",
        "Reasoning tokens",
        "User disclosure",
        *(f"Agent {scenario_id}" for scenario_id in SCENARIO_IDS),
    )
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for profile_id in profile_ids:
        profile = _profile(profile_results, profile_id)
        cot = _cot_row(summary, profile_id)
        scenario_statuses = _scenario_statuses(profile)
        reasoning = profile.get("reasoning") if profile else None
        provision_status, provision_code, disposition = provisioning[profile_id]
        worker_source, worker_exit, worker_stage, worker_code = worker_observability[
            profile_id
        ]
        if disposition == "NOT_RUN":
            profile_status = "NOT_RUN"
            runtime = "NOT_RUN"
            cot_status = "NOT_RUN"
            cot_code = provision_code
            cot_observation = "NOT_RUN"
            cot_observation_code = "NOT_RUN"
            reasoning_event = "NOT_RUN"
            reasoning_metadata = "NOT_RUN"
            reasoning_tokens = "NOT_RUN"
            user_disclosure = "NOT_RUN"
            rendered_scenarios = ("NOT_RUN",) * len(SCENARIO_IDS)
            worker_source = "provision_blocked"
            worker_exit = "NOT_RUN"
            worker_stage = "NOT_RUN"
            worker_code = provision_code
        elif disposition == "UNVERIFIED":
            profile_status = "UNVERIFIED"
            runtime = "UNVERIFIED"
            cot_status = "UNVERIFIED"
            cot_code = "UNVERIFIED"
            cot_observation = "UNVERIFIED"
            cot_observation_code = "UNVERIFIED"
            reasoning_event = "UNVERIFIED"
            reasoning_metadata = "UNVERIFIED"
            reasoning_tokens = "UNVERIFIED"
            user_disclosure = "UNVERIFIED"
            rendered_scenarios = ("UNVERIFIED",) * len(SCENARIO_IDS)
            worker_source = "UNVERIFIED"
            worker_exit = "UNVERIFIED"
            worker_stage = "UNVERIFIED"
            worker_code = "UNVERIFIED"
        else:
            profile_status = (
                _status(profile.get("status"), missing="AGENT_ERROR")
                if profile
                else "AGENT_ERROR"
            )
            runtime = _observed_runtime(profile)
            cot_status = (
                str(cot.get("status") or "UNVERIFIED") if cot else "UNVERIFIED"
            )
            cot_code = (
                str(cot.get("failure_code") or "UNVERIFIED")
                if cot
                else "UNVERIFIED"
            )
            cot_observation = (
                str(cot.get("receipt_status") or "UNVERIFIED")
                if cot
                else "UNVERIFIED"
            )
            cot_observation_code = (
                str(cot.get("receipt_failure_code") or "UNVERIFIED")
                if cot
                else "UNVERIFIED"
            )
            reasoning_event = (
                _reasoning_event(cot)
                if cot is not None
                else _reasoning_event(reasoning)
            )
            reasoning_metadata = (
                _boolean_evidence(cot, "metadata_present")
                if cot is not None
                else _boolean_evidence(reasoning, "metadata_present")
            )
            reasoning_tokens = _cot_token_evidence(cot)
            user_disclosure = (
                _boolean_evidence(cot, "user_visible_disclosure_present")
                if cot is not None
                else _boolean_evidence(reasoning, "user_visible_disclosure_present")
            )
            rendered_scenarios = tuple(
                scenario_statuses.get(scenario_id, "MISSING")
                for scenario_id in SCENARIO_IDS
            )
        row = (
            profile_id,
            provision_status,
            provision_code,
            profile_status,
            worker_source,
            worker_exit,
            worker_stage,
            worker_code,
            runtime,
            cot_status,
            cot_code,
            cot_observation,
            cot_observation_code,
            reasoning_event,
            reasoning_metadata,
            reasoning_tokens,
            user_disclosure,
            *rendered_scenarios,
        )
        lines.append("| " + " | ".join(row) + " |")

    lines.extend(
        (
            "",
            "`MISSING` and `UNVERIFIED` are evidence gaps. This local report cannot "
            "be used as the protected release gate.",
        )
    )
    return "\n".join(lines) + "\n"


def render_latency(
    profile_results: Mapping[str, Mapping[str, Any]], profile_ids: Sequence[str]
) -> str:
    """Return one deterministic latency-attribution row per selected profile."""

    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        (
            "profile_id",
            "status",
            "sample_count",
            "ack_p50_ms",
            "reply_p50_ms",
            "reply_p95_ms",
            *(f"{stage}_p50_ms" for stage in TRACE_STAGES),
            "missing_stages",
            "release_qualified",
        )
    )
    for profile_id in profile_ids:
        profile = _profile(profile_results, profile_id)
        latency = profile.get("latency") if profile else None
        latency = latency if isinstance(latency, Mapping) else {}
        stage_values = latency.get("stage_p50_ms")
        stage_values = stage_values if isinstance(stage_values, Mapping) else {}
        missing = latency.get("missing_stages")
        if not isinstance(missing, list):
            missing = list(TRACE_STAGES)
        missing_stages = ";".join(
            stage for stage in TRACE_STAGES if stage in missing
        )
        writer.writerow(
            (
                profile_id,
                (
                    _status(profile.get("status"), missing="AGENT_ERROR")
                    if profile
                    else "NOT_RUN"
                ),
                _safe_number(latency.get("sample_count"), integer=True),
                _safe_number(latency.get("ack_p50_ms")),
                _safe_number(latency.get("reply_p50_ms")),
                _safe_number(latency.get("reply_p95_ms")),
                *(_safe_number(stage_values.get(stage)) for stage in TRACE_STAGES),
                missing_stages,
                "false",
            )
        )
    return stream.getvalue()


def render_junit(
    summary: Mapping[str, Any],
    profile_results: Mapping[str, Mapping[str, Any]],
    profile_ids: Sequence[str],
) -> str:
    """Return fixed-message JUnit; missing evidence is an error, not a pass."""

    provisioning = _provisioning_dispositions(summary, profile_ids)
    preflight_only = summary.get("preflight_only") is True
    tests_per_profile = len(SCENARIO_IDS) + 1
    recheck_status, recheck_code, recheck_present = _deployment_recheck(summary)
    tests = len(profile_ids) * tests_per_profile + int(recheck_present)
    failures = 0
    recheck_error = int(recheck_present and recheck_status != "PASS")
    errors = recheck_error
    skipped = 0
    rows: list[tuple[str, dict[str, str], str, str, int, int, int]] = []
    for profile_id in profile_ids:
        statuses = _scenario_statuses(_profile(profile_results, profile_id))
        _, provision_code, disposition = provisioning[profile_id]
        suite_failures = 0
        suite_errors = 0
        suite_skipped = 0
        if preflight_only:
            suite_skipped = tests_per_profile
        elif disposition == "NOT_RUN":
            suite_errors = 1
            suite_skipped = len(SCENARIO_IDS)
        elif disposition == "UNVERIFIED":
            suite_errors = tests_per_profile
        else:
            for scenario_id in SCENARIO_IDS:
                status = _junit_scenario_status(
                    summary, profile_id, scenario_id, statuses
                )
                if status in FAILURE_STATUSES or status == COT_JUNIT_FAILURE:
                    suite_failures += 1
                elif status != "PASS":
                    suite_errors += 1
        failures += suite_failures
        errors += suite_errors
        skipped += suite_skipped
        rows.append(
            (
                profile_id,
                statuses,
                provision_code,
                disposition,
                suite_failures,
                suite_errors,
                suite_skipped,
            )
        )

    root = ElementTree.Element(
        "testsuites",
        {
            "name": "io-local-api-key-diagnostic",
            "tests": str(tests),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "release_qualified": "false",
        },
    )
    if recheck_present:
        deployment_suite = ElementTree.SubElement(
            root,
            "testsuite",
            {
                "name": "io.diagnostic.deployment-stability",
                "tests": "1",
                "failures": "0",
                "errors": str(recheck_error),
                "skipped": "0",
            },
        )
        deployment_case = ElementTree.SubElement(
            deployment_suite,
            "testcase",
            {
                "classname": "io.diagnostic.deployment-stability",
                "name": "END_OF_RUN_HEALTH_IDENTITY",
            },
        )
        if recheck_error:
            ElementTree.SubElement(
                deployment_case,
                "error",
                {
                    "type": recheck_code,
                    "message": f"deployment-recheck:{recheck_code}",
                },
            )
    for (
        profile_id,
        statuses,
        provision_code,
        disposition,
        suite_failures,
        suite_errors,
        suite_skipped,
    ) in rows:
        suite = ElementTree.SubElement(
            root,
            "testsuite",
            {
                "name": f"io.diagnostic.{profile_id}",
                "tests": str(tests_per_profile),
                "failures": str(suite_failures),
                "errors": str(suite_errors),
                "skipped": str(suite_skipped),
            },
        )
        provisioning_testcase = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": f"io.diagnostic.{profile_id}",
                "name": "PROVISIONING",
            },
        )
        if preflight_only:
            ElementTree.SubElement(
                provisioning_testcase, "skipped", {"message": "preflight-only"}
            )
        elif disposition == "NOT_RUN":
            ElementTree.SubElement(
                provisioning_testcase,
                "error",
                {
                    "type": "PROVISIONING_BLOCKED",
                    "message": f"provisioning:{provision_code}",
                },
            )
        elif disposition == "UNVERIFIED":
            ElementTree.SubElement(
                provisioning_testcase,
                "error",
                {
                    "type": "UNVERIFIED",
                    "message": "provisioning-metadata:UNVERIFIED",
                },
            )
        for scenario_id in SCENARIO_IDS:
            testcase = ElementTree.SubElement(
                suite,
                "testcase",
                {
                    "classname": f"io.diagnostic.{profile_id}",
                    "name": scenario_id,
                },
            )
            if preflight_only:
                ElementTree.SubElement(
                    testcase, "skipped", {"message": "preflight-only"}
                )
                continue
            if disposition == "NOT_RUN":
                ElementTree.SubElement(
                    testcase,
                    "skipped",
                    {
                        "type": "NOT_RUN",
                        "message": f"provisioning:{provision_code}",
                    },
                )
                continue
            if disposition == "UNVERIFIED":
                ElementTree.SubElement(
                    testcase,
                    "error",
                    {
                        "type": "UNVERIFIED",
                        "message": "provisioning-metadata:UNVERIFIED",
                    },
                )
                continue
            status = _junit_scenario_status(
                summary, profile_id, scenario_id, statuses
            )
            if status == "PASS":
                continue
            child = (
                "failure"
                if status in FAILURE_STATUSES or status == COT_JUNIT_FAILURE
                else "error"
            )
            message = (
                _cot_junit_message(summary, profile_id)
                if status == COT_JUNIT_FAILURE
                else f"diagnostic-evidence:{status}"
            )
            ElementTree.SubElement(
                testcase,
                child,
                {"type": status, "message": message},
            )

    ElementTree.indent(root, space="  ")
    return (
        ElementTree.tostring(
            root, encoding="unicode", xml_declaration=True, short_empty_elements=True
        )
        + "\n"
    )


def _write_private_text(path: Path, content: str) -> None:
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
        raise DiagnosticRenderError("unable to create diagnostic operator artifact") from None


def render_operator_artifacts(
    *,
    summary: Mapping[str, Any],
    profile_results: Mapping[str, Mapping[str, Any]],
    profile_ids: Sequence[str],
    artifact_root: Path,
) -> None:
    """Write the three derived local-diagnostic views with owner-only modes."""

    selected = tuple(profile_ids)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(profile_id not in PROFILE_IDS for profile_id in selected)
        or set(profile_results) - set(selected)
        or summary.get("qualification_mode") != "diagnostic"
        or summary.get("release_qualified") is not False
    ):
        raise DiagnosticRenderError("diagnostic operator artifact input is invalid")
    try:
        root = artifact_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DiagnosticRenderError("diagnostic artifact root is unavailable") from None
    if artifact_root.is_symlink() or not root.is_dir():
        raise DiagnosticRenderError("diagnostic artifact root is unsafe")

    outputs = {
        root / "matrix.md": render_matrix(summary, profile_results, selected),
        root / "latency.csv": render_latency(profile_results, selected),
        root / "junit.xml": render_junit(summary, profile_results, selected),
    }
    for path, content in outputs.items():
        _write_private_text(path, content)
