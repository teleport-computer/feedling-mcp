"""frame_envelopes → TEE ``frames`` per-row replication (spec §4 / D4).

Frames are the odd table out: the >150KB screenshot ciphertext must NOT land
inline in a TEE row. Instead each frame's body is re-encrypted at the storage
layer inside the enclave and parked in R2 under the ``frames-tee/`` prefix; the
TEE ``frames`` row keeps only a pointer + checksums (spec §4).

Two RDS source shapes are handled identically:
  - **inline legacy**: ``doc`` = full v1 envelope (incl. ``body_ct``).
  - **R2-backed**: ``doc`` NULL, ``env_meta`` = envelope minus ``body_ct``,
    ``body_key`` = the legacy R2 object key; the ciphertext is fetched back and
    merged before re-encryption.

Plaintext never leaves the enclave and this module never opens an envelope: it
only moves ciphertext (RDS ``body_ct`` / R2 object) into the enclave storage
endpoint and the resulting storage ciphertext into R2, then upserts the pointer.
Decryptability is classified from envelope metadata alone (visibility +
K_enclave), so local_only / no-K_enclave frames become PendingDeviceMigration
(D1) without any enclave or R2 call — and dry_run classifies the same way with
zero side effects.

``replicate`` returns the 9-tuple matching worker's ``frames`` upsert_sql (so
the TEE write + cursor advance stay in worker's single batched transaction), or
None for a dry_run "would copy" (no write).
"""
from __future__ import annotations

import base64
import hashlib

from psycopg.types.json import Jsonb

import object_storage
from tee_replicator.transforms import PendingDeviceMigration

KEY_VERSION = "v1"

# AEAD payload + wrap keys + envelope version/fingerprints: opaque crypto fields
# that must never survive into a TEE ``frames.meta`` (mirrors transforms).
_CRYPTO_FIELDS = {"v", "body_ct", "body_b64", "nonce", "K_user", "K_enclave",
                  "enclave_pk_fpr", "content_pk_fpr", "body_object_format",
                  "body_sha256", "body_size_bytes", "body_key"}
# Candidate top-level mime hints (screen frames carry the real image_mime inside
# the ciphertext, so this is best-effort — body_mime is nullable).
_MIME_FIELDS = ("content_type", "image_mime", "mime")


def _decryptable(meta: dict) -> bool:
    """enclave can open ⟺ not local_only and carries K_enclave (else only the
    device's K_user can). Classified from metadata alone — no body_ct needed."""
    return meta.get("visibility") != "local_only" and bool(meta.get("K_enclave"))


def _meta_from(envelope: dict) -> dict:
    """TEE ``frames.meta``: semantic fields only, all crypto fields dropped."""
    return {k: v for k, v in envelope.items() if k not in _CRYPTO_FIELDS}


def _body_mime(meta: dict) -> str | None:
    for k in _MIME_FIELDS:
        v = meta.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def replicate(user_id: str, frame_id: str, ts: float, row: dict, reencrypt,
              *, dry_run: bool = False):
    """Replicate one frame. ``row`` = ``{"doc", "env_meta", "body_key"}``.

    ``reencrypt(envelope, key_version) -> {body_ct_storage, key_version, sha256,
    size}`` is injected by worker (enclave storage endpoint call, with token
    retry). Raises PendingDeviceMigration for local_only / no-K_enclave frames.
    """
    doc = row.get("doc")
    env_meta = row.get("env_meta")
    body_key = row.get("body_key")
    # Metadata carrier for classification + meta (has visibility/K_enclave in
    # both shapes; env_meta is doc-minus-body_ct).
    meta_src = doc if doc is not None else (env_meta or {})
    if not _decryptable(meta_src):
        raise PendingDeviceMigration(frame_id)
    if dry_run:
        return None  # would_copy — no enclave call, no R2 put

    # Assemble the full envelope (incl. body_ct) to hand the enclave.
    if body_key:
        # strict: a definitive R2 404 (orphaned body_key — the ciphertext is
        # gone server-side, unrecoverable by retrying) is a pending-style skip
        # so it never wedges the cursor; transient R2 failures raise here and
        # keep the freeze-and-retry semantics.
        body_ct = object_storage.get_frame_body_strict(user_id, frame_id)
        if body_ct is None:
            raise PendingDeviceMigration("r2_body_missing_orphan")
        envelope = {**(env_meta or {}), "body_ct": body_ct}
    else:
        envelope = dict(doc or {})

    resp = reencrypt(envelope, KEY_VERSION)
    storage_key = object_storage.put_frame_tee_body(
        user_id, frame_id, resp["body_ct_storage"])

    meta = _meta_from(envelope)
    return (user_id, frame_id, ts, Jsonb(meta), storage_key,
            resp.get("key_version") or KEY_VERSION, _body_mime(meta),
            resp.get("sha256"), resp.get("size"), None)


def replicate_plaintext(
    user_id: str,
    frame_id: str,
    ts: float,
    row: dict,
    decrypt,
    *,
    dry_run: bool = False,
):
    """Produce one inline plaintext frame row for a ``plaintext_all`` target."""
    doc = row.get("doc")
    env_meta = row.get("env_meta")
    body_key = row.get("body_key")
    meta_src = dict(doc if doc is not None else (env_meta or {}))
    body_b64 = None
    if body_key:
        body_b64 = object_storage.get_frame_body_strict(user_id, frame_id)
        if body_b64 is None:
            raise PendingDeviceMigration("r2_body_missing_orphan")
    elif isinstance(doc, dict):
        body_b64 = doc.get("body_b64")

    is_plaintext = bool(body_b64) and (
        "body_b64" in (doc or {})
        or meta_src.get("body_object_format") == "plaintext_v1"
    )
    if not is_plaintext and not _decryptable(meta_src):
        raise PendingDeviceMigration(frame_id)
    if dry_run:
        return None

    if is_plaintext:
        try:
            plaintext = base64.b64decode(str(body_b64), validate=True)
        except Exception as exc:
            raise ValueError("plaintext frame body is not strict base64") from exc
        expected_size = meta_src.get("body_size_bytes")
        expected_sha = meta_src.get("body_sha256")
        if expected_size is not None and int(expected_size) != len(plaintext):
            raise ValueError("plaintext frame size mismatch")
        if expected_sha and hashlib.sha256(plaintext).hexdigest() != expected_sha:
            raise ValueError("plaintext frame digest mismatch")
    else:
        envelope = {**meta_src, "body_ct": body_b64} if body_key else dict(doc or {})
        plaintext = decrypt(envelope)

    meta = _meta_from(meta_src)
    return (
        user_id,
        frame_id,
        ts,
        Jsonb(meta),
        None,
        None,
        _body_mime(meta),
        hashlib.sha256(plaintext).hexdigest(),
        len(plaintext),
        plaintext,
    )
