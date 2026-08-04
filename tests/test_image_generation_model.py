from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import setup_core  # noqa: E402


def _store(user_id: str = "image-user"):
    return SimpleNamespace(user_id=user_id)


def _route(**overrides):
    route = {
        "id": "route-image",
        "credential_id": "credential-image",
        "provider": "openai",
        "model": "gpt-image-2",
        "base_url": "https://api.openai.com/v1",
        "api_key_envelope": {"ciphertext": "sealed"},
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

    config = setup_core._image_generation_config_payload(_store())

    assert config["mode"] == "follow_main"
    assert config["main_model"]["model"] == "deepseek-v4-flash"
    assert config["effective_status"] == "unsupported"
    assert config["dedicated_route"] is None


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
