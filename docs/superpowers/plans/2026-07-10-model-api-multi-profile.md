# Model API 多配置（credentials + routes）实施计划

> **RETIRED / DO NOT DEPLOY.** Historical implementation record; process-roster
> and supervisor instructions are not current operations.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户保存多把 provider API key、每把派生多条 model 路由，并选择其中一条生效。

**Architecture:** 新建 `model_api_credentials` + `model_api_routes` 两张表取代单条 `user_blobs(kind='model_api')`。partial unique index 在 DB 层强制「每用户恰一条 active route」。activate 必须先同步测活通过。`POST /v1/model_api/setup` 改为幂等 upsert，旧版 App 无感。

**Tech Stack:** Python 3.11 / FastAPI(ASGI) / psycopg3 + psycopg_pool / Alembic / Postgres / pytest

**Spec:** `docs/superpowers/specs/2026-07-10-model-api-multi-profile-design.md`

## Global Constraints

- 路由进 `backend/hosted/setup_routes_asgi.py`（FastAPI `APIRouter`），业务逻辑进 `backend/hosted/setup_core.py`（框架中立）。**`asgi_app.py` 零 diff。**
- 阻塞调用一律经 `await threadpool.run_db(...)` 移出事件循环。
- 跨模块调用一律 `from pkg import module` + `module.func()`；**禁止** `from module import func`（否则测试 monkeypatch 定义处失效）。
- 模块别名避开局部变量名（用 `hosted_config_store`、`core_envelope` 这类前缀别名）。
- 错误返回必须是稳定 slug：`{"error": "<snake_case_slug>", ...}`，动态内容放 `detail`。新增 slug 同 PR 登记进 `docs/API_ERRORS.md`。
- 测试一律放仓库根 `tests/`，用 `from asgi_test_client import make_client` 驱动。
- ⚠️ **`tests/conftest.py` 里只有两个 fixture：`backend_env` 和 `client`，外加一个 `seed_user(user_id, **doc)` 函数。** 本 plan 各任务的示例测试代码里出现的 `registered_user` / `user_store` / `second_registered_user` **都不存在**，是编写 plan 时的疏漏。写测试时改用下面的真实范式（照抄 `tests/test_db.py`）：

  ```python
  import sys, uuid
  from pathlib import Path
  sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

  import db
  from conftest import seed_user

  def _uid() -> str:
      return f"usr_{uuid.uuid4().hex[:16]}"

  # 直接写 per-user 表前必须先 seed_user()——两张新表都有 user_id → users FK
  uid = _uid(); seed_user(uid)

  # 需要 UserStore 时：
  from core.store import get_store
  store = get_store(uid)
  ```

  需要经 HTTP 打端点时用 `client` fixture，并在测试文件内自备注册 helper。**public_key 必须随机**，不要用固定值——`tests/test_model_api_path.py` 的 `_register()` 就是因为写死 `\x11*32` 而在同文件内第二次注册时撞 `account_exists_for_key` 409（那正是基线里 3 个 pre-existing 红的成因）：

  ```python
  import base64, os
  def _register(client) -> dict:
      pk = base64.b64encode(os.urandom(32)).decode()
      res = client.post("/v1/users/register",
                        json={"public_key": pk, "archive_language": "en"})
      assert res.status_code == 201, res.get_data(as_text=True)
      return res.json()
  ```
- **测试必须起真 PG**：`tests/conftest.py` 的 `collect_ignore` 在无数据库时**静默跳过** DB 模块且不报 skipped，「全绿」是假象。默认 `FEEDLING_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/postgres`。
- 单模块超 800 行需在 PR 说明理由，超 1500 行必须拆。
- 每个任务结束跑 `python -m pyflakes backend/<改动的包>`，必须干净。
- 全量回归判据是**零新增失败**（已知 pre-existing 红：8 个，名单见「前置」节）。
- **不要 `git commit`。** 所有改动留在工作树，由仓库所有者最后统一提交。各任务末尾的 "Commit" 步骤一律跳过，改为 `git status --short` 确认改动范围符合预期。
- 服务端永不解密用户内容。provider key 只以 `api_key_envelope` 形态入库；解密只经 `core.enclave._decrypt_envelope_via_enclave`。
- 路由集变更（本 plan 新增 7 条路由）须在 PR 描述里显式列出。

## 前置：启动测试数据库

```bash
docker run -d --name feedling-test-pg -p 55432:5432 \
  -e POSTGRES_PASSWORD=test postgres:16
```

基线**已在 `test` 分支的干净 checkout 上实测**（2026-07-10，worktree `feat/model-api-multi-profile`）：

```bash
python -m pytest tests/ -q \
    --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py -p no:cacheprovider
```

实测：**`8 failed, 2526 passed, 4 skipped, 9 xfailed`**（141s）。

这 8 个红全是 pre-existing，与本 plan 无关。判据是**这 8 个仍然红、且不多出第 9 个**：

```
tests/test_chat_route_debug_trace.py::test_resident_chat_message_and_poll_emit_route_trace
tests/test_chat_route_debug_trace.py::test_resident_chat_response_emits_route_trace
tests/test_chat_route_debug_trace.py::test_resident_chat_response_gate_emits_route_trace
tests/test_debug_trace_event_route.py::test_emit_event_records
tests/test_memory_capture_trace.py::test_enqueue_duplicate_capture_key_does_not_emit_queued_event
tests/test_model_api_path.py::test_model_api_setup_reasoning_effort_off_and_default_disable_gateway_reasoning
tests/test_model_api_path.py::test_chat_response_rejects_verify_ping_source_without_pending_ping
tests/test_model_api_path.py::test_chat_response_accepts_verify_ping_reply_to_pending_ping
```

⚠️ **`test_model_api_setup_reasoning_effort_off_and_default_disable_gateway_reasoning` 直接测你要改的 `model_api_setup`，但它现在就是红的** —— 原因与 setup 逻辑无关：该文件的 `_register()` 用固定 public_key（`\x11`*32），同文件内第二次注册撞 `account_exists_for_key` 409。所以**不能拿它当验证依据**。改完 setup 后它应该仍以 **同样的 409 断言** 失败；若它变成别的错误（例如 KeyError / 500），那才是真回归。

同理 `test_chat_response_*verify_ping*` 两个红是 chat gate 的桩函数签名过时（`_gate_bootstrap_for_chat` 多了 `is_verify_reply` kwarg，测试的 lambda 没跟上），与本 plan 无关。

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `backend/alembic/versions/0014_model_api_profiles.py` | 建两张表 + 从 blob 回填 | 新建 |
| `backend/db.py` | 两张表的 CRUD + activate 事务内两条 UPDATE + roster JOIN SQL + 释放 claim | 修改 |
| `backend/hosted/config_store.py` | `load_active_route` facade；`_load_runtime_provider_config` 改读新表；`record_runtime_error` 写 route 行 | 修改 |
| `backend/hosted/setup_core.py` | `setup` 改幂等 upsert；`get`/`test`/`driver`/`delete`/`key_envelope` 改读新表；新增 7 个集合端点实现 | 修改 |
| `backend/hosted/setup_routes_asgi.py` | 7 条新路由 | 修改 |
| `docs/API_ERRORS.md` | 登记 3 个新 slug | 修改 |
| `tests/test_model_api_profiles_db.py` | db 层：唯一索引、复合外键、activate 原子性、自动接管 | 新建 |
| `tests/test_model_api_profiles_routes.py` | 端点：activate gate、setup 幂等、换 key、删除接管 | 新建 |
| `tests/test_model_api_profiles_migration.py` | 回填正确性 + 幂等 | 新建 |

## 数据契约（跨任务共享，务必一致）

`db.model_api_routes_list()` / `db.model_api_active_route()` 返回的 route dict：

```python
{
    "id": str,                        # route uuid
    "credential_id": str,
    "provider": str,                  # 来自 credential
    "model": str,
    "credential_label": str,          # credential.label
    "api_key_hint": str,              # 来自 credential
    "base_url": str,                  # 来自 credential
    "supports_responses": bool,       # 来自 credential
    "reasoning_effort": str,          # NULL → ""
    "is_active": bool,
    "test_status": str,               # untested | ok | failed
    "last_test_at": str,              # ISO8601 或 ""
    "last_test_error": str,
    "last_runtime_error": str,
    "last_runtime_error_class": str,
}
```

**唯一区别**：`model_api_active_route()` 额外带 `"api_key_envelope": dict`（内部解密用）。`model_api_routes_list()` **不带** envelope——它直接喂给 `GET /routes` 的响应，带上就是把密文暴露给客户端。

---

### Task 1: 迁移 0014 —— 建表 + 回填

**Files:**
- Create: `backend/alembic/versions/0014_model_api_profiles.py`
- Test: `tests/test_model_api_profiles_migration.py`

**Interfaces:**
- Consumes: 现有 `users` 表、`user_blobs(user_id, kind='model_api')` 的 JSONB `doc`
- Produces: 表 `model_api_credentials`、`model_api_routes`；索引 `model_api_routes_one_active`、`model_api_routes_uniq`（credentials 上**没有** provider/base_url 唯一索引）

- [ ] **Step 1: 写迁移文件**

照抄 `0013_genesis_resident_claim.py` 的 `_UP` / `_DOWN` 字符串风格。

```python
"""model_api 多配置：credentials + routes 两张表。

取代单条 user_blobs(kind='model_api')。credentials 一把 key 一行（含 envelope 密文），
routes 是 (credential, model) 的组合，partial unique index 强制每用户恰一条 is_active。

user_blobs 的 model_api blob 原样保留、新代码不读不写——它是回滚快照。等新镜像稳定
运行后另开 PR 删除。

Revision ID: 0014_model_api_profiles
"""
from alembic import op

revision = "0014_model_api_profiles"
down_revision = "0013_genesis_resident_claim"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS model_api_credentials (
    id                 UUID PRIMARY KEY,
    user_id            TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    provider           TEXT NOT NULL,
    label              TEXT NOT NULL DEFAULT '',
    base_url           TEXT NOT NULL DEFAULT '',
    api_key_envelope   JSONB NOT NULL,
    api_key_hint       TEXT NOT NULL DEFAULT '',
    supports_responses BOOLEAN NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT model_api_credentials_user_id_uniq UNIQUE (user_id, id)
);

-- 刻意 NOT 加 (user_id, provider, base_url) 唯一索引：iOS 支持同一 provider、
-- 同一 base_url 下存多把不同的 key。setup 的幂等由代码锚定 active credential。

CREATE TABLE IF NOT EXISTS model_api_routes (
    id                       UUID PRIMARY KEY,
    user_id                  TEXT NOT NULL,
    credential_id            UUID NOT NULL,
    model                    TEXT NOT NULL,
    reasoning_effort         TEXT,
    is_active                BOOLEAN NOT NULL DEFAULT FALSE,
    test_status              TEXT NOT NULL DEFAULT 'untested',
    last_test_at             TIMESTAMPTZ,
    last_test_error          TEXT NOT NULL DEFAULT '',
    last_runtime_error       TEXT NOT NULL DEFAULT '',
    last_runtime_error_class TEXT NOT NULL DEFAULT '',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT model_api_routes_user_fkey
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
    CONSTRAINT model_api_routes_credential_fkey
        FOREIGN KEY (user_id, credential_id)
        REFERENCES model_api_credentials (user_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS model_api_routes_one_active
    ON model_api_routes (user_id) WHERE is_active;
CREATE UNIQUE INDEX IF NOT EXISTS model_api_routes_uniq
    ON model_api_routes (credential_id, model);

-- 回填：每个有信封的用户一条 credential + 一条 active route。
-- credentials 没有唯一索引可撞，所以幂等靠 NOT EXISTS（该用户尚无任何
-- credential 时才回填）；routes 仍靠 ON CONFLICT (credential_id, model)。
INSERT INTO model_api_credentials
    (id, user_id, provider, label, base_url, api_key_envelope, api_key_hint, supports_responses)
SELECT gen_random_uuid(),
       b.user_id,
       LOWER(COALESCE(b.doc->>'provider', '')),
       INITCAP(COALESCE(b.doc->>'provider', 'provider')),
       COALESCE(b.doc->>'base_url', ''),
       b.doc->'api_key_envelope',
       COALESCE(b.doc->>'api_key_hint', ''),
       COALESCE(b.doc->>'supports_responses', '') = 'true'
FROM user_blobs b
JOIN users u ON u.user_id = b.user_id
WHERE b.kind = 'model_api'
  AND b.doc ? 'api_key_envelope'
  AND jsonb_typeof(b.doc->'api_key_envelope') = 'object'
  AND NOT EXISTS (
        SELECT 1 FROM model_api_credentials c WHERE c.user_id = b.user_id
      );

INSERT INTO model_api_routes
    (id, user_id, credential_id, model, reasoning_effort, is_active,
     test_status, last_test_at)
SELECT gen_random_uuid(),
       c.user_id,
       c.id,
       COALESCE(b.doc->>'model', ''),
       NULLIF(COALESCE(b.doc->>'reasoning_effort', ''), ''),
       TRUE,
       COALESCE(NULLIF(b.doc->>'test_status', ''), 'untested'),
       NULLIF(b.doc->>'last_test_at', '')::timestamptz
FROM model_api_credentials c
JOIN user_blobs b
  ON b.user_id = c.user_id
 AND b.kind = 'model_api'
 AND LOWER(COALESCE(b.doc->>'provider', '')) = c.provider
 AND COALESCE(b.doc->>'base_url', '') = c.base_url
ON CONFLICT (credential_id, model) DO NOTHING;
"""

_DOWN = """
DROP TABLE IF EXISTS model_api_routes;
DROP TABLE IF EXISTS model_api_credentials;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
```

`gen_random_uuid()` 是 PG13+ 内建，无需 pgcrypto 扩展。

第二条 INSERT 里 `is_active` 恒为 TRUE：每用户只可能有一条 blob，所以只会插一条 route，不会撞 `model_api_routes_one_active`。重跑时被 `ON CONFLICT (credential_id, model) DO NOTHING` 挡下。

- [ ] **Step 2: 写回填测试**

新建 `tests/test_model_api_profiles_migration.py`：

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db


def _seed_blob(user_id: str, doc: dict) -> None:
    db.set_blob(user_id, "model_api", doc)


def test_backfill_creates_one_credential_and_one_active_route(backend_env, registered_user):
    uid = registered_user["user_id"]
    _seed_blob(uid, {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "base_url": "",
        "api_key_envelope": {"v": 1, "body_ct": "abc"},
        "api_key_hint": "sk-a...451",
        "test_status": "ok",
        "reasoning_effort": "high",
        "supports_responses": "false",
    })

    _run_backfill()

    creds = db.model_api_credentials_list(uid)
    assert len(creds) == 1
    assert creds[0]["provider"] == "anthropic"
    assert creds[0]["api_key_hint"] == "sk-a...451"

    routes = db.model_api_routes_list(uid)
    assert len(routes) == 1
    assert routes[0]["model"] == "claude-sonnet-4-5"
    assert routes[0]["is_active"] is True
    assert routes[0]["test_status"] == "ok"
    assert routes[0]["reasoning_effort"] == "high"


def test_backfill_is_idempotent(backend_env, registered_user):
    uid = registered_user["user_id"]
    _seed_blob(uid, {
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "base_url": "",
        "api_key_envelope": {"v": 1, "body_ct": "abc"},
        "test_status": "ok",
    })

    _run_backfill()
    _run_backfill()

    assert len(db.model_api_credentials_list(uid)) == 1
    assert len(db.model_api_routes_list(uid)) == 1


def test_backfill_skips_blob_without_envelope(backend_env, registered_user):
    uid = registered_user["user_id"]
    _seed_blob(uid, {"provider": "openai", "model": "gpt-4.1-mini", "test_status": "failed"})

    _run_backfill()

    assert db.model_api_credentials_list(uid) == []
    assert db.model_api_routes_list(uid) == []
```

`_run_backfill()` 复用迁移里的两条 INSERT。为避免测试直接跑 alembic（慢且与 conftest 建库流程冲突），把两条 INSERT 抽成迁移模块里的常量并在测试里执行：

在迁移文件末尾追加（迁移本身不变，只是把 SQL 暴露给测试）：

```python
# 供测试复用（迁移的回填部分，不含 DDL）
BACKFILL_SQL = _UP.split("-- 回填：", 1)[1].split("\n", 1)[1]
```

测试里的 `_run_backfill`：

```python
def _run_backfill():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mig0014",
        Path(__file__).parent.parent / "backend" / "alembic" / "versions" / "0014_model_api_profiles.py",
    )
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    with db.get_pool().connection() as conn:
        conn.execute(mig.BACKFILL_SQL)
```

> 该测试依赖 Task 2 的 `db.model_api_credentials_list` / `db.model_api_routes_list`。先写测试让它红（ImportError / AttributeError），Task 2 完成后转绿。若执行顺序上想让 Task 1 自洽，可在本任务用裸 SQL 断言，Task 2 再换成 db 函数。

- [ ] **Step 3: 跑迁移，确认建表**

```bash
cd backend && alembic upgrade head
```

预期：无报错。验证：

```bash
psql postgresql://postgres:test@127.0.0.1:55432/postgres \
  -c "\d model_api_routes"
```

预期输出里能看到 `model_api_routes_one_active` 是 `UNIQUE, btree (user_id) WHERE is_active`。

- [ ] **Step 4: 确认 downgrade 可用**

```bash
cd backend && alembic downgrade -1 && alembic upgrade head
```

预期：两条命令都成功。

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/0014_model_api_profiles.py tests/test_model_api_profiles_migration.py
git commit -m "feat(model-api): add credentials/routes tables + backfill migration"
```

---

### Task 2: db 层 CRUD

**Files:**
- Modify: `backend/db.py`（在 `world_book_*` 那组函数之后追加新的一组）
- Test: `tests/test_model_api_profiles_db.py`

**Interfaces:**
- Consumes: Task 1 的两张表
- Produces（后续任务全部依赖这些精确签名）：

```python
def model_api_credentials_list(user_id: str) -> list[dict]
def model_api_credential_get(user_id: str, credential_id: str) -> dict | None
def model_api_credential_create(user_id: str, *, provider: str, base_url: str,
                                label: str, api_key_envelope: dict,
                                api_key_hint: str, supports_responses: bool) -> str | None
def model_api_credential_update(user_id: str, credential_id: str, *,
                                label: str | None = None,
                                api_key_envelope: dict | None = None,
                                api_key_hint: str | None = None,
                                supports_responses: bool | None = None) -> bool
def model_api_credential_delete(user_id: str, credential_id: str) -> bool
def model_api_routes_list(user_id: str) -> list[dict]
def model_api_route_get(user_id: str, route_id: str) -> dict | None
def model_api_active_route(user_id: str) -> dict | None      # 带 api_key_envelope
def model_api_route_upsert(user_id: str, credential_id: str, model: str,
                           reasoning_effort: str | None) -> str | None
def model_api_route_delete(user_id: str, route_id: str) -> bool
def model_api_route_activate(user_id: str, route_id: str) -> bool
def model_api_route_mark_test(user_id: str, route_id: str, *, status: str, error: str = "") -> bool
def model_api_route_mark_runtime_error(user_id: str, *, error: str, error_class: str) -> bool
def model_api_autoselect_active(user_id: str) -> str | None
```

- [ ] **Step 1: 写失败的 db 层测试**

新建 `tests/test_model_api_profiles_db.py`：

```python
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db

_ENV = {"v": 1, "body_ct": "ct", "nonce": "n"}


def _cred(uid, provider="anthropic", base_url="", hint="sk-a...451", label=None):
    return db.model_api_credential_create(
        uid, provider=provider, base_url=base_url, label=label or f"{provider} key",
        api_key_envelope=_ENV, api_key_hint=hint, supports_responses=False,
    )


def test_same_provider_can_hold_two_distinct_keys(backend_env, registered_user):
    """iOS 的 credentialList 让用户在同一 provider 下选不同的凭据
    （个人 key / 团队 key）。credentials 表刻意没有 (user_id,provider,base_url)
    唯一索引，正是为了支持这个。"""
    uid = registered_user["user_id"]
    a = _cred(uid, hint="sk-a...451", label="Personal")
    b = _cred(uid, hint="sk-a...999", label="Team")
    assert a != b
    creds = db.model_api_credentials_list(uid)
    assert len(creds) == 2
    assert {c["api_key_hint"] for c in creds} == {"sk-a...451", "sk-a...999"}
    assert {c["label"] for c in creds} == {"Personal", "Team"}


def test_one_credential_can_have_many_routes(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r1 = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", "off")
    assert r1 != r2
    assert len(db.model_api_routes_list(uid)) == 2


def test_route_upsert_is_idempotent_on_credential_model(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r1 = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    r2 = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", "high")
    assert r1 == r2
    assert len(db.model_api_routes_list(uid)) == 1
    assert db.model_api_route_get(uid, r1)["reasoning_effort"] == "high"


def test_activate_leaves_exactly_one_active(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r1 = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)

    assert db.model_api_route_activate(uid, r1) is True
    assert db.model_api_route_activate(uid, r2) is True

    actives = [r for r in db.model_api_routes_list(uid) if r["is_active"]]
    assert len(actives) == 1
    assert actives[0]["id"] == r2


def test_active_route_carries_envelope_but_list_does_not(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r1 = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r1)

    active = db.model_api_active_route(uid)
    assert active["api_key_envelope"] == _ENV
    assert active["provider"] == "anthropic"

    listed = db.model_api_routes_list(uid)
    assert "api_key_envelope" not in listed[0]


def test_route_cannot_reference_another_users_credential(backend_env, registered_user, second_registered_user):
    uid_a = registered_user["user_id"]
    uid_b = second_registered_user["user_id"]
    cid_a = _cred(uid_a)
    # 复合外键 (user_id, credential_id) 让 DB 拒绝跨用户引用
    assert db.model_api_route_upsert(uid_b, cid_a, "claude-sonnet-4-5", None) is None
    assert db.model_api_routes_list(uid_b) == []


def test_deleting_credential_cascades_its_routes(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)

    assert db.model_api_credential_delete(uid, cid) is True
    assert db.model_api_routes_list(uid) == []


def test_autoselect_active_picks_latest_ok_route(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r_failed = db.model_api_route_upsert(uid, cid, "bad-model", None)
    r_ok = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    db.model_api_route_mark_test(uid, r_failed, status="failed", error="401")
    db.model_api_route_mark_test(uid, r_ok, status="ok")

    picked = db.model_api_autoselect_active(uid)
    assert picked == r_ok
    assert db.model_api_active_route(uid)["id"] == r_ok


def test_autoselect_returns_none_when_no_ok_route(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "bad-model", None)
    db.model_api_route_mark_test(uid, r, status="failed", error="401")

    assert db.model_api_autoselect_active(uid) is None
    assert db.model_api_active_route(uid) is None


def test_mark_runtime_error_writes_active_route(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    assert db.model_api_route_mark_runtime_error(
        uid, error="insufficient balance", error_class="provider_402") is True
    got = db.model_api_route_get(uid, r)
    assert got["last_runtime_error"] == "insufficient balance"
    assert got["last_runtime_error_class"] == "provider_402"
```

需要一个 `second_registered_user` fixture。如果 `tests/conftest.py` 没有，在本测试文件里加：

```python
@pytest.fixture
def second_registered_user(client):
    resp = client.post("/v1/account/register", json={})
    assert resp.status_code == 200
    return resp.json()
```

（先看 `tests/conftest.py` 里 `registered_user` 是怎么造的，照抄同一形态。）

- [ ] **Step 2: 跑测试确认它失败**

```bash
python -m pytest tests/test_model_api_profiles_db.py -q
```

预期：全部 FAIL，`AttributeError: module 'db' has no attribute 'model_api_credential_create'`。

- [ ] **Step 3: 实现 db 层**

在 `backend/db.py` 的 `world_book_replace_all` 之后追加。注意 `Jsonb` 已在文件顶部 import。

```python
# ─────────────────────────── model_api credentials / routes ───────────────────
#
# 取代单条 user_blobs(kind='model_api')。credentials 一把 key 一行（envelope 密文），
# routes 是 (credential, model) 组合。`model_api_routes_one_active` 这个 partial
# unique index 让「每用户恰一条 active」由 DB 强制，而不是靠调用方自觉。
#
# ⚠️ ``model_api_routes_list`` 刻意 **不返回** api_key_envelope——它直接喂给
# GET /v1/model_api/routes 的响应体。只有 ``model_api_active_route`` 带 envelope，
# 供 config_store 走 enclave 解密。别在 list 里加回来。

_ROUTE_COLUMNS = """
    r.id::text, r.credential_id::text, c.provider, r.model, c.label,
    c.api_key_hint, c.base_url, c.supports_responses,
    COALESCE(r.reasoning_effort, ''), r.is_active, r.test_status,
    COALESCE(to_char(r.last_test_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), ''),
    r.last_test_error, r.last_runtime_error, r.last_runtime_error_class
"""


def _route_row_to_dict(row: tuple) -> dict:
    return {
        "id": row[0], "credential_id": row[1], "provider": row[2], "model": row[3],
        "credential_label": row[4], "api_key_hint": row[5], "base_url": row[6],
        "supports_responses": bool(row[7]), "reasoning_effort": row[8],
        "is_active": bool(row[9]), "test_status": row[10], "last_test_at": row[11],
        "last_test_error": row[12], "last_runtime_error": row[13],
        "last_runtime_error_class": row[14],
    }


def model_api_credentials_list(user_id: str) -> list[dict]:
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT id::text, provider, label, base_url, api_key_hint, "
                "       supports_responses "
                "FROM model_api_credentials WHERE user_id = %s ORDER BY created_at, id",
                (user_id,),
            ).fetchall()
        return [{"id": r[0], "provider": r[1], "label": r[2], "base_url": r[3],
                 "api_key_hint": r[4], "supports_responses": bool(r[5])} for r in rows]
    except Exception as e:
        log.error("[db] model_api_credentials_list(%s) failed: %s", user_id, e)
        return []


def model_api_credential_get(user_id: str, credential_id: str) -> dict | None:
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT id::text, provider, label, base_url, api_key_hint, "
                "       supports_responses, api_key_envelope "
                "FROM model_api_credentials WHERE user_id = %s AND id = %s",
                (user_id, credential_id),
            ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "provider": row[1], "label": row[2], "base_url": row[3],
                "api_key_hint": row[4], "supports_responses": bool(row[5]),
                "api_key_envelope": row[6]}
    except Exception as e:
        log.error("[db] model_api_credential_get(%s,%s) failed: %s", user_id, credential_id, e)
        return None


def model_api_credential_create(user_id: str, *, provider: str, base_url: str,
                                label: str, api_key_envelope: dict,
                                api_key_hint: str, supports_responses: bool) -> str | None:
    """总是新建一条 credential，返回其 id。

    同一 (user_id, provider, base_url) 下允许多条 —— 用户可以为同一个 provider
    存多把 key（个人的 / 团队的）。setup 的幂等不靠唯一索引，而是在 setup_core 里
    锚定 active route 的 credential 决定「更新」还是「新建」。
    """
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO model_api_credentials "
                "  (id, user_id, provider, label, base_url, api_key_envelope, "
                "   api_key_hint, supports_responses) "
                "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id::text",
                (user_id, provider, label, base_url, Jsonb(api_key_envelope),
                 api_key_hint, supports_responses),
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        log.error("[db] model_api_credential_create(%s,%s) failed: %s", user_id, provider, e)
        return None


def model_api_credential_update(user_id: str, credential_id: str, *,
                               label: str | None = None,
                               api_key_envelope: dict | None = None,
                               api_key_hint: str | None = None,
                               supports_responses: bool | None = None) -> bool:
    sets, params = [], []
    if label is not None:
        sets.append("label = %s")
        params.append(label)
    if api_key_envelope is not None:
        sets.append("api_key_envelope = %s")
        params.append(Jsonb(api_key_envelope))
    if api_key_hint is not None:
        sets.append("api_key_hint = %s")
        params.append(api_key_hint)
    if supports_responses is not None:
        sets.append("supports_responses = %s")
        params.append(supports_responses)
    if not sets:
        return False
    sets.append("updated_at = now()")
    params += [user_id, credential_id]
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                f"UPDATE model_api_credentials SET {', '.join(sets)} "
                "WHERE user_id = %s AND id = %s",
                tuple(params),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_credential_update(%s,%s) failed: %s", user_id, credential_id, e)
        return False


def model_api_credential_delete(user_id: str, credential_id: str) -> bool:
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM model_api_credentials WHERE user_id = %s AND id = %s",
                (user_id, credential_id),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_credential_delete(%s,%s) failed: %s", user_id, credential_id, e)
        return False


def model_api_routes_list(user_id: str) -> list[dict]:
    """不含 api_key_envelope——直接喂给 GET /v1/model_api/routes 的响应。"""
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                f"SELECT {_ROUTE_COLUMNS} "
                "FROM model_api_routes r "
                "JOIN model_api_credentials c ON c.id = r.credential_id "
                "WHERE r.user_id = %s ORDER BY r.created_at, r.id",
                (user_id,),
            ).fetchall()
        return [_route_row_to_dict(r) for r in rows]
    except Exception as e:
        log.error("[db] model_api_routes_list(%s) failed: %s", user_id, e)
        return []


def model_api_route_get(user_id: str, route_id: str) -> dict | None:
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                f"SELECT {_ROUTE_COLUMNS} "
                "FROM model_api_routes r "
                "JOIN model_api_credentials c ON c.id = r.credential_id "
                "WHERE r.user_id = %s AND r.id = %s",
                (user_id, route_id),
            ).fetchone()
        return _route_row_to_dict(row) if row else None
    except Exception as e:
        log.error("[db] model_api_route_get(%s,%s) failed: %s", user_id, route_id, e)
        return None


def model_api_active_route(user_id: str) -> dict | None:
    """带 api_key_envelope —— 供 config_store 走 enclave 解密。"""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                f"SELECT {_ROUTE_COLUMNS}, c.api_key_envelope "
                "FROM model_api_routes r "
                "JOIN model_api_credentials c ON c.id = r.credential_id "
                "WHERE r.user_id = %s AND r.is_active",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        out = _route_row_to_dict(row)
        out["api_key_envelope"] = row[15]
        return out
    except Exception as e:
        log.error("[db] model_api_active_route(%s) failed: %s", user_id, e)
        return None


def model_api_route_upsert(user_id: str, credential_id: str, model: str,
                           reasoning_effort: str | None) -> str | None:
    """按 (credential_id, model) upsert。跨用户引用会被复合外键拒绝 → 返回 None。"""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO model_api_routes "
                "  (id, user_id, credential_id, model, reasoning_effort) "
                "VALUES (gen_random_uuid(), %s, %s, %s, %s) "
                "ON CONFLICT (credential_id, model) DO UPDATE SET "
                "  reasoning_effort = EXCLUDED.reasoning_effort, updated_at = now() "
                "RETURNING id::text",
                (user_id, credential_id, model, reasoning_effort),
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        log.error("[db] model_api_route_upsert(%s,%s,%s) failed: %s",
                  user_id, credential_id, model, e)
        return None


def model_api_route_delete(user_id: str, route_id: str) -> bool:
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM model_api_routes WHERE user_id = %s AND id = %s",
                (user_id, route_id),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_route_delete(%s,%s) failed: %s", user_id, route_id, e)
        return False


def model_api_route_activate(user_id: str, route_id: str) -> bool:
    """一个事务内两条语句完成切换：先清旧 active，再置新 active。

    ⚠️ 不要图省事写成单条 ``SET is_active = (id = %s) WHERE is_active OR id = %s``。
    那依赖「唯一索引在语句末检查」，而这对 Postgres 的**非 DEFERRABLE** partial
    unique index 不成立：同一 UPDATE 内索引维护逐行进行、行序不确定，只要目标行
    先于旧 active 行被处理，中途就同时存在两条 is_active=true，当场撞
    ``model_api_routes_one_active``。partial unique index 也无法声明 DEFERRABLE。

    两条语句在同一事务内对旧 active 行加行锁，并发 activate 在该行排队；不变量
    始终由 DB 保证——最多一条成功，落败者撞唯一索引回滚并返回 False。

    ⚠️ 必须先确认目标 route 存在且属于该用户，否则「清旧 active」那条 UPDATE 会
    在目标不存在时照样执行，把用户的 active route 清掉、返回 False —— 用户随即从
    ``list_agent_runtime_enabled_users`` 的 roster 消失，consumer 被 kill 且不自愈。
    客户端发一个陈旧 route_id 就能打停自己的托管 agent。
    """
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                target = conn.execute(
                    "SELECT 1 FROM model_api_routes WHERE user_id = %s AND id = %s FOR UPDATE",
                    (user_id, route_id),
                ).fetchone()
                if target is None:
                    return False      # 目标不存在/不属于该用户 —— 绝不能有副作用
                conn.execute(
                    "UPDATE model_api_routes SET is_active = FALSE, updated_at = now() "
                    "WHERE user_id = %s AND is_active AND id != %s",
                    (user_id, route_id),
                )
                cur = conn.execute(
                    "UPDATE model_api_routes SET is_active = TRUE, updated_at = now() "
                    "WHERE user_id = %s AND id = %s",
                    (user_id, route_id),
                )
        return cur.rowcount > 0
    except Exception as e:
        # route_id 不是合法 UUID 字面量时 psycopg 在此抛出，同样返回 False（无副作用）
        log.error("[db] model_api_route_activate(%s,%s) failed: %s", user_id, route_id, e)
        return False


def model_api_route_mark_test(user_id: str, route_id: str, *, status: str, error: str = "") -> bool:
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "UPDATE model_api_routes SET test_status = %s, last_test_error = %s, "
                "       last_test_at = now(), updated_at = now() "
                "WHERE user_id = %s AND id = %s",
                (status, str(error or "")[:300], user_id, route_id),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_route_mark_test(%s,%s) failed: %s", user_id, route_id, e)
        return False


def model_api_route_mark_runtime_error(user_id: str, *, error: str, error_class: str) -> bool:
    """写 active route 行。传空串即清空（agent-runner 回合成功时调用）。"""
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "UPDATE model_api_routes SET last_runtime_error = %s, "
                "       last_runtime_error_class = %s, updated_at = now() "
                "WHERE user_id = %s AND is_active",
                (str(error or "")[:300], str(error_class or "")[:64], user_id),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_route_mark_runtime_error(%s) failed: %s", user_id, e)
        return False


def model_api_autoselect_active(user_id: str) -> str | None:
    """删掉 active route 之后重新选主：挑 updated_at 最新的 ok route。
    没有候选则返回 None（该用户从 roster 消失，consumer 会停）。"""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "UPDATE model_api_routes SET is_active = TRUE, updated_at = now() "
                "WHERE id = ("
                "  SELECT id FROM model_api_routes "
                "  WHERE user_id = %s AND test_status = 'ok' AND NOT is_active "
                "  ORDER BY updated_at DESC, id LIMIT 1"
                ") RETURNING id::text",
                (user_id,),
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        log.error("[db] model_api_autoselect_active(%s) failed: %s", user_id, e)
        return None
```

**注意** `model_api_route_upsert` 靠 `except` 捕获复合外键违例返回 `None` —— 这是 `test_route_cannot_reference_another_users_credential` 依赖的行为。psycopg 抛 `ForeignKeyViolation`，被宽 `except Exception` 接住并 log。这与本文件其它写函数「swallow-and-log」的既有风格一致（见文件顶部 docstring 的 Durability parity 段）。

- [ ] **Step 4: 跑测试确认全绿**

```bash
python -m pytest tests/test_model_api_profiles_db.py -q
python -m pytest tests/test_model_api_profiles_migration.py -q
```

预期：两个文件全 PASS。

- [ ] **Step 5: pyflakes**

```bash
python -m pyflakes backend/db.py
```

预期：无输出。

- [ ] **Step 6: Commit**

```bash
git add backend/db.py tests/test_model_api_profiles_db.py
git commit -m "feat(model-api): db layer for credentials/routes"
```

---

### Task 3: roster SQL 改走 JOIN

**Files:**
- Modify: `backend/db.py:1260`（`list_agent_runtime_enabled_users`）
- Test: `tests/test_model_api_profiles_db.py`（追加）

**Interfaces:**
- Consumes: Task 2 的两张表与 CRUD
- Produces: `list_agent_runtime_enabled_users` 返回值形状**不变**（`[{"user_id","driver","provider","model","base_url","supports_responses","reasoning_effort"}]`），故 `supervisor` / `litellm_gateway` 零改动。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_model_api_profiles_db.py`：

```python
def test_roster_only_returns_active_ok_routes(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r_sonnet = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", "high")
    r_haiku = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    db.model_api_route_mark_test(uid, r_sonnet, status="ok")
    db.model_api_route_mark_test(uid, r_haiku, status="ok")
    db.model_api_route_activate(uid, r_sonnet)

    roster = [e for e in db.list_agent_runtime_enabled_users() if e["user_id"] == uid]
    assert len(roster) == 1
    assert roster[0]["model"] == "claude-sonnet-4-5"
    assert roster[0]["driver"] == "claude"       # anthropic → claude
    assert roster[0]["provider"] == "anthropic"
    assert roster[0]["reasoning_effort"] == "high"

    # 切到 haiku 后 roster 跟着换
    db.model_api_route_activate(uid, r_haiku)
    roster = [e for e in db.list_agent_runtime_enabled_users() if e["user_id"] == uid]
    assert roster[0]["model"] == "claude-haiku-4-5"
    assert roster[0]["reasoning_effort"] == ""


def test_roster_excludes_untested_active_route(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)   # test_status 仍是 untested

    assert [e for e in db.list_agent_runtime_enabled_users() if e["user_id"] == uid] == []


def test_roster_gateway_providers_gated_by_flag(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid, provider="gemini")
    r = db.model_api_route_upsert(uid, cid, "gemini-2.5-flash", None)
    db.model_api_route_mark_test(uid, r, status="ok")
    db.model_api_route_activate(uid, r)

    assert [e for e in db.list_agent_runtime_enabled_users() if e["user_id"] == uid] == []
    gated = [e for e in db.list_agent_runtime_enabled_users(include_gateway=True)
             if e["user_id"] == uid]
    assert len(gated) == 1
    assert gated[0]["driver"] == "codex"
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python -m pytest tests/test_model_api_profiles_db.py -k roster -q
```

预期：FAIL —— 旧 SQL 读 `user_blobs`，新表里的 route 不会出现在 roster 里。

- [ ] **Step 3: 替换 SQL**

改 `backend/db.py` 里 `list_agent_runtime_enabled_users` 的查询体。docstring 里「配了能 fit 的 provider 且 test_status='ok'」那句改成「active route 且 test_status='ok'」，其余保留（driver CASE 与 `hosted/agent_runtime_cutover.driver_for_provider` 同步的告诫必须留着）。

```python
            rows = conn.execute(
                """
                SELECT r.user_id,
                  CASE LOWER(c.provider)
                    WHEN 'anthropic' THEN 'claude'
                    WHEN 'claude'    THEN 'claude'
                    WHEN 'deepseek'  THEN 'claude'
                    ELSE 'codex'
                  END AS driver,
                  LOWER(c.provider) AS provider,
                  r.model AS model,
                  c.base_url AS base_url,
                  c.supports_responses AS supports_responses,
                  COALESCE(r.reasoning_effort, '') AS reasoning_effort
                FROM model_api_routes r
                JOIN model_api_credentials c ON c.id = r.credential_id
                WHERE r.is_active
                  AND r.test_status = 'ok'
                  AND LOWER(c.provider) = ANY(%s)
                ORDER BY r.user_id
                """,
                (providers,),
            ).fetchall()
        return [{"user_id": uid, "driver": driver, "provider": provider,
                 "model": model, "base_url": base_url,
                 "supports_responses": bool(supports_responses),
                 "reasoning_effort": reasoning_effort}
                for uid, driver, provider, model, base_url, supports_responses, reasoning_effort in rows]
```

⚠️ `supports_responses` 从前是 TEXT 比较（`== "true"`），现在是真 BOOLEAN 列 → 改成 `bool(...)`。

- [ ] **Step 4: 跑测试确认全绿**

```bash
python -m pytest tests/test_model_api_profiles_db.py -q
```

预期：全 PASS。

- [ ] **Step 5: 跑 supervisor 相关的既有测试，确认没打破**

```bash
python -m pytest tests/ -q -k "supervisor or agent_runtime or roster"
```

预期：零新增失败。若有既有测试直接 `db.set_blob(uid, "model_api", ...)` 造 roster 数据，把它改成用 Task 2 的 db 函数造 credential + route。

- [ ] **Step 6: Commit**

```bash
git add backend/db.py tests/test_model_api_profiles_db.py
git commit -m "feat(model-api): roster SQL reads active route via join"
```

---

### Task 4: config_store 读侧改造

**Files:**
- Modify: `backend/hosted/config_store.py`（`_load_runtime_provider_config:311`、`record_runtime_error:211`；新增 `load_active_route`）
- Test: `tests/test_model_api_profiles_routes.py`（新建，本任务只填第一组）

**Interfaces:**
- Consumes: `db.model_api_active_route`、`db.model_api_route_mark_runtime_error`
- Produces:
  - `config_store.load_active_route(store: UserStore) -> dict | None`（带 envelope）
  - `_load_runtime_provider_config` 签名与返回值**不变**：`(store, api_key, *, runtime_token="") -> ProviderConfig | tuple[None, dict]`

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_model_api_profiles_routes.py`：

```python
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
import provider_client
from core import enclave as core_enclave
from hosted import config_store as hosted_config_store

_ENV = {"v": 1, "body_ct": "ct", "nonce": "n"}


@pytest.fixture
def fake_enclave(monkeypatch):
    """envelope → 明文 key。patch 打在定义模块 core.enclave 上（见 CONTRIBUTING §6）。"""
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, api_key, purpose="", **kw: b"sk-plain-key",
    )


def _cred(uid, provider="anthropic", base_url=""):
    return db.model_api_credential_create(
        uid, provider=provider, base_url=base_url, label="key A",
        api_key_envelope=_ENV, api_key_hint="sk-a...451", supports_responses=False)


def test_load_runtime_provider_config_uses_active_route(
        backend_env, registered_user, user_store, fake_enclave):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r_sonnet = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    r_haiku = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    db.model_api_route_mark_test(uid, r_sonnet, status="ok")
    db.model_api_route_mark_test(uid, r_haiku, status="ok")
    db.model_api_route_activate(uid, r_haiku)

    cfg = hosted_config_store._load_runtime_provider_config(user_store, "api-key")
    assert isinstance(cfg, provider_client.ProviderConfig)
    assert cfg.model == "claude-haiku-4-5"
    assert cfg.api_key == "sk-plain-key"
    assert cfg.provider == "anthropic"


def test_load_runtime_provider_config_without_active_route(backend_env, registered_user, user_store):
    result = hosted_config_store._load_runtime_provider_config(user_store, "api-key")
    assert result == (None, {"error": "model_api_not_configured"})


def test_load_runtime_provider_config_rejects_untested_active(
        backend_env, registered_user, user_store, fake_enclave):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    cfg, err = hosted_config_store._load_runtime_provider_config(user_store, "api-key")
    assert cfg is None
    assert err["error"] == "model_api_not_tested"
    assert err["test_status"] == "untested"


def test_record_runtime_error_writes_active_route(backend_env, registered_user, user_store):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    r = db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)
    db.model_api_route_activate(uid, r)

    body, status = hosted_config_store.record_runtime_error(
        user_store, error="402 insufficient balance", error_class="provider_402")
    assert status == 200
    assert db.model_api_route_get(uid, r)["last_runtime_error"] == "402 insufficient balance"


def test_record_runtime_error_without_active_route_returns_404(
        backend_env, registered_user, user_store):
    body, status = hosted_config_store.record_runtime_error(
        user_store, error="x", error_class="y")
    assert status == 404
    assert body["error"] == "model_api_runtime_profile_missing"
```

`user_store` fixture：若 `tests/conftest.py` 没有，加

```python
@pytest.fixture
def user_store(registered_user):
    from core.store import get_store
    return get_store(registered_user["user_id"])
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python -m pytest tests/test_model_api_profiles_routes.py -q
```

预期：FAIL —— 旧实现读 `user_blobs` blob，返回 `model_api_not_configured`。

- [ ] **Step 3: 实现**

在 `backend/hosted/config_store.py` 里，`_load_model_api_config` 附近新增：

```python
def load_active_route(store: UserStore) -> dict | None:
    """当前生效的 route（含其 credential 的 api_key_envelope）。

    这是 hosted 线读 model_api 配置的唯一入口。返回形状见 db.model_api_active_route。
    """
    return db.model_api_active_route(store.user_id)
```

把 `_load_runtime_provider_config` 整体替换为：

```python
def _load_runtime_provider_config(store: UserStore, api_key: str | None, *, runtime_token: str = "") -> provider_client.ProviderConfig | tuple[None, dict]:
    route = load_active_route(store)
    if not route:
        return None, {"error": "model_api_not_configured"}
    if route.get("test_status") != "ok":
        return None, {"error": "model_api_not_tested", "test_status": route.get("test_status", "")}
    envelope = route.get("api_key_envelope")
    if not isinstance(envelope, dict):
        return None, {"error": "model_api_key_envelope_missing"}
    # A hosted (host-all) turn authenticates with a runtime token, not the
    # long-term api_key — forward it so the enclave can authorize the unwrap.
    # The enclave's /v1/envelope/decrypt accepts either credential. Only pass
    # runtime_token through when present, so api-key callers are unchanged.
    decrypt_kwargs = {"runtime_token": runtime_token} if runtime_token else {}
    try:
        provider_key = core_enclave._decrypt_envelope_via_enclave(
            envelope,
            api_key,
            purpose="model_api_provider_key",
            **decrypt_kwargs,
        ).decode("utf-8")
    except Exception as e:
        return None, {"error": "model_api_key_decrypt_failed", "detail": str(e)[:220]}
    try:
        return _provider_config_from_plain(route, provider_key)
    except provider_client.ProviderError as e:
        return None, {"error": "model_api_config_invalid", "detail": str(e)}
```

`_provider_config_from_plain` 不用改：route dict 已经带 `provider` / `model` / `base_url` 三个键。

把 `record_runtime_error` 的**持久化那一步**替换掉 —— 其余原样保留：

```python
def record_runtime_error(store: UserStore, *, error: str, error_class: str = "") -> tuple[dict, int]:
    """agent-runner consumer 上报（或清空）最近一次回合失败原因。

    写 active route 行。读侧是 setup_core 的 last_runtime_error（iOS 设置页）与
    GET /v1/model_api/routes。legacy inline 路径经 action-trace 写同一字段。
    """
    if not db.model_api_route_mark_runtime_error(
            store.user_id, error=error, error_class=error_class):
        return {"error": "model_api_runtime_profile_missing"}, 404
    try:
        # ⚠️ 原函数尾部的 notices 扇出块必须一行不改地保留：
        #   error 非空 → notices_core.emit(store, source="chat", error_class=ec, ...)
        #   error 为空 → notices_core.resolve(store, "chat:")
        # 漏掉它会让 chat 的错误不再进通知中心（test_chat_notice_fanout.py 会红）。
        ...
    except Exception:
        pass   # 扇出绝不影响主职责
    return {"ok": True}, 200      # ⚠️ 是 {"ok": True}，不是 {"status": "ok"}
```

> **本任务必须与 Task 5 合并执行。** 单独做 Task 4 会让读侧（active route）与写侧（仍写 blob）错配，`GET /v1/model_api/runtime` 和所有走 `POST /v1/model_api/setup` 造数据的端到端测试（`_setup_openrouter` 那一类 helper）全部失败。把它们 xfail 掉意味着整个过渡窗口里这些测试失去保护。读写侧一起迁移则无中间断裂，xfail 归零。

`_patch_model_api_runtime_profile` 里对 `last_runtime_error` / `last_runtime_error_class` 的写入不再需要，但**不要删 `model_api_runtime` blob 本身**——它还存着 rollout flags 与 `last_action_trace_*`。

- [ ] **Step 4: 跑测试确认全绿**

```bash
python -m pytest tests/test_model_api_profiles_routes.py -q
```

预期：全 PASS。

- [ ] **Step 5: 跑 hosted 线既有测试**

```bash
python -m pytest tests/ -q -k "hosted or chat_send or history_import or genesis"
```

预期：零新增失败。`_load_runtime_provider_config` 的 5 个调用方（`chat_send_core:60`、`setup_core:239`、`setup_core:376`、`history_import:2942`、`genesis/plaintext:1222`）签名未变，应全绿。若某测试用 `db.set_blob(uid,"model_api",...)` 造数据，改成用 db 函数造 credential + route。

- [ ] **Step 6: pyflakes + commit**

```bash
python -m pyflakes backend/hosted
git add backend/hosted/config_store.py tests/test_model_api_profiles_routes.py
git commit -m "feat(model-api): config_store reads active route"
```

---

### Task 5: setup_core 既有端点改读新表 + setup 幂等 upsert

**Files:**
- Modify: `backend/hosted/setup_core.py`（`model_api_setup:63`、`model_api_get:198`、`model_api_set_hosting:202`、`model_api_key_envelope:221`、`model_api_test`、`model_api_delete`、`model_api_runtime_status`）
- Test: `tests/test_model_api_profiles_routes.py`（追加）

**Interfaces:**
- Consumes: Task 2 db 函数、Task 4 的 `config_store.load_active_route`
- Produces:
  - `setup_core._public_route(route: dict) -> dict`（route → `GET /get` 那套扁平投影 + `configured: True`）
  - 既有 7 个端点函数签名不变

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_model_api_profiles_routes.py`：

```python
@pytest.fixture
def fake_provider(monkeypatch):
    """测活恒成功。patch 打在定义模块 provider_client 上。"""
    monkeypatch.setattr(provider_client, "test_provider_key",
                        lambda cfg: {"usage": {"total_tokens": 1}})
    monkeypatch.setattr(provider_client, "probe_responses_support", lambda cfg: False)


@pytest.fixture
def fake_envelope(monkeypatch):
    from core import envelope as core_envelope
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store",
                        lambda store, data, item_id="": (_ENV, None))


def test_setup_is_idempotent_and_does_not_accumulate_routes(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = {"X-API-Key": registered_user["api_key"]}
    body = {"provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"}

    for _ in range(3):
        resp = client.post("/v1/model_api/setup", json=body, headers=headers)
        assert resp.status_code == 200, resp.text

    assert len(db.model_api_credentials_list(uid)) == 1
    assert len(db.model_api_routes_list(uid)) == 1


def test_setup_second_model_same_key_adds_route_reuses_credential(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = {"X-API-Key": registered_user["api_key"]}

    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5", "api_key": "sk-ant-xxx"})

    assert len(db.model_api_credentials_list(uid)) == 1
    routes = db.model_api_routes_list(uid)
    assert len(routes) == 2
    # 后 setup 的那条是 active
    assert [r["model"] for r in routes if r["is_active"]] == ["claude-haiku-4-5"]


def test_get_returns_active_route_projection(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    headers = {"X-API-Key": registered_user["api_key"]}
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5",
        "api_key": "sk-ant-xxx", "reasoning_effort": "high"})

    cfg = client.get("/v1/model_api/get", headers=headers).json()["config"]
    assert cfg["configured"] is True
    assert cfg["provider"] == "anthropic"
    assert cfg["model"] == "claude-sonnet-4-5"
    assert cfg["test_status"] == "ok"
    assert cfg["reasoning_effort"] == "high"
    assert "api_key_envelope" not in cfg


def test_get_without_config_returns_unconfigured(client, registered_user):
    headers = {"X-API-Key": registered_user["api_key"]}
    cfg = client.get("/v1/model_api/get", headers=headers).json()["config"]
    assert cfg == {"configured": False}


def test_key_envelope_returns_active_credential_envelope(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    headers = {"X-API-Key": registered_user["api_key"]}
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})

    resp = client.get("/v1/model_api/key_envelope", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["api_key_envelope"] == _ENV


def test_delete_removes_all_credentials_and_routes(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = {"X-API-Key": registered_user["api_key"]}
    client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-ant-xxx"})

    assert client.delete("/v1/model_api/delete", headers=headers).status_code == 200
    assert db.model_api_credentials_list(uid) == []
    assert db.model_api_routes_list(uid) == []
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python -m pytest tests/test_model_api_profiles_routes.py -k "setup or get_ or key_envelope or delete" -q
```

预期：FAIL —— 旧 `model_api_setup` 写 blob，`db.model_api_credentials_list` 返回空。

- [ ] **Step 3: 实现 `_public_route` 与 setup 的 upsert 主体**

在 `backend/hosted/setup_core.py` 顶部新增：

```python
def _public_route(route: dict | None) -> dict:
    """active route → GET /v1/model_api/get 的扁平投影（与旧 blob 投影同形）。"""
    if not route:
        return {"configured": False}
    safe = {
        "provider": route["provider"],
        "model": route["model"],
        "base_url": route["base_url"],
        "api_key_hint": route["api_key_hint"],
        "test_status": route["test_status"],
        "last_test_at": route["last_test_at"],
        "last_test_error": route["last_test_error"],
        "configured": True,
        "privacy_mode": "tdx_cvm_backend_runtime_option_a",
    }
    if route.get("reasoning_effort"):
        safe["reasoning_effort"] = route["reasoning_effort"]
    return safe
```

`model_api_setup` 的主体改为（保留原有的 provider 校验、信封构建、测活、supports_responses 探测、notices 扇出，只换掉持久化部分）：

```python
def model_api_setup(store, payload: dict, *, caller_api_key: str | None) -> tuple[dict, int]:
    provider = str(payload.get("provider") or "")
    model = str(payload.get("model") or "")
    base_url = str(payload.get("base_url") or "")
    raw_key = str(payload.get("api_key") or "").strip()
    try:
        reasoning_effort = _normalize_reasoning_effort(payload.get("reasoning_effort"))
    except ValueError as e:
        return {"error": "invalid_reasoning_effort", "detail": str(e)}, 400
    try:
        provider, model, base_url = provider_client.validate_config(provider, model, base_url)
    except provider_client.ProviderError as e:
        return {"error": str(e)}, 400

    # 幂等锚点：当前 active route 的 credential。若它的 (provider, base_url) 与
    # 请求匹配，就复用/更新它；否则新建一条。credentials 没有唯一索引（同 provider
    # 允许多把 key），所以幂等必须在这里用代码保证，不能靠 ON CONFLICT。
    active = hosted_config_store.load_active_route(store)
    reuse = bool(active
                 and active["provider"] == provider
                 and active["base_url"] == base_url)
    existing = None
    if reuse:
        existing = db.model_api_credential_get(store.user_id, active["credential_id"])

    # raw_key 为空 → 复用 existing 的信封（「换 model 不重输 key」的路径）
    provider_key, envelope, api_key_hint = _resolve_provider_key(
        store, raw_key, existing, caller_api_key)
    if provider_key is None:
        return envelope, api_key_hint      # (error_body, status)

    try:
        test = provider_client.test_provider_key(
            provider_client.ProviderConfig(provider, model, provider_key, base_url))
    except provider_client.ProviderError as e:
        print(
            f"[model_api:{store.user_id}] setup FAILED provider={provider} "
            f"model={model} status_code={e.status_code} detail={str(e)[:160]}"
        )
        return {"error": "provider_test_failed", "detail": str(e),
                "status_code": e.status_code}, 400

    supports_responses = False
    if provider == "openai_compatible":
        supports_responses = provider_client.probe_responses_support(
            provider_client.ProviderConfig(provider, model, provider_key, base_url))
        print(
            f"[model_api:{store.user_id}] openai_compatible /responses probe -> "
            f"supports={supports_responses} base_url={base_url}"
        )

    if reuse and existing:
        credential_id = existing["id"]
        db.model_api_credential_update(
            store.user_id, credential_id,
            api_key_envelope=envelope, api_key_hint=api_key_hint,
            supports_responses=supports_responses)
    else:
        credential_id = db.model_api_credential_create(
            store.user_id, provider=provider, base_url=base_url,
            label=provider.replace("_", " ").title(),
            api_key_envelope=envelope, api_key_hint=api_key_hint,
            supports_responses=supports_responses)
        if not credential_id:
            return {"error": "model_api_credential_write_failed"}, 500

    route_id = db.model_api_route_upsert(store.user_id, credential_id, model, reasoning_effort)
    if not route_id:
        return {"error": "model_api_route_write_failed"}, 500
    db.model_api_route_mark_test(store.user_id, route_id, status="ok")
    db.model_api_route_activate(store.user_id, route_id)

    accounts_onboarding._save_onboarding_route(store, "model_api")
    print(f"[model_api:{store.user_id}] setup provider={provider} model={model}")

    warnings = _emit_responses_support_notice(store, provider, supports_responses, base_url)

    route = hosted_config_store.load_active_route(store)
    resp = {"status": "configured", "config": _public_route(route)}
    if warnings:
        resp["warnings"] = warnings
    return resp, 200
```

两个抽出来的 helper（放在 `model_api_setup` 之前）：

```python
def _resolve_provider_key(store, raw_key: str, existing: dict | None,
                          caller_api_key: str | None):
    """返回 (provider_key, envelope, api_key_hint)；失败时返回 (None, error_body, status)。

    raw_key 非空 → 新封一个信封。raw_key 为空 → 复用 existing credential 的信封，
    经 enclave 解出明文用于测活（这是「换 model 不重输 key」的路径）。
    """
    if raw_key:
        envelope, err = core_envelope._build_shared_envelope_for_store(
            store, raw_key.encode("utf-8"), item_id=f"model_api_key_{uuid.uuid4().hex}")
        if envelope is None:
            return None, {
                "error": "cannot_encrypt_provider_key",
                "detail": err,
                "required": (
                    "The user must have a content public key and the enclave "
                    "attestation endpoint must be reachable before saving a provider key."
                ),
            }, 409
        return raw_key, envelope, provider_client.mask_api_key(raw_key)

    existing_envelope = (existing or {}).get("api_key_envelope")
    if not isinstance(existing_envelope, dict):
        return None, {"error": "api_key required"}, 400
    try:
        provider_key = core_enclave._decrypt_envelope_via_enclave(
            existing_envelope, caller_api_key, purpose="model_api_provider_key",
        ).decode("utf-8")
    except Exception as e:
        return None, {"error": "model_api_key_decrypt_failed", "detail": str(e)[:220]}, 400
    return provider_key, existing_envelope, str(existing.get("api_key_hint") or "saved key")


def _emit_responses_support_notice(store, provider: str, supports_responses: bool,
                                   base_url: str) -> list[dict]:
    """openai_compatible 中转不实现 /v1/responses → LiteLLM 强制 chat-completions 桥接
    → mangle codex 工具循环 → 记忆/工具静默不可靠。配置期就能预知，双写:
      ① setup 响应带 warnings → 设置页保存后当场显示
      ② 通知中心 emit → 持久化
    换到支持 /v1/responses 的中转(或非 openai_compatible provider)时 resolve。
    """
    warnings: list[dict] = []
    try:
        from notices import core as notices_core
        from notices import catalog as notices_catalog
        _ec = "responses_unsupported"
        if provider == "openai_compatible" and not supports_responses:
            _blame = notices_catalog.blame_for(_ec)
            _text = notices_catalog.user_text_for(_ec)
            warnings.append({"error_class": _ec, "blame": _blame,
                             "severity": "warning", "user_text": _text})
            notices_core.emit(
                store, source="model_api", error_class=_ec,
                blame=_blame, severity="warning", user_text=_text,
                detail=f"probe /v1/responses -> supported=False (base_url={base_url})",
                dedupe_key=f"model_api:{_ec}")
        else:
            notices_core.resolve(store, f"model_api:{_ec}")
    except Exception:
        pass  # 扇出绝不影响 setup 主职责
    return warnings
```

⚠️ `_resolve_provider_key` 的返回元数是 3（成功）或 3（失败：`None, body, status`）。`model_api_setup` 里的解包 `provider_key, envelope, api_key_hint = ...` 在失败时把 body 绑给 `envelope`、status 绑给 `api_key_hint`，然后 `return envelope, api_key_hint`。这依赖两条路径都返回 3 元组——**务必保持**，否则解包爆 ValueError。

本任务不需要新增 db 函数：幂等锚点是 `hosted_config_store.load_active_route(store)` +
`db.model_api_credential_get(...)`，两者 Task 2 / Task 4 都已提供。

`setup_core.py` 顶部需要 `import db`（已有）。

- [ ] **Step 4: 改其余 6 个端点**

```python
def model_api_get(store) -> tuple[dict, int]:
    return {"config": _public_route(hosted_config_store.load_active_route(store))}, 200


def model_api_set_hosting(store) -> tuple[dict, int]:
    """报告该用户派生的 agent driver。AGENT 由 provider 自动派生，配了即托管；
    本端点不再有 enable/disable 开关（保留以兼容旧 client）。"""
    route = hosted_config_store.load_active_route(store)
    if not route:
        return {"error": "model_api_not_configured"}, 404
    try:
        driver = agent_runtime_cutover.resolve_driver(route)
    except agent_runtime_cutover.UnsupportedProviderError:
        return {"error": "provider_not_hostable"}, 409
    print(f"[model_api:{store.user_id}] provider={route.get('provider')} -> driver={driver}")
    return {"status": "ok", "enabled": True, "driver": driver,
            "config": _public_route(route)}, 200


def model_api_key_envelope(store) -> tuple[dict, int]:
    """Return the caller's OWN ``api_key_envelope`` ciphertext (active credential).
    The server never decrypts it; only the enclave can."""
    route = hosted_config_store.load_active_route(store)
    if not route:
        return {"error": "model_api_not_configured"}, 404
    envelope = route.get("api_key_envelope")
    if not isinstance(envelope, dict):
        return {"error": "model_api_key_envelope_missing"}, 404
    return {"api_key_envelope": envelope}, 200
```

`model_api_test`（`setup_core.py:235`）整体替换。它原先走 `_load_runtime_provider_config`，那个函数有 `test_status != "ok"` 的 gate——意味着老 `/test` 端点**测不了** untested 的配置。改用 Task 6 的 `_test_route_or_error`（直接解 envelope 测活，无 gate）后这个死角消失，是行为改善：

```python
def model_api_test(store, *, api_key: str | None) -> tuple[dict, int]:
    route = hosted_config_store.load_active_route(store)
    if not route:
        return {"error": "model_api_not_configured"}, 404
    err = _test_route_or_error(store, route, api_key)
    if err is not None:
        return err
    print(f"[model_api:{store.user_id}] test ok provider={route['provider']} model={route['model']}")
    return {"status": "ok",
            "config": _public_route(hosted_config_store.load_active_route(store))}, 200
```

`_test_route_or_error` 定义在 Task 6。若严格按任务顺序执行，本步先内联一份等价实现，Task 6 再抽出去；或把 Task 5 的这一小步挪到 Task 6 一起做。推荐后者。

`model_api_delete`：改为

```python
def model_api_delete(store) -> tuple[dict, int]:
    for cred in db.model_api_credentials_list(store.user_id):
        db.model_api_credential_delete(store.user_id, cred["id"])   # CASCADE 带走 routes
    db.delete_blob(store.user_id, hosted_config_store.MODEL_API_RUNTIME_BLOB)
    return {"status": "deleted"}, 200
```

`model_api_runtime_status`：`last_runtime_error` / `last_runtime_error_class` 两个字段改从 active route 读：

```python
    route = hosted_config_store.load_active_route(store) or {}
    ...
        "last_runtime_error": route.get("last_runtime_error", ""),
        "last_runtime_error_class": route.get("last_runtime_error_class", ""),
```

其余字段（`last_action_trace_*`）继续从 `model_api_runtime` blob 的 `profile` 读。

`agent_runtime_cutover.resolve_driver(route)` 只读 `route["provider"]`，route dict 有这个键，无需改 cutover。

- [ ] **Step 5: 跑测试确认全绿**

```bash
python -m pytest tests/test_model_api_profiles_routes.py -q
python -m pytest tests/ -q -k "model_api or setup"
```

预期：零新增失败。

- [ ] **Step 6: 确认 setup_core.py 没破 800 行红线**

```bash
wc -l backend/hosted/setup_core.py
```

若超 800，把新加的集合端点（Task 6）拆到 `backend/hosted/profiles_core.py`，在 PR 描述里说明。

- [ ] **Step 7: pyflakes + commit**

```bash
python -m pyflakes backend/hosted backend/db.py
git add backend/hosted/setup_core.py backend/db.py tests/test_model_api_profiles_routes.py
git commit -m "feat(model-api): setup becomes idempotent upsert; endpoints read new tables"
```

---

### Task 6: 新集合端点的 core 实现

**Files:**
- Modify: `backend/hosted/setup_core.py`
- Test: `tests/test_model_api_profiles_routes.py`（追加）

**Interfaces:**
- Consumes: Task 2 db 函数、Task 5 的 `_public_route` / `_resolve_provider_key`
- Produces:

```python
def model_api_routes_get(store) -> tuple[dict, int]
def model_api_route_create(store, payload: dict, *, caller_api_key: str | None) -> tuple[dict, int]
def model_api_route_activate(store, route_id: str, *, caller_api_key: str | None) -> tuple[dict, int]
def model_api_route_test(store, route_id: str, *, api_key: str | None) -> tuple[dict, int]
def model_api_route_remove(store, route_id: str) -> tuple[dict, int]
def model_api_credential_patch(store, credential_id: str, payload: dict, *, caller_api_key: str | None) -> tuple[dict, int]
def model_api_credential_remove(store, credential_id: str) -> tuple[dict, int]
```

新 slug（Task 7 登记到 `docs/API_ERRORS.md`）：`route_not_found`(404)、`credential_not_found`(404)、`api_key_or_credential_id_required`(400)。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_model_api_profiles_routes.py`：

```python
def _setup_one(client, registered_user, model="claude-sonnet-4-5"):
    headers = {"X-API-Key": registered_user["api_key"]}
    resp = client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "anthropic", "model": model, "api_key": "sk-ant-xxx"})
    assert resp.status_code == 200, resp.text
    return headers


def test_routes_list_shape_matches_ios_contract(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    headers = _setup_one(client, registered_user)
    body = client.get("/v1/model_api/routes", headers=headers).json()

    assert body["active_route_id"]
    assert len(body["routes"]) == 1
    r = body["routes"][0]
    for key in ("id", "credential_id", "provider", "model", "credential_label",
                "api_key_hint", "base_url", "test_status", "last_test_at",
                "last_test_error", "last_runtime_error", "last_runtime_error_class"):
        assert key in r, key
    assert "api_key_envelope" not in r      # 密文绝不出现在响应里


def test_create_route_reusing_credential(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]

    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5", "credential_id": cid})
    assert resp.status_code == 200, resp.text

    assert len(db.model_api_credentials_list(uid)) == 1
    assert len(db.model_api_routes_list(uid)) == 2
    # 未带 activate → 仍是原来那条 active
    assert db.model_api_active_route(uid)["model"] == "claude-sonnet-4-5"


def test_create_route_requires_key_or_credential_id(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    headers = {"X-API-Key": registered_user["api_key"]}
    resp = client.post("/v1/model_api/routes", headers=headers, json={
        "provider": "anthropic", "model": "claude-haiku-4-5"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "api_key_or_credential_id_required"


def test_activate_untested_route_runs_test_and_switches(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)

    resp = client.post(f"/v1/model_api/routes/{r2}/activate", headers=headers)
    assert resp.status_code == 200, resp.text
    assert db.model_api_active_route(uid)["id"] == r2
    assert db.model_api_route_get(uid, r2)["test_status"] == "ok"


def test_activate_fails_when_provider_test_fails_and_keeps_old_active(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    old_active = db.model_api_active_route(uid)["id"]
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "bad-model", None)

    def _boom(cfg):
        raise provider_client.ProviderError("provider_http_404", status_code=404)
    monkeypatch.setattr(provider_client, "test_provider_key", _boom)

    resp = client.post(f"/v1/model_api/routes/{r2}/activate", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"] == "provider_test_failed"
    assert resp.json()["status_code"] == 404

    assert db.model_api_active_route(uid)["id"] == old_active     # 旧 active 纹丝不动
    assert db.model_api_route_get(uid, r2)["test_status"] == "failed"


def test_activate_unknown_route_404(client, registered_user, fake_provider,
                                    fake_envelope, fake_enclave):
    headers = _setup_one(client, registered_user)
    resp = client.post(
        "/v1/model_api/routes/00000000-0000-0000-0000-000000000000/activate",
        headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"] == "route_not_found"


def test_delete_active_route_autoselects_latest_ok(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    r2 = db.model_api_route_upsert(uid, cid, "claude-haiku-4-5", None)
    db.model_api_route_mark_test(uid, r2, status="ok")
    active = db.model_api_active_route(uid)["id"]

    resp = client.delete(f"/v1/model_api/routes/{active}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["active_route_id"] == r2
    assert db.model_api_active_route(uid)["id"] == r2


def test_delete_last_route_leaves_no_active(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    active = db.model_api_active_route(uid)["id"]

    resp = client.delete(f"/v1/model_api/routes/{active}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["active_route_id"] is None
    assert db.model_api_active_route(uid) is None


def test_patch_credential_rotating_key_retests_active_route(
        client, registered_user, fake_provider, fake_envelope, fake_enclave):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]

    resp = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers,
                        json={"api_key": "sk-ant-new", "label": "Key B"})
    assert resp.status_code == 200, resp.text

    creds = db.model_api_credentials_list(uid)
    assert creds[0]["label"] == "Key B"
    assert db.model_api_active_route(uid)["test_status"] == "ok"


def test_patch_credential_keeps_old_key_when_retest_fails(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]
    old_hint = db.model_api_credentials_list(uid)[0]["api_key_hint"]

    def _boom(cfg):
        raise provider_client.ProviderError("provider_http_401", status_code=401)
    monkeypatch.setattr(provider_client, "test_provider_key", _boom)

    resp = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers,
                        json={"api_key": "sk-ant-dead"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "provider_test_failed"

    assert db.model_api_credentials_list(uid)[0]["api_key_hint"] == old_hint
    assert db.model_api_active_route(uid)["test_status"] == "ok"


def test_patch_credential_label_only_does_not_retest(
        client, registered_user, fake_provider, fake_envelope, fake_enclave, monkeypatch):
    uid = registered_user["user_id"]
    headers = _setup_one(client, registered_user)
    cid = db.model_api_credentials_list(uid)[0]["id"]

    def _boom(cfg):
        raise AssertionError("label-only patch must not call the provider")
    monkeypatch.setattr(provider_client, "test_provider_key", _boom)

    resp = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers,
                        json={"label": "Renamed"})
    assert resp.status_code == 200
    assert db.model_api_credentials_list(uid)[0]["label"] == "Renamed"
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python -m pytest tests/test_model_api_profiles_routes.py -k "routes_list or create_route or activate or delete_active or delete_last or patch_credential" -q
```

预期：全 FAIL（404 —— 路由还不存在）。

- [ ] **Step 3: 实现**

追加到 `backend/hosted/setup_core.py`：

```python
def _test_route_or_error(store, route: dict, caller_api_key: str | None):
    """对一条 route 跑真实测活。成功回写 test_status='ok' 并返回 None；
    失败回写 'failed' 并返回 (error_body, status)。"""
    envelope = route.get("api_key_envelope")
    if not isinstance(envelope, dict):
        cred = db.model_api_credential_get(store.user_id, route["credential_id"])
        envelope = (cred or {}).get("api_key_envelope")
    if not isinstance(envelope, dict):
        return {"error": "model_api_key_envelope_missing"}, 404
    try:
        provider_key = core_enclave._decrypt_envelope_via_enclave(
            envelope, caller_api_key, purpose="model_api_provider_key").decode("utf-8")
    except Exception as e:
        return {"error": "model_api_key_decrypt_failed", "detail": str(e)[:220]}, 400
    try:
        provider_client.test_provider_key(provider_client.ProviderConfig(
            route["provider"], route["model"], provider_key, route["base_url"]))
    except provider_client.ProviderError as e:
        db.model_api_route_mark_test(store.user_id, route["id"], status="failed", error=str(e))
        print(
            f"[model_api:{store.user_id}] route test FAILED provider={route['provider']} "
            f"model={route['model']} status_code={e.status_code} detail={str(e)[:160]}"
        )
        return {"error": "provider_test_failed", "detail": str(e),
                "status_code": e.status_code}, 400
    db.model_api_route_mark_test(store.user_id, route["id"], status="ok")
    return None


def model_api_routes_get(store) -> tuple[dict, int]:
    routes = db.model_api_routes_list(store.user_id)
    active = next((r["id"] for r in routes if r["is_active"]), None)
    return {"active_route_id": active, "routes": routes}, 200


def model_api_route_create(store, payload: dict, *, caller_api_key: str | None) -> tuple[dict, int]:
    provider = str(payload.get("provider") or "")
    model = str(payload.get("model") or "")
    base_url = str(payload.get("base_url") or "")
    raw_key = str(payload.get("api_key") or "").strip()
    credential_id = str(payload.get("credential_id") or "").strip()
    activate = bool(payload.get("activate"))
    try:
        reasoning_effort = _normalize_reasoning_effort(payload.get("reasoning_effort"))
    except ValueError as e:
        return {"error": "invalid_reasoning_effort", "detail": str(e)}, 400
    if bool(raw_key) == bool(credential_id):
        return {"error": "api_key_or_credential_id_required",
                "detail": "supply exactly one of api_key / credential_id"}, 400
    try:
        provider, model, base_url = provider_client.validate_config(provider, model, base_url)
    except provider_client.ProviderError as e:
        return {"error": str(e)}, 400

    if credential_id:
        cred = db.model_api_credential_get(store.user_id, credential_id)
        if not cred:
            return {"error": "credential_not_found"}, 404
        base_url = cred["base_url"]
        provider = cred["provider"]
    else:
        envelope, err = core_envelope._build_shared_envelope_for_store(
            store, raw_key.encode("utf-8"), item_id=f"model_api_key_{uuid.uuid4().hex}")
        if envelope is None:
            return {"error": "cannot_encrypt_provider_key", "detail": err}, 409
        # 显式带 api_key 就是「新建一把凭据」，总是插新行 —— 同 provider 允许多把 key。
        credential_id = db.model_api_credential_create(
            store.user_id, provider=provider, base_url=base_url,
            label=str(payload.get("label") or provider.replace("_", " ").title()),
            api_key_envelope=envelope,
            api_key_hint=provider_client.mask_api_key(raw_key),
            supports_responses=False)
        if not credential_id:
            return {"error": "model_api_credential_write_failed"}, 500

    route_id = db.model_api_route_upsert(store.user_id, credential_id, model, reasoning_effort)
    if not route_id:
        return {"error": "model_api_route_write_failed"}, 500

    if activate:
        return model_api_route_activate(store, route_id, caller_api_key=caller_api_key)
    return {"route": db.model_api_route_get(store.user_id, route_id)}, 200


def model_api_route_activate(store, route_id: str, *, caller_api_key: str | None) -> tuple[dict, int]:
    """先同步测活，通过才切换。测不过 → 400，旧 active 纹丝不动。

    为什么必须 gate：db.list_agent_runtime_enabled_users 只收 is_active AND
    test_status='ok' 的用户。激活一条未测活的 route 会让该用户下个 tick 从 roster
    消失，supervisor 走「用户离开 roster」分支杀掉 consumer 且不会自愈。
    """
    route = db.model_api_route_get(store.user_id, route_id)
    if not route:
        return {"error": "route_not_found"}, 404

    err = _test_route_or_error(store, route, caller_api_key)
    if err is not None:
        return err

    if not db.model_api_route_activate(store.user_id, route_id):
        return {"error": "route_not_found"}, 404
    # ⚠️ 不要在这里释放 in-flight reply claim。旧 consumer 此刻还活着
    # （supervisor 要 15s 后 tick 才 kill 它），清 claim 会让它与新 consumer
    # 双跑同一回合 → 双重 provider 计费。释放在 supervisor 的 respawn 分支里
    # kill_fn 之后做（见 Task 8）。
    print(f"[model_api:{store.user_id}] activated route model={route['model']}")
    return {"active_route_id": route_id,
            "route": db.model_api_route_get(store.user_id, route_id)}, 200


def model_api_route_test(store, route_id: str, *, api_key: str | None) -> tuple[dict, int]:
    route = db.model_api_route_get(store.user_id, route_id)
    if not route:
        return {"error": "route_not_found"}, 404
    err = _test_route_or_error(store, route, api_key)
    if err is not None:
        return err
    return {"status": "ok", "route": db.model_api_route_get(store.user_id, route_id)}, 200


def model_api_route_remove(store, route_id: str) -> tuple[dict, int]:
    route = db.model_api_route_get(store.user_id, route_id)
    if not route:
        return {"error": "route_not_found"}, 404
    was_active = route["is_active"]
    if not db.model_api_route_delete(store.user_id, route_id):
        return {"error": "route_not_found"}, 404
    active_id = db.model_api_autoselect_active(store.user_id) if was_active else \
        (db.model_api_active_route(store.user_id) or {}).get("id")
    return {"status": "deleted", "active_route_id": active_id}, 200


def model_api_credential_patch(store, credential_id: str, payload: dict, *,
                               caller_api_key: str | None) -> tuple[dict, int]:
    cred = db.model_api_credential_get(store.user_id, credential_id)
    if not cred:
        return {"error": "credential_not_found"}, 404

    label = payload.get("label")
    raw_key = str(payload.get("api_key") or "").strip()

    if not raw_key:
        if label is None:
            return {"error": "nothing_to_update"}, 400
        db.model_api_credential_update(store.user_id, credential_id, label=str(label))
        return {"status": "ok"}, 200

    # 换 key：先封新信封，再对 active route（若属于本 credential）测活。测不过就整体不落库。
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store, raw_key.encode("utf-8"), item_id=f"model_api_key_{uuid.uuid4().hex}")
    if envelope is None:
        return {"error": "cannot_encrypt_provider_key", "detail": err}, 409

    active = db.model_api_active_route(store.user_id)
    if active and active["credential_id"] == credential_id:
        probe = dict(active)
        probe["api_key_envelope"] = envelope
        try:
            provider_client.test_provider_key(provider_client.ProviderConfig(
                probe["provider"], probe["model"], raw_key, probe["base_url"]))
        except provider_client.ProviderError as e:
            # 不落库：旧 key 与旧 test_status 都保持原样，用户不会掉出 roster。
            return {"error": "provider_test_failed", "detail": str(e),
                    "status_code": e.status_code}, 400

    db.model_api_credential_update(
        store.user_id, credential_id,
        label=str(label) if label is not None else None,
        api_key_envelope=envelope,
        api_key_hint=provider_client.mask_api_key(raw_key))

    # 该 credential 下的非 active route 全部退回 untested（新 key 未在它们上验证过）。
    for r in db.model_api_routes_list(store.user_id):
        if r["credential_id"] == credential_id and not r["is_active"]:
            db.model_api_route_mark_test(store.user_id, r["id"], status="untested")
    return {"status": "ok"}, 200


def model_api_credential_remove(store, credential_id: str) -> tuple[dict, int]:
    cred = db.model_api_credential_get(store.user_id, credential_id)
    if not cred:
        return {"error": "credential_not_found"}, 404
    had_active = (db.model_api_active_route(store.user_id) or {}).get("credential_id") == credential_id
    db.model_api_credential_delete(store.user_id, credential_id)   # CASCADE 带走 routes
    active_id = db.model_api_autoselect_active(store.user_id) if had_active else \
        (db.model_api_active_route(store.user_id) or {}).get("id")
    return {"status": "deleted", "active_route_id": active_id}, 200
```

`model_api_route_activate` 调用了 `db.chat_expire_reply_claims`（Task 8 实现）。为让本任务的测试先通过，Task 8 之前先在 `db.py` 加一个 no-op：

```python
def chat_expire_reply_claims(user_id: str) -> int:
    return 0
```

Task 8 再填真身。

- [ ] **Step 4: 跑测试确认全绿（路由接线在 Task 7，先跑 core 层直调）**

本任务的测试打的是 HTTP 端点，需 Task 7 才通。**把 Task 6 和 Task 7 合并执行**：先写完 core，立刻做 Task 7 的接线，再跑测试。或先用 core 直调改写断言，Task 7 再切回 HTTP。推荐前者。

- [ ] **Step 5: Commit（与 Task 7 一起）**

---

### Task 7: 路由接线 + 错误 slug 登记

**Files:**
- Modify: `backend/hosted/setup_routes_asgi.py`
- Modify: `docs/API_ERRORS.md`

**Interfaces:**
- Consumes: Task 6 的 7 个 core 函数
- Produces: 7 条新路由（PR 描述里必须列出）

- [ ] **Step 1: 加路由**

追加到 `backend/hosted/setup_routes_asgi.py`（`model_api_memory_repair` 之后、`state_receipts` 之前）：

```python
@router.get("/v1/model_api/routes")
async def model_api_routes_get(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(setup_core.model_api_routes_get, auth.store)
    return JSONResponse(body, status_code=status)


@router.post("/v1/model_api/routes")
async def model_api_route_create(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_route_create, auth.store, payload, caller_api_key=caller_api_key)
    return JSONResponse(body, status_code=status)


@router.post("/v1/model_api/routes/{route_id}/activate")
async def model_api_route_activate(route_id: str, request: Request,
                                   auth: AuthResult = Depends(require_auth)):
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_route_activate, auth.store, route_id, caller_api_key=caller_api_key)
    return JSONResponse(body, status_code=status)


@router.post("/v1/model_api/routes/{route_id}/test")
async def model_api_route_test(route_id: str, request: Request,
                               auth: AuthResult = Depends(require_auth)):
    api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_route_test, auth.store, route_id, api_key=api_key)
    return JSONResponse(body, status_code=status)


@router.delete("/v1/model_api/routes/{route_id}")
async def model_api_route_remove(route_id: str, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        setup_core.model_api_route_remove, auth.store, route_id)
    return JSONResponse(body, status_code=status)


@router.patch("/v1/model_api/credentials/{credential_id}")
async def model_api_credential_patch(credential_id: str, request: Request,
                                     auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_credential_patch, auth.store, credential_id, payload,
        caller_api_key=caller_api_key)
    return JSONResponse(body, status_code=status)


@router.delete("/v1/model_api/credentials/{credential_id}")
async def model_api_credential_remove(credential_id: str,
                                      auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        setup_core.model_api_credential_remove, auth.store, credential_id)
    return JSONResponse(body, status_code=status)
```

⚠️ 路由函数名不可与模块别名撞名（CONTRIBUTING §3）。这里全是 `model_api_*`，安全。

- [ ] **Step 2: 登记新 slug**

在 `docs/API_ERRORS.md` 的「model_api / provider 配置（设置页）」表格里（约第 55-68 行）追加三行：

```markdown
| `route_not_found` | 404 | user_provider | 指定的 route id 不属于该用户或已删除 | |
| `credential_not_found` | 404 | user_provider | 指定的 credential id 不属于该用户或已删除 | |
| `api_key_or_credential_id_required` | 400 | user_provider | POST /routes 必须且只能给 api_key 与 credential_id 之一 | |
```

（`nothing_to_update`、`model_api_credential_write_failed`、`model_api_route_write_failed` 也一并登记。）

- [ ] **Step 3: 跑 Task 6 的全部测试**

```bash
python -m pytest tests/test_model_api_profiles_routes.py -q
```

预期：全 PASS。

- [ ] **Step 4: 确认 slug 守卫测试通过**

```bash
python -m pytest tests/ -q -k "api_errors or error_slug"
```

预期：PASS。

- [ ] **Step 5: pyflakes + commit**

```bash
python -m pyflakes backend/hosted
git add backend/hosted/setup_core.py backend/hosted/setup_routes_asgi.py docs/API_ERRORS.md tests/test_model_api_profiles_routes.py
git commit -m "feat(model-api): route/credential collection endpoints"
```

---

### Task 8: activate 时释放 in-flight reply claim

**Files:**
- Modify: `backend/db.py`（把 Task 6 的 no-op `chat_expire_reply_claims` 换成真身）
- Test: `tests/test_model_api_profiles_db.py`（追加）

**Interfaces:**
- Consumes: `chat_messages.doc` 里的 `reply_claimed_by` / `reply_claim_expires_at`
- Produces: `db.chat_expire_reply_claims(user_id: str) -> int`（返回被释放的行数）

**为什么要这个**：切 route 必然触发 `supervisor.py:275` 的 `config changed → kill + spawn`（`_spawn_identity` 的签名含 provider/model/base_url/provider_key/driver）。消息不会丢（`chat/service.py:72` 的 lost-turn redelivery backstop 覆盖 respawn 场景），但旧 consumer 已 claim 的那条要等 `CHAT_POLL_CLAIM_TTL_SEC`（默认 600 秒）过期才重投。主动过期它，新 consumer 下个 poll 就接手。

> ## ⚠️ 释放的位置：supervisor 里 `kill_fn` 之后，**不是** activate 端点里
>
> activate 端点返回时旧 consumer 还活着 —— supervisor 要等自己的 tick（`AGENT_TICK_INTERVAL_SEC` 默认 15s）才发现配置变了，再 SIGTERM + 最多 `_KILL_GRACE_SEC = 3.0` 秒 grace 才 SIGKILL。在这 15–18 秒窗口里清 claim，正在跑那个回合的旧 consumer 会与新 consumer **双跑同一条消息**。`chat/service.py:66-70` 明确写着 `CHAT_POLL_CLAIM_TTL_SEC` 设成 600s 就是为了防这个：「已回复的 409 只挡住重复写入，挡不住重复计费」。**用户会被 provider 计费两次。**
>
> 正确位置是 `backend/agent_runtime/supervisor.py` 的 respawn 分支，`kill_fn` 之后、`spawn_fn` 之前 —— 那时持有者一定已死：
>
> ```python
> self.kill_fn(child["pid"])
> freed = db.chat_expire_reply_claims(user_id)
> if freed:
>     wake_bus.notify("chat", user_id)
> pid = self.spawn_fn(entry, user_id, home)
> ```
>
> ## ⚠️ 缓存失效是必需的，不是可选的
>
> Task 8 原本把「poll 走 DB 还是内存缓存」列为待验证项。**答案是内存缓存**：`chat/service.py:388` 的 `_pending_chat_messages_for_poll` 遍历 per-worker 的 `store.chat_messages`，`_chat_message_claimable`（`service.py:344-351`）在到达 `db.chat_try_claim_reply` 的 DB CAS **之前**就用缓存里的 `reply_claimed_by` 把消息滤掉了。**只清 DB 行是彻底的 no-op**（而且测试还会绿，因为测试直接调 db 函数）。
>
> `wake_bus.notify("chat", uid)` 在接收方 worker 上走到 `core_store._evict_store()` → `store.reload()`（`core/store.py:894-906`），缓存才真正刷新。supervisor 是独立进程，`wake_bus._dispatch` 的 same-origin 自过滤（`wake_bus.py:85-86`）不会拦它，所以所有 app worker 都收得到。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_model_api_profiles_db.py`：

`db.chat_append(user_id, msg_id, ts, doc, max_messages)` 是 5 参；`db.chat_load(user_id)` 返回的是 **doc 列表**（`SELECT doc FROM chat_messages ORDER BY seq`），不是带 `msg_id` 的包装。所以按 doc 里的 `text` 索引。

```python
def test_expire_reply_claims_only_touches_unanswered_claimed_rows(backend_env, registered_user):
    uid = registered_user["user_id"]
    now = 1_000_000.0

    db.chat_append(uid, "m1", now, {
        "role": "user", "text": "in-flight",
        "reply_claimed_by": "consumer-A",
        "reply_claim_expires_at": str(now + 600)}, 500)
    db.chat_append(uid, "m2", now + 1, {
        "role": "user", "text": "already-replied",
        "reply_claimed_by": "consumer-A",
        "reply_claim_expires_at": str(now + 600),
        "reply_status": "replied"}, 500)
    db.chat_append(uid, "m3", now + 2, {"role": "user", "text": "unclaimed"}, 500)

    freed = db.chat_expire_reply_claims(uid)
    assert freed == 1

    docs = {d["text"]: d for d in db.chat_load(uid)}
    assert docs["in-flight"]["reply_claimed_by"] == ""
    assert docs["in-flight"]["reply_claim_expires_at"] == ""
    assert docs["already-replied"]["reply_claimed_by"] == "consumer-A"   # 已回复的不动
    assert docs["unclaimed"].get("reply_claimed_by", "") == ""
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python -m pytest tests/test_model_api_profiles_db.py -k expire_reply -q
```

预期：FAIL —— no-op 返回 0。

- [ ] **Step 3: 实现**

替换 `backend/db.py` 里的 no-op：

```python
def chat_expire_reply_claims(user_id: str) -> int:
    """释放该用户所有「已 claim 但尚未回复」的 chat 行的 claim。

    切 active route 会 respawn consumer（supervisor._spawn_identity 变了）。被 kill 的
    旧 consumer 持有的 claim 否则要等 CHAT_POLL_CLAIM_TTL_SEC（默认 600s）才过期，
    lost-turn redelivery backstop 才会重投那条消息。主动清空让新 consumer 立刻接手。

    WHERE 条件与 chat_try_claim_reply 的 CAS 对齐：只碰未回复的行。
    """
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "UPDATE chat_messages "
                "SET doc = doc || '{\"reply_claimed_by\":\"\",\"reply_claim_expires_at\":\"\"}'::jsonb "
                "WHERE user_id = %s "
                "  AND (doc->>'reply_status') IS DISTINCT FROM 'replied' "
                "  AND COALESCE(doc->>'reply_message_id','') = '' "
                "  AND COALESCE(doc->>'reply_claimed_by','') <> ''",
                (user_id,),
            )
        return cur.rowcount
    except Exception as e:
        log.error("[db] chat_expire_reply_claims(%s) failed: %s", user_id, e)
        return 0
```

- [ ] **Step 4: 跑测试确认全绿**

```bash
python -m pytest tests/test_model_api_profiles_db.py -q
python -m pytest tests/ -q -k "chat"
```

预期：零新增失败。

- [ ] **⚠️ Step 5: 验证内存缓存不会抵消这次释放（观察项，不是猜测）**

`chat/service.py` 的 poll 路径有 per-worker 内存缓存（`store.chat_messages`）。DB 里的 claim 清了，但如果 poll 的候选筛选先过内存缓存，这次释放可能不生效（不会更坏，只是无效）。

跑一个端到端探针确认：

```bash
python -m pytest tests/ -q -k "poll and claim"
```

然后读 `backend/chat/service.py` 里 `_pending_chat_messages_for_poll` 的实现，确认它取候选时是查 DB 还是读 `store.chat_messages`。

- 若查 DB：本任务完成，无需额外动作。
- 若读内存缓存：在 `chat_expire_reply_claims` 之后补一次 `core.wake_bus` 失效广播（照抄 `core/wake_bus.py` 里既有的 per-user 缓存失效用法），并在 PR 描述里写明。

**把结论写进 PR 描述**，不要留成口头结论。

- [ ] **Step 6: Commit**

```bash
git add backend/db.py tests/test_model_api_profiles_db.py
git commit -m "feat(model-api): release in-flight reply claims on route activate"
```

---

### Task 9: 销号清单 + 全量回归

**Files:**
- Modify: `backend/db.py:2575`（`delete_user_data` 的表清单）
- Test: `tests/test_model_api_profiles_db.py`（追加）

**Interfaces:**
- Consumes: 前面所有任务

- [ ] **Step 1: 写失败的测试**

```python
def test_account_deletion_clears_credentials_and_routes(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)

    db.delete_user_data(uid)

    assert db.model_api_credentials_list(uid) == []
    assert db.model_api_routes_list(uid) == []


def test_deleting_users_row_cascades(backend_env, registered_user):
    uid = registered_user["user_id"]
    cid = _cred(uid)
    db.model_api_route_upsert(uid, cid, "claude-sonnet-4-5", None)

    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id = %s", (uid,))

    assert db.model_api_credentials_list(uid) == []
    assert db.model_api_routes_list(uid) == []
```

第二个测试**在 Task 1 之后就该是绿的**（外键 CASCADE 管的），它是对迁移的验收。第一个在 Step 3 之前是红的。

- [ ] **Step 2: 跑测试确认第一个失败**

```bash
python -m pytest tests/test_model_api_profiles_db.py -k deletion -q
```

- [ ] **Step 3: 补冗余兜底清单**

`backend/db.py` 的 `delete_user_data` 里，表元组加两项。**顺序有讲究**：routes 先于 credentials（虽然 CASCADE 会管，但显式顺序让这个兜底带在没有 FK 的库上也正确）。

```python
                for table in (
                    "chat_messages",
                    "memory_moments",
                    "world_book_entries",
                    "frame_envelopes",
                    "user_logs",
                    "user_blobs",
                    "perception_items",
                    "perception_daily",
                    "agent_runtime_instances",
                    "genesis_import_chunks",
                    "genesis_import_outputs",
                    "genesis_import_jobs",
                    "model_api_routes",
                    "model_api_credentials",
                ):
```

- [ ] **Step 4: 跑测试确认全绿**

```bash
python -m pytest tests/test_model_api_profiles_db.py -q
```

- [ ] **Step 5: 全量回归**

```bash
python -m pytest tests/ -q \
    --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py
```

预期：**零新增失败**。对比 plan 开头记下的基线（`2440 passed, 7 failed`）。新增测试文件会让 passed 数上升；failed 数**必须仍是 7**。

若某个既有测试红了，八成是它用 `db.set_blob(uid, "model_api", ...)` 造数据。改成用 Task 2 的 db 函数造 credential + route。

- [ ] **Step 6: pyflakes 全包**

```bash
python -m pyflakes backend/db.py backend/hosted
```

预期：无输出。

- [ ] **Step 7: 确认 asgi_app.py 零 diff**

```bash
git diff --stat main -- backend/asgi_app.py
```

预期：无输出（CONTRIBUTING §9 自查清单第一条）。

- [ ] **Step 8: Commit**

```bash
git add backend/db.py tests/test_model_api_profiles_db.py
git commit -m "feat(model-api): cover new tables in account deletion"
```

---

## PR 描述必须包含

- **新增路由**（url_map 是大改动的回归基线，CONTRIBUTING §8）：
  ```
  GET    /v1/model_api/routes
  POST   /v1/model_api/routes
  POST   /v1/model_api/routes/{route_id}/activate
  POST   /v1/model_api/routes/{route_id}/test
  DELETE /v1/model_api/routes/{route_id}
  PATCH  /v1/model_api/credentials/{credential_id}
  DELETE /v1/model_api/credentials/{credential_id}
  ```
- **加密路径变更**：provider key 的 envelope 从 `user_blobs.doc.api_key_envelope` 搬到 `model_api_credentials.api_key_envelope`。服务端仍永不解密；解密路径仍只经 `core.enclave._decrypt_envelope_via_enclave`。
- **`POST /v1/model_api/setup` 语义变更**：覆盖式 → 幂等 upsert（旧版 App 无感，反复 setup 不堆积 route）。
- **迁移 `0014_model_api_profiles`**：建两张表 + 从 blob 回填。`model_api` blob 保留为回滚快照，新代码不读不写。**部署前提：app CVM 与 runner CVM 同时部署。**
- **Task 8 Step 5 的结论**：poll 候选筛选走 DB 还是内存缓存，`chat_expire_reply_claims` 是否需要 wake_bus 广播。

## 部署

1. app CVM 与 runner CVM **同时** bump 到本 commit（前提条件，不可分开部署）。
2. 部署后立刻核对 roster：
   ```sql
   SELECT count(*) FROM model_api_routes WHERE is_active AND test_status = 'ok';
   ```
   应与部署前 `SELECT count(*) FROM user_blobs WHERE kind='model_api' AND doc->>'test_status'='ok';` 相等。**不等就回滚。**
3. 观察 runner 日志有无 `config changed for … respawning consumer` 风暴（回填后 `_spawn_identity` 不应变化，因为 provider/model/base_url/key 都没变——若出现风暴，说明回填丢了字段）。

## 范围外（不要在本 PR 做）

- iOS 侧改动（另一个仓库）：`ModelAPISettingsView.select()` 与 `save()` 接新端点，`refresh()` 改调 `GET /routes`。
- 删除 `model_api` blob 的收尾迁移（另开 PR，稳定运行后）。
- route 级的 provider 熔断/退避。
- 「上次使用时间」排序、route 重命名等 UI 便利功能。
