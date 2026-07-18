# provider 错误可见性 — 阶段二设计 spec

日期：2026-07-18
状态：**第 2 稿**（第 1 稿经 Codex review 发现 4 个「代码做完但产品目标未达成」的断点，已全部核实成立并修订）
分支：backend `fix/provider-error-notice-blame-throttle`、iOS `fix/provider-error-preserve-code`（阶段一同分支续做）

前置：
- `2026-07-06-upstream-error-surfacing-design.md`（聊天 system 气泡 + 设置页 last_runtime_error）
- `2026-07-07-unified-error-surfacing-design.md`（通知中心 `/v1/notices` + blame 纪律）
- `docs/FRONTEND_ERROR_CONTRACT.md`（iOS 消费面契约）
- 阶段一已合本分支：导入顶层失败按真因归责、聊天横幅节流分三桶、iOS 兜底保留错误码

## 指导原则（hx 定，2026-07-18）

> **provider 的错都应该抛出来 —— 那是用户的东西，不是我们服务端的问题，需要他们自己解决。**

推论出的三档纪律（沿用既有 blame 分类）：

| blame | 处理 | 理由 |
|---|---|---|
| `user_provider` | 必须抛给用户，给行动指引 | 他不修就永远不好 |
| `provider_transient` | 也要抛（是他的中转），措辞轻 | 是他的东西，但会自愈 |
| `system` | 我们兜着，**保留有温度的兜底话术** | 抛给用户他也解决不了 |

**反向纪律同样成立**：不是他的错，别赖给他。

**验收纪律（第 2 稿新增）**：每块改动都必须指出**用户在哪个屏幕、什么时刻**看到变化。指不出来的，从本期范围移除——这正是第 1 稿的系统性缺陷。

## 背景

阶段一盘点确认：provider 失败时后端**认得出**原因，但到不了用户眼前。典型：用户余额不足 → agent 回合失败 → 写一句 agent 口吻的兜底「我这会儿有点慢，刚刚没接上」→ 用户反复重发始终看到同一句，不知道要充值（prod 案例 usr_0d16bfd4，2026-07-05，最终放弃）。同一句兜底同时覆盖余额不足、key 失效、我们自己崩了——三种归责完全不同，用户看到的字一模一样。

## 目标

1. **聊天失败**：用户当场看到真实原因与该做什么。
2. **onboarding 降级**：用户知道哪些能力没建立、为什么。

## 非目标（明确不做）

- **不做通知中心**：`/v1/notices` 后端已实现且四条 lane 在写，iOS 从未接入。2026-07-07 spec 原计划 iOS 接入——**本设计反转该计划**。理由：provider 错属于「用户在场」的错，应在触发它的地方当场抛。`/v1/notices` 保持只写不读。
- **不做重试按钮**：见决策记录。
- **不做中转能力探测三态（`probe_responses_support`）**：第 1 稿含此项，**第 2 稿移除**。Codex 核实：改完分类更准，但 onboarding 配置调用方以 `let (config, _) = ...` 丢弃 warnings，`/routes` 路径只存 `supports_responses` 不返回 warning，**用户看不到任何变化**。按验收纪律移出本期，待有可见出口时再做。（该问题仍然真实：网络抖动会被说成「你的中转不支持」，误导用户白换中转。）
- **不让 system 气泡穿透 iOS 实时轮询**。
- **不动以下既有机制**（各有设计原因，hx 定「只做加法」）：兜底话术 `FALLBACK_REPLY`（承担人设温度 + 老版 App 兼容）、`reply_status="replied"`（承担 409 双扣防护，见 07-06 spec role 审计表）、前台横幅 3h 节流分桶（Seven 2026-07-11）、`unknown` 分类边界、`content_filtered` 的 blame 归属。
- **不做设置页 `last_runtime_error` 展示**（状态刷新语义未定，hx 判风险偏高）。
- **不覆盖 supervisor 层失败**（发生在 consumer 起来之前，沿用 07-06 spec 的 out of scope）。
- **不改变 job 成败判定**：降级仍标 `completed`，只补可见性。「provider 失败到什么程度该判失败」是独立产品策略，需 Seven 拍板后另议。

## 决策记录

| 决策点 | 结论 | 否决项与理由 |
|---|---|---|
| 兜底话术去留 | **服务端照发不变**，客户端按 blame 决定是否隐藏 | 否决「provider 错不发兜底」：兜底兼着老版 App 兼容，不发会让未升级用户从「看到一句话」退化成「完全没反应」，比现状更差 |
| 新旧版差异实现 | 服务端写同一条消息 + 标记，客户端按标记渲染 | 否决「服务端按 App 版本写不同文案」：消息是持久化的，用户升级后翻历史会前后不一致 |
| 失败态传递载体 | **双载体**：兜底回复消息带实时事件 + 用户消息 metadata 存持久真相 | 否决「仅用户消息 metadata」：**增量轮询按原始 ts 过滤，更新旧消息不改 ts，实时链路根本不通**（第 1 稿致命缺陷，Codex 发现）；否决「复用 system 气泡」：被 3h 节流卡住 |
| 兜底气泡是否隐藏 | **按 blame 分**：user_provider / provider_transient 隐藏；**system 保留** | 第 1 稿「无条件隐藏」与指导原则和自身测试用例矛盾（Codex 发现） |
| 重试语义 | **本版不提供重试按钮**，user_provider 给「去设置」 | 否决「清除 replied 标记重新排队」：动 409 双扣防护承重点；否决「重试=发新消息」：留重复用户消息。且余额不足时重试本就无效 |
| onboarding 目标路径 | **genesis（当前默认路径）** | 第 1 稿只做 `history_import`——但 `useGenesisOnboarding` 默认 `true`，真实用户全走 genesis，legacy 路径仅调试可切。第 1 稿等于「代码做完真实用户看不到」（Codex 发现，hx 定：「肯定做新路」） |
| degraded 归纳依据 | **最终能力产物 + 未恢复的失败** | 否决「warning 前缀命中即判降级」：三类语义不准（详见 §3.1） |
| degraded 归纳位置 | 独立纯函数模块，genesis 与 history_import 共用 | 否决「放 notices/catalog」：catalog 只负责 `error_class → blame/user_text`，不应理解 window / 身份卡 / 开场白等业务细节（Codex 建议，采纳） |

---

## §2 聊天失败可见性

### 2.1 实时链路（第 1 稿此处是断的）

**已核实的断点**：`backend/chat/chat_core.py` 的增量拉取按 `float(m.get("ts",0)) > since` 过滤——用户消息被更新 metadata **不会产生新 ts**，永远不会重新返回；iOS `ChatViewModel` 增量轮询又只取 `m.ts > since && m.isFromAgent`。第 1 稿只写用户消息 metadata 的话，真实效果是：

> 兜底气泡实时出现 → 用户消息仍显示成功 → 杀 App 或全量同步后失败态才出现

与「当场抛给用户」正好相反。

**修订：双载体**

| 载体 | 承担 | 为何需要 |
|---|---|---|
| **兜底回复消息**（新消息，新 ts） | 实时事件 | 它是新追加的 agent 消息，天然通过增量轮询与 iOS 的 `isFromAgent` 过滤 |
| **用户消息 metadata** | 持久真相 | 全量 history / 重启后恢复失败态 |

**权威顺序**：两者同时存在时以**兜底回复消息**为准。理由：Codex 核实 `update_chat_message_metadata` 仅在 parent 存在于当前 worker 内存时才落 DB，且调用方忽略返回值——跨 worker 场景下 metadata 可能静默写失败。让实时载体同时也能支撑全量 history 解析，metadata 退化为冗余持久化，则该风险不影响功能正确性。metadata 写入失败须记 log（当前被忽略）。

### 2.2 新增字段

**兜底回复消息**上（随消息体下发）：`reply_to_message_id`（指向用户消息）、`error_class`、`blame`、`user_text`。

**用户消息 metadata** 上（冗余持久化）：`reply_error_class`、`reply_blame`、`reply_user_text`。需加入 `backend/core/store.py::update_chat_message_metadata` 的 allowlist。

**为何连 `user_text` 一起下发**：后端 `notices/catalog.py` 是文案唯一权威。只下发 `error_class` 让 iOS 本地映射，会再造一份与后端割裂的文案表（`SceneErrorCopy` 已是第三份分类器，不应加厚）。

**detail 不下发**：原始上游报错可能夹带 provider HTML、request id 等噪音甚至敏感上下文。排障走既有设置页 `last_runtime_error` 与 admin 面。**契约测试固化 `user_text ≤ 500` 且禁止写入原始 provider detail**（Codex 建议，采纳；当前 catalog 最长约 80 字符，余量充足）。

### 2.3 显示矩阵（消灭第 1 稿的自相矛盾）

| blame | 用户消息 | 兜底气泡 | 行动入口 |
|---|---|---|---|
| `user_provider` | 失败态 + `user_text` | **隐藏** | 「去设置」→ 模型配置页 |
| `provider_transient` | 失败态 + `user_text` | **隐藏** | 无 |
| `system` | 不变（正常态） | **保留显示** | 无 |

`system` 保留兜底是指导原则第三档的直接落地：我们的锅，用户做不了什么，留住有温度的话。

### 2.4 后端改动

1. `tools/chat_resident_consumer.py`：兜底分支已有 `pending_failure_notice`；写兜底回复时随 `post_reply` 带上 `classify_agent_error` 结果（沿用 07-06 spec 给 `post_reply` 加 `role`/`notice_kind` 的同一模式，加可选参数，不改既有调用）。
2. `backend/chat/chat_core.py`：写 `reply_status` 的同一处附带写入三个新字段。**`reply_status` 语义与 409 双扣防护逻辑一字不改**；metadata 写入失败改为记 log（原忽略返回值）。
3. `backend/core/store.py`：metadata allowlist 增三键。

后台车道（heartbeat/proactive/capture/dream）失败**不写**这些字段——不进聊天流（Seven 2026-07-11 决策）。

### 2.5 iOS 改动

1. `ChatMessage`：解码新字段。
2. **实时路径**：收到带 `error_class` 的兜底消息 → 按 §2.3 矩阵更新内存中对应用户消息（依 `reply_to_message_id`）→ 决定是否渲染该兜底气泡。
3. **全量/重启路径**：从兜底消息（优先）或用户消息 metadata 派生失败态。当前解码器第 424 行硬编码 `deliveryState = .sent`，改为仅在服务端明确标失败时覆盖。
4. 失败态复用现有失败气泡样式，但**不渲染重试按钮**（现有失败气泡默认带重试，需显式抑制）。

**语义澄清**（Codex 提出，采纳）：`deliveryState` 原义是「消息是否发送成功」，本设计引入的是「agent 是否成功回答」——**不同语义**。实现上新增 reply-outcome 概念，仅复用失败态的**视觉样式**，不直接复用 `.failed` 的语义位。

### 2.6 兼容性与契约

- 老版 App：服务端写入完全相同的兜底消息，老版不认识新字段 → **渲染与现状一致**。（第 1 稿写「逐字节一致」不准确：JSON 响应体确实多了字段，一致的是渲染结果。）
- 新增 history JSON 字段是 **additive public API contract**，须同步 OpenAPI、`FRONTEND_ERROR_CONTRACT.md` 与 changelog（Codex 提出，采纳）。
- 已知遗留：`system` notice 气泡在全量 history 仍会渲染，重启后可能与失败态并存造成重复。本期不处理（不穿透实时轮询只解决了实时路径），记为已知问题。

---

## §3 onboarding 降级可见性（改打 genesis 主路径）

### 3.1 为何不能按 warning 前缀归纳

第 1 稿列的 8 类 warning 中，至少三类语义不成立（Codex 指出，逐条核实成立）：

| warning | 第 1 稿的错误解读 | 实际语义 |
|---|---|---|
| `identity_guard_no_ai_source_used_generic_identity` | 「身份卡降级」 | **不是失败**。触发条件 `not has_ai_persona and not has_assistant_history and not has_ai_memory`——用户材料里本就没有 AI 侧内容，是合法 guard |
| `provider_onboarding_greeting_failed` / `_empty` | 「没有开场白」 | **有开场白**，只是通用兜底文案 |
| `provider_candidate_json_repair_failed` / `retry_failed` | 「记忆丢失」 | 后续拆分重试**可能已成功**，不能只看 warning 断言 |

**代码库已有正确判据**：`backend/genesis/foreground_identity.py::_provider_failed()` 已经把 `provider_identity_failed`（真 provider 失败，值得重试）与 `identity_guard_no_ai_source`（合法无信号，不该重试）区分开并写了注释。归纳逻辑**沿用这一既有先例**。

### 3.2 目标路径：genesis

`useGenesisOnboarding` 默认 `true`（`ChatEmptyStateView.swift`），真实用户全走 genesis；`history_import` 仅调试可切回。故 degraded 产出**以 genesis 为主**。

genesis 与 history_import 天然可共用归纳逻辑：`backend/genesis/foreground_identity.py` 直接 `from hosted import history_import`，复用同一个身份推导器，产出同一套 warning 词汇。

### 3.3 degraded 结构

放入 genesis state（`backend/genesis/service.py::write_genesis_state` 当前为固定形状，需增键；`identity_status` 只表示身份是否初始化，不表示是否降级，**不可复用**）。无降级时不写该键。

```
degraded: {
  causes: [ { error_class, blame, user_text } ],       # 允许多因，不压成一条
  affected_capabilities: [ "identity" | "memory" | "greeting" ]
}
```

**多因不压缩**（Codex 建议，采纳）：一次 job 可能同时因不同原因降级，单个 `error_class/blame/user_text` 会把多因压成一个。

**归纳依据 = 最终能力产物 + 未恢复的失败**，而非 warning 前缀命中：

- `identity`：最终身份卡是否为兜底产物 **且** 归因于 `provider_identity_failed`（而非合法 guard）
- `memory`：最终记忆条数为 0 或显著缺失 **且** 存在未被后续重试恢复的抽取失败
- `greeting`：开场白是否为兜底文案 **且** 归因于 provider 失败

同时纳入 `background_status="failed"` / `background_error`（与 `warnings` 是不同字段，只读 warnings 会漏）。

**归纳位置**：新建独立纯函数模块（如 `backend/onboarding_degraded.py`），genesis 与 history_import 共用。不放 `notices/catalog.py`——catalog 只负责 `error_class → blame/user_text`。

### 3.4 展示位置

**仅 onboarding 第一现场**（hx 定：记忆花园不加。理由：用户在第一现场仍处于「我在配置这产品」心态，愿意去处理；用两天后才发现已形成「这产品就这样」的判断）。

需覆盖 fresh start 路径：当前 fresh start 提交 genesis 后**立即进入私钥交接、后台继续跑**，失败只记埋点（`ChatEmptyStateView.swift`）。故展示位需明确到「后台完成时用户所在的那个屏幕」，实现计划中定死具体落点。

---

## 测试

### 后端

- consumer：兜底分支携带失败元信息；后台车道**不**携带（回归）。
- `chat_core`：写 `reply_status` 时同写三字段；**`reply_status` 与 409 双扣防护行为不变**（回归断言）；metadata 写失败记 log。
- metadata allowlist：新键可写、非白名单键仍被拒。
- **实时链路**：兜底消息带 `reply_to_message_id` + 错误字段出现在 `since` 增量响应中（直击第 1 稿断点）。
- 契约：`user_text ≤ 500`；断言不含原始 provider detail。
- degraded 归纳（纯函数单测，最关键的一组）：
  - 合法 guard（`identity_guard_no_ai_source`）→ **不产生** degraded
  - greeting 兜底 → `affected_capabilities` 含 `greeting`，不表述为「没有开场白」
  - 抽取失败但后续重试成功 → **不产生** memory 降级
  - 多因并发 → `causes[]` 多条，不压成一条
  - `background_status=failed` 单独触发
  - 无降级 → 不写 `degraded` 键

### iOS

无测试 target，**必须真机验证**：

1. **正常聊天零变化**（最高优先级回归）：文字、图片、连续多轮。
2. **实时性**：配错 key → 发消息 → **不杀 App、不刷新**，确认当场看到失败态与原因（直击第 1 稿断点）。
3. `user_provider`（余额/key）→ 失败态 + 「去设置」，兜底气泡不出现。
4. `system`（如 turn_timeout）→ **仍显示兜底话术**，无行动入口（验证 §2.3 矩阵）。
5. 杀 App 重开 → 失败态仍在。
6. genesis onboarding 降级 → 第一现场显示 degraded；无降级时不出现任何提示。

## 风险与回滚

- **最大风险：影响正常聊天**。缓解：后端改动只在失败分支写入，成功路径不变；新字段缺失时 iOS 退化为现状行为（兜底照显），不会更糟。真机回归以「正常聊天零变化」为第一验收项。
- 跨 worker metadata 静默写失败：已通过「兜底消息为权威载体」设计消解，metadata 仅作冗余。
- §2 与 §3 互相独立，可分别回滚。
- 部署：backend 与 agent-runner 镜像同批（consumer 改动）；iOS 随后任意节奏，后端先行完全兼容。

## 与既有系统的关系

- `/v1/notices` 保持只写不读。
- 聊天 system 气泡与 3h 节流保持原样，作为后台观测面继续存在。
- 阶段一已合三项（导入归责、节流分三桶、iOS 保留错误码）不受影响，本设计在其之上叠加。
- 阶段一分支与远端基线有漂移，相关文件新增漂移基本无冲突；#86/#107 合并后需重跑一次链路 review（Codex 提示）。
