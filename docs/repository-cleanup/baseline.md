---
document_lifecycle: historical
canonical_owner: docs/repository-cleanup/README.md
historical_reason: point-in-time
---
# 仓库清理基线（2026-08-24）

本快照建立在分支 `docs/repo-cleanup-plan-20260824`、commit `20dc0a5d52d4628b612e1d164c64b0138b9d87b5` 上；当时 `origin/test` 为 `ba838980523e2dda55c6e1c52dc65fd7ad6ce1e8`。它位于清理方案提交之后、阶段 1 实现提交之前。

数字只用于让后续变化可比较，不是删除配额。

## Tracked 文件分类

以下结果由 `python3 tools/repository_inventory.py --format markdown` 生成，语料边界为 `git ls-files`：

| 分类 | 文件数 |
|---|---:|
| documentation | 95 |
| generated | 1 |
| historical-review | 197 |
| migration | 174 |
| other | 35 |
| production | 503 |
| repository-config | 18 |
| test | 763 |
| tool-script | 110 |
| vendor | 68 |
| **合计** | **1,964** |

补充计数：

- tracked Markdown：288 份；
- `docs/superpowers/plans/` 与 `docs/superpowers/specs/`：197 份；
- 顶层 `tests/test_*.py`：746 份；
- plan/spec 中包含 retirement、deprecated、obsolete 或 supersession 语义：63 份；
- 使用统一 `Status: retired/superseded` 元数据的 plan/spec：0 份。

`historical-review` 是位置分类，不是生命周期结论。两份刚提交的清理计划也在该目录，因此会被计入；后续生命周期工具需要把“执行中的计划”和“历史计划”进一步区分。

## 已确认的当前事实矛盾

生产 compose 在 `deploy/docker-compose.phala.yaml` 中两处明确配置：

- `FEEDLING_HOSTED_RUNTIME_POLICY: "dual"`；
- `FEEDLING_RUNTIME_DEFAULT_DESIRED: "resident"`。

与之冲突的描述包括：

- `README.md` 声称 hosted resident supervisors 已退役；
- `docs/PROJECT_OVERVIEW.md` 声称 hosted manifest 为 `v2_only`；
- `docs/testing/README.md` 声称 hosted V1 不再维护。

与此同时，`backend/agent_runtime/`、`backend/hosted/runtime_reconciler.py`、`backend/hosted/chat_send_core.py` 和 agent-runner 部署接线仍存在。阶段 2 必须先建立唯一 current-state 入口并消除这些矛盾；在此之前，不能把 hosted resident 代码作为“老旧内容”删除。

## 大型执行表面

| 文件 | 行数 |
|---|---:|
| `tools/chat_resident_consumer.py` | 20,475 |
| `backend/db.py` | 17,739 |
| `backend/model_api_runtime/v2/worker.py` | 16,462 |
| `backend/model_api_runtime/v2/jobs_store.py` | 12,903 |
| `backend/admin/data_track.py` | 11,071 |

行数只表示审计成本。`tools/chat_resident_consumer.py` 是明确保护的单文件 VPS 分发边界，不进入拆分候选。其余文件也要先证明职责和依赖边界，再决定是否值得拆分。

## 基线结论

当前主要风险不是仓库绝对大小，而是 current 与 historical 事实没有隔离，且运行时描述互相冲突。第一优先级应是事实入口和文档生命周期，而不是批量删除代码。

本地 ignored 内容、迁移历史、generated/vendor 内容均与普通生产死代码分开统计。任何后续删除都必须另有消费者、持久化、兼容和部署证据。
