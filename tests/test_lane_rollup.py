"""lane_daily_rollup — frozen per-user per-lane daily cells (migration 0091).

Covers the three load-bearing behaviors the design leans on:
1. Beijing-day bucketing by ``finished_at`` (a job at 15:59Z vs 16:01Z lands
   in different cells — the day boundary IS the freeze-safety argument);
2. heartbeat ``enqueue_source`` discrimination via ``reason IS NULL``
   (serve_worker's clock tick vs the perception path);
3. write-once idempotency + the coverage watermark that keeps "genuinely
   zero" distinguishable from "before recorded history".

DB-backed tests ride conftest's FEEDLING_TEST_PG harness (init_schema applies
0091). Endpoint tests monkeypatch ``db.admin_lane_rollup`` — the route's own
logic is param validation, so that is what gets exercised against the real
ASGI app.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from admin import lane_rollup_scheduler as sched  # noqa: E402
from admin import routes_asgi as admin_asgi  # noqa: E402
import asgi.lifespan as lifespan_mod  # noqa: E402
from asgi import middleware  # noqa: E402
from core import leader as core_leader  # noqa: E402
from fastapi import FastAPI  # noqa: E402


ADMIN_TOKEN = "admin-test-token"

# 2030-06-04 12:00 Beijing as "now": days 01-03 are closed, far from the real
# clock so the live today_partial query can never accidentally see these rows.
_NOW_EPOCH = datetime(2030, 6, 4, 4, 0, tzinfo=timezone.utc).timestamp()


def _insert_job(user_id: str, lane: str, status: str, *, finished: datetime,
                reason: str | None = None, last_error: str | None = None,
                wake_result: str | None = None) -> int:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, reason, last_error,"
            " wake_result, created_at, finished_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, lane, status, reason, last_error, wake_result,
             finished, finished),
        ).fetchone()[0]


def _deliver(job_id: int, user_id: str, *, effect_type: str = "reply_final_fenced_v1",
             status: str = "applied") -> None:
    """Record that this attempt actually produced a user-visible bubble.

    The speak metric anchors on OUTPUT, so tests must create the output row
    rather than set a status column — writing only ``wake_result`` would test
    a claim, not a delivery.
    """
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_effect_outbox (effect_id, user_id, job_id,"
            " effect_type, expected_generation, payload, status)"
            " VALUES (%s, %s, %s, %s, 0, '{}'::jsonb, %s)",
            (f"eff-{job_id}-{effect_type}-{status}", user_id, job_id,
             effect_type, status),
        )


def _beijing_today():
    """被测代码整条按 Asia/Shanghai 算「今天」。测试里凡是要跟它比日期的地方
    都必须用同一时区——用 UTC 日会在每天北京 00:00-08:00 这 8 小时里随机翻红。"""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _cells(**filters) -> list[dict]:
    payload = db.admin_lane_rollup(**filters)
    return payload["rows"]


@pytest.fixture()
def clean_rollup():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM lane_daily_rollup")
        conn.execute("DELETE FROM lane_rollup_watermark")
    yield
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM lane_daily_rollup")
        conn.execute("DELETE FROM lane_rollup_watermark")


def test_freeze_buckets_by_beijing_day_boundary(clean_rollup):
    uid = "usr_rollup_bucket"
    seed_user(uid)
    # Beijing 2030-06-01 = [2030-05-31T16:00Z, 2030-06-01T16:00Z)
    _insert_job(uid, "chat", "completed",
                finished=datetime(2030, 6, 1, 15, 59, tzinfo=timezone.utc))
    _insert_job(uid, "chat", "completed",
                finished=datetime(2030, 6, 1, 16, 1, tzinfo=timezone.utc))
    frozen = db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    assert "2030-06-01" in frozen and "2030-06-02" in frozen
    by_day = {r["day"]: r for r in _cells(user_id=uid)}
    assert by_day["2030-06-01"]["completed"] == 1
    assert by_day["2030-06-02"]["completed"] == 1
    assert all(r["route"] == "model_api" and r["frozen"] for r in by_day.values())


def test_freeze_discriminates_heartbeat_enqueue_source(clean_rollup):
    uid = "usr_rollup_src"
    seed_user(uid)
    fin = datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc)
    _insert_job(uid, "heartbeat", "completed", finished=fin, reason=None)
    _insert_job(uid, "heartbeat", "failed", finished=fin,
                reason="perception_event", last_error="wake_failed:providererror")
    _insert_job(uid, "chat", "completed", finished=fin, reason="user_message")
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    rows = _cells(user_id=uid)
    by_key = {(r["lane"], r["enqueue_source"]): r for r in rows}
    assert by_key[("heartbeat", "clock")]["completed"] == 1
    assert by_key[("heartbeat", "perception")]["failed"] == 1
    assert by_key[("heartbeat", "perception")]["failure_codes"] == {
        "wake_failed:providererror": 1
    }
    # Non-heartbeat lanes must NOT leak the discriminator even when reason set.
    assert by_key[("chat", "")]["completed"] == 1
    assert ("chat", "user_message") not in by_key


def test_freeze_is_idempotent_and_advances_watermark(clean_rollup):
    uid = "usr_rollup_idem"
    seed_user(uid)
    # Activity on day 01 and 03 only — day 02 is a genuinely-zero day that
    # must still advance the watermark (else it would re-freeze forever).
    _insert_job(uid, "dream", "failed",
                finished=datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc),
                last_error="extraction_failed:upstream_unavailable")
    _insert_job(uid, "dream", "completed",
                finished=datetime(2030, 6, 3, 2, 0, tzinfo=timezone.utc))
    first = db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    assert first == ["2030-06-01", "2030-06-02", "2030-06-03"]
    again = db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    assert again == []  # watermark caught up; nothing re-frozen
    rows = _cells(user_id=uid)
    assert len(rows) == 2  # no duplicate cells from any re-run
    payload = db.admin_lane_rollup(user_id=uid)
    cov = payload["coverage"]["model_api"]
    assert cov["backfill_from"] == "2030-06-01"
    assert cov["through_day"] == "2030-06-03"
    assert cov["partial_before"] == "2030-06-01"


def test_freeze_day_rerun_writes_nothing(clean_rollup):
    from zoneinfo import ZoneInfo
    from datetime import date
    uid = "usr_rollup_rerun"
    seed_user(uid)
    _insert_job(uid, "capture", "failed",
                finished=datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc),
                last_error="extraction_failed:output_truncated")
    zone = ZoneInfo("Asia/Shanghai")
    with db.get_pool().connection() as conn:
        assert db._lane_rollup_freeze_day(conn, day=date(2030, 6, 1), zone=zone) == 1
        # Crash-retry path: same day again is pure DO NOTHING.
        assert db._lane_rollup_freeze_day(conn, day=date(2030, 6, 1), zone=zone) == 0


def test_failure_codes_collapse_free_text(clean_rollup):
    uid = "usr_rollup_sanitize"
    seed_user(uid)
    fin = datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc)
    _insert_job(uid, "chat", "failed", finished=fin,
                last_error="Traceback (most recent call last): boom")
    _insert_job(uid, "chat", "failed", finished=fin,
                last_error="turn_failed:empty_reply")
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    (row,) = _cells(user_id=uid)
    assert row["failure_codes"] == {
        "runtime_failed": 1,
        "turn_failed:empty_reply": 1,
    }
    # The sanitizer is the only thing standing between free text (potentially
    # user content) and a frozen forever-row — assert the regex is actually
    # the module's, not a lookalike.
    assert db._LANE_ROLLUP_CODE_RE.match("turn_failed:empty_reply")
    assert not db._LANE_ROLLUP_CODE_RE.match("Traceback (most recent call last)")


def test_admin_lane_rollup_filters_and_pagination(clean_rollup):
    u1, u2 = "usr_rollup_f1", "usr_rollup_f2"
    seed_user(u1)
    seed_user(u2)
    fin = datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc)
    _insert_job(u1, "heartbeat", "completed", finished=fin)
    _insert_job(u2, "dream", "failed", finished=fin,
                last_error="extraction_failed:no_json_object")
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    only_u1 = db.admin_lane_rollup(user_id=u1)
    assert {r["user_id"] for r in only_u1["rows"]} == {u1}
    only_dream = db.admin_lane_rollup(lane="dream")
    assert {r["lane"] for r in only_dream["rows"]} == {"dream"}
    paged = db.admin_lane_rollup(limit=1, offset=0)
    assert paged["pagination"]["returned"] == 1
    assert paged["pagination"]["total"] == 2
    windowed = db.admin_lane_rollup(since_day="2030-06-02")
    assert windowed["rows"] == []


def test_admin_lane_rollup_today_is_live_and_flagged(clean_rollup):
    uid = "usr_rollup_today"
    seed_user(uid)
    _insert_job(uid, "heartbeat", "failed",
                finished=datetime.now(timezone.utc),
                last_error="wake_failed:providererror")
    payload = db.admin_lane_rollup(user_id=uid)
    assert payload["rows"] == []  # today is not frozen
    (live,) = payload["today_partial"]
    assert live["frozen"] is False
    assert live["failed"] == 1
    assert live["lane"] == "heartbeat"


def test_freeze_mirrors_each_day_as_one_atomic_group(clean_rollup, monkeypatch):
    """MIRROR lane (not SNAPSHOT — permanent growth hits snapshot's 200k
    MAX_ROWS hard stop): every frozen day must reach the TEE shadow as one
    execute_many group of the verbatim main-path statements, watermark last.
    A torn/omitted group would make the TEE watermark claim days whose cells
    never arrived — after RDS shutdown that reads as "genuinely zero"."""
    from tee_shadow import mirror as tee_mirror
    groups: list[list] = []
    monkeypatch.setattr(tee_mirror, "execute_many",
                        lambda stmts: groups.append(list(stmts)))
    uid = "usr_rollup_mirror"
    seed_user(uid)
    _insert_job(uid, "heartbeat", "completed",
                finished=datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc))
    _insert_job(uid, "dream", "failed",
                finished=datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc),
                last_error="extraction_failed:no_json_object")
    frozen = db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    # 3 days frozen (01..03), one mirror group per day — including the two
    # zero-activity days, whose watermark advance must still reach the TEE.
    assert frozen == ["2030-06-01", "2030-06-02", "2030-06-03"]
    assert len(groups) == 3
    day1, day2, day3 = groups
    assert len(day1) == 3  # 2 cells + watermark
    assert len(day2) == len(day3) == 1  # watermark only
    for g in groups:
        assert "lane_rollup_watermark" in g[-1][0], "水位必须是组内最后一条"
    cell_sqls = [sql for sql, _ in day1[:-1]]
    assert all("INSERT INTO lane_daily_rollup" in s for s in cell_sqls)
    # Verbatim fidelity: the mirrored cell params are exactly the rows the
    # main path wrote (user, day, lane, counts) — not a lookalike rebuild.
    # Param layout starts (day, lane, src, completed, failed, ...) and ENDS
    # with uid, which feeds the users FOR KEY SHARE source select. uid is read
    # as p[-1], never a fixed index: adding a counted column shifts every
    # absolute position, and a hardcoded one turns a schema addition into a
    # mystery failure here instead of a real signal.
    mirrored = {(p[-1], p[0], p[1], p[3], p[4]) for _, p in day1[:-1]}
    stored = {(r["user_id"], r["day"], r["lane"], r["completed"], r["failed"])
              for r in _cells(user_id=uid)}
    assert mirrored == stored


def test_authoritative_delete_user_anonymizes_without_the_belt(
        clean_rollup, monkeypatch):
    """Seven 2026-08-18: deletion anonymize-merges, neither cascades nor keeps
    ids. The load-bearing site is ``db.delete_user``'s own transaction —
    codex2's live repro showed the belt-only wiring left orphan cells after
    the authoritative delete returned True. delete_user_data is explicitly
    forbidden here so the belt cannot carry this test."""
    def _belt_forbidden(user_id):
        raise AssertionError("delete_user_data 不许承重 — 权威路径必须自己匿名化")
    monkeypatch.setattr(db, "delete_user_data", _belt_forbidden)
    u1, u2 = "usr_rollup_anon1", "usr_rollup_anon2"
    seed_user(u1)
    seed_user(u2)
    fin = datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc)
    _insert_job(u1, "heartbeat", "failed", finished=fin,
                last_error="wake_failed:providererror")
    _insert_job(u1, "heartbeat", "failed", finished=fin,
                last_error="wake_failed:providererror")
    _insert_job(u2, "heartbeat", "failed", finished=fin,
                last_error="wake_failed:providererror")
    _insert_job(u2, "heartbeat", "failed", finished=fin,
                last_error="wake_failed:prompt_frontier_exhausted")
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)

    assert db.delete_user(u1) is True
    assert _cells(user_id=u1) == []
    (anon,) = _cells(user_id=db._LANE_ROLLUP_DELETED_USER)
    assert anon["failed"] == 2
    assert anon["failure_codes"] == {"wake_failed:providererror": 2}

    assert db.delete_user(u2) is True
    assert _cells(user_id=u2) == []
    (anon,) = _cells(user_id=db._LANE_ROLLUP_DELETED_USER)
    # Additive merge across two deletions: counts sum, codes sum per key.
    assert anon["failed"] == 4
    assert anon["failure_codes"] == {
        "wake_failed:providererror": 3,
        "wake_failed:prompt_frontier_exhausted": 1,
    }


def test_bulk_registry_removal_anonymizes(clean_rollup):
    """save_all_users' snapshot-removal is an account deletion just as much
    as delete_user — the batch path must anonymize too (codex2 #①)."""
    uid = "usr_rollup_bulk"
    seed_user(uid)
    _insert_job(uid, "dream", "failed",
                finished=datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc),
                last_error="extraction_failed:no_json_object")
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    with db.get_pool().connection() as conn:
        snapshot = [r[0] for r in conn.execute(
            "SELECT doc FROM users WHERE user_id != %s", (uid,),
        ).fetchall()]
    db.save_all_users(snapshot)
    assert _cells(user_id=uid) == []
    (anon,) = _cells(user_id=db._LANE_ROLLUP_DELETED_USER)
    assert anon["failure_codes"] == {"extraction_failed:no_json_object": 1}


def test_concurrent_freeze_cannot_resurrect_deleted_user(clean_rollup):
    """codex2 #③, two-connection proof: after delete_user RETURNS, the
    original id can never reappear. The freeze holds FOR KEY SHARE on the
    users row, so the deletion's users DELETE must wait for the freeze to
    commit — and its anonymize-merge then sweeps the freshly frozen cells.
    Removing the FOR KEY SHARE guard makes the delete slip past the open
    freeze and turns the final assertion red (orphan cells reappear)."""
    import threading
    from zoneinfo import ZoneInfo
    from datetime import date
    uid = "usr_rollup_race"
    seed_user(uid)
    _insert_job(uid, "heartbeat", "failed",
                finished=datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc),
                last_error="wake_failed:providererror")
    zone = ZoneInfo("Asia/Shanghai")
    lock_held = threading.Event()
    release = threading.Event()

    def freezer():
        with db.get_pool().connection() as conn:
            with conn.transaction():
                db._lane_rollup_freeze_day(conn, day=date(2030, 6, 1), zone=zone)
                lock_held.set()
                release.wait(timeout=15)  # hold the key-share lock open

    outcome: dict = {}

    def deleter():
        outcome["deleted"] = db.delete_user(uid)

    f = threading.Thread(target=freezer)
    f.start()
    assert lock_held.wait(timeout=15)
    d = threading.Thread(target=deleter)
    d.start()
    d.join(timeout=0.8)
    try:
        assert d.is_alive(), (
            "delete_user 没被在途 freeze 的 FOR KEY SHARE 挡住 — 守卫失效,"
            "删号可能在 freeze 提交前完成并留下孤儿格子")
    finally:
        release.set()
        f.join(timeout=15)
        d.join(timeout=15)
    assert outcome.get("deleted") is True
    assert _cells(user_id=uid) == []  # 删号返回后原 id 永不再现
    (anon,) = _cells(user_id=db._LANE_ROLLUP_DELETED_USER)
    assert anon["failed"] == 1  # freeze 落的格子被删号侧归并,没有丢


def test_freeze_after_deletion_inserts_nothing(clean_rollup):
    uid = "usr_rollup_postdel"
    seed_user(uid)
    _insert_job(uid, "heartbeat", "completed",
                finished=datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc))
    assert db.delete_user(uid) is True
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    assert _cells(user_id=uid) == []


def test_account_deletion_mirrors_the_anonymize_statements(clean_rollup, monkeypatch):
    """The TEE mirror group must be sourced from the SAME authoritative
    operation (delete_user), not from the belt."""
    from tee_shadow import mirror as tee_mirror
    groups: list[list] = []
    monkeypatch.setattr(tee_mirror, "execute_many",
                        lambda stmts: groups.append(list(stmts)))
    uid = "usr_rollup_anonm"
    seed_user(uid)
    _insert_job(uid, "dream", "failed",
                finished=datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc),
                last_error="extraction_failed:no_json_object")
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    groups.clear()
    assert db.delete_user(uid) is True
    (group,) = groups
    assert group[0][0].startswith("DELETE FROM users")
    tail = [sql for sql, _ in group[-2:]]
    assert "ON CONFLICT (user_id, day, route, lane, enqueue_source) DO UPDATE" in tail[0]
    assert tail[1].startswith("DELETE FROM lane_daily_rollup")
    # A user with no cells must NOT append the pair (mirror replay would be
    # wasted statements on every ordinary deletion).
    uid2 = "usr_rollup_never_frozen"
    seed_user(uid2)
    groups.clear()
    assert db.delete_user(uid2) is True
    (group2,) = groups
    assert all("lane_daily_rollup" not in sql for sql, _ in group2)


def test_delete_user_data_belt_stays_idempotent_backstop(clean_rollup):
    """The belt keeps its own anonymize (idempotent): running it after the
    authoritative path already merged must not double-count."""
    uid = "usr_rollup_belt"
    seed_user(uid)
    _insert_job(uid, "dream", "failed",
                finished=datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc),
                last_error="extraction_failed:no_json_object")
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    assert db.delete_user(uid) is True
    db.delete_user_data(uid)  # belt runs after — must be a no-op
    (anon,) = _cells(user_id=db._LANE_ROLLUP_DELETED_USER)
    assert anon["failure_codes"] == {"extraction_failed:no_json_object": 1}


@pytest.fixture()
def clean_tee_rollup():
    from tee_shadow import mirror as tee_mirror
    with tee_mirror.get_tee_pool().connection() as conn:
        conn.execute("DELETE FROM lane_daily_rollup")
        conn.execute("DELETE FROM lane_rollup_watermark")
    yield
    with tee_mirror.get_tee_pool().connection() as conn:
        conn.execute("DELETE FROM lane_daily_rollup")
        conn.execute("DELETE FROM lane_rollup_watermark")


def test_reconciler_heals_missed_mirror_for_cells_and_watermark(
        clean_rollup, clean_tee_rollup):
    """codex2's required fault injection: a REAL missed mirror (dual-write off
    in tests → TEE got nothing) must be healed by the reconciler for BOTH
    tables — cells without watermark would read as unrecorded history, a
    watermark without cells would claim days that never arrived."""
    from tee_shadow import mirror as tee_mirror
    from tee_shadow import reconciler
    uid = "usr_rollup_heal"
    seed_user(uid)
    _insert_job(uid, "heartbeat", "failed",
                finished=datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc),
                last_error="wake_failed:providererror")
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    with tee_mirror.get_tee_pool().connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM lane_daily_rollup").fetchone()[0] == 0  # 漏写成立
    rep_cells = reconciler.reconcile_table("lane_daily_rollup")
    rep_wm = reconciler.reconcile_table("lane_rollup_watermark")
    assert rep_cells.get("copied", 0) >= 1, rep_cells
    assert rep_wm.get("copied", 0) >= 1, rep_wm
    with tee_mirror.get_tee_pool().connection() as conn:
        cell = conn.execute(
            "SELECT user_id, day, lane, enqueue_source, failed, failure_codes "
            "FROM lane_daily_rollup WHERE user_id = %s", (uid,),
        ).fetchone()
        wm = conn.execute(
            "SELECT route, backfill_from, through_day FROM lane_rollup_watermark",
        ).fetchone()
    assert cell == (uid, "2030-06-01", "heartbeat", "clock", 1,
                    {"wake_failed:providererror": 1})
    assert wm == ("model_api", "2030-06-01", "2030-06-03")


# --- phase 2: resident / user_logs source ---------------------------------- #
#
# The V1 ring buffer (proactive_jobs keeps the newest 500 rows per user) is
# actively discarding heartbeat history, so these cells are the only durable
# record for resident users. Everything below therefore checks not just "a
# number appears" but "the number means the same thing the events page says".

# 2030-06-04 12:00 Beijing minus the 4h resident lag still leaves 06-01..06-03
# closed, so the same _NOW_EPOCH drives both freezers in these tests.

def _log_job(user_id: str, *, stream: str = "proactive_jobs",
             ts: datetime, status: str, kind: str | None = None,
             job_kind: str | None = None, terminal_at: datetime | None = None,
             status_reason: str | None = None) -> None:
    doc: dict = {"status": status, "created_at": ts.isoformat()}
    if kind:
        doc["wake_kind"] = kind
    if job_kind:
        doc["job_kind"] = job_kind
    if status_reason:
        doc["status_reason"] = status_reason
    if terminal_at is not None:
        key = "failed_at" if status in ("failed", "error", "skipped") else "completed_at"
        doc[key] = terminal_at.isoformat()
    db.log_append(user_id, stream, doc, ts=ts.timestamp())


def _seed_resident(user_id: str, route: str = "resident") -> None:
    seed_user(user_id)
    db.set_blob(user_id, "onboarding_route", {"route": route})


def test_resident_freeze_infers_lanes_like_the_events_page(clean_rollup):
    uid = "usr_rollup_res_lane"
    _seed_resident(uid)
    t = datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc)
    _log_job(uid, ts=t, status="delivered", kind="heartbeat_tick")
    _log_job(uid, ts=t, status="failed", kind="heartbeat_tick",
             status_reason="wake_failed:providererror")
    _log_job(uid, ts=t, status="posted", kind="perception_event")
    _log_job(uid, ts=t, status="completed", kind="screen_watch")
    _log_job(uid, ts=t, status="completed", job_kind="memory_dream")
    _log_job(uid, ts=t, status="failed", job_kind="memory_capture",
             status_reason="extraction_failed:no_json_object")
    db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    cells = {r["lane"]: r for r in _cells(user_id=uid)}
    assert cells["heartbeat"]["completed"] == 1
    assert cells["heartbeat"]["failed"] == 1
    assert cells["heartbeat"]["failure_codes"] == {"wake_failed:providererror": 1}
    assert cells["trigger"]["completed"] == 1
    assert cells["screen"]["completed"] == 1
    assert cells["dream"]["completed"] == 1
    assert cells["capture"]["failed"] == 1
    assert all(r["route"] == "resident" and r["enqueue_source"] == ""
               for r in _cells(user_id=uid))


def test_resident_freeze_excludes_hosted_users(clean_rollup):
    """hosted users' user_logs entries are a SECOND record of the same event
    already frozen from agent_jobs. ⚠️ Folding them in does NOT collide on the
    primary key — route is part of that key, so the insert succeeds and quietly
    mints a phantom route='resident' cell for a hosted user: double-counted
    across routes, and mislabelled. A silent extra row is worse than a raised
    key violation. Only route='resident' belongs to this source."""
    hosted = "usr_rollup_res_hosted"
    _seed_resident(hosted, route="model_api")
    _log_job(hosted, ts=datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc),
             status="delivered", kind="heartbeat_tick")
    db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    assert _cells(user_id=hosted) == []


def test_resident_freeze_buckets_by_terminal_time_and_diverges_from_events(
        clean_rollup):
    """A job created 23:58 that finishes 00:03 counts on its TERMINAL day.

    这与 events 页**有意分歧**：events 按创建时刻分桶（_day_filter('l.ts')），
    所以跨零点的 job 两边落在不同日子。两个口径回答两个问题——「那天发起了多少」
    vs「那天出了多少结果」——而 Seven 定的 A（失败率只统计有终态时刻的已终结尝试）
    要的是后者。本用例把这条分歧钉成显式契约，免得将来有人拿它当 bug「修」回去：
    改回创建时刻会让晚终结的 job 彻底蒸发（见 late-terminating 用例）。"""
    uid = "usr_rollup_res_cross"
    _seed_resident(uid)
    _log_job(uid,
             ts=datetime(2030, 6, 1, 15, 58, tzinfo=timezone.utc),   # 06-01 23:58 北京
             terminal_at=datetime(2030, 6, 1, 16, 3, tzinfo=timezone.utc),  # 06-02 00:03
             status="delivered", kind="heartbeat_tick")
    db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    (cell,) = _cells(user_id=uid)
    assert cell["day"] == "2030-06-02", "终态日才是结果发生的日子"
    board = db.admin_events_overview(day="2030-06-01", tz="Asia/Shanghai")
    (hb,) = [p for p in board["proactive"]
             if p["route"] == "resident" and p["lane"] == "heartbeat"]
    assert hb["total"] == 1, "events 仍按创建日算 —— 分歧是设计,不是漂移"


def test_resident_freeze_survives_malformed_doc_timestamps(clean_rollup):
    """'2026-99-99T…' passes a shape-only regex but explodes on ::timestamptz,
    which used to abort the whole day's aggregate (codex2 PG repro). Bucketing
    on the numeric l.ts column removes the parse entirely — a garbage doc
    timestamp must not cost the day."""
    uid = "usr_rollup_res_badts"
    _seed_resident(uid)
    ts = datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc)
    db.log_append(uid, "proactive_jobs", {
        "status": "delivered", "wake_kind": "heartbeat_tick",
        "created_at": ts.isoformat(), "completed_at": "2026-99-99T99:99:99Z",
    }, ts=ts.timestamp())
    frozen = db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    assert "2030-06-01" in frozen
    (cell,) = _cells(user_id=uid)
    assert cell["completed"] == 1


def test_resident_freeze_ignores_stale_terminal_fields(clean_rollup):
    """A job that ends 'failed' while still carrying a stale posted_at from the
    previous day must count as one failure on its own day — the old fixed
    COALESCE(completed_at, posted_at, failed_at) picked posted_at and filed the
    failure a day early, leaving the real day empty (codex2 PG repro)."""
    uid = "usr_rollup_res_stale"
    _seed_resident(uid)
    ts = datetime(2030, 6, 2, 3, 0, tzinfo=timezone.utc)
    db.log_append(uid, "proactive_jobs", {
        "status": "failed", "wake_kind": "heartbeat_tick",
        "created_at": ts.isoformat(),
        "posted_at": datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc).isoformat(),
        "failed_at": ts.isoformat(),
        "status_reason": "wake_failed:providererror",
    }, ts=ts.timestamp())
    db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    cells = {r["day"]: r for r in _cells(user_id=uid)}
    assert set(cells) == {"2030-06-02"}
    assert cells["2030-06-02"]["failed"] == 1


def test_failure_codes_never_exceed_the_failed_count(clean_rollup):
    """proactive status='error' is NOT in the proactive fail set, so it must
    not appear in failure_codes either — the two used different predicates and
    the reason distribution summed higher than its own numerator (codex2)."""
    uid = "usr_rollup_res_codes"
    uid2 = "usr_rollup_res_codes_mem"
    _seed_resident(uid)
    _seed_resident(uid2)
    t = datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc)
    _log_job(uid, ts=t, status="failed", kind="heartbeat_tick",
             status_reason="wake_failed:providererror")
    _log_job(uid, ts=t, status="error", kind="heartbeat_tick",
             status_reason="wake_failed:someothererror")
    # memory lanes DO count 'error' as a failure — same predicate both sides.
    _log_job(uid2, ts=t, status="error", job_kind="memory_capture",
             status_reason="extraction_failed:no_json_object")
    # 一次冻结覆盖两个用户:格子冻过的日子永不回访(冻结语义,4h lag 保证已关闭的
    # 日子不会再有新数据),所以先播完再冻,不能冻完再补播。
    db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    (cell,) = [r for r in _cells(user_id=uid) if r["lane"] == "heartbeat"]
    assert cell["failed"] == 1
    assert sum(cell["failure_codes"].values()) == cell["failed"]
    assert cell["failure_codes"] == {"wake_failed:providererror": 1}
    (mem,) = [r for r in _cells(user_id=uid2) if r["lane"] == "capture"]
    assert mem["failed"] == 1
    assert sum(mem["failure_codes"].values()) == mem["failed"]


def test_live_topup_without_any_watermark_still_shows_yesterday(clean_rollup):
    """First boot / total freezer stall: yesterday is neither frozen nor (with
    a hardcoded 'today' window) live — the endpoint reported it as no activity.
    With no watermark the live window must look back the bounded maximum and
    declare that bound (codex2 PG repro)."""
    uid = "usr_rollup_nowm"
    _seed_resident(uid)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    _insert_job(uid, "heartbeat", "failed", finished=yesterday,
                last_error="wake_failed:providererror")
    _log_job(uid, ts=yesterday, status="failed", kind="heartbeat_tick",
             status_reason="wake_failed:providererror")
    payload = db.admin_lane_rollup(user_id=uid)
    assert payload["rows"] == []          # 没有水位,自然没有冻结格子
    days = {r["day"] for r in payload["today_partial"]}
    assert (_beijing_today() - timedelta(days=1)).isoformat() in days, (
        "无水位时昨天被静默漏掉")
    for r in ("model_api", "resident"):
        assert payload["coverage"][r]["live_truncated_before"], (
            f"{r} 没声明回看边界 —— 读的人无法分辨『真没活动』与『在窗口之外』")


def test_resident_freeze_lags_four_hours(clean_rollup):
    """At 02:00 Beijing, the previous day must NOT be frozen yet (an in-flight
    job can still patch its doc into it); by 06:00 it is."""
    uid = "usr_rollup_res_lag"
    _seed_resident(uid)
    _log_job(uid, ts=datetime(2030, 6, 2, 3, 0, tzinfo=timezone.utc),
             status="delivered", kind="heartbeat_tick")
    at_0200 = datetime(2030, 6, 2, 18, 0, tzinfo=timezone.utc)   # 06-03 02:00 北京
    frozen = db.freeze_completed_resident_lane_days(
        now_epoch=at_0200.timestamp())
    assert "2030-06-02" not in frozen
    at_0600 = datetime(2030, 6, 2, 22, 0, tzinfo=timezone.utc)   # 06-03 06:00 北京
    frozen = db.freeze_completed_resident_lane_days(
        now_epoch=at_0600.timestamp())
    assert "2030-06-02" in frozen


def test_resident_and_model_api_coexist_under_distinct_routes(clean_rollup):
    """Same user, same day, same lane — the route column keeps the two sources
    from colliding on the primary key."""
    uid = "usr_rollup_both"
    _seed_resident(uid)
    fin = datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc)
    _insert_job(uid, "heartbeat", "completed", finished=fin)
    _log_job(uid, ts=fin, status="delivered", kind="heartbeat_tick")
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    routes = {r["route"] for r in _cells(user_id=uid)}
    assert routes == {"model_api", "resident"}
    assert len(db.admin_lane_rollup(user_id=uid, route="resident")["rows"]) == 1
    assert len(db.admin_lane_rollup(user_id=uid, route="model_api")["rows"]) == 1


def test_resident_watermark_is_independent_of_model_api(clean_rollup):
    uid = "usr_rollup_res_wm"
    _seed_resident(uid)
    _log_job(uid, ts=datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc),
             status="delivered", kind="heartbeat_tick")
    db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    cov = db.admin_lane_rollup()["coverage"]
    assert cov["resident"]["backfill_from"] == "2030-06-01"
    # 两条源各记各的水位:model_api 从未冻结过,所以没有 backfill_from/through_day,
    # 只有 live 回看窗口的边界声明(那是另一件事,不是水位)。
    assert "backfill_from" not in cov.get("model_api", {})
    assert "through_day" not in cov.get("model_api", {})


def test_resident_freeze_is_idempotent(clean_rollup):
    uid = "usr_rollup_res_idem"
    _seed_resident(uid)
    _log_job(uid, ts=datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc),
             status="delivered", kind="heartbeat_tick")
    assert db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    assert db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH) == []
    assert len(_cells(user_id=uid)) == 1


def test_resident_deleted_user_cells_are_anonymized_too(clean_rollup):
    uid = "usr_rollup_res_del"
    _seed_resident(uid)
    _log_job(uid, ts=datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc),
             status="failed", kind="heartbeat_tick",
             status_reason="wake_failed:providererror")
    db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    assert db.delete_user(uid) is True
    assert _cells(user_id=uid) == []
    (anon,) = _cells(user_id=db._LANE_ROLLUP_DELETED_USER)
    assert anon["route"] == "resident"
    assert anon["failure_codes"] == {"wake_failed:providererror": 1}


def test_live_topup_covers_the_unfrozen_tail_for_both_routes(clean_rollup):
    """The live window is derived from the WATERMARK, not from "today" — so
    resident's 4h lag tail (and any freezer stall) shows up as explicitly
    non-frozen rows instead of reading as no activity."""
    uid = "usr_rollup_tail"
    _seed_resident(uid)
    now = datetime.now(timezone.utc)
    _insert_job(uid, "heartbeat", "failed", finished=now,
                last_error="wake_failed:providererror")
    _log_job(uid, ts=now, status="failed", kind="heartbeat_tick",
             status_reason="wake_failed:providererror")
    payload = db.admin_lane_rollup(user_id=uid)
    live = {r["route"] for r in payload["today_partial"]}
    assert live == {"model_api", "resident"}
    assert all(r["frozen"] is False for r in payload["today_partial"])
    assert payload["rows"] == []


def test_live_topup_declares_what_it_refuses_to_scan(clean_rollup):
    """A long-stalled freezer must not turn one GET into an unbounded scan;
    the refused range is declared in coverage, never silently dropped."""
    from datetime import date as _date
    # 用北京日，不用 UTC 日：被测代码整条按 Asia/Shanghai 算「今天」，UTC 日只在
    # 北京 08:00 之后才与之相同，拿 UTC 日断言会在每天有 8 小时随机翻红。
    today_bj = _beijing_today()
    stale = (today_bj - timedelta(days=30)).isoformat()
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO lane_rollup_watermark (route, backfill_from, through_day)"
            " VALUES ('model_api', %s, %s)", (stale, stale))
    cov = db.admin_lane_rollup()["coverage"]["model_api"]
    cutoff = _date.fromisoformat(cov["live_truncated_before"])
    assert (today_bj - cutoff).days == db._LANE_ROLLUP_LIVE_MAX_DAYS - 1


def test_resident_rollup_agrees_with_the_events_page(clean_rollup):
    """Cross-check against the surface these numbers must not contradict.

    口径改成按终态时刻分桶之后，这条互核**仍然是本源的正确性主论据**，只是适用
    边界要说清：两边只在「当天创建、当天终结」的 job 上必然相等（跨零点的 job
    两边本就该落在不同日子，见 diverges_from_events 用例）。所以本用例刻意只造
    同日 job —— 它验的是成败词汇有没有抄错，那部分与分桶口径无关。
    ⚠️ 它只证明「两边一致」，不证明「两边都对」；词汇完整性另用 prod 实测的
    恒等式（success+failed+pending==total）验，见 _LANE_ROLLUP_V1_OK_PRED 注释。"""
    uid = "usr_rollup_res_xcheck"
    _seed_resident(uid)
    t = datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc)
    for status in ("delivered", "completed", "failed", "skipped"):
        _log_job(uid, ts=t, status=status, kind="heartbeat_tick")
    db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    (cell,) = [r for r in _cells(user_id=uid) if r["lane"] == "heartbeat"]
    board = db.admin_events_overview(day="2030-06-01", tz="Asia/Shanghai")
    (hb,) = [p for p in board["proactive"]
             if p["route"] == "resident" and p["lane"] == "heartbeat"]
    assert (cell["completed"], cell["failed"]) == (hb["success"], hb["failed"])


def test_late_terminating_job_lands_on_its_terminal_day_and_leaves_stuck(
        clean_rollup):
    """codex2 验签判据①：D-3 创建、当时 stuck、今天才 completed 的 resident job。
    按创建时刻分桶会让它**彻底蒸发** —— 创建日冻结时它还非终态所以不入格子，
    而冻结过的日子永不回访；等它终结后又不再算 stuck，两头都不计。
    按终态时刻分桶则落在今天（还开着的日子），stuck 同时减一。"""
    uid = "usr_rollup_late"
    _seed_resident(uid)
    created = datetime.now(timezone.utc) - timedelta(days=3)
    db.log_append(uid, "proactive_jobs", {
        "status": "pending", "wake_kind": "heartbeat_tick",
        "created_at": created.isoformat(),
    }, ts=created.timestamp())
    # 真的跑一次冻结，并让水位**越过创建日** —— 否则「今天算 completed」这条断言
    # 会靠「无水位时的 live 回看窗口」偶然成立，测的就不是终态分桶了（codex2）。
    # 水位起点显式播到 D-4，冻结从 D-3 起步，与其他用例的历史数据无关，结果确定。
    creation_day = (_beijing_today() - timedelta(days=3))
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO lane_rollup_watermark (route, backfill_from, through_day)"
            " VALUES ('resident', %s, %s)",
            ((creation_day - timedelta(days=1)).isoformat(),
             (creation_day - timedelta(days=1)).isoformat()))
    db.freeze_completed_resident_lane_days()
    before = db.admin_lane_rollup(user_id=uid)
    wm = before["coverage"]["resident"]
    assert wm["through_day"] > creation_day.isoformat(), (
        f"水位没越过创建日 {creation_day}，本用例无法证明终态分桶: {wm}")
    # 创建日已冻结，而它当时非终态 → 那天的格子里没有它（这正是「蒸发」的前半）
    assert [r for r in before["rows"] if r["day"] == creation_day.isoformat()] == []
    assert before["stuck"]["total"] == 1, before["stuck"]
    seq = before["stuck"]["rows"][0]["job_seqs"][0]
    done_at = datetime.now(timezone.utc)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE user_logs SET doc = doc || %s WHERE user_id=%s AND seq=%s",
            (db.Jsonb({"status": "completed",
                       "completed_at": done_at.isoformat()}), uid, seq))
    after = db.admin_lane_rollup(user_id=uid)
    assert after["stuck"]["total"] == 0, "终结后仍被报成 stuck"
    live = [r for r in after["today_partial"] if r["route"] == "resident"]
    assert [r["day"] for r in live] == [_beijing_today().isoformat()], live
    assert live[0]["completed"] == 1


def test_running_job_past_queue_deadline_is_not_stuck(clean_rollup):
    """codex2 验签判据②：running 的 job 早已离开队列，queue_deadline_at 过期是
    正常的；只要租约还在就没卡住。三字段无差别 OR 会把健康的 running 报成 stuck
    —— 而 stuck 一旦有假阳性就没人会再认真看它，等于这个指标白加。"""
    uid = "usr_rollup_running"
    seed_user(uid)
    past = datetime.now(timezone.utc) - timedelta(hours=3)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at,"
            " queue_deadline_at, lease_expires_at, deadline_at)"
            " VALUES (%s,'chat','running',%s,%s,%s,%s)",
            (uid, past, past, future, future))
    assert db.admin_lane_rollup(user_id=uid)["stuck"]["total"] == 0
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET lease_expires_at=%s, deadline_at=%s"
            " WHERE user_id=%s", (past, past, uid))
    assert db.admin_lane_rollup(user_id=uid)["stuck"]["total"] == 1


def test_stuck_matches_the_authoritative_stale_contract(clean_rollup):
    """判据只能有一份：claimed/running 的卡住判定逐字照抄 jobs_store 的
    COALESCE(lease_expires_at, deadline_at) <= clock_timestamp()
    （jobs_store.py:636 / :2006）。租约已过、deadline 还在未来的 job，jobs_store
    判 stale=true —— 这里若判不 stuck，同一个 job 两个面板两种说法，比缺这个指标
    更坏（codex2 PG 复现）。"""
    uid = "usr_rollup_contract"
    seed_user(uid)
    lease_past = datetime.now(timezone.utc) - timedelta(minutes=1)
    deadline_future = datetime.now(timezone.utc) + timedelta(hours=1)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at,"
            " lease_expires_at, deadline_at) VALUES (%s,'chat','running',%s,%s,%s)",
            (uid, lease_past, lease_past, deadline_future))
    assert db.admin_lane_rollup(user_id=uid)["stuck"]["total"] == 1, (
        "lease 已过期就该算卡住 —— COALESCE 取的是 lease，deadline 在未来不救它")
    # 与权威实现对同一行的判定必须一致，不是「差不多」
    with db.get_pool().connection() as conn:
        (authoritative,) = conn.execute(
            "SELECT COALESCE(lease_expires_at, deadline_at) IS NOT NULL"
            " AND COALESCE(lease_expires_at, deadline_at) <= clock_timestamp()"
            " FROM agent_jobs WHERE user_id=%s", (uid,)).fetchone()
    assert authoritative is True


def _authoritative_stale(uid: str) -> bool:
    """jobs_store 判 stale 的那条 SQL，原样跑在同一行上。

    测试必须对**同一行**同时断言权威判定与面板判定 —— 只断言面板自己的结果，
    改坏的只是我这边时它照样绿（两个面板各自自洽、彼此矛盾，正是要防的）。"""
    from model_api_runtime.v2.jobs_store import PENDING_CHAT_TTL_SEC
    with db.get_pool().connection() as conn:
        (stale,) = conn.execute(
            "SELECT CASE WHEN status='pending' THEN "
            "  COALESCE(queue_deadline_at,deadline_at,"
            "    CASE WHEN lane='chat' THEN "
            "      created_at + make_interval(secs => %s) END) "
            "      <= clock_timestamp() "
            "ELSE COALESCE(lease_expires_at,deadline_at) IS NOT NULL "
            "  AND COALESCE(lease_expires_at,deadline_at) "
            "      <= clock_timestamp() END "
            "FROM agent_jobs WHERE user_id=%s",
            (float(PENDING_CHAT_TTL_SEC), uid)).fetchone()
    return bool(stale)


def test_pending_stuck_follows_the_deadline_at_fallback(clean_rollup):
    """codex2 实证①：dream pending，queue_deadline_at 为 NULL 而 deadline_at 已过。
    权威契约的 COALESCE 会退到 deadline_at 判 stale；面板只看 queue_deadline_at
    就会漏判。"""
    uid = "usr_rollup_pend_deadline"
    seed_user(uid)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at,"
            " queue_deadline_at, deadline_at)"
            " VALUES (%s,'dream','pending',%s,NULL,%s)", (uid, past, past))
    assert _authoritative_stale(uid) is True
    assert db.admin_lane_rollup(user_id=uid)["stuck"]["total"] == 1


def test_pending_chat_stuck_follows_the_created_at_ttl_fallback(clean_rollup):
    """codex2 实证②：chat pending，两个 deadline 都是 NULL、created_at 在 1 小时前。
    权威契约最后一级 fallback 是 created_at + PENDING_CHAT_TTL_SEC（仅 chat 道），
    面板必须复用同一个常量而不是另造一份 —— 两处各写一份迟早会漂。"""
    from model_api_runtime.v2.jobs_store import PENDING_CHAT_TTL_SEC
    uid = "usr_rollup_pend_ttl"
    seed_user(uid)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at,"
            " queue_deadline_at, deadline_at)"
            " VALUES (%s,'chat','pending',%s,NULL,NULL)", (uid, past))
    assert _authoritative_stale(uid) is True
    assert db.admin_lane_rollup(user_id=uid)["stuck"]["total"] == 1
    # 非 chat 道没有这级 fallback：三者皆 NULL 时权威判定为 NULL(非 true)，
    # 面板同样不得报 stuck —— 边界两侧都要钉住，否则「都报 stuck」也能过。
    uid2 = "usr_rollup_pend_nottl"
    seed_user(uid2)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at,"
            " queue_deadline_at, deadline_at)"
            " VALUES (%s,'dream','pending',%s,NULL,NULL)", (uid2, past))
    assert _authoritative_stale(uid2) is False
    assert db.admin_lane_rollup(user_id=uid2)["stuck"]["total"] == 0
    assert PENDING_CHAT_TTL_SEC > 0  # TTL 来自 jobs_store，面板不另造常量


def test_safe_ts_is_stable_not_immutable(clean_rollup):
    """lane_rollup_safe_ts 必须声明 STABLE。无 offset 的文本按 session TimeZone
    解析，同一输入在不同会话是**不同瞬间**；标成 IMMUTABLE 是在骗规划器，
    允许跨会话常量折叠、也允许进索引表达式（换时区重建即静默损坏索引）。"""
    with db.get_pool().connection() as conn:
        (volatility,) = conn.execute(
            "SELECT provolatile FROM pg_proc WHERE proname='lane_rollup_safe_ts'"
        ).fetchone()
        assert volatility == "s", f"期望 STABLE(s)，实际 {volatility!r}"
        # 证明它确实随时区变 —— 这就是不能标 IMMUTABLE 的原因本身
        conn.execute("SET TimeZone='UTC'")
        (a,) = conn.execute(
            "SELECT lane_rollup_safe_ts('2026-01-01T00:00:00')").fetchone()
        conn.execute("SET TimeZone='Asia/Shanghai'")
        (b,) = conn.execute(
            "SELECT lane_rollup_safe_ts('2026-01-01T00:00:00')").fetchone()
        conn.execute("SET TimeZone='UTC'")
    assert a != b, "若两会话结果相同则本断言失去意义，需重新检查前提"


# --- stuck：与失败率并排的一等指标（Seven 2026-08-18 定 A） ------------------ #

def test_permanently_pending_job_is_absent_from_the_rate_but_present_in_stuck(
        clean_rollup):
    """Seven's ruling, both halves in one test: a never-terminating attempt
    must NOT move the failure rate (neither numerator nor denominator), and
    must NOT vanish — it has to surface in stuck, with enough to find the
    actual job.

    ⚠️ 承重情况必须说清楚，别把同义反复当守卫（T122 的教训）：
    **前半是构造上恒真的**。计数全部按状态 FILTER、没有「尝试总数」列，所以
    非终态 job 只会并进同组、对任何 FILTER 贡献 0 —— 自查时把状态清单和
    finished_at 时间闸**同时**破掉，产物依旧一模一样，没有任何突变能让它失败。
    留着它是为了把口径钉在代码里（Seven 定 A 的书面依据），不是因为它能咬人。

    而这恰恰暴露了同一件事的另一面：**正因为格子里没有「尝试总数」，永久卡住的
    job 在格子里完全不可见**。这不是两个问题，是一个问题的两面 —— stuck 区块就是
    对这一面的补偿，所以后半（stuck 可见 + 能定位）才是本测试真正承重的部分，
    突变 20/21 咬的也是它。"""
    uid = "usr_rollup_stuck"
    seed_user(uid)
    # A：与被冻结那天**同一天**创建的 pending job。放在同一天是关键 —— 早先版本
    # 把它放在窗口之外，于是它是被「日期」排除的而不是被「终态性」排除的，判据
    # 完全不承重（自查突变时 pending 混进终态集合，测试照绿）。
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at)"
            " VALUES (%s,'heartbeat','pending',%s)",
            (uid, datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc)))
    # B：早已过自身 deadline 的 pending job，用来验 stuck（stuck 按真实 now()
    # 判定，与冻结用的合成日期是两个时间轴，故必须分开造）。换一条道 ——
    # ux_agent_jobs_singleflight 不允许同一 (user, lane) 存在两个非终态 job。
    long_ago = datetime.now(timezone.utc) - timedelta(days=2)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at,"
            " queue_deadline_at) VALUES (%s,'dream','pending',%s,%s)",
            (uid, long_ago, long_ago))
    _insert_job(uid, "heartbeat", "completed",
                finished=datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc))
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    payload = db.admin_lane_rollup(user_id=uid)
    # 分子/分母都不含 A：当天只该有一条格子、且总尝试数恰为 1（那条 completed）。
    # 断言格子条数本身是承重的 —— 非终态混进来会多出一条全零幽灵格子。
    frozen = [r for r in payload["rows"] if r["route"] == "model_api"]
    assert len(frozen) == 1, f"非终态尝试漏进了格子: {frozen}"
    assert sum(r["completed"] + r["failed"] + r["expired"] + r["superseded"]
               for r in frozen) == 1
    assert all(r["failed"] == 0 for r in frozen)
    # 但它必须在 stuck 里,且能定位到具体 job
    (s,) = [r for r in payload["stuck"]["rows"] if r["route"] == "model_api"]
    assert s["count"] == 1 and s["lane"] == "dream"
    assert len(s["job_ids"]) == 1 and s["job_ids"][0] > 0
    assert s["basis"] == "past_own_deadline"
    assert payload["stuck"]["total"] == 1


def test_stuck_covers_the_resident_source_with_a_declared_threshold(clean_rollup):
    """resident's user_logs has no deadline column, so the criterion is an age
    threshold — which therefore has to travel WITH the number, or the reader
    can't tell what 'stuck' meant."""
    uid = "usr_rollup_stuck_res"
    _seed_resident(uid)
    _log_job(uid, ts=datetime.now(timezone.utc) - timedelta(days=2),
             status="pending", kind="heartbeat_tick")
    payload = db.admin_lane_rollup(user_id=uid)
    (s,) = [r for r in payload["stuck"]["rows"] if r["route"] == "resident"]
    assert s["count"] == 1 and s["lane"] == "heartbeat"
    assert s["job_seqs"] and s["basis"] == "older_than_threshold"
    assert payload["stuck"]["stuck_after_hours"] == db._LANE_ROLLUP_STUCK_AFTER_HOURS


def test_recent_nonterminal_job_is_not_yet_stuck(clean_rollup):
    """In-flight is not stuck — otherwise every healthy queue reads as broken
    and the signal is worthless."""
    uid = "usr_rollup_inflight"
    _seed_resident(uid)
    _log_job(uid, ts=datetime.now(timezone.utc), status="pending",
             kind="heartbeat_tick")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, created_at,"
            " queue_deadline_at) VALUES (%s,'chat','pending',now(),"
            " now() + interval '1 hour')", (uid,))
    payload = db.admin_lane_rollup(user_id=uid)
    assert payload["stuck"]["total"] == 0


def test_stuck_is_visible_on_the_panel_next_to_lane_health():
    """Seven: stuck must sit ALONGSIDE the failure rate, not on a secondary
    page. Endpoint-only would repeat the exact failure this metric exists to
    fix — a real signal that nobody looking at the dashboard can see."""
    from admin import data_track
    html_out = data_track._render_runtime_health_page(
        {"window_hours": 24, "lanes": [], "pool": {}},
        None, None, None,
        {"rows": [{"user_id": "usr_x", "route": "resident", "lane": "heartbeat",
                   "count": 3, "oldest_at": "2030-06-01T00:00:00+00:00",
                   "job_seqs": [11, 12], "basis": "older_than_threshold"}],
         "total": 3, "stuck_after_hours": 6.0},
    )
    assert "卡住的尝试" in html_out
    assert "usr_x" in html_out and "heartbeat" in html_out
    assert "11,12" in html_out, "定位信息必须落到页面上,只给数字等于查不下去"
    # 与 lane 健康表同页、且紧随其后 —— 这就是「并排」的可检验含义
    assert html_out.index("各 lane 健康") < html_out.index("卡住的尝试")
    assert html_out.index("卡住的尝试") < html_out.index("未成功原因 Top")


def test_stuck_unavailable_renders_as_unknown_not_zero():
    """独立失败域取不到时显「暂不可用」——0 意味着「确认过是零」,
    本页其余部分就是这个姿态,stuck 不能破例。"""
    from admin import data_track
    html_out = data_track._render_runtime_health_page(
        {"window_hours": 24, "lanes": [], "pool": {}}, None, None, None, None)
    assert "卡住的尝试" in html_out
    assert "暂不可用" in html_out
    assert "卡住的尝试 · 0" not in html_out


# --- endpoint: param validation against the real ASGI app ------------------ #

def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    admin_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


def _get(path: str, params: dict | None = None,
         token: str | None = ADMIN_TOKEN) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        headers = {"X-Admin-Token": token} if token else {}
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            return await client.get(path, params=params or {}, headers=headers)
    return asyncio.run(go())


@pytest.fixture()
def admin_env(monkeypatch):
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)


def test_endpoint_requires_admin_token(admin_env):
    assert _get("/v1/admin/lane-rollup", token=None).status_code == 401


def test_endpoint_rejects_unknown_params(admin_env, monkeypatch):
    called = []
    monkeypatch.setattr(db, "admin_lane_rollup",
                        lambda **kw: called.append(kw) or {})
    resp = _get("/v1/admin/lane-rollup", params={"uid": "x", "bogus": "1"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "unknown_query_params"
    assert body["params"] == ["bogus", "uid"]
    # The silent-drop bug this guard exists for: the db layer must never have
    # been reached with a typo'd param.
    assert called == []
    assert "admin_key" not in body["supported"]
    assert "user_id" in body["supported"]


def test_endpoint_validates_day_and_ints(admin_env):
    assert _get("/v1/admin/lane-rollup",
                params={"since_day": "2030-6-1"}).status_code == 400
    assert _get("/v1/admin/lane-rollup",
                params={"limit": "0"}).status_code == 400
    assert _get("/v1/admin/lane-rollup",
                params={"limit": "abc"}).status_code == 400
    assert _get("/v1/admin/lane-rollup",
                params={"offset": "-1"}).status_code == 400


def test_endpoint_passes_filters_through(admin_env, monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"rows": [], "today_partial": [], "coverage": {},
                "pagination": {}, "filters": {}}

    monkeypatch.setattr(db, "admin_lane_rollup", fake)
    resp = _get("/v1/admin/lane-rollup",
                params={"user_id": "usr_x", "lane": "heartbeat",
                        "since_day": "2030-06-01", "limit": "5"})
    assert resp.status_code == 200
    assert seen["user_id"] == "usr_x"
    assert seen["lane"] == "heartbeat"
    assert seen["since_day"] == "2030-06-01"
    assert seen["limit"] == 5
    assert seen["offset"] == 0


# --- scheduler: single-leader wiring --------------------------------------- #

def test_scheduler_tick_delegates_with_beijing_tz(monkeypatch):
    calls = []

    def freeze(*, now_epoch=None, tz):
        calls.append((now_epoch, tz))
        return ["2030-06-03"]

    monkeypatch.setattr(sched.db, "freeze_completed_lane_days", freeze)
    assert sched._tick(now_epoch=123.0) == ["2030-06-03"]
    assert calls == [(123.0, "Asia/Shanghai")]


def test_scheduler_start_spawns_daemon_thread(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, *, target, daemon, name):
            started.update(target=target, daemon=daemon, name=name)

        def start(self):
            started["started"] = True

    monkeypatch.setattr(sched.threading, "Thread", FakeThread)
    sched.start()
    assert started == {
        "target": sched._loop,
        "daemon": True,
        "name": "lane-rollup",
        "started": True,
    }


def test_lifespan_leader_uses_distinct_singleton_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        core_leader,
        "run_singleton",
        lambda name, start_fn: calls.append((name, start_fn)),
    )
    lifespan_mod._start_lane_rollup_leader()
    assert calls == [("lane-rollup", sched.start)]


# --- phase 3a: speak rate + the measured blind spot ------------------------- #
#
# Seven 2026-08-18: the proactive side needs TWO numbers on one denominator —
# failure rate answers "is the machinery healthy", speak rate answers "is the
# agent's choice right". A clean run that says nothing is ``completed``, i.e.
# indistinguishable from a successful reply on the failure rate alone.
#
# The rule these tests exist to hold: ``spoke`` is anchored on OUTPUT. Every
# case below creates (or withholds) an actual delivery row; none of them can be
# satisfied by writing a status column.


def test_spoke_counts_a_delivered_bubble_not_a_status_column(clean_rollup):
    """The load-bearing distinction: wake_result is a claim, the outbox row is
    the product. A turn that declared itself asleep but DID deliver counts as
    speaking; a turn that declared nothing and delivered nothing does not."""
    uid = "usr_rollup_voice_anchor"
    seed_user(uid)
    t = datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc)
    spoke_id = _insert_job(uid, "heartbeat", "completed", finished=t)
    _deliver(spoke_id, uid)
    # Declared asleep yet a bubble landed — output wins over the status field.
    contradictory = _insert_job(uid, "heartbeat", "completed", finished=t,
                                wake_result="sleep")
    _deliver(contradictory, uid)
    _insert_job(uid, "heartbeat", "completed", finished=t, wake_result="sleep")
    _insert_job(uid, "heartbeat", "completed", finished=t)

    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    (cell,) = [r for r in _cells(user_id=uid) if r["lane"] == "heartbeat"]
    assert cell["completed"] == 4
    assert cell["spoke"] == 2
    assert cell["silent_declared"] == 1
    assert cell["silent_undeclared"] == 1


def test_voice_decomposition_is_exact_for_every_frozen_cell(clean_rollup):
    """completed = spoke_completed + silent_declared + silent_undeclared.

    This identity is the whole reason the four columns can be trusted: any
    attempt that terminates ok lands in exactly one bucket. If a future edit
    drops a case (say a new wake_result value that matches neither branch),
    the sum stops closing and this fails — which is the point.

    Every NON-completed terminal status that still delivered a bubble is
    represented below, because that is where the earlier subtractive form
    broke: it deducted only ``failed`` and let expired/superseded leak."""
    uid = "usr_rollup_voice_identity"
    seed_user(uid)
    t = datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc)
    # TWO completed-and-spoke against ONE failed-and-spoke. The counts must
    # differ: with one of each, a deduction that wrongly keys on ``failed``
    # still makes the sum close by pure numeric coincidence, and this test goes
    # green against the very bug it exists to catch (observed — the first
    # version of this case had 1:1 and survived that mutation).
    _deliver(_insert_job(uid, "heartbeat", "completed", finished=t), uid)
    _deliver(_insert_job(uid, "heartbeat", "completed", finished=t), uid)
    _insert_job(uid, "heartbeat", "completed", finished=t, wake_result="sleep")
    _insert_job(uid, "heartbeat", "completed", finished=t)
    # An unfamiliar wake_result must NOT vanish from the decomposition: it is
    # undeclared silence, not a fourth invisible bucket.
    _insert_job(uid, "heartbeat", "completed", finished=t,
                wake_result="some_future_value")
    # Delivered a bubble, then did NOT complete. codex2's counter-example on a
    # fresh PG head: an intermediate reply is applied, the lease then times out
    # and the reaper terminalizes the job ``expired``. Under the subtractive
    # form this produced completed=0 against a right-hand side of 1.
    for terminal in ("failed", "expired", "superseded"):
        spoke_then = _insert_job(uid, "heartbeat", terminal, finished=t,
                                 last_error="lease_timeout")
        _deliver(spoke_then, uid,
                 effect_type="reply_intermediate_fenced_v1")
    _insert_job(uid, "scheduled", "completed", finished=t)

    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    cells = _cells(user_id=uid)
    assert cells, "no cells frozen — the identity below would be vacuous"
    for cell in cells:
        assert cell["completed"] == (
            cell["spoke_completed"]
            + cell["silent_declared"] + cell["silent_undeclared"]
        ), f"decomposition does not close for {cell['lane']}: {cell}"
    (hb,) = [r for r in cells if r["lane"] == "heartbeat"]
    # 5 spoke: two completed + the three non-completed terminals.
    assert hb["spoke"] == 5 and hb["spoke_completed"] == 2
    assert hb["silent_declared"] == 1
    assert hb["silent_undeclared"] == 2
    assert hb["expired"] == 1 and hb["superseded"] == 1


def test_spoke_uses_the_wide_reply_predicate_from_its_single_source():
    """An intermediate bubble is still the agent speaking, and
    ``applied_with_results`` is still a completed delivery.

    Both were nearly missed by hand-copying: jobs_store carries a deliberately
    NARROWER final-delivery predicate next to the wide one, and the composite
    sink's status is easy to overlook. The predicate therefore lives in exactly
    one place and this pins what that place must contain."""
    from model_api_runtime.v2 import effect_outbox as eo

    assert eo.REPLY_EFFECT_TYPES == {
        "reply",
        eo.FINAL_REPLY_EFFECT_TYPE,
        eo.TERMINAL_REPLY_EFFECT_TYPE,
        eo.INTERMEDIATE_REPLY_EFFECT_TYPE,
    }
    assert eo.DELIVERED_EFFECT_STATUSES == {
        "applied", eo.APPLIED_WITH_RESULTS_STATUS,
    }
    # The narrow final-delivery set must stay a strict subset — if they ever
    # became equal, the two questions would have collapsed into one.
    narrow = {eo.FINAL_REPLY_EFFECT_TYPE, eo.TERMINAL_REPLY_EFFECT_TYPE}
    assert narrow < eo.REPLY_EFFECT_TYPES


@pytest.mark.parametrize(
    "effect_type,status",
    [
        ("reply_intermediate_fenced_v1", "applied"),
        ("reply_terminal_fenced_v1", "applied_with_results"),
        ("reply", "applied"),
    ],
)
def test_every_delivered_reply_shape_counts_as_speaking(clean_rollup,
                                                        effect_type, status):
    uid = f"usr_voice_{effect_type[:12]}_{status[:8]}"
    seed_user(uid)
    t = datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc)
    job_id = _insert_job(uid, "heartbeat", "completed", finished=t)
    _deliver(job_id, uid, effect_type=effect_type, status=status)
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    (cell,) = [r for r in _cells(user_id=uid) if r["lane"] == "heartbeat"]
    assert cell["spoke"] == 1, f"{effect_type}/{status} should count as spoken"
    assert cell["silent_undeclared"] == 0


def test_an_undelivered_effect_is_not_speaking(clean_rollup):
    """A pending or discarded effect never reached the user. Counting it would
    make the speak rate describe intent instead of product."""
    uid = "usr_rollup_voice_pending"
    seed_user(uid)
    t = datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc)
    pending = _insert_job(uid, "heartbeat", "completed", finished=t)
    _deliver(pending, uid, status="pending")
    discarded = _insert_job(uid, "heartbeat", "completed", finished=t)
    _deliver(discarded, uid, status="discarded")
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    (cell,) = [r for r in _cells(user_id=uid) if r["lane"] == "heartbeat"]
    assert cell["spoke"] == 0
    assert cell["silent_undeclared"] == 2


def test_a_non_reply_effect_is_not_speaking(clean_rollup):
    """Status/memory/schedule effects are side effects, not bubbles."""
    uid = "usr_rollup_voice_nonreply"
    seed_user(uid)
    t = datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc)
    job_id = _insert_job(uid, "heartbeat", "completed", finished=t)
    _deliver(job_id, uid, effect_type="status")
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    (cell,) = [r for r in _cells(user_id=uid) if r["lane"] == "heartbeat"]
    assert cell["spoke"] == 0


def _post_proactive_reply(user_id: str, job_id: str, *, ts: datetime) -> None:
    """The resident delivery anchor: a chat row carrying this job's id.

    chat_core writes ``proactive_job_id`` into the message extra, so V1
    attributes a delivered bubble to a specific attempt at the same strength
    V2 gets from the outbox — no fallback to ``status='posted'`` needed."""
    db.chat_append(
        user_id, f"msg-{job_id}", ts.timestamp(),
        {
            "id": f"msg-{job_id}",
            "role": "openclaw",
            "source": "agent_initiated_proactive",
            "proactive_job_id": job_id,
        },
        1000,
    )


def _log_job_id(user_id: str, **kwargs) -> str:
    """_log_job with an explicit job_id so a delivery can point back at it."""
    job_id = kwargs.pop("job_id")
    ts = kwargs["ts"]
    doc: dict = {"status": kwargs["status"], "job_id": job_id,
                 "created_at": ts.isoformat()}
    if kwargs.get("kind"):
        doc["wake_kind"] = kwargs["kind"]
    if kwargs.get("wake_result"):
        doc["wake_result"] = kwargs["wake_result"]
    terminal_at = kwargs.get("terminal_at") or ts
    # Field name per status, matching the production bucketing CASE — 'posted'
    # reads posted_at, NOT completed_at. Writing the wrong key here silently
    # sends the row down the l.ts fallback and lands the cell on the creation
    # day, which looks like a code bug rather than a fixture bug.
    key = {
        "posted": "posted_at",
        "delivered": "posted_at",
        "failed": "failed_at",
        "error": "failed_at",
        "skipped": "failed_at",
    }.get(kwargs["status"], "completed_at")
    doc[key] = terminal_at.isoformat()
    db.log_append(user_id, "proactive_jobs", doc, ts=ts.timestamp())
    return job_id


def test_resident_spoke_follows_the_chat_row_not_the_posted_status(clean_rollup):
    """V1's anchor is the delivered chat row, reached via proactive_job_id.

    The contradiction case is the one that matters: a job stamped ``posted``
    whose message never landed must NOT count as speaking — that is exactly
    the broadcast=on-yet-zero-frames failure mode, one layer down."""
    uid = "usr_rollup_res_voice"
    _seed_resident(uid)
    t = datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc)
    spoke = _log_job_id(uid, job_id="pj-spoke", ts=t, status="posted",
                        kind="heartbeat_tick")
    _post_proactive_reply(uid, spoke, ts=t)
    # Claims delivery, produced nothing.
    _log_job_id(uid, job_id="pj-claims", ts=t, status="posted",
                kind="heartbeat_tick")
    _log_job_id(uid, job_id="pj-slept", ts=t, status="completed",
                kind="heartbeat_tick", wake_result="sleep")
    _log_job_id(uid, job_id="pj-quiet", ts=t, status="completed",
                kind="heartbeat_tick")

    db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    (cell,) = [r for r in _cells(user_id=uid) if r["lane"] == "heartbeat"]
    assert cell["completed"] == 4
    assert cell["spoke"] == 1
    assert cell["silent_declared"] == 1
    assert cell["silent_undeclared"] == 2
    assert cell["completed"] == (
        (cell["spoke_completed"])
        + cell["silent_declared"] + cell["silent_undeclared"]
    )


def test_voice_coverage_marks_where_the_numbers_start(clean_rollup):
    """``voice_from`` exists so a 0 in these columns cannot be read as "we
    measured, it stayed quiet" for days frozen before the columns existed.

    It pins to the FIRST day frozen with voice numbers and never moves — a
    later tick must not drag the honest start date forward."""
    uid = "usr_rollup_voice_cov"
    seed_user(uid)
    t = datetime(2030, 6, 1, 2, 0, tzinfo=timezone.utc)
    _deliver(_insert_job(uid, "heartbeat", "completed", finished=t), uid)
    db.freeze_completed_lane_days(now_epoch=_NOW_EPOCH)
    first = db.admin_lane_rollup(route="model_api")["coverage"]["model_api"]
    assert first["voice_from"] == "2030-06-01"

    # A later tick freezes more days; the start date must stay put.
    later = datetime(2030, 6, 6, 4, 0, tzinfo=timezone.utc).timestamp()
    db.freeze_completed_lane_days(now_epoch=later)
    again = db.admin_lane_rollup(route="model_api")["coverage"]["model_api"]
    assert again["voice_from"] == "2030-06-01"
    assert again["through_day"] > first["through_day"]


def test_live_topup_reports_voice_for_the_still_open_day(clean_rollup):
    """The unfrozen day is where an operator looks during an incident. If the
    live path returned counts without voice columns, today would silently read
    as "nobody spoke" next to frozen days that report properly."""
    uid = "usr_rollup_voice_live"
    seed_user(uid)
    now = datetime.now(timezone.utc)
    spoke = _insert_job(uid, "heartbeat", "completed", finished=now)
    _deliver(spoke, uid)
    _insert_job(uid, "heartbeat", "completed", finished=now, wake_result="sleep")

    # The open day is returned in ``today_partial``, deliberately separate from
    # the frozen ``rows`` so an estimate can never be mistaken for a cell.
    payload = db.admin_lane_rollup(user_id=uid, route="model_api")
    assert not payload["rows"], "nothing is frozen yet in this test"
    rows = [r for r in payload["today_partial"]
            if r["day"] == _beijing_today().isoformat()]
    assert rows, "live top-up returned nothing for the open day"
    (row,) = rows
    assert row["frozen"] is False
    assert row["spoke"] == 1
    assert row["silent_declared"] == 1
    assert row["completed"] == (
        (row["spoke_completed"])
        + row["silent_declared"] + row["silent_undeclared"]
    )


def test_resident_spoke_survives_a_terminal_patch_days_after_delivery(clean_rollup):
    """The delivery is found by job id, never by a time window.

    codex2's counter-example on a fresh PG head: the chat row lands on day D,
    the consumer's separate ``update_proactive_job_status`` call only stamps
    the terminal state on D+2 (its failure path merely warns, and a restart
    can delay it arbitrarily). A window with a one-day lower bound dropped the
    real delivery and recorded the attempt as ``silent_undeclared`` — inflating
    precisely the column whose whole job is to be an honest blind-spot count.
    """
    uid = "usr_rollup_res_late_patch"
    _seed_resident(uid)
    delivered_on = datetime(2030, 5, 29, 3, 0, tzinfo=timezone.utc)
    terminal_on = datetime(2030, 6, 1, 3, 0, tzinfo=timezone.utc)
    job = _log_job_id(uid, job_id="pj-late", ts=delivered_on, status="posted",
                      kind="heartbeat_tick", terminal_at=terminal_on)
    _post_proactive_reply(uid, job, ts=delivered_on)

    db.freeze_completed_resident_lane_days(now_epoch=_NOW_EPOCH)
    cells = {(r["day"], r["lane"]): r for r in _cells(user_id=uid)}
    cell = cells[("2030-06-01", "heartbeat")]
    assert cell["completed"] == 1
    assert cell["spoke"] == 1, "delivery 3 days before the terminal patch was lost"
    assert cell["silent_undeclared"] == 0
    assert cell["completed"] == (
        cell["spoke_completed"]
        + cell["silent_declared"] + cell["silent_undeclared"]
    )


def test_the_resident_anchor_has_an_index_to_stand_on():
    """The windowless anchor is only affordable because 0093/0025 build a
    partial expression index; without it each freeze degrades into a per-job
    sequential scan of chat_messages. Both chains must create it — test's
    primary is the TEE database, so the freezer queries THAT one."""
    import importlib.util
    from pathlib import Path as _P

    def _load(rel: str, name: str):
        spec = importlib.util.spec_from_file_location(
            name, _P(__file__).parent.parent / rel)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    rds = _load("backend/alembic/versions/0093_lane_rollup_voice.py", "rds93")
    tee = _load("backend/alembic_tee/versions/0025_lane_rollup_voice.py", "tee25")

    assert rds._PROACTIVE_ANCHOR_DDL == tee._PROACTIVE_ANCHOR_DDL
    ddl = rds._PROACTIVE_ANCHOR_DDL
    # The predicate the freeze actually issues must be coverable by this index:
    # both the user and the job id, and the same partial WHERE so the planner
    # can prove applicability.
    assert "chat_messages (user_id, (doc->>'proactive_job_id'))" in ddl
    assert "WHERE COALESCE(doc->>'proactive_job_id','') <> ''" in ddl
    assert "CONCURRENTLY" in ddl
    for source in ("backend/alembic/versions/0093_lane_rollup_voice.py",
                   "backend/alembic_tee/versions/0025_lane_rollup_voice.py"):
        # CONCURRENTLY cannot run inside the migration's transaction.
        assert "autocommit_block()" in (_P(__file__).parent.parent
                                        / source).read_text()
