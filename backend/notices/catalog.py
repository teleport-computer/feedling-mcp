"""Derived public views of the producer-owned ``error_class`` registry."""
from __future__ import annotations

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
    "auth_invalid",
    "image_generation_auth_invalid",
    "model_not_found",
    "image_generation_model_not_found",
})
USER_UNAVAILABLE_V2_OUTCOME_CODES = frozenset({
    "turn_failed:quota_insufficient",
    "extraction_failed:quota_insufficient",
    "turn_failed:image_generation_quota_insufficient",
    "turn_failed:auth_invalid",
    "turn_failed:image_generation_auth_invalid",
    "turn_failed:model_not_found",
    "turn_failed:image_generation_model_not_found",
})


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
