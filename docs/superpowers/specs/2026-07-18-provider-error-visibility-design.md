# provider 错误可见性 — 阶段二设计 spec

日期：2026-07-18
状态：设计定稿，待 Codex review → 实现计划
分支：backend `fix/provider-error-notice-blame-throttle`、iOS `fix/provider-error-preserve-code`（阶段一同分支续做）

前置：
- `2026-07-06-upstream-error-surfacing-design.md`（聊天 system 气泡 + 设置页 last_runtime_error）
- `2026-07-07-unified-error-surfacing-design.md`（通知中心 `/v1/notices` + blame 纪律）
- `docs/FRONTEND_ERROR_CONTRACT.md`（iOS 消费面契约）
- 阶段一已合本分支：导入顶层失败按真因归责、聊天横幅节流分三桶、iOS 兜底保留错误码

## 指导原则（hx 定，2026-07-18）

> **provider 的错都应该抛出来 —— 那是用户的东西，不是我们服务端的问题，需要他们自己解决。**

推论出的三档处理纪律（沿用既有 blame 分类）：

| blame | 处理 | 理由 |
|---|---|---|
| `user_provider` | 必须抛给用户，给行动指引 | 他不修就永远不好，我们替他扛只是拖延 |
| `provider_transient` | 也要抛（是他的中转），但会自愈，措辞轻 | 是他的东西，但不用他动手 |
| `system` | 我们兜着，保留有温度的兜底话术 | 抛给用户他也解决不了 |

**反向纪律同样成立**：不是他的错，别赖给他。探测失败不等于他的中转不支持（见 §4）。

## 背景

阶段一盘点（CC + Codex 双向独立审计后交叉对账）确认：provider 失败时后端其实**认得出**原因，但到不了用户眼前。最典型：

- 用户余额不足 → agent 回合失败 → consumer 写一句 agent 口吻的兜底「我这会儿有点慢，刚刚没接上」→ 用户反复重发，始终看到同一句，不知道要充值（prod 案例 usr_0d16bfd4，2026-07-05，最终放弃聊天）。
- 同一句兜底话术同时覆盖余额不足、key 失效、我们自己崩了——三种归责完全不同的失败，用户看到的字一模一样。
- 新用户 onboarding 时余额不足 → 记忆抽取跳过、身份卡回落成通用、开场白为空 → job 仍标 `completed`，用户只看到「初始化完成」。

## 目标

1. 聊天失败时，用户看到**真实原因**与**该做什么**，而不是通用兜底。
2. onboarding 降级完成时，用户知道**哪些没生成、为什么**。
3. 中转能力探测失败时，不把「我们没测出来」说成「你的中转不支持」。

## 非目标（明确不做）

- **不做通知中心**：`/v1/notices` 后端已实现且四条 lane 在写，但 iOS 从未接入。2026-07-07 spec 原计划 iOS 接入——**本设计反转该计划**。理由：provider 错属于「用户在场」的错，应在触发它的地方当场抛；收件箱式的沉淀面只对「用户不在场时发生的事」有价值，优先级低。`/v1/notices` 保持只写不读，零用户可见风险。
- **不做重试按钮**：见 §2 决策记录「重试语义」。
- **不让 system 气泡穿透 iOS 实时轮询**：做完 §2 后信息已挂在消息上，气泡是重复。
- **不动以下既有机制**（阶段一讨论中确认它们各有设计原因，hx 定「只做加法」）：
  - 兜底话术 `FALLBACK_REPLY`（承担人设温度 + 老版 App 兼容）
  - `reply_status="replied"` 标记（承担 409 双扣防护，见 07-06 spec role 审计表）
  - 前台横幅 3h 节流分桶（Seven 2026-07-11 决策）
  - `unknown` 分类边界（hx：不可控，不动）
  - `content_filtered` 的 blame 归属（改成消息失败态后，挪组对用户无差别）
- **不做设置页 `last_runtime_error` 展示**：状态刷新语义（何时刷新、何时清旧错）未定，hx 判风险偏高；替代想法「立即验证按钮」另议。
- **不覆盖 supervisor 层失败**：发生在 consumer 起来之前，走不到本上报路径（沿用 07-06 spec 的 out of scope）。

## 决策记录（brainstorm 结论，2026-07-18）

| 决策点 | 结论 | 否决项与理由 |
|---|---|---|
| 兜底话术去留 | **服务端照发不变**，新版 App 客户端隐藏 | 否决「provider 错不发兜底」：兜底兼着老版 App 兼容，不发会让未升级用户从「看到一句话」退化成「完全没反应」，比现状更差，且不是纯加法 |
| 新旧版差异实现 | 服务端写同一条消息 + 标记，客户端按标记决定渲染 | 否决「服务端按 App 版本写不同文案」：消息是持久化的，用户升级后翻历史会前后文案不一致 |
| 新版 App 呈现 | **只显失败态，隐掉兜底气泡** | 否决「两个都显示」：「我稍后接」与「额度不足请充值」语义打架 |
| 失败态传递载体 | **标在用户自己那条消息的 metadata 上** | 否决「标在兜底消息上」：客户端需倒查配对，且失败态仍是本地推导、重启即丢；否决「复用 system 气泡」：被 3h 节流卡住，拿不到每条消息各自的状态 |
| 重试语义 | **本版不提供重试按钮**，`user_provider` 类给「去设置」入口 | 否决「清除 replied 标记重新排队」：动 409 双扣防护承重点，非加法；否决「重试=发新消息」：聊天记录留重复用户消息。且对最常见的余额不足，重试本就无效——用户真正需要的是充值 |
| onboarding 降级 | **不判失败**，但完成时必须说明降级内容 | hx 定：不改变 job 成败判定，只补可见性 |
| 降级提示位置 | **仅 onboarding 等待页/完成时**（第一现场） | hx 定：记忆花园不加。理由：用户在第一现场仍处于「我在配置这产品」心态，愿意去处理；等他用两天后才发现，已经形成「这产品就这样」的判断 |
| 那 5 类的精准文案 | **不做** | 原计划并入通知中心，通知中心砍掉后悬空；在 iOS 侧补本地映射会再造一份与后端 catalog 割裂的文案表，得不偿失 |

---

## §2 聊天失败可见性（核心）

### 2.1 数据通路（已验证）

```
consumer: call_agent 抛异常
   → classify_agent_error(exc) → AgentErrorNotice(error_class, blame, user_text, detail)
   → 现有三腿全部保留：兜底话术 / system 气泡（节流）/ POST runtime_error
   → 【新增】post_reply 携带失败元信息
        ↓
backend /v1/chat/response
   → 写兜底消息（现状不变）
   → 更新用户消息 metadata：现有 reply_status/reply_message_id + 【新增】三个失败字段
        ↓
backend chat history（_chat_history_item：`item = dict(m)` 整条透传）
   → 新字段自动下发，传输层零改动
        ↓
iOS: 解码 → 消息失败态 + 原因 + 「去设置」；隐藏 reply_message_id 指向的兜底气泡
```

关键验证点（实现前已 grep 确认）：

- `backend/core/store.py::update_chat_message_metadata` 有 **allowlist**（现含 `reply_status`/`reply_message_id`/`replied_by`/`replied_at` 等），新键需显式加入——加法，一处。
- 该函数内 `msg.update(clean)` 直接落到消息 dict 上；`backend/chat/service.py::_chat_history_item` 以 `dict(m)` 整体透传，故新字段自动到达 iOS，**无需改传输契约**。
- iOS 当前**完全没有解码** `reply_status`（grep 零命中），故新字段无老逻辑冲突。
- iOS `ChatMessage` 解码器第 424 行硬编码 `deliveryState = .sent`，本地失败态一经重新解码即丢失（重启/重载缓存）。本设计让失败态**由服务端字段派生**，重启后自动重新正确——顺带消解该缺陷，无需单独修复。

### 2.2 新增字段

写在**用户消息**的 metadata 上（不是兜底消息）：

| 字段 | 示例 | 用途 |
|---|---|---|
| `reply_error_class` | `quota_insufficient` | 失败类型；非空即表示「这轮是兜底糊的，不是真回复」 |
| `reply_blame` | `user_provider` | 决定是否给行动入口 |
| `reply_user_text` | `模型服务额度不足，充值后再发消息即可恢复。` | 直接展示，iOS 无需本地映射 |

**为何连 `user_text` 一起下发**：后端 `notices/catalog.py` 是文案的唯一权威。若只下发 `error_class` 让 iOS 本地映射，就会再造一份与后端割裂的文案表（`SceneErrorCopy` 已是第三份分类器，不应加厚）。metadata 单值上限 500 字符，容得下。

**detail 不下发**：原始上游报错可能夹带 provider HTML、request id 等噪音甚至敏感上下文。排障走既有的设置页 `last_runtime_error` 与 admin 面。

### 2.3 后端改动

1. `tools/chat_resident_consumer.py`：兜底分支已有 `pending_failure_notice = e`；在写兜底回复时把 `classify_agent_error` 的结果随 `post_reply` 带上（沿用 07-06 spec 给 `post_reply` 加 `role`/`notice_kind` 的同一模式，加可选参数，不改既有调用）。
2. `backend/chat/chat_core.py`：写 `reply_status` 的同一处，附带写入三个新字段。**`reply_status` 语义与 409 双扣防护逻辑一字不改。**
3. `backend/core/store.py`：metadata allowlist 增三键。

后台车道（heartbeat/proactive/capture/dream）失败**不写**这些字段——它们不进聊天流（Seven 2026-07-11 决策），保持原样。

### 2.4 iOS 改动

1. `ChatMessage`：解码三个新字段；`deliveryState` 由 `reply_error_class` 非空派生为失败态（替换第 424 行的硬编码 `.sent`，仅在服务端明确标失败时覆盖，其余保持 `.sent`）。
2. 渲染：
   - 用户消息气泡下方展示 `reply_user_text`
   - `reply_blame == "user_provider"` → 附「去设置」入口，跳模型配置页
   - 其余 blame → 只展示原因，无行动入口
   - **不渲染重试按钮**（复用现有失败态样式时需显式抑制——现有失败气泡默认带重试）
3. 隐藏兜底气泡：用户消息 metadata 中已有的 `reply_message_id` 指向兜底消息，渲染时跳过该 id 的消息。

### 2.5 老版 App 行为

服务端写入完全相同的兜底消息，老版不认识新字段 → 渲染与现状**逐字节一致**。零回退风险。

---

## §3 onboarding 降级可见性

### 3.1 问题

`backend/hosted/history_import.py` 中 provider 失败大量被吞成 warning、流程继续降级完成，job 仍标 `completed`。已定位的吞没点（8 类）：

| warning slug | 用户感知的后果 |
|---|---|
| `provider_candidate_extraction_failed_window_N` | 该时间窗的记忆没抽出来 |
| `provider_candidate_json_repair_failed_window_N` | 同上（抽取结果损坏且修复失败） |
| `provider_candidate_retry_failed_window_N_part_M` | 同上（重试也失败） |
| `provider_memory_extraction_failed_window_N` | 记忆没抽出来 |
| `provider_identity_failed` | **身份卡回落成通用身份** |
| `identity_guard_no_ai_source_used_generic_identity` | **身份卡是通用的（未用 AI 材料）** |
| `provider_onboarding_greeting_failed` | 没有开场白 |
| `provider_onboarding_greeting_empty` | 开场白为空 |

另有独立字段承载后台阶段失败：`background_status="failed"` + `background_error`（与 `warnings` **不是同一字段**，只读 `warnings` 会漏）。

`warnings` 已随 job 下发、iOS 也已解码（`warnings: [String]?`），但**全仓无任何渲染点**（grep 确认零使用）。

### 3.2 做法

**后端**：在 job 里新增结构化降级摘要字段 `degraded`（无降级时不写该键），把上述 warning slug + `background_status`/`background_error` 归纳为：

- `error_class` / `blame` / `user_text`（复用 `notices/catalog.py`，与 §2 同源）
- 受影响的能力清单（身份卡 / 记忆 / 开场白）

**由后端归纳而非 iOS 解析原始 slug**：slug 是自由文本形态（含异常类名与截断详情），让客户端解析会再造一份脆弱的解析逻辑；且文案权威在后端 catalog。

**iOS**：仅在 onboarding 等待页/完成时渲染该摘要。记忆花园不加（hx 定）。

### 3.3 边界

- 导入**不判失败**：`status` 仍为 `completed`，仅补充可见性。
- 「provider 失败到什么程度该让整个导入判失败」是独立的产品策略问题，**不在本 spec 范围**，需 Seven 拍板后另议。

---

## §4 中转能力探测三态

### 4.1 问题

`backend/provider_client.py::probe_responses_support` 当前返回 `bool`：任何异常、非 2xx、非 JSON、error-shaped 2xx 一律 `False`。`False` 会生成 `responses_unsupported` 警告，其文案（`notices/catalog.py`）是：

> 你选的中转不支持 Responses 协议，AI 的记忆和工具调用可能不稳定。建议换一个支持 /v1/responses 的中转，或改用 Claude 类模型。

即：网络抖动、超时、429、5xx 都会被说成「你的中转不支持」，用户白换中转。这违反指导原则的反向纪律——**把我们没测出来的事赖给用户的东西**。

### 4.2 做法

探测结果由两态改三态：

| 结果 | 判定依据 | 回退行为 | 用户文案 |
|---|---|---|---|
| 支持 | 2xx 且 JSON 对象且无顶层 `error` | 走原生 Responses | 无 |
| **明确不支持** | 收到明确响应但表明不支持（4xx 明确语义 / error-shaped 2xx） | 回退兼容桥接 | 「不支持，建议换一个」（可绝对，因确诊） |
| **未探测成功** | 超时 / 网络错 / 5xx / 非 JSON | 回退兼容桥接（**与现状一致**） | 「探测超时，已按兼容模式运行。可能是网络波动；如果持续有问题，可以试试换个中转。」（不下定论，但给行动建议） |

**回退行为三态一致**——这是现状且正确，本次只改「说什么」。新增 error_class `responses_probe_inconclusive`（blame=`provider_transient`）进 `notices/catalog.py`；`responses_unsupported` 保留，仅收窄到「明确不支持」才使用。

hx 明确要求：未探测成功时**不要沉默**，要给出可能原因与「可以试试换个中转」的软建议，只是不下「你的中转不支持」这个定论。

---

## 测试

### 后端

- consumer：兜底分支携带失败元信息；后台车道**不**携带（回归）。
- `chat_core`：写 `reply_status` 时同写三字段；**`reply_status` 与 409 双扣防护行为不变**（回归断言）。
- metadata allowlist：新键可写、非白名单键仍被拒。
- history 透传：三字段出现在 `/v1/chat/history` 响应。
- 降级摘要：8 类 warning 与 `background_status=failed` 均能归纳出正确 `error_class`/`blame`；无 warning 时不产生摘要。
- 探测三态：明确不支持 / 超时 / 网络错 / 5xx / 非 JSON / 成功，六个用例断言三态归类与回退行为一致性。

### iOS

无测试 target，**必须真机验证**（此类为渲染与文案行为，单测不覆盖）：

1. **正常聊天零变化**（最高优先级回归）：发文字、发图、连续多轮，确认成功路径与改动前完全一致。
2. 故意配错 key → 发消息 → 确认显示「API Key 无效」+「去设置」，且兜底气泡不出现。
3. 余额不足场景 → 确认文案与行动入口正确。
4. 我们自己的错（如 turn_timeout）→ 确认**仍显示兜底话术**，不显示行动入口。
5. 杀掉 App 重开 → 确认失败态仍在（服务端派生）。
6. onboarding 降级 → 确认等待页显示降级摘要。

## 风险与回滚

- **最大风险：影响正常聊天**。缓解：所有后端改动只在失败分支写入，成功路径代码不变；新字段缺失时 iOS 退化为现状行为（兜底话术照显），不会更糟。真机回归以「正常聊天零变化」为第一验收项。
- 隐藏兜底气泡依赖 `reply_message_id` 存在；若该字段缺失（历史消息、异常路径），退化为「兜底气泡照显 + 失败态也显」——信息重复但不丢失，可接受。
- 三个 Phase（§2/§3/§4）互相独立，可分别回滚。§4 纯后端、无客户端依赖，可先行上线。
- 部署：backend 与 agent-runner 镜像同批（consumer 改动）；iOS 随后任意节奏，后端先行完全兼容。

## 与既有系统的关系

- `/v1/notices` 保持只写不读，本设计不接入、不移除。
- 聊天 system 气泡与 3h 节流保持原样，仅作为后台观测面继续存在。
- 阶段一已合的三项（导入归责、节流分三桶、iOS 保留错误码）不受影响，本设计在其之上叠加。
