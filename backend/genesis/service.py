"""Genesis import service: state blobs, ledger helpers, reducer application."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, date
from typing import Any

import db
from bootstrap import gates as boot_gates
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import util as core_util
from core.store import UserStore
from identity import service as identity_service
from memory import actions as memory_actions
from notices import catalog
from notices import core as notices

GENESIS_STATE_BLOB = "genesis_state"
GENESIS_PERSONA_BLOB = "genesis_persona"
GENESIS_VOICE_BLOB = "genesis_voice"
GENESIS_SOURCE = "genesis_import"
GENESIS_PERSONA_REF = f"user_blob:{GENESIS_PERSONA_BLOB}"
GENESIS_VOICE_REF = f"user_blob:{GENESIS_VOICE_BLOB}"
PERSONA_SOURCE_PRIORITY = {
    "ai_persona": 100,
    "merged": 100,
    "history": 50,
    "unknown": 10,
}

# Map v2-internal genesis stages to the LEGACY phase vocabulary the shipped iOS already
# knows (localizedHistoryPhase), so old apps show correct copy without an app update.
# Only what's REPORTED to the client is mapped; stored stages / flow-trace are unchanged.
_PUBLIC_STAGE_MAP = {
    "genesis_v2_foreground": "chat_history_importing",
    "genesis_v2_foreground_ready": "completed",
    "genesis_v2_background": "background_importing",
    "genesis_v2_background_deferred": "background_importing",
    "genesis_v2_done": "completed",
    # v1 / pre-gate stages: set at routes.py before the v2 branch, so they leak even on
    # v2 at job start (and throughout on a v1 fallthrough). iOS localizedHistoryPhase has
    # no case for them -> shows the raw "plaintext_reducer" text. Map to friendly phases.
    "plaintext_reducer": "chat_history_importing",
    "plaintext_reducer_done": "background_importing",
}


def public_stage(stage: str) -> str:
    """Client-facing stage name. v2-internal stages -> legacy phases the old iOS maps;
    legacy/unknown stages pass through unchanged."""
    return _PUBLIC_STAGE_MAP.get(str(stage or ""), str(stage or ""))


PRIVACY_MODE = "backend_storage_no_plaintext_user_provider_authorized"
PRIVACY_COPY = (
    "Feedling persistent storage does not store imported plaintext; plaintext is "
    "processed inside the CVM and sent only to the LLM provider the user "
    "configured with their authorized key."
)

DONE_JOB_STATUS = "done"
FAILED_JOB_STATUS = "failed"

ALLOWED_MEMORY_TYPES = {"fact", "event", "quote", "moment"}
CHUNK_ENVELOPE_META_REQUIRED = (
    "v",
    "id",
    "nonce",
    "K_user",
    "K_enclave",
    "visibility",
    "owner_user_id",
)
CHUNK_ENVELOPE_META_OPTIONAL = ("enclave_pk_fpr",)
RAW_REDUCER_OUTPUT_FIELDS = {
    "raw",
    "raw_text",
    "transcript",
    "transcripts",
    "chunk",
    "chunks",
    "chunk_text",
    "chunk_texts",
}
SAFE_JOB_METADATA_KEYS = {
    "archive_format",
    "client_version",
    "client_job_id",
    "file_count",
    "history_tier",
    "ingest",
    "locale",
    "mode",
    "schema_version",
    "source_label",
    "timeline_span_days",
    "window_count",
}


def _text(value: Any, max_chars: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:max_chars].strip()


def _now_iso() -> str:
    return core_util._now_iso()


def _sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def b64decode_required(value: str) -> bytes:
    try:
        return base64.b64decode(str(value or ""), validate=True)
    except Exception as e:  # noqa: BLE001
        raise ValueError("invalid_base64_ciphertext") from e


def b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _stable_json_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return _sha256_hex(raw.encode("utf-8"))


def _safe_job_metadata(metadata: Any) -> dict:
    """Keep only non-content import metadata.

    Genesis plaintext must arrive as encrypted chunks. Arbitrary metadata is too
    easy for clients to misuse for raw persona/profile/transcript content, so the
    persisted job doc keeps only small operational hints and hashes/counts.
    """
    if not isinstance(metadata, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        name = str(key or "").strip()
        lower = name.lower()
        if (
            name in SAFE_JOB_METADATA_KEYS
            or lower.endswith("_hash")
            or lower.endswith("_sha256")
            or lower.endswith("_count")
            or lower.endswith("_bytes")
        ):
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[name] = value
    return safe


def _chunk_envelope_meta(store: UserStore, envelope_meta: dict | None, encrypted_body: bytes) -> dict:
    if not isinstance(envelope_meta, dict):
        raise ValueError("chunk_envelope_required")
    body_ct = str(envelope_meta.get("body_ct") or "")
    if body_ct:
        if b64decode_required(body_ct) != encrypted_body:
            raise ValueError("chunk_envelope_body_ct_mismatch")

    meta: dict[str, Any] = {}
    missing: list[str] = []
    for key in CHUNK_ENVELOPE_META_REQUIRED:
        value = envelope_meta.get(key)
        if value in (None, ""):
            missing.append(key)
            continue
        meta[key] = value
    for key in CHUNK_ENVELOPE_META_OPTIONAL:
        value = envelope_meta.get(key)
        if value not in (None, ""):
            meta[key] = value
    if missing:
        raise ValueError(f"chunk_envelope_missing_fields:{','.join(missing)}")
    try:
        meta["v"] = int(meta["v"])
    except Exception as e:  # noqa: BLE001
        raise ValueError("chunk_envelope_v_invalid") from e
    meta["id"] = _text(meta.get("id"), 160)
    if not meta["id"]:
        raise ValueError("chunk_envelope_id_required")
    meta["visibility"] = _text(meta.get("visibility"), 40)
    if meta["visibility"] != "shared":
        raise ValueError("chunk_envelope_visibility_must_be_shared")
    meta["owner_user_id"] = _text(meta.get("owner_user_id"), 160)
    if meta["owner_user_id"] != store.user_id:
        raise ValueError("chunk_envelope_owner_mismatch")
    for key in ("nonce", "K_user", "K_enclave", "enclave_pk_fpr"):
        if key in meta:
            meta[key] = str(meta.get(key) or "").strip()
    return meta


def chunk_envelope_from_row(chunk: dict) -> dict:
    """Reconstruct a v1 envelope from a stored chunk row for the CVM worker."""
    aad = chunk.get("aad") if isinstance(chunk.get("aad"), dict) else {}
    meta = aad.get("envelope_meta") if isinstance(aad.get("envelope_meta"), dict) else {}
    encrypted_body = chunk.get("encrypted_body") or b""
    if isinstance(encrypted_body, memoryview):
        encrypted_body = encrypted_body.tobytes()
    if isinstance(encrypted_body, str):
        encrypted_body = encrypted_body.encode("utf-8")
    if not isinstance(encrypted_body, (bytes, bytearray)):
        raise ValueError("chunk_encrypted_body_required")
    if not meta:
        raise ValueError("chunk_envelope_meta_missing")
    return {**meta, "body_ct": b64encode(bytes(encrypted_body))}


def new_job_id() -> str:
    return core_util._new_public_id("genesis")


def gate_status_for_job_status(status: str) -> str:
    status = str(status or "").strip().lower()
    if status == DONE_JOB_STATUS:
        return DONE_JOB_STATUS
    if status == FAILED_JOB_STATUS:
        return FAILED_JOB_STATUS
    return "processing"


def write_genesis_state(store: UserStore, job: dict, *, status: str | None = None) -> dict:
    job_status = str(job.get("status") or "")
    state = {
        "v": 1,
        "status": status or gate_status_for_job_status(job_status),
        "job_status": job_status,
        "job_id": str(job.get("job_id") or ""),
        # so the spawn gate can tell a founding genesis (block spawn until done) from
        # a background companion_persona_backfill (must NOT block — cutover gate 4).
        "source_kind": str(job.get("source_kind") or ""),
        "updated_at": _now_iso(),
        "completed_at": str(job.get("completed_at") or ""),
        "memory_action_count": int(job.get("memory_action_count") or 0),
        "identity_status": str(job.get("identity_status") or ""),
        "persona_ref": str(job.get("persona_ref") or ""),
        "persona_sha256": str(job.get("persona_sha256") or ""),
        "error": str(job.get("error") or ""),
        "privacy_mode": str(job.get("privacy_mode") or PRIVACY_MODE),
    }
    db.set_blob(store.user_id, GENESIS_STATE_BLOB, state)
    return state


def create_import_job(store: UserStore, payload: dict) -> tuple[dict, int]:
    job_id = _text(payload.get("job_id") or new_job_id(), 80)
    source_kind = _text(payload.get("source_kind") or payload.get("source") or "unknown", 80)
    try:
        total_chunks = int(payload.get("total_chunks") or 0)
        total_bytes = int(payload.get("total_bytes") or 0)
    except Exception as e:  # noqa: BLE001
        raise ValueError("total_chunks_total_bytes_must_be_int") from e
    if total_chunks < 0 or total_chunks > 100000:
        raise ValueError("total_chunks_out_of_range")
    if total_bytes < 0:
        raise ValueError("total_bytes_out_of_range")
    metadata = _safe_job_metadata(payload.get("metadata"))
    metadata = {
        **metadata,
        "privacy_copy": PRIVACY_COPY,
    }
    job = db.genesis_create_job(store.user_id, {
        "job_id": job_id,
        "status": "created",
        "source_kind": source_kind,
        "file_manifest_hash": _text(payload.get("file_manifest_hash"), 128),
        "total_chunks": total_chunks,
        "total_bytes": total_bytes,
        "privacy_mode": PRIVACY_MODE,
        "metadata": metadata,
    })
    if job is None:
        existing = db.genesis_get_job(store.user_id, job_id)
        return existing or {"job_id": job_id, "status": "unknown"}, 200
    write_genesis_state(store, job)
    return job, 201


def put_chunk(
    store: UserStore,
    job_id: str,
    *,
    seq: int,
    encrypted_body: bytes,
    byte_start: int,
    byte_end: int,
    content_sha256: str = "",
    expected_ciphertext_sha256: str = "",
    aad: dict | None = None,
    envelope_meta: dict | None = None,
) -> dict:
    job = db.genesis_get_job(store.user_id, job_id)
    if not job:
        raise LookupError("genesis_job_not_found")
    total_chunks = int(job.get("total_chunks") or 0)
    if seq < 0 or (total_chunks and seq >= total_chunks):
        raise ValueError("chunk_seq_out_of_range")
    if not encrypted_body:
        raise ValueError("empty_chunk")
    cipher_hash = _sha256_hex(encrypted_body)
    if expected_ciphertext_sha256 and expected_ciphertext_sha256 != cipher_hash:
        raise ValueError("ciphertext_sha256_mismatch")
    if byte_end <= 0:
        byte_end = byte_start + len(encrypted_body)
    if byte_start < 0 or byte_end < byte_start:
        raise ValueError("invalid_byte_range")
    clean_content_hash = _text(content_sha256, 128)
    clean_envelope_meta = _chunk_envelope_meta(store, envelope_meta, encrypted_body)
    clean_aad = dict(aad or {})
    clean_aad.pop("envelope_meta", None)
    clean_aad.update({
        "user_id": store.user_id,
        "job_id": job_id,
        "seq": seq,
        "content_hash": clean_content_hash,
        "ciphertext_sha256": cipher_hash,
        "envelope_meta": clean_envelope_meta,
    })
    chunk = db.genesis_put_chunk(
        store.user_id,
        job_id,
        seq=seq,
        byte_start=byte_start,
        byte_end=byte_end,
        ciphertext_sha256=cipher_hash,
        content_sha256=clean_content_hash,
        aad=clean_aad,
        encrypted_body=encrypted_body,
    )
    updated = db.genesis_get_job(store.user_id, job_id) or job
    write_genesis_state(store, updated)
    return chunk


def finalize_upload(store: UserStore, job_id: str) -> tuple[dict, list[int]]:
    job = db.genesis_get_job(store.user_id, job_id)
    if not job:
        raise LookupError("genesis_job_not_found")
    total_chunks = int(job.get("total_chunks") or 0)
    missing = db.genesis_missing_chunk_seqs(store.user_id, job_id, total_chunks)
    if missing:
        write_genesis_state(store, {**job, "status": "uploading"})
        return job, missing
    finalized = db.genesis_mark_finalized(store.user_id, job_id) or job
    write_genesis_state(store, finalized, status="uploaded")
    return finalized, []


def mark_failed(store: UserStore, job_id: str, error: str) -> dict | None:
    job = db.genesis_set_job_status(store.user_id, job_id, status=FAILED_JOB_STATUS, error=error)
    if job:
        write_genesis_state(store, job, status=FAILED_JOB_STATUS)
    # emit unconditionally: only needs store + job_id + error, not the job row
    # (a race where the job row is already gone shouldn't silence the notice).
    ec = catalog.classify_upstream(error) or "genesis_failed"
    notices.emit(store, source="genesis", error_class=ec,
                 blame=catalog.blame_for(ec), severity="error",
                 user_text=catalog.user_text_for(ec), detail=error,
                 dedupe_key=f"genesis:{job_id}")
    return job


def _coerce_memory_type(value: Any) -> str:
    mem_type = _text(value or "fact", 40).lower()
    if mem_type not in ALLOWED_MEMORY_TYPES:
        return "fact"
    return mem_type


def _memory_action_from_output(item: dict) -> dict:
    mem_type = _coerce_memory_type(item.get("type"))
    memory = {
        "type": mem_type,
        "summary": _text(item.get("summary") or item.get("title") or item.get("description"), 2000),
        "content": str(item.get("content") or "").strip()[:5000],
        "bucket": _text(item.get("bucket"), 80),
        "threads": item.get("threads") if isinstance(item.get("threads"), list) else [],
        "occurred_at": _text(item.get("occurred_at"), 80),
        "source": GENESIS_SOURCE,
        "importance": item.get("importance", 0.5),
        "pulse": item.get("pulse", 0.3),
    }
    if not memory["summary"]:
        raise ValueError("genesis_memory_summary_required")
    if not memory["content"]:
        memory["content"] = f"Memory: {memory['summary']}"
    return {
        "type": "memory.add",
        "memory": memory,
        "reason": _text(item.get("reason") or "Genesis import fact extraction.", 500),
        "capture_mode": GENESIS_SOURCE,
    }


def apply_memory_outputs(store: UserStore, api_key: str | None, output: dict) -> tuple[int, list[dict]]:
    raw_items = output.get("memories")
    if raw_items is None:
        raw_items = output.get("facts")
    if not isinstance(raw_items, list) or not raw_items:
        return 0, []
    actions: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            actions.append(_memory_action_from_output(item))
        except ValueError:
            # LLM reducers can occasionally emit a partial memory object. Keep the
            # import alive and write the valid cards instead of failing the whole job.
            continue
    if not actions:
        return 0, []
    results: list[dict] = []
    for idx in range(0, len(actions), 20):
        batch = actions[idx:idx + 20]
        body, status = memory_actions._execute_memory_actions(store, api_key, batch)
        if status >= 400:
            raise RuntimeError(f"memory_actions_failed:{body.get('error', status)}")
        results.extend(list(body.get("results") or []))
    return len(results), results


def _reject_raw_reducer_fields(output: dict) -> None:
    for key in output:
        if str(key).strip().lower() in RAW_REDUCER_OUTPUT_FIELDS:
            raise ValueError(f"raw_reducer_field_not_allowed:{key}")


def _persona_content_from_output(output: dict) -> tuple[str, str]:
    persona = output.get("persona")
    if isinstance(persona, dict):
        return str(persona.get("content") or persona.get("text") or "").strip(), _text(
            persona.get("prompt_version") or "7.B",
            40,
        )
    return str(persona or "").strip(), "7.B"


def _persona_source_family_from_output(output: dict) -> str:
    persona = output.get("persona") if isinstance(output.get("persona"), dict) else {}
    source_family = _text(
        persona.get("source_family") if isinstance(persona, dict) else "",
        80,
    ) or _text(output.get("source_family"), 80) or "unknown"
    if source_family not in PERSONA_SOURCE_PRIORITY:
        return "unknown"
    return source_family


def _persona_source_priority(source_family: str) -> int:
    return int(PERSONA_SOURCE_PRIORITY.get(source_family, PERSONA_SOURCE_PRIORITY["unknown"]))


def _safe_voice_workset(output: dict) -> dict:
    raw = output.get("voice_workset") if isinstance(output.get("voice_workset"), dict) else {}
    notes = [
        _text(item, 500)
        for item in (raw.get("behavior_notes") if isinstance(raw.get("behavior_notes"), list) else [])
        if _text(item, 500)
    ][:16]
    exemplars: list[dict] = []
    for item in (raw.get("exemplars") if isinstance(raw.get("exemplars"), list) else []):
        if not isinstance(item, dict):
            continue
        turns = []
        for turn in (item.get("turns") if isinstance(item.get("turns"), list) else [])[:8]:
            if not isinstance(turn, dict):
                continue
            text = _text(turn.get("text"), 1200)
            if text:
                turns.append({"role": _text(turn.get("role"), 40), "text": text})
        if not turns:
            continue
        axis = [
            _text(axis_item, 40)
            for axis_item in (item.get("axis") if isinstance(item.get("axis"), list) else [])
            if _text(axis_item, 40)
        ][:8]
        exemplars.append({
            "turns": turns,
            "founding": bool(item.get("founding")),
            "axis": axis,
            "why": _text(item.get("why"), 500),
        })
    if not notes and not exemplars:
        return {}
    return {
        "v": 1,
        "source": GENESIS_SOURCE,
        "source_kind": _text(output.get("source_kind"), 80),
        "source_family": _text(output.get("source_family"), 80),
        "behavior_notes": notes,
        "exemplars": exemplars[:80],
    }


def _safe_reducer_doc(job_id: str, output: dict) -> dict:
    raw_items = output.get("memories")
    if raw_items is None:
        raw_items = output.get("facts")
    memories = raw_items if isinstance(raw_items, list) else []
    type_counts: dict[str, int] = {}
    for item in memories:
        if not isinstance(item, dict):
            continue
        mem_type = _coerce_memory_type(item.get("type"))
        type_counts[mem_type] = type_counts.get(mem_type, 0) + 1
    identity = output.get("identity") if isinstance(output.get("identity"), dict) else {}
    dims = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    persona_content, prompt_version = _persona_content_from_output(output)
    return {
        "v": 1,
        "job_id": job_id,
        "source": GENESIS_SOURCE,
        "source_kind": _text(output.get("source_kind"), 80),
        "source_family": _text(output.get("source_family"), 80),
        "plaintext_stored": False,
        "raw_sha256": _stable_json_sha256(output),
        "memory_count": len(memories),
        "memory_type_counts": type_counts,
        "identity_provided": bool(identity),
        "identity_dimension_count": len(dims),
        "persona_provided": bool(persona_content),
        "persona_sha256": _sha256_hex(persona_content.encode("utf-8")) if persona_content else "",
        "persona_prompt_version": prompt_version if persona_content else "",
        "voice_workset_provided": bool(output.get("voice_workset")),
    }


def _identity_payload_from_output(output: dict) -> dict | None:
    identity = output.get("identity")
    if not isinstance(identity, dict):
        return None
    dims = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    clean_dims: list[dict] = []
    for dim in dims[:7]:
        if not isinstance(dim, dict):
            continue
        name = _text(dim.get("name"), 80)
        desc = _text(dim.get("description") or dim.get("evidence"), 500)
        if not name or not desc:
            continue
        try:
            value = int(dim.get("value", 50))
        except Exception:
            value = 50
        clean_dims.append({
            "name": name,
            "value": max(0, min(100, value)),
            "description": desc,
        })
    agent_name = _text(identity.get("agent_name"), 80).strip(" `\"'“”‘’。，,.;；:：!！?？")
    normalized_name = agent_name.lower()
    if (
        normalized_name in identity_service._IDENTITY_RUNTIME_LABELS
        or normalized_name.startswith(("openai/", "anthropic/", "google/", "deepseek/"))
        or re.search(r"\b(?:api|model|runtime|provider|assistant|agent)\b", normalized_name)
    ):
        agent_name = ""
    category = _clean_identity_category(identity.get("category"))
    if not category and clean_dims:
        category = _category_from_dimensions(clean_dims)
    payload = {
        "agent_name": agent_name,
        # 7.C-write deliberately leaves self_intro/signature for post-respawn TA.
        "self_introduction": "",
        "dimensions": clean_dims,
    }
    if not payload["agent_name"] and not payload["dimensions"]:
        return None
    if category:
        payload["category"] = category
    return payload


def _identity_payload_for_replace(output: dict) -> dict | None:
    """Identity update mode replaces the encrypted identity body, but not the
    relationship anchor metadata. Unlike genesis init, this should preserve the
    user-provided profile fields from the uploaded identity material when present.
    """
    identity = output.get("identity") if isinstance(output.get("identity"), dict) else {}
    if not identity:
        return None
    payload = _identity_payload_from_output(output) or {
        "agent_name": "",
        "self_introduction": "",
        "dimensions": [],
    }
    if identity.get("self_introduction") is not None:
        payload["self_introduction"] = str(identity.get("self_introduction") or "").strip()[:1200]
    category = _clean_identity_category(identity.get("category"))
    if category:
        payload["category"] = category
    for key in identity_service._IDENTITY_PROFILE_STRING_FIELDS:
        if key in {"agent_name", "self_introduction"}:
            continue
        if identity.get(key) is not None:
            payload[key] = str(identity.get(key) or "")[:1200 if key in {"relationship_anchor", "tone_style", "custom_persona_prompt"} else 240]
    for key in identity_service._IDENTITY_PROFILE_LIST_FIELDS:
        if isinstance(identity.get(key), list):
            payload[key] = [str(item)[:240] for item in identity[key][:12] if str(item or "").strip()]
    return payload


def _identity_replace_payload_has_content(payload: dict) -> bool:
    if str(payload.get("agent_name") or "").strip():
        return True
    if isinstance(payload.get("dimensions"), list) and payload["dimensions"]:
        return True
    if str(payload.get("self_introduction") or "").strip():
        return True
    if str(payload.get("category") or "").strip():
        return True
    if isinstance(payload.get("signature"), list) and payload["signature"]:
        return True
    return False


def _clean_identity_category(value: Any) -> str:
    return _text(value, 120).strip(" `\"'“”‘’。，,.;；:：!！?？")


def _category_label_from_dimension_name(value: str) -> str:
    label = _text(value, 32).strip(" `\"'“”‘’。，,.;；:：!！?？")
    for suffix in ("驱动", "倾向", "风格", "能力", "特质", "模式", "性", "型", "度"):
        if label.endswith(suffix) and len(label) > len(suffix) + 1:
            label = label[: -len(suffix)].strip()
            break
    lower = label.lower()
    for suffix in (" driven", " style", " mode", " orientation", " tendency", " trait"):
        if lower.endswith(suffix) and len(label) > len(suffix) + 2:
            label = label[: -len(suffix)].strip()
            break
    return label[:24]


def _category_from_dimensions(dimensions: list[dict]) -> str:
    if not dimensions:
        return ""

    def score(dim: dict) -> int:
        try:
            return int(dim.get("value", 50))
        except Exception:
            return 50

    strongest = max(dimensions, key=score)
    weakest = min(dimensions, key=score)
    labels = [
        _category_label_from_dimension_name(str(strongest.get("name") or "")),
        _category_label_from_dimension_name(str(weakest.get("name") or "")),
    ]
    out: list[str] = []
    for label in labels:
        if label and label not in out:
            out.append(label)
    return " · ".join(out)[:120]


def _identity_payload_from_existing_plain(identity: dict | None) -> dict:
    if not isinstance(identity, dict):
        return {"agent_name": "", "self_introduction": "", "dimensions": []}
    payload = {
        "agent_name": _text(identity.get("agent_name"), 80),
        "self_introduction": str(identity.get("self_introduction") or "").strip()[:1200],
        "dimensions": identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else [],
    }
    for key in identity_service._IDENTITY_PROFILE_STRING_FIELDS:
        if key in {"agent_name", "self_introduction"}:
            continue
        if identity.get(key):
            payload[key] = str(identity.get(key) or "")[:1200 if key in {"relationship_anchor", "tone_style", "custom_persona_prompt"} else 240]
    for key in identity_service._IDENTITY_PROFILE_LIST_FIELDS:
        if isinstance(identity.get(key), list):
            payload[key] = [str(item)[:240] for item in identity[key][:12] if str(item or "").strip()]
    return payload


def _existing_identity_plain_for_update(api_key: str | None, runtime_token: str = "") -> tuple[dict | None, str]:
    if not api_key and not runtime_token:
        return None, "api_key_unavailable"
    data, err = core_enclave._enclave_get_json_for_gate(
        "/v1/identity/get",
        api_key,
        runtime_token=runtime_token,
    )
    if err:
        return None, err
    if not isinstance(data, dict) or not isinstance(data.get("identity"), dict):
        return None, "identity_plain_not_available"
    identity = data["identity"]
    status = identity.get("decrypt_status")
    if status and status != "ok":
        return None, str(status)
    return identity, ""


def _relationship_anchor_from_output(store: UserStore, output: dict, days_int: int) -> str:
    raw_started_at = _text(output.get("relationship_started_at"), 80)
    if raw_started_at:
        parsed = identity_service._parse_iso_calendar_date(raw_started_at)
        if parsed:
            return parsed.isoformat()
    return identity_service._anchor_from_days(days_int, store=store, prefer_memory=True)


def init_identity_if_absent(
    store: UserStore,
    output: dict,
    api_key: str | None = None,
    runtime_token: str = "",
) -> str:
    existing = identity_service._load_identity(store)
    payload = _identity_payload_from_output(output)
    if not payload:
        return "not_provided"

    base_payload = {"agent_name": "", "self_introduction": "", "dimensions": []}
    if existing:
        existing_plain, err = _existing_identity_plain_for_update(api_key, runtime_token)
        if existing_plain is not None:
            base_payload = _identity_payload_from_existing_plain(existing_plain)
        elif str(existing.get("relationship_anchor_source") or "") != GENESIS_SOURCE:
            return "already_initialized"

    # Genesis owns the derived name/dimensions. Preserve the profile fields that
    # the live agent writes after respawn, especially self_introduction/signature.
    merged_payload = dict(base_payload)
    merged_payload["agent_name"] = payload["agent_name"]
    merged_payload["dimensions"] = payload["dimensions"]
    if payload.get("category"):
        merged_payload["category"] = payload["category"]
    if "self_introduction" not in merged_payload:
        merged_payload["self_introduction"] = ""

    days = output.get("days_with_user")
    identity = output.get("identity") if isinstance(output.get("identity"), dict) else {}
    if days is None:
        days = identity.get("days_with_user", 0)
    try:
        days_int = max(0, int(days))
    except Exception:
        days_int = 0
    evidence = _text(
        output.get("relationship_anchor_evidence")
        or identity.get("relationship_anchor_evidence")
        or f"{GENESIS_SOURCE}:{output.get('job_id') or 'import'}",
        500,
    )
    if len(evidence) < 8:
        evidence = f"{GENESIS_SOURCE}:derived from uploaded import"
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store,
        json.dumps(merged_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        item_id=(existing or {}).get("id") or None,
    )
    if envelope is None:
        raise RuntimeError(f"identity_envelope_failed:{err}")
    now = datetime.now().isoformat()
    identity_doc = {
        "v": 1,
        "id": envelope.get("id") or (existing or {}).get("id") or core_util._new_public_id("identity"),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
        "replaced_at": now,
        "relationship_started_at": _relationship_anchor_from_output(store, output, days_int),
        "relationship_anchor_source": GENESIS_SOURCE,
        "relationship_anchor_evidence": evidence,
        "identity_agent_name_present": bool(merged_payload.get("agent_name")),
        "identity_dimension_count": len(merged_payload.get("dimensions") or []),
    }
    if envelope.get("K_enclave"):
        identity_doc["K_enclave"] = envelope["K_enclave"]
    identity_service._save_identity(store, identity_doc)
    boot_gates._log_bootstrap_event(store, "genesis_identity_written_v1", success=True)
    identity_service._append_identity_change(store, {
        "action": "replace" if existing else "init",
        "reason": "Identity updated from Genesis import." if existing else "Identity initialized from Genesis import.",
    })
    return "updated" if existing else "initialized"


def _relationship_anchor_fields_for_replace(existing: dict, output: dict) -> dict:
    """B2: choose the relationship anchor for an identity replace.

    Default = PRESERVE the existing anchor (an omitted/empty upload must never wipe or
    reset relationship history). ONLY overwrite when the upload carries an EXPLICIT,
    valid relationship time — a real ISO date PLUS non-empty evidence — so a vague
    model-derived phrase can't silently reset the anchor (Seven's legality guard)."""
    anchor = output.get("relationship_anchor") if isinstance(output.get("relationship_anchor"), dict) else {}
    started = str(anchor.get("relationship_started_at") or "").strip()
    evidence = str(anchor.get("relationship_anchor_evidence") or "").strip()
    valid_date = False
    if started:
        try:
            date.fromisoformat(started)
            valid_date = True
        except ValueError:
            valid_date = False
    if valid_date and evidence:
        return {
            "relationship_started_at": started,
            "relationship_anchor_source": str(anchor.get("relationship_anchor_source") or "upload").strip() or "upload",
            "relationship_anchor_evidence": evidence,
        }
    return {
        "relationship_started_at": existing.get("relationship_started_at", ""),
        "relationship_anchor_source": existing.get("relationship_anchor_source", ""),
        "relationship_anchor_evidence": existing.get("relationship_anchor_evidence", ""),
    }


def replace_identity_preserving_anchor(store: UserStore, output: dict) -> str:
    """Create or replace identity content for explicit update_identity imports.

    With no existing card, reuse the Genesis initialization path so an uploaded
    role card is a true create-or-update operation. With an existing card, the
    relationship anchor (relationship_started_at/source/evidence) is PRESERVED
    by default and only overwritten when the upload carries an explicit, valid
    relationship time — see ``_relationship_anchor_fields_for_replace`` (B2).
    """
    existing = identity_service._load_identity(store)
    if not existing:
        init_output = dict(output)
        anchor = output.get("relationship_anchor")
        if isinstance(anchor, dict):
            for key in (
                "relationship_started_at",
                "days_with_user",
                "relationship_anchor_evidence",
            ):
                if key not in init_output and key in anchor:
                    init_output[key] = anchor[key]
        return init_identity_if_absent(store, init_output)
    payload = _identity_payload_for_replace(output)
    if not payload:
        return "not_provided"
    if not _identity_replace_payload_has_content(payload):
        return "identity_update_empty"
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        item_id=existing.get("id") or None,
    )
    if envelope is None:
        raise RuntimeError(f"identity_envelope_failed:{err}")
    now = datetime.now().isoformat()
    identity_doc = {
        **existing,
        "v": 1,
        "id": existing.get("id") or envelope.get("id") or core_util._new_public_id("identity"),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", existing.get("enclave_pk_fpr", "")),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        "replaced_at": now,
        **_relationship_anchor_fields_for_replace(existing, output),
        "identity_agent_name_present": bool(payload.get("agent_name")),
        "identity_dimension_count": len(payload.get("dimensions") or []),
    }
    if envelope.get("K_enclave"):
        identity_doc["K_enclave"] = envelope["K_enclave"]
    identity_service._save_identity(store, identity_doc)
    boot_gates._log_bootstrap_event(store, "genesis_identity_replaced_v1", success=True)
    identity_service._append_identity_change(store, {
        "action": "replace",
        "reason": "Identity replaced from explicit Genesis identity update.",
    })
    return "updated"


def write_persona_artifact(store: UserStore, job_id: str, output: dict) -> tuple[str, str]:
    content, prompt_version = _persona_content_from_output(output)
    if not content:
        return "", ""
    digest = _sha256_hex(content.encode("utf-8"))
    source_family = _persona_source_family_from_output(output)
    source_kind = _text(output.get("source_kind"), 80)
    new_priority = _persona_source_priority(source_family)
    existing = db.get_blob(store.user_id, GENESIS_PERSONA_BLOB)
    if isinstance(existing, dict):
        try:
            existing_priority = int(existing.get("source_priority") or 0)
        except Exception:
            existing_priority = 0
        if existing_priority > new_priority:
            return GENESIS_PERSONA_REF, str(existing.get("sha256") or "")
    now = _now_iso()
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store,
        content.encode("utf-8"),
        item_id=f"genesis_persona_{job_id}",
    )
    if envelope is None:
        raise RuntimeError(f"persona_envelope_failed:{err}")
    db.set_blob(store.user_id, GENESIS_PERSONA_BLOB, {
        "v": 1,
        "job_id": job_id,
        "source": GENESIS_SOURCE,
        "encrypted": True,
        "content_envelope": envelope,
        "sha256": digest,
        "prompt_version": prompt_version,
        "source_kind": source_kind,
        "source_family": source_family,
        "source_priority": new_priority,
        "created_at": now,
        "updated_at": now,
    })
    return GENESIS_PERSONA_REF, digest


def write_voice_artifact(store: UserStore, job_id: str, output: dict) -> tuple[str, str]:
    voice_doc = _safe_voice_workset(output)
    if not voice_doc:
        return "", ""
    raw = json.dumps(voice_doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = _sha256_hex(raw)
    now = _now_iso()
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store,
        raw,
        item_id=f"genesis_voice_{job_id}",
    )
    if envelope is None:
        raise RuntimeError(f"voice_envelope_failed:{err}")
    founding_count = len([item for item in voice_doc["exemplars"] if item.get("founding")])
    db.set_blob(store.user_id, GENESIS_VOICE_BLOB, {
        "v": 1,
        "job_id": job_id,
        "source": GENESIS_SOURCE,
        "encrypted": True,
        "content_envelope": envelope,
        "sha256": digest,
        "source_kind": voice_doc["source_kind"],
        "source_family": voice_doc["source_family"],
        "behavior_note_count": len(voice_doc["behavior_notes"]),
        "exemplar_count": len(voice_doc["exemplars"]),
        "founding_exemplar_count": founding_count,
        "created_at": now,
        "updated_at": now,
    })
    return GENESIS_VOICE_REF, digest


def apply_reducer_output(
    store: UserStore,
    api_key: str | None,
    job_id: str,
    output: dict,
    *,
    runtime_token: str = "",
) -> dict:
    job = db.genesis_get_job(store.user_id, job_id)
    if not job:
        raise LookupError("genesis_job_not_found")
    output = dict(output)
    _reject_raw_reducer_fields(output)
    output["job_id"] = job_id
    db.genesis_set_job_status(store.user_id, job_id, status="processing", output={"stage": "apply_outputs"})
    write_genesis_state(store, {**job, "status": "processing"})
    # this run is starting -> clear any stale failure/partial notice for this
    # user's genesis flow *before* we emit any new ones below, so a partial
    # notice emitted later in this same call doesn't get resolved by its own
    # run (notices emitted with dedupe_key="genesis:{job_id}:partial" also
    # match the "genesis:" prefix used here).
    notices.resolve(store, "genesis:")
    memory_count, memory_results = apply_memory_outputs(store, api_key, output)
    # apply_memory_outputs has no job_id in its signature (many other call sites
    # depend on its (count, results) 2-tuple return, incl. direct unpack in
    # tests/test_genesis_service.py — widening it would ripple through those).
    # This caller DOES have job_id, so the dropped-card count is derived here
    # instead: raw input count minus what actually landed (covers both the
    # ValueError-skipped malformed items inside apply_memory_outputs AND any
    # non-dict items it silently continues past).
    raw_items = output.get("memories")
    if raw_items is None:
        raw_items = output.get("facts")
    raw_count = len(raw_items) if isinstance(raw_items, list) else 0
    dropped = raw_count - memory_count
    if dropped > 0:
        notices.emit(store, source="genesis", error_class="genesis_partial",
                     blame="system", severity="warning",
                     user_text=catalog.user_text_for("genesis_partial"),
                     detail=f"dropped {dropped} card(s)",
                     dedupe_key=f"genesis:{job_id}:partial")
    identity_status = init_identity_if_absent(store, output, api_key, runtime_token)
    persona_ref, persona_sha = write_persona_artifact(store, job_id, output)
    voice_ref, voice_sha = write_voice_artifact(store, job_id, output)
    result_doc = {
        "memory_action_count": memory_count,
        "memory_results": memory_results,
        "identity_status": identity_status,
        "persona_ref": persona_ref,
        "persona_sha256": persona_sha,
        "voice_ref": voice_ref,
        "voice_sha256": voice_sha,
    }
    db.genesis_upsert_output(
        store.user_id,
        job_id,
        "reducer",
        doc=_safe_reducer_doc(job_id, output),
        status="applied",
        ref="sanitized",
    )
    db.genesis_upsert_output(store.user_id, job_id, "apply", doc=result_doc, status="done", ref="inline")
    completed = db.genesis_complete_job(
        store.user_id,
        job_id,
        output=result_doc,
        memory_action_count=memory_count,
        identity_status=identity_status,
        persona_ref=persona_ref,
        persona_sha256=persona_sha,
    )
    if completed:
        write_genesis_state(store, completed, status=DONE_JOB_STATUS)
    return result_doc
