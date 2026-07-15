# Hosted Runtime V2 — 子项目 B（DB job + 有界 worker 骨架）Implementation Plan

> **STATUS: HISTORICAL IMPLEMENTATION RECORD.** The durable job/worker
> foundation remains current, but the `agent_action_queue` planner pipeline and
> its Python CRUD surface were superseded by provider-native tool calls plus the
> generation-fenced effect outbox. Do not treat every API shown below as live.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 Hosted Runtime V2 的 DB-job 管道地基：4 张专用 Alembic 表、`jobs_store`（CRUD + `SKIP LOCKED` claim + per-user single-flight + reaper + status 事件 + runtime_state + action-queue CRUD）、独立 worker 进程骨架，以及 `chat/send → job → worker claim → 单次解密 provider key → 最小 responder → 落加密 assistant 回复 → mark_completed` 的最小端到端闭环。

**Architecture:** worker 是 **Postgres 队列 consumer**（非 HTTP 服务），跑在与 backend 同一镜像的兄弟进程 `serve_worker.py` 里，用 `FOR UPDATE SKIP LOCKED` 从 `agent_jobs` 抢活。worker 在 `model_api_runtime/v2/`（依赖图里位于 `hosted/` **之下**、`core/` 与 `provider_client` **之上**），所以它**不能** import `hosted.*`；一切需要 `hosted/` 的接线（provider-key 解密、config gating）都由 `serve_worker.py`（装配入口，可 import 上层）**注入**下来——与 `asgi_app` 给 `core.envelope.get_user_public_key` 注入实现同构。turn 执行体全部走**注入式依赖**（`TurnDeps`），测试注入假实现，绝不碰真 enclave / provider。

**Tech Stack:** Python 3.11 / asyncio（`asyncio.to_thread` 把同步 DB/enclave 调用移出事件循环，独立进程不复用 anyio 限流器）、psycopg 3 + `psycopg_pool`（`db.get_pool()`，autocommit 连接 + 显式 `conn.transaction()` 持锁）、Alembic（`backend/alembic/versions/`）、Postgres LISTEN/NOTIFY（`core.wake_bus`）、`provider_client.reliable_chat_completion`、pytest（仓库根 `tests/`，`make_client` / 直接 `db.get_pool()`）。

## Global Constraints

逐字抄自 spec 的硬约束，每个 Task 的要求都隐含本节：

- **BYOK-only，无平台级 LLM key 兜底。**（spec §7.3 不变量）「API-key 用户回合内**所有** LLM 调用一律用该用户自己的 provider key」。B 的 responder 只用 `_load_runtime_provider_config` JIT 解密出的**用户 BYOK** ProviderConfig，代码里不存在任何平台 key 路径。
- **no-filler 铁律**（spec §7.5）：只有 model-authored 文本才写聊天气泡（`role="openclaw"`）。worker 绝不自造 assistant 文本；provider 返回空回复 → job `failed`，**不**写占位气泡。
- **服务器永不解密持久态**：加密不变（spec §5）。用户消息/回复都是 `body_ct/nonce/K_user/K_enclave` 信封，verbatim 存 `chat_messages`。worker 读用户消息明文只能经 **enclave** 解密（enclave-bound，受 `ENCLAVE_SEMAPHORE` 框住，见 R3），provider-key 每 job **单次**解密后只留 worker 内存、**绝不落库**（spec §6）。
- **测试放仓库根 `tests/`，绝不放 `backend/`**（CLAUDE.md / CONTRIBUTING §测试）。DB-backed 测试文件头加 `sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))`（照 `tests/test_agent_runtime_supervisor.py`）。
- **单文件红线**：单模块超 **800 行**须在 PR 说明为何不拆；超 **1500 行**直接拆（CONTRIBUTING）。本 plan 的模块都远低于 800 行。
- **导入约定**（CONTRIBUTING §import）：一律 `from pkg import module` + `module.func()`，**禁止** `from module import func` 拿裸函数。例外：类/常量的类型注解用途可直接 import（如 `from core.store import UserStore`、`from provider_client import ProviderConfig`）。
- **依赖只能向下，向上要用注入**（CONTRIBUTING §2）。`model_api_runtime/v2` 在 `hosted/` 之下：**不许 import `hosted.*`**。新表存取逻辑全部进 `jobs_store`；路由体委托 core 经 `await threadpool.run_db(...)`（web 层）或 worker 里的 `asyncio.to_thread(...)`（独立进程）。

---

## File Structure

```
backend/
├── alembic/versions/0014_hosted_runtime_v2.py   ← 【新】4 张表 + 索引 + FK
├── model_api_runtime/v2/                          ← 【新】V2 运行时（本 plan 只做 B 的子集）
│   ├── __init__.py
│   ├── jobs_store.py     agent_jobs / agent_action_queue / agent_status_events / runtime_state 的 CRUD + claim
│   ├── responder.py      最小 model-authored responder（provider_client.reliable_chat_completion）
│   ├── worker.py         TurnDeps + run_one_turn + run_worker_loop + 三个有界闸常量/信号量
│   └── serve_worker.py   进程入口 + 生产依赖装配（可 import hosted/core）+ 薄 /healthz
├── hosted/config_store.py    ← 改：加 hosted_runtime_mode 读写
└── hosted/chat_send_core.py  ← 改：db_action_v2 模式下 enqueue_job 而非 handle_send
tests/
├── test_v2_jobs_migration.py     Task 1
├── test_v2_jobs_store.py         Tasks 2–4
├── test_v2_responder.py          Task 5
├── test_v2_worker.py             Task 6
├── test_v2_serve_worker.py       Task 7
├── test_hosted_runtime_mode.py   Task 8
└── test_chat_send_v2_enqueue.py  Task 9
```

C（planner / executor / action-queue-drain / replan / coalesce / status 推送）**不在本 plan**。B 建好 action-queue 表 + 基本 CRUD 供 C 重度消费，但 B 的最小闭环**无 planner**、**不排空 action queue**、直接调 responder。

---

## Interfaces（供 C 对齐 —— B 产出的精确签名）

`backend/model_api_runtime/v2/jobs_store.py`（模块级）：

```python
LANES = {"chat", "manual_wake", "heartbeat", "scheduled", "capture", "maintenance"}
RUNNING_TTL_SEC = 120.0   # mark_running 时若 deadline_at 为空则补一个，供 reaper 兜底

# jobs
def enqueue_job(user_id, lane, *, reason=None, trace_id=None, priority=0, deadline_at=None) -> tuple[int, bool]
def claim_next_job(worker_id: str) -> dict | None
def mark_running(job_id) -> None
def mark_completed(job_id) -> None
def mark_failed(job_id, error: str) -> None      # attempt_count += 1
def mark_expired(job_id) -> None
def reap_stuck_jobs(now=None) -> int             # claimed/running 且 deadline_at<=now → expired
# status events
def append_status_event(user_id, kind, *, job_id=None, label=None, detail=None, seq=0) -> int
def list_status_events(user_id, *, after_id=0, limit=50) -> list[dict]
# runtime state
def get_runtime_state(user_id) -> dict
def upsert_runtime_state(user_id, patch: dict) -> dict
# action queue（B 建表 + CRUD，C 重度使用）
def add_actions(job_id, user_id, actions: list[dict]) -> list[int]
def next_pending_action(job_id) -> dict | None
def mark_action_running(action_id) -> None
def mark_action_done(action_id, result: dict) -> None
def mark_action_failed(action_id, error: str) -> None
def mark_action_skipped(action_id) -> None
def invalidate_pending_actions(job_id, *, by_job_id: int) -> int
```

`backend/model_api_runtime/v2/responder.py`：

```python
class ResponderError(Exception): ...
def respond(*, provider_config, coalesced_messages: list[dict], runtime_state: dict) -> str
```

> **⚠️ 对 spec 给定 responder 签名的裁决点（须知会 C）**：任务书给的是
> `respond(store, *, api_key, runtime_token, coalesced_messages, runtime_state)`——即 responder 自己解密。
> 但 `_load_runtime_provider_config`（解密 provider key）住在 `hosted/config_store.py`，而
> `model_api_runtime/v2` 在 `hosted/` **之下**，responder import 它会**逆依赖方向**（CONTRIBUTING §2 红线）。
> 因此本 plan 把「单次解密/resolve」上移到 worker 的**注入式** `TurnDeps.resolve_provider`（生产实现在
> `serve_worker.py`，那里可 import `hosted`），responder 只收已解出的 `ProviderConfig`。契约本质不变
> （用**用户 BYOK** key 出 model-authored 文本、内部 `reliable_chat_completion`），只是解密点从 responder
> 挪到 worker。**C 扩展 responder 时按 `respond(*, provider_config, coalesced_messages, runtime_state)` 对齐。**

`backend/model_api_runtime/v2/worker.py`：

```python
MAX_WORKERS: int                 # 每进程并发 job 数（= 并发回合数）
MAX_READ_ACTION_PARALLELISM: int # 单 job 内 executor 并行读上限（C 用）
ENCLAVE_SEMAPHORE: asyncio.Semaphore  # 跨所有 job 共享，框住所有 enclave-bound 调用（R3）

@dataclass
class TurnDeps:
    read_messages: Callable[[str], list[dict]]        # user_id -> [{"role","content"}]（enclave 解密）
    resolve_provider: Callable[[str], tuple[Any, dict]]  # user_id -> (ProviderConfig|None, meta)
    respond: Callable[[Any, list[dict], dict], str]   # (provider_config, messages, runtime_state) -> text
    append_reply: Callable[[str, str], dict]          # (user_id, text) -> chat row（加密落库）

async def run_one_turn(job: dict, deps: TurnDeps) -> str          # 返回 "completed" | "failed"
async def run_worker_loop(worker_id, *, max_workers, poll_interval, stop_event, deps) -> None
```

`backend/hosted/config_store.py`（新增，本模块内既有 `_load_model_api_runtime_profile`/`_patch_model_api_runtime_profile`）：

```python
HOSTED_RUNTIME_MODE_RESIDENT = "resident_cli"
HOSTED_RUNTIME_MODE_DB_ACTION_V2 = "db_action_v2"
def get_hosted_runtime_mode(store) -> str          # 默认 resident_cli
def set_hosted_runtime_mode(store, mode: str) -> str
```

**消费的 Plan A 接口（假定存在，B 不实现）**：`backend/capabilities/types.py` 的 `CapabilityResult`。B 的最小 responder 只用 `provider_client`，**用不到** capabilities；本 plan 不 import 它。

---

### Task 1: Alembic 0014 —— 4 张 V2 表

**Files:**
- Create: `backend/alembic/versions/0014_hosted_runtime_v2.py`
- Test: `tests/test_v2_jobs_migration.py`

**Interfaces:**
- Consumes: `db.init_schema()`（`conftest.py` 在 session 启动时已 `alembic upgrade head`，把 0014 建好）。
- Produces: 表 `agent_jobs` / `agent_action_queue` / `agent_status_events` / `runtime_state`；唯一 single-flight 索引 `ux_agent_jobs_singleflight`；claim 索引 `ix_agent_jobs_claim`。

> 说明：schema 相较 spec §5 增补两点并在此声明——(1) `agent_jobs` 加 `claimed_by TEXT`（`claim_next_job(worker_id)` 需落 owner，spec SQL 漏了列）；(2) `agent_jobs`/`agent_status_events`/`runtime_state` 的 `user_id` 加 `REFERENCES users(user_id) ON DELETE CASCADE`，对齐 0011/0012 的「删账号 cascade 清 per-user 表」不变量（MEMORY: reset 必须删净 per-user 表）。`agent_action_queue` 经 `job_id → agent_jobs ON DELETE CASCADE` 间接级联，无需自带 FK。

- [ ] **Step 1: 写迁移落地测试（失败）**

`tests/test_v2_jobs_migration.py`：

```python
"""0014 迁移落地：四张 V2 表 + single-flight 唯一索引真的存在且生效。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
import psycopg


def _seed_user(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )


def test_v2_tables_exist():
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN "
            "('agent_jobs','agent_action_queue','agent_status_events','runtime_state')"
        ).fetchall()
    names = {r[0] for r in rows}
    assert names == {"agent_jobs", "agent_action_queue", "agent_status_events", "runtime_state"}


def test_singleflight_unique_index_enforced():
    _seed_user("u_mig_1")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status) VALUES ('u_mig_1','chat','pending')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO agent_jobs (user_id, lane, status) VALUES ('u_mig_1','chat','pending')"
            )
    # cleanup so the shared session DB stays clean for later modules
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id='u_mig_1'")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_v2_jobs_migration.py -v`
Expected: FAIL —— `agent_jobs` 等表不存在（`UndefinedTable`），或 `test_singleflight...` 因表缺失报错。

- [ ] **Step 3: 写迁移**

`backend/alembic/versions/0014_hosted_runtime_v2.py`：

```python
"""hosted runtime v2: durable jobs + action queue + status events + runtime_state.

DB-backed 工作队列地基（子项目 B）。agent_jobs 支持 FOR UPDATE SKIP LOCKED claim
+ per-user/lane single-flight 唯一索引（coalesce 的强制约束）。加密不变：canonical
长期态仍是加密 chat_messages/memory；runtime_state 只存非敏感 digest。

Revision ID: 0014_hosted_runtime_v2
"""
from alembic import op

revision = "0014_hosted_runtime_v2"
down_revision = "0013_genesis_resident_claim"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS agent_jobs (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  lane TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT,
  trace_id TEXT,
  priority INT NOT NULL DEFAULT 0,
  attempt_count INT NOT NULL DEFAULT 0,
  last_error TEXT,
  claimed_by TEXT,
  invalidated_by_job_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  deadline_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_jobs_singleflight
  ON agent_jobs(user_id, lane) WHERE status IN ('pending','claimed','running');
CREATE INDEX IF NOT EXISTS ix_agent_jobs_claim
  ON agent_jobs(status, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS agent_action_queue (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  seq INT NOT NULL,
  type TEXT NOT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  visible BOOL NOT NULL DEFAULT false,
  requires_model_authorship BOOL NOT NULL DEFAULT false,
  result_json JSONB,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_action_queue_job ON agent_action_queue(job_id, seq);

CREATE TABLE IF NOT EXISTS agent_status_events (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  label TEXT,
  detail_json JSONB NOT NULL DEFAULT '{}',
  seq INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_status_events_user
  ON agent_status_events(user_id, id DESC);

CREATE TABLE IF NOT EXISTS runtime_state (
  user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  state_json JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DOWN = """
DROP TABLE IF EXISTS agent_status_events;
DROP TABLE IF EXISTS agent_action_queue;
DROP TABLE IF EXISTS runtime_state;
DROP TABLE IF EXISTS agent_jobs;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
```

> `ix_status_events_user` 用 `(user_id, id DESC)`（而非 spec 的 `created_at DESC`）：long-poll 游标读用 `id > after_id`（Task 3），`id` 单调且唯一，比 `created_at` 更适合做游标索引。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_v2_jobs_migration.py -v`
Expected: PASS（2 passed）。若 `test_singleflight...` 因外键失败，确认 `_seed_user` 先插了 `users` 行。

- [ ] **Step 5: commit**

```bash
git add backend/alembic/versions/0014_hosted_runtime_v2.py tests/test_v2_jobs_migration.py
git commit -m "feat(runtime-v2): alembic 0014 — agent_jobs/action_queue/status_events/runtime_state"
```

---

### Task 2: jobs_store —— enqueue（single-flight）+ claim（SKIP LOCKED）+ mark_*

**Files:**
- Create: `backend/model_api_runtime/v2/__init__.py`, `backend/model_api_runtime/v2/jobs_store.py`
- Test: `tests/test_v2_jobs_store.py`

**Interfaces:**
- Consumes: `db.get_pool()`（psycopg pool，autocommit 连接；持锁用 `conn.transaction()`，dict 行用 `psycopg.rows.dict_row` 游标）。
- Produces: `LANES`、`RUNNING_TTL_SEC`、`enqueue_job`、`claim_next_job`、`mark_running`、`mark_completed`、`mark_failed`、`mark_expired`（签名见顶部 Interfaces 块）。

- [ ] **Step 1: 写失败测试（enqueue coalesce + claim 独占 + 生命周期）**

`tests/test_v2_jobs_store.py`：

```python
"""jobs_store：single-flight coalesce、SKIP LOCKED 独占 claim、job 生命周期。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store


def _seed_user(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))


def test_enqueue_returns_job_id_and_not_coalesced_first_time():
    _seed_user("u_js_1"); _reset("u_js_1")
    job_id, coalesced = jobs_store.enqueue_job("u_js_1", "chat", reason="hi")
    assert isinstance(job_id, int) and job_id > 0
    assert coalesced is False


def test_enqueue_same_user_lane_coalesces_to_existing_pending():
    _seed_user("u_js_2"); _reset("u_js_2")
    first_id, first_c = jobs_store.enqueue_job("u_js_2", "chat")
    second_id, second_c = jobs_store.enqueue_job("u_js_2", "chat")
    assert second_id == first_id
    assert first_c is False and second_c is True


def test_enqueue_rejects_unknown_lane():
    import pytest
    with pytest.raises(ValueError):
        jobs_store.enqueue_job("u_js_2", "not_a_lane")


def test_claim_moves_pending_to_claimed_and_returns_row():
    _seed_user("u_js_3"); _reset("u_js_3")
    job_id, _ = jobs_store.enqueue_job("u_js_3", "chat", trace_id="t1")
    row = jobs_store.claim_next_job("worker-A")
    assert row is not None
    assert row["id"] == job_id
    assert row["status"] == "claimed"
    assert row["claimed_by"] == "worker-A"
    assert row["trace_id"] == "t1"


def test_claim_is_exclusive_second_claim_skips():
    # single-flight means at most one active job per (user, lane); after one claim
    # of the only pending job, a second claim finds nothing.
    _seed_user("u_js_4"); _reset("u_js_4")
    jobs_store.enqueue_job("u_js_4", "chat")
    first = jobs_store.claim_next_job("w1")
    second = jobs_store.claim_next_job("w2")
    assert first is not None
    assert second is None


def test_lifecycle_running_completed_frees_singleflight_slot():
    _seed_user("u_js_5"); _reset("u_js_5")
    job_id, _ = jobs_store.enqueue_job("u_js_5", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id)
    jobs_store.mark_completed(job_id)
    # completed is terminal → the partial unique index no longer covers it →
    # a new job can be enqueued fresh (not coalesced).
    new_id, coalesced = jobs_store.enqueue_job("u_js_5", "chat")
    assert new_id != job_id
    assert coalesced is False


def test_mark_failed_increments_attempt_count():
    _seed_user("u_js_6"); _reset("u_js_6")
    job_id, _ = jobs_store.enqueue_job("u_js_6", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_failed(job_id, "boom")
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, attempt_count, last_error FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
    assert row[0] == "failed"
    assert row[1] == 1
    assert row[2] == "boom"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_v2_jobs_store.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'model_api_runtime.v2'`。

- [ ] **Step 3: 写 `__init__.py` + jobs_store（本 task 的部分）**

`backend/model_api_runtime/v2/__init__.py`：

```python
"""Hosted Runtime V2 — DB job + bounded worker (子项目 B)。"""
```

`backend/model_api_runtime/v2/jobs_store.py`（本 task 先写到 `mark_expired` 为止；Task 3/4 往同文件追加）：

```python
"""DB 存取：agent_jobs / agent_action_queue / agent_status_events / runtime_state。

CONTRIBUTING §2：新表存取逻辑全部收进本模块（jobs_store）。连接走 db.get_pool()
（autocommit）；需要跨语句持行锁的地方（SKIP LOCKED claim / single-flight 选举）
用显式 conn.transaction()。行返回 dict 用 psycopg.rows.dict_row 游标。
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import db

LANES = {"chat", "manual_wake", "heartbeat", "scheduled", "capture", "maintenance"}
# mark_running 时若 job 无 deadline_at，补一个（now + 该秒数），供 reaper 兜底回收
# 卡死的 claimed/running job。chat lane 的 enqueue 不带 deadline，全靠这个兜底。
RUNNING_TTL_SEC = 120.0

_ACTIVE_STATUSES = ("pending", "claimed", "running")


def _pool():
    return db.get_pool()


def enqueue_job(
    user_id, lane, *, reason=None, trace_id=None, priority=0, deadline_at=None
) -> tuple[int, bool]:
    """入队一个 job。命中 per-user/lane single-flight（已有 active job）则合并到现有
    pending，返回 (existing_id, True)；否则新建，返回 (new_id, False)。

    实现：事务内先 SELECT ... FOR UPDATE 现有 active job；无则 INSERT。两个并发 enqueue
    可能都读不到现有行而各自 INSERT → 第二个撞 ux_agent_jobs_singleflight 唯一索引抛
    UniqueViolation → 重试一轮即读到赢家并 coalesce。唯一索引是最终防线。
    """
    if lane not in LANES:
        raise ValueError(f"unknown lane: {lane!r}")
    for _ in range(3):
        try:
            with _pool().connection() as conn:
                with conn.transaction():
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute(
                            "SELECT id FROM agent_jobs "
                            "WHERE user_id=%s AND lane=%s AND status IN ('pending','claimed','running') "
                            "ORDER BY id LIMIT 1 FOR UPDATE",
                            (user_id, lane),
                        )
                        existing = cur.fetchone()
                        if existing is not None:
                            return int(existing["id"]), True
                        cur.execute(
                            "INSERT INTO agent_jobs "
                            "(user_id, lane, status, reason, trace_id, priority, deadline_at) "
                            "VALUES (%s,%s,'pending',%s,%s,%s,%s) RETURNING id",
                            (user_id, lane, reason, trace_id, int(priority), deadline_at),
                        )
                        return int(cur.fetchone()["id"]), False
        except psycopg.errors.UniqueViolation:
            continue  # 并发 racer 抢先建了 active job；重读并 coalesce
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id FROM agent_jobs "
                "WHERE user_id=%s AND lane=%s AND status IN ('pending','claimed','running') "
                "ORDER BY id LIMIT 1",
                (user_id, lane),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("enqueue_job: coalesce read found no active job after conflict")
    return int(row["id"]), True


def claim_next_job(worker_id: str) -> dict | None:
    """抢下一个 pending job（priority DESC, created_at）。用 FOR UPDATE SKIP LOCKED 让
    多进程/多 slot 无争用地各抢各的。pending → claimed，落 claimed_by/claimed_at。
    返回整行 dict（含 id/user_id/lane/trace_id/...），无活可抢返回 None。"""
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT id FROM agent_jobs "
                    "WHERE status='pending' AND (deadline_at IS NULL OR deadline_at > now()) "
                    "ORDER BY priority DESC, created_at "
                    "FOR UPDATE SKIP LOCKED LIMIT 1"
                )
                head = cur.fetchone()
                if head is None:
                    return None
                cur.execute(
                    "UPDATE agent_jobs SET status='claimed', claimed_by=%s, claimed_at=now() "
                    "WHERE id=%s RETURNING *",
                    (worker_id, head["id"]),
                )
                return cur.fetchone()


def mark_running(job_id) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='running', started_at=now(), "
            "deadline_at = COALESCE(deadline_at, now() + make_interval(secs => %s)) "
            "WHERE id=%s",
            (float(RUNNING_TTL_SEC), job_id),
        )


def mark_completed(job_id) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='completed', finished_at=now() WHERE id=%s",
            (job_id,),
        )


def mark_failed(job_id, error: str) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='failed', finished_at=now(), "
            "last_error=%s, attempt_count=attempt_count+1 WHERE id=%s",
            (str(error)[:500], job_id),
        )


def mark_expired(job_id) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='expired', finished_at=now() WHERE id=%s",
            (job_id,),
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_v2_jobs_store.py -v`
Expected: PASS（7 passed）。

- [ ] **Step 5: commit**

```bash
git add backend/model_api_runtime/v2/__init__.py backend/model_api_runtime/v2/jobs_store.py tests/test_v2_jobs_store.py
git commit -m "feat(runtime-v2): jobs_store enqueue(single-flight)+claim(SKIP LOCKED)+lifecycle"
```

---

### Task 3: jobs_store —— reaper + status 事件 + runtime_state

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`（追加函数）
- Test: `tests/test_v2_jobs_store.py`（追加测试）

**Interfaces:**
- Consumes: Task 2 的 `enqueue_job` / `claim_next_job` / `mark_running`。
- Produces: `reap_stuck_jobs(now=None) -> int`、`append_status_event(...) -> int`、`list_status_events(...) -> list[dict]`、`get_runtime_state(user_id) -> dict`、`upsert_runtime_state(user_id, patch) -> dict`。

- [ ] **Step 1: 追加失败测试**

追加到 `tests/test_v2_jobs_store.py` 末尾：

```python
def test_reap_expires_stuck_claimed_job_by_deadline():
    _seed_user("u_js_7"); _reset("u_js_7")
    job_id, _ = jobs_store.enqueue_job("u_js_7", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id)  # stamps deadline_at = now + RUNNING_TTL_SEC
    # reap with a "now" far in the future → deadline is in the past relative to it.
    import time
    reaped = jobs_store.reap_stuck_jobs(now=time.time() + jobs_store.RUNNING_TTL_SEC + 10)
    assert reaped == 1
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    assert row[0] == "expired"


def test_reap_leaves_fresh_running_job_alone():
    _seed_user("u_js_8"); _reset("u_js_8")
    job_id, _ = jobs_store.enqueue_job("u_js_8", "chat")
    jobs_store.claim_next_job("w")
    jobs_store.mark_running(job_id)
    reaped = jobs_store.reap_stuck_jobs()  # now=None → now(); deadline is in the future
    assert reaped == 0


def test_status_events_append_and_list_by_cursor():
    _seed_user("u_js_9")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_status_events WHERE user_id='u_js_9'")
    id1 = jobs_store.append_status_event("u_js_9", "processing", label="starting")
    id2 = jobs_store.append_status_event(
        "u_js_9", "reading_memory", label="读取上下文", detail={"count": 3}
    )
    assert id2 > id1
    events = jobs_store.list_status_events("u_js_9", after_id=id1)
    assert [e["kind"] for e in events] == ["reading_memory"]
    assert events[0]["label"] == "读取上下文"
    assert events[0]["detail_json"] == {"count": 3}
    assert events[0]["id"] == id2


def test_runtime_state_upsert_merges_patch():
    _seed_user("u_js_10")
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM runtime_state WHERE user_id='u_js_10'")
    assert jobs_store.get_runtime_state("u_js_10") == {}
    jobs_store.upsert_runtime_state("u_js_10", {"a": 1})
    merged = jobs_store.upsert_runtime_state("u_js_10", {"b": 2})
    assert merged == {"a": 1, "b": 2}
    assert jobs_store.get_runtime_state("u_js_10") == {"a": 1, "b": 2}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_v2_jobs_store.py -k "reap or status_events or runtime_state" -v`
Expected: FAIL —— `AttributeError: module ... has no attribute 'reap_stuck_jobs'`。

- [ ] **Step 3: 追加实现到 jobs_store.py**

```python
def reap_stuck_jobs(now=None) -> int:
    """把 claimed/running 且已过 deadline_at 的 job 置为 expired（终态，释放 single-flight
    槽位，下一条 chat/send 可重新入队）。now 可注入用于确定性测试（不必真等超时）；
    None → 用 DB now()。返回被回收的行数。重试（re-pending）留给 C 的 replan。"""
    ts = float(now) if now is not None else None
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_jobs SET status='expired', finished_at=now(), "
                "attempt_count=attempt_count+1, "
                "last_error=COALESCE(last_error,'stuck_timeout') "
                "WHERE status IN ('claimed','running') "
                "AND deadline_at IS NOT NULL "
                "AND deadline_at <= COALESCE(to_timestamp(%s), now())",
                (ts,),
            )
            return cur.rowcount


def append_status_event(
    user_id, kind, *, job_id=None, label=None, detail=None, seq=0
) -> int:
    """写一条脱敏 status 事件（非聊天 UX/debug）。detail 只放标签+粗计数，绝无原文。
    返回新事件 id（long-poll 游标用）。"""
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO agent_status_events "
                "(job_id, user_id, kind, label, detail_json, seq) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (job_id, user_id, str(kind), label, Jsonb(dict(detail or {})), int(seq)),
            )
            return int(cur.fetchone()["id"])


def list_status_events(user_id, *, after_id=0, limit=50) -> list[dict]:
    """按 id 升序返回 user 自 after_id 之后的 status 事件（游标读）。每行含
    id/job_id/user_id/kind/label/detail_json/seq/created_at(epoch float)。"""
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, job_id, user_id, kind, label, detail_json, seq, "
                "       extract(epoch FROM created_at) AS created_at "
                "FROM agent_status_events "
                "WHERE user_id=%s AND id > %s ORDER BY id ASC LIMIT %s",
                (user_id, int(after_id), int(limit)),
            )
            return list(cur.fetchall())


def get_runtime_state(user_id) -> dict:
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT state_json FROM runtime_state WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
    return dict(row["state_json"]) if row else {}


def upsert_runtime_state(user_id, patch: dict) -> dict:
    """浅合并 patch 进 state_json（JSONB || 合并），返回合并后的 state。"""
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO runtime_state (user_id, state_json, updated_at) "
                "VALUES (%s,%s,now()) "
                "ON CONFLICT (user_id) DO UPDATE "
                "SET state_json = runtime_state.state_json || EXCLUDED.state_json, "
                "    updated_at = now() "
                "RETURNING state_json",
                (user_id, Jsonb(dict(patch or {}))),
            )
            return dict(cur.fetchone()["state_json"])
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_v2_jobs_store.py -v`
Expected: PASS（11 passed）。

- [ ] **Step 5: commit**

```bash
git add backend/model_api_runtime/v2/jobs_store.py tests/test_v2_jobs_store.py
git commit -m "feat(runtime-v2): jobs_store reaper + status events + runtime_state"
```

---

### Task 4: jobs_store —— action queue CRUD（供 C 消费）

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`（追加函数）
- Test: `tests/test_v2_jobs_store.py`（追加测试）

**Interfaces:**
- Consumes: Task 2 的 `enqueue_job`。
- Produces: `add_actions(job_id, user_id, actions) -> list[int]`、`next_pending_action(job_id) -> dict | None`、`mark_action_running(action_id)`、`mark_action_done(action_id, result)`、`mark_action_failed(action_id, error)`、`mark_action_skipped(action_id)`、`invalidate_pending_actions(job_id, *, by_job_id) -> int`。

- [ ] **Step 1: 追加失败测试**

追加到 `tests/test_v2_jobs_store.py` 末尾：

```python
def test_action_queue_add_and_next_pending_in_seq_order():
    _seed_user("u_aq_1"); _reset("u_aq_1")
    job_id, _ = jobs_store.enqueue_job("u_aq_1", "chat")
    ids = jobs_store.add_actions(job_id, "u_aq_1", [
        {"type": "memory_fetch", "payload": {"ids": ["m1"]}},
        {"type": "final_response", "visible": True, "requires_model_authorship": True},
    ])
    assert len(ids) == 2
    nxt = jobs_store.next_pending_action(job_id)
    assert nxt["type"] == "memory_fetch"
    assert nxt["seq"] == 0
    assert nxt["payload_json"] == {"ids": ["m1"]}


def test_action_lifecycle_done_advances_to_next():
    _seed_user("u_aq_2"); _reset("u_aq_2")
    job_id, _ = jobs_store.enqueue_job("u_aq_2", "chat")
    a1, a2 = jobs_store.add_actions(job_id, "u_aq_2", [
        {"type": "memory_fetch"},
        {"type": "final_response"},
    ])
    jobs_store.mark_action_running(a1)
    jobs_store.mark_action_done(a1, {"cards": 3})
    nxt = jobs_store.next_pending_action(job_id)
    assert nxt["id"] == a2
    assert nxt["type"] == "final_response"
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, result_json FROM agent_action_queue WHERE id=%s", (a1,)
        ).fetchone()
    assert row[0] == "completed"
    assert row[1] == {"cards": 3}


def test_action_failed_and_skipped_are_terminal():
    _seed_user("u_aq_3"); _reset("u_aq_3")
    job_id, _ = jobs_store.enqueue_job("u_aq_3", "chat")
    a1, a2 = jobs_store.add_actions(job_id, "u_aq_3", [{"type": "x"}, {"type": "y"}])
    jobs_store.mark_action_failed(a1, "nope")
    jobs_store.mark_action_skipped(a2)
    assert jobs_store.next_pending_action(job_id) is None
    with db.get_pool().connection() as conn:
        rows = dict(conn.execute(
            "SELECT status, count(*) FROM agent_action_queue WHERE job_id=%s GROUP BY status",
            (job_id,),
        ).fetchall())
    assert rows == {"failed": 1, "skipped": 1}


def test_invalidate_pending_actions_marks_them_and_stamps_job():
    _seed_user("u_aq_4"); _reset("u_aq_4")
    job_id, _ = jobs_store.enqueue_job("u_aq_4", "chat")
    a1, a2 = jobs_store.add_actions(job_id, "u_aq_4", [{"type": "x"}, {"type": "y"}])
    jobs_store.mark_action_running(a1)
    jobs_store.mark_action_done(a1, {})
    n = jobs_store.invalidate_pending_actions(job_id, by_job_id=999)
    assert n == 1  # only the still-pending a2
    with db.get_pool().connection() as conn:
        st = conn.execute("SELECT status FROM agent_action_queue WHERE id=%s", (a2,)).fetchone()[0]
        job = conn.execute(
            "SELECT invalidated_by_job_id FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()[0]
    assert st == "invalidated"
    assert job == 999
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_v2_jobs_store.py -k "action" -v`
Expected: FAIL —— `AttributeError: ... 'add_actions'`。

- [ ] **Step 3: 追加实现到 jobs_store.py**

```python
def add_actions(job_id, user_id, actions: list[dict]) -> list[int]:
    """把一批 action 追加进 job 的队列（seq 接续现有最大 seq）。action 形状：
    {type, payload?, visible?, requires_model_authorship?}。返回新建 action id 列表。"""
    ids: list[int] = []
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(seq), -1) AS m FROM agent_action_queue WHERE job_id=%s",
                    (job_id,),
                )
                start = int(cur.fetchone()["m"]) + 1
                for offset, action in enumerate(actions):
                    cur.execute(
                        "INSERT INTO agent_action_queue "
                        "(job_id, user_id, seq, type, payload_json, visible, requires_model_authorship) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                        (
                            job_id,
                            user_id,
                            start + offset,
                            str(action["type"]),
                            Jsonb(dict(action.get("payload") or {})),
                            bool(action.get("visible", False)),
                            bool(action.get("requires_model_authorship", False)),
                        ),
                    )
                    ids.append(int(cur.fetchone()["id"]))
    return ids


def next_pending_action(job_id) -> dict | None:
    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM agent_action_queue "
                "WHERE job_id=%s AND status='pending' ORDER BY seq ASC LIMIT 1",
                (job_id,),
            )
            return cur.fetchone()


def mark_action_running(action_id) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_action_queue SET status='running', started_at=now() WHERE id=%s",
            (action_id,),
        )


def mark_action_done(action_id, result: dict) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_action_queue SET status='completed', finished_at=now(), "
            "result_json=%s WHERE id=%s",
            (Jsonb(dict(result or {})), action_id),
        )


def mark_action_failed(action_id, error: str) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_action_queue SET status='failed', finished_at=now(), "
            "last_error=%s WHERE id=%s",
            (str(error)[:500], action_id),
        )


def mark_action_skipped(action_id) -> None:
    with _pool().connection() as conn:
        conn.execute(
            "UPDATE agent_action_queue SET status='skipped', finished_at=now() WHERE id=%s",
            (action_id,),
        )


def invalidate_pending_actions(job_id, *, by_job_id: int) -> int:
    """把 job 现有 pending action 置为 invalidated，并在 job 上记 invalidated_by_job_id
    （replan/coalesce 的安全点，C 用）。返回被作废的 pending action 数。"""
    with _pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_action_queue SET status='invalidated', finished_at=now() "
                    "WHERE job_id=%s AND status='pending'",
                    (job_id,),
                )
                affected = cur.rowcount
                cur.execute(
                    "UPDATE agent_jobs SET invalidated_by_job_id=%s WHERE id=%s",
                    (int(by_job_id), job_id),
                )
    return affected
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_v2_jobs_store.py -v`
Expected: PASS（15 passed）。

- [ ] **Step 5: commit**

```bash
git add backend/model_api_runtime/v2/jobs_store.py tests/test_v2_jobs_store.py
git commit -m "feat(runtime-v2): jobs_store action-queue CRUD (for subproject C)"
```

---

### Task 5: responder —— 最小 model-authored responder

**Files:**
- Create: `backend/model_api_runtime/v2/responder.py`
- Test: `tests/test_v2_responder.py`

**Interfaces:**
- Consumes: `provider_client.ProviderConfig`（类型）、`provider_client.reliable_chat_completion(config, messages, *, max_tokens, temperature, timeout, ...)`（返回 `{"reply": str, ...}`）。
- Produces: `ResponderError`、`respond(*, provider_config, coalesced_messages: list[dict], runtime_state: dict) -> str`。

- [ ] **Step 1: 写失败测试（注入替身，不碰真 provider）**

`tests/test_v2_responder.py`：

```python
"""最小 responder：把合并消息交给用户 BYOK provider，返回 model-authored 文本。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client
from model_api_runtime.v2 import responder


def test_respond_returns_provider_reply(monkeypatch):
    seen = {}

    def fake_reliable(config, messages, **kwargs):
        seen["config"] = config
        seen["messages"] = messages
        return {"reply": "  hello from model  ", "usage": {}}

    monkeypatch.setattr(provider_client, "reliable_chat_completion", fake_reliable)
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    out = responder.respond(
        provider_config=cfg,
        coalesced_messages=[{"role": "user", "content": "hi"}],
        runtime_state={},
    )
    assert out == "hello from model"           # 去空白
    assert seen["config"] is cfg               # 用的是传入的 BYOK config
    # 合并的用户消息被带进 provider 请求
    assert {"role": "user", "content": "hi"} in seen["messages"]


def test_respond_raises_on_empty_reply(monkeypatch):
    monkeypatch.setattr(
        provider_client, "reliable_chat_completion",
        lambda config, messages, **kw: {"reply": "   "},
    )
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    with pytest.raises(responder.ResponderError):
        responder.respond(
            provider_config=cfg,
            coalesced_messages=[{"role": "user", "content": "hi"}],
            runtime_state={},
        )


def test_respond_raises_on_no_user_messages():
    cfg = provider_client.ProviderConfig(provider="anthropic", model="m", api_key="k")
    with pytest.raises(responder.ResponderError):
        responder.respond(provider_config=cfg, coalesced_messages=[], runtime_state={})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_v2_responder.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'model_api_runtime.v2.responder'`。

- [ ] **Step 3: 写 responder.py**

```python
"""最小 model-authored responder（子项目 B）。

用**用户自己的 BYOK** ProviderConfig 出最终回复（spec §7.3 不变量：无平台 key 兜底）。
B 无 planner——直接把合并的用户消息交给 provider，取 model-authored 文本。C 会扩展本模块
（引入 planner 产出的上下文、tool 结果注入等），但对外签名保持
`respond(*, provider_config, coalesced_messages, runtime_state) -> str`。

依赖方向：本模块只 import provider_client（底层），不 import hosted.*（在其上层）。
provider-key 的单次解密由 worker 的注入式 resolve_provider 完成（见 worker.py / serve_worker.py）。
"""
from __future__ import annotations

from typing import Any

import provider_client

# 最小系统提示：no-filler 铁律——回复即 model-authored 聊天气泡内容，无占位、无“正在处理”。
_SYSTEM_PROMPT = (
    "You are the user's personal companion. Reply directly and concisely to the "
    "user's latest messages. Do not narrate tool use or system status."
)

_MAX_TOKENS = 700
_TEMPERATURE = 0.7
_TIMEOUT_SEC = 60.0


class ResponderError(Exception):
    """responder 无法产出 model-authored 文本（无用户消息 / provider 空回复 / provider 错）。"""


def _build_messages(coalesced_messages: list[dict], runtime_state: dict) -> list[dict]:
    user_turns = [
        {"role": "user", "content": str(m.get("content") or "")}
        for m in coalesced_messages
        if str(m.get("role") or "") == "user" and str(m.get("content") or "").strip()
    ]
    if not user_turns:
        raise ResponderError("no_user_messages")
    return [{"role": "system", "content": _SYSTEM_PROMPT}, *user_turns]


def respond(*, provider_config: Any, coalesced_messages: list[dict], runtime_state: dict) -> str:
    """出一条 model-authored 回复文本。空回复 / provider 错 → ResponderError（调用方据此
    把 job 标 failed，绝不写占位气泡——no-filler 铁律）。"""
    messages = _build_messages(coalesced_messages, runtime_state)
    try:
        result = provider_client.reliable_chat_completion(
            provider_config,
            messages,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            timeout=_TIMEOUT_SEC,
        )
    except Exception as e:  # noqa: BLE001 — 归一成 ResponderError 交给 worker 落 last_error
        raise ResponderError(f"provider_call_failed: {type(e).__name__}: {str(e)[:200]}") from e
    text = str((result or {}).get("reply") or "").strip()
    if not text:
        raise ResponderError("empty_reply")
    return text
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_v2_responder.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: commit**

```bash
git add backend/model_api_runtime/v2/responder.py tests/test_v2_responder.py
git commit -m "feat(runtime-v2): minimal model-authored responder (BYOK, no-filler)"
```

---

### Task 6: worker —— TurnDeps + run_one_turn + run_worker_loop + 有界闸

**Files:**
- Create: `backend/model_api_runtime/v2/worker.py`
- Test: `tests/test_v2_worker.py`

**Interfaces:**
- Consumes: Task 2/3 的 `jobs_store.claim_next_job` / `mark_running` / `mark_completed` / `mark_failed` / `get_runtime_state`。
- Produces: `MAX_WORKERS`、`MAX_READ_ACTION_PARALLELISM`、`ENCLAVE_SEMAPHORE`、`TurnDeps`、`async run_one_turn(job, deps) -> str`、`async run_worker_loop(worker_id, *, max_workers, poll_interval, stop_event, deps) -> None`。

> 并发模型：独立进程用 asyncio，把同步 DB/enclave 调用经 `asyncio.to_thread` 移出事件循环（不复用 web 层的 anyio 限流器）。`ENCLAVE_SEMAPHORE` 框住 turn 里的 **enclave-bound 段**（`resolve_provider` 解密 + `read_messages` 逐条 enclave 解密），治 R3；provider 的出站 LLM 调用（`respond`）**不**在信号量内——那是打用户自己的 provider，不是 enclave。测试用注入式 `TurnDeps`（照 `test_agent_runtime_supervisor.py` 注入 spawn/alive/kill 的套路），不碰真 enclave/provider。

- [ ] **Step 1: 写失败测试（注入式）**

`tests/test_v2_worker.py`：

```python
"""worker：注入式 TurnDeps 跑 run_one_turn / run_worker_loop 的 claim→turn 编排。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store, worker


def _seed_user(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))


class _FakeProvider:
    api_key = "k"


def _ok_deps(recorder):
    return worker.TurnDeps(
        read_messages=lambda uid: [{"role": "user", "content": "hi"}],
        resolve_provider=lambda uid: (_FakeProvider(), {}),
        respond=lambda cfg, msgs, state: "model reply",
        append_reply=lambda uid, text: recorder.setdefault("replies", []).append((uid, text)) or {"id": "r1"},
    )


def test_run_one_turn_completes_and_appends_reply():
    _seed_user("u_w_1"); _reset("u_w_1")
    job_id, _ = jobs_store.enqueue_job("u_w_1", "chat")
    job = jobs_store.claim_next_job("w")
    rec = {}
    status = asyncio.run(worker.run_one_turn(job, _ok_deps(rec)))
    assert status == "completed"
    assert rec["replies"] == [("u_w_1", "model reply")]
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    assert row[0] == "completed"


def test_run_one_turn_fails_when_provider_unresolved():
    _seed_user("u_w_2"); _reset("u_w_2")
    job_id, _ = jobs_store.enqueue_job("u_w_2", "chat")
    job = jobs_store.claim_next_job("w")
    deps = worker.TurnDeps(
        read_messages=lambda uid: [{"role": "user", "content": "hi"}],
        resolve_provider=lambda uid: (None, {"error": "model_api_key_decrypt_failed"}),
        respond=lambda cfg, msgs, state: "should not be called",
        append_reply=lambda uid, text: {"id": "r"},
    )
    status = asyncio.run(worker.run_one_turn(job, deps))
    assert status == "failed"
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
    assert row[0] == "failed"
    assert "model_api_key_decrypt_failed" in (row[1] or "")


def test_run_one_turn_fails_on_empty_model_reply_no_filler():
    _seed_user("u_w_3"); _reset("u_w_3")
    job_id, _ = jobs_store.enqueue_job("u_w_3", "chat")
    job = jobs_store.claim_next_job("w")
    rec = {}

    def _respond(cfg, msgs, state):
        raise __import__("model_api_runtime.v2.responder", fromlist=["ResponderError"]).ResponderError("empty_reply")

    deps = worker.TurnDeps(
        read_messages=lambda uid: [{"role": "user", "content": "hi"}],
        resolve_provider=lambda uid: (_FakeProvider(), {}),
        respond=_respond,
        append_reply=lambda uid, text: rec.setdefault("replies", []).append(text) or {"id": "r"},
    )
    status = asyncio.run(worker.run_one_turn(job, deps))
    assert status == "failed"
    assert "replies" not in rec  # no-filler：不写占位气泡
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    assert row[0] == "failed"


def test_run_worker_loop_drains_pending_then_stops():
    _seed_user("u_w_4"); _reset("u_w_4")
    jobs_store.enqueue_job("u_w_4", "chat")
    rec = {}
    stop = asyncio.Event()

    async def _driver():
        task = asyncio.create_task(worker.run_worker_loop(
            "w-loop", max_workers=1, poll_interval=0.02, stop_event=stop, deps=_ok_deps(rec),
        ))
        # 等到那条 job 被处理
        for _ in range(200):
            with db.get_pool().connection() as conn:
                st = conn.execute(
                    "SELECT status FROM agent_jobs WHERE user_id='u_w_4'"
                ).fetchone()
            if st and st[0] == "completed":
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_driver())
    assert rec.get("replies") == [("u_w_4", "model reply")]


def test_bounded_gates_exist():
    assert isinstance(worker.MAX_WORKERS, int) and worker.MAX_WORKERS >= 1
    assert isinstance(worker.MAX_READ_ACTION_PARALLELISM, int)
    assert isinstance(worker.ENCLAVE_SEMAPHORE, asyncio.Semaphore)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_v2_worker.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'model_api_runtime.v2.worker'`。

- [ ] **Step 3: 写 worker.py**

```python
"""V2 worker：Postgres 队列 consumer 的编排骨架（子项目 B）。

进程入口在 serve_worker.py；本模块只做「一回合一 worker」的编排 + 三个有界闸。
turn 执行体走注入式 TurnDeps（生产实现由 serve_worker 装配、可 import hosted/core；
测试注入假实现，不碰真 enclave/provider）。

并发：asyncio 事件循环 + asyncio.to_thread 把同步 jobs_store/enclave/provider 调用移出
loop。ENCLAVE_SEMAPHORE 框住 turn 里所有 enclave-bound 调用（provider-key 解密 + 逐条
chat 解密），治 spec R3（enclave 单线程瓶颈，多 worker 齐打会放大 502）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

from model_api_runtime.v2 import jobs_store

log = logging.getLogger("feedling.runtime_v2.worker")

# —— 三个有界闸 ——（spec §6）
# 每进程并发 job 数（= 并发回合数）。线上多进程 × CVM 共抢同一张 agent_jobs → 线性扩容。
MAX_WORKERS = int(os.environ.get("FEEDLING_V2_MAX_WORKERS", "4"))
# 单 job 内 executor 并行读上限（C 的 executor 用；B 无 planner 暂不消费，但常量此处定义）。
MAX_READ_ACTION_PARALLELISM = int(os.environ.get("FEEDLING_V2_MAX_READ_PARALLELISM", "4"))
# 跨所有 job 共享的 enclave 并发闸（provider-key 解密 + chat 解密都过它）。治 R3。
ENCLAVE_SEMAPHORE = asyncio.Semaphore(int(os.environ.get("FEEDLING_V2_ENCLAVE_CONCURRENCY", "2")))


@dataclass
class TurnDeps:
    """turn 执行体的注入式依赖（生产实现见 serve_worker.build_production_deps）。"""
    read_messages: Callable[[str], list[dict]]            # user_id -> [{"role","content"}]（enclave 解密）
    resolve_provider: Callable[[str], tuple[Any, dict]]   # user_id -> (ProviderConfig|None, meta)：单次解密
    respond: Callable[[Any, list[dict], dict], str]       # (provider_config, messages, runtime_state) -> text
    append_reply: Callable[[str, str], dict]              # (user_id, text) -> chat row（加密落库）


async def run_one_turn(job: dict, deps: TurnDeps) -> str:
    """执行一个已 claim 的 job：mark_running → 单次解密 provider key（enclave-bound）→
    读用户消息（enclave 解密）→ 最小 responder 出 model-authored 文本 → 落加密回复 →
    mark_completed。任一步失败 → mark_failed（不写占位气泡）。返回终态字符串。"""
    job_id = job["id"]
    user_id = str(job["user_id"])
    await asyncio.to_thread(jobs_store.mark_running, job_id)
    try:
        # —— enclave-bound 段：provider-key 单次解密 + 读用户消息（逐条 enclave 解密）——
        async with ENCLAVE_SEMAPHORE:
            provider_config, meta = await asyncio.to_thread(deps.resolve_provider, user_id)
            if provider_config is None:
                err = str((meta or {}).get("error") or "provider_unavailable")
                await asyncio.to_thread(jobs_store.mark_failed, job_id, err)
                return "failed"
            messages = await asyncio.to_thread(deps.read_messages, user_id)
        # —— provider 出站 LLM 调用：打用户自己的 provider，非 enclave，不占信号量 ——
        runtime_state = await asyncio.to_thread(jobs_store.get_runtime_state, user_id)
        text = await asyncio.to_thread(deps.respond, provider_config, messages, runtime_state)
        if not str(text or "").strip():
            await asyncio.to_thread(jobs_store.mark_failed, job_id, "empty_reply")
            return "failed"
        await asyncio.to_thread(deps.append_reply, user_id, text)
        await asyncio.to_thread(jobs_store.mark_completed, job_id)
        return "completed"
    except Exception as e:  # noqa: BLE001 — 任何失败落 last_error，绝不写占位气泡
        log.warning("[v2.worker] job %s failed: %s", job_id, e)
        await asyncio.to_thread(jobs_store.mark_failed, job_id, f"{type(e).__name__}: {str(e)[:200]}")
        return "failed"


async def _slot_loop(worker_id: str, *, poll_interval: float, stop_event: asyncio.Event, deps: TurnDeps) -> None:
    """一个 job-slot：抢一个 job 就跑一回合，抢不到就轮询等待。stop_event 置位后不再抢新活，
    跑完手上的即退出（优雅 drain）。"""
    while not stop_event.is_set():
        job = await asyncio.to_thread(jobs_store.claim_next_job, worker_id)
        if job is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
            continue
        await run_one_turn(job, deps)


async def run_worker_loop(
    worker_id: str, *, max_workers: int, poll_interval: float, stop_event: asyncio.Event, deps: TurnDeps
) -> None:
    """起 max_workers 个 job-slot 协程共抢同一张 agent_jobs（SKIP LOCKED 无争用）。
    stop_event 置位 → 所有 slot 跑完手上 job 后退出（SIGTERM 优雅 drain 的落点）。"""
    slots = [
        asyncio.create_task(
            _slot_loop(f"{worker_id}#{i}", poll_interval=poll_interval, stop_event=stop_event, deps=deps)
        )
        for i in range(max(1, int(max_workers)))
    ]
    await asyncio.gather(*slots)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_v2_worker.py -v`
Expected: PASS（5 passed）。

- [ ] **Step 5: commit**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_v2_worker.py
git commit -m "feat(runtime-v2): worker orchestration — run_one_turn + bounded gates + drain"
```

---

### Task 7: serve_worker —— 进程入口 + 生产依赖装配 + /healthz

**Files:**
- Create: `backend/model_api_runtime/v2/serve_worker.py`
- Test: `tests/test_v2_serve_worker.py`

**Interfaces:**
- Consumes: `worker.TurnDeps` / `worker.run_worker_loop`；`hosted.config_store._load_runtime_provider_config`（解密 provider key）；`core.runtime_token.mint`；`core.store.get_store`；`core.envelope._build_shared_envelope_for_store`；`core.enclave._decrypt_envelope_via_enclave`；`accounts.registry`（注入 pubkey getter + load_users）；`db.init_schema`；`core.wake_bus.start_listener`。
- Produces: `build_production_deps() -> worker.TurnDeps`、`wire_assembly() -> None`、`build_health_app()`（薄 FastAPI，`/healthz`）、`main()`（进程入口）。

> serve_worker 是**装配入口**（类比 `asgi_app` / `asgi/lifespan.py`），是**唯一**允许同时 import `hosted.*` + `core.*` + `model_api_runtime.*` 的地方——它把需要上层的实现**注入**进 worker 的 `TurnDeps`，从而让 `worker.py` / `responder.py` 保持不逆依赖。`wire_assembly()` 必须复刻 lifespan 的关键接线：`core_envelope.get_user_public_key = accounts_registry._get_user_public_key`（否则 `_build_shared_envelope_for_store` 抛 RuntimeError stub）、`accounts_registry.load_users()`（否则内存 registry 空、pubkey 查不到）、`core_wake_bus.start_listener()`（cross-worker 唤醒）。

- [ ] **Step 1: 写失败测试（只测可纯跑的装配点，不起真进程/不碰真 enclave）**

`tests/test_v2_serve_worker.py`：

```python
"""serve_worker：生产 TurnDeps 装配的可测部分 + /healthz。不起真 worker/真 enclave。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker, worker


def test_build_production_deps_returns_turndeps():
    deps = serve_worker.build_production_deps()
    assert isinstance(deps, worker.TurnDeps)
    assert callable(deps.read_messages)
    assert callable(deps.resolve_provider)
    assert callable(deps.respond)
    assert callable(deps.append_reply)


def test_wire_assembly_injects_envelope_pubkey_getter():
    from core import envelope as core_envelope
    from accounts import registry as accounts_registry

    serve_worker.wire_assembly()
    assert core_envelope.get_user_public_key is accounts_registry._get_user_public_key


def test_health_app_healthz_ok():
    from starlette.testclient import TestClient

    app = serve_worker.build_health_app()
    with TestClient(app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_v2_serve_worker.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'model_api_runtime.v2.serve_worker'`。

- [ ] **Step 3: 写 serve_worker.py**

```python
"""V2 worker 进程入口 + 生产依赖装配（子项目 B）。

装配层：这里（且只有这里）可同时 import hosted/core/model_api_runtime，把需要上层的实现
注入进 worker.TurnDeps，令 worker.py/responder.py 保持不逆依赖（CONTRIBUTING §2）。

生产 turn 依赖：
- resolve_provider：mint 一个 user-scoped runtime token → hosted.config_store 用它 JIT
  解密 provider key（单次；只留内存，不落库）。enclave-bound（受 worker.ENCLAVE_SEMAPHORE 框住）。
- read_messages：读该用户 chat_messages 中自上一条 assistant 之后的 user 行，逐条经 enclave
  解密取明文（服务器永不本地解密）。enclave-bound。
- respond：v2.responder.respond（用户 BYOK provider 出 model-authored 文本）。
- append_reply：构建加密信封 + append_chat("openclaw","model_api") + notify_chat_waiters。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from accounts import registry as accounts_registry
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import runtime_token
from core import store as core_store
from core import wake_bus as core_wake_bus
from hosted import config_store as hosted_config_store
from model_api_runtime.v2 import responder as v2_responder
from model_api_runtime.v2 import worker as v2_worker
import db

log = logging.getLogger("feedling.runtime_v2.serve_worker")

_ASSISTANT_ROLES = ("openclaw", "assistant", "agent")


def _mint_runtime_token(user_id: str) -> str:
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip().encode("utf-8")
    if not secret:
        raise RuntimeError("FEEDLING_RUNTIME_TOKEN_SECRET not set")
    return runtime_token.mint(
        secret,
        user_id=user_id,
        runtime_instance_id="v2-worker",
        scope=["envelope:decrypt"],
        ttl=900.0,
    )


def _resolve_provider(user_id: str):
    """单次解密该用户 provider key（enclave-bound）。返回 (ProviderConfig|None, meta)。"""
    store = core_store.get_store(user_id)
    try:
        token = _mint_runtime_token(user_id)
    except Exception as e:  # noqa: BLE001
        return None, {"error": "runtime_token_mint_failed", "detail": str(e)[:160]}
    runtime = hosted_config_store._load_runtime_provider_config(store, None, runtime_token=token)
    if isinstance(runtime, tuple):
        return None, runtime[1]
    return runtime, {}


def _read_messages(user_id: str) -> list[dict]:
    """读该用户自上一条 assistant 之后的 user 消息，逐条经 enclave 解密成明文文本。
    服务器永不本地解密——每条 body_ct/nonce/K_user/K_enclave 信封走 enclave /v1/envelope/decrypt。"""
    store = core_store.get_store(user_id)
    rows = list(getattr(store, "chat_messages", []) or [])
    # 找到最后一条 assistant 回复的下标；只回放其后的 user 消息（未答的那批）。
    last_assistant = -1
    for idx, m in enumerate(rows):
        if str(m.get("role") or "") in _ASSISTANT_ROLES:
            last_assistant = idx
    pending = rows[last_assistant + 1:]
    token = _mint_runtime_token(user_id)
    out: list[dict] = []
    for m in pending:
        if str(m.get("role") or "") != "user":
            continue
        if m.get("content_type") == "image":
            out.append({"role": "user", "content": "[image]"})
            continue
        envelope = {
            "body_ct": m.get("body_ct"), "nonce": m.get("nonce"),
            "K_user": m.get("K_user"), "K_enclave": m.get("K_enclave"),
            "owner_user_id": m.get("owner_user_id") or user_id, "v": m.get("v", 1),
        }
        if not envelope["body_ct"] or envelope.get("K_enclave") is None:
            continue  # 无 enclave 钥的合成/本地-only 消息跳过
        plaintext = core_enclave._decrypt_envelope_via_enclave(
            envelope, None, purpose="v2_chat_read", runtime_token=token
        ).decode("utf-8")
        if plaintext.strip():
            out.append({"role": "user", "content": plaintext})
    return out


def _respond(provider_config, messages: list[dict], runtime_state: dict) -> str:
    return v2_responder.respond(
        provider_config=provider_config,
        coalesced_messages=messages,
        runtime_state=runtime_state,
    )


def _append_reply(user_id: str, text: str) -> dict:
    store = core_store.get_store(user_id)
    env, err = core_envelope._build_shared_envelope_for_store(store, text.encode("utf-8"))
    if env is None:
        raise RuntimeError(f"reply_envelope_failed: {err}")
    row = store.append_chat("openclaw", "model_api", env)
    store.notify_chat_waiters()
    return row


def build_production_deps() -> v2_worker.TurnDeps:
    return v2_worker.TurnDeps(
        read_messages=_read_messages,
        resolve_provider=_resolve_provider,
        respond=_respond,
        append_reply=_append_reply,
    )


def wire_assembly() -> None:
    """复刻 asgi/lifespan.py 的关键接线（本进程无 lifespan）：注入 envelope pubkey getter、
    载入内存 registry、起 wake-bus listener。幂等。"""
    core_envelope.get_user_public_key = accounts_registry._get_user_public_key
    accounts_registry.load_users()
    core_wake_bus.register_handler("users", lambda _uid: accounts_registry.load_users())
    core_wake_bus.start_listener()


def build_health_app():
    """极薄 FastAPI，仅暴露 /healthz 供部署平台存活探针（spec §2.1）。"""
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "db": db.healthcheck()}

    return app


async def _serve(worker_id: str, *, poll_interval: float) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    deps = build_production_deps()
    log.info("[v2.serve_worker] starting worker=%s max_workers=%s", worker_id, v2_worker.MAX_WORKERS)
    await v2_worker.run_worker_loop(
        worker_id,
        max_workers=v2_worker.MAX_WORKERS,
        poll_interval=poll_interval,
        stop_event=stop_event,
        deps=deps,
    )
    log.info("[v2.serve_worker] drained; exiting worker=%s", worker_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    wire_assembly()
    worker_id = os.environ.get("FEEDLING_V2_WORKER_ID", f"v2-worker-{os.getpid()}")
    poll_interval = float(os.environ.get("FEEDLING_V2_POLL_INTERVAL_SEC", "1.0"))
    asyncio.run(_serve(worker_id, poll_interval=poll_interval))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_v2_serve_worker.py -v`
Expected: PASS（3 passed）。若 `test_health_app_healthz_ok` 报 starlette 缺失，确认 `backend/requirements` 已含 fastapi/starlette（本仓 ASGI 后端已依赖，测试环境应现成）。

- [ ] **Step 5: commit**

```bash
git add backend/model_api_runtime/v2/serve_worker.py tests/test_v2_serve_worker.py
git commit -m "feat(runtime-v2): serve_worker entrypoint + production TurnDeps + /healthz"
```

---

### Task 8: config_store —— hosted_runtime_mode 读写（灰度开关）

**Files:**
- Modify: `backend/hosted/config_store.py`（追加常量 + 两个函数）
- Test: `tests/test_hosted_runtime_mode.py`

**Interfaces:**
- Consumes: 既有 `config_store._load_model_api_runtime_profile(store)` / `_patch_model_api_runtime_profile(store, patch)`；`core.store.get_store`。
- Produces: `HOSTED_RUNTIME_MODE_RESIDENT = "resident_cli"`、`HOSTED_RUNTIME_MODE_DB_ACTION_V2 = "db_action_v2"`、`get_hosted_runtime_mode(store) -> str`、`set_hosted_runtime_mode(store, mode) -> str`。

> mode 存进既有 `model_api_runtime` profile blob（per-user），复用 `_patch_model_api_runtime_profile`（丢 None、保字符串）。默认 `resident_cli`——resident 路径原样不动，两条并存（spec §10）。

- [ ] **Step 1: 写失败测试**

`tests/test_hosted_runtime_mode.py`：

```python
"""hosted_runtime_mode 灰度开关：默认 resident_cli，可切 db_action_v2，非法值拒绝。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from core import store as core_store
from hosted import config_store as hosted_config_store


def _seed_model_api_user(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
    # 需要一个 model_api 配置，_patch_model_api_runtime_profile 才能建 runtime profile。
    db.set_blob(uid, "model_api", {"route": "model_api", "provider": "anthropic", "model": "m"})


def test_default_mode_is_resident_cli():
    _seed_model_api_user("u_mode_1")
    store = core_store.get_store("u_mode_1")
    assert hosted_config_store.get_hosted_runtime_mode(store) == "resident_cli"


def test_set_and_get_db_action_v2():
    _seed_model_api_user("u_mode_2")
    store = core_store.get_store("u_mode_2")
    out = hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")
    assert out == "db_action_v2"
    assert hosted_config_store.get_hosted_runtime_mode(store) == "db_action_v2"


def test_set_rejects_unknown_mode():
    _seed_model_api_user("u_mode_3")
    store = core_store.get_store("u_mode_3")
    with pytest.raises(ValueError):
        hosted_config_store.set_hosted_runtime_mode(store, "bogus")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_hosted_runtime_mode.py -v`
Expected: FAIL —— `AttributeError: module 'hosted.config_store' has no attribute 'get_hosted_runtime_mode'`。

- [ ] **Step 3: 追加实现到 config_store.py**

在 `backend/hosted/config_store.py` 末尾追加：

```python
# ---------------------------------------------------------------------------
# hosted_runtime_mode —— per-user 灰度开关（Hosted Runtime V2 子项目 B）
# resident_cli：现有常驻 CLI consumer 路径（默认，原样不动）。
# db_action_v2：chat/send 入队 agent_jobs，由独立 V2 worker 池处理（spec §10）。
# 存进既有 model_api_runtime profile blob，两条路径并存。
# ---------------------------------------------------------------------------

HOSTED_RUNTIME_MODE_RESIDENT = "resident_cli"
HOSTED_RUNTIME_MODE_DB_ACTION_V2 = "db_action_v2"
_HOSTED_RUNTIME_MODES = {HOSTED_RUNTIME_MODE_RESIDENT, HOSTED_RUNTIME_MODE_DB_ACTION_V2}


def get_hosted_runtime_mode(store: UserStore) -> str:
    """该用户的 hosted 运行时模式；未设或非法值一律回退默认 resident_cli。"""
    profile = _load_model_api_runtime_profile(store) or {}
    mode = str(profile.get("hosted_runtime_mode") or "")
    return mode if mode in _HOSTED_RUNTIME_MODES else HOSTED_RUNTIME_MODE_RESIDENT


def set_hosted_runtime_mode(store: UserStore, mode: str) -> str:
    """切换该用户的 hosted 运行时模式。非法值 ValueError。返回落地后的 mode。"""
    if mode not in _HOSTED_RUNTIME_MODES:
        raise ValueError(f"unknown hosted_runtime_mode: {mode!r}")
    _patch_model_api_runtime_profile(store, {"hosted_runtime_mode": mode})
    return mode
```

（`UserStore` 已在文件顶部 `from core.store import UserStore` 导入，可直接用于注解。）

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_hosted_runtime_mode.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: commit**

```bash
git add backend/hosted/config_store.py tests/test_hosted_runtime_mode.py
git commit -m "feat(runtime-v2): hosted_runtime_mode per-user gate (resident_cli|db_action_v2)"
```

---

### Task 9: chat/send 集成 —— db_action_v2 入队 job（gated）

**Files:**
- Modify: `backend/hosted/chat_send_core.py`（导入 + 两处改动：跳过 resident 兜底守卫、末尾分支入队）
- Test: `tests/test_chat_send_v2_enqueue.py`

**Interfaces:**
- Consumes: Task 8 的 `config_store.get_hosted_runtime_mode`；Task 2 的 `jobs_store.enqueue_job`；`core.wake_bus.notify`；既有 `agent_runtime_cutover.build_processing_response`。
- Produces: 无新公有函数——改的是 `model_api_chat_send_core` 的行为：`db_action_v2` 用户的 send 落加密用户消息后**入队 agent_job** 并返回 202 processing，**不**走 resident `handle_send`、**不**被 resident supervisor wedge guard 挡。

> gating 位置（对照现有 `chat_send_core.py`）：
> 1. 顶部 import 追加 `jobs_store` / `core_wake_bus`。
> 2. 在 `driver = resolve_driver(config)` 之后、supervisor wedge guard 之前，读一次 `_v2_mode`。
> 3. wedge guard 只在 **非** v2 模式生效（v2 用户由 worker 而非 resident supervisor 应答，不该被 resident 心跳挡）。
> 4. 末尾：v2 模式 → `enqueue_job(user_id, "chat")` + `wake_bus.notify("v2_jobs", user_id)` + `build_processing_response`；否则原样 `handle_send`。

- [ ] **Step 1: 写失败测试（monkeypatch 掉 enqueue/notify，断言被调用且不走 handle_send）**

`tests/test_chat_send_v2_enqueue.py`：

```python
"""chat/send 在 db_action_v2 模式下入队 agent_job 而非走 resident handle_send。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from core import store as core_store
from hosted import chat_send_core, config_store as hosted_config_store
from hosted import agent_runtime_cutover
from model_api_runtime.v2 import jobs_store
from core import wake_bus as core_wake_bus


def _seed(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
    db.set_blob(uid, "model_api", {
        "route": "model_api", "provider": "anthropic", "model": "m",
        "test_status": "ok", "api_key_envelope": {"body_ct": "x", "nonce": "n", "K_user": "k"},
    })


def test_db_action_v2_enqueues_job_and_skips_resident(monkeypatch):
    _seed("u_send_v2")
    store = core_store.get_store("u_send_v2")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")

    # 让信封构建/驱动解析通过、不打真 enclave/provider。
    monkeypatch.setattr(
        chat_send_core.core_envelope, "_build_shared_envelope_for_store",
        lambda s, pt, **kw: ({"id": "u-msg-1", "body_ct": "c", "nonce": "n", "K_user": "k"}, ""),
    )
    monkeypatch.setattr(chat_send_core.agent_runtime_cutover, "resolve_driver", lambda cfg: "claude")
    # append_chat 走真 store 会尝试 DB；用真的即可（已 seed user）。返回其真实 row。

    enq = {}
    monkeypatch.setattr(
        chat_send_core.jobs_store, "enqueue_job",
        lambda uid, lane, **kw: enq.update(uid=uid, lane=lane, kw=kw) or (123, False),
    )
    notified = {}
    monkeypatch.setattr(
        chat_send_core.core_wake_bus, "notify",
        lambda channel, user_id="": notified.update(channel=channel, user_id=user_id),
    )
    # 若错误地走 resident，会调用 handle_send —— 断言它没被调用。
    called = {"handle_send": False}
    monkeypatch.setattr(
        chat_send_core.agent_runtime_cutover, "handle_send",
        lambda *a, **k: called.update(handle_send=True) or ({"status": "resident"}, 202),
    )

    body, status = chat_send_core.model_api_chat_send_core(
        store, api_key="key", runtime_tok="", payload={"message": "hi"},
    )

    assert status == 202
    assert body["status"] == "processing"
    assert enq == {"uid": "u_send_v2", "lane": "chat", "kw": enq["kw"]}
    assert notified["channel"] == "v2_jobs" and notified["user_id"] == "u_send_v2"
    assert called["handle_send"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_chat_send_v2_enqueue.py -v`
Expected: FAIL —— `AttributeError: module 'hosted.chat_send_core' has no attribute 'jobs_store'`（导入未加）或 `handle_send` 被调用。

- [ ] **Step 3: 改 chat_send_core.py**

3a. 顶部 import 区（现有 `from hosted import turn as hosted_turn` 一带）追加：

```python
from core import wake_bus as core_wake_bus
from model_api_runtime.v2 import jobs_store
```

3b. 在 `driver = agent_runtime_cutover.resolve_driver(config)` 的 try/except 块之后、wedge guard 注释之前，插入一行读取 mode（替换现有第 83–96 行区间的开头）。现有代码：

```python
    except agent_runtime_cutover.UnsupportedProviderError:
        return {"error": "provider_not_configured"}, 409

    # Wedge guard: routing to the agent-runner only works if a supervisor is
```

改为：

```python
    except agent_runtime_cutover.UnsupportedProviderError:
        return {"error": "provider_not_configured"}, 409

    # Hosted Runtime V2 灰度：db_action_v2 用户由独立 worker 池应答，不依赖 resident
    # supervisor，故跳过下面的 resident wedge guard（否则会被 resident 心跳错误 503）。
    _v2_mode = hosted_config_store.get_hosted_runtime_mode(store) == "db_action_v2"

    # Wedge guard: routing to the agent-runner only works if a supervisor is
```

3c. 把 wedge guard 的 `if not live:` 改成只在非 v2 模式生效。现有：

```python
    live, reason = agent_runtime_cutover.check_supervisor_live(require_gateway=_require_gateway)
    if not live:
```

改为：

```python
    live, reason = agent_runtime_cutover.check_supervisor_live(require_gateway=_require_gateway)
    if not _v2_mode and not live:
```

3d. 末尾委托处（现有最后两行）：

```python
    body, status = agent_runtime_cutover.handle_send(store, user_row, driver)
    return body, status
```

改为：

```python
    if _v2_mode:
        # 落加密用户消息已完成（上方 append_chat）；入队/合并 chat job，唤醒 worker 池，
        # 快速返回 202 processing（不写 filler；客户端经 chat poll 取加密回复）。
        jobs_store.enqueue_job(
            store.user_id, "chat", reason="chat_send",
            trace_id=str(user_row.get("id") or "") if isinstance(user_row, dict) else None,
        )
        core_wake_bus.notify("v2_jobs", store.user_id)
        body, status = agent_runtime_cutover.build_processing_response(user_row, driver=driver)
        return body, status

    body, status = agent_runtime_cutover.handle_send(store, user_row, driver)
    return body, status
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_chat_send_v2_enqueue.py -v`
Expected: PASS（1 passed）。

- [ ] **Step 5: 跑全量 V2 + hosted send 回归**

Run: `pytest tests/test_v2_jobs_migration.py tests/test_v2_jobs_store.py tests/test_v2_responder.py tests/test_v2_worker.py tests/test_v2_serve_worker.py tests/test_hosted_runtime_mode.py tests/test_chat_send_v2_enqueue.py -v`
Expected: 全 PASS。再跑既有 hosted send 回归确保 resident 路径零影响：
Run: `pytest tests/ -k "chat_send or hosted_agent_runtime_cutover" -v`
Expected: 全 PASS（resident_cli 默认，行为不变）。

- [ ] **Step 6: commit**

```bash
git add backend/hosted/chat_send_core.py tests/test_chat_send_v2_enqueue.py
git commit -m "feat(runtime-v2): chat/send enqueues agent_job under db_action_v2 gate"
```

---

## Self-Review

**1. Spec coverage（§13 实现顺序第 3–4 步 = B 范围）：**
- §5 4 张表 → Task 1。
- §5 claim 查询（FOR UPDATE SKIP LOCKED）→ Task 2 `claim_next_job`。
- §5 single-flight 唯一索引 + coalesce → Task 1 索引 + Task 2 `enqueue_job`。
- §6 三个有界闸（MAX_WORKERS / MAX_READ_ACTION_PARALLELISM / ENCLAVE_SEMAPHORE）→ Task 6。
- §6 单次解密（每 job 非每 action）→ Task 6 `run_one_turn` 的 ENCLAVE_SEMAPHORE 段 + Task 7 `_resolve_provider`。
- §6 reaper（deadline + stuck）→ Task 3 `reap_stuck_jobs`。
- §6 优雅 drain（SIGTERM）→ Task 6 `stop_event` + Task 7 signal handler。
- §9 status 事件表 + append/list → Task 1 + Task 3（推送管线接 long-poll 属 C）。
- §10 chat/send 入队 + hosted_runtime_mode 灰度 → Task 8 + Task 9。
- §11 R3（enclave 信号量框住所有 enclave-bound 调用）→ Task 6。
- §11 R4（single-flight + 状态迁移 + 并发 claim TDD）→ Task 1 索引 + Task 2 `test_claim_is_exclusive` + Task 4 `invalidate_pending_actions`。
- §13 第 4 步「先直接 final_response，无 planner」的最小闭环 → Task 5 + 6 + 7 + 9。
- action queue 表 + CRUD（C 消费）→ Task 1 + Task 4。

**2. Placeholder scan：** 无 TODO/“类似 Task N”/“加适当错误处理”。每个引用的符号都在本 plan 定义（`jobs_store.*` Task 2–4、`responder.respond` Task 5、`worker.TurnDeps/run_one_turn/run_worker_loop` Task 6、`serve_worker.*` Task 7、`config_store.get/set_hosted_runtime_mode` Task 8）或既有代码（`db.get_pool` / `provider_client.reliable_chat_completion` / `core_enclave._decrypt_envelope_via_enclave` / `core_envelope._build_shared_envelope_for_store` / `store.append_chat` / `runtime_token.mint` / `agent_runtime_cutover.build_processing_response`，均在读码时确认签名）。

**3. Type consistency：** `enqueue_job -> (int, bool)`、`claim_next_job -> dict|None`、`run_one_turn -> str("completed"|"failed")`、`respond(*, provider_config, coalesced_messages, runtime_state) -> str` 在定义 task 与消费 task（worker/serve_worker/chat_send_core）间一致。`TurnDeps.respond` 的调用点（worker `deps.respond(provider_config, messages, runtime_state)`）与生产实现 `serve_worker._respond(provider_config, messages, runtime_state)`、测试替身签名一致。

---

## 需要裁决 / 与 spec 出入的点（回报给上游）

1. **responder 签名偏离任务书**（已在 Interfaces 块详述）：任务书要 `respond(store, *, api_key, runtime_token, ...)`，但那要求 `model_api_runtime` import `hosted.config_store` 解密 → 逆依赖方向（CONTRIBUTING 红线）。本 plan 改为 `respond(*, provider_config, coalesced_messages, runtime_state)`，把单次解密上移到 worker 的注入式 `TurnDeps.resolve_provider`（生产实现在 `serve_worker.py`）。契约本质（用户 BYOK、内部 `reliable_chat_completion`、model-authored）不变。**C 须按新签名对齐。**

2. **schema 相对 spec §5 的两处增补**：`agent_jobs` 加 `claimed_by TEXT`（`claim_next_job(worker_id)` 落 owner，spec SQL 漏列）；`agent_jobs/agent_status_events/runtime_state` 的 `user_id` 加 `REFERENCES users(user_id) ON DELETE CASCADE`（对齐 0011/0012 删账号级联清 per-user 表的不变量）。若上游希望严格照 spec 无 FK/无 claimed_by，请指示。

3. **`_read_messages` 用逐条 enclave 解密取用户消息明文**：这是 B 最小闭环里唯一的「读」——用现有 `_decrypt_envelope_via_enclave` 原语，honest 且直接触发 R3 的 ENCLAVE_SEMAPHORE。但它对每条未答 user 消息发一次 enclave 调用；C 的 capability 层落地后应换成 capability 的批量/缓存读（spec R3「同 job 内 enclave 读尽量合并/缓存」）。B 阶段可接受，需上游确认不要求 B 就上批量解密。

4. **reaper 在 B 只做 `expired`（终态），不 re-pending 重试**：spec §6 写「expired/重试」。为避免 re-pending 撞 single-flight 唯一索引的竞态，B 的 reaper 一律 expired（释放槽位，下一条 chat/send 自然重新入队）；真正的重试/replan 归 C（§8）。若上游要 B 就实现按 `attempt_count` 的自动 re-pending，请指示（需加 per-row 事务 + UniqueViolation 回退到 expired 的逻辑）。

5. **wake 唤醒用轮询兜底为主**：B 的 worker 主要靠 `poll_interval` 轮询 + `wake_bus.notify("v2_jobs")` 广播；worker 侧尚未把该 NOTIFY 接成 asyncio 即时唤醒（跨 LISTEN 线程 → asyncio 的 `call_soon_threadsafe` 桥接）。spec §6「复用 wake_bus 即时唤醒 + 周期轮询兜底」——B 先落轮询兜底（功能完备、延迟 = poll_interval），即时唤醒桥接可留到 C 的 status 推送管线一并做。若要求 B 就上即时唤醒，请指示。
```
