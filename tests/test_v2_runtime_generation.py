"""Runtime generation + cutover state machine (Hosted Runtime V2 PR A / spec A2)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import cutover

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 runtime-generation tests require the PostgreSQL test fixture",
)


@pytest.fixture(autouse=True)
def _clean_runtime_state_table():
    """Truncate v2_runtime_state before each test so generation/state assertions
    only ever see the row(s) this test seeds itself."""
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE TABLE v2_runtime_state")
    yield


def _fresh(uid):
    seed_user(uid)


def test_generation_starts_at_one_after_init():
    _fresh("u_gen1")
    # first read initializes the row lazily at generation 1, state resident
    assert db.get_runtime_generation("u_gen1") == 1


def test_valid_cutover_bumps_generation_monotonically():
    _fresh("u_gen2")
    assert db.get_runtime_generation("u_gen2") == 1
    g = db.advance_runtime_state("u_gen2", from_state="resident", to_state="draining")
    assert g == 2
    g = db.advance_runtime_state("u_gen2", from_state="draining", to_state="v2")
    assert g == 3
    assert db.get_runtime_generation("u_gen2") == 3


def test_lost_race_returns_none_no_bump():
    _fresh("u_gen3")
    db.get_runtime_generation("u_gen3")  # init at resident/1
    # from_state mismatch (already resident, ask draining->v2) => None, no bump
    assert db.advance_runtime_state("u_gen3", from_state="draining", to_state="v2") is None
    assert db.get_runtime_generation("u_gen3") == 1


def test_pure_transition_table():
    assert cutover.is_valid_transition("resident", "draining")
    assert cutover.is_valid_transition("draining", "v2")
    assert cutover.is_valid_transition("v2", "draining")
    assert cutover.is_valid_transition("draining", "resident")
    assert not cutover.is_valid_transition("resident", "v2")   # must pass through draining
    assert not cutover.is_valid_transition("v2", "resident")   # must pass through draining
