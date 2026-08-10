# 基线契约收口设计

## 背景

健康探针隔离 worktree 在 `a04b7d23` 上的定向基线为 50 项全绿；后端完整测试排除依赖本机 `localhost:5001` 服务的 `tests/test_api.py` 后，结果为 8614 通过、10 失败。只读复现、数据流追踪和近期提交检查表明，这 10 项具有同一流程根因：功能扩展已更新主实现或局部测试，但精确响应键集、导航结构、固定 SQL 下标和注册表消费者等二级契约没有同步收口。

其中 9 项是测试仍表达旧契约；唯一生产代码缺口是 `voice_transcripts` 已进入 TEE 同步注册表和复制器，却未进入 `tee_shadow.verify` 的覆盖集合。

## 目标

- 让现有测试准确表达已经合入并被其他测试覆盖的新契约。
- 让 TEE verify 至少严格核对 `voice_transcripts` 的两侧行数与 pending 状态，消除“未覆盖但全绿”的假象。
- 恢复排除外部服务型 `tests/test_api.py` 后的完整后端基线，为健康探针隔离提供可信起点。

## 非目标

- 不回滚 admin IA、ASGI 页面缓存、Genesis 活跃任务保护、voice transcript、image generation 或 web policy 功能。
- 不为通过测试而删除公开字段、放宽运行时保护或跳过失败测试。
- 不在本轮实现 `voice_transcripts` 密文内容逐字段比对；当前 verify 能力先与现有 ciphertext 表的严格行数和 terminal pending 语义对齐。
- 不启动或改造 `tests/test_api.py` 所依赖的本机 5001 服务。

## 方案

采用契约收口方案：9 项陈旧断言只更新测试，保留已确认的生产行为；TEE verify 缺口采用测试先行的最小生产修复。每个修改必须能由对应失败直接解释，不做顺手重构。

### Admin 导航与缓存

- `test_admin_usage` 在诊断二级导航 `viewbar-diag` 中查找“Token 与模型”，继续验证用户 drill-down 路径和查询参数不丢失。
- Flask/ASGI parity 比较只标准化 ASGI cache hit 注入的 `cache-note`；状态码、Content-Type 和其余 HTML 仍保持字节级比较。
- 裸 `/admin/data-track` 的语义断言改为当前“Feedling 值班首页”；DAU 页面继续断言 `Daily Active Users`。

不得删除 cache note，也不得让 Flask 为迎合 parity 引入同一缓存层。

### Genesis 活跃任务保护

当前第一次 Flask 请求创建 processing job，第二次 ASGI 请求若复用同一用户和 job id，正确行为是 `409 import_job_active`。Parity 测试应在两个框架请求之间清理测试状态，分别验证其首次请求均为 202；另以独立断言锁定未清理时第二次请求返回 409。

### Voice、image generation 与 web 新契约

- capability 精确集合加入 `voice_transcript_list` 和 `voice_transcript_read`，数量由 expected 集合派生，并锁定二者为只读能力。
- memory fetch 完整卡片加入安全默认 `voice_call_id: ""`，另验证非空 ID 只出现在 fetch、不会进入轻量 index。
- model route 时间戳测试更新 `_ROUTE_COLUMNS` 的当前位置：14、17、20、24、25；只有 `created_at` 和 `updated_at` 必须非空并带 `Z`。
- chat poll 精确键集加入 `web_policy`，验证旧式 context 默认返回 `None`，显式 policy 原样投影。

### TEE verify 缺口

先增加行为测试：

1. RDS 与 TEE 都存在同一 `(user_id, call_id)` 时，verify report 包含 `voice_transcripts` 且 `rows_ok=true`。
2. 只在 RDS 存在时，`rows_ok=false`。

随后在 `tee_shadow.verify._CIPHERTEXT_TABLES` 增加配置：

- `rds_table="voice_transcripts"`
- `tee_table="voice_transcripts"`
- `item_col="call_id"`
- `pending_table="voice_transcripts"`
- `kind=None`

`kind=None` 明确表示本轮只做严格行数与 pending 核验，不假装执行尚未设计的 envelope 内容变换。

## 测试与提交策略

遵循 TDD：测试契约更新先证明原断言为何过时；真正生产缺口必须先新增失败行为测试并观察预期红灯，再写最小实现。修复按以下边界提交并逐项审查：

1. 收口 admin 导航、缓存和 Genesis parity 测试。
2. 收口 voice/image/web 响应契约测试。
3. 为 `voice_transcripts` 增加 verify 行为测试和最小配置。

验证顺序：10 个原失败节点、所有受影响测试文件、完整后端套件（排除 `tests/test_api.py`）。完整套件必须达到零失败；`tests/test_api.py` 的外部服务依赖作为已知环境前置条件单独记录，不计作通过。

## 风险控制

- HTML 标准化只剥离明确的 cache-note 元素，禁止宽泛清洗正文。
- 精确键集测试继续保留，以防无意泄漏；只添加已确认字段。
- Genesis parity 必须隔离状态，不能削弱真实 409 并发保护。
- TEE verify 不把行数核验描述成密文内容一致性核验。
- 所有修改都在 `fix/health-probe-isolation` worktree 分支完成；基线全绿后才进入健康探针 Task 1。
