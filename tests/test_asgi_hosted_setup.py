"""Native hosted-setup parity (ASGI-migration plan §5.3 / §9).

Asserts the FastAPI routes (``hosted.setup_routes_asgi``) return the expected
status/body for all 10 routes:
model_api get/setup/driver/key_envelope/test/delete/runtime + memory/repair,
state/receipts, memory/capture_jobs. The routes call the framework-neutral
``hosted.setup_core``, so provider/enclave stubs are installed once on the shared
module objects — keeping the test fully offline and the E2E envelope handling
untouched (the server never decrypts).

These routes are gated on ``auth.require_user()`` only (no
``runtime_auth.authorize_scope``), so there is no scope-failure (403) case — the
ASGI router deliberately carries no ``require_scope``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from hosted import setup_routes_asgi as setup_asgi  # noqa: E402
from hosted import turn as hosted_turn  # noqa: E402
import provider_client  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    setup_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


@pytest.fixture()
def make_user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()

    def _make():
        res = make_client().post(
            "/v1/users/register",
            json={"public_key": _b64(os.urandom(32)), "archive_language": "en"},
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        return body["user_id"], body["api_key"]

    return _make


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


# --------------------------------------------------------------------------- #
# request helpers (Flask oracle + ASGI under test)
# --------------------------------------------------------------------------- #

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


_ENDPOINTS = [
    ("POST", "/v1/model_api/setup"),
    ("GET", "/v1/model_api/get"),
    ("POST", "/v1/model_api/driver"),
    ("GET", "/v1/model_api/key_envelope"),
    ("POST", "/v1/model_api/test"),
    ("DELETE", "/v1/model_api/delete"),
    ("GET", "/v1/model_api/runtime"),
    ("POST", "/v1/model_api/memory/repair"),
    ("GET", "/v1/state/receipts"),
    ("GET", "/v1/memory/capture_jobs"),
]


# --------------------------------------------------------------------------- #
# auth parity (401) — every route, no scope gate anywhere
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path", _ENDPOINTS)
def test_no_auth_is_401_parity(make_user, method, path):
    make_user()  # seed the registry (fresh FEEDLING_DIR)
    json_body = {} if method == "POST" else None
    f = _flask(method, path, json_body=json_body)
    a = _asgi(method, path, json_body=json_body)
    assert f == a == (401, {"error": "unauthorized"})


# --------------------------------------------------------------------------- #
# read routes on a fresh (unconfigured) user
# --------------------------------------------------------------------------- #

def test_get_no_config_parity(make_user):
    _uid, api_key = make_user()
    f = _flask("GET", "/v1/model_api/get", headers=_headers(api_key))
    a = _asgi("GET", "/v1/model_api/get", headers=_headers(api_key))
    assert f == a == (200, {"config": {"configured": False}})


def test_key_envelope_missing_parity(make_user):
    _uid, api_key = make_user()
    f = _flask("GET", "/v1/model_api/key_envelope", headers=_headers(api_key))
    a = _asgi("GET", "/v1/model_api/key_envelope", headers=_headers(api_key))
    assert f == a == (404, {"error": "model_api_key_envelope_missing"})


def test_driver_not_configured_parity(make_user):
    _uid, api_key = make_user()
    f = _flask("POST", "/v1/model_api/driver", headers=_headers(api_key), json_body={})
    a = _asgi("POST", "/v1/model_api/driver", headers=_headers(api_key), json_body={})
    assert f == a == (404, {"error": "model_api_not_configured"})


def test_test_not_configured_parity(make_user):
    _uid, api_key = make_user()
    f = _flask("POST", "/v1/model_api/test", headers=_headers(api_key), json_body={})
    a = _asgi("POST", "/v1/model_api/test", headers=_headers(api_key), json_body={})
    assert f == a == (404, {"error": "model_api_not_configured"})


def test_runtime_no_config_parity(make_user):
    _uid, api_key = make_user()
    f = _flask("GET", "/v1/model_api/runtime", headers=_headers(api_key))
    a = _asgi("GET", "/v1/model_api/runtime", headers=_headers(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1]["configured"] is False
    assert f[1]["recap_status"] == "idle"


def test_delete_no_config_parity(make_user):
    _uid, api_key = make_user()
    # Idempotent on a user with no model_api blob; same user is safe.
    f = _flask("DELETE", "/v1/model_api/delete", headers=_headers(api_key))
    a = _asgi("DELETE", "/v1/model_api/delete", headers=_headers(api_key))
    assert f == a == (200, {"deleted": False})


def test_state_receipts_empty_parity(make_user):
    _uid, api_key = make_user()
    f = _flask("GET", "/v1/state/receipts", headers=_headers(api_key))
    a = _asgi("GET", "/v1/state/receipts", headers=_headers(api_key))
    assert f == a == (200, {"receipts": [], "pending": []})


def test_capture_jobs_empty_parity(make_user):
    _uid, api_key = make_user()
    f = _flask("GET", "/v1/memory/capture_jobs", headers=_headers(api_key))
    a = _asgi("GET", "/v1/memory/capture_jobs", headers=_headers(api_key))
    assert f == a == (200, {"jobs": [], "active_recap": False})


# --------------------------------------------------------------------------- #
# validation parity (400)
# --------------------------------------------------------------------------- #

def test_setup_bad_provider_400_parity(make_user):
    _uid, api_key = make_user()
    body = {"provider": "bogus", "model": "x", "api_key": "sk-x"}
    f = _flask("POST", "/v1/model_api/setup", headers=_headers(api_key), json_body=body)
    a = _asgi("POST", "/v1/model_api/setup", headers=_headers(api_key), json_body=body)
    assert f == a
    assert f[0] == 400
    assert "provider must be" in f[1]["error"]


def test_setup_missing_key_no_existing_400_parity(make_user):
    _uid, api_key = make_user()
    body = {"provider": "openai", "model": "gpt-4.1-mini"}  # no api_key, no saved envelope
    f = _flask("POST", "/v1/model_api/setup", headers=_headers(api_key), json_body=body)
    a = _asgi("POST", "/v1/model_api/setup", headers=_headers(api_key), json_body=body)
    assert f == a == (400, {"error": "api_key required"})


def test_memory_repair_bad_mode_400_parity(make_user):
    _uid, api_key = make_user()
    body = {"mode": "nuke"}
    f = _flask("POST", "/v1/model_api/memory/repair", headers=_headers(api_key), json_body=body)
    a = _asgi("POST", "/v1/model_api/memory/repair", headers=_headers(api_key), json_body=body)
    assert f == a == (400, {"error": "mode must be dry_run or apply"})


@pytest.mark.parametrize("path", ["/v1/state/receipts", "/v1/memory/capture_jobs"])
def test_bad_limit_400_parity(make_user, path):
    _uid, api_key = make_user()
    f = _flask("GET", f"{path}?limit=abc", headers=_headers(api_key))
    a = _asgi("GET", f"{path}?limit=abc", headers=_headers(api_key))
    assert f == a == (400, {"error": "invalid limit"})


# --------------------------------------------------------------------------- #
# setup happy path (provider test + envelope stubbed) — E2E: key sealed to
# envelope, never returned in the clear; both frameworks share the stubs.
# --------------------------------------------------------------------------- #

def _fake_envelope_builder(store, plaintext: bytes, *, item_id: str | None = None):
    return {
        "v": 1,
        "id": item_id or "env_1",
        "body_ct": "ct_1",
        "nonce": "nonce_1",
        "K_user": "k_user_1",
        "K_enclave": "k_enclave_1",
        "visibility": "shared",
        "owner_user_id": store.user_id,
        "enclave_pk_fpr": "test",
    }, ""


def _blank_config_ts(resp):
    status, body = resp
    body = dict(body or {})
    cfg = dict(body.get("config") or {})
    for k in ("last_test_at", "created_at", "updated_at"):
        if k in cfg:
            cfg[k] = "<ts>"
    if "config" in body:
        body["config"] = cfg
    return status, body


def test_setup_happy_path_parity_sealed_envelope(make_user, monkeypatch):
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}})
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder)

    body = {"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-secret-123"}

    # Two independent users so neither run sees the other's persisted config.
    _uid_f, key_f = make_user()
    _uid_a, key_a = make_user()

    f = _flask("POST", "/v1/model_api/setup", headers=_headers(key_f), json_body=body)
    a = _asgi("POST", "/v1/model_api/setup", headers=_headers(key_a), json_body=body)

    assert f[0] == a[0] == 200
    fb, ab = _blank_config_ts(f), _blank_config_ts(a)
    assert fb == ab
    cfg = fb[1]["config"]
    assert cfg["configured"] is True
    assert cfg["provider"] == "openai"
    assert cfg["model"] == "gpt-4.1-mini"
    # E2E: the raw provider key is never echoed back.
    assert "sk-secret-123" not in json.dumps(fb[1])
    assert "api_key_envelope" not in cfg

    # And GET reflects the stored config identically on both users.
    gf = _flask("GET", "/v1/model_api/get", headers=_headers(key_f))
    ga = _asgi("GET", "/v1/model_api/get", headers=_headers(key_a))
    assert _blank_config_ts(gf) == _blank_config_ts(ga)
    assert gf[1]["config"]["configured"] is True


def test_key_envelope_present_after_setup_parity(make_user, monkeypatch):
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: {"reply": "ok", "usage": {}})
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder)
    body = {"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-secret-123"}

    _uid_f, key_f = make_user()
    _uid_a, key_a = make_user()
    assert _flask("POST", "/v1/model_api/setup", headers=_headers(key_f), json_body=body)[0] == 200
    assert _asgi("POST", "/v1/model_api/setup", headers=_headers(key_a), json_body=body)[0] == 200

    f = _flask("GET", "/v1/model_api/key_envelope", headers=_headers(key_f))
    a = _asgi("GET", "/v1/model_api/key_envelope", headers=_headers(key_a))

    # Blank the per-user/per-item fields (envelope id carries a random uuid; owner
    # differs by user) — the rest of the ciphertext envelope must be identical.
    def _blank_env(resp):
        status, body = resp
        env = dict((body or {}).get("api_key_envelope") or {})
        env["id"] = "<id>"
        env["owner_user_id"] = "<owner>"
        return status, {"api_key_envelope": env}

    assert _blank_env(f) == _blank_env(a)
    assert f[0] == a[0] == 200
    # The route returns the OWN api_key_envelope ciphertext verbatim.
    assert f[1]["api_key_envelope"]["id"].startswith("model_api_key_")
    assert f[1]["api_key_envelope"]["body_ct"] == "ct_1"


# --------------------------------------------------------------------------- #
# memory/repair dry_run (quality scan stubbed) — deterministic body parity.
# --------------------------------------------------------------------------- #

def test_memory_repair_dry_run_parity(make_user, monkeypatch):
    scan = {
        "warning": "some_noise",
        "scanned": 12,
        "issue_count": 3,
        "noisy_count": 5,
        "duplicate_count": 1,
        "noisy_ids": ["a", "b", "c"],
        "issues": [{"id": "a", "reason": "raw"}],
    }
    monkeypatch.setattr(hosted_turn, "_model_api_memory_quality_scan",
                        lambda store, **kw: dict(scan))

    body = {"mode": "dry_run"}
    _uid_f, key_f = make_user()
    _uid_a, key_a = make_user()
    f = _flask("POST", "/v1/model_api/memory/repair", headers=_headers(key_f), json_body=body)
    a = _asgi("POST", "/v1/model_api/memory/repair", headers=_headers(key_a), json_body=body)
    assert f == a
    assert f[0] == 200
    assert f[1]["status"] == "completed"
    assert f[1]["mode"] == "dry_run"
    assert f[1]["preview"]["old_cards_detected"] == 5
    assert f[1]["preview"]["new_cards_planned"] == 6
    assert f[1]["memory_quality"] == scan
