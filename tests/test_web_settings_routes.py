"""ASGI route surface for the web-search toggle.

The point of this file is the auth boundary. `enabled` is the user's own
preference, so it must be reachable only with the long-term api key: a hosted
consumer authenticates with a short-lived runtime token and has no business
reading or rewriting a user's settings. "The model cannot currently obtain a
token" is not a security boundary — the route has to refuse.

Pattern (client fixture, `_register`, runtime-token minting) mirrors
`tests/test_user_mcp_routes.py`, the other api-key-only control-plane surface.
"""

from __future__ import annotations

import base64
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402

_RUNTIME_SECRET = "test-runtime-secret"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _mint(user_id: str, *, scope) -> str:
    return runtime_token.mint(
        _RUNTIME_SECRET.encode("utf-8"),
        user_id=user_id,
        runtime_instance_id="ri_test",
        scope=scope,
        ttl=900.0,
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(raw), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _auth(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


# ------------------------------------------------------------------ auth gate

def test_get_requires_auth(client):
    assert client.get("/v1/web/settings").status_code == 401


def test_post_requires_auth(client):
    assert client.post("/v1/web/settings", json={"enabled": True}).status_code == 401


def test_post_rejects_a_runtime_token(client, monkeypatch):
    """The core of this file: a worker/resident holding a runtime token must not
    be able to switch a user's web access back on."""
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _RUNTIME_SECRET)
    user_id, _ = _register(client)
    tok = _mint(user_id, scope=["chat", "memory"])
    r = client.post(
        "/v1/web/settings",
        headers={"X-Feedling-Runtime-Token": tok},
        json={"enabled": True},
    )
    assert r.status_code == 403, r.get_data(as_text=True)


def test_get_rejects_a_runtime_token(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _RUNTIME_SECRET)
    user_id, _ = _register(client)
    tok = _mint(user_id, scope=["chat", "memory"])
    r = client.get("/v1/web/settings", headers={"X-Feedling-Runtime-Token": tok})
    assert r.status_code == 403, r.get_data(as_text=True)


def test_a_refused_runtime_token_leaves_the_preference_untouched(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _RUNTIME_SECRET)
    user_id, key = _register(client)
    tok = _mint(user_id, scope=["chat"])
    client.post(
        "/v1/web/settings",
        headers={"X-Feedling-Runtime-Token": tok},
        json={"enabled": True},
    )
    assert client.get("/v1/web/settings", headers=_auth(key)).get_json()["enabled"] is False


# --------------------------------------------------------------- happy path

def test_defaults_to_disabled(client):
    _, key = _register(client)
    body = client.get("/v1/web/settings", headers=_auth(key)).get_json()
    assert body["enabled"] is False
    assert body["effective_for_chat"] is False
    assert set(body) == {
        "enabled", "runtime_supported", "status", "effective_for_chat", "tools",
    }
    assert body["status"] in {"available", "degraded", "unavailable"}


def test_roundtrip_with_api_key(client):
    _, key = _register(client)
    posted = client.post(
        "/v1/web/settings", headers=_auth(key), json={"enabled": True}
    )
    assert posted.status_code == 200, posted.get_data(as_text=True)
    assert posted.get_json()["enabled"] is True
    assert client.get("/v1/web/settings", headers=_auth(key)).get_json()["enabled"] is True


def test_can_be_turned_back_off(client):
    _, key = _register(client)
    client.post("/v1/web/settings", headers=_auth(key), json={"enabled": True})
    client.post("/v1/web/settings", headers=_auth(key), json={"enabled": False})
    assert client.get("/v1/web/settings", headers=_auth(key)).get_json()["enabled"] is False


# ------------------------------------------------------------ input validation

@pytest.mark.parametrize("bad", ["no", "true", "yes", 1, 0, None, []])
def test_non_bool_is_a_400_not_a_coercion(client, bad):
    _, key = _register(client)
    r = client.post("/v1/web/settings", headers=_auth(key), json={"enabled": bad})
    assert r.status_code == 400, r.get_data(as_text=True)


def test_missing_enabled_is_a_400(client):
    _, key = _register(client)
    assert client.post("/v1/web/settings", headers=_auth(key), json={}).status_code == 400


def test_a_rejected_body_does_not_change_the_stored_preference(client):
    _, key = _register(client)
    client.post("/v1/web/settings", headers=_auth(key), json={"enabled": True})
    client.post("/v1/web/settings", headers=_auth(key), json={"enabled": "no"})
    assert client.get("/v1/web/settings", headers=_auth(key)).get_json()["enabled"] is True
