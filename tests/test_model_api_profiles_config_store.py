"""Tests for the config_store read side (Task 4): _load_runtime_provider_config /
record_runtime_error / load_active_route now read/write model_api_routes JOIN
model_api_credentials instead of the legacy user_blobs(kind='model_api') blob.

Requires a real PostgreSQL — see tests/conftest.py, which provisions a
throwaway DB and runs migrations to head (so both tables already exist)
before any module is collected.
"""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import db  # noqa: E402
import provider_client  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402

from conftest import seed_user  # noqa: E402

_ENV = {"v": 1, "body_ct": "ct", "nonce": "n"}


def _uid() -> str:
    return f"usr_{uuid.uuid4().hex[:16]}"


@pytest.fixture
def fake_enclave(monkeypatch):
    """envelope → 明文 key。patch 打在定义模块 core.enclave 上（见 CONTRIBUTING §6）。"""
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, api_key, purpose="", **kw: b"sk-plain-key",
    )


def _cred(uid, provider="anthropic", base_url=""):
    return db.model_api_credential_create(
        uid, provider=provider, base_url=base_url, label="key A",
        api_key_envelope=_ENV, api_key_hint="sk-a...451", supports_responses=False)


def test_load_runtime_provider_config_uses_active_route(backend_env, fake_enclave):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r_sonnet = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    r_haiku = db.model_api_route_upsert(
        uid, cid, "claude-haiku-4-5", "medium", 65_536
    )
    db.model_api_route_mark_test(uid, r_sonnet, status="ok")
    db.model_api_route_mark_test(uid, r_haiku, status="ok")
    db.model_api_route_activate(uid, r_haiku)

    cfg = hosted_config_store._load_runtime_provider_config(store, "api-key")
    assert isinstance(cfg, provider_client.ProviderConfig)
    assert cfg.model == "claude-haiku-4-5"
    assert cfg.api_key == "sk-plain-key"
    assert cfg.provider == "anthropic"
    assert cfg.context_window_tokens == 65_536
    assert cfg.reasoning_effort == "medium"


def test_load_runtime_provider_config_without_active_route(backend_env):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    result = hosted_config_store._load_runtime_provider_config(store, "api-key")
    assert result == (None, {"error": "model_api_not_configured"})


def test_load_runtime_provider_config_rejects_untested_active(backend_env, fake_enclave):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    cfg, err = hosted_config_store._load_runtime_provider_config(store, "api-key")
    assert cfg is None
    assert err["error"] == "model_api_not_tested"
    assert err["test_status"] == "untested"


def test_load_runtime_provider_config_forwards_runtime_token(backend_env, monkeypatch):
    """Runtime V2 托管回合走 runtime_token，不带 api_key；只有 runtime_token 非空时才
    透传给 enclave 解密调用（api-key 调用者行为不受影响 — 见下一条测试）。"""
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_mark_test(uid, r, status="ok")
    db.model_api_route_activate(uid, r)

    seen_kwargs = {}

    def fake_decrypt(envelope, api_key, purpose="", **kw):
        seen_kwargs.update(kw)
        return b"sk-plain-key"

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    cfg = hosted_config_store._load_runtime_provider_config(
        store, None, runtime_token="rt-secret-123")
    assert isinstance(cfg, provider_client.ProviderConfig)
    assert seen_kwargs == {"runtime_token": "rt-secret-123"}


def test_load_runtime_provider_config_omits_runtime_token_when_absent(backend_env, monkeypatch):
    """api-key 调用者（无 runtime_token）必须不改变行为：不传 runtime_token kwarg。"""
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_mark_test(uid, r, status="ok")
    db.model_api_route_activate(uid, r)

    seen_kwargs = {}

    def fake_decrypt(envelope, api_key, purpose="", **kw):
        seen_kwargs.update(kw)
        return b"sk-plain-key"

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    cfg = hosted_config_store._load_runtime_provider_config(store, "api-key")
    assert isinstance(cfg, provider_client.ProviderConfig)
    assert seen_kwargs == {}


def test_load_runtime_provider_config_missing_envelope(backend_env):
    """route 存在但没有 envelope（理论上不该发生，但防御式分支要保留）."""
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = db.model_api_credential_create(
        uid, provider="anthropic", base_url="", label="key A",
        api_key_envelope={"placeholder": True}, api_key_hint="sk-a...451",
        supports_responses=False)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_mark_test(uid, r, status="ok")
    db.model_api_route_activate(uid, r)

    # Force the envelope column itself to a non-dict shape via a direct UPDATE,
    # since model_api_credential_create requires a JSONB object (NOT NULL).
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE model_api_credentials SET api_key_envelope = 'null'::jsonb "
            "WHERE id = %s", (cid,))

    cfg, err = hosted_config_store._load_runtime_provider_config(store, "api-key")
    assert cfg is None
    assert err["error"] == "model_api_key_envelope_missing"


def test_load_runtime_provider_config_decrypt_failure(backend_env, monkeypatch):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_mark_test(uid, r, status="ok")
    db.model_api_route_activate(uid, r)

    def boom(envelope, api_key, purpose="", **kw):
        raise RuntimeError("enclave unreachable")

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", boom)

    cfg, err = hosted_config_store._load_runtime_provider_config(store, "api-key")
    assert cfg is None
    assert err["error"] == "model_api_key_decrypt_failed"


def test_load_runtime_provider_config_invalid_provider(backend_env, fake_enclave):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid, provider="not-a-real-provider")
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_mark_test(uid, r, status="ok")
    db.model_api_route_activate(uid, r)

    cfg, err = hosted_config_store._load_runtime_provider_config(store, "api-key")
    assert cfg is None
    assert err["error"] == "model_api_config_invalid"


def test_load_active_route_returns_envelope(backend_env):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    route = hosted_config_store.load_active_route(store)
    assert route is not None
    assert route["api_key_envelope"] == _ENV
    assert route["provider"] == "anthropic"


def test_load_active_route_none_when_unset(backend_env):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    assert hosted_config_store.load_active_route(store) is None


def test_record_runtime_error_writes_active_route(backend_env):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    body, status = hosted_config_store.record_runtime_error(
        store, error="402 insufficient balance", error_class="provider_402")
    assert status == 200
    assert db.model_api_route_get(uid, r)["last_runtime_error"] == "402 insufficient balance"
    assert db.model_api_route_get(uid, r)["last_runtime_error_class"] == "provider_402"


def test_record_runtime_error_can_clear(backend_env):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    hosted_config_store.record_runtime_error(store, error="boom", error_class="x")
    body, status = hosted_config_store.record_runtime_error(store, error="", error_class="")
    assert status == 200
    assert db.model_api_route_get(uid, r)["last_runtime_error"] == ""


def test_record_runtime_error_without_active_route_returns_404(backend_env):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    body, status = hosted_config_store.record_runtime_error(
        store, error="x", error_class="y")
    assert status == 404
    assert body["error"] == "model_api_runtime_profile_missing"


def test_record_runtime_error_does_not_touch_model_api_runtime_blob(backend_env):
    """record_runtime_error 只挪写 last_runtime_error(_class) 到 route 行；
    model_api_runtime blob（rollout flags / last_action_trace_*）保持不受影响，
    继续由 _ensure_model_api_runtime_profile / _patch_model_api_runtime_profile 管理。"""
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    before = db.get_blob(uid, hosted_config_store.MODEL_API_RUNTIME_BLOB)
    assert before is None

    body, status = hosted_config_store.record_runtime_error(
        store, error="boom", error_class="x")
    assert status == 200

    after = db.get_blob(uid, hosted_config_store.MODEL_API_RUNTIME_BLOB)
    assert after is None


# --- Issue 3: the legacy inline action-trace path (_append_model_api_action_trace /
# _patch_model_api_action_trace) used to write last_runtime_error into the
# model_api_runtime BLOB, but setup_core.model_api_runtime_status (and GET
# /v1/model_api/routes) now read it off the active ROUTE row — the same row
# record_runtime_error (the agent-runner path) writes. Writer/reader parity fix. ---

def test_action_trace_append_writes_last_runtime_error_to_active_route(backend_env):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    hosted_config_store._append_model_api_action_trace(
        store, {"status": "failed", "error": "402 insufficient balance"})

    assert db.model_api_route_get(uid, r)["last_runtime_error"] == "402 insufficient balance"


def test_action_trace_patch_writes_last_runtime_error_to_active_route(backend_env):
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    record = hosted_config_store._append_model_api_action_trace(store, {"status": "queued"})
    hosted_config_store._patch_model_api_action_trace(
        store, record["trace_id"], {"status": "failed", "error": "429 rate limited"})

    assert db.model_api_route_get(uid, r)["last_runtime_error"] == "429 rate limited"


def test_action_trace_preserves_runtime_error_class_from_agent_runner_path(backend_env):
    """Legacy inline action-trace path never computes an error_class (only
    agent-runner's record_runtime_error does). It must not clobber a class the
    agent-runner path already wrote: old blob-era _patch_model_api_runtime_profile
    only ever merged the keys it was handed, so a bare last_runtime_error patch
    left a previously-written class untouched. The route write must preserve
    that — see db.model_api_route_mark_runtime_error's error_class=None branch."""
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    hosted_config_store.record_runtime_error(
        store, error="403 quota", error_class="quota_insufficient")

    hosted_config_store._append_model_api_action_trace(
        store, {"status": "failed", "error": "different transient error"})

    route = db.model_api_route_get(uid, r)
    assert route["last_runtime_error"] == "different transient error"
    assert route["last_runtime_error_class"] == "quota_insufficient"


def test_action_trace_tolerates_missing_active_route(backend_env):
    """Best-effort side channel: must not raise when the user has no active
    route (mirrors the old tolerance where _patch_model_api_runtime_profile
    silently returned None if the runtime blob couldn't be seeded)."""
    uid = _uid()
    seed_user(uid)
    from core.store import get_store
    store = get_store(uid)

    record = hosted_config_store._append_model_api_action_trace(
        store, {"status": "failed", "error": "boom"})
    assert record["status"] == "failed"


def test_runtime_status_endpoint_surfaces_action_trace_runtime_error(client):
    """End-to-end: GET /v1/model_api/runtime must surface an error reported
    through the legacy inline action-trace path (the Issue 3 regression)."""
    import base64
    import os

    from conftest import configure_model_api_route
    from core.store import get_store

    res = client.post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(os.urandom(32)).decode("ascii"),
              "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    uid, key = body["user_id"], body["api_key"]

    configure_model_api_route(uid, provider="anthropic", model="claude-3-5-sonnet-latest")

    store = get_store(uid)
    hosted_config_store._append_model_api_action_trace(
        store, {"status": "failed", "error": "402 insufficient balance"})

    resp = client.get("/v1/model_api/runtime", headers={"X-API-Key": key})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["last_runtime_error"] == "402 insufficient balance"
