# Runtime V2 推送能力补齐（对齐 V1）— 设计

日期：2026-07-25
状态：设计已确认，待写实现计划

## 1. 背景

### 1.1 触发事件

pre 环境测试者 `usr_7b269b4c31e39203` 报告：「他给我发了消息，锁屏通知能看到，
但点进 app 看不到」。

排查结论是**环境串台，不是 pre/V2 的 bug**：

- 同一台 iPhone 上，APNs device token（md5 `fab2a380d7`）同时注册在 pre 账号
  `usr_7b269b4c31e39203` 和 prod 账号 `usr_48f7b31b4595fede` 下；两个账号的设备
  内容公钥也一致（md5 `66c885633b44`）。APNs token 是 per-app-install 唯一的，
  说明是**同一个 app 安装**用 `FeedlingAPI.switchEnvironment` 从 prod 切到了 pre。
- prod 账号在 2026-07-25 11:17:15 / 11:17:20 / 11:17:24（CST）连发三条
  `agent_initiated_proactive`，`push_decision=send`、`alert_status=delivered`。
  锁屏上的通知来自 prod。
- 用户点开 app 时 app 在 pre 环境，显示的是 pre 账号的 history（最后一条停在
  07-25 02:34），自然看不到那三条。

### 1.2 排查中暴露的真实缺口

V2 **完全没有推送能力**，这是本设计要解决的问题：

- `backend/model_api_runtime/v2/` 整个目录零处调用 push service。V2 的回复由
  `_sink_reply_in_transaction`（`serve_worker.py:1810`）在 outbox 事务里直接写
  `chat_messages`，绕开了 V1 唯一发 APNs alert 的路径
  `chat_core.response()`（`chat/chat_core.py:967`）。
- 事实佐证：pre 全库历史上 `alert_status` 只有 1 条 `skipped` + 1 条 `suppressed`，
  从未成功投递过一条聊天推送；`agent_initiated_proactive` 消息数为 0。

结果是：**在 pre 上测 V2 的用户，无论聊天回复还是主动消息，永远收不到任何通知。**

### 1.3 关键约束

APNs 私钥只注入 `backend` 容器（`deploy/docker-compose.phala.pre.yaml:181`），
`serve-worker` 容器的 environment 里没有任何 `APNS_*` 变量 —— 它现在物理上发不出
推送。但两者是**同一个镜像、同一个 CVM 内的两个容器**（compose 同文件 `:238`），
`push.service` 那份代码本来就在 worker 进程里，缺的只是密钥。

## 2. 范围

**做**：让 V2 的聊天回复和 wake 主动消息都能发出 APNs alert，决策链与 V1 逐字一致
（presence gate、`reminders_delivery` 开关、delivery metadata 回写）。

**不做**：

- Live Activity。V1 侧 `FEEDLING_AI_MSG_LIVE_ACTIVITY` 默认关
  （`push/service.py:29`），V2 保持同样默认，不引入差异。
- 推送标题文案改进。V1 硬编码 `payload.get("title") or "IO"`，中文用户看到英文
  "IO"。这次逐字对齐 V1 用 `"IO"`，文案问题留到下一轮两个 runtime 一起改，避免只
  改一边造成不一致。
- iOS 改动。推送 payload 与 V1 完全一致（`feedling.type=chat_reply`），客户端
  零改动。
- 「切环境不注销旧环境 push token」。这是 §1.1 事故的直接成因，但属于独立问题，
  见 §8 后续项。

## 3. 四个设计决策

| 决策点 | 选定方案 | 理由 |
|---|---|---|
| 明文正文如何跨过 effect outbox 的持久化边界 | 进程内内存（回合闭包槽位，见 §4.1） | 明文零落库，与 V1「明文只过内存」姿态一致 |
| 如何区分应答与主动消息 | 新增 metadata 字段 `wake_kind` | 只做加法，不碰已上线的 `source` 写入语义 |
| 一个回合多条回复气泡 | 回合末尾只推一次 | 对齐 V1「一回合一推送」，避免连珠炮 |
| 从哪里发 APNs | serve-worker 调 backend 内部端点 | APNs 私钥暴露面不扩大，且天然复用 V1 全套决策链 |

## 4. 架构与数据流

### 4.1 apply 的真实时机（设计的关键前提）

V2 **不是**在回合末尾统一 apply。`_on_reply` 每次入队 reply effect 后会立刻
`await deps.apply_pending_effects(user_id)`（`worker.py:4961` wake lane /
`:7064` chat lane），注释写明「C6: drain immediately so an intermediate bubble is
visible mid-loop」—— 中间气泡必须即时对用户可见。

此外还有两个 apply 调用点：

- **回合收尾 drain**（wake lane `worker.py:5189`，chat lane 同类）：最后一轮里的写
  工具没有后继 `on_reply` 触发 drain，由它兜底 flush。
- **回合开头 recovery drain**（`worker.py:6243`）：drain 上一个进程崩溃前已durably
  入队、但没来得及 apply 的 effect。

这个前提带来一处简化：明文 `text` 在 `_on_reply` 就地 apply 时**仍在闭包作用域
内**，不需要任何 `effect_id → 明文` 的长期映射，只需要一个回合闭包内的「最后一条
待推送」槽位。`_on_reply` 本身就是定义在回合内部的闭包，槽位天然按回合隔离 ——
无并发问题，也不可能跨回合泄漏。

### 4.2 数据流

```
【worker 进程 · 回合闭包内】
  _on_reply(text, final=…)
    ├─ 入队 reply effect（只含密文 envelope，明文不进 payload）
    ├─ await apply_pending_effects()        ← 现有行为，就地 drain
    ├─ disposition = get_effect_disposition(effect_id, …)   ← 现有行为
    └─ 若 disposition["status"] == "applied"：
         push_slot = {msg_id, text[:240], is_wake}   ← 覆盖式，只留最后一条

  （多条 reply 气泡重复上述，槽位被后来者覆盖）

【回合收尾 · finally】
    ├─ 槽位非空 → deps.send_reply_push(...)   ← 注入的 dep，生产实现在 serve_worker
    └─ 清空槽位

【serve_worker · 生产实现】
  POST {FEEDLING_API_URL}/v1/internal/push/ai_reply  (push token, TTL 60s)

【backend · 新端点】
  store ← auth.store（user_id 从 runtime token 派生，不信 payload）
    ├─ is_wake ? evaluate_delivery_v2(settings) gate : 跳过
    ├─ push_service._deliver_ai_message_push_if_background(store, body=…)
    └─ store.update_chat_message_metadata(msg_id, delivery_fields)
```

推送发起写在 `worker.py` 的回合闭包里，但 HTTP 调用本身经 `TurnDeps` 注入
（照抄 `deps.apply_pending_effects` 的既有模式），生产接线留在 `serve_worker.py`。
依赖方向与 `CONTRIBUTING.md` 一致，单测也不必起 HTTP。

放在 `finally` 而不是成功路径上：回合中途失败、只吐了中间气泡没吐 final 时，用户
仍应收到通知，不该漏。

三条不变量：

1. **明文零落库。** effect payload 里仍然只有密文 envelope；明文只在 worker 进程
   内存活一个回合。
2. **推送永不早于落库。** 推送挂在 apply 之后，被 fence 丢弃的 effect 不会产生
   推送 —— 正是 §1.1 事故的反面（推送到了、消息在别处）。
3. **复用 V1 全套决策链。** presence gate（`push/service.py:71`）、
   `reminders_delivery` 开关（`proactive/controls_v2.py:271`）、delivery metadata
   回写，一行都不重写。最后这条尤其重要：§1.1 的根因能定位，靠的就是
   `push_decision` / `alert_status` 这几个字段。

## 5. 改动清单

全部在 `feedling-mcp` 仓库。

### 5.1 `backend/model_api_runtime/v2/worker.py`

- `_build_encrypted_reply_effect_payload`：payload 增加 `wake_kind` 字段（非明文）。
  chat lane 写空字符串；wake lane 写该次 wake 的 lane 名。落库后即
  `chat_messages.doc.wake_kind`，推送分流与事后取证都读它。

  ⚠️ **它与 V1 `proactive_jobs` 日志里的同名字段不是同一套词表**（本设计初版误称
  「沿用同一套」，实现时发现不符，2026-07-25 订正）。V2 写的是 lane 名
  `heartbeat` / `scheduled` / `manual_wake` / `screen_watch`；V1 那套是
  `presence` / `screen` / `screen_watch` / `scheduled_wake` / `background_result`
  （`backend/proactive/gate.py`）。两者只有 `screen_watch` 重合 —— **同名不同义，
  跨 V1/V2 联查时不要把这两列当同一个维度 join。**
- `TurnDeps`（`:972` 一带）：新增可选注入项 `send_reply_push`，照抄
  `apply_pending_effects` 的形态（`None` 时整个特性静默关闭，单测默认不接线）。
- 两处 `_on_reply` 的 `status == "applied"` 分支（wake lane `:4986` 之后、chat lane
  `:7160`）：覆盖式写入回合闭包的 `push_slot`。挂在已有的 applied 判定上，不新增
  任何状态查询。
- 两处回合收尾：`_run_wake` 的顶层 `try`（`:4340`，目前只有 `except`，需补
  `finally`）与 `process_job` 的最外层 `finally`（`:7568`）。槽位非空则调
  `deps.send_reply_push`。

### 5.2 `backend/model_api_runtime/v2/effect_outbox.py`

**无需改动。** 判断「我刚入队的这条是否真的落库了」已有权威机制：`_on_reply` 在
drain 之后调 `get_effect_disposition(effect_id, …)`（`:528`），只认
`status == "applied"`。

不要改用 `apply_pending_effects` 的返回值 —— 该函数的 docstring 明确警告，独立的
reconciliation sweeper 可能抢在生产者 drain 之前赢下这一行，因此「本次调用改了哪些
行」不是投递确认。靠返回值会在 sweeper 抢先时漏推。

同理，`msg_id` 也不必从 apply 返回值里取：它是 `_build_encrypted_reply_effect_payload`
（`worker.py:3167`）里由 `effect_id` 确定性推导的 `sha256(effect_id)[:32]`，即
`payload["envelope"]["id"]`，`_on_reply` 作用域内现成可得。

### 5.3 `backend/model_api_runtime/v2/serve_worker.py`

- `_sink_reply_in_transaction`（`:1810`）：把 payload 里的 `wake_kind` 写进
  `_build_chat_message` 的 extra。返回值不变（仍是 post-commit thunk）。
- 新增 `_send_reply_push(...)`：`TurnDeps.send_reply_push` 的生产接线，POST
  `{FEEDLING_API_URL}/v1/internal/push/ai_reply`。
- 新增 `_mint_push_token(user_id)`：scope `["chat_push"]`，TTL 60s。**不动**现有的
  `_RUNTIME_TOKEN_SCOPE`（`:313`，注释明确要求保持稳定），仿照
  `_mint_genesis_token`（`:443`）单独签发。

### 5.4 `backend/push/routes_asgi.py`

新增 `POST /v1/internal/push/ai_reply`：

- 鉴权：`Depends(require_scope("chat_push"))`（`asgi/deps.py:39`）。user_id 一律从
  token claims 派生，**不读 payload 里的 user_id**。
- 入参：`{"msg_id": str, "body": str, "is_wake": bool}`。
- 逻辑：`is_wake` 为真时先过 `evaluate_delivery_v2` gate（用户可能关了
  `reminders_delivery`）；然后调 `push_service._deliver_ai_message_push_if_background`
  （`push/service.py:161`），title 传 `"IO"`；最后
  `store.update_chat_message_metadata(msg_id, delivery_fields)` 回写。

### 5.5 `tools/export_public_openapi.py`

`EXCLUDED_PREFIXES`（`:30`）加入 `"/v1/internal"`。该前缀目前**不在**排除列表里，
新端点会被算进公开契约：`tests/openapi/test_public_openapi.py` 的
`len(operations) == 148` 与 `requestBody == 68` 两个断言会直接变红，且这个纯内部
面会被写进对外文档。必须先于新增路由落地。

### 5.6 部署

compose 不用改 —— `FEEDLING_API_URL` 和 `FEEDLING_RUNTIME_TOKEN_SECRET`
serve-worker 都已具备。

回滚拉杆：环境变量 `FEEDLING_V2_PUSH_ENABLED`（默认开）**只在一处生效** ——
serve-worker 组装 `TurnDeps` 时据它决定是否注入 `send_reply_push`。关掉即
`send_reply_push=None`，行为退回今天的「不推送」，与 §6 最后一行是同一个机制，
不存在第二个开关。

## 6. 边界情况与降级

推送是 best-effort 的附加动作，任何一条都不能反过来伤到消息本身。

| 情况 | 行为 |
|---|---|
| 上个进程崩溃，effect 由下个回合开头的 recovery drain（`worker.py:6243`）落库 | 那次 apply 不经 `_on_reply`，没有明文、也不写槽位 → 不推，消息不丢（与 V1 崩溃行为一致） |
| 推送 HTTP 失败 / 超时 | 只记日志，绝不让回合失败 |
| 本回合 reply 全被 fence 丢弃 | 不在 applied 明细里 → 槽位为空 → 不推 |
| 回合中途失败，只吐了中间气泡 | 槽位有值，`finally` 里照常推（这正是不放在成功路径上的原因） |
| 正文为空 | `_on_reply` 对空文本本来就直接 return，不入队也不写槽位 |
| app 在前台 | V1 presence gate 判定 `suppress`，记 metadata 不发推送 |
| 用户关了 `reminders_delivery` | wake 消息记 `suppressed`，消息仍落库 |
| 槽位内存 | 回合闭包局部变量，随回合协程结束自然回收，无跨回合泄漏面 |
| 账号已删除 | V1 `_deliver_ai_message_push_if_background` 开头的两级 account-gone 检查已覆盖 |
| `deps.send_reply_push` 未注入 | 特性静默关闭，行为退回今天的「不推送」 |

## 7. 测试策略

按 `docs/testing/TESTING.md` §2 的决策矩阵，本次改动涉及后端逻辑 + 新路由 + V2
worker，需要：

**worker 侧单测**（`TurnDeps.send_reply_push` 注入假实现，断言调用次数与入参）

- 一个回合多条 reply → 只调一次，且用的是最后一条的正文与 `msg_id`
- effect 被 fence 丢弃（不在 applied 明细里）→ 不调
- 回合中途抛异常、只有中间气泡 → `finally` 仍调一次
- 假实现抛异常 → 回合结果不受影响（不 flip 成 failed）
- `send_reply_push=None` → 全流程不报错，行为等同今天
- wake lane 的调用带 `is_wake=True`，chat lane 带 `False`

**端点单测**

- 无 `chat_push` scope 的 token → 403
- payload 里塞别人的 user_id → 以 token 的 user_id 为准
- `is_wake=true` 且 `reminders_delivery` 关闭 → `suppressed`，且 metadata 回写
- 成功路径 → `alert_status` 等字段确实写回那条 chat message

**回归**

- 起 PG 后跑 L1 全量：真基线约 2440 passed / 7 个 pre-existing 红。**不起 PG 会
  静默跳过约 2000 个 DB 用例**，"全绿"是假象。
- `python -m pyflakes` 改动的包（全仓恒剩 1 条 unused 是预期）。

**端到端**

在 pre 上真机验证：发一条消息、app 切后台、确认锁屏收到通知且点进去能看到该消息；
再验证 app 在前台时不推送但 metadata 记 `suppressed`。

## 8. 后续项（不在本次范围）

1. **切环境不注销旧环境的 push token。** `FeedlingAPI.switchEnvironment`
   （iOS 仓 `App/FeedlingTest/API/FeedlingAPI.swift:834`）只做凭证按环境隔离，
   prod 的 device token 仍留在 prod 库、prod 的托管 agent 仍在跑。只要测试者人在
   pre，prod 就会持续往这台手机推 —— §1.1 的现象会反复出现。
2. **推送标题文案。** 两个 runtime 一起从硬编码 `"IO"` 改成 agent 名字或本地化
   文案。
3. **prod 主动消息连发。** §1.1 里 prod 账号 9 秒内落了三条主动消息，形似多个 wake
   各跑了一轮，需要单独取证。
