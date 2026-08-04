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
            "capture": {"terminal_seen_no_gap": 10, "partial": 0, "missing": 0, "open": 0},
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
        "capture": {"terminal_seen_no_gap": 100, "partial": 0, "missing": 0, "open": 0},
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


def test_runtime_health_level_guard_is_warning_not_hard_failure():
    lane = _lane(
        lane="heartbeat",
        completed=90,
        failed=10,
        failure_rate=0.10,
        operational_failures=0,
        operational_failure_rate=0.0,
        safety_suppressions=10,
    )

    level, reasons = _dt._runtime_health_level(_payload([lane]))

    assert level == "warn"
    assert any("安全抑制" in reason for reason in reasons)
    assert not any("系统故障率" in reason for reason in reasons)


def test_runtime_health_level_chat_keeps_user_impact_rate():
    lane = _lane(
        lane="chat",
        completed=92,
        failed=8,
        failure_rate=0.08,
        operational_failures=0,
        operational_failure_rate=0.0,
        control_outcomes=8,
    )

    level, reasons = _dt._runtime_health_level(_payload([lane]))

    assert level == "warn"
    assert any("回复失败率" in reason for reason in reasons)


def test_runtime_health_level_red_on_missing_trajectory():
    # 漏写没有「轻微」档：一条就是数据缺口
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(capture={"terminal_seen_no_gap": 9, "partial": 0, "missing": 1, "open": 0})])
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
            capture={"terminal_seen_no_gap": 0, "partial": 0, "missing": 0, "open": 0},
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
            capture={"terminal_seen_no_gap": 0, "partial": 0, "missing": 0, "open": 57},
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


def test_runtime_split_keeps_service_green_when_execution_is_bad():
    payload = _payload([
        _lane(failure_rate=0.40, failed=40, completed=60),
    ])

    service_level, service_reasons = _dt._runtime_service_level(payload, {})
    execution_level, execution_reasons = _dt._runtime_execution_level(payload)

    assert (service_level, service_reasons) == ("ok", [])
    assert execution_level == "bad"
    assert any("失败率" in reason for reason in execution_reasons)


def test_runtime_mcp_window_failures_affect_execution_not_current_service():
    payload = _payload()
    delivery = {
        "effect_outbox": {"pending": 0, "oldest_pending_age_sec": None},
        "terminal_failure_outbox": {
            "status_undelivered": 0,
            "runtime_error_undelivered": 0,
            "oldest_undelivered_age_sec": None,
        },
        "mcp_mutation": {"unknown": 1, "unresolved": 2},
    }

    service_level, service_reasons = _dt._runtime_service_level(payload, delivery)
    execution_level, execution_reasons = _dt._runtime_execution_level(
        payload, delivery
    )

    assert (service_level, service_reasons) == ("ok", [])
    assert execution_level == "warn"
    assert "MCP 结果未知 1 次" in execution_reasons
    assert "MCP 悬空 2 次" in execution_reasons


def test_runtime_service_is_bad_when_live_workers_have_zero_capacity():
    level, reasons = _dt._runtime_service_level(
        _payload(live_workers=1, capacity=0)
    )

    assert level == "bad"
    assert "无可执行槽位" in reasons


def test_runtime_split_keeps_trajectory_out_of_execution_quality():
    payload = _payload([
        _lane(capture={
            "terminal_seen_no_gap": 99,
            "partial": 0,
            "missing": 1,
            "open": 0,
        }),
    ])

    execution_level, execution_reasons = _dt._runtime_execution_level(payload)
    trajectory_level, trajectory_reasons = _dt._runtime_trajectory_level(payload)

    assert (execution_level, execution_reasons) == ("ok", [])
    assert trajectory_level == "bad"
    assert any("漏写 1 条" in reason for reason in trajectory_reasons)


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


def test_runtime_health_windows_stay_within_jobs_store_health_clamp():
    # I-3：design §4 说"两个函数不各自读 request.args，因此不可能出现窗口
    # 不一致"——这句话只覆盖了调用方，没覆盖被调方各自的钳制上界。
    # jobs_store.recent_runtime_health 把 within_hours 钳到 24*30（720），
    # recent_token_usage_by_lane 钳到 24*366。今天 max(_RUNTIME_HEALTH_WINDOWS)
    # == 720 == 24*30，两边钳制结果恰好相同——这是巧合，不是不变量。
    # 谁往白名单加一档 > 720 小时（比如 90 天 = 2160），健康列会被静默钳到
    # 720、token 列却查满新值，同一行两个窗口，页顶还标着新窗口数——且没有
    # 任何测试会红。这条断言就是那个会红的守卫：把巧合钉成显式约束。
    assert max(_dt._RUNTIME_HEALTH_WINDOWS) <= 24 * 30


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


def test_render_runtime_health_page_warns_service_when_delivery_is_missing(
    bound_request,
):
    html_out = _dt._render_runtime_health_page(
        _payload(),
        delivery=None,
    )
    start = html_out.index("当前可服务")
    section_start = html_out.rindex("<section", 0, start)
    section_end = html_out.index("</section>", start)
    service_card = html_out[section_start:section_end]

    assert "health-dimension warn" in service_card
    assert "注意" in service_card
    assert "交付数据暂不可用" in service_card
    assert '综合告警档位（取三项最差）：<span class="warn">注意</span>' in html_out


def test_render_runtime_health_page_places_mcp_window_warning_in_execution_card(
    bound_request,
):
    html_out = _dt._render_runtime_health_page(
        _payload(),
        delivery={
            "effect_outbox": {"pending": 0, "oldest_pending_age_sec": None},
            "terminal_failure_outbox": {
                "status_undelivered": 0,
                "runtime_error_undelivered": 0,
                "oldest_undelivered_age_sec": None,
            },
            "mcp_mutation": {"unknown": 1, "unresolved": 0},
        },
    )
    service_start = html_out.index("当前可服务")
    service_end = html_out.index("</section>", service_start)
    execution_start = html_out.index("近 24h 运行质量")
    execution_end = html_out.index("</section>", execution_start)

    assert "MCP 结果未知" not in html_out[service_start:service_end]
    assert "MCP 结果未知 1 次" in html_out[execution_start:execution_end]


def test_render_runtime_health_page_renders_na_not_fake_zero(bound_request):
    html_out = _dt._render_runtime_health_page(_payload([_lane(
        sampled_jobs=0, completed=0, failure_rate=None,
        p50_ok_ms=None, p95_ok_ms=None,
        capture={"terminal_seen_no_gap": 0, "partial": 0, "missing": 0, "open": 0},
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
    # I-1: level="bad" 曾被映射到 CSS class "warn"，页顶综合告警档位永远不会真正
    # 变红——100% 失败率与 6% 失败率在页顶显示成同一个橙色。
    html_out = _dt._render_runtime_health_page(_payload([
        _lane(failure_rate=1.0, failed=20, completed=0),
    ]))
    assert "<span class=\"bad\">" in html_out or "<span class='bad'>" in html_out
    assert "综合告警档位（取三项最差）" in html_out
    # 不应该把 bad 结论渲染成 warn class
    import re as _re
    m = _re.search(r"综合告警档位（取三项最差）：<span class=\"([a-z]+)\">", html_out)
    assert m is not None, html_out
    assert m.group(1) == "bad"


def test_render_runtime_health_page_does_not_claim_ok_when_wedged(bound_request):
    # I-3: reviewer 实证——lane 全部 job 在飞（无 pending，worker 心跳还活着），
    # 页面之前会显示「这不是故障」+ 综合告警档位「正常」。这是本分支专门要修的洞:
    # 数据在页面上，但人被页面告知没事。
    html_out = _dt._render_runtime_health_page(_payload([_lane(
        sampled_jobs=0, completed=0, failed=0, expired=0,
        failure_rate=None, p50_ok_ms=None, p95_ok_ms=None,
        capture={"terminal_seen_no_gap": 0, "partial": 0, "missing": 0, "open": 57},
    )], inflight=57, capacity=8))
    assert "这不是故障" not in html_out
    # 断言综合告警档位那个元素，而不是全页搜「正常」：这两个字会合法地出现在说明
    # 文字里（例如"高吞吐下瞬时积压是正常的"），全页搜会把无关的散文当成回归。
    # 收紧后仍然抓得住原 bug——档位若是 ok，结论就会渲染成 >正常</span>。
    assert ">正常</span>" not in html_out


def test_render_runtime_health_page_shows_capture_open_bucket(bound_request):
    # 捕获列要显示四个桶：见终态·无缺口/有缺口/漏写/在飞；在飞不染色，仅数字显示
    html_out = _dt._render_runtime_health_page(_payload([_lane(
        failure_rate=0.0, failed=0, completed=100,
        capture={"terminal_seen_no_gap": 80, "partial": 15, "missing": 2, "open": 3},
    )]))
    assert "80 / " in html_out                    # 见终态·无缺口
    assert "<b class='warn'>15</b>" in html_out   # 有缺口 用 warn class 标记
    assert "<b class='bad'>2</b>" in html_out     # 漏写 用 bad class 标记
    assert "/ 3</td>" in html_out                 # 在飞 无特殊标记


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


def test_runtime_window_links_drop_users_only_runtime_state(client, monkeypatch):
    monkeypatch.setattr(_dt, "_runtime_health_summary", _fake_summary)
    page = client.get(
        "/admin/data-track?view=runtime&hours=24&runtime_state=v2",
        headers=_admin_headers(),
    ).get_data(as_text=True).replace("&amp;", "&")

    assert "view=runtime&hours=168" in page
    assert "view=runtime&hours=720" in page
    assert "view=runtime&hours=168&runtime_state=" not in page
    assert "view=runtime&hours=168&runtime_state=v2" not in page
    assert "view=runtime&hours=720&runtime_state=v2" not in page


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


# ---- Task 2: 渲染两列 token ----


def _tokens(lane_name: str = "chat", **overrides) -> dict:
    base = {
        "model_calls": 118,
        "usage_reported_calls": 103,
        "usage_coverage": 0.873,
        "prompt_tokens": 951_161,
        "completion_tokens": 40_473,
        "total_tokens": 991_634,
        "cache_read_tokens": 469_353,
        "cache_miss_tokens": 482_000,
        "cache_hit_ratio": 0.493,
        "cache_reported_calls": 59,
        "cache_coverage": 0.5,
    }
    base.update(overrides)
    return {"window_hours": 24, "lanes": {lane_name: base}}


def test_fmt_tokens_compact_covers_all_branches():
    assert _dt._fmt_tokens_compact(None) == "—"
    assert _dt._fmt_tokens_compact(951) == "951"
    assert _dt._fmt_tokens_compact(951_161) == "951.2k"
    assert _dt._fmt_tokens_compact(1_200_000) == "1.2M"


def test_fmt_tokens_compact_promotes_at_true_rounding_boundary():
    # 真实边界是 999_950（.1f 在 999.95 处四舍五入进位），不是天真猜测的
    # 999_500。[999_950, 1_000_000) 必须显示成 M 而非 "1000.0k"；
    # M→B 是同一个 bug 的更高一档，[999_950_000, 1_000_000_000) 必须显示
    # 成 B 而非 "1000.0M"。
    assert _dt._fmt_tokens_compact(999_949) == "999.9k"    # 刚好在边界之下
    assert _dt._fmt_tokens_compact(999_950) == "1.0M"      # 边界值本身：升档
    assert _dt._fmt_tokens_compact(999_949_999) == "999.9M"
    assert _dt._fmt_tokens_compact(999_950_000) == "1.0B"


def _lane_row_html(html_out: str, lane_name: str) -> str:
    """抽取给定 lane 那一行 `<tr>...</tr>` 的 HTML。

    避免"整页里有没有这个子串"这种宽断言——多 lane 共存时，另一行的数字
    串到这行里也会让宽断言误判为通过（I-2 的教训：heartbeat 的数字必须只
    出现在 heartbeat 自己那行，不能出现在 chat 行里）。
    """
    marker = f"<b>{lane_name}"
    start = html_out.index(marker)
    row_start = html_out.rindex("<tr>", 0, start)
    row_end = html_out.index("</tr>", start) + len("</tr>")
    return html_out[row_start:row_end]


def test_render_runtime_health_page_shows_token_columns(bound_request):
    html_out = _dt._render_runtime_health_page(_payload(), _tokens())
    assert "951.2k" in html_out          # prompt
    assert "40.5k" in html_out           # completion
    assert "49.3%" in html_out           # cache 命中率
    assert "87.3%" in html_out           # usage 上报覆盖率
    assert "token 入/出" in html_out      # 表头
    # 命中率与两种上报覆盖率分列（2026-07-30 审计：原先混在一列里的「上报」
    # 指 usage 上报，读者会误解成 cache 上报）
    assert "缓存命中" in html_out
    assert "上报 usage/cache" in html_out


def test_render_runtime_health_page_token_columns_are_dash_without_data(bound_request):
    # 某 lane 有 job 但无任何 turn metric 行——两列显 —，且不得抛 KeyError。
    # payload 里的 lane 是 chat，tokens 里只有 maintenance 的开销数据（一个
    # payload 里完全没有的 lane，用来同时验证 I-2 的并集不会把它的数字串到
    # chat 行上；maintenance 本身如何渲染见
    # test_render_runtime_health_page_includes_token_only_lane）。
    html_out = _dt._render_runtime_health_page(_payload(), _tokens(lane_name="maintenance"))
    assert "token 入/出" in html_out
    chat_row = _lane_row_html(html_out, "chat")
    # 精确断言：chat 行的 token / 缓存命中 / 上报覆盖 三列都渲染成 muted 的 —。
    # 只写 `assert "—" in html_out` 是无效断言——页面别处本来就有 —。
    # 数量是 3 而非 2：命中率与上报覆盖率已拆成两列（2026-07-30 审计）。
    # 新增控制/抑制拆分后，旧 payload 对这两列也只能显示未知（—）。
    assert chat_row.count("<td class='muted'>—</td>") == 5
    # maintenance 的数字绝不能串到 chat 行上
    assert "951.2k" not in chat_row


def test_render_runtime_health_page_includes_token_only_lane(bound_request):
    # I-2：recent_runtime_health 的每条子查询共享 LIMIT 1000 配额，
    # recent_token_usage_by_lane 是窗口内全量、无 LIMIT——一条"窗口内有 token
    # 开销、但 job 没挤进最近 1000 条"的 lane，若渲染层只遍历 payload["lanes"]，
    # 它的开销不显示也不报错，而消灭这类盲区正是本功能存在的理由。
    # payload 里只有 chat；tokens 里额外有一个 payload 完全不知道的 maintenance。
    html_out = _dt._render_runtime_health_page(_payload(), _tokens(lane_name="maintenance"))
    row = _lane_row_html(html_out, "maintenance")
    # token 两列必须正常显示真实数字，不是 —
    assert "951.2k" in row
    assert "40.5k" in row
    assert "49.3%" in row
    assert "87.3%" in row
    # 健康列（样本/成功/失败/过期/superseded/失败率/p50/p95/capture）没有
    # 任何数据来源——必须显 —，不得显 0（0 意味着"确认过是零"，这里是
    # "压根没被健康查询看见"）。新表把控制/抑制与两种失败率拆开，故共有
    # 7 个 muted dash；样本/成功/失败/过期/系统故障五列不带 muted class，
    # 内容也必须是 — 而非 0。
    assert row.count("<td class='muted'>—</td>") == 7
    assert "<td>—</td>" in row  # 样本/成功/失败/过期这几列（不带 muted class）
    assert ">0<" not in row and "0 / 0" not in row


def test_render_runtime_health_page_token_only_lane_does_not_affect_health_level(bound_request):
    # token-only 的合成行不该参与 _runtime_health_level 的判定——那是纯 job
    # 结局层面的判断，这条 lane 没有任何 job 结局信息可供判定。
    payload = _payload([_lane(lane="chat", failure_rate=0.0)])
    html_out = _dt._render_runtime_health_page(
        payload,
        _tokens(lane_name="maintenance"),
        delivery={},
    )
    assert "综合告警档位（取三项最差）：<span class=\"ok\">正常</span>" in html_out


def test_render_runtime_health_page_tolerates_missing_tokens_arg(bound_request):
    # 不传 tokens（Task 3 接线前的中间状态）必须仍可渲染
    html_out = _dt._render_runtime_health_page(_payload())
    assert "各 lane 健康" in html_out
    assert "token 入/出" in html_out


def test_render_runtime_health_page_explains_token_scope(bound_request):
    html_out = _dt._render_runtime_health_page(_payload(), _tokens())
    assert "失败回合" in html_out        # token 含失败回合
    assert "不要与缓存列相加" in html_out  # prompt 已含 cache read/write


def test_render_runtime_health_page_declares_window_difference(bound_request):
    # spec §6：两页口径不同必须写明，否则数字对不上会被当成 bug
    html_out = _dt._render_runtime_health_page(_payload(), _tokens())
    assert "Token 与模型" in html_out
    assert "默认近 30 天" in html_out
    assert "不是 bug" in html_out


# ---- Task 3: 接线 —— 窗口算一次传两处 ----


def _fake_tokens(**kw) -> dict:
    return {
        "window_hours": kw.get("within_hours", 24),
        "lanes": {"chat": {
            "model_calls": 10, "usage_reported_calls": 9, "usage_coverage": 0.9,
            "prompt_tokens": 500_000, "completion_tokens": 20_000,
            "total_tokens": 520_000, "cache_read_tokens": 300_000,
            "cache_miss_tokens": 200_000, "cache_hit_ratio": 0.6,
        }},
    }


def test_runtime_view_passes_same_window_to_both_data_functions(client, monkeypatch):
    # 方案 B 的核心风险：两个数据函数的窗口必须同步。窗口在 page_html 里算一次、
    # 传给两处，因此不可能出现一个 24 小时、一个 720 小时。
    seen = {}

    def _health(**kw):
        seen["health"] = kw.get("within_hours")
        return _fake_summary(**kw)

    def _tokens(**kw):
        seen["tokens"] = kw.get("within_hours")
        return _fake_tokens(**kw)

    monkeypatch.setattr(_dt, "_runtime_health_summary", _health)
    monkeypatch.setattr(_dt, "_runtime_token_by_lane", _tokens)

    client.get("/admin/data-track?view=runtime&hours=168", headers=_admin_headers())
    assert seen["health"] == 168
    assert seen["tokens"] == 168

    client.get("/admin/data-track?view=runtime&hours=99999", headers=_admin_headers())
    assert seen["health"] == 24      # 非法值两处一起回落
    assert seen["tokens"] == 24


def test_runtime_view_renders_token_columns_end_to_end(client, monkeypatch):
    monkeypatch.setattr(_dt, "_runtime_health_summary", _fake_summary)
    monkeypatch.setattr(_dt, "_runtime_token_by_lane", _fake_tokens)
    page = client.get("/admin/data-track?view=runtime", headers=_admin_headers()).get_data(as_text=True)
    assert "token 入/出" in page
    assert "500.0k" in page
    assert "60.0%" in page


def test_runtime_view_keeps_health_when_only_token_query_fails(client, monkeypatch):
    """token 查询炸掉**不得**拖垮整张健康页。

    2026-07-30 审计指出的设计缺陷：原先两次数据调用共用一个 try，于是 token 聚合
    （无 LIMIT、走 seq scan、扫描量随表增长单调变大的那条查询）一旦超时，健康数据
    明明是好的、整页也会退化成降级页。而这一页恰恰是出事时才被打开的——把核心
    可用信息一起丢掉是最坏的失败模式。token 是附加信息：它挂了应该只让两列显 —。
    """
    def _boom(**_kw):
        raise RuntimeError("token pool exhausted")

    monkeypatch.setattr(_dt, "_runtime_health_summary", _fake_summary)
    monkeypatch.setattr(_dt, "_runtime_token_by_lane", _boom)
    res = client.get("/admin/data-track?view=runtime", headers=_admin_headers())
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    # 核心健康数据仍在
    assert "各 lane 健康" in body
    assert "Runtime 健康数据暂时取不到" not in body
    # token 列退化成无数据，而不是 0
    assert "token 入/出" in body
    # 异常细节不外泄
    assert "token pool exhausted" not in body


def test_runtime_view_degrades_when_health_query_fails(client, monkeypatch):
    # 健康数据是这页的核心，它没了才走降级页
    def _boom(**_kw):
        raise RuntimeError("health pool exhausted")

    monkeypatch.setattr(_dt, "_runtime_health_summary", _boom)
    monkeypatch.setattr(_dt, "_runtime_token_by_lane", _fake_tokens)
    res = client.get("/admin/data-track?view=runtime", headers=_admin_headers())
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    assert "Runtime 健康数据暂时取不到" in body
    assert "health pool exhausted" not in body


def test_runtime_token_by_lane_is_wired_to_jobs_store():
    # 装配段必须把桩换成真实实现，否则 token 列永远空白而不报任何错
    import asgi_app  # noqa: F401
    from model_api_runtime.v2 import jobs_store

    assert _dt._runtime_token_by_lane is jobs_store.recent_token_usage_by_lane


def test_render_runtime_health_page_separates_two_coverages(bound_request):
    """两种 coverage 必须分别标注，不能混在一个「上报」里。

    2026-07-30 审计：原先那一列是 `cache_hit_ratio · usage_coverage`、表头写
    「缓存命中 · 上报」——读者会把"上报"理解成 cache 上报，而它其实是 token usage
    上报。两者是不同的量（实测 prod 上 usage 覆盖 87% 而 cache 覆盖可能只有一半）。
    """
    html_out = _dt._render_runtime_health_page(_payload(), _tokens())
    # 三个数各自可见
    assert "49.3%" in html_out      # 缓存命中率
    assert "87.3%" in html_out      # usage 上报覆盖
    assert "50.0%" in html_out      # cache 上报覆盖
    # 表头把两种 coverage 分开写明，不再是笼统的「上报」
    assert "缓存命中" in html_out
    assert "上报 usage/cache" in html_out
    assert "缓存命中 · 上报</th>" not in html_out   # 旧的混淆写法必须消失


def test_render_runtime_health_page_coverage_columns_dash_without_data(bound_request):
    html_out = _dt._render_runtime_health_page(_payload(), _tokens(lane_name="other"))
    # chat 行 token/命中/覆盖均未知；旧 payload 的控制/抑制列也未知。
    assert _lane_row_html(html_out, "chat").count("<td class='muted'>—</td>") == 5


def test_runtime_page_links_drop_params_it_ignores():
    """runtime 页只读 hours；它自己生成的链接不得传播它无视的参数。

    2026-07-30 审计实证：有人拿着 `?view=runtime&day=2026-07-25&limit=...&offset=...`
    的 URL 截图，据此以为看的是 7 月 25 日的数据——而这一页只读 hours，实际渲染的是
    "生成时刻向前 24 小时"。参数看着生效、其实被忽略，比参数报错更危险：页顶还写着
    「窗口 24 小时」，读者却相信自己在看某一天。
    """
    # 范围：**本页自己的控件**（三个窗口切换按钮）不得传播它读不到的参数。
    # 顶部 nav 由全视图共用的 `_render_data_track_view_nav` 生成，给它开 runtime
    # 特例等于把单页面的参数知识塞进通用组件——从别处带进来的 URL 由说明文字兜
    # （见 test_runtime_page_explains_it_only_reads_hours）。
    with _admin_core.bind("view=runtime&day=2026-07-25&limit=50&offset=100&hours=168"):
        html_out = _dt._render_runtime_health_page(_payload(), _tokens())

    for label in ("24 小时", "7 天", "30 天"):
        # 取该按钮 <a ...>label</a> 里的 href
        idx = html_out.index(f">{label}</a>")
        href = html_out[html_out.rindex("href='", 0, idx):idx]
        assert "hours=" in href, f"{label} 按钮丢了 hours: {href}"
        assert "day=" not in href, f"{label} 按钮带上了 day: {href}"
        assert "limit=" not in href, f"{label} 按钮带上了 limit: {href}"
        assert "offset=" not in href, f"{label} 按钮带上了 offset: {href}"


def test_runtime_page_explains_it_only_reads_hours(bound_request):
    html_out = _dt._render_runtime_health_page(_payload(), _tokens())
    assert "本页只按 hours" in html_out


def test_runtime_page_heartbeat_link_also_drops_ignored_params(bound_request):
    """heartbeat → Proactive 日报的链接同样不得传播本页忽略的参数。

    review 指出：常量注释写的是"本页自己生成的链接一律清掉"，但最初只在三个窗口
    按钮上做了，同一函数里的 hb_href 漏了。而目标页 Proactive 日报只读
    since/registered_since/days（复数），**从不读单数 day**，limit/offset 在它的
    payload 里也没用——所以带过去之后照样是"看着生效实则被无视"，只是换了一跳、
    可见度更低。要么清掉，要么把注释里的"一律"改成如实描述；这里选清掉。
    """
    with _admin_core.bind("view=runtime&day=2026-07-25&limit=50&offset=100&hours=168"):
        html_out = _dt._render_runtime_health_page(
            _payload([_lane(lane="heartbeat")]), _tokens(lane_name="heartbeat")
        )
    idx = html_out.index("（日报口径）")
    href = html_out[html_out.rindex("href='", 0, idx):idx]
    assert "view=proactive" in href
    assert "day=" not in href, f"day 被带去日报页: {href}"
    assert "limit=" not in href, f"limit 被带去日报页: {href}"
    assert "offset=" not in href, f"offset 被带去日报页: {href}"


# ---------------------------------------------------------------------------
# 2026-07-30 审计后续：capture 语义降级 + 端到端交付
# ---------------------------------------------------------------------------


def _delivery(**overrides) -> dict:
    base = {
        "window_hours": 24,
        "effect_outbox": {"pending": 0, "oldest_pending_age_sec": None},
        "terminal_failure_outbox": {
            "status_undelivered": 0,
            "runtime_error_undelivered": 0,
            "oldest_undelivered_age_sec": None,
        },
        "mcp_mutation": {"unknown": 0, "unresolved": 0},
    }
    base.update(overrides)
    return base


def test_runtime_health_level_warns_on_trajectory_gap():
    # 回归测试（审计实证）：partial 此前完全不参与判定，于是页面同时显示"有 1 个
    # partial"和综合告警档位"正常"。缺口是真实的取证损失，至少 warn。
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(capture={
            "terminal_seen_no_gap": 99, "partial": 1, "missing": 0, "open": 0,
        })])
    )
    assert level == "warn"
    assert any("缺口" in r for r in reasons)


def test_runtime_health_level_missing_still_outranks_gap():
    # 漏写(bad) 与 有缺口(warn) 同时存在时取最差档，不能被 warn 盖住。
    level, _ = _dt._runtime_health_level(
        _payload([_lane(capture={
            "terminal_seen_no_gap": 0, "partial": 5, "missing": 1, "open": 0,
        })])
    )
    assert level == "bad"


def test_runtime_health_level_ignores_absent_delivery():
    # delivery 是独立失败域，取不到时（None）不得凭空降级，也不得抛异常。
    level, reasons = _dt._runtime_health_level(_payload(), None)
    assert level == "ok"
    assert reasons == []


def test_runtime_health_level_clean_delivery_stays_green():
    level, reasons = _dt._runtime_health_level(_payload(), _delivery())
    assert level == "ok"
    assert reasons == []


def test_runtime_health_level_uses_delivery_age_thresholds():
    warn, warn_reasons = _dt._runtime_health_level(_payload(), _delivery(
        effect_outbox={"pending": 3, "oldest_pending_age_sec": 4000},
    ))
    bad, _ = _dt._runtime_health_level(_payload(), _delivery(
        effect_outbox={"pending": 3, "oldest_pending_age_sec": 30_000},
    ))
    assert warn == "warn"
    assert bad == "bad"
    assert any("副作用" in r for r in warn_reasons)


def test_runtime_health_level_ignores_backlog_size_without_age():
    # 只按年龄判定：积压 5000 条但秒级排空是健康的高吞吐，不该点红。
    level, _ = _dt._runtime_health_level(_payload(), _delivery(
        effect_outbox={"pending": 5000, "oldest_pending_age_sec": 3},
    ))
    assert level == "ok"


def test_runtime_health_level_warns_on_undelivered_terminal_failure():
    level, reasons = _dt._runtime_health_level(_payload(), _delivery(
        terminal_failure_outbox={
            "status_undelivered": 2,
            "runtime_error_undelivered": 0,
            "oldest_undelivered_age_sec": 5000,
        },
    ))
    assert level == "warn"
    assert any("终态失败投递" in r for r in reasons)


def test_runtime_health_level_warns_on_unknown_mcp_mutation():
    # 远端改动结果未知：稀有、不可自愈、见一条就该有人看，故不设阈值。
    level, reasons = _dt._runtime_health_level(_payload(), _delivery(
        mcp_mutation={"unknown": 1, "unresolved": 0},
    ))
    assert level == "warn"
    assert any("未知" in r for r in reasons)


def test_runtime_health_level_warns_on_dangling_mcp_mutation():
    level, reasons = _dt._runtime_health_level(_payload(), _delivery(
        mcp_mutation={"unknown": 0, "unresolved": 2},
    ))
    assert level == "warn"
    assert any("悬空" in r for r in reasons)


def test_render_runtime_health_page_shows_delivery_section(bound_request):
    html_out = _dt._render_runtime_health_page(_payload(), None, _delivery(
        effect_outbox={"pending": 7, "oldest_pending_age_sec": 4000},
        terminal_failure_outbox={
            "status_undelivered": 1,
            "runtime_error_undelivered": 2,
            "oldest_undelivered_age_sec": 90,
        },
        mcp_mutation={"unknown": 3, "unresolved": 4},
    ))
    assert "端到端交付" in html_out
    assert "副作用积压" in html_out
    assert "7" in html_out
    assert "1h6m" in html_out       # 4000s
    assert "1 / 2" in html_out      # status / runtime_error 分开计数
    assert "3 / 4" in html_out      # unknown / unresolved 分开计数


def test_render_runtime_health_page_delivery_unavailable_is_not_zero(bound_request):
    # 取不到必须明说取不到。这个区块显 0 的含义是"队列空、全都送达了"——与
    # "数据取不到"是相反的结论，拿 0 顶替等于报了个假的好消息。
    html_out = _dt._render_runtime_health_page(_payload(), None, None)
    assert "端到端交付数据暂时取不到" in html_out
    assert "副作用积压" not in html_out


def test_render_runtime_health_page_tolerates_missing_delivery_arg(bound_request):
    # 老调用点（只传 payload / payload+tokens）不得因新参数而崩。
    assert "Runtime 健康" in _dt._render_runtime_health_page(_payload())
    assert "Runtime 健康" in _dt._render_runtime_health_page(_payload(), None)


def test_render_runtime_health_page_capture_header_states_what_it_proves(bound_request):
    html_out = _dt._render_runtime_health_page(_payload())
    # 表头与正文都不得再把这个桶叫「完整」——它证明不了轨迹可完整回放。
    assert "见终态·无缺口" in html_out
    assert "捕获 完整/部分/漏写/在飞" not in html_out


def test_render_runtime_health_page_declares_selfhost_coverage_gap(bound_request):
    # 「全用户 token 用量」的错误安全感：本页只有本实例托管的回合。
    html_out = _dt._render_runtime_health_page(_payload())
    assert "self-host" in html_out
    assert "不是全体用户的总量" in html_out


def test_render_runtime_health_page_no_longer_claims_sampling_cap(bound_request):
    # 采样上界已随 recent_runtime_health 去掉；页面上那句解释必须同步消失，
    # 否则它会变成一句"看着权威实则过时"的说明。
    html_out = _dt._render_runtime_health_page(_payload())
    assert "最近 1000 个 job" not in html_out
    assert "都是窗口内全量" in html_out


def test_runtime_delivery_health_is_wired_to_jobs_store():
    import asgi_app  # noqa: F401
    from model_api_runtime.v2 import jobs_store

    assert _dt._runtime_delivery_health is jobs_store.recent_delivery_health


def test_runtime_view_keeps_health_when_only_delivery_query_fails(client, monkeypatch):
    # 第三个独立失败域：交付查询炸了，健康数据仍然要送出去。
    def _boom(**_kw):
        raise RuntimeError("outbox table locked")

    monkeypatch.setattr(_dt, "_runtime_health_summary", _fake_summary)
    monkeypatch.setattr(_dt, "_runtime_delivery_health", _boom)
    res = client.get("/admin/data-track?view=runtime", headers=_admin_headers())
    body = res.get_data(as_text=True)

    assert res.status_code == 200
    assert "各 lane 健康" in body                      # 健康数据照常渲染
    assert "端到端交付数据暂时取不到" in body           # 只有这个区块降级
    assert "outbox table locked" not in body           # 异常细节不外泄


def test_runtime_view_passes_window_to_delivery_too(client, monkeypatch):
    # 三个数据函数必须收到同一个窗口，否则同页三块数据口径不一致。
    seen = {}

    def _capture_delivery(**kw):
        seen.update(kw)
        return _delivery()

    monkeypatch.setattr(_dt, "_runtime_health_summary", _fake_summary)
    monkeypatch.setattr(_dt, "_runtime_delivery_health", _capture_delivery)
    client.get("/admin/data-track?view=runtime&hours=168", headers=_admin_headers())

    assert seen["within_hours"] == 168


# ---- Task 3: 每用户 Runtime V2 token/model 与交付可靠性报表 ----


def _user_report() -> dict:
    empty_failure = {
        "reply_delivered_in_window": 0,
        "reply_undelivered": 0,
        "status_delivered_in_window": 0,
        "status_undelivered": 0,
        "runtime_error_delivered_in_window": 0,
        "runtime_error_undelivered": 0,
    }
    return {
        "window_hours": 24,
        "users": [
            {
                "user_id": "usr_report_a",
                "known_total_tokens": 12_900,
                "model_calls": 18,
                "models": [{
                    "provider": "anthropic",
                    "model": "claude-example",
                    "route": "route-fingerprint",
                    "lanes": ["chat", "heartbeat"],
                    "turns": 12,
                    "model_calls": 18,
                    "retries": 2,
                    "usage_reported_calls": 17,
                    "cache_reported_calls": 16,
                    "usage_coverage": 17 / 18,
                    "cache_coverage": 16 / 18,
                    "prompt_tokens": 12_000,
                    "completion_tokens": 900,
                    "total_tokens": 12_900,
                    "cache_read_tokens": 8_000,
                    "cache_write_tokens": 500,
                    "cache_miss_tokens": 4_000,
                    "cache_hit_ratio": 2 / 3,
                }],
                "delivery": {
                    "reply_effects": {
                        "applied_in_window": 10,
                        "pending": 1,
                        "needs_reconciliation": 0,
                    },
                    "status_effects": {
                        "applied_in_window": 4,
                        "pending": 0,
                        "needs_reconciliation": 0,
                    },
                    "all_effects": {
                        "applied_in_window": 24,
                        "discarded_in_window": 1,
                        "pending": 1,
                        "needs_reconciliation": 0,
                    },
                    "terminal_failure": dict(empty_failure),
                    "oldest_unfinished_age_sec": 3600,
                },
            },
            {
                "user_id": "usr_delivery_only",
                "known_total_tokens": None,
                "model_calls": 0,
                "models": [],
                "delivery": {
                    "reply_effects": {
                        "applied_in_window": 0,
                        "pending": 0,
                        "needs_reconciliation": 0,
                    },
                    "status_effects": {
                        "applied_in_window": 0,
                        "pending": 0,
                        "needs_reconciliation": 1,
                    },
                    "all_effects": {
                        "applied_in_window": 0,
                        "discarded_in_window": 0,
                        "pending": 0,
                        "needs_reconciliation": 1,
                    },
                    "terminal_failure": dict(empty_failure),
                    "oldest_unfinished_age_sec": 60,
                },
            },
        ],
    }


def test_runtime_user_delivery_level_uses_reconciliation_and_age_thresholds():
    # A fresh pending count alone is intentionally not degraded; reconciliation
    # and stale outstanding delivery are the observable delivery failures.
    assert _dt._runtime_user_delivery_level({
        "all_effects": {"needs_reconciliation": 1},
        "oldest_unfinished_age_sec": 1,
    }) == "bad"
    assert _dt._runtime_user_delivery_level({
        "all_effects": {"needs_reconciliation": 0},
        "oldest_unfinished_age_sec": 3600,
    }) == "warn"
    assert _dt._runtime_user_delivery_level({
        "all_effects": {"needs_reconciliation": 0},
        "oldest_unfinished_age_sec": 21600,
    }) == "bad"
    assert _dt._runtime_user_delivery_level({
        "all_effects": {"needs_reconciliation": 0},
        "oldest_unfinished_age_sec": 30,
    }) == "ok"
    # Fresh volume is not a delivery failure: an active worker can safely
    # drain hundreds of effects.  This catches a regression that mistakenly
    # degrades solely from the current pending count.
    assert _dt._runtime_user_delivery_level({
        "all_effects": {"pending": 999, "needs_reconciliation": 0},
        "oldest_unfinished_age_sec": 30,
    }) == "ok"
    assert _dt._runtime_user_delivery_level({
        "all_effects": {"pending": 999, "needs_reconciliation": 0},
        "oldest_unfinished_age_sec": None,
    }) == "ok"


def test_render_runtime_health_page_shows_user_delivery_without_model_usage(bound_request):
    html_out = _dt._render_runtime_health_page(
        _payload(), _tokens(), _delivery(), _user_report()
    )
    assert "用户交付可靠性" in html_out
    assert "Reply effects" in html_out
    assert "Failure reply/status/error" in html_out
    assert "needs_reconciliation" in html_out
    assert "usr_delivery_only" in html_out
    assert "<span class='pill bad'>异常</span>" in html_out
    assert "needs_reconciliation 1" in html_out
    assert "ok 不代表客户端已读" in html_out
    assert "不受所选时间窗口限制" in html_out
    assert "用户 Token / Model 与交付可靠性" not in html_out
    assert "claude-example" not in html_out
    assert 'class="runtime-user-models"' not in html_out
    assert 'class="runtime-user-delivery"' in html_out


def test_render_runtime_user_report_links_user_once_in_delivery_row(bound_request):
    report = _user_report()

    html_out = _dt._render_runtime_health_page(
        _payload(), _tokens(), _delivery(), report
    )
    table_start = html_out.index('<table class="runtime-user-delivery">')
    table_end = html_out.index("</table>", table_start)
    delivery_table = html_out[table_start:table_end]
    assert delivery_table.count("/admin/data-track/users/usr_report_a") == 1


def test_render_runtime_user_report_explains_user_id_attribution(bound_request):
    html_out = _dt._render_runtime_health_page(
        _payload(), _tokens(), _delivery(), _user_report()
    )
    assert "按 user_id 统计" in html_out
    assert "不按真人/principal 合并" in html_out
    assert "重新注册可能显示多行" in html_out


def test_render_runtime_user_report_does_not_link_unknown_user_id(bound_request):
    report = _user_report()
    report["users"][0]["user_id"] = "unknown"
    html_out = _dt._render_runtime_health_page(
        _payload(), _tokens(), _delivery(), report
    )
    assert "<code>unknown</code>" in html_out
    assert "/admin/data-track/users/unknown" not in html_out


def test_render_runtime_user_report_preserves_unknowns_and_escapes(bound_request):
    report = _user_report()
    report["users"][0]["user_id"] = "usr_<unsafe>"
    html_out = _dt._render_runtime_health_page(
        _payload(), _tokens(), _delivery(), report
    )
    assert "usr_<unsafe>" not in html_out
    assert "usr_&lt;unsafe&gt;" in html_out
    assert "usr_%3Cunsafe%3E" in html_out


def test_render_runtime_user_report_links_keep_current_admin_query_string():
    # The model and delivery tables link a user row back to Admin detail
    # without dropping the analyst's current runtime filters.
    with _admin_core.bind("q=needle&view=runtime&hours=168"):
        html_out = _dt._render_runtime_health_page(
            _payload(), _tokens(), _delivery(), _user_report()
        )
    assert (
        "href='/admin/data-track/users/usr_report_a?"
        "q=needle&amp;view=runtime&amp;hours=168'"
    ) in html_out


def test_render_runtime_health_page_user_report_unavailable_is_local(bound_request):
    html_out = _dt._render_runtime_health_page(
        _payload(), _tokens(), _delivery(), None
    )
    assert "用户交付可靠性暂时取不到" in html_out
    assert "各 lane 健康" in html_out
    assert "端到端交付" in html_out


def test_render_runtime_user_report_empty_window_is_not_unavailable(bound_request):
    html_out = _dt._render_runtime_health_page(
        _payload(), _tokens(), _delivery(), {"window_hours": 168, "users": []}
    )
    assert "所选 168 小时窗口没有用户指标或当前待交付项" in html_out
    assert "暂时取不到" not in html_out
    assert "colspan='7'" in html_out


# ---- Task 4: 每用户报表路由编排与装配 ----


def test_runtime_view_passes_same_window_to_user_report(monkeypatch):
    """路由漏传窗口会让每用户表和其余 Runtime 区块口径不一致。"""
    seen = {}

    def _health(**kwargs):
        seen["health"] = kwargs["within_hours"]
        return _payload()

    def _tokens_for_window(**kwargs):
        seen["tokens"] = kwargs["within_hours"]
        return _tokens()

    def _delivery_for_window(**kwargs):
        seen["delivery"] = kwargs["within_hours"]
        return _delivery()

    def _users(**kwargs):
        seen["users"] = kwargs["within_hours"]
        return _user_report()

    monkeypatch.setattr(_dt, "_runtime_health_summary", _health)
    monkeypatch.setattr(_dt, "_runtime_token_by_lane", _tokens_for_window)
    monkeypatch.setattr(_dt, "_runtime_delivery_health", _delivery_for_window)
    monkeypatch.setattr(_dt, "_runtime_user_report", _users)

    body = _admin_core.page_html("view=runtime&hours=168")

    assert seen == {
        "health": 168,
        "tokens": 168,
        "delivery": 168,
        "users": 168,
    }
    assert "用户交付可靠性" in body


def test_runtime_user_report_failure_does_not_hide_health(monkeypatch):
    """用户聚合超时只能降级自己的区块，不能遮蔽可用健康数据。"""
    tokens = _tokens()
    delivery = _delivery(
        effect_outbox={"pending": 7, "oldest_pending_age_sec": None},
        terminal_failure_outbox={
            "status_undelivered": 3,
            "runtime_error_undelivered": 4,
            "oldest_undelivered_age_sec": None,
        },
    )
    monkeypatch.setattr(_dt, "_runtime_health_summary", lambda **_kw: _payload())
    monkeypatch.setattr(_dt, "_runtime_token_by_lane", lambda **_kw: tokens)
    monkeypatch.setattr(_dt, "_runtime_delivery_health", lambda **_kw: delivery)

    def _boom(**_kw):
        raise RuntimeError("user report db")

    monkeypatch.setattr(_dt, "_runtime_user_report", _boom)

    body = _admin_core.page_html("view=runtime&hours=24")

    assert "Runtime 健康" in body
    assert "各 lane 健康" in body
    assert "951.2k" in body
    assert "<div class='metric-value'>3 / 4</div>" in body
    assert "用户交付可靠性暂时取不到" in body
    assert "user report db" not in body


def test_runtime_user_report_is_wired_to_jobs_store():
    """ASGI 装配遗漏时桩会悄然返回空表，必须绑定真实聚合。"""
    import asgi_app  # noqa: F401
    from model_api_runtime.v2 import jobs_store

    assert _dt._runtime_user_report is jobs_store.recent_runtime_user_delivery_report
