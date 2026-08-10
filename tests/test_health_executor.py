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
    first_finished = threading.Event()
    second_finished = threading.Event()
    first_release = threading.Event()
    second_release = threading.Event()
    third_release = threading.Event()
    workers_ready = threading.Barrier(2)

    def blocked(
        value: str,
        started: threading.Event,
        finished: threading.Event,
        release: threading.Event,
    ) -> str:
        started.set()
        workers_ready.wait(timeout=1.0)
        release.wait(timeout=1.0)
        finished.set()
        return value

    def third() -> str:
        third_started.set()
        third_release.wait(timeout=1.0)
        return "third"

    async def go():
        tasks = [
            asyncio.create_task(
                health_executor.run(
                    blocked,
                    "first",
                    first_started,
                    first_finished,
                    first_release,
                )
            ),
            asyncio.create_task(
                health_executor.run(
                    blocked,
                    "second",
                    second_started,
                    second_finished,
                    second_release,
                )
            ),
            asyncio.create_task(health_executor.run(third)),
        ]
        try:
            await asyncio.gather(
                asyncio.to_thread(first_started.wait),
                asyncio.to_thread(second_started.wait),
            )
            assert not third_started.is_set()

            first_release.set()
            await asyncio.to_thread(third_started.wait)
            assert first_finished.is_set()
            assert not second_finished.is_set()

            second_release.set()
            third_release.set()
            assert await asyncio.gather(*tasks) == ["first", "second", "third"]
        finally:
            first_release.set()
            second_release.set()
            third_release.set()

    asyncio.run(go())


def test_health_executor_cancels_queued_callable_when_run_is_cancelled():
    first_started = threading.Event()
    second_started = threading.Event()
    third_started = threading.Event()
    first_release = threading.Event()
    second_release = threading.Event()

    def blocked(started: threading.Event, release: threading.Event) -> None:
        started.set()
        release.wait(timeout=1.0)

    def queued() -> None:
        third_started.set()

    async def go():
        first_task = asyncio.create_task(
            health_executor.run(blocked, first_started, first_release)
        )
        second_task = asyncio.create_task(
            health_executor.run(blocked, second_started, second_release)
        )
        await asyncio.gather(
            asyncio.to_thread(first_started.wait),
            asyncio.to_thread(second_started.wait),
        )

        third_task = asyncio.create_task(health_executor.run(queued))
        await asyncio.sleep(0)
        third_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await third_task

        try:
            first_release.set()
            second_release.set()
            await asyncio.gather(first_task, second_task)
            assert not third_started.is_set()
        finally:
            first_release.set()
            second_release.set()

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
