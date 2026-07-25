# 前端错误契约（后端 ↔ iOS）

状态：v1.1 设计稿，2026-07-07。
每处标注 **[已上线]**（现在就能对接）/ **[Phase A/B/C]**（后端排期中，字段形状以本文为准，先做不会白做）。

---

## 一、全景：一个错误怎么到达用户

后端错误走**三条通道**到 iOS，按「用户在不在场」分工：

| 通道 | 什么时候出现 | 举例 | 状态 |
|---|---|---|---|
| ① **同步 HTTP 错误**（§三） | 用户主动操作**当场失败**——接口返回非 2xx | 保存 Key 失败、上传文件过大 | `error` slug 已上线；增强字段 Phase A |
| ② **通知中心**（§四） | 用户**不在场**时后台出的事，打开 App 才看到 | 蒸馏半夜失败了、记忆整理连续失败在退避、AI 进程起不来 | Phase B/C |
| ③ **聊天 system 气泡**（§五） | **对话回合**失败，插在聊天流里就地解释 | 发消息后 AI 连不上上游 | 已上线（待合并部署） |

另有**场景内状态字段**（§六）：各页面就地读取的持久状态（如设置页的「最近一次连接失败原因」）。

### 端到端例子（真实 prod 案例改写）

用户的 API 中转站余额耗尽（剩 ¥0.018，一次调用要 ¥0.02）：

1. 用户发消息 → AI 回合失败 → **聊天里**出现兜底话术 +（紧跟）system 气泡：「⚠️ 你的 API 服务额度不足，充值后再发消息即可恢复。详情: 403 …预扣费额度失败…」（通道③）
2. 同时**设置页** provider 区显示 `last_runtime_error`（§六），**通知中心**出现一条活跃通知 `chat / quota_insufficient`（通道②）
3. 半夜后台记忆整理也连续失败进退避 → 通知中心**再多一条** `memory / memory_backoff`（不会因为重试 25 次而刷出 25 条——同一问题只有一条，计数累加）
4. 用户充值后再发消息 → 回合成功 → 设置页错误清空，通知中心两条都变「✓ 已恢复」

### iOS 交付清单（按此对账工作量）

- [ ] **slug 本地化表**：`slug → 中文文案`（§三的目录 + §四的 error_class；现在就能开工）
- [ ] **通用错误弹窗/横幅组件**：按 §二的渲染纪律消费任意错误（含未知 slug 兜底、request_id 复制入口）
- [ ] **通知中心页**：轮询 `GET /v1/notices`，活跃/已恢复分区（§四）
- [ ] **聊天 system 气泡渲染**：`role=="system"` 居中灰字样式（§五）
- [ ] **设置页字段**：`last_runtime_error`（已可做）、`last_test_error`（Phase B）

---

## 二、总渲染纪律（三条通道共用）

所有错误都用同三个字段判读：

| 字段 | 含义 | iOS 处理 |
|---|---|---|
| `error`（HTTP）/ `error_class`（通知） | **稳定 slug**，程序判读面。后端承诺不改名（改名=breaking，会记录在本文档） | 本地化表的 key；**未知 slug 显示通用文案 + slug 原文**（新后端错误在老版 App 上仍可诊断，不要丢弃） |
| `blame` | 归责方向（下表） | 决定文案能不能给行动指引 |
| `detail` | 上游原始报错/调试信息（已截断） | 默认折叠，「详情」展开；不要当主文案 |

**blame 分类——本地化文案必须遵守的纪律**：

| blame | 含义 | 文案方向 | 禁止 |
|---|---|---|---|
| `user_provider` | 用户自己的 API 服务问题（额度/key/模型名） | 给明确行动指引：「充值」「重新保存 Key」 | — |
| `provider_transient` | 上游临时问题（限流/5xx/超时） | 「稍等会自动恢复」 | 让用户改配置 |
| `user_environment` | 用户自己的自托管运行环境问题（VPS resident consumer 过旧/离线/未接任务） | 给明确行动指引：「更新并重启 resident consumer」「把维护提示发给 agent」 | 说成模型服务商问题或 Feedling 后端故障 |
| `system` | 我们的问题 | 「系统出了点问题，我们会尽快处理」 | **绝不能**引导用户充值/改 key（有用户白折腾的真实案例） |

客户端必须把未知 `blame` 当作默认/`system` 桶处理，不得因为 enum 不认识而丢弃整条错误或通知；主展示仍以 `error_class`/`user_text` 为准。

---

## 三、通道①：同步 HTTP 错误信封

所有非 2xx 响应的 body：

```json
{
  "error":      "model_api_key_decrypt_failed",   // 现状已有，全后端 4xx/5xx 都带
  "blame":      "system",                          // [Phase A] 可选
  "detail":     "…调试信息…",                       // [Phase A] 可选
  "request_id": "req_a1b2c3d4"                     // [Phase A] 见下
}
```

- 新字段全部 **optional 解码**。
- `request_id`：`internal_error`（500 兜底）的 body **必带**；其余 5xx 是
  **尽力而为**——正常中间件路径下响应头 `X-Request-Id` 会回带，但 body 内不保证
  一定有这个字段（视具体路由是否走了统一 helper 而定）。**系统错误类弹窗请提供
  复制入口**——优先取响应头，兜底取 body 字段；用户报障给我们这个 id，后端能
  直接对账日志。

### 状态码约定

| 码 | 含义 | 无本地化文案时的通用处理 |
|---|---|---|
| 400 | 参数/校验错误（body 校验失败 = `invalid_payload`，`detail` 带字段列表） | 「请求无效」 |
| 401 | 未认证/凭证失效 | 走重新认证流程 |
| 402 | 上游付费问题 | 「API 服务额度问题」+ 引导设置页 |
| 403 | 无权限 | 「没有权限」 |
| 404 | 不存在 | 「不存在或已删除」 |
| 409 | 状态冲突（如 `already_answered`） | 一般静默/重拉状态 |
| 413 | 上传过大 | 「文件过大」 |
| 429 | 限流 | 「操作太频繁，稍后再试」 |
| 500 | 未知服务端错误 | 「系统出错」+ request_id 复制入口 |
| 502 | 上游依赖失败（enclave/模型服务） | 「服务暂时不可用，稍后自动恢复」 |
| 503 | 过载（`service_busy`） | 同上，可退避自动重试 |

### 需要本地化文案的 slug 目录（按域分组；未列出的走通用文案）

> Phase A 会把残存的自由文本错误（如 `envelope missing fields: [...]`）收敛成 slug + detail，届时更新本表。

- **认证/账号**：`unauthorized`(401) · `forbidden`(403) · `user_not_found` · `account_not_found` · `no_recoverable_account` · `invalid_or_expired_challenge` · `challenge_failed` · `token_expired` · `token_already_used` · `invalid_token`
- **model_api / provider 配置**（设置页）：`model_api_not_configured` · `model_api_not_tested` · `model_api_config_invalid` · `model_api_key_decrypt_failed`(blame=system) · `model_api_key_envelope_missing` · `model_api_runtime_profile_missing`（feat/upstream-error-surfacing 合入后生效） · `provider_not_configured` · `provider_not_hostable` · `hosting_runtime_unavailable`
- **聊天**：`already_answered`(409,静默) · `message_not_found` · `user_message_envelope_failed` · `bootstrap_incomplete`(引导未完成)
- **导入/蒸馏**：`job_not_found` · `missing_file` · `empty_file` · `payload_too_large` · `archive_failed` · `archive_unavailable`
- **通用**：`invalid_payload` · `not_found` · `not_owned` · `service_busy` · `service_unavailable` · `invalid_image` · `internal_error`(500 兜底)

---

## 四、通道②：通知中心 `GET /v1/notices` [已上线，原 Phase B/C]

### 这是什么

一条「通知」= **一个用户应该知道的、正在发生（或已恢复）的系统问题**。后端各子系统在失败点写入：已分类、已归责、已配好话术、**已去重**（同一问题重复发生只更新计数，不新增条目）、有**恢复标记**（问题好了会告诉你）。

> 它不是后端内部错误日志的转发——内部日志是排查面，iOS 只消费这个端点。

### 拉取（快照式，无游标）

```
GET /v1/notices?include_resolved=<bool, 默认 true>
认证：同其它 v1 端点（X-API-Key）
```

每次返回**全量快照**：全部未恢复的活跃通知 + 近 7 天内已恢复的。量级极小（去重后 per-user 几十条封顶），每次全量替换本地缓存即可。

```json
{
  "notices": [
    {
      "notice_id":   "ntc_9f2c…",
      "source":      "genesis",          // 哪个子系统：genesis|history_import|memory|runner|chat|model_api
      "error_class": "quota_insufficient",
      "blame":       "user_provider",
      "severity":    "error",            // error=红/问题态, warning=黄/降级
      "user_text":   "入住蒸馏失败：你的 API 服务额度不足（第 3 次尝试）",
      "detail":      "403 … 预扣费额度失败 …",
      "copyable_prompt": "可选：需要用户转发给 agent 的修复提示",
      "dedupe_key":  "genesis:job_ab12", // 同 key 始终只有一条
      "occurrences": 3,                  // 重复发生次数
      "first_ts":    1783300000.1,
      "last_ts":     1783300400.5,       // 最近一次发生
      "resolved":    false,
      "resolved_ts": null
    }
  ]
}
```

### 渲染规则

1. 文案：本地化表按 `(source, error_class)` 映射 → **映射不到用 `user_text` 兜底**（它含动态内容如失败次数，永远可显示）。
2. 分区：`resolved=false` 进「活跃」（按 `last_ts` 倒序）；`resolved=true` 进「已恢复」（可折叠）。
3. 未读红点：本地记 `max(last_ts, resolved_ts)` 水位，比较即知有无新变化（服务端不存已读态）。
4. 点击跳转按 `source`：见下表。
5. 轮询：App 前台 30–60s 或跟随现有状态轮询节拍。
6. **首版没有推送**——通知只在 App 打开时可见（后续可能对 severity=error 加推送，另行约定）。

```
通知中心示意：
● 活跃 (2)
│ ⚠ 记忆整理暂停中（已失败 8 次）      memory · 2 小时前更新
│ ✖ 入住蒸馏失败                      genesis · 额度不足 · [详情▸]
○ 已恢复 (1)
│ ✓ AI 连接失败 → 已恢复              chat
```

### 首批会出现的通知 [Phase C]

| source | error_class | 场景 | 点击跳转 |
|---|---|---|---|
| `genesis` | `genesis_failed` + 上游类（`quota_insufficient`/`rate_limited`/…） | 入住蒸馏失败 | 蒸馏进度/重试页 |
| `genesis` | `genesis_partial`（warning） | 蒸馏部分成功、丢了部分卡片 | 同上 |
| `history_import` | `import_failed` / `import_stale` | 聊天记录导入失败/卡死 | 导入页 |
| `memory` | `memory_backoff` | 记忆整理连续失败进入退避 | 设置页 provider 区 |
| `runner` | `runner_spawn_failed` / `runner_key_decrypt_failed` | AI 进程起不来（用户表现为「永远没回复」） | 设置页 |
| `runner` | `runner_degraded`（warning） | AI 在跑但部分能力受损 | 设置页 |
| `model_api` | `responses_unsupported`（warning，blame=user_provider） | openai_compatible 中转不支持 `/v1/responses`，codex 工具循环被桥接 mangle→记忆/工具静默不可靠（回合仍 rc=0，AI 嘴上说调工具却从不真调）。**setup 探测时发**，非回合失败时 | 设置页 |
| `chat` | `resident_consumer_stale`（warning，blame=user_environment） | 自托管 resident consumer 仍在 poll，但 commit 缺失/不匹配，或 resident-only 蒸馏任务迟迟没被 claim。通知可带 `copyable_prompt`，同时可能有一条 `source=resident_maintenance` 的维护消息进入聊天 | 聊天/设置页 |
| `chat` | `resident_decrypt_source_unavailable`（warning，blame=user_environment） | resident 明确报告 `degraded`/`unconfigured`/`unreachable`。通知可先于维护消息出现；老用户仅在持续失败超过宽限期后收到维护消息 | 聊天/设置页 |
| `chat` | `resident_decrypt_health_unreported`（warning，blame=user_environment） | resident 缺少有效、近期的 decrypt-health 上报。仅显示通知，不注入解密修复消息 | 聊天/设置页 |
| `genesis` | `resident_never_claimed`（error，blame=user_environment） | resident-only 入住/记忆蒸馏 job 超过 reaper 阈值仍无人 claim，已失败 | 蒸馏进度/重试页 |
| `chat` | 上游类全套（§五的列表） | 对话回合失败（与 system 气泡同源双写） | 聊天 |

---

## 五、通道③：聊天 system 气泡 [已上线，待合并部署]

对话回合失败时，聊天流里紧跟兜底话术出现一条技术说明消息：

- 识别：`/v1/chat/history` 与 `/v1/chat/poll` 消息中 `role == "system"` 且 `notice_kind == "upstream_error"`。
- 渲染：居中灰字样式（遵循 DESIGN.md 令牌）。**旧版兼容**：消息同时带 `sender: "assistant"`，没做 system 渲染的旧版显示成普通气泡，不会崩。
- 正文服务端已组好（`⚠️ <话术>\n详情: <原文截断>`，加密信封正常解密），**iOS 无需再判读**，直接显示。
- 语义：不算对用户消息的回复（不影响已回复状态）、不推送（兜底话术那条已推过）、失败重试期间不会重复出现（服务端已做排他与去抖）。

回合错误的 error_class 全集（同时会双写进通知中心）：
`quota_insufficient` · `auth_invalid` · `model_not_found` · `rate_limited` · `upstream_unavailable` · `turn_timeout` · `reply_parse_failed` · `unknown`

计划新增（Phase C 同批；老版按未知 slug 兜底即可）：
- `provider_incompatible`（user_provider）——上游不兼容请求格式（如部分中转/xAI 拒收工具 schema）
- `context_overflow`（user_provider）——上下文超限，指引清理会话/换模型
- `content_filtered`（provider_transient）——上游安全过滤拒答，换个说法重试

---

## 五之二、通道④：回合失败挂在消息上 [2026-07-18 新增]

通道③解决的是「有没有一条技术说明」，这条解决的是「**这一轮到底成没成**」。
兜底话术是 agent 口吻的正常回复，从数据上看这轮是「已回复、成功」——用户看到的
是一句「我这会儿有点慢」，不知道真实原因。本通道让失败**显示成失败**。

设计见 `docs/superpowers/specs/2026-07-18-provider-error-visibility-design.md` §2。

### 双载体（两个都要读）

| 载体 | 出现在哪 | 承担 |
|---|---|---|
| **兜底回复消息**（权威） | 该 agent 消息自身带 `turn_failure_*` + `reply_to_message_id` | **实时事件**。它是新消息、有新 ts，能通过 `since` 增量过滤 |
| 用户消息 metadata（冗余） | 该 user 消息带 `reply_error_class` / `reply_blame` / `reply_user_text` | 全量 history / 重启后恢复 |

**为什么必须双载体**：`/v1/chat/history?since=` 按消息**原始 ts** 过滤，对用户那条
旧消息就地更新 metadata 不产生新 ts，永远进不了增量流。只读 metadata 的话，实际
效果是「杀掉 App 重开才看得到失败态」。

**冲突时以兜底消息为准**：`update_chat_message_metadata` 仅在 parent 存在于当前
worker 内存时才落库，跨 worker 可能静默写失败。

### 字段

| 字段 | 值域 | 说明 |
|---|---|---|
| `turn_failure_error_class` / `reply_error_class` | 见通道③的 error_class 全集 | 非空即表示这轮是兜底糊的、不是真回复 |
| `turn_failure_blame` / `reply_blame` | `user_provider` \| `provider_transient` \| `system` | 决定渲染，见下 |
| `turn_failure_user_text` / `reply_user_text` | ≤500 字 | 服务端已组好的用户可见文案，**直接显示，不要本地映射** |

**blame 与 user_text 由服务端按 `error_class` 查 `notices/catalog.py` 下发，不采信
poster 提交的值**（`chat_core._turn_failure_attribution`）。理由是归责红线：透传意味着
一个写错的 consumer 能把我们自己的故障标成 `user_provider`、让用户白跑一趟改配置。
未知 `error_class` 一律落 `system`——宁可我们背锅，也不误导用户。客户端因此可以信任
这两个字段，无需再做合法性判断。
| `reply_to_message_id` | 消息 id | 兜底消息指向的用户消息，客户端靠它配对 |

**不下发 detail**：原始上游报错可能夹带 provider HTML、request id 乃至敏感上下文。
排障走设置页 `last_runtime_error` 与 admin 面。

### 渲染规则（显示矩阵）

| blame | 用户消息 | 兜底气泡 | 行动入口 |
|---|---|---|---|
| `user_provider` | 失败态 + `user_text` | **隐藏** | 「去设置」→ 模型配置页 |
| `provider_transient` | 失败态 + `user_text` | **隐藏** | 无（会自愈） |
| `system` | 不变 | **保留显示** | 无（我们的锅，留住有温度的兜底话术） |

**不给重试按钮**：余额不足时重试无效，用户需要的是充值。

### 归并规则（分页边界）

配对必须在**每次**消息列表变更后重跑（增量、全量、加载 older），不能只在实时收到
新消息时做。若兜底事件已加载而它指向的用户消息还在上一页，**不得隐藏该兜底气泡**
——否则失败原因会连兜底一起消失；等加载 older 后再归并。

### 兼容

新增字段是 additive：不认识这些字段的旧版 App 渲染结果与改动前完全一致
（兜底话术照显）。

---

## 六、场景内状态字段（页面就地显示）

| 端点 | 字段 | 状态 |
|---|---|---|
| `GET /v1/model_api/runtime` | `last_runtime_error` / `last_runtime_error_class`——最近一次回合失败原因，成功后自动清空 | **[已上线（待合）]** 设置页 provider 区 |
| `GET /v1/model_api/get` | `test_status` / `last_test_at` | [已上线] |
| `GET /v1/model_api/get` | `last_test_error`——连接测试失败原因（重启 App 后仍可见） | **[Phase B]** |
| `GET /v1/genesis/imports/{job_id}` | `status` / `error` / `phase` | [已上线] |
| `GET /v1/history_import/status/{job_id}` | `status` / `error` / `background_error` / `warnings[]`（**建议展示 warnings**——丢卡等静默降级都在这里） | [已上线] |
| `GET /v1/onboarding/validate` | steps 附 `error` 细节 | **[Phase B]** |

---

## 七、版本兼容承诺

1. 所有新字段**增量可选**——老版忽略未知字段即可，无破坏。
2. slug 一经写入本文档即冻结；废弃走「新增新 slug、旧 slug 保留」。
3. 未知 slug / source / severity：显示通用文案 + 原文，不丢弃。

## 八、路线图对照

| 块 | 内容 | 状态 |
|---|---|---|
| 通道③ + `last_runtime_error` | §五、§六首行 | 后端已合并（`model_api_routes.last_runtime_error*`，见 `hosted/config_store.py`） |
| Phase A | 同步信封增强（blame/request_id/校验错误统一形状）+ slug 治理 | 设计定稿，待排期 |
| Phase B | `GET /v1/notices` + chat 双写 + §六场景字段补齐 | 已上线（`backend/notices/routes_asgi.py`；chat 双写 `turn_failure_error_class`） |
| Phase C | genesis / import / memory / runner 接入通知中心 + 新增 error_class | 已上线（error_class 目录见 `backend/notices/catalog.py`） |
| iOS | 交付清单见 §一 | 本文档即输入 |
