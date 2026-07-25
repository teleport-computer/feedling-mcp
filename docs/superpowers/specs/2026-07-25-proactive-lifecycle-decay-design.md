# 主动道生命周期衰减策略

作者 claude2,2026-07-25。Seven 已拍板方向与分期。
起因:usr_a40e3713eb189d38 / usr_d98b8d68124090a6 分诊(模型名下线 + 402 欠费)
暴露出——**系统没有任何一处会判定"这个用户该歇了"**。

**状态**

| 期 | 内容 | 状态 |
|---|---|---|
| P0 | 硬 4xx 不再发兜底话术 → 改发可行动话术 | ✅ **已落**(claude2,见 §10) |
| P1 | 轴 B:供给侧健康,连续 48h 无成功即停主动道 | ✅ **已落**(codex2 `d2a635cd`,claude2 双签,见 §11) |
| P2 | 免模型召回推送 + 设置页"配置待修复"态 | ⏸ **暂不做**(Seven 2026-07-25:召回优先级低,见 §12) |
| P3 | 轴 A:参与度衰减梯子 | 待排 |

---

## 0. 一句话

主动道(心跳/屏幕监看/capture/dream)的节奏,现在**只由用户自己设的 `wake_interval_sec`
决定,与"这人还在不在"、"他的 key 还能不能用"完全无关**。本设计给主动道加两个正交的
衰减轴:**参与度衰减**(人还在,只是安静了)与**供给侧健康**(key 死了/欠费,根本产不出东西),
两轴相乘决定实际节奏。聊天道(用户主动发的消息)**全程不受影响**。

---

## 1. 现状(已核实的代码事实)

| 事实 | 位置 |
|---|---|
| 心跳默认间隔 7200s(2h)= **12 次/天**,用户可调 900–43200s | `core/store.py:45-47` |
| 调度器每轮取 `due_heartbeat_users()`,按 `decision.wake_interval_sec` 推进 `next_heartbeat_at` | `v2/scheduler.py:63-76` |
| 402/401/403 → 写 `payment_cooldown_until = now + 600s`;`due_heartbeat_users` / `due_screen_watch_users` 的 SQL 直接排除冷却中用户 | `v2/worker.py:5125-5133`、`v2/jobs_store.py:5195,5216` |
| resident 侧同样 600s 支付冷却 + 通用指数退避(60s 起,**封顶 3600s**) | `chat_resident_consumer.py:484,528-529` |
| **心跳按设计不因"没人应答"停**(Seven 2026-07-21 明确撤掉了"两次无人应答就闭嘴") | `chat_resident_consumer.py:508-527` |
| 聊天道**故意不设支付门**,用户主动发的永远尝试 | 同上注释 |
| 全局无任何"用户不活跃 → 降频"逻辑 | 全仓 grep 为零 |

**净效果**:600s 冷却是个**固定值,永不升级、永不终止**。一把永久死掉的 key,就是
**每天 144 次注定失败的唤醒,永远跑下去**。usr_d98b 07-12 之后再没发过一句话,系统
一路主动到 07-24,500 个 job 里 197 个失败(98 次 402)。

## 2. 实测浪费(prod 前 500/687 用户,2026-07-25)

参与度 = `max(最后一条用户消息, 最后一个 iOS tracking 事件)`

| 闲置时长 | 用户数 | 近 3 天仍有主动道活动 |
|---|---:|---:|
| < 1 天 | 149 | 118 |
| 1–7 天 | 103 | 42 |
| **7–30 天** | **220** | **61** |
| **> 30 天** | **28** | **4** |

- **65 个 7 天以上没露过面的用户,此刻仍在被主动道按满速唤醒**。按默认 12 次/天,
  约 **780 次/天纯浪费**的模型回合。
- 另有 **32 个用户"开 App 但不发消息"**(7 天没发言、3 天内开过 App)。
  → **只用"最后一条用户消息"判活跃会误伤这 32 人**,必须双信号取 max。

## 3. 成本口径:两笔钱,不只一笔

必须说清楚,因为它改变优先级:

1. **我们的基础设施**:每次唤醒 = 一行 job + runner 回合 + enclave decrypt 往返 + DB 写 + 推送。
2. **用户自己的钱(BYOK)**:model_api 路线的模型调用走**用户自己的 key**。
   一个一个月没打开 App 的用户,他的 DeepSeek/OpenRouter 账单**还在每 2 小时被扣一次**,
   为的是生成没人会读的消息。这比我们的基础设施成本更难辩护。

## 4. 设计:两个正交轴

### 轴 A —— 参与度衰减(人还在,只是安静了)

#### A.1 先定义"露面":三级参与度

单靠"最后一条用户消息"会误伤只读不回的用户(实测 32 人),所以分三级:

| 级 | 名称 | 判据 | 实测(44 人详情样本) |
|---|---|---|---|
| **L2** | 对话中 | 有用户消息 | 多数 |
| **L1** | 在读不聊 | 无消息,但有**≥10s 的 App 前台会话** | 5/44 |
| **L0** | 失联 | 两者皆无 | — |
| **L0⁻** | 失联且**不可达** | L0 且 `push.active_tokens = 0` | 3/44 |

**信号来源与坑**:
- 开 App = iOS 上报的 `app_session_end` tracking 事件(44 人样本 12744 条,时长中位 23s)。
- **必须设时长门槛**:样本里 **20% 的会话 ≤5s** —— 那是推送点开即退/后台刷新,
  不是"看过"。建议 **≥10s 才算一次阅读**。(注意反向坑:iOS 会把锁屏空转记成前台,
  单次曾报 12h ——所以时长**只能当下限用**,不能拿来算时长总量。)
- **可达性**独立于活跃度:`push.active_tokens = 0` 基本等于 App 被删/长期未启动。
  **不可达 + 不开 App = 主动消息 100% 落空**,连召回的物理路径都没有 —— 这是最该 park 的一档。
- 记账:`idle` = 自最后一次露面(L2 或 L1 都算);`silent` = 自最后一条用户消息。
  **主梯子只看 `idle`**,`silent` 只用来给 L1 封顶(见下)。

#### A.2 梯子(Seven 2026-07-25 修正:第一周满血)

| 档 | idle | 心跳节奏(以默认 2h/12 次一天为基准) |
|---|---|---|
| **T0 活跃** | **0–7 天** | **满血 —— 用户自己设的值,完全不动** |
| T1 | 7–15 天 | 4h(6 次/天) |
| T2 | 15–30 天 | 8h(3 次/天) |
| T3 | 30–90 天 | 24h(1 次/天) |
| T4 | > 90 天 | 7 天(1 次/周) |
| 恢复 | 发消息 **或** 开 App(≥10s) | **立刻跳回 T0**,无惩罚无爬坡 |

**L1(在读不聊)封顶规则**:读者同样按露面重置主钟,且**最低只掉到 T1,不再往下**——
他在消费产品(读我们发的消息),只是不回话,不该跟失联的人同等对待。
真正往 T2/T3/T4 掉的,只有**连 App 都不开**的 L0。

**L0⁻(不可达)加速规则**:`push.active_tokens = 0` 且 idle ≥ 15 天 → **直接跳 T4 / park**。
理由同上:发了也没人能收到。

**要点**:
- 衰减改的是**唤醒频率**,不是"能不能说话"。gate 的既有逻辑(DND、broadcast off、
  90s 撞车窗、loop guard)全部不动,只是被唤醒的次数变少。
- 上限仍尊重用户设置:若用户把间隔设得比该档还长,取**更长**的那个(绝不因衰减而变密)。
- 每档只降频,**不静音**——满足 Seven"保留轻量召回"的要求:T3 每周一次仍是一次
  真正的、有上下文的召回,而不是模板推送。

### 轴 B —— 供给侧健康(key 死了/欠费)【Seven 2026-07-25:**优先级高于轴 A,先做**】

> Seven 定调:"明显连续几天都是 apikey 不可用状态,就没必要分配资源了。"
> 这条比参与度衰减更硬 —— 参与度低的人**至少还能产出东西**(只是没人看);
> key 死掉的人是**产出恒等于零**,分配的每一份资源都是 100% 浪费,没有任何判断余地。

#### B.0 判据:用**天**做单位,不用次数

**不能用"连续失败 N 次"** —— 上游 5xx / 限流可以连挂几十次,几小时后自己就好了,
按次数判会把正常抖动误杀。实测样本里 7 个用户属于"有 401/403 失败但仍在成功产出"
(上游抖动),必须放过。

**正确判据(两个条件同时满足)**:

```
1. now - last_provider_success_at >= 48h        # 跨天完全没有一次成功
   （从未成功过的用户：用 route_selected_at 起算 —— 实测有用户 500 个 job posted 0 条，
     即 key 从配置那天起就没对过，这类同样要被这条规则覆盖）
2. 期间最近的失败全部是 blame=user_provider     # 402/401/403/模型名失效等不自愈类
```

→ `provider_state = needs_user_action`,**主动道全停**。

`last_provider_success_at` 的维护成本 = 每次成功的 provider 调用一条 UPDATE。
"成功"= 拿到可用回复,**兜底话术不算**。

状态机挂在 `v2_wake_schedule`:

```
ok ──(单次 user_provider 类失败)──▶ cooling(现状 600s,不变)
   ◀──(任意一次成功的 provider 调用)──┘

cooling ──(连续 3 次 user_provider 失败,跨度 ≥1h)──▶ needs_user_action
                                                        │
   ◀────(任意成功 provider 调用 / 设置页重新验证通过)────┘
```

`user_provider` 类 = `classify_agent_error` 里 blame=`user_provider` 的错误
(402 余额、401/403 key 失效、模型名不可用、context 超限……)见
`chat_resident_consumer.py:588-620`。这类错误的共同点:**不会自愈,重试一万次也一样**。

`needs_user_action` 下:
- **主动道全停**(心跳/屏幕/capture/dream),不是降频,是停;
- **聊天道照常尝试** —— 它天然就是"用户修好没有"的探针,零额外成本;
- 每 **24h** 允许一次探活唤醒(而不是现在的每 10 分钟),让偷偷充了值的用户能自动恢复;
- **召回必须换成免模型的固定推送**:key 是死的,根本生成不出 AI 消息。
  发一条模板通知("你的模型配置需要更新,点击查看")+ 设置页红点,
  是这个状态下**唯一**有意义的动作。

> **讨论点 2**:`needs_user_action` 要不要给个 30 天上限——30 天不修就彻底 park(连
> 24h 探活也停),只留用户下次开 App 时的按需恢复?倾向:要,否则死号永久留一份 cron 尾巴。

### 两轴相乘

实际节奏 = `max(轴A 档位间隔, 轴B 状态间隔)`,轴 B 为 `needs_user_action` 时直接停。
即"一个月没来 + key 已死"的用户 = 完全静默,只保留一条待修复的设置页提示。

## 4.5 分期(Seven 2026-07-25 定序)

| 期 | 内容 | 为什么这个顺序 |
|---|---|---|
| **P0** | **#3 硬 4xx 不再发兜底话术**,改发可行动提示 | 既是用户唯一的自救通道,**又是轴 B 的前置数据源**:兜底话术会往聊天流写假 agent 消息,把 `last_agent_at` 这个健康信号污染掉 |
| **P1** | **轴 B**:`last_provider_success_at` + `provider_state`,连续 48h 无成功且失败均为 user_provider → 停主动道 | 产出恒为零,没有判断余地;一列 + 一个 SQL 条件 + 一个写点,**比轴 A 小得多** |
| **P2** | 免模型召回推送 + 设置页"配置待修复"态 | key 是死的,生成不出 AI 消息,召回只能走模板推送 |
| **P3** | **轴 A** 参与度衰减梯子 | 涉及新信号(App 会话门槛)、新表列、档位调参,面更大、争议更多 |

## 4.6 运行时覆盖面:必须同时管住 V1 和 V2(设计初稿最大的漏洞)

初稿把落点全放在 V2 面(`v2_wake_schedule` / `jobs_store.due_heartbeat_users` /
`v2/worker.py`)。**核对后发现那样只覆盖 1 个 prod 用户。**

**事实**(2026-07-25 `/v1/admin/runtime-allowlist`):

```
allowlist 共 6 行
  desired=resident (V1): 5   ← 备注 "Seven: revert batch to V1 2026-07-25"（刚回滚）
  desired=v2:            1   ← 备注 "prod launch V1->V2 regression"
```

名单外的用户默认走 V1 → **prod 事实上是全 V1**。双运行时 spec(07-21)自己给的
目标也是"4-6 周内走完灰度",不是一周。

> ⚠️ **诊断陷阱**:data-track 里的 `runtime_version: 2` / `runtime_mode: hosted_resident`
> **不是** V1/V2 的意思——两者在 `hosted/config_store.py:54-55` 是**写死的常量**,
> 所有托管用户都长这样。真正的分流看 `v2_user_allowlist`。判断某用户跑在哪一侧,
> 最硬的证据是 trace 里的 `agent.model.call.*` 事件和 FALLBACK_REPLY 话术——
> 这两样**只存在于 `tools/chat_resident_consumer.py`,backend 里一行都没有**。

**结论:做"两侧共用"的部分,不写任何 V1 专属代码。**

| 部分 | 覆盖 | 机制 |
|---|---|---|
| P0 分类修复 | **天然 V1+V2** | 后端有镜像分类器 `backend/notices/catalog.py`(V2 经 `chat/chat_core.py:152` 用它),且 `tests/test_catalog_consumer_parity.py` **逐字锁定两份正则与次序**——改一边测试就红,等于强制同步 |
| 轴 B 判据 + `provider_health` 表 | 两侧共用 | 中立表名,不复用 V2 命名 |
| **停主动道的机制** | 两侧共用 | **放在 `backend/proactive/gate.py`**:V1 consumer 每 tick 调它拿决策、V2 scheduler 调 `wake_decision()`——`block_reason` 一处返回,两边都停。V2 的 `due_heartbeat_users` SQL 排除**降级为入队优化**,不是机制本身(全量切 V2 后再补) |
| 轴 A 降频 | 两侧共用 | gate 算 `wake_interval_sec` 时套档位系数(`gate.py:195`):V2 用它推进 `next_heartbeat_at`,V1 用它算 `heartbeat_next_tick_at`(`core/store.py:102-114`)——**同一个旋钮** |
| 状态写入 | 各写各的 | V2 worker 直写;V1 走**已有**的 `POST /v1/model_api/runtime_error`(`consumer:719`,本来就在传 `error_class`)只加字段,不新建链路 |

**明确不做**(V1 退役即作废的纯浪费):把 consumer 里 `_provider_payment_cooldown_until`
/ `_proactive_fail_streak` 两个内存态刹车改成读库。过渡期 V1 继续吃现有的 600s 内存
冷却即可——gate 那一层的硬停已经能挡住主动道。

V1 退役当天需要删的,只有"V1 写点"一个函数;gate、`provider_health` 表、档位逻辑
原封不动。

## 5. 实现落点(粗)

| 改动 | 位置 | 量级 |
|---|---|---|
| `v2_wake_schedule` 加 `last_engagement_at`、`provider_state`、`provider_fail_streak` 三列 | 新 alembic migration | 小 |
| 用户发消息 / iOS tracking 上报时刷新 `last_engagement_at` | 消息写入路径 + tracking 摄入 | 小(每消息一次 UPDATE) |
| gate 计算 `wake_interval_sec` 时套用档位倍率 | `proactive/gate.py:195` | 小,**单一注入点**,scheduler 零改动 |
| 失败分类结果回写 `provider_state`/streak | `v2/worker.py:5125` 附近 | 中 |
| `due_heartbeat_users` / `due_screen_watch_users` 排除 `needs_user_action` | `v2/jobs_store.py:5191,5212` | 小(SQL 加一个条件) |
| 免模型召回推送 + 设置页"配置待修复"态 | 推送侧 + iOS | 中 |

`wake_interval_sec` 在 gate 里算、由 scheduler 直接用来推进 `next_heartbeat_at`
(`scheduler.py:63-64`)——**整个轴 A 只需要在 gate 那一个点做乘法**,这是本设计最省的部分。

## 6. 可观测性(必须与实现同批)

- admin data-track 用户详情露出:当前档位、idle 天数、`provider_state`、连续失败数;
- 舰队面板:各档人数分布 + 每日主动唤醒总数(衰减前后对比,验证省了多少);
- 复用既有超速哨兵 `admin_proactive_heartbeat_overspeed`(`db.py:1977`)——降频后
  上限口径要跟着档位走,否则哨兵会按用户原始 interval 误判。

## 7. 风险 / 反对意见

1. **"降频=削弱陪伴"**。这是 Seven 2026-07-21 撤掉旧 loop guard 的原因。缓解:恢复
   是**瞬时且无惩罚**的(发一条消息就回 T0),且降频只对**连 App 都不开**的人生效。
2. **误判活跃**:必须双信号(消息 + App 前台)。实测有 32 人属于"只读不回",单信号会误伤。
3. **T3/T4 的召回时机**:一周一次若落在凌晨等于白发。降频后**每一次都更珍贵**,
   应叠加已有的 DND/时段偏好,必要时择时。
4. **BYOK 用户可能就是想要高频**:尊重用户设置的上界(第 4 节要点 2)已覆盖;
   但"设了高频 + 一个月不开 App"仍应降 —— 需 Seven 确认这个取舍。

## 8. 已有的刹车(别重复计算收益)

未激活用户(`settings.first_chat_ok_at` 为空)**已经**被 gate 拦死
(`proactive/gate.py:203`,`activation_pending`)。实测 500 人里有 271 人从未发过消息,
其中相当一部分属于这类,**他们本来就不产生主动唤醒**。本设计的收益只应算在
"已激活但已冷却"的那部分人头上(实测 ≥7 天没露面仍在满速跑的:65 人)。

## 9. 待确认清单(给 Seven)

1. ~~T1 档位~~ 已定:第一周满血,7/15/30/90 四级递减(§A.2)。
2. L1 读者封顶在 T1 —— 认可吗?(另一种选法:读者也一路降,只是慢一档)
3. `needs_user_action` 30 天后彻底 park,还是永远保留 24h 探活?
4. 免模型召回推送的文案与频次(死 key 用户每周一条?每月一条?)
5. 轴 A 是否也要对 resident 路线生效(本设计只写了托管 V2 的落点;
   resident 的 consumer 在用户自己机器上,成本不是我们的,但用户的 key 还是他自己在烧)。
6. `≥10s 算一次阅读`的门槛值 —— 10s 是从"20% 会话 ≤5s"推的,可调。

## 10. P0 落地记录(claude2,2026-07-25)

### 改了什么

**① 分类漏洞** —— `model_not_found` 的正则只认三种措辞
(`invalid model name|model_not_found|no such model`),DeepSeek 下线 `deepseek-chat`
时的报错原文

> `API Error: 400 The supported API model names are deepseek-v4-pro or deepseek-v4-flash, but you passed deepseek-chat`

**一条都不命中** → 掉进 `unknown` / `blame=system`。而 system 档按纪律
(`docs/FRONTEND_ERROR_CONTRACT.md §二`)**不许引导用户改配置**——于是一个"改个模型名
就好"的问题,被判成"我们自己坏了",既不告诉用户怎么修,也不许告诉。**双重失灵。**

补的每条正则都对应真实观测到的上游措辞;**不做 `400 + model` 这类宽匹配**
(400 出现在太多无关报文里)。两份副本同时改:
- `tools/chat_resident_consumer.py` `_ERROR_CLASS_RULES`
- `backend/notices/catalog.py` `_UPSTREAM_RULES`(V2 侧经 `chat/chat_core.py:152` 使用)

`tests/test_catalog_consumer_parity.py` 逐字比对两份正则与次序,只改一边必红。

**② 兜底话术分道** —— 失败分支原本无条件 `agent_result = [FALLBACK_REPLY]`
(「我这会儿有点慢,刚刚没接上。你稍后再发一次,我会继续接。」)。
现在按 blame 分道:

| blame | 发什么 | 为什么 |
|---|---|---|
| `user_provider` | 分类后的**可行动话术**(如「模型名不可用,请检查设置里的模型名。」) | 永不自愈;劝重试 = 骗用户再烧一次注定失败且照样计费的调用 |
| `provider_transient` / `system` | 保持 FALLBACK_REPLY | 对它们「稍后再发一次」是真话 |

新增 `_turn_failure_reply_text()`(纯函数)+ `_suppress_duplicate_upstream_banner()`
(显式副作用,单独一个调用点)。后者盖章 `_notify_agent_turn_failure` 用的同一个节流键,
避免"可行动话术在气泡里说一遍、system 横幅再说一遍"。**设置页上报
(`_report_runtime_error`)位于该函数的节流判断之前,不受影响。**

**不动**的两处 FALLBACK_REPLY:
- `call_agent` 清洗为空(`reply_parse_failed`,blame=system)——兜底话术本来就对;
- 退化标点碎片(stream-cut,blame=provider_transient)——同上。

### 验证

```
tests/test_consumer_error_classify.py      新增 9 例(含反向防误伤:含 model 字样的 5xx
                                           仍判 upstream_unavailable,不被新正则吞掉)
tests/test_catalog_consumer_parity.py      共享样本串 +2(两份正则必须同时认)
tests/test_chat_resident_consumer.py       新增 1 例端到端:走 _process_messages,
                                           断言可见气泡=可行动话术、不含"稍后再发一次"、
                                           turn_failure_* 元信息照常下发、无重复 system 横幅
```

`test_chat_resident_consumer + test_consumer_error_classify + test_catalog_consumer_parity
+ test_chat_notice_fanout + test_chat_system_notice_role` → **546 passed**
(DB 类需 `DATABASE_URL` 指向本地 PG,见 docs/testing)。

### 顺带修好的可观测性

兜底话术是作为 **agent 消息**写进聊天流的,所以 `last_agent_at` 对一个全崩的用户
仍显示"0 天前"(usr_d98b8d68124090a6 实例)。P0 之后 `blame=user_provider` 的失败
不再产生这种假 agent 消息 —— 这正是 P1 判定"连续 48h 没有一次成功"所依赖的信号,
**所以 P0 必须先于 P1**。

## 11. P1 落地记录(codex2 实现,claude2 双签,2026-07-25)

commit `d2a635cd`(origin/test)。新增中立表 `provider_health` + migration `0057_provider_health`,
新模块 `backend/provider_health.py`。

### 最终判据(比 §B.0 初稿多一个确认窗)

```
进入 needs_user_action 需同时满足:
  ① now − baseline ≥ 48h
       baseline = last_provider_success_at,从未成功过则用 onboarding_route.selected_at
       (再兜底 active route created_at;都取不到则 baseline = now,fail-safe 不误判)
  ② 当前失败 blame = user_provider
  ③ now − user_provider_failure_started_at ≥ 1h   ← USER_PROVIDER_CONFIRM_SEC
       段起点回填规则:baseline 之后【没有】出现过其他类型失败时,段起点 = baseline
       (即"窗口内所有失败都是 user_provider",不该被额外罚站);
       前面出现过 transient/system 失败时,段起点 = 本次事件(必须再同质满 1h)
```

**③ 是复审加的**。初稿只检查"当前这次失败是 user_provider",复审用其纯函数复现出误伤:
**50 小时纯 5xx(会自愈的上游故障)+ 恢复期抖出的 1 次 401 → 立刻判定"用户该改配置"**。
低价中转在抖动/恢复期回 401/403 是常态,这不是边角。把上游的锅算到用户头上,
正是 `docs/FRONTEND_ERROR_CONTRACT.md §二` 那条纪律要防的事(P0 刚修完它的镜像版本)。

### 行为

- `needs_user_action` → **主动道全停**(gate 返回 `block_reason=provider_needs_user_action`),
  **V1 consumer tick 与 V2 scheduler 同时生效**(§4.6 的共用咽喉);
- **聊天道零改动**(全仓 grep:chat 路径无 `provider_health` 引用);
- **manual / user_initiated 唤醒豁免**(`gate.py` `if not block_reason and not manual`)——
  与"用户主动发起的永不设门"同一口径,复审加的;
- 每 24h 一次探活,`SELECT … FOR UPDATE` + 同事务 UPDATE 原子认领;
  admission 排在所有其他 block_reason **之后**,不会出现"探针被消耗、却被 DND 拦下"的浪费;
- 任意一次成功 provider 调用 / 设置页在 **active route** 上验证通过 → 立即恢复 `ok`。
  未激活的备用 route 单测成功**不会**误解封(`model_api_route_test` 只在 `was_active` 时记)。
- DB 故障 **fail-open**:这是资源优化不是授权边界,观测性故障不得升级成舰队级主动道中断。

### 写点覆盖

| 侧 | 路径 |
|---|---|
| V1 | consumer 四条车道(chat/proactive/capture/dream)的成功与失败,均经**已有**的 `POST /v1/model_api/runtime_error`,新增 `provider_result` 字段。成功上报**每进程 15 分钟节流**(48h 判定不需要每回合精度),但设置页有错时立即清;**仅在 POST 真正成功后才盖时间戳**,失败下轮重试 |
| V2 | worker 全 provider 路径直写(tool_loop 回调 + extraction + trajectory review) |

⚠️ `last_provider_success_at` **绝不能**用 `last_agent_at` 反推初值 —— P0 之前的兜底话术
是作为 agent 消息写进聊天流的,全崩用户的 `last_agent_at` 仍显示"0 天前"。

### 验证(claude2 独立复跑,非采信提交者数字)

- 纯函数边界 5 例,含两例复审自造:**从未成功 + route 选定 49h 前**(要进)、
  **死 key 中间夹一次 503 的 2h 心跳节奏**(只晚一轮,不构成活锁);
- `test_provider_health{,_unit}` 等 P1 相关:133 passed;
- CI 的 consumer 源码耦合口径 34 个文件:**1093 passed, 1 skipped**(与 P0 基线精确对齐);
- rebase 到最新 tip 后,`provider_health.py` / `gate.py` / `chat_resident_consumer.py`
  三个关键文件与双签时的 `f3e25081` **逐字一致**(diff 为空)。

### 尚未做(P2 期,等 Seven 定文案频次)

`needs_user_action` 目前只是**静默**停掉主动道:用户侧除了 P0 那句可行动话术之外没有别的提示。
key 是死的,生成不出 AI 消息,所以这个状态下的召回**只能**是免模型的模板推送 + 设置页红点。

## 12. P2 暂不做的决定与残留缺口(Seven 2026-07-25)

Seven:"P2 就是召回用户是吧,那可以先不做。"

**成立的理由**:P2 的价值主要在"把 key 已死的流失用户拉回来",属于增长动作,不是正确性
问题。P1 已经把资源浪费止住了,这才是当初的出发点。

**残留缺口比看上去小**,因为两条告知通路已经在了:

1. **聊天道**:用户一发消息就会收到 P0 的可行动话术(如"模型名不可用,请检查设置里的
   模型名"),聊天道从不设门,所以这条路永远通;
2. **设置页**:`last_runtime_error` 本来就由 `_report_runtime_error` 写入并展示,
   死 key 用户打开设置页就能看到具体报错 —— 这不是 P2 新增的东西。

**真正缺的只有一种人**:key 已死、**且**从此不再发消息、**也**不再打开 App。
对这种人,唯一的触达手段就是 P2 那条免模型模板推送。但这批人本来也已经被 P1 判定为
"停止分配资源",不主动捞回来是一致的取舍,不是遗漏。

**若将来要做**,注意 key 是死的、**生成不出 AI 消息** —— 召回只能是固定文案推送 +
设置页红点,不能走正常的主动消息链路。
