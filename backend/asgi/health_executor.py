"""Health-only blocking executor, isolated from ordinary ASGI work."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from threading import BoundedSemaphore
from typing import Callable, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

HEALTH_CHECK_DEADLINE_SECONDS = 3.0
HEALTH_CHECK_MAX_WORKERS = 2
HEALTH_CHECK_MAX_OUTSTANDING = 4
_executor = ThreadPoolExecutor(
    max_workers=HEALTH_CHECK_MAX_WORKERS,
    thread_name_prefix="feedling-health",
)
_outstanding_slots = BoundedSemaphore(HEALTH_CHECK_MAX_OUTSTANDING)


class HealthCheckTimeout(RuntimeError):
    """The health callable could not be admitted or finish by its deadline."""


def _release_outstanding_slot(_future: Future[object]) -> None:
    _outstanding_slots.release()


async def run(
    fn: Callable[P, T],
    /,
    *args: P.args,
    deadline_seconds: float = HEALTH_CHECK_DEADLINE_SECONDS,
    **kwargs: P.kwargs,
) -> T:
    loop = asyncio.get_running_loop()
    if not _outstanding_slots.acquire(blocking=False):
        raise HealthCheckTimeout("health check executor saturated")

    try:
        future = _executor.submit(partial(fn, *args, **kwargs))
    except BaseException:
        _outstanding_slots.release()
        raise
    future.add_done_callback(_release_outstanding_slot)
    wrapped_future = asyncio.wrap_future(future, loop=loop)
    try:
        _done, pending = await asyncio.wait(
            {wrapped_future},
            timeout=deadline_seconds,
        )
    except asyncio.CancelledError:
        future.cancel()
        raise
    if pending:
        future.cancel()
        raise HealthCheckTimeout("health check deadline exceeded") from TimeoutError()
    return wrapped_future.result()
