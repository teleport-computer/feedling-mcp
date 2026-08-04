"""v2 screen-watch state: LANES/LANE_PRIORITY membership + the two new
v2_wake_schedule columns (next_screen_watch_at, last_screen_watch_frame_id)
round-trip through upsert_wake_schedule/get_wake_schedule, and
due_screen_watch_users mirrors due_heartbeat_users (payment-cooldown
exclusion, NULL-is-not-due)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest
import conftest
import db
from model_api_runtime.v2 import jobs_store


@pytest.fixture(autouse=True)
def _clean():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_wake_schedule")
    yield


def test_screen_watch_is_a_wake_lane():
    assert "screen_watch" in jobs_store.LANES
    assert jobs_store.LANE_PRIORITY["screen_watch"] == jobs_store.LANE_PRIORITY["heartbeat"]


def test_upsert_and_read_back_the_new_columns():
    conftest.seed_user("u_sw_1")
    jobs_store.upsert_wake_schedule("u_sw_1", next_screen_watch_at=500.0,
                                    last_screen_watch_frame_id="f1")
    row = jobs_store.get_wake_schedule("u_sw_1")
    assert row["last_screen_watch_frame_id"] == "f1"
    assert abs(row["next_screen_watch_at"] - 500.0) < 1.0


def test_partial_upsert_leaves_the_other_columns_alone():
    """None = leave unchanged. Advancing the timer must not wipe the frame id."""
    conftest.seed_user("u_sw_2")
    jobs_store.upsert_wake_schedule("u_sw_2", next_screen_watch_at=500.0,
                                    last_screen_watch_frame_id="f1")
    jobs_store.upsert_wake_schedule("u_sw_2", next_screen_watch_at=900.0)
    row = jobs_store.get_wake_schedule("u_sw_2")
    assert row["last_screen_watch_frame_id"] == "f1"
    assert abs(row["next_screen_watch_at"] - 900.0) < 1.0


def test_due_screen_watch_users_returns_only_due_ones():
    for uid, due in (("u_sw_due", 100.0), ("u_sw_later", 9_000.0)):
        conftest.seed_user(uid)
        jobs_store.upsert_wake_schedule(uid, next_screen_watch_at=due)
    assert jobs_store.due_screen_watch_users(now=500.0) == ["u_sw_due"]


def test_due_screen_watch_users_excludes_payment_cooldown():
    """A dead BYOK key must not keep getting hammered by the screen poller."""
    conftest.seed_user("u_sw_cool")
    jobs_store.upsert_wake_schedule("u_sw_cool", next_screen_watch_at=100.0,
                                    payment_cooldown_until=9_000.0)
    assert jobs_store.due_screen_watch_users(now=500.0) == []


def test_due_screen_watch_users_excludes_dnd_and_restores_when_disabled():
    uid = "u_sw_dnd"
    conftest.seed_user(uid)
    jobs_store.upsert_wake_schedule(uid, next_screen_watch_at=100.0)

    db.set_blob(uid, "proactive_settings", {"dnd": True})
    assert jobs_store.due_screen_watch_users(now=500.0) == []

    db.set_blob(uid, "proactive_settings", {"dnd": False})
    assert jobs_store.due_screen_watch_users(now=500.0) == [uid]


def test_null_next_screen_watch_at_is_not_due():
    conftest.seed_user("u_sw_null")
    jobs_store.upsert_wake_schedule("u_sw_null", next_heartbeat_at=1.0)
    assert jobs_store.due_screen_watch_users(now=500.0) == []
