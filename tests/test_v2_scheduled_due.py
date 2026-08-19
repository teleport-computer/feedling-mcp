import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
from model_api_runtime.v2 import jobs_store

_STREAM = "proactive_scheduled_wakes_v2"


@pytest.fixture(autouse=True)
def _clean_stream():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
        conn.execute("DELETE FROM user_logs WHERE stream = %s", (_STREAM,))
    yield


def _append(uid, timer_id, doc, ts=None):
    db.log_append(uid, _STREAM, doc, ts=ts or time.time(), item_key=timer_id)


def test_returns_user_with_a_due_pending_timer():
    conftest.seed_user("u_due_1")
    _append("u_due_1", "t1", {"status": "pending", "due_at": 100.0})
    assert jobs_store.due_scheduled_users(now=200.0) == ["u_due_1"]


def test_ignores_a_timer_that_is_not_due_yet():
    conftest.seed_user("u_due_2")
    _append("u_due_2", "t1", {"status": "pending", "due_at": 999.0})
    assert jobs_store.due_scheduled_users(now=200.0) == []


def test_takes_only_the_latest_row_per_timer():
    """user_logs is append-only: created -> claimed -> fired are THREE rows for one timer.
    Reading anything but the newest row re-fires timers that already fired."""
    conftest.seed_user("u_due_3")
    _append("u_due_3", "t1", {"status": "pending", "due_at": 100.0}, ts=1.0)
    _append("u_due_3", "t1", {"status": "fired", "due_at": 100.0}, ts=2.0)
    assert jobs_store.due_scheduled_users(now=200.0) == []


def test_reclaims_a_claim_whose_lease_expired():
    conftest.seed_user("u_due_4")
    _append("u_due_4", "t1",
            {"status": "claimed", "due_at": 100.0, "claim_expires_at": 150.0})
    assert jobs_store.due_scheduled_users(now=200.0) == ["u_due_4"]


def test_does_not_reclaim_a_live_claim():
    conftest.seed_user("u_due_5")
    _append("u_due_5", "t1",
            {"status": "claimed", "due_at": 100.0, "claim_expires_at": 999.0})
    assert jobs_store.due_scheduled_users(now=200.0) == []


def test_dedupes_a_user_with_two_due_timers_and_respects_limit():
    conftest.seed_user("u_due_6")
    _append("u_due_6", "t1", {"status": "pending", "due_at": 100.0})
    _append("u_due_6", "t2", {"status": "pending", "due_at": 101.0})
    assert jobs_store.due_scheduled_users(now=200.0) == ["u_due_6"]
    assert jobs_store.due_scheduled_users(now=200.0, limit=0) == []


def test_payment_cooldown_defers_timer_but_heartbeat_backoff_does_not():
    uid = "u_due_backoff"
    conftest.seed_user(uid)
    _append(uid, "t1", {"status": "pending", "due_at": 100.0})
    jobs_store.upsert_wake_schedule(uid, payment_cooldown_until=250.0)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_wake_schedule SET proactive_backoff_until=to_timestamp(%s), "
            "proactive_fail_streak=7 WHERE user_id=%s",
            (9_999.0, uid),
        )

    assert jobs_store.due_scheduled_users(now=200.0) == []
    assert jobs_store.due_scheduled_users(now=251.0) == [uid]
