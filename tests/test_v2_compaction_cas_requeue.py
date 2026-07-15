"""Task 6 (D8): `worker._run_compaction`'s CAS-loss branch must requeue a fresh
"maintenance" job (single-flight via `jobs_store.enqueue_job`'s per-user/lane
coalesce) instead of silently abandoning a still-over-budget tail — mirrors the
SUCCESS path's catch-up requeue (worker.py:674-677), just keyed on CAS loss
instead of "tail still huge after a successful fold".

A NON-CAS failure (e.g. the provider call raising) must NOT requeue — only a
CAS loss (`upsert_summary_row_cas` returning False) retries. Exercised via the
real `worker._run_compaction` + real `jobs_store`/DB primitives (same pattern
as tests/test_v2_compaction_integration.py), with `read_tail`/`read_summary`/
`write_summary` as simple plaintext fakes over the real `v2_conversation_summary`
table so the CAS write path itself is real.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
import provider_client
import pytest
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import worker

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-x", api_key="sk-user-byok", base_url="")


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    # claim_next_job claims globally (no user_id filter) — truncate so a
    # leftover pending job from another test file can't get claimed here.
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


def _make_tail_deps(n: int):
    """`n` messages, ts=1..n, all role=user (role doesn't matter to
    `_run_compaction` — it only cares about count vs `_TAIL_KEEP`/`_COMPACTION_BATCH`).
    Over `_TAIL_KEEP` (10) so `_run_compaction` doesn't short-circuit as a
    no-op ("tail already under budget")."""
    messages = [{"id": f"m{i}", "ts": float(i), "content": f"turn {i}"} for i in range(1, n + 1)]

    def _read_tail(uid, after_ts, limit):
        out = [m for m in messages if m["ts"] > after_ts]
        return out[-limit:] if limit > 0 else []

    def _read_summary(uid):
        row = jobs_store.get_summary_row(uid)
        if row is None:
            return "", 0.0, 0
        env = row["summary_envelope"]
        if not env:
            return "", row["watermark_ts"], row["version"]
        return str(env.get("plaintext") or ""), row["watermark_ts"], row["version"]

    def _write_summary(uid, summary, watermark_ts, expected_version):
        return jobs_store.upsert_summary_row_cas(
            uid, summary_envelope={"plaintext": summary},
            watermark_ts=watermark_ts, expected_version=expected_version)

    return _read_tail, _read_summary, _write_summary


def _enqueue_and_claim_maintenance(uid) -> dict:
    jobs_store.enqueue_job(uid, "maintenance")
    job = jobs_store.claim_next_job("w-maint")
    assert job is not None and job["lane"] == "maintenance"
    return job


def _pending_maintenance_rows(uid) -> list[tuple]:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT id, status, reason FROM agent_jobs "
            "WHERE user_id=%s AND lane='maintenance' AND status='pending'",
            (uid,),
        ).fetchall()


def test_cas_loss_requeues_fresh_maintenance_job(monkeypatch):
    uid = "u_v2_cas_requeue"
    conftest.seed_user(uid)
    _reset(uid)

    read_tail, read_summary, write_summary = _make_tail_deps(worker._TAIL_KEEP + 5)

    async def _fake_llm(cfg, msgs, **kw):
        return {"reply": "- folded bullet"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)
    # Force the CAS write to lose the race no matter what version it's called with.
    monkeypatch.setattr(jobs_store, "upsert_summary_row_cas", lambda *a, **k: False)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=read_tail,
        read_summary=read_summary,
        write_summary=write_summary,
    )

    job = _enqueue_and_claim_maintenance(uid)
    sem = asyncio.Semaphore(1)
    status = asyncio.run(worker._run_compaction(
        job["id"], uid, deps, _BYOK, sem, claimed_by=job["claimed_by"]))

    assert status == "failed"
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job["id"],),
        ).fetchone()
    assert row[0] == "failed"
    assert row[1] == "summary_cas_lost"

    # A fresh maintenance job must now be pending for a retry — never a silent
    # abandon of a still-over-budget tail.
    pending = _pending_maintenance_rows(uid)
    assert len(pending) == 1, f"expected exactly one fresh pending maintenance job, got {pending}"
    assert pending[0][2] == "cas_lost_retry"

    # The lost attempt's summary write must not have landed (still absent/CAS'd away).
    assert jobs_store.get_summary_row(uid) is None


def test_cas_loss_requeue_is_single_flight_not_a_storm(monkeypatch):
    """Two CAS-loss requeues racing for the same user (e.g. two concurrent
    `_run_compaction` attempts both losing the CAS around the same time) must
    coalesce into ONE pending maintenance job — `jobs_store.enqueue_job`'s
    per-user/lane single-flight (the same mechanism the success-path catch-up
    already relies on) — not pile up a requeue storm. Modeled directly against
    `enqueue_job` (the primitive `_run_compaction`'s CAS-loss branch calls),
    since simulating two truly concurrent `_run_compaction` racers would just
    be re-testing `enqueue_job`'s own coalesce, already covered by
    tests/test_v2_jobs_store.py — what THIS test asserts is that
    `_run_compaction`'s CAS-loss branch reuses that exact primitive (same
    lane) rather than e.g. issuing a raw INSERT that would bypass coalescing."""
    uid = "u_v2_cas_requeue_singleflight"
    conftest.seed_user(uid)
    _reset(uid)

    read_tail, read_summary, write_summary = _make_tail_deps(worker._TAIL_KEEP + 5)

    async def _fake_llm(cfg, msgs, **kw):
        return {"reply": "- folded bullet"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)
    monkeypatch.setattr(jobs_store, "upsert_summary_row_cas", lambda *a, **k: False)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=read_tail,
        read_summary=read_summary,
        write_summary=write_summary,
    )
    sem = asyncio.Semaphore(1)

    job1 = _enqueue_and_claim_maintenance(uid)
    asyncio.run(worker._run_compaction(job1["id"], uid, deps, _BYOK, sem, claimed_by=job1["claimed_by"]))

    pending = _pending_maintenance_rows(uid)
    assert len(pending) == 1, f"expected the CAS-loss requeue to land as one pending row, got {pending}"
    requeued_id = pending[0][0]
    assert pending[0][2] == "cas_lost_retry"

    # A second racer's CAS-loss requeue for the SAME user+lane (still pending,
    # not yet claimed/stale) must coalesce onto that same row, not insert a
    # second one — this is exactly what `_run_compaction`'s CAS-loss branch
    # calls (`jobs_store.enqueue_job(user_id, "maintenance", ...)`).
    jobs_store.enqueue_job(uid, "maintenance", reason="cas_lost_retry")

    pending_after = _pending_maintenance_rows(uid)
    assert len(pending_after) == 1, f"expected single-flight coalesce, got {pending_after}"
    assert pending_after[0][0] == requeued_id, "coalesce must reuse the existing pending row, not insert a new one"


def test_non_cas_failure_does_not_requeue(monkeypatch):
    """A provider/LLM error (not a CAS loss) must mark_failed WITHOUT enqueuing
    a fresh maintenance job — only CAS loss retries."""
    uid = "u_v2_cas_requeue_noncas"
    conftest.seed_user(uid)
    _reset(uid)

    read_tail, read_summary, write_summary = _make_tail_deps(worker._TAIL_KEEP + 5)

    async def _raising_llm(cfg, msgs, **kw):
        raise RuntimeError("provider blew up")

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _raising_llm)
    # CAS itself is never reached on this path — but assert it's never called,
    # to be certain this is a pre-CAS (provider) failure, not a CAS-loss one.
    def _cas_should_not_be_called(*a, **k):
        raise AssertionError("upsert_summary_row_cas must not be called on a non-CAS failure")

    monkeypatch.setattr(jobs_store, "upsert_summary_row_cas", _cas_should_not_be_called)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=read_tail,
        read_summary=read_summary,
        write_summary=write_summary,
    )

    job = _enqueue_and_claim_maintenance(uid)
    sem = asyncio.Semaphore(1)
    status = asyncio.run(worker._run_compaction(
        job["id"], uid, deps, _BYOK, sem, claimed_by=job["claimed_by"]))

    assert status == "failed"
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job["id"],),
        ).fetchone()
    assert row[0] == "failed"
    assert row[1] != "summary_cas_lost"

    pending = _pending_maintenance_rows(uid)
    assert pending == [], f"non-CAS failure must not requeue, got {pending}"


def test_cas_success_unchanged_no_double_requeue(monkeypatch):
    """Sanity: a successful CAS write on a tail under the big-batch catch-up
    threshold behaves exactly as before — completed, no maintenance requeue at
    all (this test's tail is intentionally short of `_COMPACTION_BATCH +
    _TAIL_KEEP`, so even the pre-existing success-path catch-up doesn't fire)."""
    uid = "u_v2_cas_requeue_success"
    conftest.seed_user(uid)
    _reset(uid)

    read_tail, read_summary, write_summary = _make_tail_deps(worker._TAIL_KEEP + 5)

    async def _fake_llm(cfg, msgs, **kw):
        return {"reply": "- folded bullet"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)
    # Real CAS (not monkeypatched) — expected to succeed (version 0, no racer).

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=read_tail,
        read_summary=read_summary,
        write_summary=write_summary,
    )

    job = _enqueue_and_claim_maintenance(uid)
    sem = asyncio.Semaphore(1)
    status = asyncio.run(worker._run_compaction(
        job["id"], uid, deps, _BYOK, sem, claimed_by=job["claimed_by"]))

    assert status == "completed"
    summary_row = jobs_store.get_summary_row(uid)
    assert summary_row is not None
    assert summary_row["version"] == 1

    pending = _pending_maintenance_rows(uid)
    assert pending == [], f"short tail success must not trigger any requeue, got {pending}"
