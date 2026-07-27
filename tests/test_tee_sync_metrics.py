"""TEE 影子库同步的可观测性:健康探活 + 每-tick 历史落库 + status 端点。

覆盖 migration 0015 引入的 ``tee_sync_runs`` 历史表和它的生产者(调度器
``_sync_tick``)/消费者(``status_payload``),以及 ``mirror.probe`` 健康探活。
"""
from datetime import datetime, timezone

import db
from tee_shadow import mirror


# --------------------------------------------------------------------------- #
# mirror.probe — TEE 健康探活
# --------------------------------------------------------------------------- #
def test_probe_ok_when_tee_reachable(backend_env):
    """conftest 已把 TEE_DATABASE_URL 指向可连的测试库 → 探活应绿、带往返延迟。"""
    p = mirror.probe()
    assert p["ok"] is True
    assert isinstance(p["latency_ms"], float)
    assert p["error"] is None


def test_probe_unconfigured_never_raises(backend_env, monkeypatch):
    """TEE 未接时探活不抛,返回结构化 ok=False（否则可观测端点会 500）。"""
    monkeypatch.delenv("TEE_DATABASE_URL", raising=False)
    p = mirror.probe()
    assert p == {"ok": False, "latency_ms": None, "error": "unconfigured"}


# --------------------------------------------------------------------------- #
# record_tee_sync_run / recent_tee_sync_runs 往返 + delta 钳制
# --------------------------------------------------------------------------- #
def _summary(**over) -> dict:
    from admin import tee_sync_scheduler as sched
    s = sched._blank_summary(over.pop("did_reconcile", True))
    s.update(over)
    return s


def test_record_and_recent_roundtrip(backend_env):
    db.record_tee_sync_run(_summary(
        verify_ran=True, verify_ok=True, unconverged_tables=0, unconverged_users=0,
        replicate_copied=7, replicate_errors=1, tee_healthy=True, tee_probe_ms=12.5,
        mirror_failures=0, report={"health": {"ok": True}}))
    runs = db.recent_tee_sync_runs(limit=5)
    assert len(runs) >= 1
    latest = runs[0]
    assert latest["verify_ok"] is True
    assert latest["replicate_copied"] == 7
    assert latest["replicate_errors"] == 1
    assert latest["tee_healthy"] is True
    assert latest["report"] == {"health": {"ok": True}}  # JSONB parsed back to dict
    assert "ran_at" in latest and isinstance(latest["ran_at"], str)  # ISO-8601


def test_mirror_failures_delta_clamps_on_restart(backend_env):
    # 三次连续 tick:计数 5 → 8 → 2(进程重启把内存计数归零)。
    db.record_tee_sync_run(_summary(mirror_failures=5))
    db.record_tee_sync_run(_summary(mirror_failures=8))
    db.record_tee_sync_run(_summary(mirror_failures=2))
    runs = db.recent_tee_sync_runs(limit=3)  # newest first
    assert runs[0]["mirror_failures"] == 2
    assert runs[0]["mirror_failures_delta"] == 2   # 2-8<0 → 视为重启,delta=当前值
    assert runs[1]["mirror_failures_delta"] == 3   # 8-5
    assert runs[2]["mirror_failures_delta"] == 5   # 首行:无前值 → 当前值


# --------------------------------------------------------------------------- #
# status_payload 暴露 health + recent_runs
# --------------------------------------------------------------------------- #
def test_status_payload_exposes_health_and_history(backend_env):
    from admin import tee_replication as tr
    db.record_tee_sync_run(_summary(verify_ok=True, replicate_copied=1))
    payload = tr.status_payload()
    assert payload["health"]["ok"] is True
    assert isinstance(payload["recent_runs"], list) and payload["recent_runs"]
    assert payload["latest_run"]["replicate_copied"] == 1


# --------------------------------------------------------------------------- #
# 调度器 _sync_tick 端到端:跑一轮 → 落一行带正确聚合的历史
# --------------------------------------------------------------------------- #
def test_sync_tick_persists_one_row(backend_env, monkeypatch):
    from admin import tee_replication as tr
    from admin import tee_sync_scheduler as sched

    wm = datetime(2026, 7, 13, tzinfo=timezone.utc)

    def fake_run_action(*, action, table=None, dry_run=True, confirm=None, **kw):
        if action == "reconcile":
            return {"tables": [{"table": "users", "copied": 3, "pruned": 0,
                                "skipped": 0, "rds_rows": 10, "tee_rows": 10}]}
        if action == "replicate":
            return {"copied": 1, "pending": 0, "errors": 0, "skipped": 0,
                    "watermark_ts": wm, "watermark_id": "x"}
        return {"ok": True, "mismatches": [],
                "tables": {"users": {"rows_ok": True, "user_diffs": {},
                                     "requeue_backlog": 0}}}

    monkeypatch.setattr(tr, "run_action", fake_run_action)

    # 指纹式断言:抓「本 tick 新增的那一行」,不依赖会话里已有多少行(其它调度器
    # 测试也会落行)。串行执行 → tick 后 id 最大且不在 before_ids 里的就是它。
    before_ids = {r["id"] for r in db.recent_tee_sync_runs(limit=500)}
    ok = sched._sync_tick(do_reconcile=True)
    assert ok is True

    after = db.recent_tee_sync_runs(limit=500)
    new_rows = [r for r in after if r["id"] not in before_ids]
    assert len(new_rows) == 1  # 一次 tick 恰落一行
    row = new_rows[0]
    assert row["did_reconcile"] is True
    assert row["reconcile_copied"] == 3
    assert row["replicate_copied"] == len(sched._ciphertext_tables())  # 每表 copied=1
    assert row["verify_ran"] is True and row["verify_ok"] is True
    assert row["unconverged_tables"] == 0 and row["unconverged_users"] == 0
    assert row["tee_healthy"] is True
    # full report 里保留了各阶段明细,供 drill-down
    assert "reconcile" in row["report"] and "replicate" in row["report"]
    assert "verify" in row["report"] and "health" in row["report"]


def test_sync_tick_records_whole_table_replicate_failure(backend_env, monkeypatch):
    """整表 replicate 抛异常(如 TEE 连接掉线)必须计入 replicate_table_failures +
    进 report.replicate_failed,而不是从面板上凭空消失(0016 补的漏报盲区)。"""
    from admin import tee_replication as tr
    from admin import tee_sync_scheduler as sched

    def fake_run_action(*, action, table=None, dry_run=True, confirm=None, **kw):
        if action == "reconcile":
            return {"tables": []}
        if action == "replicate":
            if table == "chat_messages":
                raise RuntimeError("the connection is lost")  # 整表挂,像线上那样
            return {"copied": 1, "pending": 0, "errors": 0, "skipped": 0,
                    "watermark_ts": 0.0, "watermark_id": "x"}
        return {"ok": True, "mismatches": [], "tables": {}}

    monkeypatch.setattr(tr, "run_action", fake_run_action)

    before_ids = {r["id"] for r in db.recent_tee_sync_runs(limit=500)}
    sched._sync_tick(do_reconcile=True)
    new = [r for r in db.recent_tee_sync_runs(limit=500) if r["id"] not in before_ids]
    assert len(new) == 1
    row = new[0]
    assert row["replicate_table_failures"] == 1          # chat_messages 整表挂被计数
    assert row["replicate_copied"] == len(sched._ciphertext_tables()) - 1  # 其余 11 张各 1
    assert row["report"]["replicate_failed"]["chat_messages"] == "the connection is lost"
    assert "chat_messages" not in (row["report"].get("replicate") or {})  # 挂的表不在成功明细里


# --------------------------------------------------------------------------- #
# db.last_tee_reconcile_age_sec — 调度器跨 worker 恢复 last_reconcile 的读侧。
# --------------------------------------------------------------------------- #

def test_last_reconcile_age_none_without_mark(backend_env):
    with db.get_pool().connection() as c:
        c.execute("DELETE FROM tee_reconcile_state")
    assert db.last_tee_reconcile_age_sec() is None
    # A recorded end-of-tick row no longer counts — only mark_reconcile_success
    # (stamped at reconcile completion) does, so a tick that dies in replicate
    # before recording still leaves reconcile correctly marked done.
    from admin import tee_sync_scheduler as sched
    row = sched._blank_summary(True)
    row["reconcile_ok"] = True
    db.record_tee_sync_run(row)
    assert db.last_tee_reconcile_age_sec() is None


def test_last_reconcile_age_reflects_mark(backend_env):
    with db.get_pool().connection() as c:
        c.execute("DELETE FROM tee_reconcile_state")
    db.mark_reconcile_success()
    age = db.last_tee_reconcile_age_sec()
    assert age is not None and 0.0 <= age < 60.0
    db.mark_reconcile_success()  # single-row upsert: idempotent, no dup-key crash
    assert (db.last_tee_reconcile_age_sec() or 999) < 60.0
