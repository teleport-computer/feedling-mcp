"""Cloudflare R2 (S3-compatible) object storage for heavy frame ciphertext.

A v1 frame envelope's ``body_ct`` is the >150KB ChaCha20-Poly1305 screenshot
blob; left inline in ``frame_envelopes.doc`` it bloats the row (TOAST) and the
DB backups. We offload just that blob to R2 and keep the small envelope
metadata + a pointer in Postgres (see db.py frame_* functions).

Crypto note: the server never decrypts. ``body_ct`` is opaque ciphertext bytes
— safe to park in object storage. This is a lowest-layer module with no
business dependencies (peer of db.py); it reads its own ``FEEDLING_R2_*`` env.

When the four env vars are unset, ``enabled()`` returns False and callers keep
the legacy inline-``doc`` behaviour, so local dev / tests work without R2.
"""

from __future__ import annotations

import base64
import logging
import os
import threading

log = logging.getLogger(__name__)

_KEY_PREFIX = "frames"
_client_lock = threading.Lock()
_cached_client = None


def _endpoint() -> str:
    return os.environ.get("FEEDLING_R2_ENDPOINT", "").strip()


def _access_key() -> str:
    return os.environ.get("FEEDLING_R2_ACCESS_KEY", "").strip()


def _secret_key() -> str:
    return os.environ.get("FEEDLING_R2_SECRET_KEY", "").strip()


def _bucket() -> str:
    return os.environ.get("FEEDLING_R2_BUCKET", "").strip()


def enabled() -> bool:
    """True only when all four R2 settings are present."""
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


def delete_user_frames(user_id: str) -> None:
    """Delete every object under ``frames/<user_id>/`` (account reset)."""
    prefix = f"{_KEY_PREFIX}/{user_id}/"
    try:
        client = _client()
        bucket = _bucket()
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
