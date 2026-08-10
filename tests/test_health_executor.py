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
