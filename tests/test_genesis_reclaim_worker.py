"""genesis.worker.reclaim_orphaned_processing_jobs: reads the DB reclaim + syncs
genesis_state per changed row. DB seam monkeypatched — pure unit."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from genesis import worker as gworker  # noqa: E402


def test_reclaim_syncs_state_per_action(monkeypatch):
    seen = {}

    def _fake_reclaim(live, *, dead_sec, error, limit=50):
        seen["live"] = live
        seen["dead_sec"] = dead_sec
        seen["error"] = error
        return [
            {"user_id": "u1", "job_id": "j1", "status": "failed",
             "worker_claimed_by": "dead:genesis", "_reclaim_action": "failed"},
            {"user_id": "u2", "job_id": "j2", "status": "uploaded",
             "worker_claimed_by": "", "_reclaim_action": "requeued"},
        ]

    monkeypatch.setattr(gworker.db, "genesis_reclaim_orphaned_processing_jobs", _fake_reclaim)
    monkeypatch.setattr(gworker, "get_store", lambda uid: object())
    monkeypatch.setattr(gworker, "_trace_genesis", lambda *a, **k: None)
    synced = []
    monkeypatch.setattr(gworker.service, "write_genesis_state",
                        lambda store, job, status: synced.append((job["job_id"], status)))

    out = gworker.reclaim_orphaned_processing_jobs(["live:genesis"])

    assert seen["live"] == ["live:genesis"]
    assert seen["dead_sec"] == gworker._genesis_worker_dead_sec()
    assert {(j["job_id"], j["action"]) for j in out} == {("j1", "failed"), ("j2", "requeued")}
    # failed -> terminal blob; requeued -> back to importing (uploaded)
    assert ("j1", "failed") in synced
    assert ("j2", "uploaded") in synced


def test_reclaim_no_live_workers_is_noop(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(gworker.db, "genesis_reclaim_orphaned_processing_jobs",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [])
    # empty live set: fail closed, never call the DB reclaim (can't tell dead from live)
    assert gworker.reclaim_orphaned_processing_jobs([]) == []
    assert called["n"] == 0


def test_dead_sec_env_floor(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_WORKER_DEAD_SEC", "10")  # below floor
    assert gworker._genesis_worker_dead_sec() == 60
    monkeypatch.setenv("FEEDLING_GENESIS_WORKER_DEAD_SEC", "300")
    assert gworker._genesis_worker_dead_sec() == 300
