"""P2: resident job lease — heartbeat (owner-only) + stale reap (re-queue under cap, fail at cap)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
from conftest import seed_user  # noqa: E402


def _backdate(user_id, job_id, *, heartbeat_ago_sec, attempts):
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE genesis_import_jobs SET resident_heartbeat_at = now() - make_interval(secs=>%s), "
            "resident_attempts = %s WHERE user_id=%s AND job_id=%s",
            (heartbeat_ago_sec, attempts, user_id, job_id),
        )


def _claim_one(user_id, job_id, consumer):
    seed_user(user_id)
    db.genesis_create_job(user_id, {"job_id": job_id, "status": "awaiting_resident"})
    got = db.genesis_claim_resident_jobs(user_id, consumer_id=consumer, limit=64)
    assert [j for j in got if j["user_id"] == user_id and j["job_id"] == job_id]


def test_heartbeat_only_owner_renews():
    uid, jid = "usr_lease_hb", "job_lease_hb"
    _claim_one(uid, jid, "cons-A")
    assert db.genesis_resident_heartbeat(uid, jid, consumer_id="cons-A") is True
    assert db.genesis_resident_heartbeat(uid, jid, consumer_id="cons-OTHER") is False


def test_reap_requeues_under_cap():
    uid, jid = "usr_lease_req", "job_lease_req"
    _claim_one(uid, jid, "cons-A")            # attempts -> 1
    _backdate(uid, jid, heartbeat_ago_sec=7200, attempts=1)   # 2h stale, 1 attempt
    reaped = db.genesis_reap_stale_resident_jobs(1800, max_attempts=3, error="consumer_died", limit=64)
    mine = [j for j in reaped if j["user_id"] == uid and j["job_id"] == jid]
    assert len(mine) == 1
    j = db.genesis_get_job(uid, jid)
    assert j["status"] == "awaiting_resident"          # re-queued (1 < 3)
    assert j["resident_consumer_id"] == ""             # consumer cleared → re-claimable
    assert j["failed_phase"] == ""


def test_reap_fails_at_cap():
    uid, jid = "usr_lease_fail", "job_lease_fail"
    _claim_one(uid, jid, "cons-A")
    _backdate(uid, jid, heartbeat_ago_sec=7200, attempts=3)   # 2h stale, at cap
    db.genesis_reap_stale_resident_jobs(1800, max_attempts=3, error="consumer_died", limit=64)
    j = db.genesis_get_job(uid, jid)
    assert j["status"] == "failed"
    assert j["error"] == "consumer_died"
    assert j["failed_phase"] == "resident_claimed"


def test_worker_reaper_ignores_resident_owned_processing():
    # The CVM worker's stale reaper must NOT fail a resident-owned processing job —
    # that's the resident reaper's job (attempt-cap re-queue), not a hard fail.
    uid, jid = "usr_worker_reap_res", "job_worker_reap_res"
    _claim_one(uid, jid, "cons-A")   # resident job -> processing, resident_consumer_id set
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE genesis_import_jobs SET updated_at = now() - make_interval(secs=>7200) "
            "WHERE user_id=%s AND job_id=%s",
            (uid, jid),
        )
    reaped = db.genesis_reap_stale_processing_jobs(1800, error="worker_stale", limit=64)
    assert not [j for j in reaped if j["user_id"] == uid and j["job_id"] == jid]
    assert db.genesis_get_job(uid, jid)["status"] == "processing"  # untouched by worker reaper


def test_reap_skips_fresh_heartbeat():
    uid, jid = "usr_lease_fresh", "job_lease_fresh"
    _claim_one(uid, jid, "cons-A")            # heartbeat = now (fresh)
    reaped = db.genesis_reap_stale_resident_jobs(1800, max_attempts=3, error="x", limit=64)
    assert not [j for j in reaped if j["user_id"] == uid and j["job_id"] == jid]
    assert db.genesis_get_job(uid, jid)["status"] == "processing"   # untouched, still owned
