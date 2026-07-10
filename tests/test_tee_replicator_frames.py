"""frames dual-form replication (inline legacy + R2-backed) → TEE frames rows.

The enclave storage re-encryption is stubbed at ``worker._make_reencrypt`` (never
touches the CVM); R2 get/put are stubbed on the ``object_storage`` module the
``frames`` module imports. Real RDS ``frame_envelopes`` rows go into the throwaway
test DB; assertions read the TEE shadow DB directly.

Covers the brief's four cases: an R2-backed row and an inline legacy row produce
the SAME TEE frames row (pointer + checksums, never the body ciphertext); a
local_only / no-K_enclave row goes to the pending table without any enclave/R2
call; and an idempotent replay overwrites the same deterministic R2 key without
duplicating the TEE row.
"""
import base64
import hashlib
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import object_storage  # noqa: E402
from tee_replicator import frames as frames_mod  # noqa: E402
from tee_replicator import worker  # noqa: E402
from conftest import seed_user  # noqa: E402

KEY_VERSION = "v1"


def _tee(sql, params=()):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        return c.execute(sql, params).fetchall()


def _seed(uid):
    seed_user(uid, api_key_hash="h", doc={})
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO users (user_id, doc) VALUES (%s, '{}'::jsonb) "
                  "ON CONFLICT (user_id) DO NOTHING", (uid,))


def _stub_reencrypt(envelope, key_version):
    """Deterministic stand-in for the enclave storage endpoint: 'decrypt' by
    prefixing, then report checksums of that plaintext (computed enclave-side)."""
    pt = b"PT:" + envelope["body_ct"].encode()
    sealed = base64.b64encode(b"SEALED:" + pt).decode()
    return {"body_ct_storage": sealed, "key_version": key_version,
            "sha256": hashlib.sha256(pt).hexdigest(), "size": len(pt)}


def _make_stub_reencrypt(_user_id):
    return _stub_reencrypt


class _FakeR2:
    """In-memory stand-in for object_storage: a body store keyed by the legacy
    frames/<user>/<frame> key, plus a record of frames-tee/ puts."""
    def __init__(self):
        self.bodies = {}          # frames/<u>/<f> -> b64 ct
        self.tee_puts = []        # (key, b64 ct)

    def install(self, monkeypatch):
        # frames.py uses the strict variant: None = definitive 404 (orphan),
        # raise = transient failure (freeze semantics).
        monkeypatch.setattr(object_storage, "get_frame_body_strict",
                            lambda u, f: self.bodies.get(f"frames/{u}/{f}"))

        def put_tee(u, f, body_b64):
            key = f"frames-tee/{u}/{f}"
            self.tee_puts.append((key, body_b64))
            return key
        monkeypatch.setattr(object_storage, "put_frame_tee_body", put_tee)


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(worker, "_make_reencrypt", _make_stub_reencrypt)
    worker._reencrypt_cache.clear()
    yield
    worker._reencrypt_cache.clear()


@pytest.fixture()
def r2(monkeypatch):
    fake = _FakeR2()
    fake.install(monkeypatch)
    return fake


def _reset_cursor():
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("DELETE FROM tee_replication_cursors WHERE table_name='frame_envelopes'")


def _env_meta(uid, frame_id, visibility="shared", k_enclave="ke"):
    """Envelope minus body_ct (the R2-backed env_meta / doc common fields)."""
    m = {"v": 1, "id": frame_id, "nonce": "n", "K_user": "k",
         "enclave_pk_fpr": "f", "visibility": visibility, "owner_user_id": uid,
         "source": "screen"}
    if k_enclave is not None:
        m["K_enclave"] = k_enclave
    return m


def _insert_inline(uid, frame_id, ts, body_ct, **kw):
    doc = {**_env_meta(uid, frame_id, **kw), "body_ct": body_ct}
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO frame_envelopes (user_id, frame_id, ts, doc) "
                  "VALUES (%s,%s,%s,%s)", (uid, frame_id, ts, Jsonb(doc)))


def _insert_r2(uid, frame_id, ts, body_ct, r2, **kw):
    env_meta = _env_meta(uid, frame_id, **kw)
    body_key = f"frames/{uid}/{frame_id}"
    r2.bodies[body_key] = body_ct
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO frame_envelopes (user_id, frame_id, ts, doc, env_meta, body_key) "
                  "VALUES (%s,%s,%s,NULL,%s,%s)",
                  (uid, frame_id, ts, Jsonb(env_meta), body_key))


def test_r2_backed_row_replicated(backend_env, r2):
    _reset_cursor()
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_r2(uid, "aa" * 8, 10.0, "AAA", r2)

    report = worker.run_table("frame_envelopes", qps=1000)

    assert report["copied"] == 1 and report["pending"] == 0 and report["errors"] == 0
    row = _tee("SELECT meta, body_storage_key, body_storage_key_version, "
               "body_sha256, body_size_bytes FROM frames WHERE user_id=%s", (uid,))
    assert len(row) == 1
    meta, skey, sver, sha, size = row[0]
    assert skey == f"frames-tee/{uid}/{'aa' * 8}"
    assert sver == KEY_VERSION
    pt = b"PT:AAA"
    assert sha == hashlib.sha256(pt).hexdigest()
    assert size == len(pt)
    # meta carries semantic fields, never the body ciphertext or wrap keys
    assert meta["source"] == "screen" and meta["owner_user_id"] == uid
    for banned in ("body_ct", "K_enclave", "K_user", "nonce", "v"):
        assert banned not in meta
    # the storage ciphertext went to the frames-tee/ prefix, not a TEE column
    assert r2.tee_puts and r2.tee_puts[0][0] == skey


def test_inline_legacy_row_same_result(backend_env, r2):
    _reset_cursor()
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_inline(uid, "bb" * 8, 10.0, "AAA")

    report = worker.run_table("frame_envelopes", qps=1000)

    assert report["copied"] == 1
    pt = b"PT:AAA"
    row = _tee("SELECT body_storage_key, body_sha256, body_size_bytes "
               "FROM frames WHERE user_id=%s", (uid,))
    assert row[0][0] == f"frames-tee/{uid}/{'bb' * 8}"
    assert row[0][1] == hashlib.sha256(pt).hexdigest()
    assert row[0][2] == len(pt)
    # no ciphertext body ever lands in a TEE frames row (meta is pointer-only)
    leak = _tee("SELECT count(*) FROM frames WHERE user_id=%s AND meta ? 'body_ct'", (uid,))
    assert leak[0][0] == 0


def test_local_only_goes_to_pending(backend_env, r2, monkeypatch):
    _reset_cursor()
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_inline(uid, "cc" * 8, 10.0, "AAA")
    _insert_inline(uid, "dd" * 8, 20.0, "XXX", visibility="local_only", k_enclave=None)

    # a local_only row must never call the enclave or R2
    def _boom(_u):
        def r(env, kv):
            raise AssertionError("enclave called for local_only frame")
        return r
    monkeypatch.setattr(worker, "_make_reencrypt", _boom)
    worker._reencrypt_cache.clear()

    report = worker.run_table("frame_envelopes", qps=1000)
    # only the shared frame reencrypts; the local_only one is pending
    assert report["pending"] == 1
    pend = _tee("SELECT item_id FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='frame_envelopes'", (uid,))
    assert [r[0] for r in pend] == ["dd" * 8]
    assert _tee("SELECT count(*) FROM frames WHERE user_id=%s AND frame_id=%s",
                (uid, "dd" * 8))[0][0] == 0


def test_idempotent_replay(backend_env, r2):
    _reset_cursor()
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_r2(uid, "ee" * 8, 10.0, "AAA", r2)

    worker.run_table("frame_envelopes", qps=1000)
    _reset_cursor()  # force a full replay of the same row
    worker.run_table("frame_envelopes", qps=1000)

    # exactly one TEE row (ON CONFLICT upsert), and the R2 key is deterministic
    # (same key both puts → overwrite-safe, no orphan objects)
    assert _tee("SELECT count(*) FROM frames WHERE user_id=%s", (uid,))[0][0] == 1
    keys = {k for k, _ in r2.tee_puts}
    assert keys == {f"frames-tee/{uid}/{'ee' * 8}"}
    assert len(r2.tee_puts) == 2  # replayed, same key


def test_r2_orphan_body_key_goes_to_pending_not_wedge(backend_env, r2):
    """body_key set but the R2 object is definitively gone (404) → the row is a
    pending-style skip (reason r2_body_missing_orphan), the cursor advances past
    it, and later rows still replicate — an orphan must never wedge the cursor."""
    _reset_cursor()
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    # R2-backed row WITHOUT seeding the fake R2 body → strict fetch returns None
    env_meta = _env_meta(uid, "11" * 8)
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO frame_envelopes (user_id, frame_id, ts, doc, env_meta, body_key) "
                  "VALUES (%s,%s,%s,NULL,%s,%s)",
                  (uid, "11" * 8, 10.0, Jsonb(env_meta), f"frames/{uid}/{'11' * 8}"))
    _insert_inline(uid, "22" * 8, 20.0, "BBB")

    report = worker.run_table("frame_envelopes", qps=1000)

    assert report["pending"] == 1 and report["errors"] == 0 and report["copied"] == 1
    pend = _tee("SELECT item_id, reason FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='frame_envelopes'", (uid,))
    # Round-2 fix: PendingDeviceMigration-derived reasons carry a fixed "pdm:"
    # prefix (tee_replicator.worker._pdm_reason) so they can never collide with
    # the requeue lane's `reason LIKE 'requeue%'` match.
    assert pend == [("11" * 8, "pdm:r2_body_missing_orphan")]
    # cursor advanced to the LAST row — the orphan did not freeze it
    cur = _tee("SELECT watermark_id FROM tee_replication_cursors "
               "WHERE table_name='frame_envelopes'")
    assert cur[0][0] == "22" * 8
    assert _tee("SELECT count(*) FROM frames WHERE user_id=%s AND frame_id=%s",
                (uid, "11" * 8))[0][0] == 0


def test_r2_transient_failure_freezes_cursor(backend_env, r2, monkeypatch):
    """A transient R2 error (strict fetch raises) keeps the freeze-and-retry
    semantics: errors counted, cursor frozen before the failed row."""
    _reset_cursor()
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_inline(uid, "33" * 8, 10.0, "AAA")
    _insert_r2(uid, "44" * 8, 20.0, "BBB", r2)

    def _flaky(u, f):
        raise RuntimeError("r2 connreset")
    monkeypatch.setattr(object_storage, "get_frame_body_strict", _flaky)

    report = worker.run_table("frame_envelopes", qps=1000)

    assert report["errors"] == 1 and report["pending"] == 0
    cur = _tee("SELECT watermark_id FROM tee_replication_cursors "
               "WHERE table_name='frame_envelopes'")
    assert cur[0][0] == "33" * 8  # frozen at the last good row before the failure


def test_dry_run_no_side_effects(backend_env, r2):
    _reset_cursor()
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_r2(uid, "ff" * 8, 10.0, "AAA", r2)

    report = worker.run_table("frame_envelopes", qps=1000, dry_run=True)

    assert report["copied"] == 1  # would_copy
    assert r2.tee_puts == []       # no enclave call, no R2 put
    assert _tee("SELECT count(*) FROM frames WHERE user_id=%s", (uid,))[0][0] == 0
    assert _tee("SELECT count(*) FROM tee_replication_cursors "
                "WHERE table_name='frame_envelopes'")[0][0] == 0
