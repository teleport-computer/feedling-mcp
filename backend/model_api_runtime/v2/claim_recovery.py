"""Bounded parent-owned retries for exact killed-claim recovery."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

from model_api_runtime.v2 import jobs_store

log = logging.getLogger("feedling.runtime_v2.claim_recovery")

_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)


@dataclass
class _Request:
    job_id: int
    claimed_by: str
    reason: str
    attempt: int
    due_at: float


class ClaimRecoveryQueue:
    def __init__(
        self,
        *,
        limit: int = 256,
        recover: Callable[..., dict[str, object] | None] = jobs_store.recover_killed_job,
    ) -> None:
        self._limit = max(1, min(int(limit), 256))
        self._recover = recover
        self._requests: dict[tuple[int, str], _Request] = {}
        self._wake = asyncio.Event()

    @property
    def pending_count(self) -> int:
        return len(self._requests)

    def enqueue(
        self,
        *,
        job_id: int,
        claimed_by: str,
        reason: str = "slot_watchdog_timeout",
    ) -> bool:
        key = (int(job_id), str(claimed_by))
        if key in self._requests:
            return True
        if len(self._requests) >= self._limit:
            log.error("claim recovery retry queue full; lease reaper is fallback")
            return False
        self._requests[key] = _Request(
            job_id=key[0],
            claimed_by=key[1],
            reason=str(reason),
            attempt=0,
            due_at=time.monotonic() + _DELAYS[0],
        )
        self._wake.set()
        return True

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            now = time.monotonic()
            due = [
                (key, request)
                for key, request in self._requests.items()
                if request.due_at <= now
            ]
            for key, request in due:
                try:
                    await asyncio.to_thread(
                        self._recover,
                        job_id=request.job_id,
                        claimed_by=request.claimed_by,
                        reason=request.reason,
                    )
                except Exception:
                    request.attempt += 1
                    if request.attempt >= len(_DELAYS):
                        self._requests.pop(key, None)
                        log.exception(
                            "exact claim recovery exhausted job=%s; lease reaper fallback",
                            request.job_id,
                        )
                    else:
                        request.due_at = time.monotonic() + _DELAYS[request.attempt]
                else:
                    self._requests.pop(key, None)
            self._wake.clear()
            next_due = min(
                (request.due_at for request in self._requests.values()),
                default=time.monotonic() + 1.0,
            )
            timeout = max(0.01, min(1.0, next_due - time.monotonic()))
            wake_task = asyncio.create_task(self._wake.wait())
            stop_task = asyncio.create_task(stop_event.wait())
            try:
                await asyncio.wait(
                    {wake_task, stop_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (wake_task, stop_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(wake_task, stop_task, return_exceptions=True)
