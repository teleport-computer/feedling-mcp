from __future__ import annotations

import asyncio
import contextvars
import copy
import http.cookiejar as _cookiejar
import json
import logging
import math
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx

from generated_image import MAX_GENERATED_IMAGES_PER_REPLY
from provider_types import ToolExchange

if TYPE_CHECKING:
    from provider_types import ToolSpec


log = logging.getLogger(__name__)


class ProviderError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# --- Genesis v2 Step 1: shared retry wrapper + failure classification ---------
# A NEW explicit wrapper. The default `chat_completion` behaviour is UNCHANGED —
# callers opt in (genesis first; dream/capture/model_api can adopt later), so the
# blast radius is small. Why it exists: cheap relay providers fail transiently
# (timeout / 429 / 5xx / empty reply) across the dozens of serial LLM calls a
# genesis import makes, and today one blip kills the whole job. Retry the
# transient ones; NEVER retry user-config ones (402 out-of-credits / 401·403 bad
# key / 4xx config) — those need the user to fix their provider, not us to hammer it.
_RETRYABLE_HTTPX = (httpx.TimeoutException, httpx.TransportError)
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_PROVIDER_CONFIG_STATUS = frozenset({400, 401, 402, 403, 404, 422})
_MAX_PG_BIGINT = (1 << 63) - 1


def classify_provider_error(exc: BaseException) -> str:
    """Classify an LLM-call failure for retry decisions.

    - "transient"       → retry (network/timeout, 429, 5xx, empty / no-usable / bad-json reply)
    - "provider_config" → DON'T retry; user must fix key / credits / config
                          (402 out of credits, 401·403 bad key, other 4xx config)
    - "unknown"         → treat as transient but capped (better a few retries than
                          silently giving up on an unrecognised blip)
    """
    if isinstance(exc, _RETRYABLE_HTTPX):
        return "transient"
    if isinstance(exc, ProviderError):
        sc = exc.status_code
        if sc in _RETRYABLE_STATUS:
            return "transient"
        if sc in _PROVIDER_CONFIG_STATUS:
            return "provider_config"
        if sc is None:
            # No HTTP status = shape error (empty / no usable reply / bad JSON) —
            # almost always a relay returning garbage; worth a few retries.
            return "transient"
        return "unknown"
    return "unknown"


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Best-effort 429 Retry-After (seconds) if the error carried it. (ProviderError
    doesn't populate `.retry_after` yet — this is the hook for when it does.)"""
    if getattr(exc, "status_code", None) != 429:
        return None
    raw = getattr(exc, "retry_after", None)
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def is_timeout_error(exc: BaseException) -> bool:
    """Recognize both raw httpx timeouts and adapter-wrapped ProviderErrors."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.TimeoutException):
            return True
        current = current.__cause__ or current.__context__
    normalized = re.sub(r"[^a-z]+", "", str(exc).lower())
    return "timeout" in normalized or "deadlineexceeded" in normalized


def reliable_chat_completion(
    *args: Any,
    max_attempts: int = 3,
    max_timeout_attempts: int | None = None,
    base_delay_sec: float = 1.0,
    max_delay_sec: float = 30.0,
    **kwargs: Any,
) -> Any:
    """`chat_completion` + bounded retry on *transient* failures only.

    Exponential backoff (base·3^n) + jitter, capped; honours 429 Retry-After when
    present. NEVER retries `provider_config` failures. On final failure the raised
    exception carries `.feedling_error_class` ("transient_exhausted" | "provider_config")
    so the caller can label the job. Blocking sleeps — only safe off the request
    path (genesis CVM worker). Default `chat_completion` is untouched; opt-in.
    """
    attempts = max(1, int(max_attempts))
    timeout_attempts_limit = (
        attempts
        if max_timeout_attempts is None
        else max(1, min(int(max_timeout_attempts), attempts))
    )
    timeout_attempts = 0
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = chat_completion(*args, **kwargs)
            return _with_reliable_retry_count(result, attempt - 1)
        except Exception as exc:  # noqa: BLE001 — classify, then re-raise or retry
            cls = classify_provider_error(exc)
            last_exc = exc
            is_timeout = is_timeout_error(exc)
            if is_timeout:
                timeout_attempts += 1
            timeout_exhausted = is_timeout and timeout_attempts >= timeout_attempts_limit
            if cls == "provider_config" or attempt >= attempts or timeout_exhausted:
                exc.feedling_error_class = (
                    "provider_config"
                    if cls == "provider_config"
                    else "transient_exhausted"
                )
                raise
            delay = min(base_delay_sec * (3 ** (attempt - 1)), max_delay_sec)
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                delay = min(max(delay, retry_after), max_delay_sec)
            time.sleep(delay + random.uniform(0.0, 0.5 * delay))
    assert last_exc is not None  # loop always sets it before this point
    raise last_exc


# Process-wide pooled HTTP client for outbound provider calls. Previously every
# chat_completion opened `with httpx.Client(...)` and closed it on exit, so each
# call redid DNS + TCP + TLS from scratch. From the prod CVM that cold path cost
# 13-31s per call (vs ~0.1s from a laptop), which is the bulk of a slow reply.
# A shared client keeps connections alive and pools them per-origin (httpx keys
# the pool by scheme/host/port), so back-to-back calls to the same provider skip
# the handshake. httpx.Client is thread-safe for issuing requests, which matters
# because both the gunicorn backend (threads) and the threaded enclave call in.
# Timeout stays per-request (passed to .post) since it varies by call site.
class _NoStoreCookieJar(_cookiejar.CookieJar):
    """A cookie jar that silently drops every cookie.

    The provider HTTP clients below are process-wide and shared across ALL
    users' BYOK calls. httpx's default jar keys cookies by ORIGIN, not by user,
    so a relay/proxy that Set-Cookies a session cookie (sticky-session,
    cf_clearance, or a cookie-form session credential) on user A's response
    would replay it on user B's request to the same host — cross-user
    credential bleed, and many users point base_url at the same relay domain.
    Connection pooling / keep-alive is a transport concern (keyed per-origin,
    independent of cookies), so refusing to store cookies costs nothing.
    """

    def set_cookie(self, cookie):  # noqa: D401 — no-op on purpose
        return None

    def extract_cookies(self, response, request):  # noqa: D401 — no-op
        return None


_shared_client: httpx.Client | None = None
_shared_client_lock = threading.Lock()

_CLIENT_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=100,
    keepalive_expiry=90.0,
)


def _build_shared_client(**kwargs) -> httpx.Client:
    return httpx.Client(
        cookies=_NoStoreCookieJar(), limits=_CLIENT_LIMITS, **kwargs)


def _http_client() -> httpx.Client:
    global _shared_client
    if _shared_client is not None:
        return _shared_client
    with _shared_client_lock:
        if _shared_client is None:
            _shared_client = _build_shared_client()
    return _shared_client


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    api_key: str
    base_url: str = ""
    # Opaque, non-plaintext session affinity generated by the hosted runtime.
    # Empty for legacy/setup callers.  Provider adapters translate it to their
    # own cache/stickiness controls; it is never used as a correctness key.
    prompt_cache_key: str = ""
    # Opaque, non-user route/model/credential fingerprint used only for rollout metrics.
    # Hosted V2 derives it with an HMAC so custom relay hostnames are not stored
    # in plaintext. It is never sent to the provider.
    prompt_cache_route_fingerprint: str = ""
    # Optional audited provider/model metadata for Runtime V2's aggregate prompt
    # frontier. It is never sent on the wire; missing metadata falls back to an
    # operator override or a conservative built-in family floor.
    context_window_tokens: int | None = None
    # Canonical hosted-route reasoning switch. Runtime V2 reads this value to
    # decide whether the provider request should ask for native reasoning.
    reasoning_effort: str = ""
    # Runtime V2 may retain exact provider-attempt evidence in its encrypted
    # trajectory. Keep this opt-in: other async callers include image/VLM
    # payloads and must not duplicate those large request bodies in memory.
    capture_attempt_trace: bool = False


_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
    # Bedrock API keys use the ordinary HTTPS Converse endpoint with bearer
    # authentication.  A route may override this to another AWS region.
    "bedrock": "https://bedrock-runtime.us-east-1.amazonaws.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "deepseek": "https://api.deepseek.com",
}

_DEEPSEEK_V4_FLASH = "deepseek-v4-flash"
_DEEPSEEK_LEGACY_RUNTIME: dict[str, tuple[str, str]] = {
    "deepseek-chat": (_DEEPSEEK_V4_FLASH, "disabled"),
    "deepseek-reasoner": (_DEEPSEEK_V4_FLASH, "enabled"),
}
_OPENROUTER_LEGACY_MODELS = {
    "deepseek/deepseek-chat": "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-reasoner": "deepseek/deepseek-v4-flash",
}


def normalize_provider(provider: str) -> str:
    p = (provider or "").strip().lower().replace("-", "_")
    aliases = {
        "anthropic": "anthropic",
        "amazon_bedrock": "bedrock",
        "aws_bedrock": "bedrock",
        "bedrock": "bedrock",
        "claude": "anthropic",
        "compatible": "openai_compatible",
        "custom": "openai_compatible",
        "custom_endpoint": "openai_compatible",
        "deep_seek": "deepseek",
        "deepseek": "deepseek",
        "gemini": "gemini",
        "google": "gemini",
        "google_gemini": "gemini",
        "open_ai": "openai",
        "openai_compatible": "openai_compatible",
        "open_router": "openrouter",
        "openrouter": "openrouter",
    }
    return aliases.get(p, p)


def default_base_url(provider: str) -> str:
    return _DEFAULT_BASE_URLS.get(normalize_provider(provider), "")


def _runtime_model(provider: str, model: str) -> tuple[str, dict[str, Any]]:
    """Return the provider-facing model id plus provider-specific request knobs."""

    provider = normalize_provider(provider)
    raw = (model or "").strip()
    lower = raw.lower()

    if provider == "deepseek" and lower in _DEEPSEEK_LEGACY_RUNTIME:
        mapped_model, thinking_type = _DEEPSEEK_LEGACY_RUNTIME[lower]
        return mapped_model, {"thinking": {"type": thinking_type}}

    if provider == "deepseek" and lower == _DEEPSEEK_V4_FLASH:
        return raw, {"thinking": {"type": "disabled"}}

    if provider == "openrouter" and lower in _OPENROUTER_LEGACY_MODELS:
        return _OPENROUTER_LEGACY_MODELS[lower], {}

    return raw, {}


def _model_has_native_image_output(provider: str, model: str) -> bool:
    """Conservative gate for wires that require explicit image modalities."""
    normalized_provider = normalize_provider(provider)
    lower = str(model or "").strip().lower()
    if normalized_provider == "gemini":
        return lower.startswith("gemini-") and "image" in lower
    if normalized_provider in {"openai", "openrouter", "openai_compatible"}:
        return any(
            marker in lower
            for marker in (
                "image",
                "flux",
                "dall-e",
                "stable-diffusion",
                "seedream",
            )
        )
    return False


def public_config(config: dict) -> dict:
    provider = normalize_provider(str(config.get("provider") or ""))
    key_hint = str(config.get("api_key_hint") or "")
    safe = {
        "provider": provider,
        "model": str(config.get("model") or ""),
        "base_url": str(config.get("base_url") or ""),
        "api_key_hint": key_hint,
        "test_status": str(config.get("test_status") or "unknown"),
        "last_test_at": str(config.get("last_test_at") or ""),
        "created_at": str(config.get("created_at") or ""),
        "updated_at": str(config.get("updated_at") or ""),
        "last_test_error": str(config.get("last_test_error") or ""),
    }
    if config.get("context_window_tokens") is not None:
        safe["context_window_tokens"] = int(config["context_window_tokens"])
    return safe


def validate_config(
    provider: str, model: str, base_url: str = ""
) -> tuple[str, str, str]:
    provider = normalize_provider(provider)
    model = (model or "").strip()
    base_url = (base_url or "").strip().rstrip("/")

    if provider not in {
        "openai",
        "openrouter",
        "anthropic",
        "bedrock",
        "gemini",
        "deepseek",
        "openai_compatible",
    }:
        raise ProviderError(
            "provider must be openai, openrouter, anthropic, bedrock, gemini, "
            "deepseek, or openai_compatible"
        )
    if not model or len(model) > 160:
        raise ProviderError("model required")
    if provider == "openai_compatible" and not base_url:
        raise ProviderError("base_url required for openai_compatible")
    # NOTE (owner): this startswith() loopback check has the SAME latent
    # prefix-forgery flaw fixed in validate_catalog_target / _validate_egress_url
    # ("http://127.0.0.1.evil.example" and "http://127.0.0.1@evil.example" both
    # pass). Left untouched here to avoid changing the normal save/test path;
    # migrate this to _validate_egress_url when that path is next revisited.
    if base_url and not (
        base_url.startswith("https://") or base_url.startswith("http://127.0.0.1")
    ):
        raise ProviderError("base_url must be https:// or local http://127.0.0.1")
    if not base_url:
        base_url = default_base_url(provider)
    if not base_url:
        raise ProviderError("base_url unavailable for provider")
    return provider, model, base_url


def mask_api_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if len(key) <= 10:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def _headers(config: ProviderConfig) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    if normalize_provider(config.provider) == "openrouter":
        headers["HTTP-Referer"] = "https://feedling.app"
        headers["X-Title"] = "Feedling IO Hosted Runtime"
    return headers


def _collect_provider_error_details(
    value: Any,
    output: list[str],
    *,
    depth: int = 0,
) -> None:
    """Collect bounded provider error text from known wrapper fields only.

    OpenRouter may surface an upstream Anthropic/OpenAI error as JSON encoded
    inside ``error.metadata.raw`` while the outer message says only "Provider
    returned error".  We need the upstream field name to make a safe,
    one-field compatibility downgrade, but must not walk arbitrary response
    payloads that could echo request content.
    """
    if depth > 5 or len(output) >= 8:
        return
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return
        if text[:1] in {"{", "["}:
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, (dict, list)):
                _collect_provider_error_details(decoded, output, depth=depth + 1)
                return
        output.append(text[:240])
        return
    if isinstance(value, list):
        for item in value[:4]:
            _collect_provider_error_details(item, output, depth=depth + 1)
        return
    if not isinstance(value, dict):
        return
    for field in ("error", "message", "detail", "raw"):
        if field in value:
            _collect_provider_error_details(value[field], output, depth=depth + 1)
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        for field in ("raw", "error", "message", "detail"):
            if field in metadata:
                _collect_provider_error_details(
                    metadata[field], output, depth=depth + 1
                )
    for field in ("code", "status"):
        if field in value:
            _collect_provider_error_details(value[field], output, depth=depth + 1)


def _response_error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            details: list[str] = []
            _collect_provider_error_details(body, details)
            generic = {
                "provider returned error",
                "upstream provider error",
                "provider error",
            }
            for detail in details:
                if detail.strip().lower() not in generic:
                    return detail[:240]
            if details:
                return details[0][:240]
    except Exception:
        pass
    return resp.text[:240]


def _raise_for_provider_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    detail = _response_error_detail(resp)
    suffix = f": {detail}" if detail else ""
    raise ProviderError(
        f"provider_http_{resp.status_code}{suffix}",
        status_code=resp.status_code,
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            elif isinstance(part, str) and part.strip():
                parts.append(part.strip())
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _latest_user_image_prompt(messages: list[dict[str, Any]]) -> str:
    """Return the latest explicit user instruction for a dedicated image API."""
    for message in reversed(messages):
        if str(message.get("role") or "").strip().lower() not in {"user", "human"}:
            continue
        if _is_image_prompt_context_message(message):
            continue
        prompt = _content_text(message.get("content"))
        if not prompt:
            continue
        if len(prompt) > 16_000:
            raise ProviderError("image prompt too long")
        return prompt
    raise ProviderError("image prompt required")


def _build_openrouter_images_payload(
    *,
    model: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build OpenRouter's dedicated buffered Images API request."""
    return {
        "model": model,
        "prompt": _latest_user_image_prompt(messages),
        "n": 1,
    }


def _image_parts(content: Any) -> list[dict[str, str]]:
    if not isinstance(content, list):
        return []
    out: list[dict[str, str]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        data_url = ""
        if isinstance(part.get("image_url"), dict):
            data_url = str(part["image_url"].get("url") or "")
        elif part.get("type") == "image_url":
            data_url = str(part.get("url") or "")
        if not data_url.startswith("data:image/") or ";base64," not in data_url:
            continue
        meta, data = data_url.split(",", 1)
        mime = meta.removeprefix("data:").split(";", 1)[0] or "image/jpeg"
        if data.strip():
            out.append({"mime_type": mime, "data": data.strip()})
    return out


def _content_to_anthropic(content: Any) -> str | list[dict[str, Any]]:
    text = _content_text(content)
    images = _image_parts(content)
    if not images:
        return text
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for image in images:
        parts.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image["mime_type"],
                    "data": image["data"],
                },
            }
        )
    return parts


def _content_to_gemini_parts(content: Any) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    text = _content_text(content)
    if text:
        parts.append({"text": text})
    for image in _image_parts(content):
        parts.append(
            {
                "inline_data": {
                    "mime_type": image["mime_type"],
                    "data": image["data"],
                },
            }
        )
    return parts


def _json_only_instruction(response_format: dict[str, Any] | None) -> str:
    if not response_format:
        return ""
    if response_format.get("type") in {"json_object", "json_schema"}:
        return "Return only a valid JSON object. Do not wrap it in Markdown."
    return ""


def _split_system_messages_anthropic(
    messages: list[Any],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    provider_messages: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ToolExchange):
            payload = _assistant_payload_for_wire(message, "anthropic")
            if not isinstance(payload, list):
                raise ProviderError("anthropic assistant turn content must be a list")
            provider_messages.append({"role": "assistant", "content": payload})
            provider_messages.extend(_encode_tool_results_anthropic(message.results))
            continue
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        text = _content_text(content)
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        converted = _content_to_anthropic(content)
        if not converted:
            continue
        mapped_role = (
            "assistant"
            if role in {"assistant", "openclaw", "agent", "model"}
            else "user"
        )
        provider_messages.append({"role": mapped_role, "content": converted})
    if not provider_messages:
        provider_messages.append({"role": "user", "content": "Say ok."})
    return "\n\n".join(system_parts).strip(), provider_messages


def _bedrock_image_format(mime_type: str) -> str | None:
    return {
        "image/gif": "gif",
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/png": "png",
        "image/webp": "webp",
    }.get(str(mime_type or "").lower())


def _content_to_bedrock(content: Any) -> list[dict[str, Any]]:
    """Convert one message to the Bedrock Converse content-block wire.

    The HTTP JSON API accepts base64 text in ``image.source.bytes``.  Unsupported
    image formats are omitted rather than mislabeled; the text part still reaches
    the model and the provider frontier already reserves image budget separately.
    """
    blocks: list[dict[str, Any]] = []
    text = _content_text(content)
    if text:
        blocks.append({"text": text})
    for image in _image_parts(content):
        image_format = _bedrock_image_format(image["mime_type"])
        if image_format is None:
            continue
        blocks.append(
            {
                "image": {
                    "format": image_format,
                    "source": {"bytes": image["data"]},
                },
            }
        )
    return blocks


def _append_bedrock_message(
    messages: list[dict[str, Any]],
    *,
    role: str,
    content: list[dict[str, Any]],
) -> None:
    """Append while satisfying Converse's alternating-role message contract."""
    if not content:
        return
    if messages and messages[-1].get("role") == role:
        messages[-1]["content"].extend(content)
    else:
        messages.append({"role": role, "content": list(content)})


def _split_system_messages_bedrock(
    messages: list[Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    system_parts: list[str] = []
    provider_messages: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ToolExchange):
            payload = _assistant_payload_for_wire(message, "bedrock")
            if not isinstance(payload, list):
                raise ProviderError("bedrock assistant turn content must be a list")
            _append_bedrock_message(
                provider_messages, role="assistant", content=copy.deepcopy(payload)
            )
            _append_bedrock_message(
                provider_messages,
                role="user",
                content=_encode_tool_results_bedrock(message.results),
            )
            continue
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        text = _content_text(content)
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        converted = _content_to_bedrock(content)
        mapped_role = (
            "assistant"
            if role in {"assistant", "openclaw", "agent", "model"}
            else "user"
        )
        _append_bedrock_message(provider_messages, role=mapped_role, content=converted)
    if not provider_messages:
        provider_messages.append({"role": "user", "content": [{"text": "Say ok."}]})
    return system_parts, provider_messages


def _split_system_messages_gemini(
    messages: list[Any],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    provider_messages: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ToolExchange):
            payload = _assistant_payload_for_wire(message, "gemini")
            if not isinstance(payload, dict):
                raise ProviderError("gemini assistant turn content must be an object")
            assistant_content = dict(payload)
            assistant_content.setdefault("role", "model")
            provider_messages.append(assistant_content)
            id_to_name = {call.id: call.name for call in message.calls}
            provider_messages.extend(
                _encode_tool_results_gemini(message.results, id_to_name)
            )
            continue
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        text = _content_text(content)
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        parts = _content_to_gemini_parts(content)
        if not parts:
            continue
        mapped_role = (
            "model" if role in {"assistant", "openclaw", "agent", "model"} else "user"
        )
        provider_messages.append({"role": mapped_role, "parts": parts})
    if not provider_messages:
        provider_messages.append({"role": "user", "parts": [{"text": "Say ok."}]})
    return "\n\n".join(system_parts).strip(), provider_messages


def _normalize_usage(provider: str, raw: dict | None) -> dict:
    """Normalize token usage, including provider prompt-cache telemetry.

    ``prompt_tokens`` is always the *effective* prompt size, including tokens
    served from or written to a cache.  This matters for Anthropic, whose
    ``input_tokens`` contains only the uncached suffix once caching is enabled.
    Cache fields use one provider-neutral vocabulary:

    - ``cache_read_tokens``: input tokens reused from a cache;
    - ``cache_write_tokens``: input tokens written to a cache this request;
    - ``cache_miss_tokens``: effective input tokens not served from cache.

    Providers which cache implicitly (OpenAI, DeepSeek, Gemini) expose different
    fields. Missing cache details remain ``None``: rollout metrics must distinguish
    "the provider reported zero" from "this relay did not report cache telemetry".
    """
    raw = raw if isinstance(raw, dict) else {}

    def _int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, float) and (
            not math.isfinite(value) or not value.is_integer()
        ):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed < 0:
            return 0
        if parsed > _MAX_PG_BIGINT:
            return None
        return parsed

    def _sum_ints(*values: int | None) -> int | None:
        present = [value for value in values if value is not None]
        if not present:
            return None
        total = sum(present)
        return total if total <= _MAX_PG_BIGINT else None

    if provider == "anthropic":
        uncached = _int(raw.get("input_tokens"))
        cache_read = _int(raw.get("cache_read_input_tokens"))
        cache_write = _int(raw.get("cache_creation_input_tokens"))
        pt = _sum_ints(uncached, cache_read, cache_write)
        ct = _int(raw.get("output_tokens"))
        tt = _int(raw.get("total_tokens"))
        cache_reported = (
            "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw
        )
        # Provider-neutral miss semantics: every effective input token not read
        # from cache. Anthropic reports newly-created cache tokens separately
        # from its uncached suffix, so both belong in the miss count.
        cache_miss = _sum_ints(uncached, cache_write) if cache_reported else None
    elif provider == "bedrock":
        uncached = _int(raw.get("inputTokens"))
        cache_read = _int(raw.get("cacheReadInputTokens"))
        cache_write = _int(raw.get("cacheWriteInputTokens"))
        pt = _sum_ints(uncached, cache_read, cache_write)
        ct = _int(raw.get("outputTokens"))
        # Bedrock's inputTokens excludes cache reads/writes. Keep the normalized
        # total aligned with Feedling's effective prompt-token semantics rather
        # than forwarding the smaller wire total.
        tt = _sum_ints(pt, ct)
        cache_reported = "cacheReadInputTokens" in raw or "cacheWriteInputTokens" in raw
        cache_miss = _sum_ints(uncached, cache_write) if cache_reported else None
    elif provider == "gemini":
        pt = _int(raw.get("promptTokenCount"))
        ct = _int(raw.get("candidatesTokenCount"))
        tt = _int(raw.get("totalTokenCount"))
        cache_read = _int(raw.get("cachedContentTokenCount"))
        cache_write = None  # implicit Gemini caching has no per-request write count
        cache_miss = (
            max(0, pt - (cache_read or 0))
            if pt is not None and cache_read is not None
            else None
        )
    else:  # openai, openrouter, deepseek, openai_compatible, Responses
        # Responses uses input/output_tokens; Chat Completions uses
        # prompt/completion_tokens.  Accept both without making the parser know
        # which OpenAI wire produced the usage object.
        pt = _int(raw.get("prompt_tokens"))
        if pt is None:
            pt = _int(raw.get("input_tokens"))
        ct = _int(raw.get("completion_tokens"))
        if ct is None:
            ct = _int(raw.get("output_tokens"))
        tt = _int(raw.get("total_tokens"))
        details = raw.get("prompt_tokens_details")
        if not isinstance(details, dict):
            details = raw.get("input_tokens_details")
        details = details if isinstance(details, dict) else {}
        cache_read = _int(details.get("cached_tokens"))
        if cache_read is None:
            cache_read = _int(raw.get("prompt_cache_hit_tokens"))
        cache_write = _int(details.get("cache_write_tokens"))
        if cache_write is None:
            cache_write = _int(raw.get("cache_write_tokens"))
        cache_miss = _int(raw.get("prompt_cache_miss_tokens"))
        if cache_miss is None and pt is not None and cache_read is not None:
            cache_miss = max(0, pt - (cache_read or 0))
    if tt is None and pt is not None and ct is not None:
        tt = _sum_ints(pt, ct)
    return {
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tt,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cache_miss_tokens": cache_miss,
    }


def _parse_tool_args(raw) -> tuple[dict, str, bool]:
    """Normalize a provider tool-call args payload to (dict, raw_str, ok).
    OpenAI/Responses send a JSON *string*; Anthropic/Gemini send an object."""
    if isinstance(raw, dict):
        return raw, "", True
    s = "" if raw is None else str(raw)
    try:
        parsed = json.loads(s) if s else {}
        return (
            (parsed if isinstance(parsed, dict) else {}),
            ("" if isinstance(parsed, dict) else s),
            isinstance(parsed, dict),
        )
    except (ValueError, TypeError):
        return {}, s, False


_CACHE_REQUEST_FIELDS = (
    "cache_control",
    "prompt_cache_key",
    "session_id",
    "prompt_cache_options",
)
_CACHE_FIELD_ALIASES = {
    "cache_control": ("cache_control", "cache control", "cachepoint", "cache point"),
    "prompt_cache_key": ("prompt_cache_key", "prompt cache key"),
    "session_id": ("session_id", "session id"),
    "prompt_cache_options": ("prompt_cache_options", "prompt cache options"),
}
_SCHEMA_ERROR_HINTS = (
    "additional properties",
    "extra field",
    "not permitted",
    "unknown field",
    "unknown parameter",
    "unrecognized field",
    "unrecognized parameter",
    "unsupported parameter",
)
_NAMED_SCHEMA_FIELD = re.compile(
    r"(?:field|parameter)\s*[:=]?\s*[`'\"]?([a-zA-Z_][a-zA-Z0-9_.-]*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _FallbackDecision:
    payload: dict[str, Any]
    code: str


def _cache_key(value: Any) -> str:
    """Return a provider-safe opaque cache/session key (max 256 chars)."""
    return str(value or "").strip()[:256]


def _cache_control_blocks(payload: dict[str, Any]):
    """Yield only provider content blocks where Feedling may inject a marker.

    Never recurse through arbitrary payload data: a tool may legitimately have
    an argument/property named ``cache_control`` and fallback must preserve it.
    """
    system = payload.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict):
                yield block
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        yield block
    tool_config = payload.get("toolConfig")
    if isinstance(tool_config, dict):
        tools = tool_config.get("tools")
        if isinstance(tools, list):
            for block in tools:
                if isinstance(block, dict):
                    yield block


def _contains_provider_cache_control(payload: dict[str, Any]) -> bool:
    return "cache_control" in payload or any(
        "cache_control" in block or "cachePoint" in block
        for block in _cache_control_blocks(payload)
    )


def _cache_fields_present(payload: dict[str, Any]) -> tuple[str, ...]:
    present: list[str] = []
    for field in _CACHE_REQUEST_FIELDS:
        if field == "cache_control":
            if _contains_provider_cache_control(payload):
                present.append(field)
        elif field in payload:
            present.append(field)
    return tuple(present)


def _without_provider_cache_control(
    payload: dict[str, Any],
) -> dict[str, Any]:
    fallback = copy.deepcopy(payload)
    fallback.pop("cache_control", None)
    system = fallback.get("system")
    if isinstance(system, list):
        fallback["system"] = [
            block
            for block in system
            if not (isinstance(block, dict) and "cachePoint" in block)
        ]
    messages = fallback.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = [
                    block
                    for block in content
                    if not (isinstance(block, dict) and "cachePoint" in block)
                ]
    tool_config = fallback.get("toolConfig")
    if isinstance(tool_config, dict):
        tools = tool_config.get("tools")
        if isinstance(tools, list):
            tool_config["tools"] = [
                block
                for block in tools
                if not (isinstance(block, dict) and "cachePoint" in block)
            ]
    for block in _cache_control_blocks(fallback):
        block.pop("cache_control", None)
    return fallback


def _without_cache_field(payload: dict[str, Any], field: str) -> dict[str, Any]:
    if field == "cache_control":
        return _without_provider_cache_control(payload)
    fallback = dict(payload)
    fallback.pop(field, None)
    return fallback


def _cache_field_named_in_error(resp) -> str | None:
    try:
        detail = _response_error_detail(resp).lower()
    except Exception:  # noqa: BLE001 — absent error text is not a field hint
        return None
    for field, aliases in _CACHE_FIELD_ALIASES.items():
        if any(alias in detail for alias in aliases):
            return field
    return None


def _cache_fallback_payload(
    payload: dict[str, Any],
    resp,
    *,
    require_error_hint: bool = False,
) -> _FallbackDecision | None:
    """Remove one rejected cache/stickiness field after an unsupported 400/422.

    BYOK relays vary widely.  A cache hint must never turn a previously valid
    request into a terminal failure, but the downgrade must preserve tools,
    messages, still-supported cache affinity, and every other semantic field.
    Named schema errors remove only the named field.  A generic downgrade is
    allowed only for an explicit schema-shaped error; an unrelated 400 (bad
    model/key/request) is never disguised as a cache compatibility retry.
    """
    if getattr(resp, "status_code", None) not in {400, 422}:
        return None
    present = _cache_fields_present(payload)
    if not present:
        return None
    try:
        detail = _response_error_detail(resp).lower()
    except Exception:  # noqa: BLE001 — absent error text is not a hint
        detail = ""
    named = _cache_field_named_in_error(resp)
    if named in present:
        field = named
    else:
        cache_hint = any(
            hint in detail for hint in ("cache", "session_id", "session id")
        )
        schema_hint = any(hint in detail for hint in _SCHEMA_ERROR_HINTS)
        if require_error_hint and not cache_hint:
            return None
        if not require_error_hint and not schema_hint:
            return None
        named_schema_field = _NAMED_SCHEMA_FIELD.search(detail)
        if (
            not require_error_hint
            and named_schema_field is not None
            and named_schema_field.group(1).lower()
            not in {
                alias for aliases in _CACHE_FIELD_ALIASES.values() for alias in aliases
            }
        ):
            # "unknown field tools" is about tools, not caching. Do not amplify
            # it into several cache retries before the tool loop's own bounded
            # tools-disabled fallback.
            return None
        # A relay may report only "additional properties are not allowed".
        # Remove one optional field at a time in deterministic order, preserving
        # every other potentially supported cache/stickiness hint.
        field = present[0]
    return _FallbackDecision(
        payload=_without_cache_field(payload, field),
        code=f"cache_rejected:{field}",
    )


def _malformed_tool_call(*, call_id: str = "", name: str = "") -> dict:
    """Normalize an unusable provider tool-call element without raising.

    Provider and relay responses are untrusted JSON.  Returning an explicit
    ``args_ok=False`` call keeps malformed wire shapes on the tool loop's
    fail-closed path (no capability dispatch, then the bounded text fallback)
    instead of letting an ``AttributeError`` escape while decoding.
    """
    return {
        "id": str(call_id or ""),
        "name": str(name or ""),
        "args": {},
        "args_raw": "",
        "args_ok": False,
    }


def _tool_args_json(call) -> str:
    if not call.args_ok and call.args_raw:
        return call.args_raw
    return json.dumps(call.args, ensure_ascii=False, separators=(",", ":"))


def _validate_tool_exchange(exchange: ToolExchange) -> None:
    call_ids = [call.id for call in exchange.calls]
    result_ids = [result.call_id for result in exchange.results]
    if call_ids != result_ids:
        raise ProviderError(
            "tool exchange results must match assistant call ids in original order"
        )


def _synthesized_assistant_payload(exchange: ToolExchange, wire: str):
    """Compatibility path for tests/legacy fakes without an exact native turn.

    Real provider responses always carry ``NativeAssistantTurn``.  Keeping this
    deterministic fallback avoids forcing every scripted loop test to manufacture
    provider JSON while ensuring production never discards provider-only fields.
    """
    if wire == "openai_chat":
        return {
            "role": "assistant",
            "content": exchange.assistant_text or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": _tool_args_json(call),
                    },
                }
                for call in exchange.calls
            ],
        }
    if wire == "openai_responses":
        items: list[dict[str, Any]] = []
        if exchange.assistant_text:
            items.append(
                {
                    "role": "assistant",
                    "content": [
                        # assistant-role parts on the Responses wire are
                        # output_text/refusal only; input_text 400s.
                        {"type": "output_text", "text": exchange.assistant_text}
                    ],
                }
            )
        items.extend(
            {
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": _tool_args_json(call),
            }
            for call in exchange.calls
        )
        return items
    if wire == "anthropic":
        blocks: list[dict[str, Any]] = []
        if exchange.assistant_text:
            blocks.append({"type": "text", "text": exchange.assistant_text})
        blocks.extend(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.args,
            }
            for call in exchange.calls
        )
        return blocks
    if wire == "bedrock":
        blocks: list[dict[str, Any]] = []
        if exchange.assistant_text:
            blocks.append({"text": exchange.assistant_text})
        blocks.extend(
            {
                "toolUse": {
                    "toolUseId": call.id,
                    "name": call.name,
                    "input": call.args,
                },
            }
            for call in exchange.calls
        )
        return blocks
    if wire == "gemini":
        parts: list[dict[str, Any]] = []
        if exchange.assistant_text:
            parts.append({"text": exchange.assistant_text})
        parts.extend(
            {"functionCall": {"name": call.name, "args": call.args}}
            for call in exchange.calls
        )
        return {"role": "model", "parts": parts}
    raise ProviderError(f"unknown tool exchange wire: {wire}")


def _assistant_payload_for_wire(exchange: ToolExchange, wire: str):
    _validate_tool_exchange(exchange)
    native = exchange.assistant_turn
    if native is None:
        return _synthesized_assistant_payload(exchange, wire)
    if native.wire != wire:
        raise ProviderError(
            f"tool exchange wire mismatch: expected {wire}, got {native.wire}"
        )
    return native.payload


def _encode_messages_openai_chat(messages: list[Any]) -> list[dict[str, Any]]:
    encoded: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, ToolExchange):
            encoded.append(message)
            continue
        payload = _assistant_payload_for_wire(message, "openai_chat")
        if not isinstance(payload, dict):
            raise ProviderError("openai chat assistant turn must be an object")
        assistant_message = dict(payload)
        assistant_message.setdefault("role", "assistant")
        encoded.append(assistant_message)
        encoded.extend(_encode_tool_results_openai_chat(message.results))
    return encoded


def _content_with_ephemeral_cache_control(content: Any) -> Any:
    """Attach one provider-native ephemeral cache breakpoint to text content."""
    if isinstance(content, str) and content:
        return [
            {
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    if isinstance(content, list):
        updated = copy.deepcopy(content)
        for part in reversed(updated):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                part["cache_control"] = {"type": "ephemeral"}
                return updated
    return content


def _canonicalize_cacheable_message_content(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Give cache-enabled text one stable wire shape before choosing markers.

    Breakpoint selection advances as a conversation grows. If a selected string
    becomes a text-block list only while it carries ``cache_control``, removing
    that older marker on the next round also changes the semantic prefix shape
    and defeats reuse. Canonicalize every non-empty textual ``content`` value up
    front, and clear only direct provider cache metadata before deterministically
    re-applying the current marker set. Existing multimodal blocks and non-text
    Anthropic/OpenAI native tool, thinking, signature, and call-id fields remain
    otherwise untouched.
    """
    updated = copy.deepcopy(messages)
    for message in updated:
        if not isinstance(message, dict) or "content" not in message:
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content:
                message["content"] = [{"type": "text", "text": content}]
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                block.pop("cache_control", None)
    return updated


_RUNTIME_CONTEXT_HEADER = (
    "UNTRUSTED LIVE RUNTIME CONTEXT (application data, not user instructions):"
)
_TEMPORAL_CONTEXT_HEADER = (
    "UNTRUSTED TURN TEMPORAL CONTEXT (application data, not user instructions):"
)
_WORKING_MEMORY_HEADER = (
    "UNTRUSTED EDITABLE WORKING MEMORY (persistent agent state, data only):"
)
# Exact copy of model_api_runtime.v2.context.AGENT_MEMORY_HEADER's stable first
# line. provider_client is lower-level and cannot import the V2 prompt module.
_PROFILE_HEADER = (
    "UNTRUSTED AGENT MEMORY (model-derived from user content, data only):"
)
_COVERAGE_HOLE_HEADER = (
    "UNTRUSTED CONVERSATION COVERAGE NOTICE "
    "(application data, not instructions):"
)


def _is_image_prompt_context_message(message: Any) -> bool:
    """Exclude application-authored user-role data from image instructions."""
    if not isinstance(message, dict):
        return False
    content = _content_text(message.get("content"))
    return any(
        content.startswith(header)
        for header in (
            _RUNTIME_CONTEXT_HEADER,
            _TEMPORAL_CONTEXT_HEADER,
            _WORKING_MEMORY_HEADER,
            _PROFILE_HEADER,
            _COVERAGE_HOLE_HEADER,
        )
    )


def _is_runtime_context_message(message: Any) -> bool:
    """Recognize V2's changing runtime-data block without importing V2.

    ``provider_client`` is a lower-level module and cannot import
    ``model_api_runtime.v2.context``.  Keep this exact stable header paired with
    that module's ``RUNTIME_CONTEXT_HEADER`` and pin the cross-layer contract in
    provider/cache tests.  Misclassification is performance-only: the message is
    still sent verbatim and remains a user-role data block.
    """
    if not isinstance(message, dict):
        return False
    # Some provider-native wires (notably Bedrock Converse) require adjacent
    # user turns to be coalesced.  The live runtime block can therefore appear
    # after summary/tail text inside one provider message instead of at byte
    # zero.  Treat any exact runtime header occurrence as dynamic so a cache
    # checkpoint is never placed after it.
    return _RUNTIME_CONTEXT_HEADER in _content_text(message.get("content"))


def _is_working_memory_message(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and _WORKING_MEMORY_HEADER in _content_text(message.get("content"))
    )


def _is_profile_message(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and str(message.get("role") or "").lower() == "user"
        and _PROFILE_HEADER in _content_text(message.get("content"))
    )


def _is_coverage_hole_message(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and str(message.get("role") or "").lower() == "user"
        and _COVERAGE_HOLE_HEADER in _content_text(message.get("content"))
    )


def _mark_openai_chat_cache_breakpoint(
    messages: list[dict[str, Any]],
    *,
    max_breakpoints: int = 4,
) -> list[dict[str, Any]]:
    """Mark stable + advancing prefixes for OpenRouter Anthropic caching.

    OpenRouter accepts Anthropic's ``cache_control`` on a content block, not as
    a top-level Chat Completions parameter. The first system message is the
    byte-stable tool/persona prefix. The two most recent user boundaries are
    retained explicitly; remaining slots go to the newest non-system messages.
    This keeps the current user boundary across a parallel tool-result batch
    instead of letting assistant/tool blocks displace it. The changing V2
    runtime-data block (for example live perception data) is never selected.
    """
    updated = _canonicalize_cacheable_message_content(messages)
    limit = max(1, min(int(max_breakpoints), 4))
    stable_candidates = [
        index
        for index, message in enumerate(updated)
        if isinstance(message, dict)
        and str(message.get("role") or "").lower() == "system"
    ]
    advancing_candidates = [
        index
        for index, message in enumerate(updated)
        if isinstance(message, dict)
        and str(message.get("role") or "").lower() != "system"
        and not _is_runtime_context_message(message)
        and not _is_coverage_hole_message(message)
    ]
    user_candidates = [
        index
        for index in advancing_candidates
        if str(updated[index].get("role") or "").lower() == "user"
    ]
    candidates: list[int] = []
    if stable_candidates:
        candidates.append(stable_candidates[0])
    for index in advancing_candidates:
        if (
            _is_working_memory_message(updated[index])
            and index not in candidates
            and len(candidates) < limit
        ):
            candidates.append(index)
    for index in advancing_candidates:
        if (
            _is_profile_message(updated[index])
            and index not in candidates
            and len(candidates) < limit
        ):
            candidates.append(index)
    # Newest first is deliberate for the Anthropic three-message-slot path:
    # profile outranks the older of the two recent user boundaries.
    for index in reversed(user_candidates[-2:]):
        if index not in candidates and len(candidates) < limit:
            candidates.append(index)
    for index in reversed(advancing_candidates):
        if index not in candidates and len(candidates) < limit:
            candidates.append(index)
    if not candidates:
        candidates = [
            index
            for index, message in enumerate(updated)
            if not _is_runtime_context_message(message)
        ][:limit]
    marked_any = False
    for index in dict.fromkeys(candidates):
        message = updated[index]
        if not isinstance(message, dict) or "content" not in message:
            continue
        marked = _content_with_ephemeral_cache_control(message.get("content"))
        if marked is not message.get("content"):
            message["content"] = marked
            marked_any = True
    if marked_any:
        return updated
    return updated


def _mark_anthropic_cache_breakpoint(
    system: str,
    messages: list[dict[str, Any]],
    *,
    system_parts: list[str] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Return Anthropic-native system/messages with one stable breakpoint."""
    if system:
        parts = [part for part in (system_parts or []) if part]
        joined = "\n\n".join(parts)
        if not parts or not system.startswith(joined):
            parts = [system]
        else:
            remainder = system[len(joined) :].strip()
            if remainder:
                parts.append(remainder)
        blocks = [{"type": "text", "text": part} for part in parts]
        # The checkpoint covers every preceding tool and system block. Put it
        # after the complete stable policy/skills prefix, not after only the
        # first system fragment.
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks, _mark_openai_chat_cache_breakpoint(messages, max_breakpoints=3)
    updated = _canonicalize_cacheable_message_content(messages)
    for message in updated:
        if not isinstance(message, dict) or "content" not in message:
            continue
        marked = _content_with_ephemeral_cache_control(message.get("content"))
        if marked is not message.get("content"):
            message["content"] = marked
            break
    return system, updated


def _encode_tools_openai_chat(tools) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _decode_tool_calls_openai_chat(body: dict) -> list[dict]:
    if not isinstance(body, dict):
        return [_malformed_tool_call()]
    choices = body.get("choices")
    if choices is None:
        return []
    if not isinstance(choices, list):
        return [_malformed_tool_call()]
    if not choices:
        return []
    choice = choices[0]
    if not isinstance(choice, dict):
        return [_malformed_tool_call()]
    message = choice.get("message")
    if message is None:
        return []
    if not isinstance(message, dict):
        return [_malformed_tool_call()]
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        return []
    if not isinstance(raw_calls, list):
        return [_malformed_tool_call()]
    out = []
    for c in raw_calls:
        if not isinstance(c, dict):
            out.append(_malformed_tool_call())
            continue
        fn = c.get("function") or {}
        if not isinstance(fn, dict):
            out.append(_malformed_tool_call(call_id=str(c.get("id") or "")))
            continue
        args, args_raw, ok = _parse_tool_args(fn.get("arguments"))
        out.append(
            {
                "id": str(c.get("id") or ""),
                "name": str(fn.get("name") or ""),
                "args": args,
                "args_raw": args_raw,
                "args_ok": ok,
            }
        )
    return out


def _encode_tools_openai_responses(tools) -> list[dict]:
    return [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        }
        for t in tools
    ]


def _decode_tool_calls_openai_responses(body: dict) -> list[dict]:
    out = []
    if not isinstance(body, dict):
        return [_malformed_tool_call()]
    output = body.get("output")
    if output is None:
        return []
    if not isinstance(output, list):
        return [_malformed_tool_call()]
    for item in output:
        if not isinstance(item, dict):
            out.append(_malformed_tool_call())
            continue
        if item.get("type") != "function_call":
            continue
        args, args_raw, ok = _parse_tool_args(item.get("arguments"))
        out.append(
            {
                "id": str(item.get("call_id") or ""),
                "name": str(item.get("name") or ""),
                "args": args,
                "args_raw": args_raw,
                "args_ok": ok,
            }
        )
    return out


def _encode_tool_results_openai_chat(results) -> list[dict]:
    return [
        {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
        for r in results
    ]


def _encode_tool_results_openai_responses(results) -> list[dict]:
    return [
        {"type": "function_call_output", "call_id": r.call_id, "output": r.content}
        for r in results
    ]


def _encode_tools_anthropic(tools) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters}
        for t in tools
    ]


def _decode_tool_calls_anthropic(body: dict) -> list[dict]:
    out = []
    if not isinstance(body, dict):
        return [_malformed_tool_call()]
    content = body.get("content")
    if content is None:
        return []
    if not isinstance(content, list):
        return [_malformed_tool_call()]
    for block in content:
        if not isinstance(block, dict):
            out.append(_malformed_tool_call())
            continue
        if block.get("type") != "tool_use":
            continue
        args, args_raw, ok = _parse_tool_args(block.get("input"))
        out.append(
            {
                "id": str(block.get("id") or ""),
                "name": str(block.get("name") or ""),
                "args": args,
                "args_raw": args_raw,
                "args_ok": ok,
            }
        )
    return out


def _encode_tool_results_anthropic(results) -> list[dict]:
    # Anthropic carries tool results as tool_result content blocks in ONE user turn.
    return [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content}
                for r in results
            ],
        }
    ]


def _encode_tools_bedrock(tools) -> list[dict[str, Any]]:
    return [
        {
            "toolSpec": {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": {"json": tool.parameters},
            },
        }
        for tool in tools
    ]


def _decode_tool_calls_bedrock(body: dict) -> list[dict]:
    if not isinstance(body, dict):
        return [_malformed_tool_call()]
    output = body.get("output")
    if output is None:
        return []
    if not isinstance(output, dict):
        return [_malformed_tool_call()]
    message = output.get("message")
    if message is None:
        return []
    if not isinstance(message, dict):
        return [_malformed_tool_call()]
    content = message.get("content")
    if content is None:
        return []
    if not isinstance(content, list):
        return [_malformed_tool_call()]
    calls: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            calls.append(_malformed_tool_call())
            continue
        if "toolUse" not in block:
            continue
        tool_use = block.get("toolUse")
        if not isinstance(tool_use, dict):
            calls.append(_malformed_tool_call())
            continue
        args, args_raw, ok = _parse_tool_args(tool_use.get("input"))
        calls.append(
            {
                "id": str(tool_use.get("toolUseId") or ""),
                "name": str(tool_use.get("name") or ""),
                "args": args,
                "args_raw": args_raw,
                "args_ok": ok,
            }
        )
    return calls


def _encode_tool_results_bedrock(results) -> list[dict[str, Any]]:
    return [
        {
            "toolResult": {
                "toolUseId": result.call_id,
                "content": [{"text": result.content}],
                "status": "success",
            },
        }
        for result in results
    ]


def _encode_tools_gemini(tools) -> list[dict]:
    return [
        {
            "functionDeclarations": [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in tools
            ]
        }
    ]


def _decode_tool_calls_gemini(body: dict) -> list[dict]:
    out = []
    if not isinstance(body, dict):
        return [_malformed_tool_call()]
    candidates = body.get("candidates")
    if candidates is None:
        return []
    if not isinstance(candidates, list):
        return [_malformed_tool_call()]
    if not candidates:
        return []
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return [_malformed_tool_call()]
    content = candidate.get("content")
    if content is None:
        return []
    if not isinstance(content, dict):
        return [_malformed_tool_call()]
    parts = content.get("parts")
    if parts is None:
        return []
    if not isinstance(parts, list):
        return [_malformed_tool_call()]
    idx = 0
    for part in parts:
        if not isinstance(part, dict):
            out.append(_malformed_tool_call(call_id=f"call_{idx}_malformed"))
            idx += 1
            continue
        if "functionCall" not in part:
            continue
        fc = part.get("functionCall")
        if not isinstance(fc, dict):
            out.append(_malformed_tool_call(call_id=f"call_{idx}_malformed"))
            idx += 1
            continue
        name = str(fc.get("name") or "")
        args, args_raw, ok = _parse_tool_args(fc.get("args"))
        out.append(
            {
                "id": f"call_{idx}_{name}",
                "name": name,
                "args": args,
                "args_raw": args_raw,
                "args_ok": ok,
            }
        )
        idx += 1
    return out


def _encode_tool_results_gemini(results, id_to_name: dict) -> list[dict]:
    # Gemini keys functionResponse on the tool NAME (no id); resolve each result's
    # synthetic call_id back to its name via the map the decoder's ids imply.
    parts = []
    for r in results:
        name = id_to_name.get(r.call_id, r.call_id)
        parts.append(
            {"functionResponse": {"name": name, "response": {"content": r.content}}}
        )
    return [{"role": "user", "parts": parts}]


def _extract_reply(body: dict[str, Any], *, required: bool = True) -> str:
    choices = body.get("choices")
    has_shape = (
        isinstance(choices, list) and len(choices) > 0 and isinstance(choices[0], dict)
    )
    if has_shape:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
            text = first.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    # An empty reply is acceptable only when the provider's real success shape is
    # present (e.g. a thinking model that spent its budget on reasoning). A 2xx
    # whose body has no success container ({}, {"error": ...}) is still a failure
    # — otherwise setup would save a config that chat/send can't actually use.
    if not required and has_shape:
        return ""
    raise ProviderError("provider response had no usable reply text")


def _extract_openai_compatible_reasoning(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    parts: list[str] = []
    for key in ("reasoning", "reasoning_content", "reasoning_text"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    content = message.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            if "reason" not in item_type and "think" not in item_type:
                continue
            text = item.get("text") or item.get("content") or item.get("reasoning")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts).strip()


def _extract_openai_compatible_stop_reason(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
        return ""
    return str(choices[0].get("finish_reason") or "").strip()


def _extract_anthropic_reply(body: dict[str, Any], *, required: bool = True) -> str:
    content = body.get("content")
    has_shape = isinstance(content, list)
    if has_shape:
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            return "\n".join(parts).strip()
    # An empty reply is acceptable only when the provider's real success shape is
    # present (e.g. a thinking model that spent its budget on reasoning). A 2xx
    # whose body has no success container ({}, {"error": ...}) is still a failure
    # — otherwise setup would save a config that chat/send can't actually use.
    if not required and has_shape:
        return ""
    raise ProviderError("provider response had no usable reply text")


def _extract_anthropic_reasoning(body: dict[str, Any]) -> str:
    content = body.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").lower()
        if item_type == "thinking":
            thinking = item.get("thinking") or item.get("text")
            if isinstance(thinking, str) and thinking.strip():
                parts.append(thinking.strip())
        elif item_type == "redacted_thinking":
            parts.append("[redacted thinking]")
    return "\n\n".join(parts).strip()


def _extract_gemini_reply(body: dict[str, Any], *, required: bool = True) -> str:
    candidates = body.get("candidates")
    has_shape = isinstance(candidates, list) and len(candidates) > 0
    if has_shape:
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            text_parts: list[str] = []
            for part in parts:
                if isinstance(part, dict) and not part.get("thought"):
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        text_parts.append(text.strip())
            if text_parts:
                return "\n".join(text_parts).strip()
    # An empty reply is acceptable only when the provider's real success shape is
    # present (e.g. a thinking model that spent its budget on reasoning). A 2xx
    # whose body has no success container ({}, {"error": ...}) is still a failure
    # — otherwise setup would save a config that chat/send can't actually use.
    if not required and has_shape:
        return ""
    raise ProviderError("provider response had no usable reply text")


def _inline_media_item(data: Any, mime_type: Any, *, name: str = "") -> dict | None:
    encoded = str(data or "").strip()
    mime = str(mime_type or "").strip().lower()
    if not encoded or not mime.startswith("image/"):
        return None
    return {"mime_type": mime, "data_base64": encoded, "name": str(name or "")}


def _extract_gemini_media(body: dict[str, Any]) -> list[dict]:
    out: list[dict] = []
    candidates = body.get("candidates")
    if not isinstance(candidates, list):
        return out
    for candidate in candidates:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("thought"):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if not isinstance(inline, dict):
                continue
            item = _inline_media_item(
                inline.get("data"),
                inline.get("mimeType") or inline.get("mime_type"),
            )
            if item is not None:
                out.append(item)
                if len(out) >= MAX_GENERATED_IMAGES_PER_REPLY:
                    return out
    return out


def _extract_gemini_reasoning(body: dict[str, Any]) -> str:
    candidates = body.get("candidates")
    if not isinstance(candidates, list):
        return ""
    parts_out: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or not part.get("thought"):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                parts_out.append(text.strip())
    return "\n\n".join(parts_out).strip()


def _extract_gemini_stop_reason(body: dict[str, Any]) -> str:
    candidates = body.get("candidates")
    if not (
        isinstance(candidates, list) and candidates and isinstance(candidates[0], dict)
    ):
        return ""
    return str(candidates[0].get("finishReason") or "").strip()


def _anthropic_supports_thinking(model: str) -> bool:
    lower = (model or "").lower()
    return (
        "claude-3-7" in lower or "claude-sonnet-4" in lower or "claude-opus-4" in lower
    )


def _openai_uses_responses_for_reasoning(model: str) -> bool:
    lower = (model or "").lower()
    return (
        lower.startswith("gpt-5")
        or lower.startswith("o1")
        or lower.startswith("o3")
        or lower.startswith("o4")
        or lower.startswith("computer-use-preview")
    )


def _content_to_openai_responses_parts(
    content: Any, *, assistant: bool = False
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    text = _content_text(content)
    if text:
        # The Responses API rejects ``input_text`` on an assistant-role item
        # with HTTP 400 ("Supported values are: output_text and refusal").
        # A conversation's prior assistant replies must therefore serialize as
        # ``output_text`` — otherwise every multi-turn request 400s (the hosted
        # codex/gpt-5 driver dropped every turn 2+).
        parts.append(
            {"type": "output_text" if assistant else "input_text", "text": text}
        )
    # Only user-role items carry input images; an assistant output part is
    # output_text/refusal only, so image parts are skipped for assistant history.
    if not assistant:
        for image in _image_parts(content):
            parts.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{image['mime_type']};base64,{image['data']}",
                }
            )
    return parts


def _openai_responses_input(messages: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ToolExchange):
            payload = _assistant_payload_for_wire(message, "openai_responses")
            if not isinstance(payload, list):
                raise ProviderError("openai responses assistant turn must be a list")
            input_items.extend(payload)
            input_items.extend(_encode_tool_results_openai_responses(message.results))
            continue
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        if role == "system":
            text = _content_text(content)
            if text:
                instructions.append(text)
            continue
        mapped_role = (
            "assistant"
            if role in {"assistant", "openclaw", "agent", "model"}
            else "user"
        )
        parts = _content_to_openai_responses_parts(
            content, assistant=(mapped_role == "assistant")
        )
        if not parts:
            continue
        input_items.append({"role": mapped_role, "content": parts})
    if not input_items:
        input_items.append(
            {"role": "user", "content": [{"type": "input_text", "text": "Say ok."}]}
        )
    return "\n\n".join(instructions).strip(), input_items


def _extract_openai_responses_output(body: dict[str, Any]) -> tuple[str, str]:
    output = body.get("output")
    if not isinstance(output, list):
        return "", ""
    reply_parts: list[str] = []
    reasoning_parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "message":
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"output_text", "text"}:
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        reply_parts.append(text.strip())
        elif item_type == "reasoning":
            summary = item.get("summary")
            if not isinstance(summary, list):
                continue
            for part in summary:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    reasoning_parts.append(text.strip())
    return "\n".join(reply_parts).strip(), "\n\n".join(reasoning_parts).strip()


def _extract_openai_responses_media(body: dict[str, Any]) -> list[dict]:
    output = body.get("output")
    if not isinstance(output, list):
        return []
    media: list[dict] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "image_generation_call":
            continue
        result = item.get("result")
        mime_type = item.get("mime_type") or item.get("mimeType") or "image/png"
        normalized = _inline_media_item(result, mime_type)
        if normalized is not None:
            media.append(normalized)
            if len(media) >= MAX_GENERATED_IMAGES_PER_REPLY:
                break
    return media


# openai-responses 的 wire 编解码是 sync（_chat_completion_openai_responses）
# 与 async（chat_completion_async）共用的单实现——同 openai-compat 段的分工
# 方式：payload 构造（含 reasoning/response_format 处理）、响应解析都在下面
# 两个纯函数里，两个调用方只各自保留 httpx sync/async 的 transport 差异。


def _build_openai_responses_payload(
    *,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    response_format: dict[str, Any] | None,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
    prompt_cache_key: str = "",
    allow_image_output: bool = False,
) -> tuple[dict[str, Any], str, dict[str, str]]:
    instructions, input_items = _openai_responses_input(messages)
    if response_format:
        json_instruction = _json_only_instruction(response_format)
        if json_instruction:
            instructions = f"{instructions}\n\n{json_instruction}".strip()
    payload: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "max_output_tokens": max(1, min(int(max_tokens), 8192)),
        "store": False,
    }
    if include_reasoning:
        payload["reasoning"] = {"effort": "medium", "summary": "concise"}
    if instructions:
        payload["instructions"] = instructions
    encoded_tools = _encode_tools_openai_responses(tools) if tools else []
    if allow_image_output:
        encoded_tools.append({"type": "image_generation"})
    if encoded_tools:
        payload["tools"] = encoded_tools
    if cache_key := _cache_key(prompt_cache_key):
        payload["prompt_cache_key"] = cache_key

    url = f"{base_url.rstrip('/')}/responses"
    headers = _headers(ProviderConfig("openai", model, key, base_url))
    return payload, url, headers


def _parse_openai_responses_body(
    body: dict[str, Any],
    *,
    model: str,
    require_reply: bool,
) -> dict[str, Any]:
    reply, reasoning = _extract_openai_responses_output(body)
    # See _parse_openai_compat_body: a pure tool-call response has no reply text
    # and must not be rejected — require reply only when no tool_calls are present.
    tool_calls = _decode_tool_calls_openai_responses(body)
    media = _extract_openai_responses_media(body)
    if require_reply and not reply and not tool_calls and not media:
        raise ProviderError("provider response had no usable reply text")
    output = body.get("output")
    return {
        "reply": reply,
        "reasoning": reasoning,
        "usage": _normalize_usage("openai", body.get("usage")),
        "raw_id": body.get("id", ""),
        "stop_reason": str(
            (
                body.get("incomplete_details")
                if isinstance(body.get("incomplete_details"), dict)
                else {}
            ).get("reason")
            or body.get("status")
            or "",
        ).strip(),
        "provider": "openai",
        "model": model,
        "tool_calls": tool_calls,
        "media": media,
        "assistant_turn": {
            "wire": "openai_responses",
            "payload": output if isinstance(output, list) else [],
        },
    }


def _chat_completion_openai_responses(
    *,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    timeout: float,
    response_format: dict[str, Any] | None,
    require_reply: bool = True,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
    prompt_cache_key: str = "",
) -> dict[str, Any]:
    payload, url, headers = _build_openai_responses_payload(
        model=model,
        base_url=base_url,
        key=key,
        messages=messages,
        max_tokens=max_tokens,
        response_format=response_format,
        include_reasoning=include_reasoning,
        tools=tools,
        prompt_cache_key=prompt_cache_key,
    )

    def post_with_payload(request_payload: dict[str, Any]) -> httpx.Response:
        try:
            return _http_client().post(
                url, headers=headers, json=request_payload, timeout=timeout
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"provider network error: {type(e).__name__}") from e

    current_payload = payload
    fallback_codes: list[str] = []
    attempts_used = 0
    for attempt in range(1, _compatibility_attempt_limit(payload) + 1):
        attempts_used = attempt
        resp = post_with_payload(current_payload)
        try:
            _raise_for_provider_status(resp)
            break
        except ProviderError:
            decision = _cache_fallback_payload(
                current_payload, resp, require_error_hint=False
            )
            if decision is None:
                raise
            fallback_codes.append(decision.code)
            _record_compatibility_fallback(
                provider="openai", model=model, code=decision.code, attempt=attempt
            )
            current_payload = decision.payload
    else:  # pragma: no cover — every fallback removes one finite field
        _raise_for_provider_status(resp)

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")
    result = _parse_openai_responses_body(
        body, model=model, require_reply=require_reply
    )
    return _with_request_diagnostics(
        result,
        initial_payload=payload,
        successful_payload=current_payload,
        attempts=attempts_used,
        fallback_codes=fallback_codes,
    )


# openai-compat 的 wire 编解码是 sync（_chat_completion_openai_compatible）与
# async（chat_completion_async）共用的单实现：payload 构造、openrouter reasoning
# 400/422 降级判定、响应解析都在下面三个纯函数里，两个调用方只各自保留
# httpx sync/async 的 transport 差异。改契约改这里，两边自动同步。


def _build_openai_compat_payload(
    *,
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float | None,
    max_tokens: int,
    response_format: dict[str, Any] | None,
    extra_body: dict[str, Any] | None,
    include_reasoning: bool,
    tools: "list[ToolSpec] | None" = None,
    prompt_cache_key: str = "",
    tool_choice: str | dict[str, Any] | None = None,
    allow_image_output: bool = False,
) -> dict[str, Any]:
    encoded_messages = _encode_messages_openai_chat(messages)
    payload: dict[str, Any] = {
        "model": model,
        "messages": encoded_messages,
        "stream": False,
        "max_tokens": max(1, min(int(max_tokens), 8192)),
    }
    # temperature is OPTIONAL on this wire: the Claude 5 / GPT-5 generation rejects
    # it outright ("`temperature` is deprecated for this model" → 400). Relays pool
    # several upstream channels per model id, so only some requests hit a rejecting
    # one — the failure looks intermittent. Pass None to omit it entirely.
    if temperature is not None:
        payload["temperature"] = temperature
    if response_format:
        payload["response_format"] = response_format
    if extra_body:
        payload.update(extra_body)
    if include_reasoning and provider == "openrouter":
        payload.setdefault("reasoning", {"enabled": True, "exclude": False})
    if allow_image_output and _model_has_native_image_output(provider, model):
        payload["modalities"] = ["text", "image"]
    if tools:
        payload["tools"] = _encode_tools_openai_chat(tools)
        if tool_choice is not None:
            payload["tool_choice"] = copy.deepcopy(tool_choice)
    if cache_key := _cache_key(prompt_cache_key):
        if provider == "openai":
            payload["prompt_cache_key"] = cache_key
        elif provider == "openrouter":
            # OpenRouter documents session_id as the explicit sticky-routing
            # key. prompt_cache_key is only its fallback when session_id is
            # absent, so sending both is redundant and can unnecessarily
            # constrain endpoint selection on older upstream routes.
            payload["session_id"] = cache_key
            if "anthropic" in model.lower() or "claude" in model.lower():
                payload["messages"] = _mark_openai_chat_cache_breakpoint(
                    encoded_messages
                )
    return payload


def _reasoning_fallback_payload(
    payload: dict[str, Any],
    resp,
    *,
    provider: str,
    include_reasoning: bool,
) -> dict[str, Any] | None:
    """openrouter 对不支持 reasoning 的模型回 400/422：去掉 reasoning 重试一次。
    不适用时返回 None（调用方原样 raise）。"""
    if (
        include_reasoning
        and provider == "bedrock"
        and resp.status_code in {400, 422}
        and "additionalModelRequestFields" in payload
    ):
        try:
            detail = _response_error_detail(resp).lower()
        except Exception:  # noqa: BLE001
            return None
        if "reasoning" not in detail and "thinking" not in detail:
            return None
        fallback = dict(payload)
        fallback.pop("additionalModelRequestFields", None)
        return fallback
    if (
        include_reasoning
        and provider == "openrouter"
        and resp.status_code in {400, 422}
        and "reasoning" in payload
    ):
        try:
            detail = _response_error_detail(resp).lower()
        except Exception:  # noqa: BLE001 — no body means no safe downgrade hint
            return None
        if "reasoning" not in detail and "thinking" not in detail:
            return None
        fallback = dict(payload)
        fallback.pop("reasoning", None)
        return fallback
    return None


def _openrouter_region_route_fallback(
    payload: dict[str, Any], resp, *, provider: str
) -> _FallbackDecision | None:
    """Relax optional routing constraints after an OpenRouter region rejection."""
    has_cache_control = _contains_provider_cache_control(payload)
    if (
        provider != "openrouter"
        or getattr(resp, "status_code", None) != 403
        or not ({"session_id", "reasoning"} & payload.keys() or has_cache_control)
    ):
        return None
    try:
        detail = _response_error_detail(resp).lower()
    except Exception:  # noqa: BLE001 — no explicit region error means no retry
        return None
    if not any(
        hint in detail
        for hint in (
            "not available in your region",
            "unavailable in your region",
            "not supported in your region",
        )
    ):
        return None
    if "session_id" in payload:
        field = "session_id"
        fallback = dict(payload)
        fallback.pop(field, None)
    elif "reasoning" in payload:
        field = "reasoning"
        fallback = dict(payload)
        fallback.pop(field, None)
    else:
        field = "cache_control"
        fallback = _without_provider_cache_control(payload)
    return _FallbackDecision(fallback, f"region_route_rejected:{field}")


def _temperature_fallback_payload(
    payload: dict[str, Any], resp
) -> dict[str, Any] | None:
    """Claude 5 / GPT-5 一代弃用 temperature，带上就 400（"`temperature` is deprecated
    for this model."）：去掉 temperature 重试一次。不适用时返回 None（调用方原样 raise）。

    为什么是降级重试、而不是干脆不发 temperature：运行时调用方是**故意**传低温的
    （genesis 蒸馏 / legacy turn 传 0.0~0.1 换确定性，好让结构化抽取的 JSON 稳定解析），
    全局不发会让这些输出变随机。所以保持默认发送，只在 provider 明确因它报错时才摘掉。

    只在报错文本确实提到 temperature 时降级 —— 否则一个坏 key / 坏模型的 400 会被静默
    重试成另一个 payload，把真正的错误盖掉。"""
    inference_config = payload.get("inferenceConfig")
    has_nested_temperature = (
        isinstance(inference_config, dict) and "temperature" in inference_config
    )
    if resp.status_code not in {400, 422} or (
        "temperature" not in payload and not has_nested_temperature
    ):
        return None
    try:
        detail = resp.text or ""
    except Exception:  # noqa: BLE001 — 拿不到 body 就不猜，原样 raise
        return None
    if "temperature" not in detail.lower():
        return None
    fallback = dict(payload)
    if "temperature" in fallback:
        fallback.pop("temperature", None)
    elif has_nested_temperature:
        nested = dict(inference_config)
        nested.pop("temperature", None)
        fallback["inferenceConfig"] = nested
    return fallback


def _compatibility_fallback(
    payload: dict[str, Any],
    resp,
    *,
    provider: str,
    include_reasoning: bool,
) -> _FallbackDecision | None:
    """Choose one monotonic, privacy-safe compatibility fallback."""
    region_route = _openrouter_region_route_fallback(
        payload, resp, provider=provider
    )
    if region_route is not None:
        return region_route
    decision = _cache_fallback_payload(payload, resp, require_error_hint=True)
    if decision is not None:
        return decision
    reasoning = _reasoning_fallback_payload(
        payload, resp, provider=provider, include_reasoning=include_reasoning
    )
    if reasoning is not None:
        return _FallbackDecision(reasoning, "reasoning_rejected")
    temperature = _temperature_fallback_payload(payload, resp)
    if temperature is not None:
        return _FallbackDecision(temperature, "temperature_rejected")
    return _cache_fallback_payload(payload, resp, require_error_hint=False)


def _compatibility_attempt_limit(payload: dict[str, Any]) -> int:
    removable = len(_cache_fields_present(payload))
    removable += int(
        "reasoning" in payload or "additionalModelRequestFields" in payload
    )
    inference_config = payload.get("inferenceConfig")
    removable += int(
        "temperature" in payload
        or (isinstance(inference_config, dict) and "temperature" in inference_config)
    )
    return 1 + removable


def _record_compatibility_fallback(
    *,
    provider: str,
    model: str,
    code: str,
    attempt: int,
) -> None:
    # Fixed vocabulary only: never log response bodies, prompts, keys, or URLs.
    log.warning(
        "[provider_client] compatibility_fallback "
        "provider=%s model=%s code=%s attempt=%d",
        provider,
        model,
        code,
        attempt,
    )


def _with_request_diagnostics(
    result: dict[str, Any],
    *,
    initial_payload: dict[str, Any],
    successful_payload: dict[str, Any],
    attempts: int,
    fallback_codes: list[str],
) -> dict[str, Any]:
    """Attach non-sensitive request-path evidence to normalized usage."""
    out = dict(result)
    usage = dict(out.get("usage") or {})
    usage.update(
        {
            "provider_retry_count": max(0, int(attempts) - 1),
            "cache_hint_requested": bool(_cache_fields_present(initial_payload)),
            "cache_hint_sent_on_success": bool(
                _cache_fields_present(successful_payload)
            ),
            "compatibility_fallbacks": list(fallback_codes),
        }
    )
    out["usage"] = usage
    return out


def _with_reliable_retry_count(result: Any, retries: int) -> Any:
    """Fold outer transient retries into the same non-sensitive counter."""
    if not isinstance(result, dict) or retries <= 0:
        return result
    out = dict(result)
    usage = dict(out.get("usage") or {})
    try:
        inner = max(0, int(usage.get("provider_retry_count") or 0))
    except (TypeError, ValueError, OverflowError):
        inner = 0
    usage["provider_retry_count"] = inner + int(retries)
    usage["transient_retry_count"] = int(retries)
    out["usage"] = usage
    return out


# Private evidence carried from the provider adapter into Runtime V2's encrypted
# trajectory.  It is deliberately an in-memory return value/exception attribute:
# observing an attempt must never add a network call or database round trip to the
# provider path.  The trace stores only the JSON request body plus normalized
# provider/model metadata -- never request headers, credentials, or the base URL.
_RUNTIME_PROVIDER_ATTEMPT_TRACE_FIELD = "_runtime_provider_attempt_trace"
_RUNTIME_PROVIDER_ATTEMPT_TRACE_VERSION = 1


def _provider_status_error_class(status_code: int) -> str | None:
    if 200 <= status_code < 400:
        return None
    if status_code in _RETRYABLE_STATUS:
        return "transient"
    if status_code in _PROVIDER_CONFIG_STATUS:
        return "provider_config"
    return "unknown"


def _attempt_duration_ms(start_ns: int) -> float:
    return round(max(0, time.monotonic_ns() - start_ns) / 1_000_000.0, 3)


def _trace_envelope(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": _RUNTIME_PROVIDER_ATTEMPT_TRACE_VERSION,
        "attempts": attempts,
    }


def _with_provider_attempt_trace(
    result: Any, attempts: list[dict[str, Any]]
) -> Any:
    if not isinstance(result, dict):
        return result
    out = dict(result)
    out[_RUNTIME_PROVIDER_ATTEMPT_TRACE_FIELD] = _trace_envelope(attempts)
    return out


def _attach_provider_attempt_trace(
    exc: BaseException, attempts: list[dict[str, Any]]
) -> None:
    # All provider exceptions in this module allow attributes.  Stay defensive
    # for a foreign exception so trace observation can never replace its cause.
    try:
        exc.feedling_provider_attempt_trace = _trace_envelope(attempts)
    except Exception:  # noqa: BLE001
        pass


def _mark_attempt_postprocess_error(
    attempts: list[dict[str, Any]], exc: BaseException
) -> None:
    """Mark a transport-2xx attempt whose response failed decode/validation."""
    for entry in reversed(attempts):
        if entry.get("kind") != "http_attempt":
            continue
        if entry.get("error_class") is None:
            entry["error_class"] = classify_provider_error(exc)
            entry["outcome"] = "postprocess_error"
            entry["postprocess_stage"] = "response_decode_or_validation"
        return


def _attempts_from_result(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    envelope = result.get(_RUNTIME_PROVIDER_ATTEMPT_TRACE_FIELD)
    if not isinstance(envelope, dict):
        return []
    attempts = envelope.get("attempts")
    return list(attempts) if isinstance(attempts, list) else []


def _attempts_from_exception(exc: BaseException) -> list[dict[str, Any]]:
    envelope = getattr(exc, "feedling_provider_attempt_trace", None)
    if not isinstance(envelope, dict):
        return []
    attempts = envelope.get("attempts")
    return list(attempts) if isinstance(attempts, list) else []


def runtime_provider_attempt_trace(value: Any) -> dict[str, Any] | None:
    """Return Runtime V2's private attempt envelope from a result or exception."""
    if isinstance(value, dict):
        envelope = value.get(_RUNTIME_PROVIDER_ATTEMPT_TRACE_FIELD)
    else:
        envelope = getattr(value, "feedling_provider_attempt_trace", None)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("attempts"), list):
        return None
    return envelope


def without_runtime_provider_attempt_trace(result: Any) -> Any:
    """Drop retained wire-attempt bodies after their encrypted append completes."""
    if not isinstance(result, dict) or _RUNTIME_PROVIDER_ATTEMPT_TRACE_FIELD not in result:
        return result
    out = dict(result)
    out.pop(_RUNTIME_PROVIDER_ATTEMPT_TRACE_FIELD, None)
    return out


def _extend_attempt_trace(
    target: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    *,
    outer_attempt: int | None = None,
) -> list[int]:
    ordinals: list[int] = []
    for raw in attempts:
        item = dict(raw)
        item["ordinal"] = len(target) + 1
        if outer_attempt is not None:
            item["outer_attempt"] = outer_attempt
        target.append(item)
        ordinals.append(item["ordinal"])
    return ordinals


async def _traced_async_json_post(
    *,
    trace: list[dict[str, Any]] | None,
    provider: str,
    model: str,
    inner_attempt: int,
    request_payload: dict[str, Any],
    post: Any,
) -> tuple[httpx.Response, dict[str, Any] | None]:
    """POST once and retain exact, privacy-bounded wire evidence in memory.

    Provider payload builders and compatibility fallbacks create new mappings
    rather than mutating an attempted payload.  Keeping that mapping by reference
    therefore preserves the exact JSON body without an extra deep-copy of a large
    prompt on the latency-sensitive path.
    """
    if trace is None:
        return await post(request_payload), None

    started_ns = time.monotonic_ns()
    entry: dict[str, Any] = {
        "ordinal": len(trace) + 1,
        "kind": "http_attempt",
        "inner_attempt": int(inner_attempt),
        "provider": provider,
        "model": model,
        "status": None,
        "error_class": None,
        "compatibility_fallback": None,
        "duration_ms": 0.0,
        "wire": {
            "encoding": "json_body",
            "payload": request_payload,
        },
    }
    try:
        response = await post(request_payload)
    except Exception as exc:  # noqa: BLE001 -- retain evidence, preserve exception
        status = getattr(exc, "status_code", None)
        entry["status"] = int(status) if isinstance(status, int) else None
        entry["error_class"] = classify_provider_error(exc)
        entry["duration_ms"] = _attempt_duration_ms(started_ns)
        trace.append(entry)
        raise
    status = int(response.status_code)
    entry["status"] = status
    entry["error_class"] = _provider_status_error_class(status)
    entry["duration_ms"] = _attempt_duration_ms(started_ns)
    trace.append(entry)
    return response, entry


def _data_url_media_item(value: Any, *, name: str = "") -> dict | None:
    url = str(value or "").strip()
    if not url.lower().startswith("data:image/"):
        return None
    header, separator, encoded = url.partition(",")
    if not separator or ";base64" not in header.lower():
        return None
    mime_type = header[5:].split(";", 1)[0].strip().lower()
    return _inline_media_item(encoded, mime_type, name=name)


def _extract_openai_compatible_media(body: dict[str, Any]) -> list[dict]:
    """Accept only inline image data; provider URLs never become server fetches."""
    media: list[dict] = []
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        message = {}
    if not isinstance(message, dict):
        message = {}

    raw_images = message.get("images")
    if isinstance(raw_images, list):
        for raw in raw_images:
            if not isinstance(raw, dict):
                continue
            image_url = raw.get("image_url") or raw.get("imageUrl")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            item = _data_url_media_item(image_url)
            if item is None:
                item = _inline_media_item(
                    raw.get("b64_json") or raw.get("data"),
                    raw.get("mime_type") or raw.get("mimeType") or "image/png",
                )
            if item is not None:
                media.append(item)
                if len(media) >= MAX_GENERATED_IMAGES_PER_REPLY:
                    return media

    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            image_url = part.get("image_url") or part.get("imageUrl")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            item = _data_url_media_item(image_url)
            if item is not None:
                media.append(item)
                if len(media) >= MAX_GENERATED_IMAGES_PER_REPLY:
                    return media

    raw_data = body.get("data")
    if isinstance(raw_data, list):
        for raw in raw_data:
            if not isinstance(raw, dict):
                continue
            item = _inline_media_item(
                raw.get("b64_json"),
                raw.get("media_type") or raw.get("mime_type") or "image/png",
            )
            if item is not None:
                media.append(item)
                if len(media) >= MAX_GENERATED_IMAGES_PER_REPLY:
                    break
    return media


def _parse_openrouter_images_body(
    body: dict[str, Any],
    *,
    provider: str = "openrouter",
    model: str,
) -> dict[str, Any]:
    """Normalize a dedicated OpenAI-style Images API response as terminal media."""
    media = _extract_openai_compatible_media(body)
    if not media:
        raise ProviderError("provider response had no usable image")
    return {
        "reply": "",
        "reasoning": "",
        "usage": _normalize_usage(provider, body.get("usage")),
        "raw_id": body.get("id", ""),
        "stop_reason": "image_generated",
        "provider": provider,
        "model": model,
        "tool_calls": [],
        "media": media,
        # The result is terminal media, so it never needs a provider-native
        # assistant turn for a later tool-exchange round.
        "assistant_turn": None,
    }


def _parse_openai_compat_body(
    resp,
    *,
    provider: str,
    model: str,
    require_reply: bool,
) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")
    # A tool-call response legitimately carries NO reply text (the model chose to
    # call a tool instead of answering) — decode tool_calls first and treat their
    # presence as making reply text optional. Without this, a tool round on the
    # V2 unified loop raises "no usable reply text" on the very first provider
    # call (openai/gemini return content=null for a pure tool call), never
    # reaching the executor. A genuinely empty/error response (no text AND no
    # tool_calls) still raises when require_reply is set.
    tool_calls = _decode_tool_calls_openai_chat(body)
    media = _extract_openai_compatible_media(body)
    try:
        assistant_payload = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        assistant_payload = {}
    if not isinstance(assistant_payload, dict):
        assistant_payload = {}
    return {
        "reply": _extract_reply(
            body, required=require_reply and not tool_calls and not media
        ),
        "reasoning": _extract_openai_compatible_reasoning(body),
        "usage": _normalize_usage(provider, body.get("usage")),
        "raw_id": body.get("id", ""),
        "stop_reason": _extract_openai_compatible_stop_reason(body),
        "provider": provider,
        "model": model,
        "tool_calls": tool_calls,
        "media": media,
        "assistant_turn": {"wire": "openai_chat", "payload": assistant_payload},
    }


def _chat_completion_openai_compatible(
    *,
    provider: str,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float | None,
    timeout: float,
    response_format: dict[str, Any] | None,
    extra_body: dict[str, Any] | None = None,
    require_reply: bool = True,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
    prompt_cache_key: str = "",
) -> dict[str, Any]:
    payload = _build_openai_compat_payload(
        provider=provider,
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        extra_body=extra_body,
        include_reasoning=include_reasoning,
        tools=tools,
        prompt_cache_key=prompt_cache_key,
    )

    def post_with_payload(request_payload: dict[str, Any]) -> httpx.Response:
        try:
            return _http_client().post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=_headers(ProviderConfig(provider, model, key, base_url)),
                json=request_payload,
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"provider network error: {type(e).__name__}") from e

    current_payload = payload
    fallback_codes: list[str] = []
    attempts_used = 0
    # A relay can reject more than one optional field in sequence (for example
    # cache affinity first, then temperature).  Walk bounded, monotonic
    # fallbacks; each step removes fields only and always preserves tools.
    for attempt in range(1, _compatibility_attempt_limit(payload) + 1):
        attempts_used = attempt
        resp = post_with_payload(current_payload)
        try:
            _raise_for_provider_status(resp)
            break
        except ProviderError:
            decision = _compatibility_fallback(
                current_payload,
                resp,
                provider=provider,
                include_reasoning=include_reasoning,
            )
            if decision is None:
                raise
            fallback_codes.append(decision.code)
            _record_compatibility_fallback(
                provider=provider,
                model=model,
                code=decision.code,
                attempt=attempt,
            )
            current_payload = decision.payload
    else:  # pragma: no cover — every fallback removes at least one finite field
        _raise_for_provider_status(resp)

    result = _parse_openai_compat_body(
        resp, provider=provider, model=model, require_reply=require_reply
    )
    return _with_request_diagnostics(
        result,
        initial_payload=payload,
        successful_payload=current_payload,
        attempts=attempts_used,
        fallback_codes=fallback_codes,
    )


# anthropic 的 wire 编解码是 sync（_chat_completion_anthropic）与 async
# （chat_completion_async）共用的单实现：payload 构造（含 thinking-budget 判定）、
# 响应解析都在下面两个纯函数里，两个调用方只各自保留 httpx sync/async 的
# transport 差异。改契约改这里，两边自动同步。


def _build_anthropic_payload(
    *,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float | None,
    response_format: dict[str, Any] | None,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
    prompt_cache_key: str = "",
) -> tuple[dict[str, Any], str, dict[str, str]]:
    cacheable_system_parts = [
        _content_text(message.get("content"))
        for message in messages
        if isinstance(message, dict)
        and str(message.get("role") or "").strip().lower() == "system"
        and _content_text(message.get("content"))
    ]
    system, provider_messages = _split_system_messages_anthropic(messages)
    json_instruction = _json_only_instruction(response_format)
    if json_instruction:
        system = f"{system}\n\n{json_instruction}".strip()
    capped_max_tokens = max(1, min(int(max_tokens), 8192))
    payload: dict[str, Any] = {
        "model": model,
        "messages": provider_messages,
        "max_tokens": capped_max_tokens,
    }
    if (
        include_reasoning
        and _anthropic_supports_thinking(model)
        and capped_max_tokens >= 1536
    ):
        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": min(1024, capped_max_tokens - 512),
        }
    elif temperature is not None:
        payload["temperature"] = temperature
    if _cache_key(prompt_cache_key):
        system, provider_messages = _mark_anthropic_cache_breakpoint(
            system,
            provider_messages,
            system_parts=cacheable_system_parts,
        )
        payload["messages"] = provider_messages
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = _encode_tools_anthropic(tools)
    # The opaque affinity key itself is intentionally not sent on Anthropic's
    # wire.  ``cache_control`` lives on the stable system/message content block
    # above; top-level cache_control is not part of the Messages API schema.

    url = f"{base_url.rstrip('/')}/messages"
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    return payload, url, headers


def _parse_anthropic_body(
    body: dict[str, Any],
    *,
    model: str,
    require_reply: bool,
) -> dict[str, Any]:
    # See _parse_openai_compat_body: a pure tool-call response has no reply text
    # and must not be rejected — require reply only when no tool_calls are present.
    tool_calls = _decode_tool_calls_anthropic(body)
    content = body.get("content")
    return {
        "reply": _extract_anthropic_reply(
            body, required=require_reply and not tool_calls
        ),
        "reasoning": _extract_anthropic_reasoning(body),
        "usage": _normalize_usage("anthropic", body.get("usage")),
        "raw_id": body.get("id", ""),
        "stop_reason": str(body.get("stop_reason") or "").strip(),
        "provider": "anthropic",
        "model": model,
        "tool_calls": tool_calls,
        "assistant_turn": {
            "wire": "anthropic",
            "payload": content if isinstance(content, list) else [],
        },
    }


def _chat_completion_anthropic(
    *,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    response_format: dict[str, Any] | None,
    require_reply: bool = True,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
    prompt_cache_key: str = "",
) -> dict[str, Any]:
    payload, url, headers = _build_anthropic_payload(
        model=model,
        base_url=base_url,
        key=key,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        include_reasoning=include_reasoning,
        tools=tools,
        prompt_cache_key=prompt_cache_key,
    )

    def post_with_payload(request_payload: dict[str, Any]) -> httpx.Response:
        try:
            return _http_client().post(
                f"{base_url.rstrip('/')}/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=request_payload,
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"provider network error: {type(e).__name__}") from e

    current_payload = payload
    fallback_codes: list[str] = []
    attempts_used = 0
    for attempt in range(1, _compatibility_attempt_limit(payload) + 1):
        attempts_used = attempt
        resp = post_with_payload(current_payload)
        try:
            _raise_for_provider_status(resp)
            break
        except ProviderError:
            # Same temperature downgrade as the openai-compat wire, plus a
            # cache-off retry for older Anthropic relays.  Both preserve tools.
            decision = _compatibility_fallback(
                current_payload,
                resp,
                provider="anthropic",
                include_reasoning=False,
            )
            if decision is None:
                raise
            fallback_codes.append(decision.code)
            _record_compatibility_fallback(
                provider="anthropic",
                model=model,
                code=decision.code,
                attempt=attempt,
            )
            current_payload = decision.payload
    else:  # pragma: no cover — finite monotonic fallback set
        _raise_for_provider_status(resp)

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")

    result = _parse_anthropic_body(body, model=model, require_reply=require_reply)
    return _with_request_diagnostics(
        result,
        initial_payload=payload,
        successful_payload=current_payload,
        attempts=attempts_used,
        fallback_codes=fallback_codes,
    )


# Amazon Bedrock Converse is a separate provider-native wire. Do not route it
# through the Anthropic Messages adapter: Converse has different tool/result
# unions, cache checkpoints, response usage fields, and bearer-token auth.


def _mark_bedrock_message_cache_breakpoints(
    messages: list[dict[str, Any]],
    *,
    max_breakpoints: int,
) -> list[dict[str, Any]]:
    updated = copy.deepcopy(messages)
    limit = max(0, int(max_breakpoints))
    if not limit:
        return updated

    # Converse coalesces adjacent user turns. Working memory, summary, tail,
    # and live runtime data may therefore share one provider message. Insert a
    # checkpoint immediately after the stable working-memory content block,
    # rather than at the end of a message that may also contain live data.
    used = 0
    for message in updated:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for index, block in enumerate(content):
            if (
                isinstance(block, dict)
                and _WORKING_MEMORY_HEADER in str(block.get("text") or "")
            ):
                if not (
                    index + 1 < len(content)
                    and isinstance(content[index + 1], dict)
                    and "cachePoint" in content[index + 1]
                ):
                    content.insert(
                        index + 1, {"cachePoint": {"type": "default"}}
                    )
                used = 1
                break
        if used:
            break
    if used >= limit:
        return updated

    candidates = [
        index
        for index, message in enumerate(updated)
        if isinstance(message, dict)
        and not _is_runtime_context_message(message)
        and isinstance(message.get("content"), list)
        and bool(message.get("content"))
    ]
    user_candidates = [
        index for index in candidates if updated[index].get("role") == "user"
    ]
    chosen: list[int] = []
    for index in user_candidates[-2:]:
        if index not in chosen and len(chosen) < limit - used:
            chosen.append(index)
    for index in reversed(candidates):
        if index not in chosen and len(chosen) < limit - used:
            chosen.append(index)
    for index in chosen:
        content = updated[index]["content"]
        if not (
            content
            and isinstance(content[-1], dict)
            and "cachePoint" in content[-1]
        ):
            content.append({"cachePoint": {"type": "default"}})
    return updated


def _build_bedrock_payload(
    *,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float | None,
    response_format: dict[str, Any] | None,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
    prompt_cache_key: str = "",
) -> tuple[dict[str, Any], str, dict[str, str]]:
    system_parts, provider_messages = _split_system_messages_bedrock(messages)
    json_instruction = _json_only_instruction(response_format)
    if json_instruction:
        system_parts.append(json_instruction)

    capped_max_tokens = max(1, min(int(max_tokens), 8192))
    inference_config: dict[str, Any] = {"maxTokens": capped_max_tokens}
    thinking_enabled = (
        include_reasoning
        and "anthropic." in model.lower()
        and _anthropic_supports_thinking(model)
        and capped_max_tokens >= 1536
    )
    if not thinking_enabled and temperature is not None:
        inference_config["temperature"] = temperature

    payload: dict[str, Any] = {
        "messages": provider_messages,
        "inferenceConfig": inference_config,
    }
    if system_parts:
        payload["system"] = [{"text": part} for part in system_parts]
    if thinking_enabled:
        payload["additionalModelRequestFields"] = {
            "thinking": {
                "type": "enabled",
                "budget_tokens": min(1024, capped_max_tokens - 512),
            },
        }
    if tools:
        payload["toolConfig"] = {"tools": _encode_tools_bedrock(tools)}

    if _cache_key(prompt_cache_key):
        # Converse evaluates cache checkpoints in tools -> system -> messages
        # order and permits at most four for current Claude families. Put the
        # most stable request components first, then use the remaining slots for
        # advancing conversation boundaries. The opaque Feedling affinity key
        # only enables this behavior; it is never sent to AWS.
        used = 0
        if "toolConfig" in payload:
            payload["toolConfig"]["tools"].append({"cachePoint": {"type": "default"}})
            used += 1
        if "system" in payload:
            payload["system"].append({"cachePoint": {"type": "default"}})
            used += 1
        payload["messages"] = _mark_bedrock_message_cache_breakpoints(
            payload["messages"], max_breakpoints=max(0, 4 - used)
        )

    url = f"{base_url.rstrip('/')}/model/{quote(model, safe='')}/converse"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    return payload, url, headers


def _bedrock_output_content(body: dict[str, Any]) -> list[dict[str, Any]] | None:
    output = body.get("output")
    if not isinstance(output, dict):
        return None
    message = output.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, list) else None


def _extract_bedrock_reply(body: dict[str, Any], *, required: bool = True) -> str:
    content = _bedrock_output_content(body)
    if content is not None:
        parts = [
            str(block.get("text") or "").strip()
            for block in content
            if isinstance(block, dict) and str(block.get("text") or "").strip()
        ]
        if parts:
            return "\n".join(parts)
        if not required:
            return ""
    raise ProviderError("provider response had no usable reply text")


def _extract_bedrock_reasoning(body: dict[str, Any]) -> str:
    content = _bedrock_output_content(body)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        reasoning = block.get("reasoningContent")
        if not isinstance(reasoning, dict):
            continue
        reasoning_text = reasoning.get("reasoningText")
        if isinstance(reasoning_text, dict):
            text = reasoning_text.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        elif "redactedContent" in reasoning:
            parts.append("[redacted thinking]")
    return "\n\n".join(parts)


def _parse_bedrock_body(
    body: dict[str, Any],
    *,
    model: str,
    require_reply: bool,
) -> dict[str, Any]:
    tool_calls = _decode_tool_calls_bedrock(body)
    content = _bedrock_output_content(body)
    return {
        "reply": _extract_bedrock_reply(
            body, required=require_reply and not tool_calls
        ),
        "reasoning": _extract_bedrock_reasoning(body),
        "usage": _normalize_usage("bedrock", body.get("usage")),
        "raw_id": str(body.get("requestId") or ""),
        "stop_reason": str(body.get("stopReason") or "").strip(),
        "provider": "bedrock",
        "model": model,
        "tool_calls": tool_calls,
        "assistant_turn": {
            "wire": "bedrock",
            "payload": content if isinstance(content, list) else [],
        },
    }


def _chat_completion_bedrock(
    *,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float | None,
    timeout: float,
    response_format: dict[str, Any] | None,
    require_reply: bool = True,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
    prompt_cache_key: str = "",
) -> dict[str, Any]:
    payload, url, headers = _build_bedrock_payload(
        model=model,
        base_url=base_url,
        key=key,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        include_reasoning=include_reasoning,
        tools=tools,
        prompt_cache_key=prompt_cache_key,
    )

    def post_with_payload(request_payload: dict[str, Any]) -> httpx.Response:
        try:
            return _http_client().post(
                url, headers=headers, json=request_payload, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"provider network error: {type(exc).__name__}"
            ) from exc

    current_payload = payload
    fallback_codes: list[str] = []
    attempts_used = 0
    for attempt in range(1, _compatibility_attempt_limit(payload) + 1):
        attempts_used = attempt
        resp = post_with_payload(current_payload)
        try:
            _raise_for_provider_status(resp)
            break
        except ProviderError:
            decision = _compatibility_fallback(
                current_payload,
                resp,
                provider="bedrock",
                include_reasoning=False,
            )
            if decision is None:
                raise
            fallback_codes.append(decision.code)
            _record_compatibility_fallback(
                provider="bedrock", model=model, code=decision.code, attempt=attempt
            )
            current_payload = decision.payload
    else:  # pragma: no cover - every fallback removes finite optional state
        _raise_for_provider_status(resp)

    try:
        body = resp.json()
    except ValueError as exc:
        raise ProviderError("provider returned non-json response") from exc
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")
    result = _parse_bedrock_body(body, model=model, require_reply=require_reply)
    return _with_request_diagnostics(
        result,
        initial_payload=payload,
        successful_payload=current_payload,
        attempts=attempts_used,
        fallback_codes=fallback_codes,
    )


# gemini 的 wire 编解码是 sync（_chat_completion_gemini）与 async
# （chat_completion_async）共用的单实现——同上一段注释的分工方式。


def _build_gemini_payload(
    *,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float | None,
    response_format: dict[str, Any] | None,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
    allow_image_output: bool = False,
) -> tuple[dict[str, Any], str, dict[str, str]]:
    system, contents = _split_system_messages_gemini(messages)
    generation_config: dict[str, Any] = {
        "maxOutputTokens": max(1, min(int(max_tokens), 8192)),
    }
    if temperature is not None:
        generation_config["temperature"] = temperature
    if response_format and response_format.get("type") in {
        "json_object",
        "json_schema",
    }:
        generation_config["responseMimeType"] = "application/json"
    if include_reasoning and "2.5" in model:
        generation_config["thinkingConfig"] = {
            "thinkingBudget": min(1024, max(128, int(max_tokens) // 2)),
            "includeThoughts": True,
        }
    if allow_image_output and _model_has_native_image_output("gemini", model):
        generation_config["responseModalities"] = ["TEXT", "IMAGE"]

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation_config,
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        payload["tools"] = _encode_tools_gemini(tools)

    url = f"{base_url.rstrip('/')}/models/{quote(model, safe='')}:generateContent"
    headers = {
        "x-goog-api-key": key,
        "Content-Type": "application/json",
    }
    return payload, url, headers


def _parse_gemini_body(
    body: dict[str, Any],
    *,
    model: str,
    require_reply: bool,
) -> dict[str, Any]:
    # See _parse_openai_compat_body: a pure tool-call response has no reply text
    # and must not be rejected — require reply only when no tool_calls are present.
    tool_calls = _decode_tool_calls_gemini(body)
    media = _extract_gemini_media(body)
    try:
        assistant_payload = body["candidates"][0]["content"]
    except (KeyError, IndexError, TypeError):
        assistant_payload = {}
    if not isinstance(assistant_payload, dict):
        assistant_payload = {}
    return {
        "reply": _extract_gemini_reply(
            body, required=require_reply and not tool_calls and not media
        ),
        "reasoning": _extract_gemini_reasoning(body),
        "usage": _normalize_usage("gemini", body.get("usageMetadata")),
        "raw_id": body.get("responseId", ""),
        "stop_reason": _extract_gemini_stop_reason(body),
        "provider": "gemini",
        "model": model,
        "tool_calls": tool_calls,
        "media": media,
        "assistant_turn": {"wire": "gemini", "payload": assistant_payload},
    }


def _chat_completion_gemini(
    *,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    response_format: dict[str, Any] | None,
    require_reply: bool = True,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
) -> dict[str, Any]:
    payload, url, headers = _build_gemini_payload(
        model=model,
        base_url=base_url,
        key=key,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        include_reasoning=include_reasoning,
        tools=tools,
    )

    try:
        resp = _http_client().post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        raise ProviderError(f"provider network error: {type(e).__name__}") from e

    _raise_for_provider_status(resp)

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")

    return _parse_gemini_body(body, model=model, require_reply=require_reply)


def chat_completion(
    config: ProviderConfig,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 700,
    temperature: float | None = 0.7,
    timeout: float = 60.0,
    response_format: dict[str, Any] | None = None,
    require_reply: bool = True,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
) -> dict[str, Any]:
    provider, model, base_url = validate_config(
        config.provider, config.model, config.base_url
    )
    request_model, extra_body = _runtime_model(provider, model)
    key = (config.api_key or "").strip()
    if not key:
        raise ProviderError("api_key required")

    if provider == "anthropic":
        return _chat_completion_anthropic(
            model=model,
            base_url=base_url,
            key=key,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            response_format=response_format,
            require_reply=require_reply,
            include_reasoning=include_reasoning,
            tools=tools,
            prompt_cache_key=config.prompt_cache_key,
        )
    if provider == "bedrock":
        return _chat_completion_bedrock(
            model=model,
            base_url=base_url,
            key=key,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            response_format=response_format,
            require_reply=require_reply,
            include_reasoning=include_reasoning,
            tools=tools,
            prompt_cache_key=config.prompt_cache_key,
        )
    if provider == "gemini":
        return _chat_completion_gemini(
            model=model,
            base_url=base_url,
            key=key,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            response_format=response_format,
            require_reply=require_reply,
            include_reasoning=include_reasoning,
            tools=tools,
        )

    if provider == "openai" and _openai_uses_responses_for_reasoning(request_model):
        return _chat_completion_openai_responses(
            model=request_model,
            base_url=base_url,
            key=key,
            messages=messages,
            max_tokens=max_tokens,
            timeout=timeout,
            response_format=response_format,
            require_reply=require_reply,
            include_reasoning=include_reasoning,
            tools=tools,
            prompt_cache_key=config.prompt_cache_key,
        )

    return _chat_completion_openai_compatible(
        provider=provider,
        model=request_model,
        base_url=base_url,
        key=key,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        response_format=response_format,
        extra_body=extra_body,
        require_reply=require_reply,
        include_reasoning=include_reasoning,
        tools=tools,
        prompt_cache_key=config.prompt_cache_key,
    )


# `probe_responses_support` lived here until 2026-07-27. It probed POST
# /responses on openai_compatible relays to choose native passthrough vs the
# LiteLLM responses→chat-completions bridge. Both ends of that choice are gone:
# the LiteLLM gateway is retired, and openai_compatible now derives the `pi`
# label (hosted/agent_runtime_cutover.py), never `codex` — so its traffic can
# never reach the one `/responses` caller left in this module, which is gated on
# `provider == "openai"`. Its only surviving consumer was a setup warning telling
# chat-only relay users (Moonshot) that memory/tool calls were unreliable; both
# were verified working end to end on V1 and V2 before removal.


# --- BYOK model catalog: list a provider's models via its /models endpoint ----
# Pure-additive bypass. NONE of these touch chat_completion / test_provider_key /
# encryption. Wire logic (target validation / request build / streaming fetch /
# strict page parse / bounded pagination / error classification) lives here in
# the leaf layer, mirroring the rest of provider_client.
#
# Resource discipline is a single WHOLE-CALL budget, not a per-page fudge: one
# monotonic wall-clock deadline shared across every page, each page's socket
# timeout = the remaining budget, and a decompressed-byte cap summed across the
# entire call (streamed, so a hostile endpoint can't buffer us to death before
# we notice). Later-page failures degrade to partial success; the first page
# still raises.
_CATALOG_TOTAL_BUDGET = 20.0          # seconds, wall-clock, across all pages
# Tight PER-PHASE httpx timeouts. These bound the connect/TLS/header phases and
# the idle-wait between chunks, but they are NOT a total-time bound: httpx's
# read timeout only caps waiting for the NEXT chunk, so a slow-dripping endpoint
# that keeps sending small chunks would run far past _CATALOG_TOTAL_BUDGET. The
# real wall-clock bound is enforced by the in-loop deadline check in
# _fetch_catalog_page. Future hardening: a full async wire wrapped in
# asyncio.timeout(_CATALOG_TOTAL_BUDGET) would give a single truly-cancellable
# total bound that also covers the connect/TLS phase.
_CATALOG_CONNECT_TIMEOUT = 5.0        # seconds, connect/write/pool phase cap
_CATALOG_READ_TIMEOUT = 10.0          # seconds, wait-for-next-chunk cap
_CATALOG_MAX_PAGES = 10
_CATALOG_MAX_MODELS = 2000
_CATALOG_MAX_BODY_BYTES = 5_000_000   # decompressed bytes, summed across pages
_CATALOG_MAX_ID_LEN = 160

_CATALOG_BEARER_PROVIDERS = {"openai", "openrouter", "deepseek", "openai_compatible"}
_CATALOG_SUPPORTED_PROVIDERS = {
    "openai", "openrouter", "anthropic", "bedrock", "gemini",
    "deepseek", "openai_compatible",
}


def validate_catalog_target(provider: str, base_url: str = "") -> tuple[str, str]:
    """Validate provider + base_url for the catalog path (no model required).

    Mirrors ``validate_config``'s provider allowlist and URL-scheme rule
    (``https://`` or local ``http://127.0.0.1`` only) minus the model check, so
    the catalog endpoint is not a looser server-side egress than save/test. An
    UNKNOWN provider raises — the caller must surface an error, never silently
    treat it as "catalog unsupported". Returns the normalized ``(provider,
    base_url)`` with the provider default filled in when blank.
    """
    provider = normalize_provider(provider)
    base_url = (base_url or "").strip().rstrip("/")
    if provider not in _CATALOG_SUPPORTED_PROVIDERS:
        raise ProviderError(
            "provider must be openai, openrouter, anthropic, bedrock, gemini, "
            "deepseek, or openai_compatible"
        )
    if provider == "openai_compatible" and not base_url:
        raise ProviderError("base_url required for openai_compatible")
    if base_url:
        _validate_egress_url(base_url)
    if not base_url:
        base_url = default_base_url(provider)
    if not base_url:
        raise ProviderError("base_url unavailable for provider")
    return provider, base_url


_CATALOG_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def _validate_egress_url(base_url: str) -> None:
    """Reject any egress target that isn't ``https://`` or an EXACT loopback
    ``http://`` host — parsed with ``urlsplit``, never ``startswith``.

    A ``startswith("http://127.0.0.1")`` prefix check is forgeable: both
    ``http://127.0.0.1.evil.example`` (real host ``evil.example``) and
    ``http://127.0.0.1@evil.example`` (userinfo ``127.0.0.1``, real host
    ``evil.example``) pass a prefix check and would send the provider key in
    plaintext to the attacker. urlsplit resolves the true ``hostname`` and lets
    us forbid userinfo outright.
    """
    parts = urlsplit(base_url)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise ProviderError("base_url must be https:// or local http://127.0.0.1")
    # Forbid userinfo: "http://127.0.0.1@evil.example" hides the real host after
    # the '@'. Reject on any credential component or a stray '@' in netloc.
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise ProviderError("base_url must be https:// or local http://127.0.0.1")
    if scheme == "http":
        host = (parts.hostname or "").lower()
        if host not in _CATALOG_LOOPBACK_HOSTS:
            raise ProviderError("base_url must be https:// or local http://127.0.0.1")


def _catalog_request(provider: str, api_key: str, base_url: str,
                     cursor: str | None) -> tuple[str, dict, dict]:
    provider = normalize_provider(provider)
    base = (base_url or default_base_url(provider)).rstrip("/")
    if provider == "bedrock":
        raise ProviderError("model_catalog_unsupported")
    # OpenRouter's public /models catalog ignores the caller's privacy settings,
    # ZDR policy, and guardrails. /models/user applies those constraints to this
    # exact API key. Keep this request unmodified: carrying public-catalog filters
    # such as output_modalities=all broadens the response beyond that default.
    url = f"{base}/models/user" if provider == "openrouter" else f"{base}/models"
    params: dict = {}
    if provider in _CATALOG_BEARER_PROVIDERS:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://feedling.app"
            headers["X-Title"] = "Feedling IO Hosted Runtime"
    elif provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        params["limit"] = 1000
        if cursor:
            params["after_id"] = cursor
    elif provider == "gemini":
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        params["pageSize"] = 1000
        if cursor:
            params["pageToken"] = cursor
    else:
        raise ProviderError("model_catalog_unsupported")
    return url, headers, params


def _parse_catalog_page(provider: str, body: Any) -> tuple[list[dict], str | None]:
    """Strictly parse one catalog page → ``(models, next_cursor)``.

    Strictness rules (a permissive parser would mislabel garbage as an empty
    catalog and 500 on non-object members):
    - root must be a dict carrying the expected array (``models`` for gemini,
      else ``data``). Missing/renamed array (``{}``, ``{"error": ...}``) is an
      ``model_catalog_invalid_response`` — NOT an empty success. A PRESENT empty
      list IS a legitimate empty catalog.
    - each array member must be a dict; non-object members are skipped, never
      allowed to raise ``AttributeError`` → 500.
    - id must be a non-empty ``str`` ≤ 160 chars.
    - anthropic ``has_more=true`` with a missing/blank ``last_id`` is treated as
      truncated/invalid (raise), never signalled as a complete page.
    """
    provider = normalize_provider(provider)
    if not isinstance(body, dict):
        raise ProviderError("model_catalog_invalid_response")

    array_key = "models" if provider == "gemini" else "data"
    raw = body.get(array_key)
    if not isinstance(raw, list):
        raise ProviderError("model_catalog_invalid_response")

    out: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        if provider == "gemini":
            name = m.get("name")
            if not isinstance(name, str):
                continue
            mid = name.split("/", 1)[1] if name.startswith("models/") else name
            disp = m.get("displayName")
        else:
            mid = m.get("id")
            if not isinstance(mid, str):
                continue
            disp = m.get("display_name") or m.get("name")
        mid = mid.strip()
        if not mid or len(mid) > _CATALOG_MAX_ID_LEN:
            continue
        display = disp if isinstance(disp, str) and disp else mid
        model = {"id": mid, "display_name": display}
        input_modalities = _catalog_input_modalities(provider, m)
        if input_modalities is not None:
            model["input_modalities"] = input_modalities
        out.append(model)

    nxt: str | None = None
    if provider == "gemini":
        tok = body.get("nextPageToken")
        nxt = tok if isinstance(tok, str) and tok else None
    elif provider == "anthropic" and body.get("has_more"):
        last = body.get("last_id")
        if isinstance(last, str) and last:
            nxt = last
        else:
            raise ProviderError("model_catalog_invalid_response")
    return out, nxt


def _catalog_input_modalities(provider: str, model: dict) -> list[str] | None:
    """Return only modality fields explicitly supplied by this catalog row.

    No maintained model table, provider-level default, or model-name heuristic
    belongs here. OpenAI, DeepSeek, Gemini, and other catalogs that omit an
    official per-row modality field remain unknown and fall through to the real
    image probe.
    """
    provider = normalize_provider(provider)
    raw = None
    if provider == "openrouter":
        architecture = model.get("architecture")
        if isinstance(architecture, dict):
            raw = architecture.get("input_modalities")
    elif provider == "anthropic":
        capabilities = model.get("capabilities")
        image_input = (
            capabilities.get("image_input")
            if isinstance(capabilities, dict)
            else None
        )
        supported = (
            image_input.get("supported")
            if isinstance(image_input, dict)
            else None
        )
        if isinstance(supported, bool):
            return ["text", "image"] if supported else ["text"]
    elif provider == "openai_compatible":
        raw = model.get("input_modalities")

    if not isinstance(raw, list):
        return None
    allowed = {"text", "image", "audio", "video"}
    cleaned = sorted({
        str(item).strip().lower()
        for item in raw
        if isinstance(item, str) and str(item).strip().lower() in allowed
    })
    # Preserve an explicit empty field as an explicit "no supported inputs"
    # declaration. Missing/malformed fields remain None and fall back to probe.
    return cleaned


def _fetch_catalog_page(client: httpx.Client, url: str, headers: dict,
                        params: dict, deadline: float,
                        bytes_so_far: int) -> tuple[dict, int]:
    """Stream one page, enforcing the byte cap AND the wall-clock deadline.

    ``deadline`` is an absolute ``time.monotonic()`` instant shared across all
    pages. Per-phase httpx timeouts (see the constants above) bound the connect/
    header phases and the wait for the next chunk; the total-time bound is
    enforced by re-checking ``time.monotonic()`` inside the chunk loop, because
    httpx's read timeout does NOT bound total response duration — a slow-drip
    endpoint could otherwise hold the sync threadpool far past the budget.

    Returns ``(parsed_json_dict, new_running_byte_total)``. Raises
    ``ProviderError`` on http>=400 (carrying ``status_code``), network failure
    (``status_code=None``), the size cap, the deadline, non-JSON, or a non-dict
    root. The caller decides first-page (raise) vs later-page (partial) handling.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderError("provider network error: DeadlineExceeded")
    # Cap each phase by the smaller of its own budget and the whole remaining
    # wall-clock, so connect/TLS/header can never themselves outlast the budget.
    timeout = httpx.Timeout(
        connect=min(_CATALOG_CONNECT_TIMEOUT, remaining),
        read=min(_CATALOG_READ_TIMEOUT, remaining),
        write=min(_CATALOG_CONNECT_TIMEOUT, remaining),
        pool=min(_CATALOG_CONNECT_TIMEOUT, remaining),
    )
    chunks: list[bytes] = []
    total = bytes_so_far
    try:
        with client.stream("GET", url, headers=headers, params=params,
                           timeout=timeout) as resp:
            status = resp.status_code
            if status >= 400:
                raise ProviderError(f"provider_http_{status}", status_code=status)
            for chunk in resp.iter_bytes():
                if time.monotonic() >= deadline:
                    # Slow-drip past the wall-clock budget: stop reading now and
                    # treat as a deadline hit. status_code=None → the caller /
                    # slug mapper classifies this as temporarily_unavailable, and
                    # a later page keeps whatever was already collected (partial).
                    raise ProviderError("provider network error: DeadlineExceeded")
                total += len(chunk)
                if total > _CATALOG_MAX_BODY_BYTES:
                    raise ProviderError("model_catalog_invalid_response")
                chunks.append(chunk)
    except ProviderError:
        raise
    except httpx.HTTPError as e:
        raise ProviderError(f"provider network error: {type(e).__name__}") from e
    try:
        body = json.loads(b"".join(chunks))
    except Exception as e:
        raise ProviderError("model_catalog_invalid_response") from e
    if not isinstance(body, dict):
        raise ProviderError("model_catalog_invalid_response")
    return body, total


def list_provider_models(
    provider: str,
    api_key: str,
    base_url: str = "",
    *,
    total_budget_sec: float | None = None,
) -> dict:
    provider = normalize_provider(provider)
    warnings: list[str] = []
    try:
        _catalog_request(provider, api_key, base_url, None)
    except ProviderError as e:
        if "model_catalog_unsupported" in str(e):
            return {"models": [], "complete": True, "warnings": [],
                    "catalog_supported": False}
        raise

    seen: set[str] = set()
    seen_cursors: set[str] = set()
    models: list[dict] = []
    cursor: str | None = None
    complete = True
    total_bytes = 0
    budget = _CATALOG_TOTAL_BUDGET if total_budget_sec is None else float(total_budget_sec)
    deadline = time.monotonic() + max(0.1, budget)
    client = _http_client()

    for _page in range(_CATALOG_MAX_PAGES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            complete = False
            warnings.append("model list truncated: time budget exceeded")
            break
        url, headers, params = _catalog_request(provider, api_key, base_url, cursor)
        try:
            body, total_bytes = _fetch_catalog_page(
                client, url, headers, params, deadline, total_bytes)
            page_models, next_cursor = _parse_catalog_page(provider, body)
        except ProviderError as e:
            if _page == 0:
                # FIRST fetch failed — key on the page index, NOT on model count.
                # A legitimately-empty first page that carries a next cursor,
                # followed by a later-page failure, must NOT be treated as a
                # first-page failure just because zero models were collected.
                # A 404 on a custom endpoint just means "no /models route" → let
                # the client fall back to manual entry, don't error.
                if (provider == "openai_compatible"
                        and getattr(e, "status_code", None) == 404):
                    return {"models": [], "complete": True, "warnings": [],
                            "catalog_supported": False}
                raise
            # A LATER page failed (network / http / size / bad shape / deadline):
            # partial success — keep whatever earlier pages returned (possibly an
            # empty set) and mark incomplete, regardless of model count.
            complete = False
            warnings.append(f"catalog page dropped: {e}")
            break

        cap_hit = False
        for idx, m in enumerate(page_models):
            mid = m["id"]
            if mid in seen:
                continue
            seen.add(mid)
            models.append(m)
            if len(models) >= _CATALOG_MAX_MODELS:
                # Only truncated if there is GENUINELY more to consume: members
                # left on this page, or a next cursor. Hitting the cap exactly on
                # the last member of the last page is a COMPLETE catalog.
                if page_models[idx + 1:] or next_cursor:
                    complete = False
                    warnings.append("model list truncated: model cap reached")
                cap_hit = True
                break
        if cap_hit:
            break

        cursor = next_cursor
        if not cursor:
            break
        if cursor in seen_cursors:
            # Provider handed back a cursor we already followed → would loop.
            complete = False
            warnings.append("model list truncated: repeated pagination cursor")
            break
        seen_cursors.add(cursor)
    else:
        # Ran the full page range with a cursor still outstanding.
        if cursor:
            complete = False
            warnings.append("model list truncated: page cap reached")

    return {"models": models, "complete": complete, "warnings": warnings,
            "catalog_supported": True}


def model_catalog_error_slug(exc: BaseException) -> str:
    """Map a catalog ``ProviderError`` to a stable client-facing slug.

    Deliberately narrow about "auth_failed": only a real 401 says "your key was
    rejected". 402/403/451 are access/policy problems, 429 is rate limiting,
    timeouts and every 5xx are transient, and every other 4xx (400/404/409/422…)
    is a rejected/malformed exchange — NOT an excuse to tell the user to swap
    their key. (404 on openai_compatible never reaches here — the orchestrator
    already turns it into ``catalog_supported:false``.)
    """
    msg = str(exc)
    if "model_catalog_unsupported" in msg:
        return "model_catalog_unsupported"
    if "model_catalog_invalid_response" in msg:
        return "model_catalog_invalid_response"
    sc = getattr(exc, "status_code", None)
    if sc == 401:
        return "model_catalog_auth_failed"
    if sc in {402, 403, 451}:
        return "model_catalog_access_denied"
    if sc == 429:
        return "model_catalog_rate_limited"
    if sc in {408, 425} or sc is None or (isinstance(sc, int) and 500 <= sc <= 599):
        return "model_catalog_temporarily_unavailable"
    return "model_catalog_invalid_response"


def test_provider_key(config: ProviderConfig) -> dict[str, Any]:
    # Validates that the key is usable for this model. We deliberately do NOT
    # require reply text: thinking/reasoning models (gemini-2.5-*, deepseek-
    # reasoner, …) may spend the whole token budget on reasoning and return an
    # empty body with finishReason=MAX_TOKENS. A 2xx response already proves the
    # key is valid, the model exists, and the account can be billed; an invalid
    # or quota'd key surfaces as an HTTP 4xx and still raises. max_tokens is set
    # high enough that most models also produce a short reply for the logs.
    return chat_completion(
        config,
        [
            {
                "role": "system",
                "content": "You are a health check endpoint. Reply with exactly: ok",
            },
            {"role": "user", "content": "Say ok."},
        ],
        max_tokens=256,
        # No temperature: the Claude 5 / GPT-5 generation rejects it outright
        # ("`temperature` is deprecated for this model" → 400), and the probe has no
        # need for it — we only care that the key/model/billing work. Sending it made
        # setup fail intermittently (measured 2/6 on claude-sonnet-5 via a relay,
        # since only some upstream channels behind a model id reject it), which read
        # to the user as "sometimes I can add this model, sometimes I can't".
        temperature=None,
        timeout=30.0,
        require_reply=False,
    )


# --- async variant (enclave ASGI migration) --------------------------------
# 全部 4 个 wire（openai-compat / anthropic / gemini / openai-responses）都有
# 原生 async 实现——payload 构造 + 响应解析各自与同步 chat_completion 共用单
# 实现（_build_<wire>_payload / _parse_<wire>_body），两条路径只各自保留
# httpx sync/async 的 transport 差异，不再有 anyio 线程桥（曾把这几个
# provider 的并发上限静默锁死在线程池大小）。
# 同步 chat_completion 与异步版各用各的 httpx client，绝不混用（spec §4）。

_shared_async_client: httpx.AsyncClient | None = None
_isolated_async_client: contextvars.ContextVar[httpx.AsyncClient | None] = (
    contextvars.ContextVar("provider_isolated_async_client", default=None)
)


def _build_shared_async_client(**kwargs) -> httpx.AsyncClient:
    # Same no-store jar as the sync client — see _NoStoreCookieJar.
    return httpx.AsyncClient(
        cookies=_NoStoreCookieJar(), limits=_CLIENT_LIMITS, **kwargs)


def _async_http_client() -> httpx.AsyncClient:
    if isolated := _isolated_async_client.get():
        return isolated
    global _shared_async_client
    if _shared_async_client is None or _shared_async_client.is_closed:
        _shared_async_client = _build_shared_async_client()
    return _shared_async_client


async def aclose_async_http_client() -> None:
    global _shared_async_client
    client, _shared_async_client = _shared_async_client, None
    if client is not None and not client.is_closed:
        await client.aclose()


async def _chat_completion_async_impl(
    config: ProviderConfig,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 700,
    # Native async callers (including Runtime V2) use provider defaults unless
    # a structured/extraction lane explicitly asks for determinism. New model
    # generations increasingly reject temperature outright; sending 0.7 here
    # caused one guaranteed hidden 400→retry on every ordinary chat round.
    temperature: float | None = None,
    timeout: float = 60.0,
    response_format: dict[str, Any] | None = None,
    require_reply: bool = True,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
    tool_choice: str | dict[str, Any] | None = None,
    _attempt_trace: list[dict[str, Any]] | None,
    allow_image_output: bool = False,
) -> dict[str, Any]:
    provider, model, base_url = validate_config(
        config.provider, config.model, config.base_url
    )
    request_model, extra_body = _runtime_model(provider, model)
    key = (config.api_key or "").strip()
    if not key:
        raise ProviderError("api_key required")

    # anthropic / gemini / openai-responses 各自的编解码与同步版共享单实现
    # （_build_<wire>_payload / _parse_<wire>_body），这里只保留 async
    # transport——不再经 anyio 线程桥调用同步 chat_completion（该桥曾把这 3
    # 个 provider 的并发上限静默锁死在线程池大小）。

    if (
        provider in {"openai", "openrouter"}
        and allow_image_output
        and _model_has_native_image_output(provider, request_model)
    ):
        payload = _build_openrouter_images_payload(
            model=request_model,
            messages=messages,
        )

        async def post_dedicated_image(
            request_payload: dict[str, Any],
        ) -> httpx.Response:
            try:
                suffix = "/images/generations" if provider == "openai" else "/images"
                return await _async_http_client().post(
                    f"{base_url.rstrip('/')}{suffix}",
                    headers=_headers(
                        ProviderConfig(provider, request_model, key, base_url)
                    ),
                    json=request_payload,
                    # Buffered image generation routinely exceeds a text turn's
                    # 60s default but remains below Runtime V2's stall watchdog.
                    timeout=max(float(timeout), 120.0),
                )
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"provider network error: {type(exc).__name__}"
                ) from exc

        resp, _ = await _traced_async_json_post(
            trace=_attempt_trace,
            provider=provider,
            model=request_model,
            inner_attempt=1,
            request_payload=payload,
            post=post_dedicated_image,
        )
        _raise_for_provider_status(resp)
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError("provider returned non-json response") from exc
        if not isinstance(body, dict):
            raise ProviderError("provider returned non-object response")
        return _parse_openrouter_images_body(
            body,
            provider=provider,
            model=request_model,
        )

    if provider == "anthropic":
        payload, url, headers = _build_anthropic_payload(
            model=model,
            base_url=base_url,
            key=key,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            include_reasoning=include_reasoning,
            tools=tools,
            prompt_cache_key=config.prompt_cache_key,
        )

        async def post_anthropic(request_payload: dict[str, Any]) -> httpx.Response:
            try:
                return await _async_http_client().post(
                    url, headers=headers, json=request_payload, timeout=timeout
                )
            except httpx.HTTPError as e:
                raise ProviderError(
                    f"provider network error: {type(e).__name__}"
                ) from e

        current_payload = payload
        fallback_codes: list[str] = []
        attempts_used = 0
        for attempt in range(1, _compatibility_attempt_limit(payload) + 1):
            attempts_used = attempt
            resp, trace_entry = await _traced_async_json_post(
                trace=_attempt_trace,
                provider="anthropic",
                model=model,
                inner_attempt=attempt,
                request_payload=current_payload,
                post=post_anthropic,
            )
            try:
                _raise_for_provider_status(resp)
                break
            except ProviderError:
                decision = _compatibility_fallback(
                    current_payload,
                    resp,
                    provider="anthropic",
                    include_reasoning=False,
                )
                if decision is None:
                    raise
                if trace_entry is not None:
                    trace_entry["compatibility_fallback"] = decision.code
                fallback_codes.append(decision.code)
                _record_compatibility_fallback(
                    provider="anthropic",
                    model=model,
                    code=decision.code,
                    attempt=attempt,
                )
                current_payload = decision.payload
        else:  # pragma: no cover — finite monotonic fallback set
            _raise_for_provider_status(resp)
        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderError("provider returned non-json response") from e
        if not isinstance(body, dict):
            raise ProviderError("provider returned non-object response")
        result = _parse_anthropic_body(body, model=model, require_reply=require_reply)
        return _with_request_diagnostics(
            result,
            initial_payload=payload,
            successful_payload=current_payload,
            attempts=attempts_used,
            fallback_codes=fallback_codes,
        )

    if provider == "bedrock":
        payload, url, headers = _build_bedrock_payload(
            model=model,
            base_url=base_url,
            key=key,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            include_reasoning=include_reasoning,
            tools=tools,
            prompt_cache_key=config.prompt_cache_key,
        )

        async def post_bedrock(request_payload: dict[str, Any]) -> httpx.Response:
            try:
                return await _async_http_client().post(
                    url, headers=headers, json=request_payload, timeout=timeout
                )
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"provider network error: {type(exc).__name__}"
                ) from exc

        current_payload = payload
        fallback_codes: list[str] = []
        attempts_used = 0
        for attempt in range(1, _compatibility_attempt_limit(payload) + 1):
            attempts_used = attempt
            resp, trace_entry = await _traced_async_json_post(
                trace=_attempt_trace,
                provider="bedrock",
                model=model,
                inner_attempt=attempt,
                request_payload=current_payload,
                post=post_bedrock,
            )
            try:
                _raise_for_provider_status(resp)
                break
            except ProviderError:
                decision = _compatibility_fallback(
                    current_payload,
                    resp,
                    provider="bedrock",
                    include_reasoning=False,
                )
                if decision is None:
                    raise
                if trace_entry is not None:
                    trace_entry["compatibility_fallback"] = decision.code
                fallback_codes.append(decision.code)
                _record_compatibility_fallback(
                    provider="bedrock",
                    model=model,
                    code=decision.code,
                    attempt=attempt,
                )
                current_payload = decision.payload
        else:  # pragma: no cover - every fallback removes finite optional state
            _raise_for_provider_status(resp)

        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError("provider returned non-json response") from exc
        if not isinstance(body, dict):
            raise ProviderError("provider returned non-object response")
        result = _parse_bedrock_body(body, model=model, require_reply=require_reply)
        return _with_request_diagnostics(
            result,
            initial_payload=payload,
            successful_payload=current_payload,
            attempts=attempts_used,
            fallback_codes=fallback_codes,
        )

    if provider == "gemini":
        payload, url, headers = _build_gemini_payload(
            model=model,
            base_url=base_url,
            key=key,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            include_reasoning=include_reasoning,
            tools=tools,
            allow_image_output=allow_image_output,
        )
        async def post_gemini(request_payload: dict[str, Any]) -> httpx.Response:
            try:
                return await _async_http_client().post(
                    url, headers=headers, json=request_payload, timeout=timeout
                )
            except httpx.HTTPError as e:
                raise ProviderError(
                    f"provider network error: {type(e).__name__}"
                ) from e

        resp, _ = await _traced_async_json_post(
            trace=_attempt_trace,
            provider="gemini",
            model=model,
            inner_attempt=1,
            request_payload=payload,
            post=post_gemini,
        )
        _raise_for_provider_status(resp)
        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderError("provider returned non-json response") from e
        if not isinstance(body, dict):
            raise ProviderError("provider returned non-object response")
        return _parse_gemini_body(body, model=model, require_reply=require_reply)

    if provider == "openai" and _openai_uses_responses_for_reasoning(request_model):
        payload, url, headers = _build_openai_responses_payload(
            model=request_model,
            base_url=base_url,
            key=key,
            messages=messages,
            max_tokens=max_tokens,
            response_format=response_format,
            include_reasoning=include_reasoning,
            tools=tools,
            prompt_cache_key=config.prompt_cache_key,
            allow_image_output=allow_image_output,
        )

        async def post_responses(request_payload: dict[str, Any]) -> httpx.Response:
            try:
                return await _async_http_client().post(
                    url, headers=headers, json=request_payload, timeout=timeout
                )
            except httpx.HTTPError as e:
                raise ProviderError(
                    f"provider network error: {type(e).__name__}"
                ) from e

        current_payload = payload
        fallback_codes = []
        attempts_used = 0
        for attempt in range(1, _compatibility_attempt_limit(payload) + 1):
            attempts_used = attempt
            resp, trace_entry = await _traced_async_json_post(
                trace=_attempt_trace,
                provider="openai",
                model=request_model,
                inner_attempt=attempt,
                request_payload=current_payload,
                post=post_responses,
            )
            try:
                _raise_for_provider_status(resp)
                break
            except ProviderError:
                decision = _cache_fallback_payload(
                    current_payload, resp, require_error_hint=False
                )
                if decision is None:
                    raise
                if trace_entry is not None:
                    trace_entry["compatibility_fallback"] = decision.code
                fallback_codes.append(decision.code)
                _record_compatibility_fallback(
                    provider="openai",
                    model=request_model,
                    code=decision.code,
                    attempt=attempt,
                )
                current_payload = decision.payload
        else:  # pragma: no cover — every fallback removes one finite field
            _raise_for_provider_status(resp)
        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderError("provider returned non-json response") from e
        if not isinstance(body, dict):
            raise ProviderError("provider returned non-object response")
        result = _parse_openai_responses_body(
            body, model=request_model, require_reply=require_reply
        )
        return _with_request_diagnostics(
            result,
            initial_payload=payload,
            successful_payload=current_payload,
            attempts=attempts_used,
            fallback_codes=fallback_codes,
        )

    # openai-compat 编解码与同步版共享单实现（_build/_reasoning_fallback/
    # _parse 三个纯函数），这里只保留 async transport。
    # NOTE: model 传 request_model（runtime 映射后真正上 wire 的模型，如
    # deepseek-chat -> deepseek-v4-flash）——与同步 chat_completion 调
    # _chat_completion_openai_compatible 时的取值一致，返回 dict 的 "model"
    # 因此是映射值而非 config.model。
    payload = _build_openai_compat_payload(
        provider=provider,
        model=request_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        extra_body=extra_body,
        include_reasoning=include_reasoning,
        tools=tools,
        prompt_cache_key=config.prompt_cache_key,
        tool_choice=tool_choice,
        allow_image_output=allow_image_output,
    )

    async def post_with_payload(request_payload: dict[str, Any]) -> httpx.Response:
        try:
            return await _async_http_client().post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=_headers(
                    ProviderConfig(provider, request_model, key, base_url)
                ),
                json=request_payload,
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"provider network error: {type(e).__name__}") from e

    current_payload = payload
    fallback_codes = []
    attempts_used = 0
    for attempt in range(1, _compatibility_attempt_limit(payload) + 1):
        attempts_used = attempt
        resp, trace_entry = await _traced_async_json_post(
            trace=_attempt_trace,
            provider=provider,
            model=request_model,
            inner_attempt=attempt,
            request_payload=current_payload,
            post=post_with_payload,
        )
        try:
            _raise_for_provider_status(resp)
            break
        except ProviderError:
            decision = _compatibility_fallback(
                current_payload,
                resp,
                provider=provider,
                include_reasoning=include_reasoning,
            )
            if decision is None:
                raise
            if trace_entry is not None:
                trace_entry["compatibility_fallback"] = decision.code
            fallback_codes.append(decision.code)
            _record_compatibility_fallback(
                provider=provider,
                model=request_model,
                code=decision.code,
                attempt=attempt,
            )
            current_payload = decision.payload
    else:  # pragma: no cover — finite monotonic fallback set
        _raise_for_provider_status(resp)

    result = _parse_openai_compat_body(
        resp, provider=provider, model=request_model, require_reply=require_reply
    )
    return _with_request_diagnostics(
        result,
        initial_payload=payload,
        successful_payload=current_payload,
        attempts=attempts_used,
        fallback_codes=fallback_codes,
    )


async def chat_completion_async(
    config: ProviderConfig,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 700,
    temperature: float | None = None,
    timeout: float = 60.0,
    response_format: dict[str, Any] | None = None,
    require_reply: bool = True,
    include_reasoning: bool = False,
    tools: "list[ToolSpec] | None" = None,
    tool_choice: str | dict[str, Any] | None = None,
    allow_image_output: bool = False,
) -> dict[str, Any]:
    """Native async completion, optionally retaining every HTTP attempt.

    Runtime V2 opts into the private trace field for its encrypted trajectory.
    When enabled, failed calls carry the same envelope on
    ``exc.feedling_provider_attempt_trace`` so terminal failures do not lose the
    evidence that led to them. Other callers preserve the ordinary result shape
    and do not retain a second reference to potentially large request payloads.
    """
    attempt_trace: list[dict[str, Any]] | None = (
        [] if config.capture_attempt_trace else None
    )
    try:
        result = await _chat_completion_async_impl(
            config,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            response_format=response_format,
            require_reply=require_reply,
            include_reasoning=include_reasoning,
            tools=tools,
            tool_choice=tool_choice,
            allow_image_output=allow_image_output,
            _attempt_trace=attempt_trace,
        )
    except Exception as exc:  # noqa: BLE001 -- annotate and preserve original
        if attempt_trace is not None:
            _mark_attempt_postprocess_error(attempt_trace, exc)
            _attach_provider_attempt_trace(exc, attempt_trace)
        raise
    if attempt_trace is None:
        return result
    return _with_provider_attempt_trace(result, attempt_trace)


async def generate_image_async(
    config: ProviderConfig,
    prompt: str,
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Generate one image through a provider-neutral saved route.

    Mainline OpenAI routes use the Responses hosted image tool; dedicated
    OpenAI/OpenRouter/Gemini-compatible image models use their native image
    wire. Text-only routes fail before a second paid provider request.
    """
    provider, model, _ = validate_config(
        config.provider,
        config.model,
        config.base_url,
    )
    normalized_prompt = str(prompt or "").strip()
    if not normalized_prompt:
        raise ProviderError("image prompt required")
    if len(normalized_prompt) > 16_000:
        raise ProviderError("image prompt too long")

    supported = _model_has_native_image_output(provider, model) or (
        provider == "openai" and _openai_uses_responses_for_reasoning(model)
    )
    if not supported:
        exc = ProviderError("image_generation_model_unsupported")
        exc.feedling_error_class = "provider_incompatible"
        raise exc

    result = await chat_completion_async(
        config,
        [{"role": "user", "content": normalized_prompt}],
        max_tokens=512,
        temperature=None,
        timeout=max(float(timeout), 120.0),
        require_reply=True,
        include_reasoning=False,
        tools=None,
        allow_image_output=True,
    )
    if not isinstance(result.get("media"), list) or not result["media"]:
        exc = ProviderError("image_generation_invalid_output")
        exc.feedling_error_class = "provider_incompatible"
        raise exc
    return result


def generate_image(
    config: ProviderConfig,
    prompt: str,
) -> dict[str, Any]:
    """Run a synchronous image request without borrowing another loop's pool."""

    async def run_isolated() -> dict[str, Any]:
        try:
            async with _build_shared_async_client() as client:
                token = _isolated_async_client.set(client)
                try:
                    return await generate_image_async(config, prompt)
                finally:
                    _isolated_async_client.reset(token)
        except Exception as exc:
            log.warning(
                "[image-generation] isolated request failed "
                "error_type=%s class=%s provider=%s model=%s",
                type(exc).__name__,
                classify_provider_error(exc),
                normalize_provider(config.provider),
                str(config.model or "")[:96],
            )
            raise

    return asyncio.run(run_isolated())


# --- Hosted runtime V2: natively async reliable wrapper ---------------------
# `reliable_chat_completion` above is the synchronous retry surface used by
# synchronous callers. V2 previously bridged it from its asyncio loop, silently
# capping provider concurrency at the default thread-pool size. This is a
# straight async mirror of the SAME retry loop (identical classification/
# backoff/terminal-labelling semantics via the shared `classify_provider_error`/
# `_retry_after_seconds` helpers) — it just `await`s `chat_completion_async`
# and sleeps via `asyncio.sleep` instead of blocking the thread, so the turn
# coroutine yields the event loop during backoff instead of parking a thread.
async def reliable_chat_completion_async(
    *args: Any,
    max_attempts: int = 3,
    base_delay_sec: float = 1.0,
    max_delay_sec: float = 30.0,
    progress_cb: Any = None,
    **kwargs: Any,
) -> Any:
    """`chat_completion_async` + bounded retry on *transient* failures only.

    Same semantics as `reliable_chat_completion`: exponential backoff (base·3^n)
    + jitter, capped; honours 429 Retry-After when present. NEVER retries
    `provider_config` failures. On final failure the raised exception carries
    `.feedling_error_class` ("transient_exhausted" | "provider_config") so the
    caller can label the job/turn.
    """
    attempts = max(1, int(max_attempts))
    last_exc: BaseException | None = None
    config = args[0] if args and isinstance(args[0], ProviderConfig) else None
    if config is None and isinstance(kwargs.get("config"), ProviderConfig):
        config = kwargs["config"]
    if config is not None:
        effective_provider = normalize_provider(config.provider)
        effective_model = _runtime_model(
            effective_provider, config.model
        )[0]
    else:
        # Generic monkeypatched/test callers may omit ProviderConfig entirely.
        effective_provider = ""
        effective_model = ""
    provider_attempt_trace: list[dict[str, Any]] | None = (
        [] if config is not None and config.capture_attempt_trace else None
    )

    def _progress(stage: str, attempt: int) -> None:
        # Optional hosted-runtime watchdog telemetry.  Keep it out of kwargs so
        # provider adapters never see it, and never let observation change retry
        # semantics.  Other callers simply omit it.
        if progress_cb is None:
            return
        try:
            progress_cb(stage, attempt)
        except Exception:  # noqa: BLE001
            pass

    for attempt in range(1, attempts + 1):
        started_ns = time.monotonic_ns()
        _progress("attempt_start", attempt)
        try:
            result = await chat_completion_async(*args, **kwargs)
            _progress("attempt_complete", attempt)
            if provider_attempt_trace is not None:
                inner_ordinals = _extend_attempt_trace(
                    provider_attempt_trace,
                    _attempts_from_result(result),
                    outer_attempt=attempt,
                )
                if inner_ordinals:
                    final_inner = provider_attempt_trace[inner_ordinals[-1] - 1]
                    marker_provider = str(
                        final_inner.get("provider") or effective_provider
                    )
                    marker_model = str(final_inner.get("model") or effective_model)
                    marker_status = final_inner.get("status")
                else:
                    marker_provider = effective_provider
                    marker_model = effective_model
                    marker_status = None
                provider_attempt_trace.append(
                    {
                        "ordinal": len(provider_attempt_trace) + 1,
                        "kind": "outer_attempt",
                        "outer_attempt": attempt,
                        "provider": marker_provider,
                        "model": marker_model,
                        "status": marker_status,
                        "error_class": None,
                        "compatibility_fallback": None,
                        "duration_ms": _attempt_duration_ms(started_ns),
                        "outcome": "success",
                        "wire": {
                            "encoding": "inner_http_attempt_ordinals",
                            "ordinals": inner_ordinals,
                        },
                    }
                )
                result = _with_provider_attempt_trace(result, provider_attempt_trace)
            return _with_reliable_retry_count(result, attempt - 1)
        except Exception as exc:  # noqa: BLE001 — classify, then re-raise or retry
            _progress("attempt_failed", attempt)
            cls = classify_provider_error(exc)
            last_exc = exc
            terminal = cls == "provider_config" or attempt >= attempts
            if provider_attempt_trace is not None:
                inner_ordinals = _extend_attempt_trace(
                    provider_attempt_trace,
                    _attempts_from_exception(exc),
                    outer_attempt=attempt,
                )
                if inner_ordinals:
                    final_inner = provider_attempt_trace[inner_ordinals[-1] - 1]
                    marker_provider = str(
                        final_inner.get("provider") or effective_provider
                    )
                    marker_model = str(final_inner.get("model") or effective_model)
                    marker_status = final_inner.get("status")
                else:
                    marker_provider = effective_provider
                    marker_model = effective_model
                    status = getattr(exc, "status_code", None)
                    marker_status = int(status) if isinstance(status, int) else None
                provider_attempt_trace.append(
                    {
                        "ordinal": len(provider_attempt_trace) + 1,
                        "kind": "outer_attempt",
                        "outer_attempt": attempt,
                        "provider": marker_provider,
                        "model": marker_model,
                        "status": marker_status,
                        "error_class": cls,
                        "compatibility_fallback": None,
                        "duration_ms": _attempt_duration_ms(started_ns),
                        "outcome": "terminal_error" if terminal else "retry",
                        "wire": {
                            "encoding": "inner_http_attempt_ordinals",
                            "ordinals": inner_ordinals,
                        },
                    }
                )
            if terminal:
                exc.feedling_error_class = (
                    "provider_config"
                    if cls == "provider_config"
                    else "transient_exhausted"
                )
                if provider_attempt_trace is not None:
                    _attach_provider_attempt_trace(exc, provider_attempt_trace)
                raise
            delay = min(base_delay_sec * (3 ** (attempt - 1)), max_delay_sec)
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                delay = min(max(delay, retry_after), max_delay_sec)
            await asyncio.sleep(delay + random.uniform(0.0, 0.5 * delay))
    assert last_exc is not None  # loop always sets it before this point
    raise last_exc
