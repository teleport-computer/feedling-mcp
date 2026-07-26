from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import prompt_frontier  # noqa: E402
from model_api_runtime.v2 import compaction  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402
from model_api_runtime.v2 import worker  # noqa: E402


def _row(seq: int, role: str, content: str, *, genuine: bool | None = None) -> dict:
    row = {
        "seq": seq,
        "id": f"m{seq}",
        "ts": float(seq),
        "role": role,
        "content": content,
    }
    if genuine is not None:
        row["_genuine_user"] = genuine
    return row


def _turn(number: int, *, chars: int = 16) -> list[dict]:
    seed = number * 10
    return [
        _row(
            seed,
            "user",
            f"turn-{number}-user " + ("u" * chars),
            genuine=True,
        ),
        _row(
            seed + 1,
            "assistant",
            f"turn-{number}-assistant " + ("a" * chars),
            genuine=False,
        ),
    ]


def _limit(tokens: int) -> prompt_frontier.ModelPromptLimit:
    return prompt_frontier.ModelPromptLimit(
        provider="test",
        model=f"synthetic-{tokens}",
        context_window_tokens=tokens,
        source="deployment_override",
    )


def _plan(builder, *, tokens: int, transcript: list | None = None):
    return builder.plan_provider_round(
        transcript=list(transcript or []),
        tools=None,
        model_limit=_limit(tokens),
        output_reserve_tokens=4096,
        safety_margin_tokens=None,
        utf8_bytes_per_token=4.0,
        image_reserve_tokens=1024,
    )


def test_group_complete_turns_drops_partial_prefix_and_keeps_internal_rows():
    rows = [
        _row(1, "assistant", "orphan", genuine=False),
        _row(2, "user", "synthetic", genuine=False),
        *_turn(1),
        _row(12, "user", "internal", genuine=False),
        *_turn(2),
    ]

    groups = worker._group_complete_turns(rows)

    assert [[row["seq"] for row in group] for group in groups] == [
        [10, 11, 12],
        [20, 21],
    ]


def test_adaptive_replay_union_preserves_every_seq_through_snapshot():
    recent = [*_turn(1), *_turn(2), *_turn(3)]
    required = [_row(30, "user", "u3"), _row(31, "assistant", "a3")]
    window = {
        "rows": recent,
        "source_truncated": False,
    }

    optional, enriched_required, truncated = worker._adaptive_replay_parts(
        window,
        watermark_seq=21,
        required_tail=required,
    )

    summary_covered = {row["seq"] for row in recent if row["seq"] <= 21}
    optional_seqs = {
        row["seq"] for row in worker._flatten_turns(optional)
    }
    required_seqs = {row["seq"] for row in enriched_required}
    stored_through_snapshot = {row["seq"] for row in recent}
    assert optional_seqs <= summary_covered
    assert summary_covered | required_seqs == stored_through_snapshot
    assert truncated is False


def test_adaptive_replay_marks_row_cap_partial_prefix_as_fallback():
    partial_window = {
        "rows": [
            _row(11, "assistant", "cut oldest turn", genuine=False),
            *_turn(2),
            *_turn(3),
        ],
        "source_truncated": True,
    }

    optional, _required, truncated = worker._adaptive_replay_parts(
        partial_window,
        watermark_seq=31,
        required_tail=[],
    )

    assert [[row["seq"] for row in group] for group in optional] == [
        [20, 21],
        [30, 31],
    ]
    assert truncated is True


def test_synthetic_64k_keeps_40_turns_while_32k_shrinks_whole_turns():
    optional = [_turn(i, chars=1400) for i in range(1, 40)]
    required = _turn(40, chars=64)
    builder = worker._make_build_messages_fn(
        system_prompt="system",
        summary="summary covers turns 1 through 39",
        tail=required,
        optional_tail_turns=optional,
        tail_target_turns=40,
        tail_lane="chat",
        temporal_snapshot={
            "now_ts": 1000.0,
            "timezone": "UTC",
            "last_user_message_ts": 400.0,
        },
    )

    messages_64k, _frontier_64k, stats_64k = _plan(builder, tokens=65536)
    messages_32k, _frontier_32k, stats_32k = _plan(builder, tokens=32768)

    assert stats_64k["effective_turns"] == 40
    assert stats_64k["fallback"] is False
    assert 1 <= stats_32k["effective_turns"] < 40
    assert stats_32k["fallback"] is True
    rendered_32k = "\n".join(str(item.get("content")) for item in messages_32k)
    for number in range(1, 40):
        assert (
            f"turn-{number}-user" in rendered_32k
        ) == (
            f"turn-{number}-assistant" in rendered_32k
        )
    assert len(messages_64k) > len(messages_32k)


def test_native_transcript_growth_can_only_reduce_optional_replay():
    optional = [_turn(i, chars=1000) for i in range(1, 40)]
    builder = worker._make_build_messages_fn(
        system_prompt="system",
        summary="covered",
        tail=_turn(40, chars=64),
        optional_tail_turns=optional,
        tail_target_turns=40,
        tail_lane="chat",
    )

    _messages_before, _plan_before, before = _plan(builder, tokens=32768)
    _messages_after, _plan_after, after = _plan(
        builder,
        tokens=32768,
        transcript=[{"role": "user", "content": "x" * 60_000}],
    )

    assert after["effective_turns"] <= before["effective_turns"]
    assert after["fallback"] is True


def test_required_only_overflow_is_not_hidden_by_optional_eviction():
    builder = worker._make_build_messages_fn(
        system_prompt="system",
        summary="covered",
        tail=_turn(40, chars=60_000),
        optional_tail_turns=[_turn(1, chars=8)],
        tail_target_turns=40,
        tail_lane="chat",
    )

    with pytest.raises(prompt_frontier.PromptFrontierExhausted):
        _plan(builder, tokens=32768)


def test_targeted_catchup_compacts_exactly_through_safe_boundary(monkeypatch):
    rows = [
        _row(seq, "user" if seq % 2 else "assistant", f"row-{seq}")
        for seq in range(1, 9)
    ]
    state = {"summary": "", "version": 0, "watermark_seq": 0}
    folded: list[int] = []
    monkeypatch.setattr(worker, "_COMPACTION_BATCH", 2)
    monkeypatch.setattr(
        jobs_store,
        "get_summary_row",
        lambda _uid: {"watermark_seq": state["watermark_seq"]},
    )

    def count(_uid, after_seq, *, through_seq=None):
        return len([
            row
            for row in rows
            if row["seq"] > after_seq
            and (through_seq is None or row["seq"] <= through_seq)
        ])

    monkeypatch.setattr(worker.db, "count_messages_after_seq", count)
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: rows[-1]["seq"])

    def read_summary(_uid):
        return (
            state["summary"],
            float(state["watermark_seq"]),
            state["version"],
            state["watermark_seq"],
        )

    def read_oldest(_uid, after_seq, limit, *, through_seq=None):
        return [
            row
            for row in rows
            if row["seq"] > after_seq
            and (through_seq is None or row["seq"] <= through_seq)
        ][:limit]

    async def compact_segment(*, old_messages, **_kwargs):
        folded.extend(row["seq"] for row in old_messages)
        return "- folded " + ",".join(str(row["seq"]) for row in old_messages)

    monkeypatch.setattr(compaction, "compact_segment", compact_segment)

    def append_segment(_uid, text, **kwargs):
        assert kwargs["previous_watermark_seq"] == state["watermark_seq"]
        state["summary"] = (state["summary"] + "\n" + text).strip()
        state["watermark_seq"] = kwargs["end_seq"]
        state["version"] += 1
        return True

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "token",
        read_summary_with_seq=read_summary,
        read_compaction_tail_after_seq=read_oldest,
        read_tail_after_seq=read_oldest,
        append_summary_segment=append_segment,
    )

    watermark, snapshot = asyncio.run(
        worker._ensure_prompt_coverage(
            "u",
            deps,
            provider_config=None,
            enclave_sem=None,
            tail_limit=0,
            compact_through_seq=5,
        )
    )

    assert folded == [1, 2, 3, 4, 5]
    assert watermark == 5
    assert snapshot == 8
    assert [row["seq"] for row in rows if row["seq"] > watermark] == [6, 7, 8]
