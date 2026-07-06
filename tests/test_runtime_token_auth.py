"""Stage D (slice 1): backend accepts a runtime token in place of the API key.

A hosted consumer should authenticate with a short-lived, user-scoped runtime
token (minted by the trusted supervisor) instead of the user's long-term Feedling
API key. This covers the backend ``require_user`` path: a valid token in
``X-Feedling-Runtime-Token`` authenticates as its user; an invalid/expired one
fails closed; and the whole feature is OFF unless ``FEEDLING_RUNTIME_TOKEN_SECRET``
is set (no behavior change for existing API-key callers).

NOTE (follow-on): routes that forward the user's API key to the enclave (content
decrypt) still need the enclave side to accept runtime tokens — out of scope here.
We test ``/v1/model_api/get``, which only needs user identity.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402

_SECRET = "test-runtime-secret"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    monkeypatch.setattr(
        core_enclave,
        "_get_enclave_info",
        lambda: {"content_pk_hex": ("22" * 32), "compose_hash": "test"},
    )
    with make_client() as c:
        yield c


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _mint(user_id: str, *, ttl: float = 900.0, now: float | None = None, scope=None) -> str:
    return runtime_token.mint(
        _SECRET.encode("utf-8"),
        user_id=user_id,
        runtime_instance_id="ri_test",
        scope=scope or ["chat", "memory", "identity"],
        ttl=ttl,
        now=now,
    )


def test_valid_runtime_token_authenticates_as_its_user(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    user_id, _api_key = _register(client)
    tok = _mint(user_id)
    res = client.get("/v1/model_api/get", headers={"X-Feedling-Runtime-Token": tok})
    assert res.status_code == 200, res.get_data(as_text=True)


def test_expired_runtime_token_is_rejected(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    user_id, _ = _register(client)
    tok = _mint(user_id, ttl=10.0, now=1000.0)  # exp=1010, long past now
    res = client.get("/v1/model_api/get", headers={"X-Feedling-Runtime-Token": tok})
    assert res.status_code == 401


def test_runtime_token_signed_with_wrong_secret_is_rejected(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    user_id, _ = _register(client)
    bad = runtime_token.mint(b"some-other-secret", user_id=user_id,
                             runtime_instance_id="ri", scope=["chat"])
    res = client.get("/v1/model_api/get", headers={"X-Feedling-Runtime-Token": bad})
    assert res.status_code == 401


def test_present_but_invalid_token_does_not_fall_back_to_api_key(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    _user_id, api_key = _register(client)
    # An invalid runtime token fails closed even if a valid API key is also sent.
    res = client.get(
        "/v1/model_api/get",
        headers={"X-Feedling-Runtime-Token": "garbage.sig", "X-API-Key": api_key},
    )
    assert res.status_code == 401


def test_feature_disabled_ignores_runtime_token_header(client, monkeypatch):
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_SECRET", raising=False)
    user_id, api_key = _register(client)
    tok = _mint(user_id)
    # Secret unset → token header ignored; a valid API key still authenticates.
    res = client.get(
        "/v1/model_api/get",
        headers={"X-Feedling-Runtime-Token": tok, "X-API-Key": api_key},
    )
    assert res.status_code == 200
    # …and with no API key, the (ignored) token does not authenticate.
    res2 = client.get("/v1/model_api/get", headers={"X-Feedling-Runtime-Token": tok})
    assert res2.status_code == 401


def test_token_for_unknown_user_is_rejected(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    _register(client)
    tok = _mint("usr_does_not_exist")
    res = client.get("/v1/model_api/get", headers={"X-Feedling-Runtime-Token": tok})
    assert res.status_code == 401


# ---- slice 4: per-route scope enforcement ----


def test_token_without_memory_scope_is_forbidden_on_memory_actions(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    user_id, _ = _register(client)
    tok = _mint(user_id, scope=["chat"])  # no "memory" scope
    res = client.post("/v1/memory/actions", json={}, headers={"X-Feedling-Runtime-Token": tok})
    assert res.status_code == 403  # scope gate fires before any execution


def test_token_with_memory_scope_passes_scope_gate(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    user_id, _ = _register(client)
    tok = _mint(user_id, scope=["chat", "memory"])
    res = client.post("/v1/memory/actions", json={}, headers={"X-Feedling-Runtime-Token": tok})
    # scope OK → past the gate; empty body fails validation (400), NOT 403
    assert res.status_code != 403
    assert res.status_code == 400


def test_token_without_identity_scope_is_forbidden_on_identity_actions(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    user_id, _ = _register(client)
    tok = _mint(user_id, scope=["chat", "memory"])  # no "identity"
    res = client.post("/v1/identity/actions", json={}, headers={"X-Feedling-Runtime-Token": tok})
    assert res.status_code == 403


def test_api_key_auth_bypasses_scope_gate(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    _user_id, api_key = _register(client)
    # api-key auth = full access; the scope gate is a no-op (not 403).
    res = client.post("/v1/memory/actions", json={}, headers={"X-API-Key": api_key})
    assert res.status_code != 403
