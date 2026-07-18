"""Voice/persona backfill for pre-genesis host users (cutover gate 4).

Old host users (onboarded before genesis) have an identity record but no
``genesis_persona`` blob, so after the agent_runtime cutover they boot generic and
their route-B persona signals (``tone_style`` / ``custom_persona_prompt`` /
``self_introduction``) become orphaned (route-B retiring, nothing reads them).

Backfill reuses the genesis production line: assemble a persona material from the
identity record, submit it as ONE chunk under source_kind
``companion_persona_backfill`` (routes to the ``ai_persona`` family →
``persona_build`` → ``genesis_persona`` blob). NOT a transcript, NOT memory/capture.

This module holds only the PURE pieces (assembly / hash / idempotency / signal
check) so they unit-test without DB or enclave. Job creation + the enqueue
trigger + supervisor pickup live in their callers (cutover gate 4 B/C).
"""
from __future__ import annotations

import hashlib

# Identity fields that carry persona/voice signal worth backfilling. Order =
# priority: the user-authored directive first, then derived tone, then self-intro.
_SIGNAL_FIELDS = ("custom_persona_prompt", "tone_style", "self_introduction")

_SOURCE_KIND = "companion_persona_backfill"


def _clean(value) -> str:
    return str(value or "").strip()


def assemble_persona_material(identity: dict | None) -> str:
    """Assemble a persona-build material from the identity record (plaintext in,
    plaintext out — the caller decrypts identity and encrypts the resulting chunk).

    Shaped to read like an uploaded AI-persona/system-prompt so persona_build (§7.B)
    treats it as the persona spec: the user-authored ``custom_persona_prompt`` is the
    backbone, ``tone_style`` the voice, ``self_introduction`` supplementary. Empty
    fields are dropped, never invented (grounding).
    """
    if not isinstance(identity, dict):
        return ""
    agent_name = _clean(identity.get("agent_name"))
    custom = _clean(identity.get("custom_persona_prompt"))
    tone = _clean(identity.get("tone_style"))
    intro = _clean(identity.get("self_introduction"))

    lines: list[str] = []
    if agent_name:
        lines.append(f"Name: {agent_name}")
    if custom:
        lines.append("Persona directive (the user's own, highest priority):")
        lines.append(custom)
    if tone:
        lines.append(f"Voice / tone: {tone}")
    if intro:
        lines.append("Self-introduction (in the companion's own words):")
        lines.append(intro)
    return "\n".join(lines).strip()


def material_sha256(material: str) -> str:
    """Stable digest of the assembled material — drives the idempotency key so the
    same identity material is not re-backfilled every supervisor tick."""
    return hashlib.sha256(_clean(material).encode("utf-8")).hexdigest()


# Job statuses that mean a backfill for this material is already in-flight or done
# (idempotency: don't re-enqueue). 'failed' is excluded so a failed run can retry.
_INFLIGHT_OR_DONE = {"created", "uploaded", "processing", "done"}


def run_persona_backfill(store, identity_plain: dict | None) -> dict | None:
    """Gate-4 A-3: create ONE genesis import job that backfills the persona/voice
    blob from the identity record (NOT a transcript). Returns the created/existing
    job, or None when there's no signal to backfill.

    Caller decrypts the identity (auth lives there) and passes the plaintext dict;
    this fn does the genesis orchestration: assemble → idempotency check → encrypt
    the material as ONE shared chunk (enclave-decryptable by the worker) → create
    job + put chunk + finalize. The existing genesis worker then claims the uploaded
    job → ai_persona → persona_build → genesis_persona blob.
    """
    material = assemble_persona_material(identity_plain)
    if not material:
        return None  # no persona signal — leave empty, Dream grows it from real chat

    import base64
    import db
    from core import envelope as core_envelope
    from genesis import service as genesis_service

    mhash = material_sha256(material)

    # Idempotency: a non-failed backfill job for this exact material means it's
    # already in-flight/done — skip (lazy trigger fires every tick). Match on
    # source_kind + material_sha256, both of which PERSIST: material_sha256 survives
    # _safe_job_metadata's ``*_sha256`` rule. (A bespoke ``backfill_key`` would be
    # FILTERED out by that whitelist, so we must NOT rely on it.)
    for job in db.genesis_list_jobs(store.user_id, limit=50):
        if str(job.get("source_kind") or "") != _SOURCE_KIND:
            continue
        meta = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        if meta.get("material_sha256") == mhash and str(job.get("status") or "") in _INFLIGHT_OR_DONE:
            return job

    envelope, err = core_envelope._build_shared_envelope_for_store(store, material.encode("utf-8"))
    if envelope is None:
        raise RuntimeError(err or "persona_backfill_envelope_failed")
    encrypted_body = base64.b64decode(envelope["body_ct"])

    job, _code = genesis_service.create_import_job(store, {
        "source_kind": _SOURCE_KIND,
        "total_chunks": 1,
        "total_bytes": len(encrypted_body),
        "metadata": {"material_sha256": mhash},
    })
    job_id = job["job_id"]
    genesis_service.put_chunk(
        store, job_id,
        seq=0, encrypted_body=encrypted_body,
        byte_start=0, byte_end=len(encrypted_body),
        content_sha256=mhash,
        envelope_meta=envelope,
    )
    genesis_service.finalize_upload(store, job_id)
    return job
