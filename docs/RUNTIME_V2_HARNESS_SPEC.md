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
- **要求**:V2 心跳/screen_watch 唤醒注入等价内容。字段与上限逐条对齐 V1,
  不新增、不扩容。工具仍保留,模型可自行深挖。

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
