from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perception.differ_v2 import PerceptionDifferV2
from proactive.adapters_v2 import wake_event_v2_from_legacy_job
from proactive.controls_v2 import (
    evaluate_delivery_v2,
    evaluate_scheduled_action_v2,
    evaluate_wake_control_v2,
    resolve_settings_v2,
    settings_v2_to_doc,
)
from proactive.runtime_v2 import (
    BackgroundLeaseRegistryV2,
    RuntimeSpineV2,
    TurnLeaseRegistryV2,
    TurnOutcomeV2,
    TurnRunnerV2,
    WakeEventV2,
    merge_wakes_v2,
)
from proactive.tool_catalog_v2 import FAST, SLOW, default_tool_catalog_v2


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


def test_wake_inbox_merges_nearby_wakes_and_dedupes_same_trigger():
    spine = RuntimeSpineV2(merge_window_sec=2.0)
    spine.submit(WakeEventV2(user_id="u1", source="heartbeat", trigger="heartbeat", created_at=100.0))
    spine.submit(WakeEventV2(user_id="u1", source="heartbeat", trigger="heartbeat", created_at=100.2))
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="perception_event",
            trigger="arrived_at_anchor",
            created_at=100.5,
            change_digest="anchor: home -> work",
        )
    )

    assert spine.drain_context("u1", now=101.0) is None
    ctx = spine.drain_context("u1", now=103.0)

    assert ctx is not None
    assert ctx.trigger == "arrived_at_anchor"
    assert ctx.merged_triggers == ("heartbeat",)
    assert len(ctx.wake_ids) == 2
    assert "anchor: home -> work" in ctx.change_digest
    assert any(tool["name"] == "perception.now" for tool in ctx.tools)


def test_latency_sensitive_wake_flushes_without_waiting_for_window():
    spine = RuntimeSpineV2(merge_window_sec=3.0)
    spine.submit(WakeEventV2(user_id="u1", source="heartbeat", trigger="heartbeat", created_at=200.0))
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="user_message",
            trigger="user_message",
            created_at=200.2,
            latency_sensitive=True,
            manual=True,
        )
    )

    ctx = spine.drain_context("u1", now=200.25)

    assert ctx is not None
    assert ctx.trigger == "user_message"
    assert ctx.latency_sensitive is True
    assert ctx.manual is True
    assert ctx.merged_triggers == ("heartbeat",)


def test_background_result_is_context_not_direct_chat_write():
    ctx = merge_wakes_v2([
        WakeEventV2(
            user_id="u1",
            source="background_result",
            trigger="background_result",
            background_payload={"tool": "perception.calendar", "result": {"events": 2}},
        )
    ])

    turn_context = ctx.as_turn_context()
    assert turn_context["trigger"] == "background_result"
    assert turn_context["background_payloads"] == [
        {"tool": "perception.calendar", "result": {"events": 2}}
    ]
    assert turn_context["switches"] == _switches()


def test_v2_settings_compatibility_maps_old_fields_without_resurrecting_old_gates():
    settings = resolve_settings_v2({
        "enabled": False,
        "dnd": True,
        "user_state": "away",
        "ai_state": "waiting",
    })

    assert settings.switches() == _switches(ambient=False, reminders_delivery=False)
    doc = settings_v2_to_doc(settings)
    assert doc["switches"] == settings.switches()
    assert "enabled" not in doc
    assert "dnd" not in doc
    assert "user_state" not in doc
    assert "ai_state" not in doc


def test_ambient_off_blocks_only_heartbeat_wake_sources():
    settings = resolve_settings_v2({"switches": {"ambient": False}})
    decision = evaluate_wake_control_v2("heartbeat", trigger="heartbeat_broadcast_off", settings=settings)
    assert decision.accepted is False
    assert decision.reason == "ambient_disabled"

    for source, trigger in (
        ("perception_event", "photo_added"),
        ("perception_event", "arrived_at_anchor"),
        ("perception_event", "unlock_after_absence"),
        ("scene_change", "screen_watch"),
        ("user_message", "user_message"),
        ("scheduled_wake", "scheduled_wake"),
        ("background_result", "background_result"),
    ):
        decision = evaluate_wake_control_v2(source, trigger=trigger, settings=settings)
        assert decision.accepted is True


def test_event_wake_switches_gate_only_matching_trigger_and_manual_bypasses():
    cases = [
        ("photo_wake_enabled", "perception_event", "photo_added", "photo_wake_disabled"),
        ("arrival_wake_enabled", "perception_event", "arrived_at_anchor", "arrival_wake_disabled"),
        ("unlock_wake_enabled", "perception_event", "unlock_after_absence", "unlock_wake_disabled"),
        ("screen_watch_enabled", "scene_change", "screen_watch", "screen_watch_disabled"),
        ("screen_watch_enabled", "scene_change", "scene_change", "screen_watch_disabled"),
    ]
    for switch, source, trigger, reason in cases:
        settings = resolve_settings_v2({"switches": {switch: False}})
        blocked = evaluate_wake_control_v2(source, trigger=trigger, settings=settings)
        manual = evaluate_wake_control_v2(source, trigger=trigger, manual=True, settings=settings)
        heartbeat = evaluate_wake_control_v2("heartbeat", trigger="heartbeat_broadcast_off", settings=settings)

        assert blocked.accepted is False, switch
        assert blocked.reason == reason
        assert manual.accepted is True
        assert manual.reason == "manual_bypass"
        assert heartbeat.accepted is True


def test_runtime_submit_applies_ambient_gate_without_suppressing_scheduled_or_background():
    settings = resolve_settings_v2({"switches": {"ambient": False}})
    spine = RuntimeSpineV2(settings_resolver=lambda _user_id: settings, merge_window_sec=1.0)

    blocked = spine.submit(WakeEventV2(user_id="u1", source="heartbeat", trigger="heartbeat", created_at=1.0))
    scheduled = spine.submit(WakeEventV2(user_id="u1", source="scheduled_wake", trigger="scheduled_wake", created_at=2.0))
    background = spine.submit(WakeEventV2(user_id="u1", source="background_result", trigger="background_result", created_at=2.1))

    ctx = spine.drain_context("u1", now=3.2)

    assert blocked.accepted is False
    assert scheduled.accepted is True
    assert background.accepted is True
    assert ctx is not None
    assert ctx.trigger == "scheduled_wake"
    assert ctx.merged_triggers == ("background_result",)
    assert ctx.switches == _switches(ambient=False)


def test_runtime_settings_resolver_failure_falls_back_to_default_switches():
    def boom(_user_id: str):
        raise RuntimeError("settings unavailable")

    spine = RuntimeSpineV2(settings_resolver=boom, merge_window_sec=0.0)

    submit = spine.submit(WakeEventV2(user_id="u1", source="heartbeat", trigger="heartbeat", created_at=1.0))
    ctx = spine.drain_context("u1", now=1.0)

    assert submit.accepted is True
    assert ctx is not None
    assert ctx.switches == _switches()


def test_runtime_submit_carries_settings_timezone_into_merged_context():
    spine = RuntimeSpineV2(
        settings_resolver=lambda _user_id: {"timezone": "Asia/Tokyo"},
        merge_window_sec=0.0,
    )

    spine.submit(WakeEventV2(user_id="u1", source="heartbeat", trigger="heartbeat", created_at=1.0))
    ctx = spine.drain_context("u1", now=1.0)

    assert ctx is not None
    assert ctx.timezone == "Asia/Tokyo"
    assert ctx.as_turn_context()["timezone"] == "Asia/Tokyo"


def test_scheduled_off_blocks_timer_execution_without_silent_loss():
    settings = resolve_settings_v2({"switches": {"scheduled": False}})
    wake_decision = evaluate_wake_control_v2("scheduled_wake", settings=settings)
    action_decision = evaluate_scheduled_action_v2({"type": "schedule_wake"}, settings=settings)

    assert wake_decision.accepted is False
    assert wake_decision.reason == "scheduled_disabled"
    assert wake_decision.transparency_required is True
    assert action_decision.accepted is False
    assert action_decision.reason == "scheduled_disabled"
    assert action_decision.transparency_required is True
    assert action_decision.switches["scheduled"] is False


def test_delivery_off_blocks_visible_delivery_not_chat_write_and_is_visible_to_agent():
    settings = resolve_settings_v2({"switches": {"reminders_delivery": False}})
    delivery = evaluate_delivery_v2(settings, source="heartbeat")
    spine = RuntimeSpineV2(settings_resolver=lambda _user_id: settings, merge_window_sec=0.0)

    submit = spine.submit(WakeEventV2(user_id="u1", source="heartbeat", trigger="heartbeat", created_at=1.0))
    ctx = spine.drain_context("u1", now=1.0)

    assert submit.accepted is True
    assert delivery.allow_chat_write is True
    assert delivery.allow_visible_delivery is False
    assert delivery.reason == "reminders_delivery_disabled"
    assert ctx is not None
    assert ctx.as_turn_context()["switches"]["reminders_delivery"] is False


def test_manual_wake_bypasses_user_silencing_gates():
    settings = resolve_settings_v2({
        "switches": {
            "ambient": False,
            "scheduled": False,
            "reminders_delivery": False,
        }
    })

    wake_decision = evaluate_wake_control_v2("heartbeat", manual=True, settings=settings)
    delivery_decision = evaluate_delivery_v2(settings, source="heartbeat", manual=True)
    action_decision = evaluate_scheduled_action_v2({"type": "send_message"}, settings=settings, manual=True)

    assert wake_decision.accepted is True
    assert wake_decision.reason == "manual_bypass"
    assert delivery_decision.allow_visible_delivery is True
    assert delivery_decision.reason == "manual_bypass"
    assert action_decision.accepted is True
    assert action_decision.reason == "manual_bypass"


def test_manual_wake_does_not_bypass_scheduled_capability_switch():
    settings = resolve_settings_v2({"switches": {"scheduled": False}})

    action_decision = evaluate_scheduled_action_v2({"type": "schedule_wake"}, settings=settings, manual=True)

    assert action_decision.accepted is False
    assert action_decision.reason == "scheduled_disabled"
    assert action_decision.transparency_required is True


def test_scheduled_wake_does_not_preempt_same_episode_event_by_fixed_priority():
    ctx = merge_wakes_v2([
        WakeEventV2(
            user_id="u1",
            source="perception_event",
            trigger="arrived_at_anchor",
            created_at=100.0,
        ),
        WakeEventV2(
            user_id="u1",
            source="scheduled_wake",
            trigger="scheduled_wake",
            created_at=100.5,
            scheduled_note="check whether she left for the hospital",
        ),
    ])

    assert ctx.trigger == "arrived_at_anchor"
    assert ctx.merged_triggers == ("scheduled_wake",)
    assert ctx.scheduled_note == "check whether she left for the hospital"


def test_turn_lease_blocks_and_reclaims_after_expiry():
    leases = TurnLeaseRegistryV2()

    first = leases.try_acquire_user("u1", owner_id="worker-a", now=10.0, ttl_sec=5.0)
    blocked = leases.try_acquire_user("u1", owner_id="worker-b", now=14.0, ttl_sec=5.0)
    reclaimed = leases.try_acquire_user("u1", owner_id="worker-b", now=15.1, ttl_sec=5.0)

    assert first is not None
    assert blocked is None
    assert reclaimed is not None
    assert reclaimed.owner_id == "worker-b"
    assert reclaimed.lease_id != first.lease_id
    assert leases.release(first) is False
    assert leases.release(reclaimed) is True


def test_turn_runner_single_flight_blocks_until_turn_lease_released():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    spine.submit(WakeEventV2(user_id="u1", source="user_message", trigger="user_message", created_at=20.0))
    leases = TurnLeaseRegistryV2()
    held = leases.try_acquire_user("u1", owner_id="worker-a", now=20.0, ttl_sec=60.0)
    calls = []
    runner = TurnRunnerV2(
        spine,
        turn_leases=leases,
        run_agent=lambda context: calls.append(context["trigger"]) or TurnOutcomeV2(actions=({"type": "sleep"},)),
    )

    busy = runner.run_ready_turn("u1", now=20.1, owner_id="worker-b")
    assert busy.status == "busy"
    assert calls == []

    assert held is not None
    assert leases.release(held) is True
    completed = runner.run_ready_turn("u1", now=20.2, owner_id="worker-b")

    assert completed.status == "completed"
    assert calls == ["user_message"]
    assert leases.current("turn:u1", now=20.3) is None


def test_background_path_releases_turn_slot_and_reenters_inbox_as_wake():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    spine.submit(WakeEventV2(user_id="u1", source="user_message", trigger="user_message", created_at=30.0))
    background_leases = BackgroundLeaseRegistryV2()

    runner = TurnRunnerV2(
        spine,
        background_leases=background_leases,
        run_agent=lambda _context: TurnOutcomeV2(
            actions=({"type": "sleep"},),
            needs_background=True,
            background_request={"tool": "perception.calendar"},
        ),
    )
    queued = runner.run_ready_turn("u1", now=30.0)

    assert queued.status == "background_queued"
    assert queued.background_job_id.startswith("bg_")
    assert queued.background_lease is not None
    assert runner.turn_leases.current("turn:u1", now=30.1) is None

    event = runner.submit_background_result(
        "u1",
        {"tool": "perception.calendar", "result": {"events": 2}},
        origin_refs=tuple(queued.context.wake_ids),
        now=31.0,
    )
    ctx = spine.drain_context("u1", now=31.0)

    assert event.source == "background_result"
    assert ctx is not None
    assert ctx.trigger == "background_result"
    assert ctx.background_payloads == (
        {"tool": "perception.calendar", "result": {"events": 2}},
    )


def test_tool_catalog_cost_classes_and_pull_only_motion():
    catalog = default_tool_catalog_v2()

    assert catalog.cost_class_for("perception.now") == FAST
    assert catalog.cost_class_for("screen.read", {"mode": "caption"}) == FAST
    assert catalog.cost_class_for("screen.read", {"mode": "full"}) == SLOW
    assert catalog.cost_class_for("perception.calendar", {"window_days": 7}) == FAST
    assert catalog.cost_class_for("perception.calendar", {"window_days": 30}) == SLOW
    assert catalog.get("perception.motion").wake_source is False


def test_perception_differ_only_discrete_anchor_wakes_location():
    differ = PerceptionDifferV2()

    motion = differ.observe("u1", "motion_state", "walking", ts=1.0)
    place = differ.observe("u1", "place_label", "home", ts=2.0)
    anchor = differ.observe("u1", "connectivity_anchor", {"anchor_id": "wifi-home", "label": "home"}, ts=3.0)

    assert motion.events == ()
    assert place.events == ()
    assert len(anchor.events) == 1
    assert anchor.events[0].source == "perception_event"
    assert anchor.events[0].trigger == "arrived_at_anchor"
    assert anchor.presence_hints["entered_anchor"] == "home"


def test_perception_differ_tracks_seen_separately_from_changed():
    differ = PerceptionDifferV2()

    first = differ.observe("u1", "connectivity_anchor", "wifi-home", ts=10.0)
    second = differ.observe("u1", "connectivity_anchor", "wifi-home", ts=20.0)

    assert first.state.last_seen_ts == 10.0
    assert first.state.last_changed_ts == 10.0
    assert second.changed is False
    assert second.state.last_seen_ts == 20.0
    assert second.state.last_changed_ts == 10.0
    assert second.events == ()


def test_legacy_job_adapter_keeps_old_shape_at_boundary():
    event = wake_event_v2_from_legacy_job(
        "u1",
        {
            "job_id": "pj_1",
            "wake_id": "wake_1",
            "trigger": "perception_location",
            "ts": 123.0,
            "context_hint": "legacy hint",
            "frame_ids": ["frame_1"],
        },
    )

    assert event.source == "perception_event"
    assert event.trigger == "perception_location"
    assert event.change_digest == "legacy hint"
    assert event.payload["legacy_proactive_job"]["job_id"] == "pj_1"
