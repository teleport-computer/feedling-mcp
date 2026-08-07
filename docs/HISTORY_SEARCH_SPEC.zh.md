# History Search Spec — 模型侧历史聊天记录检索（V2）

状态：spec 定稿（经两轮 Codex plan_review），待实现。基线 `origin/test`（4df93df7）。
背景调研与决策记录见工作区（io 根目录）`FEATURE_LOG.md`「历史聊天检索」一节
（该文件不在本仓库内）。

## 1. 问题与目标

V2 每回合上下文 = 分层滚动摘要 + 最近 tail（`_TAIL_HARD_CAP=60`）。tail 之外的
原始消息模型完全不可及；摘要有损，"上个月我们聊的那家餐厅叫什么"这类需要原话
的问题答不出。

目标：给模型两个按需检索工具，在受控预算内查回任意时期的聊天原文。
上下文组装、compaction、正常聊天路径一概不动。

心智模型：`memory_*`（提炼结论）miss 时，降级到 `history_*`（原始底账）。

## 2. 非目标

- V1（agent_runtime CLI spawn 路线）不做。
- 向量/语义检索不做（embedding 是加密架构下新的明文泄漏面）。
- 明文全文索引不做（出不了加密边界）。第二阶段可评估 enclave 内生成的加密
  lexical sidecar（n-gram/Bloom，只许 false positive），本期不实现。
- `history_fetch` 分页不做（MVP 单窗口，见 §3.2）。
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

- `before`/`after` 默认 2，各钳 [0,4]。返回结构（anchor 只出现一次，无分页）：

```
{ anchor: message, before: [旧→新], after: [旧→新], unavailable_count }
```

- 邻居按该用户 seq 序取（客户端时间戳不可靠，不做游标）。
- 锚点不可见或不存在统一返回 `not_found_or_not_visible`（不区分，
  不靠 message_id 难猜当权限控制）。
- 限制单位是**完整序列化 payload**（≤1600 字符），超限先结构化缩减
  （砍 before/after 条数、单条正文加 `content_truncated`），绝不序列化后切串。

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

## 11. 放弃的替代方案

- 摘要命中当硬过滤（一轮否决：确定性漏召回）→ 改优先级提示。
- fetch 用边缘消息当锚点翻页（二轮否决：anchor 重复/取不回目标正文的矛盾）
  → MVP 单窗口结构化返回。
- 固定 a→b→c 扫描顺序（二轮否决：backlog 吃光预算）→ 按请求形态分流。
- 明文 FTS5 索引 / 全量解密扫描 / 第一版向量检索：见 §2。
- 单工具多形态（Hermes 风格）：与现有 memory 两工具心智不一致。
- 第一版默认 OFF 分阶段开（Codex 二轮建议）：违反工作区开关纪律，见 §6。
