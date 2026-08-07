# Runtime V2 Provider 空响应恢复设计

**日期：** 2026-08-07

**状态：** 已批准设计

**范围：** Runtime V2 前台聊天、Provider 响应解析、加密轨迹遥测

**已复现 Provider / 模型：** 测试环境 Anthropic `claude-fable-5`

## 摘要

当 Provider 返回 HTTP 200，且响应结构合法，但既没有可见文本，也没有标准客户端
`tool_use` 时，Runtime V2 前台聊天目前会失败。Anthropic Fable 5 在完整 V2 提示词和
工具目录下能够稳定复现。Provider 解析器会在 V2 工具循环检查成功响应之前抛出异常，
随后这个没有 HTTP 状态码的解析异常又被误分类为 `upstream_unavailable`。

修复后，由 V2 负责前台空响应策略。Provider 解析器把结构合法的无文本响应返回给
V2，V2 工具循环使用原工具目录执行最多一次、有明确上限的语义纠正。如果纠正响应仍
为空，则通过现有 `provider_empty_reply` 路径结束。本次修改不会改变非 V2 调用方的
默认行为。

## 用户可见问题

模型配置探针能够通过，但实际前台聊天失败，用户看到“上游模型服务暂时不可用”。这个
提示不准确，因为 Provider 已经接受并完成了请求。

故障也阻断了工具调用。模型可能正在考虑使用工具，但 V2 没有拿到解析后的响应，因此
永远不会进入工具执行器。

## 证据

### 测试环境结果

| Provider 和模型 | 前台聊天 | 工具行为 | 结果 |
| --- | --- | --- | --- |
| Anthropic Opus 4.8 | 成功 | 成功 | 兼容 |
| Anthropic Opus 5 | 成功 | 成功 | 兼容 |
| OpenRouter Opus 4.8 | 成功 | 成功 | 兼容 |
| OpenRouter Opus 5 | 成功 | 成功 | 兼容 |
| Anthropic Fable 5 | 失败 | 没有工具事件 | 能复现本问题 |
| Anthropic Fable 5，精简合成提示词 | 成功 | 返回 `thinking` 和 `tool_use` | 支持工具调用 |
| OpenRouter Fable 5 | 未运行 | 未运行 | 当前 Key 的模型目录中不存在 |

在真实 Fable 5 V2 turn 中，加密 attempt trace 记录了两次上游 HTTP 200。两次都以
`postprocess_error` 结束，并抛出
`ProviderError("provider response had no usable reply text")`。V2 turn 记录了一个失败的
逻辑模型调用，没有任何工具事件。

### 已确认事实

- Anthropic API Key 有效。
- Fable 5 模型标识符可被接受。
- 当前账户能够调用该模型并产生计费。
- Provider 返回 HTTP 200，不是鉴权、余额、限流、网络或 5xx 错误。
- 请求没有因为工具 schema 非法而返回 HTTP 400 或 422。
- 故障发生在本地响应后处理阶段。
- 真实 V2 请求没有进入工具执行阶段。
- Fable 5 在精简合成提示词下能产生合法客户端 `tool_use`。

### 推断边界

真实 V2 响应很可能只有 thinking，或者包含某种成功的内容块，但该内容块没有被识别为
可见文本或客户端 `tool_use`。另一个最小化 Fable 5 请求已明确产生过 thinking-only
响应。

失败的生产形态响应正文没有保存在 attempt trace 中，因此无法确认其精确内容块序列。
实现必须基于与 Provider 无关的条件——“结构合法的成功响应，但没有可见文本，也没有
客户端工具调用”——而不能基于 Fable 特例假设。

## 根因

前台响应策略放在了错误的层级。

1. Anthropic 返回结构合法的 HTTP 200 响应。
2. `provider_client._parse_anthropic_body()` 尝试提取客户端工具调用。
3. 如果没有识别到工具调用，`_extract_anthropic_reply(required=True)` 会要求非空可见文本。
4. 它抛出 `ProviderError("provider response had no usable reply text")`。
5. 该错误产生于 HTTP 200 之后，因此没有 HTTP 状态码。
6. `classify_provider_error()` 将无状态码的响应形态错误归类为 transient。
7. V2 将 transient Provider 故障映射为 `upstream_unavailable`。

这使 `tool_loop.run_tool_loop()` 无法看到响应的 `stop_reason`、reasoning 是否存在、原生
assistant turn 或内容块形态，因此既不能纠正响应，也无法准确分类。

## 必须保持的既有行为

- 只有客户端工具调用、没有可见文本的响应合法，必须继续进入工具分发。
- 即使不要求可见文本，非法 JSON 或缺少 Provider 成功容器的 2xx 响应仍然是错误。
- Wake lane 可以有意不返回文本，并继续静默完成。
- 前台聊天最终必须产生可见回复或带归因的失败，不能静默完成。
- 工具 schema 拒绝 fallback 仍然只处理满足条件的 HTTP 400 或 422。
- 非法或超预算工具交换继续使用现有 tools-disabled fallback。
- 网络错误、限流和 5xx 继续使用现有 transport retry 与错误分类。

## 备选方案

### 方案一：只修错误分类

把 `no usable reply text` 从 `upstream_unavailable` 改成
`provider_empty_reply`。

优点是用户提示更准确，缺点是无法恢复聊天或工具调用，因此不足以解决问题。

### 方案二：为 Fable 单独修改提示词或维护模型白名单

识别 Fable 5 模型名称，追加一条 Provider 专用指令，要求输出可见文本或工具调用。

该方案依赖模型别名，对 relay 和未来模型不稳健；也无法处理其他兼容模型偶发的空成功
响应。因此不采用。

### 方案三：V2 通用语义恢复

允许 V2 收到结构合法的空成功响应，并携带原工具执行一次有上限的纠正。该方案保留工具
能力，恢复失败时能够准确归因，也能覆盖未来模型。

采用此方案。

## 设计

### Provider 解析边界

Runtime V2 前台 turn 中，`run_tool_loop()` 调用 Provider 时传入
`require_reply=False`。该参数只改变 Provider 解析器对合法无文本响应的处理，不改变
“前台聊天最终必须回复”的业务要求。

Provider 结果必须保留：

- `reply`，允许为空字符串；
- `reasoning`，如果 Provider 提供；
- `stop_reason`；
- 归一化后的 usage；
- 已解码的客户端工具调用；
- Provider 原生 assistant turn。

非法成功响应仍然抛错。V2 之外的调用方继续使用默认 `require_reply=True`，避免扩大行为
变化。

### 空成功判定

只有解析成功后，V2 工具循环才判断空成功：

```text
可见文本为空
AND 已解码的客户端工具调用为空
AND Provider 响应具有合法成功结构
```

即使存在 reasoning，也不能视为可用的前台回复。

### 一次性纠正

工具循环增加等价的 turn 内状态：

```python
empty_response_recovery_used = False
empty_response_retry_instruction = ""
```

前台 turn 第一次遇到空成功时：

1. 记录加密 `protocol_fallback` 轨迹事件，reason 为
   `empty_provider_success`。
2. 标记本 turn 已使用恢复机会。
3. 清除这次不可用响应累计的 reasoning。
4. 从同一份按时间排序的 transcript 重新构建 prompt。
5. 临时把下方纠正指令追加到 system message。
6. 保留相同的工具目录。
7. 从现有 turn 调用预算中消耗下一次调用。

纠正指令：

```text
The previous response completed without visible text or a client tool call.
Complete the user's request now. Return either non-empty visible answer text or
a valid call to one of the offered client tools. Do not return a thinking-only
response.
```

该指令只存在于运行时，不持久化为用户或 assistant 消息。纠正产生可见文本或工具调用后
立即移除。

### 纠正时保留工具

纠正请求继续携带原工具目录。如果禁用工具，会破坏真正需要记忆、网络、workspace、定时
任务或 MCP 能力的请求，并可能诱导模型声称完成了实际上没有执行的操作。

现有 tools-disabled terminal fallback 仍然只用于非法、未声明、重复 ID、混合 mutation /
reply、超预算的工具交换，以及满足条件的工具 schema 拒绝。

### 最终行为

如果纠正响应仍为空，工具循环不再重试。它将空的 terminal candidate 交给现有前台回复
边界，由该边界抛出 `TurnError("empty_reply")`。Worker 已经会将其映射为
`provider_empty_reply`。

不需要新增公共错误类型。

### Reasoning 隔离

不可用响应的 reasoning 不能合并进纠正后的最终回复。进入纠正请求前，必须清空 reasoning
累计列表和去重集合。

如果纠正随后产生工具调用，只有成功工具路径中的 reasoning 才能按正常规则进入最终
reasoning channel。

### 调用和费用预算

每个前台 turn 最多执行一次空响应恢复，并计入现有 `max_calls` 上限。不得建立独立重试
循环。

当前已观察到的故障路径本来就会执行两次完全相同的 Provider 请求，因为第一次
post-processing error 被归类为 transient。修改后，同一个上限变成一次原始请求加一次
带纠正信息的语义请求。对于本次故障，最坏 Provider 调用次数不会增加。

Transport failure 继续使用现有有界重试策略。语义空响应恢复不得在现有规则之外放大
transport attempt 数量。

## 错误分类

| 条件 | 错误类型 |
| --- | --- |
| HTTP 401 或 403 | `auth_invalid` |
| HTTP 402 | `quota_insufficient` |
| HTTP 400 或 422 不兼容 | `provider_incompatible` |
| HTTP 429 | `rate_limited` |
| HTTP 5xx 或网络故障 | `upstream_unavailable` |
| 两次合法成功都没有文本或工具调用 | `provider_empty_reply` |
| 本地解析或协议实现错误 | `reply_parse_failed` |

第一次空成功不报告为 Provider 故障，因为 V2 仍有一次有界恢复机会。

## 可观测性与隐私

加密 trajectory 必须能诊断恢复过程，同时不能新增明文 prompt 或响应日志。Provider 响应
或其加密摘要应提供：

```json
{
  "stop_reason": "max_tokens",
  "content_block_types": ["thinking"],
  "has_visible_text": false,
  "reasoning_present": true,
  "tool_call_count": 0,
  "recovery_used": true
}
```

具体字段取决于各 Provider 解析器可获得的信息。字段必须不含内容且有大小上限。可以记录
未知内容块的名称，但不能把内容块正文复制到明文日志。

预期加密事件序列：

```text
provider_request
provider_response
protocol_fallback(reason=empty_provider_success)
provider_request(recovery=true)
provider_response
```

普通日志或 metrics 不得新增 API Key、用户消息、system prompt、reasoning 正文、工具参数
或工具结果。

## 配置探针

`test_provider_key()` 有意使用 `require_reply=False`，因为其契约是验证凭证、模型、访问权
和计费能力，而不是完整的前台聊天兼容性测试。

本设计不改变该公共契约。新增独立 `chat_compatible` 探针结果属于 API 和产品变化，不在
本次修复范围内。应先通过 runtime recovery 让合法空成功可恢复，或在恢复失败时准确归因。

## 测试方案

### Provider 解析器测试

- Anthropic thinking-only 配合 `require_reply=False` 时返回结构化结果。
- Anthropic thinking-only 配合 `require_reply=True` 时，非 V2 调用方继续得到现有错误。
- Anthropic 只有工具调用、没有文本的响应在 `require_reply=True` 下继续合法。
- 非法 2xx 响应在 `require_reply=False` 下仍然失败。
- 同时包含 text 和 thinking 的响应行为不变。
- 其他 Provider wire 的合法空响应解析不发生回归。

### 工具循环测试

- thinking-only 后返回可见文本，turn 成功完成。
- thinking-only 后返回 `memory_index`，真实分发工具，随后返回最终文本。
- 纠正调用保留原工具名称和 schema。
- 纠正指令只出现在纠正请求中，不进入持久 transcript。
- 不可用响应的 reasoning 不附加到最终回复。
- 连续两次空成功只执行两次语义调用，并以 `empty_reply` 结束。
- 同一个 turn 后续再出现空响应时不能重新获得恢复机会。
- 网络错误、限流和 HTTP 5xx 重试行为不变。
- 现有工具 schema 和非法工具交换 fallback 不变。
- Wake lane 的有意静默继续成功。
- 文件交付恢复和 terminal fallback 不变。

### Worker 与 trajectory 测试

- 最终 `empty_reply` 映射为 `provider_empty_reply`，而不是
  `upstream_unavailable`。
- 加密 trajectory 记录 `empty_provider_success` 以及是否已使用恢复。
- model call 和 usage 计数包含两次成功 Provider 响应。
- 恢复成功的 turn 记录 Provider success，且不设置 Provider cooldown。
- 最终空响应只产生一个带归因的用户可见失败。

### 测试环境验收

使用隔离的 V2 测试用户验证：

- Anthropic Fable 5 普通前台聊天；
- Fable 5 明确请求 `memory_index`；
- Fable 5 在真实工具结果后返回最终文本；
- Anthropic Opus 4.8 和 Opus 5 回归；
- OpenRouter Opus 4.8 和 Opus 5 回归；
- 重复空成功最终产生一个 `provider_empty_reply`；
- trajectory 能支持诊断，但不泄露明文秘密或对话内容。

## 预计代码改动

主要实现：

- `backend/provider_client.py`
- `backend/model_api_runtime/v2/tool_loop.py`
- `backend/model_api_runtime/v2/worker.py`，仅在错误分类或 trajectory 接线确有需要时修改

测试：

- `tests/test_provider_client.py`
- `tests/test_v2_tool_loop.py`
- `tests/test_v2_worker.py`
- 现有 V2 trajectory 测试模块

文档：

- 在 `docs-site/content/docs/changelog.mdx` 的 Unreleased 下增加记录
- 除非实现修改了公共响应 schema，否则不需要重新生成 OpenAPI

## 验收标准

- Fable 5 能在测试环境完成 Runtime V2 普通前台聊天。
- Fable 5 能执行至少一个真实平台工具并返回最终文本。
- 合法 thinking-only 成功不再变成 `upstream_unavailable`。
- 每个前台 turn 最多执行一次空响应恢复。
- 恢复不突破现有模型调用预算。
- 纠正保留工具，且运行时指令不持久化。
- 第一次不可用响应的 reasoning 不附加到最终回复。
- Opus 和 OpenRouter 回归用例继续通过。
- Wake lane 的有意静默行为不变。
- 重复空成功以 `provider_empty_reply` 结束。
- 加密诊断能区分 empty / thinking-only 响应形态，且不新增敏感明文遥测。
- 向生产环境推进前，必须记录测试环境证据。

## 不在本次范围内

- Provider 专用的 Fable prompt tuning。
- 新增公共 Provider 兼容性探针或 API 字段。
- 修改 Wake lane 的静默策略。
- 修改工具授权、mutation 安全或工具结果编码。
- 未完成测试环境验证前进行生产部署。
