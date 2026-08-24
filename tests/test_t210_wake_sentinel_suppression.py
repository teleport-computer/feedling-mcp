"""T210 regression: provider end sentinels never become proactive bubbles."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
from core import tool_markup_leak
from model_api_runtime.v2 import jobs_store, worker
from test_v2_wake_worker import (
    _BYOK,
    _claim,
    _reset,
    _script_provider,
    _text_round,
    _wake_deps,
)


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def test_end_sentinel_heartbeat_is_not_delivered_and_is_measured(monkeypatch):
    uid = "u_t210_wake_sentinel"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _script_provider(monkeypatch, [_text_round("</s>")])
    delivered: list[str] = []
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda _store, text, **_kwargs: delivered.append(str(text)),
    )

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "heartbeat",
            _wake_deps(
                tail=[
                    {"id": "m1", "ts": 1.0, "role": "user", "content": "在吗"}
                ]
            ),
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "completed"
    assert delivered == []
    with db.get_pool().connection() as conn:
        outcome = conn.execute(
            "SELECT status,last_error,wake_result,wake_result_reason "
            "FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
        effect_count = conn.execute(
            "SELECT count(*) FROM v2_effect_outbox WHERE job_id=%s",
            (job_id,),
        ).fetchone()[0]
    assert outcome == (
        "completed",
        None,
        "sleep",
        jobs_store.EMPTY_VISIBLE_REPLY_SUPPRESSED_REASON,
    )
    assert effect_count == 0

    lanes = {
        row["lane"]: row for row in jobs_store.recent_runtime_health()["lanes"]
    }
    assert lanes["heartbeat"]["empty_reply_suppressions"] == 1

    # Mutation proof: the metric is keyed to the controlled suppression reason,
    # not to every ordinary weak-wake sleep.
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET wake_result_reason='ordinary_sleep' WHERE id=%s",
            (job_id,),
        )
    mutated = {
        row["lane"]: row for row in jobs_store.recent_runtime_health()["lanes"]
    }
    assert mutated["heartbeat"]["empty_reply_suppressions"] == 0


def test_scheduled_end_sentinel_keeps_must_deliver_failure(monkeypatch):
    uid = "u_t210_scheduled_sentinel"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed_by = _claim(job_id)
    _script_provider(monkeypatch, [_text_round("<|im_end|>")])
    delivered: list[str] = []
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda _store, text, **_kwargs: delivered.append(str(text)),
    )

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "scheduled",
            _wake_deps(
                tail=[
                    {"id": "m1", "ts": 1.0, "role": "user", "content": "提醒我"}
                ]
            ),
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "rescheduled"
    assert delivered == []
    with db.get_pool().connection() as conn:
        outcome = conn.execute(
            "SELECT status,last_error,wake_result,wake_result_reason "
            "FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert outcome == (
        "pending",
        "scheduled_retry:wake_failed:degenerate_reply_suppressed",
        None,
        None,
    )


@pytest.mark.parametrize("sentinel", tool_markup_leak.MODEL_SENTINEL_TOKENS)
def test_chat_and_wake_share_the_sentinel_predicate(sentinel):
    assert worker._is_degenerate_reply is tool_markup_leak.is_degenerate_visible_text
    assert worker._is_degenerate_reply(sentinel) is True
    assert worker._is_degenerate_reply(f"正常正文 {sentinel}") is False
