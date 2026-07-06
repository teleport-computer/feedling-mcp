# Memory（记忆花园）系统说明

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

入口在 `/v1/chat/history`（`backend/enclave_app.py:907` 附近），参数 `context_mode`、`context_trace`。
核心：`select_context_memories_with_trace()`（`backend/context_memory_selection.py:348`）。

两种模式：

- **`default`**（MCP / 常驻）：分三桶——
  - 转折卡（title 以 `转折｜` 开头）按时间倒序 ≤3
  - 最新创建 ≤2
  - 与最后一条用户消息**相关性最高** ≤3
  - 去重后总数 ≤8。
- **`model_api`**（Hosted API）：严格模式，只收高置信卡（见 §5）。

> 相关性**不是向量检索**，而是分层关键词评分。

### 4.3 相关性评分：`_memory_relevance()`

`backend/context_memory_selection.py:191`，对外封装 `memory_relevance_details()`（`:285`）。分层打分：

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

**稀有词 vs 通用词**由两张表区分（`context_memory_selection.py:54` 起）：

- `_EN_GENERIC_TERMS`：`project / api / model / memory / task / code …` 等通用英文词，降级为「弱词」，必须组合才有意义。
- `_ZH_GENERIC_PHRASES`：`项目 / 任务 / 今天 / 东西 …` 等通用中文短语。

`memory_relevance_details()` 返回 `{score, confidence, reason, matched_units, matched_phrases}`，`context_trace=1` 时会把选中/拒绝样本作为可审计 trace 回传。

---

## 5. `857c09e`：修复 model_api memory retrieval 误命中

**症状**：普通词 "project" 错误命中专有名词卡「TOHO Project」并被塞进上下文。

**根因**：旧逻辑只要有任意词重叠就给分，阈值 `score ≥ 0.05` 太松。

**修复两处**：

1. **引入通用词表 + 分层评分**（`context_memory_selection.py`）
   - 新增 `_EN_GENERIC_TERMS` / `_ZH_GENERIC_PHRASES`，把通用词降级；
   - 只有长度 ≥4、完整命中的实体才算 strong；
   - 用返回详情的 `_memory_relevance()` 取代旧的纯重叠计分。

2. **收紧 model_api 选择阈值**（`backend/app.py:8735`）

   ```python
   if moment.get("id") in ref_ids:
       details = {"score": 1.0, "confidence": "strong", "reason": "user_selected_context_ref"}
   else:
       details = memory_relevance_details(message, merged)   # app.py:8743
   score = float(details.get("score") or 0.0)
   confidence = str(details.get("confidence") or "none")
   # 旧: score >= 0.05  →  新: 严格门槛
   if moment.get("id") in ref_ids or (confidence in {"strong", "medium"} and score >= 0.35):
       candidates.append(...)                                # app.py:8746
   ```

   即从「任意重叠 ≥0.05」改为「置信度 ∈ {strong, medium} 且 score ≥ 0.35」，
   用户在本轮显式引用（`ref_ids`）的卡仍强制保留。

3. 新增回归测试 `test_context_memories.py`：验证 "project" 不再单独触发 TOHO Project，多词组合仍正确命中。

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
| 上下文选择主算法 | `backend/context_memory_selection.py:348` |
| 相关性评分核心 | `backend/context_memory_selection.py:191` |
| 对外封装 `memory_relevance_details` | `backend/context_memory_selection.py:285` |
| 通用词表 | `backend/context_memory_selection.py:54` / `67` |

### model_api 严格选择（857c09e 修复点）
| 功能 | 位置 |
|------|------|
| 严格阈值选择块 | `backend/app.py:8735–8746` |
| `memory_relevance_details` import | `backend/app.py:39` |
