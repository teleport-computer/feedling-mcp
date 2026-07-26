import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import setup_core


def _store(user_id="u1"):
    return SimpleNamespace(user_id=user_id)


def test_config_reports_vps_as_unavailable_without_reading_dedicated_route(monkeypatch):
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
    monkeypatch.setattr(setup_core.db, "model_api_active_route", lambda _uid: None)
    monkeypatch.setattr(
        setup_core.db,
        "model_api_vision_route",
        lambda _uid: (_ for _ in ()).throw(AssertionError("VPS must not read a V2 route")),
    )

    config = setup_core._vision_config_payload(_store())

    assert config["available"] is False
    assert config["runtime"] == "legacy_or_resident"
    assert config["effective_status"] == "managed_by_vps"
    assert config["main_model"]["source"] == "resident"


def test_config_reports_model_api_v1_as_runtime_v2_required(monkeypatch):
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

    config = setup_core._vision_config_payload(_store())

    assert config["available"] is False
    assert config["effective_status"] == "runtime_v2_required"
    assert config["mode"] == "follow_main"


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
    monkeypatch.setattr(setup_core, "_vision_runtime_v2_enabled", lambda _store: True)
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
