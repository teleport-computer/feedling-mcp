"""Chat verdict: Clear-Chat 遮蔽的 missing 必须进「判不了」，不进红也不进绿。

背景（T361）：``chat_clear`` 整表删掉 ``v2_effect_outbox``，却只把**在飞的** job 置
``superseded``，于是一个历史上真的送达过的 job 会退化成「无 final applied 证据」，
**与真缺陷在这张表上完全同形**。把它算进红 = 把合规删除报成缺陷；算进绿 = 用不
确定性洗白真缺陷。所以它只能单列成第三档。

⚠️ 这个退化是**有界**的，别写成「永久」：它只在该 job 行仍然存在、且仍落在报表
窗口内的这段时间里成立。删号会经 ``users`` 的 FK 级联把 ``agent_jobs`` 行一起带走
（主体与证据一并消失，不再产生假缺陷），窗口滚过去之后它也不再被这张报表统计。
我最初写的「永久退化 / job 永不消失」是**没核 FK 子句就下的断言**，已按复核更正。

第三档之外还有第三个桶：**本就不欠回复**。有几条合法的完成路径结构上就不产生
final effect，它们进红是**假红**，而假红正是本单要治的病。每条路径配它自己的
content-free 痕，不是一个信号盖全部：

===========================  ==========================================
路径                          痕
===========================  ==========================================
空 coalesced（worker 的       ``v2_turn_metrics`` 有行且 ``model_calls=0``
「没有未回复消息」分支）        ``AND failed=false``
                              ``AND status=CHAT_TURN_STATUS_OK``
迟到输入交接（worker 的        ``v2_turn_metrics.status``
``input_advanced`` 分支）       = ``CHAT_INPUT_ADVANCED_HANDOFF_STATUS``
legacy final 重生（outbox     ``agent_jobs.last_error``
游标的交接）                    = ``LEGACY_FINAL_REGENERATION_REASON``
===========================  ==========================================

⚠️ 裸 ``model_calls=0`` **不够**：worker 的 slot 兜底恢复路径也硬写 ``model_calls=0``，
而那个 0 的意思是「不知道这轮跑到哪了」，不是「真的没调过模型」。⭐ 一个哨兵 0 和
一个真实测量 0 在这一列里完全同形，而被洗白的恰恰是**出过事的那批** ⇒ 方向是假绿。
它同时写 ``failed=True`` 且 status 是错误串，所以谓词要带上 ``failed IS NOT TRUE``
**和** ``status = CHAT_TURN_STATUS_OK`` 两道：``failed`` 这一格自己也是 best-effort，
不能只靠它单独承重。

⚠️ 这个信号的失效方向：一条「本该调模型却静默跳过、还判成功」的缺陷会被
``model_calls=0`` 洗成绿。今天枚举过的 chat 完成路径里没有这一条，但**这是它的
假绿方向**，将来加完成路径时要回来看这一格。

⛔ ``voice_call_ended``（``worker.py:16191``）长得像同族但**没进**谓词：我没核它欠不
欠回复，没核的不进。它没有停在这段注释里 —— 它是 **T389** 的首个待判项。T389 沿
``completed`` 出口轴做 AST 机械枚举，因为本单这四条是沿**症状**（哪些写点会让分子
多一行）找的，而症状轴对「症状不同、语义相同」的出口结构性失明：第四条就是这么
漏掉又找回来的。⭐ 闭合论证的形式是「我扫了这个语法结构的全部实例」，不是「我找
不到更多了」。

已核、不适用（记在这里免得下次有人再查一遍）：
``jobs_store.finish_empty_failure_review_runner`` 的 lane 绑的是
``_TRAJECTORY_REVIEW_LANE``，不是 ``chat`` ⇒ 它写的 completed 行压根进不了这张
报表的分子。

------------------------------------------------------------------
done_with_debt（督导终裁原文，逐字保留）
------------------------------------------------------------------

**A 哪里不干净**：分子的记账单位是 ``agent_jobs.id``，而被测命题的单位是「用户的
一条消息」。coalescing/handoff 让一条消息横跨多个 job ⇒ 本单是**按症状逐条打补丁**，
不是根因修复。

**B（为什么现在不拆）**：换记账单位（``agent_jobs.id`` → 用户的一条消息）要重做
分子的取数，不在本单范围。
⚠️ 而且**残留量今天不可陈述为一个数**：本单先后收进 3 条路径，第 4 条是在我（督导）
已经判定「三条即全集」**之后**才被找到的。两次「已知集合」都只是**搜索停下来的地方**，
不是穷举到的边界 —— 我批准前者时手里没有闭合论证，只有收敛的观感。
⇒ 残留 = 「chat 道上能让 job 走到 ``completed`` 而用户那条消息未被回」的**全部出口中，
尚未被机械枚举过的部分**；该集合大小未知。已被指出、尚未核实的候选：
``voice_call_ended``（``worker.py:16191``）。
⭐ 每多一条这样的路径就多一个**假红**，而补痕的成本是常数、收益是一次性的 ——
这正是「记账单位选错了」（A）的证据，**不是**对 A 的反驳。

**C 什么条件下值得拆**：再出现第 5 条合法 completed-chat 路径被判 bad 时，或这个
指标要被用来做发版判断时（那时假红会变成阻断）。
出口枚举（见台账 T389）若交回的出口总数显著多于已补的四条，
则 A 从「将来该做」升为「现在就做」——**判据是出口数，不是又出现了几次假红**，
因为假红是要等它发生的，而出口数现在就能数。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track as dt  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


def _report(
    *,
    missing: int = 0,
    shadowed: int = 0,
    benign: int = 0,
    reconciliation: int = 0,
    recent_jobs: list | None = None,
    extra_delivery: dict | None = None,
) -> dict:
    delivery = {
        "final_effect_jobs": 10 - missing,
        "final_effect_rows": 10 - missing,
        "final_applied_jobs": 10 - missing,
        "final_pending_jobs": 0,
        "final_reconciliation_jobs": reconciliation,
        "final_discarded_jobs": 0,
        "duplicate_final_effect_jobs": 0,
        "completed_without_final_applied": missing,
        "completed_without_final_applied_users": min(missing, 2),
        "completed_without_final_applied_benign": benign,
        "completed_without_final_applied_clear_shadowed": shadowed,
    }
    delivery.update(extra_delivery or {})
    return {
        "window_hours": 24,
        "outcomes": {
            "admitted": 10, "started": 10, "completed": 10,
            "failed": 0, "expired": 0, "superseded": 0,
            "in_flight": 0, "users": 4,
        },
        "reply_delivery": delivery,
        "failure_delivery": {
            "failure_rows": 0, "fallback_reply_delivered": 0,
            "fallback_reply_pending": 0, "error_status_delivered": 0,
            "runtime_error_delivered": 0,
        },
        "reply_quality": {"multi_reply_turns": 0, "empty_reply_failures": 0},
        "settled_jobs": 10,
        "terminal_completion_rate": 1.0,
        "server_final_reply_applied_rate": 1.0,
        "latency": {"server_applied_p95_sec": 10},
        "model_breakdown": [],
        "failure_reasons": [],
        "recent_jobs": recent_jobs if recent_jobs is not None else [],
        "client_delivery_ack": None,
        "provider_attempt_accounting": None,
    }


def _joined(reasons: list[str]) -> str:
    return "；".join(reasons)


def _metric_cell(label: str, html: str) -> str:
    """按 label 取出 ``_render_metric`` 那一格的值（值在 label 之前）。"""
    marker = f"<div class='metric-label'>{label}"
    assert marker in html, f"页面上没有 {label!r} 这一格"
    head = html.split(marker)[0]
    return head.rsplit("<div class='metric-value'>", 1)[1].split("</div>")[0]


# --- 五格：每一格喂一次，锚 level 与 reason ---------------------------------


def test_cell_benign_missing_is_not_red_and_is_not_undecidable():
    """E1 那一族：进了 missing，但结构上本就不欠回复 ⇒ 不许判红。

    ⛔ 这一格**不许**靠喂 ``missing=0`` 来过 —— 那是把待证命题当输入喂进来，
    绕开被测代码，什么也证明不了（我第一版就是这么写的，被复核打回）。这里
    必须让它真的进分子（``missing`` 非零），再断言它被 benign 桶接走。
    「它到底进不进分子」由 DB 级测试锚，见
    ``test_ops_dashboard_queries.py`` 的 benign 三条路径用例。
    """
    level, reasons = dt._ops_chat_level(_report(missing=3, shadowed=0, benign=3))
    assert level != "bad", "本就不欠回复的 completed 不许判红"
    text = _joined(reasons)
    assert "没有 final reply server-applied 证据" not in text
    assert "判不了" not in text, "它不是判不了，是根本没欠"


def test_cell_benign_does_not_eat_the_real_defect_next_to_it():
    """混合：benign 只减自己那份，剩下的照旧判红。

    这一格拦的是「一个宽谓词把同窗口的真缺陷一起洗白」——benign 越权就在这里红。
    """
    level, reasons = dt._ops_chat_level(_report(missing=5, shadowed=0, benign=2))
    assert level == "bad"
    assert "3 个 completed 没有 final reply server-applied 证据" in _joined(reasons)


def test_three_buckets_are_disjoint_and_sum_to_the_total():
    """红 + 判不了 + 本就不欠 == 总量，且没有一个是负数。"""
    for missing, shadowed, benign in (
        (10, 3, 2), (10, 0, 0), (10, 10, 0), (10, 0, 10), (7, 4, 3),
    ):
        red, undecidable, no_debt = dt._chat_missing_split(
            {
                "completed_without_final_applied": missing,
                "completed_without_final_applied_clear_shadowed": shadowed,
                "completed_without_final_applied_benign": benign,
            }
        )
        assert min(red, undecidable, no_debt) >= 0, (missing, shadowed, benign)
        assert red + undecidable + no_debt == missing, (missing, shadowed, benign)


def test_benign_wins_over_shadowed_when_upstream_double_counts():
    """口径万一让同一条 job 两个桶都算上，也不许把红减成负数。

    SQL 侧已经做成互斥（benign 优先），这里是第二道：纯算术不许溢出。
    """
    assert dt._chat_missing_split(
        {"completed_without_final_applied": 3,
         "completed_without_final_applied_clear_shadowed": 3,
         "completed_without_final_applied_benign": 3}
    ) == (0, 0, 3)


def test_cell_clear_shadowed_missing_is_neither_green_nor_red():
    """⭐ 第三档。断言「不是红」是不够的 —— 必须同时断言「不是绿」。"""
    level, reasons = dt._ops_chat_level(_report(missing=3, shadowed=3))
    assert level != "bad", "被 Clear 遮蔽的 missing 不许判红"
    assert level != "ok", "判不了不是绿"
    text = _joined(reasons)
    assert "3 个 completed 判不了" in text
    # 全部被遮蔽时不许再冒出任何一条红理由
    assert "没有 final reply server-applied 证据" not in text


def test_cell_unshadowed_missing_is_red():
    """无 Clear 痕的 missing 是干净信号 ⇒ 红。这是本单要保住的那一格。"""
    level, reasons = dt._ops_chat_level(_report(missing=3, shadowed=0))
    assert level == "bad"
    text = _joined(reasons)
    assert "3 个 completed 没有 final reply server-applied 证据" in text
    assert "无 Clear Chat 痕" in text
    assert "判不了" not in text


def test_cell_mixed_reports_red_and_undecidable_separately():
    """混合窗口：红与判不了必须各报各的数，不许合并成一个总量。"""
    level, reasons = dt._ops_chat_level(_report(missing=5, shadowed=2))
    assert level == "bad"
    text = _joined(reasons)
    assert "3 个 completed 没有 final reply server-applied 证据" in text
    assert "2 个 completed 判不了" in text


def test_cell_g_needs_reconciliation_is_red_regardless_of_clear():
    """G：needs_reconciliation 是证据在场且状态为坏 ⇒ Clear 遮蔽与它无关。"""
    level, reasons = dt._ops_chat_level(
        _report(missing=2, shadowed=2, reconciliation=1)
    )
    assert level == "bad"
    assert "1 个 final reply 需要人工 reconcile" in _joined(reasons)


def test_cell_h_outside_whitelist_stays_red():
    """H（写了非白名单 effect_type）仍然判红。

    白名单过滤发生在 SQL 的 CTE 里，H 的 effect 行在聚合中已经被滤掉，于是它在
    **effect 这一面**与「压根没写过 effect 行」产生完全相同的签名，一起落进
    missing。把它捞出来的不是 effect 面，是 benign 面：H 调过模型、也没留下任何
    一条交接痕 ⇒ 三个 benign 分支它一个都不命中 ⇒ 留在红里。
    ⇒ 本测试锚的是「它仍然判红」，**不是**「我们能把它认出来」。
    """
    level, reasons = dt._ops_chat_level(_report(missing=1, shadowed=0))
    assert level == "bad"
    assert "1 个 completed 没有 final reply server-applied 证据" in _joined(reasons)


# --- 派生只有一处 -----------------------------------------------------------


def test_split_is_clamped_when_upstream_counts_disagree():
    """遮蔽数是 missing 的子集谓词算出来的；口径万一漂了也不许让红变成负数。"""
    assert dt._chat_missing_split(
        {"completed_without_final_applied": 2,
         "completed_without_final_applied_clear_shadowed": 7}
    ) == (0, 2, 0)


def test_page_renders_all_three_numbers():
    html = dt._render_chat_reliability_page(
        _report(missing=6, shadowed=2, benign=1), within_hours=24
    )
    assert "Completed 无 applied（判红）" in html
    assert "其中判不了" in html
    assert "其中本就不欠回复" in html
    assert _metric_cell("Completed 无 applied（判红）", html).startswith("3 / 涉及")
    assert _metric_cell("其中判不了（Clear Chat 已删证据）", html) == "2"
    assert _metric_cell("其中本就不欠回复（已交接/空回合）", html) == "1"


def test_page_never_shows_unknown_as_zero():
    """「未知不会显示成 0」是这一页的成文契约，新增的三格也归它管。"""
    report = _report(missing=0, shadowed=0)
    report["reply_delivery"]["completed_without_final_applied"] = None
    report["reply_delivery"]["completed_without_final_applied_clear_shadowed"] = None
    report["reply_delivery"]["completed_without_final_applied_benign"] = None
    html = dt._render_chat_reliability_page(report, within_hours=24)
    assert _metric_cell("Completed 无 applied（判红）", html).startswith("—")
    assert _metric_cell("其中判不了（Clear Chat 已删证据）", html) == "—"
    assert _metric_cell("其中本就不欠回复（已交接/空回合）", html) == "—"

    # 总量已知、遮蔽数取不到 ⇒ 红按整个总量报（偏红不偏绿），只有判不了那格是 —
    partial = _report(missing=4, shadowed=0)
    del partial["reply_delivery"]["completed_without_final_applied_clear_shadowed"]
    html2 = dt._render_chat_reliability_page(partial, within_hours=24)
    assert _metric_cell("Completed 无 applied（判红）", html2).startswith("4 / 涉及")
    assert _metric_cell("其中判不了（Clear Chat 已删证据）", html2) == "—"

    # 同理：benign 取不到时红也按偏红报，不许把未知当成 0 个 benign 去洗白
    partial_b = _report(missing=4, shadowed=0)
    del partial_b["reply_delivery"]["completed_without_final_applied_benign"]
    html3 = dt._render_chat_reliability_page(partial_b, within_hours=24)
    assert _metric_cell("Completed 无 applied（判红）", html3).startswith("4 / 涉及")
    assert _metric_cell("其中本就不欠回复（已交接/空回合）", html3) == "—"


# --- §6 隐私：这条路径只许放出那个整数 --------------------------------------


def test_offender_aggregate_never_puts_a_user_id_in_the_response_body():
    """⛔ 不许靠「我们记得不要 select 它」。

    喂一个**带 user_id 的聚合字段**进去（模拟将来有人把 per-user 明细挂到这个
    聚合上），断言它不会出现在响应体里。``recent_jobs`` 留空，页面上任何
    user_id 都只可能来自新增的聚合面。
    """
    report = _report(
        missing=5,
        shadowed=2,
        extra_delivery={
            "completed_without_final_applied_user_ids": ["usr_leaked_0001"],
        },
    )
    html = dt._render_chat_reliability_page(report, within_hours=24)
    assert "usr_leaked_0001" not in html
    assert "usr_" not in html
    # 只放出那个去重整数
    assert "涉及 2 用户" in html


# --- ⚠️ 判别量有出生日期，而它就在窗口边上 ---------------------------------


def test_failure_review_runner_stays_off_the_chat_lane():
    """「已核，不适用」这句话得有人守着，否则它只是注释。

    ``finish_empty_failure_review_runner`` 之所以不用配 benign 痕，唯一理由是它
    不落 chat lane。哪天它落了，这条断言必须先红 —— 不然它会静默变成一条新的
    假红路径，而假红零症状。
    """
    assert jobs_store._TRAJECTORY_REVIEW_LANE != "chat"


def test_chat_clear_discriminant_window_margin():
    """窗口上限一旦拉过判别量的余量，本测试必须红。

    ``chat_message_archive.cleared_at`` 随 0052 落地（2026-07-20）。更早发生的
    Clear 没有留下任何痕 ⇒ 那一段的 job 会退回「无 Clear 痕 + missing」⇒ **判红**。
    失效方向是假红（把合规删除报成缺陷，也就是本单正在修的病本身），不是假绿；
    但它没有任何症状，所以只能靠这道闸拦。

    ⛔ 两个常量都从 shipped 模块读，不许在这里手写 720 或日期 —— 写死了就变成
    「测试复述实现」，改实现时它会跟着一起改，什么也拦不住。
    """
    available_from = jobs_store.CHAT_CLEAR_ARCHIVE_AVAILABLE_FROM
    cap_hours = jobs_store.CHAT_RELIABILITY_MAX_WINDOW_HOURS
    margin_hours = (
        datetime.now(timezone.utc) - available_from
    ).total_seconds() / 3600.0
    assert cap_hours <= margin_hours, (
        f"chat 可靠性窗口上限 {cap_hours}h 已经越过 Clear 痕的可用余量 "
        f"{margin_hours:.0f}h（判别量自 {available_from.date()} 起才有）。"
        "窗口早段的 Clear 无痕可查，会被重新判成缺陷 —— 这个修复在那一段静默失效。"
    )
