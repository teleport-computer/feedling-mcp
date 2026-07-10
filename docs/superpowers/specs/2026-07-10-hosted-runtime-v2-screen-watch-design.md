# Hosted Runtime V2 — `screen_watch` lane

> parity matrix §B 最后一行、§F bucket 1 最后一项。

**Goal:** 用户在共享屏幕时，V2 能在屏幕内容真正变化、且用户不在打字时，主动开口。

**Core claim:** `screen_watch` 是 **wake 生产者**，不是记忆抽取。它归 `heartbeat`/`scheduled` 那一族。

---

## 1. 核实过的 resident 行为

`post_screen_watch_tick` 并不是独立端点 —— 它打的是 `/v1/proactive/tick`，payload 带
`job_kind=screen_watch` + `trigger=screen_watch` + **显式 frames**。所以它天然走既有 proactive gate
（Ambient 关 → 不唤醒），且**不是 forced/manual**。

resident 的 120s 循环（`chat_resident_consumer.py:7830-7860`）的完整 gate：

```
每 SCREEN_WATCH_INTERVAL_SEC=120s（下限 30s）：
  latest_fid, latest_ts = 最近一帧
  fresh   = latest_fid 且 (now - latest_ts) <= SCREEN_WATCH_FRESH_SEC(90)     # 共享真的在进行
  changed = latest_fid != last_screen_watch_frame_id                          # 只对新内容动作
  if fresh and changed:
      last_screen_watch_frame_id = latest_fid                                 # ← 进程内存
      chatting = last_user_message_age_sec < SCREEN_WATCH_CHAT_SUPPRESS_SEC(180)
      if not chatting: post_screen_watch_tick("on", frames)
```

回合本身是**轻量**的：`_screen_watch_message` 只带 frames + names-only 工具表，**不带**跨域看板；
`perception_digest = None`（`:6611`）。而且它**允许不说话** —— 绝大多数 tick 应该什么都不产出。

## 2. V2 的两个真难点

**(a) `last_screen_watch_frame_id` 在 resident 里是进程内存。** V2 没有 per-user 常驻进程，必须持久化，
否则每个 scheduler tick 都会把同一帧当成"新内容"，变成 120s 一次的唤醒风暴。

**(b) gating 必须廉价。** 它每 120s 对每个 `db_action_v2` 用户跑一次。所幸两个输入都**不需要 enclave 解密**：
- 最新帧 id/ts：`db.frame_list_meta(user_id)`（`screen/caption.py:134` 已在用），只有 id/ts/app。
- 用户上次说话时间：`store.chat_messages` 的 `role`/`ts` 是明文（密文只在 body_ct 里）。

## 3. 设计

### 3.1 状态落在 `v2_wake_schedule`（migration 0019）

```sql
ALTER TABLE v2_wake_schedule
  ADD COLUMN next_screen_watch_at TIMESTAMPTZ,
  ADD COLUMN last_screen_watch_frame_id TEXT;
```

`jobs_store.due_screen_watch_users(*, now, limit)` 镜像 `due_heartbeat_users`：到期且**不在 BYOK
支付冷却窗口内**（`payment_cooldown_until`）。冷却复用同一列 —— 一把坏 key 不该被屏幕轮询继续捶。

> 顺带记一笔：`v2_wake_schedule.next_capture_at` 在 jobs_store 里被读写、但**没有任何生产者用它**
> （capture 那轮复用了 `capture_scheduler` 自己的 gate）。它是一列已接线的死代码。本轮不动它，只记录。

### 3.2 gate 是纯函数

新 `backend/model_api_runtime/v2/screen_watch.py`（纯，stdlib）：

```python
FRESH_SEC = 90
CHAT_SUPPRESS_SEC = 180
INTERVAL_SEC = 120

def should_watch(*, latest_frame_id, latest_ts, last_frame_id, last_user_msg_ts, now) -> tuple[bool, str]:
    """(should, reason)。reason 恒非空，用于可观测性。"""
```

逐字移植 resident 的 fresh / changed / chatting 三条，顺序不变。纯函数 → 全量单测，零 I/O。

### 3.3 producer：scheduler 里再加一条 sweep

和 `scheduled`、`extraction` 两轮**完全同一套路**：`getattr(deps, "screen_watch_users", None)` /
`getattr(deps, "tick_screen_watch", None)`，两者缺一即跳过；per-user try/except 隔离；返回值新增
`screen_watch_enqueued`。

`serve_worker._tick_screen_watch_for_user(user_id) -> int`：

1. 读最新帧（`db.frame_list_meta`）与最后一条 user 消息 ts（`store.chat_messages`，零解密）。
2. 跑纯 `should_watch`。
3. **再过一遍 proactive gate**：复用已有的只读 oracle `_wake_decision_for_user(user_id)`。
   这一步是白送的 Ambient-off / 未激活 / 免打扰保护，也是**零预激活消耗**不变量的落点。
4. `should and should_wake` → `enqueue_job(uid, "screen_watch")` + `notify` + 持久化
   `last_screen_watch_frame_id`。
5. **无论如何都推进 `next_screen_watch_at`**（否则一个被 gate 挡住的用户每 tick 都被重新考虑）。

> 只有真的要唤醒时才更新 `last_screen_watch_frame_id`。被 chat 抑制掉的那一帧仍然是"未处理的新内容"，
> 用户停止打字后应该还能被看到 —— 这与 resident 有**一处刻意的差异**：resident 在 `fresh and changed`
> 时就写内存变量，即使随后被 chatting 抑制。resident 那样会**永久丢掉**那一帧。我们修掉它，并在此记录。

### 3.4 handler：wake 家族的一个轻量变体

`screen_watch` 加入 `_WAKE_LANES`，走既有 `_run_wake`，但：

- `system_prompt = _SCREEN_WATCH_SYSTEM_PROMPT`（明确告诉模型：你在看用户的屏幕；只有真有话说才开口）。
- tail 之外追加一段 **screen 上下文**：经 `screen_recent` capability 取近期帧（含 caption），折成文本。
  **不带**感知快照 —— 与 resident 的 `perception_digest = None` 对齐。
- **空回复 = 成功**（`empty_reply` → 静默 `mark_completed`，零气泡）。绝大多数 tick 走这条路。
  这条 `_run_wake` 已经有了（"weak wake sleeps"），直接继承。

## 4. 不变量

- **BYOK-only / 单次解密 / ENCLAVE_SEMAPHORE**：不变。gating 零 enclave；handler 的 screen 取数走既有闸。
- **no-filler**：只有 model-authored 文本能写气泡。被抑制/无话可说 → 零气泡、零 error chip。
- **零预激活消耗**：`_wake_decision_for_user` 在 enqueue 之前判定；未激活/Ambient-off 用户永不产生 job。
- **BYOK 支付冷却**：`due_screen_watch_users` 复用 `payment_cooldown_until`。
- **依赖方向**：`screen_watch.py` 纯（stdlib）。`serve_worker` 做装配。
- **single-flight**：`(user_id, lane)` 部分唯一索引保证同一用户同 lane 只有一个在飞的 job。

## 5. 诚实的边界

1. 服务端轮询把 resident 的 per-user 120s 循环变成了「scheduler 每 tick 扫一遍到期用户」。
   scheduler 间隔 30s，`next_screen_watch_at` 步进 120s → 实际抖动 ≤30s。无所谓。
2. `should_watch` 的 `changed` 依赖持久化的 frame id。**首次**（列为 NULL）视为 changed —— 与 resident
   进程刚启动时 `last_screen_watch_frame_id = ""` 的行为一致。
3. 与 resident 的一处刻意差异：被 chat 抑制的帧不消耗 `last_frame_id`（见 §3.3）。
4. 本轮**不**移植 resident 的 names-only 工具表裁剪 —— V2 的 wake 回合本来就不跑 planner/工具循环。

## 6. 落地文件

- `backend/alembic/versions/0019_v2_screen_watch.py`（新）
- `backend/model_api_runtime/v2/screen_watch.py`（新，纯 gate）
- `backend/model_api_runtime/v2/jobs_store.py`：`LANES`+`LANE_PRIORITY` 加 `screen_watch`；
  `due_screen_watch_users`；`upsert_wake_schedule` 支持两个新列
- `backend/model_api_runtime/v2/worker.py`：`_WAKE_LANES` 加 `screen_watch`；
  `_SCREEN_WATCH_SYSTEM_PROMPT`；`_run_wake` 接 screen 上下文
- `backend/model_api_runtime/v2/scheduler.py`：第三条 sweep
- `backend/model_api_runtime/v2/serve_worker.py`：`_tick_screen_watch_for_user` + 装配
- **不改**：`responder.py`、`provider_client.py`、`extraction.py`、`agent_loop.py`、`planner.py`、
  `executor.py`、`capabilities/*`、`proactive/*`、`tools/chat_resident_consumer.py`

## 7. 已知的连带修改

`tests/test_v2_worker.py::test_unhandled_lane_never_writes_a_bubble_and_fails_loudly_in_the_db`
上一轮用 `screen_watch` 当"未注册 lane"。本轮它被注册了 —— 必须换成另一个真正不存在的 lane
（例如 `"bogus_lane"`），**意图不变**。

## 8. 不在本轮范围

- resident tokens/turn 基线；§G Q2（LiteLLM 子进程）；`next_capture_at` 死列清理
