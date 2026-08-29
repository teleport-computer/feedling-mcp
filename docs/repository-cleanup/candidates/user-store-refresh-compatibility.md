---
document_lifecycle: current
canonical_owner: self
---
# 候选：删除 `UserStore` 定向刷新旧适配器

结论：`delete`，已实施。定向跨 worker 刷新统一使用现役 `UserStore` section API。

子计划：[实施记录](../../superpowers/plans/2026-08-29-retire-user-store-refresh-adapter.md)。

## 范围与证据

- 审计基线：`7f639ceba7ccd20fc8bbd87b3f30a5672a0f54e0`。
- `core.store._stores` 只由 `get_store()` 创建和写入 `UserStore`；仓内没有生产 adapter
  写入该缓存。
- `_refresh_store_channel()` 原先先探测 `note_section_change`。缺少该方法时，会直接调用
  `_load_frames_meta()`、`_load_world_books()` 等私有 loader，并自行管理 reload guard。
- 该 fallback 绕过 `SectionSlot` 的 stale/fresh、single-flight、失败保留和 telemetry 状态；
  唯一仓内消费者是 `test_wake_bus.py` 的轻量测试替身。
- 生产 `frames`、`blob`、`proactive` 通知始终命中 section API 分支；`wake_bus` 仍捕获并
  记录异常，因此意外的非 `UserStore` 缓存项不会导致 listener 退出。

## 实施与验证

- 删除 feature-detection 和私有 loader fallback，保留 cold section 不因 notify 被加载、
  已加载 section 被标 stale 后刷新、proactive waiter 被唤醒的现役行为。
- 将 wake bus 用例迁到真实 `UserStore` 与 `SectionSlot`，不再让测试替身反向供养生产兼容
  分支；负向测试通过真实 dispatch 证明旧 adapter 不会触发私有 loader，且异常不会阻断
  后续 handler。
- RED：在 detached 基线 `7f639ceb` 只加入最终 dispatch 测试后运行
  `python -m pytest tests/test_wake_bus.py::test_store_channel_does_not_bypass_sections_or_stop_dispatch -q`
  时，`adapter.loaded is False` 失败；旧 fallback 已实际调用 `_load_frames_meta()`。
- GREEN：`FEEDLING_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/postgres
  python -m pytest tests/test_store_cache.py tests/test_blob_wake.py tests/test_wake_bus.py -q`，
  结果 76 passed。
- 广泛回归在 commit `6d3aa396` 的实现树上运行；因本机另一个 editable package 提供
  `tests/__init__.py`，显式排除两个会被错误解析到外包的 admin data-track 测试。结果为
  12,206 passed、4 skipped、9 xfailed；11 个 Genesis 失败与改动前基线相同，另 2 个
  `FEEDLING_TEST_PG` 缺失失败已用显式环境变量单独复跑为 2 passed。本批相关测试无新增失败。
- 合入 `test` 后仍须记录 exact deployed SHA、健康检查和 canonical P0；合入前不把本地
  结果写成 test 环境证据。
- 未修改 schema、migration、公开 API、部署配置或 `tools/chat_resident_consumer.py`。

回滚方式：回退本批提交；不涉及数据或 schema 恢复。
