"""叫醒判据 —— 纯函数，不碰 DB、不碰时钟。

★ 语义：should_wake 回答的是「值不值得戳一下 agent」，
  不是「该不该说话」。返回值里不许出现任何跟「说什么」有关的东西。

★ 用词：内核这套叫 PERCEPTION_WAKE_SOURCES（感知叫醒源），刻意不叫 wake_kind
  —— io 里 proactive/gate.py 和 model_api_runtime/v2/effect_outbox.py 各有一套
  含义不同的 wake_kind，三者不可互传。详见 perception_kernel/wake.py 的注释。
"""
from __future__ import annotations

import pathlib
import sys

# Self-contained sys.path bootstrap (mirrors tests/test_perception_kernel_catalog.py):
# conftest.py only adds backend/ to sys.path inside its DB-provisioning try-block,
# so on a no-Postgres machine this file must add backend/ itself.
_BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import perception_kernel.wake as wake


def test_disabled_source_never_wakes():
    ok, reason = wake.should_wake(
        "photo", enabled_sources=("arrival",), last_wake_ts=0.0, now=1000.0, debounce_sec=60.0
    )
    assert ok is False
    assert reason == "source_disabled"


def test_debounce_blocks_a_second_wake_inside_the_window():
    ok, reason = wake.should_wake(
        "arrival", enabled_sources=("arrival",), last_wake_ts=1000.0, now=1030.0, debounce_sec=60.0
    )
    assert ok is False
    assert reason == "debounced"


def test_wake_passes_outside_the_debounce_window():
    ok, reason = wake.should_wake(
        "arrival", enabled_sources=("arrival",), last_wake_ts=1000.0, now=1100.0, debounce_sec=60.0
    )
    assert ok is True
    assert reason == "arrival"


def test_first_ever_wake_has_no_previous_timestamp():
    ok, _ = wake.should_wake(
        "unlock", enabled_sources=wake.PERCEPTION_WAKE_SOURCES, last_wake_ts=None, now=1.0,
        debounce_sec=60.0
    )
    assert ok is True


def test_motion_is_not_a_significant_change():
    # 基线语义：motion 变得太频繁，故意不作为叫醒源。
    assert wake.is_significant_change("motion_state", "still", "walking") is False


def test_place_label_change_is_significant():
    assert wake.is_significant_change("location_signal", "home", "office") is True


def test_same_value_is_never_significant():
    assert wake.is_significant_change("location_signal", "office", "office") is False


def test_kernel_vocabulary_does_not_collide_with_the_two_io_wake_kind_sets():
    """内核这套叫醒源，和 io 里两套同名不同义的 wake_kind 刻意保持区分。

    gate.py 的是「走哪条投递通道」，effect_outbox.py 的是「哪几类要防撞」，
    内核这套是「被什么感知到的」。三者不可互传，名字也不许再撞。
    """
    gate_kinds = {"screen_watch", "screen", "presence"}
    outbox_kinds = {"heartbeat", "manual_wake", "screen_watch"}
    ours = set(wake.PERCEPTION_WAKE_SOURCES)
    assert not hasattr(wake, "WAKE_KINDS"), "别再引入 WAKE_KINDS 这个名字"
    assert ours != gate_kinds and ours != outbox_kinds
    # 唯一的重叠词，含义不同，保留但不代表可互换
    assert ours & gate_kinds == {"screen_watch"}
    assert ours & outbox_kinds == {"screen_watch"}
