"""tail 锚点推进策略的纯单测（无 DB、无 provider）。

锚点的意义：verbatim tail 的起点 seq。它在多数回合保持不变，使 prompt 前缀
逐字节稳定、provider prompt cache 可复用；只有累积轮数越过滞后阈值时才前移
一次，把 prefix 的一次性失效换来长期稳定。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import tail_anchor


def test_no_anchor_yet_adopts_target_boundary():
    d = tail_anchor.decide_anchor(
        current_anchor=None,
        turns_after_anchor=0,
        boundary_seq_for_target=5000,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 5000
    assert d.advanced is True
    assert d.reason == "bootstrap"


def test_under_threshold_reuses_anchor_verbatim():
    """滞后区内绝不动锚点——这正是缓存命中的来源。"""
    d = tail_anchor.decide_anchor(
        current_anchor=5000,
        turns_after_anchor=59,
        boundary_seq_for_target=7000,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 5000
    assert d.advanced is False
    assert d.reason == "hysteresis_hold"


def test_crossing_threshold_advances_once_to_target():
    d = tail_anchor.decide_anchor(
        current_anchor=5000,
        turns_after_anchor=60,
        boundary_seq_for_target=7000,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 7000
    assert d.advanced is True
    assert d.reason == "threshold_advance"


def test_anchor_never_moves_backwards():
    """seq 单调；一个更旧的边界绝不能把锚点拉回去（会让 tail 变长且前缀重排）。"""
    d = tail_anchor.decide_anchor(
        current_anchor=7000,
        turns_after_anchor=99,
        boundary_seq_for_target=6000,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 7000
    assert d.advanced is False
    assert d.reason == "boundary_not_newer"


def test_missing_boundary_holds_anchor():
    """用户没有足够的真实用户轮（boundary 查不到）时保持不变，绝不清空。"""
    d = tail_anchor.decide_anchor(
        current_anchor=5000,
        turns_after_anchor=80,
        boundary_seq_for_target=None,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 5000
    assert d.advanced is False
    assert d.reason == "no_boundary"


def test_bootstrap_without_boundary_yields_no_anchor():
    d = tail_anchor.decide_anchor(
        current_anchor=None,
        turns_after_anchor=0,
        boundary_seq_for_target=None,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 0
    assert d.advanced is False
    assert d.reason == "no_boundary"


@pytest.mark.parametrize(
    "target,max_before",
    [(0, 60), (40, 39), (-1, 60), (40, 0)],
)
def test_invalid_limits_rejected(target, max_before):
    """max_turns_before_advance 必须严格大于 target_turns，否则没有滞后区，
    退化回逐轮滑动窗口（即当前 bug）。"""
    with pytest.raises(ValueError):
        tail_anchor.decide_anchor(
            current_anchor=5000,
            turns_after_anchor=1,
            boundary_seq_for_target=6000,
            target_turns=target,
            max_turns_before_advance=max_before,
        )


def test_defaults_have_a_real_hysteresis_band():
    assert (
        tail_anchor.DEFAULT_MAX_TURNS_BEFORE_ADVANCE
        > tail_anchor.DEFAULT_TARGET_TURNS
    )
