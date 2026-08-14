# Runtime V2 能力 → 测试 对照清单

> 2026-08-14 建。**这份不是"V2 很健康"的说明书,是找洞用的。**
> Seven 明确:V2 被灰测用户用过之后暴露了非常多问题,基础看着没事、深挖一堆。
> 所以每条能力问的是「**它真的能用吗**」,而不是「测试过了吗」。
>
> 判定口径:
> - **行为测试** = 真的调用/驱动这个能力的测试
> - **记账测试** = 只断言"这个名字在某个集合里"(如 `assert "x" in READ_ACTIONS`)。
>   这类测试**一条都不能算数** —— 它连功能被删了都发现不了,只能发现名字被删了。
> - **e2e** = 部署态真实模型调用过

---

## 一、先说结论:三个已确认的洞

### 🔴 洞 1:语音通话记录读取,零行为测试

`voice_transcript_list` / `voice_transcript_read` —— 用户的语音通话逐字记录。

- 实现是有的(`backend/capabilities/voice.py`,经 `capabilities/registry.py` 派发)
- 但它名下的 3 条"测试"**全是名字成员检查**:
  `assert "voice_transcript_read" in worker._PRIVATE_READ_TOOLS`
  连唯一那条"行为测试"(`test_voice_transcript_outbound_gate.py`)也只断言集合成员关系
- **e2e = 0**:从没有真实模型在真实回合里调用过它

**这意味着**:如果它今天就是坏的,现有测试全绿,没有任何信号。

### 🔴 洞 2:14/20 的工具从没被真实模型调用过

e2e 覆盖为 0 的工具:`chat_image_read` `identity_get` `memory_search`
`perception_history` `perception_recent_apps` `perception_snapshot` `perception_trend`
`screen_read` `screen_recent` `voice_transcript_list` `voice_transcript_read`
`web_fetch` `web_search`

单测覆盖不低(web_search 有 38 条),**但全部是喂假数据驱动的**。
模型真会不会调、调的时候参数长什么样、返回值它读不读得懂 —— 没有证据。
"灰测用户撞一堆问题"的典型产地就是这里。

### ⚠️ 洞 3:`chat_image_read` 的唯一行为测试测的不是它

`test_v2_context.py:762` 测的是**结果折叠/投影**(拿一个假的 ok 结构去折),
不是"能不能真读到那张图"。看图这件事在 V2 上没有真实覆盖。


### 📌 旁证:一个刚被独立查实的同族案例(claude4, T018)

**症状**:V2 上 MCP / 工具只能被调用一次,后续轮次静默失效。
**根因**:不是模型、不是 MCP 服务器,是我们自己在
`backend/model_api_runtime/v2/tool_loop.py` 的三条出站围栏
**在后续轮次把工具静默摘掉**(sxysun 2026-07-18/19 写入,
原意是防 prompt-injection,但把「用户自己挑的 MCP 服务器」和
「模型自己搜到的网页」当成了同一个威胁模型)。
**已修**:57b4aedb(08-12),决策记录 `docs/MCP_TRUST_BOUNDARY.md`,
prod 镜像 b2697ae 已含。claude4 做了突变验证:把围栏加回去 → 7 条测试变红。

**为什么它是本文的旁证**:
1. 这个 bug 出在 `tool_loop.py` —— **所有 20 个工具共用的那条路**。
   也就是说单个工具的单测再厚,也测不到"第二次调用被静默摘掉"这种事。
2. 症状是**静默**降级:第一次能用、之后失效,而测试只测第一次。
   这正是本文关心的那道缝——测试测的是我们以为的用法,
   用户走的是「聊着聊着又要用一次」的真实用法。
3. ⚠️ 后续如果有人提「把 MCP 围栏加回来」,先要求他读
   `docs/MCP_TRUST_BOUNDARY.md`;`tests/test_v2_tool_loop_mcp.py` 变红
   就是围栏被加回来的信号。


### 📌 旁证二:工具调用标签泄漏进用户可见回复(codex2, T016)

**症状**:V2 的可见回复里混进了 `<tool_name>` 这类工具调用 XML 标签 ——
用户直接看到内部实现细节。
**性质**:又一条**共用路径**问题(回复清洗层),而不是某个工具自己的 bug。
codex2 的修法也印证了这点:做的是**共享封闭词表清洗器**,然后在
V2 前台道 + 主动道两处接线 —— 一处根因、两处出口。

**它再次说明**:V2 的问题集中在「所有工具/所有 lane 共同流经的那几段」,
单工具视角的测试天然看不见。本文第二节那张按工具排的表,
对这类问题是**盲的** —— 补测试时别只顺着那张表走。

---

## 二、20 个工具的覆盖实况

| 工具 | 行为测试 | e2e | 风险 |
|---|:--:|:--:|---|
| `web_search` | 38 | 0 | 单测厚但没真调过 |
| `memory_write` | 26 | 1 | 相对可信 |
| `web_fetch` | 27 | 0 | 单测厚但没真调过 |
| `memory_index` | 25 | 1 | 相对可信 |
| `memory_search` | 19 | 0 | ⚠️ 无 e2e |
| `schedule_wake` | 19 | 3 | ✅ 覆盖最好 |
| `perception_snapshot` | 16 | 0 | ⚠️ 无 e2e |
| `memory_fetch` | 15 | 1 | 相对可信 |
| `identity_patch` | 11 | 1 | 相对可信 |
| `screen_read` | 11 | 0 | ⚠️ 无 e2e |
| `identity_get` | 8 | 0 | ⚠️ 无 e2e |
| `identity_nudge` | 6 | 1 | 相对可信 |
| `screen_recent` | 6 | 0 | ⚠️ 无 e2e |
| `perception_history` | 5 | 0 | ⚠️ 无 e2e |
| `perception_trend` | 5 | 0 | ⚠️ 无 e2e |
| `perception_recent_apps` | 3 | 0 | ⚠️ 无 e2e |
| `chat_image_read` | 1 | 0 | 🔴 那 1 条还不是测它 |
| `voice_transcript_list` | 1 | 0 | 🔴 只有记账 |
| `voice_transcript_read` | 1 | 0 | 🔴 只有记账 |

---

## 三、V2 的 8 条 lane(用户会遇到的场景入口)

| lane | 用户视角是什么 | 主要守卫测试 |
|---|---|---|
| `chat` | 用户发消息,AI 回 | `test_v2_p0_unified_loop.py` 等 |
| `heartbeat` | AI 自己开口(心跳) | `test_proactive_runtime_v2.py` |
| `screen_watch` | 用户共享屏幕时 AI 主动说 | `test_v2_screen_watch_lane.py` |
| `scheduled` | 用户约的提醒到点 | `test_proactive_scheduled_wake_v2.py` |
| `manual_wake` | 手动触发唤醒 | `test_proactive_runtime_v2.py` |
| `capture` | 聊天蒸成记忆卡 | `test_v2_extraction_lanes.py` |
| `dream` | 每日整理花园 | `test_v2_extraction_lanes.py` |
| `maintenance` | 后台维护 | `test_v2_runtime_health.py` |

---

## 四、怎么用这份清单

1. **要动某个能力之前**:看它这一行。行为测试少 / e2e=0 的,
   改完必须自己实跑一遍,不能只看 pytest 绿。
2. **排查线上问题时**:如果症状落在 e2e=0 的工具上,
   **优先怀疑它从来就没真正work过**,而不是"最近改坏了"。
3. **补测试的优先级**:按本文第一节的三个洞排,先补语音通话记录。

## 五、这份清单还没做的事(诚实标注)

- 只覆盖了**工具面**;lane 的行为覆盖只列了主要守卫,没有逐条核对
- "相对可信"只表示有 e2e 存在,**没有验证那些 e2e 真的会在功能坏掉时变红**
  (验法:`~/fleet/bus/mutation_check.sh`)
- 没有对照真实用户反馈 —— Seven 手上的灰测问题清单还没并进来,
  并进来之后应该能直接对上号
