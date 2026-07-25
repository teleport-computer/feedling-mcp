# D0 Rollout Infrastructure Implementation Plan

> **RETIRED / DO NOT DEPLOY.** Historical cutover plan：单向 V2 互斥闸
> rollout 机制已被 2026-07-21 dual 共存的 per-user fence
> （`resident/draining/v2` + generation）双向切换取代。对应 spec
> （`…-D0-rollout-infrastructure-design.md`）已 RETIRED，本 plan 同判。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** 让 V2 在 prod 能跑、安全、可控：部署 worker 池 + 互斥闸 + mode setter + per-turn 指标。

**Architecture:** 见 spec `2026-07-09-hosted-runtime-v2-D0-rollout-infrastructure-design.md`。四块独立：compose 服务（ops）/ 发现互斥闸 / admin setter / 指标插桩。

## Global Constraints

- **NO-COMMIT**；**worktree** 只在 `.claude/worktrees/hosted-runtime-v2`，绝不碰主 checkout。
- **依赖方向**：指标记录逻辑住 `v2/`，经 TurnDeps 注入；admin 端点住 `backend/admin/`。
- mode 值 `resident_cli`/`db_action_v2`（`config_store.HOSTED_RUNTIME_MODE_*`）。互斥闸过滤 `model_api_runtime` blob 的 `hosted_runtime_mode='db_action_v2'`。
- 测试：`python -m pytest tests/... -q`（repo 根，`DATABASE_URL` 由 conftest 设）；用 `conftest.seed_user`、`db.get_pool()`。7 pre-existing 失败无关。

---

### Task 1: 发现查询互斥闸

**Files:** Modify `backend/db.py`（`list_agent_runtime_enabled_users`）；Test `tests/test_v2_exclusivity_guard.py`（新）。

**Interfaces:** 不改签名，纯加 WHERE 谓词。

- [ ] **Step 1 失败测试**：seed 两个 model_api test_status=ok/provider=anthropic 用户；给其中一个写 `model_api_runtime` blob `{"hosted_runtime_mode":"db_action_v2"}`（`config_store.set_hosted_runtime_mode` 或直接 `db.set_blob(uid,"model_api_runtime",{...})`）。断言 `db.list_agent_runtime_enabled_users()` 返回列表**含**resident 用户、**不含**db_action_v2 用户。
- [ ] **Step 2 跑测失败**：`python -m pytest tests/test_v2_exclusivity_guard.py -q` → FAIL（db_action_v2 用户仍在列表）。
- [ ] **Step 3 实现**：在 `list_agent_runtime_enabled_users` 的 SQL `WHERE` 里加：

```sql
AND NOT EXISTS (
  SELECT 1 FROM user_blobs r
  WHERE r.user_id = user_blobs.user_id
    AND r.kind = 'model_api_runtime'
    AND COALESCE(r.doc->>'hosted_runtime_mode', '') = 'db_action_v2'
)
```
（保留现有 `except → return []` fail-safe。）
- [ ] **Step 4 跑测通过**。
- [ ] **Step 5**（不 commit，记 report）。

---

### Task 2: mode setter 控制面（admin 端点 + CLI）

**Files:** Modify `backend/admin/routes_asgi.py`；Modify `tools/io_cli.py`（或新 admin 脚本）；Test `tests/test_admin_runtime_mode.py`（新）。

**Interfaces:** `POST /v1/admin/hosted-runtime-mode {user_id,mode}` → `set_hosted_runtime_mode`（ValueError→400）；`GET /v1/admin/hosted-runtime-mode?user_id=` → 当前 mode。CLI `set-runtime-mode <uid> <mode>` / `list-runtime-mode [--mode]`。

- [ ] **Step 1 失败测试**：先 `grep -n "admin" backend/admin/routes_asgi.py` 找 admin 鉴权装配与既有路由风格（照抄 data-track 路由的 admin gate）。测试：POST 合法 mode→200+落地（`get_hosted_runtime_mode` 变）；非法 mode→400；无 model_api config 用户→400。GET 回当前 mode。
- [ ] **Step 2 跑测失败**。
- [ ] **Step 3 实现**：加路由包 `set_hosted_runtime_mode`/`get_hosted_runtime_mode`（经该请求的 store 构造，照抄现有 admin 路由如何拿 store/鉴权）；CLI 子命令打端点。`list-runtime-mode` 走一个轻查询（按 `model_api_runtime.hosted_runtime_mode` 分组列 user_id）。
- [ ] **Step 4 跑测通过**。
- [ ] **Step 5**（不 commit）。

> **Note**：admin 鉴权/ store 构造照抄现有 admin 路由，别自造。CLI 若走 io_cli 注意 `docs/CHANGELOG.md:437` 的 duplicate add_parser 崩坑——加子命令后跑一次 `io_cli --help` 确认不崩。

---

### Task 3: `v2_turn_metrics` 表 + jobs_store 读写

**Files:** Create `backend/alembic/versions/0017_v2_turn_metrics.py`（down_revision 接当前最新 = `0016_v2_conversation_summary`）；Modify `backend/model_api_runtime/v2/jobs_store.py`；Test `tests/test_v2_turn_metrics.py`（新，DB）。

**Interfaces:**
- `record_turn_metric(*, job_id: int | None, user_id: str, lane: str, prompt_tokens: int | None, completion_tokens: int | None, latency_ms: int | None) -> None`
- `recent_mean_tokens_per_turn(*, lane: str = "chat", limit: int = 50) -> float | None`
- `pending_job_count() -> int`

- [ ] **Step 1 失败测试**：迁移建表后，`record_turn_metric` 插 2 行（tokens 10+20, 20+30）→ `recent_mean_tokens_per_turn(lane="chat")` ≈ (30+50)/2=40；None-token 行不崩（跳过或计 0，测试固定预期）；`pending_job_count` 计 pending。
- [ ] **Step 2 跑测失败**。
- [ ] **Step 3 实现**：迁移建表（schema 见 spec D0.4）；三函数（纯读/写，`_pool()` autocommit，照抄现有 jobs_store 函数风格）。`recent_mean_tokens_per_turn` = `AVG(prompt_tokens+completion_tokens)` over 最近 limit 条该 lane、忽略 NULL。
- [ ] **Step 4 跑测通过**。
- [ ] **Step 5**（不 commit）。

---

### Task 4: responder 捕获 usage + TurnDeps 接线 + metrics 端点

**Files:** Modify `backend/model_api_runtime/v2/worker.py`（`TurnDeps` 加 `record_turn_metric`）、`responder.py`（抽 usage+latency、经 deps 回调）、`serve_worker.py`（装配注入真回调）；Modify `backend/admin/routes_asgi.py`（`/v1/admin/v2-metrics`）；Test 扩 `tests/test_v2_responder.py` + 新 `tests/test_v2_metrics_endpoint.py`。

**Interfaces:** `TurnDeps.record_turn_metric: Callable[..., None] | None`（默认 None，守依赖方向）。`GET /v1/admin/v2-metrics` → `{inflight,pending,live_workers,mean_service_sec,recent_mean_tokens_per_turn}`。

- [ ] **Step 1 失败测试**：`respond` 传一个记录 usage 的 fake provider 响应（带 `usage.prompt_tokens/completion_tokens`）+ spy `record_turn_metric`；断言被调且 tokens 对。provider 响应无 usage 时→记 None、不崩。metrics 端点返回五字段（mock jobs_store 计数）。
- [ ] **Step 2 跑测失败**。
- [ ] **Step 3 实现**：`responder.respond` 拿到 provider 响应后**防御性**抽 usage（`getattr`/dict 双路，provider 间形状不同，取不到 None）+ wall-clock latency_ms；若 `deps.record_turn_metric` 非 None 则调（job_id/user_id/lane 由调用方 worker 传入 respond 或经 deps 上下文——实现时确认 respond 能拿到 lane/job_id，不够则由 worker 在 respond 返回后记录，二选一，保持 responder 纯）。serve_worker `build_production_deps` 注入 `jobs_store.record_turn_metric`。metrics 端点组合 §6 `inflight_job_count`/新 `pending_job_count`/`live_worker_count`/`recent_mean_service_sec`/`recent_mean_tokens_per_turn`。
- [ ] **Step 4 跑测通过**。
- [ ] **Step 5**（不 commit）。

> **Note（给实现者）**：若在 responder 内拿不到 job_id/lane 会破坏其纯度，**改由 worker** 在 `respond()` 返回后调 `deps.record_turn_metric`（worker 有 job 上下文）——responder 只负责把 usage/latency 作为副返回值给 worker。二选一，选不破依赖方向/纯度的那个，report 里说明选了哪个。

---

### Task 5: runner compose 加 serve_worker 服务（ops，不进 CI）

**Files:** Modify `docker-compose.phala.runner.yaml`（+`docker-compose.phala.prod.runner.yaml`、test 变体）。

- [ ] **Step 1**：加第二服务/进程跑 `python -u backend/model_api_runtime/v2/serve_worker.py`，复用 runner 既有 env（`DATABASE_URL`/`FEEDLING_RUNTIME_TOKEN_SECRET`/`FEEDLING_ENCLAVE_URL`）+ `FEEDLING_V2_MAX_WORKERS`。
- [ ] **Step 2 验证**：`docker compose -f docker-compose.phala.runner.yaml config` 解析通过（本地，不起容器）。
- [ ] **Step 3**：部署走 spec 附录 runbook（compose 变→链上 re-auth）——**部署是用户/ops 行为，非本 plan 执行**。记 report 标注"待部署"。

---

## Self-Review

- spec 覆盖：互斥闸(T1)/setter(T2)/指标表(T3)/插桩+端点(T4)/compose(T5) 全覆盖。
- 类型一致：`record_turn_metric`/`recent_mean_tokens_per_turn`/`pending_job_count` 签名跨 T3/T4 一致；mode 值常量一致。
- placeholder：T2/T4 的"照抄现有 admin 路由 / 二选一"是刻意对齐现状与保纯度，非占位——给了判据。
