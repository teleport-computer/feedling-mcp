"""B4 场景字段：provider public_config.last_test_error + onboarding step
（spec Phase B / B4）。

Run:  python -m pytest tests/test_scene_fields.py -q
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client  # noqa: E402

from hosted import onboarding_validation as validation  # noqa: E402


def test_public_config_exposes_last_test_error():
    out = provider_client.public_config({
        "provider": "anthropic", "model": "claude-3-5-sonnet-latest",
        "test_status": "failed", "last_test_error": "403 预扣费额度失败"})
    assert out["last_test_error"] == "403 预扣费额度失败"


def test_public_config_last_test_error_defaults_empty():
    out = provider_client.public_config({"provider": "anthropic", "model": "x"})
    assert out["last_test_error"] == ""


# --- onboarding model_api_test step: function-level, driven via the same
# monkeypatch harness pattern already used in
# tests/test_onboarding_validation_genesis.py (_install_model_api_harness).
# `_model_api_onboarding_validation_payload` is directly callable with a
# minimal UserStore stand-in and mocked `hosted_config_store._load_model_api_config`
# plus an empty genesis job list (no genesis job -> model_api_test step keeps
# its base/non-genesis shape, which is the one this test targets), so it does
# not require the full onboarding route/HTTP context.


def _store(user_id: str = "usr_scene_fields"):
    return types.SimpleNamespace(user_id=user_id)


def _install_model_api_harness(monkeypatch, *, config: dict) -> None:
    monkeypatch.setattr(
        validation.boot_gates,
        "_bootstrap_state",
        lambda _store: {
            "memory_count": 0,
            "memory_floor": 0,
        },
    )
    monkeypatch.setattr(validation.identity_service, "_load_identity", lambda _store: None)
    monkeypatch.setattr(validation.identity_service, "_live_days_with_user", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(validation.hosted_config_store, "_load_model_api_config", lambda _store: config)
    monkeypatch.setattr(
        validation.hosted_config_store,
        "_ensure_model_api_runtime_profile",
        lambda _store, _config: {
            "runtime_mode": validation.hosted_config_store.MODEL_API_RUNTIME_MODE,
            "runtime_version": validation.hosted_config_store.MODEL_API_RUNTIME_VERSION,
            "tool_action_enabled": True,
        },
    )
    monkeypatch.setattr(validation, "_latest_history_import_job", lambda _store: None)
    monkeypatch.setattr(validation, "_model_api_hosted_chat_verified", lambda _store: False)
    monkeypatch.setattr(validation.db, "genesis_list_jobs", lambda _user_id, limit=20: [])


def test_onboarding_model_api_test_step_exposes_last_test_error(monkeypatch):
    _install_model_api_harness(
        monkeypatch,
        config={
            "provider": "openrouter",
            "model": "openai/gpt-4.1-mini",
            "test_status": "failed",
            "last_test_error": "403 预扣费额度失败",
        },
    )

    body = validation._model_api_onboarding_validation_payload(_store())
    steps = {step["id"]: step for step in body["steps"]}

    assert steps["model_api_test"]["last_test_error"] == "403 预扣费额度失败"


def test_onboarding_model_api_test_step_last_test_error_defaults_empty(monkeypatch):
    _install_model_api_harness(
        monkeypatch,
        config={
            "provider": "openrouter",
            "model": "openai/gpt-4.1-mini",
            "test_status": "ok",
        },
    )

    body = validation._model_api_onboarding_validation_payload(_store())
    steps = {step["id"]: step for step in body["steps"]}

    assert steps["model_api_test"]["last_test_error"] == ""
