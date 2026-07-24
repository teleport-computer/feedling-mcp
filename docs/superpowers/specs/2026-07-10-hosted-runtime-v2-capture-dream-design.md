# Hosted Runtime V2 — `capture` + `dream` lanes（记忆抽取）

> **Correctness update (2026-07-18):** Capture actions now carry the real
> Garden validator metadata (`type`, `occurred_at`, source/ranking fields),
> Dream's `op/card_ids/result` output maps to multi-card supersede actions, and
> rejected writes fail the extraction job instead of being marked completed.

> 承接 `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md` §B（capture/dream = resident 有、V2 无）+ §E BUG-2
> （上一轮只做了安全修复：capture job 明确失败而不再偷偷写气泡）。

**Goal:** 让 V2 自己会「攒记忆」（capture）和「做梦整理记忆」（dream）。

---

## 1. 范围修正（推翻先前判断）

先前把 capture / dream / screen_watch 归为"同形，合并一轮"。**核实后只有前两条同形**：

| lane | 输入 | 输出 | 用户可见? | 形状 |
|---|---|---|---|---|
| `capture` | 一窗对话 | 记忆卡 → memory actions | ❌ | **抽取** |
| `dream` | 现有卡片 + 近期对话 | 合并/提问 → memory actions | ❌ | **抽取（同形）** |
| `screen_watch` | 屏幕帧轮询（120s）+ `on_mention` gating | 触发 agent 回合，**可能写聊天气泡** | ✅ | **wake 生产者** |

`screen_watch` 属于 `heartbeat`/`scheduled` 那一族，**不在本轮**，单独一轮和 wake lane 放一起。

## 2. 核实过的现状

| 事实 | 位置 |
|---|---|
| `build_capture_prompt` / `parse_capture_cards` 是**纯函数** | `memory/capture_prompt_v1.py:194/136` |
| `build_dream_prompt` / `parse_dream_consolidations` 是**纯函数** | `memory/dream_prompt_v1.py:72/119` |
| 两个 prompt 都吃**预渲染字符串**，缺失退化成「（暂无）」 | 同上 |
| 落库口 = `memory_core.actions(store, api_key, payload)`，`/v1/memory/actions` 和 `capabilities.memory.write` 共用它 | `memory/memory_core.py:164` |
| 上下文取数齐全：`memory_core.buckets/threads`、`identity_core.get_identity` | `memory_core.py:150/155` |
| 执行逻辑**只长在 resident 里**（build prompt → agent → parse → `execute_memory_actions` → POST `/v1/memory/actions`） | `chat_resident_consumer.py:6098-6200`、`:6434-6460` |
| **`dream` 不在 `LANES`** | `jobs_store.py:18` |
| `agent_jobs.lane` 是 `TEXT NOT NULL`，**无 CHECK 约束** → 加 lane **不需要迁移** | `alembic/0014:20` |
| `tick_quiet_capture` / `tick_memory_dream` **没有注入缝**，直接 `_enqueue_window` → legacy `proactive_jobs` 流 | `capture_scheduler.py:280`、`dream_scheduler.py:211` |
| 那条流在 V2 下**无人排空**（与 BUG-3 同一个死胡同） | — |

## 3. 设计

### 3.1 给 capture/dream 开一道和 `submit_wake` 一样的注入缝

**不抄 gate。** capture 的 gate 有五道早退（`capture_disabled` / `no_new_messages` / `already_captured` /
`quiet_not_due` / `min_interval` / `capture_already_pending` / 失败退避），dream 另有一套。抄一遍必然漂移。

改为给 `capture_scheduler.tick_quiet_capture` 和 `dream_scheduler.tick_memory_dream` 加一个
**可选 `submit=` 参数**，默认 `None` = 保持今天行为（append 到 `proactive_jobs`）。V2 的 scheduler 传入
一个把 job 塞进 `agent_jobs` 的 submitter。

这正是 `ScheduledWakeServiceV2.fire_due_timers(submit_wake=...)` 已经成立的模式；我们只是把它推广到另外两个
scheduler。resident 路径**一字不改**。

### 3.2 抽取是一个形状，不是两个

新纯模块 `backend/model_api_runtime/v2/extraction.py`：

```python
async def extract(*, provider_config, prompt: str, parse) -> tuple[Any, str | None]:
    """BYOK LLM 调用 + 解析。`parse` 是 memory/*_prompt_v1 里的纯解析函数。
    provider 错 / 解析错 → 返回 (None, reason)，绝不抛。"""
```

`worker._run_extraction(...)` 是 capture 和 dream 共用的 handler 骨架（镜像 `_run_compaction`）：
读上下文 → build prompt → `extract` → 转 actions → `apply_memory_actions` → `mark_completed`。
**自成一体的 try/except，绝不落进 chat 路径的 except**（那条会弹用户可见 error chip）。

`_run_capture` / `_run_dream` 只是往里注入不同的 `build_prompt` / `parse` / `to_actions`。

### 3.3 `cards → memory actions` 必须移出 resident

`_capture_actions_from_cards`（`chat_resident_consumer.py`）是纯逻辑，但住在一个 V2 不该 import 的文件里。
**移植成 `extraction.cards_to_actions(cards, *, occurred_at, source_ids)` 纯函数并加测试**，resident 保持不动
（它有自己的副本，两边都跑得通；等 resident 退役时删）。

> 这是本轮唯一的**代码复制**。刻意为之：让 resident 在 kill-resident 之前保持零风险。复制的是 ~40 行纯映射逻辑，
> 不是有状态的东西。plan 里会为它写独立单测。

### 3.4 空结果不是失败

- capture 解析出 0 张卡 → `mark_completed`，理由 `nothing_worth_keeping`。**不是 failed。**
- dream 解析出 0 条合并 → 同上。

这与 wake lane 的「弱唤醒睡回去」是同一条口径：模型选择什么都不做，是成功。

### 3.5 上下文取数失败 → 降级，不失败

buckets / threads / identity 任一取不到 → 传空串，prompt 自己退化成「（暂无）」。
**整个 job 继续。** 抽取质量下降，好过一次失败重试风暴。

## 4. 不变量

- **BYOK-only**：capture/dream 的 LLM 调用全部用该用户 JIT 解密的 key。无平台 key 兜底。
- **单次解密**：`provider_config` 由 `_run_turn` 解一次，贯穿本 job。
- **ENCLAVE_SEMAPHORE**：读对话窗口 / 卡片走既有闸；本轮不新增 enclave 并发。
- **no-filler**：capture/dream **永不写聊天气泡、永不弹 error chip**。失败静默 `mark_failed`。
- **依赖方向**：`extraction.py` 只 import `provider_client` + `memory.*_prompt_v1`（纯 prompt/parse），
  **绝不** import `hosted` / `agent_runtime`。落库经 `TurnDeps.apply_memory_actions` 注入。
- **零预激活消耗**：producer 仍走 `capture_scheduler` / `dream_scheduler` 自己的 gate。
- **single-flight**：`(user_id, lane)` 部分唯一索引天然保证一个用户同 lane 只有一个在飞的 job。

## 5. 诚实的边界

1. `_capture_actions_from_cards` 在 resident 和 V2 各有一份，直到 resident 退役。
2. capture 的 `pending_capture_key` / 失败退避状态仍存在 legacy capture state 里 —— 我们复用 gate，所以也复用它的状态。**不迁移。**
3. dream 的 `questions` 产出（`parse_dream_consolidations` 的第二个返回值）在 resident 里会变成主动提问。V2 本轮**只落 consolidations，丢弃 questions**，并记在这里 —— 提问属于 wake 语义，等 screen_watch/wake 那轮统一处理。

## 6. 落地文件

- `backend/model_api_runtime/v2/jobs_store.py`：`LANES` + `LANE_PRIORITY` 加 `dream`（无迁移）
- `backend/model_api_runtime/v2/extraction.py`（新，纯）：`extract` + `cards_to_actions` + `consolidations_to_actions`
- `backend/model_api_runtime/v2/worker.py`：`_EXTRACTION_LANES`、`_run_extraction`、`_run_capture`、`_run_dream`、dispatch；`TurnDeps.read_memory_context` / `apply_memory_actions`
- `backend/model_api_runtime/v2/serve_worker.py`：两个新 dep 的生产实现 + producer 接线
- `backend/proactive/capture_scheduler.py`、`backend/proactive/dream_scheduler.py`：各加一个可选 `submit=` 参数（默认行为不变）
- `backend/model_api_runtime/v2/scheduler.py`：tick 里加 capture/dream 扫描（同 `scheduled` 的 getattr 探测法）
- **不改**：`responder.py`、`provider_client.py`、`compaction.py`、`agent_loop.py`、`planner.py`、`executor.py`、`capabilities/*`、`chat_resident_consumer.py`

## 7. 不在本轮范围

- `screen_watch` lane（wake 形状，见 §1）
- dream 的 `questions` → 主动提问
- resident 侧 `_capture_actions_from_cards` 的去重（等 kill-resident）
- resident tokens/turn 基线；§G Q2（LiteLLM）
