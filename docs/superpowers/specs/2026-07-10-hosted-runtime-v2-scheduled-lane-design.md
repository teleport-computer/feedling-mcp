# Hosted Runtime V2 — `scheduled` lane + `schedule_wake` capability（含 BUG-2 安全修复）

> 承接 `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md` §B（`scheduled` = 有 handler 无生产者）、
> §A（`schedule_wake`/`cancel_wake` 不可发射且不被解读）、§E BUG-2 / BUG-3。

**Goal:** 让 agent 能给自己排定时唤醒，并且那个唤醒真的会触发。顺带堵死"未知 lane 掉进 chat 路径写气泡"。

---

## 1. 核实过的现状

| 事实 | 位置 |
|---|---|
| `scheduled` 在 `_WAKE_LANES` 里，handler 存在 | `worker.py:116` |
| **`backend/` 里没有任何一处 enqueue `scheduled`** | 生产者缺失 = BUG-3 |
| `schedule_wake`/`cancel_wake` 不在 planner 词表；executor 把它们当控制动作 **SKIP** | `executor.py:28-31` |
| `capture ∈ LANES`，但 `process_job` 只分发 `maintenance` 和 `_WAKE_LANES`，其余**掉进 chat 路径** | `jobs_store.py:16`；`worker.py:452/457` |
| 掉进 chat 路径后，`wants_reply = lane == "chat" or stop_reason == WANTS_REPLY` → planner 一旦要求回复就**写聊天气泡** | `worker.py:544` = BUG-2 |
| 机器早就在：`ScheduledWakeServiceV2.apply_turn_actions` / `fire_due_timers`，且 `submit_wake` **是注入回调** | `proactive/scheduled_wake_v2.py:521/`fire_due_timers`` |
| 它接受的 action 形状 = `{"type":"schedule_wake","at":...,"tz":...,"reason":...}` / `{"type":"cancel_wake","wake_id":...}` | 与 planner action 形状**完全一致** |
| 但 `proactive_core.scheduled_fire` 的 `submit_wake` 塞进 **legacy `proactive_jobs` 流**，V2 下无人排空 | `proactive_core.py` |
| 存储是 append-only `user_logs`（`stream='proactive_scheduled_wakes_v2'`，`item_key=timer_id`）；某 timer 的当前状态 = 该 item_key 的**最新 seq 行** | `db.log_append`；`scheduled_wake_v2.py:395-410` |
| `due_candidates` 只按**单用户**查，无跨用户到期查询 | `scheduled_wake_v2.py:266/375` |

**所以这一轮不是重写，是接线**：复用注入缝，把提交重定向到 `agent_jobs`，再补一个跨用户 due 查询。

## 2. 设计

### 2.1 BUG-2：未知 lane 绝不落进 chat 路径

`process_job` 显式分发。`maintenance` → 压缩；`_WAKE_LANES` → wake；`chat` → chat 回合；**其余一律静默
`mark_failed("unhandled_lane:<lane>")`，零气泡、零 error chip**（背景 job 的既有口径）。

这是**安全修复**，不是 capture 功能。capture lane 的生产者+处理器与 dream/screen_watch 同形（后台
prompt → 写记忆卡 → 不出气泡），合成下一轮做才 DRY。在那之前，`capture` 落到 unhandled 分支 = 明确失败，
而不是偷偷写气泡。

### 2.2 BUG-3a：跨用户到期查询

`jobs_store.due_scheduled_users(*, now=None, limit=500) -> list[str]`

```sql
SELECT DISTINCT user_id FROM (
  SELECT DISTINCT ON (user_id, item_key) user_id, doc
  FROM user_logs
  WHERE stream = 'proactive_scheduled_wakes_v2'
  ORDER BY user_id, item_key, seq DESC        -- 每个 timer 只看最新一版
) latest
WHERE COALESCE(NULLIF(doc->>'due_at','')::float8, 0) <= <now>
  AND (doc->>'status' = 'pending'
       OR (doc->>'status' = 'claimed'
           AND COALESCE(NULLIF(doc->>'claim_expires_at','')::float8, 0) <= <now>))
LIMIT <limit>
```

`DISTINCT ON` 是**必须的**：`user_logs` 是 append-only，一个 timer 会有 created→claimed→fired 多行。
不取最新一版就会把早已 fire 的 timer 当成 pending 反复唤醒。

### 2.3 BUG-3b：`scheduled` 生产者

scheduler tick 现在多做一件事：对 `due_scheduled_users()` 里的每个用户，调
`ScheduledWakeServiceV2.fire_due_timers(user_id, submit_wake=_submit)`，其中

```python
def _submit(event):
    jobs_store.enqueue_job(user_id, "scheduled", reason="scheduled_wake")
    core_wake_bus.notify("v2_jobs", user_id)
    return WakeControlDecisionV2(True, "queued_v2", settings)
```

`fire_due_timers` 自带 claim / mark_fired / mark_blocked 的原子性（`claim_due` 的 CAS SQL），所以
**多个 scheduler 实例并发跑也不会重复触发**。handler 已经存在（`_run_wake`），一行不改。

零漂移：gate、时区解析、pending 上限、claim TTL 全部原样复用。

### 2.4 `schedule_wake` / `cancel_wake` capability

新 `backend/capabilities/wake.py`，薄 facade 包 `apply_turn_actions`。注册进 registry，加入 planner
词表的 **写动作**（`_WRITE_ACTIONS`，executor 串行执行）。executor 的 `_CONTROL_ACTIONS` 里把这两个
名字删掉——它们不再是"别人解读的控制动作"，而是真正会跑的 capability。

**关键决定：capability 的 `submit_wake` 不入队。**

`apply_turn_actions` 在请求时间已过/立即到期时会调 `submit_wake`。V2 里让它**不提交**，只返回
accepted —— timer 已经持久化了，下一次 scheduler tick（≤30s）会通过 `fire_due_timers` 正常捞起来。

理由是**分层**：`capabilities/*` 不能 import `model_api_runtime.v2.jobs_store`（v2 是 capabilities
的上层，反向 import 会成环，且 AST 守卫盯着这条方向）。让 scheduler 做唯一的入队者，也顺带保证了
"只有一个地方产生 scheduled job"。代价是最坏 30s 延迟 —— 对"稍后叫我"这个语义完全无所谓。

## 3. 不变量

- **BYOK-only / 单次解密 / ENCLAVE_SEMAPHORE**：不变。本轮不新增 LLM 调用、不新增 enclave 往返。
- **no-filler**：`scheduled` 走既有 `_run_wake`，弱唤醒静默 sleep、真失败静默 mark_failed。unhandled lane 同样静默。
- **零预激活消耗**：`fire_due_timers` 内部走 `settings` gate（`WakeControlDecisionV2`），未激活用户不会被唤醒。
- **依赖方向**：`capabilities/wake.py` 只 import `proactive.*`（不受限），**绝不** import `hosted` / `agent_runtime` / `model_api_runtime`。
- **幂等**：`enqueue_job` 的 single-flight 部分唯一索引 `(user_id, lane)` 保证同一用户同一 lane 只有一个在飞的 job。

## 4. 诚实的边界

1. capability 排定的 wake 最多晚 30s 触发（scheduler 间隔）。可接受，见 §2.4。
2. `due_scheduled_users` 全表扫 `user_logs` 里该 stream 的行。当前规模（用户数极小）无所谓；量大时需要
   `(stream, (doc->>'due_at'))` 上的部分索引。**本轮不加索引**，记在这里。
3. `capture` lane 从"偷偷写气泡"变成"明确失败"。这是改进，但仍不是功能 —— 下一轮才补。

## 5. 落地文件

- `backend/model_api_runtime/v2/worker.py`：显式 lane 分发 + unhandled 分支
- `backend/model_api_runtime/v2/jobs_store.py`：`due_scheduled_users`
- `backend/model_api_runtime/v2/scheduler.py`：tick 里新增 scheduled 触发（注入 deps）
- `backend/model_api_runtime/v2/serve_worker.py`：`_due_scheduled_users` / `_fire_scheduled_for_user` 装配
- `backend/capabilities/wake.py`（新）+ `capabilities/registry.py`
- `backend/model_api_runtime/v2/planner.py`：词表加 `schedule_wake`/`cancel_wake`（写动作）
- `backend/model_api_runtime/v2/executor.py`：从 `_CONTROL_ACTIONS` 移除这两个名字
- **不改**：`responder.py`、`provider_client.py`、`compaction.py`、`proactive/scheduled_wake_v2.py`

## 6. 不在本轮范围

- `capture` / `dream` / `screen_watch` 三条 lane 的生产者+处理器（同形，下一轮合并做）
- `user_logs` 上的 due 索引
- resident tokens/turn 基线（独立事项，卡住 D4 回滚闸）
