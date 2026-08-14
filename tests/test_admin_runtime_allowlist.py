"""Admin HTTP surface over the dual-runtime canary allowlist
(``db.upsert_runtime_allowlist`` / ``list_runtime_allowlist`` /
``delete_runtime_allowlist``) plus the reconciliation view. Mirrors the
admin-token gate + route style of ``tests/test_admin_runtime_mode.py``.
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
from fastapi import FastAPI  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires DATABASE_URL / postgres"
)

ADMIN_TOKEN = "admin-test-token"
_SEED_CREATED_AT = "2026-08-14T00:00:00+00:00"
_SEEDED_USER_IDS: set[str] = set()


def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    admin_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)
    _SEEDED_USER_IDS.clear()
    try:
        yield
    finally:
        user_ids = tuple(_SEEDED_USER_IDS)
        if user_ids:
            try:
                with db.get_pool().connection() as conn:
                    conn.execute(
                        "DELETE FROM v2_wake_schedule WHERE user_id = ANY(%s)",
                        (list(user_ids),),
                    )
                    conn.execute(
                        "DELETE FROM users WHERE user_id = ANY(%s)",
                        (list(user_ids),),
                    )
            finally:
                from accounts import registry

                with registry._users_lock:
                    registry._users[:] = [
                        row
                        for row in registry._users
                        if row.get("user_id") not in user_ids
                    ]
        _SEEDED_USER_IDS.clear()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _seed_model_api_user(user_id: str) -> None:
    _SEEDED_USER_IDS.add(user_id)
    seed_user(user_id, created_at=_SEED_CREATED_AT)
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
# POST /v1/admin/runtime-allowlist
# --------------------------------------------------------------------------- #

def test_post_sets_desired_v2(env):
    uid = _uid("allow_v2")
    _seed_model_api_user(uid)

    status, body = _asgi_json(
        "POST", "/v1/admin/runtime-allowlist", headers=_admin(),
        json={"user_id": uid, "desired": "v2", "note": "canary"},
    )

    assert status == 200
    assert body == {"user_id": uid, "desired": "v2"}
    assert db.get_runtime_allowlist_map()[uid] == "v2"
    row = next(r for r in db.list_runtime_allowlist() if r["user_id"] == uid)
    assert row["note"] == "canary"


def test_post_desired_remove_deletes_row(env):
    uid = _uid("allow_remove")
    _seed_model_api_user(uid)
    db.upsert_runtime_allowlist(uid, "v2")

    status, body = _asgi_json(
        "POST", "/v1/admin/runtime-allowlist", headers=_admin(),
        json={"user_id": uid, "desired": "remove"},
    )

    assert status == 200
    assert body == {"user_id": uid, "removed": True}
    assert uid not in db.get_runtime_allowlist_map()

    # Idempotent: removing again reports removed=False, not an error.
    status, body = _asgi_json(
        "POST", "/v1/admin/runtime-allowlist", headers=_admin(),
        json={"user_id": uid, "desired": "remove"},
    )
    assert status == 200
    assert body == {"user_id": uid, "removed": False}


def test_post_desired_remove_on_v2_user_reverts_on_next_reconcile(env, monkeypatch):
    """The rollback runbook (spec §9) is "移出名单回 V1" — this pins that a
    reconcile tick after the removal actually flips the fence back, not just
    that the allowlist row disappears (regression cover for the reconciler
    scope bug: dropping the row must not strand a fenced-v2 user forever)."""
    from core import store as core_store
    from hosted import config_store, runtime_reconciler

    monkeypatch.delenv("FEEDLING_RUNTIME_DEFAULT_DESIRED", raising=False)
    uid = _uid("allow_remove_v2")
    _seed_model_api_user(uid)
    db.upsert_runtime_allowlist(uid, "v2")
    runtime_reconciler.reconcile_once()
    mode, state, _ = config_store.get_hosted_runtime_control_strict(core_store.get_store(uid))
    assert (mode, state) == ("db_action_v2", "v2")

    status, body = _asgi_json(
        "POST", "/v1/admin/runtime-allowlist", headers=_admin(),
        json={"user_id": uid, "desired": "remove"},
    )
    assert status == 200
    assert body == {"user_id": uid, "removed": True}
    assert uid not in db.get_runtime_allowlist_map()

    stats = runtime_reconciler.reconcile_once()
    assert stats["flipped"] >= 1
    mode, state, _ = config_store.get_hosted_runtime_control_strict(core_store.get_store(uid))
    assert (mode, state) == ("resident_cli", "resident")


def test_post_invalid_desired_returns_400(env):
    uid = _uid("allow_bad")
    _seed_model_api_user(uid)

    status, body = _asgi_json(
        "POST", "/v1/admin/runtime-allowlist", headers=_admin(),
        json={"user_id": uid, "desired": "bogus"},
    )

    assert status == 400
    assert "error" in body
    assert uid not in db.get_runtime_allowlist_map()


def test_post_missing_user_id_or_desired_returns_400(env):
    status, body = _asgi_json(
        "POST", "/v1/admin/runtime-allowlist", headers=_admin(), json={"desired": "v2"},
    )
    assert status == 400

    uid = _uid("allow_missing")
    status, body = _asgi_json(
        "POST", "/v1/admin/runtime-allowlist", headers=_admin(), json={"user_id": uid},
    )
    assert status == 400


# --------------------------------------------------------------------------- #
# GET /v1/admin/runtime-allowlist (reconciliation view)
# --------------------------------------------------------------------------- #

def test_get_reports_converged_and_unconverged_rows(env):
    from core import store as core_store
    from hosted import config_store

    uid_converged = _uid("allow_conv")
    uid_pending = _uid("allow_pending")
    _seed_model_api_user(uid_converged)
    _seed_model_api_user(uid_pending)

    # Converged: fence already flipped to v2, allowlist agrees.
    config_store.set_hosted_runtime_mode(core_store.get_store(uid_converged), "db_action_v2")
    db.upsert_runtime_allowlist(uid_converged, "v2")

    # Pending: allowlist wants v2, fence is still resident (never reconciled).
    db.upsert_runtime_allowlist(uid_pending, "v2")

    status, body = _asgi_json("GET", "/v1/admin/runtime-allowlist", headers=_admin())

    assert status == 200
    rows = {r["user_id"]: r for r in body["allowlist"]}
    assert rows[uid_converged]["converged"] is True
    assert rows[uid_converged]["actual"]["mode"] == "db_action_v2"
    assert rows[uid_converged]["actual"]["state"] == "v2"
    assert rows[uid_pending]["converged"] is False
    assert rows[uid_pending]["actual"]["state"] == "resident"


# --------------------------------------------------------------------------- #
# admin-token auth
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "path,method",
    [
        ("/v1/admin/runtime-allowlist", "POST"),
        ("/v1/admin/runtime-allowlist", "GET"),
    ],
)
def test_no_token_is_401(env, path, method):
    status, body = _asgi_json(
        method, path,
        json={"user_id": "x", "desired": "v2"} if method == "POST" else None,
    )
    assert status == 401
    assert body == {"error": "unauthorized"}
