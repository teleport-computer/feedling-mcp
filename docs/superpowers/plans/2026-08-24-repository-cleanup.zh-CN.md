---
document_lifecycle: current
canonical_owner: self
---
# Feedling 仓库完整清理计划

> 状态：已确认，执行中
>
> 目标分支：`test`
>
> 原则：先校正事实，再整理历史；先证明可删，再删除；最后才做必要的结构调整。

## 一、背景与目标

这次清理的核心问题不是单纯“文件太多”，而是历史事实、当前事实和未来设想混在同一个检索面里，导致工程师或 coding agent 容易：

- 把仍在生产运行的代码误判为遗留代码；
- 根据过期设计文档选择错误的运行时和排查入口；
- 在迁移、兼容、回滚尚未结束时提前删除保护面；
- 花费大量上下文阅读已经完成或被推翻的计划；
- 用静态工具的零引用结果代替真实的生产调用和部署证据。

已经确认的典型矛盾包括：

- `README.md` 写着 hosted resident supervisor 和 per-user CLI 已退役；
- `docs/PROJECT_OVERVIEW.md` 写着 hosted manifest 固定为 `v2_only`；
- `docs/testing/README.md` 写着 hosted V1 已不再维护；
- 但当前生产 compose 仍明确配置 `FEEDLING_HOSTED_RUNTIME_POLICY: "dual"`，默认 desired runtime 为 `resident`；
- `backend/agent_runtime/` 也仍然是活跃、近期持续修改的生产实现。

2026-08-24 的只读基线：

- tracked 文件 1,962 个；
- Markdown 文档 286 份；
- `docs/superpowers/plans/` 与 `docs/superpowers/specs/` 共 195 份；
- 其中至少 61 份出现退役、过期或被取代的语义，但没有统一生命周期标记；
- 顶层 Python 测试文件 746 个；
- `tools/chat_resident_consumer.py`、`backend/db.py`、Runtime V2 worker/store 等文件规模较大。

这些数字只用于说明问题和建立基线，不设置“必须删多少文件、多少行代码”的指标。

## 二、清理完成后的目标状态

清理完成后，任何工程师或 agent 应当能在很短时间内回答：

1. 当前生产到底有哪些运行时路径；
2. 某类用户实际由哪条路径处理；
3. 哪些配置和部署文件是当前事实；
4. 哪些文档仍是规范，哪些只是历史；
5. 某个工具、脚本、字段或兼容分支为什么存在；
6. 一个候选内容是否真的可以删除；
7. 修改后应该跑哪些本地测试和真实环境验证。

仓库事实按以下优先级解释：

1. test/pre/prod 的实际运行证据与部署 commit；
2. 该 commit 中的部署配置和运行时接线；
3. 生产代码、数据库、持久化格式和 wire/API 契约；
4. 契约测试和部署测试；
5. 明确标为 current 的架构、运维和测试文档；
6. 仍然有效的设计决策；
7. 历史 spec、plan、incident、changelog 和 git 历史。

低优先级材料可以解释“为什么曾经这样设计”，但不能覆盖高优先级材料对“现在实际跑什么”的描述。

## 三、全局保护规则

以下内容默认不作为普通死代码清理对象：

- Alembic 历史迁移；
- 已持久化的数据格式和兼容读路径；
- 加密信封、公开 API、事件名和 wire shape；
- TEE 信任边界、租户隔离和隐私保护；
- 当前仍用于 V1/V2/Resident 共存、切换和回滚的控制面；
- generated 文件和 vendored 依赖；
- test/pre/prod 部署及恢复所需的运维工具；
- `tools/chat_resident_consumer.py` 的单文件 VPS 分发形态。

普通开发分支和清理 PR 默认合入 `test`。涉及公开 API、架构、信任边界、安全假设或部署拓扑时，必须同步更新 `docs-site/content/docs/`、OpenAPI 和公开 changelog。

## 四、候选项必须具备的证据

每个清理候选都要记录：

- 具体符号、文件、配置键、路由、事件名和 wire 字符串；
- 生产调用者；
- 部署和配置调用者；
- 数据库、持久化和兼容义务；
- 仅存在于测试、文档或历史材料中的调用者；
- 已检查的模糊用途，例如运维脚本、恢复工具和手工探针；
- 计划删除、合并、归档或保留的具体内容；
- 连带影响的测试、文档、配置和生成文件；
- 净减少了什么，又增加了多少 glue；
- 放弃了哪些行为、回滚能力或未来扩展性；
- 本地测试、test 环境和必要的 pre/prod 证据。

候选结论只有四类：

- `delete`：没有生产消费者，也没有兼容义务，可以删除；
- `archive`：需要保留历史，但不应继续参与默认检索；
- `retain-protected`：仍在使用或承担保护职责，明确保留；
- `feature-decision`：存在生产消费者，是否删除属于产品或架构决策，不是普通清理。

静态工具只能发现候选，不能单独证明可以删除。

## 五、执行阶段

### 阶段 1：建立可复现的仓库基线

交付物：

- `docs/repository-cleanup/baseline.md`；
- `docs/repository-cleanup/agent-diagnostic-benchmark.md`；
- tracked 文件分类工具和对应测试。

工作内容：

- 只使用 `git ls-files` 统计版本化内容；
- 区分生产代码、测试、文档、migration、generated/vendor、工具脚本和本地忽略内容；
- 不扫描 `.worktrees`、虚拟环境、构建产物和秘密文件；
- 设计一组固定排查题，记录正确入口、错误入口、阅读文件数和首次找到正确路径的耗时。

基准题至少覆盖运行时选择、`agent_runtime` 是否活跃、resident 记忆检索、解密边界、consumer 自更新、migration 保留、测试入口和部署生效证据。

### 阶段 2：建立唯一的“当前事实入口”

交付物：

- `docs/CURRENT_STATE.md`；
- 当前状态与 compose 接线的一致性测试；
- 修正后的 `AGENTS.md`、`CLAUDE.md`、README、项目总览和测试入口。

工作内容：

- 用短文档说明当前 runtime、部署拓扑、数据和信任边界、测试入口；
- 把频繁变化的 runtime policy 从多份文档中收敛到一个入口；
- 自动检查 current 文档不能和生产 compose 矛盾；
- 新会话先读 `AGENTS.md` 和 `CURRENT_STATE.md`，不再要求把完整 changelog 当作当前架构；
- 历史状态只保留在明确标记的历史章节。

### 阶段 3：引入文档生命周期

所有 tracked 文档归入以下状态：

- `current`：必须随实现同步更新的当前事实；
- `decision`：仍然有效的长期决策；
- `historical`：已实施、被否决、被取代或仅代表某个时间点；
- `generated`：由工具生成，生成器才是权威源。

先对新增和本次修改的文档做 CI 校验，完成全量分类后再逐步收紧。禁止 current 文档把 archive 中的内容当作唯一权威来源。

### 阶段 4：整理 195 份历史计划与设计

按子系统分批处理：

- hosted runtime；
- resident runtime；
- storage/TEE；
- memory/perception；
- API/product；
- operations。

每一份 plan/spec 都要记录状态、当前 owner、引用方、实现证据、遗留兼容义务和归档位置。

只有完全被取代的文档才能归档。部分被取代的文档继续保留并链接当前 owner。归档前必须把仍有价值的动机、替代方案、风险和重新引入条件搬到 current 文档或 decision 中。

生产代码不应长期引用执行计划；应改为引用当前契约、决策或模块文档。

### 阶段 5：盘点 tools、scripts 和测试辅助面

每个工具或脚本都要有明确 owner 和用途分类：

- 生产 companion；
- 部署工具；
- 恢复工具；
- migration 工具；
- 活跃诊断工具；
- 测试辅助；
- generated helper；
- 历史内容；
- 无 owner 候选。

检查范围包括 import、完整路径、CLI 名称、systemd/compose、CI、runbook 和人工运维调用。没有 Python import 不等于没人使用。

只有强证据候选进入删除 PR；恢复工具和 migration 工具需要额外的数据与事故恢复评审。

### 阶段 6：按运行时和兼容边界审计代码

分别审计：

- hosted V1 resident；
- pooled Runtime V2；
- self-hosted VPS resident；
- chat、proactive 和后台 lane；
- provider adapter 与 capability catalog；
- enclave、数据库和部署选择器。

在删除前必须检查 desired runtime、access mode、切换指标、回滚路径、数据库列、加密信封、公开 API、事件名和兼容 reader。

每个接受的代码候选单独生成子计划，写清文件、红绿测试、test 环境验证和回滚方式。不能从盘点表直接跳到实现。

### 阶段 7：将 `chat_resident_consumer.py` 设为稳定保护边界

明确决策：不拆分 `tools/chat_resident_consumer.py`。

原因：

- 它直接运行在用户 VPS 上；
- systemd 使用固定脚本路径启动；
- 自更新依赖 backend 下发 commit、Git checkout、依赖安装和原地 re-exec；
- 多个测试通过直接 import、`spec_from_file_location` 和 monkeypatch 使用文件内符号；
- hosted agent-runner 还会把同一份 consumer 烘焙进不可变镜像；
- 用户机器不由团队直接控制，新增模块和 import/update 义务会扩大升级风险。

允许做的事情：

- 创建 `docs/repository-cleanup/resident-consumer-source-map.md`；
- 记录主要职责、section header 和稳定符号，帮助 agent 定位；
- 删除经过完整生产、配置、wire、持久化和 VPS/hosted 证据证明的内部死代码；
- 在不改变分发形态的前提下改善注释和导航。

禁止做的事情：

- 把 consumer 拆到新的 Python 模块；
- 修改稳定执行路径和进程模型；
- 仅因文件行数较大就做机械重构；
- 在文档清理 PR 中改变 checkpoint、session、自更新或退出语义。

任何影响行为的内部删除，都要通过 self-update、supervisor→consumer 契约、相关 consumer 测试和真实 VPS P0，证明 checkout/re-exec、checkpoint 保留和下一轮聊天正常。

### 阶段 8：清理后再评估其他大文件

候选包括：

- `backend/db.py`；
- `backend/model_api_runtime/v2/worker.py`；
- `backend/model_api_runtime/v2/jobs_store.py`；
- `backend/admin/data_track.py`。

先删除已经证明无用的职责，再讨论拆分。每个文件单独做设计和计划，依据职责、依赖、事务、锁、生命周期和近期共同修改关系，而不是只看行数。

数据库代码必须保持事务和锁 owner；异步 worker 必须保持取消、租约、terminal outcome 和发布职责清晰。

### 阶段 9：沉淀 Feedling 专用简化 skill 与持续守卫

基于 DeepSeek [`dsh-find-simplifications`](https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/skills/dsh-find-simplifications/SKILL.md) 的方法，保留：

- 生产与非生产消费者分类；
- 精确符号、配置和 wire 字符串搜索；
- 净删除量计算；
- 生命周期和 owner 分析；
- dependency swap 的真实成本评估；
- 明确的候选拒绝规则。

替换为 Feedling 自己的：

- V1/V2/Resident 运行时模型；
- TEE、加密和租户隔离保护区；
- Alembic 约束；
- test/pre/prod 证据；
- branch flow 和公共文档同步要求；
- `chat_resident_consumer.py` 不拆分规则。

CI 只强制可确定事实，例如文档生命周期、断链、current 状态与 compose 一致、工具 owner、保护边界；unused-symbol 输出只做提示，不能自动触发删除。

## 六、建议的 PR 顺序

1. 基线与 agent 排查 benchmark；
2. 当前事实入口和矛盾修正；
3. 文档生命周期与增量 CI；
4. 按子系统归档历史文档；
5. 工具脚本 owner 盘点和强候选；
6. runtime/data/deploy 子计划与删除；
7. consumer 保护边界和源码导航图；
8. 其他大文件的独立清理或拆分；
9. Feedling simplification skill、完整 CI ratchet 和清理后 benchmark。

不能把第 2 至第 8 阶段合并成一个“大扫除 PR”。评审者必须能够拒绝某个删除或重构，而不阻塞事实校正和文档治理。

## 七、完成标准

只有同时满足以下条件，才算完成本轮仓库清理：

- live evidence、compose、代码、测试和 `CURRENT_STATE.md` 对当前 runtime 的描述一致；
- 历史材料已经分类，不再进入默认 agent grounding；
- 每个 tracked 工具和脚本都有 owner 与生命周期；
- 每个生产删除都具备调用者、持久化、兼容和部署证据；
- 公开或架构行为变化同步更新文档、OpenAPI 和 changelog；
- `tools/chat_resident_consumer.py` 保持单文件分发，内部删除不影响自更新、checkpoint、hosted image 和真实 VPS 聊天；
- 固定 benchmark 中不再出现选错 runtime 的结论；
- 每个候选 PR 都记录了对应测试和 test/pre/prod 证据。

## 八、预期收益

这次清理最终追求的不是“仓库看起来更小”，而是：

- 当前事实只有一个可靠入口；
- 历史设计仍可追溯，但不会误导当前排查；
- agent 更少打开无关文件，更快找到正确 runtime；
- 删除动作具备可复查证据，不破坏兼容、回滚和安全边界；
- 大文件是否调整由真实收益决定，而不是由行数驱动。
