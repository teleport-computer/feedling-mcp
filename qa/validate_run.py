#!/usr/bin/env python3
"""Deterministic release gate for agent-produced API-key E2E artifacts.

The qualification agent is the semantic test driver.  This module is the small,
mechanical trust boundary after it: untrusted JSON must satisfy the checked-in
schema and the locked coverage contract before CI can report a green gate.

Error messages intentionally contain only fixed field names and locked IDs.  In
particular, JSON values and filesystem paths supplied by the result are never
echoed because they may contain credentials or model output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from qa.orchestration_contract import PROFILE_AGENT_TYPES, PROFILE_IDS
except ModuleNotFoundError:  # Direct `python qa/validate_run.py` execution.
    from orchestration_contract import PROFILE_AGENT_TYPES, PROFILE_IDS

LOCKED_PROFILE_IDS = PROFILE_IDS
LOCKED_SCENARIO_IDS = tuple(f"P0-{index:02d}" for index in range(1, 14))
MEMORY_CONTRACT_PROFILE_ID = "memory-contract"
MEMORY_CONTRACT_RECEIPT = "memory-contract.json"
MEMORY_CONTRACT_SCHEMA = "qa/schemas/memory-contract-receipt.schema.json"
MEMORY_MIGRATION_OPTIONAL = "allow_not_exercised_when_disabled"
MEMORY_MIGRATION_REQUIRED = "required"
_MEMORY_CORE_CHECKS = (
    "fresh_empty_recall",
    "encrypted_v1_index_fetch",
    "quiet_window_capture_write",
    "route_chat_message_trace",
    "capture_noop_disposable_chitchat",
    "duplicate_fact_no_growth",
    "local_only_exclusion",
    "supersede_visibility",
)
_MEMORY_MIGRATION_CHECKS = (
    "legacy_migration_stable_id",
    "stale_cas_preserves_concurrent_updates",
)
_MEMORY_CONTRACT_LOCK = {
    "profile_id": MEMORY_CONTRACT_PROFILE_ID,
    "receipt_schema": MEMORY_CONTRACT_SCHEMA,
    "always_required_checks": list(_MEMORY_CORE_CHECKS),
    "migration_checks": list(_MEMORY_MIGRATION_CHECKS),
    "migration_policy": MEMORY_MIGRATION_OPTIONAL,
}
REQUIRED_TRACE_STAGES = (
    "routing",
    "queue",
    "provider",
    "persistence",
    "delivery",
)
BASELINE_RUNTIME = "deployed_current"
EXPECTED_RUNTIME = "hosted_resident"
PASS = "PASS"
PERSONA_FIXTURE_ID = "persona-import-v1"
_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_EVIDENCE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_PATH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ARTIFACT_FIELDS = {
    "run_result": "file",
    "matrix_markdown": "file",
    "latency_csv": "file",
    "junit_xml": "file",
    "profiles_directory": "directory",
}
_NONPASS_SUMMARY_FIELDS = (
    "product_fail",
    "blocked_credential",
    "blocked_evidence",
    "blocked_deployment",
    "agent_error",
    "security_fail",
)
_REDACTION_TRUE_FIELDS = (
    "provider_keys_omitted",
    "feedling_api_keys_omitted",
    "content_private_keys_omitted",
    "raw_chat_omitted",
    "raw_trace_omitted",
    "raw_reasoning_omitted",
    "synthetic_users_only",
)
_PROFILE_METADATA = {
    "official-deepseek": ("official", "deepseek", "deepseek"),
    "official-anthropic": ("official", "claude", "anthropic"),
    "official-openai": ("official", "openai", "openai"),
    "official-gemini": ("official", "gemini", "gemini"),
    "openrouter-claude": ("openrouter", "claude", "openrouter"),
    "openrouter-openai": ("openrouter", "openai", "openrouter"),
    "openrouter-glm": ("openrouter", "glm", "openrouter"),
    "relay-kongbeiqie": ("relay", "claude", "openai_compatible"),
}
_PROFILE_SECRET_AND_MODEL_ENVS = {
    "official-deepseek": ("QA_DEEPSEEK_API_KEY", "QA_DEEPSEEK_MODEL"),
    "official-anthropic": ("QA_ANTHROPIC_API_KEY", "QA_ANTHROPIC_MODEL"),
    "official-openai": ("QA_OPENAI_PROVIDER_API_KEY", "QA_OPENAI_MODEL"),
    "official-gemini": ("QA_GEMINI_API_KEY", "QA_GEMINI_MODEL"),
    "openrouter-claude": ("QA_OPENROUTER_API_KEY", "QA_OPENROUTER_CLAUDE_MODEL"),
    "openrouter-openai": ("QA_OPENROUTER_API_KEY", "QA_OPENROUTER_OPENAI_MODEL"),
    "openrouter-glm": ("QA_OPENROUTER_API_KEY", "QA_OPENROUTER_GLM_MODEL"),
    "relay-kongbeiqie": ("QA_KONGBEIQIE_API_KEY", "QA_KONGBEIQIE_MODEL"),
}
_PROFILE_ALLOWED_MODEL_REGEXES = {
    "official-deepseek": r"^deepseek-[a-z0-9][a-z0-9._-]*$",
    "official-anthropic": r"^claude-[a-z0-9][a-z0-9._-]*$",
    "official-openai": r"^(?:gpt-[a-z0-9][a-z0-9._-]*|o[1-9][a-z0-9._-]*)$",
    "official-gemini": r"^gemini-(?:2\.5|3\.5)-[a-z0-9][a-z0-9._-]*$",
    "openrouter-claude": r"^anthropic/claude-[a-z0-9][a-z0-9._:-]*$",
    "openrouter-openai": (r"^openai/(?:gpt-[a-z0-9][a-z0-9._:-]*|o[a-z0-9._:-]*)$"),
    "openrouter-glm": r"^(?:z-ai|thudm)/glm-[a-z0-9][a-z0-9._:-]*$",
    "relay-kongbeiqie": (r"^(?:\[[^\r\n\]|`]{1,32}\])?claude-[a-z0-9][a-z0-9._-]*$"),
}
_PROFILE_CONFIGURED_BASE_URLS = {
    "official-deepseek": "https://api.deepseek.com",
    "official-anthropic": "https://api.anthropic.com/v1",
    "official-openai": "https://api.openai.com/v1",
    "official-gemini": "https://generativelanguage.googleapis.com/v1beta",
    "openrouter-claude": "https://openrouter.ai/api/v1",
    "openrouter-openai": "https://openrouter.ai/api/v1",
    "openrouter-glm": "https://openrouter.ai/api/v1",
    "relay-kongbeiqie": "https://xn--vduyey89e.com/v1",
}
_RELAY_BASE_URL_ENVS = {
    "relay-kongbeiqie": "QA_KONGBEIQIE_BASE_URL",
}
_SCENARIO_CONTRACTS: dict[str, dict[str, Any]] = {
    "P0-01": {
        "required_assertions": [
            "target_is_test",
            "deployed_endpoint_reachable",
            "provisioning_receipts_confirmed",
            "agent_environment_sanitized",
            "contract_inputs_readable",
            "credentials_omitted",
        ],
        "required_evidence_codes": [
            "TARGET_TEST_CONFIRMED",
            "DEPLOYED_ENDPOINT_REACHABLE",
            "PROVISIONING_RECEIPTS_CONFIRMED",
            "AGENT_ENVIRONMENT_SANITIZED",
            "CONTRACT_INPUTS_READABLE",
            "CREDENTIAL_OMITTED",
        ],
        "minimum_id_counts": {"request_ids": 0, "turn_ids": 0, "trace_ids": 0},
        "required_turn_count": 0,
    },
    "P0-02": {
        "required_assertions": [
            "synthetic_account_is_fresh",
            "whoami_matches",
            "trace_cleared",
        ],
        "required_evidence_codes": [
            "SYNTHETIC_ACCOUNT_FRESH",
            "WHOAMI_MATCHED",
            "TRACE_CLEARED",
        ],
        "minimum_id_counts": {"request_ids": 1, "turn_ids": 0, "trace_ids": 0},
        "required_turn_count": 0,
    },
    "P0-03": {
        "required_assertions": [
            "invalid_key_rejected",
            "invalid_key_not_echoed",
            "hosted_chat_not_started",
        ],
        "required_evidence_codes": [
            "INVALID_KEY_REJECTED",
            "INVALID_KEY_NOT_ECHOED",
            "HOSTED_CHAT_NOT_STARTED",
        ],
        "minimum_id_counts": {"request_ids": 1, "turn_ids": 0, "trace_ids": 0},
        "required_turn_count": 0,
    },
    "P0-04": {
        "required_assertions": [
            "valid_key_accepted",
            "provider_config_matches",
            "credential_omitted",
        ],
        "required_evidence_codes": [
            "VALID_KEY_ACCEPTED",
            "PROVIDER_CONFIG_MATCHED",
            "CREDENTIAL_OMITTED",
        ],
        "minimum_id_counts": {"request_ids": 1, "turn_ids": 0, "trace_ids": 0},
        "required_turn_count": 0,
    },
    "P0-05": {
        "required_assertions": [
            "runtime_status_readback_succeeds",
            "runtime_configured",
            "runtime_metadata_recorded",
        ],
        "required_evidence_codes": [
            "RUNTIME_STATUS_READBACK_SUCCEEDED",
            "RUNTIME_CONFIGURED",
            "RUNTIME_METADATA_RECORDED",
        ],
        "minimum_id_counts": {"request_ids": 1, "turn_ids": 0, "trace_ids": 0},
        "required_turn_count": 0,
    },
    "P0-06": {
        "required_assertions": [
            "persona_files_archived",
            "persona_source_metadata_verified",
            "persona_import_done",
            "persona_acceptance_passed",
            "privacy_canary_absent",
        ],
        "required_evidence_codes": [
            "PERSONA_FILES_ARCHIVED",
            "PERSONA_SOURCE_METADATA_VERIFIED",
            "PERSONA_IMPORT_DONE",
            "PERSONA_ACCEPTANCE_PASSED",
            "PRIVACY_CANARY_ABSENT",
        ],
        "minimum_id_counts": {"request_ids": 1, "turn_ids": 0, "trace_ids": 0},
        "required_turn_count": 0,
    },
    "P0-07": {
        "required_assertions": [
            "driver_enabled",
            "chat_loop_verified",
            "runtime_status_readback_succeeds",
            "no_orphan_turn",
        ],
        "required_evidence_codes": [
            "DRIVER_ENABLED",
            "CHAT_LOOP_VERIFIED",
            "RUNTIME_STATUS_READBACK_SUCCEEDED",
            "NO_ORPHAN_TURN",
        ],
        "minimum_id_counts": {"request_ids": 1, "turn_ids": 0, "trace_ids": 0},
        "required_turn_count": 0,
    },
    "P0-08": {
        "required_assertions": [
            "async_ack_received",
            "exact_reply_correlated",
            "nonce_echo_confirmed",
            "fallback_absent",
            "latency_recorded",
        ],
        "required_evidence_codes": [
            "ASYNC_ACK_RECEIVED",
            "EXACT_REPLY_CORRELATED",
            "NONCE_ECHO_CONFIRMED",
            "LATENCY_ATTRIBUTED",
        ],
        "minimum_id_counts": {"request_ids": 1, "turn_ids": 1, "trace_ids": 1},
        "required_turn_count": 1,
    },
    "P0-09": {
        "required_assertions": [
            "ten_turns_ordered",
            "exact_replies_correlated",
            "memory_recall_confirmed",
            "no_orphan_turn",
        ],
        "required_evidence_codes": [
            "TEN_TURNS_ORDERED",
            "EXACT_REPLY_CORRELATED",
            "MEMORY_RECALL_CONFIRMED",
            "NO_ORPHAN_TURN",
        ],
        "minimum_id_counts": {"request_ids": 10, "turn_ids": 10, "trace_ids": 10},
        "required_turn_count": 10,
    },
    "P0-10": {
        "required_assertions": [
            "imported_memory_recalled",
            "persona_consistency_confirmed",
            "contradictory_facts_absent",
        ],
        "required_evidence_codes": [
            "MEMORY_RECALL_CONFIRMED",
            "PERSONA_CONSISTENCY_CONFIRMED",
            "EXACT_REPLY_CORRELATED",
        ],
        "minimum_id_counts": {"request_ids": 2, "turn_ids": 2, "trace_ids": 2},
        "required_turn_count": 2,
    },
    "P0-11": {
        "required_assertions": [
            "agent_identity_confirmed",
            "model_route_confirmed",
            "provider_config_matches",
            "trace_route_correlated",
        ],
        "required_evidence_codes": [
            "AGENT_IDENTITY_CONFIRMED",
            "MODEL_ROUTE_CONFIRMED",
            "PROVIDER_CONFIG_MATCHED",
            "TRACE_CORRELATION_CONFIRMED",
        ],
        "minimum_id_counts": {"request_ids": 1, "turn_ids": 1, "trace_ids": 1},
        "required_turn_count": 1,
    },
    "P0-12": {
        "required_assertions": [
            "objective_answer_correct",
            "reasoning_capability_enabled",
            "reasoning_requested_effort_medium",
            "reasoning_configured_effort_medium",
            "reasoning_effective_effort_not_attested",
            "reasoning_event_observed",
            "reasoning_metadata_present",
            "reasoning_tokens_present",
            "user_disclosure_present",
            "raw_private_reasoning_omitted",
        ],
        "required_evidence_codes": [
            "REASONING_CAPABILITY_CONFIRMED",
            "REASONING_CONFIGURATION_CONFIRMED",
            "REASONING_EFFECTIVE_EFFORT_UNATTESTED",
            "REASONING_EVENT_CONFIRMED",
            "REASONING_METADATA_CONFIRMED",
            "EXPLICIT_REASONING_TOKEN_COUNT_CONFIRMED",
            "DISCLOSURE_PRESENT",
            "EXACT_REPLY_CORRELATED",
        ],
        "minimum_id_counts": {"request_ids": 1, "turn_ids": 1, "trace_ids": 1},
        "required_turn_count": 1,
    },
    "P0-13": {
        "required_assertions": [
            "trace_stages_complete",
            "trace_correlation_confirmed",
            "latency_attributed",
            "cleanup_confirmed",
        ],
        "required_evidence_codes": [
            "TRACE_CORRELATION_CONFIRMED",
            "LATENCY_ATTRIBUTED",
            "CLEANUP_CONFIRMED",
        ],
        "minimum_id_counts": {"request_ids": 1, "turn_ids": 15, "trace_ids": 15},
        "required_turn_count": 0,
    },
}
_TRACE_LATENCY_CONTRACT = {
    "required_stages": list(REQUIRED_TRACE_STAGES),
    "pass_requires_numeric_stage_latency": True,
    "pass_requires_per_turn_stage_latency": True,
    "pass_requires_all_turns_correlated": True,
    "profile_summary_percentile_method": "nearest_rank",
}
_EXECUTION_CONTRACT = {
    "required_profile_count": 8,
    "supervisor_count": 1,
    "profile_worker_assignment_count": 8,
    "allow_profile_skip": False,
    "max_profile_concurrency": 3,
    "max_attempts_per_scenario": 2,
    "profile_timeout_seconds": 2400,
    "chat_reply_timeout_seconds": 120,
    "distillation_timeout_seconds": 900,
}
_COVERAGE_TARGET = {
    "environment": "test",
    "base_url_env": "QA_FEEDLING_BASE_URL",
    "expected_deployment_sha_env": "QA_EXPECTED_DEPLOYMENT_SHA",
    "expected_runtime": EXPECTED_RUNTIME,
    "runtime_admin_token_env": "QA_TEST_ADMIN_TOKEN",
}
_REASONING_CONTRACT = {
    "capability_enabled_required_when_expected": True,
    "requested_effort_required_when_expected": "medium",
    "configured_effort_required_when_expected": "medium",
    "effective_effort_required_when_expected": "unknown",
    "effective_effort_attested_required_when_expected": False,
    "positive_reasoning_event_count_required_when_expected": True,
    "provider_metadata_required_when_expected": True,
    "provider_token_metadata_required_when_expected": True,
    "user_visible_disclosure_required_when_expected": True,
    "raw_private_chain_of_thought_forbidden": True,
}
_ARTIFACT_CONTRACT = {
    "schema": "qa/schemas/run-result.schema.json",
    "required": [
        "run-result.json",
        MEMORY_CONTRACT_RECEIPT,
        "matrix.md",
        "latency.csv",
        "junit.xml",
        "profiles",
    ],
}
_TRANSIENT_RETRY_STAGES = frozenset(
    (
        "PERSONA_IMPORT",
        "BASIC_CHAT",
        "RELIABILITY_CHAT",
        "MEMORY_PERSONA",
        "IDENTITY",
        "REASONING",
    )
)
_TRANSIENT_RETRY_FAILURE_CODES = frozenset(("CHAT_TIMEOUT", "MISSING_REPLY"))
_PASS_RETRY_DIAGNOSTICS = frozenset(
    ("RETRY_USED", "TRANSIENT_PROVIDER_ERROR", "TRANSIENT_TRANSPORT_ERROR")
)


class GateInputError(RuntimeError):
    """A fixed, sanitized input failure safe to print in CI."""


def _read_json(path: Path, label: str) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise GateInputError(f"{label} is unreadable") from None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        raise GateInputError(f"{label} is not valid JSON") from None


def _read_private_json(path: Path, label: str) -> Any:
    try:
        metadata = path.lstat()
    except OSError:
        raise GateInputError(f"{label} is unreadable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
    ):
        raise GateInputError(f"{label} is unreadable")
    return _read_json(path, label)


def _read_private_manifest(path: Path) -> Any:
    return _read_private_json(path, "trusted provisioning manifest")


def _duplicates(values: Iterable[str]) -> set[str]:
    return {value for value, count in Counter(values).items() if count > 1}


def _fixed_missing(expected: Sequence[str], actual: set[str]) -> str:
    """List missing locked IDs only; never echo unexpected result-controlled IDs."""
    missing = [item for item in expected if item not in actual]
    return ",".join(missing) if missing else "none"


def _schema_path(error: Any) -> str:
    parts = ["$"]
    for token in error.absolute_path:
        if isinstance(token, int):
            parts.append(f"[{token}]")
        else:
            safe = str(token)
            parts.append(f".{safe}" if _SAFE_PATH_TOKEN_RE.fullmatch(safe) else ".<?>")
    return "".join(parts)


def _schema_errors(schema: Any, result: Any) -> list[str]:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from jsonschema.exceptions import SchemaError
    except ImportError:
        raise GateInputError(
            "JSON Schema validator dependency is unavailable"
        ) from None

    if not isinstance(schema, dict):
        raise GateInputError("result schema must be a JSON object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise GateInputError("result schema is invalid") from None

    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    issues: list[str] = []
    seen: set[tuple[str, str]] = set()
    try:
        errors = validator.iter_errors(result)
        for error in errors:
            path = _schema_path(error)
            rule = str(error.validator or "schema")
            if not _SAFE_PATH_TOKEN_RE.fullmatch(rule):
                rule = "schema"
            key = (path, rule)
            if key in seen:
                continue
            seen.add(key)
            issues.append(f"result violates JSON Schema at {path} (rule={rule})")
            if len(issues) == 20:
                issues.append("additional JSON Schema violations were suppressed")
                break
    except Exception:
        raise GateInputError(
            "result schema validation could not be completed"
        ) from None
    return issues


def _read_memory_contract_receipt(artifact_root: Path) -> Any:
    """Read the one fixed public receipt without following a link or large file."""

    if artifact_root.is_symlink():
        raise GateInputError("memory contract receipt is unreadable")
    try:
        root = artifact_root.resolve(strict=True)
        candidate = root / MEMORY_CONTRACT_RECEIPT
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise GateInputError("memory contract receipt is unreadable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > 1024 * 1024
        or resolved != candidate
    ):
        raise GateInputError("memory contract receipt is unreadable")
    return _read_json(candidate, "memory contract receipt")


def _validate_memory_contract_receipt(
    receipt: Any,
    *,
    migration_policy: str = MEMORY_MIGRATION_OPTIONAL,
) -> list[str]:
    """Validate deterministic memory evidence and apply the checked-in policy."""

    schema_path = (
        Path(__file__).resolve().parent
        / "schemas"
        / ("memory-contract-receipt.schema.json")
    )
    schema = _read_json(schema_path, "memory contract receipt schema")
    schema_issues = _schema_errors(schema, receipt)
    if schema_issues:
        return [
            "memory contract receipt does not satisfy its locked schema"
            for _issue in schema_issues[:1]
        ]
    if not isinstance(receipt, dict):
        return ["memory contract receipt must be a JSON object"]

    checks = receipt.get("checks")
    expected_ids = [*_MEMORY_CORE_CHECKS, *_MEMORY_MIGRATION_CHECKS]
    if not isinstance(checks, list) or len(checks) != len(expected_ids):
        return ["memory contract receipt check set is incomplete"]
    expected_layers = ["live_api"] * len(_MEMORY_CORE_CHECKS) + [
        "deployed_backend_contract"
    ] * len(_MEMORY_MIGRATION_CHECKS)
    if [check.get("id") for check in checks if isinstance(check, dict)] != expected_ids:
        return ["memory contract receipt check order is invalid"]
    if [
        check.get("layer") for check in checks if isinstance(check, dict)
    ] != expected_layers:
        return ["memory contract receipt check layers are invalid"]

    calculated_summary = {
        label.lower(): sum(1 for check in checks if check.get("status") == label)
        for label in ("PASS", "FAIL", "NOT_EXERCISED", "NOT_RUN")
    }
    if receipt.get("summary") != calculated_summary:
        return ["memory contract receipt summary is inconsistent"]

    core = checks[: len(_MEMORY_CORE_CHECKS)]
    if any(
        check.get("status") != "PASS" or check.get("failure_code") != "NONE"
        for check in core
    ):
        return ["memory contract core live checks did not all pass"]

    migration = checks[len(_MEMORY_CORE_CHECKS) :]
    migration_passed = all(
        check.get("status") == "PASS" and check.get("failure_code") == "NONE"
        for check in migration
    )
    migration_disabled = all(
        check.get("status") == "NOT_EXERCISED"
        and check.get("failure_code") == "MIGRATION_DISABLED"
        for check in migration
    )
    if migration_passed:
        if receipt.get("status") != "PASS" or receipt.get("failure_code") != "NONE":
            return ["memory contract receipt terminal status is inconsistent"]
        return []
    if (
        migration_policy == MEMORY_MIGRATION_OPTIONAL
        and migration_disabled
        and receipt.get("status") == "UNVERIFIED"
        and receipt.get("failure_code") == "MIGRATION_DISABLED"
    ):
        return []
    if migration_policy not in {
        MEMORY_MIGRATION_OPTIONAL,
        MEMORY_MIGRATION_REQUIRED,
    }:
        return ["memory migration release policy is invalid"]
    return ["memory migration contract did not satisfy the release policy"]


def _profile_id(row: Any, *, coverage: bool) -> str:
    if not isinstance(row, dict):
        return ""
    key = "id" if coverage else "profile_id"
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _validate_coverage(coverage: Any, expected_runtime: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(coverage, dict):
        return ["coverage lock must be a JSON object"]
    if set(coverage) != {
        "version",
        "suite_id",
        "scope",
        "target",
        "execution",
        "profiles",
        "required_scenarios",
        "scenario_contracts",
        "trace_latency_contract",
        "reasoning_contract",
        "deterministic_contracts",
        "artifact_contract",
    }:
        errors.append("coverage lock top-level fields do not match the release gate")
    if (
        coverage.get("version") != 1
        or coverage.get("suite_id") != "feedling-api-key-p0"
        or coverage.get("scope") != "api_key_only"
    ):
        errors.append("coverage lock identity does not match the release gate")

    profiles = coverage.get("profiles")
    if not isinstance(profiles, list):
        return ["coverage lock must contain a profiles array"]
    profile_ids = [_profile_id(row, coverage=True) for row in profiles]
    profile_set = set(profile_ids)
    if len(profiles) != len(LOCKED_PROFILE_IDS) or profile_set != set(
        LOCKED_PROFILE_IDS
    ):
        errors.append(
            "coverage lock does not contain the exact eight profiles "
            f"(missing={_fixed_missing(LOCKED_PROFILE_IDS, profile_set)})"
        )
    if "" in profile_set or _duplicates(profile_ids):
        errors.append("coverage lock contains missing or duplicate profile IDs")
    if profile_ids != list(LOCKED_PROFILE_IDS):
        errors.append("coverage lock profile order does not match the locked matrix")
    expected_profiles = []
    for profile_id in LOCKED_PROFILE_IDS:
        route_family, model_family, provider = _PROFILE_METADATA[profile_id]
        provider_key_env, model_env = _PROFILE_SECRET_AND_MODEL_ENVS[profile_id]
        expected_profile = {
            "id": profile_id,
            "route_family": route_family,
            "model_family": model_family,
            "provider": provider,
            "provider_key_env": provider_key_env,
            "model_env": model_env,
            "allowed_model_regex": _PROFILE_ALLOWED_MODEL_REGEXES[profile_id],
            "reasoning_effort": "medium",
            "reasoning_expected": True,
        }
        if profile_id in _RELAY_BASE_URL_ENVS:
            expected_profile.update(
                base_url_env=_RELAY_BASE_URL_ENVS[profile_id],
                allowed_base_url=_PROFILE_CONFIGURED_BASE_URLS[profile_id],
            )
        expected_profiles.append(expected_profile)
    if profiles != expected_profiles:
        errors.append("coverage lock profile definitions do not match the release gate")
    if any(
        not isinstance(profile, dict)
        or profile.get("reasoning_expected") is not True
        or profile.get("reasoning_effort") != "medium"
        for profile in profiles
    ):
        errors.append(
            "coverage lock does not require medium reasoning for every profile"
        )

    scenarios = coverage.get("required_scenarios")
    if not isinstance(scenarios, list) or not all(
        isinstance(item, str) for item in scenarios
    ):
        errors.append("coverage lock must contain a string required_scenarios array")
    else:
        scenario_set = set(scenarios)
        if len(scenarios) != len(LOCKED_SCENARIO_IDS) or scenario_set != set(
            LOCKED_SCENARIO_IDS
        ):
            errors.append(
                "coverage lock does not contain exact P0-01 through P0-13 scenarios "
                f"(missing={_fixed_missing(LOCKED_SCENARIO_IDS, scenario_set)})"
            )
        if _duplicates(scenarios):
            errors.append("coverage lock contains duplicate scenario IDs")
        if scenarios != list(LOCKED_SCENARIO_IDS):
            errors.append("coverage lock scenario order is not P0-01 through P0-13")

    if coverage.get("scenario_contracts") != _SCENARIO_CONTRACTS:
        errors.append(
            "coverage lock scenario evidence contracts do not match the release gate"
        )
    if coverage.get("trace_latency_contract") != _TRACE_LATENCY_CONTRACT:
        errors.append(
            "coverage lock trace and latency contract does not match the release gate"
        )
    if coverage.get("reasoning_contract") != _REASONING_CONTRACT:
        errors.append(
            "coverage lock reasoning contract does not match the release gate"
        )
    if coverage.get("deterministic_contracts") != {"memory": _MEMORY_CONTRACT_LOCK}:
        errors.append(
            "coverage lock deterministic memory contract does not match the release gate"
        )
    if coverage.get("artifact_contract") != _ARTIFACT_CONTRACT:
        errors.append("coverage lock artifact contract does not match the release gate")

    target = coverage.get("target")
    if target != {**_COVERAGE_TARGET, "expected_runtime": BASELINE_RUNTIME}:
        errors.append("coverage target does not describe the baseline deployed runtime")
    execution = coverage.get("execution")
    if execution != _EXECUTION_CONTRACT:
        errors.append(
            "coverage lock orchestration contract does not match the release gate"
        )
    return errors


def _is_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    )


def _has_complete_stage_latency(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(REQUIRED_TRACE_STAGES)
        and all(
            _is_nonnegative_number(value.get(stage)) for stage in REQUIRED_TRACE_STAGES
        )
    )


def _nearest_rank_percentile(values: list[int | float], percentile: int) -> int | float:
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100) * len(ordered)))
    return ordered[rank - 1]


def _latency_summary_matches_turns(
    latency: Mapping[str, Any], turns: list[Mapping[str, Any]]
) -> bool:
    if not turns:
        return False
    if any(
        not _is_nonnegative_number(turn.get("ack_latency_ms"))
        or not _is_nonnegative_number(turn.get("reply_latency_ms"))
        or not _has_complete_stage_latency(turn.get("stage_latency_ms"))
        for turn in turns
    ):
        return False

    ack_values = [turn["ack_latency_ms"] for turn in turns]
    reply_values = [turn["reply_latency_ms"] for turn in turns]
    if latency.get("ack_p50_ms") != _nearest_rank_percentile(ack_values, 50):
        return False
    if latency.get("reply_p50_ms") != _nearest_rank_percentile(reply_values, 50):
        return False
    if latency.get("reply_p95_ms") != _nearest_rank_percentile(reply_values, 95):
        return False

    stage_summary = latency.get("stage_p50_ms")
    return all(
        stage_summary.get(stage)
        == _nearest_rank_percentile(
            [turn["stage_latency_ms"][stage] for turn in turns], 50
        )
        for stage in REQUIRED_TRACE_STAGES
    )


def _validate_orchestration(orchestration: Any) -> list[str]:
    if not isinstance(orchestration, dict):
        return ["result orchestration evidence is missing"]
    errors: list[str] = []
    if orchestration.get("supervisor_count") != 1:
        errors.append("result did not prove exactly one qualification supervisor")
    if orchestration.get("max_configured_profile_concurrency") != 3:
        errors.append("result profile concurrency configuration is not three")
    observed = orchestration.get("max_observed_profile_concurrency")
    if (
        not isinstance(observed, int)
        or isinstance(observed, bool)
        or not 1 <= observed <= 3
    ):
        errors.append(
            "result observed profile concurrency is outside one through three"
        )

    assignments = orchestration.get("profile_worker_assignments")
    if not isinstance(assignments, list):
        return errors + ["result profile worker assignments are missing"]
    profile_ids = [
        row.get("profile_id") if isinstance(row, dict) else None for row in assignments
    ]
    worker_ids = [
        row.get("worker_id") if isinstance(row, dict) else None for row in assignments
    ]
    if profile_ids != list(LOCKED_PROFILE_IDS):
        errors.append(
            "result does not assign exactly one worker to every locked profile"
        )
    if (
        len(worker_ids) != len(LOCKED_PROFILE_IDS)
        or any(
            not isinstance(worker_id, str) or not worker_id for worker_id in worker_ids
        )
        or len(set(worker_ids)) != len(worker_ids)
    ):
        errors.append("result profile worker assignment IDs are missing or duplicated")
    return errors


def _validate_orchestration_receipt(
    receipt: Any, result: Mapping[str, Any]
) -> list[str]:
    """Bind agent-authored assignments to launcher-owned process evidence."""
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "launcher_id",
        "max_configured_profile_concurrency",
        "max_observed_profile_concurrency",
        "launch_attempts",
        "workers",
    }:
        return ["trusted orchestration receipt shape is invalid"]
    errors: list[str] = []
    if receipt.get("schema_version") != 3:
        errors.append("trusted orchestration receipt schema is unsupported")
    launcher_id = receipt.get("launcher_id")
    if not isinstance(launcher_id, str) or not _SAFE_PATH_TOKEN_RE.fullmatch(
        launcher_id
    ):
        errors.append("trusted orchestration receipt launcher identity is invalid")
    if receipt.get("max_configured_profile_concurrency") != 3:
        errors.append("trusted orchestration receipt concurrency cap is invalid")
    if receipt.get("launch_attempts") != len(PROFILE_AGENT_TYPES):
        errors.append("trusted orchestration receipt launch count is invalid")
    peak = receipt.get("max_observed_profile_concurrency")
    if not isinstance(peak, int) or isinstance(peak, bool) or not 1 <= peak <= 3:
        errors.append("trusted orchestration receipt concurrency is invalid")

    workers = receipt.get("workers")
    if not isinstance(workers, list) or len(workers) != len(PROFILE_AGENT_TYPES):
        return errors + ["trusted orchestration receipt does not cover eight workers"]
    expected_pairs = list(PROFILE_AGENT_TYPES)
    trusted_assignments: list[dict[str, str]] = []
    trusted_profile_hashes: list[str | None] = [None] * len(PROFILE_AGENT_TYPES)
    worker_ids: list[str] = []
    for index, row in enumerate(workers):
        if not isinstance(row, dict) or set(row) != {
            "profile_id",
            "agent_type",
            "attempt",
            "process_exit_code",
            "worker_id",
            "thread_id",
            "session_id",
            "permission_profile",
            "started_at",
            "stopped_at",
            "profile_result_sha256",
            "exec_events_sha256",
            "cot_receipt_sha256",
            "cot_delivery_status",
            "cot_failure_code",
        }:
            errors.append("trusted orchestration receipt worker shape is invalid")
            continue
        profile_id, agent_type = expected_pairs[index]
        if row.get("profile_id") != profile_id or row.get("agent_type") != agent_type:
            errors.append("trusted orchestration receipt worker matrix is invalid")
        if row.get("permission_profile") != f"feedling-e2e-{profile_id}":
            errors.append("trusted orchestration receipt worker permission is invalid")
        if row.get("attempt") != 1 or row.get("process_exit_code") != 0:
            errors.append("trusted orchestration receipt worker process is invalid")
        worker_id = row.get("worker_id")
        if (
            not isinstance(worker_id, str)
            or not _SAFE_PATH_TOKEN_RE.fullmatch(worker_id)
            or row.get("thread_id") != worker_id
        ):
            errors.append("trusted orchestration receipt worker identity is invalid")
            continue
        session_id = row.get("session_id")
        if session_id is not None and (
            not isinstance(session_id, str)
            or not _SAFE_PATH_TOKEN_RE.fullmatch(session_id)
        ):
            errors.append("trusted orchestration receipt worker session is invalid")
        started = _parse_timestamp(row.get("started_at"))
        stopped = _parse_timestamp(row.get("stopped_at"))
        if started is None or stopped is None or stopped < started:
            errors.append("trusted orchestration receipt timestamps are invalid")
        for field in (
            "profile_result_sha256",
            "exec_events_sha256",
            "cot_receipt_sha256",
        ):
            digest = row.get(field)
            if not isinstance(digest, str) or not _EVIDENCE_SHA256_RE.fullmatch(digest):
                errors.append(
                    "trusted orchestration receipt content binding is invalid"
                )
                break
        if (
            row.get("cot_delivery_status") != "PASS"
            or row.get("cot_failure_code") != "NONE"
        ):
            errors.append("trusted orchestration receipt COT lifecycle is invalid")
        profile_digest = row.get("profile_result_sha256")
        if isinstance(profile_digest, str) and _EVIDENCE_SHA256_RE.fullmatch(
            profile_digest
        ):
            trusted_profile_hashes[index] = profile_digest
        worker_ids.append(worker_id)
        trusted_assignments.append({"profile_id": profile_id, "worker_id": worker_id})
    if len(worker_ids) != len(PROFILE_AGENT_TYPES) or len(set(worker_ids)) != len(
        PROFILE_AGENT_TYPES
    ):
        errors.append("trusted orchestration receipt worker IDs are incomplete")

    if len(workers) == len(PROFILE_AGENT_TYPES):
        points: list[tuple[datetime, int]] = []
        for row in workers:
            if not isinstance(row, dict):
                points = []
                break
            started = _parse_timestamp(row.get("started_at"))
            stopped = _parse_timestamp(row.get("stopped_at"))
            if started is None or stopped is None or stopped < started:
                points = []
                break
            points.extend(((started, 1), (stopped, -1)))
        if points:
            points.sort(key=lambda point: (point[0], point[1]))
            active = 0
            observed = 0
            for _, delta in points:
                active += delta
                observed = max(observed, active)
            if active != 0 or observed != peak:
                errors.append(
                    "trusted orchestration receipt concurrency is inconsistent"
                )

    orchestration = result.get("orchestration")
    if not isinstance(orchestration, dict):
        return errors
    if orchestration.get("max_observed_profile_concurrency") != peak:
        errors.append("agent orchestration concurrency differs from trusted receipt")
    if orchestration.get("profile_worker_assignments") != trusted_assignments:
        errors.append("agent worker assignments differ from trusted receipt")
    profiles = result.get("profiles")
    if not isinstance(profiles, list) or len(profiles) != len(PROFILE_AGENT_TYPES):
        errors.append("agent profile results cannot be bound to trusted receipt")
        return errors
    for index, ((profile_id, _), profile) in enumerate(
        zip(PROFILE_AGENT_TYPES, profiles, strict=True)
    ):
        if (
            not isinstance(profile, dict)
            or profile.get("profile_id") != profile_id
            or trusted_profile_hashes[index] is None
        ):
            errors.append("agent profile results differ from trusted receipt")
            break
        try:
            canonical = json.dumps(
                profile,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            errors.append("agent profile results differ from trusted receipt")
            break
        if hashlib.sha256(canonical).hexdigest() != trusted_profile_hashes[index]:
            errors.append("agent profile results differ from trusted receipt")
            break
    return errors


def _validate_attempts(
    profile_id: str,
    scenario_id: str,
    scenario: Mapping[str, Any],
    diagnostics: set[str],
) -> tuple[list[str], bool]:
    errors: list[str] = []
    attempts = scenario.get("attempts")
    attempt_results = scenario.get("attempt_results")
    retried = attempts == 2
    if not isinstance(attempt_results, list) or len(attempt_results) != attempts:
        return [
            f"profile {profile_id} scenario {scenario_id} attempt history is incomplete"
        ], retried
    attempt_numbers = [
        row.get("attempt") if isinstance(row, dict) else None for row in attempt_results
    ]
    if attempt_numbers != list(range(1, len(attempt_results) + 1)):
        errors.append(
            f"profile {profile_id} scenario {scenario_id} attempt order is invalid"
        )
    for row in attempt_results:
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        failure = row.get("failure")
        if status == PASS:
            if failure is not None:
                errors.append(
                    f"profile {profile_id} scenario {scenario_id} "
                    "PASS attempt has failure evidence"
                )
        elif not isinstance(failure, dict) or failure.get("category") != status:
            errors.append(
                f"profile {profile_id} scenario {scenario_id} "
                "attempt failure category is inconsistent"
            )
    if attempt_results:
        final = attempt_results[-1]
        if not isinstance(final, dict) or final.get("status") != scenario.get("status"):
            errors.append(
                f"profile {profile_id} scenario {scenario_id} final attempt does not match status"
            )

    evidence_codes = scenario.get("evidence_codes")
    if retried:
        first = attempt_results[0] if attempt_results else None
        first_failure = first.get("failure") if isinstance(first, dict) else None
        if (
            not isinstance(first, dict)
            or first.get("status") != "AGENT_ERROR"
            or not isinstance(first_failure, dict)
            or first_failure.get("stage_code") not in _TRANSIENT_RETRY_STAGES
            or first_failure.get("failure_code") not in _TRANSIENT_RETRY_FAILURE_CODES
            or first_failure.get("reproducible") is not False
            or not isinstance(attempt_results[-1], dict)
            or attempt_results[-1].get("status") != PASS
        ):
            errors.append(
                f"profile {profile_id} scenario {scenario_id} "
                "retry is not a bounded transient retry"
            )
        if (
            not isinstance(evidence_codes, list)
            or "RETRY_OBSERVATION_RECORDED" not in evidence_codes
        ):
            errors.append(
                f"profile {profile_id} scenario {scenario_id} retry observation is missing"
            )
        if "RETRY_USED" not in diagnostics or not diagnostics.intersection(
            {"TRANSIENT_PROVIDER_ERROR", "TRANSIENT_TRANSPORT_ERROR"}
        ):
            errors.append(
                f"profile {profile_id} scenario {scenario_id} retry diagnostics are incomplete"
            )
    elif (
        isinstance(evidence_codes, list)
        and "RETRY_OBSERVATION_RECORDED" in evidence_codes
    ):
        errors.append(
            f"profile {profile_id} scenario {scenario_id} records a retry that did not occur"
        )
    return errors, retried


def _validate_scenario_contract(
    profile_id: str,
    scenario: Mapping[str, Any],
    scenario_turns: Sequence[Mapping[str, Any]],
    all_turns: Sequence[Mapping[str, Any]],
    diagnostics: set[str],
) -> tuple[list[str], bool]:
    scenario_id = scenario.get("scenario_id")
    if scenario_id not in _SCENARIO_CONTRACTS:
        return [], False
    contract = _SCENARIO_CONTRACTS[scenario_id]
    errors, retried = _validate_attempts(profile_id, scenario_id, scenario, diagnostics)

    expected_assertions = contract["required_assertions"]
    assertions = scenario.get("assertions")
    if (
        not isinstance(assertions, dict)
        or set(assertions) != set(expected_assertions)
        or any(assertions.get(name) is not True for name in expected_assertions)
    ):
        errors.append(
            f"profile {profile_id} scenario {scenario_id} assertions do not match the lock"
        )

    expected_evidence = list(contract["required_evidence_codes"])
    if retried:
        expected_evidence.append("RETRY_OBSERVATION_RECORDED")
    evidence_codes = scenario.get("evidence_codes")
    if (
        not isinstance(evidence_codes, list)
        or len(evidence_codes) != len(expected_evidence)
        or set(evidence_codes) != set(expected_evidence)
    ):
        errors.append(
            f"profile {profile_id} scenario {scenario_id} evidence codes do not match the lock"
        )

    for field, minimum in contract["minimum_id_counts"].items():
        values = scenario.get(field)
        if not isinstance(values, list) or len(values) < minimum:
            errors.append(
                f"profile {profile_id} scenario {scenario_id} required {field} are missing"
            )

    required_turn_count = contract["required_turn_count"]
    if len(scenario_turns) != required_turn_count:
        errors.append(
            f"profile {profile_id} scenario {scenario_id} turn count does not match the lock"
        )
    expected_request_ids = [row.get("request_id") for row in scenario_turns]
    expected_turn_ids = [row.get("turn_id") for row in scenario_turns]
    expected_trace_ids = [row.get("trace_id") for row in scenario_turns]
    if required_turn_count:
        if scenario.get("request_ids") != expected_request_ids:
            errors.append(
                f"profile {profile_id} scenario {scenario_id} request IDs do not match its turns"
            )
        if scenario.get("turn_ids") != expected_turn_ids:
            errors.append(
                f"profile {profile_id} scenario {scenario_id} turn IDs do not match its turns"
            )
        if scenario.get("trace_ids") != expected_trace_ids:
            errors.append(
                f"profile {profile_id} scenario {scenario_id} trace IDs do not match its turns"
            )
    elif scenario_id != "P0-13" and scenario.get("turn_ids") != []:
        errors.append(
            f"profile {profile_id} scenario {scenario_id} contains unrelated turn IDs"
        )

    if scenario_id == "P0-13":
        all_turn_ids = [row.get("turn_id") for row in all_turns]
        all_trace_ids = [row.get("trace_id") for row in all_turns]
        if scenario.get("turn_ids") != all_turn_ids:
            errors.append(
                f"profile {profile_id} scenario P0-13 does not correlate every turn"
            )
        if scenario.get("trace_ids") != all_trace_ids:
            errors.append(
                f"profile {profile_id} scenario P0-13 does not correlate every trace"
            )
    return errors, retried


def _validate_result_semantics(
    result: Mapping[str, Any], expected_runtime: str, expected_sha: str
) -> list[str]:
    errors: list[str] = []

    if result.get("overall_status") != PASS:
        errors.append("overall status is not PASS")
    if result.get("profiles_expected") != len(LOCKED_PROFILE_IDS):
        errors.append("profiles_expected is not eight")
    if result.get("profiles_completed") != len(LOCKED_PROFILE_IDS):
        errors.append("profiles_completed is not eight")
    errors.extend(_validate_orchestration(result.get("orchestration")))

    target = result.get("target")
    if not isinstance(target, dict):
        return errors + ["result target is missing"]
    if target.get("expected_runtime") != expected_runtime:
        errors.append("result target runtime does not match the release gate")
    if str(target.get("expected_deployment_sha") or "").lower() != expected_sha.lower():
        errors.append("result expected deployment SHA does not match the release gate")
    if (
        str(target.get("observed_backend_sha") or "").lower()
        != expected_sha.lower()
    ):
        errors.append("observed backend SHA does not match the expected deployment")
    if target.get("observed_worker_sha") is not None:
        errors.append("result must not invent unavailable worker build identity")

    profiles = result.get("profiles")
    if not isinstance(profiles, list):
        return errors + ["result profiles array is missing"]
    profile_ids = [_profile_id(row, coverage=False) for row in profiles]
    profile_set = set(profile_ids)
    if len(profiles) != len(LOCKED_PROFILE_IDS) or profile_set != set(
        LOCKED_PROFILE_IDS
    ):
        errors.append(
            "result does not contain the exact eight profiles "
            f"(missing={_fixed_missing(LOCKED_PROFILE_IDS, profile_set)})"
        )
    if "" in profile_set or _duplicates(profile_ids):
        errors.append("result contains missing or duplicate profile IDs")
    if profile_ids != list(LOCKED_PROFILE_IDS):
        errors.append("result profile order does not match the locked matrix")

    seen_user_ids: set[str] = set()
    seen_request_ids: set[str] = set()
    seen_turn_ids: set[str] = set()
    seen_trace_ids: set[str] = set()
    seen_persona_job_ids: set[str] = set()
    seen_persona_evidence_sha256: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            errors.append("result contains a malformed profile entry")
            continue
        profile_id = _profile_id(profile, coverage=False)
        if profile_id not in LOCKED_PROFILE_IDS:
            # Do not echo attacker/model-controlled profile IDs.
            continue
        if profile.get("status") != PASS:
            errors.append(f"profile {profile_id} status is not PASS")
        route_family, model_family, provider = _PROFILE_METADATA[profile_id]
        if (
            profile.get("route_family") != route_family
            or profile.get("model_family") != model_family
            or profile.get("provider") != provider
        ):
            errors.append(
                f"profile {profile_id} route metadata does not match the lock"
            )
        if profile.get("expected_runtime") != expected_runtime:
            errors.append(f"profile {profile_id} expected runtime does not match")
        observed_runtime = profile.get("observed_runtime")
        observed_version = profile.get("observed_runtime_version")
        if expected_runtime == EXPECTED_RUNTIME:
            if observed_runtime != expected_runtime:
                errors.append(f"profile {profile_id} did not observe Runtime V2")
            if observed_version != 2:
                errors.append(
                    f"profile {profile_id} did not observe Runtime V2 version"
                )
        elif (
            not isinstance(observed_runtime, str)
            or not observed_runtime
            or type(observed_version) is not int
            or observed_version < 1
        ):
            errors.append(f"profile {profile_id} has no deployed runtime readback")
        if profile.get("reasoning_effort") != "medium":
            errors.append(
                f"profile {profile_id} did not prove medium reasoning was enabled"
            )

        user_id = profile.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            errors.append(f"profile {profile_id} has no synthetic user ID")
        elif user_id in seen_user_ids:
            errors.append("result contains duplicate synthetic user IDs")
        else:
            seen_user_ids.add(user_id)

        diagnostic_values = profile.get("diagnostic_codes")
        diagnostics = (
            set(diagnostic_values) if isinstance(diagnostic_values, list) else set()
        )
        scenarios = profile.get("scenarios")
        scenarios_by_id: dict[str, Mapping[str, Any]] = {}
        if not isinstance(scenarios, list):
            errors.append(f"profile {profile_id} has no scenarios array")
        else:
            scenario_ids = [
                (
                    row.get("scenario_id")
                    if isinstance(row, dict) and isinstance(row.get("scenario_id"), str)
                    else ""
                )
                for row in scenarios
            ]
            scenario_set = set(scenario_ids)
            if len(scenarios) != len(LOCKED_SCENARIO_IDS) or scenario_set != set(
                LOCKED_SCENARIO_IDS
            ):
                errors.append(
                    f"profile {profile_id} does not contain exact P0-01 through P0-13 scenarios "
                    f"(missing={_fixed_missing(LOCKED_SCENARIO_IDS, scenario_set)})"
                )
            if "" in scenario_set or _duplicates(scenario_ids):
                errors.append(
                    f"profile {profile_id} contains missing or duplicate scenario IDs"
                )
            if scenario_ids != list(LOCKED_SCENARIO_IDS):
                errors.append(
                    f"profile {profile_id} scenario order is not P0-01 through P0-13"
                )
            for scenario in scenarios:
                if not isinstance(scenario, dict):
                    continue
                scenario_id = scenario.get("scenario_id")
                if scenario_id in LOCKED_SCENARIO_IDS:
                    scenarios_by_id[scenario_id] = scenario
                    if scenario.get("status") != PASS:
                        errors.append(
                            f"profile {profile_id} scenario {scenario_id} status is not PASS"
                        )
                    for request_id in scenario.get("request_ids", []):
                        if request_id in seen_request_ids:
                            errors.append("result contains duplicate request IDs")
                        else:
                            seen_request_ids.add(request_id)

        for scenario_id, scenario in scenarios_by_id.items():
            if scenario_id != "P0-06" and scenario.get("persona_finalizer") is not None:
                errors.append(
                    f"profile {profile_id} scenario {scenario_id} contains unrelated "
                    "persona finalizer evidence"
                )
        persona_scenario = scenarios_by_id.get("P0-06")
        persona_finalizer = (
            persona_scenario.get("persona_finalizer")
            if isinstance(persona_scenario, Mapping)
            else None
        )
        persona_fields = {
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
        persona_valid = (
            isinstance(persona_finalizer, dict)
            and set(persona_finalizer) == persona_fields
            and persona_finalizer.get("fixture_id") == PERSONA_FIXTURE_ID
            and isinstance(persona_finalizer.get("evidence_sha256"), str)
            and bool(
                _EVIDENCE_SHA256_RE.fullmatch(
                    persona_finalizer.get("evidence_sha256", "")
                )
            )
            and isinstance(persona_finalizer.get("request_id"), str)
            and bool(persona_finalizer.get("request_id"))
            and isinstance(persona_finalizer.get("job_id"), str)
            and bool(persona_finalizer.get("job_id"))
            and persona_finalizer.get("semantic_judgment_bound") is True
            and persona_finalizer.get("finalizer_ok") is True
            and persona_finalizer.get("private_evidence_deleted") is True
            and type(persona_finalizer.get("archive_upload_count")) is int
            and persona_finalizer.get("archive_upload_count") == 4
            and persona_finalizer.get("archive_receipts_verified") is True
            and persona_finalizer.get("genesis_upload_metadata_verified") is True
            and type(persona_finalizer.get("privacy_violation_count")) is int
            and persona_finalizer.get("privacy_violation_count") == 0
        )
        if not persona_valid:
            errors.append(
                f"profile {profile_id} persona finalizer evidence is incomplete"
            )
        else:
            if persona_scenario.get("request_ids") != [persona_finalizer["request_id"]]:
                errors.append(
                    f"profile {profile_id} persona finalizer request does not match P0-06"
                )
            job_id = persona_finalizer["job_id"]
            evidence_sha256 = persona_finalizer["evidence_sha256"]
            if job_id in seen_persona_job_ids:
                errors.append("result contains duplicate persona finalizer job IDs")
            else:
                seen_persona_job_ids.add(job_id)
            if evidence_sha256 in seen_persona_evidence_sha256:
                errors.append(
                    "result contains duplicate persona finalizer evidence hashes"
                )
            else:
                seen_persona_evidence_sha256.add(evidence_sha256)

        cleanup = profile.get("cleanup")
        if not isinstance(cleanup, dict) or cleanup.get("status") != PASS:
            errors.append(f"profile {profile_id} cleanup status is not PASS")
        elif not all(
            cleanup.get(field) is True
            for field in (
                "attempted",
                "provider_config_deleted",
                "account_reset",
                "old_credential_rejected",
            )
        ):
            errors.append(f"profile {profile_id} cleanup assertions are incomplete")

        redaction = profile.get("redaction")
        if (
            not isinstance(redaction, dict)
            or not all(redaction.get(field) is True for field in _REDACTION_TRUE_FIELDS)
            or redaction.get("prompt_injection_detected") is not False
        ):
            errors.append(f"profile {profile_id} redaction assertions are not safe")

        turns = profile.get("turns")
        p0_12_turns = (
            [
                turn
                for turn in turns
                if isinstance(turn, dict) and turn.get("scenario_id") == "P0-12"
            ]
            if isinstance(turns, list)
            else []
        )
        reasoning = profile.get("reasoning")
        if (
            not isinstance(reasoning, dict)
            or not all(
                reasoning.get(field) is True
                for field in (
                    "expected",
                    "metadata_present",
                    "token_metadata_present",
                    "user_visible_disclosure_present",
                )
            )
            or reasoning.get("capability_enabled") is not True
            or reasoning.get("requested_effort") != "medium"
            or reasoning.get("configured_effort") != "medium"
            or reasoning.get("effective_effort") != "unknown"
            or not isinstance(reasoning.get("reasoning_event_count"), int)
            or isinstance(reasoning.get("reasoning_event_count"), bool)
            or reasoning.get("reasoning_event_count", 0) <= 0
            or not reasoning.get("kind")
            or not reasoning.get("source")
            or not reasoning.get("model")
            or reasoning.get("model") != profile.get("model")
            or not isinstance(reasoning.get("reasoning_token_count"), int)
            or reasoning.get("reasoning_token_count", 0) <= 0
            or not isinstance(reasoning.get("disclosure_length"), int)
            or reasoning.get("disclosure_length", 0) <= 0
            or reasoning.get("raw_private_reasoning_stored") is not False
        ):
            errors.append(f"profile {profile_id} reasoning assertions are incomplete")
        elif (
            len(p0_12_turns) != 1
            or reasoning.get("request_id") != p0_12_turns[0].get("request_id")
            or reasoning.get("turn_id") != p0_12_turns[0].get("turn_id")
            or reasoning.get("trace_id") != p0_12_turns[0].get("trace_id")
        ):
            errors.append(
                f"profile {profile_id} reasoning evidence does not match P0-12 turn"
            )

        trace = profile.get("trace")
        required_turn_total = sum(
            contract["required_turn_count"] for contract in _SCENARIO_CONTRACTS.values()
        )
        if (
            not isinstance(trace, dict)
            or trace.get("enabled") is not True
            or trace.get("deploy_enabled") is not True
            or not isinstance(trace.get("correlated_event_count"), int)
            or isinstance(trace.get("correlated_event_count"), bool)
            or trace.get("correlated_event_count", 0)
            < required_turn_total * len(REQUIRED_TRACE_STAGES)
            or not isinstance(trace.get("observed_event_types"), list)
            or len(trace.get("observed_event_types", [])) != len(REQUIRED_TRACE_STAGES)
            or set(trace.get("observed_event_types", [])) != set(REQUIRED_TRACE_STAGES)
            or trace.get("missing_required_event_types") != []
            or trace.get("raw_trace_stored") is not False
        ):
            errors.append(f"profile {profile_id} trace assertions are incomplete")

        if isinstance(turns, list):
            scenario_turns: dict[str, list[dict[str, Any]]] = {
                scenario_id: [] for scenario_id in LOCKED_SCENARIO_IDS
            }
            for turn in turns:
                if not isinstance(turn, dict):
                    errors.append(f"profile {profile_id} contains a malformed turn")
                    continue
                scenario_id = turn.get("scenario_id")
                if scenario_id in scenario_turns:
                    scenario_turns[scenario_id].append(turn)
                else:
                    errors.append(
                        f"profile {profile_id} contains a turn outside the lock"
                    )
                request_id = turn.get("request_id")
                turn_id = turn.get("turn_id")
                trace_id = turn.get("trace_id")
                if not isinstance(request_id, str) or not request_id:
                    errors.append(
                        f"profile {profile_id} contains a turn without a request ID"
                    )
                if not isinstance(turn_id, str) or not turn_id:
                    errors.append(f"profile {profile_id} contains a turn without an ID")
                    continue
                if not isinstance(trace_id, str) or not trace_id:
                    errors.append(
                        f"profile {profile_id} contains a turn without a trace ID"
                    )
                if turn_id in seen_turn_ids:
                    errors.append("result contains duplicate turn IDs")
                else:
                    seen_turn_ids.add(turn_id)
                if isinstance(trace_id, str) and trace_id:
                    if trace_id in seen_trace_ids:
                        errors.append("result contains duplicate trace IDs")
                    else:
                        seen_trace_ids.add(trace_id)
                ack_latency = turn.get("ack_latency_ms")
                reply_latency = turn.get("reply_latency_ms")
                if (
                    turn.get("reply_count") != 1
                    or turn.get("content_assertion_passed") is not True
                    or turn.get("fallback_detected") is not False
                    or turn.get("duplicate_detected") is not False
                    or turn.get("out_of_order_detected") is not False
                    or not _is_nonnegative_number(ack_latency)
                    or not _is_nonnegative_number(reply_latency)
                    or (
                        _is_nonnegative_number(ack_latency)
                        and _is_nonnegative_number(reply_latency)
                        and not (
                            ack_latency
                            <= reply_latency
                            <= _EXECUTION_CONTRACT["chat_reply_timeout_seconds"] * 1000
                        )
                    )
                    or not _has_complete_stage_latency(turn.get("stage_latency_ms"))
                ):
                    errors.append(
                        f"profile {profile_id} contains a failed turn assertion"
                    )
            for scenario_id, contract in _SCENARIO_CONTRACTS.items():
                scenario_rows = scenario_turns[scenario_id]
                expected_count = contract["required_turn_count"]
                indices = [row.get("turn_index") for row in scenario_rows]
                if len(scenario_rows) != expected_count or indices != list(
                    range(1, expected_count + 1)
                ):
                    errors.append(
                        f"profile {profile_id} scenario {scenario_id} "
                        "turns are not exact and ordered"
                    )
            expected_turn_order = [
                (scenario_id, turn_index)
                for scenario_id in LOCKED_SCENARIO_IDS
                for turn_index in range(
                    1, _SCENARIO_CONTRACTS[scenario_id]["required_turn_count"] + 1
                )
            ]
            observed_turn_order = [
                (turn.get("scenario_id"), turn.get("turn_index")) for turn in turns
            ]
            if observed_turn_order != expected_turn_order:
                errors.append(
                    f"profile {profile_id} turn order does not match the lock"
                )

            retried_any = False
            for scenario_id in LOCKED_SCENARIO_IDS:
                scenario = scenarios_by_id.get(scenario_id)
                if scenario is None:
                    continue
                contract_errors, retried = _validate_scenario_contract(
                    profile_id,
                    scenario,
                    scenario_turns[scenario_id],
                    turns,
                    diagnostics,
                )
                errors.extend(contract_errors)
                retried_any = retried_any or retried
            if retried_any:
                if (
                    not diagnostics.issubset(_PASS_RETRY_DIAGNOSTICS)
                    or "RETRY_USED" not in diagnostics
                ):
                    errors.append(
                        f"profile {profile_id} PASS retry diagnostics are invalid"
                    )
            elif diagnostics:
                errors.append(f"profile {profile_id} has diagnostics without a retry")
        else:
            errors.append(f"profile {profile_id} has no turns array")

        latency = profile.get("latency")
        if (
            not isinstance(latency, dict)
            or not isinstance(latency.get("sample_count"), int)
            or isinstance(latency.get("sample_count"), bool)
            or not isinstance(turns, list)
            or latency.get("sample_count") != len(turns)
            or not _is_nonnegative_number(latency.get("ack_p50_ms"))
            or not _is_nonnegative_number(latency.get("reply_p50_ms"))
            or not _is_nonnegative_number(latency.get("reply_p95_ms"))
            or not _has_complete_stage_latency(latency.get("stage_p50_ms"))
            or latency.get("missing_stages") != []
            or not _latency_summary_matches_turns(latency, turns)
        ):
            errors.append(f"profile {profile_id} latency evidence is incomplete")

    summary = result.get("summary")
    if not isinstance(summary, dict):
        errors.append("result summary is missing")
    else:
        if summary.get("pass") != len(LOCKED_PROFILE_IDS):
            errors.append("summary PASS count is not eight")
        if any(summary.get(field) != 0 for field in _NONPASS_SUMMARY_FIELDS):
            errors.append("summary contains a non-PASS count")

    redaction = result.get("redaction")
    if (
        not isinstance(redaction, dict)
        or not all(redaction.get(field) is True for field in _REDACTION_TRUE_FIELDS)
        or redaction.get("prompt_injection_detected") is not False
    ):
        errors.append("run-level redaction assertions are not safe")
    return errors


def _safe_artifact_path(
    root: Path, value: Any, field: str
) -> tuple[Path | None, str | None]:
    if not isinstance(value, str) or not value:
        return None, f"artifact reference {field} is missing"
    # Use one portable path syntax and reject path spellings that can be absolute
    # or traversal-capable on another platform.
    if "\x00" in value or "\\" in value or value.startswith("/") or ":" in value:
        return None, f"artifact reference {field} is not a safe relative path"
    normalized = value[:-1] if value.endswith("/") else value
    parts = normalized.split("/")
    if not normalized or any(part in ("", ".", "..") for part in parts):
        return None, f"artifact reference {field} is not a safe relative path"
    try:
        candidate = (root / Path(*parts)).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return (
            None,
            f"artifact reference {field} is missing or escapes the artifact root",
        )
    return candidate, None


def _validate_artifacts(
    result: Mapping[str, Any], artifact_root: Path, result_path: Path
) -> list[str]:
    errors: list[str] = []
    if artifact_root.is_symlink():
        return ["artifact root is missing or unreadable"]
    try:
        root = artifact_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return ["artifact root is missing or unreadable"]
    if not root.is_dir():
        return ["artifact root is not a directory"]
    try:
        resolved_result = result_path.resolve(strict=True)
        resolved_result.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        errors.append("result JSON is not contained by the artifact root")
        resolved_result = None

    references = result.get("artifacts")
    if not isinstance(references, dict):
        return errors + ["result artifact references are missing"]
    for field, expected_kind in _ARTIFACT_FIELDS.items():
        candidate, error = _safe_artifact_path(root, references.get(field), field)
        if error:
            errors.append(error)
            continue
        assert candidate is not None
        if expected_kind == "file" and not candidate.is_file():
            errors.append(f"artifact reference {field} is not a file")
        if expected_kind == "directory" and not candidate.is_dir():
            errors.append(f"artifact reference {field} is not a directory")
        if field == "profiles_directory" and candidate.is_dir():
            for profile_id in LOCKED_PROFILE_IDS:
                profile_file = candidate / f"{profile_id}.json"
                try:
                    resolved_profile = profile_file.resolve(strict=True)
                    resolved_profile.relative_to(candidate)
                except (OSError, RuntimeError, ValueError):
                    errors.append(f"profile artifact {profile_id} is missing or unsafe")
                    continue
                if not resolved_profile.is_file():
                    errors.append(f"profile artifact {profile_id} is not a file")
        if (
            field == "run_result"
            and resolved_result is not None
            and candidate != resolved_result
        ):
            errors.append(
                "artifact reference run_result does not identify the validated result"
            )
    return errors


def _synthetic_lease_valid(value: Any, reaper: Mapping[str, Any]) -> bool:
    return (
        isinstance(value, dict)
        and value.get("registered") is True
        and isinstance(value.get("lease_id"), str)
        and re.fullmatch(r"lease_[0-9a-f]{32}", value.get("lease_id", "")) is not None
        and isinstance(value.get("absence_token"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value.get("absence_token", "")) is not None
        and isinstance(value.get("expires_at"), str)
        and bool(value.get("expires_at"))
        and type(value.get("expires_at_epoch")) is int
        and value["expires_at_epoch"] > 0
        and value.get("ttl_seconds") == reaper.get("max_ttl_seconds")
    )


def _validate_provisioning_manifest(
    manifest: Any, result: Mapping[str, Any], expected_runtime: str
) -> list[str]:
    if not isinstance(manifest, dict):
        return ["trusted provisioning manifest must be a JSON object"]
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("trusted provisioning manifest schema is unsupported")
    if (
        manifest.get("base_url") != "https://test-api.feedling.app"
        or manifest.get("runtime_mode") != expected_runtime
    ):
        errors.append(
            "trusted provisioning manifest target is not the locked test runtime"
        )
    reaper = manifest.get("synthetic_account_reaper")
    if (
        not isinstance(reaper, dict)
        or reaper.get("enabled") is not True
        or reaper.get("ready") is not True
        or reaper.get("heartbeat_fresh") is not True
        or reaper.get("label_prefix") != "agent-e2e-"
        or not isinstance(reaper.get("max_ttl_seconds"), int)
        or isinstance(reaper.get("max_ttl_seconds"), bool)
        or not 1 <= reaper["max_ttl_seconds"] <= 14_400
    ):
        errors.append(
            "trusted provisioning manifest lacks a safe synthetic-account reaper"
        )

    manifest_profiles = manifest.get("profiles")
    result_profiles = result.get("profiles")
    if not isinstance(manifest_profiles, list) or not isinstance(result_profiles, list):
        return errors + ["trusted provisioning manifest has no profiles array"]
    manifest_ids = [
        row.get("profile_id") if isinstance(row, dict) else None
        for row in manifest_profiles
    ]
    if manifest_ids != list(LOCKED_PROFILE_IDS):
        errors.append(
            "trusted provisioning manifest does not contain the locked matrix"
        )
        return errors
    result_by_id = {
        row.get("profile_id"): row for row in result_profiles if isinstance(row, dict)
    }
    run_id = result.get("run_id")
    for entry in manifest_profiles:
        profile_id = entry["profile_id"]
        result_profile = result_by_id.get(profile_id)
        if not isinstance(result_profile, dict):
            errors.append(
                f"profile {profile_id} is not bound to its provisioned account"
            )
            continue
        route_family, _model_family, provider = _PROFILE_METADATA[profile_id]
        model = entry.get("configured_model")
        configured_base_url = entry.get("configured_base_url")
        user_id = entry.get("user_id")
        expected_label = f"agent-e2e-{run_id}-{profile_id}"
        invalid_receipt = entry.get("invalid_key_receipt")
        valid_receipt = entry.get("valid_key_receipt")
        runtime_mode = entry.get("runtime_mode")
        runtime_version = entry.get("runtime_version")
        runtime_receipt = entry.get("runtime_readback_receipt")
        synthetic_lease = entry.get("synthetic_account_lease")
        synthetic_lease_valid = _synthetic_lease_valid(synthetic_lease, reaper)
        runtime_readback_valid = (
            entry.get("runtime_mode_readback_verified") is True
            and isinstance(runtime_mode, str)
            and bool(runtime_mode)
            and type(runtime_version) is int
            and runtime_version >= 1
            and runtime_receipt
            == {
                "configured": True,
                "runtime_mode": runtime_mode,
                "runtime_version": runtime_version,
            }
        )
        if expected_runtime == EXPECTED_RUNTIME:
            runtime_contract_valid = (
                entry.get("runtime_mode_set_required") is False
                and entry.get("runtime_mode_set_verified") is False
                and runtime_mode == EXPECTED_RUNTIME
                and runtime_version == 2
                and runtime_readback_valid
            )
        else:
            runtime_contract_valid = (
                entry.get("runtime_mode_set_required") is False
                and entry.get("runtime_mode_set_verified") is False
                and runtime_readback_valid
            )
        if (
            entry.get("label") != expected_label
            or entry.get("provider") != provider
            or entry.get("route_family") != route_family
            or not isinstance(model, str)
            or not model
            or re.fullmatch(_PROFILE_ALLOWED_MODEL_REGEXES[profile_id], model) is None
            or configured_base_url != _PROFILE_CONFIGURED_BASE_URLS[profile_id]
            or entry.get("reasoning_effort") != "medium"
            or entry.get("provision_status") != "ready"
            or entry.get("provision_failure_code") != "NONE"
            or not isinstance(user_id, str)
            or not user_id
            or not all(
                isinstance(entry.get(field), str) and bool(entry.get(field))
                for field in ("api_key", "secret_key_b64", "public_key_b64")
            )
            or entry.get("registration_verified") is not True
            or entry.get("fresh_state_verified") is not True
            or entry.get("invalid_key_rejected") is not True
            or entry.get("valid_key_configured") is not True
            or entry.get("trace_enabled") is not True
            or not synthetic_lease_valid
            or not runtime_contract_valid
            or not isinstance(invalid_receipt, dict)
            or invalid_receipt.get("http_status") != 400
            or invalid_receipt.get("error") != "provider_test_failed"
            or invalid_receipt.get("provider_status_code") not in (400, 401, 403)
            or valid_receipt
            != {
                "status": "configured",
                "provider": provider,
                "model": model,
                "base_url": configured_base_url,
                "reasoning_effort": "medium",
            }
        ):
            errors.append(
                f"profile {profile_id} trusted provisioning receipt is incomplete"
            )
        reasoning = result_profile.get("reasoning")
        if (
            result_profile.get("route_family") != route_family
            or result_profile.get("provider") != provider
            or result_profile.get("model") != model
            or result_profile.get("reasoning_effort") != "medium"
            or result_profile.get("user_id") != user_id
            or result_profile.get("expected_runtime") != expected_runtime
            or not isinstance(reasoning, dict)
            or reasoning.get("model") != model
        ):
            errors.append(
                f"profile {profile_id} is not bound to its provisioned account"
            )
    auxiliary = manifest.get("auxiliary_accounts")
    provider_user_ids = {
        str(entry.get("user_id") or "")
        for entry in manifest_profiles
        if isinstance(entry, dict)
    }
    provider_api_keys = {
        str(entry.get("api_key") or "")
        for entry in manifest_profiles
        if isinstance(entry, dict)
    }
    if (
        not isinstance(auxiliary, list)
        or len(auxiliary) != 1
        or not isinstance(auxiliary[0], dict)
    ):
        errors.append(
            "trusted provisioning manifest lacks the dedicated memory account"
        )
        return errors

    memory = auxiliary[0]
    memory_fields = {
        "profile_id",
        "purpose",
        "label",
        "user_id",
        "api_key",
        "secret_key_b64",
        "public_key_b64",
        "provision_status",
        "provision_failure_code",
        "synthetic_account_lease",
    }
    memory_user_id = memory.get("user_id")
    memory_api_key = memory.get("api_key")
    if (
        set(memory) != memory_fields
        or memory.get("profile_id") != MEMORY_CONTRACT_PROFILE_ID
        or memory.get("purpose") != "deterministic_memory_contract"
        or memory.get("label") != f"agent-e2e-{run_id}-{MEMORY_CONTRACT_PROFILE_ID}"
        or memory.get("provision_status") != "ready"
        or memory.get("provision_failure_code") != "NONE"
        or not all(
            isinstance(memory.get(field), str) and bool(memory.get(field))
            for field in (
                "user_id",
                "api_key",
                "secret_key_b64",
                "public_key_b64",
            )
        )
        or memory_user_id in provider_user_ids
        or memory_api_key in provider_api_keys
        or not _synthetic_lease_valid(memory.get("synthetic_account_lease"), reaper)
    ):
        errors.append(
            "trusted provisioning receipt for the memory account is incomplete"
        )
    return errors


def _validate_deployment_receipt(
    receipt: Any,
    result: Mapping[str, Any],
    expected_sha: str,
    expected_runtime: str,
    phase: str,
) -> list[str]:
    errors: list[str] = []
    label = f"{phase}-run trusted deployment receipt"
    if not isinstance(receipt, dict):
        return [f"{label} must be a JSON object"]
    expected = expected_sha.lower()
    if receipt.get("schema_version") != 1:
        errors.append(f"{label} schema is unsupported")
    if (
        receipt.get("environment") != "test"
        or receipt.get("base_url") != "https://test-api.feedling.app"
        or receipt.get("expected_runtime") != expected_runtime
        or receipt.get("liveness_verified") is not True
    ):
        errors.append(f"{label} target is not the test environment")
    receipt_expected = str(receipt.get("expected_deployment_sha") or "").lower()
    backend_sha = str(receipt.get("observed_backend_sha") or "").lower()
    deployment_sha = str(receipt.get("observed_deployment_sha") or "").lower()
    worker_sha = str(receipt.get("observed_worker_sha") or "").lower()
    if (
        receipt_expected != expected
        or backend_sha != expected
        or deployment_sha != expected
        or receipt.get("deployment_identity_verified") is not True
    ):
        errors.append(f"{label} does not match the candidate")
    live_worker_count = receipt.get("live_worker_count")
    expected_source = (
        "per_profile_runtime_readback_and_live_scenarios"
        if expected_runtime == EXPECTED_RUNTIME
        else "deployed_runtime_readback"
    )
    if (
        receipt.get("observed_worker_sha") is not None
        or live_worker_count is not None
        or receipt.get("runtime_evidence_source") != expected_source
    ):
        errors.append(f"{label} has an invalid runtime evidence source")
    target = result.get("target")
    if isinstance(target, dict) and (
        str(target.get("observed_backend_sha") or "").lower() != backend_sha
        or str(target.get("observed_worker_sha") or "").lower() != worker_sha
    ):
        errors.append(
            f"agent result deployment identity differs from the {phase}-run receipt"
        )
    return errors


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _validate_deployment_receipt_pair(
    pre_receipt: Any, post_receipt: Any, result: Mapping[str, Any]
) -> list[str]:
    if not isinstance(pre_receipt, dict) or not isinstance(post_receipt, dict):
        return []
    identity_fields = (
        "environment",
        "base_url",
        "expected_runtime",
        "expected_deployment_sha",
        "observed_backend_sha",
        "observed_deployment_sha",
        "observed_worker_sha",
        "runtime_evidence_source",
        "liveness_verified",
        "deployment_identity_verified",
    )
    errors: list[str] = []
    if any(
        pre_receipt.get(field) != post_receipt.get(field) for field in identity_fields
    ):
        errors.append("deployment identity changed during the qualification run")
    pre_time = _parse_timestamp(pre_receipt.get("verified_at"))
    post_time = _parse_timestamp(post_receipt.get("verified_at"))
    started_at = _parse_timestamp(result.get("started_at"))
    finished_at = _parse_timestamp(result.get("finished_at"))
    if (
        pre_time is None
        or post_time is None
        or started_at is None
        or finished_at is None
        or not pre_time <= started_at <= finished_at <= post_time
        or not pre_time < post_time
    ):
        errors.append(
            "deployment receipt timestamps do not bracket the qualification run"
        )
    return errors


def validate_release(
    *,
    coverage_path: Path,
    schema_path: Path,
    result_path: Path,
    artifacts_path: Path,
    provisioning_manifest_path: Path,
    orchestration_receipt_path: Path,
    deployment_receipt_path: Path,
    post_deployment_receipt_path: Path,
    expected_runtime: str,
    expected_sha: str,
) -> list[str]:
    """Return sanitized release-gate errors; an empty list means PASS."""
    if expected_runtime not in {BASELINE_RUNTIME, EXPECTED_RUNTIME}:
        return ["expected runtime is invalid"]
    if not _SHA_RE.fullmatch(str(expected_sha or "")):
        return ["expected deployment SHA is malformed"]
    if artifacts_path.is_symlink():
        return ["artifact root is missing or unreadable"]

    coverage = _read_json(coverage_path, "coverage lock")
    schema = _read_json(schema_path, "result schema")
    result = _read_json(result_path, "run result")
    memory_contract_receipt = _read_memory_contract_receipt(artifacts_path)
    provisioning_manifest = _read_private_manifest(provisioning_manifest_path)
    orchestration_receipt = _read_private_json(
        orchestration_receipt_path, "trusted orchestration receipt"
    )
    if deployment_receipt_path.is_symlink():
        raise GateInputError("pre-run trusted deployment receipt is unreadable")
    if post_deployment_receipt_path.is_symlink():
        raise GateInputError("post-run trusted deployment receipt is unreadable")
    pre_receipt = _read_json(
        deployment_receipt_path, "pre-run trusted deployment receipt"
    )
    post_receipt = _read_json(
        post_deployment_receipt_path, "post-run trusted deployment receipt"
    )

    errors = _validate_coverage(coverage, expected_runtime)
    errors.extend(
        _validate_memory_contract_receipt(
            memory_contract_receipt,
            migration_policy=_MEMORY_CONTRACT_LOCK["migration_policy"],
        )
    )
    schema_issues = _schema_errors(schema, result)
    errors.extend(schema_issues)
    if not schema_issues and isinstance(result, dict):
        errors.extend(
            _validate_deployment_receipt(
                pre_receipt, result, expected_sha, expected_runtime, "pre"
            )
        )
        errors.extend(
            _validate_deployment_receipt(
                post_receipt, result, expected_sha, expected_runtime, "post"
            )
        )
        errors.extend(
            _validate_deployment_receipt_pair(pre_receipt, post_receipt, result)
        )
        errors.extend(
            _validate_provisioning_manifest(
                provisioning_manifest, result, expected_runtime
            )
        )
        errors.extend(_validate_orchestration_receipt(orchestration_receipt, result))
        errors.extend(
            _validate_result_semantics(result, expected_runtime, expected_sha)
        )
        errors.extend(_validate_artifacts(result, artifacts_path, result_path))
    return list(dict.fromkeys(errors))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate API-key E2E release artifacts"
    )
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--provisioning-manifest", type=Path, required=True)
    parser.add_argument("--orchestration-receipt", type=Path, required=True)
    parser.add_argument("--deployment-receipt", type=Path, required=True)
    parser.add_argument("--post-deployment-receipt", type=Path, required=True)
    parser.add_argument(
        "--expected-runtime",
        choices=(BASELINE_RUNTIME, EXPECTED_RUNTIME),
        default=BASELINE_RUNTIME,
    )
    parser.add_argument("--expected-sha", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        errors = validate_release(
            coverage_path=args.coverage,
            schema_path=args.schema,
            result_path=args.result,
            artifacts_path=args.artifacts,
            provisioning_manifest_path=args.provisioning_manifest,
            orchestration_receipt_path=args.orchestration_receipt,
            deployment_receipt_path=args.deployment_receipt,
            post_deployment_receipt_path=args.post_deployment_receipt,
            expected_runtime=args.expected_runtime,
            expected_sha=args.expected_sha,
        )
    except GateInputError as exc:
        errors = [str(exc)]
    except Exception:
        # Never print exception text from untrusted JSON, paths, or dependencies.
        errors = ["release gate encountered an internal validation error"]

    if errors:
        print("release gate: FAIL", file=sys.stderr)
        for error in errors[:50]:
            print(f"ERROR: {error}", file=sys.stderr)
        if len(errors) > 50:
            print(
                "ERROR: additional release-gate errors were suppressed", file=sys.stderr
            )
        return 1
    print("release gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
