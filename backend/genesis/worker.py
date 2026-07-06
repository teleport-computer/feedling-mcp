"""CVM-side Genesis worker.

This module is intended to run inside the agent-runner service. It claims
finalized import jobs, decrypts chunk envelopes via the enclave, calls the
user-configured LLM provider with the user's key, and posts only distilled
reducer output back to the backend apply route.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any, Callable

import httpx

import db
import debug_trace
import provider_client
from core.store import get_store
from genesis import checkpoint, foreground, prompts, service
from genesis.llm_client import GenesisLLMClient

GENESIS_WORKER_SCOPES = ["envelope_decrypt", "genesis"]
AI_PERSONA_SOURCE_KINDS = {
    "agent_prompt",
    "ai_persona",
    "character",
    "character_card",
    "companion_persona",
    # voice/persona backfill for pre-genesis host users: material is assembled from
    # the existing identity record (tone_style/custom_persona_prompt/self_introduction),
    # not an uploaded transcript — but it must route to the ai_persona family so the
    # worker runs persona_build (cutover gate 4 A).
    "companion_persona_backfill",
    "system_prompt",
}
MEMORY_SUMMARY_SOURCE_KINDS = {
    "memory_summary",
    "memories_summary",
    "memory_digest",
}
USER_PROFILE_SOURCE_KINDS = {
    "personal_profile",
    "persona",
    "persona_profile",
    "profile",
    "user_persona",
    "user_profile",
}
JSON_REPAIR_SYSTEM_PROMPT = (
    "You repair malformed JSON produced by a previous model call. "
    "Return exactly one valid JSON object and nothing else. "
    "Do not add, remove, translate, summarize, or reinterpret content. "
    "Only fix JSON syntax and escaping so json.loads can parse it."
)
TRUNCATION_STOP_REASONS = {
    "length",
    "max_output_tokens",
    "max_tokens",
    "max_tokens_reached",
    "max_tokens_stop",
}


class GenesisWorkerError(Exception):
    """Retryable/non-retryable worker failure surfaced into job.error."""


def _trace_genesis(store, event_type: str, *, job_id: str = "", status: str = "ok",
                   summary: str = "", detail: dict | None = None, dur_ms: float | None = None) -> None:
    try:
        debug_trace.trace_event(
            store,
            subsystem="genesis",
            type=event_type,
            actor="backend",
            status=status,
            job_id=job_id,
            trace_id=job_id,
            turn_id=job_id,
            summary=summary,
            detail=detail or {},
            dur_ms=dur_ms,
        )
    except Exception:
        pass


def _trace_enclave(store, event_type: str, *, job_id: str = "", status: str = "ok",
                   purpose: str = "", path: str = "/v1/envelope/decrypt",
                   summary: str = "", detail: dict | None = None,
                   dur_ms: float | None = None) -> None:
    try:
        debug_trace.trace_event(
            store,
            subsystem="enclave",
            type=event_type,
            actor="backend",
            status=status,
            job_id=job_id,
            trace_id=job_id,
            turn_id=job_id,
            summary=summary,
            explain="Genesis worker called the enclave; only metadata is recorded.",
            detail={
                "purpose": purpose,
                "path": path,
                **(detail or {}),
            },
            dur_ms=dur_ms,
        )
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _headers(runtime_token: str) -> dict[str, str]:
    return {"X-Feedling-Runtime-Token": runtime_token}


def _mint(mint_runtime_token: Callable, user_id: str) -> str:
    try:
        token = mint_runtime_token(user_id, scopes=GENESIS_WORKER_SCOPES)
    except TypeError:
        token = mint_runtime_token(user_id, GENESIS_WORKER_SCOPES)
    token = str(token or "").strip()
    if not token:
        raise GenesisWorkerError("runtime_token_unavailable")
    return token


def _json_object(text: str, *, task_id: str) -> dict:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
    try:
        parsed = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise GenesisWorkerError(f"{task_id}:invalid_json") from e
    if not isinstance(parsed, dict):
        raise GenesisWorkerError(f"{task_id}:json_not_object")
    return parsed


def _json_repair_messages(task_id: str, raw_text: str, error: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": JSON_REPAIR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task_id": task_id,
                    "json_error": str(error or "")[:500],
                    "malformed_json": str(raw_text or ""),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]


def _llm_max_tokens_cap() -> int:
    return max(128, min(_env_int("FEEDLING_GENESIS_LLM_MAX_TOKENS_PER_CALL", 8000), 32000))


def _usage_output_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in ("output_tokens", "completion_tokens", "candidatesTokenCount"):
        try:
            value = int(usage.get(key) or 0)
        except Exception:
            value = 0
        if value > 0:
            return value
    return 0


def _json_text_looks_truncated(text: str) -> bool:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
    if not raw:
        return False
    return "{" in raw and not raw.rstrip().endswith("}")


def _llm_result_likely_truncated(result: Any) -> bool:
    stop_reason = str(getattr(result, "stop_reason", "") or "").strip().lower()
    normalized_stop = re.sub(r"[^a-z0-9_]+", "_", stop_reason).strip("_")
    if normalized_stop in TRUNCATION_STOP_REASONS or "max_token" in normalized_stop:
        return True
    try:
        max_tokens = int(getattr(result, "max_tokens", 0) or 0)
    except Exception:
        max_tokens = 0
    output_tokens = _usage_output_tokens(getattr(result, "usage", {}))
    if max_tokens > 0 and output_tokens >= max(1, int(max_tokens * 0.98)):
        return True
    return _json_text_looks_truncated(str(getattr(result, "text", "") or ""))


def _json_repair_max_tokens(result: Any, requested_max_tokens: int) -> int:
    try:
        original_max = int(getattr(result, "max_tokens", 0) or requested_max_tokens)
    except Exception:
        original_max = requested_max_tokens
    original_max = max(1, original_max)
    if _llm_result_likely_truncated(result):
        target = max(original_max * 2, original_max + 1200)
    else:
        target = original_max + max(800, original_max // 4)
    return min(max(original_max + 1, target), _llm_max_tokens_cap())


def _chunks(seq: list[Any], size: int) -> list[list[Any]]:
    safe = max(1, size)
    return [seq[idx:idx + safe] for idx in range(0, len(seq), safe)]


def _source_family(source_kind: str) -> str:
    kind = re.sub(r"[^a-z0-9_]+", "_", str(source_kind or "").strip().lower()).strip("_")
    if kind.endswith("_import"):
        kind = kind[:-7]
    if kind in AI_PERSONA_SOURCE_KINDS:
        return "ai_persona"
    if kind in MEMORY_SUMMARY_SOURCE_KINDS:
        return "memory_summary"
    if kind in USER_PROFILE_SOURCE_KINDS:
        return "user_profile"
    return "history"


def _idempotency_prefix(job_id: str, key_prefix: str | None = None) -> str:
    prefix = str(key_prefix or "").strip()
    if prefix:
        return prefix
    return str(job_id or "").strip()


def _joined_material(chunk_texts: list[str]) -> str:
    parts = []
    for idx, text in enumerate(chunk_texts):
        cleaned = str(text or "").strip()
        if cleaned:
            parts.append(f"--- chunk {idx + 1} ---\n{cleaned}")
    return "\n\n".join(parts).strip()


def _source_tagged_fact_text(source_family: str, text: str) -> str:
    if source_family == "user_profile":
        return (
            "source_kind=user_profile\n"
            "This material is the user's own profile/persona. Extract only durable facts about the user. "
            "Do not infer the companion's name, identity, personality, dimensions, or voice from it.\n\n"
            f"{text}"
        )
    return text


def _strip_identity(doc: dict) -> dict:
    clean = dict(doc)
    clean.pop("identity", None)
    clean.pop("days_with_user", None)
    clean.pop("relationship_anchor_evidence", None)
    return clean


def _identity_only(doc: dict) -> dict:
    identity = doc.get("identity") if isinstance(doc.get("identity"), dict) else {}
    dims = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    out = {"memories": []}
    if identity.get("agent_name") or dims:
        out["identity"] = identity
    if out.get("identity") and doc.get("days_with_user") is not None:
        out["days_with_user"] = doc.get("days_with_user")
    if out.get("identity") and doc.get("relationship_anchor_evidence"):
        out["relationship_anchor_evidence"] = doc.get("relationship_anchor_evidence")
    return out


def _memory_summary_name_only(doc: dict) -> dict:
    clean = _strip_identity(doc)
    identity = doc.get("identity") if isinstance(doc.get("identity"), dict) else {}
    agent_name = str(identity.get("agent_name") or "").strip()
    if agent_name:
        clean["identity"] = {"agent_name": agent_name, "dimensions": []}
    if clean.get("identity") and doc.get("days_with_user") is not None:
        clean["days_with_user"] = doc.get("days_with_user")
    if clean.get("identity") and doc.get("relationship_anchor_evidence"):
        clean["relationship_anchor_evidence"] = doc.get("relationship_anchor_evidence")
    return clean


def _fetch_provider_key(api_url: str, enclave_url: str, runtime_token: str, *, store=None, job_id: str = "") -> str:
    try:
        resp = httpx.get(
            f"{api_url.rstrip('/')}/v1/model_api/key_envelope",
            headers=_headers(runtime_token),
            timeout=15,
        )
        resp.raise_for_status()
        envelope = resp.json().get("api_key_envelope")
    except Exception as e:  # noqa: BLE001
        raise GenesisWorkerError(f"provider_key_envelope_fetch_failed:{type(e).__name__}") from e
    if not isinstance(envelope, dict):
        raise GenesisWorkerError("provider_key_envelope_missing")
    return _decrypt_envelope(
        enclave_url,
        runtime_token,
        envelope,
        purpose="model_api_provider_key",
        store=store,
        job_id=job_id,
    ).decode("utf-8")


def _runtime_for_user(user_id: str, provider_key: str) -> provider_client.ProviderConfig:
    config = db.get_blob(user_id, "model_api")
    if not isinstance(config, dict):
        raise GenesisWorkerError("model_api_not_configured")
    if config.get("test_status") != "ok":
        raise GenesisWorkerError(f"model_api_not_tested:{config.get('test_status', '')}")
    try:
        provider, model, base_url = provider_client.validate_config(
            str(config.get("provider") or ""),
            str(config.get("model") or ""),
            str(config.get("base_url") or ""),
        )
    except provider_client.ProviderError as e:
        raise GenesisWorkerError(f"model_api_config_invalid:{str(e)[:120]}") from e
    return provider_client.ProviderConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=provider_key,
    )


def _decrypt_envelope(
    enclave_url: str,
    runtime_token: str,
    envelope: dict,
    *,
    purpose: str,
    store=None,
    job_id: str = "",
) -> bytes:
    started_at = time.time()
    _trace_enclave(
        store,
        "enclave.call.start",
        job_id=job_id,
        purpose=purpose,
        summary="enclave decrypt call started",
    )
    try:
        resp = httpx.post(
            f"{enclave_url.rstrip('/')}/v1/envelope/decrypt",
            headers=_headers(runtime_token),
            json={"envelope": envelope, "purpose": purpose},
            timeout=30,
            verify=False,
        )
        resp.raise_for_status()
        body = resp.json()
        plaintext_b64 = body.get("plaintext_b64") if isinstance(body, dict) else ""
        out = base64.b64decode(str(plaintext_b64 or ""), validate=True)
        _trace_enclave(
            store,
            "enclave.call.done",
            job_id=job_id,
            purpose=purpose,
            summary="enclave decrypt call done",
            detail={"status_code": resp.status_code},
            dur_ms=(time.time() - started_at) * 1000,
        )
        return out
    except Exception as e:  # noqa: BLE001
        error_type = type(e).__name__
        _trace_enclave(
            store,
            "enclave.call.timeout" if isinstance(e, httpx.TimeoutException) else "enclave.call.error",
            job_id=job_id,
            status="error",
            purpose=purpose,
            summary="enclave decrypt call failed",
            detail={"error_class": error_type},
            dur_ms=(time.time() - started_at) * 1000,
        )
        raise GenesisWorkerError(f"{purpose}:decrypt_failed:{type(e).__name__}") from e


def _decrypt_chunks(enclave_url: str, runtime_token: str, chunks: list[dict], *, store=None, job_id: str = "") -> list[str]:
    texts: list[str] = []
    for chunk in chunks:
        envelope = service.chunk_envelope_from_row(chunk)
        raw = _decrypt_envelope(
            enclave_url,
            runtime_token,
            envelope,
            purpose="genesis_chunk",
            store=store,
            job_id=job_id,
        )
        texts.append(raw.decode("utf-8"))
    return texts


def _decrypt_blob_text(
    enclave_url: str,
    runtime_token: str,
    blob: dict,
    *,
    purpose: str,
    store=None,
    job_id: str = "",
) -> str:
    envelope = blob.get("content_envelope") if isinstance(blob.get("content_envelope"), dict) else {}
    if not envelope:
        return ""
    return _decrypt_envelope(enclave_url, runtime_token, envelope, purpose=purpose, store=store, job_id=job_id).decode("utf-8")


def _existing_persona_material(user_id: str, enclave_url: str, runtime_token: str, *, store=None, job_id: str = "") -> dict:
    try:
        blob = db.get_blob(user_id, service.GENESIS_PERSONA_BLOB)
        if not isinstance(blob, dict):
            return {}
        try:
            priority = int(blob.get("source_priority") or 0)
        except Exception:
            priority = 0
        if priority < 100:
            return {}
        content = _decrypt_blob_text(
            enclave_url,
            runtime_token,
            blob,
            purpose="genesis_persona",
            store=store,
            job_id=job_id,
        ).strip()
        if not content:
            return {}
        return {
            "content": content,
            "source_family": str(blob.get("source_family") or ""),
            "source_priority": priority,
        }
    except Exception:
        return {}


def _existing_voice_workset(user_id: str, enclave_url: str, runtime_token: str, *, store=None, job_id: str = "") -> dict:
    try:
        blob = db.get_blob(user_id, service.GENESIS_VOICE_BLOB)
        if not isinstance(blob, dict):
            return {}
        raw = _decrypt_blob_text(
            enclave_url,
            runtime_token,
            blob,
            purpose="genesis_voice",
            store=store,
            job_id=job_id,
        )
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _complete_json(
    llm: GenesisLLMClient,
    *,
    user_id: str,
    job_id: str,
    task_id: str,
    runtime: provider_client.ProviderConfig,
    messages: list[dict[str, str]],
    max_tokens: int,
    idempotency_key: str,
    temperature: float = 0.2,
) -> dict:
    result = llm.complete(
        user_id=user_id,
        job_id=job_id,
        task_id=task_id,
        runtime=runtime,
        messages=messages,
        max_tokens=max_tokens,
        timeout=float(_env_int("FEEDLING_GENESIS_LLM_TIMEOUT_SEC", 90)),
        idempotency_key=idempotency_key,
        temperature=temperature,
    )
    try:
        return _json_object(result.text, task_id=task_id)
    except GenesisWorkerError as first_error:
        if str(first_error) not in {f"{task_id}:invalid_json", f"{task_id}:json_not_object"}:
            raise
        repair = llm.complete(
            user_id=user_id,
            job_id=job_id,
            task_id=f"{task_id}-json-repair",
            runtime=runtime,
            messages=_json_repair_messages(task_id, result.text, str(first_error)),
            max_tokens=_json_repair_max_tokens(result, max_tokens),
            timeout=float(_env_int("FEEDLING_GENESIS_LLM_TIMEOUT_SEC", 90)),
            idempotency_key=f"{idempotency_key}:json_repair",
            temperature=0.0,
        )
        try:
            return _json_object(repair.text, task_id=task_id)
        except GenesisWorkerError as repair_error:
            raise GenesisWorkerError(f"{task_id}:invalid_json_after_repair") from repair_error


def _complete_json_retry_empty(
    llm: GenesisLLMClient,
    *,
    user_id: str,
    job_id: str,
    task_id: str,
    runtime: provider_client.ProviderConfig,
    messages: list[dict[str, str]],
    max_tokens: int,
    idempotency_key: str,
    is_empty,
    temperature: float = 0.2,
    max_attempts: int = 2,
) -> dict:
    attempts = max(1, int(max_attempts))
    last: dict = {}
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        suffix = "" if attempt == 0 else f"-empty-retry-{attempt}"
        key_suffix = "" if attempt == 0 else f":empty_retry:{attempt}"
        try:
            last = _complete_json(
                llm,
                user_id=user_id,
                job_id=job_id,
                task_id=f"{task_id}{suffix}",
                runtime=runtime,
                messages=messages,
                max_tokens=max_tokens,
                idempotency_key=f"{idempotency_key}{key_suffix}",
                temperature=temperature,
            )
        except provider_client.ProviderError as e:
            if provider_client.classify_provider_error(e) == "provider_config":
                raise  # hard error (402/401/403/quota/key) — never retry
            last_exc = e
            continue  # transient/unknown — try again next attempt
        except GenesisWorkerError as e:  # bad/invalid JSON — treat as transient
            last_exc = e
            continue
        if not is_empty(last):
            return last  # got usable content — success
    if last_exc is not None and not last:
        raise last_exc  # every attempt errored, nothing usable — surface the last error
    return last  # got a result at some point (possibly empty) — let caller handle it


def _combined_map_empty(output: dict) -> bool:
    facts = output.get("fact_candidates") if isinstance(output.get("fact_candidates"), list) else []
    voice = _voice_candidate_from_combined_map(output)
    notes = voice.get("behavior_notes_candidates") if isinstance(voice.get("behavior_notes_candidates"), list) else []
    exemplars = voice.get("exemplar_candidates") if isinstance(voice.get("exemplar_candidates"), list) else []
    return not facts and not notes and not exemplars


def _fact_write_output_empty(output: dict) -> bool:
    memories = output.get("memories") if isinstance(output.get("memories"), list) else []
    identity = output.get("identity") if isinstance(output.get("identity"), dict) else {}
    dims = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    if memories or str(identity.get("agent_name") or "").strip() or dims:
        return False
    try:
        if int(output.get("days_with_user") or 0) > 0:
            return False
    except Exception:
        pass
    return not str(output.get("relationship_anchor_evidence") or "").strip()


def _complete_text(
    llm: GenesisLLMClient,
    *,
    user_id: str,
    job_id: str,
    task_id: str,
    runtime: provider_client.ProviderConfig,
    messages: list[dict[str, str]],
    max_tokens: int,
    idempotency_key: str,
    temperature: float = 0.2,
) -> str:
    result = llm.complete(
        user_id=user_id,
        job_id=job_id,
        task_id=task_id,
        runtime=runtime,
        messages=messages,
        max_tokens=max_tokens,
        timeout=float(_env_int("FEEDLING_GENESIS_LLM_TIMEOUT_SEC", 90)),
        idempotency_key=idempotency_key,
        temperature=temperature,
    )
    return result.text


def _voice_reduce(
    llm: GenesisLLMClient,
    *,
    user_id: str,
    job_id: str,
    key_prefix: str | None = None,
    runtime: provider_client.ProviderConfig,
    candidates: list[dict],
) -> dict:
    if not candidates:
        return {"behavior_notes": [], "exemplars": []}
    idempotency_prefix = _idempotency_prefix(job_id, key_prefix)
    batch_size = max(2, _env_int("FEEDLING_GENESIS_VOICE_REDUCE_BATCH", 24))
    current = list(candidates)
    round_no = 0
    while len(current) > batch_size:
        next_round: list[dict] = []
        for idx, batch in enumerate(_chunks(current, batch_size)):
            reduced = _complete_json(
                llm,
                user_id=user_id,
                job_id=job_id,
                task_id=f"voice-reduce-{round_no}-{idx}",
                runtime=runtime,
                messages=prompts.voice_reduce_messages(batch),
                max_tokens=4000,
                idempotency_key=f"{idempotency_prefix}:voice_reduce:{round_no}:{idx}",
            )
            next_round.append({
                "behavior_notes_candidates": reduced.get("behavior_notes") or [],
                "exemplar_candidates": reduced.get("exemplars") or [],
            })
        current = next_round
        round_no += 1
    return _complete_json(
        llm,
        user_id=user_id,
        job_id=job_id,
        task_id=f"voice-reduce-{round_no}",
        runtime=runtime,
        messages=prompts.voice_reduce_messages(current),
        max_tokens=4000,
        idempotency_key=f"{idempotency_prefix}:voice_reduce:{round_no}:final",
    )


def _fact_write(
    llm: GenesisLLMClient,
    *,
    user_id: str,
    job_id: str,
    key_prefix: str | None = None,
    runtime: provider_client.ProviderConfig,
    fact_candidates: list[dict],
    persona_material: str = "",
    memory_summary: str = "",
    known_memories: list[str] | None = None,
) -> dict:
    if not fact_candidates and not persona_material and not memory_summary:
        return {"memories": [], "identity": {"agent_name": "", "dimensions": []}}
    idempotency_prefix = _idempotency_prefix(job_id, key_prefix)
    batch_size = max(4, _env_int("FEEDLING_GENESIS_FACT_WRITE_BATCH", 80))
    outputs: list[dict] = []
    for idx, batch in enumerate(_chunks(fact_candidates, batch_size) or [[]]):
        outputs.append(_complete_json_retry_empty(
            llm,
            user_id=user_id,
            job_id=job_id,
            task_id=f"fact-write-{idx}",
            runtime=runtime,
            messages=prompts.fact_write_messages(batch, persona_material, memory_summary, known_memories),
            max_tokens=4000,
            idempotency_key=f"{idempotency_prefix}:fact_write:{idx}",
            is_empty=_fact_write_output_empty,
        ))
    memories: list[dict] = []
    dims: list[dict] = []
    agent_name = ""
    days_with_user = 0
    evidence: list[str] = []
    for output in outputs:
        if isinstance(output.get("memories"), list):
            memories.extend(item for item in output["memories"] if isinstance(item, dict))
        identity = output.get("identity") if isinstance(output.get("identity"), dict) else {}
        if not agent_name and identity.get("agent_name"):
            agent_name = str(identity.get("agent_name") or "")
        if isinstance(identity.get("dimensions"), list):
            dims.extend(item for item in identity["dimensions"] if isinstance(item, dict))
        try:
            days_with_user = max(days_with_user, int(output.get("days_with_user") or 0))
        except Exception:
            pass
        if output.get("relationship_anchor_evidence"):
            evidence.append(str(output.get("relationship_anchor_evidence") or ""))
    return {
        "memories": memories,
        "identity": {"agent_name": agent_name, "dimensions": dims[:7]},
        "days_with_user": days_with_user,
        "relationship_anchor_evidence": " | ".join(evidence)[:500],
    }


def _build_reducer_output(
    *,
    user_id: str,
    job_id: str,
    key_prefix: str | None = None,
    runtime: provider_client.ProviderConfig,
    chunk_texts: list[str],
    source_kind: str = "history",
    existing_persona: dict | None = None,
    existing_voice: dict | None = None,
    skip_fact_texts: set[str] | None = None,
    known_memories: list[str] | None = None,
    include_memory: bool = True,
    include_persona_voice: bool = True,
) -> dict:
    llm = GenesisLLMClient()
    idempotency_prefix = _idempotency_prefix(job_id, key_prefix)
    source_family = _source_family(source_kind)
    material = _joined_material(chunk_texts)
    existing_persona = existing_persona if isinstance(existing_persona, dict) else {}
    existing_voice = existing_voice if isinstance(existing_voice, dict) else {}

    if source_family == "ai_persona":
        existing_notes = existing_voice.get("behavior_notes") if isinstance(existing_voice.get("behavior_notes"), list) else []
        existing_exemplars = existing_voice.get("exemplars") if isinstance(existing_voice.get("exemplars"), list) else []
        founding = [item for item in existing_exemplars if isinstance(item, dict) and item.get("founding")]
        if not founding:
            founding = [item for item in existing_exemplars if isinstance(item, dict)][:12]
        persona_source_family = "merged" if existing_notes or founding else "ai_persona"
        identity_doc = _identity_only(_fact_write(
            llm,
            user_id=user_id,
            job_id=job_id,
            key_prefix=idempotency_prefix,
            runtime=runtime,
            fact_candidates=[],
            persona_material=material,
        )) if include_memory else {"memories": []}
        out = {
            **identity_doc,
            "source_kind": source_kind,
            "source_family": source_family,
            "voice": {
                "behavior_notes_count": len(existing_notes),
                "exemplar_count": len(existing_exemplars),
                "founding_exemplar_count": len(founding),
            },
        }
        if include_persona_voice:
            persona_content = _complete_text(
                llm,
                user_id=user_id,
                job_id=job_id,
                task_id="persona-build",
                runtime=runtime,
                messages=prompts.persona_build_messages(material, existing_notes, founding),
                max_tokens=4000,
                idempotency_key=f"{idempotency_prefix}:persona_build",
            )
            out["persona"] = {
                "content": persona_content,
                "prompt_version": "7.B",
                "source_kind": source_kind,
                "source_family": persona_source_family,
            }
        return out

    if source_family == "memory_summary":
        fact_write = _memory_summary_name_only(_fact_write(
            llm,
            user_id=user_id,
            job_id=job_id,
            key_prefix=idempotency_prefix,
            runtime=runtime,
            fact_candidates=[],
            memory_summary=material,
        )) if include_memory else {"memories": []}
        return {
            **fact_write,
            "source_kind": source_kind,
            "source_family": source_family,
            "voice": {
                "behavior_notes_count": 0,
                "exemplar_count": 0,
                "founding_exemplar_count": 0,
            },
        }

    voice_candidates: list[dict] = []
    fact_candidates: list[dict] = []
    fact_map_attempts = 0
    fact_map_failures = 0
    for idx, text in enumerate(chunk_texts):
        if include_persona_voice and source_family == "history":
            try:
                voice = _complete_json(
                    llm,
                    user_id=user_id,
                    job_id=job_id,
                    task_id=f"voice-map-{idx}",
                    runtime=runtime,
                    messages=prompts.voice_map_messages(text),
                    max_tokens=1800,
                    idempotency_key=f"{idempotency_prefix}:voice_map:{idx}",
                )
                voice_candidates.append(voice)
            except (provider_client.ProviderError, GenesisWorkerError) as e:
                # Voice is an enhancement; one chunk failing to map (after the
                # client's own retries) shouldn't sink the whole import. Drop this
                # chunk's voice contribution and keep going.
                print(f"[genesis:{job_id}] voice-map-{idx} skipped: {type(e).__name__}:{str(e)[:120]}")
        if not include_memory:
            continue
        fact_map_attempts += 1
        try:
            facts = _complete_json(
                llm,
                user_id=user_id,
                job_id=job_id,
                task_id=f"fact-map-{idx}",
                runtime=runtime,
                messages=prompts.fact_map_messages(_source_tagged_fact_text(source_family, text)),
                max_tokens=1800,
                idempotency_key=f"{idempotency_prefix}:fact_map:{idx}",
            )
            if isinstance(facts.get("fact_candidates"), list):
                fact_candidates.extend(item for item in facts["fact_candidates"] if isinstance(item, dict))
        except (provider_client.ProviderError, GenesisWorkerError) as e:
            fact_map_failures += 1
            print(f"[genesis:{job_id}] fact-map-{idx} skipped: {type(e).__name__}:{str(e)[:120]}")
        # NB: the job's updated_at heartbeat lives in GenesisLLMClient.complete
        # (fires on every LLM call), so it covers the reduce phase and early-return
        # source families too — no per-chunk wiring needed here.
    # Per-chunk tolerance has a floor: if EVERY fact-map failed, the relay is
    # effectively unusable for this user — fail loudly so the job stays retryable
    # rather than writing an empty/garbage memory garden from zero candidates.
    if include_memory and fact_map_attempts and fact_map_failures == fact_map_attempts:
        raise GenesisWorkerError(f"all_fact_maps_failed:{fact_map_failures}/{fact_map_attempts}")

    if include_memory and skip_fact_texts:
        # Genesis v2: drop the candidates the foreground already wrote as core, so the
        # background reduce never re-writes them (structural dedup — foreground and
        # background see the SAME cached candidates, so normalized text matches exactly).
        fact_candidates = [
            c for c in fact_candidates
            if checkpoint.normalize_fact_text(str(c.get("summary") or c.get("content") or "")) not in skip_fact_texts
        ]

    voice_final = _voice_reduce(
        llm,
        user_id=user_id,
        job_id=job_id,
        key_prefix=idempotency_prefix,
        runtime=runtime,
        candidates=voice_candidates,
    ) if include_persona_voice and source_family == "history" else {"behavior_notes": [], "exemplars": []}
    exemplars = voice_final.get("exemplars") if isinstance(voice_final.get("exemplars"), list) else []
    founding = [item for item in exemplars if isinstance(item, dict) and item.get("founding")]
    if not founding:
        founding = [item for item in exemplars if isinstance(item, dict)][:12]
    behavior_notes = voice_final.get("behavior_notes") if isinstance(voice_final.get("behavior_notes"), list) else []
    voice_workset = {
        "behavior_notes": behavior_notes,
        "exemplars": exemplars,
    }

    fact_write = _fact_write(
        llm,
        user_id=user_id,
        job_id=job_id,
        key_prefix=idempotency_prefix,
        runtime=runtime,
        fact_candidates=fact_candidates,
        known_memories=known_memories,   # genesis v2: foreground core -> "已保存,勿重复"
    ) if include_memory else {"memories": [], "identity": {"agent_name": "", "dimensions": []}}
    if source_family == "user_profile":
        fact_write = _strip_identity(fact_write)
        return {
            **fact_write,
            "source_kind": source_kind,
            "source_family": source_family,
            "voice": {
                "behavior_notes_count": 0,
                "exemplar_count": 0,
                "founding_exemplar_count": 0,
            },
        }

    persona_material = str(existing_persona.get("content") or "").strip()
    persona_source_family = "merged" if persona_material else "history"
    out = {
        **fact_write,
        "source_kind": source_kind,
        "source_family": source_family,
        "voice": {
            "behavior_notes_count": len(behavior_notes),
            "exemplar_count": len(exemplars),
            "founding_exemplar_count": len(founding),
        },
        "voice_workset": voice_workset,
    }
    if include_persona_voice:
        persona_content = _complete_text(
            llm,
            user_id=user_id,
            job_id=job_id,
            task_id="persona-build",
            runtime=runtime,
            messages=prompts.persona_build_messages(persona_material, behavior_notes, founding),
            max_tokens=4000,
            idempotency_key=f"{idempotency_prefix}:persona_build",
        )
        out["persona"] = {
            "content": persona_content,
            "prompt_version": "7.B",
            "source_kind": source_kind,
            "source_family": persona_source_family,
        }
    return out


def build_reducer_output_from_texts(
    *,
    user_id: str,
    job_id: str,
    key_prefix: str | None = None,
    runtime: provider_client.ProviderConfig,
    chunk_texts: list[str],
    source_kind: str = "history",
    existing_persona: dict | None = None,
    existing_voice: dict | None = None,
    skip_fact_texts: set[str] | None = None,
    known_memories: list[str] | None = None,
    include_memory: bool = True,
    include_persona_voice: bool = True,
) -> dict:
    """Public wrapper for trusted in-memory Genesis inputs.

    The chunked worker path still decrypts uploaded envelopes before calling the
    private reducer. Plaintext one-shot imports enter the CVM as request bodies
    and can call this wrapper directly without staging raw text in storage.
    """
    return _build_reducer_output(
        user_id=user_id,
        job_id=job_id,
        key_prefix=key_prefix,
        runtime=runtime,
        chunk_texts=chunk_texts,
        source_kind=source_kind,
        existing_persona=existing_persona,
        existing_voice=existing_voice,
        skip_fact_texts=skip_fact_texts,
        known_memories=known_memories,
        include_memory=include_memory,
        include_persona_voice=include_persona_voice,
    )


def build_memory_output_from_fact_candidates(
    *,
    user_id: str,
    job_id: str,
    key_prefix: str | None = None,
    runtime: provider_client.ProviderConfig,
    fact_candidates: list[dict],
    known_memories: list[str] | None = None,
    llm: GenesisLLMClient | None = None,
) -> dict:
    """Run the Genesis fact_write step directly for already-mapped candidates.

    Foreground v2 already has all fact candidates after fact_map. This helper lets
    the route write the full memory set once, without re-mapping the transcript or
    waiting for voice/persona.
    """
    llm = llm or GenesisLLMClient()
    return _fact_write(
        llm,
        user_id=user_id,
        job_id=job_id,
        key_prefix=key_prefix,
        runtime=runtime,
        fact_candidates=[item for item in fact_candidates if isinstance(item, dict)],
        known_memories=known_memories,
    )


def build_voice_persona_output_from_candidates(
    *,
    user_id: str,
    job_id: str,
    key_prefix: str | None = None,
    runtime: provider_client.ProviderConfig,
    voice_candidates: list[dict],
    existing_persona: dict | None = None,
    llm: GenesisLLMClient | None = None,
) -> dict:
    """Reduce already-mapped voice candidates and build the persona artifact.

    Round2 keeps voice_reduce/persona_build unchanged. The only new prompt is
    combined_map; this helper starts after map and reuses the old reduce/build
    contracts so quality can be compared and rolled back cleanly.
    """
    llm = llm or GenesisLLMClient()
    prefix = _idempotency_prefix(job_id, key_prefix)
    existing_persona = existing_persona if isinstance(existing_persona, dict) else {}
    voice_final = _voice_reduce(
        llm,
        user_id=user_id,
        job_id=job_id,
        key_prefix=prefix,
        runtime=runtime,
        candidates=[item for item in voice_candidates if isinstance(item, dict)],
    )
    exemplars = voice_final.get("exemplars") if isinstance(voice_final.get("exemplars"), list) else []
    founding = [item for item in exemplars if isinstance(item, dict) and item.get("founding")]
    if not founding:
        founding = [item for item in exemplars if isinstance(item, dict)][:12]
    behavior_notes = voice_final.get("behavior_notes") if isinstance(voice_final.get("behavior_notes"), list) else []
    persona_material = str(existing_persona.get("content") or "").strip()
    persona_source_family = "merged" if persona_material else "history"
    persona_content = _complete_text(
        llm,
        user_id=user_id,
        job_id=job_id,
        task_id="persona-build",
        runtime=runtime,
        messages=prompts.persona_build_messages(persona_material, behavior_notes, founding),
        max_tokens=4000,
        idempotency_key=f"{prefix}:persona_build",
    )
    return {
        "persona": {
            "content": persona_content,
            "prompt_version": "7.B",
            "source_kind": "history",
            "source_family": persona_source_family,
        },
        "voice": {
            "behavior_notes_count": len(behavior_notes),
            "exemplar_count": len(exemplars),
            "founding_exemplar_count": len(founding),
        },
        "voice_workset": {
            "behavior_notes": behavior_notes,
            "exemplars": exemplars,
        },
    }


def build_persona_output_from_material(
    *,
    user_id: str,
    job_id: str,
    key_prefix: str | None = None,
    runtime: provider_client.ProviderConfig,
    persona_material: str,
    voice_workset: dict | None = None,
    source_kind: str = "identity_update",
    source_family: str = "ai_persona",
    llm: GenesisLLMClient | None = None,
) -> dict:
    """Build a persona artifact from explicit role-card material.

    Used by update_identity: the agent's spawned persona is generated from the
    uploaded role card, not from the normalized Identity Card. Existing voice
    workset is reused when present so name/persona updates do not rewrite voice.
    """
    llm = llm or GenesisLLMClient()
    prefix = _idempotency_prefix(job_id, key_prefix)
    workset = voice_workset if isinstance(voice_workset, dict) else {}
    behavior_notes = workset.get("behavior_notes") if isinstance(workset.get("behavior_notes"), list) else []
    exemplars = workset.get("exemplars") if isinstance(workset.get("exemplars"), list) else []
    founding = [item for item in exemplars if isinstance(item, dict) and item.get("founding")]
    if not founding:
        founding = [item for item in exemplars if isinstance(item, dict)][:12]
    persona_content = _complete_text(
        llm,
        user_id=user_id,
        job_id=job_id,
        task_id="persona-build",
        runtime=runtime,
        messages=prompts.persona_build_messages(str(persona_material or "").strip(), behavior_notes, founding),
        max_tokens=4000,
        idempotency_key=f"{prefix}:persona_build",
    )
    return {
        "persona": {
            "content": persona_content,
            "prompt_version": "7.B",
            "source_kind": source_kind,
            "source_family": source_family,
        },
        "source_kind": source_kind,
        "source_family": source_family,
        "voice_workset": {
            "behavior_notes": behavior_notes,
            "exemplars": exemplars,
        },
    }


def genesis_v2_enabled() -> bool:
    """Genesis v2 (foreground-fast) runs ONLY when FEEDLING_GENESIS_V2_ENABLED is
    truthy. Default OFF — the existing one-shot path stays byte-for-byte the
    fallback. Flip on (test first), flip off + restart to revert instantly, no
    deploy. This is the single gate the whole v2 main-path change hangs off of."""
    return str(os.environ.get("FEEDLING_GENESIS_V2_ENABLED", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def genesis_combined_map_enabled() -> bool:
    """Flag for Round2 onboarding: combine per-chunk fact + voice extraction.

    Off means the existing foreground map remains fact-only; routes can keep the
    old background voice/persona path as a safe rollback.
    """
    return str(os.environ.get("FEEDLING_GENESIS_COMBINED_MAP", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _voice_candidate_from_combined_map(parsed: dict) -> dict:
    raw = parsed.get("voice_candidates")
    if isinstance(raw, dict):
        notes = raw.get("behavior_notes_candidates")
        exemplars = raw.get("exemplar_candidates")
        return {
            "behavior_notes_candidates": notes if isinstance(notes, list) else [],
            "exemplar_candidates": exemplars if isinstance(exemplars, list) else [],
        }
    if isinstance(raw, list):
        notes: list = []
        exemplars: list = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            item_notes = item.get("behavior_notes_candidates")
            item_exemplars = item.get("exemplar_candidates")
            if isinstance(item_notes, list):
                notes.extend(item_notes)
            if isinstance(item_exemplars, list):
                exemplars.extend(item_exemplars)
        return {"behavior_notes_candidates": notes, "exemplar_candidates": exemplars}
    return {"behavior_notes_candidates": [], "exemplar_candidates": []}


def build_foreground_output_from_texts(
    *,
    user_id: str,
    job_id: str,
    key_prefix: str | None = None,
    runtime: provider_client.ProviderConfig,
    chunk_texts: list[str],
    source_kind: str = "history",
    foreground_core_max: int = foreground.FOREGROUND_CORE_MAX,
    llm: GenesisLLMClient | None = None,
    write_core: bool = True,
    include_voice_candidates: bool = False,
) -> dict:
    """Genesis v2 FOREGROUND — the light "open the door" pass (Codex flow).

    fact_map over every chunk ONCE -> pick 3-5 core fact_candidates -> fact_write
    ONLY those -> identity baseline. Deliberately NO voice_map/voice_reduce/persona
    /full fact_write: those are the background's job. The returned dict carries the
    SAME full fact_candidate list + the chosen core so the background partitions
    against them (one extraction, shared candidates — never a second, divergent run).

    Cache discipline: fact_map uses the SAME idempotency prefix as the background
    reduce, so the two SHARE the cached extraction. fact_write uses a distinct
    `:fg` prefix, so the foreground's core write never collides with the
    background's fact_write batches.
    """
    llm = llm or GenesisLLMClient()
    source_family = _source_family(source_kind)
    shared_prefix = _idempotency_prefix(job_id, key_prefix)   # shared with background
    fg_write_prefix = f"{shared_prefix}:fg"                   # distinct fact_write namespace

    fact_candidates: list[dict] = []
    voice_candidates: list[dict] = []
    for idx, text in enumerate(chunk_texts):
        if include_voice_candidates and source_family == "history" and genesis_combined_map_enabled():
            facts = _complete_json_retry_empty(
                llm,
                user_id=user_id,
                job_id=job_id,
                task_id=f"combined-map-{idx}",
                runtime=runtime,
                messages=prompts.combined_map_messages(text),
                max_tokens=2400,
                idempotency_key=f"{shared_prefix}:combined_map:{idx}",
                is_empty=_combined_map_empty,
            )
            voice_candidates.append(_voice_candidate_from_combined_map(facts))
        else:
            facts = _complete_json(
                llm,
                user_id=user_id,
                job_id=job_id,
                task_id=f"fact-map-{idx}",
                runtime=runtime,
                messages=prompts.fact_map_messages(_source_tagged_fact_text(source_family, text)),
                max_tokens=1800,
                idempotency_key=f"{shared_prefix}:fact_map:{idx}",   # SAME key as background -> cache shared
            )
        if isinstance(facts.get("fact_candidates"), list):
            fact_candidates.extend(item for item in facts["fact_candidates"] if isinstance(item, dict))

    core = foreground.select_core_for_foreground(fact_candidates, max_n=foreground_core_max)
    fact_write = _fact_write(
        llm,
        user_id=user_id,
        job_id=job_id,
        key_prefix=fg_write_prefix,   # distinct -> never collides with background fact_write
        runtime=runtime,
        fact_candidates=core,
    ) if write_core else {"memories": [], "identity": {"agent_name": "", "dimensions": []}}
    return {
        "memories": fact_write.get("memories") or [],
        "identity": fact_write.get("identity") or {"agent_name": "", "dimensions": []},
        "source_kind": source_kind,
        "source_family": source_family,
        "foreground": True,
        # handed to the background so it writes only the rest (structural dedup, Codex #1)
        "all_fact_candidates": fact_candidates,
        "core_fact_candidates": core,
        "voice_candidates": voice_candidates,
    }


def derive_identity_from_persona(
    *,
    user_id: str,
    job_id: str,
    key_prefix: str | None = None,
    runtime: provider_client.ProviderConfig,
    persona_content: str,
    llm: GenesisLLMClient | None = None,
) -> dict:
    """Baseline identity guarantee. The reduce can come back with NO structured identity
    (history-only upload / weak naming signal) even though a persona prose WAS generated
    — onboarding then wedges on identity_card. This extracts agent_name/dimensions/
    category from the GENERATED persona prose, via the SAME path an uploaded ai_persona
    card uses (_fact_write(persona_material)). Returns the identity dict (still empty if
    the persona truly carries no name/character — we never fabricate). One LLM call."""
    text = str(persona_content or "").strip()
    if not text:
        return {}
    llm = llm or GenesisLLMClient()
    doc = _identity_only(_fact_write(
        llm,
        user_id=user_id,
        job_id=job_id,
        key_prefix=f"{_idempotency_prefix(job_id, key_prefix)}:persona_identity",
        runtime=runtime,
        fact_candidates=[],
        persona_material=text,
    ))
    return doc.get("identity") if isinstance(doc.get("identity"), dict) else {}


def _apply_reducer_output(api_url: str, runtime_token: str, job_id: str, output: dict) -> dict:
    try:
        resp = httpx.post(
            f"{api_url.rstrip('/')}/v1/genesis/imports/{job_id}/outputs",
            headers=_headers(runtime_token),
            json={"reducer_output": output},
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        raise GenesisWorkerError(f"apply_outputs_failed:{type(e).__name__}") from e
    return body if isinstance(body, dict) else {}


def _process_job(job: dict, *, api_url: str, enclave_url: str, mint_runtime_token: Callable) -> dict:
    started_at = time.time()
    user_id = str(job.get("user_id") or "")
    job_id = str(job.get("job_id") or "")
    if not user_id or not job_id:
        raise GenesisWorkerError("invalid_claimed_job")
    store = get_store(user_id)
    _trace_genesis(
        store,
        "genesis.worker.claimed",
        job_id=job_id,
        summary="genesis worker claimed job",
        detail={
            "source_kind": str(job.get("source_kind") or ""),
            "total_chunks": int(job.get("total_chunks") or 0),
        },
    )
    service.write_genesis_state(store, {**job, "status": "processing"}, status="processing")
    total_chunks = int(job.get("total_chunks") or 0)
    if total_chunks <= 0:
        raise GenesisWorkerError("empty_import")
    missing = db.genesis_missing_chunk_seqs(user_id, job_id, total_chunks)
    if missing:
        raise GenesisWorkerError(f"missing_chunks:{len(missing)}")
    chunks = db.genesis_list_chunks(user_id, job_id)
    if total_chunks and len(chunks) != total_chunks:
        raise GenesisWorkerError(f"chunk_count_mismatch:{len(chunks)}:{total_chunks}")

    token = _mint(mint_runtime_token, user_id)
    _trace_genesis(store, "genesis.worker.token.minted", job_id=job_id, summary="runtime token minted")
    provider_key = _fetch_provider_key(api_url, enclave_url, token, store=store, job_id=job_id)
    _trace_genesis(store, "genesis.worker.provider_key.loaded", job_id=job_id,
                   summary="provider key loaded", detail={"has_provider_key": bool(provider_key)})
    runtime = _runtime_for_user(user_id, provider_key)
    chunk_texts = _decrypt_chunks(enclave_url, token, chunks, store=store, job_id=job_id)
    _trace_genesis(store, "genesis.worker.chunks.decrypted", job_id=job_id,
                   summary="encrypted chunks decrypted", detail={"chunk_count": len(chunk_texts)})
    existing_persona = _existing_persona_material(user_id, enclave_url, token, store=store, job_id=job_id)
    existing_voice = _existing_voice_workset(user_id, enclave_url, token, store=store, job_id=job_id)
    reducer_started_at = time.time()
    _trace_genesis(store, "genesis.worker.reducer.started", job_id=job_id,
                   summary="worker reducer started", detail={"chunk_count": len(chunk_texts)})
    reducer_output = _build_reducer_output(
        user_id=user_id,
        job_id=job_id,
        runtime=runtime,
        chunk_texts=chunk_texts,
        source_kind=str(job.get("source_kind") or "history"),
        existing_persona=existing_persona,
        existing_voice=existing_voice,
    )
    _trace_genesis(
        store,
        "genesis.worker.reducer.done",
        job_id=job_id,
        summary="worker reducer done",
        detail={
            "memory_count": len(reducer_output.get("memories") or []) if isinstance(reducer_output, dict) else 0,
            "has_identity": bool(isinstance(reducer_output, dict) and reducer_output.get("identity")),
            "has_persona": bool(isinstance(reducer_output, dict) and reducer_output.get("persona")),
        },
        dur_ms=(time.time() - reducer_started_at) * 1000,
    )
    _trace_genesis(store, "genesis.worker.apply.started", job_id=job_id, summary="worker apply started")
    applied = _apply_reducer_output(api_url, token, job_id, reducer_output)
    _trace_genesis(
        store,
        "genesis.worker.done",
        job_id=job_id,
        summary="genesis worker job done",
        detail={"chunks": len(chunks)},
        dur_ms=(time.time() - started_at) * 1000,
    )
    return {
        "user_id": user_id,
        "job_id": job_id,
        "status": "done",
        "chunks": len(chunks),
        "applied": applied.get("applied", applied),
    }


def _genesis_stale_sec() -> int:
    return max(300, _env_int("FEEDLING_GENESIS_STALE_SEC", 1800))


def reap_stale_processing_jobs() -> list[dict]:
    """Fail genesis imports wedged in 'processing' past the stale cutoff.

    Normal failures flip a job to 'failed' through mark_failed. This catches the
    worker/plaintext daemon dying mid-LLM-call, which would otherwise leave the
    job 'processing' forever — blocking the user's agent spawn. Goes through
    service.mark_failed so the genesis_state blob also flips terminal. Mirrors
    history-import's stale reaper. One bad reap doesn't stop the rest.
    """
    stale_sec = _genesis_stale_sec()
    error = f"genesis_stale_timeout:{stale_sec}s"
    reaped: list[dict] = []
    # The DB flips status processing->failed atomically, conditional on the row
    # being STILL processing AND STILL past the cutoff (inside the UPDATE), so we
    # can't race a job another worker just heartbeated or completed. It returns
    # the rows it actually flipped; we then best-effort sync each one's
    # genesis_state blob. The job is already failed in the DB, so a blob-sync
    # hiccup doesn't un-reap it — it still counts as reaped.
    for job in db.genesis_reap_stale_processing_jobs(stale_sec, error=error):
        user_id = str(job.get("user_id") or "")
        job_id = str(job.get("job_id") or "")
        if not user_id or not job_id:
            continue
        store = get_store(user_id)
        try:
            service.write_genesis_state(store, job, status="failed")
        except Exception as e:  # noqa: BLE001
            print(f"[genesis:reaper] blob sync failed for {user_id}/{job_id}: {type(e).__name__}:{str(e)[:120]}")
        _trace_genesis(
            store,
            "genesis.worker.stale_reaped",
            job_id=job_id,
            status="error",
            summary="stale genesis processing job reaped",
            detail={"error": error},
        )
        reaped.append({"user_id": user_id, "job_id": job_id})
    return reaped


def tick(
    *,
    api_url: str,
    enclave_url: str,
    mint_runtime_token: Callable,
    max_jobs: int = 1,
    now=None,
) -> dict:
    """Process up to max_jobs uploaded genesis jobs once.

    ``mint_runtime_token(user_id, scopes=...)`` must return a short-lived token
    carrying ``envelope_decrypt`` and ``genesis`` scopes. ``now`` is accepted for
    supervisor/test symmetry; this implementation only reports elapsed time.
    """
    start = time.time() if now is None else float(now())
    jobs = db.genesis_claim_uploaded_jobs(limit=max_jobs)
    results: list[dict] = []
    failed = 0
    for job in jobs:
        user_id = str(job.get("user_id") or "")
        job_id = str(job.get("job_id") or "")
        try:
            results.append(_process_job(
                job,
                api_url=api_url,
                enclave_url=enclave_url,
                mint_runtime_token=mint_runtime_token,
            ))
        except Exception as e:  # noqa: BLE001
            failed += 1
            if user_id and job_id:
                store = get_store(user_id)
                _trace_genesis(
                    store,
                    "genesis.worker.failed",
                    job_id=job_id,
                    status="error",
                    summary="genesis worker job failed",
                    detail={"reason": f"{type(e).__name__}:{str(e)[:180]}"},
                )
                service.mark_failed(store, job_id, f"worker_failed:{type(e).__name__}:{str(e)[:180]}")
            results.append({"user_id": user_id, "job_id": job_id, "status": "failed", "error": str(e)[:240]})
    end = time.time() if now is None else float(now())
    return {
        "claimed": len(jobs),
        "processed": len([item for item in results if item.get("status") == "done"]),
        "failed": failed,
        "results": results,
        "elapsed_ms": max(0, int((end - start) * 1000)),
    }
