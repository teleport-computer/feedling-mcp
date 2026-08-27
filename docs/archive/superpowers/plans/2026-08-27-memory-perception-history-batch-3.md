---
document_lifecycle: historical
historical_reason: implemented
canonical_owner: docs/repository-cleanup/memory-perception-history.md
---
# Memory/Perception 历史文档批次 3：Perception 内核提取谱系

> **HISTORICAL / IMPLEMENTED（2026-08-27）**：本批按下述范围完成并通过生命周期、
> current-state 与 Perception 聚焦回归。本文仅保留执行范围和验证路径；当前结论由
> `docs/repository-cleanup/memory-perception-history.md` 持有。

## 目标

把已经落地的 Perception Step 1 实施计划移出 current 搜索面，同时把仍有长期价值的
内核/IO 边界、V1/V2 共用面、prompt owner 与 freshness 语义转移到现行 owner。本文档
只规划文档生命周期清理，不修改运行时行为。

## 已确认事实

- `ac7afd62` / `b043ae8d` 冻结并补齐 prompt golden；`85f0046f` 建立内核与 AST 纯度守卫。
- `1e3c6677`、`bcf3612d`、`42e93471`、`94fd56af`、`6959a3aa`、`1ad68346`
  依次迁移 catalog、字段/权限/一瞥、历史计算、V2/V1 prompt 和部分 wake 判据。
- `27625742`、`73d99e7d`、`d97ece15` 完成命名、整体审查和验收矩阵修正。
- `c7cdae93` 已把过期 digest trend 表达为 `last_known`，避免旧值伪装成当前值。
- `PERCEPTION_WAKE_SOURCES`、`is_significant_change`、`should_wake` 刻意未接入 IO；
  `tests/test_perception_kernel_wake.py` 在 reason 映射和真实信号语义拍板前阻止直连。
- `backend/perception/` 继续持有数据库、加解密、鉴权、事务、metrics、入队与兼容
  re-export；`backend/perception_kernel/` 只持有纯判断。
- `tools/chat_resident_consumer.py` 仍从内核读取说明书，但本批不修改、移动或拆分该文件。

## 文件范围

- 移动并分类：
  `docs/superpowers/plans/2026-08-19-perception-extraction-step1.md`。
- 校准并分类：
  `docs/PERCEPTION_EXTRACTION_DESIGN.zh.md`、
  `docs/PERCEPTION_ARCHITECTURE.zh.md`、
  `docs/PERCEPTION_PROMPT_ASSETS.zh.md`。
- 更新审计与索引：
  `docs/repository-cleanup/memory-perception-history.md`、
  `docs/repository-cleanup/README.md`、
  `docs/repository-cleanup/document-lifecycle-inventory.md`。
- 完成本批后，将本文档归档为 `historical` / `implemented`。

## 执行步骤

1. 给设计、架构和 prompt 资产文档补 lifecycle，并明确 current owner 与历史快照边界。
2. 把架构文档中的旧 `perception/` owner、散落 prompt 和“尚待提取”叙述校准为
   `perception_kernel` + IO adapter 的现状。
3. 把 Step 1 实施计划移到 `docs/archive/superpowers/plans/`，添加
   `historical` front matter 与禁止重放说明。
4. 在 Memory/Perception 审计页记录实现证据、兼容义务、freshness 修复与 deferred scope。
5. 生成 lifecycle inventory，检查精确 inbound links 与 diff。
6. 运行文档生命周期/current-state 测试和 Perception 聚焦回归；独立评审后再推 PR。

## 验证命令

```bash
python3 tools/check_document_lifecycle.py --changed-vs origin/test
python3 tools/check_document_lifecycle.py --all --report
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_document_lifecycle.py tests/test_current_state_docs.py -q
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_perception_kernel_purity.py tests/test_perception_kernel_catalog.py \
  tests/test_perception_kernel_projection.py tests/test_perception_kernel_wake.py \
  tests/test_perception_prompt_golden.py tests/test_perception_history.py -q
git diff --check
```

## 非目标

- 不改变 public API、OpenAPI、部署拓扑、数据库或 wire/persistence 契约。
- 不把设计中的 Step 3（CLI/MCP、新仓库、开源）描述成已交付能力。
- 不删除 IO adapter 或兼容 re-export。
- 不改动或拆分 `tools/chat_resident_consumer.py`。
