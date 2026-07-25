"""D4 live kill switch: `v2_runtime_control` single-row table + cached read
(spec PR D Task 1)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import kill_switch

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 kill switch tests require the PostgreSQL test fixture",
)


@pytest.fixture(autouse=True)
def _reset_control_row():
    """Every test starts from the migration-seeded `id=1, turns_halted=false`
    row and the switch's own cache is invalidated on both sides, so no test's
    cached value or DB state leaks into the next."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_control SET turns_halted=false, updated_at=now() WHERE id=1"
        )
    kill_switch._invalidate()
    yield
    kill_switch._invalidate()


def test_turns_halted_defaults_false():
    assert kill_switch.turns_halted() is False


def test_set_turns_halted_true_then_read_true():
    kill_switch.set_turns_halted(True)
    assert kill_switch.turns_halted() is True


def test_set_turns_halted_false_resumes():
    kill_switch.set_turns_halted(True)
    assert kill_switch.turns_halted() is True
    kill_switch.set_turns_halted(False)
    assert kill_switch.turns_halted() is False


def test_uncached_read_bypasses_a_stale_false_observation():
    assert kill_switch.turns_halted() is False
    # Simulate another backend/runner process changing the shared control row;
    # this process's ordinary polling cache must remain stale for the contrast.
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_control SET turns_halted=true, updated_at=now() WHERE id=1"
        )
    assert kill_switch.turns_halted() is False
    assert kill_switch.turns_halted_uncached(default_on_error=True) is True


def test_default_on_error_true_on_db_failure(monkeypatch):
    def _boom():
        raise RuntimeError("control-plane DB unavailable")

    monkeypatch.setattr(db, "get_pool", _boom)
    assert kill_switch.turns_halted(default_on_error=True) is True


def test_default_on_error_false_on_db_failure(monkeypatch):
    def _boom():
        raise RuntimeError("control-plane DB unavailable")

    monkeypatch.setattr(db, "get_pool", _boom)
    assert kill_switch.turns_halted(default_on_error=False) is False


# ------------------------------------------------------- provider usage halt


@pytest.fixture(autouse=True)
def _reset_provider_usage_row():
    """Mirrors `_reset_control_row` above but for `provider_usage_halted`, so
    no test's cached value or DB state leaks into the next."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_control SET provider_usage_halted=false, updated_at=now() "
            "WHERE id=1"
        )
    kill_switch._invalidate_provider_usage()
    yield
    kill_switch._invalidate_provider_usage()


def test_provider_usage_halted_defaults_false():
    assert kill_switch.provider_usage_halted() is False


def test_provider_usage_halted_reads_row(monkeypatch):
    kill_switch._invalidate_provider_usage()
    monkeypatch.setattr(kill_switch, "_fetch_provider_usage_halted_row", lambda: (False,))
    assert kill_switch.provider_usage_halted() is False


def test_set_provider_usage_halted_true_then_read_true():
    kill_switch.set_provider_usage_halted(True)
    assert kill_switch.provider_usage_halted() is True


def test_set_provider_usage_halted_false_resumes():
    kill_switch.set_provider_usage_halted(True)
    assert kill_switch.provider_usage_halted() is True
    kill_switch.set_provider_usage_halted(False)
    assert kill_switch.provider_usage_halted() is False


def test_provider_usage_halted_fail_closed_on_db_error(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(kill_switch, "_fetch_provider_usage_halted_row", boom)
    kill_switch._invalidate_provider_usage()
    assert kill_switch.provider_usage_halted() is True


def test_provider_usage_halted_fail_closed_on_missing_row(monkeypatch):
    monkeypatch.setattr(kill_switch, "_fetch_provider_usage_halted_row", lambda: None)
    kill_switch._invalidate_provider_usage()
    assert kill_switch.provider_usage_halted() is True


def test_provider_usage_halted_read_error_is_cached(monkeypatch):
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("down")

    monkeypatch.setattr(kill_switch, "_fetch_provider_usage_halted_row", boom)
    kill_switch._invalidate_provider_usage()
    assert kill_switch.provider_usage_halted() is True
    assert kill_switch.provider_usage_halted() is True
    assert calls["n"] == 1


def test_provider_usage_halted_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(kill_switch, "_fetch_provider_usage_halted_row", boom)
    kill_switch._invalidate_provider_usage()
    kill_switch.provider_usage_halted()  # must not raise


def test_provider_usage_cache_does_not_disturb_turns_halted():
    before = kill_switch.turns_halted()
    kill_switch.set_provider_usage_halted(True)
    assert kill_switch.turns_halted() == before
