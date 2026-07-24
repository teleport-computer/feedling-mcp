# Agent-runner 多台独立扩展实施计划

> **【历史设计，禁止按本文部署】** 这里描述的是已退役的 per-user resident
> supervisor/consumer 拓扑。2026-07-18 起 hosted local/test/pre/prod 均只运行
> pooled Runtime V2 `serve-worker`；现行拓扑、容量与恢复步骤见
> `deploy/HOSTED_RUNTIME_V2_ROLLOUT.md`。

日期：2026-06-30

本文目标是把 API-key 用户的 hosted agent-runner 从“单 CVM 内一个独立
service”推进到“可横向扩到多台 Phala CVM / 多个 runner 容器”的生产形态。

配套背景：

- `docs/AGENT_RUNTIME_CC_CODEX_PLAN.zh.md`：agent-runner 总设计。
- `docs/HOSTED_MODEL_API_RETIREMENT_ROADMAP.zh.md`：API-key 用户统一到
  agent-runner 的迁移路线。
- `docs/AGENT_RUNTIME_ISOLATION.md`：per-user process/container 隔离 seam。
- `backend/agent_runtime/README.md`：当前代码现状。

## 目标

最终形态：

```text
main Phala CVM
  ingress
  backend
  enclave

runner Phala CVM / runner group
  agent-runner shard 0
  agent-runner shard 1
  ...

runner Phala CVM / runner group
  agent-runner shard N
  ...

shared
  Postgres
  object storage
```

核心性质：

- `agent-runner` 的死活不影响 `backend` 进程。
- runner CPU / OOM / CLI 卡死尽量不拖挂主服务 CVM。
- 多个 runner 可以并发服务不同 API-key 用户。
- 任意单个 runner 挂掉后，lease 过期，其他 runner 可接管。
- backend 不需要知道某个用户当前在哪台 runner 上。
- 长期 provider key 不落盘；仍通过 enclave JIT decrypt。
- 同一个用户同一时间最多一个 consumer 处理消息。

## 非目标

- 不在第一阶段做复杂动态调度器。
- 不让 backend 直连某个 runner RPC。
- 不把 Docker socket 暴露给 backend。
- 不默认启用 per-user Docker container；默认仍是一用户一 child process。
- 不为了扩容牺牲 provider key 的 JIT decrypt / 不落盘约束。

## 当前基础

现有代码已经具备这些基础：

- `deploy/docker-compose.phala.yaml` 中已有独立 `agent-runner` service。
- `backend/hosted/chat_routes_asgi.py` 的 `/v1/model_api/chat/send` 已收敛为：
  校验 provider / runner heartbeat，写入用户消息，然后等待 runner 回复或返回
  `processing`。
- `backend/agent_runtime/leases.py` 已用 `agent_runtime_instances` 做 per-user
  Postgres lease。
- `backend/agent_runtime/supervisor.py` 已支持 `FEEDLING_HOST_ALL`，可从 DB
  发现 model_api 配置为 `test_status=ok` 的用户。
- `FEEDLING_RUNTIME_TOKEN_SECRET` 路径已支持 supervisor mint per-user runtime
  token，consumer 用短期 token 访问 backend/enclave。

现有不足：

- host-all 发现是全量发现；多个 runner 会重复扫描全量用户。
- provider key 在抢到 lease 前就可能被 JIT decrypt，多个 runner 会重复解密。
- runner heartbeat 是单个全局 key，多个 runner 会互相覆盖。
- heartbeat 已从 supervisor 主循环中拆成独立线程，但仍写单个全局 key；这解决了
  “冷启动慢导致 heartbeat 变 stale”的问题，不解决“多 runner 互相覆盖”的问题。
- LiteLLM gateway reconcile 当前基于 roster，不保证只持有本 runner 已抢到的
  lease 用户。
- 已有 `AGENT_MAX_SPAWNS_PER_TICK` 限制每 tick 新 spawn 数，避免冷启动瞬间 fork
  过多；但还没有 per-runner `AGENT_MAX_CHILDREN`，一个 runner 长期仍可能持有过多
  child。
- 同一 CVM 内独立 service 只能隔离进程，不能隔离 CPU / memory 故障半径。

## 2026-06-30 当前代码进展

这轮代码已经补了几项多 runner 前的稳定性基础，文档后续阶段以这些为前提：

- `backend/agent_runtime/supervisor.py`
  - 新增 `AGENT_MAX_SPAWNS_PER_TICK`，默认每 tick 最多新起 8 个 consumer，`0`
    表示无限制。它是冷启动节流，不是总并发上限。
  - heartbeat 写入挪到独立 `_heartbeat_loop` 线程，避免 discover / JIT decrypt /
    spawn 慢时 backend wedge guard 误判 runner stale。
  - `_resolve_discovered()` 对单个用户的 mint / fetch / decrypt 异常做隔离，坏用户
    本 tick 跳过，不拖垮整张 roster。
- `backend/agent_runtime/litellm_gateway.py`
  - `openai_compatible` gateway entry 增加 `use_chat_completions_api=True`，强制
    LiteLLM 把 Codex 的 Responses 请求桥到 chat-completions relay。
- `tools/chat_resident_consumer.py`
  - memory capture / dream / migrate 使用 `call_agent(..., raw_text=True)`，绕过
    chat-bubble sanitizer，避免漂亮 JSON 被清洗后解析失败。
- `backend/proactive/routes_asgi.py`
  - memory maintenance jobs 和 introduction 一样按 pending status 恢复，绕过首次
    consumer watermark，避免 agent-runner 首次启动前创建的 capture/dream/migrate job
    永久 pending。
- `backend/bootstrap/gates.py`
  - model_api host route 在 `main_loop` 阶段不再硬要求
    `feedling-chat-resident` heartbeat，避免把 hosted agent-runner 用户误判成 resident
    consumer 未接通。

这些改动降低了单 runner host-all 冷启动和后台 job 卡死风险；但它们尚未提供
多台扩展所需的 shard、多实例 heartbeat、lease-scoped gateway secret 边界。

## 设计原则

### Backend 只投递，不调度

backend 继续只做事实源：

- 鉴权。
- 加密 chat 写入。
- bootstrap / delivery policy。
- heartbeat guard。
- chat poll / notify。

backend 不维护“user -> runner address”映射，也不向 runner 发 RPC。这样 runner
可以在任意 CVM 上，只要能访问 backend、enclave、Postgres。

### 分片减少竞争，lease 保证正确性

静态 shard 用于减少扫描、解密、spawn 竞争：

```text
stable_hash(user_id) % AGENT_RUNNER_SHARD_COUNT == AGENT_RUNNER_SHARD_INDEX
```

但 shard 不是正确性的唯一来源。正确性的最终边界仍是 Postgres lease：

- absent / expired / own lease 才能 acquire。
- 只有 lease owner 可以 renew / release。
- runner 崩溃后 TTL 到期，其他 runner 接管。

### Secrets 只在必要 runner 上出现

provider key 的 JIT decrypt 必须发生在：

1. 用户属于本 runner shard。
2. 本 runner 有容量。
3. 本 runner 成功 acquire 或即将 spawn 该用户。

避免所有 runner 都持有所有 gateway 用户的 upstream key。

## Phase 1：Shard 与容量上限

### 新增配置

在 `agent-runner` 环境变量中新增 shard 和总容量控制：

```yaml
AGENT_RUNNER_SHARD_INDEX: "${AGENT_RUNNER_SHARD_INDEX:-0}"
AGENT_RUNNER_SHARD_COUNT: "${AGENT_RUNNER_SHARD_COUNT:-1}"
AGENT_MAX_CHILDREN: "${AGENT_MAX_CHILDREN:-0}"
```

已有冷启动节流配置继续保留：

```yaml
AGENT_MAX_SPAWNS_PER_TICK: "${AGENT_MAX_SPAWNS_PER_TICK:-8}"
```

语义：

- `AGENT_RUNNER_SHARD_COUNT <= 1`：不分片，兼容当前行为。
- `AGENT_RUNNER_SHARD_INDEX`：当前 runner shard 编号，范围
  `[0, AGENT_RUNNER_SHARD_COUNT)`。
- `AGENT_MAX_CHILDREN <= 0`：不限制 child 数。
- `AGENT_MAX_CHILDREN > 0`：本 runner 最多运行这么多个 user consumer。
- `AGENT_MAX_SPAWNS_PER_TICK`：每 tick 最多新 spawn 的 consumer 数。它只摊平
  冷启动压力，不限制最终长期并发；因此不能替代 `AGENT_MAX_CHILDREN`。

### 代码改动

文件：`backend/agent_runtime/supervisor.py`

新增 pure helper：

```python
def _stable_user_shard(user_id: str, shard_count: int) -> int:
    ...

def _filter_shard(enabled: dict[str, dict], *, shard_index: int, shard_count: int) -> dict[str, dict]:
    ...
```

要求：

- hash 必须稳定，不用 Python 内置 `hash()`。
- 推荐 `sha256(user_id.encode()).digest()` 取整数。
- 输入异常时回到 shard_count=1 行为。

host-all 路径中，顺序必须调整为：

```text
_discover_enabled()
  -> _filter_shard()
  -> apply capacity budget
  -> _resolve_discovered()
  -> _effective_roster()
  -> sup.tick()
```

容量预算：

- 已在 `sup.children` 中的用户优先保留，不因为超额立即 kill。
- 对新增用户，只取剩余 capacity。
- 如果 `AGENT_MAX_CHILDREN=K` 且当前已有 K 个 child，不再 resolve/decrypt 新用户。
- 在 `AGENT_MAX_CHILDREN` 过滤后，再由已实现的 `AGENT_MAX_SPAWNS_PER_TICK`
  决定本 tick 实际新起几个。前者是总量控制，后者是速率控制。

### 测试

新增 / 扩展：

- `tests/test_agent_runtime_supervisor.py`

覆盖：

- shard_count=1 时不改变现有 roster。
- shard_count=2 时用户稳定分布。
- shard_index 越界时 fail closed 或回落到 0，并有日志。
- max_children 达到后不 spawn 新用户。
- 已运行 child 即使超过新 max，也不在本 tick 被强杀。
- 未进入本 shard 的用户不触发 provider key decrypt。

## Phase 2：多实例 heartbeat

### 问题

当前 heartbeat 写在 `server_config` 单 key：

```text
agent_runtime_supervisor_heartbeat
```

多个 runner 同时写会互相覆盖。backend 只能知道“最后一个写入者是否健康”，
不知道 runner 集群整体是否健康，也不知道 gateway 是否至少有一个实例开启。

当前代码已经把 heartbeat 写入从主 supervision loop 拆到独立线程。这是正确方向，
Phase 2 不需要回退这点；要做的是把独立线程写入的目标从“单全局 key”升级为
“per-owner instance heartbeat”。

### 数据模型

推荐新增表：

```sql
CREATE TABLE IF NOT EXISTS agent_runtime_supervisor_heartbeats (
  owner TEXT PRIMARY KEY,
  host TEXT,
  shard_index INTEGER NOT NULL,
  shard_count INTEGER NOT NULL,
  max_children INTEGER NOT NULL,
  active_children INTEGER NOT NULL,
  host_all BOOLEAN NOT NULL,
  gateway BOOLEAN NOT NULL,
  version TEXT,
  payload JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_runtime_supervisor_heartbeats_updated_idx
  ON agent_runtime_supervisor_heartbeats (updated_at);
```

保留旧 `server_config` heartbeat 一段时间作为兼容 fallback。

### DB API

文件：`backend/db.py`

新增：

```python
def set_supervisor_instance_heartbeat(owner: str, payload: dict) -> None:
    ...

def list_supervisor_instance_heartbeats(max_age_sec: float, *, now: float | None = None) -> list[dict]:
    ...

def prune_supervisor_instance_heartbeats(max_age_sec: float) -> None:
    ...
```

`set_supervisor_heartbeat()` 可以暂时继续写旧 key，避免一次性切换风险。

兼容写法建议：

```text
_heartbeat_loop()
  -> set_supervisor_instance_heartbeat(owner, rich_payload)
  -> set_supervisor_heartbeat(legacy_payload)  # transitional fallback
```

当 backend 新版本和 runner 新版本都完成部署后，再删除 legacy 写入 / 读取。

### Backend guard

文件：`backend/hosted/agent_runtime_cutover.py`

`check_supervisor_live(require_gateway=...)` 改成：

1. 优先读新多实例 heartbeat。
2. 只要存在新鲜实例满足：
   - `host_all == true`
   - 如果 `require_gateway`，则 `gateway == true`
   - `active_children < max_children` 或 `max_children <= 0` 或已有 active
     heartbeat 证明集群至少在跑
3. 没有新表数据时 fallback 到旧 `read_supervisor_heartbeat()`。

初版不需要精确判断“这个用户的 shard 是否有 live runner”。只要集群存在可用
runner，就允许写消息；具体是否有 consumer 由 runner poll + lease 处理。

后续可以增强为：

```text
target_shard = stable_hash(user_id) % shard_count
必须存在该 shard 的 live heartbeat
```

### 测试

新增 / 扩展：

- `tests/test_hosted_agent_runtime_cutover.py`

覆盖：

- 独立 heartbeat thread 写入 new table，主 tick 卡住时仍刷新。
- 无新 heartbeat 且旧 heartbeat live：兼容通过。
- 多个 heartbeat 中一个 stale、一个 live：通过。
- require_gateway=True 且所有 live gateway=false：503。
- host_all=false：503。
- DB 读取异常：保持当前 fail-open 策略。

## Phase 3：Late decrypt 与 lease-scoped LiteLLM

### 问题

当前 `_resolve_discovered()` 会在 roster 阶段为 discovered user fetch envelope
并 JIT decrypt provider key。多 runner 下，如果每个 runner 都全量 discover，
会造成重复 decrypt 和 key 扩散。

Phase 1 的 shard 能减少这个问题，但还不够。更好的边界是：

```text
只有本 runner 已 acquire lease 的用户，才进入 provider key decrypt + gateway config。
```

### 推荐重构

把 supervisor tick 拆成两段：

```text
1. Plan phase:
   discover enabled users
   shard filter
   capacity filter
   build lightweight entries: user_id, driver, provider, model, base_url

2. Acquire/spawn phase:
   for each lightweight entry:
     acquire lease
     fetch envelope
     decrypt provider key
     materialize home
     spawn child
     renew lease
```

当前 `Supervisor.tick(roster)` 假设 roster 已经带 provider key。可以选择两种
实现路径：

### 路径 A：小改

- 保留 `Supervisor.tick(roster)`。
- 在 host-all 下先 shard/capacity filter，再 `_resolve_discovered()`。
- `gateway_mgr.reconcile(gateways)` 改成只使用 `sup.children` 对应用户。

优点：改动小。  
缺点：provider key 仍在 acquire 前 decrypt，但 shard/capacity 已显著缩小范围。

### 路径 B：正确边界

- 给 `Supervisor` 新增 `prepare_entry_fn`：

```python
prepare_entry_fn(entry) -> dict
```

- `Supervisor.tick()` 在 acquire 成功后调用 `prepare_entry_fn`，这里再 fetch
  envelope / decrypt provider key / mint spawn token。
- `_spawn_identity()` 需要能处理 prepared entry。
- gateway reconcile 只基于当前 owned children 的 prepared entries。

优点：secret 边界最干净。  
缺点：改动较大，需要更细的单测。

建议一步直达采用路径 B。如果为了降低风险，可以先 merge 路径 A，再紧接路径 B。

### LiteLLM scope

文件：

- `backend/agent_runtime/supervisor.py`
- `backend/agent_runtime/litellm_gateway.py`

约束：

- gateway config 只包含本 runner 当前 lease owner 的 gateway 用户。
- 用户 lease release / child exit 后，下一次 reconcile 移除该用户路由。
- provider key rotation 不 bounce consumer，但必须更新 gateway config。
- `openai_compatible` 已需要 `use_chat_completions_api=True`，这是已落地行为；
  后续改 gateway scope 时必须保留，避免 Codex Responses 请求被 passthrough 到只支持
  chat-completions 的 relay。

### 测试

新增 / 扩展：

- `tests/test_agent_runtime_supervisor.py`
- `tests/test_litellm_gateway.py`

覆盖：

- 未 acquire lease 的 gateway 用户不进入 LiteLLM config。
- child release 后 gateway entry 被移除。
- provider key rotation 更新 gateway config。
- decrypt 失败时保留 last-good key，不 bounce 健康 child。
- openai_compatible 仍带 `use_chat_completions_api=True`，gemini/openrouter 不带。

## Phase 4：部署形态

### 同 CVM 多容器

适合第一轮验证：

```yaml
agent-runner-0:
  image: ghcr.io/teleport-computer/feedling-agent-runner:<digest>
  command: ["python", "-u", "backend/agent_runtime/supervisor.py"]
  environment:
    AGENT_RUNNER_SHARD_INDEX: "0"
    AGENT_RUNNER_SHARD_COUNT: "2"
    AGENT_MAX_CHILDREN: "4"
    AGENT_MAX_SPAWNS_PER_TICK: "2"
    ...

agent-runner-1:
  image: ghcr.io/teleport-computer/feedling-agent-runner:<digest>
  command: ["python", "-u", "backend/agent_runtime/supervisor.py"]
  environment:
    AGENT_RUNNER_SHARD_INDEX: "1"
    AGENT_RUNNER_SHARD_COUNT: "2"
    AGENT_MAX_CHILDREN: "4"
    AGENT_MAX_SPAWNS_PER_TICK: "2"
    ...
```

优点：

- compose 改动小。
- 验证多 runner lease / heartbeat / shard 逻辑。

缺点：

- CPU / memory 仍与主服务同 CVM 竞争。
- runner OOM 仍可能影响 ingress/backend。

### 独立 runner CVM

目标生产形态：

```text
main CVM:
  ingress/backend/enclave

runner CVM A:
  agent-runner-0
  agent-runner-1

runner CVM B:
  agent-runner-2
  agent-runner-3
```

要求：

- runner CVM 能访问 `FEEDLING_API_URL`。
- runner CVM 能访问 `FEEDLING_ENCLAVE_URL`。
- runner CVM 使用同一个 `DATABASE_URL`。
- runner CVM 注入同一个 `FEEDLING_RUNTIME_TOKEN_SECRET`。
- runner CVM 注入 `FEEDLING_LITELLM_API_KEY`。
- 重新审查 attestation / encrypted env / secret 注入边界。

注意：如果 `FEEDLING_ENCLAVE_URL` 暴露到 main CVM 外部，必须确认 runtime
token scope 和 TLS/attestation 约束足够，避免把 enclave decrypt 面扩大给不可信
调用者。推荐 runner CVM 仍在受控 Phala TDX 环境，并只注入最小必要 secret。

### Standby shard

静态 shard 下，如果 shard 0 所在 CVM 整体挂掉，只有同 shard 的备用 runner
能接管 shard 0 用户。推荐生产配置：

```text
runner A: shard 0/4, shard 1/4
runner B: shard 2/4, shard 3/4
runner C: standby shard 0/4, 1/4, 2/4, 3/4 各一个实例，AGENT_MAX_CHILDREN small
          或 failover 时再放开
```

更简单的第一版：

- 每个 shard 至少两个 runner 实例。
- lease 防止双跑。
- 两个实例都扫同一 shard，但 `AGENT_MAX_CHILDREN` 限制容量。

## Phase 5：运维与观测

### 必备指标

runner heartbeat payload 至少包含：

- `owner`
- `shard_index`
- `shard_count`
- `active_children`
- `max_children`
- `max_spawns_per_tick`
- `host_all`
- `gateway`
- `tick_duration_ms`
- `resolve_duration_ms`
- `spawned_this_tick`
- `last_tick_error`
- `version`

每个 child 需要能追踪：

- `user_id`
- `driver`
- `provider`
- `pid`
- `runtime_home`
- `lease_owner`
- `last_heartbeat_at`
- `last_active_at`
- `status`
- `error`

### 日志要求

必须打结构化日志：

- discover 用户数。
- shard 后用户数。
- capacity drop 用户数。
- acquire 成功 / 失败。
- spawn / respawn / kill。
- provider key decrypt 成功 / 失败，不能打印 key。
- gateway reconcile entry 数。
- heartbeat 写入失败。

### 后台诊断接口

可选新增 admin endpoint：

```text
GET /v1/admin/agent-runtime/supervisors
GET /v1/admin/agent-runtime/instances
```

只返回非秘密状态，用于排障。

## Phase 6：回滚策略

### 功能开关

保留这些全局开关：

- `FEEDLING_HOST_ALL`
- `FEEDLING_LITELLM_ENABLE`
- `FEEDLING_RUNTIME_TOKEN_SECRET`
- `AGENT_RUNNER_SHARD_COUNT`
- `AGENT_MAX_CHILDREN`

回滚路径：

1. 关闭新增 runner 实例。
2. 将 `AGENT_RUNNER_SHARD_COUNT=1`。
3. 保留单 runner。
4. 必要时关闭 `FEEDLING_HOST_ALL`，backend 对 hosted agent 返回 503 或切回未托管路径
   （取决于当时 legacy 是否已删除）。

### 数据库兼容

- 新 heartbeat 表只增不改核心用户数据。
- `agent_runtime_instances` 保持兼容。
- 旧单 heartbeat key 保留一段时间，确保新旧代码混跑时不 wedge。

## 验收清单

### 单元测试

- shard hash 稳定。
- shard filter 不改变 shard_count=1 行为。
- max_children 不超额 spawn。
- max_spawns_per_tick 只限制新 spawn 速率，不影响已有 child 续约。
- existing children 优先保留。
- multi heartbeat aggregate verdict 正确。
- gateway-required provider 在 gateway=false 时被 backend guard 拦下。
- 未 acquire lease 不 decrypt provider key。
- LiteLLM config 只包含本 owner 持有 lease 的用户。
- memory capture / dream / migrate 走 raw_text，不被 chat sanitizer 截断 JSON。
- memory maintenance jobs 可绕过首次 consumer watermark 被 poll 到。
- model_api host route 在 main_loop 阶段不被 resident heartbeat gate 误拦。

### 集成测试

- 启动两个 runner，同一个用户只被一个 runner spawn。
- kill runner A，TTL 后 runner B 接管用户。
- runner A 达到 `AGENT_MAX_CHILDREN` 后，runner B 继续接新用户。
- cold start 50 个 enabled users 时，每 tick spawn 不超过
  `AGENT_MAX_SPAWNS_PER_TICK`，heartbeat 仍保持 fresh。
- shard_count=2 时用户分布稳定，重启后不漂移。
- gateway 用户只出现在持有其 lease 的 runner LiteLLM config。
- heartbeat 中一个 runner stale 不导致全局 503。
- 所有 runner stale 时 backend 返回 `hosting_runtime_unavailable`，且不写 orphan
  user message。

### Phala 验证

- test CVM 同机多 runner 不再因全量 host-all 拉起空壳账号。
- 独立 runner CVM 掉线时 main CVM ingress/backend 仍健康。
- main CVM backend 重启后 runner 自动恢复 polling。
- enclave 短暂不可用时，新 spawn 失败但已有 child 不泄漏 provider key。

## 推荐 PR 拆分

### PR 0：单 runner 冷启动与后台 job 稳定性（已基本落地）

改动：

- `AGENT_MAX_SPAWNS_PER_TICK`。
- 独立 heartbeat thread。
- `_resolve_discovered()` per-user failure isolation。
- LiteLLM `openai_compatible` responses-to-chat bridge。
- resident consumer memory lanes `raw_text=True`。
- proactive memory-maintenance jobs watermark exempt。
- model_api host route bootstrap gate 修正。

验收：

- 单 runner host-all 冷启动不会因一次性 spawn 过多拖挂。
- heartbeat 不因主 tick 慢而 stale。
- 单个坏用户不拖垮整圈 roster。
- capture/dream/migrate 不因输出清洗而 JSON 解析失败。

### PR 1：Shard + max children

改动：

- `backend/agent_runtime/supervisor.py`
- `tests/test_agent_runtime_supervisor.py`
- compose env 示例。

验收：

- 单机多 runner 可以稳定分摊用户。

### PR 2：多实例 heartbeat

改动：

- 新 alembic migration。
- `backend/db.py`
- `backend/hosted/agent_runtime_cutover.py`
- `tests/test_hosted_agent_runtime_cutover.py`

验收：

- 多 runner 心跳不互相覆盖。

### PR 3：Late decrypt + lease-scoped gateway

改动：

- `backend/agent_runtime/supervisor.py`
- `backend/agent_runtime/litellm_gateway.py`
- `tests/test_agent_runtime_supervisor.py`
- `tests/test_litellm_gateway.py`

验收：

- provider key 只出现在持有该用户 lease 的 runner。

### PR 4：Phala 多 runner / 独立 runner CVM 部署

改动：

- `deploy/docker-compose.phala.yaml`
- `deploy/docker-compose.phala.test.yaml`
- `deploy/DEPLOYMENTS.md` 或新增 runner CVM 部署说明。

验收：

- test 环境跑至少 2 个 runner。
- kill 任一 runner，不影响 main backend。

## 最小上线版本

如果要尽快上线“可多台扩展”的第一版，最低必须包含：

1. 已落地的 `AGENT_MAX_SPAWNS_PER_TICK` + 独立 heartbeat thread。
2. shard filter。
3. `AGENT_MAX_CHILDREN`。
4. 多实例 heartbeat。
5. backend heartbeat aggregate guard。

Late decrypt / lease-scoped gateway 可以作为紧随其后的安全强化，但如果要直接
上独立多 CVM，建议不要跳过。

## 关键风险

- shard_count 调整会改变用户归属 shard，可能触发大量 respawn。需要灰度。
- runner 多实例同时启动时会集中访问 DB/enclave，必须有 capacity filter 和 backoff。
- LiteLLM gateway 如果仍全量 reconcile，会让 provider key 扩散到所有 runner。
- 独立 runner CVM 访问 enclave 会扩大 decrypt 调用面，必须依赖 runtime token scope
  和 Phala encrypted env。
- 旧 heartbeat fallback 不能长期保留为唯一判断，否则多 runner 健康状态不可观测。
