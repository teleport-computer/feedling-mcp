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
