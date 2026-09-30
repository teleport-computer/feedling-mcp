from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import pool_config
from model_api_runtime.v2.pool_config import RuntimePoolConfig


def test_wake_pool_constant_is_not_named_like_lane_vocabulary():
    # This test only guards the pool-specific constant name. The production
    # lane-to-pool budgets are guarded at the SlotSpec output by
    # test_slots_have_fixed_lane_allowlists_and_initial_budgets below.
    assert pool_config._WAKE_POOL_LANES == frozenset(
        {"heartbeat", "scheduled", "screen_watch"}
    )
    assert not hasattr(pool_config, "_WAKE_LANES")


def test_defaults_build_complete_eight_slot_topology(monkeypatch):
    for name in (
        "FEEDLING_V2_FOREGROUND_SLOTS",
        "FEEDLING_V2_WAKE_SLOTS",
        "FEEDLING_V2_HEAVY_SLOTS",
        "FEEDLING_V2_PROFILE_INSTANCE_CONCURRENCY",
        "FEEDLING_V2_ENCLAVE_INSTANCE_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)

    config = RuntimePoolConfig.from_env()

    assert [slot.slot_id for slot in config.slots] == [
        "foreground-0",
        "foreground-1",
        "foreground-2",
        "foreground-3",
        "wake-0",
        "wake-1",
        "heavy-0",
        "heavy-1",
    ]
    assert config.profile_instance_concurrency == 1
    assert config.enclave_instance_concurrency == 4


def test_slots_have_fixed_lane_allowlists_and_initial_budgets(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_FOREGROUND_SLOTS", raising=False)
    monkeypatch.delenv("FEEDLING_V2_WAKE_SLOTS", raising=False)
    monkeypatch.delenv("FEEDLING_V2_HEAVY_SLOTS", raising=False)

    config = RuntimePoolConfig.from_env()
    slots = {slot.slot_id: slot for slot in config.slots}

    assert slots["foreground-0"].lanes == frozenset({"chat", "manual_wake"})
    assert slots["wake-0"].lanes == frozenset(
        {"heartbeat", "scheduled", "screen_watch"}
    )
    assert slots["heavy-0"].lanes == frozenset(
        {"profile", "dream", "capture", "maintenance", "trajectory_review"}
    )
    assert slots["heavy-1"].lanes == frozenset(
        {"dream", "capture", "maintenance", "trajectory_review"}
    )
    assert (slots["foreground-0"].stall_budget_sec, slots["foreground-0"].absolute_budget_sec) == (240.0, 1500.0)
    assert (slots["wake-0"].stall_budget_sec, slots["wake-0"].absolute_budget_sec) == (240.0, 900.0)
    assert (slots["heavy-0"].stall_budget_sec, slots["heavy-0"].absolute_budget_sec) == (210.0, 1260.0)
    assert sum("profile" in slot.lanes for slot in config.slots) == 1


def test_slot_counts_are_read_from_capacity_environment(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_FOREGROUND_SLOTS", "2")
    monkeypatch.setenv("FEEDLING_V2_WAKE_SLOTS", "1")
    monkeypatch.setenv("FEEDLING_V2_HEAVY_SLOTS", "3")

    config = RuntimePoolConfig.from_env()

    assert [slot.pool for slot in config.slots].count("foreground") == 2
    assert [slot.pool for slot in config.slots].count("wake") == 1
    assert [slot.pool for slot in config.slots].count("heavy") == 3
    assert sum("profile" in slot.lanes for slot in config.slots) == 1


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("FEEDLING_V2_FOREGROUND_SLOTS", "0", "foreground slots"),
        ("FEEDLING_V2_WAKE_SLOTS", "-1", "wake slots"),
        ("FEEDLING_V2_HEAVY_SLOTS", "0", "heavy slots"),
        ("FEEDLING_V2_PROFILE_INSTANCE_CONCURRENCY", "2", "profile instance concurrency"),
        ("FEEDLING_V2_ENCLAVE_INSTANCE_CONCURRENCY", "3", "enclave instance concurrency"),
    ],
)
def test_invalid_capacity_is_rejected(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        RuntimePoolConfig.from_env()


def test_retired_switches_cannot_change_the_topology(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_POOL_MODE", "legacy")
    monkeypatch.setenv("FEEDLING_V2_MAX_WORKERS", "99")
    monkeypatch.setenv("FEEDLING_V2_CHAT_PREEMPTION_ENABLED", "0")
    monkeypatch.setenv("FEEDLING_V2_SLOT_PROCESS_ISOLATION", "0")

    config = RuntimePoolConfig.from_env()

    assert len(config.slots) == 8
    assert [slot.pool for slot in config.slots].count("foreground") == 4
    assert [slot.pool for slot in config.slots].count("wake") == 2
    assert [slot.pool for slot in config.slots].count("heavy") == 2


def test_heavy_extraction_slots_outlast_one_provider_wire(monkeypatch):
    """Each wire reports progress, including compatibility fallback.

    The largest lane deadline plus 30s must fit each extraction slot, and the
    absolute allowance must cover the existing provider-attempt envelope.
    """
    from model_api_runtime.v2 import extraction

    for name in ("FEEDLING_V2_FOREGROUND_SLOTS", "FEEDLING_V2_WAKE_SLOTS", "FEEDLING_V2_HEAVY_SLOTS"):
        monkeypatch.delenv(name, raising=False)
    extraction_slots = [
        slot for slot in RuntimePoolConfig.from_env().slots
        if slot.lanes & {"capture", "dream"}
    ]

    assert extraction_slots
    for slot in extraction_slots:
        assert slot.stall_budget_sec >= max(
            extraction.wire_deadline_for_lane(lane)
            for lane in slot.lanes & {"capture", "dream", "profile"}
        ) + 30.0, slot.slot_id
        assert slot.absolute_budget_sec > slot.stall_budget_sec, slot.slot_id
        assert slot.absolute_budget_sec >= extraction.nominal_provider_envelope_sec()


def test_dream_budget_anchors_and_pool_derivation(monkeypatch):
    from model_api_runtime.v2 import extraction

    assert extraction.DREAM_WIRE_DEADLINE_SEC == 180.0
    assert extraction.wire_deadline_for_lane("capture") == 90.0
    assert extraction.wire_deadline_for_lane("profile") == 90.0
    assert extraction.nominal_provider_envelope_sec() == 1206.0
    monkeypatch.setattr(extraction, "DREAM_WIRE_DEADLINE_SEC", 220.0)
    heavy = next(s for s in RuntimePoolConfig.from_env().slots if s.pool == "heavy")
    assert heavy.stall_budget_sec == 250.0
    assert heavy.absolute_budget_sec == 1500.0


def test_capture_full_retry_envelope_fits_both_watchdogs():
    from model_api_runtime.v2 import extraction, serve_worker

    # First request: three reliable attempts plus nominal backoff. Truncation
    # re-ask and budget fallback: one each, still up to two compatibility wires.
    required = (3 * 2 * 90 + 6) + (1 * 2 * 180) + (1 * 2 * 90) + 120
    assert required == 1206
    assert extraction.nominal_provider_envelope_sec() == required
    assert serve_worker._EXTRACTION_TURN_BUDGET_SEC >= required
    assert serve_worker._TURN_ABSOLUTE_TIMEOUT_SEC >= required
    for slot in RuntimePoolConfig.from_env().slots:
        if "capture" in slot.lanes:
            assert slot.absolute_budget_sec == 1260 >= required
            assert slot.stall_budget_sec == 210


def test_capture_envelope_tracks_its_actual_wire_and_attempt_limits(monkeypatch):
    from model_api_runtime.v2 import extraction

    # Default Capture and the old Dream single-call allowance happen to match.
    # A changed Capture deadline must not hide behind that accidental equality.
    monkeypatch.setattr(extraction, "WIRE_DEADLINE_SEC", 110.0)
    assert extraction.nominal_provider_envelope_sec() == 1366.0
    monkeypatch.setattr(extraction, "CAPTURE_TRUNCATION_MAX_ATTEMPTS", 2)
    assert extraction.nominal_provider_envelope_sec() == 1949.0
