"""Hosted Runtime V2 D0 Task 2 — mode setter control plane.

Admin-token-gated HTTP surface over ``hosted.config_store.set_hosted_runtime_mode``
/ ``get_hosted_runtime_mode``, so ops can flip a user between ``resident_cli``
(existing CLI consumer) and ``db_action_v2`` (V2 worker pool) without a direct
DB write. Mirrors the admin-token gate + route style of
``admin.routes_asgi``/``tests/test_asgi_admin.py``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from admin import routes_asgi as admin_asgi  # noqa: E402
from asgi import middleware  # noqa: E402
from conftest import seed_user, configure_model_api_route  # noqa: E402
from core import store as core_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from hosted import config_store  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires DATABASE_URL / postgres"
)

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
    yield


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _seed_model_api_user(user_id: str) -> None:
    seed_user(user_id)
    configure_model_api_route(user_id, provider="anthropic", model="x", test_status="ok")


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


# --------------------------------------------------------------------------- #
# POST /v1/admin/hosted-runtime-mode
# --------------------------------------------------------------------------- #

def test_post_valid_mode_sets_and_reflects_in_config_store(env):
    uid = _uid("rtmode_ok")
    _seed_model_api_user(uid)

    status, body = _asgi_json(
        "POST", "/v1/admin/hosted-runtime-mode", headers=_admin(),
        json={"user_id": uid, "mode": "db_action_v2"},
    )

    assert status == 200
    assert body == {"user_id": uid, "hosted_runtime_mode": "db_action_v2"}
    assert config_store.get_hosted_runtime_mode(core_store.get_store(uid)) == "db_action_v2"
    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule is not None
    assert schedule["next_heartbeat_at"] is not None


def test_post_control_plane_write_failure_returns_503(env, monkeypatch):
    uid = _uid("rtmode_dbfail")
    _seed_model_api_user(uid)
    monkeypatch.setattr(
        config_store,
        "set_hosted_runtime_mode",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    status, body = _asgi_json(
        "POST", "/v1/admin/hosted-runtime-mode", headers=_admin(),
        json={"user_id": uid, "mode": "resident_cli"},
    )

    assert status == 503
    assert body == {"error": "runtime_control_unavailable"}


def test_post_invalid_mode_returns_400(env):
    uid = _uid("rtmode_bad")
    _seed_model_api_user(uid)

    status, body = _asgi_json(
        "POST", "/v1/admin/hosted-runtime-mode", headers=_admin(),
        json={"user_id": uid, "mode": "bogus_mode"},
    )

    assert status == 400
    assert "error" in body
    # Never silently coerced to a valid mode.
    assert config_store.get_hosted_runtime_mode(core_store.get_store(uid)) == "resident_cli"


def test_post_user_with_no_model_api_config_returns_400(env):
    uid = _uid("rtmode_noconfig")
    seed_user(uid)  # no model_api blob written

    status, body = _asgi_json(
        "POST", "/v1/admin/hosted-runtime-mode", headers=_admin(),
        json={"user_id": uid, "mode": "db_action_v2"},
    )

    assert status == 400
    assert "error" in body


def test_post_missing_user_id_or_mode_returns_400(env):
    status, body = _asgi_json(
        "POST", "/v1/admin/hosted-runtime-mode", headers=_admin(), json={"mode": "db_action_v2"},
    )
    assert status == 400

    uid = _uid("rtmode_missingmode")
    _seed_model_api_user(uid)
    status, body = _asgi_json(
        "POST", "/v1/admin/hosted-runtime-mode", headers=_admin(), json={"user_id": uid},
    )
    assert status == 400


# --------------------------------------------------------------------------- #
# GET /v1/admin/hosted-runtime-mode
# --------------------------------------------------------------------------- #

def test_get_returns_current_mode(env):
    uid = _uid("rtmode_get")
    _seed_model_api_user(uid)
    config_store.set_hosted_runtime_mode(core_store.get_store(uid), "db_action_v2")

    status, body = _asgi_json(
        "GET", f"/v1/admin/hosted-runtime-mode?user_id={uid}", headers=_admin(),
    )

    assert status == 200
    assert body == {"user_id": uid, "hosted_runtime_mode": "db_action_v2"}


def test_get_defaults_to_resident_cli_when_unset(env):
    uid = _uid("rtmode_default")
    _seed_model_api_user(uid)

    status, body = _asgi_json(
        "GET", f"/v1/admin/hosted-runtime-mode?user_id={uid}", headers=_admin(),
    )

    assert status == 200
    assert body == {"user_id": uid, "hosted_runtime_mode": "resident_cli"}


# --------------------------------------------------------------------------- #
# GET /v1/admin/hosted-runtime-modes (list, grouped)
# --------------------------------------------------------------------------- #

def test_list_runtime_modes_groups_by_mode(env):
    uid_v2 = _uid("rtmode_list_v2")
    uid_resident = _uid("rtmode_list_res")
    _seed_model_api_user(uid_v2)
    _seed_model_api_user(uid_resident)
    config_store.set_hosted_runtime_mode(core_store.get_store(uid_v2), "db_action_v2")

    status, body = _asgi_json("GET", "/v1/admin/hosted-runtime-modes", headers=_admin())

    assert status == 200
    assert uid_v2 in body["db_action_v2"]
    assert uid_v2 not in body.get("resident_cli", [])


# --------------------------------------------------------------------------- #
# admin-token auth
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "path,method",
    [
        ("/v1/admin/hosted-runtime-mode", "POST"),
        ("/v1/admin/hosted-runtime-mode?user_id=x", "GET"),
        ("/v1/admin/hosted-runtime-modes", "GET"),
    ],
)
def test_no_token_is_401(env, path, method):
    status, body = _asgi_json(method, path, json={"user_id": "x", "mode": "db_action_v2"} if method == "POST" else None)
    assert status == 401
    assert body == {"error": "unauthorized"}
