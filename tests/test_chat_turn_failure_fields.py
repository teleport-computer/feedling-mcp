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


def test_user_text_truncated_to_500(client):
    """契约：user_text ≤ 500，杜绝把原始 provider detail 灌进用户可见文案。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key)

    res = client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r3"),
            "source": "chat",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "unknown",
            "turn_failure_blame": "system",
            "turn_failure_user_text": "x" * 900,
        },
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)

    reply = [m for m in _history(client, api_key) if m.get("role") == "openclaw"][-1]
    assert len(reply["turn_failure_user_text"]) == 500


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
