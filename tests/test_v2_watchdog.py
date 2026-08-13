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
import threading

from model_api_runtime.v2 import claim_recovery, slot_protocol, watchdog


# ---------------------------------------------------------------------------
# should_kill — pure decision function
# ---------------------------------------------------------------------------

def _kw(**overrides):
    kw = {"turn_stall_timeout_sec": 240.0, "turn_absolute_timeout_sec": 1500.0,
          "child_liveness_timeout_sec": 45.0,
          "jobs_claimable": True}
    kw.update(overrides)
    return kw


def test_claim_recovery_queue_is_bounded_and_coalesces_exact_owner_key():
    queue = claim_recovery.ClaimRecoveryQueue(limit=2, recover=lambda **kw: None)

    assert queue.enqueue(job_id=1, claimed_by="w1") is True
    assert queue.enqueue(job_id=1, claimed_by="w1") is True
    assert queue.pending_count == 1
    assert queue.enqueue(job_id=1, claimed_by="w2") is True
    assert queue.pending_count == 2
    assert queue.enqueue(job_id=2, claimed_by="w3") is False


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


def test_explicit_event_loop_heartbeat_staleness_kills_without_queue_query():
    liveness = {
        "alive": True,
        "last_progress_age_sec": 46.0,
        "event_loop_heartbeat_age_sec": 46.0,
        "last_slot_progress_age_sec": 46.0,
        "active_turn_count": 0,
    }
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is True


def test_stale_slot_progress_with_healthy_loop_kills_only_if_pre_turn_work_waits():
    liveness = {
        "alive": True,
        "last_progress_age_sec": 1.0,
        "event_loop_heartbeat_age_sec": 1.0,
        "last_slot_progress_age_sec": 46.0,
        "active_turn_count": 0,
    }
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=True)) is True
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is False


def test_active_turn_uses_its_stall_clock_not_pool_slot_clock():
    liveness = {
        "alive": True,
        "last_progress_age_sec": 1.0,
        "event_loop_heartbeat_age_sec": 1.0,
        "last_slot_progress_age_sec": 999.0,
        "active_turn_count": 1,
        "current_turn_age_sec": 200.0,
        "current_turn_stall_age_sec": 10.0,
    }
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=True)) is False


def test_should_kill_true_when_child_process_dead_even_if_jobs_not_claimable():
    liveness = {"alive": False, "last_progress_age_sec": 1.0}
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is True


def test_should_kill_true_when_child_dead_and_age_reported_as_inf():
    """`ChildSupervisor.poll_liveness()` returns `last_progress_age_sec=inf` before
    the first progress message ever arrives — must not blow up the comparison."""
    liveness = {"alive": False, "last_progress_age_sec": math.inf}
    assert watchdog.should_kill(liveness, **_kw()) is True


def test_long_progressing_turn_survives_past_old_180_second_ceiling():
    """Absolute age is not stall age: healthy round/catch-up progress wins."""
    liveness = {
        "alive": True,
        "last_progress_age_sec": 1.0,
        "current_turn_age_sec": 601.0,
        "current_turn_stall_age_sec": 1.0,
    }
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is False


def test_should_kill_true_when_one_turn_stalls_past_stall_timeout():
    liveness = {
        "alive": True,
        "last_progress_age_sec": 1.0,
        "current_turn_age_sec": 601.0,
        "current_turn_stall_age_sec": 241.0,
    }
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is True


def test_should_kill_false_when_turn_fields_absent():
    """Older/narrow supervisors may omit both per-turn fields."""
    liveness = {"alive": True, "last_progress_age_sec": 1.0}
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is False


def test_should_kill_true_when_progressing_turn_exceeds_absolute_timeout():
    liveness = {
        "alive": True,
        "last_progress_age_sec": 1.0,
        "current_turn_age_sec": 1501.0,
        "current_turn_stall_age_sec": 1.0,
    }
    assert watchdog.should_kill(liveness, **_kw(jobs_claimable=False)) is True


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


def test_watchdog_orders_capacity_snapshot_kill_recover_start(monkeypatch):
    """On a kill decision: capacity=0 must be recorded strictly before
    kill_and_respawn() runs (admission must see the drop before the SIGKILL races
    a fresh claim in) — assert via a shared ordering log both fakes append to."""
    order: list[str] = []

    from model_api_runtime.v2 import jobs_store

    def _fake_heartbeat(worker_id, *, capacity, kind="turn", pool):
        assert capacity == 0
        assert kind == "turn"
        assert pool == "foreground"
        order.append("capacity_zero")

    monkeypatch.setattr(jobs_store, "record_worker_heartbeat", _fake_heartbeat)

    active = slot_protocol.ActiveJobIdentity(3694, "profile", "heavy-0:g7")
    snapshot = slot_protocol.SlotProgress(
        "heavy-0", "g7", 123.4, 120.0, "profile.cards.batch", active
    )

    class _KillOrderSupervisor(_FakeSupervisor):
        def snapshot(self):
            order.append("snapshot")
            return snapshot

        def kill(self):
            order.append("kill")
            self.kill_calls += 1
            return active

        def start(self):
            order.append("start")

    def _recover(**kwargs):
        assert kwargs == {
            "job_id": 3694,
            "claimed_by": "heavy-0:g7",
            "reason": "slot_watchdog_timeout",
        }
        order.append("recover")
        return {"recovery": "requeued"}

    monkeypatch.setattr(watchdog.jobs_store, "recover_killed_job", _recover)

    supervisor = _KillOrderSupervisor({"alive": True, "last_progress_age_sec": 999.0})
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(watchdog._watchdog_loop(
            supervisor, "worker-a", stop_event,
            jobs_claimable_fn=lambda: True,
            interval=0.02,
            turn_stall_timeout_sec=240.0,
            turn_absolute_timeout_sec=1500.0,
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
    assert order[:5] == ["capacity_zero", "snapshot", "kill", "recover", "start"]


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
            turn_stall_timeout_sec=240.0,
            turn_absolute_timeout_sec=1500.0,
            child_liveness_timeout_sec=45.0,
        ))
        await asyncio.sleep(0.06)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert supervisor.kill_calls == 0
    assert heartbeat_calls == []
    assert claimable_calls["n"] == 0  # fresh liveness makes queue state irrelevant


def test_watchdog_restarts_and_queues_exact_retry_when_recovery_raises(monkeypatch):
    monkeypatch.setattr(
        watchdog.jobs_store, "record_worker_heartbeat", lambda *a, **k: None
    )
    monkeypatch.setattr(
        watchdog.jobs_store,
        "recover_killed_job",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    active = slot_protocol.ActiveJobIdentity(77, "profile", "heavy-0:g9")
    snapshot = slot_protocol.SlotProgress(
        "heavy-0", "g9", 10.0, 9.0, "profile.provider", active
    )

    class _Supervisor:
        def __init__(self):
            self.kill_calls = 0
            self.start_calls = 0

        def poll_liveness(self):
            return {"alive": False, "last_progress_age_sec": math.inf}

        def snapshot(self):
            return snapshot

        def kill(self):
            self.kill_calls += 1
            return active

        def start(self):
            self.start_calls += 1

    class _Queue:
        def __init__(self):
            self.requests = []

        def enqueue(self, **kwargs):
            self.requests.append(kwargs)
            return True

    supervisor = _Supervisor()
    queue = _Queue()
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(
            watchdog._watchdog_loop(
                supervisor,
                "worker-a",
                stop_event,
                jobs_claimable_fn=lambda: False,
                interval=0.02,
                turn_stall_timeout_sec=240.0,
                turn_absolute_timeout_sec=1500.0,
                child_liveness_timeout_sec=45.0,
                recovery_queue=queue,
            )
        )
        for _ in range(50):
            if supervisor.start_calls:
                break
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert supervisor.kill_calls >= 1
    assert supervisor.start_calls >= 1
    assert queue.requests[0] == {
        "job_id": 77,
        "claimed_by": "heavy-0:g9",
        "reason": "slot_watchdog_timeout",
    }


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
            turn_stall_timeout_sec=240.0,
            turn_absolute_timeout_sec=1500.0,
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


def test_dead_child_short_circuits_hung_claimable_check(monkeypatch):
    """Physical recovery must not wait for a queue-state DB round trip."""
    from model_api_runtime.v2 import jobs_store

    monkeypatch.setattr(
        jobs_store, "record_worker_heartbeat", lambda worker_id, **kwargs: None)
    supervisor = _FakeSupervisor({"alive": False, "last_progress_age_sec": math.inf})
    stop_event = asyncio.Event()

    def _must_not_run():
        raise AssertionError("dead-child path must short-circuit queue DB check")

    async def _driver():
        task = asyncio.create_task(watchdog._watchdog_loop(
            supervisor, "worker-dead", stop_event,
            jobs_claimable_fn=_must_not_run,
            interval=0.01,
            turn_stall_timeout_sec=240.0,
            turn_absolute_timeout_sec=1500.0,
            child_liveness_timeout_sec=45.0,
        ))
        for _ in range(50):
            if supervisor.kill_calls:
                break
            await asyncio.sleep(0.005)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert supervisor.kill_calls >= 1


def test_capacity_write_timeout_cannot_block_kill(monkeypatch):
    from model_api_runtime.v2 import jobs_store

    release = threading.Event()

    def _hung_heartbeat(*args, **kwargs):
        release.wait(1.0)

    monkeypatch.setattr(jobs_store, "record_worker_heartbeat", _hung_heartbeat)

    class _ReleasingSupervisor(_FakeSupervisor):
        def kill_and_respawn(self):
            super().kill_and_respawn()
            release.set()  # let the timed-out executor thread drain cleanly

    supervisor = _ReleasingSupervisor({"alive": False, "last_progress_age_sec": math.inf})
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(watchdog._watchdog_loop(
            supervisor, "worker-db-hung", stop_event,
            jobs_claimable_fn=lambda: False,
            interval=0.01,
            turn_stall_timeout_sec=240.0,
            turn_absolute_timeout_sec=1500.0,
            child_liveness_timeout_sec=45.0,
            capacity_write_timeout_sec=0.01,
        ))
        for _ in range(100):
            if supervisor.kill_calls:
                break
            await asyncio.sleep(0.005)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert supervisor.kill_calls >= 1


def test_claimable_check_timeout_does_not_freeze_watchdog(monkeypatch):
    from model_api_runtime.v2 import jobs_store

    monkeypatch.setattr(
        jobs_store, "record_worker_heartbeat", lambda worker_id, **kwargs: None)
    release = threading.Event()
    calls = {"n": 0}

    def _hung_claimable():
        calls["n"] += 1
        release.wait(1.0)
        return False

    supervisor = _FakeSupervisor({"alive": True, "last_progress_age_sec": 999.0})
    stop_event = asyncio.Event()
    calls_while_blocked = []

    async def _driver():
        task = asyncio.create_task(watchdog._watchdog_loop(
            supervisor, "worker-query-hung", stop_event,
            jobs_claimable_fn=_hung_claimable,
            interval=0.005,
            turn_stall_timeout_sec=240.0,
            turn_absolute_timeout_sec=1500.0,
            child_liveness_timeout_sec=45.0,
            jobs_claimable_timeout_sec=0.01,
        ))
        await asyncio.sleep(0.06)  # several watchdog timeouts/ticks
        calls_while_blocked.append(calls["n"])
        stop_event.set()
        release.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert calls_while_blocked == [1]  # one retained in-flight DB query, no thread pile-up
    assert supervisor.kill_calls == 0
