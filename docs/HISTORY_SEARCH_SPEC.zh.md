# History Search Spec — 模型侧历史聊天记录检索（V2 + V1）

状态：V2 三批已实现（待修 code review 六项）；V1 待做（§12）。
基线 `origin/test`（4df93df7）。
背景调研与决策记录见工作区（io 根目录）`FEATURE_LOG.md`「历史聊天检索」一节
（该文件不在本仓库内）。

## 1. 问题与目标

V2 每回合上下文 = 分层滚动摘要 + 最近 tail（`_TAIL_HARD_CAP=60`）。tail 之外的
原始消息模型完全不可及；摘要有损，"上个月我们聊的那家餐厅叫什么"这类需要原话
的问题答不出。

目标：给模型两个按需检索工具，在受控预算内查回任意时期的聊天原文。
上下文组装、compaction、正常聊天路径一概不动。

心智模型：`memory_*`（提炼结论）miss 时，降级到 `history_*`（原始底账）。

## 2. 范围与非目标

**V1 也要做**（2026-08-07 hx 拍板）：VPS 自托管用户跑的就是 V1 形态，只做 V2
等于自托管用户永远拿不到。拆批上 V2 先行，V1 见 §12，**不许做完 V2 就算完**。

非目标：

- 向量/语义检索不做（embedding 是加密架构下新的明文泄漏面）。
- 明文全文索引不做（出不了加密边界）。加密 lexical sidecar 是**第二阶段**，
  见 §13。
- `history_fetch` 分页不做（MVP 单窗口，见 §3.2）。
- **检索结果不反哺记忆**（hx 拍板）：搜到的原话不自动写成记忆卡。理由：写记忆
  只有 capture/dream 两条既定管线，自动写入会污染记忆库；先看真实使用频率。
- iOS 端改动无。

## 3. 工具接口

照 `capabilities/` 现有 memory 系列模式注册（registry + tool_schema + 薄 facade）。

### 3.1 `history_search`

```
history_search(query?: string, start?: string, end?: string,
               cursor?: string, limit?: int)
```

输入契约：

- `query` 与 (`start`|`end`) 至少给一个；给了 `cursor` 则**只传 cursor**
  （其余参数必须省略或与 cursor 内记录严格一致，否则 `cursor_mismatch`）。
- `query`：NFKC + casefold + 空白归一后非空，≤128 字符（进 cursor payload 的
  最坏情况必须稳在 cursor 1024 限内）；匹配为归一化子串匹配（中文天然支持）。
- `start` inclusive / `end` exclusive；RFC3339，带 offset 的转 UTC 处理；
  `start < end` 否则稳定报错。相对时间由模型自己换算。
- `cursor` ≤1024 字符。`limit` 默认 3，钳 [1,5]。

返回：

```
{ matches: [ { message_id, ts, role, snippet, content_truncated } ],
  complete: bool, next_cursor?: string,
  scanned_count: int, unavailable_count: int, coverage_gap: bool }
```

- 结果顺序 = 扫描优先级顺序（非时间序），工具描述写明。
- `snippet` 围绕命中位置截取 ≤240 字符。
- 语义三态（工具描述原文）：complete=true 扫完了；complete=false 且有
  next_cursor → 还有可扫的，带 cursor 原样再调；complete=false 无 cursor 且
  coverage_gap=true → 有历史区间原文已不存在（legacy retention 清理），
  结论不确定，不得回答"历史里没有"。
- `unavailable_count`：候选中解密失败/不可读的行数（不算命中也不算不存在）。

### 3.2 `history_fetch`

```
history_fetch(message_id: string, before?: int, after?: int)
```

- `before` 默认 **15**、`after` 默认 **4**，各钳 [0,15]（请求行数上限 31）。
  **刻意不对称**：这个产品问题（"那家餐厅叫什么"）几乎总是往前找线索，
  对称分配等于把一半预算浪费在后文。返回结构（anchor 只出现一次，无分页）：

```
{ anchor: message, before: [旧→新], after: [旧→新],
  unavailable_count, omitted_before: int, omitted_after: int }
```

- 邻居按该用户 seq 序取（客户端时间戳不可靠，不做游标）。
- 锚点不可见或不存在统一返回 `not_found_or_not_visible`（不区分，
  不靠 message_id 难猜当权限控制）。
- **31 是请求上限，不保证 31 条都进入最终 payload**：31 条即使正文全空，
  JSON 骨架也约 3240 字符；平均正文 40 字时已逼近 4500，80 字时必然要删减。
  实际返回条数由预算决定，删了多少必须用 `omitted_before/omitted_after` 如实报。
- 限制单位是**完整序列化 payload**（≤**4500** 字符），超限**按此顺序**结构化缩减
  （绝不序列化后切串）：
  1. 先保证所有消息的结构 + 短摘录都在（让模型看得到"这里有哪些消息"）；
  2. 再优先给近邻补全正文；
  3. 最后才丢最远的消息，并计入 `omitted_*`。
  ＊顺序不能反：先砍最远邻居会恰好砍掉这次放宽窗口想找回的前文线索。

- **完整修改清单（缺一项就会被静默砍回去）**：tool_schema 参数范围与描述、
  facade 默认/上限、readside 默认/上限、**enclave `_FETCH_ROWS_HARD_MAX`
  （现为 16，装不下 31）**、executor per-tool cap、tool_loop 水位策略、协议测试。
  ＊修订理由（hx 2026-08-07）：原值前后各 2（≤9 条 /1600 字符）是照着 2000 字符
  硬顶倒推的技术约束，不是产品需求——真实场景里"那家餐厅"的来龙去脉常常在
  七八轮之前，各 2 条经常正好卡在关键句之外。

### 3.3 结果预算：共享策略，不是各层各写一份

两道独立截断（executor 单结果 2000；tool_loop 单结果 2000 + 同批 8000 水位分摊、
且是序列化后切字符串）意味着**只在 executor 加 per-tool cap 无效**——tool_loop
仍会砍回 2000，最坏批形下砍到约 1000，JSON 必坏。

因此定义一份**共享的可信预算策略**，executor 与 tool_loop 同读：

```
history_fetch:  result_cap=4500  atomic_json=true  extra_batch_budget=2500
history_search: result_cap=1800  atomic_json=true
```

- `atomic_json=true` 的结果，水位分配时**先整额预留**，剩余额度再分给兄弟结果；
  任何情况下不对它做字符串切割。
- 同批总预算在含 history 结果时临时抬到 `8000 + 2500 = 10500`——否则 fetch 占
  4500 后，另外 7 个结果每个只剩约 500，是实打实的产品退化。
- facade 只负责"序列化前结构化缩减到 result_cap"并通过可信
  `ToolResult.metadata` 标记类型；**它不能自己决定预算**，授权在共享策略。
- 协议测试必须用真实最坏形态：`history_fetch 顶满 4500 + 7 个各 ≥2000 的兄弟`，
  逐个 `json.loads`、总量 ≤10500。**不许用短 "ok" 结果凑数释放水位**。

## 4. 执行算法（search）

**原文是检索真相，摘要叶子只是扫描优先级提示**。

首调用固定快照并编入 cursor：`snapshot_through_seq`（当时 chat_max_seq）、
`summary_watermark_seq`、runtime generation、归一化 query/start/end、
当前扫描 phase + phase 内 resume 位置、cursor 版本 + 过期时间。
cursor 由 CVM 信任域内的部署级 secret 做 HMAC 认证（实现：worker 侧签名/校验，
key = HMAC(FEEDLING_RUNTIME_TOKEN_SECRET, 独立 domain 标签)；worker 与 enclave
同域同 secret，威胁对象是模型/中间层篡改，不变）；**用户身份永远来自认证
上下文，不来自 cursor**；user 不匹配即拒。

扫描顺序按请求形态分流（不是固定串行）：

```
只有时间范围（无 query）
  → 跳过摘要提示，范围内 recent-first 直接扫原文

有 query
  → ① 未压缩区间 (watermark, snapshot] 先扫【至多一批】
    ② 摘要叶子命中段（解密 level-0 叶子、归一化子串匹配，命中段优先扫原文）
    ③ 预算余量内继续扫未命中范围，recent-first
```

（① 限一批是防 compaction backlog 把预算全吃掉、永远到不了高价值的 ②。）

每批密文批量送 enclave：enclave 内解密 + 归一化匹配，只回命中项（复用 memory
readside 批量形态，不用逐条解密的 tail reader）。

**cursor 正确性硬规则**：凑够 limit 提前停止时，cursor 必须落在**最后一条实际
检查过的候选**上——一批内命中 20 条只返回 3 条时，其余 17 条必须能被下一页
拿到，不许跳到批末尾。

legacy_opaque 摘要节点（无精确 source witness）不参与 ② 的范围推断，其覆盖区
走 ③；**其覆盖的原文可能已被旧 retention 清理**（代码注释明示），raw 行缺失时
置 `coverage_gap=true`。摘要树读取需新增 level-0 全叶子快照查询（现有 canonical
cover 查询会略过被 checkpoint 覆盖的子节点，不可复用）。

## 5. 预算与截断（防 enclave 打满、防非法 JSON）

预算状态放在 chat turn closure，**跨 provider round 累计**；history 调用之间
串行锁。所有值 env 可调，默认保守、上线看指标再调（不做压测前置）：

| 闸 | 默认 |
|---|---|
| raw 单批 | 128 条 且 密文 ≤512 KiB |
| 叶子提示扫描（独立记账） | 单调用 ≤64 叶 或 ≤256 KiB，先到者停 |
| 单调用 raw 扫描 | 512 条 或 2.5s（**enclave 内部逐批检查 deadline**，不是外层 to_thread 超时） |
| 单回合累计（两工具合计） | raw 1500 条 或 8s |
| 每个 provider round | **最多 1 个 history 调用**，多余的返回短错误 |

超预算 = 正常返回 `complete=false + next_cursor`，绝不伪装成"没找到"。

截断：见 §3.3 的共享预算策略（executor 与 tool_loop 同读、atomic_json 整额
预留、含 history 时同批预算抬到 10500）。要点重申：**只在 executor 加 per-tool
cap 无效**，tool_loop 的水位分摊会把它砍回去。

## 6. 信任边界与开关

- 两工具进 `_PRIVATE_READ_TOOLS`：读历史后下一轮移除 web/MCP/task 出站。
- **双层 gate**：
  - Offer gate：仅 chat lane 且开关开启时进工具目录；wake/subagent/heartbeat
    一律不进。
  - Dispatch gate：executor/capability 入口按 lane + 开关再拒一次
    （`tool_not_allowed`）——目录隐藏挡不住直接调用/错误接线。
- kill switch：`FEEDLING_V2_HISTORY_TOOLS_ENABLED`，**默认 ON**（回滚闸）。
  关掉 = 目录消失 + dispatch 拒绝，正常路径逐字节不变。
  ＊Codex 建议第一版默认 OFF 分阶段开——被否决：违反工作区「开关默认 ON」
  纪律（有过默认 OFF 上线数天没生效的事故）。版本错位窗口靠同镜像同 commit
  部署 + 明确报错兜底：enclave 无对应 route 时工具返回 capability-unavailable
  错误，不静默空结果。
- 只读 live `chat_messages`；**绝不读 clear 后的 `chat_message_archive`**。

## 6.5 🔴 指标/日志本身是泄漏面（调研 2026-08-07 补，实现前必读）

MongoDB Queryable Encryption 被 USENIX'23 打出实用攻击，**根因不是密码学，是
它运行必需的日志向快照攻击者暴露了查询与数据的统计信息**，配合辅助数据集即可
同时恢复查询和数据。TEE 方案的翻车点通常也在这里，不在加密本身。

因此本功能的可观测性必须满足：

- **绝不记录 query 明文、snippet、message 正文**（含错误日志、慢查询、trace）。
- 指标只保留 content-free 聚合量：调用次数、扫描行数、耗时、零结果率、
  预算耗尽次数。**命中数按区间取整**（如 0 / 1-3 / 4+），不落精确 hit 数——
  精确命中数配合已知语料可做频率推断。
- **不按 user_id 维度长期留存**上述指标；聚合到部署级或短 TTL。
- enclave 内部错误不得把待匹配文本回传到 enclave 外的日志。
- 现有 activity metadata 已只投影 content-free 的 scanned_rows（符合要求），
  新增任何指标前对照本节自查。
- **例外**：索引回填进度（§13）是必要的 durable 运维状态，可按用户持久化。
  但调用量、命中分布、query 相关量仍不得按 user_id 长期留存；Bloom 饱和度、
  不同 bigram 数这类也不许变成长寿命的用户画像指标。

## 7. 可见性 contract（search / fetch 完全同一套规则）

- 角色白名单 user/human + openclaw/assistant/agent（assistant 侧三个历史 role
  都要含，V1 时代写 'agent'）+ 附件 caption。
- **在 SQL LIMIT 和预算计数之前**排除：verify 流量、resident maintenance、
  内部 system 消息、图片/文件二进制正文（R2 不读）。
- 不可解密行计 `unavailable_count`。

## 8. 测试计划（实现的阻塞项）

1. 摘要故意省略实体词，search 经 raw fallback 找到。
2. 无摘要短会话 / legacy_opaque / compaction backlog / 并发 compaction 不重不漏
   （快照含 watermark，翻页期间 compaction 推进不重排已扫区间）。
3. cursor：篡改/过期/跨用户/条件错配被拒；密集命中（一批 20 中只回 3）分页不丢；
   同 timestamp 大量消息 seq 游标不重不漏。
4. 截断：executor + tool_loop 双层 cap 后 JSON 完整；8 调用混合批协议测试。
5. 出站隔离：history 读后下一轮 web/MCP/task 不可用。
6. 双层 gate 矩阵：wake 直接调用、未 offer 直接 dispatch、开关关闭后 dispatch，
   全部拒绝。
7. 预算：同回合跨 round 累计；千级叶子时提示扫描有界；单个超大密文；
   enclave deadline 生效。
8. 可见性：默认排除项、隐藏锚点 `not_found_or_not_visible`、clear 竞态
   （generation 失效）、archive 表不可见。
9. 失败路径：enclave route 缺失（版本错位）明确报错、401/403/5xx。
10. 真模型 e2e：搜索 → fetch → 答出旧原话（prompt 行为单测抓不到，红线）。

新测试文件按 `tests/conftest.py` `_PURE_UNIT` 白名单规则处理，加完
`--collect-only` 核对。

## 9. 文档同步（仓库要求）

模型能力与私密数据出站边界变化 → 同 PR 更新 `docs-site/content/docs/` 的
architecture / 相关 workflow / self-hosting trust model + changelog `Unreleased`。
enclave 内部 route 不进公共 OpenAPI。

## 10. 实现拆批（每批独立可验收）

1. **扫描内核 + cursor codec**（纯逻辑，不注册工具）：planner、cursor
   HMAC codec、DB metadata helpers、level-0 叶子快照查询。验收 = §8.2/8.3。
2. **enclave readside route**：bounded 叶子提示 + raw 批量 decrypt/search/fetch。
   验收 = §8.7/8.8/8.9 的 enclave 侧。
3. **runtime 接线**（同批落地，不可拆）：registry/schema/facade、双层 gate、
   `_PRIVATE_READ_TOOLS`、回合预算 + 串行锁、结果结构化缩减。验收 = §8.4/8.5/8.6。
4. **test 环境验证**：全量相关测试 + `--collect-only` + 文档同步 + 真模型 e2e
   （§8.10）+ 观察扫描量/耗时/零结果率指标。
5. **V1 / 自托管线**（§12）——V2 上线验证后做，不许省略。

## 11. 放弃的替代方案

- 摘要命中当硬过滤（一轮否决：确定性漏召回）→ 改优先级提示。
- fetch 用边缘消息当锚点翻页（二轮否决：anchor 重复/取不回目标正文的矛盾）
  → MVP 单窗口结构化返回。
- 固定 a→b→c 扫描顺序（二轮否决：backlog 吃光预算）→ 按请求形态分流。
- 明文 FTS5 索引 / 全量解密扫描 / 第一版向量检索：见 §2。
- 单工具多形态（Hermes 风格）：与现有 memory 两工具心智不一致。
- 第一版默认 OFF 分阶段开（Codex 二轮建议）：违反工作区开关纪律，见 §6。
- fetch 前后各 8 的对称窗口（一度写进 spec）：改为 before=15/after=4——
  这个产品问题几乎总是往前找线索，对称等于浪费一半预算（Codex 三轮）。
- 整用户 64 KB Bloom 总表（一度设想）：会饱和、且覆盖不全时制造假阴性，见 §13.3。
- "enclave 无持久化所以 B1 不可行"（我一度的判断）：**事实错误**，部署有共享
  `/data`；B1 真正的成本是多进程单写者与恢复协议，见 §13.1。

## 12. V1 / 自托管线（批次 5，V2 之后）

V1 不 spawn worker、走 CLI + `io_cli` verb，所以接线形态不同、核心逻辑共用：

- **共用**：`history_search.py`（归一化/planner/cursor）与 enclave 三路由**原样
  复用**，它们本就框架中立、不 import 任何 V2 模块。
- **V1 侧新增**：`io_cli` 两个 verb（`history-search` / `history-fetch`）+
  `agent_runtime/agent_tools_prompt.md` 的能力说明（触发时机文案与 V2 tool
  description 保持同一套三态语义）。
- **预算/闸的差异**：V1 没有 provider round 概念，回合预算改按单次 CLI 会话累计；
  出站隔离在 V1 上没有等价机制（CLI 自带工具不归我们管），因此 V1 侧**默认
  只给 search/fetch，不做跨轮出站移除**——这条差异必须在交付说明里点明。
- **CLI 自带工具冲突检查**：确认 claude/codex CLI 无同名/同功能原生工具
  （历史检索不是 CLI 自带能力，预期无冲突，但接线时实测一遍）。
- VPS 自托管随 consumer 自更新生效，hosted V1 随 runner 镜像重建生效。

## 13. 第二阶段：加密 lexical sidecar（不在本期，接口现在就留）

**动机**：现在"先扫哪段"对**摘要文本**匹配，而摘要按 `_SEGMENT_SYSTEM_PROMPT`
只保留"决定/事实/偏好/承诺/未完成事项/重要语境"——具体实体（店名/书名/人名）
系统性丢失，搜它必然 miss、只能顺序扫兜底。索引对**原文**建，是"精准定位"
而不是"猜哪段可能有"。**这是修正确性缺陷，不只是提速。**

### 13.1 方案选型（2026-08-07 定，Codex plan_review 同意）

| 方案 | 结论 | 理由 |
|---|---|---|
| **B2 叶级 Bloom sidecar** | ✅ **当前选它** | 贴合现有不可变摘要叶 + 精确子串协议，改动边界最小，量级几 MB |
| TEE Postgres bigram GIN | ⏳ 中长期最佳候选，见 §13.5 | 成熟度最高，但前置条件未满足 |
| B1 enclave 内 Tantivy 全文索引 | ❌ 暂不选 | 能力最强但当前不需要 BM25/模糊；多进程单写者、崩溃恢复、密钥轮换成本最高 |

**B1 被否的准确理由**（修正早前"enclave 无持久化所以不可行"的说法——**那句是错的**）：
enclave 部署实际**有**共享挂载的 `/data`（`docker-compose.phala.yaml`），所以
B1 技术上可行、不必每次从 PG 整索载入。真正的成本是：两个 enclave service ×
每个约 4 个 gunicorn 进程共享同一索引目录，而 Tantivy 同一索引只允许一个
`IndexWriter` → 必须自建单写者拓扑 + commit/reload/崩溃恢复/clear/索引版本/
密钥轮换全套协议；且 BM25/模糊会改变现有"精确子串 + seq/cursor"的产品语义。
**只有在实测确认需要排名/模糊/短语，或 B2 的 I/O 不达标时才做 B1 spike**，
spike 必须先验证：单写者拓扑、50k/500k 消息冷启动、索引大小、commit/reload、
密钥轮换。

### 13.2 B2 参数（数字已修正，早前的估算是错的）

**修正 1 —— 叶大小**：`_COMPACTION_BATCH` 代码默认 200，但 test/pre/prod 三个
compose 都覆盖成 **50**。所以 5 万条历史约 **1000 个叶**（不是 250 个）。
**读部署配置，别读代码默认值。**

**修正 2 —— 误报率必须按"单 bigram 查询"定**：早前按 p=5% 估算，但那是双
bigram（"新荣记"→"新荣"+"荣记"，两者都要命中 → 0.25%）的算法。**最常见的
查询是两个字**（"餐厅"/"电影"/"生日"），只有 **1 个** bigram，误报率就是 p 本身：
p=5% 时 1000 个叶平均 **50 个误报叶**，根本不是"筛到 3 个"。

修正后的取值：

| 目标 | p | k | 每叶（n=8000 唯一 bigram 时） |
|---|---|---|---|
| 可用 | 1% | 7 | ≈9.36 KiB |
| 接近"1000 筛到个位数" | 0.5% | 8 | ≈10.77 KiB |

- **不要固定每叶大小**：按该叶实际唯一 bigram 数算 `m`；50 条的叶若只有约
  2000 个唯一 bigram，p=1% 时约 2.34 KiB/叶，总量约 2.4 MB。
  **n=8000 是未实测的假设值，实现前先统计真实分布。**
- 加密头部存 `index_version / normalization_version / m / k / n`，缺一不可
  （归一化规则变更 = 索引全体失效，必须能识别）。
- 哈希：**一次 HMAC + double hashing 推导 k 个位置**，不要每个位置各算一次 HMAC。
- **Bloom 命中后必须做原文精确子串复核**；归一化版本不一致 / 索引缺失 /
  解密失败 → **一律回落顺序扫描，绝不返回"无结果"**。
- 单字查询没有 bigram → 用不上索引，回落顺序扫；工具描述引导 ≥2 字关键词。

#### 分词规则：中英混排必须两套（hx 2026-08-07 提出，产品有英文用户）

**按字符类型分段处理，结果并入同一个位图**：

```
CJK 段（中日韩）→ 切字符 bigram
  "新荣记的椒盐皮皮虾" → 新荣 荣记 记的 的椒 椒盐 盐皮 皮皮 皮虾

ASCII/拉丁段 → 按空白与标点切词 + casefold
  "the spicy shrimp at Xinrongji" → the spicy shrimp at xinrongji

混排 → 各按各的规则切，token 合并
  "在 Xinrongji 吃的椒盐皮皮虾" → xinrongji + 在X(跨类边界不成对) + 椒盐 盐皮 …
```

- 跨字符类边界**不生成 bigram**（"在X" 这种没有检索价值，只会污染位图）。
- 查询侧用**完全同一套**规则处理，字节级一致；规则变更 = `normalization_version`
  递增 = 存量索引全体失效，必须能识别并回落。
- 英文不做词干化/复数归一（`restaurant` 不匹配 `restaurants`）：位图只是粗筛，
  候选段仍要在原文做子串精确复核，而子串匹配天然覆盖 `restaurants`。
  **影响仅限于"这段会不会被筛掉"**，实测确认影响面后再决定是否补。
- 中文不引 jieba（分词错误会永久漏召回）；**不要 trigram**（中文多二字词，
  三元组让两字查询用不上索引）。

#### 参数直觉（写给非后端读者）

一个词算出的**不是一个编号，是 k 个**；判定"可能有"要求这 k 个编号**全部**
命中。k 越大越像"更长的密码"，别的词恰好全占同样一组编号的概率越低：

```
p=5%  → k=4，编号空间约 5 万   （4 位密码）
p=1%  → k=7，编号空间约 7.7 万 （7 位密码）
```

### 13.3 分层：先实测再加，不做固定 64 KB 总表

早前设想的"整用户 64 KB 总表先短路零结果查询"——**否决**：64 KiB 在 p=5% 下
只能容纳约 8.4 万个唯一 bigram，5 万条消息很可能超出后快速饱和；更严重的是，
总表只有在覆盖了当前快照 + tail + 新叶 + 全部回填之后，"不命中"才等价于
"整个历史没有"，否则制造**假阴性**（最不能接受的错误）。

正确顺序：① 先实现自适应叶级 Bloom，实测总 bitmap 读取/解密成本；② 确认
1000 个小 blob 的读取次数确实是瓶颈时，再做**每 16~32 叶一组**的 group filter；
③ 最多两层；④ **每层都带 `indexed_through_seq + generation + index_version`
覆盖 witness**，覆盖不完整就继续查下一层或顺序扫。

### 13.4 存量回填（hx 拍板必做）

只覆盖上线后的新消息 = 只覆盖最不需要检索的部分（新消息还在 tail 里）。

**🔴 不能塞进 maintenance lane**（已核实代码）：同用户同 lane 的 active job 会
single-flight/coalesce，且 worker 见到 `lane == "maintenance"` 就**无条件**执行
`_run_compaction`、不看 reason —— 回填要么被合并吞掉，要么 claim 成功后仍去跑
压缩。必须新增独立 lane：

- 新 lane `history_index_backfill`，优先级 **1**（低于 maintenance=10），独立 handler。
- 全局最多一个回填批次持有 enclave semaphore（默认仅 2 个并发许可），**短批次
  即释放**，不长期占用。
- **进度 witness 不能只是一个整数 cursor**，必须是每叶一条 durable sidecar：
  `(user_id, segment_id, index_version)` 唯一键 + `runtime/clear_generation`
  + `source_start_seq / source_end_seq / source_message_count` + `encrypted_index`。
  "完成"= sidecar 存在且 source witness 与当前不可变叶一致；用户级
  `next_segment_id` 只能当调度提示。
- 在线 compaction 最好把 summary leaf 与 index sidecar 放进**同一个 CAS 事务**；
  做不到时宁可留下"未索引叶"让回填补，**绝不能先标完成**。
- clear/delete 经 FK cascade + generation fence 清掉旧 sidecar。
- 回填进度是必要的 durable 运维状态，可按用户存（§6.5 已开此例外）。

### 13.5 中长期候选：TEE Postgres 明文影子库 + bigram GIN

本仓已有一套**跑在 TEE CVM 里的 PostgreSQL 明文影子库**
（`docs/TEE_POSTGRES_SHADOW_PROVISIONING.md`、`backend/tee_replicator/`），
`chat_messages.doc` 已解密复制过去。在那里可以：对归一化正文生成字符 bigram
数组 → 内建 GIN `array_ops` 索引 → 查询用数组 `@>` 要求全部 bigram 存在 →
再对候选原文精确复核。并发、WAL、崩溃恢复、索引构建全由 PG 负责，比自维护
Bloom schema 成熟得多。（不要用 `pg_trgm`：固定 trigram 服务不好两字中文。）

**为什么现在不选**：该影子库当前是**异步、best-effort、fail-open**的旁路
（文档明示"失败只计数，绝不拖垮主路径"），且 `sslmode=require` 只加密不验证
服务端身份。影子库落后或有待解密行时，**不能用它证明"没有结果"**。
前置条件：可信连接（验证服务端身份）+ 复制覆盖 witness（能证明"已同步到某
seq"）。满足后它优于 B2，届时重新评估。

### 13.6 现在就要留的接口

§4 扫描顺序里"决定优先扫哪段"是一个**可替换的过滤器**（今天=摘要匹配，
将来=Bloom 位图 / GIN 命中），实现上保持这一步与"扫描/解密"解耦，换过滤器
不动其余代码。存量老数据无索引 → 回落摘要路径，混跑不冲突。

### 13.7 启动条件与性能标准

**启动条件**（满足其一再做，不提前）：真实用户历史普遍过 2 万条；或指标显示
单次搜索平均翻页 > 2 轮。

**性能标准（hx 2026-08-07 定，直接影响参数选型）**：本工具**只在模型明确要查
旧历史时触发**，不在常规聊天路径上，因此**"几秒内出结果"即可接受，不追毫秒级**。
选参数时优先省空间/省 I/O，不要为极限速度把位图无限放大。

### 13.7.1 🔴 实测前置（四项，未测完不许按估算值实现）

**本节所有数字目前都是纸上估算**（n=8000 是拍的、6 MB 是推的、"筛到 N 段"是
概率算的）。真实中文聊天两字组重复率很高，实际唯一 bigram 数可能远低于假设。
在 hx 的本地环境（`devtools/local-console`）先测：

1. **真实每叶唯一 bigram 数分布**（50 条/叶的生产配置下）——这个数错了后面全错。
2. **实际误报率**（理论 p 与实测的差距），分别测单 bigram（两字查询）与多 bigram。
3. **位图读取 + 解密的真实耗时**（1000 张小 blob 全量 vs 分层）——决定 §13.3
   的分层要不要做。
4. **端到端对比**：同一查询在「有索引」与「现摘要路径」下各耗时多少、各解密多少条。

测完再定 p/k/m 与是否分层；结论写回本节，替换掉估算值。

### 13.8 现成件结论（调研 2026-08-07：都不引）

- Bloom：rbloom 等库要求自带确定性哈希（Python `hash()` 每进程 salt 不同），
  且存档格式是无稳定性承诺的内部细节——不宜作为持久化 schema。
  `bytearray` + HMAC 派生位置，30 行以内自己写。
- 盲索引：Python 无生产级库；核心是 `HMAC(子密钥, 归一化词)[:N]`。要抄的是
  CipherSweet 文档的工程纪律（每列独立子密钥、必须截断、归一化字节级一致）。
- 整套框架（Acra/CipherSweet/Clusion/CyborgDB）都假设"服务端看不到明文"，
  与我们"服务端有可信区域"不同构，硬套是净增复杂度。
- **完整开源实现**：截至调研日未发现可直接复用的成品（多租户服务端 TEE +
  加密原始 agent 对话 + 现有 cursor/tool 协议）。但**存在可借鉴的拼图**：
  Seshat（加密落盘的客户端全文索引）、Hermes Agent（对原始 session message
  做 SQLite FTS5 检索 + 前后文窗口）。这是"没找到直接适配项"，不是"不存在"。
