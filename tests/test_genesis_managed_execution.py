"""Actual PG transaction/fence checks; artifact entrypoint integration is separate."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

import db
import genesis_managed_store as persistence
from core import envelope
from genesis import managed_execution as execution
from test_genesis_managed_admission import account, upload, claim


@pytest.fixture
def prepared(account):
    uid, _ = account
    upload(uid)
    job, _, witness = claim(account)
    store = SimpleNamespace(user_id=uid, memory_lock=threading.RLock())
    return store, job, witness


def read_probe(uid):
    with db.get_pool().connection() as conn:
        return conn.execute("SELECT doc FROM user_blobs WHERE user_id=%s AND kind='t795_probe'", (uid,)).fetchone()


def write_probe(conn, uid):
    conn.execute("INSERT INTO user_blobs(user_id,kind,doc) VALUES(%s,'t795_probe',%s)",
                 (uid, Jsonb({"changed": True})))


def start_applying(store, witness):
    execution.begin_execution(store, "j", witness)
    execution.admit_apply(store, "j", witness)


@pytest.mark.parametrize("memory", [False, True])
@pytest.mark.parametrize("when", ["before", "during"])
def test_actual_mutation_rolls_back_when_write_policy_changes(prepared, monkeypatch, memory, when):
    store, _, witness = prepared
    start_applying(store, witness)
    if when == "before":
        monkeypatch.setattr(envelope, "PLAINTEXT_WRITES_ACCEPTED", False)
    entered = False
    with pytest.raises(persistence.DependencyUnavailable, match="account_policy"):
        with execution.mutation_guard(store, "j", witness, memory=memory) as (conn, _):
            entered = True
            write_probe(conn, store.user_id)
            monkeypatch.setattr(envelope, "resolve_content_encryption", lambda _: "on")
    assert entered == (when == "during")
    assert read_probe(store.user_id) is None
    assert persistence.execution_state(store.user_id, "j", witness) == ("applying", None)


@pytest.mark.parametrize("memory", [False, True])
def test_claim_check_and_primary_write_share_connection(prepared, monkeypatch, memory):
    store, _, witness = prepared
    start_applying(store, witness)
    pool = db.get_pool()
    seen = []

    class CountedPool:
        @contextmanager
        def connection(self):
            with pool.connection() as conn:
                seen.append(conn.info.backend_pid)
                yield conn

    monkeypatch.setattr(db, "get_pool", lambda: CountedPool())
    with execution.mutation_guard(store, "j", witness, memory=memory) as (conn, job):
        assert job["job_id"] == "j"
        assert seen == [conn.info.backend_pid]
        if memory:
            with db.memory_user_mutation_fence(store.user_id) as nested:
                assert nested is conn
        write_probe(conn, store.user_id)
    assert len(seen) == 1
    assert read_probe(store.user_id) == ({"changed": True},)


def test_no_policy_permission_means_no_started_phase(prepared, monkeypatch):
    store, before, witness = prepared
    monkeypatch.setattr(envelope, "PLAINTEXT_WRITES_ACCEPTED", False)
    with pytest.raises(persistence.DependencyUnavailable):
        execution.begin_execution(store, "j", witness)
    assert db.genesis_get_job(store.user_id, "j") == before


def test_only_prepared_can_defer_and_old_generation_cannot_change_successor(prepared, account):
    store, before, witness = prepared
    deferred = persistence.defer_prepared(
        store.user_id, "j", witness, persistence.DependencyUnavailable("identity", "dependency_changed"),
    )
    assert deferred["status"] == "uploaded"
    assert deferred["updated_at"] == before["updated_at"]
    assert deferred["finalized_at"] == before["finalized_at"]
    assert deferred["resident_attempts"] == before["resident_attempts"]
    next_job, _, next_witness = claim(account)
    assert next_witness["generation"] != witness["generation"]
    for stale_operation in (
        lambda: execution.begin_execution(store, "j", witness),
        lambda: persistence.defer_prepared(store.user_id, "j", witness, persistence.DependencyUnavailable("identity")),
        lambda: persistence.fail(store.user_id, "j", witness, "stale", server=True),
    ):
        with pytest.raises(persistence.ClaimRejected):
            stale_operation()
    assert db.genesis_get_job(store.user_id, "j") == next_job
    execution.begin_execution(store, "j", next_witness)
    with pytest.raises(persistence.ClaimRejected):
        persistence.defer_prepared(store.user_id, "j", next_witness, persistence.DependencyUnavailable("identity"))


def test_duplicate_apply_and_timeout_readback_do_not_fail_or_replay(prepared):
    store, _, witness = prepared
    start_applying(store, witness)
    before = db.genesis_get_job(store.user_id, "j")
    with pytest.raises(persistence.ClaimRejected):
        execution.admit_apply(store, "j", witness)
    with pytest.raises(persistence.ClaimRejected):
        persistence.fail(store.user_id, "j", witness, "worker_timeout")
    assert persistence.execution_state(store.user_id, "j", witness) == ("applying", None)
    assert db.genesis_get_job(store.user_id, "j") == before
    # A committed result is returned only for this capability, even on a later retry.
    metadata = deepcopy(before["metadata"])
    metadata[persistence.MARKER]["phase"] = "done"
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE genesis_import_jobs SET status='done',metadata=%s,output=%s WHERE user_id=%s",
                     (Jsonb(metadata), Jsonb({"memory_action_count": 2}), store.user_id))
    assert persistence.execution_state(store.user_id, "j", witness) == ("done", {"memory_action_count": 2})
    with pytest.raises(persistence.ClaimRejected):
        persistence.execution_state(store.user_id, "j", {**witness, "capability": "wrong"})


@pytest.mark.parametrize("mode", ["", "legacy"])
def test_untrusted_mode_cannot_authorize_writes(prepared, mode):
    store, job, witness = prepared
    metadata = deepcopy(job["metadata"])
    metadata[persistence.MARKER]["mode"] = mode
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE genesis_import_jobs SET metadata=%s WHERE user_id=%s", (Jsonb(metadata), store.user_id))
    with pytest.raises(persistence.ClaimRejected):
        execution.begin_execution(store, "j", witness)


def test_phase_helper_rejects_shortcuts(prepared):
    store, _, witness = prepared
    for after in ("applying", "done", "prepared", "unknown"):
        with pytest.raises(persistence.ClaimRejected, match="transition_invalid"):
            persistence.set_phase(store.user_id, "j", witness, ("prepared",), after)


def test_memory_local_lock_precedes_database_checkout(prepared, monkeypatch):
    store, _, witness = prepared
    start_applying(store, witness)
    pool = db.get_pool()
    checkout = threading.Event()
    started = threading.Event()

    class ObservedPool:
        @contextmanager
        def connection(self):
            checkout.set()
            with pool.connection() as conn:
                yield conn

    monkeypatch.setattr(db, "get_pool", lambda: ObservedPool())

    def mutate():
        started.set()
        with execution.mutation_guard(store, "j", witness, memory=True) as (conn, _):
            write_probe(conn, store.user_id)

    with ThreadPoolExecutor(max_workers=1) as threads:
        with store.memory_lock:
            task = threads.submit(mutate)
            assert started.wait(2)
            assert not checkout.wait(0.1)
        task.result(timeout=3)
    assert checkout.is_set() and read_probe(store.user_id) == ({"changed": True},)


def test_reaper_claim_change_waits_until_primary_mutation_commits(prepared):
    store, _, witness = prepared
    start_applying(store, witness)
    attempted, completed = threading.Event(), threading.Event()

    def reaper():
        with db.get_pool().connection() as conn:
            conn.execute("SET lock_timeout='3s'")
            attempted.set()
            conn.execute("UPDATE genesis_import_jobs SET status='failed' WHERE user_id=%s", (store.user_id,))
            completed.set()

    with ThreadPoolExecutor(max_workers=1) as threads:
        with execution.mutation_guard(store, "j", witness) as (conn, _):
            task = threads.submit(reaper)
            assert attempted.wait(2)
            assert not completed.wait(0.1)
            write_probe(conn, store.user_id)
        task.result(timeout=4)
    assert read_probe(store.user_id) == ({"changed": True},)
    with pytest.raises(persistence.ClaimRejected):
        with execution.mutation_guard(store, "j", witness):
            pytest.fail("stale claim may not reach another artifact write")
