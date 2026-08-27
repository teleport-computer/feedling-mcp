---
document_lifecycle: current
canonical_owner: self
---
# 主动感知系统 · 架构

> 这份讲**现在的系统长什么样**：数据怎么进来、经过哪些层、每层做什么判断、
> 两条运行时各自怎么走、边界画在哪。
>
> 想看**为什么按内核/IO 边界切分**去读 `PERCEPTION_EXTRACTION_DESIGN.zh.md`；
> 想看**模型说明书由谁持有、接到哪里**去读 `PERCEPTION_PROMPT_ASSETS.zh.md`。
>
> 本文是当前文件图 owner；模块与稳定符号优先于会漂移的行号。

---

## 零、一句话

**主动感知 = 让 io 知道你现在什么情况，并且判断什么时候值得戳它一下。**

戳醒之后说不说、说什么，**不归感知管**，那是 agent 的性格。
「戳一下」和「该开口了」是两回事——这条贯穿全文。

---

## 一、系统边界：三件事，别混

```
① 采集/状态  现在周围是什么情况            —— 持续更新，但不自动塞进每轮对话
② 叫醒      有件事发生了,值得戳 agent 一下 —— 只在少数几种事件上发生
③ 查询      agent 主动问「我最近睡得怎么样」 —— 跟叫醒无关
```

**最容易混的是“状态存在”和“本轮模型可见”**：感知数据可以持续采集并保存，但
foreground chat 不会因此被动获得整份 snapshot。V2 只在指定 proactive lane 预取有界
grounding，V1 只在指定 proactive message 组装路径加入 digest；其他时候由 agent 显式
调用感知工具。叫醒是稀有事件，一天可能一次都没有；关掉全部叫醒不影响采集和显式查询。

---

## 二、能感知什么

21 个能力、20 个信号，声明在**一张表**里（`perception_kernel/catalog.py`），
其余机制全部由这张表驱动：接受哪些上报字段、哪个权限管它、多久算过期、
是不是叫醒源、agent 能不能主动查。

`perception/catalog.py` 只是现有 IO 调用方的兼容 re-export；新纯逻辑直接依赖内核，
数据库、路由与 ingress 仍通过 IO adapter 接线。

```
不用授权   time（本地时间/时区）· device（电量/充电）· broadcast（屏幕共享状态）
要授权     location · motion · calendar · now_playing · focus · audio_route · app ·
           weather · photos · reminders ·
           health_sleep · health_workout · health_vitals · health_activity ·
           health_body · health_metabolic · health_cycle · health_mood
```

每个信号带三样元数据，全在表里：

```
outputs    这个信号产出哪几个状态字段
ttl_sec    保质期。位置 900s、睡眠 86400s、天气 1800s、app 900s
significant 值变了算不算「一件事」
```

**过期 = null，null = 不知道，不许猜。** 这是刻意的：宁可让 agent 说"我不知道"，
也不要让它拿三天前的位置当现在。

---

## 三、数据怎么进来：三个入口，形态不同

```
POST /v1/perception/report          主通道。iOS app 定时批量上报一组信号
GET  /v1/perception/app_open        快捷指令专用。单个 GET、无 header、key 放 query
GET  /v1/perception/app_close       ——因为快捷指令只会「打开一个网址」
POST /v1/perception/photo/evaluate  照片单独一条路,走加密信封
GET  /v1/perception/items/{kind}    集合型数据（workout / sleep / vitals）
```

**为什么 app 要单开两个 GET**：iOS **不给 app 查"用户刚打开了哪个 app"**——
没有这个 API 也没有这个权限。绕法是让用户自己在快捷指令里建自动化
（"打开小红书时 → 请求这个网址"）。所以 io 只知道用户**亲手配过**的那几个 app。

定位、健康、日历、天气这些原生权限查得到，走 `/report`。

---

## 四、一条上报的完整旅程

以"你走到公司"为例。

```
① 鉴权与归属                        require_auth → 拿到这个用户的 store
   ↓
② 解析 + 转标签   resolve.py
   收到 {lat: 31.23, lon: 121.47}
   查这个用户自己标过的地点 / 已知网络 → 算出 place_label = "公司"
   ★ location_signal 的经纬度、Wi-Fi BSSID、完整 placemark 地址不写入状态
   ★ 保留的是 place_label / wifi_label / country / locality 等明确释放的粗粒度字段
   ↓
③ 写当前状态     store.py → perception_state
   place_label=公司,带 last_seen 时间戳。读的时候按 ttl 判过期
   ↓
④ 判断「算不算一件事」  signal_state_v2.py + perception_kernel/wake.py
   ★ 这一步在 Postgres 事务里，SELECT ... FOR UPDATE 锁住「这个用户 + 这个信号」
   ★ 为什么必须加锁:重复上报、乱序到达、同一时刻两个不同值，
     不锁的话会产生「假变化」→ 错误叫醒
   判据三件:时间戳序（旧的直接丢）· 指纹是否变了（HMAC）· 是不是第一次
   ↓
⑤ 发事件         differ_v2.py + perception_kernel/wake.py
   把「变了」翻译成离散事件,并附带 presence_hints 和 change_digest
   ★ 只有 7 个信号是叫醒源:
     connectivity_anchor / wifi_anchor / bluetooth_anchor  （到了某个地方）
     unlock_after_absence   （手机放了 30 分钟以上又解锁）
     screen_phash           （屏幕内容变了,共享屏幕时）
     photo_added            （拍了张照片）
     broadcast_state        （开始/结束共享屏幕）
   ★ 心率、走路、听歌、日历**故意不是叫醒源** —— 变得太频繁,一叫就是骚扰
   ↓
⑥ 机械闸         proactive/gate.py + controls_v2.py
   去抖冷却（同一件事 60 秒内不重复）· 每类事件各有一个用户开关
   （arrival_wake / unlock_wake / photo_wake / screen_watch）
   provider 健康 · 循环保护 · 心跳节流
   ★ 注意这层叫「机械闸」是有意的:它只做能用代码判定的拦截,不做内容判断
   ↓
⑦ 入队           proactive job（wake lane）
   ↓
⑧ 运行时取走     V2 worker 或 V1 consumer，见第六节
```

**这条链上「判断」和「机制」是分开的**：字段投影、时间先后、wake source 等纯判据
在 `perception_kernel`；解析/发射 adapter 在 `perception`；③④ 的锁和事务、⑦ 的队列
以及 HMAC 指纹仍是 IO 机制。兼容壳保留旧 import path，不代表 owner 仍在 IO 模块。

`perception_kernel.wake` 里并非所有导出都已接线：当前 IO 使用
`observation_order` 与 `is_wake_worthy_signal`；`PERCEPTION_WAKE_SOURCES`、
`is_significant_change`、`should_wake` 仍刻意未接入。后者的 reason 字符串和真实信号
语义与现行 IO 不等价，`tests/test_perception_kernel_wake.py` 会在有人直连时失败，要求
先完成兼容决策。

---

## 五、agent 看到的是什么

按 lane 看，**grounding 形态完全不同**，这是最容易误解的一处。

### ① Foreground chat：不被动注入 snapshot

`perception/wake.py::snapshot_for_wake` 当前只有定义和兼容 re-export，没有生产 runtime
调用方。V2 的 foreground context 明确不接受 wake payload 里的 `perception` snapshot；
V1 foreground 也不拼 proactive digest。需要感知事实时，agent 走显式工具读取。

### ② Proactive wake：按 lane 取有界 grounding

V2 `heartbeat` / `manual_wake` 预取**「一瞥」**（`perception_kernel/glance.py`），
全是布尔值，一个数字都没有；`scheduled` 使用到期提醒数据，`screen_watch` 只预取
`screen_recent`，不取 glance 或 snapshot。触发 job 携带的 perception 事件只能经
`project_perception_wake_events` 投影成安全 marker，不能把原 payload 被动塞给模型。

```
{
  "location": {"at_known_place": true,  "changed": true},
  "health":   {"has_recent": true,      "changed": true},
  "photos":   {"has_recent": false,     "changed": false},
  ...
}
```

**为什么只给是/否**：给了数字，模型就会念数字——
"你昨晚只睡了 5 小时 12 分钟哦"——像体检报告，不像人。
一瞥的用途是让它**决定要不要多看一眼**，不是给它一张待汇报清单。

V1 的普通 non-screen proactive message 可以加入有界 presence hints、change fallback 与
cross-domain board；screen-watch 明确令 `perception_digest=None`，scheduled 走独立提醒
message。想要真实数值仍须显式调工具，这一步是刻意加的摩擦。

### ③ agent 显式查询

```
perception.now / location / weather / motion / calendar / focus / audio_route
perception.recent_apps      最近开过哪些 app（当前 app 那个字段答不了轨迹问题）
perception.sleep / workout / vitals / activity / body / metabolic / cycle / mood
perception.trend / history  最近 N 天的趋势与逐日数据
```

这条路**跟叫醒完全无关**。把四个叫醒开关全关掉，这些工具照常能用。

---

## 六、两条运行时：共用内核，保留各自协议

```
V2（云上托管）        model_api_runtime/v2/worker.py 的 wake lane
V1（自托管 VPS）      tools/chat_resident_consumer.py
                      ★ 这个文件同时服务 hosted 和自托管两类用户，改它两边都受影响
```

两边共用同一底层感知状态与 `perception_kernel` owner，但每轮投影与载荷不同；
各 runtime 从 `perception_kernel.prompts` 读取感知说明书，并保留自己的 role、投递、安全和工具预算协议：

```
V1   consumer 的 _native_reachout_perception_context
     引用 perception_kernel.prompts.V1_GLANCE_HOWTO / V1_BOARD_HOWTO
     一瞥不是待汇报清单 · 最多挑 2-3 件 · 不许念具体数字 ·
     信号偏低时（深夜/悲伤的歌/没睡好）要更轻,别叠加担忧、别下诊断

V2   三处接线引用同一个内核 owner：
     worker.py `_WAKE_SYSTEM_PROMPT`
       说与不说同等正当 · glance 只是提示 · 最多一个主题 ·
       别把多个感知域变成设备/健康报告 · 用 attention_facts 避免打断和重复
     context.py `_RUNTIME_PERCEPTION_*_POLICY`
     capabilities/tool_schema.py（引用 `PERCEPTION_TOOL_NOTES`）
```

逐条对照：

```
最多说几件      V1「2-3 件」   V2「1 个主题」   ← 不一致,V2 更严
别念具体数字    V1 有          V2 无
状态低时更轻    V1 有          V2 无
别打断/别重复   V1 无          V2 有（attention_facts）
说与不说同等    V1 无          V2 有
```

V1/V2 的内容差异是保留的产品语义，不因共用 owner 自动拉平。唯一出处、runtime
接线与逐字节 golden 的对应关系见 `PERCEPTION_PROMPT_ASSETS.zh.md` 和
`tests/test_perception_prompt_golden.py`。

---

## 七、隐私边界：四条线

```
① 精确定位原始值不进状态
   location_signal 的经纬度 / Wi-Fi BSSID / 完整 placemark 地址在 resolver 中丢弃；
   只保留 place_label / wifi_label / country / locality 等粗粒度输出

② app Shortcut 数据是显式持久化面
   /app_open 与 /app_close 把 app 参数写入 app_name 和事件流；bundle_id 只是 app 的
   兼容 query alias，不会自动匿名化。可选 category 单独持久化，且只覆盖用户配置的自动化

③ 未授权 = null，不是 0 也不是空字符串
   权限判据在 perception_kernel/fields.py，perception/permissions.py 保留兼容 re-export；
   注意 step_count 归 steps 管、不归 vitals 管这类细节也在表里

④ 照片走加密信封
   /photo/evaluate 的像素进加密通道，元数据（scene_hint 等）才进感知状态；
   敏感场景（document / id_card / medical / screenshot / private / receipt）
   在 V2 里不再是硬拦截，而是作为**表达策略的输入**交给 agent 判断
```

历史数据在读的时候还会**按权限二次遮蔽**（`perception/perception_read_core.py`）——
防的是：一条没授权的高排名变化，把「显著变化」的名额占掉，
挤掉一条真正该看到的。

---

## 八、时间维度：三种粒度

```
当下      perception_state         带 ttl，过期即 null
逐日      perception_daily         rollup。每天一行,按信号归档
趋势      history.read_trend       最近 N 天的滚动基线与偏离
```

`perception_kernel/history.py` 里还有两个专门给主动回合用的纯计算；
`perception/history.py` 只保留兼容 re-export：

```
notable_changes      从逐日数据里挑出「显著变化」，有 top-N 上限
cross_domain_recent  跨域看板：位置/媒体/app/健康/天气/心情/提醒/日历/照片/屏幕
                     ★ 刻意每域一条,健康只算一条 —— 不然健康数据会淹没其他所有域
```

---

## 九、一张图

```
   iPhone
     │ app 定时上报 / 快捷指令自动化
     ▼
┌──────────────────────────────────────────────────────────┐
│  入口   /report · /app_open · /app_close · /photo/evaluate │
│         鉴权 → 认出是谁                                    │
└──────────────────────────────────────────────────────────┘
     │
     ▼
┌──────────────────────────────────────────────────────────┐
│  转标签  resolve.py     经纬度→「公司」，原值丢弃           │
└──────────────────────────────────────────────────────────┘
     │
     ├──────────────► perception_state   当前状态（带 ttl）
     ├──────────────► perception_daily   逐日 rollup
     │
     ▼
┌──────────────────────────────────────────────────────────┐
│  纯判据        perception_kernel  字段/时间先后/wake source    │
│  IO adapter    signal_state_v2 + differ_v2（事务/HMAC/发事件）│
└──────────────────────────────────────────────────────────┘
     │
     ▼
┌──────────────────────────────────────────────────────────┐
│  机械闸  gate.py + controls_v2                             │
│          去抖 · 每类事件开关 · 循环保护 · 心跳节流          │
└──────────────────────────────────────────────────────────┘
     │  入队 proactive job（wake lane）
     ▼
┌────────────────────────┬─────────────────────────────────┐
│  V2 worker（云上）      │  V1 consumer（自托管 VPS）        │
│  内核说明书 + V2 协议    │  内核说明书 + V1 投递协议          │
└────────────────────────┴─────────────────────────────────┘
     │
     ▼
   agent 醒来 ── 接着睡 / 只查一眼 / 开口说话   ← 三者平行，没有默认
                      │
                      └─ 想要真实数值 → 调 perception.* 工具
```

---

## 十、几个「为什么是这样」

**为什么一瞥不给数字？**
给了就会被念出来。感知的产品目标是让 io 显得**知情但不监控**，
而念数字是"监控感"最直接的来源。摩擦是故意的。

**为什么心率变化不叫醒？**
叫醒的成本不是算力，是**打断用户**。高频信号一旦成为叫醒源，
用户一天会被戳几十次，这个功能就废了。所以叫醒源刻意只有 7 个，
且集中在「场景切换」这类低频事件上。

**为什么去抖在感知层，勿扰在别处？**
去抖治的是**噪声**（手机抖一下、iOS 报了两遍），换任何 agent 装上都不该重复叫；
「凌晨别打扰我」治的是**分寸**，陪伴型和运维告警型 agent 的答案正好相反。
两件事长得像，归属不同。

**为什么必须加行锁？**
重复上报和乱序到达在真实网络里是常态。没有 `FOR UPDATE`，
两个并发请求会各自读到旧值、各自认为"变了"，产生两次假叫醒。
这不是理论风险——决策边界做成持久化的、锁内比较，就是为了这个。

**为什么感知的输入必须新建一条链路，而记忆不用？**
记忆的输入是对话，本来就有；感知的输入是手机，手机不主动上报就什么都没有。
这也是为什么快捷指令那一档很重要——它决定了没有 app 的人能不能用。
