"""Resident drivers share the optional JSON aside contract."""
from __future__ import annotations

import os

import pytest

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_checkpoint.json",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

import tools.chat_resident_consumer as crc  # noqa: E402
from agent_protocol_core import self_thinking as st  # noqa: E402

@pytest.fixture(autouse=True)
def _self_thinking_on(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    monkeypatch.setitem(crc.AGENT_RUNTIME_METADATA, "model", "claude-sonnet-4-6")
    # The tag follows the mode that actually executes the turn; every case
    # below states its mode explicitly instead of inheriting the env default.
    monkeypatch.setattr(crc, "AGENT_MODE", "cli")


@pytest.mark.parametrize("cmd", [
    'pi --model openrouter/x "{message}"',
    'codex exec --json "{message}"',
    '/usr/local/bin/pi "{message}"',
    'openclaw agent --json "{message}"',  # not Claude Code → unchanged
    "",  # no CLI configured → unchanged
])
def test_non_claude_drivers_use_json_aside(monkeypatch, cmd):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", cmd)
    _assert_field_rendering()


def test_http_mode_ignores_a_leftover_claude_command(monkeypatch):
    # codex3 review (T587 R1): in http mode AGENT_CLI_CMD never runs, so it must
    # not change the copy either. The turn really routes to HTTP here.
    monkeypatch.setattr(crc, "AGENT_MODE", "http")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'claude -p "{message}"')
    called = []

    def fake_http(message, **kwargs):
        called.append("http")
        return "public reply"

    monkeypatch.setattr(crc, "call_agent_http", fake_http)
    crc.call_agent("test", raw_text=True)
    assert called == ["http"]
    assert crc._self_thinking_tag() == st.TAG_THINK
    _assert_field_rendering()


def _assert_field_rendering():
    expected = st.instruction_for_field(protocol="json").strip()
    assert crc._foreground_self_thinking_instruction() == expected
    for locale in ("zh-Hans", "en-US"):
        assert crc._wake_think_permission_line({"locale": locale}) == expected
    assert "<think>" not in expected and "<aside>" not in expected


@pytest.mark.parametrize("cmd", [
    'claude -p --model claude-opus-5 "{message}"',
    '/opt/homebrew/bin/claude --print "{message}"',
])
def test_claude_driver_uses_aside_everywhere(monkeypatch, cmd):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", cmd)
    assert crc._self_thinking_tag() == st.TAG_ASIDE
    _assert_field_rendering()


def test_truncated_aside_never_becomes_a_visible_message(monkeypatch):
    # codex3 review (T587 R2): the real consumer outlet, not just the parser.
    monkeypatch.setenv("FEEDLING_THINK_GATE", "1")
    for text in ("<asid", "<thin"):
        assert crc._agent_turn_from_obj(text).messages == []


def test_switch_off_hides_both_tags(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "0")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'claude -p "{message}"')
    assert crc._foreground_self_thinking_instruction() == ""
    assert crc._wake_think_permission_line({"locale": "zh-Hans"}) == ""
