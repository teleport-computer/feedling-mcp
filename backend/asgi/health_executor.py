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


class HealthCheckSaturated(HealthCheckTimeout):
    """The bounded health executor cannot admit another callable."""


def _release_outstanding_slot(_future: Future[object]) -> None:
    _outstanding_slots.release()


def _consume_future_exception(future: asyncio.Future[object]) -> None:
    """Observe late failures after the request-side deadline has elapsed."""
    if future.cancelled():
        return
    future.exception()


async def run(
    fn: Callable[P, T],
    /,
    *args: P.args,
    deadline_seconds: float = HEALTH_CHECK_DEADLINE_SECONDS,
    completion_callback: Callable[[Future[T]], None] | None = None,
    **kwargs: P.kwargs,
) -> T:
    loop = asyncio.get_running_loop()
    if not _outstanding_slots.acquire(blocking=False):
        raise HealthCheckSaturated("health check executor saturated")

    try:
        future = _executor.submit(partial(fn, *args, **kwargs))
    except BaseException:
        _outstanding_slots.release()
        raise
    future.add_done_callback(_release_outstanding_slot)
    if completion_callback is not None:
        future.add_done_callback(completion_callback)
    wrapped_future = asyncio.wrap_future(future, loop=loop)
    wrapped_future.add_done_callback(_consume_future_exception)
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
