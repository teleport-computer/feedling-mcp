"""P0 acceptance: a genesis job orphaned by a killed/replaced worker (esp. a
container deploy) recovers within the dead cutoff — via live-heartbeat detection —
instead of the 30-min time reaper; a live worker's job is never falsely reclaimed;
the time reaper still backstops an alive-but-wedged job."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import conftest  # noqa: E402
import db  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")


def _seed_hb(worker_id, *, age_sec, kind="genesis"):
    with db.get_pool().connection() as c:
        c.execute(
            "INSERT INTO v2_worker_heartbeats (worker_id, beat_at, kind, capacity) "
            "VALUES (%s, now() - make_interval(secs=>%s), %s, 0) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at=EXCLUDED.beat_at, kind=EXCLUDED.kind",
            (worker_id, age_sec, kind))


def _seed_processing(uid, jid, *, worker, claimed_age_sec, chunks, updated_age_sec=None):
    conftest.seed_user(uid)
    with db.get_pool().connection() as c:
        c.execute("DELETE FROM genesis_import_jobs WHERE user_id=%s AND job_id=%s", (uid, jid))
    db.genesis_create_job(uid, {"job_id": jid, "status": "uploaded",
                                "source_kind": "history_import", "total_chunks": 3})
    ua = claimed_age_sec if updated_age_sec is None else updated_age_sec
    with db.get_pool().connection() as c:
        c.execute(
            "UPDATE genesis_import_jobs SET status='processing', received_chunks=%s, "
            "worker_claimed_by=%s, worker_claimed_at=now()-make_interval(secs=>%s), "
            "updated_at=now()-make_interval(secs=>%s) WHERE user_id=%s AND job_id=%s",
            (chunks, worker, claimed_age_sec, ua, uid, jid))


def _status(uid, jid):
    with db.get_pool().connection() as c:
        return c.execute("SELECT status FROM genesis_import_jobs WHERE user_id=%s AND job_id=%s",
                         (uid, jid)).fetchone()[0]


def test_deploy_orphan_recovers_within_dead_cutoff_not_30min():
    dead = "v2-worker-OLD-2a19f08:genesis"     # the killed worker (no fresh heartbeat)
    live = "v2-worker-NEW-cd48301:genesis"     # the replacement, heartbeating now
    _seed_hb(live, age_sec=2)                   # NEW worker is alive
    _seed_hb(dead, age_sec=600)                 # OLD worker's heartbeat is 10min stale
    # a plaintext onboarding job (no stored chunks) claimed by the dead worker
    _seed_processing("u_p0_plain", "j_plain", worker=dead, claimed_age_sec=300, chunks=0)
    # a chunked import (chunks stored) claimed by the dead worker
    _seed_processing("u_p0_chunk", "j_chunk", worker=dead, claimed_age_sec=300, chunks=3)

    live_ids = jobs_store.live_genesis_worker_ids(within_sec=120)
    assert live in live_ids and dead not in live_ids   # detection: OLD is gone

    db.genesis_reclaim_orphaned_processing_jobs(live_ids, dead_sec=120, error="genesis_worker_lost")

    assert _status("u_p0_plain", "j_plain") == "failed"     # fast-fail → client retries
    assert _status("u_p0_chunk", "j_chunk") == "uploaded"   # requeued → live worker re-runs


def test_live_worker_job_never_falsely_reclaimed_even_when_updated_at_stale():
    live = "v2-worker-LIVE:genesis"
    _seed_hb(live, age_sec=3)   # worker is alive (independent heartbeat keeps beating)
    # its job's updated_at is very stale (mid long distill, no per-chunk touch) but
    # the worker still heartbeats → must NOT be reclaimed
    _seed_processing("u_p0_live", "j_live", worker=live, claimed_age_sec=1200,
                     chunks=0, updated_age_sec=1200)
    live_ids = jobs_store.live_genesis_worker_ids(within_sec=120)
    db.genesis_reclaim_orphaned_processing_jobs(live_ids, dead_sec=120, error="x")
    assert _status("u_p0_live", "j_live") == "processing"


def test_time_reaper_backstop_still_fails_wedged_job():
    # A job whose worker heartbeats fine but the job genuinely hangs past the 30-min
    # cutoff is still failed by the time reaper (unchanged backstop).
    _seed_processing("u_p0_wedge", "j_wedge", worker="whatever:genesis",
                     claimed_age_sec=60, chunks=0, updated_age_sec=2000)
    db.genesis_reap_stale_processing_jobs(1800, error="genesis_stale_timeout:1800s")
    assert _status("u_p0_wedge", "j_wedge") == "failed"
