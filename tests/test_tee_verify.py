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
import hashlib
import re
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import object_storage  # noqa: E402
from tee_replicator import transforms  # noqa: E402
from tee_shadow import mirror, verify  # noqa: E402
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

# Tables within verify's covered scope that an RDS migration seeds with an
# UNCONDITIONAL constant row at apply time (not a backfill FROM another
# table — those insert 0 rows into a fresh test DB and need no reseeding).
# `_clean` TRUNCATEs every one of `_RDS_TABLES`/`_TEE_TABLES` before each test
# in this file; without reseeding these specific tables on BOTH sides, that
# TRUNCATE permanently deletes the row for the rest of the shared session
# test DB (nothing else ever re-creates it) — exactly what happened with
# `v2_runtime_control` in fix-round-1 (many V2 test files' autouse fixtures
# do `UPDATE v2_runtime_control ... WHERE id=1`, assuming that row always
# exists; deleting it broke 54 unrelated tests elsewhere in the suite before
# this was caught by a full L1 run — see task-8-report.md).
#
# This dict is the single place that invariant lives; don't inline reseed
# SQL elsewhere in this file's fixtures. `test_migration_seeded_singletons_
# constant_is_complete` below statically cross-checks it against every RDS
# migration, so a new such table added later fails loudly here instead of
# silently corrupting the shared session DB in some unrelated test file.
_MIGRATION_SEEDED_SINGLETONS: dict[str, str] = {
    "copytext_meta": (
        "INSERT INTO copytext_meta (id, revision) VALUES (TRUE, 0) "
        "ON CONFLICT (id) DO NOTHING"),
    "v2_runtime_control": (
        "INSERT INTO v2_runtime_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING"),
}

# Heuristic (not a real SQL parser) static scan for
# test_migration_seeded_singletons_constant_is_complete: finds
# `INSERT INTO <table> (<cols>) VALUES ...` at the top level of a migration
# file — i.e. NOT nested inside a dollar-quoted `CREATE FUNCTION ... $$ ... $$`
# trigger body (those run per-row at arbitrary future times with dynamic
# values like `OLD.user_id`, not a one-time constant seed at migration-apply
# time — e.g. `chat_r2_cleanup`'s trigger-populated INSERT would otherwise be
# a false positive here). `(VALUES|SELECT)` distinguishes a literal seed from
# a `SELECT ... FROM <other table>` backfill (those insert 0 rows into an
# empty fresh test DB and need no reseeding).
_DOLLAR_QUOTED_BODY_RE = re.compile(r"\$([A-Za-z_]*)\$.*?\$\1\$", re.DOTALL)
_INSERT_RE = re.compile(r"INSERT\s+INTO\s+(\w+)\s*\([^()]*\)\s*(VALUES|SELECT)\b", re.IGNORECASE)


def _tables_with_unconditional_seed_inserts() -> set[str]:
    # 两条迁移链都要扫：_clean 在 RDS 和 TEE 两侧各 TRUNCATE 一次，所以任何一侧
    # 的迁移期播种都会被清掉。TEE 侧目前只有 0001_tee_baseline 播 copytext_meta
    # （RDS 链同样播它，所以扫不扫 alembic_tee 今天结果一样），但将来 TEE 侧单独
    # 新增一张播种表时，只扫 RDS 链会漏掉它。
    backend = Path(__file__).parent.parent / "backend"
    seeded: set[str] = set()
    for root in (backend / "alembic" / "versions", backend / "alembic_tee" / "versions"):
        for path in root.glob("*.py"):
            text = _DOLLAR_QUOTED_BODY_RE.sub("", path.read_text())
            for table, verb in _INSERT_RE.findall(text):
                if verb.upper() == "VALUES":
                    seeded.add(table)
    return seeded


@pytest.fixture(autouse=True)
def _clean(backend_env):
    with db.get_pool().connection() as c:
        c.execute("TRUNCATE " + ", ".join(_RDS_TABLES) + " CASCADE")
        for sql in _MIGRATION_SEEDED_SINGLETONS.values():
            c.execute(sql)
    with mirror.get_tee_pool().connection() as c:
        c.execute("TRUNCATE " + ", ".join(_TEE_TABLES) + " CASCADE")
        for sql in _MIGRATION_SEEDED_SINGLETONS.values():
            c.execute(sql)
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


def test_plaintext_pointer_integrity_helper_checks_object_without_exposing_bytes(
    monkeypatch,
):
    uid = "usr_integrity"
    raw = b"\x00private-file\xff"
    key = f"chatfiles/{uid}/g1/msg/version"
    doc = {
        "body_key": key,
        "body_object_format": "plaintext_v1",
        "body_size_bytes": len(raw),
        "body_sha256": hashlib.sha256(raw).hexdigest(),
    }
    monkeypatch.setattr(
        object_storage,
        "get_chat_body_bytes",
        lambda fetched_key, fetched_uid: (
            raw if (fetched_key, fetched_uid) == (key, uid) else None
        ),
    )

    assert verify._plaintext_pointer_integrity_error(uid, doc) is None
    assert verify._plaintext_pointer_integrity_error(
        uid,
        {**doc, "body_size_bytes": len(raw) + 1},
    ) == "body_size_mismatch"
    assert verify._plaintext_pointer_integrity_error(
        uid,
        {**doc, "body_sha256": "0" * 64},
    ) == "body_sha256_mismatch"


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


def test_undecryptable_row_is_reported_not_fatal(backend_env, monkeypatch):
    """一条解不开的密文行必须被记成 mismatch，而不能炸掉整趟 verify。

    prod 2026-07-28 实测：某条 envelope 让 enclave 返回
    ``enclave_http_403:{"error":"decrypt_failed: envelope missing body_ct"}``。
    该异常不在抽样循环的 catch 列表里（当时只 catch PendingDeviceMigration）
    → 冒泡冲垮整趟 verify → 再被 tee_sync_scheduler 的兜底 except 静默吞掉
    → 24h 内 ``verify_ran`` 恒 false，unconverged_tables / requeue_backlog
    全线失去量测能力，而外部只看得到一条 warning 日志。

    与 replicate 侧 2026-07-15 已修的毒行是同一模式：一条坏行做队头阻塞。
    修复语义：坏行计入 mismatch 并继续，**不得**当成「两库一致」而跳过——
    那会用虚假的全绿换来更危险的静默。
    """
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    # 一条正常行（两侧一致）+ 一条解密必失败的毒行。两侧都放行，避免行数缺口
    # 掩盖了「内容抽样阶段」的行为——本测试断言的正是抽样阶段不能夭折。
    good_rds = _insert_rds_chat(uid, "good1", 10.0, "AAA")
    _insert_tee_chat(uid, "good1", 10.0,
                     transforms.plaintext_chat_doc(good_rds, _stub_decrypt))
    _insert_rds_chat(uid, "poison1", 11.0, "POISON")
    _insert_tee_chat(uid, "poison1", 11.0, {"body": "whatever", "ts": 11.0})

    def _poison_decrypt(envelope, purpose):
        if envelope.get("body_ct") == "POISON":
            raise RuntimeError(
                'enclave_http_403:{"error":"decrypt_failed: envelope missing body_ct"}')
        return _stub_decrypt(envelope, purpose)

    monkeypatch.setattr(verify, "_make_decrypt", lambda _uid: _poison_decrypt)

    # 修复前：这一行直接抛 RuntimeError，整趟 verify 夭折。
    report = verify.run(sample_rate=1.0)

    bad = [m for m in report["mismatches"] if m["item_id"] == "poison1"]
    assert bad, "解不开的行必须被记成 mismatch，而不是让整趟 verify 崩掉"
    assert bad[0]["field"] == "<decrypt-failed>"
    assert report["ok"] is False
    # 毒行之外的对账照常完成 —— 证明 verify 没有半途夭折。
    assert report["tables"]["chat_messages"]["rds_rows"] == 2
def test_verify_covers_every_synced_table():
    """verify 范围必须随注册表扩展——否则新增表会产生'全绿假象'：
    verify_ok=true 但那些表压根没被核对过。这是上游 plan 的 Phase 1 出口 gate。"""
    from tee_shadow import table_registry as reg
    from tee_shadow import verify

    covered = set(verify.covered_tables())
    missing = sorted(set(reg.synced_tables()) - covered)
    assert not missing, f"这些表进了 TEE 但 verify 不核对它们：{missing}"


def _insert_rds_voice_transcript(uid: str, call_id: str) -> None:
    envelope = {
        "v": 1,
        "id": call_id,
        "owner_user_id": uid,
        "visibility": "shared",
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
    }
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO voice_transcripts "
            "(user_id, call_id, transcript_envelope) VALUES (%s,%s,%s)",
            (uid, call_id, Jsonb(envelope)),
        )


def _insert_tee_voice_transcript(uid: str, call_id: str) -> None:
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO voice_transcripts (user_id, call_id, doc) VALUES (%s,%s,%s)",
            (uid, call_id, Jsonb({"transcript": "hello"})),
        )


def test_voice_transcripts_equal_rows_are_strictly_verified():
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_rds_voice_transcript(uid, "vcall_equal")
    _insert_tee_voice_transcript(uid, "vcall_equal")

    report = verify.run(sample_rate=1.0)

    voice = report["tables"]["voice_transcripts"]
    assert voice["rds_rows"] == 1
    assert voice["tee_rows"] == 1
    assert voice["pending_rows"] == 0
    assert voice["rows_ok"] is True
    assert report["strict_ok"] is True
    assert report["ok"] is True


def test_voice_transcripts_rds_only_row_fails_strict_verification():
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_rds_voice_transcript(uid, "vcall_missing")

    report = verify.run(sample_rate=1.0)

    voice = report["tables"]["voice_transcripts"]
    assert voice["rds_rows"] == 1
    assert voice["tee_rows"] == 0
    assert voice["pending_rows"] == 0
    assert voice["rows_ok"] is False
    assert report["strict_ok"] is False
    assert report["ok"] is False


def test_voice_transcript_terminal_pending_row_satisfies_strict_verification():
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    call_id = "vcall_pending"
    _seed(uid)
    _insert_rds_voice_transcript(uid, call_id)
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tee_pending_device_migration "
            "(user_id, table_name, item_id, reason) VALUES (%s,%s,%s,%s)",
            (uid, "voice_transcripts", call_id, "pdm:no_k_enclave"),
        )

    report = verify.run(sample_rate=1.0)

    voice = report["tables"]["voice_transcripts"]
    assert voice["pending_rows"] == 1
    assert voice["rows_ok"] is True
    assert voice["requeue_backlog"] == 0
    assert report["strict_ok"] is True
    assert report["ok"] is True


# --------------------------------------------------------------------------- #
# Fix round 1 review: SNAPSHOT lane / kind=None ciphertext lane / C1 regression
# had zero coverage — the autouse _clean fixture truncates every newly-covered
# table to {rds:0, tee:0} on both sides for every existing test, so
# _rows_ok_advisory, the SNAPSHOT loop, strict_rows_ok, and the "kind is None"
# sampling skip could all be silently broken and nothing here would go red.
# --------------------------------------------------------------------------- #

def test_snapshot_table_rds_only_row_is_not_ok():
    """SNAPSHOT lane 的 advisory 判据仍然要抓真正的"从没同步过"：RDS 有行、TEE
    一行都没有时 rows_ok 必须是 False，report["ok"] 必须是 False——这是
    _rows_ok_advisory 唯一要保留的红灯场景，C1 的 pending 修复或别的改动都不能
    误伤它。"""
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO dau_daily_snapshot (day) VALUES ('2026-07-01')")

    report = verify.run(sample_rate=1.0)

    snap = report["tables"]["dau_daily_snapshot"]
    assert snap["rds_rows"] == 1
    assert snap["tee_rows"] == 0
    assert snap["row_drift"] == 1
    assert snap["strict_rows_ok"] is False
    assert snap["rows_ok"] is False
    assert report["ok"] is False


def test_snapshot_table_partial_lag_is_advisory_ok_but_strict_flags_it():
    """SNAPSHOT 是 tick 级整表替换，两次 tick 之间 RDS 比 TEE 多几行是正常滞后，
    不该拖垮 report["ok"]——但 strict_rows_ok 必须如实记录"这一刻两边不相等"，
    否则 M9/M10 想保留给人看的 strict 信号在测试里也会隐身不被验证。"""
    days = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"]
    with db.get_pool().connection() as c:
        for day in days:
            c.execute("INSERT INTO dau_daily_snapshot (day) VALUES (%s)", (day,))
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        for day in days[:3]:
            c.execute("INSERT INTO dau_daily_snapshot (day) VALUES (%s)", (day,))

    report = verify.run(sample_rate=1.0)

    snap = report["tables"]["dau_daily_snapshot"]
    assert snap["rds_rows"] == 5
    assert snap["tee_rows"] == 3
    assert snap["strict_rows_ok"] is False
    assert snap["rows_ok"] is True
    assert report["ok"] is True


def test_kind_none_ciphertext_table_skips_content_sampling():
    """kind=None 的 7 张新表只核行数、不做内容抽样——_sample_ciphertext_content
    写死 `_TRANSFORM[cfg["kind"]]`，`cfg["kind"] is None` 时这一行本身就会
    KeyError，所以 run() 里的 `continue` 是唯一能让这些表不崩的东西。用
    sample_rate=1.0 强制"如果没跳过一定会被抽中"，并且 TEE 侧内容故意与 RDS
    不同：如果 `continue` 被删掉，这条用例要么在 mismatches 里看见这一行、要么
    直接因为上述 KeyError 整个 run() 崩溃报错，两种结果都不是"悄悄没抽中"，
    都能把回归暴露出来。"""
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    cred_id = str(uuid.uuid4())
    with db.get_pool().connection() as c:
        c.execute(
            "INSERT INTO model_api_credentials (id, user_id, provider, api_key_envelope) "
            "VALUES (%s,%s,'openai',%s)", (cred_id, uid, Jsonb({"body_ct": "RDS-VALUE"})))
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute(
            "INSERT INTO model_api_credentials (id, user_id, provider, api_key_envelope) "
            "VALUES (%s,%s,'openai',%s)",
            (cred_id, uid, Jsonb({"body_ct": "DELIBERATELY-DIFFERENT"})))

    report = verify.run(sample_rate=1.0)

    assert report["tables"]["model_api_credentials"]["rows_ok"] is True
    assert [m for m in report["mismatches"] if m["table"] == "model_api_credentials"] == []
    assert report["ok"] is True


def test_ciphertext_terminal_pending_counts_toward_advisory_rows_ok():
    """C1 回归：strict 判据 rds==tee+pending 这里本来就成立（1==0+1），advisory
    覆写绝不能把一个本来就 True 的结果变成 False。旧代码把 `_rows_ok_advisory`
    只喂 tee_rows、无视 pending_rows，会在"RDS 1 行、TEE 0 行、1 条终态 pending"
    这个完全健康的场景里把 rows_ok 从 True 错误覆写成 False——比 strict 判据
    更严，正是 advisory 机制本该避免的那种没人再看的红灯（_split_pending 早就
    承诺过终态 pending 的行"本就不该有 TEE 对应行"）。"""
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    cred_id = str(uuid.uuid4())
    with db.get_pool().connection() as c:
        c.execute(
            "INSERT INTO model_api_credentials (id, user_id, provider, api_key_envelope) "
            "VALUES (%s,%s,'openai',%s)", (cred_id, uid, Jsonb({"body_ct": "X"})))
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute(
            "INSERT INTO tee_pending_device_migration (user_id, table_name, item_id, reason) "
            "VALUES (%s,'model_api_credentials',%s,'pdm:no_k_enclave')", (uid, cred_id))

    report = verify.run(sample_rate=1.0)

    cred_report = report["tables"]["model_api_credentials"]
    assert cred_report["rds_rows"] == 1
    assert cred_report["tee_rows"] == 0
    assert cred_report["pending_rows"] == 1
    assert cred_report["strict_rows_ok"] is True
    assert cred_report["rows_ok"] is True
    assert report["ok"] is True


def test_migration_seeded_singletons_constant_is_complete():
    """`_MIGRATION_SEEDED_SINGLETONS` carries the invariant "`_clean` must
    reseed this table on both sides after TRUNCATE" — missing a newly added
    migration-seeded singleton here reproduces the `v2_runtime_control`
    regression from fix-round-1 (see the dict's own comment): `_clean` would
    permanently delete that row from the shared session test DB, breaking
    some unrelated, later-alphabetical test file's UPDATE-only fixture in a
    way a single-file test run would never catch.

    The scan is a heuristic, not a real SQL parser (see `_INSERT_RE`'s
    comment) — manually cross-checked on 2026-07-28 against every one of
    verify's 52 covered tables' migrations (fix-round-1 review). If this scan
    starts producing a false positive/negative for some future migration,
    fix the regex or fall back to a hand-verified assertion with a dated
    comment — don't just widen this test to swallow the mismatch.
    """
    covered_real = set(verify.covered_tables()) - set(reg.PSEUDO_CIPHERTEXT_TABLES)
    seeded = _tables_with_unconditional_seed_inserts() & covered_real
    assert seeded == set(_MIGRATION_SEEDED_SINGLETONS), (
        "迁移期无条件常量播种表与 _MIGRATION_SEEDED_SINGLETONS 不一致：\n"
        f"扫描发现但常量里没有（需要加进 _clean 的重播种列表）："
        f"{sorted(seeded - set(_MIGRATION_SEEDED_SINGLETONS))}\n"
        f"常量里有但扫描没发现（该表可能已改名/该 migration 已删，需要人工确认）："
        f"{sorted(set(_MIGRATION_SEEDED_SINGLETONS) - seeded)}")
