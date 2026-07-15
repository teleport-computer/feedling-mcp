from __future__ import annotations

import asyncio
import json
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from provider_types import ToolExchange

if TYPE_CHECKING:
    from provider_types import ToolSpec


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


def reliable_chat_completion(
    *args: Any,
    max_attempts: int = 3,
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
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return chat_completion(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — classify, then re-raise or retry
            cls = classify_provider_error(exc)
            last_exc = exc
            if cls == "provider_config" or attempt >= attempts:
                exc.feedling_error_class = (
                    "provider_config" if cls == "provider_config" else "transient_exhausted"
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
_shared_client: httpx.Client | None = None
_shared_client_lock = threading.Lock()


def _http_client() -> httpx.Client:
    global _shared_client
    if _shared_client is not None:
        return _shared_client
    with _shared_client_lock:
        if _shared_client is None:
            _shared_client = httpx.Client(
                limits=httpx.Limits(
                    max_keepalive_connections=20,
                    max_connections=100,
                    keepalive_expiry=90.0,
                ),
            )
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


_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
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


def public_config(config: dict) -> dict:
    provider = normalize_provider(str(config.get("provider") or ""))
    key_hint = str(config.get("api_key_hint") or "")
    return {
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


def validate_config(provider: str, model: str, base_url: str = "") -> tuple[str, str, str]:
    provider = normalize_provider(provider)
    model = (model or "").strip()
    base_url = (base_url or "").strip().rstrip("/")

    if provider not in {
        "openai",
        "openrouter",
        "anthropic",
        "gemini",
        "deepseek",
        "openai_compatible",
    }:
        raise ProviderError(
            "provider must be openai, openrouter, anthropic, gemini, "
            "deepseek, or openai_compatible"
        )
    if not model or len(model) > 160:
        raise ProviderError("model required")
    if provider == "openai_compatible" and not base_url:
        raise ProviderError("base_url required for openai_compatible")
    if base_url and not (base_url.startswith("https://") or base_url.startswith("http://127.0.0.1")):
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


def _response_error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or err.get("code") or err.get("status") or ""
                return str(detail)[:240]
            if isinstance(err, str):
                return err[:240]
            message = body.get("message")
            if isinstance(message, str):
                return message[:240]
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
        parts.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image["mime_type"],
                "data": image["data"],
            },
        })
    return parts


def _content_to_gemini_parts(content: Any) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    text = _content_text(content)
    if text:
        parts.append({"text": text})
    for image in _image_parts(content):
        parts.append({
            "inline_data": {
                "mime_type": image["mime_type"],
                "data": image["data"],
            },
        })
    return parts


def _json_only_instruction(response_format: dict[str, Any] | None) -> str:
    if not response_format:
        return ""
    if response_format.get("type") in {"json_object", "json_schema"}:
        return "Return only a valid JSON object. Do not wrap it in Markdown."
    return ""


def _append_text_message(
    messages: list[dict[str, str]],
    *,
    role: str,
    content: str,
) -> None:
    content = content.strip()
    if not content:
        return
    if messages and messages[-1].get("role") == role:
        messages[-1]["content"] = f"{messages[-1]['content']}\n\n{content}"
    else:
        messages.append({"role": role, "content": content})


def _split_system_messages(
    messages: list[dict[str, Any]],
    *,
    assistant_role: str,
) -> tuple[str, list[dict[str, str]]]:
    system_parts: list[str] = []
    provider_messages: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        content = _content_text(message.get("content"))
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        mapped_role = (
            assistant_role
            if role in {"assistant", "openclaw", "agent", "model"}
            else "user"
        )
        _append_text_message(provider_messages, role=mapped_role, content=content)
    if not provider_messages:
        provider_messages.append({"role": "user", "content": "Say ok."})
    return "\n\n".join(system_parts).strip(), provider_messages


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
                _encode_tool_results_gemini(message.results, id_to_name))
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
        mapped_role = "model" if role in {"assistant", "openclaw", "agent", "model"} else "user"
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
            "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw)
        # Provider-neutral miss semantics: every effective input token not read
        # from cache. Anthropic reports newly-created cache tokens separately
        # from its uncached suffix, so both belong in the miss count.
        cache_miss = _sum_ints(uncached, cache_write) if cache_reported else None
    elif provider == "gemini":
        pt = _int(raw.get("promptTokenCount"))
        ct = _int(raw.get("candidatesTokenCount"))
        tt = _int(raw.get("totalTokenCount"))
        cache_read = _int(raw.get("cachedContentTokenCount"))
        cache_write = None  # implicit Gemini caching has no per-request write count
        cache_miss = (
            max(0, pt - (cache_read or 0))
            if pt is not None and cache_read is not None else None)
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
        return (parsed if isinstance(parsed, dict) else {}), ("" if isinstance(parsed, dict) else s), isinstance(parsed, dict)
    except (ValueError, TypeError):
        return {}, s, False


_CACHE_REQUEST_FIELDS = frozenset({
    "prompt_cache_key",
    "prompt_cache_options",
    "cache_control",
    "session_id",
})


def _cache_key(value: Any) -> str:
    """Return a provider-safe opaque cache/session key (max 256 chars)."""
    return str(value or "").strip()[:256]


def _cache_fallback_payload(
    payload: dict[str, Any], resp, *, require_error_hint: bool = False,
) -> dict[str, Any] | None:
    """Remove only cache/stickiness fields after an unsupported 400/422.

    BYOK relays vary widely.  A cache hint must never turn a previously valid
    request into a terminal failure, but the downgrade must preserve tools,
    messages, reasoning, and every other semantic field.  When
    ``require_error_hint`` is true, only downgrade if the provider response
    actually mentions a cache/session field; callers can make one final generic
    downgrade after their existing reasoning/temperature fallbacks.
    """
    if getattr(resp, "status_code", None) not in {400, 422}:
        return None
    present = _CACHE_REQUEST_FIELDS.intersection(payload)
    if not present:
        return None
    if require_error_hint:
        try:
            detail = str(resp.text or "").lower()
        except Exception:  # noqa: BLE001 — absent error text is not a hint
            detail = ""
        # Do not treat a generic "unknown field" as a cache rejection: it may
        # name reasoning or temperature, and stripping cache affinity first
        # would silently turn a cache-capable route cold.  The final generic
        # cache-off fallback below still covers relays that return no useful
        # field name at all after the semantic fallbacks have been attempted.
        hints = ("cache", "session_id", "prompt_cache")
        if not any(hint in detail for hint in hints):
            return None
    fallback = dict(payload)
    for field in present:
        fallback.pop(field, None)
    return fallback


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
            "tool exchange results must match assistant call ids in original order")


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
            items.append({
                "role": "assistant",
                "content": [{"type": "input_text", "text": exchange.assistant_text}],
            })
        items.extend({
            "type": "function_call",
            "call_id": call.id,
            "name": call.name,
            "arguments": _tool_args_json(call),
        } for call in exchange.calls)
        return items
    if wire == "anthropic":
        blocks: list[dict[str, Any]] = []
        if exchange.assistant_text:
            blocks.append({"type": "text", "text": exchange.assistant_text})
        blocks.extend({
            "type": "tool_use",
            "id": call.id,
            "name": call.name,
            "input": call.args,
        } for call in exchange.calls)
        return blocks
    if wire == "gemini":
        parts: list[dict[str, Any]] = []
        if exchange.assistant_text:
            parts.append({"text": exchange.assistant_text})
        parts.extend({
            "functionCall": {"name": call.name, "args": call.args}
        } for call in exchange.calls)
        return {"role": "model", "parts": parts}
    raise ProviderError(f"unknown tool exchange wire: {wire}")


def _assistant_payload_for_wire(exchange: ToolExchange, wire: str):
    _validate_tool_exchange(exchange)
    native = exchange.assistant_turn
    if native is None:
        return _synthesized_assistant_payload(exchange, wire)
    if native.wire != wire:
        raise ProviderError(
            f"tool exchange wire mismatch: expected {wire}, got {native.wire}")
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


def _encode_tools_openai_chat(tools) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]


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
        out.append({"id": str(c.get("id") or ""), "name": str(fn.get("name") or ""),
                    "args": args, "args_raw": args_raw, "args_ok": ok})
    return out


def _encode_tools_openai_responses(tools) -> list[dict]:
    return [{"type": "function", "name": t.name, "description": t.description,
             "parameters": t.parameters} for t in tools]


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
        out.append({"id": str(item.get("call_id") or ""), "name": str(item.get("name") or ""),
                    "args": args, "args_raw": args_raw, "args_ok": ok})
    return out


def _encode_tool_results_openai_chat(results) -> list[dict]:
    return [{"role": "tool", "tool_call_id": r.call_id, "content": r.content} for r in results]


def _encode_tool_results_openai_responses(results) -> list[dict]:
    return [{"type": "function_call_output", "call_id": r.call_id, "output": r.content} for r in results]


def _encode_tools_anthropic(tools) -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools]


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
        out.append({"id": str(block.get("id") or ""), "name": str(block.get("name") or ""),
                    "args": args, "args_raw": args_raw, "args_ok": ok})
    return out


def _encode_tool_results_anthropic(results) -> list[dict]:
    # Anthropic carries tool results as tool_result content blocks in ONE user turn.
    return [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content} for r in results]}]


def _encode_tools_gemini(tools) -> list[dict]:
    return [{"functionDeclarations": [
        {"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]}]


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
        out.append({"id": f"call_{idx}_{name}", "name": name,
                    "args": args, "args_raw": args_raw, "args_ok": ok})
        idx += 1
    return out


def _encode_tool_results_gemini(results, id_to_name: dict) -> list[dict]:
    # Gemini keys functionResponse on the tool NAME (no id); resolve each result's
    # synthetic call_id back to its name via the map the decoder's ids imply.
    parts = []
    for r in results:
        name = id_to_name.get(r.call_id, r.call_id)
        parts.append({"functionResponse": {"name": name, "response": {"content": r.content}}})
    return [{"role": "user", "parts": parts}]


def _extract_reply(body: dict[str, Any], *, required: bool = True) -> str:
    choices = body.get("choices")
    has_shape = isinstance(choices, list) and len(choices) > 0 and isinstance(choices[0], dict)
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
    if not (isinstance(candidates, list) and candidates and isinstance(candidates[0], dict)):
        return ""
    return str(candidates[0].get("finishReason") or "").strip()


def _anthropic_supports_thinking(model: str) -> bool:
    lower = (model or "").lower()
    return "claude-3-7" in lower or "claude-sonnet-4" in lower or "claude-opus-4" in lower


def _openai_uses_responses_for_reasoning(model: str) -> bool:
    lower = (model or "").lower()
    return lower.startswith("gpt-5") or lower.startswith("o1") or lower.startswith("o3") or lower.startswith("o4")


def _content_to_openai_responses_parts(content: Any) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    text = _content_text(content)
    if text:
        parts.append({"type": "input_text", "text": text})
    for image in _image_parts(content):
        parts.append({
            "type": "input_image",
            "image_url": f"data:{image['mime_type']};base64,{image['data']}",
        })
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
        mapped_role = "assistant" if role in {"assistant", "openclaw", "agent", "model"} else "user"
        parts = _content_to_openai_responses_parts(content)
        if not parts:
            continue
        input_items.append({"role": mapped_role, "content": parts})
    if not input_items:
        input_items.append({"role": "user", "content": [{"type": "input_text", "text": "Say ok."}]})
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
    if tools:
        payload["tools"] = _encode_tools_openai_responses(tools)
    if cache_key := _cache_key(prompt_cache_key):
        payload["prompt_cache_key"] = cache_key

    url = f"{base_url.rstrip('/')}/responses"
    headers = _headers(ProviderConfig("openai", model, key, base_url))
    return payload, url, headers


def _parse_openai_responses_body(
    body: dict[str, Any], *, model: str, require_reply: bool,
) -> dict[str, Any]:
    reply, reasoning = _extract_openai_responses_output(body)
    # See _parse_openai_compat_body: a pure tool-call response has no reply text
    # and must not be rejected — require reply only when no tool_calls are present.
    tool_calls = _decode_tool_calls_openai_responses(body)
    if require_reply and not reply and not tool_calls:
        raise ProviderError("provider response had no usable reply text")
    output = body.get("output")
    return {
        "reply": reply,
        "reasoning": reasoning,
        "usage": _normalize_usage("openai", body.get("usage")),
        "raw_id": body.get("id", ""),
        "stop_reason": str(
            (body.get("incomplete_details") if isinstance(body.get("incomplete_details"), dict) else {}).get("reason")
            or body.get("status")
            or "",
        ).strip(),
        "provider": "openai",
        "model": model,
        "tool_calls": tool_calls,
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
        model=model, base_url=base_url, key=key, messages=messages,
        max_tokens=max_tokens, response_format=response_format,
        include_reasoning=include_reasoning, tools=tools,
        prompt_cache_key=prompt_cache_key,
    )

    def post_with_payload(request_payload: dict[str, Any]) -> httpx.Response:
        try:
            return _http_client().post(
                url, headers=headers, json=request_payload, timeout=timeout)
        except httpx.HTTPError as e:
            raise ProviderError(f"provider network error: {type(e).__name__}") from e

    resp = post_with_payload(payload)
    try:
        _raise_for_provider_status(resp)
    except ProviderError:
        fallback_payload = _cache_fallback_payload(payload, resp)
        if fallback_payload is None:
            raise
        resp = post_with_payload(fallback_payload)
        _raise_for_provider_status(resp)

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")
    return _parse_openai_responses_body(body, model=model, require_reply=require_reply)


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
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": _encode_messages_openai_chat(messages),
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
    if tools:
        payload["tools"] = _encode_tools_openai_chat(tools)
    if cache_key := _cache_key(prompt_cache_key):
        if provider == "openai":
            payload["prompt_cache_key"] = cache_key
        elif provider == "openrouter":
            # OpenRouter uses session_id for provider/model stickiness and
            # forwards prompt_cache_key to upstreams which understand it.
            payload["session_id"] = cache_key
            payload["prompt_cache_key"] = cache_key
            if "anthropic" in model.lower() or "claude" in model.lower():
                payload["cache_control"] = {"type": "ephemeral"}
    return payload


def _reasoning_fallback_payload(
    payload: dict[str, Any], resp, *, provider: str, include_reasoning: bool,
) -> dict[str, Any] | None:
    """openrouter 对不支持 reasoning 的模型回 400/422：去掉 reasoning 重试一次。
    不适用时返回 None（调用方原样 raise）。"""
    if (include_reasoning and provider == "openrouter"
            and resp.status_code in {400, 422} and "reasoning" in payload):
        fallback = dict(payload)
        fallback.pop("reasoning", None)
        return fallback
    return None


def _temperature_fallback_payload(payload: dict[str, Any], resp) -> dict[str, Any] | None:
    """Claude 5 / GPT-5 一代弃用 temperature，带上就 400（"`temperature` is deprecated
    for this model."）：去掉 temperature 重试一次。不适用时返回 None（调用方原样 raise）。

    为什么是降级重试、而不是干脆不发 temperature：运行时调用方是**故意**传低温的
    （genesis 蒸馏 / legacy turn 传 0.0~0.1 换确定性，好让结构化抽取的 JSON 稳定解析），
    全局不发会让这些输出变随机。所以保持默认发送，只在 provider 明确因它报错时才摘掉。

    只在报错文本确实提到 temperature 时降级 —— 否则一个坏 key / 坏模型的 400 会被静默
    重试成另一个 payload，把真正的错误盖掉。"""
    if resp.status_code not in {400, 422} or "temperature" not in payload:
        return None
    try:
        detail = (resp.text or "")
    except Exception:  # noqa: BLE001 — 拿不到 body 就不猜，原样 raise
        return None
    if "temperature" not in detail.lower():
        return None
    fallback = dict(payload)
    fallback.pop("temperature", None)
    return fallback


def _parse_openai_compat_body(
    resp, *, provider: str, model: str, require_reply: bool,
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
    try:
        assistant_payload = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        assistant_payload = {}
    if not isinstance(assistant_payload, dict):
        assistant_payload = {}
    return {
        "reply": _extract_reply(body, required=require_reply and not tool_calls),
        "reasoning": _extract_openai_compatible_reasoning(body),
        "usage": _normalize_usage(provider, body.get("usage")),
        "raw_id": body.get("id", ""),
        "stop_reason": _extract_openai_compatible_stop_reason(body),
        "provider": provider,
        "model": model,
        "tool_calls": tool_calls,
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
        provider=provider, model=model, messages=messages,
        temperature=temperature, max_tokens=max_tokens,
        response_format=response_format, extra_body=extra_body,
        include_reasoning=include_reasoning, tools=tools,
        prompt_cache_key=prompt_cache_key)

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
    # A relay can reject more than one optional field in sequence (for example
    # cache affinity first, then temperature).  Walk bounded, monotonic
    # fallbacks; each step removes fields only and always preserves tools.
    for _ in range(4):
        resp = post_with_payload(current_payload)
        try:
            _raise_for_provider_status(resp)
            break
        except ProviderError:
            fallback_payload = (
                _cache_fallback_payload(
                    current_payload, resp, require_error_hint=True)
                or _reasoning_fallback_payload(
                    current_payload, resp,
                    provider=provider, include_reasoning=include_reasoning)
                or _temperature_fallback_payload(current_payload, resp)
                or _cache_fallback_payload(current_payload, resp)
            )
            if fallback_payload is None:
                raise
            current_payload = fallback_payload
    else:  # pragma: no cover — every fallback removes at least one finite field
        _raise_for_provider_status(resp)

    return _parse_openai_compat_body(
        resp, provider=provider, model=model, require_reply=require_reply)


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
    if include_reasoning and _anthropic_supports_thinking(model) and capped_max_tokens >= 1536:
        payload["thinking"] = {"type": "enabled", "budget_tokens": min(1024, capped_max_tokens - 512)}
    elif temperature is not None:
        payload["temperature"] = temperature
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = _encode_tools_anthropic(tools)
    if _cache_key(prompt_cache_key):
        # Anthropic automatic caching advances the breakpoint with the growing
        # conversation.  The key itself is intentionally not sent on this wire.
        payload["cache_control"] = {"type": "ephemeral"}

    url = f"{base_url.rstrip('/')}/messages"
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    return payload, url, headers


def _parse_anthropic_body(
    body: dict[str, Any], *, model: str, require_reply: bool,
) -> dict[str, Any]:
    # See _parse_openai_compat_body: a pure tool-call response has no reply text
    # and must not be rejected — require reply only when no tool_calls are present.
    tool_calls = _decode_tool_calls_anthropic(body)
    content = body.get("content")
    return {
        "reply": _extract_anthropic_reply(body, required=require_reply and not tool_calls),
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
        model=model, base_url=base_url, key=key, messages=messages,
        max_tokens=max_tokens, temperature=temperature,
        response_format=response_format, include_reasoning=include_reasoning,
        tools=tools, prompt_cache_key=prompt_cache_key,
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
    for _ in range(3):
        resp = post_with_payload(current_payload)
        try:
            _raise_for_provider_status(resp)
            break
        except ProviderError:
            # Same temperature downgrade as the openai-compat wire, plus a
            # cache-off retry for older Anthropic relays.  Both preserve tools.
            fallback_payload = (
                _cache_fallback_payload(
                    current_payload, resp, require_error_hint=True)
                or _temperature_fallback_payload(current_payload, resp)
                or _cache_fallback_payload(current_payload, resp)
            )
            if fallback_payload is None:
                raise
            current_payload = fallback_payload
    else:  # pragma: no cover — finite monotonic fallback set
        _raise_for_provider_status(resp)

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")

    return _parse_anthropic_body(body, model=model, require_reply=require_reply)


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
) -> tuple[dict[str, Any], str, dict[str, str]]:
    system, contents = _split_system_messages_gemini(messages)
    generation_config: dict[str, Any] = {
        "maxOutputTokens": max(1, min(int(max_tokens), 8192)),
    }
    if temperature is not None:
        generation_config["temperature"] = temperature
    if response_format and response_format.get("type") in {"json_object", "json_schema"}:
        generation_config["responseMimeType"] = "application/json"
    if include_reasoning and "2.5" in model:
        generation_config["thinkingConfig"] = {
            "thinkingBudget": min(1024, max(128, int(max_tokens) // 2)),
            "includeThoughts": True,
        }

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
    body: dict[str, Any], *, model: str, require_reply: bool,
) -> dict[str, Any]:
    # See _parse_openai_compat_body: a pure tool-call response has no reply text
    # and must not be rejected — require reply only when no tool_calls are present.
    tool_calls = _decode_tool_calls_gemini(body)
    try:
        assistant_payload = body["candidates"][0]["content"]
    except (KeyError, IndexError, TypeError):
        assistant_payload = {}
    if not isinstance(assistant_payload, dict):
        assistant_payload = {}
    return {
        "reply": _extract_gemini_reply(body, required=require_reply and not tool_calls),
        "reasoning": _extract_gemini_reasoning(body),
        "usage": _normalize_usage("gemini", body.get("usageMetadata")),
        "raw_id": body.get("responseId", ""),
        "stop_reason": _extract_gemini_stop_reason(body),
        "provider": "gemini",
        "model": model,
        "tool_calls": tool_calls,
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
        model=model, base_url=base_url, key=key, messages=messages,
        max_tokens=max_tokens, temperature=temperature,
        response_format=response_format, include_reasoning=include_reasoning,
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


def probe_responses_support(config: ProviderConfig) -> bool:
    """Does this relay implement the OpenAI Responses API (POST /v1/responses)?

    codex speaks the Responses wire; the in-CVM LiteLLM gateway either passes that
    straight through to a relay that implements /v1/responses (preserving codex's
    tool loop) or, for a chat-only relay, forces the responses→chat-completions
    bridge (which mangles the tool loop). We pick per relay by probing once at
    setup. Returns True ONLY on a clear 2xx; a 4xx/5xx ("not implemented") or any
    network error → False, i.e. fall back to the bridge — the safe default that
    keeps chat-only relays working. Never raises."""
    base_url = (config.base_url or default_base_url(config.provider)).rstrip("/")
    if not base_url:
        return False
    runtime_model, _ = _runtime_model(config.provider, config.model)
    try:
        resp = _http_client().post(
            f"{base_url}/responses",
            headers=_headers(config),
            json={"model": runtime_model, "input": "ping", "max_output_tokens": 16},
            timeout=20.0,
        )
    except Exception:
        return False
    if not (200 <= resp.status_code < 300):
        return False
    # A 2xx alone isn't proof: some relays answer 200 with an {"error": ...} body
    # for an endpoint they don't really implement. Mirror the rest of this client
    # (which treats error-shaped/malformed 2xx as failures) — require a JSON object
    # with no top-level "error". A genuine Responses success carries object="response".
    try:
        body = resp.json()
    except Exception:
        return False
    return isinstance(body, dict) and not body.get("error")


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


def _async_http_client() -> httpx.AsyncClient:
    global _shared_async_client
    if _shared_async_client is None or _shared_async_client.is_closed:
        _shared_async_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=90.0,
            ),
        )
    return _shared_async_client


async def aclose_async_http_client() -> None:
    global _shared_async_client
    client, _shared_async_client = _shared_async_client, None
    if client is not None and not client.is_closed:
        await client.aclose()


async def chat_completion_async(
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

    # anthropic / gemini / openai-responses 各自的编解码与同步版共享单实现
    # （_build_<wire>_payload / _parse_<wire>_body），这里只保留 async
    # transport——不再经 anyio 线程桥调用同步 chat_completion（该桥曾把这 3
    # 个 provider 的并发上限静默锁死在线程池大小）。

    if provider == "anthropic":
        payload, url, headers = _build_anthropic_payload(
            model=model, base_url=base_url, key=key, messages=messages,
            max_tokens=max_tokens, temperature=temperature,
            response_format=response_format, include_reasoning=include_reasoning,
            tools=tools, prompt_cache_key=config.prompt_cache_key,
        )

        async def post_anthropic(request_payload: dict[str, Any]) -> httpx.Response:
            try:
                return await _async_http_client().post(
                    url, headers=headers, json=request_payload, timeout=timeout)
            except httpx.HTTPError as e:
                raise ProviderError(f"provider network error: {type(e).__name__}") from e

        current_payload = payload
        for _ in range(3):
            resp = await post_anthropic(current_payload)
            try:
                _raise_for_provider_status(resp)
                break
            except ProviderError:
                fallback_payload = (
                    _cache_fallback_payload(
                        current_payload, resp, require_error_hint=True)
                    or _temperature_fallback_payload(current_payload, resp)
                    or _cache_fallback_payload(current_payload, resp)
                )
                if fallback_payload is None:
                    raise
                current_payload = fallback_payload
        else:  # pragma: no cover — finite monotonic fallback set
            _raise_for_provider_status(resp)
        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderError("provider returned non-json response") from e
        if not isinstance(body, dict):
            raise ProviderError("provider returned non-object response")
        return _parse_anthropic_body(body, model=model, require_reply=require_reply)

    if provider == "gemini":
        payload, url, headers = _build_gemini_payload(
            model=model, base_url=base_url, key=key, messages=messages,
            max_tokens=max_tokens, temperature=temperature,
            response_format=response_format, include_reasoning=include_reasoning,
            tools=tools,
        )
        try:
            resp = await _async_http_client().post(
                url, headers=headers, json=payload, timeout=timeout)
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

    if provider == "openai" and _openai_uses_responses_for_reasoning(request_model):
        payload, url, headers = _build_openai_responses_payload(
            model=request_model, base_url=base_url, key=key, messages=messages,
            max_tokens=max_tokens, response_format=response_format,
            include_reasoning=include_reasoning, tools=tools,
            prompt_cache_key=config.prompt_cache_key,
        )

        async def post_responses(request_payload: dict[str, Any]) -> httpx.Response:
            try:
                return await _async_http_client().post(
                    url, headers=headers, json=request_payload, timeout=timeout)
            except httpx.HTTPError as e:
                raise ProviderError(f"provider network error: {type(e).__name__}") from e

        resp = await post_responses(payload)
        try:
            _raise_for_provider_status(resp)
        except ProviderError:
            fallback_payload = _cache_fallback_payload(payload, resp)
            if fallback_payload is None:
                raise
            resp = await post_responses(fallback_payload)
            _raise_for_provider_status(resp)
        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderError("provider returned non-json response") from e
        if not isinstance(body, dict):
            raise ProviderError("provider returned non-object response")
        return _parse_openai_responses_body(body, model=request_model, require_reply=require_reply)

    # openai-compat 编解码与同步版共享单实现（_build/_reasoning_fallback/
    # _parse 三个纯函数），这里只保留 async transport。
    # NOTE: model 传 request_model（runtime 映射后真正上 wire 的模型，如
    # deepseek-chat -> deepseek-v4-flash）——与同步 chat_completion 调
    # _chat_completion_openai_compatible 时的取值一致，返回 dict 的 "model"
    # 因此是映射值而非 config.model。
    payload = _build_openai_compat_payload(
        provider=provider, model=request_model, messages=messages,
        temperature=temperature, max_tokens=max_tokens,
        response_format=response_format, extra_body=extra_body,
        include_reasoning=include_reasoning, tools=tools,
        prompt_cache_key=config.prompt_cache_key)

    async def post_with_payload(request_payload: dict[str, Any]) -> httpx.Response:
        try:
            return await _async_http_client().post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=_headers(ProviderConfig(provider, request_model, key, base_url)),
                json=request_payload,
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"provider network error: {type(e).__name__}") from e

    current_payload = payload
    for _ in range(4):
        resp = await post_with_payload(current_payload)
        try:
            _raise_for_provider_status(resp)
            break
        except ProviderError:
            fallback_payload = (
                _cache_fallback_payload(
                    current_payload, resp, require_error_hint=True)
                or _reasoning_fallback_payload(
                    current_payload, resp,
                    provider=provider, include_reasoning=include_reasoning)
                or _temperature_fallback_payload(current_payload, resp)
                or _cache_fallback_payload(current_payload, resp)
            )
            if fallback_payload is None:
                raise
            current_payload = fallback_payload
    else:  # pragma: no cover — finite monotonic fallback set
        _raise_for_provider_status(resp)

    return _parse_openai_compat_body(
        resp, provider=provider, model=request_model, require_reply=require_reply)


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
        _progress("attempt_start", attempt)
        try:
            result = await chat_completion_async(*args, **kwargs)
            _progress("attempt_complete", attempt)
            return result
        except Exception as exc:  # noqa: BLE001 — classify, then re-raise or retry
            _progress("attempt_failed", attempt)
            cls = classify_provider_error(exc)
            last_exc = exc
            if cls == "provider_config" or attempt >= attempts:
                exc.feedling_error_class = (
                    "provider_config" if cls == "provider_config" else "transient_exhausted"
                )
                raise
            delay = min(base_delay_sec * (3 ** (attempt - 1)), max_delay_sec)
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                delay = min(max(delay, retry_after), max_delay_sec)
            await asyncio.sleep(delay + random.uniform(0.0, 0.5 * delay))
    assert last_exc is not None  # loop always sets it before this point
    raise last_exc
