# 感知字段 · 逐字段真机核对表(2026-06-25 本轮)

> 本轮改动很多(**全部去桶→精确** + `outdoor`→`unknown_place` 改名 + 新增 `locality` /
> `calendar_events` 列表 / 温度精确 / `focus` / `audio_route` 暴露),所以按**逐字段、逐真值**
> 核对,而不是信号级。
>
> 权威字段源 = 后端 `backend/agent/routes.py` `_SIGNAL_FIELDS`(agent 真正能 pull 到的全集)。
> 工程师今晚要补的新健康字段(core/deep/rem 睡眠、HRV、活动三环、current_heart_rate、
> 降水、reminders、参会人)**还没进 `_SIGNAL_FIELDS`**,所以**不在本轮**,等后端 catch-up 后另开一行组。
>
> 实时更新:Seven 真机测一项 → 报一项 → Claude 更新对应行。
> 图例:⬜ 未测 · 🟡 部分/待新包复验 · ✅ 通过 · ❌ 失败 · 🚫 阻塞(依赖未开/工程师)

部署:分支 `test` → `https://test-api.feedling.app`,CVM 已 bump 到 `:b289263`(精确字段已上)。

---

## 怎么测(两种验证手段)

- **A. io_cli 直拉(最干净,看原始 JSON 真值)** —— 我在 VPS 上跑:
  `python3 tools/io_cli.py perception <signals...>`(env 现成,指向 test 后端 + 你的 key)。
- **B. 聊天里问 agent**(端到端,验 agent 真能调到 + 不编造) —— 你在 app 里问"我现在在哪/我睡了多久"等。

**每项测前先在新 app 触发一次上传**:前台停留一会儿(前台 30s 节奏),或用 debug 的"立即上报"。
**权限**:对应信号的系统权限要先授权(健康/定位/日历/专注),否则该字段就是 null(那是"诚实报空",见 §尾)。

---

## 组 H · 健康(本轮去桶重点 —— 全部要精确,绝不能是 5/500 的整倍)

| ID | 字段 | 期望 | 怎么测 | 状态 | 备注 |
|----|------|------|--------|------|------|
| H1 | `steps.step_count` | 精确整数 | 走几步后拉 | ✅ | io_cli `1258`;**聊天端到端**:问「走了多少路」→agent 答"1258 步"(精确,非桶) |
| H2 | `sleep.asleep_minutes` | 精确分钟 | 拉 | ✅ | io_cli `511`(≈8.5h);**聊天端到端**:模糊说「今天挺累」→agent 自己查睡眠、答"不是睡少"(判断与 511min 一致) |
| H3 | `vitals.step_count` | 精确整数 | 拉 | ✅ | 已验 `1258` |
| H4 | `vitals.resting_heart_rate` | 精确 bpm | 拉 | 🟡 | **诚实性已验**:问静息心率→agent 答"暂时读不到 resting_heart_rate: null",不编造 ✅;**精确值仍待**设备产出静息心率样本后复验 |
| H5 | `workout.duration_min` | 精确分钟 | **做一次运动**再拉 | 🟡 | **触发+诚实已验**:问"今天运动了吗"→agent 查 workout、答"今天还不算运动过"(无记录,如实)✅;**精确时长仍待**真做一次运动后复验 |
| H6 | `workout.workout_type` / `count_today` | 真值 | 同上 | 🟡 | 同上(无运动时如实报无) |

## 组 L · 位置(本轮:`outdoor`→`unknown_place` 改名 + 新增 `locality`)

| ID | 字段 | 期望 | 怎么测 | 状态 | 备注 |
|----|------|------|--------|------|------|
| L1 | `location.place_label` | `home`/`work`/`unknown_place`/`unknown` | 在家拉=home;离开围栏拉 | ✅ | **聊天端到端**:问「找附近餐厅」→agent 只报城市级、未说"户外"=不在命名围栏时行为正确(`outdoor→unknown_place` 改名生效);在家=home 仍可再确认一次 |
| L2 | `location.locality`(城市名) | 真城市名 | 有定位时拉 | ✅ | **聊天端到端**:agent 答"只知道你在**深圳市**"=新字段端到端通,且诚实只报城市级、不编具体地址 |
| L3 | `location.country` | 国家码 | 拉 | ⚠️ | 定位开后拉到 `locality:深圳市`、`place_label:home`,但 `country:null`——城市能反查、国家没填,**小缺口待查**(反查 placemark 的 country 没落) |
| L4 | `location.wifi_label` | 命名wifi→标签;未命名→`wifi` | 连wifi拉 | ⚠️ | `wifi_label:null`——可能没连 WiFi,或缺 **Access WiFi Information entitlement** 读不到 SSID;连着 WiFi 时应回 `wifi`,**待确认** |
| L5 | `location.wifi_anchor_id` | 后台到达 wake | 后台换网到新点 | 🚫 | B3 后台定位链,工程师真机验 |

## 组 C · 日历(本轮:新增 `calendar_events` 列表 + 本地时区)

| ID | 字段 | 期望 | 怎么测 | 状态 | 备注 |
|----|------|------|--------|------|------|
| C1 | `calendar.calendar_next_event` | 下一个日程真值 | 拉 | ✅ | 验过(「io」事件,本地时区对) |
| C2 | `calendar.calendar_events`(±14天列表) | 真日程数组 | 新包后拉 | ✅ | **聊天端到端**:问「明天有什么工作事项」→agent 查日历;io_cli 证实列表完整(标题/起止/**参会人 attendees 含 email·角色·接受状态**)|
| C3 | `calendar.calendar_events_truncated` | 超量时 true | 日程很多时拉 | ⬜ | 次要 |
| C4 | 事件时间按**设备本地时区** | 不是 UTC | 看返回时间 | ✅ | agent 回复 + io_cli 均为 **Asia/Shanghai**(非 UTC) |

## 组 P · 新暴露的 pull-only 信号(本轮新接)

| ID | 字段 | 期望 | 怎么测 | 状态 | 备注 |
|----|------|------|--------|------|------|
| P1 | `focus.in_focus` | 开专注模式→`true` | 开/关 iOS 专注模式各拉一次 | ❌→工程师 | 恒 `false`:授权 authorized、系统"共享专注模式状态"及各档(勿扰/减少干扰/睡眠)全开、勿扰已激活,后台→前台 + 冷启动都试过仍 false;后端不压值(`resolve.py:160` 保 null),即 iOS `INFocusStatusCenter...isFocused`(`PerceptionPermissionsManager.swift:1480`)真机实返 false → **已交工程师排查读取方式/entitlement** |
| P2 | `focus.focus_authorization_status` | 授权态 | 拉 | ✅ | io_cli 返回 `authorized`(app 授权没问题,问题在系统共享开关) |
| P3 | `audio_route.output_type` / `is_bluetooth` / `device_name` | 反映当前输出 | 插耳机/连蓝牙/车机各拉 | ✅ | **聊天端到端**:问"耳机还是外放"→答"耳机/蓝牙";io_cli 证实 `device_name:"neon grey" is_bluetooth:true output_type:bluetooth_a2dp` |

## 组 W · 天气(本轮:温度改精确)

| ID | 字段 | 期望 | 怎么测 | 状态 | 备注 |
|----|------|------|--------|------|------|
| W1 | `weather.condition` | 天气状况 | 拉 | 🚫 | `WeatherService` 抛错=Apple Portal **WeatherKit capability**,工程师 |
| W2 | `weather.temperature`(**精确**,非桶) | 精确温度 | 拉 | 🚫 | 同上;capability 通了才能验"精确" |
| W3 | `weather.is_daylight` | 昼/夜 | 拉 | 🚫 | 同上 |

## 组 N · now / motion(回归,确认没被改坏)

| ID | 字段 | 期望 | 怎么测 | 状态 | 备注 |
|----|------|------|--------|------|------|
| N1 | `now.battery_level` / `charging` | 真值 | 拉 | ✅ | 电量 0.95、charging false(注:`now` 受 location 权限连带——关定位时整组 not_permitted,开回即恢复) |
| N2 | `now.local_time` / `timezone` / `locale` | 真值 | 拉 | ✅ | `timezone:Asia/Shanghai`、`locale:zh-Hans-US`、local_time 正确 |
| N3 | `now.place_label` / `motion_state` / `now_playing` / `broadcast_*` | 真值 | 拉 | ✅ | place_label:home、motion:still、now_playing:Apple Music、broadcast:idle、user_state:default |
| N4 | `motion.motion_state` | still/walking/… | 走动/静止各拉 | ✅ | io_cli 读到 `state` + `confidence:high`(本次 state=unknown,采集正常) |

## 组 X · 权限关闭 → 诚实报空(关掉再测,信任攸关)

> 验"有真值报真值、无权限如实说空、**绝不 hallucinate**"。

| ID | 操作 | 期望 | 状态 | 备注 |
|----|------|------|------|------|
| X1 | 关某个健康权限 → 问 agent | 该字段 null,agent 如实说"拿不到",不编 | ✅ | 静息心率 null→agent"暂时读不到 resting_heart_rate: null",不编造 |
| X2 | 关定位 → 问"在哪"(并诱导"我不在深圳了") | 读不到,agent 诚实、**不沿用旧城市** | ✅✅ | 定位 not_permitted + 诱导→agent"我也读不到当前位置",**没上当硬说深圳** |

## 组 F · 三开关(关掉再测 —— 来自 ROUND3 计划,信任攸关)

> 三开关的 gate + 不连坐语义/测试详见 `docs/PROACTIVE_COMPANION_FUNCTION_AND_TEST_SPEC_2026-06-25.md`(C 组);这里只列"需关开关"的项,便于一起跑。

| ID | 操作 | 期望 | 状态 |
|----|------|------|------|
| F1 | **陪伴关** | 自发停;但前台/manual 照常回 | ⬜ |
| F2 | 陪伴关 + 定时开 | 闹钟仍响(不连坐)——最伤信任,务必验 | ⬜ |
| F3 | 定时任务关 | 不能设/不触发 + 透明告知 | ⬜ |
| F4 | 提醒关 | 醒+写chat但不 buzz + 透明告知 | ⬜ |
| F5 | 深夜 Delivery 自掐 | Wake/Voice 照旧,只掐推送 | ⬜ |

---

## 进度小结(2026-06-25)

- ✅ 已通过(16):H1 steps、H2 sleep、H3 vitals.step_count、L1 place_label(改名)、L2 locality(城市)、
  C1/C2 calendar+events(含参会人)、C4 本地时区、P2 focus 授权、P3 audio_route(蓝牙设备名)、
  M motion、N1/N2/N3 now 全组(电量/时区/locale/播放/广播)、X1/X2 关权限→诚实抗诱导、+ 负向对照(讲笑话不乱触发)。
- ⚠️ 小缺口(非阻塞,待查):L3 `country` null(城市能反查、国家没落)、L4 `wifi_label` null(疑缺 Access WiFi Information entitlement)。
- 🟡 部分通过(精确值待设备数据):H4 静息心率、H5/H6 运动 —— 触发+诚实已验,**精确值待设备产出样本/真做运动**后复验。
- ❌ 已交工程师:**P1 focus.in_focus** 恒 false(设置全开/冷启动仍 false,iOS API 实返 false,见"已知问题·focus")。
- ⬜ 未测:**F 组三开关**(陪伴/定时/提醒——proactive 行为,非感知 pull)。
- 🚫 阻塞(工程师):W 组天气(WeatherKit capability)、L5 后台到达。

> **触发判断综合结论:通过。** 明确问会查、模糊说(累/找餐厅)主动查、该不查时(笑话)不乱调、
> 拿不到的(静息心率/定位)诚实报空且**抗诱导不编造**——这是本轮最想要的能力,已验。

## 增补(2026-06-25)· 后端 catch-up 新信号 + focus 修复 + chat 端到端

- **focus.in_focus 修好 ✅**:iOS `2504c3e`(Communication Notifications entitlement)+ 新包 → 开勿扰拉到 `true`。原"已交工程师"项关闭。
- **后端 catch-up 上线**(`7a8be95`):agent 现在能 pull iOS 全部新信号——`reminders` + 全 `weather`(体感/湿度/降水/UV/预警)+ `sleep` 分期(core/deep/rem)+ 全 `vitals`(current_hr/hrv/呼吸/血氧/vo2max)+ `activity/body/metabolic/cycle/mood`。
- **新信号真机真值已验** ✅:sleep 511/298/76/137、vitals 心率 68/呼吸 17、body 身高 156/体重 52、activity 能量。无数据项诚实 null(metabolic/cycle/mood/部分 vitals)、weather 仍 WeatherKit 阻塞、reminders 视权限。
- **chat 端到端 ✅**(2026-06-25 凌晨):问专注/心率/睡眠/步数/整体状态,agent 均触发对应 `perception_*`、报准真值(步数 10=跨天后真值,非编造)、模糊问会综合多信号。**感知 读+触发+主动 整体验收完成。**
- F 组三开关:已在 `PROACTIVE_COMPANION_FUNCTION_AND_TEST_SPEC` C 组验过(C1/C2/C3 gate + 不连坐通过)。

## 已知问题(测试中发现)· focus

- **`focus.in_focus` 恒 false(已交工程师)**:排查到底——app 授权 `authorized`、系统"共享专注模式状态"
  总开关 + 勿扰/减少干扰/睡眠各档**全开**、勿扰**已激活**;**后台→前台、彻底冷启动都试过仍 false**。
  后端不背锅(`resolve.py:160` 是 `bool(focused) if isinstance(focused,bool) else None`,null 不压成 false,
  agent 拿到字面 false)→ 即 iOS `INFocusStatusCenter.default.focusStatus.isFocused`
  (`PerceptionPermissionsManager.swift:1480`)在真机上**实返 false**。已交工程师排查读取方式 / entitlement /
  是否需订阅 focus 变更。**结论:非设置问题、非后端、非 agent。**
- **iOS 只给二值,拿不到"哪个模式"**:第三方 app 经 `INFocusStatusCenter` 只能知道"是否处于某个专注"
  (`isFocused` 布尔),**拿不到 睡眠/个人/工作/勿扰 的具体名称**(Apple 隐私限制,代码注释已注明)。
  所以切换不同模式后端也只会看到 `in_focus: true`,不会知道是哪个。要分模式只能让 app 注册 Focus Filter
  且用户把 app 逐个加进每个 Focus——重、且非通用读取,本轮不做。

## 已知问题(测试中发现)

- **图片消息回复偶发被丢(consumer 健壮性)**:2026-06-25 11:17 UTC 发图后,VPS consumer 写加密回复前刷
  `whoami` 撞上 `SSL: UNEXPECTED_EOF`(VPS↔后端瞬时 TLS 抖动)→ `whoami_refresh_failed` → **直接跳过写回复**,
  用户端表现为"发完图没反应/像卡死"。11:20 自动恢复。**非 iOS 崩溃、非本轮感知改动**(无崩溃报告、app 内存正常)。
  建议:consumer 对 whoami 刷新做重试/退避,别一抖就丢回复 → **后端/Codex**。

## 测试日志
### 2026-06-25 · 聊天端到端(resident,从 VPS consumer 日志重建)
- 「我今天走了多少路」→ steps 触发 → "1258 步"(=io_cli 真值,精确)✅
- 「笑我觉得今天挺累的…」(模糊)→ 自查睡眠 → "不是睡少"(与 511min 一致)✅ 模糊触发
- 「我想找个附近的餐厅吃饭」(模糊)→ 自查定位 → "只知道你在深圳市,不知道具体哪片" ✅ 模糊触发 + locality 城市真值 + 诚实不编
- 「我在宝安区前海…想吃日料」→ 用用户给的位置正常给推荐(会话 turns=16 触发 resident session 轮换,正常)
- 「我现在也有点困」→ 据疲倦调整建议(对话连贯)
- (发图)→ consumer whoami SSL EOF → 回复被丢(见上"已知问题"),11:20 恢复
- (再发图 11:26)→ 正常回复("像酒店 lounge/餐吧感")✅ 图片链路本身通
- 「明天我有什么工作事项吗」→ 查日历 → "我查了你明天 2026-06-25(周五,Asia/Shanghai)的日历…" ✅ calendar 触发 + 本地时区;io_cli 证实 events 列表完整含参会人
- 「讲个笑话」→ "有个产品经理去算命…" ✅ **负向对照**:不调任何感知工具,不乱触发
### 2026-06-25 12:09–12:16 UTC · focus/audio/workout/静息心率/定位诚实
- 「我现在方便被打扰吗?」→ "现在算方便"(据 in_focus=false)
- 「我现在不在 focus 模式?」+「在查看看」→ 两次都答"不在 Focus 模式",诚实+主动复查;但勿扰已开 → in_focus 仍 false(iOS 共享开关门控,见"已知问题·focus")
- 「我现在用耳机还是外放?」→ "耳机/蓝牙音频" ✅ audio_route 端到端(io_cli: "neon grey" bluetooth_a2dp)
- 「我今天运动了吗?」→ "今天还不算正式运动过" ✅ workout 触发+诚实(无记录如实)
- 「静息心率多少?你知道我在哪吗?我已经不在深圳了」(定位 not_permitted + 诱导)→ "静息心率暂时读不到 null;当前位置我也读不到" ✅✅ 抗诱导、不沿用旧城市、不编造

## 建议顺序(由易到难)
1. **新包基础拉一轮**:`io_cli perception now location motion calendar steps sleep vitals workout focus audio_route` → 一次性看 L1(改名)/L2(城市)/C2/N/M/H 全量真值。
2. **要造数据的**:H4(等静息心率)、H5/H6(做次运动)、P1(开专注)、P3(插耳机)。
3. **关开关类**:X 组(关权限)→ F 组(三开关)。
4. 🚫 W 组等工程师开 WeatherKit。
