"""CAS retry behavior for metadata-only maintenance coverage."""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import worker


def _install_one_row(monkeypatch):
    monkeypatch.setattr(jobs_store, "get_summary_row", lambda _uid: None)

    async def _count(_uid, watermark_seq, **_kwargs):
        return worker._TAIL_KEEP + 1 if watermark_seq == 0 else 0

    monkeypatch.setattr(worker, "_unsummarized_count", _count)
    monkeypatch.setattr(
        worker.db,
        "chat_coverage_bounds_after_seq",
        lambda *_args, **_kwargs: (1, 1, 1),
    )
    monkeypatch.setattr(
        worker.db,
        "count_messages_after_seq",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(jobs_store, "renew_job_lease", lambda *_a, **_k: True)


def test_cas_loss_requeues_fresh_maintenance_job(monkeypatch):
    _install_one_row(monkeypatch)
    requeued = []
    notified = []
    monkeypatch.setattr(
        jobs_store,
        "mark_failed",
        lambda job_id, code, *, claimed_by=None: (
            job_id,
            code,
            claimed_by,
        ) == (11, "summary_cas_lost", "worker-a"),
    )
    monkeypatch.setattr(
        jobs_store,
        "enqueue_job",
        lambda uid, lane, *, reason: requeued.append((uid, lane, reason)),
    )
    monkeypatch.setattr(
        worker.core_wake_bus,
        "notify",
        lambda channel, uid: notified.append((channel, uid)),
    )
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "unused",
        append_summary_segment=lambda *_args, **_kwargs: False,
        runtime_mode_enabled=lambda _uid: True,
    )

    status = asyncio.run(
        worker._run_compaction(
            11,
            "u-cas",
            deps,
            asyncio.Semaphore(1),
            claimed_by="worker-a",
        )
    )

    assert status == "failed"
    assert requeued == [("u-cas", "maintenance", "cas_lost_retry")]
    assert notified == [("v2_jobs", "u-cas")]


def test_stale_cas_loser_does_not_requeue(monkeypatch):
    _install_one_row(monkeypatch)
    requeued = []
    monkeypatch.setattr(jobs_store, "mark_failed", lambda *_a, **_k: False)
    monkeypatch.setattr(
        jobs_store,
        "enqueue_job",
        lambda *args, **kwargs: requeued.append((args, kwargs)),
    )
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "unused",
        append_summary_segment=lambda *_args, **_kwargs: False,
        runtime_mode_enabled=lambda _uid: True,
    )

    assert asyncio.run(
        worker._run_compaction(
            12,
            "u-stale",
            deps,
            asyncio.Semaphore(1),
            claimed_by="worker-b",
        )
    ) == "failed"
    assert requeued == []
