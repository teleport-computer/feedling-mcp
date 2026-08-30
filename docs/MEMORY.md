---
document_lifecycle: current
canonical_owner: self
---
# Memory（记忆花园）系统说明

> ⚠️ **2026-08-23 起，记忆的判断内核是外部包**（`memgarden`，源码在
> https://github.com/teleport-computer/memgarden ，Apache-2.0；0.12.3 起从 PyPI
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

### 4.2 上下文记忆选择（聊天补记忆，重点）

入口在 `/v1/chat/history`（`backend/enclave/routes/chat.py`），核心是
`_build_context_memories()` 调用外部包的 `select_context_memories_with_trace()`。

Resident 与 Hosted Runtime V2 固定使用同一套 `default` 分桶策略：

- 转折卡按时间倒序 ≤3；
- 最新创建 ≤2；
- 与最后一条用户消息相关性最高 ≤3；
- 去重后总数 ≤8。

`context_mode`、`contextMode` 和 `context_strict` 仍作为兼容 query 参数接收，但不再
选择不同策略；`context_trace=1` 继续返回不含候选记忆正文的选择 trace（其中仍包含
用户 query 派生的匹配词）。候选集先在 enclave 内完成生命周期过滤和卡片形状翻译，
再交给 selector；注入模型的仍是原始卡片形状。

> 相关性**不是向量检索**，而是分层关键词评分。

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
| Chat 上下文宿主入口 | `backend/enclave/routes/chat.py::_build_context_memories` |
| 卡片形状与生命周期适配 | `backend/memory/card_shape.py` |
| 上下文选择主算法 | `memgarden/scoring/relevance.py`（外部包） |
| 相关性评分与通用词表 | `memgarden/scoring/relevance.py`（外部包） |
| 当前回归测试 | `tests/test_context_memories.py`、`tests/test_enclave_context_recall.py` |
