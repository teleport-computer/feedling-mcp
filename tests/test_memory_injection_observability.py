"""自动注入的落库记录：能回答问题，且**不含任何正文**。

## 为什么有这个文件

2026-08-17 之前，「每轮自动注入了哪几张卡」服务端一个字都查不到。
代价是真实的：排查「旧记忆想不起来」时据此推断「V1 只有 3 个相关名额，
所以卡被挤掉了」，后来被对照实验推翻 —— 有这条记录就不会有那个错判。

## 两条底线

1. **落库的记录里不许有正文**。挑卡的原始 trace 带卡片摘要
   （`skipped_sample[].summary`），那是随响应回去的实时调试数据、不落盘；
   落库的必须只有计数、id、理由标签。
2. **失败和「一张都没选中」要能分开**。两者在日志里长一样的话，
   排查时根本分不清是没有相关记忆，还是挑卡崩了。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from memory_garden import observability as obs  # noqa: E402

SECRET = "崽崽是公的柯基，喜欢吃鸡胸肉"


def _trace_with_content() -> dict:
    """仿真挑卡的原始 trace —— 它**确实**带摘要，这正是要防的泄漏源。"""
    return {
        "index_count": 30,
        "selected": [
            {"id": "m_1", "bucket": "relevant", "summary": "选中卡的摘要"},
            {"id": "m_2", "bucket": "turning", "summary": "另一张"},
        ],
        "rejected_sample": [
            {"id": "m_9", "reason": "no_query_overlap", "summary": SECRET},
            {"id": "m_8", "reason": "sensitive_not_allowed_for_query", "summary": "敏感卡"},
            {"id": "m_7", "reason": "no_query_overlap", "summary": "又一张"},
        ],
    }


def _record(**over):
    kw = dict(
        mode="bucketed:default",
        query="我的狗是什么品种",
        candidate_pool=32,
        selection_trace=_trace_with_content(),
        injected_ids=["m_1", "m_2"],
        cap=8,
        duration_ms=12.34,
    )
    kw.update(over)
    return obs.injection_record(**kw)


# --------------------------------------------------------------------------- #
# 底线一：不许有正文
# --------------------------------------------------------------------------- #


def test_no_card_text_reaches_the_record():
    blob = json.dumps(_record(), ensure_ascii=False)
    assert SECRET not in blob, "被拒卡的摘要漏进了落库记录"
    assert "选中卡的摘要" not in blob, "选中卡的摘要漏进了落库记录"


def test_query_is_fingerprinted_not_stored():
    rec = _record(query="我的狗是什么品种")
    blob = json.dumps(rec, ensure_ascii=False)
    assert "我的狗" not in blob, "查询原文被落库了"
    assert len(rec["query_fingerprint"]) == 12


def test_same_query_gives_the_same_fingerprint():
    """指纹要能跨轮次对上号 —— 否则没法统计「同一个问法反复查不到」。"""
    a = _record(query="磁盘为什么满了")["query_fingerprint"]
    b = _record(query="磁盘为什么满了")["query_fingerprint"]
    assert a == b
    assert a != _record(query="别的问题")["query_fingerprint"]


def test_empty_query_is_marked_not_faked():
    rec = _record(query="")
    assert rec["query_fingerprint"] == ""
    assert rec["query_empty"] is True


def test_content_free_guard_catches_a_regression():
    """守卫本身要有效 —— 否则它只是个摆设。"""
    with pytest.raises(AssertionError):
        obs.assert_content_free({"counts": {}, "leaked": {"summary": SECRET}})
    obs.assert_content_free(_record())


# --------------------------------------------------------------------------- #
# 底线二：能回答我们真正在问的问题
# --------------------------------------------------------------------------- #


def test_records_which_rule_was_used():
    """两套挑法并存期间最要紧的字段 —— 没有它，两条记录没法比。"""
    assert _record(mode="readside_relevance")["mode"] == "readside_relevance"
    assert _record(mode="bucketed:default")["mode"] == "bucketed:default"


def test_candidate_pool_and_index_count_are_both_kept():
    """两者差得多 = 有卡被 50/200 的窗口截掉了 —— 这是「旧卡失联」的观测点。"""
    counts = _record(candidate_pool=200)["counts"]
    assert counts["candidate_pool"] == 200
    assert counts["index_count"] == 30


def test_rejected_reasons_are_counted_by_kind():
    reasons = _record()["rejected_reasons"]
    assert reasons["no_query_overlap"] == 2
    assert reasons["sensitive_not_allowed_for_query"] == 1


def test_bucket_breakdown_shows_which_bucket_each_card_came_from():
    """分桶那套标 turning/recent/relevant —— 这是「打底卡占了几个名额」的观测点。

    纯相关性那套把选中项统一标成 `readside`（它没有桶的概念），所以那边这个
    字段只是复述规则名，看它不如看 `mode`。真正没有 bucket 标记时才会缺席。
    """
    assert _record()["by_bucket"] == {"relevant": 1, "turning": 1}

    readside_like = obs.injection_record(
        mode="readside_relevance", query="q", candidate_pool=10,
        selection_trace={"index_count": 5,
                         "selected": [{"id": "m_1", "bucket": "readside"}],
                         "rejected_sample": []},
        injected_ids=["m_1"], cap=8,
    )
    assert readside_like["by_bucket"] == {"readside": 1}

    unlabelled = obs.injection_record(
        mode="x", query="q", candidate_pool=1,
        selection_trace={"index_count": 1, "selected": [{"id": "m_1"}], "rejected_sample": []},
        injected_ids=["m_1"], cap=8,
    )
    assert "by_bucket" not in unlabelled


def test_zero_injection_is_recorded_as_such():
    rec = _record(injected_ids=[])
    assert rec["counts"]["injected"] == 0
    assert "注入 0 张" in obs.injection_summary(rec)


def test_duration_is_kept_for_performance_tracking():
    assert _record()["dur_ms"] == 12.3


def test_ids_are_capped_so_the_record_stays_small():
    rec = _record(injected_ids=[f"m_{i}" for i in range(50)])
    assert len(rec["injected_ids"]) <= 12


def test_missing_trace_does_not_crash():
    """挑卡崩了的时候也要能出记录 —— 那正是最需要日志的时刻。"""
    rec = obs.injection_record(
        mode="failed", query="q", candidate_pool=32,
        selection_trace=None, injected_ids=[], cap=8,
    )
    assert rec["counts"]["injected"] == 0
    assert rec["counts"]["index_count"] == 0


def test_unknown_reason_is_kept_truncated_not_dropped():
    """有人加了新拒绝理由时，日志要跟着长出来，而不是把它吞掉。"""
    rec = _record(selection_trace={
        "index_count": 1, "selected": [],
        "rejected_sample": [{"id": "m_1", "reason": "some_new_reason_nobody_told_us"}],
    })
    assert "some_new_reason_nobody_told_us" in rec["rejected_reasons"]
