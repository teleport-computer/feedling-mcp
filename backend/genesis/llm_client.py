"""Genesis LLM client interface.

The caller supplies a runtime ProviderConfig whose api_key has been decrypted
inside the CVM/enclave path. This module never persists that key or LLM reply
text; only request metadata, response hashes, lengths, and usage are recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

import db
import provider_client


@dataclass(frozen=True)
class GenesisLLMResult:
    text: str
    usage: dict
    cached: bool
    output_ref: str
    stop_reason: str = ""
    max_tokens: int = 0


class DistillModelTooSlowError(RuntimeError):
    """The job-level canary could not complete within its bounded first call."""

    feedling_error_class = "provider_config"


CompletionFn = Callable[..., dict[str, Any]]
_user_semaphores: dict[str, threading.BoundedSemaphore] = {}
_user_semaphores_lock = threading.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _per_user_concurrency() -> int:
    return max(1, min(_env_int("FEEDLING_GENESIS_LLM_USER_CONCURRENCY", 2), 16))


def _max_tokens_per_call() -> int:
    return max(128, min(_env_int("FEEDLING_GENESIS_LLM_MAX_TOKENS_PER_CALL", 8000), 32000))


@contextmanager
def _user_slot(user_id: str):
    with _user_semaphores_lock:
        sem = _user_semaphores.get(user_id)
        if sem is None:
            sem = threading.BoundedSemaphore(_per_user_concurrency())
            _user_semaphores[user_id] = sem
    acquired = sem.acquire(timeout=float(_env_int("FEEDLING_GENESIS_LLM_QUEUE_TIMEOUT_SEC", 30)))
    if not acquired:
        raise TimeoutError("genesis_llm_user_concurrency_timeout")
    try:
        yield
    finally:
        sem.release()


def _safe_output_type(idempotency_key: str) -> str:
    digest = hashlib.sha256(str(idempotency_key or "").encode("utf-8")).hexdigest()[:24]
    return f"llm:{digest}"


def _messages_hash(messages: list[dict[str, Any]]) -> str:
    raw = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class GenesisLLMClient:
    """Thin, idempotent wrapper around provider_client.chat_completion."""

    def __init__(
        self,
        completion_fn: CompletionFn | None = None,
        *,
        persist_output: bool = True,
        canary: bool = False,
    ):
        # Genesis v2 Step 1: drive every genesis LLM call through the retry wrapper
        # so a single cheap-relay blip (timeout / 429 / 5xx / empty) no longer kills
        # the whole import. provider_config failures (402 / bad key) are NOT retried.
        self._completion_fn = completion_fn or provider_client.reliable_chat_completion
        self._uses_default_completion = completion_fn is None
        self._canary_pending = bool(canary)
        # persist_output=True (default, cloud CVM): record each call's metadata + heartbeat
        # the job in Postgres. The VPS resident consumer reuses this SAME extraction engine
        # (chunk → fact_map → fact_write) with a local-agent completion_fn but has NO backend
        # DB, so it constructs the client with persist_output=False to skip both DB touches.
        self._persist_output = persist_output

    def complete(
        self,
        *,
        user_id: str,
        job_id: str,
        task_id: str,
        runtime: provider_client.ProviderConfig,
        messages: list[dict[str, Any]],
        max_tokens: int = 1200,
        timeout: float = 60.0,
        budget_label: str = "genesis",
        idempotency_key: str,
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
    ) -> GenesisLLMResult:
        if not idempotency_key:
            raise ValueError("idempotency_key_required")
        output_type = _safe_output_type(idempotency_key)

        capped_max_tokens = min(max_tokens, _max_tokens_per_call())
        # Retry/backoff lives in provider_client.reliable_chat_completion (the
        # default completion_fn): transient blips (timeout/429/5xx/empty) retry
        # there, provider_config (402/401) don't. Do NOT add a second retry layer
        # here — it would multiply one blip into N×M upstream calls.
        canary_call = self._canary_pending
        effective_timeout = (
            float(_env_int("FEEDLING_GENESIS_CANARY_TIMEOUT_SEC", 60))
            if canary_call
            else timeout
        )
        completion_kwargs = {
            "max_tokens": capped_max_tokens,
            "temperature": temperature,
            "timeout": effective_timeout,
            "response_format": response_format,
        }
        if self._uses_default_completion:
            # Keep 429/5xx at the shared default of three attempts, while bounding
            # ReadTimeout-like failures to two attempts for Genesis. The canary is
            # a single attempt by design: a slow first real call fails the job fast.
            completion_kwargs["max_attempts"] = 1 if canary_call else 3
            completion_kwargs["max_timeout_attempts"] = 1 if canary_call else 2
        try:
            with _user_slot(user_id):
                result = self._completion_fn(runtime, messages, **completion_kwargs)
        except Exception as exc:
            if not canary_call:
                raise
            raise DistillModelTooSlowError(
                f"distill_model_too_slow:{type(exc).__name__}:{str(exc)[:160]}"
            ) from exc
        self._canary_pending = False
        text = str(result.get("reply") or "")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        stop_reason = str(result.get("stop_reason") or "").strip()
        text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        doc = {
            "task_id": task_id,
            "provider": runtime.provider,
            "model": runtime.model,
            "base_url": runtime.base_url,
            "messages_sha256": _messages_hash(messages),
            "response_sha256": text_sha256,
            "response_chars": len(text),
            "max_tokens": capped_max_tokens,
            "timeout": effective_timeout,
            "budget_label": budget_label,
            "plaintext_stored": False,
            "stop_reason": stop_reason,
            "usage": usage,
        }
        if self._persist_output:
            db.genesis_upsert_output(user_id, job_id, output_type, doc=doc, status="done", ref=output_type)
            # Heartbeat the job after every LLM call. Every genesis LLM call funnels
            # through here, so this bumps updated_at across the whole reducer — map,
            # reduce, and the early-return source families alike — letting the stale
            # reaper tell a live long import from a worker that died mid-run, with no
            # per-call-site wiring to forget. Best-effort: a heartbeat blip is harmless
            # (the next call re-touches) and must never fail the LLM call.
            try:
                db.genesis_touch_job(user_id, job_id)
            except Exception:  # noqa: BLE001
                pass
        return GenesisLLMResult(
            text=text,
            usage=usage,
            cached=False,
            output_ref=output_type,
            stop_reason=stop_reason,
            max_tokens=capped_max_tokens,
        )
