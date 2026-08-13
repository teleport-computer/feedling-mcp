from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2.pool_config import RuntimePoolConfig


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
    assert (slots["heavy-0"].stall_budget_sec, slots["heavy-0"].absolute_budget_sec) == (240.0, 1200.0)
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
