"""Pure-unit coverage for the web gate decision.

Seven call sites share this module (three lanes on the offer side, three
dispatchers on the execute side, plus the subagent allowlist). Writing the rule
seven times is exactly how the two halves of a gate drift apart, so every one of
them goes through here — and every rule is pinned here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import web_gate as g  # noqa: E402

BOTH = frozenset({"web_search", "web_fetch"})


def test_tool_names_match_the_capability_registry():
    """If a capability is renamed, this gate must not silently stop covering it."""
    from capabilities import registry

    assert g.WEB_TOOL_NAMES <= set(registry.CAPABILITIES)


def test_user_off_disables_everything():
    assert g.disabled_web_tools(user_enabled=False, lane="chat",
                                search_halted=False, fetch_halted=False) == BOTH


def test_user_on_chat_enables_everything():
    assert g.disabled_web_tools(user_enabled=True, lane="chat",
                                search_halted=False, fetch_halted=False) == frozenset()


@pytest.mark.parametrize("lane", [
    "wake", "screen_watch", "scheduled", "heartbeat", "manual_wake",
    "maintenance", "capture", "dream", "", None, "future_lane", "Chat", "CHAT",
])
def test_background_and_unknown_lanes_always_disabled(lane):
    """Background search adds model rounds, tokens and latency, and is an
    outbound data flow the user never triggered. Anything not explicitly
    foreground — including a typo'd "Chat" — fails closed."""
    assert g.disabled_web_tools(user_enabled=True, lane=lane,
                                search_halted=False, fetch_halted=False) == BOTH


def test_only_chat_is_foreground():
    """Pins the policy literal to one place. If a new foreground lane is added
    this test is the reminder to think about it explicitly."""
    assert g.FOREGROUND_LANES == frozenset({"chat"})


def test_halted_flags_are_independent():
    assert g.disabled_web_tools(user_enabled=True, lane="chat", search_halted=True,
                                fetch_halted=False) == frozenset({"web_search"})
    assert g.disabled_web_tools(user_enabled=True, lane="chat", search_halted=False,
                                fetch_halted=True) == frozenset({"web_fetch"})


def test_halted_wins_over_user_preference():
    assert g.disabled_web_tools(user_enabled=True, lane="chat",
                                search_halted=True, fetch_halted=True) == BOTH


def test_returns_a_frozenset_so_callers_cannot_mutate_shared_state():
    got = g.disabled_web_tools(user_enabled=True, lane="chat",
                               search_halted=False, fetch_halted=False)
    assert isinstance(got, frozenset)


# ---------------------------------------------- resolve_user_enabled: fail closed

def test_resolve_none_callable_is_disabled():
    """TurnDeps.web_tools_enabled defaults to None (worker.py never imports
    hosted); that must read as OFF, not as "unknown, allow"."""
    assert g.resolve_user_enabled(None, "u1") is False


def test_resolve_true():
    assert g.resolve_user_enabled(lambda uid: True, "u1") is True


def test_resolve_false():
    assert g.resolve_user_enabled(lambda uid: False, "u1") is False


def test_resolve_passes_the_user_id_through():
    seen = []
    g.resolve_user_enabled(lambda uid: seen.append(uid) or True, "u-42")
    assert seen == ["u-42"]


def test_resolve_raising_callable_is_disabled_and_does_not_propagate():
    """A settings read failure must never fail the turn."""
    def boom(uid):
        raise RuntimeError("store down")

    assert g.resolve_user_enabled(boom, "u1") is False


@pytest.mark.parametrize("bad", ["yes", "true", "no", 1, 0, [], {}, object(), None])
def test_resolve_non_bool_is_disabled(bad):
    """`value is True`, never bool(value): bool("no") is True would turn a
    corrupt/mis-typed setting into web access switched ON."""
    assert g.resolve_user_enabled(lambda uid: bad, "u1") is False
