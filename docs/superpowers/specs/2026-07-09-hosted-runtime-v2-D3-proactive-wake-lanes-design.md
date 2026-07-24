# Hosted Runtime V2 — D3 Proactive/Wake Lanes 设计

> 子项目 D 的大迁移。来源：walkthrough §6（lane 模型）+ §8 gate 5 + 现状调查（2026-07-09）。**依赖 D0 池在跑。** 把 proactive/wake/capture 从 resident 常驻计时器搬到 `agent_jobs` lane，兑现"关 resident"的前置（proactive 不迁走，resident 就停不掉）。

**Goal:** 让 heartbeat/scheduled/manual_wake/capture 唤醒变成 `agent_jobs` 行、由 V2 worker 池处理，enqueue 前保住 activation gate + wake_interval + wake 决策（zero pre-activation burn），并给 chat 预留 slot 使 wake 风暴不能饿死回复。

**Architecture:** 三个新部件 + 一处核心改动：(1) **调度器**（serve_worker 内单选主循环）扫 db_action_v2 用户、算 due、跑 wake 决策 gate、只把允许的唤醒 enqueue 成对应 lane 的 job；(2) **worker lane 处理器** 给 wake lane 装 prompt 组装 + "无回复≠失败"完成语义；(3) **claim slot 预留** 让 R 个 reserved slot 只领 chat/manual_wake；(4) 落地 due-time 的新表 `v2_wake_schedule`。5 个已知地雷全程保住。

## Global Constraints

- **NO-COMMIT** / **worktree**（同 D0）。
- **依赖方向**：调度器/worker 处理器住 `backend/model_api_runtime/v2/*`，不得 import `hosted`/`agent_runtime`；enclave/hosted 访问经 `TurnDeps` 或装配层注入。wake 决策 gate 逻辑（现 `backend/proactive/gate.py`）**纯函数部分**可被 v2 import 或复制——实现时核依赖方向测试。
- **zero pre-activation burn**（硬不变量）：未激活（`first_chat_ok_at` 未设）+ 非 manual 唤醒 → **绝不 enqueue**（gate 在 enqueue 前，不在 worker 里）。弱唤醒 = 不入队 = 零 model 调用。
- **BYOK-only** + **no-filler**（wake 回复也只有 model-authored 才写气泡；"无话可说"= 静默完成，不写气泡不弹 error chip）。
- **5 地雷保住**（见下"必须保住"）。
- **测试基线**：同 D0。

## 现状（调查确证）

- **proactive 100% resident**：触发在 `tools/chat_resident_consumer.py` 的 `while _running` 循环按 `time.monotonic()` deadline 发多 lane：heartbeat tick（`:6941` POST `/v1/proactive/tick`，默认 broadcast-on 300s / broadcast-off 7200s / per-user `wake_interval_sec` clamp [900,43200] 默认 2h）、scheduled fire（`:6911` 60s POST `/scheduled/fire`）、capture tick（`:6888` POST `/capture/tick`）。
- **wake 决策**在 `backend/proactive/gate.py:110` `_build_proactive_v2_wake_decision`（broadcast off/paused、no-frame heartbeat 抑制、activation gate `:185-208`）；决策落 `proactive_jobs` 流（`gate.py:276`），resident 长轮询领取（`:3728`）经 `_process_proactive_jobs`（`:5826`）用**和 chat 同一个** `call_agent`（`:3361`）执行。
- **状态**在 `user_logs` 流（`proactive_wakes_v2`/`proactive_turns_v2`/…，`store_v2.py:34`）+ `user_blobs`（leases `store_v2.py:244`；settings `wake_interval_sec`/`first_chat_ok_at` `:372`）；scheduled 计时器在 `proactive_scheduled_wakes_v2`（`scheduled_wake_v2.py:33`）。**无全局 `next_wake_at` 列**——调度隐含在每个 resident monotonic 时钟（`next_proactive_tick_mono` `:6859`）。
- **V2 lane 是空脚手架**：`LANES={chat,manual_wake,heartbeat,scheduled,capture,maintenance}`（`jobs_store.py:16`）但只有 chat/maintenance 有生产者；`worker.process_job` 只分 maintenance vs 其余当 chat（`worker.py:270`）；`claim_next_job` lane-agnostic（`jobs_store.py:88`）。

## 设计

### D3.1 — due-time 真相源：`v2_wake_schedule` 表

新表（Alembic 0018）：
```
v2_wake_schedule(
  user_id TEXT PK REFERENCES users ON DELETE CASCADE,
  next_heartbeat_at TIMESTAMPTZ,
  next_capture_at   TIMESTAMPTZ,
  payment_cooldown_until TIMESTAMPTZ,   -- BYOK 402 熔断（地雷2）
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
```
调度器**拥有**此表：enqueue 一个 heartbeat 后把 `next_heartbeat_at = now() + wake_interval`（由 gate 决策的 broadcast_state/wake_interval_sec 决定）。scheduled 计时器另读 `proactive_scheduled_wakes_v2`（复用现有流，不搬）。

### D3.2 — 调度器（serve_worker 内单选主）

新 `backend/model_api_runtime/v2/scheduler.py` + serve_worker 起一个调度循环（复用 multiworker 的 advisory-lock 选主，保证多进程只一个调度器；见 memory `multiworker-shipped-not-deployed`）。每 tick（如 30s）：

1. 取活跃 db_action_v2 用户集（复用 D0 的发现口径反选：mode=db_action_v2）。
2. 对每用户：读 `v2_wake_schedule` + settings；跳过 `payment_cooldown_until > now()` 的（地雷2）。
3. **heartbeat**：若 `next_heartbeat_at <= now()`，跑 wake 决策 gate（activation gate + broadcast/frame 抑制）。允许→`enqueue_job(user, "heartbeat", ...)` + 更新 `next_heartbeat_at`；弱唤醒→**只更新 next_heartbeat_at，不 enqueue**（zero burn）。
4. **scheduled**：`proactive_scheduled_wakes_v2` 里 due 的计时器→`enqueue_job(user, "scheduled", ...)`，标 fired（复用现有 timer 状态机，地雷4 的 round-trip 保住）。
5. **capture**：capture cadence due→`enqueue_job(user, "capture", ...)` + 更新 `next_capture_at`。
6. single-flight（`enqueue_job` 的唯一索引）天然防重复唤醒——不必自造。

wake 决策 gate 逻辑：把 `gate.py:110` 的**纯判定部分**（输入 settings/broadcast_state/frame 存在性/activation → 输出 allow/block+interval）提成可被 v2 复用的纯函数（或 v2 侧复制，核依赖方向）。gate 现有的 `proactive_jobs` 流写入在迁移后由 job 行取代。

### D3.3 — worker lane 处理器

`worker.process_job` 在 maintenance 分支旁加 wake 分支（`lane in {heartbeat,scheduled,manual_wake,capture}`）：

- 组装 wake prompt：port `_message_for_proactive_job`（`chat_resident_consumer.py:4644`）+ perception digest；**保住** `_clean_messages_for_proactive_context` 的 role 过滤（地雷5）。
- 走 `responder.respond`（BYOK、单次解密、ENCLAVE_SEMAPHORE，与 chat 同）；有 model-authored 回复→写气泡；**无话可说→`mark_completed` 静默**（no-filler，不写气泡、不弹 error chip）。
- **完成语义**："无 user message / 无回复"**不是失败**——记 `completed`（地雷1：喂 lane 级成功计量，woke+decided 即成功）。
- capture lane：走 capture 能力（`capabilities` 里的 memory capture facade），非普通回复。

### D3.4 — claim slot 预留（核心改动）

`claim_next_job` 加可选 `lanes: set[str] | None`：

```python
def claim_next_job(worker_id, *, lanes: set[str] | None = None) -> dict | None:
    # lanes 非 None 时，WHERE ... AND lane = ANY(%s)
```

serve_worker 起 N 个 slot：**R 个 reserved slot** 传 `lanes={"chat","manual_wake"}`（只领前台/手动唤醒）；**N−R 个通用 slot** 传 `lanes=None`（领全部，`ORDER BY priority DESC` 天然先领 chat 再 heartbeat 再 capture）。R = `FEEDLING_V2_CHAT_RESERVED_SLOTS`，默认 `max(1, MAX_WORKERS // 2)`（walkthrough 的"≥8"是 16-worker 池的比例）。

- **优先级**：enqueue 时给 lane 定 priority——chat/manual_wake 高（如 100）、heartbeat/scheduled 中（如 50）、capture 低（如 10）。maintenance 已有（compaction）保持低。→ 通用 slot 天然先前台后台、capture 先被 shed。
- 效果：wake 风暴填满 N−R 通用 slot，但 R 个 reserved slot 永远给 chat/manual_wake 留门（walkthrough：wake storm can never starve a reply）。

### 必须保住（5 地雷）

1. **lane 级成功计量**：daily-report 指标只算 wake lane 的 `completed`=成功（`docs/CHANGELOG.md:154`）。wake job 完成即成功，别把"没发消息"记失败。
2. **BYOK 402 熔断**：`PROVIDER_PAYMENT_COOLDOWN_SEC=600`——wake 碰 402/余额不足→写 `v2_wake_schedule.payment_cooldown_until = now()+600s`，调度器跳过冷却中用户，别 retry-storm 死钥。
3. **activation gate 在 enqueue 前**：`first_chat_ok_at` 未设 + 非 manual → `activation_pending`，不 enqueue（`gate.py:185`）。
4. **scheduled 计时器脆弱**：schedule→pending→fire→fired、cancel→canceled round-trip 校验（`docs/CHANGELOG.md:437`）。复用 `proactive_scheduled_wakes_v2` 状态机，别另造。
5. **prompt context role 过滤**：系统通知（"⚠️ 余额不足"）不能泄进 proactive context 被当内容（`docs/CHANGELOG.md:145`）。port `_clean_messages_for_proactive_context`。

## 已定（2026-07-09 用户拍板）

- **reserved slot 数 R** = `max(1, MAX_WORKERS//2)`（MAX_WORKERS 默认 4 → R=2）。✅
- **capture lane 本轮迁**（低优先、先被 shed）。✅
- **调度器 tick 间隔** = 30s。✅
- **manual_wake 生产者**：T9 先 grep 定位现状入口（"现在跟我说话"），走 chat/send 类则按 mode 分流，独立端点则加 enqueue 点。manual wake 绕过 activation gate。

## 落地文件（汇总）

- `backend/alembic/versions/0018_v2_wake_schedule.py`：新表。
- `backend/model_api_runtime/v2/scheduler.py`（新）：调度循环 + wake 决策 gate 纯函数复用。
- `backend/model_api_runtime/v2/jobs_store.py`：`claim_next_job` 加 `lanes`；wake_schedule 读写；lane priority 常量。
- `backend/model_api_runtime/v2/worker.py`：wake lane 分支 + prompt 组装 port + 无回复静默完成。
- `backend/model_api_runtime/v2/serve_worker.py`：起调度器（单选主）+ reserved/通用 slot 分配。
- wake 决策 gate 纯函数：从 `backend/proactive/gate.py` 提取或 v2 侧复制。
- 测试：`test_v2_scheduler.py`（due 计算/gate/zero-burn/cooldown 跳过）、`test_v2_wake_schedule.py`（表读写）、`test_v2_claim_reservation.py`（reserved slot 只领 chat/manual_wake）、`test_v2_wake_worker.py`（wake prompt 组装/无回复静默完成/role 过滤）、lane priority 测试。

## 自查

- placeholder：无 TBD（4 个决策显式列为"待拍板"，非遗漏）。
- 一致性：lane 名与 `jobs_store.LANES` 一致；zero-burn（gate 在 enqueue 前）贯穿调度器设计与硬不变量；reserved slot 与 claim `lanes` 参数一致。
- scope：单一子系统（proactive 迁移），但体量大（~8-10 task），plan 会拆细。
- 歧义：`next_wake_at` 存新表非 runtime_state——显式选定（调度器独占所有权）。
