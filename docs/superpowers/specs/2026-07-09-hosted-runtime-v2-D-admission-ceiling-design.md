# Hosted Runtime V2 — §6 Admission Ceiling 设计

> 子项目 D 的第一片。来源：`~/downloads/feedling-runtime-v2-walkthrough.html`（2026-07-08，sxysun + Claude）§6 send 路径 admission ceiling + §9 merge 前置。承接 Step 1 的 V2 存活闸（`workers_alive`）。

**Goal:** send 路径在持久化任何东西之前，估算本条消息的排队等待时长；≤SLA 则放行（原逻辑），>SLA 则返回独立的 busy 响应且不落库。

**Architecture:** 一个纯函数算 est-wait（在飞 job 数 ÷ 活 worker 数 × 滚动均服务时长），三个纯读 jobs_store 查询喂它，chat_send_core 在存活闸之后 persist 之前调用；任何 DB 抖动 fail-open（放行）。

## Global Constraints（verbatim，每个 task 隐含包含）

- **NO-COMMIT**：全程不 `git commit`、不 `git add`。用户自己提交。
- **worktree**：只在 `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/hosted-runtime-v2` 工作，**绝不**碰主 checkout（`pre` 分支）。每个 subagent 硬性 worktree-cwd 约束 + 每 task 核 placement。
- **依赖方向**（AST 守卫 `tests/test_v2_dependency_direction.py`）：`backend/model_api_runtime/v2/*` 与 `backend/capabilities/*` **不得** import `hosted`/`agent_runtime`。admission 逻辑住 `v2/`，由 `chat_send_core`（assembly/hosted 层）调用注入，方向正确。
- **fail-open**：admission ceiling 自身绝不能成为故障源。任何计算/DB 异常 → admit（放行），与 wedge guard 的 fail-open 同原则。
- **busy 响应形态**：`503` + `error="busy"` / `reason="queue_over_sla"`（与现有 `workers_unavailable`(503) 同路子，iOS 零改动，靠 reason 区分）。**不**用 429。
- **测试基线**：本仓 7 个 pre-existing 失败（debug_trace×3 + debug_trace_event + memory_capture + verify-ping×2），与本工作无关。Docker PG 在 `127.0.0.1:55432`（容器 `feedling-test-pg`）。

## 现状

- `chat_send_core.py`：`db_action_v2` 分支（`_v2_mode`）在 `workers_alive()` 存活闸（返 503 `workers_unavailable`）之后，**无条件** `append_chat`（persist 加密用户消息）→ `enqueue_job` → `notify` → 202。**无任何 est-wait / SLA / 队深闸**；唯一背压是 `enqueue_job` 的单飞 coalesce。
- `agent_jobs` 列：`status`、`lane`、`priority`、`created_at`、`claimed_at`、`started_at`、`finished_at`、`deadline_at`。单飞唯一索引 `ux_agent_jobs_singleflight ON (user_id, lane) WHERE status IN ('pending','claimed','running')` → **每用户每 lane ≤1 在飞 job**。
- `v2_worker_heartbeats(worker_id PK, beat_at)`：每 serve_worker ~10s UPSERT。`workers_alive(*, within_sec=30)` 已有（bool）；本设计加 count 变体。
- `mark_running` 落 `started_at=now()`；`mark_completed`/`mark_failed`/`mark_expired` 落 `finished_at=now()`。→ 服务时长 = `finished_at − started_at` 纯读可得。

## 设计

### est-wait 算法（全纯读，无写路径改动）

```
W = 活 worker 数 = COUNT(v2_worker_heartbeats WHERE beat_at > now() - within_sec)   # 到这里已知 ≥1
Q = 在飞 job 数  = COUNT(agent_jobs WHERE status IN ('pending','claimed','running'))
S = 滚动均服务时长 = AVG(EXTRACT(EPOCH FROM finished_at - started_at))
                     over 最近 N=50 条 status='completed' 且 lane='chat' 的 job；
                     无历史（NULL）→ 用默认常量 S0
est_wait = ceil(Q / W) × S      # W 个 worker 排空 Q 个在飞 job，本条约等第 Q 位
admit iff est_wait ≤ SLA_SEC
```

设计取舍（YAGNI，当前 prod 用户量极小，此闸是安全阀、几乎不触发）：
- **不**建 EWMA 表、**不**在 mark_completed 里加写路径——`finished_at−started_at` 的滚动 AVG 已是 walkthrough 说的 rolling mean，纯读即可。
- **不**按 priority/lane 加权 Q——简单近似足够；真出现拥塞再细化，记为 follow-up。
- `W` 已被上游存活闸保证 ≥1；纯函数内仍防御 `W<=0 → 返回 0`（等价放行），不除零。

### 常量（模块级，env 可覆盖）

| 常量 | 默认 | env | 含义 |
|---|---|---|---|
| `SLA_SEC` | `60.0` | `V2_ADMISSION_SLA_SEC` | est-wait 上限，超则 busy |
| `DEFAULT_SERVICE_SEC` | `20.0` | `V2_ADMISSION_DEFAULT_SERVICE_SEC` | 无历史时的 S0 |
| `SERVICE_SAMPLE_N` | `50` | `V2_ADMISSION_SAMPLE_N` | 滚动 AVG 取样条数 |
| `HEARTBEAT_WINDOW_SEC` | `30` | （复用 workers_alive 语义） | 活 worker 判定窗口 |

### 响应形态

超 SLA：**在 append_chat 之前** 返回 `({"error": "busy", "reason": "queue_over_sla", "est_wait_sec": <int>}, 503)`，并发 debug_trace（`status="gated"`, `summary="admission_over_sla"`, detail 带 est_wait/inflight/workers）。与 `workers_unavailable`（供给死）语义严格区分。

fail-open：算 est-wait 的整段包 try/except，异常 → 落一条 debug_trace（`summary="admission_failopen"`）后继续放行，绝不因闸报错阻断用户。

## 落地文件

- **新 `backend/model_api_runtime/v2/admission.py`**（纯，无 DB/无 hosted import）：
  - `estimate_wait_sec(*, inflight: int, workers: int, mean_service_sec: float | None, default_service_sec: float) -> float`
  - `should_admit(est_wait_sec: float, *, sla_sec: float) -> bool`
  - 常量 `SLA_SEC` / `DEFAULT_SERVICE_SEC` / `SERVICE_SAMPLE_N`（读 env，带默认）
- **`backend/model_api_runtime/v2/jobs_store.py`** 加三个纯读查询：
  - `live_worker_count(*, within_sec: int = 30) -> int`
  - `inflight_job_count() -> int`
  - `recent_mean_service_sec(*, lane: str = "chat", limit: int = 50) -> float | None`
- **`backend/hosted/chat_send_core.py`**：存活闸之后加 admission 分支（try/except fail-open），超 SLA 走 busy 响应 + debug_trace。
- **测试**：
  - 新 `tests/test_v2_admission.py`——纯算法边界（除零 / 无历史用 S0 / 边界 est_wait==SLA 放行 / 超 SLA 拒）。
  - `tests/test_v2_jobs_store.py` 扩——三个新查询（有/无历史、窗口内外、在飞计数）。
  - chat_send_core 集成——超 SLA 返 503 `busy` 且 `append_chat` 零调用；fail-open（jobs_store 抛异常时仍 persist+202）。

## 自查（spec self-review）

- **placeholder**：无 TBD。所有常量、签名、返回类型、错误码给全。
- **一致性**：`should_admit(est_wait ≤ SLA)` 与 `admit iff est_wait ≤ SLA_SEC` 一致（边界 == 放行）；busy=503+reason 与 Global Constraints 一致。
- **scope**：单一子系统（send 背压），单一 plan 可覆盖。
- **歧义**：Q 计数不按 priority 加权——已在设计里显式选定并记 follow-up，非遗漏。
