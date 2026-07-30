"""聊天回合失败元信息随兜底回复下发（spec 2026-07-18 §2）。

Run: uv run --quiet pytest tests/test_chat_turn_failure_fields.py -q
"""
from __future__ import annotations

import base64
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from bootstrap import gates as boot_gates  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    # This module is about the extra dict shape POST /v1/chat/response writes,
    # not about the resident-consumer bootstrap gate — bypass it (same pattern
    # as tests/test_chat_route_debug_trace.py::test_resident_chat_response_
    # emits_route_trace) so these tests don't need to spin up a fake poller.
    monkeypatch.setattr(
        boot_gates,
        "_gate_bootstrap_for_chat",
        lambda store, allow_verify_reply=False, is_verify_reply=False: None,
    )
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


def _env(user_id: str, marker: str) -> dict:
    # visibility + owner_user_id are required by _ENVELOPE_REQUIRED (chat_core.py)
    # — the brief's helper omitted them, which 400s at envelope_missing_fields;
    # shape corrected against tests/test_asgi_chat_remaining.py::_env.
    return {
        "v": 1,
        "id": marker,
        "body_ct": _b64(f"{user_id}:{marker}".encode()),
        "nonce": _b64(b"\x02" * 12),
        "K_user": _b64(b"\x03" * 48),
        "K_enclave": _b64(b"\x04" * 48),
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def _send_user_msg(client, user_id: str, api_key: str, marker: str = "u1") -> str:
    """返回新建用户消息的 id。POST /v1/chat/message 的返回体是 {"id", "ts", "v"}
    （backend/chat/chat_core.py:write_message；路由见 backend/chat/routes_asgi.py
    的 `@router.post("/v1/chat/message")`——代码里没有 `/v1/chat/send` 这个路径,
    这里按实际路由改正)。"""
    res = client.post(
        "/v1/chat/message",
        json={"envelope": _env(user_id, marker), "client_msg_id": str(uuid.uuid4())},
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)
    return str(res.get_json()["id"])


def _history(client, api_key: str) -> list[dict]:
    res = client.get("/v1/chat/history?limit=50", headers=_headers(api_key))
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()["messages"]


def test_fallback_reply_carries_turn_failure_and_parent_link(client):
    """兜底回复是实时载体：必须同时带 turn_failure_* 与 reply_to_message_id，
    否则客户端无法在增量流里配对回它失败的那条用户消息。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key)

    res = client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r1"),
            "source": "chat",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "quota_insufficient",
            "turn_failure_blame": "user_provider",
            "turn_failure_user_text": "模型服务额度不足，充值后再发消息即可恢复。",
            "turn_failure_model": "openai/gpt-vision",
            "turn_failure_provider": "openrouter",
        },
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)

    msgs = _history(client, api_key)
    reply = [m for m in msgs if m.get("role") == "openclaw"][-1]
    assert reply["turn_failure_error_class"] == "quota_insufficient"
    assert reply["turn_failure_blame"] == "user_provider"
    assert reply["turn_failure_user_text"].startswith("模型服务额度不足")
    assert reply["reply_to_message_id"] == parent_id
    assert reply["turn_failure_model"] == "openai/gpt-vision"
    assert reply["turn_failure_provider"] == "openrouter"


def test_normal_reply_has_no_turn_failure_fields(client):
    """成功路径零变化：不带 turn_failure_* 的回复不得凭空出现这些键。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key)

    res = client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r2"),
            "source": "chat",
            "reply_to_message_id": parent_id,
        },
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)

    reply = [m for m in _history(client, api_key) if m.get("role") == "openclaw"][-1]
    assert "turn_failure_error_class" not in reply
    assert "turn_failure_blame" not in reply
    assert "turn_failure_user_text" not in reply


def test_user_text_is_server_authored_not_payload(client):
    """归责红线的执行点：blame 与 user_text 由服务端按 error_class 查 catalog，
    **不信 payload**。

    透传 payload 意味着一个写错/被改的 consumer 能把我们自己的故障标成
    user_provider、或把任意 900 字（可能夹带 provider HTML、request id、敏感
    上下文）灌进用户可见文案。截断不是脱敏，注释和 OpenAPI 宣称的
    「服务端组好」必须真由服务端组。
    """
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key, "u_auth")

    res = client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r_auth"),
            "source": "chat",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "quota_insufficient",
            # 恶意/错误的 poster：把我们的锅标成用户的，并塞长文
            "turn_failure_blame": "system",
            "turn_failure_user_text": "x" * 900,
        },
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)

    reply = [m for m in _history(client, api_key) if m.get("role") == "openclaw"][-1]
    # catalog 说 quota_insufficient 是 user_provider —— payload 的 "system" 被忽略
    assert reply["turn_failure_blame"] == "user_provider"
    assert reply["turn_failure_user_text"] == "模型服务额度不足，充值后再发消息即可恢复。"
    assert "x" * 20 not in reply["turn_failure_user_text"]
    assert len(reply["turn_failure_user_text"]) <= 500


def test_payload_cannot_blame_user_for_our_failure(client):
    """反向：我们自己的错（turn_timeout=system），poster 谎称 user_provider
    也必须被服务端纠正回 system —— 不是他的错绝不能赖给他。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key, "u_blame")

    client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r_blame"),
            "source": "chat",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "turn_timeout",
            "turn_failure_blame": "user_provider",
            "turn_failure_user_text": "去重新保存你的 API Key",
        },
        headers=_headers(api_key),
    )

    reply = [m for m in _history(client, api_key) if m.get("role") == "openclaw"][-1]
    assert reply["turn_failure_blame"] == "system"
    for banned in ("API Key", "充值", "设置里"):
        assert banned not in reply["turn_failure_user_text"]


def test_parent_metadata_mirrors_turn_failure(client):
    """冗余持久化：用户消息 metadata 同写一份，供全量 history / 重启后恢复。
    兜底消息仍是权威载体（跨 worker 时 metadata 可能静默写失败）。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key)

    res = client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r4"),
            "source": "chat",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "auth_invalid",
            "turn_failure_blame": "user_provider",
            "turn_failure_user_text": "API Key 无效或已过期，请到设置里重新保存。",
        },
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)

    parent = [m for m in _history(client, api_key) if m.get("id") == parent_id][0]
    assert parent["reply_error_class"] == "auth_invalid"
    assert parent["reply_blame"] == "user_provider"
    assert parent["reply_user_text"].startswith("API Key")
    # 既有语义不得改变
    assert parent["reply_status"] == "replied"


def test_parent_metadata_absent_on_normal_reply(client):
    """成功回合不得写这些键（成功路径零变化）。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key)

    client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r5"),
            "source": "chat",
            "reply_to_message_id": parent_id,
        },
        headers=_headers(api_key),
    )

    parent = [m for m in _history(client, api_key) if m.get("id") == parent_id][0]
    assert "reply_error_class" not in parent
    assert parent["reply_status"] == "replied"


def test_turn_failure_reaches_incremental_since_feed(client):
    """端到端实证 spec §2.1 的核心断言：失败信息必须能通过 `since` 增量过滤。

    第 1 稿方案把失败只写在【用户消息 metadata】上，而增量拉取按消息原始 ts
    过滤——就地更新旧消息不产生新 ts，永远进不了增量流，真实效果是「杀掉 App
    重开才看得到失败态」，与「当场抛给用户」正好相反（Codex review 发现）。

    改成双载体后，兜底回复是【新消息、有新 ts】，必须能被 since 拉到，并且
    自带 reply_to_message_id 供客户端配对回失败的那条用户消息。
    """
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key, "u_since")

    # 客户端此刻的水位：用户消息已在本地，只会再拉 ts 之后的新消息
    hist = _history(client, api_key)
    since = max(float(m["ts"]) for m in hist)

    res = client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r_since"),
            "source": "chat",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "quota_insufficient",
            "turn_failure_blame": "user_provider",
            "turn_failure_user_text": "模型服务额度不足，充值后再发消息即可恢复。",
        },
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)

    inc = client.get(f"/v1/chat/history?since={since}&limit=50", headers=_headers(api_key))
    assert inc.status_code == 200, inc.get_data(as_text=True)
    msgs = inc.get_json()["messages"]

    carriers = [m for m in msgs if m.get("turn_failure_error_class")]
    assert carriers, "失败信息没进增量流——实时链路又断了（spec §2.1 的核心回归）"
    c = carriers[0]
    assert c["turn_failure_error_class"] == "quota_insufficient"
    assert c["turn_failure_blame"] == "user_provider"
    assert c["reply_to_message_id"] == parent_id, "拿到失败事件却无法配对回用户消息"


@pytest.mark.parametrize("source", ["heartbeat", "agent_initiated_proactive", "verify_ping"])
def test_background_lanes_never_carry_turn_failure(client, source):
    """红线门①：后台车道失败不进聊天流（Seven 2026-07-11）。

    心跳/主动/capture/dream 的失败对用户不可行动，进聊天流会被自己看不见的
    车道刷屏。consumer 侧已不打标，服务端再独立卡一道——这条目前只靠一个 and
    从句撑着，重构一改就静默失守，故用测试钉住。
    """
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key, f"u_{source[:6]}")

    client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, f"r_{source[:6]}"),
            "source": source,
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "quota_insufficient",
            "turn_failure_blame": "user_provider",
            "turn_failure_user_text": "额度不足",
        },
        headers=_headers(api_key),
    )

    for m in _history(client, api_key):
        assert "turn_failure_error_class" not in m, f"{source} 车道漏进了聊天流"
        assert "reply_error_class" not in m


def test_system_role_never_carries_turn_failure(client):
    """红线门②：role=system 的技术通知气泡不带这些字段——它本就不算对用户
    消息的回复（07-06 spec 的 role 审计表），带上会让客户端把通知误配成回合结果。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key, "u_sysrole")

    client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r_sysrole"),
            "source": "chat",
            "role": "system",
            "notice_kind": "upstream_error",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "quota_insufficient",
            "turn_failure_blame": "user_provider",
            "turn_failure_user_text": "额度不足",
        },
        headers=_headers(api_key),
    )

    for m in _history(client, api_key):
        assert "turn_failure_error_class" not in m
