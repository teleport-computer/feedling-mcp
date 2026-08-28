---
document_lifecycle: current
canonical_owner: self
---
# 候选：删除 `db.py` 三个零消费者叶子

结论：`delete`，适合作为低风险叶子批次。

## 范围与证据

- `backend/db.py::_next_blob_revision`：全仓只有定义；现役 revision CAS 直接使用
  `_blob_revision`/SQL。
- `backend/db.py::chat_load_recent`：只有定义；现役调用均使用
  `chat_load_recent_strict`，旧函数只是吞异常的 best-effort wrapper。
- `backend/db.py::memory_profile_source_snapshot`：只有定义；现役
  `memory_profile_source_stats` 提供相同的 content-free freshness 聚合，并有 Genesis、
  V2 worker 和测试消费者。
- 未发现生产、测试、current 文档、工具脚本或字符串动态调用消费者。预计 gross 删除约
  31 行；若验证不新增长期 glue，最终 net 应接近该数字，仍以实施 diff 为准。

## 兼容与验证

- 无 schema、migration、写入、wire、部署或回滚控制面变化。
- 仓库外临时脚本直接 import `db.py` 无法由仓库证明排除；PR 中必须明确这一限制。
- 实施前再搜索完整路径、符号字符串、`getattr`/`import_module` 和所有 `tools/ops`。
- 运行 chat recent、profile freshness、blob revision/CAS 相关测试和数据库契约测试。

回滚方式：回退删除提交；不得创建或修改 Alembic migration。
