"""Dedicated image generation for resident and hosted chat runtimes."""

from __future__ import annotations

import base64

import db
import generated_image
import provider_client
from core import enclave as core_enclave
from core import envelope as core_envelope
from provider_types import ProviderResponse


_MAX_PROMPT_CHARS = 8_000


class ImageGeneratorError(RuntimeError):
    """Stable, display-safe failure returned to a resident consumer."""

    def __init__(self, error_code: str, *, provider: str = "", model: str = ""):
        super().__init__(error_code)
        self.error_code = str(error_code or "image_generation_failed")[:64]
        self.provider = str(provider or "")[:80]
        self.model = str(model or "")[:96]


def _classify_error(exc: BaseException) -> str:
    classified = provider_client.classify_provider_error(exc)
    raw = str(exc).strip().lower()
    incompatible = classified in {"provider_config", "provider_incompatible"} or raw in {
        "image_generation_model_unsupported",
        "image_generation_invalid_output",
    }
    if incompatible:
        return "image_generation_model_incompatible"
    return {
        "auth_invalid": "image_generation_auth_invalid",
        "quota_insufficient": "image_generation_quota_insufficient",
        "model_not_found": "image_generation_model_not_found",
        "rate_limited": "image_generation_rate_limited",
        "upstream_unavailable": "image_generation_unavailable",
        "turn_timeout": "image_generation_unavailable",
    }.get(classified, "image_generation_failed")


def _status_for_error(error_code: str) -> int:
    return {
        "image_generation_model_required": 409,
        "image_generation_model_not_ready": 409,
        "image_generation_auth_invalid": 401,
        "image_generation_quota_insufficient": 402,
        "image_generation_model_not_found": 404,
        "image_generation_rate_limited": 429,
        "image_generation_unavailable": 503,
    }.get(error_code, 400)


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
    if str(route.get("image_generation_test_status") or "") != "ok":
        code = "image_generation_model_not_ready"
        return {
            "error": code,
            "error_class": code,
            "provider": provider[:80],
            "model": model[:96],
        }, _status_for_error(code)

    envelope = route.get("api_key_envelope")
    if not isinstance(envelope, dict):
        code = "image_generation_model_not_ready"
        return {"error": code, "error_class": code}, _status_for_error(code)

    try:
        provider_key = core_envelope.decrypt_provider_key_envelope(
            envelope,
            caller_api_key,
            runtime_token=caller_runtime_token,
        ).decode("utf-8")
        config = provider_client.ProviderConfig(
            provider,
            model,
            provider_key,
            str(route.get("base_url") or ""),
            context_window_tokens=route.get("context_window_tokens"),
            reasoning_effort=str(route.get("reasoning_effort") or ""),
        )
        result = provider_client.generate_image(config, prompt)
        media = ProviderResponse.from_result(result).media
        if not media:
            raise provider_client.ProviderError("image_generation_invalid_output")

        images = []
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
        if not images:
            raise provider_client.ProviderError("image_generation_invalid_output")
    except Exception as exc:  # noqa: BLE001 - stable capability contract
        code = _classify_error(exc)
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

    if route_id:
        db.model_api_route_mark_image_generation_test(
            store.user_id,
            route_id,
            status="ok",
        )
    return {
        "images": images,
        "provider": provider[:80],
        "model": model[:96],
    }, 200
