#!/usr/bin/env python3
"""Validate parent-owned receipts for live P0 scenario probes.

The authoritative file is deliberately metadata-only.  Decrypted replies used
for P0-10/P0-11 semantic judgment stay in a separate agent-private facts copy;
the receipt binds that copy by SHA-256 without publishing its contents.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from qa.request_live_scenario_probe import LIVE_SCENARIO_IDS
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from request_live_scenario_probe import LIVE_SCENARIO_IDS


RECEIPT_SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
_FAILURE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_STATUSES = frozenset(
    {
        "PASS",
        "AGENT_ERROR",
        "PRODUCT_FAIL",
        "BLOCKED_CREDENTIAL",
        "BLOCKED_EVIDENCE",
        "BLOCKED_DEPLOYMENT",
        "SECURITY_FAIL",
    }
)
_RETRYABLE_SCENARIOS = frozenset({"P0-08", "P0-09", "P0-10", "P0-11"})
_TURN_COUNTS = {
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
    # P0-13 does not create new product turns.  Its parent-owned receipt
    # projects the fifteen earlier chat/COT turns so their five-stage latency
    # evidence can be bound without giving the profile worker network access.
    "P0-13": 15,
}
_TRACE_STAGES = ("routing", "queue", "provider", "persistence", "delivery")
DETERMINISTIC_ASSERTIONS = {
    "P0-02": frozenset(
        {"synthetic_account_is_fresh", "whoami_matches", "trace_cleared"}
    ),
    "P0-03": frozenset(
        {"invalid_key_rejected", "invalid_key_not_echoed", "hosted_chat_not_started"}
    ),
    "P0-04": frozenset(
        {"valid_key_accepted", "provider_config_matches", "credential_omitted"}
    ),
    "P0-05": frozenset(
        {
            "runtime_status_readback_succeeds",
            "runtime_configured",
            "runtime_metadata_recorded",
        }
    ),
    "P0-06": frozenset(
        {
            "persona_files_archived",
            "persona_source_metadata_verified",
            "persona_import_done",
        }
    ),
    "P0-07": frozenset(
        {
            "driver_enabled",
            "chat_loop_verified",
            "runtime_status_readback_succeeds",
            "no_orphan_turn",
        }
    ),
    "P0-08": frozenset(
        {
            "async_ack_received",
            "exact_reply_correlated",
            "nonce_echo_confirmed",
            "fallback_absent",
            "latency_recorded",
        }
    ),
    "P0-09": frozenset(
        {
            "ten_turns_ordered",
            "exact_replies_correlated",
            "memory_recall_confirmed",
            "no_orphan_turn",
        }
    ),
    "P0-10": frozenset({"transport_correlated"}),
    "P0-11": frozenset(
        {"transport_correlated", "provider_config_matches", "trace_route_correlated"}
    ),
    "P0-13": frozenset(
        {
            "trace_stages_complete",
            "trace_correlation_confirmed",
            "latency_attributed",
            "cleanup_confirmed",
        }
    ),
}
SEMANTIC_ASSERTIONS = {
    "P0-02": (),
    "P0-03": (),
    "P0-04": (),
    "P0-05": (),
    "P0-06": ("persona_acceptance_passed", "privacy_canary_absent"),
    "P0-07": (),
    "P0-08": (),
    "P0-09": (),
    "P0-10": (
        "imported_memory_recalled",
        "persona_consistency_confirmed",
        "contradictory_facts_absent",
    ),
    "P0-11": ("agent_identity_confirmed", "model_route_confirmed"),
    "P0-13": (),
}
_PERSONA_FINALIZER_FAILURE_CODES = frozenset(
    {"SEMANTIC_JUDGMENT_INVALID", "PERSONA_FINALIZER_FAILED"}
)
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "run_id",
        "profile_id",
        "scenario_id",
        "attempt",
        "nonce",
        "started_at",
        "finished_at",
        "status",
        "failure_code",
        "assertions",
        "semantic_assertions",
        "request_ids",
        "turn_ids",
        "trace_ids",
        "turns",
        "result_projection",
        "private_facts_sha256",
        "raw_content_stored",
    }
)
_TURN_KEYS = frozenset(
    {
        "turn_index",
        "request_id",
        "turn_id",
        "trace_id",
        "ack_latency_ms",
        "reply_latency_ms",
        "stage_latency_ms",
        "reply_count",
        "content_assertion_passed",
        "fallback_detected",
        "duplicate_detected",
        "out_of_order_detected",
    }
)


class LiveScenarioReceiptError(RuntimeError):
    """A live receipt is unsafe, malformed, replayed, or inconsistent."""


def failed_persona_result_projection(
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a bounded diagnostic result when persona capture itself failed.

    No semantic review exists on this path, so both semantic assertions remain
    false and ``persona_finalizer`` remains null.  The trusted capture receipt,
    rather than the agent, owns the terminal status and deterministic evidence.
    """

    status = receipt.get("status")
    failure_codes = {
        "PRODUCT_FAIL": "PERSONA_IMPORT_FAILED",
        "BLOCKED_EVIDENCE": "TRACE_UNAVAILABLE",
        "AGENT_ERROR": "AGENT_CRASHED",
        "BLOCKED_CREDENTIAL": "CREDENTIAL_SETUP_FAILED",
        "BLOCKED_DEPLOYMENT": "WORKER_UNREADY",
        "SECURITY_FAIL": "REDACTION_ASSERTION_FAILED",
    }
    failure_code = failure_codes.get(status)
    assertions = receipt.get("assertions")
    if (
        status == "PASS"
        or failure_code is None
        or not isinstance(assertions, Mapping)
    ):
        raise LiveScenarioReceiptError(
            "failed persona capture projection is invalid"
        )
    projected_assertions = {
        **dict(assertions),
        "persona_acceptance_passed": False,
        "privacy_canary_absent": False,
    }
    failure = {
        "category": status,
        "stage_code": "PERSONA_IMPORT",
        "failure_code": failure_code,
        "reproducible": True,
    }
    return {
        "status": status,
        "assertions": projected_assertions,
        "evidence_codes": [
            code
            for assertion, code in (
                ("persona_files_archived", "PERSONA_FILES_ARCHIVED"),
                (
                    "persona_source_metadata_verified",
                    "PERSONA_SOURCE_METADATA_VERIFIED",
                ),
                ("persona_import_done", "PERSONA_IMPORT_DONE"),
            )
            if assertions.get(assertion) is True
        ],
        "persona_finalizer": None,
        "failure": failure,
    }


def persona_finalizer_failure(code: str) -> dict[str, Any]:
    """Return the sole diagnostic sentinel for a failed parent re-finalization."""

    if code not in _PERSONA_FINALIZER_FAILURE_CODES:
        raise LiveScenarioReceiptError("persona finalizer failure code is invalid")
    return {"kind": "persona_finalizer_failure", "failure_code": code}


def _valid_persona_finalizer_failure(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"kind", "failure_code"}
        and value.get("kind") == "persona_finalizer_failure"
        and value.get("failure_code") in _PERSONA_FINALIZER_FAILURE_CODES
    )


def unfinalized_persona_result_projection(
    receipt: Mapping[str, Any], failure: Mapping[str, Any]
) -> dict[str, Any]:
    """Project a PASS capture whose independent semantic binding failed.

    Transport/import assertions remain receipt-owned.  Semantic assertions are
    false and the row is an agent error, so diagnostic evidence is retained
    without ever allowing the profile or release gate to become green.
    """

    assertions = receipt.get("assertions")
    if (
        receipt.get("scenario_id") != "P0-06"
        or receipt.get("status") != "PASS"
        or not isinstance(assertions, Mapping)
        or not _valid_persona_finalizer_failure(failure)
    ):
        raise LiveScenarioReceiptError(
            "unfinalized persona projection is invalid"
        )
    projected_assertions = {
        **dict(assertions),
        "persona_acceptance_passed": False,
        "privacy_canary_absent": False,
    }
    projected_failure = {
        "category": "AGENT_ERROR",
        "stage_code": "PERSONA_IMPORT",
        "failure_code": "MALFORMED_EVIDENCE",
        "reproducible": True,
    }
    return {
        "status": "AGENT_ERROR",
        "assertions": projected_assertions,
        "evidence_codes": [
            code
            for assertion, code in (
                ("persona_files_archived", "PERSONA_FILES_ARCHIVED"),
                (
                    "persona_source_metadata_verified",
                    "PERSONA_SOURCE_METADATA_VERIFIED",
                ),
                ("persona_import_done", "PERSONA_IMPORT_DONE"),
            )
            if projected_assertions.get(assertion) is True
        ],
        "persona_finalizer": None,
        "failure": projected_failure,
    }


def canonical_json_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise LiveScenarioReceiptError("live receipt JSON is invalid") from None
    return hashlib.sha256(payload).hexdigest()


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _safe_id(value: object, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= 256
        and (not value or _IDENTIFIER_RE.fullmatch(value) is not None)
    )


def _number_or_none(value: object) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def latency_projection(turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Project the canonical latency summary from parent-owned turn facts."""

    rows = list(turns)

    def complete_percentile(field: str, percentile: float) -> float | None:
        values = [row.get(field) for row in rows]
        if len(values) != len(rows) or any(not _number_or_none(value) for value in values):
            return None
        if any(value is None for value in values):
            return None
        return _nearest_rank([float(value) for value in values], percentile)

    stage_values: dict[str, float | None] = {}
    missing: list[str] = []
    for stage in _TRACE_STAGES:
        values: list[float] = []
        complete = True
        for row in rows:
            stage_latency = row.get("stage_latency_ms")
            value = stage_latency.get(stage) if isinstance(stage_latency, Mapping) else None
            if not _number_or_none(value) or value is None:
                complete = False
                break
            values.append(float(value))
        if rows and complete and len(values) == len(rows):
            stage_values[stage] = _nearest_rank(values, 0.5)
        else:
            stage_values[stage] = None
            missing.append(stage)
    return {
        "sample_count": len(rows),
        "ack_p50_ms": complete_percentile("ack_latency_ms", 0.5),
        "reply_p50_ms": complete_percentile("reply_latency_ms", 0.5),
        "reply_p95_ms": complete_percentile("reply_latency_ms", 0.95),
        "stage_p50_ms": stage_values,
        "missing_stages": missing,
    }


def _valid_persona_projection(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value)
        == {
            "kind",
            "evidence_sha256",
            "job_id",
            "archive_upload_count",
            "archive_receipts_verified",
            "genesis_upload_metadata_verified",
        }
        and value.get("kind") == "persona_capture"
        and isinstance(value.get("evidence_sha256"), str)
        and _SHA256_RE.fullmatch(value["evidence_sha256"]) is not None
        and _safe_id(value.get("job_id"))
        and value.get("archive_upload_count") == 4
        and value.get("archive_receipts_verified") is True
        and value.get("genesis_upload_metadata_verified") is True
    )


def _valid_parent_persona_finalizer(
    value: object, capture_receipt: Mapping[str, Any]
) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "semantic_assertions",
        "persona_finalizer",
    }:
        return False
    semantic = value.get("semantic_assertions")
    finalizer = value.get("persona_finalizer")
    capture = capture_receipt.get("result_projection")
    request_ids = capture_receipt.get("request_ids")
    if (
        value.get("kind") != "persona_finalizer"
        or not isinstance(semantic, dict)
        or set(semantic)
        != {"persona_acceptance_passed", "privacy_canary_absent"}
        or any(type(item) is not bool for item in semantic.values())
        or not isinstance(finalizer, dict)
        or set(finalizer)
        != {
            "fixture_id",
            "evidence_sha256",
            "request_id",
            "job_id",
            "semantic_judgment_bound",
            "finalizer_ok",
            "private_evidence_deleted",
            "archive_upload_count",
            "archive_receipts_verified",
            "genesis_upload_metadata_verified",
            "privacy_violation_count",
        }
        or not isinstance(capture, Mapping)
        or not isinstance(request_ids, list)
        or len(request_ids) != 1
    ):
        return False
    privacy_count = finalizer.get("privacy_violation_count")
    return bool(
        finalizer.get("fixture_id") == "persona-import-v1"
        and finalizer.get("evidence_sha256") == capture.get("evidence_sha256")
        and finalizer.get("request_id") == request_ids[0]
        and finalizer.get("job_id") == capture.get("job_id")
        and finalizer.get("semantic_judgment_bound") is True
        and type(finalizer.get("finalizer_ok")) is bool
        and finalizer.get("private_evidence_deleted") is True
        and finalizer.get("archive_upload_count")
        == capture.get("archive_upload_count")
        and finalizer.get("archive_receipts_verified")
        == capture.get("archive_receipts_verified")
        and finalizer.get("genesis_upload_metadata_verified")
        == capture.get("genesis_upload_metadata_verified")
        and type(privacy_count) is int
        and privacy_count >= 0
        and semantic.get("persona_acceptance_passed")
        is finalizer.get("finalizer_ok")
        and semantic.get("privacy_canary_absent") is (privacy_count == 0)
    )


def _valid_trace_cleanup_projection(
    value: object, turns: Sequence[Mapping[str, Any]]
) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "latency",
        "trace",
        "cleanup",
    }:
        return False
    latency = value.get("latency")
    trace = value.get("trace")
    cleanup = value.get("cleanup")
    if value.get("kind") != "trace_cleanup" or latency != latency_projection(turns):
        return False
    if not isinstance(trace, dict) or set(trace) != {
        "enabled",
        "deploy_enabled",
        "correlated_event_count",
        "observed_event_types",
        "missing_required_event_types",
        "raw_trace_stored",
    }:
        return False
    observed = trace.get("observed_event_types")
    missing = trace.get("missing_required_event_types")
    if (
        type(trace.get("enabled")) is not bool
        or type(trace.get("deploy_enabled")) is not bool
        or type(trace.get("correlated_event_count")) is not int
        or trace["correlated_event_count"] < 0
        or not isinstance(observed, list)
        or not isinstance(missing, list)
        or any(not isinstance(item, str) for item in observed + missing)
        or any(item not in _TRACE_STAGES for item in observed + missing)
        or observed != [stage for stage in _TRACE_STAGES if stage in observed]
        or missing != [stage for stage in _TRACE_STAGES if stage in missing]
        or len(observed) != len(set(observed))
        or len(missing) != len(set(missing))
        or set(observed).intersection(missing)
        or set(observed).union(missing) != set(_TRACE_STAGES)
        or trace.get("raw_trace_stored") is not False
    ):
        return False
    if not isinstance(cleanup, dict) or set(cleanup) != {
        "attempted",
        "provider_config_deleted",
        "account_reset",
        "old_credential_rejected",
        "status",
    }:
        return False
    cleanup_values = [
        cleanup.get(field)
        for field in (
            "attempted",
            "provider_config_deleted",
            "account_reset",
            "old_credential_rejected",
        )
    ]
    return bool(
        all(type(item) is bool for item in cleanup_values)
        and cleanup.get("status") in _STATUSES
        and ((all(cleanup_values)) is (cleanup.get("status") == "PASS"))
    )


def _read_owned_private(path: Path) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise LiveScenarioReceiptError("live receipt path is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise LiveScenarioReceiptError("live receipt is unavailable") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_RECEIPT_BYTES
        ):
            raise LiveScenarioReceiptError("live receipt is unsafe")
        content = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if len(content) != before.st_size or any(
            getattr(before, field) != getattr(after, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        ):
            raise LiveScenarioReceiptError("live receipt changed while reading")
        return content
    finally:
        os.close(descriptor)


def _object_without_duplicate_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LiveScenarioReceiptError("live receipt contains duplicate keys")
        result[key] = value
    return result


def validate_receipt_object(
    receipt: object,
    *,
    run_id: str,
    profile_id: str,
    scenario_id: str | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        raise LiveScenarioReceiptError("live scenario receipt shape is invalid")
    actual_scenario = receipt.get("scenario_id")
    actual_attempt = receipt.get("attempt")
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("kind") != "live_scenario_probe"
        or receipt.get("run_id") != run_id
        or receipt.get("profile_id") != profile_id
        or actual_scenario not in LIVE_SCENARIO_IDS
        or (scenario_id is not None and actual_scenario != scenario_id)
        or type(actual_attempt) is not int
        or actual_attempt not in (1, 2)
        or (
            actual_attempt == 2
            and actual_scenario not in _RETRYABLE_SCENARIOS
        )
        or (attempt is not None and actual_attempt != attempt)
        or not _safe_id(run_id)
        or not _safe_id(profile_id)
        or not _safe_id(receipt.get("nonce"))
        or receipt.get("status") not in _STATUSES
        or not isinstance(receipt.get("failure_code"), str)
        or _FAILURE_RE.fullmatch(receipt["failure_code"]) is None
        or not isinstance(receipt.get("raw_content_stored"), bool)
        or receipt["raw_content_stored"] is not False
        or not isinstance(receipt.get("private_facts_sha256"), str)
        or _SHA256_RE.fullmatch(receipt["private_facts_sha256"]) is None
    ):
        raise LiveScenarioReceiptError("live scenario receipt identity is invalid")
    if (receipt["status"] == "PASS") is not (receipt["failure_code"] == "NONE"):
        raise LiveScenarioReceiptError("live scenario receipt status is inconsistent")
    started = _timestamp(receipt.get("started_at"))
    finished = _timestamp(receipt.get("finished_at"))
    if started is None or finished is None or finished < started:
        raise LiveScenarioReceiptError("live scenario receipt timestamps are invalid")

    assertions = receipt.get("assertions")
    if (
        not isinstance(assertions, dict)
        or set(assertions) != set(DETERMINISTIC_ASSERTIONS[actual_scenario])
        or any(type(value) is not bool for value in assertions.values())
        or receipt.get("semantic_assertions")
        != list(SEMANTIC_ASSERTIONS[actual_scenario])
    ):
        raise LiveScenarioReceiptError("live scenario assertions are invalid")
    if receipt["status"] == "PASS" and not all(assertions.values()):
        raise LiveScenarioReceiptError("passing live receipt has failed assertions")

    turns = receipt.get("turns")
    if not isinstance(turns, list) or len(turns) > _TURN_COUNTS[actual_scenario]:
        raise LiveScenarioReceiptError("live scenario turn evidence is invalid")
    if receipt["status"] == "PASS" and len(turns) != _TURN_COUNTS[actual_scenario]:
        raise LiveScenarioReceiptError("passing live receipt has incomplete turns")
    for index, turn in enumerate(turns, start=1):
        if not isinstance(turn, dict) or set(turn) != _TURN_KEYS:
            raise LiveScenarioReceiptError("live scenario turn shape is invalid")
        if (
            turn.get("turn_index") != index
            or not _safe_id(turn.get("request_id"))
            or not _safe_id(turn.get("turn_id"))
            or not _safe_id(turn.get("trace_id"))
            or (
                actual_scenario != "P0-13"
                and (
                    turn.get("turn_id") != turn.get("request_id")
                    or turn.get("trace_id") != turn.get("request_id")
                )
            )
            or not _number_or_none(turn.get("ack_latency_ms"))
            or not _number_or_none(turn.get("reply_latency_ms"))
            or not isinstance(turn.get("stage_latency_ms"), dict)
            or set(turn["stage_latency_ms"]) != set(_TRACE_STAGES)
            or any(
                not _number_or_none(value)
                for value in turn["stage_latency_ms"].values()
            )
            or type(turn.get("reply_count")) is not int
            or turn["reply_count"] < 0
            or turn.get("content_assertion_passed") not in (True, False, None)
            or any(
                type(turn.get(field)) is not bool
                for field in (
                    "fallback_detected",
                    "duplicate_detected",
                    "out_of_order_detected",
                )
            )
        ):
            raise LiveScenarioReceiptError("live scenario turn evidence is invalid")
        if (
            turn["ack_latency_ms"] is not None
            and turn["reply_latency_ms"] is not None
            and turn["reply_latency_ms"] < turn["ack_latency_ms"]
        ):
            raise LiveScenarioReceiptError("live scenario latency is inconsistent")
        if receipt["status"] == "PASS" and (
            turn["ack_latency_ms"] is None
            or turn["reply_latency_ms"] is None
            or turn["reply_count"] != 1
            or turn["fallback_detected"]
            or turn["duplicate_detected"]
            or turn["out_of_order_detected"]
            or (
                actual_scenario not in {"P0-10", "P0-11", "P0-13"}
                and turn["content_assertion_passed"] is not True
            )
            or (
                actual_scenario == "P0-13"
                and any(value is None for value in turn["stage_latency_ms"].values())
            )
        ):
            raise LiveScenarioReceiptError("passing live receipt turn is incomplete")

    request_ids = receipt.get("request_ids")
    turn_ids = receipt.get("turn_ids")
    trace_ids = receipt.get("trace_ids")
    if not all(
        isinstance(values, list)
        and len(values) == len(set(values))
        and all(_safe_id(value) for value in values)
        for values in (request_ids, turn_ids, trace_ids)
    ):
        raise LiveScenarioReceiptError("live scenario identifiers are invalid")
    if actual_scenario == "P0-13":
        if (
            len(request_ids) != 1
            or turn_ids != [turn["turn_id"] for turn in turns]
            or trace_ids != [turn["trace_id"] for turn in turns]
        ):
            raise LiveScenarioReceiptError(
                "trace-cleanup identifiers do not match projected turns"
            )
    elif turns:
        expected = [turn["request_id"] for turn in turns]
        if request_ids != expected or turn_ids != expected or trace_ids != expected:
            raise LiveScenarioReceiptError("live scenario identifiers do not match turns")
    elif actual_scenario in {"P0-08", "P0-09", "P0-10", "P0-11"}:
        if request_ids or turn_ids or trace_ids:
            raise LiveScenarioReceiptError("live scenario identifiers are inconsistent")
    elif (
        (receipt["status"] == "PASS" and len(request_ids) != 1)
        or len(request_ids) > 1
        or turn_ids
        or trace_ids
    ):
        raise LiveScenarioReceiptError("live scenario probe identifier is invalid")
    projection = receipt.get("result_projection")
    if actual_scenario == "P0-06":
        if receipt["status"] == "PASS" and not _valid_persona_projection(projection):
            raise LiveScenarioReceiptError("persona capture projection is invalid")
        if projection is not None and not _valid_persona_projection(projection):
            raise LiveScenarioReceiptError("persona capture projection is invalid")
    elif actual_scenario == "P0-13":
        if receipt["status"] == "PASS" and not _valid_trace_cleanup_projection(
            projection, turns
        ):
            raise LiveScenarioReceiptError("trace-cleanup projection is invalid")
        if projection is not None and not _valid_trace_cleanup_projection(
            projection, turns
        ):
            raise LiveScenarioReceiptError("trace-cleanup projection is invalid")
        if isinstance(projection, Mapping):
            cleanup = projection["cleanup"]
            trace = projection["trace"]
            latency = projection["latency"]
            expected_trace_complete = not latency["missing_stages"]
            expected_correlation = bool(
                len(turns) == 15 and len(set(trace_ids)) == 15
            )
            expected_cleanup = all(
                cleanup[field]
                for field in (
                    "attempted",
                    "provider_config_deleted",
                    "account_reset",
                    "old_credential_rejected",
                )
            )
            if (
                assertions.get("trace_stages_complete") is not expected_trace_complete
                or assertions.get("trace_correlation_confirmed")
                is not expected_correlation
                or assertions.get("latency_attributed") is not expected_trace_complete
                or assertions.get("cleanup_confirmed") is not expected_cleanup
                or trace.get("missing_required_event_types")
                != latency.get("missing_stages")
            ):
                raise LiveScenarioReceiptError(
                    "trace-cleanup projection is inconsistent"
                )
    elif projection is not None:
        raise LiveScenarioReceiptError("unrelated live result projection is present")
    return dict(receipt)


def validate_aggregate_object(
    payload: object,
    *,
    run_id: str,
    profile_id: str,
    allow_failed_persona: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "kind",
        "run_id",
        "profile_id",
        "receipts",
        "persona_finalizer",
    }:
        raise LiveScenarioReceiptError("live receipt aggregate shape is invalid")
    if (
        payload.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or payload.get("kind") != "live_scenario_receipt_set"
        or payload.get("run_id") != run_id
        or payload.get("profile_id") != profile_id
        or not isinstance(payload.get("receipts"), list)
    ):
        raise LiveScenarioReceiptError("live receipt aggregate identity is invalid")
    receipts = payload["receipts"]
    validated: list[dict[str, Any]] = []
    cursor = 0
    for scenario_id in LIVE_SCENARIO_IDS:
        attempts: list[int] = []
        while cursor < len(receipts):
            candidate = receipts[cursor]
            if not isinstance(candidate, dict) or candidate.get("scenario_id") != scenario_id:
                break
            row = validate_receipt_object(
                candidate,
                run_id=run_id,
                profile_id=profile_id,
                scenario_id=scenario_id,
            )
            attempts.append(row["attempt"])
            validated.append(row)
            cursor += 1
        if attempts not in ([1], [1, 2]):
            raise LiveScenarioReceiptError("live receipt attempts are incomplete")
        scenario_rows = validated[-len(attempts) :]
        if len(attempts) == 2 and (
            scenario_id not in _RETRYABLE_SCENARIOS
            or [row["status"] for row in scenario_rows] != ["AGENT_ERROR", "PASS"]
            or scenario_rows[0].get("failure_code")
            not in {"CHAT_TIMEOUT", "MISSING_REPLY"}
        ):
            raise LiveScenarioReceiptError(
                "live receipt retry is not a bounded transient retry"
            )
    if cursor != len(receipts):
        raise LiveScenarioReceiptError("live receipt scenario order is invalid")
    capture_rows = [
        row for row in validated if row.get("scenario_id") == "P0-06"
    ]
    failed_persona = bool(
        len(capture_rows) == 1
        and capture_rows[0].get("status") != "PASS"
        and payload.get("persona_finalizer") is None
    )
    failed_persona_finalizer = bool(
        len(capture_rows) == 1
        and capture_rows[0].get("status") == "PASS"
        and _valid_persona_finalizer_failure(payload.get("persona_finalizer"))
    )
    if len(capture_rows) != 1 or not (
        (
            allow_failed_persona
            and (failed_persona or failed_persona_finalizer)
        )
        or _valid_parent_persona_finalizer(
            payload.get("persona_finalizer"), capture_rows[0]
        )
    ):
        raise LiveScenarioReceiptError(
            "parent persona finalizer is missing or inconsistent"
        )
    result = dict(payload)
    result["receipts"] = validated
    return result


def validate_live_scenario_receipts(
    path: Path,
    *,
    run_id: str,
    profile_id: str,
    allow_failed_persona: bool = False,
) -> tuple[dict[str, Any], str]:
    try:
        payload = json.loads(
            _read_owned_private(path),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LiveScenarioReceiptError("live receipt JSON is invalid") from None
    result = validate_aggregate_object(
        payload,
        run_id=run_id,
        profile_id=profile_id,
        allow_failed_persona=allow_failed_persona,
    )
    return result, canonical_json_sha256(result)


def validate_result_binding(
    profile_result: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    *,
    allow_failed_persona: bool = False,
) -> None:
    """Bind agent-authored projections to immutable transport observations.

    The profile agent retains authority only for the explicitly listed semantic
    assertions in P0-10/P0-11.  It cannot invent calls, identifiers, latencies,
    deterministic assertions, or turn ordering, and cannot turn a parent failure
    into PASS.
    """

    scenarios = profile_result.get("scenarios")
    turns = profile_result.get("turns")
    receipts = aggregate.get("receipts")
    parent_persona = aggregate.get("persona_finalizer")
    if (
        not isinstance(scenarios, list)
        or not isinstance(turns, list)
        or not isinstance(receipts, list)
        or not (
            isinstance(parent_persona, Mapping)
            or (allow_failed_persona and parent_persona is None)
        )
    ):
        raise LiveScenarioReceiptError("live receipts do not match worker result")
    by_scenario: dict[str, list[Mapping[str, Any]]] = {
        scenario_id: [] for scenario_id in LIVE_SCENARIO_IDS
    }
    for receipt in receipts:
        if not isinstance(receipt, Mapping) or receipt.get("scenario_id") not in by_scenario:
            raise LiveScenarioReceiptError("live receipts do not match worker result")
        by_scenario[str(receipt["scenario_id"])].append(receipt)
    result_scenarios = {
        row.get("scenario_id"): row
        for row in scenarios
        if isinstance(row, Mapping)
    }
    if len(result_scenarios) != len(scenarios):
        raise LiveScenarioReceiptError("live receipts do not match worker result")

    expected_turns: list[tuple[str, Mapping[str, Any]]] = []
    trace_cleanup_receipt: Mapping[str, Any] | None = None
    for scenario_id in LIVE_SCENARIO_IDS:
        scenario = result_scenarios.get(scenario_id)
        rows = by_scenario[scenario_id]
        if not isinstance(scenario, Mapping) or not rows:
            raise LiveScenarioReceiptError("live receipts do not match worker result")
        if (
            scenario.get("attempts") != len(rows)
            or scenario.get("started_at") != rows[0].get("started_at")
            or scenario.get("finished_at") != rows[-1].get("finished_at")
        ):
            raise LiveScenarioReceiptError("live receipts do not match worker result")
        attempt_results = scenario.get("attempt_results")
        if not isinstance(attempt_results, list) or len(attempt_results) != len(rows):
            raise LiveScenarioReceiptError("live receipts do not match worker result")
        for index, (attempt_result, receipt) in enumerate(
            zip(attempt_results, rows, strict=True), start=1
        ):
            if (
                not isinstance(attempt_result, Mapping)
                or attempt_result.get("attempt") != index
                or (
                    receipt.get("status") != "PASS"
                    and attempt_result.get("status") != receipt.get("status")
                )
                or (
                    scenario_id == "P0-13"
                    and attempt_result.get("status") != receipt.get("status")
                )
            ):
                raise LiveScenarioReceiptError("worker result is greener than live receipt")
        bounded_retry = [row.get("status") for row in rows] == [
            "AGENT_ERROR",
            "PASS",
        ]
        if (
            any(row.get("status") != "PASS" for row in rows)
            and not bounded_retry
            and scenario.get("status") == "PASS"
        ):
            raise LiveScenarioReceiptError("worker result is greener than live receipt")
        if scenario_id == "P0-13" and scenario.get("status") != rows[-1].get(
            "status"
        ):
            raise LiveScenarioReceiptError("trace-cleanup status does not match receipt")
        assertions = scenario.get("assertions")
        if not isinstance(assertions, Mapping):
            raise LiveScenarioReceiptError("live receipts do not match worker result")
        for key, value in rows[-1]["assertions"].items():
            if key in assertions and assertions.get(key) is not value:
                raise LiveScenarioReceiptError("live assertion does not match worker result")
        for field in ("request_ids", "turn_ids", "trace_ids"):
            expected_ids = [
                value for receipt in rows for value in receipt.get(field, [])
            ]
            if scenario.get(field) != expected_ids:
                raise LiveScenarioReceiptError("live identifiers do not match worker result")
        if scenario_id == "P0-06":
            if (
                allow_failed_persona
                and parent_persona is None
                and rows[-1].get("status") != "PASS"
            ):
                expected = failed_persona_result_projection(rows[-1])
                attempt_failure = (
                    attempt_results[0].get("failure")
                    if isinstance(attempt_results[0], Mapping)
                    else None
                )
                if (
                    scenario.get("status") != expected["status"]
                    or attempt_results[0].get("status")
                    != expected["status"]
                    or scenario.get("assertions") != expected["assertions"]
                    or scenario.get("persona_finalizer") is not None
                    or scenario.get("failure") != expected["failure"]
                    or attempt_failure != expected["failure"]
                    or scenario.get("evidence_codes")
                    != expected["evidence_codes"]
                    or profile_result.get("status") == "PASS"
                ):
                    raise LiveScenarioReceiptError(
                        "failed persona capture does not match worker result"
                    )
                continue
            if (
                allow_failed_persona
                and rows[-1].get("status") == "PASS"
                and _valid_persona_finalizer_failure(parent_persona)
            ):
                expected = unfinalized_persona_result_projection(
                    rows[-1], parent_persona
                )
                attempt_failure = (
                    attempt_results[0].get("failure")
                    if isinstance(attempt_results[0], Mapping)
                    else None
                )
                if (
                    scenario.get("status") != expected["status"]
                    or attempt_results[0].get("status")
                    != expected["status"]
                    or scenario.get("assertions") != expected["assertions"]
                    or scenario.get("persona_finalizer") is not None
                    or scenario.get("failure") != expected["failure"]
                    or attempt_failure != expected["failure"]
                    or scenario.get("evidence_codes")
                    != expected["evidence_codes"]
                    or profile_result.get("status") == "PASS"
                ):
                    raise LiveScenarioReceiptError(
                        "unfinalized persona review does not match worker result"
                    )
                continue
            if not isinstance(parent_persona, Mapping):
                raise LiveScenarioReceiptError(
                    "parent persona finalizer does not match worker result"
                )
            semantic = parent_persona.get("semantic_assertions")
            finalizer = parent_persona.get("persona_finalizer")
            if (
                not _valid_parent_persona_finalizer(parent_persona, rows[-1])
                or not isinstance(semantic, Mapping)
                or not isinstance(finalizer, Mapping)
                or scenario.get("persona_finalizer") != finalizer
                or any(
                    assertions.get(key) is not value
                    for key, value in semantic.items()
                )
            ):
                raise LiveScenarioReceiptError(
                    "parent persona finalizer does not match worker result"
                )
            privacy_ok = semantic.get("privacy_canary_absent") is True
            acceptance_ok = semantic.get("persona_acceptance_passed") is True
            if privacy_ok and acceptance_ok:
                expected_status = "PASS"
                expected_failure = None
            elif not privacy_ok:
                expected_status = "SECURITY_FAIL"
                expected_failure = {
                    "category": "SECURITY_FAIL",
                    "stage_code": "PERSONA_IMPORT",
                    "failure_code": "REDACTION_ASSERTION_FAILED",
                    "reproducible": True,
                }
            else:
                expected_status = "PRODUCT_FAIL"
                expected_failure = {
                    "category": "PRODUCT_FAIL",
                    "stage_code": "PERSONA_IMPORT",
                    "failure_code": "PERSONA_ACCEPTANCE_FAILED",
                    "reproducible": True,
                }
            expected_codes = [
                code
                for assertion, code in (
                    ("persona_files_archived", "PERSONA_FILES_ARCHIVED"),
                    (
                        "persona_source_metadata_verified",
                        "PERSONA_SOURCE_METADATA_VERIFIED",
                    ),
                    ("persona_import_done", "PERSONA_IMPORT_DONE"),
                    (
                        "persona_acceptance_passed",
                        "PERSONA_ACCEPTANCE_PASSED",
                    ),
                    ("privacy_canary_absent", "PRIVACY_CANARY_ABSENT"),
                )
                if assertions.get(assertion) is True
            ]
            attempt_failure = (
                attempt_results[0].get("failure")
                if isinstance(attempt_results[0], Mapping)
                else None
            )
            if (
                scenario.get("status") != expected_status
                or attempt_results[0].get("status") != expected_status
                or scenario.get("failure") != expected_failure
                or attempt_failure != expected_failure
                or scenario.get("evidence_codes") != expected_codes
                or (
                    expected_status != "PASS"
                    and profile_result.get("status") == "PASS"
                )
            ):
                raise LiveScenarioReceiptError(
                    "parent persona verdict does not match worker result"
                )
        if scenario_id == "P0-13":
            trace_cleanup_receipt = rows[-1]
        else:
            expected_turns.extend(
                (scenario_id, turn)
                for receipt in rows
                for turn in receipt.get("turns", [])
            )

    actual_turns = [
        row
        for row in turns
        if isinstance(row, Mapping) and row.get("scenario_id") in LIVE_SCENARIO_IDS
    ]
    if len(actual_turns) != len(expected_turns):
        raise LiveScenarioReceiptError("live turns do not match worker result")
    for actual, (expected_scenario, expected) in zip(
        actual_turns, expected_turns, strict=True
    ):
        if (
            actual.get("scenario_id") != expected_scenario
            or actual.get("turn_index") != expected.get("turn_index")
            or actual.get("request_id") != expected.get("request_id")
            or actual.get("turn_id") != expected.get("turn_id")
            or actual.get("trace_id") != expected.get("trace_id")
            or actual.get("ack_latency_ms") != expected.get("ack_latency_ms")
            or actual.get("reply_latency_ms") != expected.get("reply_latency_ms")
            or actual.get("reply_count") != expected.get("reply_count")
            or actual.get("fallback_detected") != expected.get("fallback_detected")
            or actual.get("duplicate_detected") != expected.get("duplicate_detected")
            or actual.get("out_of_order_detected")
            != expected.get("out_of_order_detected")
            or (
                expected.get("content_assertion_passed") is not None
                and actual.get("content_assertion_passed")
                != expected.get("content_assertion_passed")
            )
        ):
            raise LiveScenarioReceiptError("live turns do not match worker result")

    if trace_cleanup_receipt is None:
        raise LiveScenarioReceiptError("trace-cleanup receipt is missing")
    projection = trace_cleanup_receipt.get("result_projection")
    projected_turns = trace_cleanup_receipt.get("turns")
    if not isinstance(projection, Mapping) or not isinstance(projected_turns, list):
        raise LiveScenarioReceiptError("trace-cleanup projection is missing")
    actual_by_trace = {
        row.get("trace_id"): row
        for row in turns
        if isinstance(row, Mapping) and _safe_id(row.get("trace_id"))
    }
    if (
        len(actual_by_trace) != len(turns)
        or len(projected_turns) != len(actual_by_trace)
    ):
        raise LiveScenarioReceiptError("trace-cleanup turns do not match worker result")
    for expected in projected_turns:
        actual = actual_by_trace.get(expected.get("trace_id"))
        if not isinstance(actual, Mapping) or any(
            actual.get(field) != expected.get(field)
            for field in (
                "request_id",
                "turn_id",
                "trace_id",
                "ack_latency_ms",
                "reply_latency_ms",
                "stage_latency_ms",
                "reply_count",
                "fallback_detected",
                "duplicate_detected",
                "out_of_order_detected",
            )
        ):
            raise LiveScenarioReceiptError(
                "trace-cleanup turns do not match worker result"
            )
        if (
            expected.get("content_assertion_passed") is not None
            and actual.get("content_assertion_passed")
            != expected.get("content_assertion_passed")
        ):
            raise LiveScenarioReceiptError(
                "trace-cleanup turns do not match worker result"
            )
    if (
        profile_result.get("latency") != projection.get("latency")
        or profile_result.get("trace") != projection.get("trace")
        or profile_result.get("cleanup") != projection.get("cleanup")
    ):
        raise LiveScenarioReceiptError(
            "trace-cleanup profile projection does not match worker result"
        )
    p0_13 = result_scenarios.get("P0-13")
    assertions = p0_13.get("assertions") if isinstance(p0_13, Mapping) else None
    expected_codes = [
        code
        for assertion, code in (
            ("trace_correlation_confirmed", "TRACE_CORRELATION_CONFIRMED"),
            ("latency_attributed", "LATENCY_ATTRIBUTED"),
            ("cleanup_confirmed", "CLEANUP_CONFIRMED"),
        )
        if isinstance(assertions, Mapping) and assertions.get(assertion) is True
    ]
    if not isinstance(p0_13, Mapping) or p0_13.get("evidence_codes") != expected_codes:
        raise LiveScenarioReceiptError(
            "trace-cleanup evidence codes do not match receipt"
        )
    receipt_status = trace_cleanup_receipt.get("status")
    receipt_failure = trace_cleanup_receipt.get("failure_code")
    expected_failure = None if receipt_status == "PASS" else receipt_failure
    expected_stage = (
        "CLEANUP"
        if receipt_failure == "PRECONDITION_MISSING"
        else "TRACE_LATENCY_CLEANUP"
    )
    scenario_failure = p0_13.get("failure")
    attempts = p0_13.get("attempt_results")
    if expected_failure is None:
        failure_matches = scenario_failure is None
        attempt_failure_matches = bool(
            isinstance(attempts, list)
            and len(attempts) == 1
            and isinstance(attempts[0], Mapping)
            and attempts[0].get("failure") is None
        )
    else:
        failure_matches = bool(
            isinstance(scenario_failure, Mapping)
            and scenario_failure.get("category") == receipt_status
            and scenario_failure.get("stage_code") == expected_stage
            and scenario_failure.get("failure_code") == expected_failure
        )
        attempt_failure = (
            attempts[0].get("failure")
            if isinstance(attempts, list)
            and len(attempts) == 1
            and isinstance(attempts[0], Mapping)
            else None
        )
        attempt_failure_matches = bool(
            isinstance(attempt_failure, Mapping)
            and attempt_failure.get("category") == receipt_status
            and attempt_failure.get("stage_code") == expected_stage
            and attempt_failure.get("failure_code") == expected_failure
        )
    if not failure_matches or not attempt_failure_matches:
        raise LiveScenarioReceiptError(
            "trace-cleanup failure does not match receipt"
        )
