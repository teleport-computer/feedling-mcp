"""CI-safe smoke test for scripts/provider_probe/probe.py — imports the
MANUAL live-probe module and checks its pure, network-free surface
(build_probe_tools()). The actual probe (scripts.provider_probe.probe.main())
makes real network calls against real provider APIs and is NEVER invoked
here; see the probe module's docstring for how a human runs it manually.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.provider_probe import probe  # noqa: E402
from provider_types import ToolSpec  # noqa: E402 - backend is on sys.path via probe's own import


def test_build_probe_tools_returns_two_tool_specs():
    tools = probe.build_probe_tools()
    assert len(tools) == 2
    assert all(isinstance(t, ToolSpec) for t in tools)
    assert {t.name for t in tools} == {"web_search", "get_time"}


def test_wires_cover_all_four_providers():
    assert set(probe.WIRES) == {"openai_chat", "openai_responses", "anthropic", "gemini"}
