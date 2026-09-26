"""T732: a relay's exhausted-balance 403 is a quota failure, not an invalid key.

Real body from T729 (relay kimi-k3, new-api family). Before the fix every lane
classified it as an authentication failure and told the user their API key was
invalid.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from notices import error_contract  # noqa: E402

QUOTA_403_BODY = (
    '{"error":{"message":"预扣费额度失败, 用户剩余额度: ¥0.447134, 需要预扣费额度: ¥0.500080 '
    '(request id: 202609252041321931314858268d9d6PNFysoS0)","type":"new_api_error",'
    '"param":"","code":"insufficient_user_quota"}}'
)
QUOTA_403_DETAIL = "预扣费额度失败, 用户剩余额度: ¥0.447134, 需要预扣费额度: ¥0.500080"
AUTH_403_BODY = '{"error":{"message":"Invalid API key","type":"invalid_request_error"}}'
AUTH_AND_QUOTA_403_BODY = '{"error":{"message":"invalid api key; insufficient_user_quota"}}'


class _ProviderError(Exception):
    def __init__(self, status_code, raw_body="", detail=""):
        super().__init__(f"provider_http_{status_code}: {detail or raw_body}")
        self.status_code = status_code
        self.raw_response_body = raw_body
        self.response_detail = detail


@pytest.mark.parametrize("body", [QUOTA_403_BODY, QUOTA_403_DETAIL])
def test_exhausted_balance_403_is_quota_not_auth(body):
    assert error_contract.provider_response_is_quota_exhausted(403, body) is True
    assert error_contract.provider_response_is_auth_failure(403, body) is False


QUOTA_MARKERS = ["insufficient_user_quota", "insufficient_quota", "预扣费额度失败", "余额不足"]


@pytest.mark.parametrize("marker", QUOTA_MARKERS)
def test_each_quota_marker_alone_and_against_explicit_auth(marker):
    body = f'{{"error":{{"message":"{marker}"}}}}'
    assert error_contract.provider_response_is_quota_exhausted(403, body) is True
    assert error_contract.provider_response_is_auth_failure(403, body) is False
    mixed = f'{{"error":{{"message":"Invalid API key; {marker}"}}}}'
    assert error_contract.provider_response_is_quota_exhausted(403, mixed) is False
    assert error_contract.provider_response_is_auth_failure(403, mixed) is True


@pytest.mark.parametrize("body", [
    "forbidden; 用户剩余额度: ¥100.00",          # a balance field alone states no shortfall
    '{"error":{"message":"用户剩余额度: ¥100.00","code":"forbidden"}}',
    "forbidden",
])
def test_balance_field_or_plain_forbidden_keeps_the_fail_closed_default(body):
    assert error_contract.provider_response_is_quota_exhausted(403, body) is False
    assert error_contract.provider_response_is_auth_failure(403, body) is True


@pytest.mark.parametrize("status,body,quota,auth", [
    (401, QUOTA_403_BODY, False, True),        # a real 401 stays authentication
    (403, AUTH_403_BODY, False, True),         # explicit auth evidence wins
    (403, AUTH_AND_QUOTA_403_BODY, False, True),
    (403, "", False, True),                    # unrecognised 403: fail-closed default kept
    (403, "forbidden", False, True),
    (402, "", True, False),
    (429, QUOTA_403_BODY, False, False),
    (True, QUOTA_403_BODY, False, False),
])
def test_quota_reading_is_narrow(status, body, quota, auth):
    assert error_contract.provider_response_is_quota_exhausted(status, body) is quota
    assert error_contract.provider_response_is_auth_failure(status, body) is auth


def _v2_chat(exc):
    from model_api_runtime.v2 import provider_errors
    return provider_errors.error_class_for_exception(exc)


def _provider_health(exc):
    import provider_health
    return provider_health.error_class_for_exception(exc)


def _extraction(exc):
    from model_api_runtime.v2 import extraction
    return extraction._provider_failure_code(exc)


def _vision(exc):
    from hosted import vision_observer
    return vision_observer.classify_vision_error(exc).error_code


def _image(exc):
    from hosted import image_generator
    return image_generator.classify_image_generation_error(exc)


def _genesis_exc(exc):
    from genesis import service
    return service.classify_genesis_error(str(exc), exc)


def _genesis_text(exc):
    from genesis import service
    return service.classify_genesis_error(str(exc))


def _setup_probe(exc):
    from hosted import setup_core
    return setup_core._provider_test_failure_class(exc)


LANES = [
    ("v2_chat", _v2_chat, "quota_insufficient", "auth_invalid"),
    ("provider_health", _provider_health, "quota_insufficient", "auth_invalid"),
    ("extraction", _extraction, "quota_insufficient", "auth_invalid"),
    ("vision", _vision, "vision_model_quota_insufficient", "vision_model_auth_invalid"),
    ("image", _image, "image_generation_quota_insufficient", "image_generation_auth_invalid"),
    ("genesis_exc", _genesis_exc, "provider_quota", "bad_api_key"),
    ("genesis_text", _genesis_text, "provider_quota", "bad_api_key"),
    ("setup_probe", _setup_probe, "quota_insufficient", "auth_invalid"),
]


@pytest.mark.parametrize("name,classify,quota_code,auth_code", LANES, ids=[l[0] for l in LANES])
def test_every_lane_reports_exhausted_balance_as_quota(name, classify, quota_code, auth_code):
    detail = QUOTA_403_DETAIL if name == "genesis_text" else ""
    exc = _ProviderError(403, QUOTA_403_BODY, detail)
    assert classify(exc) == quota_code


DETAIL_LANES = [lane for lane in LANES if lane[0] in {
    "v2_chat", "provider_health", "vision", "image", "setup_probe", "genesis_exc"}]


@pytest.mark.parametrize("name,classify,quota_code,auth_code", DETAIL_LANES, ids=[l[0] for l in DETAIL_LANES])
def test_detail_only_lanes_also_read_quota_evidence(name, classify, quota_code, auth_code):
    """Callers that fall back to response_detail when the raw body is absent."""
    assert classify(_ProviderError(403, "", QUOTA_403_DETAIL)) == quota_code


def test_extraction_reads_the_raw_body_that_provider_client_always_attaches():
    import inspect
    import provider_client
    source = inspect.getsource(provider_client._raise_for_provider_status)
    assert "raw_response_body = resp.text" in source
    assert "raw_response_body=raw_response_body" in source


@pytest.mark.parametrize("text,expected", [
    *[(f"provider_http_403: Invalid API key; {m}", "provider_auth") for m in QUOTA_MARKERS],
    *[(f"provider_http_403: {m}", "quota") for m in QUOTA_MARKERS],
    ("401 unauthorized; insufficient_quota", "provider_auth"),
    ("provider_http_401: insufficient_user_quota", "provider_auth"),
    ("insufficient_quota: add credit", "quota"),                # no status code: old fallback
    ("Your credit balance is too low", "quota"),
    ("provider_http_403: " + QUOTA_403_BODY, "quota"),
    ("403 " + QUOTA_403_DETAIL, "quota"),
    ("provider_http_403: Invalid API key", "provider_auth"),
    ("401 unauthorized", "provider_auth"),
    ("403 forbidden; 用户剩余额度: ¥100.00", "provider_auth"),
])
def test_resident_consumer_attempt_class(text, expected):
    sys.path.insert(0, str(ROOT))
    import os
    os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")
    os.environ.setdefault("FEEDLING_API_KEY", "test_key_00000000")
    from tools import chat_resident_consumer as resident
    assert resident._provider_attempt_error_class(text) == expected


@pytest.mark.parametrize("name,classify,quota_code,auth_code", LANES, ids=[l[0] for l in LANES])
def test_every_lane_keeps_real_auth_failures(name, classify, quota_code, auth_code):
    exc = _ProviderError(403, AUTH_403_BODY, "Invalid API key")
    assert classify(exc) == auth_code
    assert classify(_ProviderError(401, "", "unauthorized")) == auth_code
