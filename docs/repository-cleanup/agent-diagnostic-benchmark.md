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
- 从提问到首次找到正确证据的耗时；
- 是否选错 runtime；
- 是否把文档说法当成高于部署/代码的事实；
- 无证据断言。

“打开文件”指读取了文件正文，不把一次文件名搜索的结果全部算作打开。前后对比必须使用相同模型、提示词、工具权限和计时方式，并保存 exact commit。

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

| Commit | 题号 | 首个正确证据 | 打开文件 | 无关文件数 | 首次正确证据耗时 | 选错 runtime | 无证据断言 |
|---|---|---|---|---:|---:|---|---|
| `<exact SHA>` | `Q1`…`Q8` | `<path>` | `<paths>` | `<n>` | `<seconds>` | `yes/no` | `<text or none>` |

如果运行环境不能提供隔离的新会话或可靠计时，应把该轮标为 `invalid`，而不是用当前实现者已经读过仓库的会话补数。
