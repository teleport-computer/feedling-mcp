"""叫醒判据 —— 纯函数，不碰 DB、不碰时钟。

★ 语义：should_wake 回答的是「值不值得戳一下 agent」，
  不是「该不该说话」。返回值里不许出现任何跟「说什么」有关的东西。
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


def test_disabled_kind_never_wakes():
    ok, reason = wake.should_wake(
        "photo", enabled_kinds=("arrival",), last_wake_ts=0.0, now=1000.0, debounce_sec=60.0
    )
    assert ok is False
    assert reason == "kind_disabled"


def test_debounce_blocks_a_second_wake_inside_the_window():
    ok, reason = wake.should_wake(
        "arrival", enabled_kinds=("arrival",), last_wake_ts=1000.0, now=1030.0, debounce_sec=60.0
    )
    assert ok is False
    assert reason == "debounced"


def test_wake_passes_outside_the_debounce_window():
    ok, reason = wake.should_wake(
        "arrival", enabled_kinds=("arrival",), last_wake_ts=1000.0, now=1100.0, debounce_sec=60.0
    )
    assert ok is True
    assert reason == "arrival"


def test_first_ever_wake_has_no_previous_timestamp():
    ok, _ = wake.should_wake(
        "unlock", enabled_kinds=wake.WAKE_KINDS, last_wake_ts=None, now=1.0, debounce_sec=60.0
    )
    assert ok is True


def test_motion_is_not_a_significant_change():
    # 基线语义：motion 变得太频繁，故意不作为叫醒源。
    assert wake.is_significant_change("motion_state", "still", "walking") is False


def test_place_label_change_is_significant():
    assert wake.is_significant_change("location_signal", "home", "office") is True


def test_same_value_is_never_significant():
    assert wake.is_significant_change("location_signal", "office", "office") is False
