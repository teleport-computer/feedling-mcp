"""Pure unit tests for the V2 proactive scheduler tick (Task 4).

All IO is injected via a fake `deps` object — no DB, no wall clock reads
inside the module under test. Zero-burn invariant: a blocked/weak wake
decision must NEVER call enqueue_heartbeat (no job => no model call).
"""
import inspect

from model_api_runtime.v2 import scheduler


class FakeDeps:
    def __init__(self, users, decisions, *, raise_for=None):
        self._users = list(users)
        self._decisions = dict(decisions)
        self._raise_for = raise_for or set()
        self.enqueued = []
        self.advanced = []

    def due_users(self):
        return list(self._users)

    def wake_decision(self, user_id):
        if user_id in self._raise_for:
            raise RuntimeError(f"boom for {user_id}")
        return self._decisions[user_id]

    def enqueue_heartbeat(self, user_id):
        self.enqueued.append(user_id)

    def advance_heartbeat(self, user_id, next_at_epoch):
        self.advanced.append((user_id, next_at_epoch))


def test_should_wake_true_enqueues_and_advances():
    deps = FakeDeps(
        users=["u1"],
        decisions={"u1": {"should_wake": True, "wake_interval_sec": 900, "block_reason": ""}},
    )
    now = 1000.0
    result = scheduler.run_scheduler_tick(deps, now=now)

    assert deps.enqueued == ["u1"]
    assert deps.advanced == [("u1", now + 900)]
    assert result == {"considered": 1, "enqueued": 1, "skipped": 0,
                      "scheduled_fired": 0, "extraction_enqueued": 0,
                      "screen_watch_enqueued": 0}


def test_zero_burn_blocked_does_not_enqueue_but_still_advances():
    deps = FakeDeps(
        users=["u2"],
        decisions={"u2": {"should_wake": False, "wake_interval_sec": 1800, "block_reason": "weak_wake"}},
    )
    now = 2000.0
    result = scheduler.run_scheduler_tick(deps, now=now)

    assert deps.enqueued == []  # zero-burn: no job, no model call
    assert deps.advanced == [("u2", now + 1800)]
    assert result == {"considered": 1, "enqueued": 0, "skipped": 1,
                      "scheduled_fired": 0, "extraction_enqueued": 0,
                      "screen_watch_enqueued": 0}


def test_runtime_mode_block_does_not_mutate_v2_schedule():
    deps = FakeDeps(
        users=["rolled-back"],
        decisions={"rolled-back": {
            "should_wake": False,
            "wake_interval_sec": 7200,
            "block_reason": "runtime_mode",
        }},
    )

    result = scheduler.run_scheduler_tick(deps, now=2000.0)

    assert deps.enqueued == []
    assert deps.advanced == []
    assert result["skipped"] == 1


def test_final_runtime_mode_fence_blocks_enqueue_after_decision():
    deps = FakeDeps(
        users=["raced"],
        decisions={"raced": {
            "should_wake": True,
            "wake_interval_sec": 60,
            "block_reason": "",
        }},
    )
    deps.runtime_mode_enabled = lambda _uid: False

    result = scheduler.run_scheduler_tick(deps, now=2000.0)

    assert deps.enqueued == []
    assert deps.advanced == []
    assert result["skipped"] == 1


def test_mixed_batch_two_wake_one_blocked():
    deps = FakeDeps(
        users=["a", "b", "c"],
        decisions={
            "a": {"should_wake": True, "wake_interval_sec": 600, "block_reason": ""},
            "b": {"should_wake": False, "wake_interval_sec": 7200, "block_reason": "unactivated"},
            "c": {"should_wake": True, "wake_interval_sec": 300, "block_reason": ""},
        },
    )
    now = 500.0
    result = scheduler.run_scheduler_tick(deps, now=now)

    assert sorted(deps.enqueued) == ["a", "c"]
    assert set(deps.advanced) == {("a", now + 600), ("b", now + 7200), ("c", now + 300)}
    assert result == {"considered": 3, "enqueued": 2, "skipped": 1,
                      "scheduled_fired": 0, "extraction_enqueued": 0,
                      "screen_watch_enqueued": 0}


def test_per_user_error_isolation_does_not_abort_tick():
    deps = FakeDeps(
        users=["good1", "bad", "good2"],
        decisions={
            "good1": {"should_wake": True, "wake_interval_sec": 60, "block_reason": ""},
            "good2": {"should_wake": True, "wake_interval_sec": 60, "block_reason": ""},
        },
        raise_for={"bad"},
    )
    now = 10.0
    result = scheduler.run_scheduler_tick(deps, now=now)

    assert sorted(deps.enqueued) == ["good1", "good2"]
    # the erroring user must not be advanced (its wake_decision blew up before we
    # could compute next_at), but must count as considered+skipped, and must not
    # abort processing of the remaining users.
    assert ("bad", now + 60) not in deps.advanced
    assert set(deps.advanced) == {("good1", now + 60), ("good2", now + 60)}
    assert result == {"considered": 3, "enqueued": 2, "skipped": 1,
                      "scheduled_fired": 0, "extraction_enqueued": 0,
                      "screen_watch_enqueued": 0}


def test_empty_due_users_all_zero():
    deps = FakeDeps(users=[], decisions={})
    result = scheduler.run_scheduler_tick(deps, now=123.0)

    assert deps.enqueued == []
    assert deps.advanced == []
    assert result == {"considered": 0, "enqueued": 0, "skipped": 0,
                      "scheduled_fired": 0, "extraction_enqueued": 0,
                      "screen_watch_enqueued": 0}


def test_module_does_not_import_forbidden_packages():
    src = inspect.getsource(scheduler)
    assert "import proactive" not in src
    assert "import hosted" not in src
    assert "import agent_runtime" not in src


class _SchedFakeDeps:
    """The scheduler's deps are duck-typed. Heartbeat side is inert here."""

    def __init__(self, *, due_scheduled=None, fire=None):
        self._due_scheduled = due_scheduled
        self._fire = fire
        if due_scheduled is not None:
            self.due_scheduled_users = lambda: list(due_scheduled)
        if fire is not None:
            self.fire_scheduled = fire

    def due_users(self):
        return []


def test_tick_fires_due_scheduled_users_and_counts_them():
    fired = []
    deps = _SchedFakeDeps(due_scheduled=["u1", "u2"],
                          fire=lambda uid: (fired.append(uid), 1)[1])
    out = scheduler.run_scheduler_tick(deps, now=100.0)
    assert fired == ["u1", "u2"]
    assert out["scheduled_fired"] == 2


def test_tick_isolates_a_failing_scheduled_user():
    """One user's broken timer must not stop the sweep — mirrors heartbeat isolation."""
    def _fire(uid):
        if uid == "bad":
            raise RuntimeError("boom")
        return 1

    deps = _SchedFakeDeps(due_scheduled=["bad", "good"], fire=_fire)
    out = scheduler.run_scheduler_tick(deps, now=100.0)
    assert out["scheduled_fired"] == 1


def test_tick_skips_the_scheduled_sweep_when_deps_are_absent():
    """Existing FakeDeps have neither attribute; the sweep must no-op, not AttributeError."""
    deps = _SchedFakeDeps()          # no due_scheduled_users / fire_scheduled at all
    out = scheduler.run_scheduler_tick(deps, now=100.0)
    assert out["scheduled_fired"] == 0


def test_tick_sweeps_extraction_users_and_isolates_failures():
    def _tick(uid):
        if uid == "bad":
            raise RuntimeError("boom")
        return 1

    deps = _SchedFakeDeps()                     # the helper added last round
    deps.extraction_users = lambda: ["bad", "good"]
    deps.tick_extraction = _tick
    out = scheduler.run_scheduler_tick(deps, now=100.0)
    assert out["extraction_enqueued"] == 1


def test_tick_skips_extraction_sweep_when_deps_absent():
    out = scheduler.run_scheduler_tick(_SchedFakeDeps(), now=100.0)
    assert out["extraction_enqueued"] == 0


def test_tick_sweeps_screen_watch_users_and_isolates_failures():
    def _tick(uid):
        if uid == "bad":
            raise RuntimeError("boom")
        return 1

    deps = _SchedFakeDeps()                     # the duck-typed helper
    deps.screen_watch_users = lambda: ["bad", "good"]
    deps.tick_screen_watch = _tick
    out = scheduler.run_scheduler_tick(deps, now=100.0)
    assert out["screen_watch_enqueued"] == 1


def test_tick_skips_screen_watch_sweep_when_deps_absent():
    out = scheduler.run_scheduler_tick(_SchedFakeDeps(), now=100.0)
    assert out["screen_watch_enqueued"] == 0


def test_one_eligible_snapshot_filters_every_scheduler_lane():
    seen = {"eligible_calls": 0, "heartbeat": [], "scheduled": [], "extract": [], "screen": []}
    deps = _SchedFakeDeps(
        due_scheduled=["resident", "v2"],
        fire=lambda uid: seen["scheduled"].append(uid) or 1,
    )
    deps.due_users = lambda: ["resident", "v2"]
    deps.wake_decision = lambda uid: {
        "should_wake": True, "wake_interval_sec": 60, "block_reason": "",
    }
    deps.enqueue_heartbeat = lambda uid: seen["heartbeat"].append(uid)
    deps.advance_heartbeat = lambda uid, ts: None
    deps.extraction_users = lambda: ["resident", "v2"]
    deps.tick_extraction = lambda uid: seen["extract"].append(uid) or 1
    deps.screen_watch_users = lambda: ["resident", "v2"]
    deps.tick_screen_watch = lambda uid: seen["screen"].append(uid) or 1

    def _eligible():
        seen["eligible_calls"] += 1
        return ["v2"]

    deps.eligible_users = _eligible
    scheduler.run_scheduler_tick(deps, now=100.0)

    assert seen == {
        "eligible_calls": 1,
        "heartbeat": ["v2"],
        "scheduled": ["v2"],
        "extract": ["v2"],
        "screen": ["v2"],
    }
