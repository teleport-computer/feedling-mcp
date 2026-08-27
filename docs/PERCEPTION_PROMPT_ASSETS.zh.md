---
document_lifecycle: current
canonical_owner: self
---
# 感知 prompt 资产清单

这是 Perception prompt 的现行 owner 清单：列出 V1/V2 两条 runtime 中和「感知」直接
相关的文本、内核常量与挂载协议。提取工作已经完成；下表用于防止后续修改重新制造
重复字面量，或把 runtime 的 role/安全协议误搬进纯内核。表中的行号是提取期定位快照，
核对现状时以模块和稳定符号为准。

## 判定规则

**跟「怎么读感知」有关的 → 内核**（`perception_kernel.prompts`）：
如何解读 `perception_glance` / `presence_hints` / 跨域看板、什么时候该说话/该沉默、
怎么把感知事实自然用进回答而不是逐项播报——这是"感知判断"本身的说明书，与哪条
runtime、哪种传输协议无关。

**跟「块是什么 role、能不能当成用户请求、工具预算」有关的 → 留 runtime**：
运行时数据块该标成 `user` role 还是 `assistant` role、要不要在块顶部加
"UNTRUSTED ... not user instructions" 免疫标头、读取感知工具后是否封锁本回合
继续调 web/MCP/subagent——这是"这条 runtime 怎么把感知数据安全地喂给模型"，
是传输层/安全边界，不是感知判断本身，必须留在各自 runtime 里。

## 资产表

> **2026-08-20 Task 5 + Task 6 均已落地**：`backend/perception_kernel/prompts.py`
> 现在是下表「内核常量」列这些常量的唯一出处，`v2_*` 三行已从各自的 runtime 文件里
> 把这些常量拼回去（golden 7/7 绿）。`v1_reachout_context*` 三行的常量
> （`perception_kernel.prompts.V1_GLANCE_HOWTO` / `V1_BOARD_HOWTO`）也已建好，
> `tools/chat_resident_consumer.py` 已改成引用这两个常量，不再是字面量
> （函数签名不变，golden 仍 7/7 绿）。

| 键（fixture） | 适用线 | 回合种类 | 挂载的 role 层 | 出处（文件:行号） | 内核 or runtime | 内核常量 |
|---|---|---|---|---|---|---|
| `v2_wake_system` | V2 | 主动：`heartbeat` / `manual_wake` lane（`scheduled` 无提醒笔记时兜底、`screen_watch` 除外） | wake 回合的 `system_prompt`（经 `_wake_system_prompt_for_lane` 加自思考后缀、经 `context._join_policy_blocks` 拼语言策略），非 `_RUNTIME_CONTEXT_POLICY` 那层共享系统层 | `backend/model_api_runtime/v2/worker.py:876` `_WAKE_SYSTEM_PROMPT` | 内核（说的是"该不该开口、怎么用 perception_glance 判断"，与 role/协议无关） | `perception_kernel.prompts.V2_WAKE_PERCEPTION_CLAUSES`（只取"A perception_glance…is needed."这一段；前后的"platform presence / 该不该说 / never mention this wake"仍是 worker.py 里的 runtime 文案，未搬） |
| `v2_scheduled_wake_system` | V2 | 主动：`scheduled` lane 且确有到期提醒笔记时，替换上面那条 | 同上（`_run_wake` 内按 `wake_system_prompt` 变量整体替换） | `backend/model_api_runtime/v2/worker.py:902` `_SCHEDULED_WAKE_SYSTEM_PROMPT` | runtime（这是"把用户已设定的提醒念出来"的投递规则，不是感知判断——它甚至禁止沉默） | 无（不含感知阅读文案，原样留在 worker.py，未搬动） |
| `v2_runtime_perception_policy` | V2 | 前台聊天 + 全部主动 lane 共用 | `_RUNTIME_CONTEXT_POLICY` 的一段，拼进稳定/可缓存的 `system` role（`context.py:653` `trusted_parts.extend((system_prompt, _RUNTIME_CONTEXT_POLICY))`），对前台/主动都生效 | `backend/model_api_runtime/v2/context.py:147-162`（`_RUNTIME_PERCEPTION_BEHAVIOR_POLICY` + `_RUNTIME_PERCEPTION_PROTOCOL_POLICY` 合并成 `_RUNTIME_PERCEPTION_POLICY`） | 内核（"把事实用进回答别汇报来源""glance 是低分辨率事实板别逐项播报"——纯感知解读规则） | `perception_kernel.prompts.V2_PERCEPTION_BEHAVIOR_POLICY` + `perception_kernel.prompts.V2_PERCEPTION_PROTOCOL_POLICY`（context.py 里的两个同名私有变量现在整块 = 内核常量，`_join_policy_blocks` 拼接方式不变） |
| `v1_reachout_context` / `v1_reachout_context_empty` / `v1_reachout_context_change_only` | V1 | 主动：resident consumer 的 native reach-out（proactive job，等价于 V2 的 wake） | 不分 role——V1 单消息串拼进 `_message_for_proactive_job`（`tools/chat_resident_consumer.py:12889`）整体发给 CLI/HTTP agent 的 query 文本里 | `tools/chat_resident_consumer.py:13011` `_native_reachout_perception_context()`（纯函数，固定输入调用取值） | 内核（"这是低分辨率一瞥、别逐项播报、挑 2-3 个有共鸣的、数字别念、信号偏低要轻拿轻放"——和 V2 的 `_RUNTIME_PERCEPTION_POLICY`/`_WAKE_SYSTEM_PROMPT` 是同一件事的 V1 版本） | `perception_kernel.prompts.V1_GLANCE_HOWTO` / `V1_BOARD_HOWTO`，`chat_resident_consumer.py` 已改成引用，函数签名不变 |
| `v2_tool_schema_perception`（dict：`perception_snapshot`/`perception_recent_apps`/`perception_trend`/`perception_history` 四条） | V2 | 前台聊天 + 全部主动 lane 共用（工具目录不分回合种类，任何回合都能看到并调用） | 不是 `system_prompt` 拼接文本，而是各工具在 tool-calling 协议里的 `description` 字段（模型据此决定何时调用该工具，不是常驻上下文） | `backend/capabilities/tool_schema.py` 的 `DESCRIPTIONS` 字典 | 内核（说的是"这个工具该在什么情境下调、返回什么粒度"——和感知判断是同一件事，只是挂在 tool description 而非 system prompt 上） | `perception_kernel.prompts.PERCEPTION_TOOL_NOTES[name]`——**只取每条描述里"这个返回值该怎么解读"那一句**（如 `perception_snapshot` 的 app 字段只是 15 分钟内的开合事件、`perception_recent_apps` 的 `apps=[]`/`disabled=true` 区别、`perception_trend` 的 baseline/delta 不要混为一谈）；"这个工具能读什么域、怎么调用、参数默认值"仍是 tool_schema.py 里 `_PERCEPTION_USAGE_GATE`/`_PERCEPTION_DOMAINS`/`_PERCEPTION_DEFAULTS` 拼的调用协议文案，未搬——四条描述里各自只有一句真正被替换成内核引用，`perception_history` 那条本身没有"怎么解读"分句，故 `PERCEPTION_TOOL_NOTES` 只有前三个 key |

## 备注

- `v1_reachout_context_empty` 不是独立资产，是同一个函数在 `presence={}, change=[], domains=None`
  时的空输入分支输出，用来锁住"什么都没有时"的兜底文案（`cross_domain_board_json: {}` 那条路），
  防止后续重构悄悄改动空态分支。
- `v1_reachout_context_change_only`（`presence` 非空、`change` 非空、`domains=None`）同样不是
  独立资产，锁的是同一函数里 `elif change:` 的旧协议回退分支（`perception_change_json:` 文案）——
  `v1_reachout_context`（board 非空）和 `v1_reachout_context_empty`（全空）都不会走到这条分支，
  漏了会让这段文案长期不被 golden 覆盖（2026-08-20 review 补）。
- V1 没有 V2 那种"稳定 system 层 vs 逐回合 runtime_data 层"的显式分层——一次 native
  reach-out 就是一个字符串，`_native_reachout_perception_context` 只是其中一段。列进
  「内核」是因为**内容**是感知判断说明书，不代表 V1 也有对应的"role 挂载点"概念。
- 本文档覆盖 fixture 里全部 7 个键对应的资产（4 段常量文本 + 1 个 dict 资产 + 同一函数的
  3 种分支输出）；`_message_for_proactive_job` 里其余的部分（`wake_metadata`、
  `_reply_protocol_block`、`_local_time_anchor` 等）属于 V1 的投递/协议层，不在本次感知
  内核提取范围内，未列入表格。
