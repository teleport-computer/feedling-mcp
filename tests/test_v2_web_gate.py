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
    assert g.disabled_web_tools(user_enabled=False,
                                search_halted=False, fetch_halted=False) == BOTH


def test_user_on_enables_everything():
    assert g.disabled_web_tools(user_enabled=True,
                                search_halted=False, fetch_halted=False) == frozenset()


def test_the_gate_takes_no_lane_at_all():
    """The regression this guards against is re-introducing a lane policy.

    The proactive companion had web access before this feature existed, so a
    background-lane carve-out would silently take a capability away under the
    banner of adding a setting. One switch, every lane — and the signature is
    where that is enforced, because a caller cannot pass a lane it does not take.
    """
    import inspect

    assert "lane" not in inspect.signature(g.disabled_web_tools).parameters
    assert not hasattr(g, "FOREGROUND_LANES")


def test_halted_flags_are_independent():
    assert g.disabled_web_tools(user_enabled=True, search_halted=True,
                                fetch_halted=False) == frozenset({"web_search"})
    assert g.disabled_web_tools(user_enabled=True, search_halted=False,
                                fetch_halted=True) == frozenset({"web_fetch"})


def test_halted_wins_over_user_preference():
    assert g.disabled_web_tools(user_enabled=True,
                                search_halted=True, fetch_halted=True) == BOTH


def test_returns_a_frozenset_so_callers_cannot_mutate_shared_state():
    got = g.disabled_web_tools(user_enabled=True,
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


# ------------------------------------------------- halted_web_tools (live half)

def test_halted_web_tools_maps_flags_to_names():
    assert g.halted_web_tools(search_halted=False, fetch_halted=False) == frozenset()
    assert g.halted_web_tools(search_halted=True, fetch_halted=False) == frozenset({"web_search"})
    assert g.halted_web_tools(search_halted=False, fetch_halted=True) == frozenset({"web_fetch"})
    assert g.halted_web_tools(search_halted=True, fetch_halted=True) == BOTH


def test_halted_web_tools_does_not_consider_lane_or_user():
    """The live half must not re-derive the policy the turn-entry snapshot
    already encodes — that is how two halves of a gate drift apart."""
    import inspect

    params = set(inspect.signature(g.halted_web_tools).parameters)
    assert params == {"search_halted", "fetch_halted"}
