# Runtime V2 单一确定性历史覆盖设计

**日期：** 2026-08-12  
**状态：** 已获产品方向确认，待实施  
**范围：** Runtime V2 conversation compaction、部署配置、公开自托管文档

## 背景

Runtime V2 已用加密 `v2_agent_profile` 中的 `memory` 与 `user` 两个字段承载长期语义：前者描述对用户及共同经历的记忆，后者描述双方的相处和沟通方式。与此同时，旧的 conversation compaction 仍能调用用户 provider，把超出逐字回放窗口的聊天写成语义 bullet summary。

现有 `FEEDLING_V2_PROFILE_COVERAGE_DETERMINISTIC` 开关在两条写路径之间切换：关闭时写 provider 生成的语义 summary；开启时只写精确 seq/count coverage sentinel。这个双轨让同一份长期语义同时由 profile 和 conversation summary 维护，并保留了 provider 超时、输出拒绝、批次缩小和隔离等复杂失败面。

本设计删除双轨。Runtime V2 的旧聊天语义只由 MEMORY/USER profile 承担；conversation compaction 只维护有界 prompt 所需的历史覆盖证明和 watermark。

## 目标

- 删除 `FEEDLING_V2_PROFILE_COVERAGE_DETERMINISTIC` 配置项和真假分支。
- 所有新的 maintenance compaction、inline prompt catch-up 和 summary checkpoint 都只生成确定性 count sentinel。
- conversation compaction 不读取待覆盖聊天正文，也不为 leaf 或 checkpoint 调用 provider。
- 删除只服务于 semantic compaction 的实现、重试、输出验证、quarantine 和测试。
- 保留既有 model-authored immutable segments 的存储读取兼容，不迁移、不更新、不删除历史行。
- 历史 model-authored segments 参与下一次 frontier 归并时，以可信 metadata 中的 `source_message_count` 生成确定性父 sentinel；不保留子节点的语义文本到新父节点。
- 保持 raw encrypted Chat rows 为持久源账本；compaction 不授权删除聊天原文。

## 非目标

- 不修改 MEMORY/USER profile 的生成、加密、刷新周期或 prompt header。
- 不回填缺失 profile，不为 profile failure 新增长期 summary fallback。
- 不在线迁移 `v2_conversation_summary` 或 `v2_conversation_summary_segments`。
- 不改变近期逐字回放的 turn 数、tail anchor 或 coverage-hole 提示策略。
- 不删除供其他非-conversation 功能使用的通用 provider 客户端能力。

## 最终数据流

### 新 leaf

后台 maintenance 或当前回合的同步 catch-up 先从数据库读取精确 coverage metadata：`start_seq`、`end_seq` 和 `source_message_count`。它不解密、渲染或发送这批消息的正文，而是本地生成：

```text
- [<source_message_count> 条更早的消息已由长期记忆覆盖]
```

随后通过现有 CAS 写入 immutable segment，并推进 summary watermark。CAS loss 仍按现有所有权规则重试，因为它保护并发正确性，而不是 semantic compaction 特有行为。

### 新 checkpoint

frontier 超过边界时，checkpoint 根据待归并节点的 metadata 合计 `source_message_count`，生成同格式的父 sentinel。子节点可以是新的 deterministic segment，也可以是历史 model-authored segment；父节点的生成均不检查、拼接或发送子节点正文。

新 checkpoint 保留 child IDs、精确 seq 范围、coverage kind 和 source count，因此历史追溯与 frontier 完整性仍由结构化 metadata 证明。语义文本不会向新父节点传播。

### Prompt 选择

profile 状态为 `ok` 时，prompt 使用 MEMORY + USER，并继续隐藏 materialized conversation summary。profile 缺失、disabled、degraded、读取失败或解密失败时，现有选择器仍可返回 materialized summary；但随着 frontier 被重新归并，这份 summary 会逐渐只剩 count sentinel。

这是刻意接受的行为：删除 semantic compaction 后，系统不再承诺用 conversation summary 作为长期语义 fallback。profile 可靠性是长期语义完整性的唯一前置条件。

## 代码删除边界

### 删除

- worker 中 `_PROFILE_COVERAGE_DETERMINISTIC` 环境变量读取及所有条件分支。
- maintenance 和 inline catch-up 的 provider-backed `compact` / `compact_segment` 调用链。
- checkpoint 的 provider-backed fallback；混合历史 frontier 也直接 deterministic rollup。
- semantic compaction 专用的 provider progress、usage、reject、batch-shrink、provider-failure 和 quarantine 控制流。
- `compaction.py` 中只服务于 conversation semantic fold/checkpoint 的 prompts、解析器、异常和异步 provider 方法。
- test/pre/prod compose、CI matrix、自托管配置表中的该变量。
- 仅验证开关真假路径或 semantic output 的测试；由固定 deterministic contract 测试取代。

### 保留

- `deterministic_fold`，以及按 metadata 合并计数的确定性 checkpoint helper。
- immutable summary segment schema、reader、frontier selection、CAS append 与 rebalance。
- 历史 segment envelope 解密能力，因为未归并的历史节点仍可能直接出现在 materialized summary 中。
- exact coverage、watermark、tail、coverage-hole 和 raw-row retention 安全断言。
- 与 provider semantic compaction 无关的附件/voice prompt filtering 测试；若其唯一入口随 semantic compaction 删除，则改为测试实际仍存在的 prompt 路径，而不是保留死 API。

删除后，生产代码不得再暴露可从 raw chat 创建 model-authored conversation summary 的函数或配置入口。

## 错误处理与兼容性

- metadata coverage 不完整、CAS loss、lease loss、runtime mode 改变仍保持 fail-closed；不得越过未证明覆盖的 seq 推进 watermark。
- deterministic 本地渲染失败属于代码/数据契约错误，不能退回 provider。
- 历史 model-authored rows 保持 immutable；部署不需要 DDL 或数据迁移。
- mixed frontier 的 checkpoint 只信任数据库 metadata，不信任历史 summary 文本中的数字或格式。
- profile fallback 不再保证长期语义，这一变化需进入公开 changelog 和 self-hosting 说明。

## 测试策略

按 TDD 先建立会在当前双轨实现上失败的契约测试：

1. worker 模块不再读取或暴露 `FEEDLING_V2_PROFILE_COVERAGE_DETERMINISTIC`。
2. maintenance compaction 在没有该环境变量时写 deterministic sentinel，并且正文 reader/provider stub 若被调用就立即失败。
3. inline catch-up 同样只用 coverage bounds 写 sentinel，不读取正文、不调用 provider。
4. mixed frontier（历史 model-authored child + deterministic child）生成 count sentinel checkpoint，不调用 provider，并保留精确 child IDs/seq/count metadata。
5. 已有历史 segment 在尚未归并前仍能被 reader 解密和 materialize。
6. semantic compaction 的公开内部函数和专用异常不再存在；仓库运行时代码和部署配置中不再出现该环境变量。
7. raw chat retention 与 compaction watermark 不授权 GC 的既有安全测试继续通过。

验证分层进行：先跑 deterministic compaction、summary frontier、adaptive tail、history safety 和 compose contract 的定向测试，再按 `docs/testing/TESTING.md` 的后端矩阵运行带真实 Postgres 的相关套件。由于公开 self-hosting 行为改变，还需运行 docs-site 的 types、lint 和 build；若 OpenAPI 未变化，则不重新生成 OpenAPI。

## 文档与发布

- 从 self-hosting 配置表删除该环境变量，直接说明 Runtime V2 conversation maintenance 固定使用 deterministic coverage。
- 在 `Unreleased` changelog 记录：provider-backed conversation summarization 已移除，MEMORY/USER 是唯一长期语义层，raw encrypted Chat rows 仍保留。
- 更新内部 Runtime V2 flow 文档，删除 test-only/rollout gate 描述。
- 部署后无需切换 flag；新镜像启动即采用单一路径。回滚只能回滚镜像版本，不能通过环境变量恢复 semantic compaction。

## 验收标准

- Runtime V2 conversation compaction 的 leaf 和 checkpoint 新写均为本地确定性 sentinel。
- 任何 compaction 路径都不把旧聊天或历史 summary 内容发送给 provider。
- 历史 model-authored segments 无需迁移即可继续读取，并可被 metadata-only checkpoint 覆盖。
- prod/pre/test compose 和 CI 不再注入已删除变量。
- 公开文档不再把 semantic compaction 描述为可选回滚路径。
- 相关定向测试、后端测试矩阵及 docs-site 检查全部通过。
