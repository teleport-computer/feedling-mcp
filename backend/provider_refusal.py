"""Content-free observations of explicit provider refusal metadata.

This is not a retry/error classifier. Only Anthropic's structured refusal
marker is recognized; reply text and stop_details.explanation are never read.
Category vocabulary: https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)
CATEGORIES = frozenset({
    "cyber", "bio", "frontier_llm", "reasoning_extraction", "general_harms",
    "unclassified",
})


def from_anthropic_body(body: dict) -> dict | None:
    if body.get("stop_reason") != "refusal":
        return None
    details = body.get("stop_details")
    category = details.get("category") if isinstance(details, dict) else None
    return project({"stop_reason": "refusal", "refusal_category": category})


def project(value: object) -> dict | None:
    """Revalidate metadata at every public boundary; never stringify input."""
    if not isinstance(value, dict) or value.get("stop_reason") != "refusal":
        return None
    category = value.get("refusal_category")
    return {
        "stop_reason": "refusal",
        "refusal_category": (
            category if isinstance(category, str) and category in CATEGORIES
            else "unclassified"
        ),
    }


async def observe(callback, result: object, attempt: int) -> None:
    """Observe each outer attempt without changing its success/retry decision."""
    if callback is None:
        return
    raw = (
        result.get("provider_refusal") if isinstance(result, dict)
        else getattr(result, "provider_refusal", None)
    )
    detail = project(raw)
    if detail is None:
        return
    try:
        await callback({**detail, "attempt": attempt})
    except Exception:  # diagnostics must not change provider behavior
        log.warning("provider refusal observer failed")
