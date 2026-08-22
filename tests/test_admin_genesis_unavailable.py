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
