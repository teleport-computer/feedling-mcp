"""落卡队头阻塞的逃生阀。

## 背景（2026-09-12 prod 实测）

落卡只在**成功**时推进游标。一条让模型吐出非法 JSON 的消息（实测形态：模型
引用用户原话时，把半角双引号写进 JSON 字符串却不转义）会造成队头阻塞：

    解析失败 → 游标不动 → 下次还从同一条消息开始 → 又失败 → 永远

prod 上 152 个有落卡活动的用户里，**58 个处于这个状态**（连续两天 0 成功）。
用户的感受是"io 不再记东西了，做梦也不整理了"——后者是因为 capture 不产新卡，
dream 判定 seed card 不够。

而每一步都"正常失败"：有退避、有错误码、没有告警。

## 契约

- 同一窗口连续失败到 ``CAPTURE_POISON_SKIP_AFTER`` 次 → 推进游标跳过它
- **换了窗口 streak 从 1 重数** —— 三次互不相干的偶发失败不该触发跳过
- 拿不到窗口终点时**不跳** —— 宁可继续卡着，也不要把游标推到说不清的位置
- 跳过必须留痕（那一批记忆永久丢了，不能静默）
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FEEDLING_DATA_DIR",
                      tempfile.mkdtemp(prefix="feedling-poison-test-"))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from proactive import capture_scheduler as cs  # noqa: E402

W1 = {"after_message_id": "msg_a", "until_message_id": "msg_c",
      "through_seq": 120, "until_ts": 1000.0, "message_count": 8}
W2 = {"after_message_id": "msg_c", "until_message_id": "msg_f",
      "through_seq": 140, "until_ts": 2000.0, "message_count": 5}


def _fail_once(state: dict, window: dict | None) -> dict:
    """走生产用的那个共用函数（V1/V2 两条线都调它）。"""
    patch, _streak, skipped = cs._capture_failure_patch(
        state, window, now_ts=9999.0)
    return {**state, **patch, "_skipped": skipped}


def test_the_same_window_failing_three_times_advances_the_cursor():
    """连续三次同一窗口失败 → 游标推过去，用户不再被永久卡住。"""
    state = {"last_captured_until_message_id": "msg_a",
             "capture_fail_streak": 0, "capture_fail_window_key": ""}
    for attempt in (1, 2):
        state = _fail_once(state, W1)
        assert not state["_skipped"], f"第 {attempt} 次就跳过了，太早"
        assert state["last_captured_until_message_id"] == "msg_a"

    state = _fail_once(state, W1)
    assert state["_skipped"], "第三次还没跳过 —— 用户会被永久卡住"
    assert state["last_captured_until_message_id"] == "msg_c"
    assert state["last_captured_until_seq"] == 120
    # 跳过之后 streak 归零：下一批是干净的，不该带着旧账退避
    assert state["capture_fail_streak"] == 0
    assert state["capture_skipped_windows"] == 1
    assert state["last_capture_skipped_at"] == 9999.0


def test_failures_on_different_windows_never_trigger_a_skip():
    """🔴 三次互不相干的偶发失败（provider 超时那类）**不许**跳过。

    没有这条约束的话，streak 会跨窗口累加，好数据会被误丢。
    """
    state = {"last_captured_until_message_id": "msg_a",
             "capture_fail_streak": 0, "capture_fail_window_key": ""}
    for window in (W1, W2, W1, W2):
        state = _fail_once(state, window)
        assert not state["_skipped"], "换着窗口失败竟然触发了跳过"
        assert state["capture_fail_streak"] == 1
        assert state["last_captured_until_message_id"] == "msg_a"


def test_a_window_without_an_end_is_never_skipped():
    """拿不到窗口终点 → 不跳。把游标推到说不清的位置比继续卡着更糟。"""
    blind = {"after_message_id": "msg_a", "through_seq": 120}
    state = {"capture_fail_streak": 5, "capture_fail_window_key":
             cs._window_key(blind)}
    assert cs._poison_skip_patch(state, blind, now_ts=1.0) is None
    assert cs._poison_skip_patch(state, None, now_ts=1.0) is None
    assert cs._poison_skip_patch(state, {}, now_ts=1.0) is None


def test_a_moving_window_end_does_not_reset_the_streak():
    """🔴 新消息让窗口终点前移时，streak **不许**重置。

    这是最容易写错、也最致命的一处：卡住的是**起点**（游标停在毒消息前面），
    而终点会随新消息不断前移。把终点算进窗口身份的话，每来一条新消息
    streak 就归零，逃生阀永远不触发 —— 用户还是被永久卡死。

    第一版就是这么写错的（身份 = 起点+终点），靠变异测试抓出来的。
    """
    state = {"last_captured_until_message_id": "msg_a",
             "capture_fail_streak": 0, "capture_fail_window_key": ""}
    # 每次失败之间都有新消息进来，终点一直在变，起点没动
    windows = [
        {**W1, "until_message_id": "msg_c", "through_seq": 120},
        {**W1, "until_message_id": "msg_d", "through_seq": 130},
        {**W1, "until_message_id": "msg_e", "through_seq": 140},
    ]
    for i, w in enumerate(windows, 1):
        state = _fail_once(state, w)
        if i < 3:
            assert not state["_skipped"], f"第 {i} 次就跳了"
            assert state["capture_fail_streak"] == i, (
                f"第 {i} 次失败后 streak 是 {state['capture_fail_streak']}，"
                "说明终点前移把 streak 重置了 —— 逃生阀会永远不触发")
    assert state["_skipped"], "终点一直在动，逃生阀就没触发 —— 用户仍被卡死"
    assert state["last_captured_until_message_id"] == "msg_e"


def test_a_different_frontier_starts_a_fresh_streak():
    """游标推进之后（起点变了）→ streak 从 1 重数。

    不然上一批的失败账会算到下一批头上，把好数据误跳掉。
    """
    state = {"capture_fail_streak": 2, "capture_fail_window_key": "after:msg_a"}
    state = _fail_once(state, W2)      # W2 的起点是 msg_c
    assert not state["_skipped"]
    assert state["capture_fail_streak"] == 1


def test_the_threshold_is_high_enough_to_ride_out_transient_failures():
    """阈值必须 >= 2：provider 5xx / 超时这类偶发失败，重试一次就好。

    定成 1 的话，一次网络抖动就会丢掉一批真实记忆。
    """
    assert cs.CAPTURE_POISON_SKIP_AFTER >= 2


def test_a_job_without_window_info_still_accumulates_the_backoff_streak():
    """🔴 拿不到窗口标识时，streak 必须照老行为累加。

    落卡退避告警是按 streak 到 3 才发的（tests/test_memory_backoff_notice.py）。
    我第一版在拿不到窗口时把 streak 重置成 1 —— **整个退避机制就哑了**，
    是 CI 抓到的回归。

    拿不到窗口时也**绝不能跳过**：跳过要知道把游标推到哪。
    """
    state: dict = {}
    for expected in (1, 2, 3, 4):
        state = _fail_once(state, None)
        assert state["capture_fail_streak"] == expected, (
            f"第 {expected} 次失败后 streak 是 {state['capture_fail_streak']} "
            "—— 退避告警会发不出来")
        assert not state["_skipped"], "没有窗口信息竟然敢跳过"
    # 也不许因为没窗口就把游标动了
    assert "last_captured_until_message_id" not in state


def test_an_empty_window_dict_is_treated_as_no_window():
    """空 dict 和 None 一样处理 —— 都是"说不清是哪个窗口"。"""
    state: dict = {}
    for expected in (1, 2, 3):
        state = _fail_once(state, {})
        assert state["capture_fail_streak"] == expected
        assert not state["_skipped"]
