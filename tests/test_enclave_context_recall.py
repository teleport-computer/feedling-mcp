"""Enclave chat-context recall, compatibility, and candidate-limit contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, keys, readside  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402
from enclave.routes import chat as chat_routes  # noqa: E402
from memory import memory_core  # noqa: E402


def _select_context(monkeypatch, cards, messages):
    monkeypatch.setattr(readside, "moments_to_cards", lambda *_: cards)
    monkeypatch.setenv("FEEDLING_MEMORY_RECALL_UNIFIED_RANKER", "1")
    return chat_routes._build_context_memories([], messages, {
        "want_trace": True, "authorized_user_id": "usr_recall", "content_sk": None,
    })


@pytest.mark.parametrize("agent_role", ["assistant", "agent", "openclaw"])
def test_user_entity_survives_long_assistant_topic(monkeypatch, agent_role):
    """Real IO projection + jieba + selector: last-four query loses this card."""
    topic = "相册 照片 视频 镜头 构图 曝光 色彩 胶片 摄影 拍摄 编辑 剪辑"
    cards = [{"id": "pet", "summary": "Mochi 是我养的猫。",
              "created_at": "2026-06-01T00:00:00"}]
    cards += [{"id": f"photo{i}", "summary": topic + f"方案{i}",
               "created_at": "2026-07-01T00:00:00"} for i in range(10)]
    cards += [{"id": f"neutral{i}", "summary": f"地铁站通勤路线 {i}",
               "created_at": "2026-05-01T00:00:00"} for i in range(50)]
    messages = [
        {"role": "human", "content": "看看这个"},
        {"role": agent_role, "content": topic * 10},
        {"role": "user", "content": "Mochi 今天也在旁边呢"},
        {"role": agent_role, "content": topic * 10},
    ]
    picked, trace, _ = _select_context(monkeypatch, cards, messages)
    assert "pet" in {card["id"] for card in picked}
    assert "pet" in {item["id"] for item in trace["selected"]}
    assert len(picked) <= 8


@pytest.mark.parametrize("latest", [
    {"role": "user", "content": ""},
    {"role": "human", "content": " \n "},
    {"role": "user", "content": None, "content_type": "image", "image_omitted": True},
    {"role": "user", "content": "", "content_type": "image", "image_b64": "AA=="},
    {"role": "human", "content": "你看看", "content_type": "image"},
])
def test_recall_query_uses_latest_two_nonempty_user_texts(monkeypatch, latest):
    seen = []
    original = chat_routes._unified_selection

    def observe(cards, query):
        seen.append(query)
        return original(cards, query)

    monkeypatch.setattr(chat_routes, "_unified_selection", observe)
    messages = [
        {"role": "user", "content": "更早的话题"},
        {"role": "human", "content": "上一条用户消息"},
        {"role": "assistant", "content": "不能成为查询"},
        {"role": "user", "content": "最近用户话题"},
        {"role": "agent", "content": "不能成为查询"},
        {"role": "openclaw", "content": "不能成为查询"},
        {"role": "system", "content": "不能成为查询"},
        latest,
    ]
    _select_context(monkeypatch, [], messages)
    assert seen == ["你看看\n最近用户话题" if latest.get("content") == "你看看"
                    else "最近用户话题\n上一条用户消息"]


def test_recall_query_has_no_assistant_only_fallback(monkeypatch):
    picked, _, log = _select_context(monkeypatch, [], [
        {"role": "assistant", "content": "Mochi 是你的猫"},
        {"role": "user", "content": None, "content_type": "image"},
    ])
    assert picked == []
    assert log["query_empty"] is True


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


def test_context_mode_compat_inputs_do_not_fork_selection(enclave_history_client):
    """Passing an old mode parameter must not restore the split selector."""
    query_strings = (
        "context_trace=1",
        "context_mode=model_api&context_trace=1",
        "context_strict=1&context_trace=1",
    )
    selected_ids = []

    for query_string in query_strings:
        res = enclave_history_client.get(
            f"/v1/chat/history?{query_string}",
            headers={"X-API-Key": "key_routeb"},
        )
        assert res.status_code == 200
        body = res.get_json()
        ids = [item["id"] for item in body["context_memories"]]
        assert "mem_cat" in ids
        assert "index_sample" not in body["context_memory_trace"]
        selected_ids.append(ids)

    assert selected_ids[1:] == selected_ids[:1] * 2


def test_context_recall_uses_configurable_memory_limit(enclave_history_client, monkeypatch):
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

    monkeypatch.delenv("MEMORY_READSIDE_MODEL_API_LIMIT", raising=False)
    enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1",
                              headers={"X-API-Key": "key_routeb"})
    assert captured_limits[-1] == readside.MEMORY_READSIDE_MODEL_API_DEFAULT_LIMIT

    configured = max(
        readside.MEMORY_READSIDE_MODEL_API_MIN_LIMIT,
        readside.MEMORY_READSIDE_MODEL_API_DEFAULT_LIMIT // 2,
    )
    monkeypatch.setenv("MEMORY_READSIDE_MODEL_API_LIMIT", str(configured))
    enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1",
                              headers={"X-API-Key": "key_routeb"})
    assert captured_limits[-1] == configured

    # Enclave 不再有第二套静默上限：越过 backend 支持边界的配置也原样发出，
    # 真实 backend 会以 invalid limit 显式拒绝（由 test_asgi_memory 守卫）。
    unsupported = memory_core.MEMORY_LIST_MAX_LIMIT + 1
    monkeypatch.setenv("MEMORY_READSIDE_MODEL_API_LIMIT", str(unsupported))
    enclave_history_client.get("/v1/chat/history?context_mode=model_api&context_trace=1",
                              headers={"X-API-Key": "key_routeb"})
    assert captured_limits[-1] == unsupported


def test_model_api_limit_default_uses_full_backend_supported_page():
    assert (
        readside.MEMORY_READSIDE_MODEL_API_DEFAULT_LIMIT
        == memory_core.MEMORY_LIST_MAX_LIMIT
    )


@pytest.mark.parametrize("raw", ["not-an-integer", "1.5"])
def test_model_api_limit_rejects_invalid_config(monkeypatch, raw):
    monkeypatch.setenv("MEMORY_READSIDE_MODEL_API_LIMIT", raw)

    with pytest.raises(ValueError, match="must be an integer"):
        readside.memory_readside_model_api_limit()


def test_model_api_limit_rejects_non_positive_config(monkeypatch):
    unsupported = readside.MEMORY_READSIDE_MODEL_API_MIN_LIMIT - 1
    monkeypatch.setenv("MEMORY_READSIDE_MODEL_API_LIMIT", str(unsupported))

    with pytest.raises(ValueError, match="must be positive"):
        readside.memory_readside_model_api_limit()


@pytest.mark.parametrize("raw", ["not-an-integer", "0"])
def test_context_recall_bad_memory_limit_leaves_failed_log(
    enclave_history_client, monkeypatch, raw
):
    monkeypatch.setenv("MEMORY_READSIDE_MODEL_API_LIMIT", raw)

    res = enclave_history_client.get(
        "/v1/chat/history?context_mode=model_api&context_trace=1",
        headers={"X-API-Key": "key_routeb"},
    )

    assert res.status_code == 200
    body = res.get_json()
    assert body["context_memories"] == []
    assert body["context_memory_log"] == {
        "mode": "failed",
        "error": "ValueError",
        "counts": {"candidate_pool": 0, "injected": 0},
    }


def test_context_recall_backend_limit_rejection_is_visible_as_failed_recall(
    enclave_history_client, monkeypatch
):
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_routeb"}
        if path == "/v1/chat/history":
            return {"messages": [], "total": 0}
        assert path == "/v1/memory/list"
        request = httpx.Request("GET", "https://backend.test/v1/memory/list")
        response = httpx.Response(
            400,
            request=request,
            json={"error": "invalid limit"},
        )
        raise httpx.HTTPStatusError(
            "invalid limit",
            request=request,
            response=response,
        )

    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    unsupported = memory_core.MEMORY_LIST_MAX_LIMIT + 1
    monkeypatch.setenv("MEMORY_READSIDE_MODEL_API_LIMIT", str(unsupported))

    res = enclave_history_client.get(
        "/v1/chat/history?context_mode=model_api&context_trace=1",
        headers={"X-API-Key": "key_routeb"},
    )

    assert res.status_code == 200
    body = res.get_json()
    assert body["context_memories"] == []
    assert body["context_memory_log"] == {
        "mode": "failed",
        "error": "HTTPStatusError",
        "counts": {"candidate_pool": 0, "injected": 0},
    }
