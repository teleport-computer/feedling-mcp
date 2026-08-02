"""Genesis import service: state blobs, ledger helpers, reducer application."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from datetime import datetime, date
from typing import Any

import httpx

import db
from bootstrap import gates as boot_gates
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import util as core_util
from core.store import UserStore
from identity import card_policy
from identity.user_naming import sanitize_user_name
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

# --- T16: genesis failure classification (onboarding observability) ----------
# A fixed, closed enum the app/support tooling can switch on, independent of
# the raw `error` string's exact wording (which changes freely as worker.py's
# call sites evolve). `error` is NEVER replaced — error_code/error_hint are
# additive fields alongside it (write_genesis_state below).
#
# V2 migration note: `backend/genesis/worker.py` on `pre` runs inside the
# serve-worker thread pool (+ `backend/model_api_runtime/v2/daemon.py`) instead
# of the standalone CVM worker loop this file's `tick()` drives, but it raises
# the SAME GenesisWorkerError/ProviderError shapes for the same failure kinds
# (JSON parse, provider 401/403/429, timeouts, stale-reap). This enum + hints
# dict has exactly one copy — classify_genesis_error/GENESIS_ERROR_HINTS here
# — so the 2026-07-27 test→pre merge should see zero conflict on this section;
# pre's daemon.py only needs to call the same write_genesis_state/mark_failed
# seam, not duplicate the classification logic.
GENESIS_ERROR_CODES = (
    "bad_api_key",
    "provider_timeout",
    "provider_quota",
    "model_bad_json",
    "model_empty_output",
    "worker_restarted",
    "consumer_offline",
    "decrypt_failed",
    "internal",
)

GENESIS_ERROR_HINTS: dict[str, str] = {
    "bad_api_key": "模型 API key 无效或无权限,检查 key",
    # usr_9037eaa8 (2026-07-24): a relay "thinking" model timed out 15+ times
    # in a row; the old "稍后重试" hint sent the user retrying into the same
    # wall. Name the two dominant real causes so the fix is actionable.
    "provider_timeout": "模型响应超时——thinking/慢速模型或不稳定中转最常见;换更快的模型或更稳的服务后重试",
    "provider_quota": "模型额度用尽,检查账户额度",
    "model_bad_json": "模型输出的格式坏了,已重试仍失败——换个模型或重试一次",
    "model_empty_output": "模型没有产出内容,重试或换模型",
    "worker_restarted": "服务重启打断了生成,已自动重新排队",
    # consumer_offline is a VPS resident-lane value: defined here for contract
    # completeness (the app/support UI can already switch on it), but NOT
    # wired below — no real raise site emits a distinguishable "consumer is
    # offline" error string for a genesis job today (see classify_genesis_error
    # docstring). Do not fabricate a match just to light this one up.
    "consumer_offline": "你的 VPS resident consumer 离线了,请检查并重启",
    "decrypt_failed": "解密失败,可能是密钥或运行环境问题,请重试或联系支持",
    "internal": "内部错误,请稍后重试",
}

# English mirror of GENESIS_ERROR_HINTS. The hosted onboarding checklist is
# read by clients that render `required` verbatim, so the failure line ships
# both languages; keep the two dicts key-identical (contract-tested).
GENESIS_ERROR_HINTS_EN: dict[str, str] = {
    "bad_api_key": "the model API key is invalid or unauthorized — check the key",
    "provider_timeout": (
        "the model timed out — thinking/slow models and unstable relays are "
        "the usual cause; switch to a faster model or steadier provider, then retry"
    ),
    "provider_quota": "model quota/credits exhausted — top up or switch keys",
    "model_bad_json": "the model kept returning malformed output — retry once or switch models",
    "model_empty_output": "the model produced no usable output — retry or switch models",
    "worker_restarted": "a service restart interrupted the job; it was re-queued automatically",
    "consumer_offline": "your VPS resident consumer is offline — check and restart it",
    "decrypt_failed": "decryption failed — likely a key/runtime issue; retry or contact support",
    "internal": "internal error — retry later",
}


def genesis_failure_required_text(error: str) -> str:
    """Bilingual, cause-aware `required` line for the onboarding checklist.

    Replaces the old static "Start onboarding again with the latest app build"
    — which named the wrong fix for every real failure cause (usr_9037eaa8,
    2026-07-24: five provider-timeout jobs answered with "update the app").
    Imported materials survive a failed job, so the action is always "fix the
    cause, then restart Genesis", never a reinstall/update."""
    code = classify_genesis_error(error)
    zh = GENESIS_ERROR_HINTS.get(code, GENESIS_ERROR_HINTS["internal"])
    en = GENESIS_ERROR_HINTS_EN.get(code, GENESIS_ERROR_HINTS_EN["internal"])
    # User-facing vocabulary: "文件解读", never the internal term "蒸馏"
    # (Seven, 2026-07-24).
    return (
        f"文件解读失败:{zh}。处理后在 App 里重新发起导入即可,已上传的材料不会丢。"
        f" / Reading your onboarding materials failed: {en}. Then restart the "
        "import from the app — your uploaded materials are kept."
    )

_BAD_API_KEY_STATUS = frozenset({401, 403})
_PROVIDER_QUOTA_STATUS = frozenset({402, 429})


def classify_genesis_error(error: str, exc: BaseException | None = None) -> str:
    """Classify a genesis job failure into a fixed enum (GENESIS_ERROR_CODES).

    Pure string matching against the RAW error text already stored in
    job.error / mark_failed's `error` argument — it never invents or discards
    that string, it only labels it. `exc` is optional and only sharpens two
    cases (provider status_code, httpx timeout type) when the caller happens
    to still hold the live exception (e.g. worker.tick()'s except block);
    every raise site's message is ALSO string-matched below so classification
    still works from the persisted string alone (the reaper paths write
    `error` via raw SQL and never have an exception object at all).

    Real raise-string survey (backend/genesis/worker.py + provider_client.py,
    2026-07-23):
      - `{task_id}:invalid_json` / `:json_not_object` / `:invalid_json_after_repair`
        (worker._json_object / _complete_json's repair-then-give-up path)
        -> model_bad_json.
      - `all_fact_maps_failed:N/M:{cause}` (_build_reducer_output's "every
        chunk's fact-map failed" floor check) -> {cause} directly, where
        {cause} is one of bad_api_key/provider_quota/provider_timeout/
        model_bad_json/internal — worker._classify_fact_map_failures picks
        it by priority across every fact-map exception in the batch, so a
        wall of 401s classifies as bad_api_key, not "no output". The bare
        legacy form `all_fact_maps_failed:N/M` (no cause suffix, from jobs
        that failed before this fix landed) still -> model_empty_output.
        NOTE: the brief's
        "_complete_json_retry_empty 耗尽" does not itself raise a distinct
        string — when every retry attempt errors, it re-raises the LAST
        GenesisWorkerError, which is already one of the invalid_json family
        above; when every attempt instead returns a valid-but-empty JSON body
        (is_empty() true, no exception), the loop returns quietly and the
        caller proceeds with an empty result. all_fact_maps_failed is the
        real, reachable "nothing usable came back" failure signal.
      - `provider_http_401` / `_403` (provider_client._raise_for_provider_status)
        -> bad_api_key; `_402` / `_429` -> provider_quota (402 = out of
        credits, folded into the quota bucket per its user-facing meaning).
      - httpx timeout / "TimeoutException" in the wrapped message (worker
        call sites wrap as f"...:{type(e).__name__}") -> provider_timeout.
      - `genesis_stale_timeout:...` / `resident_stale_timeout:...` /
        `resident_never_claimed:...` (the three stale-processing reapers in
        worker.py) -> worker_restarted: the job was requeued/failed because
        the worker/consumer that held it stopped heartbeating, not because of
        anything the model produced.
      - `...decrypt_failed:{type}` (worker._decrypt_envelope, the enclave
        envelope-decrypt call) -> decrypt_failed.
      - anything else -> internal.
    """
    text = str(error or "")
    lower = text.lower()

    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code in _BAD_API_KEY_STATUS:
            return "bad_api_key"
        if status_code in _PROVIDER_QUOTA_STATUS:
            return "provider_quota"
    if exc is not None and isinstance(exc, httpx.TimeoutException):
        return "provider_timeout"

    # Stale-reap requeue/fail strings all contain "timeout" too (e.g.
    # "genesis_stale_timeout:1800s") — must be checked before the generic
    # timeout match below so a dead worker isn't mislabeled as a slow provider.
    if "stale_timeout" in lower or "resident_never_claimed" in lower:
        return "worker_restarted"

    status_match = re.search(r"provider_http_(\d{3})\b", lower)
    if status_match:
        code = int(status_match.group(1))
        if code in _BAD_API_KEY_STATUS:
            return "bad_api_key"
        if code in _PROVIDER_QUOTA_STATUS:
            return "provider_quota"

    if (
        "invalid_json_after_repair" in lower
        or "invalid_json" in lower
        or "json_not_object" in lower
    ):
        return "model_bad_json"

    # I6: worker._classify_fact_map_failures appends the real cause it picked
    # (by priority across every fact-map exception in the batch) as a third
    # colon-segment — check that BEFORE the bare-string fallback below so a
    # wall of 401s/429s/timeouts/bad-json keeps its real classification
    # instead of collapsing to model_empty_output. Legacy strings persisted
    # before this fix (no cause suffix) fall through to the old behavior.
    fact_map_cause = re.search(
        r"all_fact_maps_failed:\d+/\d+:(bad_api_key|provider_quota|provider_timeout|model_bad_json|internal)",
        lower,
    )
    if fact_map_cause:
        return fact_map_cause.group(1)

    if "all_fact_maps_failed" in lower:
        return "model_empty_output"

    # Checked BEFORE the generic timeout substring below: an enclave
    # envelope-decrypt call that happens to fail with a timeout is still
    # fundamentally a decrypt failure (worker._decrypt_envelope's own raise
    # site), not a provider/LLM timeout.
    if "decrypt_failed" in lower:
        return "decrypt_failed"

    if "timeout" in lower:
        return "provider_timeout"

    return "internal"


def _claimed_age_sec(job: dict) -> int | None:
    """Seconds since this job's row last moved — real, not fabricated: prefers
    `resident_claimed_at` (VPS-lane claim timestamp) and falls back to
    `updated_at` (bumped on every LLM call heartbeat AND on cloud-worker
    claim — see genesis_claim_uploaded_jobs / GenesisLLMClient.complete).
    Returns None when neither is present/parseable so callers omit the field
    instead of writing a fake age."""
    for key in ("resident_claimed_at", "updated_at"):
        raw = job.get(key)
        if not raw:
            continue
        epoch = core_util._to_epoch(raw)
        if epoch > 0:
            return max(0, int(time.time() - epoch))
    return None

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
    shape_error = core_envelope.validate_uploaded_envelope(
        envelope_meta, user_id=store.user_id)
    if shape_error is not None:
        raise ValueError(str(shape_error.get("error") or "chunk_envelope_invalid"))
    if envelope_meta.get("body") is not None:
        if str(envelope_meta.get("body") or "").encode("utf-8") != encrypted_body:
            raise ValueError("chunk_envelope_body_mismatch")
        return {
            "v": int(envelope_meta.get("v") or 1),
            "id": _text(envelope_meta.get("id"), 160),
            "visibility": "shared", "owner_user_id": store.user_id,
            "body_object_format": "plaintext_v1",
        }
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
    if meta.get("body_object_format") == "plaintext_v1":
        clean = {k: v for k, v in meta.items() if k != "body_object_format"}
        return {**clean, "body": bytes(encrypted_body).decode("utf-8")}
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


def write_genesis_state(
    store: UserStore, job: dict, *, status: str | None = None, exc: BaseException | None = None,
) -> dict:
    job_status = str(job.get("status") or "")
    resolved_status = status or gate_status_for_job_status(job_status)
    state = {
        "v": 1,
        "status": resolved_status,
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
        # `error` stays the raw string, unchanged — T16 adds error_code/error_hint
        # alongside it below, additive only (existing consumers reading only
        # `error`/`status` are unaffected). Same seam covers BOTH mark_failed()
        # callers AND the three stale-reap paths in worker.py, which write
        # `error` via raw SQL and call this function directly without going
        # through mark_failed at all.
        "error": str(job.get("error") or ""),
        "privacy_mode": str(job.get("privacy_mode") or PRIVACY_MODE),
    }
    if resolved_status == FAILED_JOB_STATUS:
        error_code = classify_genesis_error(state["error"], exc)
        state["error_code"] = error_code
        state["error_hint"] = GENESIS_ERROR_HINTS.get(error_code, GENESIS_ERROR_HINTS["internal"])
    elif resolved_status == "processing":
        # Best-effort "why is this still processing" signal for a wedged job —
        # only added when cheaply available on the job dict already loaded for
        # this write; never fabricated (see _claimed_age_sec docstring).
        claimed_by = str(job.get("resident_consumer_id") or "").strip()
        if claimed_by:
            state["worker_claimed_by"] = claimed_by
        claimed_age_sec = _claimed_age_sec(job)
        if claimed_age_sec is not None:
            state["claimed_age_sec"] = claimed_age_sec
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


def mark_failed(
    store: UserStore, job_id: str, error: str, *, exc: BaseException | None = None,
) -> dict | None:
    # `exc` (optional): pass the live exception when the caller still has it
    # (e.g. worker.tick()'s `except Exception as e`) so classify_genesis_error
    # can use its status_code/type instead of only re-parsing `error`'s text.
    # V2 note: pre's serve-worker/daemon.py equivalent should pass its own
    # caught exception through the same way when it lands this call.
    job = db.genesis_set_job_status(store.user_id, job_id, status=FAILED_JOB_STATUS, error=error)
    if job:
        write_genesis_state(store, job, status=FAILED_JOB_STATUS, exc=exc)
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


def _normalized_memory_date(value: Any) -> str:
    raw = _text(value, 80)
    if not raw:
        return ""
    parsed = identity_service._parse_iso_calendar_date(raw)
    return parsed.isoformat() if parsed else ""


def _memory_output_fallback_occurred_at(output: dict, fallback_occurred_at: str = "") -> str:
    explicit = _normalized_memory_date(fallback_occurred_at)
    if explicit:
        return explicit
    top_level = _normalized_memory_date(output.get("relationship_started_at"))
    if top_level:
        return top_level
    anchor = output.get("relationship_anchor") if isinstance(output.get("relationship_anchor"), dict) else {}
    return _normalized_memory_date(anchor.get("relationship_started_at"))


def _memory_output_preserves_dates(output: dict, preserve_dates: bool) -> bool:
    if preserve_dates:
        return True
    return str(output.get("source_family") or "").strip() == "memory_summary"


def _memory_item_preserves_dates(item: dict, output_preserve_dates: bool) -> bool:
    if output_preserve_dates:
        return True
    return str(item.get("_source_family") or item.get("source_family") or "").strip() == "memory_summary"


def _memory_threads_from_output(item: dict, *, preserve_tags: bool = False) -> list:
    raw_threads = item.get("threads") if isinstance(item.get("threads"), list) else []
    if not preserve_tags:
        return raw_threads

    values: list[Any] = list(raw_threads)
    raw_tags = item.get("tags")
    if isinstance(raw_tags, list):
        values.extend(raw_tags)
    elif isinstance(raw_tags, str):
        values.extend(part for part in re.split(r"[,，、\n]+", raw_tags) if part.strip())

    seen: set[str] = set()
    threads: list[str] = []
    for value in values:
        clean = _text(value, 80)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        threads.append(clean)
    return threads[:16]


def _memory_occurred_at_from_output(
    item: dict,
    *,
    preserve_dates: bool = False,
    fallback_occurred_at: str = "",
) -> str:
    if not preserve_dates:
        return _text(item.get("occurred_at"), 80)
    return (
        _normalized_memory_date(item.get("occurred_at") or item.get("date"))
        or _normalized_memory_date(fallback_occurred_at)
    )


def _memory_action_from_output(
    item: dict,
    *,
    preserve_dates: bool = False,
    fallback_occurred_at: str = "",
) -> dict:
    mem_type = _coerce_memory_type(item.get("type"))
    memory = {
        "type": mem_type,
        "summary": _text(item.get("summary") or item.get("title") or item.get("description"), 2000),
        "content": str(item.get("content") or "").strip()[:5000],
        "bucket": _text(item.get("bucket"), 80),
        "threads": _memory_threads_from_output(item, preserve_tags=preserve_dates),
        "occurred_at": _memory_occurred_at_from_output(
            item,
            preserve_dates=preserve_dates,
            fallback_occurred_at=fallback_occurred_at,
        ),
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


def apply_memory_outputs(
    store: UserStore,
    api_key: str | None,
    output: dict,
    *,
    preserve_dates: bool = False,
    fallback_occurred_at: str = "",
) -> tuple[int, list[dict]]:
    raw_items = output.get("memories")
    if raw_items is None:
        raw_items = output.get("facts")
    if not isinstance(raw_items, list) or not raw_items:
        return 0, []
    output_preserve_dates = _memory_output_preserves_dates(output, preserve_dates)
    effective_fallback_occurred_at = _memory_output_fallback_occurred_at(output, fallback_occurred_at)
    actions: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item_preserve_dates = _memory_item_preserves_dates(item, output_preserve_dates)
        try:
            actions.append(_memory_action_from_output(
                item,
                preserve_dates=item_preserve_dates,
                fallback_occurred_at=effective_fallback_occurred_at,
            ))
        except ValueError:
            # LLM reducers can occasionally emit a partial memory object. Keep the
            # import alive and write the valid cards instead of failing the whole job.
            continue
    if not actions:
        return 0, []
    results: list[dict] = []
    written = 0
    for idx in range(0, len(actions), 20):
        batch = actions[idx:idx + 20]
        body, status = memory_actions._execute_memory_actions(store, api_key, batch)
        if status >= 400:
            raise RuntimeError(f"memory_actions_failed:{body.get('error', status)}")
        rows = list(body.get("results") or [])
        results.extend(rows)
        batch_written = (
            int(body.get("applied_count") or 0)
            if "applied_count" in body
            else sum(
                1
                for row in rows
                if not isinstance(row, dict)
                or str(row.get("status") or "").strip().lower() != "error"
            )
        )
        written += batch_written
        batch_failed = (
            int(body.get("failed_count") or 0)
            if "failed_count" in body
            else sum(
                1
                for row in rows
                if isinstance(row, dict)
                and str(row.get("status") or "").strip().lower() == "error"
            )
        )
        if batch_failed == len(batch):
            first_error = next(
                (
                    str(row.get("error") or "memory_action_failed")
                    for row in rows
                    if isinstance(row, dict)
                    and str(row.get("status") or "") == "error"
                ),
                "memory_action_failed",
            )
            raise RuntimeError(f"memory_actions_failed:{first_error}")
    return written, results


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
        raw_value = dim.get("value", 50)
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raw_value = 50
        # Same 0–100 int contract as card_policy.sanitize: a BYOK weak model that
        # emits a 0–1-scale float (0.95) must be rescaled to 95, not int()-truncated
        # to 0 (silently zeroing the whole card) and never stored as a raw float.
        clean_dims.append({
            "name": name,
            "value": card_policy.normalize_dimension_value(raw_value),
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
    user_name = sanitize_user_name(identity.get("user_preferred_name"))
    if user_name != "TA":
        payload["user_preferred_name"] = user_name
    # B2: the 4 remaining user-layer fields (D1) — GROUNDED, so absence/empty in
    # `identity` (the distiller's own output) just means no signal, never invented
    # here. Same 1200/240 cap convention as the rest of this module (relationship_anchor/
    # tone_style/custom_persona_prompt get the long-text cap).
    for key in ("custom_persona_prompt", "language_preference", "relationship_anchor"):
        value = _text(identity.get(key), 1200 if key in {"relationship_anchor", "custom_persona_prompt"} else 240)
        if value:
            payload[key] = value
    stable_defs = identity.get("stable_definitions")
    if isinstance(stable_defs, list):
        clean_defs = [_text(item, 240) for item in stable_defs[:12]]
        clean_defs = [item for item in clean_defs if item]
        if clean_defs:
            payload["stable_definitions"] = clean_defs
    has_user_layer_signal = bool(
        payload.get("user_preferred_name") or payload.get("custom_persona_prompt")
        or payload.get("language_preference") or payload.get("relationship_anchor")
        or payload.get("stable_definitions")
    )
    if not payload["agent_name"] and not payload["dimensions"] and not has_user_layer_signal:
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
    """True iff `payload` carries ANY writable profile signal — dimensions, or
    any card_policy PROFILE_FIELDS entry (agent_name/self_introduction/category/
    signature, but ALSO tone_style/agent_role/custom_persona_prompt/
    language_preference/relationship_anchor/do_not_say/boundaries/
    stable_definitions/user_preferred_name).

    Reuses identity_service._IDENTITY_PROFILE_FIELDS (== card_policy.PROFILE_FIELDS)
    as the single source of truth — same list _merge_identity_replace_payload
    iterates — instead of a hand-picked subset. A hand-picked subset previously
    missed exactly the user-authored fields this task exists to protect: a
    redistill whose only change was e.g. custom_persona_prompt or tone_style
    was rejected as `identity_update_empty` (fails closed, no data loss, but
    silently drops a legitimate update) — reproduced and fixed post-review."""
    if isinstance(payload.get("dimensions"), list) and payload["dimensions"]:
        return True
    for key in identity_service._IDENTITY_PROFILE_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and value:
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
    # B2: thread the 5 user-layer fields the distiller may have derived (GROUNDED —
    # `payload` only carries a key here when `_identity_payload_from_output` found
    # explicit material signal for it). Previously `user_preferred_name` was already
    # computed above but silently dropped here — this fixes that alongside adding
    # the other 4.
    for key in ("user_preferred_name", "custom_persona_prompt", "language_preference",
                "relationship_anchor", "stable_definitions"):
        if payload.get(key):
            merged_payload[key] = payload[key]
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
        "enclave_pk_fpr": "",
        **core_envelope.envelope_storage_fields(envelope),
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
    # KNOWN RESIDUAL (not fixed here — same shape as identity/actions.py's
    # _identity_relationship_days_set, see that comment): this is a plain
    # overwrite, not the CAS `_save_identity_cas` used by profile_patch /
    # dimension_nudge / replace_identity_preserving_anchor, and it isn't under
    # identity_mutation_lock either. The `existing`/`base_payload` snapshot
    # above was read at the TOP of this function, so a concurrent
    # profile_patch/dimension_nudge CAS win landing after that read (this
    # function's own enclave round trip for `_existing_identity_plain_for_update`
    # included) can be silently clobbered here — and this can happen even when
    # a card ALREADY exists (the `existing and relationship_anchor_source ==
    # GENESIS_SOURCE` re-init branch above), not just on first init. Low
    # real-world likelihood (genesis init/re-init is a one-shot onboarding
    # path, not a hot path) but worth flagging alongside the C1 fix rather
    # than presenting profile_patch/dimension_nudge/rewrap as the only
    # writers on this row — relationship_days_set (identity/actions.py) has
    # the same residual.
    identity_service._save_identity(store, identity_doc)
    boot_gates._log_bootstrap_event(store, "genesis_identity_written_v1", success=True)
    identity_service._append_identity_change(store, {
        "action": "replace" if existing else "init",
        "reason": "Identity updated from Genesis import." if existing else "Identity initialized from Genesis import.",
    })
    return "updated" if existing else "initialized"


def _merge_identity_replace_payload(existing_plain: dict, distilled: dict) -> dict:
    """T12 (spec 3.6 / D5): key-level overlay of a distilled REPLACE payload onto
    the LATEST decrypted card, so a field the distill lane didn't address is
    NEVER wiped ("没提的字段永不丢失"). `existing_plain` must be freshly
    decrypted at write time (see replace_identity_preserving_anchor) — not a
    snapshot the consumer's prompt-building read earlier, which may already be
    stale by the time this lands.

    Rule: a distilled value WINS only when it actually addresses the field
    (non-empty string / non-empty list); an absent or blank distilled field
    falls back to the existing card's value untouched. Iterates
    card_policy's own field lists (PROFILE_STRING_FIELDS / PROFILE_LIST_FIELDS)
    instead of a hand-copied subset — the single-source-of-truth discipline
    the module docstring on card_policy.py calls out (hand-copied field lists
    have silently dropped user-authored fields like custom_persona_prompt
    before)."""
    merged = _identity_payload_from_existing_plain(existing_plain)
    for key in identity_service._IDENTITY_PROFILE_STRING_FIELDS:
        value = distilled.get(key)
        if isinstance(value, str) and value.strip():
            merged[key] = value
    if isinstance(distilled.get("dimensions"), list) and distilled["dimensions"]:
        merged["dimensions"] = distilled["dimensions"]
    for key in identity_service._IDENTITY_PROFILE_LIST_FIELDS:
        value = distilled.get(key)
        if isinstance(value, list) and value:
            merged[key] = value
    return merged


# Bounded retry count for replace_identity_preserving_anchor's CAS-conflict
# recovery — mirrors identity/actions.py's _IDENTITY_WRITE_MAX_ATTEMPTS (same
# reasoning: 3 is generous for 2 gunicorn workers racing the same user).
_IDENTITY_REPLACE_MAX_ATTEMPTS = 3


def _relationship_anchor_fields_for_replace(existing: dict, output: dict) -> dict:
    """B2: choose the relationship anchor for an identity replace.

    Default = PRESERVE the existing anchor (an omitted/empty upload must never wipe or
    reset relationship history). ONLY overwrite when the upload carries an EXPLICIT,
    valid relationship time — a real ISO date PLUS non-empty evidence — so a vague
    model-derived phrase can't silently reset the anchor (Seven's legality guard)."""
    preserved = {
        "relationship_started_at": existing.get("relationship_started_at", ""),
        "relationship_anchor_source": existing.get("relationship_anchor_source", ""),
        "relationship_anchor_evidence": existing.get("relationship_anchor_evidence", ""),
    }
    # An anchor the USER explicitly calibrated (source user_calibrated) outranks
    # anything a replace/redistill could derive from an upload, so it must NEVER
    # be overwritten here. Without this guard, re-uploading a persona doc that
    # restated a duration would silently reset a day count the user had
    # deliberately corrected. Existing card says user_calibrated -> preserve it,
    # full stop.
    if str(existing.get("relationship_anchor_source") or "") == "user_calibrated":
        return preserved
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
    return preserved


def replace_identity_preserving_anchor(
    store: UserStore,
    output: dict,
    api_key: str | None = None,
    runtime_token: str = "",
) -> str:
    """Create or replace identity content for explicit update_identity imports
    AND the resident-distill (redistill) lane's identity.replace landing point.

    With no existing card, reuse the Genesis initialization path so an uploaded
    role card is a true create-or-update operation. With an existing card, the
    relationship anchor (relationship_started_at/source/evidence) is PRESERVED
    by default and only overwritten when the upload carries an explicit, valid
    relationship time — see ``_relationship_anchor_fields_for_replace`` (B2).

    T12 (spec 3.6 / D5): the distilled payload is a KEY-LEVEL OVERLAY onto the
    LATEST decrypted card, computed at write time — not the caller's own
    (possibly stale, pre-job) view of the card. This closes the same
    lost-update / dropped-field class of bug the identity-actions CAS wave
    (identity/actions.py::_with_identity_mutation_lock_and_retry) closed for
    profile_patch / dimension_nudge: the whole read-latest -> merge -> encrypt
    -> CAS-write span runs under the per-user identity_mutation_lock, and the
    write itself is a compare-and-swap against a snapshot taken at the START
    of that same span (identity_service._save_identity_cas), so a concurrent
    profile_patch/dimension_nudge/replace landing mid-span makes the CAS fail
    and this function retries from a fresh read rather than silently
    clobbering it. This retires the prior plain-overwrite behavior noted as a
    KNOWN RESIDUAL in identity/actions.py::_identity_replace_action.
    """
    if not identity_service._load_identity(store):
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
        return init_identity_if_absent(store, init_output, api_key, runtime_token)

    distilled = _identity_payload_for_replace(output)
    if not distilled:
        return "not_provided"
    if not _identity_replace_payload_has_content(distilled):
        return "identity_update_empty"

    with identity_service.identity_mutation_lock(store.user_id):
        for attempt in range(_IDENTITY_REPLACE_MAX_ATTEMPTS):
            # Snapshot the raw blob FIRST — before the enclave plaintext read —
            # so it covers the ENTIRE span as the CAS `expected` value,
            # including the enclave round trip itself (same ordering rationale
            # as identity/actions.py::_load_identity_snapshot_for_write; a
            # write landing during the enclave call must fail the CAS, not be
            # silently clobbered by it).
            snapshot = identity_service._load_identity(store)
            if not snapshot:
                return "identity_not_initialized"
            existing_plain, _plain_err = _existing_identity_plain_for_update(api_key, runtime_token)
            if existing_plain is None:
                return "identity_plain_unavailable"

            merged = _merge_identity_replace_payload(existing_plain, distilled)
            envelope, err = core_envelope._build_shared_envelope_for_store(
                store,
                json.dumps(merged, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                item_id=snapshot.get("id") or None,
            )
            if envelope is None:
                raise RuntimeError(f"identity_envelope_failed:{err}")
            now = datetime.now().isoformat()
            identity_doc = {
                **snapshot,
                "v": 1,
                "id": snapshot.get("id") or envelope.get("id") or core_util._new_public_id("identity"),
                "enclave_pk_fpr": snapshot.get("enclave_pk_fpr", ""),
                **core_envelope.envelope_storage_fields(envelope),
                "created_at": snapshot.get("created_at") or now,
                "updated_at": now,
                "replaced_at": now,
                **_relationship_anchor_fields_for_replace(snapshot, output),
                "identity_agent_name_present": bool(merged.get("agent_name")),
                "identity_dimension_count": len(merged.get("dimensions") or []),
            }
            if envelope.get("K_enclave"):
                identity_doc["K_enclave"] = envelope["K_enclave"]
            if identity_service._save_identity_cas(store, snapshot, identity_doc):
                boot_gates._log_bootstrap_event(store, "genesis_identity_replaced_v1", success=True)
                identity_service._append_identity_change(store, {
                    "action": "replace",
                    "reason": "Identity replaced from explicit Genesis identity update.",
                })
                return "updated"
            # CAS lost the race to a concurrent writer — retry the whole span
            # from a fresh read (identity_service.IdentityWriteConflict's
            # documented recovery path). Falls through the loop.
        return "identity_write_conflict"


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
