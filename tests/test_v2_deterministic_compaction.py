from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from model_api_runtime.v2 import compaction
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker


def _install_metadata_store(monkeypatch, *, source_count: int):
    seqs = list(range(1, source_count + 1))
    state = {
        "watermark_seq": 0,
        "watermark_ts": 0.0,
        "version": 0,
    }
    writes: list[dict] = []

    def _summary_row(_user_id):
        if state["version"] == 0:
            return None
        return dict(state)

    def _count(
        _user_id,
        after_seq,
        *,
        through_seq=None,
        exclude_synthetic_sources=False,
    ):
        del exclude_synthetic_sources
        return len(
            [
                seq
                for seq in seqs
                if seq > int(after_seq)
                and (through_seq is None or seq <= int(through_seq))
            ]
        )

    def _bounds(
        _user_id,
        after_seq,
        *,
        limit,
        through_seq=None,
    ):
        selected = [
            seq
            for seq in seqs
            if seq > int(after_seq)
            and (through_seq is None or seq <= int(through_seq))
        ][: int(limit)]
        if not selected:
            return 0, 0, 0
        return selected[0], selected[-1], len(selected)

    def _append(_user_id, segment_text, **kwargs):
        assert kwargs["previous_watermark_seq"] == state["watermark_seq"]
        assert kwargs["expected_version"] == state["version"]
        writes.append({"segment_text": segment_text, **kwargs})
        state["watermark_seq"] = int(kwargs["end_seq"])
        state["version"] += 1
        state["watermark_ts"] = float(kwargs["watermark_ts"])
        return True

    monkeypatch.setattr(worker, "_PROFILE_COVERAGE_DETERMINISTIC", True)
    monkeypatch.setattr(jobs_store, "get_summary_row", _summary_row)
    monkeypatch.setattr(worker.db, "count_messages_after_seq", _count)
    monkeypatch.setattr(worker.db, "chat_coverage_bounds_after_seq", _bounds)
    monkeypatch.setattr(
        worker.db,
        "chat_max_seq",
        lambda _user_id: seqs[-1] if seqs else 0,
    )
    monkeypatch.setattr(worker.db, "v2_effective_batch_cap", lambda _user_id: None)
    return state, writes, _append


def test_maintenance_deterministic_path_never_reads_or_calls_model(monkeypatch):
    state, writes, append = _install_metadata_store(
        monkeypatch,
        source_count=worker._TAIL_KEEP + 17,
    )
    completed = []
    monkeypatch.setattr(
        jobs_store,
        "mark_completed",
        lambda job_id, *, claimed_by=None: completed.append(
            (job_id, claimed_by)
        )
        or True,
    )
    monkeypatch.setattr(
        jobs_store,
        "renew_job_lease",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        jobs_store,
        "mark_failed",
        lambda *_args, **_kwargs: False,
    )

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("plaintext/provider path must stay unreachable")

    deps = worker.TurnDeps(
        read_messages=lambda _user_id: [],
        resolve_provider=lambda _user_id: (object(), {}),
        mint_enclave_token=lambda _user_id: "runtime-token",
        append_summary_segment=append,
        read_summary=_must_not_run,
        read_summary_with_seq=_must_not_run,
        read_compaction_tail=_must_not_run,
        read_compaction_tail_after_seq=_must_not_run,
        read_summary_frontier_metadata=lambda _user_id: None,
        runtime_mode_enabled=lambda _user_id: True,
    )

    status = asyncio.run(
        worker._run_compaction(
            41,
            "u-deterministic-maintenance",
            deps,
            object(),
            asyncio.Semaphore(1),
            claimed_by="worker-a",
        )
    )

    assert status == "completed"
    assert state["watermark_seq"] == 17
    assert completed == [(41, "worker-a")]
    assert len(writes) == 1
    assert writes[0]["source_message_count"] == 17
    assert writes[0]["segment_text"] == compaction.deterministic_fold(
        source_message_count=17
    )
    assert writes[0]["head_summary"] == writes[0]["segment_text"]


def test_deterministic_maintenance_cas_loss_requeues_fresh_metadata(monkeypatch):
    _state, _writes, _append = _install_metadata_store(
        monkeypatch,
        source_count=worker._TAIL_KEEP + 1,
    )
    requeued = []
    notified = []
    monkeypatch.setattr(
        jobs_store,
        "renew_job_lease",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        jobs_store,
        "mark_failed",
        lambda job_id, code, *, claimed_by=None: (
            job_id,
            code,
            claimed_by,
        )
        == (42, "summary_cas_lost", "worker-b"),
    )
    monkeypatch.setattr(
        jobs_store,
        "enqueue_job",
        lambda user_id, lane, *, reason: requeued.append(
            (user_id, lane, reason)
        ),
    )
    monkeypatch.setattr(
        worker.core_wake_bus,
        "notify",
        lambda channel, user_id: notified.append((channel, user_id)),
    )
    deps = worker.TurnDeps(
        read_messages=lambda _user_id: [],
        resolve_provider=lambda _user_id: (object(), {}),
        mint_enclave_token=lambda _user_id: "runtime-token",
        append_summary_segment=lambda *_args, **_kwargs: False,
        runtime_mode_enabled=lambda _user_id: True,
    )

    status = asyncio.run(
        worker._run_compaction(
            42,
            "u-deterministic-cas",
            deps,
            object(),
            asyncio.Semaphore(1),
            claimed_by="worker-b",
        )
    )

    assert status == "failed"
    assert requeued == [
        (
            "u-deterministic-cas",
            "maintenance",
            "cas_lost_retry",
        )
    ]
    assert notified == [("v2_jobs", "u-deterministic-cas")]


def test_inline_wake_catchup_drains_large_gap_without_model_or_decrypt(
    monkeypatch,
):
    total = worker._COMPACTION_BATCH + 73
    state, writes, append = _install_metadata_store(
        monkeypatch,
        source_count=total,
    )

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("plaintext/provider path must stay unreachable")

    deps = worker.TurnDeps(
        read_messages=lambda _user_id: [],
        resolve_provider=lambda _user_id: (object(), {}),
        mint_enclave_token=lambda _user_id: "runtime-token",
        append_summary_segment=append,
        read_summary=_must_not_run,
        read_summary_with_seq=_must_not_run,
        read_compaction_tail=_must_not_run,
        read_compaction_tail_after_seq=_must_not_run,
        read_summary_frontier_metadata=lambda _user_id: None,
    )
    usage = []

    watermark, max_seq = asyncio.run(
        worker._ensure_prompt_coverage(
            "u-deterministic-wake",
            deps,
            provider_config=object(),
            enclave_sem=asyncio.Semaphore(1),
            tail_limit=worker._TAIL_BUDGET,
            add_usage=usage.append,
        )
    )

    assert watermark == total - worker._TAIL_BUDGET
    assert max_seq == total
    assert state["watermark_seq"] == watermark
    assert [write["source_message_count"] for write in writes] == [
        worker._COMPACTION_BATCH,
        total - worker._TAIL_BUDGET - worker._COMPACTION_BATCH,
    ]
    assert usage == []


def test_metadata_frontier_reconstructs_exact_text_without_decrypt(monkeypatch):
    state = {
        "version": 7,
        "watermark_seq": 22,
        "watermark_ts": 0.0,
        "summary_envelope": {"ciphertext": "head"},
        "has_segment_rows": True,
        "materialized_segment_ids": (4, 5),
        "first_source_seq": 10,
        "covered_source_count": 5,
        "segments": [
            {
                "segment_id": 4,
                "coverage_kind": "exact",
                "level": 0,
                "start_seq": 10,
                "end_seq": 14,
                "source_message_count": 2,
                "legacy_opaque_through_seq": 0,
                "child_segment_ids": (),
                "summary_envelope": {"ciphertext": "leaf-a"},
            },
            {
                "segment_id": 5,
                "coverage_kind": "exact",
                "level": 0,
                "start_seq": 18,
                "end_seq": 22,
                "source_message_count": 3,
                "legacy_opaque_through_seq": 0,
                "child_segment_ids": (),
                "summary_envelope": {"ciphertext": "leaf-b"},
            },
        ],
    }
    monkeypatch.setattr(
        jobs_store,
        "get_summary_frontier_state",
        lambda _user_id: state,
    )
    monkeypatch.setattr(
        serve_worker,
        "_read_summary_frontier",
        lambda _user_id: (_ for _ in ()).throw(
            AssertionError("exact metadata frontier must not decrypt")
        ),
    )

    snapshot = serve_worker._read_summary_frontier_metadata("u-frontier")

    assert snapshot is not None
    assert snapshot.head_version == 7
    assert [item.text for item in snapshot.segments] == [
        compaction.deterministic_fold(source_message_count=2),
        compaction.deterministic_fold(source_message_count=3),
    ]


def test_phala_worker_composes_wire_deterministic_flag_default_off():
    setting = (
        'FEEDLING_V2_PROFILE_COVERAGE_DETERMINISTIC: '
        '"${FEEDLING_V2_PROFILE_COVERAGE_DETERMINISTIC:-0}"'
    )
    for name in (
        "docker-compose.phala.yaml",
        "docker-compose.phala.test.yaml",
        "docker-compose.phala.pre.yaml",
    ):
        text = (ROOT / "deploy" / name).read_text()
        assert text.count(setting) == 1, name
