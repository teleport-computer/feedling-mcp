"""Cloudflare R2 (S3-compatible) object storage for heavy frame ciphertext.

A v1 frame envelope's ``body_ct`` is the >150KB ChaCha20-Poly1305 screenshot
blob; left inline in ``frame_envelopes.doc`` it bloats the row (TOAST) and the
DB backups. We offload just that blob to R2 and keep the small envelope
metadata + a pointer in Postgres (see db.py frame_* functions).

Crypto note: the server never decrypts. ``body_ct`` is opaque ciphertext bytes
— safe to park in object storage. This is a lowest-layer module with no
business dependencies (peer of db.py); it reads its own ``R2_*`` env.

Config (reuses the repo's existing ``R2_*`` credentials; the frame bucket is a
dedicated var so it never collides with the WAL-G backup ``R2_BUCKET``):

  - ``R2_ENDPOINT``           : S3 API endpoint, e.g.
                                ``https://<accountid>.r2.cloudflarestorage.com``
                                (derived from ``R2_ACCOUNT_ID`` if unset).
  - ``R2_ACCESS_KEY_ID``      : R2 S3 access key id.
  - ``R2_SECRET_ACCESS_KEY``  : R2 S3 secret.
  - ``R2_FRAMES_BUCKET``      : the bucket for frame ciphertext (e.g.
                                ``io-image-frames``). The token MUST be
                                scoped to this bucket.
  - ``R2_CONNECT_TIMEOUT_SEC``: bounded TCP/TLS connect timeout (default 3s).
  - ``R2_READ_TIMEOUT_SEC``   : bounded response/read timeout (default 10s).

When credentials/bucket are unset, ``enabled()`` returns False and callers keep
the legacy inline-``doc`` behaviour, so local dev / tests work without R2.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import threading

log = logging.getLogger(__name__)

_KEY_PREFIX = "frames"
# TEE storage-layer re-encrypted bodies live under a distinct prefix in the SAME
# bucket (D4). The ciphertext here is sealed with the enclave's storage key, not
# the E2E content key — it is never the raw ``body_ct`` under _KEY_PREFIX.
_TEE_KEY_PREFIX = "frames-tee"
_client_lock = threading.Lock()
_cached_client = None


def _positive_float_env(name: str, default: float) -> float:
    """Read a strictly-positive timeout without letting bad env wedge startup."""
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else float(default)


def _endpoint() -> str:
    ep = os.environ.get("R2_ENDPOINT", "").strip()
    if ep:
        return ep
    acct = os.environ.get("R2_ACCOUNT_ID", "").strip()
    return f"https://{acct}.r2.cloudflarestorage.com" if acct else ""


def _access_key() -> str:
    return os.environ.get("R2_ACCESS_KEY_ID", "").strip()


def _secret_key() -> str:
    return os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()


def _bucket() -> str:
    return os.environ.get("R2_FRAMES_BUCKET", "").strip()


def enabled() -> bool:
    """True only when R2 credentials + the frames bucket are all present."""
    return bool(_endpoint() and _access_key() and _secret_key() and _bucket())


def _client():
    """Lazily build a process-wide boto3 S3 client pointed at R2."""
    global _cached_client
    with _client_lock:
        if _cached_client is None:
            import boto3
            from botocore.config import Config

            _cached_client = boto3.client(
                "s3",
                endpoint_url=_endpoint(),
                aws_access_key_id=_access_key(),
                aws_secret_access_key=_secret_key(),
                region_name="auto",  # R2 convention
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=_positive_float_env(
                        "R2_CONNECT_TIMEOUT_SEC", 3.0,
                    ),
                    read_timeout=_positive_float_env(
                        "R2_READ_TIMEOUT_SEC", 10.0,
                    ),
                ),
            )
        return _cached_client


def client():
    """Public accessor for the shared boto3 S3 client.

    Lets sibling modules (e.g. ``diagnostics/storage.py``) reuse the same
    connection pool against a *different* bucket without reaching into the
    private ``_client``. The client is not bucket-bound; the caller passes
    ``Bucket=`` per call.
    """
    return _client()


def credentials_present() -> bool:
    """True when the R2 credentials (endpoint + key + secret) are configured,
    independent of any specific bucket. ``enabled()`` additionally requires the
    *frames* bucket; bucket-specific callers should AND this with their own."""
    return bool(_endpoint() and _access_key() and _secret_key())


def frame_key(user_id: str, frame_id: str) -> str:
    return f"{_KEY_PREFIX}/{user_id}/{frame_id}"


def put_frame_body(user_id: str, frame_id: str, body_ct_b64: str) -> str:
    """Upload the decoded ciphertext bytes; return the R2 object key.

    Raises on failure so the caller (db.frame_upsert) can fall back to inline
    ``doc`` storage rather than persist a pointer to a missing object.
    """
    key = frame_key(user_id, frame_id)
    raw = base64.b64decode(body_ct_b64)
    _client().put_object(
        Bucket=_bucket(),
        Key=key,
        Body=raw,
        ContentType="application/octet-stream",
    )
    return key


def frame_tee_key(user_id: str, frame_id: str) -> str:
    return f"{_TEE_KEY_PREFIX}/{user_id}/{frame_id}"


def put_frame_tee_body(user_id: str, frame_id: str, body_ct_b64: str) -> str:
    """Upload the storage-layer ciphertext (D4) under the frames-tee/ prefix;
    return the R2 object key. Deterministic key → overwrite-safe, so a replay
    of the tee_replicator converges without orphaning objects. Raises on
    failure so the replicator freezes the cursor rather than persist a pointer
    to a missing object."""
    key = frame_tee_key(user_id, frame_id)
    raw = base64.b64decode(body_ct_b64)
    _client().put_object(
        Bucket=_bucket(),
        Key=key,
        Body=raw,
        ContentType="application/octet-stream",
    )
    return key


def get_frame_body_strict(user_id: str, frame_id: str) -> str | None:
    """Like ``get_frame_body`` but distinguishes definitive not-found from
    transient failure: returns None ONLY on an *object-level* 404/NoSuchKey
    (the object is truly gone — an orphaned ``body_key``); any other failure
    raises, INCLUDING NoSuchBucket. A misconfigured/unavailable bucket is not
    the same fact as "this one object is missing" — treating NoSuchBucket as
    an orphan would mass-mark every R2-backed frame as pending on a bucket
    misconfig, and fixing the config afterward would not self-heal (the
    cursor already advanced past them). The tee_replicator needs the
    distinction — an orphan is skipped as pending, while a transient/config
    R2 error must freeze the cursor and be retried."""
    key = frame_key(user_id, frame_id)
    try:
        resp = _client().get_object(Bucket=_bucket(), Key=key)
    except Exception as e:  # noqa: BLE001
        if _is_missing_object(e):
            return None
        raise
    raw = resp["Body"].read()
    return base64.b64encode(raw).decode("ascii")


def get_frame_body(user_id: str, frame_id: str) -> str | None:
    """Fetch the object and re-encode to the original base64 ``body_ct``.

    Returns None if the object is missing or the fetch fails (the caller then
    surfaces an absent frame rather than crashing a read path)."""
    key = frame_key(user_id, frame_id)
    try:
        resp = _client().get_object(Bucket=_bucket(), Key=key)
    except Exception as e:  # noqa: BLE001
        if _is_not_found(e):
            return None
        log.error("[r2] get_frame_body(%s) failed: %s", key, e)
        return None
    raw = resp["Body"].read()
    return base64.b64encode(raw).decode("ascii")


def delete_frame_body(user_id: str, frame_id: str) -> None:
    key = frame_key(user_id, frame_id)
    try:
        _client().delete_object(Bucket=_bucket(), Key=key)
    except Exception as e:  # noqa: BLE001
        log.error("[r2] delete_frame_body(%s) failed: %s", key, e)


def delete_frame_tee_body(user_id: str, frame_id: str) -> None:
    """Delete the TEE storage-layer re-encrypted body (``frames-tee/`` prefix,
    D4) for one frame. A single-frame RDS delete/prune must reap this too, else
    the replicator's re-encrypted object is orphaned in R2 (``delete_user_frames``
    already covers the whole-account reset case for both prefixes)."""
    key = frame_tee_key(user_id, frame_id)
    try:
        _client().delete_object(Bucket=_bucket(), Key=key)
    except Exception as e:  # noqa: BLE001
        log.error("[r2] delete_frame_tee_body(%s) failed: %s", key, e)


def delete_user_frames(user_id: str) -> None:
    """Delete every object under ``frames/<user_id>/`` AND the TEE storage-layer
    mirror ``frames-tee/<user_id>/`` (account reset must reap both prefixes)."""
    try:
        client = _client()
        bucket = _bucket()
        for key_prefix in (_KEY_PREFIX, _TEE_KEY_PREFIX):
            prefix = f"{key_prefix}/{user_id}/"
            token = None
            while True:
                kwargs = {"Bucket": bucket, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = client.list_objects_v2(**kwargs)
                objs = [{"Key": c["Key"]} for c in resp.get("Contents", [])]
                if objs:
                    client.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                if not resp.get("IsTruncated"):
                    break
                token = resp.get("NextContinuationToken")
    except Exception as e:  # noqa: BLE001
        log.error("[r2] delete_user_frames(%s) failed: %s", user_id, e)


def _is_not_found(e) -> bool:
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        code = resp.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            return True
    return e.__class__.__name__ in ("NoSuchKey", "404")


# --------------------------------------------------------------------------- #
# Chat message bodies — heavy ciphertext offloaded off the chat_messages row,
# mirroring the frame offload above. Its own bucket so it never collides with
# frames or the WAL-G backup bucket. Target bucket is ``io-user-attachments``
# (reuses the existing R2_* credentials — no new key needed); set
# ``R2_CHAT_FILES_BUCKET=io-user-attachments`` to enable. Until that env is set,
# ``chat_files_enabled()`` is False and db.chat_append/chat_load keep the full
# body inline in Postgres, exactly as before — a no-op until enabled. No default
# is baked in on purpose: enabling is an explicit, one-way switch (once pointer
# rows exist, removing the env makes those bodies unreadable).
#
# Two prefixes, one bucket: images are separated from files so they can carry
# their own lifecycle rule / usage accounting (they dwarf files in both count and
# bytes). The prefix is chosen ONCE, at write time, and the resulting key is
# persisted on the row as ``body_key``. Reads and deletes take that key verbatim
# and never recompute it — so the layout can change again without stranding a
# single historical object.
# --------------------------------------------------------------------------- #

_CHAT_KEY_PREFIX = "chatfiles"
_CHAT_IMAGE_KEY_PREFIX = "chatimages"
_CHAT_KEY_PREFIXES = (_CHAT_KEY_PREFIX, _CHAT_IMAGE_KEY_PREFIX)


def _chat_files_bucket() -> str:
    return os.environ.get("R2_CHAT_FILES_BUCKET", "").strip()


def chat_files_enabled() -> bool:
    """True only when R2 credentials + the chat-files bucket are all present."""
    return bool(_endpoint() and _access_key() and _secret_key() and _chat_files_bucket())


def chat_body_key(
    user_id: str,
    msg_id: str,
    content_type: str = "file",
    *,
    upload_version: str | None = None,
    storage_generation: int | None = None,
) -> str:
    """The R2 key a body of this content_type is written under. Write-side only —
    readers/deleters use the ``body_key`` stored on the row.

    Runtime offloads pass both the lifecycle ``storage_generation`` pinned by
    their initial database write and a fresh ``upload_version``. The generation
    segment is advanced only when account deletion retires all retained chat;
    Clear Chat preserves the generation because it archives the ciphertext. The
    version prevents two in-flight writes for one message overwriting each other
    before PostgreSQL chooses the winner. ``None`` retains the original key shape
    for maintenance/legacy callers. Readers and deleters support every layout
    because they always follow the persisted key.
    """
    prefix = _CHAT_IMAGE_KEY_PREFIX if content_type == "image" else _CHAT_KEY_PREFIX
    if storage_generation is None:
        base = f"{prefix}/{user_id}/{msg_id}"
    else:
        generation = int(storage_generation)
        if generation < 0:
            raise ValueError("storage_generation must be >= 0")
        base = f"{prefix}/{user_id}/g{generation}/{msg_id}"
    return f"{base}/{upload_version}" if upload_version else base


def new_chat_body_upload_version() -> str:
    """Return an unguessable per-attempt suffix for a runtime chat upload."""
    return secrets.token_hex(16)


def put_chat_body(
    user_id: str,
    msg_id: str,
    body_ct_b64: str,
    content_type: str = "file",
    *,
    upload_version: str | None = None,
    storage_generation: int | None = None,
) -> str:
    """Upload the decoded ciphertext bytes; return the R2 object key to persist.

    Raises on failure so the caller (db.chat_append) can fall back to inline
    ``doc`` storage rather than persist a pointer to a missing object.

    Runtime callers pass a value from :func:`new_chat_body_upload_version` to
    allocate a private key for this attempt. The DB then uses compare-and-swap
    promotion, so a losing upload can be deleted without risking the winner.
    """
    key = chat_body_key(
        user_id,
        msg_id,
        content_type,
        upload_version=upload_version,
        storage_generation=storage_generation,
    )
    raw = base64.b64decode(body_ct_b64)
    _client().put_object(
        Bucket=_chat_files_bucket(),
        Key=key,
        Body=raw,
        ContentType="application/octet-stream",
    )
    return key


def chat_key_owned_by(key: str, user_id: str) -> bool:
    """True only when ``key`` is one this user's own rows could have written, i.e.
    it sits under an allowed prefix AND that prefix's owner segment is ``user_id``.

    Taking the key from the row (rather than recomputing it) is what lets an older
    key layout still resolve — but it also means the key is now DATA, and data can
    be wrong: a bad migration, a manual repair, or a polluted row could carry
    another user's key, and a verbatim fetch would then hand this user someone
    else's ciphertext (and a verbatim delete would destroy someone else's object).
    Both stay inside the owner's own prefix. Enforced here, at the only door to the
    bucket, so no caller can forget it."""
    if not key or not user_id:
        return False
    return any(key.startswith(f"{p}/{user_id}/") for p in _CHAT_KEY_PREFIXES)


def chat_body_storage_generation(key: str, user_id: str) -> int | None:
    """Generation encoded in a current-layout key, or ``None`` for legacy keys.

    Parsing is intentionally ownership-aware and conservative. A malformed key
    is never classified as an old generation merely because one segment happens
    to start with ``g``.
    """
    if not chat_key_owned_by(key, user_id):
        return None
    parts = key.split("/")
    if len(parts) < 5 or not parts[2].startswith("g"):
        return None
    raw = parts[2][1:]
    if not raw.isdigit():
        return None
    return int(raw)


def list_user_chat_body_keys(user_id: str) -> list[str]:
    """List every persisted chat-body key under both of this user's prefixes.

    Unlike the account-purge convenience helper below, errors propagate. The DB
    reconciler must retain its durable cleanup intent when an inventory request
    fails instead of mistaking the failure for an empty prefix.
    """
    client = _client()
    bucket = _chat_files_bucket()
    keys: list[str] = []
    for prefix_root in _CHAT_KEY_PREFIXES:
        prefix = f"{prefix_root}/{user_id}/"
        token = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            keys.extend(str(item["Key"]) for item in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
    return keys


def get_chat_body(key: str, user_id: str) -> str | None:
    """Fetch the object at ``key`` (the row's ``body_key``, owned by ``user_id``)
    and re-encode to the original base64 ``body_ct``.

    Returns None if the key isn't this user's, the object is missing, or the fetch
    fails (the caller then surfaces an absent body rather than crashing the read)."""
    if not chat_key_owned_by(key, user_id):
        if key:
            log.error("[r2] get_chat_body refused foreign key %s for user %s", key, user_id)
        return None
    try:
        resp = _client().get_object(Bucket=_chat_files_bucket(), Key=key)
    except Exception as e:  # noqa: BLE001
        if _is_not_found(e):
            return None
        log.error("[r2] get_chat_body(%s) failed: %s", key, e)
        return None
    raw = resp["Body"].read()
    return base64.b64encode(raw).decode("ascii")


def delete_chat_body(key: str, user_id: str) -> bool:
    """Delete an owned body key, returning whether deletion was confirmed.

    S3 DELETE is idempotent, so a successful response also confirms an already
    absent object. Callers with durable cleanup rows use ``False`` to retain and
    retry the intent instead of losing it on a transient R2 error.
    """
    if not chat_key_owned_by(key, user_id):
        if key:
            log.error("[r2] delete_chat_body refused foreign key %s for user %s", key, user_id)
        return False
    try:
        _client().delete_object(Bucket=_chat_files_bucket(), Key=key)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("[r2] delete_chat_body(%s) failed: %s", key, e)
        return False


def delete_user_chat_files(user_id: str) -> None:
    """Delete every chat body this user owns — BOTH prefixes (account reset/clear).
    Missing either one would strand ciphertext for a deleted account."""
    try:
        client = _client()
        bucket = _chat_files_bucket()
        for prefix_root in _CHAT_KEY_PREFIXES:
            prefix = f"{prefix_root}/{user_id}/"
            token = None
            while True:
                kwargs = {"Bucket": bucket, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = client.list_objects_v2(**kwargs)
                objs = [{"Key": c["Key"]} for c in resp.get("Contents", [])]
                if objs:
                    client.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                if not resp.get("IsTruncated"):
                    break
                token = resp.get("NextContinuationToken")
    except Exception as e:  # noqa: BLE001
        log.error("[r2] delete_user_chat_files(%s) failed: %s", user_id, e)


def _is_missing_object(e) -> bool:
    """Object-level "definitely gone" only — NoSuchKey / HTTP 404. Deliberately
    EXCLUDES NoSuchBucket: a missing/misconfigured bucket is a deployment fault,
    not proof any given object was deleted, and must not be treated as an
    orphan (see ``get_frame_body_strict``)."""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        code = resp.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            return True
    return e.__class__.__name__ in ("NoSuchKey", "404")
