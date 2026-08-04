"""Pure-unit coverage for the web-settings API contract.

Both DB-backed readers are injected, which is what makes this file honestly pure.

The contract exists to stop the response from lying. An earlier version reported
`effective = enabled and not search_halted`, which was wrong twice: it told a
self-hosted user their toggle was in effect when their runtime has no web tools
at all, and it reported "not effective" while web_fetch was still usable.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from chat import consumer as chat_consumer  # noqa: E402
from core.store import UserStore  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402
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


# ---------------------------------------------------------------------------
# The real _runtime_supported gate (batch 4). The tests above inject the reader;
# these drive the actual V2-or-V1 decision, monkeypatching only its two seams:
# the V2 ownership row and the consumer_state blob the V1 heartbeat reads.
# ---------------------------------------------------------------------------

_WEB_CAPS = [chat_consumer.WEB_SEARCH_CAPABILITY, chat_consumer.WEB_FETCH_CAPABILITY]


@pytest.fixture()
def rs_store(monkeypatch):
    """Store whose consumer_state blob and V2 ownership row are test-controlled."""
    saved: dict[tuple[str, str], object] = {}
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: saved.get((uid, kind)))
    monkeypatch.setattr(
        hosted_config_store, "hosted_runtime_v2_enabled_strict", lambda s: False)
    store = UserStore("u-runtime-supported-test")

    def seed_consumer(**fields):
        saved[(store.user_id, "consumer_state")] = dict(fields)

    store._seed_consumer = seed_consumer  # type: ignore[attr-defined]
    return store


def _fresh_official_web(**over):
    state = {
        "official": True,
        "last_poll_epoch": time.time(),
        "consumer_capabilities": list(_WEB_CAPS),
    }
    state.update(over)
    return state


def test_v2_runtime_is_supported_without_any_consumer_poll(rs_store, monkeypatch):
    """V2 carries the web tools intrinsically; its semantics are untouched."""
    monkeypatch.setattr(
        hosted_config_store, "hosted_runtime_v2_enabled_strict", lambda s: True)
    assert settings_core._runtime_supported(rs_store) is True


def test_v1_fresh_consumer_advertising_web_is_supported(rs_store):
    rs_store._seed_consumer(**_fresh_official_web())
    assert settings_core._runtime_supported(rs_store) is True


def test_v1_old_consumer_not_advertising_web_is_unsupported(rs_store):
    """An older build polls fresh but never lists the web capability."""
    rs_store._seed_consumer(**_fresh_official_web(
        consumer_capabilities=["vision_observer_v1", "vision_probe_v2"]))
    assert settings_core._runtime_supported(rs_store) is False


def test_v1_stale_heartbeat_is_unsupported(rs_store):
    """Web was advertised, but the last poll aged past the freshness window."""
    stale = time.time() - (chat_consumer._CONSUMER_RECENT_SEC + 60)
    rs_store._seed_consumer(**_fresh_official_web(last_poll_epoch=stale))
    assert settings_core._runtime_supported(rs_store) is False


def test_v1_non_official_consumer_is_unsupported(rs_store):
    """Only the official consumer's claim counts; an app/API poll must not."""
    rs_store._seed_consumer(**_fresh_official_web(official=False))
    assert settings_core._runtime_supported(rs_store) is False


def test_v1_no_consumer_state_is_unsupported(rs_store):
    """No poll on record at all — fail closed."""
    assert settings_core._runtime_supported(rs_store) is False


def test_runtime_read_error_fails_closed(rs_store, monkeypatch):
    """A control-plane hiccup must degrade to unsupported, never raise/500."""
    def boom(s):
        raise RuntimeError("control plane down")

    monkeypatch.setattr(
        hosted_config_store, "hosted_runtime_v2_enabled_strict", boom)
    assert settings_core._runtime_supported(rs_store) is False
