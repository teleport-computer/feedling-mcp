# Test → Pre Runtime V2 第二轮同步设计

**日期：** 2026-08-15

**目标分支：** `pre`

**输入分支：** `test`

**冻结基线：** `origin/pre@19ef79ee`，`origin/test@79c130b6`

## 目标

把上一轮同步之后进入 `test` 的 Runtime V2、记忆、管理诊断、CI 安全分层和 profile durable retry 变更推进到 `pre`，同时保持 pre 已上线的 TEE-primary、明文/密文双档、双 enclave 入口和双 CVM 发布契约。

本轮是一次新的受控 promotion，不重置或改写任一远程分支。合并开始后以冻结的 `origin/test@79c130b6` 为输入；如果远程 `test` 再前进，新增提交留到下一轮，不在本轮中途追赶。

## 输入变化

相对上一轮共同基线 `6cad3ed2`，本轮 test 输入包含以下实质变化：

- Runtime V2 wake 轮禁止删除记忆、主动沉默输出消歧和写权限 provenance 加固。
- Dream 首卡预算、profile 卡完整内容保留和约 1000 字的记忆卡写入指导。
- content-free admin memory diagnostics，以及相关公开文档和回归测试。
- `seed_user` 测试隔离、pytest uncovered baseline 收缩和三层 CI 安全覆盖。
- mailbox `post.sh --ack` 原子归档行为。
- profile durable retry：持久化 retry 状态、延迟任务 claim fence、恢复/刷新/watchdog 路径。

该批次引入 RDS migration `0088_agent_jobs_available_at`，但 test 不包含与 pre 对应的 TEE migration。因此不能直接合并后部署。

## 合并策略

### 选择：保留 test migration，新增汇合 head

保留 test 的 `0088_agent_jobs_available_at` 文件和 revision ID 原样，在 pre 新增无数据变更的 RDS merge revision `0089_merge_pre_test_agent_jobs`：

```text
0086_merge_voice_wake ─┐
                      ├─ 0088_merge_pre_test_heads ─┐
0087_first_chat ──────┘                             ├─ 0089_merge_pre_test_agent_jobs
0087_first_chat ─────── 0088_agent_jobs_available_at ┘
```

`0089` 的 `down_revision` 同时引用 `0088_merge_pre_test_heads` 和 `0088_agent_jobs_available_at`。当前 pre 数据库已经位于前者；升级到 `0089` 时，Alembic 会先执行尚未执行的 agent-jobs sibling migration，再写入唯一 merge head。

### 未选择方案

1. **把 test 的 0088 改名为 pre 的 0089：** 线性直观，但会让同一逻辑在 test/pre 使用不同 revision ID，未来同步和审计持续漂移。
2. **让两个 0088 head 长期并存：** 少一个文件，但违反部署和启动代码要求唯一 Alembic head 的约束。
3. **把 pre 重置或 rebase 到 test：** 会丢失 pre 的 TEE-primary、明文路径、双入口和发布门禁历史。

## TEE-primary 迁移

新增 `backend/alembic_tee/versions/0021_agent_jobs_available_at.py`，线性接在 `0020_v2_first_chat_activation` 后：

- 使用与 RDS `0088_agent_jobs_available_at` 完全一致的 `_UP` SQL。
- 为 `agent_jobs` 增加非空 `available_at TIMESTAMPTZ DEFAULT now()`。
- 创建 pending jobs 的 `(available_at, priority DESC, created_at)` 部分索引。
- 把 `phase4_primary_prepared.tee_heads` 更新为 `0021_agent_jobs_available_at`。
- downgrade 继续遵循 TEE 链约定：不原地删除生产数据结构，要求从备份恢复。

迁移 contract 测试必须逐字比较 RDS `_UP` 与 TEE `_UP`，避免两条数据库链出现行为差异。

## 冲突处理

无落盘合并预演确认有三个实际内容冲突：

1. `tests/test_v2_jobs_migration.py`
   - 最终断言 RDS 唯一 head 为 `0089_merge_pre_test_agent_jobs`。
   - 保留 pre 对既有双链汇合拓扑的断言。
   - 加入 test 对 `available_at` 列、默认值、索引和 durable retry 数据结构的断言。
2. `tests/test_v2_profile_cards.py`
   - 接受 test 已发布的“完整 profile card 内容到达 provider request”行为。
   - 删除与新行为相反的旧固定截断断言。
   - 保留不冲突的 provider trace 和身份卡顺序断言。
3. `tests/test_v2_profile_storage.py`
   - 保留 pre 的 plaintext profile document shape 回归。
   - 合入 test 的 durable retry metadata 规范化与持久化回归。

以下文件虽然 Git 可自动合并，仍按高风险文件人工复核：

- `.github/workflows/ci.yml`：保留 pre TEE startup contract、plaintext suites 和 migration convergence suite，同时接受 test 新增的安全分层及 profile retry suites。
- `backend/model_api_runtime/v2/serve_worker.py`、`worker.py`、`jobs_store.py`：确认 plaintext 路由、完整 profile 内容和 durable retry 同时成立。
- 两份 changelog 与公开文档：保留 pre 信任边界记录并接受 test 新增行为说明。
- test 专属 compose 文件只接受 test 镜像 pin 更新，不改 pre compose；pre 的 ingress、enclave-domain、serve-worker 和 runner 拓扑保持不变。

## 不可回退边界

1. pre 应用继续使用 `FEEDLING_DATABASE_SCHEMA=tee`，启动时验证 TEE 唯一 head、phase-4 marker 和关键触发器。
2. 用户级明文/密文双档继续按行形状路由；plaintext profile、聊天、记忆、媒体和感知路径不得重新进入 enclave 解密路径。
3. `pre-enclave.feedling.app` 继续为 `attested_ingress`，Phala `-5003s` 入口继续为 `direct_tls` + 客户端证书指纹固定。
4. 主 CVM 继续承载 backend、双 enclave entrypoint、ingress 和 pooled serve-worker；独立 runner CVM 只运行 V1 agent-runner。
5. 不修改 `main`、`prod`、iOS 或本轮冻结点之后新增的 test 提交。

## 验证

### 合并前基线

在 `origin/pre@19ef79ee` worktree 使用本地 PostgreSQL 运行完整测试：

```text
9703 passed, 3 skipped, 9 xfailed, 3 subtests passed
```

### 合并后聚焦验证

- RDS/TEE Alembic 唯一 head、真实 PostgreSQL upgrade 和 SQL parity。
- durable retry 的 jobs migration/store、profile retry/refresh/lane/storage、watchdog 和 route activation。
- profile card 完整内容、plaintext profile shape 和 provider request trace。
- pre runtime preflight、TEE schema、phase-4 cutover、严格 YAML 与部署门禁测试。
- plaintext chat/history/memory/media/perception 和 enclave strict-boundary suites。
- pytest discovery ratchet、三层安全 suites 和 CI workflow contract。

### 完整与公开文档验证

- 使用本地 PostgreSQL 运行完整 `tests`，不把缺少数据库导致的跳过视为通过。
- OpenAPI contract tests。
- `docs-site` 的 `openapi:generate`、`types:check`、`lint` 和 `build`；生成结果必须复核 diff。

## 发布顺序

1. 合并冻结的 `origin/test@79c130b6`，解决三个冲突并提交集成结果。
2. 完成代码审查与全部本地验证，确认工作树干净。
3. 推送最终提交到 `pre`；镜像发布可以继续，但 pre schema preflight 应在 Phala mutation 前阻止旧 TEE head 部署。
4. 从新的 pre ref 运行 `TEE migrate`，把数据库从 `0020` 升到 `0021`，并验证应用启动契约。
5. 重新运行被 schema gate 拦截的 pre 部署，依次更新主 CVM、canary、runner CVM 和链上 compose hash。
6. 核对 API、自定义 enclave、Phala 直连入口、镜像 SHA、容器 restart count、wake bus 和 worker 状态。

除预期的首次 schema preflight 拒绝之外，任一测试、迁移、镜像或部署失败都会停止后续步骤。不得绕过 preflight，不得用本地拼装的 `main` 或强推替代审查流程。

## 非目标

- 不修改 profile durable retry 的产品语义，只把 test 已验证的实现安全推进 pre。
- 不在本轮追加远程 test 后续提交。
- 不进行用户数据内容检查或数据库写入式排障。
- 不清理上一轮 worktree、历史分支或部署提交。
