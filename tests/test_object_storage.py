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
    monkeypatch.setenv("FEEDLING_R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("FEEDLING_R2_ACCESS_KEY", "ak")
    monkeypatch.setenv("FEEDLING_R2_SECRET_KEY", "sk")
    monkeypatch.setenv("FEEDLING_R2_BUCKET", "feedling-frames")
    monkeypatch.setattr(object_storage, "_client", lambda: client)
    return client


def test_enabled_requires_all_four_env(monkeypatch):
    for k in ("FEEDLING_R2_ENDPOINT", "FEEDLING_R2_ACCESS_KEY",
              "FEEDLING_R2_SECRET_KEY", "FEEDLING_R2_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    assert object_storage.enabled() is False
    monkeypatch.setenv("FEEDLING_R2_ENDPOINT", "https://x")
    monkeypatch.setenv("FEEDLING_R2_ACCESS_KEY", "ak")
    monkeypatch.setenv("FEEDLING_R2_SECRET_KEY", "sk")
    assert object_storage.enabled() is False  # bucket still missing
    monkeypatch.setenv("FEEDLING_R2_BUCKET", "b")
    assert object_storage.enabled() is True


def test_frame_key_format():
    assert object_storage.frame_key("u1", "abc123") == "frames/u1/abc123"


def test_put_get_roundtrip_preserves_body_ct(fake):
    body_ct = base64.b64encode(b"\x00\xff\x10ciphertext\x80").decode()
    key = object_storage.put_frame_body("u1", "f1", body_ct)
    assert key == "frames/u1/f1"
    # Stored as raw decoded bytes (smaller than base64), not the b64 string.
    assert fake.store[("feedling-frames", "frames/u1/f1")] == base64.b64decode(body_ct)
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
