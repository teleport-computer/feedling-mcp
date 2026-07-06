"""Cross-worker invalidation of cached blob-backed store state (Codex P2).

tokens / push_state / live_activity_state / frames_meta are cached in-memory on
the UserStore, so a write on one worker must broadcast a wake or another worker
serves stale state until the 15-min TTL. The catch: the loaders re-persist
normalized state on read, so a reload (itself often triggered by a wake) must
NOT re-broadcast — that's the _reload_guard. These tests pin both halves.

Run:  python -m pytest tests/test_blob_wake.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from core import store as core_store  # noqa: E402
from core import wake_bus  # noqa: E402


@pytest.fixture()
def store(monkeypatch):
    core_store._stores.clear()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(wake_bus, "notify", lambda ch, uid="": calls.append((ch, uid)))
    st = core_store.get_store("usr_blob_wake_test")
    st._calls = calls
    return st


def _channels(store):
    return [c for c, _ in store._calls]


def test_genuine_token_save_broadcasts_blob(store):
    store._calls.clear()
    store._save_tokens()
    assert "blob" in _channels(store)


def test_record_push_broadcasts_blob(store):
    store._calls.clear()
    store.record_successful_push()
    assert "blob" in _channels(store)


def test_live_activity_save_broadcasts_blob(store):
    store._calls.clear()
    store._save_live_activity_state()
    assert "blob" in _channels(store)


def test_frames_meta_persist_broadcasts_frames(store):
    store._calls.clear()
    store._persist_frames_meta()
    assert "frames" in _channels(store)


def test_reload_does_not_broadcast(store):
    # reload() runs the loaders' write-on-read normalization; the guard must
    # suppress every blob/frames wake so workers don't NOTIFY-storm each other.
    store._calls.clear()
    store.reload()
    assert _channels(store) == []
