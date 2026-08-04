# Runtime V2 感知分层对齐 V1 设计

**日期：** 2026-08-03

**状态：** 已完成方案讨论，待实现计划

**范围：** Hosted Runtime V2 的 chat / proactive wake 首轮感知 grounding

## 1. 背景与问题

当前 Runtime V2 会在普通聊天以及除 `screen_watch` 外的大多数 wake 首轮中，预取完整
`perception_snapshot`，再通过 allowlist 把数值和布尔字段直接放入模型 prompt。注入字段包括
电量、温度、步数、睡眠分钟、心率、活动量和身体指标等。

这一设计解决了模型不知道感知工具、面对“我今天走了多少步”时直接回答无法读取的问题，但也产生了
新的行为风险：在 heartbeat 或感知 wake 中，小模型会把首轮看到的结构化数字当作应当汇报的内容，
连续复述电量、天气和健康数据。单靠 prompt 中的“不应逐项播报”指令无法可靠约束指令遵循较弱的模型。

V1 的总体策略更接近分层读取：普通聊天由模型按需调用感知工具；主动唤醒先获得低分辨率概览，
需要细节时再拉工具。但 V1 的现有 cross-domain board 仍包含精确数字，因此本设计对齐的是 V1 的
分层意图，而不是逐字段复制 V1 的实现。

## 2. 目标

1. 普通聊天不再被动获得实时感知值；只有明确需要时才通过工具读取。
2. 主动唤醒首轮只获得无精确数值的低分辨率感知概览。
3. 感知事件 wake 只突出本次变化，不捎带无关设备、天气和健康状态。
4. 从数据边界上阻止主动消息演变成设备状态播报，而不是依赖模型遵守软提示。
5. 保留 Runtime V2 的原生工具循环、权限判断、文本读取出站 fence、事件合并和 prompt-cache 结构。
6. 用户明确询问精确数据时，模型仍能通过工具正确回答。

## 3. 非目标

- 不修改 iOS 感知上报协议。
- 不修改感知数据存储、加密、TTL 或权限模型。
- 不改变工具返回的精确数据契约。
- 不把 V1 cross-domain board 原样移植到 V2。
- 不在本次工作中重新设计感知事件的产品开关或 UI。
- 不以关键词后处理删除模型回复中的数字；合法的数字回答必须保留。

## 4. 核心设计

### 4.1 Foreground chat：感知值改为纯工具拉取

`chat` lane 不再调用 `_perception_grounding_results()` 生成静态
`runtime_data.perception_snapshot`。系统仍向模型提供原生感知工具 schema；当用户的问题依赖当前状态时，
模型调用 `perception_snapshot`、`perception_trend`、`perception_history`、`photo_read` 或相关工具。

这意味着普通聊天首轮不会自动出现电量、温度、步数、睡眠、心率等数值。用户明确询问时，工具调用结果
可以包含精确数据，模型应直接、自然地回答。

### 4.2 Proactive wake：使用无数值 `perception_glance`

除 `screen_watch` 和 `scheduled` 外的主动 wake，可以在首轮注入一个由服务端确定性生成的
`perception_glance`。该对象只能包含固定枚举、布尔值、`unknown` 和稳定的结构字段，不得包含原值、
基线、差值、百分比、分钟数、温度或计数。

建议的最小结构：

```json
{
  "location": {"available": true, "notable_change": false},
  "media": {"available": true, "active": true, "notable_change": false},
  "app": {"available": true, "recent_activity": true},
  "health": {"available": true, "notable_change": true},
  "weather": {"available": true, "notable_change": false},
  "mood": {"available": false, "recorded": false},
  "reminders": {"available": true, "has_due": false, "has_overdue": false},
  "calendar": {"available": true, "has_upcoming": true},
  "photos": {"available": true, "recent_activity": true},
  "screen": {"active": false}
}
```

第一版不引入“天气很热”“睡眠偏少”“活动量更高”等产品阈值。`available` 只表示对应域存在至少一个已授权、
新鲜且非空的字段；`active` / `recorded` / `has_due` / `has_overdue` / `has_upcoming` /
`recent_activity` 只由现有结构字段是否存在或现有布尔/计数是否大于零投影而来，输出中不保留原计数。
`notable_change` 只复用现有 `perception_history.notable_changes()` 是否为该域返回条目这一判断，不新增阈值。

第一版实现应遵循 YAGNI：只有当现有感知数据能够通过上述明确、稳定、可测试的规则映射到某个字段时才输出；
无法可靠归类的域直接省略。glance 不承担自然语言总结，不调用模型生成，也不对健康或环境状态下结论。

### 4.3 感知事件上下文：只描述本次变化

V2 Differ 和 wake context 继续保留 `trigger`、`change_digest`、`presence_hints`、`origin_refs`，但进入
首轮 prompt 前必须投影成有界、与本次事件相关的结构：

- `unlock_after_absence`：只表明用户在一段离开后解锁。
- `arrived_at_anchor`：只表明地点锚发生变化；用户命名文本仍按低信任数据处理。
- `photo_added`：只表明新增照片，并可携带安全粗分类；不自动读取像素。
- `scene_change`：交给独立 `screen_watch` 链路，不附加普通感知快照。

感知事件不得成为附带完整电量、天气、步数和睡眠状态的理由。若事件详情需要文本或精确值，模型应调用
相应工具。

### 4.4 Lane 行为矩阵

| Lane | 首轮自动感知 | 精确数据获取 |
| --- | --- | --- |
| `chat` | 无 | 原生感知工具 |
| `heartbeat` | 无数值 `perception_glance` | 原生感知工具 |
| 感知事件合并到 `heartbeat` | glance + 本次事件投影 | 原生感知工具 |
| `manual_wake` | 无数值 glance | 原生感知工具 |
| `scheduled` | 仅用户设置的提醒内容 | 需要时调用工具 |
| `screen_watch` | 仅安全的 frame 数量等元数据 | `screen_recent` / `screen_read` |

### 4.5 回复策略

主动 wake 的稳定 system policy 增加以下语义：

- glance 是决定是否深入查看的线索，不是播报清单。
- 主动消息最多围绕一个主题；不得把多个感知域组织成状态报告。
- 精确数字只有在用户明确询问，或模型主动拉取后确认确实与当前表达相关时才可使用。
- 没有足够具体、自然的表达理由时保持沉默。

这些规则是数据边界后的第二层防护，不承担根本正确性。根本保证来自首轮 prompt 中不存在精确数字。

## 5. 数据流

### 5.1 普通聊天

1. 用户消息进入 V2 `chat` job。
2. Runtime 读取对话、summary、profile、时间上下文和其他既有上下文。
3. Runtime 不预取感知快照。
4. Provider 首轮获得感知工具 schema，但没有实时感知值。
5. 如问题依赖感知，模型发起工具调用。
6. 工具结果进入下一轮；文本型私有读取继续激活既有出站 fence。

### 5.2 普通主动唤醒

1. Scheduler 创建 wake job。
2. Runtime 读取当前感知状态并运行纯函数 glance projector。
3. projector 丢弃所有精确数值和自由文本，只输出固定低分辨率状态。
4. glance 作为低信任 runtime data 注入首轮。
5. 模型决定保持沉默、直接表达一个主题，或调用工具深入读取。

### 5.3 感知事件唤醒

1. iOS 上报进入现有 store 和 V2 Differ。
2. Differ 产生离散 wake event；普通电量、时间、运动和播放变化继续不直接触发 wake。
3. event 与 heartbeat job 关联并参与现有合并机制。
4. Runtime 注入 glance 和事件投影，不注入完整 snapshot。
5. 模型只围绕本次事件判断是否表达；需要详情时调用工具。

## 6. 组件边界

### 6.1 Glance projector

新增独立、纯函数式 projector，职责仅为：

- 输入已授权且通过新鲜度判断的感知投影。
- 输出固定 schema 的无数值 glance。
- 对未知、缺失、禁用数据确定性降级。
- 拒绝任意自由文本进入输出。

projector 不读取数据库、不发网络请求、不生成自然语言、不决定是否发送消息。

### 6.2 Worker lane assembly

`backend/model_api_runtime/v2/worker.py` 负责按 lane 选择 grounding：

- 删除 chat lane 的 eager numeric snapshot。
- 普通 wake 改为调用 glance grounding。
- scheduled 和 screen_watch 保持各自独立语义。
- 感知 wake context 在进入 `action_context_str()` 前做事件投影。

### 6.3 Tool layer

现有 `perception_snapshot`、`perception_trend`、`perception_history`、照片和屏幕工具保持完整返回。工具层继续
使用 `perception.agent_fields` 作为字段来源，并保留权限、TTL、禁用原因与出站 fence。

## 7. 重复与新鲜度控制

- 相同 glance 不作为连续 heartbeat 主动表达的新理由。
- 电量、时间、温度的普通波动不产生独立 wake。
- 短时间内多个事件继续使用现有 coalescing，形成一个模型回合。
- `photo_added` 不捎带完整环境和健康状态。
- prompt 应明确利用已有 recent chat / last visible proactive 信息避免重复，但不增加回复后数字过滤器。

projector 同时计算 canonical JSON 的 SHA-256 指纹。worker 将“本轮指纹是否与该用户最近一次已完成普通
heartbeat 的 glance 指纹相同”投影为 `glance_changed` 布尔值，并把最新已完成指纹记录在既有 runtime
状态存储中。相同指纹不取消 heartbeat 本身，但不得被 prompt 描述成新的感知变化；模型仍可依据对话关系
选择表达或沉默。指纹和底层原值均不进入模型 prompt。

## 8. 安全与隐私

- `perception_glance` 只允许服务端定义的枚举和布尔值。
- 地点名、Wi-Fi、App、歌曲、日历标题、提醒标题、照片描述和 OCR 不进入 glance。
- 事件 context 和工具结果继续作为低信任 runtime data，而不是 system 指令。
- 文本型感知/照片/屏幕读取后的 web、MCP、task/subagent 出站 fence 保持不变。
- 禁止为了方便 glance 而扩大当前权限或解密边界。
- 禁用、过期和缺失一律表示未知，不能折算为零。

## 9. 错误处理与降级

- glance 预取或投影失败：省略 glance，wake 仍可正常保持沉默或使用工具。
- 单个域数据非法：只丢弃该域，不影响整轮。
- 工具调用失败：模型自然说明暂时无法读取，不猜测数值。
- 感知 grounding 失败不产生用户可见系统错误，也不阻断普通聊天。
- projector 必须 fail closed：遇到非预期文本、NaN、无限值或未知枚举时输出 unknown/省略。

## 10. 测试策略与验收标准

### 10.1 单元测试

- projector 对每个允许状态产生稳定输出。
- 精确数字、百分比、分钟数、温度和计数不会出现在 glance。
- 任意自由文本不会通过 glance schema。
- 缺失、禁用、过期和非法值正确降级。
- 相同输入产生相同 glance 与指纹。
- 相同已完成 heartbeat 的 glance 产生 `glance_changed=false`；事件 wake 不因普通 glance 相同而丢失事件。

### 10.2 Worker / prompt 测试

- chat 首轮 prompt 不包含电量、温度、步数、睡眠分钟或心率。
- chat 仍提供感知工具，工具调用后能取得精确值。
- heartbeat/manual wake 只注入 glance，不注入完整 snapshot。
- 感知 heartbeat 只附加本次事件投影。
- scheduled wake 不附加环境感知。
- screen_watch 继续只预取安全 screen 元数据。
- provider adapters 对 runtime-data 角色和结构保持一致。
- prompt caching 的稳定前缀不因本次改动退化。

### 10.3 行为回归

- “我今天走了多少步？”会触发工具并准确回答。
- “外面多少度？”会触发工具并准确回答。
- 连续相同 heartbeat 不产生重复状态播报。
- `photo_added` 主动消息不同时复述电量、天气和步数。
- 使用 Flash 级小模型运行固定样本，不得连续输出设备状态清单。
- Claude、Gemini、OpenAI/OpenAI-compatible 路由行为一致。
- 现有 prompt-injection、私有读取出站 fence、权限和感知 TTL 测试继续通过。

## 11. 文档与发布

该改动会改变公开可感知的 Agent 行为，因此实现时应：

- 更新 `docs-site/content/docs/workflows/perception.mdx`，说明普通聊天与主动唤醒的感知分层。
- 在 `docs-site/content/docs/changelog.mdx` 的 `Unreleased` 下记录行为变化。
- 若公开 API schema 未变化，不需要重新生成 OpenAPI；若实现引入或修改公开字段，则同步更新并运行
  OpenAPI 生成及契约测试。
- 按仓库规则运行相关后端测试，以及 docs-site 的 `types:check`、`lint` 和 `build`。

## 12. 实现顺序建议

1. 先用失败测试锁定 chat/wake 当前会泄露精确数字的行为。
2. 新增纯函数 glance projector 及单元测试。
3. 移除 chat eager grounding。
4. 将普通 wake grounding 替换为 glance。
5. 收紧感知事件 prompt projection。
6. 加入重复行为回归和小模型评测样本。
7. 更新公开文档与 changelog。
8. 跑完整相关验证。

## 13. 成功判定

实现成功必须同时满足：

- 首轮 prompt 中不存在可被主动复述的精确感知数字。
- 用户主动询问感知时，工具链仍能返回准确结果。
- 主动唤醒保留“有感知、能判断、可深入”的能力，而不是退化为完全失明。
- 小模型在重复 wake 样本中不再持续播报电量、温度、步数等设备状态。
- 既有安全、权限、加密和工具调用边界无回归。
