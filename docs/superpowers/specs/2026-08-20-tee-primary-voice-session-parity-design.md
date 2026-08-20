# TEE Primary Voice Session Parity Design

**日期：** 2026-08-20

**状态：** 方向已确认；等待书面复核后实施

**基线：** `fc3fa8c2dac5ff9699210fbb140c84c85c14f03c`

## 问题

`voice_call_sessions` 由 RDS 迁移 `0081_voice_call_sessions` 创建，语音开始、取消、
finalize 和回复抑制都通过 `db.get_pool()` 在当前主库读写这张表。它原先被 TEE
注册表标成 `SKIP`，理由是并发 fence 必须和 RDS chat 写事务共库；这个理由只在
RDS 是主库时成立。

TEST 于 2026-08-18 把应用主库切到 TEE 后，`db.get_pool()` 随之指向 TEE，但 TEE
迁移链从未创建 `voice_call_sessions`。结果是部署和通用数据库探针可以通过，真实语音
入口却会在创建会话时确定性失败。根本不变量应是：

> `voice_call_sessions` 必须与当前 chat 主库共库，而不是必须留在 RDS。

实库审计显示，这是当前唯一一张“RDS 存在、TEE 缺失、且 TEE-primary 运行时必需”的
业务表。其余物理差异分为独立 Alembic 版本表、TEE 同步/对账控制表、一次性人工备份表，
不应为了集合相等而搬入 TEE。

## 决策

1. 在共享 TEE 迁移分支上创建与 RDS 同形的 `voice_call_sessions`。
2. 把该表的同步 lane 从 `SKIP` 改成 `SNAPSHOT`，使 RDS-primary 阶段的生命周期状态和
   tombstone 能在冻结前收敛到 TEE；TEE-primary 后，运行时直接在本地主库读写。
3. 用显式的 `required_in_tee` 注册表属性区分“复制策略”和“TEE-primary 是否必须有表”，
   并让 schema guard 覆盖所有必需表，包括不需要跨库复制的 TEE-primary 本地临时表。
4. 把真实的语音会话 create/cancel/finalize smoke 纳入 TEE-primary promotion 验收，不能只
   检查数据库连通性和通用 API。

此变更不要求 RDS 与 TEE 的所有物理表完全相同，也不会搬运 RDS 专属控制面和人工备份表。

## 审计范围与表分类

### 本次必须修复

| 表 | 当前状态 | 目标状态 | 原因 |
| --- | --- | --- | --- |
| `voice_call_sessions` | RDS 有、TEE 无、注册为 `SKIP` | TEE 建表并改为 `SNAPSHOT` | 当前主库上的语音生命周期 fence，运行时必需 |

### 已在 TEE、但不做 RDS → TEE 数据复制

以下表当前标记为 `SKIP`，但 TEE 迁移链已经创建。它们是 TEE-primary 的本地 staging、
观测或短期交接状态，不应被误解为“永远不进 TEE”：

- `genesis_import_chunks`
- `v2_wake_shadow_decisions`
- `voice_turn_results`
- `voice_turn_streams`

这些表保持 `SKIP` lane，但登记为 `required_in_tee=True`。切到 TEE-primary 后由应用在
TEE 本地自然产生数据，不从旧 RDS 搬历史临时数据。

### 保持 RDS-only

- `alembic_version`：RDS 迁移链版本表；TEE 使用独立的 `alembic_tee_version`。
- `tee_sync_runs`、`tee_reconcile_state`、`tee_reconcile_cursors`：RDS → TEE 迁移过程的
  源侧控制面，把它们复制到被监控目标没有意义。
- `bak_20260710_*`：一次性人工事故备份表，不属于产品 schema。

这些条目保持 `SKIP` 且 `required_in_tee=False`。本次不会追求 RDS/TEE 表名集合机械相等。

## 迁移拓扑

PRE/PROD 当前 TEE head 是 `0025_lane_rollup_voice`；TEST 已沿自己的 TEE-primary 分支推进到
`0029_plaintext_shadow_merge`。不能修改已经执行过的 TEST 迁移祖先，也不能为两个环境维护
两份可能漂移的建表 SQL。

采用“共享 DDL 分支 + TEST merge revision”：

```text
0025_lane_rollup_voice
├── 0030_voice_call_sessions_primary   # 实际建表，共享 revision
└── ... TEST 现有分支 ... 0029_plaintext_shadow_merge
                          \
                           0031_merge_voice_primary
                          /
0030_voice_call_sessions_primary ------
```

- `0030_voice_call_sessions_primary` 的 `down_revision` 是
  `0025_lane_rollup_voice`，只包含幂等、可审计的建表 DDL。
- PRE/PROD 合入共享 revision 后以 `0030_voice_call_sessions_primary` 为单 head。
- TEST 同步共享 revision，并增加双父 merge revision
  `0031_merge_voice_primary`，父节点是 `0029_plaintext_shadow_merge` 和
  `0030_voice_call_sessions_primary`。升级 TEST 时会实际执行共享 DDL，最后仍保持单 head。
- 两个环境使用同一建表 revision，不复制 DDL；merge revision 不重复建表。

如果实施时最新 TEST head 已前进，只调整 merge revision 的 TEST 父节点为当时单 head；共享
DDL revision ID、内容和 `0025` 父节点保持不变。

## Schema 合同

TEE 表与 RDS `0081_voice_call_sessions` 保持同形：

- `user_id TEXT NOT NULL`
- `call_id TEXT NOT NULL`
- `status TEXT NOT NULL DEFAULT 'active'`
- `cancel_reason TEXT NOT NULL DEFAULT ''`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `ended_at TIMESTAMPTZ`
- 主键 `(user_id, call_id)`
- `user_id → users(user_id) ON DELETE CASCADE`
- `status` CHECK 约束与 RDS 一致
- `(user_id, status)` 索引与 RDS 一致

迁移必须复用或由测试逐字比对 RDS 的合同 SQL，防止两条链再次独立演化。downgrade 继续遵循
TEE 迁移的既有策略：不支持在线逆迁移，回滚使用迁移前备份。

## 注册表语义

当前 `Entry(lane, reason, manual=False)` 把两件事混在一起：

- 数据在 RDS-primary 阶段怎样复制到 TEE；
- TEE-primary 运行时是否必须拥有该物理表。

新增 `required_in_tee: bool | None = None`：

- 未显式设置时，非 `SKIP` lane 推导为 `True`，`SKIP` 推导为 `False`，保持现有条目兼容。
- TEE-primary 本地表在 `SKIP` lane 上显式设为 `True`。
- `manual=True` 只描述来源不在迁移链，不能自动豁免 `required_in_tee`。
- 提供 `tee_required_tables()` 作为 schema guard 的唯一查询入口。

`voice_call_sessions` 改为 `SNAPSHOT`，因此默认就是 TEE-required。四张 primary-local SKIP 表
显式设为 `required_in_tee=True`。RDS 专属项保持默认 false。

守卫调整为：

1. 每张 RDS 迁移表仍必须有且只有一个 registry 条目；
2. `tee_required_tables()` 的每张表必须存在于 fresh TEE head；
3. 非 `SKIP` 表自然都是 required，防止同步登记了但忘记 DDL；
4. primary-local SKIP 表也必须由 TEE 迁移链创建；
5. `SKIP` 的注释改为“不做 RDS → TEE 数据复制”，不再承诺“永远不进 TEE”。

## Snapshot 行为

RDS-primary 期间，`voice_call_sessions` 进入现有 SNAPSHOT 调度和冻结验证：

- 整表按复合主键 upsert；
- RDS 已删除的会话会从 TEE prune；
- status、cancel_reason、ended_at 的 UPDATE 会在下一 tick 收敛；
- 超过 snapshot 行数硬阀或 FK/DDL 不匹配时，该表失败并使严格冻结验证不通过。

表量是短生命周期会话控制状态，适合现有小表 snapshot 机制。它不走 MIRROR，因为语音并发
正确性仍依赖当前主库内的单事务锁，跨库 best-effort 双写不能提供这个互斥；snapshot 只负责
切换前状态收敛，不参与运行时 fence。

TEE-primary 后，应用的 `DATABASE_URL` 指向 TEE，`db.get_pool()` 的现有代码无需分支，所有
create/cancel/finalize 与 chat 写仍在同一数据库完成。旧 RDS snapshot 源必须随 promotion
流程停止，避免反向覆盖 TEE-primary 新状态。

## Promotion Gate 与验收

在 TEE-primary 切换验收中增加数据库级真实生命周期 smoke：

1. 创建隔离测试用户；
2. `voice_call_create_active` 创建唯一 call ID；
3. 走 cancel 路径并验证最终状态/原因；
4. 再创建一个 call，走 begin-finalize → mark-finalized 并验证不能被 cancel 降级；
5. 清理测试用户，依赖 FK cascade 清理会话；
6. 任一步出现 undefined table、约束、事务或状态错误都阻止 promotion。

HTTP 级真实通话 smoke 仍应在 TEST 部署后执行，用来覆盖路由、鉴权和 gateway；数据库级 smoke
是确定性 release gate，不能依赖外部语音 provider 才能发现缺表。

## 部署顺序

1. 在 fresh PostgreSQL 上验证 RDS/TEE 两条迁移链、注册表覆盖和 voice 生命周期。
2. 将共享 revision 合入 PRE；在不切主库的情况下把 PROD TEE 从 `0025` 升到共享新 head。
3. 将同一修复同步到 TEST，并通过 merge revision 把 TEST TEE 从 `0029` 升到合并后单 head。
4. 在 TEST TEE-primary 跑数据库级 gate 和一通真实语音 create/cancel/finalize smoke。
5. 重新运行 PROD terminal ciphertext preservation dry-run。此前绑定 `0025` 的 dry-run plan
   因 head 改变而作废，必须生成新的 count 和 plan SHA-256。
6. 进入最终 PROD promotion 窗口时冻结 writer，执行最后 snapshot/verify，再使用精确批准的
   preservation plan 和既有 Phase 4 gate。

建表迁移是 additive，可在应用切换前完成；无需为了这张空/小控制表安排单独停机。真正停写
窗口仍由最后数据收敛和主库切换决定。

## 回滚与故障处理

- 迁移前保留已验证的 TEE base backup；migration 或 smoke 失败时停止 promotion，不切主库。
- additive 表存在但应用仍以 RDS-primary 运行时没有行为变化，可以安全等待修复。
- TEST 已经 TEE-primary：先迁移补表；在 smoke 通过前语音入口视为不可用，不伪造健康状态。
- 一旦 TEE-primary 产生新会话，不尝试在线 downgrade/drop table；恢复使用 TEE 备份或显式
  reverse-reconciliation 方案。
- snapshot 验证失败时不得用把表改回 `SKIP` 的方式放行。

## 测试与接受标准

实施必须至少证明：

1. fresh TEE migration 创建列、PK、FK、CHECK 和索引，且保持单 head；
2. 从 `0025` 走 PRE/PROD 分支可升级，从 `0029` 走 TEST merge 分支也会执行同一 DDL；
3. `voice_call_sessions` 注册为 `SNAPSHOT`，snapshot 能复制、更新并 prune 复合主键行；
4. 四张 primary-local SKIP 表被 `tee_required_tables()` 覆盖；RDS-only 控制/备份表不被要求；
5. fresh RDS 与 TEE schema guard 无遗漏和幽灵条目；
6. 以 TEE 作为 `DATABASE_URL` 时，真实 PostgreSQL 上 create/cancel/finalize 生命周期全部通过；
7. promotion gate 缺表时失败、表存在且生命周期正确时通过；
8. 现有语音、snapshot、verify、migration convergence 测试无回归。

这项修复不改变公共 API 请求/响应合同，也不改变加密信任边界，因此不需要重新生成 OpenAPI；
若 promotion 流程或公共架构页已描述 schema gate，则同步更新相关部署文档和 `Unreleased`
changelog。
