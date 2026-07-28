"""Runtime 健康值班台：阈值判定与失败码清洗（纯函数，无需 PostgreSQL）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track as _dt  # noqa: E402
from core import reqctx  # noqa: E402
import pytest  # noqa: E402

from admin import admin_core as _admin_core  # noqa: E402


@pytest.fixture()
def bound_request():
    """渲染纯函数需要 request 上下文才能拼 href。刻意不设 autouse——
    Task 5 的 client 测试自己会 bind，嵌套 bind 会让请求上下文互相覆盖。"""
    with _admin_core.bind(""):
        yield


def _lane(**overrides) -> dict:
    base = {
        "lane": "chat",
        "sampled_jobs": 100,
        "completed": 100,
        "failed": 0,
        "expired": 0,
        "superseded": 0,
        "queue_expired": 0,
        "lease_expired": 0,
        "failure_rate": 0.0,
        "p50_ok_ms": 18_500,
        "p95_ok_ms": 38_100,
        "capture": {"complete": 100, "partial": 0, "missing": 0, "open": 0},
        "top_failures": [],
    }
    base.update(overrides)
    return base


def _payload(lanes=None, **pool_overrides) -> dict:
    pool = {
        "inflight": 0, "pending": 0, "live_workers": 2,
        "capacity": 8, "oldest_pending_age_sec": None,
    }
    pool.update(pool_overrides)
    return {
        "window_hours": 24,
        "generated_at": 1_800_000_000.0,
        "lanes": lanes if lanes is not None else [_lane()],
        "pool": pool,
    }


def test_runtime_health_level_green_on_healthy_fleet():
    level, reasons = _dt._runtime_health_level(_payload())
    assert level == "ok"
    assert reasons == []


def test_runtime_health_level_warns_between_thresholds():
    # 失败率 8% 落在 5%~15% 的黄区
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(failure_rate=0.08, failed=8, completed=92)])
    )
    assert level == "warn"
    assert any("失败率" in r for r in reasons)


def test_runtime_health_level_red_on_high_failure_rate():
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(failure_rate=1.0, failed=20, completed=0)])
    )
    assert level == "bad"
    assert any("失败率" in r for r in reasons)


def test_runtime_health_level_red_on_missing_trajectory():
    # 漏写没有「轻微」档：一条就是数据缺口
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(capture={"complete": 9, "partial": 0, "missing": 1, "open": 0})])
    )
    assert level == "bad"
    assert any("捕获" in r or "missing" in r for r in reasons)


def test_runtime_health_level_red_on_empty_worker_pool():
    level, reasons = _dt._runtime_health_level(_payload(live_workers=0))
    assert level == "bad"
    assert any("worker" in r.lower() for r in reasons)


def test_runtime_health_level_uses_p95_thresholds():
    warn, _ = _dt._runtime_health_level(_payload([_lane(p95_ok_ms=90_000)]))
    bad, _ = _dt._runtime_health_level(_payload([_lane(p95_ok_ms=300_000)]))
    assert warn == "warn"
    assert bad == "bad"


def test_runtime_health_level_uses_pending_age_thresholds():
    warn, _ = _dt._runtime_health_level(_payload(pending=1, oldest_pending_age_sec=90))
    bad, _ = _dt._runtime_health_level(_payload(pending=1, oldest_pending_age_sec=600))
    assert warn == "warn"
    assert bad == "bad"


def test_runtime_health_level_ignores_empty_samples():
    # 零样本不得判红——2795537a 的教训：分母为 0 曾被渲染成红 0%，
    # 3 条健康心跳看起来像全挂。
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(
            sampled_jobs=0, completed=0, failure_rate=None,
            p50_ok_ms=None, p95_ok_ms=None,
            capture={"complete": 0, "partial": 0, "missing": 0, "open": 0},
        )])
    )
    assert level == "ok"
    assert reasons == []


def test_runtime_health_level_takes_worst_across_lanes():
    level, _ = _dt._runtime_health_level(_payload([
        _lane(lane="chat"),
        _lane(lane="heartbeat", failure_rate=0.9, failed=9, completed=1),
    ]))
    assert level == "bad"


def test_runtime_failure_code_keeps_known_enumerations():
    assert _dt._runtime_failure_code("turn_failed:providererror") == "turn_failed:providererror"
    assert _dt._runtime_failure_code("queue_timeout") == "queue_timeout"
    assert _dt._runtime_failure_code("lease_timeout") == "lease_timeout"


def test_runtime_failure_code_buckets_unknown_free_text():
    # 将来若有人往 last_error 写自由文本（含用户内容），页面不得渗出
    leaked = "Traceback: user said 我的身份证号是 1234"
    assert _dt._runtime_failure_code(leaked) == "other"
    assert _dt._runtime_failure_code("") == "other"
    assert _dt._runtime_failure_code(None) == "other"


def test_runtime_failure_code_truncates_long_known_prefix():
    long_code = "turn_failed:" + ("x" * 200)
    assert len(_dt._runtime_failure_code(long_code)) == 64


# ---- 边界值测试：精确阈值 ----

def test_runtime_health_level_boundary_failure_rate_warn():
    # 失败率恰好 5% 进黄区
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(failure_rate=0.05, failed=5, completed=95)])
    )
    assert level == "warn"
    assert any("失败率" in r for r in reasons)


def test_runtime_health_level_boundary_failure_rate_bad():
    # 失败率恰好 15% 进红区
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(failure_rate=0.15, failed=15, completed=85)])
    )
    assert level == "bad"
    assert any("失败率" in r for r in reasons)


def test_runtime_health_level_boundary_p95_warn():
    # p95 恰好 60000ms 进黄区
    level, _ = _dt._runtime_health_level(_payload([_lane(p95_ok_ms=60_000)]))
    assert level == "warn"


def test_runtime_health_level_boundary_p95_bad():
    # p95 恰好 120000ms 进红区
    level, _ = _dt._runtime_health_level(_payload([_lane(p95_ok_ms=120_000)]))
    assert level == "bad"


def test_runtime_health_level_boundary_pending_warn():
    # pending 年龄恰好 60s 进黄区
    level, _ = _dt._runtime_health_level(_payload(pending=1, oldest_pending_age_sec=60))
    assert level == "warn"


def test_runtime_health_level_boundary_pending_bad():
    # pending 年龄恰好 180s 进红区
    level, _ = _dt._runtime_health_level(_payload(pending=1, oldest_pending_age_sec=180))
    assert level == "bad"


# ---- window_hours 参数解析测试 ----

def test_runtime_health_window_hours_accepts_whitelisted_values():
    for hours in (24, 168, 720):
        with reqctx.bind(f"hours={hours}"):
            assert _dt._runtime_health_window_hours() == hours


def test_runtime_health_window_hours_falls_back_on_bad_input():
    # 非白名单值、非数字、负数、缺失 —— 一律回落 24，不抛异常
    for qs in ("hours=99999", "hours=0", "hours=-5", "hours=abc", "hours=", ""):
        with reqctx.bind(qs):
            assert _dt._runtime_health_window_hours() == 24


# ---- Task 4: 渲染 Runtime 健康页 ----


def test_render_runtime_health_page_shows_conclusion_and_lanes(bound_request):
    html_out = _dt._render_runtime_health_page(_payload([
        _lane(lane="chat"),
        _lane(lane="heartbeat", sampled_jobs=12, completed=12),
    ]))
    assert "Runtime 健康" in html_out
    assert "正常" in html_out
    assert "chat" in html_out
    assert "heartbeat" in html_out
    assert "Worker 池" in html_out


def test_render_runtime_health_page_renders_na_not_fake_zero(bound_request):
    html_out = _dt._render_runtime_health_page(_payload([_lane(
        sampled_jobs=0, completed=0, failure_rate=None,
        p50_ok_ms=None, p95_ok_ms=None,
        capture={"complete": 0, "partial": 0, "missing": 0, "open": 0},
    )]))
    # 零样本时指标值应显示"—"而非数字 0 或"0%"；CSS 的"100%"不是数据渲染，允许存在
    # 检查失败率列是否无 pill（当样本为 0 且 rate 为 None 时）
    assert "<span class='pill" not in html_out   # 零样本时不应有失败率 pill
    assert "class='muted'>—</td>" in html_out    # 失败率应显示"—"
    assert "当前窗口无样本" in html_out


def test_render_runtime_health_page_escapes_and_buckets_failure_codes(bound_request):
    leaked = "<script>alert(1)</script> 我的身份证号是 1234"
    html_out = _dt._render_runtime_health_page(_payload([
        _lane(failure_rate=0.5, failed=1, completed=1,
              top_failures=[{"code": leaked, "count": 1}]),
    ]))
    assert "<script>" not in html_out
    assert "身份证号" not in html_out
    assert "other" in html_out


def test_render_runtime_health_page_points_at_break_glass_inspector(bound_request):
    html_out = _dt._render_runtime_health_page(_payload([
        _lane(failure_rate=0.5, failed=1, completed=1,
              top_failures=[{"code": "turn_failed:providererror", "count": 1}]),
    ]))
    assert "上游原始错" in html_out
    assert "trajectory inspector" in html_out


def test_render_runtime_health_page_offers_window_switches(bound_request):
    html_out = _dt._render_runtime_health_page(_payload())
    assert "hours=24" in html_out
    assert "hours=168" in html_out
    assert "hours=720" in html_out


def test_render_runtime_health_page_declares_scope_split(bound_request):
    # 与 Proactive 日报页的口径分工必须写在页面上，否则两页数字打架时无从判断
    html_out = _dt._render_runtime_health_page(_payload())
    assert "运行时视角" in html_out
    assert "Proactive 日报" in html_out


def test_render_runtime_health_page_failure_rate_three_tiers(bound_request):
    # 失败率应该分三档：<5% 绿、5%-15% 黄、≥15% 红
    good = _dt._render_runtime_health_page(_payload([_lane(failure_rate=0.03, failed=1, completed=33)]))
    warn = _dt._render_runtime_health_page(_payload([_lane(failure_rate=0.08, failed=4, completed=50)]))
    bad = _dt._render_runtime_health_page(_payload([_lane(failure_rate=0.20, failed=20, completed=80)]))

    assert "pill ok" in good      # 3% 应显示绿色 pill
    assert "pill warn" in warn    # 8% 应显示黄色 pill
    assert "pill bad" in bad      # 20% 应显示红色 pill


def test_render_runtime_health_page_shows_capture_open_bucket(bound_request):
    # 捕获列要显示四个桶：完整/部分/漏写/在飞；在飞不染色，仅数字显示
    html_out = _dt._render_runtime_health_page(_payload([_lane(
        failure_rate=0.0, failed=0, completed=100,
        capture={"complete": 80, "partial": 15, "missing": 2, "open": 3},
    )]))
    # 表格行应包含四个数字，用"/"分隔
    assert "80 / 15 /" in html_out           # 完整 / 部分 /
    assert "<b class='bad'>2</b>" in html_out   # 漏写 用 bad class 标记
    assert "/ 3</td>" in html_out            # 在飞 无特殊标记
