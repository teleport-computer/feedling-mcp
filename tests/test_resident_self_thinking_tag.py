"""T587: the resident consumer picks the self-thinking tag by driver.

Claude Code driver → ``aside``; pi / codex → ``think`` rendered byte for byte as
before this change (pinned by sha256 of the exact strings the consumer emitted
on origin/test c23df32c, 2026-09-15).
"""
from __future__ import annotations

import hashlib
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

_PRE_CHANGE_SHA = {
    "wake_zh": "dfe7299ccaa209aa09450b5dbff69aeba97dd50677b745f4e0b1104d36cea4da",
    "wake_en": "4361a7a37d958140acc50dbe823e98c055cf6ac063caf027d375b18aa7a03f69",
    "foreground": "69153a072882272872ee97fbd443f8d5dd58e96062d05ccc7fad7ff81db8751e",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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
def test_non_claude_drivers_keep_think_byte_for_byte(monkeypatch, cmd):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", cmd)
    _assert_think_rendering_unchanged()


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
    _assert_think_rendering_unchanged()


def _assert_think_rendering_unchanged():
    assert crc._self_thinking_tag() == st.TAG_THINK
    assert crc._foreground_self_thinking_instruction() == st.INSTRUCTION.strip()
    assert _sha(crc._foreground_self_thinking_instruction()) == _PRE_CHANGE_SHA["foreground"]
    assert _sha(crc._wake_think_permission_line({"locale": "zh-Hans"})) == _PRE_CHANGE_SHA["wake_zh"]
    assert _sha(crc._wake_think_permission_line({"locale": "en-US"})) == _PRE_CHANGE_SHA["wake_en"]


@pytest.mark.parametrize("cmd", [
    'claude -p --model claude-opus-5 "{message}"',
    '/opt/homebrew/bin/claude --print "{message}"',
])
def test_claude_driver_uses_aside_everywhere(monkeypatch, cmd):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", cmd)
    assert crc._self_thinking_tag() == st.TAG_ASIDE
    fg = crc._foreground_self_thinking_instruction()
    assert fg == st.instruction(st.TAG_ASIDE).strip()
    assert "<think>" not in fg and "<aside>" in fg
    for locale in ("zh-Hans", "en-US"):
        line = crc._wake_think_permission_line({"locale": locale})
        assert "<think>" not in line and "<aside>...</aside>" in line
        # No private-channel wording in the aside rendering.
        assert "保持私密" not in line and "stays private" not in line


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
