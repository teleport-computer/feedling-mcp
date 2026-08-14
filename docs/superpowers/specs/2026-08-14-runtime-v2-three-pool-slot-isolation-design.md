# Runtime V2 三池与单 Slot 进程隔离设计

- 日期：2026-08-14
- 状态：设计方向已确认，书面规范待复核
- 目标分支：`test`
- 优先级：P0 Chat 抢占与即时 claim 回收；P1 三池/单 Slot 进程；P2 Profile 与容量优化
- 取代范围：取代 `2026-07-13-hosted-runtime-v2-PR-D-pool-history-safety-design.md`
  中“一个 turn-child 承载全部 slot、watchdog 整池重启”的故障域设计；保留其进度时钟、
  lease/write fence、outbox 幂等、kill switch 与历史完整性设计。

## 1. 摘要

生产 Runtime V2 当前由一个 `turn_child` 进程运行 4 个 asyncio slot。虽然其中默认有 2 个
slot 只领取 `chat/manual_wake`，所有 slot 仍共享一个进程。任一 Profile、Heartbeat 或其他
Job 长时间不报告进度时，watchdog 会杀掉整个 child，连带终止进程内所有 Job；被杀 Job 的
数据库 claim 还会保留到约 300 秒 lease timeout。同一用户的任意 `claimed/running` 后台 Job
又会阻止 Chat 被领取，因此预留 Chat slot 也无法解除阻塞。

本设计把运行时改为三个逻辑池、每个 slot 一个独立进程：

| Pool | Lane | 生产初始 Slot |
| --- | --- | ---: |
| Foreground | `chat`, `manual_wake` | 4 |
| Wake | `heartbeat`, `scheduled`, `screen_watch` | 2 |
| Heavy | `profile`, `dream`, `capture`, `maintenance`, `trajectory_review` | 2 |

生产机器为 8C16G，初始总并发从 4 提升至 8。每个 slot 同时只运行一个 Job，watchdog 杀一个
slot 进程即只杀一个 Job。Chat 入队时原子抢占同用户的非 Chat Job，物理进程收到取消通知后
退出；数据库状态已经先行终结，因此旧进程即使尚未退出也不能再写。watchdog 杀进程后立即按
`job_id + claimed_by` 回收 claim，不再等待 lease 自然过期。

## 2. 已确认的事故事实

2026-08-14 的生产事故中，两条 Chat 都通过了入口准入，但从未被领取：

- Job `3688` 入队时全局 active 为 2，估算等待约 25.18 秒，小于 60 秒准入 SLA；
- Job `3698` 入队时全局 active 为 2，估算等待约 25.23 秒，同样通过准入；
- 两条 Chat 分别被同用户的 Heartbeat `3686` 和 Profile `3694` 挡住；
- `claim_next_job` 的 `NOT EXISTS` 条件禁止同一用户同时存在第二个 `claimed/running` Job，
  不区分 lane；
- Profile `3694` 在 `provider_config_resolved` 之后约 240 秒没有可见阶段进度；
- watchdog 杀的是整个 `turn_child`，不是某个用户或某个 Job；
- Heartbeat `3686` 的第三次 provider request 发出约 4 秒后即被连带杀死，不能据此认定它
  自身已卡死；
- 被杀 Job 的 claim 未立即回收，继续阻止同用户 Chat，直到 lease timeout；
- 当前 Profile 会读取、解密全部合格 Memory Garden 卡片；事故用户有 554 张卡，约 1.07 MB；
- `recent_mean_service_sec` 只统计已完成 Chat，失败/超时样本不会污染该均值；
- 4 个 slot 中的 Chat 预留已经启用，默认分配为 2 个 Chat-only、2 个 unrestricted。

因此，“准入只统计前台”和“启用预留 slot”都值得保留或校正，但不能单独解决本案。根因是：

1. 同用户单飞缺少 Chat 对后台 Job 的运行中抢占；
2. watchdog 的物理故障域大于一个 Job；
3. kill 后 claim 回收慢于用户可接受等待；
4. Profile 的阶段观测和资源边界不足。

## 3. 目标与非目标

### 3.1 目标

1. 同用户后台 Job 运行时，新 Chat 在正常数据库和 Worker 条件下 2 秒内可被领取。
2. 一个 Job 卡死只终止它所在的 slot 进程，不中断其他用户或其他 pool 的 Job。
3. slot 进程死亡或被 watchdog 杀死后，对应 claim 在 5 秒内释放。
4. 后台 pending/claimed/running 不再导致全站 Chat 被入口准入拒绝。
5. 生产初始容量为 8 slot，并可按 `6/2/2`、`8/2/2` 分档扩容到 10、12 slot。
6. Enclave 和数据库并发按整个 serve-worker 实例预算，不随子进程数意外倍增。
7. Profile 有全局并发上限、阶段进度、分批读取和总时间边界。
8. 任何取消、kill、重试路径都保持回复、工具副作用和持久化写入的幂等与 generation/lease
   fence。

### 3.2 非目标

- 不修改模型 prompt、人格或主动开口产品语义；
- 不在本阶段实现按 provider route 的完整自适应容量预测；
- 不把 queue TTL 动态绑定到模型平均执行时长；
- 不为每种后台 lane 建立独立 pool；
- 不以简单提高 `FEEDLING_V2_ENCLAVE_CONCURRENCY` 代替全局资源治理；
- 不在本设计中横向增加 CVM；先完成单 CVM 的进程隔离与容量验证。

## 4. 核心设计决策

### 4.1 Pool 是隔离类别，Slot 是容量单位

系统只创建三个逻辑 pool，不创建多个同名 Foreground/Background pool。扩容通过增加 pool 内
slot 完成：

```text
Foreground Pool
  ├─ slot fg-0 process
  ├─ slot fg-1 process
  ├─ slot fg-2 process
  └─ slot fg-3 process

Wake Pool
  ├─ slot wake-0 process
  └─ slot wake-1 process

Heavy Pool
  ├─ slot heavy-0 process
  └─ slot heavy-1 process
```

每个 slot 进程内部 `max_workers=1`，同一时间只 claim 和运行一个 Job。Parent 继续拥有
reaper、scheduler、heartbeat、watchdog、Genesis 和所有 slot supervisor。一个 slot 退出不会
影响其他 slot 的 event loop、进程内 semaphore 或 DB pool。

### 4.2 三池 Lane 映射

| Lane | Pool | 队列优先级 | 说明 |
| --- | --- | ---: | --- |
| `chat` | Foreground | 100 | 用户即时消息，最高优先级 |
| `manual_wake` | Foreground | 100 | 用户显式触发，但运行中仍应向新 Chat 让路 |
| `heartbeat` | Wake | 50 | 主动唤醒，不与重型 Profile 共享进程 |
| `scheduled` | Wake | 50 | 用户提醒，抢占时必须可靠重排而非丢弃 |
| `screen_watch` | Wake | 50 | 时间敏感，但允许向 Chat 让路 |
| `capture` | Heavy | 10 | 有 prepared-batch 取消/恢复要求 |
| `maintenance` | Heavy | 10 | 可重试后台工作 |
| `dream` | Heavy | 10 | 可重试、可能触发 Profile |
| `profile` | Heavy | 10 | 重型全量画像生成，全局并发 1 |
| `trajectory_review` | Heavy | 1 | 离线分析，最低优先级 |

现有 `ORDER BY priority DESC, created_at` 继续作为同一 pool 内的 pending 排序。优先级不是抢占
机制；运行中抢占由第 5 节定义。

### 4.3 Pool 心跳与容量

当前 `v2_worker_heartbeats` 只表达一个 turn worker 的总容量，无法让 Chat 准入只读取
Foreground 容量。设计为 heartbeat 增加显式 `pool` 维度，避免依赖 `worker_id` 字符串约定：

```text
worker_id + pool + kind → capacity, beat_at, build identity
```

每个 pool 的 parent-side 聚合器写入该 pool 当前健康 slot 数：

- slot 正在运行健康 Job：计入 capacity；
- slot 被判定卡死、正在 kill/respawn：立即从 capacity 扣除；
- slot 新进程完成启动握手后：重新计入；
- 整个 pool 停止：capacity=0。

Chat 准入只读取 Foreground pool 的活容量。Admin 同时展示各 pool 的配置容量、健康容量、busy、
restarting 和 pending。

## 5. Chat 抢占与同用户单飞

### 5.1 原则

Chat 是同用户的最高优先级输入。新 Chat 到达时：

- 若已有同用户 Chat 在运行，继续沿用现有 input-generation/coalesce 语义，不启动第二个 Chat；
- 若已有同用户非 Chat Job 在 `claimed/running`，必须让其终止、重排或恢复，然后入队 Chat；
- 抢占首先提交数据库权威状态，再通知物理进程退出；不能先杀进程再等待 lease。

### 5.2 原子数据库事务

Chat send 的权威事务继续遵守现有 `runtime_state → agent_job` 锁顺序，并在同一事务完成：

1. 锁定用户 runtime state；
2. 读取并锁定该用户当前 active Job；
3. 根据 lane 执行抢占策略；
4. 将旧 Job 终结或转入可恢复状态，使其 lease 立即无效；
5. append/coalesce 用户消息并创建或唤醒 Chat Job；
6. 提交后发送 wake/cancel 通知。

该事务完成后，不再存在会挡住 Chat claim 的同用户 `claimed/running` 后台行。

### 5.3 Lane 抢占策略

| Active Lane | 新 Chat 到达时 |
| --- | --- |
| `chat` | 不抢占；把新输入 coalesce 到同一 Chat Job |
| `manual_wake` | `superseded:foreground_chat_preempted` |
| `heartbeat`, `screen_watch` | `superseded:foreground_chat_preempted`；未消费上下文交给 successor |
| `profile`, `dream`, `maintenance`, `trajectory_review` | `superseded:foreground_chat_preempted`；以后按正常 due/retry 规则重建 |
| `scheduled` | 取消当前执行并创建/保留同一 durable reminder 的 successor，禁止丢提醒和重复提醒 |
| `capture` | 调用 prepared-batch cancel/recovery 协议；保留未提交输入，随后重新入队 |

任何 lane 的旧 owner 在状态提交后都失去 lease。所有写入、工具 dispatch、reply effect、Profile
CAS 和 Capture commit 边界必须重新验证 `job_id + claimed_by + valid lease`。已经发送到外部系统
且不可撤回的工具调用依赖既有 effect/call idempotency；抢占不得生成第二个副作用 ID。

### 5.4 取消通知与物理终止

Parent 为每个 slot 维护：

```text
pool, slot_id, pid, job_id, lane, claimed_by, generation, turn_start
```

turn-child 的 progress pipe 协议增加 job identity。Chat 事务提交后，在现有 wake bus 上发布
content-free cancel 通知：`job_id + claimed_by + reason`。持有该 Job 的 Parent 收到通知后只终止
匹配 generation 的 slot 进程并 respawn。

通知丢失时仍安全：

- 数据库状态已经终结，所有 write fence 必须失败；
- slot 的 owner/lease 定期检查会发现所有权丢失并退出；
- Parent 的短周期 active-job reconciliation 作为通知丢失兜底，发现 slot 正运行已终结 Job 时
  杀死该 slot。

## 6. Per-Slot Watchdog 与即时回收

### 6.1 监控单位

每个 slot 有独立 `ChildSupervisor` 和 watchdog state。继续保留现有四类时钟：

- event-loop heartbeat age；
- slot progress age；
- current turn stall age；
- current turn absolute age。

progress 消息必须携带 `job_id/lane/claimed_by`，从而使 kill 决策与数据库回收精确绑定。

### 6.2 Kill 顺序

watchdog 判定某 slot 卡死后执行：

```text
slot capacity → 0
  → 冻结 (job_id, claimed_by, generation) 快照
  → SIGKILL 对应 slot 进程
  → owner-fenced terminalize/recover 该 Job
  → 释放同用户 active singleflight
  → respawn slot
  → 启动握手成功后 capacity → 1
```

数据库回收使用 `WHERE id=? AND claimed_by=? AND status IN ('claimed','running')`，不得影响已经被
其他 owner 接管或已自然完成的 Job。

终结结果：

| Lane | Watchdog Kill 后 |
| --- | --- |
| `chat` | `failed:slot_watchdog_timeout`，结算失败回复义务，不制造重复气泡 |
| `scheduled`, `capture` | 执行各自的 durable recovery/requeue |
| 其他后台 lane | `failed` 或 `superseded` 后按已有 backoff/due 规则重建 |

若数据库暂时不可用，仍先杀物理进程，随后 Parent 将精确回收请求留在有界重试队列；reaper 的
lease timeout 是最终兜底，但不再是正常恢复路径。

### 6.3 Lane 时间预算

首版保留现有安全下限，改为按 slot 当前 lane 选择预算：

| Pool | Stall Budget | Absolute Budget | 备注 |
| --- | ---: | ---: | --- |
| Foreground | 240s | 1500s | 保留长 Chat/tool-loop 合法空间 |
| Wake | 240s | 900s | 主动任务必须有界 |
| Heavy | 240s，Profile batching 上线后 120s | 1200s | Profile 每个批次/Provider round 都刷新进度 |

Heavy 首先沿用 240 秒，避免在全量读卡尚未分批前引入新的误杀。Profile batching 和批次进度
上线并通过等价 554-card 负载后，再把 Heavy stall 收紧到 120 秒。该值是“连续无阶段进度”，
不是总执行时间。Profile 单次 Provider timeout 为 90 秒，Enclave 请求 timeout 为 20 秒；正常
边界会及时刷新 stall clock。具体默认值在 test/pre 故障注入后可向上调整，但不能低于其最慢
单步的合法 timeout。

## 7. Foreground 准入与 TTL

### 7.1 Lane-aware Admission

新增/扩展查询：

```python
inflight_job_count(lanes={"chat", "manual_wake"})
live_worker_capacity(pool="foreground")
```

Chat admission 的估算只使用：

- Foreground pending/claimed/running；
- Foreground 健康 capacity；
- 最近 completed Chat 的服务时长。

后台积压不再转化为前台入口 503。Admin 的全局 inflight 指标继续保留，但不得用于 Chat 准入。

### 7.2 服务时长指标

当前均值只包含 completed Chat，失败/timeout 不会污染，本阶段不以“剔除失败样本”为修复项。
首版保留现有指标以控制变量；P2 再根据生产数据评估 P75/P90、EWMA 或 route 级模型。不能使用
中位数作为唯一保守估计，因为它会低估长尾。

### 7.3 Queue TTL

`PENDING_CHAT_TTL_SEC` 继续表达产品允许的排队时间，不绑定模型执行均值。初始保持 120 秒；若
灰度期间需要过渡缓冲，可以配置为 180 秒，但不作为根因修复。Job claim 后使用独立 execution
lease/watchdog budget。

## 8. Profile 有界化

### 8.1 频率

Profile 正常触发规则保持不变：

- 用户首次切换 V2 时生成；
- Profile 缺失时在成功 Chat 后生成；
- `state=ok` 至少满 7 天且 Memory Garden count/max_updated_at 变化才刷新；
- Dream 完成后强制刷新；
- 失败后按 5 分钟起、最长 6 小时的指数退避，在后续触发点重新入队。

健康用户不会每次 Chat 都生成 Profile。风险集中在大 Memory Garden 用户和反复失败用户。

### 8.2 全局并发

Heavy 有 2 个 slot，但 Profile 在整个 serve-worker 实例内最多运行 1 个。首版不引入新的
dispatcher：只有一个指定 Heavy slot 的 lane allowlist 包含 `profile`，其他 Heavy slot 明确排除
`profile`；指定 slot 空闲时仍可领取其他 Heavy lane。这样复用现有 claim lane 白名单即可得到
确定性实例级上限，第二个 Heavy slot 可继续处理 Capture、Dream 或 Maintenance。

若未来横向增加 serve-worker CVM，Profile 并发限制升级为数据库 lease/advisory admission，不能
让每个 CVM 都独立认为自己拥有“全局 1”。

### 8.3 分批读取与阶段轨迹

`_read_profile_cards` 从一次读取全部卡片改为有界批次：

- index 仍证明完整 cardinality；
- fetch 每批 50～100 张，默认 64；
- 每批保持稳定顺序和完整性校验；
- 所有批次完成后才进入 Profile generation；
- 任一批次失败则不写部分画像。

新增 content-free trajectory/progress：

```text
profile_index_started / profile_index_completed
profile_fetch_batch_started / profile_fetch_batch_completed
profile_provider_request / profile_provider_response
profile_write_started / profile_write_completed
```

事件只记录 card count、batch index/count、字符数、耗时、provider call ordinal 和错误类别，不记录
卡片或 prompt 明文。

### 8.4 时间与调用预算

- Enclave 单次请求继续有 20 秒 transport timeout；
- Profile Provider 单次请求保持 90 秒；
- 最大 Provider 调用数保持 8，后续用生产数据评估；
- Profile Job absolute budget 初始 1200 秒；
- 超过预算按 Profile failure metadata/backoff 终结，不生成用户可见错误气泡。

## 9. Enclave、数据库与 Provider 资源预算

### 9.1 Enclave

当前 `ENCLAVE_SEMAPHORE` 是子进程内对象。改成 8 个 slot 进程后，若每个进程仍允许 2 并发，
理论总并发会意外放大到 16。新设计要求 Parent 运行一个跨 slot 的实例级 admission broker，
slot child 通过独立双向 IPC 请求 `acquire(pool, slot_generation, request_id)` 和 `release`，初始
总并发 4：

| Pool | 保底额度 |
| --- | ---: |
| Foreground | 2 |
| Wake | 1 |
| Heavy | 1 |

空闲额度允许按 Foreground → Wake → Heavy 的优先级借用，但 Heavy 不得占用 Foreground 的最后
两个保底 token。broker 按 `slot_generation + request_id` 记账；slot 进程退出、被 kill 或 IPC
断开时，Parent 自动释放该 generation 持有的全部 token，避免许可泄漏。等待 acquire 本身也要
上报独立的 `enclave_admission_wait` 阶段，不能被误认为 Enclave 请求已卡死。

Enclave 服务自身还应保留硬并发上限作为最后保护。横向扩 CVM 时，各实例配置额度之和不能超过
Enclave 硬上限，直到引入集中式 broker。

### 9.2 PostgreSQL

每个 slot 同时只运行一个 Job，因此不得在每个 child 中复用现有按 `MAX_WORKERS` 推导的大 pool。
初始预算：

- 每个 slot child：max 2 connections；
- Parent supervisor/loops：max 8 connections；
- 8-slot serve-worker 实例预算上限：约 24 connections；
- 10-slot：约 28；
- 12-slot：约 32。

部署前必须把该预算与 backend、Enclave、replicator 等其他连接相加，并核对 RDS
`max_connections`。Admin 增加按进程/角色观察的 pool used/wait 指标。

### 9.3 Provider

8 个 slot 可以同时等待外部模型。继续保持每用户单 Chat 合并，并监控：

- provider/route 的 concurrent requests；
- 429、timeout、5xx；
- request P75/P95/P99；
- 共享 relay 是否因跨用户并发恶化。

本阶段不阻塞于 route 级调度；若真实数据表明共享 relay 成为瓶颈，再增加 route/credential
admission。

## 10. 用户失败语义

失败提示按责任和可恢复性区分：

| 错误 | 行为 |
| --- | --- |
| `queue_timeout` | 说明平台暂时无法及时处理，并准确说明消息是否保留/会否自动恢复 |
| `slot_watchdog_timeout` | 平台执行异常；服务端有自动重驱时不要求用户立即重发 |
| provider timeout/5xx | 说明用户模型服务无响应，不鼓励连续重发 |
| provider 401/403/402 | 明确提示 key、权限或余额问题 |

只有存在 durable、幂等的服务端自动重试时，文案才能承诺“无需重发”。不能用话术掩盖一个已
终局失败且不会自动恢复的 Job。

## 11. 配置与恢复

三池和单 Slot 进程隔离是 Runtime V2 的唯一拓扑，不保留 legacy 模式、共享多 Slot child，
也不保留可拼成部分上线状态的独立开关。完整实现以一个 PR 合入 `test`，先在 test 环境验证。
以下容量参数不是 Secret，直接写入 test 主 CVM compose 的 `serve-worker.environment`，不放入
GitHub Secret、GitHub Variable 或 Phala encrypted env：

```yaml
FEEDLING_V2_FOREGROUND_SLOTS: "4"
FEEDLING_V2_WAKE_SLOTS: "2"
FEEDLING_V2_HEAVY_SLOTS: "2"
FEEDLING_V2_PROFILE_INSTANCE_CONCURRENCY: "1"
FEEDLING_V2_ENCLAVE_INSTANCE_CONCURRENCY: "4"
```

旧 `FEEDLING_V2_MAX_WORKERS` 不再参与容量计算。Admin 展示最终解析后的三个容量值。

以下内容仍属于 Secret，继续通过现有加密环境注入，绝不能内联到 YAML：

- `DATABASE_URL`；
- `FEEDLING_RUNTIME_TOKEN_SECRET` 及 previous secret；
- provider/API key；
- 其他密码、token 和私钥。

直接修改 YAML 会改变 compose hash，需要按现有发布流程重新授权/部署。容量和故障域变更因此
进入代码审查和部署记录，不能在 GitHub Secret 页面静默漂移。

恢复不切换运行模式：重新部署此前已验证的 known-good image/commit。加法迁移 `0085` 保留
安装，不做降级；它与旧 image 向后兼容。恢复后重新核对 worker heartbeat、Foreground capacity、
队列年龄和 exact-claim 回收，再决定是否继续测试。

## 12. 可观测性

Admin 和指标至少新增：

- 每 pool configured/live/busy/restarting slot；
- 每 pool pending、oldest pending age、claim latency；
- preemption count，按 old lane/result 分类；
- preemption commit → process exit latency；
- watchdog kill count，按 pool/lane/reason；
- watchdog kill → claim released latency；
- stale-owner write fence rejection count；
- Profile card count、batch count、各阶段 latency、provider calls；
- Enclave broker used/wait/P95，按 pool；
- DB pool used/wait/timeout，按 parent/slot；
- Chat admission rejects，按 `no_foreground_capacity/over_sla/control_halted` 分类。

所有标签必须 content-free，禁止 user_id、prompt、卡片正文等高基数或私密内容进入公共 metrics。
按需调查时通过受控 Admin trace 使用 job_id。

## 13. 测试与验收

### 13.1 P0 故障注入

1. Profile 已 running 时发送 Chat：旧 Profile 原子 superseded，Chat 2 秒内 claim。
2. Heartbeat 已 running 时发送 Chat：未消费上下文转 successor，Chat 不等待 lease timeout。
3. Scheduled 已 running 时发送 Chat：提醒不丢、不重复，Chat 先执行。
4. Capture prepared/commit 各边界被抢占：无部分写、无锁泄漏、可恢复重排。
5. Profile slot 永久阻塞：只 kill 该 slot；同时运行的 Chat 和 Wake 完成。
6. Chat slot 永久阻塞：只终结该 Chat，不影响其他 Foreground slot。
7. kill 发生在 reply/effect 各持久化边界：最终最多一个回复和一个副作用。
8. kill 后数据库正常：claim 5 秒内释放。
9. kill 后数据库短暂不可用：物理进程仍被杀，恢复请求最终精确回收，不误伤新 owner。
10. cancel 通知丢失：reconciliation 发现已终局的活进程并杀死，旧 owner 写入被 fence。

### 13.2 调度与容量

1. 20 个 Heavy pending 时，Foreground 仍按真实前台容量准入并 claim Chat。
2. Foreground 4 个 slot 全忙时，估算只使用 Foreground capacity，不借用 Wake/Heavy 假容量。
3. pool 内按现有 priority/FIFO 排序；pool 间无串领。
4. Profile 同时最多 1 个，第二个 Heavy slot 可运行其他 lane。
5. 任一 pool respawn 不改变其他 pool heartbeat/capacity。
6. 8 个 slot 下 Enclave 实际并发不超过实例级 4。
7. 8 个 slot 下 serve-worker DB 连接不超过配置预算。

### 13.3 Profile

1. 554 张卡按稳定批次完整读取，数量和 ID 无截断、重复或遗漏。
2. 每批产生 content-free progress，正常 Profile 不被 120 秒 stall budget 误杀。
3. 任一批次 Enclave timeout：不写部分 Profile，记录失败 metadata 并退避。
4. Provider 第 1～8 次调用任一边界被 kill：旧 owner 不写，重试无重复画像写入。
5. 超过 absolute budget：只终结 Profile，不产生用户可见 Chat 错误。

### 13.4 性能验收

- Chat claim latency P95 ≤ 2 秒；
- watchdog kill → claim released P95 ≤ 5 秒；
- 8-slot 稳态内存使用 < 70%（16G 机器）；
- 8-slot 稳态 CPU 使用 < 70%（8C 机器，排除短时峰值）；
- Enclave P95 相比 4-slot 基线无不可接受回退；
- Provider 429/timeout 率无显著增长；
- DB connection wait/timeout 不增长到影响 Chat。

“不可接受回退”和“显著增长”的数值阈值必须用 test 当前数据固定，不在没有基线的设计阶段
伪造百分比。

## 14. test 实施与扩容顺序

完整三池实现以一个 PR 合入 `test`，不拆成可独立启停的部分拓扑。顺序是：

1. 采集 test 的 pool、DB、Enclave、provider 与 claim latency 基线；
2. 部署 `4/2/2 = 8`、Chat 原子抢占、per-slot watchdog、精确 claim 回收；
3. 同时启用 Profile batching/concurrency 1、Enclave broker 4 和 child DB pool 2；
4. 完成 P0 故障注入、554-card 等价负载和资源检查；
5. 观察完整 test 流量窗口后，另行评审是否增加 Foreground slot。

任何普通开发分支 PR 目标为 `test`。进入 `main` 的生产推广必须来自 `test` 或 `pre`，并记录
test/pre 的故障注入、资源和用户链路证据。该改动改变部署拓扑、故障域和隔离假设，实施时必须
同步更新 `docs-site/content/docs/` 下的架构、工作流、自托管信任模型和 changelog；若公共 API
错误契约变化，还要更新 OpenAPI、生成 `docs-site/openapi/public.json` 并运行文档合同测试。

## 15. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 进程数增加导致内存膨胀 | test 的 8-slot 要求 <70% 内存；child DB pool 固定小上限 |
| Enclave semaphore 随进程倍增 | Parent 实例级 broker + Enclave 服务硬上限 |
| 抢占时外部工具已发出 | lease/write fence + effect/call idempotency + 故障注入 |
| Scheduled/Capture 被抢占后丢工作 | lane-specific durable successor/recovery，不做通用 `superseded` |
| kill 后误回收新 owner | 所有回收带 `job_id + claimed_by + generation` |
| cancel 通知丢失 | DB 先终结 + Parent reconciliation + lease fence |
| 三池容量被错误汇总 | heartbeat 显式 `pool` 字段；Chat 只读 Foreground |
| 8-slot Provider 并发放大 | Enclave broker 保持实例上限 4；监控 route 429/timeout |
| 新拓扑部署异常 | 重部署 previous known-good image/commit；保留兼容的加法迁移 `0085` |

## 16. 完成定义

设计实现完成必须同时满足：

1. 第 13 节 P0、调度、Profile 测试全部通过；
2. test 环境数据库测试未因缺少 Postgres 而静默跳过；
3. test 环境完成真实 kill/respawn、Chat 抢占、554-card 等价负载验证；
4. 8-slot 资源指标满足性能门槛；
5. 公共架构/部署/错误契约文档同步完成；
6. previous known-good image/commit 恢复流程在 test 验证；
7. 本设计不包含 pre/prod 推广；后续推广另行记录验证证据并遵守仓库分支规则。
