"""Pure-unit coverage for the V1 web EXECUTION gate.

This is the security floor, so the load-bearing assertions are negative: when
the user's switch is off, or the operator halt is set, or the switch is
unreadable, the real network-touching ``capabilities.web`` call must NOT run.
We assert that by counting calls on a spy that stands in for ``web.search`` /
``web.fetch`` — a call count of zero is the safety property.

Everything is monkeypatched: no DB, no network. The store is a bare object with
just ``load_web_settings``; the kill switch and the web tools are replaced with
spies.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from capabilities import errors  # noqa: E402
from capabilities.types import CapabilityResult, ok  # noqa: E402
from web import execution_core  # noqa: E402


class _FakeStore:
    """Minimal stand-in. ``load_web_settings`` returns whatever we seed, or
    raises when ``raise_on_load`` is set (to exercise the fail-closed path)."""

    def __init__(self, *, enabled=None, raise_on_load=False):
        self._enabled = enabled
        self._raise = raise_on_load

    def load_web_settings(self):
        if self._raise:
            raise RuntimeError("blob store unavailable")
        return {} if self._enabled is None else {"enabled": self._enabled}


class _Spy:
    """Records calls and returns a fixed sentinel ok() result."""

    def __init__(self):
        self.calls = []
        self.result = ok(data={"sentinel": True})

    def __call__(self, store, *, params=None):
        self.calls.append((store, params))
        return self.result


@pytest.fixture()
def spies(monkeypatch):
    search = _Spy()
    fetch = _Spy()
    monkeypatch.setattr(execution_core.web, "search", search)
    monkeypatch.setattr(execution_core.web, "fetch", fetch)
    return search, fetch


def _set_halted(monkeypatch, value):
    monkeypatch.setattr(execution_core.kill_switch, "web_halted", lambda: value)


# --------------------------------------------------------------- search: OFF

def test_search_disabled_never_touches_network(monkeypatch, spies):
    search, _fetch = spies
    _set_halted(monkeypatch, (False, False))  # nothing halted — switch alone must block
    store = _FakeStore(enabled=False)

    result = execution_core.run_search(store, {"query": "hi"})

    assert isinstance(result, CapabilityResult)
    assert result.ok is False
    assert result.error["code"] == errors.DISABLED
    assert result.error["retryable"] is False
    assert search.calls == []                 # THE safety assertion: zero network calls


def test_search_missing_enabled_key_is_off(monkeypatch, spies):
    search, _fetch = spies
    _set_halted(monkeypatch, (False, False))
    store = _FakeStore(enabled=None)          # settings blob with no "enabled"

    result = execution_core.run_search(store, {"query": "hi"})

    assert result.ok is False
    assert result.error["code"] == errors.DISABLED
    assert search.calls == []


# ------------------------------------------------------------ search: HALTED

def test_search_halted_returns_degraded_and_skips_network(monkeypatch, spies):
    search, _fetch = spies
    _set_halted(monkeypatch, (True, False))   # search halted, fetch fine
    store = _FakeStore(enabled=True)

    result = execution_core.run_search(store, {"query": "hi"})

    assert result.ok is False
    assert result.error["code"] == errors.UNAVAILABLE
    assert result.error["retryable"] is True
    assert search.calls == []


# --------------------------------------------------------------- search: OK

def test_search_enabled_and_open_runs_and_returns_tool_result(monkeypatch, spies):
    search, _fetch = spies
    _set_halted(monkeypatch, (False, False))
    store = _FakeStore(enabled=True)

    result = execution_core.run_search(store, {"query": "hi"})

    assert result is search.result            # returns web.search's result verbatim
    assert result.ok is True
    assert len(search.calls) == 1
    passed_store, passed_params = search.calls[0]
    assert passed_store is store
    assert passed_params == {"query": "hi"}


# ---------------------------------------------------------------- fetch: OFF

def test_fetch_disabled_never_touches_network(monkeypatch, spies):
    _search, fetch = spies
    _set_halted(monkeypatch, (False, False))
    store = _FakeStore(enabled=False)

    result = execution_core.run_fetch(store, {"url": "https://example.com"})

    assert result.ok is False
    assert result.error["code"] == errors.DISABLED
    assert fetch.calls == []


# ------------------------------------------------------------- fetch: HALTED

def test_fetch_halted_returns_degraded_and_skips_network(monkeypatch, spies):
    _search, fetch = spies
    _set_halted(monkeypatch, (False, True))   # fetch halted, search fine
    store = _FakeStore(enabled=True)

    result = execution_core.run_fetch(store, {"url": "https://example.com"})

    assert result.ok is False
    assert result.error["code"] == errors.UNAVAILABLE
    assert result.error["retryable"] is True
    assert fetch.calls == []


# ---------------------------------------------------------------- fetch: OK

def test_fetch_enabled_and_open_runs_and_returns_tool_result(monkeypatch, spies):
    _search, fetch = spies
    _set_halted(monkeypatch, (False, False))
    store = _FakeStore(enabled=True)

    result = execution_core.run_fetch(store, {"url": "https://example.com"})

    assert result is fetch.result
    assert len(fetch.calls) == 1
    passed_store, passed_params = fetch.calls[0]
    assert passed_store is store
    assert passed_params == {"url": "https://example.com"}


# ----------------------------------------------------------- fail-closed read

def test_switch_read_exception_fails_closed_search(monkeypatch, spies):
    """load_web_settings blowing up must be read as OFF, not as permission."""
    search, _fetch = spies
    _set_halted(monkeypatch, (False, False))
    store = _FakeStore(raise_on_load=True)

    result = execution_core.run_search(store, {"query": "hi"})

    assert result.ok is False
    assert result.error["code"] == errors.DISABLED
    assert search.calls == []                 # never reached the network


def test_switch_read_exception_fails_closed_fetch(monkeypatch, spies):
    _search, fetch = spies
    _set_halted(monkeypatch, (False, False))
    store = _FakeStore(raise_on_load=True)

    result = execution_core.run_fetch(store, {"url": "https://example.com"})

    assert result.ok is False
    assert result.error["code"] == errors.DISABLED
    assert fetch.calls == []
