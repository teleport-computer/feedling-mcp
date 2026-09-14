"""Content-free classification of resident memory-lane agent-call failures.

The resident consumer (``tools/chat_resident_consumer.py``
``_agent_call_failed_reason``) reports a failed capture/dream/migrate agent
call as ``<lane>_agent_call_failed:<ExceptionType>: <provider error text>``.
That tail is free text (provider bodies can echo request content), so every
content-free surface used to collapse the whole value to ``runtime_failed``:
a user's revoked key, empty balance or retired model looked exactly like a
Feedling bug.

This module turns the tail into one registered error class **at the backend
boundary**, so it also covers self-hosted consumers that lag behind on
versions. The text is matched with the producer-owned registry
(``notices.error_contract.classify_text``) — no second keyword list — with one
deliberate tightening: that registry's ``upstream_unavailable`` matcher also
accepts any bare 5xx-looking number, which is too weak for a free-form CLI
message ("… 500 tokens …"), so here it additionally requires HTTP/status
context or an explicit transport/overload phrase.

The raw tail is never returned or stored by this module.
"""
from __future__ import annotations

import re

from notices import error_contract

AGENT_CALL_FAILED_PREFIXES = frozenset({
    "capture_agent_call_failed",
    "dream_agent_call_failed",
    "migrate_agent_call_failed",
})

# Every value is a registered public ``error_class`` (so display redaction in
# ``notices.status_reason`` keeps it) and satisfies the content-free code shape
# ``^[a-z0-9_:-]{1,120}$`` used by the lane rollup and admin projections.
# ``classify_text`` can only yield registered codes, so this set is the subset
# that describes a provider/agent-account outcome, plus ``unknown``.
AGENT_CALL_FAILURE_CLASSES = frozenset({
    "auth_invalid",
    "cli_config_invalid",
    "content_filtered",
    "context_overflow",
    "model_not_found",
    "provider_account_expired",
    "provider_incompatible",
    "quota_insufficient",
    "rate_limited",
    "resident_agent_cli_logged_out",
    "unknown",
    "upstream_unavailable",
})

_STRONG_UPSTREAM_EVIDENCE = re.compile(
    r"provider_http_5\d{2}"
    r"|\b(?:https?|status(?:[ _-]?code)?|error|code)\b\W{0,3}5\d{2}\b"
    r"|\b5\d{2}\b\W{0,3}(?:internal server error|bad gateway"
    r"|service (?:temporarily )?unavailable|gateway time-?out)"
    r"|internal server error|bad gateway|service (?:temporarily )?unavailable"
    r"|gateway time-?out|overloaded|timed? ?out|\btimeout\b"
    r"|connection (?:refused|reset|error|aborted|closed)|unreachable"
    r"|stream disconnected|ended without finish_reason",
    re.IGNORECASE,
)
# The registry's auth matcher runs before upstream_unavailable and claims every
# 403 except the relay's generic "Request failed" shell; a 403 that still lands
# on upstream_unavailable is therefore that shell, which is real evidence.
_RELAY_403 = re.compile(r"\b403\b|provider_http_403", re.IGNORECASE)


def classify_failure_text(text: object) -> str:
    """Map free provider/CLI error text to one ``AGENT_CALL_FAILURE_CLASSES`` value."""
    candidate = str(text or "")
    spec = error_contract.classify_text(candidate)
    code = spec.code if spec is not None else "unknown"
    if code == "upstream_unavailable" and not (
        _STRONG_UPSTREAM_EVIDENCE.search(candidate) or _RELAY_403.search(candidate)
    ):
        return "unknown"
    return code if code in AGENT_CALL_FAILURE_CLASSES else "unknown"


def normalize_reason(raw: object) -> str:
    """Return the content-free form of a memory-lane agent-call failure reason.

    ``dream_agent_call_failed:RuntimeError: 401 invalid api key`` becomes
    ``dream_agent_call_failed:auth_invalid``. An already-classified value is
    returned unchanged (idempotent), and any reason without one of the
    ``AGENT_CALL_FAILED_PREFIXES`` is returned exactly as given.
    """
    text = str(raw or "").strip()
    prefix, sep, tail = text.partition(":")
    if prefix not in AGENT_CALL_FAILED_PREFIXES:
        return str(raw or "")
    if not sep:
        return prefix
    tail = tail.strip()
    if tail in AGENT_CALL_FAILURE_CLASSES:
        return f"{prefix}:{tail}"
    return f"{prefix}:{classify_failure_text(tail)}"


def is_agent_call_failed_reason(raw: object) -> bool:
    return str(raw or "").strip().partition(":")[0] in AGENT_CALL_FAILED_PREFIXES
