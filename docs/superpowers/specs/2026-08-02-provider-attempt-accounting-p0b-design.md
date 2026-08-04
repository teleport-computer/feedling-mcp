# Provider Attempt Accounting P0-B 设计

## 目标

P0-B 建立 content-free、coverage-aware 的 canonical provider-attempt ledger，让每个
可观测 HTTP dispatch、retry、compatibility retry 和 failover 都拥有独立账本行。
它填补 whole-turn metric 无法精确回答单次重试、请求已发出后进程死亡和
possibly-billed unknown 的缺口。

用量记录必须 fail-open：账本或 RDS 故障不得阻断 provider 调用、回复、
heartbeat 或其他业务任务。因此“canonical”表示已记录 attempt 的权威事实，
不表示 100% 不丢；不完整性通过 coverage 和 reconciliation 暴露。

## 现有日志的定位

`backend/provider_attempt_ledger.py` 和 `provider_attempts` JSON stream 是返回后 best-effort
诊断日志。它可以继续用于历史排查，但不作为 authoritative totals 的数据源。
旧行如果回填，必须标记 `source='legacy_best_effort'` 并默认排除在权威费用合计之外。

## RDS Schema

在当前业务 RDS 新增：

### `llm_provider_attempts`

- `attempt_id` UUID/opaque text primary key。
- `user_id` 外键，用户删除时级联删除。
- `installation_id`、`runtime`、`lane`、`job_id`、`turn_id`、`round_id`、`call_id`。
- `outer_attempt_ordinal`、`inner_attempt_ordinal`、`retry_kind`。
- requested/resolved provider、model 和 transport。
- `started_at`、`finished_at`、`outcome`、safe `error_class`、provider request ID。
- input/output/reasoning/cache read/write/miss tokens，可空。
- `usage_known`、`usage_unknown_reason`、`possibly_billed`。
- latency、TTFT，cost、currency、`rate_card_version`。
- `source`、`revision`、`created_at`、`updated_at`。

唯一幂等键是稳定的 logical call identity 加 outer/inner attempt ordinal，不依赖线程
调度顺序。provider request ID 不能作唯一键，因为失败前可能拿不到。

### `llm_provider_attempt_corrections`

追加式保留 late usage/cost correction，包含 attempt、revision、修正前后的 content-free
字段、reason code 和时间。主表保存最新 revision，修正表保存审计历史。

### `llm_rate_cards`

版本化 provider/model 费率，包含币种、input/output/reasoning/cache 单价、生效时间和
版本。没有匹配费率或 usage 时 cost 保持 `NULL`。

### `llm_usage_rollup_watermarks`

记录 attempt 聚合的最后完成水位、late correction 水位和重放代数。日聚合必须可从
attempt ledger 重建，不另写一套无法对账的计数器。

## Attempt 生命周期

1. Provider adapter 在每次实际网络 dispatch 前生成确定性 `attempt_id`。
2. 热路径只把 `started` 事件 `put_nowait` 到有界进程内队列，然后立即继续
   provider 请求；不同步等待 RDS。队列满或 recorder 异常只增加 coverage gap。
3. 后台 recorder 尝试写入 `started` 行；写入失败只记 safe counter，不影响调用。
4. 响应、安全分类后的异常或超时到达后，热路径再以 `put_nowait`
   排队完整终态；后台 recorder 用包含完整字段的 upsert 落库。
   即使 start 丢失，complete 成功也能恢复该 attempt。
5. 已写 start 但无终态的行在宽限后变为 `possibly_billed=true`。
6. 完全未写入 RDS 的 dispatch 通过 jobs/whole-turn calls 与 ledger rows 的差值体现为
   coverage gap，不伪造 attempt row。
7. Late correction 必须使用更高 revision，相同或更低 revision 是幂等 no-op。

Provider 调用热路径上的账本操作均有严格时间上限和异常隔离。任何记录失败
都不得更改 provider retry/failover 决策、用户可见结果或 job 终态。

## Usage Analytics 升级

P0-B 中：

- turns 和 whole-turn outcome 继续来自 `v2_turn_metrics`。
- calls、retry/failover、reasoning、possibly-billed、TTFT 和 cost 来自
  `llm_provider_attempts`。
- 页面展示 whole-turn `model_calls` 与 ledger attempt count 的 reconciliation。
- `logical_call_coverage = distinct recorded call_id / whole-turn model_calls`；attempt rows
  可因 retry/failover 多于 logical calls，不能直接用 attempt count 除以 model calls。
- 每个 logical call 内的 outer/inner ordinal 缺口另作 `attempt_sequence_gaps`
  展示，不与 logical-call coverage 混合。
- Provider/model breakdown 区分 requested 与 resolved identity。
- authoritative/estimated/unknown cost 分开展示。

## 隐私、保留和删除

主表和修正表禁止 prompt、reply、reasoning content、tool content、provider raw body、
headers、credentials、endpoint 和 raw stack locals。provider request ID 按运营需要限长。

Attempt 行按 `user_id` 级联删除。只有不可逆去标识化且无法回推个人的 fleet
aggregate 可在用户删除后保留。retention/pruning 使用现有 scheduler/process，不引入新基建。

P0-B 只增加当前业务 RDS 的 schema/index、现有 worker 内存队列和现有后台进程
的 recorder/reconciler；不新增数据库实例、消息队列服务或部署单元。

## 验收

1. 外层 retry、内层 compatibility retry 和 failover 每次生成独立行。
2. 返回 usage 的失败 attempt 计入 token/cost。
3. Start 失败不阻断 provider；complete upsert 可以恢复缺失 start。
4. Start/complete 都失败时显示 coverage gap，不显示为零。
5. Worker 在 dispatch 后死亡留下 possibly-billed row。
6. 重放、重复请求和相同 correction revision 不重复计费。
7. Rate-card 版本切换不改写历史 cost；late correction 有审计记录。
8. 记录 RDS 延迟、超时或异常不影响业务返回和 retry 次数。
9. 用户删除后 attempt、correction 和可关联 rollup 同步删除。

## Stacked PR 交付

本 spec 对应第二层 stacked PR。分支 `feat/provider-attempt-accounting`
从 P0-A HEAD 创建，PR 先以 P0-A 分支为 base；P0-A 合入 `test` 后改为
`test`。
