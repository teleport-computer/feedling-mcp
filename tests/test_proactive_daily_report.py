"""Proactive 日报 lane 口径（2026-07-06 修复的回归测试）。

背景：日报「成功率 3%」的真因是 memory-maintenance（capture/dream/migrate）
重试风暴灌满 failed，而这些 job 永远不产生 delivered；同时 gate 拒绝的
skipped（用户关 ambient）被算成失败、「心跳」列的分类器还在匹配早已不存在的
heartbeat* kind（现网 self-initiated tick 的 kind 是 presence）。

口径修复后的契约：
- 成功率只看 wake lane：(delivered + completed) / (delivered + completed + failed)。
  completed（醒了、正常决策、只是没发消息——sleep/纯动作）算成功：口径衡量
  「系统是否健康」，不是「醒了的里面有多少真正送达」。failed 只含
  status='failed'（skipped 单独计数，不进分母）。
- maintenance jobs 单独成列（maintenance / maintenance_failed），不进 wake 统计。
- kind='presence' 计入「心跳」列（兼容历史 heartbeat* kind）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_DATA_DIR = tempfile.mkdtemp(prefix="feedling-proactive-daily-test-")
os.environ.setdefault("FEEDLING_DATA_DIR", _DATA_DIR)
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from admin import data_track  # noqa: E402

from conftest import seed_user  # noqa: E402

# 放到一个远离其它测试写入的「专属日」，避免同日 GROUP BY 串数据。
_DAY_EPOCH = 978307200.0  # 2001-01-01T00:00:00Z → 北京日 2001-01-01


def _job(user_id: str, *, status: str, trigger: str = "", job_kind: str = "",
         offset: float = 0.0) -> None:
    doc: dict = {"status": status}
    if trigger:
        doc["trigger"] = trigger
    if job_kind:
        doc["job_kind"] = job_kind
    db.log_append(user_id, "proactive_jobs", doc, ts=_DAY_EPOCH + 3600.0 + offset)


def test_daily_report_splits_maintenance_lane():
    uid = "usr_daily_report_lane"
    seed_user(uid)
    # wake lane
    _job(uid, status="posted", trigger="presence", offset=1)
    _job(uid, status="failed", trigger="presence", offset=2)
    _job(uid, status="skipped", trigger="presence", offset=3)  # gate 关闭 ≠ 失败
    _job(uid, status="pending", trigger="presence", offset=4)
    _job(uid, status="completed", trigger="presence", offset=9)  # sleep/纯动作=成功
    _job(uid, status="delivered", trigger="screen_watch", offset=5)
    # maintenance lane — 不得污染 wake 统计
    _job(uid, status="failed", job_kind="memory_capture", offset=6)
    _job(uid, status="failed", job_kind="memory_dream", offset=7)
    _job(uid, status="completed", job_kind="memory_migrate", offset=8)

    rows = db.admin_data_track_proactive_daily(
        since_epoch=_DAY_EPOCH, days=366, tz="Asia/Shanghai",
    )
    by_day = {r["day"]: r for r in rows}
    row = by_day.get("2001-01-01")
    assert row is not None, f"expected 2001-01-01 row, got days={list(by_day)}"

    assert row["jobs"] == 9
    assert row["delivered"] == 2            # posted + delivered，仅 wake
    assert row["completed"] == 1            # sleep/纯动作，算成功
    assert row["failed"] == 1               # 仅 wake status='failed'
    assert row["skipped"] == 1              # gate 拒绝单独计数
    assert row["pending"] == 1
    assert row["maintenance"] == 3
    assert row["maintenance_failed"] == 2   # migrate completed 不算失败
    assert row["heartbeat"] == 5            # presence×5 计入心跳列
    assert row["screen"] == 1


def test_daily_payload_success_rate_is_wake_lane_only(monkeypatch):
    fake_rows = [{
        "day": "2026-07-05",
        "jobs": 4541,
        "delivered": 50,
        "completed": 50,        # 醒了但没说话，算成功
        "failed": 100,          # wake 真失败
        "skipped": 40,
        "pending": 80,
        "maintenance": 3630,
        "maintenance_failed": 3349,
        "heartbeat": 300,
        "screen": 4,
    }]
    monkeypatch.setattr(
        db, "admin_data_track_proactive_daily", lambda **kw: fake_rows,
    )
    monkeypatch.setattr(
        data_track, "_data_track_request_filters",
        lambda: {"since": "", "since_epoch": 0.0, "days": 30},
    )
    payload = data_track._data_track_proactive_daily_payload()
    summary = payload["summary"]
    # 成功率 = (delivered+completed)/(delivered+completed+failed)，maintenance 不进分母
    assert abs(summary["overall_success_rate"] - 0.5) < 1e-9
    assert summary["total_completed"] == 50
    assert summary["total_maintenance"] == 3630
    assert summary["total_maintenance_failed"] == 3349
    assert abs(payload["rows"][0]["success_rate"] - 0.5) < 1e-9


def test_classify_proactive_kind_presence_is_heartbeat():
    assert data_track._classify_proactive_kind("presence") == "heartbeat"
    # 历史 kind 仍然归 heartbeat lane
    assert data_track._classify_proactive_kind("heartbeat_broadcast_off") == "heartbeat"


# ---------------------------------------------------------------------------
# 2026-07-24 心跳治理④:全量 kind 分桶 + heartbeat_throttled 列 + 超速哨兵。
# 背景:07-22 心跳暴增(中位 68/天 vs 默认物理上限 12)排查花了半天,因为
# 日报只有心跳/屏幕两列、人均超速无人看见。这组测试钉住新的观测契约。
# ---------------------------------------------------------------------------

_DAY2_EPOCH = 981072000.0  # 2001-02-02T00:00:00Z → 北京日 2001-02-02(专属日)


def _job2(user_id: str, *, status: str, trigger: str = "", job_kind: str = "",
          status_reason: str = "", offset: float = 0.0) -> None:
    doc: dict = {"status": status}
    if trigger:
        doc["trigger"] = trigger
    if job_kind:
        doc["job_kind"] = job_kind
    if status_reason:
        doc["status_reason"] = status_reason
    db.log_append(user_id, "proactive_jobs", doc, ts=_DAY2_EPOCH + 3600.0 + offset)


def test_daily_report_counts_heartbeat_throttled():
    uid = "usr_daily_throttled"
    seed_user(uid)
    _job2(uid, status="posted", trigger="presence", offset=1)
    # ①服务端闸拦下的 tick:status=skipped + status_reason=heartbeat_throttled
    _job2(uid, status="skipped", trigger="presence",
          status_reason="heartbeat_throttled", offset=2)
    _job2(uid, status="skipped", trigger="presence",
          status_reason="heartbeat_throttled", offset=3)
    # 普通 gate skip(用户关 ambient)不算 throttled
    _job2(uid, status="skipped", trigger="presence", offset=4)

    rows = db.admin_data_track_proactive_daily(
        since_epoch=_DAY2_EPOCH, days=366, tz="Asia/Shanghai",
    )
    row = {r["day"]: r for r in rows}.get("2001-02-02")
    assert row is not None
    assert row["heartbeat_throttled"] == 2
    assert row["skipped"] == 3  # throttled 也是 skipped 的子集


def test_proactive_kinds_reports_every_kind_verbatim():
    uid = "usr_daily_kinds"
    seed_user(uid)
    _job2(uid, status="posted", trigger="presence", offset=10)
    _job2(uid, status="posted", trigger="presence", offset=11)
    _job2(uid, status="posted", trigger="unlock_after_absence", offset=12)
    _job2(uid, status="posted", job_kind="screen_watch", offset=13)
    _job2(uid, status="completed", job_kind="memory_capture", offset=14)

    kinds = db.admin_data_track_proactive_kinds(
        since_epoch=_DAY2_EPOCH, days=366, tz="Asia/Shanghai",
    )
    day = kinds.get("2001-02-02") or {}
    assert day.get("presence", 0) >= 2
    # 事件驱动源原样出现——不再混进大杂烩(2026-07 分类盲区教训)
    assert day.get("unlock_after_absence") == 1
    assert day.get("screen_watch", 0) >= 1
    assert day.get("memory_capture", 0) >= 1


def test_heartbeat_overspeed_sentinel_flags_only_over_cap():
    over_uid = "usr_overspeed_hot"
    ok_uid = "usr_overspeed_ok"
    seed_user(over_uid)
    seed_user(ok_uid)
    # over_uid:interval=900 → cap=96,+1 容差=97;塞 98 条 → 超速
    db.set_blob(over_uid, "proactive_settings", {"wake_interval_sec": 900})
    for i in range(98):
        _job2(over_uid, status="posted", trigger="presence", offset=100 + i)
    # ok_uid:默认 7200 → cap=12;塞 13 条(= cap+1 容差内)→ 不超速
    for i in range(13):
        _job2(ok_uid, status="posted", trigger="presence", offset=300 + i)

    overspeed = db.admin_proactive_heartbeat_overspeed(
        since_epoch=_DAY2_EPOCH, days=60, tz="Asia/Shanghai",
    )
    day = overspeed.get("2001-02-02") or []
    flagged = {e["user_id"]: e for e in day}
    assert over_uid in flagged
    assert flagged[over_uid]["heartbeats"] == 98
    assert flagged[over_uid]["interval_sec"] == 900
    assert flagged[over_uid]["cap"] == 96
    assert ok_uid not in flagged  # cap+1 容差:重启/日界首 tick 不误报


def test_daily_payload_merges_kinds_and_overspeed(monkeypatch):
    fake_rows = [{
        "day": "2026-07-22", "jobs": 100, "delivered": 10, "completed": 10,
        "failed": 5, "skipped": 20, "pending": 5, "maintenance": 30,
        "maintenance_failed": 10, "heartbeat": 40, "screen": 2,
        "heartbeat_throttled": 15,
    }]
    monkeypatch.setattr(
        db, "admin_data_track_proactive_daily", lambda **kw: fake_rows,
    )
    monkeypatch.setattr(
        db, "admin_data_track_proactive_kinds",
        lambda **kw: {"2026-07-22": {"presence": 40, "unlock_after_absence": 3}},
    )
    monkeypatch.setattr(
        db, "admin_proactive_heartbeat_overspeed",
        lambda **kw: {"2026-07-22": [
            {"user_id": "usr_x", "heartbeats": 467, "interval_sec": 1800, "cap": 48},
        ]},
    )
    monkeypatch.setattr(
        data_track, "_data_track_request_filters",
        lambda: {"since": "", "since_epoch": 0.0, "days": 30},
    )
    payload = data_track._data_track_proactive_daily_payload()
    row = payload["rows"][0]
    assert row["kinds"] == {"presence": 40, "unlock_after_absence": 3}
    assert row["overspeed_users"][0]["heartbeats"] == 467
    assert row["heartbeat_throttled"] == 15

    html_page = data_track._render_proactive_daily_page(payload)
    assert "限频拦截" in html_page
    assert "超速用户" in html_page
    assert "unlock_after_absence" in html_page   # kind 矩阵原样列出事件源
    assert "467/48上限" in html_page             # 最凶用户红标明细
    assert ">15<" in html_page                   # throttled 列出数


def test_render_daily_page_has_maintenance_column():
    payload = {
        "summary": {
            "generated_at": "2026-07-06T00:00:00",
            "timezone": "Asia/Shanghai",
            "days_returned": 1,
            "latest_day": "2026-07-05",
            "latest_success_rate": 0.5,
            "total_jobs": 10,
            "total_delivered": 2,
            "total_completed": 1,
            "total_failed": 2,
            "total_maintenance": 5,
            "total_maintenance_failed": 4,
            "overall_success_rate": 0.5,
        },
        "filters": {"since": "", "days": 30, "view": "proactive"},
        "rows": [{
            "day": "2026-07-05", "jobs": 10, "delivered": 2, "completed": 1,
            "failed": 2, "skipped": 1, "pending": 1, "maintenance": 5,
            "maintenance_failed": 4, "heartbeat": 3, "screen": 1,
            "success_rate": 0.5, "fail_rate": 0.5,
        }],
        "definition": {},
    }
    html_page = data_track._render_proactive_daily_page(payload)
    assert "维护" in html_page
    assert "5(f4)" in html_page  # maintenance(f maintenance_failed) 风格与用户页一致
