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

import pytest

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FEEDLING_DATA_DIR",
                      tempfile.mkdtemp(prefix="feedling-poison-test-"))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from proactive import capture_scheduler as cs  # noqa: E402
from memory import capture_failure as cf  # noqa: E402

W1 = {"after_message_id": "msg_a", "until_message_id": "msg_c",
      "through_seq": 120, "until_ts": 1000.0, "message_count": 8}
W2 = {"after_message_id": "msg_c", "until_message_id": "msg_f",
      "through_seq": 140, "until_ts": 2000.0, "message_count": 5}


def _fail_once(state: dict, window: dict | None,
               reason: str = "json_decode_error:JSONDecodeError") -> dict:
    """走生产用的那个共用函数（V1/V2 两条线都调它）。

    默认原因是解析失败 —— 下面这批测试模拟的就是毒消息那个场景
    （同样的输入必然同样失败）。写入类失败的阈值见文件末尾那组测试。
    """
    patch, _streak, skipped = cs._capture_failure_patch(
        state, window, now_ts=9999.0, reason=reason)
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
             cf.window_key(blind)}
    assert cf.poison_skip_patch(state, blind, now_ts=1.0) is None
    assert cf.poison_skip_patch(state, None, now_ts=1.0) is None
    assert cf.poison_skip_patch(state, {}, now_ts=1.0) is None


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
    assert cf.CAPTURE_POISON_SKIP_AFTER >= 2


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


# --------------------------------------------------------------------------- #
# 两档阈值（2026-09-14）
# --------------------------------------------------------------------------- #

def _fail_until_skip(reason: str, limit: int = 12) -> int | None:
    state: dict = {}
    for attempt in range(1, limit + 1):
        patch, _s, skipped = cs._capture_failure_patch(
            state, W1, now_ts=float(attempt), reason=reason)
        state = {**state, **patch}
        if skipped:
            return attempt
    return None


def test_a_write_failure_gets_more_retries_than_a_parse_failure():
    """🔴 「存不进去」这类会自己好的失败，不能按「读不懂」的 3 次就跳。

    2026-09-14 prod 实测：引号修好后，同一批窗口能解析了，却在写入时栽了
    （capture_shared_envelope_requires_enclave_key）。而这类失败**重试能好**：

        第一批  失败 失败 成功          ← 第三次过了
        第二批  失败 失败 失败 → 跳过   ← 按 3 次跳掉，9 条消息的记忆丢了

    用确定性失败的阈值处理会自己好的失败，就是在丢本来保得住的记忆。
    """
    write_skip = _fail_until_skip("capture_memory_write_failed:RuntimeError")
    parse_skip = _fail_until_skip("json_decode_error:JSONDecodeError")
    assert parse_skip == cf.CAPTURE_POISON_SKIP_AFTER
    assert write_skip == cf.CAPTURE_TRANSIENT_SKIP_AFTER
    assert write_skip > parse_skip, "写入失败没比解析失败多给机会"


def test_parse_failures_still_skip_fast():
    """修写入那边，不能把解析失败也拖慢 —— 那一类重试一万次也一样。"""
    for reason in ("json_decode_error:JSONDecodeError", "no_json_object",
                   "not_an_object"):
        assert _fail_until_skip(reason) == cf.CAPTURE_POISON_SKIP_AFTER, reason


def test_an_unknown_failure_defaults_to_the_patient_threshold():
    """🔴 没见过的失败原因走保守的那一档。

    白名单只列确定性失败。反过来用黑名单的话，下一种新冒出来的
    「会自己好的失败」会被 3 次就跳掉，又开始悄悄丢记忆。
    """
    assert _fail_until_skip("some_brand_new_failure") == cf.CAPTURE_TRANSIENT_SKIP_AFTER
    assert _fail_until_skip("") == cf.CAPTURE_TRANSIENT_SKIP_AFTER


def test_even_transient_failures_eventually_skip():
    """会自己好的失败也**必须有上限** —— 不然某批写入失败其实是确定性的时候，
    用户又会被永久卡死，也就是这整套逃生阀要修的问题。"""
    assert _fail_until_skip("capture_memory_write_failed:RuntimeError") is not None
    assert cf.CAPTURE_TRANSIENT_SKIP_AFTER > cf.CAPTURE_POISON_SKIP_AFTER


def test_the_reason_is_read_from_either_place_on_the_job():
    """原因可能在 capture_result.reason，也可能只在 status_reason。"""
    assert cs._failure_reason_of({"capture_result": {"reason": "json_decode_error:X"}}) \
        == "json_decode_error:X"
    assert cs._failure_reason_of({"status_reason": "no_json_object"}) == "no_json_object"
    assert cs._failure_reason_of(None) == ""


def test_the_real_v1_call_site_passes_the_failure_reason(monkeypatch):
    """🔴 经过**真实调用点**（record_capture_job_status），不是直接调函数。

    上面那组测试直接调 `_capture_failure_patch`，所以调用点漏传 reason 时
    它们照样全绿 —— 而漏传的后果是所有失败都默认走 6 次档，
    解析失败（重试一万次也一样）被白白拖慢一倍，用户多卡一倍的时间。

    变异测试抓到的缺口：把调用点的 `reason=` 删掉，前面 13 条全过。

    数据库那几步换成内存版，只验「调用点有没有把原因送进阈值判断」。
    """
    saved: dict = {}
    monkeypatch.setattr(cs, "load_capture_state", lambda store: dict(saved))
    monkeypatch.setattr(cs, "save_capture_state",
                        lambda store, state, now=None: saved.update(state) or dict(saved))
    monkeypatch.setattr(cs.capture_jobs, "notify_backoff", lambda *a, **k: None)
    monkeypatch.setattr(cs, "refresh_capture_state_from_chat",
                        lambda store, now=None: dict(saved))
    monkeypatch.setattr(cs, "_record_skipped_window", lambda *a, **k: None)
    monkeypatch.setattr(cs, "_capture_trace_job_id", lambda job: "j")

    job = {"job_id": "j", "source": cs.capture_jobs.CAPTURE_JOB_SOURCE,
           "capture_window": W1,
           "capture_result": {"status": "failed",
                              "reason": "json_decode_error:JSONDecodeError"}}
    # 解析失败 → 第 3 次就该跳过、游标推过去
    for _ in range(cf.CAPTURE_POISON_SKIP_AFTER):
        try:
            cs.record_capture_job_status(object(), job, status="failed", now=1.0)
        except Exception:  # noqa: BLE001 —— 下游 trace 之类的副作用不关心
            pass
    assert saved.get("last_captured_until_message_id") == W1["until_message_id"], (
        "解析失败连续 3 次后游标没推过去 —— 调用点大概漏传了失败原因，"
        "所有失败都掉进了 6 次那一档")


def _run_reasons(reasons, window=None):
    """按顺序喂失败原因，返回第几次跳过（没跳返回 None）。"""
    w = window or {"after_message_id": "msg_a", "until_message_id": "msg_c",
                   "until_ts": 1.0, "through_seq": 120}
    state: dict = {}
    for i, reason in enumerate(reasons, start=1):
        patch, _s, skipped = cf.capture_failure_patch(state, w, now_ts=float(i), reason=reason)
        state.update(patch)
        if skipped:
            return i
    return None


def test_one_parse_failure_cannot_inherit_earlier_write_failures():
    """🔴 写入、写入、解析 —— 第三次不能按「解析 3 次」就跳。

    以前两档共用一个计数、阈值只看本次原因，一次解析失败就继承了前面的写入失败，
    绕过 6 次保护，把本来重试能保住的记忆提前丢掉（Codex 第四轮复现）。
    """
    write, parse = "capture_memory_write_failed", "extraction_failed:json_decode_error"
    assert _run_reasons([write, write, parse]) is None
    # 解析连续 3 次才快速跳；中间夹一次别的失败，解析计数清零。
    assert _run_reasons([parse, parse, parse]) == 3
    assert _run_reasons([parse, parse, write, parse]) is None
    assert _run_reasons([write, parse, parse, parse]) == 4
    # 总失败数到 6 次兜底，不管混成什么样。
    assert _run_reasons([write, parse, write, parse, write, parse]) == 6


@pytest.mark.parametrize("reason,expected", [
    # V1：CLI 原始错误文本（prod 上真实出现过的形状）
    ('capture_agent_call_failed:RuntimeError: cli agent exited 1: Failed to authenticate. '
     'API Error: 401 {"error":"Insufficient balance"} (api_status=401)', "account"),
    ("capture_agent_call_failed:RuntimeError: Failed to authenticate: OAuth session expired "
     "and could not be refreshed", "account"),
    # V2：extraction 的公开 provider 分类
    ("extraction_failed:auth_invalid", "account"),
    ("extraction_failed:quota_insufficient", "account"),
    ("extraction_failed:rate_limited", "account"),
    ("extraction_failed:upstream_unavailable", "account"),
    # 可能真是内容引起的 —— 不能归到永不跳过
    ("extraction_failed:content_filtered", "other"),
    ("extraction_failed:unknown", "other"),
    ("capture_agent_call_failed:RuntimeError: openai-compatible response carried no assistant text",
     "other"),
    ("capture_memory_write_failed", "other"),
    ("json_decode_error:JSONDecodeError", "parse"),
    ("extraction_failed:json_decode_error", "parse"),
])
def test_failure_class(reason, expected):
    assert cf.failure_class(reason) == expected


def test_account_failures_never_skip_and_do_not_count_toward_skipping():
    """🔴 余额不足失败再多次也不跳；充值后偶发的别的失败也不能继承这些次数立刻跳。"""
    balance = "extraction_failed:quota_insufficient"
    other = "capture_memory_write_failed"
    assert _run_reasons([balance] * 30) is None
    # 8 次余额不足 + 5 次别的失败：别的失败只有 5 次，不到 6
    assert _run_reasons([balance] * 8 + [other] * 5) is None
    assert _run_reasons([balance] * 8 + [other] * 6) == 14
    # 账号失败夹在中间会打断「连续解析失败」
    parse = "extraction_failed:json_decode_error"
    assert _run_reasons([parse, parse, balance, parse]) is None


def test_frontier_seq_takes_the_later_of_seq_and_message_id():
    """两份进度记法取靠后的；数字不可信（未初始化）时只看 id；id 查不到时只看数字。"""
    seq_of = {"m3": 120, "m9": 300}.get
    # prod 上旧逃生阀留下的：数字 0 + 已初始化 + id 记着真实位置
    assert cf.frontier_seq({"last_captured_until_message_id": "m3",
                            "last_captured_until_seq": 0,
                            "capture_seq_initialized": True}, seq_of) == 120
    # V1 完成只更新 id、数字停在旧值
    assert cf.frontier_seq({"last_captured_until_message_id": "m9",
                            "last_captured_until_seq": 120,
                            "capture_seq_initialized": True}, seq_of) == 300
    # 正常 V2：两份一致
    assert cf.frontier_seq({"last_captured_until_message_id": "m3",
                            "last_captured_until_seq": 120,
                            "capture_seq_initialized": True}, seq_of) == 120
    # 数字未初始化：不信数字
    assert cf.frontier_seq({"last_captured_until_message_id": "m3",
                            "last_captured_until_seq": 999,
                            "capture_seq_initialized": False}, seq_of) == 120
    # id 被清理查不到：只看可信的数字
    assert cf.frontier_seq({"last_captured_until_message_id": "gone",
                            "last_captured_until_seq": 150,
                            "capture_seq_initialized": True}, seq_of) == 150
    # 老数据只有数字、没有标志位：数字可信
    assert cf.frontier_seq({"last_captured_until_seq": 77}, seq_of) == 77
    assert cf.frontier_seq({}, seq_of) == 0
