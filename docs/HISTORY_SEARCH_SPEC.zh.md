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

- `before`/`after` 默认 **8**，各钳 [0,15]（窗口最多 31 条）。返回结构
  （anchor 只出现一次，无分页）：

```
{ anchor: message, before: [旧→新], after: [旧→新], unavailable_count }
```

- 邻居按该用户 seq 序取（客户端时间戳不可靠，不做游标）。
- 锚点不可见或不存在统一返回 `not_found_or_not_visible`（不区分，
  不靠 message_id 难猜当权限控制）。
- 限制单位是**完整序列化 payload**（≤**4500** 字符），超限先结构化缩减
  （从最远的邻居开始砍、单条正文加 `content_truncated`），绝不序列化后切串。
  **fetch 需要 executor 对本工具单独放宽单结果上限**（默认 `_RESULT_CHAR_CAP`
  =2000 装不下），实现时给 history_fetch 配独立 cap，其余工具不变。
  ＊修订理由（hx 2026-08-07）：原值前后各 2（≤9 条 /1600 字符）是照着 2000 字符
  硬顶倒推的技术约束，不是产品需求——真实场景里"那家餐厅"的来龙去脉常常在
  七八轮之前，各 2 条经常正好卡在关键句之外。fetch 是模型确认要看才调的，
  不存在滥用风险，4500 字符对上下文预算无压力。

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

截断：executor 单结果 2000 字符外，tool_loop 还有**同批 8000 字符水位分摊**——
8 个混合调用时单结果实际额度可能只剩 ~1000。因此 history 结果必须**序列化前
结构化缩减**到预算内（砍 matches 条数/snippet 长度），并配「8 调用混合批、
history 结果最大化、逐个 json.loads」的协议测试。

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

## 13. 第二阶段：加密 lexical sidecar（不在本期，但接口现在就留）

**动机**：现在"先扫哪段"靠摘要匹配，摘要有损→常回落到顺序扫。历史到几万条时
单次搜索翻页轮次上升。

**先决判断（调研 2026-08-07 修正）**：既然 enclave 内能见明文，**优先考虑
在 enclave 内建普通全文索引、只在落盘时整体加密**（Element/Matrix 的 seshat =
tantivy + EncryptedMmapDirectory 就是这个形态；微软 Always Encrypted 的演进也是
从确定性加密走向 enclave 内解密而不是走 SSE）。这样 BM25/短语/模糊全部可用，
检索质量不打折——比 Bloom 位图"只能回答有没有"强。

**取舍取决于 enclave 形态**（实现前必须先查清）：本项目 enclave 是无状态路由、
无持久化磁盘时，完整倒排索引要存回后端 DB 再整索取回，体积可能不划算；此时退回
Bloom 位图（每叶几 KB、只做预筛）。**两个方案二选一的判据是索引载入成本，不是
安全性**——两者都在同一信任域内、落盘都加密。

**Bloom 方案（备选）**：compaction 生成摘要叶子时，顺带在 enclave 内对该批消息
算字符 bigram 的 Bloom 位图（几 KB），加密与摘要同存。查询时在 enclave 内对
query 算同样哈希，位图不命中 = **确定性排除**（只误报不漏报），命中才解密原文。
预期把"500 段筛到 3 段"，解密量下降一到两个数量级。
**注意**：有 TEE 时 Bloom 的作用是**省 I/O，不是省信任**，误报没有安全含义。

**泄漏面控制**：位图本身加密存储、只在 enclave 内比对；不得把可比对的盲索引
token 暴露给数据库（否则新增词频/相等性/访问模式泄漏）。

**现在要留的接口**：§4 扫描顺序里"决定优先扫哪段"是一个可替换的过滤器
（今天=摘要匹配，将来=位图匹配或 enclave 内索引），实现上保持这一步与
"扫描/解密"解耦，换过滤器不动其余代码。存量老数据无索引 → 回落摘要路径。

**实现选型时的现成件结论**（调研 2026-08-07，结论是都不引）：
- Bloom：rbloom 等库要求自带确定性哈希（Python `hash()` 有每进程 salt），
  且其存档格式是无稳定性承诺的内部细节——把它变成我们的持久化 schema 不划算。
  `bytearray` + HMAC 派生位置，30 行以内自己写。
- 盲索引：Python 无生产级库；核心就是 `HMAC(子密钥, 归一化词)[:N]`。要抄的是
  CipherSweet 文档的工程纪律（每列独立子密钥、必须截断、归一化字节级一致）。
- 中文：**切字符 bigram，不引 jieba**——分词错误会永久漏召回，bigram 无死角；
  这是 SQLite/PG 社区处理 CJK 的通行解法。**不要 trigram**（中文多为二字词，
  三元组让两字查询用不上索引）。
- 整套框架（Acra/CipherSweet/Clusion/CyborgDB）全部假设"服务端看不到明文"，
  与我们"服务端有可信区域"不同构，硬套是净增复杂度。
- LLM agent + 加密历史检索的完整开源实现：**不存在**（agent memory 那一堆
  全假设明文；Proton Lumo 闭源且未公开检索机制）。这块只能自己设计。

**启动条件**（满足其一再做，不提前）：真实用户历史普遍过 2 万条；或指标显示
单次搜索平均翻页 > 2 轮。

**索引不只是提速，是修掉根子上的缺陷**（hx 2026-08-07 讨论确认）：现在第 ⑦ 步
对**摘要文本**匹配，而摘要按 `_SEGMENT_SYSTEM_PROMPT` 只保留"决定/事实/偏好/
承诺/未完成事项/重要语境"——具体实体（店名、书名、人名）极易被压掉，搜它必然
miss、只能靠顺序扫兜底。索引是对**原文**建的，实体一定在指纹里，是"精准定位"
而非"猜哪段可能有"。

**存量历史必须回填**（hx 拍板）：只覆盖上线后的新消息等于只覆盖最不需要检索的
那部分（新消息还在 tail 里）——老历史才是这个功能的主要目标。回填形态：
低优先级后台 job（同 maintenance lane）限速重扫已有摘要叶的源消息，在 enclave
内算指纹、加密存回；期间无指纹的叶自动回落摘要路径，混跑不冲突，逐段完成逐段
切换。回填要能中断续跑、要有进度可观测（content-free 计数，见 §6.5）。
