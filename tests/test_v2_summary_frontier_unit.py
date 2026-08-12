from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

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
    candidate = frontier.choose_rollup_candidate((legacy,), max_frontier_chars=40)
    assert candidate is not None
    assert candidate.child_segment_ids == (9,)
    assert candidate.coverage_kind == "legacy_opaque"
    assert candidate.legacy_opaque_through_seq == 0


def test_rollup_candidate_ignores_historical_child_text_size():
    legacy = frontier.SummarySegment(
        segment_id=1,
        coverage_kind="legacy_opaque",
        level=0,
        start_seq=0,
        end_seq=0,
        source_message_count=0,
        legacy_opaque_through_seq=0,
        child_segment_ids=(),
        text="- " + "legacy context " * 12_000,
    )
    candidate = frontier.choose_rollup_candidate(
        (legacy,),
        max_frontier_chars=40,
    )
    assert candidate is not None
    assert candidate.child_segment_ids == (1,)
    assert candidate.source_message_count == 0


def test_fanout_rollup_ignores_provider_input_size_of_exact_children():
    items = tuple(
        _segment(index, text="- " + "historical prose " * 1_500)
        for index in range(1, 9)
    )

    candidate = frontier.choose_rollup_candidate(items, fanout=8)

    assert candidate is not None
    assert candidate.child_segment_ids == tuple(range(1, 9))


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



def test_mixed_historical_frontier_rolls_up_from_metadata_without_provider(
    monkeypatch,
):
    monkeypatch.setattr(worker, "_SUMMARY_FRONTIER_MAX_SEGMENTS", 1)
    state = {
        "snapshot": frontier.SummaryFrontierSnapshot(
            segments=(
                frontier.SummarySegment(
                    segment_id=1,
                    coverage_kind="legacy_opaque",
                    level=0,
                    start_seq=0,
                    end_seq=10,
                    source_message_count=0,
                    legacy_opaque_through_seq=10,
                    child_segment_ids=(),
                    text="- historical model-authored prose",
                ),
                frontier.SummarySegment(
                    segment_id=2,
                    coverage_kind="exact",
                    level=0,
                    start_seq=20,
                    end_seq=22,
                    source_message_count=3,
                    legacy_opaque_through_seq=0,
                    child_segment_ids=(),
                    text="- [3 条更早的消息已由长期记忆覆盖]",
                ),
            ),
            head_version=1,
            watermark_seq=22,
        ),
    }
    checkpoint_calls = []

    async def _must_not_call_provider(*_args, **_kwargs):
        raise AssertionError("conversation coverage must not use a provider")

    monkeypatch.setattr(
        provider_client,
        "reliable_chat_completion_async",
        _must_not_call_provider,
    )

    def _append_checkpoint(_user_id, summary, **kwargs):
        checkpoint_calls.append((summary, kwargs))
        assert summary == "- [更早的历史摘要及 3 条消息已由长期记忆覆盖]"
        assert kwargs["source_message_count"] == 3
        assert kwargs["child_segment_ids"] == (1, 2)
        assert kwargs["expected_version"] == 1
        assert kwargs["expected_watermark_seq"] == 22
        state["snapshot"] = frontier.SummaryFrontierSnapshot(
            segments=(
                frontier.SummarySegment(
                    segment_id=3,
                    coverage_kind="legacy_opaque",
                    level=1,
                    start_seq=0,
                    end_seq=22,
                    source_message_count=3,
                    legacy_opaque_through_seq=10,
                    child_segment_ids=(1, 2),
                    text=summary,
                ),
            ),
            head_version=2,
            watermark_seq=22,
        )
        return True

    deps = worker.TurnDeps(
        read_messages=lambda _user_id: [],
        resolve_provider=lambda _user_id: (object(), {}),
        mint_enclave_token=lambda _user_id: "token",
        read_summary_frontier_metadata=lambda _user_id: state["snapshot"],
        append_summary_checkpoint=_append_checkpoint,
    )

    result = asyncio.run(
        worker._rebalance_summary_frontier(
            "user-1",
            deps,
            enclave_sem=asyncio.Semaphore(1),
        )
    )

    assert result == list(state["snapshot"].segments)
    assert len(checkpoint_calls) == 1
