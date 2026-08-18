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
from datetime import datetime, timezone
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
                reason: str | None = None, last_error: str | None = None) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, reason, last_error,"
            " created_at, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (user_id, lane, status, reason, last_error, finished, finished),
        )


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
    # Param layout: (day, lane, src, completed, failed, expired, superseded,
    # codes, uid) — uid last, feeding the users FOR KEY SHARE source select.
    mirrored = {(p[8], p[0], p[1], p[3], p[4]) for _, p in day1[:-1]}
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
