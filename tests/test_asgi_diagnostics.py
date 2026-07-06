"""Native /v1 diagnostics parity: flow-trace debug endpoints + client-log upload
+ admin read.

Asserts the FastAPI routes (diagnostics.routes_asgi) return the same status/body
as the Flask oracle (diagnostics.routes) — both call the same framework-neutral
diagnostics_core. Covers user-auth 401 (trace + upload), admin-token auth
(401/503, mirroring copytext), upload validation (missing/empty/oversized), and
the R2-off inline path plus a monkeypatched R2-on path so no test hits the
network (R2 credentials are stripped in the fixture; storage.* is stubbed for the
R2-on case).
"""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from diagnostics import routes_asgi as diag_asgi  # noqa: E402
from diagnostics import storage as diag_storage  # noqa: E402
from fastapi import FastAPI  # noqa: E402

ADMIN_TOKEN = "admin-test-token"


def _build_asgi_app() -> FastAPI:
    # Standalone app: the diagnostics router + the fixed-body exception handlers,
    # independent of asgi_app.py's package list (owned by the orchestrator).
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    diag_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()
_pk_counter = itertools.count(1)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)
    # R2 OFF → the upload route exercises the inline-Postgres fallback (no network).
    for var in ("R2_ENDPOINT", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY", "R2_USER_LOGS_BUCKET"):
        monkeypatch.delenv(var, raising=False)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    yield


def _register() -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    import base64
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(raw).decode("ascii"), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


# --------------------------------------------------------------------------- #
# request helpers → (status, body) tuples
# --------------------------------------------------------------------------- #

def _flask_get(path, headers=None):
    res = make_client().get(path, headers=headers or {})
    return res.status_code, res.get_json(silent=True)


def _flask_post_json(path, headers=None, json_body=None):
    res = make_client().post(path, headers=headers or {}, json=json_body)
    return res.status_code, res.get_json(silent=True)


def _flask_delete(path, headers=None):
    res = make_client().delete(path, headers=headers or {})
    return res.status_code, res.get_json(silent=True)


def _flask_upload(api_key, content, meta=None):
    data = {"file": (io.BytesIO(content), "diagnostics.log")}
    if meta is not None:
        data["meta"] = json.dumps(meta)
    res = make_client().post(
        "/v1/diagnostics/logs",
        data=data,
        content_type="multipart/form-data",
        headers=({"X-API-Key": api_key} if api_key else {}),
    )
    return res.status_code, res.get_json(silent=True)


def _asgi(method, path, headers=None, **kw):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.request(method, path, headers=headers or {}, **kw)
            body = None
            if resp.content:
                try:
                    body = resp.json()
                except Exception:
                    body = None
            return resp.status_code, body
    return asyncio.run(go())


def _asgi_upload(api_key, content, meta=None):
    files = {"file": ("diagnostics.log", content, "text/plain")}
    data = {"meta": json.dumps(meta)} if meta is not None else None
    return _asgi("POST", "/v1/diagnostics/logs",
                 headers=({"X-API-Key": api_key} if api_key else {}),
                 files=files, data=data)


def _key(api_key):
    return {"X-API-Key": api_key}


def _admin(token=ADMIN_TOKEN):
    return {"X-Admin-Token": token}


# --------------------------------------------------------------------------- #
# upload (POST /v1/diagnostics/logs)
# --------------------------------------------------------------------------- #

def test_upload_success_parity(env):
    _uid, api_key = _register()
    f = _flask_upload(api_key, b"hello-log", meta={"app_version": "1.2.3"})
    a = _asgi_upload(api_key, b"hello-log", meta={"app_version": "1.2.3"})
    assert f == a
    assert f == (201, {"status": "ok"})


def test_upload_missing_file_parity(env):
    _uid, api_key = _register()
    f = _flask_post_json("/v1/diagnostics/logs", headers=_key(api_key), json_body=None)
    # No multipart at all on the ASGI side either.
    a = _asgi("POST", "/v1/diagnostics/logs", headers=_key(api_key))
    assert f == a
    assert f == (400, {"error": "missing_file"})


def test_upload_empty_file_parity(env):
    _uid, api_key = _register()
    f = _flask_upload(api_key, b"")
    a = _asgi_upload(api_key, b"")
    assert f == a
    assert f == (400, {"error": "empty_file"})


def test_upload_oversized_parity(env):
    _uid, api_key = _register()
    big = b"x" * (2 * 1024 * 1024 + 1024)  # > _MAX_REQUEST_BYTES
    f = _flask_upload(api_key, big)
    a = _asgi_upload(api_key, big)
    assert f == a
    assert f == (413, {"error": "payload_too_large"})


def test_upload_requires_auth_parity(env):
    f = _flask_upload(None, b"hi")
    a = _asgi_upload(None, b"hi")
    assert f == a
    assert f == (401, {"error": "unauthorized"})


# --------------------------------------------------------------------------- #
# admin read (GET /v1/admin/diagnostics/logs/{user_id})
# --------------------------------------------------------------------------- #

def test_admin_read_parity_inline(env):
    uid, api_key = _register()
    _flask_upload(api_key, "hello 测试 🚀".encode("utf-8"), meta={"device": "iPhone"})
    # Both admin routes read the SAME row → byte-identical (ts included).
    f = _flask_get(f"/v1/admin/diagnostics/logs/{uid}", headers=_admin())
    a = _asgi("GET", f"/v1/admin/diagnostics/logs/{uid}", headers=_admin())
    assert f == a
    assert f[0] == 200
    assert f[1]["user_id"] == uid
    assert f[1]["logs"][0]["content"] == "hello 测试 🚀"
    assert "download_url" not in f[1]["logs"][0]


def test_admin_read_asgi_upload_visible(env):
    uid, api_key = _register()
    assert _asgi_upload(api_key, b"from-asgi") == (201, {"status": "ok"})
    status, body = _asgi("GET", f"/v1/admin/diagnostics/logs/{uid}", headers=_admin())
    assert status == 200
    assert body["logs"][-1]["content"] == "from-asgi"


def test_admin_read_no_token_parity(env):
    uid, _api_key = _register()
    f = _flask_get(f"/v1/admin/diagnostics/logs/{uid}")
    a = _asgi("GET", f"/v1/admin/diagnostics/logs/{uid}")
    assert f == a
    assert f == (401, {"error": "unauthorized"})


def test_admin_read_wrong_token_parity(env):
    uid, _api_key = _register()
    f = _flask_get(f"/v1/admin/diagnostics/logs/{uid}", headers=_admin("wrong"))
    a = _asgi("GET", f"/v1/admin/diagnostics/logs/{uid}", headers=_admin("wrong"))
    assert f == a
    assert f == (401, {"error": "unauthorized"})


def test_admin_read_unconfigured_is_503_parity(env, monkeypatch):
    uid, _api_key = _register()
    monkeypatch.delenv("FEEDLING_ADMIN_TOKEN", raising=False)
    f = _flask_get(f"/v1/admin/diagnostics/logs/{uid}", headers=_admin())
    a = _asgi("GET", f"/v1/admin/diagnostics/logs/{uid}", headers=_admin())
    assert f == a
    assert f == (503, {"error": "service_unavailable", "detail": "admin token is not configured"})


def test_admin_read_r2_on_parity(env, monkeypatch):
    """R2-on branch, deterministic: stub storage so the upload records an r2_key
    and the admin read presigns it — no boto3/network."""
    uid, api_key = _register()
    monkeypatch.setattr(diag_storage, "enabled", lambda: True)
    monkeypatch.setattr(diag_storage, "put_log", lambda u, ts_iso, data: f"{u}/{ts_iso}.log")
    monkeypatch.setattr(diag_storage, "presign_get", lambda key, ttl: f"https://r2.test/{key}?sig=x")
    _flask_upload(api_key, b"r2-log")
    f = _flask_get(f"/v1/admin/diagnostics/logs/{uid}", headers=_admin())
    a = _asgi("GET", f"/v1/admin/diagnostics/logs/{uid}", headers=_admin())
    assert f == a
    assert f[0] == 200
    entry = f[1]["logs"][0]
    assert entry["r2_key"].endswith(".log")
    assert entry["download_url"].startswith("https://r2.test/")
    assert "content" not in entry


# --------------------------------------------------------------------------- #
# flow-trace debug endpoints (GET/DELETE /v1/debug/trace, POST enable)
# --------------------------------------------------------------------------- #

def test_trace_read_parity(env):
    _uid, api_key = _register()
    f = _flask_get("/v1/debug/trace", headers=_key(api_key))
    a = _asgi("GET", "/v1/debug/trace", headers=_key(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1] == {"enabled": True, "deploy_enabled": True, "verbose": True, "events": []}


def test_trace_read_requires_auth_parity(env):
    f = _flask_get("/v1/debug/trace")
    a = _asgi("GET", "/v1/debug/trace")
    assert f == a
    assert f == (401, {"error": "unauthorized"})


def test_trace_enable_parity(env):
    _uid, api_key = _register()
    f = _flask_post_json("/v1/debug/trace/enable", headers=_key(api_key), json_body={"enabled": True})
    a = _asgi("POST", "/v1/debug/trace/enable", headers=_key(api_key), json={"enabled": True})
    assert f == a
    assert f == (200, {"enabled": True, "deploy_enabled": True})


def test_trace_enable_empty_body_degrades_parity(env):
    """Validation/degradation path: an empty/malformed body → enabled=False
    (Flask ``get_json(silent=True) or {}`` == ASGI ``_read_json_silent or {}``)."""
    _uid, api_key = _register()
    # No JSON content-type / no body on either side.
    f = _flask_post_json("/v1/debug/trace/enable", headers=_key(api_key), json_body=None)
    a = _asgi("POST", "/v1/debug/trace/enable", headers=_key(api_key))
    assert f == a
    assert f == (200, {"enabled": False, "deploy_enabled": True})


def test_trace_clear_parity(env):
    _uid, api_key = _register()
    f = _flask_delete("/v1/debug/trace", headers=_key(api_key))
    a = _asgi("DELETE", "/v1/debug/trace", headers=_key(api_key))
    assert f == a
    assert f == (200, {"status": "ok"})


def test_trace_enable_then_read_reflects_state(env):
    _uid, api_key = _register()
    _asgi("POST", "/v1/debug/trace/enable", headers=_key(api_key), json={"enabled": True})
    status, body = _asgi("GET", "/v1/debug/trace", headers=_key(api_key))
    assert status == 200
    assert body["enabled"] is True
