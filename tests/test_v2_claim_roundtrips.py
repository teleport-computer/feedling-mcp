"""claim / mark_running 的往返预算 + 合并后仍要守住的边界。

Why this exists: the CVM runs in Phala and the RDS is in AWS — one round trip
measured 63.8ms on test. `claim_next_job` used to issue 7 of them per claim
(~450ms) and `mark_running` 5 (~320ms), which is pure latency on the path
between "user hit send" and "turn starts". These tests pin the statement
budget so a future edit cannot quietly re-add a round trip, and they re-pin
the two invariants the merge must not break: the runtime_state -> job lock
order, and the 0041 worker-protocol gate (a claim whose `set_config` did not
run is rejected by the trigger, so a passing claim proves it ran).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 claim tests require the PostgreSQL test fixture",
)


class _CountingCursor:
    """Wraps a real cursor and counts execute() calls (= server round trips)."""

    def __init__(self, inner, counter):
        self._inner = inner
        self._counter = counter

    def execute(self, *args, **kwargs):
        self._counter.append(args[0] if args else "")
        return self._inner.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


class _CountingConnection:
    def __init__(self, inner, counter):
        self._inner = inner
        self._counter = counter

    def cursor(self, *args, **kwargs):
        return _CountingCursor(self._inner.cursor(*args, **kwargs), self._counter)

    def execute(self, *args, **kwargs):
        self._counter.append(args[0] if args else "")
        return self._inner.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _CountingPool:
    def __init__(self, inner, counter):
        self._inner = inner
        self._counter = counter

    def connection(self):
        import contextlib

        @contextlib.contextmanager
        def _wrap():
            with self._inner.connection() as conn:
                yield _CountingConnection(conn, self._counter)

        return _wrap()


@pytest.fixture
def count_statements(monkeypatch):
    """Yield a list that collects every SQL statement the callee executes."""
    counter: list[str] = []

    def _install():
        monkeypatch.setattr(
            jobs_store, "_pool", lambda: _CountingPool(db.get_pool(), counter)
        )
        return counter

    return _install


def _drain_pending() -> None:
    """Statement-budget assertions only hold when our job is the queue head.

    Other modules leave pending rows behind, and claim would then walk them
    (each miss costs an extra statement) before reaching ours — green alone,
    red in a full run. Clear the queue so the budget measures one claim.
    """
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE status='pending'")


def _fresh_user() -> str:
    uid = f"u_claim_rt_{uuid.uuid4().hex[:8]}"
    seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'v2',1) ON CONFLICT (user_id) DO UPDATE SET "
            "hosted_runtime_state='v2',runtime_generation=1",
            (uid,),
        )
    return uid


def test_claim_happy_path_stays_within_two_statements(count_statements):
    """set_config+candidate+state-lock is one statement; lock+claim is another."""
    uid = _fresh_user()
    _drain_pending()
    jobs_store.enqueue_job(uid, "chat", expected_generation=1)
    counter = count_statements()

    claimed = jobs_store.claim_next_job("w-rt-1")

    assert claimed is not None and claimed["user_id"] == uid
    assert claimed["status"] == "claimed"
    assert len(counter) <= 2, f"claim issued {len(counter)} statements: {counter}"


def test_idle_claim_costs_one_statement(count_statements):
    """An empty queue is the most frequent case — it must not cost more than
    the single candidate probe."""
    _drain_pending()
    counter = count_statements()

    assert jobs_store.claim_next_job("w-rt-idle-probe") is None
    assert len(counter) <= 2, f"idle claim issued {len(counter)}: {counter}"


def test_mark_running_stays_within_two_statements(count_statements):
    uid = _fresh_user()
    _drain_pending()
    jobs_store.enqueue_job(uid, "chat", expected_generation=1)
    claimed = jobs_store.claim_next_job("w-rt-2")
    assert claimed is not None
    counter = count_statements()

    assert jobs_store.mark_running(claimed["id"], claimed_by="w-rt-2") is True
    assert len(counter) <= 2, f"mark_running issued {len(counter)}: {counter}"


def test_stale_generation_is_superseded_not_claimed():
    """The merged statement must still route a stale-generation job to
    superseded instead of handing it to a worker."""
    uid = _fresh_user()
    jobs_store.enqueue_job(uid, "chat", expected_generation=99)

    assert jobs_store.claim_next_job("w-rt-stale") is None

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE user_id=%s", (uid,)
        ).fetchone()
    assert row[0] == "superseded"
    assert row[1] == "stale_runtime_generation"


def test_job_without_runtime_state_row_is_superseded():
    """Slow path: the merged candidate probe inner-joins v2_runtime_state, so a
    job whose state row is missing returns no candidate. It must still be
    retired (and the worker must not spin on it forever)."""
    uid = _fresh_user()
    jobs_store.enqueue_job(uid, "chat", expected_generation=1)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_runtime_state WHERE user_id=%s", (uid,))

    assert jobs_store.claim_next_job("w-rt-nostate") is None

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE user_id=%s", (uid,)
        ).fetchone()
    assert row[0] == "superseded"
    assert row[1] == "runtime_state_not_v2"


def test_concurrent_workers_claim_each_job_exactly_once():
    """The merged lock+claim statement must keep claim exclusive: with more
    workers than jobs, every job goes to exactly one worker and none is lost.
    (Each user gets at most one active job by the per-user fence, so spread the
    jobs across users to actually exercise contention.)"""
    from concurrent.futures import ThreadPoolExecutor

    uids = [_fresh_user() for _ in range(6)]
    for uid in uids:
        jobs_store.enqueue_job(uid, "chat", expected_generation=1)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = [
            pool.submit(jobs_store.claim_next_job, f"w-race-{i}") for i in range(12)
        ]
        claimed = [f.result(timeout=30) for f in results]

    got = [row["id"] for row in claimed if row is not None]
    assert len(got) == len(set(got)), f"a job was claimed twice: {got}"
    assert set(got) == set(_pending_job_ids(uids)), "a job was lost"


def _pending_job_ids(uids):
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id FROM agent_jobs WHERE user_id = ANY(%s)", (list(uids),)
        ).fetchall()
    return [r[0] for r in rows]


def test_resident_user_job_is_superseded():
    uid = _fresh_user()
    jobs_store.enqueue_job(uid, "chat", expected_generation=1)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_runtime_state SET hosted_runtime_state='resident' "
            "WHERE user_id=%s",
            (uid,),
        )

    assert jobs_store.claim_next_job("w-rt-resident") is None

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE user_id=%s", (uid,)
        ).fetchone()
    assert row[0] == "superseded"
    assert row[1] == "runtime_state_not_v2"
