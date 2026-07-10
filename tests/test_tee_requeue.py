"""Requeue-lane compensation for in-place ciphertext rewrites (C1/C2/C3).

The cursor-based replicator only scans forward and never revisits a row whose PK
(and sort key) stayed put. Three write paths mutate rows in place — identity
UPDATE (C1), /v1/content/swap (C2), and memory/world_book replace-all (C3) — so
each enqueues a ``tee_pending_device_migration`` requeue row that
``worker.run_table`` drains at the start of the next pass.

Enclave decrypt is stubbed at ``worker._make_decrypt`` (never touches the CVM).
Dual-write is turned ON so the mirror actually writes to the throwaway TEE DB.
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
from content import content_core  # noqa: E402
from core import store as core_store  # noqa: E402
from identity import service as identity_service  # noqa: E402
from tee_replicator import worker  # noqa: E402
from conftest import seed_user  # noqa: E402


def _tee(sql, params=()):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        return c.execute(sql, params).fetchall()


def _tee_exec(sql, params=()):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute(sql, params)


def _uid():
    return f"usr_{uuid.uuid4().hex[:8]}"


def _rds(sql, params=()):
    with db.get_pool().connection() as c:
        return c.execute(sql, params).fetchall()


def _seed_both(uid):
    """Seed the user row in RDS (registry) + TEE (FK target for content rows)."""
    seed_user(uid, api_key_hash="h", doc={})
    _tee_exec("INSERT INTO users (user_id, doc) VALUES (%s, '{}'::jsonb) "
              "ON CONFLICT (user_id) DO NOTHING", (uid,))


def _set_cursor(table, wm_ts, wm_id):
    _tee_exec("INSERT INTO tee_replication_cursors (table_name, watermark_ts, watermark_id, updated_at) "
              "VALUES (%s,%s,%s, now()) ON CONFLICT (table_name) DO UPDATE SET "
              "watermark_ts=EXCLUDED.watermark_ts, watermark_id=EXCLUDED.watermark_id",
              (table, wm_ts, wm_id))


def _clear_cursor(table):
    _tee_exec("DELETE FROM tee_replication_cursors WHERE table_name=%s", (table,))


def _stub_decrypt(envelope, purpose):
    return b"PT:" + envelope["body_ct"].encode()


@pytest.fixture(autouse=True)
def _dual_write_and_stub(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    monkeypatch.setattr(worker, "_make_decrypt", lambda _uid: _stub_decrypt)
    worker._decrypt_cache.clear()
    yield
    worker._decrypt_cache.clear()


def _ct_env(uid, item_id, body_ct="CT", visibility="shared", k_enclave="ke"):
    env = {"id": item_id, "v": 1, "body_ct": body_ct, "nonce": "n", "K_user": "k",
           "enclave_pk_fpr": "f", "visibility": visibility, "owner_user_id": uid}
    if k_enclave is not None:
        env["K_enclave"] = k_enclave
    return env


# --------------------------------------------------------------------------- #
# C1 — identity: ciphertext must not clobber TEE plaintext; UPDATE requeues.
# --------------------------------------------------------------------------- #

def test_save_identity_does_not_clobber_tee_and_requeues():
    uid = _uid()
    _seed_both(uid)
    # A prior replicator pass left plaintext in the TEE user_blobs identity row.
    _tee_exec("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s,'identity',%s)",
              (uid, Jsonb({"persona": "plaintext"})))

    store = core_store.get_store(uid)
    identity_service._save_identity(store, _ct_env(uid, "identity", body_ct="IDID"))

    # The TEE plaintext row is NOT overwritten by the ciphertext (set_blob skips
    # the identity mirror).
    assert _tee("SELECT doc FROM user_blobs WHERE user_id=%s AND kind='identity'",
                (uid,)) == [({"persona": "plaintext"},)]
    # RDS holds the ciphertext envelope.
    assert db.get_blob(uid, "identity")["body_ct"] == "IDID"
    # A requeue row was enqueued (table_name/item_id match worker's identity table).
    pend = _tee("SELECT reason FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='identity' AND item_id='identity'", (uid,))
    assert pend == [("requeue",)]


def test_worker_identity_pass_consumes_requeue():
    uid = _uid()
    _seed_both(uid)
    _tee_exec("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s,'identity',%s)",
              (uid, Jsonb({"persona": "stale"})))
    store = core_store.get_store(uid)
    identity_service._save_identity(store, _ct_env(uid, "identity", body_ct="IDID"))
    # Advance the cursor PAST this user so only the requeue-consume path can act.
    _set_cursor("identity", 0.0, uid)

    report = worker.run_table("identity", qps=1000)

    # TEE plaintext re-derived from the current ciphertext; requeue row cleared.
    row = _tee("SELECT doc->>'body', doc ? 'body_ct' FROM user_blobs "
               "WHERE user_id=%s AND kind='identity'", (uid,))
    assert row == [("PT:IDID", False)]
    assert _tee("SELECT count(*) FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='identity'", (uid,))[0][0] == 0
    assert report["copied"] >= 1


# --------------------------------------------------------------------------- #
# C2 — /v1/content/swap: local_only deletes the TEE row; shared requeues.
# --------------------------------------------------------------------------- #

def _store_with_chat(uid, msg_id, ts, env):
    store = core_store.get_store(uid)
    msg = dict(env)
    msg["ts"] = ts
    with store.chat_lock:
        store.chat_messages = [msg]
    return store


def test_swap_chat_to_local_only_deletes_tee_row_and_marks_pending():
    uid = _uid()
    _seed_both(uid)
    # A replicated TEE plaintext chat row exists.
    _tee_exec("INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,'c1',10.0,%s)",
              (uid, Jsonb({"body": "PT:OLD"})))
    store = _store_with_chat(uid, "c1", 10.0, _ct_env(uid, "c1"))

    env = _ct_env(uid, "c1", body_ct="NEW", visibility="local_only", k_enclave=None)
    resp, code = content_core.swap(store, {"items": [{"type": "chat", "id": "c1", "envelope": env}]})
    assert code == 200 and resp["summary"]["ok"] == 1

    # TEE plaintext row deleted; a terminal visibility_local_only marker remains.
    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s AND msg_id='c1'",
                (uid,))[0][0] == 0
    assert _tee("SELECT reason FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='chat_messages' AND item_id='c1'",
                (uid,)) == [("visibility_local_only",)]


def test_swap_chat_to_shared_requeues_then_worker_restores():
    uid = _uid()
    _seed_both(uid)
    store = _store_with_chat(uid, "c1", 10.0, _ct_env(uid, "c1"))

    env = _ct_env(uid, "c1", body_ct="SHARED", visibility="shared")
    resp, code = content_core.swap(store, {"items": [{"type": "chat", "id": "c1", "envelope": env}]})
    assert code == 200 and resp["summary"]["ok"] == 1

    # A requeue row is present right after the swap.
    assert _tee("SELECT reason FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='chat_messages' AND item_id='c1'",
                (uid,)) == [("requeue_visibility_shared",)]

    # Advance the cursor past the row so ONLY the requeue-consume path restores it.
    _set_cursor("chat_messages", 10.0, "c1")
    worker.run_table("chat_messages", qps=1000)

    assert _tee("SELECT doc->>'body' FROM chat_messages WHERE user_id=%s AND msg_id='c1'",
                (uid,)) == [("PT:SHARED",)]
    assert _tee("SELECT count(*) FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='chat_messages'", (uid,))[0][0] == 0


# --------------------------------------------------------------------------- #
# C3 — memory_replace_all: removed rows deleted, survivors requeued.
# --------------------------------------------------------------------------- #

def test_memory_replace_all_deletes_removed_requeues_survivors_and_converges():
    uid = _uid()
    _seed_both(uid)
    # Seed 3 moments in BOTH DBs: RDS as ciphertext envelopes, TEE as plaintext.
    for mid, oa in (("A", "2026-06-01"), ("B", "2026-06-02"), ("C", "2026-06-03")):
        env = {**_ct_env(uid, mid, body_ct=f"OLD{mid}"), "occurred_at": oa}
        db.memory_upsert(uid, mid, oa, env)
        _tee_exec("INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
                  "VALUES (%s,%s,%s,%s)", (uid, mid, oa, Jsonb({"body": f"PT:OLD{mid}"})))

    # Replace with 2: C removed, B edited (new ciphertext), A unchanged survivor.
    a_env = {**_ct_env(uid, "A", body_ct="OLDA"), "occurred_at": "2026-06-01"}
    b_env = {**_ct_env(uid, "B", body_ct="EDITB"), "occurred_at": "2026-06-02"}
    db.memory_replace_all(uid, [a_env, b_env])

    # Removed row deleted from TEE; survivors A+B both requeued.
    assert _tee("SELECT count(*) FROM memory_moments WHERE user_id=%s AND moment_id='C'",
                (uid,))[0][0] == 0
    reqs = _tee("SELECT item_id FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='memory_moments' AND reason LIKE 'requeue%%' "
                "ORDER BY item_id", (uid,))
    assert [r[0] for r in reqs] == ["A", "B"]

    # Advance the cursor past every row → only requeue-consume converges the edit.
    _set_cursor("memory_moments", 0.0, "zzzzzzzz")
    worker.run_table("memory_moments", qps=1000)

    bodies = dict(_tee("SELECT moment_id, doc->>'body' FROM memory_moments "
                       "WHERE user_id=%s ORDER BY moment_id", (uid,)))
    assert bodies == {"A": "PT:OLDA", "B": "PT:EDITB"}  # B converged to the edit
    assert _tee("SELECT count(*) FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='memory_moments'", (uid,))[0][0] == 0


# --------------------------------------------------------------------------- #
# Round 3 Fix 1 (P1) — PendingDeviceMigration transitioning to a terminal
# reason must delete the stale TEE plaintext row it left behind. A row that
# was already replicated to TEE plaintext, then rewritten in RDS to
# local_only/no-K_enclave (same PK — the requeue lane's whole reason for
# being), previously left its OLD plaintext sitting in TEE forever: the
# terminal ``pdm:`` marker was written, but nobody deleted the content row it
# superseded. That's a privacy leak (stale plaintext of content the user just
# made private/undecryptable) and it broke verify's rds == tee + pending
# balance (both sides counted the row: tee=1 AND pending=1 against rds=1).
# --------------------------------------------------------------------------- #

def test_requeue_pdm_deletes_stale_tee_identity_row_and_marks_terminal():
    uid = _uid()
    _seed_both(uid)
    store = core_store.get_store(uid)

    # First save: normal ciphertext → requeue lane replicates it to TEE plaintext.
    identity_service._save_identity(store, _ct_env(uid, "identity", body_ct="V1"))
    _set_cursor("identity", 0.0, uid)  # cursor loop can't see this uid; requeue-only
    report1 = worker.run_table("identity", qps=1000)
    assert report1["copied"] == 1 and report1["pending"] == 0
    assert _tee("SELECT doc->>'body' FROM user_blobs WHERE user_id=%s AND kind='identity'",
                (uid,)) == [("PT:V1",)]

    # The device rewrites the SAME identity row as local_only/no-K_enclave —
    # _save_identity requeues again (C1's unconditional mark_pending).
    identity_service._save_identity(
        store, _ct_env(uid, "identity", body_ct="V2", visibility="local_only", k_enclave=None))
    report2 = worker.run_table("identity", qps=1000)

    # Stale TEE plaintext must be GONE; a terminal pdm: marker remains; RDS
    # row is untouched (local_only content still lives in RDS, just not TEE).
    assert _tee("SELECT count(*) FROM user_blobs WHERE user_id=%s AND kind='identity'",
                (uid,))[0][0] == 0
    pend = _tee("SELECT reason FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='identity' AND item_id='identity'", (uid,))
    assert len(pend) == 1 and pend[0][0].startswith("pdm:")
    assert report2["copied"] == 0 and report2["pending"] == 1
    # Balance: rds=1, tee=0, terminal pending=1 → rds == tee + pending.
    assert db.get_blob(uid, "identity") is not None


def test_requeue_pdm_deletes_stale_tee_chat_row_and_marks_terminal():
    uid = _uid()
    _seed_both(uid)
    ts = 5_000_000.0  # far past any other test's chat_messages rows in this table

    # A prior replicator pass already left plaintext in TEE for this row.
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,'c1',%s,%s)",
                  (uid, ts, Jsonb(_ct_env(uid, "c1", body_ct="V1"))))
    _tee_exec("INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,'c1',%s,%s)",
              (uid, ts, Jsonb({"body": "PT:V1"})))

    # Row rewritten in place to local_only/no-K_enclave (same PK) and requeued
    # — exercising the worker's generic requeue-then-PDM mechanism directly
    # (identity's C1 write is the one product path that hits this today; this
    # proves the same defensive delete holds for any table whose requeue lane
    # is populated, per the worker's per-table requeue_delete_tee_sql wiring).
    with db.get_pool().connection() as c:
        c.execute("UPDATE chat_messages SET doc = %s WHERE user_id = %s AND msg_id = 'c1'",
                  (Jsonb(_ct_env(uid, "c1", body_ct="V2", visibility="local_only", k_enclave=None)), uid))
    from tee_shadow import mirror
    mirror.mark_pending(uid, "chat_messages", "c1", "requeue")
    _set_cursor("chat_messages", ts, "c1")  # cursor loop can't revisit this row; requeue-only

    report = worker.run_table("chat_messages", qps=1000)

    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s AND msg_id='c1'",
                (uid,))[0][0] == 0
    pend = _tee("SELECT reason FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='chat_messages' AND item_id='c1'", (uid,))
    assert len(pend) == 1 and pend[0][0].startswith("pdm:")
    assert report["copied"] == 0 and report["pending"] == 1
    # Balance: rds=1, tee=0, terminal pending=1.
    assert _rds("SELECT count(*) FROM chat_messages WHERE user_id=%s AND msg_id='c1'",
                (uid,))[0][0] == 1


# --------------------------------------------------------------------------- #
# Round 3 Fix 2 (P2) — memory_upsert / world_book_upsert must requeue their
# in-place rewrite, same as memory_replace_all's survivors. Before this fix,
# a single-row edit through these two functions left NO pending marker at
# all: the append-only cursor never revisits a same-PK row, so an edited
# moment/entry that had already been replicated to TEE stayed stuck showing
# the OLD plaintext forever, with no mechanism to ever re-derive it.
# --------------------------------------------------------------------------- #

def test_memory_upsert_requeues_inplace_rewrite_and_worker_converges():
    uid = _uid()
    _seed_both(uid)
    oa = "2026-06-01"
    # A prior pass already replicated the old plaintext into TEE.
    _tee_exec("INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
              "VALUES (%s,'m1',%s,%s)", (uid, oa, Jsonb({"body": "PT:OLD"})))

    ok = db.memory_upsert(uid, "m1", oa, {**_ct_env(uid, "m1", body_ct="NEW"), "occurred_at": oa})
    assert ok is True

    assert _tee("SELECT reason FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='memory_moments' AND item_id='m1'",
                (uid,)) == [("requeue",)]

    _set_cursor("memory_moments", 0.0, "zzzzzzzz")  # forward scan can't revisit this PK
    worker.run_table("memory_moments", qps=1000)

    assert _tee("SELECT doc->>'body' FROM memory_moments WHERE user_id=%s AND moment_id='m1'",
                (uid,)) == [("PT:NEW",)]
    assert _tee("SELECT count(*) FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='memory_moments'", (uid,))[0][0] == 0


def test_world_book_upsert_requeues_inplace_rewrite_and_worker_converges():
    uid = _uid()
    _seed_both(uid)
    ua = "2026-06-01T00:00:00Z"
    # A prior pass already replicated the old plaintext into TEE.
    _tee_exec("INSERT INTO world_book_entries (user_id, entry_id, updated_at, doc) "
              "VALUES (%s,'e1',%s,%s)", (uid, ua, Jsonb({"body": "PT:OLD"})))

    ok = db.world_book_upsert(uid, "e1", ua, {**_ct_env(uid, "e1", body_ct="NEW"), "updated_at": ua})
    assert ok is True

    assert _tee("SELECT reason FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='world_book_entries' AND item_id='e1'",
                (uid,)) == [("requeue",)]

    _set_cursor("world_book_entries", 0.0, "zzzzzzzz")  # forward scan can't revisit this PK
    worker.run_table("world_book_entries", qps=1000)

    assert _tee("SELECT doc->>'body' FROM world_book_entries WHERE user_id=%s AND entry_id='e1'",
                (uid,)) == [("PT:NEW",)]
    assert _tee("SELECT count(*) FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='world_book_entries'", (uid,))[0][0] == 0
