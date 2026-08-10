from contextlib import nullcontext
from types import SimpleNamespace

from hosted import setup_core


def test_route_activation_repairs_model_api_onboarding_route(monkeypatch):
    route = {
        "id": "route-main",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "is_active": False,
    }
    selected_routes = []

    monkeypatch.setattr(
        setup_core.db,
        "hosted_runtime_config_mutation_lock",
        lambda _user_id: nullcontext(),
    )

    monkeypatch.setattr(
        setup_core.db,
        "model_api_route_get",
        lambda _user_id, _route_id: dict(route),
    )
    monkeypatch.setattr(
        setup_core, "_test_route_or_error", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(setup_core, "_runtime_should_restore_v2", lambda _store: True)
    monkeypatch.setattr(
        setup_core, "_fence_v2_config_change_or_error", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        setup_core.db, "model_api_route_activate", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        setup_core.provider_health, "record_success", lambda _user_id: None
    )
    monkeypatch.setattr(
        setup_core, "_restore_v2_or_error", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        setup_core, "_apply_runtime_policy_or_error", lambda _store: None
    )
    monkeypatch.setattr(
        setup_core.accounts_onboarding,
        "_save_onboarding_route",
        lambda _store, value: selected_routes.append(value),
    )

    body, status = setup_core.model_api_route_activate(
        SimpleNamespace(user_id="user-route-repair"),
        route["id"],
        caller_api_key="caller-key",
    )

    assert status == 200
    assert body["active_route_id"] == route["id"]
    assert selected_routes == ["model_api"]
