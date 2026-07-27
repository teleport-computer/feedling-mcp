"""Endpoint tests for the model_api setup/get/driver/key_envelope/test/delete
routes after the multi-profile write-side migration (Task 5): /v1/model_api/setup
is now an idempotent upsert into model_api_credentials + model_api_routes, and the
read endpoints project the active route.

Requires a real PostgreSQL — see tests/conftest.py.
"""

import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import db  # noqa: E402
import provider_client  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402

_ENV = {"v": 1, "body_ct": "ct", "nonce": "n"}


def _register(client):
    res = client.post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(os.urandom(32)).decode("ascii"),
              "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], {"X-API-Key": body["api_key"]}


@pytest.fixture
def fake_provider(monkeypatch):
    """测活恒成功。patch 打在定义模块 provider_client 上。"""
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: {"usage": {"total_tokens": 1}})


@pytest.fixture
def fake_envelope(monkeypatch):
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store",
                        lambda store, data, item_id="": (_ENV, None))


@pytest.fixture
def fake_enclave(monkeypatch):
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, api_key, purpose="", **kw: b"sk-plain-key")


def test_setup_is_idempotent_and_does_not_accumulate_routes(
        client, fake_provider, fake_envelope, fake_enclave):
    uid, headers = _register(client)
    body = {"provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"}

    for _ in range(3):
        resp = client.post("/v1/model_api/setup", json=body, headers=headers)
        assert resp.status_code == 200, resp.get_data(as_text=True)

    assert len(db.model_api_credentials_list(uid)) == 1
    assert len(db.model_api_routes_list(uid)) == 1


def test_setup_second_model_same_key_adds_route_reuses_credential(
        client, fake_provider, fake_envelope, fake_enclave):
    uid, headers = _register(client)

    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "sk-ant-xxx"})

    assert len(db.model_api_credentials_list(uid)) == 1
    routes = db.model_api_routes_list(uid)
    assert len(routes) == 2
    # 后 setup 的那条是 active
    assert [r["model"] for r in routes if r["is_active"]] == ["claude-haiku-4-5"]


def test_setup_change_model_reusing_saved_key_without_reentering_it(
        client, fake_provider, fake_envelope, fake_enclave):
    """raw api_key 为空 → 复用 active credential 的信封（换 model 不重输 key）。"""
    uid, headers = _register(client)
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})

    resp = client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5"})  # no api_key
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert len(db.model_api_credentials_list(uid)) == 1
    active = db.model_api_active_route(uid)
    assert active["model"] == "claude-haiku-4-5"


def test_get_returns_active_route_projection(
        client, fake_provider, fake_envelope, fake_enclave):
    _uid, headers = _register(client)
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5",
        "api_key": "sk-ant-xxx", "reasoning_effort": "high"})

    cfg = client.get("/v1/model_api/get", headers=headers).get_json()["config"]
    assert cfg["configured"] is True
    assert cfg["provider"] == "anthropic"
    assert cfg["model"] == "claude-sonnet-4-5"
    assert cfg["test_status"] == "ok"
    assert cfg["reasoning_effort"] == "high"
    assert "api_key_envelope" not in cfg


def test_get_without_config_returns_unconfigured(client):
    _uid, headers = _register(client)
    cfg = client.get("/v1/model_api/get", headers=headers).get_json()["config"]
    assert cfg == {"configured": False}


def test_driver_derived_from_active_route(
        client, fake_provider, fake_envelope, fake_enclave):
    _uid, headers = _register(client)
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})

    resp = client.post("/v1/model_api/driver", headers=headers, json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["driver"] == "claude"
    assert body["enabled"] is True


def test_driver_without_config_404(client):
    _uid, headers = _register(client)
    resp = client.post("/v1/model_api/driver", headers=headers, json={})
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "model_api_not_configured"


def test_key_envelope_returns_active_credential_envelope(
        client, fake_provider, fake_envelope, fake_enclave):
    _uid, headers = _register(client)
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})

    resp = client.get("/v1/model_api/key_envelope", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["api_key_envelope"] == _ENV


def test_test_endpoint_validates_active_route(
        client, fake_provider, fake_envelope, fake_enclave):
    """/test decrypts the active route's envelope via the enclave and tests the key,
    with no test_status gate (an untested route can be validated)."""
    _uid, headers = _register(client)
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})

    resp = client.post("/v1/model_api/test", headers=headers, json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["config"]["test_status"] == "ok"


def test_delete_removes_all_credentials_and_routes(
        client, fake_provider, fake_envelope, fake_enclave):
    uid, headers = _register(client)
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})

    assert client.delete("/v1/model_api/delete", headers=headers).status_code == 200
    assert db.model_api_credentials_list(uid) == []
    assert db.model_api_routes_list(uid) == []


def test_setup_change_provider_adds_new_credential(
        client, fake_provider, fake_envelope, fake_enclave):
    """换 provider（anthropic → openai）→ reuse=False → 新建 credential，
    不复用上一个 provider 的 credential。"""
    uid, headers = _register(client)
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-openai-yyy"})

    assert len(db.model_api_credentials_list(uid)) == 2
    assert len(db.model_api_routes_list(uid)) == 2
    active = db.model_api_active_route(uid)
    assert active["provider"] == "openai"
    assert active["model"] == "gpt-4.1-mini"


def test_setup_returns_500_when_activate_loses_race(
        client, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """并发 DELETE 在 route_upsert 与 route_activate 之间 CASCADE 删掉 route →
    activate 返回 False。setup 必须回 500 model_api_route_write_failed，绝不能回
    200「configured」描述一个从未激活的配置。patch 打在定义模块 db 上。"""
    _uid, headers = _register(client)
    monkeypatch.setattr(db, "model_api_route_activate", lambda user_id, route_id: False)

    resp = client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "model_api_route_write_failed"


def test_setup_reuse_credential_update_write_failure_500(
        client, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """Important (Task 9 write-result-checking sweep): the reuse branch of
    model_api_setup — rotating the key on an existing credential when
    (provider, base_url) match the current active route — silently ignored
    model_api_credential_update's return value. If that write fails (DB hiccup,
    or a concurrent delete), the response must NOT say "configured": the new
    key just passed test_provider_key but never landed, and the credential row
    would still carry the OLD envelope while the caller thinks setup succeeded."""
    uid, headers = _register(client)
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})
    old_hint = db.model_api_credentials_list(uid)[0]["api_key_hint"]
    monkeypatch.setattr(db, "model_api_credential_update", lambda *a, **k: False)

    # Same provider/base_url as the active route -> reuse branch (credential_update).
    resp = client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "sk-ant-rotated"})
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "model_api_credential_write_failed"
    # Old key/hint must survive untouched -- no half-write.
    assert db.model_api_credentials_list(uid)[0]["api_key_hint"] == old_hint


def test_legacy_test_endpoint_mark_ok_write_failure_500(
        client, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """Important (Task 9 write-result-checking sweep): _test_active_route's
    success-path mark_test('ok') write was unchecked. A silently-swallowed
    failure there returns None (== "success") to model_api_test(), which then
    replies 200 "status": "ok" even though test_status on the route never
    flipped to 'ok' -- a route the roster (is_active AND test_status='ok')
    would still exclude despite the caller being told the test passed."""
    uid, headers = _register(client)
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})
    monkeypatch.setattr(db, "model_api_route_mark_test", lambda *a, **k: False)

    resp = client.post("/v1/model_api/test", headers=headers, json={})
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "model_api_route_write_failed"


def test_get_response_key_set_adds_prompt_frontier_contract(
        client, fake_provider, fake_envelope, fake_enclave):
    """GET /get keeps the old projection and exposes the persisted frontier."""
    _uid, headers = _register(client)
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5",
        "api_key": "sk-ant-xxx", "reasoning_effort": "high"})

    cfg = client.get("/v1/model_api/get", headers=headers).get_json()["config"]
    assert set(cfg.keys()) == {
        "provider", "model", "base_url", "api_key_hint", "test_status",
        "last_test_at", "created_at", "updated_at", "last_test_error",
        "configured", "privacy_mode", "reasoning_effort",
        "context_window_tokens",
    }
    # created_at/updated_at are populated by the DB defaults on insert.
    assert cfg["created_at"] and cfg["updated_at"]
