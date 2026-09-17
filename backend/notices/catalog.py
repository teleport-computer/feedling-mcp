"""Derived public views of the producer-owned ``error_class`` registry."""
from __future__ import annotations

# migrate 错误码仅用于识别历史 job 终态；老卡迁移机制已删。

import re

from notices import error_contract


# These exports remain source-compatible for backend callers, but their only
# source of truth is ErrorSpec. Adding a second literal here is forbidden.
ERROR_CLASSES = frozenset(spec.code for spec in error_contract.public_specs())
_CATALOG: dict[str, tuple[str, str]] = {
    spec.code: (spec.blame, spec.safe_text_zh)
    for spec in error_contract.public_specs()
}
_UPSTREAM_RULES = tuple(
    (spec.code, spec.matcher())
    for spec in error_contract.matcher_specs()
)


def registry_export(source_loaders=None) -> error_contract.RegistryExport:
    """Export availability without turning failure into an empty set."""
    return error_contract.registry_export(source_loaders)


# Seven 2026-08-21: only these exact provider/account outcomes are explicit
# enough to remove from Feedling's failure numerator. V1 and V2 remain separate
# keyspaces because their producer values are different contracts.
USER_UNAVAILABLE_V1_REASONS = frozenset({
    "quota_insufficient",
    "extraction_failed:quota_insufficient",
    "image_generation_quota_insufficient",
    "provider_account_expired",
    "auth_invalid",
    "image_generation_auth_invalid",
    "model_not_found",
    "image_generation_model_not_found",
})
USER_UNAVAILABLE_V2_OUTCOME_CODES = frozenset({
    "turn_failed:quota_insufficient",
    "extraction_failed:quota_insufficient",
    "turn_failed:image_generation_quota_insufficient",
    "turn_failed:provider_account_expired",
    "turn_failed:auth_invalid",
    "turn_failed:image_generation_auth_invalid",
    "turn_failed:model_not_found",
    "turn_failed:image_generation_model_not_found",
})

# --------------------------------------------------------------------------- #
# 2026-09-15 hx-approved memory-lane additions — PENDING SEVEN'S REVIEW
# --------------------------------------------------------------------------- #
# Product decision (hx, 2026-09-15): a memory-lane (capture/dream/migrate)
# failure *proven* to be the user's own account or model configuration leaves
# Feedling's operational failure numerator, exactly like Seven's chat-lane
# entries above. "Proven" means the memory-lane classifier
# (``notices.agent_call_failure``, strong-evidence gated; V2 extraction's
# provider classification; V2 ``provider_setup`` resolver errors) produced one
# of the account classes below.
#
# Additions only: Seven's two sets above are left exactly as approved and are
# extended by union right after this block, so reverting this block restores
# them byte-for-byte. Deliberately NOT here (they stay operational failures):
# upstream_unavailable, rate_limited, timeouts (incl. the resident agent call's
# own ``turn_timeout``), content_filtered, context_overflow,
# provider_incompatible, provider_config, cli_config_invalid, unknown, and every
# Feedling-side code (database_pool_timeout, lease_timeout, watchdog codes,
# write failures).
#
# ``resident_agent_cli_logged_out`` is deliberately NOT excused either, matching
# Seven's chat set: on hosted V1 runners a platform key-injection/decrypt bug
# makes the Claude CLI print the same "Not logged in · Please run /login"
# (``agent_runtime/spawners.py``), so excusing it would hide a platform bug from
# the failure rate. The capture escape valve still treats it as an account class
# (waits instead of skipping, with the login notice copy).
#
# V1 keyspace: the backend stores ``<lane>_agent_call_failed:<class>``
# (``proactive_core._job_status_patch`` via ``agent_call_failure.normalize_reason``).
# Rows written before that normalization keep a raw tail and stay operational.
MEMORY_LANE_USER_UNAVAILABLE_V1_REASONS = frozenset({
    "capture_agent_call_failed:auth_invalid",
    "capture_agent_call_failed:quota_insufficient",
    "capture_agent_call_failed:model_not_found",
    "capture_agent_call_failed:provider_account_expired",
    "dream_agent_call_failed:auth_invalid",
    "dream_agent_call_failed:quota_insufficient",
    "dream_agent_call_failed:model_not_found",
    "dream_agent_call_failed:provider_account_expired",
    "migrate_agent_call_failed:auth_invalid",
    "migrate_agent_call_failed:quota_insufficient",
    "migrate_agent_call_failed:model_not_found",
    "migrate_agent_call_failed:provider_account_expired",
})
# V2 keyspace (``agent_jobs.last_error``). ``extraction_failed:quota_insufficient``
# is already in Seven's set. ``extraction_failed:provider_account_expired`` is
# not added: V2 extraction never produces it (its provider classification maps
# 401/403 to auth_invalid) and it is not a registered producer code.
MEMORY_LANE_USER_UNAVAILABLE_V2_OUTCOME_CODES = frozenset({
    "extraction_failed:auth_invalid",
    "extraction_failed:model_not_found",
    "provider_setup:model_api_not_configured",
    "provider_setup:model_api_not_tested",
    "provider_setup:model_api_key_envelope_missing",
    "provider_setup:model_api_config_invalid",
})
USER_UNAVAILABLE_V1_REASONS = (
    USER_UNAVAILABLE_V1_REASONS | MEMORY_LANE_USER_UNAVAILABLE_V1_REASONS
)
USER_UNAVAILABLE_V2_OUTCOME_CODES = (
    USER_UNAVAILABLE_V2_OUTCOME_CODES
    | MEMORY_LANE_USER_UNAVAILABLE_V2_OUTCOME_CODES
)
# ------------------------ end of 2026-09-15 additions ----------------------- #


def v1_proactive_outcome_class(status: object, reason: object) -> str:
    """Classify the resident/V1 proactive status-reason keyspace.

    ``skipped`` is a control-plane outcome (including heartbeat_throttled), not
    a failed realization. A failed job leaves our numerator only for Seven's
    exact user-unavailable reasons. Unknown reasons remain operational failures.
    """
    normalized_status = str(status or "").strip()
    normalized_reason = str(reason or "").strip() or "unknown"
    if normalized_status == "skipped":
        return "control"
    if normalized_status != "failed":
        return ""
    if normalized_reason in USER_UNAVAILABLE_V1_REASONS:
        return "user_unavailable"
    return "operational_failure"


_FALLBACK_BLAME = "system"
_FALLBACK_USER_TEXT = "连接模型服务时出了问题。"

# Retired values can still exist in mirrored notice streams. Filtering them at
# read time avoids mutating only one side of the primary/shadow pair.
RETIRED_ERROR_CLASSES = frozenset({"responses_unsupported"})


def blame_for(error_class: str) -> str:
    """Unknown input always falls back to system blame."""
    entry = _CATALOG.get(error_class)
    return entry[0] if entry is not None else _FALLBACK_BLAME


def user_text_for(error_class: str, **ctx) -> str:
    """Return stable localized safe text without consulting a second map."""
    spec = error_contract.spec_for(error_class)
    if spec is None:
        return _FALLBACK_USER_TEXT
    return spec.text(str(ctx.get("language") or ""))


def classify_upstream(text: str) -> str:
    """Classify provider text using the registry's ordered matchers."""
    candidate = text or ""
    lowered = candidate.lower()
    if "resident_never_claimed" in lowered:
        return "resident_never_claimed"
    spec = error_contract.classify_text(candidate)
    if spec is not None:
        return spec.code
    if re.search(r"\b404\b", candidate) and "model" in lowered:
        return "model_not_found"
    return ""
