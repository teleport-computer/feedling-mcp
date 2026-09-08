---
document_lifecycle: current
canonical_owner: self
---
# Memory（记忆花园）系统说明

> ⚠️ **2026-08-23 起，记忆的判断内核是外部包**（`memgarden`，源码在
> https://github.com/teleport-computer/memgarden ，Apache-2.0；0.12.8 起从 PyPI
> 安装，版本钉在 `backend/requirements.lock`，和同源的 `agent-protocol-core` 锁步同版本）。下面出现的 `memgarden/...` 路径指的是**那个包里**的文件，
> 不在本仓库。要改内核逻辑，去那个仓库改、发新版本、再更新这里的 lock。
> 宿主侧（谁触发、怎么加解密、存哪、identity 装配、trace 落库）仍在本仓库。



> 本文档描述 **Memory Garden**——后端给 AI/用户存取「记忆卡」的业务功能。
> 与 Claude Code 自身的 `.claude` memory 无关。
> ⚠️ 行号基于撰写时的单体 `app.py`（landmark commit `857c09e`）。此后代码已拆分
> 领域包（2026-06-12）并完成 Flask→ASGI 迁移（2026-07-04）：记忆逻辑现在
> `backend/memory/`，路由在 `backend/memory/routes_asgi.py`。文中 `app.py:NNNN`
> 行号全部失效，请以函数名 grep 领域包为准；机制描述仍有效。

---

## 1. 概览

记忆是一张张「记忆卡」（moment）。每张卡有类型，路由到 iOS 的三个 tab：

| Tab | type | 含义 |
|-----|------|------|
| Story | `moment` | 你和用户之间发生的一件事 |
| Story | `quote` | 用户说过、你仍在回味的话 |
| About me | `fact` | 用户的偏好/关系/习惯/世界（密度层） |
| About me | `event` | 用户生活中一个有日期的事件 |
| TA 在想 | `insight` | 你对用户的理解，需 anchor ≥1 张已有卡 |
| TA 在想 | `reflection` | 你的独立思考，需 anchor ≥2 张，按关系年龄限频 |

数据流：

```
AI 调用工具 → 构造 v1 加密信封 → 后端 HTTP 路由（ASGI） → 加密信封原样落库 → PostgreSQL
                                                              ↓
聊天补记忆 ← 分层关键词相关性评分 ← 全量读出 ← memory_moments 表
```

关键点：**title / description 在客户端加密，服务端从不解密**，只读明文元数据（type、occurred_at、visibility 等）用于校验、排序和相关性匹配。

---

## 2. 存储：`memory_moments` 表

定义见 `backend/alembic/versions/0001_baseline.py:57`：

```sql
CREATE TABLE memory_moments (
    user_id     TEXT NOT NULL,
    moment_id   TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT '',
    doc         JSONB NOT NULL,
    PRIMARY KEY (user_id, moment_id)
);
CREATE INDEX memory_user_occ_idx ON memory_moments (user_id, occurred_at);
```

`doc` 是整张卡的 JSON。字段分两类：

- **明文（服务端可读）**：`id` / `type` / `occurred_at` / `created_at` / `source` / `visibility` / `anchor_memory_ids` / 归档标记。
- **密文信封（服务端不可读）**：`body_ct`（密文，含 title、description、her_quote、context 等用户可见内容）、`nonce`、`K_user`、`K_enclave`（`visibility=shared` 时才有）。

`occurred_at` 单独提成列只为排序/索引。

---

## 3. 写入链路

### 3.1 MCP 工具层

`feedling_memory_add_moment` → `memory_add_moment()`（`backend/mcp_server.py:1523`）

- 参数：`title, type, occurred_at, description, source, her_quote, context, linked_dimension, anchor_memory_ids`。
- 在工具层就做类型校验（`type` 必须 ∈ moment/quote/fact/event/insight/reflection；insight 需 anchor≥1，reflection 需 anchor≥2），并经 `_check_memory_quality()`（`mcp_server.py:1402`）做质量门控。
- 把用户可见内容打包进**密文 body**，明文元数据留在信封外，POST 给后端。

### 3.2 HTTP 路由层

`POST /v1/memory/add` → `memory_add()`（`backend/app.py:14049`）

1. 校验 envelope 完整性：`type` 合法、`occurred_at` 非空；`visibility=shared` 必须带 `K_enclave`。
2. 类型特定校验（`app.py:14106`）：`insight`/`reflection` 的 anchor 数量，并用 `_validate_anchor_ids()` 确认被引用的卡存在且属于本人；`reflection` 还过限频检查。
3. **不解密**，把整个 envelope 当一条 moment：`_load_moments()` 读出全量 → append → `_save_moments()`。

### 3.3 持久化层

`_save_moments()`（`app.py:13442`）在 `store.memory_lock` 下调用 `db.memory_replace_all()`（`backend/db.py:792`）：

```python
with conn.transaction():
    conn.execute("DELETE FROM memory_moments WHERE user_id = %s", (user_id,))
    for m in moments:
        conn.execute(
            "INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id, moment_id) DO UPDATE SET "
            "occurred_at = EXCLUDED.occurred_at, doc = EXCLUDED.doc",
            (user_id, str(mid), str(m.get("occurred_at") or ""), Jsonb(m)),
        )
```

> **整集原子替换**：一个事务里先删该用户全部，再逐条 upsert。这是全系统写入的统一模式。

### 3.4 更新 / 删除

- `retype` / `update`（`app.py:14164`，MCP `mcp_server.py:1634/1667`）和 `delete`（`app.py:14236`）同样走「读全量 → 改内存列表 → 整集写回」，不是单行 UPDATE/DELETE。

---

## 4. 读取链路

### 4.1 简单列表

`GET /v1/memory/list` → `memory_list()`（`app.py:14017`）→ `db.memory_load()`（`db.py:751`）：

```sql
SELECT doc FROM memory_moments WHERE user_id = %s ORDER BY occurred_at, moment_id
```

过滤归档卡 → 按 `occurred_at` 倒序 → 截断 limit 返回。

### 4.2 上下文记忆选择与注入（聊天补记忆，重点）

**选卡**入口在 `/v1/chat/history`（`backend/enclave/routes/chat.py::_build_context_memories`），
调用外部包 `memgarden.scoring.relevance` 的选卡器：已发布的 0.19.0 走
`select_context_memories_with_trace`（转折卡 ≤3 / 最新 ≤2 / 相关 ≤3，去重 ≤8，mode
`bucketed:unified`）；当依赖升级到带 `select_relevant_context_memories_with_trace`
的版本时，按特性探测切到相关性阈值 + 软配额策略（mode `relevant:unified`），
io 不依赖未发布行为。选卡 query 是**最近四条对话**（含上一条 AI 回复）拼接，
不再只看最后一句。转折角色只认卡片显式 `roles`（`backend/memory/card_shape.py::roles_of`），
不再从标题前缀猜。`context_trace=1` 返回不含卡片正文的逐卡选择原因（bucket / reason /
matched_phrases / score）。

**注入**（2026-09-08 起真实到达 prompt，此前只选不注）：

- **V1 resident**（`tools/chat_resident_consumer.py`）：每条用户消息组装前用它自己的
  `seq` 单独请求 `GET /v1/chat/history?before_seq=seq+1&limit=4&context_trace=1`
  （`_auto_memory_fetch_for_turn`），页尾用户消息必须就是本条；把选中卡渲染成
  「相关记忆」块（`_auto_memory_render`：每张 id + ≤120 字摘要 + 一句命中原因，
  按 score 排序，与用户显式引用的卡去重，预算 2500 字，超出按整卡丢弃，**永不放正文**），
  拼在用户消息之前、`quoted_memories` 块之上。无 `seq`、拉取失败、`mode=failed`
  或页不匹配 ⇒ 记 unknown 且不注入，不复用旧卡。
- **V2**（`backend/model_api_runtime/v2/memory_context.py::render`）：serve-worker 以本轮
  冻结的 seq 边界读同一接口（`serve_worker._read_context_memories`），块以
  application-data 角色放在 profile/system 之后、对话回放之前（不是特权前缀），
  JSON 条目 id / summary / reason，与 profile 摘要逐字去重，2500 字整条丢。

**到达证据**（口径：选出 ≠ 注入）：`memory.select.traced` 只记 enclave 选卡；
`memory.context.applied` 在最终发给驱动 / provider 的 payload 上核对整块是否到达
（V1 逐条渲染行，V2 整块相等），记 ids / chars / profile_used；每轮
`memory.recall.completed` 汇总 injected / selected / index / search / fetch 计数，
缺读数记 `unknown`（null），不写 0。

> 相关性**不是向量检索**，而是分层关键词评分（§4.3）；这是刻意决定，见
> `docs/HISTORY_SEARCH_SPEC.zh.md`。

**T513 增量（读侧已实现；写侧 cues 的产出依赖外部包升级）**：

- `retrieval_cues`：卡片可选字段，`list[str]`，严格只收字符串，≤5 条、每条 ≤120 字，去空去重；
  缺字段的旧卡密文形状不变。io 两侧写路径（V1 consumer `_capture_inner_from_card`、V2
  `extraction._inner_from_card`）已能透传该字段，读侧并入 `_search_content` 与 V2 一行索引。
  **已发布的 memgarden 0.19.0 尚无产出 cues 的 capture / dream parser**；产出方在外部仓
  PR-1+PR-2（未发版、io 未升 pin），发版并升 pin 前线上不会出现该字段。
- `memory_fetch` 一跳关联：返回 `related_items`（≤6 条 id / summary / source_id / relation /
  status）与 `related_status`（`ok` / `bounded` / `unavailable` / `not_needed`），只含同用户可读且
  经生命周期过滤的卡；`superseded` 关系只沿显式 `anchor_memory_ids` / `supersedes` 给出并带历史
  status，按 thread 关联不返回已退休卡；主卡正文优先占预算，预算不够时可省略 related 元信息——
  **缺失不证明无关联**。不回填用户数据。
- V2 摘要新鲜度：history 请求带 `context_recent=1` 时，最近 7 天 `created_at` 的新卡最多 3 张优先占
  `context_memories` 的 8 席，不新增 prompt 预算，「最近」不等于「相关」，profile / quoted 去重照旧；
  旧端忽略该 flag 自然降级。

### 4.3 相关性评分：`_memory_relevance()`

外部包 `memgarden.scoring.relevance` 的 `_memory_relevance()` 负责分层打分，
`memory_relevance_details()` 是公开封装：

| 匹配类型 | 分数 | 置信度 |
|---------|------|--------|
| 实体短语完整命中（长度≥4） | 0.86–0.94 | strong |
| 多词短语命中 | 0.68–0.80 | strong |
| ≥2 个稀有词 | 0.52–0.64 | medium |
| 1 稀有词 + 弱词支持 | 0.36 | medium |
| 单个稀有词 | 0.28 | weak |
| 仅弱词重叠 | ≤0.18 | weak |
| 仅字符二元组相似 | ≤0.16 | weak |
| 无重叠 | 0.0 | none |

**稀有词 vs 通用词**由外部包 `memgarden.scoring.relevance` 的两张表区分：

- `_EN_GENERIC_TERMS`：`project / api / model / memory / task / code …` 等通用英文词，降级为「弱词」，必须组合才有意义。
- `_ZH_GENERIC_PHRASES`：`项目 / 任务 / 今天 / 东西 …` 等通用中文短语。

`memory_relevance_details()` 返回 `{score, confidence, reason, matched_units, matched_phrases}`，`context_trace=1` 时会把选中/拒绝样本作为可审计 trace 回传。

---

## 5. 相关性评分与通用词降权

历史问题是普通词 "project" 会把专有名词卡「TOHO Project」打成强相关。

**根因**：旧逻辑只要有任意词重叠就给分，阈值 `score ≥ 0.05` 太松。

当前评分层通过以下规则降低这类误命中的排序：

- `_EN_GENERIC_TERMS` / `_ZH_GENERIC_PHRASES` 把通用词降为弱信号；
- 长实体短语、多词短语和多个稀有词获得更高置信度；
- 分桶策略仍会独立加入转折卡和最近卡，所以“出现在上下文”不等价于“被相关性命中”；
- `tests/test_context_memories.py` 分别覆盖相关性 bucket、卡片翻译、生命周期过滤和
  trace 元数据，避免把打底行为误判为检索误命中。

---

## 6. 关键代码索引

### 写入
| 功能 | 位置 |
|------|------|
| MCP 工具 `memory_add_moment` | `backend/mcp_server.py:1523` |
| 写入质量门控 `_check_memory_quality` | `backend/mcp_server.py:1402` |
| 路由 `POST /v1/memory/add` | `backend/app.py:14049` |
| 类型/anchor 校验 | `backend/app.py:14106` |
| `_load_moments` / `_save_moments` | `backend/app.py:13419` / `13442` |
| `db.memory_replace_all`（原子替换） | `backend/db.py:792` |
| 表定义 | `backend/alembic/versions/0001_baseline.py:57` |

### 读取与评分
| 功能 | 位置 |
|------|------|
| 路由 `GET /v1/memory/list` | `backend/app.py:14017` |
| `db.memory_load` | `backend/db.py:751` |
| Chat 上下文宿主入口（选卡） | `backend/enclave/routes/chat.py::_build_context_memories` |
| V1 逐轮注入 | `tools/chat_resident_consumer.py::_auto_memory_fetch_for_turn` / `_auto_memory_render` / `_auto_memory_arrival` |
| V2 逐轮注入 | `backend/model_api_runtime/v2/memory_context.py::render`、`serve_worker._read_context_memories` |
| 召回观测事件 | `memory.select.traced` / `memory.context.applied` / `memory.recall.tool_result` / `memory.recall.completed`（`backend/model_api_runtime/v2/memory_recall.py`、consumer `_emit_recall_completed`） |
| 卡片形状、生命周期与显式角色 | `backend/memory/card_shape.py` |
| 上下文选择主算法 | `memgarden/scoring/relevance.py`（外部包） |
| 相关性评分与通用词表 | `memgarden/scoring/relevance.py`（外部包） |
| 当前回归测试 | `tests/test_context_memories.py`、`tests/test_enclave_context_recall.py`、`tests/test_io_cli_auth.py`（V1 注入/到达）、`tests/test_v2_context.py` / `tests/test_v2_worker.py`（V2 注入） |
