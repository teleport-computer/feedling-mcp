# D3 Proactive/Wake Lanes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`)。这是大迁移；port-类 task 的逐行代码在执行该 task 时读源 `file:line` 展开，本 plan 给 task 边界+接口+测试用例+不变量+port 指令。

**Goal:** heartbeat/scheduled/manual_wake/capture → `agent_jobs` 行，由 V2 池处理；enqueue 前保 activation/interval/wake 决策（zero pre-activation burn）；chat slot 预留防 wake 风暴饿死回复。

**Architecture:** 见 spec `…-D3-proactive-wake-lanes-design.md`。依赖 D0 池在跑。

## Global Constraints

- **NO-COMMIT**；**worktree** 只在 worktree，绝不碰主 checkout。
- **依赖方向**：scheduler/worker 处理器住 `v2/`，不 import `hosted`/`agent_runtime`；wake 决策 gate 纯函数被 v2 复用（提取或复制，跑 `test_v2_dependency_direction.py` 核）。
- **zero pre-activation burn**（硬）：未激活+非 manual → 绝不 enqueue。gate 在 enqueue 前。
- **no-filler**：wake 无话可说 → 静默 `mark_completed`，不写气泡、不弹 error chip。
- **5 地雷保住**：lane 级成功计量 / BYOK 402 熔断600s / activation gate 前置 / scheduled 计时器 round-trip / prompt role 过滤（见 spec）。
- 测试同 D0。lane 名 ∈ `jobs_store.LANES`。

---

### Task 1: `v2_wake_schedule` 表 + 读写

**Files:** Create `backend/alembic/versions/0018_v2_wake_schedule.py`（down_revision 接 D0 的 `0017_v2_turn_metrics`）；Modify `jobs_store.py`；Test `tests/test_v2_wake_schedule.py`。

**Interfaces:** `get_wake_schedule(user_id)->dict|None`；`upsert_wake_schedule(user_id,*,next_heartbeat_at=..,next_capture_at=..,payment_cooldown_until=..)->None`；`due_heartbeat_users(*,now=None,limit=..)->list[str]`（`next_heartbeat_at<=now 且 cooldown 未生效`）。

- [ ] TDD：建表（schema 见 spec D3.1）；读写幂等；due 查询按时间+cooldown 过滤；cooldown 中用户不 due。全码执行时展开。

---

### Task 2: claim slot 预留 + lane priority

**Files:** Modify `jobs_store.py`（`claim_next_job` 加 `lanes`；priority 常量）；Test `tests/test_v2_claim_reservation.py`。

**Interfaces:** `claim_next_job(worker_id, *, lanes: set[str] | None = None)`；`LANE_PRIORITY = {"chat":100,"manual_wake":100,"heartbeat":50,"scheduled":50,"capture":10,"maintenance":10}`；`enqueue_job` 默认 priority 按 lane 取（不破现有显式 priority 参）。

- [ ] TDD：`lanes={"chat","manual_wake"}` 的 claim **跳过** pending 的 heartbeat/capture 只领 chat/manual_wake；`lanes=None` 按 priority DESC 先领 chat 再 heartbeat 再 capture。SQL：`WHERE status='pending' AND (%(lanes)s IS NULL OR lane = ANY(%(lanes)s)) AND (deadline_at IS NULL OR deadline_at>now()) ORDER BY priority DESC, created_at`。

---

### Task 3: wake 决策 = 注入真 gate（装配层适配器）

> **执行时设计修正**：`gate._build_proactive_v2_wake_decision` 不是可复制的纯函数（重耦合 store：读 frames/device events/settings + `evaluate_wake_control_v2`）。**但它只读**（真正 enqueue 在 `proactive_core.py:252-257` 单独一步）。→ 复用它当只读决策 oracle，比复制纯函数好（0 漂移、5 地雷自动保住、v2 保纯）。不建 wake_gate.py。

**Files:** Modify `backend/model_api_runtime/v2/serve_worker.py`（装配层适配器）；Test `tests/test_v2_wake_decision_adapter.py`。

**Interfaces:** `_wake_decision_for_user(user_id: str) -> dict`（住 serve_worker=装配层，**可** import proactive/hosted/core）：
```python
def _wake_decision_for_user(user_id):
    store = core_store.get_store(user_id)
    payload = {"trigger": "heartbeat"}   # 心跳唤醒 payload
    d = gate._build_proactive_v2_wake_decision(store, payload)  # 只读
    return {"should_wake": bool(d.get("should_wake_agent")),
            "wake_interval_sec": int(d.get("wake_interval_sec") or 7200),
            "block_reason": str(d.get("reason") or "")}
```

- [ ] TDD（集成，真 store+真 gate）：seed 用户 + proactive settings。**未激活**（`first_chat_ok_at` 未设）→ `should_wake=False`、`block_reason="activation_pending"`（地雷3，zero pre-activation burn 自动成立）。**已激活 + broadcast on** → `should_wake=True`。用真 `gate._build_proactive_v2_wake_decision`，不 mock 决策本身（要验证真 gate 的 activation/broadcast 判定）。依赖方向：适配器在 serve_worker，纯 v2 core 不碰 proactive。

---

### Task 4: 调度器循环

**Files:** Create `backend/model_api_runtime/v2/scheduler.py`；Test `tests/test_v2_scheduler.py`。

**Interfaces:** `run_scheduler_tick(deps, *, now=None) -> dict`（纯，返回 enqueued 统计）。`deps` 注入（便于用 fake 测）：`due_users() -> list[str]`、`wake_decision(user_id) -> {"should_wake":bool,"wake_interval_sec":int,"block_reason":str}`（=T3 适配器）、`enqueue_heartbeat(user_id) -> None`、`advance_heartbeat(user_id, next_at_epoch) -> None`。

- [ ] TDD（关键，全 fake deps）：
  - due 用户 + `should_wake=True` → 调 `enqueue_heartbeat(uid)` **且** `advance_heartbeat(uid, now+wake_interval_sec)`。
  - **zero-burn**：`should_wake=False`（未激活/弱唤醒）→ **不调 enqueue_heartbeat**（断言 0 次）、仍 `advance_heartbeat`（推进下次，避免每 tick 重打）。
  - cooldown 用户不在 `due_users()` 返回里（T1 的 `due_heartbeat_users` 已排除，地雷2）——scheduler 不必再判。
  - scheduled/capture lane：本 task 只做 heartbeat；scheduled（`proactive_scheduled_wakes_v2` due→enqueue"scheduled"+标 fired，地雷4）与 capture 由 **T8b** 补（见下）或本 task 扩，实现者按 deps 模式加对应 `due_scheduled()`/`due_capture()` + enqueue。
  - single-flight 天然防重复（唯一索引，不自造）。

---

### Task 5: serve_worker 接线调度器 + slot 预留分配

**Files:** Modify `serve_worker.py`（起调度循环，advisory-lock 单选主复用 multiworker 选主；reserved/general slot 分配）。

**Interfaces:** N slot 中 R=`FEEDLING_V2_CHAT_RESERVED_SLOTS`（默认 `max(1,MAX_WORKERS//2)`）个传 `lanes={"chat","manual_wake"}`，其余 `lanes=None`。调度器循环与 heartbeat/reaper 循环并列，单选主。

- [ ] TDD/集成：单选主保证多进程只一个调度器 tick；reserved slot 数正确；调度循环起停干净。

---

### Task 6: worker wake-lane 处理器

**Files:** Modify `worker.py`（`process_job` 加 wake 分支）；Test `tests/test_v2_wake_worker.py`。

**Interfaces:** `process_job` 在 maintenance 分支旁加 `if lane in {"heartbeat","scheduled","manual_wake","capture"}: return await _run_wake(...)`。

- [ ] TDD：
  - wake prompt 组装：port `_message_for_proactive_job`（`chat_resident_consumer.py:4644`）+ perception digest；**role 过滤**保 `_clean_messages_for_proactive_context`（地雷5）。
  - 走 `responder.respond`（BYOK/单次解密/ENCLAVE_SEMAPHORE，同 chat）。
  - **无回复静默完成**：无 model-authored 回复 → `mark_completed`、不写气泡、**不** `_surface_terminal_error`（no-filler + 地雷1：completed=成功）。
  - capture lane → 走 capture 能力 facade（非普通回复）。
  - provider 解析失败（key 轮换/enclave 瞬时）→ **不弹用户 error chip**（同 D1 maintenance 门控：`lane in wake → 静默 mark_failed`）。

---

### Task 7: BYOK 402 熔断（地雷2）

**Files:** Modify `worker.py`/`scheduler.py`；Test `tests/test_v2_wake_payment_cooldown.py`。

- [ ] TDD：wake turn 碰 402/余额不足 → `upsert_wake_schedule(uid, payment_cooldown_until=now()+600)`；调度器下 tick 跳过该用户（断言不再 enqueue）；600s 后恢复。复用 `provider_client.classify_provider_error` 判 payment。

---

### Task 8: lane 级成功计量保住（地雷1）

**Files:** 核对 `admin/data_track.py` 的 `admin_data_track_proactive_daily` 口径；确保 wake job `completed`（含"没发消息"）计成功、`capture/heartbeat*` 常 0 lane 排除逻辑对新 job 行仍成立；Test `tests/test_v2_wake_success_accounting.py`。

- [ ] TDD：一批 wake job（部分 completed-无消息 / 部分 failed-真错）→ daily 口径成功率只算 wake lane completed，不被"没发消息"拉低（`docs/CHANGELOG.md:154`）。

---

### Task 9: manual_wake 生产者接线

**Files:** grep 定位手动唤醒入口（用户戳"现在跟我说话"），加 `enqueue_job(uid,"manual_wake")`；Test。

- [ ] **先 fact-find**：`grep -rn "manual_wake\|manual wake\|即时唤醒\|wake now" backend/ tools/` 定位现状入口。若走 chat/send 类路径则在该处按 mode 分流；若独立端点则加 enqueue 点。manual wake **绕过 activation gate**（gate T3：is_manual→always allow）。

---

## Self-Review

- spec 覆盖：due 表(T1)/预留(T2)/gate(T3)/调度器(T4)/接线(T5)/worker(T6)/熔断(T7)/计量(T8)/manual(T9)。5 地雷分别落在 T7/T8/T3/T1+T4/T6。
- 一致性：lane 名、`claim_next_job(lanes=)`、`LANE_PRIORITY`、`v2_wake_schedule` 字段跨 task 一致。
- 依赖方向：gate 纯函数(T3)、scheduler/worker 不 import hosted——每 task 跑 `test_v2_dependency_direction.py`。
- port-task（T3/T6）逐行码执行时读源展开——非占位，给了源 file:line + 必保不变量。
- **执行前置**：T9 需先 fact-find manual wake 入口；spec 4 决策（R 默认/capture 是否迁/tick 间隔/manual 路径）需用户 review 时定。
