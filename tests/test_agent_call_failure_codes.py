"""Resident memory-lane agent-call failures become content-free codes (Bug 20).

Before: the resident consumer wrote
``dream_agent_call_failed:RuntimeError: <provider error text>``; every
content-free surface replaced the whole value with ``runtime_failed``, so ~135
weekly V1 dream failures that were users' own account problems (revoked key,
empty balance, retired model) were indistinguishable from Feedling bugs.

After: the backend classifies the tail with the producer-owned error registry
at write time (status endpoint) and, for rows written before that, at rollup
freeze time. Raw provider text never reaches the stored code.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

import db  # noqa: E402
from notices import agent_call_failure  # noqa: E402
from notices import catalog as notices_catalog  # noqa: E402
from notices import error_contract  # noqa: E402
from notices import status_reason  # noqa: E402
from proactive import proactive_core  # noqa: E402

SECRET = "PRIVATE-PROMPT-ECHO-7f3a"


@pytest.mark.parametrize(
    ("raw_tail", "expected"),
    [
        (f"RuntimeError: pi agent produced no reply: 401 Unauthorized {SECRET}", "auth_invalid"),
        (f"RuntimeError: cli agent exited 1: invalid api key {SECRET}", "auth_invalid"),
        # Relays answer an empty balance with 401; balance evidence must win.
        (f"RuntimeError: HTTP 401: Insufficient balance {SECRET}", "quota_insufficient"),
        (f'RuntimeError: 403 {{"error":{{"message":"insufficient_user_quota"}}}} {SECRET}', "quota_insufficient"),
        (f"RuntimeError: API Error: 402 Payment Required {SECRET}", "quota_insufficient"),
        (f"RuntimeError: Error code: 404 - model_not_found: gpt-9 {SECRET}", "model_not_found"),
        (f"RuntimeError: 429 Too Many Requests {SECRET}", "rate_limited"),
        (f"RuntimeError: status 503 Service Unavailable {SECRET}", "upstream_unavailable"),
        (f"RuntimeError: provider_http_502 {SECRET}", "upstream_unavailable"),
        (f"TimeoutExpired: Command timed out after 600 seconds {SECRET}", "upstream_unavailable"),
        (f"RuntimeError: Connection refused {SECRET}", "upstream_unavailable"),
        (f"RuntimeError: Not logged in · Please run /login {SECRET}", "resident_agent_cli_logged_out"),
        # A bare 5xx-looking number is not upstream evidence.
        (f"RuntimeError: wrote 500 tokens then gave up {SECRET}", "unknown"),
        (f"ValueError: something odd {SECRET}", "unknown"),
        ("RuntimeError", "unknown"),
        # Real prod tails (09-2026) keep their class.
        ('RuntimeError: cli agent exited 1: Failed to authenticate. API Error: 401 '
         '{"error":"Insufficient balance"} (api_status=401)', "quota_insufficient"),
        ("RuntimeError: Failed to authenticate: OAuth session expired and could not "
         "be refreshed", "resident_agent_cli_logged_out"),
        # A relay/WAF block: 403 alone carries no auth semantics and there is no
        # policy wording either, so the class is honestly unknown (it used to
        # tell the user their API key was invalid).
        ("RuntimeError: 403 Your request was blocked", "unknown"),
        (f"RuntimeError: HTTP 403 content policy blocked this request {SECRET}", "content_filtered"),
        (f"RuntimeError: 403 Forbidden: invalid api key {SECRET}", "auth_invalid"),
        (f'RuntimeError: HTTP 403 {{"error":{{"message":"Unauthorized"}}}} {SECRET}', "auth_invalid"),
        # Chinese relay shapes: a status label / JSON error field AND auth or
        # quota words.
        (f"RuntimeError: 错误码 401：API 密钥无效 {SECRET}", "auth_invalid"),
        (f"RuntimeError: 状态码 401：鉴权失败 {SECRET}", "auth_invalid"),
        (f"RuntimeError: 401 鉴权失败 {SECRET}", "auth_invalid"),
        (f"RuntimeError: 状态码 403：令牌无效 {SECRET}", "auth_invalid"),
        (f'RuntimeError: {{"code":"invalid_token","msg":"鉴权失败，请检查令牌"}} {SECRET}', "auth_invalid"),
        (f'RuntimeError: {{"error":{{"code":401,"message":"未授权"}}}} {SECRET}', "auth_invalid"),
        (f"RuntimeError: 错误码 402：余额不足 {SECRET}", "quota_insufficient"),
        (f"RuntimeError: 状态码 429：额度不足，请充值 {SECRET}", "quota_insufficient"),
        (f'RuntimeError: {{"code":403,"msg":"账户余额不足"}} {SECRET}', "quota_insufficient"),
        ("RuntimeError: invalid key", "auth_invalid"),
        ('RuntimeError: {"error": {"message": "Insufficient balance"}}', "quota_insufficient"),
        ("RuntimeError: HTTP 400: context_length_exceeded", "context_overflow"),
    ],
)
def test_memory_lane_reasons_classify_into_content_free_codes(raw_tail, expected):
    for prefix in sorted(agent_call_failure.AGENT_CALL_FAILED_PREFIXES):
        code = agent_call_failure.normalize_reason(f"{prefix}:{raw_tail}")
        assert code == f"{prefix}:{expected}"
        assert SECRET not in code
        assert db._LANE_ROLLUP_CODE_RE.fullmatch(code)
        # Display redaction keeps the whole classified code.
        assert status_reason.sanitize_status_reason(code) == code


@pytest.mark.parametrize(
    "raw_tail",
    [
        # A failure tail can echo the request. A registry keyword in that echo
        # must not blame the user's key, balance or model.
        f"RuntimeError: agent failed; prompt text says insufficient balance between goals {SECRET}",
        f"RuntimeError: unknown field authentication {SECRET}",
        f"RuntimeError: prompt mentions quota planning {SECRET}",
        f"RuntimeError: payload contained model not found in user text {SECRET}",
        f"RuntimeError: prompt mentioned 403 items {SECRET}",
        f"KeyError: invalid key 'mood' in payload {SECRET}",
        f"RuntimeError: echo: the model does not exist in our story {SECRET}",
        f"RuntimeError: user wrote about content policy and safety {SECRET}",
        f"RuntimeError: prompt says rate limit yourself {SECRET}",
        f"RuntimeError: that feature is not supported in the diary {SECRET}",
        # Bare 403 / "Forbidden" / "blocked" / "permission" are not auth semantics.
        f"RuntimeError: HTTP 403 Forbidden {SECRET}",
        f"RuntimeError: provider_http_403: request blocked {SECRET}",
        f"RuntimeError: status 403: permission to use this region is restricted {SECRET}",
        # A 403 plus an unrelated 5xx-looking number is not upstream evidence.
        f"RuntimeError: 403 agent wrote 500 tokens {SECRET}",
        # Chinese auth/quota words without a status label or JSON error field.
        f"RuntimeError: 用户说他的密钥无效，还说余额不足 {SECRET}",
        f"RuntimeError: prompt 里写着 鉴权失败 和 未授权 {SECRET}",
        f"RuntimeError: 日记：今天额度不足 {SECRET}",
    ],
)
def test_request_echo_keywords_without_error_evidence_are_unknown(raw_tail):
    for prefix in sorted(agent_call_failure.AGENT_CALL_FAILED_PREFIXES):
        assert agent_call_failure.normalize_reason(f"{prefix}:{raw_tail}") == f"{prefix}:unknown"


def test_echoed_keyword_does_not_hide_a_real_error_later_in_the_tail():
    """The first registry match lacking evidence falls through to the next one."""
    raw = (
        f"RuntimeError: unknown field authentication {SECRET}; "
        "upstream answered 503 Service Unavailable"
    )
    assert agent_call_failure.classify_failure_text(raw) == "upstream_unavailable"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Chat / resident notice: a 403 whose message is a content-policy block
        # used to stop at auth_invalid ("API Key 无效…重新保存").
        ("HTTP 403 content policy blocked this request", "content_filtered"),
        ("provider_http_403: blocked by safety system", "content_filtered"),
        ('403 {"error":{"type":"content_filter","message":"flagged"}}', "content_filtered"),
        # Explicit auth words still win over policy wording.
        ("HTTP 403 unauthorized: content policy", "auth_invalid"),
        ("401 invalid api key", "auth_invalid"),
        ("cli agent exited 1: unexpected status 401 Unauthorized", "auth_invalid"),
        # The historical fail-closed bare 403 (T497/T504 ruling) is unchanged
        # when there is no policy wording.
        ("provider_http_403: forbidden", "auth_invalid"),
        ("403 Your request was blocked", "auth_invalid"),
        ("provider_http_403: Request failed. Please try again later.", "upstream_unavailable"),
    ],
)
def test_registry_403_with_content_policy_wording_is_not_auth(text, expected):
    assert error_contract.classify_text(text).code == expected
    assert notices_catalog.classify_upstream(text) == expected


def test_normalization_is_idempotent_and_leaves_other_reasons_alone():
    code = "dream_agent_call_failed:auth_invalid"
    assert agent_call_failure.normalize_reason(code) == code
    assert agent_call_failure.normalize_reason("dream_agent_call_failed") == (
        "dream_agent_call_failed"
    )
    for untouched in (
        "json_decode_error",
        "dream_nothing_to_consolidate",
        f"agent_call_failed: 401 {SECRET}",  # chat lane: out of scope, unchanged
        "",
    ):
        assert agent_call_failure.normalize_reason(untouched) == untouched


def test_every_class_is_a_registered_displayable_error_class():
    registered = {spec.code for spec in error_contract.public_specs()}
    assert agent_call_failure.AGENT_CALL_FAILURE_CLASSES <= registered
    assert {
        "auth_invalid", "quota_insufficient", "model_not_found",
        "rate_limited", "upstream_unavailable", "unknown",
    } <= agent_call_failure.AGENT_CALL_FAILURE_CLASSES


def test_registry_reads_insufficient_balance_as_quota_for_every_lane():
    """The shared matcher change also fixes Chat's notice for 401+balance."""
    assert notices_catalog.classify_upstream("401 Insufficient Balance") == (
        "quota_insufficient"
    )
    assert notices_catalog.classify_upstream("401 Unauthorized") == "auth_invalid"


def test_status_patch_stores_only_the_code_in_every_reason_field():
    raw = f"dream_agent_call_failed:RuntimeError: 401 invalid api key {SECRET}"
    patch = proactive_core._job_status_patch({
        "status": "failed",
        "reason": raw,
        "dream_result": {"status": "failed", "reason": raw, "job_kind": "memory_dream"},
        "capture_result": {"status": "failed", "reason": raw},
        "noop_reason": raw,
    })
    expected = "dream_agent_call_failed:auth_invalid"
    assert patch["status_reason"] == expected
    assert patch["noop_reason"] == expected
    assert patch["dream_result"] == {
        "status": "failed", "reason": expected, "job_kind": "memory_dream",
    }
    assert patch["capture_result"]["reason"] == expected
    assert SECRET not in repr(patch)


def test_status_patch_keeps_non_agent_call_reasons_byte_for_byte():
    patch = proactive_core._job_status_patch({
        "status": "failed",
        "reason": "json_decode_error",
        "dream_result": {"status": "failed", "reason": "json_decode_error"},
        "noop_reason": "json_decode_error",
    })
    assert patch["status_reason"] == "json_decode_error"
    assert patch["noop_reason"] == "json_decode_error"
    assert patch["dream_result"] == {"status": "failed", "reason": "json_decode_error"}


def test_rollup_code_keeps_class_for_legacy_rows_and_still_logs_raw(caplog):
    raw = f"dream_agent_call_failed:RuntimeError: HTTP 401: Insufficient balance {SECRET}"
    with caplog.at_level("WARNING", logger=db.log.name):
        code = db.content_free_failure_code(
            raw, source="lane_rollup_v1", user_id="usr_x", lane="dream",
            day="2030-06-01",
        )
    assert code == "dream_agent_call_failed:quota_insufficient"
    assert "replacement=dream_agent_call_failed:quota_insufficient" in caplog.text
    # Other free text still collapses exactly as before.
    assert db.content_free_failure_code(
        f"Provider Error {SECRET}", source="lane_rollup_v1", user_id="usr_x",
        lane="heartbeat", day="2030-06-01",
    ) == "runtime_failed"
