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
| 2026-07-07 TEE Postgres Phase 2–3 shadow/dual-write implementation plan | `historical` / `superseded`；[TEE-primary migration runbook](../CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md) | 归档前精确全路径搜索无命中；basename 搜索只命中现已归档的 [2026-07-04 migration design](../archive/superpowers/specs/2026-07-04-tee-postgres-migration-design.md) 的“已 RETIRED”说明。没有生产代码、部署配置、workflow 或 current runbook 把该 plan 当作执行入口 | `08afdcc0` 一次性交付初版 `alembic_tee`、`tee_shadow`、`tee_replicator`、admin/CI 接线和配套测试；`8f3c4603` 在 hosted supervisor topology 退役后给 plan 加上 `RETIRED / DO NOT DEPLOY`；test 在 `82c4c019` 已把 TEE 数据库扶正为 primary | 保留显式 gate 下的 legacy RDS→TEE fail-open mirror、异步 decrypt/replication、owner/app 凭证分离、混合明文/密文 reader、TEE migration 历史与回滚/验证能力；不得把当前 TEE→plaintext projection 当作旧 shadow 的延续 | [archive plan](../archive/superpowers/plans/2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md) |

## 批次 2：已实施的 TEE Postgres Phase 0–1 基建计划

审计日期：2026-08-27。结论：将一次性的 spike 与 pg CVM 基建实施清单归档为
`historical` / `implemented`；由 current
[TEE Postgres CVM 运维手册](../TEE_POSTGRES_SHADOW_PROVISIONING.md) 接管开通、TLS、角色、
备份、迁移与恢复操作。同步移除 deployment record 对旧 Phase/Task 编号的依赖，避免
agent 把已经交付的项目清单当作当前执行入口。

| 原文档 | 生命周期 / current owner | 仓库引用方 | 实现 / 拓扑证据 | 当前兼容义务 | 归档位置 |
|---|---|---|---|---|---|
| 2026-07-07 TEE Postgres Phase 0–1 spike + pg CVM 基建实施计划 | `historical` / `implemented`；[TEE Postgres CVM 运维手册](../TEE_POSTGRES_SHADOW_PROVISIONING.md) | 归档前精确全路径搜索只命中 [`deploy/DEPLOYMENTS.md`](../../deploy/DEPLOYMENTS.md) 的 Task 编号索引；没有 runtime、workflow、compose 或 migration 代码以该 plan 为入口。批次 2 已把该索引改为 current owner 和长期运维约束 | `08afdcc0` 同一交付提交加入该计划、`deploy/postgres/`、PG compose/workflow、`alembic_tee` 和首批 shadow/replicator 实现；`64bcc96a` 建立可复制的 provisioning runbook；test 在 `82c4c019` 扶正 TEE primary | 独立 CVM/KMS 身份、direct TLS、owner/app 凭证分离、checkout HEAD 的 immutable image、WAL-G 在装载数据前 fail-closed、`WALG_LIBSODIUM_KEY` 与 TLS `ca.key` 的平台外冷存、环境前缀隔离、redeploy 携带完整 secret 集、跨 worker LISTEN/NOTIFY 与 idle/reconnect 验收、restore 必须等到 `pg_is_in_recovery() = false`、schema head 从 exact release 推导；不得删除 `deploy/postgres/`、`backend/core/wake_bus.py` 或 `backend/alembic_tee/` | [archive plan](../archive/superpowers/plans/2026-07-07-tee-pg-phase0-1-infra.md) |

批次 2 同时修正了 current owner 内的时间边界：RDS primary → TEE shadow 双写/回填与
2026-07-29 migration-secret 缺口均标为历史阶段；固定 revision、表数、容量和备份计数均
标为 point-in-time evidence。CVM 存在不代表该环境已经扶正，也不代表 legacy mirror
仍开启；必须按 live 配置与 exact deployed release 判定。

## 批次 3：被双轨内容模型取代的 2026-07-04 总设计

审计日期：2026-08-27。结论：将“所有用户内容最终明文化并删除整个信封层”的早期总设计
归档为 `historical` / `superseded`。Phase 0–1 的 PG CVM 基建已经由 current
[TEE Postgres CVM 运维手册](../TEE_POSTGRES_SHADOW_PROVISIONING.md) 接管；内容行形、
客户端兼容、TEE-primary 扶正/回滚和 post-promotion plaintext shadow 由 current
[migration runbook](../CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md) 接管。

| 原文档 | 生命周期 / current owner | 仓库引用方 | 设计 / 实现证据 | 当前兼容义务 | 归档位置 |
|---|---|---|---|---|---|
| 2026-07-04 TEE 内明文 Postgres 迁移设计 | `historical` / `superseded`；[migration runbook](../CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md)，PG 运维细节由 [provisioning guide](../TEE_POSTGRES_SHADOW_PROVISIONING.md) 持有 | 归档前精确搜索只命中两个已归档 implementation plan、历史 changelog/审计与 cleanup batch plan；没有 runtime、workflow、compose、current runbook 或 public docs 把它当作现行入口 | `051221af` 创建初始设计；`08afdcc0` 交付 Phase 0–3 初版；`5b297f79` 标记部分退役；test 在 `82c4c019` 扶正 TEE primary，同时保留 mixed encrypted/plaintext 支持 | 保留 per-user 加密选择、`local_only` 加密、旧客户端密文写入、row-shape reader、历史 ciphertext、provider/runtime credential envelope、rewrap/兼容路由、独立 PG CVM/direct TLS、角色分离、WAL-G、LISTEN/NOTIFY 与 R2 双形态；归档不授权删除这些 surface | [archive design](../archive/superpowers/specs/2026-07-04-tee-postgres-migration-design.md) |

### D1–D4 决策结果

| 决策 | 2026-07-04 提案 | 当前结果 / owner |
|---|---|---|
| D1 `local_only` 与全明文终态 | 取消 `local_only`，存量由设备重传，否则接受丢弃 | **否决**。`local_only` 始终要求加密；显式 encrypted tier 继续存在，未知用户 fail-closed 到加密。见 current migration runbook invariants |
| D2 数据库拓扑 | 独立 PG CVM，native Postgres wire over direct TLS，以保留连接池和 LISTEN/NOTIFY | **采纳并修正**。保留独立 CVM、direct TLS、角色分离、WAL-G 和 wake bus；`--kms phala` 下 PG CVM 使用部署账号 KMS 授权，不创建或复用主 app 的链上 AppAuth。见 current provisioning guide |
| D3 旧客户端切换 | iOS 强更；切读后旧信封写返回 400/426 | **否决**。plaintext-tier 账号仍接受旧客户端 encrypted upload；读取按 row shape，而不是当前 preference；API/iOS/worker/runner 以一个 mixed-compatible release 推进。见 current migration runbook |
| D4 大对象 | frame body 保留 R2 offload，以 KMS 存储加密，并确保明文不离开 enclave | **部分采纳并重定义**。保留 R2 offload、owner/pointer/SHA-256/length 校验和 mixed-shape reader；encrypted-tier 对象仍是 ciphertext，但 `plaintext_v1` body 以 raw plaintext bytes 写入 R2，因此对象存储及其备份属于明文接收方。原提案的 KMS 存储加密和“明文不离开 enclave”假设未采纳。见 migration runbook、`backend/db.py`、`backend/object_storage.py` 与 public changelog 的信任边界说明 |

旧设计的 Phase 7 “删除 `content_encryption.py`、`core/envelope.py`、enclave decrypt、
rewrap 端点和 iOS 加密代码”已经被双轨模型取代，不是 repository cleanup 候选。只有在产品
明确取消 encrypted tier、`local_only`、历史密文和旧客户端兼容，并另行完成 API、客户端、
信任边界与数据迁移决策后，才允许重新提出这类删除。

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
- R2 同时承载 encrypted-tier ciphertext 与 `plaintext_v1` raw plaintext bytes；必须保留
  owner/pointer/SHA-256/length 校验和 mixed-shape reader。plaintext tier 的信任边界包含
  对象存储及其备份，不能继续沿用旧设计“明文不离开 enclave”的假设。
- migration owner DSN 与 runtime app DSN 继续分离；schema/Alembic 历史、加密信封、
  tenant ownership、备份和恢复义务不因计划归档而消失。
- 归档只移走过时的执行清单。它不授权删除 `tee_shadow`、`tee_replicator`、scheduler、
  migration、recovery、verification、deployment 或 trust-boundary 代码。

## Rationale transfer 与后续

current migration runbook 接管仍有效的迁移不变量、mixed-version 兼容、TEE-primary
扶正和 plaintext shadow 边界；本审计页保存“为什么旧 Phase 2–3 不能再部署”以及
实现/退役证据。current provisioning guide 接管 Phase 0–1 的长期基础设施、备份和恢复
义务；其一次性实施步骤已归档。已执行完的 cleanup 批次 1/2 计划也以 `implemented`
归档，避免其阶段性限制继续出现在 current 搜索面。2026-07-04 总设计的 D1–D4 结果已由
批次 3 转移；历史替代方案和未采纳终态保留在 archive 供追溯，不再覆盖 current owner。
