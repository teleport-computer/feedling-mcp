"""V2 落卡的逃生阀 —— 接在持久批次协议（jobs_store）上。

## 为什么要单独一个文件

V1 的逃生阀在 ``proactive.capture_scheduler``，2026-09-13 上线。但 V2 的落卡
**根本不走那里**：worker 的 ``_record_extraction_status`` 对 lane == "capture"
直接 return，V2 的失败状态走 ``jobs_store._capture_fail_on_cursor``。

2026-09-14 prod 实测：还卡着的落卡用户抽查 12 个，**12 个全是 V2**。
最初报障那个用户恢复了，只是因为他走的是 V1。

之前我说「V1 和 V2 都覆盖了」是错的：看到 V2 有调用点就下了结论，没往上查
那条路会不会提前返回。

## 契约

- ``window`` 给了 → 走和 V1 **同一个**判断函数（两边各写一遍必然漂）
- ``window`` 不给 / 为空 → **行为逐字节不变**（其余 6 个调用点都不是毒消息场景）
- ``increment_backoff=False`` → 不碰 streak，也不跳（落卡被关这类）
- 跳过和任务标 failed 在同一个事务里（这里用假游标验写回的内容）
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FEEDLING_DATA_DIR",
                      tempfile.mkdtemp(prefix="feedling-v2-poison-"))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import jobs_store  # noqa: E402
from proactive import capture_scheduler as cs  # noqa: E402

WINDOW = {"after_message_id": "msg_a", "after_seq": 100,
          "until_message_id": "msg_c", "until_ts": 1000.0,
          "through_seq": 120, "message_count": 8}


class _Cur:
    """只记下写进 user_blobs 的状态，并假装任务行更新成功。"""

    def __init__(self):
        self.saved_state = None
        self.rowcount = 1

    def execute(self, sql, params=()):
        if "UPDATE user_blobs" in sql:
            self.saved_state = dict(params[0].obj if hasattr(params[0], "obj")
                                    else params[0])
        self.rowcount = 1


def _fail(state, *, error, window=None, increment_backoff=True):
    cur = _Cur()
    out = jobs_store._capture_fail_on_cursor(
        cur, state=dict(state), job_id=1, user_id="u", claimed_by="w",
        error=error, increment_backoff=increment_backoff, window=window)
    return out


def test_v2_parse_failures_skip_after_three_on_the_same_frontier():
    """🔴 V2 用户终于能被放出来：同一游标解析失败 3 次 → 游标推过去。

    失败原因用 worker **真实会传进来**的那个码：V2 先把
    ``json_decode_error:JSONDecodeError`` 归一成 ``extraction_failed:json_decode_error``。
    我第一版这里直接写了 V1 的裸原因，测试绿了，V2 上却一直落进 6 次档（Codex 抓到）。
    """
    from model_api_runtime.v2 import worker

    code = worker._extraction_failure_code(
        RuntimeError("json_decode_error:JSONDecodeError"))
    assert code == "extraction_failed:json_decode_error"
    state: dict = {"last_captured_until_message_id": "msg_a",
                   "last_captured_until_seq": 100}
    for i in range(1, cs.CAPTURE_POISON_SKIP_AFTER):
        state = _fail(state, error=code, window=WINDOW)
        assert state["last_captured_until_seq"] == 100, f"第 {i} 次就跳了"
    state = _fail(state, error=code, window=WINDOW)
    assert state["last_captured_until_seq"] == 120, "V2 解析失败 3 次后游标没推过去"
    assert state["last_captured_until_message_id"] == "msg_c"
    assert state["capture_skipped_windows"] == 1


def test_v2_write_failures_get_the_patient_threshold():
    """V2 上「存不进去」同样走 6 次档 —— 和 V1 用的是同一个判断函数。"""
    state: dict = {"last_captured_until_seq": 100}
    reason = "capture_memory_write_failed:RuntimeError"
    for _ in range(cs.CAPTURE_TRANSIENT_SKIP_AFTER - 1):
        state = _fail(state, error=reason, window=WINDOW)
    assert state["last_captured_until_seq"] == 100
    state = _fail(state, error=reason, window=WINDOW)
    assert state["last_captured_until_seq"] == 120


def test_without_a_window_the_behaviour_is_byte_identical():
    """🔴 其余 6 个调用点不传 window —— 必须和改动前**完全一样**。

    它们都不是毒消息场景：落卡被关、批次丢失、游标已被别人推进、校验拒绝。
    在这些路径上跳过会丢掉本不该丢的东西。
    """
    state = {"capture_fail_streak": 7, "last_captured_until_seq": 100}
    for window in (None, {}):
        out = _fail(state, error="capture_frontier_changed", window=window)
        assert out["capture_fail_streak"] == 8
        assert out["last_captured_until_seq"] == 100
        assert "capture_skipped_windows" not in out
        assert "capture_fail_window_key" not in out, "老路径不该写新字段"


def test_no_backoff_means_no_streak_and_no_skip():
    """increment_backoff=False（落卡被关这类）：不累加、更不跳，即使带了窗口。"""
    state = {"capture_fail_streak": 2, "last_captured_until_seq": 100,
             "capture_fail_window_key": cs._window_key(WINDOW)}
    out = _fail(state, error="json_decode_error:X", window=WINDOW,
                increment_backoff=False)
    assert out["capture_fail_streak"] == 2
    assert out["last_captured_until_seq"] == 100


def test_first_ever_window_is_keyed_by_seq_not_by_the_moving_end():
    """新用户第一批：起点没有消息 id、seq 是 0。身份键要锚在 seq 上。

    回落到终点的话，故障期间每来一条新消息 key 就变一次 → streak 永远到不了阈值。
    """
    state: dict = {}
    for i, until in enumerate(("m2", "m3", "m4"), start=1):
        window = {"after_message_id": "", "after_seq": 0, "until_message_id": until,
                  "until_ts": float(i), "through_seq": i + 1}
        state = _fail(state, error="extraction_failed:json_decode_error", window=window)
    assert state["capture_skipped_windows"] == 1
    assert state["last_captured_until_message_id"] == "m4"


# 「worker 真的把窗口传进来了」由 tests/test_v2_capture_batch_protocol.py 里两条
# 走真 worker + 真 Postgres 的测试验（first_window_parse_failure / prepared_batch
# commit_keeps_raising）。以前这里是一条 grep 源码的测试，只能证明那行字还在，
# 证明不了 prepared 重试那条分支上传进去的窗口是不是空壳。
