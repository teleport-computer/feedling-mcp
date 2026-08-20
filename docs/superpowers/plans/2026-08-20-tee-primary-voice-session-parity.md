# TEE Primary Voice Session Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `voice_call_sessions` 在 RDS-primary 与 TEE-primary 两种部署中始终和当前 chat 主库共库，并用迁移、注册表与 promotion gate 防止再次漏表。

**Architecture:** PRE/PROD 从 `0025_lane_rollup_voice` 增加一个共享的实际 DDL revision；TEST 在同步该 revision 后，用双父 merge revision 接回现有 `0029_plaintext_shadow_merge`。注册表把复制 lane 与 TEE-primary schema 必需性分开：`voice_call_sessions` 走 SNAPSHOT，四张 primary-local 临时表保持 SKIP 但显式要求存在于 TEE。

**Tech Stack:** Python 3.11/3.12、Alembic、PostgreSQL 17、psycopg 3、pytest、GitHub Actions、MDX 文档站

**Spec:** `docs/superpowers/specs/2026-08-20-tee-primary-voice-session-parity-design.md`

## Global Constraints

- 共享 DDL revision 固定为 `0030_voice_call_sessions_primary`，父节点固定为 `0025_lane_rollup_voice`。
- PRE/PROD 只合入共享 revision，保持单 head `0030_voice_call_sessions_primary`。
- TEST 同步后新增 `0031_merge_voice_primary`，父节点为当时 TEST 单 head 与 `0030_voice_call_sessions_primary`；当前基线父节点是 `0029_plaintext_shadow_merge`。
- `voice_call_sessions` 必须逐列、PK、FK、CHECK、索引与 RDS `0081_voice_call_sessions` 一致。
- `voice_call_sessions` 使用 SNAPSHOT；并发 fence 仍只依赖当前主库内事务，不增加跨库 MIRROR 写。
- `genesis_import_chunks`、`v2_wake_shadow_decisions`、`voice_turn_results`、`voice_turn_streams` 保持 SKIP，但必须存在于 fresh TEE schema。
- RDS Alembic 版本表、TEE 同步控制表和一次性人工备份表保持 RDS-only。
- 不改公共 HTTP 请求/响应合同，不重新生成 OpenAPI。
- 每个生产代码改动先看到对应测试失败，再做最小实现；每个任务独立提交。

---

### Task 1: 建立共享 TEE DDL revision

**Files:**
- Create: `backend/alembic_tee/versions/0030_voice_call_sessions_primary.py`
- Modify: `backend/alembic/versions/0081_voice_call_sessions.py`
- Modify: `tests/test_pre_runtime_preflight.py`
- Modify: `tests/test_pre_test_migration_convergence.py`
- Modify: `tests/test_tee_schema.py`

**Interfaces:**
- Consumes: RDS revision `0081_voice_call_sessions` 的现有建表合同。
- Produces: 两条迁移链共享的模块常量 `_UP: str`；TEE head `0030_voice_call_sessions_primary`；head-bound `_UPDATE_PREPARED_HEAD: str`。

- [ ] **Step 1: 先把 head 与合同测试改成新预期**

在两个 preflight/convergence 测试中断言：

```python
assert script.get_heads() == ["0030_voice_call_sessions_primary"]
assert (
    script.get_revision("0030_voice_call_sessions_primary").down_revision
    == "0025_lane_rollup_voice"
)
assert (
    tee.get_revision("0030_voice_call_sessions_primary").module._UP
    == rds.get_revision("0081_voice_call_sessions").module._UP
)
```

在 `tests/test_tee_schema.py` 增加真实 schema 断言，查询 `information_schema.columns`、
`pg_constraint` 和 `pg_indexes`，至少锁住：

```python
assert columns == {
    "user_id": ("text", "NO", None),
    "call_id": ("text", "NO", None),
    "status": ("text", "NO", "'active'::text"),
    "cancel_reason": ("text", "NO", "''::text"),
    "created_at": ("timestamp with time zone", "NO", "now()"),
    "ended_at": ("timestamp with time zone", "YES", None),
}
assert primary_key == ["user_id", "call_id"]
assert foreign_key == ("users", "CASCADE")
assert "status = ANY" in status_check
assert "(user_id, status)" in status_index
```

- [ ] **Step 2: 运行迁移测试，确认因 revision/`_UP` 缺失而失败**

Run:

```bash
PYTHONPATH=backend ../../.venv-test/bin/python -m pytest -p no:cacheprovider \
  tests/test_pre_runtime_preflight.py \
  tests/test_pre_test_migration_convergence.py \
  tests/test_tee_schema.py -q
```

Expected: FAIL；head 仍是 `0025_lane_rollup_voice`，RDS module 尚无 `_UP`，TEE 表不存在。

- [ ] **Step 3: 把 RDS 历史 migration 的 SQL 提取成不改字节语义的 `_UP`**

在 `0081_voice_call_sessions.py` 中把原 `op.execute(...)` 字符串提取为：

```python
_UP = """
CREATE TABLE IF NOT EXISTS voice_call_sessions (
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  call_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'finalizing', 'cancelled', 'finalized')),
  cancel_reason TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ,
  PRIMARY KEY (user_id, call_id)
);
CREATE INDEX IF NOT EXISTS ix_voice_call_sessions_status
  ON voice_call_sessions (user_id, status);
"""


def upgrade() -> None:
    op.execute(_UP)
```

不要改变已发布 DDL 的列、默认值、约束或索引。

- [ ] **Step 4: 创建共享 TEE revision**

`0030_voice_call_sessions_primary.py` 使用同一 `_UP` 字符串，并更新 prepared marker head：

```python
revision = "0030_voice_call_sessions_primary"
down_revision = "0025_lane_rollup_voice"
branch_labels = None
depends_on = None

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0030_voice_call_sessions_primary"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
```

- [ ] **Step 5: 重跑迁移与 fresh-schema 测试**

Run: 使用 Step 2 的同一命令，并确保本地 PostgreSQL 可访问。

Expected: PASS；Alembic TEE 单 head 是 `0030_voice_call_sessions_primary`，合同完全一致。

- [ ] **Step 6: 提交共享 migration**

```bash
git add backend/alembic/versions/0081_voice_call_sessions.py \
  backend/alembic_tee/versions/0030_voice_call_sessions_primary.py \
  tests/test_pre_runtime_preflight.py \
  tests/test_pre_test_migration_convergence.py \
  tests/test_tee_schema.py
git commit -m "fix: add voice sessions to tee primary schema"
```

### Task 2: 分离复制 lane 与 TEE schema 必需性

**Files:**
- Modify: `backend/tee_shadow/table_registry.py`
- Modify: `tests/test_tee_table_registry.py`
- Modify: `tests/test_tee_snapshot.py`

**Interfaces:**
- Consumes: `Entry`, `REGISTRY`, `tables_in_lane()`, `synced_tables()`。
- Produces: `Entry.required_in_tee: bool | None`、`Entry.tee_required: bool`、`tee_required_tables() -> tuple[str, ...]`。

- [ ] **Step 1: 写 registry 语义的失败测试**

在 `tests/test_tee_table_registry.py` 增加：

```python
def test_tee_required_tables_include_synced_and_primary_local_tables():
    required = set(reg.tee_required_tables())
    assert set(reg.synced_tables()) <= required
    assert {
        "genesis_import_chunks",
        "v2_wake_shadow_decisions",
        "voice_turn_results",
        "voice_turn_streams",
        "voice_call_sessions",
    } <= required
    assert {
        "alembic_version",
        "tee_sync_runs",
        "tee_reconcile_state",
        "tee_reconcile_cursors",
        "bak_20260710_usr450_blobs",
    }.isdisjoint(required)


def test_voice_call_sessions_use_snapshot_lane():
    assert reg.REGISTRY["voice_call_sessions"].lane == reg.SNAPSHOT
```

把 `test_tee_schema_covers_every_synced_table` 改为通过
`reg.tee_required_tables()` 计算 `want`，并重命名为
`test_tee_schema_covers_every_required_table`。

- [ ] **Step 2: 运行 registry 测试并确认失败**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend ../../.venv-test/bin/python -m pytest -p no:cacheprovider \
  tests/test_tee_table_registry.py tests/test_tee_registry_guard_enforced.py -q
```

Expected: FAIL with missing `tee_required_tables` and `voice_call_sessions` still `SKIP`。

- [ ] **Step 3: 最小实现新 registry 合同**

扩展 dataclass：

```python
@dataclass(frozen=True)
class Entry:
    lane: str
    reason: str
    manual: bool = False
    required_in_tee: bool | None = None

    @property
    def tee_required(self) -> bool:
        if self.lane != SKIP:
            return True
        return self.required_in_tee is True


def tee_required_tables() -> tuple[str, ...]:
    return tuple(sorted(t for t, entry in REGISTRY.items() if entry.tee_required))
```

更新 `SKIP` 注释为“不做 RDS → TEE 数据复制”。四张 primary-local SKIP 表显式传
`required_in_tee=True`；`voice_call_sessions` 改为：

```python
"voice_call_sessions": Entry(
    SNAPSHOT,
    "语音取消/finalize 的当前主库并发控制面；RDS-primary 时整表快照保证切换前状态与 tombstone 收敛，TEE-primary 后与 chat 写事务共库",
),
```

- [ ] **Step 4: 写 voice session snapshot 的失败测试**

在 `tests/test_tee_snapshot.py` 使用真实两侧表插入同一用户和三条会话，调用现有
`snapshot.snapshot_table("voice_call_sessions")`，验证 insert/update/prune：

```python
result = snapshot.snapshot_table("voice_call_sessions")
assert result["ok"] is True
assert tee_rows == [
    (uid, "cancelled", "user_hangup"),
    (uid, "finalized", ""),
]
```

源侧删除第三条、更新前两条后，TEE 不得留下 stale call ID。

- [ ] **Step 5: 运行 snapshot 测试并确认先失败、实现后通过**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend ../../.venv-test/bin/python -m pytest -p no:cacheprovider \
  tests/test_tee_snapshot.py tests/test_tee_table_registry.py \
  tests/test_tee_registry_guard_enforced.py -q
```

Expected before registry change: FAIL because table不在 SNAPSHOT；after Step 3: PASS without
snapshot engine changes。

- [ ] **Step 6: 提交 registry 与 snapshot 覆盖**

```bash
git add backend/tee_shadow/table_registry.py \
  tests/test_tee_table_registry.py tests/test_tee_snapshot.py
git commit -m "fix: require primary-local tables in tee schema"
```

### Task 3: 锁住 TEE-primary 真实语音生命周期

**Files:**
- Create: `tests/test_voice_tee_primary.py`

**Interfaces:**
- Consumes: `db.close_pool()`、`db.voice_call_create_active()`、`db.voice_call_cancel()`、`db.voice_call_begin_finalize()`、`db.voice_call_mark_finalized()`、`db.voice_call_status()`。
- Produces: 以 TEE `DATABASE_URL` 执行真实 PostgreSQL lifecycle 的回归测试。

- [ ] **Step 1: 写 TEE-primary 生命周期测试**

测试保存原 `DATABASE_URL`，关闭 pool，把 `DATABASE_URL` 指向 `TEE_DATABASE_URL`，插入隔离
user 后验证两个竞争结果：

```python
db.close_pool()
monkeypatch.setenv("DATABASE_URL", os.environ["TEE_DATABASE_URL"])

db.voice_call_create_active(uid, "cancel-wins")
assert db.voice_call_cancel(uid, "cancel-wins", "connect_failed") == {
    "status": "cancelled", "replayed": False,
}
assert db.voice_call_begin_finalize(uid, "cancel-wins") == {
    "status": "cancelled", "replayed": True,
}

db.voice_call_create_active(uid, "finalize-wins")
assert db.voice_call_begin_finalize(uid, "finalize-wins")["status"] == "finalizing"
assert db.voice_call_mark_finalized(uid, "finalize-wins")["status"] == "finalized"
assert db.voice_call_cancel(uid, "finalize-wins", "user_hangup") == {
    "status": "finalized", "replayed": True,
}
```

`finally` 中删除测试 user、`db.close_pool()` 并恢复原 `DATABASE_URL`，避免污染后续测试。

- [ ] **Step 2: 在未包含 Task 1 migration 的基线演示确定性失败**

在父基线 commit 或通过临时 drop table 的隔离库运行：

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend ../../.venv-test/bin/python -m pytest -p no:cacheprovider \
  tests/test_voice_tee_primary.py -q
```

Expected: FAIL with `UndefinedTable: voice_call_sessions`。回到当前实现 commit 后重跑。

- [ ] **Step 3: 验证 migration 已使现有 db 实现原样通过**

Run: Step 2 同一命令。

Expected: PASS；不修改 `backend/db.py`，证明现有 `get_pool()` 主库抽象已经足够。

- [ ] **Step 4: 提交 TEE-primary 生命周期回归**

```bash
git add tests/test_voice_tee_primary.py
git commit -m "test: cover voice lifecycle on tee primary"
```

### Task 4: 把 voice lifecycle 加入 Phase 4 promotion gate

**Files:**
- Modify: `backend/admin/phase4_cutover.py`
- Modify: `tests/test_phase4_cutover.py`

**Interfaces:**
- Consumes: destination psycopg connection and `voice_call_sessions` schema contract。
- Produces: `_voice_session_smoke(destination) -> dict[str, object]`；cutover report key `voice_session_smoke`。

- [ ] **Step 1: 写 promotion gate 的失败测试**

扩展 `test_phase4_prepare_copies_frame_bridge_and_aligns_sequences`：

```python
assert report["voice_session_smoke"] == {
    "ok": True,
    "cancel_winner": "cancelled",
    "finalize_winner": "finalized",
}
```

再建一对隔离数据库，迁移后从 destination drop `voice_call_sessions`，调用 dry-run，断言：

```python
with pytest.raises(psycopg.errors.UndefinedTable):
    phase4_cutover.run(apply=False, writes_frozen=False)
```

测试库必须在 `finally` 中 terminate connections 并 drop，不触碰共享或线上数据库。

- [ ] **Step 2: 运行 Phase 4 测试确认缺少 report key/缺表未被 gate 捕获**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend ../../.venv-test/bin/python -m pytest -p no:cacheprovider \
  tests/test_phase4_cutover.py -q
```

Expected: FAIL；当前 report 没有 `voice_session_smoke`。

- [ ] **Step 3: 实现回滚事务中的数据库级 lifecycle smoke**

在模块 imports 增加 `import uuid`，新增 helper，使用随机 user/call ID，所有写入强制回滚：

```python
def _voice_session_smoke(destination: psycopg.Connection) -> dict[str, object]:
    suffix = uuid.uuid4().hex
    user_id = f"usr_phase4_voice_{suffix}"
    cancel_call = f"cancel-{suffix}"
    finalize_call = f"finalize-{suffix}"
    with destination.transaction(force_rollback=True):
        destination.execute(
            "INSERT INTO users (user_id,created_at,doc) VALUES (%s,'',%s)",
            (user_id, Jsonb({"user_id": user_id})),
        )
        destination.execute(
            "INSERT INTO voice_call_sessions (user_id,call_id,status) VALUES (%s,%s,'active')",
            (user_id, cancel_call),
        )
        destination.execute(
            "UPDATE voice_call_sessions SET status='cancelled',cancel_reason='promotion_smoke',ended_at=now() "
            "WHERE user_id=%s AND call_id=%s AND status='active'",
            (user_id, cancel_call),
        )
        destination.execute(
            "INSERT INTO voice_call_sessions (user_id,call_id,status) VALUES (%s,%s,'finalizing')",
            (user_id, finalize_call),
        )
        destination.execute(
            "UPDATE voice_call_sessions SET status='finalized',ended_at=now() "
            "WHERE user_id=%s AND call_id=%s AND status='finalizing'",
            (user_id, finalize_call),
        )
        rows = dict(destination.execute(
            "SELECT call_id,status FROM voice_call_sessions WHERE user_id=%s",
            (user_id,),
        ).fetchall())
        if rows != {cancel_call: "cancelled", finalize_call: "finalized"}:
            raise RuntimeError("TEE voice session lifecycle smoke failed")
    return {"ok": True, "cancel_winner": "cancelled", "finalize_winner": "finalized"}
```

在 head 验证之后、drain gate 之前调用它，并将返回值放入 dry-run/apply report。不要打印生成的
user ID 或 call ID。

- [ ] **Step 4: 重跑 Phase 4 和 voice 测试**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend ../../.venv-test/bin/python -m pytest -p no:cacheprovider \
  tests/test_phase4_cutover.py tests/test_voice_finalize_db.py \
  tests/test_voice_tee_primary.py -q
```

Expected: PASS；dry-run 后 destination 不残留 `usr_phase4_voice_*` 行。

- [ ] **Step 5: 提交 promotion gate**

```bash
git add backend/admin/phase4_cutover.py tests/test_phase4_cutover.py
git commit -m "fix: gate tee promotion on voice session lifecycle"
```

### Task 5: 同步公共架构与运维文档

**Files:**
- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify: `docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md`

**Interfaces:**
- Consumes: 新 TEE schema/registry/promotion gate 行为。
- Produces: 运维人员可执行且不误导的迁移顺序；Unreleased 用户可见部署说明。

- [ ] **Step 1: 更新 managed PostgreSQL promotion 架构段落**

在 `architecture.mdx` 明确：当前主库必须持有 voice lifecycle fence；promotion preflight 不仅验证
Alembic head，也在回滚事务中执行 create/cancel/finalize smoke。不要描述内部用户 ID、DSN 或
terminal preservation 行内容。

- [ ] **Step 2: 更新 runbook 顺序**

在 Phase 4 前新增可执行检查：

```sql
SELECT to_regclass('public.voice_call_sessions');
SELECT version_num FROM alembic_tee_version;
```

说明：共享 revision 上线后，旧的 head-bound preservation dry-run 作废，必须重新生成 plan
count 与 SHA-256；TEST 真实语音 smoke 通过后才能进入 PROD freeze。

- [ ] **Step 3: 更新 Unreleased changelog**

加入一条：managed TEE-primary schema 现在包含语音 lifecycle fence，promotion 会验证真实状态
转换，从而避免“数据库切换健康但一打电话即缺表”的部分成功。

- [ ] **Step 4: 运行文档检查**

Run:

```bash
cd docs-site
npm run types:check
npm run lint
npm run build
```

Expected: 全部 exit 0。无需运行 `npm run openapi:generate`，因为 HTTP contract 未变化。

- [ ] **Step 5: 提交文档**

```bash
git add docs-site/content/docs/architecture.mdx \
  docs-site/content/docs/changelog.mdx \
  docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md
git commit -m "docs: require voice smoke before tee promotion"
```

### Task 6: PRE 分支完整回归与发布审查

**Files:**
- Modify only if verification exposes a scoped defect.

**Interfaces:**
- Consumes: Tasks 1–5 的 PRE/PROD 单-head 实现。
- Produces: 可合入 PRE 的验证证据；不迁移或写入线上数据库。

- [ ] **Step 1: 跑 focused PostgreSQL 回归**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend ../../.venv-test/bin/python -m pytest -p no:cacheprovider \
  tests/test_pre_runtime_preflight.py \
  tests/test_pre_test_migration_convergence.py \
  tests/test_tee_schema.py \
  tests/test_tee_table_registry.py \
  tests/test_tee_registry_guard_enforced.py \
  tests/test_tee_snapshot.py \
  tests/test_voice_gateway.py \
  tests/test_voice_finalize_db.py \
  tests/test_voice_tee_primary.py \
  tests/test_phase4_cutover.py -q
```

Expected: PASS。

- [ ] **Step 2: 跑完整 backend suite**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
PYTHONPATH=backend ../../.venv-test/bin/python -m pytest -p no:cacheprovider tests -q
```

Expected: PASS；如有环境型失败，保留原始输出并单独重跑确认，不把 skip 当通过。

- [ ] **Step 3: 做静态收口**

```bash
git diff --check
git status --short
PYTHONPATH=backend ../../.venv-test/bin/python -m compileall -q \
  backend/alembic backend/alembic_tee backend/tee_shadow backend/admin
```

Expected: 无 whitespace/error；worktree 仅有计划内提交。

- [ ] **Step 4: 请求代码审查并修复 Important/Critical**

审查必须核对迁移 DAG、prepared-head pin、注册表分类、snapshot FK/order、promotion smoke 的回滚
语义、文档与 OpenAPI 判断。每个建议先复现/验证，再修改；修复后重跑受影响测试。

### Task 7: 同步到 TEST 并创建 merge revision

**Files:**
- Create on TEST integration branch: `backend/alembic_tee/versions/0031_merge_voice_primary.py`
- Modify on TEST integration branch: `backend/admin/plaintext_shadow.py`
- Modify on TEST integration branch: `backend/tee_shadow/table_registry.py`
- Modify on TEST integration branch: `tests/test_pre_runtime_preflight.py`
- Modify on TEST integration branch: `tests/test_pre_test_migration_convergence.py`
- Modify on TEST integration branch: any test-only head pin assertion that names `0029_plaintext_shadow_merge`

**Interfaces:**
- Consumes: 已合入 PRE 的共享 `0030_voice_call_sessions_primary` commit，以及 TEST 当前 TEE head。
- Produces: TEST 单 head `0031_merge_voice_primary`，同时包含原 `0029` ancestry 与共享 voice DDL branch。

- [x] **Step 1: 从最新 `origin/test` 建隔离 integration worktree并同步共享 commits**

先 `git fetch origin test pre`，确认 TEST 当前单 head。如果仍为 `0029_plaintext_shadow_merge`，
将 Tasks 1–5 的 commits cherry-pick/merge 到 TEST integration branch；若 head 已前进，把下列 merge
revision 的第一个父节点替换为实测单 head，并同步测试预期。

- [x] **Step 2: 先把 TEST head 测试改成 merge revision 并确认失败**

```python
assert script.get_heads() == ["0031_merge_voice_primary"]
assert set(script.get_revision("0031_merge_voice_primary").down_revision) == {
    "0029_plaintext_shadow_merge",
    "0030_voice_call_sessions_primary",
}
```

Run:

```bash
PYTHONPATH=backend .venv-test/bin/python -m pytest -p no:cacheprovider \
  tests/test_pre_runtime_preflight.py tests/test_pre_test_migration_convergence.py -q
```

Expected: FAIL because merge revision 尚不存在。

- [x] **Step 3: 创建 TEST merge revision**

```python
revision = "0031_merge_voice_primary"
down_revision = (
    "0029_plaintext_shadow_merge",
    "0030_voice_call_sessions_primary",
)
branch_labels = None
depends_on = None

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0031_merge_voice_primary"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute("TRUNCATE TABLE plaintext_shadow_restore_evidence")
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
```

merge revision 不重复执行建表 SQL；Alembic 会沿未执行的 `0030` 分支执行共享 DDL。由于
schema 已从 `0029` 改变，merge revision 清空绑定旧 head 的 restore evidence；同时把
plaintext-shadow 的合法 head pin 更新到 `0031`，并为新增 SNAPSHOT 表声明
`(user_id, call_id)` capture key。

- [x] **Step 4: 验证从现有 TEST head 升级**

创建隔离数据库，先只 upgrade 到 `0029_plaintext_shadow_merge`，断言缺表；再 upgrade 到 head，
断言 `voice_call_sessions` 存在且 `alembic_tee_version` 只有
`0031_merge_voice_primary`。随后运行 Tasks 1–4 的 focused suite。

- [ ] **Step 5: 提交并按仓库 branch flow 合入 TEST**

```bash
git add backend/alembic_tee/versions/0031_merge_voice_primary.py \
  tests/test_pre_runtime_preflight.py tests/test_pre_test_migration_convergence.py
git commit -m "fix: merge voice session schema into test tee head"
```

PR 目标为 `test`；记录 fresh migration、TEE-primary voice lifecycle 和 Phase 4 gate 证据。

### Task 8: 上线前只读与迁移操作

**Files:**
- No repository changes unless runbook evidence reveals a documentation defect.

**Interfaces:**
- Consumes: 已合入环境分支且验证过的 release commit。
- Produces: TEST 已修复证据、PROD TEE 新 head、重新生成的 preservation dry-run plan；不执行最终 preservation apply 或主库切换。

- [ ] **Step 1: 升级 TEST TEE schema并验证**

用 TEST owner migration DSN 执行 release-local `alembic_tee upgrade head`，随后只读检查：

```sql
SELECT version_num FROM alembic_tee_version;
SELECT to_regclass('public.voice_call_sessions');
```

Expected: TEST 单 head 为 merge revision，表名非 NULL。

- [ ] **Step 2: 运行 TEST database smoke 与一通真实语音 smoke**

先运行 release-local promotion/voice lifecycle dry-run，再走真实 TEST voice session
create → cancel/finalize。验收同时查 API 返回和数据库最终状态；任何失败停止后续 PROD 操作。

- [ ] **Step 3: 升级 PROD TEE shared head**

确认最新 base backup 仍可恢复，再用 PROD TEE owner migration DSN 将 `0025` 升到
`0030_voice_call_sessions_primary`。该步骤只改 shadow schema，不切 `DATABASE_URL`、不冻结线上
writer、不执行 preservation apply。

- [ ] **Step 4: 重新运行 PROD preservation dry-run**

使用与候选 release 完全相同的 checkout/TEE head 运行 dry-run，记录新的 aggregate count、
plan SHA-256、head 与 blockers。旧的 `cb17510b...` plan 视为失效，不得复用。

- [ ] **Step 5: 停在最终 freeze/apply 授权门前**

向用户报告 TEST smoke、PROD head 和新 dry-run 证据。只有在 writers 已冻结且用户对精确新 count
和 SHA-256 再次批准后，才允许执行 preservation apply 与 Phase 4 主库切换。
