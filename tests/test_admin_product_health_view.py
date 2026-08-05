"""产品健康 view: dispatch 扇出 + 渲染诚实性 + 8 个 db builder 的口径测试。"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from admin import admin_core  # noqa: E402
from conftest import seed_user  # noqa: E402

requires_pg = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="product-health builder tests require PostgreSQL",
)

_BJT = ZoneInfo("Asia/Shanghai")


def _bj_monday(dt: datetime):
    local = dt.astimezone(_BJT).date()
    return local - timedelta(days=local.weekday())


def _bj_noon(day) -> datetime:
    return datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=_BJT)


# 快照种子用远期/近期两个 cohort 周，清理按周删——快照表没有 user_id 列。
_SNAP_WEEK_RECENT = ( _bj_monday(datetime.now(timezone.utc)) - timedelta(weeks=3) ).isoformat()
_SNAP_WEEK_ANCIENT = ( _bj_monday(datetime.now(timezone.utc)) - timedelta(weeks=20) ).isoformat()


@pytest.fixture(autouse=True)
def _clean_health_rows():
    if not os.environ.get("DATABASE_URL"):
        yield
        return

    def clean() -> None:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM user_logs WHERE user_id LIKE 'u_health_%'")
            conn.execute("DELETE FROM chat_messages WHERE user_id LIKE 'u_health_%'")
            conn.execute("DELETE FROM v2_turn_metrics WHERE user_id LIKE 'u_health_%'")
            conn.execute("DELETE FROM agent_jobs WHERE user_id LIKE 'u_health_%'")
            conn.execute(
                "DELETE FROM retention_cohort_snapshot WHERE cohort_week IN (%s, %s)",
                (_SNAP_WEEK_RECENT, _SNAP_WEEK_ANCIENT),
            )
            conn.execute("DELETE FROM users WHERE user_id LIKE 'u_health_%'")
        from accounts import registry

        with registry._users_lock:
            registry._users[:] = [
                u
                for u in registry._users
                if not str(u.get("user_id") or "").startswith("u_health_")
            ]

    clean()
    try:
        yield
    finally:
        clean()


# --------------------------------------------------------------------------- #
# 渲染层：contract 形状的假 payload（与 db builder 冻结口径逐字段对齐）。
# --------------------------------------------------------------------------- #


def _fake_retention() -> dict:
    return {
        "periods": [1, 2, 4, 8],
        "cohorts": [
            {
                "cohort_week": "2026-07-20",
                "n": 6,
                "cells": {
                    1: {"pct": 50.0, "active": 3},
                    2: {"pct": 33.3, "active": 2},
                    4: None,
                    8: None,
                },
            }
        ],
    }


def _fake_activation() -> dict:
    return {
        "cohorts": [
            {
                "cohort_week": "2026-07-20",
                "n": 6,
                "t1": 5,
                "t2": 4,
                "t3": 3,
                "t3_rate": 0.5,
                "median_t0_t3_hours": 6.5,
                "coverage_complete": True,
            }
        ]
    }


def _fake_w4_split() -> dict:
    return {
        "cohorts": [
            {
                "cohort_week": "2026-06-22",
                "n_all": 8,
                "n_activated": 5,
                "w4_all": 2,
                "w4_activated": 2,
                "w4_rate_all": 0.25,
                "w4_rate_activated": 0.4,
            }
        ]
    }


def _fake_stickiness() -> dict:
    return {
        "window": {"start_day": "2026-07-27", "end_day": "2026-08-02"},
        "wau": 9,
        "avg_dau": 3.4,
        "dau_latest": 4,
        "stickiness": 0.38,
        "l_distribution": {1: 3, 2: 2, 3: 1, 4: 1, 5: 1, 6: 0, 7: 1},
    }


def _fake_concentration() -> dict:
    return {
        "sessions": {"total": 90, "top_decile": 40, "users": 9, "share": 0.44},
        "tokens": {"total": 120000, "top_decile": 70000, "users": 8, "share": 0.58},
    }


def _fake_growth() -> dict:
    return {
        "rows": [
            {
                "week": "2026-07-27",
                "new_registered": 3,
                "newly_activated": 2,
                "active": 8,
                "churned": 1,
                "resurrected": 1,
                "net_change": 2,
            },
            {
                "week": "2026-07-20",
                "new_registered": 2,
                "newly_activated": 1,
                "active": 7,
                "churned": None,
                "resurrected": None,
                "net_change": None,
            },
        ]
    }


def _fake_power() -> dict:
    return {
        "definition": {"min_jobs": 5, "streak_weeks": 4},
        "weekly": [
            {"week": "2026-07-27", "qualifying_users": 4, "power_users": 2},
            {"week": "2026-07-20", "qualifying_users": 3, "power_users": None},
        ],
        "current": 2,
        "monthly": [{"month": "2026-07", "power_users": 2}],
    }


def _fake_reply_rate() -> dict:
    return {
        "rows": [
            {
                "week": "2026-07-27",
                "proactive_msgs": 30,
                "replied_24h": 12,
                "users": 6,
                "reply_rate": 0.4,
            }
        ]
    }


_BUILDER_ATTRS: dict[str, tuple[str, object]] = {
    "retention": ("admin_product_health_weekly_cohort_retention", _fake_retention),
    "activation": ("admin_product_health_activation_weekly", _fake_activation),
    "w4_split": ("admin_product_health_w4_split", _fake_w4_split),
    "stickiness": ("admin_product_health_stickiness", _fake_stickiness),
    "concentration": ("admin_product_health_concentration", _fake_concentration),
    "growth": ("admin_product_health_growth_accounting_weekly", _fake_growth),
    "power": ("admin_product_health_power_users", _fake_power),
    "reply_rate": ("admin_product_health_proactive_reply_rate", _fake_reply_rate),
}


def _stub_health_builders(monkeypatch, counters: dict, **overrides) -> None:
    # raising=False：与 db 层并行落地，dispatch/渲染测试不依赖 db builder
    # 已存在——stub 即 contract。
    for name, (attr, payload_fn) in _BUILDER_ATTRS.items():
        fn = overrides.get(name)
        if fn is None:

            def fn(_name=name, _payload=payload_fn, **_kwargs):
                counters[_name] = counters.get(_name, 0) + 1
                return _payload()

        monkeypatch.setattr(admin_core.db, attr, fn, raising=False)


_H2_QUESTIONS = ("用户留下来了吗", "新用户能激活吗", "强度是真的吗", "还缺什么证据")


def test_health_page_sections_cache_and_clean_logs(monkeypatch, caplog):
    counters: dict[str, int] = {}
    _stub_health_builders(monkeypatch, counters)

    secret = "sekrit"
    with caplog.at_level(logging.INFO):
        page = admin_core.page_html(f"view=health&admin_key={secret}")
        cached = admin_core.page_html(f"view=health&admin_key={secret}")

    # 四个 <h2> 问题句：页面按「问题」而不是按「表」组织。
    assert page.count("<h2") >= 4
    for question in _H2_QUESTIONS:
        assert question in page

    # 每个数据区一个折叠口径说明 + 一个常开不折叠的证据缺口盒子
    # （缺口清单就是第四节的正文，不许藏进 <summary> 后面）。
    assert len(re.findall(r"<details[^>]*>\s*<summary>口径说明", page)) >= 3
    gap_chunk = page[page.find("还缺什么证据"):][:600]
    assert "note-box" in gap_chunk
    assert "<summary>" not in gap_chunk

    # 每块指标 tile 都有一行 '?' 口径 hint。
    assert "class='hint'" in page

    # 导航高亮：产品健康是当前页。
    assert re.search(r"aria-current='page'[^>]*>\s*产品健康", page) or re.search(
        r"aria-current='page'>产品健康", page
    )

    # 本页口径固定，自链接不许携带 hours 窗参（看起来可调实际不可调）。
    health_hrefs = [
        h for h in re.findall(r"href='([^']*)'", page) if "view=health" in h
    ]
    assert health_hrefs
    for href in health_hrefs:
        assert "hours=" not in href

    # 第二次请求走 60s 页面缓存：builder 一次都不再跑。
    assert "页面缓存" in cached
    assert counters == {name: 1 for name in _BUILDER_ATTRS}

    # 计时日志在，admin_key 不在——日志与缓存 key 都只见摘要不见明文。
    assert "[admin:perf]" in caplog.text
    assert secret not in caplog.text
    for key in admin_core._page_cache:
        assert re.fullmatch(r"[0-9a-f]{64}", key)
        assert secret not in key


def test_health_single_builder_failure_isolated(monkeypatch, caplog):
    counters: dict[str, int] = {}

    def boom(**_kwargs):
        raise RuntimeError("retention snapshot query exploded")

    _stub_health_builders(monkeypatch, counters, retention=boom)

    with caplog.at_level(logging.INFO):
        page = admin_core.page_html("view=health")

    # 失败的 builder 只降级自己的区块……
    assert "暂不可用" in page
    assert "admin product-health retention query failed" in caplog.text
    # ……其余区块照常渲染（w4 假 payload 的 cohort 周标签出现在页面上）。
    for question in _H2_QUESTIONS:
        assert question in page
    assert "2026-06-22" in page

    # 降级 ≠ 失败：页面照常进缓存。
    cached = admin_core.page_html("view=health")
    assert "页面缓存" in cached


def test_health_none_never_renders_zero(monkeypatch):
    counters: dict[str, int] = {}
    _stub_health_builders(
        monkeypatch,
        counters,
        **{name: (lambda **_kwargs: None) for name in _BUILDER_ATTRS},
    )

    page = admin_core.page_html("view=health")

    # 全部数据取不到：页面结构仍在，值一律是「—/未知/暂不可用」。
    for question in _H2_QUESTIONS:
        assert question in page
    assert ("—" in page) or ("未知" in page) or ("暂不可用" in page)
    # 绝不把「取不到」伪装成健康的 0：任何 tile 值都不许出现 0%。
    for value in re.findall(r"<div class='metric-value'>([^<]*)<", page):
        assert "0%" not in value
        assert value.strip() not in {"0", "0.0"}


def test_health_view_not_in_hours_param_set(monkeypatch):
    # 在**其他** ops 视图（带 hours/day 参数）里渲染导航：产品健康的链接
    # 不许把窗参捎带过去。
    monkeypatch.setattr(
        admin_core.db, "recent_genesis_import_health", lambda **_kwargs: None
    )
    page = admin_core.page_html("view=imports&hours=168&day=2026-08-01")

    health_hrefs = [
        h for h in re.findall(r"href='([^']*)'", page) if "view=health" in h
    ]
    assert health_hrefs  # 导航里必须有产品健康入口
    for href in health_hrefs:
        assert "hours=" not in href
        assert "day=" not in href


def _metric_value(page: str, label: str) -> str:
    m = re.search(
        r"<div class='metric-value'>([^<]*)</div>"
        rf"<div class='metric-label'>{re.escape(label)}",
        page,
    )
    assert m is not None, f"tile {label!r} not found"
    return m.group(1).strip()


def test_health_renderer_right_censoring_honesty():
    """进行中/未成熟的周不许当定论：t3 tile 跳过未成熟周、cum 只数覆盖完整
    的周、铁杆 headline 跳过本周、增长表本周行带进行中标注。"""
    from admin import data_track

    this_monday = _bj_monday(datetime.now(timezone.utc))
    cur = this_monday.isoformat()
    last_week = (this_monday - timedelta(weeks=1)).isoformat()
    matured = (this_monday - timedelta(weeks=3)).isoformat()

    activation = {"cohorts": [
        # 本周：覆盖完整但 t3 窗（注册周+3 天）没走完——0% 是右删失不是事实。
        {"cohort_week": cur, "n": 4, "t1": 0, "t2": 0, "t3": 0,
         "t3_rate": 0.0, "median_t0_t3_hours": None, "coverage_complete": True},
        # 覆盖不完整的周：t3=2 是部分下界，cum 不许把它当完整周计入。
        {"cohort_week": last_week, "n": 3, "t1": 2, "t2": 2, "t3": 2,
         "t3_rate": None, "median_t0_t3_hours": None, "coverage_complete": False},
        {"cohort_week": matured, "n": 8, "t1": 6, "t2": 5, "t3": 4,
         "t3_rate": 0.5, "median_t0_t3_hours": 6.0, "coverage_complete": True},
    ]}
    growth = {"rows": [
        {"week": cur, "new_registered": 1, "newly_activated": 0, "active": 2,
         "churned": None, "resurrected": None, "net_change": None},
        {"week": matured, "new_registered": 0, "newly_activated": 1, "active": 3,
         "churned": 1, "resurrected": 0, "net_change": -1},
    ]}
    power = {
        "definition": {"min_jobs": 5, "streak_weeks": 4},
        "weekly": [
            # 本周（进行中）：周初 0 是删失不是流失，headline 不许用。
            {"week": cur, "qualifying_users": 0, "power_users": 0},
            {"week": last_week, "qualifying_users": 3, "power_users": 3},
        ],
        "current": 0,
        "monthly": [],
    }

    page = data_track._render_product_health_page(
        None, activation, None, None, None, growth, power, None,
    )

    # t3 tile：跳过未成熟的本周，取成熟周的 50%——绝不渲染本周的假 0%。
    assert f"50.0%（{matured}）" in page
    assert f"0.0%（{cur}）" not in page
    # 激活表：未成熟 ≠ 未知 ≠ 0%。
    assert "未成熟" in page
    assert "未知" in page
    # cum 只数 coverage_complete：0（本周）+ 4（成熟周），不含 incomplete 的 2。
    assert _metric_value(page, "近 10 周累计激活数") == "4"
    # 铁杆 headline 取最新完整周（3），不取进行中的本周（0）。
    assert _metric_value(page, "最新完整周铁杆用户") == "3"
    # 增长表本周行有进行中标注。
    assert "（进行中）" in page


def test_health_cum_activated_not_zero_on_funnel_outage():
    """漏斗断供：所有 cohort coverage_complete=False、t3=0（契约里 t3 恒为
    int）——cum tile 必须是 —，不是自信的 0。"""
    from admin import data_track

    this_monday = _bj_monday(datetime.now(timezone.utc))
    activation = {"cohorts": [
        {"cohort_week": (this_monday - timedelta(weeks=k)).isoformat(),
         "n": 2, "t1": 0, "t2": 0, "t3": 0, "t3_rate": None,
         "median_t0_t3_hours": None, "coverage_complete": False}
        for k in range(3)
    ]}
    page = data_track._render_product_health_page(
        None, activation, None, None, None, None, None, None,
    )
    assert _metric_value(page, "近 10 周累计激活数") == "—"


# --------------------------------------------------------------------------- #
# db 层：8 个 builder 的口径测试（与 A 并行落地——写给冻结 contract）。
# --------------------------------------------------------------------------- #


def _insert_session(conn, user_id: str, ts: float, key: str) -> None:
    conn.execute(
        "INSERT INTO user_logs (user_id,stream,ts,item_key,doc) VALUES "
        "(%s,'tracking_events',%s,%s,'{\"type\":\"app_session_end\"}'::jsonb)",
        (user_id, ts, key),
    )


def _insert_chat(conn, user_id: str, msg_id: str, ts: float, role: str, source: str) -> None:
    conn.execute(
        "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES "
        "(%s,%s,%s,jsonb_build_object('role',%s::text,'source',%s::text))",
        (user_id, msg_id, ts, role, source),
    )


@requires_pg
def test_retention_reads_snapshot_only_frozen_math_and_horizon():
    with db.get_pool().connection() as conn:
        # 快照说 cohort 4 人、W1 活跃 2、W2 活跃 1；live 表里只种 1 个用户 +
        # 会把 live 算出完全不同数字的活动——builder 必须只信快照。
        conn.execute(
            "INSERT INTO retention_cohort_snapshot "
            "(cohort_week,period_index,cohort_size,active_count) VALUES "
            "(%s,1,4,2),(%s,2,4,1)",
            (_SNAP_WEEK_RECENT, _SNAP_WEEK_RECENT),
        )
        # 超出 12 周地平线的 cohort：有快照也不出现。
        conn.execute(
            "INSERT INTO retention_cohort_snapshot "
            "(cohort_week,period_index,cohort_size,active_count) VALUES (%s,1,9,9)",
            (_SNAP_WEEK_ANCIENT,),
        )
    cohort_day = datetime.fromisoformat(_SNAP_WEEK_RECENT)
    signup = _bj_noon(cohort_day + timedelta(days=1))
    seed_user("u_health_ret1", created_at=signup.astimezone(timezone.utc).isoformat())
    with db.get_pool().connection() as conn:
        _insert_session(conn, "u_health_ret1", (signup + timedelta(days=8)).timestamp(), "hret-1")

    payload = db.admin_product_health_weekly_cohort_retention()
    assert payload["periods"] == [1, 2, 4, 8]
    weeks = [c["cohort_week"] for c in payload["cohorts"]]
    assert _SNAP_WEEK_ANCIENT not in weeks  # 地平线约束
    assert weeks == sorted(weeks, reverse=True)  # newest first
    row = next(c for c in payload["cohorts"] if c["cohort_week"] == _SNAP_WEEK_RECENT)
    # 冻结格算术：n 与 active 均出自快照，pct = active/n。
    assert row["n"] == 4
    assert row["cells"][1] == {"pct": pytest.approx(50.0), "active": 2}
    assert row["cells"][2] == {"pct": pytest.approx(25.0), "active": 1}
    assert row["cells"][4] is None  # 没冻结的格是 None，不是编出来的 0
    assert row["cells"][8] is None

    # 删掉 live 用户：快照数字一个都不许动（deletion-proof，无 live 回退）。
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM user_logs WHERE user_id = 'u_health_ret1'")
        conn.execute("DELETE FROM users WHERE user_id = 'u_health_ret1'")
    assert db.admin_product_health_weekly_cohort_retention() == payload


@requires_pg
def test_activation_coverage_flips_false_on_funnel_outage(monkeypatch):
    signup = datetime.now(timezone.utc) - timedelta(days=10)
    seed_user("u_health_act1", created_at=signup.isoformat())
    week = _bj_monday(signup).isoformat()

    # funnel 整体挂掉（None）与漏行（[] 覆盖不了 SQL 注册数）都必须把
    # coverage_complete 打成 False——覆盖不完整时比率显示未知，不显示假 0。
    for broken in (None, []):
        monkeypatch.setattr(
            db, "admin_onboarding_funnel", lambda *_a, _v=broken, **_k: _v
        )
        payload = db.admin_product_health_activation_weekly()
        row = next(
            (c for c in payload["cohorts"] if c["cohort_week"] == week), None
        )
        assert row is not None
        assert row["n"] >= 1
        assert row["coverage_complete"] is False
        assert row["t3_rate"] is None
        assert row["median_t0_t3_hours"] is None


def _w4_row(payload: dict, week: str) -> dict | None:
    return next((c for c in payload["cohorts"] if c["cohort_week"] == week), None)


@requires_pg
def test_w4_window_alignment_and_fallback_split():
    base = _bj_noon(datetime.now(_BJT).date() - timedelta(days=40))
    week = _bj_monday(base).isoformat()
    before = _w4_row(db.admin_product_health_w4_split(), week) or {
        "n_all": 0, "n_activated": 0, "w4_all": 0, "w4_activated": 0,
    }

    created = base.astimezone(timezone.utc).isoformat()
    for uid in ("u_health_w4a", "u_health_w4b", "u_health_w4c", "u_health_w4d"):
        seed_user(uid, created_at=created)
    t0 = base.timestamp()
    with db.get_pool().connection() as conn:
        # w4a：真回复激活 + 第 28 天回来 → 两个分子都算。
        _insert_chat(conn, "u_health_w4a", "hw4-a-reply", t0 + 86400, "agent", "hosted_v2")
        _insert_session(conn, "u_health_w4a", (base + timedelta(days=28)).timestamp(), "hw4-a-s28")
        # w4b：第 35 天才回来 → 已滑出 [t0+28d, t0+35d) 窗，不算 W4。
        _insert_session(conn, "u_health_w4b", (base + timedelta(days=35)).timestamp(), "hw4-b-s35")
        # w4c：只有 fallback 回复 → 不算激活；第 28 天回来 → 只进全量分母。
        _insert_chat(conn, "u_health_w4c", "hw4-c-fb", t0 + 86400, "agent", "foreground_fallback")
        _insert_session(conn, "u_health_w4c", (base + timedelta(days=28)).timestamp(), "hw4-c-s28")
        # w4d：第 28 天回来，但首条真回复在第 29 天——激活晚于 W4 窗口开始，
        # 不算激活者（激活必须先于被预测的那周，否则成熟 cohort 的率会随
        # 后补回复漂移、因果倒置）；只进全量分母/分子。
        _insert_session(conn, "u_health_w4d", (base + timedelta(days=28)).timestamp() + 60, "hw4-d-s28")
        _insert_chat(conn, "u_health_w4d", "hw4-d-late", (base + timedelta(days=29)).timestamp(), "agent", "hosted_v2")

    after = _w4_row(db.admin_product_health_w4_split(), week)
    assert after is not None
    assert after["n_all"] == before["n_all"] + 4
    assert after["n_activated"] == before["n_activated"] + 1  # fallback/晚激活都不算
    assert after["w4_all"] == before["w4_all"] + 3  # w4a + w4c + w4d；w4b 的 +35d 不算
    assert after["w4_activated"] == before["w4_activated"] + 1  # 只有 w4a


@requires_pg
def test_stickiness_l_distribution_and_incomplete_day_exclusion():
    before = db.admin_product_health_stickiness()
    today = datetime.now(_BJT).date()
    # 窗口 = 最近 7 个**完整**北京日（今天进行中，不算）。
    assert before["window"]["end_day"] == (today - timedelta(days=1)).isoformat()
    assert before["window"]["start_day"] == (today - timedelta(days=7)).isoformat()
    if before["wau"] == 0:
        assert before["stickiness"] is None  # 0 做分母 → 未知，不是 0

    created = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    for uid in ("u_health_st7", "u_health_st1", "u_health_st0"):
        seed_user(uid, created_at=created)
    with db.get_pool().connection() as conn:
        for offset in range(1, 8):  # 7/7：每个完整日都活跃
            ts = _bj_noon(today - timedelta(days=offset)).timestamp()
            _insert_session(conn, "u_health_st7", ts, f"hst7-{offset}")
        _insert_session(  # 1/7：只活跃一天
            conn, "u_health_st1", _bj_noon(today - timedelta(days=3)).timestamp(), "hst1-1"
        )
        _insert_session(  # 只在今天（不完整日）活跃 → 整个窗口都看不见它
            conn, "u_health_st0", _bj_noon(today).timestamp(), "hst0-1"
        )

    after = db.admin_product_health_stickiness()
    assert after["wau"] == before["wau"] + 2  # st0 被不完整日排除
    assert after["l_distribution"][7] == before["l_distribution"][7] + 1
    assert after["l_distribution"][1] == before["l_distribution"][1] + 1
    assert after["dau_latest"] >= 1
    assert after["stickiness"] is not None
    assert after["stickiness"] == pytest.approx(after["avg_dau"] / after["wau"], rel=1e-6)


@requires_pg
def test_concentration_top_decile_small_n_and_null_tokens():
    before = db.admin_product_health_concentration()
    created = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    for uid in ("u_health_c1", "u_health_c2", "u_health_c3"):
        seed_user(uid, created_at=created)
    base_ts = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    with db.get_pool().connection() as conn:
        for i in range(5):
            _insert_session(conn, "u_health_c1", base_ts + i * 3600, f"hc1-{i}")
        for i in range(3):
            _insert_session(conn, "u_health_c2", base_ts + i * 3600, f"hc2-{i}")
        _insert_session(conn, "u_health_c3", base_ts, "hc3-0")
        conn.execute(
            "INSERT INTO v2_turn_metrics (job_id,user_id,lane,prompt_tokens,"
            "completion_tokens,model_calls,retries,failed,status,"
            "usage_reported_calls,created_at) VALUES "
            "(987655000001,'u_health_c1','chat',1000,500,1,0,false,'ok',1,now() - interval '2 days'),"
            "(987655000002,'u_health_c1','chat',1000,500,1,0,false,'ok',1,now() - interval '2 days'),"
            "(987655000003,'u_health_c2','chat',100,50,1,0,false,'ok',1,now() - interval '2 days'),"
            # provider 没报 usage 的 turn：token 口径里必须整行排除，不算 0。
            "(987655000004,'u_health_c2','chat',NULL,NULL,1,0,false,'ok',0,now() - interval '2 days'),"
            # 进行中的今天（未走完的北京日）：token 侧必须与 session 侧同一对
            # 完整日边界——这行若被计入，两个并排的 share 就不同窗了。
            "(987655000005,'u_health_c3','chat',777777,1,1,0,false,'ok',1,now())"
        )

    after = db.admin_product_health_concentration()
    assert after["sessions"]["total"] == before["sessions"]["total"] + 9
    assert after["sessions"]["users"] == before["sessions"]["users"] + 3
    assert after["tokens"]["total"] == before["tokens"]["total"] + 3150
    if before["sessions"]["users"] == 0:
        # n<10：top decile = ceil(3/10) = 1 个用户，即 5 场的 c1。
        assert after["sessions"]["top_decile"] == 5
        assert after["sessions"]["share"] == pytest.approx(5 / 9, rel=1e-3)
    if before["tokens"]["users"] == 0:
        assert after["tokens"]["top_decile"] == 3000
        assert after["tokens"]["share"] == pytest.approx(3000 / 3150, rel=1e-3)


def _growth_row(payload: dict, week: str) -> dict | None:
    return next((r for r in payload["rows"] if r["week"] == week), None)


def _gv(row: dict | None, key: str) -> int:
    return int((row or {}).get(key) or 0)


@requires_pg
def test_growth_accounting_weekly_states_and_oldest_baseline():
    this_monday = _bj_monday(datetime.now(timezone.utc))
    wk = {k: (this_monday - timedelta(weeks=k)) for k in (1, 2, 3)}
    label = {k: d.isoformat() for k, d in wk.items()}
    noon = {k: _bj_noon(d + timedelta(days=2)).timestamp() for k, d in wk.items()}

    before = db.admin_product_health_growth_accounting_weekly()
    created_old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    seed_user("u_health_ga", created_at=created_old)
    seed_user("u_health_gb", created_at=created_old)
    created_c = _bj_noon(wk[1] + timedelta(days=1)).astimezone(timezone.utc)
    seed_user("u_health_gc", created_at=created_c.isoformat())
    with db.get_pool().connection() as conn:
        # gA：wk3、wk2 活跃，wk1 消失 → wk1 记一笔 churn。
        _insert_session(conn, "u_health_ga", noon[3], "hga-3")
        _insert_session(conn, "u_health_ga", noon[2], "hga-2")
        # gB：wk3 活跃、wk2 消失（churn）、wk1 回来（resurrected）。
        _insert_session(conn, "u_health_gb", noon[3], "hgb-3")
        _insert_session(conn, "u_health_gb", noon[1], "hgb-1")
        # gC：wk1 注册 + 活跃 + 首条真回复 → new_registered / newly_activated。
        _insert_session(conn, "u_health_gc", noon[1], "hgc-1")
        _insert_chat(conn, "u_health_gc", "hgc-reply", noon[1] + 60, "agent", "hosted_v2")

    after = db.admin_product_health_growth_accounting_weekly()
    # newest first。
    row_weeks = [r["week"] for r in after["rows"]]
    assert row_weeks == sorted(row_weeks, reverse=True)

    b1, a1 = _growth_row(before, label[1]), _growth_row(after, label[1])
    b2, a2 = _growth_row(before, label[2]), _growth_row(after, label[2])
    assert a1 is not None and a2 is not None
    assert _gv(a1, "active") == _gv(b1, "active") + 2  # gB + gC
    assert _gv(a1, "new_registered") == _gv(b1, "new_registered") + 1
    assert _gv(a1, "newly_activated") == _gv(b1, "newly_activated") + 1
    assert _gv(a1, "churned") == _gv(b1, "churned") + 1  # gA
    assert _gv(a1, "resurrected") == _gv(b1, "resurrected") + 1  # gB
    assert _gv(a2, "active") == _gv(b2, "active") + 1  # gA
    assert _gv(a2, "churned") == _gv(b2, "churned") + 1  # gB

    # 地平线最老一行是基线：没有上一周可比，churn/resurrect/net 一律 None。
    oldest = after["rows"][-1]
    assert oldest["churned"] is None
    assert oldest["resurrected"] is None
    assert oldest["net_change"] is None

    # 最新一行是进行中的本周：churn/resurrect/net 右删失（gA/gB 只是这周
    # **还没**打开 app），发 None 而不是编出「流失 N / 净变化 -N」的定论；
    # active/new_registered/newly_activated 保留为运行下界。
    newest = after["rows"][0]
    assert newest["week"] == this_monday.isoformat()
    assert newest["churned"] is None
    assert newest["resurrected"] is None
    assert newest["net_change"] is None
    assert newest["active"] is not None
    assert newest["new_registered"] is not None
    assert newest["newly_activated"] is not None


@requires_pg
def test_power_user_census_streak_union_and_throttled_skip():
    now = datetime.now(timezone.utc)
    this_monday = _bj_monday(now)
    # current 是**进行中的本周**：streak 要求本周 + 前 3 周都合格，所以连种
    # 5 个周（0..4），上周那行（需要 1..4 周）也顺带可判定。本周的时间戳
    # 取「一小时前，但不早于本周一 00:30」——跑在北京周一凌晨也不会掉进上周。
    cur_ts = max(
        now.timestamp() - 3600,
        datetime.combine(this_monday, datetime.min.time(), tzinfo=_BJT).timestamp()
        + 1800,
    )
    week_ts = [cur_ts] + [
        _bj_noon(this_monday - timedelta(weeks=k) + timedelta(days=2)).timestamp()
        for k in (1, 2, 3, 4)
    ]
    before = db.admin_product_health_power_users()
    assert before["definition"] == {"min_jobs": 5, "streak_weeks": 4}

    created = (now - timedelta(days=60)).isoformat()
    for uid in ("u_health_p5", "u_health_p4", "u_health_pu", "u_health_pt",
                "u_health_ph"):
        seed_user(uid, created_at=created)

    seq = 0
    with db.get_pool().connection() as conn:
        def v2_jobs(uid: str, ts: float, count: int, *,
                    lane: str = "scheduled") -> None:
            # Runtime V2 侧的用户主动 job（scheduled/manual_wake lanes；
            # heartbeat lane 是自主 tick，不进 census）。终态 status 避开
            # pending/claimed/running 的 single-flight 唯一索引。
            for _ in range(count):
                conn.execute(
                    "INSERT INTO agent_jobs (user_id,lane,status,created_at) "
                    "VALUES (%s,%s,'completed',to_timestamp(%s))",
                    (uid, lane, ts),
                )

        def v1_jobs(uid: str, ts: float, count: int, *, status: str,
                    reason: str = "", kind: str = "scheduled") -> None:
            nonlocal seq
            for _ in range(count):
                seq += 1
                conn.execute(
                    "INSERT INTO user_logs (user_id,stream,ts,item_key,doc) VALUES "
                    "(%s,'proactive_jobs',%s,%s,"
                    " jsonb_build_object('status',%s::text,'status_reason',%s::text,"
                    "                    'job_kind',%s::text))",
                    (uid, ts, f"hpw-{seq}", status, reason, kind),
                )

        for ts in week_ts:
            v2_jobs("u_health_p5", ts, 5)      # 5×每周 → power user
            v2_jobs("u_health_p4", ts, 4)      # 4/周 → 差一票，不算
            # pu：V1+V2 并集 3+2=5 → 算。V1 侧故意用 heartbeat_broadcast_on
            # ——匹配 heartbeat% 但在用户动作例外表里（与 overspeed 哨兵的
            # kind 链逐字对齐），是人按的开关，必须计入。
            v2_jobs("u_health_pu", ts, 2)
            v1_jobs("u_health_pu", ts, 3, status="delivered",
                    kind="heartbeat_broadcast_on")
            v1_jobs("u_health_pt", ts, 3, status="delivered")
            # 被心跳闸拦下的 skipped job 不是用户用量，不进 5 票门槛。
            v1_jobs("u_health_pt", ts, 2, status="skipped",
                    reason="heartbeat_throttled")
            # ph：每周 10 个自主心跳（V2 heartbeat lane + V1 heartbeat/presence
            # kind）——量的是 agent 在线不是人使用，一个都不许进门槛；不剔除
            # 的话默认 cadence ~84 tick/周会让任何在线用户躺成铁杆。
            v2_jobs("u_health_ph", ts, 5, lane="heartbeat")
            v1_jobs("u_health_ph", ts, 3, status="delivered", kind="heartbeat")
            v1_jobs("u_health_ph", ts, 2, status="delivered", kind="presence")

    after = db.admin_product_health_power_users()
    # p5 + pu；ph 的纯心跳流量若被误计会让这里 +3。
    assert (after["current"] or 0) == (before["current"] or 0) + 2

    last_week = (this_monday - timedelta(weeks=1)).isoformat()
    b_row = next((r for r in before["weekly"] if r["week"] == last_week), None)
    a_row = next((r for r in after["weekly"] if r["week"] == last_week), None)
    assert a_row is not None
    # 上周合格（>=5 job）的用户：p5、pu；pt 的 throttled skip 只剩 3 票，
    # ph 的 10 个心跳 tick 全部不计。
    assert _gv(a_row, "qualifying_users") == _gv(b_row, "qualifying_users") + 2
    assert (a_row["power_users"] or 0) == ((b_row or {}).get("power_users") or 0) + 2

    # 地平线开头连 4 周窗都凑不齐：streak 不可评估 → None，不是 0。
    assert after["weekly"][-1]["power_users"] is None

    assert isinstance(after["monthly"], list)


@requires_pg
def test_proactive_reply_rate_right_censoring():
    def _sum(payload: dict, key: str) -> int:
        return sum(int(r[key] or 0) for r in payload["rows"])

    before = db.admin_product_health_proactive_reply_rate()
    created = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    for uid in ("u_health_rr1", "u_health_rr2", "u_health_rr3", "u_health_rr4"):
        seed_user(uid, created_at=created)

    last_wed_noon = _bj_noon(
        _bj_monday(datetime.now(timezone.utc)) - timedelta(weeks=1) + timedelta(days=2)
    ).timestamp()
    now_ts = datetime.now(timezone.utc).timestamp()
    with db.get_pool().connection() as conn:
        # rr1：主动消息 + 1 小时内用户回复 → 24h 内已回。
        _insert_chat(conn, "u_health_rr1", "hrr1-p", last_wed_noon, "agent", "agent_initiated_proactive")
        _insert_chat(conn, "u_health_rr1", "hrr1-u", last_wed_noon + 3600, "user", "")
        # rr2：回复来得太晚（+30h）→ 不算 24h 内已回。
        _insert_chat(conn, "u_health_rr2", "hrr2-p", last_wed_noon, "agent", "agent_initiated_proactive")
        _insert_chat(conn, "u_health_rr2", "hrr2-u", last_wed_noon + 30 * 3600, "user", "")
        # rr3：消息才 2 小时新 → 24h 观察窗没走完，右删失，整条不进分母。
        _insert_chat(conn, "u_health_rr3", "hrr3-p", now_ts - 2 * 3600, "agent", "agent_initiated_proactive")
        # rr4：主动消息后只有 verify_ping 的 user 行 → 机器探活不是回复。
        _insert_chat(conn, "u_health_rr4", "hrr4-p", last_wed_noon, "agent", "agent_initiated_proactive")
        _insert_chat(conn, "u_health_rr4", "hrr4-vp", last_wed_noon + 3600, "user", "verify_ping")

    after = db.admin_product_health_proactive_reply_rate()
    row_weeks = [r["week"] for r in after["rows"]]
    assert row_weeks == sorted(row_weeks, reverse=True)  # newest first
    assert _sum(after, "proactive_msgs") == _sum(before, "proactive_msgs") + 3
    assert _sum(after, "replied_24h") == _sum(before, "replied_24h") + 1
    assert _sum(after, "users") == _sum(before, "users") + 3
