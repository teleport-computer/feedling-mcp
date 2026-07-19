"""Deterministic, sanitized profile rows for interrupted diagnostic workers.

The headless qualification agent normally authors a semantic ``profileResult``.
Local diagnostic runs still need a complete selected matrix when that agent
times out, exits non-zero, or emits malformed evidence.  This module builds the
smallest honest fallback row: it preserves only locked profile metadata and
non-secret provisioning facts, marks all behavioral evidence unavailable, and
can never qualify a release.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


class DiagnosticResultError(RuntimeError):
    """A fixed diagnostic fallback contract failure."""


_PROFILE_METADATA: dict[str, tuple[str, str, str]] = {
    "official-deepseek": ("official", "deepseek", "deepseek"),
    "official-anthropic": ("official", "claude", "anthropic"),
    "official-openai": ("official", "openai", "openai"),
    "official-gemini": ("official", "gemini", "gemini"),
    "openrouter-claude": ("openrouter", "claude", "openrouter"),
    "openrouter-openai": ("openrouter", "openai", "openrouter"),
    "openrouter-glm": ("openrouter", "glm", "openrouter"),
    "openrouter-kimi": ("openrouter", "kimi", "openrouter"),
    "relay-kongbeiqie": ("relay", "claude", "openai_compatible"),
}
_TRACE_STAGES = ("routing", "queue", "provider", "persistence", "delivery")
_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ALLOWED_RUNTIME_REQUIREMENTS = frozenset(("deployed_current", "hosted_resident"))
_BLOCKED_PROVISIONING_FAILURE_CODE = "VALID_KEY_REJECTED"
_SCENARIO_ASSERTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "PREFLIGHT",
        (
            "target_is_test",
            "deployed_endpoint_reachable",
            "provisioning_receipts_confirmed",
            "agent_environment_sanitized",
            "contract_inputs_readable",
            "credentials_omitted",
        ),
    ),
    (
        "ONBOARDING",
        ("synthetic_account_is_fresh", "whoami_matches", "trace_cleared"),
    ),
    (
        "INVALID_KEY_VALIDATION",
        ("invalid_key_rejected", "invalid_key_not_echoed", "hosted_chat_not_started"),
    ),
    (
        "VALID_KEY_RECOVERY",
        ("valid_key_accepted", "provider_config_matches", "credential_omitted"),
    ),
    (
        "RUNTIME_SELECTION",
        (
            "runtime_status_readback_succeeds",
            "runtime_configured",
            "runtime_metadata_recorded",
        ),
    ),
    (
        "PERSONA_IMPORT",
        (
            "persona_files_archived",
            "persona_source_metadata_verified",
            "persona_import_done",
            "persona_acceptance_passed",
            "privacy_canary_absent",
        ),
    ),
    (
        "ACTIVATION",
        (
            "driver_enabled",
            "chat_loop_verified",
            "runtime_status_readback_succeeds",
            "no_orphan_turn",
        ),
    ),
    (
        "BASIC_CHAT",
        (
            "async_ack_received",
            "exact_reply_correlated",
            "nonce_echo_confirmed",
            "fallback_absent",
            "latency_recorded",
        ),
    ),
    (
        "RELIABILITY_CHAT",
        (
            "ten_turns_ordered",
            "exact_replies_correlated",
            "memory_recall_confirmed",
            "no_orphan_turn",
        ),
    ),
    (
        "MEMORY_PERSONA",
        (
            "imported_memory_recalled",
            "persona_consistency_confirmed",
            "contradictory_facts_absent",
        ),
    ),
    (
        "IDENTITY",
        (
            "agent_identity_confirmed",
            "model_route_confirmed",
            "provider_config_matches",
            "trace_route_correlated",
        ),
    ),
    (
        "REASONING",
        (
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
        ),
    ),
    (
        "TRACE_LATENCY_CLEANUP",
        (
            "trace_stages_complete",
            "trace_correlation_confirmed",
            "latency_attributed",
            "cleanup_confirmed",
        ),
    ),
)


def _safe_model(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        return "unavailable"
    if any(
        character.isspace() or not character.isprintable() for character in value
    ):
        return "unavailable"
    return value


def _safe_user_id(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_USER_ID_RE.fullmatch(value):
        return value
    return None


def _safe_runtime(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_USER_ID_RE.fullmatch(value):
        return value
    return None


def agent_error_profile(
    manifest_profile: Mapping[str, Any],
    *,
    profile_id: str,
    expected_runtime: str,
) -> dict[str, Any]:
    """Build one schema-compatible, evidence-negative ``AGENT_ERROR`` row."""

    metadata = _PROFILE_METADATA.get(profile_id)
    if metadata is None or manifest_profile.get("profile_id") != profile_id:
        raise DiagnosticResultError("diagnostic fallback profile is invalid")
    if expected_runtime not in _ALLOWED_RUNTIME_REQUIREMENTS:
        raise DiagnosticResultError("diagnostic fallback runtime is invalid")

    route_family, model_family, provider = metadata
    observed_runtime = _safe_runtime(manifest_profile.get("runtime_mode"))
    runtime_version = manifest_profile.get("runtime_version")
    observed_runtime_version = (
        runtime_version
        if type(runtime_version) is int and runtime_version >= 1
        else None
    )
    trace_enabled = manifest_profile.get("trace_enabled") is True

    return {
        "profile_id": profile_id,
        "route_family": route_family,
        "model_family": model_family,
        "provider": provider,
        "model": _safe_model(manifest_profile.get("configured_model")),
        "reasoning_effort": "medium",
        "user_id": _safe_user_id(manifest_profile.get("user_id")),
        "expected_runtime": expected_runtime,
        "observed_runtime": observed_runtime,
        "observed_runtime_version": observed_runtime_version,
        "status": "AGENT_ERROR",
        "scenarios": [],
        "turns": [],
        "latency": {
            "sample_count": 0,
            "ack_p50_ms": None,
            "reply_p50_ms": None,
            "reply_p95_ms": None,
            "stage_p50_ms": {stage: None for stage in _TRACE_STAGES},
            "missing_stages": list(_TRACE_STAGES),
        },
        "reasoning": {
            "expected": True,
            "capability_enabled": False,
            "requested_effort": "medium",
            "configured_effort": "medium",
            "effective_effort": "unknown",
            "reasoning_event_count": 0,
            "metadata_present": False,
            "token_metadata_present": False,
            "user_visible_disclosure_present": False,
            "request_id": "unavailable",
            "turn_id": "unavailable",
            "trace_id": "unavailable",
            "kind": None,
            "source": None,
            "model": None,
            "reasoning_token_count": None,
            "disclosure_length": None,
            "raw_private_reasoning_stored": False,
        },
        "trace": {
            "enabled": trace_enabled,
            "deploy_enabled": trace_enabled,
            "correlated_event_count": 0,
            "observed_event_types": [],
            "missing_required_event_types": list(_TRACE_STAGES),
            "raw_trace_stored": False,
        },
        "cleanup": {
            "attempted": False,
            "provider_config_deleted": False,
            "account_reset": False,
            "old_credential_rejected": False,
            "status": "AGENT_ERROR",
        },
        "diagnostic_codes": [
            "AGENT_EXECUTION_ERROR",
            "TRACE_PARTIAL",
            "STAGE_TIMING_UNAVAILABLE",
        ],
        "redaction": {
            "provider_keys_omitted": True,
            "feedling_api_keys_omitted": True,
            "content_private_keys_omitted": True,
            "raw_chat_omitted": True,
            "raw_trace_omitted": True,
            "raw_reasoning_omitted": True,
            "synthetic_users_only": True,
            "prompt_injection_detected": False,
        },
    }


def blocked_provision_profile(
    manifest_profile: Mapping[str, Any],
    *,
    profile_id: str,
    expected_runtime: str,
    provisioning_failure_code: str,
) -> dict[str, Any]:
    """Build a diagnostic-only result for a rejected valid provider key.

    A provisioning blocker means no headless agent was launched.  Every SOP row
    is therefore present but has zero attempts, no IDs, no timings, and false
    scenario assertions.  Only the fixed ``VALID_KEY_REJECTED`` outcome is
    accepted; other provisioning failures must not be collapsed into this
    credential-specific representation.  The sanitized operational code
    remains parent-owned summary evidence rather than being forced into the
    narrower worker-result schema.

    This shape intentionally validates only against the Codex authoring schema.
    Its zero-attempt scenario rows are rejected by the formal release schema, so
    it cannot masquerade as an agent-executed qualification result.
    """

    if (
        provisioning_failure_code != _BLOCKED_PROVISIONING_FAILURE_CODE
        or manifest_profile.get("provision_status") != "blocked"
        or manifest_profile.get("provision_failure_code")
        != provisioning_failure_code
    ):
        raise DiagnosticResultError(
            "blocked diagnostic profile requires VALID_KEY_REJECTED"
        )

    result = agent_error_profile(
        manifest_profile,
        profile_id=profile_id,
        expected_runtime=expected_runtime,
    )
    status = "BLOCKED_CREDENTIAL"
    result.update(
        {
            "observed_runtime": None,
            "observed_runtime_version": None,
            "status": status,
            "scenarios": [
                {
                    "scenario_id": f"P0-{index:02d}",
                    "status": status,
                    "started_at": "NOT_RUN",
                    "finished_at": "NOT_RUN",
                    "attempts": 0,
                    "attempt_results": [],
                    "assertions": {name: False for name in assertion_names},
                    "evidence_codes": [],
                    "request_ids": [],
                    "turn_ids": [],
                    "trace_ids": [],
                    "persona_finalizer": None,
                    "failure": {
                        "category": status,
                        "stage_code": stage_code,
                        "failure_code": "CREDENTIAL_SETUP_FAILED",
                        "reproducible": True,
                    },
                }
                for index, (stage_code, assertion_names) in enumerate(
                    _SCENARIO_ASSERTIONS, start=1
                )
            ],
            "diagnostic_codes": ["STAGE_TIMING_UNAVAILABLE"],
        }
    )
    result["trace"].update({"enabled": False, "deploy_enabled": False})
    result["cleanup"]["status"] = status
    return result
