"""Hosted ``/v1/model_api/chat/send`` Runtime V2 routing contracts."""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import provider_client  # noqa: E402
from accounts import registry as accounts_registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from bootstrap import gates as boot_gates  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from chat import chat_core  # noqa: E402
from hosted import chat_send_core  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402
from push import service as push_service  # noqa: E402
from conftest import configure_model_api_route  # noqa: E402


@pytest.fixture(autouse=True)
def _legacy_exact_job_shapes_profile_off(monkeypatch):
    """Keep exact routing queue counts on the profile-off contract."""
    monkeypatch.setenv("FEEDLING_V2_PROFILE_ENABLED", "0")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Most of this file's tests rely on setup's startup materialization
    # landing a fresh user on V2 with no explicit flip — the v2_only fleet
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
    monkeypatch.setattr(jobs_store, "workers_alive", lambda **kw: True)
    monkeypatch.setattr(jobs_store, "live_worker_capacity", lambda **kw: 4)
    monkeypatch.setattr(jobs_store, "inflight_job_count", lambda: 0)
    monkeypatch.setattr(jobs_store, "recent_mean_service_sec", lambda **kw: None)
    monkeypatch.setattr(chat_send_core.kill_switch, "turns_halted", lambda **kw: False)
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


def _mark_active_route_vision_ready(user_id: str) -> None:
    route = db.model_api_active_route(user_id)
    assert route is not None
    assert db.model_api_route_mark_vision_test(user_id, route["id"], status="ok")


def test_pre_v2_only_fresh_setup_sends_through_v2_without_admin_flip(
    client, monkeypatch
):
    """The iOS acceptance path: configure Pre and chat, with no user flip."""
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "v2_only")
    user_id, api_key = _register(client)
    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        _fake_envelope_builder(),
    )

    _setup_openrouter(client, api_key, monkeypatch)

    mode, state, generation = db.get_hosted_runtime_control_strict(user_id)
    assert (mode, state, generation) == ("db_action_v2", "v2", 3)
    assert jobs_store.get_wake_schedule(user_id) is not None

    monkeypatch.setattr(chat_send_core.jobs_store, "workers_alive", lambda **kw: True)
    monkeypatch.setattr(
        chat_send_core.jobs_store, "live_worker_capacity", lambda **kw: 4
    )
    monkeypatch.setattr(chat_send_core.jobs_store, "inflight_job_count", lambda: 0)
    monkeypatch.setattr(
        chat_send_core.jobs_store, "recent_mean_service_sec", lambda **kw: None
    )
    monkeypatch.setattr(
        chat_send_core.kill_switch, "turns_halted", lambda **kw: False
    )
    response = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello from iOS pre"},
        headers=_headers(api_key),
    )

    assert response.status_code == 202, response.get_data(as_text=True)
    assert response.get_json()["status"] == "processing"
    with db.get_pool().connection() as conn:
        jobs = conn.execute(
            "SELECT lane,status,reason FROM agent_jobs WHERE user_id=%s",
            (user_id,),
        ).fetchall()
    assert jobs == [("chat", "pending", "chat_send")]


def test_v2_only_setup_fails_loud_when_cutover_cannot_persist(
    client, monkeypatch
):
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "v2_only")
    _user_id, api_key = _register(client)
    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        _fake_envelope_builder(),
    )
    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    monkeypatch.setattr(
        hosted_config_store,
        "apply_hosted_runtime_policy",
        lambda store: (_ for _ in ()).throw(RuntimeError("control plane down")),
    )

    response = client.post(
        "/v1/model_api/setup",
        json={
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "api_key": "sk-or-test",
        },
        headers=_headers(api_key),
    )

    assert response.status_code == 503
    assert response.get_json() == {"error": "runtime_policy_unavailable"}


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


def test_resident_delivery_replay_returns_winner_without_duplicate_push(client, monkeypatch):
    user_id, api_key = _register(client)
    monkeypatch.setattr(
        boot_gates,
        "_gate_bootstrap_for_chat",
        lambda store, allow_verify_reply=False, is_verify_reply=False: None,
    )
    delivered = []
    monkeypatch.setattr(
        push_service,
        "_deliver_ai_message_push_if_background",
        lambda *args, **kwargs: delivered.append(kwargs) or {
            "push_decision": "sent",
            "push_reason": "test",
        },
    )

    store = core_store.get_store(user_id)
    real_mark_first_chat_ok = chat_core._maybe_mark_first_chat_ok
    mark_attempts = 0

    def _crash_window_then_self_heal(mark_store, parent_message_id):
        nonlocal mark_attempts
        mark_attempts += 1
        if mark_attempts == 1:
            # Model the process dying after the reply transaction commits but
            # before the separate idempotent activation marker is written.
            return None
        return real_mark_first_chat_ok(mark_store, parent_message_id)

    monkeypatch.setattr(
        chat_core,
        "_maybe_mark_first_chat_ok",
        _crash_window_then_self_heal,
    )
    parent = store.append_chat(
        "user", "chat", _chat_envelope(user_id, "resident-replay-parent"))
    delivery_id = "a" * 32
    first_envelope = _chat_envelope(user_id, delivery_id)
    second_envelope = {
        **first_envelope,
        "body_ct": "fresh-ciphertext-for-same-semantic-delivery",
        "nonce": "fresh-nonce",
        "K_user": "fresh-user-key",
        "K_enclave": "fresh-enclave-key",
    }
    payload = {
        "envelope": first_envelope,
        "reply_to_message_id": parent["id"],
        "resident_delivery_id": delivery_id,
        "alert_body": "done",
    }

    first = client.post(
        "/v1/chat/response", json=payload, headers=_headers(api_key))
    assert store.proactive_activation_ready() is False
    replay = client.post(
        "/v1/chat/response",
        json={**payload, "envelope": second_envelope},
        headers=_headers(api_key),
    )

    assert first.status_code == 200, first.get_data(as_text=True)
    assert replay.status_code == 200, replay.get_data(as_text=True)
    assert replay.get_json() == first.get_json()
    assert mark_attempts == 2
    assert store.proactive_activation_ready() is True
    assert len(delivered) == 1
    stored = [msg for msg in store.chat_messages if msg.get("id") == delivery_id]
    assert len(stored) == 1
    assert stored[0]["body_ct"] == first_envelope["body_ct"]
    parent_row = next(msg for msg in store.chat_messages if msg["id"] == parent["id"])
    assert parent_row["reply_message_id"] == delivery_id


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


def test_send_configured_routes_to_v2_worker_pool(client, monkeypatch):
    """A configured OpenRouter turn is durably queued for Runtime V2."""
    user_id, api_key = _register(client)

    # 假 envelope 用于 setup（加密 api_key）和 chat/send（加密用户消息）
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_openrouter(client, api_key, monkeypatch)

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello"},
        headers=_headers(api_key),
    )
    assert res.status_code == 202, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "processing"
    assert body["runtime"]["driver"] == "pi"
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_jobs WHERE user_id=%s", (user_id,)
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("request_value", "expected"),
    [("missing", False), (True, True), (False, False)],
)
def test_send_persists_strict_include_reasoning_on_v2_turn(
    client, monkeypatch, request_value, expected
):
    user_id, api_key = _register(client)
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder()
    )
    _setup_openrouter(client, api_key, monkeypatch)
    payload = {"message": "hello"}
    if request_value != "missing":
        payload["include_reasoning"] = request_value

    response = client.post(
        "/v1/model_api/chat/send", json=payload, headers=_headers(api_key)
    )

    assert response.status_code == 202, response.get_data(as_text=True)
    row_id = response.get_json()["user_message"]["id"]
    row = next(
        item
        for item in core_store.get_store(user_id).chat_messages
        if item.get("id") == row_id
    )
    assert row["include_reasoning"] is expected


@pytest.mark.parametrize("invalid", [None, 1, "true", [], {}])
def test_send_rejects_non_boolean_include_reasoning_on_v2(
    client, monkeypatch, invalid
):
    user_id, api_key = _register(client)
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder()
    )
    _setup_openrouter(client, api_key, monkeypatch)

    response = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello", "include_reasoning": invalid},
        headers=_headers(api_key),
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "invalid_include_reasoning",
        "detail": "include_reasoning must be a boolean",
    }
    store = core_store._stores.get(user_id)
    assert store is None or not [
        row for row in store.chat_messages if row.get("role") == "user"
    ]


def test_send_client_msg_id_deduplicates_hosted_and_cross_route_retry(client, monkeypatch):
    """The incident path (hosted send, lost response, resident-route retry)
    must keep one user row and return the first row pointer from both APIs."""
    user_id, api_key = _register(client)
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder()
    )
    _setup_openrouter(client, api_key, monkeypatch)
    key = "5968A42D-D06A-4B9B-B183-8BCB47E44CB4"

    first = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello", "client_msg_id": key},
        headers=_headers(api_key),
    )
    cross_route = client.post(
        "/v1/chat/message",
        json={
            "envelope": _chat_envelope(user_id, "retry-new-envelope"),
            "client_msg_id": key.lower(),
        },
        headers=_headers(api_key),
    )
    hosted_retry = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello", "client_msg_id": key.lower()},
        headers=_headers(api_key),
    )

    assert first.status_code == hosted_retry.status_code == 202
    assert cross_route.status_code == 200
    first_body = first.get_json()
    retry_body = hosted_retry.get_json()
    assert retry_body == first_body
    assert cross_route.get_json()["id"] == first_body["user_message"]["id"]
    store = core_store.get_store(user_id)
    rows = [
        row for row in store.chat_messages
        if row.get("role") == "user" and row.get("client_msg_id") == key.lower()
    ]
    assert len(rows) == 1
    assert rows[0]["source"] == "model_api"


def test_send_rejects_invalid_client_msg_id_before_append(client):
    user_id, api_key = _register(client)
    response = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello", "client_msg_id": "not-a-uuid"},
        headers=_headers(api_key),
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "client_msg_id_invalid",
        "detail": "client_msg_id must be a UUID string",
    }
    store = core_store._stores.get(user_id)
    assert store is None or not [
        row for row in store.chat_messages if row.get("role") == "user"
    ]


def test_send_duplicate_stays_poll_based_after_assistant_exists(client, monkeypatch):
    user_id, api_key = _register(client)
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder()
    )
    _setup_openrouter(client, api_key, monkeypatch)
    key = "13fc8cc4-45f8-4867-8e98-bcfcc89e3b1f"
    first = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello", "client_msg_id": key},
        headers=_headers(api_key),
    )
    assert first.status_code == 202
    assert first.get_json()["reply_ready"] is False

    store = core_store.get_store(user_id)
    parent_id = first.get_json()["user_message"]["id"]
    reply = store.append_chat(
        "openclaw", "model_api", _chat_envelope(user_id, "already-replied")
    )
    store.update_chat_message_metadata(
        parent_id,
        {"reply_status": "replied", "reply_message_id": reply["id"]},
    )

    duplicate = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello", "client_msg_id": key},
        headers=_headers(api_key),
    )

    assert duplicate.status_code == 202
    body = duplicate.get_json()
    assert body["reply_ready"] is False
    assert body["user_message"]["id"] == parent_id
    assert "assistant_message" not in body
    assert len([
        row for row in store.chat_messages
        if row.get("client_msg_id") == key
    ]) == 1


def test_send_image_turn_also_routes(client, monkeypatch):
    """Image turns enter the same Runtime V2 queue."""
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_openrouter(client, api_key, monkeypatch)
    _mark_active_route_vision_ready(user_id)

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
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_jobs WHERE user_id=%s", (user_id,)
        ).fetchone()[0] == 1


def test_send_image_turn_persists_real_mime(client, monkeypatch):
    """PNG 图片 turn 的真实 MIME 被持久化到 chat row——enclave history 透传给
    consumer 后才不会把 PNG/WebP 误当 JPEG（Codex P2 修复）。"""
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_openrouter(client, api_key, monkeypatch)
    _mark_active_route_vision_ready(user_id)
    tiny_png_b64 = _b64(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "", "image_b64": tiny_png_b64, "image_mime": "image/png"},
        headers=_headers(api_key),
    )
    assert res.status_code == 202, res.get_data(as_text=True)
    row_id = res.get_json()["user_message"]["id"]
    captured = next(
        row for row in core_store.get_store(user_id).chat_messages
        if row.get("id") == row_id
    )
    # chat row 必须带上真实 MIME（白名单透传），而非默认 jpeg。
    assert captured.get("image_mime") == "image/png", (
        f"chat row 应持久化真实 MIME，实际 {captured.get('image_mime')!r}"
    )


def test_send_503_when_v2_workers_not_live(client, monkeypatch):
    """A dead pooled worker fleet fails before persisting the user turn."""
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_openrouter(client, api_key, monkeypatch)
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-or-test",
    )
    monkeypatch.setattr(jobs_store, "workers_alive", lambda **kw: False)

    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello"},
        headers=_headers(api_key),
    )
    assert res.status_code == 503, res.get_data(as_text=True)
    body = res.get_json()
    assert body == {
        "error": "workers_unavailable",
        "reason": "no_live_v2_worker_heartbeat",
    }
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
    _mark_active_route_vision_ready(user_id)
    monkeypatch.setattr(
        core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-or-test",
    )

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
    row_id = res.get_json()["user_message"]["id"]
    row = next(
        item for item in core_store.get_store(user_id).chat_messages
        if item.get("id") == row_id
    )
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
    configure_model_api_route(
        user_id, provider="openrouter", model="openai/gpt-4o-mini", test_status="ok"
    )
    hosted_config_store.set_hosted_runtime_mode(
        core_store.get_store(user_id), "db_action_v2"
    )
    # Ownership can only be materialized for a supported active route. Model a
    # corrupt/unknown provider read after that fence to exercise the request's
    # fail-before-persist behavior.
    monkeypatch.setattr(
        hosted_config_store,
        "_load_model_api_config",
        lambda _store: {"provider": "bogus", "model": "x"},
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


def test_unified_v2_pool_accepts_anthropic_without_provider_tier_gate(client, monkeypatch):
    """All providers share one V2 loop; no resident CLI capability tier exists."""
    user_id, api_key = _register(client)

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    _setup_anthropic(client, api_key, monkeypatch)
    res = client.post(
        "/v1/model_api/chat/send",
        json={"message": "hello"},
        headers=_headers(api_key),
    )
    assert res.status_code == 202, res.get_data(as_text=True)
    assert res.get_json()["runtime"]["driver"] == "claude"
