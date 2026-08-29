---
document_lifecycle: current
canonical_owner: self
---
# 候选：删除 `v2_user_triage.py` 的旧 semantic-compaction 诊断

结论：`delete`（局部，已实施）。保留通用 runtime/jobs/metrics/summary metadata/
trajectory/ledger/peers 诊断，只删失真的事故专用分支。

## 范围与证据

- `tools/v2_user_triage.py` 是人工只读 DB CLI，无生产或测试消费者；current 文档无引用。
- 工具随 2026-07-28 semantic-compaction 事故加入，此后未更新。
- 生产 semantic compaction 已删除；当前 compaction 是 metadata-only，不读正文、不调用
  provider。
- 工具仍硬编码已删除的 `COMPACTION_BATCH_CHARS=120000`，并输出 oversized ciphertext
  “永远无法 fold / 应 quarantine”的旧结论。
- 它还把所有 `responder_error` 计作 coverage failure，可能把当前系统误诊为
  “compaction is wedged”。实际 production gross 删除 121 行、net 删除 76 行。

`FEEDLING_V2_TAIL_BUDGET_MSGS` 及精确 `prompt_coverage_incomplete` 仍是现役的
metadata-only coverage/maintenance 信号；test/pre/prod compose 当前值为 50。不能把
它们与已删除的 char-budget/body hydration 路径一起清理。

## 兼容与验证

- 删除 intro 中的旧 body-size 事故说明、`COMPACTION_BATCH_CHARS`、`section_backlog`、
  `--backlog` 和接线；从 coverage 计数中删除泛化的 `responder_error` 归因。
- 保留精确 `prompt_coverage_incomplete` 计数、watermark、未覆盖消息数、summary segments、
  trajectory 和 ledger 输出。不再用硬编码 20 推导“wedged”；如需阈值判断，应读取或
  显式接收实际部署值，不能假设本地默认等于 live 配置。
- 运行 `python tools/v2_user_triage.py --help`。
- 在 test DB 对一个成功用户和一个失败用户各跑一次，确认通用 sections 可用且不再做
  semantic-compaction 推断；记录只读查询和 exact deployed schema head。
- 同步清理 `backend/voice/transcript_store.py` 对已删除
  `_bounded_compaction_prefix` 的源码注释，但不改变 voice 行为。

回滚方式：回退提交；旧诊断仅能作为历史材料，不能作为 live truth。

## 实施结果（2026-08-29）

- 基于 `origin/test@5f6eacedf5f853982831c9dbc9958b5d02f27267` 实施。
- 删除旧事故 intro、正文大小阈值、backlog-head 查询、`--backlog` 参数和硬编码 tail
  threshold；精确 `prompt_coverage_incomplete` 信号继续保留。
- `turn_failed:responder_error` 仍会展示，但不再被推断为 coverage failure；summary 只报告
  已存事实，不再声称“compaction is wedged”。
- trajectory 继续展示事件耗时和 payload size，但相同 size 只作为记录事实，不再被解释为
  “同一批次重发”或 self-locking failure。
- 更新 voice transcript archive 注释，使其描述当前 prompt/capture 的数据边界，不再引用
  已删除的 `_bounded_compaction_prefix`。
- 新增工具级回归测试，锁住泛化错误分类、精确信号输出和已删除 CLI 参数。该批不修改
  runtime、schema、migration、公开 API 或 `chat_resident_consumer.py`。
- 提交前门禁为 73 项相关测试通过；修改后的工具也已在 test 数据库按现役 schema
  完成只读回归，所有保留 section 正常，输出中不再出现 backlog-head/旧 compaction
  诊断。
