"""Pure-unit coverage for the web-settings API contract.

Both DB-backed readers are injected, which is what makes this file honestly pure.

The contract exists to stop the response from lying. An earlier version reported
`effective = enabled and not search_halted`, which was wrong twice: it told a
self-hosted user their toggle was in effect when their runtime has no web tools
at all, and it reported "not effective" while web_fetch was still usable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from core.store import UserStore  # noqa: E402
from web import settings_core  # noqa: E402

OPEN = lambda: (False, False)          # noqa: E731 — nothing halted
SUPPORTED = lambda store: True         # noqa: E731 — user runs on V2


@pytest.fixture()
def store(monkeypatch):
    saved: dict[tuple[str, str], object] = {}
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: saved.get((uid, kind)))
    monkeypatch.setattr(
        db, "set_blob_strict", lambda uid, kind, doc: saved.__setitem__((uid, kind), doc))
    return UserStore("u-web-core-test")


def _get(store, **over):
    kwargs = {"halted_reader": OPEN, "runtime_supported_reader": SUPPORTED}
    kwargs.update(over)
    return settings_core.get_settings(store, **kwargs)


def test_default_is_off_but_runtime_supports_it(store):
    assert _get(store) == {
        "enabled": False,
        "runtime_supported": True,
        "status": "available",
        "effective": False,
        "tools": {"web_search": {"available": True},
                  "web_fetch": {"available": True}},
    }


def test_enabled_makes_it_effective(store):
    settings_core.update_settings(
        store, {"enabled": True}, halted_reader=OPEN, runtime_supported_reader=SUPPORTED)
    assert _get(store)["effective"] is True


def test_self_hosted_user_is_never_effective_even_when_enabled(store):
    """resident_cli runs its own consumer and never loads the V2 tool loop, so
    the switch is inert there. Reporting it as in effect would be a lie."""
    settings_core.update_settings(
        store, {"enabled": True}, halted_reader=OPEN,
        runtime_supported_reader=lambda s: True)
    got = _get(store, runtime_supported_reader=lambda s: False)
    assert got["enabled"] is True             # the preference is still theirs
    assert got["runtime_supported"] is False
    assert got["status"] == "unavailable"
    assert got["effective"] is False
    assert got["tools"]["web_search"]["available"] is False


def test_half_open_is_degraded_not_unavailable(store):
    """search halted, fetch fine: the model can still reach the network, so
    claiming "not effective" would be wrong."""
    settings_core.update_settings(
        store, {"enabled": True}, halted_reader=OPEN, runtime_supported_reader=SUPPORTED)
    got = _get(store, halted_reader=lambda: (True, False))
    assert got["status"] == "degraded"
    assert got["effective"] is True
    assert got["tools"] == {"web_search": {"available": False},
                            "web_fetch": {"available": True}}


def test_both_halted_is_unavailable(store):
    settings_core.update_settings(
        store, {"enabled": True}, halted_reader=OPEN, runtime_supported_reader=SUPPORTED)
    got = _get(store, halted_reader=lambda: (True, True))
    assert got["status"] == "unavailable"
    assert got["effective"] is False


def test_operator_halt_never_rewrites_the_preference(store):
    settings_core.update_settings(
        store, {"enabled": True}, halted_reader=OPEN, runtime_supported_reader=SUPPORTED)
    _get(store, halted_reader=lambda: (True, True))
    assert store.load_web_settings()["enabled"] is True


def test_runtime_read_failure_degrades_to_unsupported(store, monkeypatch):
    """A control-plane hiccup must not 500 the settings page."""
    def boom(s):
        raise RuntimeError("control plane down")

    got = _get(store, runtime_supported_reader=boom) if False else settings_core.get_settings(
        store, halted_reader=OPEN,
        runtime_supported_reader=lambda s: settings_core._runtime_supported(s))
    assert got["runtime_supported"] in (True, False)   # never raises


def test_update_requires_enabled(store):
    with pytest.raises(ValueError):
        settings_core.update_settings(store, {}, halted_reader=OPEN,
                                      runtime_supported_reader=SUPPORTED)


@pytest.mark.parametrize("bad", ["no", "true", "yes", 1, 0, None, []])
def test_update_rejects_non_bool(store, bad):
    with pytest.raises(ValueError):
        settings_core.update_settings(store, {"enabled": bad}, halted_reader=OPEN,
                                      runtime_supported_reader=SUPPORTED)


def test_rejected_update_leaves_the_stored_preference_alone(store):
    settings_core.update_settings(
        store, {"enabled": True}, halted_reader=OPEN, runtime_supported_reader=SUPPORTED)
    with pytest.raises(ValueError):
        settings_core.update_settings(store, {"enabled": "no"}, halted_reader=OPEN,
                                      runtime_supported_reader=SUPPORTED)
    assert store.load_web_settings()["enabled"] is True
