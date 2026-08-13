"""Hosted Runtime V2 PR D, Task 4 (D3 capacity reflects health).

`serve_worker._heartbeat_loop` must derive the `capacity` it writes to the
`kind='turn'` heartbeat row from the turn-child's ACTUAL health
(`child_supervisor.ChildSupervisor.poll_liveness()`), not the constant
`v2_worker.MAX_WORKERS` — otherwise a heartbeat tick ~10s after the watchdog
(Task 3) writes capacity=0 on a kill decision would silently re-advertise full
capacity for a child that is mid-SIGKILL/respawn.

Mirrors the `_reaper_loop`/`_watchdog_loop` driver pattern in
`tests/test_v2_serve_worker.py`/`tests/test_v2_watchdog.py` (short interval +
poll-until-N-calls + stop_event.set() + bounded `asyncio.wait_for`) — no
pytest-asyncio plugin in this repo, tests drive coroutines via `asyncio.run`.
"""
import asyncio

from model_api_runtime.v2 import jobs_store, serve_worker, slot_protocol, worker


class _FakeSupervisor:
    def __init__(self, liveness: dict, snapshot=None):
        self._liveness = liveness
        self._snapshot = snapshot

    def poll_liveness(self) -> dict:
        return dict(self._liveness)

    def snapshot(self):
        return self._snapshot


def _drive_one_beat(monkeypatch, liveness: dict, *, max_workers: int = 4,
                     pool: str = "foreground",
                     capacity_stale_sec: float = 45.0):
    """Run `_heartbeat_loop` until it records exactly one heartbeat, then stop it.
    Returns the captured `(worker_id, kwargs)` of the FIRST recorded call."""
    calls = []
    monkeypatch.setattr(worker, "MAX_WORKERS", max_workers)
    monkeypatch.setattr(
        jobs_store,
        "record_worker_heartbeat",
        lambda worker_id, **kwargs: calls.append((worker_id, kwargs)),
    )
    supervisor = _FakeSupervisor(liveness)
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(serve_worker._heartbeat_loop(
            "worker-a", stop_event, supervisor=supervisor, pool=pool, interval=0.02,
            capacity_stale_sec=capacity_stale_sec))
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert calls, "heartbeat_loop never recorded a beat"
    return calls[0]


def test_heartbeat_records_full_capacity_when_child_alive_and_fresh(monkeypatch):
    worker_id, kwargs = _drive_one_beat(
        monkeypatch, {"alive": True, "last_progress_age_sec": 1.0}, max_workers=4)
    assert worker_id == "worker-a"
    assert kwargs == {
        "capacity": 1,
        "kind": "turn",
        "pool": "foreground",
        "runtime_state": {"slot": {"stage": "starting", "busy": False}},
    }


def test_heartbeat_records_zero_capacity_when_child_dead(monkeypatch):
    worker_id, kwargs = _drive_one_beat(
        monkeypatch, {"alive": False, "last_progress_age_sec": 1.0}, max_workers=4)
    assert worker_id == "worker-a"
    assert kwargs == {
        "capacity": 0,
        "kind": "turn",
        "pool": "foreground",
        "runtime_state": {"slot": {"stage": "starting", "busy": False}},
    }


def test_heartbeat_records_zero_capacity_when_progress_stale(monkeypatch):
    """Alive but wedged (progress older than the stale threshold) must also
    advertise zero capacity — this is the race the watchdog's capacity=0 write
    must agree with, not fight."""
    worker_id, kwargs = _drive_one_beat(
        monkeypatch,
        {"alive": True, "last_progress_age_sec": 999.0},
        max_workers=4,
        capacity_stale_sec=45.0,
    )
    assert worker_id == "worker-a"
    assert kwargs == {
        "capacity": 0,
        "kind": "turn",
        "pool": "foreground",
        "runtime_state": {"slot": {"stage": "starting", "busy": False}},
    }


def test_heartbeat_records_full_capacity_when_progress_just_under_threshold(monkeypatch):
    worker_id, kwargs = _drive_one_beat(
        monkeypatch,
        {"alive": True, "last_progress_age_sec": 10.0},
        max_workers=4,
        capacity_stale_sec=45.0,
    )
    assert worker_id == "worker-a"
    assert kwargs == {
        "capacity": 1,
        "kind": "turn",
        "pool": "foreground",
        "runtime_state": {"slot": {"stage": "starting", "busy": False}},
    }


def test_job_cancel_router_ignores_stale_owner_and_targets_exact_claim():
    router = serve_worker._JobCancelRouter()
    cancelled = []
    router.bind("worker:heavy:0:g8", lambda: cancelled.append("heavy-0:g8"))

    stale = serve_worker.core_wake_bus.JobCancellation(
        3694, "worker:heavy:0:g7", "foreground_chat_preempted"
    )
    current = serve_worker.core_wake_bus.JobCancellation(
        3694, "worker:heavy:0:g8", "foreground_chat_preempted"
    )

    assert router.handle(stale) is False
    assert cancelled == []
    assert router.handle(current) is True
    assert cancelled == ["heavy-0:g8"]


def test_job_cancel_router_matches_supervisor_snapshot_job_and_owner():
    active = slot_protocol.ActiveJobIdentity(
        3694, "profile", "worker:heavy:0:g8"
    )
    snapshot = slot_protocol.SlotProgress(
        "heavy-0", "g8", 123.4, 120.0, "profile.cards.batch", active
    )

    class _SnapshotSupervisor:
        def __init__(self):
            self.kills = 0

        def snapshot(self):
            return snapshot

        def kill_and_respawn(self):
            self.kills += 1

    supervisor = _SnapshotSupervisor()
    router = serve_worker._JobCancelRouter()
    router.watch(supervisor)

    wrong_job = serve_worker.core_wake_bus.JobCancellation(
        3695, active.claimed_by, "foreground_chat_preempted"
    )
    exact = serve_worker.core_wake_bus.JobCancellation(
        active.job_id, active.claimed_by, "foreground_chat_preempted"
    )

    assert router.handle(wrong_job) is False
    assert supervisor.kills == 0
    assert router.handle(exact) is True
    assert supervisor.kills == 1


def test_heartbeat_survives_missing_last_progress_age_sec(monkeypatch):
    """`poll_liveness()` contract always includes `last_progress_age_sec`, but the
    heartbeat loop must not crash if a supervisor implementation omits it — treat
    a missing age as "never reported progress" (i.e. stale/zero capacity), the
    same fail-safe direction `watchdog.should_kill` takes for `math.inf`."""
    worker_id, kwargs = _drive_one_beat(
        monkeypatch, {"alive": True}, max_workers=4, capacity_stale_sec=45.0)
    assert worker_id == "worker-a"
    assert kwargs == {
        "capacity": 0,
        "kind": "turn",
        "pool": "foreground",
        "runtime_state": {"slot": {"stage": "starting", "busy": False}},
    }


def test_heartbeat_writes_the_explicit_pool_identity(monkeypatch):
    worker_id, kwargs = _drive_one_beat(
        monkeypatch,
        {"alive": True, "last_progress_age_sec": 1.0},
        max_workers=2,
        pool="wake",
    )

    assert worker_id == "worker-a"
    assert kwargs == {
        "capacity": 1,
        "kind": "turn",
        "pool": "wake",
        "runtime_state": {"slot": {"stage": "starting", "busy": False}},
    }


def test_heartbeat_runtime_state_exposes_stage_without_job_identity():
    snapshot = slot_protocol.SlotProgress(
        "heavy-0",
        "g7",
        123.4,
        120.0,
        "profile.cards.batch",
        slot_protocol.ActiveJobIdentity(3694, "profile", "worker:heavy:0:g7"),
    )
    state = serve_worker._heartbeat_slot_state(
        _FakeSupervisor({"alive": True}, snapshot=snapshot)
    )

    assert state == {"slot": {"stage": "profile.cards.batch", "busy": True}}
    serialized = repr(state)
    assert "3694" not in serialized
    assert "worker:heavy" not in serialized
    assert "g7" not in serialized
