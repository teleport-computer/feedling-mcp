"""Single source of truth for what a valid IO identity card must satisfy.

Imported by BOTH the backend write paths (init / replace / profile_patch /
dimension_nudge) AND io_cli's local pre-validation, so the two never drift.
Therefore this module MUST stay pure stdlib — io_cli runs standalone on a VPS
and cannot pull backend DB deps.

Contract = B (evidence-first, sparse-allowed): we validate STRUCTURE only.
We do NOT require exactly 7 dimensions and we do NOT reject clustered /
low-spread / sparse cards — those are quality nudges owned by the prompt, not
gates. Blocking on them would hurt onboarding success rate.
"""
from __future__ import annotations

# Single source of truth. backend/identity/service.py imports this.
RUNTIME_LABELS: frozenset[str] = frozenset({
    "io", "feedling", "p0", "p-zero",
    "hermes", "claude", "claude code", "claude desktop", "claude-code", "claude-desktop",
    "claude.ai", "anthropic", "openclaw", "open-claw", "open claw", "cursor",
    "chatgpt", "chat-gpt", "gpt", "gpt-4", "gpt-4o", "gpt-5", "openai", "openrouter",
    "gemini", "google ai", "google", "bard", "deepseek", "minimax", "copilot", "github copilot",
    "agent", "assistant", "ai", "bot",
})

MAX_DIMENSIONS = 12  # sanity cap, NOT a floor
_VALUE_MIN, _VALUE_MAX = 0, 100
_OK: tuple[bool, str] = (True, "")


def is_runtime_label(name: str) -> bool:
    return str(name or "").strip().lower() in RUNTIME_LABELS


def normalize_dimension_value(v) -> int:
    """Coerce a dimension value onto the 0–100 INTEGER contract.

    The card contract is an integer 0–100 score, but self-hosted BYOK weak
    models routinely misread the scale as a 0–1 probability and emit floats
    like 0.95 / 0.6. A raw float survives ``clamp``-only sanitizing and then
    crashes iOS' integer JSONDecoder (dataCorrupted) on the decrypted card,
    which the UI misreports as ``decrypt failed`` and retries forever.

    Rule:
      * ``0 < v <= 1``  → treat as a 0–1-scale misuse and rescale ``round(v*100)``
        (so 1.0 is a full-marks dimension, not a score of 1).
      * otherwise       → ``round(v)``.
      * clamp to ``[0, 100]``; always return ``int``.

    Callers MUST have already ensured ``v`` is a real number (not bool / str /
    None) — the structure validators / sanitizer drop non-numbers before this.
    """
    scaled = round(v * 100) if 0 < v <= 1 else round(v)
    return int(max(_VALUE_MIN, min(_VALUE_MAX, scaled)))


def validate_dimensions_structure(dims) -> tuple[bool, str]:
    if not isinstance(dims, list):
        return (False, "dimensions_must_be_list")
    if len(dims) > MAX_DIMENSIONS:
        return (False, "too_many_dimensions")
    seen: set[str] = set()
    for d in dims:
        if not isinstance(d, dict):
            return (False, "dimension_must_be_object")
        name = str(d.get("name") or "").strip()
        if not name:
            return (False, "dimension_name_empty")
        key = name.lower()
        if key in seen:
            return (False, "dimension_name_duplicate")
        seen.add(key)
        value = d.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return (False, "dimension_value_not_number")
        if value < _VALUE_MIN or value > _VALUE_MAX:
            return (False, "dimension_value_out_of_range")
    return _OK


def validate_full_identity_card(card: dict) -> tuple[bool, str]:
    """init / full replace. Structure only (contract B — no count/spread floor)."""
    if not isinstance(card, dict):
        return (False, "identity_must_be_object")
    # agent_name MAY be empty here: contract B / 优先 onboarding 成功率 — do NOT
    # block onboarding on a missing name. The agent should supply a default name
    # (Batch 1 guardrail), but that is guidance, not a gate. A NON-empty name
    # still cannot be a runtime label (e.g. "Claude").
    name = str(card.get("agent_name") or "").strip()
    if name and is_runtime_label(name):
        return (False, "agent_name_is_runtime_label")
    return validate_dimensions_structure(card.get("dimensions", []))


def validate_profile_patch(patch: dict) -> tuple[bool, str]:
    """Only validate fields PRESENT in the patch — never judge the whole card,
    so a name change is not rejected because the old card is sparse."""
    if not isinstance(patch, dict):
        return (False, "patch_must_be_object")
    if "agent_name" in patch:
        name = str(patch.get("agent_name") or "").strip()
        if not name:
            return (False, "agent_name_empty")
        if is_runtime_label(name):
            return (False, "agent_name_is_runtime_label")
    if "dimensions" in patch:
        return validate_dimensions_structure(patch.get("dimensions"))
    return _OK


def validate_dimension_nudge(target_name: str, new_value) -> tuple[bool, str]:
    if not str(target_name or "").strip():
        return (False, "dimension_name_empty")
    if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
        return (False, "dimension_value_not_number")
    if new_value < _VALUE_MIN or new_value > _VALUE_MAX:
        return (False, "dimension_value_out_of_range")
    return _OK


def sanitize_identity_card(card: dict) -> dict:
    """Best-effort clean so the card ALWAYS PASSES structure validation WITHOUT
    losing usable content (contract: capture more, don't reject fuzzy issues).
    Normalize values to a 0–100 INTEGER (0–1-scale floats are rescaled ×100,
    everything else rounded, then clamped — see normalize_dimension_value);
    drop non-dict / non-number-valued / unnamed dims;
    drop duplicate dimension names (keep first); truncate to MAX_DIMENSIONS.
    A non-list ``dimensions`` (missing/None/wrong type) normalizes to ``[]``
    rather than being left as-is, so the output is never structurally invalid.
    Does NOT touch agent_name — empty is allowed and a runtime-label name is a
    STRONG check the caller handles; we never invent a name here."""
    if not isinstance(card, dict):
        return card
    out = dict(card)
    dims = card.get("dimensions")
    if isinstance(dims, list):
        cleaned: list = []
        seen: set[str] = set()
        for d in dims:
            if not isinstance(d, dict):
                continue
            name = str(d.get("name") or "").strip()
            if not name or name.lower() in seen:
                continue
            v = d.get("value")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            nd = dict(d)
            nd["name"] = name
            nd["value"] = normalize_dimension_value(v)
            seen.add(name.lower())
            cleaned.append(nd)
            if len(cleaned) >= MAX_DIMENSIONS:
                break
        out["dimensions"] = cleaned
    else:
        out["dimensions"] = []
    return out
