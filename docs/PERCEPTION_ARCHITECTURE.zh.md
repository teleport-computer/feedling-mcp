# 主动感知系统 · 架构

> 这份讲**现在的系统长什么样**：数据怎么进来、经过哪些层、每层做什么判断、
> 两条运行时各自怎么走、边界画在哪。
>
> 想看**要改成什么样**去读 `PERCEPTION_EXTRACTION_DESIGN.zh.md`；
> 想看**模型看到的原文在哪**去读 `PERCEPTION_PROMPT_ASSETS.zh.md`。
>
> 基线：`origin/test`。文中的行号会漂，模块名不会。

---

## 零、一句话

**主动感知 = 让 io 知道你现在什么情况，并且判断什么时候值得戳它一下。**

戳醒之后说不说、说什么，**不归感知管**，那是 agent 的性格。
「戳一下」和「该开口了」是两回事——这条贯穿全文。

---

## 一、系统边界：三件事，别混

```
① 感知      现在周围是什么情况            —— 每一轮对话都用得上
② 叫醒      有件事发生了,值得戳 agent 一下 —— 只在少数几种事件上发生
③ 查询      agent 主动问「我最近睡得怎么样」 —— 跟叫醒无关
```

**最容易混的是 ① 和 ②**：感知一直在收数据、每轮都喂给模型；
叫醒是稀有事件，一天可能一次都没有。关掉全部叫醒，① 和 ③ 照常工作。

---

## 二、能感知什么

21 个能力、20 个信号，声明在**一张表**里（`perception/catalog.py`），
其余机制全部由这张表驱动：接受哪些上报字段、哪个权限管它、多久算过期、
是不是叫醒源、agent 能不能主动查。

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
   ★ 经纬度当场丢弃,不入库。agent 永远看不到坐标
   ★ 同理:WiFi 名 → 「已知网络」，app bundle id → 「社交类」
   ↓
③ 写当前状态     store.py → perception_state
   place_label=公司,带 last_seen 时间戳。读的时候按 ttl 判过期
   ↓
④ 判断「算不算一件事」  signal_state_v2.py
   ★ 这一步在 Postgres 事务里，SELECT ... FOR UPDATE 锁住「这个用户 + 这个信号」
   ★ 为什么必须加锁:重复上报、乱序到达、同一时刻两个不同值，
     不锁的话会产生「假变化」→ 错误叫醒
   判据三件:时间戳序（旧的直接丢）· 指纹是否变了（HMAC）· 是不是第一次
   ↓
⑤ 发事件         differ_v2.py
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

**这条链上「判断」和「机制」是分开的**：②④⑤ 是判断（纯逻辑），
③④ 的锁和事务、⑦ 的队列是机制。这也是内核提取那批要沿着切的那条线。

---

## 五、agent 看到的是什么

分两种时机，**内容形态完全不同**，这是最容易误解的一处。

### ① 每一轮对话（含你主动说话的那种）

`perception/wake.py::snapshot_for_wake` 把当前状态拼进本轮上下文。
**不用调工具**，agent 手上直接就有。

```
形态：当前状态的粗粒度快照
      place_label / motion_state / battery_level / in_focus / app_name …
      过期字段一律 null
```

### ② 主动回合（被感知戳醒的那种）

给的是**「一瞥」**（`glance.py`）——**全是布尔值，一个数字都没有**：

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

想要真实数值？**它得自己调工具**。这一步是刻意加的摩擦。

### ③ agent 主动查（任何时候）

```
perception.now / location / weather / motion / calendar / focus / audio_route
perception.recent_apps      最近开过哪些 app（当前 app 那个字段答不了轨迹问题）
perception.sleep / workout / vitals / activity / body / metabolic / cycle / mood
perception.trend / history  最近 N 天的趋势与逐日数据
```

这条路**跟叫醒完全无关**。把四个叫醒开关全关掉，这些工具照常能用。

---

## 六、两条运行时：同一份数据，两套读法

```
V2（云上托管）        model_api_runtime/v2/worker.py 的 wake lane
V1（自托管 VPS）      tools/chat_resident_consumer.py
                      ★ 这个文件同时服务 hosted 和自托管两类用户，改它两边都受影响
```

两边拿到的**数据是同一份**，但告诉模型「该怎么读」的说明书**内容不一样**，
而且各自散落：

```
V1   consumer 的 _native_reachout_perception_context —— 一大段英文
     一瞥不是待汇报清单 · 最多挑 2-3 件 · 不许念具体数字 ·
     信号偏低时（深夜/悲伤的歌/没睡好）要更轻,别叠加担忧、别下诊断

V2   散在三处：
     worker.py `_WAKE_SYSTEM_PROMPT`
       说与不说同等正当 · glance 只是提示 · 最多一个主题 ·
       别把多个感知域变成设备/健康报告 · 用 attention_facts 避免打断和重复
     context.py `_RUNTIME_PERCEPTION_*_POLICY`
     capabilities/tool_schema.py（工具说明里还有一份读法）
```

逐条对照：

```
最多说几件      V1「2-3 件」   V2「1 个主题」   ← 不一致,V2 更严
别念具体数字    V1 有          V2 无
状态低时更轻    V1 有          V2 无
别打断/别重复   V1 无          V2 有（attention_facts）
说与不说同等    V1 无          V2 有
```

**没有任何一个地方能一眼看全「感知该怎么读」。**
这就是内核提取那批要消掉的东西——注意它消的是**散落**，
不是**内容差异**；差异要不要拉平是另一个产品判断。

---

## 七、隐私边界：三条线

```
① 原始值不落库
   经纬度 / WiFi 名 / bundle id 在 resolve 阶段就换成标签，原值当场丢弃

② 未授权 = null，不是 0 也不是空字符串
   权限判据在 permissions.py，按能力粒度判；
   注意 step_count 归 steps 管、不归 vitals 管这类细节也在表里

③ 照片走加密信封
   /photo/evaluate 的像素进加密通道，元数据（scene_hint 等）才进感知状态；
   敏感场景（document / id_card / medical / screenshot / private / receipt）
   在 V2 里不再是硬拦截，而是作为**表达策略的输入**交给 agent 判断
```

历史数据在读的时候还会**按权限二次遮蔽**（`perception_core.py` 里那层 mask）——
防的是：一条没授权的高排名变化，把「显著变化」的名额占掉，
挤掉一条真正该看到的。

---

## 八、时间维度：三种粒度

```
当下      perception_state         带 ttl，过期即 null
逐日      perception_daily         rollup。每天一行,按信号归档
趋势      history.read_trend       最近 N 天的滚动基线与偏离
```

`history.py` 里还有两个专门给主动回合用的：

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
│  算不算一件事  signal_state_v2   事务 + FOR UPDATE + HMAC   │
│  发事件        differ_v2         7 个叫醒源                 │
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
│  一瞥 + 三处说明书       │  一瞥 + 一处说明书                │
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
