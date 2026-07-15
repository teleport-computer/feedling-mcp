"""The resident distill job reaper — wired at last.

Live-e2e-confirmed bug (2026-07-14): a resident-claimed job whose consumer hit a
transient error after claim stayed 'processing' FOREVER — db.genesis_reap_stale_
resident_jobs existed but had zero callers, and the cloud reaper explicitly
excludes resident-owned rows. These tests cover the new worker wrapper
``reap_stale_resident_jobs``: stale + under attempt cap → requeued to
awaiting_resident; stale + at/over cap → failed; fresh heartbeat → untouched.
"""
import sys
import tempfile
import os
from pathlib import Path

os.environ.setdefault("FEEDLING_DATA_DIR", tempfile.mkdtemp(prefix="feedling-reaper-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import db  # noqa: E402
from genesis import worker as genesis_worker  # noqa: E402

from conftest import seed_user  # noqa: E402


def _mk_resident_job(uid: str, job_id: str) -> None:
    db.genesis_create_job(uid, {
        "job_id": job_id,
        "status": "awaiting_resident",
        "source_kind": "update_identity",
        "total_chunks": 1,
        "total_bytes": 10,
        "privacy_mode": "resident_sealed",
        "metadata": {"mode": "update_identity", "ingest": "resident_sealed"},
    })


def _backdate_heartbeat(uid: str, job_id: str, seconds: int) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE genesis_import_jobs SET resident_heartbeat_at = now() - make_interval(secs => %s) "
            "WHERE user_id = %s AND job_id = %s",
            (seconds, uid, job_id),
        )


def _set_attempts(uid: str, job_id: str, attempts: int) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE genesis_import_jobs SET resident_attempts = %s WHERE user_id = %s AND job_id = %s",
            (attempts, uid, job_id),
        )


def test_stale_resident_job_requeued_under_cap():
    uid = "usr_reaper_requeue"
    seed_user(uid)
    _mk_resident_job(uid, "genesis_reap1")
    claimed = db.genesis_claim_resident_jobs(uid, consumer_id="c1", limit=1)
    assert len(claimed) == 1 and claimed[0]["status"] == "processing"
    _backdate_heartbeat(uid, "genesis_reap1", 24 * 3600)

    reaped = genesis_worker.reap_stale_resident_jobs()

    ids = {(r.get("user_id"), r.get("job_id")) for r in reaped}
    assert (uid, "genesis_reap1") in ids
    job = db.genesis_get_job(uid, "genesis_reap1")
    assert job["status"] == "awaiting_resident"       # requeued, claimable again
    assert not job.get("resident_consumer_id")
    assert not job.get("error")
    # and it IS re-claimable:
    again = db.genesis_claim_resident_jobs(uid, consumer_id="c2", limit=1)
    assert len(again) == 1 and again[0]["job_id"] == "genesis_reap1"


def test_stale_resident_job_failed_at_attempt_cap():
    uid = "usr_reaper_fail"
    seed_user(uid)
    _mk_resident_job(uid, "genesis_reap2")
    db.genesis_claim_resident_jobs(uid, consumer_id="c1", limit=1)
    _set_attempts(uid, "genesis_reap2", 99)
    _backdate_heartbeat(uid, "genesis_reap2", 24 * 3600)

    genesis_worker.reap_stale_resident_jobs()

    job = db.genesis_get_job(uid, "genesis_reap2")
    assert job["status"] == "failed"
    assert "resident_stale" in str(job.get("error") or "")


def test_fresh_heartbeat_untouched():
    uid = "usr_reaper_fresh"
    seed_user(uid)
    _mk_resident_job(uid, "genesis_reap3")
    db.genesis_claim_resident_jobs(uid, consumer_id="c1", limit=1)
    # heartbeat is 'now()' from the claim — well inside the lease.

    genesis_worker.reap_stale_resident_jobs()

    job = db.genesis_get_job(uid, "genesis_reap3")
    assert job["status"] == "processing"
    assert job.get("resident_consumer_id") == "c1"
