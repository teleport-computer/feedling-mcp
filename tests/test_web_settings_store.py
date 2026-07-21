"""Pure-unit coverage for the web-search toggle blob.

Monkeypatches db.get_blob/set_blob rather than hitting Postgres: what is under
test is the default / strict-bool / allowlist behaviour, not persistence. That
is what makes this file safe to list in conftest's ``_PURE_UNIT`` — a DB-backed
test in that set turns the graceful "no database" skip into a hard collection
error on machines without Postgres.

Strict booleans are the point of most of these cases. ``bool("no") is True``,
so a coercing implementation would turn ``{"enabled": "no"}`` into web access
switched ON — the user says no and gets yes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from core.store import WEB_SETTINGS_BLOB, UserStore  # noqa: E402


@pytest.fixture()
def store(monkeypatch):
    """A UserStore whose blob persistence is an in-memory dict.

    ``UserStore.__init__`` touches no database, so constructing one is free.
    """
    saved: dict[tuple[str, str], object] = {}
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: saved.get((uid, kind)))
    monkeypatch.setattr(
        db, "set_blob", lambda uid, kind, doc: saved.__setitem__((uid, kind), doc))
    s = UserStore("u-web-settings-test")
    s._saved_blobs = saved
    return s


def _stored(store, doc):
    store._saved_blobs[("u-web-settings-test", WEB_SETTINGS_BLOB)] = doc


def test_default_is_disabled(store):
    """No migration for existing users (product decision): missing blob = OFF."""
    assert store.load_web_settings() == {"version": 1, "enabled": False}


def test_roundtrip_and_blob_kind(store):
    assert store.save_web_settings({"enabled": True})["enabled"] is True
    assert store._saved_blobs[("u-web-settings-test", WEB_SETTINGS_BLOB)] == {
        "version": 1, "enabled": True}
    assert store.load_web_settings()["enabled"] is True


def test_can_be_turned_back_off(store):
    store.save_web_settings({"enabled": True})
    assert store.save_web_settings({"enabled": False})["enabled"] is False
    assert store.load_web_settings()["enabled"] is False


def test_unknown_keys_are_dropped(store):
    assert "evil" not in store.save_web_settings({"enabled": True, "evil": "x"})


@pytest.mark.parametrize("bad", ["yes", "no", "true", "false", "", 1, 0, None, []])
def test_non_bool_input_is_rejected(store, bad):
    """Strict booleans: never bool()-coerce. bool("no") is True — the user says
    no and would get web access switched on."""
    with pytest.raises(ValueError):
        store.save_web_settings({"enabled": bad})


def test_only_real_bools_accepted(store):
    assert store.save_web_settings({"enabled": True})["enabled"] is True
    assert store.save_web_settings({"enabled": False})["enabled"] is False


def test_rejected_write_does_not_touch_storage(store):
    store.save_web_settings({"enabled": True})
    with pytest.raises(ValueError):
        store.save_web_settings({"enabled": "no"})
    assert store.load_web_settings()["enabled"] is True


def test_corrupt_stored_value_reads_as_disabled(store):
    """A non-bool that somehow reached the blob fails closed on read."""
    _stored(store, {"enabled": "yes"})
    assert store.load_web_settings()["enabled"] is False


def test_load_rebuilds_the_contract_and_drops_unknown_fields(store):
    """Never ``{**default, **doc}`` — a historic blob's unknown fields would
    otherwise leak straight into the response contract."""
    _stored(store, {"enabled": True, "version": 99, "legacy_junk": "x"})
    assert store.load_web_settings() == {"version": 1, "enabled": True}


def test_empty_or_non_dict_patch_keeps_current(store):
    store.save_web_settings({"enabled": True})
    assert store.save_web_settings({})["enabled"] is True
    assert store.save_web_settings("true")["enabled"] is True


def test_corrupt_blob_falls_back_to_disabled(store):
    _stored(store, ["not", "a", "dict"])
    assert store.load_web_settings() == {"version": 1, "enabled": False}


def test_load_survives_storage_errors(store, monkeypatch):
    """A blob read failure must degrade to "off", never wedge the caller."""
    def boom(uid, kind):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_blob", boom)
    assert store.load_web_settings() == {"version": 1, "enabled": False}
