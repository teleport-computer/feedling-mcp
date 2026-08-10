from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import anyio.to_thread
import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import asgi_app
import db
from accounts import registry
from asgi import health_executor
from asgi import runner_health
from core import wake_bus


async def _get(path: str):
    transport = httpx.ASGITransport(app=asgi_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path)


def test_runner_health_maps_dedicated_deadline_to_structured_503(monkeypatch):
    async def exceed_deadline(*_args, **_kwargs):
        raise health_executor.HealthCheckTimeout("test deadline")

    monkeypatch.setenv("FEEDLING_EXPECTED_RUNNER_COUNT", "1")
    monkeypatch.setattr(health_executor, "run", exceed_deadline)

    runner = asyncio.run(_get("/healthz/runner"))
    assert runner.status_code == 503
    assert runner.json()["checks"]["runner_fleet"]["reason"] == (
        "runner_health_check_timeout"
    )


def test_health_routes_ignore_saturated_ordinary_threadpool(monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPECTED_RUNNER_COUNT", "1")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("process liveness must not touch the database")

    monkeypatch.setattr(db, "health_probe", fail_if_called)
    monkeypatch.setattr(db, "get_pool", fail_if_called)

    def healthy_runner_fleet(**kwargs):
        assert kwargs == {"timeout": 1.0, "statement_timeout_ms": 1000}
        return [{"ts": 995.0, "host_all": True}]

    monkeypatch.setattr(
        db,
        "list_supervisor_instance_heartbeats",
        healthy_runner_fleet,
    )
    monkeypatch.setattr(registry, "_users", [{"user_id": "u1"}])
    monkeypatch.setattr(wake_bus, "_enabled", lambda: True)
    monkeypatch.setattr(wake_bus, "_listener_started", True)
    monkeypatch.setattr(runner_health.time, "time", lambda: 1000.0)

    entered = threading.Event()
    release = threading.Event()

    def occupy_ordinary_pool() -> None:
        entered.set()
        release.wait(timeout=2.0)

    async def go():
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        blocker = asyncio.create_task(anyio.to_thread.run_sync(occupy_ordinary_pool))
        try:
            async def wait_until_entered():
                while not entered.is_set():
                    if blocker.done():
                        await blocker
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_until_entered(), timeout=1.0)
            transport = httpx.ASGITransport(app=asgi_app.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://t",
            ) as client:
                return await asyncio.wait_for(
                    asyncio.gather(
                        client.get("/healthz"),
                        client.get("/healthz/runner"),
                    ),
                    timeout=1.0,
                )
        finally:
            release.set()
            if not entered.is_set():
                blocker.cancel()
            try:
                try:
                    await blocker
                except asyncio.CancelledError:
                    pass
            finally:
                limiter.total_tokens = original_tokens

    api, runner = asyncio.run(go())
    assert api.status_code == 200
    assert runner.status_code == 200
