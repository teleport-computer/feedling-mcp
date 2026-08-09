# Feedling MCP — Changelog

> Landmark diffs over time. Two months from now, this is how we remember
> when a decision was made and why.
>
> Source-of-truth for "where we are now" is this changelog plus `git log`.
> `PROJECT_BRIEF.md` and `ROADMAP.md` were retired 2026-04-20 — historical
> references to them below are preserved verbatim.

---

## 给 Claude Code 的说明

**每次开新对话时**，请按顺序读：
1. `CHANGELOG.md`（最近的变化——尤其是最上面 3-5 条）
2. `CLAUDE.md`（当前 repo-level guardrails；`HANDOFF.md` 已删除）

**每次完成一个 task 或做出决策时**，在文档顶部追加一条记录。格式见下面。

---

## 记录格式

每条记录格式统一：

```
## YYYY-MM-DD

### [TAG] 一句话标题
- 改了什么 / 发生了什么
- 为什么改（如果是决策类）
- 影响哪些文档 / 任务
```

**Tag 用哪些**：

| Tag | 用在 |
|-----|------|
| `[DECISION]` | Open Decision 被拍板（记录拍了什么、为什么） |
| `[DONE]` | Task 完成标记 |
| `[BLOCKER]` | 遇到卡住的问题（不是普通 bug，是影响方向的） |
| `[PIVOT]` | 产品方向的重要调整 |
| `[UI]` | UI 设计稿更新或 UI_SPEC 变化 |
| `[FEEDBACK]` | 内测反馈驱动的改动 |

---

## 记录正文（最新的在上面）

## 2026-08-09 — 地址不是 API 端点时,别把它说成「API key 未通过测试」

**[DONE] 用户报障:两个中转站都提示 key 未通过测试;实测两站完全健康,真因是 base_url 漏了结尾的 `/v1`。**

- 我们把 **URL 问题的报错说成了 key 问题**,用户一直在换 key。判据落在
  `setup_core._looks_like_wrong_api_endpoint`,四个入口(保存配置 / 手动测试 /
  加路由 / 改凭证)统一走 `_provider_test_failed_body`,避免同一错误从不同入口
  说法不一。
- 判定为地址问题时把 `status_code` 清成 `null`:客户端会把 provider 的 404
  映射成「模型不存在」,不清就是换一句话继续指错方向。原始错误仍写进
  `route.test_error`。
- **⚠️ 判据必须按状态码收窄(codex2 gatekeep 抓到的 blocker)**:我原先断言
  「真错误一律是 JSON」,但 relay/WAF/计费层会用 **HTML 页面**返回
  401/402/429/5xx —— 本仓 `tests/test_catalog_consumer_parity.py:158-161`
  就存着这样的样本。HTML 只在 **404** 时才算地址问题,其余状态码原样透传,
  否则额度不足/鉴权失败/限流会被一并说成地址错误,比原来的错更严重。
- 刻意**不**自动补 `/v1`:6 个 provider 里 gemini(`/v1beta`)、bedrock、
  deepseek 本就不是 `/v1` 结尾;且 `base_url` 属敏感字段、admin 不下发,
  改一个看不见影响面的东西等于拿存量用户赌。也不替用户拼具体地址(有的站
  要 `/api/v1`、`/openai/v1`,猜错让用户照抄后更迷惑)。
- 不需要 iOS 改动:复用 `provider_test_failed` 老 slug,现有 App 直接显示 detail。

## 2026-08-08 — 思维链泄漏统一闸

**问题**：模型自己写的 `<think>` 心里话漏进用户聊天气泡。**三个出口各漏各的**，
且三条路此前用的是**两套不同判据 + 一处没有**：

| 出口 | 漏的形状 | 原因 |
|---|---|---|
| V2 聊天 | 模型一轮写了两个块 | 剥离器只处理开头第一块，第二块原样进气泡 |
| V1 聊天 | 孤立的 `</think>`（开标签被上游吃掉） | 正则要求开闭成对，配不上对 → 整段原样放行 |
| 主动消息 | 任何 think | **一处剥离都没有** |

共同毛病是 **fail-open**：遇到不认识的形状就把原文端给用户。

**自我繁殖**：漏出去的内容原样存进 `chat_messages` 正文（正常情况下思考封在
**另一个**信封里、模型永远看不到），下一轮当历史喂回去 → 模型看到「我上一条就
这么写的」→ 照抄 → 漏得更多。这解释了为什么主动消息那条 lane 我们**根本没在
提示词里要求**它写 think，它却照写不误。

**改动**

- 新增 `core.self_thinking.strip_all_thinking()`：扫全文剥所有完整块 + 孤立闭
  标签前缀，剥完仍有标签残留则 **fail-CLOSED**（返回 `FAILED`，调用方不发）。
  `split_thinking()` 一行未动，只增不改。
- 四个对外出口（V2 聊天 / V2 唤醒 / V1 聊天 / 主动消息）+ 一个历史入口全部改调
  同一个函数，判据统一。
- 指令换版：从「每一次输出都写、工具轮也写」改成「**只在不再调工具的那次输出
  写一个块，内容覆盖整轮**」。13 模型 × 2 遍实测：能写的 6 个模型 12/12 全对
  （1 块 / 在开头 / 正文零残留 / 工具轮 0 个）；`gpt-5` 在旧指令下会拒绝
  （"抱歉，我不能分享我的内部推理" + 整段转英文），新指令下两遍都正常。

**开关**：`FEEDLING_THINK_GATE`，**默认开**。关掉后五个调用点对同样的输入逐字回到
改动前的**解析 / 出口 / 历史处理**行为，用于线上出问题时立刻止血 —— 是 kill switch，
不是灰度门。**注意它不回滚提示词**：`INSTRUCTION` 归 `FEEDLING_V2_SELF_THINKING` 管，
所以关掉本开关不等于系统整体回到基点（Codex review 2026-08-08 指出，原文案说成
「逐字节回到改动前」是不成立的）。

**与 `FEEDLING_V2_SELF_THINKING` 解耦**：安全剥离不再挂在 self-thinking 开关下。
那是公开支持的自托管配置，关掉它时模型仍可能自己写 `<think>`（主动消息那条 lane
就是实证：我们从没要求它写，它照写），此前的写法会在那种配置下完全跳过剥离，等于
把心里话原样发给用户。现在的判定是「闸开 → 一律剥离；闸关但 self-thinking 开 →
旧行为；两者都关 → 不处理」，而**展示与否**仍然只看 `FEEDLING_V2_SELF_THINKING`。

**放弃的替代方案**：给模型一个专门的 think 工具走结构化交付（`tool_calls`）。
实测 gpt-5.4 **不会**把正文和 think 工具调用放进同一次输出，会多一次 API 往返；
2026-08-08 定为「先不做」。

**已知取舍**

1. **误剥**：用户或 io 正常聊天里提到 `</think>` 这几个字会被当协议残留剥掉。
   低频，而泄漏每天都在发生 —— 优先堵漏（hx 2026-08-08 明确「误杀先不考虑」）。
2. **4 个模型完全不写 think**（openrouter-deepseek-r1 / glm / 中转·哈吉米 /
   中转·空悲切）→ 这些用户看不到「推理过程」。是"没有"不是"漏"，之后单独处理。
3. V1 的展示格式**不变**（保留换行、上限 700）：内核提供 `sanitize=False`，本次
   统一的是剥离**判据**，不顺带改展示。

**上线状态**：⬜ 未上线。backend 部分随 test 分支 CI 自动出镜像 + 部署 CVM；
`tools/chat_resident_consumer.py` **CI 不管**，必须手动上 VPS
`systemctl restart feedling-chat-resident` 才生效。

## 2026-08-07 — 语音挂断取消与迟到回复隔离

**[DONE] 失败通话不再把迟到 AI 回复写进普通聊天。**

- 新增幂等 `POST /v1/voice/cancel` 和持久 call lifecycle tombstone；取消会
  清理未归档逐轮消息及临时语音结果。
- Resident 与 Hosted Runtime V2 都在最终消息事务内检查 lifecycle，并把
  `voice_call_id`、`voice_turn_id`、`reply_to_message_id` 完整落到回复行。
- finalize 在写 archive/card 前先取得 lifecycle；cancel 与 finalize 只会有
  一个赢家，不会出现 cancelled 状态却残留半套归档。

## 2026-08-07 — Dashboard 卡片语义修正（prod 第二日实景反馈）

**[FEEDBACK] 四处「数字对不上/误导」全部修正，根因都是口径与分母。**

- 导入卡红灯顶 90% 大数字：分母从 completed 改为终态（completed+failed）
  ——失败的导入到不了 completed，旧分母把 3 个失败藏进大数字（9/10=90%
  配 23.1% 失败率的自相矛盾）。started>0 但无终态时显「—」不显 0%。
- 漏斗 W1 假流失：builder 的 w1_eligible（窗口已走完人数）现在随 payload
  下发，转化行分母用它——旧渲染拿 t3 总数当分母，把「注册不满 14 天、
  窗口未到期」的人标成流失（实景 ↓40%·流失 75，实际 91 人成熟、33 人
  未到期）。标签改「次周仍活跃（第 8–14 天）」防误读成 WAU。
- 同词不同尺加标尺名：用户页判定句「近1日活跃」改「近1日发过消息」
  （滚动 24h 真人消息），与「日活与时长」页 打开过App DAU（北京自然日）
  是两把尺、无需对上——hint 里显式写明；产品健康 WAU 标签补「打开过
  App」。chat/延迟卡「无样本」标注 Hosted V2 通道（V1/BYOK 不经此路径，
  不代表没人聊天）。
- 小样本环比不染色（两侧 <20 中性灰，17→4 的 −76% 红是真数字假信号）；
  首页队列同因 >3 条折叠（注册波刷屏把 stalled 挤下屏）。
- 测试 299 通过（含四组新回归）。


## 2026-08-07 — 空回复归因：中转抽风不再算作「系统出了问题」

**[DONE] prod 用户 usr_7f30d63f 报「今天好几次接不上」，分诊查实是他的中转不稳，但我们的错误归因把锅背到了自己头上。**

- 现场：他的中转 08-05 上午配额爆掉后开始间歇返回 **HTTP 200 + 空内容**
  （断流 / 配额紧张时的假成功）。我们清洗后为空 → 一律 `reply_parse_failed`
  （blame=system，文案「系统处理回复时出了问题」）→ 用户自然来找我们。
  同一场中转故障，前半场（规矩报错）归因正确，后半场（假成功）归因错误。
- **归因边界**：模型本来就没给内容 = provider（新类 `provider_empty_reply`，
  blame=provider_transient）；给过内容、被我们的清洗/压制掏空 = 我们
  （`reply_parse_failed` 不变）。
- 落点：标记由**四个 helper 抛出点**铸造（openai / simple / cli generic / pi）
  —— 生产 helper 在返回前就抛异常，只在 `call_agent` 里判空是死代码；
  分类器把空回复判定排在 `_ERROR_CLASS_RULES` **之后**，且抛出时带上 body 的
  协议层诊断（`error.message` / `finish_reason`），这样 `200 + insufficient_quota`
  这种中转标准形状仍然命中 `quota_insufficient` 而不是被空回复遮蔽。
- V2 侧：`TurnError("empty_reply")` 归 provider；但 **`tool_budget_exhausted`
  拆成独立 reason**（跑光的是我们自己的 `_TURN_MAX_LLM_CALLS`，不是中转的错），
  wake `scheduled` 的 self-thinking SILENT 也归 system（模型给过完整 `<think>`、
  是我们剥空的）。
- 三轮 gatekeep（codex2 两轮 + 一次独立对抗预检）抓到的关键缺陷：①第一版
  测试用 lambda 返回 `""` 绕过真实 helper 边界＝假绿，核心工单场景根本没修到；
  ②`_raw_assistant_text` 是记忆车道的窄提取器（不读 top-level messages/actions），
  拿它回头判空会把**我们自己的协议泄漏压制**误判成 provider 空回复 —— 改为在
  压制**之前**快照；③标记加 `feedling:` 命名空间，否则 pi 透传的 provider 文本
  可能原样命中而劫持归因。
- 文档：`docs/FRONTEND_ERROR_CONTRACT.md`、`docs/API_ERRORS.md` 的 error_class
  闭集，以及 docs-site changelog（用户可见文案变更）同批更新。
- ⚠️ 用户可见变化：按 §2.3 显示矩阵，`provider_transient` 隐藏兜底气泡、只出
  失败横幅（与 `rate_limited` / `upstream_unavailable` 同阵营，非本批新增形态）。

## 2026-08-06 — Dashboard prod 首日热路径修补

**[DONE] IA v2 上 prod 首日实测（首页 ~8s、产品健康顶 30s deadline、verdicts 每次 5-8s）暴露三处，全部修复。**

- 迁移 0079：`chat_messages(ts)` 普通索引 + `user_logs(ts) WHERE
  stream='proactive_jobs'` 部分索引——都是 0078 时"等 prod EXPLAIN"的
  存量 follow-up，证据到齐即落地。服务首页队列/事件流、产品健康回复率、
  铁杆 census（此前 30s 超时主因）与既有主动任务日报。
- 首页队列 model_config_pending 加近 14 天活动过滤（chat ∪
  app_session_end，两臂都有索引）：prod 首日 20 条截断，多为弃置账号的
  历史坏配置——幽灵账号不是"需要你"的工单。
- verdicts JSON 加 30s TTL 缓存 + single-flight：原"逐请求重建"假设在
  prod 数据量下不成立（agent 轮询持续压库）；诚实通道改为 payload 一等
  字段 `cached`/`cache_age_sec`（HTML 有 cache-note，JSON 有这个）。
- 测试 297 通过（含 0079 head-pin 更新、队列幽灵过滤回归、缓存语义翻转）。


## 2026-08-06 — Admin dashboard IA v2：首页 + 导航 13→4 + 统一漏斗

**[DONE] /admin/data-track 默认页改为「首页」：判定条 + 用户队列 + 产品脉搏 + 事件流 + 成本行；导航收敛为 首页/产品健康/用户/诊断 四项。**

- 设计原则落地：频率决定层级（每天看的在首屏零点击，出事才看的 11 个
  诊断页收进 `view=diag` 枢纽 + 二级行，旧 URL 全部不变）；名单强于比率
  （「需要你的用户」直接列出 有去无回/onboarding 卡住/模型配置待处理 的
  具体用户行，封顶 20 + truncated 标记；resident 掉线检测因在内存态、
  页面如实标注缺口）；无趋势不成信息（脉搏卡全部带 7 日 sparkline）。
- 状态条四灯：系统 = 既有三把 `_ops_*_level` 尺子取最差（bad>warn>
  unknown>ok，合成在 admin_core——db 不许 import data_track）；增长 =
  WAU 环比（带 min-n 护栏，小分母不告警）；成本 = token 3× 中位数跑飞
  检测；数据完整性 = **永远灰**直到 ACK/session 来源缺口闭合。
- 统一漏斗 `admin_funnel_snapshot`：注册→已连接→内容就绪→首次真回复→
  W1 仍活跃，严格单调、28 天窗口带前窗对照、W1 未成熟显 None；首页迷你
  版 + 用户页完整版（带逐级掉落与前窗），替换掉用户页原来那组**不单调
  的独立行为百分比条**（原数据保留在折叠的人群记账 details 里）。
- 新 JSON 端点 `GET /v1/admin/data-track/verdicts`（admin 鉴权同级、不进
  页面缓存）：给 agent 读的判定 + 队列 + 脉搏，与页面同一套 builder。
- 两轮对抗审查修复 10 处（3 major：主动消息把"有去无回"误清；成本判定
  绿灯却给"可判定天数不足"理由；激活率标题渲染进行中周为定数。7 minor
  含 min-n 护栏、成熟死 cohort W1 显 0 不显 —、非有限数崩页、孤儿 CSS、
  user_id 转义口径统一等）。种子舰队端到端验证（412 账号）：首页冷构
  ~25ms vs 用户页 ~100ms（本地）。测试 296 通过；`test_asgi_admin` 两个
  parity 失败为存量（cache-note vs 旧断言，已另立任务）。

## 2026-08-05 — Dream 阀门重构：拆内容闸、只留确定性「明显不对」闸（V1/V2 同步）

**[DECISION]+[DONE] usr_a40e 墓碑卡事故复盘，Seven 定产品哲学：出口只拦「明显不对」，绝不判内容质量、绝不拒绝内容上的可能性。**

- 现场：deepseek-v4-pro 把 dream supersede 语义理解反，把「已被 <卡id> 取代——原文」
  记账注记写进新卡 summary/content；占位符闸（实义文字，过）、语义审查员（同一弱模型
  自审自查，放行）、15% 增量栅栏（多卡合并绕过）三道防线全没拦住，花园展示出墓碑卡。
- **拆**（两条 lane 同步，V1 consumer + V2 extraction/worker）：
  - 15% 增量栅栏（`_has_substantive_increment` 两份拷贝）——内容质量判断，
    「更短但更准」的合法改写会被判死；
  - 逐提案语义审查员（`_review_dream_consolidations` 两份实现 + review prompt/parser）
    ——既误放（本次）也误杀（fail-closed 连审查调用挂了都毙提案），每条提案还多烧
    一次用户 BYOK 调用。
- **加**（共享一份，不再两处复制）：
  - `memory/dream_gates.py`：卡 id 泄漏闸（result 硬字段含花园真实卡 id → 与内容闸
    同路打回重问，零误伤）+ 爆炸半径保险丝（单晚退休 > 活跃卡 80% 且 ≥10 张 →
    整个 job 失败不部分执行；env `FEEDLING_DREAM_FUSE_RATIO`/`FEEDLING_DREAM_FUSE_MIN_CARDS`）；
  - `card_text.py` 墓碑短语闸（`已被/superseded by + ≥8位hex`，capture/dream 全 lane
    兜底；裸「取代」散文不误伤）；
  - dream prompt 红线补一句：result 写新卡内容本身、绝不出现卡 id（仅一句，
    prompt 不膨胀——硬约束在出口代码里，见复盘讨论）。
- **刻度**：`memory_lane_health` 增加 `failed_reasons`（按 `last_error` 细分，
  「保险丝熔断」和「provider 挂」分开看）；V2 dream 记 `dream_funnel` trajectory
  （proposals/applied/superseding）；V1 `dream_result` 增 `active_cards`+`content_gate`。
- 测试：`test_dream_gates.py` 新增（含 **V1/V2 跨 lane 一致性锁**：同一份
  consolidations 两条 lane 必须退休同一批卡）；lanes/integration/consumer 各测试
  迁到新语义；673+ 相关用例全绿。
- 待办：usr_a40e 花园复原（recover CLI 走 Actions，时间窗定位墓碑批次，dry-run
  清单过目后 apply）——修复部署 prod 之后做，防止下一晚 dream 再产墓碑。

## 2026-08-04 — 新增 Admin「产品健康」view（留存/激活/强度/证据缺口）

**[DONE] /admin/data-track?view=health：投资人级产品指标常态化进 dashboard，全部只用现库可证实的数。**

- 四个问题分区：①用户留下来了吗（`retention_cohort_snapshot` 冻结 cohort
  W1/W2/W4/W8 热力表——只读快照、未成熟显「—」不做实时补算；WAU/粘性/L1-L7
  分布用 `app_session_end` 使用口径）；②新用户能激活吗（复用带 cutoff 的
  onboarding funnel：cohort×t1/t2/t3 趋势 + 注册→首真回复中位时长 + W4
  激活者-vs-全量留存分层，coverage 不完整显「未知」）；③强度是真的吗
  （Top10% session/token 集中度——token 标注仅 V2 灰度；铁杆用户 census =
  连续 4 周每周 ≥5 个 admitted **非心跳** 主动 job，V1+V2 wake 合并、
  排除维护类/限流 skip/自主心跳；主动消息 24h 回复率右删失下界）；
  ④还缺什么证据（常开缺口清单：session 来源标记、客户端已读 ACK、BYOK
  自付不可见——各自阻塞哪个核心指标）。
- 诚实性规则全页强制（两轮对抗审查后修复 9 处）：进行中的北京周不渲染
  成已定数字（流失/净变化/激活率/铁杆数全部跳过或标「进行中·不可判定」）；
  funnel 故障传导为「未知」而非 0；W4 激活标记冻结在 W4 窗口开始前
  （成熟 cohort 不再回溯漂移）；session 与 token 集中度同用完整北京日窗口。
- 复用 commit 868281284 的底座：共享 admin-ops executor 并行 8 个
  builder（本地实测 2-30ms/个）、60s 缓存、`[admin:perf]` 计时、逐 builder
  失败域。宽口径快照 vs 使用口径两把尺子在口径说明中显式声明，不可互读。
- 测试：`tests/test_admin_product_health_view.py`（14 项：8 个 builder 的
  种子数据语义 + 渲染诚实性/缓存/日志无 secret）。仍未做：0079 可选
  `proactive_jobs` 部分索引（视 prod EXPLAIN）、`chat_messages(ts)` 索引
  （回复率查询目前顺扫、60s 缓存兜底）。

## 2026-08-04 — Admin 运营总览提速 + 布局重做

**[DONE] /admin/data-track 性能修复（overview 4.5s TTFB → 缓存命中亚秒）+ 运营总览布局按「问题→判定→证据」重排。**

- 性能四件套：① `admin_onboarding_funnel` 新增 `registered_cutoff_ts`——
  KPI 调用不再全量扫 `chat_messages`×2 / `user_logs` / `memory_moments`
  （cohort user_id 过滤让里程碑 CTE 走既有 `(user_id,…)` 索引；`view=events`
  的全量 funnel 页路径逐字节不变）；② overview 七个 report builder 并行
  （ThreadPoolExecutor，逐 builder 失败域与日志语义保持原样，worker 线程
  不碰 reqctx 绑定）；③ `page_html` 60s TTL 缓存（single-flight、
  stale-on-error、5s 失败冷却、600s 硬保留清扫；key 是含 admin_key 的
  first-value-wins 规范参数串的 sha256 摘要——按鉴权通道隔离缓存条目、
  不落明文 secret，cookie 会话拿不到别人 query-key 构建的页面；
  `view=debug`（可带 reveal 明文）完全绕过缓存；命中必带
  「页面缓存 · 数据生成于 N 分钟前」诚实声明，置于 main 顶部）；④ 迁移 0078：`user_logs` app_session_end 部分索引
  （CONCURRENTLY，沿用 0074 的 invalid-shell 防护）。另：全部 ops builder
  加 `[admin:perf] builder=… elapsed_ms=…` 生产计时（codex 建议的第一步）。
- 环比基线：`recent_admin_product_kpis` / `recent_token_usage_by_lane` 新增
  `offset_hours`（前一窗口，半开区间；offset=0 行为与查询计划不变），
  overview 指标卡带 ▲/▼ 环比（onboarding cohort 明确不做环比）。
- 布局：四张问题卡改「比率大字 + 分数小字」且整卡可点进明细页；新增灰色
  `unknown`（证据不足）一等档位，与琥珀 warn（测得需注意/结构性证据缺口）
  分离——纯无证据不再显得像告警，chat 卡因 ACK 结构缺失仍保持 warn；每格
  指标带 '?' 一行口径悬浮；三段长口径说明折叠为 `<details>`；导航 tab
  分组为 质量/增长/系统（加上产品健康后共 13 个）。
- 三方对抗审查（SQL 窗口 / 并发缓存 / 渲染 XSS+UX）后修复：跨鉴权通道
  缓存互串（blocker）、7 线程 fan-out 打满共享 16 连接池（改共享 4 线程
  `admin-ops` executor）、import 卡 100% 失败窗口误显「无样本」、重复扣费卡
  测得候选仍显灰、6 个页面 nav 分组 CSS 缺失、环比 ≥10× 封顶、模型调用
  环比改中性、offset 窗口上界改半开、funnel 查询失败改返 None（覆盖率
  诚实显「未知」而非 0/0）。
- 测试：`tests/test_admin_dashboard_perf.py` + `tests/test_admin_kpi_windows.py`
  新增（funnel cohort 等价、offset 半开边界、缓存 single-flight/通道隔离/
  debug 绕过/硬保留、失败域降级、日志与缓存 key 不含 admin_key）；conftest
  增加 autouse 缓存清理 fixture。本地 PG14 上
  `test_admin_usage.py` 的 44 个失败为存量 `pg_input_is_valid`（PG16 函数）
  环境问题，与本改动无关（pristine origin/test 同样 44 失败）。
- 后续（未做）：`ops_snapshot` 汇总表（把 dau/users 视图 12-18s 也压下来）；
  客户端 ACK 采集（chat 卡变绿的前提，也是 fundraise「loop engagement」
  指标的前提）。

## 2026-08-05 — V2→V1 颗粒度对齐总攻（Seven 全权委托,一日闭环）

**[DONE] Runtime V2 体验收敛到 V1:人设注入、心跳自觉、工具面 parity、test 开关常态全开,live E2E 全绿。**

- 根因框架(三旋钮):①信息食谱——V2 原本**完全没有人设注入**(`persona`
  一词在 v2 runtime 不存在),已补(95bbd545,每 turn JIT 解密全文进 system
  前缀);②harness——wake 补 attention_facts+系统措辞禁令(a53a2923,叠加
  志豪 PR #158 的伪 user nudge 移除/wake 语义恢复/感知 baseline);③模型
  本性——弱模型不爱调工具,靠确定性注入兜底(保留的 V2 结构优势)。
- 工具面 parity(3f9d375d):新增 perception_recent_apps;memory_index 补
  ambient/include_sensitive、memory_fetch 补 limit/archived/superseded、
  memory_write 补 reason 审计;D4 改名校验前移;V1 agent_tools_prompt.md 的
  产品措辞逐条搬进工具描述。chat_image_read 判定为有意 limitation(P2 提案
  =vision-observer 模式)。DND 现在拦 heartbeat/screen_watch(d8b33a26)。
- test compose 定为「常态全开」:CAPTURE/PROFILE/DETERMINISTIC 硬编码 1
  (86f0763c/ecb5c055),守门测试按环境分派;**prod/pre 一字未动**。
- live E2E(本地 rig+真 deepseek-chat):人设首轮零工具在场✓、wake 无泄漏
  /无第三人称✓、DND 闸✓、capture 3 张精准卡✓、profile state=ok✓。两个
  假阴性已分诊(空料输入不写卡/不产画像=正确保守;会话中途改人设名被对话
  连续性带跑=V1 同病,验收看注入层)。
- 文档:docs/RUNTIME_V2_FLOWS.md(全流程报告,每 lane 触发→流程→prompt→
  副作用→V1 差异)、docs/RUNTIME_V2_PARITY.md(债务台账)。
- 遗留(归 Seven):prod 放 V2/用户迁回、PROFILE 上 prod、chat_image_read
  P2、reminder 列表 API P2。

## 2026-07-31 — V2 照片唤醒按需读取真实图片

**[FIX] V2 模型调用 `photo_read(include_image=true)` 后，现在会看到安全的视觉观察文本，而不再只有照片元数据。**

- `photo_read` 仅在模型明确请求图片时于后端进程内解密像素，并交给视觉观察器；base64 不进入模型工具结果、日志或持久化上下文。
- 优先使用用户配置的专用视觉路由；未配置时复用当前回合的主模型路由。专用路由失败时不会静默回退，避免意外跨越用户选择的信任边界。
- chat、wake 和只读子任务共用同一按需观察链路；模型未调用 `photo_read` 时不会读取或观察照片。
- 变更仅限 V2 后端，V1 路径与 iOS 授权/UI 行为不变。

## 2026-07-30 — Runtime 值班台补审计的三条实质缺口：capture 语义、完整窗口、端到端交付

**[FIX] 同一份外部审计里剩下的三条（前四条见下一条记录），按"页面自信地报绿最危险"排序做掉。**
分支 `fix/runtime-health-p1`，L1 `7280 passed / 0 failed`（基线 7251 + 新增 29 条）。

- **`capture.complete` 改名 `terminal_seen_no_gap`，且 `partial` 开始参与降级**。
  那个桶只证明"找到了 `turn_terminal` 事件且没有 `capture_gap`"，**不**证明 prompt /
  provider 往返 / tool call / 最终回复这些 artifact 齐全——叫 `complete` 会让人（和下一个
  改这段代码的人）以为轨迹可以完整回放。更严重的是 `_runtime_health_level` 此前**只看
  `missing`**：一个带 capture_gap 的回合在页面上既显示了 partial 计数、总体结论又写"正常"，
  审计截图里就是这个组合。缺口是真实的取证损失，现在至少 `warn`（`missing` 仍是 `bad`，
  取最差档，有测试钉住"warn 不会盖住 bad"）。
  姊妹函数 `recent_chat_operational_health` 仍用 `complete`/`complete_rate` 未改——那是
  `/model_api` 指标端点的对外契约，改名要单独走版本沟通。
- **健康侧去掉 `LIMIT 1000` 采样上界，与 token 列口径对齐**。四条子查询此前各带 1000 行
  上界，于是 24h 档写着"24 小时"、实际是"最近 1000 个 job"；而同页 token 查询从一开始就是
  窗口内全量。两列因此在 168h / 720h 档覆盖不同的时间跨度，**放在一行读却不是同一批样本**，
  且故障总量被静默少报。采样上界在这里不是性能旋钮而是正确性缺陷。回归测试插 1200 行
  （> 旧上界）断言 `sampled_jobs == 1200`，并已实测：把 LIMIT 加回去这条立刻变红。
  扫描量由新迁移 **0071** 的三条索引承担（`ix_agent_jobs_terminal_finished_at` /
  `ix_agent_jobs_created_at` / `ix_v2_turn_metrics_created_at`，全部 `CONCURRENTLY`，
  照 0048 的 invalid-空壳 重试处置）。
  ⚠️ **顺带修正一条此前写反的性能结论**：`ix_v2_turn_metrics_lane_created_at` 不是
  "用不上"，而是"对非前导列的范围谓词给不出范围收窄"。本机 PG 16 实测（30 万行 / 摊 60 天）：
  24h 档无新索引走 Seq Scan（4226 buffer）、有新索引走 Index Scan（1454 buffer）；720h 档
  规划器**确实会**去用那条复合索引，但退化成全索引扫描（21k+ buffer）。数字记在 0071 注释里。
- **新增「端到端交付」区块（`recent_delivery_health`）**。这是审计最实的一条：`agent_jobs`
  判 `completed` 只证明回合跑完了，不证明产物到达用户——副作用走 `v2_effect_outbox` 异步
  apply、用户可见的终态失败走 `v2_terminal_failure_outbox` 投递，这两条队列堵住时 job 层面
  一切正常，页面此前照样报绿。三块的窗口语义**刻意不同**：两个 outbox 是**当前积压状态**
  （不随窗口变化——三天前该 apply 的 effect 还堵着，那是现在的故障），`v2_mcp_mutation_attempts`
  的 unknown/unresolved 才是窗口内计数。判定只按**堵了多久**、不按积压条数（高吞吐下瞬时
  积压 5000 条是健康的）；MCP 结果未知不设阈值、见一条就 warn（稀有、不可自愈）。
  阈值刻意保守（1h warn / 6h bad）：稳态基线还没实测，值班台最不能犯的错是长期挂一条误报的
  红——那会训练出"这页的红不用看"。收紧前不要当灵敏告警使。
- **页面写明覆盖范围**：本页只统计本实例托管的 V2 回合，self-host consumer 只 best-effort
  上报部分元数据、离线实例完全不可见，所以 token 与失败率**不是全体用户的总量**。这一条是
  成本最低、防的却是最贵的误用（拿这页数字当全量用量账）。
- `admin_core` 的 runtime 分支现在是**三个独立失败域**：健康挂了才降级整页，token 或交付
  挂了各自只让对应区块显"取不到"（不是显 0——0 的含义是"确认过是零"，与"取不到"相反）。

**未做**（审计提到、本轮没碰）：worker build / heartbeat age（`recent_worker_heartbeats()`
已有数据，只差渲染）、provider health 参与判定、wake/proactive lane 健康、hourly rollup、
四层拆分（Runtime Health / Usage / Trace Coverage / Private Trace Viewer）。admin key 轮换与
移除 query-string auth 按产品决定不做。

---

## 2026-07-30 — 外部审计打回 Runtime 值班台的四处：日志漏脱敏、失败域过宽、参数假生效、两种 coverage 混列

**[FIX] 一次外部只读审计（针对 PR #124 + #129 上线后的生产页）指出四条，全部实测复现后修掉。**
前三条的共同形状是**页面自信地显示一个错误或误导性的东西，而不是报错**——值班台上这比崩溃更危险。

- **访问日志漏脱敏 `admin_key`（安全）**。`asgi/middleware.py:_display_path` 的判据是
  `k.lower() == "key"`，精确等于 `key`——而 admin 后台把凭据放在名为 `admin_key` 的
  query 参数里（`data_track._data_track_qs` 保留列表第一项，因此它出现在**每个**导航
  链接上）。于是可复用的 admin 凭据原样进了服务端访问日志，也进浏览器历史。
  判据改为**子串**匹配 `key/token/secret/password/passwd/auth`：精确匹配单个名字，等于
  把"以后不会出现别的别名"当前提，而这个前提在同一仓库里当时就不成立（`admin_key` /
  `admin_token` / `api_key` 三个都在用）。⚠️ 本次只修"以后不再泄漏"；**已经进过日志的
  那份凭据仍然有效**，轮换是独立的运维决定（按 `admin-password-rotate-needs-redeploy`
  的教训，改 secret 后必须重新部署才生效）。
- **token 查询失败会拖垮整张健康页**。`page_html` 的 runtime 分支原先两次数据调用共用
  一个 `try`，于是 token 聚合（无 LIMIT、走 seq scan、扫描量随表增长单调变大的那条）
  一旦超时，健康数据明明是好的、整页也退化成降级页——而这一页恰恰是出事时才被打开的。
  拆成**独立失败域**：health 挂了才降级，token 挂了只让两列显 `—`。这条是 PR #129 的
  spec §4 明确写的"任一数据源失败都走同一个降级页"，三轮 task review 都照 spec 检查、
  因此全部放过；只有从"这页什么时候被打开"的运维视角看才发现它错了。
- **`day` / `limit` / `offset` 在本页假生效**。runtime 页只读 `hours`，但那几个参数在
  全视图共用的 `_data_track_qs` 保留列表里，会一路跟着 URL 走。审计实证了代价：有人拿
  `?view=runtime&day=2026-07-25` 的截图当成"7 月 25 日的数据"，而页面渲染的其实是生成
  时刻向前 24 小时，页顶还写着「窗口 24 小时」，读者不会怀疑自己看错日期。本页自己的
  控件（三个窗口按钮）不再传播它们，说明区写明"本页只按 hours"。顶部 nav 由共用组件
  生成，不给它开单页面特例——那会把页面知识倒灌进通用逻辑，改由说明文字兜。
- **两种 coverage 混在一列**。原先「缓存命中 · 上报」= `cache_hit_ratio · usage_coverage`，
  那个"上报"指 token usage 上报，读者会当成 cache 上报。而 `cache_reported_calls` 一直
  在写入路径采集、聚合查询从没取过它——真正的 cache coverage 此前不可得。数据层补
  `cache_reported_calls` / `cache_coverage`，渲染拆成「缓存命中」+「上报 usage/cache」
  两列（12 → 13 列，空状态 colspan 同步）。

L1 全量 7250 passed / 0 failed。无迁移。

审计同时给出的方向性判断（**未在本次实施**，作为后续依据）：当前 Runtime 页是"值班摘要"
而非完整 telemetry dashboard，约展示了后端已采集数据的 45%；delivery/outbox、MCP unknown
outcome、wake 成功率、worker build/heartbeat age、self-host 覆盖标注均尚未上页；建议最终
拆成 Runtime Health / Usage / Trace Coverage / Private Trace Viewer 四层。⚠️ 审计引用的
基线文档 `2026-07-11-agent-trajectory-telemetry-requirements.md` **不在本仓库**（`find` +
`git log --all` 均无，它在审计者本机路径下）——想按那份愿景推进的人需要先向审计方索取，
不要在本仓库里找。

## 2026-07-30 — CIPHERTEXT lane 补上删除传播的兜底：853 行"已删还在"

**[DONE] 新增 `tee_shadow/ciphertext_prune.py`**，按主键集合差集删掉 TEE 侧的残留行，
挂在 `tee_sync_scheduler` 的 reconcile 档（默认每天一次）。配套 `alembic 0068`
给 `tee_sync_runs` 加三个扁平列（`prune_stale` / `prune_deleted` / `prune_refused`）。

**问题**：CIPHERTEXT lane 的复制是只追加的游标扫描，删除完全靠热路径双写和
requeue lane 传播。两条都可能漏——`mirror.execute` 按影子期铁律吞掉一切异常
（它必须如此），而热路径删除失败时不会补落 requeue 标记。漏掉的行就永久留在 TEE，
游标只前进、绝不回头。**prod 实测 853 行残留，全部在 `chat_messages`**，13 个用户，
其中 840 行（98%）在 `chat_message_archive` 里能找到对应记录 = 用户主动 clear 过。
只有这张表中招是有道理的：它是唯一有批量删除热路径的表，一次
`mirror.execute_many` 失败就留下几百行。这不是"多了几行无害数据"——**用户以为
删掉的明文对话还留在影子库里**。

**[GOTCHA] 顺序铁律：先查 TEE，再查 RDS。这是正确性的全部，不是性能偏好。**

设 TEE 快照时刻 T1 < RDS 快照时刻 T2。若某行在 T1 已在 TEE，则 replicator 必在
T1 前写入它，而 replicator 只搬 RDS 中存在的行 ⇒ 该行 T1 前就在 RDS ⇒ T2 时它
不在 RDS，只可能是期间被删了 ⇒ 删掉 TEE 侧那行正确。

反序则有真实的误删窗口：某行在 RDS 快照后写入、又在 TEE 快照前被搬进 TEE ⇒
`∈TEE快照` 且 `∉RDS快照` ⇒ 被判成残留删掉。**而游标早已越过它，永远不会搬回来**。
`reconciler` 的 prune 是反序的，那里可以接受（MIRROR lane 每轮重新全表 copy，
误删下轮就补回来）；CIPHERTEXT lane 没有这个后悔药。测试里有一条守卫专门盯它。

**[GOTCHA] prod dry-run 抓到一个本地永远测不出的缺陷：连接被网关掐断会连锁全灭。**
第一版没有重试。真环境 dry-run 的结果是 `chat_messages` 成功（853），**随后 8 张表
全部 `SSL SYSCALL error: EOF detected`**——因为拉 16 万主键要几分钟，期间池里其它
连接一直空闲，被 Phala 网关静默掐断。与 `tee_replicator.worker._flush_batch` 治的是
同一个病（2026-07-14 那次 chat/memory 整表挂），沿用同样的判定与对策：
`OperationalError` 或连接 broken/closed = 换连接重试（有界 + 小退避）。补上之后
9 张表全绿、0 错误。**这类缺陷本地测不出来**——本地 PG 不经网关。

**几处设计取舍：**
- **安全阈值**：单表删除量超过 `max(2000, TEE 行数 × 10%)` 就整表放弃、一行不删。
  防的是"RDS 侧查询异常返回空集"被当成"用户删光了数据"进而清空 TEE。宁可让残留
  多留一天等人看，也不做一次自动的大规模不可逆删除。绝对下限是给小表兜底的
  （`v2_conversation_summary` 只有 6 行，纯按比例算一次正常账号删除就会触发拒绝）。
- **覆盖 9 张表，从 `worker._TABLES` 派生**，不另立清单（本仓库因"手工清单漏登记"
  吃过多次亏）。剩下 3 张纯 append-only、连 `requeue_delete_tee_sql` 都没有的表
  （`chat_message_archive` / `v2_trajectory_events` /
  `v2_conversation_summary_segments`）**故意不接**：它们没有删除语义，prune 无从判断
  "消失"是删除还是尚未复制。未覆盖的表进 `uncovered` 报告字段——静默的覆盖缺口正是
  这套机制要根治的东西，它自己不能再制造一个。
- **加扁平列而不是只进 JSONB**：前一天刚因为 `missing_in_tee` 只活在 JSONB 里
  而让 4 列数据静默失同步一整批部署，不重蹈覆辙。`prune_refused` 非 0 就该有人看。
- 遗留：prune 删掉 frames 的 TEE 明文指针行时不清理对应的 R2 对象，会留孤儿。
  与 requeue lane 的既有行为一致（那条 DELETE 同样不碰 R2），未处理。

---

## 2026-07-30 — 主模型看图验证统一为显式 catalog + 隐藏双图 probe

**[DONE] 视觉能力验证与实际失败归属收口。** 新增公开的
`POST /v1/vision/main/test`：Model API 只信 provider catalog 的显式模态字段，缺字段
才发真实双图探测；resident 走隔离 session 的隐藏双图 side-channel，测试内容不进入
Chat、推送、摘要、Live Activity 或 capture。配置状态统一为
`testing / ok / unsupported / failed / untested`，绑定的 provider、model 或 resident
入口变化会使旧结果失效。每次主模型 setup 成功后也会异步启动同一套探测，不等待
catalog 或双图调用，因此用户不打开视觉设置、不发图也能通过
`GET /v1/vision/config` 提前得到 verdict；期间 route 再次变化时旧结果由版本围栏
丢弃。setup、探测失败和所有探测状态都不阻塞配置或发送：图片始终沿用户配置的主模型
或 dedicated route 进入真实调用。VPS resident 也在官方 consumer 首次上报或更换
`consumer_id / entry_signature / provider / model` binding 时自动入队现有隐藏 probe；
同一 binding 的 pending/终态 verdict 不重复探测，显式 catalog/modalities 已能判定时
不额外调用模型，poll 与发送均不等待 provider I/O。resident 回报的明确图片拒绝
`vision_model_required / vision_model_incompatible` 统一落为 `unsupported`；
auth、quota、rate limit、timeout、upstream 与空回复仍保留 `failed`。只有 provider
真正以明确的 text-only 图片错误拒绝本回合时，才返回 `vision_model_required` 与可操作
的中英双语换模型提示；Runtime V2 同时把该回合捕获的 active route 写为
`unsupported`，让后续 config 查询触发非阻断提示，且用 route 版本 fence 防止旧失败
覆盖用户刚切换的新配置。Hosted V1 同样在发图时捕获 route 版本，并在接收 terminal
failure 回复的原子事务中写回 `unsupported`，避免 allowlist 外 V1 用户长期停在
`untested`。

**[DONE] 图片失败归属只在证据明确时挂到视觉模型。** auth、quota、model、provider、
rate limit、upstream、timeout、reply parse 和 dedicated observer 失败映射到稳定
`vision_model_*` 码；图片回合里的工具、存储或其他未知内部异常保留原失败归属。

公开 OpenAPI 已登记 main-test 端点；resident probe 回传留在
`/v1/internal/vision/main/test/result`，不进入公开契约。

---

## 2026-07-29 — TEE 迁移链补到 0008：写了迁移不等于执行了迁移

**[DONE] `alembic_tee 0008`** 补 `model_api_routes` 落后 RDS 的 4 个 vision 列
（`is_vision` / `vision_test_status` / `last_vision_test_at` /
`last_vision_test_error`，来自 RDS `0066_model_api_vision_route`）。同时把两个
环境的 TEE 库从 **0006 升到 0008**（含别人写好但一直没执行的 `0007`）。

**[GOTCHA] 这次暴露的不是代码缺陷，是流程缺口。** 07-27 那批工作交付后，别人
接手新功能时**正确地**用上了这套机制：登记了 `chat_turn_activity_events`
（SNAPSHOT）、把两张 voice 临时表登记成 SKIP、还写了 `alembic_tee 0007`。但
**没有人执行它**——`tee-migrate` workflow 需要的 4 个 repo secret 至今没建，
`alembic_tee` 目前只能手工跑。后果是 test 的 TEE 库停在 0006，`chat_turn_activity_events`
在 TEE 侧根本不存在，snapshot lane 每个 tick 报一次
`两侧无公共列，拒绝整表清空`（护栏正确拦住了整表清空，没有误删任何数据）。

**两种漂移的可见性差异，值得记住：**

| 漂移 | 表现 | 可见性 |
|---|---|---|
| RDS 新建表、TEE 没有 | `snapshot_failures = 1`，每 tick 一次 | **有红灯** |
| RDS 加列、TEE 没有 | `ok: true`，行数照样对得上 | **只在 `missing_in_tee` 里，无红灯** |

`model_api_routes` 的这 4 列就属于后者——整表一直在同步、行数一直是 27、
`snapshot_failures` 一直是 0，只有这 4 列的数据静静地没进 TEE。加列不建表就撞不上
"无公共列"护栏，在 CI 和失败计数上都是静默的。**`missing_in_tee` 是这类漂移唯一的
信号，必须有人定期看**（查询见 `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` §3）。

**几处刻意不做的：**
- **不搬** `0066` 的 `model_api_routes_one_vision` partial unique index。与 `0004`
  baseline 政策一致（`0004` 同样没搬 `0014` 的 `one_active` / `uniq`）：TEE 是
  SNAPSHOT 整表替换的只读影子，业务唯一约束在这里没有防护价值（RDS 侧已保证），
  却会把任何 RDS 侧的边界数据问题放大成 TEE 侧的整表停止同步。
- **不补** `thinking_fallback`（`0005` 已这么裁决过）：test RDS 的历史残留列，
  全仓 grep 零命中，prod RDS 没有。TEE 不跟着某一个环境长歪，让它一直报着。
- prod TEE 提前有了这 4 列和那张空表，而 prod RDS 还停在 `0063`（那批新迁移没上
  prod）。这是无害的：`missing_in_rds` 只入报告，不触发 warning、不影响 `ok`；
  而 prod 跑的 registry 里还没有 `chat_turn_activity_events` 这一条，SNAPSHOT lane
  根本不会碰那张空表。

**[GOTCHA] 顺带查出、本次未动：`monitoring` 角色对 TEE 库 55 张表全部零权限。**
`pg_default_acl` 里只有 `app` 和 `tee_replicator`，从来没配过 `monitoring`。不是这次
引入的，是既有状态——这个"只读角色"实际上是废的。改角色权限是安全边界动作，
留给单独决策。

---

## 2026-07-29 — Runtime 值班台加上开销：各 lane 的 token 与缓存效率

**[DONE] `/admin/data-track?view=runtime` 的 lane 健康表增加 token 两列**，窗口跟随
页面切换（24h / 7天 / 30天）。此前值班台只回答"跑得好不好"，不回答"花了多少"；而
token 统计只存在于 users 页的「运营 Telemetry」区块，且是**全站 chat lane、固定 30 天**
的单一口径——非 chat lane 的开销完全不可见。心跳 lane 烧闲置用户 BYOK 是出过事的
（`usr_57c24d0d` 零聊天、65 个回合全是 sleep），当前口径恰恰看不到它。

- `jobs_store.recent_token_usage_by_lane(within_hours)`：按 lane 的 `GROUP BY` 聚合。
  三条口径写进了 docstring：**统计全部回合、不过滤 `failed`**（失败回合照样烧钱，
  provider 已经算过钱了——这与同文件 `recent_runtime_health` 的延迟分位数只算成功回合
  正好相反，两者过滤条件相反是刻意的）；**无上报是 `None` 不是 `0`**（靠 `sum()` 的
  NULL 传播，计数列用 `coalesce(...,0)`、token 列裸 `sum`，否则"provider 没回 usage"
  会伪装成"用了 0 个 token"）；**不加 `LIMIT`**（sum 聚合加采样上界会静默少报总量——
  这条决策本身仍然正确；扫描路径当时以为由 `ix_v2_turn_metrics_lane_created_at` 的
  lane 前缀控制，这句话后来被 review 实测证伪，见下面 07-29 review 修复条目）。
- 渲染两列：「token 入/出」`951.2k / 40.5k`、「缓存命中 · 上报」`49% · 87%`。某条 lane
  不在返回的 `lanes` 字典里时（有 job 但一个回合都没终态）两列显 `—`，不 `KeyError`、
  也不显 0——判据一律 `is None` 而非 falsy，否则真实的 `0` token 会被误显成"无数据"。
- 接线：窗口在 `page_html` 里**算一次、传给两个数据函数**，而不是让它们各自读
  `request.args`——后者会让同一页出现一个 24 小时、一个 720 小时的窗口，且几乎看不出来。
  `try/except` 同时覆盖两次数据调用、但不包住渲染调用（渲染层的 bug 不该被误吞成
  "数据源故障"降级页）。

⚠️ **users 页那块保留不动，两处窗口口径不同**（users 固定近 30 天、本页跟随窗口），
页面说明里写明了"两处数字不一致是窗口不同、不是 bug，切到 30 天时应当一致"。

L1 全量 7086 passed / 0 failed（基线 7068 + 新增 18）。无迁移、未改
`recent_token_usage_summary`、未动其余视图页。

### [DONE] 整分支 code review 后的 4 个 Important + 2 个 Minor 修复（同日）

- **索引论证反了（docstring/design §3）**：`ix_v2_turn_metrics_lane_created_at` 是
  `(lane, created_at DESC)`，`lane` 打头恰恰意味着它服务不了本查询（无 lane 等值谓词，
  PG 16 无 skip scan）。本地 PG 16 实测走 Parallel Seq Scan。改写 docstring 说实话，
  「不加 LIMIT」的决策保留，只换掉依据；把补索引记为 design 里的明确 follow-up。
- **渲染层漏掉 token-only 的 lane**：`_render_runtime_health_page` 现在遍历
  `payload["lanes"]` ∪ `tokens["lanes"]` 的并集——一条窗口内有 token 开销、但 job 没
  挤进健康侧 `LIMIT 1000` 采样的 lane，此前不显示也不报错，现在会以健康列全 `—`、
  token 列正常数字的合成行出现。页顶补一句采样上界差异的说明。
- **窗口不一致的"不可能"只是巧合**：`recent_runtime_health` 钳 `24*30`，
  `recent_token_usage_by_lane` 钳 `24*366`，今天恰好都等于 720。加了白名单守卫测试
  `max(_RUNTIME_HEALTH_WINDOWS) <= 24*30`，把巧合钉成会红的约束。
  `_RUNTIME_HEALTH_WINDOWS` 加超过 720 小时的档位时这条测试会先红。
- **`cache_hit_ratio` 与 users 页算法不一致**：只上报一侧（`cache_read=None,
  cache_miss=500` 或反过来）时旧算法显 `0.0%` / `100.0%`（假装完美命中），users 页显
  `—`。已对齐 users 页：任一为 `None` → ratio 为 `None`。Anthropic 只有 cache write
  无 cache read 的回合是真实路径，不是理论构造。
- Minor：`_fmt_tokens_compact` 在 `[999_950, 1e6)` / `[999_950_000, 1e9)` 两个区间
  会显示成上一档的 `"1000.0k"` / `"1000.0M"`（先除后 `.1f` 进位）；真实边界是 999_950，
  不是 999_500。新增 B 档并按格式化结果收紧阈值。
- Minor：`test_render_runtime_health_page_token_columns_are_dash_without_data` 的
  `>= 2` 收紧为按 lane 行精确断言 `== 2`。

无迁移、无索引改动（记为 follow-up）。design/plan 两份文档同步更正，plan 文档保留原始
代码片段、在末尾追加 Post-review 修正记录不改写历史。

L1 全量 7092 passed / 0 failed（基线 7086 + 本轮新增 6：cache_hit_ratio 部分上报两个方向、
窗口白名单守卫、`_fmt_tokens_compact` 真实进位边界、token-only lane 出现在渲染结果里、
token-only lane 不参与健康结论判定）。1 skipped / 9 xfailed 与基线一致，非本轮引入。

## 2026-07-28 — TEE SNAPSHOT lane 上真环境后炸出的两个坑：TRUNCATE 权限 + 列漂移

**[DONE] Task 10（计划外追加）+ Task 8 收尾**。全量对齐的代码合进 `test` 部署后，
SNAPSHOT lane 一行都没同步成功。两个根因**本地测试库结构上都覆盖不到**，记在这里
是因为它们会重复发生在任何"本地绿了就以为能上"的改动上。

**坑一：`TRUNCATE` 是 PostgreSQL 里独立于 DML 四件套的权限。**
`deploy/postgres/ensure-roles.sh` 给 `app` / `tee_replicator` 授的是
`SELECT, INSERT, UPDATE, DELETE`，而 SNAPSHOT lane 的整表原子替换是
`TRUNCATE + COPY` → 27 张表全数 `permission denied for table X`。
本地 pytest 跑的是 `postgres` 超级用户，**这类角色权限缺口在本地永远绿**。
排查时极易误判：`has_table_privilege(role, tbl, 'INSERT')` 查出来 54/54 全绿，
只有 `TRUNCATE` 那一项是 0，必须逐权限查才看得见。
修法两层：`ensure-roles.sh` 补上（长期，PG CVM 重部署带着走）+ 直连两个 TEE 实库
执行 GRANT（脚本只在 CVM 启动时跑，既有库等不到）。

**坑二：列漂移会让整张表永久失败。**
`snapshot_table` 原先用裸表名 `COPY (FORMAT BINARY)`——按列位置严格匹配整表，
两侧列集差一列就 `row field count is N, expected M`，且**每个 tick 都失败**。
两种来源都是常态而非异常：①滚动部署时间窗（`0059`/`0060`/`0061` 给
`v2_turn_metrics` / `v2_wake_schedule` 加的 10 列，`0004` 的 DDL 是 07-27 从 prod
派生的，test RDS 跑在前面）；②环境自身的历史残留（test RDS 的
`model_api_routes.thinking_fallback` 全仓 grep 零命中，没有任何代码创建它——TEE
侧反而是与代码一致的那个）。
修法：`COPY` 两侧改用列集**交集**，两侧独有的列分别报进 `missing_in_tee` /
`missing_in_rds`（落进 `tee_sync_runs.report`），`missing_in_tee` 非空时
`log.warning`。外加 `alembic_tee 0005` 补齐真实缺的 10 列——但**不补**
`thinking_fallback`：TEE 不该跟着某个环境长歪，让它一直报着才对。
护栏：两侧无公共列时拒绝执行。没有它，交集逻辑会把"完全对不上"降级成"共同列为空
的正常快照"——`TRUNCATE` 照跑、一行写不回、报告仍是 `ok=True`，比原先的整表失败
更糟（原先只是不同步，那样把已有影子数据也抹了）。

test 实测曲线：`snapshot_failures` 27（部署后）→ 3（补授权）→ 1（落 0005）→ 0
（部署 Task 10）。prod 解密探针 **2921/2921 = 100% PASS**（28 秒，零毒行）。

**这一轮补上了原始诉求里漏掉的一层**：表注册表守的是"表存在"，守不住"列一致"。
而列漂移在有滚动部署的系统里是常态。现在漂移是可观测的量，不是某张表悄悄停摆。
排查配方见 `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` §3。

---

## 2026-07-28 — TEE 影子库全量对齐：51 张表归类落地 + verify/registry 双守卫

**[DONE] `2026-07-27-tee-full-table-alignment` 计划全部代码 Task（1-8）完成**。
起点：2026-07-27 实测 RDS 61 张基表里只有 20 张镜进了 TEE 影子库，且这套三层
手工白名单（`db.py` 双写点 / `tee_replicator.worker._TABLES` / `tee_shadow.
reconciler.TABLES`）互不校验，谁都不是全集——Runtime V2 的 19 张新表就是这样
在无人发现的情况下漏掉的。

- **新增 `backend/tee_shadow/table_registry.py` 作为单一真源**：61 张 RDS 表
  逐条登记 lane（`MIRROR` 13 / `CIPHERTEXT` 11 / `SNAPSHOT` 27 / `SKIP` 10 /
  `LOGICAL` 0，预留给尚未开通的 PG 原生逻辑复制），51 张非 SKIP 表全部进 TEE。
  `tests/test_tee_table_registry.py` 守完备性：RDS 迁移链建出的每一张表必须
  有且只有一条登记（漏登记直接红），且非 SKIP 登记必须能在 `alembic_tee` 的
  真实库里找到对应表（专治"revision 合并了但从未在实库执行"）。
- **新增 SNAPSHOT lane**（`tee_shadow/snapshot.py`）：给"明文、数据量小、但有
  UPDATE/DELETE"的 27 张表（`agent_action_queue`/`v2_turn_metrics`/`v2_runtime_
  control` 等）用 `TRUNCATE`+`COPY` 整表原子替换，不必像 CIPHERTEXT 那样为每
  张可变表单独接 requeue 补偿。`agent_runtime_instances`/`agent_runtime_
  supervisor_heartbeats` 最初按"双写热路径"标了 MIRROR，实测两张表情况不同但
  结论一致——都改判 SNAPSHOT：`agent_runtime_instances` 的 8 处租约热路径写点
  （`agent_runtime/leases.py`）确实只写 RDS（文件顶部注明有意不镜像，ephemeral
  TTL 锁）；`agent_runtime_supervisor_heartbeats` 其实**有** `db.py` 的 mirror
  双写（upsert/prune 两个写点都调了 `mirror.execute`，原样保留不动），但这条
  镜像没有 `reconciler.TABLES` 兜底、漂了没人拉回来，且心跳每次都改
  `updated_at`，接 reconciler 会在 strict 逐列比对上变成永久红——SNAPSHOT 每
  tick 整表替换天然收敛，不需要这些。0004 迁移已建表、prod 2026-07-28 复测
  230+1 行且 0 孤儿（0004 迁移里写的 220 行是 07-27 写迁移时的旧值），FK 前提
  满足。
- **`tee_replicator.worker._TABLES` 接入 7 张新密文表**（`chat_message_
  archive`/`model_api_credentials`/`v2_conversation_summary(+_segments)`/
  `v2_trajectory_events`/`v2_trajectory_reviews`/`v2_workspace_entries`）。
  test 环境 CVM 容器内探针 gate：7 张表 100% PASS（0 pending_device / 0
  permanent_fail / 0 异常），`v2_trajectory_events` 实测 124/124（`enclave_pk_
  fpr` 全空仍全部解得开）；`chat_message_archive`/`v2_trajectory_reviews` 当时
  test 环境 0 行，未被真实数据覆盖，接线按合成行验证补了这个盲区。冷启动有一个
  Critical 修复：`_decode_cursor` 对从未跑过的新表返回空串水位线，遇到排序列是
  TIMESTAMPTZ/BIGINT/UUID 的表（而非既有表的 TEXT 排序列）直接 cast 失败、
  scheduler 逐表 try/except 静默吞掉、每 tick 无声重复失败——修法是给 `_Table`
  加 `cursor_zero` 字段，逐表配出各自类型能 parse 的下界。
- **`tee_shadow/verify.py` 覆盖范围从 18 张扩到全部 51 张（新增 `covered_
  tables()`）**：SNAPSHOT lane 只核行数（整表替换对增量抽样没有信息量）；7 张
  新密文表因信封列名各不相同（`payload_envelope`/`api_key_envelope`/
  `summary_envelope`/`review_envelope`/`content_envelope`，`_sample_ciphertext_
  content` 写死读 `doc`）暂只做行数核算，内容抽样参数化列为独立后续工作。新增
  `_rows_ok_advisory`：SNAPSHOT/新密文表是"tick 级快照"或"游标增量"，天生会与
  持续在写的 RDS 有瞬时行数差，不能像老表那样要求 `rds==tee` 精确相等（会让
  gate 永久红、没人再看）——判据收窄成"RDS 有行而 TEE 一行都没有"，真正抓的是
  "这张表从没同步成功过"。`tests/test_tee_verify.py::test_verify_covers_every_
  synced_table` 守住"注册表新增表 = verify 范围自动跟上"，否则 `verify_ok=true`
  但新表压根没被核对会是一次"全绿假象"。
- **补上 alembic_tee 的迁移落地通道**（`.github/workflows/tee-migrate.yml`）：
  之前完全靠人工执行，已合并的 0002/0003 在 2026-07-27 之前从未在 test/prod
  实库跑过（两库停在 0001）。新通道手动触发、typo guard、owner 角色 direct-TLS
  连接、落地后强制断言 `alembic_tee_version == 代码 head`。2026-07-27 已用它
  （加一次本地手动执行）把两个实库从 0001 推到 0004 head，各 54 张表。
- prod 解密探针（`scripts/tee/decrypt_probe.py --dsn PROD ...`）与 prod 部署
  验收（`snapshot_failures=0`/`verify_ok=t`/`unconverged_tables=0`）按分工由
  controller 在本次之外单独执行，本条记录只覆盖代码 Task 的落地范围。

文档：本文件 + `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md`（新增 §2.9 迁移落地
通道）+ 上游 `docs/superpowers/plans/2026-07-23-tee-promotion-decrypt-removal.md`
（Task 0.6 两个 checkbox 勾掉，Task 1.5 v2 表迁移策略处标注由本计划完成）。
公开文档（`docs-site/`）与 OpenAPI 不涉及：不碰公开 API 契约、不改架构对外
叙事、不改信任边界（TEE 库定位与可见性没变）。

## 2026-07-28 — Admin Runtime 健康值班台：收尾修复最终 code review 的三个 Important 项

**[DONE] `/admin/data-track?view=runtime` 值班台过审前的三条修复 + 一处文档更正**。这是
该功能五个任务全部实现（9 commit）之后、最终 review 判「Ready to merge: With fixes」的
收尾工作。前三条缺陷原样存在于设计与实施计划文档里（实现是忠实照抄的），因此代码与文档
一并改：

- **I-1（页顶结论永远不会真正变红）**：`_render_runtime_health_page` 里
  `level_cls = {"ok": "ok", "warn": "warn", "bad": "warn"}[level]` 把 `bad` 映射成
  CSS class `"warn"`——100% 失败率与 6% 失败率在页顶显示成同一个橙色，人眼分诊能力在
  第一眼就被抹掉，而 per-lane 表反而认真做了三档。改成 `"bad": "bad"`。
- **I-2（失败码白名单太窄，非 chat lane 塌成 `other`）**：`_runtime_failure_code` 原来只
  放行精确匹配 `queue_timeout`/`lease_timeout` 和无条件的 `turn_failed:` 前缀（不校验
  冒号后内容形状）。但 `mark_failed`/`mark_expired` 落库的真实码还有 `wake_failed:*`
  （heartbeat/proactive lane）、`extraction_failed:*`、`compaction_failed:*`、
  `mcp_mutation_outcome_unknown`、`runtime_expired`——旧白名单下这些码在 chat 之外每条
  lane 都塌成 `other`，而 heartbeat 恰恰是本页专门加了「（日报口径）」链接、明确要给人
  看的 lane。改为按形状放行（`^[a-z0-9_]+(:[a-z0-9_]+)?$`，admin 层自定义常量，不
  import `model_api_runtime`），同时渲染层清洗后按 `(lane, code)` 重新合并计数——否则
  同一 lane 的两个不同原始码清洗成同一个桶会渲染成两行都叫 `other`。
- **I-3（claimed/running 卡死时页面明文说「这不是故障」）**：`_runtime_health_level` 对
  job 全部卡在 `claimed`/`running`（无 pending、worker 心跳还活着）的 lane 全部指标都
  跳过，误判 `("ok", [])`；`empty_note` 只看「所有 lane sampled_jobs 都为 0」，把「真的
  没数据」和「卡死」混为一谈。reviewer 实证过这个具体形状（`inflight=57/capacity=8`）：
  页面显示「这不是故障」+ 总体结论「正常」。修正：`sampled_jobs==0 and capture.open>0`
  → 至少 `warn`；`pool.inflight > pool.capacity` → `bad`（矛盾态）；`empty_note` 只在
  终态样本、`capture.open`、`pool.inflight`、`pool.pending` 全为 0 时才说「这不是
  故障」，否则改为「窗口内无终态 job，但有 N 个回合在飞——可能是卡死」。
- **I-4（文档修正，不改代码）**：设计文档 §8 原先写「索引够用」，实查
  `agent_jobs.finished_at` 无任何索引、`agent_jobs` 无保留策略、新增的三条 CTE
  （outcome/failure/capture）在全 lane 范围都拿不到有效索引，`LIMIT 1000` 只截结果不
  减扫描。本分支承诺不含迁移，故不加索引，只把错误结论改正并把
  `CREATE INDEX CONCURRENTLY ix_agent_jobs_finished_at ...` + `agent_jobs` 保留策略
  记为设计文档 §10 的明确 follow-up（含「上线前在 prod 规模跑一次 EXPLAIN」）。
- 死代码清理：`tests/test_data_track_runtime_view.py` 里未使用的 `import base64`、
  `import db`、`_route_pk_counter = itertools.count(9_000)`（连带其 `itertools` 导入）
  一并删除。

改动范围：`backend/admin/data_track.py`（`_runtime_failure_code` / `_runtime_health_level`
/ `_render_runtime_health_page`）、`tests/test_data_track_runtime_view.py`（8 条新用例覆盖
上述三个 Important 项 + 死代码清理）、设计文档 §5/§6/§8/§10、实施计划里对应的三处代码块
（均加了「最终 code review 修正」批注，保留原文作历史记录）。不涉及迁移、不改
`recent_runtime_health()` 的 SQL 口径、不动既有 6 个视图页。
## 2026-07-27 — 摘要折叠死锁：一个 V2 用户被自己的一批消息卡了三天

**[FIX] 折叠被拒的原因不再是黑的；`prompt_coverage_incomplete` 从
`responder_error` 桶里单列出来**。

`usr_7f30…`（prod，3146 条历史）自 07-24 17:31 起在 V2 下**每一个 turn 都失败**，
14/14，零成功。取证链：

- 摘要水位 `v2_conversation_summary.watermark_seq` 冻结在 682015，其后 1946 条
  未摘要；同期其他 V2 用户水位都在正常推进。
- 一个 turn 内（job 9）**连折 6 批成功**（段 3/4/6/7/8/9，各 200 条，每批
  14–22 秒），第 7 批开始三连败，每次只要 2.5–4 秒。三次 `provider_request` 的
  `payload_bytes` **逐字相同**——同一批消息被反复发送。
- 每次调用的 completion tokens 呈双峰：26–53（一两句话）或 500（打满被截断）。
  26–53 tokens 不可能超行数/超长，所以否决只能来自格式类规则。
- **规模不是原因**：卡住那批的可折叠内容（body_ct 合计 21,804 B / 200 条）与
  成功折过的段 3（21,292 B / 199 条）相差 2%。两批都含 R2 外置大文件，成功那批
  的还更大（4.73 MB vs 2.78 MB）——外置正文是懒加载的，从没进过 prompt。

机制：`compaction._validated_new_bullets` 是**全有或全无**校验，任一行违规整批
返回 `None` → 折叠 no-op → 水位不推进（这一步是对的，宁可卡住也不能假装覆盖）→
下一轮读到**逐字相同**的同一批 → 同样被否。失败的后果恰好保证了下次输入不变，
**没有任何自愈路径**。`_ensure_prompt_coverage` 耗尽 3 次无进展预算后抛
`TurnError("prompt_coverage_incomplete")`，又被 `_safe_failure_code` 折进
`responder_error` —— 一个和另外 11 个抛点共用的桶。用户侧表现为完全静默：turn
从未走到回答用户的那次模型调用。

本次改动（**只加可观测性，不改判据**）：

- `compaction`：拆出 `_validate_new_bullets`，返回 `(rendered, reject_code)`。
  reject_code 只含规则名与计数（`duplicate_within_batch:3`、
  `line_count_over_budget:40`、`line_not_bullet:0` …），**绝不含任何被摘要的内容**
  ——它要流到 worker 的明文状态面。`compact`/`compact_segment`/`compact_checkpoint`
  新增可选 `reject_out` 回调；回调抛错不会影响折叠结果。
- `worker`：catch-up 记录最后一次否决码，耗尽重试时抛
  `TurnError("prompt_coverage_incomplete:<reject_code>")`；`_safe_failure_code`
  为该码单列一档。于是**不解密、不读代码**，只看明文 `agent_jobs.last_error` /
  `v2_turn_metrics.status` 就能知道是哪条规则卡住了谁。no-op 分支另记一条
  `compaction_rejected` trajectory 事件（含批次大小与已重试次数）。
- `deploy/docker-compose.phala.yaml`（serve-worker）：把
  `FEEDLING_V2_COMPACTION_BATCH_MSGS` / `_BATCH_CHARS` /
  `FEEDLING_V2_PROMPT_CATCHUP_DEADLINE_SEC` / `FEEDLING_V2_TAIL_BUDGET_MSGS`
  以 `${VAR:-码内默认}` 形式接出来（值即当前默认，**不是行为变更**），这样下次
  卡住可以用加密 env 缩批解卡，不必再发一次版；另接出
  `FEEDLING_V2_TRAJECTORY_INSPECT_ENABLED`（默认 `0`）。

⚠️ **`responder_error` 的统计口径变了**：以前所有 coverage 停滞都记在
`responder_error` 名下，现在改记 `prompt_coverage_incomplete[:code]`。按该字段
分组的看板需要同步。

**[FIX] 死锁本身：缩批 → 隔离 → 越过（quarantine-and-advance）**。参数怎么调都
绕不开「全有全无 + 输入确定性」这个自锁结构，出路只能是给它一条出路。两级升级：

1. **先缩批**。连续 `max_retries` 次无进展后，批次上限除以 4（200→50→12→3→1），
   每个尺寸拿到自己的重试预算（它是一个真正不同的请求，不是失败请求的重复）。
   多数拒绝是**批次**的属性而非某一条消息的属性——行数超预算、两条消息归纳出同一
   条 bullet——换个尺寸就不再发生。健康路径完全不变（第一次就成功时永不缩批）。
2. **缩到 1 条仍被拒 → 隔离那一条**。写一个确定性的替身段（`coverage_kind` 仍是
   `exact`，seq 范围正好是它替换的那一行，所以 `validate_canonical_frontier` 的
   三个见证依旧成立），文本明说「此处有 1 条消息未能被自动摘要，其内容不在本摘要
   覆盖范围内，原文仍完整保留在聊天记录中」。**一次只越过一行，绝不牺牲整批**——
   为解锁一行而丢掉 199 行完全折得动的消息是不可接受的。

隔离**损失的是摘要覆盖，不是数据**：`chat_messages` 是不可变原文表，一行都没动，
历史照常可读，将来更聪明的压缩器可以回头重折。每次隔离都打一行明文日志
（`[v2-compaction] quarantined unfoldable row user=… seq=… reject=…`）并记一条
`compaction_quarantined` trajectory 事件。开关
`FEEDLING_V2_COMPACTION_QUARANTINE_ENABLED`（默认开），置 `0` 恢复严格 fail-closed。

**另一条同样致命、但走不同代码路径的形状也一并修了**：`_bounded_compaction_prefix`
遇到「首行单条就超过整批字符预算」时抛 `ValueError`（它拒绝跳过超大首行，因为跳过
会让 seq 覆盖不诚实）——这条路径**根本到不了 provider**，所以缩批对它毫无作用。
`usr_7f30…` 真正的墙就是这个：seq 682632 是一条 R2 外置、hydrate 后 798,262 字符
的消息，卡在积压队头。现在这条路径直接走隔离。**如果只修了折叠被拒那一半，这个
用户部署后仍然会卡在原地。**

**[FIX] V2 的 provider 尝试也写明文台账**。V1 的常驻 consumer 一直往
`user_logs.provider_attempts` 写一份纯元数据台账（outcome / usage / trigger），
所以「这个用户的中转到底通不通」是一条 SQL。**V2 把同样的事实只记在加密
trajectory 里**，于是同一个问题在 V2 侧要走 break-glass 解密。

这个缺口在 usr_90184ac4 上直接变成了误诊：他 14/14 `providererror`，看起来像
「中转扛不住 V2」。查了 V1 台账才发现——**同一套凭证、同一个中转，V1 侧 902 次
成功、峰值 70,926 input tokens、最近一次成功就在今天**，而 V2 单次调用才约 43k。
中转没坏，prompt 也不比 V1 大，差异在我们发出去的请求形状。**这个结论完全来自
V1 那份明文台账**；如果 V2 也有，第一分钟就能得出。

`provider_attempt_ledger.record_runtime_attempt()`：同一个 stream、同一套字段名，
额外带 `runtime`/`lane`/`provider`/`model`/`error_class`（V1 从 consumer 自己的
配置拿，V2 得自己声明）。写入点是 trajectory sink 的一层旁路（`_record_trajectory`
+ 前台 turn 的 `_ledger_tapped_sink`，两条 lane 都覆盖），只取元数据——outcome、
token 数、provider/model、错误**类名**，不含 prompt、回复、上游原始错误文本。

⚠️ 两个刻意的防御，都是被测试打出来的：telemetry 失败一律吞掉（写台账不能让一个
本该成功的 turn 失败），且对 trajectory sink 的形状保持宽容——没有 `user_id` 的
窄 recorder 就不写台账、不抛异常（前台 turn 也走这条包装，一个 AttributeError
会炸掉它本该解释的那些 turn，wake lane 的测试替身当场证明了这一点）。

回归：`4 failed / 6741 passed`（干净 HEAD 基线）→ `3 failed / 6770 passed`，失败
集合是基线的子集（`e2b_template` 那条本身是 flaky，这次自己绿了）。新增测试覆盖：
缩批优先于牺牲覆盖、单行隔离后用户解锁、一次只推进一行、隔离文本确定性且无内容、
超大单行被隔离而非永久卡死、关掉开关后两条路径都仍然 fail-closed，以及台账镜像的
四条（成功/失败字段映射、非 provider 事件不记、原始错误体不进台账、写入失败不影响
turn）。

**[DECISION] 折叠批次 200→50、tail 预算 20→50（2026-07-28）**。解密 usr_90184 的
`provider_error` 后确认：他和 usr_7f30 挂在**同一条 lane（prompt_catchup）**，是同
一个「批次过大」的两种表现——usr_7f30 的 200 条渲染不成合规 bullet（折叠被拒 →
死锁），usr_90184 的 200 条在 compaction 的 `timeout=60.0` 内答不完
（`duration_ms=60340`、`error_class=transient`、`status=null`，即根本没收到 HTTP
响应，不是中转拒绝）。四分之一大小的请求两头都缓解。

tail 预算 20→50 是更上游的一刀：**它决定一个 turn 是否进入折叠路径**。积压不超过它
就整批留在逐字 tail 里，一次折叠都不做。20 太低，几乎每个活跃用户每回合都在折叠；
而全程健康的那两个 prod 用户（21/21、13/15）恰恰是因为历史从没到过这个线。

⚠️ 权衡：`plan_provider_round` 里**对话消息是 required 组件，超预算不会被裁而是直接
`prompt_frontier_exhausted`**（只有 tool schema 是 optional）。多带 30 条逐字消息约
数千 token；prod 健康用户 prompt 峰值 22,768 / 15,554，对 120k 可用预算安全。唯一
的 frontier_exhausted 记录（usr_5adeef，07-24 17:42）发生在 unaudited default 还是
32768 的时期，07-25 12:31 已改为 131072。若将来有用户自配小 context_window 或中转
真实窗口偏小，这两个值仍是首先要回调的旋钮（都是 `${VAR:-}` 形式，改 env 即可）。

**[FIX] 这些旋钮的 `-e` 接线补齐（2026-07-28）**。之前只把它们写进了 compose，
CI 的 `phala deploy` 没传值，等于**只能用默认值**——"改 env 就能救急"这句话当时并
不成立。现在三个环境的 5 个旋钮（batch msgs/chars、catchup deadline、tail budget、
quarantine 开关）都接了 `-e` + `vars.<ENV>_<KNOB>`。刻意不写 `|| 默认值`：repo var
未设时必须传空，让 compose 的 `${VAR:-默认}` 生效；设了才覆盖。这也是确定性复现
隔离路径的前提——把 `COMPACTION_BATCH_CHARS` 临时调到几百，任何一条消息都能触发
超预算隔离，不必去凑一个 R2 大文件。

**[ADD] `tools/v2_user_triage.py`** —— 把这次的排查路径固化成一条命令（只读、不
解密）：runtime / jobs / metrics / summary / backlog head / trajectory / provider
ledger / peers。四个判据是事故的直接产物：水位停滞时长、队头 R2 指针是否超批次
预算、重试间 `payload_bytes` 是否逐字相同、V1↔V2 provider 结果对照。⚠️ 告警判据
改过两版才不误报：只看「停滞时长」会把 21/21 全绿但一天没说话的用户标黄；加上
「有积压 + 有失败」仍会误报 usr_90184（他的 frontier 是健康的）。最终判据是
**停滞 + 有积压 + 失败是 coverage 形状的**，三者缺一不可，并对「有失败但 frontier
健康」主动提示去看 provider。

## 2026-07-27 — 退役 `responses_unsupported`：一条对着 Kimi 用户空放了很久的警告

**[DECISION] 删除 `/responses` 探测与 `responses_unsupported` 警告**。用户配
Kimi/Moonshot（`openai_compatible`）时，setup 会返回一条 warning：「你选的中转
不支持 Responses 协议，AI 的记忆和工具调用可能不稳定，建议换一个中转」。这条
警告是误报，代码取证与真机实测两头闭合：

- 它的因由（`setup_core._emit_responses_support_notice` 的注释）是「中转不实现
  `/v1/responses` → LiteLLM 强制 responses→chat-completions 桥接 → mangle codex
  工具循环」。**三个前提全部失效**：① LiteLLM 网关早已退役（`db.py` 自己写着
  「pi 走 openai-completions wire，不经 LiteLLM 网关」）；② `openai_compatible`
  派生的 driver label 是 `pi` 而非 `codex`（`hosted/agent_runtime_cutover.py`），
  而 warning 的触发条件恰恰限死在 `openai_compatible`——只在**永远用不到 codex
  的那类 provider 上**报警；③ Runtime V2 全程 `chat_completion_async`，
  `provider_client` 里 `/responses` 的唯一入口条件是 `provider == "openai"`。
- `supports_responses` 在整个 backend **没有任何行为消费点**（`consumer_env` 也
  不读它），唯一下游就是这条 warning。
- test 环境实测四个临时账号（`kimi-k2.5` + `https://api.moonshot.cn/v1`）：V1(pi)
  路径 3 轮、V2 路径 1 轮，记忆写入 4/4、下一轮回读 4/4、错误气泡 0；V2 的
  `v2_trajectory_events` 记到 `tool_call_started` / `tool_call_result` /
  `tool_batch_planned` / `tool_batch_result` **各 3 次**，`turn_terminal` 2 次。
  即：警告声称会不稳的两件事，在它自己声称会出问题的路径上都是好的。

改动：删 `provider_client.probe_responses_support`（省掉每次 openai_compatible
setup 一次最多 20s 的网络往返）、删 `setup_core._emit_responses_support_notice`
与两处 probe 调用、setup 响应不再有 `warnings` 字段、`notices/catalog` 移除该
error class。`supports_responses` **列保留**并钉死 false——V1 roster payload 仍
带着它，删列要迁移而收益为零。

存量已 emit 的通知走**读侧过滤**下架（`catalog.RETIRED_ERROR_CLASSES` +
`notices.core.list_notices` 跳过），不做 SQL 回填：`user_logs` 有 TEE 影子库
镜像，回填只改主库会让两边分叉。

文档：`docs/FRONTEND_ERROR_CONTRACT.md` 删该行；`docs/testing/TESTING.md` §6 与
`RELEASE_TESTING_PROTOCOL.md` 第 5 格改口径——**「能回话 ≠ 记忆/工具能用」这条
测试纪律保留且加强，但没有任何配置字段能替代跑一轮真回合**（旧文写的「验一家新
中转必须读 `warnings[]` 和 `supports_responses`」是错的指引）。公开文档与
OpenAPI 不涉及（零命中）。

同期确认的一条独立事实（与本条无关，仍成立）：Moonshot 的 key 有**区域锁**，
`.cn` 签发的 key 打 `api.moonshot.ai` 直接 `401 Invalid Authentication`；用户报
`provider_test_failed`/401 时先核 `base_url` 与 key 签发区是否配对。

## 2026-07-26

### [DONE] V1/V2 聊天执行记录统一为可信投影
- V2 provider-native 工具调度在调用开始和真实结果/异常边界写入 display-safe
  `agent_status_events`；只保留受限的工具名、call/job/effect id、状态、耗时和结果
  分类，不保存参数、结果正文、助手文案或推理。
- V1 resident 的 `io_cli` 也在真实命令开始/结束边界写同一份按 turn 持久化的
  fixed-shape 事件；记忆分类只从 `memory-index` / `memory-fetch` 实际返回项统计，
  自定义分类时只给总数。
- 用户鉴权的 `GET /v1/chat/turn-activity/{turn_id}` 现在同时读取 V1/V2；新增
  resident 专用写入口，只接受已存在 user turn 下的固定字段，V2 所有权会拒绝该写入。
- 最终回复把同一份受限工具事件附在 `activity_events` metadata；provider-native
  reasoning 继续走独立加密 thinking envelope，二者不混合。
- `memory_search` / `memory_fetch` 在 capability 真实结果还未截断时提取返回总数；
  只有每一项都命中固定双语通用桶时才附完整分类计数，任一自定义/未知桶则只保留总数。
  记忆摘要、正文、搜索词和原始桶名都不进入活动记录。

### [DONE] V1 resident 补齐可下载文件

### [DONE] Claude / Codex / Pi 共用文件生成与原子回复
- V1 CLI resident 新增 `io_cli send-file`：模型把 UTF-8 源文件写进每用户隔离的
  `outbound-files`，Word/PDF 在投递时渲染成真实 `.docx` / `.pdf`。
- 明确格式请求沿用 V2 的完成保护：错误后缀会被拒绝，首轮漏发文件会再补一次，仍
  未生成时保留模型正文，不再把整轮替换成固定失败话术；未指定格式的请求只走语义
  prompt，不参与硬性终态阻塞。
- `/v1/chat/response` 新增加密 `file_followups`；文字主回复、文件 Card 与父消息 CAS
  在同一个 PostgreSQL 事务提交，保证文字在上、Card 在下，且不会出现半成功。
- 同时覆盖 VPS/self-hosted 与 API-key hosted resident；未推送、未建 PR、未合并。

## 2026-07-25 — TEE Redis：三台 CVM 开通 + 砍掉离线备份

三套 Redis CVM（test/pre/prod）全部开通、running、冒烟 ALL GREEN，落在
prod9 node 18，仍**零业务流量**。cvm-id 已写进 `deploy/*-redis-cvm-id.txt`。

**[DECISION] 移除全部离线备份链**（age 加密 + R2 推送 + restore 演练 +
backup sidecar + 备份监控维度）。理由：Redis 在本架构里是**纯临时层**——
缓存、队列/唤醒总线、分布式锁三类用途的数据全部可从 Postgres 重建
（**PG 是权威源**）；且恢复旧快照对锁/队列**有害**（复活已释放的锁、已消费
的队列项），restore 本身就是 bug。保留 CVM 内 **AOF** 仅用于软重启不掉数据；
整卷丢失时让 Redis 空启动、从 PG 回暖。这同时消掉了 07-25 部署中一个无法
定位的 age 解密悖论（recipient 逐字节一致却解不开 CVM 推的快照）——备份既然
不该存在，悖论自然作废。

删除：`deploy/redis/{backup-push,backup-loop,restore,e2e-drill}.sh`、
`Dockerfile.backup`、`docker-compose.e2e.yaml`、`tests/test_redis_backup_scripts.py`。
精简：compose 去 backup 服务、entrypoint 去备份 fail-closed、`redis-deploy.yml`
去备份镜像/age/R2 注入、`redis-monitor.yml` 去 R2 快照新鲜度（只剩持久化+内存）。

部署中固化的坑（写进 `deploy/DEPLOYMENTS.md`）：`--node-id 18`（gateway 只在
prod9 node 18 配好）、`docker build --platform linux/amd64`、GHCR 包必须 public
且有分钟级传播延迟、dstack 首建停在 stopped 要手动 start、gateway 注册 ~30min
延迟、客户端连 gateway 必须发 `--sni`（否则健康 Redis 恒报 eof）。

## 2026-07-25

### [DONE] Runtime V2 补齐推送能力（此前 V2 用户零推送）

- **此前状态**：`model_api_runtime/v2/` 全目录零处调用 push service —— V2 serve-worker
  跑完一个回合、明文回复落库之后，没有任何代码路径去发 APNs。pre 环境切到 V2 的
  用户（含主动唤醒消息）从未成功投递过一条聊天推送；用户只有打开 app 手动拉取
  才能看到新回复。V1（`chat_core.response` / consumer）的推送链路完好，问题只在
  V2 这一侧缺投递入口。
- **改动**：分 6 步补齐，投递判定与决策链**复用 V1 同一套**，不新造平行逻辑：
  1. `tools/export_public_openapi.py` 新增排除前缀 `/v1/internal`（连同
     `EXCLUDED_PREFIXES` 断言测试）—— 为下一步的内部端点预留不进公开契约的位置。
  2. `backend/push/routes_asgi.py` 新增 `POST /v1/internal/push/ai_reply`，鉴权走
     `require_scope("chat_push")`（runtime-token，非用户 session）；
     `backend/push/push_core.ai_reply_push()` 是承接点：`is_wake=True`（agent 主动
     唤醒）时先过 `proactive/controls_v2.evaluate_delivery_v2`（`reminders_delivery`
     开关），被挡则把 `push_decision`/`alert_status`/`live_activity_status` 等字段
     标 `suppressed` 写回 `chat_messages.doc` 后直接返回；放行或非 wake（用户消息
     的应答）则调 `push.service._deliver_ai_message_push_if_background`——与 V1 走
     同一条投递函数，结果字段同样回写进消息行的 delivery metadata。
  3. `chat_messages.doc` 新增 `wake_kind`（`heartbeat`/`scheduled`/`manual_wake`/
     `screen_watch`）标记 agent 主动发起的唤醒消息，与用户消息的应答区分；chat
     lane 的回复不带这个键。落库前要在 `UserStore._build_chat_message` 的 extra
     白名单里显式登记，否则会被该方法的 allowlist 静默丢弃（本次改动过程中发现
     并修的一个隐藏坑）。
  4/5. `model_api_runtime/v2/worker.py` 的 wake lane（独立函数 `_run_wake`）与
     chat lane（`process_job`）各自在回合末尾维护一个覆盖式、仅内存的 `push_slot`
     （240 字符截断的回复正文），
     `finally` 块里最多触发一次 `deps.send_reply_push(...)`；构建槽位和发送推送都
     包在 try/except 里，任何异常只吞掉记日志、绝不影响回合本身的完成/失败结果。
     一个回合可能吐多条气泡，只有最后一条会成为推送（用户点进 app 能看到全部气泡，
     推送只是唤醒手段）。
  6. `model_api_runtime/v2/serve_worker.py::build_production_deps` 接上生产传输：
     `_send_reply_push` 用短 TTL（60s）、独立窄 scope（`chat_push`，特意不并入
     `_RUNTIME_TOKEN_SCOPE`）铸一个 runtime token，经 compose 内网 `httpx.post` 到
     上面的 `/v1/internal/push/ai_reply`——因为 APNs 私钥只注入 backend 容器，
     serve-worker 没有钥去直接发推送，只能把已落库回复的明文正文经内存传过去
     （不写库、不进日志正文），姿态与 V1 consumer 走 HTTP 传 `push_body` 一致。
- **回滚拉杆**：`FEEDLING_V2_PUSH_ENABLED`（默认开，置 `0` 时
  `build_production_deps` 把 `send_reply_push` 传 `None`，两条 lane 的 finally 判空
  直接跳过，不改变回合逻辑）。
- **`/v1/internal` 不进公开契约**：runtime-token scope 鉴权、只被同 compose 网络内
  的 serve-worker 调用，不是产品 API；`tools/export_public_openapi.py` 已排除，
  本次未改 `docs-site/openapi/public.json`。
- **测试覆盖**：`tests/test_v2_push_endpoint.py`（内部端点+`ai_reply_push` 决策分支）、
  `tests/test_v2_push_delivery.py`（`wake_kind` 落库）、`tests/test_v2_serve_worker.py`
  （`_send_reply_push` 传输层）、`tests/test_v2_atomic_reply_cursor.py`（两条 lane 的
  push_slot 接线，含 `test_push_transport_failure_does_not_fail_the_turn`——这条测试
  真的走 provider 出结果 → `_on_reply` 写库 → 收尾推送一次的正向路径，显式断言
  `send_reply_push` 被调用且其异常不冒到回合返回值；早期版本套错了 recovery-drain
  骨架导致断言恒真，复审时改写并核实）。
- **已知取舍**：worker 崩溃后由 recovery drain 落库的回复不经 `_on_reply`，不写
  槽位，那条推送会丢（消息本身不丢；V1 同场景也没有推送，接受）；一个回合多条气泡
  只推最后一条（用户点进 app 能看到全部气泡，推送只是唤醒手段，一次足够）；`title`
  仍硬编码 `"IO"`，留到下一轮和 V1 一起改，避免两个 runtime 观感不一致。详细设计见
  `docs/superpowers/specs/2026-07-25-v2-push-parity-design.md`。
- **未做**：pre 环境真机端到端验证（切后台收锁屏通知、前台不弹通知且
  `push_decision=suppress`、关闭「主动提醒投递」开关后 `alert_status=suppressed`
  等四条）需要部署到 pre 加真机操作，本次未执行，留待人工验证。

### [DONE] prod `users` 全表重载风暴 —— 常驻心跳改走定向广播

- 症状：prod 每分钟 30–74 次全表 `users` 重载（667 行 × ~7 个订阅进程）。
- 真因：`/v1/chat/poll` 的常驻存活心跳每 60s 给每个在线常驻用户重写一次
  `access_bindings[resident].last_seen_at`，走的 `registry.persist_user()`
  发的是**不带 user_id** 的 `users` 广播，订阅方一律 `load_users()` 全表重载。
  单行写被放大成 N × 进程数 × 667 行读，随在线常驻用户数线性增长。
- ⚠️ 更正：07-24 的交接文档诊断为"access_bindings 纯序列化顺序抖动被误判为真
  改动"，并建议给 `persist_user` 加 canonical noop 短路。**该诊断已被 prod 复测
  推翻** —— 27/27 变化行都是真实时间戳标量变化，pure-ordering 行数为 0，noop
  短路修不了这个风暴。取证与更正见
  `docs/incidents/2026-07-25-users-reload-storm-resident-heartbeat.md`。
- 改动：新增 `db.load_user()`（单行读，DB 错误上抛）、
  `registry.reload_user()`（原地刷新一行 + 重建 key cache，读失败保留现有行，
  绝不把瞬时错误当成"行已删除"去踢掉活用户）、
  `registry.reload_users_after_notify()`（带 user_id 走单行、不带走全表），
  `persist_user(..., targeted_broadcast=True)` 仅供心跳使用；
  `asgi/lifespan.py` 与 V2 `serve_worker.py` 的 `users` handler 都接上。
- 其余 `persist_user` 调用点（注册/发钥/公钥/偏好/access flip）语义不变，仍是
  全表广播 —— 它们稀疏，不构成风暴。
- code review 后补回收窄丢掉的三个性质（都是 `load_users` 原本顺带提供的）：
  ①`reload_user` 的库读挪回 `_users_lock` **内**（原先锁外读、锁内装，监听线程会
  用旧快照回滚同进程的并发编辑，再被下一次整文档 upsert 写回库，永久丢刚签发的
  key）；②新增 `_normalize_row_cas_locked()` 做单行 normalize+CAS（admin
  data_track 原样快照 `_users`，依赖重载时归一化）；③新增
  `start_periodic_full_reload()`（每进程 daemon 线程，默认 300s，
  `FEEDLING_REGISTRY_FULL_RELOAD_SEC`）——原先那 38.7 次/分的无差别广播事实上
  兼任了每 ~1.5s 一次的全量自愈通道，收窄后必须显式补回；配套
  `load_users(guard_empty=True)` 防止 `db.load_all_users` 吞错返回 `[]` 时清空
  registry 导致全站 401。
- 另修：单行重载改用 `_reindex_user_keys_locked()` 只动该用户的 key hash（原先仍
  调全量 `_rebuild_key_cache()` 清空重建，`_resolve_user` 无锁读会在窗口内 miss 并
  跌进 deepcopy 慢路径，等于换个触发点保留了同一个锁 convoy）；并改掉一处与行为
  不符的注释（心跳并非"只碰 resident binding"，`persist_user` 一如既往 upsert 整
  文档，targeted 指的是广播范围）。
- 第二轮 review 修复收窄的副作用（10 条抓出，逐条处理）：①**心跳改 CAS**——原先
  整文档 upsert 陈旧快照会覆盖别 worker 刚签发的 API key（自愈间隔拉长后放大成
  永久丢 key，CONFIRMED），改成对新鲜 DB 行做 `compare_and_set_user`，只改
  resident binding 时间戳、DB I/O 出锁；②`load_users` 用
  `db.load_all_users(raise_on_error=True)` 区分读失败（保留快照）vs 真空表（清空），
  替掉只加在周期路径且会阻止合法清空的 `guard_empty`；③`_env_float` 守卫防坏值
  import 期崩全站；④daemon 线程移出被测试调用的 `wire_assembly`（backend gate 在
  `start_background`，V2 移到 `main`）；⑤自愈间隔 300s→60s；⑥reindex 先加后删消除
  无锁读窗口；⑦定向 reindex 不抢别用户的共享 hash（防串号）；⑧去掉对非 dict 抛错
  的 sort；⑨测试改 patch registry 本地线程工厂而非 stdlib threading。
- 第三轮 review 修复：①周期自愈的 gate 移除——第二轮 gate 在 `start_background`
  （默认 False）会让未设该 flag 的 web worker 有 `users` handler、serve auth 却无
  自愈通道（丢 notify → 已删/吊销账号持续鉴权），改成与 handler 配对无条件启动；
  ②删掉 `persist_user` 的死参数 `targeted_broadcast`（心跳改 CAS 后无 caller，且是
  footgun）；③修两处文档不一致（测试数、自愈段过时的 300s/guard_empty）。
- 第四轮 review 修复：①**`turn_child.main` 补起周期自愈**——第三轮漏了这个长命
  spawn 子进程（经 wire_assembly 装配但走自己入口），丢 notify 后它用陈旧公钥封装
  托管回复 → decrypt-failed（CONFIRMED，用户可见）；②`compare_and_set_user` 的 TEE
  影子库 mirror 改无条件 upsert，收敛 drift 的影子行（原条件 CAS UPDATE 在影子 drift
  时静默 no-op）；③`_reindex` 删除步从 O(全舰队 key) 改成 O(该用户 key)（用旧行 diff）；
  ④CAS-loss 重试复用赢家行、省一次 load_user。
- 验证：`tests/test_users_reload_targeted.py`（7 条）+
  `tests/test_users_reload_review_fixes.py`（21 条）；全量 pytest 6269 passed，
  3 条既有失败与干净 HEAD 基线逐条一致。**未部署**。
- 第五轮 review 修复：**回退第四轮把 TEE 影子库 mirror 改无条件 upsert 的改动**——
  它会在删除竞态下复活已删用户的加密影子行（心跳 CAS 更新主库 commit 后、跑独立影子
  mirror 前若账号删除插入，无条件 upsert 把已删行复活进影子库，滞留到 24h prune）；
  mirror 回退为条件 CAS（`WHERE doc=expected`，影子行被删则 UPDATE 0 行、不复活），
  drift 交给 reconcile。更正第四轮方向错误：复活已删数据比 drift 不收敛严重得多。
  另修一处空断言测试（补 deepcopy 隔离的真断言）。
- 未做（已记录在 incident 文档「遗留」）：`last_seen_at` 仍存在账号文档里，本次
  只把扇出缩小 667 倍、没消除，成本仍按（在线常驻数 × 进程数）增长；
  `reload_user` 锁内单行读、`_normalize_all_users_cas` 稳态全表 deepcopy 为已知权衡。

### [FIX] #107 感知 fence 回归的第二处：device event 观察也恒跑 + 解密失败不再静默

- **背景**：PR #107 把 `perception_ingress_runtime_v2_enabled` 从 env baseline
  改成跟随 chat runtime fence，07-24 10:12Z 上 prod 后所有 resident-chat 用户
  （≈全 prod）掉进不解密的 legacy 路径。`/report` 已由 hotfix `c9ab3a12`（PR
  #114）修好，本次修的是**同源但被漏掉的第二处**。
- **`proactive_core.device_events_append`**：`ingest_device_event_v2` 不再受
  fence 控制（这处**根本没有 legacy else 分支**，不像 `photo_evaluate` 有对称
  的 `_maybe_wake`）。它是 `unlock_after_absence` / `screen_phash` 两类唤醒的
  **唯一**生产者（`perception/differ_v2.py`）。prod 取证：unlock 唤醒从部署前
  ~13/h 掉到 0/h，且 #114 之后**没有恢复**；`runtime_v2` 感知事件同期
  118/h → 0 → 修 report 后只回到 ~36/h（缺口就是这条）。
- **`service.ingest_snapshot_v2`**：信封解密失败（`decrypt_failed:*` /
  `decrypt_skipped` / `invalid_envelope`）以前**完全静默**——`results[key]` 在
  解密前就置了 `accepted`，失败只是不写值，无日志无事件。现在补 `log.warning`
  + 一条审计行。客户端契约不变（仍返回 `accepted`），也仍然不臆造值。
  审计行走**独立**的 `perception_decrypt_failures` 流（cap 500/用户），
  **刻意不写 `perception_events`**：`_last_wake_ts` / `_last_v2_wake_ts` 只扫
  该流最新 50 行找上一次 wake，失败风暴会把 wake 行挤出窗口 → burst/cluster
  去重静默失效，正好赶在全站不健康的时候。
- **注释订正（本次事故的心智根因）**：`core/util.runtime_v2_default_on`、
  `proactive/screen_flag_v2`、`proactive/resident_runtime_v2` 都写着「prod 默认
  OFF」，但三份主 compose 全都注入 `FEEDLING_RUNTIME_V2_DEFAULT_ON=true`
  ——prod 一直是 ON。正是这个错误认知让 #107 的改动看起来像 no-op。新增守卫
  `test_every_main_compose_turns_the_runtime_v2_baseline_on` 把 compose 与函数
  钉在一起。
- **测试**：`test_device_event_ingress_runs_even_when_the_chat_fence_says_legacy`、
  `test_snapshot_v2_records_an_audit_event_when_a_signal_fails_to_decrypt`；旧的
  `test_device_event_route_only_runs_perception_ingress_when_flag_on`（钉的正是
  被修掉的错误行为）改写为 `..._surfaces_the_perception_ingress_result`。
- **状态**：未提交、未部署。上线后应观察 unlock 唤醒回到 ~10-15/h、
  `runtime_v2` 感知事件回到 100+/h 量级；主动消息量会随之回升到 07-23 水平。

### [DONE] 例行清理第五轮（链接完整性 / git 洁癖 / enclave+hosted 深扫 / 根级文档）

- **全仓 Markdown 链接检查**：修 5 处现役悬空指针——openclaw 插件 README
  的 `agent/routes.py`→`perception_core.py`；PARITY_MATRIX 测试清单删已随
  dual 恢复而删除的 `test_hosted_resident_retirement.py` 行；
  OPTIMIZATION_BACKLOG #14 补「hosted tick 线已整体退役」后记；
  DEPLOYMENTS 两处历史指针（acme_dns01、bootstrap-test-cvm.yml）加已删
  注记。历史横幅文档内的悬空引用（litellm/app.py/设计期 v2 模块名等
  130+ 处）按惯例保持原状。
- **hosted/ 深扫**：删 2 个死符号——`agent_runtime_cutover._env_truthy`
  （dual 恢复批次带回但从未接线）、`mcp_tools.MAX_MCP_TOOL_DESCRIPTION_CHARS`
  （兄弟常量都活，唯它孤儿）；`config_store.set_last_runtime_error` 判仅
  测试供养入 #15 台账并修正其 docstring 的幻影调用方声明；
  **`file_text.py` 头部重写**——原自述「compatibility tests only、V2 不
  import」已失实（它是 V2 `local` sandbox provider 的生产提取器）。
- **enclave/ 深扫**：零死符号；修 3 处失实注释（config.py 两处 Flask 措辞
  →ASGI、health.py 「single-threaded」删除——现为 4 worker×32 线程池）。
- **根级文档**：CONTRIBUTING 包结构树补齐 15+ 漏列包（8 个承载路由的领域
  包、asgi/ 装配层、enclave/ 包、agent_runtime/capabilities/workspace 等）
  与 6 个漏列底层模块；DESIGN.md 头部定位更新（iOS 已迁独立仓、docs-site
  纳入公开面、admin HTML 面板不受其约束）；README 幽灵数字 31→23
  （git 移除提交正文自洽值）。建议未动：CLAUDE.md「所有 UI 先读
  DESIGN.md」可限定为 iOS/SwiftUI（用户指令文件，留 owner 定）。
- **git 洁癖**：全仓 1318 个被跟踪文件零垃圾（无生成物/pyc/DS_Store/大
  文件）；唯一补丁 `.gitignore` 加 `.pytest_cache/`。

### [DONE] 例行清理第四轮（db/SQL 层 / V1 consumer / plans 档案横幅 / 目录型资产）

- **db.py**：删 2 个全仓零引用的全死函数 `try_stamp_hosted_tick`（与第二轮
  删掉的 `FEEDLING_HOSTED_TICK_*` 死配置同源——hosted tick 整线已退役）与
  `genesis_latest_done_job`。死表 0、死 SQL 常量 0、tee 三包死函数 0。
  6 个「仅测试供养」候选未删，已登记进 OPTIMIZATION_BACKLOG #15 待领域
  裁定（`insert_user` 维持 #15 既有勿删判定；`chat_newest_ts` 判 oracle
  保留）。
- **僵尸 monkeypatch 修正**：`test_chat_history_selfheal.py` 的 fail-open
  测试 patch 的是探针已不调用的 `chat_newest_ts`（boom 永不触发、测试空
  转），改 patch 真实探针 `chat_count_since`。
- **V1 consumer（13k 行专项）**：仅一个死常量 `_WEEKDAYS_ZH`（时间渲染早
  已抽到 `chat/reply_language.py`，那份是活的），已删；无死分支、无误导
  注释；HTTP/Hermes 路径是 env-gated 冷路径非死码。
- **plans/specs 档案横幅**：`2026-07-18-runtime-v2-completion-design.md`
  补 RETIRED（整篇 V2-only/杀 resident 命题被 07-21 dual 反转、原先无任何
  标注，最严重一份）；`2026-07-09-…-D0-rollout-infrastructure.md` plan 补
  RETIRED（其 spec 早已 RETIRED、plan 漏标）；
  `2026-07-04-tee-postgres-migration-design.md` 补部分退役说明
  （Phase 0–1 现役、Phase 2–3 随 supervisor 拓扑作废）。
- **目录型资产全干净**：copytext（DB 服务非静态目录）、notices catalog
  24 类全有发射方、conftest 仅 2 个高频 fixture、model_api_runtime 无外部
  prompt 资产、loadtest/provider_probe 无断裂引用——零死项。

### [DONE] 例行清理第三轮（tests 整洁 / 部署脚本 / 剩余 docs / 模块 docstring）

- **tests/ 未用 import 清理**：pyflakes 告警 73→38。逐条核实后删 30 条纯
  标准库死 import、2 处部分符号、2 处冗余局部绑定（改裸调用保留副作用）、
  `test_perception.py` 7 处函数内重复 import。**19 条 `content_encryption`
  探针 import 是 try/except stub 注入模式（带 noqa），全部保留**；另 2 处
  作者显式注明故意保留的也未动。11 条被测模块整模块别名 import 疑似死码但
  不排除冒烟意图，留待人工；2 处疑似「测试漏断言」线索见 CHANGELOG 下方注。
- **部署面**：`wait-cvm-ready.sh` 删 litellm 残句；`feedling-backend.service`
  Description Flask→ASGI；`setup.sh` 补 LEGACY 横幅（VPS 时代路径，指向
  docs-site self-hosting）；`BUILD.md` 补 `requirements-v2-worker.lock`
  再生成步骤（命令对齐 lock 文件头）。
- **docs**：`AUDIT.md` 修 5 处指向已迁出 iOS 仓的 `testapp/` 路径（iOS
  2026-05-22 迁独立仓，07-06 大清理漏网）+ `.gitignore` 删 3 条 testapp
  残留；`FRONTEND_ERROR_CONTRACT.md` Phase B/C 状态「待排期」→已上线
  （notices 路由/catalog 均已 ship）；`OPTIMIZATION_BACKLOG.md` #5 帧存
  R2 标 ✅；`PROD_DEPLOY_VERIFICATION_2026-07.md` 加快照横幅并移除对已因
  泄漏事故删除的密码文档的悬空引用。
- **backend docstring**：`agent_runtime/__init__` 停在「P0 单用户 Claude
  Agent SDK 原型」的描述重写为多租户 cli-mode supervisor 现状；
  `content_encryption.py` 4 处 `enclave_app.py _box_seal_open_hkdf` 引用
  改为现址 `enclave/envelope.py box_seal_open_hkdf`（函数已去下划线）；
  agent_runtime README 迁移号 0005→0004、Layout 补 `introduction.py`。
- **两个疑似测试缺口已核实并处置**：`test_multi_tenant_isolation.py` 的
  `my_markers` 经查非漏断言——chat/identity/memory/whoami 四段各有独立正反
  断言，属纯冗余构造，已删；`test_chat_resident_consumer.py` 原
  `test_empty_content_decrypt_source_available_replies` 确实没测它声称的
  场景（`_process_messages` 不做解密，合并在 poll 循环上游；喂
  `decrypted_msg` 才是它真正测的契约），已改名
  `test_decrypted_content_from_decrypt_source_replies`、删除无效的
  `empty_msg`/`get_decrypted_history` patch 并如实重写 docstring；合并
  胶水的两个 helper（`_poll_decrypt_since`/`_filter_messages_to_poll_ids`）
  与「配置源仍无明文」读失败分支均确认已有专门测试覆盖。

### [DONE] 例行清理第二轮（docs-site / env 接线 / 参考文档事实性 / 符号级）

- **死配置删除**：`FEEDLING_PROACTIVE_GATE_PROVIDER`/`_MODEL`（三份 phala
  compose；gate 已 v2 化、`proactive/gate.py` 硬编码 `proactive_v2:wake`，
  全仓零读取方；`OPENROUTER_API_KEY` 保留——dashboard 调试翻译在用，注释
  已改写）；`feedling.env.example` 的 `FEEDLING_HOSTED_TICK_*` 两条（被
  `PROACTIVE_TICK_*` 取代）；`chat_resident.env.example` 的
  `FALLBACK_COOLDOWN`（无读取方）。
- **符号级死代码**：删 `model_api_runtime/v2/summary_frontier.render_frontier`
  （零调用，渲染已走 `render_replacement`）；删 `hosted/config_store.py` 与
  `perception/service.py` 各一条真·未用顶层 import；清 tools/ 六处未用
  import（`user_logs.py` 的 boto3 是带 noqa 的故意探测，保留）。
- **参考文档事实性修正**：`PROJECT_OVERVIEW.md` 删悬空 `acme_dns01.py` 表行；
  `TESTING.md` L2 路径 `tools/`→`tests/`、决策矩阵 E 行 litellm 遗词改现役
  pi-driver 体系；`README.md` memory floor 数值对齐代码（1+月 ≥12 非 ≥15）；
  `RUNTIME_FLOWS.md` §3 历史段顶部补「点名符号可能已删」免责；
  `API_ERRORS.md` 补登 8 个漏登记 slug（`client_msg_id_invalid`、
  `invalid_reasoning_effort`、`model_api_config_delete_failed`、
  `nudge_delta_exceeds_cap`、`material_empty`、`redistill_job_active`、
  `confirmation_mismatch`、`device_already_enrolled`，新增通知中继小节）。
- **docs-site 公开文档三处 dual-runtime 失实修正**（changelog.mdx Unreleased
  的 V2-only 表述、架构图 serve-worker「separate CVM」定位、self-hosting
  `FEEDLING_HOSTED_RUNTIME_POLICY` 行）；`npm run types:check`/`lint`/`build`
  全绿，OpenAPI 无需重生成（无 API 面变更）。
- 判保留：`workspace/sandbox.py` 的 `register_*_sandbox_provider` 部署扩展
  钩子；tests/ 内 73 条良性 unused-import 告警（独立整洁项，未动）。

### [DONE] 过时代码/文档/注释例行清理（四路全仓扫描 + 逐项取证）

- **删文档 2 份**（均核实已落地后删）：`docs/PI_USER_MCP_GAP_给志豪_2026-07-17.md`
  （pi user-MCP 桥已在 `spawners.py` `{mcp}` 模板 + consumer `-e <bridge>` 实现，
  诊断失效）、`docs/IDENTITY_CARD_NEVER_GATES_2026-07-12.md`（已由
  `2e72f13e`/`24f32df5` 落地）。
- **删测试 3 处**：`tests/test_bootstrap_gates.py` 两个被 v1 淘汰、长期
  `@skip("P6 …retired by v1")` 的 per-tab 语义用例（未 skip 的 retype 错误路径
  用例保留）；`tests/conftest.py` `_PURE_UNIT` 白名单里指向已删除
  `test_model_api_prompts.py` 的死条目。
- **修过时注释**：`deploy/Dockerfile` 启动注释从 Flask/app.py 时代重写为
  ASGI + 多 worker（leader election）现状；三份 phala compose 的
  `backend/app.py` 指向改 `backend/push/apns.py`；`gunicorn_conf.py` 一句
  Flask 残留；alembic `0007` 的 backfill 脚本路径。
- **补历史标注**：`DEPLOYMENTS.md` Phase-E compose 快照行（mcp 已删、backend
  已 ASGI）；两份 `pi-on-multiprofile` plan/spec 横幅补 deepseek 07-14 回退
  claude driver 说明。
- **取证后判保留（勿反复清理）**：`backend/hosted_runtime.py` 与
  `backend/proactive/background_v2.py` 虽仅测试引用，但 07-18 第五/六轮清理
  已 git 取证终审判保留（OPTIMIZATION_BACKLOG #15：proactive V2 在建预留面 /
  oracle），无新证据不推翻；`deploy/PHALA_ACCOUNT_MIGRATION.md`（prod 仍在
  sxysuns 账号，§Phase 2 未走完）；`AGENT_CLI_INTEGRATION_SURVEY.md`（被
  live 代码注释引用）；`UPLOAD_MEMORY_IDENTITY_CALIBRATION_2026-07-07.md`
  （`genesis/prompts.py` 仍标 DRAFT 待定稿，是产品意图正本）；
  `docs/superpowers/plans+specs` 整目录（持续维护的日期档案）；
  `tools/frame_envelope_roundtrip_test.py`（5001 仍是现役端口，"Flask 时代
  端口"判据不成立）。
- 验证：pyflakes 零告警；全量 pytest 与清理前基线对照零新增失败。

## 2026-07-24 — TEE Redis CVM 基础设施（未开通）

三套独立 Redis CVM（test/pre/prod）的全部代码就绪：官方 `redis:8-alpine`
TLS-only + backup sidecar（每小时 `redis-cli --rdb` 快照 → age 非对称加密 → R2）。
部署纪律复刻 TEE Postgres：`--kms phala` 身份（无链上 AppAuth）、手动 workflow、
cvm-id fail-closed、永不并入 merge 自动部署。

**当前零流量**：没有任何业务代码引用 Redis，三台 CVM 也尚未开通
（cvm-id 文件为空 → workflow 拒绝运行）。缓存 / 队列 / 锁的接入各自另开 spec。

关键决策见 `docs/superpowers/specs/2026-07-24-tee-redis-cvm-design.md`：
`noeviction`（避免静默驱逐锁与队列）、sidecar 而非内嵌镜像、显式 sleep 循环
而非 cron（PG 2026-07-14 cron PATH 静默失败的教训）、`redis-cli --rdb` 而非
拷卷文件、age 非对称加密（备份机被攻破也解不了历史备份）。

首次开通 runbook 见 `deploy/DEPLOYMENTS.md`「TEE Redis」章节。

## 2026-07-22

### [DONE] Task 11 — 双运行时部署拓扑：serve-worker 并入主 CVM，runner 回 V1-only

- 三份主 CVM compose（`docker-compose.phala.yaml` prod / `.test.yaml` /
  `.pre.yaml`）各新增 `serve-worker` 服务：与 `backend` 同镜像、同 tag，
  `command` 跑 `backend/model_api_runtime/v2/serve_worker.py`，
  `FEEDLING_ENCLAVE_URL`/`FEEDLING_API_URL` 改为 compose 内网地址
  （`https://enclave:5003` / `http://backend:5001`，照抄 backend 现用值，
  不再走公网 gateway passthrough）。`backend` 与 `serve-worker` 都加
  `FEEDLING_HOSTED_RUNTIME_POLICY: "dual"`（原 `v2_only` 字面量）+
  `FEEDLING_RUNTIME_DEFAULT_DESIRED: "resident"`——日一部署行为与部署前
  完全一致（全员 fence=resident），个别用户走 allowlist 单独切 v2。
- `deploy/docker-compose.phala.prod.runner.yaml` 从 `origin/test` 逐字节
  恢复为 V1-only 形态（`agent-runner` + `supervisor.py`）——这就是 prod
  当前的真实部署形态，仓库文件此前因早前 Runtime-V2-only 迁移任务而漂移。
- `deploy/docker-compose.phala.runner.yaml`（test 独立 runner CVM）与
  `deploy/docker-compose.phala.pre.runner.yaml` 均是/回到 V1 `agent-runner`
  形态。**Code review 纠偏**：test 环境不是「需要新配一个 V1 runner 的缺口」——
  test **今天本来就是 V1 托管**：`origin/test` 上的 `docker-compose.phala.
  runner.yaml` 从来就是 agent-runner 形态，CI `deploy-test-runner-cvm` 把它
  部署到真实存在的 `feedling-io-agents-test` CVM（P0 的 host-all 修复正是在
  这个 CVM 上验证的）；仓库文件此前因分支上更早一轮 Runtime-V2-only 迁移工作
  漂移成了纯 serve-worker 形态,未曾推送到该 CVM 的真实部署。本次连同 prod 一起
  从 `origin/test` 逐字节恢复。pre 的 runner 是从
  `origin/test:deploy/docker-compose.phala.runner.yaml` 的 V1 runner 模板改编
  （env var 名不变，仅重命名 app/container/volume 为 pre 前缀）——三环境现在
  拓扑完全同构：主 CVM `dual`（backend + serve-worker）+ 独立 runner CVM
  V1 `agent-runner`。P1 用 pre 这一对验证双跑全链路，早于 P3 动 prod。
- CI（`.github/workflows/ci.yml` + `deploy/pin-runtime-release.sh`）：
  release-pin 脚本**无需改动**——serve-worker 现在与 backend 共用
  `ghcr.io/…/feedling:<sha>` 镜像引用，脚本对 `main_compose` 的正则替换
  本就覆盖它；已 dry-read 确认。`deploy-test-runner-cvm` /
  `deploy-prod-runner-cvm` / `deploy-pre-runner-cvm` 三个 job 的
  `phala deploy -e` 参数都改回 V1 变量
  （`AGENT_MAX_CHILDREN`/`AGENT_RUNTIME_USERS`/`FEEDLING_HOST_ALL` 等），
  移除了各自 V2-only 的 post-deploy 校验步骤（`check-v2-runner-fleet.py`
  liveness/fleet-identity gate、pre 的 prompt-cache canary）。
  `deploy/check-prod-runner-topology.sh` 的「≥2 个独立 runner CVM」检查默认
  降为软告警（`DEPLOY_PROD_RUNNER_CVM` 开关 + `PROD_RUNNER_TOPOLOGY_ENFORCE`
  重新拉紧），否则当前只有 1 个已配置的 prod runner CVM 会让硬门槛永久卡死
  所有未来 prod 主 CVM 部署——**这个默认极性与 disable 开关是 Task 11 新引入
  的，不是「恢复」origin/test 的旧脚本**（origin/test 从无 disable 参数、也
  从无「main CVM 不得出现在 runner inventory」检查）。Code review 还纠正了一处
  安全回归：main-CVM-membership 检查此前被放在 `enabled` 提前返回之后，会被
  `enabled=false` 一并跳过；已挪到提前返回之前、无条件执行，并补了组合回归
  测试（`enabled=false` + main CVM 出现在 inventory → 仍必须 hard-fail）。
- **CI 安全核对**：workflow 顶层 `on.push.branches: [main, test, pre]` /
  `on.pull_request.branches: [main, test, pre]` 已排除 `feat/dual-runtime`；
  每个 deploy/validate job 的 `if:` 额外要求
  `github.ref == 'refs/heads/{main|test|pre}' && github.event_name == 'push'`。
  推本分支不会匹配任何一个 job，不会触发对 prod/test/pre 的任何自动部署。
- `deploy/DEPLOYMENTS.md` 新增「双运行时拓扑」小节（拓扑图 + 环境变量表 +
  P3 部署序）；三环境现已同构描述，删掉了此前一版里关于「test 缺 V1
  runner」的条件式披露（那是基于 test 现状的错误前提写的，已用上面的纠偏
  重写）。

## 2026-07-20

### [DONE] Runtime and product telemetry share one honest operator dashboard

- The existing admin data-track page now combines 30-day Runtime V2 effective
  input/output/cache token totals, provider-usage coverage, V2 account/turn
  counts, current hosted/self-hosted activated-account coverage, and the
  existing iOS foreground-duration roll-up.
- Missing provider usage stays unknown and lowers the displayed coverage ratio
  instead of becoming a false zero. Self-hosted coverage is explicitly the
  observable activated account-route count: fully offline instances that never
  contact the official backend cannot be counted, and reinstall orphan rows can
  keep it from being an exact human or deployment count.

### [DONE] Runtime V2 release closure makes trajectories durably debuggable

- Encrypted raw chat, trajectory, and review content has no time-based TTL or
  background GC. Chat Clear moves raw rows into an immutable non-prompt archive
  and preserves trajectory/review evidence while clearing live conversation/
  prompt state; account deletion remains the complete per-user erasure boundary.
- Added a default-off, runner-local break-glass inspector for one exact user/job.
  It requires a validated operator, fixed reason, and case reference, and writes
  durable requested/outcome audit phases without creating a plaintext HTTP or
  admin endpoint.
- `GET /v1/admin/v2-metrics` now exposes a bounded content-free `turn_health`
  snapshot covering terminal outcomes, queue/lease expiry, oldest pending age,
  p95 latency, and trajectory completeness/capture gaps.
- Automatic Memory Capture and Memory Dream are independent deployment
  controls and every managed environment, including Pre, defaults both off.
  The dormant Capture path now uses exact raw-seq coverage with live-row-only
  provider disclosure, encrypted prepared-batch recovery before provider setup, and one
  atomic Memory/log/frontier/job commit. User opt-out, fleet halt, Chat Clear,
  and runtime cutover now linearize against the complete provider disclosure,
  while Memory retype derives changes from the fresh cross-process-fenced row.
  Lost ownership and synthetic-only windows fail or advance without leaking
  content, repeating the provider call, surfacing a chat error, or silently
  skipping a frontier.
- The optional E2B artifact path now shares the iOS/backend 25 MiB boundary and
  a content-addressed template tag. Source is activation-ready, but remains
  fail-closed until an environment supplies the provider credential/template
  and approves the external data boundary.
- Typing-signal pre-warm is explicitly removed from the Runtime V2 release scope.
  Remaining release gates are operational: target-CVM load/fault soak, external
  health alerting, a second production runner, and live zero-resident inventory.

## 2026-07-19

### [DONE] Runtime V2 flight recorder becomes byte-complete for model-visible turns

- Oversized provider/tool/reply trajectory events now use exact, digest-verified
  encrypted chunks appended in one stream transaction and one multi-row INSERT
  instead of retaining only a 512 KiB prefix. Serialization, compression, and
  envelope sealing run off the shared asyncio loop; unsupported values now fail
  visibly instead of becoming depth/unsupported omission markers.
- Every async compatibility and transient provider HTTP attempt is retained in
  memory and folded into the existing encrypted response/error event: exact JSON
  request body, effective request model, status/error class, fallback, ordinals,
  and monotonic duration. This adds no attempt-level database or network call.
- Parallel tool results now carry monotonic duration and durable platform/MCP
  effect evidence; reply delivery records the durable effect disposition.
  Aggregate metrics still name the user-configured provider/model, while the
  trajectory names the effective wire model per attempt.
- Required or best-effort evidence failures emit an encrypted `capture_gap`
  marker before terminal capture, so an otherwise terminal trajectory remains
  explicitly partial rather than claiming false completeness.
- Per-user `v2_turn_metrics` now cascade on account deletion, with the redundant
  reset belt, dedicated child-key index, and direct cascade coverage updated so
  model/token telemetry cannot survive the documented complete-erasure boundary.
- Offline review remains bounded and side-effect-free, and retention/GC plus a
  restricted inspection/export policy remain explicit product/operations work.

### [DONE] Runtime V2 harness parity lands in source with explicit live gates

- The unified model-visible catalog now has 24 built-in tools: 21 platform
  capabilities plus bounded `task` subagents, loop-native `reply`, and chat-only
  `send_file`. `send_file` publishes only an explicit, current-user encrypted
  `/workspace` entry through the idempotent reply outbox; UTF-8 text formats are
  delivered directly, while `.docx` and `.pdf` targets are rendered into real
  Word/PDF bytes instead of renaming Markdown. Final text and file cards commit
  as one ordered reply bundle. Explicit formats get one bounded completion
  recovery and reject a Markdown substitution. If the file is still missing, the
  model's non-empty terminal text is preserved and the turn completes with a
  `required_file_missing` trajectory event. It never scans or accepts a host
  filesystem path.
  Independent reads/tasks run concurrently; disjoint workspace writes may commit in
  conflict-free waves, while conflicting paths and external effects remain
  provider-ordered.
- Added the encrypted, backend-pluggable VFS: read-only `/artifacts` and
  `/skills`, editable `/workspace`, and editable `/memory/WORKING.md` separate
  from Memory Garden. Existing text views and virtual text edits do not acquire
  a sandbox; uncached artifact materialization fails closed unless an E2B or CVM
  provider is configured. E2B is source-wired but remains a deployment data-
  boundary decision, not an automatic upload path.
- Provider adapters preserve a deterministic tool/system/trusted-skills cache
  prefix and normalize cache reads/writes/misses, including Bedrock Converse
  cache points. Editable `WORKING.md` is deliberately pull-only rather than
  eagerly cached; reading private workspace state removes later outbound
  web/MCP/`task` tools for that turn. The existing Pre canary proves OpenRouter
  only; native Bedrock and a live trusted-skills mutation still need deployment
  evidence.
- Runtime V2 now persists an append-only encrypted per-job trajectory.
  Optional provider-backed failure review is default-off, fail-closed,
  database-admission-bounded, and structurally has no reply/tool/effect surface.
  It is offline analysis rather than deterministic replay; automatic retention/
  GC and restricted inspection/export policy remain open.
- Eager perception grounding is now a strict fixed-field numeric/boolean/null
  projection. Third-party calendar/reminder/app/place/weather text and
  screen/photo content are pull-only; after an explicit text-bearing read the
  loop removes later web, MCP, and `task` channels. Numeric health reads remain
  compatible with later outbound work.
- Raw encrypted Chat rows and attachment bodies remain the durable ledger. The
  5,000-message value is only an in-process hot-window bound, never a database
  retention rule. Runtime V2 now stores exact-range conversation-summary leaves
  and higher-level checkpoints as immutable encrypted rows, retains every child,
  and binds a bounded materialized prompt view to the canonical IDs with a
  versioned CAS. Existing aggregate summaries migrate lazily and oversized old
  summaries are reduced through bounded hierarchical calls even without a new
  message.
- Explicit Chat clear is now a generation-fenced atomic reset of the live
  conversation: raw encrypted rows move to an immutable non-prompt archive,
  while summary, chat-derived artifacts, pending effects/status, and reply
  cursor are removed. Paused old workers cannot recreate cleared context.
  Independent Memory, user-authored workspace, schedules, content-free metrics,
  archived chat, and encrypted trajectory telemetry remain until account
  deletion, the complete-erasure boundary.
- Hosted resident retirement is complete in source and managed manifests, but
  live fleet closure still requires deploying the reviewed image everywhere,
  provisioning production's second runner failure domain, and verifying zero
  legacy hosted processes. Typing-signal pre-warm also remains unimplemented.
- Production runner deploys now bind each worker heartbeat to the target
  inventory CVM ID and exact seven-character image build. The worker fails
  closed on missing/mismatched identity, and CI outlives stale heartbeat windows
  before requiring a current-build turn + Genesis pair for every listed CVM;
  the gate remains intentionally blocked while only one production CVM ID is
  provisioned.

## 2026-07-18

### [DONE] Hosted resident fleet retired; all managed hosting is Runtime V2-only

- Local, test, pre, and production backend manifests now force literal
  `v2_only`; every runner manifest contains only the pooled `serve-worker`.
  Test/pre/prod worker-CVM deploy jobs are mandatory and fail when their
  topology is missing.
- Removed the hosted resident supervisor, spawners, leases, token helper,
  per-user CLI homes/checkpoints/volumes, roster/host-all controls, and resident
  rollback/admin selector. The historical `feedling-agent-runner` package name
  remains, but its image now contains only the Python Runtime V2 worker.
- Hosted send requires the exact V2 ownership tuple and fails before persistence
  on stale ownership, dead workers, kill switch, or admission rejection. The
  independent user-operated `/v1/chat/*` resident consumer remains separate and
  cannot claim hosted accounts.
- Preserved iOS retry correctness during the cutover: `client_msg_id` duplicate
  detection now runs inside the same transaction as V2 message append and job
  enqueue, so a lost `202` cannot create a second row or execute a second turn.
- Replaced the resident rollback guide with V2 scale/recovery procedures and
  added structural tests that reject any return of hosted resident services,
  selectors, CLI toolchains, or optional worker deploys.
## 2026-07-23（数据导出补上世界书）

### [DONE] `/v1/content/export` 加 `world_book`，schema_version 2→3

- **背景**：审查导出/备份功能时发现，导出只覆盖 chat / memory / identity
  （+frames 密文），但**世界书是用户在 App 里亲手写的内容**
  （Settings → 世界书，`WorldBookListView`/`WorldBookEntryEditor`），
  且删号会连带删掉（`db.delete_user_data` 的 14 张表清单里有
  `world_book_entries`）。导出交给用户的是一份不完整的副本。
- **实测取证**（test 环境合成账号，跑完已清理）：同一账号
  `GET /v1/worldbook/list` 返回 1 条，`GET /v1/content/export` 顶层键只有
  `[attestation_snapshot, chat, exported_at, frames, identity, memory, notes,
  schema_version, user_id]`——没有任何 worldbook 字段。同一轮还确认了
  local_only 聊天消息**确实**在导出里（无 K_enclave，本地私钥解密成功）。
- **改动**：`content_core.export_data` 从 `store.world_books` 读出完整信封
  （与 `worldbook_core.list_envelopes` 同源），密文逐字返回、服务端不解密；
  iOS `buildPlaintextExport` 加 `decryptWorldBookForExport`（沿用
  memory/identity 的"元数据 + 明文平铺合并"形状），plaintext 侧
  schema_version 3→4；导出文案（zh + en）补上世界书。
- **schema_version 2→3 的理由**：空 `world_book` 数组和"这份导出早于世界书
  支持"在 JSON 里长得一样，只有版本号能区分。
- **TDD**：`tests/test_content_export_world_book.py` 5 条（导出条目、不按
  visibility 过滤、信封字段完整性、空列表、版本号），先红后绿；
  `test_asgi_content.py` 的 parity 断言随契约更新为 3。
- **部署后 test 环境实测通过**：合成账号 seed 2 条世界书 → 导出 2 条 →
  本地私钥解密出原文；`worldbook/list` 与 export 计数一致，schema_version=3，
  信封无明文泄漏。跑完账号已 reset，库中零残留。
  ⚠️ 取证坑：第一轮跑出空结果，实为**部署滚动中途**（compose_hash 从
  6ddb3480→ab0a16b3 切换期间），当时未改动的 `worldbook/list` 同样读 0；
  且 ingress 有约 3 分钟 TLS 握手 EOF。查库确认行已落地后排除。
- **附带定性（既有设计，非本次引入）**：**世界书不支持 local_only**。
  enclave `routes/worldbook.py:48` 显式把 `visibility == "local_only" or not
  K_enclave` 判进 `unavailable_ids` → `worldbook_core` 翻成 400
  `worldbook_validate_failed`；iOS `sealForCurrentUser` 写死 `shared` 且世界书
  页面无可见性 UI。合理：世界书存在的意义是被注入 agent prompt，注入必须经
  enclave 解密，local_only 条目永远不会生效。
  遗留小坑（未修，超本次范围）：`_validate_content_cap_with_enclave` 在
  `FEEDLING_ENCLAVE_URL` 未配置时直接跳过校验，故自托管不配 enclave 时
  local_only 世界书**能写进去但永远静默不生效**；且 400 的 slug
  `worldbook_validate_failed` 没说明真实原因。
- **未做**：frames 仍只在服务端导出、iOS 侧丢弃（"服务端搬 40 MiB、客户端
  全扔"）；导出仍无还原/回灌路径；80 MiB 上限与 60s 超时无逃生舱。
  这三项见当日审查结论，未立项。

## 2026-07-18（hermes/openclaw 自托管用户 MCP 接线）

### [DONE] 给自托管 hermes/openclaw 用户加 user-MCP 接线

- 自托管 hermes 与 OpenClaw 用户此前完全无 MCP 接线：config sync 抵达机器但 `_materialize_user_mcp` 仅输出 claude/codex 目标——hermes 读自己的 `~/.hermes/config.yaml` `mcp_servers`、OpenClaw 读 `~/.openclaw/openclaw.json` `mcp.servers`，两者都够不着；CA 注入只支持 Node（`NODE_EXTRA_CA_CERTS` 对 Python hermes 无效）。
- **hermes 接线**：materialize 加 hermes 目标，用 pyyaml 合并进 `config.yaml` `mcp_servers`，保留其他键和用户新增 server，备份 `.feedling-bak`；hermes CA 复用 codex 的 `SSL_CERT_FILE=castore`（纯 Python）。决策：hermes CLI `hermes mcp add` 因交互式+discovery 阻塞（~10s，无人值守不可用）被否决，改走 pyyaml 合并。
- **OpenClaw 接线**：OpenClaw 是**独立 Node runtime**（不是 hermes 别名——起草时误判，已订正），读 `openclaw.json` 的 `mcp.servers`。materialize 加 OpenClaw 目标，JSON 合并（含 `transport:"streamable-http"`）；CA 走 Node `NODE_EXTRA_CA_CERTS`（复用 claude/pi 分支，无新代码）。docker 端到端验证：`openclaw agent --local` 自动加载 `mcp.servers` 并调用 `<server>__<tool>`（deepwiki 真被调用）。
- 文档：tools/README.md 加 hermes/OpenClaw MCP 一节；io-onboarding `skill-resident-agent.md` 补 hermes（`pip install mcp` 前提）与 OpenClaw 路线说明。

## 2026-07-18（第六批：终局——全局复扫 + 灰区 16 项终审全保留）

### [DONE] 仓库清理第三轮·第六批（真收官）

- 第五批大删之后全局复扫：仅新暴露 2 个孤儿（MEMORY_CONTEXT_FRAMING_V1、
  memory_core.existing_terms_via_api_key——唯一历史调用方在已删的 turn.py
  死半边），已删；除有意保留的 SCENE_HINTS 外全仓符号级归零。
- 灰区 16 项逐一 git 取证 + 用法定性，**全部保留**（oracle 型/测试缝/在建
  预留面三类，详见 OPTIMIZATION_BACKLOG #15 终审记录）；hosted_runtime 模块
  同判保留（活语义测试的输入构造器）。
- 结论：可安全机械删除的空间已彻底挖尽；#15 记录了「测试改用手工 fixture
  后可随手删 hosted_runtime」的未来路径。

## 2026-07-18（第五批：测试手术——hosted legacy 内联轮次机器整体下线）

### [DONE] 仓库清理第三轮·第五批：turn.py 死半边 + model_api_runtime 整包

- **方法升级**：对 turn.py 做文件内传递可达性分析（根=被 turn.py 之外生产
  代码引用的函数）→ 一次性定位完整死簇 20 函数（capture/recap/state-pending/
  web-search 提取/回复解析——整套 legacy 内联轮次机器），替代此前逐个追链。
  关键证据：`_model_api_recap_due` 唯一"生产调用"在死函数
  `_model_api_maybe_run_memory_capture` 体内（活 capture 早已走 proactive
  capture lane）。
- **`model_api_runtime` 整包删除**：prompts/tools/__init__——全仓唯一 import
  方是其专属测试（memory_tools 模式的包级复刻）；"model_api_runtime" 字符串
  在 prod 的命中全是 blob/路由名非 Python import。
- **测试手术**（16 个死路径测试）：两个 worldbook 注入测试文件整删（只测死
  装配路径；consumer 活注入点无测试=既有缺口，已记 backlog #15）、
  proactive_jobs/conformance/model_api_path 各删 1-2 个专属死测试、
  prompts 专属测试文件整删。保留 `_patch_model_api_action_trace`
  （log_trim 防驱逐回归测试依赖，见 backlog #15）。
- 级联三波共 -45 函数/常量 + 全部孤儿 import（每波过借道检查）。
- 验证：全量 pytest 对比基线零新增失败（passed 减少数与被删测试数精确对账）；
  两入口 + hosted 全家 import 冒烟通过。

## 2026-07-18（第四批：清理收官——「仅测试供养」全量分诊，零删除）

### [DONE] 仓库清理第三轮·第四批（收官）

- 把第三批 memory_tools 的发现系统化成全量扫描：backend 顶层符号
  prod-corpus 零引用 + test-corpus 有引用 → 40 个候选，**全部逐个分诊后
  零删除**——proactive V2 全家是 flag-gated 在建功能、reset_cache/insert_user
  是测试基建、model_api 退役家族 8 个虽经 git 取证确认真死但测试引用散在
  conftest/worldbook/log_trim 等共享文件（删除=测试手术）。
  分诊清单落 `OPTIMIZATION_BACKLOG.md` #15，附复现方法。
- **清理宣告收敛**：文件级→顶层符号→常量/方法→test-only 模块→仅测试供养，
  五个维度全部扫到不动点；剩余项均需领域判断或 ops 确认，机械清理到此为止。

## 2026-07-18（第三批：test-only 死模块 + tools 内部死码）

### [DONE] 仓库清理第三轮·第三批

- **删 `model_api_runtime/memory_tools.py` + 其测试**：git 取证确认在本次
  清理开始前（95decf00）它就已无生产调用方——`execute_memory_tool` /
  `memory_tool_instruction_message` 只被自己的测试文件引用，测试在给死代码
  续命（hosted model_api 退役残骸的最后一块）。
- **tools 内部死符号**：chat_resident_consumer 5 个协议演化残留
  （_extract_openai_reply / _structured_reply_payload / _split_agent_result /
  update_proactive_state / _resident_perception_trend）+ e2e_encryption_test
  的 unb64；consumer 删后 py_compile + 全量 consumer 测试通过。
- **零发现的维度**（扫了但干净）：无人使用的 pytest fixture、backend 不可达
  代码（return 后语句）、requirements 未用依赖——唯一"疑似"的
  python-multipart 是 FastAPI 表单运行时依赖（07-07 有漏装致 500 前科），
  不可删。
- 验证：3436 passed（−4 = 被删测试文件的用例数，精确对账）/ 5 pre-existing
  failed，零新增。

### [DONE] 仓库清理第三轮·第二批：死常量、死方法、活文档状态复核

- **新扫描维度**（此前只扫了顶层函数/类）：模块级常量 + 类方法 + tests
  死 helper，同一标准（全仓含 docs-site/contracts 全文件类型 whole-word
  grep ≤1 即 def-only）迭代到不动点。
- 删 37 个死常量/方法/helper：hosted_runtime 的 TOOL_*/RUNTIME_ENGINE_*/
  BACKGROUND_METHOD 簇（wire 值已确认无其他发射方）、turn.py 死 env knob
  （MODEL_API_WEB_SEARCH_MAX_RESULTS/TIMEOUT、PROVIDER_REASONING_ENABLED、
  STATE_RECEIPT_MAX——对应 FEEDLING_* flag 本已无效）、memory_readside 死
  limit 簇、genesis checkpoint 死前缀/死状态、3 个死方法
  （InMemoryMetricsSinkV2.list_events / DeliveryDecisionV2.allow_push /
  PerceptionDifferV2.state_for）、tests/_clear_cursor 等。
- **有意保留**：perception/catalog.SCENE_HINTS（与 iOS Vision 分类器共享的
  canonical enum，跨仓契约文档，本仓 grep 不到≠死）；pytest_report_header
  等钩子与 Test* 类为框架按名调用，扫描已排除。
- **活文档失实修正**：OPTIMIZATION_BACKLOG 复核（#1/#4 补 ✅ 及 commit 证据，
  #3 标注部分完成——api_key 路径仍回环 whoami）；DEPLOYMENTS.md TEE Postgres
  段从「待开通」改为已开通（test+prod，指向 TEE_POSTGRES_SHADOW_PROVISIONING）。
- 验证：全量 pytest 对比基线零新增失败；两入口 + 全部触及包 import 冒烟通过；
  不碰 deploy 配置/alembic/路由（iOS 为独立仓，路由是公共 API 面）。

## 2026-07-18

### [DONE] 仓库清理第三轮：过时文档 + 符号级死码 + 全量 unused imports

- **文档**（21 删）：已落地/被推翻的一次性 spec 与事故文档
  （`INCIDENT_usr_f13f_2026-07-16`（诊断已被 07-17 取证推翻）、
  `DAU_DAILY_SNAPSHOT_2026-07-14`、`DATATRACK_..._2026-07-13`、
  `PROACTIVE_AUTONOMY_SWITCHES_2026-07-06`、`V1-迁移-测试清单`）；
  `docs/memory/` 整目录（07-13 清理漏网的 3 个"已被取代"存档）；
  13 个已 ship 的 superpowers plan（保留 `2026-07-07-tee-pg-phase0-1-infra`，
  被 `deploy/DEPLOYMENTS.md` runbook 按 Task 编号引用）。
- **backend 死码**（25 符号，全部 grep 证实仅剩 def 处一条引用）：
  `hosted_runtime` 三个死 dataclass、`hosted/turn` 7 个孤儿 wrapper、
  `memory/actions._memory_content_patch_action`（dispatcher 已 coerce 到
  supersede，见 conformance test）、`db.genesis_latest_done_job`/`genesis_get_output`、
  `dstack_tls.derive_key_only`（调用方随 MCP server 一起删了）、
  `context_memory_selection.memory_relevance_score` 死对、
  `proactive/runtime_v2.SingleFlightRegistryV2` 等。
- **unused imports 清扫**：autoflake 全 backend 扫（204 处，26 文件），未触碰
  `__init__.py`/alembic。⚠️ 教训：`identity/service.py` 的
  `RUNTIME_LABELS as _IDENTITY_RUNTIME_LABELS` 是故意 re-export（3 处消费方走
  `identity_service._IDENTITY_RUNTIME_LABELS` 模块属性访问，pyflakes 看不见），
  被误删导致 30 个测试红，已恢复并加注释说明；其余 203 处经别名感知扫描
  （`alias.attr` + 字符串形 monkeypatch）确认无借道引用。
- 审计结论：scripts/ tools/ tests/ deploy/ workflows 无可删项（上两轮清得干净）。
- **第二轮级联清扫**（全 backend 顶层符号引用扫描迭代到不动点，共 4 轮）：
  又删 41 个死符号——大头是 hosted model_api 退役残骸的整条死链
  （turn 的 state-plan/pending-confirmation/web-search 执行路径 →
  model_api_runtime 的 run_web_searches/web_search_duckduckgo/web_search_trace →
  hosted_runtime.background_execution_trace，这些在本轮之前就互为唯一调用方、
  整簇不可达）；另有 asgi/deps.require_store、enclave/readside 两个孤儿、
  observability_v2 两个死 Sink 类、genesis 三个死 helper、5 处 unused local
  （含 chat_send_core 每图白算一次的 base64 死计算）；bootstrap 提示词删掉
  已死的 FEEDLING_MCP_URL 指引。路由 handler（装饰器注册）已全部排除在扫描外。
- 验证：全量 pytest 对比基线——两轮清理后 3440 passed / 5 failed，失败全部为
  test 分支 pre-existing（memory_readside×3 + prod_runner_topology×2），零新增；
  基线里第 6 个红（consumer_whoami_key_guard）系时间敏感 flake，单跑稳定通过。

## 2026-07-18

### [FEAT] Notify Relay：自部署用户推送中继（后端 + iOS）

- **背景**：自部署用户的后端没有官方 APNs `.p8`（不能外发，用户来信索要），
  收不到任何推送。方案 = 官方后端开推送中继：App 在设置页向**官方**服务器匿名
  enroll device token 换取 `nrt_` 中继凭证，自部署后端携 `X-Relay-Token` 调
  `POST /v1/notify-relay/push` 由官方代发。类型 1=alert、2=LA update（灵动岛
  同体）、3=LA start、4=LA end；widget 无 silent push 链路，明确排除本期。
- **后端**：新包 `backend/notify_relay/`（routes/relay_core/ratelimit），
  迁移 0020 + TEE 0002 双表（`notify_relay_configs` 明文 auth_token —— 幂等
  返回需要，故意不 hash；`notify_relay_logs` IDENTITY 镜像走 OVERRIDING
  SYSTEM VALUE），reconciler.TABLES/_IDENTITY_TABLES 已注册。限流是
  per-worker 内存滑窗（register 10/h/IP、push 120/min/token、bad-auth
  30/min/IP，env 可调）。LA payload 复用点 = `live_activity.py` 抽出的纯函数
  `build_content_state`（原 `/v1/push/*` 行为字节不变）。接口B 透传目标
  token（LA token 每活动轮换，注册制跟不上——用户拍板）。
- **契约面**：OpenAPI 143→145 操作（register 匿名 security=[]，push 走新
  securityScheme `RelayTokenAuth`），`public.json` 已重生成；SELF_HOSTING.md
  新增 §10（4 个 curl 示例 + 隐私披露：log content 默认截 512 字符，
  `NOTIFY_RELAY_LOG_CONTENT_MAX=0` 可关）；docs-site self-hosting 页 +
  changelog Unreleased 已更；CORS allow_headers 加 `X-Relay-Token`。
- **iOS**：`NotifyRelayClient.swift`（NotifyRelayManager + Keychain 的
  NotifyRelayTokenStore，按环境分槽、本地不同步）；设置页新 `.notifyRelay`
  路由，入口仅 `storageMode == .selfHosted` 显示；⚠️ 请求显式打
  `FeedlingEnvironment.current.apiURL`——自部署模式 `baseURL` 指向用户后端。
- **验证**：`tests/test_notify_relay.py` 30 用例全绿；全量 3500 passed
  （5 红均 pre-existing：memory_readside×3 / prod_runner_topology×2）；
  OpenAPI 契约 12 绿；docs-site types/lint/build 绿。**未 commit 未部署**；
  test 环境端到端（真机 sandbox curl 四类型）待部署后做。
- **Codex review 两修**（同日）：P1 alert(type 1) 锁定注册设备——显式 token
  与注册 device_token 不一致返 400（否则泄露的中继 token+已知 APNs token 可借
  官方钥给他人设备推 alert；type 2-4 透传是有意设计不受影响）；P2 bad-auth
  限流前置——`peek`（只查不记账）在 `get_config` 查库**之前**短路超限 IP，
  枚举/DoS 不再每请求打一次 DB。测试 33 用例全绿（新增越权 400、超限跳过查库、
  peek 不烧合法配额三用例）。
- **Codex 复审再两修**（同日）：P1 限流身份改取 XFF **末跳**（首跳客户端可控，
  每请求伪造新 IP 即可绕过全部 per-IP 限流），`NOTIFY_RELAY_XFF_HOPS` env
  可调（默认 1=CVM ingress；0=直连部署忽略 XFF 用 socket 对端）；P2 TEE 影子
  表 `notify_relay_configs` **有意去掉 device_token UNIQUE**（主库保留）——
  换机顶替+镜像漏删场景下，reconciler 按 auth_token upsert 不再于 prune 前
  撞唯一约束，漏写可自愈（alembic_tee 0002 注释有完整推理）。新增 XFF 伪造
  ×2 + reconcile 顶替收敛 3 个用例，37 用例全绿。
- **多代理 code-review 十修**（同日，Codex 配额耗尽后转 workflow 高强度审查，
  10 条全 CONFIRMED）：①register 命中已注册设备**不再回显 auth_token**
  （device_token 非秘密，凭它换回别人的中继 token = 越权；只在新建/带有效
  auth_token 时返回，命中即 `already_enrolled`）；②disabled token 与已知 token
  的 DB 查询前置 per-token push 限流（peek 于 get_config 前），被撤销 token 不再
  无限触库；③register 并发竞态 UPDATE 命中 0 行时 fall-through 铸新而非
  `_register_body(None)` 崩 500；④APNS_KEY 存在但无效（jwt 抛异常）时收尾
  pending log 并返 503 而非 500 幽灵行；⑤user_id 限长 128 防匿名灌库；
  ⑥public.json 恢复被版本漂移误删的 ValidationError input/ctx（保 diff 干净）；
  ⑦_log_finish/_touch_last_used 镜像钉主库 RETURNING 值而非各库 now()（免污染
  TEE verify gate）；⑧CHANGELOG 恢复丢失的 07-17 日期标题；⑨LA aps 信封抽
  `build_live_activity_aps` 纯函数两侧共用（消除已分叉的形状/兜底）；⑩
  notify_relay_logs 加保留期清理（`NOTIFY_RELAY_LOG_RETENTION_DAYS` 默认 30，
  按 log_id 采样触发）。测试增至 49 用例全绿，OpenAPI/TEE/push 回归全绿。
- **第四轮 workflow 审查八修**（同日，8 条全 verified）：①**register Path A
  不再顶替他人 device**——上轮重写引入的回归：只证明"持有某 token"就删/改任意
  device_token 的行 = 可 DoS/劫持受害者；改为目标 device 被别的 token 占用时
  返 409 拒绝、绝不 evict；②disabled/撤销 token 不能再经 register 重绑
  （owns 检查加 `disabled=FALSE`）；③push 移除 bad-auth 前置 peek——它会让
  共用 NAT/egress IP 的**有效** token 被邻居的坏认证预算误 429；未知 token 改由
  失败路径 allow 记账限流（取舍：放弃"查库前短路未知 token"换取不误伤合法流量，
  已知 token 的 DB 仍由 push-limiter peek 保护）；④register 的 TEE 镜像移到主库
  commit **之后**（Path A 原在事务块内，主库 rollback 时影子会留脏写）；
  ⑤prune 用主库算的 cutoff 时间戳删两库，不再各库跑 now()（时钟偏移致 churn）；
  ⑥re-enroll 省略 apns_env 时 COALESCE 保留原值，不把 sandbox 翻成 production；
  ⑦OpenAPI `NotifyRelayRegisterResponse` 的 auth_token/apns_env 改为非必填 +
  加 already_enrolled（原 required 与不回显 token 的 200 响应矛盾）；⑧收尾写
  合并——`_log_finish` 一个连接一次 execute_many 同时写 log 完成态 + last_used，
  省掉每次 push 的独立 touch 连接/往返。附带把 ValidationError schema 固化进
  导出工具（防生成环境版本漂移反复增删 input/ctx）。测试增至 66 用例全绿。

## 2026-07-15

### [DONE] Pre becomes automatic Hosted Runtime V2 acceptance environment

- Added a strict Pre-only `v2_only` ownership policy. Backend startup backfills
  all existing active/tested/supported accounts before serving; fresh setup,
  successful tests, and route activation persist the same atomic
  `db_action_v2` + `v2` generation-fenced control. iOS testers no longer need a
  user id, admin flip, recovery drill, or runtime-service knowledge.
- Requests fail closed on policy mismatch and worker loss. The Pre deploy now
  requires the runner job and gates on a build-matched turn worker, capacity,
  Genesis heartbeat, and complete runtime-policy coverage.
- Hardened configuration lifetime races: V2 enablement revalidates and locks an
  active tested route inside the ownership transaction; setup, active-route
  changes, and active-key rotation generation-fence any pinned provider snapshot;
  deletion performs a second resident fence; failed/last routes fence current
  work; config-lock waiters are connection-bounded and deadline-visible; and
  `resident_only` rollback also discovers orphaned V2 controls with no route.
- Cutover recovery now chooses the newest unanswered message before consulting
  its durable terminal marker, so it cannot fall back to an older row and make
  the worker reread/rebill a terminally failed newer message. Gunicorn also
  closes the startup reconciliation pool before forking clean worker-local pools.
- Replaced startup/reconnect account normalization's stale whole-registry rewrite
  with per-user JSONB compare-and-swap, and made wake-bus handler registration
  idempotent. A concurrent fresh iOS signup can no longer be deleted by an older
  worker snapshot while the V2 runner is reconnecting.
- The resident supervisor stays deployed but has an empty eligible roster on
  Pre. `PRE_HOSTED_RUNTIME_POLICY=resident_only` is the explicit fleet rollback;
  test and production remain per-user rollout environments.
## 2026-07-17

### [FEAT] pi 路线终于能用用户 MCP 了（v2 spec §11 的后续项，欠了 4 天）

- **缺口**：`_user_mcp_cli_value` 只有 claude（`--mcp-config`）和 codex
  （`-c mcp_servers.*`）两个分支，pi 模板根本没有 `{mcp}` 占位符 → 对 pi 返回空。
  影响 **gemini / openrouter / openai_compatible 全部托管用户**（不含 deepseek，
  它 07-14 已回 claude driver）。iOS 的 MCP 设置页对任何路线都放行添加、无能力提示，
  用户白配一通。usr_6f5a 的「连了 ombre brain MCP 但 AI 看不到」就是它。
- **做法**：pi 官方无 MCP（README:491 明示），写 extension 桥
  （`tools/pi_mcp_bridge/`，零 npm 依赖手写 MCP client，与 `mcp_probe.py` 同协议同
  理由）读**已物化**的 `user-mcp.json`、把每个 MCP 工具注册成 pi 原生工具。
  **数据链路一行没改**——`user_mcp_materialize.py` 的 docstring 早就写了这个文件
  「doubles as the generic user-mcp.json」。
- **模板必须动**：`-t bash` → `-ne -xt read,edit,write`。`-t` 是 allowlist 且
  **对 extension 工具同样生效**（`pi --help` 明示；`agent-session.js:1867` 在工具进
  registry 前就过滤）——不换的话桥注册的工具会被静默丢弃，`setActiveTools()` 也救不回。
  `-ne` 补回 `-t bash` 原本提供的隔离（关掉 extension 自动发现，显式 `-e` 照常）。
  **对无 MCP 的 pi 用户行为不变**（active 集合仍是 `["bash"]`）。
- lane gating 白送：chat 回合注入 `-e <bridge>`，background 不注入 → 那个回合工具
  **根本不存在**，结构性满足 v2 spec §1「不静默消耗用户第三方额度」。
- CA 顺带修：`_user_mcp_ca_env`（已更名 `_user_mcp_child_env`）删掉 pi 的
  early-return——pi 与 claude 同为 Node 进程，`NODE_EXTRA_CA_CERTS` 对它天然正确。

### [LESSON] 一句过期事实被升级成错误结论，把一个到期的待办误销案了

- 这个洞存在 4 天，不是因为难，是因为**没人回头改一句话**：v2 spec §1（07-08）写
  「test 无 pi driver，本期不涉及」——**当时属实**；07-13 pi driver 合流 test
  （`1e01ef7e`），该句过期但没改；07-16 的 spec 读到它，写成「**路线已放弃**」；
  07-17 该结论被 `8cb9314b` 刻进代码注释 `# pi: route abandoned`。
  §11 那个「等 pi driver 合流就做」的待办在 07-13 到期，却被反向**误销案**。
  「本期不涉及」（排期）→「路线已放弃」（战略）这一步偷换，**从来没有人做过那个决定**。
- 同一种病不是孤例：起草新 spec 时据 `Dockerfile.agent-runner:42` 的注释把 deepseek
  写进 pi 影响面，经 `db.py:1800` / `agent_runtime_cutover.py:101` / `supervisor.py:714`
  三处交叉核对才纠正——那句注释是 07-14 改回 claude 后没跟进的。
- 因此新 spec §6 给**每条 pi 内部行为断言都标了源码行号证据 + 时效性声明**（全部绑定
  pi 0.80.3，升级必须重验，且**不得在未重验时被下游文档升级成更强表述**）。
- 已订正：v2 spec §1（标注过期而非删除——它当时是对的）、`chat_resident_consumer.py`
  的注释、锁死错误理由的那条测试断言、Dockerfile 的 deepseek。
  ⚠️ **遗留**：`2026-07-16-user-mcp-network-relaxation` 的 spec/plan 两处仍未订正——
  它们是 ca-fetch 那条线**尚未提交**的产物，只存在于主工作树，不在本分支。

### [DECISION] prod runner 拓扑闸降级为警告（用户拍板）

- `validate prod runner topology`（ec55ae18 今晨新增，要求 ≥2 台独立 prod
  runner 才允许任何 prod CVM 变更）在只有 1 台 runner 的现状下拦死了全部
  prod 部署（PR #84 合并即失败）。用户拍板先解除硬拦截。
- 改法：`deploy/check-prod-runner-topology.sh` 单 runner 时降级为
  `::warning::` + exit 0；设 `PROD_RUNNER_TOPOLOGY_ENFORCE=true` 可一键恢复
  硬闸。机制/job/needs 边均保留，第二台 runner 上线后应立即重新武装。
- ⚠️ 07-15 事故模式（部署窗口内唯一托管路径短暂消失）在恢复硬闸前依然存在。

### [DONE] runner 部署等待脱离 phala CLI 的 300s 硬顶

- `phala deploy --wait` 的就绪等待在 CLI 里写死 300s（dist/index.js
  `Xl(e,t=3e5)`），无 flag/env 可调；runner 镜像拉取+启动经常超过它，
  CI 在更新其实会成功的情况下按 CLI 超时报失败。
- 改法：test/prod 两个 runner 部署步骤去掉 `--wait`，deploy 后改跑新脚本
  `deploy/wait-cvm-ready.sh <cvm-id> [timeout]`（轮询 `phala cvms get --json`
  的 `status==running && progress 为空`，默认 900s，`CVM_READY_TIMEOUT_SEC`
  可配）。已对 idle 的 feedling-prod-runner-1 实测脚本判定正确。
- 主 CVM 两处 deploy 未动（未观察到超时）；如需同样处理照抄一行即可。

### [DONE] usr_f13f 事故①取证 + 信封 fpr 标签校验 + consumer 缓存钥加界

- 取证结论（推翻事故报告的诊断）：身份卡**没有**搁浅在旧钥——07-15 10:16:10
  既有 rewrap 自愈已把它修到当前钥（TEE 影子库有明文佐证，enclave 可解）；
  真凶是**写手用陈旧 whoami 缓存钥封新消息**（37/37 条消息落地后秒级被 iOS
  触发的 rewrap 修复 = 两天 rewrap 风暴）。详见 memory
  `usr-f13f-identity-decrypt-incident-misdiagnosis`。
- 修复 1：`build_envelope` 现在给所有信封写 `content_pk_fpr` 标签（此前只有
  rewrap 会写）；`/v1/chat/message`、`/v1/chat/response`（含 thinking 信封）
  对带标签但指纹 ≠ 当前注册钥的信封返回 409 `content_pk_fpr_mismatch`
  （已登记 API_ERRORS.md + 公开 OpenAPI/changelog）。无标签信封放行（旧客户端
  兼容）。附带收益：rewrap 对已是当前钥的新行能正确 skip，不再全量过 enclave。
- 修复 2：consumer `_refresh_whoami_for_encrypted_reply` 的 cached-keys 兜底
  加上限 `WHOAMI_STALE_KEYS_MAX_AGE_SEC`（默认 3600s，超龄拒绝封装、响亮跳过）；
  `post_reply` 收到 409 fpr 不匹配时强刷 whoami（无视 TTL）重封重试一次。
- Codex review 补漏 ①：`append_chat` 的字段白名单原本会把 `content_pk_fpr`
  丢掉（入库即失标签，rewrap 对聊天行的 skip 永远打不中）——已补
  `content_pk_fpr` / `thinking_content_pk_fpr` / `caption_content_pk_fpr`
  三处持久化（core/store.py 两层白名单 + chat_core thinking_extra）。
- Codex review 补漏 ②：TTL 快捷路径可绕过陈旧上限（把
  `WHOAMI_STALE_KEYS_MAX_AGE_SEC` 配得比 TTL 小时，年龄在两者之间的缓存钥
  会被 TTL 直接放行）——快捷路径现在同时要求 age < 上限（0 仍表示关闭上限）。
- 测试：`tests/test_chat_envelope_fpr_guard.py`（11）+
  `tests/test_consumer_whoami_key_guard.py`（6）全绿；全量 L1 3337 passed，
  5 个失败在干净 HEAD 上原样复现（历史遗留）；L2 e2e_encryption chat 往返过
  （memory/identity 段是脚本契约漂移的历史 400，与本改动无关）；OpenAPI 契约
  测试 12/12；docs-site build/lint/types 过。
- iOS 侧遗留（转客户端）：iOS 封信封尚未带 fpr 标签；`applyIdentity` 会把
  解密失败卡的 marker 推进 LiveActivity/widget；rewrap 成功后 throttle 重置
  导致每 poll 可再 fire。
- 未提交未部署；部署后按 TESTING.md 行 F 做 L3（重启 resident consumer 验证）。

## 2026-07-14

### [DECISION] deepseek 从 pi 驱动改回 claude（cc）驱动

- 驱动派生改回：`deepseek` → `claude`（Claude Code CLI，
  `ANTHROPIC_BASE_URL={base_url}/anthropic` 指向 deepseek 的 Anthropic 兼容层），
  撤销 07-13 pi consolidation 把 deepseek 归入 pi anthropic-messages 桥的部分
  （该桥未验证，07-14 usr_a7b0aba7 事故：胡言乱语 + 图片被注入 "(image omitted)"
  + 批量慢回）。gemini/openrouter/openai_compatible 维持 pi 不变。
- 改动三处 + 测试：`hosted/agent_runtime_cutover.py`（`_CLAUDE_PROVIDERS`
  加回 deepseek）、`db.list_agent_runtime_enabled_users` SQL CASE
  （`deepseek→claude`）、`agent_runtime/spawners.py`（恢复
  `_CLAUDE_COMPAT_BASE_URLS`/`_claude_anthropic_base_url`、deepseek 走
  thinking-claude 命令、删除 `_pi_models_json` 的 deepseek 分支）。
- 行为即 07-13 之前的 prod 状态（deepseek→claude 实测可用，见 2026-06-25 记录）。

### [DONE] backend 内存增长根因修复（arena 膨胀 + 三个 churn 源）

- **根因**（prod 实测取证）：backend worker RSS 无界增长（12h 到 2-3GB/worker）
  的本体是 glibc per-thread malloc arena 膨胀——每 worker ~60 个 64MiB arena 近乎
  全驻留、占 RSS 80%+，高水位只涨不还；不是 Python 对象泄漏。churn 饲料 =
  请求处理分配（40 线程池线程）+ 三个可修的放大器（下条）。
- **修复四件套**（全部 TDD，测试先行）：
  1. `accounts/registry.py` `_set_user_timezone`：值未变直接 return——此前 iOS
     app-presence 每 ~1min/设备重报同一时区，每次都触发 users 行 upsert + TEE
     mirror + 跨 worker 广播 → **4 个 worker 各 63 次/min 整表重载 556 用户**
     （pg_stat 实测 +75 全表扫/min）。对照实验证明重载 churn 本身不涨内存
     （单监听线程完美复用），但 DB/CPU/锁是真浪费。
  2. `admin/tee_sync_scheduler.py`：per-table 整表失败指数退避（2^n × interval，
     封顶 1h，成功清零）——此前 text-cursor 分隔符 NUL bug（07-14 已另修，
     `4ef7cd4`）让 memory_moments/world_book_entries 每 tick 必败重跑，把名义
     300s 的 tick 拖成 13-87 分钟连轴转（重拉 + enclave 重解密同一批 ~800 行）。
  3. `gunicorn_conf.py` + 两份 phala compose：`max_requests=2000/jitter=500/
     graceful_timeout=120`（worker 定期回收 = arena 归零的唯一可靠手段；graceful
     必须盖过 30s 长轮询）+ `MALLOC_ARENA_MAX=4`（arena 数量封顶）。
  4. `screen/ws.py`：帧 ingest 循环内每帧重验 key——账号删除后已建立的广播扩展
     WS 继续推帧，对不存在的用户反复写库撞 FK + 广播幽灵 store 重载（prod 实测
     usr_25ce… 53 次/10min）；key 失效即 4401 关闭。
- 排查全记录见 memory：backend-memory-growth-root-cause /
  tee-sync-nul-byte-stuck-loop / timezone-noop-persist-reload-storm。
- 止血提醒：prod 部署本批前，晚间活跃期 backend 容器仍需手动 `docker restart`
  （available < 1000M 即 OOM killer 红线）。

## 2026-07-13

### [DONE] pi driver consolidation on the multi-profile schema

- Re-homed pi driver consolidation onto test's `model_api_routes`/`model_api_credentials` schema (pi was greenfield on test).
- Final driver map: `anthropic`→`claude` (native Anthropic wire), `openai`→`codex` (native OpenAI Responses), `gemini`/`openrouter`/`openai_compatible`/`deepseek`→`pi` (native direct relay; deepseek via pi anthropic-messages @ `/anthropic` endpoint, text-only).
- Retired the in-CVM LiteLLM gateway entirely (module + env + venv install deleted); every provider now direct-native relay.
- Reasoning effort forwarded **natively** by pi (openrouter etc.), no gateway intermediary. **NOTE:** the `_PI_MODEL_REASONING_KEY` models.json field is a **PLACEHOLDER** pending a pre-spike on `pre` branch (verify openrouter reasoning returns a real chain with no litellm process).
- Discovery `list_agent_runtime_enabled_users()` derives pi via the `model_api_routes`/`model_api_credentials` JOIN with CASE fallback (ELSE→`pi`), unconditional (no `include_gateway` flag).
- Status: implemented on branch `feat/pi-on-multiprofile`, **NOT committed/deployed**; pre-spike on `pre` is the pre-prod validation gate before test deployment.

## 2026-07-11

### [DONE] Hosted Runtime V2 安全审计跟进进入 draft PR；用户切换仍 HOLD

- 以 `feat/hosted-runtime-v2` 的 `bfc8862` 为审计基线，提交 bounded follow-up
  `20c4b0b`，并开出以该 feature branch 为 base 的
  [draft PR #70](https://github.com/teleport-computer/feedling-mcp/pull/70)；没有合进
  `main`、没有部署、没有切任何用户。
- 工程师随后把 feature branch 推到 `0333bc4`，用“缺失/非法 mode = V2”做隐式全量
  切换。child branch 保留该提交历史并以 `f08fe5a` 显式 revert：只有明确
  `db_action_v2` 才进 V2；缺失/非法仍走 resident，mode 读取失败拒绝路由；resident
  discovery 的 DB 失败不再伪装成空 roster 而误杀全 fleet，admin/scheduler 列表也会
  枚举没有 runtime blob 的 active-route 用户。该 reconciliation slice **109 passed**；
  broader changed-surface 为 **680 passed / 1 xfailed / 3 个已在 engineer tree 复现的
  baseline failures**。
- 关闭本轮三个直接 blocker：chat 入队即有 queue deadline，pending/active 由独立
  reaper 终态化并显示错误；Anthropic/Gemini 的 AnyIO limiter 按 worker slots 扩容
  （native async 仍是后续）；rollout CLI 改用包含 planner rounds + responder 的
  loop-aware tokens/turn 口径。
- 同批补齐 mixed-version queue/lease 兼容、per-user 跨 lane 串行、late-input successor、
  strict chat durability、runtime/effect fences、worker identity/slot supervision、迁移图修复、
  隐私 scrub、SSRF/redirect/body cap 与 enclave-private memory search。验证：focused
  **385 passed**，Alembic 单 head `0024_v2_worker_capacity`，真实 PostgreSQL
  `0020→0024` 升级通过；共享 fixture gate 为 **574.0 tokens/turn、2.3333 calls/turn**。
- **仍是 NO-GO**：resident→V2 稳定 cursor/generation、transactional outbox + 幂等 effects、
  永久 hung call 的硬恢复、effect commit 原子 generation fence、tool-output prompt-injection
  trust boundary、summary coverage/retention invariant、保留 Genesis 的 live pool kill switch
  未完成。完整验收合同与工程师交接见
  [Hosted Runtime V2 audit handoff](HOSTED_RUNTIME_V2_AUDIT_HANDOFF_2026-07-11.md)，运维顺序见
  [rollout runbook](../deploy/HOSTED_RUNTIME_V2_ROLLOUT.md)。
- 存储词汇校正：conversation summary/trajectory 的产品要求是 storage-agnostic；当前外部
  RDS adapter 用 envelope encryption，目标 pg CVM 则在 LUKS2 FDE 盘上存 plaintext 并
  删除 envelope/decrypt/rewrap 层。模型在两种拓扑中都只在授权 CVM 内看到 plaintext。

## 2026-07-09

### [DONE] 用户 MCP 服务器（user_mcp）—— 配置分发模型

- iOS 设置页新增「远程 HTTP MCP server」配置（`name` + `url` + 自定义请求头），
  整体（url+headers）经 `core/envelope.py` 的共享信封路径加密落库，服务器不留
  明文；`name`/`enabled`/host hint/header 名（不含值）留明文供列表展示。write-only：
  iOS 不能读回明文，编辑即整体重传。
- 新增端点：`GET/POST /v1/mcp/servers`（列表/新增）、
  `PATCH/DELETE /v1/mcp/servers/{name}`（改/删）、
  `POST /v1/mcp/servers/{name}/test`（后端直接探测一次，SSRF 防护，不代理调用）、
  `GET /v1/mcp/envelopes`（consumer 拉信封解密用）。
- 下发复用现有 poll 通道：`backend/chat/poll_core.py` 的 `poll_context` 在
  `runtime_v2`/`client_release` 之外新增 user_mcp 配置 fingerprint 广告，托管和
  自跑 consumer 走同一份 `tools/chat_resident_consumer.py`，都靠长轮询
  `GET /v1/chat/poll` 感知变更，无需新通道。
- consumer 侧：fingerprint 变化 → 拉 `/v1/mcp/envelopes` → enclave 解密 →
  物化成 claude/codex 的原生 MCP 配置（claude 用 `.mcp.json`/`--mcp-config`，
  codex 0.142 用 `config.toml` 的 `[mcp_servers.<name>]`）。**仅聊天回合**可用，
  proactive/后台回合绝不可用——避免静默消耗用户第三方 API 额度。codex 后台回合
  对每个 enabled server 显式下发 `-c mcp_servers.<name>.enabled=false` 覆盖关闭
  （codex 对 `-c` 是深合并：`-c mcp_servers={}` 空父表是 no-op，无法禁用，只有
  逐 server `enabled=false` 才真生效）。
  自跑（VPS）agent 走标准化 `USER_MCP_FILE`（`{"mcpServers": {...}}`），只保证
  配置送达、生效 best-effort。
- 后端不做 MCP 代理层：只负责配置的加密存储、探测（`/test`）和下发广告，实际
  MCP 调用完全发生在 agent runtime 侧（claude/codex 原生 / 自跑 agent 自行加载）。
- 架构决策记录见 `docs/superpowers/specs/2026-07-08-user-mcp-servers-design.md`
  （v1「后端代理统一 MCP client」方案已废弃，改为本条的「配置分发」v2）。

## 2026-07-08

### [DONE] 通知设施 Phase C（四个生产者接入 + consumer 分类器扩容）

- **C1 genesis**：`backend/genesis/service.py::mark_failed`（蒸馏 job 整体
  失败）先过 `catalog.classify_upstream(error)` 分类，未命中时兜底
  `genesis_failed`；`apply_reducer_output` 统计记忆卡片丢弃数
  （dropped>0）时 emit `genesis_partial`（warning），`backend/genesis/plaintext.py`
  直传路径同样接线。两个 dedupe_key（`genesis:{job_id}` /
  `genesis:{job_id}:partial`）共享 `genesis:` 前缀，新一轮 run 开始时统一
  resolve 掉上一轮的失败/部分通知。
- **C2 history_import**：`backend/hosted/history_import.py` 导入失败落
  `import_failed`；job 卡在 queued/processing 超过阈值时的 stale 判定落
  `import_stale`（均 error）。
- **C3 memory**：`backend/proactive/capture_jobs.py` 新增退避统计入口，
  capture/migrate/dream 三条 lane（`capture_scheduler.py` /
  `dream_scheduler.py` 共用同一入口）在 streak ≥ 3
  （`_BACKOFF_NOTICE_STREAK`，前两次退避噪音价值低不打扰）时 emit
  `memory_backoff`（warning），lane 恢复 completed 时按 `memory_backoff:{lane}`
  精确 resolve，不跨 lane 清。
- **C4 runner/supervisor**：`backend/agent_runtime/supervisor.py` 新增
  per-(user_id, error_class) 60s 去抖（`RUNNER_NOTICE_MIN_INTERVAL_SEC`，
  默认 60s）+ never-raise 的 `_emit_runner_notice`/`_resolve_runner_notice`。
  子进程拉起失败接 `runner_spawn_failed`；provider key 解密失败接
  `runner_key_decrypt_failed`；runtime-token 刷新失败但进程仍存活接
  `runner_degraded`（warning，唯一能 resolve 它的路径是 token 刷新恢复，
  spawn 成功不清）。顺带补了 tick 里两处此前裸奔（无 try/except）的
  spawn_fn 调用点，避免单用户异常连坐同批用户。
- **消费者分类器扩容**：`backend/notices/catalog.py` 新增
  `provider_incompatible`/`context_overflow`/`content_filtered` 3 类 chat
  上游分类（与 `tools/chat_resident_consumer.py` 的 `_ERROR_CLASS_RULES` 同源，
  `tests/test_catalog_consumer_parity.py` 锁一致）；新增
  `catalog.classify_upstream()` 给 genesis 等 backend-only 生产者复用同一套
  上游错误文本分类规则，未命中返回空串，由调用方兜底到各自专属
  error_class。
- **文档**：`docs/API_ERRORS.md` 新增「通知中心 error_class」一节，登记
  上述 11 个新类的 blame/severity/触发场景——明确标注这是 `GET /v1/notices`
  的 error_class 命名空间，与上面的 HTTP `{"error": slug}` 契约表不是一回事。
- **部署注意**：C4 改的 `backend/agent_runtime/supervisor.py` 跑在 **runner
  镜像**里（不是 backend 镜像）；本条改动涉及 `notices.emit`/`catalog` 的新
  接口，**backend 与 runner 必须同批部署**，否则旧 backend 镜像里没有对应
  接口会炸。
- 全量基线：Phase C 前 2370 passed / 5 pre-existing failed → Phase C 后
  `pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py`
  跑出 **2398 passed / 4 skipped / 9 xfailed / 5 failed**（净新增 28 个通过
  测试）；5 个失败逐一核对与 Phase C 前完全同一批（`test_chat_route_debug_trace.py`
  ×3 + `test_debug_trace_event_route.py::test_emit_event_records` +
  `test_memory_capture_trace.py::test_enqueue_duplicate_capture_key_does_not_emit_queued_event`），
  未引入新回归。
- 状态：**未部署、未 commit**（worktree `feedling-mcp-error-contract`，分支
  `feat/error-contract`）。

### [DONE] 通知设施 Phase B（backend/notices/ 包 + GET /v1/notices）

- 新 `backend/notices/` 包（emit / resolve / list_notices）：系统错误的可回溯
  通知面，明文存储于既有 `user_logs` 表的 `user_notices` stream（不建新表），
  `item_key=dedupe_key` 做 upsert 去重；`emit`/`resolve` 绝不抛出（观测性设施
  不得拖垮主流程）；`list_notices` 读侧额外按 `dedupe_key` 去重保留最新一条，
  兑现契约「同 key 始终只有一条」，同时自愈 emit 读-改非原子 / DB 瞬时读失败
  可能留下的重复未 resolved 行。
- 新增 `GET /v1/notices` 快照读端点。
- B4 场景字段已接线：provider `public_config.last_test_error` + onboarding
  `model_api_test` step 的 `last_test_error`。
- 范围：Task 1-3 完成；B3（chat 扇出门控）延后到
  `feat/upstream-error-surfacing` 合入 main 之后再做。
- 状态：**未部署、未 commit**（worktree `feedling-mcp-error-contract`，分支
  `feat/error-contract`）。全分支终审已过（结论：可合并）。

### [DONE] 统一错误信封 Phase A（api_error() helper + request_id 链路）

- 新 `api_error()` helper（`backend/asgi/responses.py`）统一拼装
  `{"error", "blame", "detail", "request_id"?}` 信封；`request_id` 生成/透传
  链路打通：中间件生成 → 响应头 `X-Request-Id` 回带 → 日志 `rid` 字段 →
  `internal_error`（500 兜底）body 必带同一个 id（其余 5xx 尽力而为，见
  `docs/FRONTEND_ERROR_CONTRACT.md` §三）。
- `RequestValidationError` 统一重塑成 400 `invalid_payload`（`detail` 带
  `[{loc,msg}]`，替换掉裸 FastAPI 校验报文）。
- 6 处自由文本错误收敛成 slug + detail（chat_core.py×3、memory_core.py×2、
  actions.py×1）。
- 新增 `docs/API_ERRORS.md`（全量 slug 契约表）+ 守卫测试锁关键 slug 不漂移；
  `CONTRIBUTING.md` 新增 §7「错误返回纪律」（原 §7 不变量顺延为 §8、原 §8 PR
  自查清单顺延为 §9）。
- 状态：**未部署、未 commit**（worktree
  `feedling-mcp-error-contract`，分支 `feat/error-contract`）。全分支终审
  已过（结论：可合并），本条同时记录终审后的一批文档级修订（措辞/slug 名/
  交叉引用号，不改后端代码、不改测试断言语义）。

## 2026-07-06

### [DONE] 仓库清理：陈旧文档删除 + ASGI 迁移收尾（§13.2/13.3 执行完毕）

四轮探查 + 分批执行的大扫除。三块：

- **文档**：删 34 份陈旧文档——docs/ 顶层 6 份与 docs/memory/ 逐字节重复的旧
  副本、`docs/generated/` 快照、18 份已 ship 的一次性时点文档（V1 测试系列、
  WAKE_INTERVAL、AFULL_PLAN、BACKEND_ASGI_ROUTE_MATRIX 等）、docs/superpowers/
  已落地的 plans/specs 10 份（含 backend-asgi-migration-plan，其 §13 剩余项本次
  执行完毕）。恢复 2 份仍被 `IO-v1-下一期-TODO.md` 未勾选事项引用的 V1 清单。
  所有被删文件的悬空引用已清（指向 git 历史）。长期文档同步去 Flask 时代描述：
  README / PROJECT_OVERVIEW / CONTENT_ENCRYPTION_INTERACTION_CURRENT / MEMORY /
  AUDIT / DEPLOYMENTS / SELF_HOSTING / CONTRIBUTING（MEMORY.md 头部加了
  「行号基于拆分前单体」的显著提示；DEPLOYMENTS 现状表去掉已下线的 MCP 行）。
- **死代码**：删 `tools/worldbook_e2e.py`（零引用）、
  `deploy/docker-compose.asgi-parallel.yaml`（迁移期 :5005 工装，cutover 已完成）、
  `backend/proactive/eval_v2.py`（+其 2 个测试；observability_v2 的 4 个生产覆盖
  保留）、`bootstrap/gates.py` 的 DEPRECATED `_gate_required_for_missing_tabs`、
  requirements 里的 Pillow（全仓无 import PIL；lock 已用 uv 重生成）。
- **ASGI 迁移收尾（migration plan §13.2/§13.3/§16）**：61 个测试文件从
  `import app as appmod` 重写为直接驱动领域包 + `asgi_test_client.make_client()`
  （新增 conftest `backend_env`/`client` fixture；make_client 镜像 lifespan 的
  wake-hook 与 envelope 接线）；子进程/CI 入口 `python backend/app.py` →
  新 `backend/serve_dev.py`（5 处 + ci.yml）；**删除 `backend/app.py`**（803 行
  facade + 符号回灌循环）、`hosted/setup_routes.py`（13 行空壳）、
  `hosted/chat_routes.py`（Flask 时代 native tool loop，生产零引用，连带
  test_hosted_memory_tool_loop.py 与 2 处测试引用；AUTOSEED_SCRUB_FLAGS 的
  字符串条目保留——那是存量档案清洗清单，与代码无关）。
  守护换代：test_asgi_app_import_guard →
  `tests/test_no_app_py_regression.py`（app.py 不得重生 + 测试不得再 import 旧
  facade，覆盖 `import backend.app` 变体）。
- **⚠️ 生产行为变化（有意，修 cutover 遗留 bug）**：app.py:756-762 的 3 处装配
  注入（`push_live_activity.load_identity`、admin data-track 的
  `_latest_history_import_job`/`_onboarding_validation_payload`）在 ASGI 切换时
  漏接，生产上 Live Activity identity 与 admin 数据面板一直是 stub 空值；现已
  接进 `asgi_app.py` 末尾装配段。上线后这两处从空值变有值属修复生效。
- 验证：全量 pytest 对基线零新增失败（5 个 pre-existing 失败与本次无关）；
  `import app` 全形态 grep 归零；路由面与 07-03 快照逐条 diff 对账——本次清理
  零路由增删（139 vs 133 = 方法合并行拆分 +5、cutover 已删的 static -1、
  同期 WIP 新增 debug 路由 +2）；requirements.lock diff 仅 -pillow。

### [DONE] enclave ASGI 迁移审查修复（5 个正确性回归 + 4 项清理）

对 3687f98（enclave Flask→FastAPI）做多智能体行为等价性审查，确认并修复
5 个并发模型/HTTP parity 回归（全部 TDD，先红后绿）：

- **singleflight 失败广播**（`enclave/auth.py`）：leader 的瞬时失败/
  CancelledError 不再扇出给全部同凭证等待者；等待者收哨兵后各自独立重试
  （旧线程版 per-key 锁语义），CancelledError 不再逃出路由层变 500。
- **CPU 重活离事件循环**：大 payload 的 `json.dumps`（chat/history、
  frames/decrypt 的 ~470KB image_b64）经新 `routes/_json.py` 在线程池渲染；
  gzip 中间件的 `gzip.compress` 同样 to_thread——防止图片重请求把
  /healthz 队头阻塞成网关 502。
- **回环连接池排队**（`enclave/backend_client.py`）：`Timeout(15, pool=None)`
  ——池满排队等空位（旧 32 gthread 准入闸语义），不再 >100 并发时 15s 后
  整批 PoolTimeout→502。
- **Range parity**（`routes/frames.py`）：畸形 Range（如 `bytes=5--3`）按
  RFC 7233 忽略回 200 全量，不再 416（byte-pos 按 1*DIGIT 显式校验）。
- **HEAD parity**：/v1/chat/history、/v1/memory/list、/v1/identity/get 显式
  挂 HEAD（FastAPI 不像 Flask 自动加），405→200。

清理：`routes/_body.py` 改包装主 backend `asgi.http.read_json_silent`
（收敛 +json 门槛漂移、继承 ClientDisconnect 修复）；新 `routes/_errors.py`
收敛 15 处复制的 httpx→401/502 与 content_sk→503 映射块；provider_client
的 openai-compat 编解码抽成 sync/async 共享纯函数（payload 构造/reasoning
降级判定/响应解析单实现）；chat/history 的 memory/list 拉取与解密并行
（省一次串行 RTT）；asgi_worker 的 uvicorn TLS monkeypatch 从 import 副作用
收进 `EnclaveUvicornWorker.init_process`。

测试：enclave 相关 192 全绿（含 20+ 新回归测试）；全量 2266 passed，
6 个失败是 debug_trace/capture_trace 的 pre-existing 环境问题（测试库连接，
与本批改动无 import 交集）。未提交未部署。

### [DONE] 托管回合上游报错透出

托管回合失败时不再只是静默重试/兜底回复——consumer 错误分类器
（`classify_agent_error`）把失败归类到 error_class + 责任方（system /
user_provider），然后：

- 发一条 `role="system"` 聊天通知（前台必发；后台同 error_class 每
  `SYSTEM_NOTICE_DEBOUNCE_SEC`（默认 6h）一条，任一成功回合清零）；
- `POST /v1/model_api/runtime_error` 写设置页 `last_runtime_error`，
  成功回合清空。
- `SEND_FALLBACK_ON_AGENT_ERROR` 的 `FALLBACK_REPLY` 兜底文案保留，不冲突。
- iOS 端渲染这条 system 通知是后续独立仓任务。
- spec 见 `docs/superpowers/specs/2026-07-06-upstream-error-surfacing-design.md`。

最终整体审查修了 3 条 Important + 1 条 Minor：
- 去抖用 `dict.get(k, 0.0)` 配 `time.monotonic()`（自开机秒数）在
  uptime < debounce 窗口时把「从未发过」误判成「刚发过」，导致 CVM
  刚启动时首个后台通知被假抑制——改成 `get(k)` 为 `None` 时直接放行。
- respawn 后新进程 `_runtime_error_reported` 从 `False` 起步，成功回合
  永不触发清空请求，respawn 前留下的滞留错误永久卡在设置页——改成
  每进程以 `True` 起步，首个成功回合无条件清一次（代价一次 HTTP）。
- `_clean_messages_for_proactive_context` 没有 role 过滤，system 通知
  会混入前台连续性上下文和 proactive 上下文，agent 把「⚠️ 额度不足」当成
  自己说过的话——补上 role=="system" 过滤，写法对齐 `_capture_live_history`
  的白名单方式。
- CHANGELOG 记录格式微调（本条）。

测试：`tests/test_consumer_error_classify.py` 新增去抖首发不假抑制、
respawn 后成功回合清空、proactive 上下文过滤 system 通知三个用例。

### [DONE] Proactive 日报口径修复 + memory-maintenance 失败退避

排查「日报成功率 3%」：真因不是投递管线坏，而是 memory-maintenance
（capture/dream/migrate）jobs 在坏钥用户身上无退避重试（prod 实测 ~75s 一次、
40 用户 2 天 7412 条 failed），灌满日报的 failed 分母；真实 wake lane 成功率
~55%。爆发点 = 7/5 单 runner 接管 102 用户：此前这些用户的 job 静默积压在
pending（1200-1400/天），切换后被消费、坏钥用户变成大声失败。

- **日报口径**（`db.admin_data_track_proactive_daily` + `admin/data_track.py`）：
  成功率只算 wake lane，且 completed（醒了、正常决策、只是没发消息——
  sleep/纯动作）算成功：(delivered+completed)/(delivered+completed+failed)，
  口径衡量「系统是否健康」而非「醒了的里面有多少真正送达」（拍板
  2026-07-06，新口径下 07-06 约 78%）；failed 只含 status='failed'；gate
  拒绝的 skipped 单独计数；maintenance 单独成列（表格新增 完成 / Skipped /
  维护(失败) 列）；「心跳」列分类器补上现网 kind `presence`（旧 SQL 只匹配
  heartbeat*，恒 0；与并行 commit 1e30c39 对 db.py 的最小修复同源合一，
  保留其 `admin_events_overview` 的 presence 修复）。
- **失败退避**（`proactive/capture_jobs.py` 新增 `failure_backoff_sec`/
  `in_failure_backoff`，capture/dream/migrate 三条 lane 接入）：terminal=failed
  后同一窗口指数退避（base 600s × 2^(streak-1)，封顶 6h；
  `FEEDLING_MAINTENANCE_FAIL_BACKOFF_{BASE,MAX}_SEC` 可调），completed 重置
  streak，debug 面板 force 绕过；migrate 终态此前无人记录，补了
  `record_migrate_job_status` + `proactive_core.job_status` 接线。
  Codex review 后加固：`job_status` 只在状态真正转变时才调 recorder——
  consumer 重复上报同一终态 failed 不再重复累 streak/无故翻倍退避。
- 测试：`tests/test_proactive_daily_report.py`（SQL lane 拆分/payload/分类器/
  渲染）+ `tests/test_capture_failure_backoff.py`（三 lane 退避、翻倍、重置、
  force 绕过、route 接线）。`test_memory_capture_trace.py::
  test_enqueue_duplicate_capture_key_does_not_emit_queued_event` 在本改动前
  已失败（pre-existing，与本次无关）。
- 未做（后续方向）：坏钥用户的 provider 级熔断（连续 401/403/余额不足时
  暂停 maintenance lane）。

## 2026-07-04

### [DONE] enclave Flask→FastAPI/asyncio 迁移，全仓 flask 依赖清零

`backend/enclave_app.py`（2333 行 Flask 单文件，全仓最后一个 flask 使用方）
迁移为模块化 `backend/enclave/` 包 + FastAPI/asyncio，16 个 task 完成
（主 backend 的同类迁移已在 PR #44 合入，见 992d908；enclave 是收尾）。

- **模块划分**：`config/keys/attestation/state/envelope/auth/backend_client/
  readside/visual` + `routes/{health,envelope,memory,worldbook,chat,identity,
  frames}` + `asgi_worker.py`（enclave 专用 UvicornWorker）+ `serving.py`（内嵌
  gunicorn 组装、TLS PEM 材料化）；`enclave_app.py` 收缩成 ~30 行薄入口，
  compose 命令 `python -u backend/enclave_app.py` 不变（compose_hash 不变）。
- **混合并发模型**：11 条路由全 `async def`；enclave→backend 回环换成进程级
  `httpx.AsyncClient`；解密批处理按请求整批下放 `anyio.to_thread.run_sync`
  （libsodium/cryptography 释放 GIL，真并行，事件循环不被 CPU 工作阻塞）；
  whoami 缓存的 singleflight 从 `threading.Lock` 改 per-key `asyncio.Future`；
  `/v1/envelope/decrypt` 保持"每次实时解析、绝不走缓存"不变。
- **手工 Range/ETag**：`/image` 路由手写替代 Flask `send_file(conditional=True)`
  ——单区间 206 + `Content-Range`、非法区间 416、`ETag`/`If-None-Match` → 304，
  是 dstack-gateway 每连接 ~1Mbps 限速下并行分块拉图的关键能力，专项测试覆盖。
  flask-compress gzip 换成 Starlette `GZipMiddleware`（阈值对齐 500 字节）。
- **`FEEDLING_ENCLAVE_THREADS` 语义变更**：从 gthread worker 线程数变为 anyio
  解密线程池容量（CapacityLimiter），默认值仍是 32，环境变量名不变，compose
  不用动。`FEEDLING_ENCLAVE_WORKERS`（prod=2）语义不变。
- **两处有意行为偏差**（spec 明确标注，不是回归）：
  1. `OPTIONS` 请求：Flask 自动 200 → 新栈 FastAPI 路由表行为，未注册的
     OPTIONS 回 405 + `Allow`（`test_enclave_routes_health.py`）；
  2. `/v1/envelope/decrypt` 非对象 body：旧代码曾 500 → 新代码归一为 400
     （`envelope.py` 内联注释 "有意偏差 #2"）。
  错误码/错误字符串（含两套并存拼法 `missing_api_key`/`missing api_key` 等）
  逐字保留，未借机统一命名。
- **依赖清零**：`backend/requirements.txt` 删除 `flask>=3.0.0` /
  `flask-compress>=1.14` 两行及其头部说明段；`uv pip compile` 重新生成
  `requirements.lock`，diff 只消失 flask 系传递闭包八个包（flask/
  flask-compress/itsdangerous/blinker/jinja2/werkzeug/brotli/backports-zstd，
  后两个是 flask-compress 的压缩算法依赖），无关依赖零变化。新增
  `tests/test_no_flask_anywhere.py` 守卫（grep 全 backend 无 `import flask`
  + 逐个 import 11 个 `enclave.*` 模块断言 `"flask" not in sys.modules`），
  锁死"全仓无 flask"不变量。
- **验证**：全量测试 2125 passed（较主迁移后基线 2037 净增 88，全部来自本次
  新增的 enclave 单测），4 skipped，9 xfailed；已知非本次引入的 2 个失败
  （`tests/test_data_track.py` 两个 fast-validation 断言，未改动过、与本次
  迁移无关）与 `tests/test_api.py` 收集错误（需 `:5001` 活服务，非本次回归）
  按既有基线原样保留，无新增失败。`docker-compose.memory-sandbox.yaml` 冒烟
  通过：`up -d --build` 后 backend + enclave 均 healthy，`curl /healthz` →
  `{"ok":true,"ready":true}`，`/attestation` 正常返回 quote/measurements，
  enclave 日志确认跑在 `enclave.asgi_worker.EnclaveUvicornWorker` 上。
- **test CVM 待验收**（不属于本次范围，spec §5/§8，由用户驱动上线后跑）：
  TLS 钉扎三条硬验收——① `openssl s_client` 取实际 served leaf cert，
  sha256(DER) 与 attestation 指纹比对必须相等；② iOS 审计卡实测通过；
  ③ 最低 TLS 版本 ≥1.2（握手对 TLS1.1 必须失败）。理论上钉扎不破（同一份
  PEM 材料化路径），但 uvicorn/gunicorn 构建 SSLContext 与现有自定义
  `ssl_context` hook 的协商细节可能有出入，需真实 TDX 环境验证。另需在
  test CVM 上过一遍 chat/memory/identity/frames e2e、Range 并行分块拉图、
  runtime-token 与 api-key 双路径。
- **影响文档**：`docs/superpowers/specs/2026-07-04-enclave-asgi-migration-design.md`
  是本次设计的 spec 源文档。本条目工作树未 commit（worktree
  `enclave-asgi-migration`，基于 test 分支），由用户在验收后提交。

### [DONE] 丢回合重投递兜底：consumer respawn 窗口不再永久丢用户消息（未 commit / 未部署）

- **背景**：prod ASGI 切换当日全量巡检发现 3 条 model_api 用户消息永久无回复。根因不在
  HTTP 层：supervisor 一检测到 config changed（model_api/setup、driver、preferences 写入都算）
  就 signal 15 respawn consumer，而 consumer 重启后游标从 checkpoint/最新历史 ts 播种，
  `since` 严格大于 ⇒ 重启前落库、未回复的消息被永久跳过——claim CAS 机制再也看不到它。
- **修**（server 为主）：`chat/service._pending_chat_messages_for_poll` 加重投递兜底——
  `ts <= since` 的 user 消息在同时满足「未回复 + claim 空/过期 + 在
  `FEEDLING_CHAT_REDELIVERY_WINDOW_SEC`（默认 3600s，0=关）窗口内 + 在未回复尾巴上
  （其后没有已回复的可见 user 消息，见 `_redelivery_floor`；verify_ping 不算对话也不重投）」
  时仍可投递。**backstop claim 走 DB CAS 严格模式** `db.chat_try_claim_reply(redelivery=True)`——
  两轮 Codex review 抓出的多 worker 缓存问题都封在这一条 SQL 里：① 拒绝**任何**未过期
  claim（含自己的；不同于正常路径的幂等自刷新）——防止把进行中的重投回合再发给它的
  claimer 跑重复回合；② `NOT EXISTS` 在 claim 时刻权威判定 supersede 尾巴——父消息
  reply_status 元数据更新不跨 worker 广播，缓存侧 `_redelivery_floor` 可能漏掉「更新的
  消息已被回复」，没有这条乱序迟到回复仍可能发生。缓存侧检查降级为快速预过滤
  （prod 跑 4 worker，缓存不可信）。重试节奏 = claim TTL：`CHAT_POLL_CLAIM_TTL_SEC`
  默认 120→600——重投递生效后 claim 过期意味着重复回合（双烧 provider，409 只挡双落库），
  TTL 必须盖过最长正常回合。
- **批量恢复走滚动**（第三轮 Codex review）：per-poll 重投预算
  `FEEDLING_CHAT_REDELIVERY_BATCH_MAX`（默认 5）——consumer 逐条跑回合（30-90s/条），
  不设预算的话大批 claim 的尾部会在处理到之前过期（重复回合）+ 超出解密拉取量；
  超预算的消息**不 claim**、下次 poll 立即接上（滚动恢复，不是等 TTL）。预算只管
  backstop，`ts > since` 的活对话永不受限。
- **配套**（consumer 两处）：`tools/chat_resident_consumer.py` 解密窗口改
  `_poll_decrypt_since(last_ts, poll_messages)`——重投的消息 ts 在游标之前，原
  `since=last_ts` 取不到明文会走 wedge-skip 白烧 claim；现在窗口拉回到批次最旧消息之前，
  且拉取量 `_poll_decrypt_limit` 按批次放大（2×批次+20，下限 50）保证整批可解密。
- 测试：`tests/test_chat_poll_redelivery.py`（16 项，含 supersede/窗口/TTL/排序/陈旧缓存
  ×2/预算滚动×2 语义）+ `tests/test_consumer_decrypt_since.py`（6 项）；全量回归通过。
- CI 适配：`tests/test_api.py` 长轮询超时用例原本留着未回复的用户消息（第 3/5/6 节的
  openclaw 回复不带 `reply_to_message_id`）——兜底生效后这些消息被立即重投、poll 不再
  park（CI #687 三红）。改为真实 consumer 行为（回复都挂 reply_to），本地按 CI 同款
  方式起服务全量验证通过。
- 部署注意：backend 镜像 + runner 镜像都要更新（consumer 走自身 self-update 亦可）；
  乱序回复不可能出现——重投只发生在「其后无任何已回复消息」的尾巴上。

### [DONE] 自定义陪伴频率 wake_interval_sec + 心跳激活门（三层，已 ship）

两件事一起落地（Seven 主导）。**① 自定义陪伴频率**:用户在设置页选「陪伴频率」7 档
(15min–12h,**默认 2h**),per-user `wake_interval_sec`(clamp `[900,43200]`),consumer 即时
生效、无需重启。为什么:原写死全局 30min 太勤/费 token——Seven 拍默认改 2h、最小档 15min(去 10min);
硬地板同步提到 900,防客户端绕 UI。**② 心跳激活门**:**首次成功聊天前,所有自发主动唤醒
(heartbeat/photo_added/arrived_at_anchor/unlock_after_absence/screen_watch/introduction)在
enqueue 前拦掉、零 token**;首聊成功后自然开启。为什么:用户刚填 API key、卡 onboarding、根本没和
AI 说过话时就开心跳纯烧 token、不符预期(Seven 要求加门)。

- **后端(Codex)**:`core/store.py` 加 `wake_interval_sec`(常量+`normalize_*`+F1 keep-old-on-save)
  与 `first_chat_ok_at` flag(幂等、不进 save 白名单防伪造激活);`proactive/gate.py`
  `_build_proactive_v2_wake_decision` decision 带 `wake_interval_sec`,且**未激活 && 非 manual →
  `activation_pending` 不 enqueue**(优先于原 wake_control);`perception/service.py` 4 条 direct-wake
  旁路(`_maybe_wake`/`_fire_wake`/`_submit_wake_event_v2_compat`/`_fire_wake_event_v2`)同拦;
  `agent_runtime/supervisor.py` post-respawn introduction 也拦;`chat/routes.py` **flip 点**=
  `/v1/chat/response` 成功回复 `role=user & source=model_api` 消息时 `mark_first_chat_ok()`。
- **consumer(CC)**:`_proactive_tick_interval_for_broadcast_state` 加 per-user 入参,读
  `decision.wake_interval_sec` + clamp `[900,43200]`,env fallback 对齐 7200;调度点 L6271 接线。
- **iOS(CC)**:设置页 `proactiveCard` 7 档 Menu 选择器(默认高亮 2h、ambient 关置灰)+
  `ProactiveStateResponse.wake_interval_sec` + `updateProactiveSwitch(wakeIntervalSec:)` + 双语文案。
- **验证**:全量 PG e2e **1462 passed**(10 个 pre-existing 非本次,已用清洁 origin/test worktree 隔离);
  consumer 189 passed;iOS `xcodebuild`(iphonesimulator) **BUILD SUCCEEDED**。双向 review(CC 审后端 /
  Codex 审 consumer)。e2e 中揪出并修:F1 introduction 旁路未拦、11 个 perception 测试漏更新激活。
- **commit**:test `0bc9e7d`、iOS main `758c823`。默认 30min→2h、最小 10min→15min。
- **影响文档**:`docs/WAKE_INTERVAL_CUSTOM_PLAN_2026-06-29.md` 整合成 SHIPPED 最终版(含激活门)。

### [BLOCKER→FIXED] claude 有 Read 授权仍"没权限读图"——非交互缺 permission-mode → 幻觉

单 runner + wedge 修好后,claude(sonnet-4-5)发图**仍"我需要权限才能读取这张图片"**。
本地用部署完全同款命令 + claude-code 2.1.195 复现并二分定位:**即便 `Read({home}/images/**)`
在 `--allowed-tools` 和 settings.json 里都有**,`claude -p`(尤其 `--output-format stream-json`
的 thinking 路径)在**非交互**下仍**拒绝自己的 Read**——allow 规则被当"提示",默认 permission
模式无交互审批者时对文件读 auto-deny,vision 模型于是**瞎编**(实测回过"棒棒糖小熊",探针图实为
蓝底黄框 MANGO-7391)。二分结论:元凶是缺 permission-mode,不是授权缺失;`--allowed-tools` 单独
(无 settings.json)反而能读,加了 settings.json 的 `permissions.allow` 但没 `defaultMode` 才触发拒绝。

修:两个 claude 命令 builder 都加 **`--permission-mode acceptEdits`**(`spawners.py`,
`_CLAUDE_PERMISSION_FLAG`),外加 settings.json 的 `permissions.defaultMode=acceptEdits`。这是能让
预授权 allowlist 在非交互下被认可的**最小权限**做法,不用 `--dangerously-skip-permissions`
那种 codex 式全 bypass;Bash 仍限定 io_cli。实测:同款命令现在正确读出图(暗号+配色),不再幻觉。
1512 passed。教训:claude headless 读文件要显式给 permission-mode,光有 allow 规则/settings 不够。

### [BLOCKER→FIXED] 测试用户切 claude 后"没响应" — checkpoint wedge + 测试 runner 改单节点

修完 thinking-claude Read 后,测试用户切 claude 发图**仍没响应**(连报错都没有)。SSH 进测试
runner CVM 看日志坐实:consumer 的聊天 checkpoint 卡在 12:11(`checkpoint.json last_ts`),日志
反复刷 `poll returned claimed messages but decrypt history did not include those ids; keeping
checkpoint for retry` —— poll 认领了消息 id,但 `get_decrypted_history(since=cursor)` 返回里不含这些
id(解不出 / 或该消息 ts 正好等于 exclusive `since` 边界取不到)→ `_filter_messages_to_poll_ids`
得空 → **无限重试、cursor 永不前进** → 16:15/16:18 等新消息全被堵。**跟看图修复无关**(该用户
AGENT_CLI_CMD 已带 Read,已验证)。

结构性诱因:测试 runner CVM `feedling-io-agents-test` 跑**两个** agent-runner 容器(0/1),**各自
独立数据卷**(`feedling_agent_runtime_r0/r1`);per-user 聊天 checkpoint 存在卷里、**不在 lease 行**,
所以 lease 从一个 runner 漂到另一个时,会从另一卷的**陈旧 checkpoint** 重放并 wedge。线上 runner
(`docker-compose.phala.prod.runner.yaml`)本就是**单 runner 单卷**,不犯此病。

两处修复:
1. **测试 runner 改单节点**(`deploy/docker-compose.phala.runner.yaml`):2 runner→1 `agent-runner`、
   全新卷 `feedling_agent_runtime_runner`(对齐线上;新卷顺带清掉旧陈旧 checkpoint)。test/prod compose
   分开,不影响线上。CI 的镜像 pin 用全局 sed 匹配,单服务不受影响。
2. **wedge 根因**(`tools/chat_resident_consumer.py`):认领消息持续取不到时不再无限重试——同一 cursor
   连续 `CHAT_POLL_WEDGE_SKIP_AFTER`(默认 5)次 miss 后,`_advance_past_unfetchable` 把 cursor 推过这批
   认领消息的最大 ts(边界情况 nudge `+1e-3`),跳过解不出的消息、放行后续。有界重试保留了瞬时解密抖动
   的自愈窗口。纯函数 + 两个单测。1512 passed。

### [BLOCKER→FIXED] 图片修复不完整:thinking-claude 命令漏了 Read 授权（claude 仍看不到图）

### [BLOCKER→FIXED] 图片修复不完整:thinking-claude 命令漏了 Read 授权（claude 仍看不到图）

PR #40 合并部署后,用户切 anthropic/claude-sonnet-4-5 实测**仍"没获得看图片的权限"**。
SSH 进测试 runner CVM 读到实况:该 claude consumer 的 `--allowed-tools`(AGENT_CLI_CMD)里
**没有 Read**,而 settings.json 里**有**——`claude -p` 下 `--allowed-tools` 覆盖 settings.json,
故 Read 被拒。真因:除 `_default_cli_cmd` 外还有一条 **`_default_thinking_claude_cmd`**
(stream-json/effort,`_claude_cli_should_stream_thinking` 判定 deepseek + anthropic 的
sonnet-4/opus-4/3-7 走它),它仍用 `_io_cli_allow_rules`(无 Read)——#40 只补了非 thinking 分支
与 settings.json,漏了这条。修:thinking 分支也改用 `_claude_allow_rules(io_cli, home)`
(`spawners.py`,一行 + 两个 thinking 测试加 Read 断言)。三处 claude 授权点(非thinking/thinking/
settings)现已统一带图片 Read。教训:改 claude 授权要覆盖**所有** claude 命令 builder。1487 passed。

## 2026-07-03

### [DONE] 聊天 AI「看不到图片/截图/屏幕共享」— cli 路径像素从没喂进模型（API+VPS 双中招）

**真因**:托管(API)+ VPS 用户跑同一份 `chat_resident_consumer.py`,在 `AGENT_MODE=cli`
下,图片虽已解密落盘(`IMAGE_TEMP_DIR={home}/images`)、路径也下传,但**像素从没作为
多模态输入到达模型**。`_prepare_cli_command` 只在模板含 `{image_path}` token 时附图,否则
退化到 `_message_for_agent`——把**文件路径当纯文本**塞进 prompt。而两个 driver 的默认命令
都不含该 token:codex(`codex exec … {message}`)从不带 `-i`;claude(`claude -p`)的
`--allowed-tools` 只有 io_cli 动词、**无 Read**,`-p` 非交互下打不开那个图片文件。结果模型
只看到一句路径字符串,如实回答"看不到图"。`usr_6d8c6387242778cb` 报的「API 屏幕共享看不到
内容」即此。

**改了什么**(分支 `fix/chat-images-cli-vision`,PR #40 → test):
- **codex**(`tools/chat_resident_consumer.py`):`_prepare_cli_command` 检测 codex 命令且有
  图时,注入 `_inject_codex_images`——按图加 **`--image=<path>`**(codex 原生 vision 输入),插在
  `exec` 子命令后。用 `=` 绑定单值而非裸 `-i <path>`,否则 clap 的变参 `--image <FILE>...` 会把
  紧跟的 positional prompt 当成第二个图片值吞掉(精简模板 `codex exec {message}` 会丢消息——Codex
  review P2 抓到)。同时**跳过**误导性的路径文本(codex 直接拿到像素)。已含 `-i`/`--image`/
  `{image_path}` 的自配模板不双注入。修复托管 + VPS codex,VPS 零配置。
- **claude**(`backend/agent_runtime/spawners.py`):默认 `--allowed-tools` 与 `settings.json`
  的 allowlist 加 `Read({home}/images/**)`(`_claude_allow_rules` = io_cli 动词 + 图片 Read),
  紧扣 IMAGE_TEMP_DIR、最小授权,让 `claude -p` 能打开被注入路径的图。自配 `cli_cmd` 的 VPS
  用户需自己加(README 已说明)。
- **文档**:`tools/README.md` 补 CLI 模式"怎么让模型真看到图"一节 + 屏幕帧 `on_mention` 触发词/
  `SCREEN_CONTEXT_MODE=always` 说明。
- 测试:`tests/test_chat_resident_consumer.py`(codex `--image=` 注入 + prompt 不被吞 + 自配
  `-i` 不双注入)、`tests/test_agent_runtime_spawners.py`(默认 claude cmd + settings.json 含图片
  Read)。rebase 到 test 后全量 `tests/` 通过。

**没改**(刻意):屏幕 `SCREEN_CONTEXT_MODE=on_mention` 默认与触发正则——正则已很宽(屏幕/共享/
画面/看到/这个/screen/share/look at…),`always` 有隐私/token 成本,真凶是上面的像素投递,已修。
仅文档化。

## 2026-07-02

### [DONE] 非官方托管模型如实报自身模型（identity honesty，未部署未提交）

deepseek/gemini/中转站等跑在 claude/codex/pi 壳子里的模型，被问「你是什么模型」
会继承壳子自带 base prompt 的「我是 Claude Code / Codex」身份。在三 driver 共用的
追加系统提示（`spawners.agent_home_files` 拼的 `system_append`）顶部按 provider 注入
一段身份改写块：非官方模型如实自称配置的 model id，官方原生 anthropic/openai 不加
任何 prompt。判定与内容均为纯函数（`_is_official_identity` / `_identity_override_block`，
白名单＝provider∈{anthropic,openai} 且 base_url 空；**provider 缺省也按官方处理**——
legacy/native/default 条目（claude→原生 anthropic、codex→原生 openai，同 `_codex_transport`
「missing→native」约定）不误伤，真实第三方一定带显式 provider；自称源＝model id，空则回退
provider 名）。身份块置于 persona 之上、与人设解耦（只压「什么模型」不动「你是谁」）。`base_url`
纳入 `_spawn_identity` 使 endpoint 变更触发 reseed。纯 prompt 软约束，个别模型可能偶发
漏说。**未提交、未部署。**

两轮 Codex review 修复：(1) 缺省 provider 按官方处理，legacy/native/default 条目不误伤
（`_codex_transport`「missing→native」同款约定）；(2) 官方 provider 的 base_url 只有为
**非默认**值才翻非官方——`validate_config` 会给官方 provider 也持久化默认 base_url，单纯
非空不算冒充（用 `provider_client.default_base_url` 比对，惰性导入避免破坏 consumer 导入
契约）；(3) gateway codex 用户 `model` 已被改写成内部 `gw-<uid>` 别名，新增 `identity_model`
贯穿 `_wire_gateway_models`→`materialize_home`，身份块自称真实上游模型而非别名，并纳入
`_spawn_identity`。
spec/plan：`docs/superpowers/{specs,plans}/2026-07-02-agent-model-identity-honesty*.md`。

## 2026-06-29

### [DONE] photo_added 唤醒加 new-photo 提示（拉取式）

相册来新图时,`photo_added` 唤醒(原触发不变)现在多一条 `new_photo` 提示:大概是什么
(scene_hint)/是否截图/时段/photo_id,并告诉 agent「想看就 `photo_read(id, include_image=true)`
拉真实像素」+「看完想说就说,像注意到朋友的照片、不是交差报告」。**拉取式,不自动附图,agent
自判看不看**。consumer 侧:`_new_photo_hint()`(best-effort,取最近 1 张 metadata,失败则空、不崩
唤醒),只在 photo_added job 注入。真机前 e2e:提示渲染正确 + `photo_read --include-image` 解出
~203KB JPEG。提交 `5a13264` + `d9a45ca`。

### [BLOCKER→FIXED] io_cli `PHASE2_VERBS` 冲突 → 所有 io_cli 命令全崩

`schedule-wake` 在 `3f98b39` 被加成真子命令,但仍留在 `PHASE2_VERBS`(「未实现」占位 loop),
`sub.add_parser('schedule-wake')` 启动即 `conflicting subparser` → **io_cli 一跑就崩 → OpenClaw 所有
走 io_cli 的原生工具(photo_read/memory_*/screen_*/schedule_wake…)自 3f98b39 起全废**。修:从
`PHASE2_VERBS` 删掉;加防回归测试 `tests/test_io_cli_parser.py`(PHASE2 永不与真子命令重名 + 子进程
冒烟)。提交 `517f7e1`。教训:加真子命令时必须同步从占位列表移除。

### [DONE] 今日 wake prompt / action 协议改动部署后双向 e2e

`3f98b39`(schedule/cancel wake 工具化、删死 ai_state、request_broadcast 折叠进 message、message=
纯回复)+ `a873eeb`(删 battery 字段、平衡 speak/quiet、数字不照报)部署后验证:
- consumer 侧(CC):prompt 构建无 battery / 有时间锚(距上次 Nh,有间隔才加) / 平衡措辞 /
  无 ai_state / new_photo 只在照片唤醒;schedule+cancel wake 工具 ok;action 分类(sleep /
  request_broadcast→可见消息)ok。
- 后端侧(Codex):`pytest tests/test_proactive_* test_perception_* test_io_cli_*` = **185 passed**;
  `/v1/proactive/scheduled/actions` 真触发链 schedule→pending→fire→fired、cancel→canceled 验证;
  gate 路径 / photo_added differ 链完好。
- 残留(待下次部署一起清,不单独 redeploy):consumer `_split_proactive_actions` 的 `set_ai_state`
  死分类(本次已本地删、未提交);后端 `ai_state` legacy/dashboard 字段(Codex 评估)。

### [BLOCKER] test CVM 重部署脆弱性 + 链上 compose_hash 依赖 gas（运维教训）

排查"consumer 全 401 崩溃循环"时发现根因**不是账号/key/代码**,而是 **test CVM 后端被当天连环
部署(4 次 deploy-test-cvm)搞到不健康**:`test-api`/`test-mcp`(走 dstack-ingress)整层 TLS 挂、
但 enclave :5003 直连口仍活;Andrew 在 Phala 层 restart 后恢复。两条运维教训记下:
- **这台 test CVM 每次重部署会整体 blip ~2min**(enclave 先回、test-api 随后),通常**自愈**,
  不要看到瞬时 000 就当宕机;短时间连推多次才会雪崩。
- **每次 deploy 都要发 compose_hash 上 Sepolia test 合约**(`0x9AC0…F2D5`,owner
  `0xa0eBcd26…`)。`517f7e1` 这次 publish **因部署钱包 gas 不足而失败** → 新 compose_hash 没上链 →
  iOS attestation 会拒新 CVM、挡 onboarding。充 Sepolia ETH + re-run `deploy CVM (test)` job 后
  publish success、白名单补齐。**部署钱包余额是真机 onboarding 的隐性前置依赖。**

## 2026-06-27

### [DONE] 照片解密读取修复 + 快捷指令 app 上报打通（真机 e2e）

感知输入真机排查发现两个问题,均已解决:

- **照片像素解密(真 bug,Codex `9b25544` 修)**:enclave 三处解密(`/decrypt` `/caption`
  `/image`)假设明文是屏幕帧的 UTF-8/JSON,但**照片明文是裸 JPEG 字节**(0xFF D8 FF)→
  `plaintext_parse` 502,agent 永远拿不到照片像素。修法:`_parse_visual_plaintext` 按 magic-byte
  (JPEG/PNG/WebP/HEIC/AVIF)识别裸图并 base64 包装;坏字节/非 dict JSON 仍 fail-closed;屏幕帧
  JSON 路径零改动(无回归)。CC 审计通过。真机验:`photo-read --include-image` → `decrypt_status=ok`,
  **image_b64_len=208696**;`screen-read` 回归 ok。
- **caption(enclave VLM)残留 gap**:**test enclave 未配
  `FEEDLING_SCREEN_VLM_API_KEY`** → `/caption` 在鉴权/解密前返回 503
  `screen_caption_unconfigured`;raw-photo caption parser 目前由单测覆盖,真实出图描述待部署 key 后补验。
- **快捷指令 app 上报**:URL/key/端点全对,问题是 iOS 自动化设成了「确认后运行」→ 改「立即运行」
  后,真机打开淘宝实时落 `recent_apps` + app 信号 ✅。
- **照片分类**:正常(真照片 `is_screenshot=false`)。
- **照片仍是"拉取式"**:photo_added wake 只通知,不自动附图;Seven 倾向改"推送式"(wake 自动附
  解密照片,复用屏幕帧 `images=` 机制)——follow-up 待做。
- 体检表更新:`docs/PERCEPTION_COVERAGE_HEALTHCHECK_2026-06-27.md` §B。

### [DONE] 主动 wake digest：从「健康独大 top-N」改成「均衡跨域桌面 + Agent 自判」

`/v1/agent/perception/digest` 之前第二半 `change` = `notable_changes()` 取 top-8,**只比较量化
数值信号**(vitals/metabolic/weather/activity/sleep/body/cycle)→「什么值得主动提」结构上只能是
健康数值,Agent 退化成身体监测机。现改:后端**均衡铺开跨域近况**,健康折叠成 1 行,与音乐/位置/
app/照片/提醒/天气/心情/日历/屏幕**平级**;**后端不产 flag**,Agent 读桌面自判最多 2-3 条拟人化
值得一提(可跨域组合,优先生活情境而非健康播报)。spec/示例:`docs/DIGEST_CROSSDOMAIN_REDESIGN_PLAN_2026-06-27.md`。

- **后端**:`backend/perception/history.py` 新增纯函数 `cross_domain_recent()`(10 域;health 复用
  `notable_changes` 折叠;轻量 novelty `new_artist`/`long_dwell` 作事实上下文,非排名);
  `backend/agent/routes.py` digest 端点返回 `{days, changes, domains}`(`changes` 保留向后兼容)。
- **消费端**:`tools/chat_resident_consumer.py` `_proactive_perception_digest` 改 3 元
  `(presence, change, domains)`;wake prompt 渲染 `cross_domain_board_json` + 自判指令;旧后端无
  `domains` 时回退 legacy change。
- **测试**:cross_domain_recent 单测(均衡/折叠/novelty/诚实报空/降级)+ 路由 + 消费端渲染/兼容,
  本地 276 passed(4 个 `dstack_sdk` 缺失 error 与本改动无关);DB 测试由 CI 跑。VPS resident e2e 待部署后验。
- **路径**:resident/VPS,不碰 hosted。**全感知接口/字段清单**(对比用):同 spec 文档 §1。

### [DONE] Agent 声音/身份/Genesis：全链路建成 + 部署 test CVM（e2e 待跑）

host(API key)用户的空白 runtime,在 **CVM/enclave 内**从上传历史蒸馏出**声音(persona 文件)+
事实(Garden)+ 展示身份卡**。spec:`docs/AGENT_VOICE_IDENTITY_SPEC_2026-06-27.md`。CC×Codex 分工
(CC=agent_runtime/prompt/审计;Codex=backend/infra),每个 Codex 交付经 CC 审 diff + 重跑测试再 push。

- **流水线**(origin/test):chunked 加密上传 ledger → CVM worker(claim/解密/map-reduce/写) →
  按 `source_kind` 路由(history→声音+事实;ai_persona→adopt 主干;memory_summary→事实;
  user_profile→事实+防火墙) → §7.B persona+voice 跨 job 加密合并 → CC 的 supervisor 激活 hook
  (default-off daemon,`FEEDLING_GENESIS_WORKER_ENABLED`)。事实走 `/v1/memory/actions` 同 capture
  lane schema(source=`genesis_import`)。
- **隐私姿态**(`PRIVACY_MODE=backend_storage_no_plaintext_user_provider_authorized`):raw 上传只存密文;
  解密+LLM 只在 CVM 内、用**用户自己的 key**;persona/voice/identity blob 全加密;outputs 只存 hash/count;
  chunk owner 绑定+完整性校验。**不 overclaim**(明文会发给用户自己配的 provider)。
- **审计抓到并修复**(CC 作为审核员):persona/outputs 明文落库→加密;fresh-start 死锁→gate 放行;
  AI persona 没 adopt→source_kind 路由;声音丢失→§7.B 合并;spec 隐私 overclaim→改精确口径。
- **CC 自做**:host session cap 24、persona seed+解密 reader、genesis gate、`io_cli identity-write`(7.D)、
  激活 hook、`tools/genesis_e2e.py` e2e harness(自助注册测试用户→封装上传→验证)。
- **已部署**:`a3475c6` 把 `:34ce885` 部署到 test CVM,worker 上线(dormant 待 job)。
- **待办**:真 e2e 跑一遍(需一把测试 provider key 喂 harness;`TEST_FEEDLING_RUNTIME_TOKEN_SECRET`
  已确认存在);§7.B order-edge(history 须先于 ai_persona)留 fast-follow;iOS 上传客户端;7.D 触发编排;
  VPS skill.md 的 grounding 子句。

## 2026-06-26

### [DONE] A-full：落卡 capture lane（Phase-1）+ 退役 proactive 模拟工具路（Phase-2）

承接 A-lite（perception 统一到 CLI tools）。memory v1 后端落地后启动，目标：proactive 全走原生
CLI 工具，消灭"把 agent 当裸模型返 JSON"的双路。

**Phase-1 — 落卡 capture lane（对齐《IO 记忆·落卡+Dream 完整方案》第一部分）**
- 独立 capture lane（复用 job 原语，不复用 proactive reach-out 语义）：PR A `2136073` 基座
  (typed `job_kind=memory_capture` + `capture_key` 幂等 + poll 跳 wake gate + 分发)；PR B `457ba01`
  触发 coordinator（append_chat 钩子 + `/v1/device/events` 边界 + `/v1/capture/tick` 静默兜底 +
  `capture_state` 去重）；PR C.1 `e148da2` 落卡 prompt+parser；PR C.2 `bf2cf66` 原生 handler
  （window→原生 call_agent→parse→封 v1 信封→`/v1/memory/actions`，不写 chat/不投递）。
- 触发 = 会话断点（静默 1200s / 退后台 / 轮数 24 兜底），**不是 agent 每轮主动调**。
  **不变量（测试钉死）：关「AI 主动找我」≠ 停记忆。**
- VPS e2e：静默触发→handler 调真 agent 回看 55 轮→写 2 张高质量卡（memory 38→40，桶复用"我们的关系"）。
- 验证交接文档：`docs/CAPTURE_LANE_VERIFICATION_2026-06-26.md`（给工程师独立验证）。

**Phase-2 — 退役模拟工具路**
- 审计发现：VPS proactive reach-out 当时走的就是模拟路（`RUNTIME_V2_DEFAULT_ON=true`）且功能完整，
  native/legacy 是退化旧桩 → 不能直接删。先 P2-1 `a3d2d9b` 补原生 reach-out 同等能力
  （native send_message action-only / schedule_wake·cancel_wake 解 gate / perception digest 改直连
  `/v1/agent/perception` / 唤醒 agent 可调原生 perception·memory·screen / cost 标签 D2），再
  P2-2a `b008909` 翻 test 默认到 native 验证（prod 不动），native VPS e2e 通过后 P2-2b `aa31380`
  删除：`run_tool_loop_v2`(tool_loop_v2.py)、`/v1/proactive/tool/execute` 路由、`_resident_run_agent_v2`、
  `_resident_call_tool_v2` + 对应测试/ci。proactive wake 现在**始终走原生**。
- **保留** `tool_executor_v2`/`tool_catalog_v2`（hosted 前台 chat / dashboard / runtime_v2 仍用）。
- 删后 VPS e2e：手动+自动唤醒都走 native（digest 直连、agent 跑、sleep 有理有据、**零 `/v1/proactive/tool/execute`**、无报错）。
**Tail（同日收尾）**
- Tail-1 修：`FEEDLING_RUNTIME_V2_DEFAULT_ON` 是**共享 baseline**（perception ingress / chat / screen caption /
  hosted chat 都跟它），P2-2a 为验 wake 把 test 翻 false 顺带关了 test 感知 v2；wake 已 native-only 后翻回 true
  恢复（`4d72392`），prod 一直 true 未动。
- Tail-3 `io_cli`：补 `photo-recent` 原生读工具（`b7d52a6`）；`send/wait-for-wake/schedule-wake` 是 native 输出
  动作不是 pull 工具，改成澄清 stub。
- **Tail-2 Dream（方案 Part 2，已 ship）**：`job_kind=memory_dream` 复用 capture 基座（PR D.1 `30378a9`
  prompt+parser、PR D.2 `6a0f687` lane）。夜间/攒量触发→原生 call_agent 整理（merge/thicken/supersede）→
  **只 `memory.supersede` 软退绝不硬删**、不写 chat、不走 reach-out gate、questions 落 status 不发用户。
  VPS e2e：force tick→agent 整理 40 卡→8 consolidations（merged 5 / superseded 31），active 40→17，
  旧卡 status=superseded 仍可 fetch、superseded_by 链保留、零 chat。
- 尾巴（后排）：prod 切 native 是一次 prod 部署（待点头，code 已 native-only）；Dream eval 留后；
  io_cli send/wait/schedule 仍 stub（设计上就是输出动作）。

### [DONE] resident consumer 自动更新（路径感知锁步）

自托管 consumer/io_cli 走 git clone 分发，onboarding 后除非手动 `git pull`+重启否则永远跑旧代码。现在让 consumer 自动跟上后端部署的 commit：

- **后端下发期望版本**：`backend/chat/consumer.py` 新增 `expected_consumer_commit()`（`FEEDLING_EXPECTED_CONSUMER_COMMIT` 显式 pin → 回退 `FEEDLING_GIT_COMMIT`）；`/v1/chat/poll` 三个 return 加 `client_release.expected_consumer_commit`。后端不需要 `.git`。
- **consumer 路径感知自更新**（`tools/chat_resident_consumer.py`）：idle（timed_out）poll 时比对本地 `HEAD` 与下发 commit；**仅当** `git diff HEAD..target` 命中本进程实际加载的 repo 文件（从 `sys.modules` 自动推导 + 显式补 `io_cli.py`/requirements）才 `git fetch`+`checkout --detach`+`os.execv` 原地重启。**无关后端发版不触发**（解决"每次发版都 pull"）。
- **安全边界**：默认开（`FEEDLING_AUTO_UPDATE=0` 关）；脏工作区跳过并告警，不丢本地改动；requirements 变了先 `pip install` 兜底；hosted（`FEEDLING_RUNTIME_TOKEN_FILE` 存在的 in-CVM）禁用（镜像不可变 + attestation）。io_cli 同仓库免费搭车。
- 测试：`tests/test_chat_resident_self_update.py`（纯函数真值表 + 编排 mock）、`tests/test_expected_consumer_commit.py`、`tests/test_chat_poll_client_release.py`。文档：`tools/README.md` § Auto-update、`deploy/chat_resident.env.example`。

## 2026-06-25

### [DONE] 主动陪伴系统打通（心跳/三开关/定时器）+ 感知后端 catch-up（全新信号暴露给 agent）

**主动陪伴（Bug A/B 端到端修复 + VPS 真测）** — resident 路从"从未真正工作"到全通：
- **Bug B（心跳从不触发）**：resident consumer 空 broadcast 派生成 `heartbeat_unknown` 被 gate 拦死 → 改成 `heartbeat_broadcast_off`（对齐 hosted）。`008079f`。
- **Bug A（开关不 gate）**：`/v1/proactive/tick` 改用 `evaluate_wake_control_v2`（移除 legacy dnd/user_state 拦 wake，符合 D6/D16：dnd 只 gate Delivery）；resident `/jobs/poll` 按开关 gate pending job；`/chat/response` 应用 delivery gate（提醒关→写 chat 不 buzz）。`008079f`。
- **resident 定时器服务端 fire**：新增 `POST /v1/proactive/scheduled/fire` + consumer 60s loop（`fire_due_timers`，复用 scheduled gate + 透明回灌）。`f772691`。
- **VPS 三个隐藏阻塞**（修复后才跑通）：env `PROACTIVE_POLL/TICK_ENABLED=false`（总开关关着）→ 开；consumer venv 缺 `psycopg`+`psycopg_pool` → 装（已进 `tools/chat_resident_requirements.txt`）；插件 SIGNALS 扩到 17。
- **真测通过**：心跳→主动消息端到端；C1 关陪伴 gate、F2 不连坐（关陪伴定时仍 fire）、C3 关定时 gate；E2 定时器到点 fire→消息。仅 resident，**API/hosted 一行未动（待砍）**。

**感知后端 catch-up** — iOS 采集远超后端暴露,补齐 `_SIGNAL_FIELDS` 让 agent 真能拉：`7a8be95`。
- 新增 6 信号:`reminders` + `health_activity/body/metabolic/cycle/mood`;扩 `weather`（体感/湿度/降水/UV/预警）、`health_sleep`（core/deep/rem）、`health_vitals`（current_hr/hrv/呼吸/血氧/vo2max）。catalog + resolve + ios_contract（加密 allowlist）+ agent routes + io_cli + 插件 + 测试/fixture 全同步。
- **真机验通**:新包上 focus=true（iOS `2504c3e` entitlement 修复）、睡眠分期 511/298/76/137、心率 68/呼吸 17、身高 156/体重 52、活动能量 — 真值端到端;无数据项诚实 null。

**iOS 修复（拉入）**：`2504c3e` focus Communication Notifications entitlement（focus 修好）、`ed9c54f` 发图 picker 改 fullScreenCover（修跳 home）。

**文档整合**：新增 `PROACTIVE_COMPANION_FUNCTION_AND_TEST_SPEC_2026-06-25.md`（功能定义 + 详尽测试)、`PERCEPTION_FIELDS_REALDEVICE_CHECKLIST_2026-06-25.md`（逐字段真机核对)。删除被取代的时点文档:`ROUND3_REALDEVICE_TEST_PLAN.md`（→ 上述两份）、`PERCEPTION_FIELD_RECONCILIATION_2026-06-23.md`（→ 06-25 字段核对表）。

## 2026-06-25

### [DONE] 即时感知收口：focus/audio_route 可 pull + place_label/气温口径校正 + spec 校准
- **focus + audio_route 暴露给 agent pull**：发现这俩被 iOS 采集、后端也接收存储，但 `/v1/agent/perception` 的 `_SIGNAL_FIELDS` 漏了它们 → agent 实际拉不到（spec 却把它们列为 pull-only "agent 自己拉"）。补：后端 `_SIGNAL_FIELDS` + `_SIGNAL_PERMISSION_KEYS` 加 focus/audio_route（不入默认快档）、`tools/io_cli.py` 加 `EXTRA_SIGNALS`、OpenClaw `feedling-io-tools` 插件 SIGNALS 9→11 + 重启 gateway。**12 个文档信号现在全可 pull**（io_cli `b7a7e3d` / 后端 `e49b6da` 已部署 test；插件改动仅在 VPS）。
- **`place_label` 回退 `outdoor` → `unknown_place`**：`outdoor` 误导（用户没配 geofence 时在家也报 outdoor，像"在户外"）。它真实含义是"有定位但不在任何已命名地点"。iOS resolver + 后端 `resolve.py` + spec 同步;`unknown`（无 fix）保留。**未推**（与下条一起，待用户 push）。
- **气温 `temperature_bucket`(5℃ 桶) → `temperature`(精确摄氏度)**：产品决定——5℃ 桶无价值，天气敏感度低、weather 是 pull-only 不参与 changed，精确值无额外代价。iOS WeatherValues + 后端 catalog/resolve/routes/tool_executor + 5 个测试文件 + spec 同步。**未推**。
- **删除过时 spec**：`Specs/perception-report-fields.md`（2026-06-08 预 V2 版，声称原样上报精确坐标/BSSID/地址，与部署的 V2 加密粗化口径冲突）已删，统一以 `perception-data-and-reporting.md` 为准。
- **本轮 spec 校准**（`perception-data-and-reporting.md`）：修"12 个 key"→14（13 信号 + unsupported，对齐 `EXPECTED_REPORT_KEYS_V2`）；§3.8 日历事件时间标注为**设备本地时区**（ISO8601 带偏移）；§3.4/§3.5/§6/§1.1 同步 unknown_place + 精确气温口径。其余逐字段核过与代码一致。
- 日历加回参会人/组织者、天气加降水预报/体感、HealthKit 加心情(State of Mind)/活动三环/HRV 等**新增**字段 → 用户决定**交给 iOS 工程师**实现，不在本轮 scope。
- 待推 bundle（用户统一 push）：iOS `cf8ece5`(outdoor)/`96540fa`(temp)/本轮 spec 校准；后端 `7bc2218`(outdoor)/`1d4b717`(temp)。

## 2026-06-23

### [DONE] resident agent 原生感知端到端跑通（io_cli + OpenClaw 插件）+ config 去硬编码
- 验证 resident（OpenClaw）经 io_cli 原生工具调 `/v1/agent/perception` **端到端通**：agent 在真实聊天里报出真实电量 / 位置 / 睡眠（睡眠 390min=6.5h、位置 outdoor/home、电量 70%）。
- **根因修复**：OpenClaw `feedling-io-tools` 插件的 config 没经 gateway 交付（`register(api, config)` 收到空 `{}`）→ `path.resolve(undefined)` 抛错 → 发消息时工具崩。改成 **`config → 环境变量 → 报错`（零代码硬编码）**，host 路径放 `openclaw-gateway.service` 的 systemd Environment；插件在 `definePluginEntry` 里声明 `configSchema`；改完必须 `systemctl --user restart openclaw-gateway` 重载（gateway 常驻、缓存插件）。工具失败改返 `{ok:false,error}` 不再崩。
- 清掉 OpenClaw 两处 stale 配置（`skills.feedling` 指 localhost:5001、`lossless-claw`）。
- **注**：插件代码只在 VPS（`~/.openclaw/workspace/plugins/feedling-io-tools/`），**未进仓库**——后续应落仓或转 MCP server，见 `AGENT_CLI_INTEGRATION_SURVEY.md`。

### [DONE] 感知增强：上报城市 locality + ±14 天日历列表 + 日历本地时区（iOS + 后端）
- **#1 city**：iOS location 上报新增 `locality`（反地理编码城市名，如"深圳市"）；后端 catalog/resolver/`/v1/agent/perception` 落地暴露。**有意放开城市级定位口径**（街道 / 坐标仍不出设备）——因 `place_label` 对没配 geofence 的用户恒为 `outdoor`，agent 没有"在哪座城"的感知。
- **#3 calendar**：从"24h 单个 next_event"扩成 `calendar_events` **前后 14 天列表**（含全天事件、按 start 排序、封顶 40 条 + `calendar_events_truncated`），保留 `calendar_next_event` 给唤醒/快照（changed 判定只看它，避免窗口滑动误唤醒）。
- **时区**：日历事件时间改用**设备本地时区**输出（ISO8601 带 `+08:00` 偏移），agent 直接读本地钟点（如 15:00），不靠它自觉用 `now.timezone` 换算——少一处出错点。
- 提交：iOS `4edf2bd`（city + ±14天列表）、`a97a73a`（本地时区）；后端 `31ae6c9`（已部署 test CVM）。后端 71/84 测试过，用**真实信封 body 形状**覆盖。

### [FEEDBACK] 感知字段语义对齐审计（以 iOS 上报端为准）
- 系统性对齐 iOS 采集/上报 ↔ 后端接收/存储/暴露：**契约逐字 1:1，无后端凭空字段**（`timezone`/`temperature_bucket` 等 iOS 端确实存在）。完整对照 + 修正清单入 `PERCEPTION_FIELD_RECONCILIATION_2026-06-23.md`。
- 真机查清各信号语义：`location` 恒 `outdoor`=没配 home/work geofence（resolver 回退）；`sleep`=近 24h 滚动入睡时长（凌晨读=昨晚）；`steps` 凌晨 null=今天还没走（正确）；`weather` null=`WeatherService` 抛错（entitlement 在，疑 Apple Portal WeatherKit capability 未生效，**留工程师**，Xcode Console 搜 `weather fetch failed`）；`calendar` 只读 iOS 已同步的日历账户（飞书/Google 工作日历若没同步进 iOS Calendar 就读不到）。
- `focus` / `audio_route` 后端 `/v1/agent/perception` 未暴露（iOS 在采集）——待补。

### [DONE] iOS 聊天 typing 指示器多条待回修复 + 一个自递归崩溃
- 修"连发两条、reply1 一到就灭点点点"：改用 `pendingReplies` 计数，全部待回落地才隐藏指示器；`isWaitingForReply=false` 时自动归零防卡死。
- 修一个**自己引入的崩溃**：上面改动用 `replace_all` 抽取 5 分钟超时块时，误把新加的 `beginAwaitingReply()` helper **自身函数体**也替换成调用自己 → 无限递归 → **发送即崩溃**（栈溢出）。恢复 helper 体。提交 `a1ecd3e`。**教训**：无限递归是运行时错、`xcodebuild` 能过不代表不崩，改完必须真机/模拟器跑一次冒烟。

### [DONE] docs 清理 + agent-CLI 调研文档
- 删 12 个已 ship 的 Round 3 PR 执行脚手架文档（`PROACTIVE_PERCEPTION_PR1…PR10`；聚合的 `ROUND3_EXECUTION_PLAN` / `RUNTIME_V2_MIGRATION` 保留作 PR 总览 + 迁移契约）。
- 二次清理：删 `PROACTIVE_GATE_V1.md`（V2 后自标 archived、非活跃路径）、`ROUND3_HANDOFF.md`（merge-前交接清单，branch 早已 merge 进 test）、`ROUND3_VALIDATION_STATUS.md`（06-20 审计快照；当前真机状态以 `ROUND3_REALDEVICE_TEST_PLAN.md` + 本 changelog 为准）。`MODEL_API_PATH_P0.md` **保留**——它是托管 Model-API 这条 live 路径的唯一设计文档、且被 `PROJECT_OVERVIEW` 文档索引引用。docs 41 → 26。
- 新增 `AGENT_CLI_INTEGRATION_SURVEY.md`：各 agent（OpenClaw/Hermes/Claude Code/Codex）接 CLI 的机制调研。结论——**io_cli + skill/exec 是有 shell 能力 agent 的通用最小公分母**，不必每个 agent 写专属 adapter；native 插件/MCP 是更强的"升级位"；≥2 个非 OpenClaw runtime 要 production-grade typed 工具就做 **Feedling MCP server**，而不是继续扩散 per-agent adapter。

## 2026-06-22

### [DECISION] V2 baseline 扩展到全部 4 个 rollout flag + prod 也默认 ON
- **背景**：上一条只把 `perception_ingress` / `resident_wake` / `resident_chat` 接入 env baseline。但 hosted(API) 用户线还有 `hosted_wake_runtime_v2_enabled`、`hosted_chat_full_tool_loop_v2_enabled` 仍 OFF → hosted 用户感知数据进来了（ingress 已 ON），但 wake 走 legacy executor、前台聊天不 pull 感知工具（半截）。`screen_caption_enabled` 也 OFF。
- **改动**：
  - 三个 reader（`hosted/wake_consumer.py`、`hosted/chat_routes.py`、`proactive/screen_flag_v2.py`）改为未设值时回落 `core/util.runtime_v2_default_on()` baseline；显式 per-user 值仍优先。
  - `hosted/config_store.py` 停止播种这三个 + perception 共 **4 个** flag；scrub 从单 flag 的 bool marker 泛化为 **set marker** `v2_autoseed_scrubbed_flags`（`AUTOSEED_SCRUB_FLAGS` 列表），兼容旧 `perception_v2_autoseed_scrubbed` bool（迁移进 set 并删除旧 key）。每个 flag 一次性清理历史播种 False，之后运维显式写的 False（per-user opt-out）存活。
  - **prod 也默认 ON**：上一轮已给 `docker-compose.phala.yaml` 加了 `FEEDLING_RUNTIME_V2_DEFAULT_ON: "true"`，所以 4 个 flag 在 test+prod 两个 compose 下都默认 ON。**两个 compose 不用再改**——新接入的 flag 共用同一个 env baseline 自动跟着 ON。
- **screen_caption 隐私决定**：它把屏幕截图外发第三方 VLM(OpenRouter)，原为 fail-closed opt-in。用户**明确选择默认 ON**（含 prod）。reader 仍保留 error→OFF 的 fail-closed。
- **测试**：`test_runtime_v2_default_flag.py` 扩展（4-flag scrub + set marker + 旧 bool marker 迁移 + hosted_wake/hosted_chat/screen_caption baseline）；本地非 DB 回归 200 passed。需 PG 的（`test_hosted_wake_v2_cutover` / `test_model_api_wake` / `test_proactive_tool_execute_route`）交给 CI。
### [DECISION] Perception/Resident V2 rollout flags 改为 env-gated baseline（test 默认 ON / prod OFF）
- **背景**:三个 V2 灰度 flag(`perception_ingress_runtime_v2_enabled`、
  `resident_wake_runtime_v2_enabled`、`resident_chat_runtime_v2_enabled`)默认全 OFF,
  又**没有任何 setter**(只能 `db.set_blob` 直写 per-user blob),test 上每个账号都得手翻,很烦。
- **改动**:
  - 新增 `core/util.runtime_v2_default_on()` 读环境变量 `FEEDLING_RUNTIME_V2_DEFAULT_ON`
    作为三个 flag 的**基线默认**;显式的 per-user blob 值仍然优先(operator opt-in/opt-out 不变)。
  - 三个 reader(`perception/service.py`、`proactive/resident_runtime_v2.py`)未设值时回落到基线。
  - **修坑**:`hosted/config_store._ensure_model_api_runtime_profile` 之前会把
    `perception_ingress_runtime_v2_enabled` 自动播种成 `False`,把每个 hosted profile 钉死、
    让 env 基线失效。现在①不再播种该 key ②对已存在的"自动播种 False"做**一次性** scrub——
    用 `perception_v2_autoseed_scrubbed` marker 门控,只清理一次历史 artifact;**marker 落下后,
    运维日后显式写的 `False`(per-user 回滚/opt-out)会被保留**,不再每读必删(Codex review P2)。
    显式 `True` 任何时候都保留。
  - `deploy/docker-compose.phala.test.yaml` 的 **backend** 服务加 `FEEDLING_RUNTIME_V2_DEFAULT_ON: "true"`;
    **prod compose 不加** → prod 仍 OFF、保留 legacy 回滚口子。
- **为什么不硬 `True`**:这些 flag 的设计就是"翻一个 flag 即回滚,不用回滚代码";env-gated 既解了
  test 的手翻痛点,又不动 prod 的回滚安全性。
- **测试**:`tests/test_runtime_v2_default_flag.py`(6 例:env 基线、显式 override、scrub、保留 True)全过;
  perception/ingress/runtime_v2 回归 89 passed。resident 聊天 consumer 在 VPS 上无需 env——它从
  `/v1/proactive/jobs/poll` 的 `runtime_v2` 拿服务端已算好的基线值。
- **影响文档**:`PROACTIVE_PERCEPTION_PR7_INGRESS_CUTOVER.md` / `PR9_RESIDENT_CUTOVER.md` 里"default
  false"现在应理解为"prod 基线 false / test 基线 true,per-user 仍可覆盖"。

### [DONE] 修 resident reply loop:OpenClaw 输出解析 + verify_loop 真调 agent + skill 路由硬规则
- **背景**:一次 VPS onboarding 实测——onboarding 各步显示"成功 + 发了问候",但用户回消息后
  iOS 一直 loading、永远收不到回复。SSH 进 VPS 看 consumer 日志定位到三个叠加问题。
- **诊断(VPS 日志 + 复现 OpenClaw 命令)**:
  - consumer 收到了消息、解密成功;调 OpenClaw → OpenClaw **回得好好的**
    (`result.payloads[0].text="能看到..."`,status=ok);**但 consumer 解析不出来** →
    `_reply_from_json_obj`/`_agent_turn_from_obj` 不认 `result.payloads[].text`(只认到 `result` 就停)
    → 判 "no usable reply" + `SEND_FALLBACK_ON_AGENT_ERROR=false` → 什么都不发 → iOS 永转。
  - **verify_loop=true 是假阳性**:consumer 见 verify ping 走"罐头 liveness 回复"短路、**根本没调
    真 agent**,所以掩盖了上面的解析失败,让 onboarding 误判通过。
  - agent 还把 consumer 接到了 **OpenClaw**(用户其实在跟 Hermes 对话),并改了 OpenClaw 的
    IDENTITY.md/BOOTSTRAP.md——把"agent_name 别叫 Hermes"误套到"换 runtime 当传输"。
- **改动**:
  - **②(consumer,feedling-mcp test)**`tools/chat_resident_consumer.py`:加 `_openclaw_payload_texts`
    显式 extractor,接进 `_reply_from_json_obj` / `_multi_reply_json_from_obj` / `_agent_turn_from_obj`
    三处,支持 OpenClaw `result.payloads[].text`(含多气泡);加 3 个回归测试。
  - **③(consumer)**verify ping 不再罐头短路,改成**有界真 agent 探活**:慢(>20s,可配
    `VERIFY_PROBE_TIMEOUT_SEC`)→ 回退罐头 ack(不冤枉健康慢 agent);完成但无可用回复 →
    **不 ack,让 verify 失败**(把解析/传输坏掉的链路在 onboarding 阶段就暴露)。
  - **①(skill,io-onboarding main)**`skill-resident-agent.md` 加硬规则:**consumer 的 agent 入口
    必须是收到 onboarding 指令的那个 runtime 本身**,多 runtime 同机时不许改接"更顺手"的兄弟;
    runtime 自报名字是 agent_name 的事,别为此换 runtime 或改 IDENTITY.md/BOOTSTRAP.md。
  - **④(consumer)**自测中发现:test 版 consumer 顶层 import `proactive.adapters_v2`/
    `runtime_v2` → `observability_v2` → `db` → **psycopg**,而 resident(纯 HTTP 客户端、无 DB)
    的 venv 没有 psycopg → 切 test 分支后 import 直接崩。这俩符号只在 proactive-job 路用,
    已改**惰性导入**,聊天回复路 import 即 psycopg-free。
- **端到端自测(真实 VPS + 真实 OpenClaw)**:把 VPS consumer 切到 test 分支、重启(decrypt
  source OK enclave、无 crash),调 `/v1/chat/verify_loop` → **`passing=true`,response 15.1s**;
  consumer 日志 `verify ping — exercising real agent path` → `real agent reply OK`。即
  poll→真调 OpenClaw→解析 payloads→回写 整条链已通,原"回消息没回复"复现并修复。
- **遗留**:① 是 skill 约束,不能 100% 强制 agent 守规;OpenClaw 仍非文档化入口(但现在能解析了);
  proactive-job 路仍需 psycopg(把 `merge_wakes_v2` 从 db-bound 模块拆出是单独的后端清理)。

## 2026-06-21

### [DONE] 修 VPS resident 入驻接不上(MCP→enclave 迁移残留,跨仓库)
- **现象**:test 环境 VPS 用户复制连接信息给自己的 agent,agent 卡在 Live
  connection——consumer 去探 `test-mcp.feedling.app/mcp`、`/sse` 全 404,
  decrypt source 不可达,verify_loop 永远 false。memory/identity 都没问题。
- **根因(三个叠加,均非 agent 的错)**:
  1. iOS `FeedlingAPI.residentConsumerConfig` 仍发死的 `FEEDLING_MCP_URL/KEY`,
     且**完全不发** `FEEDLING_ENCLAVE_URL`(MCP 下线后 consumer 唯一的解密源)。
  2. `skill-resident-agent.md` 让 agent 拉 `origin/main` 且以 HEAD==main 为 gate;
     但 main 停在 MCP 下线**前**(aef4809),那版 consumer 仍走 MCP,与 test 后端对不上。
     enclave 直连 consumer 在 `test` 分支。
  3. 结果根本没人给 `FEEDLING_ENCLAVE_URL`。
- **验证前提**:curl test enclave `-5003s.../v1/chat/history` → 无 key 401、带
  Bearer key 200、attestation 200 → 解密源可用,方案成立。
- **改动(决定:consumer ref 用 `test` 分支;解密走 enclave 直连)**:
  - **iOS**(feedling-mcp-ios):`CVMEndpoints` 加派生量 `enclaveURL`
    (`https://<appId>-5003s.<gateway>`,按环境自动出 test/prod);
    `residentConsumerConfig` 去掉 `FEEDLING_MCP_URL/KEY`、改发 `FEEDLING_ENCLAVE_URL`;
    `connectionDetailsBlock` 删掉死的 "Chat-client MCP command" 行。
  - **io-onboarding** `skill-resident-agent.md`(EN+ZH):连接信息加 `FEEDLING_ENCLAVE_URL`;
    consumer 来源 `origin/main`→`origin/test`、删 HEAD==main gate;新增"agent_name(卡里名字)
    ≠ 选哪个 runtime 当传输,别为改名去换 runtime 或改 IDENTITY.md/BOOTSTRAP.md"。
- **顺带解释用户的"想让 Hermes 连却选了 OpenClaw"**:agent 把"名字别叫 Hermes"误套到
  "用哪个 runtime 传输",还改了 IDENTITY.md/BOOTSTRAP.md(违反"别包新人格")——已在 skill 拆清。
- **未做(留作单独清理)**:chat-client(路由A)的 `mcpConnectionString`/empty-state MCP 命令
  仍是死的;`main` 落后 `test` 两个月的 release 卫生;prod 是否同病(取决于 prod-mcp 是否还活)。
- 验证 enclave 可达通过;iOS/skill 改动仅配置/文档,未跑构建(需 Xcode)。

### [BLOCKER] 感知工具循环只在 wake 路,前台聊天未收敛(违反 D1)→ 已派 Codex
- **审出的缺口**(外部 Claude 排查 + 我代码核实):`run_tool_loop_v2` + `ToolExecutorV2`
  (全 catalog perception+memory)只接进**主动 wake 路**;**前台聊天两路都没接**——
  hosted `chat_routes._run_model_api_memory_tool_loop` 只认 memory 工具(`MEMORY_INDEX/FETCH`,
  无 perception.*),resident `_process_messages`(consumer:3154)走老单发回复、不进 tool loop。
  结果:**聊天时 agent 无法按上下文 pull 感知**(perception 只是被动 push 的快照)。
- **定性**:不是 spec 遗漏,是实现缺口 + **违反 D1**(chat 与 proactive 应是同一引擎)。
  spec 明确要求聊天 agentic 调工具:D1 / §2.1("聊运动 pull 步数")/ §6+B2(前台 agentic =
  路由器本身,D9 硬前置)。
- **派 Codex**(mailbox 20260621T145308Z):hosted+resident 聊天两路收敛到 `run_tool_loop_v2` +
  `combined_runtime_adapters_v2`(全 catalog),flag 默认 OFF;**关键约束**:前台延迟敏感,
  必须守**快档 cost_class 预算 + 软交棒**(D17/D9/§6)——slow 工具走 `needs_background` 后台
  回灌,不能像 wake 那样内联跑 slow 工具把用户卡在"思考中"。审计待 Codex 实现后做。
- 影响文档:`PROACTIVE_PERCEPTION_SPEC_V2.md` §9 B2 状态改为"部分完成"。

### [DONE] Proactive tool-loop execution (D11: bounded multi-turn for both hosted + resident)
- **Unified loop shipped**: Both hosted and resident proactive wakes now run `run_tool_loop_v2()` — a bounded multi-turn agent loop that calls the model, parses `tool_calls` JSON, executes tools, and feeds results back (max 4 iterations, capped at `MAX_TOOL_ITERS_V2`). One shared `ToolExecutorV2` instance per run provides budget continuity and unified tool implementations.
- **Hosted wiring (in-process)**: Proactive runtime injects call-model and call-tool closures into the loop; tools execute immediately in-process.
- **Resident wiring (HTTP)**: `chat_resident_consumer` wraps the external agent call (`call_agent`, Hermes CLI/HTTP) in `run_tool_loop_v2` for V2 jobs only (legacy single-shot path untouched). Tool calls go to the new endpoint `POST /v1/proactive/tool/execute`, which runs the shared `ToolExecutorV2` server-side (perception/memory/screen tools; only `screen.read` reaches the enclave) and returns `ToolResultV2.as_dict()` (ok, outcome, result, error_code, needs_background, trace).
- **Budget handoff stops the turn**: when a tool in a turn returns `needs_background`, the loop stops executing that turn's remaining `tool_calls` and defers immediately — avoids wasted inline work (and an HTTP round-trip per call on resident) after the decision to background.
- **Resident tool-only replies survive normalization**: `tool_calls` are now preserved through the resident agent-output normalizer on every transport — the early-return guards in the string/CLI path (`_agent_turn_from_obj`, `call_agent_cli`) and the OpenAI-HTTP path (`_call_agent_http_openai`) previously only treated messages/actions/thinking as "usable", so a tool-only model reply wrapped in any log/header text was flattened to a plain message and the tool never ran. `tool_calls` are also de-duped (one emission could arrive via multiple nested JSON paths, e.g. an OpenAI `choice.message`), so the loop never double-executes a single call.
- **Impact**: `screen.read` and all V2 tools now reachable end-to-end for both user types (hosted + resident). Cross-HTTP-call budget accumulation for resident deferred (iteration cap bounds cost per turn).
- **Changelog + CI**: Added D11 test suite to `.github/workflows/ci.yml` (4 new test files); `tests/test_tool_loop_v2.py` added to pure-unit conftest. No further changes needed.
- **未做**: Native function-calling; cross-HTTP-call budget accumulation for resident; proactive caption-on-change (tool loop is the foundation, not the feature).

## 2026-06-21

### [DONE] Screen frame VLM captioning via in-enclave OpenRouter (Tasks 1-6 complete)
- **Tasks 1-5 shipped**: New enclave route `GET /v1/screen/frames/<id>/caption` decrypts frame IN-ENCLAVE and calls OpenRouter `qwen/qwen3-vl-8b-instruct` via `provider_client`, returning caption text only (never pixels). Backend never holds plaintext pixels. New backend `screen/caption.py` calls that route, caches caption per frame_id. `screen.read`/`screen.recent` tools now implemented in `ToolExecutorV2` for isolated testing.
- **New per-user flag**: `screen_caption_enabled` (default OFF, fail-closed). Enclave env: `FEEDLING_SCREEN_VLM_API_KEY` (required dstack secret; absent → fail-closed `screen_caption_unconfigured`), optional `FEEDLING_SCREEN_VLM_MODEL` (default `qwen/qwen3-vl-8b-instruct`), `FEEDLING_SCREEN_VLM_BASE_URL` (default OpenRouter). Deployed config documented in `deploy/DEPLOYMENTS.md` § Enclave configuration.
- **Task 6 (docs-only)**: Updated `deploy/DEPLOYMENTS.md` with VLM secret + optional env overrides, non-code privacy prerequisites (user disclosure + OpenRouter zero-retention config). Added to changelog.
- **Follow-up (now resolved)**: at the time of this entry the model multi-turn tool-execution loop (D11) was still pending, so these tools were tested in isolation only. D11 landed the same day (see the D11 entry above) — `screen.read`/`screen.recent` are now reachable by the live agent on both hosted and resident paths.
- **未做（不在本计划范围）**: Proactive frame captioning、per-user API key、on-device VLM、legacy caption deletion。

## 2026-06-20

### [DONE] 三个用户开关（陪伴/定时任务/提醒）端到端落地
- **后端**（test `b4386f9` → 合 test）：`/v1/proactive/state` GET/POST 暴露
  `ambient` / `scheduled` / `reminders_delivery`；`scheduled` 升为 first-class
  持久化；映射单一真相（`enabled=ambient`、`dnd=!reminders_delivery`、scheduled
  独立）。**iOS**（main `c1dbbbc`/rebase）：Settings 三个 RailToggle，按 subset-accept
  只发改动键；**清掉死代码** `ProactiveUserState`/`user_state`/`ai_state`（不符合
  D6，且无处引用）。
- **为什么**：spec §8.2 的三层 gate（Wake/Voice/Delivery）需要三个用户可见开关，
  且**定时任务独立于陪伴**（D16，关陪伴不该连坐闹钟）。此前 iOS 根本没有这几个开关
  入口，后端 settings 也只有 enabled/dnd。

### [DONE] 感知能力 iOS↔后端全量打通（parity review 后收口）
- **起因**：一次跨仓库 parity review 发现 iOS 发的一批信号后端不接、后端能 wake 的
  事件 iOS 不发。逐项收口（后端 test `85adbfb`+`dad4900`，CI 绿、镜像已发；iOS main
  `7663631`）：
  - **weather / health（睡眠·运动·体征）/ focus** → 注册为加密 **pull-only** 信号，
    字段名逐字对齐 iOS，走 enclave 解密 + resolver 丢原始；focus 出 `in_focus`
    pull 提示，**删掉映射到已删 user_state 的死代码**（`resolve_focus`/`_apply_focus`）。
  - **audio_route**（蓝牙锚点的可行子集）→ iOS 读 `AVAudioSession.currentRoute`
    （车机/耳机+设备名），加密 pull-only。任意系统级蓝牙连接 iOS 不给第三方 app。
  - **久别解锁** → iOS 在"前台回归 after >30min 空闲"（gap 端上算）发
    `unlock_after_absence`；后端早已接好（零改动）。第三方 app 拿不到可用的硬件解锁
    事件，"重新在场"才是可落地的最直接信号。
  - **WiFi 锚点 wake（§3.3/D13）** → iOS 发 `wifi_anchor_id`＝BSSID 的端上 HMAC
    （真 BSSID 永不出设备）；后端把解密 token 喂差分器产 `arrived_at_anchor`，**仅在
    iOS `changed=true` 时喂**（挡掉"部署后差分器内存态清空+静止用户被批量误唤醒"）。
  - **后台到达 wake（option B）** → iOS 低功耗 SLC + visit 监测、Always 升权、
    `location` 后台模式。Seven 选 B（"一到某地就主动找你"）而非 A（只 pull 上下文）。
- **设计决策（Seven 拍板）**：连续信号一律 pull-only / 不 wake（§3.1/D5）；focus 只作
  pull 在场提示、绝不复活 user_state（D6/D15）；蓝牙走音频路由子集；WiFi 锚点做哈希
  指纹**自动学**（不靠用户命名，D13）。
- **影响文档**：`PROACTIVE_PERCEPTION_SPEC_V2.md`（§2.1/§3.1/§9 B1↓依赖+B1b）、
  iOS `PERCEPTION_BACKEND_TODO.md`，新增 iOS `PERCEPTION_HANDOFF_2026-06-20.md`。
- **仍需（工程师，非代码）**：后台定位整条链真机验证（无法在本机 build/跑）、Apple
  开发者后台开 HealthKit + WeatherKit capability。

## 2026-06-16

### [DONE] 解除"单 worker 天花板"——后端可跑 `-w N`（LISTEN/NOTIFY 唤醒总线 + advisory-lock 选主）
- **动机**：生产 `gunicorn -w 1 --threads 32`，32 线程是全部并发预算，而
  `/v1/chat/poll`、`/v1/proactive/jobs/poll` 天然挂线程（≤30s）。活跃用户一多，
  等待者吃光线程池、正常请求排队 → 已观察到的 prod 慢/502；且永远无法加 worker。
  根因是 4 类绑死单进程的状态：① UserStore 进程内写穿缓存 ② threading.Event
  长轮询 waiter ③ :9998 WS 在 import 期绑端口 ④ 必须单例的 hosted tick/consumer
  + 明文 `last_seen_api_key`（仅内存）。
- **Layer A 跨进程唤醒/失效**：新增 `backend/core/wake_bus.py`，用 Postgres
  LISTEN/NOTIFY（不引新组件）。写 chokepoint（`store.append_chat` /
  `append_proactive_job` / frame 落库 / 注册表编辑）落库后发 `NOTIFY`；每 worker
  一个常驻 listener 收到非自己来源的通知就 `_evict_store`（就地 reload + 唤醒本地
  waiter）。db 层加 `pg_notify` / `listen_connection`（SQL 归 db.py，协议归 core）。
- **暗雷修复**：① chat-poll 的 reply claim 从"读缓存判可领 + 写穿"改成
  `db.chat_try_claim_reply` 的 DB 条件 CAS（两 worker 不再双投同一回复）；
  ② 用户注册表 `_users`/`_key_to_user` 进程内、查 miss 不回库——register/发 key 等
  真实编辑走 `_save_users(broadcast=True)` 发 `users` 通道，各 worker reload，
  否则新用户在别的 worker 会 401。
- **Layer B 单例选主**：新增 `backend/core/leader.py`（`pg_try_advisory_lock`），
  WS ingest 收进 `run_singleton("ws", …)`，只有持锁 worker 绑 :9998、挂了别的
  worker 接管。
- **Layer C hosted wake 分布式 + 按 key 在位执行**（比原计划更简）：发现 job 认领
  已经是 `update_proactive_job(only_if_status="pending")` 的原子状态 CAS，**无需
  新表/迁移**。tick 改成每 worker 各跑、只处理本 worker 持 key 的用户
  （`_hosted_keyholder_user_ids`，创建+模型调用都需明文 key 故必须在 key 所在
  worker 跑）；重复创建用 `db.try_stamp_hosted_tick` 原子心跳槽 CAS 防住；
  `try_consume_pending_for_user` 作 `proactive` 通道 handler 跨 worker 即时认领。
- **compose**：三个 compose `-w 1` → `-w 2`（先小、可灰度再提 N），注释更新；
  改 compose 字面量会改 `compose_hash`，**部署需重新上链**（CONTRIBUTING §8 /
  DEPLOYMENTS.md）。每 worker 约 +17 个 DB 连接（池 16 + listener 1），调大 `-w`
  前核对库 `max_connections`。
- **验证**：本地 `gunicorn -w 2` 端到端——注册落一个 worker 后 whoami 40/40 全 200
  （users 通道）；一 worker 长轮询、另一 worker 发消息，10/10 轮真实停泊后均
  ~10ms 内被唤醒（跨进程唤醒总线）；advisory-lock 选主 + 接管、claim CAS 单赢家
  均有单测/集成验证。全量 pytest 450 passed（仅 2 个预先存在的 enclave 红用例，
  零新增失败）。新增测试：`test_wake_bus`、`test_chat_poll_claim_cas`、
  `test_hosted_wake_distribution`。
- **Codex review 修复（两轮）**：① 注册表所有真实单用户编辑（注册/发 key/key
  恢复/link-token/access-binding/公钥/偏好）从 `_save_users` 全表
  DELETE+重插改成 `registry.persist_user` 单行 upsert + `users` 广播——否则两
  worker 并发编辑不同用户时，陈旧快照全表重写会抹掉对方刚建的用户（已用 -w 2 并发
  注册 16 用户验证零丢失）；`_save_users` 全表重写只留给 normalization/测试。
  ② `load_users()` 整个 reload 包进 `_users_lock`（它现在也在监听线程上跑，与请求
  线程并发改注册表）。③ chat-poll claim 的 DB CAS 补 replied 状态拒绝
  （`reply_status='replied'` / `reply_message_id`），防别的 worker 已回复后本
  worker 凭陈旧缓存重复认领。④ 账号删除路径补 `users` 广播（否则别的 worker 仍
  鉴权已删账号）。⑤ 缓存型 blob（`tokens`/`push_state`/`live_activity_state`/
  `frames_meta`）写 chokepoint 补 `blob`/`frames` 广播——否则别的 worker 用陈旧
  token/推送冷却到 15min TTL，坏掉推送投递/去重；用线程局部 `_reload_guard` 抑制
  reload 期写穿归一化的回广播（防 NOTIFY 风暴）。⑥ **部署安全**：phala / phala.test
  两个 compose 用 pinned 旧镜像（`857c09e`/`b14c3db`，import 期绑 :9998），保持
  `-w 1`，注释写明须与"换含本 patch 的镜像"同一次部署一起提 `-w`；只有带 `build:`
  的 base `docker-compose.yaml` 设 `-w 2`（从源码构建，安全）。
- **影响文档**：CONTRIBUTING §7 不变量（单 worker → 多 worker 已支持）、
  `core/store.py` / `db.py` 模块 docstring、三个 `deploy/docker-compose*.yaml`。

### [DONE] enclave 改用 gunicorn gthread（撤掉 Werkzeug 开发服务器）
- `backend/enclave_app.py` 的入口从 `app.run(threaded=True)`（Flask 自带
  Werkzeug 开发服务器，非生产级 WSGI）换成**编程方式内嵌的 gunicorn**
  （`BaseApplication`）：`worker_class=gthread`、单 worker、32 线程、
  `timeout=120` / `graceful_timeout=30`。单 worker 精确沿用原"单进程多线程"
  模型——进程内 whoami / content-key 缓存与 singleflight（见文件头）保持一致，
  挡住 history-import 触发的回环鉴权线程风暴。
- **关键约束：不动 compose。** gunicorn 内嵌进 `__main__`，compose 入口仍是
  `python -u backend/enclave_app.py`，所以 `compose_hash` 不变、无需重新上链
  （CONTRIBUTING §7 不变量）。
- **保住自签 TLS + cert-DER pinning。** bootstrap() 派生的 PEM 写到 tmpfs 临时
  文件（0600，atexit 清理；TDX 下 /tmp 是内存盘，密钥不落盘）供 gunicorn 翻开
  `is_ssl`；实际 SSLContext 走 gunicorn 的 `ssl_context` 钩子，复刻原
  `_build_ssl_context` 的精确姿态——裸 `PROTOCOL_TLS_SERVER`、min TLS 1.2、无
  客户端证书校验、无 HTTP/2 ALPN，确保握手服务的正是 REPORT_DATA 里 pin 的那张
  leaf，iOS 审计卡的 `sha256(cert.DER)` 校验不受影响。
- gunicorn import 延迟到入口，`import enclave_app`（测试套件）不强依赖它。
- 验证：自签证书冒烟测试确认 gunicorn 起 https、`/healthz` 走 TLS 返回、服务的
  证书指纹 == 注入证书、明文打 TLS 端口被拒、SIGTERM 优雅退出；
  `tests/test_enclave_route_errors.py` 11 例全过。
- `backend/requirements.txt` gunicorn 注释补上 enclave 用法（`ssl_context` 钩子
  需 >=21.0；已 pin >=23）。**部署：只需 bump CVM 镜像，compose 文件不动。**

### [DONE] 文档：补全历史导入端到端流程（RUNTIME_FLOWS §3.6）
- 把 `RUNTIME_FLOWS.md` §3.6 从"两遍蒸馏"一句话扩成完整阶段流水线：
  异步 job + 轮询、job 复用 / stale 判定、解析历史与支撑材料（过滤账号
  元数据）、关系起点与 small/large 分级、时间窗口提候选、聚类写记忆卡、
  派生身份卡、生成开场问候、`chat_ready` 首批放行、large/ultra 后台续抽。
- §4.3 同步补一句指回 §3.6。
- 只动文档，对照 `hosted/history_import.py:_process_history_import_sync` 与
  `_HISTORY_IMPORT_PHASES` 校对阶段名，未改代码。

### [DONE] 收尾：撤未接线的 wake_interval、加固捕获锁、补测试（push 前清理）
- **撤掉死旋钮**：P4 初版加了 `wake_interval_sec`（唤醒频率），但只存储、无
  代码读它生效——正是 P1 批判的"写了没人读"。按"凡发出去的旋钮都得是活的"
  原则，本批从 `core/store.py` 撤掉它（默认/白名单/校验三处），只保留已真接
  线的 `wake_directive`。频率旋钮连同**实际接线 + iOS + 测试**整体延到后续
  （task #6）。
- **加固捕获锁**：P2 给记忆捕获加的每用户锁，存在一个泄漏窗口——`_start_`
  占用后若 `_append_memory_capture_job`/线程启动抛异常，`finish()` 不会执行、
  用户被永久挡在捕获之外。`hosted/turn.py` 用 try/except 包裹交接段，异常时
  释放守卫再抛。
- **补测试（纯单元，本地全过）**：
  - `tests/test_context_memories.py` +3：strict 软召回 `index_sample`（排除已
    选中、上限 20、转折优先）。
  - `tests/test_model_api_wake.py` +2：`user_directive` 进/不进 wake payload。
  - 新增 `tests/test_history_import_identity.py`（5）：`_normalize_identity_payload`
    的语气字段透传/净化/截断。
  - 新增 `tests/test_model_api_prompts.py`（5）：前台 prompt 含 custom_persona_prompt
    优先级指令 + memory_index 召回指令；persona/索引值进 prompt。
  - 两个文件加入 `conftest.py` 的 `_PURE_UNIT` 白名单（无 DB 也收集）。
  - 全量无 DB 跑：**107 passed**；显式 context 跑：62 passed。
- **补测试（DB 依赖，CI 验）**：`tests/test_identity_actions.py` +2，照搬现有
  通过模式——`custom_persona_prompt` 经 profile_patch 可写回身份体；
  `wake_directive` 白名单/截断/拒未知键。本地无 Postgres 跑不了，CI 验。

### [DONE] P4(backend)：proactive 自定义（D2 power-user）；iOS 待做
- D2 定的是"全自定义（默认值 + 高级区）"。本批后端落 `wake_directive`：
  用户自己的"什么时候来找我"自然语言指令（`proactive_settings`，≤1000 字，
  现有 `GET/POST /v1/proactive/settings` 即可读写）。
- **吸取 P1 教训，不加死字段**：`wake_directive` 已**真接进** hosted wake
  prompt——`model_api_runtime/wake.py` `build_wake_event_message` 增可选
  `user_directive`，`hosted/wake_consumer.py` 从 settings 取并传入，wake
  事件 payload 带 `user_wake_directive`，agent 据此权衡发不发（用户指令、
  非硬规则）。
- **未做（明确标注待做，task #6）**：唤醒**频率**旋钮（连同 hosted tick
  cadence + resident consumer 端接线）、**iOS UI**（proactive 设置面板 +
  `custom_persona_prompt` 编辑入口，在 feedling-mcp-ios 仓库）。

### [DONE] P3：API 召回加"软召回索引"（D3 LLM 软召回，加性、可回退）
- **问题**：model_api 的 strict 召回只放 corrections + 词面命中阈值的卡
  （`context_memory_selection.py` 严格分支），语义相关但词面不重叠的卡被
  硬丢——即 feedback point 2"召回太硬，只是文字对应"。
- **改法（加性，不删词面路径）**：strict 分支用**同一批已解密 moments**额外
  构造一个紧凑 `index_sample`（id/type/title/occurred_at，转折优先+最近，
  上限 20），零额外 provider/enclave 调用。`hosted/context.py` 把它作为
  `context_memory_selection.memory_index` 注入 prompt；`prompts.py` 加一句
  指令：标题/日期相关时可自然"想起"，但不得编造标题外细节、也不强行召回。
  → 模型自己软召回，而非关键词过滤替它决定。
- **性质**：现有 `selected`/词面选择完全不变（可回退）；index 只是补充面。
  index 的字段/条数/措辞属可调内容，留待迭代。
- **验证**：`tests/test_context_memories.py` 59 个纯单元测试全过（含本次改的
  strict 分支）；新增 `index_sample` key 不影响既有断言。

### [DONE] P2(API)：history import 蒸馏语气 + 记忆捕获加每用户锁
- **蒸馏语气（修 4a 角色漂移的"蒸馏端"）**：`hosted/history_import.py` 的身份
  派生 `_derive_identity_with_provider` 之前只产
  `agent_name/self_introduction/category/signature/dimensions`，语气只能塞进
  自我介绍。现在 prompt 增产 `tone_style`（怎么说话：语域/口头禅/称呼/句式，
  要求引用真实例句）、`agent_role`、`do_not_say`、`boundaries`；
  `_normalize_identity_payload` 对这四个字段做净化（长度上限、zh/en 一致性、
  list 清洗）并对空值省略。它们随密文身份体落库 → 经 enclave 解密 →
  P1a 已把它们接进 prompt，**蒸馏端 + 读取端闭环**：API 用户的语气现在
  能跨 import 存活，而不只是 fact。
- **记忆捕获加每用户锁（修 USER_PATHS_REVIEW §8）**：`hosted/turn.py` 的状态
  动作和 recap 各有每用户锁，唯独记忆捕获没有——turn-24 的捕获还在跑时
  turn-48 又触发会重叠、产重复卡。新增 `_model_api_capture_active_users`
  守卫：`_start_model_api_memory_capture_job` 入口检查/占用，运行体的唯一出口
  `finish()` 释放（幂等、覆盖所有 return 分支）。镜像 recap 既有模式。
- 性质：prompt 内容（蒸馏措辞）后续可调；字段结构与锁是骨架。语法校验通过。
- **未做（属调参/成本）**：捕获 cadence 仍是默认 24 轮
  （`FEEDLING_MODEL_API_CAPTURE_TURN_INTERVAL` 可配）；要更"持续"可调小，
  但有 provider 调用成本，归 P4 自定义一并考虑。

### [DONE] P1b：新增 custom_persona_prompt 用户可编辑 persona 覆盖槽（D1 用户层 / feedback 4b）
- 用户反馈想要"一个能自己加 prompt 精准定位角色的地方"。新增单个自由文本
  字段 `custom_persona_prompt`，与系统蒸馏的 `tone_style` 分开、优先级最高。
- 借现成白名单机制，改动最小：
  - `identity/service.py`：`custom_persona_prompt` 加入
    `_IDENTITY_PROFILE_STRING_FIELDS`，故 `identity.profile_patch` 自动支持
    写入（iOS 编辑入口留待 P4）。
  - `identity/actions.py`：两处 max_len 把它归入 1200 字一档（自由 prompt
    需要更长，区别于 240 字的短字段）。
  - `hosted/context.py`：接进 `identity_summary`，随 P1a 一并进 prompt（前台
    聊天 + 记忆捕获 + wake 都读 `context_payload["identity"]`）。
  - `model_api_runtime/prompts.py`：前台 system prompt 加一句——
    `custom_persona_prompt` 存在时视为**最高优先级** persona 指令，压过其余
    identity/profile 文本（安全边界除外）。
- 性质：纯加性，零迁移；空值不渲染。四个改动文件语法校验通过。

### [DONE] P1a：把 persona/语气字段接进 hosted 聊天 prompt（修 API 角色漂移根因）
- **背景（决策 D0–D3）**：本轮定了记忆/identity 重设计的四个地基决策——
  D0 卡库=权威外置记忆库（插件模型）、D1 persona 双层（系统蒸馏 + 用户可
  编辑覆盖）、D2 proactive 全自定义（默认值 + 高级区）、D3 召回改 LLM 软
  召回。落地按 P1（schema 地基）→ P2（持续落卡 + 蒸馏语气）→ P3（软召回）
  → P4（自定义暴露）分阶段推进。本条是 P1 的第一个最小落地。
- **诊断**：`tone_style` / `agent_role` / `do_not_say` / `boundaries` /
  `stable_definitions` 等 persona 字段在 identity 密文体里**能写**（经
  `identity.profile_patch`，见 `identity/actions.py` `_IDENTITY_PROFILE_FIELDS`），
  但 hosted 聊天 prompt 的两个 identity 入口（`hosted/context.py`
  `identity_summary` 与 `history_import.py` `_model_api_agent_profile_context`）
  都**不读**它们——persona 是 write-only 死字段。这是 model_api 用户反馈
  "角色漂移"（只蒸馏 fact 不蒸馏语气）的结构性真凶：两头断（蒸馏不写、
  prompt 不读）。
- **改动**：`backend/hosted/context.py` 的 `identity_summary` 补上述 persona
  字段。prompt builder（`model_api_runtime/prompts.py`）是整包
  `json.dumps(context_payload)` 注入、不挑 key，故此一处接线即同时惠及
  **前台聊天**与**记忆捕获 worker**（两者都读 `context_payload["identity"]`）。
- **性质**：纯读取侧加性改动，零 schema 改动、零迁移；字段为空时只渲染空值，
  模型忽略。值在 P2 蒸馏阶段填入——"先接线、后填值"。
- **验证**：语法通过；未跑 DB 测试（本地无 Postgres，按 repo 约定 CI 跑）。
  无测试断言 `identity_summary` 的 key 集合，改动不影响
  `test_identity_actions.py` 的已存断言（它校验存盘明文，非 prompt 摘要）。
- **下一步**：P1b 加"用户可编辑 persona 覆盖槽"（D1 的用户层，4b）——需先定
  字段命名/语义。

## 2026-06-16

### [DONE] 新增 docs/USER_PATHS_REVIEW.md：BPS/API 两路功能总览 + 缺漏盘点
- 把 resident（BPS，自建服务器）与 model_api（API，托管）两条用户路的
  Onboarding / Chat+Memory / Proactive 运行方式并排梳理成一份功能向文档
  （不含加密/部署）。
- Part 2 盘点了一次系统性代码阅读发现的缺漏，按 🔴/🟡/🟢 分级：
  - 🔴 假完成/静默失败：model_api 记忆门槛不分档、实时连接没真验证、
    provider 失败致用户消息孤儿且无退避、resident proactive job 无回收超时。
  - 🟡 状态错乱：中途切路由搁浅、记忆删除无引用完整性、天数锚点漂移、
    后台记忆捕获无每用户锁、`tool_action_enabled` 门形同虚设、import job
    无恢复、official_import 疑似死代码。
- 元观察：claim 租约 / 分档门 / 活性验证 / 每用户锁等"三处都该有"的机制
  普遍只实现一两处——根因是缺一张强制对齐两条路的 capability matrix。
- 文档明示：行号为阅读近似值，修复前需就地核对；条目待逐项落进
  OPTIMIZATION_BACKLOG.md。

## 2026-06-12

### [DONE] 测试文件统一收口到 tests/
- `backend/` 下最后 4 个测试迁入 `tests/`：`test_api.py`（活服务器集成
  脚本）、`test_model_api_wake.py`、`test_perception.py`、
  `test_semantic_analysis.py`；后三个加了 tests/ 惯例的
  `sys.path.insert(..., "backend")` 头。
- 本地全量命令简化为 `pytest tests/ -q --ignore=tests/e2e_model_api_test.py
  --ignore=tests/test_api.py`（不再需要带 `backend/`）；CI 的
  test_api.py 调用路径同步更新。
- `tests/conftest.py` 的「无 Postgres 全部跳过」改为豁免 `_PURE_UNIT`
  集合（semantic_analysis / model_api_wake / perception / provider_client），
  没有数据库的机器仍能跑 95 个纯单元用例。
- CONTRIBUTING.md §1 决策表与 §6 测试规范新增硬规则：测试只放 tests/。
- 验证：418 通过 + 2 个已知长期红，零新增失败。

### [DONE] 新增 CONTRIBUTING.md：后端代码组织规范
- 把拆分重构沉淀成团队规则：app.py 只做装配；新路由进领域包 Blueprint、
  新逻辑进 service 层；依赖只准向下（向上用注入钩子）；跨模块调用
  `module.func()` 形式保证 monkeypatch 单点生效；全局单例只就地变更
  不重绑；COMPAT 段只减不增；单文件 800 行预警 / 1500 行强拆；附 PR
  自查清单。
- `CLAUDE.md` 阅读顺序加入该文档（写后端代码前必读）。


## 2026-06-12

### [DECISION][DONE] 移除 MCP 用户条线（路由 A）
- **拍板**：不再支持 MCP 客户端（Claude.ai / Claude Desktop）直连这条
  用户线。现存接入只剩路由 B（Resident Consumer）和路由 C（Model API
  托管）。
- **删了什么**：
  - `backend/mcpsrv/`（13 文件 ~2,180 行）+ `backend/mcp_server.py` 入口
    + `backend/acme_dns01.py`（MCP LE 证书插件，383 行）
  - consumer 的 MCP 解密回退路径（`tools/chat_resident_consumer.py` 的
    `FEEDLING_MCP_URL`/`_fetch_from_mcp`/transport 探测，~250 行）——
    **resident 用户现在必须配置 `FEEDLING_ENCLAVE_URL` 直连 enclave**
  - `tests/test_mcp_session_isolation.py`（2026-05-11 P0 回归套件，保护
    对象已不存在）+ consumer 测试里 5 个 MCP transport 用例；CI 同步去掉
  - 三个 docker-compose 的 `mcp:` 服务块、ingress 的 `mcp.feedling.app`
    域名/路由、`FEEDLING_MCP_TLS_IN_ENCLAVE`；`deploy/feedling-mcp.service`、
    Caddyfile 的 mcp 站点、SELF_HOSTING Option B
  - `fastmcp` 依赖（requirements.txt + lock 重新生成，纯减 587 行）
- **attestation 兼容**：enclave 删除了 MCP 证书指纹派生路径
  （`MCP_TLS_IN_ENCLAVE` / `MCP_TLS_KEY_PATH`），但 bundle 里保留
  `mcp_tls_cert_pubkey_fingerprint_hex` 字段恒为空——iOS 审计卡走既有的
  "Pre-Phase-C.2 deployment" 披露行，不破坏解析。生产 compose 本就设
  `FEEDLING_MCP_TLS_IN_ENCLAVE=false`，行为一致。
- **留了什么（不是 MCP 专属）**：bootstrap 门禁、`/v1/chat/verify_loop`、
  consumer 心跳、`official_import` access_mode（默认值未动，是否砍另议）、
  enclave 解密端点、identity/memory 的 HTTP envelope-action 端点。
- **部署影响**：compose 变更 → 需要新 compose_hash 上链；Cloudflare 的
  `mcp.feedling.app` CNAME/TXT/CAA 记录可清理。
- **外部跟进（其他仓库）**：io-onboarding 的 `skill.md`（MCP agent 说明书）
  需改写或归档；iOS 的 `ChatEmptyStateView.skillURL` 入口与 MCP String
  相关 UI 需同步调整。
- 文档同步：PROJECT_OVERVIEW（§5.2/§5.5 墓碑化 + 拓扑图）、RUNTIME_FLOWS
  （顶部历史注记）、AUDIT.md（row 5/7 措辞）、README、SELF_HOSTING。


## 2026-06-12

### [DONE] backend 单体拆分：app.py 17.6K 行 → 14 个领域包 + 898 行装配层
- 按「功能域分包为主 + hosted 条线单独成包」拆分（方案见 2026-06-11 拍板）：
  `core/`（config/util/enclave/envelope/store——UserStore+缓存）、`accounts/`
  （registry/auth/onboarding/access/recover/routes）、`push/`（apns/tokens/
  live_activity/service/routes）、`screen/`（frames/ws/summary/routes）、
  `proactive/`（service/gate/dashboard/routes）、`identity/`、`memory/`、
  `bootstrap/`（gates+routes）、`chat/`（service/consumer/routes/verify_loop）、
  `tracking/`、`admin/`、`content/`、`hosted/`（model_api 托管条线 8 模块：
  config_store/setup_routes/history_import/context/turn/chat_routes/
  onboarding_validation/wake_consumer）。`mcp_server.py` 2,029 行 → `mcpsrv/`
  包（session/client/server/tools_{push,screen,chat,identity,memory,meta}/tls）
  + 75 行入口。
- 路由全部转 Blueprint，url_map 与拆分前逐条 diff 为零；gunicorn `app:app`
  入口、四容器部署拓扑、`python -u backend/mcp_server.py` 入口零改动。
- 解耦手段：`core/store.py` 新增 `on_proactive_job_appended` 钩子（替代
  UserStore→hosted 的向上调用）；`core/envelope.get_user_public_key`、
  `push/live_activity.load_identity`、`hosted/wake_consumer.flask_app`、
  admin 的 onboarding 验证函数均由 app.py 装配段注入。`_load_users` 改为
  `_users[:]` 就地替换（避免 re-export 分叉）；pepper 改 lazy（import 不再
  要求 DB 可达）。
- 决策变更：`hosted_runtime.py` 与 `model_api_runtime/` 保持原位不吸收进
  hosted/（本就是独立清晰模块，吸收只增加 shim 风险）。
- app.py 仍保留「COMPAT re-exports（迁移期）」段 + hosted 符号兜底回灌循环，
  供测试/工具按旧路径取符号；收敛为白名单是后续独立 PR（见 backlog #6）。
- 测试：436 通过，与拆分前基线完全一致（仅剩 2 个迁移前就长期红的
  enclave 依赖用例）；测试的 monkeypatch 目标已同步迁到新模块。
- 依赖层级（低→高）：db/content_encryption/provider_client → core →
  accounts → push/screen → proactive/identity/memory → bootstrap.gates →
  chat → tracking/admin/content → hosted → app.py（装配）。跨模块调用一律
  `from pkg import module` + `module.func()`，保证 monkeypatch 单点生效。


---

## 2026-06-07

### [DONE] UserStore 缓存加 TTL + 定向 evict 接口（修 register-orphan 恢复不可见）

- **背景**：排查"内测者历史 chat/memory 全没了"，只读直连 prod RDS 证实**数据没丢、
  可解密**——是 `/v1/users/register` 在重装/重连时铸新空账号、孤儿化老账号（详见
  `docs/orphan-account-recovery-plan.md` 与 `tools/recover_orphan_accounts.py`）。合并恢复
  后发现 `/v1/chat/history` 仍显示旧值：chat 读的是进程内 `UserStore.chat_messages`
  缓存，`_load_chat` 只在 store 首建时读一次 DB、之后常驻，无良性重载接口（只有
  delete-all-data 里的 `_stores.pop`）。memory/identity/model_api 实时读 DB，合并后即刻可见。
- **改动**（`backend/app.py`）：`gunicorn -w 1` → 全后端单一共享 `_stores`。UserStore 是
  写穿透缓存（DB 唯一真相），故可安全丢弃重建。
  - `get_store` 加 **TTL**（`STORE_CACHE_TTL_SECONDS=900`）+ `loaded_at`，过期即刷新。
  - 新增 `UserStore.reload()` + `_evict_store()` + `POST /v1/admin/store/evict`（admin
    鉴权）。TTL 与 evict 都走**原地刷新（refresh-in-place，保持对象身份不变）**而非换对象——
    避免 Codex review 指出的竞态：若某请求在过期/驱逐前拿到旧 store、在新对象装入后才写入，
    写会落 DB 但只进旧实例，新实例漏看（chat 走内存缓存不实时读 DB）。原地刷新下永远只有一个
    实例，写穿透不被遮蔽；`reload()` 在 `chat_lock` 内重读，与并发 append 串行化、不丢写。
  - 刷新时 `_wake_store_waiters()` 唤醒长轮询 waiter（chat/proactive），让 park 的 poll 立即
    返回重连、读到刚浮现的消息。
  - 恢复工具 survivor 选择修复（Codex P1）：`_pick_survivor` 改为优先「有 live api_key + 最新
    注册」而非「chat 量最大」——否则刚重装、暂无 chat 的活跃账号会输给有 chat 的死孤儿，把数据
    搬进死账号。修复后 dry-run 还多识别出 1 个被旧逻辑漏掉的用户（`hlv5AVd7…`，98 chat/39 mem/
    identity/model_api 滞留死账号）。
- **部署即生效**：部署本次改动会重启 backend → 冲掉所有陈旧 store → canary 测试者
  `usr_0f93a433a006b702` 的 209 chat 当场现身；之后带外改动走 evict/TTL，不必再重部署。
- 测试：新增 `tests/test_store_cache.py`（TDD，5 项：TTL 命中/过期重载、evict 失效/鉴权/
  刷出带外写）。全套 `tests/` **316 passed**（唯一 1 个 `test_model_api...relationship_days`
  失败为既有、需 enclave attestation 可达的环境依赖，已 git stash 在干净树复现证明无关）。

### [DONE] 密钥找回账号（堵住 register-orphan 根因：换机/重装丢 api_key）

- **根因确认（确是 app 程序问题）**：DB 证据——同一 public_key 名下的账号在**同一秒/隔
  2–3 秒**被批量创建（monster lineage 16s 内 6 个），不可能是人手 → 客户端并发/循环连发
  `/v1/users/register`。两个子 bug：(A) **爆发型**（并发 register）——iOS 已用
  `registrationTask` 串行化修掉；(B) **换机/重装型**——仍漏：内容密钥对走 iCloud Keychain
  **同步**跨设备存活，而 api_key 为修 5/10 重启竞态被改成**仅本机**，换机恢复时密钥对在、
  api_key 没了，守卫标记（UserDefaults）又被重装清空 → 照常 register → 同密钥铸新孤儿。
- **修复（keypair proof-of-possession 找回）**：让"会同步存活"的密钥对当身份锚。
  - backend（`app.py`）：`POST /v1/account/recover/challenge`（按 public_key 找规范账号
    `_canonical_account_for_pubkey`，`build_envelope` local_only 把随机挑战封给该公钥）+
    `POST /v1/account/recover/verify`（设备回传解密结果证明持私钥 → 为既有账号
    `_issue_api_key_for_user_locked` 重签 api_key，**不铸新账号**）。挑战一次性 + 300s TTL +
    `hmac.compare_digest`；复用现有信封 scheme，iOS 用既有解密路径即可解。
  - iOS（`FeedlingAPI.swift`）：`ensureRegisteredIfCloud` 在 register 前先调
    `recoverViaKeypairIfPossible()`——用 `loadPrivateKeysForDecryption()`（含**可同步槽**，
    换机后存活的那把）遍历候选公钥走 challenge/verify，成功即 `setCredentials` 不再 register；
    404/离线/无密钥则回退原守卫与注册。
  - **Codex review 跟进修复（iOS）**：(P1) 把 keypair 找回纳入既有 `registrationTask` 单一在途
    任务（抽出 `acquireCloudCredentials()`）——`@MainActor` 下 guard→任务安装之间无 await 即原子,
    避免并发启动各自找回、各领一把 api_key 互相覆盖；(P2) `wipeLocalAccountState()`（"删除我的
    数据"/重置走这里）补 `DiagnosticLog.shared.clear()`,清掉残留 userId/agent 名等可导出 PII；
    (P3 二轮) 找回从 Bool 改为三态 `KeypairRecoveryOutcome{recovered/noAccount/transientFailure}`:
    **只有"对所有候选公钥都确定 404"才允许注册**;离线/超时/5xx/解密失败等瞬时错误返回
    `transientFailure` → 阻止注册并重试,不再因瞬时失败而铸新号孤儿化(纵深防御,与服务端去重叠加)。
  - **服务端兜底（register 去重）**：`/v1/users/register` 收到**已有账号的 public_key** 时
    直接返回 **409**（提示走找回），**绝不铸第二个账号**。这关一拦，无论客户端在线与否、
    版本新旧、iCloud 同步时序如何，孤儿都创建不出来。空 public_key（legacy 客户端）仍放行；
    Reset-and-reimport 先清密钥对 → 新公钥，不受影响。
- 测试：`tests/test_account_recover.py`（8 项 TDD，含**完整 X25519+ChaCha20 往返**证明与
  iOS `box_seal`/`unseal` 线格式一致、多账号共用公钥落到最新活跃账号、错答/重放/未知公钥拒绝、
  register 去重拒绝重复公钥/放行新公钥/放行空公钥）。因去重改动同步把 `test_data_track` 的
  注册 helper 改为每次唯一公钥。全套 `tests/` **326 passed**（同上 1 个既有 enclave 环境依赖
  失败无关）。iOS 侧无法在此跑 XCTest，需设备/CI 构建验证。

---

## 2026-06-02

### [DONE] 引入 Alembic 管理数据库 schema

- schema 改为由 **Alembic 单一真相源**管理（`backend/alembic/versions/`），取代
  原先 `db.init_schema()` 里内联的一段 `CREATE TABLE IF NOT EXISTS` DDL。
- `db.init_schema()` 现在跑 `alembic upgrade head`（programmatic），从
  `DATABASE_URL` 读连接（`backend/alembic/env.py` 把 `postgresql://` 映射到
  psycopg3 方言 `postgresql+psycopg://`）。app 启动、migrate 容器、测试 conftest
  都走同一条路，不会双真相源漂移。
- baseline 迁移 `0001_baseline` 用幂等 DDL（`IF NOT EXISTS`），所以对**已经建好
  8 张表的线上 RDS** 安全——下次部署 `upgrade head` 时只是 no-op + 记录
  `alembic_version=0001_baseline`，不动数据。
- 依赖：`requirements.txt` + `requirements.lock`（uv 重新生成）加 `alembic>=1.13`
  （带 SQLAlchemy 2.x，仅用于驱动迁移；请求路径仍走 psycopg pool）。
- 脚手架：`backend/alembic.ini`、`alembic/env.py`、`alembic/script.py.mako`、
  `alembic/versions/0001_baseline.py`。已确认 `.dockerignore` 不排除、Dockerfile
  `COPY backend/` 会带进镜像。
- **后续改 schema 的流程**：`cd backend && alembic revision -m "..."` 写迁移 →
  提交 → 部署时 `init_schema()`（=`upgrade head`）自动应用；也可手动
  `DATABASE_URL=... alembic upgrade head`。
- 验证：本地临时 PG 实测「全新库建表+打戳 / 幂等重跑 / 已有表的库安全 no-op+打戳」
  三场景全过；`alembic current|history|revision` CLI 正常；全套测试 **213 passed**。

### [DONE] 老数据迁移方案：Phala CVM 内一次性 migrate 容器

- 老用户数据在 Phala CVM 的 `feedling_backend_data` volume（`/data` 下的旧
  JSON/JSONL 文件）。TDX enclave 不便把数据导出，故采用「CVM 内自迁移」：
  `docker-compose.phala.yaml` 新增一次性 `migrate` service（同镜像，挂同一
  `feedling_backend_data` volume，注入 `DATABASE_URL`，跑
  `migrate_to_pg.py --data-dir /data`），`backend` 通过
  `depends_on: migrate: condition: service_completed_successfully` 等它跑完再起。
- **防重复覆盖**：`migrate_to_pg.py` 加 `migration_done` 标记（写在
  `server_config`）。首次成功后置标记，之后每次启动（CVM 重启会重跑该一次性
  容器）检测到标记即 no-op，绝不用旧文件覆盖用户已写入 RDS 的新数据。`--force`
  可强制重跑。→ 该 migrate service 可永久留在 compose；确认无误后也可在后续
  常规部署中移除（移除会再变 compose_hash → 多一次 attest）。
- 加 service 会改变 compose_hash → 上链 + iOS consent（部署本就会弹）。
- 验证：本地临时 PG 实测「首次迁移 → 重启 no-op 保留新数据 → --force 重导入」
  三场景全过；`docker compose config` 解析新 compose 正常。
- 线上 RDS 8 张表已建好（init_schema 幂等，启动自动建）；唯一需要的 GitHub
  secret `DATABASE_URL` 已配置。

### [DONE] 迁移已完成 + 移除 migrate service（修 starting）

- **迁移事实上已完成**：线上含 migrate service 的版本部署后，`migrate` 在
  2026-06-02 03:42 UTC 跑完，把老数据导入 RDS 并置 `migration_done` 标记。RDS
  现有 107+ 用户、chat 时间跨 2026-04-21~至今、且在实时增长（backend 正常服务）。
- **CVM starting 的成因 + 处理**：部署期间 CVM 一度长时间停在 `updating/starting`
  （一次性 `restart:no` 容器退出 + `depends_on: service_completed_successfully`
  在 dstack-dev-0.5.8 下的表现），最终自行恢复 `running`。为避免复发，已从
  `docker-compose.phala.yaml` **移除 `migrate` service 及 backend 的
  `depends_on: migrate`**——迁移已完成、`migration_done` 标记已在，不再需要它。
- 安全性：`migration_done` 已设 → 即便将来再跑 migrate 也永久 no-op（除非
  `--force`），不会覆盖 RDS 现有数据。
- 后续如需再迁移：手动运行 `migrate_to_pg.py`（marker 会让它 no-op，需要时加
  `--force`），不再走常驻 compose service。

### [DONE] 数据缺失补救工具：migrate_to_pg 加 --merge + SSH 执行路径

- 用户反馈 03:42 那次迁移**不完整**（RDS 缺数据）。`migrate_to_pg.py` 新增
  **`--merge`** 模式：跳过 `delete_user_data`，只 upsert/append → 补回缺失数据
  而**不回退** live backend 在迁移后写入 RDS 的新行（row-per-item 表按 id 幂等
  upsert；append-only user_logs 可能产生重复，非关键）。`--merge` 隐含绕过
  `migration_done` 标记。本地临时 PG 验证：现有新数据保留 + 缺失用户/记录补回。
- **执行路径**（TDX CVM 内,SSH 直连默认被 publickey 挡）：下次
  `phala deploy ... --ssh-pubkey ~/.ssh/id_rsa.pub` 注入 SSH 公钥后,可
  `phala ssh <cvm> -- docker exec <backend容器> python backend/migrate_to_pg.py
  --data-dir /data --verify|--merge` 精确对账与补缺。容器名用 `phala ps` 确认。
- 三种模式:`--verify`(只读对账文件 vs RDS)、`--merge`(安全补缺)、`--force`
  (全量 delete+reimport,会回退增量,仅维护窗口用)。

---

## 2026-06-01

### [DONE] PostgreSQL 迁移扩展覆盖新功能 + 解决 stash 冲突

- 迁移期间 main 合入了新功能（`Add identity and memory agent actions`、
  `Add beta data track dashboard` 等），`git stash pop` 在 `backend/app.py`
  留下 3 处未解决的冲突标记（"Updated upstream" vs "Stashed changes"）。已逐一
  解决：保留上游新功能 + 保留迁移的 `import db` / db 持久化。
- 修复合并引入的断裂引用（上游新代码调用了迁移已删除的方法）：
  `_set_user_public_key` / content-rewrap 里的 `_persist_chat()`、
  tracking/memory dashboard 里的 `self._append_jsonl` / `store._read_jsonl`
  —— 全部改为 `db.*`。
- 把新功能引入的文件持久化也迁到 PostgreSQL：`tracking_events` /
  `memory_changes` / `memory_capture_jobs` 三个 JSONL 流 → `user_logs`；
  `onboarding_route` / `model_api` → `user_blobs`；`history_import_jobs`
  （原每任务一个 .json 的目录）→ `user_blobs`（kind 前缀 `history_import_job:`）。
  无需改 schema（复用 user_logs / user_blobs 表）。
- `db.py` 新增 `delete_blob` / `list_blobs(prefix)`，删除 `app.py` 中已无用的
  `_read/_write_json_object`、模块级 `_append_jsonl`、全部 `_*_file` 路径属性。

### [DONE] 第二轮：合并再次拉取的后端 + 覆盖用户模型重构

- 又拉取了后端代码（`Keep history import out of visible chat`、
  `Omit oversized chat bodies from lightweight history` 等），`backend/app.py`
  再次出现冲突标记，已解决。
- **用户模型重构**：上游把 users 从单 `api_key_hash` 改成富模型
  （`principal_id` + `api_keys[]` 多密钥/吊销 + access bindings），且 `_save_users()`
  被重新引入并在 11 处调用。把 `db.py` 的 `users` 表改为**整文档 JSONB**
  （`user_id` PK + `doc`），新增 `save_all_users`；`_save_users()` 改为 db 持久化，
  所有 key 管理调用点无需改动即可工作。
- 新增的文件持久化继续迁到 DB：`access_link_tokens`（全局）→ 新增 `global_blobs`
  表 + `get/set_global_blob`。修复上游 `_set_user_public_key` 调用已删除
  `_save_users` 的断裂引用。
- 迁移脚本同步扩展：覆盖整文档 users（保留 api_keys/principal_id，api_key 仍有效）、
  onboarding_route / model_api blob、tracking/memory_changes/memory_capture 日志、
  history_import_jobs（每任务一 blob）、全局 access_link_tokens。
- 适配新增功能测试文件中残留的文件存储断言（`USERS_FILE` monkeypatch、
  `identity_file`/`memory_file`/`model_api.json` 直读、`_persist_chat`）→ 全部改走 `db.*`。
- 验证：全套测试对真实 Postgres 全绿（**206 passed**），迁移 e2e（含新用户模型 +
  全部新文件 + 全局 blob + api_key 存活）通过。后端仍无任何用户数据落地文件。

---

## 2026-05-31

### [DONE] 持久层从本地文件迁移到外部 PostgreSQL

- 新增 `backend/db.py`：psycopg3 + 连接池存储层，承载所有原本写在
  `FEEDLING_DATA_DIR` 下的 JSON/JSONL 文件。Schema 为混合模型：小的单例
  blob（push_state / live_activity_state / tokens / proactive_settings /
  identity / bootstrap / consumer_state / frames_meta）进 `user_blobs` KV 表；
  高频集合用 row-per-item 表（`chat_messages` / `memory_moments` /
  `frame_envelopes` / `user_logs`）。全局 `users` 表 + `server_config`（pepper）。
- `backend/app.py`：`UserStore` 与全局 users 注册表保留各自的内存缓存与
  `threading.Lock`，只把 `_load_X`/`_save_X` 的函数体换成调用 `db.py`。仍是单
  gunicorn worker；长轮询 waiter 仍基于内存 `threading.Event`，未占用 DB 连接。
  chat 不再每次重写整个文件——append 现在是单行 INSERT + 有界 trim（O(1)）。
- 加解密**未改动**：服务器从不解密，envelope 的 `body_ct`/`nonce`/`K_user`/
  `K_enclave` 等字段逐字节存为 JSONB 并原样返回，enclave 解密路径不受影响。
  `/v1/content/swap` 的可见性切换（含 K_enclave 增删）走整行替换以保证语义。
- 新增 `backend/migrate_to_pg.py`：一次性、幂等地把现有文件树导入 PG。**会导入
  `.pepper`**，使既有 api_key_hash 继续有效（否则所有 api_key 失效）。`--verify`
  复核计数。文件系统保留作回滚。
- 依赖：`requirements.txt` + `requirements.lock`（uv 重新生成，含 hash）加
  `psycopg[binary,pool]>=3.2`。
- 部署：`docker-compose.yaml` / `docker-compose.phala.yaml` /
  `feedling.env.example` 加 `DATABASE_URL`（`:?` 形式，未设置即 fail-fast；
  必须 `sslmode=require`）。
- **为什么 / 安全影响**：选了外部托管 PG（数据离开 enclave）。数据在库内是
  E2E 密文，但库能看到明文元数据（user_id / 时间戳 / app 名 / visibility）。
  `DATABASE_URL` 因是 `${VAR}` 插值不进 compose_hash，运营方理论上可重定向到
  另一个 PG 而不重新 attest——已在 compose 注释里标注为 attested config 并提示
  需在部署信任文档中披露。

---

## 2026-05-21

### [DONE] Resident onboarding path made reusable after live iPhone test

- Re-centered onboarding on an independent `feedling-chat-resident` /
  IO resident consumer service for Hermes / OpenClaw / Mac / server agents.
  The live path is poll `/v1/chat/poll` → call the agent HTTP/CLI entry →
  POST `/v1/chat/response`, verified by `feedling_chat_verify_loop`.
- Simplified iOS onboarding copy to three handoff items: skill URL,
  path-specific IO connection details, and a short start prompt. Detailed
  CLI/HTTP/systemd choices now live in the public `io-onboarding` skill.
- Updated resident consumer docs and examples to use Hermes/OpenClaw CLI
  `HERMES_HOME=<real profile> hermes chat -Q --source tool --max-turns 60 -q "{message}"`,
  persist session id for `--resume`, avoid wrapper persona prompts, and keep
  user-visible fallback templates off by default.
- Updated README inventories for current verification endpoints/tools and
  clarified that direct MCP is enough for bootstrap/tool calls, while reliable
  ongoing IO Chat needs an always-on owner.

---

## 2026-05-18

### [DONE] Onboarding protocol docs synced to floor-based memory standard

- Synced backend bootstrap instructions and README bootstrap flow with the
  public `io-onboarding` skill: Step 0, four memory passes, 7-dimension
  identity, verify tools, chat-loop verification, and broadcast as the final
  onboarding step.
- Changed memory verification from target-based gating back to floor-based
  gating: relationship floors are still exposed, but hitting the floor now
  passes unless metadata issues are present.
- Clarified `docs/DESIGN_E2E.md` as historical derivation rather than current
  wire-format source of truth, and refreshed deployment records from live
  `/attestation` (`b1e72a6`, compose hash `0xf09f1ddc...`).

---

## 2026-05-14

### [DONE] Production docs and privacy sweep after prod9 redeploy

- Refreshed live deployment records against `/attestation` after the
  privacy cleanup redeployed prod9: running commit
  `0573be37114c61ef2d55bf36ac57c2f06e1bdc7f`, compose hash
  `0x01dd452868a645a830642af6e122e882f34a40a436d22e4ad4a2978e1dd6570f`.
- Removed stale private deployment references from the production compose,
  made `tools/audit_live_cvm.py` portable instead of using a local absolute
  path, and updated sample attestation URLs to the current GitHub repo.
- Re-ran the targeted privacy/stale scan, DCAP parser tests, syntax check,
  and GitHub Actions CI/publish flows.

---

## 2026-05-13

### [DONE] README caught up to prod9 pure-CVM architecture

- Updated `README.md` to describe the current production shape:
  `dstack-ingress`, Flask backend, FastMCP, and enclave all run inside
  the Phala prod9 TDX CVM.
- Rewrote stale VPS/Caddy and Phase C.2 MCP-TLS claims: custom-domain
  TLS now terminates at `dstack-ingress`; the attestation port keeps
  its own pinnable TLS; content privacy rests on v1 envelopes sealed to
  `enclave_content_pk`.
- Refreshed the audit command, status checklist, deploy notes, HTTP
  endpoint inventory, MCP tool count, and config reference to match the
  current source.
- Updated `tools/README.md` so the audit utility snippet uses the
  current env+curl flow instead of the retired `--cvm-url` flag.
- Updated `docs/AUDIT.md`, `docs/DESIGN_E2E.md`,
  `deploy/DEPLOYMENTS.md`, `deploy/BUILD.md`,
  `ios/FeedlingDCAP/README.md`, and `CLAUDE.md` for prod9/current-state
  clarity and redacted retired host/user/APNs identifiers from tracked
  docs.
- Corrected the changelog preamble itself now that `HANDOFF.md` is gone.

---

## 2026-05-10

### [DONE] CVM deploy CI now follows the repo's GHCR owner

- Fixed the `deploy-cvm` job after the repo/package moved from
  `account-link` to `teleport-computer`: CI was publishing
  `ghcr.io/teleport-computer/feedling:<sha>` but waiting for
  `ghcr.io/account-link/feedling:<sha>`, so it timed out before
  `phala deploy`.
- `ci.yml` now derives the GHCR owner from `github.repository_owner`,
  checks the image with `docker manifest inspect`, and pins
  `deploy/docker-compose.phala.yaml` to the same owner dynamically.
- Updated the current Phala compose image references to
  `ghcr.io/teleport-computer/feedling:*`; the next push to `main`
  should publish the new image, pin the compose to the new short SHA,
  and continue through the real CVM deploy.

---

## 2026-04-21

### [DONE] Code + CI ready for prod9 migration (pure-CVM, ingress-terminated)

Endgame direction per user: VPS is going away, prod users re-onboard from
scratch, dstack-ingress 2.2 inside the CVM terminates TLS for both
`api.feedling.app` and `mcp.feedling.app`. Required prod9 (only gateway
that supports `_dstack-app-address.<domain>` TXT routing per
dstack-tutorial 04) — new node → new app_id → new compose_hash to
authorize on-chain.

**Compose** (`deploy/docker-compose.phala.yaml`):
- Added `ingress` service: `dstacktee/dstack-ingress:2.2@sha256:d05a7b3…`,
  multi-domain mode (`DOMAINS` newline-list, `ROUTING_MAP
  domain=host:port` to backend:5001 and mcp:5002), mounts
  `/var/run/tappd.sock` per upstream multi-domain example.
- `mcp` service dropped its in-enclave ACME: `FEEDLING_MCP_TLS=false`,
  removed CF env + `/var/run/dstack.sock` mount + the
  `feedling_mcp_tls_data_v2` volume.
- `enclave` service adds `FEEDLING_MCP_TLS_IN_ENCLAVE=false` — see
  backend change below.
- Dry-run `compose_hash` with `:78b51a6` pin =
  `0x1f0169bab4b1ee19058bd72bdb1fb46cc9b1b9de75a1e2a348134959c908efb9`
  (real hash TBD after CI repins image).

**Backend** (`backend/enclave_app.py`): new env var
`FEEDLING_MCP_TLS_IN_ENCLAVE` (default `true` for backward compat) gates
the `mcp_tls_cert_pubkey_fingerprint_hex` derivation. When false the
field stays empty in the attestation bundle — iOS falls through to the
existing "Pre-Phase-C.2 deployment" disclosure row, and
`audit_live_cvm.py` Row 8 becomes a pass-with-disclosure.

**iOS** (`testapp/FeedlingTest/CVMEndpoints.swift` NEW + edits): all
four URL shapes (attestation, ws ingest, api, mcp) now come from a
single `CVMEndpoints` enum driven by `appId` + `gatewayDomain`.
Overridable via `FEEDLING_CVM_APP_ID`/`FEEDLING_CVM_GATEWAY_DOMAIN` env
or UserDefaults. `FeedlingAPI.swift`
(`resolveIngestWSEndpoint`+`attestationURL`) and `AuditCardView.swift`
(3 sites) no longer hardcode app_id or gateway. Registered new file in
the xcodeproj (PBXBuildFile + PBXFileReference + Group + Sources
phase). Defaults still point at prod5 so pre-cutover builds work; flip
to prod9 in a follow-up commit once app_id is known.

**Broadcast extension**: `SharedConfig.defaultIngestEndpoint` replaces
three `ws://[retired VPS IP redacted]:9998/ingest` fallbacks. Extension is a
separate target and can't import `CVMEndpoints`; the real endpoint is
still written by `FeedlingAPI.init` to App Group UserDefaults, so the
fallback only matters on very first broadcast.

**Audit tool** (`tools/audit_live_cvm.py`): default URLs derived from
`FEEDLING_CVM_APP_ID`/`FEEDLING_CVM_GATEWAY_DOMAIN` env. Row 8
(`mcp_tls_cert_pubkey_fingerprint_hex`) treats empty value as
pass-with-disclosure (ingress-terminated TLS; content-layer envelope
crypto remains the real trust boundary).

**CI** (`.github/workflows/ci.yml`): `deploy-vps` job deleted;
`deploy-cvm` now gates on the test jobs directly. Added
`FEEDLING_COMPOSE_FILE: deploy/docker-compose.phala.yaml` to the
`publish-compose-hash.sh` step — fixes a pre-existing bug where the
script hashed `docker-compose.yaml` (local-dev compose) instead of the
phala compose.

**Validation (2026-04-21)**:
- `docker compose -f deploy/docker-compose.phala.yaml config --quiet` OK
- `python -m compileall backend tools` OK
- `xcodebuild` for scheme `FeedlingTest` succeeded (iPhone 17 / iOS 26.4 sim)
- `xcodebuild` for scheme `FeedlingBroadcast` succeeded
- Compose_hash dry-run reproducible.

**Next (fully CI-driven — two `workflow_dispatch` triggers, no manual
CLI)**: trigger `bootstrap-prod9.yml` with `confirm=yes` → it purges
stale CF records, `phala deploy`s to node 18, polls for LE readiness,
publishes compose_hash, flips `CVM_ID` repo var, auto-commits iOS
`CVMEndpoints` bump `[skip ci]`. Run `audit_live_cvm.py` + fresh iOS
install to confirm 8/8 + 6/6. Then trigger `retire-prod5-vps.yml` with
`confirm=yes-delete-prod5` → `phala cvms delete` prod5, SSH stop+mask
VPS systemd units + tombstone, purge retired VPS DNS from CF, delete
stale `VPS_*` repo vars/secret. `HANDOFF.md` was later retired; current
deployment state lives in `deploy/DEPLOYMENTS.md`.

### [DONE] Bootstrap + retire workflows for CI-driven prod9 migration

Added `.github/workflows/bootstrap-prod9.yml` and
`.github/workflows/retire-prod5-vps.yml`. Both are `workflow_dispatch`
only with mandatory `confirm` inputs.

- `bootstrap-prod9.yml` does the full "stand up replacement CVM" flow
  (purge conflicting CF records → `phala deploy -c phala-compose
  --node-id 18 -j --wait` → readiness probes → publish compose_hash →
  `gh variable set CVM_ID` → auto-commit iOS `CVMEndpoints` defaults
  bump tagged `[skip ci]`). Pre-flight gate aborts if a CVM named
  `feedling-enclave-v2` already exists (prevents double-deploy).
- `retire-prod5-vps.yml` deletes the prod5 CVM, SSHes the VPS as
  `[retired service user]` and stops/disables/masks the `feedling-backend`
  + `feedling-mcp` systemd-user units (drops `~/RETIRED.md`), purges
  any CF record still pointing at `[retired VPS IP redacted]`, and removes
  `VPS_HOST`/`VPS_USER`/`VPS_DEPLOY_KEY` from repo state. Safety
  gate refuses to run unless `CVM_ID` has already been flipped away
  from the hardcoded prod5 UUID (i.e., bootstrap ran successfully
  first).

Zero secrets move through a human. Everything runs on GitHub-hosted
runners using the repo's existing `PHALA_CLOUD_API_KEY`, `CF_API_TOKEN`,
`CF_ZONE_ID`, `ETH_DEPLOYER_KEY`, and `VPS_DEPLOY_KEY`.

---

## 2026-04-20

### [DONE] Phase D deploy — multi-tenant-only CVM live

Pairs with the v0 / SINGLE_USER strip below. After the strip landed,
the VPS data directory was wiped (kept `.pepper` + APNs key), VPS
services restarted on the new code, then the CVM was redeployed.

- Image: `ghcr.io/account-link/feedling:78b51a6`
- Compose hash:
  `0xd92bcd3cb1713ffe8e152417ab46e8179510c37ceed5ae6d423c586a2cd60049`
- On-chain (Sepolia): tx
  `0x235f0120d6982cbf8872e927ee2e59133627177ca9d3f862554d748ac6e60c7c`
  at block 10696873.
- CLI audit: `tools/audit_live_cvm.py` → 8/8 green.
- Remaining: prod user reinstalls fresh and verifies the in-app audit
  card shows 8/8 green + the new compose-hash-changed consent modal
  fires on first launch (task #36).

Task #35 closed.

### [DONE] v0 / SINGLE_USER strip — backend is envelope-only

Closes tasks #23 and #33 in a single commit. The one real prod user
OK'd wiping her data + fresh multi-tenant reinstall, so instead of
keeping rewrap as a 30-day compatibility shim we retired the entire
v0 stack in one go.

**Backend (`backend/`)**
- `app.py`: removed all `SINGLE_USER` branches, v0 plaintext accept
  branches in `/v1/chat/message`, `/v1/chat/response`, `/v1/memory/add`,
  `/v1/identity/init`; removed the HTTP `/v1/identity/nudge` endpoint
  (identity mutation now only lives in MCP `feedling.identity.nudge`);
  removed `/v1/content/rewrap` and all `_rewrap_*` helpers.
- `app.py`: added `/v1/content/export` inlining full v1 frame envelopes
  (schema bumped 1→2, cap 50→80 MiB — frames are now part of the
  portable dataset the user walks away with).
- `app.py`: restored a purpose-built `/v1/content/swap` endpoint for
  ongoing in-place envelope swaps (used by iOS visibility-toggle).
  Same validation shape as old rewrap minus the `already_v1` status —
  no v0 concept left in the response.
- `mcp_server.py`: dropped the `SINGLE_USER` constant and every v0
  fallback in `chat_post_message`, `identity_init`, `memory_add_moment`,
  `identity_nudge` — they now fail loud when pubkeys are unavailable.
- `enclave_app.py`: dropped `if v == 0:` pass-throughs in chat, memory,
  and identity decrypt loops.
- `chat_bridge.py` + `deploy/feedling-chat-bridge.service`: deleted.
  MCP's in-enclave `feedling.chat.post_message` replaces them (and
  avoids the April spam-reply incident where a systemd restart race
  caused duplicate Hermes replies).

**Deploy (`deploy/`)**
- `docker-compose.yaml`, `docker-compose.phala.yaml`: removed
  `SINGLE_USER` env, shared `FEEDLING_API_KEY` stubs. Backend is always
  multi-tenant.
- `setup.sh`, `feedling.env.example`: removed shared-key / SINGLE_USER
  provisioning. Fresh VPS bootstrap now produces a multi-tenant box.

**iOS (`testapp/FeedlingTest/`)**
- `FeedlingAPI.swift`: removed `runSilentV1MigrationIfNeeded`,
  `RewrapSummary`, `collectV0Chat/MemoryEnvelopes`, `postRewrap`, the
  `@Published migrationProgress` state, and the 403-SINGLE_USER branch
  in `ensureRegisteredIfCloud`. `flipMemoryVisibility` now POSTs to
  `/v1/content/swap`.
- `ContentView.swift`: removed `MigrationProgressRow` + its usage in
  the Privacy hero.
- `FeedlingTestApp.swift`: removed the migration kickoff call from the
  `.task { … }` startup block.
- `ChatViewModel.swift`, `SampleHandler+WebSocketQueue.swift`: removed
  plaintext fallbacks and dead `WebSocketManager.sendFrame` — backend
  now rejects non-envelope writes, so silent fallbacks would just
  produce invisible 400s / dropped frames.

**Tests + CI**
- `backend/test_api.py`: removed `/v1/identity/nudge` cases, added
  header note that write-path tests POST plaintext and will 400 against
  the v1-only backend until they're rewritten to build envelopes
  client-side.
- `tools/e2e_encryption_test.py`, `.github/workflows/ci.yml`: dropped
  `SINGLE_USER` env + the CI matrix dimension (no more `single-user` +
  `multi-tenant` rows — multi-tenant is the only mode).

**Docs**
- `HANDOFF.md`, `docs/NEXT.md`, `docs/AUDIT.md`, `docs/DESIGN_E2E.md`,
  `CLAUDE.md`: updated to reflect the stripped state. Phase 5's
  "retire v0 over 30 days" checkbox flipped to done.

**Exit criterion**: `grep -r "SINGLE_USER\|single_user" backend/ deploy/`
returns no hits outside this file. Server never stores unencrypted
content and no longer exposes a path to write plaintext.

---

### [DONE] Phase C.2 — ACME-DNS-01 Let's Encrypt cert inside the CVM

`mcp.feedling.app` now serves a real CA-signed Let's Encrypt cert
whose private key is provably inside the TDX enclave. Closes task #30.

**backend/acme_dns01.py (new, ~260 lines, zero new deps)**
- Pure-Python ACME v2 client (RFC 8555) — JWS ES256, JWK thumbprints,
  order/auth/challenge/finalize flow, all over `httpx`.
- `CfDns` helper talks to Cloudflare API to create/delete the
  `_acme-challenge` TXT record for DNS-01.
- `get_or_renew()` caches the cert PEM at `/tls/<domain>.cert.pem`
  (volume-backed, survives restarts) and re-issues when <30 days
  left. LE rate limit is 5 certs/week/domain — 30-day buffer means
  ~12 reissues/year worst case.
- `start_renewal_watchdog()` spawns a daemon thread that checks
  daily; on renewal it `os._exit(0)`s to let Docker restart the
  container and pick up the fresh cert.

**backend/dstack_tls.py (extended)**
- New path constant `MCP_TLS_KEY_PATH = "feedling-mcp-tls-v1"` +
  `derive_key_only(dstack, path)` helper. Cert private key is
  derived from dstack-KMS with a stable hash, so LE renewals rotate
  the cert but NOT the key — audit Row 8 stays green indefinitely.

**backend/mcp_server.py (extended)**
- Replaces `_materialize_tls_cert` with `_acquire_tls_cert`. Priority:
  ACME (when `FEEDLING_ACME_DOMAIN` is set) > dstack-KMS self-signed
  (Phase C.1 fallback) > plain HTTP. Surfaces the pubkey fingerprint
  via a module-level `_mcp_cert_pubkey_fingerprint_hex` that gets
  baked into `/attestation` alongside the attestation-port fingerprint.

**backend/enclave_app.py (extended)**
- `bootstrap()` derives the MCP cert pubkey from dstack-KMS and
  computes sha256(SubjectPublicKeyInfo DER). Result is served as
  `mcp_tls_cert_pubkey_fingerprint_hex` in `/attestation`.
- Stable-per-app-id (not per-compose) because the derivation path
  is constant — same rationale as `enclave_tls_cert_fingerprint_hex`
  and `enclave_content_pk`.

**deploy/docker-compose.phala.yaml**
- Added `FEEDLING_ACME_DOMAIN=mcp.feedling.app`, `FEEDLING_ACME_EMAIL`,
  `FEEDLING_TLS_CACHE_DIR=/tls`, `FEEDLING_CF_ZONE_ID=${CF_ZONE_ID}`,
  `FEEDLING_CF_API_TOKEN=${CF_API_TOKEN}` to the mcp service.
- CF_* are injected at deploy time via `phala deploy -e KEY=VAL`
  (encrypted env channel, never in the compose file, never hashed
  into compose_hash). Zone ID is non-secret; API token is
  `Zone:DNS:Edit`-scoped to `feedling.app`.
- New named volume `feedling_mcp_tls_data_v2` mounted at `/tls`.
  The `_v2` suffix forces Docker to create a fresh volume because
  the v1 volume was root-owned (Docker initializes empty named
  volumes as root when the container image doesn't pre-create the
  mount path). The MCP process runs as `feedling` UID 1000 so
  root-owned `/tls` = `EACCES` on first cert write → ACME silently
  fell back to the dstack-KMS self-signed cert on the first deploy.

**deploy/Dockerfile**
- Pre-creates `/tls` with `feedling:feedling` ownership alongside
  `/data`. New named volumes get initialized from the container's
  directory state, so this guarantees feedling ownership for any
  future fresh volume.

**tools/audit_live_cvm.py**
- Row 8 rewritten for the real LE path. Uses `openssl s_client -showcerts`
  to fetch the full cert chain (Python's `ssl.getpeercert` returns
  only the leaf); builds an `x509.verification.PolicyBuilder` chain
  from the system CA bundle; calls `build_server_verifier(DNSName(
  "mcp.feedling.app")).verify(leaf, intermediates)` to CA-validate
  the cert for the expected name. Then pins the cert's SPKI pubkey
  sha256 against the attested value.
- SNI workaround: Phala dstack-gateway routes by SNI and only
  accepts `-PORTs.*.phala.network` hostnames — sending
  `mcp.feedling.app` as SNI gets the gateway to drop the TCP
  connection before the TLS handshake reaches the CVM. Fix:
  send the gateway hostname as SNI, then verify the cert manually
  for `mcp.feedling.app`.

**deploy/Caddyfile**
- `mcp.feedling.app` reverse proxy: `tls_server_name` changed
  from `mcp.feedling.app` to the gateway hostname + added
  `tls_insecure_skip_verify`. Same SNI-routing reason. The real
  trust root is the attestation; Caddy is just a compatibility
  shim for Claude.ai and other MCP clients that expect a stable
  hostname and a CA-valid cert.

**Operational**
- CF DNS: new A record `mcp.feedling.app → [retired VPS IP redacted]` (VPS
  where Caddy runs). DNS-only (not Cloudflare-proxied) so Caddy
  can do its own HTTP-01 ACME for the public-facing `mcp.feedling.app`
  cert without Cloudflare terminating first.
- New compose_hash `0x23a2c2869567d15220383e4acb5ceb5cf27d78e087d2d4e357e4b3c053a5dc68`
  published on-chain: Sepolia tx `0xe2a9ceab…`.
- MCP cert pubkey fingerprint: `e98665a3e94ac90a0a26453a73e16d5a569f791c181cfbc6ba98598f358cf63e`
  — expect this to stay constant across all future deploys (stable
  dstack-KMS derivation).
- CLI audit: **8/8 green**.

### [DONE] Phase B wave-2 + MIGRATION.md

Finishing out the Phase B surface + directly answering "what does
the one prod user actually do to migrate to E2E?".

**docs/MIGRATION.md (new)**
- Three concrete options for a self-hosted VPS user to move to
  Feedling Cloud's TEE-backed encryption. Option 1 (recommended)
  uses the Phase B Reset & re-import pipeline; the user's agent
  re-adds content via MCP tools, which now wrap everything into
  v1 envelopes on the way in. Option 2 keeps self-hosted without
  encryption (legitimate — they own the server). Option 3 is
  self-hosted with their own TEE (documented, not recommended).
- Linked from the in-app audit card footer alongside AUDIT.md +
  the repo root.

**Per-item memory visibility toggle**
- `FeedlingAPI.flipMemoryVisibility(moment, toLocalOnly:)` — builds
  a fresh envelope with the new visibility from the plaintext iOS
  already has in memory, POSTs to `/v1/content/rewrap`. No server
  trip for re-decryption.
- MemoryGardenView: long-press context menu on each card with
  "Hide from agent" / "Share with agent"; subtle `eye.slash`
  indicator in the card header when `local_only`. Reloads the
  garden after a successful flip.
- Chat is intentionally skipped — many items, transient; the
  "hide from agent" affordance matters more on persistent
  memory-garden entries.

**Inline migration progress**
- `FeedlingAPI` gains `@Published migrationProgress: (done, total)?`.
  `runSilentV1MigrationIfNeeded` sets it before the batching loop,
  updates per batch, clears on completion or error.
- New `MigrationProgressRow` renders an inline `ProgressView` with
  label "Upgrading your old data — N of M" beneath the Privacy
  hero when migrationProgress is non-nil. Hidden otherwise.

**iOS verification**
- `xcodebuild BUILD SUCCEEDED` on iPhone 16 Pro sim with all
  wave-2 changes.

**No backend change** — wave-2 is iOS-only on top of existing
`/v1/content/rewrap`. CVM does not need a redeploy for this ship.

---

### [DONE] Phase C.3 — encrypted identity.nudge + encrypted agent chat reply + UX fixes

Closes the last two plaintext-at-rest write paths. Also applies the
user's last-round UX feedback (privacy hero tap, audit-card on-chain
copy, GitHub + agent-audit-guide links).

**Backend (backend/app.py)**
- New `POST /v1/identity/replace` — accepts a v1 envelope and replaces
  the existing card in place, preserving `created_at`. Used by MCP
  to implement nudge on v1 cards. Same envelope field validation as
  `/v1/identity/init`.
- `POST /v1/chat/response` now accepts `envelope` in addition to
  `content`. Mirrors what `/v1/chat/message` does for user-authored
  chat. Push-live-activity sidecar still works via a `push_body`
  companion field (the push payload is plaintext metadata by
  necessity — APNs doesn't see inside the envelope).

**MCP (backend/mcp_server.py)**
- `feedling.chat.post_message`: wraps `content` in a v1 envelope
  before POSTing when pubkeys are available. Same fallback rule
  as `memory.add_moment` (v0 plaintext when no enclave reachable).
- `feedling.identity.nudge`: new orchestration. Tries legacy v0
  endpoint first; if server responds 409 with
  `error="nudge_not_supported_on_v1_cards_yet"`, catches and falls
  through to the new `_identity_nudge_v1` helper which: fetches
  the decrypted card from the enclave's `/v1/identity/get`,
  mutates the named dimension (clamped [0,100], records
  `last_nudge_reason`), re-wraps the whole card via
  `build_envelope`, POSTs to `/v1/identity/replace`. Plaintext
  lives inside the MCP process only — inside the TDX-attested
  container boundary — for the duration of one RPC.

**iOS (testapp/FeedlingTest/ContentView.swift)**
- Privacy hero row in Settings → Privacy wrapped in a
  `NavigationLink` to `AuditCardPage`. Previously the tap did
  nothing (the user caught this).
- Dropped the hand-drawn chevron from the row since the
  NavigationLink adds its own.

**iOS (testapp/FeedlingTest/AuditCardView.swift)**
- Divider label "On-chain audit (public transparency, not security)"
  → "Public release log" — the parenthetical was confusing and
  undersold what the log is.
- Etherscan link label "View AppAuth deploy on Etherscan" →
  "View on Etherscan".
- Rewrote `AuditMechanismCopy.onChainAudit` to describe what the
  release log *is* rather than inventing a cryptographic
  guarantee it doesn't provide. Previous copy implied the
  on-chain log gates key release, which is future work.
- Two new footer links: "Read the audit guide (for your agent)"
  → `docs/AUDIT.md` on GitHub, and "Browse the source on GitHub"
  → the repo root. Closes the "user hands their agent a repo and
  asks 'is this safe'" gap.

**docs/AUDIT.md (new)**
- Agent-consumable "is this safe?" guide, ~260 lines, 7 sections:
  plain-English trust model; a 10-item mechanical-verification
  checklist with effort estimates per item; key files to read by
  concern; known caveats (things we DO claim vs things we DON'T);
  runnable verifier snippet; an honest-asterisk section about iOS
  binary provenance we don't currently solve for; responsible-
  disclosure pointer. Written so an agent can walk through it end
  to end without needing external context.

**Live verification (Phala CVM)**
- Running: git_commit `cc329a8`, compose_hash
  `0xa04608c72639c66a625706b7ac4b9f1ac8dd449c690a0544b173ecede265e83e`,
  Sepolia tx `0x7873c5dd4c9b6636994d9a3adda7ded8618394ce1a9f577a1ba9c74dc5acf7b0`.
- CLI auditor **8/8 green**.
- `TLS fingerprint` now stable across **six** compose rotations —
  `5698f0ade4bb412d…` unchanged from Phase 3 through Phase C.3.
  Phala dstack-KMS per-app derivation confirmed load-bearing.
- Live E2E: `/v1/identity/replace` correctly rejects missing
  envelope (400), `/v1/chat/response` envelope-branch field-
  validates (400 on malformed), plaintext content path still
  works (200, back-compat preserved). Full decrypt-mutate-rewrap
  flow validated against dstack simulator before deploy.

**What's left on Phase C**
- Phase C part 2: ACME-DNS-01 for `mcp.feedling.app` so
  Claude.ai sees a CA-signed cert issued inside the enclave.
  Needs a DNS API token + renewal scheduler. Task #30.

---

### [DONE] Phase C (part 1) — MCP in-enclave TLS + audit card Row 8

Closes the last plaintext-metadata gap at the TLS layer on the
pinnable path: MCP port 5002 now terminates TLS inside the enclave
with the same dstack-KMS-derived cert that port 5003 uses. The
`-5002s.` passthrough URL becomes pinnable end-to-end.

**Backend**
- New shared `backend/dstack_tls.py` — pulls `derive_tls_cert_and_key`
  and `TLS_KEY_PATH` out of `enclave_app.py` so both services use
  one source of truth for the cert (deterministic ECDSA-P256 derived
  from dstack-KMS at path `feedling-tls-v1`). Cert DER byte-stable
  across reboots of the same compose; matches across ports.
- `backend/enclave_app.py` — dropped inline derivation + a pile of
  crypto imports; imports from `dstack_tls` now. Behavior identical.
- `backend/mcp_server.py` — new `_materialize_tls_cert()`: when
  `FEEDLING_MCP_TLS=true`, derive the cert via dstack-KMS at boot,
  write cert + key to tempfiles, hand paths to uvicorn via
  `ssl_certfile` / `ssl_keyfile`. Plain HTTP otherwise so local
  dev stays simple. Logs the scheme on boot.

**Compose (deploy/docker-compose.phala.yaml)**
- `mcp` service: `FEEDLING_MCP_TLS=true` + mounts
  `/var/run/dstack.sock` so it can derive via dstack-sdk at boot.

**Audit (tools/audit_live_cvm.py)**
- Row 8 (new): MCP TLS cert bound to attestation. Raw TLS handshake
  against `-5002s.*`, compare `sha256(peer cert DER)` to the
  bundle's `enclave_tls_cert_fingerprint_hex`. Skipped with
  disclosure when attestation-side is still pre-Phase-3.
- Docstring refreshed "7-row" → "8-row".

**iOS (testapp/FeedlingTest/AuditCardView.swift)**
- `AuditReport` gains `mcpTlsCertBindingChecked` +
  `mcpTlsDisclosure`.
- After the attestation-port pin, a second `PinningCaptureDelegate`
  session opens a TLS handshake against the MCP URL and compares
  its captured `sha256(cert.DER)` to the same attested fingerprint.
- New "MCP port TLS bound to attestation" row with its own
  tap-to-expand mechanism copy explaining that the MCP port is the
  one the agent connects to, and that this second pin catches a
  middleman sitting between agent and enclave.

**Live verification (Phala CVM)**
- Running: git_commit `60014a7`, compose_hash
  `0x14cd6edb382b3229ebe36bf030f1bdc087765a9004d1ad323af58904c72df38f`,
  Sepolia tx
  `0xa6e0282c698cbe8e925c968624a2f2315bad5cc868568053598ccb6071984252`.
- CLI auditor **8/8 green** against the live CVM — MCP port
  fingerprint `5698f0ade4bb412d…` === attested fingerprint
  `5698f0ade4bb412d…` === attestation-port handshake fingerprint.
- `enclave_content_pk` + `enclave_tls_cert_fingerprint` unchanged
  across FIVE compose rotations now (Phase 3 → A.1 → A.1 fixed →
  A.6 → B → C). Phala dstack-KMS derivation is stable per app_id,
  confirmed once more.

**mcp.feedling.app unchanged**
- The `mcp.feedling.app` hostname (what Claude.ai uses) still
  terminates TLS at Caddy on the VPS for now, so no existing MCP
  connection breaks. The pinnable path is the
  `-5002s.dstack-pha-prod5.phala.network` URL.
- Moving `mcp.feedling.app` to layer4 SNI passthrough + ACME-DNS-01
  inside the enclave is the next Phase C sub-ship (requires a DNS
  API token + renewal logic; flagged in `docs/NEXT.md` §Phase C).

**Still pending for Phase C**
- ACME-in-enclave for `mcp.feedling.app`.
- Identity nudge decrypt-mutate-rewrap (MCP now runs inside the
  TDX boundary; can orchestrate the dance now — need a new
  `/v1/identity/replace` endpoint).
- Agent-authored chat reply encryption (`feedling.chat.post_message`
  — wrap plaintext before `/v1/chat/response` POST, extend endpoint
  to accept envelopes like `/v1/chat/message` does).

---

### [DONE] Phase B — Privacy UX + onboarding + audit card expansion

After `/plan-design-review` (9/10 overall) and `/plan-eng-review`
(scope accepted, 3 architectural fixes applied in-line), shipped the
full Phase B user-visible surface. The audit card explicitly promoted
to a first-class treatment per @sxysun's request to preserve the
attestation-details page and its "how we get them" affordance.

**Backend (backend/app.py)**
- `GET /v1/content/export` — caller's chat + memory + identity as
  one JSON blob, 50 MiB cap, ciphertext returned verbatim (iOS
  decrypts client-side). Attestation snapshot (compose_hash +
  enclave_content_pk at export time) bundled so future agents can
  verify origin. Frames excluded in Phase B (too large, low
  continuity).
- `POST /v1/account/reset` — destructive, requires
  `{"confirm": "delete-all-data"}` body token as a second signal of
  intent. Wipes user dir, removes user from users.json, revokes
  api_key cache. Idempotent in the safe-to-retry sense (second call
  401s because the user no longer exists).
- `Response` added to Flask imports.

**iOS (testapp/FeedlingTest/)**
- Design tokens (DESIGN.md mirror) inlined in `FeedlingAPI.swift`:
  `Color.feedlingSage / feedlingPaper / feedlingSurface / feedlingInk
  / feedlingInkMuted / feedlingDivider`; serif display font via
  `.system(design: .serif)` (iOS New York — zero asset loading);
  `Spacing.*` + `Radius.*` + `FeedlingMotion.*`;
  `FeedlingPrimaryButtonStyle` + `FeedlingSecondaryButtonStyle`.
  Kept inline in FeedlingAPI.swift because Xcode's `project.pbxproj`
  requires coordinated edits for new source files — documented.
- `ContentView` now wraps the tab bar in an onboarding gate.
  Gate flips on user action.
- `ComposeHashChangeConsentView` (full-screen modal): triggered when
  `/attestation` returns a `compose_hash` that differs from
  `UserDefaults "feedling.lastAcceptedComposeHash"`. Per the dstack
  tutorial §1 catch, trigger is **compose_hash**, NOT MRTD —
  MRTD/RTMR0-2 are dstack-OS platform signals that change for
  reasons unrelated to our app.
- `OnboardingView` (3 slides, SwiftUI `TabView.page`):
  lock.shield / arrow.triangle.branch / hand.raised.square.on.square
  as the single glyph anchor per slide. No custom illustrations
  (AI-slop-free). Decision tokenized in `docs/PHASE_B_PLAN.md`.
- `PrivacyPageView` — NavigationLink destination from Settings.
  Hero row + Your data + Where your data lives + Advanced sections.
- `ExportSheet` — export → iOS share sheet, with an explicit iCloud
  Drive caveat in the copy.
- `DeleteSheet` — "download my data first" checkbox defaults to ON
  (decision `2A` from `/plan-design-review`). If checked,
  pipeline exports through iOS share sheet, then deletes after
  dismissal; if unchecked, delete is immediate.
- `ResetAndReimportSheet` — 3-step pipeline (export / delete /
  re-register) with visible step indicator.
- `RunbookView` — fetches `skill/SKILL.md` from GitHub raw so users
  can pass a live copy to their agent. Offline fallback included.
- `StorageBackendView` — thin wrapper around the existing storage
  toggle so Privacy's "Where your data lives" row has a destination.
- `AuditCardView` extended: `AuditRowView` per-row tap-to-expand
  mechanism panel (plain-language explanations naming primitives
  honestly — TDX, PCK, `mr_config_id` — but with analogies); new
  collapsed "Show raw /attestation (for auditors)" footer panel
  with SF Mono horizontally-scrollable pretty-printed JSON;
  existing PinningCaptureDelegate + DCAP + SPKI-pin flow unchanged
  (security primitives preserved per eng review).
- `FeedlingAPI.exportMyData` / `deleteMyDataAndResetLocalState` /
  `acceptComposeHashChange` / `signOutForComposeChange` /
  `hasCompletedOnboardingV1` + `evaluateComposeHashChange` wired
  into `refreshEnclaveAttestation`.
- `ContentKeyStore.wipeKeypair` + `KeyStore.wipeKeypair` for the
  delete path.

**Live verification (Phala CVM)**
- Running: git_commit `123a45b`, new compose_hash published on
  Sepolia (see DEPLOYMENTS.md §Phase B for the tx hash).
- CLI auditor 7/7 green against the new image.
- Export + reset endpoints verified locally end-to-end:
  register → seed → export → `Content-Disposition` filename valid;
  reset w/o confirm → 400; reset with confirm → 200; post-reset
  call → 401. Same behavior on the live CVM.
- iOS build: `xcodebuild BUILD SUCCEEDED` on iPhone 16 Pro sim.
  First-launch screenshot captured at
  `docs/screenshots/onboarding_slide1_phase_b.png`.

**Deferred to Phase B wave-2**
- Per-item visibility toggles (endpoint exists via rewrap; UI is
  a list + switch per row — ~2h of iOS).
- Inline migration-progress row in the Privacy hero (wire in
  `runSilentV1MigrationIfNeeded` progress stream).
- `docs/screenshots/` captures of Slides 2-3, Privacy page, Delete
  sheet, audit-card expanded state — need UI automation to drive
  the sim without controlling the user's mouse.
- Copy review by @sxysun — the audit-card mechanism reveals, the
  compose-hash consent copy, the onboarding headlines. The register
  is load-bearing ("name primitives honestly + analogies") and
  needs the product-voice pass flagged in `PHASE_B_PLAN.md §4`.

---

### [DONE] Phase A.6 — Silent v0→v1 migration on first launch

**Backend (backend/app.py)**
- New `POST /v1/content/rewrap` endpoint. Batched, idempotent. Takes `{items: [{type, id, envelope}]}` for `type ∈ {chat, memory}` and swaps the item's v0 plaintext fields with the v1 envelope fields in place, preserving metadata (ts/role/source for chat; occurred_at/created_at/source for memory). Per-item result + summary counts. Owner binding enforced: `envelope.owner_user_id` must match the caller's resolved `user_id`, else the item is rejected before storage. Identity intentionally not supported — would trap users pre-Phase-C because `nudge` can't mutate a v1 card.
- `/v1/identity/nudge`: when called against a v1 card, now returns `409 {"error": "nudge_not_supported_on_v1_cards_yet", "phase_reference": "docs/NEXT.md §Phase C"}` instead of silently 404'ing because `dimensions` is encrypted inside `body_ct`.

**iOS (testapp/FeedlingTest/)**
- `FeedlingAPI.ensureUserIdIfNeeded()` — when an api_key is present but `userId` is empty (env-injected creds, self-hosted handoff), populate via `/v1/users/whoami`. Needed so migration can bind AEAD AAD to the right owner.
- `FeedlingAPI.runSilentV1MigrationIfNeeded()` — gated on dated `UserDefaults` flag. Fetches chat (up to 500) + memory (up to 200), collects v0 items, wraps each via `ContentEncryption.envelope`, POSTs in batches of 100 to `/v1/content/rewrap`. Sets flag only when all batches complete with no errors; transient failures retry on next launch.
- `FeedlingTestApp.swift` wires the new startup steps in sequence: register → ensureUserId → content keypair → attestation refresh → migration. Non-blocking.

**Live verification (Phala CVM)**
- Running: git_commit `90c8ff6`, compose_hash `0x9f7fe0a823bf2820877851863d322b0f3be7fff819a40a8826e6ca994597cf48`, Sepolia tx `0xb3b434b6db6abd45eb492d2a708d8d7d6b99d5af59d5f01bc1686a74ed3e6c27`.
- `enclave_content_pk` + `enclave_tls_cert_fingerprint` unchanged from Phase A (confirms the dstack-KMS key-derivation-independent-of-compose-hash observation is stable across two more compose rotations).
- CLI auditor 7/7 green. `/v1/content/rewrap` reachable on prod.
- Local E2E against dstack simulator: seeded 3 v0 chat + 3 v0 memory, iOS launched with seeded api_key, migration reported `ok=6`, server afterwards had 0 v0 / 3+3 v1 items, enclave decrypt returned correct plaintext for all.

**Follow-up (A.6e)**
- Only one real prod user today (a private tester). After her iOS launches the updated app and the migration flips her data to v1, strip the v0 accept branches in backend handlers, the v0 fallback paths in MCP tools, and the `/v1/content/rewrap` endpoint itself (single-use). Tracked as task #23.

---

### [DONE] Phase A — Content encryption rollout for agent-authored writes

**Backend**
- `/v1/users/whoami` now returns `public_key` (user's X25519 content pubkey from users.json)
  and `enclave_content_public_key_hex` (cached from enclave `/attestation`, 60s TTL).
  One round trip gives MCP everything it needs to wrap an envelope.
- New `backend/content_encryption.py` — Python counterpart to iOS `ContentEncryption.swift`.
  `box_seal` uses HKDF-SHA256(salt=None, info="feedling-box-seal-v1"), nonce=SHA256(ek||rcp)[:12],
  ChaChaPoly. `build_envelope` produces the `{"envelope": …}` shape POSTed to
  `/v1/{chat/message,memory/add,identity/init}`.

**MCP (backend/mcp_server.py)**
- `feedling.memory.add_moment` wraps `{title, description, type}` into a v1 envelope
  before POSTing. Plaintext metadata (`occurred_at`, `source`) rides alongside inside
  the envelope dict for server-side sorting.
- `feedling.identity.init` applies the same wrap to `{agent_name, self_introduction, dimensions}`.
- `feedling.identity.nudge` intentionally left on the plaintext path — in-place mutation of an
  encrypted card requires decrypt→mutate→rewrap, cleanly solved by Phase C (MCP-in-TEE).
- New `_get_decrypted()` — when `FEEDLING_ENCLAVE_URL` is set, MCP routes `memory.list`,
  `identity.get`, `chat.get_history` through the enclave's decrypt proxy so agents see
  plaintext. Unset → fall back to Flask.
- Fallback: pre-v1 users (no uploaded pubkey) or unreachable enclave → v0 plaintext POST,
  so agents never lose write capability mid-session.

**Compose (deploy/docker-compose.phala.yaml)**
- `backend.FEEDLING_ENCLAVE_URL = https://enclave:5003` (Phase 3 missed this — backend calls
  enclave `/attestation` to cache content pubkey for whoami).
- `mcp.FEEDLING_ENCLAVE_URL = https://enclave:5003` (routes MCP reads through decrypt proxy).
- `enclave.FEEDLING_FLASK_URL = http://backend:5001` (fixes a latent bug: enclave's decrypt
  handlers call `/v1/users/whoami` on Flask, but the 127.0.0.1 default doesn't resolve
  across distinct compose containers — returned 500 on the first test deploy).

**Live verification (Phala CVM)**
- Running: git_commit `8b53404`, compose_hash `0x593cb8aaa1fd5ed964fdb3a1718200114ab36537f1cf551fd5162fc02512eb80`,
  Sepolia tx `0x5b5a933dfc6e1f6376a32029d7a31632723dcc75447104b12ebd5da5e2f3e825`.
- CLI auditor 7/7 green. End-to-end: register a fresh user → MCP-side wrap via
  `backend/content_encryption.build_envelope` → server stores ciphertext only (no plaintext
  `title`/`description`/`type`) → enclave `/v1/memory/list` returns plaintext via `K_enclave`.
- Observation worth recording: Phala dstack-KMS derives per-app keypairs from
  `(kms_root, app_id, path)`, NOT from `compose_hash`. So `enclave_content_pk` and the
  TLS cert are stable across compose rotations for this app_id. This is stronger than
  `docs/DESIGN_E2E.md` §5.3 assumed — no re-wrap dance is needed after a compose update,
  which simplifies operational rollouts.

**Still pending for Phase A**
- A.6 silent migration of pre-existing v0 plaintext rows (chat/memory/identity) into v1
  envelopes on first iOS launch post-update. Design in NEXT.md; needs a
  `POST /v1/content/rewrap` endpoint.
- Chat replies from agent (`feedling.chat.post_message`) still POST plaintext — paired
  with `identity.nudge` as the "Phase C dependencies" bucket, for the same
  decrypt-mutate-or-write-through-TEE reason.

---

## 2026-04-20

### [DONE] Phase 3 — TLS-in-enclave + iOS cert pinning

**Enclave (backend/enclave_app.py)**
- 新增 `FEEDLING_ENCLAVE_TLS=true` 开关；启用后从 dstack-KMS 派生 ECDSA-P256 keypair
  (`feedling-tls-v1` path)，用 RFC-6979 deterministic ECDSA 签发自签 cert —
  同一 compose_hash 下跨 reboot 的 cert.DER 完全一致（本地 simulator 验证过两次 boot 哈希相同）。
- `build_report_data()` 现在把 sha256(cert.DER) 真正填入，替换原先的 32-byte 零占位符。
- Flask `app.run(ssl_context=…)` — SSL 材料先写入临时文件、`load_cert_chain` 后立即 unlink，
  cert/key 不落盘。
- `/attestation` bundle 新增 `tls_in_enclave: true` 标志 + 更新 notes；`phase` 字段从 1 跳到 3。

**Compose (deploy/docker-compose.phala.yaml)**
- enclave service 加 `FEEDLING_ENCLAVE_TLS: "true"`。
- healthcheck 从 `curl http://127.0.0.1:5003` 改成 `curl -k https://127.0.0.1:5003`。

**iOS (testapp/FeedlingTest/)**
- attestation URL 从 `-5003.` 切到 `-5003s.`（dstack-gateway TLS passthrough 后缀）。
- `PinningCaptureDelegate`（AuditCardView.swift）在 TLS 握手时记录 leaf cert sha256(DER)，
  审计流程把它和 bundle 的 `enclave_tls_cert_fingerprint_hex` 比对 —
  匹配 = 绿；不匹配 = 硬红 "MITM detected."；全零 = 沿用原 amber 免责声明。
- `FeedlingAPI.refreshEnclaveAttestation` 的那条启动时 fetch 用 `AttestationTrustShim`
  接受自签 cert（只是预热 content pubkey，真正的 pin 在审计卡里）。
- `AuditCardView` 底部的 TLS row 文案更新：不再提 "Phase 1 placeholder"。

**CLI auditor (tools/audit_live_cvm.py)**
- 新增 Row 7：raw TLS 握手取 peer cert DER，sha256 和 bundle 的 fingerprint 比对。
  全零 fingerprint 走 pre-Phase-3 disclosure 分支（不算 pass 但也不算 fail）。
  文件开头的 docstring 从 "6-row audit" 改为 "7-row audit"。

**Live 验证**
- Phala CVM `feedling-enclave` (UUID `4386636e-1325-4b92-99d8-f2ca00befdb4`) 跑在 git_commit `451b5b0`。
- 新 compose_hash `0xb0fb1f848151ec8fb39c4814f138b1d1b143d4d729dc800302d5123c1c0f2163` 已在
  Eth Sepolia FeedlingAppAuth 上 authorize（tx `0x8de67abaf677e221ba4ee34b5a004753d0f4981bdc3c952cbcb4112a652a169c`）。
- TLS cert fingerprint: `5698f0ade4bb412d6b0847a62d695138f3bbd287dc7d1dbdeb67b15dc445e5ef`。
- CLI 7/7 green；iOS 6/6 green（见 `docs/screenshots/audit_card_phase3_tls_pinned.png`）。

**Trust model note**: self-signed cert 是有意的；用户信任链不是 CA chain，而是
"TDX-attested REPORT_DATA 里有这张 cert 的 fingerprint"。伪造 TLS cert 的操作员也
必须同时伪造 REPORT_DATA，而 REPORT_DATA 由 Intel PCK 签名 — 做不到。

**Deferred**
- `docs/NEXT.md` 里 "Phase 4-6" 的内容迁移 / 全量加密 / 用户自放 enclave job 未动。
- iOS 审计卡文案 copy review 仍然待 @sxysun 过一遍。
- Sepolia → Base 迁移（详见 deploy/DEPLOYMENTS.md §Planned）。

---

## 2026-04-19

### [DONE] NEXT.md Steps 1-5：multi-tenant backend + MCP SSE + iOS onboarding + self-hosted runbook

**后端 (backend/app.py)**
- 引入 `SINGLE_USER` 环境变量：`true` = 兼容旧的 flat layout；`false` = 多租户 `~/feedling-data/<user_id>/…`。
- 新增 `POST /v1/users/register` + `GET /v1/users/whoami`。
- 新增 `require_user()` 中间件：接受 `X-API-Key` / `Authorization: Bearer` / `?key=` 任何一种形式；
  SHA-256(HMAC+pepper) 哈希比对，per-process 缓存避免 bcrypt 开销。
- 所有 module-level state（frames/chat/tokens/push cooldown/live activity dedupe/bootstrap/identity/memory）
  重构为 `UserStore` 类，每用户一份；waiters 也按用户隔离，防止跨用户唤醒。
- WebSocket ingest handler 也接 `?key=` 或 `Bearer <key>`；`SINGLE_USER=true` 时跳过鉴权。

**MCP server (backend/mcp_server.py)**
- 切换 transport 到 SSE（保留 streamable-http 作为备选，走 `FEEDLING_MCP_TRANSPORT` 环境变量）。
- 新增 `KeyCaptureMiddleware`（ASGI 层）：监听每个 HTTP 请求，从 query/header 抓取 `key`，映射到 session_id；
  未知 session_id 时按 client IP 回溯 pending_keys，保证 SSE GET 与后续 POST tool call 能绑定。
- 每个 tool 调 `_current_api_key()` 拿当前 session 的 key，作为 `X-API-Key` 转发给 Flask；
  Flask 里 401 会自动冒泡成 tool error。

**iOS (testapp/)**
- `FeedlingAPI.swift` 完全重写：@MainActor ObservableObject + legacy static accessors；
  持久化到 UserDefaults + app-group shared defaults；`authorizedRequest(path:…)` 自动注入 X-API-Key。
- `KeyStore`：首次启动生成 Curve25519 keypair，private 存 Keychain（accessibleAfterFirstUnlockThisDeviceOnly）。
- `ensureRegisteredIfCloud()`：在 cloud mode 下如缺 api_key 自动注册；403（后端为 single-user）时记号跳过，不再重试。
- Settings tab 加了 Storage 切换、Agent Setup 复制按钮、用户 ID 展示、Regenerate key 按钮。
- broadcast extension 通过 app-group key（`ingest_ws_token`）自动拿到 api_key 作为 WS Bearer。

**Skill runbook (skill/SKILL.md)**
- 新增 Self-Hosted Setup 小节：0 前置 → 1 clone → 2 `openssl rand -hex 32` → 3 venv+deps → 4 env → 5 systemd → 6 smoke → 7 Caddy（可选）→ 8 告诉用户 → 9 端到端验收；每一步都带 **Verify** 行。
- 新增 troubleshooting 表（chat bridge / MCP 401 / Live Activity / frames not arriving）。

**部署 (deploy/)**
- `feedling.env.example` 加 `SINGLE_USER`、`FEEDLING_MCP_TRANSPORT`。
- `setup.sh` 新增 `--install-caddy` 开关，能自动 `openssl rand -hex 32` 并写 env 文件。
- `Caddyfile` 给 `/v1/chat/poll` 长轮询放宽 response timeout 到 90s。

**测试**
- `backend/test_api.py` 加 `--multi-tenant` 和 `--key <shared>` 两种模式；
  新增 Section 8（isolation + 401 + Bearer + query-key + whoami）；single-user 也保持全绿。
- 本地和测试 EC2 (`ec2-34-228-180-146`) 全通过；MCP SSE 端到端：initialize → tools/list → tools/call bootstrap 在正确/错误 key 下表现都对。

---

## 2026-04-18

### [DONE] Phase 0 T0.1 + Phase 1 T1.1/T1.2/T1.3 + Phase 2 T2.1-T2.5 + T3.1/T3.2/T4.2

**后端 (backend/app.py)**
- 新增 identity card HTTP endpoint（init/get/nudge），5 维固定
- 新增 memory garden HTTP endpoint（add/list/get/delete）
- 新增 bootstrap endpoint（first_time 返回 instructions，already_bootstrapped 防重复）
- T3.1：删除 `should_notify`，改为 `rate_limit_ok`（纯平台层 flag）
- T3.2：push payload 通用化，ContentState 改为 title/subtitle/body/personaId/templateId/data
- T4.2：`bootstrap_events.jsonl` 日志（bootstrap_started / identity_written / memory_moment_added）

**MCP server (backend/mcp_server.py)**
- 新建 FastMCP server，14 个 tool，全部调 localhost:5001
- push tool 参数同步更新为新 ContentState 字段

**部署 (deploy/)**
- Caddyfile：mcp.feedling.app → 5002，api.feedling.app → 5001
- 3 个 systemd service（feedling-backend / feedling-mcp / feedling-chat-bridge）
- setup.sh + feedling.env.example

**iOS (testapp/)**
- T3.2：ScreenActivityAttributes.ContentState 改为通用字段
- T3.2：ScreenActivityWidget.swift 渲染 title/body/subtitle
- T2.4：AppTab 扩展为 chat/identity/garden/settings 四 tab
- T2.1：IdentityView.swift + IdentityViewModel.swift（radar chart，5 维，10s 轮询）
- T2.2：MemoryGardenView.swift + MemoryViewModel.swift（卡片列表，10s 轮询，新卡片高亮）
- T2.3：Settings 加 Connection section（API URL + pairing code 占位符）
- T2.5：bootstrap 检测（identity nil → non-nil 时自动切到 Identity tab）
- FeedlingTestApp.swift：注入 IdentityViewModel / MemoryViewModel

### [DECISION] chat_bridge 改为 opt-in，默认不启动

- chat_bridge.py 是临时 Hermes 自动回复桥，有了真 MCP Agent 后会冲突
- 迁移到 systemd 后 feedling-chat-bridge service 只 install 不 enable
- Hermes 用户手动 `systemctl enable feedling-chat-bridge`，Claude.ai / OpenClaw 用户不需要跑

### [DECISION] 身份卡维度固定 5 个

- v1 先硬编码 5 维，UI 定稿后再调整
- 影响：T1.1 数据库 schema dimensions 数组长度验证改为 exactly 5；Open Decision #1 关闭

### [DECISION] 删除 T0.2 OAuth server，不做

- Claude.ai connector UI 只需填 Name + URL，不需要 OAuth
- 删除 ROADMAP T0.2（OAuth 2.1 + Dynamic Client Registration）
- 删除 Open Decision #1（自建 vs Auth0）
- 影响：PROJECT_BRIEF Section 6.1 和 Section 7.1 去掉 OAuth 相关描述

---

## 2026-04-18

### [BRIEF][ROADMAP] 项目起点 / Project kickoff

- 建立 `PROJECT_BRIEF.md` 和 `ROADMAP.md` 两份文档
- 两周 roadmap：Phase 0（MCP server 层）→ Phase 1（身份卡 + 记忆花园后端）→ Phase 2（iOS UI 粗糙版）→ Phase 3（技术债）→ Phase 4（内测准备）
- 目标用户：人机恋群体 + 用 Claude / ChatGPT / 自跑 Agent 的技术派
- 内测渠道：300 人的人机恋群里挑 30-100 人
- 核心原则：Feedling 不替换只增补；身体 vs 大脑；Feedling 没有意见
- 关键产品决定：Claude.ai 用户的记忆花园数据来源 = Agent 自己用 `conversation_search` 搜历史，Feedling 不导入任何 Claude 数据

### [ROADMAP] 记下 4 个 Open Decisions 待定

1. OAuth server 自建还是 Auth0
2. 身份卡维度数量严格 3-5 还是完全自由
3. Persona 系统 v1 要不要对用户可见
4. UI 设计稿定稿时间

---

## 模板示例（删掉或保留都行）

以下是几条示例，展示不同情境下该怎么记：

---

## 2026-04-22（示例）

### [DONE] Phase 0 完成
- T0.1 FastMCP server 层跑通
- T0.2 OAuth 用了 Auth0 免费 tier（见下面 DECISION）
- T0.3 Caddy + Let's Encrypt 部署在 `mcp.feedling.app`
- T0.4 claude.ai 里成功添加 custom connector，推送 "hello from Claude" 到灵动岛成功
- **实际用时：2.5 天（估计 3 天，稍快）**

### [DECISION] OAuth 用 Auth0 不自建
- **选择**：Auth0 免费 tier
- **原因**：两周 scope 下自建 OAuth 2.1 + DCR 风险太高，Auth0 能节省 2 天
- **影响**：v2 可能会自建替换，届时需要迁移策略
- **影响文档**：ROADMAP Open Decisions #1 勾掉

---

## 2026-04-25（示例）

### [UI] 设计师给出身份卡/记忆花园定稿
- 新增 `docs/UI_SPEC.md`
- 身份卡确定为六边形（不是五边形），6 维
- **影响 Open Decision #2**：维度数量定为严格 6 维（不是 3-5）
- **影响 ROADMAP**：
  - 新增 Phase 2.5 "UI polish"，预计 2 天
  - Phase 1 的 identity schema 里 dimensions 数组长度约束改为 6
  - 已写入数据库的测试数据需要 migration

### [ROADMAP] 删除一个 task
- T3.5 "Mock endpoint 清理" 挪到 v2，v1 不做

---

## 2026-05-02（示例）

### [FEEDBACK] 内测第一周反馈
- 15 个用户接入成功，3 个卡在 Claude.ai connector 授权环节
- 共同痛点：onboarding guide 里没讲"为什么要 OAuth"，用户警惕
- **动作**：改 `docs/onboarding/claude_ai.md`，加一段"Feedling 拿到什么、拿不到什么"的解释
- 2 个用户反馈身份卡维度看不懂——维度名字是 Agent 写的，但没有解释文字默认不展开
  - **动作**：T2.1 改，默认展开第一维的 description

### [PIVOT] 削减 ChatGPT 用户支持
- 内测发现 ChatGPT Developer Mode 流程太复杂，3 个想接的都放弃了
- **决定**：v1 内测不再主推 ChatGPT 路径
- **影响**：`PROJECT_BRIEF.md` Section 6.3 → 改成"Claude.ai / Claude Desktop / 自跑 Agent"三类；ChatGPT 挪到"Not in scope"
- **不删除相关代码**——只是不在 onboarding 里提

---

（示例结束。实际使用中，删除上面三条示例，只保留真实发生的记录。）
