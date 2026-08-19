from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, keys, readside  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402
from enclave.routes import chat as chat_route  # noqa: E402


def _moment(mid: str, title: str, description: str, *, linked: str = "") -> dict:
    return {
        "id": mid,
        "title": title,
        "description": description,
        "type": "fact",
        "source": "test",
        "occurred_at": "2026-06-21T10:00:00",
        "created_at": "2026-06-21T10:00:00",
        "her_quote": "",
        "context": "",
        "linked_dimension": linked,
    }


def _moment_envelope(mid: str) -> dict:
    """Envelope shape as returned by /v1/memory/list — the plaintext moment
    content lives behind a faked envelope.decrypt_envelope, not here."""
    return {
        "id": mid,
        "occurred_at": "2026-06-21T10:00:00",
        "created_at": "2026-06-21T10:00:00",
        "source": "test",
        "v": 1,
        "visibility": "shared",
    }


def _inner_json(title: str, description: str, *, linked: str = "") -> bytes:
    return json.dumps({
        "title": title,
        "description": description,
        "type": "fact",
        "her_quote": "",
        "context": "",
        "linked_dimension": linked,
    }).encode("utf-8")


def test_routeb_readside_helper_selects_index_then_returns_old_context_shape():
    moments = [
        _moment(
            "mem_cat",
            "猫咪照顾",
            "用户聊猫咪健康问题时，先需要被安抚，再给观察饮水和精神状态的建议。",
            linked="猫咪",
        ),
        _moment(
            "mem_lark",
            "Lark 工作流",
            "用户希望 agent 帮忙读 Lark 群消息并整理重点。",
            linked="Lark",
        ),
    ]

    selected, trace = readside.select_context_memories_via_readside(
        moments,
        "猫咪最近不吃饭，我有点担心",
    )

    assert [item["id"] for item in selected] == ["mem_cat"]
    assert selected[0]["title"] == "猫咪照顾"
    assert "summary" not in selected[0]
    assert trace["mode"] == "model_api_readside_v1"
    assert trace["readside_enabled"] is True
    assert trace["index_count"] == 2
    assert trace["selected"][0]["id"] == "mem_cat"


@pytest.fixture()
def enclave_history_client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()

    from enclave import envelope as envmod

    async def fake_backend_get(path, headers, params=None):
        assert headers.get("X-API-Key") == "key_routeb"
        if path == "/v1/users/whoami":
            return {"user_id": "usr_routeb"}
        if path == "/v1/chat/history":
            return {
                "messages": [
                    {
                        "id": "chat_1",
                        "role": "user",
                        "ts": 1,
                        "v": 1,
                        "visibility": "shared",
                        "content_type": "text",
                    }
                ],
                "total": 1,
            }
        assert path == "/v1/memory/list"
        return {
            "moments": [_moment_envelope("mem_cat"), _moment_envelope("mem_lark")],
            "total": 2,
        }
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    def fake_decrypt_envelope(env, uid, sk):
        eid = env.get("id")
        if eid == "chat_1":
            return "猫咪最近不吃饭".encode("utf-8")
        if eid == "mem_cat":
            return _inner_json("猫咪照顾",
                               "用户聊猫咪健康问题时，先需要被安抚，再给观察饮水和精神状态的建议。",
                               linked="猫咪")
        if eid == "mem_lark":
            return _inner_json("Lark 工作流",
                               "用户希望 agent 帮忙读 Lark 群消息并整理重点。",
                               linked="Lark")
        raise AssertionError(f"unexpected envelope id {eid}")

    monkeypatch.setattr(envmod, "decrypt_envelope", fake_decrypt_envelope)

    return _AsgiTestClient(build_app())


def test_routeb_flag_false_keeps_legacy_context_selection(enclave_history_client, monkeypatch):
    monkeypatch.delenv("MEMORY_READSIDE_FOR_MODEL_API", raising=False)
    monkeypatch.setattr(
        chat_route,
        "select_context_memories_with_trace",
        lambda moments, latest, mode="": ([{"id": "legacy", "title": "legacy"}], {"mode": "model_api"}),
    )
    monkeypatch.setattr(
        readside,
        "select_context_memories_via_readside",
        lambda *_args, **_kwargs: pytest.fail("readside should not run when flag is false"),
    )

    res = enclave_history_client.get(
        "/v1/chat/history?context_mode=model_api&context_trace=1",
        headers={"X-API-Key": "key_routeb"},
    )

    assert res.status_code == 200
    body = res.get_json()
    assert body["context_memories"] == [{"id": "legacy", "title": "legacy"}]
    assert body["context_memory_trace"]["mode"] == "model_api"


def test_model_api_now_uses_the_same_selection_as_resident(enclave_history_client, monkeypatch):
    """2026-08-18 统一：两条 runtime 走**同一套**挑卡，开关不再影响挑法。

    hx 拍板「v2 的挑卡参考 v1 的，就是挑桶那种」，目标是用户切 runtime 无感。

    此前这条测试断言的是反面 ——「开关为真时不许走老 selector」。那个分叉
    经查并非有意设计：2026-06-21 换管道时顺手丢掉了打底逻辑，当次设计文档
    对「转折/最近/打底」一个字未提，代码注释也没有取舍记录。
    """
    monkeypatch.setenv("MEMORY_READSIDE_FOR_MODEL_API", "true")

    res = enclave_history_client.get(
        "/v1/chat/history?context_mode=model_api&context_trace=1",
        headers={"X-API-Key": "key_routeb"},
    )

    assert res.status_code == 200
    body = res.get_json()
    # 开关仍为真，但挑法已统一 —— trace 不再是 readside 那套的形状
    assert body["context_memory_trace"].get("mode") != "model_api_readside_v1", (
        "model_api 仍在走 readside 挑法 —— 统一没生效"
    )


def test_routeb_readside_uses_configurable_memory_limit(enclave_history_client, monkeypatch):
    captured_limits = []

    from enclave import envelope as envmod

    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_routeb"}
        if path == "/v1/chat/history":
            return {"messages": [], "total": 0}
        assert path == "/v1/memory/list"
        captured_limits.append(int(params["limit"]))
        return {"moments": [_moment_envelope("mem_cat")], "total": 1}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    monkeypatch.setattr(
        envmod, "decrypt_envelope",
        lambda e, u, s: _inner_json("猫咪照顾", "用户聊猫咪健康问题时，先需要被安抚。", linked="猫咪"),
    )

    monkeypatch.delenv("MEMORY_READSIDE_FOR_MODEL_API", raising=False)
    enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1",
                              headers={"X-API-Key": "key_routeb"})
    assert captured_limits[-1] == 200

    monkeypatch.setenv("MEMORY_READSIDE_FOR_MODEL_API", "true")
    # 2026-08-18 统一之后默认值从 50 提到 200（resident 一直在用的值）——
    # 挑法一样但候选池 50 vs 200 的话，「切 runtime 无感」仍不成立：
    # 卡多的用户在小池子里会漏掉旧卡。旋钮本身保留，只是默认值变了。
    monkeypatch.delenv("MEMORY_READSIDE_MODEL_API_LIMIT", raising=False)
    enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1",
                              headers={"X-API-Key": "key_routeb"})
    assert captured_limits[-1] == 200

    monkeypatch.setenv("MEMORY_READSIDE_MODEL_API_LIMIT", "80")
    enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1",
                              headers={"X-API-Key": "key_routeb"})
    assert captured_limits[-1] == 80

    monkeypatch.setenv("MEMORY_READSIDE_MODEL_API_LIMIT", "999")
    enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1",
                              headers={"X-API-Key": "key_routeb"})
    assert captured_limits[-1] == 200
