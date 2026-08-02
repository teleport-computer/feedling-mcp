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

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from capabilities import errors  # noqa: E402
from capabilities.types import CapabilityResult, ok  # noqa: E402
from web import execution_core  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_limiter():
    """The per-user web limiter is a module singleton whose hits would otherwise
    bleed across tests (monotonic time barely moves in a run). Reset before each
    test so rate-limit assertions are deterministic and unrelated tests never
    exhaust the budget for each other."""
    execution_core._web_limiter.reset()
    yield
    execution_core._web_limiter.reset()


class _FakeStore:
    """Minimal stand-in. ``load_web_settings`` returns whatever we seed, or
    raises when ``raise_on_load`` is set (to exercise the fail-closed path).
    ``user_id`` feeds the per-user rate-limit key."""

    def __init__(self, *, enabled=None, raise_on_load=False, user_id="u-test"):
        self._enabled = enabled
        self._raise = raise_on_load
        self.user_id = user_id

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


# ---------------------------------------------------------- rate limit (Part A)

def test_rate_limit_blocks_search_after_budget_and_skips_network(monkeypatch, spies):
    """Same user over the per-minute budget → refused, and the safety property:
    ``web.search`` is NOT called for the rejected request (zero network calls
    beyond the ones that were within budget)."""
    search, _fetch = spies
    _set_halted(monkeypatch, (False, False))
    monkeypatch.setenv(execution_core._WEB_RATE_ENV, "3/60")  # 3 calls / 60s
    store = _FakeStore(enabled=True)

    for _ in range(3):  # first 3 within budget
        assert execution_core.run_search(store, {"query": "hi"}).ok is True
    assert len(search.calls) == 3

    over = execution_core.run_search(store, {"query": "hi"})  # 4th over budget
    assert over.ok is False
    assert over.error["code"] == errors.RATE_LIMITED
    assert over.error["retryable"] is True
    assert len(search.calls) == 3  # THE safety assertion: no extra network call


def test_rate_limit_is_shared_across_search_and_fetch(monkeypatch, spies):
    """search + fetch draw on ONE per-user bucket — total outbound web calls are
    capped, so a fetch can be refused because searches already spent the budget."""
    search, fetch = spies
    _set_halted(monkeypatch, (False, False))
    monkeypatch.setenv(execution_core._WEB_RATE_ENV, "2/60")
    store = _FakeStore(enabled=True)

    assert execution_core.run_search(store, {"query": "a"}).ok is True
    assert execution_core.run_fetch(store, {"url": "https://example.com"}).ok is True
    third = execution_core.run_fetch(store, {"url": "https://example.com"})
    assert third.ok is False
    assert third.error["code"] == errors.RATE_LIMITED
    assert len(search.calls) == 1
    assert len(fetch.calls) == 1  # the refused fetch never reached the network


def test_rate_limit_is_per_user(monkeypatch, spies):
    """One user hitting the ceiling must not throttle a different user."""
    search, _fetch = spies
    _set_halted(monkeypatch, (False, False))
    monkeypatch.setenv(execution_core._WEB_RATE_ENV, "1/60")
    alice = _FakeStore(enabled=True, user_id="alice")
    bob = _FakeStore(enabled=True, user_id="bob")

    assert execution_core.run_search(alice, {"query": "a"}).ok is True
    assert execution_core.run_search(alice, {"query": "a"}).ok is False  # alice capped
    assert execution_core.run_search(bob, {"query": "b"}).ok is True     # bob unaffected
    assert len(search.calls) == 2


def test_rate_limit_runs_after_switch_and_halt_gates(monkeypatch, spies):
    """Budget is charged only after the auth gates pass: a disabled user is
    rejected by the switch and never consumes rate-limit budget."""
    search, _fetch = spies
    _set_halted(monkeypatch, (False, False))
    monkeypatch.setenv(execution_core._WEB_RATE_ENV, "1/60")
    disabled = _FakeStore(enabled=False)

    # Many disabled calls — all rejected by the switch (DISABLED), none charged.
    for _ in range(5):
        r = execution_core.run_search(disabled, {"query": "hi"})
        assert r.error["code"] == errors.DISABLED

    # The same user, now enabled, still has a full budget (nothing was spent).
    enabled = _FakeStore(enabled=True, user_id=disabled.user_id)
    assert execution_core.run_search(enabled, {"query": "hi"}).ok is True
    assert len(search.calls) == 1


# ------------------------------------------------- search char budget (Part B)

class _SearchSpy:
    """Returns a search-shaped ok() with a caller-supplied results list."""

    def __init__(self, results):
        self.result = ok(data={"query": "q", "results": results})

    def __call__(self, store, *, params=None):
        return self.result


def test_search_result_set_over_budget_is_truncated_and_valid(monkeypatch):
    _set_halted(monkeypatch, (False, False))
    monkeypatch.setenv(execution_core._SEARCH_CHAR_BUDGET_ENV, "300")
    # 20 items, each well over the shrunk budget when accumulated.
    results = [{"title": f"t{i}", "url": f"https://e/{i}", "snippet": "x" * 60}
               for i in range(20)]
    monkeypatch.setattr(execution_core.web, "search", _SearchSpy(results))
    store = _FakeStore(enabled=True)

    result = execution_core.run_search(store, {"query": "q"})

    assert result.ok is True
    assert result.data["truncated"] is True
    kept = result.data["results"]
    assert 0 < len(kept) < len(results)          # some dropped, not all
    # The payload is still valid JSON (whole items kept, never a raw string cut).
    round_trip = json.loads(json.dumps(result.data))
    assert round_trip["results"] == kept
    # truncated flag is the first key so a downstream hard-cut can't drop it.
    assert next(iter(result.data)) == "truncated"


def test_search_result_set_within_budget_not_truncated(monkeypatch):
    _set_halted(monkeypatch, (False, False))
    monkeypatch.setenv(execution_core._SEARCH_CHAR_BUDGET_ENV, "100000")
    results = [{"title": "t", "url": "https://e", "snippet": "short"}]
    monkeypatch.setattr(execution_core.web, "search", _SearchSpy(results))
    store = _FakeStore(enabled=True)

    result = execution_core.run_search(store, {"query": "q"})

    assert result.ok is True
    assert result.data["truncated"] is False
    assert result.data["results"] == results


def test_search_first_item_over_budget_still_returned(monkeypatch):
    """A single oversized item is kept (already per-field capped upstream) rather
    than returning an empty set that reads as 'the web found nothing'."""
    _set_halted(monkeypatch, (False, False))
    monkeypatch.setenv(execution_core._SEARCH_CHAR_BUDGET_ENV, "10")
    results = [{"title": "big", "snippet": "y" * 500}, {"title": "second"}]
    monkeypatch.setattr(execution_core.web, "search", _SearchSpy(results))
    store = _FakeStore(enabled=True)

    result = execution_core.run_search(store, {"query": "q"})

    assert result.data["results"] == [results[0]]  # first kept, rest dropped
    assert result.data["truncated"] is True


def test_search_error_result_passes_through_budget(monkeypatch):
    """An error result (no results list) is returned unchanged by the budgeter."""
    _set_halted(monkeypatch, (False, False))

    def _boom(store, *, params=None):
        from capabilities.types import err as _err
        return _err(errors.UPSTREAM, "boom", retryable=True)

    monkeypatch.setattr(execution_core.web, "search", _boom)
    store = _FakeStore(enabled=True)

    result = execution_core.run_search(store, {"query": "q"})
    assert result.ok is False
    assert result.error["code"] == errors.UPSTREAM
    assert "truncated" not in result.data
