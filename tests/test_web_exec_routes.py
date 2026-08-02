"""Auth boundary for the V1 web EXECUTION endpoints (/v1/agent/web/{search,fetch}).

These endpoints are the cloud-only product surface. The hard rule they enforce
(``require_runtime_scope("web")``):

  * a HOSTED consumer holding a per-user runtime token WITH the ``web`` scope may
    reach them (200);
  * a runtime token WITHOUT the ``web`` scope is refused (403);
  * a long-term API KEY is refused outright (403) — this is the VPS-exclusion
    boundary. A self-hosted resident authenticates with an api key and must never
    reach our web tools; it uses its own model provider's built-in web ability.

Contrast with ``/v1/web/settings`` (test_web_settings_routes.py), which is the
inverse gate: api-key ONLY (iOS control plane), runtime tokens refused.

Fixture / minting pattern mirrors test_web_settings_routes.py.
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
from web import execution_core  # noqa: E402

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


class _StubResult:
    """Minimal CapabilityResult stand-in — the route only calls ``to_dict``."""

    def to_dict(self):
        return {"ok": True, "stub": True}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _RUNTIME_SECRET)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    # Never touch the real web engine — auth is what these tests exercise; the
    # handler body is stubbed so a token that passes the gate returns cleanly
    # without any network I/O.
    monkeypatch.setattr(execution_core, "run_search", lambda store, params: _StubResult())
    monkeypatch.setattr(execution_core, "run_fetch", lambda store, params: _StubResult())
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


_SEARCH = "/v1/agent/web/search"
_FETCH = "/v1/agent/web/fetch"


# ----------------------------------------------- the VPS-exclusion hard boundary

@pytest.mark.parametrize("path", [_SEARCH, _FETCH])
def test_api_key_is_refused_with_403(client, path):
    """THE critical assertion: an api-key caller (i.e. every VPS / self-hosted
    resident) is refused outright — our web tools are cloud-only."""
    _, key = _register(client)
    r = client.post(path, headers={"X-API-Key": key}, json={"query": "hi", "url": "https://e.x"})
    assert r.status_code == 403, r.get_data(as_text=True)


# --------------------------------------------------------------- runtime tokens

@pytest.mark.parametrize("path", [_SEARCH, _FETCH])
def test_runtime_token_with_web_scope_passes(client, path):
    user_id, _ = _register(client)
    tok = _mint(user_id, scope=["chat", "web"])
    r = client.post(
        path,
        headers={"X-Feedling-Runtime-Token": tok},
        json={"query": "hi", "url": "https://e.x"},
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {"ok": True, "stub": True}


@pytest.mark.parametrize("path", [_SEARCH, _FETCH])
def test_runtime_token_without_web_scope_is_403(client, path):
    user_id, _ = _register(client)
    tok = _mint(user_id, scope=["chat", "memory"])
    r = client.post(
        path,
        headers={"X-Feedling-Runtime-Token": tok},
        json={"query": "hi", "url": "https://e.x"},
    )
    assert r.status_code == 403, r.get_data(as_text=True)


@pytest.mark.parametrize("path", [_SEARCH, _FETCH])
def test_no_credential_is_401(client, path):
    assert client.post(path, json={"query": "hi", "url": "https://e.x"}).status_code == 401


@pytest.mark.parametrize("path", [_SEARCH, _FETCH])
def test_a_web_scoped_token_for_another_user_is_403(client, path):
    """The scope check still binds the token to its own user — a web-scoped token
    minted for a different user id must not authorize."""
    _register(client)  # ensure at least one real user exists
    tok = _mint("u_someone_else", scope=["web"])
    r = client.post(
        path,
        headers={"X-Feedling-Runtime-Token": tok},
        json={"query": "hi", "url": "https://e.x"},
    )
    # Unknown user id → resolve fails 401; a known-but-mismatched user → 403.
    assert r.status_code in (401, 403), r.get_data(as_text=True)
