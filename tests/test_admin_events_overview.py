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
