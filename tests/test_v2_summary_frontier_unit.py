from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import compaction
from model_api_runtime.v2 import summary_frontier as frontier
from model_api_runtime.v2 import worker
import provider_client


def _segment(
    segment_id: int,
    *,
    level: int = 0,
    start: int | None = None,
    end: int | None = None,
    count: int = 1,
    text: str | None = None,
    children: tuple[int, ...] = (),
) -> frontier.SummarySegment:
    start = segment_id * 10 if start is None else start
    end = start if end is None else end
    return frontier.SummarySegment(
        segment_id=segment_id,
        coverage_kind="exact",
        level=level,
        start_seq=start,
        end_seq=end,
        source_message_count=count,
        legacy_opaque_through_seq=0,
        child_segment_ids=children,
        text=text or f"- segment {segment_id}",
    )


def test_exact_frontier_proves_order_boundaries_and_source_count():
    items = (_segment(1, start=10, end=12, count=3),
             _segment(2, start=20, end=21, count=2))
    assert frontier.validate_canonical_frontier(
        items,
        watermark_seq=21,
        first_source_seq=10,
        covered_source_count=5,
    ) == items
    with pytest.raises(frontier.SummaryFrontierIntegrityError):
        frontier.validate_canonical_frontier(
            items,
            watermark_seq=21,
            first_source_seq=10,
            covered_source_count=4,
        )


def test_zero_raw_legacy_opaque_frontier_is_valid_and_can_roll_up_unary():
    legacy = frontier.SummarySegment(
        segment_id=9,
        coverage_kind="legacy_opaque",
        level=0,
        start_seq=0,
        end_seq=0,
        source_message_count=0,
        legacy_opaque_through_seq=0,
        child_segment_ids=(),
        text="- " + "x" * 80,
    )
    assert frontier.validate_canonical_frontier(
        (legacy,), watermark_seq=0, first_source_seq=0, covered_source_count=0
    ) == (legacy,)
    candidate = frontier.choose_rollup_candidate(
        (legacy,), max_frontier_chars=40, max_rollup_input_chars=200
    )
    assert candidate is not None
    assert candidate.child_segment_ids == (9,)
    assert candidate.coverage_kind == "legacy_opaque"
    assert candidate.legacy_opaque_through_seq == 0


def test_rollup_prefers_closed_same_level_fanout_and_replaces_exact_run():
    items = tuple(_segment(index) for index in range(1, 9))
    candidate = frontier.choose_rollup_candidate(items, fanout=8)
    assert candidate is not None
    assert candidate.reason == "fanout"
    assert candidate.child_segment_ids == tuple(range(1, 9))
    rendered = frontier.render_replacement(
        items,
        child_segment_ids=candidate.child_segment_ids,
        parent_text="- parent",
    )
    assert rendered == "- parent"


def test_bounded_frontier_is_not_rewritten_without_a_closed_run():
    items = tuple(_segment(index) for index in range(1, 5))
    assert frontier.choose_rollup_candidate(items, fanout=8) is None


def test_compact_segment_never_resends_an_existing_aggregate_summary():
    seen = []

    async def _llm(_config, messages, **_kwargs):
        seen.extend(messages)
        return {"reply": "- covered new batch"}

    # Above the verbatim-fold threshold, so this exercises the provider path
    # whose prompt shape is what this test is about.
    result = asyncio.run(
        compaction.compact_segment(
            provider_config=object(),
            old_messages=[
                {"role": "user", "content": "NEW_BATCH_ONLY " + "x" * 500}
                for _ in range(compaction._VERBATIM_FOLD_MAX_CHARS // 500 + 2)
            ],
            llm=_llm,
        )
    )
    assert result == "- covered new batch"
    assert "NEW_BATCH_ONLY" in str(seen)
    assert "现有摘要" not in str(seen)


def test_compact_checkpoint_rejects_malformed_output_as_full_noop():
    async def _llm(_config, _messages, **_kwargs):
        return {"reply": "prose before bullet\n- child"}

    result = asyncio.run(
        compaction.compact_checkpoint(
            provider_config=object(),
            child_summaries=["- child one", "- child two"],
            llm=_llm,
        )
    )
    assert result is None


def test_oversized_legacy_summary_rolls_up_without_new_messages(monkeypatch):
    """Lazy-seeded legacy heads must not need a new chat row to become bounded."""

    # Strictly beyond one 120K checkpoint request: this must map/reduce rather
    # than require a new message or fail at the old aggregate's size.
    oversized = "- " + ("legacy context " * 12_000)
    bounded = "- bounded legacy checkpoint"
    state = {
        "snapshot": frontier.SummaryFrontierSnapshot(
            segments=(
                frontier.SummarySegment(
                    segment_id=1,
                    coverage_kind="legacy_opaque",
                    level=0,
                    start_seq=0,
                    end_seq=0,
                    source_message_count=0,
                    legacy_opaque_through_seq=0,
                    child_segment_ids=(),
                    text=oversized,
                ),
            ),
            head_version=1,
            watermark_seq=0,
        ),
    }
    checkpoint_calls = []
    provider_calls = []

    async def _llm(_config, messages, **_kwargs):
        provider_calls.append(messages)
        return {"reply": bounded, "usage": {"prompt_tokens": 10}}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _llm)

    def _append_checkpoint(_user_id, summary, **kwargs):
        checkpoint_calls.append((summary, kwargs))
        assert kwargs["child_segment_ids"] == (1,)
        assert kwargs["expected_version"] == 1
        assert kwargs["expected_watermark_seq"] == 0
        state["snapshot"] = frontier.SummaryFrontierSnapshot(
            segments=(
                frontier.SummarySegment(
                    segment_id=2,
                    coverage_kind="legacy_opaque",
                    level=1,
                    start_seq=0,
                    end_seq=0,
                    source_message_count=0,
                    legacy_opaque_through_seq=0,
                    child_segment_ids=(1,),
                    text=summary,
                ),
            ),
            head_version=2,
            watermark_seq=0,
        )
        return True

    deps = worker.TurnDeps(
        read_messages=lambda _user_id: [],
        resolve_provider=lambda _user_id: (object(), {}),
        mint_enclave_token=lambda _user_id: "token",
        read_summary_frontier=lambda _user_id: state["snapshot"],
        append_summary_checkpoint=_append_checkpoint,
        read_summary_with_seq=lambda _user_id: (bounded, 0.0, 2, 0),
    )

    result = asyncio.run(
        worker._bound_materialized_summary(
            "user-1",
            oversized,
            deps,
            provider_config=object(),
            enclave_sem=asyncio.Semaphore(1),
            claimed_by=None,
            job_id=None,
            add_usage=None,
            trajectory_recorder=None,
        )
    )

    assert result == bounded
    assert len(checkpoint_calls) == 1
    assert len(provider_calls) >= 3
    assert all(
        len(str(call[1]["content"])) < len(oversized)
        for call in provider_calls
    )
