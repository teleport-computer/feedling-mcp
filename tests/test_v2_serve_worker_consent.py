"""Production assembly consent gates for Runtime V2 extraction lanes."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker  # noqa: E402


@pytest.mark.parametrize(
    ("helper_name", "deployment_flag", "setting_key"),
    [
        ("_capture_enabled_for_user", "_CAPTURE_ENABLED", "capture_enabled"),
        ("_dream_enabled_for_user", "_DREAM_ENABLED", "dream_enabled"),
    ],
)
def test_extraction_consent_helpers_are_symmetric(
    monkeypatch,
    helper_name,
    deployment_flag,
    setting_key,
):
    helper = getattr(serve_worker, helper_name)
    monkeypatch.setattr(serve_worker, deployment_flag, True)

    monkeypatch.setattr(
        serve_worker.db,
        "get_blob_strict",
        lambda _uid, _kind: {setting_key: False},
    )
    assert helper("u_disabled") is False

    monkeypatch.setattr(
        serve_worker.db,
        "get_blob_strict",
        lambda _uid, _kind: None,
    )
    assert helper("u_missing_settings") is True

    monkeypatch.setattr(serve_worker, deployment_flag, False)
    monkeypatch.setattr(
        serve_worker.db,
        "get_blob_strict",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("deployment-off must not read settings")
        ),
    )
    assert helper("u_deployment_off") is False

    monkeypatch.setattr(serve_worker, deployment_flag, True)
    monkeypatch.setattr(
        serve_worker.db,
        "get_blob_strict",
        lambda _uid, _kind: ["malformed"],
    )
    with pytest.raises(RuntimeError, match="proactive settings malformed"):
        helper("u_malformed")


def test_production_deps_wire_the_real_consent_helpers(monkeypatch):
    monkeypatch.setattr(serve_worker, "_CAPTURE_ENABLED", True)
    monkeypatch.setattr(serve_worker, "_DREAM_ENABLED", True)
    monkeypatch.setattr(
        serve_worker.db,
        "get_blob_strict",
        lambda _uid, _kind: {
            "capture_enabled": False,
            "dream_enabled": False,
        },
    )

    deps = serve_worker.build_production_deps()

    assert deps.capture_enabled is serve_worker._capture_enabled_for_user
    assert deps.dream_enabled is serve_worker._dream_enabled_for_user
    assert deps.capture_enabled("u_disabled") is False
    assert deps.dream_enabled("u_disabled") is False
