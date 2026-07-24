# Hosted Runtime V2 — Agent Loop 设计

> **STATUS: HISTORICAL / SUPERSEDED.** This intermediate JSON-planner loop was
> replaced by the provider-native `tool_loop.py` architecture. Keep this file as
> decision archaeology; do not use `agent_loop.py`, planner tiers, or a separate
> responder as current implementation guidance.

> 承接 `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md` §C（turn shape，最深的一条 gap）。这是 walkthrough §9 条件3 的**重新定义版**——当初判定为"陪伴产品过度设计"而弃做的是**原生 tool-calling wire 协议**，不是 agent loop 本身。两者不是一回事。

**Goal:** 让 V2 有一个真正的 agent loop（`decide → act → observe → decide`），且**不把 provider 原生工具协议当作前置条件**。

**Core claim:** **N 轮 `plan → execute` 与 agent loop 控制流同构。** 差别只在 "decide 由谁做"。把 decide 做成**可插拔后端**，JSON-planner 是通用默认，provider 原生 tool-calling 是一等 provider 上的可选增强。

---

## 1. 为什么循环不放进 executor

`executor` 是**无状态批量调度器**：拿一批 action，并行读 / 串行写，返回结果。它不认识模型、不持有 BYOK key、不拼 wire。

把循环塞进去，等于逼它拿用户的 key、懂 provider 协议、持有 messages 数组——正好把 V2 花力气拆开的**「决定」与「执行」重新焊死**。

**结论：循环是编排，住在 executor 之上。在 (a) 和 (b) 两种后端下，`executor.py` 一行都不用改。** 这条性质本身就是"放对了地方"的判据。

## 2. 也不放进 responder

`responder` 是**纯的**：无 DB、无 store、无副作用，只把 `(provider_config, summary, tail, action_results)` 变成一段文本。而工具里有**写操作**（`memory_write` / `identity_patch`）。让 responder 去驱动工具，它就不纯了，而且会由"写回复的模块"顺手改用户的记忆。

## 3. 架构：`v2/agent_loop.py`，纯状态机 + 两个注入回调

和 D3 的 `scheduler.py` 同一套路（纯逻辑 + 注入 deps，用 fake 全量单测），那套很好用。

```python
# backend/model_api_runtime/v2/agent_loop.py —— 纯：无 DB / 无 hosted / 无 provider import
@dataclass
class Decision:
    actions: list[dict]          # 本轮要跑的 [{type, payload}]
    wants_reply: bool = False    # plan 里有 final_response 哨兵 = 收手去回复（见 §13）
    final_text: str | None = None  # 原生后端在收手时自带回复；JSON-planner 恒为 None

async def run_turn(*, decide, run_tools, max_rounds: int) -> tuple[dict, str | None]:
    """decide(round_idx, prior_results) -> Decision
       run_tools(actions)              -> dict  (action_type -> [result,...])
    返回 (累积的 action_results, final_text|None)。
    停止条件：wants_reply / 撞 max_rounds / 无进展 / 无 actions。
    撞上限或无进展时**强制收口去回复**（chat lane 不存在「无回复正常完成」终态，见 §13）。"""
```

`worker.process_job` 负责接线：

```python
results, final_text = await agent_loop.run_turn(
    decide=_json_planner_decide(store, provider_config, ...),   # 默认后端
    run_tools=_executor_bridge(store, job_id, api_key, runtime_token, enclave_sem),
    max_rounds=_LOOP_MAX_ROUNDS,
)
reply = final_text or await v2_responder.respond(
    provider_config=provider_config, summary=summary, tail=tail, action_results=results)
```

- `_json_planner_decide` 包 `planner.plan(..., prior_action_results=...)`，永远 `final_text=None`。
- `_executor_bridge` 包 `executor.execute_plan(...)`（已有的并行读/串行写/ENCLAVE_SEMAPHORE 全部原样复用）。
- **`executor.py` / `responder.py` 不改。** `planner.plan` 只多吃一个 `prior_action_results`。

### 3.1 为什么 `final_text` 这个口子必须现在留

原生 tool-calling 后端里，**停止发工具的那个模型顺手就把回复写了**。若不接住它，就得丢掉这次生成再让 responder 重写一遍——白烧一次 token。所以 `Decision.final_text` 现在就要在协议里，尽管默认后端永远不填。

## 4. 为什么保留 planner / responder 的分工

有个诱人的简化：让一个模型循环输出 `{"actions":[...]}` 或 `{"reply":"..."}`。**不要。** 把散文塞进 JSON 字段，转义与长文本会明显掉质量——这正是当初拆开 planner（出结构）和 responder（写自由文本）的原因。

**循环的是 planner；收尾写字的是 responder。**

## 5. decide 后端（可插拔）

| 后端 | 状态 | 说明 |
|---|---|---|
| **`json_planner`**（默认） | 本轮实现 | 纯文本补全，**任何 provider 都吃**，包括不支持 `tools=` 的中转站。已有防御解析 + 规则回退。 |
| **`native_tools`** | 本轮**只留 seam，不实现** | `provider_client` 加 `tools=` / 收 `tool_calls` / 吃 `tool_result`。需要：per-provider wire 适配（anthropic `tool_use` ↔ openai `tool_calls`）、每个 capability 的 JSON schema、**prompt caching**。探测不支持 → 自动降级 `json_planner`。 |

**为什么原生不是前置条件**：用户自带 key，其中大量是中转站。中转站对 `tools=` 的支持极不可靠（前科：xAI 因 codex 塞了个 `type:namespace` 工具**422 拒掉整个请求**）。而多轮原生每轮重发整段 conversation → **tokens/turn 爆**，而那正是 walkthrough 自己定的回滚条件；压住它需要 prompt caching，弱中转多半也不支持。

反过来，JSON-planner 的 prompt 是**紧凑的、目的构造的**（用户消息 + digest + 上轮结果），不是整段对话——**多轮 plan→execute 在 token 上很可能比无缓存的原生循环还便宜。**

## 6. 护栏（单轮不需要、多轮必须有）

| 护栏 | 规则 |
|---|---|
| 轮数上限 | `_LOOP_MAX_ROUNDS`（默认 **3**）。撞上限 → 停止取工具，交给 responder 用手上的结果收口。 |
| **无进展检测** | 本轮 action 签名集合 == 上轮 → 停。或本轮全部结果 `ok=False` / 空 `data` → 停。**防止空转烧用户的 key。** |
| 每回合 LLM 调用上限 | 见下方"调用次数会相乘"。硬上限 `_TURN_MAX_LLM_CALLS`。 |
| tokens/turn | D4 的回滚门依然适用。循环必然抬高均值，**上线前必须先量出单轮基线**。 |

### 6.1 调用次数会相乘 —— 这是最大的落地风险

现有的 replan 是**消息驱动**的外层循环（`worker.py:347-358`，安全点发现新用户消息才重规划，`replan_budget` 默认 ≤2）。新的工具循环是**模型驱动**的内层循环。

```
LLM 调用数 ≤ replan_budget × max_rounds + 1(responder)
          ≤        2      ×     3      + 1  = 7
```

**必须显式设一个 `_TURN_MAX_LLM_CALLS` 硬闸跨两层计数**，否则一个话痨用户 + 一个爱查东西的 planner 能把 BYOK key 烧穿。内外两层的语义不同（外层"用户又说话了"，内层"我还想再查"），**不要合并**，但要共用一个预算。

## 7. 与 BUG-1 的强耦合：先修图片，或先摘掉 `chat_image_read`

矩阵 §E BUG-1：`chat_image_read` 返回 `image_b64`，`_fold_action_results` 原样吃、`_action_context_str` 做 `json.dumps(...)[:8000]` 硬截断 → 模型收到 8000 字符的半截 base64，把记忆卡/感知全挤掉。

**循环会让它更糟**（planner 有更多机会选它）。所以本轮必须二选一：

- **(推荐)** 先做多模态：`provider_client` 构造真正的 image content block，`chat_image_read` 的字节走**带外**通道进 provider，不进文本 context；或
- **(止血)** 在多模态落地前，把 `chat_image_read` 从 planner 词表里摘掉。

顺带把 `_fold_action_results` 改成**按 capability 白名单取字段**，而不是 `data` 原样吞——今天任何返回大 blob 的 capability 都能毒掉 context。

## 8. 诚实的损失（JSON-planner 后端相对原生）

1. **工具参数构造质量**：planner 从紧凑 prompt 里写参数，原生模型在完整对话视野下写。差距真实，难量化。
2. **写回复的模型看不到原始工具结果**：responder 拿到的是截断的 JSON blob。**可缓解**（把 `action_context` 从"截断 json.dumps"升成"本回合结构化的工具轮次记录"），但不能完全消除。
3. **没有约束解码**：JSON 靠模型自觉，多轮把脆弱性乘以轮数。弱模型退化成规则 planner 时，规则不会从结果里学东西——**那些用户实际上仍是 1 轮**。这点必须承认，不能假装循环对所有人生效。

## 9. 不变量（不得因本设计放松）

- **BYOK-only**：planner 每轮、responder 一次，全部用该用户 JIT 解密的 key。无平台 key 兜底。
- **单次解密**：`provider_config` 由 `_run_turn` 解一次，贯穿整个循环。
- **ENCLAVE_SEMAPHORE**：capability 调用仍由 executor 统一过闸；循环不新增 enclave 并发。
- **no-filler**：只有 model-authored 文本能写气泡。撞上限/无进展 **不写占位气泡**——交给 responder 用现有结果正常作答。
- **依赖方向**：`agent_loop.py` 纯，不 import `hosted`/`agent_runtime`/`provider_client`。

## 10. 落地文件

- 新 `backend/model_api_runtime/v2/agent_loop.py`（纯状态机 + `Decision`）
- `planner.py`：`plan(..., prior_action_results=None)`，折进 prompt
- `worker.py`：`_json_planner_decide` / `_executor_bridge` 两个 bridge + 接线 + `_TURN_MAX_LLM_CALLS` 跨层预算
- `responder.py`：`_fold_action_results` 改白名单取字段（修 BUG-1 的一半）
- **不改**：`executor.py`、`capabilities/*`
- 测试：`test_v2_agent_loop.py`（纯，fake decide/run_tools：多轮累积、撞上限收口、无进展停、`final_text` 短路 responder）+ worker 集成（跨层预算、BYOK 单解密、no-filler）

## 11. 明确不在本轮范围

- `native_tools` 后端实现（只留 `Decision.final_text` + `decide` 可插拔这两个 seam）
- prompt caching（`cache_control`）——它是 `native_tools` 的前置，不是本轮的
- capability 的 JSON schema——同上

## 12. 已定决策（2026-07-10）

1. **`_LOOP_MAX_ROUNDS = 3`，`_TURN_MAX_LLM_CALLS = 6`。**
   注意 §6.1 的上界是 `replan_budget(2) × max_rounds(3) + 1 = 7 > 6`——**这不是笔误，硬闸就是要真的咬住**。撞到 6 时强制收口（见下条 BUG-4 处理）。若两个上限相等，硬闸永远不触发，等于没有。

2. **BUG-1 走「先止血」，不是 spec 原推荐的「先做多模态」。** 我推翻了自己的推荐，理由：
   - 止血（把 `chat_image_read` 从 `_READ_ACTIONS` + `_PLANNER_SYSTEM` 词表摘掉）之后，planner **根本发不出**这个 action，BUG-1 不可达——"循环会让它更糟"这个耦合当场消失。原推荐是为了避免带病上循环，止血同样达成，且**小得多**。
   - 多模态是 **per-provider wire 改动**（anthropic image block ≠ openai image_url ≠ 各家中转的支持度），有自己独立的风险面。把它和循环这个**控制流**改动捆在一轮，两者的回归会互相掩盖。
   - 终态相同，每轮爆炸半径更小。多模态紧接着独立立一轮，届时把 `chat_image_read` 加回词表。
   - 同时把 `_fold_action_results` 改成**丢 blob 键 + 每 action 独立字符上限**，作为纵深防御——即使将来某个 capability 又开始返回大 blob，也毒不掉 context。

3. **上线前必须先量出单轮 tokens/turn 基线。** 作为实现计划的 Task 2（在循环落地之前），用 D4 的 `scripts/loadtest/` + mock provider 量，结果落文件。否则 D4 runbook 的 `compare_tokens.py --resident-baseline` 回滚门没有参照物，循环上线后无法判断 token 均值抬高是预期内还是失控。

## 13. 循环顺带修掉 BUG-4（矩阵 §E）

写本文时未意识到：`validate_plan` **不强制** `final_response`，而 `worker.py:458` 把"plan 里没有 final_response"直接当成"这回合不用回复"——于是可信模型漏写 `final_response` 时，用户消息被静默吞掉，零气泡。

**"planner 没要求回复" 和 "planner 想先多查点东西" 今天是同一个信号**，worker 猜了前者。循环里这个信号的正确含义恰恰是后者：

- plan 含 `final_response` → 停止取工具，交 responder。
- plan 不含 `final_response` 且轮数/预算未尽 → **再来一轮**（把上轮结果喂回 planner）。
- 撞 `_LOOP_MAX_ROUNDS` 或 `_TURN_MAX_LLM_CALLS` → **强制** `final_response`，用手上的结果收口。

所以 chat lane 在循环下**不存在"无回复正常完成"这个终态**，BUG-4 由构造消失，不需要单独补丁。这也让 §3 的停止条件从"decide 返回空 actions"改成**复用既有的 `final_response` 哨兵**——不新增协议。
