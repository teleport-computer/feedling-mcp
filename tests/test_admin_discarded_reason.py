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


def test_no_render_site_puts_a_row_field_into_a_bare_code_cell():
    """任何 ``<td><code>`` 里塞 ``row.get(...)`` 的写法都必须走助手。

    ⚠️ **这条我改过两次,两次都因为我把「我知道的东西」写进了判据:**

      第一版按字面量 grep 找出口,数出「三处」——实际有四处
      (第四处兜底值是 ``—``,是这条测试自己扫出来的)。
      第二版正则写死字段名 ``code|error_code`` ——
      **仍然漏掉 ``last_error``**,由 codex2 在 PR 复审中抓到:
      同一页 ``runtime_failed`` 出现两次,只有一次带「原因已丢弃」。

    ⇒ 判据不能列举**我想得到的字段名**,只能钉**形状**:
      「``<td><code>`` 里出现 ``row.get(...)``」——不管那个键叫什么。
    ⚠️ 也不再断言「调用次数 >= N」:那同样是在编码我此刻知道的出口数量,
      新增一个出口时它不会红。
    """
    source = Path(data_track.__file__).read_text(encoding="utf-8")

    # 前提:助手存在。否则下面的扫描恒为空、这条测试恒绿。
    assert "def _failure_code_cell(" in source

    found = set(re.findall(
        r"<td><code>\{html\.escape\(str\(row\.get\('([a-z_]+)'\)[^}]*\}</code></td>",
        source,
    ))

    # 可审豁免:每一条都必须写明**为什么它不是失败码**。
    # ⚠️ 这不是「已知问题清单」,是「已判定无关」清单 ——
    #    新出现的键会直接红,逼下一个人做一次判断,而不是默认放行。
    #    (同 Supervisor 2026-08-22 对 actionlint 基线的口径:基线必须可审,
    #     既有条目要写明为何保留,否则它退化成没人敢动也没人知道为什么的清单。)
    NOT_A_FAILURE_CODE = {
        "job_id",   # 任务标识,不是失败原因;它永远不会取值 runtime_failed
    }

    bypass = sorted(found - NOT_A_FAILURE_CODE)
    assert bypass == [], (
        "以下键被直接渲染进 <td><code>,绕过了 _failure_code_cell。"
        "若它确实可能取到失败码,请改走助手;若确实无关,请加进 "
        f"NOT_A_FAILURE_CODE 并写明理由:{bypass}"
    )


def test_whole_chat_page_never_shows_a_bare_discarded_code():
    """整页行为断言:同一页上每一处 ``runtime_failed`` 都必须带说明。

    ⚠️ 这条来自 codex2 的 PR 复审反例(2026-08-22):
    「失败原因 Top」那格已经带了说明,而「最近 chat 任务」那格仍是裸的 ——
    **同一页两个同值,一个有说明一个没有**,读表的人只会看见离他最近的那个。

    ⭐ 它与上面那条源码扫描互补,缺一不可:
      源码扫描抓「有没有新出口绕过助手」——但只在写法长得像已知形状时有效;
      本条抓「**页面上真的还有没有裸值**」——不依赖任何写法。
    """
    report = {
        "outcomes": {}, "reply_delivery": {}, "failure_delivery": {},
        "reply_quality": {},
        "failure_reasons": [{"code": "runtime_failed", "count": 7}],
        "recent_jobs": [{"user_id": "usr_x", "status": "failed",
                         "last_error": "runtime_failed"}],
    }
    page = data_track._render_chat_reliability_page(report, within_hours=24)

    # 前提:这份构造确实让该值在页面上出现了不止一次 ——
    # 否则「每一处都带说明」可能只是因为只有一处。
    occurrences = page.count("runtime_failed")
    assert occurrences >= 2, f"构造未产生多处同值(仅 {occurrences} 处),本条无意义"

    assert page.count("原因已丢弃") == occurrences, (
        f"页面上 runtime_failed 出现 {occurrences} 次,"
        f"但只有 {page.count('原因已丢弃')} 次带说明"
    )
