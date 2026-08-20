# Runtime V2 源码学习手册设计

> 日期：2026-08-17
>
> 状态：已完成对话设计确认，等待书面设计评审

## 目标

为希望独立修改 Runtime V2 并编写测试的工程师提供一份当前源码导向的学习手册。手册应让读者从一个真实 Chat 请求出发，理解运行时拓扑、队列与状态机、Prompt 与统一工具循环、副作用提交和恢复机制，并能为常见改动定位实现文件、选择测试层级和完成定向验证。

## 非目标

- 不重写 `docs/RUNTIME_V2_FLOWS.md` 已覆盖的逐业务场景说明。
- 不替代 `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md` 的能力现状和 rollout 台账。
- 不重复 `docs/RUNTIME_V2_WORKSPACE.md` 的 Workspace、Sandbox 和 Subagent 专题。
- 不修改 Runtime V2 实现、公共 API、数据库 schema 或部署配置。
- 不把历史设计文档或过期行号当作当前行为的唯一证据。

## 读者与完成标准

目标读者具备 Python、asyncio、PostgreSQL 和常规单元测试基础，但尚不了解 Feedling Runtime V2。

读完并完成练习后，读者应能：

1. 画出 backend、serve-worker 父进程、slot 子进程、Enclave、Provider 和 PostgreSQL 的边界。
2. 从 `POST /v1/model_api/chat/send` 追踪到可见回复的完整调用链。
3. 解释 single-flight、lease、runtime generation、reply cursor、effect fence 和幂等键分别保护什么故障。
4. 判断一个变更属于 foreground、wake 或 heavy pool，以及是否存在 lane 特例。
5. 根据变更风险选择纯函数、注入 fake、PostgreSQL 生命周期、wire 或 E2E 测试。
6. 采用先失败测试、再最小实现、最后定向回归的方式完成一次 Runtime V2 修改。

## 交付物

### 1. 主手册

新增 `docs/RUNTIME_V2_SOURCE_GUIDE.md`，作为当前源码的维护者入口。文档使用稳定符号名和相对源码链接，绝对行号只在确有帮助时作为附加信息，不作为导航主键。

### 2. README 导航

在 `README.md` 的 “Where to go next” 表格中加入源码学习手册入口，并保持现有 flow、parity、rollout 和历史 audit 文档的职责不变。

### 3. 图示

主手册内嵌三张 Mermaid 图：

- 进程、组件和信任边界架构图；
- Chat 请求到回复的时序图；
- Job、Effect 与恢复相关的简化状态图。

交互式浏览器图用于设计和讲解辅助，保存在 gitignored 的 `.superpowers/` 会话目录中，不作为仓库正式文档依赖。

## 文档结构

### 第一层：必修主干

1. **先建立心智模型**：Runtime V2 是 PostgreSQL 队列驱动的、多用户共享调度、每 slot 独立子进程执行的 provider-native agent runtime。
2. **进程与组件拓扑**：`serve_worker.main`、`_serve`、`RuntimePoolConfig`、`SlotFleet`、`turn_child.main`、`worker._slot_loop` 的职责与边界。
3. **Chat 主链**：`model_api_chat_send_core` → 原子 append/enqueue → `claim_next_job` → `_run_turn` → `process_job` → `build_turn_messages` → `run_tool_loop` → effect outbox → final reply。
4. **可靠性不变量**：用户/lane single-flight、priority、queue deadline、owner-fenced lease、runtime generation、ordered reply cursor、effect ordering/idempotency、terminal failure outbox。
5. **Prompt 与工具**：静态 trusted prefix、应用数据、原文 tail、coverage、temporal/runtime context、动态工具目录、provider-native transcript、并行读取和出站数据 fence。
6. **副作用与事务**：读工具即时返回；平台写和回复进入 outbox；applier 在 generation、ownership、input frontier 和 collision fence 下提交。

### 第二层：扩展分支

按“触发者、lane、共用主干、特例、终态、测试”六项模板讲解：

- manual wake、heartbeat、scheduled、screen watch；
- profile；
- capture 和 dream；
- maintenance/compaction；
- Genesis daemon；
- trajectory capture/review。

其中 scheduled 必须明确区别于可沉默的普通 wake；profile 必须说明 delayed retry 不占可认领 slot；Genesis 必须说明它由 serve-worker 父进程托管但不属于普通 `agent_jobs` turn slot。

### 第三层：独立开发指引

1. “改什么看哪里”的模块索引。
2. 从最小测试到真实链路的测试金字塔。
3. 常用 `rg`、单测、PostgreSQL 测试命令。
4. 五类带答案方向的源码练习：Prompt 小改、读工具字段、写工具 effect、lane 调度、恢复/fence。
5. 修改前后检查表：依赖方向、敏感数据、幂等、终态、文档与测试。

## 教学顺序

采用“主链驱动 + 测试反证”，而不是逐文件穷举：

1. 建立拓扑；
2. 跑通 Chat 主链；
3. 掌握正确性护栏；
4. 扩展到后台 lanes；
5. 用测试反证理解；
6. 完成一次安全修改。

每阶段以“能解释、能定位、能用测试证明”为完成条件，避免把阅读文件数量当作学习进度。

## 源码证据标准

每个关键结论尽量满足三角验证：

- **实现证据**：链接当前函数、类型、常量或装配点。
- **测试证据**：链接保护该行为的测试文件，必要时点出代表性测试名。
- **数据证据**：涉及持久状态和事务时，对照 migration/schema 与 `jobs_store`/`db` 访问函数。

历史 spec、plan、audit 和 commit 只用于解释“为什么”，不能覆盖当前实现事实。若历史文档与源码冲突，以当前实现和当前测试为准，并在手册中指出语义演进。

## 必须核对的易漂移结论

- 当前是 foreground、wake、heavy 三池，默认 slot 数分别为 4、2、2。
- 每个 slot 是独立子进程，不是父进程内的多个 turn 协程。
- 父进程负责 scheduler、reaper、watchdog、fleet heartbeat、reconcile、usage rollup 和 Genesis 托管。
- Chat 消息 INSERT 与 chat job enqueue/coalesce 在同一事务提交。
- `claim_next_job` 同时执行 lane 限定、priority、单用户运行互斥、lease 与 runtime generation 检查。
- Chat、wake 和后台抽取虽然共享统一 provider/tool 基础设施，但沉默、失败可见性和送达保证不同。
- 最终回复和工具写入经 effect outbox；生产 Chat 的回复、cursor 和 source-job 完成具有原子提交路径。
- Profile 使用可持久化 delayed retry；未来可用时间之前不应占据 worker slot。
- Enclave 并发由父进程 broker 跨 slot 管理，并为三类 pool 保留容量。

## 测试与验证

文档实现完成后至少执行以下验证：

1. 检查所有新增相对链接指向现存文件和锚点。
2. 运行进程池、子进程监督和 Enclave broker 的定向测试。
3. 运行 job enqueue/claim/lease/generation 与原子回复 cursor 的定向测试。
4. 运行 context、tool loop、effect outbox 和 effect sink 的定向测试。
5. 运行 scheduler、wake、profile retry 的定向测试。
6. 对测试输出中的 skipped 单独检查；没有 PostgreSQL 时，不把 DB 测试跳过写成通过。

优先使用仓库 `docs/testing/TESTING.md` 的环境和命令约定。由于本次只改内部 Markdown 与 README，不要求运行完整后端、OpenAPI 或 docs-site 构建；若链接检查或 Markdown lint 在仓库中已有明确命令，则一并运行。

## 风险与控制

- **文档过长**：正文围绕一条 Chat 主链组织，专题细节链接既有文档。
- **源码漂移**：以符号名和相对链接代替脆弱行号，并列出最后核验日期与分支/commit。
- **把实现偶然性写成契约**：区分“设计不变量”“当前默认配置”“实现细节”。
- **测试假绿**：明确 PostgreSQL 缺失会导致跳过，并报告真实通过/跳过数量。
- **安全边界误读**：单列 Provider 凭证、runtime token、Enclave plaintext 与数据库 ciphertext 的流向。
- **初学者直接扎进万行文件**：给出函数级停靠点和每阶段退出条件，不建议线性通读 `worker.py` 或 `jobs_store.py`。

## 完成定义

- 主手册和 README 导航已提交。
- 三张图与所有源码/测试链接可读。
- Chat 主链、三池拓扑、可靠性不变量和 lane 差异均有当前源码证据。
- 定向测试和链接验证有实际输出记录，跳过项被如实说明。
- 文档末尾提供可以直接执行的学习顺序、练习和修改检查表。
