"""Native POST /v1/bootstrap parity + auth (plan §9.4).

Asserts the FastAPI route returns the same status+body as the Flask oracle for
the first-time onboarding gate, the already-bootstrapped short-circuit, and the
archive_language surfacing; and that bad/missing auth is a fixed-body 401.

Because POST /v1/bootstrap has a persistent side effect (it flips the user's
``bootstrap`` blob to bootstrapped), the Flask-oracle-vs-ASGI comparison uses
two independently-registered users so the first-time body is observed once on
each backend rather than one backend seeing ``already_bootstrapped``.
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
from core import store as core_store  # noqa: E402

_FAKE_ENCLAVE = {"content_pk_hex": ("33" * 32), "compose_hash": "test-compose"}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(core_enclave, "_get_enclave_info", lambda: dict(_FAKE_ENCLAVE))
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    yield


def _register(archive_language: str | None = None, pk_byte: bytes = b"\x11") -> tuple[str, str]:
    payload = {"public_key": _b64(pk_byte * 32)}
    if archive_language is not None:
        payload["archive_language"] = archive_language
    res = make_client().post("/v1/users/register", json=payload)
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _asgi_post(path: str, headers: dict | None = None):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(path, headers=headers or {})
            return resp.status_code, resp.json()

    return asyncio.run(go())


def _flask_post(path: str, headers: dict | None = None):
    res = make_client().post(path, headers=headers or {})
    return res.status_code, res.get_json()


# --------------------------------------------------------------------------- #
# first-time parity (two distinct users so each backend sees first_time once)
# --------------------------------------------------------------------------- #

def test_bootstrap_first_time_parity():
    _uid_f, key_f = _register(pk_byte=b"\x11")
    _uid_a, key_a = _register(pk_byte=b"\x22")
    f_status, f_body = _flask_post("/v1/bootstrap", {"X-API-Key": key_f})
    a_status, a_body = _asgi_post("/v1/bootstrap", {"X-API-Key": key_a})
    assert f_status == a_status == 200
    assert f_body["status"] == a_body["status"] == "first_time"
    # Instructions are static (no per-user substitution) → byte-identical.
    assert a_body["instructions"] == f_body["instructions"]
    assert a_body == f_body


def test_bootstrap_archive_language_parity():
    _uid_f, key_f = _register(archive_language="zh-Hant-TW", pk_byte=b"\x33")
    _uid_a, key_a = _register(archive_language="zh-Hant-TW", pk_byte=b"\x44")
    f_status, f_body = _flask_post("/v1/bootstrap", {"X-API-Key": key_f})
    a_status, a_body = _asgi_post("/v1/bootstrap", {"X-API-Key": key_a})
    assert f_status == a_status == 200
    assert a_body.get("archive_language") == "zh-Hant-TW"
    assert a_body == f_body


# --------------------------------------------------------------------------- #
# already-bootstrapped short-circuit
# --------------------------------------------------------------------------- #

def test_bootstrap_already_bootstrapped():
    _uid, api_key = _register(pk_byte=b"\x55")
    # First POST flips the blob (first_time); second must short-circuit.
    first_status, first_body = _asgi_post("/v1/bootstrap", {"X-API-Key": api_key})
    assert first_status == 200 and first_body["status"] == "first_time"
    second_status, second_body = _asgi_post("/v1/bootstrap", {"X-API-Key": api_key})
    assert second_status == 200
    assert second_body == {"status": "already_bootstrapped"}
    # Flask oracle agrees on the same (now-bootstrapped) user.
    f_status, f_body = _flask_post("/v1/bootstrap", {"X-API-Key": api_key})
    assert f_status == 200
    assert f_body == {"status": "already_bootstrapped"}


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #

def test_bootstrap_bad_auth_is_fixed_401():
    _register(pk_byte=b"\x66")
    status, body = _asgi_post("/v1/bootstrap", {"X-API-Key": "nope"})
    assert status == 401
    assert body == {"error": "unauthorized"}


def test_bootstrap_no_auth_is_401():
    _register(pk_byte=b"\x77")
    status, body = _asgi_post("/v1/bootstrap")
    assert status == 401
    assert body == {"error": "unauthorized"}
