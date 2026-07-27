# TEE 影子库全量对齐 + 表同步机制 — 设计

> 日期：2026-07-27　状态：设计已获用户批准，待写实施计划
> 上游：`docs/superpowers/plans/2026-07-23-tee-promotion-decrypt-removal.md`
> （本设计落实其 **Task 0.6** 与 **Phase 1 的 v2 表迁移策略**两项）

## 1. 问题

RDS 有 61 张表（prod），TEE 影子库只有 20 张。缺口不是"同步坏了"，而是
**TEE 同步从设计上就是三层手工白名单登记制，且迁移无落地通道**：

1. **DDL** 靠手写 `backend/alembic_tee/` revision，与 RDS 的 `backend/alembic/`
   链零派生关系。
2. **数据流**要在三处分别手工登记：`db.py` 写点插 `mirror.execute`、
   `tee_replicator/worker.py` 的 `_TABLES`、`tee_shadow/reconciler.py` 的
   `TABLES`。三处互不校验，谁都不是全集。
3. **`alembic_tee` 没有任何 CI 钩子**，靠人工跑 `python -m backend.alembic_tee`。

第 3 条已经出过事故：`0002_notify_relay` / `0003_merge_tee_heads` 早已合并进
仓库，但 test 与 prod 的 `alembic_tee_version` **实测都停在 `0001_tee_baseline`**
——合了从未执行，所以 `notify_relay_configs` / `notify_relay_logs` 两张表在代码
里存在、在实库里不存在。

因此"V2 的 19 张新表没同步"是这套机制的必然产物，不是故障。要解决的是机制，
不只是补数据。

## 2. 实测事实（2026-07-27）

| 项 | 值 |
|---|---|
| prod RDS 表数 | 61 |
| test RDS 表数 | 56（差 5 张 `bak_20260710_*`，prod 独有） |
| TEE prod / test 表数 | 各 20，两边清单一致 |
| 缺口 | 45 张 |
| 缺口数据总量 | **3189 行 / 约 20.8 MB**（其中 16 MB 是 `v2_trajectory_events`） |
| `alembic_tee_version` | test 与 prod 均为 `0001_tee_baseline`（head 应为 `0003`） |
| RDS 逻辑复制 | `rds.logical_replication=off`、`wal_level=replica`（PG 17.9） |
| TEE PG | 17.10，`max_logical_replication_workers=4`（做 subscriber 已就绪） |

数据量小到不构成约束，这是后面"放弃增量、直接全量刷"这个取舍成立的前提。

### 2.1 既有慢性病（不在本设计范围，但会被本设计放大）

prod `tee_sync_runs` 最近 3 次 tick：`reconcile_ok = f`（长期 false）、
`verify_ran = f`、单趟耗时 **11 分钟**，`requeue_backlog` 在增长
（717 → 776 → 3028）。这是上游 plan 的 **Phase 0 Task 0.2**，至今未修。

**在一个 reconcile 从未成功的系统上再挂 32 张表会放大这个问题。** 本设计不修
它，但把它列为实施的前置风险，建议紧接着处理。

## 3. 目标与非目标

**目标**

1. TEE 与 RDS 全量对齐——每张 RDS 活表在 TEE 有对应 schema 与数据，为上游 plan
   的 Phase 4 cutover（TEE 扶正为唯一主库）铺路。
2. 建立机制，使**新增 RDS 表不可能被静默漏掉**。
3. 给 `alembic_tee` 建立落地通道，消除"合了没执行"。

**非目标**

- 不开 PG 逻辑复制（需改 RDS 参数组并重启实例，另排运维窗口）。本设计为它预留
  接口，见 §4.1 的 `LOGICAL` lane。
- 不修 §2.1 的 `reconcile_ok` 慢性病。
- 不改动 RDS 侧任何写路径、不改 enclave。

## 4. 设计

### 4.1 表注册表——单一真源

新增 `backend/tee_shadow/table_registry.py`。**每一张 RDS 表必须有且只有一条
登记**，形如 `(表名, lane, 理由)`。lane 五选一：

| lane | 含义 | 实现 |
|---|---|---|
| `MIRROR` | 热路径双写 | 现有 `mirror.execute`（`db.py` 写点） |
| `CIPHERTEXT` | replicator + enclave 解密 transform | 现有 `worker._TABLES` |
| `SNAPSHOT` | 整表快照刷 | **本次新增**，见 §4.4 |
| `SKIP` | 不同步，**理由必填** | 无 |
| `LOGICAL` | PG 逻辑复制 | **预留，本次不实现**，成员为空 |

注册表是整个设计的支点。现有三层白名单退化为"从注册表派生"，不再是独立真源；
`LOGICAL` 这个空 lane 使将来上逻辑复制时的改动收敛为"把表从 `SNAPSHOT` 改成
`LOGICAL`"，而不是重做一套机制。

### 4.2 CI 守卫——让漏登记变成红灯

新增 `tests/test_tee_table_registry_complete.py`，仿 `test_no_flask_anywhere`
的既有模式，与注册表比对，**差一张即红**。

"RDS 应有的表全集"如何取得：**不静态解析 alembic 迁移文件**（迁移是增量 op
序列，静态推导最终表集合既脆弱又易错）。改为在 conftest 已有的测试 PG 上
`alembic upgrade head`，再读 `information_schema.tables` 取实际结果。这条路径
同时验证了迁移链本身可用。

差异是双向的，两个方向语义不同：

- **推导集合 − 注册表 ≠ ∅** → 有 RDS 表未登记 lane，**红灯**。这是守卫的主职责。
- **注册表 − 推导集合 ≠ ∅** → 注册表里有 alembic 链外的表（如 `bak_20260710_*`
  这类人工建的备份表）。**允许**，但这类条目必须显式标注 `manual=True`，否则
  同样红——防止把打错的表名当成"人工表"蒙混过关。

**这个守卫在没有 PG 时必须 fail，不能 skip。** 本仓库有过先例：`conftest.py` 的
`collect_ignore` 在无 PG 时静默跳过约 2000 个 DB 用例、零 skipped 计数，"391
passed 全绿"是假象（见 memory `pytest-silently-skips-db-modules-without-pg`）。
一个会静默跳过的守卫等于没有守卫，所以它必须显式断言 PG 可用，不可用就红。

这正面回答了"新表如何自动同步"：机制不是自动同步，而是**自动拦截未决策的表**。
加了 RDS 表却没登记 lane 的 PR 合不进去，红灯的修法就是回答"这张表进不进 TEE、
走哪条 lane、为什么"。

明确的设计取舍：**不做 schema 全自动派生**。自动派生会把"要不要迁"这个真正需要
人判断的问题替人答了——`bak_*` 备份表、staging 表、同步控制面表都不该进 TEE，
这类判断没有机器可推导的规则。

### 4.3 DDL 派生与落地通道

- **派生**：新增 `scripts/tee/derive_tee_ddl.py`，从 RDS 实库导出
  `SNAPSHOT` / `CIPHERTEXT` 表的 DDL，剥掉 RDS 专属物（信封 CHECK 对解密后的
  明文列必须重写；per-user FK 改指向 TEE 自己的 `users` 表），产出 alembic_tee
  revision 草稿。**不再手抄 DDL。**
- **落地通道**（Task 0.6 未还的账）：把 `alembic_tee` 的执行做成手动触发的
  GitHub workflow（仿 pg-deploy，带 typo guard，使用 `TEE_MIGRATION_DATABASE_URL`
  的 owner 角色）。并加断言：**执行后 `alembic_tee_version` 必须等于代码里的
  head，否则红**。没有这条断言，"合了没执行"会再次发生。

### 4.4 SNAPSHOT lane

新增 `backend/tee_shadow/snapshot.py`。每张表一次原子替换：RDS 侧
`COPY (SELECT …) TO STDOUT`，TEE 侧**单事务内** `TRUNCATE` + `COPY FROM`。

25 张 SNAPSHOT 表合计约 2 MB / 1340 行，全量刷一趟成本可忽略。因此
**不做增量、不做游标、不做 requeue 补偿**——这正是它相对"扩展现有白名单"方案的
全部价值：`UPDATE` 与 `DELETE` 天然正确，没有补偿逻辑。这批表里大量是可变行
（队列 status 流转、心跳、allowlist、凭证轮换），用现有 append-only 游标模型
处理它们需要给每张表配 requeue + prune，成本线性于表数且永久。

挂在现有 `admin/tee_sync_scheduler.py` 上作为独立 tick，复用其 advisory-lock
选主与 run-lock（保证与手动 admin 触发、与 replicate/reconcile 永不重叠）。
间隔单列一个 env 变量，不与既有节奏抢。结果记入 `tee_sync_runs.report`，与现有
`replicate` / `reconcile` 平级。

**顺序约束**：TEE 侧这些表带指向 `users` 的 per-user CASCADE FK，故 snapshot
必须在 `users` 已同步之后执行，且 `TRUNCATE` 按显式依赖顺序进行——不用
`TRUNCATE … CASCADE`，它在这里太钝，会连带清掉不该清的表。

### 4.5 CIPHERTEXT lane 扩展

7 张密文表并入现有 replicator 通道。它们用的是同一套标准信封
（`body_ct` / `nonce` / `K_user` / `K_enclave`），其中
`chat_message_archive.doc` 与 `chat_messages.doc` **完全同形**，直接复用现有
`transforms.plaintext_chat_doc`。因此实际工作量是**一套通用信封 transform +
逐表接线**，而非 7 套独立实现。待解密合计 1849 行。

**已知风险（实测）**：`v2_trajectory_events` 的 567 行 `enclave_pk_fpr` 全为空
——CHECK 约束把该键列为可选，所以这些行没记录用哪把 enclave 钥封装。无法静态
判定解密成功率，只能实际试。历史上正是这类无 fpr 自检的行造成过 790 行毒行
（见 memory `tee-replicate-poison-row-headofline-quarantine`）。故设**硬 gate**：

先在 test 环境对这 7 张表**逐张**跑解密探针（`v2_trajectory_events` 在 test 有
124 行；其余 6 张的 test 行数在实施时现查），**任一张成功率不足 100% 就停下来
单独决策该表的 lane**，回退选项是"密文原样搬"（TEE 建同形 jsonb 列不解密）。
探针必须在 prod 回填之前跑完——prod 侧行数更多（合计 1849 行），毒行概率更高。

### 4.6 错误处理

沿用影子期铁律，一条不改：**任何 TEE 侧失败只 log + 计数，绝不传染主路径**。

- 单表 snapshot 失败 → 该表跳过，其余表继续，计数进 `tee_sync_runs`；下个 tick
  自动重试（全量刷天然幂等，不需要断点续传）。
- 解密失败 → 走现有 quarantine-and-advance，不冻结水位线（790 毒行事故的既有
  修复，直接复用）。
- `TRUNCATE` + `COPY` 保持在**同一事务**：中途失败时 TEE 侧保留旧的完整快照，
  不出现空表窗口。

### 4.7 测试与验收

- 注册表完备性守卫（§4.2），跑在 CI。
- `snapshot.py` 单测：幂等性、事务原子性（注入失败后表内容不变）、FK 顺序。
- **`verify` 范围必须随注册表扩展**。现状只覆盖 `reconciler.TABLES` + 6 张密文
  表；新增 32 张若不进 `verify`，就会出现上游 plan 点名的"**全绿假象**"。这是
  Phase 1 出口 gate 的硬性要求。
- test 端到端：跑一轮完整 tick，`verify` 报告 32 张新表行数与 RDS 一致。
- 解密探针 gate（§4.5）。

## 5. 表归类全清单

注册表覆盖 RDS 的**全部 61 张表**：其中 16 张已在 TEE 有对应物，按现状登记为
`MIRROR`（13 张明文运维表）或 `CIPHERTEXT`（chat_messages / memory_moments /
world_book_entries 等）；TEE 侧另有 4 张本地表（`alembic_tee_version`、`frames`、
`tee_pending_device_migration`、`tee_replication_cursors`）不属于 RDS 表集合，
不进注册表。

下面列的是 **45 张缺口**的归类。

> 实施注记：`tee_sync_scheduler._CIPHERTEXT_TABLES` 中的 `identity` 条目在 TEE
> 侧没有同名表，其写入目标需在实施第 3 步登记 lane 时确认清楚（可能写入
> `users` 或 `user_blobs`），不要想当然。

### 缺口分档（45 张）

### SKIP — 11 张（设计上就不该进 TEE）

| 表 | 理由 |
|---|---|
| `alembic_version` | RDS 自己的迁移链版本表；TEE 有 `alembic_tee_version` |
| `bak_20260710_usr450_blobs` | 一次性人工备份表，prod 独有 |
| `bak_20260710_usr450_chat` | 同上 |
| `bak_20260710_usr450_memory` | 同上 |
| `bak_20260710_usr450_users` | 同上 |
| `bak_20260710_usr5d4a_users` | 同上 |
| `tee_sync_runs` | TEE 同步的**控制面**，本就该住 RDS |
| `tee_reconcile_state` | 同上 |
| `tee_reconcile_cursors` | 同上 |
| `genesis_import_chunks` | staging 数据，冻结窗口处理，上游 plan 已决定不复制 |
| `frame_envelopes` | TEE 对应物是形状不同的 `frames`，已由 CIPHERTEXT lane 覆盖 |

### 已登记欠执行 — 2 张

`notify_relay_configs`、`notify_relay_logs`：DDL 在 `alembic_tee/0002` 已写、
reconciler 白名单已登记，**只欠在实库执行迁移**。属纯还账。

### CIPHERTEXT — 7 张（解密成明文，用户 2026-07-27 拍板）

| 表 | 密文列 | prod 行数 |
|---|---|---|
| `chat_message_archive` | `doc`（896 行完整信封 + 1 行疑似 R2 offload 指针） | 897 |
| `v2_trajectory_events` | `payload_envelope` | 567 |
| `model_api_credentials` | `api_key_envelope`（BYOK provider key） | 365 |
| `v2_conversation_summary_segments` | `summary_envelope` | 16 |
| `v2_conversation_summary` | `summary_envelope` | 4 |
| `v2_trajectory_reviews` | `review_envelope` | 0 |
| `v2_workspace_entries` | `content_envelope` | 0 |

`chat_message_archive` 那 1 行无 `body_ct` 的记录需按 `chat_messages` 的 R2
offload 路径先水合 `body_ct` 再解密（见 `worker._chat_unpack`）。

### SNAPSHOT — 25 张（明文，整表快照刷）

`agent_action_queue`(0)、`agent_jobs`(66)、`agent_status_events`(153)、
`chat_r2_cleanup`(0)、`chat_r2_lifecycle`(334)、`dau_daily_snapshot`(14)、
`model_api_routes`(380)、`provider_health`(30)、`retention_cohort_snapshot`(105)、
`runtime_state`(2)、`user_growth_daily_snapshot`(8)、`v2_capture_batches`(0)、
`v2_effect_outbox`(32)、`v2_effect_sink_applied`(1)、`v2_mcp_mutation_attempts`(0)、
`v2_runtime_control`(1)、`v2_runtime_state`(28)、`v2_sandbox_usage_events`(0)、
`v2_terminal_failure_outbox`(32)、`v2_trajectory_access_audit`(0)、
`v2_trajectory_streams`(66)、`v2_turn_metrics`(66)、`v2_user_allowlist`(7)、
`v2_wake_schedule`(7)、`v2_worker_heartbeats`(8)。

括号内为 prod 行数，合计 1340 行 / 约 2 MB。已实测确认这批表的 jsonb 列
（`payload_json` / `result_json` / `detail_json` / `state_json` / `actions_json` /
`v2_effect_outbox.payload`）装的是明文，不含信封。

## 6. 实施顺序

1. **还账**：`alembic_tee` 0002/0003 落到 test → 核对 `alembic_tee_version` →
   落到 prod。
2. **注册表 + CI 守卫**：先让守卫红，暴露全部 45 张未登记表。
3. **归类**：按 §5 逐张登记 lane 与理由，守卫转绿。
4. **DDL 派生**：生成新 revision，test 落地 → prod 落地。
5. **SNAPSHOT lane**：实现 `snapshot.py`，接进 scheduler。
6. **CIPHERTEXT lane**：通用信封 transform + 7 张表接线；**先跑 test 解密探针
   gate**。
7. **verify 扩范围**；test 端到端验证 → prod。

第 1 步与第 2 步无依赖，可并行。第 6 步的探针 gate 不过则该表回退为
"密文原样搬"并回到 §5 修改归类。

## 7. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| `reconcile_ok` 长期 false、`requeue_backlog` 增长（§2.1） | 高 | 本设计不修；建议紧接着做上游 Task 0.2。实施期间盯 `requeue_backlog` 趋势 |
| `v2_trajectory_events` 无 `enclave_pk_fpr`，可能解不开 | 中 | §4.5 的 test 探针 gate；失败则回退密文原样搬 |
| `verify` 未随注册表扩范围 → 全绿假象 | 中 | §4.7 列为硬性验收项 |
| snapshot 的 `TRUNCATE` 触发 CASCADE 误删 | 中 | 显式依赖排序，禁用 `TRUNCATE … CASCADE` |
| TEE 侧新表挤占连接池（`FEEDLING_TEE_POOL_MAX=32`） | 低 | snapshot 串行执行，单连接；不并发刷表 |

## 8. 决策记录（2026-07-27，用户拍板）

1. **终点是"为 TEE 扶正做准备，全量对齐"**，而非只补机制。
2. **同步机制走"C 过渡 + A 目标态"**：先上整表快照刷，PG 逻辑复制作为目标态
   另排 RDS 重启窗口。注册表的 `LOGICAL` lane 为此预留。
3. **7 张密文表全部解密成明文存**，包括 `model_api_credentials` 的 BYOK
   provider key。

   > 关于第 3 条中的 BYOK 凭证，设计时提出过反对意见并已被用户明确否决，此处
   > 如实记录其含义：TEE 库的 `feedling_owner` 角色可读全库（日常排查即在使用
   > 该角色），因此 365 个用户的 provider API key 将以**可直接使用的明文**形式
   > 落在 TEE 库中。这是相对现状的实质安全降级，且与上游 plan 把该表列为
   > Phase 2 Task 2.3 专项处理的安排不同。若后续要收敛，收敛点是 Task 2.3。
