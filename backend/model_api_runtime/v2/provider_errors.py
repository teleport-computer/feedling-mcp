"""V2 provider failure causes using the shared Resident/notice vocabulary.

Keep this independent of worker orchestration: model-call telemetry and terminal
turn failures must classify the same exception alike. Retry policy remains in
provider_client.classify_provider_error and has a different, coarser vocabulary.
"""
from __future__ import annotations

import asyncio

import provider_client
from notices import catalog as notices_catalog
from notices import error_contract


def error_class_for_exception(exc: BaseException) -> str:
    """Return a registered cause; exception text is classification-only."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "provider_timeout"
    status_code = getattr(exc, "status_code", None)
    if status_code == 403 and error_contract.provider_response_is_quota_exhausted(
        status_code,
        getattr(exc, "raw_response_body", "") or getattr(exc, "response_detail", ""),
    ):
        return "quota_insufficient"
    if status_code in {401, 403}:
        return (
            "auth_invalid"
            if error_contract.provider_response_is_auth_failure(
                status_code,
                getattr(exc, "raw_response_body", "")
                or getattr(exc, "response_detail", ""),
            )
            else "upstream_unavailable"
        )
    classified = notices_catalog.classify_upstream(str(exc))
    if classified:
        return classified
    if status_code == 402:
        return "quota_insufficient"
    if status_code in {400, 422}:
        return "provider_incompatible"
    if status_code == 408:
        return "provider_timeout"
    if status_code == 429:
        return "rate_limited"
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return "upstream_unavailable"
    if provider_client.classify_provider_error(exc) == "transient":
        return "upstream_unavailable"
    return "unknown"
