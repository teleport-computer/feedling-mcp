"""Endpoint tests for the new route/credential *collection* endpoints (Task 6+7):

  GET    /v1/model_api/routes
  POST   /v1/model_api/routes
  POST   /v1/model_api/routes/{route_id}/activate
  POST   /v1/model_api/routes/{route_id}/test
  DELETE /v1/model_api/routes/{route_id}
  PATCH  /v1/model_api/credentials/{credential_id}
  DELETE /v1/model_api/credentials/{credential_id}

Kept separate from tests/test_model_api_profiles_routes.py (Task 4+5's file, which
covers the singular /v1/model_api/{setup,get,driver,key_envelope,test,delete}
endpoints) so the two task's test suites don't collide.

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


@pytest.fixture
def registered_user(client):
    """A fresh registered user. public_key MUST be random — a fixed literal
    would collide across tests sharing the session DB and trip
    account_exists_for_key (409)."""
    res = client.post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(os.urandom(32)).decode("ascii"),
              "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return {"user_id": body["user_id"], "api_key": body["api_key"]}


@pytest.fixture
def fake_provider(monkeypatch):
    """测活恒成功。patch 打在定义模块 provider_client 上。"""
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: {"usage": {"total_tokens": 1}})
    monkeypatch.setattr(provider_client, "probe_responses_support", lambda cfg: False)


@pytest.fixture
def fake_envelope(monkeypatch):
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store",
                        lambda store, data, item_id="": (_ENV, None))


@pytest.fixture
def fake_enclave(monkeypatch):
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, api_key, purpose="", **kw: b"sk-plain-key")


def _setup_one(client, registered_user, model="claude-sonnet-4-5"):
    headers = {"X-API-Key": registered_user["api_key"]}
    resp = client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": model, "api_key": "sk-ant-xxx"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return headers


# ─────────────────────────── GET /routes ───────────────────────────

def test_routes_list_shape_matches_ios_contract(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    headers = _setup_one(client, registered_user)
    body = client.get("/v1/model_api/routes", headers=headers).get_json()

    assert body["active_route_id"]
    assert len(body["routes"]) == 1
    r = body["routes"][0]
    for key in ("id", "credential_id", "provider", "model", "credential_label",
                "api_key_hint", "base_url", "reasoning_effort", "is_active",
                "test_status", "last_test_at", "last_test_error",
                "last_runtime_error", "last_runtime_error_class"):
        assert key in r, key
    assert "api_key_envelope" not in r      # 密文绝不出现在响应里


# ─────────────────────────── POST /routes ───────────────────────────

def test_create_route_reusing_credential(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]

    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5", "credential_id": cid})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    # 四个 route 端点共用同一个无 envelope 的投影——显式钉住，防将来加字段泄漏密文。
    assert "api_key_envelope" not in resp.get_json().get("route", {})

    assert len(db.model_api_credentials_list(uid)) == 1
    assert len(db.model_api_routes_list(uid)) == 2
    # 未带 activate → 仍是原来那条 active
    assert db.model_api_active_route(uid)["model"] == "claude-sonnet-4-5"


def test_create_route_requires_key_or_credential_id(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    headers = {"X-API-Key": registered_user["api_key"]}
    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "api_key_or_credential_id_required"


def test_create_route_rejects_both_key_and_credential_id(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]

    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5",
        "credential_id": cid, "api_key": "sk-ant-yyy"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "api_key_or_credential_id_required"


def test_create_route_unknown_credential_id_404(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    headers = _setup_one(client, registered_user)
    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5",
        "credential_id": "00000000-0000-0000-0000-000000000000"})
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "credential_not_found"


def test_create_route_with_api_key_always_makes_new_credential(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    """同 provider 允许多把 key —— 带 api_key 总是新建 credential，不是幂等 upsert
    （那是 /v1/model_api/setup 的语义，不是这个端点的）。"""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)

    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "sk-ant-second"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert len(db.model_api_credentials_list(uid)) == 2
    assert len(db.model_api_routes_list(uid)) == 2


def test_create_route_with_api_key_probes_supports_responses_true(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """Issue 2 regression: the api_key branch of model_api_route_create used to
    hardcode supports_responses=False, unlike model_api_setup which probes. A
    relay that DOES implement /v1/responses would then get forced through the
    LiteLLM chat-completions bridge, mangling codex's tool loop."""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    monkeypatch.setattr(provider_client, "probe_responses_support", lambda cfg: True)

    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "openai_compatible", "model": "gpt-relay",
        "base_url": "https://relay.example.com/v1", "api_key": "sk-relay-key"})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    creds = [c for c in db.model_api_credentials_list(uid) if c["provider"] == "openai_compatible"]
    assert len(creds) == 1
    assert creds[0]["supports_responses"] is True


def test_create_route_with_api_key_probes_supports_responses_false(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    monkeypatch.setattr(provider_client, "probe_responses_support", lambda cfg: False)

    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "openai_compatible", "model": "gpt-relay",
        "base_url": "https://relay.example.com/v1", "api_key": "sk-relay-key"})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    creds = [c for c in db.model_api_credentials_list(uid) if c["provider"] == "openai_compatible"]
    assert len(creds) == 1
    assert creds[0]["supports_responses"] is False


def test_create_route_reusing_openai_compatible_credential_omits_base_url(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    """Issue 1 regression: reusing a saved openai_compatible credential via
    credential_id only (no provider/base_url in the payload — exactly what the
    iOS "选已有凭据" flow and API_MODEL_API_ROUTES.md's contract describe) must
    succeed. Previously validate_config ran against the payload's (empty)
    base_url BEFORE the credential's base_url was loaded, so this 400'd with
    base_url required for openai_compatible even though the credential itself
    had a valid base_url."""
    uid = registered_user["user_id"]
    headers = {"X-API-Key": registered_user["api_key"]}
    create_resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "openai_compatible", "model": "gpt-4o-mini",
        "base_url": "https://relay.example.com/v1", "api_key": "sk-relay-key"})
    assert create_resp.status_code == 200, create_resp.get_data(as_text=True)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    assert db.model_api_credentials_list(uid)[0]["base_url"] == "https://relay.example.com/v1"

    resp = client.post("/v1/model_api/routes", headers=headers,
                        json={"credential_id": cid, "model": "gpt-4o-mini"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    route = resp.get_json()["route"]
    assert route["provider"] == "openai_compatible"
    assert route["base_url"] == "https://relay.example.com/v1"
    assert route["credential_id"] == cid


def test_create_route_credential_id_path_bad_model_still_400(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    """Issue 1 fix reordered validate_config to run AFTER the credential is
    loaded — make sure model validation still applies on the credential_id
    path (only provider/base_url are meant to be exempt from the payload)."""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]

    resp = client.post("/v1/model_api/routes", headers=headers,
                        json={"credential_id": cid, "model": ""})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "model required"


def test_create_route_with_activate_true_switches_active(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]

    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5",
        "credential_id": cid, "activate": True})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert db.model_api_active_route(uid)["model"] == "claude-haiku-4-5"


# ── orphan-credential cleanup on a failed route_upsert (Fix 2) ──

def test_create_route_upsert_failure_cleans_up_credential_created_this_request(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """api_key branch: credential_create succeeds, then route_upsert fails (DB
    hiccup) → the credential this request just minted must not be left behind
    holding a real encrypted key the user can neither see nor delete."""
    uid = registered_user["user_id"]
    headers = {"X-API-Key": registered_user["api_key"]}
    monkeypatch.setattr(db, "model_api_route_upsert", lambda *a, **k: None)

    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "sk-ant-orphan"})
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "model_api_route_write_failed"
    assert db.model_api_credentials_list(uid) == []


def test_create_route_upsert_failure_keeps_reused_credential(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """credential_id (reuse) branch: only what THIS request created gets
    cleaned up. A credential that existed before this request — and that other
    routes may still reference — must survive a failed route_upsert."""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    monkeypatch.setattr(db, "model_api_route_upsert", lambda *a, **k: None)

    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5", "credential_id": cid})
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "model_api_route_write_failed"
    creds = db.model_api_credentials_list(uid)
    assert len(creds) == 1
    assert creds[0]["id"] == cid


# ─────────────────────────── activate ───────────────────────────

def test_activate_untested_route_runs_test_and_switches(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)

    resp = client.post(f"/v1/model_api/routes/{r2}/activate", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert "api_key_envelope" not in resp.get_json().get("route", {})
    assert db.model_api_active_route(uid)["id"] == r2
    assert db.model_api_route_get(uid, r2)["test_status"] == "ok"


def test_activate_fails_when_provider_test_fails_and_keeps_old_active(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    old_active = db.model_api_active_route(uid)["id"]
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "bad-model", None)

    def _boom(cfg):
        raise provider_client.ProviderError("provider_http_404", status_code=404)
    monkeypatch.setattr(provider_client, "test_provider_key", _boom)

    resp = client.post(f"/v1/model_api/routes/{r2}/activate", headers=headers)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "provider_test_failed"
    assert resp.get_json()["status_code"] == 404

    assert db.model_api_active_route(uid)["id"] == old_active     # 旧 active 纹丝不动
    assert db.model_api_route_get(uid, r2)["test_status"] == "failed"


def test_activate_mark_ok_write_failure_500(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """Important (Task 9 write-result-checking sweep): _test_route_or_error's
    success-path mark_test('ok') write was unchecked. A silently-swallowed
    failure there returns None (== "test passed") to model_api_route_activate(),
    which then flips is_active=True and replies 200 -- but test_status never
    reached 'ok', so the just-"activated" route is excluded from the roster
    (is_active AND test_status='ok') despite the caller being told it worked."""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    monkeypatch.setattr(db, "model_api_route_mark_test", lambda *a, **k: False)

    resp = client.post(f"/v1/model_api/routes/{r2}/activate", headers=headers)
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "model_api_route_write_failed"


def test_activate_unknown_route_404(client, registered_user, fake_provider,
                                    fake_envelope, fake_enclave):
    headers = _setup_one(client, registered_user)
    resp = client.post(
        "/v1/model_api/routes/00000000-0000-0000-0000-000000000000/activate",
        headers=headers)
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "route_not_found"


# ─────────────────────────── /routes/{id}/test ───────────────────────────

def test_route_test_endpoint_marks_ok(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)

    resp = client.post(f"/v1/model_api/routes/{r2}/test", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert "api_key_envelope" not in resp.get_json().get("route", {})
    assert db.model_api_route_get(uid, r2)["test_status"] == "ok"
    # non-active route test does NOT switch the active route
    assert db.model_api_active_route(uid)["id"] != r2


def test_route_test_mark_ok_write_failure_500(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """Same _test_route_or_error mark_test('ok') check as
    test_activate_mark_ok_write_failure_500, exercised through the sibling
    /routes/{id}/test entry point (the two endpoints share the helper)."""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    monkeypatch.setattr(db, "model_api_route_mark_test", lambda *a, **k: False)

    resp = client.post(f"/v1/model_api/routes/{r2}/test", headers=headers)
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "model_api_route_write_failed"


def test_route_test_unknown_route_404(client, registered_user, fake_provider,
                                      fake_envelope, fake_enclave):
    headers = _setup_one(client, registered_user)
    resp = client.post(
        "/v1/model_api/routes/00000000-0000-0000-0000-000000000000/test",
        headers=headers)
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "route_not_found"


# ── /routes/{id}/test on the ACTIVE route: takeover on failure (Fix 1) ──
#
# Symmetric with DELETE /routes/{id}: testing the active route and having it
# fail must not strand it — is_active + test_status='failed' would drop the
# user out of db.list_agent_runtime_enabled_users(), and the supervisor kills
# a consumer that never self-heals.

def test_route_test_active_route_failure_takes_over_to_other_ok_route(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    db.model_api_route_mark_test(uid, r2, status="ok")
    r1 = db.model_api_active_route(uid)["id"]

    def _boom(cfg):
        raise provider_client.ProviderError("provider_http_429", status_code=429)
    monkeypatch.setattr(provider_client, "test_provider_key", _boom)

    resp = client.post(f"/v1/model_api/routes/{r1}/test", headers=headers)
    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["error"] == "provider_test_failed"
    assert body["status_code"] == 429
    assert body["active_route_id"] == r2

    r1_row = db.model_api_route_get(uid, r1)
    assert r1_row["test_status"] == "failed"
    assert r1_row["is_active"] is False
    assert db.model_api_active_route(uid)["id"] == r2

    roster = {u["user_id"] for u in db.list_agent_runtime_enabled_users()}
    assert uid in roster


def test_route_test_active_route_failure_no_candidate_leaves_none_active(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """Only route → no autoselect candidate. active_route_id: null is the
    correct (legal) response, not a 500 — the user just has no working config
    anymore and drops out of the roster."""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    r1 = db.model_api_active_route(uid)["id"]

    def _boom(cfg):
        raise provider_client.ProviderError("provider_http_429", status_code=429)
    monkeypatch.setattr(provider_client, "test_provider_key", _boom)

    resp = client.post(f"/v1/model_api/routes/{r1}/test", headers=headers)
    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["active_route_id"] is None

    assert db.model_api_active_route(uid) is None
    roster = {u["user_id"] for u in db.list_agent_runtime_enabled_users()}
    assert uid not in roster


def test_route_test_non_active_route_failure_does_not_touch_active(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """Regression guard: the takeover must only fire for the active route —
    testing a non-active route that fails must leave the active route alone
    and must NOT add active_route_id to the body."""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    active = db.model_api_active_route(uid)["id"]

    def _boom(cfg):
        raise provider_client.ProviderError("provider_http_429", status_code=429)
    monkeypatch.setattr(provider_client, "test_provider_key", _boom)

    resp = client.post(f"/v1/model_api/routes/{r2}/test", headers=headers)
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert "active_route_id" not in resp.get_json()

    assert db.model_api_active_route(uid)["id"] == active
    assert db.model_api_route_get(uid, r2)["is_active"] is False


def test_route_test_active_route_deactivate_write_failure_no_double_active(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """Codex finding: model_api_route_deactivate's bool return was discarded.
    Simulate the DB-hiccup path — deactivate() returns False WITHOUT actually
    clearing is_active (leaves r1 still active), exactly like a swallowed
    write exception would. Naively autoselecting anyway would try to make r2
    active too and collide with model_api_routes_one_active, so the fix must
    re-read reality and skip autoselect when a route is still active — and it
    must report the TRUE active route id (still r1), not silently lie."""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    db.model_api_route_mark_test(uid, r2, status="ok")
    r1 = db.model_api_active_route(uid)["id"]

    monkeypatch.setattr(db, "model_api_route_deactivate", lambda *a, **k: False)

    def _boom(cfg):
        raise provider_client.ProviderError("provider_http_429", status_code=429)
    monkeypatch.setattr(provider_client, "test_provider_key", _boom)

    resp = client.post(f"/v1/model_api/routes/{r1}/test", headers=headers)
    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["active_route_id"] == r1
    assert "api_key_envelope" not in body

    # Invariant: never two active routes, even though the patched deactivate()
    # never actually flipped is_active. r1 is still (truthfully) active.
    rows = db.model_api_routes_list(uid)
    active_rows = [r for r in rows if r["is_active"]]
    assert len(active_rows) == 1
    assert active_rows[0]["id"] == r1
    assert db.model_api_route_get(uid, r2)["is_active"] is False


# ─────────────────────────── DELETE /routes/{id} ───────────────────────────

def test_delete_active_route_autoselects_latest_ok(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    db.model_api_route_mark_test(uid, r2, status="ok")
    active = db.model_api_active_route(uid)["id"]

    resp = client.delete(f"/v1/model_api/routes/{active}", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["active_route_id"] == r2
    assert db.model_api_active_route(uid)["id"] == r2


def test_delete_last_route_leaves_no_active(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    active = db.model_api_active_route(uid)["id"]

    resp = client.delete(f"/v1/model_api/routes/{active}", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["active_route_id"] is None
    assert db.model_api_active_route(uid) is None


def test_delete_unknown_route_404(client, registered_user, fake_provider,
                                  fake_envelope, fake_enclave):
    headers = _setup_one(client, registered_user)
    resp = client.delete(
        "/v1/model_api/routes/00000000-0000-0000-0000-000000000000", headers=headers)
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "route_not_found"


def test_delete_non_active_route_keeps_current_active(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    active = db.model_api_active_route(uid)["id"]

    resp = client.delete(f"/v1/model_api/routes/{r2}", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["active_route_id"] == active


# ─────────────────────────── GET /credentials ───────────────────────────

def test_get_credentials_lists_route_count(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)

    resp = client.get("/v1/model_api/credentials", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert len(body["credentials"]) == 1
    cred = body["credentials"][0]
    assert cred["id"] == cid
    assert cred["route_count"] == 2
    for key in ("id", "provider", "label", "base_url", "api_key_hint",
                "supports_responses", "route_count"):
        assert key in cred, key


def test_get_credentials_includes_credential_with_zero_routes(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    """A credential whose last route was deleted must still appear (with
    route_count == 0) — otherwise it's invisible (no id iOS can surface) and
    thus never deletable, so its key sits in the DB forever."""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    active = db.model_api_active_route(uid)["id"]
    del_resp = client.delete(f"/v1/model_api/routes/{active}", headers=headers)
    assert del_resp.status_code == 200

    resp = client.get("/v1/model_api/credentials", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert len(body["credentials"]) == 1
    assert body["credentials"][0]["route_count"] == 0


def test_get_credentials_never_leaks_envelope(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "sk-ant-second"})

    resp = client.get("/v1/model_api/credentials", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert len(body["credentials"]) == 2
    for cred in body["credentials"]:
        assert "api_key_envelope" not in cred


def test_get_credentials_empty_when_none_configured(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    headers = {"X-API-Key": registered_user["api_key"]}
    resp = client.get("/v1/model_api/credentials", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["credentials"] == []


# ─────────────────────────── PATCH /credentials/{id} ───────────────────────────

def test_patch_credential_rotating_key_retests_active_route(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    active_id = db.model_api_active_route(uid)["id"]

    # Minor 1: 换 key 成功后必须刷新 active route 的 last_test_at（新 key 刚被验证）。
    # 秒粒度时间戳的 before/after 对比会 flaky，改成 spy 断言 mark_test(active, ok) 被调到。
    marks: list = []
    _orig_mark = db.model_api_route_mark_test
    def _spy(user_id, route_id, *, status, error=""):
        marks.append((route_id, status))
        return _orig_mark(user_id, route_id, status=status, error=error)
    monkeypatch.setattr(db, "model_api_route_mark_test", _spy)

    resp = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers,
                        json={"api_key": "sk-ant-new", "label": "Key B"})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    creds = db.model_api_credentials_list(uid)
    assert creds[0]["label"] == "Key B"
    assert db.model_api_active_route(uid)["test_status"] == "ok"
    assert (active_id, "ok") in marks      # active route 的 last_test_at 被刷新过


def test_patch_credential_keeps_old_key_when_retest_fails(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    old_hint = db.model_api_credentials_list(uid)[0]["api_key_hint"]

    def _boom(cfg):
        raise provider_client.ProviderError("provider_http_401", status_code=401)
    monkeypatch.setattr(provider_client, "test_provider_key", _boom)

    resp = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers,
                        json={"api_key": "sk-ant-dead"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "provider_test_failed"

    assert db.model_api_credentials_list(uid)[0]["api_key_hint"] == old_hint
    assert db.model_api_active_route(uid)["test_status"] == "ok"


def test_patch_credential_label_only_does_not_retest(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]

    def _boom(cfg):
        raise AssertionError("label-only patch must not call the provider")
    monkeypatch.setattr(provider_client, "test_provider_key", _boom)

    resp = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers,
                        json={"label": "Renamed"})
    assert resp.status_code == 200
    assert db.model_api_credentials_list(uid)[0]["label"] == "Renamed"


def test_patch_credential_nothing_to_update_400(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]

    resp = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers, json={})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "nothing_to_update"


def test_patch_credential_label_only_write_failure_500(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """Important: label-only 写库瞬时失败（update 返回 False）不能假报 200——否则
    用户以为改名成功。必须回 500 model_api_credential_write_failed。"""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    monkeypatch.setattr(db, "model_api_credential_update",
                        lambda *a, **k: False)

    resp = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers,
                        json={"label": "Renamed"})
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "model_api_credential_write_failed"


def test_patch_credential_rotate_key_write_failure_500(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """Important: 换 key 探测通过后写库失败（update 返回 False）不能假报 200——否则
    用户去 provider 吊销旧 key，服务端还攥着旧 envelope，下个 agent 回合必崩。
    必须回 500 model_api_credential_write_failed。"""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    monkeypatch.setattr(db, "model_api_credential_update",
                        lambda *a, **k: False)

    resp = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers,
                        json={"api_key": "sk-ant-new"})
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "model_api_credential_write_failed"


def test_patch_credential_unknown_id_404(client, registered_user, fake_provider,
                                         fake_envelope, fake_enclave):
    headers = _setup_one(client, registered_user)
    resp = client.patch(
        "/v1/model_api/credentials/00000000-0000-0000-0000-000000000000",
        headers=headers, json={"label": "x"})
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "credential_not_found"


def test_patch_credential_rotating_key_marks_non_active_routes_untested(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    """换 key 后，该 credential 下非 active 的 route 全部退回 untested——新 key
    未在它们上验证过。active route 不受影响（它刚被同步测活验证）。"""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    db.model_api_route_mark_test(uid, r2, status="ok")

    resp = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers,
                        json={"api_key": "sk-ant-new"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert db.model_api_route_get(uid, r2)["test_status"] == "untested"


# ─────────────────────────── DELETE /credentials/{id} ───────────────────────────

def test_delete_credential_cascades_routes(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    assert len(db.model_api_routes_list(uid)) == 2

    resp = client.delete(f"/v1/model_api/credentials/{cid}", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["active_route_id"] is None
    assert db.model_api_credentials_list(uid) == []
    assert db.model_api_routes_list(uid) == []


def test_delete_credential_with_active_route_autoselects(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    """删掉持有 active route 的 credential（CASCADE 带走它的 routes）后，若还有
    别的 ok route，自动接管。"""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    active_cid = db.model_api_active_route(uid)["credential_id"]

    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "sk-ant-second"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    other_route = next(r for r in db.model_api_routes_list(uid)
                       if r["credential_id"] != active_cid)
    db.model_api_route_mark_test(uid, other_route["id"], status="ok")

    resp = client.delete(f"/v1/model_api/credentials/{active_cid}", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["active_route_id"] == other_route["id"]
    assert db.model_api_active_route(uid)["id"] == other_route["id"]


def test_delete_credential_write_failure_returns_not_found(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    """Important (Task 9 write-result-checking sweep): model_api_credential_remove
    ignored model_api_credential_delete's return value entirely, so a
    swallowed-exception/race False would still fall through to "status":
    "deleted" 200 -- a false success for a credential still sitting in the DB
    with its key intact. Mirrors model_api_route_remove's existing
    route_delete-False -> 404 handling (a False here almost always means a
    concurrent delete already removed the row, i.e. genuinely not_found by the
    time of this call) -- reuses the already-registered credential_not_found
    slug rather than inventing a new one."""
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    monkeypatch.setattr(db, "model_api_credential_delete", lambda *a, **k: False)

    resp = client.delete(f"/v1/model_api/credentials/{cid}", headers=headers)
    assert resp.status_code == 404, resp.get_data(as_text=True)
    assert resp.get_json()["error"] == "credential_not_found"
    # Not actually deleted (the monkeypatched delete never wrote anything).
    assert db.model_api_credentials_list(uid) != []


def test_delete_credential_unknown_id_404(client, registered_user, fake_provider,
                                          fake_envelope, fake_enclave):
    headers = _setup_one(client, registered_user)
    resp = client.delete(
        "/v1/model_api/credentials/00000000-0000-0000-0000-000000000000",
        headers=headers)
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "credential_not_found"
