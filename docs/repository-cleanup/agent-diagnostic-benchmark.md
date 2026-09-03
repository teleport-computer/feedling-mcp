---
document_lifecycle: current
canonical_owner: self
---
# Agent 排查基准

本基准衡量仓库能否让一个全新会话快速选中正确 runtime 和证据链。它不是知识问答：答案必须来自被测 commit，不能使用其他会话的记忆。

## 运行协议

每次测试都在同一个 exact commit 上，从没有预读仓库内容的新会话开始。只给出一道题，允许只读仓库，禁止访问 live 用户数据或秘密配置。逐题记录：

- 最终答案和置信度；
- 首个正确证据路径；
- 打开的全部文件；
- 无关文件数；
- 从提问到首次专门打开正确证据文件正文的耗时；
- 是否选错 runtime；
- 是否把文档说法当成高于部署/代码的事实；
- 无证据断言。

“打开文件”指通过 `sed`、`cat` 等读取了文件正文；`rg` 等内容搜索即使返回匹配行，
也只算导航，不算专门打开文件。相应地，计时指标固定为“首次专门打开正确证据文件
正文”，不是“搜索输出第一次出现答案”。这样既能与打开文件口径一致，也能避免
搜索范围和输出截断改变判点。前后对比必须使用相同模型、提示词、工具权限和计时
方式，并保存 exact commit。

## Oracle 与判分点

### Q1：没有 desired-runtime row 的 hosted API-key 用户走哪条生产路径？

正确答案：当前生产 compose 的 policy 为 `dual`，缺省 desired 为 `resident`；还需沿 `backend/hosted/runtime_reconciler.py` 和 hosted 路由确认选择逻辑。不能仅凭 README 或项目总览回答 `v2_only`。

首选证据：`deploy/docker-compose.phala.yaml`、`backend/hosted/runtime_reconciler.py`、`backend/hosted/config_store.py`。

判错：直接回答 pooled Runtime V2、忽略 per-user override，或不说明答案绑定的部署 commit。

### Q2：`backend/agent_runtime/` 已退役还是仍是活跃路径？

正确答案：在当前 `dual`/`resident` 接线下，它仍是 hosted resident 的 supervisor/runtime 实现；是否真正部署还应由 agent-runner 健康和 exact deployed commit 证明。

首选证据：生产/runner compose、`backend/hosted/chat_send_core.py`、`backend/hosted/agent_runtime_cutover.py`、`backend/agent_runtime/supervisor.py`。

判错：因文档中的 retired 字样或 TEE migration 文件名而判定整个目录已废弃。

### Q3：resident 侧搜索记忆时，为什么 `memory_search` 零命中不能证明功能不存在？

正确答案：resident 的 CLI 契约使用带 `--query` 的 `memory-index`，再用 `memory-fetch` 取全文；它没有同名 `memory_search` 入口。

首选证据：`docs/testing/RUNTIME_MAP.md`、`tools/io_cli.py`、`backend/agent_runtime/agent_tools_prompt.md`。

判错：把 V2 的结构化 `memory_search` 工具名套到 resident，或据字符串零命中建议删除搜索能力。

### Q4：VPS resident 聊天由哪个进程解密？

正确答案：VPS consumer 通过 `FEEDLING_ENCLAVE_URL` 调 enclave decrypt/history 入口获取明文；普通 backend 存储/传输密文，不能把“backend 路由可见”误写为“backend 持有用户明文”。

首选证据：`tools/chat_resident_consumer.py`、`tools/io_cli.py` 和 enclave 路由/信任边界文档。

判错：声称用户 VPS 本地数据库直接保存服务端明文，或忽略 enclave 配置。

### Q5：哪些文件决定 consumer 自更新是否触发？

正确答案：backend 通过 chat poll/maintenance 提供 `expected_consumer_commit`；consumer 比较 commit，checkout 后由 `_runtime_repo_files()`、`_RELEVANT_PATH_PREFIXES`、`_RELEVANT_PATH_FILES` 和 requirements 变化判断是否安装依赖及 re-exec。

首选证据：`backend/chat/consumer.py`、`backend/chat/poll_core.py`、`backend/chat/resident_maintenance.py`、`tools/chat_resident_consumer.py`、`tests/test_chat_resident_self_update.py`。

判错：认为只上传一个拆出的模块就会自动生效，或忽略 systemd/原脚本 re-exec 边界。

### Q6：功能退役后哪些 schema 文件可以清理？

正确答案：已应用的 Alembic 历史保留；只有当前代码/契约中的兼容表面在持久化、回滚和 wire reader 义务全部证明消失后才可另立候选。迁移文件名带 `drop_retired` 也不等于可以删除迁移。

首选证据：`backend/alembic/versions/`、`backend/alembic_tee/versions/`、数据库兼容测试和部署版本表。

判错：删除历史 migration，或只根据当前 Python 引用判断数据库字段安全可删。

### Q7：修改某条 runtime 后，权威测试入口在哪里？

正确答案：先从 `docs/testing/README.md` 按改动类型选择，再读取 `docs/testing/TESTING.md` 的环境、L1/L2/L3 和真实链路要求；pytest 命令必须带文档要求的数据库环境。实际 CI 清单以 `.github/workflows/ci.yml` 为准。

首选证据：`docs/testing/README.md`、`docs/testing/TESTING.md`、`.github/workflows/ci.yml`。

判错：裸跑单个 pytest 后宣称 runtime 修改已完成，或把 uncovered baseline 当测试登记处。

### Q8：如何证明代码已经部署，而不只是 merged？

正确答案：读取部署环境健康信息中的 `release.git_commit`/`git_commit`，与目标 SHA 比较，再执行受影响真实 lane 的探针或 trace。PR merged、镜像构建成功和本地 HEAD 都不能单独证明环境正在运行该版本。

首选证据：`backend/asgi/health.py`、部署 runbook/探针、目标环境的健康响应。

判错：以 git 分支、CI 绿或 deploy 命令返回 0 代替 exact-SHA 和链路证据。

## 清理前结构基线

基线 commit `20dc0a5d52d4628b612e1d164c64b0138b9d87b5` 在正式计时前已经暴露一个判分阻断：Q1 和 Q2 的 current 文档与生产 compose 直接矛盾。也就是说，8 题中至少 2 题无法仅依靠未分级文档得到唯一可靠答案，必须跳过文档到部署和代码层纠错。

这一结论是仓库结构预检，不冒充受控 agent 性能数据；因此不虚构耗时、无关文件数或错误率。首次受控逐题结果必须在阶段 2 修改 current 文档之前、用上述 exact commit 的独立 checkout 采集；阶段 9 再用相同协议重跑。原始记录使用下表结构：

| Commit | 题号 | 首个正确证据 | 打开文件 | 无关文件数 | 首次专门打开正确证据正文耗时 | 选错 runtime | 无证据断言 |
|---|---|---|---|---:|---:|---|---|
| `<exact SHA>` | `Q1`…`Q8` | `<path>` | `<paths>` | `<n>` | `<seconds>` | `yes/no` | `<text or none>` |

如果运行环境不能提供隔离的新会话或可靠计时，应把该轮标为 `invalid`，而不是用当前实现者已经读过仓库的会话补数。

## 2026-08-28 清理后受控轮次

本轮状态为 `valid`。8 道题分别运行在全新的 ephemeral 会话中，固定参数如下：

- 被测 commit：`ede0fe7f2fca3e6c24bb31bed6c9ec5784566d70`；
- Codex CLI：`0.149.1`；模型：`gpt-5.4`；reasoning effort：`medium`；
- sandbox：`read-only`；禁止 live/secret 访问；
- 每题使用相同 wrapper，只替换题目正文；事件流逐行加高精度时间戳；
- “首次专门打开正确证据正文耗时”从 `thread.started` 计到首个包含 oracle
  所需事实的文件正文读取完成；内容搜索命中不计；总耗时来自
  `/usr/bin/time -p` 的 `real`；
- 仓库外的 skill 指令若被会话实际读取，也计入打开文件与无关文件。
- 完整运行参数、逐题 prompt 和 prompt SHA-256 保存在
  `agent-diagnostic-benchmark-2026-08-28-manifest.json`。

### 结果

| 题号 | 结论 | 置信度 | 首个正确证据 | 首开证据耗时 | 总耗时 | 打开文件 | 无关文件 | 选错 runtime | 文档高于部署/代码 | 无证据断言 |
|---|---|---|---|---:|---:|---:|---:|---|---|---|
| Q1 | 缺省选择 hosted Resident | 高 | `deploy/docker-compose.phala.yaml` | 37.36s | 147.46s | 11 | 1 | no | no | none |
| Q2 | `backend/agent_runtime/` 仍是活跃路径 | 高 | `deploy/docker-compose.phala.runner.yaml` | 65.22s | 174.02s | 20 | 1 | no | no | none |
| Q3 | Resident 使用 `memory-index --query` + `memory-fetch` | 高 | `tools/io_cli.py`、`tools/chat_resident_consumer.py` | 61.06s | 141.95s | 12 | 2 | no | no | none |
| Q4 | VPS consumer 经 enclave history/decrypt 取得明文 | 高 | `tools/chat_resident_consumer.py` | 70.25s | 208.80s | 21 | 4 | no | no | none |
| Q5 | poll 提供目标 commit，relevant files 决定重装/re-exec | 高 | `tests/test_chat_resident_self_update.py` | 115.87s | 232.35s | 14 | 1 | no | no | none |
| Q6 | 保留已应用 migration，另立 forward cleanup | 高 | `docs/CURRENT_STATE.md` | 33.92s | 177.94s | 24 | 1 | no | no | none |
| Q7 | 测试入口是 `docs/testing/README.md`，CI 以 workflow 为准 | 高 | `docs/testing/README.md` | 56.02s | 176.40s | 6 | 1 | no | no | none |
| Q8 | exact deployed SHA + 受影响 lane 探针才是 live proof | 高 | `docs/CURRENT_STATE.md` | 90.50s | 185.14s | 10 | 2 | no | no | none |

汇总：8/8 命中 oracle，错误 runtime 选择、文档覆盖部署事实和无证据断言均为
0。首次专门打开正确证据正文平均 66.28s、中位数 63.14s；总耗时平均
180.51s、中位数
177.17s。清理前只有结构预检，没有合法的逐题计时轮次，因此这些数字是首次受控
验收数据，不能写成相对基线的性能提升。

所有会话都以退出码 0 完成，但 stderr 中出现过模型目录刷新、MCP transport 和
WebSocket 的瞬时网络错误。总耗时包含这些启动/退出噪声；首开证据耗时来自事件流，
更适合衡量仓库导航成本。后续若做性能对比，仍须保持同一 CLI、模型和采集方式。

### 打开文件审计

- Q1：`AGENTS.md`、`docs/CURRENT_STATE.md`、`CONTRIBUTING.md`（无关）、
  `deploy/docker-compose.phala.yaml`、`deploy/docker-compose.phala.prod.runner.yaml`、
  `backend/hosted/runtime_reconciler.py`、`backend/hosted/config_store.py`、
  `backend/hosted/chat_send_core.py`、`backend/hosted/agent_runtime_cutover.py`、
  `backend/asgi/lifespan.py`、`backend/db.py`。
- Q2：`AGENTS.md`、`docs/CURRENT_STATE.md`、三套主环境 compose、三套 runner
  compose、`deploy/Dockerfile.agent-runner`、`deploy/check-prod-runner-topology.sh`、
  `deploy/check-v2-runner-fleet.py`（无关）、`backend/hosted/chat_routes_asgi.py`、
  `backend/hosted/chat_send_core.py`、`backend/hosted/runtime_reconciler.py`、
  `backend/hosted/config_store.py`、`backend/hosted/agent_runtime_cutover.py`、
  `backend/agent_runtime/supervisor.py`、`backend/agent_runtime/spawners.py`、
  `backend/asgi/runner_health.py`、`.github/workflows/ci.yml`。
- Q3：两份仓库外 `using-superpowers` 指令（均无关）、`tools/io_cli.py`、
  `tools/chat_resident_consumer.py`、`tools/io_cli_catalog.py`、
  `backend/capabilities/memory.py`、`backend/capabilities/registry.py`、
  `backend/capabilities/tool_schema.py`、`backend/model_api_runtime/v2/tool_surface.py`、
  `backend/model_api_runtime/v2/prompt_frontier.py`、`tests/test_v2_worker_tool_loop.py`、
  `tests/test_v1_downloadable_files.py`。
- Q4：`AGENTS.md`、`docs/CURRENT_STATE.md`、四份仓库外 skill 指令（均无关）、
  `tools/chat_resident_consumer.py`、`tools/io_cli.py`、`backend/enclave/routes/chat.py`、
  `backend/enclave/envelope.py`、`backend/enclave/auth.py`、`backend/enclave/keys.py`、
  `backend/core/enclave.py`、`backend/enclave/routes/envelope.py`、
  `backend/enclave/routes/decrypt_selfcheck.py`、`backend/chat/routes_asgi.py`、
  `backend/chat/chat_core.py`、`backend/chat/service.py`、`backend/db.py`、
  `docs-site/content/docs/architecture.mdx`、`docs-site/content/docs/self-hosting.mdx`。
- Q5：`AGENTS.md`、`docs/CURRENT_STATE.md`、`CONTRIBUTING.md`（无关）、
  `docs/testing/README.md`、`tools/chat_resident_consumer.py`、
  `backend/chat/poll_core.py`、`backend/chat/consumer.py`、
  `backend/chat/routes_asgi.py`、`backend/chat/resident_maintenance.py`、
  `tests/test_chat_resident_self_update.py`、`tests/test_chat_poll_client_release.py`、
  `tests/test_expected_consumer_commit.py`、`tests/test_agent_runtime_resident_contract.py`、
  `tests/test_resident_maintenance.py`。
- Q6：`AGENTS.md`、`docs/CURRENT_STATE.md`、`CONTRIBUTING.md`（无关）、两份测试入口、
  两套 Alembic `env.py`、`backend/db.py`、`tests/conftest.py`、五份 migration
  测试、四份 Alembic revision、`backend/model_api_runtime/v2/profile_store.py`、
  `backend/memory/migration.py`、`docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md`、
  `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`、`docs/RUNTIME_V2_FLOWS.md`、
  `docs/PROJECT_OVERVIEW.md`。
- Q7：`AGENTS.md`、`docs/CURRENT_STATE.md`、`CONTRIBUTING.md`（无关）、
  `docs/testing/README.md`、`docs/testing/TESTING.md`、`.github/workflows/ci.yml`。
- Q8：`AGENTS.md`、`docs/CURRENT_STATE.md`、`CONTRIBUTING.md`（无关）、
  `docs/testing/README.md`、`docs/testing/TESTING.md`、
  `docs/testing/RELEASE_TESTING_PROTOCOL.md`、`deploy/DEPLOYMENTS.md`、
  `backend/asgi/health.py`、`tests/test_asgi_healthz.py`、
  `backend/provider_health.py`（无关）。

### 验收结论与后续候选

正确性门禁通过：新会话没有再把 Hosted Resident 错判为退役，也没有把 merged、
镜像或分支 HEAD 当作 live proof。效率上仍有两个明显候选：Q5 首次专门打开正确
证据正文用了 115.87s，说明 consumer 自更新证据仍较分散；Q4 打开 21 个文件，说明 VPS/managed
trust boundary 仍需要跨多层核对。

Q7 同时发现一处 current 文档漂移：`docs/testing/TESTING.md` 把 CI 的
`syntax + static` 概括为 pyflakes，但 `.github/workflows/ci.yml` 实际运行
`compileall`、language eval 和 dependency provenance，没有显式 pyflakes。本轮在同一
PR 中修正文档；L1 的本地 pyflakes 要求保持不变。
