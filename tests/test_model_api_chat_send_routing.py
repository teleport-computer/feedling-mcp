"""路由测试: /v1/model_api/chat/send 收口后的行为。

Task 3 删掉 inline 运行时后，每次 send 若 driver 能 resolve，则走
agent_runtime_cutover.handle_send；否则 409 provider_not_configured。
图片 turn 不再被 should_route 拦在 legacy 路径。

测试用 monkeypatch 替换 handle_send，不真正启动 consumer。
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client  # noqa: E402
from accounts import registry as accounts_registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from bootstrap import gates as boot_gates  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import agent_runtime_cutover  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
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
    # Default: a live supervisor so the wedge guard lets sends through. Tests that
    # exercise the guard's 503 path re-monkeypatch this to return not-live.
    monkeypatch.setattr(agent_runtime_cutover, "check_supervisor_live", lambda **kw: (True, ""))
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


def _fake_envelope_builder():
    """每次调用返回一个假的 envelope，不需要真实 enclave。"""
    counter = {"n": 0}

    def _build(store, plaintext: bytes, *, item_id: str | None = None):
        counter["n"] += 1
        return {
            "v": 1,
            "id": item_id or f"env_{counter['n']}",
            "body_ct": f"ct_{counter['n']}",
            "nonce": f"nonce_{counter['n']}",
            "K_user": f"k_user_{counter['n']}",
            "K_enclave": f"k_enclave_{counter['n']}",
            "visibility": "shared",
            "owner_user_id": getattr(store, "user_id", "test"),
            "enclave_pk_fpr": "test",
        }, ""

    return _build


def _chat_envelope(user_id: str, msg_id: str) -> dict:
    return {
        "v": 1,
        "id": msg_id,
        "body_ct": f"ct_{msg_id}",
        "nonce": f"nonce_{msg_id}",
        "K_user": f"k_user_{msg_id}",
        "K_enclave": f"k_enclave_{msg_id}",
        "visibility": "shared",
        "owner_user_id": user_id,
        "enclave_pk_fpr": "test",
    }


def _setup_openrouter(client, api_key: str, monkeypatch) -> None:
    """POST /v1/model_api/setup with provider=openrouter so DB has a valid config."""
    monkeypatch.setattr(
        provider_client, "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    res = client.post(
        "/v1/model_api/setup",
        json={"provider": "openrouter", "model": "openai/gpt-4o-mini", "api_key": "sk-or-test"},
        headers=_headers(api_key),
    )
    assert res.status_code == 200, res.get_data(as_text=True)


@pytest.mark.parametrize("source", ["chat", "model_api"])
def test_chat_response_marks_first_user_success_once_for_real_chat_sources(client, monkeypatch, source):
    user_id, api_key = _register(client)
    monkeypatch.setattr(
        boot_gates,
        "_gate_bootstrap_for_chat",
        lambda store, allow_verify_reply=False, is_verify_reply=False: None,
    )

    store = core_store.get_store(user_id)
    assert store.proactive_activation_ready() is False

    bad_user = store.append_chat("user", source, _chat_envelope(user_id, f"{source}-bad-user-1"))
    bad = client.post(
        "/v1/chat/response",
        json={"reply_to_message_id": bad_user["id"]},
        headers=_headers(api_key),
    )
    assert bad.status_code == 400
    assert store.proactive_activation_ready() is False

    user_msg = store.append_chat("user", source, _chat_envelope(user_id, f"{source}-user-1"))
    first = client.post(
        "/v1/chat/response",
        json={
            "envelope": _chat_envelope(user_id, f"{source}-assistant-1"),
            "reply_to_message_id": user_msg["id"],
        },
        headers=_headers(api_key),
    )
    assert first.status_code == 200, first.get_data(as_text=True)
    first_chat_ok_at = store.first_chat_ok_at()
    assert first_chat_ok_at

    second_user = store.append_chat("user", source, _chat_envelope(user_id, f"{source}-user-2"))
    second = client.post(
        "/v1/chat/response",
        json={
            "envelope": _chat_envelope(user_id, f"{source}-assistant-2"),
            "reply_to_message_id": second_user["id"],
        },
        headers=_headers(api_key),
    )
    assert second.status_code == 200, second.get_data(as_text=True)
    assert store.first_chat_ok_at() == first_chat_ok_at


def test_chat_response_does_not_mark_first_chat_ok_for_verify_ping(client, monkeypatch):
    user_id, api_key = _register(client)
    monkeypatch.setattr(
        boot_gates,
        "_gate_bootstrap_for_chat",
        lambda store, allow_verify_reply=False, is_verify_reply=False: None,
    )

    store = core_store.get_store(user_id)
    ping_user = store.append_chat("user", "verify_ping", _chat_envelope(user_id, "verify-ping-user-1"))

    reply = client.post(
        "/v1/chat/response",
        json={
            "envelope": _chat_envelope(user_id, "verify-ping-assistant-1"),
            "reply_to_message_id": ping_user["id"],
        },
        headers=_headers(api_key),
    )

    assert reply.status_code == 200, reply.get_data(as_text=True)
    assert store.proactive_activation_ready() is False


def test_send_configured_routes_to_agent_runner(client, monkeypatch):
    """配了 openrouter 的用户，send 应托管到 agent-runner，返回 202，
    且 handle_send 收到 driver=='codex'。"""
    user_id, api_key = _register(client)

    # 假 envelope 用于 setup（加密 api_key）和 chat/send（加密用户消息）
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_openrouter(client, api_key, monkeypatch)

    # 让 _load_runtime_provider_config 成功（绕过真实 enclave 解密）
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-or-test",
    )

    calls: list[str] = []

    def fake_handle_send(store, user_row, driver, **kwargs):
        calls.append(driver)
        return {"status": "processing"}, 202

    monkeypatch.setattr(agent_runtime_cutover, "handle_send", fake_handle_send)

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello"},
        headers=_headers(api_key),
    )
    assert res.status_code == 202, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "processing"
    assert calls == ["codex"], f"expected driver='codex', got {calls}"


def test_send_image_turn_also_routes(client, monkeypatch):
    """图片 turn 不再被 should_route 拦在 legacy，也走 agent-runner（返回 202）。"""
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_openrouter(client, api_key, monkeypatch)

    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-or-test",
    )

    calls: list[str] = []

    def fake_handle_send(store, user_row, driver, **kwargs):
        calls.append(driver)
        return {"status": "processing"}, 202

    monkeypatch.setattr(agent_runtime_cutover, "handle_send", fake_handle_send)

    # 最小 JPEG 头（2 字节），不需要完整图片，只要能通过 base64 解码即可
    tiny_image_b64 = _b64(b"\xff\xd8\xff\xe0" + b"\x00" * 10)

    res = client.post(
        "/v1/model_api/chat/send",
        json={
            "message": "",
            "image_b64": tiny_image_b64,
            "image_mime": "image/jpeg",
        },
        headers=_headers(api_key),
    )
    # 以前会被 should_route(has_image=True) 拦住走 inline → 现在直接 202
    assert res.status_code == 202, res.get_data(as_text=True)
    assert len(calls) == 1, f"handle_send 应被调用一次，实际 calls={calls}"


def test_send_image_turn_persists_real_mime(client, monkeypatch):
    """PNG 图片 turn 的真实 MIME 被持久化到 chat row——enclave history 透传给
    consumer 后才不会把 PNG/WebP 误当 JPEG（Codex P2 修复）。"""
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_openrouter(client, api_key, monkeypatch)
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-or-test",
    )

    captured: dict = {}

    def fake_handle_send(store, user_row, driver, **kwargs):
        captured["row"] = user_row
        return {"status": "processing"}, 202

    monkeypatch.setattr(agent_runtime_cutover, "handle_send", fake_handle_send)

    tiny_png_b64 = _b64(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "", "image_b64": tiny_png_b64, "image_mime": "image/png"},
        headers=_headers(api_key),
    )
    assert res.status_code == 202, res.get_data(as_text=True)
    # chat row 必须带上真实 MIME（白名单透传），而非默认 jpeg。
    assert captured["row"].get("image_mime") == "image/png", (
        f"chat row 应持久化真实 MIME，实际 {captured['row'].get('image_mime')!r}"
    )


def test_send_503_when_supervisor_not_live(client, monkeypatch):
    """配置正常，但 supervisor 心跳缺失/陈旧（其 consumer 不会接这条）时，
    send 必须 503 hosting_runtime_unavailable，且 **不** 调 handle_send、
    **不** 写孤儿用户消息（守卫在 append_chat 之前）。"""
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_openrouter(client, api_key, monkeypatch)
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-or-test",
    )
    # supervisor down → guard must short-circuit before routing
    monkeypatch.setattr(
        agent_runtime_cutover, "check_supervisor_live",
        lambda **kw: (False, "stale_supervisor_heartbeat_120s"),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        agent_runtime_cutover, "handle_send",
        lambda *a, **k: (calls.append("x"), ({"status": "processing"}, 202))[1],
    )

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello"},
        headers=_headers(api_key),
    )
    assert res.status_code == 503, res.get_data(as_text=True)
    body = res.get_json()
    assert body["error"] == "hosting_runtime_unavailable"
    assert body["reason"].startswith("stale_supervisor_heartbeat")
    assert calls == [], "supervisor down 时不应路由到 handle_send"
    # 守卫早于 append_chat，store 里不应有任何用户消息（无孤儿 turn）
    store = core_store._stores.get(user_id)
    if store:
        user_msgs = [m for m in store.chat_messages if m.get("role") == "user"]
        assert user_msgs == [], f"守卫触发后 store 不应有用户消息，实际: {user_msgs}"


def test_send_image_with_caption_persists_caption_envelope(client, monkeypatch):
    """带文字说明的图片 turn：caption_body_ct 必须持久化到 chat row。
    _chat_caption_extra_from_envelope 把 caption envelope 展平为 caption_*
    字段，store 白名单透传——enclave history 才能解出用户的问题文字。"""
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_openrouter(client, api_key, monkeypatch)
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-or-test",
    )

    captured: dict = {}

    def fake_handle_send(store, user_row, driver, **kwargs):
        captured["row"] = user_row
        return {"status": "processing"}, 202

    monkeypatch.setattr(agent_runtime_cutover, "handle_send", fake_handle_send)

    tiny_png_b64 = _b64(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
    res = client.post(
        "/v1/model_api/chat/send",
        json={
            "message": "这张图里是什么？",
            "image_b64": tiny_png_b64,
            "image_mime": "image/png",
        },
        headers=_headers(api_key),
    )
    assert res.status_code == 202, res.get_data(as_text=True)
    row = captured["row"]
    # caption envelope 字段必须持久化到 chat row
    assert row.get("caption_body_ct"), (
        f"chat row 应含 caption_body_ct，实际 row keys={list(row.keys())}"
    )
    # K_enclave 同样必须存在（enclave 解密依赖它）
    assert row.get("caption_K_enclave"), (
        f"chat row 应含 caption_K_enclave，实际 row keys={list(row.keys())}"
    )
    # 原有 image_mime 不受影响
    assert row.get("image_mime") == "image/png", (
        f"image_mime 应为 image/png，实际 {row.get('image_mime')!r}"
    )


def test_send_unsupported_provider_returns_409(client, monkeypatch):
    """driver 无法 resolve（provider 无 fit）时返回 409 provider_not_configured，
    且 **不** 写入孤儿用户消息（append_chat 在 resolve_driver 之后）。"""
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())

    # 让 _load_runtime_provider_config 返回成功（绕过 400 检查）
    fake_runtime = provider_client.ProviderConfig(
        provider="bogus", model="x", api_key="k"
    )
    monkeypatch.setattr(
        hosted_config_store,
        "_load_runtime_provider_config",
        lambda store, api_key, **kwargs: fake_runtime,
    )
    # _ensure_model_api_runtime_profile 在 line 355 调用，接受假 config 即可
    monkeypatch.setattr(
        hosted_config_store,
        "_ensure_model_api_runtime_profile",
        lambda store, config=None, **kwargs: None,
    )
    # 让 _load_model_api_config 返回一个 resolve_driver 会 raise 的 config
    monkeypatch.setattr(
        hosted_config_store,
        "_load_model_api_config",
        lambda store: {"provider": "bogus", "model": "x", "test_status": "ok"},
    )

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello"},
        headers=_headers(api_key),
    )
    assert res.status_code == 409, res.get_data(as_text=True)
    body = res.get_json()
    assert body["error"] == "provider_not_configured"


def _setup_anthropic(client, api_key: str, monkeypatch) -> None:
    """POST /v1/model_api/setup with provider=anthropic so DB has a valid config."""
    monkeypatch.setattr(
        provider_client, "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    res = client.post(
        "/v1/model_api/setup",
        json={"provider": "anthropic", "model": "claude-opus-4-5", "api_key": "sk-ant-test"},
        headers=_headers(api_key),
    )
    assert res.status_code == 200, res.get_data(as_text=True)


def test_gateway_off_does_not_block_anthropic_user(client, monkeypatch):
    """supervisor 心跳 gateway=False，但用户是 anthropic（非 gateway-transport）时，
    chat/send **不** 被 503——anthropic 走 claude driver，不经 LiteLLM gateway。"""
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_anthropic(client, api_key, monkeypatch)
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-ant-test",
    )
    # 模拟 "heartbeat 存在但 gateway=False" 的情形：
    # check_supervisor_live 收到 require_gateway=False → live；require_gateway=True → not-live。
    monkeypatch.setattr(
        agent_runtime_cutover, "check_supervisor_live",
        lambda *, require_gateway=True, **kw: (
            (True, "") if not require_gateway else (False, "supervisor_gateway_disabled")
        ),
    )
    calls: list[str] = []

    def fake_handle_send(store, user_row, driver, **kwargs):
        calls.append(driver)
        return {"status": "processing"}, 202

    monkeypatch.setattr(agent_runtime_cutover, "handle_send", fake_handle_send)

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello"},
        headers=_headers(api_key),
    )
    assert res.status_code == 202, (
        f"anthropic 用户在 gateway=False 时不应 503，实际: {res.status_code} {res.get_data(as_text=True)}"
    )
    assert calls == ["claude"], f"anthropic 用户应路由到 claude driver，实际 calls={calls}"


def test_gateway_off_blocks_openrouter_user(client, monkeypatch):
    """supervisor 心跳 gateway=False，用户是 openrouter（gateway-transport）时，
    chat/send 必须返回 503 supervisor_gateway_disabled。"""
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_openrouter(client, api_key, monkeypatch)
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-or-test",
    )
    # 模拟 "heartbeat 存在但 gateway=False"：require_gateway=True → not-live。
    monkeypatch.setattr(
        agent_runtime_cutover, "check_supervisor_live",
        lambda *, require_gateway=True, **kw: (
            (True, "") if not require_gateway else (False, "supervisor_gateway_disabled")
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        agent_runtime_cutover, "handle_send",
        lambda *a, **k: (calls.append("x"), ({"status": "processing"}, 202))[1],
    )

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello"},
        headers=_headers(api_key),
    )
    assert res.status_code == 503, (
        f"openrouter 用户在 gateway=False 时应 503，实际: {res.status_code} {res.get_data(as_text=True)}"
    )
    body = res.get_json()
    assert body["error"] == "hosting_runtime_unavailable"
    assert body["reason"] == "supervisor_gateway_disabled"
    assert calls == [], "gateway 阻断时不应路由到 handle_send"
