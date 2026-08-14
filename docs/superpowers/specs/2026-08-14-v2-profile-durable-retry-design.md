# Runtime V2 Profile Job 持久化重试设计

**日期：** 2026-08-14  
**状态：** 已获方向确认，待实施计划  
**目标分支：** `test`  
**前置依赖：** [PR #187](https://github.com/teleport-computer/feedling-mcp/pull/187) 先合入 `test`

## 背景

Runtime V2 用加密 `v2_agent_profile` 中的 `MEMORY` 和 `USER` 字段承载长期语义。PR #187 删除 provider-backed conversation semantic compaction 后，conversation maintenance 只保留 seq/count coverage sentinel，profile 因而成为旧聊天长期语义的唯一模型蒸馏层。

当前 profile 失败时会把 Job 标成 `failed`，并在 profile blob 的 `last_attempt.retry_not_before` 中记录退避时间。这个时间本身不会唤醒 Job；只有后续 Chat、Dream 或其他显式触发再次调用 `_enqueue_profile_if_due`，profile 才可能重试。用户不再活动时，首次失败的 profile 可以永久停留在 `pending` 或 `degraded`。

test 环境近 14 天的只读聚合显示：22 个 V2 用户共产生 452 个 profile Job，其中 442 个完成、7 个失败、2 个 lease timeout、1 个因 runtime generation 变化 supersede。失败原因包括 `field_empty:memory`、`field_empty:user` 和 `reply_not_json`。这说明失败率不高，但当前恢复依赖下一次用户活动；同时，少数测试用户可能产生大量 Job，因此自动恢复必须有分类、退避和上限，不能把所有错误都无限重试。

## 已确认的产品与架构决定

- 启用无用户活动时的 profile 自动恢复。
- Dream 的现有调度频率和成功后的强制 profile 刷新保持不变。
- 每轮 Chat 后的 profile 到期检查保持不变；它只是轻量检查，不等于每轮重新生成。
- provider 瞬时失败持久化退避重试。
- 模型输出形状错误只做有界自动重试。
- 数据不完整、输入超预算等结构问题等待数据或配置变化，不无限烧用户额度。
- 只先部署 test；prod 必须等待本地、CI 和 test 实环境证据。

## 目标

1. profile 瞬时失败后，即使用户不再聊天、Dream 不再运行、worker 被重启或重新部署，也能在退避到期后恢复。
2. 延迟等待中的 profile Job 不占 heavy slot，不进入 watchdog 的“有可领取任务在等待”判断，也不影响 foreground Chat 准入。
3. 保留 per-user/per-lane single-flight，不产生同一用户的并发 profile 蒸馏。
4. 明确区分瞬时、输出形状、provider 配置、source 数据和内部错误，避免永久错误形成重试风暴。
5. profile 失败和自动重试始终为后台行为，不生成聊天气泡或终态错误提示。

## 非目标

- 不修改 Dream 的夜间窗口、23 小时最短间隔、最少新增卡片数或 `force_dream` 语义。
- 不修改 profile 的 7 天成功态陈旧地板、MEMORY/USER 字符上限、prompt 或注入位置。
- 不恢复 semantic conversation compaction，也不改变 PR #187 的 deterministic coverage 设计。
- 不改变三池 lane 归属；profile 继续属于 heavy pool。
- 不为所有 lane 一次性设计通用工作流引擎；本批只提供足够安全的延迟领取原语，并只接入 profile。

## 方案选择

### 采用：`agent_jobs.available_at` 持久化延迟领取

为 `agent_jobs` 增加 `available_at timestamptz NOT NULL DEFAULT now()`。profile 瞬时失败后保留同一 Job 行，把它从 owner-held 状态原子地转回延迟 `pending`。worker 只有在 `available_at <= clock_timestamp()` 时才能 claim。

这个方案的优点是：退避状态与 Job 所有权在同一个持久层；部署或进程崩溃不会丢定时器；不需要周期扫描全部 V2 用户；single-flight 自然继续生效。

### 未采用：扫描 profile blob

让 scheduler 周期扫描缺失或到期的 `v2_agent_profile` 可以避免修改 claim 协议，但会随用户数增长反复扫描 JSONB，并把“何时可执行”同时放在 blob 和 scheduler 两处，边界不清晰。

### 未采用：进程内 asyncio timer

进程内 timer 改动小，但 worker 重启、watchdog 替换和部署都会丢失，不满足长期语义层的可靠性要求。

## 数据模型与队列语义

### Schema

新增字段：

```sql
ALTER TABLE agent_jobs
ADD COLUMN available_at timestamptz NOT NULL DEFAULT now();
```

新增仅覆盖 `status='pending'` 的 `available_at` 部分索引，支持到期过滤。现有行通过默认值立即可领取，不需要业务数据回填。

`available_at` 表示“最早允许领取时间”，不是 queue deadline。延迟等待时间不得消耗运行 lease，也不得让 reaper 把 Job 判为排队超时。

### Claim

`claim_next_job` 的 candidate、orphan probe 和最终 locked-row revalidation 三处必须使用同一条件：

```sql
j.status = 'pending' AND j.available_at <= clock_timestamp()
```

三处少改任何一处都会产生“候选认为可领取、最终更新不一致”或 orphan 误退休。priority 和 `created_at` 的既有排序保持不变。

### 原子延迟重排

新增 jobs-store 原语：

```python
reschedule_owned_job(
    job_id: int,
    *,
    claimed_by: str,
    error: str,
    available_at: float,
) -> bool
```

它按既有 `runtime_state -> agent_jobs` 锁顺序验证：

- Job 仍由 `claimed_by` 持有；
- 状态仍为 `claimed` 或 `running`；
- lease 未过期；
- 用户仍处于同一 V2 runtime generation。

成功后在一个事务内：

- `status='pending'`；
- `attempt_count=attempt_count+1`；
- 写入 content-free `last_error`；
- 写入 `available_at`；
- 清除 `claimed_by`、`claimed_at`、`started_at`、`lease_expires_at`、`deadline_at` 和 `finished_at`。

返回 `False` 表示 ownership 或 generation 已丢失；调用方不得补写第二个 Job。

### Single-flight 与显式触发

延迟 `pending` 仍属于活动 profile Job，因此普通 post-chat enqueue 只会 coalesce，不得把 `available_at` 提前。

Dream 的现有 `force=True` 和 provider 配置成功变更属于显式强制触发。它们可以把已存在的延迟 profile Job 原子地调整为 `available_at=now()`；这保持 Dream 现有“完成后立即刷新 profile”的语义，不改变 Dream 的频率和准入闸。

## Profile 重试决策

新增纯决策对象和 helper，输入只能是 content-free error class/reject code、前一次 retry family/attempts 和当前时间，输出如下四类之一：

```python
ProfileRetryDecision(
    disposition: Literal[
        "scheduled", "provider_config", "source_change", "terminal"
    ],
    retry_family: Literal[
        "transient", "shape", "provider_config", "source", "terminal"
    ],
    retry_attempts: int,
    retry_not_before: float,
    reason: str,
)
```

### 1. Provider 瞬时失败

当可靠 provider wrapper 在单 Job 内完成既有 3 次尝试后，异常携带 `feedling_error_class='transient_exhausted'`，profile 使用跨 Job 指数退避：5 分钟、10 分钟、20 分钟，直到 6 小时封顶。该类继续持久化自动重试，以便网络、429 或 5xx 恢复后无需用户活动即可成功。

### 2. 模型输出形状失败

`reply_not_json`、`missing_field:*`、`field_empty:*`、`placeholder_detected:*`、`memory_chars_over_budget:*`、`user_chars_over_budget:*` 和其他现有 shape reject 允许最多 3 次跨 Job 自动重试。这里不改变 `profile.generate_profile` 内已有的一次即时纠形。

超过上限后写 `retry_disposition='terminal'`，不再创建定时重试；后续 Dream 强制刷新仍可发起新的自然尝试。

### 3. Provider 配置失败

当异常携带 `feedling_error_class='provider_config'`，或 provider resolution 在请求前失败时，写 `retry_disposition='provider_config'`，不定时重试。401/402/403、无 key、余额不足和配置错误需要用户修复，后台重放只会额外消耗资源。

provider 配置成功变更后显式 ready/enqueue 一次 profile，确保修复配置后无需等待 7 天陈旧地板。

### 4. Source/数据失败

`profile_source_exceeds_budget:*`、卡片索引/读取不完整、计数不一致和明确的数据契约错误写 `retry_disposition='source_change'`。只有 `memory_profile_source_stats` 的卡数或 `max_updated_at` 相对失败 witness 发生变化，post-chat 到期检查才重新入队；Dream 强制刷新保持现有行为。

### 5. Ownership 与内部错误

- `RuntimeModeChanged`、`LostJobLease`、取消和 watchdog kill 不由 profile 生成新的 retry Job，继续交给 generation fence、reaper 和精确 Job 恢复路径。
- 未识别的内部异常默认 `terminal` 并产生 content-free 告警，不按网络抖动无限重试。

## Profile 文档状态

`v2_agent_profile.last_attempt` 向后兼容增加：

```json
{
  "attempts": 2,
  "reject_code": "profile_generation_failed:providererror",
  "retry_disposition": "scheduled",
  "retry_family": "transient",
  "retry_attempts": 2,
  "retry_not_before": 1785000000.0
}
```

既有 `attempts` 保留累计生成次数语义，不参与有界重试判断。`retry_attempts` 只统计当前连续失败家族；`retry_family` 取 `transient`、`shape`、`provider_config`、`source`、`terminal` 或空字符串。成功/empty 时两者清空归零；失败家族改变时从 1 重新计数。这样一个历史上成功刷新过很多次的用户，不会因为累计 `attempts` 很大而在第一次 shape 失败时直接熔断。

- `scheduled`：数据库中应存在一个延迟 profile Job。
- `provider_config`：等待 provider 配置成功变更。
- `source_change`：等待 Garden source witness 变化。
- `terminal`：停止自动重试，等待显式触发或人工调查。

旧文档没有 `retry_disposition` 时保持现有兼容行为。成功生成或确认 Garden 为空后清空 reject、retry disposition 和 retry time。

## 崩溃与竞态处理

profile 先通过现有 CAS 写失败元数据，再调用 `reschedule_owned_job`。若进程在两步之间崩溃，owner-held Job 的 lease 会由现有 reaper/watchdog 精确恢复，不会永久丢失。

为避免这种恢复路径绕过 blob 中已经写入的未来退避时间，`_run_profile` 在调用 provider 前读取已有 `last_attempt`：如果 disposition 为 `scheduled` 且 `retry_not_before > now`，只把 Job 重新延迟到该时间，不读取 Garden、不调用 provider。

CAS winner、runtime generation 和 lease fence 继续优先于 retry 决策。陈旧 owner 不得覆盖新 profile，也不得制造第二个活动 Job。

## 与现有触发器的关系

### Post-chat

每轮 Chat 后继续调用 `_enqueue_profile_if_due`：

- profile 缺失时补生成；
- 成功 profile 未满 7 天时只做 strict blob read，直接返回；
- 满 7 天且 Garden 变化时入队；
- `source_change` 只有 source witness 变化才入队；
- 已有 delayed pending Job 时只 coalesce，不提前执行。

### Dream

Dream 的调度、用户开关、夜间窗口、最短间隔、新卡阈值和 `force=True` 全部保持不变。Dream 成功后仍立即触发 profile；若已有 delayed profile Job，则显式把它调为 ready-now，而不是创建并发 Job。

### Provider 配置变更

provider 配置成功写入后增加 best-effort profile ready/enqueue。失败只记录日志，不能让配置 API 本身失败；single-flight 处理并发触发。

## Watchdog、准入和可观测性

- `pending_job_count()` 的 watchdog/claimable 口径只统计 `available_at <= now()` 的 pending Job。
- aggregate Admin 仍可统计所有活动行，但 pool queue metrics 必须拆成 `pending_ready` 和 `pending_delayed`；为兼容现有消费者，旧 `pending` 字段保留为 `pending_ready` 的别名。
- `oldest_pending_sec` 只针对 ready Job，不能让一个等待 6 小时的 profile 显示成 heavy pool 排队 6 小时。
- delayed profile 不进入 foreground admission；现有 foreground lane filter 保持不变。
- trajectory 和日志记录 retry disposition、attempt、delay 与 content-free error code；不得记录 Memory Garden 文本、provider 原始响应、key 或用户标识以外的内容。
- profile Job 状态不得产生聊天气泡、fallback reply 或“稍后再发一次”的用户话术。

## 测试设计

### Jobs store 与迁移

1. 迁移后既有 pending Job 立即可领取。
2. future `available_at` Job 在 candidate、orphan 和 locked-row 三条路径均不可领取。
3. 时间到达后同一行可正常 claim。
4. `reschedule_owned_job` 只允许当前 owner + 有效 lease + 同 generation。
5. 重排会清理 ownership 字段、增加 attempt，并保留 single-flight。
6. runtime rollback 后延迟 Job被 supersede，不执行 provider。

### Retry policy

1. `transient_exhausted` 写 5 分钟起步、6 小时封顶的延迟。
2. provider config 不生成 delayed Job。
3. shape reject 在前三次自动重排，超过上限终态。
4. source failure 只有 Garden witness 变化才重新入队。
5. unknown/internal error 不进入无限 retry。
6. 成功和 empty state 清空 retry metadata。

### Watchdog 与三池

1. 只有 delayed profile 时，`pending_job_count()` 返回 0，watchdog 不杀 slot。
2. heavy queue metrics 同时报告 ready=0、delayed=1。
3. delayed profile 不占 heavy slot；foreground Chat 正常 claim 和完成。
4. 多 worker/多触发器竞态下仍只有一个活动 profile Job。

### PR #187 组合回归

1. profile `ok` 时 prompt 注入 MEMORY/USER，conversation summary 继续被抑制。
2. profile 暂时失败不影响当前 Chat 回复。
3. deterministic coverage 不调用 semantic compaction/provider。
4. profile 自动恢复后下一轮 Chat 使用恢复后的 MEMORY/USER。

## Test 环境验证

1. PR #187 先合入 `test`，本优化分支再 rebase 最新 `test`。
2. 使用本地 Postgres 跑 jobs-store、profile、watchdog、三池和 PR #187 相关定向套件，再跑后端完整矩阵。
3. 部署 test，创建专用 V2 用户。
4. 注入可恢复的 transient provider failure，确认 Chat 正常、profile Job 进入 delayed pending。
5. 不继续聊天，等待到期后确认 profile 自动恢复为 `ok`。
6. 连续 Chat 与 Dream，确认无重复活动 profile Job、无 foreground 延迟回归。
7. 检查子进程数量、watchdog、`progress pipe closed`、ready/delayed queue metrics 和 profile trajectory。
8. 清理测试账号。prod/pre 不改配置、不部署。

## 文档与发布

这是 Runtime V2 长期语义可靠性和后台 provider 调用行为的变化。实现 PR 需要：

- 更新内部 `docs/RUNTIME_V2_FLOWS.md` 的 profile 失败恢复流程；
- 在 public changelog 的 `Unreleased` 记录持久化、有界、分类重试；
- 检查 self-hosting profile/provider 使用说明，明确瞬时失败可在用户无活动时后台重试；
- 若无公共 API schema 变化，不重新生成 OpenAPI；仍运行 OpenAPI contract tests，以及 docs-site 的 types、lint 和 build。

## 验收标准

- transient profile 失败在无用户活动、worker 重启后仍能到点恢复。
- shape 自动重试有明确上限；provider config/source/internal 错误不会形成定时重试风暴。
- delayed profile 不可 claim、不占 slot、不触发 watchdog、不污染 ready queue latency。
- Dream 和 post-chat 的既有产品语义保持不变。
- per-user single-flight、lease、runtime generation 和 CAS fence 全部保持成立。
- PR #187 的 deterministic-only coverage 与 profile 自动恢复组合测试通过。
- test 环境真实 V2 用户验证通过前，prod 不部署。
