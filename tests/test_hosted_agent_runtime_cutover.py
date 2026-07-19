"""Hosted response compatibility after resident-process retirement."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import agent_runtime_cutover as cutover  # noqa: E402


@pytest.mark.parametrize(
    ("provider", "driver"),
    [
        ("anthropic", "claude"),
        ("deepseek", "claude"),
        ("openai", "codex"),
        ("gemini", "pi"),
        ("openrouter", "pi"),
        ("openai_compatible", "pi"),
    ],
)
def test_provider_label_remains_compatible(provider, driver):
    assert cutover.driver_for_provider(provider) == driver
    assert cutover.resolve_driver({"provider": provider}) == driver


def test_resolve_driver_rejects_missing_provider():
    with pytest.raises(cutover.UnsupportedProviderError):
        cutover.resolve_driver(None)


def test_hosting_ready_accepts_runtime_token_secret(monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "test-secret")
    cutover.assert_hosting_ready()


def test_hosting_ready_rejects_retired_policy(monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "resident_only")
    with pytest.raises(RuntimeError, match="FEEDLING_HOSTED_RUNTIME_POLICY"):
        cutover.assert_hosting_ready()


def test_hosting_ready_requires_runtime_token_secret(monkeypatch):
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="FEEDLING_RUNTIME_TOKEN_SECRET"):
        cutover.assert_hosting_ready()


def test_processing_response_is_async_ciphertext_reference():
    body, status = cutover.build_processing_response(
        {"id": "u1", "ts": 1.0}, driver="claude"
    )
    assert status == 202
    assert body["status"] == "processing"
    assert body["reply_ready"] is False
    assert "reply" not in body
    assert body["user_message"] == {"id": "u1", "ts": 1.0}
    assert body["runtime"]["driver"] == "claude"


def test_resident_supervisor_helpers_are_not_reachable():
    for name in (
        "check_supervisor_live",
        "evaluate_supervisor_heartbeat",
        "wait_for_reply",
        "handle_send",
        "build_ready_response",
    ):
        assert not hasattr(cutover, name)
