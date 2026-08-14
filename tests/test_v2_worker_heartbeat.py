"""v2_worker_heartbeats: liveness table + jobs_store.record_worker_heartbeat /
jobs_store.workers_alive. Backs the send-path guard that refuses chat/send in
db_action_v2 mode when no serve_worker process has beaten recently (Task 2)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store


def _clear():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_worker_heartbeats")


def test_workers_alive_false_on_empty_table():
    _clear()
    assert jobs_store.workers_alive() is False


def test_workers_alive_true_after_heartbeat():
    _clear()
    jobs_store.record_worker_heartbeat("w1")
    assert jobs_store.workers_alive() is True
    _clear()


def test_workers_alive_false_when_only_heartbeat_is_stale():
    _clear()
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_worker_heartbeats (worker_id, beat_at) "
            "VALUES ('w_stale', now() - interval '60 seconds')"
        )
    assert jobs_store.workers_alive(within_sec=30) is False
    _clear()


def test_record_worker_heartbeat_upserts_same_worker_id():
    _clear()
    jobs_store.record_worker_heartbeat("w1")
    jobs_store.record_worker_heartbeat("w1")
    with db.get_pool().connection() as conn:
        rows = conn.execute("SELECT worker_id FROM v2_worker_heartbeats").fetchall()
    assert [r[0] for r in rows] == ["w1"]
    _clear()


def test_live_worker_capacity_sums_turn_slots_and_ignores_genesis():
    _clear()
    jobs_store.record_worker_heartbeat("turn-a", capacity=4)
    jobs_store.record_worker_heartbeat("turn-b", capacity=8)
    jobs_store.record_worker_heartbeat("genesis", kind="genesis", capacity=0)
    assert jobs_store.live_worker_count() == 2
    assert jobs_store.live_worker_capacity() == 12
    _clear()
