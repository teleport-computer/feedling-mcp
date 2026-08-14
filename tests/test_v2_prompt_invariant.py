"""Prompt coverage invariants for metadata-only Runtime V2 catch-up."""

from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import worker


def _store(monkeypatch, *, total: int, append_result=True):
    seqs = list(range(1, total + 1))
    state = {"watermark_seq": 0, "watermark_ts": 0.0, "version": 0}
    writes = []

    def _summary_row(_uid):
        return dict(state) if state["version"] else None

    def _count(
        _uid,
        after_seq,
        *,
        through_seq=None,
        exclude_synthetic_sources=False,
    ):
        del exclude_synthetic_sources
        return sum(
            seq > int(after_seq)
            and (through_seq is None or seq <= int(through_seq))
            for seq in seqs
        )

    def _bounds(_uid, after_seq, *, limit, through_seq=None):
        selected = [
            seq
            for seq in seqs
            if seq > int(after_seq)
            and (through_seq is None or seq <= int(through_seq))
        ][: int(limit)]
        return (
            (selected[0], selected[-1], len(selected))
            if selected
            else (0, 0, 0)
        )

    def _append(_uid, text, **kwargs):
        writes.append((text, kwargs))
        landed = append_result() if callable(append_result) else append_result
        if landed:
            state["watermark_seq"] = int(kwargs["end_seq"])
            state["version"] += 1
        return landed

    monkeypatch.setattr(jobs_store, "get_summary_row", _summary_row)
    monkeypatch.setattr(worker.db, "count_messages_after_seq", _count)
    monkeypatch.setattr(worker.db, "chat_coverage_bounds_after_seq", _bounds)
    monkeypatch.setattr(
        worker.db,
        "chat_max_seq",
        lambda _uid: seqs[-1] if seqs else 0,
    )
    return state, writes, _append


def _deps(append):
    return worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "unused",
        append_summary_segment=append,
        read_summary_frontier_metadata=lambda _uid: None,
    )


def test_inline_catchup_closes_only_the_pre_tail_gap(monkeypatch):
    monkeypatch.setattr(worker, "_COMPACTION_BATCH", 4)
    total = worker._TAIL_BUDGET + 9
    state, writes, append = _store(monkeypatch, total=total)

    watermark, max_seq = asyncio.run(
        worker._ensure_prompt_coverage(
            "u-gap",
            _deps(append),
            enclave_sem=None,
            tail_limit=worker._TAIL_BUDGET,
        )
    )

    assert watermark == 9
    assert max_seq == total
    assert state["watermark_seq"] == 9
    assert [call[1]["source_message_count"] for call in writes] == [4, 4, 1]


def test_no_gap_is_a_metadata_only_noop(monkeypatch):
    state, writes, append = _store(monkeypatch, total=3)

    assert asyncio.run(
        worker._ensure_prompt_coverage(
            "u-no-gap",
            _deps(append),
            enclave_sem=None,
            tail_limit=3,
        )
    ) == (0, 3)
    assert state["watermark_seq"] == 0
    assert writes == []


def test_source_count_mismatch_fails_without_advancing(monkeypatch):
    _state, writes, append = _store(
        monkeypatch,
        total=worker._TAIL_BUDGET + 2,
    )
    monkeypatch.setattr(
        worker.db,
        "chat_coverage_bounds_after_seq",
        lambda *_args, **_kwargs: (1, 1, 1),
    )

    with pytest.raises(worker.TurnError, match="prompt_coverage_incomplete"):
        asyncio.run(
            worker._ensure_prompt_coverage(
                "u-mismatch",
                _deps(append),
                enclave_sem=None,
                tail_limit=worker._TAIL_BUDGET,
            )
        )
    assert writes == []


def test_repeated_cas_loss_exhausts_without_inventing_coverage(monkeypatch):
    _state, writes, append = _store(
        monkeypatch,
        total=worker._TAIL_BUDGET + 1,
        append_result=False,
    )

    with pytest.raises(worker.TurnError, match="prompt_coverage_incomplete"):
        asyncio.run(
            worker._ensure_prompt_coverage(
                "u-cas-loss",
                _deps(append),
                enclave_sem=None,
                tail_limit=worker._TAIL_BUDGET,
                max_retries=2,
            )
        )
    assert len(writes) == 2


def test_lease_loss_blocks_summary_write(monkeypatch):
    _state, writes, append = _store(
        monkeypatch,
        total=worker._TAIL_BUDGET + 1,
    )
    monkeypatch.setattr(jobs_store, "renew_job_lease", lambda *_a, **_k: False)

    with pytest.raises(worker.LostJobLease):
        asyncio.run(
            worker._ensure_prompt_coverage(
                "u-lease",
                _deps(append),
                enclave_sem=None,
                tail_limit=worker._TAIL_BUDGET,
                job_id=7,
                claimed_by="worker-a",
            )
        )
    assert writes == []


def test_runtime_mode_change_blocks_summary_write(monkeypatch):
    _state, writes, append = _store(
        monkeypatch,
        total=worker._TAIL_BUDGET + 1,
    )
    deps = _deps(append)
    deps.runtime_mode_enabled = lambda _uid: False

    with pytest.raises(worker.RuntimeModeChanged):
        asyncio.run(
            worker._ensure_prompt_coverage(
                "u-mode",
                deps,
                enclave_sem=None,
                tail_limit=worker._TAIL_BUDGET,
            )
        )
    assert writes == []


@pytest.mark.parametrize(
    ("count", "limit", "expected"),
    [(0, 50, False), (50, 50, False), (51, 50, True)],
)
def test_gap_from_count_pure_boundary_cases(count, limit, expected):
    assert worker._gap_from_count(count, limit) is expected


def test_coverage_incomplete_has_stable_failure_classification():
    exc = worker.TurnError("prompt_coverage_incomplete")
    assert worker._turn_failure_error_class(exc) == "unknown"
    assert worker._safe_failure_code("turn_failed", exc) == (
        "turn_failed:prompt_coverage_incomplete"
    )
