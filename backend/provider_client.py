from __future__ import annotations

import asyncio
import random
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


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
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    provider_messages: list[dict[str, Any]] = []
    for message in messages:
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
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    provider_messages: list[dict[str, Any]] = []
    for message in messages:
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


def _openai_responses_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
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
) -> dict[str, Any]:
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

    try:
        resp = _http_client().post(
            f"{base_url.rstrip('/')}/responses",
            headers=_headers(ProviderConfig("openai", model, key, base_url)),
            json=payload,
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise ProviderError(f"provider network error: {type(e).__name__}") from e

    _raise_for_provider_status(resp)

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")
    reply, reasoning = _extract_openai_responses_output(body)
    if require_reply and not reply:
        raise ProviderError("provider response had no usable reply text")
    return {
        "reply": reply,
        "reasoning": reasoning,
        "usage": body.get("usage") if isinstance(body.get("usage"), dict) else {},
        "raw_id": body.get("id", ""),
        "stop_reason": str(
            (body.get("incomplete_details") if isinstance(body.get("incomplete_details"), dict) else {}).get("reason")
            or body.get("status")
            or "",
        ).strip(),
        "provider": "openai",
        "model": model,
    }


# openai-compat 的 wire 编解码是 sync（_chat_completion_openai_compatible）与
# async（chat_completion_async）共用的单实现：payload 构造、openrouter reasoning
# 400/422 降级判定、响应解析都在下面三个纯函数里，两个调用方只各自保留
# httpx sync/async 的 transport 差异。改契约改这里，两边自动同步。

def _build_openai_compat_payload(
    *,
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    response_format: dict[str, Any] | None,
    extra_body: dict[str, Any] | None,
    include_reasoning: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max(1, min(int(max_tokens), 8192)),
    }
    if response_format:
        payload["response_format"] = response_format
    if extra_body:
        payload.update(extra_body)
    if include_reasoning and provider == "openrouter":
        payload.setdefault("reasoning", {"enabled": True, "exclude": False})
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


def _parse_openai_compat_body(
    resp, *, provider: str, model: str, require_reply: bool,
) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")
    return {
        "reply": _extract_reply(body, required=require_reply),
        "reasoning": _extract_openai_compatible_reasoning(body),
        "usage": body.get("usage") if isinstance(body.get("usage"), dict) else {},
        "raw_id": body.get("id", ""),
        "stop_reason": _extract_openai_compatible_stop_reason(body),
        "provider": provider,
        "model": model,
    }


def _chat_completion_openai_compatible(
    *,
    provider: str,
    model: str,
    base_url: str,
    key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    response_format: dict[str, Any] | None,
    extra_body: dict[str, Any] | None = None,
    require_reply: bool = True,
    include_reasoning: bool = False,
) -> dict[str, Any]:
    payload = _build_openai_compat_payload(
        provider=provider, model=model, messages=messages,
        temperature=temperature, max_tokens=max_tokens,
        response_format=response_format, extra_body=extra_body,
        include_reasoning=include_reasoning)

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

    resp = post_with_payload(payload)
    try:
        _raise_for_provider_status(resp)
    except ProviderError:
        fallback_payload = _reasoning_fallback_payload(
            payload, resp, provider=provider, include_reasoning=include_reasoning)
        if fallback_payload is None:
            raise
        resp = post_with_payload(fallback_payload)
        _raise_for_provider_status(resp)

    return _parse_openai_compat_body(
        resp, provider=provider, model=model, require_reply=require_reply)


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
) -> dict[str, Any]:
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
    else:
        payload["temperature"] = temperature
    if system:
        payload["system"] = system

    try:
        resp = _http_client().post(
            f"{base_url.rstrip('/')}/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise ProviderError(f"provider network error: {type(e).__name__}") from e

    _raise_for_provider_status(resp)

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")

    return {
        "reply": _extract_anthropic_reply(body, required=require_reply),
        "reasoning": _extract_anthropic_reasoning(body),
        "usage": body.get("usage") if isinstance(body.get("usage"), dict) else {},
        "raw_id": body.get("id", ""),
        "stop_reason": str(body.get("stop_reason") or "").strip(),
        "provider": "anthropic",
        "model": model,
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
) -> dict[str, Any]:
    system, contents = _split_system_messages_gemini(messages)
    generation_config: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max(1, min(int(max_tokens), 8192)),
    }
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

    try:
        resp = _http_client().post(
            f"{base_url.rstrip('/')}/models/{quote(model, safe='')}:generateContent",
            headers={
                "x-goog-api-key": key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise ProviderError(f"provider network error: {type(e).__name__}") from e

    _raise_for_provider_status(resp)

    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError("provider returned non-json response") from e
    if not isinstance(body, dict):
        raise ProviderError("provider returned non-object response")

    return {
        "reply": _extract_gemini_reply(body, required=require_reply),
        "reasoning": _extract_gemini_reasoning(body),
        "usage": (
            body.get("usageMetadata")
            if isinstance(body.get("usageMetadata"), dict)
            else {}
        ),
        "raw_id": body.get("responseId", ""),
        "stop_reason": _extract_gemini_stop_reason(body),
        "provider": "gemini",
        "model": model,
    }


def chat_completion(
    config: ProviderConfig,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 700,
    temperature: float = 0.7,
    timeout: float = 60.0,
    response_format: dict[str, Any] | None = None,
    require_reply: bool = True,
    include_reasoning: bool = False,
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
        temperature=0.0,
        timeout=30.0,
        require_reply=False,
    )


# --- async variant (enclave ASGI migration) --------------------------------
# 只有 openai-wire（openai 非 responses / openrouter / deepseek /
# openai_compatible）有原生 async 实现——enclave caption 走 openrouter，这是
# 唯一需要"45s 长等待只挂协程"的调用方。anthropic / gemini / openai-responses
# 的编解码保持单实现（同步版），经 anyio 线程桥调用，避免双份 wire codec 漂移。
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
    temperature: float = 0.7,
    timeout: float = 60.0,
    response_format: dict[str, Any] | None = None,
    require_reply: bool = True,
    include_reasoning: bool = False,
) -> dict[str, Any]:
    provider, model, base_url = validate_config(
        config.provider, config.model, config.base_url
    )
    request_model, extra_body = _runtime_model(provider, model)
    key = (config.api_key or "").strip()
    if not key:
        raise ProviderError("api_key required")

    if provider in ("anthropic", "gemini") or (
        provider == "openai" and _openai_uses_responses_for_reasoning(request_model)
    ):
        import anyio.to_thread
        from functools import partial

        return await anyio.to_thread.run_sync(partial(
            chat_completion, config, messages,
            max_tokens=max_tokens, temperature=temperature, timeout=timeout,
            response_format=response_format, require_reply=require_reply,
            include_reasoning=include_reasoning,
        ))

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
        include_reasoning=include_reasoning)

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

    resp = await post_with_payload(payload)
    try:
        _raise_for_provider_status(resp)
    except ProviderError:
        fallback_payload = _reasoning_fallback_payload(
            payload, resp, provider=provider, include_reasoning=include_reasoning)
        if fallback_payload is None:
            raise
        resp = await post_with_payload(fallback_payload)
        _raise_for_provider_status(resp)

    return _parse_openai_compat_body(
        resp, provider=provider, model=request_model, require_reply=require_reply)


# --- Hosted runtime V2: natively async reliable wrapper ---------------------
# `reliable_chat_completion` (above) bridges `chat_completion` off the request
# path via blocking `time.sleep` — fine for the genesis CVM worker, but the V2
# hosted runtime's worker calls it from inside an asyncio event loop via
# `asyncio.to_thread`, which silently caps concurrency at the thread pool size
# (~32 workers) no matter how high the worker pool's own dial is set. This is a
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
    for attempt in range(1, attempts + 1):
        try:
            return await chat_completion_async(*args, **kwargs)
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
            await asyncio.sleep(delay + random.uniform(0.0, 0.5 * delay))
    assert last_exc is not None  # loop always sets it before this point
    raise last_exc
