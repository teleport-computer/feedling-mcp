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

归档前，D0 plan/spec 只有彼此引用；D4 plan/spec 除彼此引用外，只有
`scripts/loadtest/run_loadtest.py` 把 D4 plan 当作操作说明。该生产代码引用已经改为
当前 README。生成的生命周期清单会随新路径重建，不视为人工调用者。
