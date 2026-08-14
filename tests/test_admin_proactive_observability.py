"""主动侧观测:V1 口径的 `proactive` 块必须自曝其短,V2 的真相必须自己有块。

2026-08-10 的教训:我拿 admin 数据面的 `proactive` 块去判一个 Runtime V2 用户,
看到 `heartbeat_jobs: 0` / `proactive_messages: 0` / `last_at` 停在十天前,
差点向 Seven 报「你的心跳十天没跑了」。真相是那个块读的是 **V1 的
`proactive_jobs` 日志**,而 V2 的唤醒 job 在 `agent_jobs`、回复行 source 恒为
`model_api` —— 它对 V2 用户结构性地看不见,0 是「没测量」不是「没发生」。

这里锁三件事:
  1. V1 口径的块**必须带 lens 标注**,不许再裸着一个会被读成故障的 0;
  2. `v2_wake_schedule` 那一行(决定发不发)必须可见,**且**给出「现在为什么
     不到期」的人话结论;
  3. 没有 schedule 行时不能显示成空 —— 「没有行」本身就是结论。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track as dt  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


def test_v1_proactive_block_declares_its_own_blind_spot():
    """光加新块不够:旧块那个 0 还摆在那儿骗人,必须自带口径标注。

    ⚠️ 断言必须打在 **production 的 `_with_proactive_lens`** 上。我第一版写成
    「测试自己造个 dict 再赋值再断言」,那样 production 把标注删了也照样绿 ——
    正是 TESTING.md 里「守卫本身在撒谎」那条。
    """
    out = dt._with_proactive_lens({"heartbeat_jobs": 0, "proactive_messages": 0})

    assert out["lens"] == dt.PROACTIVE_V1_LENS
    # 标注要说清「0 是没测量而不是没发生」,以及去哪看真相,否则等于没标。
    assert "agent_jobs" in out["lens_note"]
    assert "v2_wake_activity" in out["lens_note"]
    # 原始计数不许被标注动过。
    assert out["heartbeat_jobs"] == 0 and out["proactive_messages"] == 0


def test_wake_schedule_missing_row_is_a_conclusion_not_a_blank(monkeypatch):
    monkeypatch.setattr(jobs_store, "get_wake_schedule", lambda uid: None)

    out = dt._v2_wake_schedule_detail("usr_x", {})

    assert out["present"] is False
    # 「没有行」= 心跳永远不会到期(due_heartbeat_users 要求 next_heartbeat_at 非空)。
    assert out["blocked_by"] == ["no_schedule_row"]
    assert out.get("note"), "没有行时必须给出人话结论,不能显示成空"


@pytest.mark.parametrize(
    "blockers",
    [[], ["not_due_yet"], ["unarmed"], ["payment_cooldown"], ["dnd"],
     ["proactive_backoff"]],
)
def test_blocked_by_is_taken_from_the_scheduler_s_own_predicate(monkeypatch, blockers):
    """`blocked_by` 必须来自 `heartbeat_due_diagnosis`(跑的是调度器同一份 SQL),
    而不是在 admin 侧用 Python 重写一遍规则。

    重写一遍的下场我第一版已经演示过:退避那条我算不出「真人发言逃生口」,只能给
    一个模棱两可的 `maybe_`,support 依旧判断不了 —— 又一个「看起来在测量、其实
    没有」的信号,正是本批要消灭的东西(codex 复验 2026-08-10 抓出)。
    """
    monkeypatch.setattr(
        jobs_store, "get_wake_schedule", lambda uid: {"next_heartbeat_at": 1.0})
    monkeypatch.setattr(
        jobs_store, "heartbeat_due_diagnosis",
        lambda uid, **kw: {"present": True, "blocked_by": list(blockers)})

    out = dt._v2_wake_schedule_detail("usr_x", {})

    assert out["blocked_by"] == blockers


def test_backoff_verdict_is_definite_not_a_maybe(monkeypatch):
    """退避要给**确定**结论。逃生口(失败之后用户又真人发过言)由 SQL 求值。"""
    monkeypatch.setattr(
        jobs_store, "get_wake_schedule",
        lambda uid: {"next_heartbeat_at": 1.0, "proactive_backoff_until": 4e12})
    monkeypatch.setattr(
        jobs_store, "heartbeat_due_diagnosis",
        lambda uid, **kw: {"present": True, "blocked_by": ["proactive_backoff"]})

    out = dt._v2_wake_schedule_detail("usr_x", {})

    assert out["blocked_by"] == ["proactive_backoff"]
    assert not any(b.startswith("maybe_") for b in out["blocked_by"]), out["blocked_by"]


def test_no_fabricated_schedule_columns(monkeypatch):
    """只许报 `v2_wake_schedule` **真实存在**的列。

    我第一版凭印象加了 `next_scheduled_at` —— 那一列根本不存在,`get_wake_schedule`
    也从不返回它,于是永远是空字符串。而一个空值会被 support 读成「这个用户没有
    定时任务」。**这与本批要修的病是同一种**,只是这次是我自己造的
    (codex 复验 2026-08-10 抓出)。
    """
    monkeypatch.setattr(
        jobs_store, "get_wake_schedule",
        lambda uid: {"next_heartbeat_at": 1.0, "next_capture_at": 2.0})
    monkeypatch.setattr(
        jobs_store, "heartbeat_due_diagnosis",
        lambda uid, **kw: {"present": True, "blocked_by": []})

    out = dt._v2_wake_schedule_detail("usr_x", {})

    assert "next_scheduled_at" not in out, "又造了一个永远为空的假字段"
    # 真实存在的列要给出来(第一版把 next_capture_at 漏了)。
    assert out["next_capture_at"], out
    # 定时任务不在这张表里,要指明真源而不是留空让人误读。
    assert "scheduled" in out["scheduled_timers_source"]


def test_wake_activity_reader_never_500s_the_page(monkeypatch):
    """观测面永远不许把页面打挂——与既有 `_v2_chat_failures_detail` 同姿态。"""
    def _boom(*a, **k):
        raise RuntimeError("db is down")

    monkeypatch.setattr(jobs_store, "wake_lane_activity_for_user", _boom)
    out = dt._v2_wake_activity_detail("usr_x")
    assert "error" in out and "RuntimeError" in out["error"]

    monkeypatch.setattr(jobs_store, "get_wake_schedule", _boom)
    out2 = dt._v2_wake_schedule_detail("usr_x", {})
    assert "error" in out2


def test_wake_lane_vocabulary_matches_the_v2_runtime_lanes():
    """support 查询覆盖的 lane 必须与 worker 的 `_WAKE_LANES` 一致。

    从被测模块读,不写死清单:V2 以后新增一条唤醒道而这里没跟,用户就会又一次
    「明明跑了却在管理端看不见」。
    """
    from model_api_runtime.v2 import worker as v2_worker

    assert set(jobs_store.WAKE_LANES_FOR_SUPPORT) == set(v2_worker._WAKE_LANES)


def test_heartbeat_due_diagnosis_sql_actually_runs_against_the_real_schema():
    """把那条 SQL 真的打到库上跑一遍。

    上面所有 blocked_by 用例都 monkeypatch 掉了 `heartbeat_due_diagnosis`,
    于是**它的 SQL 本身一次都没被执行过** —— 列名写错、`_LATEST_GENUINE_USER_SEQ_SQL`
    拼接处少个空格,单测全绿而线上第一次调用就炸。这正是「守卫没有覆盖它声称覆盖
    的东西」。

    这里不追求断言语义,只要求:真实 schema 上能跑通,且未接管用户返回 present=False。
    """
    import conftest
    import db
    from accounts import registry

    uid = "u_wake_due_sql"
    conftest.seed_user(uid, created_at="2026-08-14T00:00:00+00:00")

    try:
        with db.get_pool().connection() as conn:
            created_at = conn.execute(
                "SELECT created_at FROM users WHERE user_id=%s",
                (uid,),
            ).fetchone()[0]
        assert created_at == "2026-08-14T00:00:00+00:00"

        # 未被调度器接管过 → 无行 → present False(顺带证明 SQL 能执行)。
        assert jobs_store.heartbeat_due_diagnosis(uid) == {"present": False}

        # 接管之后:武装一个已到期的心跳,应当没有任何 blocker。
        jobs_store.upsert_wake_schedule(uid, next_heartbeat_at=1.0)
        armed = jobs_store.heartbeat_due_diagnosis(uid)
        assert armed["present"] is True
        assert armed["blocked_by"] == [], armed

        # 未到期 → 唯一 blocker 是 not_due_yet(4e12 epoch ≈ 公元 128000 年)。
        jobs_store.upsert_wake_schedule(uid, next_heartbeat_at=4e12)
        later = jobs_store.heartbeat_due_diagnosis(uid)
        assert later["blocked_by"] == ["not_due_yet"], later
    finally:
        try:
            with db.get_pool().connection() as conn:
                conn.execute(
                    "DELETE FROM v2_wake_schedule WHERE user_id=%s",
                    (uid,),
                )
                conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))
        finally:
            with registry._users_lock:
                registry._users[:] = [
                    row
                    for row in registry._users
                    if row.get("user_id") != uid
                ]

    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_wake_schedule WHERE user_id=%s",
            (uid,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM users WHERE user_id=%s",
            (uid,),
        ).fetchone()[0] == 0
    with registry._users_lock:
        assert not any(row.get("user_id") == uid for row in registry._users)


def test_worldbook_stats_counts_without_leaking_entry_names():
    """世界书只出数量与时间,**绝不出条目 id**。

    id 是用户自己起的名字(「月光咖啡馆」这种)—— 那是内容,不该出现在支持面板里。
    正文本身端到端加密,服务端读不到,不用额外防。

    为什么要有这个字段:2026-08-11 usr_99c3eca0 先报「世界书保存失败」、再报
    「问它世界书内容命中不了」。管理端当时连**有几条**都看不到,只能去下客户端
    诊断日志、数 attestation 与封装失败的比例(1 : 247)才反推出「他一条都没存进去」。
    有了它,「没存进去」和「存了没匹配上」一眼可分——那是两条完全不同的排查路径。
    """
    class _FakeStore:
        world_books_lock = __import__("threading").Lock()
        world_books = [
            {"id": "月光咖啡馆", "updated_at": "2026-08-11T10:00:00", "body_ct": "..."},
            {"id": "青岚学院", "updated_at": "2026-08-11T12:00:00", "body_ct": "..."},
        ]

    out = dt._worldbook_stats(_FakeStore())

    assert out["entries"] == 2
    assert out["last_updated_at"] == "2026-08-11T12:00:00"
    blob = __import__("json").dumps(out, ensure_ascii=False)
    assert "月光咖啡馆" not in blob and "青岚学院" not in blob, "条目名泄漏了"
    assert "body_ct" not in blob


def test_worldbook_stats_distinguishes_empty_from_broken():
    """零条目要显式是 0,不是缺字段——「没存进去」本身就是结论。"""
    class _EmptyStore:
        world_books_lock = __import__("threading").Lock()
        world_books = []

    out = dt._worldbook_stats(_EmptyStore())
    assert out["entries"] == 0
    assert out["last_updated_at"] == ""

    class _BoomStore:
        @property
        def world_books_lock(self):
            raise RuntimeError("db down")

    boom = dt._worldbook_stats(_BoomStore())
    assert "error" in boom, "读不到时要显式报错,不能装成 0 条"
