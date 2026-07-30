import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import setup_core, vision_routing
from chat import consumer as chat_consumer


def _store(user_id="u1"):
    return SimpleNamespace(user_id=user_id)


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


def test_generated_probe_is_a_png_with_all_four_color_labels():
    encoded, expected = setup_core._vision_probe_image()

    assert encoded.startswith("iVBOR")
    assert set(expected.split(",")) == {"red", "green", "blue", "yellow"}
    assert len(expected.split(",")) == 4


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


def test_explicit_catalog_modalities_skip_paid_pixel_probe(monkeypatch):
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
        setup_core.provider_client,
        "chat_completion",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("explicit catalog metadata must avoid a paid probe")
        ),
    )
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_vision_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    assert setup_core._run_route_vision_test_or_error(
        _store(), route, "caller"
    ) is None
    assert marked == [{"status": "ok", "error": ""}]


def test_missing_catalog_modalities_falls_through_to_two_image_probe(monkeypatch):
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
        "_vision_probe_images",
        lambda: (
            [{"data_url": "data:image/png;base64,a"}, {"data_url": "data:image/png;base64,b"}],
            ["red,green,blue,yellow", "yellow,blue,green,red"],
        ),
    )
    captured = {}

    def complete(_config, messages, **_kwargs):
        captured["content"] = messages[0]["content"]
        return {"reply": "red,green,blue,yellow\nyellow,blue,green,red"}

    monkeypatch.setattr(setup_core.provider_client, "chat_completion", complete)
    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_mark_vision_test",
        lambda _uid, _rid, **kwargs: marked.append(kwargs) or True,
    )

    assert setup_core._run_route_vision_test_or_error(
        _store(), route, "caller"
    ) is None
    assert len([block for block in captured["content"] if block["type"] == "image_url"]) == 2
    assert marked == [{"status": "ok"}]


def test_unavailable_catalog_endpoint_still_runs_pixel_probe(monkeypatch):
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
        "_vision_probe_images",
        lambda: (
            [{"data_url": "data:image/png;base64,a"}, {"data_url": "data:image/png;base64,b"}],
            ["red,green,blue,yellow", "yellow,blue,green,red"],
        ),
    )
    probes = []
    monkeypatch.setattr(
        setup_core.provider_client,
        "chat_completion",
        lambda *_args, **_kwargs: probes.append(True) or {
            "reply": "red,green,blue,yellow\nyellow,blue,green,red"
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
    assert probes == [True]


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


def test_follow_main_cached_unsupported_blocks_with_exact_model(monkeypatch):
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
    assert error == ({
        "error": "vision_model_incompatible",
        "retryable": False,
        "provider": "anthropic",
        "model": "claude-text-only",
    }, 400)


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


def test_resident_cached_unsupported_blocks_with_exact_model(monkeypatch):
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
    assert error == ({
        "error": "vision_model_incompatible",
        "retryable": False,
        "provider": "openrouter",
        "model": "text/model",
    }, 400)
