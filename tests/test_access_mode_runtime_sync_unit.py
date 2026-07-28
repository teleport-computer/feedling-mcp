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


def test_model_api_switch_never_bypasses_allowlist_reconciler(monkeypatch):
    store, saved = _stub_access_switch(monkeypatch)
    monkeypatch.setattr(
        config_store,
        "set_hosted_runtime_mode",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("access-mode must not directly flip V2")
        ),
    )

    body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "model_api"}
    )

    assert status == 200
    assert body["active_route"] == "model_api"
    assert saved == ["model_api"]


def test_model_api_switch_does_not_require_an_active_route(monkeypatch):
    store, saved = _stub_access_switch(monkeypatch)
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
        "get_hosted_runtime_control_strict",
        lambda _store: (
            config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
            "v2",
            9,
        ),
    )
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


def test_partial_runtime_failure_rolls_back_runtime_and_access_mode(monkeypatch):
    store, saved = _stub_access_switch(monkeypatch, previous="model_api")
    selected: list[str] = []
    monkeypatch.setattr(
        config_store,
        "get_hosted_runtime_control_strict",
        lambda _store: (
            config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
            "v2",
            9,
        ),
    )

    def fail_after_partial_write(_store, mode):
        selected.append(mode)
        if len(selected) == 1:
            raise RuntimeError("state write failed after blob write")
        return mode

    monkeypatch.setattr(
        config_store, "set_hosted_runtime_mode", fail_after_partial_write
    )

    body, status = accounts_core.access_modes_switch(
        store, {"access_mode": "resident"}
    )

    assert status == 503
    assert body == {"error": "runtime_control_unavailable"}
    assert selected == [
        config_store.HOSTED_RUNTIME_MODE_RESIDENT,
        config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
    ]
    assert saved == ["resident", "model_api"]
