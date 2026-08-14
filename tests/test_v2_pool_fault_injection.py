"""Real-process fault injection for Runtime V2's three-pool topology."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import queue
import threading
import time

from model_api_runtime.v2 import (
    child_supervisor,
    enclave_broker,
    pool_config,
    pool_supervisor,
    slot_protocol,
    watchdog,
)


def _wait_until(predicate, *, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        wake.wait(0.02)
    return bool(predicate())


def _fleet_fault_target(
    conn,
    _worker_id,
    _poll_interval,
    pool,
    slot_id,
    _lanes,
    _db_pool_max,
    events,
    slot_generation,
) -> None:
    events.put(("started", slot_id, slot_generation, os.getpid(), time.monotonic()))
    if slot_id == "heavy-0":
        active = slot_protocol.ActiveJobIdentity(
            99001, "profile", f"fault:{slot_id}:{slot_generation}"
        )
        now = time.monotonic()
        conn.send(
            slot_protocol.encode_message(
                slot_protocol.SlotProgress(
                    slot_id,
                    slot_generation,
                    now,
                    now,
                    "profile_provider_request:1",
                    active,
                )
            )
        )
        events.put(("claimed", slot_id, slot_generation, os.getpid(), time.monotonic()))
        threading.Event().wait(3600)
        return

    idle = slot_protocol.SlotProgress(
        slot_id, slot_generation, time.monotonic(), None, "idle", None
    )
    conn.send(slot_protocol.encode_message(idle))
    wake = threading.Event()
    while True:
        try:
            conn.send(
                slot_protocol.encode_message(
                    slot_protocol.LoopHeartbeat(
                        slot_generation, time.monotonic()
                    )
                )
            )
        except (BrokenPipeError, EOFError, OSError):
            return
        wake.wait(0.05)


def _enclave_holder_target(
    conn,
    pool,
    slot_id,
    events,
    slot_generation,
) -> None:
    request = enclave_broker.EnclaveRequest(
        request_id=f"{slot_id}:{slot_generation}",
        pool=pool,
        slot_id=slot_id,
        slot_generation=slot_generation,
    )
    conn.send(enclave_broker.acquire_message(request))
    events.put(("requested", slot_id, slot_generation, os.getpid()))
    threading.Event().wait(3600)


def _pid(supervisor) -> int:
    with supervisor._lock:
        return int(supervisor._proc.pid)


def test_hung_profile_watchdog_recovers_before_replacement_and_preserves_siblings(
    monkeypatch,
):
    ctx = multiprocessing.get_context("spawn")
    events = ctx.Queue()

    def factory(target, **kwargs):
        kwargs["spawn_args"] = (*kwargs["spawn_args"], events)
        return child_supervisor.ChildSupervisor(target, **kwargs)

    fleet = pool_supervisor.SlotFleet(
        pool_config.RuntimePoolConfig.from_env(),
        spawn_target=_fleet_fault_target,
        worker_id="fault-worker",
        poll_interval=0.01,
        supervisor_factory=factory,
    )
    fleet.start_all()
    heavy_key = pool_supervisor.SlotKey("heavy", 0)
    heavy = fleet.supervisor(heavy_key)
    try:
        assert _wait_until(
            lambda: heavy.snapshot() is not None
            and heavy.snapshot().active_job is not None
        )
        assert fleet.healthy_capacity("foreground", stale_sec=2.0) == 4
        assert all(
            "chat" in fleet.spec(pool_supervisor.SlotKey("foreground", index)).lanes
            for index in range(4)
        )
        before = {key: _pid(fleet.supervisor(key)) for key in fleet.keys()}
        old_generation = heavy.snapshot().slot_generation
        stop = asyncio.Event()
        loop_holder = {}

        monkeypatch.setattr(
            watchdog.jobs_store, "record_worker_heartbeat", lambda *args, **kwargs: None
        )

        def recover(**kwargs):
            assert kwargs == {
                "job_id": 99001,
                "claimed_by": f"fault:heavy-0:{old_generation}",
                "reason": "slot_watchdog_timeout",
            }
            events.put(("recovered", "heavy-0", old_generation, os.getpid(), time.monotonic()))
            loop_holder["loop"].call_soon_threadsafe(stop.set)
            return {"recovery": "requeued"}

        monkeypatch.setattr(watchdog.jobs_store, "recover_killed_job", recover)
        assert _wait_until(
            lambda: heavy.poll_liveness()["current_turn_stall_age_sec"] > 0.05
        )

        async def drive_watchdog():
            loop_holder["loop"] = asyncio.get_running_loop()
            await watchdog._watchdog_loop(
                heavy,
                "fault-worker:heavy",
                stop,
                jobs_claimable_fn=lambda: False,
                interval=0.01,
                child_liveness_timeout_sec=100.0,
                turn_stall_timeout_sec=0.05,
                turn_absolute_timeout_sec=100.0,
                pool="heavy",
            )

        asyncio.run(drive_watchdog())
        assert _wait_until(
            lambda: heavy.snapshot() is not None
            and heavy.snapshot().slot_generation != old_generation
        )
        after = {key: _pid(fleet.supervisor(key)) for key in fleet.keys()}

        assert after[heavy_key] != before[heavy_key]
        assert {key: pid for key, pid in after.items() if key != heavy_key} == {
            key: pid for key, pid in before.items() if key != heavy_key
        }

        observed = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not (
            any(item[0] == "recovered" for item in observed)
            and sum(item[0] == "claimed" for item in observed) >= 2
        ):
            try:
                observed.append(events.get(timeout=0.2))
            except queue.Empty:
                pass
        recovery_at = next(item[4] for item in observed if item[0] == "recovered")
        replacement_claim_at = max(item[4] for item in observed if item[0] == "claimed")
        assert recovery_at <= replacement_claim_at
    finally:
        fleet.stop_all()
        events.close()


def test_child_death_releases_enclave_generation_and_fifth_caller_advances():
    ctx = multiprocessing.get_context("spawn")
    events = ctx.Queue()
    broker = enclave_broker.EnclaveBroker(
        limit=4,
        reservations={"foreground": 2, "wake": 1, "heavy": 1},
        on_grant=lambda _request: None,
    )
    supervisors = []
    for index in range(5):
        slot_id = f"foreground-{index}"
        supervisors.append(
            child_supervisor.ChildSupervisor(
                _enclave_holder_target,
                liveness_timeout_sec=5.0,
                spawn_args=("foreground", slot_id, events),
                broker=broker,
                pool="foreground",
                slot_id=slot_id,
            )
        )
    broker.set_on_grant(
        lambda request: [supervisor.grant_enclave(request) for supervisor in supervisors]
    )
    for supervisor in supervisors:
        supervisor.start()
    try:
        assert _wait_until(
            lambda: broker.snapshot()["total_granted"] == 4
            and broker.snapshot()["waiting"]["foreground"] == 1
        )
        with broker._lock:
            granted_generation = next(iter(broker._granted.values())).slot_generation
        granted_supervisor = next(
            supervisor
            for supervisor in supervisors
            if supervisor._slot_generation == granted_generation
        )

        granted_supervisor.kill()

        assert _wait_until(
            lambda: broker.snapshot()["total_granted"] == 4
            and broker.snapshot()["waiting"]["foreground"] == 0
        )
        assert broker.snapshot()["granted"] == {
            "foreground": 4,
            "wake": 0,
            "heavy": 0,
        }
    finally:
        for supervisor in supervisors:
            supervisor.stop(drain_timeout=0.2, kill_timeout=0.2)
        events.close()
