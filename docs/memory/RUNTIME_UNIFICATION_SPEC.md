# Runtime Unification Spec — VPS ⇄ API 统一

> 来源：2026-06-23 语音过代码 + 架构讨论。这份文档把当时口头定的结论落成可执行的 spec。
> 给 浩轩 / 志豪：先读这一篇，再读 `MODEL_API_PATH_P0.md`（`PROACTIVE_V2_ARCHITECTURE.md` 已删，见 git 历史）。
>
> **一句话**：现在 VPS 用户和 API 用户在后端走两条完全不同的代码路径，没有理由。
> 产品的核心卖点是"让用户自己的 agent 实时感知屏幕"，但现在 agent 根本拿不到屏幕图像。
> 先把 **VPS 这条线** 端到端修通修对，再把 **API 用户做成一个 agent runtime**，让它和 VPS 走同一套东西。

---

## 0. 背景：两类用户

| | VPS 用户 | API 用户 |
|---|---|---|
| 形态 | 自己 VPS 上跑 Cloud Code / Codex，**有真实的 agent loop** | 只有一次 LLM call，**没有 loop**，我们在外面套了个假 agent |
| 现状代码 | MCP 工具面 (`mcp_server.py`)，pull 式：agent 自己调 `feedling_screen_*`、`feedling_memory_*` | push 式：`_model_api_context_messages` 把 screen/memory/identity 拼进 prompt |
| 谁更完整 | **agent loop 更真**（这才是真实目标用户） | context 拼装更全，但 agent 是手搓 JSON |
| 妹妹实际在用 | ✅ Cloud Code / Codex | ⚠️ 兜底 |

> 注：妹妹群体试过 OpenClaw/OpenCloud 基本都放弃了（"对工具来讲跟它聊天很奇怪"），
> 也基本没人用 Hermes。**所以 onboarding/测试以 Cloud Code / Codex 真实环境为准**，
> Hermes / OpenClaw 路径不是当前重点。

---

## 1. 核心问题（都已对照代码确认）

| # | 问题 | 代码位置 | 性质 |
|---|---|---|---|
| P1 | VPS 和 API 两套 context 拼装，没有共用 | API: `app.py:8421` `_model_api_context_messages`；VPS: `mcp_server.py:845` `feedling_chat_get_history` + 独立的 `feedling_screen_*`(518–595) | 架构分叉 |
| P2 | **agent 拿不到屏幕**：API 把 screen 藏在关键词门后；VPS 的 chat history 根本不带 screen | `app.py:8410` `_model_api_should_attach_screen`；VPS 的 screen 是另一个 pull 工具 | 直接 bug，违背卖点 |
| P3 | 主动唤醒 (2.6) 的 `recent_chat_context` 既没 screen 也没 memory | `app.py:1718` `_build_proactive_v2_wake_decision`，`gate_input.memory_context` 硬编码 `identity_loaded:False, memory_count:0, decrypt_ok:False` (1845–1852) | 设计成 agent-owned，但 VPS agent 唤醒时什么都没被预取 |
| P4 | VPS consumer 拿不到记忆花园 / 身份卡 (2.7)，但 API 能拿 | 同上 wake builder 不加载；MCP 虽有 `feedling_memory_list/get`(1712) `feedling_identity_get`(1295)，但纯 pull、唤醒时不 surface | 不对称，没道理 |
| P5 | API 的"agent"是手搓 JSON 协议，不是 runtime | `hosted_runtime.py:73–194`（`reply`+`tool_requests` 契约 + 单独的 background controller） | 手搓 if-JSON-do-something |
| P6 | web search 手搓 DuckDuckGo HTML 正则爬虫 | `model_api_runtime/tools.py:122` `web_search_duckduckgo` | 该删，换 provider 自带 |
| P7 | 屏幕信息没被蒸馏成 memory（购物那个例子） | `prompts.py:75` `build_memory_capture_messages` + `app.py:9444` `_model_api_run_memory_capture` 只吃聊天文本；`semantic_analysis.py` 不碰 frame | 能力缺失 |
| P8 | 屏幕信息存内存里，没落库 | `store.frames_meta`（in-memory） | backlog：PG JSONB / Cloudflare |
| P9 | memory 选择是自己写的启发式，不是 Memory Palace 包 | `context_memory_selection.py`（`default` vs `model_api/strict` 两套策略 348–478） | 开放决策，低优先 |

---

## 2. 目标架构

```
                 ┌─────────────────────────────────────┐
   user message  │   build_companion_context()         │   ← 唯一的 context 拼装
   + surface ───▶│   identity + memory(garden+identity  │      VPS / API / proactive 都调它
                 │   card) + screen(frame+OCR+image) +   │
                 │   recent_chat + pending_state         │
                 └───────────────┬─────────────────────┘
                                 │ 同一份 context
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
       VPS agent (Cloud Code/Codex)      API agent runtime（新）
       通过工具拿 / 被预取                  复用同一批工具定义
       MCP / CLI / HTTP 暴露能力           不再手搓 JSON 协议
```

两条原则：
1. **一个 context 拼装函数**，VPS / API / proactive 全部复用。"调一个 function，不要两个。"
2. **感知能力打包成正经工具**（MCP / CLI / HTTP），VPS agent 用工具；API 用户做成 runtime 后复用同一批工具。

---

## 3. 任务分解（按口头结论的顺序：**VPS 优先**）

### Phase A — 把 VPS 这条线修通修对（先做）

**A1. 抽出统一 context 拼装函数** ⭐ 最高杠杆
- 把 `app.py:8421` `_model_api_context_messages` 里拼 `{identity, context_memories, screen_context, screen_images, pending_state_updates}` 的逻辑，抽成
  `build_companion_context(store, api_key, user_message, *, surface)`。
- `surface ∈ {"vps", "api", "proactive"}`，差异只体现在最后的"投喂方式"，**拼装内容一致**。
- model_api 路由 (`app.py:10291`) 和 MCP / proactive 路径都改成调这个函数。
- 验收：删掉两套拼装里重复的字段组装；同一个 user message 在两条线得到等价 context。

**A2. screen 变成默认 context，而不是关键词 pull**（P2）
- `app.py:8410` `_model_api_should_attach_screen`：默认带最近 frame（让 agent 自己决定忽略），
  不要再靠 "屏幕/screenshot" 关键词才给。产品卖点就是感知屏幕，藏起来是反的。
- VPS：`feedling_chat_get_history` 返回的 context 里，或唤醒预取里，带上最近屏幕（frame OCR + image）。
- 验收：VPS agent 在一次正常回合里能"看到"当前屏幕，不需要用户显式说"看屏幕"。

**A3. 主动唤醒预取 screen + memory**（P3 + P4）
- `app.py:1718` `_build_proactive_v2_wake_decision`：把 `frame_ids` 真正解密成 frame，
  并调 A1 的函数把相关 memory + identity card 填进去；
  把 `gate_input.memory_context`（1845–1852 现在全 False/0）真实填上。
- 验收：唤醒决策里 `decrypt_ok=True`、`memory_count>0`、`identity_loaded=True`，
  VPS consumer 唤醒时能直接拿到屏幕和记忆。

**A4. 屏幕蒸馏成 memory**（P7）— 浩轩（你在改 memory，顺手加）
- `prompts.py:75` `build_memory_capture_messages` + `app.py:9444` `_model_api_run_memory_capture`：
  额外吃最近 frame 的 OCR / app context，让"今天在购物 + 三天前也纠结过"这种连接能形成。
- frame→card 的提炼放 `semantic_analysis.py`。
- 验收：跑一个 test case——用户三天前和闺蜜聊过 shopping、今天又看 shopping，能成功 trigger 相关记忆。

### Phase B — 把 API 用户做成 runtime（VPS 修好之后）

**B1. 用真 runtime 替换手搓 JSON**（P5）
- `hosted_runtime.py` 的 JSON 契约改成真正的 tool-calling agent loop，
  **复用 MCP server 已经暴露的同一批工具定义**（screen / memory / identity）。
- 目标：API 用户 == VPS 用户，只是 agent loop 由我们的 runtime 提供。

**B2. 删掉手搓 web search**（P6）
- 删 `model_api_runtime/tools.py:122` `web_search_duckduckgo`，换 provider 自带的 web search / tool use。
- 任何 agent runtime 都自带这个，没必要自己爬 DuckDuckGo。

### Phase C — backlog（可并行，低风险）

- **C1**（P8）：`store.frames_meta` 落 PG JSONB / 对象存储（Cloudflare，key 应该有）。
- **C2**（P9）：决定 `context_memory_selection.py` 是继续自研还是直接 vendor Memory Palace 包；
  建立我们自己情感场景的 benchmark（Memory Palace 自带 benchmark 用户群不一样）。

---

## 4. 为什么是这个顺序

VPS 已经有真实 agent loop（Cloud Code / Codex），是真实目标用户。
先把这一条线弄干净，API 用户随后"做成 runtime 直接复用"即可，而不是永远维护两套。
反过来（先做 API 再让 VPS 抄 API）是反的——口头讨论里已经纠正过一次。

---

## 5. 测试 / onboarding 注意

- 不要每写一步就测一次——架构刚大改完，先把架构调到自己觉得对的状态，再进细节测试。
- 测试不必都在手机上跑：把后端那段拎出来，专门搞一个**测试用记忆库**，独立跑流程。
- onboarding 真实环境以 **Cloud Code / Codex 在 VPS 上**为准（小红书 "人机恋 cloud code setup" 教程那套），
  复制真实环境再测；Hermes / OpenClaw 当前可跳过。
- 核心验收问题始终是那一个：**VPS 用户把自己的 agent 搬进 IO 之后，加了感知能力，聊起来会不会怪？**

---

## 6. 一句话给每个人

- **浩轩**：A1（统一拼装）+ A4（屏幕蒸馏成 memory，和你正在改的 memory 一起）。
- **志豪**：A2 / A3（screen 进 context + 唤醒预取）+ 真实环境 onboarding 测试。
- **Phase B / C**：VPS 修好后再排期。
