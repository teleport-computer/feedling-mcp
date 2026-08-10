from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from asgi import health_executor


def test_health_executor_runs_two_checks_concurrently():
    barrier = threading.Barrier(2)

    def check(value: str) -> str:
        barrier.wait(timeout=1.0)
        return value

    async def go():
        return await asyncio.gather(
            health_executor.run(check, "api"),
            health_executor.run(check, "runner"),
        )

    assert asyncio.run(go()) == ["api", "runner"]


def test_health_executor_limits_concurrency_to_two_workers():
    first_started = threading.Event()
    second_started = threading.Event()
    third_started = threading.Event()
    release = threading.Event()
    workers_ready = threading.Barrier(2)

    def blocked(value: str, started: threading.Event) -> str:
        started.set()
        workers_ready.wait(timeout=1.0)
        release.wait(timeout=1.0)
        return value

    def third() -> str:
        third_started.set()
        return "third"

    async def go():
        tasks = [
            asyncio.create_task(
                health_executor.run(blocked, "first", first_started)
            ),
            asyncio.create_task(
                health_executor.run(blocked, "second", second_started)
            ),
            asyncio.create_task(health_executor.run(third)),
        ]
        try:
            await asyncio.gather(
                asyncio.to_thread(first_started.wait),
                asyncio.to_thread(second_started.wait),
            )
            assert not third_started.is_set()

            release.set()
            assert await asyncio.gather(*tasks) == ["first", "second", "third"]
            assert third_started.is_set()
        finally:
            release.set()

    asyncio.run(go())


def test_health_executor_preserves_callable_timeout_error():
    def raises_timeout() -> None:
        raise TimeoutError("inner")

    async def go():
        with pytest.raises(TimeoutError, match="inner"):
            await health_executor.run(raises_timeout)

    asyncio.run(go())


def test_health_executor_maps_outer_deadline_to_stable_exception():
    started = threading.Event()
    release = threading.Event()

    def blocked() -> None:
        started.set()
        release.wait(timeout=1.0)

    async def go():
        task = asyncio.create_task(
            health_executor.run(blocked, deadline_seconds=0.01)
        )
        while not started.is_set():
            await asyncio.sleep(0)
        with pytest.raises(health_executor.HealthCheckTimeout):
            await task

    try:
        asyncio.run(go())
    finally:
        release.set()
