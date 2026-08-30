---
document_lifecycle: current
canonical_owner: self
---
# 候选：删除 `db.py` 三个零消费者叶子

结论：`delete`（已实施），低风险叶子批次。

## 范围与证据

- `backend/db.py::_next_blob_revision`：全仓只有定义；现役 revision CAS 直接使用
  `_blob_revision`/SQL。
- `backend/db.py::chat_load_recent`：只有定义；现役调用均使用
  `chat_load_recent_strict`，旧函数只是吞异常的 best-effort wrapper。
- `backend/db.py::memory_profile_source_snapshot`：只有定义；现役
  `memory_profile_source_stats` 提供相同的 content-free freshness 聚合，并有 Genesis、
  V2 worker 和测试消费者。
- 未发现生产、测试、current 文档、工具脚本或字符串动态调用消费者。实施 diff 最终从
  `backend/db.py` gross/net 删除 37 行，且未新增长期 glue。

## 兼容与验证

- 无 schema、migration、写入、wire、部署或回滚控制面变化。
- 仓库外临时脚本直接 import `db.py` 无法由仓库证明排除；PR 中必须明确这一限制。
- 实施前再搜索完整路径、符号字符串、`getattr`/`import_module` 和所有 `tools/ops`。
- 运行 chat recent、profile freshness、blob revision/CAS 相关测试和数据库契约测试。

回滚方式：回退删除提交；不得创建或修改 Alembic migration。

## 实施结果（2026-08-28）

- 从 `origin/test`（`1ae5da56850ffaf14ce55cd24d2a8e8c7c916471`）建立独立
  worktree，并在最终验证前 rebase 到后续部署钉住提交 `ec32e1f2`；删除上述三个定义，
  `backend/db.py` 净删除 37 行，未增加兼容层。
- 删除前后精确扫描定义、调用、字符串引用以及 `tools/`、`ops/`、`scripts/` 消费者，
  除候选记录外未发现仓内引用。
- 现役 `_blob_revision`/CAS、`chat_load_recent_strict` 和
  `memory_profile_source_stats` 路径保持不变；相关 PostgreSQL 行为测试删除前后均通过。
- 未修改 schema、migration、公开 API、部署配置或 `chat_resident_consumer.py`。

已知边界仍是仓库外临时脚本：若其直接 import 这三个未承诺的内部函数，升级后会收到
`AttributeError`/`ImportError`。仓库内没有该消费者证据；如需回滚，回退本批删除提交。
