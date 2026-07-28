from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from accounts import access, accounts_core, onboarding, registry  # noqa: E402
from hosted import config_store  # noqa: E402


def _stub_access_switch(monkeypatch, *, previous: str = "resident"):
    saved: list[str] = []
    monkeypatch.setattr(onboarding, "_load_onboarding_route", lambda _store: previous)

    def save(_store, mode: str):
        saved.append(mode)
        return {"route": mode}

    monkeypatch.setattr(onboarding, "_save_onboarding_route", save)
    monkeypatch.setattr(registry, "_find_user_entry_locked", lambda _uid: None)
    monkeypatch.setattr(
        access,
        "_access_modes_payload",
        lambda _store: {"active_route": saved[-1]},
    )
    return SimpleNamespace(user_id="usr_test"), saved


def test_model_api_switch_moves_configured_runtime_to_v2(monkeypatch):
    store, saved = _stub_access_switch(monkeypatch)
    selected: list[str] = []
    monkeypatch.setattr(config_store, "load_active_route", lambda _store: {"id": "r1"})
    monkeypatch.setattr(
        config_store,
        "set_hosted_runtime_mode",
        lambda _store, mode: selected.append(mode) or mode,
    )

    body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "model_api"}
    )

    assert status == 200
    assert body["active_route"] == "model_api"
    assert saved == ["model_api"]
    assert selected == [config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2]


def test_model_api_onboarding_without_route_does_not_flip_runtime(monkeypatch):
    store, saved = _stub_access_switch(monkeypatch)
    monkeypatch.setattr(config_store, "load_active_route", lambda _store: None)
    monkeypatch.setattr(
        config_store,
        "set_hosted_runtime_mode",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected switch")),
    )

    body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "model_api"}
    )

    assert status == 200
    assert body["active_route"] == "model_api"
    assert saved == ["model_api"]


def test_resident_switch_moves_runtime_back_to_resident(monkeypatch):
    store, saved = _stub_access_switch(monkeypatch, previous="model_api")
    selected: list[str] = []
    monkeypatch.setattr(
        config_store,
        "set_hosted_runtime_mode",
        lambda _store, mode: selected.append(mode) or mode,
    )

    body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "resident"}
    )

    assert status == 200
    assert body["active_route"] == "resident"
    assert saved == ["resident"]
    assert selected == [config_store.HOSTED_RUNTIME_MODE_RESIDENT]


def test_runtime_failure_rolls_back_access_mode(monkeypatch):
    store, saved = _stub_access_switch(monkeypatch)
    monkeypatch.setattr(config_store, "load_active_route", lambda _store: {"id": "r1"})

    def fail(_store, _mode):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(config_store, "set_hosted_runtime_mode", fail)

    body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "model_api"}
    )

    assert status == 503
    assert body == {"error": "runtime_control_unavailable"}
    assert saved == ["model_api", "resident"]
