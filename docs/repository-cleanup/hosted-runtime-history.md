---
document_lifecycle: current
canonical_owner: self
---
# Hosted runtime 历史文档审计

本页记录阶段 4 的 hosted runtime 分批归档证据。它只说明历史材料如何使用，
不替代 [`docs/CURRENT_STATE.md`](../CURRENT_STATE.md)、实际 compose、运行代码或
环境证据。

## 批次 1：V2-only 单向切换与关闭 Resident

审计日期：2026-08-24。结论：`archive`。

这四份 2026-07-09 文档共同假设“用户单向迁往 V2，最终关闭 hosted Resident”。
2026-07-21 接受的
[`dual-runtime` 决策](../superpowers/specs/2026-07-21-dual-runtime-v1-v2-coexistence-design.md)
明确反转了该前提：per-user fence 支持 Resident/V2 双向切换，Resident supervisor
和回滚路径在共存期仍是生产组成部分。当前 compose 与
[`CURRENT_STATE.md`](../CURRENT_STATE.md) 继续把 `dual` 记录为部署事实。

| 原文档 | 状态与当前 owner | 仓库引用方 | 实现/运行证据 | 兼容义务 | 归档位置 |
|---|---|---|---|---|---|
| D0 rollout plan | `superseded`；dual-runtime decision | 仅与配套 D0 spec 互引 | `backend/hosted/runtime_reconciler.py`、`backend/alembic/versions/0052_dual_runtime_coexistence.py` 和 dual-runtime 路由测试 | 保留 per-user fence、generation、draining 与双向回滚 | [`archive/.../D0 plan`](../archive/superpowers/plans/2026-07-09-hosted-runtime-v2-D0-rollout-infrastructure.md) |
| D0 rollout design | `superseded`；dual-runtime decision | 仅配套 D0 plan | 同上；环境 compose 是 policy 的最终仓库事实 | 不得按旧文档删除 roster、selector 或 Resident 控制面 | [`archive/.../D0 design`](../archive/superpowers/specs/2026-07-09-hosted-runtime-v2-D0-rollout-infrastructure-design.md) |
| D4 kill-resident plan | `superseded`；dual-runtime decision | load-test driver 的操作说明已迁到 `scripts/loadtest/README.md` | `tests/test_dual_runtime_send_routing.py`、`tests/test_hosted_runtime_policy.py` 与 runner topology 测试 | 保留 hosted Resident；VPS resident 是另一条独立且受保护的分发边界 | [`archive/.../D4 plan`](../archive/superpowers/plans/2026-07-09-hosted-runtime-v2-D4-loadtest-rollout-killresident.md) |
| D4 kill-resident design | `superseded`；dual-runtime decision | 仅配套 D4 plan | 同上；当前部署不能执行 kill-resident runbook | load-test 结果不能单独授权移除 Resident 或回滚路径 | [`archive/.../D4 design`](../archive/superpowers/specs/2026-07-09-hosted-runtime-v2-D4-loadtest-rollout-killresident-design.md) |

## Rationale transfer

- 双跑风险、per-user ownership、排空、generation fence、失败模式与双向回滚已经由
  dual-runtime decision 接管。
- 当前运行入口和事实优先级已经由 `CURRENT_STATE.md` 接管。
- 仍有用的 load-test 运行方式、模拟边界和 CVM 证据要求已经迁到
  [`scripts/loadtest/README.md`](../../scripts/loadtest/README.md)。
- 本批次不修改运行代码、数据库、compose 或 `tools/chat_resident_consumer.py`。

## 引用检查

归档前，D0 plan/spec 只有彼此引用；D4 plan/spec 除彼此引用外，
`scripts/loadtest/run_loadtest.py` 与 `scripts/loadtest/compare_tokens.py` 都有继承的
操作/比较指引。前者现在指向当前 README；后者现在指向当前 README 和 token baseline，
不再把 archived D4 Task 5 当作运行手册。生成的生命周期清单会随新路径重建，不视为人工调用者。

## 批次 2：retired staged-planner 与 PR-C unified-loop 谱系

审计日期：2026-08-24。结论：archive 三份被统一 loop 取代的设计/计划，另 archive 一份
实现该 unified loop 的已落地 PR-C implementation plan；保留中间 agent-loop 设计作为被后续
多模态设计引用的决策考古；PR-C design 是当前 `decision`。
在归档前复核 `backend/model_api_runtime/v2/tool_loop.py`：它声明“一套 loop 供每个模型使用”、
没有 `is_official` 分支；`worker.py` 的 chat、wake 与 child 三个入口均直接调用
`v2_tool_loop.run_tool_loop`。`tests/test_v2_p0_unified_loop.py` 以及 focused tool-loop/
worker/wake 测试覆盖真实 loop、worker 和 executor 的行为，而非仅以零引用作判断。

| 原文档 | 状态与当前 owner | 仓库引用方 / backlinks | 实现证据 | 兼容义务 | archive / retain 路径 |
|---|---|---|---|---|---|
| A+B+C short-planner design | `superseded`；PR-C unified-loop decision | 07-09 merge-conditions backlog 已改链到 archive；配套 C plan 同批归档 | `tool_loop.py` provider-native catalog/round loop；chat 与 wake 由 worker 直调 | 不得把 `planner → executor → forced responder` 当作当前 V2 控制流 | [`archive spec`](../archive/superpowers/specs/2026-07-08-hosted-runtime-v2-abc-design.md) |
| C action-queue planner plan | `superseded`；PR-C unified-loop decision | 仅指向同批 archive spec 的历史内部链接 | 同上；当前 dispatcher/tool results 由 unified loop 驱动 | 历史 BYOK 与有界执行说明不授权恢复 tiered planner | [`archive plan`](../archive/superpowers/plans/2026-07-08-hosted-runtime-v2-C-action-queue-planner.md) |
| Intermediate agent-loop implementation plan | `superseded`；PR-C unified-loop decision | 指向保留的 07-10 design；无生产代码调用 plan | worker 现行 chat/wake/child 均调用 `run_tool_loop`，非 `agent_loop.run_turn` | 不得按 `final_response` sentinel 或独立 responder 重建当前路径 | [`archive plan`](../archive/superpowers/plans/2026-07-10-hosted-runtime-v2-agent-loop.md) |
| PR-C unified-loop implementation plan | `implemented`；PR-C unified-loop decision | 仅历史实现记录；不作为生产执行入口 | `tool_loop.py`、worker 直调和 P0 unified-loop tests 仍匹配其核心决策 | 保留为已实现步骤的审计记录，当前决策以 design 为准 | [`archive plan`](../archive/superpowers/plans/2026-07-13-hosted-runtime-v2-PR-C-unified-tool-loop.md) |
| Intermediate agent-loop design | `superseded`；PR-C unified-loop decision | [`07-10 multimodal design`](../superpowers/specs/2026-07-10-hosted-runtime-v2-multimodal-design.md) 仍引用其 §12 决策 2 | 当前目录排除 `chat_image_read` 的模型可见 tool surface，与 PR-C catalog 取代旧 JSON planner 一致 | 原地保留该被引用的「先止血、multimodal 另立一轮」理由；不可静默归档 | retain: [`07-10 design`](../superpowers/specs/2026-07-10-hosted-runtime-v2-agent-loop-design.md) |
| PR-C unified-loop design | `decision`；`canonical_owner: self` | 上述 historical 文档 owner；PR-D 等后续设计以它的 unified-loop 接口为前提 | 统一 provider-native loop、无 provider tiering、chat/wake 同一入口均由代码与 tests 证实 | 继续作为 staged-planner 谱系的 current owner；V1/V2/Resident 的切换与回滚仍由 dual-runtime 决策和控制面约束 | retain: [`PR-C design`](../superpowers/specs/2026-07-13-hosted-runtime-v2-PR-C-unified-tool-loop-design.md) |

## 批次 2 rationale transfer

- PR-C design 接管「所有模型共用 provider-native loop、无 `is_official` 行为分层、无 tool call
  的文本即终态回复、chat/wake 同一 loop」这些仍在生产实现的核心决定。
- 07-10 agent-loop design 的多模态拆分理由仍被 07-10 multimodal design 精确引用，所以只改为
  historical/superseded 并留在原位置；没有把该部分理由搬走或丢失。
- 本批次只移动/分类文档和更新 Markdown backlink；不改 runtime、部署、数据库、API、测试，且
  `tools/chat_resident_consumer.py` 保持零 diff。V1/V2/Resident 的 coexistence、切换和回滚
  义务仍归 07-21 dual-runtime decision 与当前控制面事实。

## 批次 2 引用检查

所有原始精确路径已重写为 archive 路径或在归档文档内改为可解析的相对链接；basename 搜索只保留
有效 archive、retained design 与审计说明。生产 Python 未引用 implementation plan；归档记录可被
文档引用，但不作为运行指引。

## 批次 3：PR-A effect foundation 与 PR-B provider transport/telemetry

审计日期：2026-08-24。结论：两份 design 的核心决策均仍由当前代码和聚焦测试支撑，保留为
`decision` / `canonical_owner: self`；两份 implementation plan 已被实现，改为 `historical` /
`implemented` 并归档。设计中的部署门槛、旧目录与当时的实现快照均明确为历史基线，不能覆盖
[`CURRENT_STATE.md`](../CURRENT_STATE.md) 或现行 dual-runtime 控制面。

| 原文档 | 状态与当前 owner | 仓库引用方 / backlinks | 实现 / 测试证据 | 兼容义务 | archive / retain 路径 |
|---|---|---|---|---|---|
| PR-A effect-foundation design | `decision`; self | 配套 PR-A plan（现为 archive historical record）；后续 PR-C design 以其 outbox 接口为前提 | `backend/model_api_runtime/v2/effect_outbox.py`、`cutover.py`、`cursor.py`、`jobs_store.py`、`backend/db.py` 与 `backend/hosted/chat_send_core.py`；`tests/test_v2_p0_exactly_once.py`、`test_v2_p0_aba.py`、`test_v2_p0_seq_integrity.py`、`test_v2_send_enqueue_atomic.py` | 保留 generation fencing、draining、seq cursor、重放幂等与原子 send/enqueue；不得破坏 Resident/V2 双向切换或 rollback | retain: [PR-A design](../superpowers/specs/2026-07-12-hosted-runtime-v2-PR-A-effect-foundation-design.md) |
| PR-A effect-foundation plan | `historical` / `implemented`; PR-A design | 仅历史实现记录；原 source-spec backlink 仍指向 retained design，无生产调用者 | 同上；P0 replay、ABA 与相同 timestamp 的 seq integrity 覆盖计划的基础保证 | 历史任务步骤不构成运行手册；持久化 effect、cursor 和 generation contract 仍由 retained design / code / tests 约束 | [archive plan](../archive/superpowers/plans/2026-07-12-hosted-runtime-v2-PR-A-effect-foundation.md) |
| PR-B provider transport/telemetry design | `decision`; self | 配套 PR-B plan（现为 archive historical record）；PR-C unified-loop decision 消费其 transport | `backend/provider_client.py` 的 Bedrock codec（约 `:1744`）及 sync/async routing（约 `:3990` / `:4826`）、`backend/provider_types.py`、`backend/model_api_runtime/v2/jobs_store.py`、migration `0029_v2_turn_metrics_whole_turn.py`；four-wire baseline tests 加 `tests/test_provider_tools_bedrock.py` | 原 2026-07 设计是四 wire baseline；当前须保持五个活跃 native provider wire families（OpenAI Chat/Responses、Anthropic、Gemini、Bedrock）的 text dict compatibility、tool-call identity/transcript codec、async、usage normalization 与每 job telemetry 幂等；provider 失败与工具 schema fallback 不得改变既有语义 | retain: [PR-B design](../superpowers/specs/2026-07-13-hosted-runtime-v2-PR-B-provider-transport-telemetry-design.md) |
| PR-B provider transport/telemetry plan | `historical` / `implemented`; PR-B design | 仅历史实现记录；无生产代码把该 plan 当 operating documentation | 计划的 four-wire call-id acceptance 属于已落地历史边界；后续 Bedrock native tool/parallel-call/transcript/sync-async 覆盖见 `tests/test_provider_tools_bedrock.py` | 历史 plan 不授权回退至 text-only transport 或 append-only metric，也不能把其四 wire scope 误作当前上限；PR-C loop 仍须使用 retained decision 的五-wire 兼容接口 | [archive plan](../archive/superpowers/plans/2026-07-13-hosted-runtime-v2-PR-B-provider-transport-telemetry.md) |

### 批次 3 rationale transfer

- PR-A retained decision 接管 effect outbox、generation fence、seq cursor 和 send/enqueue
  一致性的长期理由；当前 Resident/V2 coexistence 的最终选择仍由 dual-runtime decision 管理。
- PR-B retained decision 接管 provider-native transport、tool codec、dict-return compatibility、
  native async 及 idempotent whole-turn telemetry；原 four-wire 设计 baseline 已由后续 Bedrock
  扩展为当前五个活跃 native provider wire families。PR-C 是该 transport 的当前消费者，而非替代者。
- 本批次只分类、归档和修复 Markdown 生命周期材料；没有修改 runtime、部署、数据库、API、wire/
  security contract、测试或 `tools/chat_resident_consumer.py`。

### 批次 3 引用检查

精确路径与 basename 搜索确认：PR-A 的 source-spec backlink 继续解析到 retained design；两份
moved plan 的所有外部可解析链接均使用 archive 路径或 retained decision。生产 Python 没有引用
implementation plan；archive 的任务步骤仅保留可追溯性，不是 operating documentation。

## 批次 4：PR-D pool/history safety 与三池单 slot 隔离

审计日期：2026-08-24。结论：保留两份 design 为 `decision` / `canonical_owner: self`，并将
两份已落地 implementation plan 移入 archive，标为 `historical` / `implemented`。PR-D 的
共享多 slot child、整池 watchdog 重启与其原始 `MAX_WORKERS=4` 快照已经被三池决策取代；
其 progress clock、owner-fenced lease/write recovery、effect-outbox 幂等、live kill switch、
seq cursor、prompt coverage、durable source retention、CAS-loss requeue 与 reconcile sweeper
仍是当前必须保持的安全义务。

| 原文档 | 状态与当前 owner | 仓库引用方 / backlinks | 实现 / 测试 / 部署证据 | 兼容义务 | archive / retain 路径 |
|---|---|---|---|---|---|
| PR-D pool/history design | `decision`; self；共享 child/整池重启部分由三池决策取代 | `watchdog.py`、`child_supervisor.py`、`serve_worker.py` 及 P0 tests 已改链到 retained decisions；配套 plan 为 historical record | `watchdog.py` 的 stall/absolute 与 capacity-zero→kill→recover 顺序；`kill_switch.py` 和 `chat_send_core.py` 的 live halt；`cursor.py`、`worker.py`、`db.py` 的 seq/coverage/requeue/reconcile；`test_v2_watchdog.py`、`test_v2_kill_switch.py`、`test_v2_p0_history_safety.py`、`test_v2_compaction_cas_requeue.py`、`test_v2_reconcile_sweeper.py` | 保留 Genesis 隔离、generation/owner fence、effect idempotency、durable raw source、prompt coverage 和 re-drive 语义；不可恢复 shared-child topology | retain: [PR-D design](../superpowers/specs/2026-07-13-hosted-runtime-v2-PR-D-pool-history-safety-design.md) |
| PR-D pool/history plan | `historical` / `implemented`; PR-D design | 无生产 Python implementation-plan backlink；历史步骤仅留存 | 上述当前代码与 focused safety tests 证明核心步骤已落地 | 任务清单不是运行手册；拓扑部分须服从三池决策 | [archive plan](../archive/superpowers/plans/2026-07-13-hosted-runtime-v2-PR-D-pool-history-safety.md) |
| Three-pool slot-isolation design | `decision`; self | PR-D retained decision 将被替代的故障域明确委派给它；配套 plan 为 historical record | `pool_config.py` 默认 `4/2/2` 与 lane allowlists；`pool_supervisor.py` / `slot_protocol.py` / `turn_child.py` 每 `SlotSpec` 一进程；`serve_worker.py` parent fleet；`test_v2_pool_config.py`、`test_v2_pool_supervisor.py`、`test_v2_child_supervisor.py`、`test_v2_pool_fault_injection.py`、`test_v2_watchdog.py` | 三个逻辑池与 per-slot process 是当前 topology；Chat preemption、即时且 owner-fenced recovery、资源预算不得倒退；V1/V2/Resident coexistence、per-user switching、rollback 与 VPS self-update 不受本批次改变 | retain: [three-pool design](../superpowers/specs/2026-08-14-runtime-v2-three-pool-slot-isolation-design.md) |
| Three-pool slot-isolation plan | `historical` / `implemented`; three-pool design | 无生产消费者；部署/rollout 叙述只作为 point-in-time evidence | `deploy/docker-compose.phala.test.yaml` 明确覆写为 `2/1/1`；其他 compose 未在本批次被当作 live slot-count 证据；代码默认仍为 `4/2/2` | 历史 test/pre/prod 数量、机器规格和 rollout gate 不授权当前操作；live 环境须以 `release.git_commit` 和该 SHA compose 核对 | [archive plan](../archive/superpowers/plans/2026-08-14-runtime-v2-three-pool-slot-isolation.md) |

### 批次 4 partial-supersession rationale

- PR-D 向三池决策只转移故障域：从「一个 child 承载多个 slot、杀整个 child」改为三个 pool、
  每 slot 一个 child、只 kill/recover 该 slot 的 active claim。进度时钟和健康容量语义继续存在，
  但按 per-slot 实现。
- PR-D 的 kill switch、lease/write fence、outbox idempotency、seq/reply cursor、history
  coverage/catch-up、raw source retention、CAS-loss requeue 与 orphan reconcile 不因拓扑替换而
  失效，仍由 retained PR-D decision 和当前代码/tests 约束。
- `pool_config.py` 的 `4/2/2` 是代码默认，test compose 的 `2/1/1` 是环境覆写；两者不能推断
  pre/prod 的当前运行数量。当前运行与部署事实仍由 `CURRENT_STATE.md`、exact deployed SHA 和
  对应 compose 共同决定。
- 本批次仅变更 lifecycle Markdown 与 Python/test 注释或 docstring；不修改运行行为、测试逻辑、
  数据库、compose、public API/wire/security contract 或 `tools/chat_resident_consumer.py`。

### 批次 4 引用检查

精确路径和 basename 搜索确认生产 Python 不再引用任一 implementation plan；测试中的 invariant
说明直接指向 retained decision。所有 moved-plan 链接使用 archive 路径，设计之间的
partial-supersession 链接保持可解析。生成的 lifecycle inventory 由检查器重新生成。

## 批次 5：admission-ceiling 历史合同与当前 queue telemetry

审计日期：2026-08-24。结论：保留 2026-07-09 design 为 `decision` /
`canonical_owner: self`，并将已实现的配套 implementation plan 归档为 `historical` /
`implemented`。原先“超过 SLA 在持久化前返回 503 `busy`”的产品合同已由
`7c08413a0d3657f6fd1214767bba4011959e77fd` 有意取代；当前 public Chat workflow
规定 live-but-overloaded pool 仍须接收消息，容量估算只作 fail-open telemetry。

| 原文档 | 状态与当前 owner | 仓库引用方 / backlinks | 实现 / 测试证据 | 兼容义务 | archive / retain 路径 |
|---|---|---|---|---|---|
| §6 admission-ceiling design | `decision`; self；顶部 current reconciliation 取代旧 503 作为可执行结论 | 配套 plan 改为 archive historical record；本审计页记录 retain/archive 决定 | [`chat_send_core.py`](../../backend/hosted/chat_send_core.py) 对 foreground capacity、`chat`/`manual_wake` in-flight、recent chat mean 只记录 `admission_over_sla`；[`test_chat_send_v2_enqueue.py`](../../tests/test_chat_send_v2_enqueue.py) 覆盖 overload 后仍 202、原子入库/enqueue、coalesce/preemption 与估算异常 fail-open | live overload 不得在持久化前丢弃用户输入；foreground scope 必须只计 `chat`/`manual_wake`；`workers_alive`、runtime control、kill switch、provider/config failures 保持独立 fail-closed gates | retain: [design](../superpowers/specs/2026-07-09-hosted-runtime-v2-D-admission-ceiling-design.md) |
| §6 admission-ceiling implementation plan | `historical` / `implemented`; retained design | 无生产实现调用者；所有 task/checklist 均明确为不可执行历史记录 | [`admission.py`](../../backend/model_api_runtime/v2/admission.py)、[`jobs_store.py`](../../backend/model_api_runtime/v2/jobs_store.py)、[`test_v2_admission.py`](../../tests/test_v2_admission.py) 与 [`test_v2_jobs_store.py`](../../tests/test_v2_jobs_store.py) 保留估算及 queue metrics 的实现证据 | 历史 `busy` response、旧路径、旧 line numbers 与 NO-COMMIT/worktree 指令不得被当作 current runbook；当前对外承诺以 [Chat workflow](../../docs-site/content/docs/workflows/chat.mdx) 为准 | [archive plan](../archive/superpowers/plans/2026-07-09-hosted-runtime-v2-D-admission-ceiling.md) |

### 批次 5 rationale transfer

- 保留的 design 仍说明为什么 queue estimate 是纯函数、foreground-only 的近似及其失败模式；
  但其顶部 reconciliation 明确将 admission 阈值改为 telemetry classification，不能再驱动
  pre-persistence 503。
- `live_worker_capacity(within_sec=30, pool="foreground")`、`chat`/`manual_wake` 的
  in-flight scope 和 recent-`chat` mean 是当前估算输入；容量估算或其 DB 读取失败时必须
  fail open。此点不削弱 liveness、runtime-control、kill-switch 或 provider/configuration
  的 fail-closed 边界。
- `isolation_events.admission_rejects.over_sla` 是 legacy-shaped 的 zero/default metrics
  label，不从 `admission_over_sla` telemetry 取值，且不表示消息被拒绝；任何改名或移除
  都是单独的 wire-compatible cleanup，不属于本批次。
- 本批次变更 lifecycle Markdown、生成的 inventory、active parity 表述，并作一项文档层
  API error-contract 修正（删除已失效的 live-overload `busy` / 503 行）及指定 Python/test
  注释或 docstring 的诊断用语规范；不修改 runtime、数据库、schema、compose、API/wire/
  security 行为、公开 `docs-site/`、`deploy/` 或
  `tools/chat_resident_consumer.py`。

### 批次 5 引用检查

精确路径和 basename 搜索应只留下 retained design、archive plan 与本审计记录；archive plan
到 retained design 的 owner link 可解析。当前 public behavior 的来源不迁入或修改
`docs-site/`，而是继续由 [Chat workflow](../../docs-site/content/docs/workflows/chat.mdx) 说明。
审查复核同时移除了 [`API_ERRORS.md`](../API_ERRORS.md) 中已失效的 live-overload
`busy` / 503 / `queue_over_sla` 行，避免将 telemetry 误发布为 HTTP error contract。
当前 [`HOSTED_RUNTIME_V2_PARITY_MATRIX.md`](../HOSTED_RUNTIME_V2_PARITY_MATRIX.md) 也将
该能力表述为 queue-wait/capacity telemetry，并明确 over-SLA 不会在持久化前返回 503 或拒绝。
