"""Unit tests for the R2 object-storage layer (backend/object_storage.py).

Pure unit: no Postgres, no real network. A fake S3 client stands in for boto3
so we exercise the put/get/delete/prefix-delete plumbing and the base64
round-trip that reconstructs ``body_ct`` byte-for-byte.
"""

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import object_storage  # noqa: E402


class _Streaming:
    def __init__(self, data: bytes):
        self._d = data

    def read(self) -> bytes:
        return self._d


class _ClientError(Exception):
    """Mimics botocore.exceptions.ClientError shape (has .response)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3:
    def __init__(self):
        self.store: dict[tuple, bytes] = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.store[(Bucket, Key)] = bytes(Body)
        return {}

    def get_object(self, Bucket, Key, **kw):
        if (Bucket, Key) not in self.store:
            raise _ClientError("NoSuchKey")
        return {"Body": _Streaming(self.store[(Bucket, Key)])}

    def delete_object(self, Bucket, Key, **kw):
        self.store.pop((Bucket, Key), None)
        return {}

    def list_objects_v2(self, Bucket, Prefix, **kw):
        keys = [k for (b, k) in self.store if b == Bucket and k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def delete_objects(self, Bucket, Delete, **kw):
        for o in Delete["Objects"]:
            self.store.pop((Bucket, o["Key"]), None)
        return {}


@pytest.fixture
def fake(monkeypatch):
    client = _FakeS3()
    monkeypatch.setenv("R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_FRAMES_BUCKET", "io-image-frames")
    monkeypatch.setattr(object_storage, "_client", lambda: client)
    return client


def test_enabled_requires_creds_and_bucket(monkeypatch):
    for k in ("R2_ENDPOINT", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
              "R2_SECRET_ACCESS_KEY", "R2_FRAMES_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    assert object_storage.enabled() is False
    monkeypatch.setenv("R2_ENDPOINT", "https://x")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    assert object_storage.enabled() is False  # bucket still missing
    monkeypatch.setenv("R2_FRAMES_BUCKET", "b")
    assert object_storage.enabled() is True


def test_endpoint_derived_from_account_id(monkeypatch):
    for k in ("R2_ENDPOINT", "R2_ACCOUNT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_FRAMES_BUCKET", "b")
    assert object_storage.enabled() is False  # no endpoint, no account id
    monkeypatch.setenv("R2_ACCOUNT_ID", "abc123")
    assert object_storage.enabled() is True  # endpoint derived from account id


def test_frame_key_format():
    assert object_storage.frame_key("u1", "abc123") == "frames/u1/abc123"


def test_put_get_roundtrip_preserves_body_ct(fake):
    body_ct = base64.b64encode(b"\x00\xff\x10ciphertext\x80").decode()
    key = object_storage.put_frame_body("u1", "f1", body_ct)
    assert key == "frames/u1/f1"
    # Stored as raw decoded bytes (smaller than base64), not the b64 string.
    assert fake.store[("io-image-frames", "frames/u1/f1")] == base64.b64decode(body_ct)
    # Read back re-encodes to the exact same base64 string.
    assert object_storage.get_frame_body("u1", "f1") == body_ct


def test_get_missing_returns_none(fake):
    assert object_storage.get_frame_body("u1", "ghost") is None


def test_delete_frame_body(fake):
    object_storage.put_frame_body("u1", "f1", base64.b64encode(b"x").decode())
    object_storage.delete_frame_body("u1", "f1")
    assert object_storage.get_frame_body("u1", "f1") is None


def test_delete_user_frames_removes_only_that_prefix(fake):
    object_storage.put_frame_body("u1", "f1", base64.b64encode(b"a").decode())
    object_storage.put_frame_body("u1", "f2", base64.b64encode(b"b").decode())
    object_storage.put_frame_body("u2", "f1", base64.b64encode(b"c").decode())
    object_storage.delete_user_frames("u1")
    assert object_storage.get_frame_body("u1", "f1") is None
    assert object_storage.get_frame_body("u1", "f2") is None
    assert object_storage.get_frame_body("u2", "f1") is not None


def test_delete_user_frames_also_reaps_tee_prefix(fake):
    """Account reset must delete BOTH frames/<u>/ and frames-tee/<u>/ — the TEE
    storage-layer mirror is user data too. Other users' tee objects survive."""
    ct = base64.b64encode(b"a").decode()
    object_storage.put_frame_body("u1", "f1", ct)
    object_storage.put_frame_tee_body("u1", "f1", ct)
    object_storage.put_frame_tee_body("u2", "f1", ct)
    object_storage.delete_user_frames("u1")
    assert ("io-image-frames", "frames-tee/u1/f1") not in fake.store
    assert ("io-image-frames", "frames/u1/f1") not in fake.store
    assert ("io-image-frames", "frames-tee/u2/f1") in fake.store


def test_frame_tee_key_format():
    assert object_storage.frame_tee_key("u1", "abc") == "frames-tee/u1/abc"


def test_put_frame_tee_body_stores_raw_bytes(fake):
    ct = base64.b64encode(b"\x00sealed\xff").decode()
    key = object_storage.put_frame_tee_body("u1", "f1", ct)
    assert key == "frames-tee/u1/f1"
    assert fake.store[("io-image-frames", key)] == base64.b64decode(ct)


def test_delete_frame_tee_body(fake):
    ct = base64.b64encode(b"sealed").decode()
    object_storage.put_frame_tee_body("u1", "f1", ct)
    assert ("io-image-frames", "frames-tee/u1/f1") in fake.store
    object_storage.delete_frame_tee_body("u1", "f1")
    assert ("io-image-frames", "frames-tee/u1/f1") not in fake.store


def test_get_frame_body_strict_none_only_on_404(fake):
    """strict: definitive NoSuchKey → None (orphan); any other error raises
    (transient — the tee_replicator must freeze-and-retry, not skip)."""
    assert object_storage.get_frame_body_strict("u1", "ghost") is None

    ct = base64.b64encode(b"x").decode()
    object_storage.put_frame_body("u1", "f1", ct)
    assert object_storage.get_frame_body_strict("u1", "f1") == ct

    def _transient(Bucket, Key, **kw):
        raise _ClientError("SlowDown")  # throttling — not a 404

    fake.get_object = _transient
    with pytest.raises(Exception):
        object_storage.get_frame_body_strict("u1", "f1")
    # the lenient variant still swallows the same error
    assert object_storage.get_frame_body("u1", "f1") is None


def test_get_frame_body_strict_nosuchbucket_raises_not_none(fake):
    """Round 4 Fix 2 (P2): NoSuchBucket must NOT be treated as an orphaned
    object. A misconfigured/unavailable bucket is a deployment fault, not
    proof that this specific object was deleted — the strict variant must
    raise (freeze-and-retry) so the tee_replicator doesn't mass-mark every
    R2-backed frame as pending, which would not self-heal even after the
    bucket config is fixed (the cursor would already have advanced past
    them). Only NoSuchKey/404 (object-level) may return None."""
    def _no_bucket(Bucket, Key, **kw):
        raise _ClientError("NoSuchBucket")

    fake.get_object = _no_bucket
    with pytest.raises(Exception):
        object_storage.get_frame_body_strict("u1", "f1")


def test_is_missing_object_excludes_nosuchbucket():
    assert object_storage._is_missing_object(_ClientError("NoSuchKey")) is True
    assert object_storage._is_missing_object(_ClientError("404")) is True
    assert object_storage._is_missing_object(_ClientError("NoSuchBucket")) is False
    assert object_storage._is_missing_object(_ClientError("SlowDown")) is False
