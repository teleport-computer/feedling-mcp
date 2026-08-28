---
document_lifecycle: current
canonical_owner: self
---
# 候选：删除 `v2_user_triage.py` 的旧 semantic-compaction 诊断

结论：`delete`（局部）。保留通用 runtime/jobs/metrics/summary metadata/trajectory/
ledger/peers 诊断，只删失真的事故专用分支。

## 范围与证据

- `tools/v2_user_triage.py` 是人工只读 DB CLI，无生产或测试消费者；current 文档无引用。
- 工具随 2026-07-28 semantic-compaction 事故加入，此后未更新。
- 生产 semantic compaction 已删除；当前 compaction 是 metadata-only，不读正文、不调用
  provider。
- 工具仍硬编码已删除的 `COMPACTION_BATCH_CHARS=120000`，并输出 oversized ciphertext
  “永远无法 fold / 应 quarantine”的旧结论。
- 它还把所有 `responder_error` 计作 coverage failure，可能把当前系统误诊为
  “compaction is wedged”。预计 gross 删除约 90–110 行，最终 net 待实施 diff 复算。

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
