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
    assert result == {"considered": 1, "enqueued": 1, "skipped": 0}


def test_zero_burn_blocked_does_not_enqueue_but_still_advances():
    deps = FakeDeps(
        users=["u2"],
        decisions={"u2": {"should_wake": False, "wake_interval_sec": 1800, "block_reason": "weak_wake"}},
    )
    now = 2000.0
    result = scheduler.run_scheduler_tick(deps, now=now)

    assert deps.enqueued == []  # zero-burn: no job, no model call
    assert deps.advanced == [("u2", now + 1800)]
    assert result == {"considered": 1, "enqueued": 0, "skipped": 1}


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
    assert result == {"considered": 3, "enqueued": 2, "skipped": 1}


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
    assert result == {"considered": 3, "enqueued": 2, "skipped": 1}


def test_empty_due_users_all_zero():
    deps = FakeDeps(users=[], decisions={})
    result = scheduler.run_scheduler_tick(deps, now=123.0)

    assert deps.enqueued == []
    assert deps.advanced == []
    assert result == {"considered": 0, "enqueued": 0, "skipped": 0}


def test_module_does_not_import_forbidden_packages():
    src = inspect.getsource(scheduler)
    assert "import proactive" not in src
    assert "import hosted" not in src
    assert "import agent_runtime" not in src
