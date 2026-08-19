"""Unit tests for `child_supervisor.ChildSupervisor` (Hosted Runtime V2 PR D,
Task 2 — D1 structural split). All fake targets are module-level functions (NOT
closures/lambdas/methods) because `multiprocessing.get_context("spawn")` pickles
the target to hand it to the fresh child interpreter — anything not
module-level-picklable would blow up at `Process.start()`.

Deliberately does NOT spawn the real `turn_child.main` in these fast unit tests —
that would need a live DB/enclave and real turn plumbing, defeating the point of
testing the supervisor mechanics in isolation. A separate opt-in smoke test at the
bottom of this file spawns the real thing, gated behind an env var so it never
runs as part of the normal fast suite.
"""
from __future__ import annotations

import multiprocessing
import os
import threading
import time

import pytest

from model_api_runtime.v2 import child_supervisor as child_supervisor_module
from model_api_runtime.v2 import enclave_broker
from model_api_runtime.v2 import slot_protocol
from model_api_runtime.v2.child_supervisor import ChildSupervisor

# ---------------------------------------------------------------------------
# Module-level fake spawn targets (must be picklable for the "spawn" context).
# ---------------------------------------------------------------------------


def _progress(slot_generation, *, turn_start=None, stage="idle"):
    active_job = (
        None
        if turn_start is None
        else slot_protocol.ActiveJobIdentity(3694, "profile", "worker:heavy:0:g7")
    )
    return slot_protocol.encode_message(
        slot_protocol.SlotProgress(
            slot_id="heavy-0",
            slot_generation=slot_generation,
            monotonic_at=time.monotonic(),
            turn_start=turn_start,
            stage=stage,
            active_job=active_job,
        )
    )


def _fake_target_periodic_progress(conn, *_args) -> None:
    """Sends a progress heartbeat every ~50ms forever (until killed/pipe breaks)."""
    while True:
        try:
            conn.send(_progress(_args[-1]))
        except Exception:
            return
        time.sleep(0.05)


def _fake_target_wedge_after_one(conn, *_args) -> None:
    """Sends exactly ONE progress heartbeat, then wedges (simulates a hung turn
    slot / deadlocked event loop): the process stays alive but never makes
    progress again."""
    try:
        conn.send(_progress(_args[-1]))
    except Exception:
        pass
    time.sleep(3600)


def _fake_target_wedged_mid_turn(conn, *_args) -> None:
    """Sends exactly ONE progress message carrying a FIXED turn_start (as if a
    turn had just started claim-side), then wedges forever — simulates a slot
    stuck INSIDE `await _run_turn(...)`: it never reaches turn-completion (which
    would send turn_start=None) and never reaches another idle-poll wake either.
    Used to prove `current_turn_age_sec` keeps climbing with wall-clock time even
    though this target never sends a second message (hard-timeout fix)."""
    try:
        now = time.monotonic()
        conn.send(_progress(_args[-1], turn_start=now, stage="profile.cards.batch"))
    except Exception:
        pass
    time.sleep(3600)


def _fake_target_idle_only(conn, *_args) -> None:
    """Sends periodic progress messages with turn_start=None — simulates a slot
    that is alive and cycling (claim-miss / idle-poll wake) but never actually
    inside a turn. `current_turn_age_sec` must read 0.0 the whole time."""
    while True:
        try:
            conn.send(_progress(_args[-1]))
        except Exception:
            return
        time.sleep(0.05)


def _fake_target_pid_then_wedge(conn, pid_holder, slot_generation) -> None:
    """Publishes its own PID into a shared `multiprocessing.Value`, sends one
    progress heartbeat, then wedges — lets a test assert `kill_and_respawn()`
    actually produces a fresh process (new PID), not the same one still limping
    along."""
    pid_holder.value = os.getpid()
    try:
        conn.send(_progress(slot_generation))
    except Exception:
        pass
    time.sleep(3600)


def _fake_target_record_each_spawn(
    conn, spawn_count, spawned_pids, slot_generation
) -> None:
    """Record every actual OS child so a lost supervisor handle is observable."""
    with spawn_count.get_lock():
        index = spawn_count.value
        spawned_pids[index] = os.getpid()
        spawn_count.value += 1
    while True:
        try:
            conn.send(_progress(slot_generation))
        except Exception:
            return
        time.sleep(0.05)


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _pid_is_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # Different owner somehow reused the PID — treat as "can't tell", not dead.
        return False
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_slot_progress_codec_round_trip_and_strict_decode():
    progress = slot_protocol.SlotProgress(
        slot_id="heavy-0",
        slot_generation="g7",
        monotonic_at=123.4,
        turn_start=120.0,
        stage="profile.cards.batch",
        active_job=slot_protocol.ActiveJobIdentity(
            job_id=3694,
            lane="profile",
            claimed_by="worker:heavy:0:g7",
        ),
    )
    assert slot_protocol.decode_message(slot_protocol.encode_message(progress)) == progress
    with pytest.raises(ValueError):
        slot_protocol.decode_message(("progress", "heavy-0", 123.4, 120.0))
    with pytest.raises(ValueError):
        slot_protocol.decode_message(
            {**slot_protocol.encode_message(progress), "unexpected": True}
        )


def test_supervisor_snapshot_preserves_identity_and_discards_late_generation():
    sup = ChildSupervisor(_fake_target_idle_only, liveness_timeout_sec=5.0)
    sup._slot_generation = "g8"
    current = slot_protocol.SlotProgress(
        "heavy-0",
        "g8",
        123.4,
        120.0,
        "profile.cards.batch",
        slot_protocol.ActiveJobIdentity(3694, "profile", "worker:heavy:0:g8"),
    )
    late = slot_protocol.SlotProgress(
        "heavy-0",
        "g7",
        124.0,
        120.0,
        "profile.write",
        slot_protocol.ActiveJobIdentity(3694, "profile", "worker:heavy:0:g7"),
    )

    sup._handle_message(slot_protocol.encode_message(current))
    sup._handle_message(slot_protocol.encode_message(late))

    assert sup.snapshot() == current


def test_first_valid_child_message_marks_startup_complete(monkeypatch):
    clock = {"now": 150.0}
    monkeypatch.setattr(child_supervisor_module.time, "monotonic", lambda: clock["now"])
    sup = ChildSupervisor(_fake_target_idle_only, liveness_timeout_sec=5.0)
    sup._slot_generation = "g8"
    sup._started_at = 100.0
    sup._last_progress_at = 100.0
    sup._last_slot_progress_at = 100.0
    sup._proc = type("AliveProcess", (), {"is_alive": lambda self: True})()

    booting = sup.poll_liveness()
    assert booting["startup_complete"] is False
    assert booting["startup_age_sec"] == pytest.approx(50.0)

    sup._handle_message(slot_protocol.encode_message(current := slot_protocol.SlotProgress(
        "heavy-0", "g8", 150.0, None, "idle", None
    )))

    ready = sup.poll_liveness()
    assert ready["startup_complete"] is True
    assert ready["startup_age_sec"] == pytest.approx(50.0)
    assert sup.snapshot() == current


def test_supervisor_routes_duplex_enclave_grant_and_release_by_generation():
    parent, child = multiprocessing.Pipe(duplex=True)
    broker = enclave_broker.EnclaveBroker(
        limit=4,
        reservations={"foreground": 2, "wake": 1, "heavy": 1},
        on_grant=lambda _request: None,
    )
    sup = ChildSupervisor(
        _fake_target_idle_only,
        liveness_timeout_sec=5.0,
        broker=broker,
        pool="foreground",
        slot_id="foreground-0",
    )
    sup._slot_generation = "g7"
    sup._read_conn = parent
    broker.set_on_grant(sup.grant_enclave)
    request = enclave_broker.EnclaveRequest(
        "r1", "foreground", "foreground-0", "g7"
    )
    try:
        sup._handle_message(enclave_broker.acquire_message(request))
        assert enclave_broker.decode_grant_message(child.recv()) == ("r1", "g7")
        assert broker.snapshot()["total_granted"] == 1

        sup._handle_message(enclave_broker.release_message("r1", "g7"))
        assert broker.snapshot()["total_granted"] == 0

        sup._handle_message(
            enclave_broker.acquire_message(
                enclave_broker.EnclaveRequest(
                    "late", "foreground", "foreground-0", "g6"
                )
            )
        )
        assert broker.snapshot()["total_granted"] == 0
    finally:
        parent.close()
        child.close()


def test_healthy_child_reports_alive_with_fresh_progress():
    sup = ChildSupervisor(_fake_target_periodic_progress, liveness_timeout_sec=5.0)
    sup.start()
    try:
        assert _wait_until(lambda: sup.poll_liveness()["last_progress_age_sec"] < 1.0), (
            "expected periodic progress within a few ticks of start()"
        )
        liveness = sup.poll_liveness()
        assert liveness["alive"] is True
        assert liveness["last_progress_age_sec"] < 1.0
        # Give it another beat and confirm the age keeps resetting (not just a
        # one-time fluke from the initial start() timestamp).
        time.sleep(0.2)
        assert sup.poll_liveness()["last_progress_age_sec"] < 1.0
    finally:
        sup.stop()


def test_wedged_child_stays_alive_but_progress_goes_stale():
    liveness_timeout_sec = 0.3
    sup = ChildSupervisor(_fake_target_wedge_after_one, liveness_timeout_sec=liveness_timeout_sec)
    sup.start()
    try:
        # start() seeds _last_progress_at to avoid a false startup wedge. Wait
        # for the child's actual one-and-only progress message to replace that
        # seed; merely checking age < 1 can pass before the spawned child runs.
        with sup._lock:
            startup_seed = sup._last_progress_at
        assert _wait_until(
            lambda: sup._last_progress_at is not None
            and sup._last_progress_at != startup_seed
        )
        # Now wait until that real heartbeat—not the startup seed—is stale.
        assert _wait_until(
            lambda: sup.poll_liveness()["last_progress_age_sec"] > liveness_timeout_sec
        )
        liveness = sup.poll_liveness()
        assert liveness["alive"] is True, "the OS process is still running (it's wedged, not dead)"
        assert liveness["last_progress_age_sec"] > liveness_timeout_sec, (
            "no progress since the single initial heartbeat should read as stale"
        )
    finally:
        sup.stop()


# ---------------------------------------------------------------------------
# hard-timeout fix: current_turn_age_sec (D2 watchdog.should_kill clause (c),
# previously dead code because nothing populated this field).
# ---------------------------------------------------------------------------


def test_wedged_mid_turn_current_turn_age_grows_without_new_messages():
    """A slot stuck inside `_run_turn` sends exactly one progress message with a
    fixed turn_start, then goes silent. `current_turn_age_sec` must nonetheless
    keep growing across successive `poll_liveness()` calls, purely from
    wall-clock time passing — it does NOT depend on any further message
    arriving, which is exactly the property that makes clause (c) able to catch
    a single wedged turn that the coarse `last_progress_age_sec` alone cannot
    (other slots keep the coarse clock fresh)."""
    sup = ChildSupervisor(_fake_target_wedged_mid_turn, liveness_timeout_sec=5.0)
    sup.start()
    try:
        assert _wait_until(lambda: sup.poll_liveness()["current_turn_age_sec"] > 0.0), (
            "expected the single wedge-time progress message to seed a turn_start"
        )
        first = sup.poll_liveness()["current_turn_age_sec"]
        time.sleep(0.3)
        second = sup.poll_liveness()["current_turn_age_sec"]
        assert second > first + 0.2, (
            "current_turn_age_sec must climb with wall-clock time even though "
            "the wedged target sent no new message in between polls"
        )
        # last_progress_age_sec also goes stale (only one message was ever
        # sent) — current_turn_age_sec is the property under test here, but
        # sanity-check the two fields aren't accidentally swapped/aliased.
        liveness = sup.poll_liveness()
        assert liveness["alive"] is True
        assert liveness["last_progress_age_sec"] > 0.2
    finally:
        sup.stop()


def test_idle_slot_reports_zero_current_turn_age():
    """A slot that is alive and cycling (idle-poll wakes, turn_start=None on
    every message) must report current_turn_age_sec == 0.0 — no active turn to
    measure the age of. Guards against clause (c) misfiring on a healthy-but-
    idle child."""
    sup = ChildSupervisor(_fake_target_idle_only, liveness_timeout_sec=5.0)
    sup.start()
    try:
        assert _wait_until(lambda: sup.poll_liveness()["last_progress_age_sec"] < 1.0)
        liveness = sup.poll_liveness()
        assert liveness["current_turn_age_sec"] == 0.0
        # Give it a few more idle beats and confirm it stays at 0.0 (not just a
        # one-time fluke before the first message landed).
        time.sleep(0.2)
        assert sup.poll_liveness()["current_turn_age_sec"] == 0.0
    finally:
        sup.stop()


def test_round_and_catchup_boundaries_refresh_stall_not_absolute_age(monkeypatch):
    """Deterministic fake-clock proof for the production budget mismatch.

    Simulate ten successful 60-second provider/Capture boundaries. The
    turn is now 601s old (well past the former 180s hard kill) but only 1s
    stalled, so the supervisor must expose those as different clocks.  Once
    no further boundary arrives, the stall clock grows and becomes killable.
    """
    clock = {"now": 1_000.0}
    monkeypatch.setattr(
        child_supervisor_module.time, "monotonic", lambda: clock["now"])
    sup = ChildSupervisor(_fake_target_idle_only, liveness_timeout_sec=45.0)
    turn_start = clock["now"]
    sup._slot_generation = "g-test"
    sup._handle_message(
        slot_protocol.encode_message(
            slot_protocol.SlotProgress(
                "heavy-0",
                "g-test",
                turn_start,
                turn_start,
                "claimed",
                slot_protocol.ActiveJobIdentity(3694, "profile", "worker"),
            )
        )
    )

    for elapsed in range(60, 601, 60):
        clock["now"] = turn_start + elapsed
        sup._handle_message(
            slot_protocol.encode_message(
                slot_protocol.SlotProgress(
                    "heavy-0",
                    "g-test",
                    clock["now"],
                    turn_start,
                    "profile.cards.batch",
                    slot_protocol.ActiveJobIdentity(3694, "profile", "worker"),
                )
            )
        )

    clock["now"] = turn_start + 601.0
    progressing = sup.poll_liveness()
    assert progressing["current_turn_age_sec"] == pytest.approx(601.0)
    assert progressing["current_turn_stall_age_sec"] == pytest.approx(1.0)

    # Event-loop liveness is a separate signal and cannot keep a wedged turn
    # alive.  It refreshes the coarse process clock, not this slot's stall age.
    clock["now"] = turn_start + 850.0
    sup._handle_message(
        slot_protocol.encode_message(
            slot_protocol.LoopHeartbeat("g-test", clock["now"])
        )
    )
    wedged = sup.poll_liveness()
    assert wedged["last_progress_age_sec"] == pytest.approx(0.0)
    assert wedged["event_loop_heartbeat_age_sec"] == pytest.approx(0.0)
    assert wedged["last_slot_progress_age_sec"] == pytest.approx(250.0)
    assert wedged["active_turn_count"] == 1
    assert wedged["current_turn_age_sec"] == pytest.approx(850.0)
    assert wedged["current_turn_stall_age_sec"] == pytest.approx(250.0)


def test_split_kill_then_start_replaces_wedged_child_with_a_fresh_pid():
    pid_holder = multiprocessing.Value("i", 0)
    sup = ChildSupervisor(
        _fake_target_pid_then_wedge, liveness_timeout_sec=0.3, spawn_args=(pid_holder,))
    sup.start()
    try:
        assert _wait_until(lambda: pid_holder.value != 0), "child never published its PID"
        old_pid = pid_holder.value
        pid_holder.value = 0  # so we can unambiguously detect the respawned child's PID below

        assert sup.kill() is None

        assert _wait_until(lambda: _pid_is_dead(old_pid), timeout=5.0), (
            "old (wedged) child should be SIGKILLed and reaped by kill()"
        )
        sup.start()
        assert _wait_until(lambda: pid_holder.value != 0), "respawned child never published its PID"
        new_pid = pid_holder.value
        assert new_pid != old_pid

        liveness = sup.poll_liveness()
        assert liveness["alive"] is True
        assert liveness["last_progress_age_sec"] < 1.0
    finally:
        sup.stop()


def test_concurrent_fenced_restarts_spawn_exactly_one_replacement():
    """Two owners of one stale snapshot must not create an untracked child."""
    spawn_count = multiprocessing.Value("i", 0)
    spawned_pids = multiprocessing.Array("i", 4)
    sup = ChildSupervisor(
        _fake_target_record_each_spawn,
        liveness_timeout_sec=5.0,
        spawn_args=(spawn_count, spawned_pids),
    )
    sup.start()
    gate = threading.Barrier(3)
    results: list[bool] = []

    def _restart() -> None:
        gate.wait()
        results.append(sup.restart_if_snapshot(expected))

    try:
        assert _wait_until(lambda: sup.snapshot() is not None)
        expected = sup.snapshot()
        assert expected is not None
        old_pid = spawned_pids[0]
        threads = [threading.Thread(target=_restart) for _ in range(2)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(10.0)
            assert not thread.is_alive()

        assert sorted(results) == [False, True]
        assert _wait_until(lambda: spawn_count.value == 2)
        assert _pid_is_dead(old_pid)
        assert sup.poll_liveness()["alive"] is True
    finally:
        sup.stop()
        for pid in spawned_pids[: spawn_count.value]:
            assert _wait_until(lambda pid=pid: _pid_is_dead(pid))


def test_duplicate_start_does_not_overwrite_a_live_child():
    """A stray start call must not make the existing child untrackable."""
    spawn_count = multiprocessing.Value("i", 0)
    spawned_pids = multiprocessing.Array("i", 2)
    sup = ChildSupervisor(
        _fake_target_record_each_spawn,
        liveness_timeout_sec=5.0,
        spawn_args=(spawn_count, spawned_pids),
    )
    sup.start()
    try:
        assert _wait_until(lambda: spawn_count.value == 1)
        first_pid = spawned_pids[0]

        sup.start()
        time.sleep(0.1)

        assert spawn_count.value == 1
        assert spawned_pids[0] == first_pid
        assert sup.poll_liveness()["alive"] is True
    finally:
        sup.stop()
        assert _wait_until(lambda: _pid_is_dead(spawned_pids[0]))


def test_stop_invalidates_snapshot_so_queued_restart_cannot_resurrect_slot():
    spawn_count = multiprocessing.Value("i", 0)
    spawned_pids = multiprocessing.Array("i", 2)
    sup = ChildSupervisor(
        _fake_target_record_each_spawn,
        liveness_timeout_sec=5.0,
        spawn_args=(spawn_count, spawned_pids),
    )
    sup.start()
    assert _wait_until(lambda: sup.snapshot() is not None)
    expected = sup.snapshot()
    assert expected is not None

    sup.stop()

    assert sup.snapshot() is None
    assert sup.restart_if_snapshot(expected) is False
    time.sleep(0.1)
    assert spawn_count.value == 1
    assert _wait_until(lambda: _pid_is_dead(spawned_pids[0]))


class _UnkillableProcess:
    pid = 424242

    def __init__(self) -> None:
        self.kill_calls = 0
        self.join_calls = 0
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def kill(self) -> None:
        self.kill_calls += 1

    def join(self, _timeout: float) -> None:
        self.join_calls += 1


def _supervisor_with_unkillable_process(*, broker=None):
    sup = ChildSupervisor(
        _fake_target_periodic_progress,
        liveness_timeout_sec=5.0,
        broker=broker,
    )
    active = slot_protocol.ActiveJobIdentity(3694, "profile", "worker:heavy:0:g7")
    expected = slot_protocol.SlotProgress(
        "heavy-0", "g7", 10.0, 9.0, "profile.provider", active
    )
    proc = _UnkillableProcess()
    sup._proc = proc
    sup._slot_generation = "g7"
    sup._snapshot = expected
    return sup, proc, expected


def test_fenced_restart_does_not_spawn_until_old_process_is_confirmed_dead():
    sup, proc, expected = _supervisor_with_unkillable_process()
    starts = []
    sup._start_locked = lambda: starts.append("start")

    assert sup.restart_if_snapshot(expected, join_timeout=0.0) is False
    assert starts == []
    assert sup._proc is proc
    assert proc.kill_calls == 1


def test_watchdog_respawn_does_not_spawn_while_old_process_survives_sigkill():
    sup, proc, _expected = _supervisor_with_unkillable_process()
    starts = []
    sup._start_locked = lambda: starts.append("start")

    sup.kill_and_respawn(join_timeout=0.0)

    assert starts == []
    assert sup._proc is proc
    assert proc.kill_calls == 1


def test_kill_for_recovery_reports_unconfirmed_termination():
    sup, proc, expected = _supervisor_with_unkillable_process()

    outcome = sup.kill_for_recovery(join_timeout=0.0)

    assert outcome.terminated is False
    assert outcome.active_job == expected.active_job
    assert sup._proc is proc


def test_failed_fenced_kill_retains_broker_generation_until_process_dies():
    class _Broker:
        def __init__(self):
            self.dropped = []

        def drop_generation(self, generation):
            self.dropped.append(generation)

    broker = _Broker()
    sup, proc, expected = _supervisor_with_unkillable_process(broker=broker)

    assert sup.restart_if_snapshot(expected, join_timeout=0.0) is False
    assert broker.dropped == []

    proc.alive = False
    sup.kill(join_timeout=0.0)
    assert "g7" in broker.dropped


def test_stop_cleanly_joins_and_reports_not_alive():
    sup = ChildSupervisor(_fake_target_periodic_progress, liveness_timeout_sec=5.0)
    sup.start()
    assert _wait_until(lambda: sup.poll_liveness()["alive"] is True)

    sup.stop()

    liveness = sup.poll_liveness()
    assert liveness["alive"] is False


def test_poll_liveness_before_start_is_not_alive_with_infinite_age():
    sup = ChildSupervisor(_fake_target_periodic_progress, liveness_timeout_sec=5.0)
    liveness = sup.poll_liveness()
    assert liveness["alive"] is False
    assert liveness["last_progress_age_sec"] == float("inf")


def test_stop_is_a_noop_safe_to_call_without_start():
    sup = ChildSupervisor(_fake_target_periodic_progress, liveness_timeout_sec=5.0)
    sup.stop()  # must not raise
    assert sup.poll_liveness()["alive"] is False


# ---------------------------------------------------------------------------
# Opt-in real-spawn smoke test — NOT part of the fast unit path (see module
# docstring). Spawns the actual `turn_child.main` (needs a real DATABASE_URL,
# which conftest.py already exports for the whole suite) but enqueues/claims no
# job — it should come up, idle-poll, emit a progress heartbeat, and die cleanly
# on SIGTERM within a few seconds. Opt in with FEEDLING_V2_RUN_CHILD_SMOKE=1.
# ---------------------------------------------------------------------------

_RUN_CHILD_SMOKE = os.environ.get("FEEDLING_V2_RUN_CHILD_SMOKE", "").strip().lower() in {
    "1", "true", "yes", "on",
}


@pytest.mark.skipif(
    not _RUN_CHILD_SMOKE,
    reason="opt-in real-process smoke test; set FEEDLING_V2_RUN_CHILD_SMOKE=1 to run "
           "(spawns the real turn_child.main against DATABASE_URL — no real turns claimed)",
)
def test_turn_child_main_is_importable_spawnable_and_exits_on_sigterm():
    from model_api_runtime.v2 import turn_child

    assert callable(turn_child.main)

    ctx = multiprocessing.get_context("spawn")
    read_conn, write_conn = multiprocessing.Pipe(duplex=False)
    proc = ctx.Process(
        target=turn_child.main, args=(write_conn, "test-child-smoke", 0.1), daemon=True)
    proc.start()
    write_conn.close()
    try:
        assert _wait_until(lambda: read_conn.poll(0.1), timeout=15.0), (
            "real turn_child.main never sent a progress heartbeat within 15s"
        )
        proc.terminate()
        proc.join(10.0)
        assert not proc.is_alive(), "turn_child.main did not drain within 10s of SIGTERM"
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(2.0)
        read_conn.close()
