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
(``notices.error_contract`` matchers, in registry order) plus a strong-evidence
gate per class: a CLI failure tail can echo the request, so a bare registry
keyword ("authentication", "quota", "model not found", a 5xx-looking number)
is not enough — the class also needs an explicit provider error code, an HTTP
status in a status position, or the term inside a JSON error field.

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
# The relay's generic "Request failed. Please try again later." 403 shell (the
# registry's exact, whole-candidate shape, Seven's T497) is real upstream evidence.
_RELAY_403 = re.compile(error_contract._GENERIC_UPSTREAM_403, re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Strong evidence for the account/request classes
# --------------------------------------------------------------------------- #
#
# The registry matchers are tuned for provider error strings, where a bare word
# ("authentication", "quota", "model not found") is reliable. A resident agent
# failure tail is free CLI text that can echo the request — a prompt about
# "quota planning" or a payload saying "model not found" must not tell the user
# their key, balance or model is broken. So every class below additionally
# needs one of: an explicit provider error code, an HTTP status in a status
# position (``HTTP 401``, ``api_status=401``, ``provider_http_402``, a status
# leading the message, ``401 Unauthorized``, a Chinese relay's ``错误码 401：``
# / ``状态码 401``), or the term inside a JSON error field
# (``{"error": "Insufficient balance"}``, ``{"msg": "鉴权失败"}``). When the
# registry's first match lacks that evidence, the next registry match is tried;
# nothing left means ``unknown``.
#
# 403 follows the chat registry (Seven's T497/T504): every 403 except the
# relay's generic "Request failed" shell is auth. Keep the two sides identical.


def _status_position(codes: str) -> str:
    return (
        rf"provider_http_(?:{codes})\b"
        rf"|\b(?:https?|status(?:[ _-]?code)?|error[ _-]?code|api[ _-]?error"
        rf"|api_status|code)\b\W{{0,3}}(?:{codes})\b"
        # Chinese relay labels: 错误码 401：… / 状态码：401 / HTTP 状态码 403
        rf"|(?:错误码|错误代码|状态码|返回码)\s*[:：=]?\s*(?:{codes})\b"
        # a status leading the message after an exception type / CLI prefix
        rf"|(?:\A|:\s)(?:{codes})\b(?=\s*(?:[-:：{{(]|[A-Za-z]|[\u4e00-\u9fff]))"
    )


def _json_error_field(term: str) -> str:
    return (
        r"""["'](?:error|message|errorMessage|error_msg|errmsg|msg|detail|code|type)["']"""
        r"""\s*:\s*"""
        rf"""(?:\{{[^}}]{{0,200}}?)?["'][^"']{{0,200}}?(?:{term})"""
    )


def _evidence(*, codes: str, term: str, tokens: str = "") -> re.Pattern:
    """``tokens`` alone suffice; ``term`` needs a nearby status or a JSON error field."""
    parts = [
        rf"(?:{_status_position(codes)})[\s\S]{{0,160}}?(?:{term})",
        _json_error_field(term),
    ]
    if tokens:
        parts.insert(0, tokens)
    return re.compile("|".join(parts), re.IGNORECASE)


_QUOTA_TERM = (
    r"insufficient[ _-]?(?:balance|quota|credits?|funds)|quota|credit balance"
    r"|余额|额度|配额|payment required|out of credits|requires more credits"
)
# Auth semantics only. "forbidden" is the reason phrase of every 403 and
# "blocked" / "permission" describe content, WAF or region blocks just as often,
# so none of them makes a 403 an invalid key.
_CHINESE_AUTH_TERM = (
    r"(?:api\s*)?(?:密钥|秘钥|令牌|token)\s*(?:无效|错误|不正确|已失效|已过期)"
    r"|无效的?\s*(?:api\s*)?(?:密钥|秘钥|令牌|token)"
    r"|鉴权失败|认证失败|身份验证失败|未授权|未经授权"
)
_AUTH_TERM = (
    r"unauthori[sz]ed|authentication|invalid[ _-]?(?:x-)?api[ _-]?key|invalid[ _-]?key"
    r"|incorrect api key|api key not valid|" + _CHINESE_AUTH_TERM
)
_MODEL_TERM = (
    r"model[ _-]?not[ _-]?found|no such model|unknown model|invalid model name"
    r"|not a valid model|does not exist|model"
)

_STRONG_EVIDENCE: dict[str, re.Pattern] = {
    "quota_insufficient": _evidence(
        codes="401|402|403|429",
        term=_QUOTA_TERM,
        tokens=(
            r"provider_http_402\b|insufficient_quota|insufficient_user_quota"
            r"|insufficient_balance|billing_hard_limit_reached"
            r"|credit balance is too low"
            rf"|{_status_position('402')}"
        ),
    ),
    "provider_account_expired": _evidence(
        codes="401|402|403", term=r"expired", tokens=r"account_expired",
    ),
    "auth_invalid": _evidence(
        codes="401|403",
        term=_AUTH_TERM,
        tokens=(
            r"invalid_api_key|authentication_error|incorrect api key"
            r"|invalid[ _-]?(?:x-)?api[ _-]?key|failed to authenticate"
            # 403 同聊天侧（Seven 的 T497/T504 规则）：除中转站通用 403 空壳外都算鉴权。
            rf"|{_status_position('401|403')}"
        ),
    ),
    "model_not_found": _evidence(
        codes="400|404|422", term=_MODEL_TERM,
        tokens=rf"\bmodel_not_found\b|{_status_position('404')}",
    ),
    "provider_incompatible": _evidence(
        codes="400|404|415|422",
        term=r"not supported|unsupported|unknown variant|invalid_request_error",
        tokens=r"unsupported_parameter|unsupported_value",
    ),
    "context_overflow": _evidence(
        codes="400|413|422",
        term=r"context|too many tokens|prompt is too long",
        tokens=(
            r"context_length_exceeded|maximum context length is \d+"
            r"|prompt is too long: \d+ tokens"
        ),
    ),
    "content_filtered": _evidence(
        codes="400|403|422",
        term=r"content[ _]?filter|content policy|safety|blocked by",
        tokens=r"\bcontent_filter\b|content_policy_violation",
    ),
    "rate_limited": _evidence(
        codes="429",
        term=r"too many requests|rate.?limit",
        tokens=(
            rf"rate_limit_exceeded|rate_limit_error|{_status_position('429')}"
            r"|\b429\s+too many requests"
        ),
    ),
}
# The pi relay's whole provider message is "invalid key"; only that position
# ("RuntimeError: invalid key", end of text) counts, not the phrase in prose.
_BARE_INVALID_KEY_MESSAGE = re.compile(r":\s*invalid[ _-]?key\s*\.?\s*\Z", re.IGNORECASE)
# Emitted only by resident code with an exact, non-echoable phrase (the
# registry matchers are the whole sentence), so no extra evidence is required.
_SELF_EVIDENT = frozenset({"cli_config_invalid", "resident_agent_cli_logged_out"})
_CHINESE_AUTH_IN_ERROR_FIELD = re.compile(
    _json_error_field(_CHINESE_AUTH_TERM), re.IGNORECASE
)


_INSUFFICIENT_BALANCE = re.compile(r"insufficient[ _-]?balance", re.IGNORECASE)


def _has_strong_evidence(code: str, text: str) -> bool:
    if code in _SELF_EVIDENT:
        return True
    if code == "upstream_unavailable":
        return bool(_STRONG_UPSTREAM_EVIDENCE.search(text) or _RELAY_403.search(text))
    pattern = _STRONG_EVIDENCE.get(code)
    return bool(pattern is not None and pattern.search(text))


def classify_failure_text(text: object) -> str:
    """Map free provider/CLI error text to one ``AGENT_CALL_FAILURE_CLASSES`` value.

    Walks the producer-owned registry (``error_contract.matcher_specs``) in its
    own order and returns the first matching class that also has strong
    evidence in ``text`` (see ``_STRONG_EVIDENCE``).
    """
    candidate = str(text or "")
    # Before the registry: a relay answering "401 {"error":"Insufficient balance"}"
    # is out of money, not a bad key. The shared chat registry (Seven's) is left
    # untouched; memory lanes recognise this shape here, with the same JSON-error /
    # status evidence every other class needs.
    if _STRONG_EVIDENCE["quota_insufficient"].search(candidate) and _INSUFFICIENT_BALANCE.search(candidate):
        return "quota_insufficient"
    for spec in error_contract.matcher_specs():
        if spec.code not in AGENT_CALL_FAILURE_CLASSES:
            continue
        matcher = spec.matcher()
        if matcher is None or not matcher.search(candidate):
            continue
        if _has_strong_evidence(spec.code, candidate):
            return spec.code
    # Outside the registry: the pi relay's bare "invalid key" message, which the
    # resident consumer itself already treats as an auth failure.
    if _BARE_INVALID_KEY_MESSAGE.search(candidate):
        return "auth_invalid"
    # Outside the registry: a Chinese auth message with no English keyword or
    # status for the registry to match, but inside a JSON error field
    # (``{"code": "invalid_token", "msg": "鉴权失败"}``). Never the bare words.
    if _CHINESE_AUTH_IN_ERROR_FIELD.search(candidate):
        return "auth_invalid"
    return "unknown"


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
