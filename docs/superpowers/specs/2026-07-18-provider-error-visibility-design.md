# provider 错误可见性 — 阶段二设计 spec

日期：2026-07-18
状态：**第 3 稿**（第 1 稿 4 个断点、第 2 稿 3 个生命周期缺口，均由 Codex review 指出并逐条核实成立后修订）
分支：backend `fix/provider-error-notice-blame-throttle`、iOS `fix/provider-error-preserve-code`

前置：
- `2026-07-06-upstream-error-surfacing-design.md`（聊天 system 气泡 + 设置页 last_runtime_error）
- `2026-07-07-unified-error-surfacing-design.md`（通知中心 `/v1/notices` + blame 纪律）
- `docs/FRONTEND_ERROR_CONTRACT.md`（iOS 消费面契约）
- 阶段一已合本分支：导入顶层失败按真因归责、聊天横幅节流分三桶、iOS 兜底保留错误码

## 指导原则（hx 定，2026-07-18）

> **provider 的错都应该抛出来 —— 那是用户的东西，不是我们服务端的问题，需要他们自己解决。**

| blame | 处理 | 理由 |
|---|---|---|
| `user_provider` | 必须抛给用户，给行动指引 | 他不修就永远不好 |
| `provider_transient` | 也要抛（是他的中转），措辞轻 | 是他的东西，但会自愈 |
| `system` | 我们兜着，**保留有温度的兜底话术** | 抛给用户他也解决不了 |

**反向纪律**：不是他的错，别赖给他。

**验收纪律**：每块改动必须指出**用户在哪个屏幕、什么时刻**看到变化；指不出来的移出本期。前两稿共 7 个缺陷中有 5 个是这一类。

## 目标与范围

**§2 聊天失败可见性** —— 用户当场看到真实原因与该做什么。
**§3 onboarding 降级可见性** —— 用户知道哪些能力没建立、为什么。

**建议分批交付**（见文末「交付建议」）：§2 自成闭环可先行；§3 经两轮 review 后已确认牵动 genesis 的结果持久化与生命周期契约，工作量显著大于 §2。

## 非目标

- **不做通知中心**：`/v1/notices` 后端已实现且四条 lane 在写，iOS 从未接入。本设计**反转** 2026-07-07 spec 的 iOS 接入计划：provider 错属于「用户在场」的错，应在触发它的地方当场抛。保持只写不读。
- **不做重试按钮**。
- **不做中转能力探测三态**：第 1 稿含此项，第 2 稿移除。onboarding 配置调用方以 `let (config, _) = ...` 丢弃 warnings，`/routes` 只存 `supports_responses` 不返回 warning，**用户看不到任何变化**。问题本身仍真实（网络抖动被说成「你的中转不支持」），待有可见出口再做。
- **不让 system 气泡穿透 iOS 实时轮询**。
- **不动既有机制**（各有设计原因，hx 定「只做加法」）：`FALLBACK_REPLY`（人设温度 + 老版兼容）、`reply_status="replied"`（409 双扣防护，07-06 spec role 审计表）、前台横幅 3h 节流（Seven 2026-07-11）、`unknown` 分类边界、`content_filtered` blame 归属。
- **不做设置页 `last_runtime_error` 展示**。
- **不覆盖 supervisor 层失败**。
- **不改变 job 成败判定**：降级仍标完成，只补可见性。「provider 失败到什么程度该判失败」需 Seven 另议。

## 决策记录

| 决策点 | 结论 | 否决项与理由 |
|---|---|---|
| 兜底话术去留 | 服务端照发，客户端按 blame 决定隐藏 | 否决「不发兜底」：兼着老版兼容，不发会让未升级用户从「看到一句话」退化成「完全没反应」 |
| 新旧版差异 | 服务端同一条消息 + 标记，客户端按标记渲染 | 否决「按版本写不同文案」：消息持久化，升级后翻历史前后不一致 |
| 失败态载体 | **双载体**：兜底消息带实时事件 + 用户消息 metadata 存持久真相，**兜底消息为权威** | 否决「仅 metadata」：增量拉取按原始 ts 过滤，更新旧消息不改 ts，**实时链路不通**；否决「复用 system 气泡」：被 3h 节流卡住 |
| 兜底是否隐藏 | **按 blame 分**（见 §2.3） | 第 1 稿「无条件隐藏」与指导原则第三档及自身测试矛盾 |
| 重试语义 | 本版不提供重试按钮，user_provider 给「去设置」 | 否决「清 replied 重新排队」：动 409 双扣防护；否决「重试=发新消息」：留重复消息。且余额不足时重试本就无效 |
| onboarding 目标路径 | **genesis**（`useGenesisOnboarding` 默认 true，真实用户全走这条） | 第 1 稿只做 legacy `history_import`，等于真实用户看不到（hx 定「肯定做新路」） |
| degraded 权威落点 | **per-job 结果**（`GET /v1/genesis/imports/{job_id}` 直接返回），genesis state 仅镜像 | 否决「只写 genesis state」：iOS 只读 `obj["job"]`，从不读 `state`（已核实）；且 state 是 per-user 最新态、非 per-job，并发 backfill 时可能指向别的 job。否决「塞进现有 output」：`genesis_set_job_status()` 整块替换 output 而非 merge，后台阶段写入会覆盖前景产出 |
| 降级判据 | **结构化 provenance + 确定性计数器**，不用「显著缺失」阈值 | 否决「记忆条数阈值」：0 条可能只是材料里没有值得存的长期记忆，不代表失败；阈值会变成「2 条算显著、3 条不算」的产品玄学 |
| degraded 归纳位置 | 独立纯函数模块，genesis 与 history_import 共用 | 否决「放 notices/catalog」：catalog 只负责 `error_class → blame/user_text`，不应理解 window / 身份卡 / 开场白等业务细节 |

---

## §2 聊天失败可见性

### 2.1 双载体

**已核实的断点**：增量拉取按 `float(m.get("ts",0)) > since` 过滤，用户消息更新 metadata **不产生新 ts**，永不重新返回；iOS 增量轮询又只取 `m.ts > since && m.isFromAgent`。仅写 metadata 的话真实效果是「杀 App 后才看到失败态」，与「当场抛给用户」相反。

| 载体 | 承担 | 为何需要 |
|---|---|---|
| **兜底回复消息**（新消息、新 ts） | 实时事件 + 全量解析 | 新追加的 agent 消息，天然过增量轮询与 `isFromAgent` 过滤 |
| **用户消息 metadata** | 冗余持久化 | 兜底消息缺失时的兜底恢复路径 |

**权威顺序：兜底消息优先。** `update_chat_message_metadata` 仅在 parent 存在于当前 worker 内存时才落 DB 且调用方忽略返回值——跨 worker 可能静默写失败。让实时载体同时支撑全量解析，则该风险不影响功能正确性。metadata 写入失败须记 log。

### 2.2 新增字段

**兜底回复消息**：`reply_to_message_id`、`error_class`、`blame`、`user_text`。
**用户消息 metadata**（冗余）：`reply_error_class`、`reply_blame`、`reply_user_text`，需加入 `store.py::update_chat_message_metadata` allowlist。

**为何下发 `user_text`**：后端 `notices/catalog.py` 是文案唯一权威；只发 `error_class` 让 iOS 本地映射会再造一份割裂文案表（`SceneErrorCopy` 已是第三份分类器）。

**detail 不下发**：可能夹带 provider HTML、request id、敏感上下文。契约测试固化 `user_text ≤ 500` 且断言不含原始 provider detail。

### 2.3 显示矩阵

| blame | 用户消息 | 兜底气泡 | 行动入口 |
|---|---|---|---|
| `user_provider` | 失败态 + `user_text` | 隐藏 | 「去设置」→ 模型配置页 |
| `provider_transient` | 失败态 + `user_text` | 隐藏 | 无 |
| `system` | 不变 | **保留显示** | 无 |

### 2.4 归并矩阵（分页 / orphan）

关联归纳必须在**每次 `upsertMessages`、冷缓存恢复、加载 older 之后**重跑，不能只在「实时收到新消息」时执行。

| 已加载内容 | 行为 |
|---|---|
| 兜底事件 + 用户消息 | 事件优先，更新用户消息 reply-outcome，按 blame 隐藏/保留兜底 |
| 只有用户 metadata | 用 metadata 恢复 reply-outcome |
| **只有兜底事件，parent 未加载**（分页切割） | **暂不隐藏事件**，作为独立失败提示展示；加载 older 后归并 |
| 两载体冲突 | 兜底事件优先，记诊断 log |
| 两者皆无 | 保持旧行为 |

第三行是关键：若无条件隐藏，会出现「错误原因和兜底一起消失」。

> ring trim 不构成风险：用户消息一定早于其兜底回复，正常 trim 会先淘汰用户消息，不会出现「兜底还在、parent 已删」。

### 2.5 后端改动

1. `tools/chat_resident_consumer.py`：兜底分支写回复时随 `post_reply` 带 `classify_agent_error` 结果（沿用 07-06 给 `post_reply` 加 `role`/`notice_kind` 的模式，加可选参数）。
2. `backend/chat/chat_core.py`：写 `reply_status` 同处附带三字段；**`reply_status` 与 409 双扣防护一字不改**；metadata 写失败改为记 log。
3. `backend/core/store.py`：allowlist 增三键。

后台车道失败**不写**这些字段（不进聊天流，Seven 2026-07-11）。

### 2.6 iOS 改动

**语义分离（不复用 `deliveryState`）**：

- `deliveryState` 语义**保持不变**：只表达「用户消息有没有成功发到服务器」。第 424 行的 `deliveryState = .sent` **不动**。
- **新增** `replyFailure: ReplyFailure?`：表达「agent 是否成功回答」。两者是不同语义，不可混用。
- 视觉上复用现有失败气泡样式，**不渲染重试按钮**。
- `retryMessage()` 仅接受真正的 delivery failure；**provider reply failure 永不进入现有 retry 分支**。

**关联层**：reply-outcome 不能只在单条 `ChatMessage.init(from:)` 内推导——assistant 事件必须在**消息列表 reconciliation 层**关联回 user message（见 §2.4 矩阵）。

### 2.7 兼容性与契约

- 老版 App：写入完全相同的兜底消息，不认识新字段 → **渲染结果与现状一致**（响应体确实多了字段，故不称「逐字节一致」）。
- 新增 history JSON 字段是 **additive public API contract**，须同步 OpenAPI、`FRONTEND_ERROR_CONTRACT.md`、changelog。
- 已知遗留：`system` notice 气泡在全量 history 仍渲染，重启后可能与失败态并存造成重复。本期不处理。

---

## §3 onboarding 降级可见性（genesis 主路径）

### 3.1 genesis 的实际失败语义（与 history_import 不同）

**关键事实（本轮核实，修正前两稿的框定）**：genesis 里 `classify_provider_error(e) == "provider_config"`（402 余额不足 / 401·403 坏 key / 其它 4xx 配置类）在 fact_map 路径上是 **`raise` 硬中断** → job 判 `failed` → 走 `mark_failed` → 已有 notice 与 `job.error`，**本就是大声报错**，不属于静默降级。

静默降级路径**只有 transient 重试耗尽**（429/5xx/网络/坏 JSON）→ `history_windows_failed += 1` → 跳过该窗继续。

**但 genesis 内部不一致（本轮新发现，Codex 未提）**：identity 路径复用 `history_import._derive_identity_with_provider`，该推导器**吞掉所有异常**并返回兜底身份，`foreground_identity` 只按 warning 重试。代码注释自陈：

> NOTE: the deriver hides the status code, so 402/quota also gets a few (capped) retries

即：同一个「余额不足」，走 fact_map 会硬中断判失败，走 identity 却静默降级成通用身份卡。**degraded 归责必须处理这一不一致**，否则 identity 降级会被错误归成 `provider_transient`。实现计划需决定：是让推导器透出状态码（注释里已预告的 refine），还是在归纳层按可得信息保守归责。

### 3.2 为何不能按 warning 前缀归纳

| warning | 错误解读 | 实际语义 |
|---|---|---|
| `identity_guard_no_ai_source_used_generic_identity` | 「身份卡降级」 | **不是失败**。触发条件是用户材料里本就没有 AI 侧内容，是合法 guard |
| `provider_onboarding_greeting_failed` / `_empty` | 「没有开场白」 | **有开场白**，只是通用兜底文案 |
| `provider_candidate_json_repair_failed` / `retry_failed` | 「记忆丢失」 | 后续拆分重试**可能已成功** |

代码库已有正确判据：`genesis/foreground_identity.py::_provider_failed()` 已区分 `provider_identity_failed`（真失败）与 `identity_guard_no_ai_source`（合法无信号）并写了注释。**沿用之。**

### 3.3 判据：结构化 provenance + 确定性计数器

**memory** —— 用 genesis 既有计数器，不用条数阈值：

| 条件 | 判定 |
|---|---|
| `history_windows_failed == 0` | **不判降级**，哪怕最终记忆为 0 |
| `0 < failed < total` | `memory_partial`（部分材料没完成提取） |
| `failed == total && total > 0` | `memory_unavailable` |

最终记忆条数**只用于展示**，不作失败判据。

**前置改动**：`worker.py` 当前在 `except` 里只累加计数、**原始异常随即丢弃**，归纳层因此不知该归 user_provider / provider_transient / system。需在该 catch 点保留**脱敏后的结构化未恢复失败**：

```
capability=memory  stage=fact_map
error_class=upstream_unavailable  blame=provider_transient  exhausted=true
```

**不保存原始 detail。**

**greeting** —— 返回结构化 provenance，**不靠比对文案字符串猜是不是兜底**：`generated` / `fallback_provider_error` / `fallback_empty` / `append_failed`。（当前 `plaintext.py` 直接丢弃 `_warnings`。）

**identity** —— 沿用 `_provider_failed()`：仅 `provider_identity_failed` 判降级，合法 guard 不判。归责受 §3.1 不一致影响。

### 3.4 结果存放：per-job 权威

- **权威**：`GET /v1/genesis/imports/{job_id}` 返回的 job/result 必须**直接包含 `degraded`**（iOS 只读 `obj["job"]`，已核实从不读 `state`）。
- **镜像**：`genesis_state` 可镜像一份供 gate / validation / admin 使用，但**不能是 iOS 唯一数据源**；若读 state，必须先校验 `state.job_id == 请求的 job_id`（state 是 per-user 最新态，并发 persona backfill 时可能指向别的 job）。
- **不塞现有 `output`**：`genesis_set_job_status()` 整块替换 output 而非 merge，后台阶段写 `{stage, error}` 会覆盖前景产出的 degraded。需给 job 独立持久字段，或定义不被阶段 output 覆盖的专用结果存储。

```
degraded: {
  causes: [ { capability, error_class, blame, user_text } ],   # 允许多因，不压成一条
  affected_capabilities: [ "identity" | "memory" | "greeting" ]
}
```

### 3.5 生命周期：两套「完成」

**已核实**：genesis v2 前景建好基础记忆/身份/greeting 后**立即标 `done`**，之后才跑 background enrichment；而 iOS 一看到 `done` 就**结束轮询并清除 active job**。因此后来才产生的后台失败即使写进 job，**也没人再读**。

必须拆成两个状态：

| 状态 | 含义 | 用途 |
|---|---|---|
| `foreground_done` / `chat_ready` | 可以进入产品 | 放用户进去 |
| `background_status` = `processing` \| `completed` \| `degraded` | 后台丰富是否终结 | 决定何时停止轮询 |

iOS 在 foreground done 后放用户进入，但**保留一个轻量轮询直到 background terminal**。否则「纳入 background failure」只是纸面支持。

> 注意：`background_status` / `background_error` 是 legacy `history_import` 的现成字段，**genesis 目前没有同名契约**——这是**新增 genesis 字段**，不是复用。

### 3.6 展示位置

**第一展示位**：现有私钥交接完成页（`ChatEmptyStateView.swift`）。

- job 仍在跑 → 「TA 还在后台建立中」
- 正常结束 → 提示消失或转完成态
- degraded → 私钥卡下方显示受影响能力与原因

**用户可能在后台完成前离开**。采用：**持久化一条 job-specific、一次性的 onboarding result**，在 Identity 首页首次展示后标记已读。（否决「禁止离开直到 background terminal」：卡用户。）

该一次性结果仍属 onboarding 第一落点，**不是通知中心**，也不会两天后才冒出来（hx 定：记忆花园不加）。

---

## 测试

### 后端 · §2

- consumer：兜底分支携带失败元信息；后台车道**不**携带（回归）。
- `chat_core`：写 `reply_status` 时同写三字段；**409 双扣防护行为不变**（回归断言）；metadata 写失败记 log。
- allowlist：新键可写、非白名单键仍被拒。
- **实时链路**：兜底消息带 `reply_to_message_id` + 错误字段出现在 `since` 增量响应中（直击第 1 稿断点）。
- 契约：`user_text ≤ 500`、不含原始 provider detail。

### 后端 · §3

- **per-job 权威**：`GET /v1/genesis/imports/{job_id}` 响应含 `degraded`；后台阶段写 output 后 degraded **不被覆盖**（直击第 2 稿断点）。
- state 镜像：`state.job_id` 与请求 job 不一致时不得被采信。
- degraded 归纳（纯函数单测，最关键的一组）：
  - `history_windows_failed == 0` → **不判降级**，即使记忆为 0
  - `0 < failed < total` → `memory_partial`；`failed == total` → `memory_unavailable`
  - 合法 guard（`identity_guard_no_ai_source`）→ **不产生** degraded
  - greeting provenance = `fallback_provider_error` → 判降级；`generated` → 不判
  - 多因并发 → `causes[]` 多条，不压成一条
  - 无降级 → 不写 `degraded` 键
- provenance 落地：worker catch 点保留结构化未恢复失败且**不含原始 detail**。
- 生命周期：foreground done 后 `background_status` 仍为 `processing`，terminal 后转 `completed`/`degraded`。

### iOS（无测试 target，必须真机）

1. **正常聊天零变化**（最高优先级回归）：文字、图片、连续多轮。
2. **实时性**：配错 key → 发消息 → **不杀 App、不刷新**，当场看到失败态与原因。
3. `user_provider` → 失败态 + 「去设置」，兜底气泡不出现。
4. `system`（turn_timeout）→ **仍显示兜底话术**，无行动入口。
5. 杀 App 重开 → 失败态仍在。
6. **分页 orphan**：滚动到兜底消息在页内、parent 在上一页 → 确认不隐藏、显示为独立失败提示；加载 older 后归并。
7. genesis 降级：私钥交接页显示；**中途离开再回 Identity 首页仍能看到一次**；已读后不再重复出现。
8. 无降级时不出现任何提示。

## 风险与回滚

- **最大风险：影响正常聊天**。缓解：后端只在失败分支写入，成功路径不变；新字段缺失时 iOS 退化为现状行为，不会更糟。真机回归以「正常聊天零变化」为第一验收项。
- 跨 worker metadata 静默写失败：已由「兜底消息为权威载体」消解。
- §3 涉及 genesis 结果持久化与轮询生命周期，**回滚面大于 §2**，故建议分批（见下）。
- 部署：backend 与 agent-runner 镜像同批；iOS 随后任意节奏，后端先行完全兼容。

## 交付批次（hx 定，2026-07-18：拆批）

经两轮 review，§2 与 §3 体量明显不对等：

- **§2**：自成闭环，改动集中在 consumer / chat_core / store allowlist + iOS 渲染层。
- **§3**：牵动 genesis 结果持久化（per-job 权威字段）、生命周期契约（foreground/background 两套状态）、多处 provenance 采集（worker catch 点、greeting）、iOS 轮询策略与一次性结果存储。

**决定：拆成两批。**

| 批次 | 范围 | 状态 |
|---|---|---|
| **第一批** | **§2 聊天失败可见性** | **本 spec 立即进入实现计划；尽快上 test 验证，不拉长战线** |
| 第二批 | §3 onboarding 降级可见性 | 待第一批落地后另起（§3 内容保留在本文档作为输入，实现前需另起 spec 并再过一轮 review——两轮 review 各挖出新的生命周期问题，判断尚未探底） |

理由：§2 已可独立交付用户可见价值；绑在一起会让整批的回滚面与验收周期都被 §3 放大。

**第一批的完成定义**：后端 + iOS 改动合入、后端测试绿、真机跑完 §「iOS」测试清单第 1–6 项（第 7–8 项属 §3，第二批再验），上 test 环境实测一轮。

## 与既有系统的关系

- `/v1/notices` 保持只写不读。
- 聊天 system 气泡与 3h 节流保持原样。
- 阶段一已合三项不受影响，本设计在其之上叠加。
- 阶段一分支与远端基线有漂移，#86/#107 合并后需重跑一次链路 review。
