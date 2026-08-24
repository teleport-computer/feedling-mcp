"""Shared privacy policy for dedicated-vision fallback decisions."""

from __future__ import annotations


_MAIN_FALLBACK_ERROR_CODES = frozenset({
    "vision_model_rate_limited",
    "vision_model_unavailable",
    "vision_model_empty_response",
})


def is_main_fallback_eligible(error_code: str, reason: str = "") -> bool:
    """Return the exact approved dedicated-failure fallback whitelist.

    This predicate intentionally ignores ``retryable``.  Retryability is a
    transport hint, while this decision authorizes disclosure of raw pixels to
    a second provider route and therefore has a narrower, explicit vocabulary.
    """
    code = str(error_code or "").strip()
    return code in _MAIN_FALLBACK_ERROR_CODES or (
        code == "vision_model_failed"
        and str(reason or "").strip() == "output_truncated"
    )
