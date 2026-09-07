"""Dedicated image generation for resident and hosted chat runtimes."""

from __future__ import annotations

import base64
import logging
import sys
import time
import uuid

import db
import debug_trace
import generated_image
import provider_client
from core import enclave as core_enclave
from core import envelope as core_envelope
from notices import error_contract
from provider_types import ProviderResponse


_MAX_PROMPT_CHARS = 8_000
_EXPECTED_KEY_DECRYPT_VALUE_ERRORS = frozenset({
    "envelope_body_b64_invalid",
    "envelope_owner_mismatch",
    "envelope_shape_unrecognized",
    "plaintext_envelope_required",
})
_EXPECTED_KEY_DECRYPT_RUNTIME_ERRORS = frozenset({
    "api_key_unavailable",
    "enclave_invalid_decrypt_response",
})
log = logging.getLogger(__name__)

_ATTEMPT_EVENT_TYPE = "image_generation.attempt.finished"
_ATTEMPT_STREAM = "image_generation_attempts"
_ATTEMPT_ERROR_CATEGORIES = frozenset({
    "image_generation_auth_invalid",
    "image_generation_failed",
    "image_generation_key_decrypt_failed",
    "image_generation_model_incompatible",
    "image_generation_model_not_found",
    "image_generation_model_not_ready",
    "image_generation_model_required",
    "image_generation_processing_failed",
    "image_generation_quota_insufficient",
    "image_generation_rate_limited",
    "image_generation_test_failed",
    "image_generation_unavailable",
    "model_api_key_decrypt_failed",
    "model_api_key_envelope_missing",
    "model_api_route_write_failed",
})


def new_attempt_id() -> str:
    return f"image_generation:{uuid.uuid4().hex}"


def _safe_error_category(value: object) -> str:
    category = str(value or "").strip()
    if not category:
        return ""
    if category in _ATTEMPT_ERROR_CATEGORIES:
        return category
    return "image_generation_unknown_failure"


def provider_called_for_error(exc: BaseException) -> bool:
    """Distinguish the sole local capability rejection from upstream calls."""
    return str(exc).strip().lower() != "image_generation_model_unsupported"


def _safe_status_code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return status if 100 <= status <= 599 else None


def observe_attempt(
    store,
    *,
    attempt_id: str,
    operation: str,
    provider: str,
    model: str,
    outcome: str,
    provider_called: bool,
    error_category: str = "",
    status_code: object = None,
    dur_ms: float | None = None,
) -> None:
    """Persist content-free image-generation outcome metadata, fail-open.

    ``error_category`` is closed here even though callers already pass only
    local error codes. Provider exception text, prompts, credentials, response
    bodies, and generated media must never enter either sink.
    """
    safe_outcome = "ok" if outcome == "ok" else "failed"
    safe_error = _safe_error_category(error_category)
    safe_operation = str(operation or "unknown")[:48]
    safe_provider = str(provider or "")[:80]
    safe_model = str(model or "")[:160]
    safe_attempt_id = str(attempt_id or "")[:120]
    duration = max(0.0, float(dur_ms or 0.0))
    payload = {
        "attempt_id": safe_attempt_id,
        "operation": safe_operation,
        "provider": safe_provider,
        "model": safe_model,
        "outcome": safe_outcome,
        "error_category": safe_error,
        "provider_called": bool(provider_called),
        "status_code": _safe_status_code(status_code),
        "dur_ms": round(duration, 1),
    }
    signal_failures: list[str] = []
    try:
        debug_trace.trace_event(
            store,
            subsystem="image_generation",
            type=_ATTEMPT_EVENT_TYPE,
            actor="backend",
            status="ok" if safe_outcome == "ok" else "error",
            outcome_class=(
                None if safe_outcome == "ok" else "operational_failure"
            ),
            summary=f"image generation {safe_outcome}",
            trace_id=safe_attempt_id,
            detail=dict(payload),
            dur_ms=duration,
        )
    except Exception:  # noqa: BLE001 - user-log/stderr remain independent
        signal_failures.append("trace_events")

    now = time.time()
    record = {
        "source": "backend",
        **payload,
        "ts": now,
    }
    try:
        stored = db.log_append(
            store.user_id,
            _ATTEMPT_STREAM,
            record,
            ts=now,
            item_key=safe_attempt_id,
        )
    except Exception:  # noqa: BLE001 - stderr remains the independent fallback
        stored = False
    if not stored:
        signal_failures.append("user_logs")

    if safe_outcome != "ok" or signal_failures:
        print(
            f"[image-generation:{store.user_id}] attempt_finished "
            f"attempt_id={safe_attempt_id or '-'} operation={safe_operation} "
            f"provider={safe_provider or '-'} model={safe_model or '-'} "
            f"outcome={safe_outcome} error_category={safe_error or '-'} "
            f"provider_called={str(bool(provider_called)).lower()} "
            f"status_code={payload['status_code'] or '-'} "
            f"signal_failures={','.join(signal_failures) or 'none'}",
            file=sys.stderr,
            flush=True,
        )


def classify_image_generation_error(
    exc: BaseException,
    *,
    dedicated: bool = True,
    fallback_code: str = "image_generation_failed",
) -> str:
    """Map provider failures to stable image-generation codes.

    The generic provider classifier intentionally folds every non-retryable
    4xx into ``provider_config``. Image-generation setup needs the original
    status plus the original 403 body to tell a bad credential, the relay's
    generic unavailable shell, an exhausted account, a missing endpoint/model,
    and an incompatible image wire apart.
    """
    raw = str(exc).strip().lower()
    incompatible_code = (
        "image_generation_model_incompatible"
        if dedicated
        else "image_generation_model_required"
    )
    if raw in {
        "image_generation_model_unsupported",
        "image_generation_invalid_output",
    }:
        return incompatible_code

    status_code = _safe_status_code(getattr(exc, "status_code", None))
    if status_code in {401, 403}:
        if error_contract.provider_response_is_auth_failure(
            status_code,
            getattr(exc, "raw_response_body", "")
            or getattr(exc, "response_detail", ""),
        ):
            return "image_generation_auth_invalid"
        return "image_generation_unavailable"
    if status_code == 402:
        return "image_generation_quota_insufficient"
    if status_code == 404:
        return "image_generation_model_not_found"
    if status_code in {400, 415, 422}:
        return incompatible_code
    classified = str(
        getattr(exc, "feedling_error_class", "")
        or provider_client.classify_provider_error(exc)
        or ""
    )
    return {
        "provider_config": incompatible_code,
        "provider_incompatible": incompatible_code,
    }.get(classified, fallback_code)


def _classify_error(exc: BaseException) -> str:
    return classify_image_generation_error(exc)


def _status_for_error(error_code: str) -> int:
    return {
        "image_generation_model_required": 409,
        "image_generation_model_not_ready": 409,
        "image_generation_key_decrypt_failed": 409,
        "image_generation_auth_invalid": 401,
        "image_generation_quota_insufficient": 402,
        "image_generation_model_not_found": 404,
        "image_generation_rate_limited": 429,
        "image_generation_unavailable": 503,
    }.get(error_code, 400)


def _key_decrypt_failure_code(exc: BaseException) -> str:
    """Map only the documented envelope/enclave failure contract.

    Internal programming errors such as ``TypeError`` must keep escaping so a
    helper signature drift cannot masquerade as a credential problem. Enclave
    availability failures reuse the existing retryable 503 class instead of
    telling the user to save an unchanged provider key again.
    """
    detail = str(exc)
    if detail == "enclave_unavailable" or detail.startswith("enclave_error:"):
        return "image_generation_unavailable"
    if detail.startswith("enclave_http_5"):
        return "image_generation_unavailable"
    if isinstance(exc, UnicodeDecodeError):
        return "image_generation_key_decrypt_failed"
    if isinstance(exc, ValueError) and detail in _EXPECTED_KEY_DECRYPT_VALUE_ERRORS:
        return "image_generation_key_decrypt_failed"
    if isinstance(exc, RuntimeError) and (
        detail in _EXPECTED_KEY_DECRYPT_RUNTIME_ERRORS
        or detail.startswith("enclave_http_")
        or detail.startswith("enclave_plaintext_decode:")
    ):
        return "image_generation_key_decrypt_failed"
    return ""


def normalize_provider_media(result: object) -> list[dict[str, str]]:
    """Validate and normalize the exact media shape used by real generation.

    This is deliberately outside every provider-classification ``try``. Shape,
    decode, and raster-normalization failures are our processing failures, not
    evidence that the user's saved provider route is bad.
    """
    media = ProviderResponse.from_result(result).media
    images: list[dict[str, str]] = []
    for index, item in enumerate(
        media[: generated_image.MAX_GENERATED_IMAGES_PER_REPLY], start=1
    ):
        normalized = generated_image.normalize_generated_image(
            generated_image.decode_base64_image(item.data_base64),
            declared_mime=item.mime_type,
            name=item.name,
            index=index,
        )
        images.append({
            "mime_type": normalized.mime_type,
            "data_base64": base64.b64encode(normalized.data).decode("ascii"),
            "name": normalized.name,
        })
    return images


def _provider_failure_response(
    store,
    *,
    route_id: str,
    provider: str,
    model: str,
    exc: BaseException,
) -> tuple[dict, int]:
    """Persist a route verdict only for a failure from the provider seam."""
    code = _classify_error(exc)
    log.warning(
        "[image-generation] provider call failed user=%s provider=%s model=%s "
        "error=%s code=%s",
        str(store.user_id)[:8],
        provider[:80],
        model[:96],
        type(exc).__name__,
        code,
    )
    if route_id:
        db.model_api_route_mark_image_generation_test(
            store.user_id,
            route_id,
            status=(
                "unsupported"
                if code == "image_generation_model_incompatible"
                else "failed"
            ),
            error=code,
        )
    return {
        "error": code,
        "error_class": code,
        "provider": provider[:80],
        "model": model[:96],
    }, _status_for_error(code)


def generate_with_pinned_route(
    store,
    payload: dict,
    *,
    caller_api_key: str | None,
    caller_runtime_token: str = "",
) -> tuple[dict, int]:
    """Generate bounded inline media using only the configured dedicated route."""
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt or len(prompt) > _MAX_PROMPT_CHARS:
        code = "image_generation_invalid_prompt"
        return {"error": code, "error_class": code}, 400

    route = db.model_api_image_generation_route(store.user_id)
    if not isinstance(route, dict):
        code = "image_generation_model_required"
        return {"error": code, "error_class": code}, _status_for_error(code)

    provider = str(route.get("provider") or "")
    model = str(route.get("model") or "")
    route_id = str(route.get("id") or "")
    attempt_id = new_attempt_id()
    started = time.monotonic()

    def observe(
        outcome: str,
        *,
        error_category: str = "",
        provider_called: bool = False,
        status_code: object = None,
    ) -> None:
        observe_attempt(
            store,
            attempt_id=attempt_id,
            operation="runtime_generate",
            provider=provider,
            model=model,
            outcome=outcome,
            error_category=error_category,
            provider_called=provider_called,
            status_code=status_code,
            dur_ms=(time.monotonic() - started) * 1000.0,
        )

    if str(route.get("image_generation_test_status") or "") != "ok":
        code = "image_generation_model_not_ready"
        observe("failed", error_category=code)
        return {
            "error": code,
            "error_class": code,
            "provider": provider[:80],
            "model": model[:96],
        }, _status_for_error(code)

    envelope = route.get("api_key_envelope")
    if not isinstance(envelope, dict):
        code = "image_generation_model_not_ready"
        observe("failed", error_category=code)
        return {"error": code, "error_class": code}, _status_for_error(code)

    try:
        provider_key = core_envelope.decrypt_provider_key_envelope(
            envelope,
            caller_api_key,
            caller_user_id=str(store.user_id),
            runtime_token=caller_runtime_token,
        ).decode("utf-8")
    except (RuntimeError, ValueError) as exc:
        code = _key_decrypt_failure_code(exc)
        if not code:
            observe(
                "failed",
                error_category="image_generation_processing_failed",
            )
            raise
        observe("failed", error_category=code)
        return {
            "error": code,
            "error_class": code,
            "provider": provider[:80],
            "model": model[:96],
        }, _status_for_error(code)
    except Exception:
        observe(
            "failed",
            error_category="image_generation_processing_failed",
        )
        raise
    config = provider_client.ProviderConfig(
        provider,
        model,
        provider_key,
        str(route.get("base_url") or ""),
        context_window_tokens=route.get("context_window_tokens"),
        reasoning_effort=str(route.get("reasoning_effort") or ""),
    )
    try:
        result = provider_client.generate_image(config, prompt)
    except Exception as exc:  # noqa: BLE001 - this try contains only provider I/O
        provider_called = provider_called_for_error(exc)
        observe(
            "failed",
            error_category=_classify_error(exc),
            provider_called=provider_called,
            status_code=(
                getattr(exc, "status_code", None) if provider_called else None
            ),
        )
        return _provider_failure_response(
            store,
            route_id=route_id,
            provider=provider,
            model=model,
            exc=exc,
        )

    try:
        images = normalize_provider_media(result)
    except Exception:
        observe(
            "failed",
            error_category="image_generation_processing_failed",
            provider_called=True,
        )
        raise
    if not images:
        observe(
            "failed",
            error_category="image_generation_model_incompatible",
            provider_called=True,
        )
        return _provider_failure_response(
            store,
            route_id=route_id,
            provider=provider,
            model=model,
            exc=provider_client.ProviderError("image_generation_invalid_output"),
        )

    if route_id:
        db.model_api_route_mark_image_generation_test(
            store.user_id,
            route_id,
            status="ok",
        )
    observe("ok", provider_called=True)
    return {
        "images": images,
        "provider": provider[:80],
        "model": model[:96],
    }, 200
