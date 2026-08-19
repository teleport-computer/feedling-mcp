"""Pure, content-free retry policy for Runtime V2 profile generation."""

from __future__ import annotations

from dataclasses import dataclass
import re


_SHAPE_EXACT = frozenset({"reply_not_text", "reply_empty", "reply_not_json"})
_SHAPE_PREFIXES = (
    "missing_field:",
    "field_empty:",
    "placeholder_detected:",
    "memory_chars_over_budget:",
    "style_chars_over_budget:",
    # Legacy reject codes may remain in stored retry metadata until the next
    # successful MEMORY/STYLE distillation rewrites the profile.
    "user_chars_over_budget:",
    "fields_overlap:",
    "map_reply_chars_over_budget:",
    "map_line_count_over_budget:",
    "map_line_not_bullet:",
    "map_bullet_empty:",
    "map_bullet_chars_over_budget:",
    "map_rendered_chars_over_budget:",
)
_MAP_SHAPE_EXACT = frozenset({"map_reply_not_text", "map_reply_empty"})
_SOURCE_EXACT = frozenset(
    {
        "profile_cards_index_invalid",
        "profile_cards_count_invalid",
        "profile_cards_id_missing",
        "profile_cards_render_incomplete",
    }
)
_SOURCE_CODE_RE = re.compile(
    r"^(?:profile_source_exceeds_budget:[0-9]+|"
    r"profile_cards_(?:truncated:[0-9]+/[0-9]+|"
    r"index_failed:[0-9]+|fetch_failed:[0-9]+))$"
)
_GENERATION_ERROR_RE = re.compile(r"^profile_generation_failed:[a-z0-9_]+$")


@dataclass(frozen=True)
class ProfileRetryDecision:
    disposition: str
    retry_family: str
    retry_attempts: int
    retry_not_before: float
    reason: str


def classify_retry_family(*, error_class: str, reject_code: str) -> str:
    reliable_class = str(error_class or "").strip().lower()
    code = str(reject_code or "").strip()
    if reliable_class in {"transient", "transient_exhausted"}:
        return "transient"
    if reliable_class in {"provider_config", "provider_incompatible"}:
        return "provider_config"
    if (
        code in _SHAPE_EXACT
        or code in _MAP_SHAPE_EXACT
        or any(code.startswith(prefix) for prefix in _SHAPE_PREFIXES)
    ):
        return "shape"
    if code in _SOURCE_EXACT or _SOURCE_CODE_RE.fullmatch(code):
        return "source"
    return "terminal"


def _retry_reason(family: str, reject_code: str) -> str:
    code = str(reject_code or "").strip()
    if family != "terminal":
        return code
    if _GENERATION_ERROR_RE.fullmatch(code) or code in {
        "profile_cas_failed",
        "lostjoblease",
        "runtimemodechanged",
    }:
        return code
    return "profile_retry_terminal"


def decide_profile_retry(
    *,
    error_class: str,
    reject_code: str,
    previous_retry_family: str,
    previous_retry_attempts: int,
    now: float,
) -> ProfileRetryDecision:
    family = classify_retry_family(
        error_class=error_class,
        reject_code=reject_code,
    )
    retry_attempts = (
        max(0, int(previous_retry_attempts)) + 1
        if str(previous_retry_family or "") == family
        else 1
    )
    reason = _retry_reason(family, reject_code)
    if family == "transient":
        delay = min(21600.0, 300.0 * (2 ** max(0, retry_attempts - 1)))
        return ProfileRetryDecision(
            "scheduled", family, retry_attempts, float(now) + delay, reason
        )
    if family == "provider_config":
        return ProfileRetryDecision(
            "provider_config", family, retry_attempts, 0.0, reason
        )
    if family == "shape":
        if retry_attempts <= 3:
            delay = min(21600.0, 300.0 * (2 ** max(0, retry_attempts - 1)))
            return ProfileRetryDecision(
                "scheduled", family, retry_attempts, float(now) + delay, reason
            )
        return ProfileRetryDecision(
            "terminal", family, retry_attempts, 0.0, reason
        )
    if family == "source":
        return ProfileRetryDecision(
            "source_change", family, retry_attempts, 0.0, reason
        )
    return ProfileRetryDecision(
        "terminal", "terminal", retry_attempts, 0.0, reason
    )
