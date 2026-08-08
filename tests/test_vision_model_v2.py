import base64
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from accounts import onboarding as accounts_onboarding
from hosted import setup_core, vision_routing
from chat import consumer as chat_consumer


def _store(user_id="u1"):
    return SimpleNamespace(user_id=user_id)


def _run_pixel_probe(monkeypatch, reply):
    route = {
        "id": "r-thinking",
        "credential_id": "c1",
        "provider": "openai_compatible",
        "model": "claude-opus-4-6-thinking",
        "base_url": "https://relay.example/v1",
        "api_key_envelope": {"body_ct": "ciphertext"},
    }
    marked = []
    captured = {}
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )
    monkeypatch.setattr(
        setup_core.provider_client,
        "list_provider_models",
        lambda *_args: (_ for _ in ()).throw(
            setup_core.provider_client.ProviderError(
                "provider_http_405", status_code=405
            )
        ),
    )
    monkeypatch.setattr(
        setup_core,
        "_vision_probe_image",
        lambda: ("encoded-png", "red,green,blue,yellow"),
    )

    def complete(_config, _messages, **kwargs):
        captured.update(kwargs)
        return {"reply": reply}

    monkeypatch.setattr(setup_core.provider_client, "chat_completion", complete)
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_vision_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )
    result = setup_core._run_route_vision_test_or_error(
        _store(), route, "caller"
    )
    return result, marked, captured


@pytest.mark.parametrize("reply", ["", "I cannot inspect that input."])
def test_thinking_probe_without_color_reply_is_retryable_failure(monkeypatch, reply):
    result, marked, captured = _run_pixel_probe(monkeypatch, reply)

    body, status = result
    assert status == 400
    assert body == {
        "error": "vision_model_empty_response",
        "detail": "vision_model_empty_response",
        "retryable": True,
    }
    assert marked == [{
        "status": "failed",
        "error": "vision_model_empty_response",
    }]
    assert captured["max_tokens"] == 2000
    assert "warning" not in body
    assert "banner" not in body


def test_thinking_probe_nonempty_wrong_colors_are_unsupported(monkeypatch):
    result, marked, _captured = _run_pixel_probe(monkeypatch, "red and purple")

    body, status = result
    assert status == 400
    assert body["error"] == "vision_model_incompatible"
    assert body["retryable"] is False
    assert marked == [{
        "status": "unsupported",
        "error": "vision_model_incompatible",
    }]


def test_setup_vision_probe_kick_is_daemonized_and_nonblocking(
    monkeypatch,
    enable_setup_auto_vision_probe,
):
    created = {}

    class FakeThread:
        def __init__(self, *, target, args, daemon, name):
            created.update(
                target=target,
                args=args,
                daemon=daemon,
                name=name,
                started=False,
            )

        def start(self):
            created["started"] = True

    monkeypatch.setattr(setup_core.threading, "Thread", FakeThread)

    setup_core._kick_setup_main_vision_test(
        _store("user-auto-probe"),
        "route-1",
        "2026-07-30T12:00:00.123456Z",
        "caller-key",
    )

    assert created["target"] is setup_core._run_setup_main_vision_test
    assert created["args"][1:] == (
        "route-1",
        "2026-07-30T12:00:00.123456Z",
        "caller-key",
    )
    assert created["daemon"] is True
    assert created["started"] is True


def test_setup_vision_probe_reuses_main_test_with_route_version_fence(
    monkeypatch,
    enable_setup_auto_vision_probe,
):
    route = {"id": "route-1", "is_active": True}
    captured = {}
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_get",
        lambda user_id, route_id: route,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route_version",
        lambda user_id: {
            "route_id": "route-1",
            "updated_at_token": "2026-07-30T12:00:00.123456Z",
        },
    )
    monkeypatch.setattr(
        setup_core,
        "_test_route_vision_or_error",
        lambda store, current, caller_api_key, **kwargs: captured.update(
            {
                "store": store,
                "route": current,
                "caller_api_key": caller_api_key,
                **kwargs,
            }
        ),
    )

    store = _store("user-auto-probe")
    setup_core._run_setup_main_vision_test(
        store,
        "route-1",
        "2026-07-30T12:00:00.123456Z",
        "caller-key",
    )

    assert captured == {
        "store": store,
        "route": route,
        "caller_api_key": "caller-key",
        "reuse_cached": False,
        "expected_updated_at": "2026-07-30T12:00:00.123456Z",
    }


def test_config_reports_text_only_vps_main_as_unsupported(monkeypatch):
    monkeypatch.setattr(
        setup_core.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "resident",
    )
    monkeypatch.setattr(
        vision_routing.chat_consumer,
        "consumer_supports_capability",
        lambda _store, _capability: False,
    )
    monkeypatch.setattr(
        vision_routing.chat_consumer,
        "resident_vision_validation",
        lambda _store: {
            "status": "unsupported",
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
            "input_modalities": ["text"],
        },
    )
    monkeypatch.setattr(setup_core.db, "model_api_active_route", lambda _uid: None)
    monkeypatch.setattr(setup_core.db, "model_api_vision_route", lambda _uid: None)

    config = setup_core._vision_config_payload(_store())

    assert config["available"] is False
    assert config["runtime"] == "vps"
    assert config["effective_status"] == "unsupported"
    assert config["main_model"]["source"] == "resident"
    assert config["main_model"]["model"] == "deepseek/deepseek-v4-flash"


def test_config_allows_image_capable_vps_main_without_dedicated_observer(monkeypatch):
    monkeypatch.setattr(
        setup_core.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "resident",
    )
    monkeypatch.setattr(
        vision_routing.chat_consumer,
        "consumer_supports_capability",
        lambda _store, _capability: False,
    )
    monkeypatch.setattr(
        vision_routing.chat_consumer,
        "resident_vision_validation",
        lambda _store: {
            "status": "ok",
            "provider": "openrouter",
            "model": "openai/gpt-5-mini",
            "input_modalities": ["text", "image"],
        },
    )
    monkeypatch.setattr(setup_core.db, "model_api_active_route", lambda _uid: None)
    monkeypatch.setattr(setup_core.db, "model_api_vision_route", lambda _uid: None)

    config = setup_core._vision_config_payload(_store())

    assert config["available"] is False
    assert config["mode"] == "follow_main"
    assert config["effective_status"] == "ok"
    assert config["main_model"]["vision_test_status"] == "ok"


def test_config_reports_model_api_v1_as_available_with_resident_observer(monkeypatch):
    monkeypatch.setattr(
        setup_core.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "model_api",
    )
    monkeypatch.setattr(
        setup_core.hosted_config_store,
        "hosted_runtime_v2_enabled_strict",
        lambda _store: False,
    )
    monkeypatch.setattr(
        vision_routing.chat_consumer,
        "consumer_supports_capability",
        lambda _store, _capability: True,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: {
            "id": "main",
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "vision_test_status": "untested",
            "last_vision_test_error": "",
        },
    )
    monkeypatch.setattr(setup_core.db, "model_api_vision_route", lambda _uid: None)

    config = setup_core._vision_config_payload(_store())

    assert config["available"] is True
    assert config["runtime"] == "hosted_v1"
    assert config["effective_status"] == "untested"
    assert config["mode"] == "follow_main"
    assert config["main_model"]["vision_test_status"] == "untested"


def test_config_reports_model_api_follow_main_unsupported_signal(monkeypatch):
    monkeypatch.setattr(
        setup_core.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "model_api",
    )
    monkeypatch.setattr(
        setup_core.hosted_config_store,
        "hosted_runtime_v2_enabled_strict",
        lambda _store: True,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: {
            "id": "main",
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "vision_test_status": "unsupported",
            "last_vision_test_error": "vision_model_incompatible",
        },
    )
    monkeypatch.setattr(setup_core.db, "model_api_vision_route", lambda _uid: None)

    config = setup_core._vision_config_payload(_store())

    assert config["available"] is True
    assert config["runtime"] == "v2"
    assert config["mode"] == "follow_main"
    assert config["effective_status"] == "unsupported"
    assert config["main_model"]["vision_test_status"] == "unsupported"
    assert config["main_model"]["provider"] == "openrouter"
    assert config["main_model"]["model"] == "deepseek/deepseek-v4-pro"


def test_config_uses_v2_main_when_legacy_onboarding_route_is_missing(monkeypatch):
    monkeypatch.setattr(
        setup_core.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "resident",
    )
    monkeypatch.setattr(
        setup_core.hosted_config_store,
        "hosted_runtime_v2_enabled_strict",
        lambda _store: True,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: {
            "id": "main",
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "vision_test_status": "unsupported",
            "last_vision_test_error": "vision_model_incompatible",
        },
    )
    monkeypatch.setattr(setup_core.db, "model_api_vision_route", lambda _uid: None)

    config = setup_core._vision_config_payload(_store())

    assert config["runtime"] == "v2"
    assert config["main_model"]["source"] == "model_api"
    assert config["main_model"]["provider"] == "deepseek"
    assert config["main_model"]["model"] == "deepseek-v4-pro"


def test_config_reports_model_api_v1_without_main_as_not_configured(monkeypatch):
    monkeypatch.setattr(
        setup_core.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "model_api",
    )
    monkeypatch.setattr(
        setup_core.hosted_config_store,
        "hosted_runtime_v2_enabled_strict",
        lambda _store: False,
    )
    monkeypatch.setattr(
        vision_routing.chat_consumer,
        "consumer_supports_capability",
        lambda _store, _capability: False,
    )
    monkeypatch.setattr(setup_core.db, "model_api_active_route", lambda _uid: None)
    monkeypatch.setattr(setup_core.db, "model_api_vision_route", lambda _uid: None)

    config = setup_core._vision_config_payload(_store())

    assert config["available"] is False
    assert config["runtime"] == "hosted_v1"
    assert config["effective_status"] == "untested"


def test_config_requires_resident_update_only_for_saved_dedicated_route(monkeypatch):
    monkeypatch.setattr(
        setup_core.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "model_api",
    )
    monkeypatch.setattr(
        setup_core.hosted_config_store,
        "hosted_runtime_v2_enabled_strict",
        lambda _store: False,
    )
    monkeypatch.setattr(
        vision_routing.chat_consumer,
        "consumer_supports_capability",
        lambda _store, _capability: False,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: {
            "id": "main",
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "vision_test_status": "ok",
        },
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_vision_route",
        lambda _uid: {
            "id": "vision",
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "vision_test_status": "ok",
        },
    )

    config = setup_core._vision_config_payload(_store())

    assert config["mode"] == "dedicated"
    assert config["effective_status"] == "untested"


def test_config_exposes_dedicated_route_only_for_model_api_v2(monkeypatch):
    route = {
        "id": "vision",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "vision_test_status": "ok",
        "last_vision_test_error": "",
        "api_key_envelope": {"body_ct": "secret-ciphertext"},
    }
    monkeypatch.setattr(
        setup_core.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "model_api",
    )
    monkeypatch.setattr(
        setup_core.hosted_config_store,
        "hosted_runtime_v2_enabled_strict",
        lambda _store: True,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_active_route",
        lambda _uid: {**route, "id": "main", "model": "gpt-5.4"},
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_vision_route",
        lambda _uid: dict(route),
    )

    config = setup_core._vision_config_payload(_store())

    assert config["available"] is True
    assert config["runtime"] == "v2"
    assert config["mode"] == "dedicated"
    assert config["effective_status"] == "ok"
    assert config["dedicated_route"]["id"] == "vision"
    assert "api_key_envelope" not in config["dedicated_route"]


def test_generated_probe_is_a_small_png_with_random_color_subset():
    encoded, expected = setup_core._vision_probe_image()

    assert encoded.startswith("iVBOR")
    raw = base64.b64decode(encoded)
    assert struct.unpack(">II", raw[16:24]) == (96, 24)
    expected_colors = expected.split(",")
    assert len(expected_colors) == len(set(expected_colors)) == 4
    assert set(expected_colors) < set(setup_core.vision_probe_capability.color_names())


@pytest.mark.parametrize(
    ("reply", "expected", "matches"),
    [
        ("red and yellow", "red,green,blue,yellow", True),
        ("red", "red,green,blue,yellow", False),
        ("red, yellow, purple", "red,green,blue,yellow", False),
        ("orange,purple,pink,black", "orange,purple,pink,black", True),
    ],
)
def test_probe_reply_requires_two_correct_colors_and_no_absent_color(
    reply, expected, matches
):
    assert setup_core._vision_probe_reply_matches(reply, expected) is matches


def test_failed_new_vision_route_is_cleaned_up_inside_configure(monkeypatch):
    deleted = []
    monkeypatch.setattr(setup_core, "_vision_routing_available", lambda _store: True)
    monkeypatch.setattr(setup_core.db, "model_api_routes_list", lambda _uid: [])
    monkeypatch.setattr(
        setup_core.model_api_route_create,
        "__wrapped__",
        lambda _store, _payload, **_kwargs: ({
            "route": {"id": "new-route", "credential_id": "new-credential"}
        }, 200),
    )
    monkeypatch.setattr(
        setup_core.vision_config_set,
        "__wrapped__",
        lambda _store, _payload, **_kwargs: (
            {"error": "vision_model_test_failed"},
            400,
        ),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_credential_delete",
        lambda uid, credential_id: deleted.append((uid, credential_id)) or True,
    )

    body, status = setup_core.vision_route_configure.__wrapped__(
        _store(),
        {"provider": "openai", "model": "gpt-4.1-mini", "api_key": "secret"},
        caller_api_key="caller",
    )

    assert (body, status) == ({"error": "vision_model_test_failed"}, 400)
    assert deleted == [("u1", "new-credential")]


def test_follow_main_can_clear_dedicated_route_before_resident_update(monkeypatch):
    monkeypatch.setattr(setup_core, "_vision_routing_available", lambda _store: False)
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_clear_vision",
        lambda _uid: True,
    )
    monkeypatch.setattr(
        setup_core,
        "_vision_config_payload",
        lambda _store: {"mode": "follow_main", "available": False},
    )

    body, status = setup_core.vision_config_set.__wrapped__(
        _store(),
        {"mode": "follow_main"},
        caller_api_key="caller",
    )

    assert status == 200
    assert body == {"config": {"mode": "follow_main", "available": False}}


def test_dedicated_selection_rejects_failed_pixel_probe(monkeypatch):
    selected = []
    route = {"id": "vision-route"}
    monkeypatch.setattr(setup_core, "_vision_routing_available", lambda _store: True)
    monkeypatch.setattr(setup_core.db, "model_api_route_get", lambda _uid, _rid: route)
    monkeypatch.setattr(
        setup_core,
        "_test_route_vision_or_error",
        lambda *_args, **_kwargs: (
            {
                "error": "vision_model_incompatible",
                "detail": "provider rejected image input",
                "retryable": False,
            },
            400,
        ),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_set_vision",
        lambda _uid, route_id: selected.append(route_id) or True,
    )

    body, status = setup_core.vision_config_set.__wrapped__(
        _store(),
        {"mode": "dedicated", "route_id": "vision-route"},
        caller_api_key="caller",
    )

    assert status == 400
    assert body["error"] == "vision_model_incompatible"
    assert selected == []


def test_catalog_image_declaration_cannot_override_hard_pixel_rejection(monkeypatch):
    marked = []
    route = {
        "id": "r1",
        "credential_id": "c1",
        "provider": "openrouter",
        "model": "vendor/vision",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_envelope": {"body_ct": "ciphertext"},
    }
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )
    monkeypatch.setattr(
        setup_core.provider_client,
        "list_provider_models",
        lambda *_args: {
            "models": [{"id": "vendor/vision", "input_modalities": ["text", "image"]}]
        },
    )
    monkeypatch.setattr(
        setup_core,
        "_vision_probe_image",
        lambda: ("encoded-png", "red,green,blue,yellow"),
    )
    captured = {}

    def complete(_config, messages, **kwargs):
        captured["content"] = messages[0]["content"]
        captured["kwargs"] = kwargs
        raise setup_core.provider_client.ProviderError(
            "provider_http_415", status_code=415
        )

    events = []
    monkeypatch.setattr(setup_core.provider_client, "chat_completion", complete)
    monkeypatch.setattr(
        setup_core.debug_trace,
        "trace_event",
        lambda *_args, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_vision_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    body, status = setup_core._run_route_vision_test_or_error(
        _store(), route, "caller"
    )
    assert status == 400
    assert body["error"] == "vision_model_incompatible"
    assert isinstance(captured["content"], list)
    assert captured["kwargs"]["max_tokens"] == 2000
    assert marked == [{
        "status": "unsupported",
        "error": "vision_model_incompatible",
    }]
    assert events[0]["detail"] == {
        "provider": "openrouter",
        "model": "vendor/vision",
        "declared": ["text", "image"],
        "probed": "unsupported",
    }
    assert "base_url" not in events[0]["detail"]


def test_catalog_text_only_declaration_cannot_override_successful_pixel_probe(monkeypatch):
    marked = []
    route = {
        "id": "r1",
        "credential_id": "c1",
        "provider": "openrouter",
        "model": "vendor/vision",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_envelope": {"body_ct": "ciphertext"},
    }
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )
    monkeypatch.setattr(
        setup_core.provider_client,
        "list_provider_models",
        lambda *_args: {
            "models": [{
                "id": "vendor/vision",
                "input_modalities": ["text"],
            }],
            "complete": True,
            "catalog_supported": True,
        },
    )
    monkeypatch.setattr(
        setup_core,
        "_vision_probe_image",
        lambda: ("encoded-png", "red,green,blue,yellow"),
    )
    calls = []
    monkeypatch.setattr(
        setup_core.provider_client,
        "chat_completion",
        lambda _config, messages, **kwargs: calls.append((messages, kwargs)) or {
            "reply": "red and yellow"
        },
    )
    events = []
    monkeypatch.setattr(
        setup_core.debug_trace,
        "trace_event",
        lambda *_args, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_vision_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    assert setup_core._run_route_vision_test_or_error(
        _store(), route, "caller"
    ) is None
    assert isinstance(calls[0][0][0]["content"], list)
    assert marked == [{"status": "ok"}]
    assert events[0]["detail"]["probed"] == "ok"
    assert events[0]["detail"]["declared"] == ["text"]


def test_model_missing_from_catalog_still_uses_authoritative_pixel_probe(monkeypatch):
    marked = []
    route = {
        "id": "r1",
        "credential_id": "c1",
        "provider": "openai",
        "model": "vendor/not-allowed",
        "base_url": "https://api.openai.com/v1",
        "api_key_envelope": {"body_ct": "ciphertext"},
    }
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )
    monkeypatch.setattr(
        setup_core.provider_client,
        "list_provider_models",
        lambda *_args: {"models": [{"id": "vendor/allowed", "input_modalities": ["image"]}]},
    )
    monkeypatch.setattr(
        setup_core,
        "_vision_probe_image",
        lambda: ("encoded-png", "red,green,blue,yellow"),
    )
    calls = []
    monkeypatch.setattr(
        setup_core.provider_client,
        "chat_completion",
        lambda _config, messages, **kwargs: calls.append((messages, kwargs)) or {
            "reply": "green,blue"
        },
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_vision_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    assert setup_core._run_route_vision_test_or_error(
        _store(), route, "caller"
    ) is None
    assert isinstance(calls[0][0][0]["content"], list)
    assert marked == [{"status": "ok"}]


def test_unknown_catalog_model_404_reports_incompatible_instead_of_missing(monkeypatch):
    marked = []
    route = {
        "id": "r1",
        "credential_id": "c1",
        "provider": "openai_compatible",
        "model": "provider-specialized-model",
        "base_url": "https://private.example/v1",
        "api_key_envelope": {"body_ct": "ciphertext"},
    }
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )
    monkeypatch.setattr(
        setup_core.provider_client,
        "list_provider_models",
        lambda *_args: {"models": [{"id": "provider-specialized-model"}]},
    )
    monkeypatch.setattr(
        setup_core,
        "_vision_probe_image",
        lambda: ("encoded-png", "red,green,blue,yellow"),
    )

    def endpoint_rejects(*_args, **_kwargs):
        raise setup_core.provider_client.ProviderError(
            "provider_http_404",
            status_code=404,
        )

    monkeypatch.setattr(
        setup_core.provider_client,
        "chat_completion",
        endpoint_rejects,
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_vision_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    body, status = setup_core._run_route_vision_test_or_error(
        _store(), route, "caller"
    )

    assert status == 400
    assert body["error"] == "vision_model_incompatible"
    assert marked == [{
        "status": "unsupported",
        "error": "vision_model_incompatible",
    }]


def test_missing_catalog_modalities_accepts_partial_image_recognition(monkeypatch):
    marked = []
    route = {
        "id": "r1",
        "credential_id": "c1",
        "provider": "openai",
        "model": "opaque-model",
        "base_url": "https://api.openai.com/v1",
        "api_key_envelope": {"body_ct": "ciphertext"},
    }
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )
    monkeypatch.setattr(
        setup_core.provider_client,
        "list_provider_models",
        lambda *_args: {"models": [{"id": "opaque-model"}]},
    )
    monkeypatch.setattr(
        setup_core,
        "_vision_probe_image",
        lambda: ("encoded-png", "red,green,blue,yellow"),
    )
    captured = {}

    def complete(_config, messages, **_kwargs):
        captured["content"] = messages[0]["content"]
        captured["kwargs"] = _kwargs
        return {"reply": "I can see red and yellow."}

    monkeypatch.setattr(setup_core.provider_client, "chat_completion", complete)
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_vision_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    assert setup_core._run_route_vision_test_or_error(
        _store(), route, "caller"
    ) is None
    assert isinstance(captured["content"], list)
    assert captured["content"][1]["type"] == "image_url"
    assert captured["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert captured["kwargs"]["max_tokens"] == 2000
    assert marked == [{"status": "ok"}]


def test_unavailable_catalog_endpoint_still_runs_real_image_probe(monkeypatch):
    route = {
        "id": "r1",
        "credential_id": "c1",
        "provider": "openai_compatible",
        "model": "private-vision-model",
        "base_url": "https://private.example/v1",
        "api_key_envelope": {"body_ct": "ciphertext"},
    }
    monkeypatch.setattr(
        setup_core.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: b"provider-key",
    )

    def catalog_unavailable(*_args):
        raise setup_core.provider_client.ProviderError(
            "provider_http_405", status_code=405
        )

    monkeypatch.setattr(
        setup_core.provider_client, "list_provider_models", catalog_unavailable
    )
    monkeypatch.setattr(
        setup_core,
        "_vision_probe_image",
        lambda: ("encoded-png", "red,green,blue,yellow"),
    )
    probes = []
    monkeypatch.setattr(
        setup_core.provider_client,
        "chat_completion",
        lambda _config, messages, **kwargs: probes.append((messages, kwargs)) or {
            "reply": "red,green,blue,yellow"
        },
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_vision_test",
        lambda *_args, **_kwargs: True,
    )

    assert setup_core._run_route_vision_test_or_error(
        _store(), route, "caller"
    ) is None
    assert len(probes) == 1
    assert isinstance(probes[0][0][0]["content"], list)
    assert probes[0][0][0]["content"][1]["type"] == "image_url"
    assert probes[0][1]["max_tokens"] == 2000


def test_resident_probe_side_channel_hides_expected_and_pending_beats_old_ok(monkeypatch):
    state = {
        "consumer_id": "resident-1",
        "agent_entry_signature": "entry-1",
        "agent_provider": "pi-provider",
        "agent_model": "pi-model",
        "resident_vision_validation": {
            "probe_id": "old",
            "consumer_id": "resident-1",
            "agent_entry_signature": "entry-1",
            "provider": "pi-provider",
            "model": "pi-model",
            "status": "ok",
        }
    }
    validation = {
        "passing": True,
        "official": True,
        "consumer_id": "resident-1",
        "consumer_capabilities": [chat_consumer.VISION_PROBE_CAPABILITY],
        "agent_entry_signature": "entry-1",
        "agent_provider": "pi-provider",
        "agent_model": "pi-model",
        "agent_input_modalities": [],
        "agent_input_modalities_source": "",
    }
    store = _store()
    monkeypatch.setattr(chat_consumer, "_consumer_validation_state", lambda *_args, **_kwargs: dict(validation))
    monkeypatch.setattr(chat_consumer, "_load_consumer_state", lambda _store: state)

    def mutate(_store, fn):
        result = fn(state)
        return state, result

    monkeypatch.setattr(chat_consumer, "_mutate_consumer_state", mutate)
    monkeypatch.setattr(
        chat_consumer,
        "consumer_agent_runtime",
        lambda *_args, **_kwargs: {
            "consumer_id": "resident-1",
            "entry_signature": "entry-1",
            "provider": "pi-provider",
            "model": "pi-model",
            "input_modalities": [],
            "input_modalities_source": "",
        },
    )

    probe, error = chat_consumer.begin_vision_probe(
        store,
        images=["image-a", "image-b"],
        expected=["a,b,c,d", "d,c,b,a"],
        now_epoch=100,
    )
    assert error == ""
    projected = chat_consumer.vision_probe_for_poll(
        store, validation, now_epoch=101
    )
    assert projected["probe_id"] == probe["probe_id"]
    assert "expected" not in projected
    assert chat_consumer.resident_vision_validation(
        store, now_epoch=101
    )["status"] == "testing"
    result, status = chat_consumer.complete_vision_probe(
        store,
        {
            "probe_id": probe["probe_id"],
            "status": "ok",
            "observed": ["a,b,c,d", "d,c,b,a"],
        },
        validation,
        now_epoch=102,
    )
    assert status == 200
    assert result["status"] == "ok"
    assert chat_consumer.resident_vision_validation(
        store, now_epoch=103
    )["status"] == "ok"


def test_resident_poll_auto_probes_new_binding_once_and_reprobes_on_change(
    monkeypatch,
):
    state = {
        "consumer_id": "resident-1",
        "consumer_name": "feedling-chat-resident",
        "official": True,
        "last_poll_epoch": 100,
        "consumer_capabilities": [chat_consumer.VISION_PROBE_CAPABILITY],
        "agent_entry_signature": "entry-1",
        "agent_provider": "openrouter",
        "agent_model": "deepseek/text-only",
        "agent_input_modalities": [],
        "agent_input_modalities_source": "",
    }
    store = _store("resident-auto")

    def validation(_store, *, now_epoch=None):
        return {
            "passing": True,
            "official": True,
            "consumer_id": state["consumer_id"],
            "consumer_capabilities": list(state["consumer_capabilities"]),
            "agent_entry_signature": state["agent_entry_signature"],
            "agent_provider": state["agent_provider"],
            "agent_model": state["agent_model"],
            "agent_input_modalities": [],
            "agent_input_modalities_source": "",
        }

    def mutate(_store, fn):
        result = fn(state)
        return state, result

    monkeypatch.setattr(chat_consumer, "_consumer_validation_state", validation)
    monkeypatch.setattr(chat_consumer, "_load_consumer_state", lambda _store: state)
    monkeypatch.setattr(chat_consumer, "_mutate_consumer_state", mutate)
    monkeypatch.setattr(
        accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "resident",
    )
    monkeypatch.setattr(
        chat_consumer.vision_probe_capability,
        "generate_images",
        lambda: (
            [
                {"data_url": "data:image/png;base64,image-a"},
                {"data_url": "data:image/png;base64,image-b"},
            ],
            ["red,green,blue,yellow", "yellow,blue,green,red"],
        ),
    )

    info = validation(store)
    assert chat_consumer._maybe_begin_resident_vision_probe(
        store, info=info, now_epoch=100
    )
    first_probe = dict(state["resident_vision_probe"])
    assert not chat_consumer._maybe_begin_resident_vision_probe(
        store, info=info, now_epoch=101
    )
    projected = chat_consumer.vision_probe_for_poll(
        store, info, now_epoch=101
    )
    assert projected["probe_id"] == first_probe["probe_id"]
    assert "expected" not in projected

    result, status = chat_consumer.complete_vision_probe(
        store,
        {
            "probe_id": first_probe["probe_id"],
            "status": "ok",
            "observed": ["wrong", "answer"],
        },
        info,
        now_epoch=102,
    )
    assert status == 200
    assert result["status"] == "unsupported"
    assert not chat_consumer._maybe_begin_resident_vision_probe(
        store, info=info, now_epoch=103
    )

    state["agent_entry_signature"] = "entry-2"
    state["agent_model"] = "deepseek/another-text-only"
    changed_info = validation(store)
    assert chat_consumer._maybe_begin_resident_vision_probe(
        store, info=changed_info, now_epoch=104
    )
    assert state["resident_vision_probe"]["probe_id"] != first_probe["probe_id"]
    assert state["resident_vision_probe"]["model"] == "deepseek/another-text-only"


def test_poll_heartbeat_invokes_resident_auto_probe_only_for_poll(monkeypatch):
    state = {}
    calls = []

    def mutate(_store, fn):
        result = fn(state)
        return state, result

    monkeypatch.setattr(chat_consumer, "_mutate_consumer_state", mutate)
    monkeypatch.setattr(
        chat_consumer,
        "_touch_resident_binding_seen",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        chat_consumer,
        "_maybe_begin_resident_vision_probe",
        lambda store, **kwargs: calls.append((store, kwargs)) or True,
    )
    store = _store("resident-auto")
    info = {
        "official": True,
        "consumer_name": "feedling-chat-resident",
        "consumer_id": "resident-1",
    }

    chat_consumer._record_consumer_event(store, "poll", info=info)
    chat_consumer._record_consumer_event(store, "response", info=info)

    assert len(calls) == 1
    assert calls[0][0] is store
    assert calls[0][1]["info"] is info


def test_unified_main_test_returns_model_identity_and_stable_status(monkeypatch):
    route = {
        "id": "main",
        "provider": "openai",
        "model": "gpt-vision",
    }
    monkeypatch.setattr(
        setup_core.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "model_api",
    )
    monkeypatch.setattr(setup_core.db, "model_api_active_route", lambda _uid: route)
    monkeypatch.setattr(
        setup_core,
        "_test_route_vision_or_error",
        lambda *_args, **_kwargs: (
            {
                "error": "vision_model_auth_invalid",
                "status_code": 401,
                "retryable": False,
            },
            400,
        ),
    )

    body, status = setup_core.vision_main_test(
        _store(), caller_api_key="caller"
    )

    assert status == 200
    assert body == {
        "status": "failed",
        "source": "model_api",
        "provider": "openai",
        "model": "gpt-vision",
        "error_code": "vision_model_auth_invalid",
        "retryable": False,
        "status_code": 401,
    }


def test_dedicated_route_for_send_pins_ready_route(monkeypatch):
    route = {"id": "vision", "vision_test_status": "ok"}
    monkeypatch.setattr(vision_routing.db, "model_api_vision_route", lambda _uid: route)
    monkeypatch.setattr(
        vision_routing,
        "runtime_capability",
        lambda _store: {"available": True, "runtime": "hosted_v1"},
    )

    selected, error = vision_routing.dedicated_route_for_send(_store())

    assert selected == route
    assert error is None


def test_dedicated_route_for_send_does_not_gate_on_resident_capability(monkeypatch):
    route = {"id": "vision", "vision_test_status": "ok"}
    monkeypatch.setattr(vision_routing.db, "model_api_vision_route", lambda _uid: route)
    monkeypatch.setattr(
        vision_routing,
        "runtime_capability",
        lambda _store: {"available": False, "runtime": "vps"},
    )

    selected, error = vision_routing.dedicated_route_for_send(_store())

    assert selected == route
    assert error is None


def test_follow_main_cached_unsupported_does_not_block_send(monkeypatch):
    route = {
        "id": "main",
        "provider": "anthropic",
        "model": "claude-text-only",
        "vision_test_status": "unsupported",
    }
    monkeypatch.setattr(vision_routing.db, "model_api_vision_route", lambda _uid: None)
    monkeypatch.setattr(
        vision_routing.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "model_api",
    )
    monkeypatch.setattr(vision_routing.db, "model_api_active_route", lambda _uid: route)

    selected, error = vision_routing.dedicated_route_for_send(_store())

    assert selected is None
    assert error is None


def test_follow_main_untested_still_allows_real_image_call(monkeypatch):
    route = {
        "id": "main",
        "provider": "openai",
        "model": "custom-model",
        "vision_test_status": "untested",
    }
    monkeypatch.setattr(vision_routing.db, "model_api_vision_route", lambda _uid: None)
    monkeypatch.setattr(
        vision_routing.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "model_api",
    )
    monkeypatch.setattr(vision_routing.db, "model_api_active_route", lambda _uid: route)

    selected, error = vision_routing.dedicated_route_for_send(_store())

    assert selected is None
    assert error is None


def test_resident_cached_unsupported_does_not_block_send(monkeypatch):
    monkeypatch.setattr(vision_routing.db, "model_api_vision_route", lambda _uid: None)
    monkeypatch.setattr(
        vision_routing.accounts_onboarding,
        "_load_onboarding_route",
        lambda _store: "vps",
    )
    monkeypatch.setattr(
        vision_routing.chat_consumer,
        "resident_vision_validation",
        lambda _store: {
            "status": "unsupported",
            "provider": "openrouter",
            "model": "text/model",
        },
    )

    selected, error = vision_routing.dedicated_route_for_send(_store())

    assert selected is None
    assert error is None


def test_selected_unsupported_dedicated_route_stays_pinned(monkeypatch):
    route = {
        "id": "vision",
        "provider": "openrouter",
        "model": "text/model",
        "vision_test_status": "unsupported",
    }
    monkeypatch.setattr(
        vision_routing.db, "model_api_vision_route", lambda _uid: route
    )

    selected, error = vision_routing.dedicated_route_for_send(_store())

    assert selected == route
    assert error is None
