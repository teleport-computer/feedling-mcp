"""`web_search_halted` / `web_fetch_halted`: read/write plus fail-closed semantics.

DB-backed on purpose (it exercises the real control table), so this file is NOT
in conftest's ``_PURE_UNIT``.

Unlike ``turns_halted`` there is no fail-open entry point: web has no legitimate
fail-open caller, and keeping a ``default_on_error=False`` parameter would be a
bypass hatch on a safety gate. Fixed semantics:

    DB ok         -> the two real columns
    DB error      -> (True, True), cached ~2s
    row missing   -> (True, True), cached
    never raises  -> the chat continues, the model just answers without tools
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import kill_switch  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    kill_switch.set_web_halted(search=False, fetch=False)
    kill_switch._invalidate_all_for_tests()
    yield
    kill_switch.set_web_halted(search=False, fetch=False)
    kill_switch._invalidate_all_for_tests()


def test_defaults_to_not_halted():
    assert kill_switch.web_halted() == (False, False)


def test_search_and_fetch_are_independent():
    kill_switch.set_web_halted(search=True)
    assert kill_switch.web_halted() == (True, False)
    kill_switch.set_web_halted(search=False, fetch=True)
    assert kill_switch.web_halted() == (False, True)


def test_partial_set_leaves_the_other_column_alone():
    kill_switch.set_web_halted(search=True, fetch=True)
    kill_switch.set_web_halted(fetch=False)
    assert kill_switch.web_halted() == (True, False)


def test_set_invalidates_cache():
    """Must observe the new value without waiting out the TTL."""
    assert kill_switch.web_halted() == (False, False)
    kill_switch.set_web_halted(fetch=True)
    assert kill_switch.web_halted() == (False, True)


def test_turns_halted_is_untouched():
    """The web columns must not disturb the existing turn kill switch."""
    before = kill_switch.turns_halted()
    kill_switch.set_web_halted(search=True, fetch=True)
    assert kill_switch.turns_halted() == before


def test_read_error_fails_closed(monkeypatch):
    """A control-plane read failure must disable web, never enable it."""
    def boom():
        raise RuntimeError("control plane down")

    monkeypatch.setattr(kill_switch, "_fetch_web_halted_row", boom)
    kill_switch._invalidate_all_for_tests()
    assert kill_switch.web_halted() == (True, True)


def test_read_error_is_cached_so_a_flapping_db_is_not_hammered(monkeypatch):
    """Without caching the error, a DB wobble makes every turn and every tool
    batch re-query — the outage amplifies itself into a query storm."""
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("down")

    monkeypatch.setattr(kill_switch, "_fetch_web_halted_row", boom)
    kill_switch._invalidate_all_for_tests()
    assert kill_switch.web_halted() == (True, True)
    assert kill_switch.web_halted() == (True, True)
    assert kill_switch.web_halted() == (True, True)
    assert calls["n"] == 1


def test_missing_control_row_fails_closed(monkeypatch):
    """No control row = unknown state = no web. Must not fail open."""
    monkeypatch.setattr(kill_switch, "_fetch_web_halted_row", lambda: None)
    kill_switch._invalidate_all_for_tests()
    assert kill_switch.web_halted() == (True, True)


def test_web_halted_never_raises(monkeypatch):
    """Callers treat (True, True) as "no tools this batch" and carry on; an
    exception here would fail the whole turn."""
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(kill_switch, "_fetch_web_halted_row", boom)
    kill_switch._invalidate_all_for_tests()
    kill_switch.web_halted()  # must not raise


def test_no_fail_open_parameter():
    """Deliberately no ``default_on_error`` knob — see the module docstring."""
    import inspect

    assert list(inspect.signature(kill_switch.web_halted).parameters) == []
