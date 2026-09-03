---
document_lifecycle: historical
canonical_owner: docs/superpowers/plans/2026-08-24-repository-cleanup.zh-CN.md
historical_reason: implemented
---
# Memory/Perception 历史文档批次 1 实施计划

> **IMPLEMENTED / DO NOT RE-RUN**：本计划对应的内核计划归档、current owner 修正、
> 供应链说明和批次 1 审计已经完成。正文中的路径、基线、预期计数与命令保留为实施期
> 证据；后续状态由 repository cleanup 总计划和 Memory/Perception 审计持有。

> **Agent 执行要求：** 按 `superpowers:executing-plans` 逐项执行，并在提交前完成独立复审。

**目标：** 将核心范围已经完成的 Memory Garden 内核提取实施计划移出默认 agent 搜索面，同时保留仍有效的 Garden/IO 分层决策，并把当前外部依赖、供应链和兼容义务写回现行 owner。测试方案因仍含未转移的有效工具警告，本批明确保留。

**架构：** `docs/MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md` 保存“判断力内核与 IO 适配层分离”的长期决策；`docs-site/content/docs/architecture.mdx` 保存当前外部包和 measured image 供应链事实；`CONTRIBUTING.md` 保存仓库内依赖方向；`backend/requirements*.txt`、CI 与聚焦测试是可执行事实。本批只归档一次性实施材料，不改变 Memory Garden、Dream、Genesis、V1/V2 或 resident 行为。

**基线：** `origin/test@26c4ac55`。

## 全局约束

- PR 目标为 `test`。
- 不修改 `backend/`、`deploy/`、`.github/`、依赖版本、数据库/Alembic、公开 API、运行时配置或 `tools/chat_resident_consumer.py`。
- 不把“计划已实施”解释为可以删除兼容 re-export、Memory/Dream/Genesis 路径、locale 参数、mixed runtime 调用方或外部包供应链守卫。
- `memgarden` 与 `agent-protocol-core` 继续作为 hash-pinned release wheels 安装；仓库内不得恢复 `backend/memgarden`、`backend/memory_garden` 或 `backend/agent_protocol_core` 副本。
- Garden 内核继续只承载可跨宿主复用的判断力；数据库、网络、加解密、ownership、锁、审计、调度和模型调用留在 IO 仓库。
- provenance 校验的当前事实必须准确：CI 会验证 GitHub artifact attestation，但该步骤 `continue-on-error`，因此它是可见的 best-effort 证据，不是阻断部署的硬保证。
- 生命周期清单只通过 `python3 tools/check_document_lifecycle.py --all --report` 生成，并证明字节级确定性。

---

## Task 1：建立当前 owner 与长期边界

**文件：**

- 修改：`docs/MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md`
- 修改：`docs/superpowers/specs/2026-08-17-garden-io-boundary.md`
- 修改：`CONTRIBUTING.md`
- 修改：`docs-site/content/docs/architecture.mdx`

### Step 1：分类两份仍有效的设计

- 给提取设计添加 `document_lifecycle: decision`、`canonical_owner: self`。
- 给 Garden/IO 边界添加 `document_lifecycle: decision`，canonical owner 指向提取设计。
- 两份文档顶部都增加 current-state note：原始文件图和行号是提取期快照；当前内核已成为外部 `memgarden` 包，现行实现与供应链事实由 architecture、requirements lock、CI 和测试共同持有。

### Step 2：修正 current 依赖方向

将此前未分类的 `CONTRIBUTING.md` 标为 `current/self`，并更新其依赖说明：

- `memgarden` 是安装进来的外部低层依赖，不是 `backend/` 子包；
- `memgarden` 依赖外部 `agent_protocol_core`，不依赖 IO 的 `backend/core`；
- IO 的 memory/genesis/runtime 路径单向 import 外部包；IO 侧的加密、存储、锁、审计和模型 effect 不进入内核；
- 链接长期决策和 public architecture 当前供应链说明。

### Step 3：修正 provenance 当前事实

更新 public architecture：hash pin 仍是阻断性字节一致性保证；CI 另外尝试验证两个 wheel 的 GitHub artifact attestation，但该步骤非阻断。不得继续写“没有 provenance attestation yet”，也不得把 best-effort 检查夸大成部署硬保证。

### Step 4：验证 current owner

```bash
python3 tools/check_document_lifecycle.py --changed-vs origin/test
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_document_lifecycle.py tests/test_current_state_docs.py \
  tests/test_repository_inventory.py -q
git diff --check
```

预期：生命周期校验通过，文档基线 45 项通过，无 whitespace 错误。

## Task 2：归档已实施计划并转移理由

**文件：**

- 移动：`docs/superpowers/plans/2026-08-14-memory-garden-kernel.md` → `docs/archive/superpowers/plans/2026-08-14-memory-garden-kernel.md`
- 保留并审计：`docs/superpowers/plans/2026-08-14-memory-garden-test-plan.md`
- 新建：`docs/repository-cleanup/memory-perception-history.md`
- 修改：`docs/repository-cleanup/README.md`

### Step 1：证明引用所有权

精确搜索完整路径和 basename。预期：两份计划没有仓库引用方；提取设计只被 current `CONTRIBUTING.md`、perception 设计/计划和计划自身引用。归档不得让 current 文档指向 archive 作为唯一权威来源。

### Step 2：移动并分类实施材料

内核实施计划使用：

```yaml
---
document_lifecycle: historical
canonical_owner: docs/MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md
historical_reason: implemented
---
```

增加 scoped `CORE EXTRACTION IMPLEMENTED / DO NOT RE-RUN` 横幅，说明本地内核后来重命名并外置；明确 storage adapter 切流和 CLI/MCP 壳未实施，转移到 current decision 的 deferred scope。测试方案因旧 V1 envelope 工具警告仍有效而留在原处。

### Step 3：建立审计记录

审计页至少记录：

- `1fa56d68` 提取方向、`2b1585fd` 包骨架、`5e50e79e` 调用方切换、`3bda1af4` 语言行为修正；
- `70903b4a` / `ec3d6cb1` Garden/IO 边界收敛；
- `5746b24f` 重命名为 `memgarden`、`4d25dbfb` 删除本地副本并改为外部依赖；
- current owner、仓库引用、运行时消费者、兼容义务、供应链限制和重新引入本地副本的禁止条件；
- 本批不处理 perception 的实施计划；它留到独立批次核对 2026-08-26 的 freshness 修复和当前 owner 后再决定。

在 cleanup README 增加审计页入口。

## Task 3：生成清单并回归

### Step 1：暂存分类变化并证明清单确定性

```bash
git add CONTRIBUTING.md docs/MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md \
  docs/superpowers/specs/2026-08-17-garden-io-boundary.md \
  docs/archive/superpowers/plans/2026-08-14-memory-garden-kernel.md \
  docs/repository-cleanup/README.md \
  docs/repository-cleanup/memory-perception-history.md \
  docs-site/content/docs/architecture.mdx \
  docs/superpowers/plans/2026-08-27-memory-perception-history-batch-1.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/memory-perception-batch1-a.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/memory-perception-batch1-b.md
cmp /tmp/memory-perception-batch1-a.md /tmp/memory-perception-batch1-b.md
cp /tmp/memory-perception-batch1-a.md docs/repository-cleanup/document-lifecycle-inventory.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/memory-perception-batch1-after.md
cmp docs/repository-cleanup/document-lifecycle-inventory.md /tmp/memory-perception-batch1-after.md
```

预期分类计数：`current=34`、`decision=17`、`historical=35`、`generated=1`。

### Step 2：运行 Memory Garden 聚焦回归

```bash
PYTHONPATH=backend /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_garden_selection_pluggable.py \
  tests/test_garden_card_shape.py \
  tests/test_route_b_card_shape_recall.py \
  tests/test_route_b_sensitive_gate.py \
  tests/test_memory_injection_observability.py \
  tests/test_memgarden_is_a_real_dependency.py \
  tests/test_card_leak_signals_wired.py \
  tests/test_memgarden_policies.py \
  tests/test_memgarden_capture_golden.py \
  tests/test_memgarden_dream_migrate_golden.py \
  tests/test_memgarden_prompt_params.py \
  tests/test_memgarden_storage_port.py \
  tests/test_memgarden_dreaming.py -q
```

### Step 3：验证 public docs

```bash
cd docs-site
npm run types:check
npm run lint
npm run build
```

### Step 4：验证保护范围与引用

```bash
git diff --exit-code origin/test -- backend deploy .github tools/chat_resident_consumer.py
test ! -e backend/memgarden
test ! -e backend/memory_garden
test ! -e backend/agent_protocol_core
test ! -f docs/superpowers/plans/2026-08-14-memory-garden-kernel.md
test -f docs/archive/superpowers/plans/2026-08-14-memory-garden-kernel.md
test -f docs/superpowers/plans/2026-08-14-memory-garden-test-plan.md
git diff --check
```

### Step 5：独立复审

复审 current owner、外部依赖方向、provenance 保证强度、历史证据、兼容义务、确定性清单、测试和保护范围。无 critical/important 问题后才提交并创建到 `test` 的 PR。
