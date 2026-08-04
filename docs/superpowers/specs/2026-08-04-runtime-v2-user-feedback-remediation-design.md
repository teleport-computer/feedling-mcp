# Runtime V2 线上用户反馈修复设计

- 日期：2026-08-04
- 状态：设计已确认，待出实施计划
- 目标分支：`test`
- 优先级：P0 定位假阳性与伪 user 唤醒；P1 画像就绪闸与可观测性

## 1. 背景与线上证据

2026-08-02 至 2026-08-03 的 Runtime V2 生产灰度收到四类一致反馈：前台回复像未读人设、
主动联系减少、主动消息像定时器且会把系统唤醒当作用户问题、用户未移动时却收到“到家”消息。
生产已回退到 resident；本设计在重新灰度前修掉仍存在于 `test` 的根因。

只读排查得到以下内容无关证据：

- 594 条已应用的 V2 chat 回复中，319 条携带 thinking，覆盖率 53.7%。
- 最近灰度中有 chat 行为的 6 个用户里，4 个有 `state=ok` 的 `v2_agent_profile`，2 个缺失。
- 135 次 heartbeat 中 110 次完成、75 次产生可见回复、25 次失败；35 次完成后沉默。
- 3 个用户产生 20 次 `arrived_at_anchor`；重复事件最短间隔 49 秒，3 次小于 15 分钟，
  8 次小于 1 小时。
- arrival 事件发生到服务端入队的 p90 为 12.2 秒、最大 62.3 秒；入队到完成回复 p90 为
  99.3 秒、最大约 230 秒。不存在服务端数小时排队，问题是 arrival 假阳性。

## 2. 根因

### 2.1 感知 differ 是多进程内存态

`perception.ingress_v2.DEFAULT_DIFFER_V2` 是进程内单例，`PerceptionDifferV2.observe()` 将
`prev is None` 直接判为变化。生产 backend 有多个 Gunicorn worker；同一 anchor 首次落到
另一 worker 或进程重启后都会产生 `arrived_at_anchor`，即使用户没有移动。

### 2.2 主动唤醒伪装成用户消息

V2 在对话尾部追加 `_WAKE_NUDGE`，序列化成 `role=user`。`_genuine_user=False` 只影响服务端
分组，provider 仍将其视作用户输入。模型因此会回答唤醒文案，并把该英文假消息当作最新用户
语言和意图来源。

### 2.3 V2 主动 prompt 比 V1 更克制

V1 明确声明说话和沉默同等有效、不需要很强理由。V2 要求有“specific / natural / genuinely
worth saying”的理由，否则沉默正确。这是产品行为变化，不是实现细节。本次按用户确认直接恢复
V1 语义。

### 2.4 画像不是切入 V2 的前置条件

画像生成与 runtime ownership 切换目前解耦。切入后画像可以缺失，导致 V2 使用旧 summary
fallback；运营面无法区分“已读 V2 画像”与“没有画像”。

## 3. 目标与非目标

### 3.1 目标

1. 同一感知输入在任意 worker、任意重启次数下产生相同结果。
2. 首次位置观察只建立 baseline，绝不产生 arrival；真实变化只产生一次 arrival。
3. provider 输入中的 `role=user` 只来自真实聊天记录。
4. heartbeat 主动语义与 V1 对齐：说话和沉默同等有效，不设额外“值得说”门槛。
5. 只有完整 V2 画像 ready 的用户才允许自动切入 V2。
6. 内容无关地观测 prompt 装载、TTFT、thinking、画像来源、主动结果与感知判定。

### 3.2 非目标

- 不通过人为 sleep 延迟回复。
- 不改变 manual wake 与 scheduled reminder 的用户授权语义。
- 不把地点原值、prompt 正文、thinking 正文写入指标。
- 不在本轮调整主动频率设置的 15 分钟至 12 小时产品范围。
- 不重新开放生产灰度或部署生产。

## 4. 总体架构

```text
iOS 感知上报
    |
    v
PostgreSQL 持久化 signal baseline（按 user_id + signal，时间戳有序）
    |
    +-- baseline / duplicate / stale / unchanged --> 不发 wake
    |
    +-- changed -------------------------------> 一次 arrival wake
                                                     |
真实对话历史 + V1 等价主动 system prompt + 不可信 runtime event
                                                     |
                                                     v
                                          模型自主说话或沉默
```

灰度入口增加画像前置条件：

```text
active provider route
  AND desired=v2
  AND v2_agent_profile.state=ok
  AND non-empty memory
  AND non-empty user
  AND runtime control consistent
```

条件不满足时保持 resident，并暴露非敏感原因；不做半切换。

## 5. P0-A：持久化感知 baseline

### 5.1 数据模型

新增 `perception_signal_state_v2`：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `user_id` | text | 用户标识 |
| `signal` | text | 归一化 signal，如 `wifi_anchor` |
| `value_fingerprint` | text | canonical JSON 的 HMAC-SHA-256，不保存原值 |
| `last_seen_at` | timestamptz | 最新被接受事件时间 |
| `last_changed_at` | timestamptz | 最近真实变化时间 |
| `source_event_id` | text nullable | 最近事件的幂等键 |
| `updated_at` | timestamptz | 数据库更新时间 |

主键为 `(user_id, signal)`；`source_event_id` 不跨用户做全局唯一。表进入主库迁移、TEE 全表
对齐和 account/reset 清理清单。它只存指纹和时间，不进入用户内容导出。

HMAC 使用 `FEEDLING_RUNTIME_TOKEN_SECRET`，消息域固定为
`perception-signal-v2\0<user_id>\0<signal>\0<canonical-json>`。部署缺少该密钥时持久 differ
fail closed，不产生自动 wake；不允许退化成无密钥 SHA-256，以免低熵的 `home/work` 等值可被
离线枚举。

### 5.2 原子判定接口

`perception.signal_state_v2.observe_signal_state(...) -> SignalObservationDecision` 在一个数据库
事务和行锁内返回以下之一：

- `baseline_created`：无旧行；插入 baseline，不发 wake。
- `duplicate`：`source_event_id` 与当前相同；不写、不发 wake。
- `stale`：事件时间早于 `last_seen_at`；不写、不发 wake。
- `conflict_same_ts`：事件时间相同但指纹不同；不写、不发 wake，等待更新事件消解顺序。
- `unchanged`：指纹相同；只推进 `last_seen_at` 和事件 ID，不发 wake。
- `changed`：指纹不同；推进 seen/changed，允许创建一次 wake。

时间相同但 event ID 不同按确定性顺序处理：指纹相同为 unchanged；指纹不同为
`conflict_same_ts`，不让数据库抢锁顺序决定用户可见行为。事件 ID 缺失时仍由时间戳和指纹去重。

数据库读取或写入失败时 fail closed：感知 snapshot 原有存储可以成功，但本次不产生主动消息。
不会退回内存 differ，因为错误时的静默优于误报。

### 5.3 作用范围

第一阶段仅将会触发用户消息的离散信号接入持久判定：`wifi_anchor`、
`connectivity_anchor`、`bluetooth_anchor`、`unlock_after_absence`、`photo_added` 和
`screen_phash`。位置类严格执行 baseline-no-wake。显式事件类若具有稳定 event ID，可通过
`allow_first_event=True` 保留首次真实事件语义；调用点必须显式选择，默认仍为 false。

## 6. P0-B：主动回合不再制造 user 消息

### 6.1 Provider 消息合同

heartbeat 与 perception-triggered heartbeat 的 provider 输入：

- `system`：V1 等价主动语义，明确这是平台触发的 presence moment，不是用户提问。
- runtime-data block：事件类型、时间与安全投影，保持 untrusted。
- conversation tail：只包含数据库中真实 user/assistant/tool 历史。
- 不追加 `_WAKE_NUDGE`，不再出现合成 `role=user`。

工具循环新增内部 `WakeTurnSeed`/空 transcript 启动语义。它只用于“允许无新用户输入启动一轮”
的控制流，不序列化给 provider。`build_messages()` 必须支持 system + 历史为空；如果某个 provider
wire 明确拒绝纯 system 输入，则自动 heartbeat 在无真实历史时睡回去，不调用模型，绝不伪造 user。

manual wake 使用同一消息合同，但保留 `manual=True` 的授权来源。scheduled reminder 将提醒说明
放在 system/runtime-data 中，标注为“用户此前明确授权的提醒内容”，不表达成用户刚发的话。

### 6.2 恢复 V1 主动语义

V2 system prompt 与 V1 保持同一产品含义：

- 这是 presence check，不是请求。
- 开口与保持安静同等有效，二者都不是默认或“安全答案”。
- 完全按自身性格、真实对话和当前时刻决定；不需要强理由。
- 感知 glance 只用于决定是否深入读取，不用于汇报设备状态。
- 不向用户提及 wake、timer、prompt 或系统字段。

测试禁止重新引入 `only if`、`genuinely worth saying`、`silence is correct` 等单向门槛。

## 7. P1-A：画像就绪灰度闸

### 7.1 Ready 定义

`profile_store.profile_ready_for_v2(raw) -> bool` 必须同时满足：

- 文档通过现有 schema 校验；
- `state == "ok"`；
- `disabled is False`；
- `memory` 为非空字符串；
- `user` 为非空字符串。

`empty`、`missing`、生成失败、解密失败、字段缺一均不 ready。

### 7.2 切换行为

runtime reconciler 在 desired=v2 时先检查 ready：

- ready：执行现有 generation-fenced resident -> v2 切换。
- not ready：保持 resident，记录 `profile_missing`、`profile_empty`、`profile_failed` 或
  `profile_unreadable`；触发/保留幂等 profile generation job。
- 已经在 V2、单轮画像读取暂时失败：当前 turn fail closed 并进入已有重试/错误观测，不在请求
  中途修改 runtime ownership。

管理员显式切换接口同样应用 ready 闸，返回可操作的 409，而不是表面成功后仍缺画像。

## 8. P1-B：内容无关观测

### 8.1 Turn metrics

扩展 `v2_turn_metrics` 或同粒度附表，记录：

- `prompt_assembly_ms`：开始装载到固定 base prompt 完成。
- `provider_ttft_ms`：provider 请求开始到首个有效 content/reasoning/tool event；无法观测时为 NULL。
- `thinking_protocol_status`：`complete` / `absent` / `malformed` / `not_applicable`。
- `profile_prompt_source`：`v2_profile` / `legacy_fallback` / `unavailable`。
- `wake_outcome`：`spoke` / `slept` / `failed` / `yielded_to_chat` / `not_applicable`。

TTFT 的“有效事件”排除 SSE keepalive、空白和纯分隔符。provider 不支持流式或当前客户端只返回
完整体时，`provider_ttft_ms` 为 NULL，继续由现有 `latency_ms` 表示整轮耗时；绝不拿完成时间冒充
首 token。

### 8.2 Perception decision metrics

每次持久 differ 判定记录：

- signal 类别；
- outcome（含 `conflict_same_ts`）；
- `event_age_ms`；
- 是否携带 event ID；
- 是否最终提交 wake。

不记录值指纹、地点、事件原文或用户内容。管理页聚合 baseline、duplicate、stale、unchanged、changed
和 changed->wake 的数量，帮助发现客户端重放与服务端误触发。

## 9. 竞态与错误处理

- 多 worker 同时首次观察：主键/行锁保证一个 baseline，其他为 unchanged/duplicate，零 wake。
- 多 worker 同时观察真实变化：只有提交该变化的事务返回 changed；相同输入重试不再发 wake。
- runtime rollback 与 wake 并发：保留现有 generation fence，定位判定成功不代表必须交付消息。
- 用户新消息与 wake 并发：保留 yield-to-chat，真实用户消息优先。
- profile 在切换检查后被禁用：切换事务内重新读取/锁定必要状态，避免 TOCTOU。
- TEE 镜像失败：主库事务权威；按现有 mirror/outbox 口径重试，不回滚成内存 differ。
- account/reset：删除持久 signal state，重置后的第一次观察重新建立 baseline。

## 10. 测试与验收

所有实现遵循 RED -> GREEN -> REFACTOR。

### 10.1 持久 differ

- 首次 anchor 只建立 baseline，无事件。
- 新 `PerceptionDifferV2` 实例/模拟不同 worker 观察同一 anchor，无事件。
- 真实 anchor 变化只返回一次 changed；并发重复只有一个 wake。
- 重复 event ID、相同值、旧时间戳、同时间戳冲突值均不 wake。
- DB 错误不回退内存，不 wake。
- reset 删除 baseline，下一次仍只建 baseline。
- 主库迁移、TEE DDL、迁移单 head 与全表对齐测试通过。

### 10.2 Wake prompt

- 所有 heartbeat provider messages 中，每个 `role=user` 都能追溯到真实 chat row。
- 无真实历史的自动 heartbeat 不调用 provider。
- manual/scheduled 不把运行时载体伪装成最新用户问题。
- system prompt 包含 V1 等价语义且不含单向沉默门槛。
- 现有感知安全投影、工具权限、yield-to-chat 和 reply generation fence 保持通过。

### 10.3 Profile gate

- missing/empty/failed/unreadable/缺字段均不切 V2。
- ready profile 正常切换。
- 非 ready 用户会幂等触发 profile job，不产生重复在飞任务。
- 管理员切换返回稳定错误合同。

### 10.4 Observability

- prompt assembly、TTFT、thinking、profile source、wake outcome 正确落库。
- 非流式 provider 的 TTFT 语义明确。
- 指标和管理 API 不含 prompt、地点、指纹或 thinking 正文。

## 11. 文档与发布

该变更修改公开行为、架构和感知信任边界，必须同一 PR 更新：

- `docs-site/content/docs/architecture.mdx`
- `docs-site/content/docs/reliability.mdx`
- `docs-site/content/docs/workflows/perception.mdx`
- `docs-site/content/docs/changelog.mdx` 的 `Unreleased`
- 本设计不新增公开 API 字段，因此不改 public OpenAPI；若实施发现必须新增公开字段，视为范围变化，
  先回到设计确认，再更新 source/override 并生成 `docs-site/openapi/public.json`

发布顺序：合入 `test`，运行真实 PostgreSQL L1、相关 OpenAPI 契约测试、docs types/lint/build；部署
test 后以至少两个 backend worker 验证重启/负载均衡不产生 arrival，再考虑小范围生产灰度。
生产灰度 gate 必须同时查看画像 ready 率、thinking 覆盖、主动开口率、arrival 判定分布和 TTFT。

## 12. 实施拆分

1. **P0-A**：持久 perception signal state、迁移、TEE 对齐、baseline-no-wake。
2. **P0-B**：移除 fake user seed，恢复 V1 主动语义，保持 scheduled/manual 合同。
3. **P1-A**：画像 ready helper、reconciler/admin 切换闸。
4. **P1-B**：TTFT、prompt/profile/thinking/wake/perception 指标与管理聚合。
5. **文档与 test 环境证据**：公开文档与多 worker 验证记录；不改 public OpenAPI。
