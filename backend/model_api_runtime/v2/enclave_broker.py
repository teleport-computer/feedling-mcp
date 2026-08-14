"""Instance-wide Enclave admission for Runtime V2's isolated slot children."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from model_api_runtime.v2.pool_config import PoolName


_POOLS: tuple[PoolName, ...] = ("foreground", "wake", "heavy")
_BORROW_PRIORITY = {pool: index for index, pool in enumerate(_POOLS)}


def acquire_message(request: EnclaveRequest) -> dict[str, object]:
    return {
        "v": 1,
        "t": "enclave_acquire",
        "request_id": request.request_id,
        "pool": request.pool,
        "slot_id": request.slot_id,
        "slot_generation": request.slot_generation,
    }


def release_message(
    request_id: str, slot_generation: str, *, cancel: bool = False
) -> dict[str, object]:
    return {
        "v": 1,
        "t": "enclave_cancel" if cancel else "enclave_release",
        "request_id": str(request_id),
        "slot_generation": str(slot_generation),
    }


def grant_message(request_id: str, slot_generation: str) -> dict[str, object]:
    return {
        "v": 1,
        "t": "enclave_granted",
        "request_id": str(request_id),
        "slot_generation": str(slot_generation),
    }


def decode_child_message(payload: Any) -> tuple[str, EnclaveRequest | tuple[str, str]] | None:
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    kind = payload.get("t")
    if kind == "enclave_acquire" and set(payload) == {
        "v", "t", "request_id", "pool", "slot_id", "slot_generation"
    }:
        return (
            "acquire",
            EnclaveRequest(
                request_id=str(payload["request_id"]),
                pool=payload["pool"],
                slot_id=str(payload["slot_id"]),
                slot_generation=str(payload["slot_generation"]),
            ),
        )
    if kind in {"enclave_release", "enclave_cancel"} and set(payload) == {
        "v", "t", "request_id", "slot_generation"
    }:
        return (
            "cancel" if kind == "enclave_cancel" else "release",
            (str(payload["request_id"]), str(payload["slot_generation"])),
        )
    return None


def decode_grant_message(payload: Any) -> tuple[str, str] | None:
    if not isinstance(payload, dict) or set(payload) != {
        "v", "t", "request_id", "slot_generation"
    }:
        return None
    if payload.get("v") != 1 or payload.get("t") != "enclave_granted":
        return None
    return str(payload["request_id"]), str(payload["slot_generation"])


@dataclass(frozen=True)
class EnclaveRequest:
    request_id: str
    pool: PoolName
    slot_id: str
    slot_generation: str

    def __post_init__(self) -> None:
        if not self.request_id or not self.slot_id or not self.slot_generation:
            raise ValueError("enclave request identity must be non-empty")
        if self.pool not in _POOLS:
            raise ValueError("invalid enclave request pool")


class EnclaveBroker:
    """Allocate one instance-wide permit set with pool-aware borrowing.

    Reservations are guarantees while a pool waits, not permanently idle
    partitions: any pool may borrow unused permits.  Once capacity is released,
    queued borrowers are considered in Foreground/Wake/Heavy order and FIFO
    within a pool.  This returns borrowed permits to an active reserved pool
    without reducing total utilization while reservations are idle.

    Every grant is owned by ``(request_id, slot_generation)``.  Dropping a dead
    generation atomically removes its waiters and releases all of its grants;
    late release/cancel messages from that generation are harmless no-ops.
    """

    def __init__(
        self,
        *,
        limit: int,
        reservations: Mapping[PoolName, int],
        on_grant: Callable[[EnclaveRequest], None],
    ) -> None:
        self.limit = int(limit)
        self.reservations = {
            pool: int(reservations.get(pool, 0)) for pool in _POOLS
        }
        if self.limit <= 0:
            raise ValueError("enclave broker limit must be positive")
        if any(value < 0 for value in self.reservations.values()):
            raise ValueError("enclave reservations must be non-negative")
        if sum(self.reservations.values()) > self.limit:
            raise ValueError("enclave reservations exceed limit")
        self._on_grant = on_grant
        self._lock = threading.RLock()
        self._granted: dict[str, EnclaveRequest] = {}
        self._waiters: list[tuple[int, EnclaveRequest]] = []
        self._sequence = 0

    def request(self, request: EnclaveRequest) -> bool:
        callbacks: list[EnclaveRequest]
        with self._lock:
            existing = self._granted.get(request.request_id)
            if existing is not None:
                return existing == request
            if any(item.request_id == request.request_id for _seq, item in self._waiters):
                return False
            self._sequence += 1
            self._waiters.append((self._sequence, request))
            callbacks = self._drain_locked()
        self._notify(callbacks)
        return request.request_id in {item.request_id for item in callbacks}

    def set_on_grant(self, callback: Callable[[EnclaveRequest], None]) -> None:
        with self._lock:
            self._on_grant = callback

    def release(self, request_id: str, slot_generation: str) -> None:
        callbacks: list[EnclaveRequest] = []
        with self._lock:
            current = self._granted.get(str(request_id))
            if current is None or current.slot_generation != str(slot_generation):
                return
            self._granted.pop(str(request_id), None)
            callbacks = self._drain_locked()
        self._notify(callbacks)

    def cancel(self, request_id: str, slot_generation: str) -> bool:
        removed = False
        callbacks: list[EnclaveRequest] = []
        with self._lock:
            kept = []
            for sequence, request in self._waiters:
                if (
                    request.request_id == str(request_id)
                    and request.slot_generation == str(slot_generation)
                ):
                    removed = True
                else:
                    kept.append((sequence, request))
            self._waiters = kept
            current = self._granted.get(str(request_id))
            if current is not None and current.slot_generation == str(slot_generation):
                self._granted.pop(str(request_id), None)
                removed = True
            if removed:
                callbacks = self._drain_locked()
        self._notify(callbacks)
        return removed

    def drop_generation(self, slot_generation: str) -> None:
        generation = str(slot_generation)
        callbacks: list[EnclaveRequest]
        with self._lock:
            self._granted = {
                request_id: request
                for request_id, request in self._granted.items()
                if request.slot_generation != generation
            }
            self._waiters = [
                (sequence, request)
                for sequence, request in self._waiters
                if request.slot_generation != generation
            ]
            callbacks = self._drain_locked()
        self._notify(callbacks)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            granted = Counter(request.pool for request in self._granted.values())
            waiting = Counter(request.pool for _sequence, request in self._waiters)
            return {
                "limit": self.limit,
                "total_granted": len(self._granted),
                "granted": {pool: int(granted[pool]) for pool in _POOLS},
                "waiting": {pool: int(waiting[pool]) for pool in _POOLS},
            }

    def _drain_locked(self) -> list[EnclaveRequest]:
        granted: list[EnclaveRequest] = []
        while len(self._granted) < self.limit and self._waiters:
            counts = Counter(request.pool for request in self._granted.values())
            waiting_pools = {request.pool for _sequence, request in self._waiters}
            reserved_deficits = {
                pool
                for pool in waiting_pools
                if counts[pool] < self.reservations[pool]
            }
            eligible_pools = reserved_deficits or waiting_pools
            index = min(
                (
                    position
                    for position in range(len(self._waiters))
                    if self._waiters[position][1].pool in eligible_pools
                ),
                key=lambda position: (
                    _BORROW_PRIORITY[self._waiters[position][1].pool],
                    self._waiters[position][0],
                ),
            )
            _sequence, request = self._waiters.pop(index)
            self._granted[request.request_id] = request
            granted.append(request)
        return granted

    def _notify(self, requests: list[EnclaveRequest]) -> None:
        for request in requests:
            self._on_grant(request)


class BrokerSemaphore:
    """Async context-manager semaphore backed by the parent process broker."""

    def __init__(
        self,
        conn,
        *,
        pool: PoolName,
        slot_id: str,
        slot_generation: str,
    ) -> None:
        self._conn = conn
        self.pool = pool
        self.slot_id = str(slot_id)
        self.slot_generation = str(slot_generation)
        self._pending: dict[str, asyncio.Future] = {}
        self._held_by_task: dict[asyncio.Task, list[str]] = {}
        self._reader_task: asyncio.Task | None = None
        self._closed = False

    async def acquire(self) -> bool:
        if self._closed:
            raise RuntimeError("enclave broker semaphore is closed")
        self._ensure_reader()
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future
        request = EnclaveRequest(
            request_id=request_id,
            pool=self.pool,
            slot_id=self.slot_id,
            slot_generation=self.slot_generation,
        )
        try:
            self._conn.send(acquire_message(request))
            await future
        except BaseException:
            self._pending.pop(request_id, None)
            try:
                self._conn.send(
                    release_message(request_id, self.slot_generation, cancel=True)
                )
            except (BrokenPipeError, EOFError, OSError, ValueError):
                pass
            raise
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("enclave permit acquired outside an asyncio task")
        self._held_by_task.setdefault(task, []).append(request_id)
        return True

    def release(self) -> None:
        task = asyncio.current_task()
        held = None if task is None else self._held_by_task.get(task)
        if not held:
            raise RuntimeError("enclave permit released without matching acquire")
        request_id = held.pop()
        if not held:
            self._held_by_task.pop(task, None)
        self._conn.send(release_message(request_id, self.slot_generation))

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        self.release()

    async def close(self) -> None:
        self._closed = True
        for request_id, future in tuple(self._pending.items()):
            if not future.done():
                future.cancel()
            try:
                self._conn.send(
                    release_message(request_id, self.slot_generation, cancel=True)
                )
            except (BrokenPipeError, EOFError, OSError, ValueError):
                pass
        self._pending.clear()
        task = self._reader_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _ensure_reader(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._grant_reader())

    async def _grant_reader(self) -> None:
        while not self._closed:
            try:
                ready = await asyncio.to_thread(self._conn.poll, 0.2)
                if not ready:
                    continue
                decoded = decode_grant_message(self._conn.recv())
            except (BrokenPipeError, EOFError, OSError, ValueError):
                break
            if decoded is None:
                continue
            request_id, generation = decoded
            if generation != self.slot_generation:
                continue
            future = self._pending.pop(request_id, None)
            if future is not None and not future.done():
                future.set_result(True)
        error = RuntimeError("enclave broker connection closed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
