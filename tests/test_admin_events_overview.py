"""DB-backed coverage for db.admin_events_overview().

The event-health board depends on PostgreSQL JSONB aggregation and route joins,
so this intentionally follows tests/test_db.py: it runs only when conftest has
provisioned a real test database.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set - needs a real Postgres", allow_module_level=True)

import db  # noqa: E402
from admin import data_track as data_track_module  # noqa: E402
from conftest import seed_user  # noqa: E402

db.init_schema()

_SEED_CREATED_AT = "2026-08-14T00:00:00+00:00"
_SEEDED_USER_IDS: set[str] = set()


@pytest.fixture(autouse=True)
def _cleanup_seeded_users():
    _SEEDED_USER_IDS.clear()
    try:
        yield
    finally:
        user_ids = tuple(_SEEDED_USER_IDS)
        if user_ids:
            try:
                with db.get_pool().connection() as conn:
                    conn.execute(
                        "DELETE FROM v2_wake_schedule WHERE user_id = ANY(%s)",
                        (list(user_ids),),
                    )
                    conn.execute(
                        "DELETE FROM users WHERE user_id = ANY(%s)",
                        (list(user_ids),),
                    )
            finally:
                from accounts import registry

                with registry._users_lock:
                    registry._users[:] = [
                        row
                        for row in registry._users
                        if row.get("user_id") not in user_ids
                    ]
        _SEEDED_USER_IDS.clear()


def _seed_test_user(user_id: str) -> None:
    _SEEDED_USER_IDS.add(user_id)
    seed_user(user_id, created_at=_SEED_CREATED_AT)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _iso(base: datetime, seconds: int) -> str:
    return (base + timedelta(seconds=seconds)).isoformat()


def test_admin_events_overview_aggregates_routes_events_and_durations():
    now = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
    u_res = _uid("events_res")
    u_api = _uid("events_api")
    u_import = _uid("events_import")
    for uid in (u_res, u_api, u_import):
        _seed_test_user(uid)
    db.set_blob(u_res, "onboarding_route", {"route": "resident"})
    db.set_blob(u_api, "onboarding_route", {"route": "model_api"})
    db.set_blob(u_import, "onboarding_route", {"route": "official_import"})
    before = db.admin_events_overview()

    db.log_append(u_api, "proactive_jobs", {
        "job_id": "pj_screen",
        "job_kind": "screen_tick",
        "status": "delivered",
        "created_at": _iso(now, 0),
        "posted_at": _iso(now, 3),
    }, ts=now.timestamp(), item_key="pj_screen")
    db.log_append(u_import, "proactive_jobs", {
        "job_id": "pj_trigger",
        "trigger": "scheduled_wake",
        "status": "failed",
        "created_at": _iso(now, 0),
        "failed_at": _iso(now, 7),
    }, ts=now.timestamp(), item_key="pj_trigger")
    db.log_append(u_import, "proactive_jobs", {
        "job_id": "cap_resident",
        "job_kind": "memory_capture",
        "status": "completed",
        "created_at": _iso(now, 0),
        "completed_at": _iso(now, 20),
    }, ts=now.timestamp(), item_key="cap_resident")
    db.log_append(u_api, "memory_capture_jobs", {
        "job_id": "mc_api",
        "mode": "recap",
        "status": "failed",
        "created_at": _iso(now, 0),
        "completed_at": _iso(now, 30),
    }, ts=now.timestamp(), item_key="mc_api")

    with db.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO genesis_import_jobs
              (user_id, job_id, status, source_kind, metadata, created_at, updated_at, completed_at)
            VALUES
              (%s, %s, %s, %s, %s, now(), now(), now()),
              (%s, %s, %s, %s, %s, now(), now(), NULL)
            """,
            (
                u_res, "g_first", "done", "history", Jsonb({"mode": "onboarding"}),
                u_api, "g_second", "failed", "history", Jsonb({"mode": "add_memory"}),
            ),
        )

    db.chat_append(u_api, "m_user_api", now.timestamp(), {
        "id": "m_user_api", "role": "user", "source": "chat",
    }, 5000)
    db.chat_append(u_api, "m_real_api", now.timestamp() + 1, {
        "id": "m_real_api", "role": "agent", "source": "model_api",
    }, 5000)
    db.chat_append(u_api, "m_fallback_api", now.timestamp() + 2, {
        "id": "m_fallback_api", "role": "agent", "source": "foreground_fallback",
    }, 5000)
    db.chat_append(u_import, "m_proactive_fallback", now.timestamp(), {
        "id": "m_proactive_fallback", "role": "openclaw", "source": "proactive_fallback",
    }, 5000)

    out = db.admin_events_overview()

    def rows(section: str, *keys: str) -> dict[tuple, dict]:
        return {tuple(r[k] for k in keys): r for r in out[section]}

    def before_rows(section: str, *keys: str) -> dict[tuple, dict]:
        return {tuple(r[k] for k in keys): r for r in before[section]}

    def delta(section: str, key: tuple, field: str, *keys: str) -> int:
        after_row = rows(section, *keys).get(key, {})
        before_row = before_rows(section, *keys).get(key, {})
        return int(after_row.get(field) or 0) - int(before_row.get(field) or 0)

    proactive = rows("proactive", "route", "lane")
    assert proactive[("model_api", "screen")]["success"] == 1
    if not before_rows("proactive", "route", "lane").get(("model_api", "screen")):
        assert proactive[("model_api", "screen")]["median_dur"] == pytest.approx(3.0)
    assert proactive[("official_import", "trigger")]["failed"] == 1
    if not before_rows("proactive", "route", "lane").get(("official_import", "trigger")):
        assert proactive[("official_import", "trigger")]["median_dur"] == pytest.approx(7.0)
    assert delta("proactive", ("official_import", "other"), "total", "route", "lane") == 0

    capture = rows("capture", "route")
    assert delta("capture", ("official_import",), "success", "route") == 1
    assert delta("capture", ("model_api",), "failed", "route") == 1
    if not before_rows("capture", "route").get(("official_import",)):
        assert capture[("official_import",)]["median_dur"] == pytest.approx(20.0)
    if not before_rows("capture", "route").get(("model_api",)):
        assert capture[("model_api",)]["median_dur"] == pytest.approx(30.0)

    assert delta("genesis", ("resident", "first"), "success", "route", "distill") == 1
    assert delta("genesis", ("model_api", "second"), "failed", "route", "distill") == 1

    assert delta("reply", ("model_api",), "user_msgs", "route") == 1
    assert delta("reply", ("model_api",), "real_replies", "route") == 1
    assert delta("reply", ("model_api",), "fallback_replies", "route") == 1
    assert delta("reply", ("official_import",), "real_replies", "route") == 0
    assert delta("reply", ("official_import",), "fallback_replies", "route") == 0


# ---------------------------------------------------------------------------
# 按天口径（2026-08-04）
#
# 在此之前这个看板统计的是**全量历史**，而页面 URL 上的 `day=`/`hours=` 参数对它
# 毫无作用。后果是故障被历史稀释到看不见：今天 200 次全挂、历史 10000 次成功，
# 页面照样显示 98% 绿；而且分母只增不减，越往后越钝。
# ---------------------------------------------------------------------------


def _bj_epoch(day: str, hour: int = 12) -> float:
    """北京时间某天某点的 epoch。"""
    from zoneinfo import ZoneInfo
    d = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, tzinfo=ZoneInfo("Asia/Shanghai"))
    return d.timestamp()


def test_events_overview_scopes_counts_to_one_beijing_day():
    u = _uid("events_day")
    _seed_test_user(u)
    db.set_blob(u, "onboarding_route", {"route": "model_api"})

    # 昨天成功、今天失败：按天看应该是两幅完全不同的画面
    db.log_append(u, "proactive_jobs", {
        "job_id": "d1", "job_kind": "heartbeat", "status": "delivered",
    }, ts=_bj_epoch("2026-03-01"), item_key="d1")
    db.log_append(u, "proactive_jobs", {
        "job_id": "d2", "job_kind": "heartbeat", "status": "failed",
    }, ts=_bj_epoch("2026-03-02"), item_key="d2")

    day1 = db.admin_events_overview(day="2026-03-01")
    day2 = db.admin_events_overview(day="2026-03-02")

    def lane(payload, want_lane="heartbeat"):
        return [r for r in payload["proactive"]
                if r["lane"] == want_lane and r["route"] == "model_api"]

    r1 = lane(day1)
    r2 = lane(day2)
    assert len(r1) == 1 and r1[0]["success"] == 1 and r1[0]["failed"] == 0, r1
    assert len(r2) == 1 and r2[0]["success"] == 0 and r2[0]["failed"] == 1, r2


def test_events_overview_does_not_leak_across_the_beijing_midnight():
    """北京 23:59 与次日 00:01 必须落在不同的两天。

    若 SQL 按 UTC 分桶，北京时间当天 00:00-08:00 的事件会被算进"昨天"——运营看到的
    "今天"就少了一整个上午。
    """
    u = _uid("events_mid")
    _seed_test_user(u)
    db.set_blob(u, "onboarding_route", {"route": "model_api"})

    db.log_append(u, "proactive_jobs", {
        "job_id": "late", "job_kind": "heartbeat", "status": "delivered",
    }, ts=_bj_epoch("2026-03-05", hour=23), item_key="late")
    db.log_append(u, "proactive_jobs", {
        "job_id": "early", "job_kind": "heartbeat", "status": "delivered",
    }, ts=_bj_epoch("2026-03-06", hour=0), item_key="early")

    d5 = [r for r in db.admin_events_overview(day="2026-03-05")["proactive"]
          if r["route"] == "model_api" and r["lane"] == "heartbeat"]
    d6 = [r for r in db.admin_events_overview(day="2026-03-06")["proactive"]
          if r["route"] == "model_api" and r["lane"] == "heartbeat"]

    assert len(d5) == 1 and d5[0]["total"] == 1
    assert len(d6) == 1 and d6[0]["total"] == 1


def test_events_overview_rejects_a_malformed_day_instead_of_widening():
    """坏日期必须报错，不能静默回退成全量。

    静默回退正是这次要消灭的行为：调用方以为拿到的是某一天，实际是开天辟地至今。
    """
    with pytest.raises(ValueError):
        db.admin_events_overview(day="2026/03/05")
    with pytest.raises(ValueError):
        db.admin_events_overview(day="yesterday")


def test_events_overview_without_a_day_still_returns_all_time():
    """不传 day 时维持旧行为（全量），供不关心日期的调用方使用。"""
    payload = db.admin_events_overview()
    assert set(payload) == {"proactive", "capture", "genesis", "reply"}


def test_history_import_rolling_rate_counts_every_attempt_not_latest_per_user():
    """A retry success must not erase the same user's earlier failed attempt."""
    uid = _uid("events_import_attempts")
    _seed_test_user(uid)
    now = datetime.now(timezone.utc)
    before = db.admin_history_import_job_rolling_windows()
    db.set_blob(uid, "history_import_job:failed_attempt", {
        "job_id": "failed_attempt",
        "status": "failed",
        "created_at": (now - timedelta(hours=3)).isoformat(),
        "failed_at": (now - timedelta(hours=2)).isoformat(),
    })
    db.set_blob(uid, "history_import_job:successful_retry", {
        "job_id": "successful_retry",
        "status": "completed",
        "created_at": (now - timedelta(hours=2)).isoformat(),
        "completed_at": (now - timedelta(hours=1)).isoformat(),
    })

    payload = db.admin_history_import_job_rolling_windows()
    one_day = next(row for row in payload["windows"]
                   if row["key"] == "rolling_1d")
    before_day = next(row for row in before["windows"]
                      if row["key"] == "rolling_1d")
    assert one_day["completed"] - before_day["completed"] == 1
    assert one_day["failed"] - before_day["failed"] == 1
    assert one_day["denominator"] - before_day["denominator"] == 2


def _master_frozen(*, v1="green", v2="green") -> dict:
    return {
        "timezone": "Asia/Shanghai",
        "closed_through_day": "2030-06-07",
        "windows": [{
            "key": "24h", "start_day": "2030-06-07",
            "end_day": "2030-06-07", "day_count": 1,
            "routes": {
                "resident": {
                    "active_users": 17,
                    "coverage": {"level": v1, "covered_days": 1,
                                 "required_days": 1},
                    "lanes": {"heartbeat": {
                        "completed": 9, "failed": 1,
                        "expired": 0, "superseded": 0,
                        "failure_codes": {"heartbeat_throttled": 1},
                    }},
                    "lane_sources": {},
                },
                "model_api": {
                    "active_users": 5,
                    "coverage": {"level": v2,
                                 "covered_days": 1 if v2 == "green" else 0,
                                 "required_days": 1},
                    "lanes": {"chat": {
                        "completed": 6, "failed": 4,
                        "expired": 0, "superseded": 2,
                        "failure_codes": {
                            "extraction_failed:quota_insufficient": 1,
                            "extraction_failed:upstream_unavailable": 3,
                        },
                    }},
                    "lane_sources": {},
                },
            },
        }],
    }


def _master_row(payload: dict, key: str, *, runtime=False) -> dict:
    windows = payload["runtime_windows"] if runtime else payload["windows"]
    return next(row for row in windows[0]["rows"] if row["key"] == key)


def test_master_does_not_relabel_mixed_v1_runtime_as_an_access_path():
    payload = data_track_module._event_path_master_payload(_master_frozen())
    heartbeat = _master_row(payload, "heartbeat")
    assert heartbeat["cells"]["resident"]["state"] == "unavailable"
    assert heartbeat["cells"]["apikey_v1"]["state"] == "unavailable"
    assert "混合 resident 与 APIKey-V1" in heartbeat["cells"]["apikey_v1"]["detail"]
    assert heartbeat["cells"]["apikey_v2"]["state"] == "metric"

    runtime_heartbeat = _master_row(payload, "heartbeat", runtime=True)
    assert runtime_heartbeat["cells"]["runtime_v1"]["state"] == "unavailable"
    assert "failed 混合 failed 与 skipped" in (
        runtime_heartbeat["cells"]["runtime_v1"]["detail"]
    )
    assert "resident_cli 234 vs V2 32" not in str(payload)
    assert payload["runtime_windows"][0]["active_users"] == {
        "runtime_v1": 17,
        "runtime_v2": 5,
    }
    page = data_track_module._render_event_master_tables(payload)
    assert "冻结行按 user_id 去重" in page
    assert "V1 runtime（混合接入方式） 17 人" in page
    assert "V2 runtime（APIKey） 5 人" in page
    assert "混合接入方式" in payload["runtime_paths"][0]["label"]


def test_complete_zero_and_missing_probe_render_differently():
    metric = data_track_module._render_event_master_cell({
        "state": "metric", "coverage": "green", "success": 0,
        "failure": 0, "denominator": 0, "superseded": 0,
        "denominator_rule": "completed + failed",
    }, action="Capture", path="APIKey-V2", window="最近 1 个已关闭北京日")
    missing = data_track_module._render_event_master_cell({
        "state": "unavailable", "coverage": "red",
        "message": "当前记不到这一级", "detail": "零探针",
    }, action="Capture", path="APIKey-V1", window="最近 1 个已关闭北京日")
    assert "🟢" in metric and "0 次终态作业" in metric
    assert "🔴" in missing and "当前记不到这一级" in missing
    assert metric != missing


def test_runtime_population_does_not_turn_missing_or_partial_into_zero():
    frozen = _master_frozen(v2="yellow")
    del frozen["windows"][0]["routes"]["resident"]["active_users"]
    payload = data_track_module._event_path_master_payload(frozen)
    assert payload["runtime_windows"][0]["active_users"] == {
        "runtime_v1": None,
        "runtime_v2": None,
    }
    page = data_track_module._render_event_master_tables(payload)
    assert page.count("人数不报（冻结覆盖不完整或来源未提供）") == 2
    assert "V1 runtime（混合接入方式） 0 人" not in page
    assert "V2 runtime（APIKey） 0 人" not in page


def test_route_population_is_never_rendered_as_an_access_path_population():
    payload = data_track_module._event_path_master_payload(_master_frozen())

    runtime_window = {
        **payload["runtime_windows"][0],
        "active_users": {"runtime_v1": 9, "runtime_v2": 9},
    }
    runtime_page = data_track_module._render_event_master_tables({
        **payload,
        "windows": [],
        "runtime_windows": [runtime_window],
    })
    # First prove the probe is wired correctly: this exact field is visible on
    # the runtime table before we rely on its absence from the path table.
    assert "窗口内 runtime route 活跃用户" in runtime_page
    assert "V1 runtime（混合接入方式） 9 人" in runtime_page
    assert "V2 runtime（APIKey） 9 人" in runtime_page

    path_window = {
        **payload["windows"][0],
        "active_users": {
            "resident": 9,
            "apikey_v1": 9,
            "apikey_v2": 9,
        },
    }
    path_page = data_track_module._render_event_master_tables({
        **payload,
        "windows": [path_window],
        "runtime_windows": [],
    })
    assert "窗口内 runtime route 活跃用户" not in path_page
    assert "自有服务器 9 人" not in path_page
    assert "APIKey-V1 9 人" not in path_page
    assert "APIKey-V2 9 人" not in path_page


def test_model_call_probe_coverage_mutation_changes_cell_shape(monkeypatch):
    action = {
        "key": "model_call", "label": "正常聊天 · 单次模型调用",
        "desc": "probe mutation",
        "runtime_probe": {"runtime_v1": "yellow", "runtime_v2": "red"},
    }
    monkeypatch.setattr(data_track_module, "_EVENT_MASTER_ACTIONS", (action,))
    red_payload = data_track_module._event_path_master_payload(_master_frozen())
    red_html = data_track_module._render_event_master_tables(red_payload)

    action["runtime_probe"]["runtime_v2"] = "yellow"
    yellow_payload = data_track_module._event_path_master_payload(_master_frozen())
    yellow_html = data_track_module._render_event_master_tables(yellow_payload)

    assert "🔴" in red_html
    assert "🟡" in yellow_html
    assert "有 V2 调用点" not in red_html
    assert "有 V2 调用点" in yellow_html
    assert red_html != yellow_html


def test_metric_cell_states_action_path_window_and_denominator():
    payload = data_track_module._event_path_master_payload(_master_frozen())
    raw_chat = _master_frozen()["windows"][0]["routes"]["model_api"]["lanes"]["chat"]
    assert raw_chat["failed"] == 4
    assert raw_chat["failure_codes"]["extraction_failed:quota_insufficient"] == 1
    chat = _master_row(payload, "chat_job")
    assert chat["cells"]["apikey_v2"]["failure"] == 3
    assert chat["cells"]["apikey_v2"]["user_unavailable"] == 1
    page = data_track_module._render_event_master_tables(payload)
    assert "动作=正常聊天 · 整个回复任务" in page
    assert "路径=apikey_v2" in page
    assert "窗口=最近 1 个已关闭北京日（2030-06-07 至 2030-06-07）" in page
    assert "分母=9（成功 6 + 失败 3" in page
    assert "用户侧不可用 1（剔除）" in page
    assert "superseded 2（剔除）" in page


def test_v2_operational_failure_rate_excludes_control_but_keeps_unknown():
    frozen = _master_frozen()
    chat = frozen["windows"][0]["routes"]["model_api"]["lanes"]["chat"]
    chat.update({
        "completed": 5, "failed": 3, "expired": 0, "superseded": 0,
        "failure_codes": {
            "runtime_mode_changed": 1,
            "extraction_failed:quota_insufficient": 1,
            "future_unknown_code": 1,
        },
    })
    payload = data_track_module._event_path_master_payload(frozen)
    cell = _master_row(payload, "chat_job")["cells"]["apikey_v2"]
    assert cell["raw_non_success"] == 3
    assert cell["control_outcomes"] == 1
    assert cell["user_unavailable"] == 1
    assert cell["failure"] == 1
    assert cell["denominator"] == 6


def test_history_import_overall_quality_labels_are_load_bearing():
    page = data_track_module._render_history_import_overall({
        "calculated_at": "2030-06-08T00:00:00+00:00",
        "coverage": "red",
        "reason": "无路径快照、未物理冻结；T247 补",
        "windows": [{
            "key": "rolling_1d", "label": "过去 1 个滚动日",
            "completed": 3, "failed": 1, "denominator": 4,
        }],
    })
    assert "全路径合计（滚动窗口 · 即时重算 · 未冻结）" in page
    assert "🔴 覆盖" in page
    assert "无路径快照、未物理冻结；T247 补" in page
    assert "计算时刻（北京）" in page
    assert "75.0% 成功 · 25.0% 失败" in page
    assert "分母=4 个全部路径 terminal job" in page


def test_product_na_and_observability_gap_use_different_words():
    payload = data_track_module._event_path_master_payload(_master_frozen())
    history = _master_row(payload, "onboarding_history")
    identity = _master_row(payload, "onboarding_identity")
    assert history["cells"]["apikey_v2"]["message"] == "N/A（产品当前不执行）"
    assert history["cells"]["apikey_v2"]["coverage"] == "black"
    assert identity["cells"]["apikey_v2"]["message"] == "当前记不到这一级"
    assert identity["cells"]["apikey_v2"]["coverage"] == "red"


def test_rollup_read_timeout_is_not_rendered_as_zero_or_missing_probe(monkeypatch):
    class ReadTimeout(Exception):
        sqlstate = "57014"

    class TimeoutPool:
        def connection(self, **_kwargs):
            raise ReadTimeout("canceling statement due to statement timeout")

    monkeypatch.setattr(db, "get_pool", lambda: TimeoutPool())
    frozen = db.admin_event_path_rollup_windows(through_day="2030-06-07")
    assert frozen["read_status"] == {
        "level": "timeout",
        "message": "取数超时（记了，但这里读不出来）",
    }
    payload = data_track_module._event_path_master_payload(frozen)
    chat = _master_row(payload, "chat_job")
    cell = chat["cells"]["apikey_v2"]
    assert cell["state"] == "timeout"
    page = data_track_module._render_event_master_tables(payload)
    assert "⏱️" in page
    assert "取数超时（记了，但这里读不出来）" in page
    assert "下一步是修读取路径，不是补埋点" in page


def test_history_import_read_timeout_has_its_own_marker():
    page = data_track_module._render_history_import_overall({
        "calculated_at": "2030-06-08T00:00:00+00:00",
        "coverage": "timeout",
        "reason": "取数超时（记了，但这里读不出来）；无路径快照、未物理冻结；T247 补",
        "windows": [],
    })
    assert "⏱️ 覆盖" in page
    assert "取数超时（记了，但这里读不出来）" in page
    assert "🔴 覆盖" not in page
