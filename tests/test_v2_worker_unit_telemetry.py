"""Pure Runtime V2 telemetry/error-code tests (no PostgreSQL required)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import worker  # noqa: E402


@pytest.mark.parametrize(
    "reason",
    [
        "degenerate_reply_suppressed",
        "protocol_fragment_suppressed",
        "malformed_self_thinking_suppressed",
    ],
)
def test_wake_safety_suppressions_keep_distinct_stable_codes(reason):
    exc = worker.TurnError(reason)
    assert worker._safe_failure_code("wake_failed", exc) == f"wake_failed:{reason}"
    assert worker._turn_failure_error_class(exc) == "reply_parse_failed"


def test_provider_attempt_ledger_inherits_job_lane_when_event_omits_it(monkeypatch):
    captured = {}

    def _capture(user_id, event_kind, payload, **kwargs):
        captured.update(
            user_id=user_id,
            event_kind=event_kind,
            payload=payload,
            kwargs=kwargs,
        )

    monkeypatch.setattr(worker, "_note_provider_attempt", _capture)

    class _Recorder:
        user_id = "u_lane"
        job_id = 41
        _ledger_lane = "maintenance"
        _ledger_route = ("anthropic", "claude-test")

    original = {"error_class": "upstream_unavailable"}
    asyncio.run(
        worker._mirror_provider_attempt(_Recorder(), "provider_error", original)
    )

    assert original == {"error_class": "upstream_unavailable"}
    assert captured["payload"]["lane"] == "maintenance"
    assert captured["kwargs"]["provider"] == "anthropic"


@pytest.mark.parametrize(
    ("self_on", "self_text", "failed", "provider_text", "expected"),
    [
        (True, "agent summary", False, "private native cot", "self"),
        (True, "", True, "private native cot", "marker"),
        (True, "", False, "private native cot", "none"),
        (False, "", False, "private native cot", "native_legacy"),
    ],
)
def test_thinking_surface_selector_has_four_explicit_branches(
    self_on, self_text, failed, provider_text, expected
):
    text, kind, source, native, branch = worker._select_thinking_surface(
        provider_text,
        self_thinking_on=self_on,
        self_thinking_text=self_text,
        self_thinking_failed=failed,
    )

    assert branch == expected
    if expected == "self":
        assert text == self_text
        assert (kind, source, native) == (
            "agent_summary",
            "self_thinking",
            False,
        )
    elif expected == "marker":
        assert text == worker.self_thinking.THINKING_FAILED_MARKER
    elif expected == "none":
        assert text == ""
    else:
        assert text == provider_text
        assert (kind, source, native) == ("provider_reasoning", None, True)


def test_thinking_surface_trace_contains_metadata_only():
    captured = {}

    def _emit(user_id, event_type, **fields):
        captured.update(user_id=user_id, event_type=event_type, **fields)

    config = type("Provider", (), {"model": "model-safe-name"})()
    asyncio.run(
        worker._emit_thinking_surfaced_trace(
            _emit,
            "u_trace",
            config,
            lane="chat",
            branch="self",
            chars=17,
        )
    )

    assert captured["event_type"] == "thinking.surfaced"
    assert captured["detail"] == {
        "branch": "self",
        "chars": 17,
        "model": "model-safe-name",
        "lane": "chat",
    }
    assert set(captured["detail"]) == {"branch", "chars", "model", "lane"}


def test_thinking_surface_trace_bounds_user_configured_model_name():
    captured = {}

    def _emit(_user_id, _event_type, **fields):
        captured.update(fields)

    overlong_model = "relay-model-" * 20
    config = type("Provider", (), {"model": overlong_model})()
    asyncio.run(
        worker._emit_thinking_surfaced_trace(
            _emit,
            "u_trace",
            config,
            lane="wake",
            branch="none",
            chars=0,
        )
    )

    assert captured["detail"]["model"] == overlong_model[:96]
    assert len(captured["detail"]["model"]) < len(overlong_model)


def test_thinking_surface_has_dedicated_admin_timeline_label():
    from admin import data_track

    assert data_track._debug_friendly_step(
        {"type": "thinking.surfaced", "subsystem": "agent"}
    ) == ("💭", "思考展示 · 分支")
