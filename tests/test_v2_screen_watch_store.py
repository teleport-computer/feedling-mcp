"""v2 screen-watch state: LANES/LANE_PRIORITY membership + the two new
v2_wake_schedule columns (next_screen_watch_at, last_screen_watch_frame_id)
round-trip through upsert_wake_schedule/get_wake_schedule, and
due_screen_watch_users mirrors due_heartbeat_users (payment-cooldown
exclusion, NULL-is-not-due)."""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest
import conftest
import db
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import scheduler
from model_api_runtime.v2 import serve_worker


@pytest.fixture(autouse=True)
def _clean():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id LIKE 'u_sw_%'")
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


def test_startup_seed_repairs_null_clocks_and_reaches_a_real_enqueued_job(
    monkeypatch,
):
    """New and pre-existing V2 users must cross the real due query and enqueue.

    The old-user row models production: heartbeat already armed the shared row
    while screen-watch stayed NULL. The new user starts with no row at all.
    """
    now = 500.0
    users = ["u_sw_seed_existing", "u_sw_seed_new"]
    for user_id in users:
        conftest.seed_user(user_id)
        conftest.set_v2_runtime_owner(user_id)
    jobs_store.upsert_wake_schedule(
        "u_sw_seed_existing", next_heartbeat_at=9_000.0
    )
    assert jobs_store.get_wake_schedule("u_sw_seed_new") is None
    assert (
        jobs_store.get_wake_schedule("u_sw_seed_existing")[
            "next_screen_watch_at"
        ]
        is None
    )

    monkeypatch.setattr(
        serve_worker.admin_core,
        "list_runtime_modes",
        lambda: {"db_action_v2": list(users)},
    )
    assert serve_worker._seed_existing_v2_wake_schedules(now=now) == 2
    assert set(jobs_store.due_screen_watch_users(now=now)) == set(users)
    assert (
        jobs_store.get_wake_schedule("u_sw_seed_existing")["next_heartbeat_at"]
        == 9_000.0
    )
    assert serve_worker._seed_existing_v2_wake_schedules(now=now) == 0

    monkeypatch.setattr(serve_worker.time, "time", lambda: now)
    monkeypatch.setattr(jobs_store, "due_heartbeat_users", lambda: [])
    monkeypatch.setattr(
        serve_worker.hosted_config_store,
        "hosted_runtime_v2_enabled_strict",
        lambda _store: True,
    )
    monkeypatch.setattr(
        serve_worker.core_store,
        "get_store",
        lambda _user_id: types.SimpleNamespace(chat_messages=[]),
    )
    monkeypatch.setattr(
        serve_worker.db,
        "frame_list_meta",
        lambda user_id: [
            {"filename": f"{user_id}-fresh.env.json", "ts": now, "app": None}
        ],
    )
    monkeypatch.setattr(
        serve_worker,
        "_wake_decision_for_user",
        lambda _user_id, **_kwargs: {
            "should_wake": True,
            "wake_interval_sec": 7_200,
            "block_reason": "",
        },
    )
    notifications = []
    monkeypatch.setattr(
        serve_worker.core_wake_bus,
        "notify",
        lambda channel, user_id: notifications.append((channel, user_id)),
    )

    result = scheduler.run_scheduler_tick(
        serve_worker._build_scheduler_deps(), now=now
    )
    assert result["screen_watch_enqueued"] == 2
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT user_id,lane,status,reason FROM agent_jobs "
            "WHERE user_id IN (%s,%s) ORDER BY user_id",
            tuple(users),
        ).fetchall()
    assert [(row[0], row[1], row[2], row[3]) for row in rows] == [
        ("u_sw_seed_existing", "screen_watch", "pending", "screen_watch"),
        ("u_sw_seed_new", "screen_watch", "pending", "screen_watch"),
    ]
    assert set(notifications) == {("v2_jobs", user_id) for user_id in users}
