"""v2_wake_schedule：per-user 下次心跳/采集到期时间 + BYOK 支付冷却的读写。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store

from conftest import seed_user
from incident_guard_reference import legacy_consumer

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 wake-schedule tests require the PostgreSQL test fixture",
)


@pytest.fixture(autouse=True)
def _clean_wake_schedule_table():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_wake_schedule")
    yield


def test_upsert_then_get_round_trips_heartbeat_time():
    seed_user("u_ws_1")
    jobs_store.upsert_wake_schedule("u_ws_1", next_heartbeat_at=100.0)
    row = jobs_store.get_wake_schedule("u_ws_1")
    assert row is not None
    assert row["user_id"] == "u_ws_1"
    assert row["next_heartbeat_at"] is not None
    assert row["next_capture_at"] is None
    assert row["payment_cooldown_until"] is None


def test_get_wake_schedule_returns_none_when_absent():
    seed_user("u_ws_absent")
    assert jobs_store.get_wake_schedule("u_ws_absent") is None


def test_partial_upsert_leaves_other_columns_unchanged():
    seed_user("u_ws_2")
    jobs_store.upsert_wake_schedule("u_ws_2", next_heartbeat_at=100.0)
    first = jobs_store.get_wake_schedule("u_ws_2")
    assert first["next_heartbeat_at"] is not None

    # Only touching payment_cooldown_until must not null out next_heartbeat_at.
    jobs_store.upsert_wake_schedule("u_ws_2", payment_cooldown_until=200.0)
    second = jobs_store.get_wake_schedule("u_ws_2")
    assert second["payment_cooldown_until"] is not None
    assert second["next_heartbeat_at"] == first["next_heartbeat_at"]


def test_due_heartbeat_users_excludes_future_and_includes_due():
    seed_user("u_ws_due")
    seed_user("u_ws_future")
    now = time.time()
    jobs_store.upsert_wake_schedule("u_ws_due", next_heartbeat_at=now - 10)
    jobs_store.upsert_wake_schedule("u_ws_future", next_heartbeat_at=now + 1000)

    due = jobs_store.due_heartbeat_users(now=now)
    assert "u_ws_due" in due
    assert "u_ws_future" not in due


def test_due_heartbeat_users_excludes_users_in_payment_cooldown():
    seed_user("u_ws_cooldown")
    now = time.time()
    jobs_store.upsert_wake_schedule(
        "u_ws_cooldown",
        next_heartbeat_at=now - 10,
        payment_cooldown_until=now + 1000,
    )

    due = jobs_store.due_heartbeat_users(now=now)
    assert "u_ws_cooldown" not in due


def test_due_heartbeat_users_includes_user_whose_cooldown_has_expired():
    seed_user("u_ws_cooldown_over")
    now = time.time()
    jobs_store.upsert_wake_schedule(
        "u_ws_cooldown_over",
        next_heartbeat_at=now - 10,
        payment_cooldown_until=now - 5,
    )

    due = jobs_store.due_heartbeat_users(now=now)
    assert "u_ws_cooldown_over" in due


def _append_user_row(user_id: str, msg_id: str, *, source: str = "chat") -> int:
    db.chat_append_strict(
        user_id,
        msg_id,
        time.time(),
        {
            "id": msg_id,
            "role": "user",
            "source": source,
            "body_ct": "test-ciphertext",
        },
        5000,
    )
    return int(db.chat_seq_for_msg_id(user_id, msg_id))


def test_self_wake_limit_replay_and_user_reset_match_v1_semantics(monkeypatch):
    """V1 and V2 both accept 3 AI self-schedules, reject the fourth, and
    resume after genuine user input. Maintenance traffic never resets it."""
    uid = "u_ws_self_wake_limit"
    seed_user(uid)
    legacy = legacy_consumer()
    previous_streak = legacy._self_wake_streak
    monkeypatch.setattr(legacy, "MAX_CONSECUTIVE_SELF_WAKES", 3)
    legacy._self_wake_streak = 0

    outcomes = []
    try:
        for index in range(1, 5):
            v1_accepted = not legacy._self_wake_loop_tripped()
            outcome = jobs_store.reserve_self_wake(
                uid,
                effect_id=f"self-wake-{index}",
                max_consecutive=3,
            )
            outcomes.append(outcome)
            assert outcome["accepted"] is v1_accepted
            if v1_accepted:
                legacy._note_self_wake()

        replay = jobs_store.reserve_self_wake(
            uid,
            effect_id="self-wake-4",
            max_consecutive=3,
        )
        assert replay == {
            "accepted": False,
            "streak": 3,
            "reason": "self_wake_loop_guard",
            "replayed": True,
        }

        _append_user_row(uid, "maintenance", source="resident_maintenance")
        still_blocked = jobs_store.reserve_self_wake(
            uid,
            effect_id="self-wake-5",
            max_consecutive=3,
        )
        assert still_blocked["accepted"] is (
            not legacy._self_wake_loop_tripped()
        )
        assert still_blocked["accepted"] is False
        assert still_blocked["streak"] == 3

        _append_user_row(uid, "genuine-user")
        legacy._reset_proactive_idle_guard()
        resumed = jobs_store.reserve_self_wake(
            uid,
            effect_id="self-wake-6",
            max_consecutive=3,
        )
        assert resumed["accepted"] is (
            not legacy._self_wake_loop_tripped()
        )
        assert resumed["accepted"] is True
        assert resumed["streak"] == 1
    finally:
        legacy._self_wake_streak = previous_streak

    assert [row["accepted"] for row in outcomes] == [True, True, True, False]
    assert [row["streak"] for row in outcomes] == [1, 2, 3, 3]


def test_heartbeat_and_event_scheduling_do_not_consume_self_wake_budget():
    uid = "u_ws_self_wake_lane_scope"
    seed_user(uid)
    for index in range(1, 4):
        assert jobs_store.reserve_self_wake(
            uid,
            effect_id=f"self-wake-{index}",
            max_consecutive=3,
        )["accepted"]
    before = jobs_store.get_wake_schedule(uid)

    jobs_store.upsert_wake_schedule(uid, next_heartbeat_at=time.time() + 300)
    after_heartbeat = jobs_store.get_wake_schedule(uid)
    jobs_store.upsert_wake_schedule(
        uid,
        next_screen_watch_at=time.time() + 600,
        last_screen_watch_frame_id="event-frame",
    )
    after_event = jobs_store.get_wake_schedule(uid)

    assert before["self_wake_streak"] == 3
    assert after_heartbeat["self_wake_streak"] == 3
    assert after_event["self_wake_streak"] == 3
