"""Hosted Runtime V2 PR D, Task 3 (D2 watchdog + hard-timeout).

Two layers:
- `watchdog.should_kill` — pure decision function, no I/O, exhaustively unit tested.
- `watchdog._watchdog_loop` — the parent asyncio loop that polls a (fake, injected)
  supervisor + a jobs-claimable predicate every ``interval`` seconds and, on a kill
  decision, writes capacity=0 BEFORE calling `supervisor.kill_and_respawn()` (so
  admission observes the drop immediately, without racing the actual kill).

Mirrors the `_reaper_loop`/`_heartbeat_loop` driver pattern in
`tests/test_v2_serve_worker.py` (short interval + poll-until-N-calls + stop_event.set()
+ bounded `asyncio.wait_for`) — no pytest-asyncio plugin in this repo, tests drive
coroutines via `asyncio.run`.
"""
import asyncio
import math

from model_api_runtime.v2 import watchdog


# ---------------------------------------------------------------------------
# should_kill — pure decision function
# ---------------------------------------------------------------------------

def _kw(**overrides):
    kw = {"turn_hard_timeout_sec": 180.0, "child_liveness_timeout_sec": 45.0,
          "jobs_claimable": True}
    kw.update(overrides)
    return kw


def test_should_kill_false_when_healthy_and_fresh():
    liveness = {"alive": True, "last_progress_age_sec": 1.0}
    assert watchdog.should_kill(liveness, **_kw()) is False


def test_should_kill_true_when_all_slots_stuck_and_jobs_claimable():
    """The acceptance case: progress has gone stale past the liveness timeout AND
    there is claimable work waiting — this is the "all slots wedged" signal."""
    liveness = {"alive": True, "last_progress_age_sec": 46.0}
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=True)) is True


def test_should_kill_false_when_stale_but_idle_no_claimable_work():
    """A healthy-but-idle child legitimately has no work and stops emitting fresh
    progress between polls; the jobs_claimable guard must prevent killing it."""
    liveness = {"alive": True, "last_progress_age_sec": 999.0}
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is False


def test_should_kill_true_when_child_process_dead_even_if_jobs_not_claimable():
    liveness = {"alive": False, "last_progress_age_sec": 1.0}
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is True


def test_should_kill_true_when_child_dead_and_age_reported_as_inf():
    """`ChildSupervisor.poll_liveness()` returns `last_progress_age_sec=inf` before
    the first progress message ever arrives — must not blow up the comparison."""
    liveness = {"alive": False, "last_progress_age_sec": math.inf}
    assert watchdog.should_kill(liveness, **_kw()) is True


def test_should_kill_true_on_turn_hard_timeout_when_reported():
    """Optional per-turn age field (clause c): a single wedged turn over the hard
    timeout kills even if progress is otherwise still ticking (e.g. other slots)."""
    liveness = {"alive": True, "last_progress_age_sec": 1.0, "current_turn_age_sec": 181.0}
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is True


def test_should_kill_false_when_turn_age_field_absent():
    """Absent `current_turn_age_sec` must skip clause (c) entirely, not be treated
    as 0 > timeout (which would trivially always be False anyway) or crash."""
    liveness = {"alive": True, "last_progress_age_sec": 1.0}
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is False


def test_should_kill_false_when_turn_age_under_timeout():
    liveness = {"alive": True, "last_progress_age_sec": 1.0, "current_turn_age_sec": 179.0}
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is False


# ---------------------------------------------------------------------------
# _watchdog_loop — parent loop wiring
# ---------------------------------------------------------------------------

class _FakeSupervisor:
    def __init__(self, liveness: dict):
        self._liveness = liveness
        self.kill_calls = 0

    def poll_liveness(self) -> dict:
        return dict(self._liveness)

    def kill_and_respawn(self) -> None:
        self.kill_calls += 1


def test_watchdog_loop_kills_and_writes_capacity_zero_before_kill_and_respawn(monkeypatch):
    """On a kill decision: capacity=0 must be recorded strictly before
    kill_and_respawn() runs (admission must see the drop before the SIGKILL races
    a fresh claim in) — assert via a shared ordering log both fakes append to."""
    order: list[str] = []

    from model_api_runtime.v2 import jobs_store

    def _fake_heartbeat(worker_id, *, capacity, kind="turn"):
        assert capacity == 0
        assert kind == "turn"
        order.append("capacity_zero")

    monkeypatch.setattr(jobs_store, "record_worker_heartbeat", _fake_heartbeat)

    class _KillOrderSupervisor(_FakeSupervisor):
        def kill_and_respawn(self) -> None:
            order.append("kill_and_respawn")
            super().kill_and_respawn()

    supervisor = _KillOrderSupervisor({"alive": True, "last_progress_age_sec": 999.0})
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(watchdog._watchdog_loop(
            supervisor, "worker-a", stop_event,
            jobs_claimable_fn=lambda: True,
            interval=0.02,
            turn_hard_timeout_sec=180.0,
            child_liveness_timeout_sec=45.0,
        ))
        for _ in range(50):
            if supervisor.kill_calls >= 1:
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert supervisor.kill_calls >= 1
    assert order[:2] == ["capacity_zero", "kill_and_respawn"]


def test_watchdog_loop_does_not_kill_when_healthy(monkeypatch):
    from model_api_runtime.v2 import jobs_store

    heartbeat_calls = []
    monkeypatch.setattr(
        jobs_store, "record_worker_heartbeat",
        lambda worker_id, **kwargs: heartbeat_calls.append(kwargs))

    supervisor = _FakeSupervisor({"alive": True, "last_progress_age_sec": 1.0})
    stop_event = asyncio.Event()
    claimable_calls = {"n": 0}

    def _claimable():
        claimable_calls["n"] += 1
        return True

    async def _driver():
        task = asyncio.create_task(watchdog._watchdog_loop(
            supervisor, "worker-a", stop_event,
            jobs_claimable_fn=_claimable,
            interval=0.02,
            turn_hard_timeout_sec=180.0,
            child_liveness_timeout_sec=45.0,
        ))
        for _ in range(50):
            if claimable_calls["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert supervisor.kill_calls == 0
    assert heartbeat_calls == []


def test_watchdog_loop_swallows_per_iteration_errors(monkeypatch):
    """A poll_liveness/jobs_claimable_fn exception on one tick must not crash the
    parent loop — it logs and keeps ticking (mirrors _reaper_loop's transient-error
    contract)."""
    from model_api_runtime.v2 import jobs_store
    monkeypatch.setattr(
        jobs_store, "record_worker_heartbeat", lambda worker_id, **kwargs: None)

    calls = {"n": 0}

    class _FlakySupervisor:
        def poll_liveness(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient supervisor error")
            return {"alive": True, "last_progress_age_sec": 1.0}

        def kill_and_respawn(self):
            pass

    supervisor = _FlakySupervisor()
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(watchdog._watchdog_loop(
            supervisor, "worker-a", stop_event,
            jobs_claimable_fn=lambda: True,
            interval=0.02,
            turn_hard_timeout_sec=180.0,
            child_liveness_timeout_sec=45.0,
        ))
        for _ in range(50):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert calls["n"] >= 2  # survived the first raise and ticked again
