"""Runtime 健康值班台：阈值判定与失败码清洗（纯函数，无需 PostgreSQL）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track as _dt  # noqa: E402
from core import reqctx  # noqa: E402
import pytest  # noqa: E402

from admin import admin_core as _admin_core  # noqa: E402

from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", "admin-test-token")
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


def _admin_headers() -> dict[str, str]:
    # NOTE: brief text says "X-Admin-Key"; the real admin auth (routes_asgi.py
    # _extract_admin_token) only reads "X-Admin-Token" (matches every other
    # test file in this repo, e.g. test_data_track.py::_admin_headers).
    return {"X-Admin-Token": "admin-test-token"}


def _fake_summary(**_kw) -> dict:
    return {
        "window_hours": _kw.get("within_hours", 24),
        "generated_at": 1_800_000_000.0,
        "lanes": [{
            "lane": "chat", "sampled_jobs": 10, "completed": 9, "failed": 1,
            "expired": 0, "superseded": 0, "queue_expired": 0, "lease_expired": 0,
            "failure_rate": 0.1, "p50_ok_ms": 18_000, "p95_ok_ms": 38_000,
            "capture": {"complete": 10, "partial": 0, "missing": 0, "open": 0},
            "top_failures": [{"code": "turn_failed:providererror", "count": 1}],
        }],
        "pool": {
            "inflight": 1, "pending": 0, "live_workers": 2,
            "capacity": 8, "oldest_pending_age_sec": None,
        },
    }


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


def test_runtime_health_level_warns_on_wedged_lane_with_no_terminal_jobs():
    # I-3: worker 活着、job 全卡在 claimed/running（无 pending，无 missing/p95
    # 触发），当前实现全部指标点跳过 → "ok"。这条 lane 的 open>0 却 sampled_jobs
    # ==0 本身就是矛盾态，必须至少 warn。
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(
            sampled_jobs=0, completed=0, failed=0, expired=0,
            failure_rate=None, p50_ok_ms=None, p95_ok_ms=None,
            capture={"complete": 0, "partial": 0, "missing": 0, "open": 57},
        )])
    )
    assert level != "ok"
    assert any("在飞" in r or "终态" in r for r in reasons)


def test_runtime_health_level_bad_on_inflight_exceeding_capacity():
    # I-3: inflight > capacity 是明确的矛盾态（池账目对不上），必须判 bad。
    level, reasons = _dt._runtime_health_level(
        _payload(inflight=57, capacity=8)
    )
    assert level == "bad"
    assert any("容量" in r or "capacity" in r.lower() for r in reasons)


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


def test_runtime_failure_code_truncates_long_valid_code():
    # 新语义：形状合法（scope:kind，只含 [a-z0-9_]）的码无论多长都放行、截断到 64。
    long_code = "wake_failed:" + ("x" * 200)
    result = _dt._runtime_failure_code(long_code)
    assert result == long_code[:64]
    assert len(result) == 64


def test_runtime_failure_code_rejects_free_text_after_known_prefix():
    # 这是被移除的行为：旧实现只要 startswith("turn_failed:") 就无条件放行 +
    # 截断，哪怕冒号后是含空格/中文的自由文本（"turn_failed: 我的身份证号是
    # 1234" 这种）。新实现按形状校验，冒号后必须仍是 [a-z0-9_]+，自由文本一律
    # 落 other。
    leaked = "turn_failed:" + ("我的身份证号是 1234 " * 20)
    assert _dt._runtime_failure_code(leaked) == "other"


def test_runtime_failure_code_covers_non_chat_lane_shapes():
    # I-2: 白名单曾经只放行 turn_failed:/queue_timeout/lease_timeout 三种形状，
    # 其余全部写入点（wake_failed:*/extraction_failed:*/compaction_failed:*/
    # mcp_mutation_outcome_unknown/runtime_expired）塌成 other。heartbeat lane
    # 的失败码正是 wake_failed:*，本页专门给 heartbeat 加了日报口径链接，不能
    # 让它的失败原因永远只显示 other。
    assert _dt._runtime_failure_code("wake_failed:timeouterror") == "wake_failed:timeouterror"
    assert _dt._runtime_failure_code("extraction_failed:valueerror") == "extraction_failed:valueerror"
    assert _dt._runtime_failure_code("compaction_failed:keyerror") == "compaction_failed:keyerror"
    assert _dt._runtime_failure_code("mcp_mutation_outcome_unknown") == "mcp_mutation_outcome_unknown"
    assert _dt._runtime_failure_code("runtime_expired") == "runtime_expired"


def test_runtime_failure_code_rejects_malformed_shapes():
    # 含空格/中文/大写的串一律落 other，即便"看起来像"一个已知码。
    assert _dt._runtime_failure_code("Wake_Failed:TimeoutError") == "other"
    assert _dt._runtime_failure_code("wake failed: timeout") == "other"
    assert _dt._runtime_failure_code("wake_failed: 超时") == "other"


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


def test_render_runtime_health_page_merges_duplicate_other_rows(bound_request):
    # I-2: 清洗发生在渲染层且不重新聚合——同一 lane 的两个不同原始码若都被清洗
    # 成 other，会渲染成两行都叫 other（reviewer 实证：('other','3') 和
    # ('other','2') 两行）。渲染前必须按 (lane, code) 重新合并计数。
    html_out = _dt._render_runtime_health_page(_payload([
        _lane(failure_rate=0.5, failed=5, completed=5, top_failures=[
            {"code": "some free text A", "count": 3},
            {"code": "some free text B", "count": 2},
        ]),
    ]))
    # 只应该出现一行 other，计数合并为 5
    assert html_out.count("<code>other</code>") == 1
    assert "<td>5</td>" in html_out


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


def test_render_runtime_health_page_uses_bad_class_for_bad_conclusion(bound_request):
    # I-1: level="bad" 曾被映射到 CSS class "warn"，页顶总体结论永远不会真正
    # 变红——100% 失败率与 6% 失败率在页顶显示成同一个橙色。
    html_out = _dt._render_runtime_health_page(_payload([
        _lane(failure_rate=1.0, failed=20, completed=0),
    ]))
    assert "<span class=\"bad\">" in html_out or "<span class='bad'>" in html_out
    assert "总体结论" in html_out
    # 不应该把 bad 结论渲染成 warn class
    import re as _re
    m = _re.search(r"总体结论：<span class=\"([a-z]+)\">", html_out)
    assert m is not None, html_out
    assert m.group(1) == "bad"


def test_render_runtime_health_page_does_not_claim_ok_when_wedged(bound_request):
    # I-3: reviewer 实证——lane 全部 job 在飞（无 pending，worker 心跳还活着），
    # 页面之前会显示「这不是故障」+ 总体结论「正常」。这是本分支专门要修的洞:
    # 数据在页面上，但人被页面告知没事。
    html_out = _dt._render_runtime_health_page(_payload([_lane(
        sampled_jobs=0, completed=0, failed=0, expired=0,
        failure_rate=None, p50_ok_ms=None, p95_ok_ms=None,
        capture={"complete": 0, "partial": 0, "missing": 0, "open": 57},
    )], inflight=57, capacity=8))
    assert "这不是故障" not in html_out
    assert "正常" not in html_out


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


def test_runtime_view_renders_and_highlights_nav(client, monkeypatch):
    monkeypatch.setattr(_dt, "_runtime_health_summary", _fake_summary)
    page = client.get(
        "/admin/data-track?view=runtime", headers=_admin_headers()
    ).get_data(as_text=True)
    assert "Runtime 健康" in page
    assert "各 lane 健康" in page
    assert "turn_failed:providererror" in page


def test_runtime_view_appears_in_nav_of_other_views(client, monkeypatch):
    monkeypatch.setattr(_dt, "_runtime_health_summary", _fake_summary)
    page = client.get("/admin/data-track", headers=_admin_headers()).get_data(as_text=True)
    assert "view=runtime" in page


def test_runtime_view_falls_back_on_invalid_hours(client, monkeypatch):
    seen = {}

    def _capture(**kw):
        seen.update(kw)
        return _fake_summary(**kw)

    monkeypatch.setattr(_dt, "_runtime_health_summary", _capture)
    client.get("/admin/data-track?view=runtime&hours=99999", headers=_admin_headers())
    assert seen["within_hours"] == 24
    client.get("/admin/data-track?view=runtime&hours=abc", headers=_admin_headers())
    assert seen["within_hours"] == 24
    client.get("/admin/data-track?view=runtime&hours=168", headers=_admin_headers())
    assert seen["within_hours"] == 168


def test_runtime_view_requires_admin(client):
    res = client.get("/admin/data-track?view=runtime")
    assert res.status_code in (401, 302, 303)


def test_runtime_health_summary_is_wired_to_jobs_store():
    # 装配段必须把桩换成真实实现，否则页面永远空白（asgi-lifespan 漏接线的老坑）
    import asgi_app  # noqa: F401
    from model_api_runtime.v2 import jobs_store

    assert _dt._runtime_health_summary is jobs_store.recent_runtime_health


def test_runtime_view_degrades_to_error_card_not_500(client, monkeypatch):
    # 数据函数炸了不能把整页打成 500——值班台恰恰是出事时才被打开的那一页。
    def _boom(**_kw):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(_dt, "_runtime_health_summary", _boom)
    res = client.get("/admin/data-track?view=runtime", headers=_admin_headers())
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    assert "Runtime 健康数据暂时取不到" in body
    # NOTE: brief text asserted "view=users", but the existing nav_item()
    # implementation (data_track.py::_render_data_track_view_nav, unchanged
    # by this task) omits the `view=` query param for the default "users"
    # view — every other nav test in this repo relies on that same
    # behavior. "view=dau" is a stable, non-default nav link that proves the
    # nav bar (and thus the escape hatch to other views) is still present.
    assert "view=dau" in body            # nav 仍在，其他视图还能点
    assert "pool exhausted" not in body  # 异常细节不外泄到页面


def test_runtime_pages_share_one_stylesheet(bound_request):
    # 两个新页共用 _RUNTIME_PAGE_CSS（Task 4 Step 3a 抽出的常量）；
    # 这条测试防止将来有人又复制粘贴出第三份。
    main_page = _dt._render_runtime_health_page(_payload())
    error_page = _dt._render_runtime_health_error_page()
    assert _dt._RUNTIME_PAGE_CSS in main_page
    assert _dt._RUNTIME_PAGE_CSS in error_page
