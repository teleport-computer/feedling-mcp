"""Proactive 日报 lane 口径（2026-07-06 修复的回归测试）。

背景：日报「成功率 3%」的真因是 memory-maintenance（capture/dream/migrate）
重试风暴灌满 failed，而这些 job 永远不产生 delivered；同时 gate 拒绝的
skipped（用户关 ambient）被算成失败、「心跳」列的分类器还在匹配早已不存在的
heartbeat* kind（现网 self-initiated tick 的 kind 是 presence）。

口径修复后的契约：
- 成功率只看 wake lane：delivered / (delivered + failed)，failed 只含
  status='failed'（skipped 单独计数，不进分母）。
- maintenance jobs 单独成列（maintenance / maintenance_failed），不进 wake 统计。
- kind='presence' 计入「心跳」列（兼容历史 heartbeat* kind）。
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

_DATA_DIR = tempfile.mkdtemp(prefix="feedling-proactive-daily-test-")
os.environ.setdefault("FEEDLING_DATA_DIR", _DATA_DIR)
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

appmod = importlib.import_module("app")  # noqa: F401  (装配 DB schema)
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

    assert row["jobs"] == 8
    assert row["delivered"] == 2            # posted + delivered，仅 wake
    assert row["failed"] == 1               # 仅 wake status='failed'
    assert row["skipped"] == 1              # gate 拒绝单独计数
    assert row["pending"] == 1
    assert row["maintenance"] == 3
    assert row["maintenance_failed"] == 2   # migrate completed 不算失败
    assert row["heartbeat"] == 4            # presence×4 计入心跳列
    assert row["screen"] == 1


def test_daily_payload_success_rate_is_wake_lane_only(monkeypatch):
    fake_rows = [{
        "day": "2026-07-05",
        "jobs": 4541,
        "delivered": 109,
        "failed": 109,          # wake 真失败
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
    # 成功率 = wake delivered/(delivered+failed)，maintenance 不进分母
    assert abs(summary["overall_success_rate"] - 0.5) < 1e-9
    assert summary["total_maintenance"] == 3630
    assert summary["total_maintenance_failed"] == 3349
    assert abs(payload["rows"][0]["success_rate"] - 0.5) < 1e-9


def test_classify_proactive_kind_presence_is_heartbeat():
    assert data_track._classify_proactive_kind("presence") == "heartbeat"
    # 历史 kind 仍然归 heartbeat lane
    assert data_track._classify_proactive_kind("heartbeat_broadcast_off") == "heartbeat"


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
            "total_failed": 2,
            "total_maintenance": 5,
            "total_maintenance_failed": 4,
            "overall_success_rate": 0.5,
        },
        "filters": {"since": "", "days": 30, "view": "proactive"},
        "rows": [{
            "day": "2026-07-05", "jobs": 10, "delivered": 2, "failed": 2,
            "skipped": 1, "pending": 1, "maintenance": 5, "maintenance_failed": 4,
            "heartbeat": 3, "screen": 1, "success_rate": 0.5, "fail_rate": 0.5,
        }],
        "definition": {},
    }
    html_page = data_track._render_proactive_daily_page(payload)
    assert "维护" in html_page
    assert "5(f4)" in html_page  # maintenance(f maintenance_failed) 风格与用户页一致
