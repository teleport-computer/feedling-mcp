# Chat 通知收敛与纯唤醒协议设计

**日期：** 2026-08-25

**状态：** 书面复核通过（2026-08-25）

**目标分支：** `test`

## 1. 背景与问题

生产已经启用 `FEEDLING_CHAT_SYNC_MODE=incremental` 和 256 行 hot cache。
PostgreSQL 的 `chat_messages` statement trigger 会在事务提交时写入
`chat_change_state` / `chat_change_events`，并发送带目标版本的 v2 通知：

```json
{"v":2,"c":"chat","u":"<user_id>","r":123}
```

但是应用写路径仍在提交后调用 `wake_bus.notify("chat", user_id)`，发送旧 payload：

```json
{"c":"chat","u":"<user_id>","o":"<worker_id>"}
```

新 receiver 收到旧 payload 后，即使运行在 `incremental` 模式，也会调用
`reload_chat_hot_strict()`，重新读取最新 256 行。因此一次普通 chat mutation 可能先由
v2 通知完成增量同步，随后又被旧通知触发一次 hot snapshot。

2026-08-25 的只读生产检查提供了以下旁证：

- 上线后同时间段 RDS `NetworkTransmitThroughput` 从 17.60 MB/s 降到
  12.33 MB/s，下降约 29.9%，仍未达到原先针对 chat 分量的 85% 目标；
- `chat_change_events` 在约 14 小时内产生 132,008 行、占 39 MB；
- 其中 131,069 行是单消息 `upsert`；
- 最近活跃用户的 256 行 hot snapshot 平均约 0.346 MiB，按事件频率加权约
  0.242 MiB。

生产日志 API 在本次检查中不可用，因此不能用日志直接证明每一个旧 payload 都落到
了缓存进程；但代码路径可以确定：只要接收进程已经缓存该用户，旧 payload 就会执行
hot snapshot。

## 2. 目标与非目标

### 2.1 目标

1. Chat 数据 mutation 只使用数据库事务内的 v2 change notification。
2. 非 chat 数据变化但需要 chat poll 立即返回时，使用明确的 wake-only payload。
3. 新 receiver 收到遗留旧 payload 时，在 incremental 模式下不再无条件读取 256 行。
4. 保持旧 receiver、新 receiver、旧 sender 和新 sender 的滚动部署兼容。
5. 保持 chat、voice、MCP、vision、activity 和 Runtime V2 的用户可见行为不变。
6. 用内容无关、固定枚举的遥测区分增量同步、纯唤醒、旧 payload 和安全快照。

### 2.2 非目标

以下优化有价值，但不与本次协议收敛混在同一实现中：

- `chat_change_events` 的保留和清理策略；
- 15 分钟 `UserStore` TTL 的分组件刷新；
- claim-only/update-only 事件的进一步抑制；
- 启用 `pg_stat_statements`、Performance Insights 或 Enhanced Monitoring；
- 把 hot cache 从 256 继续降低到 128 或 64；
- 修改任何公开 HTTP API、E2EE 边界或持久聊天历史保留语义。

## 3. 调用点审计

当前代码共有 17 个显式 `wake_bus.notify("chat", ...)` 调用。

### 3.1 Chat mutation：11 个

这些路径已经 insert、update 或 delete `chat_messages`，数据库 trigger 会发送 v2
通知。应用侧旧通知应删除，本地 `store.notify_chat_waiters()` 和本地缓存合并保持不变。

| 文件 | 数量 | mutation 类型 |
|---|---:|---|
| `backend/core/store.py` | 4 | append、finalize、sequence finalize、idempotent append |
| `backend/chat/chat_core.py` | 1 | clear history delete |
| `backend/voice/routes_asgi.py` | 2 | voice cancel/finalize 的 card insert 与行清理 |
| `backend/hosted/history_import.py` | 1 | onboarding greeting insert |
| `backend/model_api_runtime/v2/jobs_store.py` | 2 | terminal failure reply insert |
| `backend/agent_runtime/supervisor.py` | 1 | reply claim release update |

### 3.2 Wake-only：6 个

这些路径没有修改 `chat_messages`。它们改变 poll response 中的配置、探针或活动状态，
必须让远端 parked poll 立即返回，但不应刷新 chat cache。

| 文件 | 数量 | 唤醒原因 |
|---|---:|---|
| `backend/chat/activity_store.py` | 1 | resident tool activity |
| `backend/hosted/mcp_core.py` | 2 | MCP fingerprint/config 变化 |
| `backend/hosted/setup_core.py` | 1 | resident vision probe 创建 |
| `backend/model_api_runtime/v2/jobs_store.py` | 2 | terminal status / turn activity |

这 6 个调用迁移到新的 `notify_chat_wake_only(user_id)`。

## 4. 协议设计

### 4.1 Durable delta payload

保持数据库 trigger 的现有 payload 不变：

```json
{"v":2,"c":"chat","u":"<user_id>","r":123}
```

语义：版本 `r` 对应的 chat mutation 已提交。receiver 对缓存用户执行版本比较、连续
event 读取和变化消息 point read；gap、reset、缺行或超过批次上限时执行 bounded hot
snapshot。

### 4.2 Wake-only payload

新增发送接口：

```python
notify_chat_wake_only(user_id: str) -> None
```

payload 使用现有 `chat` channel，并增加严格的 wake-only 标记：

```json
{"c":"chat","u":"<user_id>","o":"<worker_id>","w":1}
```

不使用 `v` 字段，避免与 durable delta 版本协议混淆。新 receiver 只允许精确 key 集合
`{"c","u","o","w"}`、`w == 1`、非空且有界的 origin。合法 payload 只调用本地
chat waiter wake，不读取 version、event 或 hot snapshot。

沿用 `c="chat"` 而不是新增 channel，是为了滚动部署兼容：旧 receiver 只检查
channel 和 user，会把含 `w` 的 payload 当普通 chat 通知安全刷新；新 receiver 则采用
纯唤醒快路径。

### 4.3 Legacy payload

旧 payload 保持可接收：

```json
{"c":"chat","u":"<user_id>","o":"<worker_id>"}
```

处理规则：

- `legacy` 模式：继续执行 bounded hot snapshot，保留回滚语义；
- `observe` 模式：继续执行当前 observe 比较流程；
- `incremental` 模式：调用 `ensure_chat_fresh(force=True)` 读取当前 durable version，
  有变化则增量应用；随后无条件唤醒 chat waiter；
- malformed 或带未知 key 的 payload：拒绝，不得退化成 full reload。

第一方代码完成迁移后，steady state 不应再发送 legacy chat payload；保留接收支持只为
滚动部署、回滚和外部旧进程。

## 5. 数据流

### 5.1 Chat mutation

```text
request/worker
  -> mutate chat_messages
  -> PostgreSQL statement trigger
       -> increment chat_change_state
       -> insert chat_change_events
       -> pg_notify(v2 target version)
  -> local cache apply / local waiter wake

remote listener
  -> validate v2 payload
  -> skip when cache absent or already at target
  -> otherwise apply contiguous events + point rows
  -> bounded snapshot only on a safety fallback
  -> wake local waiter
```

应用不再为同一次 mutation 额外执行 `SELECT pg_notify(...)`。

### 5.2 Wake-only state change

```text
request/worker
  -> commit non-chat state
  -> local waiter wake when a local store exists
  -> notify_chat_wake_only(user)

remote listener
  -> validate strict wake-only payload
  -> find cached store
  -> wake chat waiter only
```

## 6. 滚动部署与回滚兼容

| Sender / receiver | 结果 |
|---|---|
| 新 DB v2 -> 新 receiver | 增量同步 |
| 新 DB v2 -> 旧 receiver | 旧 receiver 按 generic chat 通知完整刷新，安全 |
| 新 wake-only -> 新 receiver | 只唤醒，不查 chat |
| 新 wake-only -> 旧 receiver | 旧 receiver 完整刷新，昂贵但安全 |
| 旧 legacy -> 新 incremental receiver | version reconcile + 无条件 waiter wake |
| 旧 legacy -> 旧 receiver | 现有完整刷新 |

数据库 migration 已在 TEST/PROD 部署。新镜像上线前仍要检查 change tables、三个
statement trigger 和当前 migration head。回滚旧镜像不需要 schema downgrade：旧镜像会
恢复发送 legacy payload，数据库继续发送 v2，行为退回较昂贵但正确的双通知。

## 7. 失败处理与一致性

- `pg_notify` 仍是 best-effort latency hint，持久数据和 change events 才是事实源。
- Trigger notification 与 mutation 同事务；回滚时 event 和 notification 一并消失。
- wake-only 丢失时，parked poll 最多等待自身 timeout 后重试，不丢数据。
- v2 丢失时，后续版本、普通 version check、15 分钟 TTL 或 listener reconnect catch-up
  会恢复；版本缺口执行 bounded snapshot。
- receiver DB 读取失败时保留 last-good cache，返回失败遥测，不用空结果覆盖缓存。
- strict snapshot 继续使用“两次乐观读取 + 一次持锁收敛”的已上线实现。
- wake-only 必须在相应非-chat 状态成功提交后发送，不能早于事务提交。

## 8. 遥测与隐私

继续使用固定枚举、hash user ID、禁止文档/消息 ID/密文/DSN 的现有规则。至少区分：

- `event_sync`
- `already_fresh`
- `wake_only`
- `legacy_payload`
- `sync_failed`
- `snapshot_fallback`，附固定原因：`gap`、`reset`、`overflow`、`missing_row`、
  `generation_conflict`

实现可以沿用结构化日志；不要求本次引入新的 metrics backend。steady-state TEST/PROD 的
第一方 `legacy_payload` 应降到零。任何持续 legacy payload 都视为仍有旧进程或漏迁移
调用点。

## 9. 测试设计

### 9.1 单元测试

- v2 payload 只在版本落后时调用增量同步；
- wake-only payload 只唤醒，不调用 version/event/snapshot DB primitive；
- incremental 模式 legacy payload 调用 version reconcile，不调用 legacy snapshot；
- legacy/observe 模式保持原回滚和对照语义；
- same-origin wake-only 被过滤，本地 fast path 不重复唤醒；
- malformed/未知字段 payload 被拒绝且不产生 DB 查询；
- DB 读取失败保留缓存并记录固定错误原因。

### 9.2 调用点合同测试

- 11 个 mutation 路径不再调用通用 legacy `notify("chat")`；
- 6 个 wake-only 路径调用 `notify_chat_wake_only()`；
- 每条路径原有本地 waiter、缓存合并、capture 和业务返回值保持不变。

### 9.3 PostgreSQL 与多 worker 测试

- append、finalize、sequence finalize、delete、clear、voice cleanup、claim release；
- MCP config、vision probe、resident activity、Runtime V2 status；
- listener outage/reconnect、worker recycle；
- 新旧 sender/receiver 混合 payload；
- trigger rollback 不产生 event/notify。

## 10. 文档与发布

这是内部同步架构变化，不修改公开 API，但会改变系统架构和部署观测语义。因此同一 PR
需要更新：

- `docs/ops/chat-incremental-sync-runbook.md`；
- `docs-site/content/docs/architecture.mdx`；
- `docs-site/content/docs/changelog.mdx` 的 `Unreleased`。

发布顺序：

1. 合入 `test`，由 TEST 部署 workflow 发布；
2. 检查 migration、trigger、health、attestation 和容器镜像 pin；
3. 执行真实 chat、voice、MCP、vision/activity smoke；
4. 统计 `legacy_payload`、`wake_only`、`event_sync`、fallback 和错误；
5. 对比 TEST DB 查询和网络流量；
6. 只有 TEST 证据通过后，才提出从 `test`/`pre` 到 `main` 的生产发布。

## 11. 完成标准

- 第一方代码中不再存在 mutation 后的 legacy chat notify；
- wake-only 业务仍能跨进程立即结束 parked poll；
- incremental receiver 收到 legacy payload 时不执行 256 行 snapshot；
- steady-state `legacy_payload` 为零；
- 普通 chat mutation 不再额外执行应用侧 `SELECT pg_notify(...)`；
- 所有相关单元、DB、多 worker、文档和 TEST smoke 通过；
- TEST 不出现 chat 丢消息、重复回复、claim/redelivery、voice cleanup、MCP 配置延迟或
  activity 可见性回归；
- RDS NetworkTransmit 不回升，并记录可比较窗口供后续事件保留和 TTL 拆分决策使用。
