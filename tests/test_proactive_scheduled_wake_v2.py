from __future__ import annotations

from datetime import datetime, timezone
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from proactive import proactive_core, scheduled_wake_v2, store_v2
from proactive.controls_v2 import resolve_settings_v2
from proactive.runtime_v2 import RuntimeSpineV2, TurnOutcomeV2, TurnRunnerV2, WakeEventV2
from proactive.scheduled_wake_v2 import (
    InMemoryScheduledWakeStoreV2,
    SCHEDULED_BLOCKED,
    SCHEDULED_CANCELED,
    SCHEDULED_CLAIMED,
    SCHEDULED_FIRED,
    SCHEDULED_PENDING,
    ScheduledWakeServiceV2,
    schedule_instant_v2,
)


class _RouteStore:
    user_id = "u1"

    def __init__(self):
        self.jobs = []

    def append_proactive_job(self, job):
        self.jobs.append(job)
        return job


def _service(*, cap: int = 20, claim_ttl: float = 60.0, self_wake_min_lead: float = 300.0):
    store = InMemoryScheduledWakeStoreV2()
    return store, ScheduledWakeServiceV2(
        store,
        pending_cap=cap,
        claim_ttl_sec=claim_ttl,
        self_wake_min_lead_sec=self_wake_min_lead,
        owner_id="worker-a",
    )


def _patch_scheduled_route(monkeypatch, *, now: float):
    scheduled_store = InMemoryScheduledWakeStoreV2()

    class _SettingsStore:
        def load(self, user_id):
            return resolve_settings_v2({"timezone": "UTC"})

    monkeypatch.setattr(scheduled_wake_v2, "DBScheduledWakeStoreV2", lambda: scheduled_store)
    monkeypatch.setattr(store_v2, "DBProactiveSettingsStoreV2", lambda: _SettingsStore())
    monkeypatch.setattr(scheduled_wake_v2.time, "time", lambda: now)
    return scheduled_store


def test_schedule_action_persists_wall_time_timezone_note_and_origin_refs():
    store, service = _service()

    result = service.apply_turn_actions(
        "u1",
        [{
            "type": "schedule_wake",
            "at": "2026-06-20T09:30:00+08:00",
            "tz": "Asia/Shanghai",
            "note": "check whether she left for the hospital",
            "repeat": "daily",
            "origin_refs": ["msg_1", "msg_2"],
        }],
        settings=resolve_settings_v2({"timezone": "Asia/Shanghai"}),
        turn_id="turn_1",
        wake_ids=("wake_1",),
        now=10.0,
    )[0]
    record = store.list_records("u1")[0]

    assert result.status == "scheduled"
    assert result.timer_id == record.timer_id
    assert result.next_trigger_at == "2026-06-20T09:30:00"
    assert result.timezone == "Asia/Shanghai"
    assert record.status == SCHEDULED_PENDING
    assert record.at == "2026-06-20T09:30:00"
    assert record.timezone == "Asia/Shanghai"
    assert record.note == "check whether she left for the hospital"
    assert record.repeat == "daily"
    assert record.repeat_series_id == record.timer_id
    assert record.origin_refs == ("msg_1", "msg_2")
    assert service.agent_context_for_user("u1")["pending_count"] == 1
    assert service.agent_context_for_user("u1")["pending_cap"] == 20
    assert service.agent_context_for_user("u1")["timers"][0]["repeat"] == "daily"


def test_schedule_dedupes_same_due_and_repeat_but_keeps_distinct_repeats():
    store, service = _service()
    at = "2026-06-20T09:30:00Z"

    daily = service.apply_turn_actions(
        "u1",
        [{"type": "schedule_wake", "at": at, "tz": "UTC", "repeat": "daily"}],
        now=1.0,
    )[0]
    daily_duplicate = service.apply_turn_actions(
        "u1",
        [{"type": "schedule_wake", "at": at, "tz": "UTC", "repeat": "daily"}],
        now=2.0,
    )[0]
    weekly = service.apply_turn_actions(
        "u1",
        [{"type": "schedule_wake", "at": at, "tz": "UTC", "repeat": "weekly"}],
        now=3.0,
    )[0]

    pending = [record for record in store.list_records("u1") if record.pending_like]
    due_at = next(record.due_at for record in pending if record.timer_id == daily.timer_id)
    assert daily_duplicate.timer_id == daily.timer_id
    assert daily_duplicate.reason == "already_scheduled"
    assert weekly.timer_id != daily.timer_id
    assert {(record.due_at, record.repeat) for record in pending} == {
        (due_at, "daily"),
        (due_at, "weekly"),
    }


def test_schedule_rejects_unsupported_repeat():
    store, service = _service()

    result = service.apply_turn_actions(
        "u1",
        [{"type": "schedule_wake", "at": "2026-06-20T09:30:00Z", "repeat": "monthly"}],
        now=1.0,
    )[0]

    assert result.status == "invalid"
    assert result.reason == "invalid_repeat"
    assert store.list_records("u1") == []


def test_schedule_instant_keeps_event_timezone_wall_clock_across_dst():
    wall, tz, due_at = schedule_instant_v2("2026-11-01T01:30:00", "America/New_York")

    assert wall == "2026-11-01T01:30:00"
    assert tz == "America/New_York"
    assert due_at > 0


def test_schedule_instant_accepts_english_and_chinese_relative_times():
    now = 1_800_000_000.0

    for value in ("in 1 minute", "1m", "1 分钟后", "一分钟后"):
        wall, tz, due_at = schedule_instant_v2(value, "Asia/Shanghai", now=now)

        assert wall == datetime.fromtimestamp(now + 60.0, timezone.utc).astimezone(
            scheduled_wake_v2.ZoneInfo("Asia/Shanghai")
        ).replace(tzinfo=None).isoformat()
        assert tz == "Asia/Shanghai"
        assert due_at == now + 60.0


def test_schedule_action_uses_turn_time_for_relative_schedule():
    store, service = _service()
    now = 1_800_000_000.0

    result = service.apply_turn_actions(
        "u1",
        [{"type": "schedule_wake", "at": "一分钟后", "tz": "Asia/Shanghai"}],
        now=now,
    )[0]
    record = store.list_records("u1")[0]

    assert result.status == "scheduled"
    assert record.due_at == now + 60.0


def test_schedule_action_clamps_near_self_wake_to_min_lead():
    store, service = _service(self_wake_min_lead=300.0)
    now = 1_800_000_000.0
    requested = datetime.fromtimestamp(now + 60.0, timezone.utc).isoformat()

    result = service.apply_turn_actions(
        "u1",
        [{"type": "schedule_wake", "at": requested, "tz": "UTC"}],
        self_wake=True,
        now=now,
    )[0]
    record = store.list_records("u1")[0]
    spine = RuntimeSpineV2(merge_window_sec=0.0)

    assert result.status == "scheduled"
    assert result.reason == "self_wake_min_lead_clamped"
    assert result.as_dict()["reason"] == "self_wake_min_lead_clamped"
    assert record.due_at == now + 300.0
    assert record.at == datetime.fromtimestamp(now + 300.0, timezone.utc).replace(tzinfo=None).isoformat()
    assert service.fire_due_timers("u1", settings={}, now=now + 299.0, submit_wake=spine.submit) == ()


def test_schedule_action_does_not_clamp_without_self_wake_flag():
    store, service = _service(self_wake_min_lead=300.0)
    now = 1_800_000_000.0
    requested = datetime.fromtimestamp(now + 60.0, timezone.utc).isoformat()

    result = service.apply_turn_actions(
        "u1",
        [{"type": "schedule_wake", "at": requested, "tz": "UTC"}],
        now=now,
    )[0]
    record = store.list_records("u1")[0]

    assert result.status == "scheduled"
    assert result.reason == ""
    assert record.due_at == now + 60.0
    assert record.at == datetime.fromtimestamp(now + 60.0, timezone.utc).replace(tzinfo=None).isoformat()


def test_schedule_action_keeps_future_self_wake_unchanged():
    store, service = _service(self_wake_min_lead=300.0)
    now = 1_800_000_000.0
    requested = datetime.fromtimestamp(now + 600.0, timezone.utc).isoformat()

    result = service.apply_turn_actions(
        "u1",
        [{"type": "schedule_wake", "at": requested, "tz": "UTC"}],
        self_wake=True,
        now=now,
    )[0]
    record = store.list_records("u1")[0]

    assert result.status == "scheduled"
    assert result.reason == ""
    assert record.due_at == now + 600.0
    assert record.at == datetime.fromtimestamp(now + 600.0, timezone.utc).replace(tzinfo=None).isoformat()


def test_scheduled_actions_route_self_wake_payload_controls_min_lead(monkeypatch):
    now = 1_800_000_000.0
    scheduled_store = _patch_scheduled_route(monkeypatch, now=now)
    requested = datetime.fromtimestamp(now + 60.0, timezone.utc).isoformat()

    body, status = proactive_core.scheduled_actions(
        _RouteStore(),
        {
            "actions": [{"type": "schedule_wake", "at": requested, "tz": "UTC"}],
            "self_wake": True,
        },
    )

    record = scheduled_store.list_records("u1")[0]
    assert status == 200
    assert body["results"][0]["reason"] == "self_wake_min_lead_clamped"
    assert record.due_at == now + 300.0


def test_scheduled_actions_route_does_not_clamp_without_self_wake(monkeypatch):
    now = 1_800_000_000.0
    scheduled_store = _patch_scheduled_route(monkeypatch, now=now)
    requested = datetime.fromtimestamp(now + 60.0, timezone.utc).isoformat()

    body, status = proactive_core.scheduled_actions(
        _RouteStore(),
        {"actions": [{"type": "schedule_wake", "at": requested, "tz": "UTC"}]},
    )

    record = scheduled_store.list_records("u1")[0]
    assert status == 200
    assert "reason" not in body["results"][0]
    assert record.due_at == now + 60.0


def test_pending_cap_eviction_is_reported_and_visible_to_agent():
    store, service = _service(cap=2)

    first = service.apply_turn_actions("u1", [{"type": "schedule_wake", "at": "2026-06-20T09:00:00", "tz": "UTC"}], now=1.0)[0]
    second = service.apply_turn_actions("u1", [{"type": "schedule_wake", "at": "2026-06-20T10:00:00", "tz": "UTC"}], now=2.0)[0]
    third = service.apply_turn_actions("u1", [{"type": "schedule_wake", "at": "2026-06-20T11:00:00", "tz": "UTC"}], now=3.0)[0]
    records = {record.timer_id: record for record in store.list_records("u1")}

    assert second.status == "scheduled"
    assert third.evicted_timer_ids == (first.timer_id,)
    assert records[first.timer_id].status == SCHEDULED_CANCELED
    assert records[first.timer_id].cancel_reason == "pending_cap_evicted"
    context = service.agent_context_for_user("u1")
    assert context["pending_count"] == 2
    assert [timer["wake_id"] for timer in context["timers"]] == [second.timer_id, third.timer_id]


def test_cancel_wake_prevents_future_fire():
    store, service = _service()
    scheduled = service.apply_turn_actions("u1", [{"type": "schedule_wake", "at": "2026-06-20T09:00:00", "tz": "UTC"}], now=1.0)[0]
    canceled = service.apply_turn_actions("u1", [{"type": "cancel_wake", "wake_id": scheduled.timer_id, "reason": "plans_changed"}], now=2.0)[0]
    spine = RuntimeSpineV2(merge_window_sec=0.0)

    fired = service.fire_due_timers("u1", settings={}, now=2_000_000_000.0, submit_wake=spine.submit)

    assert canceled.status == "canceled"
    assert canceled.reason == "plans_changed"
    assert fired == ()
    assert store.list_records("u1")[0].status == SCHEDULED_CANCELED


def test_daily_repeat_is_mechanical_and_exposes_new_pending_wake_id():
    store, service = _service(self_wake_min_lead=10_000_000.0)
    scheduled = service.apply_turn_actions(
        "u1",
        [{
            "type": "schedule_wake",
            "at": "2026-06-20T09:00:00Z",
            "tz": "UTC",
            "repeat": "daily",
            "note": "drink water",
        }],
        now=1.0,
    )[0]
    original = store.list_records("u1")[0]
    for offset_hours in range(1, 11):
        service.apply_turn_actions(
            "u1",
            [{
                "type": "schedule_wake",
                "at": datetime.fromtimestamp(
                    original.due_at + offset_hours * 3600.0,
                    timezone.utc,
                ).isoformat(),
                "tz": "UTC",
            }],
            now=1.0 + offset_hours,
        )
    spine = RuntimeSpineV2(merge_window_sec=0.0)

    fired = service.fire_due_timers(
        "u1",
        settings={},
        now=original.due_at,
        submit_wake=spine.submit,
    )
    records = {record.timer_id: record for record in store.list_records("u1")}
    context = service.agent_context_for_user("u1")

    assert len(fired) == 1
    assert records[scheduled.timer_id].status == SCHEDULED_FIRED
    assert fired[0].next_timer_id != scheduled.timer_id
    next_record = records[fired[0].next_timer_id]
    assert next_record.status == SCHEDULED_PENDING
    # The huge self-wake lead does not clamp this: recurrence is a scheduler
    # transition, not an agent-authored schedule_wake call.
    assert next_record.due_at == original.due_at + 86400.0
    assert next_record.repeat_series_id == original.repeat_series_id
    assert context["pending_count"] == 11
    renewed_context = next(
        timer for timer in context["timers"] if timer["wake_id"] == next_record.timer_id
    )
    assert renewed_context["repeat"] == "daily"


def test_weekly_repeat_uses_fixed_seven_day_interval():
    store, service = _service()
    service.apply_turn_actions(
        "u1",
        [{
            "type": "schedule_wake",
            "at": "2026-06-20T09:00:00Z",
            "tz": "UTC",
            "repeat": "weekly",
        }],
        now=1.0,
    )
    original = store.list_records("u1")[0]

    fired = service.fire_due_timers(
        "u1",
        settings={},
        now=original.due_at,
        submit_wake=RuntimeSpineV2(merge_window_sec=0.0).submit,
    )
    next_record = next(record for record in store.list_records("u1") if record.pending_like)

    assert next_record.timer_id == fired[0].next_timer_id
    assert next_record.due_at == original.due_at + 604800.0
    assert next_record.repeat == "weekly"


def test_canceling_fired_repeat_id_cancels_next_and_stops_chain():
    store, service = _service()
    scheduled = service.apply_turn_actions(
        "u1",
        [{
            "type": "schedule_wake",
            "at": "2026-06-20T09:00:00Z",
            "tz": "UTC",
            "repeat": "daily",
        }],
        now=1.0,
    )[0]
    original = store.list_records("u1")[0]
    fired = service.fire_due_timers(
        "u1",
        settings={},
        now=original.due_at,
        submit_wake=RuntimeSpineV2(merge_window_sec=0.0).submit,
    )[0]

    canceled = service.apply_turn_actions(
        "u1",
        [{"type": "cancel_wake", "wake_id": scheduled.timer_id}],
        now=original.due_at + 1.0,
    )[0]
    records = {record.timer_id: record for record in store.list_records("u1")}
    later = service.fire_due_timers(
        "u1",
        settings={},
        now=original.due_at + 86400.0,
        submit_wake=RuntimeSpineV2(merge_window_sec=0.0).submit,
    )

    assert canceled.status == "canceled"
    assert records[fired.next_timer_id].status == SCHEDULED_CANCELED
    assert records[fired.next_timer_id].repeat_canceled_at > 0
    assert later == ()
    assert not any(record.pending_like for record in records.values())


def test_due_timer_fires_once_across_workers_and_survives_service_restart():
    store, service_a = _service(claim_ttl=30.0)
    service_b = ScheduledWakeServiceV2(store, owner_id="worker-b")
    service_a.apply_turn_actions("u1", [{"type": "schedule_wake", "at": "2026-06-20T09:00:00", "tz": "UTC", "note": "check in"}], now=1.0)
    spine = RuntimeSpineV2(merge_window_sec=0.0)

    first = service_a.fire_due_timers("u1", settings={}, now=2_000_000_000.0, submit_wake=spine.submit)
    second = service_b.fire_due_timers("u1", settings={}, now=2_000_000_000.1, submit_wake=spine.submit)
    ctx = spine.drain_context("u1", now=2_000_000_000.0)
    record = store.list_records("u1")[0]

    assert len(first) == 1
    assert second == ()
    assert record.status == SCHEDULED_FIRED
    assert ctx is not None
    assert ctx.trigger == "scheduled_wake"
    assert ctx.scheduled_note == "check in"


def test_fired_timer_records_the_v2_job_that_will_deliver_its_note():
    store, service = _service()
    service.apply_turn_actions(
        "u1",
        [{
            "type": "schedule_wake",
            "at": "2026-06-20T09:00:00",
            "tz": "UTC",
            "note": "提醒我喝水",
        }],
        now=1.0,
    )

    fired = service.fire_due_timers(
        "u1",
        settings={},
        now=2_000_000_000.0,
        submit_wake=lambda _event: SimpleNamespace(accepted=True, job_id=42),
    )

    assert len(fired) == 1
    record = store.list_records("u1")[0]
    assert record.status == SCHEDULED_FIRED
    assert record.fired_job_id == 42
    assert record.note == "提醒我喝水"


def test_claimed_due_timer_is_reclaimed_after_claim_ttl():
    store, service_a = _service(claim_ttl=5.0)
    service_b = ScheduledWakeServiceV2(store, claim_ttl_sec=5.0, owner_id="worker-b")
    service_a.apply_turn_actions("u1", [{"type": "schedule_wake", "at": "2026-06-20T09:00:00", "tz": "UTC"}], now=1.0)
    timer_id = store.list_records("u1")[0].timer_id
    claimed = store.claim_due("u1", timer_id, owner_id="worker-a", now=2_000_000_000.0, ttl_sec=5.0)
    spine = RuntimeSpineV2(merge_window_sec=0.0)

    blocked = service_b.fire_due_timers("u1", settings={}, now=2_000_000_004.0, submit_wake=spine.submit)
    reclaimed = service_b.fire_due_timers("u1", settings={}, now=2_000_000_006.0, submit_wake=spine.submit)

    assert claimed is not None
    assert blocked == ()
    assert len(reclaimed) == 1
    assert store.list_records("u1")[0].status == SCHEDULED_FIRED


def test_scheduled_wake_is_not_suppressed_when_ambient_is_off():
    store, service = _service()
    settings = resolve_settings_v2({"switches": {"ambient": False, "scheduled": True}})
    service.apply_turn_actions("u1", [{"type": "schedule_wake", "at": "2026-06-20T09:00:00", "tz": "UTC"}], settings=settings, now=1.0)
    spine = RuntimeSpineV2(settings_resolver=lambda _uid: settings, merge_window_sec=0.0)

    fired = service.fire_due_timers("u1", settings=settings, now=2_000_000_000.0, submit_wake=spine.submit)
    ctx = spine.drain_context("u1", now=2_000_000_000.0)

    assert len(fired) == 1
    assert ctx is not None
    assert ctx.trigger == "scheduled_wake"
    assert ctx.switches["ambient"] is False


def test_scheduled_off_due_timer_becomes_transparency_background_result():
    store, service = _service()
    service.apply_turn_actions("u1", [{"type": "schedule_wake", "at": "2026-06-20T09:00:00", "tz": "UTC", "note": "hospital"}], now=1.0)
    settings = resolve_settings_v2({"switches": {"scheduled": False}})
    spine = RuntimeSpineV2(settings_resolver=lambda _uid: settings, merge_window_sec=0.0)

    fired = service.fire_due_timers("u1", settings=settings, now=2_000_000_000.0, submit_wake=spine.submit)
    ctx = spine.drain_context("u1", now=2_000_000_000.0)
    record = store.list_records("u1")[0]

    assert fired[0].status == "blocked"
    assert fired[0].reason == "scheduled_disabled"
    assert fired[0].transparency_wake_id
    assert record.status == SCHEDULED_BLOCKED
    assert record.block_reason == "scheduled_disabled"
    assert ctx is not None
    assert ctx.trigger == "scheduled_transparency"
    assert ctx.background_payloads[0]["reason"] == "scheduled_disabled"
    assert ctx.background_payloads[0]["timer"]["note"] == "hospital"


def test_blocked_timer_with_undelivered_transparency_is_retried_not_dropped():
    """If the transparency wake can't be enqueued (submit failure), the timer must
    not be marked terminally blocked with the explanation silently dropped — it
    stays claimed and retries once the submit path recovers."""
    store, service = _service(claim_ttl=5.0)
    service.apply_turn_actions(
        "u1",
        [{"type": "schedule_wake", "at": "2026-06-20T09:00:00", "tz": "UTC", "note": "hospital"}],
        now=1.0,
    )
    settings = resolve_settings_v2({"switches": {"scheduled": False}})

    def failing_submit(_event):
        raise RuntimeError("inbox down")

    fired = service.fire_due_timers("u1", settings=settings, now=2_000_000_000.0, submit_wake=failing_submit)
    record = store.list_records("u1")[0]

    assert fired[0].status == "deferred"
    assert record.status == SCHEDULED_CLAIMED          # left claimed, not terminal
    assert record.status != SCHEDULED_BLOCKED

    # Submit path recovers and the claim TTL lapses: the timer is reclaimed, the
    # transparency wake is delivered, and only then is it terminally blocked.
    spine = RuntimeSpineV2(settings_resolver=lambda _uid: settings, merge_window_sec=0.0)
    retried = service.fire_due_timers("u1", settings=settings, now=2_000_000_010.0, submit_wake=spine.submit)
    record2 = store.list_records("u1")[0]
    ctx = spine.drain_context("u1", now=2_000_000_010.0)

    assert retried[0].status == "blocked"
    assert retried[0].transparency_wake_id
    assert record2.status == SCHEDULED_BLOCKED
    assert ctx is not None and ctx.trigger == "scheduled_transparency"


def test_policy_rejected_transparency_still_marks_timer_blocked():
    """A deliberate transparency rejection (submit returns accepted=False, e.g.
    policy/backpressure) is terminal — the timer must be marked blocked, not
    deferred into an endless retry loop."""
    store, service = _service()
    service.apply_turn_actions(
        "u1",
        [{"type": "schedule_wake", "at": "2026-06-20T09:00:00", "tz": "UTC", "note": "hospital"}],
        now=1.0,
    )
    settings = resolve_settings_v2({"switches": {"scheduled": False}})

    class _Rejected:
        accepted = False
        reason = "backpressure"
        transparency_required = False

    def rejecting_submit(_event):
        return _Rejected()

    fired = service.fire_due_timers("u1", settings=settings, now=2_000_000_000.0, submit_wake=rejecting_submit)
    record = store.list_records("u1")[0]

    assert fired[0].status == "blocked"
    assert fired[0].transparency_wake_id == ""
    assert record.status == SCHEDULED_BLOCKED
    assert record.block_reason == "scheduled_disabled"


def test_scheduled_action_rejected_while_disabled_consumes_transparency_required():
    store, service = _service()
    settings = resolve_settings_v2({"switches": {"scheduled": False}})
    spine = RuntimeSpineV2(settings_resolver=lambda _uid: settings, merge_window_sec=0.0)

    result = service.apply_turn_actions(
        "u1",
        [{"type": "schedule_wake", "at": "2026-06-20T09:00:00", "tz": "UTC"}],
        settings=settings,
        now=10.0,
        submit_wake=spine.submit,
    )[0]
    ctx = spine.drain_context("u1", now=10.0)

    assert result.status == "rejected"
    assert result.reason == "scheduled_disabled"
    assert result.transparency_required is True
    assert result.transparency_wake_id
    assert ctx is not None
    assert ctx.trigger == "scheduled_transparency"


def test_delivery_off_is_carried_to_scheduled_wake_context_without_blocking_it():
    store, service = _service()
    settings = resolve_settings_v2({"switches": {"reminders_delivery": False}})
    service.apply_turn_actions("u1", [{"type": "schedule_wake", "at": "2026-06-20T09:00:00", "tz": "UTC"}], settings=settings, now=1.0)
    spine = RuntimeSpineV2(settings_resolver=lambda _uid: settings, merge_window_sec=0.0)

    service.fire_due_timers("u1", settings=settings, now=2_000_000_000.0, submit_wake=spine.submit)
    ctx = spine.drain_context("u1", now=2_000_000_000.0)

    assert ctx is not None
    assert ctx.switches["reminders_delivery"] is False


def test_turn_runner_exposes_pending_timer_cap_and_applies_schedule_action():
    store, scheduled = _service(cap=3)
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    spine.submit(WakeEventV2(user_id="u1", source="user_message", trigger="user_message", created_at=10.0))
    captured = []
    runner = TurnRunnerV2(
        spine,
        scheduled_wakes=scheduled,
        run_agent=lambda context: captured.append(context) or TurnOutcomeV2(
            actions=({
                "type": "schedule_wake",
                "at": "2026-06-20T09:00:00",
                "tz": "UTC",
                "note": "follow up",
            },),
        ),
    )

    result = runner.run_ready_turn("u1", now=10.0)

    assert result.status == "completed"
    assert captured[0]["scheduled_wakes"]["pending_cap"] == 3
    assert result.scheduled_action_results[0]["status"] == "scheduled"
    assert store.list_records("u1")[0].note == "follow up"
