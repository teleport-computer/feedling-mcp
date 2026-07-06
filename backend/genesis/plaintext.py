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
import threading
import time
from datetime import date
from typing import Any

import db
import debug_trace
from accounts import runtime_auth
from core import enclave as core_enclave
from genesis import dedup, foreground, foreground_identity, genesis_core, service, worker
from hosted import config_store as hosted_config_store
from hosted import history_import
from identity import service as identity_service

_SECONDS_PER_DAY = 24 * 60 * 60
_PLAINTEXT_ACTIVE_LOCK = threading.Lock()
_PLAINTEXT_ACTIVE_JOBS: set[tuple[str, str]] = set()
_PLAINTEXT_SOURCE_ORDER = (
    history_import._AI_PERSONA_SOURCE,
    history_import._HISTORY_SOURCE,
    history_import._MEMORY_SUMMARY_SOURCE,
    history_import._USER_PROFILE_SOURCE,
)
_PLAINTEXT_SUPPORT_SOURCE_FAMILIES = {
    history_import._AI_PERSONA_SOURCE,
    history_import._USER_PROFILE_SOURCE,
    history_import._MEMORY_SUMMARY_SOURCE,
}
_PLAINTEXT_MODES = {"onboarding", "add_memory", "update_identity"}


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
    被砍的 history 块由后台补全,不影响身份(名字来自人物卡,全读)。"""
    cap = _foreground_history_cap()
    out: list[dict] = []
    for g in source_groups:
        if str(g.get("source_family") or "") == "history":
            chunks = list(g.get("chunk_texts") or [])
            if len(chunks) > cap:
                chunks = history_import._select_evenly(chunks, cap)
            out.append({**g, "chunk_texts": chunks})
        else:
            out.append(g)
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
            raise ValueError(
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
        raise ValueError("plaintext_import_empty")
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
        if str(job.get("status") or "") == service.FAILED_JOB_STATUS:
            continue
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
    for output in outputs:
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
            merged.append(item)
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
    identity = {"agent_name": agent_name, "dimensions": dimensions} if (agent_name or dimensions) else {}

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
) -> bool:
    """Genesis v2 foreground-fast orchestration (behind FEEDLING_GENESIS_V2_ENABLED).

    Foreground restores the legacy chat_ready contract: pick 3-5 core memories, derive a
    REAL Identity Card via the existing hosted deriver (foreground_identity, no new
    prompt), write identity + relationship anchor (_store_identity_payload) + a greeting,
    and only THEN complete the job — so the app enters with a named/anchored TA, never a
    blank home. Background then does the heavy full reduce (rest of memories + voice +
    persona), skipping the core and NOT re-writing identity; a background failure never
    fails the already-greetable onboarding.

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
    combined_map = worker.genesis_combined_map_enabled()
    foreground_reduces: list[dict] = []
    primary_reduce: dict | None = None
    voice_candidates: list[dict] = []
    persona_material_parts: list[str] = []
    for idx, group in enumerate(fg_source_groups, start=1):
        group_kind = str(group.get("source_kind") or history_import._HISTORY_SOURCE)
        group_family = str(group.get("source_family") or worker._source_family(group_kind))
        group_chunks = [str(t) for t in (group.get("chunk_texts") or []) if str(t or "").strip()]
        if not group_chunks:
            continue
        if combined_map and group_family == "ai_persona":
            persona_material_parts.extend(group_chunks)
        reduce = worker.build_foreground_output_from_texts(
            user_id=store.user_id, job_id=job_id,
            key_prefix=f"{job_id}:source_pass:{idx}:{group_family}",
            runtime=runtime, chunk_texts=group_chunks, source_kind=group_kind,
            include_voice_candidates=combined_map,
            write_core=False,
        )
        foreground_reduces.append(reduce)
        voice_candidates.extend([c for c in (reduce.get("voice_candidates") or []) if isinstance(c, dict)])
        if idx == fg_idx:
            primary_reduce = reduce

    if not foreground_reduces:
        return False
    primary_reduce = primary_reduce or foreground_reduces[0]
    all_fact_candidates: list[dict] = []
    for reduce in foreground_reduces:
        candidates = reduce.get("all_fact_candidates") or reduce.get("core_fact_candidates") or []
        all_fact_candidates.extend([c for c in candidates if isinstance(c, dict)])

    core = primary_reduce.get("core_fact_candidates") or foreground.select_core_for_foreground(all_fact_candidates)
    if not core:
        return False  # nothing to work with -> let the v1 full path handle it

    full_fact_write = worker.build_memory_output_from_fact_candidates(
        user_id=store.user_id,
        job_id=job_id,
        key_prefix=f"{job_id}:foreground_full",
        runtime=runtime,
        fact_candidates=all_fact_candidates,
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
        )
        fg_merged = _plaintext_merge_reducer_outputs(
            [{**fg_merged, **voice_persona_output}],
            relationship_anchor=relationship_anchor,
        )
    full_memories = fg_merged.get("memories") or []
    days = int((relationship_anchor or {}).get("days_with_user") or 0)
    # explicit relationship_started_at (user typed a date) -> honored verbatim below,
    # per the documented priority; blank -> _store_identity_payload falls back to memory.
    explicit_started_at = str((relationship_anchor or {}).get("relationship_started_at") or "").strip()
    msgs = analysis_messages if isinstance(analysis_messages, list) else []
    language = history_import._import_language_for_store(store, msgs)

    # Foreground-ready contract, restored from the legacy chat_ready: the user only
    # enters once the Identity Card is REAL. Derive it with the EXISTING hosted deriver
    # (orchestration only — no new prompt/logic), then write identity + relationship
    # anchor + a greeting BEFORE completing. So the home is never blank and validate's
    # identity_card passes at entry. Heavy voice/persona/full-memory stay in background.
    identity_payload, id_warnings = foreground_identity.derive_foreground_identity(
        runtime=runtime, analysis_messages=msgs, core_memories=full_memories,
        days_with_user=days, language=language,
    )
    provider_failure = _provider_identity_failure(id_warnings)
    if provider_failure:
        service.mark_failed(store, job_id, f"foreground_identity_failed:{provider_failure}")
        return True
    identity_first = bool(msgs) and foreground_identity.has_identity_signal(identity_payload)
    persona_ref = ""
    persona_sha = ""
    if combined_map:
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
        )
        completed = db.genesis_complete_job(
            store.user_id, job_id, output={"stage": "genesis_v2_foreground_ready"},
            memory_action_count=mem_count, identity_status="initialized",
            persona_ref=persona_ref, persona_sha256=persona_sha,
        )
        if completed:
            service.write_genesis_state(store, completed, status=service.DONE_JOB_STATUS)
    else:
        # edge: foreground couldn't derive an identity -> current backstop (complete on
        # core + let the background fill identity via init_identity/persona baseline).
        # Rare, never worse than before; the iOS minimal-seed page is the real fix.
        _append_plaintext_onboarding_greeting(
            store,
            runtime=runtime,
            analysis_messages=msgs,
            memories=full_memories,
            identity_payload=identity_payload,
            days=days,
            language=language,
        )
        service.apply_reducer_output(store, api_key, job_id, fg_merged)

    if combined_map:
        return True

    # foreground core memory texts -> background as "already saved, don't repeat"
    # (semantic dedup of reworded twins lives in the model).
    core_memory_texts = [
        t for t in (str((m or {}).get("summary") or (m or {}).get("content") or "").strip()
                    for m in full_memories) if t
    ]

    # background continuation — never fails the (already greetable) job. When the
    # foreground already wrote identity, the background must NOT re-write it.
    try:
        _run_plaintext_background_enrichment(
            store, api_key, job_id, runtime=runtime, source_groups=source_groups,
            relationship_anchor=relationship_anchor,
            skip_family=fg_family, skip_texts=foreground.core_skip_texts(core),
            known_memories=core_memory_texts, write_identity=not identity_first,
            include_memory=False,
        )
    except Exception as e:  # noqa: BLE001
        db.genesis_set_job_status(
            store.user_id, job_id, status=service.DONE_JOB_STATUS,
            output={"stage": "genesis_v2_background_deferred",
                    "error": f"{type(e).__name__}:{str(e)[:180]}"},
        )
    return True


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
) -> None:
    """Background continuation: the full reduce over every group (skipping the core the
    foreground already wrote for skip_family), then apply the REST incrementally —
    memories + persona + voice. Does NOT re-complete the job (foreground already did).

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
        db.genesis_set_job_status(
            store.user_id, job_id, status=service.DONE_JOB_STATUS,
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
    if include_memory:
        service.apply_memory_outputs(store, api_key, merged)
    # Identity is normally written by the FOREGROUND now (identity-first contract), so the
    # background skips it (write_identity=False). Only the edge fallback (foreground could
    # not derive an identity) asks the background to fill it — from the full reduce, or a
    # persona-derived baseline so onboarding never wedges on an empty identity_card.
    if write_identity:
        if not _merged_has_identity(merged) and isinstance(merged.get("persona"), dict):
            persona_content = str(merged["persona"].get("content") or "").strip()
            if persona_content:
                baseline = worker.derive_identity_from_persona(
                    user_id=store.user_id, job_id=job_id, runtime=runtime, persona_content=persona_content,
                )
                if baseline.get("agent_name") or baseline.get("dimensions"):
                    merged["identity"] = baseline
        service.init_identity_if_absent(store, merged, api_key)
    service.write_persona_artifact(store, job_id, merged)
    service.write_voice_artifact(store, job_id, merged)
    db.genesis_set_job_status(
        store.user_id, job_id, status=service.DONE_JOB_STATUS, output={"stage": "genesis_v2_done"},
    )


def _append_plaintext_onboarding_greeting(
    store,
    *,
    runtime,
    analysis_messages: list[dict],
    memories: list[dict],
    identity_payload: dict,
    days: int,
    language: str,
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
        greeting_text = (
            "好久不见，很高兴又能和你聊天。"
            if str(language).startswith("zh")
            else "Good to see you again — I'm glad we can talk."
        )
    try:
        history_import._append_model_api_onboarding_greeting(store, greeting_text)
    except Exception:
        return ""
    return str(greeting_text or "")


def _run_plaintext_add_memory_job(
    store,
    api_key: str | None,
    job_id: str,
    *,
    runtime,
    source_groups: list[dict],
) -> None:
    fact_candidates: list[dict] = []
    first_output: dict = {}
    for idx, group in enumerate(source_groups, start=1):
        group_kind = str(group.get("source_kind") or history_import._HISTORY_SOURCE)
        group_family = str(group.get("source_family") or worker._source_family(group_kind))
        group_chunks = [str(text) for text in (group.get("chunk_texts") or []) if str(text or "").strip()]
        if not group_chunks:
            continue
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
    )
    merged = _plaintext_merge_reducer_outputs([{**first_output, **memory_output}], relationship_anchor={})
    mem_count, _results = service.apply_memory_outputs(store, api_key, merged)
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


def _run_plaintext_update_identity_job(
    store,
    api_key: str | None,
    job_id: str,
    *,
    runtime,
    analysis_messages: list[dict] | None,
) -> None:
    if not identity_service._load_identity(store):
        service.mark_failed(store, job_id, "identity_not_initialized")
        return
    msgs = analysis_messages if isinstance(analysis_messages, list) else []
    language = history_import._import_language_for_store(store, msgs)
    identity_payload, warnings = history_import._derive_identity_with_provider(
        runtime,
        msgs,
        [],
        0,
        language,
    )
    provider_failure = _provider_identity_failure(warnings)
    if provider_failure:
        service.mark_failed(store, job_id, f"update_identity_failed:{provider_failure}")
        return
    if not _identity_payload_has_content(identity_payload):
        service.mark_failed(store, job_id, "identity_update_empty")
        return
    persona_material = _plaintext_persona_material_from_messages(msgs)
    if not persona_material:
        service.mark_failed(store, job_id, "persona_material_required")
        return
    voice_workset = _plaintext_existing_voice_workset_for_update(store, api_key)
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
        )
    except Exception as e:  # noqa: BLE001
        service.mark_failed(store, job_id, f"persona_rebuild_failed:{type(e).__name__}:{str(e)[:160]}")
        return
    status = service.replace_identity_preserving_anchor(store, {"identity": identity_payload})
    if status != "updated":
        service.mark_failed(store, job_id, status)
        return
    try:
        persona_ref, persona_sha = service.write_persona_artifact(store, job_id, persona_output)
    except Exception as e:  # noqa: BLE001
        service.mark_failed(store, job_id, f"persona_write_failed:{type(e).__name__}:{str(e)[:160]}")
        return
    completed = db.genesis_complete_job(
        store.user_id,
        job_id,
        output={"stage": "plaintext_update_identity_done"},
        memory_action_count=0,
        identity_status="updated",
        persona_ref=persona_ref,
        persona_sha256=persona_sha,
    )
    if completed:
        service.write_genesis_state(store, completed, status=service.DONE_JOB_STATUS)


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
    active_key = (store.user_id, job_id)
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
            raise ValueError("plaintext_import_empty")

        job = db.genesis_set_job_status(
            store.user_id,
            job_id,
            status="processing",
            output={"stage": "plaintext_reducer"},
            processed_chunks=0,
        )
        if job:
            service.write_genesis_state(store, job, status="processing")
        runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
        if isinstance(runtime, tuple):
            _, err = runtime
            raise RuntimeError(json.dumps(err, ensure_ascii=False))
        _trace_genesis(
            store,
            "genesis.plaintext.runtime.loaded",
            job_id=job_id,
            summary="runtime config loaded",
            detail={"mode": mode},
        )

        if mode == "add_memory":
            _trace_genesis(store, "genesis.plaintext.add_memory.started", job_id=job_id, summary="add memory job started")
            _run_plaintext_add_memory_job(
                store,
                api_key,
                job_id,
                runtime=runtime,
                source_groups=source_groups,
            )
            _trace_genesis(store, "genesis.plaintext.done", job_id=job_id, summary="add memory job done",
                           detail={"mode": mode}, dur_ms=(time.time() - started_at) * 1000)
            return
        if mode == "update_identity":
            _trace_genesis(store, "genesis.plaintext.update_identity.started", job_id=job_id,
                           summary="update identity job started")
            _run_plaintext_update_identity_job(
                store,
                api_key,
                job_id,
                runtime=runtime,
                analysis_messages=analysis_messages,
            )
            _trace_genesis(store, "genesis.plaintext.done", job_id=job_id, summary="update identity job done",
                           detail={"mode": mode}, dur_ms=(time.time() - started_at) * 1000)
            return

        # Genesis v2 (FEEDLING_GENESIS_V2_ENABLED): foreground-fast — greet on 3-5 core
        # + identity baseline, push the heavy reduce to background. Returns False when
        # the foreground yields nothing greetable, so we fall through to the v1 path.
        if worker.genesis_v2_enabled() and _run_plaintext_genesis_v2(
            store, api_key, job_id,
            runtime=runtime, source_groups=source_groups, relationship_anchor=relationship_anchor,
            analysis_messages=analysis_messages,
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
            processed_chunks += len(group_chunk_texts)
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
        db.genesis_set_job_status(
            store.user_id,
            job_id,
            status="processing",
            output={"stage": "plaintext_reducer_done"},
            processed_chunks=sum(len(group.get("chunk_texts") or []) for group in source_groups),
        )
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
        service.mark_failed(store, job_id, f"plaintext_import_failed:{type(e).__name__}:{str(e)[:220]}")
    finally:
        with _PLAINTEXT_ACTIVE_LOCK:
            _PLAINTEXT_ACTIVE_JOBS.discard(active_key)


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
    active_key = (store.user_id, job_id)
    with _PLAINTEXT_ACTIVE_LOCK:
        if active_key in _PLAINTEXT_ACTIVE_JOBS:
            return False
        _PLAINTEXT_ACTIVE_JOBS.add(active_key)
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
    try:
        thread.start()
    except Exception:
        with _PLAINTEXT_ACTIVE_LOCK:
            _PLAINTEXT_ACTIVE_JOBS.discard(active_key)
        raise
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
