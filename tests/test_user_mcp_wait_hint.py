"""
The claude-only "you may WaitForMcpServers" prompt hint
=======================================================

Spec: docs/superpowers/specs/2026-08-13-mcp-handshake-wait-hint-design.md

claude CLI emits its ``init`` snapshot ~2.5s after start and runs the turn with
whatever MCP servers connected in time; the rest contribute ZERO tools, so the
model does not know the capability exists. The consumer spawns a fresh
``claude --print`` per turn, so claude's own "it'll be there next turn" recovery
never gets a next turn.

These lock the three gates. The load-bearing one is the driver gate: codex and
pi both wait for their handshake AND have no ``WaitForMcpServers``, so injecting
there would send the model after a tool that does not exist — trading "no
failure" for "a new failure".

Run with:
    cd backend && PYTHONPATH=. /path/to/venv/python -m pytest \
        ../tests/test_user_mcp_wait_hint.py -v
"""

import os
import sys
import types
from pathlib import Path

# Same module bootstrap as the sibling consumer suites — consumer reads env at
# module scope, and these setdefault()s leak cross-module, so the values must
# match test_user_mcp_consumer.py exactly.
_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "cli",
    "AGENT_CLI_CMD": "claude --allowed-tools 'x' {mcp} -p {message}",
    "CHECKPOINT_FILE": "/tmp/feedling_test_user_mcp_checkpoint.json",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as c  # noqa: E402

CLAUDE_TPL = "claude --allowed-tools 'x' {mcp} -p {message}"
CODEX_TPL = "codex exec --json {mcp} {message}"
PI_TPL = "pi {mcp} --mode json {message}"

TWO_SERVERS = {
    "fingerprint": "sha256:x",
    "servers": [
        {"name": "tavily_", "enabled": True,
         "url": "https://mcp.tavily.example/mcp",
         "headers": {"Authorization": "Bearer SECRET-TOKEN"}},
        {"name": "ombre", "enabled": True,
         "url": "https://ombre.example/mcp", "headers": {}},
        {"name": "disabled_one", "enabled": False,
         "url": "https://off.example/mcp", "headers": {}},
    ],
}


def _apply(monkeypatch, applied, template=CLAUDE_TPL, mode="cli"):
    monkeypatch.setattr(c, "_user_mcp_applied", applied)
    monkeypatch.setattr(c, "AGENT_CLI_CMD", template)
    monkeypatch.setattr(c, "AGENT_MODE", mode)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_claude_chat_turn_gets_the_hint_with_every_enabled_server(monkeypatch):
    _apply(monkeypatch, TWO_SERVERS)
    out = c._prepend_user_mcp_wait_hint("用户说的话", lane="chat")

    assert out.endswith("用户说的话")
    assert "WaitForMcpServers" in out
    assert "tavily_" in out and "ombre" in out
    # Disabled servers are not wired into --mcp-config, so naming them would
    # point the model at a server it cannot reach.
    assert "disabled_one" not in out


def test_hint_never_leaks_urls_or_header_values(monkeypatch):
    """The hint carries names only. URLs and header values are the secret part
    of an MCP config (`_public()` in mcp_core strips them even from the app's
    own list endpoint); a prompt is the last place they should surface."""
    _apply(monkeypatch, TWO_SERVERS)
    out = c._prepend_user_mcp_wait_hint("x", lane="chat")

    assert "SECRET-TOKEN" not in out
    assert "Authorization" not in out
    assert "mcp.tavily.example" not in out
    assert "https://" not in out


def test_server_order_is_stable_regardless_of_backend_order(monkeypatch):
    """Same config must render the same bytes however the backend happened to
    order the list — the rule `user_mcp_materialize._enabled` already applies to
    the emitted argv."""
    _apply(monkeypatch, TWO_SERVERS)
    first = c._prepend_user_mcp_wait_hint("x", lane="chat")

    reversed_applied = dict(TWO_SERVERS)
    reversed_applied["servers"] = list(reversed(TWO_SERVERS["servers"]))
    _apply(monkeypatch, reversed_applied)
    assert c._prepend_user_mcp_wait_hint("x", lane="chat") == first


# ---------------------------------------------------------------------------
# The three gates — each returns the SAME object, not merely an equal string
# ---------------------------------------------------------------------------


def test_gate_non_chat_lane_is_byte_identical(monkeypatch):
    """Background/proactive turns are never wired with MCP at all
    (`_user_mcp_cli_value` returns "" off the chat lane), so naming servers
    there advertises tools the model does not have."""
    _apply(monkeypatch, TWO_SERVERS)
    content = "背景轮次"
    for lane in ("background", "proactive", "voice", ""):
        assert c._prepend_user_mcp_wait_hint(content, lane=lane) is content


def test_gate_non_claude_driver_is_byte_identical(monkeypatch):
    """LOAD-BEARING. codex blocks for startup_timeout_sec=20 and pi's bridge is
    awaited, so neither has the race — and neither has WaitForMcpServers.
    Injecting there sends the model after a nonexistent tool."""
    content = "用户说的话"
    for tpl in (CODEX_TPL, PI_TPL):
        _apply(monkeypatch, TWO_SERVERS, template=tpl)
        assert c._prepend_user_mcp_wait_hint(content, lane="chat") is content


def test_gate_no_enabled_server_is_byte_identical(monkeypatch):
    """Gates on the in-memory applied state, not on-disk file existence: a stale
    /tmp config can outlive the servers it was written for (same reasoning as
    `_user_mcp_child_env`)."""
    content = "用户说的话"
    for applied in (
        {"fingerprint": None, "servers": []},
        {"fingerprint": "sha256:x", "servers": [
            {"name": "off", "enabled": False, "url": "https://x", "headers": {}}]},
        {},
    ):
        _apply(monkeypatch, applied)
        assert c._prepend_user_mcp_wait_hint(content, lane="chat") is content


def test_gate_http_backend_agent_is_byte_identical(monkeypatch):
    """AGENT_MODE != "cli" means an http-backend agent (hermes etc.) — it never
    spawns a claude process, so there is no claude built-in to point at."""
    _apply(monkeypatch, TWO_SERVERS, mode="http")
    content = "用户说的话"
    assert c._prepend_user_mcp_wait_hint(content, lane="chat") is content


# ---------------------------------------------------------------------------
# Wording invariants the cross-model evidence depends on (spec §5.3)
# ---------------------------------------------------------------------------


def test_hint_tells_the_model_not_to_give_up_and_to_bound_its_waiting(monkeypatch):
    """Two clauses carry weight and were measured together, so they are locked:
    "don't report it as unavailable" is what stops the truthful-but-wrong "I
    can't"; "only the one you need, at most once" bounds the cost Codex flagged
    (repeated Wait calls stacking, and waiting on a server the turn never needs).
    """
    _apply(monkeypatch, TWO_SERVERS)
    out = c._prepend_user_mcp_wait_hint("x", lane="chat")

    assert "不要因为" in out and "用不了" in out
    assert "最多等一次" in out
    assert "只等你真正需要的那一台" in out
