from __future__ import annotations

import base64
import json
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry as accounts_registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from chat import service as chat_service  # noqa: E402
from hosted import turn as hosted_turn  # noqa: E402
import provider_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import chat_send_core  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402
from hosted import history_import  # noqa: E402
from hosted import setup_core  # noqa: E402
from identity import service as identity_service  # noqa: E402
from model_api_runtime.v2 import jobs_store, prompt_frontier  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Fresh-setup happy-path sends here rely on setup's startup
    # materialization landing V2 with no explicit flip — the v2_only fleet
    # contract (see test_asgi_hosted_chat_send.py's ``env`` fixture for the
    # full rationale). Pin it here so the default "dual" policy (Task 5)
    # doesn't leave fresh users on the still-resident per-user fence.
    monkeypatch.setenv(hosted_config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
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
    # Full-path hosted sends enter the pooled V2 worker lane.
    monkeypatch.setattr(jobs_store, "workers_alive", lambda **kw: True)
    monkeypatch.setattr(jobs_store, "live_worker_capacity", lambda **kw: 4)
    monkeypatch.setattr(jobs_store, "inflight_job_count", lambda **kw: 0)
    monkeypatch.setattr(jobs_store, "recent_mean_service_sec", lambda **kw: None)
    monkeypatch.setattr(chat_send_core.kill_switch, "turns_halted", lambda **kw: False)
    with make_client() as c:
        yield c


def _register(client, seed: int = 0x11) -> tuple[str, str]:
    # ``seed`` fills the 32-byte content public key; callers that register more
    # than one account in a single test must pass distinct seeds (the key is the
    # account identity — a repeat 409s as account_exists_for_key).
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(bytes([seed]) * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _wait_history_import_job(client, api_key: str, job_id: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    last_job = {}
    while time.time() < deadline:
        res = client.get(f"/v1/history_import/status/{job_id}", headers=_headers(api_key))
        assert res.status_code == 200, res.get_data(as_text=True)
        last_job = res.get_json()["job"]
        if last_job["status"] in {"completed", "failed"}:
            return last_job
        time.sleep(0.02)
    raise AssertionError(f"history import job did not finish: {last_job}")


def _identity_payload() -> dict:
    names = ["Attentive", "Steady", "Playful", "Protective", "Curious", "Direct", "Tender"]
    return {
        "agent_name": "IO",
        "self_introduction": "I imported the history and can now answer with context.",
        "category": "Attentive · Grounded",
        "signature": ["Built from receipts", "Ready to keep noticing"],
        "dimensions": [
            {"name": name, "value": 50 + idx, "description": f"Grounded dimension {idx}"}
            for idx, name in enumerate(names)
        ],
    }


def _fake_shared_envelope_builder(captured: list | None = None):
    counter = {"n": 0}

    def _build(store, plaintext: bytes, *, item_id: str | None = None):
        counter["n"] += 1
        if captured is not None:
            try:
                captured.append(json.loads(plaintext.decode("utf-8")))
            except Exception:
                captured.append(plaintext.decode("utf-8"))
        return {
            "v": 1,
            "id": item_id or f"env_{counter['n']}",
            "body_ct": f"ct_{counter['n']}",
            "nonce": f"nonce_{counter['n']}",
            "K_user": f"k_user_{counter['n']}",
            "K_enclave": f"k_enclave_{counter['n']}",
            "visibility": "shared",
            "owner_user_id": store.user_id,
            "enclave_pk_fpr": "test",
        }, ""

    return _build


def test_chat_response_plaintext_reasoning_builds_thinking_extra(monkeypatch):
    captured_plaintexts: list = []

    class Store:
        user_id = "user_test"

    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        _fake_shared_envelope_builder(captured_plaintexts),
    )

    extra = chat_service._chat_plaintext_thinking_extra_for_store(
        Store(),
        {
            "reasoning_text": "Checked the provider-native reasoning field.",
            "reasoning_kind": "provider_reasoning",
            "reasoning_source": "openrouter",
            "reasoning_model": "anthropic/claude-sonnet-4.5",
            "reasoning_native": True,
        },
    )

    assert captured_plaintexts == ["Checked the provider-native reasoning field."]
    assert extra["thinking_body_ct"] == "ct_1"
    assert extra["thinking_nonce"] == "nonce_1"
    assert extra["thinking_kind"] == "provider_reasoning"
    assert extra["thinking_source"] == "openrouter"
    assert extra["thinking_model"] == "anthropic/claude-sonnet-4.5"
    assert extra["thinking_native"] is True


def test_chat_response_plaintext_reasoning_default_is_summary(monkeypatch):
    captured_plaintexts: list = []

    class Store:
        user_id = "user_test"

    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        _fake_shared_envelope_builder(captured_plaintexts),
    )

    extra = chat_service._chat_plaintext_thinking_extra_for_store(
        Store(),
        {"reasoning_text": "A tagged or flattened reasoning block."},
    )

    assert captured_plaintexts == ["A tagged or flattened reasoning block."]
    assert extra["thinking_kind"] == "provider_reasoning_summary"
    assert extra["thinking_source"] == "chat_response.reasoning_text"
    assert "thinking_native" not in extra


def test_model_api_setup_encrypts_and_redacts(client, monkeypatch):
    user_id, api_key = _register(client)
    raw_provider_key = "sk-test-secret"
    scheduled = {}

    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(
        setup_core,
        "_kick_setup_main_vision_test",
        lambda store, route_id, route_updated_at, caller_api_key: scheduled.update(
            {
                "user_id": store.user_id,
                "route_id": route_id,
                "route_updated_at": route_updated_at,
                "caller_api_key": caller_api_key,
            }
        ),
    )

    route_res = client.post(
        "/v1/onboarding/route",
        json={"route": "model_api"},
        headers=_headers(api_key),
    )
    assert route_res.status_code == 200

    setup = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openrouter",
            "model": "openai/gpt-4.1-mini",
            "api_key": raw_provider_key,
        },
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)
    public = setup.get_json()["config"]
    assert public["configured"] is True
    assert public["provider"] == "openrouter"
    assert public["context_window_tokens"] == 128_000
    assert "api_key" not in public
    assert "api_key_envelope" not in public
    assert scheduled["user_id"] == user_id
    assert scheduled["route_id"] == db.model_api_active_route(user_id)["id"]
    assert scheduled["route_updated_at"].endswith("Z")
    assert scheduled["caller_api_key"] == api_key

    get_res = client.get("/v1/model_api/get", headers=_headers(api_key))
    assert get_res.status_code == 200
    assert "api_key_envelope" not in get_res.get_json()["config"]

    cred_id = db.model_api_credentials_list(user_id)[0]["id"]
    stored = db.model_api_credential_get(user_id, cred_id)
    config_text = json.dumps(stored)
    assert raw_provider_key not in config_text
    assert "api_key_envelope" in config_text

    validate = client.get("/v1/onboarding/validate", headers=_headers(api_key))
    assert validate.status_code == 200
    body = validate.get_json()
    assert body["route"] == "model_api"
    assert body["stage"] == "history_import"
    assert all(step["id"] != "resident_consumer" for step in body["steps"])
    runtime = db.get_blob(user_id, "model_api_runtime")
    assert runtime["runtime_mode"] == "hosted_resident"
    assert runtime["tool_action_enabled"] is True
    assert any(step["id"] == "hosted_runtime" and step["passing"] for step in body["steps"])


def test_model_api_setup_auto_probe_is_nonblocking_and_updates_config(
    client,
    monkeypatch,
    enable_setup_auto_vision_probe,
):
    _user_id, api_key = _register(client)
    probe_started = threading.Event()
    release_probe = threading.Event()

    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )

    def text_only_catalog(*_args):
        probe_started.set()
        assert release_probe.wait(timeout=2)
        return {
            "models": [
                {
                    "id": "vendor/text-only",
                    "input_modalities": ["text"],
                }
            ]
        }

    monkeypatch.setattr(provider_client, "list_provider_models", text_only_catalog)
    pixel_probes = []

    def reject_pixel_probe(_config, messages, **kwargs):
        pixel_probes.append((messages, kwargs))
        # A single color can never satisfy the randomized probe's requirement
        # for two correct colors, so the authoritative result is unsupported.
        return {"reply": "red"}

    monkeypatch.setattr(
        provider_client,
        "chat_completion",
        reject_pixel_probe,
    )
    route_res = client.post(
        "/v1/onboarding/route",
        json={"route": "model_api"},
        headers=_headers(api_key),
    )
    assert route_res.status_code == 200

    setup = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openrouter",
            "model": "vendor/text-only",
            "api_key": "sk-test-secret",
            "context_window_tokens": 32_768,
        },
        headers=_headers(api_key),
    )

    assert setup.status_code == 200, setup.get_data(as_text=True)
    assert probe_started.wait(timeout=1)
    before = client.get("/v1/vision/config", headers=_headers(api_key)).get_json()
    assert before["config"]["effective_status"] == "untested"

    release_probe.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        after = client.get("/v1/vision/config", headers=_headers(api_key)).get_json()
        if after["config"]["effective_status"] == "unsupported":
            break
        time.sleep(0.01)
    else:
        pytest.fail("setup-triggered vision probe did not publish its verdict")

    assert after["config"]["main_model"]["vision_test_status"] == "unsupported"
    assert len(pixel_probes) == 1
    assert isinstance(pixel_probes[0][0][0]["content"], list)


@pytest.mark.parametrize(
    ("provider", "model", "base_url"),
    [
        ("openrouter", "vendor/unknown-model", ""),
        ("openai_compatible", "private-model", "https://relay.host/v1"),
    ],
)
def test_model_api_setup_rejects_unconfigured_prompt_frontier_before_io(
    client, monkeypatch, provider, model, base_url,
):
    # Fail-closed mode: with the unaudited default disabled an unconfigured
    # custom route is still rejected before any provider I/O or key encryption.
    # (Default-on gives a conservative window instead — see test_v2_prompt_frontier.)
    monkeypatch.setenv("FEEDLING_V2_UNAUDITED_DEFAULT_CONTEXT_WINDOW_TOKENS", "0")
    user_id, api_key = _register(client)
    provider_calls = []

    def provider_test(cfg):
        provider_calls.append(cfg)
        raise AssertionError("frontier validation must run before provider I/O")

    def envelope_build(*_args, **_kwargs):
        raise AssertionError("frontier validation must run before key encryption")

    monkeypatch.setattr(provider_client, "test_provider_key", provider_test)
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store", envelope_build
    )
    payload = {
        "provider": provider,
        "model": model,
        "api_key": "sk-test",
    }
    if base_url:
        payload["base_url"] = base_url

    setup = client.post(
        "/v1/model_api/setup",
        json=payload,
        headers=_headers(api_key),
    )

    assert setup.status_code == 400
    body = setup.get_json()
    assert body["error"] == "prompt_context_limit_unconfigured"
    assert body["required"] == "context_window_tokens"
    assert provider_calls == []
    assert db.model_api_routes_list(user_id) == []
    assert db.model_api_credentials_list(user_id) == []


def test_model_api_setup_custom_relay_uses_unaudited_default(client, monkeypatch):
    """Recovery: a custom relay that does NOT send context_window_tokens now
    configures successfully using the unaudited default (65536)
    instead of being rejected with prompt_context_limit_unconfigured — the
    gate that had silently blocked every custom relay app-wide since 07-19."""
    monkeypatch.delenv("FEEDLING_V2_UNAUDITED_DEFAULT_CONTEXT_WINDOW_TOKENS", raising=False)
    user_id, api_key = _register(client)
    tested = []
    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: tested.append(cfg) or {"reply": "ok", "usage": {}},
    )

    setup = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openai_compatible",
            "model": "private-model",
            "base_url": "https://relay.host/v1",
            "api_key": "sk-relay",
        },
        headers=_headers(api_key),
    )

    assert setup.status_code == 200, setup.get_data(as_text=True)
    assert setup.get_json()["config"]["context_window_tokens"] == 65536
    assert tested[0].context_window_tokens == 65536
    route = db.model_api_active_route(user_id)
    assert route["context_window_tokens"] == 65536


def test_model_api_setup_persists_explicit_custom_prompt_frontier(
    client, monkeypatch,
):
    user_id, api_key = _register(client)
    tested = []
    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: tested.append(cfg) or {"reply": "ok", "usage": {}},
    )

    setup = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openai_compatible",
            "model": "private-model",
            "base_url": "https://relay.host/v1",
            "api_key": "sk-relay",
            "context_window_tokens": 32_768,
        },
        headers=_headers(api_key),
    )

    assert setup.status_code == 200, setup.get_data(as_text=True)
    assert setup.get_json()["config"]["context_window_tokens"] == 32_768
    assert tested[0].context_window_tokens == 32_768
    route = db.model_api_active_route(user_id)
    assert route["context_window_tokens"] == 32_768
    runtime = hosted_config_store._provider_config_from_plain(route, "sk-relay")
    assert runtime.context_window_tokens == 32_768
    assert runtime.reasoning_effort == ""

    # Exact idempotent setup may reuse the persisted contract, so a client does
    # not have to resend the field on every key/model health refresh.
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"sk-relay",
    )
    repeated = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openai_compatible",
            "model": "private-model",
            "base_url": "https://relay.host/v1",
        },
        headers=_headers(api_key),
    )
    assert repeated.status_code == 200, repeated.get_data(as_text=True)
    assert repeated.get_json()["config"]["context_window_tokens"] == 32_768


def test_model_api_setup_replaces_metadata_below_runtime_floor(
    client, monkeypatch,
):
    user_id, api_key = _register(client)
    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {}},
    )

    setup = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openai_compatible",
            "model": "private-model",
            "base_url": "https://relay.host/v1",
            "api_key": "sk-relay",
            # Default output + safety reservations consume 5,120 tokens.
            "context_window_tokens": 5_120,
        },
        headers=_headers(api_key),
    )

    assert setup.status_code == 200, setup.get_data(as_text=True)
    resolved = prompt_frontier.unaudited_default_context_window()
    assert setup.get_json()["config"]["context_window_tokens"] == resolved
    route = db.model_api_routes_list(user_id)[0]
    assert route["context_window_tokens"] == resolved


def test_model_api_setup_leaves_responses_support_false(client, monkeypatch):
    """`supports_responses` 列保留（V1 roster payload 仍带它，删列要迁移且
    收益为零），但不再由探测填写——恒 false，对每个 provider 都一样。"""
    user_id, api_key = _register(client)
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: {"reply": "ok", "usage": {}})
    client.post("/v1/onboarding/route", json={"route": "model_api"}, headers=_headers(api_key))
    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai_compatible", "model": "gpt-5.4",
              "base_url": "https://relay.host/v1", "api_key": "sk-relay",
              "context_window_tokens": 128_000},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)
    route = db.model_api_active_route(user_id)
    assert route["supports_responses"] is False


def test_model_api_setup_persists_reasoning_effort(client, monkeypatch):
    # reasoning_effort is persisted on the provider route for the unified native
    # V2 loop; it must not make the account eligible for the external resident
    # consumer roster.
    user_id, api_key = _register(client)
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: {"reply": "ok", "usage": {}})

    setup = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "api_key": "sk-or",
            "reasoning_effort": "medium",
        },
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)
    assert setup.get_json()["config"]["reasoning_effort"] == "medium"
    route = db.model_api_active_route(user_id)
    assert route["reasoning_effort"] == "medium"
    runtime = hosted_config_store._provider_config_from_plain(route, "sk-or")
    assert runtime.reasoning_effort == "medium"

    mode, state, _generation = db.get_hosted_runtime_control_strict(user_id)
    assert (mode, state) == ("db_action_v2", "v2")


def test_model_api_setup_reasoning_effort_off_and_default(client, monkeypatch):
    user_off, key_off = _register(client, seed=0x21)
    user_default, key_default = _register(client, seed=0x22)
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: {"reply": "ok", "usage": {}})

    off = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "api_key": "sk-or",
            "reasoning_effort": "off",
        },
        headers=_headers(key_off),
    )
    assert off.status_code == 200, off.get_data(as_text=True)
    default = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "api_key": "sk-or",
        },
        headers=_headers(key_default),
    )
    assert default.status_code == 200, default.get_data(as_text=True)

    route_off = db.model_api_active_route(user_off)
    route_default = db.model_api_active_route(user_default)
    assert route_off["reasoning_effort"] == "off"
    assert not route_default.get("reasoning_effort")


def test_model_api_setup_rejects_invalid_reasoning_effort(client, monkeypatch):
    _user_id, api_key = _register(client)

    def provider_test_must_not_run(cfg):
        raise AssertionError("invalid reasoning_effort should fail before provider probe")

    monkeypatch.setattr(provider_client, "test_provider_key", provider_test_must_not_run)
    setup = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "api_key": "sk-or",
            "reasoning_effort": "mediumish",
        },
        headers=_headers(api_key),
    )
    assert setup.status_code == 400
    assert setup.get_json()["error"] == "invalid_reasoning_effort"


def test_setup_never_warns_about_responses_for_chat_only_relay(client, monkeypatch):
    """chat-only 中转（Moonshot 全系）配置成功后不得带任何 responses 警告。

    退役理由（2026-07-27，代码取证 + test 环境四轮实测）：那条 warning 的因由是
    「LiteLLM 强制 responses→chat-completions 桥接 → mangle codex 工具循环」，
    三个前提全部失效——LiteLLM 已移除；openai_compatible 派生的是 pi 而非 codex
    (hosted/agent_runtime_cutover.py)；V2 全程 chat_completion_async，
    provider_client 里 /responses 的唯一入口条件是 provider == "openai"。
    实测 Kimi/Moonshot 在 V1(pi) 与 V2 两条路径上记忆写入、连续回读、工具调用
    (trajectory 记到 tool_call_started/result 各 3 次) 全部正常。
    """
    _uid, api_key = _register(client)
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: {"reply": "ok", "usage": {}})
    client.post("/v1/onboarding/route", json={"route": "model_api"}, headers=_headers(api_key))
    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai_compatible", "model": "kimi-k2.5",
              "base_url": "https://api.moonshot.cn/v1", "api_key": "sk-relay",
              "context_window_tokens": 128_000},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)
    assert not setup.get_json().get("warnings"), setup.get_json()
    notices = client.get("/v1/notices", headers=_headers(api_key)).get_json()["notices"]
    assert [n for n in notices if n["error_class"] == "responses_unsupported"] == []


def test_setup_does_not_probe_responses_endpoint(client, monkeypatch):
    """setup 不得再对中转打 /responses：该探测唯一的下游是已退役的 warning，
    `supports_responses` 在整个 backend 没有任何行为消费点（consumer_env 也不读
    它），却要为每次 openai_compatible setup 付一次最多 20s 的网络往返。"""
    _uid, api_key = _register(client)
    assert not hasattr(provider_client, "probe_responses_support")
    posted: list = []
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: {"reply": "ok", "usage": {}})

    class _NoResponsesProbe:
        def post(self, url, **_kw):
            posted.append(url)
            raise AssertionError(f"setup must not POST {url}")

    monkeypatch.setattr(provider_client, "_http_client", lambda: _NoResponsesProbe())
    client.post("/v1/onboarding/route", json={"route": "model_api"}, headers=_headers(api_key))
    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai_compatible", "model": "kimi-k2.5",
              "base_url": "https://api.moonshot.cn/v1", "api_key": "sk-relay",
              "context_window_tokens": 128_000},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)
    assert posted == []


@pytest.mark.xfail(
    reason="model_api memory repair apply still assumes legacy type fields; "
           "retired route-B repair path, not part of v1 onboarding",
    strict=False,
)
def test_model_api_memory_repair_archives_noisy_cards_only_after_replacements(client, monkeypatch):
    user_id, api_key = _register(client)
    captured_plaintexts: list = []
    memory_plaintexts = {
        "bad_import": {
            "type": "moment",
            "title": "导入片段 7",
            "description": "===== BEGIN CHAT HISTORY FILE: conversations.json =====\n{\"conversation_id\":\"raw\"}",
            "context": "raw import artifact",
        },
        "good_directness": {
            "type": "fact",
            "title": "Direct answers",
            "description": "User prefers direct, concrete engineering answers with clear tradeoffs.",
        },
        "good_memory": {
            "type": "fact",
            "title": "Readable memory",
            "description": "User wants imported history distilled into readable long-term memory, not raw archive fragments.",
        },
    }

    def fake_decrypt(envelope, key, purpose):
        if purpose == "model_api_provider_key":
            return b"sk-test"
        plain = memory_plaintexts.get(str(envelope.get("id") or ""))
        if plain is None:
            plain = {"title": "Unknown", "description": "Unknown memory.", "type": "fact"}
        return json.dumps(plain).encode("utf-8")

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_shared_envelope_builder(captured_plaintexts))
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)
    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})

    def fake_chat_completion(cfg, messages, **kwargs):
        return {
            "reply": json.dumps({
                "candidates": [
                    {
                        "candidate_type": "preference",
                        "subject": "user",
                        "title": "Readable memory preference",
                        "summary": (
                            "User repeatedly wants imported histories to become readable durable memory "
                            "instead of raw JSON or generic archive fragments."
                        ),
                        "importance_signals": ["explicit_memory", "future_utility"],
                        "first_seen_at": "2026-06-02",
                        "confidence": 0.92,
                    },
                    {
                        "candidate_type": "relationship_event",
                        "subject": "relationship",
                        "title": "API runtime review",
                        "summary": (
                            "User reviewed the API runtime and asked that memory/identity changes be written "
                            "through Feedling instead of only claimed in chat."
                        ),
                        "importance_signals": ["decision_made", "future_utility"],
                        "first_seen_at": "2026-06-02",
                        "confidence": 0.9,
                    },
                ]
            }),
            "usage": {},
        }

    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    db.memory_replace_all(user_id, [
        {
            "v": 1,
            "id": "bad_import",
            "type": "moment",
            "occurred_at": "2026-06-01",
            "created_at": "2026-06-01T00:00:00",
            "source": "history_import",
            "body_ct": "ct_bad",
            "nonce": "n_bad",
            "K_user": "ku_bad",
            "K_enclave": "ke_bad",
            "visibility": "shared",
            "owner_user_id": user_id,
        },
        {
            "v": 1,
            "id": "good_directness",
            "type": "fact",
            "occurred_at": "2026-06-02",
            "created_at": "2026-06-02T00:00:00",
            "source": "history_import",
            "body_ct": "ct_good_1",
            "nonce": "n_good_1",
            "K_user": "ku_good_1",
            "K_enclave": "ke_good_1",
            "visibility": "shared",
            "owner_user_id": user_id,
        },
        {
            "v": 1,
            "id": "good_memory",
            "type": "fact",
            "occurred_at": "2026-06-02",
            "created_at": "2026-06-02T00:00:01",
            "source": "history_import",
            "body_ct": "ct_good_2",
            "nonce": "n_good_2",
            "K_user": "ku_good_2",
            "K_enclave": "ke_good_2",
            "visibility": "shared",
            "owner_user_id": user_id,
        },
    ])

    dry = client.post(
        "/v1/model_api/memory/repair",
        json={"mode": "dry_run"},
        headers=_headers(api_key),
    )
    assert dry.status_code == 200, dry.get_data(as_text=True)
    dry_preview = dry.get_json()["preview"]
    assert dry_preview["old_cards_detected"] == 1
    assert dry_preview["new_cards_planned"] == 6
    assert dry_preview["noisy_ids"] == ["bad_import"]

    apply = client.post(
        "/v1/model_api/memory/repair",
        json={"mode": "apply", "synchronous": True},
        headers=_headers(api_key),
    )
    assert apply.status_code == 200, apply.get_data(as_text=True)
    job = apply.get_json()["job"]
    assert job["status"] == "completed"
    assert job["new_cards_created"] >= 1
    assert job["old_cards_archived"] == 1

    saved = db.memory_load(user_id)
    by_id = {row["id"]: row for row in saved}
    assert by_id["bad_import"]["is_archived"] is True
    assert by_id["bad_import"]["archive_reason"]
    assert any(row.get("source") == "model_api_repair" for row in saved)
    assert any(
        isinstance(item, dict) and "readable durable memory" in item.get("description", "")
        for item in captured_plaintexts
    )

    visible = client.get("/v1/memory/list?limit=20", headers=_headers(api_key))
    assert visible.status_code == 200, visible.get_data(as_text=True)
    assert all(row["id"] != "bad_import" for row in visible.get_json()["moments"])

    with_archived = client.get("/v1/memory/list?limit=20&include_archived=true", headers=_headers(api_key))
    assert with_archived.status_code == 200, with_archived.get_data(as_text=True)
    assert any(row["id"] == "bad_import" for row in with_archived.get_json()["moments"])


def test_model_api_setup_logs_provider_test_failure(client, monkeypatch, capsys):
    # A failed self-test (bad/quota'd key, or an unsupported model name) must
    # leave a server-side log line with provider/model/status_code so the
    # failure is traceable — the response body alone never reaches the logs.
    _, api_key = _register(client)

    def boom(cfg):
        raise provider_client.ProviderError(
            "provider_http_404: model: claude-3-5-haiku-latest", status_code=404
        )

    monkeypatch.setattr(provider_client, "test_provider_key", boom)

    setup = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "anthropic",
            "model": "claude-3-5-haiku-latest",
            "api_key": "sk-ant-whatever",
        },
        headers=_headers(api_key),
    )
    assert setup.status_code == 400
    assert setup.get_json()["error"] == "provider_test_failed"

    out = capsys.readouterr().out
    assert "anthropic" in out
    assert "claude-3-5-haiku-latest" in out
    assert "404" in out
    assert "sk-ant-whatever" not in out  # never log the raw provider key


def test_model_api_setup_can_reuse_saved_key_when_model_changes(client, monkeypatch):
    _, api_key = _register(client)
    calls = []

    def fake_test_provider_key(cfg):
        calls.append((cfg.provider, cfg.model, cfg.api_key, cfg.base_url))
        return {"reply": "ok", "usage": {"total_tokens": 1}}

    monkeypatch.setattr(provider_client, "test_provider_key", fake_test_provider_key)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-existing"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)
    first = setup.get_json()["config"]

    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-existing",
    )
    update = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1"},
        headers=_headers(api_key),
    )
    assert update.status_code == 200, update.get_data(as_text=True)
    second = update.get_json()["config"]
    assert second["provider"] == "openai"
    assert second["model"] == "gpt-4.1"
    assert second["api_key_hint"] == first["api_key_hint"]
    assert calls[-1] == ("openai", "gpt-4.1", "sk-existing", "https://api.openai.com/v1")


def test_history_import_relationship_date_accepts_flexible_user_input():
    assert identity_service._parse_iso_calendar_date("20260602") == date(2026, 6, 2)
    assert identity_service._parse_iso_calendar_date("2026/06/02") == date(2026, 6, 2)
    assert identity_service._parse_iso_calendar_date("2026年6月2日") == date(2026, 6, 2)
    assert identity_service._parse_iso_calendar_date("2026-02-31") is None

    parsed, err = history_import._relationship_start_from_import(
        {"relationship_started_at": "20260602"},
        [],
    )
    assert parsed == date(2026, 6, 2)
    assert err == ""

    fallback, err = history_import._relationship_start_from_import(
        {"relationship_started_at": "not a date"},
        [],
    )
    assert fallback == date.today()
    assert err == ""


def test_history_import_and_hosted_chat_complete_model_api_path(client, monkeypatch):
    user_id, api_key = _register(client)

    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-test-secret",
    )

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "memory candidate" in joined.lower() or "Memory Garden" in joined:
            return {
                "reply": (
                    '{"memories":['
                    '{"type":"moment","title":"First import moment","description":"User shared a concrete concern.","occurred_at":"2026-05-31"},'
                    '{"type":"fact","title":"User preference","description":"User prefers direct answers.","occurred_at":"2026-05-31"}'
                    "]}"
                ),
                "usage": {},
            }
        if "Derive a Feedling Identity Card" in joined:
            return {"reply": json.dumps(_identity_payload()), "usage": {}}
        return {"reply": "I can answer from the imported history now.", "usage": {"total_tokens": 12}}

    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test-secret"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    transcript = "\n".join([
        "2026-05-31 User: I prefer direct answers and want to test the API route.",
        "2026-05-31 Assistant: I will keep replies direct and grounded.",
    ])
    upload = client.post(
        "/v1/history_import/upload",
        json={
            "format": "plaintext",
            "content": transcript,
            "relationship_started_at": "2026-05-31",
            "client_job_id": "test-history-import-complete",
        },
        headers=_headers(api_key),
    )
    assert upload.status_code == 202, upload.get_data(as_text=True)
    queued_job = upload.get_json()["job"]
    assert queued_job["status"] == "queued"
    assert queued_job["phase"] == "upload_received"
    duplicate = client.post(
        "/v1/history_import/upload",
        json={
            "format": "plaintext",
            "content": transcript,
            "relationship_started_at": "2026-05-31",
            "client_job_id": "test-history-import-complete",
        },
        headers=_headers(api_key),
    )
    assert duplicate.status_code in (200, 202), duplicate.get_data(as_text=True)
    assert duplicate.get_json()["job"]["job_id"] == queued_job["job_id"]
    job = _wait_history_import_job(client, api_key, queued_job["job_id"])
    assert job["status"] == "completed"
    assert job["phase"] == "completed"
    assert job["progress"] == 100
    assert job["messages_parsed"] == 2
    assert job["memories_created"] >= 2
    assert job["identity_written"] is True
    assert job["chat_messages_imported"] == 0
    assert job["onboarding_greeting_written"] is True

    mid_validate = client.get("/v1/onboarding/validate", headers=_headers(api_key)).get_json()
    assert mid_validate["route"] == "model_api"
    assert mid_validate["stage"] == "complete"
    assert mid_validate["passing"] is True

    pre_chat = client.get("/v1/chat/history?limit=20", headers=_headers(api_key))
    assert pre_chat.status_code == 200
    pre_rows = pre_chat.get_json()["messages"]
    assert len(pre_rows) == 1
    assert not any(row["source"] == "history_import" for row in pre_rows)
    assert pre_rows[0]["source"] == "model_api"
    assert pre_rows[0]["role"] == "openclaw"
    assert pre_rows[0]["model_api_kind"] == "onboarding_greeting"

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/chat/history":
            return {
                "messages": [
                    {
                        "role": "openclaw",
                        "content": "I can answer from the imported history now.",
                        "source": "model_api",
                    },
                ],
                "context_memories": [
                    {"title": "User preference", "description": "User prefers direct answers."},
                ],
            }, ""
        if path == "/v1/identity/get":
            return {"identity": _identity_payload()}, ""
        return {}, ""

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_context)

    chat = client.post(
        "/v1/model_api/chat/send",
        json={"message": "Can you reply using my imported history?"},
        headers=_headers(api_key),
    )
    # agent-runner 路径：返回 202 而非 inline 200
    assert chat.status_code == 202, chat.get_data(as_text=True)
    assert chat.get_json()["status"] == "processing"

    final_validate = client.get("/v1/onboarding/validate", headers=_headers(api_key)).get_json()
    assert final_validate["passing"] is True
    assert final_validate["stage"] == "complete"

    history = client.get("/v1/chat/history?limit=20", headers=_headers(api_key))
    assert history.status_code == 200
    rows = history.get_json()["messages"]
    assert not any(row["source"] == "history_import" for row in rows)
    assert any(
        row["source"] == "model_api"
        and row["role"] == "openclaw"
        and row.get("model_api_kind") == "onboarding_greeting"
        for row in rows
    )
    assert any(row["source"] == "model_api" and row["role"] == "user" for row in rows)
    assert all("body_ct" in row for row in rows if row["source"] == "model_api")
    cred_id = db.model_api_credentials_list(user_id)[0]["id"]
    stored_cred = db.model_api_credential_get(user_id, cred_id)
    assert "sk-test-secret" not in json.dumps(stored_cred)


def test_history_import_reuses_inflight_client_job(client, monkeypatch):
    user_id, api_key = _register(client)
    release_provider = threading.Event()
    provider_entered = threading.Event()

    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-test-secret",
    )

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "memory candidate" in joined.lower() or "Memory Garden" in joined:
            provider_entered.set()
            assert release_provider.wait(timeout=2)
            return {
                "reply": (
                    '{"memories":['
                    '{"type":"moment","title":"Inflight moment","description":"The job was reused.","occurred_at":"2026-05-31"},'
                    '{"type":"fact","title":"Inflight fact","description":"Duplicate start did not duplicate work.","occurred_at":"2026-05-31"}'
                    "]}"
                ),
                "usage": {},
            }
        if "Derive a Feedling Identity Card" in joined:
            return {"reply": json.dumps(_identity_payload()), "usage": {}}
        return {"reply": "Ready.", "usage": {}}

    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test-secret"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    payload = {
        "format": "plaintext",
        "content": "2026-05-31 User: Please reuse this import job.",
        "relationship_started_at": "2026-05-31",
        "client_job_id": "test-inflight-reuse",
    }
    first = client.post("/v1/history_import/upload", json=payload, headers=_headers(api_key))
    assert first.status_code == 202, first.get_data(as_text=True)
    first_job = first.get_json()["job"]
    assert provider_entered.wait(timeout=2)

    duplicate = client.post("/v1/history_import/upload", json=payload, headers=_headers(api_key))
    assert duplicate.status_code == 202, duplicate.get_data(as_text=True)
    assert duplicate.get_json()["job"]["job_id"] == first_job["job_id"]

    release_provider.set()
    job = _wait_history_import_job(client, api_key, first_job["job_id"])
    assert job["status"] == "completed"
    assert job["memories_created"] >= 2


def test_model_api_chat_send_accepts_user_image(client, monkeypatch):
    user_id, api_key = _register(client)

    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-test-secret",
    )
    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test-secret"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)
    active = db.model_api_active_route(user_id)
    assert active is not None
    assert db.model_api_route_mark_vision_test(user_id, active["id"], status="ok")

    # 图片 turn 和文本 turn 一样进入 pooled V2 lane。
    chat = client.post(
        "/v1/model_api/chat/send",
        json={
            "message": "What is in this image?",
            "image_mime": "image/jpeg",
            "image_b64": _b64(b"fake-jpeg-bytes"),
        },
        headers=_headers(api_key),
    )
    assert chat.status_code == 202, chat.get_data(as_text=True)
    assert chat.get_json()["status"] == "processing"

    history = client.get("/v1/chat/history?limit=10", headers=_headers(api_key))
    rows = history.get_json()["messages"]
    assert any(row["role"] == "user" and row["content_type"] == "image" for row in rows)


def test_history_import_accepts_json_file_and_persona_profile(client, monkeypatch):
    user_id, api_key = _register(client)

    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-test-secret",
    )

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "memory candidate" in joined.lower() or "Memory Garden" in joined:
            assert "Long-term user profile" in joined
            return {
                "reply": (
                    '{"memories":['
                    '{"type":"moment","title":"JSON import test","description":"User tested JSON export import.","occurred_at":"2026-05-30"},'
                    '{"type":"fact","title":"Persona preference","description":"User likes durable setup context.","occurred_at":"2026-05-30"}'
                    "]}"
                ),
                "usage": {},
            }
        if "Derive a Feedling Identity Card" in joined:
            return {"reply": json.dumps(_identity_payload()), "usage": {}}
        return {"reply": "ok", "usage": {}}

    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test-secret"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    chat_export = {
        "messages": [
            {
                "role": "user",
                "content": "I am testing JSON history import.",
                "created_at": "2026-05-30T08:00:00",
            },
            {
                "role": "assistant",
                "content": [{"text": "I will preserve context."}],
                "created_at": "2026-05-30T08:01:00",
            },
        ]
    }
    upload = client.post(
        "/v1/history_import/upload",
        json={
            "format": "auto",
            "content": json.dumps(chat_export),
            "history_filename": "chat-export.json",
            "persona_content": "Long-term user profile: prefers durable setup context.",
            "persona_filename": "persona.md",
            "client_job_id": "test-json-history-import",
        },
        headers=_headers(api_key),
    )
    assert upload.status_code == 202, upload.get_data(as_text=True)
    job = _wait_history_import_job(client, api_key, upload.get_json()["job"]["job_id"])
    assert job["status"] == "completed"
    assert job["messages_parsed"] == 2
    assert job["support_materials"] == 1
    assert job["history_filename"] == "chat-export.json"
    assert job["persona_filename"] == "persona.md"
    assert job["memories_created"] >= 2
    assert job["identity_written"] is True


def test_wrapped_chat_history_json_parses_without_upload_artifacts():
    chat_export = [
        {
            "mapping": {
                "u1": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["我最近在测试 API onboarding。"]},
                        "create_time": 1780200000.0,
                    }
                },
                "a1": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["我会把导入内容变成可读记忆。"]},
                        "create_time": 1780200060.0,
                    }
                },
                "sys": {
                    "message": {
                        "author": {"role": "system"},
                        "content": {"content_type": "text", "parts": ["internal setup"]},
                        "create_time": 1780200120.0,
                    }
                },
            }
        }
    ]
    wrapped = (
        "===== BEGIN CHAT HISTORY FILE: conversations-011.json =====\n"
        + json.dumps(chat_export, ensure_ascii=False)
        + "\n===== END CHAT HISTORY FILE: conversations-011.json ====="
    )
    warnings = []

    messages = history_import._parse_import_history_content(wrapped, "auto", warnings)

    assert warnings == []
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "API onboarding" in messages[0]["content"]
    assert all("BEGIN CHAT HISTORY FILE" not in m["content"] for m in messages)
    assert all("conversation_id" not in m["content"] for m in messages)

    cards = history_import._fallback_memory_cards(
        messages,
        date(2026, 5, 31),
        story_needed=1,
        about_needed=1,
        language=history_import._detect_import_language(messages),
    )
    assert len(cards) == 2
    assert not cards[0]["title"].startswith("导入")
    assert all("BEGIN CHAT HISTORY FILE" not in card["description"] for card in cards)


def test_large_history_sampling_keeps_middle_and_latest_messages():
    messages = []
    for idx in range(180):
        messages.append({
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": f"message-{idx} " + ("x" * 180),
            "ts": 1_700_000_000 + idx,
            "source": "history_import",
        })
    messages[0]["content"] = "EARLIEST_MARKER " + messages[0]["content"]
    messages[90]["content"] = "MIDDLE_MARKER " + messages[90]["content"]
    messages[-1]["content"] = "LATEST_MARKER " + messages[-1]["content"]

    sample = history_import._transcript_sample(messages, max_chars=5000)

    assert "EARLIEST_MARKER" in sample
    assert "MIDDLE_MARKER" in sample
    assert "LATEST_MARKER" in sample


def test_import_memory_targets_do_not_force_historical_floor_padding():
    targets = history_import._import_memory_targets(
        [{"role": "user", "content": f"m{i}", "source": "history_import"} for i in range(120)],
        [],
    )

    assert targets["tier"] == "small"
    assert targets["story"] == 4
    assert targets["about_me"] == 8


def test_history_import_profile_marks_three_year_history_as_ultra():
    start = 1_600_000_000
    messages = [
        {
            "role": "user",
            "content": "long relationship marker",
            "source": "history_import",
            "ts": start + idx * 90 * 24 * 3600,
        }
        for idx in range(14)
    ]

    profile = history_import._history_import_profile(messages, [], content_chars=80_000)
    targets = history_import._import_memory_targets(
        messages,
        [],
        profile,
    )

    assert profile["tier"] == "ultra"
    assert targets["total"] == 120
    assert targets["chat_ready_cards"] == 20
    assert targets["background"] is True


def test_import_memory_filters_generic_import_cards_and_repetitive_low_value_content():
    cards = history_import._dedupe_memory_cards([
        {
            "type": "moment",
            "title": "导入片段 7",
            "description": "Please explain what this general concept means",
            "occurred_at": "2026-06-01",
        },
        {
            "type": "fact",
            "title": "Project preference",
            "description": "User repeatedly cares that long-term memory is written as readable human meaning rather than raw archive fragments.",
            "occurred_at": "2026-06-01",
        },
        {
            "type": "event",
            "title": "Memory writing preference",
            "description": "User repeatedly cares that long-term memory is written as readable human meaning rather than raw archive fragments.",
            "occurred_at": "2026-06-01",
        },
    ])

    assert len(cards) == 1
    assert cards[0]["title"] == "Project preference"


def test_candidate_pipeline_renders_high_value_cards_without_generic_tasks():
    raw = {
        "candidates": [
            {
                "candidate_type": "user_fact",
                "subject": "user",
                "title": "Generic question",
                "summary": "How do I explain this generic concept?",
                "confidence": 0.9,
            },
            {
                "candidate_type": "boundary",
                "subject": "user",
                "title": "Memory boundary",
                "summary": "User wants imported memory to preserve durable relationship meaning and not raw JSON or generic task answers.",
                "importance_signals": ["relationship_boundary", "future_utility"],
                "confidence": 0.9,
                "evidence_quotes": ["memory must be readable human meaning"],
            },
            {
                "candidate_type": "relationship_event",
                "subject": "relationship",
                "title": "API onboarding review",
                "summary": "User reviewed API onboarding quality and asked for memory distillation instead of direct archive dumping.",
                "importance_signals": ["explicit_memory"],
                "confidence": 0.85,
            },
        ]
    }

    candidates = history_import._coerce_import_candidates(raw, date(2026, 6, 1), window_id="w1")
    cards = history_import._render_candidates_to_memory_cards(
        candidates,
        date(2026, 6, 1),
        {"story": 2, "about_me": 2, "ta_thinking": 0, "total": 4},
        language="en",
    )

    assert len(candidates) == 2
    assert any("raw JSON" in card["content"] for card in cards)
    assert any("API onboarding" in card["content"] for card in cards)
    assert all("generic concept" not in card["content"] for card in cards)


def test_identity_import_keeps_unknown_agent_name_empty():
    payload = _identity_payload()
    payload["agent_name"] = "IO"

    normalized = history_import._normalize_identity_payload(payload, [], 7, "zh-Hans")

    assert normalized["agent_name"] == ""

    payload["agent_name"] = "小哆啦"
    normalized = history_import._normalize_identity_payload(payload, [], 7, "zh-Hans")

    assert normalized["agent_name"] == "小哆啦"


def test_candidate_render_merges_similar_cards_filters_sensitive_claims_and_sorts_newest_first():
    raw = {
        "candidates": [
            {
                "candidate_type": "user_fact",
                "subject": "user",
                "title": "User real name",
                "summary": "User's real name is Sven.",
                "first_seen_at": "2026-05-01",
                "confidence": 0.95,
            },
            {
                "candidate_type": "preference",
                "subject": "user",
                "title": "Direct feedback",
                "summary": "User repeatedly prefers direct feedback and clear engineering tradeoffs.",
                "importance_signals": ["repeated"],
                "first_seen_at": "2026-05-03",
                "confidence": 0.9,
            },
            {
                "candidate_type": "user_fact",
                "subject": "user",
                "title": "Feedback style",
                "summary": "User prefers direct feedback and clear engineering tradeoffs when reviewing product quality.",
                "importance_signals": ["repeated"],
                "first_seen_at": "2026-05-04",
                "confidence": 0.88,
            },
            {
                "candidate_type": "relationship_event",
                "subject": "relationship",
                "title": "Late review",
                "summary": "User reviewed the imported memory result and corrected the system toward readable memory.",
                "importance_signals": ["explicit_memory"],
                "first_seen_at": "2026-05-05",
                "confidence": 0.85,
            },
        ]
    }

    candidates = history_import._coerce_import_candidates(raw, date(2026, 5, 1), window_id="w1")
    cards = history_import._render_candidates_to_memory_cards(
        candidates,
        date(2026, 5, 1),
        {"story": 2, "about_me": 4, "ta_thinking": 0, "total": 6},
        language="en",
    )

    assert all("real name" not in card["content"].lower() for card in cards)
    assert sum("direct feedback" in card["content"] for card in cards) == 1
    assert [card["occurred_at"] for card in cards] == sorted([card["occurred_at"] for card in cards], reverse=True)


def test_candidate_extraction_repairs_malformed_provider_json(monkeypatch):
    calls = []

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        calls.append(joined)
        if "previous model response was not valid json" in joined.lower():
            return {
                "reply": json.dumps({
                    "candidates": [{
                        "candidate_type": "preference",
                        "subject": "user",
                        "title": "Readable memory",
                        "summary": "User wants imported history distilled into readable durable memory.",
                        "importance_signals": ["explicit_memory"],
                        "first_seen_at": "2026-06-01",
                        "confidence": 0.9,
                    }]
                }),
                "usage": {},
            }
        return {"reply": "Readable memory is important, but this is not JSON.", "usage": {}}

    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    candidates, warnings = history_import._extract_memory_candidates_with_provider(
        provider_client.ProviderConfig("openai", "gpt-4.1-mini", "sk-test"),
        [{"id": "w1", "text": "2026-06-01 User: Please turn this into readable memory."}],
        date(2026, 6, 1),
        per_window_target=3,
        language="en",
    )

    assert len(candidates) == 1
    assert candidates[0]["title"] == "Readable memory"
    assert any("provider_candidate_json_repaired_window_1" in warning for warning in warnings)


def test_onboarding_greeting_for_unknown_name_asks_for_name(monkeypatch):
    captured = {}

    def fake_chat_completion(cfg, messages, **kwargs):
        captured["prompt"] = "\n".join(str(m.get("content") or "") for m in messages)
        return {"reply": "我先把能读懂的部分记下来了。现在我还没有名字，你想怎么叫我？", "usage": {}}

    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    text, warnings = history_import._generate_model_api_onboarding_greeting(
        provider_client.ProviderConfig("openai", "gpt-4.1-mini", "sk-test"),
        [{"role": "user", "content": "这是之前的聊天。", "source": "history_import"}],
        [],
        {"agent_name": "", "self_introduction": ""},
        10,
        "zh-Hans",
    )

    assert warnings == []
    assert "还没有名字" in captured["prompt"]
    assert "你想怎么叫我" in text


def test_onboarding_greeting_uses_reliable_provider_call_with_extended_timeout(monkeypatch):
    captured = {}

    def raw_chat_completion(*args, **kwargs):
        raise AssertionError("onboarding greeting should use reliable_chat_completion")

    def fake_reliable_chat_completion(cfg, messages, **kwargs):
        captured["kwargs"] = kwargs
        return {"reply": "I have a first memory ready. What would you like to call me?", "usage": {}}

    monkeypatch.setattr(provider_client, "chat_completion", raw_chat_completion)
    monkeypatch.setattr(provider_client, "reliable_chat_completion", fake_reliable_chat_completion)

    text, warnings = history_import._generate_model_api_onboarding_greeting(
        provider_client.ProviderConfig("openai", "gpt-4.1-mini", "sk-test"),
        [{"role": "user", "content": "This is prior chat.", "source": "history_import"}],
        [],
        {"agent_name": "", "self_introduction": ""},
        10,
        "en",
    )

    assert warnings == []
    assert text.startswith("I have a first memory ready")
    assert captured["kwargs"]["max_tokens"] == 320
    assert captured["kwargs"]["timeout"] == history_import.GENESIS_PROVIDER_DERIVE_TIMEOUT_SEC == 120.0


def test_onboarding_greeting_append_marks_introduced(client):
    # The onboarding greeting IS this user's introduction: a successful append
    # must set the durable introduced marker so the resident introduction job
    # (agent_runtime.introduction) can never double-greet — e.g. after a later
    # route switch to resident, or a fast-path widened to model_api.
    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)
    assert store.introduction_done() is False
    row = history_import._append_model_api_onboarding_greeting(store, "hello, first greeting")
    assert row["model_api_kind"] == "onboarding_greeting"
    assert store.introduction_done() is True


def test_onboarding_greeting_append_is_idempotent_across_retries(client):
    # Genesis job retries (crash between greeting append and job-complete, then
    # the app re-POSTs the same client_job_id and the job re-runs) must not
    # append a second greeting: the check is DB-level, keyed on the existing
    # onboarding_greeting row, not on the in-memory ring or introduced_at.
    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)
    first = history_import._append_model_api_onboarding_greeting(store, "hello one")
    second = history_import._append_model_api_onboarding_greeting(store, "hello two")
    assert second["id"] == first["id"]
    rows = [m for m in db.chat_load(user_id)
            if isinstance(m, dict) and m.get("model_api_kind") == "onboarding_greeting"]
    assert len(rows) == 1


def test_onboarding_greeting_concurrent_appends_yield_one_row(client):
    # Two genesis jobs for the same user (different client_job_ids; the active
    # lock is (user_id, job_id)-grained) can race the greeting append. The
    # stable msg_id upsert key must collapse them to ONE persisted row against
    # the real database, whichever thread wins.
    import threading as _threading

    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)
    results: list = [None, None]

    def _run(i):
        try:
            results[i] = history_import._append_model_api_onboarding_greeting(store, f"hello {i}")
        except Exception as e:  # noqa: BLE001
            results[i] = e

    threads = [_threading.Thread(target=_run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(isinstance(r, dict) for r in results), results
    # First-writer-wins: BOTH callers must return the identical winner doc
    # (same ciphertext), not merely the same id — and the DB row IS that doc.
    assert results[0]["id"] == results[1]["id"]
    assert results[0]["body_ct"] == results[1]["body_ct"]
    assert results[0]["nonce"] == results[1]["nonce"]
    rows = [m for m in db.chat_load(user_id)
            if isinstance(m, dict) and m.get("model_api_kind") == "onboarding_greeting"]
    assert len(rows) == 1
    assert rows[0]["body_ct"] == results[0]["body_ct"]
    assert rows[0]["nonce"] == results[0]["nonce"]
    assert store.introduction_done() is True


def test_onboarding_greeting_cross_process_insert_is_first_writer_wins(client):
    # Cross-process: two workers race the greeting DB primitive concurrently on
    # separate pool connections with DIFFERENT envelopes (different ciphertext
    # and ts). Exactly one may insert; both must get back the identical winner
    # doc; the winner's ciphertext must never be rewritten (a same-PK rewrite
    # could also slip behind the TEE replicator's (ts, msg_id) forward cursor).
    import threading as _threading

    user_id, _api_key = _register(client)
    msg_id = history_import._onboarding_greeting_msg_id(user_id)
    doc_a = {"id": msg_id, "role": "openclaw", "source": "model_api",
             "model_api_kind": "onboarding_greeting", "ts": 2.0,
             "body_ct": "cipher-A", "nonce": "nonce-A"}
    doc_b = {"id": msg_id, "role": "openclaw", "source": "model_api",
             "model_api_kind": "onboarding_greeting", "ts": 1.0,
             "body_ct": "cipher-B", "nonce": "nonce-B"}
    outcomes: list = [None, None]

    def _run(i, doc):
        outcomes[i] = db.chat_insert_onboarding_greeting_once(user_id, msg_id, doc["ts"], doc)

    threads = [_threading.Thread(target=_run, args=(0, doc_a)),
               _threading.Thread(target=_run, args=(1, doc_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    (win_a, ins_a), (win_b, ins_b) = outcomes
    assert sorted([ins_a, ins_b]) == [False, True]               # exactly one inserter
    assert win_a == win_b                                        # identical winner doc, not just id
    rows = [m for m in db.chat_load(user_id)
            if isinstance(m, dict) and m.get("model_api_kind") == "onboarding_greeting"]
    assert len(rows) == 1
    assert rows[0]["body_ct"] == win_a["body_ct"]                # DB row == returned winner
    assert rows[0]["nonce"] == win_a["nonce"]


def test_onboarding_greeting_lookup_failure_propagates(client, monkeypatch):
    # "Could not look" must never be treated as "absent": a transient DB read
    # failure at the precheck would otherwise bypass an existing greeting and
    # insert a duplicate. The raising lookup must propagate and nothing may be
    # appended or marked.
    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)

    def _boom(_user_id):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(db, "chat_onboarding_greeting_row", _boom)
    monkeypatch.setattr(store, "append_chat",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not append on lookup failure")))
    with pytest.raises(RuntimeError):
        history_import._append_model_api_onboarding_greeting(store, "never appended")
    assert store.introduction_done() is False


def test_onboarding_greeting_wake_notify_fires_for_inserter_only(client, monkeypatch):
    # The cross-worker wake must actually fire for the inserter (a broken
    # import here was once swallowed silently — other workers' long-polls
    # never woke), and must NOT fire for a loser/idempotent re-call that adds
    # no new content.
    from core import wake_bus as core_wake_bus

    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)
    notified: list = []
    monkeypatch.setattr(core_wake_bus, "notify",
                        lambda kind, *args: notified.append((kind,) + args))

    history_import._append_model_api_onboarding_greeting(store, "hello")
    assert notified == [("chat", user_id)]

    history_import._append_model_api_onboarding_greeting(store, "retry")
    assert notified == [("chat", user_id)]


def test_onboarding_greeting_insert_failure_does_not_mark(client, monkeypatch):
    # The greeting write path must never mark introduced when the row did not
    # durably land: the insert primitive RAISES on DB failure (unlike
    # chat_append's swallow) and the exception must propagate un-marked —
    # a marker without a persisted greeting would suppress every future
    # recovery.
    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)

    def _boom(*_a, **_k):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(db, "chat_insert_onboarding_greeting_once", _boom)
    with pytest.raises(RuntimeError):
        history_import._append_model_api_onboarding_greeting(store, "never durable")
    assert store.introduction_done() is False


def test_onboarding_greeting_existing_row_heals_missing_marker(client):
    # Crash window: greeting row persisted but the marker write was lost. A
    # retry must reuse the existing row AND backfill introduced_at.
    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)
    first = history_import._append_model_api_onboarding_greeting(store, "hello")
    cur = store.load_proactive_settings()
    cur["introduced_at"] = ""
    db.set_blob(user_id, "proactive_settings", cur)
    assert store.introduction_done() is False
    retry = history_import._append_model_api_onboarding_greeting(store, "retry text")
    assert retry["id"] == first["id"]
    assert store.introduction_done() is True


def test_onboarding_greeting_envelope_failure_leaves_introduced_unset(client, monkeypatch):
    # Marker must anchor on the greeting the user actually RECEIVES: an append
    # that failed (no chat row) must not mark introduced, or the failure would
    # be permanently papered over and suppress a later introduction.
    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)
    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _body, **_kwargs: (None, "forced_envelope_failure"),
    )
    with pytest.raises(RuntimeError):
        history_import._append_model_api_onboarding_greeting(store, "never lands")
    assert store.introduction_done() is False


def test_support_material_sections_split_character_and_personal_profile():
    payload = {
        "persona_filename": "combined.md",
        "persona_content": """
===== BEGIN ORIGINAL SYSTEM PROMPT: system.md =====
你是小哆啦，说话要保持原本的猫猫语气，不要改成人设。
===== END ORIGINAL SYSTEM PROMPT: system.md =====

===== BEGIN CHARACTER CARD =====
小哆啦是一个稳定、细心、会记得小事的陪伴型 AI。
===== END CHARACTER CARD =====

===== BEGIN PERSONAL PROFILE CARD: profile.md =====
用户喜欢直接的反馈，也希望记忆写得像人能读懂的话。
===== END PERSONAL PROFILE CARD: profile.md =====
""",
    }

    support = history_import._persona_support_messages(payload)

    assert [m["source"] for m in support] == ["ai_persona_import", "ai_persona_import", "user_profile_import"]
    assert [m["source_detail"] for m in support] == ["ai_persona_import", "ai_persona_import", "user_profile_import"]
    assert "AI Persona material (system.md)" in support[0]["content"]
    assert "猫猫语气" in support[0]["content"]
    assert "小哆啦" in support[1]["content"]
    assert "用户喜欢直接的反馈" in support[2]["content"]
    assert all("BEGIN " not in m["content"] and "END " not in m["content"] for m in support)


def test_support_materials_accept_explicit_agent_character_and_personal_profile_fields():
    payload = {
        "agent_prompt_content": "你是小哆啦，保持用户已经习惯的语气和边界。",
        "agent_prompt_filename": "system.md",
        "character_content": "小哆啦是一个稳定、细心、会记得小事的陪伴型 AI。",
        "character_filename": "character.md",
        "personal_profile_content": "用户喜欢直接的反馈，也希望记忆写得像人能读懂的话。",
        "personal_profile_filename": "profile.md",
    }

    support = history_import._persona_support_messages(payload)

    assert [m["source"] for m in support] == ["ai_persona_import", "ai_persona_import", "user_profile_import"]
    assert [m["source_detail"] for m in support] == ["agent_prompt_import", "character_import", "user_profile_import"]
    assert "AI Persona material (system.md)" in support[0]["content"]
    assert "AI Persona material (character.md)" in support[1]["content"]
    assert "User profile (profile.md)" in support[2]["content"]
    assert "已经习惯的语气" in support[0]["content"]
    assert "小哆啦" in support[1]["content"]
    assert "用户喜欢直接的反馈" in support[2]["content"]


def test_support_materials_accept_memory_summary_as_first_class_source():
    payload = {
        "ai_persona_content": "TA 叫小哆啦，语气稳定。",
        "ai_persona_filename": "persona.txt",
        "memory_summary_content": "1. 用户在五月反复提到需要稳定陪伴。\n2. 他们约定重要提醒要直接说。",
        "memory_summary_filename": "memory.txt",
        "personal_profile_content": "用户喜欢直接反馈。",
        "personal_profile_filename": "profile.txt",
    }

    support = history_import._persona_support_messages(payload)

    assert [m["source"] for m in support] == [
        "ai_persona_import",
        "user_profile_import",
        "memory_summary_import",
    ]
    assert "AI Persona material (persona.txt)" in support[0]["content"]
    assert "User profile (profile.txt)" in support[1]["content"]
    assert "Memory summary (memory.txt)" in support[2]["content"]


def test_history_import_windows_keep_memory_summary_separate_from_large_history():
    payload = {
        "ai_persona_content": "TA 叫小哆啦，语气稳定。",
        "memory_summary_content": "用户在五月反复提到需要稳定陪伴。\n他们约定重要提醒要直接说。",
    }
    support = history_import._persona_support_messages(payload)
    history = [
        {"role": "user", "content": f"history line {idx}", "source": "history_import"}
        for idx in range(240)
    ]

    windows = history_import._build_transcript_windows(
        support + history,
        max_chars=2500,
        max_windows=4,
    )

    assert any(w.get("source_families") == ["memory_summary_import"] for w in windows)
    assert any(w.get("source_families") == ["ai_persona_import"] for w in windows)
    assert any(w.get("source_families") == ["history_import"] for w in windows)
    assert any("用户在五月反复提到需要稳定陪伴" in w["text"] for w in windows)


def test_memory_summary_fallback_splits_high_recall_cards_without_ai_persona_story_pollution():
    messages = history_import._persona_support_messages({
        "ai_persona_content": "TA 叫小哆啦，温柔稳定。",
        "memory_summary_content": "1. 用户在五月反复提到需要稳定陪伴。\n2. 用户希望重要提醒要直接说。\n3. 他们在一次争执后约定先确认情绪。",
        "personal_profile_content": "用户喜欢直接反馈。",
    })

    cards = history_import._fallback_memory_cards(
        messages,
        date(2026, 5, 1),
        story_needed=2,
        about_needed=2,
        language="zh-Hans",
    )

    assert len(cards) >= 4
    assert not any("温柔稳定" in c["description"] and c["type"] in {"moment", "quote"} for c in cards)
    assert any("稳定陪伴" in c["description"] for c in cards)
    assert any("直接" in c["description"] for c in cards)
    assert all("用户" not in c["description"] for c in cards)


def test_identity_without_ai_source_does_not_use_user_profile_as_companion(monkeypatch):
    def fake_chat_completion(cfg, messages, **kwargs):
        return {
            "reply": json.dumps({
                "agent_name": "Seven",
                "self_introduction": "我是 Seven，我喜欢直接反馈，也在做 Feedling。",
                "category": "用户画像",
                "signature": ["直接反馈", "做产品"],
                "dimensions": [
                    {"name": f"维度{i}", "value": 50, "description": "来自用户档案。"}
                    for i in range(7)
                ],
            }, ensure_ascii=False),
            "usage": {},
        }

    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    identity, warnings = history_import._derive_identity_with_provider(
        provider_client.ProviderConfig("openai", "gpt-4.1-mini", "sk-test"),
        [{"role": "user", "content": "User profile:\n用户叫 Seven，喜欢直接反馈。", "source": "user_profile_import"}],
        [],
        3,
        "zh-Hans",
    )

    assert identity["agent_name"] == ""
    assert "Seven" not in identity["self_introduction"]
    assert "identity_guard_no_ai_source_used_generic_identity" in warnings


def test_identity_deriver_uses_reliable_provider_call_with_extended_timeout(monkeypatch):
    captured = {}

    def raw_chat_completion(*args, **kwargs):
        raise AssertionError("identity derivation should use reliable_chat_completion")

    def fake_reliable_chat_completion(cfg, messages, **kwargs):
        captured["kwargs"] = kwargs
        return {
            "reply": json.dumps({
                "agent_name": "Mira",
                "self_introduction": "I remember the small details and speak directly.",
                "category": "Steady companion",
                "signature": ["Keeps context", "Speaks plainly"],
                "dimensions": [
                    {"name": "Steady", "value": 82, "description": "The persona says Mira stays steady."}
                ],
            }),
            "usage": {},
        }

    monkeypatch.setattr(provider_client, "chat_completion", raw_chat_completion)
    monkeypatch.setattr(provider_client, "reliable_chat_completion", fake_reliable_chat_completion)

    identity, warnings = history_import._derive_identity_with_provider(
        provider_client.ProviderConfig("openai", "gpt-4.1-mini", "sk-test"),
        [{"role": "user", "content": "AI persona: IO is steady and direct.", "source": "ai_persona_import"}],
        [],
        3,
        "en",
    )

    assert warnings == []
    assert identity["agent_name"] == "Mira"
    assert captured["kwargs"]["max_tokens"] == 1800
    assert captured["kwargs"]["timeout"] == history_import.GENESIS_PROVIDER_DERIVE_TIMEOUT_SEC == 120.0


def test_support_materials_extract_chatgpt_memories_json_without_raw_artifacts():
    payload = {
        "personal_profile_filename": "memories.json",
        "personal_profile_content": json.dumps([
            {
                "conversations_memory": "**工作上下文**\nSeven 正在做 Feedling MCP 和 API onboarding。",
                "account_uuid": "user-secret-id",
            }
        ]),
    }

    support = history_import._persona_support_messages(payload)

    assert len(support) == 1
    content = support[0]["content"]
    assert "工作上下文" in content
    assert "Feedling MCP" in content
    assert "conversations_memory" not in content
    assert "account_uuid" not in content
    assert "[{" not in content


def test_support_materials_ignore_account_metadata_json():
    payload = {
        "personal_profile_filename": "users.json",
        "personal_profile_content": json.dumps([
            {
                "uuid": "user-secret-id",
                "email_address": "seven@example.com",
                "verified_phone_number": "+10000000000",
                "full_name": "Seven",
            }
        ]),
    }

    assert history_import._persona_support_messages(payload) == []


def test_support_materials_keep_memory_archive_items_with_bare_id():
    # Regression: a long-term-memory archive legitimately carries a per-item `id`
    # (e.g. "m0001"), and its narrative field is often NOT one of the whitelisted
    # content keys (here `记忆`). A bare `id` must NOT trip the account-metadata
    # skip — otherwise every item is dropped, the whole upload normalizes to empty,
    # and _prepare_plaintext_import raises `..._required` → HTTP 400 for the user.
    payload = {
        "memory_summary_filename": "Elio 的长期记忆.json",
        "memory_summary_content": json.dumps([
            {
                "id": "m0001",
                "date": "2026-05-29",
                "source": "Elio_self_written",
                "type": "经历",
                "tags": ["日记", "和好"],
                "记忆": "Neve 的第一个通宵失眠夜，整夜反复推开窗口，我在，她不解释。",
            }
        ], ensure_ascii=False),
    }

    support = history_import._persona_support_messages(payload)

    assert len(support) == 1
    assert "通宵失眠夜" in support[0]["content"]


def test_support_materials_still_ignore_account_metadata_with_id():
    # Guardrail intact: an account-export blob (strong PII: uuid/email/phone) with
    # NO real content is still dropped, even though it also carries an `id`.
    payload = {
        "personal_profile_filename": "users.json",
        "personal_profile_content": json.dumps([
            {
                "id": "user-secret-id",
                "uuid": "user-secret-id",
                "email_address": "seven@example.com",
                "verified_phone_number": "+10000000000",
                "full_name": "Seven",
            }
        ]),
    }

    assert history_import._persona_support_messages(payload) == []


def test_support_materials_read_support_material_content_alias():
    # A client that sends only the `support_material_content` alias (no
    # `memory_summary_content`) must not silently drop the material.
    payload = {
        "support_material_filename": "memory.txt",
        "support_material_content": "Neve 喜欢在深夜写日记，Elio 会安静陪着。",
    }

    support = history_import._persona_support_messages(payload)

    assert len(support) == 1
    assert "深夜写日记" in support[0]["content"]


def test_import_language_prefers_user_archive_language(monkeypatch):
    monkeypatch.setattr(accounts_registry, "_get_user_archive_language", lambda user_id: "zh-Hans-US")
    store = type("Store", (), {"user_id": "usr_test"})()

    language = history_import._import_language_for_store(
        store,
        [{"role": "user", "content": "Work context and product strategy are written in English."}],
    )

    assert language == "zh-Hans-US"


def test_history_import_allows_confirmed_fresh_start_without_materials(client, monkeypatch):
    user_id, api_key = _register(client)

    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-test-secret",
    )

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        assert "Fresh start" in joined
        if "memory candidate" in joined.lower() or "Memory Garden" in joined:
            return {
                "reply": (
                    '{"memories":['
                    '{"type":"moment","title":"Fresh start","description":"User started without imported material.","occurred_at":"2026-06-01"},'
                    '{"type":"fact","title":"Blank setup","description":"No prior material was provided.","occurred_at":"2026-06-01"}'
                    "]}"
                ),
                "usage": {},
            }
        if "Derive a Feedling Identity Card" in joined:
            return {"reply": json.dumps(_identity_payload()), "usage": {}}
        return {"reply": "ok", "usage": {}}

    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test-secret"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    upload = client.post(
        "/v1/history_import/upload",
        json={
            "format": "auto",
            "content": "",
            "fresh_start": True,
            "client_job_id": "test-fresh-start-import",
        },
        headers=_headers(api_key),
    )
    assert upload.status_code == 202, upload.get_data(as_text=True)
    job = _wait_history_import_job(client, api_key, upload.get_json()["job"]["job_id"])
    assert job["status"] == "completed"
    assert job["messages_parsed"] == 0
    assert job["support_materials"] == 1
    assert "fresh_start_without_support_material" in job["warnings"]
    assert job["identity_written"] is True


def test_chat_history_hides_verify_reply_but_keeps_ping(client):
    """The visible /v1/chat/history feed must hide the verify-loop liveness
    REPLY (agent/openclaw, source='verify_ping') so a reply that outlives
    verify_loop's GC window can never leak as a stray visible message (e.g.
    '__verify_ack__').

    But the verify PING itself (user-role, source='verify_ping') must REMAIN in
    this route's output: the enclave decrypt proxy reuses /v1/chat/history to
    deliver the ping to the resident consumer (which detects it by source).
    Dropping it here would starve enclave-backed consumers and wedge verify_loop
    (regression guard for the enclave-poll path)."""
    import uuid

    from core import store as core_store

    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)

    def _env(body: str) -> dict:
        return {
            "v": 1,
            "id": uuid.uuid4().hex,
            "body_ct": _b64(body.encode("utf-8")),
            "nonce": _b64(b"\x00" * 12),
            "K_user": _b64(b"\x00" * 32),
            "visibility": "local_only",
            "owner_user_id": user_id,
        }

    # A normal user message, a synthetic ping, AND a leaked liveness reply.
    real = store.append_chat("user", "chat", _env("hello"))
    ping = store.append_chat("user", "verify_ping", _env("__VERIFY_PING__:abc"))
    reply = store.append_chat("openclaw", "verify_ping", _env("__verify_ack__"))

    res = client.get("/v1/chat/history?limit=50", headers=_headers(api_key))
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()

    ids = [m.get("id") for m in body["messages"]]
    # The liveness reply is hidden from the visible feed...
    assert reply["id"] not in ids, f"verify_ping reply leaked into history: {ids}"
    # ...but the ping survives so the enclave consumer can still receive it.
    assert ping["id"] in ids, "verify_ping PING must stay for enclave-backed pollers"
    assert real["id"] in ids, "the real user message must still appear"
    # total reflects the visible feed: real + ping (reply filtered out)
    assert body["total"] == 2, body


def test_chat_history_hides_stale_verify_ping(client):
    """A verify PING that outlived its verify_loop (GC skipped by a mid-run
    SIGTERM) must NOT linger in the visible feed as a '__VERIFY_PING__:...'
    bubble. Fresh pings stay (enclave consumer needs them); stale ones drop."""
    import time
    import uuid

    from chat import chat_core
    from core import store as core_store

    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)

    def _env(body: str) -> dict:
        return {
            "v": 1, "id": uuid.uuid4().hex, "body_ct": _b64(body.encode("utf-8")),
            "nonce": _b64(b"\x00" * 12), "K_user": _b64(b"\x00" * 32),
            "visibility": "local_only", "owner_user_id": user_id,
        }

    real = store.append_chat("user", "chat", _env("hello"))
    fresh = store.append_chat("user", "verify_ping", _env("__VERIFY_PING__:fresh"))
    stale = store.append_chat("user", "verify_ping", _env("__VERIFY_PING__:stale"))
    # Backdate the stale ping past the visible TTL (verify_loop long dead).
    stale_ts = time.time() - (chat_core.VERIFY_PING_VISIBLE_TTL_SEC + 60)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE chat_messages SET ts=%s, "
            "doc=jsonb_set(doc, '{ts}', to_jsonb(%s::double precision), true) "
            "WHERE user_id=%s AND msg_id=%s",
            (stale_ts, stale_ts, user_id, stale["id"]),
        )

    res = client.get("/v1/chat/history?limit=50", headers=_headers(api_key))
    assert res.status_code == 200, res.get_data(as_text=True)
    ids = [m.get("id") for m in res.get_json()["messages"]]

    assert real["id"] in ids
    assert fresh["id"] in ids, "a FRESH ping must still reach enclave-backed consumers"
    assert stale["id"] not in ids, f"stale verify_ping leaked into history: {ids}"


def test_message_body_refuses_verify_ping(client):
    """Fetching a verify_ping row by id must 404 — it is never legitimate user
    content, so a leaked ping id can't be re-fetched out-of-band."""
    import uuid

    from core import store as core_store

    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)
    ping = store.append_chat("user", "verify_ping", {
        "v": 1, "id": uuid.uuid4().hex, "body_ct": _b64(b"__VERIFY_PING__:x"),
        "nonce": _b64(b"\x00" * 12), "K_user": _b64(b"\x00" * 32),
        "visibility": "local_only", "owner_user_id": user_id,
    })
    res = client.get(f"/v1/chat/messages/{ping['id']}/body", headers=_headers(api_key))
    assert res.status_code == 404, res.get_data(as_text=True)


def test_export_excludes_verify_ping(client):
    """Data export must not carry synthetic verify-loop rows."""
    import uuid

    from content import content_core
    from core import store as core_store

    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)

    def _env(body: str) -> dict:
        return {
            "v": 1, "id": uuid.uuid4().hex, "body_ct": _b64(body.encode()),
            "nonce": _b64(b"\x00" * 12), "K_user": _b64(b"\x00" * 32),
            "visibility": "local_only", "owner_user_id": user_id,
        }

    real = store.append_chat("user", "chat", _env("hello"))
    ping = store.append_chat("user", "verify_ping", _env("__VERIFY_PING__:x"))
    reply = store.append_chat("openclaw", "verify_ping", _env("__verify_ack__"))

    import json as _json
    result = content_core.export_data(store)
    export = _json.loads(result.raw_body)
    ids = {m.get("id") for m in export.get("chat", [])}
    assert real["id"] in ids
    assert ping["id"] not in ids, "verify_ping PING leaked into export"
    assert reply["id"] not in ids, "verify_ping REPLY leaked into export"


def test_verify_ping_reply_never_delivers_push(client):
    """Defense-in-depth: a verify_ping reply must not deliver push / Live Activity
    even if the caller supplies a body (the consumer already sends suppress_push).

    write_response returns only {id, ts, v}, so we observe the real safety
    property by mocking the delivery function and asserting it is never called."""
    import uuid
    from unittest.mock import patch

    from chat import chat_core
    from core import store as core_store
    from push import service as push_service

    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)
    payload = {
        "envelope": {
            "v": 1, "id": uuid.uuid4().hex, "body_ct": _b64(b"__verify_ack__"),
            "nonce": _b64(b"\x00" * 12), "K_user": _b64(b"\x00" * 32),
            "visibility": "local_only", "owner_user_id": user_id,
        },
        "source": "verify_ping",
        "push_body": "should never surface",
        "push_live_activity": True,
    }
    with patch.object(
        push_service, "_deliver_ai_message_push_if_background", return_value={}
    ) as deliver:
        body, status = chat_core.write_response(
            store, payload, consumer_id="c", consumer_info={}, allow_verify_reply=True,
        )
    assert status in (200, 201), body
    deliver.assert_not_called()


def _verify_reply_envelope(user_id: str) -> dict:
    import uuid

    return {
        "v": 1,
        "id": uuid.uuid4().hex,
        "body_ct": _b64(b"__verify_ack__"),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x00" * 32),
        "visibility": "local_only",
        "owner_user_id": user_id,
    }


def test_chat_response_rejects_verify_ping_source_without_pending_ping(client, monkeypatch):
    """Because source=='verify_ping' rows are scrubbed from the visible feed, a
    reply that (mis)uses this source without an outstanding probe would silently
    vanish from the transcript. The route must reject it (409) unless an actual
    verify ping is pending. Bootstrap gate is stubbed open so we isolate the
    new source gate (a fresh user would otherwise 409 on bootstrap first)."""
    from bootstrap import gates as boot_gates

    user_id, api_key = _register(client)
    monkeypatch.setattr(
        boot_gates, "_gate_bootstrap_for_chat",
        lambda store, allow_verify_reply=False: None,
    )
    # No pending verify ping in the store.
    res = client.post(
        "/v1/chat/response",
        json={"envelope": _verify_reply_envelope(user_id), "source": "verify_ping"},
        headers=_headers(api_key),
    )
    assert res.status_code == 409, res.get_data(as_text=True)
    assert "pending verify ping" in res.get_json().get("error", "")


def test_chat_response_accepts_verify_ping_reply_to_pending_ping(client, monkeypatch):
    """The legitimate path: when a verify ping is outstanding, the resident
    consumer's source='verify_ping' liveness reply satisfies the new source gate
    and is accepted. Bootstrap gate is stubbed open to isolate the source gate
    (in production allow_verify_reply also bypasses it at the main_loop stage)."""
    from bootstrap import gates as boot_gates
    from core import store as core_store

    user_id, api_key = _register(client)
    monkeypatch.setattr(
        boot_gates, "_gate_bootstrap_for_chat",
        lambda store, allow_verify_reply=False: None,
    )
    store = core_store.get_store(user_id)
    # An outstanding synthetic ping with no reply after it → pending.
    store.append_chat(
        "user", "verify_ping",
        {
            "v": 1, "id": "ping_pending_01",
            "body_ct": _b64(b"__VERIFY_PING__:x"), "nonce": _b64(b"\x00" * 12),
            "K_user": _b64(b"\x00" * 32), "visibility": "local_only",
            "owner_user_id": user_id,
        },
    )
    res = client.post(
        "/v1/chat/response",
        json={
            "envelope": _verify_reply_envelope(user_id),
            "source": "verify_ping",
            # This branch's gate is the consumer's strict contract: the ack must
            # bind to THIS ping (source ∧ reply_to_message_id) — see the resident
            # consumer's verify-ack sender, which always sets reply_to.
            "reply_to_message_id": "ping_pending_01",
        },
        headers=_headers(api_key),
    )
    assert res.status_code == 200, res.get_data(as_text=True)


# ---------------------------------------------------------------------------
# base_url 指向的不是 API 端点(用户报障 2026-08-09:两个中转站都提示
# "API key 未通过测试",实测两站都健康,真因是 base_url 漏了结尾的 /v1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "label"), [
    (provider_client.ProviderError("provider returned non-json response"),
     "HTTP 200 + 站点首页 HTML"),
    (provider_client.ProviderError(
        "provider_http_404: <!doctype html>\n<html lang=\"zh\">", status_code=404),
     "HTTP 404 + 错误页 HTML"),
])
def test_non_api_endpoint_points_at_the_url_not_the_key(client, monkeypatch, exc, label):
    """地址不是 API 端点时,提示必须指向地址而不是 key。

    ⚠️ 同时锁死 status_code 被清成 None:客户端把 provider 的 404 映射成
    「模型不存在」,带着 404 回去等于换一句话继续把用户往错方向支。"""
    _, api_key = _register(client)
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: (_ for _ in ()).throw(exc))

    r = client.post("/v1/model_api/setup", json={
        "provider": "openai_compatible", "model": "gpt-4o-mini",
        "api_key": "sk-whatever", "base_url": "https://relay.example.com",
    }, headers=_headers(api_key))

    assert r.status_code == 400, label
    body = r.get_json()
    assert body["error"] == "provider_test_failed", label
    assert body["status_code"] is None, f"{label}: 404 必须清掉,否则客户端说成模型不存在"
    assert "/v1" in body["detail"], label
    assert "不是 API Key 的问题" in body["detail"], label


def test_real_key_failure_keeps_the_original_detail(client, monkeypatch):
    """真的 key 无效(provider 回 JSON)→ 判据不许命中,原始信息原样透传。"""
    _, api_key = _register(client)
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: (_ for _ in ()).throw(
                            provider_client.ProviderError(
                                "provider_http_401: Invalid token", status_code=401)))

    r = client.post("/v1/model_api/setup", json={
        "provider": "openai_compatible", "model": "gpt-4o-mini",
        "api_key": "sk-bad", "base_url": "https://relay.example.com/v1",
    }, headers=_headers(api_key))

    body = r.get_json()
    assert body["error"] == "provider_test_failed"
    assert body["status_code"] == 401           # 保留,客户端据此说"鉴权失败"
    assert "Invalid token" in body["detail"]    # 原始信息不被吞掉
    assert "/v1" not in body["detail"]          # 不许对真 key 错误乱给地址建议


@pytest.mark.parametrize(("exc", "label"), [
    (provider_client.ProviderError("provider_http_401: relay auth html <!doctype html>",
                                   status_code=401), "401 鉴权页(HTML)"),
    (provider_client.ProviderError("provider_http_402: payment page html <html>",
                                   status_code=402), "402 支付页(HTML)"),
    (provider_client.ProviderError("provider_http_429: relay throttle html <html>",
                                   status_code=429), "429 限流页(HTML)"),
    (provider_client.ProviderError("provider_http_504: <!DOCTYPE html> gateway timeout",
                                   status_code=504), "504 网关故障页(HTML)"),
])
def test_html_error_pages_that_are_not_404_keep_their_real_meaning(
    client, monkeypatch, exc, label
):
    """relay/WAF/计费层会用 **HTML 页面**返回 401/402/429/5xx —— 本仓
    tests/test_catalog_consumer_parity.py:158-161 就存着这样的样本。

    把它们一并判成「地址错误」会让额度不足、鉴权失败、限流全部指错方向,
    比原来的错更严重。所以 HTML 只在 404 时才算地址问题(codex2 gatekeep
    2026-08-09 抓到:我原先的「真错误一律 JSON」是没验证的断言)。"""
    _, api_key = _register(client)
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: (_ for _ in ()).throw(exc))

    r = client.post("/v1/model_api/setup", json={
        "provider": "openai_compatible", "model": "gpt-4o-mini",
        "api_key": "sk-whatever", "base_url": "https://relay.example.com/v1",
    }, headers=_headers(api_key))

    body = r.get_json()
    assert body["error"] == "provider_test_failed", label
    assert body["status_code"] == exc.status_code, f"{label}: 状态码必须保留"
    assert "/v1" not in body["detail"], f"{label}: 不许被改写成地址建议"
    assert str(exc) in body["detail"], f"{label}: 原始信息必须原样透传"
