# Model API 多配置（credential + route）后端设计

> **RETIRED / DO NOT DEPLOY.** Historical design; supervisor/roster sequencing
> is not part of current Runtime V2 operations.

日期：2026-07-10
配套前端：`teleport-computer/feedling-mcp-ios` PR #76（`codex/model-api-profiles-debug`）

## 背景

iOS PR #76 已经把「保存多把 API key、选其中一个生效」的 UI 与数据模型做完了，但它是纯前端 mock：DEBUG 下多条配置存在 `UserDefaults`（`ModelAPIDebugStore`），Release 下 `refresh()` 只从 `GET /v1/model_api/get` 拿到唯一一条 `source == .server` 的 route，`select()` 直接 `return` —— 代码注释写明「backend currently returns only one route... until it exposes route IDs」。

后端现状：每用户一条 `user_blobs(user_id, kind='model_api')`，整份配置是 JSONB `doc`，主键天然唯一，换 provider = 覆盖式重写。没有任何 profile 概念。原始 provider key 从不入库，只存 `api_key_envelope`（用户 content pubkey + enclave content pubkey 双向 X25519 信封，服务端解不开，只有 TDX CVM enclave 能解）。

本设计给后端补上 route 列表、route id 与激活语义。

## 前端已确定的两层模型

PR #76 引入两层概念，后端照搬：

- `ModelAPICredentialReference` = provider + label + apiKey + baseURL（一份**凭据**）
- `ModelAPIRouteProfile` = credentialID + model + status（一条**路由**）

一个 credential 可派生多条 route（同一把 Anthropic key，一条跑 Sonnet、一条跑 Haiku）。全局只有一个 `activeRouteID`。sheet 里「选已有凭据」那一步的 UI 已经写好。

## 决策记录

1. **分两张表**，不塞 `user_blobs` 的 JSONB 数组。
2. **activate 时同步测活**，测不过返回 400，旧 active 不动。
3. **`reasoning_effort` 下沉到 route**（原为 per-user，见 commit `374191e`）。
4. **`POST /v1/model_api/setup` 改为幂等 upsert**，另加集合端点；旧版 App 无感。

### 为什么分表而不是 JSONB 数组

- **并发正确性**：blob 方案里增删改一条 route 是对 JSONB 的 read-modify-write。ASGI 迁移后线程池激活了同用户竞态（memory 的 15 个写点最终靠 RLock 锁内重读才补上）。两台设备同时加 route 就丢写。分表后每条 route 是单行 INSERT/UPDATE，问题不存在。
- **「每用户最多一条 active」由 DB 强制**，靠 partial unique index，而非代码自觉。
- **级联删除免费**：`routes.credential_id → credentials ON DELETE CASCADE`，删 credential 自动清掉派生的 route。JSON 数组里这个级联要手写。
- **per-route 的 `test_status` / `last_test_error` / `last_runtime_error` 是行上的列**，不必 patch JSON 数组的某个元素。

代价是四条读侧里有三条要改（见「读侧改造」）。

## 数据模型

```sql
CREATE TABLE model_api_credentials (
  id                 UUID PRIMARY KEY,
  user_id            TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  provider           TEXT NOT NULL,
  label              TEXT NOT NULL,
  base_url           TEXT NOT NULL DEFAULT '',
  api_key_envelope   JSONB NOT NULL,
  api_key_hint       TEXT NOT NULL DEFAULT '',
  supports_responses BOOLEAN NOT NULL DEFAULT FALSE,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, id)
);

-- 刻意 NOT 加 (user_id, provider, base_url) 唯一索引：iOS 支持同一 provider、
-- 同一 base_url 下存多把不同的 key（ModelAPIConfigurationSheet 的 credentialList
-- 列出同 provider 的多个凭据供选择；debug samples 里就是两把 OpenAI key）。
-- setup 的幂等改由代码锚定 active credential，见「端点契约 › POST /setup」。

CREATE TABLE model_api_routes (
  id                       UUID PRIMARY KEY,
  user_id                  TEXT NOT NULL,
  credential_id            UUID NOT NULL,
  model                    TEXT NOT NULL,
  reasoning_effort         TEXT,
  is_active                BOOLEAN NOT NULL DEFAULT FALSE,
  test_status              TEXT NOT NULL DEFAULT 'untested',
  last_test_at             TIMESTAMPTZ,
  last_test_error          TEXT,
  last_runtime_error       TEXT,
  last_runtime_error_class TEXT,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

  FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
  FOREIGN KEY (user_id, credential_id)
    REFERENCES model_api_credentials (user_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX model_api_routes_one_active
  ON model_api_routes (user_id) WHERE is_active;
CREATE UNIQUE INDEX model_api_routes_uniq
  ON model_api_routes (credential_id, model);
```

`test_status` 取值：`untested` | `ok` | `failed`。
`reasoning_effort` 为 NULL 表示继承全局 env 默认（`FEEDLING_AGENT_REASONING_EFFORT`，默认 `off`）；取值沿用 `setup_core._normalize_reasoning_effort`（`off` / `low` / `medium` / `high` / 正整数字符串）。

**`routes.user_id` 是刻意的冗余**：partial unique index `(user_id) WHERE is_active` 需要它，且能让 users 的 CASCADE 直接挂上。

**复合外键 `(user_id, credential_id) → (user_id, id)`** 让数据库保证「route 不可能引用另一个用户的 credential」。跨用户串号只靠代码把关迟早出事。

`supports_responses` 归 credential，因为它是中转站 `/v1/responses` 的能力，与 `base_url` 绑定，不随 model 变。

销号：两张表的 `ON DELETE CASCADE` 已经覆盖，但仍须把表名加进 `db.delete_user_data`（`backend/db.py:2575`）的冗余兜底清单——本仓库有过销号漏删 per-user 表的历史（见 `0012_per_user_cascade_fk`）。

## 读侧改造

新增共享入口 `config_store.load_active_route(store)`，返回 active route 连同它的 credential。三处读侧收敛到它，第四处不动。

### 1. `db.list_agent_runtime_enabled_users`（`backend/db.py:1260`）

原生 SQL，形状几乎不变，driver 的 `CASE` 原样搬过来（须继续与 `hosted/agent_runtime_cutover.driver_for_provider` 保持同步）：

```sql
SELECT r.user_id,
  CASE LOWER(c.provider)
    WHEN 'anthropic' THEN 'claude'
    WHEN 'claude'    THEN 'claude'
    WHEN 'deepseek'  THEN 'claude'
    ELSE 'codex'
  END AS driver,
  LOWER(c.provider), r.model, c.base_url,
  c.supports_responses, COALESCE(r.reasoning_effort, '')
FROM model_api_routes r
JOIN model_api_credentials c ON c.id = r.credential_id
WHERE r.is_active
  AND r.test_status = 'ok'
  AND LOWER(c.provider) = ANY(%s)
ORDER BY r.user_id
```

### 2. `config_store._load_runtime_provider_config`（`backend/hosted/config_store.py:311`）

改成读 active route，拿它 credential 的 `api_key_envelope` 去 enclave 解密。`runtime_token` / `api_key` 的分支逻辑（`decrypt_kwargs = {"runtime_token": …} if runtime_token else {}`）原样保留。

调用方无需改动：`hosted/chat_send_core.py:60`、`hosted/setup_core.py:239`、`:376`、`hosted/history_import.py:2942`、`genesis/plaintext.py:1222`。

### 3. `GET /v1/model_api/key_envelope`（`backend/hosted/setup_core.py:221`）

返回 **active credential** 的 envelope。`supervisor._fetch_key_envelope`（`backend/agent_runtime/supervisor.py:615`）一行不用改。

### 4. `litellm_gateway` —— 不动

它从 supervisor 的 roster 拿参数（`build_model_entry(user_id, provider, model, base_url, supports_responses, reasoning_effort)`），roster entry 的字段名保持不变。`_resolve_reasoning_effort` 的 per-user override 自然变成 per-route override。

### 另：`record_runtime_error`

`config_store.record_runtime_error`（`backend/hosted/config_store.py:211`）现在 patch 到 `model_api_runtime` blob，改成写 active route 行的 `last_runtime_error` / `last_runtime_error_class`。该 blob 里的 rollout flags 等其余字段不动。

## 端点契约

```
GET    /v1/model_api/routes
POST   /v1/model_api/routes
POST   /v1/model_api/routes/{id}/activate
POST   /v1/model_api/routes/{id}/test
DELETE /v1/model_api/routes/{id}
PATCH  /v1/model_api/credentials/{id}
DELETE /v1/model_api/credentials/{id}
```

路由定义放 `backend/hosted/setup_routes_asgi.py`，实现体放 `backend/hosted/setup_core.py`（framework-neutral），沿用现有分层。全部走 `Depends(require_auth)`。

### `GET /routes`

响应字段对齐 iOS `ModelAPIRouteProfile`，让前端少写映射：

```json
{ "active_route_id": "…",
  "routes": [{
    "id": "…", "credential_id": "…",
    "provider": "anthropic", "model": "claude-sonnet-4-5",
    "credential_label": "Anthropic Key A", "api_key_hint": "sk-a…451",
    "base_url": "", "reasoning_effort": "",
    "test_status": "ok", "last_test_at": "…", "last_test_error": "",
    "last_runtime_error": "", "last_runtime_error_class": ""
  }] }
```

iOS `serverRoute(from:)` 已有 `test_status → ModelAPIRouteStatus` 映射与 `issueText` 优先级（runtime error 优先于 test error）。`source` 恒为 `.server`。

**顺带修一个 iOS 侧的死代码**：`last_runtime_error` 目前只在 `GET /v1/model_api/runtime`（`setup_core.py:336`）返回，**不在 `GET /get` 里**，所以 `ModelAPIConfig.lastRuntimeError` 恒为 nil，PR #76 里 `modelAPIConfigIssueText` 的 runtime 分支永不命中。新的 `GET /routes` 直接带上该字段即可治好。

### `POST /routes`

```
{provider, model, base_url?, api_key?, credential_id?, label?, reasoning_effort?, activate?}
```

`api_key` 与 `credential_id` **二选一**：前者新建 credential（走 enclave 建信封），后者复用已有的（对应 iOS sheet 的「选已有凭据」）。两者都给或都不给 → 400。

`activate: true` 则建完立刻走 activate 流程（含同步测活）。

provider 校验沿用 `provider_client.validate_config`（`backend/provider_client.py:223`）：合法集合 `{openai, openrouter, anthropic, gemini, deepseek, openai_compatible}`；`openai_compatible` 必须带 `base_url`；`base_url` 必须 `https://` 或 `http://127.0.0.1`；官方 provider 空 base_url 回填默认。

建信封失败（用户未注册 content pubkey 或 enclave attestation 不可达）→ 409 `cannot_encrypt_provider_key`，与现有 `setup_core.py:81` 一致。

### `POST /routes/{id}/activate`

1. 同步测活（复用现有 `model_api_test` 逻辑）。失败 → 400 `provider_test_failed`（带 `status_code`），**旧 active 纹丝不动**，不写库。
2. 通过 → 回写该 route 的 `test_status='ok'` / `last_test_at`，并在**一个事务内用两条语句**完成切换（先清旧 active，再置新 active）：

```sql
BEGIN;
-- 先确认目标存在且属于该用户，否则下面那条「清旧 active」会在目标不存在时
-- 照样把用户的 active route 清掉（客户端发个陈旧 route_id 就能打停自己的 agent）
SELECT 1 FROM model_api_routes
 WHERE user_id = %(uid)s AND id = %(route_id)s FOR UPDATE;   -- 无行 → 直接返回，不写入
UPDATE model_api_routes SET is_active = FALSE, updated_at = now()
 WHERE user_id = %(uid)s AND is_active AND id != %(route_id)s;
UPDATE model_api_routes SET is_active = TRUE, updated_at = now()
 WHERE user_id = %(uid)s AND id = %(route_id)s;
COMMIT;
```

> ⚠️ **不要写成单条 `SET is_active = (id = %s) WHERE is_active OR id = %s`。** 那个写法基于「唯一索引在语句末检查」的假设，而这对 Postgres 的**非 DEFERRABLE** partial unique index **不成立**——同一条 UPDATE 内索引维护是逐行进行的，行处理顺序不确定；只要目标行先于旧 active 行被处理，中途就会同时存在两条 `is_active=true`，当场抛 `duplicate key violates model_api_routes_one_active`。这是个依赖物理行序的随机失败，已用最小复现证实。partial unique **index** 也无法声明为 DEFERRABLE（只有 unique **constraint** 可以，而 constraint 不支持 WHERE 子句）。
>
> 两条语句在同一事务内对旧 active 行加行锁，并发 activate 会在该行上排队。不变量始终由 DB 保证：并发下最多一条成功，落败者撞唯一索引回滚并返回 `False`。

**释放 in-flight claim 不在这里做** —— 见下。

## 释放 in-flight reply claim（在 supervisor 里，不在 activate 端点里）

`_spawn_identity`（`backend/agent_runtime/supervisor.py:988`）的签名包含 `provider` / `model` / `base_url` / `provider_key` / `driver`，切 route 必然触发 `supervisor.py:275` 的 `config changed → kill + spawn`。消息本身不会丢（`chat/service.py:72` 的 lost-turn redelivery backstop 明确覆盖 `respawn re-seed` 场景，窗口 3600s、每 poll 最多 5 条），但被 kill 的旧 consumer 已 claim 的那条消息要等 `CHAT_POLL_CLAIM_TTL_SEC`（默认 **600 秒**）过期才会被重投。主动释放 claim 能让新 consumer 下一个 poll 就接手。

**但释放的时机必须是「旧 consumer 确实死了之后」，而不是 activate 请求处理时。**

`activate` 端点返回时旧 consumer 还活着：supervisor 要等自己的 tick（`AGENT_TICK_INTERVAL_SEC`，默认 15s）才发现配置变了，再 SIGTERM + 最多 `_KILL_GRACE_SEC = 3.0` 秒 grace 才 SIGKILL。在这 15–18 秒窗口里清掉 claim，正在跑那个回合的旧 consumer 会与新 consumer 双跑同一条消息 —— `chat/service.py:66-70` 明确写着 `CHAT_POLL_CLAIM_TTL_SEC` 设成 600s 就是为了防这个，因为「已回复的 409 只挡住重复写入，挡不住重复计费」。**用户会被 provider 计费两次。**

所以释放放在 `supervisor.py` 的 respawn 分支里，`kill_fn` 之后、`spawn_fn` 之前：

```python
self.kill_fn(child["pid"])          # 旧 consumer 已死
freed = db.chat_expire_reply_claims(user_id)
if freed:
    wake_bus.notify("chat", user_id)   # 让所有 app worker 失效缓存
pid = self.spawn_fn(entry, user_id, home)
```

**缓存失效是必需的，不是可选的**：`chat/service.py:388` 的 `_pending_chat_messages_for_poll` 遍历 per-worker 内存里的 `store.chat_messages`，`_chat_message_claimable`（`service.py:344-351`）在到达 `db.chat_try_claim_reply` 的 DB CAS **之前**就用缓存里的 `reply_claimed_by` 把消息滤掉了。只清 DB 行是彻底的 no-op。`wake_bus.notify("chat", uid)` 在接收方 worker 上会走到 `core_store._evict_store()` → `store.reload()`（`core/store.py:894-906`），缓存才真正刷新。

supervisor 是独立进程，`wake_bus._dispatch` 的 same-origin 自过滤（`wake_bus.py:85-86`）不会拦它，所以**所有** app worker 都会收到广播 —— 这比在 activate 端点里做更干净（那里发起广播的 worker 会过滤掉自己的通知，还得额外显式 `store.reload()`）。

tick 间隔默认 15s（`AGENT_TICK_INTERVAL_SEC`），所以切换后最长 15 秒生效。

`_enqueue_introduction` 有 `_needs_introduction_identity` 把关（`supervisor.py:599`），切模型不会重复发自我介绍。

inline 聊天路径每次请求现读 DB，切换立即生效，不受 respawn 影响。

### 为什么 activate 必须 gate 在测活上

`list_agent_runtime_enabled_users` 的 SQL 有 `AND test_status = 'ok'`，它决定谁进 roster。若允许激活一条 `untested` 的 route，下一个 tick 该用户从 roster **消失**，supervisor 走「用户离开 roster」分支杀掉 consumer，且不会自己回来——用户点一下切换，agent 就没了。

### `PATCH /credentials/{id}`

```
{label?, api_key?}
```

改 `api_key` → 重建信封、更新 `api_key_hint`，该 credential 下所有 route 的 `test_status` 退回 `untested`。

**但若该 credential 有 active route**，退回 `untested` 会让用户当场掉出 roster。所以此时必须同步测活那条 active route；测不过 → 400 且**不落库**，保留旧 key。这与 activate 的 gate 是同一条规则。

只改 `label` 不触发测活。

### `DELETE /routes/{id}`

删的若是 active route：若还有别的 `test_status='ok'` 的 route，自动激活 `updated_at` 最新的那条；否则该用户变成无 active（consumer 会停），响应里 `active_route_id: null`，由 iOS 提示。

### `DELETE /credentials/{id}`

CASCADE 带走它的所有 route。若其中含 active route，按上一条规则重新选主。

### `POST /v1/model_api/setup`（改为幂等 upsert）

保持路径与请求体不变（`provider`, `model`, `base_url`, `api_key`, `reasoning_effort`），语义改为：

1. **锚定 active credential** 做幂等（不是靠唯一索引）：若当前 active route 的 credential 的 `(provider, base_url)` 恰好与请求匹配，就更新那条 credential 的 key / hint / `supports_responses`；否则新建一条 credential。`supports_responses` 仍由 setup 期对中转站 `/v1/responses` 的能力探测得出，写在 credential 上。
2. 按 `(credential_id, model)` upsert route —— 命中则复用（更新 `reasoning_effort`），否则新建。这一条**有**唯一索引兜底。
3. 同步测活 + 激活。

```python
active = load_active_route(store)
if active and active["provider"] == provider and active["base_url"] == base_url:
    credential_id = active["credential_id"]
    update_credential_key(credential_id, envelope, hint, supports_responses)
else:
    credential_id = insert_credential(...)
route_id = upsert_route(credential_id, model, reasoning_effort)
mark_test(route_id, "ok"); activate(route_id)
```

旧版 App 只有一条 credential，反复 setup 同一套配置必然命中 active 分支 → 不堆积。新 UI 要存同 provider 的第二把 key 时走 `POST /routes`（显式带 `api_key`），不经过这里。

已知代价：旧版 App 在 provider 之间来回切换会残留旧 credential（低频，且 `DELETE /credentials/{id}` 可清）。这是不加唯一索引换来的，可接受。

旧版 App 反复 setup 同一套配置**不会堆积 route**，且 `GET /get` 继续返回 active route 的扁平投影 —— 旧版 App 完全无感。iOS PR #76 的 `save()` 现在就是调 `setupModelAPI(...)`，几乎不用改。

响应保持 `{"status": "configured", "config": {…脱敏…}, "warnings?": […]}`。

### 保持不变的端点

下列端点的**请求/响应契约不变**，但实现一律改为读新表（而非 `model_api` blob）：

- `GET /v1/model_api/get` —— 由 active route + 其 credential 现场构造扁平投影；无 active 时返回 `{"configured": false}`。
- `POST /v1/model_api/test` —— 无 route id 参数，语义仍为「测当前 active route」；结果回写该 route 行。
- `POST /v1/model_api/driver` —— driver 由 active route 的 credential.provider 派生。
- `DELETE /v1/model_api/delete` —— 删光该用户所有 credential，CASCADE 带走 routes。
- `GET /v1/model_api/runtime`、`POST /v1/model_api/runtime_error`、`POST /v1/model_api/memory/repair`。

`GET /runtime` 的 `last_runtime_error` 改从 active route 行读取（原读 `model_api_runtime` blob）。该 blob 的其余字段（rollout flags、`last_action_trace_*`）继续留在 blob 里。

## 迁移与部署顺序

`db.list_agent_runtime_enabled_users` 这份代码同时存在于 app CVM 和 runner CVM 两个镜像里。**前提：两者同时部署**（已确认），因此不需要新旧 SQL 并存，也**不做双写**。

### ⚠️ 迁移必须先于代码镜像上线

新的 `list_agent_runtime_enabled_users` JOIN 两张新表。如果新镜像在 `alembic upgrade head` 之前启动，这条 SQL 会因表不存在而抛异常 —— 而该函数的 `except` 块**返回 `[]`**（`db.py` 的读函数惯例）。supervisor 拿到空 roster，走「用户离开 roster」分支，**杀掉全站每一个 consumer**，且不会自愈。爆炸半径是全部托管用户，直到迁移跑完为止。

正确顺序：**先 `alembic upgrade head`，确认两张表存在且回填完成，再滚新镜像。**

### 部署 skew 的两个方向（若未能同时 bump）

- **新 runner + 旧 app**：旧 app 的 `setup` 仍写 blob、不建 route 行；新 runner 读新表 → 在这个窗口里重新配置的用户拿到 `model_api_not_configured`，且从 roster 消失。
- **旧 runner + 新 app**：新 app 写新表、不碰 blob；旧 runner 读陈旧 blob → 在这个窗口里**轮换过 key** 的用户，其 agent 仍被喂旧 key，每个回合都上游鉴权失败。

只有在窗口内改动配置的用户受影响（prod 用户量很小），但两个方向的坏法不同。同一个部署窗口内 bump 两个镜像。

**第 1 步 — 迁移 `0014_model_api_profiles`**（`backend/alembic/versions/`，`down_revision = "0013_genesis_resident_claim"`）

建两张表，从 `user_blobs` 的 `model_api` blob 回填：每个用户一条 credential + 一条 active route。`test_status` 沿用旧值；`reasoning_effort` 从 blob 顶层搬进 route；`label` 取 provider 名首字母大写。

回填只针对 `doc ? 'api_key_envelope'` 的行（没有信封的历史残留跳过）。迁移须幂等（重跑不重复插入）：credentials 的插入用 `NOT EXISTS (SELECT 1 FROM model_api_credentials c WHERE c.user_id = b.user_id)` 守卫（该用户还没有任何 credential 时才回填），routes 的插入用 `ON CONFLICT (credential_id, model) DO NOTHING`。

**`model_api` blob 原样保留，但新代码既不读也不写它。** 保留它只为回滚：若新镜像上线后需退回老镜像，老代码读 blob 仍能跑。代价是回滚窗口内用户在新版所做的配置变更会丢失，退回迁移时刻的快照——prod 用户量小，可接受。

**第 2 步 — 新后端代码**

读写全部走新表。`model_api_runtime` blob（rollout flags、`last_action_trace_*`）继续存在，只是 `last_runtime_error` / `last_runtime_error_class` 两个字段的写侧移到 route 行。

**第 3 步 — 收尾（独立 PR，稳定运行一段时间之后）**

单独出一个迁移删掉 `model_api` blob。在此之前它是无人问津的只读快照。

## 测试

**必须起真 PG**。`conftest` 的 `collect_ignore` 在没有数据库时会**静默跳过** DB 相关模块且不报 skipped，「全绿」是假象。用 docker 起 PG 在 55432，真实基线是 2440 passed / 7 个 pre-existing 红。

要覆盖的行为：

- 并发两次 activate，只有一条胜出，且始终恰有一条 `is_active`。
- 跨用户 `credential_id` 被复合外键拒绝。
- activate 一条 `untested` 的 route → 400，且旧 active 不变、用户仍在 roster 里。
- `PATCH /credentials` 换 key 时 active route 测活失败 → 400，旧 key 保留，`test_status` 不变。
- 删除 active route → 自动接管 `updated_at` 最新的 `ok` route；无候选时 `active_route_id: null`。
- `POST /setup` 反复提交同一套配置 → 不堆积 route。
- 回填迁移幂等：连跑两次结果一致。
- `list_agent_runtime_enabled_users` 只返回 `is_active AND test_status='ok'` 的用户。
- 销号：删 users 行后两张表清空。

## 范围外

- iOS 侧改动（另一个仓库；`select()` 与 `save()` 需接上新端点）。
- route 级的 provider 熔断/退避（proactive 坏钥重试风暴是独立问题）。
- 「上次使用时间」排序、route 重命名等 UI 便利功能。
