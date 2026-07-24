"""Server-owned heartbeat cadence gate and CAS scheduler-state coverage."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from core import store as core_store  # noqa: E402
from proactive import gate  # noqa: E402
from proactive import proactive_core  # noqa: E402

from conftest import seed_user  # noqa: E402


class _Store:
    user_id = "usr_heartbeat_gate"

    def __init__(self, *, next_tick_at: float = 0.0):
        self.settings = {
            "first_chat_ok_at": "2026-07-24T00:00:00",
            "wake_interval_sec": 900,
            core_store.HEARTBEAT_NEXT_TICK_AT_KEY: next_tick_at,
        }
        self.decisions = []
        self.jobs = []
        self.skipped = []
        self.order = []

    def load_proactive_settings(self):
        return dict(self.settings)

    def list_device_events(self, since_epoch=0, limit=100):
        return []

    def append_gate_decision(self, decision):
        self.order.append("decision")
        self.decisions.append(decision)
        return decision

    def append_proactive_job(self, job):
        self.order.append("job")
        self.jobs.append(job)
        return job

    def append_skipped_proactive_job(self, job):
        self.order.append("skipped")
        self.skipped.append(job)
        return job


@pytest.fixture(autouse=True)
def _resident_mode(monkeypatch):
    monkeypatch.setattr(
        proactive_core.hosted_config_store,
        "get_hosted_runtime_mode_strict",
        lambda _store: "",
    )


def test_second_heartbeat_inside_interval_is_throttled_without_advancing(monkeypatch):
    store = _Store()
    monkeypatch.setattr(gate.time, "time", lambda: 1000.0)
    advances = []

    def _advance(user_id, *, now, wake_interval_sec):
        store.order.append("advance")
        advances.append((user_id, now, wake_interval_sec))
        return 1900.0

    monkeypatch.setattr(core_store, "advance_proactive_heartbeat_tick", _advance)

    first = proactive_core.proactive_tick(
        store,
        {
            "trigger": "heartbeat_broadcast_on",
            "broadcast_state": "on",
            "now": 999999999.0,
        },
        api_key="key",
    )

    assert first["enqueued"] is True
    assert first["decision"]["heartbeat_next_tick_at"] == 1900.0
    assert advances == [(store.user_id, 1000.0, 900)]
    assert store.order == ["decision", "job", "advance"]

    store.settings[core_store.HEARTBEAT_NEXT_TICK_AT_KEY] = 1900.0
    monkeypatch.setattr(gate.time, "time", lambda: 1001.0)
    second = proactive_core.proactive_tick(
        store,
        {"trigger": "heartbeat_broadcast_on", "broadcast_state": "on"},
        api_key="key",
    )

    assert second["enqueued"] is False
    assert second["job"] is None
    assert second["decision"]["reason"] == gate.HEARTBEAT_THROTTLED_REASON
    assert second["decision"]["heartbeat_next_tick_at"] == 1900.0
    assert len(advances) == 1
    assert len(store.jobs) == 1
    assert len(store.skipped) == 1
    assert store.skipped[0]["status"] == "skipped"
    assert store.skipped[0]["status_reason"] == "heartbeat_throttled"


def test_heartbeat_deadline_is_not_written_when_job_enqueue_fails(monkeypatch):
    store = _Store()
    monkeypatch.setattr(gate.time, "time", lambda: 1000.0)
    monkeypatch.setattr(
        store,
        "append_proactive_job",
        lambda _job: (_ for _ in ()).throw(RuntimeError("enqueue failed")),
    )
    advances = []
    monkeypatch.setattr(
        core_store,
        "advance_proactive_heartbeat_tick",
        lambda *args, **kwargs: advances.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="enqueue failed"):
        proactive_core.proactive_tick(
            store,
            {"trigger": "heartbeat_broadcast_on", "broadcast_state": "on"},
            api_key="key",
        )

    assert advances == []


@pytest.mark.parametrize(
    "payload",
    [
        {"trigger": "photo_added", "frames": [{"id": "frame-photo"}]},
        {"trigger": "scheduled_wake", "frames": [{"id": "frame-scheduled"}]},
        {"trigger": "self_wake", "frames": [{"id": "frame-self"}]},
        {
            "trigger": "heartbeat_broadcast_on",
            "broadcast_state": "on",
            "manual": True,
        },
        {
            "trigger": "heartbeat_broadcast_on",
            "broadcast_state": "on",
            "job_kind": "screen_watch",
        },
    ],
)
def test_non_heartbeat_lanes_do_not_read_or_write_deadline(monkeypatch, payload):
    store = _Store(next_tick_at=999999.0)
    monkeypatch.setattr(gate.time, "time", lambda: 1000.0)
    advances = []
    monkeypatch.setattr(
        core_store,
        "advance_proactive_heartbeat_tick",
        lambda *args, **kwargs: advances.append((args, kwargs)),
    )

    result = proactive_core.proactive_tick(store, payload, api_key="key")

    assert result["decision"]["reason"] != gate.HEARTBEAT_THROTTLED_REASON
    assert result["enqueued"] is True
    assert advances == []


def test_interval_reduction_shrinks_existing_deadline_immediately(monkeypatch):
    uid = "usr_heartbeat_interval_shrink"
    seed_user(uid)
    db.set_blob(
        uid,
        "proactive_settings",
        {
            "wake_interval_sec": 7200,
            core_store.HEARTBEAT_NEXT_TICK_AT_KEY: 10000.0,
            "peer_field": "preserve",
        },
    )
    monkeypatch.setattr(core_store.time, "time", lambda: 1000.0)
    store = core_store.get_store(uid)

    saved = store.save_proactive_settings({"wake_interval_sec": 900})

    persisted = db.get_blob(uid, "proactive_settings")
    assert saved["wake_interval_sec"] == 900
    assert saved[core_store.HEARTBEAT_NEXT_TICK_AT_KEY] == 1900.0
    assert persisted[core_store.HEARTBEAT_NEXT_TICK_AT_KEY] == 1900.0
    assert persisted["peer_field"] == "preserve"


def test_heartbeat_deadline_cas_retries_without_clobbering_peer_write(monkeypatch):
    reads = iter([
        {
            core_store.HEARTBEAT_NEXT_TICK_AT_KEY: 0.0,
            "peer_field": "old",
        },
        {
            core_store.HEARTBEAT_NEXT_TICK_AT_KEY: 1500.0,
            "peer_field": "concurrent",
        },
    ])
    monkeypatch.setattr(db, "get_blob", lambda _uid, _kind: next(reads))
    attempts = []

    def _cas(_uid, _kind, expected, replacement, *, insert_if_missing=False):
        attempts.append((expected, replacement, insert_if_missing))
        return len(attempts) == 2

    monkeypatch.setattr(db, "set_blob_if_unchanged", _cas)

    updated = core_store.advance_proactive_heartbeat_tick(
        "usr_cas",
        now=1000.0,
        wake_interval_sec=900,
    )

    assert updated == 1900.0
    assert len(attempts) == 2
    assert attempts[1][0]["peer_field"] == "concurrent"
    assert attempts[1][1]["peer_field"] == "concurrent"
    assert attempts[1][1][core_store.HEARTBEAT_NEXT_TICK_AT_KEY] == 1900.0
