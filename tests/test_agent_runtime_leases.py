"""DB-backed tests for backend/agent_runtime/leases.py.

Requires the throwaway test Postgres (conftest provisions DATABASE_URL + runs
migrations, including 0004_agent_runtime_instances). NOT a pure-unit test.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from agent_runtime import leases

T0 = 1_000_000.0  # fixed epoch base so TTL math is deterministic


@pytest.fixture(autouse=True)
def _clean_table():
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE agent_runtime_instances")
    yield


def test_acquire_on_empty_row_succeeds():
    ok = leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                        lease_owner="sup_A", ttl=300.0, now=T0)
    assert ok is True
    row = leases.get("u_1")
    assert row["lease_owner"] == "sup_A"
    assert row["driver"] == "claude"
    assert row["runtime_home"] == "/d/u_1"
    assert row["status"] == "starting"


def test_second_owner_cannot_acquire_live_lease():
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    # B tries 100s later — A's lease (expires T0+300) is still live.
    ok = leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                        lease_owner="sup_B", ttl=300.0, now=T0 + 100)
    assert ok is False
    assert leases.get("u_1")["lease_owner"] == "sup_A"


def test_owner_can_reacquire_idempotently():
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    ok = leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                        lease_owner="sup_A", ttl=300.0, now=T0 + 10)
    assert ok is True
    assert leases.get("u_1")["lease_owner"] == "sup_A"


def test_expired_lease_can_be_taken_over():
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    # B tries after A's lease expired (T0+300).
    ok = leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                        lease_owner="sup_B", ttl=300.0, now=T0 + 400)
    assert ok is True
    assert leases.get("u_1")["lease_owner"] == "sup_B"


def test_renew_extends_only_for_current_owner():
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    assert leases.renew("u_1", "sup_A", ttl=300.0, now=T0 + 100,
                        status="running", pid=4242) is True
    row = leases.get("u_1")
    assert row["status"] == "running"
    assert row["pid"] == 4242
    # A non-owner cannot renew.
    assert leases.renew("u_1", "sup_B", ttl=300.0, now=T0 + 110) is False


def test_renew_fails_after_expiry():
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    # Owner tries to renew after its own lease expired — must lose it.
    assert leases.renew("u_1", "sup_A", ttl=300.0, now=T0 + 400) is False


def test_set_session_ref_persists_for_owner():
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    assert leases.set_session_ref("u_1", "sup_A", "sess_xyz", now=T0 + 5) is True
    assert leases.get("u_1")["session_ref"] == "sess_xyz"


def test_release_clears_lease_for_owner():
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    leases.release("u_1", "sup_A", now=T0 + 5)
    row = leases.get("u_1")
    assert row["lease_owner"] is None
    assert row["status"] == "idle"
    # After release, another supervisor can immediately acquire.
    assert leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                          lease_owner="sup_B", ttl=300.0, now=T0 + 6) is True


def test_list_active_returns_only_live_leases():
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    leases.acquire("u_2", driver="claude", runtime_home="/d/u_2",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    # u_2's lease expires before the query time; u_1's is still live.
    active = leases.list_active(now=T0 + 350, lease_owner="sup_A")
    ids = {r["user_id"] for r in active}
    assert ids == set()  # both expired at T0+350 (ttl 300)
    active2 = leases.list_active(now=T0 + 100, lease_owner="sup_A")
    assert {r["user_id"] for r in active2} == {"u_1", "u_2"}
