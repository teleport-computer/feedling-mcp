"""genesis claim attribution + death-detected orphan reclaim (db layer)."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import conftest  # noqa: E402
import db  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")


def _clean(uid):
    conftest.seed_user(uid)  # genesis_import_jobs.user_id has a FK to users
    with db.get_pool().connection() as c:
        c.execute("DELETE FROM genesis_import_jobs WHERE user_id=%s", (uid,))


def _mk_uploaded(uid, jid, *, chunks=0):
    db.genesis_create_job(uid, {"job_id": jid, "status": "uploaded",
                                "source_kind": "history_import", "total_chunks": 3})
    with db.get_pool().connection() as c:
        c.execute("UPDATE genesis_import_jobs SET status='uploaded', received_chunks=%s, "
                  "finalized_at=now() WHERE user_id=%s AND job_id=%s", (chunks, uid, jid))


def _mk_processing(uid, jid, *, worker, claimed_age_sec, chunks):
    db.genesis_create_job(uid, {"job_id": jid, "status": "uploaded",
                                "source_kind": "history_import", "total_chunks": 3})
    with db.get_pool().connection() as c:
        c.execute(
            "UPDATE genesis_import_jobs SET status='processing', received_chunks=%s, "
            "worker_claimed_by=%s, worker_claimed_at = now() - make_interval(secs=>%s), "
            "updated_at = now() - make_interval(secs=>%s) "
            "WHERE user_id=%s AND job_id=%s",
            (chunks, worker, claimed_age_sec, claimed_age_sec, uid, jid))


# --- Task 3: claim records the worker id ---

def test_claim_records_worker_id():
    uid, jid = "u_gclaim", "genesis_claim1"
    _clean(uid)
    _mk_uploaded(uid, jid)
    claimed = db.genesis_claim_uploaded_jobs(worker_id="w-1:genesis", limit=5)
    assert any(j["job_id"] == jid for j in claimed)
    with db.get_pool().connection() as c:
        row = c.execute("SELECT worker_claimed_by, worker_claimed_at, status "
                        "FROM genesis_import_jobs WHERE user_id=%s AND job_id=%s",
                        (uid, jid)).fetchone()
    assert row[0] == "w-1:genesis"
    assert row[1] is not None
    assert row[2] == "processing"


def test_claim_without_worker_id_is_blank_attribution():
    uid, jid = "u_gclaim2", "genesis_claim2"
    _clean(uid)
    _mk_uploaded(uid, jid)
    db.genesis_claim_uploaded_jobs(limit=5)  # old-style call, no worker_id
    with db.get_pool().connection() as c:
        wid, st = c.execute("SELECT worker_claimed_by, status FROM genesis_import_jobs "
                            "WHERE user_id=%s AND job_id=%s", (uid, jid)).fetchone()
    assert wid == "" and st == "processing"


# --- Task 4: death-detected reclaim ---

def test_orphan_with_chunks_requeued_to_uploaded():
    uid = "u_reclaim"
    _clean(uid)
    _mk_processing(uid, "j_chunks", worker="dead:genesis", claimed_age_sec=300, chunks=3)
    changed = db.genesis_reclaim_orphaned_processing_jobs(
        ["live:genesis"], dead_sec=120, error="genesis_worker_lost")
    row = next(j for j in changed if j["job_id"] == "j_chunks")
    assert row["_reclaim_action"] == "requeued"
    with db.get_pool().connection() as c:
        st, wid = c.execute("SELECT status, worker_claimed_by FROM genesis_import_jobs "
                            "WHERE user_id=%s AND job_id='j_chunks'", (uid,)).fetchone()
    assert st == "uploaded" and wid == ""


def test_orphan_plaintext_failed_fast():
    uid = "u_reclaim2"
    _clean(uid)
    _mk_processing(uid, "j_plain", worker="dead:genesis", claimed_age_sec=300, chunks=0)
    changed = db.genesis_reclaim_orphaned_processing_jobs(
        ["live:genesis"], dead_sec=120, error="genesis_worker_lost")
    assert next(j for j in changed if j["job_id"] == "j_plain")["_reclaim_action"] == "failed"
    with db.get_pool().connection() as c:
        st, err = c.execute("SELECT status, error FROM genesis_import_jobs "
                            "WHERE user_id=%s AND job_id='j_plain'", (uid,)).fetchone()
    assert st == "failed" and "genesis_worker_lost" in err


def test_live_worker_job_untouched():
    uid = "u_reclaim3"
    _clean(uid)
    _mk_processing(uid, "j_live", worker="live:genesis", claimed_age_sec=300, chunks=0)
    changed = db.genesis_reclaim_orphaned_processing_jobs(
        ["live:genesis"], dead_sec=120, error="x")
    assert not any(j["job_id"] == "j_live" for j in changed)
    with db.get_pool().connection() as c:
        st = c.execute("SELECT status FROM genesis_import_jobs WHERE user_id=%s AND job_id='j_live'",
                       (uid,)).fetchone()[0]
    assert st == "processing"


def test_recently_claimed_orphan_not_yet_dead():
    uid = "u_reclaim4"
    _clean(uid)
    _mk_processing(uid, "j_new", worker="dead:genesis", claimed_age_sec=30, chunks=0)
    changed = db.genesis_reclaim_orphaned_processing_jobs(
        ["live:genesis"], dead_sec=120, error="x")
    assert not any(j["job_id"] == "j_new" for j in changed)  # claimed 30s ago < 120s dead_sec


def test_unattributed_processing_job_left_to_time_reaper():
    # A processing job with blank worker_claimed_by (pre-attribution / legacy) is
    # NOT eligible for death-detected reclaim — the 30-min time reaper owns it.
    uid = "u_reclaim5"
    _clean(uid)
    _mk_processing(uid, "j_blank", worker="", claimed_age_sec=300, chunks=0)
    changed = db.genesis_reclaim_orphaned_processing_jobs(
        ["live:genesis"], dead_sec=120, error="x")
    assert not any(j["job_id"] == "j_blank" for j in changed)
