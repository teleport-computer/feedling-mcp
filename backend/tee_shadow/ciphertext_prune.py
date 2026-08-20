"""CIPHERTEXT lane 的删除传播兜底：两侧主键集合差集，删掉 TEE 的残留行。

## 为什么需要它

CIPHERTEXT lane 的复制是**只追加的游标扫描**（`tee_replicator.worker.run_table`）。
删除完全靠两条路径传播：

1. 热路径双写 —— `db.chat_clear` 等走 `mirror.execute_many` 删 TEE 行；
2. requeue lane —— 写侧落 `tee_pending_device_migration` 标记，
   `_consume_requeue` 发现 RDS 行没了就删 TEE 行。

两条都可能漏：`mirror.execute` 按影子期铁律**吞掉一切异常**（它必须如此，
否则影子库的故障会传染主路径），而热路径删除失败时并不会补落 requeue 标记。
一旦漏掉，那行就永久留在 TEE —— 游标只会前进，绝不会回头重看它。

2026-07-29 prod 实测：11 张 CIPHERTEXT 表里 **853 行残留，全部在
`chat_messages`**，涉及 13 个用户，其中 840 行（98%）能在
`chat_message_archive` 里找到对应记录 —— 即用户主动 clear 过、RDS 已经删了、
TEE 没跟上。只有 `chat_messages` 中招是有道理的：它是唯一有**批量删除热路径**
的表（一次 clear 删掉一个用户的全部消息），一次 `mirror.execute_many` 失败就
留下几百行；其余表的删除是零星的、且多数有 requeue 补偿。

这不是"多了几行无害数据"——用户以为删掉的明文对话还留在影子库里。

## 顺序铁律：先查 TEE，再查 RDS

**这个顺序是正确性的全部，不是性能偏好。反过来会永久丢数据。**

设 TEE 快照时刻 T1、RDS 快照时刻 T2，本模块保证 T1 < T2。

若某行 r 在 T1 时已在 TEE，则 replicator 必然在 T1 之前写入了它；而 replicator
只搬 RDS 中存在的行，所以 r 在 T1 之前就存在于 RDS。于是 T2 时 r 若不在 RDS，
只可能是 (T1, T2) 之间被删了 —— 删掉 TEE 侧那行正是我们要做的事。

反序（先 RDS 后 TEE）则有一个真实的误删窗口：r 在 T1(RDS 快照) 之后才写入
RDS，replicator 又恰好在 T2(TEE 快照) 之前把它搬进 TEE，于是
`r ∈ TEE 快照` 而 `r ∉ RDS 快照` → 被判为残留删掉。而**游标早已越过 r，
它永远不会被重新搬回来**，这行用户数据就此从影子库消失。

`tee_shadow.reconciler` 的 prune 是反序的（先 src 后 tee），那里可以接受：
MIRROR lane 每轮 reconcile 都会重新全表 copy 一遍，误删的行下一轮就补回来。
CIPHERTEXT lane 没有这个后悔药。

## 安全阈值

单表单次删除超过 `max(_MIN_ABS_GUARD, TEE 行数 × _MAX_FRACTION)` 就**整表放弃**
并告警，不删任何一行。防的是"RDS 侧查询异常返回了空集/残缺集"这类故障被当成
"用户删了所有数据"，进而清空 TEE。宁可让残留多留一天等人来看，也不要自动
执行一次大规模不可逆删除。

## 节奏

挂在 `tee_sync_scheduler` 的 reconcile 档（默认每天一次），不是每 tick。成本是
两侧全量主键扫描（prod 实测 12 万个键、几十秒），与 reconcile 同档匹配。
"""
from __future__ import annotations

import logging
import os
import time

import db
import psycopg
from tee_shadow import mirror

log = logging.getLogger("feedling.tee_shadow")

# 安全阈值：删除量超过 max(绝对下限, TEE 行数 × 比例) → 整表放弃。
# 绝对下限存在的意义：小表（v2_conversation_summary 只有 6 行）按比例算出来的
# 阈值太小，一次正常的账号删除就会触发放弃。
_MIN_ABS_GUARD = 2_000
_MAX_FRACTION = 0.10

# 一次 DELETE 批的大小。逐行 DELETE 走 direct-TLS 网关时每行一次往返，
# executemany 让 psycopg3 把整批压成一次交换（同 reconciler 的批量 upsert）。
_DELETE_BATCH = 500

_RETRY_BACKOFF_SEC = 1.0

# Prune deletion is a distinct typed contract from the requeue/terminal lane.
# In particular trajectory pending uses item_id="job:event", while pruning uses
# the real composite key (job_id,event_index); sharing one SQL slot is unsafe.
_PRUNE_DELETE_SQL = {
    "chat_messages": "DELETE FROM chat_messages WHERE user_id = %s AND msg_id = %s",
    "memory_moments": "DELETE FROM memory_moments WHERE user_id = %s AND moment_id = %s",
    "world_book_entries": "DELETE FROM world_book_entries WHERE user_id = %s AND entry_id = %s",
    "identity": "DELETE FROM user_blobs WHERE user_id = %s AND kind = 'identity'",
    "frame_envelopes": "DELETE FROM frames WHERE user_id = %s AND frame_id = %s",
    "v2_trajectory_events": "DELETE FROM v2_trajectory_events WHERE job_id = %s AND event_index = %s",
    "voice_transcripts": "DELETE FROM voice_transcripts WHERE user_id = %s AND call_id = %s",
    "model_api_credentials": "DELETE FROM model_api_credentials WHERE user_id = %s AND id = %s",
    "v2_conversation_summary": "DELETE FROM v2_conversation_summary WHERE user_id = %s",
    "v2_trajectory_reviews": "DELETE FROM v2_trajectory_reviews WHERE user_id = %s AND source_job_id = %s",
    "v2_workspace_entries": "DELETE FROM v2_workspace_entries WHERE user_id = %s AND path = %s",
}


def _conn_retries() -> int:
    try:
        return max(1, int(os.environ.get("FEEDLING_TEE_PRUNE_CONN_RETRIES", "3") or 3))
    except (TypeError, ValueError):
        return 3


def _with_conn_retry(pool_getter, work, *, label: str):
    """取一条连接跑 ``work(conn)``；连接断则换新连接重试（有界 + 小退避）。

    2026-07-29 prod dry-run 实测的必要性：`chat_messages` 要拉两侧共 16 万个主键，
    耗时以分钟计；这期间池里其它连接一直空闲，Phala 网关会静默掐断它们。结果是
    **第一张表成功之后，其余 8 张表连锁 `SSL SYSCALL error: EOF detected` 全灭**。

    这与 `tee_replicator.worker._flush_batch` 治的是同一个病（2026-07-14 test 根因：
    direct-TLS 经网关掉线让 chat/memory 整表挂），故沿用同样的判定与对策：
    `psycopg.OperationalError` 或连接已 broken/closed = 连接级故障，换连接重试有意义。

    重试对本模块的两种 work 都是安全的：取数是纯读；DELETE 按主键、天然幂等，
    重放一批已删过的行是 no-op。
    """
    attempts = _conn_retries()
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with pool_getter().connection() as conn:
                return work(conn)
        except psycopg.OperationalError as e:
            last = e
            log.warning("[tee-prune] %s: 连接故障(第 %d/%d 次): %s",
                        label, attempt, attempts, e)
            if attempt < attempts:
                time.sleep(_RETRY_BACKOFF_SEC * attempt)
    raise last  # type: ignore[misc]


def prunable_tables() -> tuple[str, ...]:
    """配了 prune 三件套（两条 keys SQL + 一条 DELETE）的表，字典序。

    从 `worker._TABLES` 派生而不是另立清单——本仓库已经因为"手工清单漏登记"
    吃过多次亏（2026-07-27 之前 V2 的 19 张表一处都没登记）。想让一张表进
    prune，就在它的 `_Table` 上填那两条 SQL，这里自动吸收。
    """
    from tee_replicator import worker as tee_worker

    return tuple(sorted(
        t for t, cfg in tee_worker._TABLES.items()
        if cfg.prune_rds_keys_sql and cfg.prune_tee_keys_sql
        and t in _PRUNE_DELETE_SQL
    ))


def unprunable_tables() -> tuple[str, ...]:
    """CIPHERTEXT lane 里**没有**接 prune 的表，字典序。

    单独暴露出来是为了让"没覆盖"这件事进报告、可被看见。静默的覆盖缺口正是
    这套机制要根治的问题，prune 自己不能再制造一个。
    """
    from tee_replicator import worker as tee_worker

    covered = set(prunable_tables())
    return tuple(sorted(t for t in tee_worker._TABLES if t not in covered))


def prune_table(
    table: str,
    *,
    dry_run: bool = False,
    expected_stale: int | None = None,
) -> dict:
    """对一张表做两侧主键差集，删掉 TEE 侧的残留行。绝不抛——失败落在返回值里。"""
    from tee_replicator import worker as tee_worker

    rep: dict = {
        "table": table, "rds_rows": 0, "tee_rows": 0,
        "stale": 0, "deleted": 0, "error": None, "refused": None,
    }
    cfg = tee_worker._TABLES.get(table)
    if cfg is None:
        rep["error"] = f"{table} 不在 replicator 的表配置里"
        return rep
    delete_sql = _PRUNE_DELETE_SQL.get(table)
    if not (cfg.prune_rds_keys_sql and cfg.prune_tee_keys_sql and delete_sql):
        rep["error"] = f"{table} 没有配 prune（缺 keys SQL 或 DELETE）"
        return rep

    try:
        # ⚠️ 顺序铁律：TEE 在前、RDS 在后。理由见模块 docstring —— 反过来会在
        # 复制窗口里误删刚搬进 TEE 的新行，且游标已越过、永不恢复。
        # 两个 with 分开写（不是 `with a() as x, b() as y`）是为了让这个先后
        # 关系在代码结构上是显式的，改动的人一眼能看到它有序。
        tee_keys = _with_conn_retry(
            mirror.get_tee_pool,
            lambda c: {tuple(r) for r in c.execute(cfg.prune_tee_keys_sql)},
            label=f"{table} tee-keys")
        rds_keys = _with_conn_retry(
            db.get_pool,
            lambda c: {tuple(r) for r in c.execute(cfg.prune_rds_keys_sql)},
            label=f"{table} rds-keys")
    except Exception as e:  # noqa: BLE001 — 影子期铁律：绝不传染主路径
        rep["error"] = str(e)[:200]
        log.warning("[tee-prune] %s: 取主键集合失败: %s", table, e)
        return rep

    rep["rds_rows"] = len(rds_keys)
    rep["tee_rows"] = len(tee_keys)
    stale = tee_keys - rds_keys
    rep["stale"] = len(stale)
    if not stale:
        return rep

    # 安全阈值：宁可让残留多留一天等人来看，也不要自动做一次大规模不可逆删除。
    guard = max(_MIN_ABS_GUARD, int(len(tee_keys) * _MAX_FRACTION))
    exact_override = expected_stale is not None and expected_stale == len(stale)
    if len(stale) > guard and not exact_override:
        rep["refused"] = (
            f"残留 {len(stale)} 行超过阈值 {guard}（TEE {len(tee_keys)} 行）——"
            f"整表放弃，不删任何一行。先 dry-run 确认后用精确 expected_stale 重试")
        log.error("[tee-prune] %s: %s", table, rep["refused"])
        return rep

    if dry_run:
        return rep

    keys = sorted(stale)

    def _delete_batch(conn, batch):
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(delete_sql, batch)

    try:
        # 每批各自取连接：一批之间的间隔足以让网关掐断长期持有的那条
        # （同 _with_conn_retry 的 docstring）。DELETE 按主键幂等，重放安全。
        for i in range(0, len(keys), _DELETE_BATCH):
            batch = keys[i:i + _DELETE_BATCH]
            _with_conn_retry(mirror.get_tee_pool,
                             lambda c, b=batch: _delete_batch(c, b),
                             label=f"{table} delete[{i}:{i + len(batch)}]")
            rep["deleted"] += len(batch)
    except Exception as e:  # noqa: BLE001
        rep["error"] = str(e)[:200]
        log.warning("[tee-prune] %s: 删除失败（已删 %d 行）: %s",
                    table, rep["deleted"], e)
        return rep

    log.info("[tee-prune] %s: 删掉 %d 行残留（TEE %d / RDS %d）",
             table, rep["deleted"], rep["tee_rows"], rep["rds_rows"])
    return rep


def prune_all(*, dry_run: bool = False) -> dict:
    """跑完所有配了 prune 的表。单表失败不影响其余表。"""
    tables = [prune_table(t, dry_run=dry_run) for t in prunable_tables()]
    return {
        "tables": tables,
        "stale": sum(t["stale"] for t in tables),
        "deleted": sum(t["deleted"] for t in tables),
        "errors": sum(1 for t in tables if t["error"]),
        "refused": [t["table"] for t in tables if t["refused"]],
        # 未覆盖的表进报告——静默的覆盖缺口正是本机制要根治的东西。
        "uncovered": list(unprunable_tables()),
    }
