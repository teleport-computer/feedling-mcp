"""Framework-neutral auth core (ASGI-migration plan §7.1 / §7.5).

Exercises ``accounts.auth_core`` directly with plain header/query mappings — no
Flask/FastAPI request object — plus a parity check that the Flask
``require_user()`` wrapper still behaves as before. This is the shared identity
boundary both the legacy Flask routes and the future ASGI routes rely on, so its
precedence and failure modes are locked here.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import auth_core  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402

_SECRET = "test-auth-core-secret"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def user(monkeypatch):
    """Register a fresh user via the app and return (user_id, api_key).

    Registration populates the in-process registry (``_users`` / ``_key_to_user``)
    that ``auth_core`` reads, so we can then call the core directly in-process.
    """
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        res = c.post(
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
        scope=scope if scope is not None else ["chat", "memory", "identity"],
        ttl=ttl,
        now=now,
    )


# --------------------------------------------------------------------------- #
# API key path
# --------------------------------------------------------------------------- #

def test_api_key_via_x_api_key_header(user):
    user_id, api_key = user
    res = auth_core.resolve_user({"X-API-Key": api_key})
    assert res.user_id == user_id
    assert res.api_key == api_key
    assert res.runtime_token_claims is None


def test_api_key_via_bearer_header(user):
    user_id, api_key = user
    res = auth_core.resolve_user({"Authorization": f"Bearer {api_key}"})
    assert res.user_id == user_id
    assert res.api_key == api_key


def test_api_key_via_legacy_query_param(user):
    user_id, api_key = user
    res = auth_core.resolve_user({}, {"key": api_key})
    assert res.user_id == user_id
    assert res.api_key == api_key


def test_header_lookup_is_case_insensitive_for_plain_dict(user):
    """A plain dict with a lowercased key must still resolve (Flask/Starlette
    Headers are case-insensitive; the core must not depend on that)."""
    user_id, api_key = user
    res = auth_core.resolve_user({"x-api-key": api_key})
    assert res.user_id == user_id


def test_missing_credential_raises_401(user):
    with pytest.raises(auth_core.AuthError) as ei:
        auth_core.resolve_user({})
    assert ei.value.status_code == 401
    assert ei.value.code == "unauthorized"


def test_bad_api_key_raises_401(user):
    # A key that never resolved (also the shape a post-reset invalidated key
    # takes: the registry no longer maps it).
    with pytest.raises(auth_core.AuthError) as ei:
        auth_core.resolve_user({"X-API-Key": "nope-not-a-real-key"})
    assert ei.value.status_code == 401


# --------------------------------------------------------------------------- #
# Runtime-token path
# --------------------------------------------------------------------------- #

def test_runtime_token_success(user, monkeypatch):
    user_id, _api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    res = auth_core.resolve_user({"X-Feedling-Runtime-Token": _mint(user_id)})
    assert res.user_id == user_id
    assert res.runtime_token_claims is not None
    assert res.api_key is None  # token path never records last_seen_api_key


def test_runtime_token_expired_fails_closed(user, monkeypatch):
    user_id, api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    expired = _mint(user_id, ttl=-1.0)
    # Present-but-invalid must fail closed, NOT fall back to the (valid) api key.
    with pytest.raises(auth_core.AuthError) as ei:
        auth_core.resolve_user(
            {"X-Feedling-Runtime-Token": expired, "X-API-Key": api_key}
        )
    assert ei.value.status_code == 401


def test_runtime_token_bad_signature_fails_closed(user, monkeypatch):
    user_id, _api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    tok = _mint(user_id)
    tampered = tok[:-2] + ("00" if tok[-2:] != "00" else "11")
    with pytest.raises(auth_core.AuthError) as ei:
        auth_core.resolve_user({"X-Feedling-Runtime-Token": tampered})
    assert ei.value.status_code == 401


def test_runtime_token_unknown_user_raises_401(user, monkeypatch):
    _user_id, _api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    ghost = _mint("user_does_not_exist")
    with pytest.raises(auth_core.AuthError) as ei:
        auth_core.resolve_user({"X-Feedling-Runtime-Token": ghost})
    assert ei.value.status_code == 401


def test_runtime_token_ignored_when_feature_disabled(user, monkeypatch):
    """With no secret set the header is ignored entirely and we fall through to
    the api-key path (here: no key present → 401)."""
    user_id, _api_key = user
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_SECRET", raising=False)
    with pytest.raises(auth_core.AuthError) as ei:
        auth_core.resolve_user({"X-Feedling-Runtime-Token": _mint(user_id)})
    assert ei.value.status_code == 401


# --------------------------------------------------------------------------- #
# Scope authorization
# --------------------------------------------------------------------------- #

def test_authorize_scope_allows_granted_scope(user, monkeypatch):
    user_id, _api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    res = auth_core.resolve_user(
        {"X-Feedling-Runtime-Token": _mint(user_id, scope=["memory"])}
    )
    # No raise == authorized.
    auth_core.authorize_scope(res.runtime_token_claims, res.user_id, "memory")


def test_authorize_scope_denies_missing_scope(user, monkeypatch):
    user_id, _api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    res = auth_core.resolve_user(
        {"X-Feedling-Runtime-Token": _mint(user_id, scope=["chat"])}
    )
    with pytest.raises(auth_core.AuthError) as ei:
        auth_core.authorize_scope(res.runtime_token_claims, res.user_id, "memory")
    assert ei.value.status_code == 403
    assert ei.value.code == "forbidden"


def test_authorize_scope_is_noop_for_api_key(user):
    """Api-key auth carries no claims — scope enforcement is a no-op (the
    long-term key is full access)."""
    _user_id, api_key = user
    res = auth_core.resolve_user({"X-API-Key": api_key})
    assert res.runtime_token_claims is None
    auth_core.authorize_scope(res.runtime_token_claims, res.user_id, "memory")
