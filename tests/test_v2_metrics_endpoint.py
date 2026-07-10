"""Hosted Runtime V2 D0 Task 4 — GET /v1/admin/v2-metrics.

Admin-token-gated JSON endpoint that surfaces jobs_store's queue-depth/worker-
liveness/service-time/token-throughput counters, which D4 load-testing
consumes. Mirrors the admin-token gate + route style of
test_admin_runtime_mode.py, but the five jobs_store functions
admin_core.v2_metrics composes are monkeypatched directly rather than
requiring seeded rows — admin_core.v2_metrics is a thin composition with no
logic of its own to exercise against real data here (jobs_store's own
functions already have coverage in test_v2_jobs_store.py/test_v2_turn_metrics.py).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import routes_asgi as admin_asgi  # noqa: E402
from asgi import middleware  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

ADMIN_TOKEN = "admin-test-token"


def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    admin_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(jobs_store, "inflight_job_count", lambda: 3)
    monkeypatch.setattr(jobs_store, "pending_job_count", lambda: 1)
    monkeypatch.setattr(jobs_store, "live_worker_count", lambda **kw: 2)
    monkeypatch.setattr(jobs_store, "recent_mean_service_sec", lambda **kw: 4.5)
    monkeypatch.setattr(jobs_store, "recent_mean_tokens_per_turn", lambda **kw: 123.0)
    monkeypatch.setattr(jobs_store, "genesis_worker_alive", lambda **kw: True)
    monkeypatch.setattr(
        jobs_store,
        "wake_success_stats",
        lambda **kw: {
            "completed": 4,
            "failed": 1,
            "expired": 0,
            "success_rate": 0.8,
            "by_lane": {"heartbeat": {"completed": 4}, "scheduled": {"failed": 1}},
        },
    )
    yield


def _admin(token=ADMIN_TOKEN):
    return {"X-Admin-Token": token}


def _asgi(method, path, headers=None, **kw):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.request(method, path, headers=headers or {}, **kw)

    return asyncio.run(go())


def _asgi_json(method, path, headers=None, **kw):
    resp = _asgi(method, path, headers=headers, **kw)
    body = None
    if resp.content:
        try:
            body = resp.json()
        except Exception:
            body = None
    return resp.status_code, body


def test_v2_metrics_returns_every_field(env):
    status, body = _asgi_json("GET", "/v1/admin/v2-metrics", headers=_admin())

    assert status == 200
    assert body == {
        "inflight": 3,
        "pending": 1,
        "live_workers": 2,
        "mean_service_sec": 4.5,
        "recent_mean_tokens_per_turn": 123.0,
        "wake": {
            "completed": 4,
            "failed": 1,
            "expired": 0,
            "success_rate": 0.8,
            "by_lane": {"heartbeat": {"completed": 4}, "scheduled": {"failed": 1}},
        },
        "genesis_alive": True,
    }


def test_v2_metrics_surfaces_a_dead_genesis_thread(env, monkeypatch):
    """A dead genesis thread must be visible even when every turn worker is healthy.

    `live_workers` counts kind='turn' rows only, so a genesis thread that died to a
    lazy-import error inside `run_loop` leaves no other trace anywhere.
    """
    monkeypatch.setattr(jobs_store, "genesis_worker_alive", lambda **kw: False)

    status, body = _asgi_json("GET", "/v1/admin/v2-metrics", headers=_admin())

    assert status == 200
    assert body["genesis_alive"] is False
    assert body["live_workers"] == 2


def test_v2_metrics_no_token_is_401(env):
    status, body = _asgi_json("GET", "/v1/admin/v2-metrics")

    assert status == 401
    assert body == {"error": "unauthorized"}


def test_v2_metrics_wrong_token_is_401(env):
    status, body = _asgi_json("GET", "/v1/admin/v2-metrics", headers=_admin("wrong-token"))

    assert status == 401
    assert body == {"error": "unauthorized"}
