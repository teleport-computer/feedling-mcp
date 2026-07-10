# TEE Postgres Phase 2–3（明文 schema + 双写 + 解密复制）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 达到目标形态「RDS 主 + TEE 明文影子库」：13 张明文表同步双写 + reconciler 兜底，6 类密文内容经 enclave 解密异步复制成明文，一致性验证全绿。切读（Phase 4+）不在本 plan 内。

**Architecture:** db.py 加第二连接池（影子 mirror 尽力而为，绝不影响主路径）；`backend/tee_shadow/` 承载 mirror/reconciler；`backend/tee_replicator/` 承载解密复制（水位游标 + enclave decrypt + 幂等 upsert + 限速断点续传）；TEE 库用独立 `backend/alembic_tee/` 迁移链（owner 凭证独立步骤跑，app 角色无 DDL）。

**Tech Stack:** psycopg3/psycopg_pool、Alembic（第二链）、httpx（enclave decrypt 复用 `core.enclave`）、pytest（双临时库）。

**Spec:** `docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md`。**前置**：Plan 1（Phase 0–1）完成，`TEE_DATABASE_URL` 可用。

## Global Constraints

- **实施分支基线 = `test`**。与 Plan 1 无代码交集，可并行开发（本地用第二个 throwaway PG 库当 TEE 库，不等 CVM）。
- 双写期 **TEE 是影子库**：mirror 任何失败只 log+计数，绝不抛给业务路径；关掉 `FEEDLING_TEE_DUAL_WRITE` 系统行为必须与现状 bit 一致。
- **热路径绝不同步 decrypt**（enclave 单线程 502 教训）；解密只发生在 tee_replicator（限速）。
- 凭证例外矩阵（spec §4【补充】）：`user_blobs` 里 `model_api.api_key_envelope` 等 provider key/凭证 **保持 envelope 原样镜像**，不解密、不明文化；本 plan 只明文化用户内容。
- env 命名循本仓惯例：`TEE_DATABASE_URL`（对齐 `DATABASE_URL`）、`FEEDLING_TEE_*` 开关；点用点读（`os.environ`），无中央 config。
- alembic_tee 用 `TEE_MIGRATION_DATABASE_URL`（owner 凭证）由 CI/手动独立跑；app 进程用 `TEE_DATABASE_URL`（`app` 角色）**不做 DDL**（spec §3 角色拆分决策）。
- 每个 Task 末尾 commit 须用户明确授权。

## 现状事实（实施者速查，已核实于 test 分支）

- `backend/db.py`：无通用 query helper，每个函数内联 `with get_pool().connection() as conn`；`get_pool()`（:63，min 2/max 16/autocommit）；`listen_connection()`（:141，池外专用连接）；`init_schema()`（:90，启动时跑 RDS alembic）。
- 密文写入点：`chat_append`(:1815)、`memory_upsert`(:1969)/`memory_replace_all`、`world_book_upsert`(:2064)、`frame_upsert`(:2152，R2 offload 双形态：inline `doc` vs `env_meta`+`body_key`+`doc=NULL`)、`genesis_put_chunk`(:1537，`encrypted_body BYTEA`)；photo 复用 `frame_envelopes`（`perception/store.py:366`）。
- chat doc 里最多三套 envelope：主 `body_ct/...` + `thinking_*` + `caption_*`（`core/store.py:334-421` 有完整字段清单）。
- enclave decrypt 客户端：`core.enclave._decrypt_envelope_via_enclave(record, api_key, purpose=...)`（`backend/core/enclave.py:133`，POST `/v1/envelope/decrypt`，认 `X-API-Key` 或 `X-Feedling-Runtime-Token`）；token 铸造：`core.runtime_token`（supervisor.py:59,1141 的 `mint_token(user_id)` 用法照抄）。
- 测试约定：`tests/conftest.py` 连 `FEEDLING_TEST_PG`（默认 `postgresql://postgres:test@127.0.0.1:55432/postgres`）建 throwaway 库；fixture `backend_env` + `client`（`make_client`）；`seed_user` 先建 users 行（CASCADE FK）。

---

### Task 1: alembic_tee 迁移链 + 明文 schema + 测试基建

**Files:**
- Create: `backend/alembic_tee/__init__.py`、`backend/alembic_tee/__main__.py`、`backend/alembic_tee/alembic.ini`、`backend/alembic_tee/env.py`、`backend/alembic_tee/versions/0001_tee_baseline.py`
- Modify: `tests/conftest.py`（第二 throwaway 库 + `TEE_DATABASE_URL`）
- Test: `tests/test_tee_schema.py`

**Interfaces:**
- Produces: `python -m backend.alembic_tee upgrade`（读 `TEE_MIGRATION_DATABASE_URL`，回退 `TEE_DATABASE_URL`）；TEE 库表集（下述）；conftest fixture 保证每个测试会话有独立 TEE 库并升到 head。Task 2–7 全部消费。

TEE 库表集（版本表名 `alembic_tee_version`，与 RDS 链隔离）：

1. **13 张明文表 DDL 原样复制**（从 `backend/alembic/versions/0001_baseline.py` 等抄 `CREATE TABLE IF NOT EXISTS` 原文，含 0012 的 per-user `ON DELETE CASCADE`）：`server_config, global_blobs, users, user_blobs, user_logs, perception_items, perception_daily, copytext_strings, copytext_meta, genesis_import_jobs, genesis_import_outputs, agent_runtime_instances, agent_runtime_supervisor_heartbeats`。
2. **明文内容表**（行形不变、doc 内容明文化——切读时读路径改动最小）：
   - `chat_messages`：**与 RDS 列形完全一致**（`user_id, seq BIGINT GENERATED ALWAYS AS IDENTITY, msg_id, ts, doc JSONB`，PK `(user_id, msg_id)` + `chat_user_seq_idx`；2026-07-07 裁决，保证镜像/复制 SQL 两库同构）；doc 存 `{id, role, ts, source, content_type, visibility, owner_user_id, body TEXT, thinking JSONB?, caption JSONB?}`；`thinking = {body, kind, source, model, visibility}`、`caption = {body, visibility}`。
   - `memory_moments` / `world_book_entries`：同列结构，doc 内 `body`（明文）+ 保留 `visibility/owner_user_id/occurred_at/updated_at/id` 等非信封字段。
   - `frames(user_id, frame_id, ts, meta JSONB, body_storage_key TEXT, body_storage_key_version TEXT, body_mime TEXT, body_sha256 TEXT, body_size_bytes BIGINT)`（spec §4：inline 密文不落 TEE 行内，body 一律 R2 存储层加密）。
   - `genesis_import_chunks_pending` **不建**——见 Task 5 Step 1 的决策（chunks 是 staging 数据，冻结窗口处理，不复制）。
3. **运维表**：
   - `tee_replication_cursors(table_name TEXT PRIMARY KEY, watermark_ts DOUBLE PRECISION NOT NULL DEFAULT 0, watermark_id TEXT NOT NULL DEFAULT '', updated_at TIMESTAMPTZ NOT NULL DEFAULT now())`
   - `tee_pending_device_migration(user_id TEXT, table_name TEXT, item_id TEXT, reason TEXT, marked_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(user_id, table_name, item_id))`（local_only 解不了的行，等 D1 的 iOS 重传）

- [ ] **Step 1: conftest 加第二 throwaway 库**

在 `tests/conftest.py` 现有建库逻辑旁（module-level，紧跟 `os.environ["DATABASE_URL"] = ...`）：

```python
_TEE_DB = f"feedling_tee_test_{uuid.uuid4().hex[:12]}"
with psycopg.connect(_ADMIN_URL, autocommit=True) as _c:
    _c.execute(f'CREATE DATABASE "{_TEE_DB}"')
os.environ["TEE_DATABASE_URL"] = _admin_url_for(_TEE_DB)
os.environ["TEE_MIGRATION_DATABASE_URL"] = os.environ["TEE_DATABASE_URL"]
```
并在 `pytest_unconfigure` 里 DROP（照抄现有 drop 逻辑）。conftest 里 `db.init_schema()` 后面加 `from alembic_tee import upgrade_head; upgrade_head()`。

- [ ] **Step 2: 失败测试**

```python
# tests/test_tee_schema.py
import os, psycopg

def _tee_conn():
    return psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True)

def test_tee_schema_has_all_tables():
    want = {"server_config","global_blobs","users","user_blobs","user_logs",
            "perception_items","perception_daily","copytext_strings","copytext_meta",
            "genesis_import_jobs","genesis_import_outputs","agent_runtime_instances",
            "agent_runtime_supervisor_heartbeats","chat_messages","memory_moments",
            "world_book_entries","frames","tee_replication_cursors",
            "tee_pending_device_migration"}
    with _tee_conn() as c:
        rows = c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
    assert want <= {r[0] for r in rows}

def test_tee_version_table_is_isolated():
    with _tee_conn() as c:
        rows = c.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE 'alembic%'").fetchall()
    assert {r[0] for r in rows} == {"alembic_tee_version"}
```

Run: `pytest tests/test_tee_schema.py -v` → FAIL（`alembic_tee` 不存在）。

- [ ] **Step 3: 实现 alembic_tee**

`env.py` 照抄 `backend/alembic/env.py`，改两点：URL 读 `TEE_MIGRATION_DATABASE_URL` 回退 `TEE_DATABASE_URL`；`context.configure(..., version_table="alembic_tee_version")`。`__init__.py`：

```python
# backend/alembic_tee/__init__.py
"""TEE 明文库的独立 Alembic 链（spec §4）。owner 凭证独立跑，app 角色不做 DDL。"""
from pathlib import Path

def upgrade_head() -> None:
    from alembic import command
    from alembic.config import Config
    here = Path(__file__).resolve().parent
    cfg = Config(str(here / "alembic.ini"))
    cfg.set_main_option("script_location", str(here))
    command.upgrade(cfg, "head")
```

```python
# backend/alembic_tee/__main__.py
import sys
from . import upgrade_head
if __name__ == "__main__":
    assert sys.argv[1:] == ["upgrade"], "usage: python -m backend.alembic_tee upgrade"
    upgrade_head()
    print("[alembic_tee] schema at head")
```

`versions/0001_tee_baseline.py`：13 张表 DDL 从 RDS 链原样搬 + 上述明文内容表/运维表（`CREATE TABLE IF NOT EXISTS` 原生 SQL，风格照 `0001_baseline.py`）。

- [ ] **Step 4: 测试通过**

Run: `pytest tests/test_tee_schema.py -v` → 2 PASS。再跑 `pytest tests/ -x -q` 确认零回归。

- [ ] **Step 5: Commit（须授权）**

```bash
git add backend/alembic_tee/ tests/conftest.py tests/test_tee_schema.py
git commit -m "feat(tee-shadow): independent alembic_tee chain + plaintext schema"
```

---

### Task 2: 影子池 + mirror 执行器

**Files:**
- Create: `backend/tee_shadow/__init__.py`、`backend/tee_shadow/mirror.py`
- Test: `tests/test_tee_mirror.py`

**Interfaces:**
- Produces: `tee_shadow.mirror.execute(sql: str, params: tuple = ()) -> None`（尽力而为，永不 raise）；`tee_shadow.mirror.enabled() -> bool`；`tee_shadow.mirror.get_tee_pool()`；`tee_shadow.mirror.failure_count() -> int`。Task 3 在 db.py 写入点调用；Task 4/5 复用 `get_tee_pool()`。

- [ ] **Step 1: 失败测试**

```python
# tests/test_tee_mirror.py
import os, psycopg
from tee_shadow import mirror

def _tee(sql):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        return c.execute(sql).fetchall()

def test_mirror_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("FEEDLING_TEE_DUAL_WRITE", raising=False)
    mirror.execute("INSERT INTO server_config (key, value) VALUES (%s, %s)", ("k1", b"v"))
    assert _tee("SELECT count(*) FROM server_config WHERE key='k1'")[0][0] == 0

def test_mirror_writes_when_enabled(monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    mirror.execute(
        "INSERT INTO server_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("k2", b"v"))
    assert _tee("SELECT count(*) FROM server_config WHERE key='k2'")[0][0] == 1

def test_mirror_swallows_failure_and_counts(monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    before = mirror.failure_count()
    mirror.execute("INSERT INTO no_such_table VALUES (1)")  # 必须不 raise
    assert mirror.failure_count() == before + 1
```

Run: `pytest tests/test_tee_mirror.py -v` → FAIL（模块不存在）。

- [ ] **Step 2: 实现 mirror.py**

```python
# backend/tee_shadow/mirror.py
"""TEE 影子库尽力而为镜像（spec §5.1）。

影子期铁律：任何失败只 log+计数，绝不传染主路径；漏写由 reconciler 补偿。
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("feedling.tee_shadow")
_pool = None
_pool_lock = threading.Lock()
_failures = 0
_failures_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("FEEDLING_TEE_DUAL_WRITE", "") == "1" and bool(
        os.environ.get("TEE_DATABASE_URL"))


def get_tee_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool
                _pool = ConnectionPool(
                    os.environ["TEE_DATABASE_URL"],
                    min_size=1,
                    max_size=int(os.environ.get("FEEDLING_TEE_POOL_MAX", "4")),
                    timeout=5,
                    max_idle=300,
                    kwargs={"autocommit": True},
                    open=True,
                )
    return _pool


def failure_count() -> int:
    return _failures


def _record_failure(exc: Exception, sql: str) -> None:
    global _failures
    with _failures_lock:
        _failures += 1
    log.warning("[tee-mirror] shadow write failed (#%d): %s | sql=%.80s",
                _failures, exc, sql)


def execute(sql: str, params: tuple = ()) -> None:
    if not enabled():
        return
    try:
        with get_tee_pool().connection() as conn:
            conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001 — 影子期吞掉一切
        _record_failure(exc, sql)
```

- [ ] **Step 3: 测试通过 + 全量基线**

Run: `pytest tests/test_tee_mirror.py -v` → 3 PASS；`pytest tests/ -x -q` 零回归。

- [ ] **Step 4: Commit（须授权）**

```bash
git add backend/tee_shadow/ tests/test_tee_mirror.py
git commit -m "feat(tee-shadow): best-effort mirror executor + shadow pool"
```

---

### Task 3: 明文表双写接线（db.py 写入点 fan-out）

**Files:**
- Modify: `backend/db.py`（下列写入点各加一行 mirror 调用）
- Test: `tests/test_tee_dual_write.py`

**Interfaces:**
- Consumes: `tee_shadow.mirror.execute`。
- Produces: `FEEDLING_TEE_DUAL_WRITE=1` 时 13 张明文表新写入两库同步；**所有表**（含密文表）的 DELETE/prune/visibility 类明文安全操作也镜像（水位复制看不见 DELETE，spec §5 一致性的关键补丁）。

**接线清单（已核实的 db.py 公开函数，每个在主写 `conn.execute(...)` 之后追加 `mirror.execute(同一 SQL, 同一 params)`）：**

- server_config：`set_config`、`set_config_if_absent`
- users：`insert_user`、`upsert_user`、`save_all_users`、`delete_user`
- global_blobs：`set_global_blob`
- user_blobs：`set_blob`、`delete_blob`、`try_stamp_hosted_tick`
- user_logs：`log_append`、`log_patch_item`、`log_trim`、`log_prune_older_than`
- heartbeats：`set_supervisor_heartbeat`、`set_supervisor_instance_heartbeat`、`prune_supervisor_instance_heartbeats`
- genesis（明文两张）：`genesis_create_job`、`genesis_claim_uploaded_jobs`、`genesis_reap_stale_processing_jobs`、`genesis_set_job_status`、`genesis_touch_job`、`genesis_upsert_output`、`genesis_complete_job`、`genesis_mark_finalized`（仅 jobs/outputs 的语句镜像；chunks 语句跳过）
- **密文表的明文安全操作**（TEE 侧表名 frames 不同，SQL 需按 TEE schema 改写后镜像）：`chat_delete`、`chat_clear`、`chat_update_metadata`、`chat_try_claim_reply`（只镜像 claim 状态字段更新）、`memory_delete`、`world_book_delete`、`frame_delete`（→ `DELETE FROM frames ...`）、`frame_prune_to`、`delete_user_data`、`delete_user_frames`
- perception：`perception/store.py:213` 的明文 INSERT（perception_items）与 perception_daily 写点，同法接线
- copytext：`backend/copytext/` 内写点（实施时 `grep -n "INSERT INTO copytext\|UPDATE copytext" backend/` 全数接线）

多语句事务（如 `genesis_put_chunk` 用 `conn.transaction()`）：镜像端把同组语句包进 `mirror.execute_many(list[(sql, params)])`（在 mirror.py 补一个同风格函数，事务内执行、失败整组吞掉计数一次）。

- [ ] **Step 1: 失败测试（代表性三点 + 开关关等价性）**

```python
# tests/test_tee_dual_write.py
import os, psycopg
import db

def _tee(sql, params=()):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        return c.execute(sql, params).fetchall()

def test_upsert_user_dual_writes(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    db.upsert_user({"user_id": "usr_dw1", "api_key_hash": "h", "doc": {}})
    assert _tee("SELECT count(*) FROM users WHERE user_id='usr_dw1'")[0][0] == 1

def test_log_append_dual_writes(backend_env, monkeypatch, seed_user):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    seed_user("usr_dw2")
    db.log_append("usr_dw2", {"kind": "test", "ts": 1.0})
    assert _tee("SELECT count(*) FROM user_logs WHERE user_id='usr_dw2'")[0][0] == 1

def test_chat_delete_mirrors_to_tee(backend_env, monkeypatch, seed_user):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    seed_user("usr_dw3")
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO users (user_id, doc) VALUES ('usr_dw3','{}'::jsonb) ON CONFLICT DO NOTHING")
        c.execute("INSERT INTO chat_messages (user_id, id, ts, doc) VALUES ('usr_dw3','m1',1.0,'{}'::jsonb)")
    db.chat_delete("usr_dw3", "m1")
    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id='usr_dw3'")[0][0] == 0

def test_flag_off_is_bit_identical(backend_env, monkeypatch):
    monkeypatch.delenv("FEEDLING_TEE_DUAL_WRITE", raising=False)
    db.upsert_user({"user_id": "usr_dw4", "api_key_hash": "h", "doc": {}})
    assert _tee("SELECT count(*) FROM users WHERE user_id='usr_dw4'")[0][0] == 0
```

（`seed_user` 直接用 conftest 现有 fixture；签名以 conftest 为准。）
Run: `pytest tests/test_tee_dual_write.py -v` → FAIL。

- [ ] **Step 2: 逐点接线（模式示例）**

以 `set_config` 为例，其余同模式：

```python
def set_config(key: str, value: bytes) -> None:
    sql = ("INSERT INTO server_config (key, value) VALUES (%s, %s) "
           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    with get_pool().connection() as conn:
        conn.execute(sql, (key, value))
    from tee_shadow import mirror
    mirror.execute(sql, (key, value))
```

要点：SQL 提成局部变量避免两份漂移；import 放函数内（避免模块环依赖，db.py 是最底层）。TEE 表名不同的（frame_envelopes→frames）单独写镜像 SQL。

- [ ] **Step 3: 测试通过 + 全量基线**

Run: `pytest tests/test_tee_dual_write.py tests/ -x -q` → 全 PASS（重点看关开关等价性）。

- [ ] **Step 4: 接线完整性 grep 自查**

Run: `grep -n "INSERT INTO\|UPDATE \|DELETE FROM" backend/db.py | grep -iv "select" | wc -l`，逐条核对每个语句要么已接 mirror、要么属于密文表 doc 写入（Task 5 管）、要么是纯读。把结论（哪些语句归哪类）写进 PR 描述。

- [ ] **Step 5: Commit（须授权）**

```bash
git add backend/db.py backend/perception/store.py backend/copytext/ tests/test_tee_dual_write.py
git commit -m "feat(tee-shadow): dual-write fan-out on plaintext writes + delete mirroring"
```

---

### Task 4: reconciler（首次回填 + 周期补偿）

**Files:**
- Create: `backend/tee_shadow/reconciler.py`、`backend/tee_shadow/__main__.py`
- Test: `tests/test_tee_reconciler.py`

**Interfaces:**
- Consumes: `mirror.get_tee_pool()`、`db.get_pool()`。
- Produces: `reconciler.reconcile_table(table: str, *, prune: bool = True) -> dict`（返回 `{"table", "copied", "pruned", "rds_rows", "tee_rows"}`）；`reconciler.reconcile_all() -> list[dict]`；CLI `python -m backend.tee_shadow reconcile [--table T]`。Task 8 的 workflow 与停 RDS gate 消费报告。

语义：对 13 张明文表做**全表收敛**——RDS→TEE 按主键分批（1000 行）upsert；`prune=True` 时删除 TEE 有而 RDS 没有的行（id 集合差）。首次运行 = 存量回填（spec §5.1【补充】），之后周期跑 = 双写失败补偿。密文表**不在此处理**（Task 5 的 replicator 管）。

- [ ] **Step 1: 失败测试**

```python
# tests/test_tee_reconciler.py
import os, psycopg
import db
from tee_shadow import reconciler

def _tee(sql, params=()):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        return c.execute(sql, params).fetchall()

def test_backfill_copies_preexisting_rows(backend_env):
    db.upsert_user({"user_id": "usr_rc1", "api_key_hash": "h", "doc": {"a": 1}})  # 双写关着：只在 RDS
    report = reconciler.reconcile_table("users")
    assert report["copied"] >= 1
    assert _tee("SELECT doc->>'a' FROM users WHERE user_id='usr_rc1'")[0][0] == "1"

def test_prune_removes_tee_orphans(backend_env):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        c.execute("INSERT INTO users (user_id, doc) VALUES ('usr_ghost','{}'::jsonb)")
    report = reconciler.reconcile_table("users")
    assert report["pruned"] >= 1
    assert _tee("SELECT count(*) FROM users WHERE user_id='usr_ghost'")[0][0] == 0

def test_converges_after_simulated_mirror_outage(backend_env, monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    monkeypatch.setenv("TEE_DATABASE_URL", "postgresql://invalid:1/x")  # 制造双写失败
    import tee_shadow.mirror as m; m._pool = None
    db.upsert_user({"user_id": "usr_rc3", "api_key_hash": "h", "doc": {}})
    monkeypatch.undo(); m._pool = None                                   # 恢复
    report = reconciler.reconcile_table("users")
    assert _tee("SELECT count(*) FROM users WHERE user_id='usr_rc3'")[0][0] == 1
    assert report["rds_rows"] == report["tee_rows"]
```

Run: FAIL（模块不存在）。

- [ ] **Step 2: 实现**

核心（每表一条主键定义，其余通用）：

```python
# backend/tee_shadow/reconciler.py
"""RDS→TEE 明文表全表收敛：首次=存量回填，周期=双写失败补偿（spec §5.1【补充】）。"""
from __future__ import annotations

import logging

import db
from tee_shadow import mirror

log = logging.getLogger("feedling.tee_shadow")

# table -> (pk 列元组, 全列 SELECT 列表)。列清单以 alembic_tee 0001 为准。
TABLES: dict[str, tuple[tuple[str, ...], str]] = {
    "server_config": (("key",), "key, value"),
    "global_blobs": (("key",), "key, doc"),
    "users": (("user_id",), "user_id, api_key_hash, doc"),
    "user_blobs": (("user_id", "kind"), "user_id, kind, doc"),
    "user_logs": (("user_id", "seq"), "user_id, seq, ts, doc"),
    "perception_items": (("user_id", "id"), "user_id, id, ts, doc"),
    "perception_daily": (("user_id", "day"), "user_id, day, doc"),
    "copytext_strings": (("key",), "key, lang, value"),
    "copytext_meta": (("key",), "key, value"),
    "genesis_import_jobs": (("user_id", "job_id"), "user_id, job_id, doc"),
    "genesis_import_outputs": (("user_id", "job_id"), "user_id, job_id, doc"),
    "agent_runtime_instances": (("user_id",), "user_id, doc"),
    "agent_runtime_supervisor_heartbeats": (("owner",), "owner, ts, doc"),
}
# ⚠️ 上表列名在实施时逐表对照 backend/alembic/versions/ 的真实 DDL 校正（TDD 会逼出来）。

BATCH = 1000


def reconcile_table(table: str, *, prune: bool = True) -> dict:
    pk, cols = TABLES[table]
    col_list = [c.strip() for c in cols.split(",")]
    placeholders = ", ".join(["%s"] * len(col_list))
    conflict = ", ".join(pk)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in col_list if c not in pk)
    upsert = (f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
              f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}")
    copied = pruned = rds_rows = 0
    with db.get_pool().connection() as src, mirror.get_tee_pool().connection() as dst:
        with src.cursor(name=f"reconcile_{table}") as cur:  # server-side cursor 流式
            cur.execute(f"SELECT {cols} FROM {table}")
            while rows := cur.fetchmany(BATCH):
                rds_rows += len(rows)
                with dst.transaction():
                    for row in rows:
                        dst.execute(upsert, row)
                copied += len(rows)
        if prune:
            pk_cols = ", ".join(pk)
            src_keys = {tuple(r) for r in src.execute(f"SELECT {pk_cols} FROM {table}")}
            tee_keys = {tuple(r) for r in dst.execute(f"SELECT {pk_cols} FROM {table}")}
            for key in tee_keys - src_keys:
                cond = " AND ".join(f"{c} = %s" for c in pk)
                dst.execute(f"DELETE FROM {table} WHERE {cond}", key)
                pruned += 1
        tee_rows = dst.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    report = {"table": table, "copied": copied, "pruned": pruned,
              "rds_rows": rds_rows, "tee_rows": tee_rows}
    log.info("[reconcile] %s", report)
    return report


def reconcile_all() -> list[dict]:
    return [reconcile_table(t) for t in TABLES]
```

`__main__.py`：`python -m backend.tee_shadow reconcile [--table users]` → 打 JSON 报告，任何表 `rds_rows != tee_rows` 时 exit 1。

- [ ] **Step 3: 测试通过 + 全量基线**

Run: `pytest tests/test_tee_reconciler.py tests/ -x -q` → PASS。

- [ ] **Step 4: Commit（须授权）**

```bash
git add backend/tee_shadow/ tests/test_tee_reconciler.py
git commit -m "feat(tee-shadow): full-table reconciler (backfill + compensation + prune)"
```

---

### Task 5: tee_replicator —— 密文内容解密复制

**Files:**
- Create: `backend/tee_replicator/__init__.py`、`backend/tee_replicator/transforms.py`、`backend/tee_replicator/worker.py`、`backend/tee_replicator/__main__.py`
- Test: `tests/test_tee_replicator_transforms.py`、`tests/test_tee_replicator_worker.py`

**Interfaces:**
- Consumes: `core.enclave._decrypt_envelope_via_enclave(record, api_key, purpose)`（token 经 `core.runtime_token` 铸造，用法照抄 `agent_runtime/supervisor.py:1141` 的 `mint_token`）；`mirror.get_tee_pool()`；Task 1 的游标表。
- Produces: `transforms.plaintext_chat_doc(doc: dict, decrypt) -> dict`（`decrypt(envelope: dict, purpose: str) -> bytes`）、同族 `plaintext_memory_doc` / `plaintext_world_book_doc`；`worker.run_table(table: str, *, qps: float, dry_run: bool, limit: int | None) -> dict`；CLI `python -m backend.tee_replicator run --table chat_messages --qps 2 [--dry-run]`。Task 6 消费同一 transforms 做抽样比对；Task 8 的 workflow 触发 CLI。

**范围决策（此处定案，勿再漂移）：**
- 复制：`chat_messages`（主/thinking/caption 三套 envelope）、`memory_moments`、`world_book_entries`、`frame_envelopes`（→ `frames`，Task 6 单独做，因涉 R2）、identity envelope（`user_blobs` kind=identity，明文化进 TEE `user_blobs`）。
- **不复制 `genesis_import_chunks`**：chunks 是上传 staging，distill 完成即无用；切读窗口冻结进行中 import 即可。偏离 spec §4 一处，已在 spec 决策点外——**需用户确认**；确认后回填 spec。
- `local_only` 行（`visibility=="local_only"` 或无 `K_enclave`）：不解密，写 `tee_pending_device_migration` 一行，游标照常推进。
- 水位：`(ts, id)` 复合游标，只追加式扫描；**in-place 更新**（rewrap 戳、visibility swap）靠 Task 3 已镜像的明文安全操作 + Task 6 抽样比对兜底。

- [ ] **Step 1: transforms 失败测试（纯函数，不碰网络）**

```python
# tests/test_tee_replicator_transforms.py
from tee_replicator import transforms

def _decrypt_stub(envelope, purpose):
    return b"PT:" + envelope["body_ct"].encode()   # 测试桩：可预测映射

def test_chat_doc_all_three_envelopes():
    doc = {"id": "m1", "role": "assistant", "ts": 1.0, "source": "chat",
           "content_type": "text", "visibility": "shared", "owner_user_id": "u",
           "v": 1, "body_ct": "AAA", "nonce": "n", "K_user": "k", "K_enclave": "ke",
           "enclave_pk_fpr": "f",
           "thinking_v": 1, "thinking_body_ct": "BBB", "thinking_nonce": "n",
           "thinking_K_user": "k", "thinking_K_enclave": "ke",
           "thinking_kind": "reasoning", "thinking_source": "codex", "thinking_model": "m",
           "caption_v": 1, "caption_body_ct": "CCC", "caption_nonce": "n",
           "caption_K_user": "k", "caption_K_enclave": "ke", "caption_visibility": "shared"}
    out = transforms.plaintext_chat_doc(doc, _decrypt_stub)
    assert out["body"] == "PT:AAA"
    assert out["thinking"]["body"] == "PT:BBB" and out["thinking"]["kind"] == "reasoning"
    assert out["caption"]["body"] == "PT:CCC"
    for k in out:  # 不许信封字段泄漏进明文 doc
        assert "body_ct" not in k and "K_user" not in k and "K_enclave" not in k
    assert out["visibility"] == "shared" and out["role"] == "assistant"

def test_chat_doc_plain_main_only():
    doc = {"id": "m2", "role": "user", "ts": 2.0, "visibility": "shared",
           "owner_user_id": "u", "v": 1, "body_ct": "DDD", "nonce": "n",
           "K_user": "k", "K_enclave": "ke", "enclave_pk_fpr": "f"}
    out = transforms.plaintext_chat_doc(doc, _decrypt_stub)
    assert out["body"] == "PT:DDD" and "thinking" not in out and "caption" not in out

def test_local_only_raises_pending():
    doc = {"id": "m3", "visibility": "local_only", "body_ct": "X", "nonce": "n",
           "K_user": "k", "v": 1, "owner_user_id": "u", "ts": 3.0, "role": "user"}
    try:
        transforms.plaintext_chat_doc(doc, _decrypt_stub)
        assert False, "expected PendingDeviceMigration"
    except transforms.PendingDeviceMigration:
        pass
```

Run: FAIL。

- [ ] **Step 2: 实现 transforms.py**

```python
# backend/tee_replicator/transforms.py
"""密文 doc → 明文 doc。纯函数 + 注入 decrypt 回调，方便测试。"""
from __future__ import annotations

_ENVELOPE_KEYS = {"v", "body_ct", "nonce", "K_user", "K_enclave", "enclave_pk_fpr",
                  "content_pk_fpr"}
_PREFIXES = ("thinking_", "caption_")


class PendingDeviceMigration(Exception):
    """local_only / 无 K_enclave：enclave 解不了，转 D1 重传流程。"""


def _sub_envelope(doc: dict, prefix: str) -> dict | None:
    if f"{prefix}body_ct" not in doc:
        return None
    return {k[len(prefix):]: v for k, v in doc.items() if k.startswith(prefix)}


def _decryptable(env: dict) -> bool:
    return env.get("visibility") != "local_only" and bool(env.get("K_enclave"))


def plaintext_chat_doc(doc: dict, decrypt) -> dict:
    main = {k: v for k, v in doc.items() if k in _ENVELOPE_KEYS or k == "visibility"
            or k in ("owner_user_id", "id")}
    main.setdefault("visibility", doc.get("visibility", "shared"))
    if not _decryptable({**doc, **main}):
        raise PendingDeviceMigration(doc.get("id", ""))
    out = {k: v for k, v in doc.items()
           if k not in _ENVELOPE_KEYS and not k.startswith(_PREFIXES)}
    out["body"] = decrypt(
        {k: doc[k] for k in _ENVELOPE_KEYS | {"owner_user_id", "id", "visibility"} if k in doc},
        purpose=f"tee_replicate:chat:{doc.get('id','')}").decode("utf-8", "replace")
    for prefix, key in (("thinking_", "thinking"), ("caption_", "caption")):
        sub = _sub_envelope(doc, prefix)
        if sub is None:
            continue
        if not _decryptable(sub):
            raise PendingDeviceMigration(f"{doc.get('id','')}:{key}")
        body = decrypt({**sub, "owner_user_id": doc.get("owner_user_id", "")},
                       purpose=f"tee_replicate:chat_{key}:{doc.get('id','')}")
        meta = {"body": body.decode("utf-8", "replace"),
                "visibility": sub.get("visibility", out["visibility"])}
        for extra in ("kind", "source", "model"):
            if extra in sub:
                meta[extra] = sub[extra]
        out[key] = meta
    return out


def plaintext_memory_doc(doc: dict, decrypt) -> dict:
    if not _decryptable(doc):
        raise PendingDeviceMigration(doc.get("id", ""))
    out = {k: v for k, v in doc.items() if k not in _ENVELOPE_KEYS}
    out["body"] = decrypt(doc, purpose=f"tee_replicate:memory:{doc.get('id','')}"
                          ).decode("utf-8", "replace")
    return out


def plaintext_world_book_doc(doc: dict, decrypt) -> dict:
    if not _decryptable(doc):
        raise PendingDeviceMigration(doc.get("id", ""))
    out = {k: v for k, v in doc.items() if k not in _ENVELOPE_KEYS}
    out["body"] = decrypt(doc, purpose=f"tee_replicate:world_book:{doc.get('id','')}"
                          ).decode("utf-8", "replace")
    return out
```

（decrypt 回调把 envelope 字段子集原样交给 `core.enclave._decrypt_envelope_via_enclave`；worker 里组装。）
Run transforms 测试 → PASS。

- [ ] **Step 3: worker 失败测试（decrypt 打桩，验证游标/幂等/限速/pending）**

```python
# tests/test_tee_replicator_worker.py 关键用例（decrypt 层 monkeypatch 成桩）：
# 1) run_table 首跑复制全部 shared 行、游标推进到最大 (ts,id)
# 2) 再跑一次 copied==0（幂等）；新增一行后只复制增量
# 3) local_only 行 → tee_pending_device_migration 有记录、不复制、游标照推
# 4) dry_run=True 时 TEE 零写入但报告给出 would_copy 计数
# 5) qps 限速：mock time 校验批间 sleep 调用（或注入 clock）
```

（写全五个测试，风格同上两文件；decrypt 桩注入点是 `worker._make_decrypt`。）
Run: FAIL。

- [ ] **Step 4: 实现 worker.py**

要点（完整实现按此骨架）：

```python
# backend/tee_replicator/worker.py 骨架
TABLES = {
    "chat_messages": ("SELECT user_id, msg_id, ts, doc FROM chat_messages "
                      "WHERE (ts, msg_id) > (%s, %s) ORDER BY ts, msg_id LIMIT %s",
                      transforms.plaintext_chat_doc,
                      "INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,%s,%s,%s) "
                      "ON CONFLICT (user_id, msg_id) DO UPDATE SET ts=EXCLUDED.ts, doc=EXCLUDED.doc"),
    "memory_moments": (...同型...),
    "world_book_entries": (...同型...),
}

def _make_decrypt(user_id: str):
    from core import enclave as core_enclave
    from core import runtime_token
    token = runtime_token.mint(user_id)   # 实名以 core/runtime_token.py 为准（supervisor.py 用法照抄）
    def decrypt(envelope: dict, purpose: str) -> bytes:
        pt, err = core_enclave._decrypt_envelope_via_enclave(
            envelope, api_key=None, runtime_token=token, purpose=purpose)
        if err:
            raise RuntimeError(f"enclave decrypt failed: {err}")
        return pt
    return decrypt

def run_table(table, *, qps=2.0, dry_run=False, limit=None) -> dict:
    # 读游标 → 循环: RDS 批读(500) → 逐行 transform(捕 PendingDeviceMigration→记 pending 表)
    # → TEE upsert(事务/批) → 推游标（每批持久化）→ sleep(len(batch)/qps)
    # 返回 {"table","copied","pending","errors","watermark_ts","watermark_id"}
```

失败行为：单行 decrypt 失败重试 2 次后记 `errors` + 落 `user_logs`（复用 `db.log_append` 风格）并**跳过不推该行之前的游标**（游标推进到失败行前一行，保证重启重试）。
Run: worker 测试 → PASS；`pytest tests/ -x -q` 零回归。

- [ ] **Step 5: identity envelope 复制**

`user_blobs` kind=identity 的 envelope 走 `identity_service._load_identity` 同款字段（rewrap 已有先例，`content/content_core.py:451`），transform 同 memory；TEE 侧写回 `user_blobs` 的明文 doc。作为 `run_table("identity")` 特例实现 + 一个测试。

- [ ] **Step 6: Commit（须授权）**

```bash
git add backend/tee_replicator/ tests/test_tee_replicator_*.py
git commit -m "feat(tee-replicator): cursor-based decrypt replication (chat/memory/worldbook/identity)"
```

---

### Task 6: frames 复制（inline + R2 双形态）+ 存储层加密

**Files:**
- Create: `backend/tee_replicator/frames.py`
- Modify: `backend/enclave/routes/`（新端点 `POST /v1/storage/reencrypt-frame`）
- Test: `tests/test_tee_replicator_frames.py`

**Interfaces:**
- Consumes: `db.frame_*`、`object_storage`（现有 R2 客户端）、enclave 新端点。
- Produces: `frames.replicate(user_id, frame_id, row) -> dict`（TEE `frames` 行 + R2 新前缀 `frames-tee/<user>/<frame>` 密文对象）；enclave 端点契约：入参 `{envelope 或 env_meta+body_ct, key_version}` → 出参 `{body_ct_storage, key_version, sha256, size}`。

- [ ] **Step 1: 先清点 inline 存量（决定测试样本策略）**

Run: `psql "$TEST_DATABASE_URL" -c "SELECT count(*) FILTER (WHERE doc IS NOT NULL) AS inline, count(*) FILTER (WHERE body_key IS NOT NULL) AS r2 FROM frame_envelopes;"`
`backfill_frames_to_r2.py` 大概率已跑过 → inline≈0 时测试用手工构造的 legacy 样本（spec §4【补充】）。

- [ ] **Step 2: enclave 存储重加密端点（D4）**

enclave 内新增：解开 v1 envelope（复用现有 decrypt 路径）→ 用 KMS 派生的存储对称钥（`derive_key(purpose="frame-storage-v1")`，派生方式照 enclave 现有 content_sk 派生同族）AES-GCM 加密 → 返回密文+版本号。**明文不出 enclave**。测试：enclave 单测（现有 enclave 测试文件同风格）验证 roundtrip 与 key_version 标签。

- [ ] **Step 3: frames.replicate 失败测试**

用例：① R2-backed 行（`env_meta`+`body_key`）：从 R2 拉 ct→enclave 重加密→写新前缀→TEE `frames` 行字段齐全（`body_storage_key/…_version/mime/sha256/size`）；② inline legacy 行（`doc.body_ct`）同结果；③ local_only → pending 表；④ 幂等重放不产生重复对象。R2 与 enclave 均打桩。
Run: FAIL → 实现 → PASS。

- [ ] **Step 4: 接入 worker 游标框架**

`run_table("frame_envelopes")` 委托 `frames.replicate`；photos 天然包含（同表）。全量基线跑绿。

- [ ] **Step 5: Commit（须授权）**

```bash
git add backend/tee_replicator/frames.py backend/enclave/ tests/test_tee_replicator_frames.py
git commit -m "feat(tee-replicator): frame dual-form replication + enclave storage re-encryption"
```

---

### Task 7: 一致性验证 job

**Files:**
- Create: `backend/tee_shadow/verify.py`
- Test: `tests/test_tee_verify.py`

**Interfaces:**
- Consumes: 两池 + `transforms`（同一 decrypt 注入）。
- Produces: `verify.run(*, sample_rate: float = 0.02) -> dict`：per-table per-user 行数对比 + 按比例抽样（RDS 行经 enclave 解密后与 TEE 行逐字段比对，覆盖 chat 主/thinking/caption、memory、world_book、frames meta 五种形态）；输出 `{"tables": {...}, "mismatches": [...], "ok": bool}`。CLI `python -m backend.tee_shadow verify`。这是**停 RDS gate 的硬条件**报告。

- [ ] **Step 1: 失败测试**：① 两库一致 → `ok=True` 零 mismatch；② 人为改 TEE 一行 body → mismatch 定位到 (table,user,id,field)；③ 行数差 → tables 报告差值。decrypt 打桩。
- [ ] **Step 2: 实现**（行数：`GROUP BY user_id` 两侧对比；抽样：`TABLESAMPLE`/`ORDER BY random() LIMIT`，`sample_rate` 控制）。
- [ ] **Step 3: 测试通过 + 全量基线。**
- [ ] **Step 4: Commit（须授权）**

```bash
git add backend/tee_shadow/verify.py tests/test_tee_verify.py
git commit -m "feat(tee-shadow): consistency verification job (counts + sampled field compare)"
```

---

### Task 8: 执行防护（admin 触发 + CI workflow）

**Files:**
- Create: `backend/admin/tee_replication.py`（挂进 `backend/admin/routes_asgi.py`，风格照现有 admin 路由）
- Create: `.github/workflows/tee-replicate.yml`
- Test: `tests/test_admin_tee_replication.py`

**Interfaces:**
- Consumes: Task 4/5/6/7 的 CLI 级函数。
- Produces: `POST /v1/admin/tee-replication/run`（body `{"action": "reconcile"|"replicate"|"verify", "table": str|null, "dry_run": bool, "confirm": str}`，非 dry_run 必须 `confirm=="MIGRATE"` 否则 400；沿用现有 admin 认证）+ `GET /v1/admin/tee-replication/status`（游标/pending/failure 计数）。workflow 为手动 dispatch 包装（默认 `dry_run=true`、字面 `MIGRATE` 确认、`concurrency: tee-replication` 与部署组分离——spec §5 执行防护四要素）。

- [ ] **Step 1: 失败测试**：① 无 confirm 的非 dry_run → 400；② dry_run 返回 plan 且零写入；③ status 返回游标形状。（`client` fixture 走 HTTP。）
- [ ] **Step 2: 实现 admin 路由 + workflow**（workflow 用 curl 打 admin 端点，secrets 存 admin key；replicator 在 CVM 内跑，CI 只当遥控器）。
- [ ] **Step 3: 测试通过 + 全量基线。**
- [ ] **Step 4: Commit（须授权）**

```bash
git add backend/admin/ .github/workflows/tee-replicate.yml tests/test_admin_tee_replication.py
git commit -m "feat(tee-replicator): guarded admin trigger + manual CI workflow"
```

---

### Task 9: test 环境端到端验收（Phase 2+3 收口）

**Files:**
- Modify: `docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md`（验收结果回填）

- [ ] **Step 1: test CVM 开双写**（`FEEDLING_TEE_DUAL_WRITE=1` + `TEE_DATABASE_URL` 加进 ci.yml test 部署 secrets → 部署主 app + 两 runner）
- [ ] **Step 2: 首轮 reconcile**（workflow: action=reconcile, MIGRATE）→ 报告 13 表 `rds_rows == tee_rows`
- [ ] **Step 3: 存量解密复制**（action=replicate，先 dry_run 看 plan，再 MIGRATE，QPS 低速，盯 enclave 无 502）→ 增量追平（游标滞后 < 1min）
- [ ] **Step 4: 断连演练**：临时改坏 TEE 连接 30min → 恢复 → reconcile 拉平（spec §5.1 验收原文）
- [ ] **Step 5: verify 全绿** + 抽样覆盖五种密文形态（spec §7 Phase 3 验收）
- [ ] **Step 6: 关开关回归**：`FEEDLING_TEE_DUAL_WRITE=0` 部署一轮，全功能回归无差异 → 重新开启
- [ ] **Step 7: 结果回填 spec + Commit（须授权）**

---

## Self-Review 记录

- Spec 覆盖：§4 明文 schema→Task 1（凭证例外：`user_blobs` doc 原样镜像即保留 envelope，Task 3 天然满足）；§5.1 双写+补偿→Task 2/3/4；§5.2 解密复制→Task 5/6（含 local_only pending、三套 chat envelope、frames 双形态、限速断点续传）；§5 执行防护→Task 8；一致性验证→Task 7；Phase 2/3 验收→Task 9。
- 有意偏离 spec 待用户确认一处：`genesis_import_chunks` 不复制（Task 5 范围决策，staging 数据 + 冻结窗口）。
- 已知精确名留白两处（实施第一步解决，非 placeholder）：`core.runtime_token` 的铸造函数名（照 supervisor.py:1141 用法）；reconciler TABLES 列清单逐表对照真实 DDL（测试会逼出）。
- 类型一致性：`mirror.execute(sql, params)` 贯穿 Task 2/3/4；`decrypt(envelope, purpose) -> bytes` 贯穿 Task 5/6/7；游标 `(watermark_ts, watermark_id)` 贯穿 Task 5/6/8。
