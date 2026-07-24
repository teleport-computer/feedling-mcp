# 托管回合上游报错透出（chat system 消息 + 设置页状态）

> **RETIRED / DO NOT DEPLOY.** Historical design; managed hosted execution is
> Runtime V2-only and has no resident supervisor path.

日期：2026-07-06
状态：设计定稿，待实现

## 背景

prod 案例 usr_0d16bfd42532f949（2026-07-05）：用户的中转站（api.dzzi.ai）余额不足，
上游 403「预扣费额度失败, 用户剩余额度: ¥0.018」→ codex 回合退出 1 → 用户只看到
通用兜底话术「我这会儿有点慢，刚刚没接上」。用户完全不知道要充值，重存了配置也没用，
最后放弃聊天。排查时真实报错只能从 prod 库 `user_logs` 的 `proactive_jobs` failed
条目里挖（见 memory `fallback-triage-via-proactive-jobs-status-reason`）。

同类历史案例：dded（上游 gcli2api 自身死）、codex 残留 config 打死端口——都是
「上游/配置有明确可判读的错，用户只见通用兜底」。

## 目标

失败发生时，把可判读的原因送到用户 App 的两个位置：

1. **聊天流里的 system 消息**（新 role="system"）：分类中文话术 + 原始错误摘要。
2. **设置页 provider 状态**：补上 agent-runner 路径写 `model_api_runtime.last_runtime_error`
   （读侧 iOS 已有，`backend/hosted/setup_core.py` `last_runtime_error` 字段）。

现有兜底话术**保留不动，两者都发**：agent 口吻的兜底保持人设温度 + 老版 App 兼容，
system 消息承载技术原因。

## 非目标

- **supervisor 层失败不覆盖**（provider key 信封解不开、consumer spawn 失败）：发生在
  consumer 起来之前，走不到本上报路径。这类用户表现为「完全没回复」而非兜底话术，
  有自己的心跳/lease 观测面。显式 out of scope。
- **iOS 渲染是独立仓的独立任务**：后端先行完全兼容（老版 App 对未知 role 不渲染
  也可接受，因为兜底话术照发）。本 spec 只约定线上契约。
- 不做推送通知、不做独立通知中心。

## 决策记录（brainstorm 结论）

| 决策点 | 结论 |
|---|---|
| 透出渠道 | 聊天 system 消息 + 设置页 provider 状态（不改兜底文案本身，不做推送） |
| 文案加工 | 分类话术 + 原始错误截断摘要 |
| 触发范围 | 前台聊天回合失败必发；后台任务（capture/dream/proactive）按错误类别去抖 |
| 与兜底关系 | 两者都发 |
| 传输形态 | chat_messages 新 role="system"（方案 C），接受老版 App 不显示的降级 |

## 架构

```
codex/claude 回合失败 (RuntimeError / TimeoutExpired / ValueError)
        │
        ├─ 错误分类器 classify_agent_error(exc) → (error_class, blame, 话术)
        │       │
        │       ├─ 腿① post_reply(role="system", notice_kind="upstream_error",
        │       │        suppress_push=True)  ← 前台必发 / 后台去抖
        │       │
        │       └─ 腿② POST /v1/model_api/runtime_error {error, error_class}
        │            ← 每次失败都写；成功回合写 {error: ""} 清空
        │
        └─ 兜底话术照发（现状不动）
```

全部改动落在 `tools/chat_resident_consumer.py`（分类器、两腿调用、去抖）和
backend（/v1/chat/response 收 role、新 runtime_error 瘦路由、history 透传）。

## 组件 1：错误分类器（consumer 内纯函数）

**输入**：异常对象（含 `call_agent` 抛出的 RuntimeError 文本、TimeoutExpired、
「no usable reply」ValueError）。
**输出**：`(error_class: str, blame: str, user_text: str)`。

### 错误来源三层（现有 `_cli_error_detail`，`tools/chat_resident_consumer.py:2175` 已汇聚，无需新增采集）

- **claude CLI**（`--output-format json`）：`is_error` result 文本 + `api_error_status`。
  覆盖 Anthropic wire：`credit balance is too low`、`invalid x-api-key`、429、529。
- **codex CLI**（`--json`）：`type=="error"` 事件 `message`（取最后一条）。覆盖：
  litellm gateway 错误透传（`litellm.APIError ...`）、直连 provider 原始错误、
  codex 自身错误（`stream disconnected`、`exceeded retry limit, last status: 429`、
  connection refused）。
- **兜底**：stderr（CLI 崩溃/bwrap 起不来）→ stdout 前 300 字。

### 分类表（正则按序匹配，首中即停）

| error_class | blame | 匹配特征（不区分大小写） | 话术方向 |
|---|---|---|---|
| `quota_insufficient` | user_provider | `余额`/`额度`/`quota`/`insufficient_quota`/`credit balance`/`credit` | 你的 API 服务额度不足，充值后再发消息即可恢复 |
| `auth_invalid` | user_provider | `401`/`invalid api key`/`invalid x-api-key`/`unauthorized`/`authentication` | API Key 无效或已过期，请到设置里重新保存 |
| `model_not_found` | user_provider | `invalid model name`/`model_not_found`/`404` 与 `model` 同现 | 模型名不可用，请检查设置里的模型名 |
| `rate_limited` | provider_transient | `429`/`too many requests`/`rate limit` | 你的 API 服务限流了，稍等几分钟再试 |
| `upstream_unavailable` | provider_transient | `5\d\d`/`overloaded`/`timeout`/`timed out`/`connection`（上游语境） | 你的模型服务暂时不可用，稍后会自动恢复 |
| `turn_timeout` | system | 异常类型 == TimeoutExpired（120s 上限） | 这轮回复超时了，稍后再试 |
| `reply_parse_failed` | system | 「no usable reply after sanitization」 | 系统处理回复时出了问题，我们会尽快排查 |
| `unknown` | system | 其余 | 连接模型服务时出了问题 |

**归责纪律**：`blame=system` 的话术**绝不能**引导用户去改 key/充值/改配置（会误导，
参考 dded 案例用户白折腾）。只有 `user_provider` 才给行动指引。

**消息正文**：`⚠️ {话术}` + 换行 + `详情: {原始错误截断 200 字}`。
匹配次序注意：`quota_insufficient` 必须先于 `auth_invalid`/`rate_limited`
（本案例 403 里同时含「403 Forbidden」和「额度」，语义是余额不是权限）。

## 组件 2：腿① — chat system 消息

### consumer 侧

- `post_reply()` 新增可选参数 `role: str = ""`、`notice_kind: str = ""`，非空时进
  request body。system 消息调用形态：`post_reply(text, role="system",
  notice_kind="upstream_error", suppress_push=True)`——兜底话术那条已推送，不双推。
- 发送位置有两个，注意机制不同：
  - **`:6503` 前台异常路径**：`call_agent` 抛异常 → 分类异常 → 发兜底 + system 消息。
  - **`:3410` 回复清洗为空路径**：默认（`SEND_FALLBACK_ON_AGENT_ERROR=true`）它*不抛异常*、
    静默返回 `[FALLBACK_REPLY]`——若只挂在异常路径上，`reply_parse_failed` 永远不触发。
    该处需额外置一个模块级「本回合清洗失败」标记（或改为返回携带标记的结构），由前台
    调用方在发送回复后检查并补发 system 消息（合成 `reply_parse_failed` 分类）。
  system 消息发送失败只 log 不重试、绝不影响回合收尾。

### backend 侧（`/v1/chat/response`）

- 接受可选 `role`，白名单 `{"openclaw", "system"}`，缺省/非法值一律落 `openclaw`。
- 接受可选 `notice_kind`（限长 64 的字符串，仅 `role=="system"` 时落库）。
- 加密信封路径完全复用（system 消息同样是 v1 ciphertext envelope）。

### role 审计结论（新 role 在各判断点的行为）

| 判断点 | 现状 | system 消息行为 | 需改动 |
|---|---|---|---|
| 认领/重投递 `backend/chat/service.py:341,365` | 只认 `role=="user"` | 自动排除，不会被 consumer 当用户消息认领 | 否 |
| 已回复判定 `backend/chat/chat_core.py:124,183` | `("agent","openclaw")` | **不算回复**——已回复标记由兜底话术承担；system 消息若算回复会干扰 409 双扣防护 | 否（刻意不加 system） |
| 前台上下文 / capture 窗口 `chat_core.py:574`、consumer `:4210` | 只认 user/agent/openclaw | **不进** model 上下文与记忆 capture（对 agent 是噪音） | 否（自动排除） |
| 历史下发 iOS `service.py:299-303` `_chat_history_item` | 按 role 分支 | 加 else/system 分支：role 原样透传 + 带上 `notice_kind` | **是** |

### 老版 App 兼容

未知 role 若被老版丢弃不显示——可接受：兜底话术照发，老版体验与现状完全一致；
新版 App 升级后才看到 system 气泡。

## 组件 3：去抖策略

- **前台聊天回合**失败：必发（用户正等着回复）。
- **后台任务**（memory_capture / dream / proactive）失败：consumer 进程内存里按
  `error_class` 记 `last_notified_at`，同类错误 **6 小时内只发一条**；任何一次成功
  回合（前台或后台）清空全部记录（恢复后再坏会重新提醒）。
- 内存态即可：consumer respawn 丢状态顶多多发一条，可接受。
- 实测校准场景：usr_0d16bfd4 的 25 次 capture 重试风暴 → 只发 1 条。

## 组件 4：腿② — 设置页 last_runtime_error

- backend 新增瘦路由 `POST /v1/model_api/runtime_error`（api-key 与 runtime-token
  双认——**必须**收 runtime-token，host-all consumer 没有 api-key，教训见
  `model-api-providerkey-runtime-token-decrypt-gap`）。
- body：`{"error": str(≤300), "error_class": str(≤64)}`；实现复用
  `backend/hosted/config_store._patch_model_api_runtime_profile`。
- consumer：每次分类后调用（失败写 error，成功回合写 `{"error": ""}` 清空——设置页
  始终反映最新状态）。调用失败只 log。
- iOS 读侧已存在（`setup_core.py` 的 `last_runtime_error`），零改动可见。

## 组件 5：iOS（独立仓任务，另行排期）

聊天流渲染 `role=="system"`：居中灰色小字样式，具体遵循 iOS 仓 DESIGN.md 令牌。
`notice_kind` 预留区分未来其它系统通知。不做不阻塞后端上线。

## 测试

1. **分类器单测**（用 prod 真实报错串做用例）：
   - `403 ... 预扣费额度失败, 用户剩余额度: ¥0.018` → quota_insufficient / user_provider
   - `exceeded retry limit, last status: 429` → rate_limited / provider_transient
   - `Unexpected response type: NoneType`（脑裂案例）→ unknown / system
   - `400 Invalid model name` → model_not_found / user_provider
   - claude `credit balance is too low (api_status=400)` → quota_insufficient
   - TimeoutExpired → turn_timeout / system
   - 「no usable reply after sanitization」→ reply_parse_failed / system
2. **/v1/chat/response**：role 白名单（system 落库、非法值落 openclaw）、notice_kind
   限长与仅 system 落库、history 下发含 role/notice_kind。
3. **去抖单测**：同类 6h 去抖、成功重置、前台绕过去抖、不同 error_class 互不影响。
4. **回归**：system 消息不被认领、不把用户消息标为已回复、不进 capture 窗口/前台上下文。
5. **runtime_error 路由**：api-key 与 runtime-token 两种鉴权、清空语义、限长截断。

## 部署顺序

backend 与 consumer（agent-runner 镜像）同批先上——老 App 无感知、行为不回退；
iOS 渲染随后任意节奏。无迁移、无链上操作。

## 风险

- 分类误判 → 话术兜底为 unknown/system 向，不给误导性行动指引；原始摘要始终附带。
- system 消息含上游原文可能夹带 request id 等噪音 → 截断 200 字，且消息本体走加密
  信封，不落明文。
- 未来新增 role 时，`chat_core.py` 各元组判断是硬编码——本次刻意不抽象（YAGNI），
  但在 role 白名单处留一行注释指向本 spec 的审计表。
