# 双运行时共存（V1 agent-runner + V2 主 CVM 内嵌）+ per-user allowlist 切换 — 设计 spec

日期：2026-07-21
状态：待评审
分支基底：`pre`（含 `ec377440` merge），新分支 `feat/dual-runtime`
前置调查：V2 三面隔离审计全 clean；V1 退役 commit = `2b294a1f`（73 文件，-10019 行）；
退役前的 `chat_send_core` 本身就是 shipped 过的 V1/V2 双分支路由器。

---

## 1. 目标与非目标

**目标**

1. prod 上 V1（agent-runner 托管，现状）与 V2（Runtime V2 worker 池）同时在线。
2. 按 user_id 灰度：DB allowlist 控制哪些用户走 V2，其余默认走 V1（= 今天的 prod 行为，零扰动）。
3. 切换双向可回滚：加名单 → 排空切到 V2；移出名单 → 排空切回 V1。改名单不重部署。
4. V2 serve-worker 与 backend/enclave 同 CVM 部署（compose 内网直连 enclave）；V1 继续独占
   runner CVM，**prod runner 侧零改动**。
5. 内建终局：全量切 V2 后，退役 V1 = 关 runner CVM + 重放 `2b294a1f`（现成模板）。

**非目标**

- 不改 V1 任何执行语义（supervisor/spawners/consumer 从 test 原样恢复）。
- 不做按百分比自动灰度（明确 user_id 名单制）。
- 不动 iOS：`agent_runtime_cutover` 的 driver label 保持 wire 兼容，客户端无感。
- 不在本项目里解决 V1 已知的历史问题（proactive 重试风暴等）——V1 是过渡态，原样保留。
- 共存态不是长期态：目标 4-6 周内走完灰度到全量。

---

## 2. 拓扑

```
┌─ 主 CVM（prod: feedling-enclave-v2）────────────────────────┐  ┌─ runner CVM（feedling-prod-runner-1）─┐
│ ingress │ backend(双路由 + reconciler) │ enclave            │  │ V1 agent-runner：supervisor +          │
│ serve-worker ← 新容器，backend 同镜像                        │  │ chat_resident_consumer host-all 舰队   │
│   FEEDLING_ENCLAVE_URL=<compose 内网 enclave 地址>           │  │ **prod 现状，一行不动**                 │
└──────────────┬──────────────────────────────────────────────┘  └───────────────┬───────────────────────┘
               └───────── 共享 RDS：agent_jobs / v2_* 表 / v2_user_allowlist / per-user fence ─────────────┘
```

要点：

- serve-worker 启动命令 `python -u backend/model_api_runtime/v2/serve_worker.py`，纯 backend
  树内代码、native provider 调用、**不需要 CLI 二进制** → 直接用 backend 镜像，主 CVM 一个
  镜像两个容器（backend + serve-worker）。
- V2→enclave 走 compose 内网，绕开 runner→主 CVM 公网 gateway 这条历史慢路
  （enclave↔backend reentrant bottleneck 的诱因之一）。
- Genesis import 是 V2 serve-worker 内的队列 worker（`FOR UPDATE SKIP LOCKED` 认领，
  从不依赖 resident CLI），共存期直接全量走 V2（见 §6 lane 表）。

---

## 3. 路由脊柱（恢复 shipped 过的双分支，非新造）

### 3a. per-user 真相源

现存机制原样复用（pre 里没删）：

- mode 常量：`HOSTED_RUNTIME_MODE_RESIDENT = "resident_cli"` /
  `HOSTED_RUNTIME_MODE_DB_ACTION_V2 = "db_action_v2"`（config_store.py:383-384）。
- per-user fence：`db.get_hosted_runtime_control_strict(user_id)` →
  `(mode, state ∈ {resident, draining, v2}, generation)`。
- 原子切换：`_set_hosted_runtime_mode_for_user_id`（per-user
  `hosted_runtime_config_mutation_lock` + generation-fence），双向可用。

### 3b. policy 恢复双值

`hosted_runtime_policy()`（config_store.py:426）从「只收 `v2_only`」恢复为：

```
FEEDLING_HOSTED_RUNTIME_POLICY ∈ { "v2_only", "dual" }     # 默认值见下
```

- `dual`：send 按 per-user fence 分流（本项目的运行态）。
- `v2_only`：现 pre 语义，非-v2 用户 503 fail-closed（退役后回归此值）。
- `forced_hosted_runtime_mode()` 在 dual 下不再恒返 v2，改为「per-user fence 是真相」。
- 默认值 = `dual`（prod 部署即进共存态）；退役时翻回 `v2_only`。

### 3c. chat_send_core 双分支（从 `2b294a1f` 反向恢复 + 与后续 1 个演化 commit 对账）

```
fenced (mode, state)
  ├─ (db_action_v2, v2)      → workers_alive 闸 + kill_switch 闸
  │                            → db.chat_append_and_enqueue(expected_runtime_mode=db_action_v2)
  │                            → notify "v2_jobs"                      ← pre 现状，零改动
  ├─ (resident_cli, resident) → check_supervisor_live wedge 闸（恢复）
  │                            → append + wake resident consumer       ← 恢复退役前分支
  └─ (*, draining)            → 503 fail-closed（切换瞬间的短窗口，客户端重试即穿过）
```

恢复的 `agent_runtime_cutover.py` 函数（`2b294a1f` 删除清单）：
`check_supervisor_live` / `evaluate_supervisor_heartbeat` / `evaluate_supervisor_instances` /
`_instance_is_fresh` / `_heartbeat_max_age` / `find_reply_row` / `wait_for_reply` /
`build_ready_response` / `handle_send` / `_env_truthy`。
现存的 wire-label 函数（`driver_for_provider` / `build_processing_response` 等）不动。

两个闸的错误码保持退役前的区分：V2 死池 = `workers_unavailable`，V1 死 supervisor =
`supervisor_unavailable`——两者语义无关，运维排查靠这个区分。

---

## 4. 切换控制器（唯一全新组件）

### 4a. 控制表（新 alembic migration，纯增量）

```sql
CREATE TABLE v2_user_allowlist (
    user_id     TEXT PRIMARY KEY,
    desired     TEXT NOT NULL CHECK (desired IN ('v2', 'resident')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT ''
);
```

语义：**不在表里 = desired resident（V1，默认）**。表即真相、实时生效、改名单不重部署
（规避 admin-password 那类 env-只在-deploy-注入的坑）。默认值由
`FEEDLING_RUNTIME_DEFAULT_DESIRED ∈ {resident, v2}`（默认 `resident`）控制，只被
reconciler 读取；P6 全量切换 = 把它翻为 `v2` 重部署一次，存量行保留作为显式 pin
（这是整个项目里唯一一次靠重部署翻的开关，因为它本身就是一次性的全量事件）。

### 4b. admin 端点（走现有 admin 鉴权）

- `POST /v1/admin/runtime-allowlist` — upsert `{user_id, desired, note}` / 删除。
- `GET  /v1/admin/runtime-allowlist` — 名单 + 每行 `desired` vs 实际
  `(mode, state, generation)` 的对账视图（一眼看出谁在漂移/卡 draining）。

### 4c. Reconciler（backend 后台环，advisory-lock 选主，与 wake-bus 同模式）

```
每 ~15s（多 worker 下仅 leader 执行）：
  rows   = 全 allowlist + 默认值
  actual = get_hosted_runtime_control_strict(user)
  对每个 desired ≠ actual 的用户，逐个串行：
    desired=v2      → _set_hosted_runtime_mode_for_user_id(user, db_action_v2)
                      （内部：resident → draining → v2，generation++）
    desired=resident → 反向同一状态机（v2 → draining → resident）
  单用户失败：记录、跳过、下轮重试（指数退避，防坏用户卡整环）
```

**消息不丢的保证（复用 PR-A 原子机制，不新造）**：

- 消息在 `db.chat_append_and_enqueue` 单事务内携带 `expected_runtime_mode` 入队；
  generation/mode 不匹配的 worker 拒绝认领 → flip 前入队的消息由旧运行时收尾或按
  fence 语义重投，不会两边双跑。
- draining 窗口 send 返回 503 → 客户端既有重试逻辑穿过（窗口 = 单用户排空时长，秒级）。
- V1 侧退出：supervisor 本来就监听 roster/lease 变化（lease 删除 ≤15s 杀 consumer，
  删号事故中已验证）；flip 到 v2 的用户从 V1 roster 消失即消费者退出。
- V2 侧退出（回滚方向）：v2 → draining 后 admission 拒绝新 job，在途 turn 由
  kill-at-durable-boundary 语义收尾（PR-D 已交付的性质）。

**专项测试（P0 必须有）**：flip 瞬间并发发消息，断言恰好一条回复、零丢失、零双跑——
双向各测一次。

---

## 5. V1 恢复清单（来源逐文件钉死）

原则：**能取 test 当前版本的取 test**（比退役前快照多了 c6ae9a61 隔离加固）；
只有「pre 已演化的共享文件」才做三方合（pre 现状 × `2b294a1f` diff × test 现状）。

| # | 文件 | 动作 | 来源 | 说明 |
|---|------|------|------|------|
| 1 | `backend/agent_runtime/{supervisor,spawners,leases,tokens}.py` + README + agent_tools_prompt.md + requirements.txt | 整文件恢复 | **test 当前版** | test 版含 c6ae9a61 的 consumer_env 钉路径等加固 |
| 2 | `tools/chat_resident_consumer.py` / `tools/io_cli.py` | 整文件替换为 hosted 形态 | **test 当前版** | 带回 `_HOSTED`/runtime-token/host-all 支持；`ec377440` 里删掉的 keyless 降级守卫（`_USER_MCP_PATHS_PINNED`/`_CHAT_SCRATCH_PINNED` 等）在双运行时下是**活代码**（#97 belt-and-braces），随 test 版整体回归 |
| 3 | `backend/db.py` | 三方合 | `2b294a1f` diff | 恢复 V1 表面：`set_supervisor_heartbeat` / `read_supervisor_heartbeat` / `set_supervisor_instance_heartbeat` / `list_supervisor_instance_heartbeats` / `prune_supervisor_instance_heartbeats` / `list_agent_runtime_enabled_users` + 对应表（retirement 测试断言的清单即恢复清单） |
| 4 | `backend/hosted/chat_send_core.py` | 三方合 | `2b294a1f` diff | §3c；退役后仅 1 个 commit 演化，冲突面小 |
| 5 | `backend/hosted/config_store.py` | 三方合 | `2b294a1f` diff | §3b policy 双值；退役后 7 个 commit 演化，重点对账 |
| 6 | `backend/hosted/agent_runtime_cutover.py` | 三方合 | `2b294a1f` diff | §3c 函数清单（298 行 → 现 95 行，恢复被删部分） |
| 7 | `backend/hosted/setup_core.py`、`backend/chat/chat_core.py`、`backend/core/{store,enclave,runtime_token}.py`、`backend/genesis/daemon.py`、`backend/admin/admin_core.py`、`backend/accounts/runtime_auth.py`、`backend/bootstrap/gates.py` | 三方合（各 <60 行） | `2b294a1f` diff | 被剥掉的 V1 钩子逐文件恢复 |
| 8 | V1 测试套：`test_agent_runtime_{supervisor,spawners,leases,tokens,discovery,genesis_gate,resident_contract,resolve_cache}.py`、`test_runner_notice.py`（~5000 行） | 整文件恢复 | **test 当前版** | V1 代码 = test 原样 → 应基本全绿 |
| 9 | `tests/test_hosted_resident_retirement.py` | 改写为共存契约 | 新写 | 「V1 必须不存在」→「dual 下两边接线完整」；退役时翻回原样 |
| 10 | `tests/test_hosted_runtime_mode.py`、`test_hosted_runtime_policy.py`、`test_asgi_hosted_chat_send.py`、`test_model_api_chat_send_routing.py`、`test_hosted_agent_runtime_cutover.py` | 三方合 | `2b294a1f` diff | 恢复被裁掉的双分支用例，保留 pre 新增的 V2 用例 |
| 11 | `deploy/docker-compose.phala.yaml`（主 CVM prod）+ test/pre 对应 | 新增 serve-worker 服务 | 新写（参照 pre.runner.yaml 的 env 块） | §7 |
| 12 | `deploy/docker-compose.phala.prod.runner.yaml` | **恢复为 test 当前版** | test | prod runner 回到 V1-only 形态（即 prod 现状），不含 serve-worker |

明确**不**恢复的：`agent_runtime` 的 pi 桥接实验、`FEEDLING_LITELLM_ENABLE` 相关已退役
gateway 路径——以 test 当前版为准，test 没有的不复活。

---

## 6. lane 一致性（防双跑烧双份 BYOK）

一个用户的各 lane 必须整体跟随其 fenced mode，逐 lane 决策：

| lane | 共存期归属 | 机制 |
|------|-----------|------|
| chat send | per-user fence | §3c 双分支 |
| proactive / wake | **跟随 chat mode** | V1 侧：supervisor 的 wake 派发对 state=v2 的用户跳过（roster 本身按 mode 过滤即天然满足，接线时验证）；V2 侧：wake 只对 state=v2 用户入队。`hosted_wake_runtime_v2_enabled` 的读点在 plan 阶段枚举并改为 per-user fence 判定 |
| perception ingress | **跟随 chat mode** | 已是 per-user 判定（`perception/service.py:67 perception_ingress_runtime_v2_enabled(user_or_store)`，读点 proactive_core.py:143 / service.py:937 / perception_read_core.py:49）；改为委托 fence 状态而非独立 flag，防 flag 与 fence 漂移 |
| genesis import | **全量 V2** | serve-worker 内队列 worker，`FOR UPDATE SKIP LOCKED`，从不依赖 resident CLI；与 chat mode 正交、无双跑面 |
| screen/memory 等 consumer 内工具 | 跟随 chat（消费者即运行时） | 无独立决策点 |

**硬规则**：任何 lane 不得出现「fence 说 v2 但 V1 supervisor 仍为其起 consumer/发 wake」。
共存契约测试（§5-9）覆盖：state=v2 用户不出现在 `list_agent_runtime_enabled_users`
返回里（或等价 roster 过滤点）。

---

## 7. 部署

### 7a. 主 CVM compose 变更（prod/test/pre 三份同构）

新增服务（backend 同镜像）：

```yaml
serve-worker:
  image: <与 backend 同一 GHCR tag>
  command: ["python", "-u", "backend/model_api_runtime/v2/serve_worker.py"]
  restart: unless-stopped
  environment:
    FEEDLING_API_URL:      <compose 内网 backend>
    FEEDLING_ENCLAVE_URL:  <compose 内网 enclave>     # 关键差异：不走公网 gateway
    DATABASE_URL:          ${DATABASE_URL}
    FEEDLING_RUNTIME_TOKEN_SECRET: ${...}             # 与 backend/enclave 同值
    FEEDLING_V2_MAX_WORKERS: "4"                      # 灰度期保守值
    FEEDLING_V2_FLEET_IDENTITY_REQUIRED: "1"
    FEEDLING_V2_RUNNER_CVM_ID / FEEDLING_V2_DEPLOYED_BUILD: <CI 注入>
    FEEDLING_GENESIS_WORKER_ENABLED: "1"
    FEEDLING_V2_SANDBOX_PROVIDER: "disabled"
    （其余照抄 pre.runner.yaml 的 x-serve-worker-env）
```

pre 环境的 runner-side serve-worker 在切到此拓扑后移除（避免双池抢 job；单一部署形态，
三环境一致）。

### 7b. 迁移与部署顺序（prod）

1. alembic：V2 表（0017/0018 系）+ `v2_user_allowlist`。**纯增量，V1 不读这些表**，
   先跑 migration 后起新镜像是安全序。
2. 主 CVM 原地部署新镜像（backend 双路由 + serve-worker 容器）。
   原地重部署不翻钥（7/5 实证 compose_hash 变但钥不翻）；仍先 pre → test 走完整流程。
3. runner CVM：不动。
4. 部署完成瞬间：全员 fence 应为 resident → 行为与部署前完全一致（P3 验收点）。

### 7c. 容量

- 主 CVM 新增：serve-worker 常驻（Python 主进程 + MAX_WORKERS 个 slot 协程）+ 每并发
  turn 一个 spawn 的 turn_child 解释器。灰度期（个位数用户）估 <500MB。
- prod 主 CVM 有内存墙历史（glibc arena、无 swap、available<1000M 红线）。
  **P5 扩量前必须 resize 决策**（`phala cvms resize`），P3 起每日 `phala ssh -- free -m` 盯。

---

## 8. 阶段计划与门

| 阶段 | 动作 | 门（不过不进下一阶段） |
|------|------|------|
| **P0 开发** | §5 恢复 + §3 路由 + §4 控制器；本地 PG 全量测试 | V1 套 + V2 套 + 路由/reconciler 套全绿；「flip 中消息不丢」双向专项过 |
| **P1 pre 验证** | pre 部署双运行时（pre runner 临时恢复 V1 容器以测双跑）| 双测试号（一 resident 一 v2）全功能 E2E；双向 flip E2E 零丢失 |
| **P2 test 环境** | 同构部署，泡 ≥3 天 | test 用户默认 resident 无扰动；serve-worker 稳定 |
| **P3 prod 上线** | migration → 主 CVM 新镜像 + serve-worker；runner 不动 | 全员仍 V1 且行为不变；healthz + 路由巡检；fence 全量盘点 = resident |
| **P4 灰度** | allowlist 加 2-3 内部号 → 核对 → 扩到目标灰度集 | 每 canary：chat/proactive/记忆/MCP/图片 全功能核对；出问题移出名单即回 V1 |
| **P5 扩量** | 分批加名单；主 CVM resize | 内存 > 红线余量；V2 p95 达标 |
| **P6 全量** | 默认值翻 v2；reconciler 批量排空存量 | 全员 state=v2；V1 supervisor 空转 7 天无异常 |
| **P7 退役** | stop runner CVM（观察一周再删）；代码重放 `2b294a1f`（删 agent_runtime/、retirement 测试翻回、policy 回 `v2_only`）；删 allowlist 控制器 | 仓库回单运行时；runner CVM 下线 |

**回滚矩阵**：P4/P5 单用户回滚 = 移出名单（秒级）；P3 整体回滚 = 主 CVM 回退旧镜像
（V1 全程未动，新表无人读、留着无害）；P6 后回滚 = 名单反向 pin + reconciler 反排空。

---

## 9. 失败模式

| 场景 | 行为 |
|------|------|
| V2 池全死（heartbeat 断） | v2 用户 send 503 `workers_unavailable`；resident 用户不受影响。可紧急把 v2 用户移出名单回 V1 |
| V1 supervisor 死 | resident 用户 503 `supervisor_unavailable`；v2 用户不受影响（两错误码保持可区分） |
| reconciler 崩/leader 丢 | 名单停止推进，已切用户不受影响（fence 是持久真相，不依赖 reconciler 在线）；advisory lock 释放后新 leader 接续 |
| 用户卡 draining（排空不完成） | admin GET 对账视图可见；reconciler 重试 + 指数退避；超时告警人工介入 |
| allowlist 表读失败 | send 路径**不读该表**（只读 fence）→ 送信不受影响；仅 reconciler 暂停推进 |
| kill_switch 拉闸 | 只停 V2 turns（现语义），V1 与 Genesis 不受影响 |

设计不变量：**send 热路径只读 per-user fence（一次 strict 读），allowlist 表只被
reconciler 和 admin 面读** —— 名单表故障不能影响送信。

---

## 10. 测试

1. **恢复套**：V1 ~5000 行测试原样过（证明恢复保真）。
2. **共存契约**（替代 retirement 测试）：dual policy 下双分支接线完整；`v2_only` 下
   等价 pre 现状（退役路径的回归保障）。
3. **路由矩阵**：`(mode, state) × policy` 全组合的 send 行为表驱动测试。
4. **Reconciler**：单向/双向收敛、失败退避、leader 切换、坏用户不卡环。
5. **flip 消息不丢**（P0 门）：flip 瞬间并发 send，恰好一条回复、零双跑，双向。
6. **lane 一致性**：state=v2 用户不出现在 V1 roster/wake 派发；反向亦然。
7. **E2E（P1）**：pre 双测试号全功能 + 双向 flip。
8. 基线：`python -m pytest tests/ -q --ignore=tests/test_api.py
   --ignore=tests/e2e_model_api_test.py`（PG 127.0.0.1:55432），对齐 5006+ 基线。

---

## 11. 风险与开放问题

1. **共享文件三方合是工作量主体**（db.py / setup_core / config_store / chat_send_core），
   每处要读懂退役后 160 commit 的演化。P0 估 1-1.5 周。
2. **prod fence 初始状态实查**（P3 前）：盘点 prod 用户 `hosted_runtime_mode` blob 实际值，
   确认部署后全员判定落 resident；有脏值（如残留 db_action_v2 实验值）先清。
3. **`hosted_wake_runtime_v2_enabled` 的读点枚举**（plan 阶段）：确认 V1 wake 派发与 V2
   wake 入队各自的 gate 点，改为 fence 判定。
4. **主 CVM 内存**：turn_child spawn 解释器是 V1 runner 没有的内存形态；P5 前 resize。
5. **维护税**：共存期每个后端改动要考虑两条线——把共存窗口压在 4-6 周是明确目标，
   不让过渡态变长期态。
6. **pre 环境形态切换**：pre 现在 runner 上跑 serve-worker；P1 起切到主 CVM 内嵌形态，
   需一次性迁移 pre 的 compose 与 CI pin 脚本（`pin-runtime-release.sh` 的目标文件变了）。
7. **genesis 单一认领者**（P0 验证）：共存镜像里 genesis 只能有一个认领面——V2
   serve-worker 的队列 worker。确认 `backend/genesis/daemon.py`（retirement 改过 10 行）
   在新镜像下不会与 serve-worker 双认领同一 `genesis_import_jobs` 行；若 V1 血脉的
   daemon 认领路径仍活，须显式关闭其中一个。

---

## 12. 决策记录

| 决策 | 选择 | 依据 |
|------|------|------|
| 灰度控制 | DB allowlist 表，实时生效 | 用户拍板；避开 env-deploy-注入坑 |
| 切换方向 | 双向可回滚 | 用户拍板 |
| V2 部署位 | 主 CVM 内嵌（backend 同镜像） | 用户提出；V1 零改动 + enclave 内网直连 + 退役边界干净 |
| V1 来源 | test 当前版（非退役前快照） | 含 c6ae9a61 隔离加固 |
| 路由真相 | per-user fence（resident/draining/v2 + generation） | 现存 shipped 机制，send 热路径不读名单表 |
| 消息不丢 | 复用 PR-A `chat_append_and_enqueue` + expected_runtime_mode + generation fence | 已验证机制，不新造 |
| Genesis | 共存期全量 V2 | 队列 worker 与 resident CLI 无依赖 |
| 终局 | 重放 `2b294a1f` | 退役 commit 是现成已验证模板 |
