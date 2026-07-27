"""In-process TEE auto-sync scheduler (admin.tee_sync_scheduler).

Drives the same tee_replication.run_action a manual run would; these tests stub
run_action and assert the per-tick call sequence + skip semantics. (A completed
tick now also records one tee_sync_runs metrics row + probes TEE health — a
best-effort side effect covered by test_tee_sync_metrics.py; it is harmless
here and not asserted on.)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from admin import tee_sync_scheduler as sched  # noqa: E402
from admin import tee_replication as tr  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_table_backoff():
    """`sched._table_backoff` 是模块级状态（生产上 worker 级正确；测试间必须归零）。
    别的测试文件（如 test_tee_sync_metrics 走过失败路径的 _sync_tick）留下的退避
    条目会让这里的「每 tick 全表 replicate」断言看到被跳过的表——反之亦然。"""
    sched._table_backoff.clear()
    yield
    sched._table_backoff.clear()


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake_run_action(*, action, table=None, dry_run=True, confirm=None, **kw):
        recorded.append((action, table, dry_run, confirm))
        return {"ok": True, "copied": 0, "pending": 0, "errors": 0}

    monkeypatch.setattr(tr, "run_action", fake_run_action)
    return recorded


def test_replicate_tick_hits_every_ciphertext_table(calls):
    sched._sync_tick(do_reconcile=False)
    replicated = [c for c in calls if c[0] == "replicate"]
    assert [c[1] for c in replicated] == list(sched._ciphertext_tables())
    # non-dry-run + confirm gate carried on every replicate
    assert all(c[2] is False and c[3] == "MIGRATE" for c in replicated)
    # do_reconcile=False → no reconcile/verify
    assert not any(c[0] in ("reconcile", "verify") for c in calls)


def test_reconcile_runs_before_replicate_then_verify(calls):
    # Order is load-bearing: reconcile backfills the plaintext `users` parent
    # BEFORE any ciphertext child replicate, or the children FK-fail.
    sched._sync_tick(do_reconcile=True)
    actions = [c[0] for c in calls]
    assert actions == ["reconcile"] + ["replicate"] * 5 + ["verify"]
    reconcile = next(c for c in calls if c[0] == "reconcile")
    verify = next(c for c in calls if c[0] == "verify")
    assert reconcile[3] == "MIGRATE"
    assert verify[2] is False  # dry_run=False, verify is confirm-exempt


def test_reconcile_failure_does_not_block_replicate(monkeypatch):
    # A reconcile error must be swallowed and replicate still attempted (the
    # loop degrades, never dies).
    calls = []

    def fake(*, action, table=None, dry_run=True, confirm=None, **kw):
        calls.append(action)
        if action == "reconcile":
            raise RuntimeError("reconcile boom")
        return {"ok": True}

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=True)
    assert calls[0] == "reconcile"
    assert calls.count("replicate") == 5


def test_already_running_aborts_replicate_phase(monkeypatch):
    calls = []

    def fake(*, action, table=None, **kw):
        calls.append((action, table))
        if action == "reconcile":
            return {"tables": []}
        raise tr.AlreadyRunning()

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=True)
    # reconcile ran; first replicate raises AlreadyRunning → return before the rest
    assert calls == [("reconcile", None), ("replicate", "chat_messages")]


def test_unconfigured_aborts_silently(monkeypatch):
    calls = []

    def fake(*, action, table=None, **kw):
        calls.append(action)
        if action == "reconcile":
            return {"tables": []}
        raise tr.Unconfigured()

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=True)
    assert calls == ["reconcile", "replicate"]  # stopped on first replicate Unconfigured


def test_one_table_error_does_not_stop_the_pass(monkeypatch):
    seen = []

    def fake(*, action, table=None, dry_run=True, confirm=None, **kw):
        seen.append(table if action == "replicate" else action)
        if table == "memory_moments":
            raise RuntimeError("enclave hiccup")
        return {"ok": True}

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=False)
    # memory_moments raised a generic error but the loop continued past it
    assert seen == list(sched._ciphertext_tables())


def test_start_spawns_a_daemon_thread(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, target, daemon, name):
            started["daemon"] = daemon
            started["name"] = name

        def start(self):
            started["started"] = True

    monkeypatch.setattr(sched.threading, "Thread", FakeThread)
    sched.start()
    assert started == {"daemon": True, "name": "tee-sync", "started": True}


def test_sync_tick_returns_reconcile_success(calls):
    # Reconcile succeeds → True (caller may hold off next reconcile).
    assert sched._sync_tick(do_reconcile=True) is True
    # No reconcile due → True (no retry pressure).
    assert sched._sync_tick(do_reconcile=False) is True


def test_failed_reconcile_returns_false_for_soon_retry(monkeypatch):
    def fake(*, action, table=None, **kw):
        if action == "reconcile":
            raise RuntimeError("SSL eof")
        return {"ok": True}

    monkeypatch.setattr(tr, "run_action", fake)
    # reconcile failed → False so _loop won't advance last_reconcile (retries next tick).
    assert sched._sync_tick(do_reconcile=True) is False


def test_first_tick_always_reconciles_regardless_of_monotonic(monkeypatch):
    """首个 tick(last_reconcile is None)必 reconcile —— 建立 users 基线,不能靠
    monotonic() 的绝对值(宿主 uptime 小的新 CVM 上它 < reconcile 间隔 → 旧逻辑首 tick
    不 reconcile → FK 全线失败,2026-07-14 prod 实测)。"""
    monkeypatch.setenv("FEEDLING_TEE_RECONCILE_INTERVAL_SEC", "86400")
    # 新进程:last_reconcile=None,monotonic 才几秒(远 < 86400)——旧逻辑会返回 False。
    assert sched._should_reconcile(None, 5.0) is True
    # 已 reconcile 过:未到间隔不重跑
    assert sched._should_reconcile(1000.0, 1000.0 + 86399) is False
    # 到间隔:重跑
    assert sched._should_reconcile(1000.0, 1000.0 + 86400) is True


# --------------------------------------------------------------------------- #
# per-table 失败退避：整表 replicate 连败时指数退避，不再每 tick 无退避地重拉
# 重解密同一段卡住的行（2026-07-14 prod 实测：两张 text-cursor 表连败让名义 300s
# 的 tick 连轴转成 13-87 分钟，成为 backend 内存/CPU churn 主源之一）。
# --------------------------------------------------------------------------- #

def _fail_table_fake(recorded, failing_table):
    def fake(*, action, table=None, dry_run=True, confirm=None, **kw):
        recorded.append((action, table))
        if action == "replicate" and table == failing_table:
            raise RuntimeError("text fields cannot contain NUL")
        return {"ok": True, "copied": 0, "pending": 0, "errors": 0}
    return fake


def test_failing_table_skipped_while_backing_off(monkeypatch):
    recorded: list = []
    monkeypatch.setattr(tr, "run_action", _fail_table_fake(recorded, "memory_moments"))

    sched._sync_tick(do_reconcile=False)          # 第一败
    recorded.clear()
    sched._sync_tick(do_reconcile=False)          # 紧接着的下一 tick
    tables = [t for a, t in recorded if a == "replicate"]
    assert "memory_moments" not in tables          # 退避中 → 跳过
    assert "chat_messages" in tables               # 其他表不受影响


def test_backoff_expires_then_retries(monkeypatch):
    recorded: list = []
    monkeypatch.setattr(tr, "run_action", _fail_table_fake(recorded, "memory_moments"))

    sched._sync_tick(do_reconcile=False)
    # 手动把退避窗口拨到已过期
    fails, _retry_at = sched._table_backoff["memory_moments"]
    sched._table_backoff["memory_moments"] = (fails, 0.0)
    recorded.clear()
    sched._sync_tick(do_reconcile=False)
    assert ("replicate", "memory_moments") in recorded  # 窗口过了 → 重试


def test_backoff_resets_on_success(monkeypatch):
    recorded: list = []
    fail_once = {"n": 0}

    def fake(*, action, table=None, dry_run=True, confirm=None, **kw):
        recorded.append((action, table))
        if action == "replicate" and table == "memory_moments" and fail_once["n"] == 0:
            fail_once["n"] = 1
            raise RuntimeError("transient")
        return {"ok": True, "copied": 0, "pending": 0, "errors": 0}

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=False)                      # 败一次 → 进退避
    sched._table_backoff["memory_moments"] = (1, 0.0)         # 窗口拨到过期
    sched._sync_tick(do_reconcile=False)                      # 重试成功
    assert "memory_moments" not in sched._table_backoff        # 成功 → 清零


def test_backoff_delay_doubles_and_caps(monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_SYNC_INTERVAL_SEC", "300")
    assert sched._backoff_delay(1) == 300.0
    assert sched._backoff_delay(2) == 600.0
    assert sched._backoff_delay(3) == 1200.0
    assert sched._backoff_delay(10) == sched._BACKOFF_CAP_SEC


# --------------------------------------------------------------------------- #
# last_reconcile 必须跨 worker 存活：gunicorn max_requests 回收 leader worker 后,
# 新 leader 若从 None 起步就会重做 reconcile-first——test 实测 reconcile ~40min >
# worker 寿命 → reconcile 永远完不成、tee_sync_runs 零新行(2026-07-14 部署后 2h)。
# 从 tee_sync_runs 恢复上次成功 reconcile 的时点(换算到本进程 monotonic 轴)。
# --------------------------------------------------------------------------- #

def test_restore_last_reconcile_from_db_age(monkeypatch):
    import time as _time
    monkeypatch.setattr(sched.db, "last_tee_reconcile_age_sec", lambda: 100.0)
    restored = sched._restore_last_reconcile()
    assert restored is not None
    assert abs((_time.monotonic() - 100.0) - restored) < 5.0


def test_restore_last_reconcile_none_when_no_history(monkeypatch):
    monkeypatch.setattr(sched.db, "last_tee_reconcile_age_sec", lambda: None)
    assert sched._restore_last_reconcile() is None


def test_restore_last_reconcile_swallows_db_errors(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(sched.db, "last_tee_reconcile_age_sec", boom)
    assert sched._restore_last_reconcile() is None


# --- reconcile completion stamped immediately (alembic 0019 / option B) ------ #

def test_reconcile_success_marked_on_reconcile_tick_only(calls, monkeypatch):
    """reconcile 成功 → 立刻 mark_reconcile_success(在 replicate 之前),让被回收的
    worker 也留下「reconcile 已完成」。replicate-only tick(do_reconcile=False)不 mark。"""
    marks = []
    monkeypatch.setattr(sched.db, "mark_reconcile_success", lambda: marks.append(1))
    sched._sync_tick(do_reconcile=True)
    assert len(marks) == 1            # 本 tick 真 reconcile 了 → 记一次
    sched._sync_tick(do_reconcile=False)
    assert len(marks) == 1            # 只 replicate → 不再 mark


def test_reconcile_failure_does_not_mark(monkeypatch):
    """reconcile 抛错 → 绝不 mark（否则跳过重试、坏基线被当成功),下个 tick 会重试。"""
    marks = []
    monkeypatch.setattr(sched.db, "mark_reconcile_success", lambda: marks.append(1))

    def fake(*, action, table=None, **kw):
        if action == "reconcile":
            raise RuntimeError("gateway eof")
        return {"ok": True}

    monkeypatch.setattr(tr, "run_action", fake)
    sched._sync_tick(do_reconcile=True)
    assert marks == []


def test_snapshot_action_is_accepted():
    from admin import tee_replication as tr

    # 不触发真实运行——只验证校验层放行 snapshot 这个 action。
    tr._validate("snapshot", None, True)


def test_snapshot_action_rejects_unknown_table():
    from admin import tee_replication as tr

    with pytest.raises(tr.BadRequest) as e:
        tr._validate("snapshot", "not_a_real_table", True)
    assert e.value.error == "unknown_table"


def test_ciphertext_tables_are_derived_from_registry():
    """scheduler 不再手工维护密文表清单——它必须来自注册表，否则又会出现
    '加了表但某一处没登记' 的老问题。"""
    from admin import tee_sync_scheduler as sched
    from tee_shadow import table_registry as reg

    want = set(reg.tables_in_lane(reg.CIPHERTEXT)) | set(reg.PSEUDO_CIPHERTEXT_TABLES)
    assert set(sched._ciphertext_tables()) == want


def test_scheduler_covers_every_worker_table():
    """调度器必须覆盖 worker._TABLES 的**每一个 key**，包括非表名的伪 key。

    这条是上一条的反向守卫，缺了它就会重演 2026-07-28 的回归：把写死的
    _CIPHERTEXT_TABLES 改成"按 lane 派生"时，静默丢掉了 "identity" 这个伪表名
    （它实际操作 user_blobs WHERE kind='identity'，而 user_blobs 整表登记在
    MIRROR lane，因此永远不会出现在 CIPHERTEXT lane 里）。后果是整条用户身份/
    人设的同步路径消失，而上一条测试是同义反复、抓不到。
    """
    from admin import tee_sync_scheduler as sched
    from tee_replicator import worker

    missing = sorted(set(worker._TABLES) - set(sched._ciphertext_tables()))
    assert not missing, (
        f"worker 配了这些复制目标，但调度器不会去跑它们：{missing}\n"
        "若某个 key 不是 RDS 表名（伪表名），把它加进 "
        "table_registry.PSEUDO_CIPHERTEXT_TABLES。"
    )


def test_sync_tick_records_snapshot_metrics(monkeypatch):
    from admin import tee_sync_scheduler as sched

    monkeypatch.setattr(sched, "_ciphertext_tables", lambda: ())

    calls = {}

    def fake_run_action(**kw):
        calls[kw["action"]] = kw
        if kw["action"] == "snapshot":
            return {"tables": [{"table": "provider_health", "rows": 3, "ok": True}],
                    "copied": 3, "failures": 0}
        return {"tables": []}

    from admin import tee_replication as tr

    monkeypatch.setattr(tr, "run_action", fake_run_action)
    monkeypatch.setattr(sched.mirror, "probe", lambda: {"ok": True, "latency_ms": 1.0})
    monkeypatch.setattr(sched.db, "record_tee_sync_run", lambda s: None)
    monkeypatch.setattr(sched.db, "mark_reconcile_success", lambda: None)

    sched._sync_tick(do_reconcile=True)
    assert "snapshot" in calls
