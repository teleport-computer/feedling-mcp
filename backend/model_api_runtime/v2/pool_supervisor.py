"""Parent-owned registry for Runtime V2's one-process-per-slot fleet."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Callable

from model_api_runtime.v2 import (
    child_supervisor,
    enclave_broker,
    pool_config,
    slot_protocol,
)


@dataclass(frozen=True, order=True)
class SlotKey:
    pool: pool_config.PoolName
    index: int


class SlotFleet:
    def __init__(
        self,
        config: pool_config.RuntimePoolConfig,
        *,
        spawn_target: Callable,
        worker_id: str,
        poll_interval: float,
        db_pool_max: int = 2,
        broker: enclave_broker.EnclaveBroker | None = None,
        supervisor_factory=child_supervisor.ChildSupervisor,
    ) -> None:
        self.config = config
        self._broker = broker
        self._lock = threading.RLock()
        self._specs: dict[SlotKey, pool_config.SlotSpec] = {}
        self._supervisors: dict[SlotKey, child_supervisor.ChildSupervisor] = {}
        for spec in config.slots:
            key = SlotKey(spec.pool, spec.index)
            self._specs[key] = spec
            self._supervisors[key] = supervisor_factory(
                spawn_target,
                liveness_timeout_sec=spec.stall_budget_sec,
                spawn_args=(
                    str(worker_id),
                    float(poll_interval),
                    spec.pool,
                    spec.slot_id,
                    tuple(sorted(spec.lanes)),
                    int(db_pool_max),
                ),
                broker=broker,
                pool=spec.pool,
                slot_id=spec.slot_id,
            )
        if broker is not None:
            broker.set_on_grant(self._grant_enclave)

    def keys(self) -> tuple[SlotKey, ...]:
        return tuple(self._supervisors)

    def spec(self, key: SlotKey) -> pool_config.SlotSpec:
        return self._specs[key]

    def supervisor(self, key: SlotKey):
        return self._supervisors[key]

    def start_all(self) -> None:
        for key in self.keys():
            self._supervisors[key].start()

    def stop_all(self) -> None:
        for key in reversed(self.keys()):
            self._supervisors[key].stop()

    def snapshots(self) -> dict[SlotKey, slot_protocol.SlotProgress | None]:
        with self._lock:
            return {key: supervisor.snapshot() for key, supervisor in self._supervisors.items()}

    def find_claim(self, claimed_by: str) -> SlotKey | None:
        for key, snapshot in self.snapshots().items():
            if (
                snapshot is not None
                and snapshot.active_job is not None
                and snapshot.active_job.claimed_by == str(claimed_by)
            ):
                return key
        return None

    def restart_if_snapshot(
        self,
        key: SlotKey,
        expected: slot_protocol.SlotProgress,
    ) -> bool:
        """Restart one child only if its full generation/job snapshot matches."""
        supervisor = self._supervisors[key]
        restart_if_snapshot = getattr(supervisor, "restart_if_snapshot", None)
        if callable(restart_if_snapshot):
            return bool(restart_if_snapshot(expected))
        else:
            # Narrow compatibility for simple unit-test doubles. Production
            # supervisors always own the atomic lifecycle operation above.
            if supervisor.snapshot() != expected:
                return False
            supervisor.kill()
            supervisor.start()
            return True

    def healthy_capacity(self, pool: pool_config.PoolName, *, stale_sec: float) -> int:
        healthy = 0
        for key, supervisor in self._supervisors.items():
            if key.pool != pool:
                continue
            liveness = supervisor.poll_liveness()
            age = float(liveness.get("last_progress_age_sec", math.inf))
            if bool(liveness.get("alive")) and age <= float(stale_sec):
                healthy += 1
        return healthy

    def _grant_enclave(self, request: enclave_broker.EnclaveRequest) -> None:
        for supervisor in self._supervisors.values():
            supervisor.grant_enclave(request)

    def broker_snapshot(self) -> dict[str, object]:
        if self._broker is None:
            return {
                "limit": 0,
                "total_granted": 0,
                "granted": {pool: 0 for pool in ("foreground", "wake", "heavy")},
                "waiting": {pool: 0 for pool in ("foreground", "wake", "heavy")},
            }
        return self._broker.snapshot()
