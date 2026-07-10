"""worker.run_table integration tests against the double-DB fixtures.

The enclave decrypt is stubbed at ``worker._make_decrypt`` (never touches the
CVM). Real RDS rows go into the throwaway test DB; assertions read the TEE
shadow DB directly. Covers: first-run full copy + cursor advance, idempotent
increment, local_only → pending, dry_run zero-write, qps sleep injection, and a
mid-batch decrypt failure freezing the cursor before the failed row.
"""

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from tee_replicator import worker  # noqa: E402
from conftest import seed_user  # noqa: E402


def _tee(sql, params=()):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        return c.execute(sql, params).fetchall()


def _stub_decrypt(envelope, purpose):
    return b"PT:" + envelope["body_ct"].encode()


def _make_stub(_user_id):
    return _stub_decrypt


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(worker, "_make_decrypt", _make_stub)
    worker._decrypt_cache.clear()
    yield
    worker._decrypt_cache.clear()


def _reset_cursor(table):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("DELETE FROM tee_replication_cursors WHERE table_name=%s", (table,))


def _seed(uid):
    """Seed the user in BOTH the RDS (via registry) and the TEE users table.

    The replicator writes per-user content rows whose CASCADE FK points at the
    TEE ``users`` table; in production the reconciler backfills ``users`` first.
    Dual-write is off in tests, so seed the TEE side explicitly here.
    """
    seed_user(uid, api_key_hash="h", doc={})
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO users (user_id, doc) VALUES (%s, '{}'::jsonb) "
                  "ON CONFLICT (user_id) DO NOTHING", (uid,))


def _chat_doc(uid, msg_id, body_ct, visibility="shared", k_enclave="ke"):
    doc = {"id": msg_id, "role": "user", "ts": None, "source": "chat",
           "content_type": "text", "v": 1, "body_ct": body_ct, "nonce": "n",
           "K_user": "k", "enclave_pk_fpr": "f", "visibility": visibility,
           "owner_user_id": uid}
    if k_enclave is not None:
        doc["K_enclave"] = k_enclave
    return doc


def _insert_chat(uid, msg_id, ts, body_ct, **kw):
    doc = _chat_doc(uid, msg_id, body_ct, **kw)
    doc["ts"] = ts
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,%s,%s,%s)",
                  (uid, msg_id, ts, Jsonb(doc)))


def test_first_run_copies_and_advances_cursor(backend_env):
    _reset_cursor("chat_messages")
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_chat(uid, "a", 10.0, "AAA")
    _insert_chat(uid, "b", 20.0, "BBB")

    report = worker.run_table("chat_messages", qps=1000, dry_run=False)

    assert report["copied"] == 2 and report["pending"] == 0 and report["errors"] == 0
    rows = _tee("SELECT doc->>'body' FROM chat_messages WHERE user_id=%s ORDER BY ts", (uid,))
    assert [r[0] for r in rows] == ["PT:AAA", "PT:BBB"]
    # no ciphertext survives into the TEE plaintext doc
    leak = _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s AND doc ? 'body_ct'", (uid,))
    assert leak[0][0] == 0
    cur = _tee("SELECT watermark_ts, watermark_id FROM tee_replication_cursors WHERE table_name='chat_messages'")
    assert cur[0][0] == 20.0 and cur[0][1] == "b"


def test_idempotent_increment(backend_env):
    _reset_cursor("chat_messages")
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_chat(uid, "a", 10.0, "AAA")
    worker.run_table("chat_messages", qps=1000)

    # rerun with no new rows → nothing copied
    report2 = worker.run_table("chat_messages", qps=1000)
    assert report2["copied"] == 0

    # add one newer row → only the increment is copied
    _insert_chat(uid, "c", 30.0, "CCC")
    report3 = worker.run_table("chat_messages", qps=1000)
    assert report3["copied"] == 1
    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s", (uid,))[0][0] == 2


def test_local_only_goes_to_pending(backend_env):
    _reset_cursor("chat_messages")
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_chat(uid, "a", 10.0, "AAA")
    _insert_chat(uid, "loc", 20.0, "XXX", visibility="local_only", k_enclave=None)
    _insert_chat(uid, "b", 30.0, "BBB")

    report = worker.run_table("chat_messages", qps=1000)

    assert report["copied"] == 2 and report["pending"] == 1
    pend = _tee("SELECT item_id FROM tee_pending_device_migration WHERE user_id=%s AND table_name='chat_messages'", (uid,))
    assert [r[0] for r in pend] == ["loc"]
    # cursor still advanced past the local_only row to the last row
    cur = _tee("SELECT watermark_id FROM tee_replication_cursors WHERE table_name='chat_messages'")
    assert cur[0][0] == "b"
    # local_only row not copied to TEE content table
    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s AND msg_id='loc'", (uid,))[0][0] == 0


def test_dry_run_zero_writes(backend_env):
    _reset_cursor("chat_messages")
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_chat(uid, "a", 10.0, "AAA")

    report = worker.run_table("chat_messages", qps=1000, dry_run=True)

    assert report["copied"] == 1  # would_copy
    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s", (uid,))[0][0] == 0
    assert _tee("SELECT count(*) FROM tee_replication_cursors WHERE table_name='chat_messages'")[0][0] == 0


def test_qps_sleep_injected(backend_env, monkeypatch):
    _reset_cursor("chat_messages")
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_chat(uid, "a", 10.0, "AAA")
    _insert_chat(uid, "b", 20.0, "BBB")

    sleeps = []
    monkeypatch.setattr(worker, "_sleep", lambda s: sleeps.append(s))
    worker.run_table("chat_messages", qps=2.0)
    # one batch of 2 rows at 2 qps → sleeps 2/2 == 1.0s
    assert sleeps and sleeps[0] == pytest.approx(1.0)


def test_stale_token_reminted_and_retry_uses_fresh_token(backend_env, monkeypatch):
    """TTL-aware decrypt cache: (a) an over-age cached entry is never reused
    (age > _TOKEN_MAX_AGE → re-mint), and (b) a 401-shaped decrypt failure
    forces a re-mint before the retry, which then succeeds with the fresh
    token — the row copies instead of freezing the cursor."""
    import time as _time

    _reset_cursor("chat_messages")
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_chat(uid, "a", 10.0, "AAA")

    mints = []

    def _minting(user_id):
        gen = len(mints)
        mints.append(user_id)

        def decrypt(envelope, purpose):
            if gen == 0:  # first token → enclave rejects it as expired
                raise RuntimeError("enclave_http_401:token_expired")
            return b"PT:" + envelope["body_ct"].encode()

        return decrypt

    def _poison(envelope, purpose):
        raise AssertionError("over-age cached token was reused")

    monkeypatch.setattr(worker, "_make_decrypt", _minting)
    worker._decrypt_cache.clear()
    # pre-seed an over-age cache entry: the age check must discard it unused
    worker._decrypt_cache[uid] = (_time.time() - worker._TOKEN_MAX_AGE - 1, _poison)

    report = worker.run_table("chat_messages", qps=1000)

    assert report["copied"] == 1 and report["errors"] == 0
    # mint #1 (age-based re-mint, its token 401s) + mint #2 (auth-failure re-mint)
    assert mints == [uid, uid]
    assert _tee("SELECT doc->>'body' FROM chat_messages WHERE user_id=%s AND msg_id='a'",
                (uid,))[0][0] == "PT:AAA"
    # the fresh entry replaced the stale one in the cache
    assert _time.time() - worker._decrypt_cache[uid][0] < worker._TOKEN_MAX_AGE


def test_identity_envelope_replicated_as_plaintext(backend_env):
    _reset_cursor("identity")
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    env = {"id": "identity", "visibility": "shared", "owner_user_id": uid, "v": 1,
           "body_ct": "IDID", "nonce": "n", "K_user": "k", "K_enclave": "ke",
           "enclave_pk_fpr": "f", "display_name": "Ada"}
    db.set_blob(uid, "identity", env)

    report = worker.run_table("identity", qps=1000)

    assert report["copied"] == 1
    row = _tee("SELECT doc->>'body', doc->>'display_name', doc ? 'body_ct' "
               "FROM user_blobs WHERE user_id=%s AND kind='identity'", (uid,))
    assert row[0][0] == "PT:IDID" and row[0][1] == "Ada" and row[0][2] is False


def test_seq_preserved_and_setval_prevents_collision(backend_env):
    """chat seq must ride along from RDS verbatim — the cursor's ORDER BY ts
    controls replay/traversal order, not the stored seq value. Insert three
    rows whose ts order is intentionally scrambled vs their RDS
    insertion/seq order, then assert (a) each TEE row's seq matches its RDS
    counterpart exactly (not renumbered by TEE arrival order) and (b) TEE's
    own ``ORDER BY seq`` reproduces the true RDS seq order — then assert the
    post-pass setval actually moved the TEE identity sequence past the
    highest carried-over value, so a later direct-write insert (the
    post-cutover world once RDS is retired) can't mint a colliding seq."""
    _reset_cursor("chat_messages")
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    # Insertion order (== RDS seq order): x1, x2, x3. ts order: x2(1.0),
    # x3(2.0), x1(3.0) — deliberately the reverse-ish of seq order.
    _insert_chat(uid, "x1", 3.0, "AAA")
    _insert_chat(uid, "x2", 1.0, "BBB")
    _insert_chat(uid, "x3", 2.0, "CCC")

    with db.get_pool().connection() as c:
        rds_seq = dict(c.execute(
            "SELECT msg_id, seq FROM chat_messages WHERE user_id=%s", (uid,)).fetchall())
    assert rds_seq["x1"] < rds_seq["x2"] < rds_seq["x3"]

    report = worker.run_table("chat_messages", qps=1000)
    assert report["copied"] == 3 and report["errors"] == 0

    tee_seq = dict(_tee("SELECT msg_id, seq FROM chat_messages WHERE user_id=%s", (uid,)))
    assert tee_seq == rds_seq  # carried over verbatim, not TEE-renumbered

    # TEE's own seq order reproduces the RDS seq order (x1, x2, x3) — NOT the
    # ts order (which would be x2, x3, x1).
    by_seq = [r[0] for r in _tee(
        "SELECT msg_id FROM chat_messages WHERE user_id=%s ORDER BY seq", (uid,))]
    assert by_seq == ["x1", "x2", "x3"]

    # setval: a plain DEFAULT insert straight into TEE (no explicit seq —
    # the post-cutover direct-write world) must mint a seq past the highest
    # carried-over value, not collide with/duplicate an already-replicated one.
    max_seq = max(rds_seq.values())
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        new_seq = c.execute(
            "INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,%s,%s,%s) "
            "RETURNING seq",
            (uid, "post-cutover", 99.0, Jsonb({"id": "post-cutover"}))).fetchone()[0]
    assert new_seq > max_seq


def test_decrypt_failure_freezes_cursor_before_failed_row(backend_env, monkeypatch):
    _reset_cursor("chat_messages")
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_chat(uid, "a", 10.0, "AAA")
    _insert_chat(uid, "bad", 20.0, "BAD")
    _insert_chat(uid, "c", 30.0, "CCC")

    def _flaky(_user_id):
        def decrypt(envelope, purpose):
            if envelope["body_ct"] == "BAD":
                raise RuntimeError("enclave_http_403")
            return b"PT:" + envelope["body_ct"].encode()
        return decrypt

    monkeypatch.setattr(worker, "_make_decrypt", _flaky)
    worker._decrypt_cache.clear()

    report = worker.run_table("chat_messages", qps=1000)

    assert report["errors"] == 1
    # cursor frozen at the last good row BEFORE the failure ("a"), so a rerun retries "bad"
    cur = _tee("SELECT watermark_id FROM tee_replication_cursors WHERE table_name='chat_messages'")
    assert cur[0][0] == "a"
    # the later good row "c" was still processed this batch
    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s AND msg_id='c'", (uid,))[0][0] == 1


def _insert_chat_file_pointer(uid, msg_id, ts):
    """R2-offloaded file row: envelope metadata + body_key pointer, NO body_ct
    (the shape db.chat_append leaves behind after a successful offload)."""
    doc = _chat_doc(uid, msg_id, "IGNORED")
    del doc["body_ct"]
    doc["content_type"] = "file"
    doc["body_key"] = f"chatfiles/{uid}/{msg_id}"
    doc["body_ct_len"] = 7
    doc["ts"] = ts
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,%s,%s,%s)",
                  (uid, msg_id, ts, Jsonb(doc)))


def test_offloaded_file_row_hydrated_from_r2_then_replicated(backend_env, monkeypatch):
    """chat-file R2 offload (R2_CHAT_FILES_BUCKET) leaves pointer rows with no
    body_ct; _chat_unpack must hydrate them back before the enclave transform,
    or the row freezes the chat cursor forever."""
    import object_storage

    _reset_cursor("chat_messages")
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_chat(uid, "a", 10.0, "AAA")
    _insert_chat_file_pointer(uid, "filemsg", 20.0)

    monkeypatch.setattr(object_storage, "chat_files_enabled", lambda: True)
    monkeypatch.setattr(object_storage, "get_chat_file_body",
                        lambda u, m: "FILECT" if (u, m) == (uid, "filemsg") else None)

    report = worker.run_table("chat_messages", qps=1000)

    assert report["copied"] == 2 and report["errors"] == 0
    body = _tee("SELECT doc->>'body' FROM chat_messages WHERE user_id=%s AND msg_id='filemsg'", (uid,))
    assert body[0][0] == "PT:FILECT"
    # pointer keys never land in the TEE plaintext doc's crypto fields; the body
    # arrives decrypted inline (body_key survives as an inert metadata field)
    leak = _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s AND doc ? 'body_ct'", (uid,))
    assert leak[0][0] == 0
    cur = _tee("SELECT watermark_id FROM tee_replication_cursors WHERE table_name='chat_messages'")
    assert cur[0][0] == "filemsg"


def test_offloaded_file_row_hydration_failure_freezes_cursor(backend_env, monkeypatch):
    """R2 fetch failure → doc stays a pointer → transform fails → freeze (same
    retry-next-pass policy as any transient error), later rows still copy."""
    import object_storage

    _reset_cursor("chat_messages")
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_chat(uid, "a", 10.0, "AAA")
    _insert_chat_file_pointer(uid, "filemsg", 20.0)
    _insert_chat(uid, "c", 30.0, "CCC")

    monkeypatch.setattr(object_storage, "chat_files_enabled", lambda: True)
    monkeypatch.setattr(object_storage, "get_chat_file_body", lambda u, m: None)

    def _strict(_user_id):
        def decrypt(envelope, purpose):
            if "body_ct" not in envelope:
                raise RuntimeError("enclave_http_400:missing body_ct")
            return b"PT:" + envelope["body_ct"].encode()
        return decrypt

    monkeypatch.setattr(worker, "_make_decrypt", _strict)
    worker._decrypt_cache.clear()

    report = worker.run_table("chat_messages", qps=1000)

    assert report["errors"] == 1
    cur = _tee("SELECT watermark_id FROM tee_replication_cursors WHERE table_name='chat_messages'")
    assert cur[0][0] == "a"
    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s AND msg_id='c'", (uid,))[0][0] == 1
