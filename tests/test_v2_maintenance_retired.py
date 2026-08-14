"""Rolling-deploy tombstone for legacy Runtime V2 maintenance jobs."""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

import conftest
import db
from model_api_runtime.v2 import jobs_store, worker


def _claimed_maintenance(uid: str, owner: str = "retire-worker") -> dict:
    conftest.seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_turn_metrics WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM agent_jobs")
    conftest.set_v2_runtime_owner(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "maintenance", reason="legacy_compact")
    job = jobs_store.claim_next_job(owner, lanes={"maintenance"})
    assert job is not None and job["id"] == job_id
    return job


def test_claimed_maintenance_is_content_free_tombstone(monkeypatch):
    uid = "u_maintenance_retired"
    job = _claimed_maintenance(uid)

    def prohibited(*args, **kwargs):
        raise AssertionError("retired maintenance touched protected dependency")

    monkeypatch.setattr(jobs_store, "enqueue_job", prohibited)
    monkeypatch.setattr(jobs_store, "append_trajectory_event", prohibited)
    monkeypatch.setattr(jobs_store, "append_trajectory_events_batch", prohibited)
    deps = worker.TurnDeps(
        read_messages=prohibited,
        resolve_provider=prohibited,
        mint_enclave_token=prohibited,
        read_recent_turns=prohibited,
        read_compaction_tail_after_seq=prohibited,
        append_summary_segment=prohibited,
        seal_trajectory_payload=prohibited,
        runtime_mode_enabled=prohibited,
    )

    assert asyncio.run(worker._run_turn(job, deps)) == "completed"

    with db.get_pool().connection() as conn:
        status = conn.execute(
            "SELECT status FROM agent_jobs WHERE id=%s", (job["id"],)
        ).fetchone()
        metric = conn.execute(
            "SELECT failed,status,model_calls,prompt_tokens,completion_tokens "
            "FROM v2_turn_metrics WHERE job_id=%s",
            (job["id"],),
        ).fetchone()
        successors = conn.execute(
            "SELECT count(*) FROM agent_jobs WHERE user_id=%s AND id<>%s",
            (uid, job["id"]),
        ).fetchone()
    assert status == ("completed",)
    assert metric == (False, "maintenance_retired", 0, None, None)
    assert successors == (0,)


def test_maintenance_tombstone_respects_lease_loss(monkeypatch):
    job = _claimed_maintenance("u_maintenance_retired_lease")
    flushed = []

    monkeypatch.setattr(jobs_store, "mark_completed", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        worker.TurnMetrics,
        "flush",
        lambda self, *, failed, status: flushed.append((failed, status)),
    )

    def prohibited(*args, **kwargs):
        raise AssertionError("lease-lost tombstone touched protected dependency")

    deps = worker.TurnDeps(
        read_messages=prohibited,
        resolve_provider=prohibited,
        mint_enclave_token=prohibited,
        append_summary_segment=prohibited,
    )
    assert asyncio.run(worker._run_turn(job, deps)) == "failed"
    assert flushed == [(True, "lease_lost")]
