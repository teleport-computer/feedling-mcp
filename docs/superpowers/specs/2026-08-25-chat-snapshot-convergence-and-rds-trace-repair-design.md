# Chat 严格快照收敛与 RDS Trace 修复设计

**日期：** 2026-08-25
**目标分支：** `test`
**状态：** 已确认，待实施

## 1. 背景与生产证据

生产已启用持久化 chat 变更游标、Postgres `LISTEN/NOTIFY`、多 worker 增量同步与 256 行 hot cache。上线后的稳定窗口中，Chat 业务接口没有 5xx，`chat_change_state.version` 与 `chat_change_events` 最大版本也没有发现不一致；但精确 10 分钟日志窗口仍出现：

- 57 次 `chat cache changed during strict snapshot reload`；
- 38 次 `/v1/debug/trace` 500；
- PROD RDS 迁移头为 `0101_chat_change_events`，不存在 `trace_events`。

两个现象互不依赖：前者是单个 worker 内热用户缓存的并发收敛问题，后者是当前 selected primary 与 trace schema 的部署时序问题。

## 2. 根因

### 2.1 严格快照重试会被持续本地写入饿死

`UserStore.reload_chat_hot_strict()` 当前最多进行三次乐观尝试。每次尝试先记录 `_chat_cache_generation`，在不持有 `chat_lock` 的情况下读取 version-consistent 数据库快照，再次加锁后仅在 generation 未变化时替换缓存。

这个检查能防止数据库读取期间的本地已提交写入被旧快照覆盖，但它没有有界收敛保证。只要同一用户在三次数据库读取期间都发生本地缓存提交，三次 generation 检查都会失败，函数最终抛错。此时数据库和缓存可能都是正确的；失败只是乐观算法在持续竞争下饥饿。

### 2.2 Trace API 与 selected-primary schema 不一致

Trace 读写函数使用应用当前的 primary pool。生产仍是 RDS-primary，但最初 `trace_events` 只存在于 TEE migration `0033_trace_events`，因此应用在 RDS 上访问不存在的表。

T306 已在 `origin/test` 落地正确修复：

- RDS migration `0102_trace_events` 与 TEE migration 共享字节一致的表、分区和索引契约；
- `trace_events` 被登记为 selected-primary-local、`SKIP` 跨库复制且 `required_in_tee=True`；
- 动态分区从 PostgreSQL catalog 继承根表 lane，避免逐日注册；
- lifespan、分区维护、监控、公开架构文档和迁移测试已同步更新。

因此本次不另造 trace migration，也不改变 trace 的数据所有权模型；只验证并随正常发布链路交付 T306。

## 3. 方案比较

### 方案 A：两次乐观快照 + 一次串行化兜底（采用）

前两次继续在锁外读取数据库；若都因 generation 变化而冲突，第三次持有该用户的 `chat_lock` 完成一次 version-consistent hot snapshot，并在同一临界区替换缓存。

优点：

- 总数据库读取上限仍是三次；
- 正常路径不增加锁等待；
- 竞争路径必定有界收敛；
- 不需要推导写入类型，也不会错误合并删除或元数据更新。

代价：连续竞争时，同一用户的本地缓存写入会等待一次最多 256 行的数据库快照。锁是 per-user、per-process 的，不阻塞其他用户或其他 worker。

### 方案 B：本地 mutation journal 与快照合并（不采用）

在 generation 变化后，把快照与读取期间发生的本地 upsert、delete、metadata mutation 合并。

它能保持全程锁外读，但需要新增严格排序的 mutation journal、删除 tombstone 和清理边界；遗漏一种本地变更就可能复活删除行或覆盖较新的元数据，正确性和维护成本显著更高。

### 方案 C：放弃当前快照并等待下一次唤醒（不采用）

generation 冲突时保留当前缓存、返回未收敛状态，依赖下一次 NOTIFY 或 poll 重试。

它避免锁等待，但无法给当前调用提供有界收敛；丢失通知或低后续流量时会延长陈旧窗口，也不能消除现有线上错误。

## 4. 严格快照算法

`reload_chat_hot_strict()` 保持现有接口和返回值：

1. 计算并限制 hot snapshot limit，行为与现状一致。
2. 最多执行两次乐观尝试：
   - 在 `chat_lock` 下初始化缓存状态并读取 generation；
   - 释放锁，读取 repeatable-read 的 `(version, rows)`；
   - 再次加锁；generation 相等则原子替换并返回，不等则进入下一次尝试。
3. 两次均冲突后，进入一次串行化兜底：
   - 获取该 store 的 `chat_lock`；
   - 持锁读取同一个 `chat_load_hot_snapshot_strict()`；
   - 持锁调用 `_replace_chat_rows_locked()` 并返回。
4. 数据库异常继续向上抛出；`ensure_chat_fresh()` 维持 last-good-cache、返回 `False` 的现有 fail-open 合约。

### 4.1 正确性不变量

- 任何数据库失败都不能清空或部分替换 last-good cache。
- 乐观尝试只能在 generation 未变化时替换缓存。
- 串行化兜底持有 `chat_lock`，所以本进程内没有缓存提交能在“快照读取”和“缓存替换”之间插入。
- 数据库快照仍在一个 read-only repeatable-read transaction 中同时读取版本和 rows。
- 远端 worker 在快照之后提交的更高版本由 durable event/后续 NOTIFY 继续推进；本次修复不改变跨 worker 一致性模型。
- 锁顺序保持 `chat_sync_lock -> chat_lock`，不新增反向获取。

### 4.2 观测语义

成功的串行化兜底不再产生 `incremental sync failed`。数据库错误及其他真实同步错误仍沿用现有失败日志。若增加兜底观测，只使用固定、不含用户内容的 slug，并避免把成功兜底记为错误。

## 5. Trace 修复交付边界

本分支以包含 T306 的最新 `origin/test` 为基线，不 cherry-pick、不复制迁移。验证范围包括：

- RDS migration head 包含 `0102_trace_events`；
- RDS/TEE `_UP` 契约一致；
- `trace_events` 根表、DEFAULT 分区、当日/未来分区和索引存在；
- selected-primary-local 注册为 `SKIP`，分区子表继承根表；
- trace insert/query/clear 正常，账户删除后 bounded incident trace 仍保留；
- 分区监控在 RDS-primary 和 TEE-primary 都按当前 selected primary 启动。

不在本次范围内：

- 直接对 PROD 手工执行 DDL；
- 把 RDS trace 历史复制到 TEE；
- TEE-primary 流量切换；
- trace API 契约或保留期调整。

## 6. 测试策略

### 6.1 RED 测试

在 `tests/test_chat_incremental_sync.py` 增加确定性并发测试：

- 前两次 snapshot callback 都模拟本地已提交写入，使 generation 连续变化；
- 第三次 callback 断言当前线程持有 `chat_lock`，返回包含全部提交行的最新快照；
- 断言恰好三次读取、没有异常、缓存版本与 ID/seq 索引一致。

保留现有“一次冲突、第二次成功”测试，证明正常乐观路径没有退化。

另覆盖串行化兜底中的数据库异常：异常向上抛出且 last-good cache 不变。

### 6.2 回归切片

至少执行：

- `tests/test_chat_incremental_sync.py`
- `tests/test_wake_bus.py`
- `tests/test_chat_poll_cross_worker_staleness.py`
- `tests/test_trace_events.py`
- `tests/test_tee_table_registry.py`
- `tests/test_pre_test_migration_convergence.py`
- `tests/test_debug_trace_event_route.py`

数据库用例必须连接本机 PostgreSQL，不能接受因数据库缺失而静默 skip 的假绿。

## 7. 发布与验证

1. 从最新 `origin/test` 的普通修复分支提交设计、测试和实现，PR 目标为 `test`。
2. 合入 `test` 后由标准 TEST 部署链运行 RDS migration `0102_trace_events`。
3. TEST 验证：
   - migration head 与表/分区/index；
   - debug trace enable/event/query/clear；
   - Chat history/poll/response；
   - 多 worker 写入与快照竞争回归；
   - 日志中无 `chat cache changed during strict snapshot reload` 和 trace relation missing。
4. 记录 TEST 证据后，才允许从 `test` 或 `pre` 发起 `main` 发布。
5. PROD 发布后比较同口径窗口：
   - `strict snapshot` 失败必须为 0；
   - `/v1/debug/trace` relation-missing 5xx 必须为 0；
   - Chat 业务 5xx、版本 mismatch 不得回升；
   - 继续观察 RDS ReadIOPS、吞吐与 CPU，确保串行化兜底没有形成新的查询风暴。

## 8. 回滚

Chat 改动仅影响进程内收敛策略，应用版本回滚即可恢复旧行为；没有数据迁移。

RDS `0102_trace_events` 是 additive migration。若应用回滚，保留表和 trace 数据，不在紧急回滚中删除分区表；migration 自带 downgrade 仅供隔离验证，生产若需物理删除必须另行评审并保存 incident evidence。
