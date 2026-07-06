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
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
from accounts import auth_core  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402


def _asgi_get(path: str):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get(path)
            return resp.status_code, resp.json()

    return asyncio.run(go())


def test_healthz_parity_with_flask_oracle():
    flask_resp = make_client().get("/healthz")
    status, body = _asgi_get("/healthz")
    assert status == flask_resp.status_code == 200
    assert body == flask_resp.get_json() == {"ok": True, "mode": "multi_tenant"}


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
