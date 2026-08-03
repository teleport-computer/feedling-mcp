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
import provider_client
from core import enclave as core_enclave
from genesis import checkpoint, dedup, foreground, foreground_identity, lightweight_identity, service, worker
from genesis.llm_client import GenesisLLMClient
from hosted import config_store as hosted_config_store
from hosted import history_import
from identity import service as identity_service
from identity.user_naming import sanitize_user_name
from notices import catalog
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


def _staged_ttl_sec() -> int:
    try:
        return max(60, int(os.environ.get("FEEDLING_GENESIS_STAGED_TTL_SEC", "86400")))
    except (TypeError, ValueError):
        return 86400


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
        if isinstance(item, dict) and "thinking" not in str(item.get("id") or "").lower()
    ]
    for needle in ("haiku", "flash", "mini"):
        match = next((model_id for model_id in ids if needle in model_id.lower()), "")
        if match:
            return match
    return None


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
) -> dict:
    profile = prepared.get("profile") if isinstance(prepared.get("profile"), dict) else {}
    source_stats = prepared.get("source_stats") if isinstance(prepared.get("source_stats"), dict) else {}
    metadata: dict[str, Any] = {
        "ingest": "plaintext",
        "input_hash": input_hash,
        "client_job_id": client_job_id,
        "mode": mode if mode in _PLAINTEXT_MODES else "onboarding",
        "history_tier": str(profile.get("tier") or "small"),
        "window_count": len(prepared.get("chunk_texts") or []),
        "history_count": int(profile.get("message_count") or 0),
        "timeline_span_days": int(prepared.get("timeline_span_days") or 0),
        "support_count": int(profile.get("support_count") or 0),
        "warning_count": len(prepared.get("warnings") or []),
        "content_bytes": int(prepared.get("content_bytes") or 0),
        **_plaintext_worker_metadata(),
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


class _PlaintextCheckpointProgress:
    """Encrypted map checkpoint plus the non-content progress projection."""

    def __init__(self, store, api_key: str | None, job_id: str, source_groups: list[dict]):
        self.store = store
        self.api_key = api_key
        self.job_id = job_id
        self.source_groups = source_groups
        loaded = service.load_genesis_checkpoint(store, api_key, job_id)
        self.doc = checkpoint.resume(loaded) if loaded else checkpoint.new_checkpoint()
        self.doc.setdefault("map_outputs", {})
        service.write_genesis_checkpoint(store, job_id, self.doc)
        self.publish(stage="plaintext_reducer")

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

    def materials(self, *, active_pass: int = 0) -> list[dict]:
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

    # B2: the 5 user-layer fields (user_preferred_name/custom_persona_prompt/
    # language_preference/relationship_anchor/stable_definitions) are the
    # OPPOSITE of the TA-identity firewall above — they describe the USER, so
    # (unlike agent_name/dimensions) they are read from ALL outputs INCLUDING
    # source_family=="user_profile", never excluded by it. See
    # genesis/worker.py's _USER_LAYER_STRING_FIELDS / _USER_LAYER_LIST_FIELD
    # for the shared field list this mirrors.
    all_identity_outputs = [output for output in outputs if isinstance(output.get("identity"), dict)]
    user_layer: dict[str, str] = {}
    for key in ("user_preferred_name", "custom_persona_prompt", "language_preference", "relationship_anchor"):
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
        raw = core_enclave._decrypt_envelope_via_enclave(envelope, api_key, purpose="genesis_voice")
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
        if not isinstance(blob, dict) or not blob.get("body_ct"):
            return {}
        raw = core_enclave._decrypt_envelope_via_enclave(blob, api_key, purpose="identity_update_merge")
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


def _write_back_plaintext_user_name(store, api_key: str | None, user_name: str) -> str:
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
    try:
        return service.replace_identity_preserving_anchor(store, {"identity": payload}, api_key)
    except Exception:
        return "write_failed"


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
        raw = core_enclave._decrypt_envelope_via_enclave(envelope, api_key, purpose="genesis_persona")
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


def _run_plaintext_genesis_v2(
    store,
    api_key: str | None,
    job_id: str,
    *,
    runtime,
    source_groups: list[dict],
    relationship_anchor: dict | None = None,
    analysis_messages: list[dict] | None = None,
    user_name: str = "",
    llm: GenesisLLMClient | None = None,
    progress: _PlaintextCheckpointProgress | None = None,
) -> bool:
    """Genesis v2 foreground-fast orchestration (behind FEEDLING_GENESIS_V2_ENABLED).

    Foreground restores the legacy chat_ready contract: pick 3-5 core memories, derive a
    REAL Identity Card via the existing hosted deriver (foreground_identity, no new
    prompt), write identity + relationship anchor (_store_identity_payload) + a greeting,
    and publish identity_ready — so the app can enter with a named/anchored TA, never a
    blank home. Background then does the heavy full reduce over every remaining window,
    skipping the core and NOT re-writing identity. The job reaches done only after every
    material window has a durable checkpoint.

    Edge: if the deriver can't produce an identity, fall back to the v1-style apply
    (complete on core + background fills identity) — never a fake-complete.

    Returns True when it handled the job. Returns False only when there's nothing to work
    with (no core), so the caller runs the v1 full path instead.
    """
    # Foreground only: cap the history bucket to a small, evenly-sampled window so large
    # imports stay fast (support buckets — ai_persona/user_profile/memory_summary — are
    # never sampled here, since identity/name lives in the character card). `source_groups`
    # itself is left untouched — background enrichment below still consumes the full,
    # un-sampled groups so the dropped history chunks get fully processed there.
    fg_source_groups = _cap_foreground_history_chunks(source_groups)

    # primary group: prefer the real chat history (best greeting signal), else the first
    fg_group = next(
        (g for g in fg_source_groups if str(g.get("source_family") or "") == "history"),
        fg_source_groups[0],
    )
    fg_idx = fg_source_groups.index(fg_group) + 1
    fg_kind = str(fg_group.get("source_kind") or history_import._HISTORY_SOURCE)
    fg_family = str(fg_group.get("source_family") or worker._source_family(fg_kind))

    db.genesis_set_job_status(
        store.user_id, job_id, status="processing",
        output={"stage": "genesis_v2_foreground", "source_family": fg_family}, processed_chunks=0,
    )
    msgs = analysis_messages if isinstance(analysis_messages, list) else []
    # fresh_start-only = every analysis message is the synthetic sentinel (routed
    # into the history bucket by _plaintext_route_family, so group source_kind
    # can't tell it apart from real history — the message `source` can).
    # isinstance INSIDE the all() condition, never as an `if` filter — filtering
    # would make non-dict junk (["bad"]) vacuously pass as all([]) is True and
    # mis-route it into the fresh_start carve-out.
    fresh_start_only = bool(msgs) and all(
        isinstance(m, dict)
        and str(m.get("source") or "") == history_import._FRESH_START_SOURCE
        for m in msgs
    )
    # Computed BEFORE the foreground loop, and combined-map is disabled outright
    # for sentinel-only input: with the flag on (test compose sets it), the loop
    # would otherwise extract voice candidates from the sentinel and
    # build_voice_persona_output_from_candidates would distill persona from
    # synthetic text — the same "never derive anything from the sentinel" rule
    # the identity skip below enforces. Gating the flag here also keeps
    # include_voice_candidates off, not just the final artifact write.
    combined_map = worker.genesis_combined_map_enabled() and not fresh_start_only
    foreground_reduces: list[dict] = []
    primary_reduce: dict | None = None
    voice_candidates: list[dict] = []
    persona_material_parts: list[str] = []
    for idx, group in enumerate(fg_source_groups, start=1):
        group_kind = str(group.get("source_kind") or history_import._HISTORY_SOURCE)
        group_family = str(group.get("source_family") or worker._source_family(group_kind))
        raw_group_chunks = list(group.get("chunk_texts") or [])
        checkpoint_indices = group.get("_checkpoint_chunk_indices")
        if (
            not isinstance(checkpoint_indices, list)
            or len(checkpoint_indices) != len(raw_group_chunks)
            or not all(isinstance(value, int) for value in checkpoint_indices)
        ):
            checkpoint_indices = list(range(len(raw_group_chunks)))
        indexed_group_chunks = [
            (full_index, str(text))
            for full_index, text in zip(checkpoint_indices, raw_group_chunks)
            if str(text or "").strip()
        ]
        if not indexed_group_chunks:
            continue
        checkpoint_indices = [full_index for full_index, _text in indexed_group_chunks]
        group_chunks = [text for _full_index, text in indexed_group_chunks]
        resume_map_outputs = None
        on_map_completed = None
        if progress:
            completed = progress.resume_outputs(idx, group_family)
            resume_map_outputs = {
                local_index: completed[full_index]
                for local_index, full_index in enumerate(checkpoint_indices)
                if full_index in completed
            }

            def on_map_completed(
                chunk_index,
                output,
                *,
                source_pass=idx,
                family=group_family,
                full_indices=tuple(checkpoint_indices),
            ):
                progress.record_map(
                    source_pass,
                    family,
                    full_indices[chunk_index],
                    output,
                )

        if combined_map and group_family == "ai_persona":
            persona_material_parts.extend(group_chunks)
        reduce = worker.build_foreground_output_from_texts(
            user_id=store.user_id, job_id=job_id,
            key_prefix=f"{job_id}:source_pass:{idx}:{group_family}",
            runtime=runtime, chunk_texts=group_chunks, source_kind=group_kind,
            include_voice_candidates=combined_map,
            write_core=False,
            user_name=user_name,
            llm=llm,
            resume_map_outputs=resume_map_outputs,
            on_map_completed=on_map_completed,
        )
        foreground_reduces.append(reduce)
        voice_candidates.extend([c for c in (reduce.get("voice_candidates") or []) if isinstance(c, dict)])
        if idx == fg_idx:
            primary_reduce = reduce

    if not foreground_reduces:
        return False
    primary_reduce = primary_reduce or foreground_reduces[0]
    hw_total = int(primary_reduce.get("history_windows_total") or 0)
    hw_failed = int(primary_reduce.get("history_windows_failed") or 0)
    all_fact_candidates: list[dict] = []
    for reduce in foreground_reduces:
        candidates = reduce.get("all_fact_candidates") or reduce.get("core_fact_candidates") or []
        all_fact_candidates.extend([c for c in candidates if isinstance(c, dict)])

    core = primary_reduce.get("core_fact_candidates") or foreground.select_core_for_foreground(all_fact_candidates)
    if not core and not fresh_start_only:
        return False  # nothing to work with -> let the v1 full path handle it
    # fresh_start has no material BY DEFINITION, so `core` is always empty for it.
    # Falling back to v1 here would complete the job WITHOUT the onboarding
    # greeting (the v1 full path never greets) — the user would open an empty
    # chat. Continue instead: the nameless greeting+done branch below handles an
    # empty core, and _fact_write short-circuits on zero candidates.

    full_fact_write = worker.build_memory_output_from_fact_candidates(
        user_id=store.user_id,
        job_id=job_id,
        key_prefix=f"{job_id}:foreground_full",
        runtime=runtime,
        fact_candidates=all_fact_candidates,
        user_name=user_name,
        llm=llm,
    )
    fg_merged = _plaintext_merge_reducer_outputs(
        [{**primary_reduce, **full_fact_write}],
        relationship_anchor=relationship_anchor,
    )
    if combined_map:
        voice_persona_output = worker.build_voice_persona_output_from_candidates(
            user_id=store.user_id,
            job_id=job_id,
            key_prefix=f"{job_id}:foreground_voice_persona",
            runtime=runtime,
            voice_candidates=voice_candidates,
            existing_persona={"content": "\n\n".join(persona_material_parts).strip()} if persona_material_parts else None,
            user_name=user_name,
            llm=llm,
        )
        fg_merged = _plaintext_merge_reducer_outputs(
            [{**fg_merged, **voice_persona_output}],
            relationship_anchor=relationship_anchor,
        )
    _attach_plaintext_user_name(fg_merged, user_name)
    full_memories = fg_merged.get("memories") or []
    days = int((relationship_anchor or {}).get("days_with_user") or 0)
    # explicit relationship_started_at (user typed a date) -> honored verbatim below,
    # per the documented priority; blank -> _store_identity_payload falls back to memory.
    explicit_started_at = str((relationship_anchor or {}).get("relationship_started_at") or "").strip()
    language = history_import._import_language_for_store(store, msgs)

    # Foreground-ready contract: derive identity when the material contains a real
    # signal, but never invent one or make its absence a speaking gate. Greeting and
    # identity readiness happen before entry; full-memory completion stays in the
    # continuation. fresh_start skips the identity LLM entirely: deriving a
    # name from the synthetic sentinel is meaningless (a hallucinated one would be
    # wrong), and a provider hiccup there must not push a material-less onboarding
    # into the retryable-failed path — the greeting below must land regardless.
    if fresh_start_only:
        identity_payload, id_warnings = {"agent_name": "", "dimensions": []}, []
    else:
        identity_payload, id_warnings = foreground_identity.derive_foreground_identity(
            runtime=runtime, analysis_messages=msgs, core_memories=full_memories,
            days_with_user=days, language=language,
        )
    provider_failure = _provider_identity_failure(id_warnings)
    if provider_failure or not foreground_identity.has_identity_signal(identity_payload):
        # Non-LLM lightweight fallback: try to salvage a name from the uploaded
        # character card / profile text (never calls the LLM). Covers the common
        # real failure mode (provider hiccup) that used to hard-fail the job.
        support_texts = [str(m.get("content") or "") for m in msgs
                         if history_import._is_import_support_message(m)]
        lite = lightweight_identity.derive_from_support(
            support_texts, days_with_user=days, language=language)
        if lightweight_identity.has_signal(lite):
            identity_payload = lite
        else:
            # Only an explicit provider failure is retryable. Material that simply
            # contains no identity signal is a valid nameless onboarding result and
            # follows the same greeting + done path as fresh_start.
            if provider_failure:
                service.mark_failed(store, job_id, "onboarding_no_identity:provider_unstable")
                return True
    if sanitize_user_name(user_name) != "TA":
        identity_payload["user_preferred_name"] = sanitize_user_name(user_name)
    identity_first = bool(msgs) and foreground_identity.has_identity_signal(identity_payload)
    persona_ref = ""
    persona_sha = ""
    if combined_map and identity_first:
        persona_ref, persona_sha = service.write_persona_artifact(store, job_id, fg_merged)
        service.write_voice_artifact(store, job_id, fg_merged)

    if identity_first:
        # core memories now; identity via the legacy _store_identity_payload (exact old
        # path — writes the card + relationship anchor); greeting via the legacy pair.
        mem_count, _mr = service.apply_memory_outputs(store, api_key, {"memories": full_memories})
        history_import._store_identity_payload(
            store, identity_payload, days_with_user=days,
            evidence=f"genesis_foreground:{job_id}", language=language,
            relationship_started_at=explicit_started_at,
        )
        _append_plaintext_onboarding_greeting(
            store,
            runtime=runtime,
            analysis_messages=msgs,
            memories=full_memories,
            identity_payload=identity_payload,
            days=days,
            language=language,
            fresh_start=fresh_start_only,
        )
        identity_status = "initialized"
    else:
        # No foreground identity signal: enter nameless after greeting/core, then
        # let background enrichment add an identity later if the full material supports it.
        _append_plaintext_onboarding_greeting(
            store,
            runtime=runtime,
            analysis_messages=msgs,
            memories=full_memories,
            identity_payload=identity_payload,
            days=days,
            language=language,
            fresh_start=fresh_start_only,
        )
        foreground_result = service.apply_reducer_output(
            store,
            api_key,
            job_id,
            fg_merged,
            complete_job=False,
        ) or {}
        mem_count = int(foreground_result.get("memory_action_count") or 0)
        identity_status = str(foreground_result.get("identity_status") or "")
        persona_ref = str(foreground_result.get("persona_ref") or persona_ref)
        persona_sha = str(foreground_result.get("persona_sha256") or persona_sha)

    # The foreground has produced a usable entry state, but the import is not done
    # until the full source_groups set is checkpointed below.
    notices_core.resolve(store, "genesis:")

    if progress:
        progress.mark_identity_ready()
        progress.publish(
            stage="genesis_v2_foreground_ready",
            status="processing",
            extra={
                "history_windows_total": hw_total,
                "history_windows_failed": hw_failed,
            },
        )
    else:
        db.genesis_set_job_status(
            store.user_id,
            job_id,
            status="processing",
            output={
                "stage": "genesis_v2_foreground_ready",
                "identity_ready": True,
                "history_windows_total": hw_total,
                "history_windows_failed": hw_failed,
            },
        )

    completion = {
        "memory_action_count": mem_count,
        "identity_status": identity_status,
        "persona_ref": persona_ref,
        "persona_sha256": persona_sha,
    }

    if fresh_start_only:
        # No material by definition -> nothing to enrich. Background here would
        # re-reduce the pure sentinel as history with write_identity=True (prod
        # can be reached regardless of the combined-map deployment flag) —
        # inventing persona/identity from synthetic text the foreground
        # explicitly refuses to distill, plus wasted provider calls. The greeting
        # landed and there are no real material windows to enrich; complete now.
        _complete_plaintext_v2_job(store, job_id, progress=progress, **completion)
        return True

    # foreground core memory texts -> background as "already saved, don't repeat"
    # (semantic dedup of reworded twins lives in the model).
    core_memory_texts = [
        t for t in (str((m or {}).get("summary") or (m or {}).get("content") or "").strip()
                    for m in full_memories) if t
    ]

    # Background continuation consumes the full unsampled source groups. Combined-map
    # already produced voice/persona in the foreground, so its continuation only fills
    # memory windows; both modes must write the remaining memories.
    try:
        _run_plaintext_background_enrichment(
            store, api_key, job_id, runtime=runtime, source_groups=source_groups,
            relationship_anchor=relationship_anchor,
            skip_family=fg_family, skip_texts=foreground.core_skip_texts(core),
            known_memories=core_memory_texts, write_identity=not identity_first,
            include_memory=True,
            include_persona_voice=not combined_map,
            user_name=user_name,
            llm=llm,
            progress=progress,
            completion=completion,
        )
    except Exception as e:  # noqa: BLE001
        service.mark_failed(
            store,
            job_id,
            f"genesis_v2_background_failed:{type(e).__name__}:{str(e)[:220]}",
            exc=e,
        )
    return True


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


def _run_plaintext_background_enrichment(
    store,
    api_key: str | None,
    job_id: str,
    *,
    runtime,
    source_groups: list[dict],
    relationship_anchor: dict | None,
    skip_family: str,
    skip_texts: set[str],
    known_memories: list[str] | None = None,
    write_identity: bool = True,
    include_memory: bool = True,
    include_persona_voice: bool = True,
    user_name: str = "",
    llm: GenesisLLMClient | None = None,
    progress: _PlaintextCheckpointProgress | None = None,
    completion: dict[str, Any] | None = None,
) -> None:
    """Background continuation: the full reduce over every group (skipping the core the
    foreground already wrote for skip_family), then apply the REST incrementally —
    memories + persona + voice. With completion metadata, this is the only path that
    marks a material-backed v2 job done.

    Dedup is two-layered against the foreground core (`known_memories`): the model
    dedups reworded twins semantically inside fact_write (known_memories = "already
    saved, don't repeat"), and a CONSERVATIVE lexical backstop drops any near-identical
    survivor before apply. The lexical threshold is high on purpose — it must never
    merge two distinct same-template facts (美式/拿铁, 蛋子/金毛)."""
    known = [t for t in (str(x or "").strip() for x in (known_memories or [])) if t]
    reducer_outputs: list[dict] = []
    existing_persona: dict = {}
    existing_voice: dict = {}
    for idx, group in enumerate(source_groups, start=1):
        group_kind = str(group.get("source_kind") or history_import._HISTORY_SOURCE)
        group_family = str(group.get("source_family") or worker._source_family(group_kind))
        group_chunks = [str(t) for t in (group.get("chunk_texts") or []) if str(t or "").strip()]
        if not group_chunks:
            continue
        if progress:
            progress.publish(
                stage="genesis_v2_background",
                source_family=group_family,
                source_pass=idx,
                status="processing",
            )
        else:
            db.genesis_set_job_status(
                store.user_id, job_id, status="processing",
                output={"stage": "genesis_v2_background", "source_family": group_family,
                        "source_pass": idx, "source_pass_total": len(source_groups)},
            )
        output = worker.build_reducer_output_from_texts(
            user_id=store.user_id, job_id=job_id,
            key_prefix=f"{job_id}:source_pass:{idx}:{group_family}",
            runtime=runtime, chunk_texts=group_chunks, source_kind=group_kind,
            existing_persona=existing_persona, existing_voice=existing_voice,
            # only the foreground group needs its already-written core skipped/deduped
            skip_fact_texts=skip_texts if group_family == skip_family else None,
            known_memories=known if group_family == skip_family else None,
            include_memory=include_memory,
            include_persona_voice=include_persona_voice,
            user_name=user_name,
            llm=llm,
            resume_map_outputs=(progress.resume_outputs(idx, group_family) if progress else None),
            on_map_completed=(
                (lambda chunk_index, mapped, source_pass=idx, family=group_family:
                 progress.record_map(source_pass, family, chunk_index, mapped))
                if progress else None
            ),
        )
        if progress and group_family in {"ai_persona", "memory_summary"}:
            progress.record_non_map_group(
                idx,
                group_family,
                cards=_material_card_count(output),
            )
        reducer_outputs.append(output)
        next_persona = _plaintext_existing_persona_from_output(output)
        if next_persona:
            existing_persona = next_persona
        next_voice = _plaintext_existing_voice_from_output(output)
        if next_voice:
            existing_voice = next_voice

    merged = _plaintext_merge_reducer_outputs(reducer_outputs, relationship_anchor=relationship_anchor)
    # conservative lexical backstop: drop any near-identical survivor the model missed
    if include_memory and known and isinstance(merged.get("memories"), list):
        kept, dropped = dedup.filter_semantic_dups(merged["memories"], known)
        if dropped:
            merged["memories"] = kept
    # apply the REST without re-completing: memories (core already excluded), persona, voice
    background_memory_count = 0
    if include_memory:
        apply_result = service.apply_memory_outputs(store, api_key, merged)
        if isinstance(apply_result, tuple) and apply_result:
            background_memory_count = int(apply_result[0] or 0)
    # Identity is normally written by the FOREGROUND now (identity-first contract), so the
    # background skips it (write_identity=False). When foreground had no signal,
    # background may still derive one from the full reduce or persona; absence remains valid.
    background_identity_status = ""
    if write_identity:
        if not _merged_has_identity(merged) and isinstance(merged.get("persona"), dict):
            persona_content = str(merged["persona"].get("content") or "").strip()
            if persona_content:
                baseline = worker.derive_identity_from_persona(
                    user_id=store.user_id,
                    job_id=job_id,
                    runtime=runtime,
                    persona_content=persona_content,
                    user_name=user_name,
                )
                if baseline.get("agent_name") or baseline.get("dimensions"):
                    # B2: merge, don't overwrite — `merged["identity"]` may already
                    # carry user-layer signal (custom_persona_prompt etc, which
                    # _merged_has_identity deliberately doesn't count as "usable
                    # identity" below) from a user_profile pass; a plain assignment
                    # here would silently wipe it the moment a persona baseline
                    # also exists.
                    existing_identity = merged.get("identity") if isinstance(merged.get("identity"), dict) else {}
                    merged["identity"] = {**existing_identity, **baseline}
        background_identity_status = service.init_identity_if_absent(
            store, _attach_plaintext_user_name(merged, user_name), api_key
        )
    background_persona_ref, background_persona_sha = service.write_persona_artifact(
        store, job_id, merged)
    service.write_voice_artifact(store, job_id, merged)
    if completion is not None:
        _complete_plaintext_v2_job(
            store,
            job_id,
            progress=progress,
            memory_action_count=(
                int(completion.get("memory_action_count") or 0) + background_memory_count
            ),
            identity_status=(
                background_identity_status
                or str(completion.get("identity_status") or "")
            ),
            persona_ref=(
                background_persona_ref
                or str(completion.get("persona_ref") or "")
            ),
            persona_sha256=(
                background_persona_sha
                or str(completion.get("persona_sha256") or "")
            ),
        )
    elif progress:
        progress.publish(stage="genesis_v2_done", status=service.DONE_JOB_STATUS)
    else:
        db.genesis_set_job_status(
            store.user_id, job_id, status=service.DONE_JOB_STATUS, output={"stage": "genesis_v2_done"},
        )
    # Foreground readiness already resolved stale failure notices. Do not resolve
    # again here: later stages may add a fresh partial notice in future revisions.


def _append_plaintext_onboarding_greeting(
    store,
    *,
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
    try:
        history_import._append_model_api_onboarding_greeting(store, greeting_text)
    except Exception as e:  # noqa: BLE001 — greeting is best-effort for import
        # jobs (a distilled import without a greeting beats a failed job); the
        # durable/idempotent contract lives in _append_model_api_onboarding_greeting.
        print(f"[genesis:{getattr(store, 'user_id', '')}] onboarding greeting append failed: {type(e).__name__}:{str(e)[:160]}")
        return ""
    return str(greeting_text or "")


def _run_plaintext_add_memory_job(
    store,
    api_key: str | None,
    job_id: str,
    *,
    runtime,
    source_groups: list[dict],
    relationship_anchor: dict | None = None,
    user_name: str = "",
    llm: GenesisLLMClient | None = None,
    progress: _PlaintextCheckpointProgress | None = None,
) -> None:
    # this add_memory job path bypasses service.apply_reducer_output (which resolves
    # genesis notices for the reducer-driven completion path) -> resolve here too, at
    # the *start* of the run (matching the fix in apply_reducer_output: resolving
    # stale notices before any new emit, not after, so a partial notice emitted later
    # in this same run isn't immediately clobbered by its own dedupe-key prefix match).
    notices_core.resolve(store, "genesis:")
    fact_candidates: list[dict] = []
    first_output: dict = {}
    # A: long-term-memory archive uploads (source_family=memory_summary) → keep_all (write the
    # user's curated facts thoroughly). Chat-history uploads keep the normal selective behavior.
    keep_all_job = False
    for idx, group in enumerate(source_groups, start=1):
        group_kind = str(group.get("source_kind") or history_import._HISTORY_SOURCE)
        group_family = str(group.get("source_family") or worker._source_family(group_kind))
        group_chunks = [str(text) for text in (group.get("chunk_texts") or []) if str(text or "").strip()]
        if not group_chunks:
            continue
        keep_all = group_family == "memory_summary"
        keep_all_job = keep_all_job or keep_all
        db.genesis_set_job_status(
            store.user_id,
            job_id,
            status="processing",
            output={
                "stage": "plaintext_add_memory",
                "source_family": group_family,
                "source_pass": idx,
                "source_pass_total": len(source_groups),
            },
        )
        output = worker.build_foreground_output_from_texts(
            user_id=store.user_id,
            job_id=job_id,
            key_prefix=f"{job_id}:add_memory:{idx}:{group_family}",
            runtime=runtime,
            chunk_texts=group_chunks,
            source_kind=group_kind,
            write_core=False,
            keep_all=keep_all,
            user_name=user_name,
            llm=llm,
            resume_map_outputs=(progress.resume_outputs(idx, group_family) if progress else None),
            on_map_completed=(
                (lambda chunk_index, mapped, source_pass=idx, family=group_family:
                 progress.record_map(source_pass, family, chunk_index, mapped))
                if progress else None
            ),
        )
        if not first_output:
            first_output = output
        candidates = output.get("all_fact_candidates") or output.get("core_fact_candidates") or []
        fact_candidates.extend([item for item in candidates if isinstance(item, dict)])

    memory_output = worker.build_memory_output_from_fact_candidates(
        user_id=store.user_id,
        job_id=job_id,
        key_prefix=f"{job_id}:add_memory:fact_write",
        runtime=runtime,
        fact_candidates=fact_candidates,
        keep_all=keep_all_job,
        user_name=user_name,
        llm=llm,
    )
    merged = _plaintext_merge_reducer_outputs([{**first_output, **memory_output}], relationship_anchor=relationship_anchor)
    raw_items = merged.get("memories")
    if raw_items is None:
        raw_items = merged.get("facts")
    raw_count = len(raw_items) if isinstance(raw_items, list) else 0
    mem_count, _results = service.apply_memory_outputs(
        store,
        api_key,
        merged,
        preserve_dates=keep_all_job,
        fallback_occurred_at=str((relationship_anchor or {}).get("relationship_started_at") or "").strip(),
    )
    dropped = raw_count - mem_count
    if dropped > 0:
        notices_core.emit(store, source="genesis", error_class="genesis_partial",
                           blame="system", severity="warning",
                           user_text=catalog.user_text_for("genesis_partial"),
                           detail=f"dropped {dropped} card(s)",
                           dedupe_key=f"genesis:{job_id}:partial")
    completed = db.genesis_complete_job(
        store.user_id,
        job_id,
        output={"stage": "plaintext_add_memory_done"},
        memory_action_count=mem_count,
        identity_status="skipped",
        persona_ref="",
        persona_sha256="",
    )
    if completed:
        service.write_genesis_state(store, completed, status=service.DONE_JOB_STATUS)
        if progress:
            progress.mark_identity_ready()
            progress.publish(stage="plaintext_add_memory_done", status=service.DONE_JOB_STATUS)
    _write_back_plaintext_user_name(store, api_key, user_name)


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
    status = service.replace_identity_preserving_anchor(
        store, {"identity": identity_payload, "relationship_anchor": relationship_anchor or {}}, api_key
    )
    if status not in {"initialized", "updated"}:
        service.mark_failed(store, job_id, status)
        return
    try:
        persona_ref, persona_sha = service.write_persona_artifact(store, job_id, persona_output)
    except Exception as e:  # noqa: BLE001
        service.mark_failed(
            store, job_id, f"persona_write_failed:{type(e).__name__}:{str(e)[:160]}", exc=e,
        )
        return
    if progress:
        for source_pass, group in enumerate(progress.source_groups, start=1):
            family = str(group.get("source_family") or "ai_persona")
            progress.record_non_map_group(source_pass, family, cards=2)
    completed = db.genesis_complete_job(
        store.user_id,
        job_id,
        output={"stage": "plaintext_update_identity_done"},
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

        job = db.genesis_set_job_status(
            store.user_id,
            job_id,
            status="processing",
            output={"stage": "plaintext_reducer"},
        )
        if job:
            service.write_genesis_state(store, job, status="processing")
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
            store, api_key, job_id, source_groups
        )
        user_name = _resolve_plaintext_user_name(
            store, api_key, runtime, source_groups
        )

        if mode == "add_memory":
            _trace_genesis(store, "genesis.plaintext.add_memory.started", job_id=job_id, summary="add memory job started")
            _run_plaintext_add_memory_job(
                store,
                api_key,
                job_id,
                runtime=runtime,
                source_groups=source_groups,
                relationship_anchor=relationship_anchor,
                user_name=user_name,
                llm=llm,
                progress=progress,
            )
            _trace_genesis(store, "genesis.plaintext.done", job_id=job_id, summary="add memory job done",
                           detail={"mode": mode}, dur_ms=(time.time() - started_at) * 1000)
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

        # Genesis v2 (FEEDLING_GENESIS_V2_ENABLED): foreground-fast — greet on 3-5 core
        # + identity baseline, push the heavy reduce to background. Returns False when
        # the foreground yields nothing greetable, so we fall through to the v1 path.
        if worker.genesis_v2_enabled() and _run_plaintext_genesis_v2(
            store, api_key, job_id,
            runtime=runtime, source_groups=source_groups, relationship_anchor=relationship_anchor,
            analysis_messages=analysis_messages,
            user_name=user_name,
            llm=llm,
            progress=progress,
        ):
            _trace_genesis(store, "genesis.plaintext.done", job_id=job_id, summary="genesis v2 job handled",
                           detail={"mode": mode, "genesis_v2": True}, dur_ms=(time.time() - started_at) * 1000)
            return

        reducer_outputs: list[dict] = []
        existing_persona: dict = {}
        existing_voice: dict = {}
        processed_chunks = 0
        for idx, group in enumerate(source_groups, start=1):
            group_source_kind = str(group.get("source_kind") or history_import._HISTORY_SOURCE)
            group_source_family = str(group.get("source_family") or worker._source_family(group_source_kind))
            group_chunk_texts = [str(text) for text in (group.get("chunk_texts") or []) if str(text or "").strip()]
            if not group_chunk_texts:
                continue
            db.genesis_set_job_status(
                store.user_id,
                job_id,
                status="processing",
                output={
                    "stage": "plaintext_reducer",
                    "source_family": group_source_family,
                    "source_pass": idx,
                    "source_pass_total": len(source_groups),
                },
                processed_chunks=processed_chunks,
            )
            pass_started_at = time.time()
            _trace_genesis(
                store,
                "genesis.plaintext.reducer_pass.started",
                job_id=job_id,
                summary="source reducer pass started",
                detail={
                    "source_family": group_source_family,
                    "source_pass": idx,
                    "source_pass_total": len(source_groups),
                    "chunk_count": len(group_chunk_texts),
                },
            )
            output = worker.build_reducer_output_from_texts(
                user_id=store.user_id,
                job_id=job_id,
                key_prefix=f"{job_id}:source_pass:{idx}:{group_source_family}",
                runtime=runtime,
                chunk_texts=group_chunk_texts,
                source_kind=group_source_kind,
                existing_persona=existing_persona,
                existing_voice=existing_voice,
                user_name=user_name,
                llm=llm,
                resume_map_outputs=progress.resume_outputs(idx, group_source_family),
                on_map_completed=(
                    lambda chunk_index, mapped, source_pass=idx, family=group_source_family:
                    progress.record_map(source_pass, family, chunk_index, mapped)
                ),
            )
            if progress and group_source_family in {"ai_persona", "memory_summary"}:
                progress.record_non_map_group(
                    idx,
                    group_source_family,
                    cards=_material_card_count(output),
                )
            _trace_genesis(
                store,
                "genesis.plaintext.reducer_pass.done",
                job_id=job_id,
                summary="source reducer pass done",
                detail={
                    "source_family": group_source_family,
                    "source_pass": idx,
                    "source_pass_total": len(source_groups),
                    "memory_count": len(output.get("memories") or []) if isinstance(output, dict) else 0,
                    "has_identity": bool(isinstance(output, dict) and output.get("identity")),
                    "has_persona": bool(isinstance(output, dict) and output.get("persona")),
                },
                dur_ms=(time.time() - pass_started_at) * 1000,
            )
            reducer_outputs.append(output)
            processed_chunks = progress.processed_chunks()
            next_persona = _plaintext_existing_persona_from_output(output)
            if next_persona:
                existing_persona = next_persona
            next_voice = _plaintext_existing_voice_from_output(output)
            if next_voice:
                existing_voice = next_voice

        reducer_output = _plaintext_merge_reducer_outputs(
            reducer_outputs,
            relationship_anchor=relationship_anchor,
        )
        _attach_plaintext_user_name(reducer_output, user_name)
        progress.publish(stage="plaintext_reducer_done")
        _trace_genesis(
            store,
            "genesis.plaintext.apply.started",
            job_id=job_id,
            summary="apply merged reducer output",
            detail={
                "source_groups": len(source_groups),
                "memory_count": len(reducer_output.get("memories") or []) if isinstance(reducer_output, dict) else 0,
                "has_identity": bool(isinstance(reducer_output, dict) and reducer_output.get("identity")),
                "has_persona": bool(isinstance(reducer_output, dict) and reducer_output.get("persona")),
            },
        )
        service.apply_reducer_output(store, api_key, job_id, reducer_output)
        progress.mark_identity_ready()
        progress.publish(stage="plaintext_reducer_done", status=service.DONE_JOB_STATUS)
        _write_back_plaintext_user_name(store, api_key, user_name)
        _trace_genesis(
            store,
            "genesis.plaintext.done",
            job_id=job_id,
            summary="plaintext genesis job done",
            detail={"mode": mode, "source_groups": len(source_groups)},
            dur_ms=(time.time() - started_at) * 1000,
        )
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
