# 新注册 Model API 用户默认进入 Runtime V2 — 设计 spec

日期：2026-08-10
状态：已批准，待实现
适用范围：Hosted Model API 路线；resident 与 official import 不参与

## 1. 背景与目标

当前托管运行时处于 `dual` 共存模式：每个账号由
`v2_user_allowlist` 的期望态和 `v2_runtime_state` 的 ownership fence 决定走
resident V1 还是 Hosted Runtime V2。三套环境的
`FEEDLING_RUNTIME_DEFAULT_DESIRED` 仍为 `resident`，因此没有显式控制记录的账号
默认留在 V1。

本设计让一个明确 UTC 时间点之后注册、并成功配置 Model API 托管路线的账号默认直接
进入 V2，同时满足：

1. cutoff 之前注册的存量账号不会被自动迁移；
2. resident 路线完全不受影响；
3. 人工 allowlist 控制高于自动 cohort 默认；
4. 用户第一次可发送的托管聊天就由 V2 执行，不经历 resident 窗口；
5. 现有双向 fence、generation、wake schedule 和 reconciler 仍是唯一运行时切换机制。

非目标：

- 不把全局 `FEEDLING_RUNTIME_DEFAULT_DESIRED` 改成 `v2`；
- 不迁移 cutoff 之前注册的用户；
- 不改变独立 resident consumer 的执行方式；
- 不引入百分比或随机分桶；
- 不借此退役 V1。

## 2. 配置与 cohort 定义

新增环境变量：

```text
FEEDLING_V2_NEW_USER_CUTOFF=<UTC ISO-8601 timestamp>
```

cohort 判定同时满足：

1. `users.created_at` 可按现有用户时间口径严格解析；
2. `users.created_at >= FEEDLING_V2_NEW_USER_CUTOFF`；
3. 用户已有成功测试并激活的 Model API route；
4. 没有优先级更高的人工 runtime allowlist 记录。

cutoff 未配置、为空、格式非法，或用户注册时间不可可信解析时，判定结果均为“不自动
切换”，账号保持 resident。不能使用镜像构建时间、进程启动时间或首次 setup 时间代替
注册时间。

`FEEDLING_RUNTIME_DEFAULT_DESIRED` 保持 `resident`。现有全局 `v2` 默认会把所有未
显式 pin 的存量托管用户纳入迁移，因此不用于本需求。

## 3. 控制记录与优先级

沿用 `v2_user_allowlist`，不新增数据库迁移。自动生成的记录采用：

```text
desired    = v2
updated_by = new-user-cohort
note       = registered-at-or-after:<normalized-cutoff>
```

优先级从高到低：

1. 人工或用户路线选择产生的显式 `resident` / `v2` 记录；
2. `updated_by=new-user-cohort` 的自动记录；
3. 无记录时的全局 resident 默认。

cohort helper 只在记录不存在时创建自动记录；它不得覆盖任何非
`new-user-cohort` 来源的记录。已有自动 `desired=v2` 记录时，helper 可以继续推动未完成
的 V2 fence 收敛，但不得重复创建记录或无意义增加 runtime generation。

用户主动切换到 resident 路线时，控制面必须立即写入显式
`desired=resident`，再复用现有反向切换流程把 fence 收回 resident。这样 reconciler
不会因遗留的自动 V2 记录把用户重新翻回 V2。

## 4. Model API setup 数据流

`POST /v1/model_api/setup` 保持现有 provider 验证和配置写入顺序：

1. 校验 provider、model、base URL 与 context window；
2. 测试 provider key；
3. 持久化 credential；
4. 持久化并激活 route；
5. 初始化 model API runtime profile；
6. 应用新用户 cohort policy；
7. 保存 onboarding route 并返回成功。

第 6 步只在 active route 已落盘后执行，因为 V2 fence 的现有前置条件就是可用的
Model API route。它在 setup 已有的跨进程 per-user config mutation 临界区内完成，读取：

- PostgreSQL `users.created_at`；
- 当前单用户 allowlist 记录及 `updated_by`；
- 当前 `(hosted_runtime_mode, hosted_runtime_state, runtime_generation)`。

命中 cohort 后，helper 写入自动 allowlist 记录，并复用现有 V2 切换实现完成 wake
schedule seed、ownership fence 和 generation 更新。不得另写一套直接 UPDATE fence 的
捷径。

setup 只有在 active route 与 V2 ownership 都持久化成功后才返回 200，因此用户第一次
聊天不会先被 resident 接走。重复 setup、换模型或换 key 时，如果 runtime 已在 V2，
不得重复翻转 generation。

## 5. 并发与失败语义

cohort policy 与现有 model API 配置修改共用
`hosted_runtime_config_mutation_lock(user_id)`。实现时必须复用适合“锁已经持有”场景的
内部 transition helper，避免递归获取同一控制锁。

并发优先级以串行化后的持久状态为准：

- admin 先写显式 pin：后到的 cohort helper 看见人工来源后不覆盖；
- cohort 先写自动 V2、admin 后写 resident：admin 最终结果为准，reconciler 收敛到
  resident；
- 两个重复 setup：最多创建一条自动控制记录，第二个请求看到已收敛状态并保持幂等。

route 已激活但自动 V2 切换失败时：

- setup 返回 `503 runtime_policy_unavailable`，不报告配置完成；
- 自动 `desired=v2` 记录保留，供 reconciler 与后续 setup 重试继续收敛；
- 不偷偷回落 V1，也不清理已经验证成功的 provider route；
- 客户端重试必须能在不重复 credential/route 的情况下完成收敛。

worker readiness 继续由现有 V2 admission gate 管理。worker 不可用不会改变 ownership，
首次 chat 按现有契约返回明确的 V2 unavailable 错误，不回落到 resident。

## 6. Resident 与其他路线

- 注册阶段不写 V2 allowlist，也不翻 runtime fence；此时用户还没有 V2 的 active route
  前置条件。
- resident 注册、resident onboarding、独立 resident consumer 和 official import 均不
  调用 cohort helper。
- 只有成功激活 Model API route 才触发 cohort 判定。
- cutoff 后注册但先使用 resident、之后主动配置 Model API 的账号，在首次成功 setup 时
  属于新 cohort；如果其间已有显式 resident pin，则人工 pin 优先，不自动翻 V2。

## 7. 可观测性与运维

现有 runtime allowlist reconciliation view 应能按 `updated_by=new-user-cohort` 筛出自动
cohort，并继续展示 `desired`、实际 mode/state/generation 与 `converged`。

至少记录以下不含敏感信息的结构化事件或等价计数：

- cohort eligible / ineligible（原因仅限 before-cutoff、no-cutoff、invalid-time、
  explicit-pin）；
- 自动控制记录 created / already-present；
- fence converged / convergence-failed；
- setup 因 cohort policy 返回 503 的次数。

发布后重点观察：

- 自动 cohort 的 desired/actual 收敛率；
- V2 首轮真实回复成功率与 terminal error；
- worker capacity、pending/oldest-job age 与 p95 latency；
- chat、proactive、Memory、MCP、图片能力的 cohort 回归。

不得记录 provider key、聊天明文或用户内容。

## 8. 回滚

停止新增自动 V2：移除 cutoff 配置或把 cutoff 调整到未来。该操作只影响尚未完成首次
Model API setup 的账号，不自动改变已经进入 V2 的 ownership。

回滚自动 cohort：只选择
`updated_by='new-user-cohort' AND desired='v2'` 的控制记录，将其显式更新为
`desired='resident'`，再由 reconciler 使用既有反向 fence 流程收敛。人工 V2 canary
和其他来源的 allowlist 记录不受影响。

单用户回滚：通过现有 admin allowlist API 写 `desired=resident`。不能通过删除自动行来
表达长期 resident pin，因为以后再次 setup 会重新满足 cohort 条件。

## 9. 测试要求

单元与集成测试至少覆盖：

| 场景 | 预期 |
|---|---|
| cutoff 前注册，cutoff 后首次配置 Model API | resident |
| cutoff 后注册并配置 Model API | setup 成功时 mode/state 已为 V2 |
| cutoff 后注册但选择 resident | resident，且无自动 V2 记录 |
| 新用户已有人工 resident pin | setup 不覆盖 |
| 新用户已有人工 V2 pin | 幂等收敛 V2 |
| 已有自动 V2 记录但 fence 未收敛 | setup 重试继续收敛 |
| cutoff 缺失、非法或 created_at 异常 | fail-safe resident |
| 重复 setup、换 model、换 key | 不重复增加 generation |
| V2 用户切换 resident | 写 resident pin，reconciler 不翻回 |
| 并发 setup 与 admin pin | 无双跑，最终人工控制优先 |
| V2 transition 失败 | setup 503，route 保留，重试可恢复 |

E2E 必须从真实注册开始，分别验证：

1. 新 Model API 用户第一次加密 chat 由 V2 worker 接收并产出一条回复；
2. 同期新 resident 用户继续由 V1 路线处理；
3. cutoff 前的 Model API 用户行为不变；
4. 自动 V2 用户反向 pin resident 后消息不丢、不双跑。

## 10. 上线顺序

1. 先部署支持代码，不设置 cutoff，确认所有环境行为不变。
2. test 设置明确 UTC cutoff，完成新 Model API、resident、存量和回滚矩阵，并跑真实加密
   首轮。
3. pre 使用新的 UTC cutoff 泡测，验证 chat、proactive、Memory、MCP、图片、队列与
   terminal error。
4. prod 所有相关 backend/worker 版本部署并通过 readiness gate 后，再设置 prod cutoff。
   cutoff 取明确的业务生效时刻，不取构建或进程启动时间。
5. 上线后按自动 cohort 来源观察收敛与首轮质量；异常时先停止新增，再只回滚自动来源。

该变更会修改用户可见的 onboarding/runtime 行为。实现时必须同步
`docs-site/content/docs/` 中相关 architecture/workflow 文档，并在公开 changelog 的
`Unreleased` 下记录；如公开 API 契约发生变化，还需同步 OpenAPI 与契约测试。
