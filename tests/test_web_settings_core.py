"""Pure-unit coverage for the web-settings API core.

`kill_switch.web_halted` is DB-backed, so every case injects a `halted_reader`;
that injection is what makes this file honestly pure and safe for `_PURE_UNIT`.

The invariant these tests exist to protect: `enabled` is the USER'S SAVED
PREFERENCE and nothing else. An operator halting web must not rewrite it, or
restoring the feature would silently leave every user switched off with no way
to know they have to go back and re-enable it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from chat import web_settings_core  # noqa: E402
from core.store import UserStore  # noqa: E402

OPEN = lambda: (False, False)  # noqa: E731 — nothing halted


@pytest.fixture()
def store(monkeypatch):
    saved: dict[tuple[str, str], object] = {}
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: saved.get((uid, kind)))
    monkeypatch.setattr(
        db, "set_blob", lambda uid, kind, doc: saved.__setitem__((uid, kind), doc))
    return UserStore("u-web-core-test")


def test_get_defaults_to_disabled_but_available(store):
    assert web_settings_core.get_settings(store, halted_reader=OPEN) == {
        "enabled": False,
        "available": True,
        "effective": False,
        "unavailable_reason": None,
        "capabilities": {"search": True, "fetch": True},
    }


def test_update_turns_on_and_effective_follows(store):
    got = web_settings_core.update_settings(
        store, {"enabled": True}, halted_reader=OPEN)
    assert got["enabled"] is True
    assert got["effective"] is True


def test_kill_switch_does_not_rewrite_the_user_preference(store):
    """The whole point of splitting enabled / available / effective."""
    web_settings_core.update_settings(store, {"enabled": True}, halted_reader=OPEN)
    got = web_settings_core.get_settings(store, halted_reader=lambda: (True, True))
    assert got["enabled"] is True          # preference untouched
    assert got["available"] is False
    assert got["effective"] is False
    assert got["unavailable_reason"] == "globally_disabled"
    # and the store itself still holds the user's choice
    assert store.load_web_settings()["enabled"] is True


def test_search_halted_alone_reports_unavailable(store):
    """The product entry is called "web search", so `available` tracks search.
    `not (search and fetch)` would claim availability while search is down."""
    got = web_settings_core.get_settings(store, halted_reader=lambda: (True, False))
    assert got["available"] is False
    assert got["capabilities"] == {"search": False, "fetch": True}


def test_fetch_halted_alone_keeps_search_available(store):
    got = web_settings_core.get_settings(store, halted_reader=lambda: (False, True))
    assert got["available"] is True
    assert got["capabilities"] == {"search": True, "fetch": False}


def test_update_requires_enabled(store):
    with pytest.raises(ValueError):
        web_settings_core.update_settings(store, {}, halted_reader=OPEN)


@pytest.mark.parametrize("bad", ["no", "true", "yes", 1, 0, None, []])
def test_update_rejects_non_bool(store, bad):
    """Strict booleans end to end: bool("no") is True would flip web ON for a
    client that meant to turn it off."""
    with pytest.raises(ValueError):
        web_settings_core.update_settings(store, {"enabled": bad}, halted_reader=OPEN)


def test_update_rejects_non_dict_payload(store):
    with pytest.raises(ValueError):
        web_settings_core.update_settings(store, "true", halted_reader=OPEN)


def test_rejected_update_leaves_the_stored_preference_alone(store):
    web_settings_core.update_settings(store, {"enabled": True}, halted_reader=OPEN)
    with pytest.raises(ValueError):
        web_settings_core.update_settings(store, {"enabled": "no"}, halted_reader=OPEN)
    assert store.load_web_settings()["enabled"] is True
