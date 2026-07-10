import os
import uuid

import psycopg

import db
from tee_shadow import reconciler


def _tee(sql, params=()):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        return c.execute(sql, params).fetchall()


def test_backfill_copies_preexisting_rows(backend_env):
    db.upsert_user({"user_id": "usr_rc1", "api_key_hash": "h", "doc": {"a": 1}})  # 双写关着：只在 RDS
    report = reconciler.reconcile_table("users")
    assert report["copied"] >= 1
    # upsert_user stores the whole passed-in entry dict verbatim as the `doc`
    # JSONB column (see db.upsert_user), so the top-level field to check is
    # api_key_hash, not a nested "a" (the brief's literal ->>'a' path doesn't
    # exist at the top level of the stored doc — corrected here).
    assert _tee("SELECT doc->>'api_key_hash' FROM users WHERE user_id='usr_rc1'")[0][0] == "h"


def test_prune_removes_tee_orphans(backend_env):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO users (user_id, doc) VALUES ('usr_ghost','{}'::jsonb)")
    report = reconciler.reconcile_table("users")
    assert report["pruned"] >= 1
    assert _tee("SELECT count(*) FROM users WHERE user_id='usr_ghost'")[0][0] == 0


def test_user_blobs_identity_rows_owned_by_replicator(backend_env):
    """kind='identity' 归 tee_replicator 明文化：RDS 里是密文信封、TEE 里是明文，
    reconciler 绝不能把密文盖到明文上，也绝不能把 TEE 侧的明文行 prune 掉。
    其余 kind（如 model_api 的 provider-key 信封）照旧原样镜像（credential
    有意保持加密）。"""
    db.upsert_user({"user_id": "usr_idb", "api_key_hash": "h"})
    with db.get_pool().connection() as c:
        c.execute(
            "INSERT INTO user_blobs (user_id, kind, doc) VALUES "
            "('usr_idb', 'identity', '{\"envelope\": \"ciphertext\"}'::jsonb), "
            "('usr_idb', 'model_api', '{\"provider\": \"enc-envelope\"}'::jsonb)"
        )
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute(
            "INSERT INTO users (user_id, doc) VALUES ('usr_idb', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING"
        )
        c.execute(
            "INSERT INTO user_blobs (user_id, kind, doc) VALUES "
            "('usr_idb', 'identity', '{\"persona\": \"plaintext\"}'::jsonb)"
        )

    report = reconciler.reconcile_table("user_blobs")

    # TEE 的明文 identity 行：内容原封不动，且没有被 prune。
    rows = _tee("SELECT doc FROM user_blobs WHERE user_id='usr_idb' AND kind='identity'")
    assert rows == [({"persona": "plaintext"},)]
    # model_api 信封照旧原样镜像。
    rows = _tee("SELECT doc FROM user_blobs WHERE user_id='usr_idb' AND kind='model_api'")
    assert rows == [({"provider": "enc-envelope"},)]
    # 报告只统计 reconciler 辖区内（排除 identity）的行，两侧应收敛一致。
    assert report["rds_rows"] == report["tee_rows"]
    assert report["copied"] >= 1
    assert report["pruned"] == 0


def test_identity_sequence_setval_after_backfill(backend_env):
    """user_logs.seq is GENERATED ALWAYS AS IDENTITY. After copying rows with a
    high RDS seq, the TEE sequence must be setval'd past the max copied seq —
    otherwise a plain INSERT on the TEE side reuses a low seq and collides on PK."""
    uid = "usr_seq1"
    db.upsert_user({"user_id": uid, "api_key_hash": "h", "doc": {}})
    # Drive the RDS identity column to a high value: insert 5 rows, keep the last.
    with db.get_pool().connection() as c:
        for i in range(5):
            c.execute("INSERT INTO user_logs (user_id, stream, ts, item_key, doc) "
                      "VALUES (%s, 'test', %s, %s, '{}'::jsonb)", (uid, float(i), f"k{i}"))
        max_rds_seq = c.execute(
            "SELECT MAX(seq) FROM user_logs WHERE user_id=%s", (uid,)).fetchone()[0]
    assert max_rds_seq >= 5

    reconciler.reconcile_table("users")   # FK parent first
    reconciler.reconcile_table("user_logs")

    # A plain INSERT on the TEE side must get a seq strictly greater than every
    # copied seq (no OVERRIDING) — proving the sequence was advanced.
    new_seq = _tee(
        "INSERT INTO user_logs (user_id, stream, ts, item_key, doc) "
        "VALUES (%s, 'test', 99.0, 'kN', '{}'::jsonb) RETURNING seq", (uid,))[0][0]
    assert new_seq > max_rds_seq


def test_backfill_copies_jsonb_array_doc(backend_env):
    """Round 4 Fix 1 (P1): a JSONB column's value can be a top-level JSON
    *array*, not just an object — e.g. backend/accounts/access.py's
    ``_save_access_link_tokens`` does
    ``db.set_global_blob("access_link_tokens", rows)`` where ``rows`` is a
    ``list[dict]``, so ``global_blobs.doc`` for the real ``access_link_tokens``
    key is a JSON array. psycopg reads such a jsonb column back as a plain
    Python ``list``; before the fix ``_wrap_jsonb`` only wrapped ``dict`` in
    ``Jsonb()``, so psycopg would instead try to adapt the bare list as a PG
    array parameter and the INSERT would fail — reconcile must not choke on
    this legitimate existing data shape.

    Uses a dedicated test-only key rather than the literal
    ``"access_link_tokens"``: this suite's ``backend_env`` fixture resets
    in-memory registry state per test but does NOT truncate Postgres tables
    between tests (see conftest.py), and other tests in the suite (e.g.
    test_access_modes.py) exercise the real ``/v1/access/link-token`` route,
    which writes an actual row under that key in the same session-scoped
    test DB — colliding with a hardcoded literal here."""
    array_key = f"test_jsonb_array_doc_{uuid.uuid4().hex[:8]}"
    with db.get_pool().connection() as c:
        c.execute(
            "INSERT INTO global_blobs (key, doc) VALUES (%s, %s::jsonb)",
            (array_key, '["tok1", "tok2"]'),
        )
    report = reconciler.reconcile_table("global_blobs")
    assert report["copied"] >= 1
    rows = _tee("SELECT doc FROM global_blobs WHERE key=%s", (array_key,))
    assert rows == [(["tok1", "tok2"],)]


def test_converges_after_simulated_mirror_outage(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    monkeypatch.setenv("TEE_DATABASE_URL", "postgresql://invalid:1/x")  # 制造双写失败
    import tee_shadow.mirror as m
    m._pool = None
    db.upsert_user({"user_id": "usr_rc3", "api_key_hash": "h", "doc": {}})
    monkeypatch.undo()
    m._pool = None  # 恢复
    report = reconciler.reconcile_table("users")
    assert _tee("SELECT count(*) FROM users WHERE user_id='usr_rc3'")[0][0] == 1
    assert report["rds_rows"] == report["tee_rows"]
