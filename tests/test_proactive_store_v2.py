from __future__ import annotations

import sys
import uuid
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from proactive.runtime_v2 import RuntimeSpineV2, TurnOutcomeV2, WakeEventV2, merge_wakes_v2
from proactive.scheduled_wake_v2 import DBScheduledWakeStoreV2, SCHEDULED_FIRED, ScheduledWakeServiceV2
from proactive.store_v2 import (
    BACKGROUND_COMPLETED,
    BACKGROUND_JOB_STREAM_V2,
    DBBackgroundLeaseRegistryV2,
    DBBackgroundJobStoreV2,
    DBProactiveSettingsStoreV2,
    DBTurnLeaseRegistryV2,
    DBTurnStoreV2,
    DBWakeInboxV2,
    SETTINGS_KIND_V2,
    TURN_COMPLETED,
    TURN_STALE_RECOVERED,
    TURN_STREAM_V2,
)
import db

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 substrate tests require the PostgreSQL test fixture",
)


def _switches(**overrides):
    doc = {
        "ambient": True,
        "scheduled": True,
        "reminders_delivery": True,
        "dream_enabled": True,
        "capture_enabled": True,
        "screen_watch_enabled": True,
        "photo_wake_enabled": True,
        "arrival_wake_enabled": True,
        "unlock_wake_enabled": True,
    }
    doc.update(overrides)
    return doc


def _uid() -> str:
    return "usr_v2_" + uuid.uuid4().hex[:12]


def test_db_turn_lease_allows_only_one_worker_to_win():
    uid = _uid()
    seed_user(uid)
    leases = DBTurnLeaseRegistryV2()

    first = leases.try_acquire_user(uid, owner_id="worker-a", now=10.0, ttl_sec=30.0)
    second = leases.try_acquire_user(uid, owner_id="worker-b", now=11.0, ttl_sec=30.0)

    assert first is not None
    assert first.user_id == uid
    assert first.scope == f"turn:{uid}"
    assert second is None


def test_db_turn_lease_reclaims_expired_owner_and_rejects_old_release():
    uid = _uid()
    seed_user(uid)
    leases = DBTurnLeaseRegistryV2()

    first = leases.try_acquire_user(uid, owner_id="worker-a", now=10.0, ttl_sec=5.0)
    reclaimed = leases.try_acquire_user(uid, owner_id="worker-b", now=15.1, ttl_sec=5.0)

    assert first is not None
    assert reclaimed is not None
    assert reclaimed.owner_id == "worker-b"
    assert reclaimed.lease_id != first.lease_id
    assert leases.release(first) is False
    assert leases.release(reclaimed) is True
    assert leases.current(f"turn:{uid}", now=16.0) is None


def test_db_background_lease_is_independent_and_reclaimable():
    uid = _uid()
    seed_user(uid)
    leases = DBBackgroundLeaseRegistryV2()

    first = leases.try_acquire_job("bg_1", user_id=uid, owner_id="worker-a", now=20.0, ttl_sec=5.0)
    blocked = leases.try_acquire_job("bg_1", user_id=uid, owner_id="worker-b", now=24.0, ttl_sec=5.0)
    reclaimed = leases.try_acquire_job("bg_1", user_id=uid, owner_id="worker-b", now=25.1, ttl_sec=5.0)

    assert first is not None
    assert blocked is None
    assert reclaimed is not None
    assert reclaimed.scope == "background:bg_1"
    assert leases.release(first) is False
    assert leases.current_for_job(uid, "bg_1", now=26.0) == reclaimed


def test_db_wake_inbox_round_trips_and_merges_persisted_wakes():
    uid = _uid()
    seed_user(uid)
    inbox = DBWakeInboxV2()
    spine = RuntimeSpineV2(
        inbox=inbox,
        settings_resolver=lambda _user_id: {"timezone": "Asia/Tokyo"},
        merge_window_sec=2.0,
    )

    spine.submit(WakeEventV2(user_id=uid, source="heartbeat", trigger="heartbeat", created_at=100.0))
    spine.submit(WakeEventV2(user_id=uid, source="heartbeat", trigger="heartbeat", created_at=100.3))
    spine.submit(
        WakeEventV2(
            user_id=uid,
            source="perception_event",
            trigger="arrived_at_anchor",
            created_at=100.5,
            change_digest="anchor: cafe",
        )
    )

    assert spine.drain_context(uid, now=101.0) is None
    ctx = spine.drain_context(uid, now=103.0)

    assert ctx is not None
    assert ctx.trigger == "arrived_at_anchor"
    assert ctx.merged_triggers == ("heartbeat",)
    assert len(ctx.wake_ids) == 2
    assert "anchor: cafe" in ctx.change_digest
    assert ctx.timezone == "Asia/Tokyo"
    assert inbox.pending_events(uid) == []


def test_db_wake_inbox_latency_sensitive_flushes_persisted_queue():
    uid = _uid()
    seed_user(uid)
    inbox = DBWakeInboxV2()
    spine = RuntimeSpineV2(inbox=inbox, merge_window_sec=10.0)

    spine.submit(WakeEventV2(user_id=uid, source="heartbeat", trigger="heartbeat", created_at=200.0))
    spine.submit(
        WakeEventV2(
            user_id=uid,
            source="user_message",
            trigger="user_message",
            created_at=200.2,
            latency_sensitive=True,
            manual=True,
        )
    )

    ctx = spine.drain_context(uid, now=200.3)

    assert ctx is not None
    assert ctx.trigger == "user_message"
    assert ctx.merged_triggers == ("heartbeat",)
    assert ctx.manual is True


def test_db_turn_store_recovers_stale_running_turn_without_old_owner_completion():
    uid = _uid()
    seed_user(uid)
    leases = DBTurnLeaseRegistryV2()
    turn_store = DBTurnStoreV2()
    context = merge_wakes_v2([
        WakeEventV2(user_id=uid, source="user_message", trigger="user_message", created_at=300.0)
    ])

    old_lease = leases.try_acquire_user(uid, owner_id="worker-a", now=300.0, ttl_sec=5.0)
    assert old_lease is not None
    stale_turn = turn_store.start_turn(uid, context, old_lease, now=300.0, turn_id="turn_stale")
    assert stale_turn is not None
    duplicate_start = turn_store.start_turn(uid, context, old_lease, now=300.1, turn_id="turn_duplicate")
    assert duplicate_start is None

    recovered = turn_store.recover_stale_running(uid, now=306.0)
    new_lease = leases.try_acquire_user(uid, owner_id="worker-b", now=306.1, ttl_sec=5.0)
    stale_start = turn_store.start_turn(uid, context, old_lease, now=306.2, turn_id="turn_stale_owner")
    old_completion = turn_store.complete_turn(
        uid,
        stale_turn.turn_id,
        old_lease,
        outcome=TurnOutcomeV2(messages=("late duplicate",)),
        delivery_committed=True,
        now=306.3,
    )

    assert len(recovered) == 1
    assert recovered[0].status == TURN_STALE_RECOVERED
    assert new_lease is not None
    assert stale_start is None
    assert old_completion is None

    new_turn = turn_store.start_turn(uid, context, new_lease, now=306.4, turn_id="turn_recovered")
    assert new_turn is not None
    completed = turn_store.complete_turn(
        uid,
        new_turn.turn_id,
        new_lease,
        outcome=TurnOutcomeV2(actions=({"type": "sleep"},)),
        now=306.5,
    )

    assert completed is not None
    assert completed.status == TURN_COMPLETED
    turns = db.log_read_all(uid, TURN_STREAM_V2)
    assert [turn["status"] for turn in turns] == [TURN_STALE_RECOVERED, TURN_COMPLETED]


def test_db_turn_store_records_actions_as_v2_action_stream():
    uid = _uid()
    seed_user(uid)
    leases = DBTurnLeaseRegistryV2()
    turn_store = DBTurnStoreV2()
    context = merge_wakes_v2([
        WakeEventV2(user_id=uid, source="perception_event", trigger="arrived_at_anchor", created_at=400.0)
    ])

    lease = leases.try_acquire_user(uid, owner_id="worker-a", now=400.0, ttl_sec=30.0)
    assert lease is not None
    turn = turn_store.start_turn(uid, context, lease, now=400.0, turn_id="turn_actions")
    assert turn is not None

    records = turn_store.record_actions(
        uid,
        turn.turn_id,
        (
            {"type": "send_message", "text": "hello"},
            {"type": "schedule_wake", "at": "2026-06-20T09:00:00+08:00", "tz": "Asia/Shanghai"},
        ),
        now=400.1,
    )
    listed = turn_store.list_actions(uid, turn_id=turn.turn_id)

    assert [record.action_type for record in records] == ["send_message", "schedule_wake"]
    assert [record.action_type for record in listed] == ["send_message", "schedule_wake"]
    assert listed[0].doc["turn_id"] == turn.turn_id
    assert listed[0].doc["action"]["text"] == "hello"


def test_db_background_job_store_lifecycle_and_duplicate_completion_guard():
    uid = _uid()
    seed_user(uid)
    jobs = DBBackgroundJobStoreV2()
    leases = DBBackgroundLeaseRegistryV2()

    job = jobs.create_job(
        uid,
        {"tool": "memory.fetch"},
        turn_id="turn_bg",
        wake_ids=("wake_bg",),
        now=500.0,
        job_id="bg_db",
    )
    lease = leases.try_acquire_job(job.job_id, user_id=uid, owner_id="worker-a", now=500.1, ttl_sec=30.0)
    assert lease is not None

    running = jobs.mark_running(uid, job.job_id, lease, now=500.1)
    completed = jobs.complete_job(uid, job.job_id, lease, {"ok": True}, now=500.2)
    duplicate = jobs.complete_job(uid, job.job_id, lease, {"ok": True}, now=500.3)
    rows = db.log_read_all(uid, BACKGROUND_JOB_STREAM_V2)

    assert running is not None
    assert completed is not None
    assert completed.status == BACKGROUND_COMPLETED
    assert completed.request == {"tool": "memory.fetch"}
    assert duplicate is None
    assert rows[-1]["status"] == BACKGROUND_COMPLETED
    assert rows[-1]["result"] == {"ok": True}


def test_db_settings_store_uses_v2_shape_with_legacy_fallback():
    uid = _uid()
    seed_user(uid)
    settings_store = DBProactiveSettingsStoreV2()
    db.set_blob(uid, "proactive_settings", {
        "enabled": False,
        "dnd": True,
        "user_state": "away",
        "ai_state": "waiting",
    })

    fallback = settings_store.load(uid)
    saved = settings_store.save(uid, {"switches": {"ambient": True, "scheduled": False}})
    doc = db.get_blob(uid, SETTINGS_KIND_V2)

    assert fallback.switches() == _switches(ambient=False, reminders_delivery=False)
    assert saved.switches() == _switches(scheduled=False, reminders_delivery=False)
    assert doc["kind"] == SETTINGS_KIND_V2
    assert doc["switches"] == saved.switches()
    assert "enabled" not in doc
    assert "dnd" not in doc
    assert "user_state" not in doc
    assert "ai_state" not in doc


def test_db_scheduled_wake_survives_restart_and_fires_once():
    uid = _uid()
    seed_user(uid)
    first_service = ScheduledWakeServiceV2(DBScheduledWakeStoreV2(), owner_id="worker-a")
    first_service.apply_turn_actions(
        uid,
        [{"type": "schedule_wake", "at": "2026-06-20T09:00:00", "tz": "UTC", "note": "db check"}],
        now=1.0,
    )
    restarted_service = ScheduledWakeServiceV2(DBScheduledWakeStoreV2(), owner_id="worker-b")
    competing_service = ScheduledWakeServiceV2(DBScheduledWakeStoreV2(), owner_id="worker-c")
    spine = RuntimeSpineV2(merge_window_sec=0.0)

    first = restarted_service.fire_due_timers(uid, settings={}, now=2_000_000_000.0, submit_wake=spine.submit)
    second = competing_service.fire_due_timers(uid, settings={}, now=2_000_000_000.1, submit_wake=spine.submit)
    records = DBScheduledWakeStoreV2().list_records(uid)

    assert len(first) == 1
    assert second == ()
    assert records[0].status == SCHEDULED_FIRED
    assert spine.drain_context(uid, now=2_000_000_000.0).scheduled_note == "db check"
