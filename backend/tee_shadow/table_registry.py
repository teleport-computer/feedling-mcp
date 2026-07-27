"""RDS 表 → TEE 同步 lane 的单一真源。

背景：TEE 同步原本是三层手工白名单登记制——DDL 手写 alembic_tee revision、
数据流分别登记在 db.py 的 mirror 写点 / tee_replicator.worker._TABLES /
tee_shadow.reconciler.TABLES。三处互不校验，谁都不是全集，所以 Runtime V2 的
19 张新表可以一张都没登记而无人发现（2026-07-27 实测 RDS 61 张 / TEE 20 张）。

本模块是那个"全集"。规则：**每一张 RDS 表必须有且只有一条登记**，由
tests/test_tee_table_registry.py 强制。加了 RDS 表却没登记 lane 的改动合不进去。

lane 语义见下面常量的注释。注册表是纯数据 + 查询 helper，不做任何 I/O——它被
scheduler、verify、snapshot 三处消费，必须能在任何上下文里安全 import。
"""
from __future__ import annotations

from dataclasses import dataclass

# 热路径双写：写主库的同时经 tee_shadow.mirror.execute 尽力而为地写 TEE。
# 适用于低频写的明文运维表。
MIRROR = "MIRROR"

# 游标驱动的密文→明文复制：tee_replicator.worker 拉 RDS 密文行、过 enclave
# 解密、写 TEE 明文行。适用于装信封的表。
CIPHERTEXT = "CIPHERTEXT"

# 整表快照刷：TRUNCATE + COPY 原子替换（tee_shadow.snapshot）。适用于数据量
# 小、但有 UPDATE/DELETE 的明文表——全量替换天然处理可变行，不需要 requeue
# 补偿和 prune。
SNAPSHOT = "SNAPSHOT"

# 不同步。理由必填且必须具体（守卫会拒绝"暂不需要"这类含糊理由）。
SKIP = "SKIP"

# PG 原生逻辑复制。**预留，尚未实现**：需要把 RDS 的 rds.logical_replication
# 打开（当前 off、wal_level=replica）并重启实例，属独立运维窗口。通道打通后，
# 把表从 SNAPSHOT 改成 LOGICAL 即可，不需要重做机制——这正是这个空 lane 存在
# 的意义。守卫强制它保持为空。
LOGICAL = "LOGICAL"

LANES = (MIRROR, CIPHERTEXT, SNAPSHOT, SKIP, LOGICAL)


@dataclass(frozen=True)
class Entry:
    lane: str
    reason: str
    # True = 这张表不由 alembic 迁移链创建（人工 SQL 建的，如一次性备份表）。
    # 守卫据此放行"注册表里有、迁移链里没有"的条目；不标就当成表名打错。
    manual: bool = False


REGISTRY: dict[str, Entry] = {
    # Task 3 填充。
}


def tables_in_lane(lane: str) -> tuple[str, ...]:
    """某条 lane 下的表名，按字典序。"""
    return tuple(sorted(t for t, e in REGISTRY.items() if e.lane == lane))


def synced_tables() -> tuple[str, ...]:
    """所有会进 TEE 的表（即非 SKIP），按字典序。"""
    return tuple(sorted(t for t, e in REGISTRY.items() if e.lane != SKIP))
