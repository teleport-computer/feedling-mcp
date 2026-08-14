"""Stale slot owners must be rejected at irreversible Job boundaries."""

from __future__ import annotations

import os

import pytest

import db
from conftest import seed_user
from model_api_runtime.v2 import jobs_store, worker

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires PostgreSQL"
)


def _claimed_chat(user_id: str, owner: str) -> int:
    seed_user(user_id)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'v2',1) ON CONFLICT (user_id) DO UPDATE SET "
            "hosted_runtime_state='v2',runtime_generation=1",
            (user_id,),
        )
    job_id, _ = jobs_store.enqueue_job(user_id, "chat")
    claimed = jobs_store.claim_next_job(owner, lanes={"chat"})
    assert claimed is not None and int(claimed["id"]) == job_id
    return job_id


def test_replaced_owner_cannot_start_renew_or_finish_chat_job():
    owner = "foreground-0:g7"
    job_id = _claimed_chat("u_stale_owner_fence", owner)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET claimed_by='foreground-0:g8' WHERE id=%s",
            (job_id,),
        )

    assert jobs_store.mark_running(job_id, claimed_by=owner) is False
    assert jobs_store.renew_job_lease(job_id, owner) is False
    assert jobs_store.finish_chat_job(
        job_id,
        claimed_by=owner,
        observed_generation=0,
    )[0] is False


def test_timeout_failures_map_to_stable_classes_without_exception_text():
    assert jobs_store._terminal_error_class("queue_timeout") == "platform_queue_timeout"
    assert jobs_store._terminal_error_class("slot_watchdog_timeout") == (
        "platform_execution_timeout"
    )
    assert jobs_store._terminal_error_class("provider_transport_timeout") == (
        "provider_timeout"
    )
    assert worker._turn_failure_error_class(TimeoutError("secret relay detail")) == (
        "provider_timeout"
    )
