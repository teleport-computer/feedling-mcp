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

from model_api_runtime.v2 import jobs_store, serve_worker, worker


class _FakeSupervisor:
    def __init__(self, liveness: dict):
        self._liveness = liveness

    def poll_liveness(self) -> dict:
        return dict(self._liveness)


def _drive_one_beat(monkeypatch, liveness: dict, *, max_workers: int = 4,
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
            "worker-a", stop_event, supervisor=supervisor, interval=0.02,
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
    assert kwargs == {"capacity": 4, "kind": "turn"}


def test_heartbeat_records_zero_capacity_when_child_dead(monkeypatch):
    worker_id, kwargs = _drive_one_beat(
        monkeypatch, {"alive": False, "last_progress_age_sec": 1.0}, max_workers=4)
    assert worker_id == "worker-a"
    assert kwargs == {"capacity": 0, "kind": "turn"}


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
    assert kwargs == {"capacity": 0, "kind": "turn"}


def test_heartbeat_records_full_capacity_when_progress_just_under_threshold(monkeypatch):
    worker_id, kwargs = _drive_one_beat(
        monkeypatch,
        {"alive": True, "last_progress_age_sec": 10.0},
        max_workers=4,
        capacity_stale_sec=45.0,
    )
    assert worker_id == "worker-a"
    assert kwargs == {"capacity": 4, "kind": "turn"}


def test_heartbeat_survives_missing_last_progress_age_sec(monkeypatch):
    """`poll_liveness()` contract always includes `last_progress_age_sec`, but the
    heartbeat loop must not crash if a supervisor implementation omits it — treat
    a missing age as "never reported progress" (i.e. stale/zero capacity), the
    same fail-safe direction `watchdog.should_kill` takes for `math.inf`."""
    worker_id, kwargs = _drive_one_beat(
        monkeypatch, {"alive": True}, max_workers=4, capacity_stale_sec=45.0)
    assert worker_id == "worker-a"
    assert kwargs == {"capacity": 0, "kind": "turn"}
