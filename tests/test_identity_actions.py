from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import provider_client  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from identity import actions as identity_actions_mod  # noqa: E402
from identity import service as identity_service  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
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


def _plain_identity() -> dict:
    return {
        "agent_name": "bro",
        "self_introduction": "I keep the real thread with you.",
        "category": "Blunt · Observant",
        "signature": ["Direct", "Context-heavy"],
        "dimensions": [
            {"name": "Signal sensitivity", "value": 92, "description": "Notices subtle shifts."},
            {"name": "Context retention", "value": 88, "description": "Keeps prior context."},
            {"name": "Narrative loyalty", "value": 82, "description": "Tracks the real thread."},
            {"name": "Strategic sharpness", "value": 74, "description": "Finds the useful next move."},
            {"name": "Evidence discipline", "value": 70, "description": "Asks for receipts."},
            {"name": "Aesthetic weirdness", "value": 76, "description": "Keeps a distinctive voice."},
            {"name": "Tenderness under pressure", "value": 58, "description": "Can soften under stress."},
        ],
    }


def _seed_identity(user_id: str) -> None:
    db.set_blob(user_id, "identity", {
        "v": 1,
        "id": "identity_1",
        "body_ct": "old",
        "nonce": "old_nonce",
        "K_user": "old_k_user",
        "K_enclave": "old_k_enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
        "created_at": "2026-05-31T00:00:00",
        "updated_at": "2026-05-31T00:00:00",
        "relationship_started_at": "2026-04-01",
        "relationship_anchor_source": "test",
        "relationship_anchor_evidence": "seeded identity for test",
    })


def _seed_memory(user_id: str, memory_id: str = "mom_1") -> None:
    db.memory_replace_all(user_id, [{
        "v": 1,
        "id": memory_id,
        "type": "fact",
        "occurred_at": "2026-05-01",
        "created_at": "2026-05-01T00:00:00",
        "source": "bootstrap",
        "body_ct": "old",
        "nonce": "old_nonce",
        "K_user": "old_k_user",
        "K_enclave": "old_k_enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
    }])


def _plain_memory() -> dict:
    return {
        "title": "Wrong city",
        "description": "User moved to New York in May.",
        "type": "fact",
        "context": "Imported profile",
    }


def _fake_envelope_builder(captured: list):
    def _build(store, plaintext: bytes, item_id: str | None = None):
        try:
            captured.append(json.loads(plaintext.decode("utf-8")))
        except Exception:
            captured.append(plaintext.decode("utf-8"))
        envelope = {
            "id": item_id or "env_1",
            "body_ct": f"ct_{len(captured)}",
            "nonce": f"nonce_{len(captured)}",
            "K_user": f"k_user_{len(captured)}",
            "K_enclave": f"k_enclave_{len(captured)}",
            "visibility": "shared",
            "owner_user_id": store.user_id,
            "enclave_pk_fpr": "test",
        }
        return envelope, ""
    return _build


def test_identity_profile_patch_reencrypts_existing_card(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(
        core_enclave,
        "_enclave_get_json_for_gate",
        lambda path, key, params=None: ({"identity": _plain_identity()}, "") if path == "/v1/identity/get" else ({}, ""),
    )
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "identity.profile_patch",
                "patch": {"agent_name": "小秘"},
                "reason": "User asked for a displayed name change.",
            }],
        },
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "ok"
    assert body["effects"][0]["type"] == "identity_updated"
    assert body["effects"][0]["fields"] == ["agent_name"]
    assert captured_plaintexts[-1]["agent_name"] == "小秘"
    assert captured_plaintexts[-1]["self_introduction"] == _plain_identity()["self_introduction"]
    saved = db.get_blob(user_id, "identity")
    assert saved["id"] == "identity_1"
    assert saved["body_ct"] == "ct_1"
    assert saved["relationship_started_at"] == "2026-04-01"


def test_identity_profile_patch_passes_runtime_token_to_enclave(client, monkeypatch):
    user_id, _api_key = _register(client)
    _seed_identity(user_id)
    store = core_store.get_store(user_id)
    captured: dict = {}
    captured_plaintexts: list = []

    def fake_enclave_context(path, key, params=None, runtime_token=""):
        captured["path"] = path
        captured["key"] = key
        captured["runtime_token"] = runtime_token
        return ({"identity": _plain_identity()}, "") if path == "/v1/identity/get" else ({}, "")

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_context)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    body, status = identity_actions_mod._execute_identity_actions(
        store,
        None,
        [{
            "type": "identity.profile_patch",
            "patch": {"self_introduction": "我已经回来了。"},
        }],
        runtime_token="rtok_identity",
    )

    assert status == 200
    assert body["status"] == "ok"
    assert captured == {
        "path": "/v1/identity/get",
        "key": None,
        "runtime_token": "rtok_identity",
    }
    assert captured_plaintexts[-1]["self_introduction"] == "我已经回来了。"


@pytest.mark.xfail(reason="inline background runtime removed in chat-send 收口 (Task 3); behavior moved to agent-runner consumer — needs consumer-side coverage", strict=False)
def test_model_api_chat_background_runtime_executes_detected_identity_rename(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", lambda envelope, key, purpose: b"sk-test")
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/identity/get":
            return {"identity": _plain_identity()}, ""
        if path == "/v1/chat/history":
            return {"messages": [], "context_memories": []}, ""
        return {}, ""

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "Feedling hosted runtime's background execution controller" in joined:
            return {
                "reply": json.dumps({
                    "actions": [{
                        "type": "identity.patch",
                        "confidence": 0.96,
                        "payload": {"agent_name": "小秘"},
                        "reason": "User asked the agent to rename itself.",
                    }]
                }),
                "usage": {"total_tokens": 3},
            }
        return {"reply": "收到，我以后就叫小秘。", "usage": {"total_tokens": 9}}

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_context)
    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "call yourself 小秘", "state_sync": True},
        headers=_headers(api_key),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["reply"] == "收到，我以后就叫小秘。"
    assert body["effects"] == []
    assert body["identity_actions"] == []
    assert body["state"]["background_execution"]["status"] == "completed"
    assert any(isinstance(item, dict) and item.get("agent_name") == "小秘" for item in captured_plaintexts)


@pytest.mark.xfail(reason="inline background runtime removed in chat-send 收口 (Task 3); behavior moved to agent-runner consumer — needs consumer-side coverage", strict=False)
def test_model_api_chat_background_runtime_updates_relationship_days(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", lambda envelope, key, purpose: b"sk-test")
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/identity/get":
            return {"identity": _plain_identity()}, ""
        if path == "/v1/chat/history":
            return {"messages": [], "context_memories": []}, ""
        return {}, ""

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "Feedling hosted runtime's background execution controller" in joined:
            return {
                "reply": json.dumps({
                    "actions": [{
                        "type": "identity.relationship_days_set",
                        "confidence": 0.97,
                        "payload": {"days_with_user": 68},
                        "reason": "User corrected the displayed relationship day count from 368 to 68.",
                    }]
                }),
                "usage": {},
            }
        return {"reply": "你说得对，我会按 68 天记。", "usage": {}}

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_context)
    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "我们在一起不是 368 天，是 68 天。", "state_sync": True},
        headers=_headers(api_key),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["state"]["background_execution"]["status"] == "completed"
    saved = db.get_blob(user_id, "identity")
    assert saved["relationship_anchor_source"] == "user_calibrated"
    assert identity_service._live_days_with_user(saved, store=core_store.get_store(user_id)) == 68


@pytest.mark.xfail(reason="inline background runtime removed in chat-send 收口 (Task 3); behavior moved to agent-runner consumer — needs consumer-side coverage", strict=False)
def test_model_api_chat_background_runtime_nudges_identity_dimension(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", lambda envelope, key, purpose: b"sk-test")
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/identity/get":
            return {"identity": _plain_identity()}, ""
        if path == "/v1/chat/history":
            return {"messages": [], "context_memories": []}, ""
        return {}, ""

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "Feedling hosted runtime's background execution controller" in joined:
            return {
                "reply": json.dumps({
                    "actions": [{
                        "type": "identity.dimension_nudge",
                        "confidence": 0.94,
                        "payload": {
                            "dimension": "Context retention",
                            "delta": 5,
                        },
                        "reason": "User asked to strengthen this identity dimension.",
                    }]
                }),
                "usage": {},
            }
        return {"reply": "好，我会更重视上下文连续性。", "usage": {}}

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_context)
    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "把 Context retention 调高一点。", "state_sync": True},
        headers=_headers(api_key),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["effects"] == []
    assert body["identity_actions"] == []
    assert body["state"]["background_execution"]["status"] == "completed"
    saved_identity = next(item for item in captured_plaintexts if isinstance(item, dict) and item.get("dimensions"))
    changed = next(dim for dim in saved_identity["dimensions"] if dim.get("name") == "Context retention")
    assert changed["value"] == 93


def test_memory_content_patch_reencrypts_existing_card(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_memory(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: json.dumps(_plain_memory()).encode("utf-8"),
    )
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    res = client.post(
        "/v1/memory/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "memory.content_patch",
                "memory_id": "mom_1",
                "patch": {"description": "User moved to Tokyo in April."},
                "reason": "User corrected this card.",
            }],
        },
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "ok"
    assert body["effects"][0]["type"] == "memory_superseded"
    assert body["effects"][0]["supersedes"] == "mom_1"
    assert captured_plaintexts[-1]["summary"] == "User moved to Tokyo in April."
    assert "User moved to Tokyo in April." in captured_plaintexts[-1]["content"]
    saved = db.memory_load(user_id)
    old_card = next(item for item in saved if item["id"] == "mom_1")
    new_card = next(item for item in saved if item["id"] != "mom_1")
    assert old_card["status"] == "superseded"
    assert old_card["superseded_by"] == new_card["id"]
    assert new_card["body_ct"] == "ct_1"
    assert new_card["status"] == "active"
    assert new_card["updated_at"]


@pytest.mark.xfail(reason="inline background runtime removed in chat-send 收口 (Task 3); behavior moved to agent-runner consumer — needs consumer-side coverage", strict=False)
def test_model_api_chat_background_runtime_executes_memory_context_patch(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    _seed_memory(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})

    def fake_decrypt(envelope, key, purpose):
        if purpose == "model_api_provider_key":
            return b"sk-test"
        return json.dumps(_plain_memory()).encode("utf-8")

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/identity/get":
            return {"identity": _plain_identity()}, ""
        if path == "/v1/chat/history":
            return {"messages": [], "context_memories": []}, ""
        return {}, ""

    calls = {"n": 0}

    def fake_chat_completion(cfg, messages, **kwargs):
        calls["n"] += 1
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "Feedling hosted runtime's background execution controller" in joined:
            return {
                "reply": json.dumps({
                    "actions": [{
                        "type": "memory.patch",
                        "confidence": 0.96,
                        "target": {"memory_id": "mom_1"},
                        "payload": {"patch": {"description": "User moved to Tokyo in April."}},
                        "reason": "User corrected the selected memory card.",
                    }]
                }),
                "usage": {},
            }
        if "Memory Capture worker" in joined:
            return {"reply": '{"memories":[]}', "usage": {}}
        return {"reply": "我把这条记忆改成东京了。", "usage": {}}

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_context)
    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    res = client.post(
        "/v1/model_api/chat/send",
        json={
            "message": "这张记忆写错了，改成 User moved to Tokyo in April.",
            "context_refs": [{"type": "memory", "id": "mom_1", "title": "Wrong city"}],
            "state_sync": True,
        },
        headers=_headers(api_key),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["effects"] == []
    assert body["memory_actions"] == []
    assert body["state"]["background_execution"]["status"] == "completed"
    assert any(
        isinstance(item, dict)
        and item.get("summary") == "User moved to Tokyo in April."
        and "User moved to Tokyo in April." in item.get("content", "")
        for item in captured_plaintexts
    )
    assert body["context"]["context_refs"] == 1


@pytest.mark.xfail(reason="inline background runtime removed in chat-send 收口 (Task 3); behavior moved to agent-runner consumer — needs consumer-side coverage", strict=False)
def test_model_api_chat_background_runtime_writes_general_correction_memory(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    captured_plaintexts: list = []
    context_params: list[dict] = []

    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", lambda envelope, key, purpose: b"sk-test")
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/identity/get":
            return {"identity": _plain_identity()}, ""
        if path == "/v1/chat/history":
            context_params.append(dict(params or {}))
            return {
                "messages": [],
                "context_memories": [{
                    "id": "mom_correction",
                    "title": "用户更新了 AI 设定",
                    "description": "以后不要再使用烂梗王设定。",
                    "source": "model_api_correction",
                }],
            }, ""
        return {}, ""

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "Feedling hosted runtime's background execution controller" in joined:
            return {
                "reply": json.dumps({
                    "actions": [{
                        "type": "memory.add_correction",
                        "confidence": 0.95,
                        "payload": {
                            "memory": {
                                "type": "fact",
                                "title": "用户更新了 AI 设定",
                                "description": "以后不要再使用烂梗王设定，语气改得温柔一点。",
                            }
                        },
                        "reason": "User corrected a durable agent behavior preference.",
                    }]
                }),
                "usage": {},
            }
        if "Memory Capture worker" in joined:
            return {"reply": '{"memories":[]}', "usage": {}}
        return {"reply": '{"reply":"我以后不会再用这个设定。","thinking_summary":"记下了这条纠正。"}', "usage": {}}

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_context)
    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "以后不要再用烂梗王设定，改成温柔一点。", "state_sync": True},
        headers=_headers(api_key),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["reply"] == "我以后不会再用这个设定。"
    assert body["effects"] == []
    assert body["memory_actions"] == []
    assert body["state"]["background_execution"]["status"] == "completed"
    assert context_params[-1]["context_mode"] == "model_api"
    assert any(
        isinstance(item, dict)
        and "烂梗王" in item.get("content", "")
        for item in captured_plaintexts
    )
    saved = db.memory_load(user_id)
    assert any(item.get("source") == "model_api_correction" for item in saved)


@pytest.mark.xfail(reason="inline background runtime removed in chat-send 收口 (Task 3); behavior moved to agent-runner consumer — needs consumer-side coverage", strict=False)
def test_model_api_chat_background_runtime_patches_user_preferred_name(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", lambda envelope, key, purpose: b"sk-test")
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/identity/get":
            return {"identity": _plain_identity()}, ""
        if path == "/v1/chat/history":
            return {"messages": [], "context_memories": []}, ""
        return {}, ""

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "Feedling hosted runtime's background execution controller" in joined:
            return {
                "reply": json.dumps({
                    "actions": [{
                        "type": "identity.patch",
                        "confidence": 0.96,
                        "payload": {
                            "user_preferred_name": "Seven",
                            "do_not_say": ["老板"],
                        },
                        "reason": "User updated address preference.",
                    }]
                }),
                "usage": {},
            }
        return {"reply": '{"reply":"好，以后叫你 Seven。","thinking_summary":"更新了称呼偏好。"}', "usage": {}}

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_context)
    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "以后不要叫我老板，叫我 Seven。", "state_sync": True},
        headers=_headers(api_key),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["effects"] == []
    assert body["state"]["background_execution"]["status"] == "completed"
    assert any(
        isinstance(item, dict)
        and item.get("user_preferred_name") == "Seven"
        and item.get("do_not_say") == ["老板"]
        for item in captured_plaintexts
    )


@pytest.mark.xfail(reason="inline background runtime removed in chat-send 收口 (Task 3); behavior moved to agent-runner consumer — needs consumer-side coverage", strict=False)
def test_model_api_chat_low_confidence_memory_delete_requires_confirmation(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    _seed_memory(user_id, memory_id="mom_delete")
    captured_plaintexts: list = []

    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})

    def fake_decrypt(envelope, key, purpose):
        if purpose == "model_api_provider_key":
            return b"sk-test"
        return json.dumps({
            "title": "烧卖和蒸饺设定",
            "description": "用户以前说过烧卖和蒸饺。",
            "type": "fact",
        }).encode("utf-8")

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/identity/get":
            return {"identity": _plain_identity()}, ""
        if path == "/v1/chat/history":
            return {"messages": [], "context_memories": []}, ""
        return {}, ""

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "Feedling hosted runtime's background execution controller" in joined:
            if "确认" in joined:
                return {
                    "reply": json.dumps({
                        "pending_decision": {
                            "decision": "confirm",
                            "pending_ids": [],
                            "reason": "User confirmed the pending delete action.",
                        },
                        "actions": [],
                    }),
                    "usage": {},
                }
            return {
                "reply": json.dumps({
                    "actions": [{
                        "type": "memory.delete",
                        "confidence": 0.7,
                        "target": {"memory_id": "mom_delete"},
                        "reason": "User may be asking to delete this setting.",
                    }]
                }),
                "usage": {},
            }
        if "Feedling has NOT applied the pending Identity/Memory update yet" in joined:
            assert "烧卖和蒸饺设定" in joined
            return {
                "reply": json.dumps({
                    "reply": "我先捏住这条不删：`烧卖和蒸饺设定`。你点头回「确认」，我再把它从记忆里拿掉；不对就回「取消」。",
                    "thinking_summary": "等你确认删除目标。",
                }, ensure_ascii=False),
                "usage": {},
            }
        return {"reply": "已处理。", "usage": {}}

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_context)
    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    first = client.post(
        "/v1/model_api/chat/send",
        json={"message": "忘掉烧卖和蒸饺那个设定。", "state_sync": True},
        headers=_headers(api_key),
    )
    assert first.status_code == 200, first.get_data(as_text=True)
    first_body = first.get_json()
    assert first_body["state"]["background_execution"]["status"] == "pending_confirmation"
    assert first_body["state"]["pending"]
    assert first_body["state"]["pending"][0]["target"] == "烧卖和蒸饺设定"
    assert len(db.memory_load(user_id)) == 1
    assert any(
        isinstance(item, str) and "确认" in item and "烧卖和蒸饺设定" in item
        for item in captured_plaintexts
    )

    second = client.post(
        "/v1/model_api/chat/send",
        json={"message": "确认", "state_sync": True},
        headers=_headers(api_key),
    )
    assert second.status_code == 200, second.get_data(as_text=True)
    second_body = second.get_json()
    assert second_body["effects"] == []
    assert second_body["state"]["background_execution"]["status"] == "completed"
    assert len(db.memory_load(user_id)) == 0


@pytest.mark.xfail(reason="inline background runtime removed in chat-send 收口 (Task 3); behavior moved to agent-runner consumer — needs consumer-side coverage", strict=False)
def test_model_api_chat_skips_running_capture_on_ordinary_turn_until_cadence(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"reply": "ok", "usage": {}})
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", lambda envelope, key, purpose: b"sk-test")
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    def fake_enclave_context(path, key, params=None):
        if path == "/v1/identity/get":
            return {"identity": _plain_identity()}, ""
        if path == "/v1/chat/history":
            return {"messages": [], "context_memories": []}, ""
        return {}, ""

    provider_calls: list[str] = []

    def fake_chat_completion(cfg, messages, **kwargs):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        provider_calls.append(joined)
        return {"reply": "今天可以简单吃点。", "usage": {}}

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_context)
    monkeypatch.setattr(provider_client, "chat_completion", fake_chat_completion)

    setup = client.post(
        "/v1/model_api/setup",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "sk-test"},
        headers=_headers(api_key),
    )
    assert setup.status_code == 200, setup.get_data(as_text=True)

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "今天吃什么？"},
        headers=_headers(api_key),
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["capture"]["status"] == "skipped"
    assert body["capture"]["reason"].startswith("cadence:")
    assert len(provider_calls) == 1
    assert "Feedling hosted runtime's background execution controller" not in provider_calls[0]


def test_identity_profile_patch_writes_custom_persona_prompt(client, monkeypatch):
    # P1b: the user-authored custom_persona_prompt is a first-class profile field,
    # writable via profile_patch (the path iOS / corrections use) and round-tripped
    # into the re-encrypted identity body. (DB-backed — runs in CI.)
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    captured_plaintexts: list = []

    monkeypatch.setattr(
        core_enclave,
        "_enclave_get_json_for_gate",
        lambda path, key, params=None: ({"identity": _plain_identity()}, "") if path == "/v1/identity/get" else ({}, ""),
    )
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured_plaintexts))

    directive = "像老朋友一样直接损我，别用敬语。"
    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "identity.profile_patch",
                "patch": {"custom_persona_prompt": directive},
                "reason": "User set a custom persona directive.",
            }],
        },
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "ok"
    assert "custom_persona_prompt" in body["effects"][0]["fields"]
    assert captured_plaintexts[-1]["custom_persona_prompt"] == directive


def test_proactive_settings_wake_directive_validation(client):
    # P4: wake_directive is whitelisted + capped at 1000 chars. wake_interval_sec
    # is whitelisted and clamped now that the tick loop contract carries it.
    # Unknown keys are still rejected. (DB-backed — CI.)
    user_id, _api_key = _register(client)
    store = core_store.get_store(user_id)
    saved = store.save_proactive_settings({
        "wake_directive": "x" * 2000,
        "wake_interval_sec": 100,
        "bogus_key": "nope",
    })
    assert len(saved["wake_directive"]) == 1000          # capped
    assert saved["wake_interval_sec"] == 900             # clamped
    assert "bogus_key" not in saved                      # unknown keys rejected
    assert store.load_proactive_settings()["wake_directive"] == "x" * 1000
    assert store.load_proactive_settings()["wake_interval_sec"] == 900
