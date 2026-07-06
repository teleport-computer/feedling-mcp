"""Native /v1/memory/* parity (ASGI-migration plan §5.3 / §9).

Asserts the FastAPI routes (``memory.routes_asgi``) return the same status/body as
the Flask oracle (``memory.routes``) for every route, plus auth-failure (401) and
scope-failure (403) on the three scope-gated write surfaces (``/actions``,
``/legacy_batch`` and the POST side of ``/migration_state``). Both sides call the
same framework-neutral ``memory.memory_core``, so a single monkeypatch on the
shared enclave / service module objects covers both paths — keeping the test
fully offline and the E2E envelope handling identical across frameworks (the
server never decrypts; the enclave call is stubbed).
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from memory import actions as memory_actions_mod  # noqa: E402
from memory import memory_core  # noqa: E402
from memory import routes_asgi as memory_asgi  # noqa: E402
from memory import service as memory_service  # noqa: E402
import memory_readside_core  # noqa: E402

_SECRET = "test-runtime-secret"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    memory_asgi.register_asgi(app)
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


# --------------------------------------------------------------------------- #
# request helpers
# --------------------------------------------------------------------------- #

def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _flask(method: str, path: str, *, headers=None, json_body=None):
    client = make_client()
    kw: dict = {"headers": headers or {}}
    if json_body is not None:
        kw["json"] = json_body
    res = client.open(path, method=method, **kw)
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


def _both(method, path, *, api_key=None, json_body=None):
    headers = _headers(api_key) if api_key else None
    f = _flask(method, path, headers=headers, json_body=json_body)
    a = _asgi(method, path, headers=headers, json_body=json_body)
    return f, a


def _envelope(user_id: str, mid: str = "mom_test", *, mem_type: str = "fact") -> dict:
    return {
        "id": mid,
        "type": mem_type,
        "body_ct": "ct",
        "nonce": "n",
        "K_user": "ku",
        "K_enclave": "ke",
        "visibility": "shared",
        "owner_user_id": user_id,
        "occurred_at": "2026-06-20T10:00:00",
        "source": "chat",
    }


def _stub_enclave(monkeypatch, fn):
    # One patch on the shared module object covers both Flask + ASGI paths.
    monkeypatch.setattr(memory_readside_core, "post_enclave_readside", fn)


# --------------------------------------------------------------------------- #
# auth-failure (401)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path,body", [
    ("GET", "/v1/memory/list", None),
    ("GET", "/v1/memory/get?id=x", None),
    ("GET", "/v1/memory/verify", None),
    ("GET", "/v1/memory/buckets", None),
    ("GET", "/v1/memory/threads", None),
    ("GET", "/v1/memory/migration_state", None),
    ("POST", "/v1/memory/index", {}),
    ("POST", "/v1/memory/fetch", {"ids": []}),
    ("POST", "/v1/memory/actions", {}),
    ("POST", "/v1/memory/migration_state", {}),
    ("POST", "/v1/memory/legacy_batch", {}),
    ("POST", "/v1/memory/add", {}),
    ("POST", "/v1/memory/retype", {}),
    ("DELETE", "/v1/memory/delete?id=x", None),
])
def test_no_auth_is_401_parity(user, method, path, body):
    f, a = _both(method, path, json_body=body)
    assert f == a == (401, {"error": "unauthorized"})


# --------------------------------------------------------------------------- #
# scope-failure (403) on the three scope-gated write surfaces
# --------------------------------------------------------------------------- #

def _token(user_id: str, scope: list[str]) -> str:
    return runtime_token.mint(
        _SECRET.encode("utf-8"),
        user_id=user_id,
        runtime_instance_id="ri_test",
        scope=scope,
        ttl=900.0,
    )


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/v1/memory/actions", {"actions": []}),
    ("POST", "/v1/memory/migration_state", {"migrated": 0, "legacy_remaining": 0}),
    ("POST", "/v1/memory/legacy_batch", {"batch_size": 8}),
])
def test_scope_missing_is_403_parity(user, monkeypatch, method, path, body):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    uid, _api_key = user
    tok = _token(uid, ["identity"])  # NOT memory
    headers = {"X-Feedling-Runtime-Token": tok}
    f = _flask(method, path, headers=headers, json_body=body)
    a = _asgi(method, path, headers=headers, json_body=body)
    assert f == a == (403, {"error": "forbidden"})


def test_migration_state_get_allows_scopeless_token_parity(user, monkeypatch):
    # GET side is auth-only (no scope) — a token without memory scope must pass.
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    monkeypatch.setattr(db, "get_blob", lambda *_a, **_k: None)
    uid, _api_key = user
    headers = {"X-Feedling-Runtime-Token": _token(uid, ["identity"])}
    f = _flask("GET", "/v1/memory/migration_state", headers=headers)
    a = _asgi("GET", "/v1/memory/migration_state", headers=headers)
    assert f == a
    assert f[0] == 200 and "state" in f[1]


# --------------------------------------------------------------------------- #
# list / get / delete (plain store)
# --------------------------------------------------------------------------- #

def test_list_empty_parity(user):
    _uid, api_key = user
    f, a = _both("GET", "/v1/memory/list", api_key=api_key)
    assert f == a == (200, {"moments": [], "total": 0})


def test_list_invalid_limit_400_parity(user):
    _uid, api_key = user
    f, a = _both("GET", "/v1/memory/list?limit=abc", api_key=api_key)
    assert f == a == (400, {"error": "invalid limit"})


def test_get_missing_id_400_parity(user):
    _uid, api_key = user
    f, a = _both("GET", "/v1/memory/get", api_key=api_key)
    assert f == a == (400, {"error": "id required"})


def test_get_not_found_404_parity(user):
    _uid, api_key = user
    f, a = _both("GET", "/v1/memory/get?id=nope", api_key=api_key)
    assert f == a == (404, {"error": "not_found"})


def test_delete_missing_id_400_parity(user):
    _uid, api_key = user
    f, a = _both("DELETE", "/v1/memory/delete", api_key=api_key)
    assert f == a == (400, {"error": "id required"})


def test_delete_not_found_404_parity(user):
    _uid, api_key = user
    f, a = _both("DELETE", "/v1/memory/delete?id=nope", api_key=api_key)
    assert f == a == (404, {"error": "not_found"})


# --------------------------------------------------------------------------- #
# verify (plain store, deterministic within a day)
# --------------------------------------------------------------------------- #

def test_verify_parity(user):
    _uid, api_key = user
    f, a = _both("GET", "/v1/memory/verify", api_key=api_key)
    assert f == a
    assert f[0] == 200
    assert f[1]["archive_language"] == "en"
    assert f[1]["counts"]["total"] == 0


# --------------------------------------------------------------------------- #
# index / fetch / buckets / threads (enclave stubbed)
# --------------------------------------------------------------------------- #

def test_index_parity_with_stubbed_enclave(user, monkeypatch):
    uid, api_key = user
    moments = [{
        "v": 1, "id": "m1", "owner_user_id": uid, "visibility": "shared",
        "body_ct": "ct", "nonce": "n", "K_user": "ku", "K_enclave": "ke",
        "status": "active", "importance": 0.9, "occurred_at": "2026-06-20T10:00:00",
    }]
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [dict(m) for m in moments])

    def fake_enclave(api_key_arg, candidates, *, operation, payload=None, runtime_token=None):
        assert api_key_arg == api_key
        assert operation == "index"
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    _stub_enclave(monkeypatch, fake_enclave)
    f, a = _both("POST", "/v1/memory/index", api_key=api_key, json_body={})
    assert f == a
    assert f[0] == 200
    assert [it["id"] for it in f[1]["items"]] == ["m1"]
    assert f[1]["user_card_count"] == 1


def test_index_invalid_limit_400_parity(user):
    _uid, api_key = user
    f, a = _both("POST", "/v1/memory/index", api_key=api_key, json_body={"limit": -3})
    assert f == a == (400, {"error": "invalid limit"})


def test_fetch_bad_ids_400_parity(user):
    _uid, api_key = user
    f, a = _both("POST", "/v1/memory/fetch", api_key=api_key, json_body={"ids": "not-a-list"})
    assert f == a == (400, {"error": "ids must be a list of non-empty strings"})


def test_fetch_parity_with_stubbed_enclave(user, monkeypatch):
    uid, api_key = user
    moments = [{
        "v": 1, "id": "m1", "owner_user_id": uid, "visibility": "shared",
        "body_ct": "ct", "nonce": "n", "K_user": "ku", "K_enclave": "ke",
        "status": "active", "importance": 0.5, "occurred_at": "2026-06-20T10:00:00",
    }]
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [dict(m) for m in moments])
    monkeypatch.setattr(memory_service, "_save_moments", lambda _store, _moments: None)

    def fake_enclave(api_key_arg, candidates, *, operation, payload=None, runtime_token=None):
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates], "unavailable_ids": []}

    _stub_enclave(monkeypatch, fake_enclave)
    f, a = _both("POST", "/v1/memory/fetch", api_key=api_key, json_body={"ids": ["m1", "missing"]})
    assert f == a
    assert f[0] == 200
    assert [it["id"] for it in f[1]["items"]] == ["m1"]
    assert f[1]["missing_ids"] == ["missing"]


def test_buckets_and_threads_parity_with_stubbed_enclave(user, monkeypatch):
    uid, api_key = user
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [])

    def fake_enclave(api_key_arg, candidates, *, operation, payload=None, runtime_token=None):
        return {"items": [
            {"id": "m1", "status": "active", "bucket": "关系", "threads": ["t1", "t2"]},
        ]}

    _stub_enclave(monkeypatch, fake_enclave)
    fb, ab = _both("GET", "/v1/memory/buckets", api_key=api_key)
    assert fb == ab == (200, {"buckets": ["关系"]})
    ft, at = _both("GET", "/v1/memory/threads", api_key=api_key)
    assert ft == at == (200, {"threads": ["t1", "t2"]})


# --------------------------------------------------------------------------- #
# add / retype (v1 envelope; deterministic via patched store)
# --------------------------------------------------------------------------- #

def _patch_empty_store(monkeypatch):
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [])
    monkeypatch.setattr(memory_service, "_save_moments", lambda _store, _moments: None)
    monkeypatch.setattr(memory_core.boot_gates, "_log_bootstrap_event", lambda *_a, **_k: None)


def _blank(body, *fields):
    if not isinstance(body, dict):
        return body
    out = dict(body)
    moment = out.get("moment")
    if isinstance(moment, dict):
        moment = dict(moment)
        for f in fields:
            if f in moment:
                moment[f] = "<ts>"
        out["moment"] = moment
    return out


def test_add_missing_envelope_400_parity(user):
    _uid, api_key = user
    f, a = _both("POST", "/v1/memory/add", api_key=api_key, json_body={})
    assert f == a == (400, {"error": "envelope required (v1 encryption is mandatory)"})


def test_add_wrong_owner_403_parity(user, monkeypatch):
    _uid, api_key = user
    _patch_empty_store(monkeypatch)
    env = _envelope("someone-else")
    f, a = _both("POST", "/v1/memory/add", api_key=api_key, json_body={"envelope": env})
    assert f == a == (403, {"error": "envelope.owner_user_id does not match caller"})


def test_add_success_201_parity(user, monkeypatch):
    uid, api_key = user
    _patch_empty_store(monkeypatch)
    env = _envelope(uid)
    f = _flask("POST", "/v1/memory/add", headers=_headers(api_key), json_body={"envelope": env})
    a = _asgi("POST", "/v1/memory/add", headers=_headers(api_key), json_body={"envelope": env})
    assert f[0] == a[0] == 201
    # created_at is stamped with datetime.now() at add time — blank before compare.
    assert _blank(f[1], "created_at") == _blank(a[1], "created_at")
    assert f[1]["moment"]["id"] == "mom_test"
    assert f[1]["v"] == 1


def test_retype_type_invalid_400_parity(user, monkeypatch):
    _uid, api_key = user
    _patch_empty_store(monkeypatch)
    f, a = _both("POST", "/v1/memory/retype", api_key=api_key,
                 json_body={"id": "m1", "type": "bogus"})
    assert f == a
    assert f[0] == 400
    assert f[1]["error"] == "type_invalid"


def test_retype_not_found_404_parity(user, monkeypatch):
    _uid, api_key = user
    _patch_empty_store(monkeypatch)
    f, a = _both("POST", "/v1/memory/retype", api_key=api_key,
                 json_body={"id": "ghost", "type": "fact"})
    assert f == a == (404, {"error": "not_found"})


# --------------------------------------------------------------------------- #
# actions (scope no-op under api-key auth)
# --------------------------------------------------------------------------- #

def test_actions_required_400_parity(user):
    _uid, api_key = user
    f, a = _both("POST", "/v1/memory/actions", api_key=api_key, json_body={})
    assert f == a == (400, {"error": "actions required"})


def test_actions_unsupported_type_parity(user):
    _uid, api_key = user
    body = {"actions": [{"type": "memory.bogus"}]}
    f, a = _both("POST", "/v1/memory/actions", api_key=api_key, json_body=body)
    assert f == a
    assert f[0] == 400
    assert f[1]["results"][0]["error"] == "unsupported_memory_action"


# --------------------------------------------------------------------------- #
# migration_state (db patched for determinism/isolation)
# --------------------------------------------------------------------------- #

def test_migration_state_get_parity(user, monkeypatch):
    _uid, api_key = user
    monkeypatch.setattr(db, "get_blob", lambda *_a, **_k: None)
    f, a = _both("GET", "/v1/memory/migration_state", api_key=api_key)
    assert f == a
    assert f[0] == 200 and "state" in f[1]


def test_migration_state_post_parity(user, monkeypatch):
    _uid, api_key = user
    monkeypatch.setattr(db, "get_blob", lambda *_a, **_k: None)
    monkeypatch.setattr(db, "set_blob", lambda *_a, **_k: None)
    body = {"migrated": 0, "legacy_remaining": 0}
    f, a = _both("POST", "/v1/memory/migration_state", api_key=api_key, json_body=body)

    def _blank_state(resp):
        status, b = resp
        state = {**(b or {}).get("state", {}), "updated_at": "<ts>"}
        return status, {**(b or {}), "state": state}

    # ``updated_at`` in the advanced state is stamped with time.time() per call.
    assert _blank_state(f) == _blank_state(a)
    assert f[0] == 200 and "state" in f[1]


def test_migration_state_post_bad_ints_400_parity(user, monkeypatch):
    _uid, api_key = user
    monkeypatch.setattr(db, "get_blob", lambda *_a, **_k: None)
    monkeypatch.setattr(db, "set_blob", lambda *_a, **_k: None)
    body = {"migrated": "not-int"}
    f, a = _both("POST", "/v1/memory/migration_state", api_key=api_key, json_body=body)
    assert f == a == (400, {"error": "migrated/legacy_remaining must be ints"})


# --------------------------------------------------------------------------- #
# legacy_batch (enclave decrypt stubbed; forwards api key)
# --------------------------------------------------------------------------- #

def test_legacy_batch_parity(user, monkeypatch):
    uid, api_key = user
    monkeypatch.setattr(db, "get_blob", lambda *_a, **_k: None)
    legacy = {
        "id": "m1", "body_ct": "ct1", "nonce": "n", "K_user": "k", "K_enclave": "ke",
        "visibility": "shared", "owner_user_id": uid, "status": "active",
        "occurred_at": "2020-01-01",
    }
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [dict(legacy)])
    monkeypatch.setattr(
        memory_actions_mod, "_memory_plain_from_envelope",
        lambda moment, key, runtime_token="": ({"title": "t", "description": "d"}, ""),
    )
    body = {"batch_size": 8}
    f, a = _both("POST", "/v1/memory/legacy_batch", api_key=api_key, json_body=body)
    assert f == a
    assert f[0] == 200
    assert [r["id"] for r in f[1]["batch"]] == ["m1"]
    assert f[1]["legacy_remaining"] == 1
