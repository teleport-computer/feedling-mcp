from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import image_generator, setup_core  # noqa: E402


def _store(user_id: str = "image-user"):
    return SimpleNamespace(user_id=user_id)


def _route(**overrides):
    route = {
        "id": "route-image",
        "credential_id": "credential-image",
        "provider": "openai",
        "model": "gpt-image-2",
        "base_url": "https://api.openai.com/v1",
        "api_key_envelope": {
            "body_ct": "sealed",
            "nonce": "nonce",
            "K_user": "user-key",
            "K_enclave": "enclave-key",
            "id": "credential-image",
            "owner_user_id": "image-user",
            "visibility": "shared",
            "v": 1,
        },
        "image_generation_test_status": "ok",
        "last_image_generation_test_error": "",
    }
    route.update(overrides)
    return route


def test_config_payload_follows_main_without_replacing_chat_model(monkeypatch):
    active = _route(
        id="route-main",
        provider="deepseek",
        model="deepseek-v4-flash",
        image_generation_test_status="unsupported",
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: active,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_image_generation_route",
        lambda _uid: None,
    )
    monkeypatch.setattr(
        setup_core.vision_routing,
        "runtime_capability",
        lambda _store: {"runtime": "v2", "onboarding_route": "model_api"},
    )

    config = setup_core._image_generation_config_payload(_store())

    assert config["mode"] == "follow_main"
    assert config["main_model"]["model"] == "deepseek-v4-flash"
    assert config["effective_status"] == "unsupported"
    assert config["dedicated_route"] is None


def test_resident_config_exposes_text_only_main_and_dedicated_capability(monkeypatch):
    monkeypatch.setattr(
        setup_core.vision_routing,
        "runtime_capability",
        lambda _store: {"runtime": "vps", "onboarding_route": "resident"},
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: None,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_image_generation_route",
        lambda _uid: None,
    )
    monkeypatch.setattr(
        setup_core.vision_routing.chat_consumer,
        "consumer_agent_runtime",
        lambda _store: {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
        },
    )
    monkeypatch.setattr(
        setup_core.vision_routing.chat_consumer,
        "consumer_supports_capability",
        lambda _store, capability: (
            capability == setup_core.vision_routing.chat_consumer.IMAGE_GENERATION_CAPABILITY
        ),
    )

    config = setup_core._image_generation_config_payload(_store())

    assert config["available"] is True
    assert config["runtime"] == "vps"
    assert config["main_model"] == {
        "source": "resident",
        "route_id": None,
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash",
        "image_generation_test_status": "unsupported",
        "last_image_generation_test_error": "image_generation_model_required",
    }
    assert config["effective_status"] == "unsupported"


def test_resident_config_uses_declared_agent_image_generation_capability(monkeypatch):
    monkeypatch.setattr(
        setup_core.vision_routing,
        "runtime_capability",
        lambda _store: {"runtime": "vps", "onboarding_route": "resident"},
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: None,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_image_generation_route",
        lambda _uid: None,
    )
    monkeypatch.setattr(
        setup_core.vision_routing.chat_consumer,
        "consumer_agent_runtime",
        lambda _store: {"provider": "openai", "model": "gpt-5.6-sol"},
    )
    monkeypatch.setattr(
        setup_core.vision_routing.chat_consumer,
        "consumer_supports_capability",
        lambda _store, capability: capability in {
            setup_core.vision_routing.chat_consumer.IMAGE_GENERATION_CAPABILITY,
            setup_core.vision_routing.chat_consumer.AGENT_IMAGE_GENERATION_CAPABILITY,
        },
    )

    config = setup_core._image_generation_config_payload(_store())

    assert config["main_model"]["image_generation_test_status"] == "ok"
    assert config["main_model"]["last_image_generation_test_error"] == ""
    assert config["effective_status"] == "ok"


def test_resident_main_test_uses_declared_agent_capability(monkeypatch):
    monkeypatch.setattr(
        setup_core.vision_routing,
        "runtime_capability",
        lambda _store: {"runtime": "vps"},
    )
    monkeypatch.setattr(
        setup_core.vision_routing.chat_consumer,
        "consumer_supports_capability",
        lambda _store, capability: (
            capability
            == setup_core.vision_routing.chat_consumer.AGENT_IMAGE_GENERATION_CAPABILITY
        ),
    )
    monkeypatch.setattr(
        setup_core,
        "_image_generation_config_payload",
        lambda _store: {"effective_status": "ok"},
    )

    body, status = setup_core.image_generation_main_test.__wrapped__(
        _store(),
        caller_api_key="caller-key",
    )

    assert status == 200
    assert body == {"config": {"effective_status": "ok"}}


def test_resident_generate_requires_a_pinned_image_route(monkeypatch):
    monkeypatch.setattr(
        image_generator.db,
        "model_api_image_generation_route",
        lambda _uid: None,
    )

    body, status = image_generator.generate_with_pinned_route(
        _store(),
        {"prompt": "draw a red robot"},
        caller_api_key="caller-key",
    )

    assert status == 409
    assert body == {
        "error": "image_generation_model_required",
        "error_class": "image_generation_model_required",
    }


def test_resident_generate_uses_only_the_pinned_image_route(monkeypatch):
    marked = []
    decrypted = []
    monkeypatch.setattr(
        image_generator.db,
        "model_api_image_generation_route",
        lambda _uid: _route(),
    )

    def fake_decrypt(envelope, caller_key, *, purpose, runtime_token=""):
        decrypted.append((envelope, caller_key, purpose, runtime_token))
        return b"provider-key"

    monkeypatch.setattr(
        image_generator.core_enclave,
        "_decrypt_envelope_via_enclave",
        fake_decrypt,
    )

    async def generated(config, prompt):
        assert config.provider == "openai"
        assert config.model == "gpt-image-2"
        assert config.api_key == "provider-key"
        assert prompt == "draw a red robot"
        return {
            "media": [
                {
                    "mime_type": "image/png",
                    "data_base64": "aW1hZ2U=",
                    "name": "robot.png",
                }
            ]
        }

    monkeypatch.setattr(
        image_generator.provider_client,
        "generate_image_async",
        generated,
    )
    monkeypatch.setattr(
        image_generator.generated_image,
        "normalize_generated_image",
        lambda *_args, **_kwargs: SimpleNamespace(
            data=b"normalized-image",
            mime_type="image/png",
            name="robot.png",
        ),
    )
    monkeypatch.setattr(
        image_generator.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    body, status = image_generator.generate_with_pinned_route(
        _store(),
        {"prompt": "draw a red robot"},
        caller_api_key="caller-key",
        caller_runtime_token="runtime-token",
    )

    assert status == 200
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-image-2"
    assert body["images"] == [
        {
            "mime_type": "image/png",
            "data_base64": "bm9ybWFsaXplZC1pbWFnZQ==",
            "name": "robot.png",
        }
    ]
    assert decrypted == [
        (
            _route()["api_key_envelope"],
            "caller-key",
            "model_api_provider_key",
            "runtime-token",
        )
    ]
    assert marked == [{"status": "ok"}]


def test_select_dedicated_tests_before_changing_route(monkeypatch):
    route = _route(image_generation_test_status="untested")
    events = []
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_get",
        lambda _uid, _rid: route,
    )
    monkeypatch.setattr(
        setup_core,
        "_test_route_image_generation_or_error",
        lambda *_args, **_kwargs: events.append("tested") or None,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_set_image_generation",
        lambda _uid, _rid: events.append("selected") or True,
    )
    monkeypatch.setattr(
        setup_core,
        "_image_generation_config_payload",
        lambda _store: {"mode": "dedicated", "effective_status": "ok"},
    )

    body, status = setup_core.image_generation_config_set.__wrapped__(
        _store(),
        {"mode": "dedicated", "route_id": route["id"]},
        caller_api_key="caller-key",
    )

    assert status == 200
    assert body["config"]["effective_status"] == "ok"
    assert events == ["tested", "selected"]


def test_failed_follow_main_probe_preserves_existing_dedicated_route(monkeypatch):
    active = _route(id="route-main", provider="deepseek", model="deepseek-v4-flash")
    cleared = []
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: active,
    )
    monkeypatch.setattr(
        setup_core,
        "_test_route_image_generation_or_error",
        lambda *_args, **_kwargs: (
            {"error": "image_generation_model_incompatible", "retryable": False},
            400,
        ),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_clear_image_generation",
        lambda _uid: cleared.append(True) or True,
    )

    body, status = setup_core.image_generation_config_set.__wrapped__(
        _store(),
        {"mode": "follow_main"},
        caller_api_key="caller-key",
    )

    assert status == 400
    assert body["error"] == "image_generation_model_incompatible"
    assert cleared == []


def test_route_probe_marks_real_generated_media_ready(monkeypatch):
    marked = []
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )

    async def generated(*_args, **_kwargs):
        return {
            "media": [
                {
                    "mime_type": "image/png",
                    "data_base64": "aW1hZ2U=",
                }
            ]
        }

    monkeypatch.setattr(
        setup_core.provider_client,
        "generate_image_async",
        generated,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    error = setup_core._test_route_image_generation_or_error(
        _store(),
        _route(),
        "caller-key",
    )

    assert error is None
    assert marked == [{"status": "ok"}]


def test_route_probe_maps_text_only_model_to_configuration_error(monkeypatch):
    marked = []
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )

    async def unsupported(*_args, **_kwargs):
        exc = setup_core.provider_client.ProviderError(
            "image_generation_model_unsupported"
        )
        exc.feedling_error_class = "provider_incompatible"
        raise exc

    monkeypatch.setattr(
        setup_core.provider_client,
        "generate_image_async",
        unsupported,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_image_generation_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    body, status = setup_core._test_route_image_generation_or_error(
        _store(),
        _route(provider="deepseek", model="deepseek-v4-flash"),
        "caller-key",
    )

    assert status == 400
    assert body == {
        "error": "image_generation_model_incompatible",
        "retryable": False,
    }
    assert marked == [
        {
            "status": "unsupported",
            "error": "image_generation_model_incompatible",
        }
    ]


def _configure_rollback_env(monkeypatch, *, select_status, routes_before=()):
    """Wire image_generation_route_configure so create succeeds and select
    returns select_status; record every rollback delete in order."""
    deleted = []
    monkeypatch.setattr(
        setup_core.db,
        "model_api_routes_list",
        lambda _uid: [{"id": route_id} for route_id in routes_before],
    )
    monkeypatch.setattr(
        setup_core.model_api_route_create,
        "__wrapped__",
        lambda _store, _payload, **_kwargs: ({
            "route": {"id": "new-route", "credential_id": "new-credential"}
        }, 200),
    )
    monkeypatch.setattr(
        setup_core.image_generation_config_set,
        "__wrapped__",
        lambda _store, _payload, **_kwargs: (
            ({"config": {}}, 200)
            if select_status == 200
            else ({"error": "image_generation_invalid_output"}, select_status)
        ),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_delete",
        lambda uid, route_id: deleted.append(("route", uid, route_id)) or True,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_credential_delete",
        lambda uid, credential_id: deleted.append(
            ("credential", uid, credential_id)
        ) or True,
    )
    return deleted


def test_failed_configure_with_new_key_rolls_back_route_and_credential(monkeypatch):
    deleted = _configure_rollback_env(monkeypatch, select_status=502)

    body, status = setup_core.image_generation_route_configure.__wrapped__(
        _store(),
        {"provider": "openai", "model": "gpt-image-2", "api_key": "secret"},
        caller_api_key="caller-key",
    )

    assert (body, status) == ({"error": "image_generation_invalid_output"}, 502)
    # 路线必须显式删:tee schema 没有 credential→route 级联,只删凭据会留下
    # JOIN 不出来、用户也删不掉的僵尸路线(2026-08 生图排查实锤)。
    assert deleted == [
        ("route", "image-user", "new-route"),
        ("credential", "image-user", "new-credential"),
    ]


def test_failed_configure_with_reused_credential_keeps_credential(monkeypatch):
    deleted = _configure_rollback_env(monkeypatch, select_status=502)

    body, status = setup_core.image_generation_route_configure.__wrapped__(
        _store(),
        {"credential_id": "new-credential", "model": "gpt-image-2"},
        caller_api_key="caller-key",
    )

    assert status == 502
    assert deleted == [("route", "image-user", "new-route")]


def test_failed_configure_leaves_preexisting_route_untouched(monkeypatch):
    deleted = _configure_rollback_env(
        monkeypatch, select_status=502, routes_before=("new-route",)
    )

    body, status = setup_core.image_generation_route_configure.__wrapped__(
        _store(),
        {"provider": "openai", "model": "gpt-image-2", "api_key": "secret"},
        caller_api_key="caller-key",
    )

    assert status == 502
    assert deleted == []


def test_successful_configure_deletes_nothing(monkeypatch):
    deleted = _configure_rollback_env(monkeypatch, select_status=200)

    body, status = setup_core.image_generation_route_configure.__wrapped__(
        _store(),
        {"provider": "openai", "model": "gpt-image-2", "api_key": "secret"},
        caller_api_key="caller-key",
    )

    assert status == 200
    assert deleted == []
