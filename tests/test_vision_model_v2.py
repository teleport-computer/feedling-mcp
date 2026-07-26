import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import setup_core, vision_routing


def _store(user_id="u1"):
    return SimpleNamespace(user_id=user_id)


def test_config_reports_vps_as_unavailable_until_resident_advertises_observer(monkeypatch):
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
    monkeypatch.setattr(setup_core.db, "model_api_active_route", lambda _uid: None)
    monkeypatch.setattr(setup_core.db, "model_api_vision_route", lambda _uid: None)

    config = setup_core._vision_config_payload(_store())

    assert config["available"] is False
    assert config["runtime"] == "vps"
    assert config["effective_status"] == "resident_update_required"
    assert config["main_model"]["source"] == "resident"


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
            "vision_test_status": "ok",
            "last_vision_test_error": "",
        },
    )
    monkeypatch.setattr(setup_core.db, "model_api_vision_route", lambda _uid: None)

    config = setup_core._vision_config_payload(_store())

    assert config["available"] is True
    assert config["runtime"] == "hosted_v1"
    assert config["effective_status"] == "ok"
    assert config["mode"] == "follow_main"


def test_config_reports_model_api_v1_resident_update_required(monkeypatch):
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
    assert config["effective_status"] == "resident_update_required"


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


def test_dedicated_route_for_send_fails_closed_before_resident_update(monkeypatch):
    route = {"id": "vision", "vision_test_status": "ok"}
    monkeypatch.setattr(vision_routing.db, "model_api_vision_route", lambda _uid: route)
    monkeypatch.setattr(
        vision_routing,
        "runtime_capability",
        lambda _store: {"available": False, "runtime": "vps"},
    )

    selected, error = vision_routing.dedicated_route_for_send(_store())

    assert selected is None
    assert error == ({"error": "vision_resident_update_required", "runtime": "vps"}, 409)
