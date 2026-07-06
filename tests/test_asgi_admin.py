"""Native admin data-track parity: 7 admin-token-gated routes (5 JSON, 2 HTML).

Asserts the FastAPI routes (admin.routes_asgi) return the same status/body as the
Flask oracle (admin.data_track) — both run the *same* admin.data_track functions,
the ASGI side via admin.admin_core (which materialises a Flask request context
from the query string). Covers:
  - JSON routes (summary / users / dau / users/{id}): status + body parity,
    with the volatile ``generated_at`` / ``stuck_for_sec`` fields normalised.
  - HTML pages (/admin/data-track [+ ?view=dau], /admin/data-track/users/{id}):
    status + Content-Type + body parity, with the embedded ``generated_at``
    ISO timestamp normalised; the 404 branch is text/plain.
  - store/evict: side-effect payload + the 400 (missing user_id) branch.
  - admin-token auth: 401 (missing/bad) + 503 (unconfigured), mirroring copytext.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import re
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from admin import routes_asgi as admin_asgi  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402

ADMIN_TOKEN = "admin-test-token"
_pk_counter = itertools.count(1)


def _build_asgi_app() -> FastAPI:
    # Standalone app: the admin router + the fixed-body exception handlers,
    # independent of asgi_app.py's package list (owned by the orchestrator).
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    admin_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    yield


def _register() -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(raw).decode("ascii"), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


# --------------------------------------------------------------------------- #
# request helpers
# --------------------------------------------------------------------------- #

def _flask_get_json(path, headers=None):
    res = make_client().get(path, headers=headers or {})
    return res.status_code, res.get_json(silent=True)


def _flask_get_raw(path, headers=None):
    res = make_client().get(path, headers=headers or {})
    return res.status_code, res.get_data(as_text=True), res.headers.get("Content-Type")


def _asgi(method, path, headers=None, **kw):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.request(method, path, headers=headers or {}, **kw)
            return resp

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


def _asgi_raw(method, path, headers=None, **kw):
    resp = _asgi(method, path, headers=headers, **kw)
    return resp.status_code, resp.text, resp.headers.get("content-type")


def _admin(token=ADMIN_TOKEN):
    return {"X-Admin-Token": token}


# --------------------------------------------------------------------------- #
# normalisers for volatile fields
# --------------------------------------------------------------------------- #

_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?")


def _norm_json(obj):
    """Blank out fields that depend on wall-clock time between the two calls."""
    if isinstance(obj, dict):
        return {
            k: ("NORM" if k in ("generated_at", "stuck_for_sec") else _norm_json(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_norm_json(x) for x in obj]
    return obj


def _norm_html(text: str) -> str:
    return _TS_RE.sub("TS", text)


# --------------------------------------------------------------------------- #
# JSON routes — parity
# --------------------------------------------------------------------------- #

def test_summary_parity_empty(env):
    f = _flask_get_json("/v1/admin/data-track/summary", headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/summary", headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    assert f[1]["summary"]["users_total"] == 0
    assert "users" not in f[1]


def test_users_parity_empty(env):
    f = _flask_get_json("/v1/admin/data-track/users", headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/users", headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    assert f[1]["users"] == []
    assert f[1]["pagination"]["total"] == 0


def test_users_parity_with_user(env):
    uid, _key = _register()
    f = _flask_get_json("/v1/admin/data-track/users", headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/users", headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    assert any(u["user_id"] == uid for u in f[1]["users"])


def test_users_query_params_parity(env):
    _register()
    qs = "?sort=chat&dir=asc&limit=10&offset=0&q=en"
    f = _flask_get_json("/v1/admin/data-track/users" + qs, headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/users" + qs, headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    # The filter echo must reflect the query string parsed on the ASGI side.
    assert f[1]["filters"]["sort"] == "chat"
    assert f[1]["filters"]["dir"] == "asc"


def test_dau_parity(env):
    f = _flask_get_json("/v1/admin/data-track/dau", headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/dau", headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    assert f[1]["summary"]["timezone"] == "Asia/Shanghai"


def test_user_detail_parity(env):
    uid, _key = _register()
    f = _flask_get_json(f"/v1/admin/data-track/users/{uid}", headers=_admin())
    a = _asgi_json("GET", f"/v1/admin/data-track/users/{uid}", headers=_admin())
    assert f[0] == a[0] == 200
    assert _norm_json(f[1]) == _norm_json(a[1])
    assert f[1]["user"]["user_id"] == uid


def test_user_detail_not_found_parity(env):
    f = _flask_get_json("/v1/admin/data-track/users/does-not-exist", headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/users/does-not-exist", headers=_admin())
    assert f == a
    assert f == (404, {"error": "user_not_found"})


# --------------------------------------------------------------------------- #
# HTML pages — parity (status + Content-Type + normalised body)
# --------------------------------------------------------------------------- #

def test_data_track_page_parity(env):
    f_status, f_body, f_ct = _flask_get_raw("/admin/data-track", headers=_admin())
    a_status, a_body, a_ct = _asgi_raw("GET", "/admin/data-track", headers=_admin())
    assert f_status == a_status == 200
    assert f_ct == a_ct == "text/html; charset=utf-8"
    assert _norm_html(f_body) == _norm_html(a_body)
    assert "Feedling Beta Data Track" in f_body


def test_data_track_dau_page_parity(env):
    f_status, f_body, f_ct = _flask_get_raw("/admin/data-track?view=dau", headers=_admin())
    a_status, a_body, a_ct = _asgi_raw("GET", "/admin/data-track?view=dau", headers=_admin())
    assert f_status == a_status == 200
    assert f_ct == a_ct == "text/html; charset=utf-8"
    assert _norm_html(f_body) == _norm_html(a_body)
    assert "Daily Active Users" in f_body


def test_user_detail_page_existing(env):
    uid, _key = _register()
    f_status, f_body, f_ct = _flask_get_raw(f"/admin/data-track/users/{uid}", headers=_admin())
    a_status, a_body, a_ct = _asgi_raw("GET", f"/admin/data-track/users/{uid}", headers=_admin())
    assert f_status == a_status == 200
    assert f_ct == a_ct == "text/html; charset=utf-8"
    # Body embeds a volatile JSON dump (stuck_for_sec) — assert stable substrings.
    for needle in (uid, "Back to data track", "chat messages"):
        assert needle in f_body
        assert needle in a_body


def test_user_detail_page_not_found_parity(env):
    f_status, f_body, f_ct = _flask_get_raw("/admin/data-track/users/nope", headers=_admin())
    a_status, a_body, a_ct = _asgi_raw("GET", "/admin/data-track/users/nope", headers=_admin())
    assert f_status == a_status == 404
    assert f_ct == a_ct == "text/plain; charset=utf-8"
    assert f_body == a_body == "user not found"


# --------------------------------------------------------------------------- #
# store/evict (POST)
# --------------------------------------------------------------------------- #

def test_store_evict_missing_user_id_parity(env):
    f = make_client().post("/v1/admin/store/evict", headers=_admin(), json={})
    a = _asgi_json("POST", "/v1/admin/store/evict", headers=_admin(), json={})
    assert (f.status_code, f.get_json(silent=True)) == a
    assert a == (400, {"error": "user_id required"})


def test_store_evict_uncached_parity(env):
    # A never-cached user id evicts to False on both sides (no state consumed).
    unique = f"evict-{uuid.uuid4().hex}"
    f = make_client().post(
        "/v1/admin/store/evict", headers=_admin(), json={"user_id": unique}
    )
    a = _asgi_json("POST", "/v1/admin/store/evict", headers=_admin(), json={"user_id": unique})
    assert (f.status_code, f.get_json(silent=True)) == a
    assert a == (200, {"evicted": False, "user_id": unique})


def test_store_evict_query_param(env):
    unique = f"evict-{uuid.uuid4().hex}"
    a = _asgi_json("POST", f"/v1/admin/store/evict?user_id={unique}", headers=_admin())
    assert a == (200, {"evicted": False, "user_id": unique})


def test_store_evict_cached_true(env):
    uid, _key = _register()
    core_store.get_store(uid)  # cache it
    a = _asgi_json("POST", "/v1/admin/store/evict", headers=_admin(), json={"user_id": uid})
    assert a == (200, {"evicted": True, "user_id": uid})


# --------------------------------------------------------------------------- #
# admin-token auth parity (mirrors copytext admin tests)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "path,method",
    [
        ("/v1/admin/data-track/summary", "GET"),
        ("/admin/data-track", "GET"),
        ("/v1/admin/store/evict", "POST"),
    ],
)
def test_no_token_is_401_parity(env, path, method):
    f = make_client().open(path, method=method)
    a = _asgi_json(method, path)
    assert (f.status_code, f.get_json(silent=True)) == a
    assert a == (401, {"error": "unauthorized"})


def test_wrong_token_is_401_parity(env):
    f = _flask_get_json("/v1/admin/data-track/summary", headers=_admin("wrong"))
    a = _asgi_json("GET", "/v1/admin/data-track/summary", headers=_admin("wrong"))
    assert f == a
    assert a == (401, {"error": "unauthorized"})


def test_unconfigured_is_503_parity(env, monkeypatch):
    monkeypatch.delenv("FEEDLING_ADMIN_TOKEN", raising=False)
    f = _flask_get_json("/v1/admin/data-track/summary", headers=_admin())
    a = _asgi_json("GET", "/v1/admin/data-track/summary", headers=_admin())
    assert f == a
    assert a == (503, {"error": "service_unavailable", "detail": "admin token is not configured"})
