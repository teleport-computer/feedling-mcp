"""tee_shadow.verify — RDS↔TEE consistency verification job (Task 7).

The job scans ALL rows in scope (no per-user filter — that's the whole point:
it's the "stop-RDS" gate report), which makes it fundamentally different from
every other tee_* test in this suite (those scope assertions to one freshly
generated ``usr_*`` id and tolerate whatever other tests left behind in the
shared session DB). ``verify.run()`` has no such per-user knob, so every test
here truncates the full set of tables verify.py looks at (both DBs) via the
autouse ``_clean`` fixture, then seeds exactly the rows each test cares about.
That keeps the assertions on ``report["ok"]`` deterministic regardless of
what ran before/after in the shared session DB.

Note: ``genesis_import_jobs`` is in the truncate list (via ``reconciler.
TABLES``) but ``genesis_import_chunks`` is not — it doesn't need to be:
``TRUNCATE genesis_import_jobs ... CASCADE`` implicitly clears
``genesis_import_chunks`` too via its FK to ``genesis_import_jobs``, so it's
part of the effective cleanup set even though it never appears in
``_RDS_TABLES``/verify's scope.
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
from tee_replicator import transforms  # noqa: E402
from tee_shadow import mirror, reconciler, verify  # noqa: E402
from tee_shadow import table_registry as reg  # noqa: E402
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
    monkeypatch.setattr(verify, "_make_decrypt", _make_stub)
    yield


# Full set of tables verify.py reads, per DB side (see module docstring: a
# whole-DB job needs a whole-DB clean slate for deterministic assertions).
#
# Derived from verify.covered_tables() (rather than hand-listed) so this list
# can't silently drift out of sync with verify's actual scope again — Task 8
# widened that scope from 18 to 52 tables (7 new CIPHERTEXT + 27 SNAPSHOT),
# and a stale hand-list here would leave leftover rows in the newly-covered
# tables from earlier tests in the shared session DB, permanently flipping
# report["ok"] to False for the rest of the run regardless of what a given
# test actually seeded. "identity" is a pseudo key (see
# table_registry.PSEUDO_CIPHERTEXT_TABLES) — its physical table (user_blobs)
# is already covered via reconciler.TABLES.
_TEE_ALIAS = {cfg["rds_table"]: cfg["tee_table"] for cfg in verify._CIPHERTEXT_TABLES.values()}
_RDS_TABLES = sorted(t for t in verify.covered_tables()
                      if t not in reg.PSEUDO_CIPHERTEXT_TABLES)
_TEE_TABLES = [_TEE_ALIAS.get(t, t) for t in _RDS_TABLES] + ["tee_pending_device_migration"]


@pytest.fixture(autouse=True)
def _clean(backend_env):
    # v2_runtime_control is a migration-seeded SINGLETON row (0030_v2_runtime_
    # control.py: `INSERT ... VALUES (1) ON CONFLICT DO NOTHING`, never
    # reinserted by app code) that many V2 tests (test_v2_kill_switch*.py,
    # test_v2_capture_batch_protocol.py, ...) mutate with UPDATE-only
    # statements assuming `id=1` always exists. It's the one SNAPSHOT-lane
    # table with that property among the newly-covered set (checked: the
    # other 26 SNAPSHOT tables + 7 new CIPHERTEXT tables have no unconditional
    # migration seed — their only INSERTs are backfills FROM other tables,
    # which are empty on a fresh test DB). Truncating it here without
    # reseeding on BOTH sides would permanently delete that row for the rest
    # of the shared session DB (breaking every later-alphabetical V2 test
    # file) and/or leave RDS/TEE row counts mismatched (rds=1,tee=0) forever,
    # tripping every subsequent report["ok"] assertion in this file — same
    # class of problem `copytext_meta`'s reseed below already solves.
    with db.get_pool().connection() as c:
        c.execute("TRUNCATE " + ", ".join(_RDS_TABLES) + " CASCADE")
        c.execute("INSERT INTO copytext_meta (id, revision) VALUES (TRUE, 0) "
                  "ON CONFLICT (id) DO NOTHING")
        c.execute("INSERT INTO v2_runtime_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    with mirror.get_tee_pool().connection() as c:
        c.execute("TRUNCATE " + ", ".join(_TEE_TABLES) + " CASCADE")
        c.execute("INSERT INTO copytext_meta (id, revision) VALUES (TRUE, 0) "
                  "ON CONFLICT (id) DO NOTHING")
        c.execute("INSERT INTO v2_runtime_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    yield


def _seed(uid):
    """Seed both sides with the SAME users.doc content — db.upsert_user stores
    the whole entry dict verbatim as the RDS ``doc`` column (see
    tests/test_tee_reconciler.py's own note on this), so the TEE side must
    mirror that same dict, not an empty placeholder, or the plaintext-table
    sampling stage of verify.run() (which scans the real ``users`` row content,
    unlike the other tee_* tests that never assert on it) reports a spurious
    "doc" field mismatch."""
    entry = {"user_id": uid, "api_key_hash": "h", "doc": {}}
    seed_user(uid, **{k: v for k, v in entry.items() if k != "user_id"})
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO users (user_id, doc) VALUES (%s, %s) "
                  "ON CONFLICT (user_id) DO NOTHING", (uid, Jsonb(entry)))


def _chat_doc(uid, msg_id, body_ct, visibility="shared", k_enclave="ke"):
    doc = {"id": msg_id, "role": "user", "ts": None, "source": "chat",
           "content_type": "text", "v": 1, "body_ct": body_ct, "nonce": "n",
           "K_user": "k", "enclave_pk_fpr": "f", "visibility": visibility,
           "owner_user_id": uid}
    if k_enclave is not None:
        doc["K_enclave"] = k_enclave
    return doc


def _insert_rds_chat_doc(uid, msg_id, ts, doc):
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,%s,%s,%s)",
                  (uid, msg_id, ts, Jsonb(doc)))


def _insert_rds_chat(uid, msg_id, ts, body_ct, **kw):
    doc = _chat_doc(uid, msg_id, body_ct, **kw)
    doc["ts"] = ts
    _insert_rds_chat_doc(uid, msg_id, ts, doc)
    return doc


def _chat_doc_with_subenvelopes(uid, msg_id, body_ct):
    """Same shape as ``_chat_doc`` but with FULL ``thinking_*``/``caption_*``
    sub-envelope fields present, so ``transforms.plaintext_chat_doc``'s
    sub-envelope branches actually fire (see transforms.py's ``_SUB_PREFIXES``
    / ``_sub_envelope``: it requires ``{prefix}body_ct`` plus whatever other
    ``{prefix}*`` fields are present, decryptable via ``{prefix}K_enclave``)."""
    doc = _chat_doc(uid, msg_id, body_ct)
    for prefix, body_ct_val in (("thinking_", "THINK"), ("caption_", "CAP")):
        kind = prefix.rstrip("_")
        doc[f"{prefix}body_ct"] = body_ct_val
        doc[f"{prefix}nonce"] = "n"
        doc[f"{prefix}K_user"] = "k"
        doc[f"{prefix}K_enclave"] = "ke"
        doc[f"{prefix}kind"] = kind
        doc[f"{prefix}source"] = "assistant"
        doc[f"{prefix}model"] = "m"
        doc[f"{prefix}native"] = True
        doc[f"{prefix}id"] = f"{msg_id}:{kind}"
        doc[f"{prefix}visibility"] = "shared"
        doc[f"{prefix}owner_user_id"] = uid
    return doc


def _insert_tee_chat(uid, msg_id, ts, doc):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,%s,%s,%s)",
                  (uid, msg_id, ts, Jsonb(doc)))


def _seed_consistent_pair(uid):
    """Seed one row in every one of the five ciphertext shapes, consistent on
    both sides (TEE computed via the SAME transforms functions production
    uses), plus rely on the ``users`` row (already plaintext-table coverage)."""
    # chat
    rds_doc = _insert_rds_chat(uid, "m1", 10.0, "AAA")
    tee_doc = transforms.plaintext_chat_doc(rds_doc, _stub_decrypt)
    _insert_tee_chat(uid, "m1", 10.0, tee_doc)

    # memory
    mem_rds = {"id": "mo1", "occurred_at": "2026-01-01", "body_ct": "MMM",
               "nonce": "n", "K_user": "k", "K_enclave": "ke",
               "visibility": "shared", "owner_user_id": uid, "v": 1}
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
                  "VALUES (%s,%s,%s,%s)", (uid, "mo1", "2026-01-01", Jsonb(mem_rds)))
    mem_tee = transforms.plaintext_memory_doc(mem_rds, _stub_decrypt)
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
                  "VALUES (%s,%s,%s,%s)", (uid, "mo1", "2026-01-01", Jsonb(mem_tee)))

    # world_book
    wb_rds = {"id": "wb1", "updated_at": "2026-01-01", "body_ct": "WWW",
              "nonce": "n", "K_user": "k", "K_enclave": "ke",
              "visibility": "shared", "owner_user_id": uid, "v": 1}
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO world_book_entries (user_id, entry_id, updated_at, doc) "
                  "VALUES (%s,%s,%s,%s)", (uid, "wb1", "2026-01-01", Jsonb(wb_rds)))
    wb_tee = transforms.plaintext_world_book_doc(wb_rds, _stub_decrypt)
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO world_book_entries (user_id, entry_id, updated_at, doc) "
                  "VALUES (%s,%s,%s,%s)", (uid, "wb1", "2026-01-01", Jsonb(wb_tee)))

    # identity (user_blobs kind='identity')
    id_rds = {"id": "identity", "body_ct": "III", "nonce": "n", "K_user": "k",
              "K_enclave": "ke", "visibility": "shared", "owner_user_id": uid, "v": 1}
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s,'identity',%s)",
                  (uid, Jsonb(id_rds)))
    id_tee = transforms.plaintext_identity_doc(id_rds, _stub_decrypt)
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s,'identity',%s)",
                  (uid, Jsonb(id_tee)))

    # frames: RDS frame_envelopes row + a consistent TEE frames pointer row
    # (verify's frames check is existence-only: meta/pointer fields present +
    # sha256 non-empty — no decrypt, no R2, per the brief).
    frame_rds = {"id": "f1", "body_ct": "FFF", "nonce": "n", "K_user": "k",
                 "K_enclave": "ke", "visibility": "shared", "owner_user_id": uid, "v": 1}
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO frame_envelopes (user_id, frame_id, ts, doc) VALUES (%s,%s,%s,%s)",
                  (uid, "f1", 10.0, Jsonb(frame_rds)))
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute(
            "INSERT INTO frames (user_id, frame_id, ts, meta, body_storage_key, "
            "body_storage_key_version, body_mime, body_sha256, body_size_bytes) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (uid, "f1", 10.0, Jsonb({"visibility": "shared"}), "frames-tee/u/f1",
             "v1", "image/png", "deadbeef", 123))


def test_consistent_dbs_report_ok_with_zero_mismatches(backend_env):
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _seed_consistent_pair(uid)

    report = verify.run(sample_rate=1.0)

    assert report["mismatches"] == []
    assert report["ok"] is True
    # sanity: the equation is non-trivial (not just "nothing to check")
    assert report["tables"]["chat_messages"]["rds_rows"] == 1
    assert report["tables"]["chat_messages"]["tee_rows"] == 1


def test_mutated_tee_row_is_pinpointed_as_mismatch(backend_env):
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _seed_consistent_pair(uid)
    # Corrupt the TEE side's decrypted body for the chat row only.
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("UPDATE chat_messages SET doc = jsonb_set(doc, '{body}', '\"TAMPERED\"') "
                  "WHERE user_id=%s AND msg_id='m1'", (uid,))

    report = verify.run(sample_rate=1.0)

    assert report["ok"] is False
    hits = [m for m in report["mismatches"]
            if m["table"] == "chat_messages" and m["user_id"] == uid
            and m["item_id"] == "m1" and m["field"] == "body"]
    assert len(hits) == 1, report["mismatches"]


def test_row_count_gap_reported_and_not_ok(backend_env):
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    # Extra RDS-only user: no TEE counterpart at all.
    db.upsert_user({"user_id": "usr_orphan_rds", "api_key_hash": "h", "doc": {}})

    report = verify.run(sample_rate=1.0)

    assert report["ok"] is False
    users_report = report["tables"]["users"]
    assert users_report["rows_ok"] is False
    assert users_report["rds_rows"] - users_report["tee_rows"] == 1
    assert "usr_orphan_rds" in users_report["user_diffs"]
    diff = users_report["user_diffs"]["usr_orphan_rds"]
    assert diff["rds"] == 1 and diff["tee"] == 0 and diff["pending"] == 0


def test_pending_rows_count_toward_the_reconciliation_equation(backend_env):
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    # RDS-only local_only chat row, with no TEE counterpart — but marked
    # pending, as tee_replicator.worker would after classifying it. Reason
    # here is TERMINAL ("local_only", not "requeue%"-prefixed — see
    # verify._split_pending's口径), so it DOES count toward rds==tee+pending.
    _insert_rds_chat(uid, "loc", 10.0, "XXX", visibility="local_only", k_enclave=None)
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute(
            "INSERT INTO tee_pending_device_migration (user_id, table_name, item_id, reason) "
            "VALUES (%s,'chat_messages','loc','local_only')", (uid,))

    report = verify.run(sample_rate=1.0)

    chat_report = report["tables"]["chat_messages"]
    assert chat_report["rds_rows"] == 1
    assert chat_report["tee_rows"] == 0
    assert chat_report["pending_rows"] == 1
    assert chat_report["rds_rows"] == chat_report["tee_rows"] + chat_report["pending_rows"]
    assert chat_report["rows_ok"] is True
    assert chat_report["requeue_backlog"] == 0
    assert report["ok"] is True
    assert report["mismatches"] == []


def test_requeue_pending_row_excluded_from_equation_but_counted_as_backlog(backend_env):
    """Round-2 fix: a requeue-lane row (reason LIKE 'requeue%') marks a same-PK
    in-place rewrite whose OLD TEE row is typically still present — unlike a
    terminal pending row, it must NOT be counted in the rds==tee+pending
    equation (that would double-count the row and falsely report it missing).
    Seed content CONSISTENT on both sides (same pattern as
    ``_seed_consistent_pair``) so the outcome doesn't depend on whether the
    sampling stage happens to pick this row — the assertion holds either way."""
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    rds_doc = _insert_rds_chat(uid, "m1", 10.0, "AAA")
    tee_doc = transforms.plaintext_chat_doc(rds_doc, _stub_decrypt)
    _insert_tee_chat(uid, "m1", 10.0, tee_doc)
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute(
            "INSERT INTO tee_pending_device_migration (user_id, table_name, item_id, reason) "
            "VALUES (%s,'chat_messages','m1','requeue_visibility_shared')", (uid,))

    report = verify.run(sample_rate=1.0)

    chat_report = report["tables"]["chat_messages"]
    assert chat_report["rds_rows"] == 1
    assert chat_report["tee_rows"] == 1
    assert chat_report["pending_rows"] == 0          # requeue excluded from the equation
    assert chat_report["rows_ok"] is True
    assert chat_report["requeue_backlog"] == 1        # ...but still surfaced, informationally
    assert report["ok"] is True
    assert report["mismatches"] == []


def test_verify_is_read_only(backend_env):
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _seed_consistent_pair(uid)

    def _snapshot():
        with db.get_pool().connection() as c:
            rds = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in _RDS_TABLES}
        with mirror.get_tee_pool().connection() as c:
            tee = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in _TEE_TABLES}
        cursors = _tee("SELECT count(*) FROM tee_replication_cursors")[0][0]
        return rds, tee, cursors

    before = _snapshot()
    verify.run(sample_rate=1.0)
    after = _snapshot()

    assert before == after


def test_thinking_body_mismatch_pinpointed_caption_not_reported(backend_env):
    """RDS chat row carries FULL thinking_*/caption_* sub-envelopes. TEE's
    thinking.body is deliberately wrong; caption is correct. Expect exactly
    one mismatch, located at the nested field path ``thinking.body`` — not a
    generic <row-missing>/top-level hit, and no entry at all for caption."""
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    rds_doc = _chat_doc_with_subenvelopes(uid, "m2", "AAA")
    rds_doc["ts"] = 10.0
    _insert_rds_chat_doc(uid, "m2", 10.0, rds_doc)

    tee_doc = transforms.plaintext_chat_doc(rds_doc, _stub_decrypt)
    tee_doc["thinking"]["body"] = "WRONG"  # caption left untouched/correct
    _insert_tee_chat(uid, "m2", 10.0, tee_doc)

    report = verify.run(sample_rate=1.0)

    hits = [m for m in report["mismatches"]
            if m["table"] == "chat_messages" and m["item_id"] == "m2"]
    assert hits == [{"table": "chat_messages", "user_id": uid,
                      "item_id": "m2", "field": "thinking.body"}]


def test_pending_race_window_local_only_row_silently_skipped_in_sample(backend_env):
    """Race-window branch: RDS has a local_only chat row with no K_enclave
    (enclave can't decrypt it), but tee_replicator.worker hasn't gotten
    around to writing the tee_pending_device_migration row yet, and TEE has
    no plaintext row either. transforms.plaintext_chat_doc raises
    PendingDeviceMigration for it; verify.py's sampled-compare loop catches
    that and does ``continue`` (see verify.py's comment at that except
    clause) — it must NOT surface as a mismatch, because a row that's about
    to be pended isn't a genuine content divergence."""
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_rds_chat(uid, "loc2", 10.0, "XXX", visibility="local_only", k_enclave=None)
    # Deliberately NOT inserting a tee_pending_device_migration row — that's
    # the race window: worker classifies-and-pends asynchronously, so there's
    # a window where the RDS row is already undecryptable but not yet pended.

    report = verify.run(sample_rate=1.0)

    # PIN (expected race-window semantics, not a bug): the row-count side has
    # no way to distinguish "genuine gap" from "about to be pended" either —
    # rds(1) != tee(0) + pending(0), so this table's rows_ok (and therefore
    # the whole report's ok) is correctly False. That's the counts side doing
    # its job; the assertion below is about the *sampled content* side, which
    # must stay silent for this row rather than double-reporting it.
    chat_report = report["tables"]["chat_messages"]
    assert chat_report["rds_rows"] == 1
    assert chat_report["tee_rows"] == 0
    assert chat_report["pending_rows"] == 0
    assert chat_report["rows_ok"] is False
    assert report["ok"] is False

    assert [m for m in report["mismatches"] if m["item_id"] == "loc2"] == []


def test_verify_covers_every_synced_table():
    """verify 范围必须随注册表扩展——否则新增表会产生'全绿假象'：
    verify_ok=true 但那些表压根没被核对过。这是上游 plan 的 Phase 1 出口 gate。"""
    from tee_shadow import table_registry as reg
    from tee_shadow import verify

    covered = set(verify.covered_tables())
    missing = sorted(set(reg.synced_tables()) - covered)
    assert not missing, f"这些表进了 TEE 但 verify 不核对它们：{missing}"
