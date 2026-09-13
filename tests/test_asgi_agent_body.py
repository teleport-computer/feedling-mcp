"""Agent body HTTP contract: validated model output or an explicit error, never a fake body."""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import time

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
import debug_trace
import provider_client
from accounts import registry
from chat import consumer as chat_consumer
from asgi_test_client import make_client
from capabilities import identity as cap_identity, memory as cap_memory, types as cap_types
from core import config as core_config, envelope as core_envelope, store as core_store
from core import runtime_token
from hosted import agent_body_core as core

PATH = "/v1/agent-body/generate"
REQUEST = {"schema_version": 1, "grid_size": 24, "client_request_id": "body-request-1",
           "allowed_palette": ["#112233", "#aabbcc"]}


def valid_rows():
    return [[1 if 5 <= x <= 18 and 5 <= y <= 18 else 0 for x in range(24)] for y in range(24)]


@pytest.fixture
def generation_env(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    client = make_client()
    response = client.post("/v1/users/register", json={
        "public_key": base64.b64encode(os.urandom(32)).decode(), "archive_language": "en",
    })
    assert response.status_code == 201
    account = response.get_json()
    uid, api_key = account["user_id"], account["api_key"]
    db.set_onboarding_route_strict(uid, {"route": "model_api"})
    route = {"id": "body-route", "provider": "openai", "model": "gpt-4o-mini",
             "base_url": "https://api.openai.com/v1", "test_status": "ok",
             "api_key_envelope": {"id": "provider-envelope"}, "reasoning_effort": "low"}
    monkeypatch.setattr(db, "model_api_active_route", lambda user_id: route if user_id == uid else None)

    def decrypt(envelope, key, **kwargs):
        assert key == api_key and kwargs["caller_user_id"] == uid
        return b"secret-provider-key"
    monkeypatch.setattr(core_envelope, "decrypt_provider_key_envelope", decrypt)

    def identity(store, *, api_key):
        assert store.user_id == uid and api_key == account["api_key"]
        return cap_types.ok(data={"identity": {"decrypt_status": "ok", "agent_name": "小豆",
                                               "self_introduction": "private-identity-marker"}})
    monkeypatch.setattr(cap_identity, "get", identity)

    def memory(store, *, api_key, params):
        assert store.user_id == uid and api_key == account["api_key"] and params == {"limit": 12}
        return cap_types.ok(data={"items": [{"summary": "private-memory-marker"}]})
    monkeypatch.setattr(cap_memory, "index", memory)
    db.set_blob(uid, "genesis_persona", {"content_envelope": {"id": "persona-envelope"}})

    def persona(envelope, key, **kwargs):
        assert envelope["id"] == "persona-envelope" and key == api_key
        assert kwargs == {"purpose": "genesis_persona", "caller_user_id": uid}
        return b"private-persona-marker"
    monkeypatch.setattr(core_envelope, "read_envelope_body", persona)
    calls, traces = [], []
    monkeypatch.setattr(debug_trace, "trace_event", lambda store, **event: traces.append(event))

    def provider(config, messages, **kwargs):
        calls.append((config, copy.deepcopy(messages), kwargs))
        return {"reply": json.dumps({"rows": valid_rows()})}
    monkeypatch.setattr(provider_client, "chat_completion", provider)
    return client, {"X-API-Key": api_key}, route, calls, traces, uid


def post(env, payload=None):
    return env[0].post(PATH, headers=env[1], json=REQUEST if payload is None else payload)


def test_model_rows_return_verbatim_with_private_context(generation_env, caplog):
    caplog.set_level("INFO", logger=core.__name__)
    response = post(generation_env)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["rows"] == valid_rows()
    assert body["schema_version"] == 1 and body["grid_size"] == 24
    assert body["generation_id"].startswith("agent_body:")
    calls, traces = generation_env[3:5]
    assert len(calls) == 1
    config, messages, kwargs = calls[0]
    assert config.api_key == "secret-provider-key"
    assert kwargs["max_tokens"] == 8192 and 0 < kwargs["timeout"] <= 70
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["include_reasoning"] is True
    for marker in ("private-identity-marker", "private-persona-marker", "private-memory-marker"):
        assert marker in messages[1]["content"]
        assert marker not in json.dumps(traces) + caplog.text
    event = traces[-1]
    assert event["type"] == "agent_body.generate.finished" and event["status"] == "ok"
    assert set(event["detail"]) == {"client_request_id", "generation_id", "status_code", "dur_ms",
                                    "attempts", "provider", "model", "error_class", "rows_nonzero", "rows_sha256"}
    assert event["detail"]["rows_nonzero"] == 196
    assert "secret-provider-key" not in json.dumps(traces) + caplog.text


@pytest.mark.parametrize("changes", [
    {"schema_version": 2}, {"schema_version": True}, {"grid_size": 23}, {"grid_size": 24.0},
    {"client_request_id": ""}, {"client_request_id": " "}, {"client_request_id": "x" * 129},
    {"allowed_palette": []}, {"allowed_palette": ["red"]}, {"allowed_palette": ["#000000"] * 256},
])
def test_invalid_requests_do_not_call_provider(generation_env, changes):
    response = post(generation_env, {**REQUEST, **changes})
    assert response.status_code == 400
    assert response.get_json() == {"error": "agent_body_invalid_request"}
    assert not generation_env[3]


@pytest.mark.parametrize("payload", [[], "text", None])
def test_non_object_json(generation_env, payload):
    response = generation_env[0].post(PATH, headers=generation_env[1], json=payload)
    assert response.status_code == 400 and "rows" not in response.get_json()


def test_unauthenticated_request(generation_env):
    response = generation_env[0].post(PATH, json=REQUEST)
    assert response.status_code == 401
    assert not generation_env[3]


@pytest.mark.parametrize("mode,status,slug", [
    ("model_api", 400, "model_api_not_configured"),
    ("resident", 409, "agent_body_resident_update_required"),
])
def test_missing_route(generation_env, monkeypatch, mode, status, slug):
    monkeypatch.setattr(db, "model_api_active_route", lambda uid: None)
    db.set_onboarding_route_strict(generation_env[5], {"route": mode})
    response = post(generation_env)
    assert response.status_code == status and response.get_json() == {"error": slug}
    assert not generation_env[3]


@pytest.mark.parametrize("change,slug", [
    ({"test_status": "failed"}, "model_api_not_tested"),
    ({"api_key_envelope": None}, "model_api_key_envelope_missing"),
])
def test_unusable_route_preserves_loader_slug(generation_env, change, slug):
    generation_env[2].update(change)
    response = post(generation_env)
    assert response.status_code == 400 and response.get_json()["error"] == slug


def test_provider_key_decrypt_failure_is_safe(generation_env, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("private-key-diagnostic")
    monkeypatch.setattr(core_envelope, "decrypt_provider_key_envelope", fail)
    response = post(generation_env)
    assert response.status_code == 400
    assert response.get_json() == {"error": "model_api_key_decrypt_failed"}


def test_resident_uses_own_agent_even_with_server_route(generation_env):
    db.set_onboarding_route_strict(generation_env[5], {"route": "resident"})
    assert post(generation_env).status_code == 409
    assert not generation_env[3]


@pytest.mark.parametrize("bad_rows,feedback", [
    ([], "24 行"), ([[3] * 24 for _ in range(24)], "越界索引"),
    ([[0] * 24 for _ in range(24)], "全空"),
    ([[1] * 23 for _ in range(24)], "第 1 行"),
    ([[True] * 24 for _ in range(24)], "非整数"),
    ([[1.0] * 24 for _ in range(24)], "非整数"),
])
def test_invalid_rows_repaired_once(generation_env, monkeypatch, bad_rows, feedback):
    calls = []
    def provider(config, messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        return {"reply": json.dumps({"rows": bad_rows if len(calls) == 1 else valid_rows()})}
    monkeypatch.setattr(provider_client, "chat_completion", provider)
    response = post(generation_env)
    assert response.status_code == 200 and response.get_json()["rows"] == valid_rows()
    assert len(calls) == 2 and feedback in calls[1][-1]["content"]


def test_invalid_model_output_never_falls_back_to_local_rows(generation_env, monkeypatch):
    calls = []
    def provider(config, messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        return {"reply": '{"rows": []}'}
    monkeypatch.setattr(provider_client, "chat_completion", provider)
    response = post(generation_env)
    assert response.status_code == 502
    assert response.get_json()["error"] == "agent_body_generation_invalid_output"
    assert "rows" not in response.get_json()
    assert len(calls) == 2 and "24 行" in calls[1][-1]["content"]


@pytest.mark.parametrize("exc,status,slug", [
    (provider_client.ProviderError("private upstream", status_code=429), 429, "agent_body_generation_failed"),
    (provider_client.ProviderError("private upstream", status_code=401), 409, "agent_body_provider_config_failed"),
    (provider_client.ProviderError("private upstream", status_code=403), 409, "agent_body_provider_config_failed"),
    (provider_client.ProviderError("provider network error: ReadTimeout"), 504, "agent_body_generation_timeout"),
    (TimeoutError(), 504, "agent_body_generation_timeout"),
    (provider_client.ProviderError("private upstream", status_code=500), 502, "agent_body_generation_failed"),
])
def test_provider_errors(generation_env, monkeypatch, exc, status, slug):
    calls = []
    def provider(*args, **kwargs):
        calls.append(1)
        raise exc
    monkeypatch.setattr(provider_client, "chat_completion", provider)
    response = post(generation_env)
    assert response.status_code == status
    assert response.get_json()["error"] == slug and "rows" not in response.get_json()
    assert "private upstream" not in json.dumps(response.get_json()) + json.dumps(generation_env[4])
    assert len(calls) == 1


def test_no_repair_when_less_than_25_seconds_remain(generation_env, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: clock[0])
    generation = core.Generation(started=clock[0])
    calls = []
    def provider(*args, **kwargs):
        calls.append(kwargs)
        clock[0] += 61
        return {"reply": '{"rows": []}'}
    monkeypatch.setattr(provider_client, "chat_completion", provider)
    body, status = core.generate(SimpleNamespace(user_id=generation_env[5]), REQUEST,
                                 caller_api_key=generation_env[1]["X-API-Key"], generation=generation)
    assert status == 504 and "rows" not in body and len(calls) == 1


def test_http_deadline_returns_even_when_sync_provider_stalls(generation_env, monkeypatch):
    release = threading.Event()
    ended = threading.Event()
    calls = []
    def provider(*args, **kwargs):
        calls.append(1)
        try:
            release.wait(3)
            return {"reply": '{"rows": []}'}
        finally:
            ended.set()
    monkeypatch.setattr(provider_client, "chat_completion", provider)
    monkeypatch.setattr(core, "TOTAL_TIMEOUT_SECONDS", 0.1)
    start = time.monotonic()
    try:
        response = post(generation_env)
        assert response.status_code == 504 and "rows" not in response.get_json()
        assert time.monotonic() - start < 1
        assert generation_env[4][-1]["detail"]["status_code"] == 504
    finally:
        release.set()
        assert ended.wait(2)
    assert len(calls) == 1


def test_fenced_json(generation_env, monkeypatch):
    monkeypatch.setattr(provider_client, "chat_completion", lambda *a, **k: {
        "reply": "```json\n" + json.dumps({"rows": valid_rows()}) + "\n```"})
    assert post(generation_env).status_code == 200


def test_empty_provider_reply_is_failure(generation_env, monkeypatch):
    monkeypatch.setattr(provider_client, "chat_completion", lambda *a, **k: {"reply": ""})
    response = post(generation_env)
    assert response.status_code == 502 and "rows" not in response.get_json()


def test_optional_context_failures_are_labelled(generation_env, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("private-context-error")
    monkeypatch.setattr(cap_identity, "get", unavailable)
    monkeypatch.setattr(cap_memory, "index", unavailable)
    monkeypatch.setattr(core_envelope, "read_envelope_body", unavailable)
    assert post(generation_env).status_code == 200
    message = generation_env[3][0][1][1]["content"]
    assert message.count("当前读取失败") == 3 and "private-context-error" not in message


def test_runtime_token_cannot_generate_even_with_an_api_key_header(generation_env, monkeypatch):
    secret = b"agent-body-runtime-secret"
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", secret.decode())
    token = runtime_token.mint(secret, user_id=generation_env[5],
                               runtime_instance_id="agent-body-runtime", scope=["envelope_decrypt"])
    response = generation_env[0].post(PATH, json=REQUEST, headers={
        **generation_env[1], "X-Feedling-Runtime-Token": token,
    })
    assert response.status_code == 403
    assert response.get_json()["error"] == "forbidden" and not generation_env[3]


def test_palette_supports_full_uint8_range(generation_env, monkeypatch):
    rows = [[255] * 24 for _ in range(24)]
    monkeypatch.setattr(provider_client, "chat_completion", lambda *a, **k: {"reply": json.dumps({"rows": rows})})
    response = post(generation_env, {**REQUEST, "allowed_palette": ["#112233"] * 255})
    assert response.status_code == 200 and response.get_json()["rows"] == rows


def test_repair_at_25_seconds_uses_remaining_timeout(generation_env, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: clock[0])
    generation = core.Generation(started=clock[0])
    calls = []
    def provider(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            clock[0] += 60
            return {"reply": '{"rows": []}'}
        return {"reply": json.dumps({"rows": valid_rows()})}
    monkeypatch.setattr(provider_client, "chat_completion", provider)
    body, status = core.generate(SimpleNamespace(user_id=generation_env[5]), REQUEST,
                                 caller_api_key=generation_env[1]["X-API-Key"], generation=generation)
    assert status == 200 and body["rows"] == valid_rows()
    assert [call["timeout"] for call in calls] == [70, 25]


def test_valid_output_after_deadline_is_discarded(generation_env, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: clock[0])
    generation = core.Generation(started=clock[0])
    def provider(*args, **kwargs):
        clock[0] += 86
        return {"reply": json.dumps({"rows": valid_rows()})}
    monkeypatch.setattr(provider_client, "chat_completion", provider)
    body, status = core.generate(SimpleNamespace(user_id=generation_env[5]), REQUEST,
                                 caller_api_key=generation_env[1]["X-API-Key"], generation=generation)
    assert status == 504 and "rows" not in body and generation.attempts == 1


def test_missing_identity_does_not_hide_persona_or_memory(generation_env, monkeypatch):
    monkeypatch.setattr(cap_identity, "get", lambda *a, **k: cap_types.ok(data={
        "identity": {"decrypt_status": "ok", "agent_name": "TA", "dimensions": []}}))
    assert post(generation_env).status_code == 200
    message = generation_env[3][0][1][1]["content"]
    assert "身份卡：当前不可用或尚无内容" in message
    assert "private-persona-marker" in message and "private-memory-marker" in message


@pytest.fixture
def resident_env(generation_env):
    env = generation_env
    db.set_onboarding_route_strict(env[5], {"route": "resident"})
    headers = {**env[1], "X-Feedling-Consumer": "feedling-chat-resident",
               "X-Feedling-Consumer-Id": "body-consumer", "X-Feedling-Agent-Entry-Signature": "body-entry",
               "X-Feedling-Agent-Provider": "local-provider", "X-Feedling-Agent-Model": "local-model",
               "X-Feedling-Consumer-Capabilities": "agent_body_generate_v1"}
    response = env[0].get("/v1/chat/poll?timeout=0", headers=headers)
    assert response.status_code == 200
    return env, headers


def wait_body_job(env, headers):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = env[0].get("/v1/chat/poll?timeout=0", headers=headers)
        assert response.status_code == 200
        job = response.get_json().get("agent_body_job")
        if job:
            return job
        threading.Event().wait(0.01)
    pytest.fail("consumer did not receive generation job")


@pytest.mark.parametrize("credential", ["api_key", "runtime_token"])
@pytest.mark.parametrize("rows,status", [(valid_rows(), 200), ([], 502), ([["private-invalid"]], 502)])
def test_resident_poll_result_roundtrip(resident_env, monkeypatch, credential, rows, status):
    from concurrent.futures import ThreadPoolExecutor
    env, headers = resident_env
    if credential == "runtime_token":
        secret = b"agent-body-runtime-secret"
        monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", secret.decode())
        headers = {key: value for key, value in headers.items() if key != "X-API-Key"}
        headers["X-Feedling-Runtime-Token"] = runtime_token.mint(
            secret, user_id=env[5], runtime_instance_id="body-runtime", scope=["chat"])
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(post, env)
        job = wait_body_job(env, headers)
        assert set(job) == {"job_id", "expires_at_epoch", "prompt", "palette_count"}
        assert "#112233" in job["prompt"] and job["palette_count"] == 2
        for marker in ("private-identity-marker", "private-persona-marker", "private-memory-marker"):
            assert marker not in json.dumps(job)
        payload = {"job_id": job["job_id"], "status": "ok", "rows": rows, "attempts": 1}
        response = env[0].post("/v1/internal/agent-body/generate/result", headers=headers, json=payload)
        assert response.status_code == 200, response.get_json()
        assert "private-invalid" not in json.dumps(db.get_blob(env[5], "consumer_state"))
        result = future.result(timeout=3)
        assert result.status_code == status, result.get_json()
        if status == 200:
            assert result.get_json()["rows"] == rows
        else:
            assert result.get_json()["error"] == "agent_body_generation_invalid_output"
            assert "rows" not in result.get_json()
        state = db.get_blob(env[5], "consumer_state")
        assert "resident_agent_body_job" not in state
        assert set(state["resident_agent_body_result"]) == {"job_id", "status", "error_code", "finished_at"}
        duplicate = env[0].post("/v1/internal/agent-body/generate/result", headers=headers, json=payload)
        assert duplicate.status_code == 200
        assert db.get_blob(env[5], "consumer_state")["resident_agent_body_result"] == state["resident_agent_body_result"]
    assert not env[3]


def test_resident_wrong_binding_cannot_complete(resident_env):
    from concurrent.futures import ThreadPoolExecutor
    env, headers = resident_env
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(post, env)
        job = wait_body_job(env, headers)
        payload = {"job_id": job["job_id"], "status": "ok", "rows": valid_rows()}
        for field in ("X-Feedling-Consumer-Id", "X-Feedling-Agent-Entry-Signature"):
            response = env[0].post("/v1/internal/agent-body/generate/result", json=payload,
                                   headers={**headers, field: "wrong"})
            assert response.status_code == 409
            assert db.get_blob(env[5], "consumer_state")["resident_agent_body_job"]["job_id"] == job["job_id"]
        response = env[0].post("/v1/internal/agent-body/generate/result", json=payload, headers=headers)
        assert response.status_code == 200
        assert future.result(timeout=3).status_code == 200


def test_resident_timeout_cleans_job_and_rejects_late_result(resident_env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    env, headers = resident_env
    monkeypatch.setattr(core, "TOTAL_TIMEOUT_SECONDS", 0.3)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(post, env)
        job = wait_body_job(env, headers)
        assert future.result(timeout=2).status_code == 504
    # The abandoned worker owns CAS cleanup; it must finish just after its deadline.
    deadline = time.monotonic() + 2
    while "resident_agent_body_job" in db.get_blob(env[5], "consumer_state") and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert "resident_agent_body_job" not in db.get_blob(env[5], "consumer_state")
    response = env[0].post("/v1/internal/agent-body/generate/result", headers=headers,
                           json={"job_id": job["job_id"], "status": "ok", "rows": valid_rows()})
    assert response.status_code == 410
    assert "rows" not in db.get_blob(env[5], "consumer_state")["resident_agent_body_result"]


def test_official_import_has_no_agent(generation_env):
    db.set_onboarding_route_strict(generation_env[5], {"route": "official_import"})
    response = post(generation_env)
    assert response.status_code == 409 and response.get_json()["error"] == "agent_body_agent_unavailable"
    assert not generation_env[3]


def test_resident_concurrent_request_does_not_overwrite_pending_job(resident_env):
    from concurrent.futures import ThreadPoolExecutor
    env, headers = resident_env
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(post, env)
        job = wait_body_job(env, headers)
        second = post(env, {**REQUEST, "client_request_id": "another-generation"})
        assert second.status_code == 502 and "rows" not in second.get_json()
        assert wait_body_job(env, headers)["job_id"] == job["job_id"]
        env[0].post("/v1/internal/agent-body/generate/result", headers=headers,
                     json={"job_id": job["job_id"], "status": "ok", "rows": valid_rows()})
        assert first.result(timeout=3).status_code == 200


def test_resident_cleanup_collision_preserves_valid_result(resident_env, monkeypatch, caplog):
    from concurrent.futures import ThreadPoolExecutor
    env, headers = resident_env
    cleanup_calls = []
    def fail_cleanup(store, job_id):
        cleanup_calls.append(job_id)
        return False
    monkeypatch.setattr(chat_consumer, "retire_agent_body_job", fail_cleanup)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(post, env)
        job = wait_body_job(env, headers)
        response = env[0].post("/v1/internal/agent-body/generate/result", headers=headers,
            json={"job_id": job["job_id"], "status": "ok", "rows": valid_rows(), "attempts": 1})
        assert response.status_code == 200
        result = future.result(timeout=3)
        assert result.status_code == 200 and result.get_json()["rows"] == valid_rows()
    assert cleanup_calls == [job["job_id"]]
    records = [r.getMessage() for r in caplog.records if "agent_body cleanup failed" in r.getMessage()]
    assert records == [f"agent_body cleanup failed job_id={job['job_id']}"]
