"""事件总表:每格必须带集中度,否则平均值会骗人(T258)。

**为什么(prod 实证,2026-08-22)**:
V1 心跳 14 个活跃号里 **6 个整周零成功**,663 次失败 = V1 心跳失败的 66%;
V2 25 个号里 1 个整周零成功,剔掉它整体 11.2% → 9.0%。
表上那个「56% 失败」会被读成「一半心跳在失败」,真相是
「6 个号从来没成功过,其余大致正常」。
⇒ **数是对的,但它回答的不是读表人以为的那个问题。**

这批断言两个方向一起钉:
只钉「有集中度时显示出来」是不够的 —— 把「未计算」也渲染成一个数,
那一半照样绿。所以每条可见性断言旁边都配一条「该不同形的确实不同形」。

⚠️ 本文件只测**渲染契约**。SQL 侧(按窗口整体去重、GROUP BY (route,lane))
另在 test_lane_rollup 钉,那是后端那半的判据。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track  # noqa: E402


def _text(html_str: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_str)


def _metric_cell(**over) -> dict:
    cell = {
        "state": "metric", "coverage": "green",
        "success": 6, "failure": 3, "denominator": 9,
        "denominator_rule": "completed + operational failure",
    }
    cell.update(over)
    return cell


def test_zero_success_count_is_shown_next_to_the_rate():
    """集中度必须和率在同一格里,不另开区块。"""
    cell = _metric_cell(concentration={
        "users_active": 14, "users_zero_success": 6,
        "top_user_failure_share": 0.321,
    })
    out = _text(data_track._render_event_master_cell(
        cell, action="主动任务 · 时钟心跳", path="apikey_v1", window="24h"))

    # 前提:这一格确实渲染出了率,否则「贴在率旁边」无从谈起。
    assert "% 成功" in out and "% 失败" in out
    assert "零成功 6/14 人" in out
    assert "失败最集中的一个用户占 32%" in out


def test_zero_of_n_is_a_real_conclusion_not_the_same_as_uncomputed():
    """「0 人零成功」= 这格是健康的;与「没算」必须不同形。

    ⚠️ 这条是本文件的承重点:若两者同形,读表的人会把
    「我们没算」读成「这里没问题」——**沉默会被读成好消息**。
    """
    healthy = _text(data_track._render_event_master_cell(
        _metric_cell(concentration={
            "users_active": 20, "users_zero_success": 0,
            "top_user_failure_share": 0.1,
        }),
        action="a", path="p", window="w"))
    uncomputed = _text(data_track._render_event_master_cell(
        _metric_cell(), action="a", path="p", window="w"))

    assert "零成功 0/20 人" in healthy
    assert "集中度未计算" not in healthy

    assert "集中度未计算" in uncomputed
    assert "零成功" not in uncomputed
    assert healthy != uncomputed


def test_malformed_concentration_degrades_to_uncomputed_not_to_a_number():
    """脏数据不许被渲染成一个看起来正常的数。

    ⚠️ ``zero > active`` 是这里最危险的一种:它长得像个合法比例,
    但它意味着上游算错了 —— 显示出来会让人对着一个假数去修一个不存在的问题。
    """
    for bad in (
        None, "x", {}, {"users_active": "a", "users_zero_success": 1},
        {"users_active": 3, "users_zero_success": 5},      # zero > active
        {"users_active": -1, "users_zero_success": 0},
    ):
        out = _text(data_track._render_event_master_cell(
            _metric_cell(concentration=bad), action="a", path="p", window="w"))
        assert "集中度未计算" in out, bad
        assert "零成功" not in out, bad


def test_share_is_dropped_when_out_of_range_but_counts_survive():
    """比例越界只丢比例,不丢人数 —— 一个字段坏了不该带走另一个。"""
    for bad_share in (1.5, -0.2, float("inf"), float("nan"), True, "0.5"):
        out = _text(data_track._render_event_master_cell(
            _metric_cell(concentration={
                "users_active": 10, "users_zero_success": 2,
                "top_user_failure_share": bad_share,
            }), action="a", path="p", window="w"))
        assert "零成功 2/10 人" in out, bad_share
        assert "失败最集中" not in out, bad_share
