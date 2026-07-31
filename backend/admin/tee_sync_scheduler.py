"""In-process TEE 影子库自动同步（后端原生，不依赖外部 cron）。

双写（mirror）只自动搬「明文表的新写入」；存量 + 全部密文表（chat/memory/
identity/world_book/frames，经 enclave 解密成明文）不会自己进影子库。本调度器
在后端进程里定时把它们也同步进来，达到「设完即忘」。

只在**一个** worker 上跑：由 asgi/lifespan.py 经 ``core.leader.run_singleton``
（pg advisory-lock 选主）拉起，故 N 个 gunicorn worker 不会同时复制。每个 tick
走的是手动 run 走的同一入口 ``tee_replication.run_action``（复用它的单-run 锁 +
校验 + confirm 门），所以手动 admin 触发和本循环永不重叠。

节奏（env 可调）：
  - 每 ``FEEDLING_TEE_SYNC_INTERVAL_SEC``（默认 300s）：增量 replicate 每张密文表
    （游标扫描，无新行时是空 SELECT，极廉价）。
  - 每 ``FEEDLING_TEE_RECONCILE_INTERVAL_SEC``（默认 86400s）+ **首个 tick**：
    全量 reconcile（明文表漂移补偿/首次回填）+ verify（对账观测）。

故障隔离：每个操作都兜异常，失败只 log、循环继续，绝不拖垮进程。仅当
``mirror.enabled()``（FEEDLING_TEE_DUAL_WRITE=1 且 TEE_DATABASE_URL 非空）时干活。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import db
from tee_shadow import mirror

log = logging.getLogger("feedling.tee_sync")

# 密文表 —— 经 enclave 解密成明文。**从注册表派生**，不再手工维护：手工清单正是
# "加了表但某一处没登记"的老问题的来源（2026-07-27 之前 V2 的 19 张表一处都没登记）。
#
# ⚠️ 必须并上 PSEUDO_CIPHERTEXT_TABLES。worker._TABLES 的 key 不全是 RDS 表名——
# "identity" 是个伪表名，实际操作的是 `user_blobs WHERE kind='identity'`
# （RDS 侧密文信封 → enclave 解密 → TEE 侧 user_blobs 明文行）。注册表按表名登记，
# user_blobs 整表归 MIRROR lane（reconciler 的 _SCOPE_WHERE 又特意排除 kind='identity'
# 把它让给 replicator），所以 CIPHERTEXT lane 里**不会**出现 "identity" 这个 key。
# 只按 lane 派生会静默丢掉整条用户身份/人设的同步路径（2026-07-28 Task 6 实施期发现）。
def _ciphertext_tables() -> tuple[str, ...]:
    from tee_shadow import table_registry as reg

    return tuple(sorted(
        set(reg.tables_in_lane(reg.CIPHERTEXT)) | set(reg.PSEUDO_CIPHERTEXT_TABLES)
    ))


def _replicable_tables() -> tuple[str, ...]:
    """本 tick 真正要 replicate 的表 = 调度清单 ∩ worker 已配置的表。

    取交集而不是直接用 _ciphertext_tables()：注册表可能已经把某张表登记为 CIPHERTEXT，
    而 tee_replicator.worker._TABLES 里还没有它的复制配置（跨 Task 的中间态，或有人
    先登记后接线）。直接调过去会让 tee_replication._validate 抛
    BadRequest("unknown_table")，被兜底 except 吞掉后计入 replicate_table_failures
    并触发指数退避（封顶 1 小时）——每个 tick 白报错一次，既污染失败率指标，又让
    "真失败"和"还没接线"在指标里混成一团。

    取了交集之后，未接线的表被静默跳过、接线后自动被吸收，不需要任何跨 Task 的部署
    时序协调（否则就得靠"某某 Task 完成前不要部署"这种人工记忆——本仓库已经有多次
    "改完忘了部署/提交"的前科）。反向守卫 test_scheduler_covers_every_worker_table
    的语义不受影响：它管的是"worker 配了的表调度器不能漏"，方向相反。
    """
    from tee_replicator import worker as tee_worker

    return tuple(t for t in _ciphertext_tables() if t in tee_worker._TABLES)

# 首个 tick 的启动延迟（秒）：短于常规间隔，让父表回填尽快发生（见 _loop）。
_FIRST_DELAY = 30.0

# 一行结构化日志 / tee_sync_runs 行里只留这些标量（不含大块 report）——供 tail 日志
# 或按列查趋势用。
_LOG_KEYS = (
    "did_reconcile", "reconcile_ok", "verify_ok", "unconverged_tables",
    "unconverged_users", "requeue_backlog", "replicate_copied", "replicate_pending",
    "replicate_errors", "replicate_skipped", "replicate_table_failures",
    "reconcile_copied", "reconcile_pruned",
    "snapshot_copied", "snapshot_failures",
    "prune_stale", "prune_deleted", "prune_refused",
    "reconcile_skipped", "mirror_failures", "tee_healthy",
    "tee_probe_ms", "duration_ms",
)

# 复制延迟/追平信号 = 每 tick 的 ``replicate_copied``:游标持续吐行说明还在追赶,
# 趋近 0 说明追平了(verify 的行数差再确认收敛)。刻意不用「now - watermark_ts」——
# 游标表的 watermark_ts 是 DOUBLE 排序值、每表语义不同(chat=消息 ts、memory/
# world_book 干脆=0),不是墙钟时间戳,拿它算时间延迟既错又崩。


def _blank_summary(do_reconcile: bool) -> dict:
    return {
        "did_reconcile": do_reconcile,
        "reconcile_ok": None, "verify_ran": False, "verify_ok": None,
        "unconverged_tables": None, "unconverged_users": None, "requeue_backlog": None,
        "replicate_copied": 0, "replicate_pending": 0, "replicate_errors": 0,
        "replicate_skipped": 0, "replicate_table_failures": 0,
        "reconcile_copied": 0, "reconcile_pruned": 0,
        "reconcile_skipped": 0,
        "snapshot_copied": 0, "snapshot_failures": 0,
        "prune_stale": 0, "prune_deleted": 0, "prune_refused": 0,
        "mirror_failures": 0,
        "tee_healthy": False, "tee_probe_ms": None, "duration_ms": None,
        "report": {},
    }


def _interval() -> float:
    try:
        return max(30.0, float(os.environ.get("FEEDLING_TEE_SYNC_INTERVAL_SEC", "300") or 300))
    except (TypeError, ValueError):
        return 300.0


# per-table 整表失败退避。没有它，一张慢性失败的表（2026-07-14 prod 实测：text-cursor
# 分隔符 NUL 让 memory_moments/world_book_entries 每 tick 必败）会被每 tick 无退避地
# 全量重跑——重拉 + 重解密（enclave HTTP）同一段卡住的行，把名义 300s 的 tick 拖成
# 13-87 分钟连轴转，成为 backend 内存/CPU churn 的主源之一。连败按 2^n 指数退避、
# 封顶 _BACKOFF_CAP_SEC；成功一次即清零。仅内存态：worker 重启 = 立即重试，正确。
_BACKOFF_CAP_SEC = 3600.0
_table_backoff: dict[str, tuple[int, float]] = {}  # table -> (连败次数, monotonic 重试时点)


def _backoff_delay(fails: int) -> float:
    return min(_interval() * (2 ** max(0, fails - 1)), _BACKOFF_CAP_SEC)


def _reconcile_interval() -> float:
    try:
        return max(300.0, float(os.environ.get("FEEDLING_TEE_RECONCILE_INTERVAL_SEC", "86400") or 86400))
    except (TypeError, ValueError):
        return 86400.0


def _sync_tick(*, do_reconcile: bool) -> bool:
    """一轮同步。复用 ``tee_replication.run_action``（校验 + 单-run 锁 + confirm 门）。
    ``AlreadyRunning`` = 有手动 run 持锁 → 本 tick 跳过；``Unconfigured`` = TEE 未接 →
    跳过；其余单表错误只 log、继续下一张表。

    顺序铁律：**reconcile 必须在 replicate 之前**。密文子表（chat/memory/identity/
    world_book/frames）都有指向 ``users`` 的外键；父表没先回填，子表 replicate 会
    ``violates foreign key`` 全灭。所以先 reconcile 灌明文父表（users 等），再 replicate
    密文子表。（reconcile 走 direct-TLS 网关批量拷贝，大表可能数分钟，但本循环在专用
    后台线程、不碰请求路径。）"""
    from admin import tee_replication as tr

    t0 = time.monotonic()
    summary = _blank_summary(do_reconcile)

    # reconcile_ok 决定调用方要不要推进「下次 reconcile」计时器：reconcile 失败
    # （网关瞬时断连、SSL eof 等）时返回 False → 调用方不推进 → 下个 tick(5min)就
    # 重试，而不是傻等到一天后的日常周期。not do_reconcile 时视为 True（本 tick
    # 本就无需 reconcile，不制造重试压力）。
    reconcile_ok = not do_reconcile

    # (1) reconcile 明文表在前 —— 回填/修复父表，子表才有 FK 父行。
    if do_reconcile:
        try:
            rep = tr.run_action(action="reconcile", dry_run=False, confirm="MIGRATE")
            tbls = rep.get("tables") or []
            summary["reconcile_copied"] = sum(t.get("copied", 0) for t in tbls if isinstance(t, dict))
            summary["reconcile_pruned"] = sum(t.get("pruned", 0) for t in tbls if isinstance(t, dict))
            summary["reconcile_skipped"] = sum(t.get("skipped", 0) for t in tbls if isinstance(t, dict))
            summary["report"]["reconcile"] = tbls
            unconv = [t.get("table") for t in tbls
                      if isinstance(t, dict) and t.get("rds_rows") != t.get("tee_rows")]
            log.info("[tee-sync] reconcile done: copied=%s unconverged=%s",
                     summary["reconcile_copied"], unconv or "none")
            reconcile_ok = True
            # Stamp completion NOW — before the slow replicate/verify below. If the
            # worker is max_requests-recycled mid-replicate, reconcile still counts
            # as done, so the next leader runs replicate-only ticks instead of
            # redoing reconcile-first and re-starving replicate (2026-07-15 prod).
            db.mark_reconcile_success()
        except tr.AlreadyRunning:
            reconcile_ok = True  # 别人（手动 run）在跑 → 不重试风暴
        except tr.Unconfigured:
            return True  # TEE 未接，无事可做（不落指标行——无从探活）
        except Exception as e:  # noqa: BLE001
            log.warning("[tee-sync] reconcile 失败: %s", e)  # reconcile_ok 保持 False → 尽快重试
    summary["reconcile_ok"] = reconcile_ok if do_reconcile else None

    # (1.5) snapshot 明文小表 —— 在 reconcile 之后（FK 父表已在）、replicate 之前
    # （不被慢的密文复制饿死；snapshot 无 enclave 往返，是整个 tick 里最便宜的一段）。
    try:
        rep = tr.run_action(action="snapshot", dry_run=False, confirm="MIGRATE")
        summary["snapshot_copied"] = rep.get("copied") or 0
        summary["snapshot_failures"] = rep.get("failures") or 0
        summary["report"]["snapshot"] = rep.get("tables") or []
        if summary["snapshot_failures"]:
            failed = [t.get("table") for t in (rep.get("tables") or [])
                      if isinstance(t, dict) and not t.get("ok")]
            log.warning("[tee-sync] snapshot 有 %d 张表失败: %s",
                        summary["snapshot_failures"], failed)
        else:
            log.info("[tee-sync] snapshot done: copied=%s", summary["snapshot_copied"])
    except tr.AlreadyRunning:
        log.info("[tee-sync] 手动 run 持锁中 — 跳过本 tick 的 snapshot")
    except tr.Unconfigured:
        return reconcile_ok
    except Exception as e:  # noqa: BLE001 — 影子期铁律：绝不传染主路径
        log.warning("[tee-sync] snapshot 失败: %s", e)

    # (2) replicate 密文子表在后 —— 父表已在，不再 FK 失败。
    for table in _replicable_tables():
        fails, retry_at = _table_backoff.get(table, (0, 0.0))
        if retry_at > time.monotonic():
            # 连败退避中 → 本 tick 跳过这张表（其余表照常），到点自动恢复重试。
            summary["report"].setdefault("replicate_backoff", []).append(table)
            log.info("[tee-sync] replicate %s 退避中(连败%d) — 跳过本 tick", table, fails)
            continue
        try:
            rep = tr.run_action(action="replicate", table=table, dry_run=False, confirm="MIGRATE")
            _table_backoff.pop(table, None)  # 整表成功 → 退避清零
            summary["replicate_copied"] += rep.get("copied") or 0
            summary["replicate_pending"] += rep.get("pending") or 0
            summary["replicate_errors"] += rep.get("errors") or 0
            summary["replicate_skipped"] += rep.get("skipped") or 0
            summary["report"].setdefault("replicate", {})[table] = {
                k: rep.get(k) for k in
                ("copied", "pending", "errors", "skipped", "quarantined",
                 "watermark_ts", "watermark_id")}
            if rep.get("copied") or rep.get("pending") or rep.get("errors") or rep.get("quarantined"):
                log.info("[tee-sync] replicate %s: copied=%s pending=%s errors=%s quarantined=%s",
                         table, rep.get("copied"), rep.get("pending"), rep.get("errors"),
                         rep.get("quarantined"))
        except tr.AlreadyRunning:
            # 手动 run 持锁 → 本 tick 的活由它在干,不落一行半吊子指标(会污染趋势)。
            log.info("[tee-sync] 手动复制 run 持锁中 — 跳过本 tick")
            return reconcile_ok
        except tr.Unconfigured:
            return reconcile_ok
        except Exception as e:  # noqa: BLE001
            # 整表 replicate 抛错(常见:TEE direct-TLS 连接掉线 "unexpected eof" /
            # "connection is lost")。别只 log——记进 summary,否则这张整表失败会从
            # report 和 replicate_errors(只统计成功 run 的逐行错)里双双消失。
            summary["replicate_table_failures"] += 1
            summary["report"].setdefault("replicate_failed", {})[table] = str(e)[:200]
            fails += 1
            _table_backoff[table] = (fails, time.monotonic() + _backoff_delay(fails))
            log.warning("[tee-sync] replicate %s 失败(连败%d, 退避%.0fs): %s",
                        table, fails, _backoff_delay(fails), e)

    # (2.5) prune 密文表的残留行 —— 删除传播的兜底对账（tee_shadow/ciphertext_prune）。
    #
    # 只在 reconcile 档跑（默认每天一次），不是每 tick：它要两侧全量扫主键
    # （prod 实测 12 万个键、几十秒），跟 5 分钟的 replicate 档不是一个量级。
    #
    # 放在 replicate 之后、verify 之前：先让本 tick 该搬的都搬完，再删残留，
    # 最后由 verify 量到 prune 之后的真实收敛度。（prune 自身的正确性不依赖这个
    # 顺序——它靠的是模块内那条"先 TEE 后 RDS"的取数铁律。）
    if do_reconcile and reconcile_ok:
        try:
            from tee_shadow import ciphertext_prune

            rep = ciphertext_prune.prune_all()
            summary["prune_stale"] = rep.get("stale") or 0
            summary["prune_deleted"] = rep.get("deleted") or 0
            summary["prune_refused"] = len(rep.get("refused") or [])
            summary["report"]["prune"] = rep
            if rep.get("refused"):
                log.error("[tee-sync] prune 有 %d 张表因超过安全阈值被拒: %s",
                          len(rep["refused"]), rep["refused"])
            if summary["prune_deleted"] or summary["prune_stale"]:
                log.info("[tee-sync] prune done: stale=%s deleted=%s errors=%s",
                         summary["prune_stale"], summary["prune_deleted"],
                         rep.get("errors"))
        except Exception as e:  # noqa: BLE001 — 影子期铁律：绝不传染主路径
            log.warning("[tee-sync] prune 失败: %s", e)

    # (3) verify 对账 —— reconcile 成功才有意义;这是收敛度的量测来源。
    if do_reconcile and reconcile_ok:
        try:
            rep = tr.run_action(action="verify", dry_run=False)
            tables = rep.get("tables") or {}
            summary["verify_ran"] = True
            summary["verify_ok"] = bool(rep.get("ok"))
            unconv_tbls = [k for k, v in tables.items()
                           if isinstance(v, dict) and not v.get("rows_ok", True)]
            summary["unconverged_tables"] = len(unconv_tbls)
            summary["unconverged_users"] = sum(
                len(v.get("user_diffs") or {}) for v in tables.values() if isinstance(v, dict))
            summary["requeue_backlog"] = sum(
                v.get("requeue_backlog", 0) or 0 for v in tables.values() if isinstance(v, dict))
            summary["report"]["verify"] = {
                "ok": rep.get("ok"), "unconverged": unconv_tbls,
                "mismatches": len(rep.get("mismatches") or []), "tables": tables}
            log.info("[tee-sync] verify ok=%s unconverged_tables=%s unconverged_users=%s",
                     summary["verify_ok"], summary["unconverged_tables"], summary["unconverged_users"])
        except Exception as e:  # noqa: BLE001
            log.warning("[tee-sync] verify 失败: %s", e)

    # (4) 健康探活 + 游标延迟 + 双写失败计数 + 耗时 → 落一行历史 + 一行结构化日志。
    summary["mirror_failures"] = mirror.failure_count()
    health = mirror.probe()
    summary["tee_healthy"] = bool(health.get("ok"))
    summary["tee_probe_ms"] = health.get("latency_ms")
    summary["duration_ms"] = round((time.monotonic() - t0) * 1000, 1)
    summary["report"]["health"] = health
    try:
        db.record_tee_sync_run(summary)
    except Exception as e:  # noqa: BLE001 — 落库失败不影响同步/循环
        log.warning("[tee-sync] 指标落库失败: %s", e)
    log.info("[tee-sync] tick %s",
             json.dumps({k: summary.get(k) for k in _LOG_KEYS}, default=str, ensure_ascii=False))
    return reconcile_ok


def _should_reconcile(last_reconcile: float | None, now: float) -> bool:
    """首个 tick（``last_reconcile is None``）**必** reconcile —— 先把明文父表
    （users 等）基线灌进 TEE，否则子表的双写/复制全撞 users 外键。

    绝不能靠「``monotonic()`` 起点必 > reconcile 间隔」来触发首轮:宿主 uptime <
    间隔（86400s=1天;刚部署的 CVM 就是）时 ``monotonic()`` 很小、首 tick 不 reconcile，
    users 基线一直不灌 → FK 全线失败烧日志（2026-07-14 prod 实测,dual-write 开着但
    reconcile 从没跑过）。用 None 哨兵与 monotonic 的绝对值解耦。之后按 reconcile 间隔。"""
    return last_reconcile is None or (now - last_reconcile) >= _reconcile_interval()


def _restore_last_reconcile() -> float | None:
    """从 tee_sync_runs 恢复「上次成功 reconcile」的时点（换算到本进程 monotonic 轴）。

    last_reconcile 不能只活在内存里：gunicorn max_requests 回收 leader worker 后，
    新 leader 若从 None 起步就重做 reconcile-first——完整 reconcile 要数十分钟，
    worker 寿命一短它就永远跑不完（2026-07-14 test 实测：部署后 2h leader 反复换手、
    tee_sync_runs 零新行）。从 DB 恢复后，只有真到 reconcile 间隔才重跑；真正的
    首次基线（库里没有任何成功 reconcile）仍然 reconcile-first。DB 读失败兜底回
    None = 现状语义。"""
    try:
        age = db.last_tee_reconcile_age_sec()
        if age is None:
            return None
        return time.monotonic() - age
    except Exception as e:  # noqa: BLE001 — 恢复失败不阻断循环
        log.warning("[tee-sync] last_reconcile 恢复失败(退回 reconcile-first): %s", e)
        return None


def _loop() -> None:
    # 先从 DB 恢复上次成功 reconcile 时点（见 _restore_last_reconcile）；库里从没
    # 成功过 → None → 首个成功 tick 必 reconcile 建立基线（见 _should_reconcile）。
    last_reconcile: float | None = _restore_last_reconcile()
    first = True
    while True:
        # 首个 tick 只等一小会儿就跑 —— 尽快把明文父表回填上，缩短「父表未回填 →
        # 子表双写 FK 失败」的启动窗口；之后按整间隔。
        time.sleep(_FIRST_DELAY if first else _interval())
        first = False
        if not mirror.enabled():
            continue
        now = time.monotonic()
        do_reconcile = _should_reconcile(last_reconcile, now)
        try:
            reconcile_ok = _sync_tick(do_reconcile=do_reconcile)
            # 仅在 reconcile 真成功时推进计时器；失败(断连等)则保持不动，下个 tick
            # 就重试，而不是等一整个 reconcile 周期。
            if do_reconcile and reconcile_ok:
                last_reconcile = now
        except Exception as e:  # noqa: BLE001 — 循环绝不能死
            log.warning("[tee-sync] tick 错误: %s", e)


def start() -> None:
    """Spawn 同步循环线程并立即返回（照 screen.ws.start）。由 assembly 层经
    ``core.leader.run_singleton("tee-sync", ...)`` 调用，保证只一个 worker 跑。"""
    threading.Thread(target=_loop, daemon=True, name="tee-sync").start()
