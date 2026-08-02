from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
from core import runtime_token
from core import store as core_store
from hosted import vision_observer


class _Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _seed_observation(user_id: str) -> tuple[object, str, str]:
    conftest.seed_user(user_id)
    _credential_id, route_id = conftest.configure_model_api_route(
        user_id,
        provider="openrouter",
        model="qwen/qwen-vision-test",
        base_url="https://relay.example/v1",
        envelope={"v": 1, "body_ct": "provider-key-ct", "nonce": "n"},
    )
    assert db.model_api_route_mark_vision_test(
        user_id, route_id, status="ok"
    )
    assert db.model_api_route_set_vision(user_id, route_id)
    message_id = "old-image-21"
    db.chat_append(
        user_id,
        message_id,
        1.0,
        {
            "id": message_id,
            "role": "user",
            "content_type": "image",
            "vision_route_id": route_id,
            "body_ct": "image-ct",
            "nonce": "image-nonce",
        },
        5000,
    )
    # The target is genuinely older than the former 20-row capability window.
    for index in range(25):
        newer_id = f"newer-{index:02d}"
        db.chat_append(
            user_id,
            newer_id,
            float(index + 2),
            {
                "id": newer_id,
                "role": "user",
                "content_type": "text",
                "body_ct": f"newer-ct-{index}",
                "nonce": "newer-nonce",
            },
            5000,
        )
    return core_store.get_store(user_id), route_id, message_id


def test_zero_roster_runtime_token_covers_image_key_and_provider_call(
    monkeypatch,
):
    user_id = "u_vision_zero_roster"
    store, route_id, message_id = _seed_observation(user_id)
    token = runtime_token.mint(
        b"runtime-secret",
        user_id=user_id,
        runtime_instance_id="resident-zero-roster",
        scope=["envelope_decrypt"],
    )
    claims = runtime_token.verify(b"runtime-secret", token)
    runtime_token.authorize(
        claims, user_id=user_id, scope="envelope_decrypt"
    )
    seen = {"image_reads": 0, "key_decrypts": 0, "provider_calls": 0}

    def _get(url, **kwargs):
        seen["image_reads"] += 1
        assert url.endswith(f"/v1/chat/messages/{message_id}/body")
        assert kwargs["headers"] == {"X-Feedling-Runtime-Token": token}
        return _Response(
            200,
            {
                "message": {
                    "id": message_id,
                    "image_b64": "aW1hZ2UtYnl0ZXM=",
                    "image_mime": "image/png",
                }
            },
        )

    def _decrypt(envelope, api_key, *, purpose, runtime_token=""):
        seen["key_decrypts"] += 1
        assert envelope["body_ct"] == "provider-key-ct"
        assert api_key is None
        assert purpose == "model_api_provider_key"
        assert runtime_token == token
        return b"sk-zero-roster-provider"

    def _observe(config, *, image_mime, image_b64):
        seen["provider_calls"] += 1
        assert config.api_key == "sk-zero-roster-provider"
        assert image_mime == "image/png"
        assert image_b64 == "aW1hZ2UtYnl0ZXM="
        return "A settings page is visible."

    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://fake-enclave")
    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(
        vision_observer.core_enclave,
        "_decrypt_envelope_via_enclave",
        _decrypt,
    )
    monkeypatch.setattr(vision_observer, "observe_image", _observe)

    body, status = vision_observer.observe_pinned_message(
        store,
        {"message_id": message_id, "route_id": route_id},
        caller_api_key=None,
        caller_runtime_token=token,
    )

    assert status == 200
    assert body["observation"] == "A settings page is visible."
    assert body["message_id"] == message_id
    assert seen == {"image_reads": 1, "key_decrypts": 1, "provider_calls": 1}


def test_zero_roster_without_runtime_token_remains_forbidden(monkeypatch):
    store, route_id, message_id = _seed_observation("u_vision_no_credential")

    def _get(_url, **kwargs):
        assert kwargs["headers"] == {}
        return _Response(401, {"error": "missing api_key"})

    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://fake-enclave")
    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(
        vision_observer,
        "load_provider_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider key decrypt must not run after image auth failure")
        ),
    )

    body, status = vision_observer.observe_pinned_message(
        store,
        {"message_id": message_id, "route_id": route_id},
        caller_api_key=None,
        caller_runtime_token="",
    )

    assert status == 502
    assert body["error"] == "vision_image_unavailable"
    assert body["detail"] == "capability_forbidden"
