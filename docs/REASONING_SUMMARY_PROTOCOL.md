# IO Chat 思考摘要协议

## 范围

本协议把面向用户的简短判断依据附着在同一条 assistant message 上。它不是
模型内部原始 chain-of-thought，也不允许以 Markdown、特殊标签或第二条消息
混入正文。

当前仓库包含后端和 resident consumer，但不再包含 iOS 客户端源码。iOS 已在
提交 `6cab45f` 中迁往独立私有仓库；当前环境也没有该仓库的访问凭据。因此本文
定义可验证的服务端协议和客户端接入契约，不声称已修改或签名发布客户端 UI。

## 1. Hermes shim → resident consumer

shim 的 stdout 每回合输出一个 JSON 对象：

```json
{
  "messages": ["assistant 正文"],
  "reasoning_summary": "面向用户的简短判断依据",
  "reasoning_kind": "provider_reasoning_summary",
  "reasoning_source": "hermes_provider_summary",
  "reasoning_model": "gpt-5.6-sol",
  "reasoning_native": true,
  "reasoning_conversation_id": "当前 Hermes session id",
  "reasoning_turn_id": "触发本轮的 user message id",
  "reasoning_source_id": "provider reasoning item id"
}
```

- `messages` 是正文数组；现有 consumer 的多气泡行为保持不变。
- `reasoning_summary` 是可选、非空字符串；没有安全摘要时整个字段省略。
- 摘要只取自明确的 provider summary surface：Codex Responses 的
  `codex_reasoning_items[].summary[type=summary_text]`，或 OpenRouter 的
  `reasoning_details[type=reasoning.summary]`。
- 不从通用 `reasoning`、`reasoning_content` 或加密 reasoning blob 回退，防止
  原始 scratchpad / chain-of-thought 被展示。
- consumer 只把摘要附加到该回合第一条 assistant message；它不会生成第二条
  聊天气泡。
- shim 只读取本次 `chat()` 返回的 current-turn messages，不从全局 history 或
  “最近一条 assistant”反推摘要。缺少 conversation / turn / source 任一关联 ID 时
  摘要直接省略。
- 只有首行 JSON 是回复协议；随后的 `session_id: ...` 仍是现有会话连续性元数据。

## 2. resident consumer → `/v1/chat/response`

首选传输是两个彼此独立的加密信封：

```json
{
  "envelope": { "...": "正文密文信封" },
  "thinking_envelope": { "...": "摘要密文信封" },
  "thinking_kind": "provider_reasoning_summary",
  "thinking_source": "hermes_provider_summary",
  "thinking_model": "gpt-5.6-sol",
  "thinking_native": true,
  "thinking_conversation_id": "当前 Hermes session id",
  "thinking_turn_id": "触发本轮的 user message id 或 proactive job id",
  "thinking_source_id": "provider reasoning item id",
  "thinking_assistant_message_id": "envelope.id",
  "thinking_update_seq": 1
}
```

`thinking_envelope` 仅在清洗后摘要非空时出现。正文仍只存在于 `envelope`，推送
预览也只取正文。后端同时保留 `reasoning_summary` 明文输入别名用于兼容其它
可信 caller；该别名必须携带同一组完整关联字段并通过同一套校验，之后后端才会
把它封装为 canonical `thinking_*` 子信封。缺少关联 ID 的旧明文调用会被拒绝，
不会降级为无绑定摘要。

后端对带摘要的响应执行 fail-closed 校验：五个关联字段必须完整，assistant id
必须与正文 `envelope.id` 相同，turn id 必须与 `reply_to_message_id`（主动消息则为
`proactive_job_id`）相同，`thinking_update_seq` 必须为正整数。校验失败时整条摘要
不会持久化，也不会尝试按时间顺序或“最新消息”重新归属。

## 3. `/v1/chat/history` → iOS

历史接口在同一条消息对象内返回正文信封字段和以下可选摘要字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `thinking_v` | String / Int 兼容解码 | 摘要信封版本 |
| `thinking_id` | String? | 摘要信封 ID |
| `thinking_body_ct` | String? | 摘要密文；非空表示有摘要候选 |
| `thinking_nonce` | String? | AES-GCM nonce |
| `thinking_K_user` | String? | 供当前设备解封的内容密钥 |
| `thinking_K_enclave` | String? | shared 信封可能存在；客户端展示不依赖 |
| `thinking_visibility` | String? | `shared` 或 `local_only` |
| `thinking_owner_user_id` | String? | 信封 owner |
| `thinking_content_pk_fpr` | String? | 内容公钥指纹（新信封） |
| `thinking_enclave_pk_fpr` | String? | enclave 公钥指纹 |
| `thinking_kind` | String? | 此功能固定为 `provider_reasoning_summary` |
| `thinking_source` | String? | 当前为 `hermes_provider_summary` |
| `thinking_model` | String? | 产生摘要的模型标签 |
| `thinking_native` | Bool? | `true` 表示 provider 原生 summary surface |
| `thinking_conversation_id` | String? | 产生摘要的 Hermes conversation |
| `thinking_turn_id` | String? | 触发该摘要的 user message / proactive job |
| `thinking_source_id` | String? | provider reasoning item 或稳定摘要哈希 ID |
| `thinking_assistant_message_id` | String? | 必须等于同一对象的 message `id` |
| `thinking_update_seq` | Int? | 单调版本；首个摘要为 `1` |

所有字段均为可选，以兼容旧消息、旧服务端和无摘要响应。客户端应使用现有正文
信封相同的解密原语解开 `thinking_*` 子信封，得到本地瞬态字段：

```swift
var reasoningSummary: String? // 解密后 trim；空字符串转 nil，不写回正文
```

不得把解密结果拼进 `content`，也不得把它追加为新的 `ChatMessage`。

## 4. iOS UI 接入要求

1. 在 assistant 气泡内部、正文之外渲染摘要 disclosure。
2. 仅当 `reasoningSummary?.isEmpty == false` 时显示入口，文案固定为“思考摘要”。
3. 每条消息的初始展开状态为 `false`；展开状态只保存在 SwiftUI 本地状态中，
   可按 message id 维护，不上传服务端。
4. 点击只切换本地展开状态，不发网络请求、不触发 agent turn，也不修改消息。
5. 摘要区域不是额外气泡；正文布局和可复制内容不依赖摘要是否展开。
6. 解密失败时按“无摘要”处理，不显示空入口，正文仍正常显示。

## 5. 上下文与计费

UI 展开/折叠不调用模型，因此不会重复计费。shim 只是从 Hermes 已收到并保存的
provider-native summary 数据中派生展示字段，不向后续对话新增一条 summary
message。Codex 为保持 Responses API reasoning item 连续性，原本就会在 Hermes
会话里保留/回放 provider reasoning item；本功能没有把 UI 字段再次注入上下文，
也不增加额外摘要生成调用。

## 6. 兼容性

- 旧客户端忽略新增 `thinking_*` 字段，正文不变。
- 新客户端对旧消息看不到 `thinking_body_ct`，因此不显示入口。
- provider 没有给出明确安全摘要时，shim 省略 `reasoning_summary`，整条链路不
  产生 `thinking_envelope`。