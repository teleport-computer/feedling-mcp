# V2 harness 化 + 信息食谱对齐 — 规格

> 2026-08-16 Seven 定调:「该放在 harness 里的就要放在 harness 里面,试图通过
> prompt 约束完全不靠谱」「内容没给齐的要给齐」。
> 信息食谱的口径:**对齐 V1** —— V1 是部分直接注入 + 工具仍可自查的混合形态,
> 照抄这个形态,不发明新策略。
>
> 方法论沿用 `RUNTIME_V2_PARITY.md`:把「指望模型自觉」的每一处改成
> **harness 确定性保证**。本规格是那条方法论的第二轮,覆盖它当初没扫到的面。

## 为什么现在做

线上实测(prod `15928234`,2026-08-16):

| 模型 | thinking 产出 | 说明 |
|---|---|---|
| `anthropic/claude-sonnet-4.6` | 107 chars, `branch=self` | 正常 |
| `deepseek/deepseek-v4-flash-0731` | 0 chars, `branch=none` | 3 轮全 0 |
| `mimo-v2.5` | 0 chars, `branch=none` | 4 轮全 0 |

同一根因还产出:内心话整段泄漏进用户气泡(模型写 `（…）` 而非 `<think>`,
闭集词表看不见 → fail-open)、工具面 34 个全交付但 0 调用、dream 0% 成功率。

**结论:V2 把约束从「架构强制」搬到了「提示词请求」。提示词约束的强度正比于
模型的指令遵循能力,所以模型一弱,三处同时塌。**

---

## B1 — 信息食谱对齐 V1(先做这批)

**口径:V1 注入什么,V2 就注入什么。V1 的 file:line 是规格来源。**
pull-only 不是错的,错的是「只有 pull」。V1 = 直接注入 + 工具补充;V2 = 全 pull。

### B1-1 心跳感知:布尔 → 事实

- **现状**:`backend/perception/glance.py:105` `build_perception_glance()` 返回类型
  就是 `dict[str, dict[str, bool]]`。它读了 `place_label` / `title` / `artist` /
  `app_name` / 天气 / 健康数值,**按构造丢弃全部取值**,只吐
  `{"available": true, "notable_change": bool}`。
- **V1 对照**:`tools/chat_resident_consumer.py:12761-12789` 注入
  `cross_domain_board_json`(地点名、歌名+歌手、当前 App + 最近 5 个、天气状况+温度、
  mood valence、**逾期提醒标题**、下一个提醒、下一个日程、照片场景、broadcast 状态、
  健康 notables),构建在 `backend/perception/history.py:538-561`;
  另有 `:12921-12940` presence hints(`place_label` / `motion_state` /
  `now_playing` / `locale` / `broadcast_state`)。
- **要求**:V2 **心跳(heartbeat)** 唤醒注入等价内容。字段与上限逐条对齐 V1,
  不新增、不扩容。工具仍保留,模型可自行深挖。
- ⚠️ **screen_watch 不在本条范围内**(codex2 2026-08-16 核出,claude2 复核确认)。
  V1 `chat_resident_consumer.py:14462` 显式
  `perception_digest = None if _is_screen_watch_job(job) else ...`,
  注释写明「Screen-watch is a light lane… its prompt **deliberately** omits the board」。
  给 screen_watch 灌跨域板 = 做得比 V1 多,违反本规格「对齐 V1」的总口径。
  **screen_watch 只补 B1-3 的 OCR / app 名 / 像素。**

### B1-2 感知唤醒记录的孤儿字段

- **现状**:`backend/perception/glance.py:17-24` `_EVENT_FIELDS` 是**固定字面量**,
  `project_perception_wake_events()`(:171)返回 `dict(_EVENT_FIELDS[trigger])` ——
  一个常量。原事件的 `photo_id`、地点、digest 全部按构造丢弃。
  `serve_worker.py:1929-1960` 每次心跳算好 `presence_hints`、`change_digest[:2000]`、
  `origin_refs`、`source`、`wake_id`(docstring 写明「给唤醒 prompt 用」),
  **只有 trigger 名活下来**。约 2KB 已算好、已限长的 grounding 每次取出后扔掉。
- **要求**:把已计算字段接到唤醒 prompt。**这是接线,不是新功能**——
  成本最低的一条。
- ⚠️ `photo_added` 尤其重要:V1 给 `photo_id` + 场景 + time_of_day
  (`chat_resident_consumer.py:12542-12586`),模型知道该读哪张;V2 只给
  `{"new_photo": true}`,模型必须再烧一轮 `photo_recent`。

### B1-3 屏幕:OCR 文本与 app 名

- **现状**:
  - 前台聊天 `backend/model_api_runtime/v2/screen_chat.py` —— `ocr_text` / `app_name`
    出现次数 **0**。帧已解密,OCR 文本白扔。
  - screen_watch 唤醒 `worker.py:747` `_safe_eager_screen_metadata()` docstring
    写着「Retain only controlled counts; captions/labels/ids stay pull-only」,
    只返回 `recent_count` / `total`。**这条道既无 OCR 也无像素。**
- **V1 对照**:`chat_resident_consumer.py:3679-3687`(前台,`ocr_text[:2000]`
  显式标注 `(untrusted)` + `app: <name>`);`:3804-3835`(screen_watch,
  每帧 `ocr_text[:2000]`,最多 4 帧);`:14458` 像素随 `images=` 传入。
- **要求**:逐条对齐,**保留 V1 的 `(untrusted)` 标注**——那是提示注入的边界标记,
  不是装饰。

#### B1-3a 屏幕文本进首轮之后的执行闸(Seven 2026-08-16 拍板)

把屏幕 OCR 文本放进首轮 prompt = 把**任何人都能写的内容**放进模型的输入
(网页、别人发来的消息、一份文档)。V1 对此**只贴了一个 `(untrusted)` 文字标签**
(`chat_resident_consumer.py:3688`),没有执行闸 —— 而且 V1 **做不到**:它的工具面
由外部 CLI(pi)按轮决定,后端插不上手。V2 拥有循环控制权,所以这是
**V2 能做而 V1 做不到**的一处收紧。

**分道处理,判据是「这一轮里有没有用户本人的授权」:**

- **前台聊天(共享中)——不收紧。** 用户本人发了消息,那条消息就是授权
  (`tool_loop.py:1041` 注释:「still carries the original user/wake seed」)。
  保持现有 `initial_outbound_tools_blocked`(拦 web/fetch/task,不拦写操作)。
  若收紧写操作,会打断「一边共享一边说『帮我记一下这个』」这个核心流程 ——
  `external_content_seen` 一旦置位**整轮不解除**(全文件无复位点)。
  ⚠️ 残留风险如实记录:此路暴露面与 V1 相同,不优于 V1。

- **screen_watch 唤醒——收紧,但只收身份写。** 这条道用户**完全没说话**,
  整轮唯一输入就是屏幕上的字,没有任何人授权过任何写操作。
  但**不是**把 8 个写操作全拿掉:`memory_write` / `schedule_wake` 在唤醒里
  有正当用途(看到屏幕上有事,记一笔或提醒一下),砍掉会伤产品。
  **只拿掉身份写**——它改的是「伴侣认为你是谁」,用户最难察觉、最难还原,
  且 screen_watch 没有任何正当理由去改身份。

**实现要点:**

1. 复用**已有**的 `wake_disabled_tool_names`(`worker.py:7588`),在 screen_watch
   且有屏幕帧时并入身份写集合。**不动 `tool_loop`** —— 因此不触碰共享执行路径,
   不需要架构红线裁定(这是本方案相对「加 `initial_external_content_seen`」的
   主要优点)。
2. 身份写集合**必须派生,不许硬编码那三个名字**:

   ```python
   _IDENTITY_WRITE_ACTIONS = frozenset(
       a for a in cap_registry.WRITE_ACTIONS if a.startswith("identity_")
   )
   ```

   硬编码 `{"identity_patch","identity_nudge","identity_dimensions_set"}` 会在
   将来新增身份写操作时**静默漏掉**,而且不会有任何测试变红。
3. **其余写操作只埋点不拦**:记录 screen_watch 轮内发生的每一次写操作
   (工具名 + 是否有屏幕帧)。零产品代价,且把「这个风险到底发不发生」变成可测。
   目前**没有任何证据表明该攻击发生过** —— 先测量再决定是否继续收紧。

**验收(与本规格「安全闸必须断言全集」判据一致,案例库模式 22):**

- screen_watch + 有屏幕帧 → 首轮 tools **不含**任何 `identity_*` 写操作;
  且**仍含** `memory_write` / `schedule_wake` / `reply` / `screen_read`。
- screen_watch + 无屏幕帧 → 身份写仍在架上(证明是帧触发的,不是常态禁用)。
- 前台聊天 + 有屏幕帧 → 写操作**全部仍在架上**(证明没有误伤前台)。
- 测试里的工具名集合**从模块读取**,不得写死(否则常量一改测试静默失配)。

### B1-4 回复语言指令

- **现状**:`backend/chat/reply_language.py:153` 从未被 V2 调用。
  `context.py:787` 调了 `infer_reply_language_policy`,但产物**只用于本地化星期几标签**
  (`context.py:793`)——典型孤儿。前台靠事后纠正一轮(`worker.py:11866-11877`),
  **四条唤醒道什么都没有**(`worker.py:8122, 8193` 仅埋点)。
- **要求**:四条道补语言指令;前台的事后纠正保留(便宜的双保险)。

### B1-5 主动消息来源标记

- **现状**:`context.py:422-423` `_norm_role` 把 `agent_initiated_proactive` 与普通
  回复一起压成 `"assistant"`,模型看不出哪几条是自己戳了没人理。
- **V1 对照**:`chat_resident_consumer.py:11949-11957` 行内标注 `agent(proactive)`。
- **要求**:对齐。这直接关系到「主动消息重复/话痨」的自我抑制。

### B1 验收(硬要求)

真实弱模型 live E2E(本地 rig,`deepseek-chat` 或同档),**不接受 seeded 测试绿**:
1. 心跳:给定有显著变化的感知状态,弱模型 **0 次主动工具调用**的情况下,
   回复中体现具体事实(地点名/歌名/App 名之一)。
2. screen_watch:弱模型 0 次工具调用下能引用屏幕上的实际文字。
3. 语言:用户说中文,四条道产出全中文,零英文状态行。
4. 回归:强模型行为不劣化(注入不得挤掉原有内容 → 必须计入 turn 预算)。

---

## B2 — 用户可见泄漏的 harness 闸(fail-OPEN 优先)

### B2-1 思考气泡内容零校验

思考是**用户可见面**,但 `_select_thinking_surface`(`worker.py:5297`)把模型的
`<think>` 正文原样封进 `agent_summary` 就发。`self_thinking.py` 的解析器对**结构**
确实 fail-closed(破损标签绝不泄漏),但对**内容**毫无判据。而
`self_thinking.INSTRUCTION` 自己点名的两条最常见违规,**零backing**:

- **语言漂移**(`self_thinking.py:65-72`,指令自称「最常见的失误」):
  `v2_language_follow.classify_writing_system` **已存在、已在跑**,
  但 `worker.py:11855` 传的是 `text`(可见回复),**从未指向思考文本**。
  → 把思考文本也分类,复用现成的 `FinalReplyCorrectionRequest` 路径。
  **这是本规格里性价比最高的一条:仪器现成,只是没接上。**
- **内部词泄漏**(`self_thinking.py:74-75`「不出现工具名、参数、字段名」):
  `_sanitize`(:93-102)只剥控制字符 + 截断 240,不看内容。
  → 工具名是**闭集**,`cap_tool_schema.build_tool_specs()` 可枚举。
  命中则走已有的 `THINKING_FAILED_MARKER` 分支,不发布。

### B2-2 交互写卡道没有卡面校验

- **现状**:`card_text_rejection` / `sanitize_card_labels` 在
  `backend/model_api_runtime/v2/worker.py` 出现次数 **0**(实测)。capture 道跑了
  这两个校验并能整批打回;**交互 `memory_write` 这条道一个都没跑**,
  `_memory_tool_actions`(`worker.py:4660`)是纯字段搬运。
- 于是「绝不叫对方"用户"」「bucket 单语言」等全部规则,**只是贴在工具描述上的散文**
  (`backend/capabilities/tool_schema.py:613`)。
- **要求**:交互写卡道跑同一套校验,失败返回有界工具错误(dispatcher 已支持回喂)。
  ⚠️ **只拦不改写**——「出口只拦明显不对、永不判内容」是 dream 阀门那次的定论,
  本规格不推翻它。拦下后交回模型重写。

### B2-3 `threads` 无边界

`tool_schema.py:78-83` 的 `threads` 是裸 `array of string`,无 `minItems`/`maxItems`;
描述里写着「1-4 个」但 `validate_tool_args`(:910-945)从不检查数量。
→ 一行 schema 修复。

---

## B3 — 已有强制的空转

### B3-1 `tool_choice` 只对 openrouter 生效

`tool_loop.py:1190-1200`:

```python
if (forced_delivery_tool and not terminal_text_round and tools is not None
        and str(getattr(provider_config, "provider", "")).strip().lower()
        == "openrouter"):        # ← 字符串等值判断
    provider_kwargs["tool_choice"] = {...}
```

其他 provider 上,工具面收窄是**建议性的**,模型照样能返回纯文本 →
`required_file_missing`(:1663-1690),用户拿到散文而不是承诺的文件。
→ 扩到所有支持具名函数强制的 provider。紧邻的 `tool_choice="none"`(:1186)
已经演示了按 provider 能力分派的写法。

### B3-2 沉默需要一等表达(需 Seven 二次确认后再做)

现状:唤醒道的「沉默」**只能通过 `<think>` 协议表达**
(`worker.py:882-894`:输出一个完整 think 块、闭合后什么都不写)。
弱模型写不出 `<think>` → **沉默物理上不可达** → 每次唤醒必说话。

候选解法:唤醒工具面加一个零参 `stay_silent` 工具,「沉默」变成结构化工具调用,
「说话」是文本,两者都可机检、无需解析自由文本。

⚠️ **此条动的是唤醒语义,属于产品哲学范畴,未获授权前不要实现。**

---

## 通用约束

- **只拦不改写**:任何新闸只做「拦下 + 交回模型重写」,不替模型改内容。
- **注入必须计入 turn 预算**:B1 的所有注入走 `prompt_frontier`,不得挤掉人设/记忆。
- **强模型不得劣化**:每批都要有强模型回归。
- **不接受 seeded 绿**:验收必须真实模型 live E2E,且用最弱档模型。
- **埋点**:每个新闸都要有可归因的失败码,不许静默吞。

## 遇阻升级

卡住超过 10 分钟就回报,不要自行扩大范围。
**严禁造工具或改被测代码来凑证据。**
本规格里任何一条如果实现下去发现前提不成立(例如某字段其实有注入路径),
**停下回报,不要顺手改设计**——前提错了要先纠正规格。

## 证据状态

- 我(claude2)**亲自核实**:B1-1、B1-2、B1-3、B2-1(语言分类器指向)、B2-2
  (两个符号计数为 0)、B3-1(provider 等值判断)、以及三个模型的线上 thinking 数据。
- **子代理报告、我未逐条复核**:B1-4、B1-5、B2-3,以及各条中的部分 V1 行号。
  实现前请先自行核对 file:line,对不上就回报。

---

# B4 — V1 拦了 V2 没拦的真回归 + 明确会遇到的洞(Seven 2026-08-16 定稿)

全量审计枚举 22 处「指望模型自觉」,Seven 按两条规则筛定:
**① V1 拦的 V2 也要拦(V2 只会更软);② V1 没拦但重要/遇到过/明确判定会遇到的也要拦。**

**收敛结果:22 条 → 7 条要修。** 其余 15 条的处置与理由见本节末尾,**不要重新翻案**。

## 顺序(有依赖,不要乱序)

B4-1 `#21` → B4-2 `#17` → B4-3 `#15` → B4-4 `#18` → B4-5 `#3` → B4-6 `#5` → B4-7 `#6`

`#21` 必须先做:`#17` 的强制版依赖它。

---

## B4-1 (#21) `tool_choice` 强制只对 openrouter 生效

- **事实(claude2 亲验)**:`tool_loop.py:1191-1200` 的强制分支带
  `str(getattr(provider_config,"provider","")).strip().lower() == "openrouter"`。
  其他 provider 上工具面收窄只是**建议性**的,模型照样能返回纯文本 →
  掉进 `required_file_missing`(:1663-1690),用户拿到散文而非承诺的文件。
- **影响面(prod 实测)**:23 个 V2 用户里 openrouter 只有 **2** 个;
  `openai_compatible` 15、`deepseek` 5、`gemini` 1 —— **21/23 空转**。
- **修法**:换成按「该 provider 的 wire 支不支持具名函数强制」分派。
  紧邻的 `tool_choice="none"`(:1186-1187)已经演示了按能力分派的写法,照抄形状。
- **验收**:每个受支持 provider 各一条用例断言 `tool_choice` 真的进了 provider_kwargs;
  不支持的 provider 断言**不进**(别把不支持的打挂)。

## B4-2 (#17) 沉默不是一等表达

- **V1 怎么做的(已查实)**:`chat_resident_consumer.py:12708-12719` 的
  `_reply_protocol_block()` 给两个**并列出口**:

      - speak:      {"messages":["..."]}
      - stay quiet: {"actions":[{"type":"proactive.sleep","reason":"..."}]}

  沉默是**有名字、带理由的动作**;`:14742-14756` 落库为
  `status=completed, wake_result=sleep`,理由进 admin 行。
- **V2 现状**:沉默 = 输出一个完整 `<think>` 块、后面什么都不写
  (`worker.py:975 _THINKING_ONLY_NO_REPLY_REASON`, `:7763`)。
  **而这个形状在 V1 里恰恰被记为 `failed / empty_agent_reply`** —— 两个运行时
  对「它选择了不说话」的定义是**相反的**。
- **后果**:弱模型写不出 `<think>` → **沉默物理上不可达** → 每次唤醒必说话。
  这正是 Seven 反复反馈的「V2 主动唤醒乱说话」。
- **修法**:唤醒道工具面加 **`stay_silent(reason)`**。
  说话=调 reply/出文本;沉默=调 `stay_silent` 带理由。两者**都机器可判**,不解析自由文本。
  弱模型调工具远比精确吐 `<think>` 标签可靠。
  落库对齐 V1:`completed` + `wake_result=sleep` + 保留 reason(V2 目前完全没有理由字段)。
- ⚠️ **不要删除现有的 thinking-only 沉默路径** —— 那是既有行为,新增出口不等于移除旧出口。
- **强制版(依赖 B4-1)**:唤醒轮 `tool_choice` 限定 `{reply, stay_silent}` 二选一。
  B4-1 未落地前先只做「提供工具 + 落库」。
- **验收**:弱模型 live E2E —— 高「24h 已主动次数」情境下能走 `stay_silent` 并留下理由;
  admin 能看到该理由。

## B4-3 (#15) 回复写入没有 CAS(消息重复)

- **V1(已查实)**:`backend/core/store.py:980 chat_finalize_reply_once` 是数据库 CAS;
  `chat/service.py:622 chat_try_claim_reply`;重复认领返回
  `chat_core.py:1214 {"error":"already_answered"} 409`。
- **V2(claude2 亲验)**:`v2/serve_worker.py:3361` **直接调 `store._build_chat_message`**,
  绕过 `chat_finalize_reply_once`,`reply_to_message_id` 只是个普通字段,**无 CAS、无 409**。
  `v2/jobs_store.py:4029 turn_answered_by_real_reply` 存在但只是只读遥测,不是写闸。
- **这就是 Seven 反复报的「消息重复」一族的结构性来源。**
- **修法**:V2 回复写入走**同一条** CAS 终结路径。⚠️ 共享实现,禁止另写一份。
- **验收**:并发两次终结同一 turn → 第二次必须被拒;突变(拆掉 CAS)必须有用例变红。

## B4-4 (#18) V2 被显式豁免卡死回收(定时提醒丢失)

- **事实(claude2 亲验)**:`backend/proactive/poll_core.py:35`
  `_HOSTED_CONSUMER_IDS = frozenset({"hosted_runtime","hosted_runtime_v2"})`,
  `:63` `if consumer_id in _HOSTED_CONSUMER_IDS: continue` ——
  **V2 卡在 claimed/realizing 的任务永远不会被回收重投**。
  V1 有 `reclaim_stale_resident_jobs`(600s 租约)把它退回 pending。
- 叠加 `scheduled_wake_v2.py:1152-1166` 在**入队时**就 `mark_fired`,
  于是一个卡住的提醒**永久丢失**且不重试。
- **修法**:给 V2 一条等价回收路径。⚠️ 注意 V2 有自己的租约/世代栅栏,
  不要照搬 resident 的 600s 常量 —— **先读 V2 的租约语义再定**,对不上就回报。
- **验收**:构造一个卡在 realizing 的 V2 job,断言超过租约后回到 pending 并被重投。

## B4-5 (#3) 思考气泡缺内部**字段名**黑名单

- **V1(已查实)**:`chat_resident_consumer.py:4456-4468` 的 denylist
  (`session_id`/`uuid`/`input_tokens`/`cache_read`/`permission_denials`/
  `terminal_reason`/`modelUsage`/`costUSD`),在 `:4853` 逐行丢弃。
- **V2**:B2-1 已补**工具名**(从 `build_tool_specs` 派生),但**没有这份字段名黑名单**;
  `worker.py:5286-5294 _sanitize_reasoning` 的 docstring 明写
  "Only length-caps and trims; renders reasoning as-provided"。
- **两份是互补的**:V1 拦字段名不拦工具名,V2 现在拦工具名不拦字段名。
- **修法**:V2 补上等价字段名判据。⚠️ 与 V1 **共享一份**,别两处各写。
- **验收**:突变(移除判据)必须红;且**接线**与**机制**分别有用例
  (B2-1 上一轮就是机制有测、两个接线点都能删掉而全绿)。

## B4-6 (#5) 编造记忆 —— 注入事实,不是加闸

- **现状**:`v2/context.py:238-241` 四十个中文字的提示词,作用于**每一轮前台对话**,
  **无任何代码检查**。V1 同样只有提示词(`agent_runtime/agent_tools_prompt.md:105-120`),
  所以这不是回归,是两代都欠的债 —— Seven 按规则②纳入。
- **关键**:**不要做出口内容判定**(会撞上「出口只拦明显不对、永不判内容」的定论)。
  正解是**给事实**:dispatcher 本来就知道这一轮 `memory_search`/`memory_fetch`
  返回了几条。返回 0 条时,把「已检索、无命中」作为**确定性事实**注入
  `runtime_control`(`context.py:134-140` 声明该块为权威),而不是求模型自己承认。
  与 B1 给心跳灌真实事实是同一招。
- **验收**:弱模型 + 花园里没有相关卡 → 回复不出现编造的回忆;
  且**零次模型主动工具调用**时该事实依然在场。

## B4-7 (#6) 声称改了却没调工具

- **现状**:`capabilities/tool_schema.py:461-464` 提示词「Do not merely say the change
  is done」,两代都无检测器。用户看到「好的,以后叫你999」→ 身份卡没动,下轮打回原形。
- **修法**:抄现成的 `_claims_image_delivered`(`v2/tool_loop.py:229`,弹一次回炉
  `:1601-1620`)。检测「终局文本声称改名/改身份完成」且本轮 `historical_tool_names`
  里没有身份写动作 → 注入一次纠正后重来。
- ⚠️ **只弹一次,只拦不改写**;弹完仍不调就照常发,不要把回复吞掉。
- **验收**:突变(拆检测器)必须红;并断言**不误伤**——正常闲聊不触发回炉。

---

## 明确不修的 15 条(定稿,不要翻案)

| 处置 | 条目 | 理由 |
|---|---|---|
| 已修(本轮 B1/B2) | #1 卡面校验、#2 思考语言、#22 threads 边界 | #2 V2 现已**强于** V1 |
| 改无可改 | #7 文件/图片声称、#8 屏幕陈旧、#16 屏幕 live、#19 capture JSON | V1/V2 **字面同一份实现** |
| V2 已更强 | #11 历史缺口、#13 花园卡片指令、#14 压缩摘要 | V1 根本没有这些防护/功能 |
| 提示词足够 | #9 提到唤醒、#10 叙述看屏幕 | 根因(user 角色 nudge)已在 B1 结构性移除;出口按内容拦会撞上「永不判内容」定论 |
| **Seven 定:不修** | #20 capture 产出下限 | **和 V1 一样,没有下限,不写卡也行** |
| **Seven 定:不修** | #4 capture 卡带「用户」 | 提示词已是最强措辞;确定性改写器**实测会改坏真实内容**(「月活用户在下降」→「月活小雨在下降」);真因(转写标签未透传姓名)已修;残留指标只落 trajectory(48h 环、需 runner 本地工具)**fleet 级不可读**;且该计数**本就不等于泄漏**(本人正当谈论产品用户也会计上) |
| 延后 | #12 外部文字当指令 | 执行闸,B1-3a 已做一部分 |

## 通用约束(沿用,已被证明有效)

- **接线与机制分别可突变打红** —— 两者是**两个不同的洞**。
- 断言**全集**不是子集;常量/集合**从模块读取**,测试不得写死。
- **只拦不改写**;共享实现,禁止两处各写。
- **改过的文件跑它自己的测试**再报数;确认看到 `N passed`
  (本机 zsh 不做词分割,文件列表塞进变量会静默收集 0 个用例)。
- 不接受 seeded 绿,验收用最弱档真实模型。
- 前提不成立**停下回报,不要顺手改设计**。
