# Hosted Runtime V2 Usage Analytics P0-A 设计

## 目标与范围

P0-A 使用已有 `v2_turn_metrics` 交付独立的 Admin
`Usage / 模型用量` 页。它要回答 Hosted Runtime V2 的用户规模、已知
Token、每活跃用户日消耗、分位数、每用户及 provider/model 构成，
同时明确 whole-turn telemetry 的覆盖边界。

P0-A 不实现 provider 单次 attempt 账本、权威 cost 或 self-host 中央上报。
Reasoning token、possibly-billed 和 authoritative cost 在页面显示
`unavailable until P0-B`，不显示为零。

## 信息架构

- `/admin/data-track?view=runtime` 只保留 Runtime 故障值班信息，包括按
  `user_id` 的交付可靠性。
- `/admin/data-track?view=usage` 承载 Fleet Overview、每日趋势、
  provider/model breakdown 和 per-user table。
- `/admin/data-track/users/<user_id>?view=usage...` 展示单用户的每日趋势、
  model/lane breakdown 和覆盖率。

Runtime Health 不再渲染 per-user Token/model 表。P0-A 必须保留 PR #146
已有的交付可靠性查询、索引、年龄阈值和独立失败域。

## 时间和筛选合同

默认时区为 `Asia/Shanghai`，底层使用 UTC `TIMESTAMPTZ`。所有页面区块
共享同一个规范化查询对象：

```python
UsageQuery(
    start_at_utc: datetime,
    end_at_utc: datetime,
    timezone: str = "Asia/Shanghai",
    user_id: str | None = None,
    lane: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    completeness: Literal["all", "metered", "unknown"] = "all",
)
```

页面提供 24h、7d、30d、90d 预设和自定义日期。自定义区间按所选
时区的当地零点转成 UTC，起止日期都包含，最长 366 天。24h
为滚动 24 小时；其他预设为滚动精确时长。页面显示实际 UTC
起止和展示时区，避免把 `local_day` 误解为 UTC 日界。

Overview、trend、provider/model 表和 per-user 表必须消费同一个
`UsageQuery`。不允许各区块自行解析 query string 或使用不同的窗口边界。

## 数据源与 cohort

P0-A 仅读 `v2_turn_metrics`，范围为本实例 Hosted Runtime V2 所有 lane。
不从用户当前配置推断历史 provider/model，只使用每回合落库的
provider、model 和 route identity。空身份字段归一为 `unknown`，缺失数值
保持 `NULL`。

关键 cohort：

- registered accounts：截止 `end_at_utc` 的注册行数，包含重装孤儿，只作参考。
- activated users：截止 `end_at_utc` 已写记忆或发过真人消息的 distinct
  `user_id`，延用已有行为口径。
- model-active users：窗口内 `model_calls > 0` 的 distinct `user_id`。
- metered users：窗口内 `usage_reported_calls > 0` 的 distinct `user_id`。
- active user-days：distinct `(user_id, local_day)`，且当日 `model_calls > 0`。
- metered turns：至少一次调用有 usage 上报的 whole-turn metric 行。

所有使用表都按 `user_id` 归因，不按真人或 `principal_id` 合并。重新注册
会显示为不同行。

Registered/activated 是截止窗口结束时的参考 cohort，在页面与 usage-derived
指标视觉分开，不受 provider/model/lane/completeness 筛选影响。当这些维度
筛选非空时，“全体激活用户日均 token”显示 `not applicable for filtered cohort`，
不用固定全站分母除以已过滤 token。其余 usage-derived Overview、trend、
per-user 和 provider/model 数值都严格共享完整 `UsageQuery`。

## 指标合同

- `total_tokens = prompt_tokens + completion_tokens`。
- `prompt_tokens` 已包含 cache 部分；cache read/write/miss 是 breakdown，
  不再加到 total。
- 任一 token/cache 字段完全缺报时保持 `NULL`，不改成 0。
- 失败回合只要 provider 返回 usage，就计入 token。
- `usage_coverage = usage_reported_calls / model_calls`。
- `cache_coverage = cache_reported_calls / model_calls`。
- `unknown_usage_calls = max(model_calls - usage_reported_calls, 0)`。
- cache hit ratio 只在 read 和 miss 都可得且分母大于 0 时计算。

平均值不得合并为一个“用户平均”：

1. 平均每日全站 token = 窗口已知 token / 窗口精确天数；无用量日保留。
2. 平均每活跃用户日 token = 窗口已知 token / active user-days。
3. 全体激活用户日均 token = 窗口已知 token /
   `(activated users × 窗口精确天数)`。
4. 平均每 metered turn token = 已上报 usage 的成功及失败 turn token /
   metered turns。
5. user-day 已知 token 分布显示 p50、p75、p90、p95 和 max，并同时显示
   usage coverage。
6. 平均 model calls/turn 和 retries/turn 以窗口 whole-turn rows 为分母。

对部分窗口，“平均每日全站”分母使用精确秒数 / 86400，而不是页面
触及的 `local_day` 个数。自定义完整日期的分母是包含起止日的日数。

## 页面内容

### Fleet Overview

展示 registered、activated、model-active、metered users、active user-days、
turns、model calls、retries、failed turns、input/output/total/cache tokens、
unknown usage calls、usage/cache coverage。Hosted installation 在 P0-A 没有可靠的
installation identity，因此显示 `unavailable until self-host phase`，不用
`user_id` 伪装 installation 数。

### 每日趋势

按 `local_day` 展示 total tokens、model-active users、tokens/active-user-day、
tokens/metered turn、provider calls、retries、failed turns、usage/cache coverage。无数据日使用
日历序列补零；未知 token 不补零，coverage 会显示缺口。

### Per-user Usage

每个 `user_id` 一行：最近模型调用、活跃天数、turns/calls/retries/failed
turns、input/output/total/cache、calendar-day 平均、active-day 平均、每日
p50/p95、tokens/metered turn、主要 provider/model、usage/cache coverage、unknown
calls 和全站已知 token 占比。支持 user、lane、provider、model、completeness
筛选及已知 token、calls、retries、最近调用排序。

### Provider / Model

显示 users、turns、calls、retries、failed turns、input/output/cache/total、
tokens/call、latency p50/p95、failure/retry rate 和 usage/cache coverage。requested
与 resolved identity 在 P0-A 无法分开，字段标注为 whole-turn resolved/best-known。

## 查询和性能

服务层一次规范化 `UsageQuery`，再在同一个 `REPEATABLE READ, READ ONLY`
快照中生成 overview、daily、per-user 和 provider/model 结果。不使用 LIMIT
后再聚合；分页只作用于聚合完成后的 per-user 结果。

首版直接查 `v2_turn_metrics`，不双写 daily rollup。迁移只增加用量查询
实际需要的 PostgreSQL 索引。性能验收使用十倍预计数据量、实际数据分布和
`EXPLAIN (ANALYZE, BUFFERS)`；90 天默认 cohort 查询 p95 必须小于 2 秒。
如果无法达到，P0-A 必须在同一 PR 内增加可从 `v2_turn_metrics` 全量重建的
daily rollup，不得以上线后再说作为验收。

### 10x 实测后的 rollup 决策

直接查询在 30 万总行、90 天 74,019 行时，5 次 warmed 完整报表 p95 为
2.411 秒；300 万总行、90 天约 74 万行时单个完整请求超过 90 秒。把 raw
查询合成一个 materialized CTE 在 30 万行仅降到约 1.22 秒，不能线性满足
300 万行门槛。因此 P0-A 启用上述条件分支，在当前业务 RDS 增加可重建
daily rollup；这不是新增数据库实例或服务。

rollup 使用两张都保留 `user_id` 的 canonical 表，不保存跨用户、无法随账号
删除级联的 fleet 副本：

- `v2_usage_daily_users`：每个 Asia/Shanghai local day / user 一行，保存
  overview、daily、per-user 和 user-day distribution 所需的 all / metered /
  unknown 三套重叠子计数、token sum 与 known-count。
- `v2_usage_daily_dimensions`：每个 local day / user / lane / provider / model
  一行，保存 provider/model、lane、primary identity、filter option 与精确
  provider/model latency 所需的子计数和 `latency_samples`。
- `v2_usage_rollup_watermarks`：bootstrap、source update cursor、refreshed-at
  和错误/lag 元数据。两个 fact 表的已知 `user_id` 都使用 nullable FK
  `ON DELETE CASCADE`；NULL user 通过 `UNIQUE NULLS NOT DISTINCT` 保持一条
  canonical row。

`metered` 与 `unknown` 不是互斥桶。partial turn 可同时满足
`usage_reported_calls > 0` 和 `usage_reported_calls < model_calls`，因此每个
rollup row 在列内分别保存 all / metered / unknown 子聚合，不通过互斥枚举
拆行。NULL token/cache 继续用 `known_count + sum` 区分完全缺报、部分已知与
真正的零；cache 不加入 total。latency 仅为 spec 要求的 provider/model
p50/p95保存精确 integer samples，不用 daily percentile 加权或近似摘要。

rollup 不修改 `record_whole_turn_metric()` 热路径。现有 Runtime V2 worker 在
独立、限额、fail-open 的后台 tick 中按 local day 执行同一个
`REPEATABLE READ` 事务内的 `DELETE + recompute + INSERT`，两张 fact 必须来自
同一 MVCC snapshot。bootstrap 的 day range、初始 safe cursor 与 source head 也在
同一 `REPEATABLE READ` snapshot 中读取；成功发现 dirty range 或完成整日替换后才
CAS 推进对应 watermark 状态。bootstrap 不完整时 Admin 全量 raw fallback；完成后
首尾 partial day、未 ready day 和 cursor 后发现的 dirty day 整日 raw fallback。
其他展示时区始终走精确 raw 路径。
刷新失败、连接不足、锁竞争和超时只造成 lag/raw fallback/unavailable，并显示
freshness/coverage，不影响 provider、reply、retry、heartbeat 或其他 worker。

异步 cursor 在任意长事务并发下只能提供 bounded eventual consistency，不能在
完全不触碰写入热路径的同时形式化保证实时 exact。worker 不重复重扫固定 overlap
窗口，而只按 `(updated_at,id)` 顺序处理 `updated_at <= now - lateness_window` 的
成熟 source；窗口内 source 暂不推进 processed cursor，以 source head / 非零 lag
暴露，越过 safe horizon 后才进入 bounded row batch、标记 dirty day 并重建。这样
持续时间不超过 lateness window 的迟提交会在 cursor 越过其时间点前可见，同时避免
无新 source 时每个 tick 永久重建同一历史跨度；超过该窗口的任意长事务仍是明确的
一致性限制。相同 `updated_at` 下 head `id` 尚未追平也必须显示非零 lag。页面显示
processed cursor、source head、refreshed-at 与 lag；稳定 fixture、bootstrap 完成且
无 dirty day、safe horizon 前可见 source 已全部处理时必须与 raw 严格对账。账号删除由
user-grain FK 立即移除可归因 rollup，不等待异步刷新。

为使完整默认报表达标，各 breakdown 在同一个 PostgreSQL exported snapshot 下
并行读取：总共最多 3 条 `REPEATABLE READ, READ ONLY` 连接，exporter 自身执行
user/day core，两条 importer 在读取前导入同一 snapshot，分别执行 dimension
breakdowns 和精确 latency。必须有进程内单飞与 RDS advisory admission、短连接
获取超时、每 statement deadline、cancel/rollback；拿不到容量时串行 raw/rollup
fallback 或只把 Usage 对应区块标 unavailable，绝不占满业务连接池。

300 万行原型中，同一 exported snapshot 的三连接完整等价报表 5 次 warmed
p95 为 1.473 秒（core 约 0.77 秒、dimension 约 1.47 秒、exact latency 约
0.35 秒），满足默认 Asia/Shanghai 90 天 p95 小于 2 秒的门槛。生产实现必须用
同一 harness 重新验证，原型结果不能替代最终代码验收。

## 失败、隐私与删除

Usage 查询是 Admin 独立失败域；某一 breakdown 失败时只将对应区块显示
unavailable，不把缺报显示为零。页面只展示 content-free metadata，不读取
prompt、reply、conversation、memory、reasoning content、tool args/results、provider raw body、
endpoint 或 credential。

`v2_turn_metrics.user_id` 的删除语义继续沿用现有用户删除链。新增索引不改变
级联删除。页面不提供 prompt 或 content 导出。

## 验收

1. 两个用户、两天、两个模型和多个 lane 的 overview、daily、per-user 与
   provider/model 总量严格对账。
2. 成功、失败和 retry 进入正确计数；有 usage 的失败调用计入 token。
3. Usage/cache 缺报保持 unknown，不显示 0。
4. Cache token 不与 prompt token 重复相加。
5. 时区日界、DST 时区、部分日窗口和空日都有固定测试。
6. 所有筛选和排序使用同一 cohort。
7. 用户删除后关联 usage 数据不再可查。
8. 不存在隐藏的最新 N 行采样；十倍数据量下 90 天查询 p95 < 2s。

## Stacked PR 交付

本 spec 对应 stacked PR 第一层，继续使用 PR #146 和分支
`feat/admin-runtime-user-report`，目标分支为 `test`。
