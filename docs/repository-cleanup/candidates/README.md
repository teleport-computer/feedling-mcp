---
document_lifecycle: current
canonical_owner: self
---
# 代码简化候选（2026-08-28）

本轮只做证据收敛和实施排序，不授权从静态零引用直接删除。审计基线是
`a71e2b3d976ba931980b0bcdcfc89166052e3989`（当时的 `origin/test`）。行号和
gross 删除估算均以该提交为准，实施前必须 rebase `test` 并重跑完整引用搜索。

## 推荐顺序

| 顺序 | 候选 | 结论 | gross 删除 / net | 主要原因 |
|---|---|---|---:|---|
| 1 | [失效的 `e2e_model_api_test.py`](e2e-model-api-test.md) | `delete`（已实施） | gross 253 行；net 删除 239 行 | 当前 202 契约会被判失败，脚本最终又无条件返回成功 |
| 2 | [Route-B 旧 selector/feature flag 岛](route-b-readside-island.md) | `delete` | gross 约 250–300 行；net 待 diff | 生产已固定走统一 bucketed selector，旧实现仅剩测试消费者 |
| 3 | [`db.py` 三个零消费者叶子](db-dead-leaves.md) | `delete` | gross 约 31 行；预期 net 接近 31 行 | 无生产、测试、文档和动态调用证据 |
| 3 | [`v2_user_triage.py` 旧 semantic-compaction 诊断](v2-user-triage-semantic-compaction.md) | `delete`（局部） | gross 约 90–110 行；net 待 diff | 旧 body/char-budget 与泛化错误归因会误诊当前 metadata-only compaction |
| 4 | [Runtime V2 watchdog 旧兼容层](v2-watchdog-compatibility.md) | `delete` | production gross 约 35–50 行；net 待 diff | 仅服务旧 test-double/旧 Python 参数，需保留当前恢复顺序 |

第一项最符合“减少误导 agent 的检索面”：它是无消费者且无法判断当前协议的测试脚本，
替代入口已经存在。Route-B 候选 gross 收益也很高，但会修改 production source 和
compose，必须先把安全与召回断言迁移到当前真实路径。表中只有 gross 删除量可由当前
文件直接复算；新增/迁移门禁产生的 glue 必须以实施 PR 的最终 diff 计算，不能预先冒充 net。

## 延后到功能决策

- [旧 provider smoke harness](provider-smoke-harness.md)：`feature-decision`。其 822 行
  表面虽陈旧，但目前仍可能是唯一覆盖 Hosted Resident provider matrix、同账号切换和
  consumer respawn 的工具；canonical E2E 先补齐这些能力，才能重新评估删除。

## 本轮明确不删

- `tools/chat_resident_consumer.py`：继续作为用户 VPS 的单文件分发、自更新和 re-exec
  保护边界，不拆分。
- Alembic 与 TEE migration 历史：属于已应用持久化链，不以零调用判定可删。
- Redis 部署包：当前是审计/回滚恢复资产；是否退役需要独立运维决策和 live evidence。
- TEE reflow/prune/frames、enclave `serving.py`/`asgi_worker.py`：仍承担恢复、复制或
  TLS/attestation 信任边界。
- `scripts/audit_resident_model_routes.py`、`tools/seed_legacy_memory.py`、
  `tools/strict_yaml.py`、`scripts/provider_probe/probe.py`：分别仍承担 remediation、
  兼容 fixture、严格 YAML 校验和 provider 原始 wire 探测。
- `supports_responses` 数据库/API 字段：仍是持久化和公开 wire。Hosted Resident roster
  中的空转投影值得后续复核，但 current 测试和 changelog 明确把 roster shape 当兼容面，
  本轮不把“没有行为分支”升级为删除证明。
- `FEEDLING_V2_TURN_HARD_TIMEOUT_SEC` 部署 alias：runtime 仍读取，仓库无法证明外部
  环境/secret 未设置；需先审计并迁移 test/pre/prod 配置。

## 共通门禁

每个候选实施时都必须：

1. 从最新 `origin/test` 建独立 worktree；
2. 先写或迁移保护当前行为的测试，再删除旧表面；
3. 精确搜索符号、完整路径、CLI 名、配置键、文档和 workflow 消费者；
4. 不修改 migration 历史、持久化/wire 兼容面或 consumer 单文件边界；
5. 跑候选文档列出的本地门禁；
6. 按风险在 test 环境核对 exact deployed SHA、健康状态和真实业务回合；
7. 若测试或 live evidence 暴露额外消费者，结论退回 `retain-protected` 或
   `feature-decision`，不强行完成净删除。
