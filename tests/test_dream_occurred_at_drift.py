"""把「沿 supersedes 链追回原始 occurred_at」这套算法钉住。

T063:V2 dream 曾把后继卡的 occurred_at 写成「最近一条聊天的时间」而不是源卡的
事件时间(修复 521451a3)。存量坏卡要不要修,取决于**偏差有多大** ——
而那个数字是拿 tools/dream_occurred_at_drift.py 算出来的。

⚠️ 这个文件存在的理由:**报告里的数字只和算它的那段代码一样可信**。
我第一版的 p90 用 `int(n*0.9)-1` 取样,n=2 时会退化成 min,
跑出过 p90(9.58) < median(26.85) —— 一个比中位数还小的 p90 会直接进给 Seven 的报告,
而且大概率没人会去质疑它。所以分位数和链式回溯都必须有回归。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from dream_occurred_at_drift import _percentile, analyse, parse_ts  # noqa: E402


@pytest.mark.parametrize(
    "raw",
    [
        "2026-08-13T21:43:04Z",              # canonical
        "2026-08-13T17:27:18.618643",        # python isoformat:6 位微秒 + 无时区
        "2026-06-18T00:00:00",               # 无时区
        "2026-08-13",                        # date-only(V1 hosted runtime 默认值)
        "2026-08-13T10:47:06.452655+00:00",  # 带偏移
    ],
)
def test_every_backend_timestamp_shape_parses(raw):
    """后端写过至少五种形状;解析不了就会被算成「追不回」,把影响面低报。"""
    assert parse_ts(raw) is not None


@pytest.mark.parametrize("junk", ["", "   ", "not a date", "2026-13-45"])
def test_garbage_is_not_a_timestamp(junk):
    assert parse_ts(junk) is None


def test_timezoneless_is_read_as_utc():
    assert parse_ts("2026-06-18T00:00:00") == parse_ts("2026-06-18T00:00:00Z")


def _fixture() -> list[dict]:
    return [
        {"id": "src1", "occurred_at": "2026-06-18T00:00:00", "supersedes": []},
        {"id": "src2", "occurred_at": "2026-07-01T10:00:00Z", "supersedes": []},
        {"id": "s1", "occurred_at": "2026-08-14T13:00:00Z", "supersedes": ["src1", "src2"]},
        {"id": "src3", "occurred_at": "2026-08-05", "supersedes": []},
        {"id": "s2", "occurred_at": "2026-08-14T14:00:00Z", "supersedes": ["s1", "src3"]},
        {"id": "s3", "occurred_at": "2026-08-14T15:00:00Z", "supersedes": ["ghost"]},
        {"id": "plain", "occurred_at": "2026-08-01T00:00:00Z", "supersedes": []},
    ]


def test_only_successor_cards_are_counted():
    """没有 supersedes[] 的普通卡不该进影响面 —— 它们的时间本来就是对的。"""
    assert analyse(_fixture())["successors"] == 3


def test_original_time_comes_from_the_latest_source():
    """判据必须和修复后的实现一致:取源卡里**最新**的那个,不是最早、不是平均。"""
    got = analyse(_fixture())
    # s1: 2026-08-14T13:00 vs 源里最新 2026-07-01T10:00 = 44.12 天
    assert got["drift_days"]["max"] == pytest.approx(44.12, abs=0.02)


def test_multi_generation_chain_resolves_through_its_parent():
    """s2 的源卡 s1 自己也是后继 —— 必须递归解 s1 的应有值,不能拿 s1 现在的坏值。"""
    got = analyse(_fixture())
    # s2: 2026-08-14T14:00 vs max(s1 应有值 07-01, src3 08-05) = 08-05 -> 9.58 天
    assert got["drift_days"]["min"] == pytest.approx(9.58, abs=0.02)


def test_broken_chain_is_counted_not_silently_dropped():
    """源卡不在返回集里时必须**记成断链**,不能当成「没有偏差」混进可追回。"""
    got = analyse(_fixture())
    assert (got["recoverable"], got["broken_chain"]) == (2, 1)


def test_percentile_never_falls_below_the_median_on_small_samples():
    """第一版的 int(n*q)-1 在 n=2 时把 p90 算成了 min(9.58 < 26.85 中位数)。"""
    got = analyse(_fixture())["drift_days"]
    assert got["p90"] >= got["median"]
    assert got["median"] <= got["max"]


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [([1.0, 2.0, 3.0, 4.0, 5.0], 0.5, 3.0), ([1.0, 2.0, 3.0, 4.0, 5.0], 0.9, 4.6), ([7.0], 0.9, 7.0)],
)
def test_percentile_interpolates(values, q, expected):
    assert _percentile(values, q) == pytest.approx(expected, abs=0.01)
