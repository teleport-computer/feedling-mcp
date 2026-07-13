"""DB-backed tests for the multi-user supervisor's tick orchestration.

Uses the real lease table with injected spawn/alive functions, so it exercises
acquire → spawn → heartbeat → reap and the cross-supervisor isolation that P1's
acceptance ("two users concurrent, leases/home not shared") rests on.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from agent_runtime import leases
from agent_runtime import supervisor as supervisor_mod
from agent_runtime.supervisor import Supervisor, parse_roster

T0 = 2_000_000.0


@pytest.fixture(autouse=True)
def _clean_table():
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE agent_runtime_instances")
    yield


class FakeProcTable:
    """Hands out fake pids and tracks which are 'alive'."""

    def __init__(self):
        self.spawned = []   # (entry, user_id, home)
        self.alive = {}     # pid -> bool
        self.killed = []    # pids we were asked to terminate
        self._next = 1000

    def spawn(self, entry, user_id, home):
        self._next += 1
        pid = self._next
        self.alive[pid] = True
        self.spawned.append((entry, user_id, home))
        return pid

    def is_alive(self, pid):
        return self.alive.get(pid, False)

    def kill(self, pid):
        self.killed.append(pid)
        self.alive[pid] = False


def _roster(*uids):
    # The 0009 FK (agent_runtime_instances.user_id → users) means tick→acquire
    # INSERTs a row that references `users`, so each hosted user must exist there.
    # Seed a users row per roster uid — mirrors the real path where every hosted
    # user is a registered account; without it acquire is FK-rejected and nothing
    # spawns. (u_ghost-style deleted-account behaviour is covered in the leases
    # tests, not here.)
    with db.get_pool().connection() as conn:
        for u in uids:
            conn.execute(
                "INSERT INTO users (user_id, created_at, doc) "
                "VALUES (%s, '', '{}'::jsonb) ON CONFLICT (user_id) DO NOTHING",
                (u,),
            )
    return [{"user_id": u, "api_key": f"key-{u}"} for u in uids]


def _sup(procs, owner="sup_A", clock=lambda: T0):
    return Supervisor(owner=owner, lease_ttl=300.0, data_root="/agent-data",
                      spawn_fn=procs.spawn, alive_fn=procs.is_alive,
                      kill_fn=procs.kill, now=clock)


def test_tick_spawns_one_consumer_per_user_with_isolated_homes():
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u_1", "u_2"))

    assert {s[1] for s in procs.spawned} == {"u_1", "u_2"}
    homes = {s[1]: s[2] for s in procs.spawned}
    assert homes["u_1"] == "/agent-data/users/u_1"
    assert homes["u_2"] == "/agent-data/users/u_2"
    assert homes["u_1"] != homes["u_2"]
    # Both leases held by this supervisor.
    assert leases.get("u_1")["lease_owner"] == "sup_A"
    assert leases.get("u_2")["lease_owner"] == "sup_A"


def test_tick_caps_new_spawns_per_tick():
    """冷启动大量用户时，一次 tick 只新起至多 max_spawns_per_tick 个 consumer，避免
    一次性 fork 几十个把 CVM 压垮；剩下的下个 tick 继续，最终全部起齐。"""
    procs = FakeProcTable()
    sup = Supervisor(owner="sup_A", lease_ttl=300.0, data_root="/agent-data",
                     spawn_fn=procs.spawn, alive_fn=procs.is_alive,
                     kill_fn=procs.kill, now=lambda: T0,
                     max_spawns_per_tick=2)
    roster = _roster("u1", "u2", "u3", "u4", "u5")

    sup.tick(roster)
    assert len(procs.spawned) == 2          # 本 tick 只起 2 个
    sup.tick(roster)
    assert len(procs.spawned) == 4          # 下个 tick 再起 2 个
    sup.tick(roster)
    assert len(procs.spawned) == 5          # 第三 tick 起最后 1 个 → 全部起齐
    # 已起的不会被重复 spawn
    sup.tick(roster)
    assert len(procs.spawned) == 5


def test_tick_unlimited_spawns_by_default():
    """默认（max_spawns_per_tick 未设）保持原行为：一个 tick 把整张 roster 全 spawn。"""
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u1", "u2", "u3", "u4", "u5"))
    assert len(procs.spawned) == 5


def test_tick_defers_spawn_while_genesis_in_progress(monkeypatch):
    # "先 genesis 后 spawn": a host user whose import genesis is still running must
    # NOT boot a blank consumer; once genesis is done the next tick spawns.
    procs = FakeProcTable()
    sup = _sup(procs)
    monkeypatch.setattr(
        db, "get_blob",
        lambda uid, kind: {"status": "processing"} if kind == "genesis_state" else None)
    sup.tick(_roster("u_1"))
    assert procs.spawned == []                      # deferred while genesis runs
    monkeypatch.setattr(
        db, "get_blob",
        lambda uid, kind: {"status": "done"} if kind == "genesis_state" else None)
    sup.tick(_roster("u_1"))
    assert {s[1] for s in procs.spawned} == {"u_1"}  # genesis done → spawned


def test_tick_spawns_fresh_start_user_with_no_genesis(monkeypatch):
    # No genesis_state blob = fresh start (never uploaded) → must still spawn.
    procs = FakeProcTable()
    sup = _sup(procs)
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: None)
    sup.tick(_roster("u_1"))
    assert {s[1] for s in procs.spawned} == {"u_1"}


def test_tick_enqueues_introduction_after_spawn(monkeypatch):
    procs = FakeProcTable()
    enqueued = []
    sup = Supervisor(
        owner="sup_A",
        lease_ttl=300.0,
        data_root="/agent-data",
        spawn_fn=procs.spawn,
        alive_fn=procs.is_alive,
        kill_fn=procs.kill,
        now=lambda: T0,
        introduction_enqueuer=lambda user_id, entry, **kwargs: enqueued.append((user_id, entry, kwargs)) or {"job_id": "pj_intro"},
    )
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: None)

    sup.tick(_roster("u_1"))
    sup.tick(_roster("u_1"))

    assert len(enqueued) == 1
    assert enqueued[0][0] == "u_1"
    assert enqueued[0][1]["api_key"] == "key-u_1"
    assert enqueued[0][2]["api_url"]
    assert enqueued[0][2]["enclave_url"] is not None


class _IntroStore:
    """Fake store for the introduction enqueue path. Tracks the card-independent
    ``introduced_at`` marker (identity-card-never-gates, 2026-07)."""

    user_id = "u_1"

    def __init__(self, activated=False):
        self.jobs = []
        self.activated = activated
        self._introduced_at = ""

    def proactive_activation_ready(self):
        return self.activated

    def introduction_done(self):
        return bool(self._introduced_at)

    def mark_introduced(self, *, at_iso=None):
        if not self._introduced_at:
            self._introduced_at = at_iso or "2026-07-12T00:00:00"
        return {"introduced_at": self._introduced_at}

    def list_proactive_jobs(self, since_epoch=0, limit=0):
        return list(self.jobs)

    def append_proactive_job(self, job):
        self.jobs.append(job)
        return job


def _enqueue_intro(store, *, now):
    return supervisor_mod._enqueue_introduction_job_if_needed(
        "u_1",
        {"api_key": "k"},
        api_url="http://backend",
        enclave_url="https://enclave",
        now=now,
        get_store_fn=lambda _uid: store,
    )


def test_enqueue_introduction_job_when_profile_fields_empty(monkeypatch):
    store = _IntroStore(activated=False)
    monkeypatch.setattr(
        supervisor_mod,
        "_fetch_identity_plain_for_intro",
        lambda entry, **kwargs: ({"decrypt_status": "ok", "self_introduction": "", "signature": []}, ""),
    )

    # Not activated -> gated on the unified signal, nothing enqueued or marked.
    pending = _enqueue_intro(store, now=lambda: T0 - 1)
    assert pending is None
    assert store.jobs == []
    assert store.introduction_done() is False

    store.activated = True
    job = _enqueue_intro(store, now=lambda: T0)
    duplicate = _enqueue_intro(store, now=lambda: T0 + 1)

    assert store.jobs == [job]
    assert job["job_kind"] == "introduction"
    assert job["trigger"] == "post_spawn_genesis"
    assert job["source"] == "agent_initiated_proactive"
    assert job["status"] == "pending"
    # Marked at enqueue -> the duplicate short-circuits on introduction_done.
    assert store.introduction_done() is True
    assert duplicate is None
    assert len(store.jobs) == 1


def test_enqueue_introduction_no_card_still_introduces_once(monkeypatch):
    # The core new behavior: a user with NO identity card (identity_not_found)
    # still gets exactly one introduction — the card is no longer a precondition.
    store = _IntroStore(activated=True)
    monkeypatch.setattr(
        supervisor_mod,
        "_fetch_identity_plain_for_intro",
        lambda entry, **kwargs: (None, "identity_not_found"),
    )

    job = _enqueue_intro(store, now=lambda: T0)
    duplicate = _enqueue_intro(store, now=lambda: T0 + 1)

    assert job is not None
    assert job["job_kind"] == "introduction"
    assert store.introduction_done() is True
    assert duplicate is None
    assert len(store.jobs) == 1


def test_enqueue_introduction_defers_on_transient_fetch_failure(monkeypatch):
    # identity is None but the reason is a TRANSIENT enclave/HTTP failure, NOT a
    # genuine no-card. Must defer (no job, no marker) so a later spawn can read a
    # real profile — never permanently mis-intro on a one-off hiccup.
    store = _IntroStore(activated=True)
    fetch_result = {"value": (None, "identity_fetch_failed:ReadTimeout")}
    monkeypatch.setattr(
        supervisor_mod,
        "_fetch_identity_plain_for_intro",
        lambda entry, **kwargs: fetch_result["value"],
    )

    deferred = _enqueue_intro(store, now=lambda: T0)
    assert deferred is None
    assert store.jobs == []
    assert store.introduction_done() is False

    # enclave recovers, card genuinely absent -> now proceed to intro.
    fetch_result["value"] = (None, "identity_not_found")
    job = _enqueue_intro(store, now=lambda: T0 + 1)
    assert job is not None
    assert store.introduction_done() is True
    assert len(store.jobs) == 1


def test_enqueue_introduction_defers_on_config_failure(monkeypatch):
    # enclave_url_missing / auth_missing are config failures, also deferred.
    store = _IntroStore(activated=True)
    monkeypatch.setattr(
        supervisor_mod,
        "_fetch_identity_plain_for_intro",
        lambda entry, **kwargs: (None, "enclave_url_missing"),
    )
    assert _enqueue_intro(store, now=lambda: T0) is None
    assert store.jobs == []
    assert store.introduction_done() is False


def test_enqueue_introduction_defers_on_transient_decrypt_failure(monkeypatch):
    # A decrypt failure is transient — it must NOT be marked introduced (that
    # would suppress a real intro forever). It retries once the card decrypts.
    store = _IntroStore(activated=True)
    fetch_result = {"value": ({"decrypt_status": "failed"}, "")}
    monkeypatch.setattr(
        supervisor_mod,
        "_fetch_identity_plain_for_intro",
        lambda entry, **kwargs: fetch_result["value"],
    )

    deferred = _enqueue_intro(store, now=lambda: T0)
    assert deferred is None
    assert store.jobs == []
    assert store.introduction_done() is False

    # Card now decrypts with empty profile -> enqueue proceeds.
    fetch_result["value"] = ({"decrypt_status": "ok", "self_introduction": "", "signature": []}, "")
    job = _enqueue_intro(store, now=lambda: T0 + 1)
    assert job is not None
    assert store.introduction_done() is True
    assert len(store.jobs) == 1


def test_enqueue_introduction_skips_existing_profile(monkeypatch):
    # A legacy card that already carries a self_introduction: no new job, but the
    # durable marker IS recorded so future spawns skip without an enclave fetch.
    store = _IntroStore(activated=True)
    fetch_calls = {"n": 0}

    def _fetch(entry, **kwargs):
        fetch_calls["n"] += 1
        return ({"decrypt_status": "ok", "self_introduction": "I am here.", "signature": []}, "")

    monkeypatch.setattr(supervisor_mod, "_fetch_identity_plain_for_intro", _fetch)

    job = _enqueue_intro(store, now=lambda: T0)
    assert job is None
    assert store.jobs == []
    assert store.introduction_done() is True
    assert fetch_calls["n"] == 1

    # Second spawn: short-circuits on introduction_done, no further enclave read.
    again = _enqueue_intro(store, now=lambda: T0 + 1)
    assert again is None
    assert fetch_calls["n"] == 1


def test_enqueue_introduction_not_ready_never_fetches_identity(monkeypatch):
    # Store read happens first now; an un-activated user must not trigger an
    # enclave identity fetch at all.
    store = _IntroStore(activated=False)

    def _fetch(entry, **kwargs):
        raise AssertionError("identity should not be fetched before activation")

    monkeypatch.setattr(supervisor_mod, "_fetch_identity_plain_for_intro", _fetch)

    assert _enqueue_intro(store, now=lambda: T0) is None
    assert store.introduction_done() is False


def test_tick_is_idempotent_for_live_children():
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u_1"))
    sup.tick(_roster("u_1"))   # child still alive → no respawn, just heartbeat
    assert len(procs.spawned) == 1


def test_other_supervisor_cannot_steal_a_live_lease():
    procs_a = FakeProcTable()
    sup_a = _sup(procs_a, owner="sup_A")
    sup_a.tick(_roster("u_1"))

    procs_b = FakeProcTable()
    sup_b = _sup(procs_b, owner="sup_B")
    sup_b.tick(_roster("u_1"))   # u_1's lease is live and owned by A

    assert procs_b.spawned == []
    assert leases.get("u_1")["lease_owner"] == "sup_A"


def test_dead_child_is_reaped_and_respawned():
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u_1"))
    # The child dies.
    pid = sup.children["u_1"]["pid"]
    procs.alive[pid] = False

    sup.tick(_roster("u_1"))      # detects death → release + reacquire + respawn
    assert len(procs.spawned) == 2
    assert sup.children["u_1"]["pid"] != pid


def test_tick_reaps_child_no_longer_in_roster():
    # The live roster is re-derived each tick (autodiscover toggles), so a
    # user who gets disabled drops out. Their consumer must be killed + lease
    # released this tick, not left orphaned until lease expiry.
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u_1", "u_2"))
    pid2 = sup.children["u_2"]["pid"]

    sup.tick(_roster("u_1"))            # u_2 dropped from the roster
    assert pid2 in procs.killed
    assert "u_2" not in sup.children
    assert leases.get("u_2")["lease_owner"] is None   # lease released for the dropped user
    assert "u_1" in sup.children        # u_1 untouched


def test_tick_respawns_alive_child_when_config_changes():
    # Per-tick re-derivation can change a live user's driver/provider/model (e.g.
    # autodiscover flips them). The running consumer's env/home is then stale →
    # it must be restarted, not just heartbeated.
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "claude", "provider": "anthropic"}])
    pid1 = sup.children["u_1"]["pid"]

    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "codex", "provider": "openai"}])
    assert pid1 in procs.killed
    pid2 = sup.children["u_1"]["pid"]
    assert pid2 != pid1
    assert sup.children["u_1"]["entry"]["driver"] == "codex"   # registry holds new config
    assert len(procs.spawned) == 2
    assert leases.get("u_1")["lease_owner"] == "sup_A"         # lease retained across restart


def test_tick_respawn_updates_lease_driver_column():
    # After an in-place driver switch (codex→claude on an API-key change), the
    # lease row's `driver` column must reflect the NEW driver — otherwise anything
    # reading the lease (ops, dashboards) sees a stale agent. Regression: in-place
    # respawn renewed the lease without updating driver, leaving it at the old value.
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "codex", "provider": "openai"}])
    assert leases.get("u_1")["driver"] == "codex"

    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "claude", "provider": "anthropic"}])
    assert leases.get("u_1")["driver"] == "claude"


def test_tick_no_respawn_when_config_unchanged():
    procs = FakeProcTable()
    sup = _sup(procs)
    e = {"user_id": "u_1", "api_key": "k", "driver": "claude", "provider": "anthropic"}
    sup.tick([dict(e)])
    sup.tick([dict(e)])      # identical config → heartbeat only
    assert procs.killed == []
    assert len(procs.spawned) == 1


def test_respawn_releases_inflight_claim_after_kill_before_spawn(monkeypatch):
    # Task 8 core guarantee: on a config-changed respawn the old consumer's
    # in-flight reply claim is released — but ONLY after kill_fn confirms the
    # old consumer is dead and BEFORE the replacement is spawned. Ordering is
    # the whole point: releasing before the kill would let a poll re-hand the
    # message while the old consumer is still burning provider quota on it
    # (chat/service.py:66-70's double-provider-burn window).
    events: list[str] = []

    class OrderedProcs(FakeProcTable):
        def spawn(self, entry, user_id, home):
            events.append("spawn")
            return super().spawn(entry, user_id, home)

        def kill(self, pid):
            events.append("kill")
            return super().kill(pid)

    def _fake_release(user_id):
        events.append("release")
        return 1  # non-zero → notify should fire

    monkeypatch.setattr(supervisor_mod.db, "chat_expire_reply_claims", _fake_release)
    monkeypatch.setattr(supervisor_mod.wake_bus, "notify",
                        lambda channel, uid="": events.append(f"notify:{channel}"))

    _roster("u_1")  # seed the users row so acquire's FK is satisfied
    procs = OrderedProcs()
    sup = _sup(procs)
    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "claude", "provider": "anthropic"}])
    assert sup.children.get("u_1"), "initial spawn should have tracked a child"
    events.clear()  # drop the initial spawn; we only care about the respawn tick

    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "codex", "provider": "openai"}])

    assert "release" in events, events
    assert events.index("kill") < events.index("release") < events.index("spawn"), events
    assert events.index("release") < events.index("notify:chat"), events


def test_tick_provider_key_rotation_respawns_consumer():
    # LiteLLM gateway retired: the real upstream provider_key now reaches the
    # consumer directly (no more LiteLLM-only indirection excluded from
    # _spawn_identity) — rotating it must bounce the consumer so it picks up
    # the new key.
    _roster("u_1")  # seed the users row so acquire's FK is satisfied
    procs = FakeProcTable()
    sup = _sup(procs)
    e = {"user_id": "u_1", "api_key": "k", "driver": "codex", "provider": "openai",
         "provider_key": "k1"}
    sup.tick([dict(e)])
    sup.tick([{**e, "provider_key": "rotated"}])
    assert len(procs.killed) == 1
    assert len(procs.spawned) == 2


def test_heartbeat_advances_lease_expiry():
    procs = FakeProcTable()
    t = {"v": T0}
    sup = _sup(procs, clock=lambda: t["v"])
    sup.tick(_roster("u_1"))
    exp1 = leases.get("u_1")["lease_expires_at"]
    t["v"] = T0 + 100
    sup.tick(_roster("u_1"))      # heartbeat
    exp2 = leases.get("u_1")["lease_expires_at"]
    assert exp2 > exp1


def test_losing_lease_kills_the_orphaned_child():
    # sup_A spawns u_1, then misses heartbeats long enough that sup_B takes over
    # the expired lease. sup_A's next tick must kill its now-orphaned child so two
    # consumers don't both run (the "exactly one consumer per user" guarantee).
    t = {"v": T0}
    procs_a = FakeProcTable()
    sup_a = _sup(procs_a, owner="sup_A", clock=lambda: t["v"])
    sup_a.tick(_roster("u_1"))
    pid = sup_a.children["u_1"]["pid"]

    t["v"] = T0 + 400  # past sup_A's lease expiry (ttl 300)
    sup_b = _sup(FakeProcTable(), owner="sup_B", clock=lambda: T0 + 400)
    sup_b.tick(_roster("u_1"))                 # sup_B takes over
    assert leases.get("u_1")["lease_owner"] == "sup_B"

    sup_a.tick(_roster("u_1"))                 # sup_A re-ticks; child still "alive"
    assert pid in procs_a.killed               # orphan terminated
    assert "u_1" not in sup_a.children


# ---- T1.1: lease renewal decoupled from the (slow) discover/resolve reconcile ----


def test_renew_live_advances_leases_without_a_roster():
    # The renew thread keeps leases fresh on its own cadence — independent of how
    # long a host-all resolve lap takes. It renews from in-memory children, no
    # roster, no spawn/reap reconcile.
    procs = FakeProcTable()
    t = {"v": T0}
    sup = _sup(procs, clock=lambda: t["v"])
    sup.tick(_roster("u_1", "u_2"))
    exp1 = leases.get("u_1")["lease_expires_at"]

    t["v"] = T0 + 100
    sup.renew_live()
    assert leases.get("u_1")["lease_expires_at"] > exp1
    assert leases.get("u_2")["lease_expires_at"] > exp1
    assert len(procs.spawned) == 2          # pure renewal: no new spawns


def test_renew_live_reaps_a_dead_child():
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u_1"))
    pid = sup.children["u_1"]["pid"]
    procs.alive[pid] = False                # child died between ticks

    sup.renew_live()
    assert "u_1" not in sup.children
    assert leases.get("u_1")["lease_owner"] is None   # lease released


def test_renew_live_reclaims_own_expired_lease_without_killing():
    # The churn bug: if our own lease lapsed (a slow lap outran the TTL) but no
    # other supervisor took it, the renewer must RECLAIM it — not kill a healthy
    # child and re-spawn it (the death spiral that 503'd every send).
    t = {"v": T0}
    procs = FakeProcTable()
    sup = _sup(procs, clock=lambda: t["v"])
    sup.tick(_roster("u_1"))
    pid = sup.children["u_1"]["pid"]

    t["v"] = T0 + 400                        # past our own ttl (300); nobody took it
    sup.renew_live()
    assert pid not in procs.killed           # healthy child NOT killed
    assert "u_1" in sup.children
    row = leases.get("u_1")
    assert row["lease_owner"] == "sup_A"     # reclaimed
    assert row["lease_expires_at"] is not None


def test_renew_live_kills_orphan_after_lease_lost():
    t = {"v": T0}
    procs_a = FakeProcTable()
    sup_a = _sup(procs_a, owner="sup_A", clock=lambda: t["v"])
    sup_a.tick(_roster("u_1"))
    pid = sup_a.children["u_1"]["pid"]

    t["v"] = T0 + 400                        # past sup_A's ttl (300)
    sup_b = _sup(FakeProcTable(), owner="sup_B", clock=lambda: T0 + 400)
    sup_b.tick(_roster("u_1"))               # sup_B takes the expired lease
    assert leases.get("u_1")["lease_owner"] == "sup_B"

    sup_a.renew_live()                       # sup_A's renewer notices it lost the lease
    assert pid in procs_a.killed
    assert "u_1" not in sup_a.children


def test_renew_live_reaps_when_other_owner_took_over_then_expired():
    # Codex P1: A's lease lapses, B takes over and spawns; later B's lease also
    # briefly lapses. A's renewer must NOT reclaim B's row (that double-runs two
    # consumers) — it reaps its own orphaned child and leaves B's lease alone.
    t = {"v": T0}
    procs_a = FakeProcTable()
    sup_a = _sup(procs_a, owner="sup_A", clock=lambda: t["v"])
    sup_a.tick(_roster("u_1"))
    pid = sup_a.children["u_1"]["pid"]

    sup_b = _sup(FakeProcTable(), owner="sup_B", clock=lambda: T0 + 400)
    sup_b.tick(_roster("u_1"))                       # B takes the expired lease
    assert leases.get("u_1")["lease_owner"] == "sup_B"

    t["v"] = T0 + 800                                # B's lease (exp T0+700) lapsed too
    sup_a.renew_live()
    assert pid in procs_a.killed                     # A reaps its orphan, doesn't steal
    assert "u_1" not in sup_a.children
    assert leases.get("u_1")["lease_owner"] == "sup_B"   # B's row untouched


def test_respawn_does_not_lose_lease_to_concurrent_renew():
    # Race regression (Codex [P2]): the in-place respawn kills the old pid before
    # swapping the tracked child. If renew_live snapshots the old child in that
    # window and sees its pid dead, it must NOT release the lease that respawn is
    # about to renew for the replacement consumer. The respawn holds the lock
    # across kill→spawn→renew→swap, so the renewer can't interleave.
    import threading
    import time as _t

    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "claude", "provider": "anthropic"}])
    old_pid = sup.children["u_1"]["pid"]

    renew_done = threading.Event()
    real_kill = procs.kill

    def kill_then_race_renew(pid):
        real_kill(pid)                       # old pid now reports dead
        if pid == old_pid:
            # Fire the renewer at the worst moment — mid-respawn, old pid dead.
            threading.Thread(
                target=lambda: (sup.renew_live(), renew_done.set())
            ).start()
            _t.sleep(0.05)                   # let it reach the lock / its reap
    sup.kill_fn = kill_then_race_renew

    # Config change → in-place respawn.
    sup.tick([{"user_id": "u_1", "api_key": "k", "driver": "codex", "provider": "openai"}])
    renew_done.wait(2)

    row = leases.get("u_1")
    assert row is not None
    assert row["lease_owner"] == "sup_A"            # lease NOT released by the renewer
    assert row["lease_expires_at"] is not None      # still live
    assert "u_1" in sup.children                     # replacement child retained


def test_shutdown_releases_all_leases_and_kills_children():
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u_1", "u_2"))
    pids = {sup.children["u_1"]["pid"], sup.children["u_2"]["pid"]}
    sup.shutdown()
    assert set(procs.killed) == pids
    assert leases.get("u_1")["lease_owner"] is None
    assert leases.get("u_2")["lease_owner"] is None


def test_parse_roster_accepts_json_string_and_list():
    parsed = parse_roster('[{"api_key": "k1"}, {"api_key": "k2", "model": "m"}]')
    assert [e["api_key"] for e in parsed] == ["k1", "k2"]
    assert parse_roster([{"api_key": "k3"}])[0]["api_key"] == "k3"


def test_parse_roster_drops_entries_without_api_key():
    assert parse_roster('[{"model": "m"}, {"api_key": "ok"}]') == [{"api_key": "ok"}]


# ---- Stage B: supervisor self-fetches the provider-key envelope (no roster secret) ----


def test_resolve_roster_self_fetches_envelope_when_entry_has_only_api_key(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enc")
    monkeypatch.setattr(supervisor_mod, "_whoami", lambda api_url, api_key: "usr_1")
    monkeypatch.setattr(
        supervisor_mod, "_fetch_key_envelope",
        lambda api_url, api_key: {"ct": "cipher"} if api_key == "k1" else None,
    )
    monkeypatch.setattr(
        supervisor_mod, "_decrypt_provider_key",
        lambda enclave_url, api_key, env: "sk-real" if env == {"ct": "cipher"} else "",
    )
    out = supervisor_mod._resolve_roster([{"api_key": "k1"}])
    assert out[0]["user_id"] == "usr_1"
    assert out[0]["provider_key"] == "sk-real"
    assert "provider_key_envelope" not in out[0]


def test_token_writer_invoked_on_spawn_and_renew():
    # Stage D slice 3a: the supervisor refreshes each user's runtime-token file
    # when it spawns the consumer AND on every heartbeat (so a short-lived token
    # is kept fresh for the long-running child).
    procs = FakeProcTable()
    writes = []
    sup = Supervisor(owner="sup_A", lease_ttl=300.0, data_root="/agent-data",
                     spawn_fn=procs.spawn, alive_fn=procs.is_alive, kill_fn=procs.kill,
                     now=lambda: T0, token_writer=lambda uid, home: writes.append((uid, home)))
    sup.tick(_roster("u1"))
    assert ("u1", "/agent-data/users/u1") in writes   # written at spawn
    writes.clear()
    sup.tick(_roster("u1"))                            # live child → heartbeat
    assert ("u1", "/agent-data/users/u1") in writes   # refreshed on renew


def test_no_token_writer_is_a_noop():
    procs = FakeProcTable()
    sup = Supervisor(owner="sup_A", lease_ttl=300.0, data_root="/agent-data",
                     spawn_fn=procs.spawn, alive_fn=procs.is_alive, kill_fn=procs.kill,
                     now=lambda: T0)  # no token_writer
    sup.tick(_roster("u1"))  # must not raise
    assert procs.spawned == [({"user_id": "u1", "api_key": "key-u1"}, "u1", "/agent-data/users/u1")]


# ---- _effective_roster (LiteLLM gateway retired: no gateway filtering left) ----


def test_effective_roster_autodiscover_intersects_enabled(monkeypatch):
    base = [{"user_id": "a", "api_key": "k1"}, {"user_id": "b", "api_key": "k2"}]
    monkeypatch.setattr(supervisor_mod, "_discover_enabled",
                        lambda: {"a": {"driver": "claude", "provider": "anthropic",
                                      "model": "x", "base_url": ""}})
    roster = supervisor_mod._effective_roster(base, autodiscover=True)
    assert [e["user_id"] for e in roster] == ["a"]   # b not backend-enabled → excluded
    assert roster[0]["driver"] == "claude"


def test_effective_roster_empty_is_tolerated_not_fatal(monkeypatch):
    # A live agent-runner must idle (not exit/crashloop) when no user is enabled yet
    # — discovery returns nothing → empty effective roster, no exception.
    monkeypatch.setattr(supervisor_mod, "_discover_enabled", lambda: {})
    roster = supervisor_mod._effective_roster([], autodiscover=True)
    assert roster == []


def test_effective_roster_no_autodiscover_passes_base_roster_through():
    # LiteLLM gateway retired: with autodiscover off, every fit-provider entry
    # (including codex against gemini/openrouter/openai_compatible) passes through
    # as-is — no gateway filtering or requested-model rewrite.
    base = [
        {"user_id": "a", "driver": "claude", "provider": "anthropic", "api_key": "k"},
        {"user_id": "c", "driver": "codex", "provider": "gemini", "model": "gemini-2.0-flash",
         "api_key": "k", "provider_key": "pk"},
    ]
    roster = supervisor_mod._effective_roster(base, autodiscover=False)
    assert [e["user_id"] for e in roster] == ["a", "c"]
    assert {e["user_id"]: e.get("model") for e in roster}["c"] == "gemini-2.0-flash"


def test_resolve_roster_prefers_existing_secret_over_self_fetch(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enc")
    monkeypatch.setattr(supervisor_mod, "_whoami", lambda api_url, api_key: "usr_2")
    called = {"fetched": False}

    def _no_fetch(api_url, api_key):
        called["fetched"] = True
        return None

    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope", _no_fetch)
    # A roster entry that already carries a plaintext provider_key must not trigger
    # a backend fetch (dev path / explicit override wins).
    out = supervisor_mod._resolve_roster([{"api_key": "k2", "provider_key": "sk-explicit"}])
    assert out[0]["provider_key"] == "sk-explicit"
    assert called["fetched"] is False


# ---- Stage D: zero-roster credential resolution via runtime token ----


def test_auth_headers_prefers_runtime_token():
    assert supervisor_mod._auth_headers(runtime_token="tok") == {"X-Feedling-Runtime-Token": "tok"}
    assert supervisor_mod._auth_headers(api_key="k") == {"X-API-Key": "k"}


def test_resolve_discovered_builds_entries_via_token_without_api_key(monkeypatch):
    # host-all zero-roster: the supervisor knows only the user_id (from the DB).
    # It mints a runtime token and uses it to self-fetch + enclave-decrypt the
    # provider key — NO api_key anywhere.
    enabled = {"u1": {"driver": "claude", "provider": "anthropic", "model": "claude-x", "base_url": ""}}
    minted = {}

    def fake_mint(uid):
        minted[uid] = f"tok-{uid}"
        return minted[uid]

    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope",
                        lambda api_url, api_key="", runtime_token="": ({"ct": "x"} if runtime_token else None))
    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key",
                        lambda enclave_url, api_key="", envelope=None, runtime_token="": ("sk-ant" if runtime_token else ""))
    out = supervisor_mod._resolve_discovered(enabled, mint_token=fake_mint,
                                             api_url="http://b:5001", enclave_url="https://e:5003", cache={})
    e = {x["user_id"]: x for x in out}["u1"]
    assert e["provider_key"] == "sk-ant" and e["driver"] == "claude"
    assert "api_key" not in e                       # zero-roster: no api_key
    assert minted["u1"] == "tok-u1"


def test_resolve_discovered_caches_by_user_and_envelope(monkeypatch):
    # Re-resolving the same user with the same envelope must NOT re-hit the enclave
    # every tick (decrypt is a network call).
    enabled = {"u1": {"driver": "claude", "provider": "anthropic", "model": "m", "base_url": ""}}
    calls = {"n": 0}
    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope",
                        lambda api_url, api_key="", runtime_token="": {"ct": "same"})

    def dec(enclave_url, api_key="", envelope=None, runtime_token=""):
        calls["n"] += 1
        return "sk"

    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key", dec)
    cache = {}
    supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                       api_url="a", enclave_url="e", cache=cache)
    supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                       api_url="a", enclave_url="e", cache=cache)
    assert calls["n"] == 1                           # same envelope → decrypted once


def test_resolve_discovered_isolates_per_user_failure(monkeypatch):
    """一个用户的 mint/fetch/decrypt 抛异常（或超时）绝不能让整圈 resolve 崩掉——
    其余用户照常解析。否则单个坏用户会拖垮整个 roster，该 tick 谁都起不来。"""
    enabled = {
        "bad": {"driver": "claude", "provider": "anthropic", "model": "m", "base_url": ""},
        "good": {"driver": "claude", "provider": "deepseek", "model": "m", "base_url": ""},
    }

    def mint(uid):
        if uid == "bad":
            raise RuntimeError("token mint hung/failed")
        return f"tok-{uid}"

    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope",
                        lambda api_url, api_key="", runtime_token="": {"ct": "x"})
    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key",
                        lambda enclave_url, api_key="", envelope=None, runtime_token="": "sk")
    out = supervisor_mod._resolve_discovered(enabled, mint_token=mint,
                                             api_url="a", enclave_url="e", cache={})
    uids = {e["user_id"] for e in out}
    assert "good" in uids        # 好用户照常解析
    assert "bad" not in uids     # 坏用户被隔离跳过：不抛、不拖累其余


def test_effective_roster_host_all_uses_discovered_entries():
    # When host-all supplies pre-credentialed discovered entries, they ARE the
    # roster (no api_key roster needed); base_roster only overrides by user_id.
    discovered = [{"user_id": "u1", "driver": "claude", "provider": "anthropic",
                   "model": "m", "base_url": "", "provider_key": "sk"}]
    roster = supervisor_mod._effective_roster(
        [], autodiscover=False, host_all_discovered=discovered)
    assert [e["user_id"] for e in roster] == ["u1"]
    assert roster[0]["provider_key"] == "sk" and "api_key" not in roster[0]


def test_effective_roster_host_all_base_roster_overrides_by_user_id():
    # A dev-supplied base_roster entry (with api_key) wins over discovery for the
    # same user — lets an operator pin a specific credential locally.
    discovered = [{"user_id": "u1", "driver": "claude", "provider": "anthropic", "provider_key": "sk-disc"}]
    base = [{"user_id": "u1", "api_key": "k", "driver": "claude", "provider": "anthropic", "provider_key": "sk-dev"}]
    roster = supervisor_mod._effective_roster(
        base, autodiscover=False, host_all_discovered=discovered)
    assert len(roster) == 1 and roster[0]["provider_key"] == "sk-dev"


# ---- _discover_enabled calls db.list_agent_runtime_enabled_users with no args
# (LiteLLM gateway retired — discovery is unconditional, no include_gateway/
# include_pi flags to thread through) ----


def test_discover_enabled_no_include_gateway_param():
    import inspect
    assert "include_gateway" not in inspect.signature(supervisor_mod._discover_enabled).parameters


def test_discover_enabled_calls_db_with_no_args(monkeypatch):
    captured = {"called": False}

    def fake_list():
        captured["called"] = True
        return []

    monkeypatch.setattr(supervisor_mod.db, "list_agent_runtime_enabled_users", fake_list)
    supervisor_mod._discover_enabled()
    assert captured["called"] is True


# ---- Codex P2: preserve a cached provider key across transient credential failures ----


def test_resolve_discovered_keeps_cached_key_on_transient_fetch_failure(monkeypatch):
    enabled = {"u1": {"driver": "claude", "provider": "anthropic", "model": "m", "base_url": ""}}
    state = {"env": {"ct": "x"}}
    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope",
                        lambda api_url, api_key="", runtime_token="": state["env"])
    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key",
                        lambda enclave_url, api_key="", envelope=None, runtime_token="": "sk-good")
    cache = {}
    out1 = supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                              api_url="a", enclave_url="e", cache=cache)
    assert out1[0]["provider_key"] == "sk-good"
    # fetch fails this tick → must keep the cached key, NOT drop it (else a healthy
    # consumer respawns keyless on a transient blip).
    state["env"] = None
    out2 = supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                              api_url="a", enclave_url="e", cache=cache)
    assert out2[0]["provider_key"] == "sk-good"


def test_resolve_discovered_keeps_cached_key_on_decrypt_failure(monkeypatch):
    enabled = {"u1": {"driver": "claude", "provider": "anthropic", "model": "m", "base_url": ""}}
    envs = iter([{"ct": "x"}, {"ct": "y"}])   # envelope changes → forces a re-decrypt
    monkeypatch.setattr(supervisor_mod, "_fetch_key_envelope",
                        lambda api_url, api_key="", runtime_token="": next(envs))
    decs = iter(["sk-good", ""])              # 2nd decrypt fails
    monkeypatch.setattr(supervisor_mod, "_decrypt_provider_key",
                        lambda enclave_url, api_key="", envelope=None, runtime_token="": next(decs))
    cache = {}
    supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                       api_url="a", enclave_url="e", cache=cache)
    out2 = supervisor_mod._resolve_discovered(enabled, mint_token=lambda u: "t",
                                              api_url="a", enclave_url="e", cache=cache)
    assert out2[0]["provider_key"] == "sk-good"   # failed re-decrypt keeps last good key


# ---- auto verify_loop: open the bootstrap gate for a freshly-hosted user ----


def test_autoverify_triggers_once_then_marks_done():
    # A freshly-hosted user is dead-ended at needs_live_connection until verify_loop
    # runs once. Once passing, it is marked done and never re-triggers.
    calls = []
    state = {}

    def post_verify(api_url, headers):
        calls.append(headers)
        return True   # passing

    passed = supervisor_mod._maybe_autoverify(
        "u1", mint_token=lambda u: "t", api_url="a",
        state=state, post_verify=post_verify, now=lambda: 100.0)
    skipped = supervisor_mod._maybe_autoverify(
        "u1", mint_token=lambda u: "t", api_url="a",
        state=state, post_verify=post_verify, now=lambda: 100.0)
    assert len(calls) == 1                          # second call skipped (done)
    assert calls[0] == {"X-Feedling-Runtime-Token": "t"}
    assert state["u1"]["done"] is True
    assert passed is True
    assert skipped is False


def test_autoverify_backs_off_after_failure():
    # A user that cannot pass the current bootstrap gate must NOT be re-probed
    # every tick — back off so we don't generate avoidable verify traffic.
    calls = []
    state = {}
    clock = {"t": 0.0}

    def post_verify(api_url, headers):
        calls.append(clock["t"])
        return False   # never passes

    now = lambda: clock["t"]
    passed = supervisor_mod._maybe_autoverify(
        "u1", mint_token=lambda u: "t", api_url="a",
        state=state, post_verify=post_verify, now=now)
    assert len(calls) == 1                          # first probe at t=0
    skipped = supervisor_mod._maybe_autoverify(
        "u1", mint_token=lambda u: "t", api_url="a",
        state=state, post_verify=post_verify, now=now)
    assert len(calls) == 1                          # immediate retry suppressed (backoff)
    assert passed is False
    assert skipped is False
    clock["t"] = 10_000.0
    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=post_verify, now=now)
    assert len(calls) == 2                          # probes again only after the window


def test_autoverify_stops_probing_once_it_passes_after_failures():
    state = {}
    clock = {"t": 0.0}
    results = iter([False, True])

    def post_verify(api_url, headers):
        return next(results)

    now = lambda: clock["t"]
    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=post_verify, now=now)
    assert state["u1"]["done"] is False             # failed → backing off
    clock["t"] = 10_000.0
    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=post_verify, now=now)
    assert state["u1"]["done"] is True              # passed → done
    called = {"n": 0}

    def pv2(api_url, headers):
        called["n"] += 1
        return True

    clock["t"] = 99_999.0
    supervisor_mod._maybe_autoverify("u1", mint_token=lambda u: "t", api_url="a",
                                     state=state, post_verify=pv2, now=now)
    assert called["n"] == 0                         # done → never probes again


def test_supervisor_heartbeat_payload_shape():
    """每 tick 写入 server_config 的全局心跳载荷：ts + owner + host_all + pi。
    backend 的 wedge 守卫据此判断 supervisor 是否在托管。"""
    p = supervisor_mod._supervisor_heartbeat_payload(
        "host:7", host_all=True, pi=False, ts=123.5)
    assert p == {"ts": 123.5, "owner": "host:7", "host_all": True, "pi": False}


def test_heartbeat_payload_has_pi_not_gateway():
    p = supervisor_mod._supervisor_heartbeat_payload("o", host_all=True, pi=True, ts=1.0)
    assert p["pi"] is True and "gateway" not in p


def test_heartbeat_loop_writes_on_cadence_until_stopped(monkeypatch):
    """心跳必须由独立线程按固定节奏写入，不被主循环的 discover→spawn 慢工作阻塞——
    冷启动大量用户时，wedge 守卫不能因 supervision 跑得慢而误判 supervisor 死掉。"""
    import threading
    import time

    writes = []
    monkeypatch.setattr(supervisor_mod.db, "set_supervisor_heartbeat",
                        lambda payload: writes.append(payload))
    stop = threading.Event()
    t = threading.Thread(target=supervisor_mod._heartbeat_loop, kwargs=dict(
        owner="host:1", host_all=True, pi=True, interval=0.01, stop_event=stop))
    t.start()
    time.sleep(0.05)
    stop.set()
    t.join(timeout=2)

    assert not t.is_alive()                  # stop_event 能干净停止线程
    assert len(writes) >= 1                   # 持续写心跳（不依赖任何 tick 完成）
    assert writes[0]["owner"] == "host:1"
    assert writes[0]["host_all"] is True
    assert writes[0]["pi"] is True
    assert "gateway" not in writes[0]
    assert "ts" in writes[0]


def test_heartbeat_loop_survives_write_errors(monkeypatch):
    """单次心跳写失败（DB blip）绝不能让心跳线程退出——否则一次抖动后 wedge 永远 503。"""
    import threading
    import time

    calls = {"n": 0}

    def flaky(_payload):
        calls["n"] += 1
        raise RuntimeError("db blip")

    monkeypatch.setattr(supervisor_mod.db, "set_supervisor_heartbeat", flaky)
    stop = threading.Event()
    t = threading.Thread(target=supervisor_mod._heartbeat_loop, kwargs=dict(
        owner="o", host_all=False, pi=False, interval=0.01, stop_event=stop))
    t.start()
    time.sleep(0.05)
    stop.set()
    t.join(timeout=2)

    assert not t.is_alive()
    assert calls["n"] >= 2                     # 写失败后继续重试，没有退出


# ---- PR A: per-owner multi-instance heartbeat ----
# Multiple runners each write their OWN row (no clobber of the legacy single key).
# The loop also prunes dead-runner rows and survives write/prune blips.

def test_supervisor_instance_payload_shape():
    p = supervisor_mod._supervisor_instance_payload(
        "host:7", host="host", host_all=True, pi=False,
        active_children=3, max_children=4, shard_index=1, shard_count=2,
        version="abc", ts=123.5)
    assert p == {
        "ts": 123.5, "owner": "host:7", "host": "host", "host_all": True,
        "pi": False, "active_children": 3, "max_children": 4,
        "shard_index": 1, "shard_count": 2, "version": "abc",
    }


def test_heartbeat_loop_writes_per_owner_instance_row(monkeypatch):
    import threading
    import time

    monkeypatch.setattr(supervisor_mod.db, "set_supervisor_heartbeat", lambda payload: None)
    inst_writes = []
    monkeypatch.setattr(supervisor_mod.db, "set_supervisor_instance_heartbeat",
                        lambda owner, payload: inst_writes.append((owner, payload)))
    stop = threading.Event()
    t = threading.Thread(target=supervisor_mod._heartbeat_loop, kwargs=dict(
        owner="host:1", host_all=True, pi=True, interval=0.01, stop_event=stop,
        instance_payload_fn=lambda ts: supervisor_mod._supervisor_instance_payload(
            "host:1", host="host", host_all=True, pi=True, active_children=2,
            max_children=4, shard_index=0, shard_count=1, version=None, ts=ts)))
    t.start()
    time.sleep(0.05)
    stop.set()
    t.join(timeout=2)

    assert not t.is_alive()
    assert len(inst_writes) >= 1
    owner, payload = inst_writes[0]
    assert owner == "host:1"
    assert payload["active_children"] == 2 and payload["max_children"] == 4
    assert payload["host_all"] is True and "gateway" not in payload
    assert "ts" in payload


def test_heartbeat_loop_instance_write_error_does_not_kill_loop(monkeypatch):
    import threading
    import time

    legacy = {"n": 0}
    monkeypatch.setattr(supervisor_mod.db, "set_supervisor_heartbeat",
                        lambda payload: legacy.__setitem__("n", legacy["n"] + 1))

    def boom(owner, payload):
        raise RuntimeError("db blip")

    monkeypatch.setattr(supervisor_mod.db, "set_supervisor_instance_heartbeat", boom)
    stop = threading.Event()
    t = threading.Thread(target=supervisor_mod._heartbeat_loop, kwargs=dict(
        owner="o", host_all=True, pi=True, interval=0.01, stop_event=stop,
        instance_payload_fn=lambda ts: {"owner": "o", "ts": ts}))
    t.start()
    time.sleep(0.05)
    stop.set()
    t.join(timeout=2)

    assert not t.is_alive()
    assert legacy["n"] >= 2  # legacy write keeps going despite instance-write errors


def test_heartbeat_loop_prunes_dead_instance_rows(monkeypatch):
    import threading
    import time

    monkeypatch.setattr(supervisor_mod.db, "set_supervisor_heartbeat", lambda payload: None)
    monkeypatch.setattr(supervisor_mod.db, "set_supervisor_instance_heartbeat",
                        lambda owner, payload: None)
    prunes = []
    monkeypatch.setattr(supervisor_mod.db, "prune_supervisor_instance_heartbeats",
                        lambda max_age: prunes.append(max_age))
    stop = threading.Event()
    t = threading.Thread(target=supervisor_mod._heartbeat_loop, kwargs=dict(
        owner="o", host_all=True, pi=True, interval=0.01, stop_event=stop,
        instance_payload_fn=lambda ts: {"owner": "o", "ts": ts},
        prune_max_age_sec=3600.0))
    t.start()
    time.sleep(0.05)
    stop.set()
    t.join(timeout=2)

    assert not t.is_alive()
    assert prunes and prunes[0] == 3600.0


# ---- PR B: AGENT_MAX_CHILDREN (per-runner capacity ceiling) ----
# Steady-state TOTAL ceiling (vs max_spawns_per_tick's RATE limit) so one runner
# doesn't grab every user — the rest get acquired by other runners (lease-backed).

def _sup_cap(procs, *, max_children=0, max_spawns_per_tick=0, owner="sup_A", clock=lambda: T0):
    return Supervisor(owner=owner, lease_ttl=300.0, data_root="/agent-data",
                      spawn_fn=procs.spawn, alive_fn=procs.is_alive, kill_fn=procs.kill,
                      now=clock, max_children=max_children,
                      max_spawns_per_tick=max_spawns_per_tick)


def test_tick_caps_total_children_at_max_children():
    procs = FakeProcTable()
    sup = _sup_cap(procs, max_children=2)
    roster = _roster("u1", "u2", "u3", "u4")
    sup.tick(roster)
    assert len(sup.children) == 2          # capped
    sup.tick(roster)
    assert len(sup.children) == 2          # does not creep up over ticks
    assert len(procs.spawned) == 2


def test_tick_max_children_unlimited_when_zero():
    procs = FakeProcTable()
    sup = _sup_cap(procs, max_children=0)
    sup.tick(_roster("u1", "u2", "u3"))
    assert len(sup.children) == 3


def test_tick_existing_children_not_killed_when_over_max():
    # A redeploy that LOWERS max_children must not kill already-running consumers.
    procs = FakeProcTable()
    sup = _sup_cap(procs, max_children=2)
    sup.tick(_roster("u1", "u2"))
    assert len(sup.children) == 2
    sup.max_children = 1                    # simulate redeploy with a smaller cap
    sup.tick(_roster("u1", "u2", "u3"))
    assert procs.killed == []               # neither existing child killed
    assert "u3" not in sup.children         # but no NEW user acquired over the cap
    assert len(sup.children) == 2


def test_tick_does_not_acquire_beyond_max_children():
    # At capacity a new user is never lease-acquired here — left for another runner.
    procs = FakeProcTable()
    sup = _sup_cap(procs, max_children=1)
    sup.tick(_roster("u1", "u2"))
    assert len(sup.children) == 1
    assert leases.get("u2") is None         # u2 over cap → never acquired


# ---- PR B: lease-scoped gateway (owned-children filter) ----

def test_tick_pre_spawn_runs_before_new_consumers_start():
    # The gateway route for a newly-acquired user must be configured BEFORE its
    # consumer process starts, so a queued first turn can't hit a missing route.
    procs = FakeProcTable()
    sup = _sup(procs)
    events = []
    orig_spawn = procs.spawn

    def tracking_spawn(entry, uid, home):
        events.append(("spawn", uid))
        return orig_spawn(entry, uid, home)

    sup.spawn_fn = tracking_spawn
    sup.tick(_roster("u1", "u2"),
             pre_spawn=lambda ids: events.append(("pre_spawn", sorted(ids))))

    # pre_spawn fires ONCE for the whole batch, before any of those spawns.
    assert events[0] == ("pre_spawn", ["u1", "u2"])
    assert ("spawn", "u1") in events and ("spawn", "u2") in events
    assert events.index(("pre_spawn", ["u1", "u2"])) < events.index(("spawn", "u1"))
    assert events.index(("pre_spawn", ["u1", "u2"])) < events.index(("spawn", "u2"))


def test_tick_pre_spawn_not_called_without_new_acquisitions():
    # Steady state (only live children, nothing new) → no pre_spawn callback, so
    # no needless gateway reconcile/restart.
    procs = FakeProcTable()
    sup = _sup(procs)
    sup.tick(_roster("u1"))                 # initial acquire+spawn
    calls = []
    sup.tick(_roster("u1"), pre_spawn=lambda ids: calls.append(list(ids)))
    assert calls == []                      # u1 already live → nothing acquired


def test_spawn_identity_changes_when_base_url_changes():
    a = {"api_key": "k", "driver": "claude", "provider": "anthropic",
         "model": "claude-3.5-sonnet", "base_url": ""}
    b = dict(a, base_url="https://relay.example/anthropic")
    assert supervisor_mod._spawn_identity(a) != supervisor_mod._spawn_identity(b)


def test_spawn_identity_changes_when_identity_model_changes():
    # gateway 用户 model 是稳定的 gw-<uid> 别名；切换真实上游模型须触发 respawn 重落身份块
    a = {"driver": "codex", "provider": "gemini", "model": "gw-u1",
         "identity_model": "gemini-2.0-flash"}
    b = dict(a, identity_model="gemini-1.5-pro")
    assert supervisor_mod._spawn_identity(a) != supervisor_mod._spawn_identity(b)
