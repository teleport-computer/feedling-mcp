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
        # The 0009 FK requires the user to exist in `users`. These tests model
        # the real path where acquire's user is a registered account, so seed a
        # users row for the ids they use. ``u_ghost`` is deliberately NOT seeded
        # — it stands in for an account deleted out from under the supervisor,
        # exercising the FK-guarded race path.
        for uid in ("u_1", "u_2", "u_3"):
            conn.execute(
                "INSERT INTO users (user_id, created_at, doc) "
                "VALUES (%s, '', '{}'::jsonb) ON CONFLICT (user_id) DO NOTHING",
                (uid,),
            )
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


def test_renew_reclaims_own_expired_uncontested_lease():
    # Reclaim semantics (the churn fix): if our OWN lease lapsed (a slow lap
    # outran the TTL) but NO other supervisor took it, the owner may renew and
    # reclaim it — the consumer is healthy, killing it just to re-spawn is the
    # death-spiral bug. Only a takeover by a LIVE other owner counts as a loss.
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    assert leases.renew("u_1", "sup_A", ttl=300.0, now=T0 + 400) is True
    row = leases.get("u_1")
    assert row["lease_owner"] == "sup_A"
    assert row["lease_expires_at"] is not None


def test_renew_does_not_steal_live_lease_from_other_owner():
    # The flip side of reclaim: if another supervisor took over the expired lease
    # (and its lease is still LIVE), the original owner must NOT steal it back —
    # that would double-run two consumers. renew returns False → caller reaps.
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    # B takes over after A's lease expired.
    assert leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                          lease_owner="sup_B", ttl=300.0, now=T0 + 400) is True
    # A tries to renew while B holds a live lease — must fail.
    assert leases.renew("u_1", "sup_A", ttl=300.0, now=T0 + 410) is False
    assert leases.get("u_1")["lease_owner"] == "sup_B"


def test_renew_does_not_reclaim_other_owners_expired_lease():
    # Codex P1: reclaim is limited to OUR OWN lease (or a truly released/unowned
    # one) — never another owner's, even after that owner's lease also briefly
    # lapsed. A stale supervisor renewing here would steal the row and double-run.
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    # B takes over after A's lease expired (T0+300).
    assert leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                          lease_owner="sup_B", ttl=300.0, now=T0 + 400) is True
    # Later, B's lease (expires T0+700) has also lapsed. Stale A must NOT reclaim.
    assert leases.renew("u_1", "sup_A", ttl=300.0, now=T0 + 800) is False
    assert leases.get("u_1")["lease_owner"] == "sup_B"


def test_renew_does_not_reclaim_released_lease():
    # Codex P2: a released lease (lease_owner NULL) must NOT be reanimated by an
    # in-flight renew. Otherwise a child the tick just killed + released (user
    # left the roster) gets a live DB lease again with its dead PID, so a
    # removed/disabled user still looks hosted until TTL. Re-taking an unowned
    # row is acquire's job (a fresh spawn), never renew's.
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    leases.release("u_1", "sup_A", now=T0 + 5)
    assert leases.get("u_1")["lease_owner"] is None
    assert leases.renew("u_1", "sup_A", ttl=300.0, now=T0 + 10) is False
    assert leases.get("u_1")["lease_owner"] is None


def test_renew_many_renews_all_owned_in_one_call():
    for u in ("u_1", "u_2", "u_3"):
        leases.acquire(u, driver="claude", runtime_home=f"/d/{u}",
                       lease_owner="sup_A", ttl=300.0, now=T0)
    held = leases.renew_many([("u_1", 11), ("u_2", 22), ("u_3", 33)],
                             "sup_A", ttl=300.0, now=T0 + 100)
    assert held == {"u_1", "u_2", "u_3"}
    for u, expected_pid in (("u_1", 11), ("u_2", 22), ("u_3", 33)):
        row = leases.get(u)
        assert row["pid"] == expected_pid
        assert row["status"] == "running"
        # lease extended past the original expiry (T0+300)
        assert row["lease_expires_at"].timestamp() > T0 + 300


def test_renew_many_reclaims_own_expired():
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    held = leases.renew_many([("u_1", 11)], "sup_A", ttl=300.0, now=T0 + 400)
    assert held == {"u_1"}
    assert leases.get("u_1")["lease_owner"] == "sup_A"


def test_renew_many_excludes_lease_lost_to_other_owner():
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    leases.acquire("u_2", driver="claude", runtime_home="/d/u_2",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    # u_2 taken over by a live sup_B; u_1 still ours.
    leases.acquire("u_2", driver="claude", runtime_home="/d/u_2",
                   lease_owner="sup_B", ttl=300.0, now=T0 + 400)
    held = leases.renew_many([("u_1", 11), ("u_2", 22)], "sup_A",
                             ttl=300.0, now=T0 + 410)
    assert held == {"u_1"}                       # u_2 lost — not renewed
    assert leases.get("u_2")["lease_owner"] == "sup_B"


def test_renew_many_does_not_reclaim_other_owners_expired_lease():
    # Batch form of the Codex P1 guard: a stale owner's batch renew must skip a
    # row another owner took over, even once that owner's lease also lapsed.
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_B", ttl=300.0, now=T0 + 400)   # B takes over
    held = leases.renew_many([("u_1", 11)], "sup_A", ttl=300.0, now=T0 + 800)
    assert held == set()
    assert leases.get("u_1")["lease_owner"] == "sup_B"


def test_renew_many_does_not_reclaim_released_lease():
    # Batch form of the Codex P2 guard: a row released between the renew snapshot
    # and the batched UPDATE must not be reanimated with the killed PID.
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    leases.release("u_1", "sup_A", now=T0 + 5)
    held = leases.renew_many([("u_1", 11)], "sup_A", ttl=300.0, now=T0 + 10)
    assert held == set()
    assert leases.get("u_1")["lease_owner"] is None


def test_renew_many_empty_is_noop():
    assert leases.renew_many([], "sup_A", ttl=300.0, now=T0) == set()


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


def test_acquire_skips_user_absent_from_users_table():
    """删号 race 守卫：对一个 users 表里不存在的 user_id（账号已被 reset 删掉），
    acquire 必须拒绝并且 **不创建** instance 行。否则一个用「删号前快照的 roster」
    跑到一半的 supervisor tick 会 INSERT ON CONFLICT 重建该行、spawn 一个已删账号
    的 consumer，进程因账号不存在立刻死，留下 idle 孤儿（prod 上累积的那批僵尸行）。
    FK agent_runtime_instances.user_id → users(user_id) 让这次 INSERT 被拒。"""
    ok = leases.acquire("u_ghost", driver="claude", runtime_home="/d/ghost",
                        lease_owner="sup_A", ttl=300.0, now=T0)
    assert ok is False
    assert leases.get("u_ghost") is None


def test_deleting_users_row_cascades_instance_away():
    """FK ON DELETE CASCADE：删掉 users 行(账号 reset 的 delete_user 步骤)必须
    连带删掉它的 instance 行。加 FK 之前删 users 不级联 —— 正是 prod 上 delete_user
    清了账号、instance 行却残留成 idle 孤儿的另一半成因。"""
    leases.acquire("u_1", driver="claude", runtime_home="/d/u_1",
                   lease_owner="sup_A", ttl=300.0, now=T0)
    assert leases.get("u_1") is not None
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id = %s", ("u_1",))
    assert leases.get("u_1") is None
