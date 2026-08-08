# Runtime V2 Fable 5 / Opus 4.8 兼容性修复设计

## 背景

测试环境的纯 Runtime V2 实测确认了两个互相独立的问题：

- Fable 5 在普通聊天和工具请求的首轮均返回 HTTP 200，但没有可见文本或客户端工具调用。生产轨迹显示每轮只有 2–3 个 completion token；最小 A/B 进一步证明，强制模型公开输出“genuine train of thought”的 `<think>` 协议会让 Fable 5 返回 `stop_reason=refusal`，去掉该协议后普通聊天和原生 Anthropic 工具回灌均可工作。
- Opus 4.8 能正常聊天并发起工具调用，但 Runtime V2 在某种 memory discovery 工具成功后，会把该工具从下一轮 `tools` 目录移除，同时仍把包含该工具的原生 `tool_use` / `tool_result` 历史发给模型。Opus 4.8 会把这种历史与当前目录不一致解释为“之前的工具调用不真实”，继而停止或返回空内容。保持历史工具 schema 可见后，同一 A/B 能完成 `memory_index → memory_fetch → 最终回答`。

本设计只修改 Runtime V2。Runtime V1 由独立工作流处理，不在本次范围内。

## 目标与非目标

### 目标

1. Fable 5 的 V2 前台聊天不再被强制 `<think>` 协议触发拒绝。
2. Fable 5 允许没有思考气泡，但必须保留普通文本、原生客户端工具调用和现有空回复保护。
3. Opus 4.8 的原生工具历史与后续请求的工具目录保持一致。
4. memory discovery 每种模式仍最多真正执行一次；重复调用不得重复读取或产生额外副作用。
5. Opus 5 及其他现有模型继续使用当前 V2 self-thinking 行为。

### 非目标

- 不修改 Runtime V1 的 prompt、模型选择或工具循环。
- 不改变公开 API、数据库 schema、runtime fence 或部署拓扑。
- 不引入一次失败后再重试的 Fable 探测机制；已知不兼容请求应在第一次 provider call 前避免。
- 不重写全局 self-thinking 协议，也不对所有 Anthropic 模型关闭思考气泡。

## 方案选择

### 采用：V2 定向能力判断 + 保持历史工具 schema

Fable 5 在 V2 chat prompt 组装时跳过强制 self-thinking 指令；Opus 4.8 使用同一 provider-neutral 工具循环，但已进入原生 transcript 的 memory discovery schema 不再从后续 `tools` 中删除。若 prompt frontier 因预算不足省略其余可选工具目录，历史引用的 discovery schema 会作为 required component 保留；重复执行继续由已有 dispatch guard 阻止。

该方案在 provider call 前消除已知冲突，没有额外付费请求，并且不改变其他模型行为。

### 未采用：收到 `refusal` 后动态关闭 self-thinking

这种做法每个 Fable turn 至少浪费一次 provider call，增加延迟和费用；原始 system 指令还可能与纠正 suffix 冲突，恢复并不确定。

### 未采用：对所有 Anthropic 模型关闭 self-thinking

Opus 5 已在测试环境完整通过聊天和 tool call，没有证据支持扩大影响面。全量关闭会无故改变现有思考气泡体验。

## 详细设计

### 1. V2 模型 self-thinking 能力判断

在 `backend/model_api_runtime/v2/context.py` 增加一个 V2 内部纯函数，用当前 `ProviderConfig` 判断是否应在 chat system prompt 中追加 `core.self_thinking.INSTRUCTION`。

规则如下：

- 全局 `FEEDLING_V2_SELF_THINKING` 关闭时，所有模型都不追加。
- 模型标识按小写精确匹配 namespace 后的 `claude-fable-5`；例如 `anthropic/claude-fable-5` 会命中，而 `claude-fable-50` 不会。匹配时不追加强制 `<think>` 指令。
- 其他模型维持当前行为。
- provider 不作为唯一判据：同一个 Fable 模型通过 Anthropic、OpenRouter 或自定义 relay 接入时，公开思维链兼容性不应改变。

`chat_system_prompt` 接收可选 `provider_config`。生产 chat builder 必须传入当回合已解析的配置；无配置的 legacy/unit 调用保持当前默认行为，避免扩大测试夹具和非生产调用的变化面。

下游无需新增特殊分支。`core.self_thinking.split_thinking()` 对没有前导 `<think>` 的普通文本返回 `ABSENT` 并原样保留正文，因此 Fable 的普通回答不会被标记为思考失败。

### 2. Anthropic 原生工具历史一致性

删除 `backend/model_api_runtime/v2/tool_loop.py` 中根据 `completed_memory_discovery_tools` 从下一轮 `tools` 列表过滤 schema 的步骤。

已完成工具仍保留在 provider 目录中，原因是 transcript 已包含该工具的 provider-native assistant turn。后续请求需要让模型继续看到这段历史所引用的能力定义。

重复执行安全性继续由现有逻辑负责：

- `completed_memory_discovery_tools` 仍记录已成功 dispatch 的 discovery 名称。
- 后续模型若重复请求同一工具，validation 仍允许它通过，因为它是历史中已知的工具名。
- dispatch 分类将其放入 `repeated_memory_calls`，返回固定的 `already completed` 结果，不调用真实 executor。
- 同一 batch 内的重复 discovery 继续只 dispatch 第一个。

因此本次只改变“向模型展示 schema”，不改变执行次数、权限、provenance fence 或工具结果内容。

Prompt frontier 继续允许整份未引用工具目录作为 optional component 降级，但会把 `completed_memory_discovery_tools` 对应的 schema 拆成 `required_tool_schemas`。如果 required native transcript 加其引用 schema 已经超预算，回合在 provider call 前明确失败；如果只有其余目录超预算，则只发送历史引用 schema，避免发送“有 `tool_use/tool_result`、无对应定义”的不一致请求。

### 3. 空回复恢复语义

现有 `ProviderEmptyReply`、语义纠正重试、trajectory 事件和用户提示保持不变。本修复让两个已知冲突在空回复恢复之前消失，但不弱化其他 provider 异常的保护。

Fable 仍可能因其他安全原因返回真正的 `refusal`；这种情况继续走现有有界失败路径，不伪造模型正文。

## 测试设计

### Fable 5 prompt 测试

- 全局 self-thinking 开启时，`claude-fable-5` 的 V2 chat prompt 不包含 `self_thinking.INSTRUCTION`。
- namespace 形式的 Fable 5 model id 同样不包含该指令。
- 相似但不同的模型名（例如 `claude-fable-50`）仍保留现有 self-thinking 行为。
- Opus 5 仍包含该指令。
- 未传 `provider_config` 的调用维持当前行为。

### 工具循环测试

- `memory_index` 成功后的第二个 provider request 仍提供 `memory_index` 和 `memory_search` schema。
- 模型第二次请求 `memory_index` 时，真实 dispatcher 仍只调用一次，第二次收到固定的 `already completed` 结果。
- 同一 batch 的重复 discovery 仍只执行一次。
- Anthropic native `ToolExchange` 在 schema 保持可见时可继续编码为相邻的 assistant `tool_use` 和 user `tool_result`。
- frontier 省略可选目录时，历史引用的 discovery schema 与 native `ToolExchange` 仍在同一个 provider request 中；若两者作为 required components 也无法容纳，则 fail closed。

### 回归范围

- `tests/test_v2_context.py`
- `tests/test_v2_tool_loop.py`
- `tests/test_v2_prompt_frontier.py`
- `tests/test_v2_worker_tool_loop.py`
- 现有 provider Anthropic wire 测试
- Runtime V2 定向测试集合

## 文档与发布

该变化属于用户可见的模型兼容性修复，但不改变公开 API。实现提交应在 `docs-site/content/docs/changelog.mdx` 的 `Unreleased` 下记录：

- Fable 5 的 V2 聊天不再强制公开 self-thinking。
- 多轮工具调用保持历史工具定义可见，以兼容严格校验原生工具历史的模型。

不需要重新生成 OpenAPI。

## 验收标准

1. 所有新增测试先在未修改生产代码时按预期失败，再由最小实现转绿。
2. V2 定向回归无新增失败。
3. 测试环境纯 V2 Fable 5 普通聊天成功，且不要求思考气泡。
4. 测试环境纯 V2 Fable 5 能完成真实 `memory_index → memory_fetch` 工具链。
5. 测试环境纯 V2 Opus 4.8 能完成相同工具链，且轨迹包含工具计划、执行结果和最终回复。
6. Opus 5 普通聊天与工具链继续通过。
7. 所有临时用户、allowlist、jobs 和本地 orphan 清单均清理完成。
