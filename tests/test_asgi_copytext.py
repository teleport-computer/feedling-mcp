"""Native /v1/copytext parity: public read (ETag/304) + admin-gated edit.

Asserts the FastAPI routes (copytext.routes_asgi) return the same status/body as
the Flask oracle (copytext.routes) for the read bundle, ETag/304 negotiation,
admin-token auth (401/503), and payload validation (400) — both sides call the
same framework-neutral copytext_core, and the copytext tables are global (no
per-user scoping) so an autouse fixture resets them for deterministic revisions.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from copytext import routes_asgi as copytext_asgi  # noqa: E402
from fastapi import FastAPI  # noqa: E402

ADMIN_TOKEN = "test-admin-token"
SEED = {"strings": {"chat.empty.title": {"en": "Hi", "zh-Hans": "你好"}}}


def _reset() -> None:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM copytext_strings")
        conn.execute("UPDATE copytext_meta SET revision = 0 WHERE id = TRUE")


@pytest.fixture(autouse=True)
def _reset_copytext():
    _reset()
    yield


@pytest.fixture()
def admin_env(monkeypatch):
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)


def _build_asgi_app() -> FastAPI:
    # Standalone app: the copytext router + the fixed-body exception handlers,
    # independent of asgi_app.py's package list (owned by the orchestrator).
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    copytext_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


# --------------------------------------------------------------------------- #
# request helpers (each returns a comparable tuple)
# --------------------------------------------------------------------------- #

def _flask_get(path: str, headers: dict | None = None):
    res = make_client().get(path, headers=headers or {})
    return res.status_code, res.get_json(silent=True), res.headers.get("ETag")


def _asgi_get(path: str, headers: dict | None = None):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get(path, headers=headers or {})
            body = None
            if resp.content:
                try:
                    body = resp.json()
                except Exception:
                    body = None
            return resp.status_code, body, resp.headers.get("etag")

    return asyncio.run(go())


def _flask_post(path: str, *, headers=None, json_body=None, data=None):
    kw: dict = {}
    if json_body is not None:
        kw["json"] = json_body
    if data is not None:
        kw["data"] = data
    res = make_client().post(path, headers=headers or {}, **kw)
    return res.status_code, res.get_json(silent=True)


def _asgi_post(path: str, *, headers=None, json_body=None, content=None):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            kw: dict = {}
            if json_body is not None:
                kw["json"] = json_body
            if content is not None:
                kw["content"] = content
            resp = await client.post(path, headers=headers or {}, **kw)
            body = None
            if resp.content:
                try:
                    body = resp.json()
                except Exception:
                    body = None
            return resp.status_code, body

    return asyncio.run(go())


def _admin(token: str = ADMIN_TOKEN) -> dict[str, str]:
    return {"X-Admin-Token": token}


# --------------------------------------------------------------------------- #
# GET parity
# --------------------------------------------------------------------------- #

def test_get_empty_bundle_parity():
    f = _flask_get("/v1/copytext")
    a = _asgi_get("/v1/copytext")
    assert f == a
    assert f == (200, {"revision": 0, "strings": {}}, '"0"')


def test_get_seeded_bundle_parity(admin_env):
    _flask_post("/v1/copytext", headers=_admin(), json_body=SEED)  # revision 1
    f = _flask_get("/v1/copytext")
    _reset()
    _asgi_post("/v1/copytext", headers=_admin(), json_body=SEED)  # revision 1
    a = _asgi_get("/v1/copytext")
    assert f == a
    assert f[0] == 200
    assert f[2] == '"1"'
    assert f[1]["strings"]["chat.empty.title"]["zh-Hans"] == "你好"


def test_get_304_on_matching_etag_parity(admin_env):
    _flask_post("/v1/copytext", headers=_admin(), json_body=SEED)
    f_200 = _flask_get("/v1/copytext")
    f_304 = _flask_get("/v1/copytext", {"If-None-Match": f_200[2]})
    _reset()
    _asgi_post("/v1/copytext", headers=_admin(), json_body=SEED)
    a_200 = _asgi_get("/v1/copytext")
    a_304 = _asgi_get("/v1/copytext", {"If-None-Match": a_200[2]})
    # 200 bodies compare fully; 304 clients ignore the body (Werkzeug strips it),
    # so status + ETag is the contract that must match.
    assert f_200 == a_200
    assert f_304[0] == a_304[0] == 304
    assert f_304[2] == a_304[2] == '"1"'


def test_get_304_has_empty_body(admin_env):
    """A 304 MUST carry a zero-length body. copytext_core returns ``{}`` on an
    If-None-Match hit; rendering that as JSON (``b"{}"``) makes real uvicorn
    (httptools) raise "Response content longer than Content-Length" → 500. The
    ASGITransport used by the other tests does not enforce that rule, so assert
    the empty body explicitly here."""
    _asgi_post("/v1/copytext", headers=_admin(), json_body=SEED)
    etag = _asgi_get("/v1/copytext")[2]

    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get("/v1/copytext", headers={"If-None-Match": etag})
            return resp.status_code, resp.content, resp.headers.get("etag")

    status, content, got_etag = asyncio.run(go())
    assert status == 304
    assert content == b"", f"304 must have empty body, got {content!r}"
    assert got_etag == etag


def test_get_stale_etag_returns_200_parity(admin_env):
    _flask_post("/v1/copytext", headers=_admin(), json_body=SEED)
    f = _flask_get("/v1/copytext", {"If-None-Match": '"0"'})
    _reset()
    _asgi_post("/v1/copytext", headers=_admin(), json_body=SEED)
    a = _asgi_get("/v1/copytext", {"If-None-Match": '"0"'})
    assert f == a
    assert f[0] == 200


# --------------------------------------------------------------------------- #
# POST auth parity
# --------------------------------------------------------------------------- #

def test_post_no_token_is_401_parity(admin_env):
    body = {"strings": {"k": {"en": "v"}}}
    f = _flask_post("/v1/copytext", json_body=body)
    a = _asgi_post("/v1/copytext", json_body=body)
    assert f == a
    assert f == (401, {"error": "unauthorized"})


def test_post_wrong_token_is_401_parity(admin_env):
    body = {"strings": {"k": {"en": "v"}}}
    f = _flask_post("/v1/copytext", headers=_admin("wrong"), json_body=body)
    a = _asgi_post("/v1/copytext", headers=_admin("wrong"), json_body=body)
    assert f == a
    assert f == (401, {"error": "unauthorized"})


def test_post_no_admin_configured_is_503_parity(monkeypatch):
    monkeypatch.delenv("FEEDLING_ADMIN_TOKEN", raising=False)
    body = {"strings": {"k": {"en": "v"}}}
    f = _flask_post("/v1/copytext", headers=_admin(), json_body=body)
    a = _asgi_post("/v1/copytext", headers=_admin(), json_body=body)
    assert f == a
    assert f == (
        503,
        {"error": "service_unavailable", "detail": "admin token is not configured"},
    )


# --------------------------------------------------------------------------- #
# POST write + validation parity
# --------------------------------------------------------------------------- #

def test_post_valid_edit_parity(admin_env):
    body = {"strings": {"k": {"en": "v"}}}
    f = _flask_post("/v1/copytext", headers=_admin(), json_body=body)
    _reset()
    a = _asgi_post("/v1/copytext", headers=_admin(), json_body=body)
    assert f == a
    assert f == (200, {"revision": 1, "upserted": 1, "deleted": 0})


def test_post_bad_lang_is_400_parity(admin_env):
    body = {"strings": {"k": {"fr": "v"}}}
    f = _flask_post("/v1/copytext", headers=_admin(), json_body=body)
    a = _asgi_post("/v1/copytext", headers=_admin(), json_body=body)
    assert f == a
    assert f[0] == 400
    assert f[1]["error"] == "invalid_payload"


def test_post_empty_body_is_400_parity(admin_env):
    f = _flask_post("/v1/copytext", headers=_admin(), json_body={})
    a = _asgi_post("/v1/copytext", headers=_admin(), json_body={})
    assert f == a
    assert f[0] == 400
    assert f[1]["error"] == "invalid_payload"


@pytest.mark.parametrize("bad", [[1], "x", 5])
def test_post_non_object_body_is_400_parity(admin_env, bad):
    hdr = {**_admin(), "Content-Type": "application/json"}
    f = _flask_post("/v1/copytext", headers=hdr, data=json.dumps(bad))
    a = _asgi_post("/v1/copytext", headers=hdr, content=json.dumps(bad))
    assert f == a
    assert f[0] == 400, f"{bad!r} → {f}"
    assert f[1]["error"] == "invalid_payload"
