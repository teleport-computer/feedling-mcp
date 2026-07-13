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

import logging
import os
import threading
import time

from tee_shadow import mirror

log = logging.getLogger("feedling.tee_sync")

# 密文表 —— 经 enclave 解密成明文。与 tee_replicator.worker._TABLES 对齐。
_CIPHERTEXT_TABLES = (
    "chat_messages",
    "memory_moments",
    "world_book_entries",
    "identity",
    "frame_envelopes",
)


def _interval() -> float:
    try:
        return max(30.0, float(os.environ.get("FEEDLING_TEE_SYNC_INTERVAL_SEC", "300") or 300))
    except (TypeError, ValueError):
        return 300.0


def _reconcile_interval() -> float:
    try:
        return max(300.0, float(os.environ.get("FEEDLING_TEE_RECONCILE_INTERVAL_SEC", "86400") or 86400))
    except (TypeError, ValueError):
        return 86400.0


def _sync_tick(*, do_reconcile: bool) -> None:
    """一轮同步。复用 ``tee_replication.run_action``（校验 + 单-run 锁 + confirm 门）。
    ``AlreadyRunning`` = 有手动 run 持锁 → 本 tick 跳过；``Unconfigured`` = TEE 未接 →
    跳过；其余单表错误只 log、继续下一张表。"""
    from admin import tee_replication as tr

    for table in _CIPHERTEXT_TABLES:
        try:
            rep = tr.run_action(action="replicate", table=table, dry_run=False, confirm="MIGRATE")
            if rep.get("copied") or rep.get("pending") or rep.get("errors"):
                log.info("[tee-sync] replicate %s: copied=%s pending=%s errors=%s",
                         table, rep.get("copied"), rep.get("pending"), rep.get("errors"))
        except tr.AlreadyRunning:
            log.info("[tee-sync] 手动复制 run 持锁中 — 跳过本 tick")
            return
        except tr.Unconfigured:
            return
        except Exception as e:  # noqa: BLE001
            log.warning("[tee-sync] replicate %s 失败: %s", table, e)

    if not do_reconcile:
        return
    try:
        tr.run_action(action="reconcile", dry_run=False, confirm="MIGRATE")
    except (tr.AlreadyRunning, tr.Unconfigured):
        return
    except Exception as e:  # noqa: BLE001
        log.warning("[tee-sync] reconcile 失败: %s", e)
    try:
        rep = tr.run_action(action="verify", dry_run=False)
        log.info("[tee-sync] verify ok=%s", rep.get("ok"))
    except Exception as e:  # noqa: BLE001
        log.warning("[tee-sync] verify 失败: %s", e)


def _loop() -> None:
    # monotonic()，起点非 0 → 首个 tick 的 (now - 0) 必然 >= 间隔 → 首轮就 reconcile
    # （初始回填明文表 + 游标从头搬密文）。
    last_reconcile = 0.0
    while True:
        time.sleep(_interval())
        if not mirror.enabled():
            continue
        now = time.monotonic()
        do_reconcile = (now - last_reconcile) >= _reconcile_interval()
        try:
            _sync_tick(do_reconcile=do_reconcile)
            if do_reconcile:
                last_reconcile = now
        except Exception as e:  # noqa: BLE001 — 循环绝不能死
            log.warning("[tee-sync] tick 错误: %s", e)


def start() -> None:
    """Spawn 同步循环线程并立即返回（照 screen.ws.start）。由 assembly 层经
    ``core.leader.run_singleton("tee-sync", ...)`` 调用，保证只一个 worker 跑。"""
    threading.Thread(target=_loop, daemon=True, name="tee-sync").start()
