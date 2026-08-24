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
