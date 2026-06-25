# 感知历史 · 量化层设计 spec

> 状态:2026-06-25 起草(Claude)。**本轮只设计 + 落地"量化层"**;质化层(agent 日终
> 叙事 → 记忆花园)只留接口、暂不建。
>
> 起因:现在所有感知都是**实时快照**(`perception_state` 覆盖式),没有时间维度——agent
> 看得到"现在心率 X",看不到"比你平时高 8 / 这两周深睡持续变少"。量化层补这一层。
>
> 落地依赖:**等 iOS 工程师把新字段(心情 / HRV / 活动三环 / 日历参会人 / reminders /
> 天气降水 等)加完**,我们按本 spec **一次性建全**(本 spec 字段无关,新字段只是填规则)。

---

## 0. TL;DR

- **存什么**:每个有趋势/模式价值的信号,**每天一份结构化 summary stats**(不是单值、不是
  原始流)。`perception_daily(user_id, date, signal, doc)` 一张表,~十几行/天。
- **怎么算**:在 ingest 路上做**增量当日聚合**(每次 report upsert 今天那一行,running
  min/max/sum/count/分时段/集合)——**永不存原始上报**,过了今天那行自然定格。
- **谁来记**:量化层是**代码算出的事实**(确定、可算基线),**不是 agent 写**。agent 写
  叙事是质化层(后面)。
- **回报**:`perception_trend` 工具 → 近 N 天序列 + 滚动基线 + delta,让 agent 终于能
  感知**变化**。
- **默认策略:多记不少记**——只要有模式价值就 historize(连 focus 时长、戴耳机时长、听歌
  时长这类也记);只跳过纯瞬时无模式的(电量%、broadcast 开关态)。

---

## 1. 原则(落地时不可违背)

1. **多记不少记**:倾向保留。新字段默认进历史,除非明确归类为"纯瞬时态"。
2. **字段无关**:不把"当前信号清单"写死。每信号一条**注册项**(形状 + 聚合规则 + 保留期);
   工程师加的新字段加一条注册项即可,**结构不动**。(教训:`temperature_bucket→temperature`
   改名时,若历史层写死字段名就会挂——所以通用化遍历,不硬编。)
3. **不二次粗化 · 按 agent 好读、能多记就多记**:粗化**端上已经做了**——端上送来的就是
   `place_label` / 桶值 / `condition` 这类粗值,**历史层不再二次粗化、不稀释成单值**。
   直接把端上送来的粗值,**按 agent 最好读的形状、尽量多地**留下来(区间 / 分布 / 分类时长 /
   事件全集),够细到能看波动。聚合的唯一目的是"组织成每天一份、避免逐条 report 重复",
   **不是为了压缩信息**。存什么、什么形状,以**agent 读时最好用**为准。
4. **事实(代码算),不是 prose**:基线/趋势是**数学**——"vs 30 天中位数"只能从保留的数值算,
   从 agent 的 prose 里算不出来。所以量化层记数字;agent 叙事是叠在上面的质化层,**不能替代**数字。
5. **用户的"一天"**:按**设备本地时区**切日(用 time 信号的 timezone),不是 UTC 日。

---

## 2. 三层架构(本轮只建 Tier 2)

| 层 | 内容 | 保留 | 成本 | 本轮 |
|---|---|---|---|---|
| **Tier 1 · 细数据(滚动)** | 近似原始的细粒度观测,供日终算 stats / 喂 agent | 1–3 天,过期即删 | 中 | ⏸ 仅"质化层日终喂 agent"需要时才建;**量化层可不依赖它**(见 §4 增量算法) |
| **Tier 2 · 每日 summary stats** | 每信号每天一份结构化事实 | 长期(如 1 年) | 低 | ✅ **本轮建** |
| **Tier 3 · agent 日终叙事 → 记忆花园** | 事实+依据+(标注的)推断感受 | 长期 | LLM 1/天 | ⏸ 质化层,后面;**碰工程师记忆域**,只留接口 |

> 关键简化:Tier 2 的 summary stats **可增量算**(running min/max/sum/count/分时段/集合),
> **不需要保留原始**。所以量化层现在**只建 Tier 2**,不必先建 Tier 1。Tier 1 是未来质化层
> (你说的"日终把全天喂 agent 挑重点")的原料,到时再补、补完即删原始。

---

## 3. 信号形状分类 → 聚合方式(字段无关的关键)

每个信号归入一种形状,形状决定怎么增量聚合。新字段对号入座即可:

| 形状 | 含义 | 增量聚合 | 每日 doc 例 |
|---|---|---|---|
| **数值分布** numeric-dist | 连续读数(心率/HRV/气温) | running {min,max,sum,count}→avg;可选分时段 | `{min,max,avg,n, byHour?}` |
| **累加计数** cumulative | 单调累加(步数/活动能量/锻炼分钟) | running max(或末值) | `{total}` |
| **当日代表** main-of-day | 一天取一段代表(昨晚睡眠) | 取最近合格记录 | `{asleep,stages,bedtime,waketime}` |
| **分类时长** duration-by-state | 处于各状态多久(运动/focus/音频路由) | 各类累加分钟 | `{still:m, walking:m, ...}` / `{focused:m}` / `{headphones:m,car:m}` |
| **事件列表** event-list | 当天发生的离散事项(日历/workout/reminders) | 追加去重(按 id) | `[{title,start,end,...}]` |
| **主观量表** subjective | 用户自评(心情 State of Mind) | 追加每条(一天几条) | `[{valence,labels,at}]` |
| **停留分布** place-dwell | 各地点停留时长(位置) | 各 place 累加分钟 | `{home:m,work:m,...,primary}` |

---

## 4. 每信号每日 rollup(字段已定稿 2026-06-25;**实现按注册表派生,不硬编字段**)

> "记什么"按 §3 形状;**多记**为默认。下表是设计参照;**实现**用一张
> `HISTORY_SHAPES`(signal→形状[+参与字段])注册表 + 每形状一个通用增量函数,
> 信号/字段从 `catalog.SIGNALS` 派生。新增字段=注册表加一行,**结构不动**(原则 #2)。

| 信号 | 形状 | 每日记 |
|---|---|---|
| `vitals.resting_heart_rate` | 数值分布 | min/max/avg/n(基线主轴) |
| `vitals.current_heart_rate` | 数值分布 | min/max/avg/n |
| `vitals.hrv_sdnn_ms` | 数值分布 | min/max/avg/n |
| `vitals.respiratory_rate` | 数值分布 | min/max/avg/n |
| `vitals.oxygen_saturation_pct` | 数值分布 | min/max/avg/n |
| `vitals.vo2_max` | 当日代表 | 最近点值(慢变) |
| `steps.step_count` | 累加计数 | 当日总步数 |
| `sleep` | 当日代表 | 入睡时长 + 分期 core/deep/rem |
| `workout` | 事件列表 | 当天每次运动(type/时长/计数) |
| `activity` | 累加计数 | active_energy_kcal / exercise / stand / mindful 当日总量 |
| `body` | 当日代表 | weight_kg / bmi / body_fat_pct / height_cm 最近点值 |
| `metabolic` | 数值分布 | blood_glucose / bp_systolic / bp_diastolic(可多次/天) |
| `cycle` | 当日代表 | flow_level + is_active_period(最近记录) |
| `mood` | 主观量表 | 每条 valence/valence_classification/kind/label_count |
| `weather` | 数值分布 + 分类 | 当日温度 min/max/avg + condition 集合 + 降水/湿度/UV |
| `motion` | 分类时长 | 各状态分钟数(活跃/久坐由此派生) |
| `location` | 停留分布 | 各 place 停留时长 + primary + 访问集合 |
| `calendar` | 事件列表 | 当天实际发生的日程(定格,非 live next_event churn) |
| `focus` | 分类时长 | 当天处于专注模式分钟数 |
| `audio_route` | 分类时长 | 当天各输出(耳机/车机)时长 |
| `reminders` | 事件列表 | 当天到期/完成的待办(按 id 去重) |
| `now_playing` | **每日 digest**(tally) | 当天听歌总时长 + top artists/tracks(按分钟,写时 top-30 封顶)+ distinct 曲目数 |

> `now_playing` 记口味随时间的演变(top artist/曲),不是每首播放的流水;时长用相邻样本时间差累加给上一首正在播的曲/artist。

**不历史化**(纯瞬时、无当日模式意义):电量 %、charging、broadcast 开关态、time、locale/timezone。

---

## 5. 存储

```sql
-- Tier 2:每用户每天每信号一行
CREATE TABLE perception_daily (
  user_id   TEXT NOT NULL,
  date      DATE NOT NULL,          -- 设备本地时区的日期
  signal    TEXT NOT NULL,          -- 'health_vitals' / 'motion_state' / ...
  doc       JSONB NOT NULL,         -- §4 的 summary stats
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (user_id, date, signal)
);
CREATE INDEX perception_daily_user_signal_date_idx
  ON perception_daily (user_id, signal, date DESC);
```

- 体量:~15 信号 × 1 行/天 ≈ 一年 ~5k 行/用户,极小。
- (Tier 1 若将来要,复用 `perception_items` 的 ts+expires_at 模式,短 TTL。)

---

## 6. Ingest:增量当日聚合(永不存原始)

- 钩子:后端 `service` ingest 路上、更新完 `perception_state` 之后,对每个 historized 信号
  `upsert perception_daily[today][signal]`,按 §3 形状的增量规则合并今天那一行。
- **不存每条 report**——30s/5min 的 churn 只是反复刷新今天那一行(running 累加/min-max/集合并)。
- **日切**:无需特殊 job——`date`(本地时区)一变,新的一行从头开始,旧行自然定格。
- 增量字段示例:数值分布存 `{min,max,sum,count}`(读时 avg=sum/count);分类时长按相邻样本
  时间差累加;事件列表按 id 去重并入。

---

## 7. 消费:trend / history 工具(量化层的回报)

新增 agent 可 pull 的工具(经 `/v1/agent/perception` 同源或单独端点 + io_cli):

- `perception_trend(signal[, field], window_days=30)` →
  `{ daily: [{date, value}], baseline: {median, p25, p75}, current, delta, direction }`
  让 agent 说:"你静息心率近 7 天均值 66,比 30 天基线 58 高 ~14%"。
- `perception_history(signal, days=14)` → 近 N 天的每日 doc(给 agent 看原貌)。
- 基线:滚动窗口(中位数/分位数,抗离群);读时算或日级缓存。

---

## 8. at-rest(已定:明文,同现状)

- **决定:每日历史按现状口径存——粗化明文 jsonb 存 AWS RDS,与现在的 `perception_state`
  完全一样。** 落地时**不对明文做额外处理、不二次加密**;存什么、什么形状,只按"agent 读时
  最好用"来。
- **(开放 · 留给后端)**:后续后端可视需要把这份历史改成**加密 at-rest(后端持密钥)**版——
  届时 **agent 深挖完全不受影响**(后端读时透明解密),只是 DB 里躺密文、防拖库。本轮不做,
  也正因为后面可能整体搬过去,现在**不必为明文加任何特殊处理**。
- 存的就是**端上送来的粗值**(精确原始值早在端上丢了、从没到后端),**不二次粗化**。
- 永不存:原始样本、逐条精确时间戳、精确坐标 / SSID(端上即弃,历史层也拿不到)。

---

## 9. 分工

- **设计 / 规则**:Claude(本 spec)。
- **量化层落地**(perception_daily 表 + ingest 增量聚合 + trend/history 工具 + 测试)= **后端**,
  派 Codex 建、Claude 审。io_cli 加 trend/history verb = Claude。
- **质化层(Tier 3 → 记忆花园)** = **碰工程师记忆域**,设计期对齐,本轮不建。

---

## 10. 落地前要锁的决策(等工程师字段落地一并定)

1. 工程师最终加了哪些字段 → 填进 §4 表 + 定各自形状/规则。
2. 长期分辨率到哪:**只到每日 summary stats**(默认),还是某些(心率/运动)要留**分时段**长期历史?
3. **已定**:基线窗口 = **30 天**(后续可做成可配)。
4. **已定**:at-rest = **粗化明文存 RDS,同现状**,不二次加密、不为明文加特殊处理。(开放 · 留给后端:后续可改"加密 at-rest / 后端持密钥"版,见 §8;那时 agent 深挖不受影响。)
5. trend/history 工具:并进 `/v1/agent/perception` 还是独立端点。

> 字段一落地,以上填完即可**一次性建全**,无返工(架构字段无关)。
