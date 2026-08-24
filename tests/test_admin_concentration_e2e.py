"""集中度必须**从冻结数据一路活到页面上**(T258 端到端)。

⚠️ 这个文件存在的理由,是一个真实的漏检:
``test_admin_concentration.py`` 里的四条断言全部**直接构造最终的 metric cell**,
于是它们只覆盖了「渲染那一步」。而产生那个 cell 的 ``_event_path_master_payload``
当时**根本没把 concentration 放进返回 dict** —— 页面恒显示「集中度未计算」,
四条测试却全绿。是 codex2 在串接后端时发现的,不是我的测试发现的。

⇒ **在中间注入的测试,只能证明「我构造到的那条路对了」。**
   本文件从**最上游的冻结 payload** 喂进去,一路断言到渲染出的 HTML。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track  # noqa: E402


def _text(html_str: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_str)


def _frozen(*, concentration) -> dict:
    """一份最小的冻结 payload;model_api/chat 那格带(或不带)集中度。"""
    lane: dict = {
        "completed": 6, "failed": 4, "expired": 0, "superseded": 0,
        "failure_codes": {"extraction_failed:upstream_unavailable": 4},
    }
    if concentration is not _MISSING:
        lane["concentration"] = concentration
    return {
        "timezone": "Asia/Shanghai",
        "closed_through_day": "2030-06-07",
        "windows": [{
            "key": "24h", "start_day": "2030-06-07",
            "end_day": "2030-06-07", "day_count": 1,
            "routes": {
                "model_api": {
                    "active_users": 5,
                    "coverage": {"level": "green", "covered_days": 1,
                                 "required_days": 1},
                    "lanes": {"chat": lane},
                    "lane_sources": {},
                },
                "resident": {
                    "active_users": 3,
                    "coverage": {"level": "red", "covered_days": 0,
                                 "required_days": 1},
                    "lanes": {}, "lane_sources": {},
                },
            },
            # T289 makes the access-path table consume its own frozen axis;
            # route data remains only for the runtime-family diagnostic. Feed
            # the same upstream lane shape into both axes so this test proves
            # concentration survives each independent payload path.
            "paths": {
                "apikey_v2": {
                    "active_users": 5,
                    "mode_sources": {
                        "explicit": {"active_users": 5, "attempts": 10},
                    },
                    "coverage": {
                        "level": "green", "covered_days": 1,
                        "required_days": 1,
                        "effective_from": "2030-06-07",
                    },
                    "lanes": {"chat": lane},
                    "lane_sources": {},
                },
            },
        }],
    }


_MISSING = object()


def _render(frozen: dict) -> str:
    master = data_track._event_path_master_payload(frozen)
    return data_track._render_event_master_tables(master)


def test_concentration_survives_payload_to_html():
    """带集中度的冻结格 ⇒ 页面上真的看得见那两个数。"""
    frozen = _frozen(concentration={
        "users_active": 14, "users_zero_success": 6,
        "top_user_failure_share": 0.321,
    })

    # 前提:这份 payload 确实产出了一个 metric 格,否则下面断言无从谈起。
    master = data_track._event_path_master_payload(frozen)
    states = {
        c.get("state")
        for row in (master["windows"][0]["rows"])
        for c in (row.get("cells") or {}).values()
    }
    assert "metric" in states

    html_out = _render(frozen)
    out = _text(html_out)
    assert "零成功 6/14 人" in out
    assert "失败最集中的一个用户占 32%" in out

    # ⚠️ 我在这条断言上连写错两版,都是**编码了碰巧的事实**而不是我在乎的性质:
    #   第一版「整页不出现未计算」→ 红。同页其它动作本就没有集中度,显示未计算是**正确的**。
    #   第二版「恰好一格带该数字」→ 红。同一份冻结指标被显式喂进独立的
    #             access-path 与 runtime-family 轴,两格都带,也是**正确的**。
    # 我真正在乎的性质只有一条:**凡是印出了数字的格子,都不许同时说「未计算」**。
    # 格数是多少不该由这条测试规定 —— 那是表结构的自由度。
    cells = [_text(c) for c in html_out.split("<td>") if "零成功 6/14 人" in c]
    assert cells, "没有任何格子印出集中度数字"
    for c in cells:
        assert "集中度未计算" not in c


def test_missing_concentration_reaches_the_page_as_uncomputed():
    """上游没给 ⇒ 页面明说「未计算」,不是留空、也不是一个数。

    ⚠️ 反向断言是本条的承重点:若透传坏掉,上一条会红;
    但若渲染层把缺失静默吞成空字符串,只有这一条会红。
    """
    out = _text(_render(_frozen(concentration=_MISSING)))
    assert "集中度未计算" in out
    assert "零成功" not in out


def test_upstream_garbage_does_not_become_a_number_on_the_page():
    """上游给了脏数据 ⇒ 仍然只能显示「未计算」。

    ``users_zero_success > users_active`` 长得像个合法比例,
    但它意味着上游算错了 —— 印出来会让人对着假数去修不存在的问题。
    """
    out = _text(_render(_frozen(concentration={
        "users_active": 3, "users_zero_success": 5,
    })))
    assert "集中度未计算" in out
    assert "零成功" not in out
