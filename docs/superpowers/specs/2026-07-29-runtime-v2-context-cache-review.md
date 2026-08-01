# Runtime V2 上下文/缓存架构外部提案核实 — 分析与可落地项

- 日期：2026-07-29
- 状态：分析完成 + P0 前置验证完成（见 §9），尚未动代码
- ⚠️ 验证中发现一个**正在发生的用户可见故障**（§9.4），优先级高于本文的所有优化项
- 输入：一份外部撰写的《Runtime V2 重构提案：分层记忆 + 冻结上下文架构》（Draft v1，
  参照 Nous Research 的 Hermes Agent 记忆系统）
- 核实基线：本地 `test` 分支工作树（`backend/model_api_runtime/v2/`）+ prod `v2_turn_metrics` 近 7/30 天实测

## 1. 背景：这份提案从哪来，以及它的前提缺陷

外部提案的成因是一次对 Hermes Agent 记忆系统的调研。Hermes 的三层结构（有界策展记忆
`MEMORY.md`/`USER.md` → FTS 全文 session search → 可插拔外部记忆 provider）确实值得参考，
其中 **frozen snapshot**（记忆在 session 启动时冻结进 system prompt、session 中途永不变，
明确为保 prefix cache）是核心洞察。

提案据此对 Runtime V2 提出四条批评：

1. 缓存冻结策略缺失；
2. 每轮对全量历史现场 compact，O(N²) 且必然打穿；
3. 完全没利用现有 Memory System；
4. 没有字符上限。

并给出一套重构方案：epoch 版本化冻结前缀、增量物化摘要、双 MD（`USER.md`/`MEMORY.md`）、
`chat_history_search` 检索工具、新增 `epoch_state`/`summaries`/`md_store`/`frontier` 四张表，
分三期落地。

**必须先说明的前提缺陷**：该提案的作者在对话中两次明确声明「GitHub 的 tree/blob 页面拦了
我的抓取器，容器网络也是关闭的，`backend/model_api_runtime/v2/` 下的源码我现在读不到」，
其全部分析建立在**文件清单（文件名 + 行数）+ 一个错误码名 `prompt_frontier_exhausted` 的
字面推测**之上，并自陈「哪些是确认、哪些是推断，我会分开说」。

它对 `prompt_frontier_exhausted` 的语义推测错了，而四条批评中有两条正是从这个错误推测展开的。
下面逐条核实。

## 2. 核实方法

- **代码侧**：直读 `test` 分支工作树全部相关模块（`prompt_frontier.py` / `summary_frontier.py` /
  `compaction.py` / `context.py` / `worker.py` / `serve_worker.py` / `workspace/prompt.py` /
  `capabilities/tool_schema.py` / `provider_client.py`）。
- **实测侧**：prod `v2_turn_metrics`（PR #94 落的 telemetry 层）近 7 天与近 30 天数据。
  该表已带 `cache_read_tokens` / `cache_write_tokens` / `cache_miss_tokens` /
  `prompt_frontier_exhaustion_count` / `effective_tail_turns` / `tail_fallback` 全套字段。

## 3. 逐条判定

| 提案批评 | 判定 | 依据 |
| --- | --- | --- |
| ① 缓存冻结策略缺失 | **不成立** | 缓存是这套代码的一等公民，见 §3.1 |
| ② 每轮全量 compact、O(N²)、必然 exhausted | **不成立** | 摘要早已是不可变分段物化，且 compaction 不在热路径，见 §3.2 |
| ③ 没利用现有 Memory System | **部分成立** | chat lane 确无常驻画像，但受安全边界约束，见 §3.3 |
| ④ 没有字符上限 | **部分成立** | 上限存在但偏松，见 §3.4 |

### 3.1 关于「缓存冻结策略缺失」— 不成立

prompt cache 在这套代码里被系统性地对待：

- `provider_client.py:813-940, 1162-1343`：按 provider 分别适配 `cache_control: ephemeral`
  （Anthropic / OpenRouter 的 content-block 形态）、`prompt_cache_key`（OpenAI）、
  `cachePoint`（Bedrock），并在 fallback 路径上显式保留这些字段。
- `serve_worker.py:702-726`：per-user 缓存亲和 key —— HMAC(`runtime_token_secret`,
  `"feedling:v2:prompt-cache:v3"` ‖ route_scope ‖ user_id)，附带一个 route 指纹。
  注释写明「provider 缓存绝不能收到裸 user id，且 key 必须随 runtime secret 轮换」。
- `workspace/prompt.py:1-6, 44-46`：skills 块按**canonical path 排序而非数据库插入时间**，
  文件头注释直书「keeps the stable skills prefix ahead of the mutable working-memory
  boundary so provider prompt caching can reuse the longest unchanged prefix」。
- `context.py:42-51`：特意不把变化的 perception/runtime 数据编码成尾部 system 消息，
  因为 Anthropic/Gemini/OpenAI-Responses 三家适配器都会把 system 消息**上提到对话最前**，
  那样会把变化块顶到可复用历史之前、直接打死缓存。改为尾部 user-role JSON 数据块。
- `provider_client.py:722-782`：把各家异构的 usage 字段归一化成
  `cache_read/write/miss_tokens`，落进 `v2_turn_metrics`，命中率随时可查。

也就是说，提案验收标准第 7 条「接入 provider 返回的 cache read/write token 指标」**已经是既成事实**。

### 3.2 关于「每轮全量 compact、O(N²)」— 不成立

`summary_frontier.py` 已经**就是**提案 §6 想要的东西，且比提案描述的更严格：

- 不可变、append-only 分段；level-0 叶子覆盖一段精确的源消息区间，
  checkpoint 是若干子段的父节点，**从不修改或删除子节点**，只在「模型可见的
  canonical cover」里替换它们（模块 docstring）。
- 层级 rollup，`DEFAULT_ROLLUP_FANOUT = 8`，`choose_rollup_candidate()` 确定性选段。
- `validate_canonical_frontier()` 用**三个 witness**（首/末 source seq、严格非重叠有序、
  跨 checkpoint 累计的精确源行数）证明覆盖完整无洞——因为 `chat_messages.seq` 是全局的，
  同一用户的相邻行未必数值连续，所以不能只靠 seq 相邻来证明。
- 有 CAS 并发保护（`head_version` pin + `upsert_summary_row_cas`）。

而 compaction **不在热路径**：`worker.py:10009-10019` 是在**回复已经写完之后**才
best-effort 入队一个 `maintenance` lane 的 job，`enqueue_job` 自带 per-user single-flight
coalesce，入队失败只记 `log.warning`，注释明写「压缩入队失败绝不能拖垮已经写成的这条回复」。

**提案的核心误解**：`prompt_frontier_exhausted` 不是「压缩走到尽头」。
`prompt_frontier.py` 是一个**纯预算会计模块**（docstring：「knows nothing about DB rows,
encrypted envelopes, workers, providers, or the tool loop」），只回答两个问题：某条
provider/model 路由适用哪个保守 context window；一组组件在预留输出和安全余量后是否放得下。
`PromptFrontierExhausted` 是「required 组件超出 model-aware input budget」的守卫异常，
**且刻意只携带计数和组件名、绝不存内容**，以便安全落进 metrics。

提案验收标准第 4 条要求「把这个错误码从代码里移除而非捕获重试」——那是删掉安全网，不能做。

⚠️ **但「从未触发」这个说法是错的**，见 §9.3：`prompt_frontier_exhaustion_count` 恒为 0
是观测缺陷，按 `status` 统计它确实触发过。

### 3.3 关于「没利用现有 Memory System」— 部分成立，但受一条提案不知道的安全边界约束

**成立的部分**：`read_memory_context()`（`serve_worker.py:1739`）只接在 `capture`/`dream`
两条抽取 lane 上，docstring 首行即「capture/dream prompt 要的记忆上下文」。chat lane
**零常驻用户认知**——模型想知道用户是谁，只能自己调 `memory_index`/`memory_search`/`memory_fetch`。

**提案不知道的约束有两条**：

1. **`WORKING.md` 已经存在，但故意不注入。** `/memory/WORKING.md`（`workspace/backends.py:23`）
   是 agent 可编辑的持久工作记忆，等价于 Hermes 的 `MEMORY.md`。生产路径显式传
   `include_working_memory=False`（`serve_worker.py:141`），`worker.py:1224` 注释说明：
   「working_memory field is accepted but never injected: editable /memory/WORKING.md is
   pull-only through workspace_read, which activates the outbound restriction」。
   即：读私有 memory/workspace 后，本回合禁止后续 web/MCP/subagent 出站调用。这是有意的
   安全决策，不是遗漏。
2. **identity 是 E2E 信封，服务器侧拿不到人格明文。** `serve_worker.py:1747-1749` 写明
   「identity 是 E2E 信封，服务器侧拿不到明文人格文本 —— 只取 get_identity 暴露的顶层
   非敏感明文（如有），否则降级为 ""」。Garden 内容要走 enclave readside + runtime token
   往返才能拿明文，而 enclave 是单线程瓶颈（`worker.py:7417-7419` 注释：两次读都是
   enclave-bound，必须同在 `enclave_sem` 闸内）。

所以提案 §7「server-side 存两份 MD，调用时纯读取」不能照抄——它假设了一个我们没有的
明文可读平面。

**一个提案没看见、但确实存在的产品级代价**：`memory_index`/`memory_search`/`memory_fetch`/
`identity_get` 全在 `_PRIVATE_READ_TOOLS`（`worker.py:559-569`）里，一调就锁死本回合出站。
于是当前形成一个二选一：**要么了解用户、要么能上网**。常驻画像块的最大收益其实不是省 token，
而是解开这个二选一。

### 3.4 关于「没有字符上限」— 部分成立

上限存在，但偏松：`summary_frontier.py:24-26` 的 `DEFAULT_MAX_FRONTIER_SEGMENTS = 24`、
`DEFAULT_MAX_FRONTIER_CHARS = 48_000`、`DEFAULT_MAX_ROLLUP_INPUT_CHARS = 120_000`。
4.8 万字符的摘要段与「有界才可冻结」的初衷有距离。这条方向成立，但优先级低于 §5 的前两项。

### 3.5 提案里确实指出了、而我们确实没有的东西

- **`chat_history_search` 不存在。** `capabilities/tool_schema.py` 的全部工具是
  identity_patch/nudge、memory_index/search/fetch/write、perception_*、screen_*、photo_*、
  web_search/fetch、schedule_wake/cancel_wake、workspace_list/read/write/delete。
  **记忆**可检索，**原始聊天历史**不可检索——模型只能看到 40 轮 verbatim tail + 摘要。
  「我们上个月说过什么」这类查询目前无路可走。
- **记忆写入无注入/不可见 Unicode 扫描**（提案 §7.3）。`memory/card_text.py` 有的是
  bucket/threads 标签规范化，不是威胁模式扫描。
  **但优先级低于 Hermes 场景**：Hermes 需要扫描是因为它把记忆放进 **system prompt**（带
  system 权限）；我们把摘要和记忆放在 **user role** 且带显式 UNTRUSTED 标签
  （`context.py:33-40` `_SUMMARY_HEADER`、`context.py:58-60` `WORKING_MEMORY_HEADER`、
  `context.py:453-458` 注释「Giving it a system role would turn a prompt-injected
  historical message into a durable privileged instruction」）。纵深防御的位置不同。
  ⚠️ **如果将来把画像块提到 system role，这条就必须先做。**

## 4. 实测数据与口径

### 4.1 数据（prod，`lane='chat'`，近 7 天）

| 指标 | 值 |
| --- | --- |
| chat turns | 101 |
| `prompt_frontier_exhaustion_count` 合计 | 0 — ⚠️**该计数器不可信，见 §9.3** |
| `tail_fallback` 计数 | **0** |
| `effective_tail_turns` 均值 | **40.0**（恒等于 `_CHAT_TAIL_MAX_TURNS`） |
| 平均 prompt | 24,178 tokens |
| 缓存命中率（token 加权、跨 provider 混合） | 47.8% |
| 缓存命中率（**按 turn** 中位数） | **30.3%**（p25 = 0.0%，p75 = 77.0%，n=91） |

**`tail_fallback = 0` 且 `effective_tail_turns` 恒为 40** 是一条强证据：预算从未削减过任何
一轮 tail，即当前根本没有「压不下」的问题。提案假想的 O(N²)/exhausted 危机在 prod 不存在。

### 4.2 口径缺陷（重要）

命中率公式与 `jobs_store.py:4204-4211` 内置的 `hit_ratio` 一致：
`cache_read / (cache_read + cache_miss)`。但这个数字有三个坑：

1. **miss 语义跨 provider 不统一。** Anthropic 路径把 `cache_write` 也计入 miss
   （`provider_client.py:735`，成本语义：新写入的缓存 token 付全价）；OpenAI 系根本不报
   write，miss 由 `prompt_tokens - cached_tokens` 推出。两者加权混算没有可比性：

   | 家族 | turns | read/prompt | hit_pct |
   | --- | --- | --- | --- |
   | anthropic 系 | 54 | 37.0% | 37.0% |
   | openai 系 | 37 | 54.6% | 59.5% |

2. **按 token 加权会被少数 turn 主导。** `anthropic/claude-sonnet-4.6` 仅 11 个 turn，
   却因跑了 177 次 provider call、贡献 315k miss + 272k write，把整体拉到 15.1%；
   另一端 `claude-opus-4-6` 是 91.0%。
3. **部分 turn 完全不进分母。** `[官key]deepseek-v4-pro`（5 turns）、`z-ai/glm-5.2`（2 turns）
   `usage_reported_calls = 0`，未上报 usage。

**结论**：47.8% 只能用来说明「缓存在工作、且远未饱和」，不能当作前缀稳定性的度量。
按 turn 中位数 30.3%、p25 = 0.0%（四分之一回合前缀完全零命中）更接近真相。

### 4.3 按 tool loop 轮次拆分 — 一个反直觉的关键发现

| 每 turn provider 调用数 | turns | 命中率 | 平均 prompt | write/read |
| --- | --- | --- | --- | --- |
| 1（无工具） | 61 | **30.4%** | 12,791 | **1.03** |
| 2–3 | 36 | 50.1% | 25,771 | 0.45 |
| 4–6 | 7 | 67.7% | 78,247 | 0.18 |
| 7+ | 9 | 44.3% | 61,941 | 0.85 |

**turn 内缓存工作良好，turn 间缓存基本失效。** 多轮 tool loop 的后续 call 复用同一 turn
内刚写入的前缀（纯追加），命中率随轮次上升；而单轮无工具的 turn 没有 turn 内复用可言，
其 30.4% 纯粹反映**跨 turn 前缀稳定性**，且 `write/read = 1.03` —— 每读回 1 个 token 就要
重写 1 个 token，是典型的「前缀变了、缓存重建」模式。

这是本次核实中**唯一由数据独立支撑的问题**，也是 §5.1 那项优化的直接依据。

### 4.4 分析过程中被推翻的两处推断（留档）

- **推断一**：「24k prompt 里约 11.5k 稳定、12.6k 每轮重烧」——由 47.8% 这个混合口径反推，
  在发现口径缺陷（§4.2）后**不成立，已撤回**。真实分段占比需要读 trajectory 实测，尚未做。
- **推断二**：「多轮 tool loop 是第二个独立的 miss 源」——被 §4.3 数据**推翻**，
  事实相反：tool loop 是缓存表现最好的部分。

## 5. 可落地项（按 ROI 排序）

### 5.1 【P0】tail 从滑动窗口改为锚定窗口 + 滞后前移

> ⚠️ **2026-07-30 更正：本节初稿的根因定位是错的，实施后由 final review 发现并经独立验证。**
> 详见 §11。下面保留原文以便对照，但**不要照它动手**——正确的落点见 §11.2。

**初稿的（错误）根因**：`worker.py:8685-8692` 每一轮都用当前 `through_seq` 重新计算
`db.chat_recent_genuine_turn_boundary_seq(user_id, max_turns=_CHAT_TAIL_MAX_TURNS=40,
through_seq=当前最大 seq)`。这是纯滑动窗口——每来一轮新对话，窗口整体前移一轮，
tail 起点必然变化，于是摘要段之后的一切逐轮 cache miss。
`compact_through_seq = oldest_retained_seed - 1` 又让 compaction 跟着推进，摘要段随之改写。

**错在哪**：那个边界值的唯一去向是 `compact_through_seq`（压缩目标），
**它从不参与 prompt 组装**。见 §11.1 的三步验证。

**方案**：把边界 seq 持久化成一个锚点，加滞后（hysteresis）——锚点后的轮数 ≤ 60 就复用旧
锚点（当轮纯追加，前缀逐字节不变）；超过 60 才一次性把锚点前移到 40 轮处。这样连续约 20
轮完全命中，只有锚点跳变那一轮 miss。

**改动面**：`worker.py:8685` 一处 + 一个 per-user 锚点字段（`v2_summary_frontier` state
已有承载位置，不需要新表）；`compact_through_seq` 天然跟着锚点走。

**预期收益**：单轮 turn 命中率 30.4% → 目标 80%+；`write/read` 从 1.03 降到 0.2 量级。

**代价与风险**：prompt 在 40–60 轮之间浮动，最坏比现在多约 50% 的 verbatim 内容。
考虑到 cache read 通常是 miss 价的 1/10，净收益为正，但**必须先验证**：
（a）各 provider 的缓存 TTL 是否覆盖典型对话间隔（间隔过久缓存自然过期，锚定也救不了 p25=0 那部分）；
（b）锚点跳变那一轮的 prompt 尖峰是否会在小 context window 路由上触发 §5.3 的估算问题。

### 5.2 【P1】chat lane 的常驻用户画像块

**动机**：不是省 token，而是解开 §3.3 那个「要么了解用户、要么能上网」的二选一，
以及冷启动时不必靠模型自觉调 `memory_index`。

**落地形态（不照抄提案）**：
- 生成侧复用**已有的** `capture`/`dream` lane（`extraction.py`），它们本来就在 enclave
  可读平面上跑、本来就在做记忆抽取与巩固；不要新建复盘 job。
- 物化侧跟 summary frontier 一起落库（同一套加密与 CAS 语义），chat 回合**纯读**。
- 注入位置：skills 块之后、summary 之前，**保持 user role + UNTRUSTED 标签**，
  不要提到 system role（否则 §3.5 的注入扫描变成前置必做项）。
- 硬字符上限，超限报错并附现有条目、强制在同一 job 内 consolidate（这条 Hermes 语义值得抄）。

**风险**：画像块本身若每次刷新都改写，会引入一个新的缓存失效源——必须与 §5.1 的锚点
共用同一个「翻转时刻」，不能独立刷新。

### 5.3 【P2】token 估算比率校准

`prompt_frontier.py:42` `DEFAULT_ESTIMATOR_UTF8_BYTES_PER_TOKEN = 1.0`，即「1 UTF-8 字节 =
1 token」。`worker.py:381` 的覆盖开关 `FEEDLING_V2_PROMPT_ESTIMATOR_UTF8_BYTES_PER_TOKEN`
在 prod/test/pre 三套 compose 与 CI 里**均未配置**，故生产就是 1.0。

中文 3 字节/汉字、约 0.6–1 token/汉字，即真实约 3–5 字节/token —— 系统性高估 3–5 倍。
这是刻意的保守设计（docstring：「intentionally overestimating ordinary tokenizers while
keeping the implementation provider-independent and monotonic」），且当前无害
（exhaustion = 0、tail_fallback = 0，因为用户都在大窗口模型上）。

**但这是定时炸弹**：一旦有人接 32k/64k 窗口的中转模型，会在完全没有真正超限的情况下抛
`prompt_frontier_exhausted` 或削 tail。修复成本是一个环境变量，**但不能盲调**——调松了会
真超限。正确做法是按 provider 家族校准，或对中文路由取一个仍然保守的 2.5–3.0。

### 5.4 【P3】收紧 summary frontier 上限

`DEFAULT_MAX_FRONTIER_CHARS = 48_000` 偏松，按真实分布回填一个更小的值。做在 §5.1 之后，
因为锚定窗口会改变摘要推进的节奏，分布要重新看。

### 5.5 【P3】`chat_history_search` 工具

提案 §9 里唯一我们确实没有的检索能力。先做关键词检索即可（原始历史已在 server 端、
已有分段摘要索引可挂）。它与 §5.1 是互补的：锚定窗口只保证近期 40–60 轮 verbatim，
更早的长尾需要按需下钻而不是硬压进 prompt。

## 6. 明确不做

- **不重写 `summary_frontier.py` / `compaction.py`。** 它们已经是提案想要的形态，
  且带完整性证明与 CAS 并发保护，重写是纯风险。
- **不引入 `epoch_state` / `summaries` / `md_store` / `frontier` 四张新表。**
  已有的 summary frontier state + `v2_turn_metrics` 足以承载锚点与画像，新 schema 只是把
  已有不变量重做一遍。
- **不移除 `prompt_frontier_exhausted`。** 它是预算守卫，不是 bug 症状；
  提案验收标准第 4 条在此处判断有误。
- **不做提案的 epoch 全局重构与三期路线。** 该路线基于「API-key 用户每轮就是一次无状态
  call、没有常驻进程、所有刷新都要压进异步 job」的前提；而 Runtime V2 本身就是 PG-backed
  worker 池 + 四条异步 lane（chat / maintenance / capture+dream / wake），提案建议「移到
  异步 job 里做」的事项基本已经在 job 里了。
- **不按提案 §13 开放问题 1 去区分 per-session / per-user epoch。** 我们没有 SillyTavern
  式的多角色卡会话模型，V2 是托管 worker + per-user job fence。

## 7. 附录：复现口径

```sql
-- 总览（注意 §4.2 的口径缺陷）
select lane, count(*) turns,
       round((100.0*sum(cache_read_tokens)
             /nullif(sum(cache_read_tokens)+sum(cache_miss_tokens),0))::numeric,1) hit_pct,
       round(avg(prompt_tokens)) avg_prompt,
       sum(prompt_frontier_exhaustion_count) exhaust,
       count(*) filter (where tail_fallback) tail_fb,
       round(avg(effective_tail_turns),1) avg_tail
from v2_turn_metrics
where created_at > now() - interval '7 days'
group by lane order by turns desc;

-- 按 turn 加权的分位数（比 token 加权诚实）
select round((100.0*percentile_cont(0.5) within group
        (order by cache_read_tokens::numeric/nullif(prompt_tokens,0)))::numeric,1) median,
       round((100.0*percentile_cont(0.25) within group
        (order by cache_read_tokens::numeric/nullif(prompt_tokens,0)))::numeric,1) p25,
       round((100.0*percentile_cont(0.75) within group
        (order by cache_read_tokens::numeric/nullif(prompt_tokens,0)))::numeric,1) p75
from v2_turn_metrics
where lane='chat' and created_at > now()-interval '7 days'
  and cache_reported_calls>0 and prompt_tokens>0;

-- turn 内 vs turn 间缓存分离（§4.3 的关键证据）
select case when model_calls<=1 then '1 (无工具)'
            when model_calls<=3 then '2-3'
            when model_calls<=6 then '4-6' else '7+' end bucket,
       count(*) turns,
       round((100.0*sum(cache_read_tokens)
             /nullif(sum(cache_read_tokens)+sum(cache_miss_tokens),0))::numeric,1) hit_pct,
       round((sum(cache_write_tokens)::numeric
             /nullif(sum(cache_read_tokens),0))::numeric,2) write_per_read
from v2_turn_metrics
where lane='chat' and created_at>now()-interval '7 days' and cache_reported_calls>0
group by 1 order by 1;
```

连库方式见 skill `feedling-ops-recon`（`PROD_DATABASE_URL` in `.env`）。

## 9. P0 前置验证结果（2026-07-29 执行）

§5.1 列了两项落地前必须验证的事。两项都做了，结论如下，并附带两个计划外发现。

### 9.1 验证 (a)：缓存 TTL 是否覆盖典型对话间隔 —— **通过，锚定窗口有效**

方法：按「距同一用户上一个 turn 的间隔」分桶。必须**只取 `model_calls = 1` 的 turn**，
否则 turn 内的多轮复用（§4.3）会污染信号。prod 近 30 天：

| 间隔 | turns | 完全零命中 | 命中率 | 平均 prompt |
| --- | --- | --- | --- | --- |
| < 1 min | 21 | **0** | 35.8% | 13,612 |
| 1–5 min | 60 | 2 | 22.5% | 19,996 |
| 5–15 min | 11 | 4 | 16.0% | 14,237 |
| 15–60 min | 3 | 3（全零） | 0.0% | 8,356 |
| > 1 h | 9 | 5 | 35.1% | 14,347 |

**两个独立结论：**

1. **TTL 边界确认在 5 分钟。** 5 min 以内几乎从不零命中（0/21、2/60），5–15 min 开始
   36% 零命中，15–60 min 全部零命中。这与代码一致：`provider_client.py:1169` 用的是
   `cache_control: {"type": "ephemeral"}`，**没有带 `ttl` 字段**，即 Anthropic 默认 5 分钟档。
2. **决定性证据：即使在 < 1 min 间隔、缓存必然存活的情况下，命中率也只有 35.8%。**
   缓存活着却只能命中约三分之一的前缀 —— 说明失效原因不是过期，而是**前缀本身每轮在变**。
   这正是 §5.1 锚定窗口要吃掉的那部分，TTL 不是瓶颈。

**收益边界**：5 min 内的 turn 占 81/104 ≈ **78%**，P0 对这部分有效；15 min 以上的间隔
缓存已自然过期，锚定窗口救不了。

**顺带否掉一个看似聪明的优化**：Anthropic 支持 `"ttl": "1h"` 档。但按上表分布，
5 min–1 h 区间只占 13% 的 turn，而 1h 档的缓存**写入**价是 2x（vs 5 min 档的 1.25x）——
当前 `write/read ≈ 1.03`（几乎每轮都在重写）意味着要为所有 turn 付这个 2x 去救 13%，不划算。
**等 P0 落地、重写次数降下来之后再重新评估**，那时增量成本会小得多。

### 9.2 验证 (b)：锚点跳变的 prompt 尖峰 —— **主流路由安全，但暴露一个既存故障**

先实测「不可省的固定部分」到底多大（本地跑真实模块，非估计）：

| 组成 | 字节 |
| --- | --- |
| `CHAT_SYSTEM_PROMPT` | 3,124 |
| `_RUNTIME_CONTEXT_POLICY` | 2,558 |
| 26 个工具的完整 schema（name + description + parameters） | 10,485 |
| **合计** | **16,167 B ≈ 17.8k 估算 tokens**（含结构开销） |

估算器按 1 字节 = 1 token，所以上表右列直接就是 admission tokens。各档预算
（`build_prompt_budget` 实跑）：

| 路由 | window | output reserve | safety | **input budget** |
| --- | --- | --- | --- | --- |
| unaudited 默认 | 65,536 | 4,096 | 3,277 | **58,163** |
| audited family | 128,000 | 4,096 | 6,400 | **117,504** |
| 16k 路由 | 16,384 | 4,096 | 820 | **11,468** |

**结论一（P0 安全）**：主流路由是 unaudited 65,536（128/185 turns 走 `openai_compatible`），
当前单次 call 真实 prompt 平均 17,837 tokens。锚点 40 → 60 轮约增加 8.6k 估算 tokens，
总量仍在 58,163 预算内。**P0 可以做**，但——

**结论二（P0 必须带的守卫）**：锚点前移上限**不能写死 60 轮**，必须按 `model_limit`
动态计算。因为固定不可省部分已经吃掉 unaudited 预算的 31%，小窗口路由没有余量。

**结论三（初稿写错，已修正）**：初稿称「16,384 路由的 required 部分 17.8k > 11,468、
必然失败」。**这是错的** —— 复核 `prompt_frontier.py:967-972`，`plan_provider_round`
把工具 schema 登记为 `tool_schemas_component(tools, required=False, priority=1)`，
即**工具 schema 是 optional，预算不足时被静默省略而不是抛 exhausted**
（`tool_loop.py:659` 正是在检查 `"tool_schemas" in frontier_plan.omitted_optional_components`）。
真正 `required` 的只有 `message_context`（system + summary + tail + transcript）≈ 5.7k，
在 11,468 预算之内。

**但 16k 路由确实异常**：9 个用户配了 16,384 且已有 V2 fence，其中 3 个真在发消息，
近 30 天 chat turn 失败率约 80%，远高于其他档（128k 档 22.5%、131k 档 35%）。
按 `status` 拆分，失败的绝大多数是 `turn_failed:providererror`（48 次），
`prompt_frontier_exhausted` 只有 2 次 —— **所以主因不是预算守卫**，真因未知。

⚠️ 两点保留：该失败率的分母来自 join `model_api_routes` 的查询，多路由用户会被放大，
需用去重口径复核；且「工具 schema 被静默省略」本身是一个值得单独确认的行为
——小窗口路由上模型可能根本拿不到工具，却不会有任何显式信号。

**这两件事应作为独立待查项**，不进本文的落地清单。

### 9.3 计划外发现 A：`prompt_frontier_exhaustion_count` 是个不可信指标

`worker.py:9937/9997/7163/7209` 确实在终态失败路径上调了
`tm.record_prompt_frontier_exhaustion()`，但库里**所有** exhausted 行的该字段都是 0。
即计数器写了却没落库。

**后果**：不能用这个字段判断「有没有触发过」。正确口径是查 `status`。按 status 统计，
prod 近 90 天：

| status | 次数 | 首/末次 |
| --- | --- | --- |
| `turn_failed:prompt_frontier_exhausted` | 2 | 2026-07-24 |
| `turn_failed:v2_summary_frontier_exhausted` | 4 | **2026-07-29（当天）** |
| `compaction_failed:v2_summary_frontier_exhausted` | 3 | **2026-07-29（当天）** |

这条推翻了本文 §3.2 初稿里「一次都没触发过」的说法，也推翻了分析过程中一度报出的
「6 次 / 32 次」——后者是 join `model_api_routes` 造成的行放大，**真实是 2 次和 7 次**。

### 9.4 计划外发现 B：`v2_summary_frontier_exhausted` 今天首次出现，命中大历史老用户

**90 天内只在 2026-07-29 当天出现，共 7 次，涉及 3 个用户**，其中 4 次是
`turn_failed`（用户直接收不到回复），3 次是 `compaction_failed`（后台压缩失败）。

命中的正是外部提案假想的那个场景 —— 几千条历史的老用户：

| 用户 | 段总数 | canonical cover 段数 | 覆盖源消息 |
| --- | --- | --- | --- |
| `usr_7f30d63f` | 118（max level 2） | 14 | 8,718 |
| `usr_81a0645d` | 77（max level 2） | 5 | 8,659 |
| `usr_453c4b85` | 15（max level 1） | 7 | 1,080 |

**但根因不是段数或字符超限**：cover 只有 5–14 段（上限 24），总量约 6 KB 明文
（上限 48,000 chars）。真正的异常形态是 `usr_7f30d63f` 的 cover 里有
**8 个 `source_message_count = 1` 的 level-0 段** —— compaction 在**逐条**推进，
而非按 50–100 条批量。正常段应覆盖一批消息（同一 cover 里另有覆盖 2,648 条和 324 条的段）。

逐条推进的可能成因是 `serve_worker.py:960-1030` 的「oldest **contiguous** batch」语义：
批次里若有无法处理的行（解密失败、图片等），contiguous 会被切断成单条。这条尚未验证。

时间线：prod 当前 pin `a53df64` 部署于 2026-07-28 02:19 UTC，首次 exhausted 在 07-29，
相隔约 26 小时。**时间吻合但不足以断言因果** —— 也可能是这两个用户刚好积累到临界点。

**这件事应当独立立案，优先级高于 P0**：它是用户可见的失败（收不到回复），且正在发生；
而 P0 是成本与体验优化。两者都触及摘要/上下文管线，改动次序需要先想清楚，
否则 P0 的锚定窗口会让 tail 更长、摘要推进更慢，可能加剧 9.4。

## 10. 关键代码位置索引

| 关注点 | 位置 |
| --- | --- |
| 预算会计与 `PromptFrontierExhausted` | `backend/model_api_runtime/v2/prompt_frontier.py:37-200` |
| 不可变分段摘要 + rollup | `backend/model_api_runtime/v2/summary_frontier.py` 全文 |
| prompt 装配布局（冻结顺序） | `backend/model_api_runtime/v2/context.py:420-502` |
| **tail 滑动窗口根因** | `backend/model_api_runtime/v2/worker.py:8683-8692` |
| tail 深度二分适配 | `backend/model_api_runtime/v2/worker.py:3061-3136` |
| compaction 异步入队（非热路径） | `backend/model_api_runtime/v2/worker.py:10009-10019` |
| `WORKING.md` 故意不注入 | `backend/model_api_runtime/v2/worker.py:1222-1225`、`serve_worker.py:129-149` |
| 私有读工具锁出站 | `backend/model_api_runtime/v2/worker.py:559-569` |
| memory context 仅接 capture/dream | `backend/model_api_runtime/v2/serve_worker.py:1739-1798` |
| per-user 缓存亲和 key | `backend/model_api_runtime/v2/serve_worker.py:702-726` |
| usage → cache token 归一化 | `backend/provider_client.py:722-792` |
| skills 块 byte-stable 排序 | `backend/workspace/prompt.py:1-6, 37-82` |
| 工具全集 | `backend/capabilities/tool_schema.py` |

## 11. 更正：§5.1 的根因定位是错的（2026-07-30）

按 §5.1 实施的分支（4 个任务、7188 passed）在**全分支 final review** 时被判定为
「接在了一个不影响 prompt 字节的值上」。该判定经独立验证成立，实施成果已回退
（见 §11.3）。本节记录正确的根因，供下一次动手时使用。

### 11.1 三步验证（可在本地逐条复现）

1. **锚点/边界值的唯一出口不进 prompt。**
   `grep -n "oldest_retained_seed" backend/model_api_runtime/v2/worker.py` 只有三处使用，
   全部落在 `compact_through_seq = oldest_retained_seed - 1`（chat lane 在 8843-8847，
   wake lane 在 6273-6280）。`compact_through_seq` 只流向压缩/覆盖检查，
   **不进 `build_turn_messages`**。
2. **prompt 的 tail 走的是另一条路。**
   chat lane 的 `summary / tail / optional_tail_turns` 来自
   `_read_seq_adaptive_prompt_context(..., target_turns=_CHAT_TAIL_MAX_TURNS, ...)`
   （`worker.py:8881-8885`），与那个边界值无关。
3. **真正逐轮前移的滑动窗口在 optional 选择逻辑里。**
   `worker.py:3102-3107`：
   ```python
   required_turn_count = sum(1 for row in required_tail if _is_genuine_user_seed(row))
   optional_limit = max(0, target_turns - required_turn_count)
   eligible_optional = optional_turns[-optional_limit:] if optional_limit else []
   ```
   每来一轮新对话 → `required_turn_count` +1 → `optional_limit` −1 →
   `optional_turns[-optional_limit:]` 的窗口从**头部**收缩一轮 → 最老的一轮被挤掉。
   而 optional 展开后紧跟在摘要块之后（`context.py:453` 摘要在 tail 之前），
   所以摘要之后的第一条消息每轮都变，前缀从那里断裂。

这与 §4.3 实测的「单轮 turn `write/read = 1.03`」完全吻合：每读回 1 个 token 就要重写 1 个。

### 11.2 正确的落点（尚未实施）

锚点这套机制本身没问题，错的只是接线位置。要真正生效，锚点必须约束 **optional 窗口的选择**，
而不是 `compact_through_seq`：把 `eligible_optional` 的起点钉在锚点上（或让
`read_recent_turns` 的 seed 起点取自锚点），而不是每轮由
`target_turns - required_turn_count` 这个逐轮变化的差值推导。

**连带约束**：`_TAIL_HARD_CAP`（60 **行**，注意是行不是轮）必须与滞后上限一起调大，
否则「滞后区多留 20 轮」会直接撞上行数上限，变成「多丢 20 轮」。

**必须补的验收测试（原 plan 缺失，是这次错误没被更早发现的直接原因）**：
断言同一 epoch 内连续两回合构建出的 prompt 前缀**逐字节相同**。
原 plan 的测试只断言了「锚点保持/前移」和「边界查询被跳过」，这些在接错线的情况下**照样全绿**。

### 11.3 已落地与已回退

保留（与根因无关、独立有效）：

| commit | 内容 |
| --- | --- |
| `1c7812e9` | `TurnMetrics.flush` 按终态 status 兜底 exhaustion 计数（修 §9.3 的观测缺陷） |
| `c6e1a491` | `tail_anchor.py` 纯滞后策略模块（11 单测，逻辑正确，可直接复用） |
| `6f8add25` | `v2_chat_tail_anchor` 表 + RDS/TEE 双链迁移 + 单调 upsert（基础设施，可直接复用） |

已回退并归档到分支 `archive/v2-tail-anchor-wrong-wiring`：

| commit | 内容 |
| --- | --- |
| `a12626aa` | 把锚点接到 `compact_through_seq`（错误落点） |
| `98d56a11` | 上述接线的端到端测试 |

### 11.4 另一个待查项（final review 提出，未独立验证）

对 maintenance lane 掉队的用户（§9.4 里 `usr_7f30d63f` 那批），钉住 `compact_through_seq`
会让 watermark 推进变慢，coverage hole 从 ~20 行涨到 ~60 行；而 hole 的行数被写进摘要文本
（`worker.py` 的 `_with_coverage_hole_notice`）且逐轮变化，**反而会打掉从摘要开始的缓存**。
若将来重新接线，需要先确认这条路径。

### 11.5 方法论教训

prod 数据（命中率 30.4%、`write/read=1.03`）足以证明「前缀每轮在变」，
但**不能证明是哪一段代码在变**。初稿把数据的支持力度当成了定位的支持力度：
看到 `chat_recent_genuine_turn_boundary_seq(max_turns=40)` 就认定它是 tail 的来源，
没有追它的返回值去向。**下次定位这类问题，必须先 grep 出候选变量的全部使用点、
确认它真的流进了目标产物，再谈根因。**
