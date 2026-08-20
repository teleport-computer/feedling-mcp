# Test → Pre TEE-primary 同步设计

**日期：** 2026-08-14

**目标分支：** `pre`

**输入分支：** `test`

## 背景

`test` 与 `pre` 不是简单的快进关系。`pre` 保留 TEE-primary 数据库、明文/密文双档和专属发布闸；`test` 在共同祖先之后增加了 Runtime V2 三池隔离、worker slot 恢复、工具目录、wake 观测和首次聊天激活等改动。直接把 `pre` 重置为 `test` 会丢掉 pre 专属能力；直接合并后部署，则会让新运行时代码访问 TEE 主库中尚不存在的表和列。

## 决策

从当前 `origin/pre` 创建集成提交，正常合并 `origin/test`。冲突遵循以下原则：

1. 保留 pre 的 TEE-primary、明文/密文双档、preflight 和相关回归测试。
2. 接受 test 的 Runtime V2 三池、工具面、诊断、CPU recorder 和稳定性修复。
3. 文档与 changelog 合并双方条目，不用任一侧整体覆盖另一侧。
4. 所有数据库变化同时维护 RDS 与 TEE 两条迁移链；pre 部署只在 TEE 链达到新 head 后进行。

## 数据库迁移

### RDS 链

test 的新链为：

`0085_v2_wake_shadow_decisions → 0086_v2_worker_pool_heartbeats → 0087_v2_first_chat_activation`

pre 的现有链以 `0086_merge_voice_wake` 为 head。新增无数据变更的 merge revision，将 `0086_merge_voice_wake` 与 `0087_v2_first_chat_activation` 汇合为唯一 head。既有数据库无论来自 test 还是 pre 路径，都能线性升级。

### TEE 链

pre 实际运行 `FEEDLING_DATABASE_SCHEMA=tee`，线上 `DATABASE_URL` 指向 TEE Postgres，因此要在 `0017_voice_primary_alignment` 后新增三项等价迁移：

1. 创建 content-free 的 `v2_wake_shadow_decisions` 表及索引。
2. 为 `v2_worker_heartbeats` 增加 `pool`、`runtime_state` 与组合索引。
3. 按与 RDS 相同的有界 SQL 回填 `proactive_settings.first_chat_ok_at`。

最后一个 TEE migration 更新 `phase4_primary_prepared.tee_heads` 为新唯一 head，使 preflight、启动检查和 `tee-migrate` 对同一版本达成一致。迁移使用 `IF EXISTS` / `IF NOT EXISTS` 保持幂等；回填不覆盖已有激活时间；downgrade 不删除无法判定来源的激活标记。

## 冲突处理

- `.github/workflows/ci.yml`：同时保留 pre 明文边界测试和 test 新增诊断测试。
- `backend/tee_shadow/table_registry.py`：加入 test 新表的分类，同时保留 pre 现有 TEE 资产分类。TEE-primary 下不启用 dual-write，但注册表仍须与可部署拓扑一致。
- V2 migration/profile 测试：改为断言新双链 head，并保留双方新增的 content-free 轨迹事件。
- `tools/e2e/client.py`：明文账号继续发送 owner-bound plaintext envelope；密文账号继续 seal；两种路径都记录失败定位 ID。
- 两份 changelog：保留 pre 明文能力与 test Runtime V2 发布记录。

## 验证

提交前至少完成：

1. Alembic RDS/TEE 单 head 检查，以及 migration contract 测试。
2. pre runtime preflight、严格 YAML、TEE schema/registry 测试。
3. Runtime V2 三池、slot restart、首次聊天激活、profile、工具目录测试。
4. pre 明文/密文边界与二进制媒体回归测试。
5. 带本地 Postgres 的相关 DB 测试；不能把缺少 DB 导致的跳过当成通过。
6. 公共文档 `types:check`、`lint`、`build` 和 OpenAPI contract 测试，因为本次包含架构、信任边界和部署拓扑文档变化。

## 发布顺序与失败处理

1. 合并并验证集成分支，确认 RDS 与 TEE 都只有一个代码 head。
2. 将最终集成提交推进 `pre`。现有 `TEE migrate` workflow 会按目标环境强制检出 `pre`，因此迁移文件必须先存在于该远端分支。
3. 允许此次 pre 发布在 `Require PRE TEE schema at release head` 闸门按预期停止。该闸门位于 `phala deploy` 之前，失败时不会修改 main 或 runner CVM，线上继续运行旧版本。
4. 从新的 `pre` ref 运行 pre TEE migration workflow，使数据库升级到新唯一 TEE head，并核对 workflow 的 code/db head 断言。
5. 重跑被 schema 闸门拦住的 pre CI/deploy jobs；不得跳过或删除 preflight。
6. 等待 GitHub Actions 完成，核对 backend、enclave、serve-worker 与 runner 镜像 SHA 一致。
7. 检查 `https://pre-api.feedling.app/healthz`、容器健康、wake bus、worker pool 心跳和一次不含用户内容的 Runtime V2 冒烟。

首次 schema preflight 失败是上述顺序中预期的发布闸门，不算部署异常；除这一处外，任何迁移、镜像发布或 preflight 失败都停止后续步骤。数据库迁移以向前修复为主，不回滚可能已被新代码写入的数据列；应用部署失败时保持当前 `pre` 容器版本，不手工绕过 GitHub Actions 或 preflight。

## 非目标

- 不修改感知权限聚合或模型感知工具选择行为。
- 不改 prod、main 或 iOS 分支。
- 不清理历史 worktree、旧部署提交或无关文档。
