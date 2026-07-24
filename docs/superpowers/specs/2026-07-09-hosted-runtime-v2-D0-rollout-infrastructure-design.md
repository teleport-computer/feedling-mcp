# Hosted Runtime V2 — D0 Rollout Infrastructure 设计

> **RETIRED / DO NOT DEPLOY.** Historical cutover design; per-user resident
> rollback, roster, and selector paths have been removed.

> 子项目 D 的前置基建。来源：walkthrough §8 gate 7 + 两份现状调查（2026-07-09）。**这是 D3/D4 的共同前提**：没有它，V2 worker 池在 prod 一次都没跑过，且被翻到 db_action_v2 的用户会被 resident + V2 双跑。

**Goal:** 让 Hosted Runtime V2 在生产**能跑、安全、可控**——部署 worker 池、堵住双跑、造出灰度翻钥控制面、加上压测/灰度要读的 per-turn 指标。

**Architecture:** 四块相互独立、可分别落地：(1) 往 runner compose 加 `serve_worker` 服务；(2) roster 发现查询按 `hosted_runtime_mode` 排除 db_action_v2 用户（互斥 = 也是关 resident 的杠杆）；(3) `set_hosted_runtime_mode` 的 admin 端点 + CLI；(4) 新 `v2_turn_metrics` 表 + responder 路径捕获 provider usage + queue-depth 读。

## Global Constraints（每个 task 隐含包含）

- **NO-COMMIT**：全程不 `git commit`、不 `git add`。用户自己提交。
- **worktree**：只在 `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/hosted-runtime-v2` 工作，绝不碰主 checkout（`pre` 分支）。每 task 核文件落点。
- **依赖方向**（`tests/test_v2_dependency_direction.py`）：`backend/model_api_runtime/v2/*` 与 `backend/capabilities/*` 不得 import `hosted`/`agent_runtime`。指标记录逻辑住 `v2/`，admin 端点住 `backend/admin/`。
- **BYOK-only**：不因本工作引入任何平台级 LLM key 兜底。指标只记 token 计数，不改 key 解析。
- **测试基线**：本仓 7 个 pre-existing 失败与本工作无关。Docker PG 在 `127.0.0.1:55432`（容器 `feedling-test-pg`）；测试用 `conftest.seed_user`、`db.get_pool()`、`DATABASE_URL` skipif 闸。

## 现状（调查确证）

- **serve_worker 池不在任何部署 manifest**：`deploy/DEPLOYMENTS.md:103` 明说 "Not yet started in any manifest… lands in subproject D's rollout"。`deploy/Dockerfile.agent-runner:78`、`docker-compose.agent-runner.yaml:24`、`docker-compose.phala.runner.yaml:90`、`docker-compose.phala.prod.runner.yaml:54` 都只跑 `python -u backend/agent_runtime/supervisor.py`。send 闸在无池心跳时 503 `workers_unavailable`（`chat_send_core.py:101`）。→ **db_action_v2 至今零执行。**
- **无互斥闸**：`db.list_agent_runtime_enabled_users`（`db.py:1260`）查 `user_blobs WHERE kind='model_api'`，读 `doc->>'provider'`/`doc->>'test_status'`，**完全不看 `hosted_runtime_mode`**。而 mode 存在**另一个 blob** `kind='model_api_runtime'`（`config_store.py:88` `MODEL_API_RUNTIME_BLOB`）的 `doc->>'hosted_runtime_mode'`。→ db_action_v2 用户仍被发现→仍 spawn resident，同时 chat/send 又入 V2 job，**双跑**。lease 只保证"每用户一个 resident"，不保证 resident-vs-V2 互斥。
- **mode 翻钥面为零**：`set_hosted_runtime_mode`（`config_store.py:339`）**无任何生产调用者**（仅测试调）。唯一生产读者是 `chat_send_core.py:88`。无 admin 端点、无 CLI、无灰度工具。今天只能直接改 PG blob。
- **零 per-turn 指标**：`worker.py`/`responder.py`/`planner.py` 全无 `prompt_tokens`/`usage`/`latency`/`cost`。只有静态 `max_tokens` 上限（`responder.py:112`）。queue-depth 只能 ad-hoc SQL，无 gauge。压测要的 tokens/turn、saturation 全 greenfield。
- **serve_worker 已就绪**：`main()`（`serve_worker.py:363`）跑 `db.init_schema()`→`wire_assembly()`→`asyncio.run(_serve)`；`MAX_WORKERS=FEEDLING_V2_MAX_WORKERS` 默认 4（`worker.py:62`）；需 `DATABASE_URL`+`FEEDLING_RUNTIME_TOKEN_SECRET`+`FEEDLING_ENCLAVE_URL`（runner 已带）。

## 设计

### D0.1 — 部署 serve_worker 池

往 runner compose 加**第二个服务/进程**跑 `python -u backend/model_api_runtime/v2/serve_worker.py`，与 supervisor 同镜像、同 env、同 CVM。

- 改 `docker-compose.phala.runner.yaml`（prod 走 `docker-compose.phala.prod.runner.yaml`；test 走对应）。
- 环境：复用 runner 已有 `DATABASE_URL`/`FEEDLING_RUNTIME_TOKEN_SECRET`/`FEEDLING_ENCLAVE_URL`；池大小 `FEEDLING_V2_MAX_WORKERS`（默认 4，走加密 env 通道，改它无需链上 re-auth）。
- **compose 变 → `compose_hash` 变 → 链上 `addComposeHash()` re-auth**（`DEPLOYMENTS.md:134`）。这步是 ops，写进 runbook（本 spec 附录），不 TDD。
- 回滚：`DEPLOY_*_RUNNER_CVM=false` 让 runner-CVM job 休眠、CVM 保持旧镜像；或删该服务重部署。

### D0.2 — 互斥闸（发现查询按 mode 排除）

给 `list_agent_runtime_enabled_users` 的 SQL 加跨-blob `NOT EXISTS`，把 `model_api_runtime.hosted_runtime_mode='db_action_v2'` 的用户**从 resident roster 剔除**：

```sql
AND NOT EXISTS (
  SELECT 1 FROM user_blobs r
  WHERE r.user_id = user_blobs.user_id
    AND r.kind = 'model_api_runtime'
    AND COALESCE(r.doc->>'hosted_runtime_mode', '') = 'db_action_v2'
)
```

- 语义：db_action_v2 用户不再被发现 → `Supervisor.tick()` 下一 tick（~15s）reap 其 resident（`supervisor.py:167`）并释放 lease。**这既是双跑修复，也是"关 resident"的杠杆**——无需独立 teardown 路径。
- 加参 `include_gateway` 之外不改签名；纯加 WHERE 谓词。
- fail-safe：子查询异常仍走 `except` 返 `[]`（现有行为，宁可不发现也不双跑）。

### D0.3 — mode setter 控制面

- **admin 端点**（`backend/admin/routes_asgi.py` 现有 admin 面加路由）：
  - `POST /v1/admin/hosted-runtime-mode` body `{"user_id","mode"}` → 包 `set_hosted_runtime_mode`（ValueError→400）。回落地后的 mode。
  - `GET /v1/admin/hosted-runtime-mode?user_id=` → 回该用户当前 mode。
  - 走现有 admin 鉴权（照抄 data-track 路由的 admin gate）。
- **CLI**（`tools/io_cli.py` 或独立 admin 脚本）：`set-runtime-mode <user_id> <resident_cli|db_action_v2>` + `list-runtime-mode`（按 mode 列用户，喂灰度分批）。走上面端点。
- 灰度用法：内测用户先翻 → 观察 → 分批 ramp → 回滚 = 翻回 resident_cli（default）。

### D0.4 — per-turn 指标插桩

- **新表** `v2_turn_metrics`（Alembic 0017）：`id BIGSERIAL PK, job_id BIGINT, user_id TEXT, lane TEXT, prompt_tokens INT, completion_tokens INT, latency_ms INT, created_at TIMESTAMPTZ default now()`。append-only，独立于 agent_jobs（不污染 job 行、job 清理后 token 数据仍在、易 AVG）。**ciphertext-agnostic**（只存计数，无内容）。
- **捕获点**：`responder.respond` 拿到 provider 响应后，从响应对象**防御性**抽 usage（`prompt_tokens`/`completion_tokens`，provider 间形状不同，取不到记 None）+ wall-clock latency，经 `TurnDeps` 注入的 `record_turn_metric` 回调落表（守依赖方向——记录函数在 serve_worker 装配层注入，responder 不直接碰 DB）。chat 与（D3 后的）wake 都过 respond，故一处覆盖。
- **queue-depth 读**：复用 §6 的 `inflight_job_count()`；加 `pending_job_count()` + `admin` metrics 端点 `GET /v1/admin/v2-metrics` 回 `{inflight, pending, live_workers, mean_service_sec, recent_mean_tokens_per_turn}`（喂 D4 压测与灰度观测）。

## 落地文件（汇总）

- `docker-compose.phala.runner.yaml`（+prod/test 变体）：加 serve_worker 服务。
- `backend/db.py`：`list_agent_runtime_enabled_users` 加互斥 `NOT EXISTS`。
- `backend/admin/routes_asgi.py`：mode setter/getter + v2-metrics 端点。
- `tools/io_cli.py`（或 admin 脚本）：set-/list-runtime-mode 子命令。
- `backend/alembic/versions/0017_v2_turn_metrics.py`：新表。
- `backend/model_api_runtime/v2/jobs_store.py`：`pending_job_count()`、`record_turn_metric(...)`、`recent_mean_tokens_per_turn(...)`。
- `backend/model_api_runtime/v2/worker.py` + `responder.py` + `serve_worker.py`：TurnDeps 加 `record_turn_metric`，respond 捕获 usage，装配层接线。
- 测试：`test_v2_exclusivity_guard.py`（发现查询排除 db_action_v2）、`test_admin_runtime_mode.py`（端点/CLI）、`test_v2_turn_metrics.py`（表+记录+AVG）、`test_v2_metrics_endpoint.py`。

## 附录：D0.1 部署 runbook（ops，非 TDD）

1. 改 compose 加 serve_worker 服务；本地 `docker compose -f docker-compose.phala.runner.yaml config` 校验。
2. CI 构建 `feedling-agent-runner:<sha>`；bump compose tag。
3. `phala deploy --cvm-id <runner>` 就地更新；因 compose 变，`deploy/publish-compose-hash.sh` 发新 `compose_hash` 上 Sepolia（pre 合约 `0x6584…`）+ 链上 `addComposeHash()`。
4. 验证：`v2_worker_heartbeats` 有心跳、`workers_alive()` True；翻一个内测用户到 db_action_v2、发消息、看 202+回复（不再 503 workers_unavailable）。
5. 回滚：`gh variable set DEPLOY_*_RUNNER_CVM --body false`（CVM 保持旧镜像，inline main-CVM runner 无缝兜底）或翻回用户 mode。

## 自查

- placeholder：无 TBD；SQL 谓词、表 schema、端点签名、env 名给全。
- 一致性：互斥闸的 mode 值 `db_action_v2` 与 `config_store` 常量一致；跨 blob kind（`model_api` vs `model_api_runtime`）已核实。
- scope：四块独立可分别 review/上线，单 plan 可覆盖。
- 歧义：指标存新表非 agent_jobs 列——已显式选定（append-only 便 AVG、不污染 job）。
