# Runtime V2 durable-completion claim reconcile 竞态修复设计

日期：2026-08-15
目标分支：`test`
状态：方案 A 已确认

## 背景与证据

PR #214 部署到 test 后，自动 canary 成功，但 `wake-0` 被 exact-claim
reconciler 单独重启一次。日志显示 Job 5291 已从 scheduled turn 返回并进入
`completed`，同时父进程仍短暂持有 stage=`durable_completion`、带
`active_job` 的进度快照。reconciler 在紧随其后的 `idle` pipe 消息到达前运行，
因数据库不再把终态 Job 视为 live claim，误判该子进程仍在执行无效 claim。

该问题不会批量杀死用户 Job，也没有造成 canary 失败；它会在正常完成窗口内产生
不必要的单槽 SIGKILL/respawn，降低瞬时容量并污染异常指标。

## 方案比较

1. **忽略 `durable_completion` 快照（采用）**：reconciler 只校验仍可能执行
   副作用的阶段。完成态已经越过持久化边界，只需等待随后 `idle`。改动局部，保持
   progress 协议及 watchdog 计时语义不变。
2. **在上报完成态前清空 `active_job`**：也能消除误杀，但会改变
   `durable_completion` 事件现有的 active-job 可观测语义，并影响已有测试和潜在诊断。
3. **无效 claim 延迟后重查**：可以吸收竞态，但会同样延迟真正被取消、转移或过期的
   claim 回收，扩大旧子进程继续执行的窗口。

## 设计

`_reconcile_fleet_claims_once` 在构造 exact-claim 查询集合时排除
stage=`durable_completion` 的快照。它不重启这些槽位，也不把其 Job/owner 交给
`valid_active_claims`。下一条 `idle` 消息会照常清空 active turn；若子进程在该点
异常卡死，现有进程 liveness/watchdog 仍负责回收，不依赖 exact-claim reconciler。

其他 stage 的行为不变：只要快照携带 active Job，而数据库中对应
`(job_id, claimed_by)` 不再是未过期的 claimed/running claim，就继续使用
generation + snapshot fence 只重启该槽位。

## 测试与验收

- 新增回归测试：一个 `durable_completion` 快照与一个真正失效的执行态快照同时存在；
  reconciler 只能重启后者，并且查询集合不包含完成态 pair。
- 保留已有测试：有效 claim 不重启、失效 claim 只重启对应槽位。
- 运行 pool supervisor、child supervisor、worker 与 capacity health 聚焦测试。
- 合入 `test` 后确认 CI 全绿，并检查 test 日志不再出现正常完成后紧邻的
  `restarted invalid exact claim`。

## 范围与回滚

不改数据库、队列状态机、Job TTL、worker 数量、Enclave 并发或 pre/prod 配置。
回滚只需撤销 reconciler 的 stage 过滤；现有 watchdog 与 lease reaper 始终保留。
