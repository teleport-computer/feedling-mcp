"""管理端:``runtime_failed`` 必须被读成「原因已丢弃」,不是一种运行时错误(T257)。

背景(实证,非设计假设):净化层对「原始 reason 存在、但没通过安全白名单
``^[a-z0-9_:-]{1,120}$``」的处理是**整段替换成 ``runtime_failed``**
(``db.py`` 的 ``_LANE_ROLLUP_CODE_RE``、``memory_metadata`` 的同形 SQL CASE)。
2026-08-22 prod:V1 心跳 6 个账号整周零成功、663 次失败(占 V1 心跳失败 66%),
失败码 100% 是它 —— 读表的人会得出「运行时坏了」,而真相是「此处原本有答案」。

这批断言两个方向一起钉:
既要「注解出现」,也要「注解**只**出现在该出现的地方」——
只钉前者的话,把注解无条件贴给每个失败码,那一半照样绿。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track  # noqa: E402


def _text(html_str: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_str)


def test_discarded_code_carries_its_meaning_and_others_do_not():
    """``runtime_failed`` 带注解;普通失败码不带。"""
    # 前提:这两个值确实不同,且注解文案非空 —— 否则下面的对比没有意义。
    assert data_track._DISCARDED_REASON_CODE == "runtime_failed"
    assert data_track._DISCARDED_REASON_NOTE.strip()

    discarded = _text(data_track._failure_code_cell("runtime_failed"))
    normal = _text(data_track._failure_code_cell("turn_failed:auth_invalid"))

    assert "runtime_failed" in discarded
    assert "原因已丢弃" in discarded
    assert "不是一种运行时错误" in discarded

    assert "turn_failed:auth_invalid" in normal
    # 反向:注解不能无条件贴给每个码,否则它不再携带信息。
    assert "原因已丢弃" not in normal


def test_missing_code_is_not_rendered_as_the_discarded_code():
    """「没有失败码」与「原因被丢弃」必须长得不一样。

    改这条之前,聊天可靠性页把缺值兜底写成 ``runtime_failed``,
    于是两件不同的事在页面上是同一个字符串。
    """
    missing = _text(data_track._failure_code_cell(""))
    none_value = _text(data_track._failure_code_cell(None))

    assert "runtime_failed" not in missing
    assert "runtime_failed" not in none_value
    # 与本文件其余两处失败码渲染用同一个词,不另造。
    assert "other" in missing
    assert "other" in none_value


def test_every_failure_code_render_site_goes_through_the_helper():
    """三个渲染出口都必须走同一个助手。

    ⚠️ 这条是为了防「只改了看得见的那一处」:2026-08-22 同一文件里
    三处渲染失败码,兜底值曾经是 other / other / **runtime_failed** 三种,
    异类的那一处正是把两件事混成一个词的地方。
    判据取自源码而非渲染结果 —— 渲染结果只能证明「我构造到的那条路对了」。
    """
    source = Path(data_track.__file__).read_text(encoding="utf-8")

    # 前提:助手确实存在且被引用,否则下面的计数恒为 0、这条测试恒绿。
    assert "def _failure_code_cell(" in source
    assert source.count("_failure_code_cell(") >= 4  # 1 处定义 + 3 处调用

    # 不许再有绕过助手、直接把失败码塞进 <td><code> 的写法。
    bypass = re.findall(
        r"<td><code>\{html\.escape\(str\(row\.get\('(?:code|error_code)'\)[^}]*\}</code></td>",
        source,
    )
    assert bypass == [], f"仍有绕过助手的失败码渲染:{bypass}"
