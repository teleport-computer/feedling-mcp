"""Health-only blocking executor, isolated from ordinary ASGI work."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

HEALTH_CHECK_DEADLINE_SECONDS = 3.0
_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="feedling-health",
)


class HealthCheckTimeout(RuntimeError):
    """The health callable did not finish before its route deadline."""


async def run(
    fn: Callable[P, T],
    /,
    *args: P.args,
    deadline_seconds: float = HEALTH_CHECK_DEADLINE_SECONDS,
    **kwargs: P.kwargs,
) -> T:
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor, partial(fn, *args, **kwargs))
    try:
        return await asyncio.wait_for(future, timeout=deadline_seconds)
    except TimeoutError as exc:
        raise HealthCheckTimeout("health check deadline exceeded") from exc
