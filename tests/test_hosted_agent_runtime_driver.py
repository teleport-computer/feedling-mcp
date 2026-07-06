"""Stage B endpoints: per-user driver flag setter + own key-envelope ciphertext.

Both let the agent-runner stop depending on a static roster:
- ``POST /v1/model_api/driver`` flips ``agent_runtime_driver`` (legacy|claude|codex)
  so a user can be gradually moved onto the hosted runtime.
- ``GET /v1/model_api/key_envelope`` returns the caller's OWN ``api_key_envelope``
  ciphertext so the supervisor (holding the user's API key) can self-fetch the
  provider-key envelope and enclave-decrypt it JIT — instead of the roster
  carrying per-user secrets. The server only ever returns ciphertext (E2E intact).
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry as accounts_registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import agent_runtime_cutover  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    accounts_registry._users[:] = []
    accounts_registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_registry._save_users()
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


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


_FAKE_ENVELOPE = {"v": 1, "ct": "Zm9v", "item_id": "model_api_key_abc"}


def _seed_config(user_id: str, **extra) -> dict:
    store = core_store.get_store(user_id)
    config = {
        "provider": "anthropic",
        "model": "claude-3",
        "api_key_hint": "sk-…abc",
        "api_key_envelope": _FAKE_ENVELOPE,
        "test_status": "ok",
        **extra,
    }
    return hosted_config_store._save_model_api_config(store, config)


# ---- POST /v1/model_api/driver ----


def test_enabling_hosting_derives_claude_for_anthropic(client):
    user_id, api_key = _register(client)
    _seed_config(user_id, provider="anthropic")
    res = client.post("/v1/model_api/driver", json={"enabled": True}, headers=_headers(api_key))
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json()["driver"] == "claude"  # derived from provider, not chosen
    store = core_store.get_store(user_id)
    config = hosted_config_store._load_model_api_config(store)
    assert agent_runtime_cutover.resolve_driver(config) == "claude"


def test_enabling_hosting_derives_codex_for_openai(client):
    user_id, api_key = _register(client)
    _seed_config(user_id, provider="openai")
    res = client.post("/v1/model_api/driver", json={"enabled": True}, headers=_headers(api_key))
    assert res.status_code == 200
    assert res.get_json()["driver"] == "codex"


def test_gateway_provider_derives_codex_regardless_of_gateway_flag(client, monkeypatch):
    # Gateway check removed: gemini → codex unconditionally (consumer handles it)
    monkeypatch.delenv("FEEDLING_LITELLM_ENABLE", raising=False)
    user_id, api_key = _register(client)
    _seed_config(user_id, provider="gemini")
    res = client.post("/v1/model_api/driver", json={}, headers=_headers(api_key))
    assert res.status_code == 200
    assert res.get_json()["driver"] == "codex"


def test_gateway_provider_derives_codex_when_gateway_enabled(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_LITELLM_ENABLE", "1")
    user_id, api_key = _register(client)
    _seed_config(user_id, provider="gemini")
    res = client.post("/v1/model_api/driver", json={}, headers=_headers(api_key))
    assert res.status_code == 200
    # gemini/openrouter/openai_compatible → codex (LiteLLM-bridged)
    assert res.get_json()["driver"] == "codex"


def test_set_driver_requires_configured_model_api(client):
    _user_id, api_key = _register(client)
    res = client.post("/v1/model_api/driver", json={"enabled": True}, headers=_headers(api_key))
    assert res.status_code == 404


def test_driver_endpoint_always_derives_from_provider(client):
    # Endpoint no longer has enable/disable toggle; driver is always derived from provider
    user_id, api_key = _register(client)
    _seed_config(user_id, provider="anthropic", agent_runtime_driver="auto")
    res = client.post("/v1/model_api/driver", json={"enabled": False}, headers=_headers(api_key))
    assert res.status_code == 200
    body = res.get_json()
    assert body["enabled"] is True        # always True now
    assert body["driver"] == "claude"     # derived from provider, not from enabled flag
    store = core_store.get_store(user_id)
    config = hosted_config_store._load_model_api_config(store)
    assert agent_runtime_cutover.resolve_driver(config) == "claude"


# ---- GET /v1/model_api/key_envelope ----


def test_get_key_envelope_returns_owner_ciphertext(client):
    user_id, api_key = _register(client)
    _seed_config(user_id)
    res = client.get("/v1/model_api/key_envelope", headers=_headers(api_key))
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["api_key_envelope"] == _FAKE_ENVELOPE
    # never the plaintext key or even the masked hint masquerading as a key
    assert "api_key" not in body


def test_get_key_envelope_404_when_no_envelope(client):
    user_id, api_key = _register(client)
    _seed_config(user_id, api_key_envelope=None)
    res = client.get("/v1/model_api/key_envelope", headers=_headers(api_key))
    assert res.status_code == 404


def test_get_key_envelope_requires_auth(client):
    res = client.get("/v1/model_api/key_envelope")
    assert res.status_code in (401, 403)


# ---- pure unit tests for resolve_driver (new semantics) ----

import pytest
from hosted import agent_runtime_cutover as arc

def test_resolve_driver_returns_codex_for_openrouter():
    assert arc.resolve_driver({"provider": "openrouter"}) == "codex"

def test_resolve_driver_returns_claude_for_anthropic():
    assert arc.resolve_driver({"provider": "anthropic"}) == "claude"

def test_resolve_driver_raises_when_unconfigured():
    with pytest.raises(arc.UnsupportedProviderError):
        arc.resolve_driver(None)

def test_resolve_driver_raises_for_unknown_provider():
    with pytest.raises(arc.UnsupportedProviderError):
        arc.resolve_driver({"provider": "weird"})

def test_resolve_driver_ignores_per_user_flag_and_gateway(monkeypatch):
    monkeypatch.delenv("FEEDLING_LITELLM_ENABLE", raising=False)
    monkeypatch.delenv("FEEDLING_HOST_ALL", raising=False)
    assert arc.resolve_driver({"provider": "openrouter", "agent_runtime_driver": "legacy"}) == "codex"
