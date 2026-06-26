"""db.py frame functions with R2 offload enabled.

These exercise the split path: ``body_ct`` lives in R2, the small envelope
metadata + a pointer live in Postgres. A fake S3 client stands in for boto3.
The legacy (R2-disabled) behaviour is covered by test_db.py.
"""

import base64
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import object_storage  # noqa: E402


class _Streaming:
    def __init__(self, data: bytes):
        self._d = data

    def read(self) -> bytes:
        return self._d


class _ClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3:
    def __init__(self, fail_put: bool = False):
        self.store: dict[tuple, bytes] = {}
        self.fail_put = fail_put

    def put_object(self, Bucket, Key, Body, **kw):
        if self.fail_put:
            raise _ClientError("InternalError")
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


def _enable_r2(monkeypatch, client):
    monkeypatch.setenv("FEEDLING_R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("FEEDLING_R2_ACCESS_KEY", "ak")
    monkeypatch.setenv("FEEDLING_R2_SECRET_KEY", "sk")
    monkeypatch.setenv("FEEDLING_R2_BUCKET", "feedling-frames")
    monkeypatch.setattr(object_storage, "_client", lambda: client)


def _uid() -> str:
    return f"u_{uuid.uuid4().hex[:10]}"


def _env(uid: str, fid: str, body: bytes = b"\x00\xffJPEG\x80") -> dict:
    return {
        "id": fid,
        "v": 1,
        "body_ct": base64.b64encode(body).decode(),
        "nonce": base64.b64encode(b"123456789012").decode(),
        "K_user": base64.b64encode(b"user-key").decode(),
        "K_enclave": base64.b64encode(b"enc-key").decode(),
        "visibility": "shared",
        "owner_user_id": uid,
    }


def _row(uid: str, fid: str):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT doc, env_meta, body_key FROM frame_envelopes "
            "WHERE user_id = %s AND frame_id = %s",
            (uid, fid),
        ).fetchone()


def test_upsert_offloads_body_to_r2(monkeypatch):
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    env = _env(uid, "f1")
    db.frame_upsert(uid, "f1", 1.0, env)

    # ciphertext landed in R2 as raw decoded bytes
    assert fake.store[("feedling-frames", "frames/%s/f1" % uid)] == base64.b64decode(env["body_ct"])
    # PG row holds the pointer + metadata, no inline doc, no body_ct in env_meta
    doc, env_meta, body_key = _row(uid, "f1")
    assert doc is None
    assert body_key == "frames/%s/f1" % uid
    assert "body_ct" not in env_meta
    assert env_meta["nonce"] == env["nonce"]
    assert env_meta["owner_user_id"] == uid


def test_get_reconstructs_full_envelope(monkeypatch):
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    env = _env(uid, "f1")
    db.frame_upsert(uid, "f1", 1.0, env)
    assert db.frame_get(uid, "f1") == env  # byte-for-byte, incl body_ct


def test_caller_dict_not_mutated(monkeypatch):
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    env = _env(uid, "f1")
    db.frame_upsert(uid, "f1", 1.0, env)
    assert "body_ct" in env  # upsert must not pop body_ct off the caller's dict


def test_get_returns_none_when_r2_body_missing(monkeypatch):
    # Offloaded row exists but the R2 object is gone — must read as not-found,
    # never a metadata-only dict (which downstream would 200/export w/o body_ct).
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    db.frame_upsert(uid, "f1", 1.0, _env(uid, "f1"))
    fake.store.clear()  # the R2 body vanished
    assert db.frame_get(uid, "f1") is None


class _BoomConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        raise RuntimeError("db down")


class _BoomPool:
    def connection(self):
        return _BoomConn()


def test_delete_keeps_r2_when_db_delete_fails(monkeypatch):
    # If the DB row delete raises, the row survives — so its R2 body must too,
    # otherwise reads of the still-present row return a corrupt (bodyless) frame.
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    db.frame_upsert(uid, "f1", 1.0, _env(uid, "f1"))
    assert ("feedling-frames", "frames/%s/f1" % uid) in fake.store
    monkeypatch.setattr(db, "get_pool", lambda: _BoomPool())
    db.frame_delete(uid, "f1")
    assert ("feedling-frames", "frames/%s/f1" % uid) in fake.store


def test_delete_removes_r2_object(monkeypatch):
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    db.frame_upsert(uid, "f1", 1.0, _env(uid, "f1"))
    db.frame_delete(uid, "f1")
    assert db.frame_exists(uid, "f1") is False
    assert ("feedling-frames", "frames/%s/f1" % uid) not in fake.store


def test_prune_removes_r2_objects(monkeypatch):
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    for i in range(4):
        db.frame_upsert(uid, f"f{i}", float(i), _env(uid, f"f{i}"))
    evicted = db.frame_prune_to(uid, 2)  # keep newest 2 (f2, f3)
    assert set(evicted) == {"f0", "f1"}
    assert ("feedling-frames", "frames/%s/f0" % uid) not in fake.store
    assert ("feedling-frames", "frames/%s/f1" % uid) not in fake.store
    assert ("feedling-frames", "frames/%s/f2" % uid) in fake.store


def test_delete_user_data_purges_r2(monkeypatch):
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    db.frame_upsert(uid, "f1", 1.0, _env(uid, "f1"))
    db.frame_upsert(uid, "f2", 2.0, _env(uid, "f2"))
    db.delete_user_data(uid)
    assert db.frame_list_meta(uid) == []
    assert not [k for (b, k) in fake.store if k.startswith("frames/%s/" % uid)]


def test_list_meta_reads_env_meta_without_r2(monkeypatch):
    fake = _FakeS3()
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    db.frame_upsert(uid, "f1", 1.0, _env(uid, "f1"))
    # drop the R2 object to prove list_meta never reaches for the body
    fake.store.clear()
    meta = db.frame_list_meta(uid)
    assert len(meta) == 1
    assert meta[0]["id"] == "f1"
    assert meta[0]["owner_user_id"] == uid


def test_upsert_falls_back_to_inline_doc_on_upload_failure(monkeypatch):
    fake = _FakeS3(fail_put=True)
    _enable_r2(monkeypatch, fake)
    uid = _uid()
    env = _env(uid, "f1")
    db.frame_upsert(uid, "f1", 1.0, env)
    # upload failed → row keeps inline doc, no pointer; read still works
    doc, env_meta, body_key = _row(uid, "f1")
    assert body_key is None
    assert doc is not None and doc["body_ct"] == env["body_ct"]
    assert db.frame_get(uid, "f1") == env
