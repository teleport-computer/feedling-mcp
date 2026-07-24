import uuid

import db

# 迁移后加了 FK 的、以及 genesis 子表：删 users 行必须全部级联清空
_PER_USER_TABLES = [
    "chat_messages", "frame_envelopes", "memory_moments", "perception_daily",
    "perception_items", "user_blobs", "user_logs",
    "genesis_import_jobs", "genesis_import_chunks", "genesis_import_outputs",
]


def _seed_min_rows(conn, uid):
    conn.execute("INSERT INTO users (user_id, doc) VALUES (%s, '{}') "
                 "ON CONFLICT (user_id) DO NOTHING", (uid,))
    conn.execute("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s,'tokens','{}')", (uid,))
    conn.execute(
        "INSERT INTO user_logs (user_id, stream, doc) VALUES (%s, 'perception_events', '{}')",
        (uid,),
    )
    conn.execute("INSERT INTO memory_moments (user_id, moment_id, doc) VALUES (%s, 'm1', '{}')", (uid,))
    conn.execute(
        "INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s, 'msg1', 0, '{}')",
        (uid,),
    )
    conn.execute(
        "INSERT INTO frame_envelopes (user_id, frame_id, ts, doc) VALUES (%s, 'f1', 0, '{}')",
        (uid,),
    )
    conn.execute(
        "INSERT INTO perception_daily (user_id, date, signal, doc, updated_at) "
        "VALUES (%s, '2026-07-01', 'steps', '{}', 0)",
        (uid,),
    )
    conn.execute(
        "INSERT INTO perception_items (user_id, kind, item_id, ts, doc) "
        "VALUES (%s, 'photo', 'p1', 0, '{}')",
        (uid,),
    )
    conn.execute(
        "INSERT INTO genesis_import_jobs (user_id, job_id, status) VALUES (%s, 'job1', 'pending')",
        (uid,),
    )
    conn.execute(
        "INSERT INTO genesis_import_chunks "
        "(user_id, job_id, seq, ciphertext_sha256, encrypted_body) "
        "VALUES (%s, 'job1', 0, 'deadbeef', '\\x00')",
        (uid,),
    )
    conn.execute(
        "INSERT INTO genesis_import_outputs (user_id, job_id, output_type) "
        "VALUES (%s, 'job1', 'summary')",
        (uid,),
    )


def test_delete_user_cascades_per_user_rows():
    uid = f"usr_cascade_test_{uuid.uuid4().hex[:12]}"
    with db.get_pool().connection() as conn:
        _seed_min_rows(conn, uid)
    db.delete_user(uid)
    with db.get_pool().connection() as conn:
        for t in _PER_USER_TABLES:
            n = conn.execute(f"SELECT count(*) FROM {t} WHERE user_id = %s", (uid,)).fetchone()[0]
            assert n == 0, f"{t} still has {n} rows for deleted user"


def _full_registry_snapshot(conn) -> list[dict]:
    """Reconstruct the whole users list as save_all_users' callers pass it (the
    in-memory registry snapshot). user_id is forced from the column so an entry is
    never skipped. Isolation-safe: passing the FULL set means no sibling test's
    user is treated as 'removed'."""
    rows = conn.execute("SELECT user_id, doc FROM users").fetchall()
    return [{**(r[1] or {}), "user_id": r[0]} for r in rows]


def test_save_all_users_preserves_kept_users_per_user_data():
    """Regression (Codex P1): under the 0011 CASCADE FKs, save_all_users must NOT
    do DELETE-all + reinsert — that cascade-wipes every KEPT user's per-user data
    (the reinsert brings back the users row but not the cascaded child rows). A
    user still present in the snapshot must keep its chat/memory/blobs/etc."""
    uid = f"usr_saveall_keep_{uuid.uuid4().hex[:12]}"
    with db.get_pool().connection() as conn:
        _seed_min_rows(conn, uid)
        snapshot = _full_registry_snapshot(conn)  # full registry, uid included
    db.save_all_users(snapshot)
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM users WHERE user_id = %s", (uid,)
        ).fetchone()[0] == 1, "kept user row missing after save_all_users"
        for t in _PER_USER_TABLES:
            n = conn.execute(f"SELECT count(*) FROM {t} WHERE user_id = %s", (uid,)).fetchone()[0]
            assert n >= 1, f"{t} for a KEPT user was cascade-wiped by save_all_users"


def test_save_all_users_reflects_genuine_removals():
    """A user in DB but absent from the snapshot is genuinely removed (its
    per-user data cascade-deleted). save_all_users must still reflect removals."""
    gone = f"usr_saveall_gone_{uuid.uuid4().hex[:12]}"
    with db.get_pool().connection() as conn:
        _seed_min_rows(conn, gone)
        # snapshot = the whole registry EXCEPT `gone`
        snapshot = [e for e in _full_registry_snapshot(conn) if e["user_id"] != gone]
    db.save_all_users(snapshot)
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM users WHERE user_id = %s", (gone,)
        ).fetchone()[0] == 0, "removed user still present after save_all_users"
        assert conn.execute(
            "SELECT count(*) FROM user_blobs WHERE user_id = %s", (gone,)
        ).fetchone()[0] == 0, "removed user's per-user data should be cascade-deleted"
