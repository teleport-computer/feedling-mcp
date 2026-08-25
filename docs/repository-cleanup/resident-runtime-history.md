---
document_lifecycle: current
canonical_owner: self
---
# Resident runtime 历史文档审计

本页记录 Resident runtime 历史材料的归档依据。它只说明历史材料如何使用；当前
运行状态仍以 [`docs/CURRENT_STATE.md`](../CURRENT_STATE.md)、对应 compose、运行代码和
环境的 exact deployed commit 为准。

## 批次 1：已实现的 dual-runtime coexistence implementation plan

审计日期：2026-08-26。结论：将已交付的 implementation plan 归档为
`historical` / `implemented`；保留 [dual-runtime design](../superpowers/specs/2026-07-21-dual-runtime-v1-v2-coexistence-design.md)
作为 current decision。

| 原文档 | 生命周期 / current owner | 仓库调用方 | 实现 / 测试证据 | 兼容义务 | 归档位置 |
|---|---|---|---|---|---|
| 2026-07-21 dual-runtime V1/V2 coexistence implementation plan | `historical` / `implemented`；[dual-runtime design](../superpowers/specs/2026-07-21-dual-runtime-v1-v2-coexistence-design.md) | 归档前精确路径和 basename 搜索仅命中本次未提交 batch plan 的归档步骤；没有生产代码、部署配置或 current runbook 消费该 plan | migration [`0052_dual_runtime_coexistence.py`](../../backend/alembic/versions/0052_dual_runtime_coexistence.py) 恢复 Resident supervisor 状态并建立 allowlist；[`runtime_reconciler.py`](../../backend/hosted/runtime_reconciler.py) 以 per-user fence/generation 向 desired 收敛；[`test_dual_runtime_coexistence.py`](../../tests/test_dual_runtime_coexistence.py)、[`test_dual_runtime_send_routing.py`](../../tests/test_dual_runtime_send_routing.py)、[`test_runtime_reconciler.py`](../../tests/test_runtime_reconciler.py)、[`test_dual_runtime_flip_no_loss.py`](../../tests/test_dual_runtime_flip_no_loss.py) 覆盖共存、路由、reconcile 与双向 flip；[`test_agent_runtime_resident_contract.py`](../../tests/test_agent_runtime_resident_contract.py) 与 [`test_chat_resident_self_update.py`](../../tests/test_chat_resident_self_update.py) 保留 hosted Resident consumer 契约 | 主 compose 当前为 `FEEDLING_HOSTED_RUNTIME_POLICY=dual`，默认 desired 为 `resident`；每用户 `(mode, state, generation)` fence 是路由真相，`draining` fail-closed；allowlist 变更必须保持 Resident ↔ V2 的双向 rollback；V1 hosted Resident 继续运行在独立 runner CVM；用户自建环境继续由 [`tools/chat_resident_consumer.py`](../../tools/chat_resident_consumer.py) 承担受保护的 self-hosted consumer 分发边界 | [archive plan](../archive/superpowers/plans/2026-07-21-dual-runtime-v1-v2-coexistence.md) |

## 已落地证据

- `bda95682` 交付 allowlist reconciler、admin 控制面与 leader-elected
  后台接线；其 `reconcile_once()` 只让过渡暂停，send 热路径仍只读 fence。
- `db178b0f` 修复从 allowlist 移除用户后不能回到 Resident 的缺口，确保
  默认 `resident` 的 canary 阶段仍能完成 V2 → Resident rollback。
- `5b8fee70` 收紧 V2 ownership race；与 reconciler 的锁内重新读取
  desired/fence 配合，避免并发设置、allowlist 写入与启动补偿留下错误所有权。

这些 commit 是实施证据，不是 current runbook。当前 compose 的 `dual` policy、默认
`resident` desired、独立 runner CVM 和 self-hosted consumer 边界由
[`CURRENT_STATE.md`](../CURRENT_STATE.md) 及对应部署文件说明；实际环境仍须先核对
`/healthz` 的 `release.git_commit`。

## Rationale transfer

- 保留的 dual-runtime design 接管 per-user allowlist、fence/generation、draining、两条
  执行路径和双向回滚的决策理由；历史任务清单不再构成操作说明。
- `CURRENT_STATE.md` 接管当前运行时选择和拓扑：main CVM 的 pooled Runtime V2 与独立
  runner CVM 的 hosted Resident 同时存在，且 user VPS 的 consumer 保持单文件分发边界。
- 归档不授权删除 migration `0052_dual_runtime_coexistence`、Resident supervisor、
  reconciler、rollback 控制面或 `tools/chat_resident_consumer.py`；这些义务必须由当前
  design、代码、测试和部署证据共同满足。

## 引用检查

归档前已执行精确全路径和 basename 搜索（排除生成的 lifecycle inventory）。结果只包含
本次 untracked batch plan 对归档操作本身的描述；没有 caller 将 implementation plan 当作
当前执行手册。保留的 design 引用有效，且 archive record 只用于追溯，不替代 current
decision 或部署说明。
