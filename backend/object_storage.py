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

When credentials/bucket are unset, ``enabled()`` returns False and callers keep
the legacy inline-``doc`` behaviour, so local dev / tests work without R2.
"""

from __future__ import annotations

import base64
import logging
import os
import threading

log = logging.getLogger(__name__)

_KEY_PREFIX = "frames"
# TEE storage-layer re-encrypted bodies live under a distinct prefix in the SAME
# bucket (D4). The ciphertext here is sealed with the enclave's storage key, not
# the E2E content key — it is never the raw ``body_ct`` under _KEY_PREFIX.
_TEE_KEY_PREFIX = "frames-tee"
_client_lock = threading.Lock()
_cached_client = None


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
# Chat file bodies — heavy content_type="file" ciphertext offloaded off the
# chat_messages row, mirroring the frame offload above. Its own bucket so it
# never collides with frames or the WAL-G backup bucket. Target bucket is
# ``io-user-attachments`` (reuses the existing R2_* credentials — no new key
# needed); set ``R2_CHAT_FILES_BUCKET=io-user-attachments`` to enable. Until that
# env is set, ``chat_files_enabled()`` is False and db.chat_append/chat_load keep
# the full body inline in Postgres, exactly as before — a no-op until enabled.
# No default is baked in on purpose: enabling is an explicit, one-way switch
# (once pointer rows exist, removing the env makes those files unreadable).
# --------------------------------------------------------------------------- #

_CHAT_KEY_PREFIX = "chatfiles"


def _chat_files_bucket() -> str:
    return os.environ.get("R2_CHAT_FILES_BUCKET", "").strip()


def chat_files_enabled() -> bool:
    """True only when R2 credentials + the chat-files bucket are all present."""
    return bool(_endpoint() and _access_key() and _secret_key() and _chat_files_bucket())


def chat_file_key(user_id: str, msg_id: str) -> str:
    return f"{_CHAT_KEY_PREFIX}/{user_id}/{msg_id}"


def put_chat_file_body(user_id: str, msg_id: str, body_ct_b64: str) -> str:
    """Upload the decoded ciphertext bytes; return the R2 object key.

    Raises on failure so the caller (db.chat_append) can fall back to inline
    ``doc`` storage rather than persist a pointer to a missing object."""
    key = chat_file_key(user_id, msg_id)
    raw = base64.b64decode(body_ct_b64)
    _client().put_object(
        Bucket=_chat_files_bucket(),
        Key=key,
        Body=raw,
        ContentType="application/octet-stream",
    )
    return key


def get_chat_file_body(user_id: str, msg_id: str) -> str | None:
    """Fetch the object and re-encode to the original base64 ``body_ct``.

    Returns None if the object is missing or the fetch fails (the caller then
    surfaces an absent body rather than crashing the chat read path)."""
    key = chat_file_key(user_id, msg_id)
    try:
        resp = _client().get_object(Bucket=_chat_files_bucket(), Key=key)
    except Exception as e:  # noqa: BLE001
        if _is_not_found(e):
            return None
        log.error("[r2] get_chat_file_body(%s) failed: %s", key, e)
        return None
    raw = resp["Body"].read()
    return base64.b64encode(raw).decode("ascii")


def delete_chat_file_body(user_id: str, msg_id: str) -> None:
    key = chat_file_key(user_id, msg_id)
    try:
        _client().delete_object(Bucket=_chat_files_bucket(), Key=key)
    except Exception as e:  # noqa: BLE001
        log.error("[r2] delete_chat_file_body(%s) failed: %s", key, e)


def delete_user_chat_files(user_id: str) -> None:
    """Delete every object under ``chatfiles/<user_id>/`` (account reset/clear)."""
    prefix = f"{_CHAT_KEY_PREFIX}/{user_id}/"
    try:
        client = _client()
        bucket = _chat_files_bucket()
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
