"""Native whoami / bootstrap-status parity + auth dependency (plan §9.4 / §8.2).

Asserts the FastAPI routes return the same body as the Flask oracle for both the
api-key and runtime-token auth paths, that bad auth is a fixed-body 401, and
that the auth dependency wires ``current_user_id`` through to the access log
(the ASGI equivalent of ``g.user_id``, plan §5.9).
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402


def _normalize(body: dict) -> dict:
    """Blank the volatile per-binding check-in timestamps before comparing.

    ``access_modes[*].updated_at`` / ``last_seen_at`` reflect *when the resident
    last checked in*, so two sequential whoami calls legitimately differ by a few
    ms. Everything else must match exactly between the Flask oracle and the ASGI
    route (both call the same whoami_core)."""
    out = dict(body)
    modes = []
    for m in out.get("access_modes", []) or []:
        m = dict(m)
        m["updated_at"] = "<ts>"
        m["last_seen_at"] = "<ts>"
        modes.append(m)
    if modes:
        out["access_modes"] = modes
    return out

_SECRET = "test-asgi-whoami-secret"
_FAKE_ENCLAVE = {"content_pk_hex": ("33" * 32), "compose_hash": "test-compose"}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    # Deterministic enclave material for both backends (whoami_core calls this).
    monkeypatch.setattr(core_enclave, "_get_enclave_info", lambda: dict(_FAKE_ENCLAVE))
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


def _asgi_get(path: str, headers: dict | None = None):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get(path, headers=headers or {})
            return resp.status_code, resp.json()

    return asyncio.run(go())


def _flask_get(path: str, headers: dict | None = None):
    res = make_client().get(path, headers=headers or {})
    return res.status_code, res.get_json()


def _mint(user_id: str, scope=None) -> str:
    return runtime_token.mint(
        _SECRET.encode("utf-8"),
        user_id=user_id,
        runtime_instance_id="ri_test",
        scope=scope if scope is not None else ["chat", "memory", "identity"],
        ttl=900.0,
    )


# --------------------------------------------------------------------------- #
# whoami parity
# --------------------------------------------------------------------------- #

def test_whoami_parity_api_key(user):
    _uid, api_key = user
    f_status, f_body = _flask_get("/v1/users/whoami", {"X-API-Key": api_key})
    a_status, a_body = _asgi_get("/v1/users/whoami", {"X-API-Key": api_key})
    assert f_status == a_status == 200
    assert _normalize(a_body) == _normalize(f_body)
    assert a_body["enclave_content_public_key_hex"] == _FAKE_ENCLAVE["content_pk_hex"]


def test_whoami_parity_runtime_token(user, monkeypatch):
    uid, _api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    tok = _mint(uid)
    f_status, f_body = _flask_get("/v1/users/whoami", {"X-Feedling-Runtime-Token": tok})
    a_status, a_body = _asgi_get("/v1/users/whoami", {"X-Feedling-Runtime-Token": tok})
    assert f_status == a_status == 200
    assert _normalize(a_body) == _normalize(f_body)
    assert a_body["user_id"] == uid


def test_whoami_bad_auth_is_fixed_401(user):
    status, body = _asgi_get("/v1/users/whoami", {"X-API-Key": "nope"})
    assert status == 401
    assert body == {"error": "unauthorized"}


def test_whoami_no_auth_is_401(user):
    status, body = _asgi_get("/v1/users/whoami")
    assert status == 401
    assert body == {"error": "unauthorized"}


# --------------------------------------------------------------------------- #
# bootstrap/status parity
# --------------------------------------------------------------------------- #

def test_bootstrap_status_parity(user):
    _uid, api_key = user
    f_status, f_body = _flask_get("/v1/bootstrap/status", {"X-API-Key": api_key})
    a_status, a_body = _asgi_get("/v1/bootstrap/status", {"X-API-Key": api_key})
    assert f_status == a_status == 200
    assert a_body == f_body


# --------------------------------------------------------------------------- #
# contextvar -> access log (plan §5.9)
# --------------------------------------------------------------------------- #

def test_access_log_carries_uid_from_auth(user, capsys):
    uid, api_key = user
    _asgi_get("/v1/users/whoami", {"X-API-Key": api_key})
    out = capsys.readouterr().out
    # The auth dependency set current_user_id; the access-log middleware must
    # see it (contextvar propagation, plan §5.9).
    assert f"uid={uid}" in out
    assert "GET /v1/users/whoami status=200" in out
