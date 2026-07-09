# Hosted Runtime V2 (A+B+C) — DB Job + Action Queue + Short Planner 实现设计

Date: 2026-07-08
Branch: `feat/hosted-runtime-v2`（基于 `origin/test` @ e15325d）
Status: design approved — 待写实现 plan
Source: 精化自 `~/downloads/2026-07-04-hosted-runtime-v2-db-action-queue-design.md`（原路线图 Phase 0–7）

---

## 0. 一句话

把「每 API-key 用户一个常驻 CLI consumer 进程」换成「**独立 worker 进程池从 Postgres 抢 job**」：
合并用户消息 → 并行预取上下文 → planner 出 1–5 个短 action → executor 确定性排空 →
responder 写 **model-authored** 回复；tool-call 步骤经 status 事件实时推前台 app；
只持久化模型作者的 assistant 消息。本 spec 只覆盖 **A+B+C（前台，原文档 Phase 0–4）**。

---

## 1. 范围

### 本 spec 做（A+B+C）

- **A. capability 抽取层**：把散在 `tools/chat_resident_consumer.py`（consumer cli）与
  `tools/io_cli.py` 两处的能力实现，提炼成 canonical 独立单元 `backend/capabilities/`，
  io_cli / resident consumer / V2 worker 三方共用同一份实现。附 parity 矩阵文档。
- **B. DB job + 有界 worker 骨架**：4 张专用表、独立 worker 进程池、`SKIP LOCKED` claim、
  per-user single-flight、超时/重试/reaper、status 事件。
- **C. action queue + short planner/executor/responder**：`agent_action_queue`、planner 出
  1–5 JSON actions、executor 排空、replan/invalidation、多消息 coalesce、tool-call 步骤前台推送。

### 本 spec 不做（→ 子项目 D，另立 spec）

- proactive/wake/heartbeat/scheduled/capture lane 的对齐
- 100 用户压测（原 Phase 6）
- `preliminary_response`（额外模型调用的早期可见回复）
- 后台锁屏 Live Activity 推送
- 全量 rollout（原 Phase 7）——本 spec 只做 per-user 灰度开关，先内部/test 用户

> **交付节奏诚实说明**：A+B+C 单独上线**拿不到并发收益**。因为 proactive/wake 仍在
> resident 路径（D），只要用户还需 resident 进程处理 wake，就不能关他的常驻进程，
> 「idle 用户 = 0 RSS」这个头号卖点**要等 D 才兑现**。A+B+C 是地基管道，收益 gated on D。
> C 做完的压测数字不会好看，这是预期，不是回归。

---

## 2. 已锁定的架构决策

| # | 决策 | 选定 | 理由 |
|---|---|---|---|
| 1 | job/action 存储 | **全专用 Alembic 表** | claim 需 `FOR UPDATE SKIP LOCKED` + status/priority 索引 + single-flight 唯一索引；JSON 流上难干净地做 |
| 2 | worker 宿主 | **独立 worker 进程池** | 与 HTTP 并发解耦；同镜像兄弟入口，非独立 HTTP 服务/独立 repo |
| 3 | planner/responder 实现 | **纯 Python `provider_client`** | 复用 `reliable_chat_completion` + `driver_for_provider`；不 shell out、不引 Node |
| 4 | capability 层落点 | **新建顶层 `backend/capabilities/`** | 「抽成独立单元」的意图；依赖方向合法（在 perception/memory 之上、model_api_runtime 之下） |
| 5 | worker 形态 | **一回合一 worker**（同构池） | I/O-bound 异步；单进程多 job-slot + 多进程共抢队列即横向扩展，无需拆 pipeline |
| 6 | planner 用谁的 key | **用户自己的 BYOK key**（无平台级 LLM key 兜底）；弱模型退化**确定性规则 planner**（不发 LLM） | 保成本隔离 + proactive 不烧钱铁律；parity 压在 responder |
| 7 | tool-call 推送面 | **仅前台 in-app**（long-poll） | 低延迟、无 Apple 限频；不碰 Live Activity |

### 2.1 为什么 worker 不是「独立 FastAPI HTTP 服务」

worker 的输入是 **Postgres 队列（+LISTEN/NOTIFY）**，不是 HTTP，是 queue consumer 而非 server。
且 worker **必须 in-process 直调** `capabilities.*` / `provider_client` / `core.store` / enclave 解密
（原文档 §11）。若做成 HTTP 服务并回打后端拿能力，就**复刻 `backend→enclave→backend` 的
reentrant 跳**——正是当前线上 502 的根因（enclave 单线程瓶颈），还按 worker fan-out 放大。
独立 repo/镜像同理：要重建 TDX attestation / KMS 内容钥 / runtime-token 接线，代价大收益小。

**结论**：同一个 `backend/` 代码库、同一个镜像、新兄弟入口 `serve_worker.py`（类比
`asgi_app:app` / `enclave_app.py` / supervisor `main()` / `serve_dev.py`）；部署成自己的
replica set / 甚至自己的 CVM，跑在今天 resident consumer 那个 runner-CVM enclave 上下文里
（解密路径现成）。可选**极薄 FastAPI** 仅暴露 `/healthz` `/metrics`（部署平台存活探针用）。

---

## 3. 架构总览

```
┌─────────────┐  chat/send            ┌──────────────┐
│ iOS / app   │ ────────────────────▶ │ FastAPI web  │  落加密用户消息 + 入队/合并 job
│             │ ◀──── long-poll ───── │ (asgi_app)   │  快速返回, 不写 filler
│ 聊天气泡=模型 │  chat msgs + status   └──────┬───────┘
│ 状态条=runtime│                             │ waiters park / wake_bus LISTEN
└─────────────┘                             ▼
                                    ┌──────────────┐
                                    │  Postgres    │ agent_jobs / agent_action_queue
                                    │              │ agent_status_events / runtime_state
                                    └──────┬───────┘ chat_messages(加密)
                        SKIP LOCKED claim  │  ▲ pg_notify('status_<user>')
                                           ▼  │
                                    ┌──────────────┐
                                    │ V2 worker 池  │ serve_worker.py（runner-CVM）
                                    │ N asyncio slot│ 1回合1worker:
                                    │ × M 进程/CVM  │  coalesce→planner→executor→responder
                                    └──────┬───────┘  job 起手单次解密 provider key
                                           ▼ in-process 调用（零 HTTP）
                                    ┌──────────────┐
                                    │ capabilities/│ memory/identity/perception/screen/photo/chat
                                    └──────┬───────┘
                                           ▼ 现有 domain service（不改）
                              memory.service / perception / screen.service / identity.service
```

### 3.1 模块归属（遵 `CONTRIBUTING.md` 依赖方向）

```
backend/
├── capabilities/                  ← 【新】canonical 能力单元（决策 4）
│   ├── __init__.py types.py errors.py    统一 {ok,data,trace,warnings} / 错误形状
│   └── memory.py identity.py perception.py screen.py photo.py chat.py
│                                   只 import memory/identity/perception/screen（均在其下层）
├── model_api_runtime/v2/          ← 【新】V2 运行时
│   ├── jobs_store.py                 agent_jobs/action_queue/status_events/runtime_state CRUD + claim
│   ├── worker.py                     claim loop + job 编排 + 三个有界闸
│   ├── planner.py executor.py responder.py
│   ├── coalesce.py invalidation.py   多消息合并 / replan 状态机
│   └── serve_worker.py               进程入口（+ 可选薄 health FastAPI）
├── hosted/chat_routes_asgi.py     ← 改：chat/send 入队 job（gated by hosted_runtime_mode）
├── hosted/config_store.py         ← 改：加 hosted_runtime_mode 读写
├── chat/poll_core.py              ← 改：long-poll 返回体带上 status 事件游标
└── alembic/versions/0014_hosted_runtime_v2.py   ← 【新】4 张表
```

依赖核对：`capabilities` 在 `perception/identity/memory/screen` 之上、`model_api_runtime` 之下 →
`model_api_runtime/v2` 向下 import `capabilities` ✅；io_cli 的 HTTP 端点、resident consumer 各自
委托 `capabilities.*` ✅；**worker 不调用 io_cli**（原文档 §11）。

---

## 4. capability 抽取层（Phase 0–1）

### 4.1 canonical 契约

> **关键事实（取证确认）**：想要的「框架中立能力实现」**已存在**——即各领域包的 `*_core.py`
> 函数（`memory_core.index/fetch/actions`、`perception_core.agent_perception_payload/trend/history`、
> `screen_read_core.list_frames/frame_decrypt`、`perception_read_core.photos_recent/photo_content`、
> `identity_core.get_identity/run_actions`），签名皆 `fn(store, api_key, payload) -> (body, status)`
> 或 `ScreenResult`，ASGI 路由只是经 `run_db` 委托它们。所以 capability 层**不是大搬迁，是薄 facade**。

`backend/capabilities/` = 在现有 `*_core` 之上的**薄统一 facade**，只做三件事：

```python
def index(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult: ...
```

1. **收敛异构返回**为统一形状（cores 现在返回 `(body,status)` / `ScreenResult` / raise `AgentRouteError`）：
   - 成功：`{"ok": True, "data": {...}, "trace": {...}, "warnings": []}`
   - 失败：`{"ok": False, "error": {"code": "memory_unavailable", "message": "安全摘要", "retryable": True}}`
2. **size cap + 脱敏**（敏感原文经 cap/redact，供 status 事件与 responder 消费）。
3. **给 worker 一个 in-process 单一入口** + executor 的 **action-type → 函数 派发表**。

### 4.2 谁改、谁不改（取证后修正——大幅缩小改动面）

- **`*_core` 函数：不动**（已是 canonical 框架中立实现）。capability 只 import 调它们。
- **后端端点：不动**（本就 `run_db` 委托 `*_core`）——**不做**「重构端点走 capability」的回归风险动作（YAGNI）。
- **`tools/io_cli.py`：不动**（本就是 HTTP 薄壳打现有端点）。
- **`tools/chat_resident_consumer.py`：本 spec 不动**（它 spawn CLI→io_cli→HTTP，不内嵌能力逻辑）。
- **V2 worker（Plan C）→ 直接 import 调 `capabilities.*`（无 HTTP、无 io_cli bash，省进程/延迟的关键）。**

> 即「三方共用一份实现」在 `*_core` 这层**已经成立**；capability facade 的增量价值是**统一契约 +
> 脱敏 + worker 的 in-process 单一入口/派发表**，而非重写。

### 4.3 动作词表（初期约束词表）

| 类别 | actions | 并发性 |
|---|---|---|
| 读/上下文 | `identity_get` `memory_index` `memory_fetch` `perception_snapshot` `perception_history` `perception_trend` `screen_recent` `screen_read` `photo_recent` `photo_read` `chat_image_read` `recent_chat_digest` | 独立时可并行（`MAX_READ_ACTION_PARALLELISM` 闸） |
| 写/变更 | `memory_write` `identity_patch` `capture_memory` `schedule_followup` `schedule_wake` `cancel_wake` `sleep` | 串行 + 守卫 |
| 响应 | `final_response` | 模型作者，唯一写聊天气泡的 action |

现有 io_cli verb 映射来源：`memory-index`(io_cli.py:565) / `memory-fetch`(:574) /
`perception|perception-trend|perception-history` / `screen-recent`(:581) / `screen-read`(:585) /
`photo-recent|photo-read` / `chat-image` / `identity-write`。

### 4.4 Phase 0 交付物

`docs/superpowers/specs/runtime-v2-parity-matrix.md`：当前 resident/io_cli 每个能力 → 一个
capability 函数 + 一个 action type，逐行映射，作为 parity 验收清单。

---

## 5. DB schema（Alembic `0014_hosted_runtime_v2.py`）

命名跟现有约定（`NNNN_slug.py`，最新 `0013`）。加密不变：canonical 长期记忆仍是加密后端
DB 状态；`runtime_state` 只存**非敏感** digest，敏感原文不落这里。

```sql
-- durable 工作单元, per-user single-flight
CREATE TABLE agent_jobs (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,  -- 对齐 0011/0012 删账号级联
  lane TEXT NOT NULL,          -- chat|manual_wake|heartbeat|scheduled|capture|maintenance
  status TEXT NOT NULL,        -- pending|claimed|running|completed|failed|expired|cancelled
  reason TEXT, trace_id TEXT, priority INT NOT NULL DEFAULT 0,
  claimed_by TEXT,             -- worker_id（claim_next_job 落 owner）
  attempt_count INT NOT NULL DEFAULT 0, last_error TEXT,
  invalidated_by_job_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at TIMESTAMPTZ, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, deadline_at TIMESTAMPTZ
);
-- 注：agent_action_queue / agent_status_events / runtime_state 的 user_id 同样
--     REFERENCES users(user_id) ON DELETE CASCADE（B Plan Task 1 已含）。
-- 每 user 每 visible lane 至多一个活跃 job（coalesce 的强制约束）
CREATE UNIQUE INDEX ux_agent_jobs_singleflight
  ON agent_jobs(user_id, lane) WHERE status IN ('pending','claimed','running');
-- claim 扫描
CREATE INDEX ix_agent_jobs_claim ON agent_jobs(status, priority DESC, created_at);

CREATE TABLE agent_action_queue (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL, seq INT NOT NULL,
  type TEXT NOT NULL, payload_json JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending', -- pending|running|completed|failed|skipped|invalidated
  visible BOOL NOT NULL DEFAULT false, requires_model_authorship BOOL NOT NULL DEFAULT false,
  result_json JSONB, last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);
CREATE INDEX ix_action_queue_job ON agent_action_queue(job_id, seq);

CREATE TABLE agent_status_events (       -- 非聊天 UX/debug 事件（允许 runtime-authored）
  id BIGSERIAL PRIMARY KEY, job_id BIGINT, user_id TEXT NOT NULL,
  kind TEXT NOT NULL,                    -- processing|reading_memory|writing_reply|...
  label TEXT, detail_json JSONB NOT NULL DEFAULT '{}',  -- 脱敏: 标签+粗计数, 无原文
  seq INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_status_events_user ON agent_status_events(user_id, created_at DESC);

CREATE TABLE runtime_state (             -- 每用户 compact digest（planner/responder 读）
  user_id TEXT PRIMARY KEY,
  state_json JSONB NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

claim 查询：
```sql
SELECT * FROM agent_jobs
 WHERE status='pending' AND (deadline_at IS NULL OR deadline_at > now())
 ORDER BY priority DESC, created_at
 FOR UPDATE SKIP LOCKED LIMIT 1;
```

---

## 6. worker 进程与并发模型（决策 2/5）

- **进程入口** `model_api_runtime/v2/serve_worker.py`：单进程起 `MAX_WORKERS` 个 asyncio job-slot；
  线上多开进程 × CVM，全靠同一张 `agent_jobs` 的 `SKIP LOCKED` 协作抢活。**加进程 = 线性扩容。**
- **claim loop**：复用现有 LISTEN/NOTIFY 唤醒总线（`wake_bus`）即时唤醒 + 周期轮询兜底。
- **单次解密 + 两套凭证分离**：job 起手 JIT 解密用户 BYOK provider key（复用现有 runtime-token
  路径），整回合留 worker 内存，**绝不落库/绝不进 runtime_state**。注意**两套独立凭证别混**：
  - **enclave-auth**（Feedling `api_key` / `runtime_token`）→ 给 capability 的 enclave 解密转发用（executor）。
  - **BYOK provider_config**（provider/model/base_url/**LLM key**）→ 给 planner/responder 的 LLM 调用用。
  因依赖方向（`model_api_runtime/v2` 在 `hosted/` 之下，不能 import `config_store` 解密），provider
  解析走 worker 的**注入式 `TurnDeps.resolve_provider`**（生产实现在 `serve_worker.py`，那里才可
  import hosted），把 `provider_config` 注入 planner/responder，而非 import 或从 runtime_state 读。
- **三个有界闸**（都在「一回合一 worker」内部，不是独立池）：
  ```
  MAX_WORKERS                    每进程并发 job 数（= 并发回合数）
  MAX_READ_ACTION_PARALLELISM    单 job 内 executor 并行读上限
  PROVIDER_DECRYPT_SEMAPHORE     跨所有 job 共享的 provider 调用 + enclave 解密并发闸（治风险 3）
  ```
- **生命周期**：`deadline_at` + stuck-job reaper（照 genesis stale-reaper）扫 `claimed/running` 超时
  → `expired`/重试；`attempt_count` 上限后 `failed` 落 `last_error`。
- **优雅 drain**：SIGTERM 停止 claim、跑完手上 job、释放（不再续租）。

---

## 7. turn 流程：coalesce → planner → executor → responder

### 7.1 coalesce（多消息）

claim 时把该用户自「上次已回复游标」以来所有 pending 用户消息并成一轮。single-flight 唯一索引
保证同 user 同 lane 至多一个活跃 job，A/B/C 三条消息只产生一个模型回合（不是三条独立回复）。

### 7.2 planner

- **输入**：合并消息 + `recent_chat_digest` + persona/identity digest + **便宜预取的 memory index** +
  感知/屏幕/照片摘要 + lane/reason + `runtime_state`。
- **digest 一律确定性生成（无 LLM）**：`recent_chat_digest` = 近期消息的确定性选取/截断，
  memory index / 感知摘要 = capability 读的结构化结果——都不发模型调用，故不触及任何 key。
  （若将来要 LLM 摘要，同样只能用**用户自己的** key，见 §7.3 不变量。）
- **输出**：校验过的 JSON plan（1–5 actions）+ reason，Pydantic 卡形状与词表白名单。
  ```json
  {"plan":[{"type":"memory_fetch","ids":["mem_123"]},
           {"type":"perception_snapshot","signals":["now","calendar"]},
           {"type":"final_response"}],
   "reason":"Need memory and current calendar before answering."}
  ```
- **约束**：≤5 actions、优先短计划、非响应 action 不得产生可见文本、无强理由不 mutate、
  弱 wake 不值得可见输出则 `sleep`、需回复则含 `final_response`。

### 7.3 planner 用谁的 key（决策 6，治风险 2）

> **不变量（硬约束，写进实现 + 测试断言）**：API-key 用户回合内**所有** LLM 调用
> （planner + responder + 任何需要模型的 digest 生成）**一律用该用户自己的 provider key**。
> **不存在平台级 LLM key 兜底。** 弱模型的兜底是「确定性、无-LLM 的规则 planner」，
> **不是**换平台 key 去跑。这里的「用户」= 该 hosted/API-key 用户 JIT 解密出的 BYOK provider key。

- planner **跑用户自己的 BYOK key**（保成本隔离 + proactive 不烧钱铁律）。
- **该用户的 provider 是 official/可信端点**（`_is_official_identity` / `driver_for_provider` 判定，
  仍是**用户的** key，只是端点可靠 tool-call）→ 轻量结构化 JSON planner。
- **该用户带的是弱/杂牌模型** → **确定性规则 planner，不发任何 LLM 调用**：chat lane 规则兜底
  `{读近期上下文 + memory_index → final_response}`，把 parity 压在 responder（用户 key 出最终回复）。

### 7.4 executor

- 读并行（`MAX_READ_ACTION_PARALLELISM` 闸）、写串行 + 守卫。
- 每 action：出 status 事件 + tool_trace、带超时/重试/输出封顶；结果进
  `action_queue.result_json` + 汇入 `runtime_state`。
- 图片：优先直接 image content（provider API 支持处），避开 Claude/Codex 本地文件权限坑。

### 7.5 responder

- 用户 key 出 **model-authored** 回复，落**加密** `chat_messages`。
- **no-filler 铁律**：只有 `final_response` 写聊天气泡；其余全是 status 事件。runtime **绝不**
  自造 assistant 文本（`小克看到了…` 这类除非模型自己写的）。

---

## 8. replan / invalidation（安全点）

- **安全点**：读批完成后 / `final_response` 前 / 写操作前。
- 运行中新可见用户消息到达 → 现有 plan 的 pending actions 置 `status='invalidated'`，job 记
  `invalidated_by_job_id`，在下一个安全点带 A+B+C 上下文重规划。
- 若 `final_response` 流式中被打断 → **默认写完**（已在产出有用回答则保留）；provider 支持干净
  abort 且 UX 更好时可 abort 后重规划（product 可调，默认 finish）。
- 短计划 + 常 replan，优于执行陈旧长计划。

---

## 9. tool-call 步骤推送管线（决策 7：仅前台 in-app）

```
worker(runner-CVM)  ── INSERT agent_status_events + pg_notify('status_<user>') ─▶ Postgres
web 层(asgi)  long-poll waiter (chat/routes_asgi.py) 被 wake_bus LISTEN 唤醒 ─▶ 返回自游标以来的 status 事件
```

- 复用现有 long-poll：`chat/routes_asgi.py` 把 asyncio future park 在 `runtime.waiters`，由
  `notify_chat_waiters` / `wake_bus.notify` 唤醒。worker 在 runner-CVM 写库 + 对同一条 Postgres
  LISTEN/NOTIFY 总线 `pg_notify` → web 层 park 住的 poll 醒来 → `poll_core` 返回体带上 status 游标。
- app 侧：聊天气泡来自 `chat_messages`，**状态条来自 `agent_status_events`**。
- **action type → status kind**：
  ```
  job 起手     processing
  读类         reading_memory / reading_perception / reading_screen / reading_photo / retrieving_chat_image
  写类         capturing_memory / updating_identity / scheduling
  responder    writing_reply
  结束         done / sleeping
  ```
- **两条红线**：
  1. **脱敏**：status 只带标签 + 粗计数（如「读取 3 张记忆卡」），绝不带解密原文/记忆/截屏/tool 原始输出。
  2. **限频/合并**：并行读瞬间冒的多条合并为一条（「读取上下文（记忆、感知）」）并限频，不刷屏。

---

## 10. chat/send 集成 + 灰度

- `POST /v1/model_api/chat/send`（`hosted/chat_routes_asgi.py`）：落加密用户消息 → 入队/合并
  `agent_job`（若已有 pending 则 coalesce）→ 快速返回、**不写 filler**。
- **灰度**：`hosted_runtime_mode = resident_cli | db_action_v2`，per-user/provider flag 存
  `hosted/config_store.py`。resident 路径**原样不动**，两条并存；先内部/test 用户。
- **背压**：worker 满 → job 留 `pending` 且 status 可见，**绝不产生静默卡死的 turn**；
  或返回明确 busy（product 选择，默认入队 + status）。

---

## 11. 风险登记 + 缓解（写进实现，不是备注）

| # | 风险 | 缓解 |
|---|---|---|
| R1 | **planner 一次规划 ≠ 真·agent loop 的 parity**（依赖多跳读取） | prefetch 便宜 index 进 planner 输入 + 读后 replan 一轮（粗粒度 agent loop）；**parity harness 在 Phase 1–2 就上**，不拖到压测期 |
| R2 | **planner 用谁的 key** 破成本隔离/不烧钱铁律 | 决策 6：用户 key + 弱模型退化确定性规则 planner（不发 LLM），parity 压 responder |
| R3 | **enclave 串行化**：解密**不在本地**——provider-key 解密 + **每个 memory/screen 读 action 都 httpx 打 enclave**（`memory_index_core` / `frame_decrypt`，`verify=False` + runtime-token 转发）；16–32 worker 齐打 = 放大当前 502 | provider-key **每 job 单次解密**（非每 action）；共享 `ENCLAVE_SEMAPHORE` 框住**所有 enclave-bound 能力调用**（不止 key）；同 job 内 enclave 读尽量合并/缓存 |
| R4 | replan/invalidation 引入同用户竞态（此仓有前科） | 建模为 `agent_jobs`/`agent_action_queue` 显式状态迁移（`invalidated_by_job_id` / action `invalidated`）+ single-flight 唯一索引 + TDD 覆盖并发 claim |

---

## 12. 测试策略（TDD，测试进 `tests/`，走 `make_client`）

- **capability 契约测试**：同一 fixture 断言「io_cli 端点」与「直调 capability 函数」返回同形
  `{ok,data}`——保证抽取正确、三方一致。
- **parity harness（治 R1，早上）**：golden 前台 fixtures 同时跑 resident 与 V2，diff 回复 /
  工具轨迹；Phase 1–2 即跑。
- **单元**：并发 claim 只有一个赢 / `SKIP LOCKED` / single-flight 唯一索引冲突 /
  replan-invalidation 状态机 / coalesce / 单次解密 / 三个闸的背压 / status 脱敏与限频。
- **key 隔离断言（守 §7.3 不变量）**：断言 API-key 用户回合内每次 provider 调用都用该用户
  JIT-解密出的 key；测试注入一个「平台 key 探针」，断言它**从不**被 planner/responder/digest 触达；
  弱模型路径断言 planner **零** LLM 调用（走确定性规则）。
- 复用 `tests/conftest.py` 的 `make_client` / `seed_user`；参考现有
  `test_agent_runtime_supervisor.py`（注入 spawn/alive/kill）的注入式测法给 worker claim loop。

---

## 13. A+B+C 内部实现顺序

1. **P0 parity 矩阵** → `runtime-v2-parity-matrix.md`（先摸清要对齐什么）。
2. **A capability 层** + 契约测试（io_cli/consumer 收口到 `capabilities/`）。
3. **B schema `0014`** + `jobs_store` + claim/single-flight/reaper（含并发单测）。
4. **B worker 骨架** `serve_worker.py`：chat lane 跑通 `chat/send → job → worker → 单次解密 →
   （先直接 final_response，无 planner）→ 加密 assistant 回复`。end-to-end 最小闭环。
5. **C planner + executor + action queue**：接入 §7；memory/perception action 无额外 LLM 执行。
6. **C replan/invalidation + coalesce**（§8）。
7. **C status 推送管线**（§9）接 long-poll。
8. **灰度开关**（§10）+ parity harness 全绿。

---

## 14. Open items（本 spec 内待定，非阻塞）

- `poll_core` 返回体带 status 游标的具体 wire 形状（需与 iOS 对齐；本 spec 定管线，字段细节实现期定）。
- planner「official vs 弱模型」判定的确切阈值（复用 `_is_official_identity` / `driver_for_provider`，边界用例实现期补）。
- busy 背压到底「入队 + status」还是「返回 busy」——默认入队，产品可翻。

## 15. → 子项目 D（另立 spec）

proactive/wake/heartbeat/scheduled/capture lane 对齐、100 用户压测、`preliminary_response`、
Live Activity、全量 rollout。worker 已预留 `lane` 字段 + lane 分发点，D 往里加。

---

## 16. A+B+C 成功标准

- **功能**：`db_action_v2` 用户的前台聊天，parity harness 核心 fixtures 全绿（回复/工具轨迹对齐 resident）。
- **正确性**：多消息 coalesce 成一回合；并发 claim 无双回复/无双扣 key；无静默卡死 turn。
- **UX**：聊天气泡仅 model-authored；tool-call 步骤前台实时可见且脱敏；可连发多条。
- **不劣化**：resident_cli 用户零影响；不新增 backend→enclave→backend reentrant 跳。
