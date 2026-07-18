"""Hosted model-API ownership is Runtime V2 only."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from conftest import configure_model_api_route, seed_user  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import config_store  # noqa: E402


def test_persisted_resident_value_is_only_a_dormant_compatibility_state():
    assert config_store.effective_hosted_runtime_mode("resident_cli") == "resident_cli"
    assert config_store.effective_hosted_runtime_mode(None) == "resident_cli"


def test_admin_cannot_select_hosted_resident(monkeypatch):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    with pytest.raises(ValueError, match="resident runtime is retired"):
        config_store.set_hosted_runtime_mode(
            SimpleNamespace(user_id="unused"),
            config_store.HOSTED_RUNTIME_MODE_RESIDENT,
        )


def test_only_v2_policy_is_accepted(monkeypatch):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    assert config_store.hosted_runtime_policy() == "v2_only"
    assert (
        config_store.forced_hosted_runtime_mode()
        == config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )
    for retired in ("per_user", "resident_only"):
        monkeypatch.setenv(config_store.HOSTED_RUNTIME_POLICY_ENV, retired)
        with pytest.raises(RuntimeError, match="FEEDLING_HOSTED_RUNTIME_POLICY"):
            config_store.hosted_runtime_policy()


def test_v2_mode_persists_with_generation_fence(backend_env, monkeypatch):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    uid = f"runtime_v2_only_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    configure_model_api_route(
        uid, provider="anthropic", model="claude-test", test_status="ok"
    )
    store = core_store.get_store(uid)

    assert config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    ) == config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    assert config_store.get_hosted_runtime_control_strict(store)[:2] == (
        "db_action_v2",
        "v2",
    )


def test_route_delete_uses_dormant_fence_without_enabling_resident(
    backend_env, monkeypatch
):
    monkeypatch.delenv(config_store.HOSTED_RUNTIME_POLICY_ENV, raising=False)
    uid = f"runtime_delete_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    configure_model_api_route(
        uid, provider="anthropic", model="claude-test", test_status="ok"
    )
    store = core_store.get_store(uid)
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )
    before = db.get_runtime_generation(uid)

    config_store.prepare_model_api_delete(store)

    assert db.get_hosted_runtime_control_strict(uid) == (
        "resident_cli",
        "resident",
        before + 2,
    )
    with pytest.raises(ValueError, match="resident runtime is retired"):
        config_store.set_hosted_runtime_mode(
            store, config_store.HOSTED_RUNTIME_MODE_RESIDENT
        )
