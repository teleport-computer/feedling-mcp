# Model API 多配置 —— iOS 接口文档

对应 iOS PR [feedling-mcp-ios#76](https://github.com/teleport-computer/feedling-mcp-ios/pull/76)（`codex/model-api-profiles-debug`）。

后端已把「用户的 model API 配置」从单条 JSON blob 换成两张表，并暴露 8 条新端点。本文档描述**实际实现**的契约（逐字取自 `backend/hosted/setup_core.py` / `setup_routes_asgi.py` / `backend/db.py`），并给出 iOS 侧的映射与改动清单。

认证：所有端点走 `X-API-Key`（或 `Authorization: Bearer`，或旧版 `?key=`）。

---

## 概念模型（与 iOS 一一对应）

后端的两张表正是 iOS 已经建好的两层结构：

| 后端 | iOS |
|---|---|
| `model_api_credentials` 一行 | `ModelAPICredentialReference` |
| `model_api_routes` 一行 | `ModelAPIRouteProfile` |

- **credential** = 一把 provider API key（含 provider、label、base_url、密文信封、hint）
- **route** = 一个 (credential × model) 组合，带 `is_active` / `test_status` / `reasoning_effort`

**同一个 provider 可以存多把 key**（个人的、团队的），这正是 `credentialList` 那个「选已有凭据」UI 的用武之地。数据库层没有 `(user_id, provider, base_url)` 唯一索引。

**每个用户至多一条 active route**，由 Postgres 的 partial unique index 强制，不是靠代码自觉。

---

## 端点

### `GET /v1/model_api/routes` —— 列出全部 route

```json
{
  "active_route_id": "3f9c…",          // 无 active 时为 null
  "routes": [
    {
      "id": "3f9c…",
      "credential_id": "a71e…",
      "provider": "anthropic",
      "model": "claude-sonnet-4-5",
      "credential_label": "Anthropic Key A",
      "api_key_hint": "sk-a…451",
      "base_url": "",
      "supports_responses": false,
      "reasoning_effort": "high",       // "" 表示未设置
      "context_window_tokens": 128000,  // 该精确 route 的保守下限
      "is_active": true,
      "test_status": "ok",              // untested | ok | failed
      "last_test_at": "2026-07-10T08:12:03Z",   // "" 表示从未测过
      "last_test_error": "",
      "last_runtime_error": "",
      "last_runtime_error_class": "",
      "created_at": "2026-07-09T…",
      "updated_at": "2026-07-10T…"
    }
  ]
}
```

**响应中绝不含 `api_key_envelope`。** 服务端只以密文持有 provider key，且只有 TDX enclave 能解。`db.model_api_routes_list()` 在 SQL 层就不 SELECT 那一列，测试里有断言钉死。

### `POST /v1/model_api/routes` —— 新建 route

```jsonc
{
  "provider": "anthropic",
  "model": "claude-haiku-4-5",

  // ↓ api_key 与 credential_id 二选一，多给少给都是 400
  "api_key": "sk-ant-…",        // 新建一把凭据
  "credential_id": "a71e…",     // 复用已有凭据

  "base_url": "",               // 仅 openai_compatible 需要
  "label": "Anthropic Key A",   // 仅新建凭据时用；默认取 provider 名
  "reasoning_effort": "off",    // off | low | medium | high | 正整数字符串
  "context_window_tokens": 128000, // 未审计模型/自定义中转必填；必须是已核实的保守下限
  "activate": true              // 建完立刻激活（走同步测活）
}
```

- 给 `credential_id` 时，`provider` / `base_url` **以该凭据为准**，payload 里的会被忽略。
- 给 `api_key` 时**总是新建**一条 credential —— 同 provider 允许多把 key。
- `context_window_tokens` 不是厂商宣传的最大值，而是这条
  `(provider, model, base_url)` 确认可用的保守下限。已审计的官方模型族或部署
  override 会自动解析并持久化；任意未知 OpenRouter 模型和
  `openai_compatible` 自定义中转若不提供该值，会在 provider I/O 和写库前返回
  `400 prompt_context_limit_unconfigured`，不会等到第一次聊天才失败。
- 不带 `activate` → 返回 `{"route": {…}}`，新 route 处于 `untested` 且非 active。
- 带 `activate: true` → 等价于建完立刻调 activate（含同步测活），返回与 activate 相同。

失败：

| 情况 | 状态 | slug |
|---|---|---|
| 两者都给 / 都不给 | 400 | `api_key_or_credential_id_required` |
| `credential_id` 不存在 | 404 | `credential_not_found` |
| 无法建信封（缺 content pubkey / enclave 不可达） | 409 | `cannot_encrypt_provider_key` |
| `reasoning_effort` 非法 | 400 | `invalid_reasoning_effort` |

### `POST /v1/model_api/routes/{route_id}/activate` —— 切换生效

**这个端点会先同步测活，通过了才切换。**

```json
{ "active_route_id": "3f9c…", "route": { …同 GET /routes 的单条… } }
```

失败时**旧的 active route 纹丝不动**：

| 情况 | 状态 | body |
|---|---|---|
| route 不存在/不属于该用户 | 404 | `{"error": "route_not_found"}` |
| 上游测活失败 | 400 | `{"error": "provider_test_failed", "detail": "…", "status_code": 401}` |

> **为什么必须先测活**：agent-runner 的 roster 只收 `is_active AND test_status = 'ok'` 的用户。激活一条没测过的 route，用户会在下一个 15 秒 tick 从 roster 消失，supervisor 会杀掉他的 consumer 且不会自愈。所以测不过就不给切。
>
> 测活失败时该 route 会被标记 `test_status = "failed"`，UI 可以据此显示。

**切换会触发托管 agent 的 respawn**（provider/model/key 任一变化都会），最长 15 秒生效。正在处理中的那条消息不会丢，会被重新投递给新的 consumer。

### `POST /v1/model_api/routes/{route_id}/test` —— 单测一条 route

```json
{ "status": "ok", "route": { … } }
```

结果回写该 route 的 `test_status` / `last_test_at` / `last_test_error`。

**测的是当前 active route 且失败了：与 `DELETE /routes/{id}` 对称，后端会自动接管**。
该 route 被标成 `test_status = "failed"` 并同时被清掉 `is_active`（不再悬空），然后
自动接管 `updated_at` 最新的那条 `test_status = "ok"` 的另一条 route（同
`DELETE /routes/{id}` 的接管规则）。400 响应体里带上新的 `active_route_id`：

```json
{ "error": "provider_test_failed", "detail": "…", "status_code": 429, "active_route_id": "8b2d…" }
```

没有候选时 `active_route_id` 为 `null`——此时用户没有生效配置，托管 agent 会停，这是
合法结果而不是错误。**测的若不是 active route**，失败只回写 `test_status`，不触发
接管，响应体里也不会出现 `active_route_id` 键。

> 罕见的 DB 写瞬时失败：接管前会重新读一次真实的 active route，而不是盲信清 flag
> 那步的返回值。如果清 flag 没落库，这条失败的 route 仍是 `is_active=TRUE`，此时
> 不会 autoselect（避免撞 unique index），`active_route_id` 会如实回这条仍然生效
> 的失败 route 的 id——不是新接管的那条。这种情况下客户端应视为「本次没接管成
> 功」，可重试测试或稍后再查一次 `GET /routes`。

> 为什么要这样：agent-runner 的 roster 只收 `is_active AND test_status = 'ok'` 的
> 用户。如果失败的 active route 保持 `is_active=TRUE`，用户会在下一个 tick 从
> roster 消失、supervisor 杀掉 consumer 且**不会自愈**——仅仅因为用户主动点了一次
> 「测试连接」、上游恰好一次瞬时 429/超时。

失败：404 `route_not_found` / 400 `provider_test_failed`（可能带 `active_route_id`，见上）。

### `POST /v1/vision/main/test` —— 验证当前主模型是否真的能看图

请求无 body。它不按模型名猜能力，只采用 provider 返回的显式模态字段；catalog 没有
提供可判定字段时，才发送两张随机色块图做真实探测。

Model API 路径同步返回 `200`，`status` 为 `ok / unsupported / failed / untested`，并
始终带当前的 `source / provider / model`；失败时还可能带稳定 `error_code`、
`retryable / status_code / detail`。

resident/VPS 路径启动隔离、隐藏的双图 probe 并返回 `202 testing`；客户端随后轮询
`GET /v1/vision/config`，直到 `effective_status` 不再是 `testing`。probe 不进入聊天记录、
推送、摘要、Live Activity 或 capture。旧 resident 不具备该 side-channel 时返回
`409 vision_resident_update_required`。probe 回传端点位于 `/v1/internal/**`，不是公开 API。

精确 provider/model 的缓存 verdict（包括 `unsupported`）只通过
`GET /v1/vision/config` 的 `main_model.vision_test_status / effective_status` 提供给客户端
展示提示，不参与发送门禁。`follow_main` 图片继续走当前主模型；已选择 dedicated route
时仍固定走该 route。所有 `untested / testing / failed / unsupported` 状态都进入真实调用，
最终回合结果以 provider 的真实响应为准。

Runtime V2 的真实图片回合若收到明确的 text-only provider 拒绝（稳定分类为
`vision_model_required`），后端会把该回合捕获的 active provider/model route 写为
`unsupported`。这只更新后续 `GET /v1/vision/config` 的提示信号，不重试、不改路由，
也不改变已接受回合的终态结果；route 已切换或已有更新时，旧失败不会覆盖新配置。

### `DELETE /v1/model_api/routes/{route_id}` —— 删除 route

```json
{ "status": "deleted", "active_route_id": "8b2d…" }
```

删的若是 active route，后端**自动接管** `updated_at` 最新的那条 `test_status = "ok"` 的 route，新 id 在 `active_route_id` 里返回。没有候选时返回 `null`（此时用户没有生效配置，托管 agent 会停）。

失败：404 `route_not_found`。

### `GET /v1/model_api/credentials` —— 列出全部凭据

```json
{
  "credentials": [
    {
      "id": "a71e…",
      "provider": "anthropic",
      "label": "Anthropic Key A",
      "base_url": "",
      "api_key_hint": "sk-a…451",
      "supports_responses": false,
      "route_count": 2
    }
  ]
}
```

列出该用户**全部** credential，不只是被某条 route 引用的那些——`route_count`
统计有多少条 route 引用它，可以是 `0`。

这是必需的：iOS 此前只能靠对 `GET /routes` 的 `credential_id` 去重来拼凑凭据
列表，一把 route_count 为 0 的凭据（例如用户删掉了它最后一条 route）就永远不
出现在那份列表里——拿不到它的 id，`DELETE /credentials/{id}` 也就永远调不到它，
密文会一直留在库里。这个端点独立于 routes 表查询，让这类凭据也可见、可删。

**响应中绝不含 `api_key_envelope`**，与 `GET /routes` 同样的保证。

**删除一条 route 不会自动删掉它的凭据**——用户可能想留着这把 key 以后复用。有了
这个端点，那把没有 route 引用的凭据就可见、可通过 `DELETE /credentials/{id}` 手动删掉了。

### `PATCH /v1/model_api/credentials/{credential_id}` —— 改名 / 换 key

```jsonc
{ "label": "Team Key", "api_key": "sk-ant-new…" }   // 两者至少给一个
```

- 只改 `label` → 不联系 provider，直接改。
- 换 `api_key` → **若该凭据拥有当前 active route，会先拿新 key 对那条 route 同步测活**。测不过就整体不落库（旧 key、旧 `test_status` 全部保留），返回 400 `provider_test_failed`。测通过才写入，并把该凭据下**非 active** 的 route 全部标回 `untested`。

成功：`{"status": "ok"}`

失败：404 `credential_not_found` / 400 `nothing_to_update` / 400 `provider_test_failed` / 409 `cannot_encrypt_provider_key` / 500 `model_api_credential_write_failed`

### `DELETE /v1/model_api/credentials/{credential_id}` —— 删除凭据

```json
{ "status": "deleted", "active_route_id": "8b2d…" }
```

级联删除该凭据派生的所有 route。若其中含 active route，按上面的规则自动接管。

失败：404 `credential_not_found`。

---

## 保持不变的端点（旧版 App 无感）

`POST /v1/model_api/setup` 保持原路径和兼容字段，并新增可选
`context_window_tokens`，语义为幂等 upsert：

- 若当前 active route 的 credential 的 `(provider, base_url)` 与请求匹配 → 更新那把 key
- 否则 → 新建一条 credential
- 然后 upsert route、测活、激活

所以旧版 App 反复 setup 同一套配置**不会堆积 route**。`GET /v1/model_api/get` 继续返回 active route 的扁平投影：

```json
{ "config": { "configured": true, "provider": "anthropic", "model": "claude-sonnet-4-5",
              "base_url": "", "api_key_hint": "sk-a…451", "test_status": "ok",
              "last_test_at": "…", "last_test_error": "", "created_at": "…",
              "updated_at": "…", "privacy_mode": "tdx_cvm_backend_runtime_option_a",
              "reasoning_effort": "high", "context_window_tokens": 128000 } }
```

`reasoning_effort` **仅在设置过时出现**；`context_window_tokens` 在新建 route
以及已审计 route 上返回。无 active route 时 `config` 是 `{"configured": false}`。

`POST /test`、`POST /driver`、`DELETE /delete`、`GET /runtime`、`GET /key_envelope` 契约同样不变。

---

## iOS 侧映射

`ModelAPIRouteProfile` ← `GET /routes` 的 `routes[]` 单条：

| iOS 字段 | 后端字段 | 备注 |
|---|---|---|
| `id` | `id` | UUID 字符串 |
| `credentialID` | `credential_id` | |
| `provider` | `provider` | 与 `ModelAPIProvider.rawValue` 一致 |
| `model` | `model` | |
| `credentialLabel` | `credential_label` | |
| `apiKeyHint` | `api_key_hint` | 已是 mask（`sk-a…451`） |
| `baseURL` | `base_url` | |
| `status` | `test_status` | `ok`→`.ready`、`failed`→`.failed`、`untested`→`.untested` |
| `issueText` | `last_runtime_error_class` ?? `last_runtime_error` ?? `last_test_error` | 与现有 `modelAPIConfigIssueText` 的优先级一致 |
| `source` | — | 恒为 `.server` |

`activeRouteID` ← 响应顶层的 `active_route_id`。

> **顺带修好一个 iOS 侧的死代码**：`ModelAPIConfig.lastRuntimeError` 此前恒为 `nil` —— 因为 `last_runtime_error` 只在 `GET /v1/model_api/runtime` 返回，从来不在 `GET /get` 里。所以 `modelAPIConfigIssueText` 的 runtime 分支永不命中。现在 `GET /routes` 直接带上了这个字段，`issueText` 能真正工作了。

## iOS 要改的三处

1. **`refresh()`** —— 改调 `GET /v1/model_api/routes`，直接得到 `routes[]` + `active_route_id`。不再需要把 `GET /get` 的单条 config 包装成 `serverRoute(from:)`，也不再需要 `ModelAPIRouteProfile.serverRouteID` / `serverCredentialID` 那两个占位 UUID。

2. **`select(_:)`** —— 那句 `// The release backend currently returns only one route, so there is nothing else to switch to` 可以删掉了。改调 `POST /v1/model_api/routes/{id}/activate`。

   注意这个调用**会走一次真实的上游测活**，可能耗时数秒（取决于 provider）。UI 要给 loading 态，并处理 400 `provider_test_failed`（把 `detail` / `status_code` 映射成用户可读文案）。成功后用响应里的 `active_route_id` 更新本地状态。

3. **`save(_:)`** —— 两个选择：
   - 继续用 `setupModelAPI(...)`（`POST /setup`）。它现在是幂等 upsert，行为正确，但**只能操作 active route 的那条 credential**，无法为同一 provider 新建第二把 key。
   - 改用 `POST /v1/model_api/routes`（带 `activate: true`），并在 `draft.credentialID` 命中已有凭据时传 `credential_id` 而非 `api_key`。这才能真正支撑 sheet 里「选已有凭据 / 新建凭据」那两条路径。

   `ModelAPIConfigurationDraft` 已经带了 `credentialID` / `activateAfterSave`，正好对上 `POST /routes` 的 `credential_id` / `activate`。

`ModelAPIDebugStore` 那套 `UserDefaults` 里的本地 route 可以整个删掉了 —— 后端现在是真的多 route。

---

## 错误 slug 速查

新增的都已登记在 `docs/API_ERRORS.md`。iOS 需要为这些补本地化文案：

| slug | HTTP | 何时 |
|---|---|---|
| `route_not_found` | 404 | route id 不存在或不属于该用户 |
| `credential_not_found` | 404 | credential id 同上 |
| `api_key_or_credential_id_required` | 400 | `POST /routes` 的 `api_key` / `credential_id` 必须且只能给一个 |
| `nothing_to_update` | 400 | `PATCH /credentials` 两个字段都没给 |
| `model_api_credential_write_failed` | 500 | DB 写失败（罕见） |
| `model_api_route_write_failed` | 500 | DB 写失败，或 route 被并发删除 |

已有的 `provider_test_failed`（400，带 `status_code`）、`cannot_encrypt_provider_key`（409）、`invalid_reasoning_effort`（400）语义不变。
