"""In-process TEE auto-sync scheduler (admin.tee_sync_scheduler).

Drives the same tee_replication.run_action a manual run would; these tests stub
run_action and assert the per-tick call sequence + skip semantics. No DB.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from admin import tee_sync_scheduler as sched  # noqa: E402
from admin import tee_replication as tr  # noqa: E402


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake_run_action(*, action, table=None, dry_run=True, confirm=None, **kw):
        recorded.append((action, table, dry_run, confirm))
        return {"ok": True, "copied": 0, "pending": 0, "errors": 0}

    monkeypatch.setattr(tr, "run_action", fake_run_action)
    return recorded


def test_replicate_tick_hits_every_ciphertext_table(calls):
    sched._sync_tick(do_reconcile=False)
    replicated = [c for c in calls if c[0] == "replicate"]
    assert [c[1] for c in replicated] == list(sched._CIPHERTEXT_TABLES)
    # non-dry-run + confirm gate carried on every replicate
    assert all(c[2] is False and c[3] == "MIGRATE" for c in replicated)
    # do_reconcile=False → no reconcile/verify
    assert not any(c[0] in ("reconcile", "verify") for c in calls)


def test_reconcile_tick_adds_reconcile_then_verify(calls):
    sched._sync_tick(do_reconcile=True)
    actions = [c[0] for c in calls]
    assert actions == ["replicate"] * 5 + ["reconcile", "verify"]
    # reconcile carries confirm=MIGRATE; verify is read-only (no confirm needed)
    reconcile = next(c for c in calls if c[0] == "reconcile")
    verify = next(c for c in calls if c[0] == "verify")
    assert reconcile[3] == "MIGRATE"
    assert verify[2] is False  # dry_run=False, verify is confirm-exempt


def test_already_running_aborts_the_whole_tick(monkeypatch):
    calls = []

    def fake(*, action, table=None, **kw):
        calls.append((action, table))
        raise tr.AlreadyRunning()

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=True)
    # first replicate raises AlreadyRunning → return immediately, no further calls
    assert calls == [("replicate", "chat_messages")]


def test_unconfigured_aborts_silently(monkeypatch):
    calls = []

    def fake(*, action, table=None, **kw):
        calls.append(action)
        raise tr.Unconfigured()

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=True)
    assert calls == ["replicate"]  # stopped on first Unconfigured


def test_one_table_error_does_not_stop_the_pass(monkeypatch):
    seen = []

    def fake(*, action, table=None, dry_run=True, confirm=None, **kw):
        seen.append(table if action == "replicate" else action)
        if table == "memory_moments":
            raise RuntimeError("enclave hiccup")
        return {"ok": True}

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=False)
    # memory_moments raised a generic error but the loop continued past it
    assert seen == list(sched._CIPHERTEXT_TABLES)


def test_start_spawns_a_daemon_thread(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, target, daemon, name):
            started["daemon"] = daemon
            started["name"] = name

        def start(self):
            started["started"] = True

    monkeypatch.setattr(sched.threading, "Thread", FakeThread)
    sched.start()
    assert started == {"daemon": True, "name": "tee-sync", "started": True}
