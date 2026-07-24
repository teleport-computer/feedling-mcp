# 双运行时共存（V1 + V2 + allowlist 切换）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 pre 基底上恢复 V1 agent-runner 托管执行侧，与 V2 worker 池共存，由 per-user DB allowlist + reconciler 双向切换，prod 默认全员 V1。

**Architecture:** per-user fence（`resident`/`draining`/`v2` + generation，现存机制）是路由唯一真相；`chat_send_core` 恢复退役前 shipped 过的双分支；V1 执行侧从 `origin/test` 原样恢复；唯一新组件是 allowlist 表 + reconciler（`backend/core/leader.py` 现成 leader 模式）。send 热路径**只读 fence，不读 allowlist 表**。

**Tech Stack:** Python 3.11 / FastAPI(ASGI) / PostgreSQL(psycopg pool) / Alembic / pytest。

**Spec:** `docs/superpowers/specs/2026-07-21-dual-runtime-v1-v2-coexistence-design.md`

## Global Constraints

- 基底：`pre` @ `ec377440`；新分支 `feat/dual-runtime`。
- **commit 政策（覆盖本 plan 所有 commit 步骤）**：本仓规则 = 只在用户明确授权时 commit。执行本 plan 前须请用户对「每-task commit 到 feat/dual-runtime」给一次性授权；未获授权则跳过 commit 步骤、留工作树，task 边界用 `git diff` 产 review 包。
- mode 常量精确值：`HOSTED_RUNTIME_MODE_RESIDENT = "resident_cli"`、`HOSTED_RUNTIME_MODE_DB_ACTION_V2 = "db_action_v2"`（config_store.py:383-384，不得改）。
- fence 状态精确值：`state ∈ {"resident", "draining", "v2"}`。
- policy env：`FEEDLING_HOSTED_RUNTIME_POLICY ∈ {"v2_only", "dual"}`，默认 `"dual"`。
- 默认 desired env：`FEEDLING_RUNTIME_DEFAULT_DESIRED ∈ {"resident", "v2"}`，默认 `"resident"`，仅 reconciler 读。
- 错误码保持可区分：V2 死池 = `workers_unavailable`；V1 死 supervisor = wire body `hosting_runtime_unavailable`（debug summary `supervisor_unavailable`，与退役前历史一致——canary 依赖 body 判可重试）；draining = `runtime_switching`。
- V1 恢复一律取 **`origin/test` 当前版**（含 c6ae9a61 加固），不取退役前快照；只有 pre 已演化的共享文件做三方合。
- 测试：PG `127.0.0.1:55432`（`docker start feedling-test-pg` 若没起）。全套命令：
  `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py`。基线 5006 passed（+2 个已知环境失败：缺 `e2b` 依赖、`test_chat_response_finalize_cas` 并行 flake 单跑绿）。
- pyflakes：全仓恒剩 1 条 unused（identity re-export，预期）。
- alembic 现 head：`0049_merge_test_pre_heads`。新 migration 从 `0050` 起。
- ⚠️ 已知地雷：`0045_drop_retired_hosted_supervisor` 在 prod DB（test 血脉）尚未执行过；本 plan 的 0050 必须重建其 drop 的表，否则 prod 部署时 V1 表被 drop、V1 当场死。

---

## File Structure（新建/主改文件与职责）

| 文件 | 职责 |
|------|------|
| `backend/alembic/versions/0050_dual_runtime_coexistence.py` | 新建：重建 V1 supervisor 表（抄 0045._DOWN）+ `v2_user_allowlist` 表 |
| `backend/db.py` | 恢复 V1 函数（from test）+ 新增 allowlist CRUD |
| `backend/agent_runtime/{supervisor,spawners,leases,tokens}.py` 等 | 恢复：V1 执行侧（from test 原样） |
| `tools/chat_resident_consumer.py`、`tools/io_cli.py` | 恢复：hosted 形态（from test 原样） |
| `backend/hosted/config_store.py` | 改：policy 双值、`set_hosted_runtime_mode` 双向 |
| `backend/hosted/agent_runtime_cutover.py` | 恢复：supervisor-live/reply-wait 函数族 |
| `backend/hosted/chat_send_core.py` | 改：三态分流（v2 / resident / draining） |
| `backend/hosted/runtime_reconciler.py` | 新建：reconciler（唯一全新组件） |
| `backend/admin/admin_core.py` + `routes_asgi.py` | 改：runtime-allowlist 端点；`set_runtime_mode` 去 400 闸 |
| `tests/test_dual_runtime_coexistence.py` | 新建：共存契约 + 路由矩阵 |
| `tests/test_runtime_reconciler.py` | 新建：reconciler 单元/收敛 |
| `tests/test_dual_runtime_flip_no_loss.py` | 新建：flip 消息不丢（双向） |
| `deploy/docker-compose.phala*.yaml`、`ci.yml`、`pin-runtime-release.sh` | 改：主 CVM 加 serve-worker；runner 恢复 V1-only |

---

### Task 1: 分支与基线

**Files:** 无代码改动。

- [ ] **Step 1: 建分支**

```bash
cd /Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/hosted-runtime-v2
git status --short   # 必须 clean；不 clean 先停下问用户
git checkout -b feat/dual-runtime
git log --oneline -1  # 期望 ec377440
```

- [ ] **Step 2: 起 PG + 跑基线**

```bash
docker start feedling-test-pg 2>/dev/null || docker run -d --name feedling-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3
```

Expected: `5006 passed`（±已知 2 个环境失败）。把实际数字记入进度 ledger 作为本分支基线。

---

### Task 2: migration 0050 + db.py V1 表面恢复 + allowlist CRUD

**Files:**
- Create: `backend/alembic/versions/0050_dual_runtime_coexistence.py`
- Modify: `backend/db.py`
- Test: `tests/test_dual_runtime_db.py`（新建）

**Interfaces（Produces，后续 task 依赖）:**
- `db.set_supervisor_heartbeat(payload: dict) -> None` / `db.read_supervisor_heartbeat() -> dict | None`
- `db.set_supervisor_instance_heartbeat(owner: str, payload: dict) -> None`
- `db.list_supervisor_instance_heartbeats() -> list[dict]`
- `db.prune_supervisor_instance_heartbeats(max_age_sec: float) -> None`
- `db.list_agent_runtime_enabled_users() -> list[dict]`（Task 7 会加 mode 过滤）
- `db.upsert_runtime_allowlist(user_id: str, desired: str, *, updated_by: str = "", note: str = "") -> None`
- `db.delete_runtime_allowlist(user_id: str) -> bool`
- `db.list_runtime_allowlist() -> list[dict]`  # [{user_id, desired, updated_at, updated_by, note}]
- `db.get_runtime_allowlist_map() -> dict[str, str]`  # {user_id: desired}

- [ ] **Step 1: 写 migration 0050**

完整文件内容（V1 表 DDL 逐字抄自 `0045_drop_retired_hosted_supervisor.py` 的 `_DOWN`，那就是权威 schema）：

```python
"""Dual-runtime coexistence: restore V1 supervisor state + user allowlist.

Revision ID: 0050_dual_runtime_coexistence
Revises: 0049_merge_test_pre_heads

Restores the resident-supervisor tables dropped by 0045 (prod's test-lineage
DB has NOT yet run 0045; on prod the upgrade chain runs 0045 then this — the
transient drop window lasts only for the migration run itself) and adds the
v2_user_allowlist control table for the per-user canary rollout.
"""
from alembic import op

revision = "0050_dual_runtime_coexistence"
down_revision = "0049_merge_test_pre_heads"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS agent_runtime_instances (
    user_id           TEXT PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
    driver            TEXT NOT NULL,
    status            TEXT NOT NULL,
    pid               INTEGER,
    lease_owner       TEXT,
    lease_expires_at  TIMESTAMPTZ,
    session_ref       TEXT,
    runtime_home      TEXT NOT NULL,
    last_heartbeat_at TIMESTAMPTZ,
    last_active_at    TIMESTAMPTZ,
    error             TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runtime_instances_lease_idx
    ON agent_runtime_instances (lease_owner, lease_expires_at);

CREATE TABLE IF NOT EXISTS agent_runtime_supervisor_heartbeats (
    owner           TEXT PRIMARY KEY,
    host            TEXT,
    shard_index     INTEGER NOT NULL DEFAULT 0,
    shard_count     INTEGER NOT NULL DEFAULT 1,
    max_children    INTEGER NOT NULL DEFAULT 0,
    active_children INTEGER NOT NULL DEFAULT 0,
    host_all        BOOLEAN NOT NULL DEFAULT false,
    gateway         BOOLEAN NOT NULL DEFAULT false,
    version         TEXT,
    payload         JSONB NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runtime_supervisor_heartbeats_updated_idx
    ON agent_runtime_supervisor_heartbeats (updated_at);

CREATE TABLE IF NOT EXISTS v2_user_allowlist (
    user_id     TEXT PRIMARY KEY,
    desired     TEXT NOT NULL CHECK (desired IN ('v2', 'resident')),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT ''
);
"""

_DOWN = """
DROP TABLE IF EXISTS v2_user_allowlist;
DROP TABLE IF EXISTS agent_runtime_supervisor_heartbeats;
DROP TABLE IF EXISTS agent_runtime_instances;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
```

- [ ] **Step 2: 跑 migration 验证**

```bash
cd backend && python -m alembic upgrade head && cd ..
psql "postgresql://postgres:test@127.0.0.1:55432/postgres" -c "\d v2_user_allowlist" | head -8
```

Expected: 表存在，三张都建出来。（注意 conftest 用动态库名，此处直接验 alembic 可跑通即可；若 alembic env 指向别的 DB URL，用 `DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres python -m alembic upgrade head`。）

- [ ] **Step 3: 写失败测试（allowlist CRUD + V1 函数存在性）**

`tests/test_dual_runtime_db.py`：

```python
"""Dual-runtime DB surface: restored V1 supervisor functions + allowlist CRUD."""
import db


def test_v1_supervisor_db_surface_restored():
    # 反转 retirement 断言：这些必须重新存在（Task 3 的 agent_runtime 包要用）
    for name in (
        "set_supervisor_heartbeat",
        "read_supervisor_heartbeat",
        "set_supervisor_instance_heartbeat",
        "list_supervisor_instance_heartbeats",
        "prune_supervisor_instance_heartbeats",
        "list_agent_runtime_enabled_users",
    ):
        assert hasattr(db, name), name


def test_allowlist_crud_roundtrip(fresh_user):
    uid = fresh_user  # 若 conftest 无此 fixture，参照本文件同目录其它 DB 测试建用户的方式
    assert db.get_runtime_allowlist_map() == {} or uid not in db.get_runtime_allowlist_map()
    db.upsert_runtime_allowlist(uid, "v2", updated_by="test", note="canary")
    assert db.get_runtime_allowlist_map()[uid] == "v2"
    rows = db.list_runtime_allowlist()
    row = next(r for r in rows if r["user_id"] == uid)
    assert row["desired"] == "v2" and row["note"] == "canary"
    db.upsert_runtime_allowlist(uid, "resident")   # upsert 覆盖
    assert db.get_runtime_allowlist_map()[uid] == "resident"
    assert db.delete_runtime_allowlist(uid) is True
    assert uid not in db.get_runtime_allowlist_map()
    assert db.delete_runtime_allowlist(uid) is False  # 幂等


def test_allowlist_rejects_bad_desired(fresh_user):
    import pytest
    with pytest.raises(Exception):  # CHECK 约束或应用层校验
        db.upsert_runtime_allowlist(fresh_user, "bogus")
```

（`fresh_user` fixture：先 `grep -rn 'def fresh_user\|register.*user' tests/conftest.py` 找现成建用户 fixture；没有就在本测试文件内用与 `tests/test_chat_send_v2_enqueue.py` 相同的建用户手法写一个局部 fixture——先读那个文件再落。）

- [ ] **Step 4: 跑测试确认失败**

```bash
python -m pytest tests/test_dual_runtime_db.py -q
```

Expected: FAIL（`db` 缺 V1 函数与 allowlist 函数）。

- [ ] **Step 5: 恢复 db.py 的 V1 函数（from test）**

```bash
# 参照物落盘（不要整文件覆盖 db.py —— pre 的 db.py 已大量演化）
git show origin/test:backend/db.py > /tmp/db_test_ref.py
grep -n 'AGENT_RUNTIME_SUPERVISOR_HEARTBEAT_KEY\|def set_supervisor_heartbeat\|def read_supervisor_heartbeat\|def set_supervisor_instance_heartbeat\|def list_supervisor_instance_heartbeats\|def prune_supervisor_instance_heartbeats\|def list_agent_runtime_enabled_users' /tmp/db_test_ref.py
```

把这些函数块（test 版 ~251-360 行的 supervisor 心跳族 + ~2171 起的 `list_agent_runtime_enabled_users`，以边界空行为界完整拷贝，含 `AGENT_RUNTIME_SUPERVISOR_HEARTBEAT_KEY` 常量）原样插入 pre 的 `backend/db.py`——放在文件中与 test 相近的位置（server_config 相关函数附近）。**不改一个字**（Task 7 才加 mode 过滤）。

- [ ] **Step 6: 实现 allowlist CRUD（db.py 追加）**

```python
# --------------------------------------------------------------------------- #
# Dual-runtime canary allowlist (spec 2026-07-21). Read by the reconciler and
# the admin surface ONLY — the send hot path reads the per-user fence, never
# this table, so an allowlist outage can not affect message delivery.
# --------------------------------------------------------------------------- #

_RUNTIME_ALLOWLIST_DESIRED = frozenset({"v2", "resident"})


def upsert_runtime_allowlist(user_id: str, desired: str, *,
                             updated_by: str = "", note: str = "") -> None:
    if desired not in _RUNTIME_ALLOWLIST_DESIRED:
        raise ValueError(f"desired must be one of {sorted(_RUNTIME_ALLOWLIST_DESIRED)}")
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO v2_user_allowlist (user_id, desired, updated_at, updated_by, note)
            VALUES (%s, %s, now(), %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                desired = EXCLUDED.desired,
                updated_at = now(),
                updated_by = EXCLUDED.updated_by,
                note = EXCLUDED.note
            """,
            (user_id, desired, updated_by, note),
        )


def delete_runtime_allowlist(user_id: str) -> bool:
    with get_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM v2_user_allowlist WHERE user_id = %s", (user_id,))
        return cur.rowcount > 0


def list_runtime_allowlist() -> list[dict]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT user_id, desired, updated_at, updated_by, note "
            "FROM v2_user_allowlist ORDER BY user_id").fetchall()
    return [
        {"user_id": r[0], "desired": r[1],
         "updated_at": r[2].isoformat() if r[2] else None,
         "updated_by": r[3], "note": r[4]}
        for r in rows
    ]


def get_runtime_allowlist_map() -> dict[str, str]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT user_id, desired FROM v2_user_allowlist").fetchall()
    return {r[0]: r[1] for r in rows}
```

（若 db.py 的连接惯用法不是 `conn.execute(...)`（如用 cursor），以文件内相邻函数的写法为准改写——**风格跟随现文件**。）

- [ ] **Step 7: 跑测试确认通过**

```bash
python -m pytest tests/test_dual_runtime_db.py -q
```

Expected: PASS。

- [ ] **Step 8: 处置 retirement 守卫测试（会被本 task 打破）**

```bash
git rm tests/test_hosted_resident_retirement.py
```

理由记录：它断言「V1 必须不存在」，与本项目目标正相反；Task 9 用 `test_dual_runtime_coexistence.py` 建立替代契约（含 `v2_only` policy 下等价 pre 的回归保障）。P7 退役时从 git 历史找回。

- [ ] **Step 9: 全量回归**

```bash
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3
```

Expected: 基线 + 新增用例数，0 新失败。

- [ ] **Step 10: Commit（须已获用户授权，见 Global Constraints）**

```bash
git add backend/alembic/versions/0050_dual_runtime_coexistence.py backend/db.py tests/test_dual_runtime_db.py
git add -u tests/test_hosted_resident_retirement.py
git commit -m "feat(dual-runtime): migration 0050 + restore V1 db surface + allowlist CRUD"
```

---

### Task 3: 恢复 agent_runtime 包 + V1 包级测试

**Files:**
- Restore(from origin/test): `backend/agent_runtime/{supervisor,spawners,leases,tokens}.py`、`backend/agent_runtime/README.md`、`backend/agent_runtime/agent_tools_prompt.md`、`backend/agent_runtime/requirements.txt`
- Reconcile: `backend/agent_runtime/__init__.py`、`backend/agent_runtime/introduction.py`（pre 版被退役改过，取并集）
- Restore(from origin/test): `tests/test_agent_runtime_supervisor.py`、`tests/test_agent_runtime_spawners.py`、`tests/test_agent_runtime_leases.py`、`tests/test_agent_runtime_tokens.py`、`tests/test_agent_runtime_discovery.py`、`tests/test_agent_runtime_genesis_gate.py`、`tests/test_agent_runtime_resident_contract.py`、`tests/test_agent_runtime_resolve_cache.py`、`tests/test_runner_notice.py`

**Interfaces:**
- Consumes: Task 2 的 `db.set_supervisor_heartbeat` 等（supervisor/leases 直接调）。
- Produces: `agent_runtime.supervisor`（V1 supervisor 主循环）、`agent_runtime.spawners`（consumer 进程管理 + `consumer_env` per-user 钉路径）——Task 6/7 引用其存在性。

- [ ] **Step 1: 整文件恢复**

```bash
git checkout origin/test -- \
  backend/agent_runtime/supervisor.py backend/agent_runtime/spawners.py \
  backend/agent_runtime/leases.py backend/agent_runtime/tokens.py \
  backend/agent_runtime/README.md backend/agent_runtime/agent_tools_prompt.md \
  backend/agent_runtime/requirements.txt \
  tests/test_agent_runtime_supervisor.py tests/test_agent_runtime_spawners.py \
  tests/test_agent_runtime_leases.py tests/test_agent_runtime_tokens.py \
  tests/test_agent_runtime_discovery.py tests/test_agent_runtime_genesis_gate.py \
  tests/test_agent_runtime_resident_contract.py tests/test_agent_runtime_resolve_cache.py \
  tests/test_runner_notice.py
```

- [ ] **Step 2: 对账 `__init__.py` / `introduction.py`**

```bash
git diff origin/test -- backend/agent_runtime/__init__.py backend/agent_runtime/introduction.py
```

规则：pre 版若只有「退役措辞」差异 → 取 test 版；若 pre 加了 V2 引用的新内容（introduction 文案被 V2 用）→ 保留 pre 内容 + 恢复 test 内容，两者并集。逐 hunk 判断，不整文件盲覆盖。

- [ ] **Step 3: 跑恢复的 V1 测试**

```bash
python -m pytest tests/test_agent_runtime_supervisor.py tests/test_agent_runtime_spawners.py tests/test_agent_runtime_leases.py tests/test_agent_runtime_tokens.py tests/test_agent_runtime_discovery.py tests/test_agent_runtime_genesis_gate.py tests/test_agent_runtime_resident_contract.py tests/test_agent_runtime_resolve_cache.py tests/test_runner_notice.py -q 2>&1 | tail -5
```

Expected: 大部分 PASS。失败逐个归因：(a) 依赖 pre 已改名/已删的共享符号 → 小修 import/调用点向 pre 现状对齐（记录每处）；(b) 依赖 Task 5/6 才恢复的 config_store/cutover 符号 → 用 `pytest.importorskip`/skip 标注 `# TODO(dual-runtime Task N)` 并在该 task 解除。**不许为过测试改 V1 语义**。

- [ ] **Step 4: conftest 对账**

`2b294a1f` 改过 `tests/conftest.py`（10 行）。检查：

```bash
git show 2b294a1f -- tests/conftest.py
```

若删的是 agent_runtime 相关 collect/fixture，按需恢复该 10 行中支撑 V1 测试的部分。

- [ ] **Step 5: 全量回归 + Commit**

```bash
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3
git add backend/agent_runtime/ tests/test_agent_runtime_*.py tests/test_runner_notice.py tests/conftest.py
git commit -m "feat(dual-runtime): restore V1 agent_runtime package from test lineage"
```

---

### Task 4: 恢复 tools consumer / io_cli 的 hosted 形态

**Files:**
- Restore(from origin/test): `tools/chat_resident_consumer.py`、`tools/io_cli.py`
- Restore(from origin/test): `tests/test_chat_resident_consumer.py`、`tests/test_io_cli_image.py`、`tests/test_user_mcp_consumer.py`（test 版含 ec377440 里被删的 3 个 keyless 测试——双运行时下它们守护活代码，必须回来）

**Interfaces:**
- Produces: consumer 的 `_HOSTED`/runtime-token/host-all 路径（V1 spawners 依赖其 CLI 契约）。

- [ ] **Step 1: 整文件恢复**

```bash
git checkout origin/test -- tools/chat_resident_consumer.py tools/io_cli.py \
  tests/test_chat_resident_consumer.py tests/test_io_cli_image.py tests/test_user_mcp_consumer.py
```

⚠️ 这会**覆盖掉 `ec377440` 里对这 5 个文件做的「剥 host-all」手术**——那是故意的：该手术的前提（pre 无 V1）被本项目推翻。test 版 = c6ae9a61 完整加固版，keyless 守卫（`_USER_MCP_PATHS_PINNED`/`_CHAT_SCRATCH_PINNED`）随之回归为活代码。

- [ ] **Step 2: 对账 pre 独有演化**

```bash
git log --oneline origin/test..HEAD -- tools/chat_resident_consumer.py tools/io_cli.py
```

对每个 pre 侧 commit（除 ec377440 的剥离手术外）判断：是否有 V2 时代对这两个文件的真实修复需要保留（如 `runtime-v2-status` 子命令）。有则把该 hunk 重新叠上。已知点：pre 版 io_cli 有 `"runtime-v2-status"` 命令（test_hosted_resident_retirement 曾断言）——检查 test 版是否也有，没有则从 pre 版恢复该子命令块。

- [ ] **Step 3: 跑测试**

```bash
python -m pytest tests/test_chat_resident_consumer.py tests/test_io_cli_image.py tests/test_user_mcp_consumer.py -q 2>&1 | tail -3
```

Expected: PASS（这些测试与被测文件同源恢复）。失败按 Task 3 Step 3 同规则归因。

- [ ] **Step 4: 全量回归 + Commit**

```bash
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3
git add tools/ tests/test_chat_resident_consumer.py tests/test_io_cli_image.py tests/test_user_mcp_consumer.py
git commit -m "feat(dual-runtime): restore hosted-mode consumer & io_cli from test lineage"
```

---

### Task 5: config_store 双值 policy + 双向 set_hosted_runtime_mode + admin 去闸

**Files:**
- Modify: `backend/hosted/config_store.py:383-460,593`
- Modify: `backend/admin/admin_core.py:114-131`
- Restore-reconcile: `tests/test_hosted_runtime_policy.py`、`tests/test_hosted_runtime_mode.py`、`tests/test_admin_runtime_mode.py`（`2b294a1f` 裁掉的双分支用例恢复，保留 pre 新增用例）

**Interfaces:**
- Produces:
  - `hosted_runtime_policy() -> str`，返回 `"dual"` 或 `"v2_only"`（env `FEEDLING_HOSTED_RUNTIME_POLICY`，默认 `"dual"`）。
  - `set_hosted_runtime_mode(store, mode)` 接受 `"resident_cli"` 与 `"db_action_v2"` 双向。
  - `admin_core.set_runtime_mode(user_id, mode)` 双向可用（移除 400 闸）。
- Consumes: 现存 `_set_hosted_runtime_mode_for_user_id`（config_store.py:480，双向 fence 转换本来就在）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_hosted_runtime_policy.py` 追加（保留现有用例）：

```python
def test_policy_dual_is_default_and_valid(monkeypatch):
    monkeypatch.delenv("FEEDLING_HOSTED_RUNTIME_POLICY", raising=False)
    assert config_store.hosted_runtime_policy() == "dual"
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "v2_only")
    assert config_store.hosted_runtime_policy() == "v2_only"
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "bogus")
    with pytest.raises(RuntimeError):
        config_store.hosted_runtime_policy()


def test_set_runtime_mode_accepts_resident_again(fresh_user_store):
    # 双向：v2 → resident → v2，fence generation 单调递增
    config_store.set_hosted_runtime_mode(fresh_user_store, "db_action_v2")
    _, state1, gen1 = config_store.get_hosted_runtime_control_strict(fresh_user_store)
    config_store.set_hosted_runtime_mode(fresh_user_store, "resident_cli")
    mode2, state2, gen2 = config_store.get_hosted_runtime_control_strict(fresh_user_store)
    assert mode2 == "resident_cli" and state2 == "resident" and gen2 > gen1
```

（`fresh_user_store` fixture 同 Task 2 说明——先读现文件里既有用例怎么建 store，跟随之。）

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_hosted_runtime_policy.py -q 2>&1 | tail -3
```

- [ ] **Step 3: 实现 config_store 改动**

参照物：`git show 2b294a1f -- backend/hosted/config_store.py > /tmp/cs_retire.diff`（看被删的双值形态）。落点：

```python
HOSTED_RUNTIME_POLICY_V2_ONLY = "v2_only"
HOSTED_RUNTIME_POLICY_DUAL = "dual"
_HOSTED_RUNTIME_POLICIES = {HOSTED_RUNTIME_POLICY_V2_ONLY, HOSTED_RUNTIME_POLICY_DUAL}


def hosted_runtime_policy() -> str:
    """Process-wide routing policy. ``dual`` routes per-user by the fence;
    ``v2_only`` fails-closed for non-V2 users (the retirement-era behavior,
    restored at P7)."""
    policy = str(
        os.environ.get(HOSTED_RUNTIME_POLICY_ENV, HOSTED_RUNTIME_POLICY_DUAL)
        or HOSTED_RUNTIME_POLICY_DUAL
    ).strip().lower()
    if policy not in _HOSTED_RUNTIME_POLICIES:
        raise RuntimeError(
            f"{HOSTED_RUNTIME_POLICY_ENV} must be one of "
            f"{sorted(_HOSTED_RUNTIME_POLICIES)!r}; got {policy!r}")
    return policy
```

`forced_hosted_runtime_mode()`：现在恒返 v2。改为——`v2_only` 下保持现行为；`dual` 下**不再是单值概念**，检查它的所有调用点（`grep -rn forced_hosted_runtime_mode backend/`），逐个改为读 per-user fence（大概率只剩 setup/startup 物化路径用它；对照 /tmp/cs_retire.diff 里退役前 `_forced_mode` 的用法恢复语义）。

`set_hosted_runtime_mode(store, mode)`（:593）：若现版拒绝 `resident_cli`，恢复双向（`_set_hosted_runtime_mode_for_user_id` 本来就双向，通常只需放开入口校验）。

- [ ] **Step 4: admin 去闸**

`backend/admin/admin_core.py:114`，删这两行：

```python
    if mode != config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2:
        return {"error": "hosted resident runtime is retired"}, 400
```

保留 wake-schedule 种子逻辑，但只对 `db_action_v2` 方向执行（恢复退役前的 `if mode == ...` 包裹——见 `git show 2b294a1f -- backend/admin/admin_core.py` 里被删的原形，逐字恢复）。

- [ ] **Step 5: 恢复被裁的 mode/policy 测试用例**

```bash
git show 2b294a1f -- tests/test_hosted_runtime_mode.py > /tmp/thrm.diff
git show 2b294a1f -- tests/test_hosted_runtime_policy.py > /tmp/thrp.diff
git show 2b294a1f -- tests/test_admin_runtime_mode.py > /tmp/tarm.diff
```

把 diff 中 `-` 侧（被删）的 resident 方向用例恢复进现文件；与 pre 新增用例共存。凡断言「resident 被拒/400」的 pre 用例改为限定 `v2_only` policy 下成立（monkeypatch env）。

- [ ] **Step 6: 跑三个测试文件 + 全量**

```bash
python -m pytest tests/test_hosted_runtime_policy.py tests/test_hosted_runtime_mode.py tests/test_admin_runtime_mode.py -q 2>&1 | tail -3
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3
```

- [ ] **Step 7: Commit**

```bash
git add backend/hosted/config_store.py backend/admin/admin_core.py tests/test_hosted_runtime_policy.py tests/test_hosted_runtime_mode.py tests/test_admin_runtime_mode.py
git commit -m "feat(dual-runtime): dual policy + bidirectional runtime-mode flips"
```

---

### Task 6: cutover 函数恢复 + chat_send_core 三态分流 + 小钩子

**Files:**
- Modify: `backend/hosted/agent_runtime_cutover.py`（恢复被删函数族）
- Modify: `backend/hosted/chat_send_core.py:76-130` 区域（三态分流）
- Reconcile(小钩子，各 <60 行，对照 `git show 2b294a1f -- <file>` 恢复被删 hunk): `backend/hosted/setup_core.py`、`backend/chat/chat_core.py`、`backend/core/store.py`、`backend/core/enclave.py`、`backend/core/runtime_token.py`、`backend/accounts/runtime_auth.py`、`backend/bootstrap/gates.py`
- Restore-reconcile: `tests/test_asgi_hosted_chat_send.py`、`tests/test_model_api_chat_send_routing.py`、`tests/test_hosted_agent_runtime_cutover.py`

**Interfaces:**
- Consumes: Task 5 的 `hosted_runtime_policy()` 双值；Task 2/3 的 V1 db 函数与包。
- Produces: `agent_runtime_cutover.check_supervisor_live(*, require_pi: bool = False) -> tuple[bool, str]`（Task 9 契约测试引用）；chat_send_core 三态行为（Task 9/10 依赖）。

- [ ] **Step 1: 提取退役前参照物**

```bash
git show 2b294a1f^:backend/hosted/agent_runtime_cutover.py > /tmp/cutover_ref.py
git show 2b294a1f^:backend/hosted/chat_send_core.py > /tmp/send_ref.py
```

- [ ] **Step 2: 恢复 cutover 函数族**

从 `/tmp/cutover_ref.py` 原样搬回（现文件 95 行保留不动，追加恢复）：
`_env_truthy` / `_heartbeat_max_age` / `evaluate_supervisor_heartbeat` / `evaluate_supervisor_instances` / `_instance_is_fresh` / `check_supervisor_live` / `_is_assistant` / `find_reply_row` / `wait_for_reply` / `build_ready_response` / `handle_send`。
它们引用的 db 函数已由 Task 2 恢复。跑 `python -m pyflakes backend/hosted/agent_runtime_cutover.py` 清 import。

- [ ] **Step 3: 写失败的路由矩阵测试**

在（Task 9 才建正式契约文件之前）`tests/test_model_api_chat_send_routing.py` 里先落最小三态矩阵——参照该文件现有用例的 client/monkeypatch 手法：

```python
# dual policy 下的三态分流矩阵（错误码是运维契约，精确断言）：
# (mode, state)                        → 期望
# (db_action_v2, v2) + workers alive   → 202 processing（现状路径）
# (db_action_v2, v2) + workers dead    → 503 workers_unavailable
# (resident_cli, resident) + sup live  → resident 分支（202/200，与退役前一致）
# (resident_cli, resident) + sup dead  → 503 body hosting_runtime_unavailable（summary supervisor_unavailable）
# (*, draining)                        → 503 runtime_switching
# 不合法组合（如 resident_cli+v2）      → 503 runtime_control_invalid
# v2_only policy 下：非 (db_action_v2,v2) 一律 503 runtime_policy_not_ready（pre 现状回归）
```

每行一个测试函数，monkeypatch `get_hosted_runtime_control_strict` / `jobs_store.workers_alive` / `agent_runtime_cutover.check_supervisor_live` 返回值组合。

- [ ] **Step 4: 实现 chat_send_core 三态分流**

现文件 89-100 行的 v2-only 闸改为：

```python
    _policy = hosted_config_store.hosted_runtime_policy()
    _v2_tuple = (
        _runtime_mode == hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
        and _runtime_state == "v2")
    _resident_tuple = (
        _runtime_mode == hosted_config_store.HOSTED_RUNTIME_MODE_RESIDENT
        and _runtime_state == "resident")

    if _policy == hosted_config_store.HOSTED_RUNTIME_POLICY_V2_ONLY:
        if not _v2_tuple:
            # pre 现状：退役语义原样保留，P7 回归此分支
            ...(现有 runtime_policy_not_ready 块不动)...
        # 落入现有 V2 路径
    else:  # dual
        if _runtime_state == "draining":
            debug_trace.trace_event(
                store, subsystem="route", type="route.decided",
                actor="host_agent_runtime", status="gated",
                summary="runtime_switching",
                detail={"mode": "blocked", "reason": "runtime_switching"})
            return {"error": "runtime_switching"}, 503
        if _v2_tuple:
            pass  # 落入现有 V2 路径（workers_alive + kill_switch + enqueue）
        elif _resident_tuple:
            return _send_resident(store, ...)  # 恢复的 resident 分支，见下
        else:
            return {"error": "runtime_control_invalid"}, 503
```

`_send_resident(...)`：从 `/tmp/send_ref.py` 恢复 resident 路径为一个独立函数——内容 = 退役前的 supervisor wedge 闸（`check_supervisor_live(require_pi=...)`，死则 503 body `hosting_runtime_unavailable`）+ append + wake + `handle_send`/`build_processing_response` 收尾。**行为以 Step 3 测试和 /tmp/send_ref.py 为准，不即兴改进**。恢复时对齐 pre 的一个演化 commit（`git log --oneline 2b294a1f..HEAD -- backend/hosted/chat_send_core.py` 查看，保留其改动意图）。

- [ ] **Step 5: 小钩子对账（7 个文件）**

对每个文件：`git show 2b294a1f -- backend/<path>`，凡被删 hunk 中 V1 需要的（resident 分支引用的符号、runtime_auth 的 consumer 鉴权路径、gates 的启动闸）恢复；纯措辞/注释改动不动。每恢复一处跑 `python -m pyflakes <file>`。setup_core.py:274 现有 `hosted_runtime_v2_enabled_strict` 用法**保留**（V2 设置路径），只恢复它旁边被删的 resident 设置分支。

已核事实（不用恢复的项，防止对照 spec §5 误判漏项）：`backend/genesis/daemon.py` 的退役改动**纯 docstring 措辞**（recon 已核对 2b294a1f 全 diff），零代码变化，跳过；genesis 双认领风险由 `db.genesis_claim_uploaded_jobs` 的 `FOR UPDATE SKIP LOCKED` 原子认领天然排除（daemon docstring 原文：running this loop in several processes at once is safe and de-dupes），spec §11.7 关闭。

- [ ] **Step 6: 恢复被裁的 send/cutover 测试并跑绿**

```bash
git show 2b294a1f -- tests/test_asgi_hosted_chat_send.py > /tmp/tahcs.diff
git show 2b294a1f -- tests/test_hosted_agent_runtime_cutover.py > /tmp/tharc.diff
```

同 Task 5 Step 5 规则恢复。然后：

```bash
python -m pytest tests/test_model_api_chat_send_routing.py tests/test_asgi_hosted_chat_send.py tests/test_hosted_agent_runtime_cutover.py tests/test_chat_send_v2_enqueue.py -q 2>&1 | tail -3
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3
```

- [ ] **Step 7: 解除 Task 3 Step 3 留下的 skip 标注**（若有）并确认对应测试绿。

- [ ] **Step 8: Commit**

```bash
git add backend/hosted/ backend/chat/ backend/core/ backend/accounts/runtime_auth.py backend/bootstrap/gates.py tests/
git commit -m "feat(dual-runtime): three-state send routing + restored resident branch"
```

---

### Task 7: lane 一致性——V1 roster 排除 v2 用户

**Files:**
- Modify: `backend/db.py`（`list_agent_runtime_enabled_users` SQL）
- Test: `tests/test_dual_runtime_db.py`（追加）

**Interfaces:**
- Consumes: Task 2 恢复的原版 roster SQL。
- Produces: roster 保证不含 fence state='v2'/'draining' 用户——V1 supervisor 的天然过滤点（supervisor 不为其spawn consumer、不发 wake，防双跑烧双份 BYOK 的单点闸）。

**背景（recon 钉死的事实）**：test 版该函数只按 active route + test_status 过滤，**不看 mode**。V2 侧生产者已全部 `hosted_runtime_v2_enabled_strict` 过滤（serve_worker 5 处 + proactive_core.py:267），V1 侧就差这一个闸。

- [ ] **Step 1: 写失败测试（追加到 tests/test_dual_runtime_db.py）**

```python
def test_v1_roster_excludes_v2_and_draining_users(three_users_with_routes):
    # three_users_with_routes: 三个有 active ok route 的用户（建法参照
    # tests/test_model_api_chat_send_routing.py 的 route 种子手法）
    u_resident, u_v2, u_draining = three_users_with_routes
    _force_fence(u_v2, mode="db_action_v2", state="v2")
    _force_fence(u_draining, mode="db_action_v2", state="draining")
    # u_resident 不动（默认 resident）
    roster_ids = {r["user_id"] for r in db.list_agent_runtime_enabled_users()}
    assert u_resident in roster_ids
    assert u_v2 not in roster_ids
    assert u_draining not in roster_ids
```

（`_force_fence`：用 `db.get_hosted_runtime_control_strict` 的写侧对偶——`grep -n 'hosted_runtime_control' backend/db.py` 找到 fence 的存储载体（blob 或专表）后直写测试态；若已有测试 helper 复用之。）

- [ ] **Step 2: 确认失败 → 实现**

在恢复的 roster SQL 的 `WHERE` 里追加排除（fence 存储为 `model_api_runtime` blob 的 `hosted_runtime_mode` + 控制行——以 Step 1 查明的真实载体为准写 `NOT EXISTS`/`LEFT JOIN ... IS DISTINCT FROM` 子句；语义 = **只纳入 state='resident' 或无 fence 行（默认 resident）的用户**）。

- [ ] **Step 3: perception lane 跟随 fence（spec §6 第三行）**

背景：`perception_ingress_runtime_v2_enabled`（`backend/perception/service.py:47,67`）是独立 per-user flag，读点 3 处（proactive_core.py:143 / service.py:937 / perception_read_core.py:49）。若它与 fence 漂移（flag 说 v2、fence 说 resident），感知路径与聊天路径分家。

先查写点：`grep -rn 'perception_ingress_runtime_v2_enabled' backend/ --include='*.py' | grep -v 'def \|read\|if '` 找谁在 set 这个 flag（大概率 setup/admin 的 V2 物化路径）。然后二选一（按写点形态取小者）：

- (a) 若 flag 只在 mode 翻转时写：在 `admin_core.set_runtime_mode` 持久化 mode 成功后同步写 flag = (mode == db_action_v2)，flip 即对齐；
- (b) 若 flag 有独立生命周期：把 `service.perception_ingress_runtime_v2_enabled` 改为委托 fence（`get_hosted_runtime_mode_strict == db_action_v2`），flag 退化为遗留字段。

追加测试到 `tests/test_dual_runtime_db.py`：

```python
def test_perception_flag_follows_fence_after_flip(fresh_user):
    from perception import service
    from hosted import runtime_reconciler
    import db as _db
    _db.upsert_runtime_allowlist(fresh_user, "v2")
    runtime_reconciler.reconcile_once()
    assert service.perception_ingress_runtime_v2_enabled(fresh_user) is True
    _db.upsert_runtime_allowlist(fresh_user, "resident")
    runtime_reconciler.reconcile_once()
    assert service.perception_ingress_runtime_v2_enabled(fresh_user) is False
```

（此测试依赖 Task 8 的 reconciler——若按任务顺序执行到此 reconciler 未建，先用 `admin_core.set_runtime_mode` 直调替代 flip，Task 8 完成后回补 reconciler 版断言。）

- [ ] **Step 4: 确认通过 + 全量 + Commit**

```bash
python -m pytest tests/test_dual_runtime_db.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3
git add backend/db.py backend/perception/ backend/admin/admin_core.py tests/test_dual_runtime_db.py
git commit -m "feat(dual-runtime): V1 roster excludes v2/draining users + perception lane follows fence"
```

---

### Task 8: reconciler + admin allowlist 端点

**Files:**
- Create: `backend/hosted/runtime_reconciler.py`
- Modify: `backend/admin/admin_core.py`（allowlist 端点函数）、`backend/admin/routes_asgi.py`（路由注册，仿现有 runtime-mode 路由）
- Modify: asgi 装配（reconciler 线程接线——`grep -n 'tee_sync_scheduler\|dau_snapshot' backend/asgi_app.py` 找现成后台环的接线点，同模式挂）
- Test: `tests/test_runtime_reconciler.py`（新建）

**Interfaces:**
- Consumes: `db.get_runtime_allowlist_map()`（Task 2）、`config_store.set_hosted_runtime_mode`（Task 5 双向）、`config_store.get_hosted_runtime_control_strict`、`backend/core/leader.py` 的 advisory-lock leader 原语、`admin_core.set_runtime_mode`（含 wake-schedule 种子，复用而非绕过）。
- Produces:
  - `runtime_reconciler.desired_for(user_id: str, allow_map: dict[str, str]) -> str`（纯函数，返回 `"v2"`/`"resident"`）
  - `runtime_reconciler.reconcile_once() -> dict`（returns `{"checked": int, "flipped": int, "failed": int, "skipped_backoff": int}`）
  - `runtime_reconciler.run_loop(stop_event) -> None`（leader-gated 周期环）
  - admin: `POST /v1/admin/runtime-allowlist`（body `{user_id, desired, note?}`；`desired:"remove"` 删行）、`GET /v1/admin/runtime-allowlist`（对账视图）。

- [ ] **Step 1: 写失败测试**

`tests/test_runtime_reconciler.py`：

```python
"""Reconciler: allowlist desired → per-user fence convergence."""
import time
import pytest
from hosted import runtime_reconciler
import db


def test_desired_for_defaults_resident(monkeypatch):
    monkeypatch.delenv("FEEDLING_RUNTIME_DEFAULT_DESIRED", raising=False)
    assert runtime_reconciler.desired_for("usr_x", {}) == "resident"
    assert runtime_reconciler.desired_for("usr_x", {"usr_x": "v2"}) == "v2"
    monkeypatch.setenv("FEEDLING_RUNTIME_DEFAULT_DESIRED", "v2")
    assert runtime_reconciler.desired_for("usr_x", {}) == "v2"          # P6 全量默认
    assert runtime_reconciler.desired_for("usr_x", {"usr_x": "resident"}) == "resident"  # 显式 pin 胜默认


def test_reconcile_once_converges_both_directions(fresh_user):
    uid = fresh_user
    db.upsert_runtime_allowlist(uid, "v2")
    stats = runtime_reconciler.reconcile_once()
    assert stats["flipped"] >= 1
    from hosted import config_store
    from core import store as core_store
    mode, state, _ = config_store.get_hosted_runtime_control_strict(core_store.get_store(uid))
    assert (mode, state) == ("db_action_v2", "v2")
    # 反向
    db.upsert_runtime_allowlist(uid, "resident")
    runtime_reconciler.reconcile_once()
    mode, state, _ = config_store.get_hosted_runtime_control_strict(core_store.get_store(uid))
    assert (mode, state) == ("resident_cli", "resident")


def test_reconcile_converged_user_is_noop(fresh_user):
    uid = fresh_user
    db.upsert_runtime_allowlist(uid, "v2")
    runtime_reconciler.reconcile_once()
    stats = runtime_reconciler.reconcile_once()   # 已收敛
    assert stats["flipped"] == 0


def test_one_bad_user_does_not_wedge_the_loop(fresh_user, second_user, monkeypatch):
    db.upsert_runtime_allowlist(fresh_user, "v2")
    db.upsert_runtime_allowlist(second_user, "v2")
    orig = runtime_reconciler._flip_user
    def boom_first(uid, desired):
        if uid == fresh_user:
            raise RuntimeError("simulated flip failure")
        return orig(uid, desired)
    monkeypatch.setattr(runtime_reconciler, "_flip_user", boom_first)
    stats = runtime_reconciler.reconcile_once()
    assert stats["failed"] == 1 and stats["flipped"] == 1   # 坏用户不挡好用户


def test_failed_user_backs_off(fresh_user, monkeypatch):
    db.upsert_runtime_allowlist(fresh_user, "v2")
    monkeypatch.setattr(runtime_reconciler, "_flip_user",
                        lambda uid, d: (_ for _ in ()).throw(RuntimeError("x")))
    runtime_reconciler.reconcile_once()
    stats = runtime_reconciler.reconcile_once()   # 退避窗口内
    assert stats["skipped_backoff"] >= 1
```

- [ ] **Step 2: 确认失败（module 不存在）**

```bash
python -m pytest tests/test_runtime_reconciler.py -q 2>&1 | tail -3
```

- [ ] **Step 3: 实现 runtime_reconciler.py**

```python
"""Dual-runtime canary reconciler.

Drives per-user fence (resident/draining/v2 + generation) toward the desired
runtime recorded in ``v2_user_allowlist``. Leader-elected via
``core.leader`` so only one backend worker runs the loop. The send hot path
never reads the allowlist table — the fence is the routing truth — so this
loop being down only pauses *transitions*, never delivery.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import db
from hosted import config_store

log = logging.getLogger(__name__)

RECONCILE_INTERVAL_SEC = float(os.environ.get("FEEDLING_RECONCILE_INTERVAL_SEC", "15"))
_DEFAULT_DESIRED_ENV = "FEEDLING_RUNTIME_DEFAULT_DESIRED"
_BACKOFF_BASE_SEC = 60.0
_BACKOFF_MAX_SEC = 3600.0

# user_id -> (fail_count, not_before_ts)；进程内即可——重启清零只是提早重试
_failures: dict[str, tuple[int, float]] = {}


def desired_for(user_id: str, allow_map: dict[str, str]) -> str:
    if user_id in allow_map:
        return allow_map[user_id]
    default = os.environ.get(_DEFAULT_DESIRED_ENV, "resident").strip().lower()
    return default if default in ("resident", "v2") else "resident"


def _flip_user(user_id: str, desired: str) -> None:
    """One fenced transition. Reuses admin_core.set_runtime_mode so the V2
    direction keeps its wake-schedule seeding (seed-before-persist order)."""
    from admin import admin_core  # noqa: PLC0415 — avoid import cycle at module load
    mode = (config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
            if desired == "v2" else config_store.HOSTED_RUNTIME_MODE_RESIDENT)
    body, status = admin_core.set_runtime_mode(user_id, mode)
    if status != 200:
        raise RuntimeError(f"set_runtime_mode({user_id}, {mode}) -> {status}: {body}")


def _current_actual(user_id: str) -> str | None:
    """'v2' | 'resident' | None(转换中/异常，本轮跳过)."""
    from core import store as core_store  # noqa: PLC0415
    try:
        mode, state, _gen = config_store.get_hosted_runtime_control_strict(
            core_store.get_store(user_id))
    except Exception:
        return None
    if mode == config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2 and state == "v2":
        return "v2"
    if mode == config_store.HOSTED_RUNTIME_MODE_RESIDENT and state == "resident":
        return "resident"
    return None  # draining 或不一致 tuple：等它收敛或人工介入，本轮不动


def reconcile_once() -> dict:
    stats = {"checked": 0, "flipped": 0, "failed": 0, "skipped_backoff": 0}
    allow_map = db.get_runtime_allowlist_map()
    # 范围 = 名单里的用户 + （默认为 v2 时）所有还在 resident 的托管用户。
    # P4/P5（默认 resident）阶段名单就是全部工作集；P6 翻默认后由
    # list_agent_runtime_enabled_users 提供存量 resident 用户集。
    user_ids = set(allow_map)
    if os.environ.get(_DEFAULT_DESIRED_ENV, "resident").strip().lower() == "v2":
        user_ids.update(r["user_id"] for r in db.list_agent_runtime_enabled_users())
    now = time.time()
    for uid in sorted(user_ids):
        stats["checked"] += 1
        fail_count, not_before = _failures.get(uid, (0, 0.0))
        if now < not_before:
            stats["skipped_backoff"] += 1
            continue
        desired = desired_for(uid, allow_map)
        actual = _current_actual(uid)
        if actual == desired:
            _failures.pop(uid, None)
            continue
        if actual is None:
            continue  # draining/不一致 tuple：转换中，下轮再看
        try:
            _flip_user(uid, desired)
            stats["flipped"] += 1
            _failures.pop(uid, None)
            log.info("[reconciler] flipped %s -> %s", uid, desired)
        except Exception as e:  # noqa: BLE001 — 单用户失败不挡环
            stats["failed"] += 1
            backoff = min(_BACKOFF_BASE_SEC * (2 ** fail_count), _BACKOFF_MAX_SEC)
            _failures[uid] = (fail_count + 1, now + backoff)
            log.warning("[reconciler] flip %s -> %s failed (retry in %.0fs): %s",
                        uid, desired, backoff, e)
    return stats


def run_loop(stop_event: threading.Event) -> None:
    from core import leader  # noqa: PLC0415
    # leader.py 的现成原语：session 级 pg_try_advisory_lock，同 key 全局单跑。
    # 具体 API 以 backend/core/leader.py 为准（先读它，签名对齐后接入）。
    while not stop_event.is_set():
        try:
            with leader.try_leadership("runtime_reconciler") as is_leader:
                if is_leader:
                    reconcile_once()
        except Exception as e:  # noqa: BLE001
            log.warning("[reconciler] loop error: %s", e)
        stop_event.wait(RECONCILE_INTERVAL_SEC)
```

⚠️ `leader.try_leadership` 是**假定名**——Step 3 落地前先读 `backend/core/leader.py`，用它真实的 API（以及 `tee_sync_scheduler.py` 怎么用它）改写 `run_loop`；测试不测 `run_loop`（leader 集成由现有 leader 测试覆盖），测 `reconcile_once`。

- [ ] **Step 4: admin 端点**

`admin_core.py` 追加：

```python
def set_runtime_allowlist(user_id: str, desired: str, *, note: str = "") -> tuple[dict, int]:
    if desired == "remove":
        removed = db.delete_runtime_allowlist(user_id)
        return {"user_id": user_id, "removed": removed}, 200
    try:
        db.upsert_runtime_allowlist(user_id, desired, updated_by="admin", note=note)
    except ValueError as e:
        return {"error": str(e)}, 400
    return {"user_id": user_id, "desired": desired}, 200


def get_runtime_allowlist() -> dict:
    from core import store as core_store  # noqa: PLC0415
    from hosted import config_store as cs  # noqa: PLC0415
    rows = db.list_runtime_allowlist()
    for row in rows:
        try:
            mode, state, gen = cs.get_hosted_runtime_control_strict(
                core_store.get_store(row["user_id"]))
            row["actual"] = {"mode": mode, "state": state, "generation": gen}
            row["converged"] = (
                (row["desired"] == "v2" and state == "v2")
                or (row["desired"] == "resident" and state == "resident"))
        except Exception as e:  # noqa: BLE001 — 对账视图不因单行炸
            row["actual"] = {"error": str(e)[:80]}
            row["converged"] = False
    return {"allowlist": rows}
```

`routes_asgi.py`：仿现有 runtime-mode 路由注册 `POST/GET /v1/admin/runtime-allowlist`（admin 鉴权装饰器与相邻路由一致）。

- [ ] **Step 5: 接线后台环**

在 asgi 装配层找 `tee_sync_scheduler`/`dau_snapshot_scheduler` 的启动点（lifespan 或 assembly），同模式挂 `runtime_reconciler.run_loop`（含 stop_event 优雅退出）。**asgi_app.py 是 assembly-only（CONTRIBUTING）——只加装配行，不加逻辑**。

- [ ] **Step 6: 跑绿 + 全量 + Commit**

```bash
python -m pytest tests/test_runtime_reconciler.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3
git add backend/hosted/runtime_reconciler.py backend/admin/ backend/asgi_app.py tests/test_runtime_reconciler.py
git commit -m "feat(dual-runtime): allowlist reconciler + admin endpoints"
```

---

### Task 9: 共存契约测试（替代 retirement 守卫）

**Files:**
- Create: `tests/test_dual_runtime_coexistence.py`

**Interfaces:** Consumes 前面所有 task 的产物；纯测试 task。

- [ ] **Step 1: 写契约测试（应直接绿——它验证的是 Task 2-8 的完成态；有红即前面有漏）**

```python
"""Coexistence contract: both runtimes fully wired under dual policy, and
v2_only remains exactly the pre-era behavior (the P7 retirement regression net).
Replaces test_hosted_resident_retirement.py for the coexistence window."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v1_implementation_present():
    for rel in (
        "backend/agent_runtime/supervisor.py",
        "backend/agent_runtime/spawners.py",
        "backend/agent_runtime/leases.py",
        "backend/agent_runtime/tokens.py",
    ):
        assert (ROOT / rel).exists(), rel


def test_v2_implementation_present():
    for rel in (
        "backend/model_api_runtime/v2/serve_worker.py",
        "backend/model_api_runtime/v2/worker.py",
        "backend/hosted/runtime_reconciler.py",
    ):
        assert (ROOT / rel).exists(), rel


def test_consumer_keeps_hosted_mode_support():
    # ec377440 的剥离已被 Task 4 反转；双运行时窗口内 consumer 必须双栈
    src = (ROOT / "tools/chat_resident_consumer.py").read_text()
    for needed in ("_HOSTED", "FEEDLING_API_KEY", "X-API-Key"):
        assert needed in src, needed


def test_v1_db_surface_present():
    import db
    for name in ("set_supervisor_heartbeat", "list_agent_runtime_enabled_users"):
        assert hasattr(db, name)


def test_dual_policy_routes_and_v2only_regresses(monkeypatch):
    from hosted import config_store
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "dual")
    assert config_store.hosted_runtime_policy() == "dual"
    monkeypatch.setenv("FEEDLING_HOSTED_RUNTIME_POLICY", "v2_only")
    assert config_store.hosted_runtime_policy() == "v2_only"
    # v2_only 的 send 行为回归由 test_model_api_chat_send_routing.py 的
    # runtime_policy_not_ready 用例覆盖（Task 6 Step 3 最后一行）


def test_reconciler_is_the_only_allowlist_reader_in_send_path():
    # 设计不变量：send 热路径不读 allowlist 表
    src = (ROOT / "backend/hosted/chat_send_core.py").read_text()
    assert "runtime_allowlist" not in src
```

- [ ] **Step 2: 跑绿 + 全量 + Commit**

```bash
python -m pytest tests/test_dual_runtime_coexistence.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3
git add tests/test_dual_runtime_coexistence.py
git commit -m "test(dual-runtime): coexistence contract replaces retirement guard"
```

---

### Task 10: flip 消息不丢专项（P0 硬门）

**Files:**
- Create: `tests/test_dual_runtime_flip_no_loss.py`

**Interfaces:** Consumes：Task 6 三态 send、Task 8 `_flip_user`。

- [ ] **Step 1: 先读参照**

读 `tests/test_chat_send_v2_enqueue.py` 与 `tests/test_v2_send_enqueue_atomic.py` 全文——它们已有「send→enqueue 原子性」的 fixture/断言手法，本 task 复用其建用户、发消息、查 `agent_jobs` 的全套工具函数。

- [ ] **Step 2: 写测试（行为规格）**

```python
"""P0 gate: no message lost / double-run across a runtime flip, both directions.

机制断言（不是端到端 LLM 回合）：
1. flip 前 send 的消息带旧 expected_runtime_mode 入队/入库；
2. flip 期间（draining 窗口）send 得到 503 runtime_switching，不产生半状态行；
3. flip 后 send 的消息带新 expected_runtime_mode；
4. 全程 chat 行数 == 成功 send 数（无丢失），每条消息 enqueue 恰好 ≤1 次（无双投）；
5. generation 严格递增，旧 generation 的 job 不会被新运行时按旧 mode 认领
   （断言 job 行的 expected_runtime_mode/generation 字段组合）。
"""


def test_flip_resident_to_v2_no_loss(dual_user, send, jobs_for, chat_rows):
    uid = dual_user  # 初始 resident
    ok1 = send(uid, "before-flip")           # resident 路径落库
    _flip(uid, "v2")                          # reconciler 单步（runtime_reconciler._flip_user）
    ok2 = send(uid, "after-flip")             # v2 路径入队
    rows = chat_rows(uid)
    assert {r["body"] for r in rows if r["role"] == "user"} == {"before-flip", "after-flip"}
    jobs = jobs_for(uid)
    modes = {j["expected_runtime_mode"] for j in jobs}
    assert modes <= {"db_action_v2"}          # v2 侧 job 只带 v2 mode
    assert len([j for j in jobs if j["dedupe_key"]]) == len(set(j["dedupe_key"] for j in jobs))


def test_flip_v2_to_resident_no_loss(dual_user_v2, send, jobs_for, chat_rows):
    uid = dual_user_v2  # 初始 v2（fixture 建号后先 flip 到 v2）
    ok1 = send(uid, "before-flip-back")       # v2 路径入队
    _flip(uid, "resident")
    ok2 = send(uid, "after-flip-back")        # resident 路径落库（不入 v2 队列）
    rows = chat_rows(uid)
    assert {r["body"] for r in rows if r["role"] == "user"} == {
        "before-flip-back", "after-flip-back"}
    jobs = jobs_for(uid)
    # flip 后的消息绝不能再进 v2 队列；flip 前的 job 保持旧 expected_runtime_mode
    after_flip_jobs = [j for j in jobs if j.get("payload_body") == "after-flip-back"
                       or j.get("body") == "after-flip-back"]
    assert after_flip_jobs == []
    assert all(j["expected_runtime_mode"] == "db_action_v2" for j in jobs)


def test_send_during_draining_is_clean_503(dual_user, send_raw, force_state):
    force_state(dual_user, "draining")
    body, status = send_raw(dual_user, "mid-drain")
    assert status == 503 and body["error"] == "runtime_switching"
    # 关键：无 chat 行、无 job 行（fail-closed 不落半状态）
```

fixture（`send`/`jobs_for`/`chat_rows`/`force_state`）在本文件内实现，全部照抄 Step 1 两个参照文件的手法改名——不发明新基建。

- [ ] **Step 3: 跑绿。任何一条红都按 systematic-debugging 走（先根因后修）——这是 P0 硬门，不许 skip/xfail 混过。**

- [ ] **Step 4: 全量 + Commit**

```bash
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3
git add tests/test_dual_runtime_flip_no_loss.py
git commit -m "test(dual-runtime): flip-no-loss P0 gate (both directions)"
```

---

### Task 11: compose / CI / 部署文件

**Files:**
- Modify: `deploy/docker-compose.phala.yaml`（prod 主 CVM）、`deploy/docker-compose.phala.test.yaml`、`deploy/docker-compose.phala.pre.yaml` —— 各加 serve-worker 服务
- Restore(from origin/test): `deploy/docker-compose.phala.prod.runner.yaml`（prod runner 回 V1-only 形态 = prod 现状）
- Modify: `deploy/docker-compose.phala.pre.runner.yaml`（移除 serve-worker，pre runner 回 V1-only——P1 用它测双跑）
- Modify: `deploy/pin-runtime-release.sh` + `.github/workflows/ci.yml`（V2 pin 目标从 runner compose 改为主 CVM compose 的 serve-worker 镜像 tag）
- Modify: `deploy/DEPLOYMENTS.md`、`docs/CHANGELOG.md`

- [ ] **Step 1: 主 CVM compose 加 serve-worker**

以 `deploy/docker-compose.phala.pre.runner.yaml` 现文件的 `x-serve-worker-env` 块为蓝本，在三份主 CVM compose 各加：

```yaml
  serve-worker:
    image: <该 compose 里 backend 服务的同一镜像引用>   # 同镜像双容器
    container_name: serve-worker
    command: ["python", "-u", "backend/model_api_runtime/v2/serve_worker.py"]
    restart: unless-stopped
    environment:
      FEEDLING_API_URL: "http://backend:<backend 服务内部端口，照抄 ingress 转发目标>"
      FEEDLING_ENCLAVE_URL: "http://enclave:<enclave 服务内部端口，照抄 backend 现用的 enclave 内网地址>"
      DATABASE_URL: "${DATABASE_URL}"
      FEEDLING_RUNTIME_TOKEN_SECRET: "${FEEDLING_RUNTIME_TOKEN_SECRET}"
      FEEDLING_V2_MAX_WORKERS: "4"
      FEEDLING_V2_FLEET_IDENTITY_REQUIRED: "1"
      FEEDLING_V2_RUNNER_CVM_ID: "${FEEDLING_V2_RUNNER_CVM_ID:-main-cvm}"
      FEEDLING_V2_DEPLOYED_BUILD: "${FEEDLING_V2_DEPLOYED_BUILD:-}"
      FEEDLING_GENESIS_WORKER_ENABLED: "1"
      FEEDLING_V2_SANDBOX_PROVIDER: "disabled"
      FEEDLING_V2_IMAGE_MAX_B64_CHARS: "2700000"
      FEEDLING_HOSTED_RUNTIME_POLICY: "dual"
      FEEDLING_RUNTIME_DEFAULT_DESIRED: "resident"
```

`backend`/`enclave` 的内部端口不许猜：从同一 compose 文件里 backend 现有的 enclave URL env 与 ingress 的转发目标里**照抄**。backend 服务自身 environment 里也加 `FEEDLING_HOSTED_RUNTIME_POLICY: "dual"` 与 `FEEDLING_RUNTIME_DEFAULT_DESIRED: "resident"`。

- [ ] **Step 2: runner compose 恢复**

```bash
git checkout origin/test -- deploy/docker-compose.phala.prod.runner.yaml
```

pre.runner.yaml：删除 serve-worker 服务块，从 `origin/test:deploy/docker-compose.phala.runner.yaml`（test 的 runner 模板）取 V1 agent-runner 服务块适配 pre 的环境变量名。

- [ ] **Step 3: CI/pin 脚本对账**

```bash
grep -n 'runner\|serve-worker\|pin-runtime' .github/workflows/ci.yml deploy/pin-runtime-release.sh | head -20
```

把「deploy runner CVM (pre)」流程的 pin 目标从 pre.runner.yaml 的 serve-worker 镜像改为 pre.yaml 主 CVM 的 serve-worker 镜像 tag；runner CVM 的 deploy job 改为部署 V1 形态 compose。**改完 dry-read 一遍 workflow 逻辑**：确保 push 本分支不会触发对 prod 的任何自动部署。

- [ ] **Step 4: 文档**

`deploy/DEPLOYMENTS.md` 加「双运行时拓扑」小节（拓扑图 + 环境变量表 + P3 部署序：migration 先行 → 主 CVM → runner 不动）；`docs/CHANGELOG.md` 记一条。

- [ ] **Step 5: compose 语法验证 + Commit**

```bash
docker compose -f deploy/docker-compose.phala.yaml config -q && echo OK-prod
docker compose -f deploy/docker-compose.phala.pre.yaml config -q && echo OK-pre
docker compose -f deploy/docker-compose.phala.test.yaml config -q && echo OK-test
git add deploy/ .github/workflows/ci.yml docs/CHANGELOG.md
git commit -m "deploy(dual-runtime): serve-worker on main CVM; runner back to V1-only"
```

（`config -q` 需要 env 占位符可解析；若报缺 env，用 `--env-file /dev/null` 加 `|| true` 判读输出是「缺变量」还是「语法错」，语法错必须修。）

---

### Task 12: 收尾全量验证

- [ ] **Step 1: 全量测试 + pyflakes**

```bash
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -5
python -m pyflakes backend/ tools/ | grep -v '_IDENTITY_RUNTIME_LABELS' || true
```

Expected: 基线 + 全部新增用例通过，0 新失败；pyflakes 除已知 1 条外干净。

- [ ] **Step 2: 对照 spec §5 恢复清单逐项打勾**（12 项全有着落；lane 表 §6 四行全有测试或代码引用）。

- [ ] **Step 3: 汇报**：分支 commit 列表、测试数字对比基线、已知偏差清单。**不 push**——push pre/test 会触发 CI 部署，须用户明确指令。

---

## 执行期通用规则

1. 每个 task 内先测后码（恢复类 task 例外：恢复本身就是「让既有测试变绿」）。
2. 任何测试失败先归因（pre-existing / 本 task 引入 / 依赖后续 task），禁止为绿改 V1 语义。
3. `git show 2b294a1f -- <file>` 是共享文件对账的唯一权威参照；`origin/test:<file>` 是 V1 整文件恢复的唯一权威来源。
4. 遇到 plan 与仓库现实冲突（符号改名、fixture 不存在）：以仓库现实为准小幅适配并记录；结构性冲突（如 fence 存储载体与假设不符）停下上报。
