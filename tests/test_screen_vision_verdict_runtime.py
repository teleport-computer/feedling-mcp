"""Hosted V1 propagation and learning for the setup pixel-probe verdict."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent_runtime import spawners, supervisor  # noqa: E402
from hosted import config_store  # noqa: E402


def test_consumer_env_receives_pixel_probe_verdict():
    env = spawners.consumer_env(
        {"PATH": "/bin"},
        {
            "api_key": "fk",
            "driver": "claude",
            "provider": "anthropic",
            "model": "claude-test",
            "vision_test_status": "ok",
        },
        user_id="u1",
        home="/agent-data/users/u1",
    )

    assert env["FEEDLING_AGENT_VISION_TEST_STATUS"] == "ok"


def test_probe_verdict_change_rotates_hosted_consumer():
    before = {
        "driver": "claude",
        "provider": "anthropic",
        "model": "claude-test",
        "vision_test_status": "ok",
    }
    after = {**before, "vision_test_status": "unsupported"}

    assert supervisor._spawn_identity(before) != supervisor._spawn_identity(after)


def test_runtime_image_rejection_marks_exact_active_route_unsupported(monkeypatch):
    route = {
        "id": "route-1",
        "vision_test_status": "ok",
        "updated_at": "2026-08-10T12:00:00.000000Z",
    }
    calls = []
    monkeypatch.setattr(
        config_store.db,
        "model_api_active_route_vision_verdict",
        lambda user_id: route if user_id == "u1" else None,
    )
    monkeypatch.setattr(
        config_store.db,
        "model_api_route_mark_vision_test",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        config_store.db,
        "model_api_route_mark_runtime_error",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("vision learning is not a generic runtime failure")
        ),
    )

    body, status = config_store.record_runtime_error(
        SimpleNamespace(user_id="u1"),
        error="",
        error_class="vision_model_incompatible",
        provider_result="vision_unsupported",
    )

    assert (body, status) == ({"ok": True}, 200)
    assert calls == [
        (
            ("u1", "route-1"),
            {
                "status": "unsupported",
                "error": "vision_model_incompatible",
                "expected_updated_at": "2026-08-10T12:00:00.000000Z",
            },
        )
    ]
