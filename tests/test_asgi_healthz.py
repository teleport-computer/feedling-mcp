"""ASGI skeleton parity (ASGI-migration plan §8.2 / §14.1).

Drives the FastAPI app via ``httpx.ASGITransport`` and asserts:
- ``/healthz`` is byte-for-byte semantically equal to the Flask oracle,
- an unmigrated route is a 404 (the no-fallback behavior, plan §5.3),
- typed ``AuthError`` maps to the fixed Flask error body (plan §3.1),
- the access log redacts the legacy ``?key=`` param (security parity, §5.9).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
import db  # noqa: E402
from accounts import auth_core  # noqa: E402
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from core import wake_bus  # noqa: E402


def _asgi_get(path: str):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get(path)
            return resp.status_code, resp.json()

    return asyncio.run(go())


class _FakePool:
    """Stand-in for the psycopg pool so /healthz stats don't need a live DB."""

    def __init__(self, stats):
        self._stats = stats

    def get_stats(self):
        return self._stats


_HEALTHY_STATS = {
    "pool_size": 6,
    "pool_available": 5,
    "requests_waiting": 0,
    "pool_max": 16,
}


@pytest.fixture()
def healthy(monkeypatch):
    """Patch every /healthz dependency into a healthy state (no live PG)."""
    monkeypatch.setattr(db, "health_probe", lambda **_kwargs: {
        "ok": True, "latency_ms": 1.2, "error": None,
    })
    monkeypatch.setattr(db, "get_pool", lambda: _FakePool(dict(_HEALTHY_STATS)))
    monkeypatch.setattr(registry, "_users", [{"user_id": "u1"}, {"user_id": "u2"}])
    monkeypatch.setattr(wake_bus, "_listener_started", True)
    monkeypatch.setattr(wake_bus, "_enabled", lambda: True)


def test_healthz_healthy_shape(healthy):
    status, body = _asgi_get("/healthz")
    assert status == 200
    # Legacy fields preserved for old probes.
    assert body["ok"] is True
    assert body["mode"] == "multi_tenant"
    # New roll-up + metadata.
    assert body["status"] == "healthy"
    assert set(body["release"]) == {"git_commit", "image_digest", "built_at"}
    assert isinstance(body["uptime_s"], (int, float))
    assert "pid" in body["worker"]
    checks = body["checks"]
    assert checks["db"]["status"] == "ok"
    assert checks["db_pool"]["status"] == "ok"
    assert checks["registry"] == {"status": "ok", "users_loaded": 2}
    assert checks["wake_bus"]["status"] == "ok"


def test_healthz_db_down_is_503_unhealthy(healthy, monkeypatch):
    monkeypatch.setattr(db, "health_probe", lambda **_kwargs: {
        "ok": False, "latency_ms": 2000.0, "error": "pool timeout",
    })
    status, body = _asgi_get("/healthz")
    assert status == 503
    assert body["ok"] is False
    assert body["status"] == "unhealthy"
    assert body["checks"]["db"]["status"] == "down"
    assert body["checks"]["db"]["error"] == "pool timeout"


def test_healthz_pool_saturated_is_degraded_200(healthy, monkeypatch):
    saturated = dict(_HEALTHY_STATS, requests_waiting=3, pool_available=0)
    monkeypatch.setattr(db, "get_pool", lambda: _FakePool(saturated))
    status, body = _asgi_get("/healthz")
    assert status == 200  # degraded still serves
    assert body["status"] == "degraded"
    assert body["checks"]["db_pool"]["status"] == "saturated"


def test_healthz_empty_registry_is_degraded(healthy, monkeypatch):
    monkeypatch.setattr(registry, "_users", [])
    status, body = _asgi_get("/healthz")
    assert status == 200
    assert body["status"] == "degraded"
    assert body["checks"]["registry"] == {"status": "empty", "users_loaded": 0}


def test_healthz_wake_bus_not_listening_is_degraded(healthy, monkeypatch):
    monkeypatch.setattr(wake_bus, "_listener_started", False)
    status, body = _asgi_get("/healthz")
    assert status == 200
    assert body["status"] == "degraded"
    assert body["checks"]["wake_bus"]["status"] == "not_listening"


def test_unknown_route_is_404_not_401():
    # A path with no ASGI route → plain 404 (no runtime fallback), NOT 401.
    # Use a guaranteed-nonexistent path so this doesn't break as real routes
    # get migrated.
    status, _ = _asgi_get("/v1/__no_such_route__/xyz")
    assert status == 404


def test_autherror_maps_to_fixed_body():
    app2 = FastAPI()
    middleware.register_exception_handlers(app2)

    @app2.get("/boom401")
    async def boom401():
        raise auth_core.AuthError(401, "unauthorized")

    @app2.get("/boom403")
    async def boom403():
        raise auth_core.AuthError(403, "forbidden", "scope_denied")

    async def go():
        transport = httpx.ASGITransport(app=app2)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get("/boom401"), await client.get("/boom403")

    r401, r403 = asyncio.run(go())
    assert r401.status_code == 401 and r401.json() == {"error": "unauthorized"}
    assert r403.status_code == 403 and r403.json() == {"error": "forbidden"}


def test_access_log_redacts_key_param():
    # Security parity (§3.1/§5.9): the legacy ?key= API key must never be logged.
    scope = {"path": "/v1/x", "query_string": b"key=SUPERSECRET&since=5"}
    disp = middleware._display_path(scope)
    assert "SUPERSECRET" not in disp
    assert "REDACTED" in disp
    assert "since=5" in disp
    # case-insensitive
    scope_upper = {"path": "/v1/x", "query_string": b"KEY=SUPERSECRET"}
    assert "SUPERSECRET" not in middleware._display_path(scope_upper)
    # no query string → bare path
    assert middleware._display_path({"path": "/v1/x", "query_string": b""}) == "/v1/x"


def test_access_log_redacts_all_secret_bearing_param_names():
    """精确匹配 ``key`` 会漏掉每一个真实在用的别名。

    2026-07-30 审计实证：admin 后台把 ``admin_key`` 放进每个链接的 query string
    （``data_track._data_track_qs`` 的保留列表第一项），而当时的判据是
    ``k.lower() == "key"``——``admin_key`` != ``key``，于是可复用的 admin 凭据
    原样进了服务端访问日志。``admin_token`` / ``api_key`` 同理。
    """
    for name in ("admin_key", "admin_token", "api_key", "API_KEY", "X-Admin-Key",
                 "access_token", "session_token", "secret", "password"):
        scope = {
            "path": "/admin/data-track",
            "query_string": f"{name}=SUPERSECRET&view=runtime".encode(),
        }
        disp = middleware._display_path(scope)
        assert "SUPERSECRET" not in disp, f"{name} 未脱敏"
        assert "REDACTED" in disp, f"{name} 未脱敏"
        # 非敏感参数必须原样保留——脱敏不能把日志变成无法排查
        assert "view=runtime" in disp

    # 不含敏感名的 query string 完整保留，一个字符都不改
    plain = {"path": "/admin/data-track", "query_string": b"view=runtime&hours=168"}
    assert middleware._display_path(plain) == "/admin/data-track?view=runtime&hours=168"
