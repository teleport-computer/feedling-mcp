import asyncio

from model_api_runtime.v2 import (
    pool_config,
    pool_supervisor,
    serve_worker,
    slot_protocol,
)


class _FakeSupervisor:
    next_pid = 1000

    def __init__(
        self,
        target,
        *,
        liveness_timeout_sec,
        spawn_args,
        broker=None,
        pool="",
        slot_id="",
    ):
        self.target = target
        self.liveness_timeout_sec = liveness_timeout_sec
        self.spawn_args = spawn_args
        self.started = 0
        self.stopped = 0
        self.restarts = 0
        self.pid = None
        self._snapshot = None

    def start(self):
        self.started += 1
        type(self).next_pid += 1
        self.pid = type(self).next_pid

    def stop(self):
        self.stopped += 1
        self.pid = None

    def kill(self):
        active = None if self._snapshot is None else self._snapshot.active_job
        self.pid = None
        self._snapshot = None
        return active

    def snapshot(self):
        return self._snapshot

    def restart_if_snapshot(self, expected):
        if self._snapshot != expected:
            return False
        self.restarts += 1
        self.kill()
        self.start()
        return True


def _fleet():
    return pool_supervisor.SlotFleet(
        pool_config.RuntimePoolConfig.from_env(),
        spawn_target=lambda: None,
        worker_id="worker",
        poll_interval=0.5,
        db_pool_max=2,
        supervisor_factory=_FakeSupervisor,
    )


def test_default_fleet_builds_and_starts_eight_one_slot_children(monkeypatch):
    for name in (
        "FEEDLING_V2_FOREGROUND_SLOTS",
        "FEEDLING_V2_WAKE_SLOTS",
        "FEEDLING_V2_HEAVY_SLOTS",
    ):
        monkeypatch.delenv(name, raising=False)
    fleet = _fleet()

    fleet.start_all()

    assert len(fleet.keys()) == 8
    assert all(fleet.supervisor(key).started == 1 for key in fleet.keys())
    assert sum("profile" in fleet.spec(key).lanes for key in fleet.keys()) == 1
    for key in fleet.keys():
        spec = fleet.spec(key)
        args = fleet.supervisor(key).spawn_args
        assert args[2] == spec.pool
        assert args[3] == spec.slot_id
        assert args[4] == tuple(sorted(spec.lanes))
        assert args[5] == 2
        assert "chat" not in spec.lanes or spec.pool == "foreground"
        assert "profile" not in spec.lanes or key == pool_supervisor.SlotKey("heavy", 0)


def test_restarting_one_slot_does_not_change_other_seven_pids():
    fleet = _fleet()
    fleet.start_all()
    target = pool_supervisor.SlotKey("heavy", 0)
    before = {key: fleet.supervisor(key).pid for key in fleet.keys()}

    fleet.supervisor(target).stop()
    fleet.supervisor(target).start()

    after = {key: fleet.supervisor(key).pid for key in fleet.keys()}
    assert after[target] != before[target]
    assert {key: pid for key, pid in after.items() if key != target} == {
        key: pid for key, pid in before.items() if key != target
    }


def test_find_claim_matches_exact_current_snapshot_owner():
    fleet = _fleet()
    key = pool_supervisor.SlotKey("heavy", 0)
    active = slot_protocol.ActiveJobIdentity(3694, "profile", "worker:heavy:0:g7")
    fleet.supervisor(key)._snapshot = slot_protocol.SlotProgress(
        "heavy-0", "g7", 10.0, 9.0, "profile.provider", active
    )

    assert fleet.find_claim(active.claimed_by) == key
    assert fleet.find_claim("worker:heavy:0:g8") is None


def test_pool_capacity_counts_only_healthy_children():
    fleet = _fleet()
    fleet.start_all()
    for index, key in enumerate(fleet.keys()):
        fleet.supervisor(key).poll_liveness = lambda healthy=index != 0: {
            "alive": healthy,
            "last_progress_age_sec": 0.0,
        }

    assert fleet.healthy_capacity("foreground", stale_sec=45.0) == 3
    assert fleet.healthy_capacity("wake", stale_sec=45.0) == 2
    assert fleet.healthy_capacity("heavy", stale_sec=45.0) == 2


def test_restart_if_snapshot_is_generation_and_job_fenced():
    fleet = _fleet()
    fleet.start_all()
    key = pool_supervisor.SlotKey("heavy", 0)
    supervisor = fleet.supervisor(key)
    active = slot_protocol.ActiveJobIdentity(3694, "profile", "worker:heavy:0:g7")
    stale = slot_protocol.SlotProgress(
        "heavy-0", "g7", 10.0, 9.0, "profile.provider", active
    )
    supervisor._snapshot = slot_protocol.SlotProgress(
        "heavy-0",
        "g8",
        11.0,
        10.0,
        "profile.provider",
        slot_protocol.ActiveJobIdentity(3695, "profile", "worker:heavy:0:g8"),
    )
    pid_before = supervisor.pid

    assert fleet.restart_if_snapshot(key, stale) is False
    assert supervisor.pid == pid_before

    current = supervisor.snapshot()
    assert fleet.restart_if_snapshot(key, current) is True
    assert supervisor.restarts == 1
    assert supervisor.pid != pid_before


def test_periodic_reconcile_restarts_only_the_invalid_exact_claim(monkeypatch):
    fleet = _fleet()
    fleet.start_all()
    valid_key = pool_supervisor.SlotKey("foreground", 0)
    invalid_key = pool_supervisor.SlotKey("heavy", 0)
    valid = slot_protocol.ActiveJobIdentity(
        3694, "chat", "worker:foreground:0:g7"
    )
    invalid = slot_protocol.ActiveJobIdentity(
        3695, "profile", "worker:heavy:0:g8"
    )
    fleet.supervisor(valid_key)._snapshot = slot_protocol.SlotProgress(
        "foreground-0", "g7", 10.0, 9.0, "chat.provider", valid
    )
    fleet.supervisor(invalid_key)._snapshot = slot_protocol.SlotProgress(
        "heavy-0", "g8", 10.0, 9.0, "profile.provider", invalid
    )
    before = {key: fleet.supervisor(key).pid for key in fleet.keys()}
    monkeypatch.setattr(
        serve_worker.jobs_store,
        "valid_reconcile_claims",
        lambda pairs: {(valid.job_id, valid.claimed_by)},
    )

    assert asyncio.run(serve_worker._reconcile_fleet_claims_once(fleet)) == 1

    after = {key: fleet.supervisor(key).pid for key in fleet.keys()}
    assert after[invalid_key] != before[invalid_key]
    assert {key: pid for key, pid in after.items() if key != invalid_key} == {
        key: pid for key, pid in before.items() if key != invalid_key
    }


def test_periodic_reconcile_ignores_durable_completion_snapshot(monkeypatch):
    fleet = _fleet()
    fleet.start_all()
    completed_key = pool_supervisor.SlotKey("wake", 0)
    invalid_key = pool_supervisor.SlotKey("heavy", 0)
    completed = slot_protocol.ActiveJobIdentity(
        5291, "scheduled", "worker:wake:0:g9"
    )
    invalid = slot_protocol.ActiveJobIdentity(
        5292, "profile", "worker:heavy:0:g8"
    )
    fleet.supervisor(completed_key)._snapshot = slot_protocol.SlotProgress(
        "wake-0", "g9", 12.0, 9.0, "durable_completion", completed
    )
    fleet.supervisor(invalid_key)._snapshot = slot_protocol.SlotProgress(
        "heavy-0", "g8", 12.0, 9.0, "profile.provider", invalid
    )
    queried = []

    def _valid_reconcile_claims(pairs):
        queried.extend(pairs)
        return set()

    monkeypatch.setattr(
        serve_worker.jobs_store, "valid_reconcile_claims", _valid_reconcile_claims
    )
    before = {key: fleet.supervisor(key).pid for key in fleet.keys()}

    assert asyncio.run(serve_worker._reconcile_fleet_claims_once(fleet)) == 1
    assert queried == [(invalid.job_id, invalid.claimed_by)]

    after = {key: fleet.supervisor(key).pid for key in fleet.keys()}
    assert after[completed_key] == before[completed_key]
    assert after[invalid_key] != before[invalid_key]


def test_periodic_reconcile_accepts_owned_terminal_before_final_pipe_signal(
    monkeypatch,
):
    """DB completion can commit before trajectory unwind reaches the final
    durable_completion pipe signal; the matching owner must remain valid."""
    fleet = _fleet()
    fleet.start_all()
    key = pool_supervisor.SlotKey("foreground", 0)
    terminal = slot_protocol.ActiveJobIdentity(
        5371, "chat", "worker:foreground:0:g7"
    )
    fleet.supervisor(key)._snapshot = slot_protocol.SlotProgress(
        "foreground-0",
        "g7",
        12.0,
        9.0,
        "turn_terminal_trajectory",
        terminal,
    )
    queried = []

    def _valid_reconcile_claims(pairs):
        queried.extend(pairs)
        return {(terminal.job_id, terminal.claimed_by)}

    monkeypatch.setattr(
        serve_worker.jobs_store,
        "valid_reconcile_claims",
        _valid_reconcile_claims,
    )
    before = fleet.supervisor(key).pid

    assert asyncio.run(serve_worker._reconcile_fleet_claims_once(fleet)) == 0
    assert queried == [(terminal.job_id, terminal.claimed_by)]
    assert fleet.supervisor(key).pid == before
