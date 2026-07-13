"""Hosted Runtime V2 PR D, Task 5 — Half-A P0 / acceptance tests
(``docs/superpowers/plans/2026-07-13-hosted-runtime-v2-PR-D-pool-history-safety.md``).

This module re-asserts the STRONG acceptance properties Tasks 1-4 (kill_switch,
child_supervisor.ChildSupervisor, watchdog.should_kill/_watchdog_loop, the
health-derived _heartbeat_loop) are supposed to jointly guarantee, using fake
supervisors / monkeypatched jobs_store — no real turn work is ever spawned:

1. P0 — all slots stuck: capacity=0 is recorded strictly BEFORE
   kill_and_respawn() runs (so admission observes the drop before the SIGKILL
   races a fresh claim in), and the kill path never touches Genesis: a
   ``kind='genesis'`` heartbeat row's ``beat_at`` is byte-for-byte unchanged
   across the whole watchdog tick (not merely "still fresh" — literally
   untouched), and grep-level source inspection confirms watchdog.py contains
   no genesis reference at all.
2. Kill switch: real DB-backed ``kill_switch.set_turns_halted`` flips
   chat_send_core admission to 503 turns_halted and back; the ``_slot_loop``
   claim gate never calls ``claim_next_job`` while halted; the Genesis claim
   function (``db.genesis_claim_uploaded_jobs``) is provably ungated (grep at
   test time confirms no kill_switch import/reference in db.py at all, and the
   function is still callable while halted).
3. watchdog.should_kill — focused re-assertion of the three acceptance
   branches (all-stuck+claimable → kill, healthy+fresh → no kill,
   stale-but-idle → no kill) at the P0 level.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from core import enclave as core_enclave
from core import store as core_store
from hosted import chat_send_core, config_store as hosted_config_store
from model_api_runtime.v2 import jobs_store, kill_switch, watchdog, worker

from conftest import configure_model_api_route


def _seed(uid: str) -> None:
    """Mirrors tests/test_chat_send_v2_enqueue.py's `_seed` — a minimal users
    row + a configured model_api route, enough for chat_send_core's admission
    path to run without touching a real enclave/provider."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
    configure_model_api_route(
        uid, provider="anthropic", model="m", test_status="ok",
        envelope={"body_ct": "x", "nonce": "n", "K_user": "k"})


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    """Every test starts from the migration-seeded halted=false row, and the
    module cache is invalidated on both sides so no cached value nor DB state
    leaks between tests (mirrors tests/test_v2_kill_switch.py's fixture)."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_control SET turns_halted=false, updated_at=now() WHERE id=1"
        )
    kill_switch._invalidate()
    yield
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_control SET turns_halted=false, updated_at=now() WHERE id=1"
        )
    kill_switch._invalidate()


def _genesis_beat_at(worker_id: str):
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT beat_at FROM v2_worker_heartbeats WHERE worker_id=%s", (worker_id,)
        ).fetchone()
    assert row is not None, f"genesis heartbeat row for {worker_id} must exist"
    return row[0]


# ---------------------------------------------------------------------------
# P0 #1 — all slots stuck: capacity zeroes strictly before kill_and_respawn,
# and the kill path is provably Genesis-blind.
# ---------------------------------------------------------------------------

class _WedgedSupervisor:
    """Fake supervisor: the child process is alive but its progress is older
    than CHILD_LIVENESS_TIMEOUT — the "all slots stuck" wedge signal."""

    def __init__(self):
        self.kill_calls = 0

    def poll_liveness(self) -> dict:
        return {"alive": True, "last_progress_age_sec": 999.0}

    def kill_and_respawn(self) -> None:
        self.kill_calls += 1


def test_p0_all_slots_stuck_zeroes_capacity_before_kill_and_genesis_unaffected(monkeypatch):
    genesis_worker_id = "genesis-p0-worker"
    turn_worker_id = "turn-p0-worker"

    # An independently-written genesis heartbeat row — separate PK (worker_id),
    # separate `kind`. If the watchdog kill path ever touched it, its beat_at
    # would move; it must not.
    jobs_store.record_worker_heartbeat(genesis_worker_id, kind="genesis", capacity=0)
    beat_before = _genesis_beat_at(genesis_worker_id)
    assert jobs_store.genesis_worker_alive(within_sec=60) is True

    order: list[str] = []
    real_heartbeat = jobs_store.record_worker_heartbeat

    def _spy_heartbeat(worker_id, *, kind="turn", capacity=1):
        # The watchdog's own contract (watchdog.py's `_watchdog_loop` docstring
        # and Task 3's kill branch) is to write ONLY the turn worker's row with
        # capacity=0 — assert that's exactly what happens, nothing genesis-kind
        # and nothing but capacity=0.
        assert kind == "turn", f"watchdog must never write a non-turn heartbeat, got kind={kind!r}"
        assert worker_id == turn_worker_id
        assert capacity == 0
        order.append("capacity_zero")
        return real_heartbeat(worker_id, kind=kind, capacity=capacity)

    monkeypatch.setattr(jobs_store, "record_worker_heartbeat", _spy_heartbeat)

    supervisor = _WedgedSupervisor()
    _real_kill = supervisor.kill_and_respawn

    def _kill_and_respawn():
        order.append("kill_and_respawn")
        _real_kill()

    supervisor.kill_and_respawn = _kill_and_respawn  # type: ignore[method-assign]

    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(watchdog._watchdog_loop(
            supervisor, turn_worker_id, stop_event,
            jobs_claimable_fn=lambda: True,  # work is waiting -> the wedge signal is real
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

    # (a) capacity=0 was recorded, (b) kill_and_respawn ran, (c) in that order.
    assert supervisor.kill_calls >= 1
    assert "capacity_zero" in order
    assert "kill_and_respawn" in order
    assert order.index("capacity_zero") < order.index("kill_and_respawn")

    # Genesis heartbeat literally untouched — same beat_at timestamp, byte for
    # byte, not merely "still within the freshness window".
    beat_after = _genesis_beat_at(genesis_worker_id)
    assert beat_after == beat_before
    assert jobs_store.genesis_worker_alive(within_sec=60) is True

    # Static confirmation the watchdog code path never references anything
    # genesis-related — no import, no string literal, no attribute access.
    src = inspect.getsource(watchdog)
    assert "genesis" not in src.lower()


# ---------------------------------------------------------------------------
# P0 #1b — hard-timeout fix: a SINGLE wedged turn (other slots fine, so the
# freshest-progress staleness clause (b) never fires) still zeroes capacity and
# kills, via clause (c)/current_turn_age_sec. Before this fix, `should_kill`'s
# clause (c) was dead code (nothing populated `current_turn_age_sec`), so this
# acceptance item ("hard timeout -> capacity=0 -> restart turn child") was
# nominal only — a wedged turn's slot silently stayed occupied until the
# data-plane job-lease reaper marked the DB row failed (~300s later), with no
# process-level kill at all.
# ---------------------------------------------------------------------------

class _SingleWedgedTurnSupervisor:
    """Fake supervisor: the child process is alive, progress is FRESH overall
    (other slots are healthy and cycling), but one turn has been running longer
    than the hard timeout. This is precisely the case clause (b) cannot see —
    `last_progress_age_sec` stays fresh because other slots keep claiming/
    completing/idle-polling — only `current_turn_age_sec` catches it."""

    def __init__(self, current_turn_age_sec: float, order: "list[str] | None" = None):
        self._current_turn_age_sec = current_turn_age_sec
        self._order = order
        self.kill_calls = 0

    def poll_liveness(self) -> dict:
        return {
            "alive": True,
            "last_progress_age_sec": 1.0,  # fresh — other slots are fine
            "current_turn_age_sec": self._current_turn_age_sec,
        }

    def kill_and_respawn(self) -> None:
        if self._order is not None:
            self._order.append("kill_and_respawn")
        self.kill_calls += 1


def test_p0_single_wedged_turn_hard_timeout_zeroes_capacity_before_kill(monkeypatch):
    turn_worker_id = "turn-p0-hardtimeout-worker"

    order: list[str] = []
    real_heartbeat = jobs_store.record_worker_heartbeat

    def _spy_heartbeat(worker_id, *, kind="turn", capacity=1):
        assert kind == "turn"
        assert worker_id == turn_worker_id
        assert capacity == 0
        order.append("capacity_zero")
        return real_heartbeat(worker_id, kind=kind, capacity=capacity)

    monkeypatch.setattr(jobs_store, "record_worker_heartbeat", _spy_heartbeat)

    # 181s > the 180s turn_hard_timeout_sec used below.
    supervisor = _SingleWedgedTurnSupervisor(current_turn_age_sec=181.0, order=order)
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(watchdog._watchdog_loop(
            supervisor, turn_worker_id, stop_event,
            # jobs_claimable=False: nothing else is queued — clause (b) would
            # NEVER fire here (fresh last_progress_age_sec AND no claimable
            # work). Only clause (c) can produce a kill in this scenario.
            jobs_claimable_fn=lambda: False,
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

    assert supervisor.kill_calls >= 1, (
        "a single wedged turn over turn_hard_timeout_sec must trigger a kill even "
        "though the freshest progress across the pool is fresh and no other work "
        "is claimable — this is the live path clause (c) now covers"
    )
    assert "capacity_zero" in order
    assert "kill_and_respawn" in order
    assert order.index("capacity_zero") < order.index("kill_and_respawn")


def test_p0_single_wedged_turn_under_hard_timeout_does_not_kill(monkeypatch):
    """Negative control: a turn that hasn't yet crossed turn_hard_timeout_sec,
    with fresh coarse progress and no claimable work, must not be killed."""
    monkeypatch.setattr(
        jobs_store, "record_worker_heartbeat",
        lambda worker_id, **kwargs: pytest.fail("must not write capacity=0 here"))

    supervisor = _SingleWedgedTurnSupervisor(current_turn_age_sec=5.0)
    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(watchdog._watchdog_loop(
            supervisor, "turn-p0-hardtimeout-worker-ok", stop_event,
            jobs_claimable_fn=lambda: False,
            interval=0.02,
            turn_hard_timeout_sec=180.0,
            child_liveness_timeout_sec=45.0,
        ))
        await asyncio.sleep(0.1)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert supervisor.kill_calls == 0


# ---------------------------------------------------------------------------
# Kill switch acceptance: admission 503, slot-loop claim gate, Genesis ungated.
# ---------------------------------------------------------------------------

def test_kill_switch_halts_admission_then_resumes(monkeypatch):
    """(a) set_turns_halted(True) -> chat_send_core admission returns
    ({"error": "turns_halted"}, 503) for a db_action_v2 user, before anything
    is persisted; flipping back to False resumes normal (non-503) admission.
    Mirrors tests/test_chat_send_v2_enqueue.py's db_action_v2 enqueue setup."""
    uid = "u_p0_kill_switch_admission"
    _seed(uid)
    store = core_store.get_store(uid)
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")

    monkeypatch.setattr(
        chat_send_core.core_envelope, "_build_shared_envelope_for_store",
        lambda s, pt, **kw: ({"id": "u-msg-1", "body_ct": "c", "nonce": "n", "K_user": "k"}, ""),
    )
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose, **kw: b"sk-or-test",
    )
    monkeypatch.setattr(chat_send_core.agent_runtime_cutover, "resolve_driver", lambda cfg: "claude")
    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: True)
    monkeypatch.setattr(chat_send_core.jobs_store, "live_worker_capacity", lambda **kw: 1)
    monkeypatch.setattr(chat_send_core.jobs_store, "inflight_job_count", lambda: 0)
    monkeypatch.setattr(chat_send_core.jobs_store, "recent_mean_service_sec", lambda **kw: None)

    append_chat_calls = {"n": 0}
    _real_append_chat = store.append_chat

    def _append_chat_spy(*a, **k):
        append_chat_calls["n"] += 1
        raise AssertionError("append_chat must not be called while turns are halted")

    monkeypatch.setattr(store, "append_chat", _append_chat_spy)

    kill_switch.set_turns_halted(True)
    kill_switch._invalidate()

    body, status = chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi"},
    )
    assert status == 503
    assert body == {"error": "turns_halted"}
    assert append_chat_calls["n"] == 0

    # Flip back -> admission resumes (a normal, non-503 db_action_v2 enqueue).
    kill_switch.set_turns_halted(False)
    kill_switch._invalidate()
    monkeypatch.setattr(store, "append_chat", _real_append_chat)

    notified = {}
    monkeypatch.setattr(
        chat_send_core.core_wake_bus, "notify",
        lambda channel, user_id="": notified.update(channel=channel, user_id=user_id),
    )
    handle_send_called = {"n": 0}
    monkeypatch.setattr(
        chat_send_core.agent_runtime_cutover, "handle_send",
        lambda *a, **k: handle_send_called.update(n=handle_send_called["n"] + 1) or ({"status": "resident"}, 202),
    )

    body2, status2 = chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi again"},
    )
    assert status2 != 503
    assert status2 == 202
    assert body2["status"] == "processing"
    assert notified.get("channel") == "v2_jobs"


def test_kill_switch_slot_loop_claim_gate_does_not_claim_while_halted(monkeypatch):
    """(b) worker._slot_loop's claim gate: while halted, one guarded iteration
    must never call jobs_store.claim_next_job at all."""
    claim_calls = {"n": 0}

    def _claim(worker_id, lanes=None):
        claim_calls["n"] += 1
        return None

    monkeypatch.setattr(worker.jobs_store, "claim_next_job", _claim)
    monkeypatch.setattr(worker.kill_switch, "turns_halted", lambda: True)

    stop_event = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(worker._slot_loop(
            "w-p0-halted", poll_interval=0.02, stop_event=stop_event,
            deps=worker.TurnDeps(
                read_messages=lambda uid: [],
                resolve_provider=lambda uid: (None, {}),
                is_official=lambda cfg: False,
                mint_enclave_token=lambda uid: "rt",
                apply_pending_effects=lambda *a, **k: None,
            ),
        ))
        # A couple of guarded iterations' worth of wall time — long enough that
        # a claim WOULD have happened by now if the gate were broken.
        await asyncio.sleep(0.08)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_driver())
    assert claim_calls["n"] == 0


def test_kill_switch_genesis_claim_path_is_ungated(monkeypatch):
    """(c) the Genesis import claim path never consults the kill switch: static
    proof (no kill_switch reference anywhere in db.py, where
    genesis_claim_uploaded_jobs lives) plus a live proof (the claim function
    is still callable and returns normally while turns are halted)."""
    import db as db_module

    src = inspect.getsource(db_module)
    assert "kill_switch" not in src

    kill_switch.set_turns_halted(True)
    kill_switch._invalidate()
    try:
        # Must not raise / must not itself consult the halted flag.
        rows = db.genesis_claim_uploaded_jobs(limit=1)
        assert isinstance(rows, list)
    finally:
        kill_switch.set_turns_halted(False)
        kill_switch._invalidate()


# ---------------------------------------------------------------------------
# watchdog.should_kill — focused P0-level re-assertion of the three
# acceptance branches (Task 3's fuller exhaustive suite lives in
# tests/test_v2_watchdog.py; this is the acceptance-level spot-check).
# ---------------------------------------------------------------------------

def test_should_kill_true_for_all_stuck_and_claimable():
    liveness = {"alive": True, "last_progress_age_sec": 999.0}
    assert watchdog.should_kill(
        liveness, turn_hard_timeout_sec=180.0, child_liveness_timeout_sec=45.0,
        jobs_claimable=True,
    ) is True


def test_should_kill_false_for_healthy_fresh_child():
    liveness = {"alive": True, "last_progress_age_sec": 1.0}
    assert watchdog.should_kill(
        liveness, turn_hard_timeout_sec=180.0, child_liveness_timeout_sec=45.0,
        jobs_claimable=True,
    ) is False


def test_should_kill_false_for_stale_but_idle_pool():
    """Negative acceptance case: don't kill a healthy child that simply has no
    work — stale progress alone, with nothing claimable, must not trigger a
    kill."""
    liveness = {"alive": True, "last_progress_age_sec": 999.0}
    assert watchdog.should_kill(
        liveness, turn_hard_timeout_sec=180.0, child_liveness_timeout_sec=45.0,
        jobs_claimable=False,
    ) is False
