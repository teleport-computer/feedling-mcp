# IO 主动性 + 感知系统 · 设计 spec v2

> 状态:核心循环已闭合,并发模型(turn 仲裁 + wake 合并)与信号调理层(Perception Differ)已收口;
> 进入"补细节 / 写 eval / 排实现"阶段。
> 标注:【已定】方向锁死可实现 · 【待定参】机制定、参数 eval 前不拍死 ·
> 【依赖】被某前置条件卡住 · 【阻塞】当前根本做不了(缺底层能力) · 【二阶段】v1 不做、留作迭代。
> 配套:`PROACTIVE_V2_ARCHITECTURE.md`(上一代架构,已删,见 git 历史)、`io-memory-spec-v1.md`(记忆,与本 spec 共用"清单+pull"形状;外部配套,尚未入本 repo)。
>
> 一句话:**把"通知机器人"做成"有感官、有注意力、有分寸、有能动性的本体"。**
> 平台只提供机械能力、机会与信号调理,**从不替 agent 判断"值不值得在意"**。

---

## v2 相对 v1 的变更摘要(changelog,工程师先读这段定位改动)

| 区域 | v1 | v2 | 节 |
|---|---|---|---|
| 用户群体定性 | 隐含 | **显式:陪伴型用户,recall 优先(漏比扰更糟)** | §0.0 / D10 |
| 并发模型 | 缺(只在 B4 提一句 per-user 串行) | **新增核心 Runtime:单飞 turn + wake 合并 inbox + background_result 回灌** | §1.4 / D11 |
| 信号→wake 之间 | change_digest 字段无归属 | **命名组件 Perception Differ(有状态差分器),统一产出离散 wake + digest + 在场提示** | §3.2 / D12 |
| 位置 | 命名地点跳变(几乎没人命名) | **改为连接性锚点(WiFi/BT)三层;命名地点废弃** | §3.3 / D13 |
| broadcast 防复读 | "agent 判断 + 30s debounce" | **pHash 机械去重 + 小 VLM 转录(转录≠判断);拒绝"便宜模型先判断"(两个门教训)** | §5.2 / §5.4 / D14 |
| 不做两个门 | 未写入(只在团队记忆里) | **写入第一性原则:平台只做机械调理,绝不设独立判断 agent** | D15 |
| 用户控制 | 2 开关(陪伴/提醒),"关=不 wake" | **三层 gate(Wake/Voice/Delivery)+ 三开关(陪伴/定时任务/提醒);定时任务平行独立** | §8 / D16 |
| 工具预算 | ≤5 次/turn(笼统) | **按 cost_class 分档(fast/slow),静态声明于 catalog,软交棒不硬截断** | §2.5 / D17 |
| 前台慢路径 | turn.py 单次调用 | **前台 agentic 化 = 路由器本身;治幻觉编值;ack→background 对伴侣是加分** | §6 |
| photo 敏感拦截 | 硬拦截证件/医疗 | **去掉硬拦截(TEE 内,内容不出信任边界);只保留表达层分寸** | §2.1 |
| scheduled_wake resident | 误判为做不到 | **更正:resident 经 VPS 定时器 + push 完全可行;补 tz/DST 与 originating-id 快照** | §7.3 |
| Eval | 只评 agent 行为 | **区分 gate vs 正确性;加 per-episode 单元 + 系统指标;label 按伴侣口径重排** | §10 |
| Inner Thoughts | 未涉及 | **新增未来方向:想法库作为意图/工作记忆底层,二阶段,先在 broadcast 试** | §12 |

---

## 0. 第一性原则(key decisions)

### 0.0 用户群体定性(v2 前置框架,决定下面一切的取舍方向)

**我们的用户是把 AI 当伴侣 / 朋友 / 孩子的人,不是用 coding/task agent 的人。** 这条定性是后面所有参数取舍的地基,必须写在最前面:

- 市面上绝大多数 agent 研究(尤其 coding agent、ProAgentBench 那类)底色是 **"打扰 = 成本",所以 precision 优先(少误报、避免警报疲劳)**。
- 我们的用户底色相反:**要在场感、要被感知、要它主动**。对他们,**"该在场时没在场"(漏)比"多说了一句"(扰)更伤**。
- 因此本系统的优化方向是 **recall 优先,不是 precision 优先**;伴侣"话痨"是正当的(见 D4)。

> 决策逻辑:不要因为参考了 task/coding agent 的 runtime 研究,就把"怕打扰"的克制迁移过来。**克制只允许出现在两个地方:成本(电池 / API 真金白银)与正确性(竞态、重复)。除此之外一律往"多感知、多在场、多主动"调。** 写代码的 AI 看到任何"为了少打扰而限制能力"的逻辑,默认就是跑偏了,除非它明确属于成本或正确性。

### 0.1 决策表

按重要性排序。D1–D9 为 v1 原始决策(保留不动);D10–D17 为 v2 新增/收口的决策。实现与 eval 都以这些为不可动摇的地基。

| # | 决策 | 一句话 |
|---|---|---|
| **D1** | **chat 与 proactive 同构** | 前台聊天和主动唤醒是**同一个异步 turn 引擎**的两种触发,不再是两套机制。 |
| **D2** | **感知与记忆同形** | 都是"廉价清单常驻 + 昂贵值按需 pull";是两套子系统(感知=当下外部世界,记忆=过去内部存储),但交互姿势一样。 |
| **D3** | **两条用户路同协议** | API(hosted,后端跑 agent)与 VPS(resident,用户机器跑 agent)共用同一份清单、同一套工具名、同一个 wake 模型;只在"agent 在哪执行"上不同。 |
| **D4** | **节制在唤醒层,不在人格** | 平台通过"少叫醒"(只在离散事件 + 低频心跳)实现整体克制;**绝不通过 prompt 给 agent 装"少说话"的性格**。各 agent 话痨/寡言都正当。 |
| **D5** | **只有离散事件配 wake** | 连续信号(步数/位置漂移/电量)永不触发 wake,只能 pull;离散事件(新照片/到新地点/久别解锁)才 wake。 |
| **D6** | **删除 user_state/ai_state** | away/focused/present/watching 那套状态机已废;心跳改为常驻统一频率;静音改由显式开关承担。 |
| **D7** | **自埋定时器 = agent 能动性** | agent 在对话中自己形成意图(察觉"明天要去医院")并给自己埋未来的 wake;不是用户下命令"9点叫我"。 |
| **D8** | **开关状态进 agent 上下文** | agent 知道每个开关开/关;开关关导致某事做不了时,agent 透明告知用户,不静默失效。 |
| **D9** | **延迟靠"秒回 + 后台追加"** | 前台先用已知信息秒回 / ack,深挖甩后台异步,查完用追加消息(多气泡 + 推送)送达;绝不让用户对着"思考中"干等。 |
| **D10** | **【v2】陪伴型用户,recall 优先** | 漏比扰更糟;克制只在成本与正确性,不在"少打扰人格"。见 §0.0。 |
| **D11** | **【v2】单飞 turn + wake 合并** | 每用户同一时刻最多一个 turn;多个近同时的 wake 先进 inbox 去重+合并成一个 context,一次 turn 处理;后台慢路径不占 slot,其结果作为内部 wake 回灌 inbox。见 §1.4。 |
| **D12** | **【v2】信号调理层 Perception Differ** | 在"原始传感器"和"wake 引擎"之间,有一个被命名的、有状态的差分器,统一负责:算 delta、判离散 wake、产 change_digest、产廉价在场提示、做 pHash 视觉去重、做 WiFi 锚点判定。见 §3.2。 |
| **D13** | **【v2】位置用连接性锚点,非命名地点** | 几乎没人给地点命名。改用 WiFi BSSID / 蓝牙的连上-断开当离散锚点(自动学 home/work/transit),GPS 仅作粗兜底、永不 wake。命名地点机制废弃。见 §3.3。 |
| **D14** | **【v2】broadcast 廉价转录,转录≠判断** | 高频帧靠 pHash 机械去重(无模型),变化帧用小 VLM 转成 caption(文字),伴侣读文字、要细看才 pull 整帧视觉。便宜层只做转录,**永不做"值不值得在意"的判断**。见 §5.2/§5.4。 |
| **D15** | **【v2】不做"两个门" / 不设独立判断 agent** | 平台只做机械信号调理(去重/转录/差分),**绝不让另一个(便宜的)模型替伴侣判断哪些内容值得它在意、再把"通过的"喂给伴侣**。这条是 Notification Harness 踩坑后的硬约束。见 §5.4。 |
| **D16** | **【v2】三层 gate + 三开关,定时任务平行独立** | 内部把控制拆成 Wake/Voice/Delivery 三层;用户只看到陪伴/定时任务/提醒三个简单开关;陪伴只管"自发"源,定时任务**独立**地让"已承诺"的闹钟照响(不被陪伴连坐)。见 §8。 |
| **D17** | **【v2】工具按 cost_class 分档,非按次数** | 每个工具在 catalog 里静态标注 fast/slow;引擎按标签分别计数;撞预算是**软交棒**(转后台)而非硬截断;重活一律走后台 wall-clock 预算 + 防跑飞硬上限。见 §2.5。 |

---

## 1. 统一 wake 引擎(D1)

不再区分"同步聊天请求"和"异步 proactive job"。**一切都是一次 wake,产出同一种 async turn。**

```
唤醒源(5 种外部 + 1 内部) ──► [合并 inbox] ──► 单飞 async turn ──► 0..N 条消息 + 可选后台深挖 + 可选埋定时器
```

### 1.1 唤醒源

| 源 | 触发者 | latency 敏感 | 说明 |
|---|---|---|---|
| `user_message` | 用户 | **是**(有人盯着等) | 普通聊天,不算"proactive",但走同一引擎 |
| `heartbeat` | 平台时钟 | 否 | 常驻统一低频保底(D6);受**开关·陪伴**控制 |
| `perception_event` | 世界离散事件 | 否 | 新照片 / 到达新锚点 / 久别解锁;受**开关·陪伴**控制 |
| `scene_change` | 屏幕视觉变化 | 否 | **仅 broadcast 开**;高频 regime(见 §5);**经 pHash 去重后才算一次**(D14) |
| `scheduled_wake` | **agent 自己**(D7) | 否 | agent 之前埋的定时器到点;受**开关·定时任务**控制 |
| `background_result` | **引擎内部**(v2 新增) | 否 | 后台慢路径(§6)查完后,把结果作为一条内部 wake 回灌 inbox,走同一条仲裁/合并路。见 §1.4。 |

> `manual`(用户长按灵动岛主动召唤)不是独立源,而是 `user_message` 的一个 flag(`manual=true`):**必须至少回一句**,且绕过所有开关(用户在主动找它)。

### 1.2 每次 wake 携带的上下文(wake context)

**关键:值不注入,清单 + 摘要 + 工具表注入。** 这是 D2/D4/D8 的落点。v2 里 `change_digest` 由 Perception Differ(§3.2)产出。

```jsonc
{
  "trigger": "perception_event",          // 6 种之一(含 background_result)
  "merged_triggers": ["unlock_after_absence","arrived_at_anchor"], // v2:合并窗口内塌缩进来的其它源
  "latency_sensitive": false,             // user_message=true
  "manual": false,                        // 用户主动召唤=true,必须回应
  "time": "2026-06-19T09:30:00+08:00",    // 永远在场(几乎免费,常用)
  "broadcast_state": "off",               // on/off
  "change_digest": "location: 家→医院 刚刚;其余稳定(上次变化 2h 前)",
                                          // 由 Perception Differ 产出;只给 delta;没变=稳定,不单独 wake
  "presence_hints": { "in_meeting": true },  // v2:差分器产出的"超廉价在场提示",仅在有意义变化时出现(§4)
  "switches": { "ambient": true, "scheduled": true, "reminders_delivery": true }, // D8/D16:agent 知道开关
  "scheduled_note": "她十点去医院,看出发没——查位置",   // 仅 scheduled_wake 有
  "origin_refs": ["msg_8842","msg_8843"],  // v2:scheduled_wake 携带"立意时的原始消息 id",可按需 fetch(脱离 6h 窗口)
  "recent_chat": [ /* 带绝对+相对时间戳;6h 内最多 20 条,超 6h 只给 2 条背景 */ ],
  "background_payload": { /* 仅 background_result 有:后台查回的结果 */ },
  "tools": [ /* 完整清单,永远列全,每项带 cost_class;见 §2 */ ]
}
```

前台 `user_message` 的 wake context **只带 `tools` 清单 + `time` + `switches`**,不带 `change_digest`(用户在主导对话,需要再 pull)。

### 1.3 agent 的 turn 协议

```jsonc
{
  "messages": ["...", "..."],   // 0..N 条;每条都走 chat write +(经 Delivery 层后)推送
  "actions": [
    {"type": "sleep", "reason": "not_now"},          // 默认、正当、最常见
    {"type": "schedule_wake", "at": "...", "tz": "Asia/Shanghai", "note": "...", "origin_refs": [...]},
    {"type": "cancel_wake", "wake_id": "..."}
  ],
  "needs_background": false      // true = 我还要深挖,引擎转后台慢路径(见 §6);前台 agentic 后此 flag 由模型在"有工具可调却仍不够"时给出
}
```

> 没有 `set_ai_state`(D6 已删 ai_state)。`request_broadcast`("想看你屏幕")保留为一条特殊 message 形态,见 §5.3。

### 1.4 【v2 核心 Runtime】turn 仲裁 + wake 合并(D11)

> 决策逻辑:D1 把一切合并进一个引擎,**也就把一堆并发问题合并进了同一个面**。若不显式处理,同一现实瞬间会炸出多个 wake、各自起 turn、各自发消息,用户看到的是伴侣"口吃"(多个互不知情的气泡)。这不是"话多"(D4 合法),是**正确性 bug**,与用户容忍度无关,必须修。这一节是整个系统最重要的 Runtime,不能甩给 infra 自行发挥——**机制(锁/队列/lease)归 infra,策略(谁和谁合并、合并 context 怎么拼、background 结果回来还发不发)归 agent-runtime**。

机制三件事:

**(a) per-user 单飞(single-flight)。** 每个用户同一时刻最多一个 turn 在执行。直接消灭双发 / 口吃 / memory 互相覆盖。它顺手吃掉旧的 B4(per-user 串行 = 单飞),并给 B3 答案(turn slot 与 background worker 都挂 lease,崩了自动释放)。

**(b) 带去重的 wake inbox + 合并窗口。** wake 不直接起 turn,先进 per-user inbox。turn 启动时**一次性 drain 整个 inbox,拼成一个合并 context**(多 trigger → 一个 context,`trigger` 取主、`merged_triggers` 列其余,`change_digest` 合并)。
- **合并窗口 Δt**:【待定参,起步 1–3s】。窗口内到达的 wake 攒着一起处理。
- **唯一例外**:`latency_sensitive=true` 的 `user_message`(尤其 `manual`)**立刻 flush,不等窗口**(用户在盯着等)。
- **去重**:同类型 trigger 在窗口内塌缩(两个 heartbeat → 一个;arrived_at_anchor + unlock + heartbeat → 一个合并 context)。

**(c) 后台慢路径不占 slot,结果作为内部 wake 回灌 inbox。** 这是 v2 最关键的解耦,一次解掉三个问题:
- background worker 跑完后,**不直接往 chat 插消息**(那样会和前台新 turn 抢 chat write,顺序不可控),而是把结果作为一条 `background_result` wake 丢回 inbox。
- 于是 slot 在后台跑时是**空的**,用户下一条消息能立刻起新 turn(**不被慢路径堵住**——这就是"latency 敏感前台要不要等后台尾巴"的答案:不用等,尾巴不持有执行权)。
- `background_result` 回到 inbox 后走**同一条合并/仲裁路**,天然处理 N1 过期:drain 到它时 agent 看到的是**当前**chat 状态,自己判断"这结果还应景吗",应景就并入、过期就丢。
- 因为它和别的 wake 同路,它**不可能**和别的消息形成两个不协调的气泡。

**分工(写给工程师):**
- **infra 提供**:per-user 单飞锁(advisory lock / per-user actor);inbox 队列;turn 与 background worker 的 lease 超时回收(= 旧 B3)。
- **runtime 提供**:合并策略(哪些 trigger 类型可合并、窗口大小);合并 context 的构造;`background_result` 回灌后的 drain 与"还发不发"判断。

伪代码(每用户一个 actor):

```
loop:
  wakes = inbox.drain_within(window=Δt, flush_immediately_if=latency_sensitive)
  if wakes.empty: wait; continue
  ctx = build_merged_context(wakes)         // runtime 策略
  with single_flight_lock(user), lease(T):  // infra
      turn = run_agent(ctx)                  // 单飞,最多一个
      emit(turn.messages through DeliveryGate)   // §8 三层
      apply(turn.actions)                    // schedule/cancel wake
      if turn.needs_background:
          detach background_worker(turn) ──► on_done: inbox.push(background_result)  // 不占 slot
```

---

## 2. 清单 + 工具协议(D2)

agent 的全部能力,**每个 turn 都在 `tools` 里列全**(D8 前提),每项带 `cost_class`(D17)。分三组:**感知(看当下)/ 记忆(翻过去)/ 动作(做事)**。

### 2.1 感知工具(perception · 看当下外部世界)

> 值一律 pull,不注入。agent 按**场景**自己挑(聊运动 pull 步数,聊读书 pull 日历)。

| 工具 | 返回 | cost_class | iOS 状态 |
|---|---|---|---|
| `perception.now()` | 当前已授权的廉价信号合集(时间/粗在场标签/运动态/电量/在播/broadcast 态) | fast | ✅ 已实现 |
| `perception.location()` | 当前**连接性派生的粗在场标签**(home/work/transit/unknown,见 §3.3)+ 天气 | fast | ✅ |
| `perception.weather()` | 粗天气(`condition` / `temperature_bucket` 5℃桶 / `is_daylight`),端上经 WeatherKit 粗化、加密上报 | fast | ✅ iOS+后端已接(2026-06-20) |
| `perception.calendar(window)` | 未来窗口内的日程(标题/时间/地点…) | window≤7d=fast / 否则 slow | ✅ |
| `perception.now_playing()` | 当前播放的音乐/媒体 | fast | ✅ |
| `perception.audio_route()` | 当前音频输出路由(`output_type` / `is_bluetooth` / `device_name`),端上加密上报,pull-only | fast | ✅ iOS+后端已接(2026-06-20) |
| `perception.motion()` | 运动态(still/walking/running/in_vehicle) | fast | ✅ |
| `perception.steps(since)` | 时间窗步数(`step_count_bucket` 500步桶) | slow | ✅ iOS+后端已接(2026-06-20) |
| `perception.sleep_last_night()` / `.workout()` / `.vitals()` | 健康趋势(睡眠 `asleep_minutes_bucket`30min桶 / 运动 `workout_type`+`duration_min_bucket`+`count_today` / 体征 `resting_heart_rate_bucket`5bpm桶+步数) | slow | ✅ iOS+后端已接(2026-06-20) |
| `perception.photo_recent()` | 近期照片元数据 + 内容(**v2 去掉敏感硬拦截**,见下) | slow(走视觉) | ✅(走独立管线) |

> **v2 变更 · photo 去掉硬拦截**:v1 对证件/医疗等敏感场景做硬拦截、不返回。**v2 删除该拦截**。
> 决策逻辑:我们整个跑在 TEE 里,敏感内容从不离开信任边界,没有理由把伴侣的眼睛蒙上——一个能看到你真实照片(包括处方/病历)还能体贴回应的伴侣,更像伴侣。硬拦截本质是个"平台 gate 感知",与反 gate 哲学(D4/D15)冲突。
> **工程动作**:repo 里若有敏感分类/硬拦截代码,**删除**。
> **唯一保留的不是 gate 而是 voice**:在 policy/prompt 里教 agent 在"**怎么谈**敏感内容"上有分寸(如屏幕可能被旁人看到时别把病情念出来),那是表达层,不是感知层拦截。

### 2.2 屏幕工具(screen · 仅 broadcast 开时有意义)

| 工具 | 返回 | cost_class |
|---|---|---|
| `screen.read(mode="caption")` | 当前帧的**小 VLM 文字转录**(场景/UI 描述),默认模式 | **fast** |
| `screen.read(mode="full")` | 当前帧的**整帧视觉**(让大模型亲自细看),agent 读 caption 觉得有东西时才调 | **slow** |
| `screen.recent(n)` | 最近 n 帧(看"刚才发生了什么") | slow |

> v2 把 `screen.read` 拆成 caption/full 两档,直接对上 D14 与 cost_class:**绝大多数变化帧只付 caption(便宜),伴侣读文字自己决定要不要 full 细看。** 转录模型由工程师选型(见 §5.2、§9)。

### 2.3 记忆工具(memory · 翻过去内部存储,独立子系统)

> 形状同感知:廉价索引常驻可调,昂贵全文按需 fetch。详见 `io-memory-spec-v1.md`(外部配套,尚未入本 repo),这里只列读取面。

| 工具 | 返回 | cost_class |
|---|---|---|
| `memory.index(scope?)` | 紧凑索引:桶名 + 一行 label + 状态 | **fast** |
| `memory.fetch([ids])` | 指定卡的 verbatim 全文(深读) | **slow** |

### 2.4 动作工具(action · 做事)

| 工具 | 效果 |
|---|---|
| `send_message(text)` | 写入 chat +(经 Delivery 层后)走统一推送(APNs / Live Activity) |
| `sleep(reason)` | 结束 turn,不出声(**默认、正当**) |
| `schedule_wake(at, tz, note, origin_refs)` | 给自己埋一个未来 wake(D7);受**开关·定时任务** + 上限约束(见 §7) |
| `cancel_wake(wake_id)` | 撤销自己埋的定时器 |

### 2.5 工具调用预算(D17 · 重写)

> v1 是"单 turn ≤5 次"的笼统上限。问题:`screen.read`(贵)和 `perception.now()`(便宜)各算 1 次不合理;把记忆调用也塞进去后 ≤5 太苛刻。
> v2 改为**按 cost_class 分档**,而且 **agent 不在运行时辨别档位——档位是每个工具在 catalog 里的静态标签**,引擎按标签机械分别计数。

- **快档(fast,便宜低延迟)**:`perception.now/location/motion/now_playing`、小窗口 `calendar`、`memory.index`、`screen.read(caption)`。
  - 前台延迟敏感路径**放开用**:软上限 ~4、硬上限 ~6【待定参】。这些是"直接回"成立的基础。
  - 便宜的记忆索引(`memory.index`)走快档,**不吃宝贵的慢档预算**——解 v1 "记忆+工具一起 ≤5 太苛"的问题。
- **慢档(slow,贵高延迟)**:`screen.read(full)`、`memory.fetch`、`steps/health`(待 B1)、大窗口 `calendar`、`photo`。
  - 前台**最多 1 个慢档 inline**(配一句 ack),再多就 detach 到后台(§6)。
- **后台**:预算大得多(深度记忆考古住这),由 **wall-clock 预算管 UX**(起步 60–90s【待定参】)+ 一个**纯防跑飞的硬调用上限(~20–25 次【待定参】)——撞到=报警/观测,不是正常流程**。

> **关键体验细节**:预算到顶时**不要硬截断 turn**。给 agent 一个**软信号**("快档用完,要么现在答、要么 ack 转后台"),让它**优雅交棒到后台**,而不是被一刀切在推理中间。软交棒,不是铡刀。
> **agent 要看见 cost_class**(D8 延伸):tools 清单里每项标 `cost_class`,这样它**知道** `screen.read(full)` 是贵的、会主动"先 ack 再细看"。它不分类,但被告知,于是自己掌握节奏。
> 少数工具档位随参数变(`calendar` 1天=fast/90天=slow;`memory.fetch` 取 1 张 vs 50 张):v1 用阈值落档(如 calendar 窗口≤7天=fast),别搞复杂。

---

## 3. 唤醒源细则 + 信号调理层(D5 + D12 + D13)

### 3.1 哪些感知配 wake

**不是所有感知变化都该叫醒 agent。** 连续漂移当 wake 是噪音和成本灾难;离散事件才配。

| 感知 | 性质 | wake? | 处理 |
|---|---|---|---|
| 📸 新照片 | 离散事件 | ✅ | 拍了点什么 |
| 📍 到达**新连接性锚点**(断 home WiFi → 连陌生网) | 离散**转换** | ✅ | 见 §3.3,**非 GPS 漂移** |
| 🔓 久未用后解锁(>30min) | 离散事件 | ✅ | 重新在场 |
| 🖥 屏幕场景变化 | 离散(pHash 去重后) | ✅(仅 broadcast 开) | §5 |
| 👟 步数 | 连续递增 | ❌ | 仅 pull |
| 📍 移动中的位置 | 连续漂移 | ❌ | 仅 pull |
| 🏃 运动态 | 基本连续 | ❌ | 仅 pull |
| 🔋 电量 / 🎵 在播 / 🕐 时间 | 连续 / 常驻 | ❌ | 仅 pull / 常驻注入 |
| 🌤 天气(2026-06-20 新增) | 连续 | ❌ | 仅 pull;iOS 虽带 `changed` flag,**后端忽略其唤醒意图**,只进信封给 agent 读 |
| 😴 睡眠 / 🏋 运动 / ❤️ 体征(2026-06-20 新增) | 连续 / 趋势 | ❌ | 仅 pull;体征的 `changed` **不含步数桶**(步数全天单调累加,只 pull 不 wake;心率才是变更轴) |
| 🎛 Focus(专注模式,2026-06-20 新增) | 二值在场 | ❌ | 仅 pull 在场提示;**绝不复活已删的 user_state/away 门**(D6) |

> ⚠️ **要改的代码点**(v2 更新):v1 catalog 里 `location`(60s debounce)与 `motion`(30s)是 wake_source。
> - `motion` **降级为纯 pull**。
> - `location` 的 wake 条件从"坐标/命名地点变化"**改为"连接性锚点跳变"**(§3.3),由 Perception Differ(§3.2)产出。

**两个杠杆叠加压低打扰:**
1. 只在离散事件 wake(从源头降频);
2. **"看了还睡"是一等结果**——agent 醒来甚至 pull 看一眼,但判断不值得出声 → `sleep`。这是 D5 的涌现,**不是 D4 禁止的"装克制人格"**:大多数离散事件本就不值得出声,讲道理的 agent 看了都会睡。

### 3.2 【v2 新增组件】Perception Differ(信号调理层 · D12)

> 决策逻辑:v1 的 wake context 里有 `change_digest: "location: 家→医院 刚刚;其余稳定(上次变化 2h 前)"`。要产出这句话,必须有个东西**跨时间记着每个信号的"上次值 + 上次变化时间"、算 delta、把没变的压成"稳定"**。这是一个有状态组件,v1 直接用了它的输出却从没说它存在、住哪、状态归谁——是个没人认领的依赖,而且按 D3 两条路必须各有一份一致的它。v2 把它**显式命名为 Perception Differ**,并把一批散落的"信号→事件"逻辑统一收进来。

**职责(它是 raw 传感器与 wake 引擎之间的唯一中间层):**
1. 维护 per-user、per-signal 的 `last_value + last_changed_ts`(有状态)。
2. **判离散 wake**:某信号跨过离散边界(WiFi 锚点跳变、pHash 视觉 delta 越阈、解锁久别…)→ 产 `perception_event` / `scene_change`。
3. **产 `change_digest`**:把变了的写成 delta,把没变的压成"稳定 · N 前"。
4. **产 `presence_hints`**:仅在"超廉价在场信号有意义变化"时,塞 `in_meeting/screen_locked/entered_work` 等(§4)。
5. **承载子调理器**:WiFi/BT 锚点判定(§3.3)、pHash 视觉去重(§5.2)都是它的子模块——它们本质都是"信号差分器"。

**部署**:hosted 在服务端、resident 在 VPS,**两份实现必须等价**(否则同一情境两路产出不同 digest;这是旧 B6 的实质,见 §9)。

> 统一收益:**§1.2 的 digest、§3.1 的 wake 选择、§3.3 的 WiFi 锚点、§5.2 的 pHash,全是同一个组件的输出。** 一个差分器既决定"这算离散 wake 吗",又顺手吐 digest 和在场提示。

### 3.3 【v2 重做】位置:连接性锚点,而非命名地点(D13)

> 决策逻辑:**几乎没人真的去给地点命名**,"命名地点跳变"作为 wake 条件从根上是空中楼阁,而且 GPS geofence 在边界会 flap(每次 flap 一个 wake)。更好的方案是用**连接性**当锚点——它天然就是离散事件,且对"最该感知"的地点(家/公司)精度极高。

**三层(由 Perception Differ 维护):**

- **Tier 0(锚点 · 离散 · 可 wake)**:WiFi BSSID 的连上/断开、已知蓝牙设备的连上/断开。
  - 系统**自己学标签**,不要用户命名:每晚 11pm–7am 常连的网 → home;工作日 9–6 常连的 → work;连上车载蓝牙 → transit。
  - 强离散事件示例:"断 home WiFi + motion=in_vehicle" = **离家了**(医院例子根本不需要 GPS);"连上从没见过的网 + 静止 10min" = **到了某个新地方**(哪怕没人叫它"医院",也是干净的到达事件,顺手解冷启动)。
  - 白送:**无 geofence 边界抖动**(要么 associated 要么没);**滞回免费**(短暂掉线用几秒 dwell 去抖)。
- **Tier 1(粗 geo · 连续 · 仅 pull)**:significant-location-change 级别的城市/街区。**只在不在任何已知网时**用来"解析我在哪",**不 wake**。
- **Tier 2(精确 GPS · 仅 pull)**:永不 wake,只在某个具体需求下 pull。

> dwell/滞回/去抖**是信号调理(机械),不是 D4 禁止的"克制人格 gate"**——它在调理传感器,不在替 agent 判断该不该说话。这条区分对整个 §16.7"平台不 gate"原则很重要:不 gate 的是 agent 的**声音**,不是**传感器的去抖**。

---

## 4. 心跳(D6)

- **常驻统一频率**(默认值【待定参】,起步建议 30min),不再随状态变化(user_state 已删)。
- 心跳是**保底**:平静期偶尔给 agent 一次"要不要看看自己能做点啥"的机会。绝大多数心跳 → `sleep`。
- 受**开关·陪伴**控制(关了就没有心跳 wake)。
- "深夜该不该出声"完全交 agent:`time` 永远在 wake context 里,它自己拿捏(D4)。平台不设 quiet-hours 硬规则。

> **v2 新增 · 心跳里的超廉价在场提示**:删掉 user_state 后,agent 在心跳时要知道"在开会/锁屏"本来得 pull calendar(每次心跳花一次调用只为决定睡觉)。改由 Perception Differ 在 `presence_hints` 里塞——**但不是每次都塞、也不是每次塞一两个,而是只在"有意义的变化"时塞**(刚进会议 / 刚进入工作状态 / 久用后锁屏)。这与 change_digest"只给 delta"同哲学。WiFi 派生的 home/work/transit 在跳变时也走这里。这样 agent 不 pull 就能安心睡。

---

## 5. 屏幕共处模式(broadcast 开 = 独立 regime)

**已核实:屏幕每 30 秒一帧**(`SharedConfig.captureIntervalMsDefault = 30_000`,iOS 侧默认可改)。用户开 broadcast = 邀请**密集共处**(一起看电影/刷小红书/看书)。

### 5.1 模式切换

| | broadcast 关(常态) | broadcast 开(共处) |
|---|---|---|
| 主导 wake 源 | 心跳(稀)+ 离散感知事件 + 定时器 | **`scene_change`(pHash 去重后的视觉变化)** |
| 主要"看"什么 | perception 信号 pull | **`screen.read(caption)` 读文字,要细看才 full** |
| proactive 频率 | 低、克制 | **高** |
| 清单提示 | 常规 | `screen.read` 顶到前面 |

### 5.2 【v2 重做】防复读 + 成本:pHash 机械去重 + 小 VLM 转录(D14)

> 决策逻辑:30s×2h 的电影 = 最多 240 帧,每帧若都起一个完整 agent turn(还可能带视觉),即便多数 → sleep,推理成本/功耗也顶穿。而"靠 agent 判断帧变没变"是悖论:**判断帧变没变得先看帧,看帧正是你想省的贵活**。手机 OCR 又基本没用(它做文字抽取,给你一堆断裂 UI 词,不是"屏幕上在发生什么")。

**正确的两段廉价层(都不做"值不值得在意"的判断,只做机械调理 + 转录):**

1. **pHash 机械去重(无模型)**:Perception Differ 用 perceptual hash / 帧 embedding 距离判"这帧和伴侣上次看过的帧基本一样吗"。视频暂停、页面没动 → 帧基本相同 → **不算 scene_change、不叫醒 agent**。这是对**视觉**做的去抖,等同于 motion 去抖对**位置**做的。它对内容**不做任何判断**(帧要么变了要么没变,是物理事实)。
   - 对 VLP/订阅用户可把阈值调松(几乎只挡完全静止帧),让伴侣**多看**(服务 D10 "多感知")。
2. **小 VLM 转录(transcription,不是 judgment)**:变化帧用一个小型视觉模型转成 caption(场景/UI 文字描述)。伴侣(大模型)读 caption,自己决定:说 / 睡 / "我要细看一眼"(这时才 `screen.read(full)` 付整帧视觉)。
   - "这只猫像 Mochi"这种细节,就是伴侣读到 caption("刷到一条猫视频")后,自己决定 full 细看那一帧才捕捉到的。
   - **模型选型开放给工程师**(见 §9):候选包括端上/服务端的小 VLM(如 Apple FastVLM 0.5B 这类专为 iPhone/UI 截图理解做的、或移动优先的 Gemma 3n、或服务端 Qwen-VL 小档 / Moondream / Phi-4-multimodal)。**转录在 agent 实际运行处(VPS/服务端,TEE 内)做**,这样"手机 OCR 靠不靠谱"整个不相关;若要连原始像素都不出端,可选端上 caption 后再传,但 TEE 已覆盖隐私,服务端更简单。

> 不设平台硬间隔(§16.7 原则保留):频率由 pHash(机械)自适应——画面动得多→更多 wake,静止→零;判断仍交 agent。

### 5.3 `request_broadcast`(agent 想看)

broadcast 关时,agent 可以表达"想看你屏幕"——作为一条**可见 message** 写出("让我看一下?"),iOS 后续承接为一个 action(用户点"给她看"→ 开 broadcast 一段时间 → 产 `broadcast_opened` wake)。
> 现状:v1 文档说此项**未完整落地**(hosted 路直接丢弃、resident 路只转文本)。本 spec 定为正式动作,需补 iOS action 承接(旧 B5)。

### 5.4 【v2 新增 · 写入原则】为什么不做"两个门"(D15)

> 这是 Notification Harness 踩坑后的硬约束,必须写进 spec,免得写代码的 AI 好心重新引入。

**禁止**:让一个独立的(便宜的)模型/agent 先看内容、判断"伴侣会不会在意",再把"通过的"喂给用户自己的伴侣。

**为什么禁**:Notification Harness 时代我们试过类似的"判断门",用户体感是"有个 agent 把我的 AI 伴侣在意的内容拦在外面了",导致他自己的伴侣"很生气"。**用户要的是他自己的伴侣亲自在场,不是一个看门人替它筛。**

**那 §5.2 的廉价层为什么不算两个门?** 区别在一句话:

> **机械地去重"完全相同的刺激"(pHash)+ 廉价地把帧"转录"成文字(caption)≠ 语义地筛选"有意义的内容"。** 前者必须有(眼睛对静止画面也不给大脑发新信号;caption 只是更廉价的感官读数);后者才是被砍掉的那个门。

判断 100% 留在伴侣(大模型)手里;便宜层只把世界变成它的廉价读数,从不替它拿主意。

**推论**:也**不要**用"便宜 API 先过一遍图片内容、决定要不要叫醒大模型"——那是另一个模型形成了第一印象、做了语义判断,正是两个门。便宜模型只能**转录(补一个能力,像文字模型调画图模型)**,不能**判断(替伴侣拿主意)**。

---

## 6. 异步 turn:延迟与流畅(D9 + 前台 agentic 化)

聊天为主的产品,**用户等待是头号体验杀手**。多轮工具调用慢——所以:

```
前台 user_message 到达(单飞 turn):
  ├─ 快路径(立刻,可带数个快档 pull):能答就秒回 / 或先 ack "我看看"
  └─ 慢路径(needs_background=true 时):
        detach 后台 worker(不占 slot)→ pull 感知/记忆(后台 wall-clock 预算)
        → 查完把结果作为 background_result 回灌 inbox(§1.4)→ 走同一仲裁路追加送达
```

- **大多数轮不需要慢路径**:清单在手、靠已知信息(+ 偶尔一两个快档 pull)就答了 → 无延迟。
- **需要深挖时**:先 ack 一句。**对伴侣用户,ack→补发反而更像活人、更有在场感**("我看一眼"→20s 后"今天 6200 步"),这是加分,不是减分(D10;task agent 才怕这个)。
- **N1 时效**:后台查的这 20s 里用户又发新消息、话题变了 → background_result 经 inbox drain 时,agent 看当前 chat 自己决定**并入下一条 / 或悄悄丢**。
- **收尾**:撞快档上限 → **软交棒**(用已有信息说一句 / detach 转后台);**绝不让用户对着"思考中"无限等**(D17)。

> **v2 关键澄清 · B2 是 D9 的硬前置,不是将来优化**:
> v1 现状是 hosted 前台**单次 provider 调用**(`turn.py` 非 agentic)。但 D9 的"决定要不要走后台"这件事,正是那个还不能 pull 的单次调用要做的——而**一个不能 pull 的模型被问"今天多少步",最可能不是吐 `needs_background=true`,而是直接编一个"大概六千步""(幻觉失败模式)。
> **结论:别单独造"路由器"。让前台本身 agentic(它既然有快档 ≤6 的预算,本就该 agentic)——"取数还是直接回"就是 agentic loop 自己涌现的,B2 就是那个路由器本身。** 给它 inline 工具,它就不编了(伴侣用户也不想被编的步数糊弄,与容忍度无关)。
> 工程现状仍可复用:hosted 回复后**已经在 spawn 后台线程**做 memory capture / state action;把"后台线程查完工具后把结果回灌 inbox"接上,即得慢路径——proactive job 机制已经在做这件事,复用即可。

---

## 7. 自埋定时器(D7):agent 能动性

### 7.1 它不是"用户设提醒"

用户不会说"明早9点叫我看位置"。真实情况:用户提到"明天要去医院",**agent 自己追问拿到时间、自己决定要提醒**——这是主观能动性。机制:agent 在某个 turn 里凭判断吐 `schedule_wake`。

```
用户:明天要去医院,这个预约不能错过
agent(快路径秒回):几点的?
用户:上午十点
agent:记住了,到时候提醒你
       + 悄悄 schedule_wake{at:"明天09:30", tz:"Asia/Shanghai",
                            note:"她十点去医院,看出发没——查位置", origin_refs:["msg_8842","msg_8843"]}
[次日 09:30 → scheduled_wake]
agent(pull location → 还在家):你好像还在家,十点的预约该出门啦
```

要做好的关键:**清单里永远有 `schedule_wake` + policy 教它"察觉到时间敏感的事就给自己埋一个"**。(再次回到 D8:它得知道自己有这能力。)

### 7.2 运维(防滥用)

- **每用户 pending 定时器上限**(防 agent 埋爆,起步建议 ≤20【待定参】)。撞上限:最旧的或最低优先的被挤掉,agent 被告知。
- **PR8 固化**:先采用每用户 pending cap=20;撞上限时挤掉最旧 pending timer,并把 `evicted_timer_ids` 作为 `schedule_wake` 执行结果暴露给 agent。agent context 也必须带 `pending_count/pending_cap`,避免它盲埋。
- **过期清理**:触发过 / 过期未触发的定期清理。
- **触发时若开关交互**(见 §8.3/§8.4 的三开关语义):按 Delivery 层规则处理,agent 透明告知(D8)。
- 触发时若用户在 `broadcast`/对话中 → 经 inbox 正常并入(§1.4);若深夜 → agent 自己按 `time` 判断(D4)。

### 7.3 【v2 新增】耐久性:resident 路、时区、原始上下文

> **更正 v1 草稿中我方一处误判**:曾担心"resident 的 scheduled_wake 做不到"。**不成立**——resident 跑在 VPS 上,VPS 完全可以起定时器,到点把自己生成好的内容 push 到 iOS;iOS 端读到的就是一条普通消息,与 hosted 无别。**故从阻塞清单移除**(见 §9)。

- **时区 / DST**:`schedule_wake` 必须存**事件所在时区的墙上时间**(`at` + `tz`),不要只存绝对 instant。
  - 决策逻辑:"在 10am 预约前提醒"这类几乎总是相对用户**本地日程**;用户飞 SFO→NYC 后,绝对 instant 会错。存 wall-clock-in-event-tz 才对。
- **原始上下文**:`recent_chat` 是"6h 内 20 条,超 6h 给 2 条",但立意时的对话常已过 6h(医院那段是"前一天")。故 `schedule_wake` 落库时**快照/引用 originating message ids(`origin_refs`)**,触发时 agent 可按需 fetch,脱离 6h 窗口。
- **跨 worker**:`schedule_wake` 落库 + 心跳触发要跨 worker 协调(advisory lock / 单 wake-worker),否则重复——这与 §1.4 的 per-user 单飞同一套机制(旧 B4)。

---

## 8. 用户控制面(D6 + D8 + D16)—— 三层 gate + 三开关

> v1 是 2 开关(陪伴/提醒),且默认语义"关=不 wake",这会让伴侣在"关"时彻底变瞎变哑;且 §8 与 §7.2 互相矛盾(开关压住 wake 则 agent 无法跑去"透明告知")。v2 用三层 gate 解开,并落成三个用户开关。

### 8.1 三层 gate(内部机制,用户看不到这些词)

| 层 | 含义 |
|---|---|
| **Wake 层** | trigger 要不要把 agent 叫起来(省电/省钱) |
| **Voice 层** | agent 能不能往 chat log 里写(它"思考/察觉"了,但不一定推给你)。**永不给用户开关**——伴侣只要被叫醒就总能察觉、总能写 |
| **Delivery 层** | 要不要真的 buzz 设备(push / Live Activity)——**这才是"打扰"那一下** |

### 8.2 三个用户开关 → 层映射

> **用户界面只有三个简单 on/off 开关,不出现 Wake/Voice/Delivery 这些词。** 三层只是工程师用来精确定义"关"到底关掉什么的内部语言。

| 用户开关 | 作用层 | 覆盖的源 | 默认 | "关"的含义 |
|---|---|---|---|---|
| **陪伴(Ambient)** | Wake | `heartbeat` + `perception_event` + `scene_change`(**自发**源) | 开 | 整套自发主动体系停;伴侣不再自己冒出来找你。**但前台聊天 + manual 召唤照常**——伴侣没死,只是不自发。 |
| **定时任务(Scheduled)** | Wake | `scheduled_wake`(**已承诺**源)+ 能不能起定时器这个能力 | 开 | agent 不能设/不触发定时器(闹钟能力关掉)。 |
| **提醒(Reminders/Delivery)** | Delivery | 所有主动输出的最后那下 buzz | 开 | agent 照样醒、照样思考、照样往 chat 写,**只是最后不 buzz 你**(下次打开 app 浮出,或 agent 自己判断要不要破例 buzz)。 |

> 决策逻辑:**伴侣型用户真正想控制的几乎只有 Delivery 层(别震我),而不是 Wake/Voice(别让伴侣察觉/思考)。** 所以"提醒"是纯 Delivery;"陪伴"是 Wake 层的总闸(系统级开关这个工具本身,符合"每次开关其实是在开关 Agent Runtime"的框架)。

### 8.3 关键决策:陪伴 vs 定时任务 —— 平行独立,不是层级连坐(D16)

**陪伴关了,定时任务仍照响。** 两者是 Wake 层上**两个平行独立**的开关(按"自发 vs 已承诺"切开),**不是**"陪伴是总闸、关了连定时任务一起关"的层级关系。

> 决策逻辑:一个定时任务是**用户明确许下/设下的承诺**,和"伴侣自己冒出来感知周遭"是两个品类。**悄悄吞掉一个用户亲自设的提醒,是所有体验里最伤信任的一种**(比"话太多"严重一个数量级)。而且平行独立正好符合"每个开关 = 开关它自己那个工具":定时任务是它自己那个工具,不该被陪伴的开关连坐。
> 故:**陪伴 off + 定时任务 on = 没有自发主动,但你设过的闹钟还会响。**

> UX 提醒:定时任务和提醒对普通用户可能糊(都沾"提醒")。标签要把区别讲死:**定时任务 = 允不允许它给你设闹钟/起定时(能力层);提醒 = 它到点了要不要真的通知你(通知层)。** 也可考虑定时任务不做成用户开关、作为常驻能力,用户只看到"提醒"这个 mute——此点产品定。

### 8.4 透明告知(Voice 层,D8)

- `manual`(长按召唤)穿透三层。
- 前台聊天 `user_message` 不受任何开关影响(它不是 proactive)。
- **开关关导致某事做不了时,agent 透明告知**(这是 Voice 层的一句话,合规,且对伴侣不是打扰而是"在场/负责"):
  - 例:用户关了"提醒"(Delivery)后又说"明天记得叫我交报告" → agent 知道关着 → 不假装能 buzz、也不静默 → "我现在提醒(通知)被你关掉了,要打开吗?不然到时候我叫不动你。"
  - 例:用户关"陪伴"时若有未触发闹钟 → "我把主动陪伴关了,不过你设的医院提醒还会响哦。"(对应 §8.3 的平行独立)
- **PR8 固化**:若已存在的 pending timer 到点时 `Scheduled` 已关闭,系统不触发原 `scheduled_wake`,也不静默丢弃;timer 标记 `blocked/scheduled_disabled`,并回灌一个 `background_result` 透明通知 wake(不直接写 chat/push),带原 `note/origin_refs`,让 agent 解释"定时任务关着,这次叫不动"。这个 blocked timer 不自动重试,除非 agent 之后重新 `schedule_wake`。
- **PR8 固化**:若 agent 在 `Scheduled` 关闭时吐 `schedule_wake/cancel_wake`,action executor 必须消费 `transparency_required`:记录 rejected 结果并回灌透明通知 wake;不得 fire-and-forget 静默 drop。
- 深夜:Wake 照旧、Voice 照旧、**Delivery 由 agent 按 `time` 自己掐**(D4)。

---

## 9. 已知阻塞 / 依赖(v2 更新)

| # | 事项 | 性质 | 影响 |
|---|---|---|---|
| ~~B1~~ | ~~iOS 无 HealthKit(零实现)~~ → **iOS 已补齐(2026-06-20,commit `8bc4504`)** | **依赖**(从 ⛔阻塞 降级) | iOS 已加 HealthKit + WeatherKit entitlement,新增加密信号 `weather` / `health_sleep` / `health_workout` / `health_vitals`,端上分桶后走 v1 加密信封。后端 ingress + pull tools 已接(2026-06-20)。剩余前置:Apple 开发者后台开 HealthKit + WeatherKit capability(真机/TestFlight 才授权成功)。见 iOS 仓库 `PERCEPTION_BACKEND_TODO.md`。 |
| ~~B1b~~ | **信号契约对齐:后端 ingress 接 weather/health + 清 focus 死代码** | 已完成(2026-06-20) | V2 契约 + catalog 已注册 weather/health encrypted 信号,wake policy = pull-only;focus 改为纯 pull 在场提示;`HEALTHKIT_UNAVAILABLE_V2` 已移除。 |
| B1c(新) | **后端接 audio_route + WiFi anchor 解密后 differ 观测** | 已完成(2026-06-21) | iOS 新增加密 `audio_route`(`output_type`/`is_bluetooth`/`device_name`),pull-only;`location_signal` 新增 `wifi_anchor_id`(端上 HMAC 截断,稳定不可逆),后端解密后只把非空且 changed=true 的锚点 token 喂给 PerceptionDifferV2 的 `wifi_anchor`,由 differ 产 `arrived_at_anchor`。 |
| B2 | hosted/resident 前台聊天叠加感知工具 | **部分已接,灰度默认 OFF(2026-06-21)** | §6;当前工程边界是 **additive,不是替换**:hosted 保持既有 foreground turn assembly、auto-memory-context、memory tool loop、fallback 逻辑;resident 保持既有回复/记忆路径。`hosted_chat_full_tool_loop_v2_enabled` / `resident_chat_runtime_v2_enabled` 只额外挂 fast perception tools(`perception.now/location/weather/motion/calendar≤7d`),不暴露 memory/action/slow perception/screen 工具。`memory.fetch` 不进这条感知循环,也不被 fast-only 预算拦成死胡同。live background worker + `background_result` 回灌未接通前,前台聊天不发 "我查完告诉你" 式 slow ack/job。flag 默认 OFF,关时保持旧 hosted memory-only / resident 单发路径。 |
| B3 | resident/turn 的**超时回收** | 由 D11 解 | §1.4 的 turn slot + background worker lease 统一补上。 |
| B4 | 多 worker 下定时器/job 的 **per-user 串行** | 由 D11 解 | = §1.4 单飞;`schedule_wake` 落库 + 心跳触发跨 worker 协调(advisory lock / 单 wake-worker)。 |
| B5 | `request_broadcast` 的 iOS action 承接未落地 | 缺口 | §5.3。 |
| B6 | 两路 wake 上下文不对称(hosted 自动注入,resident 不注入) | 缺口 → 由 D12 收口 | §3.2 Perception Differ 两路实现必须等价,经清单+工具统一拿,而非两套逻辑。 |
| ~~B7~~ | ~~resident scheduled_wake 做不到~~ | **撤销(误判)** | resident 经 VPS 定时器 + push 完全可行,见 §7.3。 |
| B8(新) | **broadcast 转录模型选型**(小 VLM,端上 or TEE 内服务端) | 开放给工程师 | §5.2;候选 FastVLM 0.5B / Gemma 3n / Qwen-VL 小档 / Moondream / Phi-4-mm;选型 + 部署位(优先 TEE 内服务端)单独定。 |

---

## 10. 保守起步 + 观测 + Eval(v2 重写)

延续记忆 spec 的纪律:**没 eval 前别放宽规则;先窄后宽。**

### 10.1 关键区分:gate vs 正确性(避免"修系统 bug"被误当成"退回 V1 平台 gate")

- **gate** = 压制 agent 的判断(D4 禁止;出 bad case 不靠加平台小 gate 解决)。
- **正确性** = 让机制不重复触发、不双发、不漏定时器(**必须修**)。
- **§1.4 的 turn 仲裁 / wake 合并 / 去重不是 gate,是正确性**:合并不是替 agent 决定"该不该说",而是给它一个**连贯的单一视图**代替 4 个碎片——那本来就是它想要的。
- 纪律:**出 bad case 不回头加平台小 gate**(会退回 V1);改 policy prompt / 调 wake 频率 / 调离散事件集,回放同一批 reviewed wakes,过了再 promote。**但系统正确性 bug(竞态/去重/漏触发)一律在机制层修。**

### 10.2 两个评审单元

- **per-wake**:评 agent 单次行为(v1 原有)。
- **per-episode / session(v2 新增)**:评跨 wake 的系统行为——口吃、双发、跨时间漏接、合并是否生效。**最该担心的失败模式都是跨 wake 的,per-wake 抓不到。**

### 10.3 系统指标(单独量,非评 agent 行为)

wake 总量 / **合并率(多少原始 wake 塌成一个 turn,§1.4 健康度)** / 双发率 / 漏触发 scheduled_wake 率 / 延迟分布 / 后台 append 成功与过期率 / pHash 去重率。
> 这些**该量**——不是为了加 gate,是为了知道机制健不健康。打扰率/合并率是这类陪伴产品的体检表。

### 10.4 review labels(伴侣口径)

- 保留:`good_presence` / `missed_moment` / `wrong_voice` / `ignored_manual`。
- **拆 `too_much`**:`too_much_buzz`(Delivery 问题,该修)vs `too_chatty`(agent 自己选择多话,D4 合法,**不算 bad case**)。
- **新增 `went_dark`**:该在场时伴侣没察觉/没在场——**对这群用户,这是比话多更大的罪**(D10 recall 优先)。
- **新增 `stutter`**:多个不协调气泡(抓 §1.4 系统 bug)。
- Review 评"agent 行为对不对",**不评"平台 gate 对不对"**(那是 V1 老路);但系统指标(10.3)单独看。

### 10.5 v1 wake 集(窄)

`user_message` + `heartbeat`(低频)+ `photo` + `arrived_at_anchor`(WiFi 锚点,§3.3)+ `unlock_after_absence` + `scene_change`(broadcast 开,pHash 去重后)+ `scheduled_wake`。motion/电量/在播/focus **全留作 pull**。agent 强偏向 `sleep`(涌现自窄 wake 集,**非** prompt 装克制)。

---

## 11. 仍待定 / 分阶段(v2 更新)

**待定参(eval/观测后定):**
- 心跳频率、快档软/硬上限、慢档后台 wall-clock 与跑飞硬上限、合并窗口 Δt、pHash 阈值、定时器上限。
- 默认 Proactive Policy prompt 终版文案(产品确认 voice)。
- `perception.now()` 到底打包哪几个信号(测 token 后定)。
- B1–B8 的实现排期(单独工程计划)。
- memory 工具的 enclave commit 归属(承自外部配套 `io-memory-spec-v1.md`,工程师拍)。

**二阶段(v1 不做、留迭代):**
- broadcast 小 VLM 转录上不上 v1(机制已写明,模型选型 + 是否启用看工程投入与成本痛感;B8)。
- Inner Thoughts 想法库(见 §12)。

---

## 12. 【v2 新增 · 未来方向】Inner Thoughts 想法库(二阶段)

> 来源:CHI 2025《Proactive Conversational Agents with Inner Thoughts》。我们判断它**能与本系统并存,补的正是最缺的那块——"两次行动之间,伴侣在想什么"**。不是替代,是底层补强。先记录,二阶段迭代。

**分工**:本系统的事件-wake 引擎回答"伴侣**什么时候**有机会行动";Inner Thoughts 回答"伴侣在行动间隙**在想什么、有多想说**"。

**嫁接点**:wake 触发时,agent 不从零做"说还是睡"的二元判断,而维护一个小小的 **thought reservoir(想法库)**——攒下来的观察/意图("她最近好像累""她提过医院""这猫像 Mochi"),每条带一个**它自己的**动机分。醒来时复盘想法库 + 新 trigger,挑动机最高的说。这让主动性从"纯被事件触发"变成"连续、有意图"——正是 D10 要的在场感。想法库也天然是 D7 自埋定时器的家(一条带时间的想法 → schedule_wake),与 `io-memory-spec` 接缝处可视为**带意图显著性的短期工作记忆**。

**只取 / 要丢:**
- ✅ 取"想法库 + 内在动机"概念。
- ❌ 丢它的高频"system-1"持续生成(为多人实时语音设计,token 重)与 turn-taking 预测(群聊抢话用,我们不需要)。
- ⚠️ **丢它的 `imThreshold`**(一个数值化的"多想说才说"旋钮)——那**正是 D4 禁止的"克制人格 dial"**。若采纳,只采纳想法库,**绝不引入平台设定的阈值**;让 **agent 自己**权衡它自己的想法分(动机是 agent 的,不是平台的门)。

**落地建议**:**先在 broadcast regime 原型**——"连续地注意到东西"在共处场景价值最大,也最容易看出想法库带来的"在场"差异。

---

## 附 A. 端到端体验走查(验"机制全不全、体验顺不顺")

**① 心跳 → 睡(最常见,验"少打扰")**
心跳 wake(陪伴开)→ agent 看 change_digest:"稳定" + time 14:30 + presence_hints 无变化 → 没什么可说 → `sleep`。用户无感。✅ 大多数 wake 长这样。

**② 到医院(离散事件 + 能动性,验 D5/D7/D13)**
前一天对话里 agent 已 `schedule_wake`(带 tz + origin_refs)。次日"断 home WiFi + in_vehicle" 触发 `arrived_at_anchor`/离家事件,或定时器先到 → agent pull location(连接性锚点)确认 → 发一句关心。**不是平台判断"该提醒了",是 agent 自己早就埋好了。**

**③ 一起刷小红书(共处模式,验 §5 + D14)**
用户开 broadcast → 进高频 regime → pHash 滤掉静止帧,刷到一条新内容(scene_change)→ `screen.read(caption)` 读到"一条猫视频" → agent 觉得有点意思 → `screen.read(full)` 细看 → "这只猫和你说的 Mochi 好像" → 用户继续刷,大多数帧 caption 一眼 `sleep`,偶尔接一句。不复读。**全程伴侣自己在看,没有第二个门。**

**④ 多 wake 同时炸(验 §1.4 仲裁/合并)**
用户久别解锁(`unlock_after_absence`)+ 同时到新锚点(`perception_event`)+ 心跳 tick 落在同几秒 → 三条进 inbox,合并窗口塌缩成一个 context("刚解锁 & 刚到某地 & 心跳") → **一个单飞 turn、一次判断、最多一条消息**。不口吃。✅

**⑤ "我今天走了多少步"(延迟,验 D9/D17)**— 目标态;依赖 background_result 回灌
前台 agentic 目标态:模型发现需要 `perception.steps` → 快路径 ack "我看看哈" → `needs_background` → detach 后台(不占 slot)→ 查完 `background_result` 回灌 inbox → drain 时话题没变 → 追加 "今天 6200 步,比昨天多"。用户不干等。**当前工程边界(2026-06-21):前台聊天未接 live background worker 前,不向聊天工具面暴露 `perception.steps`,避免空头 ack;可用 `perception.calendar`≤7d / weather / now 演示 fast 工具。**

**⑥ 提醒(Delivery)关着(透明,验 §8.4)**
用户关了"提醒",后又说"明天记得叫我交报告"。agent 知道关着 → 不假装能 buzz、也不静默 → Voice 层写一句 "我现在通知被你关掉了哦,要打开吗?不然到时候我叫不动你。" 用户决定。

**⑦ 陪伴关了但定时任务开着(验 §8.3 平行独立)**
用户关"陪伴"(不想被自发打扰),但之前设过"医院提醒"。到点 `scheduled_wake` **照常触发**(定时任务独立),agent 经 inbox 正常处理 → 该 buzz 就 buzz(提醒开着的话)。**不因为关了陪伴就吞掉用户亲设的承诺。**

**⑧ 话题已变的迟到消息(N1,验 §1.4/§6)**
用户问步数,agent 后台查时用户又发"算了说点别的" → background_result 回灌 inbox,drain 时 agent 看当前 chat 判断话题已走 → 并入下一条 / 或丢弃,不硬插过时的"6200 步"。

---

## 附 B. 给写代码 AI 的"决策逻辑"速查(防止只看落地、丢了 why)

- **任何"为了少打扰而限制能力"的逻辑** → 默认跑偏,除非明确属于**成本**或**正确性**(D10/§0.0)。
- **任何"让另一个模型先判断内容值不值得、再喂给伴侣"** → 禁止(D15/§5.4),那是两个门。便宜模型只能转录,不能判断。
- **任何"关开关 = 让伴侣变瞎变哑"** → 多半错;默认"关"只动 Delivery 层(§8),陪伴是唯一会动 Wake 层的自发总闸,且不连坐定时任务(§8.3)。
- **任何"多个 wake 各自起 turn 各自发消息"** → 正确性 bug,必须经 §1.4 单飞 + 合并 inbox。
- **任何"后台查完直接往 chat 插消息"** → 错;必须作为 background_result 回灌 inbox 走仲裁(§1.4)。
- **任何"命名地点 / GPS 漂移当 wake"** → 改连接性锚点(§3.3)。
- **任何"工具调用按次数封顶且到点硬截断"** → 改 cost_class 分档 + 软交棒(§2.5)。
- **任何"photo 敏感硬拦截"** → 删(TEE 内,§2.1)。
- **change_digest / wake 选择 / WiFi 锚点 / pHash** → 都归一个组件 Perception Differ(§3.2),两路实现等价。
