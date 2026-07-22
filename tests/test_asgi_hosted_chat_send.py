"""Flask/ASGI parity for the hosted Runtime V2 chat-send adapter.

Both adapters must return immediately after the encrypted user row and durable
V2 job are committed.  There is no hosted resident supervisor, reply wait
window, or send-time provider-key decrypt on this path.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
import provider_client  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import chat_routes_asgi  # noqa: E402
from hosted import chat_send_core  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


def _app_has_route(app, path: str) -> bool:
    def walk(routes) -> bool:
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None and walk(original.routes):
                return True
            if getattr(route, "path", None) == path:
                return True
        return False

    return walk(app.routes)


if not _app_has_route(asgi_app.app, "/v1/model_api/chat/send"):
    chat_routes_asgi.register_asgi(asgi_app.app)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _fake_envelope_builder():
    counter = {"n": 0}

    def build(store, plaintext: bytes, *, item_id: str | None = None):
        counter["n"] += 1
        n = counter["n"]
        return {
            "v": 1,
            "id": item_id or f"env_{n}",
            "body_ct": f"ct_{n}",
            "nonce": f"nonce_{n}",
            "K_user": f"k_user_{n}",
            "K_enclave": f"k_enclave_{n}",
            "visibility": "shared",
            "owner_user_id": getattr(store, "user_id", "test"),
            "enclave_pk_fpr": "test",
        }, ""

    return build


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # This module's happy-path fixtures never flip ownership through
    # config_store/admin — they rely on setup's startup materialization to
    # land a fresh user on V2 automatically. That's the v2_only contract
    # (apply_hosted_runtime_policy forces V2 fleet-wide); under the "dual"
    # default (the default since Task 5) a fresh user's per-user fence stays
    # resident until something explicitly flips it, and every send here would
    # 503 runtime_policy_not_ready. Pin v2_only so this file keeps exercising
    # that always-true-on-Pre contract (also the regression net for the
    # eventual v2_only-only retirement).
    monkeypatch.setenv(hosted_config_store.HOSTED_RUNTIME_POLICY_ENV, "v2_only")
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    monkeypatch.setattr(
        core_enclave,
        "_get_enclave_info",
        lambda: {"content_pk_hex": "22" * 32, "compose_hash": "test"},
    )
    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        _fake_envelope_builder(),
    )
    monkeypatch.setattr(jobs_store, "workers_alive", lambda **kw: True)
    monkeypatch.setattr(jobs_store, "live_worker_capacity", lambda **kw: 4)
    monkeypatch.setattr(jobs_store, "inflight_job_count", lambda: 0)
    monkeypatch.setattr(jobs_store, "recent_mean_service_sec", lambda **kw: None)
    monkeypatch.setattr(chat_send_core.kill_switch, "turns_halted", lambda **kw: False)
    return monkeypatch


def _register() -> tuple[str, str]:
    response = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    body = response.get_json()
    return body["user_id"], body["api_key"]


def _setup(api_key: str, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_client,
        "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    response = make_client().post(
        "/v1/model_api/setup",
        json={
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "api_key": "sk-or-test",
        },
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200, response.get_data(as_text=True)


def _flask_post(payload, api_key: str):
    response = make_client().post(
        "/v1/model_api/chat/send",
        json=payload,
        headers={"X-API-Key": api_key},
    )
    return response.status_code, response.get_json()


def _asgi_post(payload, api_key: str):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/model_api/chat/send",
                json=payload,
                headers={"X-API-Key": api_key},
            )
            return response.status_code, response.json()

    return asyncio.run(go())


def _normalized(body: dict) -> dict:
    body = dict(body)
    message = body.get("user_message")
    if isinstance(message, dict):
        body["user_message"] = {"id": "<id>", "ts": "<ts>"}
    return body


def test_processing_202_parity_and_no_inline_reply(env):
    monkeypatch = env
    user_id, api_key = _register()
    _setup(api_key, monkeypatch)

    flask_status, flask_body = _flask_post({"message": "hello"}, api_key)
    asgi_status, asgi_body = _asgi_post({"message": "hello"}, api_key)

    assert flask_status == asgi_status == 202
    assert _normalized(flask_body) == _normalized(asgi_body)
    assert flask_body["status"] == "processing"
    assert flask_body["reply_ready"] is False
    assert "assistant_message" not in flask_body
    store = core_store.get_store(user_id)
    assert len([row for row in store.chat_messages if row.get("role") == "user"]) == 2


def test_dead_v2_pool_fails_before_append_on_both_adapters(env):
    monkeypatch = env
    user_id, api_key = _register()
    _setup(api_key, monkeypatch)
    monkeypatch.setattr(jobs_store, "workers_alive", lambda **kw: False)

    flask_status, flask_body = _flask_post({"message": "hello"}, api_key)
    asgi_status, asgi_body = _asgi_post({"message": "hello"}, api_key)

    assert flask_status == asgi_status == 503
    assert flask_body == asgi_body == {
        "error": "workers_unavailable",
        "reason": "no_live_v2_worker_heartbeat",
    }
    store = core_store.get_store(user_id)
    assert not [row for row in store.chat_messages if row.get("role") == "user"]


@pytest.mark.parametrize(
    ("payload", "content_type"),
    [
        (
            {
                "content_type": "file",
                "file_name": "notes.md",
                "file_mime": "text/markdown",
                "file_b64": _b64(b"# Title\nbody\n"),
                "message": "read this",
            },
            "file",
        ),
        (
            {
                "content_type": "file",
                "file_name": "pic.png",
                "file_mime": "image/png",
                "file_b64": _b64(b"\x89PNG\r\n\x1a\n"),
            },
            "image",
        ),
    ],
)
def test_asgi_file_turns_enter_v2(env, payload, content_type):
    monkeypatch = env
    user_id, api_key = _register()
    _setup(api_key, monkeypatch)

    status, body = _asgi_post(payload, api_key)

    assert status == 202, body
    store = core_store.get_store(user_id)
    user_rows = [row for row in store.chat_messages if row.get("role") == "user"]
    assert user_rows[-1]["content_type"] == content_type


def test_auth_and_validation_parity(env):
    monkeypatch = env
    _user_id, api_key = _register()
    _setup(api_key, monkeypatch)

    for payload, expected in (({"message": ""}, 400), ({"message": "x" * 12001}, 413)):
        flask_status, flask_body = _flask_post(payload, api_key)
        asgi_status, asgi_body = _asgi_post(payload, api_key)
        assert flask_status == asgi_status == expected
        assert flask_body == asgi_body

    status, body = _asgi_post({"message": "x"}, "not-a-key")
    assert status == 401
    assert body == {"error": "unauthorized"}
