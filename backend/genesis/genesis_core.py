"""Framework-neutral genesis import operations (ASGI-migration plan §5.3).

A pure relocation of the Flask ``/v1/genesis/*`` route bodies so both the Flask
adapter (``genesis.routes``) and the native FastAPI router
(``genesis.routes_asgi``) share one implementation and return byte-identical
responses.

E2E boundary (unchanged): genesis chunks are v1 E2E envelopes / ciphertext; the
server NEVER decrypts them. Reads (``list`` / ``status``) are plain store ops.
``put_chunk`` persists ciphertext + envelope metadata as-is. ``finalize`` /
``apply_outputs`` / ``persona_backfill`` forward the caller's credential (api key
OR runtime token) to the enclave-owned apply/backfill paths exactly as Flask does
— these functions take the already-resolved credential as an argument and never
read ``flask.request``, so no new server-side plaintext is ever introduced here.

Background-worker discipline (plan §5.7): the plaintext import route ENQUEUES a
background distill job via the SAME mechanism Flask uses — the routes-resident
``_start_plaintext_genesis_job`` (a daemon thread) is injected here as
``start_job`` and merely spawned; the heavy ``_run_plaintext_genesis_job`` never
runs inline on the request path. ``persona_backfill`` likewise submits ONE genesis
import job that the supervisor/worker loop drains. All store / enclave / enqueue
work is blocking, so ASGI callers run these on the threadpool (plan §5.2).

The plaintext helper cluster + background machinery stay physically in
``genesis.routes`` (many tests patch them as ``routes._…`` and rely on internal
cross-call resolution), so the plaintext orchestration receives them via
dependency injection rather than importing the Flask module.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from typing import Any

import db
from genesis import plaintext as plaintext_helpers
from genesis import service
from hosted import history_import
from identity import service as identity_service
from notices import core as notices

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")

log = logging.getLogger("feedling.genesis.plaintext_import")

# Material fields a plaintext import can carry. We log only their CHAR LENGTHS
# (never the content) so a rejected import is diagnosable without leaking plaintext.
_PLAINTEXT_MATERIAL_FIELDS = (
    "content",
    "memory_summary_content",
    "support_material_content",
    "character_content",
    "ai_persona_content",
    "personal_profile_content",
)


def _plaintext_material_sizes(payload: dict) -> dict:
    """{field: char_len} for every non-empty material field. Lengths only, no content."""
    return {
        k: len(str(payload.get(k) or ""))
        for k in _PLAINTEXT_MATERIAL_FIELDS
        if str(payload.get(k) or "").strip()
    }


def _log_plaintext_import_rejected(store, *, mode: str, reason: str, payload: dict) -> None:
    """Always-on breadcrumb for a 400'd plaintext import. The high-signal case is
    ``material_present=True`` with a ``..._required`` reason: the user DID upload
    material but it normalized to empty (e.g. a memory archive whose items use a
    non-whitelisted narrative key) — the exact silent-drop this endpoint used to
    surface only as an opaque client-side "invalid request". Grep server logs for
    ``genesis.plaintext.rejected``. Best-effort; never breaks the request path."""
    try:
        sizes = _plaintext_material_sizes(payload)
        material_present = bool(sizes)
        uid = getattr(store, "user_id", "") or ""
        log.warning(
            "genesis.plaintext.rejected user=%s mode=%s material_present=%s sizes=%s reason=%s",
            uid, mode or "", material_present, sizes, str(reason)[:120],
        )
        try:
            import debug_trace
            debug_trace.trace_event(
                store, subsystem="genesis", type="genesis.plaintext.rejected",
                actor="backend", status="failed",
                summary="plaintext import rejected (400)",
                explain=(
                    "上传了素材但被判为空（可能是记忆归档用了非白名单正文键 / id 误杀）"
                    if material_present else "无可用素材"
                ),
                detail={"mode": mode or "", "material_present": material_present,
                        "sizes": sizes, "reason": str(reason)[:160]},
            )
        except Exception:
            pass
    except Exception:
        pass


def _log_resident_sealed_rejected(store, *, mode: str, reason: str, env, **facts) -> None:
    """Always-on breadcrumb for a rejected resident (sealed) import — the sealed-lane
    sibling of ``_log_plaintext_import_rejected``. These rejections return BEFORE a job
    row is created, so (like the cloud upload 400) they otherwise leave no trace: no job,
    no client-visible slug beyond a generic copy. The material is ciphertext, so we log
    only structural facts (envelope present, body_ct byte length, missing fields,
    visibility) — never content. Grep server logs for ``genesis.sealed.rejected``.
    Best-effort; never breaks the request path."""
    try:
        env_present = isinstance(env, dict)
        detail = {"mode": mode or "", "envelope_present": env_present, **facts}
        if env_present:
            try:
                detail["body_ct_bytes"] = len(
                    base64.b64decode(str(env.get("body_ct") or ""), validate=True))
            except Exception:
                detail["body_ct_bytes"] = -1  # unparseable base64
            detail["visibility"] = str(env.get("visibility") or "")
        uid = getattr(store, "user_id", "") or ""
        log.warning(
            "genesis.sealed.rejected user=%s mode=%s reason=%s detail=%s",
            uid, mode or "", str(reason)[:120], detail,
        )
        try:
            import debug_trace
            debug_trace.trace_event(
                store, subsystem="genesis", type="genesis.sealed.rejected",
                actor="backend", status="failed",
                summary="sealed import rejected", detail={"reason": str(reason)[:160], **detail})
        except Exception:
            pass
    except Exception:
        pass


def _bad(error: str, status: int = 400, **extra) -> tuple[dict, int]:
    return {"error": error, **extra}, status


def _bad_from_value_error(e: ValueError, status: int = 400) -> tuple[dict, int]:
    error = str(getattr(e, "error", "") or "")
    if error == "material_empty":
        detail = str(getattr(e, "detail", "") or str(e))
        return _bad("material_empty", status, detail=detail)
    return _bad(str(e), status)


def _is_sealed_body(payload: dict) -> bool:
    """A self-hosted upload is a client-sealed envelope, tagged ``format: sealed_v1``
    (NOT the legacy plaintext body). This tag is the ROUTING signal: sealed → resident
    lane (the user's own local agent distills), plaintext → server-side worker. The two
    lanes coexist on one backend; no global mode switch, and the body type makes it
    impossible to feed ciphertext to the worker as plaintext (or vice versa)."""
    return isinstance(payload, dict) and str(payload.get("format") or "").strip().lower() == "sealed_v1"


def resident_distill_max_bytes() -> int:
    """Max sealed-material size (bytes) accepted in resident mode. Distill cost is NOT the
    reason for the cap — the consumer chunks the decrypted document (``_window_document``)
    exactly like the cloud worker chunks server-side, so it processes any size on the user's
    own machine. The cap guards the ONE-SHOT sealed envelope: the whole document is a single
    AEAD ciphertext that must fit through one HTTP POST + one enclave ``/v1/envelope/decrypt``
    + one DB blob, and it can't be split without breaking the AAD (unlike the cloud plaintext
    path, which downsamples/chunks server-side). Configurable via
    FEEDLING_RESIDENT_DISTILL_MAX_BYTES; default 8 MiB — well above any real chat-log /
    memory-archive / persona, well under the single-POST + single-decrypt ceiling.
    Measured on the ciphertext the server actually stores (server-verifiable, un-fakeable)."""
    try:
        v = int(os.environ.get("FEEDLING_RESIDENT_DISTILL_MAX_BYTES", "") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else 8 * 1024 * 1024


def _resident_sealed_import(store, payload: dict) -> tuple[dict, int]:
    """Resident-mode ingest: the material is a client-sealed envelope (the server never
    sees plaintext). Store the ciphertext + create an ``awaiting_resident`` job for the
    resident consumer to claim, decrypt (via the enclave), and distill locally.

    The app-facing job status is ``processing`` (the ``awaiting_resident``/claim detail
    stays internal). Idempotent: the same material re-uploaded maps to the same job_id.

    ``source_kind`` discriminator: ``mode == "update_identity"`` on this sealed lane
    ALWAYS maps to ``source_kind = "resident_redistill"`` (I1b fix — forced server-side
    from the already-validated ``mode``, not from the client-supplied ``job_kind``
    field, which is no longer trusted as the sole gate: every real caller of sealed
    ``mode="update_identity"`` today IS the terminal ``identity-redistill`` lane,
    io_cli → consumer IPC, T11), which is DB-level exclusive per user
    (0023_redistill_job_exclusivity.py): a second concurrent redistill job for the same
    user 409s instead of silently racing the first. Onboarding's plain ``add_memory`` /
    other modes are untouched and keep today's unlimited-concurrency behavior — the
    exclusivity index only watches ``source_kind = 'resident_redistill'``.

    V2 NOTE (2026-07-27 pre-merge): when ``test`` merges into ``pre``, the exclusivity
    migration (0023_redistill_job_exclusivity.py) needs a merge revision against pre's
    alembic head (0052+ at last check), and the TEE mirror schema sync should be
    re-evaluated then too — see that migration's docstring.

    NOTE: the sealed-envelope field names + AAD binding below are the iOS<->backend crypto
    contract (P5) and MUST be reconciled with the client sealer + verified on a real enclave
    e2e (red line) before merge — the DB/size/job logic here is what's unit-verified.
    """
    env = payload.get("envelope")
    mode_hint = str(payload.get("mode") or "").strip().lower()
    if not isinstance(env, dict):
        _log_resident_sealed_rejected(store, mode=mode_hint, reason="sealed_envelope_incomplete", env=env)
        return _bad("sealed_envelope_incomplete", 400)
    # Reuse the proven v1 content-envelope wire shape (the SAME one memory.add / identity /
    # the genesis chunk path already use, so the enclave decrypts it unchanged): body_ct +
    # the key/metadata fields (nonce / K_user / K_enclave / owner_user_id / visibility / id).
    required = ["body_ct", "nonce", "K_user", "owner_user_id", "visibility"]
    missing = [k for k in required if not env.get(k)]
    if str(env.get("visibility") or "") == "shared" and not env.get("K_enclave"):
        missing.append("K_enclave")
    if missing:
        _log_resident_sealed_rejected(
            store, mode=mode_hint, reason="sealed_envelope_incomplete", env=env, missing=missing)
        return _bad("sealed_envelope_incomplete", 400, missing=missing)
    if str(env.get("owner_user_id") or "") != store.user_id:
        # defense in depth (like identity.init / memory.add) — reject a mismatched owner.
        _log_resident_sealed_rejected(store, mode=mode_hint, reason="envelope_owner_mismatch", env=env)
        return _bad("envelope_owner_mismatch", 403)
    try:
        encrypted_body = base64.b64decode(str(env.get("body_ct") or ""), validate=True)
    except Exception:
        _log_resident_sealed_rejected(store, mode=mode_hint, reason="body_ct_invalid", env=env)
        return _bad("body_ct_invalid", 400)
    max_bytes = resident_distill_max_bytes()
    if len(encrypted_body) > max_bytes:
        _log_resident_sealed_rejected(
            store, mode=mode_hint, reason="material_too_large", env=env,
            got_bytes=len(encrypted_body), max_bytes=max_bytes)
        return _bad("material_too_large", 413, max_bytes=max_bytes, got_bytes=len(encrypted_body))

    client_job_id = history_import._history_import_client_job_id(payload)
    job_id = "genesis_" + hashlib.sha256(
        f"{store.user_id}:{client_job_id}:{env.get('id') or ''}".encode("utf-8")
    ).hexdigest()[:16]
    # aad carries everything except the ciphertext, so /pending can rebuild the full envelope.
    aad = {k: v for k, v in env.items() if k != "body_ct"}
    ciphertext_sha256 = hashlib.sha256(encrypted_body).hexdigest()

    # material_kind lets the resident consumer pick the extraction口径 deterministically from
    # the app entry (long-term-memory archive → keep_all, chat log → selective) — the sealed
    # blob has no source_family the way the cloud plaintext path does.
    material_kind = str(payload.get("material_kind") or "").strip().lower()
    # base_identity_replaced_at snapshots the P5 concurrency baseline (Task 3's outer
    # ``replaced_at`` field, stamped only by full identity init/replace) AT JOB-CREATION
    # TIME, so a later conflict check (Task 5) compares against the identity that existed
    # when this job was queued — not whatever identity happens to exist when the resident
    # consumer eventually claims it. No identity on file (or a legacy card missing the
    # field) → "" (back-compat: "" means "no baseline, skip the check").
    current_identity = identity_service._load_identity(store)
    base_identity_replaced_at = str((current_identity or {}).get("replaced_at") or "")
    job_kind_hint = str(payload.get("job_kind") or "").strip().lower()
    # I1b: source_kind used to be job_kind_hint-or-mode_hint, i.e. the DB-level
    # redistill exclusivity (0023) depended on the client remembering to also
    # send job_kind="resident_redistill" alongside mode="update_identity" — an
    # omitted (or simply wrong) job_kind silently fell back to mode_hint
    # ("update_identity") and dodged the partial-unique index entirely, even
    # though every real caller of mode="update_identity" on this sealed lane
    # IS the identity-redistill lane (T11's IPC relay is the only producer;
    # the resident consumer's own dispatch already keys off mode=="update_identity"
    # to run _resident_distill_identity — see _handle_redistill_ipc). So derive
    # source_kind from the mode the server itself just validated, not from the
    # separate, independently-omittable job_kind field: mode=="update_identity"
    # on the sealed path ALWAYS means resident_redistill, full stop — job_kind
    # can no longer opt a request out of exclusivity. Every other mode
    # (add_memory / onboarding / …) is untouched and keeps unlimited
    # concurrency exactly as before.
    if mode_hint == "update_identity":
        source_kind = "resident_redistill"
    else:
        source_kind = job_kind_hint or mode_hint or "resident"
    try:
        created = db.genesis_create_job(store.user_id, {
            "job_id": job_id,
            "status": "awaiting_resident",
            "source_kind": source_kind,
            "total_chunks": 1,
            "total_bytes": len(encrypted_body),
            "privacy_mode": "resident_sealed",
            "metadata": {"mode": mode_hint, "material_kind": material_kind,
                         "client_job_id": client_job_id, "ingest": "resident_sealed",
                         "base_identity_replaced_at": base_identity_replaced_at},
        })
    except db.GenesisRedistillJobActive as e:
        # DB-level exclusivity (0023_redistill_job_exclusivity.py): this user already has
        # an active resident_redistill job under a DIFFERENT job_id — surface it so the
        # caller (io_cli / consumer, T11) can point the user at the job already running
        # instead of silently racing a second distill against the same identity card.
        _log_resident_sealed_rejected(
            store, mode=mode_hint, reason="redistill_job_active", env=env,
            active_job_id=e.active_job_id)
        return _bad("redistill_job_active", 409, active_job_id=e.active_job_id)
    # I5: job-insert (above) and chunk-insert (below) are two SEPARATE transactions.
    # `created is None` on ON CONFLICT DO NOTHING covers two different situations that
    # used to be treated as the same thing:
    #   (a) steady-state idempotent re-upload — the job AND its chunk 0 are already
    #       fully stored from a prior successful call.
    #   (b) a crash landed AFTER the job insert committed but BEFORE the chunk insert
    #       below ran — the job row exists but chunk 0 was never written. Skipping the
    #       chunk write here (the old behavior) left an unrepairable "awaiting_resident"
    #       job with zero chunks: the resident consumer's claim path treats a chunkless
    #       job as malformed and just waits for the reaper to fail it — a retry with the
    #       SAME request_id could never self-heal it.
    # Distinguish them by re-checking chunk 0's presence (not just the job row) before
    # deciding to skip the write, so (b) gets backfilled. Only skip when the job has
    # already moved past the point chunks are meaningful (done/failed — e.g. the
    # resident consumer already distilled it and cleaned up the ciphertext via
    # genesis_delete_chunks) so a stray retry can't resurrect ciphertext for a job
    # that's already finished and had its material purged.
    write_chunk = created is not None
    if created is None:
        existing_job = db.genesis_get_job(store.user_id, job_id)
        existing_status = str((existing_job or {}).get("status") or "")
        if existing_status not in ("done", "failed"):
            existing_chunks = db.genesis_missing_chunk_seqs(store.user_id, job_id, total_chunks=1)
            write_chunk = bool(existing_chunks)  # seq 0 missing -> [0], present -> []
    if write_chunk:
        # genesis_put_chunk is itself safe to call on a chunk that already exists with
        # the SAME ciphertext (verifies the hash and no-ops); it only raises on a
        # genuine mismatch (different ciphertext already stored under this job_id/seq),
        # which is a real anomaly worth surfacing rather than papering over.
        try:
            db.genesis_put_chunk(
                store.user_id, job_id,
                seq=0, byte_start=0, byte_end=len(encrypted_body),
                ciphertext_sha256=ciphertext_sha256,
                content_sha256="",
                aad=aad, encrypted_body=encrypted_body,
            )
        except ValueError as e:
            if str(e) == "chunk_hash_conflict":
                _log_resident_sealed_rejected(
                    store, mode=mode_hint, reason="chunk_hash_conflict", env=env)
                return _bad("chunk_hash_conflict", 409)
            raise
    return {"job": {"job_id": job_id, "status": "processing"}}, 200


def resident_pending(store, *, consumer_id: str) -> tuple[dict, int]:
    """Resident consumer polls for its user's sealed distill jobs. Atomically claims this
    user's ``awaiting_resident`` jobs and returns them WITH the sealed material (ciphertext
    + aad) for the consumer to decrypt via the enclave and distill locally. Per-user: uses
    the same credential the consumer already uses for chat poll — never another user's jobs."""
    cid = str(consumer_id or "").strip()
    if not cid:
        return _bad("consumer_id_required", 400)
    claimed = db.genesis_claim_resident_jobs(store.user_id, consumer_id=cid, limit=4)
    jobs: list[dict] = []
    for job in claimed:
        chunks = db.genesis_list_chunks(store.user_id, job["job_id"])
        sealed = None
        if chunks:
            c = chunks[0]
            body = c.get("encrypted_body") or b""
            # Rebuild the full v1 envelope (aad holds all fields except body_ct) so the
            # consumer can POST {"envelope": ...} straight to the enclave /v1/envelope/decrypt.
            env = dict(c.get("aad") or {})
            env["body_ct"] = base64.b64encode(body).decode("ascii")
            sealed = {"envelope": env}
        meta = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        jobs.append({
            "job_id": job["job_id"],
            "mode": (meta.get("mode") or "") or job.get("source_kind") or "",
            "material_kind": str(meta.get("material_kind") or ""),
            # "" for jobs created before this field existed (no metadata key) — the
            # consumer (Task 5) must treat "" as "no baseline, skip the conflict check".
            "base_identity_replaced_at": str(meta.get("base_identity_replaced_at") or ""),
            "sealed": sealed,
        })
    return {"jobs": jobs}, 200


def resident_complete(store, job_id: str, payload: dict) -> tuple[dict, int]:
    """Consumer reports a resident distill job finished (agent distilled + wrote memory /
    identity locally). Marks the job done + **deletes the stored sealed material** (ephemeral —
    consumed). ``memory_action_count`` / ``identity_status`` are informational for the app poll."""
    if not isinstance(payload, dict):
        return _bad("json_object_required", 400)
    job = db.genesis_get_job(store.user_id, job_id)
    if not job:
        return _bad("job_not_found", 404)
    mac = int(payload.get("memory_action_count") or 0)
    db.genesis_complete_job(
        store.user_id, job_id,
        output={"stage": "resident_distill_done"},
        memory_action_count=mac,
        identity_status=str(payload.get("identity_status") or ""),
        persona_ref="", persona_sha256="",
    )
    db.genesis_delete_chunks(store.user_id, job_id)
    # 兑现 spec 无条件规则「任一 job done → resolve」：resident 蒸馏完成也清该用户
    # 的历史 genesis 失败通知（本函数不 emit partial，无自清风险）。
    notices.resolve(store, "genesis:")
    return {"job": {"job_id": job_id, "status": "done", "memory_action_count": mac}}, 200


def resident_heartbeat(store, job_id: str, *, consumer_id: str) -> tuple[dict, int]:
    """Consumer renews the lease on a job it's actively distilling. Owner-only (must be the
    consumer that claimed it, still processing) — keeps the stale reaper from re-queueing it."""
    ok = db.genesis_resident_heartbeat(store.user_id, job_id, consumer_id=str(consumer_id or "").strip())
    if not ok:
        return _bad("heartbeat_rejected", 409)  # not the owner, or job no longer processing
    return {"ok": True, "job_id": job_id}, 200


def _valid_job_id(job_id: str) -> bool:
    return bool(_JOB_ID_RE.match(str(job_id or "")))


def _job_response(job: dict | None, *, extra: dict | None = None) -> dict:
    job = job or {}
    # Report the client-facing stage name (v2-internal -> legacy phase the old iOS maps),
    # so shipped apps show correct copy without an update. Stored stage is unchanged.
    out = job.get("output")
    if isinstance(out, dict) and out.get("stage"):
        public_output = {**out, "stage": service.public_stage(out["stage"])}
        if isinstance(out.get("materials"), list):
            public_output["materials"] = service.public_materials_for_job(job)
        job = {**job, "output": public_output}
    body = {
        "job": job,
        "privacy_mode": service.PRIVACY_MODE,
        "privacy_copy": service.PRIVACY_COPY,
    }
    if extra:
        body.update(extra)
    return body


def _json_chunk_payload(payload: dict) -> tuple[bytes, dict[str, Any]]:
    envelope = payload.get("envelope") if isinstance(payload.get("envelope"), dict) else {}
    envelope_meta = payload.get("envelope_meta") if isinstance(payload.get("envelope_meta"), dict) else envelope
    body_ct = str(payload.get("ciphertext_b64") or envelope.get("body_ct") or "")
    raw = service.b64decode_required(body_ct)
    payload = {**payload, "envelope_meta": envelope_meta}
    return raw, payload


def _binary_chunk_payload(raw: bytes, headers, query) -> tuple[bytes, dict[str, Any]]:
    """Binary chunk body + metadata, sourced from headers (falling back to query).

    ``headers`` must be a case-insensitive mapping (Flask ``request.headers`` /
    Starlette ``request.headers``); ``query`` is Flask ``request.args`` /
    Starlette ``request.query_params``. Mirrors the old Flask
    ``header.get(...) or args.get(...) or ""`` precedence exactly."""
    raw = raw or b""

    def _hq(header_name: str, query_name: str):
        return headers.get(header_name) or query.get(query_name)

    envelope_meta_raw = _hq("X-Envelope-Meta", "envelope_meta") or ""
    envelope_meta: dict = {}
    if envelope_meta_raw:
        try:
            envelope_meta = json.loads(envelope_meta_raw)
        except Exception as e:  # noqa: BLE001
            raise ValueError("invalid_envelope_meta_json") from e
        if not isinstance(envelope_meta, dict):
            raise ValueError("invalid_envelope_meta_json")
    meta = {
        "byte_start": _hq("X-Byte-Start", "byte_start"),
        "byte_end": _hq("X-Byte-End", "byte_end"),
        "content_sha256": _hq("X-Content-SHA256", "content_sha256"),
        "ciphertext_sha256": _hq("X-Ciphertext-SHA256", "ciphertext_sha256"),
        "envelope_meta": envelope_meta,
    }
    return raw, meta


# --------------------------------------------------------------------------- #
# per-route neutral operations (return (body, status))
# --------------------------------------------------------------------------- #

def list_imports(store, *, limit_raw) -> tuple[dict, int]:
    try:
        limit = int(limit_raw if limit_raw is not None else 20)
    except Exception:
        limit = 20
    return {
        "jobs": db.genesis_list_jobs(store.user_id, limit=limit),
        "state": db.get_blob(store.user_id, service.GENESIS_STATE_BLOB),
    }, 200


def create_import(store, payload: dict) -> tuple[dict, int]:
    try:
        job, status = service.create_import_job(store, payload)
    except ValueError as e:
        return _bad(str(e), 400)
    return _job_response(job, extra={"status": "created" if status == 201 else "exists"}), status


def get_import_status(store, job_id: str, *, include_missing_raw) -> tuple[dict, int]:
    if not _valid_job_id(job_id):
        return _bad("invalid_job_id", 400)
    job = db.genesis_get_job(store.user_id, job_id)
    if not job:
        return _bad("genesis_job_not_found", 404)
    # The app should see a continuous processing->done arc; hide the internal
    # `awaiting_resident` claim status that sits between upload and the resident claim.
    if str(job.get("status") or "") == "awaiting_resident":
        job = {**job, "status": "processing"}
    include_missing = str(include_missing_raw or "").lower() in {"1", "true", "yes"}
    extra: dict[str, Any] = {
        "state": db.get_blob(store.user_id, service.GENESIS_STATE_BLOB),
        "persona": db.get_blob(store.user_id, service.GENESIS_PERSONA_BLOB),
    }
    if include_missing:
        extra["missing_chunks"] = db.genesis_missing_chunk_seqs(
            store.user_id,
            job_id,
            int(job.get("total_chunks") or 0),
        )
    output = job.get("output") if isinstance(job.get("output"), dict) else {}
    status_projection: dict[str, Any] = {
        "job_id": str(job.get("job_id") or job_id),
        "status": str(job.get("status") or ""),
        "identity_ready": bool(output.get("identity_ready")),
        "materials": service.public_materials_for_job(job),
        "error_class": None,
        "friendly_copy": "",
    }
    if str(job.get("status") or "") == service.FAILED_JOB_STATUS:
        error_class = service.classify_genesis_error(str(job.get("error") or ""))
        status_projection["error_class"] = error_class
        status_projection["friendly_copy"] = service.genesis_failure_required_text(
            str(job.get("error") or ""))
    extra.update(status_projection)
    return _job_response(job, extra=extra), 200


def put_chunk(
    store,
    job_id: str,
    seq: int,
    *,
    is_json: bool,
    json_body: dict | None,
    raw_body: bytes,
    headers,
    query,
) -> tuple[dict, int]:
    if not _valid_job_id(job_id):
        return _bad("invalid_job_id", 400)
    try:
        if is_json:
            raw, meta = _json_chunk_payload(json_body or {})
        else:
            raw, meta = _binary_chunk_payload(raw_body, headers, query)
        byte_start = int(meta.get("byte_start") or 0)
        byte_end = int(meta.get("byte_end") or 0)
        expected_hash = str(meta.get("ciphertext_sha256") or "").strip().lower()
        if expected_hash and expected_hash != hashlib.sha256(raw).hexdigest():
            return _bad("ciphertext_sha256_mismatch", 400)
        aad = meta.get("aad") if isinstance(meta.get("aad"), dict) else {}
        chunk = service.put_chunk(
            store,
            job_id,
            seq=seq,
            encrypted_body=raw,
            byte_start=byte_start,
            byte_end=byte_end,
            content_sha256=str(meta.get("content_sha256") or ""),
            expected_ciphertext_sha256=expected_hash,
            aad=aad,
            envelope_meta=meta.get("envelope_meta") if isinstance(meta.get("envelope_meta"), dict) else None,
        )
    except LookupError as e:
        return _bad(str(e), 404)
    except ValueError as e:
        return _bad(str(e), 409 if str(e) == "chunk_hash_conflict" else 400)
    return {"status": "uploaded", "chunk": chunk}, 200


def finalize(store, job_id: str, payload: dict, *, api_key: str | None) -> tuple[dict, int]:
    if not _valid_job_id(job_id):
        return _bad("invalid_job_id", 400)
    try:
        job, missing = service.finalize_upload(store, job_id)
    except LookupError as e:
        return _bad(str(e), 404)
    if missing:
        return _job_response(job, extra={
            "status": "missing_chunks",
            "missing_chunks": missing[:200],
            "missing_count": len(missing),
        }), 409

    reducer_output = payload.get("reducer_output")
    if isinstance(reducer_output, dict):
        try:
            applied = service.apply_reducer_output(store, api_key, job_id, reducer_output)
            job = db.genesis_get_job(store.user_id, job_id) or job
            return _job_response(job, extra={"status": "done", "applied": applied}), 200
        except ValueError as e:
            return _bad(str(e), 400)
        except Exception as e:  # noqa: BLE001
            # T16: exc=e lets classify_genesis_error use e.status_code if this
            # ever wraps a ProviderError; string match is the fallback either way.
            failed = service.mark_failed(
                store, job_id, f"apply_outputs_failed:{type(e).__name__}:{str(e)[:180]}", exc=e,
            )
            return _job_response(failed or job, extra={"status": "failed", "error": str(e)[:240]}), 500

    return _job_response(job, extra={"status": "uploaded"}), 202


def apply_outputs(
    store, job_id: str, payload: dict, *, api_key: str | None, runtime_token: str
) -> tuple[dict, int]:
    if not _valid_job_id(job_id):
        return _bad("invalid_job_id", 400)
    reducer_output = payload.get("reducer_output") if isinstance(payload.get("reducer_output"), dict) else payload
    if not isinstance(reducer_output, dict):
        return _bad("reducer_output_required", 400)
    try:
        applied = service.apply_reducer_output(
            store,
            api_key,
            job_id,
            reducer_output,
            runtime_token=runtime_token,
        )
    except LookupError as e:
        return _bad(str(e), 404)
    except ValueError as e:
        return _bad(str(e), 400)
    except Exception as e:  # noqa: BLE001
        import debug_trace
        debug_trace.trace_event(
            store, subsystem="genesis", type="genesis.outputs.applied", actor="backend",
            job_id=job_id, status="failed", summary="apply failed",
            detail={"reason": f"{type(e).__name__}:{str(e)[:80]}"})
        failed = service.mark_failed(
            store, job_id, f"apply_outputs_failed:{type(e).__name__}:{str(e)[:180]}", exc=e,
        )
        return _job_response(failed, extra={"status": "failed", "error": str(e)[:240]}), 500
    job = db.genesis_get_job(store.user_id, job_id)
    import debug_trace
    _a = applied if isinstance(applied, dict) else {}
    debug_trace.trace_event(
        store, subsystem="genesis", type="genesis.outputs.applied", actor="backend",
        job_id=job_id, summary="genesis outputs applied",
        detail={
            "source_kind": str((job or {}).get("source_kind") or ""),
            "memory_action_count": _a.get("memory_action_count"),
            "identity_status": str(_a.get("identity_status") or ""),
            "persona_ref": str(_a.get("persona_ref") or ""),
        },
    )
    return _job_response(job, extra={"status": "done", "applied": applied}), 200


def persona_backfill(store, *, api_key: str | None, runtime_token: str) -> tuple[dict, int]:
    from identity import actions as identity_actions
    from genesis import persona_backfill as persona_backfill_mod
    identity_plain, err = identity_actions._identity_plain_for_action(
        store, api_key, runtime_token=runtime_token)
    if identity_plain is None:
        return _bad(err or "identity_unavailable", 409)
    try:
        job = persona_backfill_mod.run_persona_backfill(store, identity_plain)
    except Exception as e:  # noqa: BLE001
        return _bad(f"persona_backfill_failed:{type(e).__name__}:{str(e)[:160]}", 500)
    if job is None:
        return {"status": "no_signal"}, 200  # nothing to backfill; Dream grows it
    return {
        "status": "enqueued",
        "job_id": job.get("job_id"),
        "job_status": job.get("status"),
    }, 202


def plaintext_import(
    store,
    payload: dict,
    *,
    api_key: str | None,
    prepare,
    find_reusable,
    plaintext_mode,
    job_metadata,
    start_job,
    distill_model: str = "",
) -> tuple[dict, int]:
    """Enqueue (or reuse) a plaintext genesis distill job.

    ``prepare`` / ``find_reusable`` / ``plaintext_mode`` / ``job_metadata`` /
    ``start_job`` are the routes-resident helpers (see module docstring); they are
    injected so the enqueue path (``start_job`` spawns the background distill
    thread) stays the SINGLE mechanism both frameworks drive, and the many tests
    that patch ``routes._start_plaintext_genesis_job`` keep working."""
    if not isinstance(payload, dict):
        return _bad("json_object_required", 400)
    try:
        normalized_distill_model = plaintext_helpers._distill_model_override(distill_model)
    except ValueError as e:
        return _bad(str(e), 400)

    # Route by body type, not a global switch. A SEALED body (self-hosted app encrypted it
    # client-side so the server never sees plaintext) → resident lane, where the user's own
    # local agent claims + distills. A PLAINTEXT body (cloud app) → the server-side worker
    # below. Both lanes coexist on the same backend, so cloud and self-hosted users each get
    # the right path with no per-deployment configuration.
    if _is_sealed_body(payload):
        return _resident_sealed_import(store, payload)

    input_hash = history_import._history_import_payload_hash(payload)
    client_job_id = history_import._history_import_client_job_id(payload)
    mode = plaintext_mode(payload, client_job_id=client_job_id)
    existing = find_reusable(
        store,
        client_job_id=client_job_id,
        input_hash=input_hash,
        mode=mode,
    )
    if existing and str(existing.get("status") or "") == service.DONE_JOB_STATUS:
        return _job_response(existing, extra={"status": "done"}), 200

    # Fast-path the stable 409 response. The partial unique index remains the
    # authoritative cross-worker race guard between this read and insertion.
    try:
        active_jobs = db.genesis_list_jobs(store.user_id, limit=100)
    except Exception:
        active_jobs = []
    active_plaintext = next((
        job for job in active_jobs
        if str((job or {}).get("status") or "") == "processing"
        and isinstance((job or {}).get("metadata"), dict)
        and str(job["metadata"].get("ingest") or "") == "plaintext"
    ), None)
    if active_plaintext:
        return _bad(
            "import_job_active",
            409,
            active_job_id=str(active_plaintext.get("job_id") or ""),
        )

    try:
        prepared = prepare(payload)
    except ValueError as e:
        _log_plaintext_import_rejected(store, mode=mode, reason=str(e), payload=payload)
        return _bad_from_value_error(e, 400)
    queued_output = {
        "stage": "plaintext_queued",
        "materials": plaintext_helpers._queued_plaintext_materials(prepared["source_groups"]),
        "identity_ready": False,
    }

    if existing:
        existing_metadata = (
            existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
        )
        if normalized_distill_model or existing_metadata.get("distill_model"):
            existing = db.genesis_patch_job_metadata(
                store.user_id,
                str(existing.get("job_id") or ""),
                {"distill_model": normalized_distill_model or None},
            ) or existing
        try:
            existing = db.genesis_set_job_status(
                store.user_id,
                str(existing.get("job_id") or ""),
                status="processing",
                output=queued_output,
            ) or existing
        except db.GenesisPlaintextJobActive as e:
            return _bad("import_job_active", 409, active_job_id=e.active_job_id)
        service.write_genesis_state(store, existing, status="processing")
        start_job(
            store,
            api_key,
            existing,
            mode=mode,
            chunk_texts=prepared["chunk_texts"],
            source_kind=prepared["source_kind"],
            source_groups=prepared["source_groups"],
            relationship_anchor=prepared["relationship_anchor"],
            analysis_messages=prepared["analysis_messages"],
        )
        return _job_response(existing, extra={"status": "processing"}), 202

    metadata = job_metadata(
        payload,
        prepared,
        client_job_id=client_job_id,
        input_hash=input_hash,
        mode=mode,
    )
    if normalized_distill_model:
        metadata["distill_model"] = normalized_distill_model
    total_bytes = sum(len(text.encode("utf-8")) for text in prepared["chunk_texts"])
    try:
        job, _status = service.create_import_job(store, {
            "source_kind": prepared["source_kind"],
            "file_manifest_hash": input_hash,
            "total_chunks": len(prepared["chunk_texts"]),
            "total_bytes": total_bytes,
            "metadata": metadata,
        }, initial_status="processing")
    except db.GenesisPlaintextJobActive as e:
        return _bad("import_job_active", 409, active_job_id=e.active_job_id)
    except ValueError as e:
        _log_plaintext_import_rejected(store, mode=mode, reason=str(e), payload=payload)
        return _bad_from_value_error(e, 400)

    job = db.genesis_set_job_status(
        store.user_id,
        str(job.get("job_id") or ""),
        status="processing",
        output=queued_output,
    ) or job
    service.write_genesis_state(store, job, status="processing")
    start_job(
        store,
        api_key,
        job,
        mode=mode,
        chunk_texts=prepared["chunk_texts"],
        source_kind=prepared["source_kind"],
        source_groups=prepared["source_groups"],
        relationship_anchor=prepared["relationship_anchor"],
        analysis_messages=prepared["analysis_messages"],
    )
    return _job_response(job, extra={"status": "processing"}), 202


def plaintext_estimate(store, payload: dict, *, api_key: str | None) -> tuple[dict, int]:
    if not isinstance(payload, dict):
        return _bad("json_object_required", 400)
    if _is_sealed_body(payload):
        return _bad("sealed_estimate_unsupported", 400)
    try:
        prepared = plaintext_helpers._prepare_plaintext_import(payload)
        materials, total = plaintext_helpers._estimate_plaintext_materials(prepared)
        staged_id = service.create_genesis_staged_payload(
            store, payload, ttl_sec=plaintext_helpers._staged_ttl_sec())
    except ValueError as e:
        return _bad_from_value_error(e, 400)
    except Exception as e:  # noqa: BLE001
        return _bad(f"staged_import_create_failed:{type(e).__name__}", 500)
    try:
        recommended_model = plaintext_helpers._recommended_distill_model(store, api_key)
    except Exception:  # noqa: BLE001
        recommended_model = None
    return {
        "staged_id": staged_id,
        "materials": materials,
        "est_total_tokens": total,
        "recommended_model": recommended_model,
    }, 201


def plaintext_commit(
    store,
    payload: dict,
    *,
    api_key: str | None,
    prepare,
    find_reusable,
    plaintext_mode,
    job_metadata,
    start_job,
) -> tuple[dict, int]:
    if not isinstance(payload, dict):
        return _bad("json_object_required", 400)
    staged_id = str(payload.get("staged_id") or "").strip()
    if not staged_id:
        return _bad("staged_id_required", 400)
    try:
        staged_payload = service.load_genesis_staged_payload(store, api_key, staged_id)
    except LookupError as e:
        return _bad(str(e), 404)
    except TimeoutError as e:
        return _bad(str(e), 410)
    except ValueError as e:
        return _bad(str(e), 409 if str(e) == "staged_import_consumed" else 400)
    except Exception as e:  # noqa: BLE001
        return _bad(f"staged_import_load_failed:{type(e).__name__}", 500)
    body, status = plaintext_import(
        store,
        staged_payload,
        api_key=api_key,
        prepare=prepare,
        find_reusable=find_reusable,
        plaintext_mode=plaintext_mode,
        job_metadata=job_metadata,
        start_job=start_job,
        distill_model=str(payload.get("distill_model") or ""),
    )
    if 200 <= status < 300:
        job = body.get("job") if isinstance(body.get("job"), dict) else {}
        try:
            service.mark_genesis_staged_consumed(
                store, staged_id, str(job.get("job_id") or ""))
        except Exception:
            log.exception("genesis staged tombstone write failed staged_id=%s", staged_id)
    return body, status
