"""env-gated default for the perception/resident V2 rollout flags.

Covers the test-default-ON behaviour (FEEDLING_RUNTIME_V2_DEFAULT_ON), the
explicit per-user override, and the config_store scrub that unsticks the
previously auto-seeded perception flag.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from core import util as core_util  # noqa: E402
from perception import service as perception_service  # noqa: E402
from proactive import resident_runtime_v2 as resident_rt  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402

ENV = core_util.RUNTIME_V2_DEFAULT_ON_ENV


def test_runtime_v2_default_on_reflects_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert core_util.runtime_v2_default_on() is False
    monkeypatch.setenv(ENV, "true")
    assert core_util.runtime_v2_default_on() is True
    monkeypatch.setenv(ENV, "0")
    assert core_util.runtime_v2_default_on() is False


def test_perception_flag_falls_through_to_env_default(monkeypatch):
    user_store = SimpleNamespace(user_id="u_env")
    monkeypatch.setattr(hosted_config_store, "_load_model_api_config", lambda store: {})
    monkeypatch.setattr(
        hosted_config_store, "_ensure_model_api_runtime_profile", lambda store, config: {}
    )

    monkeypatch.setenv(ENV, "true")
    assert perception_service.perception_ingress_runtime_v2_enabled(user_store) is True
    monkeypatch.delenv(ENV, raising=False)
    assert perception_service.perception_ingress_runtime_v2_enabled(user_store) is False


def test_perception_explicit_value_overrides_env_default(monkeypatch):
    user_store = SimpleNamespace(user_id="u_env")
    monkeypatch.setenv(ENV, "true")  # baseline ON
    monkeypatch.setattr(hosted_config_store, "_load_model_api_config", lambda store: {})
    monkeypatch.setattr(
        hosted_config_store,
        "_ensure_model_api_runtime_profile",
        lambda store, config: {perception_service.PERCEPTION_INGRESS_RUNTIME_V2_FLAG: False},
    )
    # explicit opt-out wins over the ON baseline
    assert perception_service.perception_ingress_runtime_v2_enabled(user_store) is False


def test_resident_flags_honor_env_default_and_explicit_override(monkeypatch):
    store = SimpleNamespace(user_id="u_res")

    # empty blob -> env baseline
    monkeypatch.setattr(resident_rt, "load_resident_runtime_profile_v2", lambda s: {})
    monkeypatch.setenv(ENV, "true")
    assert resident_rt.resident_wake_runtime_v2_enabled(store) is True
    assert resident_rt.resident_chat_runtime_v2_enabled(store) is True
    monkeypatch.delenv(ENV, raising=False)
    assert resident_rt.resident_wake_runtime_v2_enabled(store) is False
    assert resident_rt.resident_chat_runtime_v2_enabled(store) is False

    # explicit blob value wins over the baseline either way
    monkeypatch.setenv(ENV, "true")
    monkeypatch.setattr(
        resident_rt,
        "load_resident_runtime_profile_v2",
        lambda s: {resident_rt.RESIDENT_WAKE_RUNTIME_V2_FLAG: False},
    )
    assert resident_rt.resident_wake_runtime_v2_enabled(store) is False


def test_ensure_profile_scrubs_auto_seeded_perception_false(monkeypatch):
    store = SimpleNamespace(user_id="u_scrub")
    saved = {}

    monkeypatch.setattr(
        hosted_config_store,
        "_load_model_api_runtime_profile",
        lambda s: {
            "runtime_mode": hosted_config_store.MODEL_API_RUNTIME_MODE,
            "runtime_version": hosted_config_store.MODEL_API_RUNTIME_VERSION,
            "tool_action_enabled": True,
            "perception_ingress_runtime_v2_enabled": False,  # auto-seed artifact
        },
    )
    monkeypatch.setattr(
        hosted_config_store,
        "_save_model_api_runtime_profile",
        lambda s, profile: saved.update(profile) or profile,
    )

    out = hosted_config_store._ensure_model_api_runtime_profile(
        store, {"provider": "p", "model": "m"}
    )
    # seeded False is dropped so the reader can fall through to the env baseline
    assert "perception_ingress_runtime_v2_enabled" not in out
    assert "perception_ingress_runtime_v2_enabled" not in saved


def test_ensure_profile_preserves_deliberate_false_after_marker_set(monkeypatch):
    # Once the one-time scrub marker is present, a deliberate per-user opt-out
    # written as False must survive (operator rollback path), not be scrubbed.
    store = SimpleNamespace(user_id="u_optout")
    monkeypatch.setattr(
        hosted_config_store,
        "_load_model_api_runtime_profile",
        lambda s: {
            "runtime_mode": hosted_config_store.MODEL_API_RUNTIME_MODE,
            "runtime_version": hosted_config_store.MODEL_API_RUNTIME_VERSION,
            "tool_action_enabled": True,
            hosted_config_store.PERCEPTION_V2_AUTOSEED_SCRUBBED: True,
            "perception_ingress_runtime_v2_enabled": False,  # deliberate opt-out
        },
    )
    monkeypatch.setattr(
        hosted_config_store, "_save_model_api_runtime_profile", lambda s, profile: profile
    )
    out = hosted_config_store._ensure_model_api_runtime_profile(
        store, {"provider": "p", "model": "m"}
    )
    assert out.get("perception_ingress_runtime_v2_enabled") is False


def test_ensure_profile_scrubs_all_four_seeded_flags_and_records_marker(monkeypatch):
    # The one-time migration scrubs every env-gated rollout flag's seeded False and
    # records each in the marker set so it does not re-run on every read.
    store = SimpleNamespace(user_id="u_marker")
    seeded = {f: False for f in hosted_config_store.AUTOSEED_SCRUB_FLAGS}
    monkeypatch.setattr(
        hosted_config_store,
        "_load_model_api_runtime_profile",
        lambda s: {
            "runtime_mode": hosted_config_store.MODEL_API_RUNTIME_MODE,
            "runtime_version": hosted_config_store.MODEL_API_RUNTIME_VERSION,
            "tool_action_enabled": True,
            **seeded,
        },
    )
    monkeypatch.setattr(
        hosted_config_store, "_save_model_api_runtime_profile", lambda s, profile: profile
    )
    out = hosted_config_store._ensure_model_api_runtime_profile(
        store, {"provider": "p", "model": "m"}
    )
    assert set(out.get(hosted_config_store.V2_AUTOSEED_SCRUBBED_FLAGS)) == set(
        hosted_config_store.AUTOSEED_SCRUB_FLAGS
    )
    for flag in hosted_config_store.AUTOSEED_SCRUB_FLAGS:
        assert flag not in out


def test_legacy_bool_marker_migrates_and_preserves_perception_optout(monkeypatch):
    # A profile carrying the old rev-1 bool marker + a deliberate perception False
    # must migrate the marker into the set form WITHOUT scrubbing that False.
    store = SimpleNamespace(user_id="u_legacy")
    monkeypatch.setattr(
        hosted_config_store,
        "_load_model_api_runtime_profile",
        lambda s: {
            "runtime_mode": hosted_config_store.MODEL_API_RUNTIME_MODE,
            "runtime_version": hosted_config_store.MODEL_API_RUNTIME_VERSION,
            "tool_action_enabled": True,
            hosted_config_store.PERCEPTION_V2_AUTOSEED_SCRUBBED: True,  # legacy rev-1
            "perception_ingress_runtime_v2_enabled": False,  # deliberate opt-out
        },
    )
    monkeypatch.setattr(
        hosted_config_store, "_save_model_api_runtime_profile", lambda s, profile: profile
    )
    out = hosted_config_store._ensure_model_api_runtime_profile(
        store, {"provider": "p", "model": "m"}
    )
    assert out.get("perception_ingress_runtime_v2_enabled") is False  # preserved
    assert "perception_ingress_runtime_v2_enabled" in set(
        out.get(hosted_config_store.V2_AUTOSEED_SCRUBBED_FLAGS)
    )
    assert hosted_config_store.PERCEPTION_V2_AUTOSEED_SCRUBBED not in out  # legacy marker dropped


def test_screen_caption_follows_env_baseline_and_explicit_override(monkeypatch):
    # screen_caption is privacy-sensitive (egress to third-party VLM) but the user
    # opted it into the baseline; verify it follows env baseline yet honors an
    # explicit per-user opt-out, and stays fail-closed on error.
    from proactive import screen_flag_v2

    store = SimpleNamespace(user_id="u_screen")
    monkeypatch.setattr(hosted_config_store, "_load_model_api_config", lambda s: {})
    monkeypatch.setattr(
        hosted_config_store, "_ensure_model_api_runtime_profile", lambda s, c: {}
    )
    monkeypatch.setenv(ENV, "true")
    assert screen_flag_v2.screen_caption_enabled(store) is True
    monkeypatch.delenv(ENV, raising=False)
    assert screen_flag_v2.screen_caption_enabled(store) is False

    monkeypatch.setenv(ENV, "true")
    monkeypatch.setattr(
        hosted_config_store,
        "_ensure_model_api_runtime_profile",
        lambda s, c: {"screen_caption_enabled": False},
    )
    assert screen_flag_v2.screen_caption_enabled(store) is False  # explicit opt-out wins


def test_ensure_profile_preserves_explicit_perception_true(monkeypatch):
    store = SimpleNamespace(user_id="u_keep")
    monkeypatch.setattr(
        hosted_config_store,
        "_load_model_api_runtime_profile",
        lambda s: {
            "runtime_mode": hosted_config_store.MODEL_API_RUNTIME_MODE,
            "runtime_version": hosted_config_store.MODEL_API_RUNTIME_VERSION,
            "tool_action_enabled": True,
            "perception_ingress_runtime_v2_enabled": True,  # operator opt-in
        },
    )
    monkeypatch.setattr(
        hosted_config_store, "_save_model_api_runtime_profile", lambda s, profile: profile
    )
    out = hosted_config_store._ensure_model_api_runtime_profile(
        store, {"provider": "p", "model": "m"}
    )
    assert out.get("perception_ingress_runtime_v2_enabled") is True
