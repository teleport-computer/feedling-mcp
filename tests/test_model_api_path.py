from __future__ import annotations

import base64
import os
import sys
import threading
import time
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402
from accounts import registry as accounts_registry  # noqa: E402
from hosted import turn as hosted_turn  # noqa: E402
import provider_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    appmod._users[:] = []
    appmod._key_to_user.clear()
    appmod._stores.clear()
    appmod._save_users()
    monkeypatch.setattr(
        core_enclave,
        "_get_enclave_info",
        lambda: {"content_pk_hex": ("22" * 32), "compose_hash": "test"},
    )
    appmod.app.config.update(TESTING=True)
    with appmod.app.test_client() as c:
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
                captured.append(appmod.json.loads(plaintext.decode("utf-8")))
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

    extra = appmod._chat_plaintext_thinking_extra_for_store(
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

    extra = appmod._chat_plaintext_thinking_extra_for_store(
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

    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
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
    assert "api_key" not in public
    assert "api_key_envelope" not in public

    get_res = client.get("/v1/model_api/get", headers=_headers(api_key))
    assert get_res.status_code == 200
    assert "api_key_envelope" not in get_res.get_json()["config"]

    config_text = appmod.json.dumps(appmod.db.get_blob(user_id, "model_api") or {})
    assert raw_provider_key not in config_text
    assert "api_key_envelope" in config_text

    validate = client.get("/v1/onboarding/validate", headers=_headers(api_key))
    assert validate.status_code == 200
    body = validate.get_json()
    assert body["route"] == "model_api"
    assert body["stage"] == "history_import"
    assert all(step["id"] != "resident_consumer" for step in body["steps"])
    runtime = appmod.db.get_blob(user_id, "model_api_runtime")
    assert runtime["runtime_mode"] == "hosted_resident"
    assert runtime["tool_action_enabled"] is True
    assert any(step["id"] == "hosted_runtime" and step["passing"] for step in body["steps"])


def test_model_api_runtime_status_tracks_hosted_runtime_and_action_trace(client, monkeypatch):
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_shared_envelope_builder())
    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-test",
    )
    monkeypatch.setattr(
        core_enclave,
        "_enclave_get_json_for_gate",
        lambda path, key, params=None: ({"messages": [], "context_memories": []}, "")
        if path == "/v1/chat/history"
        else ({"identity": {}}, ""),
    )
    monkeypatch.setattr(
        provider_client,
        "chat_completion",
        lambda cfg, messages, **kwargs: {"reply": "Hosted runtime reply.", "usage": {"total_tokens": 7}},
    )

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    before = client.get("/v1/model_api/runtime", headers=_headers(api_key))
    assert before.status_code == 200, before.get_data(as_text=True)
    before_body = before.get_json()
    assert before_body["runtime_mode"] == "hosted_resident"
    assert before_body["tool_action_enabled"] is True
    assert before_body["last_action_trace_id"] in {"", None}

    chat = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello"},
        headers=_headers(api_key),
    )
    assert chat.status_code == 200, chat.get_data(as_text=True)
    chat_body = chat.get_json()
    trace_id = chat_body["state"]["action_trace_id"]
    assert trace_id
    assert chat_body["runtime"]["engine"] == "feedling_native"
    assert chat_body["runtime"]["background_execution"]["method"] == "feedling_background_execution"
    assert chat_body["state"]["background_execution"]["method"] == "feedling_background_execution"
    assert chat_body["state"]["background_execution"]["method"] == "feedling_background_execution"

    after = client.get("/v1/model_api/runtime", headers=_headers(api_key))
    assert after.status_code == 200, after.get_data(as_text=True)
    after_body = after.get_json()
    assert after_body["last_action_trace_id"] == trace_id
    assert after_body["last_action_trace_status"] == "ok"
    assert appmod.db.get_blob(user_id, "model_api_runtime")["last_action_trace_id"] == trace_id


def test_model_api_chat_uses_memory_selection_trace_without_prompting_rejected_cards(client, monkeypatch):
    user_id, api_key = _register(client)
    history_params: list[dict] = []
    provider_messages: list[list[dict]] = []

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_shared_envelope_builder())
    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-test",
    )

    def fake_enclave(path, key, params=None):
        if path == "/v1/chat/history":
            history_params.append(dict(params or {}))
            return {
                "messages": [{"role": "user", "content": "明天有一个 project 要完成，好累"}],
                "context_memories": [
                    {
                        "id": "pressure",
                        "title": "近期项目压力",
                        "description": "用户明天有一个项目要完成，觉得很累。",
                        "selection": {
                            "score": 0.72,
                            "confidence": "strong",
                            "matched_units": ["明天", "完成", "很累"],
                            "reason": "phrase_match",
                            "bucket": "query",
                        },
                    }
                ],
                "context_memory_trace": {
                    "mode": "model_api",
                    "query_units": ["project", "明天", "完成"],
                    "selected": [
                        {
                            "id": "pressure",
                            "title": "近期项目压力",
                            "score": 0.72,
                            "confidence": "strong",
                            "matched_units": ["明天", "完成", "很累"],
                            "reason": "phrase_match",
                            "bucket": "query",
                            "selected": True,
                        }
                    ],
                    "rejected_sample": [
                        {
                            "id": "toho",
                            "title": "TOHO Project 老二次元偏好",
                            "score": 0.18,
                            "confidence": "weak",
                            "matched_units": ["project"],
                            "reason": "weak_generic_overlap",
                            "bucket": "rejected",
                            "selected": False,
                        }
                    ],
                },
            }, ""
        if path == "/v1/identity/get":
            return {"identity": _identity_payload()}, ""
        return {}, ""

    def fake_chat_completion(cfg, messages, **kwargs):
        provider_messages.append(messages)
        return {"reply": "先把明天那个项目收个尾，别让自己硬扛。", "usage": {"total_tokens": 10}}

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave)
    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    chat = client.post(
        "/v1/model_api/chat/send",
        json={"message": "明天有一个 project 要完成，好累"},
        headers=_headers(api_key),
    )
    assert chat.status_code == 200, chat.get_data(as_text=True)
    assert history_params[-1]["context_trace"] == "1"
    prompt = "\n".join(str(m.get("content") or "") for m in provider_messages[0])
    assert "candidate memory context" in prompt
    assert "relevant memory cards" not in prompt
    assert "TOHO Project 老二次元偏好" not in prompt
    assert "weak_generic_overlap" not in prompt

    trace_id = chat.get_json()["state"]["action_trace_id"]
    traces = appmod.db.log_read(user_id, appmod.MODEL_API_ACTION_TRACE_STREAM, limit=5)
    trace = next(item for item in traces if item["trace_id"] == trace_id)
    selection = trace["context"]["memory_selection"]
    assert selection["selected"][0]["id"] == "pressure"
    assert selection["rejected_sample"][0]["id"] == "toho"
    assert selection["rejected_sample"][0]["reason"] == "weak_generic_overlap"


def test_model_api_chat_send_runs_backend_web_search_tool(client, monkeypatch):
    _, api_key = _register(client)
    provider_calls: list[list[dict]] = []
    search_requests: list[dict] = []

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_shared_envelope_builder())
    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-test",
    )
    monkeypatch.setattr(
        core_enclave,
        "_enclave_get_json_for_gate",
        lambda path, key, params=None: ({"messages": [], "context_memories": []}, "")
        if path == "/v1/chat/history"
        else ({"identity": {}}, ""),
    )

    def fake_web_search(requests):
        search_requests.extend(requests)
        return {
            "enabled": True,
            "status": "ok",
            "requests": requests,
            "result_count": 1,
            "errors": [],
            "results": [
                {
                    "query": requests[0]["query"],
                    "status": "ok",
                    "results": [
                        {
                            "title": "OpenAI product update",
                            "url": "https://example.com/openai-update",
                            "snippet": "A current public result.",
                        }
                    ],
                }
            ],
        }

    def fake_chat_completion(cfg, messages, **kwargs):
        provider_calls.append(messages)
        if len(provider_calls) == 1:
            return {
                "reply": appmod.json.dumps({
                    "reply": "",
                    "tool_requests": [
                        {
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": appmod.json.dumps({
                                    "query": "OpenAI product update",
                                    "reason": "needs current public information",
                                }),
                            },
                        }
                    ],
                }),
                "usage": {"total_tokens": 3},
            }
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        assert "Backend web_search tool results JSON" in joined
        assert "OpenAI product update" in joined
        return {
                "reply": appmod.json.dumps({
                    "reply": "The current public result says there was an OpenAI product update.",
                    "context_summary": "Searched the web for OpenAI product update.",
                }),
                "usage": {"total_tokens": 8},
            }

    monkeypatch.setattr(hosted_turn, "_run_model_api_web_searches", fake_web_search)
    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    chat = client.post(
        "/v1/model_api/chat/send",
        json={"message": "Tell me what OpenAI announced."},
        headers=_headers(api_key),
    )
    assert chat.status_code == 200, chat.get_data(as_text=True)
    body = chat.get_json()
    assert body["reply"] == "The current public result says there was an OpenAI product update."
    assert body["thinking_summary"] == "Searched the web for OpenAI product update."
    assert body["tools"]["web_search"]["requests"] == 1
    assert body["tools"]["web_search"]["results"] == 1
    assert search_requests[0]["query"] == "OpenAI product update"
    assert len(provider_calls) == 2


def test_model_api_chat_surfaces_provider_reasoning_before_context_summary(client, monkeypatch):
    _, api_key = _register(client)
    provider_kwargs: list[dict] = []

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_shared_envelope_builder())
    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", lambda envelope, key, purpose: b"sk-test")
    monkeypatch.setattr(
        core_enclave,
        "_enclave_get_json_for_gate",
        lambda path, key, params=None: ({"messages": [], "context_memories": []}, "")
        if path == "/v1/chat/history"
        else ({"identity": _identity_payload()}, ""),
    )

    def fake_chat_completion(cfg, messages, **kwargs):
        provider_kwargs.append(kwargs)
        return {
            "reply": appmod.json.dumps({
                "reply": "看到了，我直接回你。",
                "context_summary": "对齐了当前 Identity 设定。",
            }),
            "reasoning": "I considered the user's latest message and relevant memory before answering.",
            "usage": {"total_tokens": 9},
        }

    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openrouter", "model": "anthropic/claude-sonnet-4.5", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    chat = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello"},
        headers=_headers(api_key),
    )

    assert chat.status_code == 200, chat.get_data(as_text=True)
    body = chat.get_json()
    assert provider_kwargs[-1]["include_reasoning"] is True
    assert body["reply"] == "看到了，我直接回你。"
    assert body["context_summary"] == ""
    assert body["thinking_kind"] == "provider_reasoning"
    assert body["provider_reasoning"] == "I considered the user's latest message and relevant memory before answering."
    assert body["thinking_summary"] == body["provider_reasoning"]


def test_model_api_chat_does_not_treat_generic_query_as_web_search_request(client, monkeypatch):
    _, api_key = _register(client)
    provider_calls: list[list[dict]] = []
    search_requests: list[dict] = []

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_shared_envelope_builder())
    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", lambda envelope, key, purpose: b"sk-test")
    monkeypatch.setattr(
        core_enclave,
        "_enclave_get_json_for_gate",
        lambda path, key, params=None: ({"messages": [], "context_memories": []}, "")
        if path == "/v1/chat/history"
        else ({"identity": {}}, ""),
    )
    monkeypatch.setattr(hosted_turn, "_run_model_api_web_searches", lambda requests: search_requests.extend(requests) or {})

    def fake_chat_completion(cfg, messages, **kwargs):
        provider_calls.append(messages)
        return {
            "reply": appmod.json.dumps({
                "reply": "I can answer without a hosted web search.",
                "query": "this is ordinary model output metadata",
            }),
            "usage": {"total_tokens": 4},
        }

    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    chat = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello"},
        headers=_headers(api_key),
    )
    assert chat.status_code == 200, chat.get_data(as_text=True)
    body = chat.get_json()
    assert body["reply"] == "I can answer without a hosted web search."
    assert body["tools"]["web_search"]["requests"] == 0
    assert search_requests == []
    assert len(provider_calls) == 1


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
        return appmod.json.dumps(plain).encode("utf-8")

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_shared_envelope_builder(captured_plaintexts))
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)
    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})

    def fake_chat_completion(cfg, messages, **kwargs):
        return {
            "reply": appmod.json.dumps({
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

    appmod.db.memory_replace_all(user_id, [
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

    saved = appmod.db.memory_load(user_id)
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
        raise appmod.ProviderError(
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
    assert appmod._parse_iso_calendar_date("20260602") == appmod.date(2026, 6, 2)
    assert appmod._parse_iso_calendar_date("2026/06/02") == appmod.date(2026, 6, 2)
    assert appmod._parse_iso_calendar_date("2026年6月2日") == appmod.date(2026, 6, 2)
    assert appmod._parse_iso_calendar_date("2026-02-31") is None

    parsed, err = appmod._relationship_start_from_import(
        {"relationship_started_at": "20260602"},
        [],
    )
    assert parsed == appmod.date(2026, 6, 2)
    assert err == ""

    fallback, err = appmod._relationship_start_from_import(
        {"relationship_started_at": "not a date"},
        [],
    )
    assert fallback == appmod.date.today()
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
            return {"reply": appmod.json.dumps(_identity_payload()), "usage": {}}
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
    assert chat.status_code == 200, chat.get_data(as_text=True)
    chat_body = chat.get_json()
    assert chat_body["reply"] == "I can answer from the imported history now."
    assert chat_body["thinking_summary"] == ""
    assert chat_body["context"]["identity_loaded"] is True
    assert chat_body["context"]["memories"] == 1

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
    assert any(row["source"] == "model_api" and row["role"] == "openclaw" for row in rows)
    assert all("body_ct" in row for row in rows if row["source"] == "model_api")
    assert "sk-test-secret" not in appmod.json.dumps(appmod.db.get_blob(user_id, "model_api") or {})


def test_model_api_context_summary_parsing_drops_generic_runtime_fallback():
    reply, summary = appmod._model_api_parse_turn_reply(
        '{"reply":"好，我在。","thinking_summary":"参考了 8 条相关记忆。\\n对齐了当前 Identity 设定。"}'
    )
    assert reply == "好，我在。"
    assert summary == ""

    reply, summary = appmod._model_api_parse_turn_reply(
        '{"reply":"我先不删。","context_summary":"准备删除 Memory：烧卖和蒸饺设定，等待用户确认。"}'
    )
    assert reply == "我先不删。"
    assert summary == "准备删除 Memory：烧卖和蒸饺设定，等待用户确认。"


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
            return {"reply": appmod.json.dumps(_identity_payload()), "usage": {}}
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
    _, api_key = _register(client)
    captured = {}

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
        if any(isinstance(m.get("content"), list) for m in messages):
            captured["messages"] = messages
        return {"reply": "I can see the image.", "usage": {"total_tokens": 11}}

    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test-secret"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    chat = client.post(
        "/v1/model_api/chat/send",
        json={
            "message": "What is in this image?",
            "image_mime": "image/jpeg",
            "image_b64": _b64(b"fake-jpeg-bytes"),
        },
        headers=_headers(api_key),
    )
    assert chat.status_code == 200, chat.get_data(as_text=True)
    assert chat.get_json()["user_content_type"] == "image"

    user_messages = [m for m in captured["messages"] if m.get("role") == "user"]
    content = user_messages[-1]["content"]
    assert content[0] == {"type": "text", "text": "What is in this image?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

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
            return {"reply": appmod.json.dumps(_identity_payload()), "usage": {}}
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
            "content": appmod.json.dumps(chat_export),
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
        + appmod.json.dumps(chat_export, ensure_ascii=False)
        + "\n===== END CHAT HISTORY FILE: conversations-011.json ====="
    )
    warnings = []

    messages = appmod._parse_import_history_content(wrapped, "auto", warnings)

    assert warnings == []
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "API onboarding" in messages[0]["content"]
    assert all("BEGIN CHAT HISTORY FILE" not in m["content"] for m in messages)
    assert all("conversation_id" not in m["content"] for m in messages)

    cards = appmod._fallback_memory_cards(
        messages,
        appmod.date(2026, 5, 31),
        story_needed=1,
        about_needed=1,
        language=appmod._detect_import_language(messages),
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

    sample = appmod._transcript_sample(messages, max_chars=5000)

    assert "EARLIEST_MARKER" in sample
    assert "MIDDLE_MARKER" in sample
    assert "LATEST_MARKER" in sample


def test_large_history_extraction_windows_cover_full_timeline():
    messages = []
    for idx in range(120):
        messages.append({
            "role": "user",
            "content": f"history-window-message-{idx} " + ("x" * 350),
            "ts": 1_700_000_000 + idx,
            "source": "history_import",
        })
    messages[0]["content"] = "FIRST_WINDOW_MARKER " + messages[0]["content"]
    messages[-1]["content"] = "LAST_WINDOW_MARKER " + messages[-1]["content"]

    windows = appmod._transcript_extraction_windows(messages, max_chars=5000, max_windows=5)
    joined = "\n".join(windows)

    assert len(windows) > 1
    assert "FIRST_WINDOW_MARKER" in joined
    assert "LAST_WINDOW_MARKER" in joined


def test_import_memory_targets_do_not_force_historical_floor_padding():
    targets = appmod._import_memory_targets(
        {"story": 15, "about_me": 60, "ta_thinking": 12, "total": 87},
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

    profile = appmod._history_import_profile(messages, [], content_chars=80_000)
    targets = appmod._import_memory_targets(
        {"story": 15, "about_me": 60, "ta_thinking": 12, "total": 87},
        messages,
        [],
        profile,
    )

    assert profile["tier"] == "ultra"
    assert targets["total"] == 120
    assert targets["chat_ready_cards"] == 20
    assert targets["background"] is True


def test_import_memory_filters_generic_import_cards_and_repetitive_low_value_content():
    cards = appmod._dedupe_memory_cards([
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

    candidates = appmod._coerce_import_candidates(raw, appmod.date(2026, 6, 1), window_id="w1")
    cards = appmod._render_candidates_to_memory_cards(
        candidates,
        appmod.date(2026, 6, 1),
        {"story": 2, "about_me": 2, "ta_thinking": 0, "total": 4},
        language="en",
    )

    assert len(candidates) == 2
    assert any(card["type"] == "fact" and "raw JSON" in card["description"] for card in cards)
    assert any(card["type"] == "moment" and "API onboarding" in card["description"] for card in cards)
    assert all("generic concept" not in card["description"] for card in cards)


def test_identity_import_keeps_unknown_agent_name_empty():
    payload = _identity_payload()
    payload["agent_name"] = "IO"

    normalized = appmod._normalize_identity_payload(payload, [], 7, "zh-Hans")

    assert normalized["agent_name"] == ""

    payload["agent_name"] = "小哆啦"
    normalized = appmod._normalize_identity_payload(payload, [], 7, "zh-Hans")

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

    candidates = appmod._coerce_import_candidates(raw, appmod.date(2026, 5, 1), window_id="w1")
    cards = appmod._render_candidates_to_memory_cards(
        candidates,
        appmod.date(2026, 5, 1),
        {"story": 2, "about_me": 4, "ta_thinking": 0, "total": 6},
        language="en",
    )

    assert all("real name" not in card["description"].lower() for card in cards)
    assert sum("direct feedback" in card["description"] for card in cards) == 1
    assert [card["occurred_at"] for card in cards] == sorted([card["occurred_at"] for card in cards], reverse=True)


def test_candidate_extraction_repairs_malformed_provider_json(monkeypatch):
    calls = []

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        calls.append(joined)
        if "previous model response was not valid json" in joined.lower():
            return {
                "reply": appmod.json.dumps({
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

    candidates, warnings = appmod._extract_memory_candidates_with_provider(
        appmod.ProviderConfig("openai", "gpt-4.1-mini", "sk-test"),
        [{"id": "w1", "text": "2026-06-01 User: Please turn this into readable memory."}],
        appmod.date(2026, 6, 1),
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

    text, warnings = appmod._generate_model_api_onboarding_greeting(
        appmod.ProviderConfig("openai", "gpt-4.1-mini", "sk-test"),
        [{"role": "user", "content": "这是之前的聊天。", "source": "history_import"}],
        [],
        {"agent_name": "", "self_introduction": ""},
        10,
        "zh-Hans",
    )

    assert warnings == []
    assert "还没有名字" in captured["prompt"]
    assert "你想怎么叫我" in text


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

    support = appmod._persona_support_messages(payload)

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

    support = appmod._persona_support_messages(payload)

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

    support = appmod._persona_support_messages(payload)

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
    support = appmod._persona_support_messages(payload)
    history = [
        {"role": "user", "content": f"history line {idx}", "source": "history_import"}
        for idx in range(240)
    ]

    windows = appmod._build_transcript_windows(
        support + history,
        max_chars=2500,
        max_windows=4,
    )

    assert any(w.get("source_families") == ["memory_summary_import"] for w in windows)
    assert any(w.get("source_families") == ["ai_persona_import"] for w in windows)
    assert any(w.get("source_families") == ["history_import"] for w in windows)
    assert any("用户在五月反复提到需要稳定陪伴" in w["text"] for w in windows)


def test_memory_summary_fallback_splits_high_recall_cards_without_ai_persona_story_pollution():
    messages = appmod._persona_support_messages({
        "ai_persona_content": "TA 叫小哆啦，温柔稳定。",
        "memory_summary_content": "1. 用户在五月反复提到需要稳定陪伴。\n2. 用户希望重要提醒要直接说。\n3. 他们在一次争执后约定先确认情绪。",
        "personal_profile_content": "用户喜欢直接反馈。",
    })

    cards = appmod._fallback_memory_cards(
        messages,
        appmod.date(2026, 5, 1),
        story_needed=2,
        about_needed=2,
        language="zh-Hans",
    )

    assert len(cards) >= 4
    assert not any("温柔稳定" in c["description"] and c["type"] in {"moment", "quote"} for c in cards)
    assert any("稳定陪伴" in c["description"] for c in cards)
    assert any("直接" in c["description"] for c in cards)


def test_identity_without_ai_source_does_not_use_user_profile_as_companion(monkeypatch):
    def fake_chat_completion(cfg, messages, **kwargs):
        return {
            "reply": appmod.json.dumps({
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

    identity, warnings = appmod._derive_identity_with_provider(
        appmod.ProviderConfig("openai", "gpt-4.1-mini", "sk-test"),
        [{"role": "user", "content": "User profile:\n用户叫 Seven，喜欢直接反馈。", "source": "user_profile_import"}],
        [],
        3,
        "zh-Hans",
    )

    assert identity["agent_name"] == ""
    assert "Seven" not in identity["self_introduction"]
    assert "identity_guard_no_ai_source_used_generic_identity" in warnings


def test_support_materials_extract_chatgpt_memories_json_without_raw_artifacts():
    payload = {
        "personal_profile_filename": "memories.json",
        "personal_profile_content": appmod.json.dumps([
            {
                "conversations_memory": "**工作上下文**\nSeven 正在做 Feedling MCP 和 API onboarding。",
                "account_uuid": "user-secret-id",
            }
        ]),
    }

    support = appmod._persona_support_messages(payload)

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
        "personal_profile_content": appmod.json.dumps([
            {
                "uuid": "user-secret-id",
                "email_address": "seven@example.com",
                "verified_phone_number": "+10000000000",
                "full_name": "Seven",
            }
        ]),
    }

    assert appmod._persona_support_messages(payload) == []


def test_import_language_prefers_user_archive_language(monkeypatch):
    monkeypatch.setattr(accounts_registry, "_get_user_archive_language", lambda user_id: "zh-Hans-US")
    store = type("Store", (), {"user_id": "usr_test"})()

    language = appmod._import_language_for_store(
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
            return {"reply": appmod.json.dumps(_identity_payload()), "usage": {}}
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
