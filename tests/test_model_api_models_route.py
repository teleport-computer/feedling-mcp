"""Integration tests for POST /v1/model_api/models (catalog endpoint).

Uses ``make_client`` over the real assembled ASGI app (needs Postgres). NOT in
the conftest ``_PURE_UNIT`` whitelist — without a DB these are collect-ignored /
skipped, not silently green. ``provider_client.list_provider_models`` is
monkeypatched so no real provider network is hit.

Registration/auth helpers mirror ``tests/test_model_api_path.py`` (the plan's
sketch used a nonexistent ``make_client().register_user()``).
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
from core import store as core_store  # noqa: E402
import provider_client  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    accounts_registry._users[:] = []
    accounts_registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_registry._save_users()
    with make_client() as c:
        yield c


def _register(client, seed: int = 0x11) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(bytes([seed]) * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def test_models_route_new_key_success(client, monkeypatch):
    monkeypatch.setattr(provider_client, "list_provider_models",
        lambda provider, key, base_url="": {
            "models": [{"id": "gpt-5.4", "display_name": "GPT-5.4"}],
            "complete": True, "warnings": [], "catalog_supported": True})
    _uid, api_key = _register(client)
    r = client.post("/v1/model_api/models", headers=_headers(api_key),
                    json={"provider": "openai", "api_key": "sk-x"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["provider"] == "openai"
    assert body["models"][0]["id"] == "gpt-5.4"
    assert body["catalog_supported"] is True


def test_models_route_requires_exactly_one_credential(client, monkeypatch):
    _uid, api_key = _register(client)
    r = client.post("/v1/model_api/models", headers=_headers(api_key),
                    json={"provider": "openai"})      # 都没给
    assert r.status_code == 400
    assert r.get_json()["error"] == "api_key_or_credential_id_required"
    r2 = client.post("/v1/model_api/models", headers=_headers(api_key),
                     json={"provider": "openai", "api_key": "k", "credential_id": "c"})
    assert r2.status_code == 400                       # 都给了


def test_models_route_provider_auth_failure_maps_to_400_not_401(client, monkeypatch):
    def boom(*a, **k):
        raise provider_client.ProviderError("provider_http_401", status_code=401)
    monkeypatch.setattr(provider_client, "list_provider_models", boom)
    _uid, api_key = _register(client)
    r = client.post("/v1/model_api/models", headers=_headers(api_key),
                    json={"provider": "openai", "api_key": "bad"})
    assert r.status_code == 400                        # 不是 401
    assert r.get_json()["error"] == "model_catalog_auth_failed"


def test_models_route_bedrock_unsupported_is_200(client, monkeypatch):
    monkeypatch.setattr(provider_client, "list_provider_models",
        lambda *a, **k: {"models": [], "complete": True, "warnings": [],
                         "catalog_supported": False})
    _uid, api_key = _register(client)
    r = client.post("/v1/model_api/models", headers=_headers(api_key),
                    json={"provider": "bedrock", "api_key": "k"})
    assert r.status_code == 200
    assert r.get_json()["catalog_supported"] is False
