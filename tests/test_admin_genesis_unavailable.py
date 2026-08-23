"""genesis 读取失败 ≠ 这个用户没有蒸馏(T245 域2)。

改这里之前,`db.genesis_list_jobs` 抛异常会被折成 `jobs=[] / job_count=0`,
而页面那张卡显示 `status or 'none'` ——
**「量不到」和「量到了零」在管理端逐字同形**,
而它们的下一步相反:前者去修读取,后者就是正常。

⚠️ 这一处的位置最坏:它在**我们用来看见故障的那个面上**。
量具自己坏掉时,它显示的是「一切正常」。

本文件两个方向一起钉:
既要「失败时看得出来」,也要「**正常时不许乱报失败**」——
只钉前者的话,把那张卡改成永远显示「取不到」,那一半照样绿。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track  # noqa: E402


def _text(html_str: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_str)


def _user(genesis: dict) -> dict:
    """最小的用户详情 payload;只填这张页面必读的字段。"""
    return {
        "user_id": "usr_x", "principal_id": "p", "route": "model_api",
        "onboarding": {"stage": "done", "steps_done": 1, "steps_total": 1,
                       "stuck_for_sec": 0},
        "chat": {"total": 0}, "memory": {"total": 0},
        "proactive": {"proactive_messages": 0},
        "genesis": genesis,
        "responder": {},
    }


def test_read_failure_and_genuinely_absent_do_not_look_the_same():
    """两种状态在页面上必须不同形 —— 这是本单的全部要点。"""
    failed = _text(data_track._render_user_detail_page(
        _user({"status": "", "job_count": 0, "jobs_source": "unavailable"})))
    absent = _text(data_track._render_user_detail_page(
        _user({"status": "", "job_count": 0, "jobs_source": "ok"})))

    # 前提:两份构造的 status/job_count **完全相同**,唯一差别是 jobs_source ——
    # 否则「显示不同」可能来自别的字段,这条测试就不再证明我要证明的事。
    assert failed != absent

    assert "取不到" in failed
    assert "读取失败" in failed
    # 反向:正常态不许出现失败措辞,否则这个标记不再携带信息。
    assert "取不到" not in absent
    assert "none" in absent


def test_missing_marker_is_treated_as_ok_not_as_failure():
    """旧 payload(没有 jobs_source 这个键)不许被误报成失败。

    ⚠️ 兼容性方向也要钉:一个只在新代码里存在的标记,
    如果缺失时默认判失败,**所有历史/外部 payload 会集体变成红的**,
    而那是假故障 —— 比漏报更糟,因为它会淹掉真故障。
    """
    legacy = _text(data_track._render_user_detail_page(
        _user({"status": "completed", "job_count": 3})))
    assert "取不到" not in legacy
    assert "completed" in legacy


def test_stats_marks_source_unavailable_when_the_query_raises(monkeypatch):
    """产生这个标记的那一层:查库抛异常 ⇒ jobs_source=unavailable。"""
    class _Store:
        user_id = "usr_x"

    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(data_track.db, "get_blob", lambda *a, **k: {})
    monkeypatch.setattr(data_track.db, "genesis_list_jobs", _boom)

    out = data_track._genesis_stats(_Store())

    # 前提:构造确实让它走了异常分支(job_count 归零),否则下面的断言没有意义。
    assert out["job_count"] == 0
    assert out["jobs_source"] == "unavailable"


def test_stats_marks_source_ok_on_a_genuinely_empty_result(monkeypatch):
    """反向:查库成功但确实没有任务 ⇒ jobs_source=ok,job_count 同样是 0。

    ⭐ 这条和上一条的 `job_count` **完全相同**,唯一差别是 jobs_source ——
    它就是本单要建立的那个区分本身。
    """
    class _Store:
        user_id = "usr_x"

    monkeypatch.setattr(data_track.db, "get_blob", lambda *a, **k: {})
    monkeypatch.setattr(data_track.db, "genesis_list_jobs", lambda *a, **k: [])

    out = data_track._genesis_stats(_Store())

    assert out["job_count"] == 0
    assert out["jobs_source"] == "ok"


# --------------------------------------------------------------------------- #
# T245 域12:漏斗的字段级 —— 「本来就没有」与「有值但读不出来」必须不同形
# --------------------------------------------------------------------------- #

def _funnel(count_value) -> dict:
    """两阶段漏斗;第二阶段的 count 由调用方给,用来区分三种情形。"""
    return {
        "window_days": 28,
        "stages": [
            {"id": "a", "label": "注册", "count": 100},
            {"id": "b", "label": "首答", "count": count_value},
        ],
    }


def test_funnel_absent_count_and_unreadable_count_do_not_look_the_same():
    """原本这两种都渲染成 `—`,连 title 都写着「尚未走完**或**查询失败」。

    ⭐ 那句 title 是**作者自己写下的证据**:他知道两件事被混在一起了,只是没分开。
    它们的下一步相反 —— 前者什么都不用做,后者要去修数据。
    """
    absent = _text(data_track._render_funnel(_funnel(None), compact=True))
    unreadable = _text(data_track._render_funnel(_funnel("abc"), compact=True))

    # 前提:两份构造除了那个 count 值以外完全相同,否则「显示不同」可能来自别处。
    assert absent != unreadable

    assert "坏值" in unreadable
    # 反向:合法缺值不许被报成坏值,否则这个标记不携带信息。
    assert "坏值" not in absent
    assert "—" in absent


def test_funnel_still_renders_normal_counts_and_does_not_crash_on_bad_value():
    """坏值不许把整段漏斗炸掉 —— 观测面永不因一个坏字段而 500。

    ⚠️ 这条是配套的反向保护:我第一版改法让 `_count` 返回哨兵对象,
    而下游有 7 处直接拿它做除法/减法 —— **那会把「显示得更诚实」变成「整页崩掉」**。
    最终改法刻意最小侵入:`_count` 仍返回 None,只在显示处加独立判据。
    """
    ok = _text(data_track._render_funnel(_funnel(42), compact=True))
    assert "42" in ok
    assert "坏值" not in ok

    # 坏值那份必须照常渲染出第一阶段,不抛异常
    bad = _text(data_track._render_funnel(_funnel("abc"), compact=True))
    assert "100" in bad


# --------------------------------------------------------------------------- #
# T245 域14/15:安全信号本身不许因为坏数据而消失
# --------------------------------------------------------------------------- #

def test_bad_coverage_does_not_make_the_low_coverage_warning_vanish():
    """⚠️ 本文件里最危险的一条。

    改之前:`coverage` 坏值 → `float()` 抛 → `except: pass` → 警告字符串保持空
    ⇒ **「usage 缺报较多,以上都是已知下限」整句消失**,
      页面反而显得比数据健康时更干净。

    ⭐ **数据坏了,安全信号跟着一起没了** —— 这比「显示一个错的数」更危险:
      错的数还在提醒你这里有个量;**消失的警告让你以为这里没有问题。**
    """
    low = _text(data_track._home_cost_section({"coverage": 0.5}))
    bad = _text(data_track._home_cost_section({"coverage": "abc"}))
    healthy = _text(data_track._home_cost_section({"coverage": 0.99}))

    # 前提:低覆盖那份确实触发了原有警告,否则本条无从对比。
    assert "缺报较多" in low

    # 坏值不许静默 —— 必须有话说,且与「健康」明显不同。
    assert "读不出来" in bad
    assert bad != healthy

    # 反向:健康时不许乱报,否则这两个标记都不再携带信息。
    assert "读不出来" not in healthy
    assert "缺报较多" not in healthy


def test_missing_coverage_is_not_reported_as_unreadable():
    """coverage 缺失(合法的「本来就没有」)不许误报成「读不出来」。

    与 genesis 的 test_missing_marker_is_treated_as_ok_not_as_failure 对称:
    cost section 此前只钉了坏值方向,缺失方向没人喂 ——
    删掉 `coverage is not None and` 的突变可以在全绿下存活
    (float(None) 抛 TypeError 落进 except,合法缺失被渲染成坏数据警告)。
    """
    missing = _text(data_track._home_cost_section({}))

    # 前提(防恒真):同一函数、坏值那份确实产出「读不出来」,
    # 否则下面的阴性断言在文案改词后会静默变成永真。
    bad = _text(data_track._home_cost_section({"coverage": "abc"}))
    assert "读不出来" in bad

    # 缺失 ≠ 读不出来。
    assert "读不出来" not in missing
    # 缺失也不触发低覆盖警告 —— 那句是给「量到了且低」的。
    assert "缺报较多" not in missing


def test_unreadable_cohort_week_is_not_folded_into_immature():
    """域15:读不出是哪一周 ≠ 这一周还没走完。

    ⭐ 原代码那句注释本身就说明了它们该分开:
    「注册 3 天内还没回复 ≠ 不会回复」是**关于时间的陈述** ——
    而坏日期根本不是那回事,是我们连是哪一周都不知道。
    前者等一等就好,后者要去修数据。
    """
    assert data_track._health_t3_matured("2020-01-06") is True     # 早就成熟
    assert data_track._health_t3_matured("2099-01-06") is False    # 还没走完
    # 关键:坏值既不是 True 也不是 False
    assert data_track._health_t3_matured("not-a-date") is None
    assert data_track._health_t3_matured(None) is None


def test_unreadable_cohort_week_renders_differently_from_immature():
    """域15 的**渲染层**:坏值那一行必须与「未成熟」那一行不同形。

    ⚠️ 这条是补上来的:我第一版只测了产生层(`_health_t3_matured` 返回 None),
    突变「消费方把 None 并进未成熟」**全绿** ——
    也就是我证明了「这一层分得开」,却没证明「页面上真的分开了」。
    ⭐ 同族于本单要治的病本身:**内部区分了,不等于看得见。**
    """
    def _act(week: str) -> dict:
        return {"cohorts": [{
            "cohort_week": week, "registered": 10, "coverage_complete": True,
        }]}

    immature = _text(data_track._health_activation_table(_act("2099-01-06")))
    unreadable = _text(data_track._health_activation_table(_act("not-a-date")))

    # 前提:两份构造只差 cohort_week 这一个字段。
    assert immature != unreadable
    assert "坏值" in unreadable
    # 反向:合法的「还没走完」不许被报成坏值。
    assert "坏值" not in immature


# --------------------------------------------------------------------------- #
# T245 域13:「省略型」—— 一句话从页面上蒸发,而缺席不留痕迹
# --------------------------------------------------------------------------- #

def test_unreadable_retention_says_so_instead_of_dropping_the_whole_clause():
    """⭐ 省略型是本单最隐蔽的一类。

    改之前:`d14.pct` 坏值 → `except: pass` → 「新来 100 个能留住 N 个」**整句消失**,
    而读的人**无从知道这里本该有一句**。
    它既不返回 0 也不返回 None —— **它让一条陈述消失,而缺席不留痕迹。**
    """
    good = data_track._home_human_summary(
        {"wau": 10, "prev_wau": 10}, {"curve": {"d14": {"pct": 42}}}, None)
    bad = data_track._home_human_summary(
        {"wau": 10, "prev_wau": 10}, {"curve": {"d14": {"pct": "abc"}}}, None)

    # 前提:正常那份确实产生了这句话,否则本条无从对比。
    assert "留住" in good
    assert "42" in good

    assert "读不出来" in bad
    # 反向:正常时不许乱报。
    assert "读不出来" not in good


def test_no_previous_week_and_unreadable_previous_week_differ():
    """首周(本来就没有上一周)与「上一周读不出来」必须不同形。"""
    first_week = data_track._home_human_summary({"wau": 10, "prev_wau": None},
                                                None, None)
    unreadable = data_track._home_human_summary({"wau": 10, "prev_wau": "abc"},
                                                None, None)

    # 前提:两份的 wau 相同,唯一差别是 prev_wau。
    assert first_week != unreadable
    assert "比不了" in unreadable
    assert "比不了" not in first_week
    # 首周不该出现任何对比从句
    assert "比上一周" not in first_week
