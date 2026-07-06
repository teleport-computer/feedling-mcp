"""Native /v1/worldbook/* parity (ASGI-migration plan §5.3 / §9).

Asserts the FastAPI routes (``worldbook.routes_asgi``) return the same
status/body as the Flask oracle (``worldbook.routes``) for list/upsert/match/
delete, plus auth-failure (401) and validation (400) paths. Both sides call the
same framework-neutral ``worldbook.worldbook_core``, so the enclave stub is
installed once on the shared ``worldbook_readside_core`` module object and covers
both paths — keeping the test fully offline and the E2E envelope handling
identical across frameworks (the server never decrypts; the enclave call is
stubbed).

These routes are gated on ``auth.require_user()`` only (no
``runtime_auth.authorize_scope``), so there is no scope-failure (403) case — the
ASGI router deliberately carries no ``require_scope``.
"""

from __future__ import annotations

import asyncio
import sys
import base64
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from worldbook import routes_asgi as worldbook_asgi  # noqa: E402
from worldbook import worldbook_core  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _build_asgi_app() -> FastAPI:
    # Standalone app: the worldbook router + fixed-body exception handlers,
    # independent of asgi_app.py's package list (owned by the orchestrator).
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    worldbook_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _env(user_id: str, entry_id: str = "wb1", *, body_ct: str = "ct") -> dict:
    return {
        "v": 1,
        "id": entry_id,
        "body_ct": body_ct,
        "nonce": "nonce",
        "K_user": "k-user",
        "K_enclave": "k-enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
        "enclave_pk_fpr": "fpr",
    }


def _reset_world_books(user_id: str) -> None:
    store = core_store.get_store(user_id)
    with store.world_books_lock:
        ids = [str(item.get("id") or "") for item in store.world_books]
    for entry_id in ids:
        if entry_id:
            store.delete_world_book(entry_id)


# --------------------------------------------------------------------------- #
# request helpers
# --------------------------------------------------------------------------- #

def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _flask_get(path: str, headers: dict | None = None):
    res = make_client().get(path, headers=headers or {})
    return res.status_code, res.get_json(silent=True)


def _flask_post(path: str, *, headers=None, json_body=None):
    res = make_client().post(path, headers=headers or {}, json=json_body)
    return res.status_code, res.get_json(silent=True)


def _flask_delete(path: str, headers: dict | None = None):
    res = make_client().delete(path, headers=headers or {})
    return res.status_code, res.get_json(silent=True)


def _asgi(method: str, path: str, *, headers=None, json_body=None):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            kw: dict = {}
            if json_body is not None:
                kw["json"] = json_body
            resp = await client.request(method, path, headers=headers or {}, **kw)
            body = None
            if resp.content:
                try:
                    body = resp.json()
                except Exception:
                    body = None
            return resp.status_code, body

    return asyncio.run(go())


# --------------------------------------------------------------------------- #
# auth parity (401)
# --------------------------------------------------------------------------- #

def test_list_no_auth_is_401_parity(user):
    f = _flask_get("/v1/worldbook/list")
    a = _asgi("GET", "/v1/worldbook/list")
    assert f == a == (401, {"error": "unauthorized"})


def test_upsert_no_auth_is_401_parity(user):
    f = _flask_post("/v1/worldbook/upsert", json_body={})
    a = _asgi("POST", "/v1/worldbook/upsert", json_body={})
    assert f == a == (401, {"error": "unauthorized"})


def test_delete_no_auth_is_401_parity(user):
    f = _flask_delete("/v1/worldbook/delete?id=wb1")
    a = _asgi("DELETE", "/v1/worldbook/delete?id=wb1")
    assert f == a == (401, {"error": "unauthorized"})


# --------------------------------------------------------------------------- #
# list parity
# --------------------------------------------------------------------------- #

def test_list_empty_parity(user):
    uid, api_key = user
    f = _flask_get("/v1/worldbook/list", _headers(api_key))
    a = _asgi("GET", "/v1/worldbook/list", headers=_headers(api_key))
    assert f == a == (200, {"envelopes": []})


def test_upsert_then_list_parity(user):
    uid, api_key = user
    env = _env(uid, "wb1", body_ct="body-1")

    f_up = _flask_post("/v1/worldbook/upsert", headers=_headers(api_key), json_body=env)
    f_list = _flask_get("/v1/worldbook/list", _headers(api_key))
    _reset_world_books(uid)

    a_up = _asgi("POST", "/v1/worldbook/upsert", headers=_headers(api_key), json_body=env)
    a_list = _asgi("GET", "/v1/worldbook/list", headers=_headers(api_key))

    assert f_up == a_up == (200, {"id": "wb1"})
    # ``updated_at`` is stamped with ``datetime.now()`` at upsert time, so the two
    # sequential upserts legitimately differ by ms — blank it before comparing.
    def _blank_ts(resp):
        status, body = resp
        envs = [{**e, "updated_at": "<ts>"} for e in (body or {}).get("envelopes", [])]
        return status, {**(body or {}), "envelopes": envs}
    assert _blank_ts(f_list) == _blank_ts(a_list)
    assert f_list[0] == 200
    envelopes = f_list[1]["envelopes"]
    assert len(envelopes) == 1
    assert {k: envelopes[0][k] for k in env} == env


# --------------------------------------------------------------------------- #
# upsert validation parity (400, no state mutation)
# --------------------------------------------------------------------------- #

def test_upsert_outer_id_mismatch_400_parity(user):
    uid, api_key = user
    body = {"id": "outer", "envelope": _env(uid, "inner")}
    f = _flask_post("/v1/worldbook/upsert", headers=_headers(api_key), json_body=body)
    a = _asgi("POST", "/v1/worldbook/upsert", headers=_headers(api_key), json_body=body)
    assert f == a
    assert f[0] == 400
    assert f[1] == {"error": "top-level id must match envelope id"}


def test_upsert_wrong_owner_400_parity(user):
    _uid, api_key = user
    body = _env("other-user", "wb1")
    f = _flask_post("/v1/worldbook/upsert", headers=_headers(api_key), json_body=body)
    a = _asgi("POST", "/v1/worldbook/upsert", headers=_headers(api_key), json_body=body)
    assert f == a
    assert f == (400, {"error": "owner_user_id does not match caller"})


# --------------------------------------------------------------------------- #
# delete parity
# --------------------------------------------------------------------------- #

def test_delete_missing_id_400_parity(user):
    _uid, api_key = user
    f = _flask_delete("/v1/worldbook/delete", _headers(api_key))
    a = _asgi("DELETE", "/v1/worldbook/delete", headers=_headers(api_key))
    assert f == a == (400, {"error": "id required"})


def test_delete_ok_parity(user):
    _uid, api_key = user
    f = _flask_delete("/v1/worldbook/delete?id=wbX", _headers(api_key))
    a = _asgi("DELETE", "/v1/worldbook/delete?id=wbX", headers=_headers(api_key))
    assert f == a == (200, {"ok": True})


# --------------------------------------------------------------------------- #
# match parity (enclave stubbed; read-only so shared store is safe)
# --------------------------------------------------------------------------- #

def test_match_parity_with_stubbed_enclave(user, monkeypatch):
    uid, api_key = user
    up = _flask_post("/v1/worldbook/upsert", headers=_headers(api_key),
                     json_body=_env(uid, "wb-match"))
    assert up[0] == 200

    def fake_match(api_key_arg, world_books, messages, *, runtime_token=None):
        assert api_key_arg == api_key
        assert [item["id"] for item in world_books] == ["wb-match"]
        assert messages == [{"role": "user", "content": "hello trigger"}]
        return {
            "block": "<world_book>\nhello\n</world_book>",
            "matched_names": ["Match"],
            "rejected_over_cap": [],
            "unavailable_ids": [],
        }

    # One patch on the shared module object covers both Flask + ASGI paths.
    monkeypatch.setattr(
        worldbook_core.worldbook_readside_core, "post_enclave_worldbook_match", fake_match)

    body = {"message": "hello trigger"}
    f = _flask_post("/v1/worldbook/match", headers=_headers(api_key), json_body=body)
    a = _asgi("POST", "/v1/worldbook/match", headers=_headers(api_key), json_body=body)
    assert f == a
    assert f[0] == 200
    assert f[1] == {
        "block": "<world_book>\nhello\n</world_book>",
        "matched_names": ["Match"],
        "rejected_over_cap": [],
        "unavailable_ids": [],
    }


def test_match_empty_worldbook_short_circuits_parity(user):
    _uid, api_key = user
    body = {"message": "anything"}
    f = _flask_post("/v1/worldbook/match", headers=_headers(api_key), json_body=body)
    a = _asgi("POST", "/v1/worldbook/match", headers=_headers(api_key), json_body=body)
    assert f == a
    assert f == (200, {"block": "", "matched_names": [], "rejected_over_cap": [],
                       "unavailable_ids": []})


def test_upsert_over_cap_400_parity(user, monkeypatch):
    uid, api_key = user

    def fake_validate(api_key_arg, world_books, messages, *, runtime_token=None):
        assert [item["id"] for item in world_books] == ["too-big"]
        assert messages == []
        return {"block": "", "matched_names": [], "rejected_over_cap": ["too-big"]}

    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "http://enclave.test")
    monkeypatch.setattr(
        worldbook_core.worldbook_readside_core, "post_enclave_worldbook_match", fake_validate)

    body = _env(uid, "too-big")
    f = _flask_post("/v1/worldbook/upsert", headers=_headers(api_key), json_body=body)
    a = _asgi("POST", "/v1/worldbook/upsert", headers=_headers(api_key), json_body=body)
    assert f == a
    assert f == (400, {"error": "content_too_long", "id": "too-big", "max_chars": 20000})
    # Rejected before persist — nothing stored.
    assert _flask_get("/v1/worldbook/list", _headers(api_key))[1] == {"envelopes": []}
