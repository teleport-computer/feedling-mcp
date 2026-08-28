---
document_lifecycle: current
canonical_owner: self
---
# 候选：删除 Route-B 旧 selector/feature flag 岛

结论：`delete`，已按“先迁移安全与召回断言，再删除旧岛”的顺序实施。

## 范围与证据

- `backend/enclave/readside.py::memory_readside_for_model_api_enabled()` 全仓无调用。
- `context_moment_to_index_item()` 与 `select_context_memories_via_readside()` 只有测试
  消费者；旧实现约 120 行。
- `backend/enclave/routes/chat.py::_build_context_memories()` 已固定调用
  `select_context_memories_with_trace(..., mode="default")`，并明确说明 2026-08-18
  完成 resident/model_api bucketed selector 统一。
- `MEMORY_READSIDE_FOR_MODEL_API=true` 仍注入 test/pre/prod compose，却不再改变挑法
  或候选池。
- `tests/test_route_b_card_shape_recall.py` 全文件只测不可达旧 selector；
  `tests/test_enclave_routeb_readside.py` 仍含旧 flag/selector scaffolding。
- 旧 `model_api_readside_v1` trace 已不可达。当前 `context_mode/context_strict` 仍被解析，
  但选择函数忽略其值；`docs/MEMORY.md` 仍错误描述 default/model_api 两套现役模式。

实施批次删除 540 行，包含旧实现、test-only 测试岛、三个 compose 注入和陈旧
注释/import；同时增加和迁移真实路径门禁、当前文档及实施记录。最终净删除量以实施 PR
相对其最新 `test` 基线的 diff 为准。

## 兼容与保护

- 保留 `MEMORY_READSIDE_MODEL_API_LIMIT`；它仍控制 `/v1/memory/list` 候选池。
- 保留 `FEEDLING_MEMORY_READSIDE_HARD_MAX`；它属于另一条 readside 契约。
- 不删除或改名 `context_mode/context_strict` query 参数。先把它们记录为兼容接收、
  当前不分叉；是否移除 wire 参数另立 API 决策。
- 不触碰数据库、加密信封、租户边界或 consumer。
- 旧路径保护过 `_search_content` 不进入 trace、canonical/legacy 卡均可召回、退役
  sensitivity 字段不复活。这些测试意图必须迁到当前真实 bucketed 路径，不能随旧实现删除。

## 红绿步骤与验证

1. 红：在 `tests/test_context_memories.py`、`tests/test_garden_card_shape.py` 和 enclave ASGI
   路由测试补齐上述召回/隐私/兼容断言，确认它们覆盖当前 `_build_context_memories`。
2. 绿：删除三个旧函数/依赖、旧测试岛、flag 注入和 stale current 文档；不改 active limit。
3. 本地运行 context selector、garden card shape、enclave history/ASGI、compose parsing、
   release-pin 和 current-doc tests。
4. test 部署后核对 exact SHA，跑一轮 resident 与 V2 的记忆召回、trace 隐私检查；
   compose hash 变化只能证明新配置已发布，不能反推旧环境变量已被平台清除。

回滚方式：回退代码与 compose 提交并按正常 test/pre/prod compose 流程重新部署。

## 实施结果

- 当前 `/v1/chat/history` 仍固定使用统一 bucketed selector；`context_mode`、
  `contextMode` 和 `context_strict` 继续作为兼容输入被接受，但不再暗示存在第二套策略。
- canonical card 召回、trace 私密内容不外泄和 query-mode convergence 已迁到真实 selector
  与 ASGI 路由测试；回归测试通过 mutation red/green 证明能拦截策略重新分叉。
- 已删除旧 readside adapter、零消费者 policy module、旧测试岛和
  `MEMORY_READSIDE_FOR_MODEL_API` 的仓库 compose 注入；保留 active limit、wire 参数、
  数据库/加密/租户边界及 `tools/chat_resident_consumer.py`。
- 本地最终门禁：受影响测试 153 passed；标准完整测试 12082 passed、3 skipped、
  9 xfailed。test 环境部署与真实回合证据在合入 `test` 后记录。
