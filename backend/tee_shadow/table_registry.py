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
    # ---------------------------------------------------------------- #
    # MIRROR —— 明文运维表，热路径双写（db.py 的 mirror.execute 写点）。
    # 这 13 张是 alembic_tee 0001 baseline 的"13 张明文运维表"，加 0002 的
    # notify_relay 两张。列定义与 RDS 逐列对齐。
    # ---------------------------------------------------------------- #
    "server_config": Entry(MIRROR, "全局配置，低频写；reconciler.TABLES 已覆盖"),
    "global_blobs": Entry(MIRROR, "全局 blob，低频写；reconciler.TABLES 已覆盖"),
    "users": Entry(MIRROR, "账号父表，所有 per-user 表的 FK 目标，必须最先同步"),
    "user_blobs": Entry(MIRROR, "per-user 杂项；kind='identity' 归 CIPHERTEXT、"
                                "kind='consumer_state' 有意不镜像（见 reconciler._SCOPE_WHERE）"),
    "user_logs": Entry(MIRROR, "per-user 日志流；seq 是 IDENTITY 列，靠 OVERRIDING SYSTEM VALUE 搬"),
    "perception_items": Entry(MIRROR, "感知条目，明文；reconciler.TABLES 已覆盖"),
    "perception_daily": Entry(MIRROR, "感知日聚合，明文；reconciler.TABLES 已覆盖"),
    "copytext_strings": Entry(MIRROR, "文案表，明文；reconciler.TABLES 已覆盖"),
    "copytext_meta": Entry(MIRROR, "文案版本哨兵单行表；reconciler.TABLES 已覆盖"),
    "genesis_import_jobs": Entry(MIRROR, "入住导入作业元数据，明文；reconciler.TABLES 已覆盖"),
    "genesis_import_outputs": Entry(MIRROR, "入住导入产物，明文；reconciler.TABLES 已覆盖"),
    "agent_runtime_instances": Entry(MIRROR, "runtime 实例登记，明文运维表"),
    "agent_runtime_supervisor_heartbeats": Entry(MIRROR, "supervisor 心跳，明文运维表"),
    "notify_relay_configs": Entry(MIRROR, "自部署推送中继配置；alembic_tee 0002 已建表"),
    "notify_relay_logs": Entry(MIRROR, "推送中继日志；id 是 IDENTITY 列"),

    # ---------------------------------------------------------------- #
    # CIPHERTEXT —— 装信封的表，经 enclave 解密成明文写进 TEE。
    # ---------------------------------------------------------------- #
    "chat_messages": Entry(CIPHERTEXT, "对话正文信封 doc；worker._TABLES 已覆盖"),
    "memory_moments": Entry(CIPHERTEXT, "记忆信封 doc；worker._TABLES 已覆盖"),
    "world_book_entries": Entry(CIPHERTEXT, "世界书信封 doc；worker._TABLES 已覆盖"),
    "frame_envelopes": Entry(
        CIPHERTEXT,
        "帧信封；TEE 侧对应物是形状不同的 frames（R2 存储层指针），由 worker "
        "的 frames row_writer 处理，不是同名表",
    ),
    # 以下 7 张是 2026-07-27 全量对齐新增（用户拍板解密成明文存）。
    "chat_message_archive": Entry(
        CIPHERTEXT,
        "归档对话，doc 与 chat_messages.doc 完全同形（prod 897 行中 896 行是完整"
        "信封、1 行是 R2 offload 指针需先水合 body_ct）；复用 plaintext_chat_doc",
    ),
    "v2_trajectory_events": Entry(
        CIPHERTEXT,
        "V2 轨迹事件 payload_envelope；表级 CHECK ck_v2_trajectory_envelope 强制 "
        "K_enclave + visibility='shared'，故服务端可解",
    ),
    "model_api_credentials": Entry(
        CIPHERTEXT,
        "BYOK provider key 信封 api_key_envelope。2026-07-27 用户拍板解密成明文存"
        "（设计文档 §8 已记录其安全含义：TEE owner 角色可读全库）",
    ),
    "v2_conversation_summary": Entry(CIPHERTEXT, "V2 对话摘要 summary_envelope，标准单信封"),
    "v2_conversation_summary_segments": Entry(CIPHERTEXT, "V2 摘要分段 summary_envelope，标准单信封"),
    "v2_trajectory_reviews": Entry(
        CIPHERTEXT,
        "V2 轨迹复核 review_envelope（可空，CHECK 允许 NULL）；prod 当前 0 行",
    ),
    "v2_workspace_entries": Entry(CIPHERTEXT, "V2 工作区条目 content_envelope；prod 当前 0 行"),

    # ---------------------------------------------------------------- #
    # SNAPSHOT —— 明文、数据量小、但有 UPDATE/DELETE 的表。整表原子替换。
    # 已实测确认这批表的 jsonb 列（payload_json / result_json / detail_json /
    # state_json / actions_json / v2_effect_outbox.payload）装的是明文，不含信封。
    # ---------------------------------------------------------------- #
    "agent_action_queue": Entry(SNAPSHOT, "动作队列，行状态流转（UPDATE 密集），明文 payload_json"),
    "agent_jobs": Entry(SNAPSHOT, "agent 作业表，status 流转，明文"),
    "agent_status_events": Entry(SNAPSHOT, "agent 状态事件，明文 detail_json"),
    "chat_r2_cleanup": Entry(SNAPSHOT, "R2 清理队列，行会被删，明文"),
    "chat_r2_lifecycle": Entry(SNAPSHOT, "R2 生命周期状态，UPDATE 密集，明文"),
    "dau_daily_snapshot": Entry(SNAPSHOT, "DAU 日快照，每日批量写，明文"),
    "model_api_routes": Entry(SNAPSHOT, "BYOK 路由配置（不含凭证，凭证在 model_api_credentials），明文"),
    "provider_health": Entry(SNAPSHOT, "provider 健康状态，UPDATE 密集，明文"),
    "retention_cohort_snapshot": Entry(SNAPSHOT, "留存 cohort 快照，批量写，明文"),
    "runtime_state": Entry(SNAPSHOT, "runtime 状态 state_json，UPDATE 密集，明文"),
    "user_growth_daily_snapshot": Entry(SNAPSHOT, "增长日快照，批量写，明文"),
    "v2_capture_batches": Entry(SNAPSHOT, "V2 捕获批次 actions_json，status 流转，明文"),
    "v2_effect_outbox": Entry(SNAPSHOT, "V2 效果 outbox，投递后删行，明文 payload（实测非信封）"),
    "v2_effect_sink_applied": Entry(SNAPSHOT, "V2 效果幂等标记，明文"),
    "v2_mcp_mutation_attempts": Entry(SNAPSHOT, "V2 MCP 变更尝试记录，明文"),
    "v2_runtime_control": Entry(SNAPSHOT, "V2 运行时总控单行表，明文"),
    "v2_runtime_state": Entry(SNAPSHOT, "V2 per-user 运行时 fence，UPDATE 密集，明文"),
    "v2_sandbox_usage_events": Entry(SNAPSHOT, "V2 sandbox 用量事件，明文"),
    "v2_terminal_failure_outbox": Entry(SNAPSHOT, "V2 终态失败 outbox，投递后删行，明文"),
    "v2_trajectory_access_audit": Entry(SNAPSHOT, "V2 轨迹访问审计（审计元数据本身是明文）"),
    "v2_trajectory_streams": Entry(SNAPSHOT, "V2 轨迹流游标，UPDATE 密集，明文"),
    "v2_turn_metrics": Entry(SNAPSHOT, "V2 回合指标，明文"),
    "v2_user_allowlist": Entry(SNAPSHOT, "V2 灰度名单，UPDATE 密集，明文"),
    "v2_wake_schedule": Entry(SNAPSHOT, "V2 唤醒排程，UPDATE 密集，明文"),
    "v2_worker_heartbeats": Entry(SNAPSHOT, "V2 worker 心跳，UPDATE 密集，明文"),

    # ---------------------------------------------------------------- #
    # SKIP —— 永远不进 TEE。理由必须具体。
    # ---------------------------------------------------------------- #
    "alembic_version": Entry(
        SKIP, "RDS 迁移链自己的版本表；TEE 有独立的 alembic_tee_version，两条链互不感知"),
    "genesis_import_chunks": Entry(
        SKIP, "入住导入的 staging 数据，冻结窗口内处理完即弃，非用户资产（上游 plan 已决定不复制）"),
    "tee_sync_runs": Entry(
        SKIP, "TEE 同步自身的控制面/指标表，必须住在 RDS——复制到被它监控的库里没有意义"),
    "tee_reconcile_state": Entry(SKIP, "TEE reconcile 的控制面状态，同上，必须住 RDS"),
    "tee_reconcile_cursors": Entry(SKIP, "TEE reconcile 的游标，同上，必须住 RDS"),
    "bak_20260710_usr450_blobs": Entry(
        SKIP, "2026-07-10 单用户事故的一次性人工备份表，非生产数据", manual=True),
    "bak_20260710_usr450_chat": Entry(
        SKIP, "2026-07-10 单用户事故的一次性人工备份表，非生产数据", manual=True),
    "bak_20260710_usr450_memory": Entry(
        SKIP, "2026-07-10 单用户事故的一次性人工备份表，非生产数据", manual=True),
    "bak_20260710_usr450_users": Entry(
        SKIP, "2026-07-10 单用户事故的一次性人工备份表，非生产数据", manual=True),
    "bak_20260710_usr5d4a_users": Entry(
        SKIP, "2026-07-10 单用户事故的一次性人工备份表，非生产数据", manual=True),
}


def tables_in_lane(lane: str) -> tuple[str, ...]:
    """某条 lane 下的表名，按字典序。"""
    return tuple(sorted(t for t, e in REGISTRY.items() if e.lane == lane))


def synced_tables() -> tuple[str, ...]:
    """所有会进 TEE 的表（即非 SKIP），按字典序。"""
    return tuple(sorted(t for t, e in REGISTRY.items() if e.lane != SKIP))


# worker._TABLES 里那些**不是 RDS 表名**的 key。
#
# "identity" 实际操作 `user_blobs WHERE kind='identity'`：RDS 侧是密文信封，经 enclave
# 解密后写进 TEE 侧 user_blobs 的明文行。它没法作为 REGISTRY 的一条登记——REGISTRY 按
# 表名索引，而 user_blobs 整表已登记为 MIRROR（reconciler 的 _SCOPE_WHERE 排除了
# kind='identity'，正是把这部分让给 replicator）。
#
# 放在这里而不是散落在调度器里，是为了让"CIPHERTEXT 通道要跑哪些东西"仍然只有一个出处。
# 它刻意不进 REGISTRY，因此不受完备性守卫约束（守卫比的是真实表名）。
PSEUDO_CIPHERTEXT_TABLES: tuple[str, ...] = ("identity",)


# CIPHERTEXT lane 里会被**原地 UPDATE** 的表。
#
# 这条清单存在的理由：CIPHERTEXT lane 是 append-only 游标模型，游标永不回头，所以
# 原地改写的行必须由写侧显式 mirror.mark_pending(..., "requeue") 打标记，replicator
# 下一趟才会按 PK 重新拉取。漏打标记不会有任何报错或红灯——TEE 侧只是永久停在首次
# 复制时的状态，数据悄悄陈旧。
#
# tests/test_tee_requeue_coverage.py 守着这条清单与实际 mark_pending 调用的一致性。
# 给 CIPHERTEXT lane 加了会被 UPDATE 的表时，登记到这里并在写点补上 mark_pending。
MUTABLE_CIPHERTEXT_TABLES: tuple[str, ...] = (
    "model_api_credentials",
    "v2_conversation_summary",
    "v2_trajectory_reviews",
    "v2_workspace_entries",
)
