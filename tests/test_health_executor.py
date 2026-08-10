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


def test_health_executor_rejects_work_beyond_outstanding_limit():
    worker_started = [threading.Event(), threading.Event()]
    worker_release = threading.Event()
    queued_started = [threading.Event(), threading.Event()]
    excess_started = threading.Event()

    def blocked(started: threading.Event) -> None:
        started.set()
        assert worker_release.wait(timeout=2.0)

    def queued(started: threading.Event) -> None:
        started.set()

    async def go():
        running_tasks = [
            asyncio.create_task(health_executor.run(blocked, started))
            for started in worker_started
        ]
        queued_tasks = []
        excess_task = None
        try:
            entered = await asyncio.gather(
                *(asyncio.to_thread(started.wait, 1.0) for started in worker_started)
            )
            assert entered == [True, True]

            queued_tasks = [
                asyncio.create_task(health_executor.run(queued, started))
                for started in queued_started
            ]
            await asyncio.sleep(0)

            excess_task = asyncio.create_task(
                health_executor.run(queued, excess_started, deadline_seconds=10.0)
            )
            await asyncio.sleep(0)

            assert excess_task.done()
            with pytest.raises(health_executor.HealthCheckTimeout):
                await excess_task

            for task in queued_tasks:
                task.cancel()
            cancelled = await asyncio.gather(*queued_tasks, return_exceptions=True)
            assert all(isinstance(item, asyncio.CancelledError) for item in cancelled)

            worker_release.set()
            await asyncio.gather(*running_tasks)
            assert not any(started.is_set() for started in queued_started)
            assert not excess_started.is_set()
        finally:
            worker_release.set()
            pending_tasks = [*running_tasks, *queued_tasks]
            if excess_task is not None:
                pending_tasks.append(excess_task)
            for task in pending_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)

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
