from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import prompt_frontier  # noqa: E402
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


def test_synthetic_64k_keeps_40_turns_while_32k_shrinks_whole_turns():
    optional = [_turn(i, chars=1400) for i in range(1, 40)]
    required = _turn(40, chars=64)
    builder = worker._make_build_messages_fn(
        system_prompt="system",
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
        tail=_turn(40, chars=60_000),
        optional_tail_turns=[_turn(1, chars=8)],
        tail_target_turns=40,
        tail_lane="chat",
    )

    with pytest.raises(prompt_frontier.PromptFrontierExhausted):
        _plan(builder, tokens=32768)


def test_adaptive_frontier_truncates_worldbook_with_explicit_marker():
    worldbook = "<world_book>\n" + ("world detail " * 3_000) + "\n</world_book>"
    builder = worker._make_build_messages_fn(
        system_prompt="system",
        tail=_turn(40, chars=64),
        optional_tail_turns=[_turn(1, chars=400)],
        tail_target_turns=2,
        tail_lane="chat",
        worldbook_context=worldbook,
    )

    messages, _frontier, stats = _plan(builder, tokens=8192)
    rendered = "\n".join(str(item.get("content")) for item in messages)

    assert stats["worldbook_truncated"] is True
    assert "WORLD BOOK CONTEXT TRUNCATED TO FIT THE PROMPT BUDGET" in rendered
    assert "turn-40-user" in rendered
    assert "turn-40-assistant" in rendered
