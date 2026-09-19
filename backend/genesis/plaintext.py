"""Genesis plaintext-import pipeline helpers (framework-neutral).

The Flask ``genesis.routes`` blueprint was deleted in the ASGI cutover; the
native ``genesis.routes_asgi`` router and the plaintext-import worker call
these helpers directly. No Flask here — every function takes already-parsed
args + the store."""

from __future__ import annotations

import json
import math
import os
import re
import socket
import threading
import time
import uuid
from dataclasses import replace
from datetime import date
from typing import Any

import db
import debug_trace
import distillation_ledger
import provider_client
from core import envelope as core_envelope
from genesis import checkpoint, service, worker
from genesis.llm_client import GenesisLLMClient
from hosted import config_store as hosted_config_store
from hosted import history_import
from identity import service as identity_service
from identity.user_naming import sanitize_user_name
from memory import garden_import
from notices import core as notices_core

_SECONDS_PER_DAY = 24 * 60 * 60
_PLAINTEXT_SOURCE_ORDER = (
    history_import._AI_PERSONA_SOURCE,
    history_import._USER_PROFILE_SOURCE,
    history_import._MEMORY_SUMMARY_SOURCE,
    history_import._HISTORY_SOURCE,
)
_PLAINTEXT_SUPPORT_SOURCE_FAMILIES = {
    history_import._AI_PERSONA_SOURCE,
    history_import._USER_PROFILE_SOURCE,
    history_import._MEMORY_SUMMARY_SOURCE,
}
_PLAINTEXT_MODES = {"onboarding", "add_memory", "update_identity"}
MATERIAL_EMPTY_ERROR = "material_empty"
_MATERIAL_KIND_BY_FAMILY = {
    "ai_persona": "ai_persona",
    "user_profile": "user_profile",
    "memory_summary": "memory_summary",
    "history": "chat_history",
}
_FAST_DISTILL_MODEL_BY_PROVIDER = {
    "anthropic": "claude-haiku-4-5",
    "deepseek": "deepseek-v4-flash",
    "gemini": "gemini-flash-lite-latest",
    "openai": "gpt-4o-mini",
    "openrouter": "anthropic/claude-haiku-4-5",
}
_PLAINTEXT_WORKER_HOST = socket.gethostname()
_PLAINTEXT_WORKER_PID = os.getpid()
_PLAINTEXT_WORKER_INSTANCE = uuid.uuid4().hex
_PLAINTEXT_WORKER_ID_LOCK = threading.Lock()


class MaterialEmptyError(ValueError):
    """No usable plaintext import material survived parsing/normalization."""

    def __init__(self, detail: str = "plaintext_import_empty"):
        super().__init__(detail)
        self.error = MATERIAL_EMPTY_ERROR
        self.detail = detail


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


def _plaintext_fresh_start_message() -> dict:
    return {
        "role": "user",
        "content": "Fresh start. No persona profile or previous chat history was provided.",
        "ts": None,
        "source": history_import._FRESH_START_SOURCE,
        "source_family": history_import._FRESH_START_SOURCE,
    }


def _plaintext_source_kind(history_messages: list[dict], support_messages: list[dict]) -> str:
    if history_messages:
        return history_import._HISTORY_SOURCE
    families = {
        history_import._import_source_family(str(m.get("source") or m.get("source_family") or ""))
        for m in support_messages
    }
    if len(families) == 1:
        family = next(iter(families))
        if family == history_import._AI_PERSONA_SOURCE:
            return "ai_persona"
        if family == history_import._USER_PROFILE_SOURCE:
            return "user_profile"
        if family == history_import._MEMORY_SUMMARY_SOURCE:
            return "memory_summary"
    return history_import._HISTORY_SOURCE


def _plaintext_mode_from_client_job_id(client_job_id: str) -> str:
    lowered = str(client_job_id or "").strip().lower()
    if lowered.startswith("garden-"):
        return "add_memory"
    if lowered.startswith("identity-"):
        return "update_identity"
    return "onboarding"


def _plaintext_mode(payload: dict, *, client_job_id: str) -> str:
    explicit = str(payload.get("mode") or "").strip().lower()
    if explicit in _PLAINTEXT_MODES:
        return explicit
    return _plaintext_mode_from_client_job_id(client_job_id)


def _plaintext_route_family(msg: dict) -> str:
    family = history_import._import_source_family(str(msg.get("source") or msg.get("source_family") or ""))
    if family in _PLAINTEXT_SUPPORT_SOURCE_FAMILIES:
        return family
    return history_import._HISTORY_SOURCE


def _plaintext_chunk_texts_for_messages(messages: list[dict], *, window_limit: int) -> list[str]:
    windows = history_import._build_transcript_windows(
        messages,
        max_chars=18000,
        max_windows=window_limit,
    )
    if len(windows) > window_limit:
        windows = history_import._select_evenly(windows, window_limit)
    return [
        str(window.get("text") or "").strip()
        for window in windows
        if str(window.get("text") or "").strip()
    ]


def _plaintext_source_groups(analysis_messages: list[dict], *, window_limit: int) -> list[dict]:
    buckets: dict[str, list[dict]] = {family: [] for family in _PLAINTEXT_SOURCE_ORDER}
    for msg in analysis_messages:
        if not isinstance(msg, dict):
            continue
        buckets.setdefault(_plaintext_route_family(msg), []).append(msg)

    groups: list[dict] = []
    for source_kind in _PLAINTEXT_SOURCE_ORDER:
        messages = buckets.get(source_kind) or []
        if not messages:
            continue
        chunk_texts = _plaintext_chunk_texts_for_messages(messages, window_limit=window_limit)
        if not chunk_texts:
            continue
        groups.append({
            "source_kind": source_kind,
            "source_family": worker._source_family(source_kind),
            "chunk_texts": chunk_texts,
            "message_count": len(messages),
        })
    return groups


def _foreground_history_cap() -> int:
    try:
        return max(1, int(os.environ.get("FEEDLING_GENESIS_FG_HISTORY_CAP", "8")))
    except (TypeError, ValueError):
        return 8


def _cap_foreground_history_chunks(source_groups: list[dict]) -> list[dict]:
    """前台用:只对 history 桶采样到 cap(_select_evenly);其它桶(人物卡/档案/长期记忆)全读。
    被砍的 history 块由后台补全,不影响身份(名字来自人物卡,全读)。每组同时保留
    全量窗口序号,让前台 checkpoint 可被未采样的后台 pass 正确复用。"""
    cap = _foreground_history_cap()
    out: list[dict] = []
    for g in source_groups:
        chunks = list(g.get("chunk_texts") or [])
        indexed_chunks = list(enumerate(chunks))
        if str(g.get("source_family") or "") == "history":
            if len(chunks) > cap:
                indexed_chunks = history_import._select_evenly(indexed_chunks, cap)
        out.append({
            **g,
            "chunk_texts": [chunk for _index, chunk in indexed_chunks],
            "_checkpoint_chunk_indices": [index for index, _chunk in indexed_chunks],
        })
    return out


def _plaintext_timeline_span_days(messages: list[dict]) -> int:
    timestamps: list[float] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        raw = msg.get("ts")
        if raw in (None, ""):
            continue
        try:
            ts = float(raw)
        except Exception:
            continue
        if math.isfinite(ts):
            timestamps.append(ts)
    if len(timestamps) < 2:
        return 0
    return int(max(0.0, max(timestamps) - min(timestamps)) // _SECONDS_PER_DAY)


def _plaintext_relationship_anchor(payload: dict, *, messages: list[dict]) -> dict:
    # Reuse the ORIGINAL relationship-start logic (history_import._relationship_start_from_import):
    # typed date -> use it; else the EARLIEST message timestamp; else today (fresh_start).
    # The genesis path previously reinvented this and left relationship_started_at BLANK
    # for the no-typed-date case, which fell through to prefer_memory (genesis' today-dated
    # core memories) and collapsed 相处天数 to 0.
    start, _evidence = history_import._relationship_start_from_import(payload, messages)
    if not start:
        return {"relationship_started_at": "", "days_with_user": 0, "relationship_anchor_evidence": ""}
    iso = start.isoformat()
    return {
        "relationship_started_at": iso,
        "days_with_user": max(0, (date.today() - start).days),
        "relationship_anchor_evidence": f"plaintext_import:relationship_started_at={iso}",
    }


def _prepare_plaintext_import(payload: dict) -> dict:
    content = str(payload.get("content") or "")
    fmt = str(payload.get("format") or "auto").strip().lower()
    warnings: list[str] = []
    history_messages = history_import._parse_import_history_content(content, fmt, warnings)
    support_messages = history_import._persona_support_messages(payload)
    if not history_messages and not support_messages:
        if not bool(payload.get("fresh_start")):
            raise MaterialEmptyError(
                "content, ai_persona_content, character_content, personal_profile_content, "
                "memory_summary_content, persona_content, or fresh_start=true required"
            )
        support_messages = [_plaintext_fresh_start_message()]
        warnings.append("fresh_start_without_support_material")

    analysis_messages = support_messages + history_messages
    profile = history_import._history_import_profile(
        history_messages,
        support_messages,
        content_chars=len(content),
    )
    window_limit = int(profile.get("total_windows") or 8)
    source_groups = _plaintext_source_groups(analysis_messages, window_limit=window_limit)
    chunk_texts = [
        text
        for group in source_groups
        for text in (group.get("chunk_texts") or [])
        if str(text or "").strip()
    ]
    if not chunk_texts:
        raise MaterialEmptyError("plaintext_import_empty")
    timeline_span_days = _plaintext_timeline_span_days(history_messages)
    relationship_anchor = _plaintext_relationship_anchor(payload, messages=history_messages)
    return {
        "analysis_messages": analysis_messages,
        "chunk_texts": chunk_texts,
        "content_bytes": len(content.encode("utf-8")),
        "history_messages": history_messages,
        "profile": profile,
        "relationship_anchor": relationship_anchor,
        "source_kind": _plaintext_source_kind(history_messages, support_messages),
        "source_groups": source_groups,
        "source_stats": history_import._import_source_stats(analysis_messages),
        "support_messages": support_messages,
        "timeline_span_days": timeline_span_days,
        "warnings": warnings,
    }


_STAGED_TTL_DEFAULT_SEC = 259200  # 72h


def _staged_ttl_sec() -> int:
    """How long a staged upload stays retryable.

    Retry re-commits the SAME staged_id, so this TTL is exactly the window in
    which the retry button can still work; past it `load_genesis_staged_payload`
    deletes the blob and commit answers 410. 24h was too short for the shape we
    actually see — a long import fails while the user is away, and they come
    back the next evening to a button that can only fail
    (usr_3b73f1cb0a9ec975, 2026-08-06). 72h covers "came back a day or two
    later" while staying bounded: `create_genesis_staged_payload` reaps a user's
    previous stage and a DONE job consumes its own, so only failed/abandoned
    imports hold a blob at all — at most one per account."""
    try:
        return max(60, int(os.environ.get(
            "FEEDLING_GENESIS_STAGED_TTL_SEC", str(_STAGED_TTL_DEFAULT_SEC))))
    except (TypeError, ValueError):
        return _STAGED_TTL_DEFAULT_SEC


def _plaintext_stale_sec() -> int:
    try:
        return max(60, int(os.environ.get("FEEDLING_GENESIS_PLAINTEXT_STALE_SEC", "120")))
    except (TypeError, ValueError):
        return 120


def _plaintext_heartbeat_sec() -> int:
    try:
        return max(5, min(
            int(os.environ.get("FEEDLING_GENESIS_PLAINTEXT_HEARTBEAT_SEC", "15")),
            60,
        ))
    except (TypeError, ValueError):
        return 15


def _voice_checkpoint_enabled() -> bool:
    return os.environ.get("FEEDLING_GENESIS_VOICE_CHECKPOINT_ENABLED", "1") != "0"


def _plaintext_worker_metadata() -> dict[str, Any]:
    global _PLAINTEXT_WORKER_HOST, _PLAINTEXT_WORKER_PID, _PLAINTEXT_WORKER_INSTANCE
    current_pid = os.getpid()
    if current_pid != _PLAINTEXT_WORKER_PID:
        with _PLAINTEXT_WORKER_ID_LOCK:
            if current_pid != _PLAINTEXT_WORKER_PID:
                _PLAINTEXT_WORKER_HOST = socket.gethostname()
                _PLAINTEXT_WORKER_PID = current_pid
                _PLAINTEXT_WORKER_INSTANCE = uuid.uuid4().hex
    return {
        "plaintext_worker_host": _PLAINTEXT_WORKER_HOST,
        "plaintext_worker_pid": _PLAINTEXT_WORKER_PID,
        "plaintext_worker_instance": _PLAINTEXT_WORKER_INSTANCE,
    }


def _plaintext_owner_process_is_dead(metadata: dict) -> bool:
    """Return true only when the persisted owner is certainly dead locally.

    A different host can be a live worker during a rolling deploy, so it must
    age out through the heartbeat lease. On the same host, a missing PID is an
    authoritative crash signal and lets the replacement process recover now.
    """
    _plaintext_worker_metadata()
    owner_instance = str(metadata.get("plaintext_worker_instance") or "")
    owner_host = str(metadata.get("plaintext_worker_host") or "")
    try:
        owner_pid = int(metadata.get("plaintext_worker_pid") or 0)
    except (TypeError, ValueError):
        return False
    if not owner_instance or owner_instance == _PLAINTEXT_WORKER_INSTANCE:
        return False
    if not owner_host or owner_host != _PLAINTEXT_WORKER_HOST or owner_pid <= 0:
        return False
    if os.name != "posix":
        return False
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def _plaintext_checkpoint_bytes(user_id: str, job_id: str) -> int:
    try:
        blob = db.get_blob_strict(
            user_id,
            service._checkpoint_blob_kind(job_id),
        )
        if not isinstance(blob, dict):
            return 0
        return max(0, int(blob.get("checkpoint_bytes") or 0))
    except Exception:
        return 0


def _plaintext_interruption_progress(job: dict) -> tuple[int, int]:
    output = job.get("output") if isinstance(job.get("output"), dict) else {}
    materials = output.get("materials") if isinstance(output.get("materials"), list) else []
    windows_done = 0
    windows_total = 0
    for material in materials:
        if not isinstance(material, dict):
            continue
        try:
            windows_done += max(0, int(material.get("windows_done") or 0))
            windows_total += max(0, int(material.get("windows_total") or 0))
        except (TypeError, ValueError):
            continue
    return windows_done, windows_total


def _fail_stale_plaintext_job(store, job: dict | None) -> dict | None:
    row = job if isinstance(job, dict) else {}
    if str(row.get("status") or "") != "processing":
        return None
    metadata = _metadata_for_job(row)
    if str(metadata.get("ingest") or "") != "plaintext":
        return None
    stale_sec = _plaintext_stale_sec()
    owner_instance = str(metadata.get("plaintext_worker_instance") or "")
    owner_dead = _plaintext_owner_process_is_dead(metadata)
    elapsed_sec = service._claimed_age_sec(row)
    if owner_dead:
        interruption_cause = "owner_pid_dead"
    elif elapsed_sec is not None and elapsed_sec >= stale_sec:
        interruption_cause = "heartbeat_aged"
    else:
        interruption_cause = "unknown"
    failed = db.genesis_fail_stale_plaintext_job(
        store.user_id,
        str(row.get("job_id") or ""),
        older_than_sec=stale_sec,
        error=(
            "plaintext_worker_restarted"
            if owner_dead
            else f"plaintext_stale_timeout:{stale_sec}s"
        ),
        expected_worker_instance=owner_instance,
        force=owner_dead,
    )
    if failed:
        try:
            service.write_genesis_state(store, failed, status=service.FAILED_JOB_STATUS)
        except Exception:  # noqa: BLE001
            pass
        windows_done, windows_total = _plaintext_interruption_progress(failed)
        failed_metadata = _metadata_for_job(failed)
        job_id = str(failed.get("job_id") or row.get("job_id") or "")
        _trace_genesis(
            store,
            "genesis.plaintext.interrupted",
            job_id=job_id,
            status="error",
            summary="plaintext genesis worker interrupted",
            detail={
                "cause": interruption_cause,
                "windows_done": windows_done,
                "windows_total": windows_total,
                "elapsed_sec": max(0, int(elapsed_sec or 0)),
                "history_tier": str(failed_metadata.get("history_tier") or "")[:80],
                "distill_model": str(failed_metadata.get("distill_model") or "")[:160],
                "checkpoint_bytes": _plaintext_checkpoint_bytes(store.user_id, job_id),
            },
        )
    return failed


def _estimate_plaintext_materials(prepared: dict) -> tuple[list[dict], int]:
    materials: list[dict] = []
    for group in prepared.get("source_groups") or []:
        if not isinstance(group, dict):
            continue
        chunks = [str(text) for text in group.get("chunk_texts") or [] if str(text or "").strip()]
        if not chunks:
            continue
        family = str(group.get("source_family") or "history")
        # Conservative arithmetic: input chars / 3.5 plus prompt and maximum
        # output reservations for every real map window.
        input_tokens = math.ceil(sum(len(text) for text in chunks) / 3.5)
        estimated = input_tokens + len(chunks) * (1200 + 2400)
        materials.append({
            "kind": _MATERIAL_KIND_BY_FAMILY.get(family, family),
            "windows": len(chunks),
            "est_tokens": estimated,
        })
    return materials, sum(item["est_tokens"] for item in materials)


def _queued_plaintext_materials(source_groups: list[dict]) -> list[dict]:
    materials: list[dict] = []
    for group in source_groups:
        if not isinstance(group, dict):
            continue
        total = len([
            text for text in group.get("chunk_texts") or []
            if str(text or "").strip()
        ])
        if not total:
            continue
        family = str(group.get("source_family") or "history")
        materials.append({
            "kind": _MATERIAL_KIND_BY_FAMILY.get(family, family),
            "status": "queued",
            "windows_done": 0,
            "windows_total": total,
            "cards": 0,
        })
    return materials


def _recommended_distill_model(store, api_key: str | None) -> str | None:
    runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
    if isinstance(runtime, tuple):
        return None
    provider = provider_client.normalize_provider(runtime.provider)
    if provider in _FAST_DISTILL_MODEL_BY_PROVIDER:
        return _FAST_DISTILL_MODEL_BY_PROVIDER[provider]
    if provider != "openai_compatible":
        return None
    try:
        catalog = provider_client.list_provider_models(
            provider, runtime.api_key, runtime.base_url, total_budget_sec=3.0)
    except Exception:
        return None
    ids = [
        str(item.get("id") or "")
        for item in catalog.get("models") or []
        if (
            isinstance(item, dict)
            and re.fullmatch(
                r"[A-Za-z0-9._/:-]+", str(item.get("id") or "")
            ) is not None
            and "thinking" not in str(item.get("id") or "").lower()
            and not _openai_compatible_gemini_flash(str(item.get("id") or ""))
        )
    ]
    for needle in ("haiku", "flash", "mini"):
        match = next((model_id for model_id in ids if needle in model_id.lower()), "")
        if match:
            return match
    return None


def _openai_compatible_gemini_flash(model_id: str) -> bool:
    normalized = str(model_id or "").strip().lower()
    return "gemini" in normalized and "flash" in normalized


def _distill_model_override(value: Any) -> str:
    model = str(value or "").strip()
    if not model:
        return ""
    if len(model) > 160 or any(ord(char) < 32 for char in model):
        raise ValueError("invalid_distill_model")
    return model


def _material_card_count(output: dict | None) -> int:
    value = output if isinstance(output, dict) else {}
    cards = value.get("memories")
    if not isinstance(cards, list):
        cards = value.get("facts")
    count = len(cards) if isinstance(cards, list) else 0
    if _identity_payload_has_content(value.get("identity")):
        count += 1
    if value.get("persona") or value.get("persona_content"):
        count += 1
    return count


def _plaintext_job_metadata(
    payload: dict,
    prepared: dict,
    *,
    client_job_id: str,
    input_hash: str,
    mode: str,
    staged_id: str = "",
) -> dict:
    profile = prepared.get("profile") if isinstance(prepared.get("profile"), dict) else {}
    source_stats = prepared.get("source_stats") if isinstance(prepared.get("source_stats"), dict) else {}
    metadata: dict[str, Any] = {
        "ingest": "plaintext",
        "input_hash": input_hash,
        # Staged payload backing this job; consumed on DONE so a failed job's retry
        # can reuse it (see service.consume_staged_for_completed_job). Sourced from a
        # trusted caller arg (plaintext_commit after it loaded the stage), NOT from the
        # client payload — the public /plaintext direct-import path never sets it.
        "staged_id": str(staged_id or ""),
        "client_job_id": client_job_id,
        "mode": mode if mode in _PLAINTEXT_MODES else "onboarding",
        "history_tier": str(profile.get("tier") or "small"),
        "window_count": len(prepared.get("chunk_texts") or []),
        "history_count": int(profile.get("message_count") or 0),
        "timeline_span_days": int(prepared.get("timeline_span_days") or 0),
        "support_count": int(profile.get("support_count") or 0),
        "warning_count": len(prepared.get("warnings") or []),
        "content_bytes": int(prepared.get("content_bytes") or 0),
    }
    filename_fields = [
        payload.get("history_filename"),
        payload.get("ai_persona_filename"),
        payload.get("character_filename") or payload.get("character_card_filename"),
        payload.get("personal_profile_filename") or payload.get("persona_filename"),
        payload.get("memory_summary_filename") or payload.get("memory_sample_filename"),
    ]
    metadata["file_count"] = len([x for x in filename_fields if str(x or "").strip()])
    for prefix, family in (
        ("ai_persona", history_import._AI_PERSONA_SOURCE),
        ("user_profile", history_import._USER_PROFILE_SOURCE),
        ("memory_summary", history_import._MEMORY_SUMMARY_SOURCE),
        ("fresh_start", history_import._FRESH_START_SOURCE),
    ):
        stats = source_stats.get(family) if isinstance(source_stats.get(family), dict) else {}
        metadata[f"{prefix}_count"] = int(stats.get("count") or 0)
    return metadata


def _metadata_for_job(job: dict | None) -> dict:
    metadata = (job or {}).get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _metadata_plaintext_mode(metadata: dict) -> str:
    mode = str(metadata.get("mode") or "").strip().lower()
    if mode in _PLAINTEXT_MODES:
        return mode
    return _plaintext_mode_from_client_job_id(str(metadata.get("client_job_id") or ""))


def _plaintext_map_task_id(source_pass: int, source_family: str) -> str:
    return f"plaintext-map:{source_pass}:{source_family}"


def _plaintext_voice_task_id(source_pass: int, source_family: str) -> str:
    return f"plaintext-voice:{source_pass}:{source_family}"


class _PlaintextCheckpointProgress:
    """Encrypted fact/voice map checkpoint plus the non-content progress projection."""

    def __init__(self, store, api_key: str | None, job_id: str, source_groups: list[dict],
                 *, use_garden: bool = False):
        self.store = store
        self.api_key = api_key
        self.job_id = job_id
        self.source_groups = source_groups
        loaded = service.load_genesis_checkpoint(store, api_key, job_id)
        reset_detail = None
        if use_garden and _checkpoint_is_legacy(loaded):
            # Failed jobs can be found again by input_hash. Their old map progress
            # is not an import-session checkpoint; restart from the submitted
            # material and let the garden's existing-card index reconcile writes.
            old_phase = loaded.get("phase")
            reset_detail = {
                "reason": "legacy_progress",
                "engine": garden_import.ENGINE,
                "old_phase": old_phase if isinstance(old_phase, str) and old_phase in checkpoint.PHASES else "unknown",
                **{name: len(loaded[name]) if isinstance(loaded.get(name), (dict, list)) else 0
                   for name in ("map_outputs", "tasks", "voice_outputs", "material_cards")},
            }
            loaded = None
        self.doc = checkpoint.resume(loaded) if loaded else checkpoint.new_checkpoint()
        # ``use_garden`` 只对写记忆卡的模式（onboarding / add_memory）为 True；
        # update_identity 不写卡，进度照旧按窗口任务算。
        self.legacy = (not use_garden) or _checkpoint_is_legacy(self.doc)
        if not self.legacy:
            self.doc["import_engine"] = garden_import.ENGINE
        self.doc.setdefault("map_outputs", {})
        if _voice_checkpoint_enabled():
            self.doc.setdefault("voice_outputs", {})
        service.write_genesis_checkpoint(store, job_id, self.doc)
        if reset_detail is not None:
            _trace_genesis(store, "genesis.plaintext.legacy_checkpoint_reset",
                           job_id=job_id, detail=reset_detail)
        self.publish(stage="plaintext_reducer")

    # -- memgarden 导入会话的进度（加密 checkpoint 里的 ``garden_import``） ------ #

    def garden_state(self, *, locale: str, user_name: str) -> dict:
        """这个 job 的导入进度。第一次调用定下 locale/称呼/策略，续跑沿用存下的值。"""
        state = self.doc.get("garden_import")
        if not garden_import.is_state(state):
            state = garden_import.new_state(locale=locale, user_name=user_name)
            self.save_garden(state)
        return state

    def save_garden(self, state: dict) -> None:
        # 两段式候选、写到一半的卡都在里面（用户内容）—— 走同一个加密 checkpoint，
        # 不单独落明文。可见进度由调用方每批 publish 一次。
        self.doc["garden_import"] = state
        service.write_genesis_checkpoint(self.store, self.job_id, self.doc)

    def _task_id(self, source_pass: int, source_family: str) -> str:
        return _plaintext_map_task_id(source_pass, source_family)

    def resume_outputs(self, source_pass: int, source_family: str) -> dict[int, dict]:
        task_id = self._task_id(source_pass, source_family)
        outputs = self.doc.get("map_outputs") if isinstance(self.doc.get("map_outputs"), dict) else {}
        resumed: dict[int, dict] = {}
        for idx in range(len(self.source_groups[source_pass - 1].get("chunk_texts") or [])):
            key = checkpoint.task_key(task_id, idx)
            value = outputs.get(key)
            if checkpoint.is_task_done(self.doc, task_id, idx) and isinstance(value, dict):
                resumed[idx] = value
        return resumed

    def resume_voice_outputs(self, source_pass: int, source_family: str) -> dict[int, dict]:
        if not _voice_checkpoint_enabled():
            return {}
        task_id = _plaintext_voice_task_id(source_pass, source_family)
        outputs = (
            self.doc.get("voice_outputs")
            if isinstance(self.doc.get("voice_outputs"), dict)
            else {}
        )
        resumed: dict[int, dict] = {}
        for idx in range(len(self.source_groups[source_pass - 1].get("chunk_texts") or [])):
            key = checkpoint.task_key(task_id, idx)
            value = outputs.get(key)
            if checkpoint.is_task_done(self.doc, task_id, idx) and isinstance(value, dict):
                resumed[idx] = value
        return resumed

    def record_voice(
        self,
        source_pass: int,
        source_family: str,
        chunk_index: int,
        output: dict,
    ) -> None:
        if not _voice_checkpoint_enabled():
            return
        task_id = _plaintext_voice_task_id(source_pass, source_family)
        key = checkpoint.task_key(task_id, chunk_index)
        outputs = dict(self.doc.get("voice_outputs") or {})
        outputs[key] = output
        self.doc["voice_outputs"] = outputs
        candidate_count = sum(
            len(output.get(name) or [])
            for name in ("behavior_notes_candidates", "exemplar_candidates")
            if isinstance(output.get(name), list)
        )
        self.doc = checkpoint.upsert_task(
            self.doc,
            task_id=task_id,
            chunk_id=chunk_index,
            status=checkpoint.TASK_DONE,
            source_pass=str(source_pass),
            output_summary=f"voice_candidates={candidate_count}",
        )
        service.write_genesis_checkpoint(self.store, self.job_id, self.doc)

    def record_map(self, source_pass: int, source_family: str, chunk_index: int, output: dict) -> None:
        task_id = self._task_id(source_pass, source_family)
        key = checkpoint.task_key(task_id, chunk_index)
        outputs = dict(self.doc.get("map_outputs") or {})
        outputs[key] = output
        self.doc["map_outputs"] = outputs
        self.doc = checkpoint.upsert_task(
            self.doc,
            task_id=task_id,
            chunk_id=chunk_index,
            status=checkpoint.TASK_DONE,
            source_pass=str(source_pass),
            output_summary=f"candidates={len(output.get('fact_candidates') or [])}",
        )
        # Durable checkpoint first, visible progress second. A crash can under-report
        # completed work, but can never report a window that cannot be resumed.
        service.write_genesis_checkpoint(self.store, self.job_id, self.doc)
        self.publish(
            stage="plaintext_reducer",
            source_family=source_family,
            source_pass=source_pass,
        )

    def record_map_diagnostics(
        self, source_pass: int, source_family: str, diagnostics: list[dict]
    ) -> None:
        if not diagnostics:
            return
        existing = (
            self.doc.get("map_diagnostics")
            if isinstance(self.doc.get("map_diagnostics"), list)
            else []
        )
        safe = list(existing)
        for raw in diagnostics:
            if not isinstance(raw, dict) or len(safe) >= 6:
                break
            safe.append({
                "source_pass": max(1, int(source_pass)),
                "source_family": str(source_family or "")[:80],
                "chunk_index": max(0, int(raw.get("chunk_index") or 0)),
                "task_id": str(raw.get("task_id") or "")[:120],
                "discard_reason": str(raw.get("discard_reason") or "unknown")[:120],
                "raw_output_snippet": str(raw.get("raw_output_snippet") or "")[:500],
                "raw_output_chars": max(0, int(raw.get("raw_output_chars") or 0)),
                "raw_output_truncated": bool(raw.get("raw_output_truncated")),
            })
        self.doc["map_diagnostics"] = safe
        service.write_genesis_checkpoint(self.store, self.job_id, self.doc)
        self.publish(
            stage="plaintext_reducer",
            source_family=source_family,
            source_pass=source_pass,
        )

    def record_non_map_group(
        self, source_pass: int, source_family: str, *, cards: int = 0
    ) -> None:
        """Mark direct-reduce source windows complete after that reduce succeeds."""
        task_id = self._task_id(source_pass, source_family)
        total = len(self.source_groups[source_pass - 1].get("chunk_texts") or [])
        for chunk_index in range(total):
            if checkpoint.is_task_done(self.doc, task_id, chunk_index):
                continue
            self.doc = checkpoint.upsert_task(
                self.doc,
                task_id=task_id,
                chunk_id=chunk_index,
                status=checkpoint.TASK_DONE,
                source_pass=str(source_pass),
                output_summary="direct_reduce_complete",
            )
        material_cards = dict(self.doc.get("material_cards") or {})
        material_cards[task_id] = max(0, int(cards))
        self.doc["material_cards"] = material_cards
        service.write_genesis_checkpoint(self.store, self.job_id, self.doc)
        self.publish(
            stage="plaintext_reducer",
            source_family=source_family,
            source_pass=source_pass,
        )

    def _garden_materials(self, *, active_pass: int = 0) -> list[dict]:
        state = self.doc.get("garden_import") if isinstance(self.doc.get("garden_import"), dict) else {}
        sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
        materials: list[dict] = []
        for source_pass, group in enumerate(self.source_groups, start=1):
            family = str(group.get("source_family") or "history")
            total = len(group.get("chunk_texts") or [])
            suffix = f":{source_pass}:{family}"
            done = cards = 0
            for key, entry in sessions.items():
                if not str(key).endswith(suffix) or not isinstance(entry, dict):
                    continue
                done += garden_import.entry_windows_done(entry)[0]
                cards += int(entry.get("cards_written") or 0)
            done = min(done, total)
            if total and done >= total:
                status = "done"
            elif source_pass == active_pass or done:
                status = "processing"
            else:
                status = "queued"
            materials.append({
                "kind": _MATERIAL_KIND_BY_FAMILY.get(family, family),
                "status": status,
                "windows_done": done,
                "windows_total": total,
                "cards": cards,
            })
        return materials

    def materials(self, *, active_pass: int = 0) -> list[dict]:
        if not self.legacy:
            return self._garden_materials(active_pass=active_pass)
        materials: list[dict] = []
        outputs = self.doc.get("map_outputs") if isinstance(self.doc.get("map_outputs"), dict) else {}
        material_cards = self.doc.get("material_cards") if isinstance(self.doc.get("material_cards"), dict) else {}
        for source_pass, group in enumerate(self.source_groups, start=1):
            family = str(group.get("source_family") or "history")
            task_id = self._task_id(source_pass, family)
            total = len(group.get("chunk_texts") or [])
            done = sum(
                1 for idx in range(total)
                if checkpoint.is_task_done(self.doc, task_id, idx)
            )
            if total and done >= total:
                status = "done"
            elif source_pass == active_pass or done:
                status = "processing"
            else:
                status = "queued"
            cards = int(material_cards.get(task_id) or 0)
            if not cards:
                cards = sum(
                    len((outputs.get(checkpoint.task_key(task_id, idx)) or {}).get("fact_candidates") or [])
                    for idx in range(total)
                    if isinstance(outputs.get(checkpoint.task_key(task_id, idx)), dict)
                )
            materials.append({
                "kind": _MATERIAL_KIND_BY_FAMILY.get(family, family),
                "status": status,
                "windows_done": done,
                "windows_total": total,
                "cards": cards,
            })
        return materials

    def mark_identity_ready(self) -> None:
        self.doc["identity_ready"] = True
        service.write_genesis_checkpoint(self.store, self.job_id, self.doc)

    def processed_chunks(self) -> int:
        return sum(item["windows_done"] for item in self.materials())

    def publish(
        self,
        *,
        stage: str,
        source_family: str = "",
        source_pass: int = 0,
        status: str = "processing",
        extra: dict[str, Any] | None = None,
    ) -> None:
        output: dict[str, Any] = {
            "stage": stage,
            "materials": self.materials(active_pass=source_pass),
            "identity_ready": bool(self.doc.get("identity_ready")),
        }
        diagnostics = self.doc.get("map_diagnostics")
        if isinstance(diagnostics, list) and diagnostics:
            output["map_diagnostics"] = diagnostics[:6]
        if source_family:
            output["source_family"] = source_family
        if source_pass:
            output["source_pass"] = source_pass
            output["source_pass_total"] = len(self.source_groups)
        if extra:
            output.update(extra)
        db.genesis_set_job_status(
            self.store.user_id,
            self.job_id,
            status=status,
            output=output,
            processed_chunks=self.processed_chunks(),
        )


def _checkpoint_is_legacy(doc: dict | None) -> bool:
    """旧流水线开始过、还没跑完的 job：没有引擎标记，但已经有 fact_map/voice 进度。

    只看「有没有进度」而不是「有没有 checkpoint」—— checkpoint 在 job 一开始就会写一个
    空的，那种还什么都没做的 job 直接走新引擎，不需要为它保留旧路径。"""
    if not isinstance(doc, dict):
        return False
    if str(doc.get("import_engine") or "") == garden_import.ENGINE:
        return False
    return bool(doc.get("map_outputs") or doc.get("tasks") or doc.get("voice_outputs")
                or doc.get("material_cards"))


def _find_reusable_plaintext_job(
    store,
    *,
    client_job_id: str,
    input_hash: str,
    mode: str,
) -> dict | None:
    try:
        jobs = db.genesis_list_jobs(store.user_id, limit=100)
    except Exception:
        return None
    for job in jobs:
        metadata = _metadata_for_job(job)
        if metadata.get("ingest") != "plaintext":
            continue
        if _metadata_plaintext_mode(metadata) != mode:
            continue
        if client_job_id and str(metadata.get("client_job_id") or "") == client_job_id:
            return job
        if input_hash and str(metadata.get("input_hash") or "") == input_hash:
            return job
    return None


def _plaintext_identity_name(identity: dict | None) -> str:
    if not isinstance(identity, dict):
        return ""
    name = str(identity.get("agent_name") or "").strip()
    return name[:80]


def _plaintext_identity_dimensions(identity: dict | None) -> list[dict]:
    if not isinstance(identity, dict):
        return []
    dims = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    return [dim for dim in dims if isinstance(dim, dict)][:7]


def _plaintext_positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _plaintext_memory_key(item: dict) -> str:
    return "|".join([
        re.sub(r"\s+", " ", str(item.get("type") or "")).strip().lower(),
        re.sub(r"\s+", " ", str(item.get("summary") or item.get("title") or "")).strip().lower()[:500],
        re.sub(r"\s+", " ", str(item.get("content") or item.get("description") or "")).strip().lower()[:1000],
    ])


def _plaintext_merge_memories(outputs: list[dict]) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    source_families = {
        str(output.get("source_family") or "").strip()
        for output in outputs
        if str(output.get("source_family") or "").strip()
    }
    annotate_source_family = len(source_families) > 1
    for output in outputs:
        source_family = str(output.get("source_family") or "").strip()
        raw_items = output.get("memories")
        if raw_items is None:
            raw_items = output.get("facts")
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            key = _plaintext_memory_key(item)
            if key in seen:
                continue
            seen.add(key)
            merged_item = dict(item)
            if annotate_source_family and source_family and not str(merged_item.get("_source_family") or "").strip():
                merged_item["_source_family"] = source_family
            merged.append(merged_item)
    return merged


def _plaintext_merge_voice_workset(outputs: list[dict]) -> dict:
    notes: list[str] = []
    exemplars: list[dict] = []
    seen_notes: set[str] = set()
    seen_exemplars: set[str] = set()
    for output in outputs:
        if str(output.get("source_family") or "") == "user_profile":
            continue
        workset = output.get("voice_workset") if isinstance(output.get("voice_workset"), dict) else {}
        for note in workset.get("behavior_notes") if isinstance(workset.get("behavior_notes"), list) else []:
            clean = re.sub(r"\s+", " ", str(note or "").strip())
            if not clean or clean in seen_notes:
                continue
            seen_notes.add(clean)
            notes.append(clean)
        for exemplar in workset.get("exemplars") if isinstance(workset.get("exemplars"), list) else []:
            if not isinstance(exemplar, dict):
                continue
            key = json.dumps(exemplar, ensure_ascii=False, sort_keys=True, default=str)[:2000]
            if key in seen_exemplars:
                continue
            seen_exemplars.add(key)
            exemplars.append(exemplar)
    if not notes and not exemplars:
        return {}
    return {
        "behavior_notes": notes[:16],
        "exemplars": exemplars[:80],
    }


def _plaintext_merge_reducer_outputs(outputs: list[dict], *, relationship_anchor: dict | None = None) -> dict:
    relationship_anchor = relationship_anchor if isinstance(relationship_anchor, dict) else {}
    usable_identity_outputs = [
        output for output in outputs
        if str(output.get("source_family") or "") != "user_profile"
        and isinstance(output.get("identity"), dict)
    ]

    def first_identity_name(*families: str) -> str:
        for family in families:
            for output in usable_identity_outputs:
                if str(output.get("source_family") or "") != family:
                    continue
                name = _plaintext_identity_name(output.get("identity"))
                if name:
                    return name
        return ""

    def first_identity_dims(*families: str) -> list[dict]:
        for family in families:
            for output in usable_identity_outputs:
                if str(output.get("source_family") or "") != family:
                    continue
                dims = _plaintext_identity_dimensions(output.get("identity"))
                if dims:
                    return dims
        return []

    agent_name = first_identity_name("ai_persona", "history", "memory_summary")
    dimensions = first_identity_dims("ai_persona", "history")

    # B2: the 4 user-layer fields (user_preferred_name/custom_persona_prompt/
    # relationship_anchor/stable_definitions) are the
    # OPPOSITE of the TA-identity firewall above — they describe the USER, so
    # (unlike agent_name/dimensions) they are read from ALL outputs INCLUDING
    # source_family=="user_profile", never excluded by it. See
    # genesis/worker.py's _USER_LAYER_STRING_FIELDS / _USER_LAYER_LIST_FIELD
    # for the shared field list this mirrors.
    all_identity_outputs = [output for output in outputs if isinstance(output.get("identity"), dict)]
    user_layer: dict[str, str] = {}
    for key in ("user_preferred_name", "custom_persona_prompt", "relationship_anchor"):
        for output in all_identity_outputs:
            value = str(output["identity"].get(key) or "").strip()
            if value:
                user_layer[key] = value
                break
    stable_definitions: list[str] = []
    for output in all_identity_outputs:
        defs = output["identity"].get("stable_definitions")
        if isinstance(defs, list):
            stable_definitions.extend(str(item).strip() for item in defs if str(item or "").strip())
    stable_definitions = list(dict.fromkeys(stable_definitions))[:12]

    identity: dict[str, Any] = {}
    if agent_name or dimensions:
        identity["agent_name"] = agent_name
        identity["dimensions"] = dimensions
    identity.update(user_layer)
    if stable_definitions:
        identity["stable_definitions"] = stable_definitions

    persona: dict = {}
    for output in outputs:
        if str(output.get("source_family") or "") == "user_profile":
            continue
        candidate = output.get("persona") if isinstance(output.get("persona"), dict) else {}
        if str(candidate.get("content") or "").strip():
            persona = candidate

    voice_workset = _plaintext_merge_voice_workset(outputs)
    voice = {
        "behavior_notes_count": len(voice_workset.get("behavior_notes") or []),
        "exemplar_count": len(voice_workset.get("exemplars") or []),
        "founding_exemplar_count": len([
            item for item in (voice_workset.get("exemplars") or [])
            if isinstance(item, dict) and item.get("founding")
        ]),
    }
    if not voice_workset:
        for output in reversed(outputs):
            candidate = output.get("voice") if isinstance(output.get("voice"), dict) else {}
            if candidate:
                voice = candidate
                break

    output_days = max(_plaintext_positive_int(output.get("days_with_user")) for output in outputs) if outputs else 0
    days = _plaintext_positive_int(relationship_anchor.get("days_with_user")) or output_days
    evidence = str(relationship_anchor.get("relationship_anchor_evidence") or "").strip()
    if not evidence:
        evidence = " | ".join(
            str(output.get("relationship_anchor_evidence") or "").strip()
            for output in outputs
            if str(output.get("relationship_anchor_evidence") or "").strip()
        )[:500]

    source_families = [str(output.get("source_family") or "") for output in outputs if str(output.get("source_family") or "")]
    merged: dict[str, Any] = {
        "memories": _plaintext_merge_memories(outputs),
        "source_kind": "plaintext_multi_source" if len(source_families) > 1 else str((outputs[0] if outputs else {}).get("source_kind") or "history_import"),
        "source_family": "merged" if len(set(source_families)) > 1 else (source_families[0] if source_families else "history"),
        "voice": voice,
        "days_with_user": days,
    }
    if identity:
        merged["identity"] = identity
    if evidence:
        merged["relationship_anchor_evidence"] = evidence
    if str(relationship_anchor.get("relationship_started_at") or "").strip():
        merged["relationship_started_at"] = str(relationship_anchor.get("relationship_started_at") or "").strip()
    if persona:
        merged["persona"] = persona
    if voice_workset:
        merged["voice_workset"] = voice_workset
    return merged


def _plaintext_existing_persona_from_output(output: dict) -> dict:
    persona = output.get("persona") if isinstance(output.get("persona"), dict) else {}
    content = str(persona.get("content") or "").strip()
    if not content:
        return {}
    return {
        "content": content,
        "source_family": str(persona.get("source_family") or output.get("source_family") or ""),
    }


def _plaintext_existing_voice_from_output(output: dict) -> dict:
    workset = output.get("voice_workset") if isinstance(output.get("voice_workset"), dict) else {}
    if not workset:
        return {}
    return {
        "behavior_notes": workset.get("behavior_notes") if isinstance(workset.get("behavior_notes"), list) else [],
        "exemplars": workset.get("exemplars") if isinstance(workset.get("exemplars"), list) else [],
    }


def _plaintext_persona_material_from_messages(messages: list[dict] | None) -> str:
    chunks: list[str] = []
    seen: set[str] = set()
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        source = history_import._import_source_family(str(msg.get("source") or msg.get("source_family") or ""))
        if source != history_import._AI_PERSONA_SOURCE:
            continue
        content = str(msg.get("content") or "").strip()
        if not content or content in seen:
            continue
        seen.add(content)
        chunks.append(content)
    return "\n\n".join(chunks).strip()


def _plaintext_existing_voice_workset_for_update(store, api_key: str | None) -> dict:
    try:
        blob = db.get_blob(store.user_id, service.GENESIS_VOICE_BLOB)
        if not isinstance(blob, dict):
            return {}
        envelope = blob.get("content_envelope")
        if not isinstance(envelope, dict):
            return {}
        raw = core_envelope.read_envelope_body(
            envelope,
            api_key,
            purpose="genesis_voice",
            caller_user_id=str(store.user_id),
        )
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            return {}
        notes = parsed.get("behavior_notes") if isinstance(parsed.get("behavior_notes"), list) else []
        exemplars = parsed.get("exemplars") if isinstance(parsed.get("exemplars"), list) else []
        return {"behavior_notes": notes, "exemplars": exemplars}
    except Exception:
        return {}


def _plaintext_existing_identity_for_update(store, api_key: str | None) -> dict:
    """Decrypt the current identity card so update_identity can 部分补全 (merge:
    keep fields the new material doesn't address). Best-effort — on any failure
    return {} so the job falls back to the old fresh-derive behavior."""
    try:
        blob = identity_service._load_identity(store)
        if not isinstance(blob, dict):
            return {}
        shape = core_envelope.classify_envelope_shape(blob)
        if shape in ("plaintext_text", "plaintext_binary"):
            raw = core_envelope.read_plaintext_envelope_body(
                blob, owner_user_id=store.user_id)
        else:
            raw = core_envelope.read_envelope_body(
                blob,
                api_key,
                purpose="identity_update_merge",
                caller_user_id=str(store.user_id),
            )
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _plaintext_user_profile_messages(source_groups: list[dict]) -> list[dict]:
    profiles: list[dict] = []
    for group in source_groups:
        if not isinstance(group, dict):
            continue
        family = worker._source_family(
            str(group.get("source_family") or group.get("source_kind") or "")
        )
        if family != "user_profile":
            continue
        for text in group.get("chunk_texts") if isinstance(group.get("chunk_texts"), list) else []:
            content = str(text or "").strip()
            if content:
                profiles.append({
                    "role": "user",
                    "content": content,
                    "source": history_import._USER_PROFILE_SOURCE,
                })
    return profiles


def _resolve_plaintext_user_name(
    store,
    api_key: str | None,
    runtime,
    source_groups: list[dict],
) -> str:
    """Resolve once: encrypted Identity Card first, strict User Profile second."""
    existing = _plaintext_existing_identity_for_update(store, api_key)
    name = sanitize_user_name(existing.get("user_preferred_name"))
    if name != "TA":
        return name
    profiles = _plaintext_user_profile_messages(source_groups)
    if not profiles:
        return "TA"
    try:
        return history_import._extract_import_user_name_with_provider(runtime, profiles)
    except Exception:
        return "TA"


def _attach_plaintext_user_name(output: dict, user_name: str) -> dict:
    """Attach a real preferred name to an identity output without inventing one."""
    name = sanitize_user_name(user_name)
    if name == "TA" or not isinstance(output, dict):
        return output
    identity = output.get("identity") if isinstance(output.get("identity"), dict) else {}
    if not identity:
        return output
    identity = dict(identity)
    identity["user_preferred_name"] = name
    output["identity"] = identity
    return output


def _attach_plaintext_profile(
    store,
    api_key: str | None,
    job_id: str,
    *,
    runtime,
    output: dict,
    key_prefix: str,
    llm: GenesisLLMClient | None,
) -> dict:
    """Generate MEMORY/STYLE from this pass only, before any output lands."""
    del api_key
    rendered_cards, _source_count, memory_material = (
        service.render_genesis_profile_source(output)
    )
    output.update(worker.build_profile_output_from_sources(
        user_id=store.user_id,
        job_id=job_id,
        key_prefix=key_prefix,
        runtime=runtime,
        rendered_cards=rendered_cards,
        memory_material=memory_material,
        output=output,
        llm=llm,
    ))
    return output


def _write_back_plaintext_user_name(
    store, api_key: str | None, user_name: str, *, job_id: str,
) -> str:
    """Best-effort preferred-name merge for paths that intentionally skip identity."""
    name = sanitize_user_name(user_name)
    if name == "TA":
        return "not_provided"
    existing = _plaintext_existing_identity_for_update(store, api_key)
    if not existing:
        return "not_provided"
    if sanitize_user_name(existing.get("user_preferred_name")) == name:
        return "unchanged"
    payload = dict(existing)
    payload["user_preferred_name"] = name
    with distillation_ledger.ArtifactAttempt(store, job_id, "identity") as attempt:
        try:
            outcome = service.replace_identity_preserving_anchor(
                store, {"identity": payload}, api_key
            )
        except Exception:
            attempt.finish("write_failed")
            return "write_failed"
        attempt.finish(outcome)
        return outcome


def _plaintext_existing_persona_for_update(store, api_key: str | None) -> str:
    """Decrypt the current genesis persona so update_identity can merge (旧 persona
    + 新材料) instead of rebuilding from the new material alone. Best-effort — on
    any failure return "" so persona falls back to the old rebuild behavior."""
    try:
        blob = db.get_blob(store.user_id, service.GENESIS_PERSONA_BLOB)
        if not isinstance(blob, dict):
            return ""
        envelope = blob.get("content_envelope")
        if not isinstance(envelope, dict):
            return ""
        raw = core_envelope.read_envelope_body(
            envelope,
            api_key,
            purpose="genesis_persona",
            caller_user_id=str(store.user_id),
        )
        return raw.decode("utf-8")
    except Exception:
        return ""


def _merged_has_identity(merged: dict) -> bool:
    """True when the reduce output carries a usable Identity Card (a name or any
    dimension). Mirrors service._identity_payload_from_output's emptiness rule."""
    ident = merged.get("identity") if isinstance(merged.get("identity"), dict) else {}
    dims = ident.get("dimensions") if isinstance(ident.get("dimensions"), list) else []
    return bool(str(ident.get("agent_name") or "").strip()) or len(dims) > 0


def _identity_payload_has_content(identity_payload: dict | None) -> bool:
    payload = identity_payload if isinstance(identity_payload, dict) else {}
    if str(payload.get("agent_name") or "").strip():
        return True
    if str(payload.get("self_introduction") or "").strip():
        return True
    if str(payload.get("category") or "").strip():
        return True
    dimensions = payload.get("dimensions") if isinstance(payload.get("dimensions"), list) else []
    if dimensions:
        return True
    signature = payload.get("signature") if isinstance(payload.get("signature"), list) else []
    return bool(signature)


def _provider_identity_failure(warnings: list[str] | tuple[str, ...] | None) -> str:
    for warning in warnings or []:
        text = str(warning or "")
        if text.startswith("provider_identity_failed:"):
            return text
    return ""


def _complete_plaintext_v2_job(
    store,
    job_id: str,
    *,
    progress: _PlaintextCheckpointProgress | None,
    memory_action_count: int,
    identity_status: str,
    persona_ref: str,
    persona_sha256: str,
) -> None:
    output: dict[str, Any] = {
        "stage": "genesis_v2_done",
        "identity_ready": True,
    }
    if progress:
        materials = progress.materials()
        incomplete = [
            item for item in materials
            if int(item.get("windows_done") or 0) != int(item.get("windows_total") or 0)
        ]
        if incomplete:
            raise RuntimeError("genesis_v2_incomplete_material_windows")
        output["materials"] = materials
    completed = db.genesis_complete_job(
        store.user_id,
        job_id,
        output=output,
        memory_action_count=memory_action_count,
        identity_status=identity_status,
        persona_ref=persona_ref,
        persona_sha256=persona_sha256,
    )
    if completed:
        service.write_genesis_state(store, completed, status=service.DONE_JOB_STATUS)


    # Foreground readiness already resolved stale failure notices. Do not resolve
    # again here: later stages may add a fresh partial notice in future revisions.


def _append_plaintext_onboarding_greeting(
    store,
    *,
    job_id: str,
    runtime,
    analysis_messages: list[dict],
    memories: list[dict],
    identity_payload: dict,
    days: int,
    language: str,
    fresh_start: bool = False,
) -> str:
    try:
        greeting_text, _warnings = history_import._generate_model_api_onboarding_greeting(
            runtime,
            analysis_messages,
            memories,
            identity_payload,
            days,
            language,
        )
    except Exception:
        greeting_text = ""
    if not str(greeting_text or "").strip():
        # Fallback copy must match the relationship: a fresh_start user has
        # never met the agent — "好久不见" would read as mistaken identity.
        # Imports keep the reunion copy.
        if fresh_start:
            greeting_text = (
                "你好，很高兴认识你。我现在还没有名字，你想以后怎么称呼我？"
                if str(language).startswith("zh")
                else "Hi, it's really nice to meet you. I don't have a name yet — what would you like to call me?"
            )
        else:
            greeting_text = (
                "好久不见，很高兴又能和你聊天。"
                if str(language).startswith("zh")
                else "Good to see you again — I'm glad we can talk."
            )
    with distillation_ledger.ArtifactAttempt(store, job_id, "greeting") as attempt:
        try:
            history_import._append_model_api_onboarding_greeting(store, greeting_text)
        except Exception as e:  # noqa: BLE001 — greeting stays best-effort
            attempt.finish("write_failed")
            print(f"[genesis:{getattr(store, 'user_id', '')}] onboarding greeting append failed: {type(e).__name__}:{str(e)[:160]}")
            return ""
        attempt.finish("written")
    return str(greeting_text or "")


def _run_plaintext_update_identity_job(
    store,
    api_key: str | None,
    job_id: str,
    *,
    runtime,
    analysis_messages: list[dict] | None,
    relationship_anchor: dict | None = None,
    user_name: str = "",
    llm: GenesisLLMClient | None = None,
    progress: _PlaintextCheckpointProgress | None = None,
) -> str | None:
    msgs = analysis_messages if isinstance(analysis_messages, list) else []
    language = history_import._import_language_for_store(store, msgs)
    # 部分补全:merge onto the current card so fields the upload doesn't mention are
    # kept (not re-derived to empty). Best-effort decrypt; {} => old fresh-derive.
    existing_identity = _plaintext_existing_identity_for_update(store, api_key)
    identity_payload, warnings = history_import._derive_identity_with_provider(
        runtime,
        msgs,
        [],
        0,
        language,
        existing_identity=existing_identity,
    )
    provider_failure = _provider_identity_failure(warnings)
    if provider_failure:
        service.mark_failed(store, job_id, f"update_identity_failed:{provider_failure}")
        return
    if not _identity_payload_has_content(identity_payload):
        service.mark_failed(store, job_id, "identity_update_empty")
        return
    if sanitize_user_name(user_name) != "TA":
        identity_payload["user_preferred_name"] = sanitize_user_name(user_name)
    persona_material = _plaintext_persona_material_from_messages(msgs)
    if not persona_material:
        service.mark_failed(store, job_id, "persona_material_required")
        return
    voice_workset = _plaintext_existing_voice_workset_for_update(store, api_key)
    existing_persona = _plaintext_existing_persona_for_update(store, api_key)
    try:
        persona_output = worker.build_persona_output_from_material(
            user_id=store.user_id,
            job_id=job_id,
            key_prefix=f"{job_id}:update_identity",
            runtime=runtime,
            persona_material=persona_material,
            voice_workset=voice_workset,
            source_kind="identity_update",
            source_family="ai_persona",
            existing_persona=existing_persona,
            user_name=user_name,
            llm=llm,
        )
    except Exception as e:  # noqa: BLE001
        service.mark_failed(
            store, job_id, f"persona_rebuild_failed:{type(e).__name__}:{str(e)[:160]}", exc=e,
        )
        return
    _attach_plaintext_profile(
        store,
        api_key,
        job_id,
        runtime=runtime,
        output=persona_output,
        key_prefix=f"{job_id}:update_identity",
        llm=llm,
    )
    identity_field_lock = service.identity_field_lock_for_job(store, job_id)
    with distillation_ledger.ArtifactAttempt(store, job_id, "identity") as attempt:
        status = service.replace_identity_preserving_anchor(
            store,
            {"identity": identity_payload, "relationship_anchor": relationship_anchor or {}},
            api_key,
            field_lock=identity_field_lock,
        )
        attempt.finish(status)
    if status not in {"initialized", "updated", "locked"}:
        service.mark_failed(store, job_id, status)
        return
    try:
        persona_ref, persona_sha = service.write_persona_artifact(
            store, job_id, persona_output
        )
    except Exception as e:  # noqa: BLE001
        service.mark_failed(
            store, job_id, f"persona_write_failed:{type(e).__name__}:{str(e)[:160]}", exc=e,
        )
        return
    profile_ref, profile_sha, profile_status = service.write_profile_artifact(
        store,
        job_id,
        persona_output,
        api_key,
    )
    if progress:
        for source_pass, group in enumerate(progress.source_groups, start=1):
            family = str(group.get("source_family") or "ai_persona")
            progress.record_non_map_group(source_pass, family, cards=2)
    completed = db.genesis_complete_job(
        store.user_id,
        job_id,
        output={
            "stage": "plaintext_update_identity_done",
            "identity_field_lock": identity_field_lock,
            "profile_ref": profile_ref,
            "profile_sha256": profile_sha,
            "profile_status": profile_status,
        },
        memory_action_count=0,
        identity_status=status,
        persona_ref=persona_ref,
        persona_sha256=persona_sha,
    )
    if completed:
        service.write_genesis_state(store, completed, status=service.DONE_JOB_STATUS)
        if progress:
            progress.mark_identity_ready()
            progress.publish(stage="plaintext_update_identity_done", status=service.DONE_JOB_STATUS)
        # this update_identity path never goes through service.apply_reducer_output
        # (which resolves genesis notices on the other completion paths) -> resolve
        # here too, so a prior genesis_failed notice from an earlier failed attempt
        # doesn't linger past a successful retry. Safe: this function never emits a
        # "genesis:...:partial" notice of its own (identity-only, not memory), and
        # every mark_failed exit above returns immediately, so this branch and the
        # failure branches are mutually exclusive within a single run.
        notices_core.resolve(store, "genesis:")
    return status


def _run_plaintext_genesis_job(
    store,
    api_key: str | None,
    job_id: str,
    *,
    mode: str = "onboarding",
    chunk_texts: list[str] | None = None,
    source_kind: str = history_import._HISTORY_SOURCE,
    source_groups: list[dict] | None = None,
    relationship_anchor: dict | None = None,
    analysis_messages: list[dict] | None = None,
) -> None:
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_run_plaintext_job_heartbeat,
        args=(store, job_id, heartbeat_stop),
        name=f"genesis-plaintext-heartbeat-{job_id[:24]}",
        daemon=True,
    )
    heartbeat_thread.start()
    started_at = time.time()
    group_count = len(source_groups) if isinstance(source_groups, list) else 0
    chunk_count = len(chunk_texts or [])
    _trace_genesis(
        store,
        "genesis.plaintext.started",
        job_id=job_id,
        summary="plaintext genesis job started",
        detail={"mode": mode, "source_kind": source_kind, "source_groups": group_count, "chunk_count": chunk_count},
    )
    try:
        if source_groups is None:
            source_groups = [{
                "source_kind": source_kind,
                "source_family": worker._source_family(source_kind),
                "chunk_texts": list(chunk_texts or []),
                "message_count": 0,
            }]
        source_groups = [
            group for group in source_groups
            if isinstance(group, dict) and group.get("chunk_texts")
        ]
        if not source_groups:
            raise MaterialEmptyError("plaintext_import_empty")

        job = db.genesis_get_job(store.user_id, job_id)
        runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
        if isinstance(runtime, tuple):
            _, err = runtime
            raise RuntimeError(json.dumps(err, ensure_ascii=False))
        job_metadata = job.get("metadata") if isinstance((job or {}).get("metadata"), dict) else {}
        distill_model = _distill_model_override(job_metadata.get("distill_model"))
        if distill_model:
            runtime = replace(runtime, model=distill_model)
        _trace_genesis(
            store,
            "genesis.plaintext.runtime.loaded",
            job_id=job_id,
            summary="runtime config loaded",
            detail={"mode": mode},
        )
        llm = GenesisLLMClient(canary=True)
        progress = _PlaintextCheckpointProgress(
            store, api_key, job_id, source_groups,
            use_garden=mode in {"onboarding", "add_memory"},
        )
        user_name = _resolve_plaintext_user_name(
            store, api_key, runtime, source_groups
        )

        if mode == "add_memory":
            from genesis import plaintext_garden

            _trace_genesis(store, "genesis.plaintext.add_memory.started", job_id=job_id,
                           summary="add memory job started", detail={"engine": garden_import.ENGINE})
            plaintext_garden.run_add_memory(
                store, api_key, job_id, runtime=runtime, source_groups=source_groups,
                relationship_anchor=relationship_anchor, analysis_messages=analysis_messages,
                user_name=user_name, llm=llm, progress=progress)
            _trace_genesis(store, "genesis.plaintext.done", job_id=job_id, summary="add memory job done",
                           detail={"mode": mode, "engine": garden_import.ENGINE},
                           dur_ms=(time.time() - started_at) * 1000)
            return
        if mode == "onboarding":
            from genesis import plaintext_garden

            path = plaintext_garden.run_onboarding(
                store, api_key, job_id, runtime=runtime, source_groups=source_groups,
                relationship_anchor=relationship_anchor, analysis_messages=analysis_messages,
                user_name=user_name, llm=llm, progress=progress)
            _trace_genesis(store, "genesis.plaintext.done", job_id=job_id, summary="plaintext genesis job done",
                           detail={"mode": mode, "engine": garden_import.ENGINE,
                                   "genesis_v2": path == "genesis_v2"},
                           dur_ms=(time.time() - started_at) * 1000)
            return
        if mode == "update_identity":
            _trace_genesis(store, "genesis.plaintext.update_identity.started", job_id=job_id,
                           summary="update identity job started")
            identity_status = _run_plaintext_update_identity_job(
                store,
                api_key,
                job_id,
                runtime=runtime,
                analysis_messages=analysis_messages,
                relationship_anchor=relationship_anchor,
                user_name=user_name,
                llm=llm,
                progress=progress,
            )
            _trace_genesis(store, "genesis.plaintext.done", job_id=job_id, summary="update identity job done",
                           detail={"mode": mode, "identity_status": identity_status or ""},
                           dur_ms=(time.time() - started_at) * 1000)
            return

    except Exception as e:  # noqa: BLE001
        _trace_genesis(
            store,
            "genesis.plaintext.failed",
            job_id=job_id,
            status="error",
            summary="plaintext genesis job failed",
            detail={"mode": mode, "reason": f"{type(e).__name__}:{str(e)[:180]}"},
            dur_ms=(time.time() - started_at) * 1000,
        )
        service.mark_failed(
            store, job_id, f"plaintext_import_failed:{type(e).__name__}:{str(e)[:220]}", exc=e,
        )
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)
        try:
            terminal_job = db.genesis_get_job(store.user_id, job_id)
            if str((terminal_job or {}).get("status") or "") == service.DONE_JOB_STATUS:
                service.delete_genesis_checkpoint(store, job_id)
                # Unified terminal chokepoint for every mode (V1 / V2 / add_memory /
                # update_identity): release the staged materials only on success.
                service.consume_staged_for_completed_job(store, job_id, job=terminal_job)
        except Exception:
            pass


def _run_plaintext_job_heartbeat(store, job_id: str, stop_event) -> None:
    while not stop_event.wait(_plaintext_heartbeat_sec()):
        try:
            worker_instance = _plaintext_worker_metadata()["plaintext_worker_instance"]
            db.genesis_touch_plaintext_job(
                store.user_id,
                job_id,
                worker_instance=worker_instance,
            )
        except Exception:  # noqa: BLE001
            pass


def _start_plaintext_genesis_job(
    store,
    api_key: str | None,
    job: dict,
    *,
    mode: str = "onboarding",
    chunk_texts: list[str],
    source_kind: str,
    source_groups: list[dict] | None = None,
    relationship_anchor: dict | None = None,
    analysis_messages: list[dict] | None = None,
) -> bool:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return False
    thread = threading.Thread(
        target=_run_plaintext_genesis_job,
        args=(store, api_key, job_id),
        kwargs={
            "mode": mode,
            "chunk_texts": chunk_texts,
            "source_kind": source_kind,
            "source_groups": source_groups,
            "relationship_anchor": relationship_anchor,
            "analysis_messages": analysis_messages,
        },
        name=f"genesis-plaintext-{job_id[:24]}",
        daemon=True,
    )
    thread.start()
    return True


# --------------------------------------------------------------------------- #
# HTTP surface — thin Flask adapters over ``genesis.genesis_core`` (plan §5.3).
# Each parses the Flask request + resolves auth/scope/credentials exactly as
# before, then delegates to the framework-neutral core so the ASGI router
# (``genesis.routes_asgi``) returns byte-identical bodies. The plaintext helper
# cluster + background machinery below stay here (tests patch them as
# ``routes._…``); the plaintext route injects them so the enqueue mechanism is
# the SAME on both frameworks.
# --------------------------------------------------------------------------- #
