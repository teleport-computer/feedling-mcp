# 主动性自定义开关 — 把 TA 的自主行为拆成用户可控开关 — 2026-07-06

作者:CC × Seven。状态:**待实现**。文案已定稿(双语)。
分工:后端(gate 拆分 + 字段 + state 契约 + 测试)→ **Codex**;consumer + iOS → **CC**。
流程:Codex 后端做完发 review_request → CC 审 + 起本地 PG 复跑全量回归 → CC 做 iOS(在最新
ios main 上,当前 `0820321` PR#56 新设置层级)+ xcodebuild → 都绿再 commit+push。

---

## 背景 / 为什么
Feedling 有一批**自主行为**(花 token、写用户数据、主动唤醒),现在要么写死、要么只被一个粗的
`ambient` 开关连带管。Seven:把它们拆成用户可控开关,**默认全开**(我们觉得最好的配置),
省 token / 要清净的用户自己关。核心洞见:**"陪伴/心跳关了 ≠ 停止做梦/记忆/看屏幕"**——现在
dream/capture 不看任何开关,perception 事件全被单个 ambient 连带管,预期不符。

## 关键结构改动:拆 `ambient`
现在 `backend/proactive/controls_v2.py:25`
`SELF_INITIATED_WAKE_SOURCES_V2 = {"heartbeat","perception_event","scene_change"}` 全部走
`SWITCH_AMBIENT`(controls_v2.py:211)。要给事件单独开关,**把"按粗 source 查 ambient"改成
"按具体 trigger 查各自开关"**:

| trigger | 归哪个开关 |
|---|---|
| `heartbeat*`(闲时) | `ambient`(=「心跳」,保留) |
| `photo_added` | `photo_wake_enabled`(新) |
| `arrived_at_anchor` | `arrival_wake_enabled`(新) |
| `unlock_after_absence` | `unlock_wake_enabled`(新) |
| `scene_change` / `screen_watch` | `screen_watch_enabled`(新) |

**语义(Seven 拍:方案 A)**:心跳只管闲时唤醒;事件各自独立开关、默认开。关「心跳」= 没事不主动找你,
但拍照/到地点/解锁/共享屏幕照样醒(各由自己开关控制)。
**向后兼容注意**:老用户没这些字段 → 全默认 True → 行为 = 事件照常。**但原来把 ambient 关掉、以为
全静音的老用户,拆分后会重新收到事件唤醒**(因为事件开关默认开)。Seven 已接受(默认全开=最佳配置)。

---

## 8 个开关(定稿文案,双语)

新增 **6 个 bool 字段**(全默认 `true`)进扁平 `proactive_settings` blob;`ambient`/`wake_interval_sec`
沿用已有,只改 iOS 文案。

| 字段 | 中文名 | 中文小字 | EN name | EN subtitle | 管什么 |
|---|---|---|---|---|---|
| `ambient`(旧) | 心跳 | TA 会时不时自己想起你，看看要不要找你 | Heartbeat | Every now and then he thinks of you, and sees if he should reach out | heartbeat 闲时唤醒 |
| `wake_interval_sec`(旧) | 心跳频率 | TA 隔多久主动想起你一次 | Heartbeat rhythm | How often he comes around to check on you | heartbeat 节奏(15m–12h,默认 2h) |
| `dream_enabled` 🆕 | 做梦 | 夜里默默整理你们的记忆，让 TA 越来越懂你 | Dreaming | At night he quietly weaves your moments together, and comes to know you better | dream_scheduler 夜间巩固 |
| `capture_enabled` 🆕 | 主动记忆 | 聊完，TA 会主动记下你在乎的事 | Remembering | After you talk, he holds on to what matters to you | capture(resident)+ model_api capture + model_api recap/consolidate |
| `screen_watch_enabled` 🆕 | 屏幕共享 | 共享屏幕时，TA 会留意屏幕，看看要不要搭话 | Screen sharing | While you share your screen, he follows along and may chime in | scene_change/screen_watch |
| `photo_wake_enabled` 🆕 | 照片唤醒 | 相册有新照片时，TA 会看一眼 | New photos | When a new photo shows up, he takes a quiet look | photo_added |
| `arrival_wake_enabled` 🆕 | 到地点唤醒 | 你到了新地方，TA 会留意到 | New places | When you arrive somewhere new, he notices | arrived_at_anchor |
| `unlock_wake_enabled` 🆕 | 解锁唤醒 | 你离开一阵子，再解锁手机回来时，TA 会察觉到 | Unlocking | When you unlock after being away a while, he senses you're back | unlock_after_absence |

**事件唤醒组·组级小字**
zh:这些时刻会让 TA 留意到你，要不要开口 TA 自己决定
EN:These moments let him notice you — whether to say anything is up to him

无 App 使用开关(归感知权限,授权即可用)。

---

## ① 后端(Codex)

### 1. `backend/core/store.py`
- `load_proactive_settings` default dict:加 6 个 `*_enabled = True`(dream/capture/screen_watch/photo_wake/arrival_wake/unlock)。
- 加载合并后:对这 6 个做 `bool()` 归一(缺省 True)——照 wake_interval 那样在 load 里兜底。
- `save_proactive_settings` `allowed` 白名单:加这 6 个 key;写入 `cur[key] = bool(value)`。

### 2. `backend/proactive/routes_asgi.py` + `proactive_core.py`
- `_proactive_state_doc`:返回这 6 个 bool(缺省 True)。
- `POST /v1/proactive/state` 子集白名单:加这 6 个 key。

### 3. `backend/proactive/controls_v2.py` + `gate.py`(核心:拆 ambient)
- 把 `_build_proactive_v2_wake_decision` / `evaluate_wake_control_v2` 从"self-initiated → 查 ambient"
  改成**按 trigger 查对应开关**(见上表)。`heartbeat*`→ambient 不变;事件 trigger 各查自己的
  `*_enabled`(缺省 True=放行)。block reason 分别用 `photo_wake_disabled` / `arrival_wake_disabled` /
  `unlock_wake_disabled` / `screen_watch_disabled`。
- 保持:manual/user-initiated 永远放行;激活门(activation_pending)优先级不变。

### 4. dream / capture gate
- `backend/proactive/dream_scheduler.py::tick_memory_dream`:开头读 `store.load_proactive_settings()`,
  `dream_enabled` False → 直接返回(不 enqueue memory_dream)。
- `backend/proactive/capture_scheduler.py::tick_quiet_capture`(resident)+ hosted model_api capture
  (`backend/hosted/turn.py` 的 `FEEDLING_MODEL_API_MEMORY_CAPTURE` 那条 turn 触发)+ model_api recap
  (`_model_api_recap_due`):都加 `capture_enabled` False → skip。**capture 和 recap 共用一个开关**(Seven:80轮整理折进主动记忆)。

### 5. 测试(必须补,防覆盖盲区)
- gate:每个事件 trigger,对应开关 off → block(对应 reason)、on → 放行;heartbeat 只受 ambient、
  不受事件开关;manual 永远放行。
- state round-trip:6 个 bool 默认 True、可 patch False/True、老用户无字段→True。
- dream/capture:`dream_enabled=False` → tick 不 enqueue;`capture_enabled=False` → capture+recap 都 skip;
  True → 正常。

## ② consumer(CC)
- `tools/chat_resident_consumer.py`:screen_watch tick 前尊重 `screen_watch_enabled`(从拉到的 proactive
  state 或 tick decision 读)——避免关了还发 tick(后端 gate 也会拦,consumer 短路省一次往返)。其余
  开关后端 gate 即够,consumer 无需改。

## ③ iOS(CC)——在 `0820321`(PR#56 新层级)上
- 位置:**自定义 → 主动性**(`customizationSettingsList` → `.proactive` → `proactiveCard`)。
- `proactiveCard` 里按组加(在现有 ambient/scheduled/reminders/interval 基础上):
  - 心跳组:心跳(ambient 改名)、心跳频率(interval 改名)
  - 记忆组:做梦、主动记忆
  - 事件唤醒组:照片唤醒、到地点唤醒、解锁唤醒 + 组级小字
  - 屏幕共享:屏幕共享
- `FeedlingAPI.swift`:6 个 `@Published proactive*Enabled`(默认 true)+ `ProactiveStateResponse` 解码
  6 个 `*_enabled` + `updateProactiveSwitch` 加 6 个 `Bool?` 参 + `applyProactiveState` 映射。
- `Localizable.xcstrings`:上表全部中英文案 + 改 `settings.proactive.ambient.name`/`.description` 成「心跳」
  文案、`wake_interval` 相关成「心跳频率」。
- 契约:`POST/GET /v1/proactive/state` 带这 6 个 bool。

## 验收 / 自测(强制)
- 后端:CC 起本地 PG 跑聚焦 + **全量回归**(清洁 origin/test worktree 隔离 pre-existing)。
- iOS:`xcodebuild FeedlingTest -sdk iphonesimulator` BUILD SUCCEEDED。
- 真机 e2e(Seven):逐个开关关掉 → 对应行为停;默认全开体验正常。
