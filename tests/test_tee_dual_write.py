import os, psycopg
import db
from conftest import seed_user  # noqa: E402


def _tee(sql, params=()):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        return c.execute(sql, params).fetchall()


def test_upsert_user_dual_writes(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    db.upsert_user({"user_id": "usr_dw1", "api_key_hash": "h", "doc": {}})
    assert _tee("SELECT count(*) FROM users WHERE user_id='usr_dw1'")[0][0] == 1


def test_log_append_dual_writes(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    seed_user("usr_dw2")
    db.log_append("usr_dw2", "test", {"kind": "test", "ts": 1.0})
    assert _tee("SELECT count(*) FROM user_logs WHERE user_id='usr_dw2'")[0][0] == 1


def test_log_append_dual_write_seq_matches_primary(backend_env, monkeypatch):
    # Fix 1 (P1): the mirror must carry the PRIMARY-assigned seq (OVERRIDING
    # SYSTEM VALUE), not let TEE mint its own via its own IDENTITY sequence —
    # otherwise the two dbs' (user_id, stream, seq) rows diverge even though
    # row *content* looks fine, breaking seq-ordered reads/joins across dbs.
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    seed_user("usr_dw6")
    db.log_append("usr_dw6", "test", {"kind": "test", "n": 1}, ts=1.0, item_key="a")
    db.log_append("usr_dw6", "test", {"kind": "test", "n": 2}, ts=2.0, item_key="b")
    with db.get_pool().connection() as conn:
        primary_rows = set(conn.execute(
            "SELECT user_id, stream, seq FROM user_logs WHERE user_id='usr_dw6'").fetchall())
    tee_rows = set(_tee(
        "SELECT user_id, stream, seq FROM user_logs WHERE user_id='usr_dw6'"))
    assert len(primary_rows) == 2
    assert primary_rows == tee_rows


def test_chat_delete_mirrors_to_tee(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    seed_user("usr_dw3")
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO users (user_id, doc) VALUES ('usr_dw3','{}'::jsonb) ON CONFLICT DO NOTHING")
        c.execute("INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES ('usr_dw3','m1',1.0,'{}'::jsonb)")
        # A stale pending marker on this exact row (e.g. left over from a
        # visibility swap right before the delete) must not outlive it.
        c.execute("INSERT INTO tee_pending_device_migration (user_id, table_name, item_id, reason) "
                  "VALUES ('usr_dw3','chat_messages','m1','visibility_local_only')")
    db.chat_delete("usr_dw3", "m1")
    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id='usr_dw3'")[0][0] == 0
    assert _tee("SELECT count(*) FROM tee_pending_device_migration "
                "WHERE user_id='usr_dw3' AND table_name='chat_messages'")[0][0] == 0


def test_chat_append_trim_mirrors_pinned_eviction_and_clears_pending(backend_env, monkeypatch):
    """Round-2 Fix 1 (P1): chat_append's ring-buffer trim must propagate to the
    TEE mirror (pinned to the EXACT evicted msg_ids, same pattern as
    frame_prune_to), and any pending_device_migration marker those evicted rows
    carried must be cleared too — otherwise a trimmed-away row's marker
    outlives it and permanently unbalances verify's rds == tee + pending."""
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    uid = "usr_dw_trim1"
    seed_user(uid)
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO users (user_id, doc) VALUES (%s,'{}'::jsonb) "
                  "ON CONFLICT DO NOTHING", (uid,))
        # A prior replicator pass already left a TEE plaintext row for the
        # oldest message, plus a stale pending marker on it.
        c.execute("INSERT INTO chat_messages (user_id, msg_id, ts, doc) "
                  "VALUES (%s,'old',1.0,'{}'::jsonb)", (uid,))
        c.execute("INSERT INTO tee_pending_device_migration (user_id, table_name, item_id, reason) "
                  "VALUES (%s,'chat_messages','old','requeue_visibility_shared')", (uid,))

    db.chat_append(uid, "old", 1.0, {"id": "old"}, max_messages=0)  # no-op trim, seed RDS too
    db.chat_append(uid, "new1", 2.0, {"id": "new1"}, max_messages=2)
    db.chat_append(uid, "new2", 3.0, {"id": "new2"}, max_messages=2)

    # "old" evicted from RDS by the ring-buffer trim (max_messages=2 keeps
    # only new1/new2).
    with db.get_pool().connection() as conn:
        rds_ids = {r[0] for r in conn.execute(
            "SELECT msg_id FROM chat_messages WHERE user_id=%s", (uid,)).fetchall()}
    assert rds_ids == {"new1", "new2"}

    # Mirror delete pinned to the evicted id + its pending marker cleared.
    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s AND msg_id='old'",
                (uid,))[0][0] == 0
    assert _tee("SELECT count(*) FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name='chat_messages'", (uid,))[0][0] == 0


def test_delete_user_data_clears_pending_across_all_tables(backend_env, monkeypatch):
    """delete_user_data is a full-user wipe — no RDS row survives for this
    user, so no tee_pending_device_migration row should either, regardless of
    which table/reason it names."""
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    uid = "usr_dw_wipe1"
    seed_user(uid)
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO users (user_id, doc) VALUES (%s,'{}'::jsonb) "
                  "ON CONFLICT DO NOTHING", (uid,))
        c.execute("INSERT INTO tee_pending_device_migration (user_id, table_name, item_id, reason) "
                  "VALUES (%s,'chat_messages','m1','requeue'), "
                  "(%s,'memory_moments','mo1','visibility_local_only')", (uid, uid))

    db.delete_user_data(uid)

    assert _tee("SELECT count(*) FROM tee_pending_device_migration "
                "WHERE user_id=%s", (uid,))[0][0] == 0


def test_proactive_store_v2_log_patch_dual_writes(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    seed_user("usr_dw5")
    # log_append is itself mirrored, so the row lands in both DBs; each side's
    # patch SQL locates its own row via the item_key subquery (seq differs).
    db.log_append("usr_dw5", "proactive_wakes_v2", {"status": "pending", "n": 1},
                  ts=1.0, item_key="wk1")
    assert _tee("SELECT count(*) FROM user_logs WHERE user_id='usr_dw5'")[0][0] == 1

    from proactive.store_v2 import _patch_log_item_guarded
    merged = _patch_log_item_guarded(
        "usr_dw5", "proactive_wakes_v2", "wk1", {"status": "drained"},
        status="pending")
    assert merged is not None and merged["status"] == "drained"
    rows = _tee(
        "SELECT doc->>'status' FROM user_logs "
        "WHERE user_id='usr_dw5' AND item_key='wk1'")
    assert rows == [("drained",)]


def test_log_patch_item_only_mirrors_on_primary_hit(backend_env, monkeypatch):
    # Fix 2 (P2): guarded patches must only mirror when the primary's
    # only_if_status guard actually matched a row. A rejected/no-op attempt
    # must not be replayed against TEE (same convention as
    # chat_try_claim_reply / scheduled_wake claim_due).
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    seed_user("usr_dw7")
    db.log_append("usr_dw7", "test", {"status": "pending"}, ts=1.0, item_key="j1")

    from tee_shadow import mirror
    calls = []
    orig_execute = mirror.execute

    def spy(sql, params=()):
        calls.append(sql)
        return orig_execute(sql, params)

    monkeypatch.setattr(mirror, "execute", spy)

    # Guard mismatch on primary (actual status is "pending", not
    # "processing") -> rejected -> must NOT mirror -> TEE row unchanged.
    rejected = db.log_patch_item(
        "usr_dw7", "test", "j1", {"status": "done"}, only_if_status="processing")
    assert rejected is None
    assert calls == []
    assert _tee(
        "SELECT doc->>'status' FROM user_logs WHERE user_id='usr_dw7' AND item_key='j1'"
    ) == [("pending",)]

    # Guard matches on primary -> accepted -> mirrors -> TEE row updated.
    accepted = db.log_patch_item(
        "usr_dw7", "test", "j1", {"status": "done"}, only_if_status="pending")
    assert accepted is not None and accepted["status"] == "done"
    assert len(calls) == 1
    assert _tee(
        "SELECT doc->>'status' FROM user_logs WHERE user_id='usr_dw7' AND item_key='j1'"
    ) == [("done",)]


def test_proactive_store_v2_log_patch_guarded_rejected_not_mirrored(backend_env, monkeypatch):
    # Fix 2 (P2), sibling test to test_proactive_store_v2_log_patch_dual_writes
    # above: the rejected-guard branch of _patch_log_item_guarded must not
    # mirror at all (previously mirrored unconditionally).
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    seed_user("usr_dw8")
    db.log_append("usr_dw8", "proactive_wakes_v2", {"status": "pending", "n": 1},
                  ts=1.0, item_key="wk2")

    from tee_shadow import mirror
    calls = []
    orig_execute = mirror.execute

    def spy(sql, params=()):
        calls.append(sql)
        return orig_execute(sql, params)

    monkeypatch.setattr(mirror, "execute", spy)

    from proactive.store_v2 import _patch_log_item_guarded
    rejected = _patch_log_item_guarded(
        "usr_dw8", "proactive_wakes_v2", "wk2", {"status": "drained"},
        status="drained")  # wrong guard: actual status is "pending"
    assert rejected is None
    assert calls == []
    rows = _tee(
        "SELECT doc->>'status' FROM user_logs WHERE user_id='usr_dw8' AND item_key='wk2'")
    assert rows == [("pending",)]


def test_flag_off_is_bit_identical(backend_env, monkeypatch):
    monkeypatch.delenv("FEEDLING_TEE_DUAL_WRITE", raising=False)
    db.upsert_user({"user_id": "usr_dw4", "api_key_hash": "h", "doc": {}})
    assert _tee("SELECT count(*) FROM users WHERE user_id='usr_dw4'")[0][0] == 0


def test_set_config_if_absent_mirrors_rds_adopted_value_not_candidate(backend_env, monkeypatch):
    """Round 4 Fix 3 (P2): when RDS already holds the key (the primary INSERT
    is a no-op via ON CONFLICT DO NOTHING) but the TEE row is absent/stale
    (e.g. dual-write was off during the original bootstrap, or a concurrent
    racer's own candidate landed there first), the mirror must carry the
    value RDS actually ended up with — not this call's candidate. Otherwise a
    pepper-bootstrap race forks the shadow secret away from the primary."""
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    key = "test_pepper_dw_existing"
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO server_config (key, value) VALUES (%s, %s)", (key, b"va"))
    # TEE starts empty for this key (simulating the divergence window).
    assert _tee("SELECT count(*) FROM server_config WHERE key=%s", (key,))[0][0] == 0

    result = db.set_config_if_absent(key, b"vb")

    assert result == b"va"  # caller gets back what RDS actually has
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT value FROM server_config WHERE key=%s", (key,)).fetchone()
    assert bytes(row[0]) == b"va"  # RDS untouched by the losing candidate
    tee_row = _tee("SELECT value FROM server_config WHERE key=%s", (key,))
    assert bytes(tee_row[0][0]) == b"va"  # mirror gets RDS's value, not vb


def test_set_config_if_absent_new_key_dual_writes_candidate(backend_env, monkeypatch):
    """Sibling case: key does not exist anywhere yet — the candidate wins on
    both sides normally (unchanged primary-path semantics)."""
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    key = "test_pepper_dw_new"
    result = db.set_config_if_absent(key, b"candidate")
    assert result == b"candidate"
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT value FROM server_config WHERE key=%s", (key,)).fetchone()
    assert bytes(row[0]) == b"candidate"
    tee_row = _tee("SELECT value FROM server_config WHERE key=%s", (key,))
    assert bytes(tee_row[0][0]) == b"candidate"
