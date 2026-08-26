---
document_lifecycle: current
canonical_owner: self
---
# Storage/TEE 历史文档审计

本页记录 storage/TEE 历史材料的归档依据。它只说明历史材料如何使用；当前数据库
拓扑、迁移步骤和信任边界仍以
[`CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md`](../CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md)、
[`deploy/DEPLOYMENTS.md`](../../deploy/DEPLOYMENTS.md)、对应 release 的 workflow/compose、
运行代码和目标环境的 exact deployed commit 为准。

## 批次 1：已退役的 RDS → TEE Phase 2–3 shadow plan

审计日期：2026-08-26。结论：将文首已明确标记 `RETIRED / DO NOT DEPLOY` 的
[Phase 2–3 implementation plan](../archive/superpowers/plans/2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md)
归档为 `historical` / `superseded`；由 current
[TEE-primary migration runbook](../CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md)
接管现行操作说明。

| 原文档 | 生命周期 / current owner | 仓库引用方 | 实现 / 拓扑证据 | 当前兼容义务 | 归档位置 |
|---|---|---|---|---|---|
| 2026-07-07 TEE Postgres Phase 2–3 shadow/dual-write implementation plan | `historical` / `superseded`；[TEE-primary migration runbook](../CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md) | 归档前精确全路径搜索无命中；basename 搜索只命中 [2026-07-04 migration design](../superpowers/specs/2026-07-04-tee-postgres-migration-design.md) 的“已 RETIRED”说明。没有生产代码、部署配置、workflow 或 current runbook 把该 plan 当作执行入口 | `08afdcc0` 一次性交付初版 `alembic_tee`、`tee_shadow`、`tee_replicator`、admin/CI 接线和配套测试；`8f3c4603` 在 hosted supervisor topology 退役后给 plan 加上 `RETIRED / DO NOT DEPLOY`；test 在 `82c4c019` 已把 TEE 数据库扶正为 primary | 保留显式 gate 下的 legacy RDS→TEE fail-open mirror、异步 decrypt/replication、owner/app 凭证分离、混合明文/密文 reader、TEE migration 历史与回滚/验证能力；不得把当前 TEE→plaintext projection 当作旧 shadow 的延续 | [archive plan](../archive/superpowers/plans/2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md) |

## 当前 owner 与方向边界

- test 自 2026-08-18 的 `82c4c019` 起使用 TEE primary：
  [`DEPLOYMENTS.md`](../../deploy/DEPLOYMENTS.md) 记录
  `TEST_DATABASE_URL` 为 TEE `app` role DSN、`FEEDLING_DATABASE_SCHEMA=tee`，旧的
  dual-write secret 已移除。仓库描述不能替代 live `/healthz` 和 exact release 校验。
- [`CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md`](../CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md)
  接管环境隔离、mixed-row 行形、发布顺序、Phase 4、回滚和验证操作。
- workflow/compose topology gate 与 [`backend/db.py`](../../backend/db.py) 接管
  `rds|tee` primary selector。TEE primary 模式使用 `DATABASE_URL`，不能继续把
  `TEE_DATABASE_URL` 当作同一库的第二连接。
- [`backend/tee_shadow/`](../../backend/tee_shadow/) 与
  [`backend/tee_replicator/`](../../backend/tee_replicator/) 仍承载迁移、补偿、校验和
  兼容职责；[`backend/admin/tee_sync_scheduler.py`](../../backend/admin/tee_sync_scheduler.py)
  只在 legacy mirror 的显式条件满足时工作；[`backend/alembic_tee/`](../../backend/alembic_tee/)
  是现行 TEE schema 历史，不能按普通旧代码删除。
- 当前 post-promotion plaintext shadow 是 **TEE primary → 独立 plaintext projection**。
  它由 `PLAINTEXT_SHADOW_DATABASE_URL` 与独立 gate 控制，目标不是 failover source；这和
  历史 plan 的 **RDS primary → TEE shadow** 方向相反。

## 兼容与信任边界

- legacy mirror 只有在 primary selector 仍为 RDS、`FEEDLING_TEE_DUAL_WRITE=1` 且
  `TEE_DATABASE_URL` 存在时才启用；mirror 保持 fail-open，不把影子库故障传播到主写路径。
- 密文解密复制保持异步，不能把 enclave decrypt 放回业务热路径；加密账号和历史混合行仍按
  current runbook 的 row-shape 规则读取。
- migration owner DSN 与 runtime app DSN 继续分离；schema/Alembic 历史、加密信封、
  tenant ownership、备份和恢复义务不因计划归档而消失。
- 归档只移走过时的执行清单。它不授权删除 `tee_shadow`、`tee_replicator`、scheduler、
  migration、recovery、verification、deployment 或 trust-boundary 代码。

## Rationale transfer 与后续

current migration runbook 接管仍有效的迁移不变量、mixed-version 兼容、TEE-primary
扶正和 plaintext shadow 边界；本审计页保存“为什么旧 Phase 2–3 不能再部署”以及
实现/退役证据。部分退役的 2026-07-04 总设计仍留在默认位置：其中 Phase 0–1 基建理由
和早期替代方案仍有价值，待后续独立批次逐条转移理由后再决定生命周期，不能随本批归档。
