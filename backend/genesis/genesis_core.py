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
import os
import re
from typing import Any

import db
from genesis import service
from hosted import history_import
from identity import service as identity_service
from notices import core as notices

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


def _bad(error: str, status: int = 400, **extra) -> tuple[dict, int]:
    return {"error": error, **extra}, status


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

    NOTE: the sealed-envelope field names + AAD binding below are the iOS<->backend crypto
    contract (P5) and MUST be reconciled with the client sealer + verified on a real enclave
    e2e (red line) before merge — the DB/size/job logic here is what's unit-verified.
    """
    env = payload.get("envelope")
    mode_hint = str(payload.get("mode") or "").strip().lower()
    if not isinstance(env, dict):
        return _bad("sealed_envelope_incomplete", 400)
    # Reuse the proven v1 content-envelope wire shape (the SAME one memory.add / identity /
    # the genesis chunk path already use, so the enclave decrypts it unchanged): body_ct +
    # the key/metadata fields (nonce / K_user / K_enclave / owner_user_id / visibility / id).
    required = ["body_ct", "nonce", "K_user", "owner_user_id", "visibility"]
    missing = [k for k in required if not env.get(k)]
    if str(env.get("visibility") or "") == "shared" and not env.get("K_enclave"):
        missing.append("K_enclave")
    if missing:
        return _bad("sealed_envelope_incomplete", 400, missing=missing)
    if str(env.get("owner_user_id") or "") != store.user_id:
        # defense in depth (like identity.init / memory.add) — reject a mismatched owner.
        return _bad("envelope_owner_mismatch", 403)
    try:
        encrypted_body = base64.b64decode(str(env.get("body_ct") or ""), validate=True)
    except Exception:
        return _bad("body_ct_invalid", 400)
    max_bytes = resident_distill_max_bytes()
    if len(encrypted_body) > max_bytes:
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
    created = db.genesis_create_job(store.user_id, {
        "job_id": job_id,
        "status": "awaiting_resident",
        "source_kind": mode_hint or "resident",
        "total_chunks": 1,
        "total_bytes": len(encrypted_body),
        "privacy_mode": "resident_sealed",
        "metadata": {"mode": mode_hint, "material_kind": material_kind,
                     "client_job_id": client_job_id, "ingest": "resident_sealed",
                     "base_identity_replaced_at": base_identity_replaced_at},
    })
    # created is None on ON CONFLICT DO NOTHING (idempotent re-upload) — chunk already stored.
    if created is not None:
        db.genesis_put_chunk(
            store.user_id, job_id,
            seq=0, byte_start=0, byte_end=len(encrypted_body),
            ciphertext_sha256=ciphertext_sha256,
            content_sha256="",
            aad=aad, encrypted_body=encrypted_body,
        )
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
        job = {**job, "output": {**out, "stage": service.public_stage(out["stage"])}}
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
            failed = service.mark_failed(store, job_id, f"apply_outputs_failed:{type(e).__name__}:{str(e)[:180]}")
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
        failed = service.mark_failed(store, job_id, f"apply_outputs_failed:{type(e).__name__}:{str(e)[:180]}")
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
) -> tuple[dict, int]:
    """Enqueue (or reuse) a plaintext genesis distill job.

    ``prepare`` / ``find_reusable`` / ``plaintext_mode`` / ``job_metadata`` /
    ``start_job`` are the routes-resident helpers (see module docstring); they are
    injected so the enqueue path (``start_job`` spawns the background distill
    thread) stays the SINGLE mechanism both frameworks drive, and the many tests
    that patch ``routes._start_plaintext_genesis_job`` keep working."""
    if not isinstance(payload, dict):
        return _bad("json_object_required", 400)

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

    try:
        prepared = prepare(payload)
    except ValueError as e:
        return _bad(str(e), 400)

    if existing:
        existing = db.genesis_set_job_status(
            store.user_id,
            str(existing.get("job_id") or ""),
            status="processing",
            output={"stage": "plaintext_queued"},
            processed_chunks=0,
        ) or existing
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
    total_bytes = sum(len(text.encode("utf-8")) for text in prepared["chunk_texts"])
    try:
        job, _status = service.create_import_job(store, {
            "source_kind": prepared["source_kind"],
            "file_manifest_hash": input_hash,
            "total_chunks": len(prepared["chunk_texts"]),
            "total_bytes": total_bytes,
            "metadata": metadata,
        })
    except ValueError as e:
        return _bad(str(e), 400)

    job = db.genesis_set_job_status(
        store.user_id,
        str(job.get("job_id") or ""),
        status="processing",
        output={"stage": "plaintext_queued"},
        processed_chunks=0,
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
