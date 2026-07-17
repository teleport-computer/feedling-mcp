"""Backstop reaper for genesis jobs wedged 'awaiting_resident' — never claimed.

Gap (2026-07-17, prod): a sealed resident-distill job that NO consumer ever claims
sits in 'awaiting_resident' with no timeout. The two existing reapers only touch
'processing' rows (cloud excludes resident-owned; resident variant requires a claim),
so an unclaimed job — consumer running stale code that never opened the distill lane,
offline, or never started — leaves the app spinning "开始中" forever, no error, no
feedback. This reaper fails such jobs past a generous cutoff so the app surfaces a
terminal/visible failure instead of an eternal spinner. The cutoff is deliberately long so
a consumer that self-updates late still claims the job before it is reaped.
"""
import sys
import tempfile
import os
from pathlib import Path

os.environ.setdefault("FEEDLING_DATA_DIR", tempfile.mkdtemp(prefix="feedling-unclaimed-reaper-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import db  # noqa: E402
from genesis import worker as genesis_worker  # noqa: E402
from notices import catalog  # noqa: E402

from conftest import seed_user  # noqa: E402


def _mk_unclaimed_job(uid: str, job_id: str) -> None:
    db.genesis_create_job(uid, {
        "job_id": job_id,
        "status": "awaiting_resident",
        "source_kind": "update_identity",
        "total_chunks": 1,
        "total_bytes": 10,
        "privacy_mode": "resident_sealed",
        "metadata": {"mode": "update_identity", "ingest": "resident_sealed"},
    })


def _backdate_updated(uid: str, job_id: str, seconds: int) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE genesis_import_jobs SET updated_at = now() - make_interval(secs => %s) "
            "WHERE user_id = %s AND job_id = %s",
            (seconds, uid, job_id),
        )


def test_stale_unclaimed_job_failed():
    uid = "usr_unclaimed_stale"
    seed_user(uid)
    _mk_unclaimed_job(uid, "genesis_unclaimed1")
    _backdate_updated(uid, "genesis_unclaimed1", 25 * 3600)  # past the 24h default cutoff
    assert db.get_blob(uid, "genesis_state") is None

    reaped = genesis_worker.reap_stale_unclaimed_jobs()

    ids = {(r.get("user_id"), r.get("job_id")) for r in reaped}
    assert (uid, "genesis_unclaimed1") in ids
    job = db.genesis_get_job(uid, "genesis_unclaimed1")
    assert job["status"] == "failed"
    assert str(job.get("error") or "").startswith("resident_never_claimed:")
    blob = db.get_blob(uid, "genesis_state")
    assert blob["job_id"] == "genesis_unclaimed1"
    assert blob["status"] == "failed"


def test_fresh_unclaimed_job_untouched():
    uid = "usr_unclaimed_fresh"
    seed_user(uid)
    _mk_unclaimed_job(uid, "genesis_unclaimed2")
    # just created — updated_at is now(), well inside the cutoff

    genesis_worker.reap_stale_unclaimed_jobs()

    job = db.genesis_get_job(uid, "genesis_unclaimed2")
    assert job["status"] == "awaiting_resident"
    assert not job.get("error")


def test_claimed_job_not_touched_by_unclaimed_reaper():
    uid = "usr_unclaimed_claimed"
    seed_user(uid)
    _mk_unclaimed_job(uid, "genesis_unclaimed3")
    db.genesis_claim_resident_jobs(uid, consumer_id="c1", limit=1)  # awaiting_resident -> processing
    _backdate_updated(uid, "genesis_unclaimed3", 25 * 3600)  # old, but now 'processing'

    genesis_worker.reap_stale_unclaimed_jobs()

    job = db.genesis_get_job(uid, "genesis_unclaimed3")
    # claimed rows belong to the resident-stale reaper, not this one
    assert job["status"] == "processing"


def test_unclaimed_error_is_not_misclassified_as_upstream_unavailable():
    uid = "usr_unclaimed_notice_classification"
    seed_user(uid)
    _mk_unclaimed_job(uid, "genesis_unclaimed4")
    _backdate_updated(uid, "genesis_unclaimed4", 25 * 3600)

    genesis_worker.reap_stale_unclaimed_jobs()

    job = db.genesis_get_job(uid, "genesis_unclaimed4")
    error = str(job.get("error") or "")
    assert catalog.classify_upstream(error) != "upstream_unavailable"
    assert error.startswith("resident_never_claimed:")


def test_old_unclaimed_job_does_not_clobber_newer_processing_state():
    uid = "usr_unclaimed_mixed_jobs"
    seed_user(uid)

    # /pending claims at most four jobs. Make J2-J5 older so the claim leaves J1
    # awaiting_resident, matching the mixed state seen in production.
    processing_ids = [f"genesis_processing{i}" for i in range(2, 6)]
    for job_id in processing_ids:
        _mk_unclaimed_job(uid, job_id)
        _backdate_updated(uid, job_id, 60)
    _mk_unclaimed_job(uid, "genesis_old_unclaimed")

    claimed = db.genesis_claim_resident_jobs(uid, consumer_id="c1", limit=4)
    assert {job["job_id"] for job in claimed} == set(processing_ids)
    assert db.genesis_get_job(uid, "genesis_old_unclaimed")["status"] == "awaiting_resident"

    current_job_id = claimed[0]["job_id"]
    db.set_blob(uid, "genesis_state", {
        "v": 1,
        "job_id": current_job_id,
        "status": "processing",
        "sentinel": "keep-whole-blob",
    })
    blob_before = db.get_blob(uid, "genesis_state")
    _backdate_updated(uid, "genesis_old_unclaimed", 25 * 3600)

    genesis_worker.reap_stale_unclaimed_jobs()

    assert db.genesis_get_job(uid, "genesis_old_unclaimed")["status"] == "failed"
    assert db.get_blob(uid, "genesis_state") == blob_before
