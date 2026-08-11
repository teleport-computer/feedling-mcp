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
    assert data_track._classify_proactive_kind("broadcast_opened") == "screen"
    assert data_track._classify_proactive_kind("broadcast_closed") == "screen"


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


def test_heartbeat_overspeed_ignores_throttled_ticks():
    """闸拦下的 tick 不算「放行心跳」:consumer 在闸内持续 tick 会积大量
    throttled skipped 行,若被数进哨兵,闸守得越好标得越红——语义正反
    (codex review ④ 抓出的口径 bug)。哨兵只对 admitted 心跳报警。"""
    uid = "usr_overspeed_throttled"
    seed_user(uid)  # 默认 interval 7200 → cap 12
    # 100 条被闸拦下的 tick + cap 内 5 条真放行 → 不应标红
    for i in range(100):
        _job2(uid, status="skipped", trigger="presence",
              status_reason="heartbeat_throttled", offset=500 + i)
    for i in range(5):
        _job2(uid, status="posted", trigger="presence", offset=700 + i)

    overspeed = db.admin_proactive_heartbeat_overspeed(
        since_epoch=_DAY2_EPOCH, days=60, tz="Asia/Shanghai",
    )
    flagged = {e["user_id"] for e in overspeed.get("2001-02-02") or []}
    assert uid not in flagged

    # 同一用户 admitted 超 cap(14 > 12+1)才标红——throttled 行仍然不计入计数
    for i in range(9):
        _job2(uid, status="posted", trigger="presence", offset=800 + i)
    overspeed = db.admin_proactive_heartbeat_overspeed(
        since_epoch=_DAY2_EPOCH, days=60, tz="Asia/Shanghai",
    )
    flagged = {e["user_id"]: e for e in overspeed.get("2001-02-02") or []}
    assert uid in flagged
    assert flagged[uid]["heartbeats"] == 14  # 5+9 admitted;100 条 throttled 不在内


# ---------------------------------------------------------------------------
# 2026-07-24 (b) Runtime V2 观测补盲:V2 心跳走 agent_jobs(lane='heartbeat'),
# 从不写 legacy proactive_jobs 流——日报加 V2心跳 列,超速哨兵 UNION 两源。
# ---------------------------------------------------------------------------

def _v2_hb(user_id: str, *, status: str = "completed", at_epoch: float) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at, finished_at) "
            "VALUES (%s, 'heartbeat', %s, to_timestamp(%s), to_timestamp(%s))",
            (user_id, status, at_epoch, at_epoch + 5),
        )


def test_v2_heartbeat_daily_counts_by_beijing_day():
    uid = "usr_v2_hb_daily"
    seed_user(uid)
    _v2_hb(uid, status="completed", at_epoch=_DAY2_EPOCH + 3600)
    _v2_hb(uid, status="completed", at_epoch=_DAY2_EPOCH + 3700)
    _v2_hb(uid, status="failed", at_epoch=_DAY2_EPOCH + 3800)
    _v2_hb(uid, status="expired", at_epoch=_DAY2_EPOCH + 3900)

    out = db.admin_v2_heartbeat_daily(
        since_epoch=_DAY2_EPOCH, days=366, tz="Asia/Shanghai",
    )
    day = out.get("2001-02-02") or {}
    assert day.get("jobs") == 4
    assert day.get("completed") == 2
    assert day.get("failed") == 1
    assert day.get("expired") == 1


def test_overspeed_sentinel_unions_v1_and_v2_sources():
    """dual 共存:用户在两个 runtime 各产生 cap 内的心跳,合计超 cap 才是
    真实频率——分开看各自合规、合起来超速,哨兵必须逮到(切 runtime 不脱管)。"""
    uid = "usr_v2_overspeed_mix"
    seed_user(uid)  # 默认 interval 7200 → cap 12,+1 容差=13
    # V1 侧 8 条(cap 内) + V2 侧 8 条(cap 内)= 合计 16 > 13 → 超速
    for i in range(8):
        _job2(uid, status="posted", trigger="presence", offset=900 + i)
    for i in range(8):
        _v2_hb(uid, status="completed", at_epoch=_DAY2_EPOCH + 5000 + i * 10)

    overspeed = db.admin_proactive_heartbeat_overspeed(
        since_epoch=_DAY2_EPOCH, days=60, tz="Asia/Shanghai",
    )
    flagged = {e["user_id"]: e for e in overspeed.get("2001-02-02") or []}
    assert uid in flagged
    assert flagged[uid]["heartbeats"] == 16


def test_daily_payload_carries_v2_heartbeat_column(monkeypatch):
    fake_rows = [{
        "day": "2026-07-24", "jobs": 50, "delivered": 5, "completed": 5,
        "failed": 2, "skipped": 3, "pending": 0, "maintenance": 10,
        "maintenance_failed": 1, "heartbeat": 30, "screen": 0,
        "heartbeat_throttled": 4,
    }]
    monkeypatch.setattr(db, "admin_data_track_proactive_daily", lambda **kw: fake_rows)
    monkeypatch.setattr(db, "admin_data_track_proactive_kinds", lambda **kw: {})
    monkeypatch.setattr(db, "admin_proactive_heartbeat_overspeed", lambda **kw: {})
    monkeypatch.setattr(
        db, "admin_v2_heartbeat_daily",
        lambda **kw: {
            "2026-07-24": {"jobs": 7, "completed": 6, "failed": 1, "expired": 0},
            # V2-only day (legacy 流全空的将来态) —— 不得被静默丢掉
            "2026-07-25": {"jobs": 3, "completed": 3, "failed": 0, "expired": 0},
        },
    )
    monkeypatch.setattr(
        data_track, "_data_track_request_filters",
        lambda: {"since": "", "since_epoch": 0.0, "days": 30},
    )
    payload = data_track._data_track_proactive_daily_payload()
    by_day = {r["day"]: r for r in payload["rows"]}
    assert by_day["2026-07-24"]["v2_heartbeat"] == 7
    assert by_day["2026-07-24"]["v2_heartbeat_failed"] == 1
    assert "2026-07-25" in by_day, "V2-only day must still produce a row"
    assert by_day["2026-07-25"]["v2_heartbeat"] == 3
    # 行序:天倒序,V2-only 天排最前
    assert payload["rows"][0]["day"] == "2026-07-25"
    # V2-only 最新天不得把顶部 metric 拉成假 0%
    assert payload["summary"]["latest_has_legacy"] is False
    assert payload["summary"]["total_v2_heartbeat"] == 10

    html_page = data_track._render_proactive_daily_page(payload)
    assert "V2心跳" in html_page
    # codex review (b) P1:V2-only 健康天曾被渲染成红 0% 假告警——
    # 现在成功率格显 —,顶部"最近一天成功率"显 N/A,而不是 0%。
    assert ">N/A<" in html_page
    first_row = html_page.split("<tbody>")[1].split("</tr>")[0]
    assert "0%" not in first_row, "V2-only day must not render a red 0% success"
    assert "—" in first_row


def test_agent_jobs_heartbeat_history_index_exists():
    """0056 迁移:agent_jobs 心跳历史 partial index——没有它,admin 页每次
    加载对 append-only 的 agent_jobs 全表扫两遍(codex review (b) P1)。"""
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'agent_jobs' AND indexname = 'ix_agent_jobs_hb_history'"
        ).fetchone()
    assert row is not None, "ix_agent_jobs_hb_history missing (0056 migration)"
    assert "lane = 'heartbeat'" in row[0]


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
