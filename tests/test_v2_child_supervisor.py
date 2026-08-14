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
import time

import pytest

from model_api_runtime.v2 import child_supervisor as child_supervisor_module
from model_api_runtime.v2.child_supervisor import ChildSupervisor

# ---------------------------------------------------------------------------
# Module-level fake spawn targets (must be picklable for the "spawn" context).
# ---------------------------------------------------------------------------


def _fake_target_periodic_progress(conn, *_args) -> None:
    """Sends a progress heartbeat every ~50ms forever (until killed/pipe breaks)."""
    while True:
        try:
            conn.send(("progress", 0, time.monotonic()))
        except Exception:
            return
        time.sleep(0.05)


def _fake_target_wedge_after_one(conn, *_args) -> None:
    """Sends exactly ONE progress heartbeat, then wedges (simulates a hung turn
    slot / deadlocked event loop): the process stays alive but never makes
    progress again."""
    try:
        conn.send(("progress", 0, time.monotonic()))
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
        conn.send(("progress", 0, now, now))
    except Exception:
        pass
    time.sleep(3600)


def _fake_target_idle_only(conn, *_args) -> None:
    """Sends periodic progress messages with turn_start=None — simulates a slot
    that is alive and cycling (claim-miss / idle-poll wake) but never actually
    inside a turn. `current_turn_age_sec` must read 0.0 the whole time."""
    while True:
        try:
            conn.send(("progress", 0, time.monotonic(), None))
        except Exception:
            return
        time.sleep(0.05)


def _fake_target_pid_then_wedge(conn, pid_holder) -> None:
    """Publishes its own PID into a shared `multiprocessing.Value`, sends one
    progress heartbeat, then wedges — lets a test assert `kill_and_respawn()`
    actually produces a fresh process (new PID), not the same one still limping
    along."""
    pid_holder.value = os.getpid()
    try:
        conn.send(("progress", 0, time.monotonic()))
    except Exception:
        pass
    time.sleep(3600)


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

    Simulate ten successful 60-second provider/compaction boundaries.  The
    turn is now 601s old (well past the former 180s hard kill) but only 1s
    stalled, so the supervisor must expose those as different clocks.  Once
    no further boundary arrives, the stall clock grows and becomes killable.
    """
    clock = {"now": 1_000.0}
    monkeypatch.setattr(
        child_supervisor_module.time, "monotonic", lambda: clock["now"])
    sup = ChildSupervisor(_fake_target_idle_only, liveness_timeout_sec=45.0)
    turn_start = clock["now"]
    sup._handle_message(("progress", 3, turn_start, turn_start))

    for elapsed in range(60, 601, 60):
        clock["now"] = turn_start + elapsed
        sup._handle_message(("progress", 3, clock["now"], turn_start))

    clock["now"] = turn_start + 601.0
    progressing = sup.poll_liveness()
    assert progressing["current_turn_age_sec"] == pytest.approx(601.0)
    assert progressing["current_turn_stall_age_sec"] == pytest.approx(1.0)

    # Event-loop liveness is a separate signal and cannot keep a wedged turn
    # alive.  It refreshes the coarse process clock, not this slot's stall age.
    clock["now"] = turn_start + 850.0
    sup._handle_message(("loop_heartbeat", clock["now"]))
    wedged = sup.poll_liveness()
    assert wedged["last_progress_age_sec"] == pytest.approx(0.0)
    assert wedged["event_loop_heartbeat_age_sec"] == pytest.approx(0.0)
    assert wedged["last_slot_progress_age_sec"] == pytest.approx(250.0)
    assert wedged["active_turn_count"] == 1
    assert wedged["current_turn_age_sec"] == pytest.approx(850.0)
    assert wedged["current_turn_stall_age_sec"] == pytest.approx(250.0)


def test_kill_and_respawn_replaces_wedged_child_with_a_fresh_pid():
    pid_holder = multiprocessing.Value("i", 0)
    sup = ChildSupervisor(
        _fake_target_pid_then_wedge, liveness_timeout_sec=0.3, spawn_args=(pid_holder,))
    sup.start()
    try:
        assert _wait_until(lambda: pid_holder.value != 0), "child never published its PID"
        old_pid = pid_holder.value
        pid_holder.value = 0  # so we can unambiguously detect the respawned child's PID below

        sup.kill_and_respawn()

        assert _wait_until(lambda: _pid_is_dead(old_pid), timeout=5.0), (
            "old (wedged) child should be SIGKILLed and reaped by kill_and_respawn()"
        )
        assert _wait_until(lambda: pid_holder.value != 0), "respawned child never published its PID"
        new_pid = pid_holder.value
        assert new_pid != old_pid

        liveness = sup.poll_liveness()
        assert liveness["alive"] is True
        assert liveness["last_progress_age_sec"] < 1.0
    finally:
        sup.stop()


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
