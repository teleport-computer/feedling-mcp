# TEE SNAPSHOT Unique-Key Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every TEE SNAPSHOT table converge when RDS and TEE contain the same business-unique key under different primary keys.

**Architecture:** Keep the existing RDS binary COPY into a TEE temporary staging table. Within the existing destination transaction, prune target primary keys absent from staging before primary-key UPSERT so stale rows release secondary unique keys; retain all current guards, FK behavior, column-drift behavior, and rollback semantics.

**Tech Stack:** Python 3.11+, psycopg 3 SQL composition and binary COPY, PostgreSQL 16, pytest.

## Global Constraints

- Do not enumerate table-specific business unique keys or change database schema.
- Temporary-table COPY, stale-row prune, and UPSERT must remain in one explicit transaction.
- Retained rows with matching primary keys must update in place so external FK children survive.
- Source-absent rows must continue to follow normal PostgreSQL FK delete behavior.
- Existing no-primary-key, missing-common-primary-key, no-common-column, and `MAX_ROWS=200_000` guards remain unchanged.
- Phase 4 apply and TEE-primary configuration switching remain outside this implementation and require separate approval.

---

### Task 1: Reproduce and fix secondary unique-key replacement

**Files:**
- Modify: `tests/test_tee_snapshot.py`
- Modify: `backend/tee_shadow/snapshot.py`

**Interfaces:**
- Consumes: `snapshot.snapshot_table(table: str) -> dict` and internal `snapshot._prune_target(dst, table_ident, stage_ident, pk_idents) -> None`.
- Produces: unchanged public behavior and signatures; `_merge_payload(...)` executes stale-row prune before primary-key UPSERT.

- [ ] **Step 1: Add a real-table failing regression test**

Add this fixture and test beside the existing `_snap_probe` coverage in `tests/test_tee_snapshot.py`:

```python
@pytest.fixture
def unique_key_table():
    table = "_snap_unique_probe"
    ddl = (
        f"CREATE TABLE {table} ("
        "id TEXT PRIMARY KEY, business_key TEXT NOT NULL UNIQUE, v TEXT NOT NULL)"
    )
    for pool in (db.get_pool(), mirror.get_tee_pool()):
        with pool.connection() as c:
            c.execute(f"DROP TABLE IF EXISTS {table}")
            c.execute(ddl)
    yield table
    for pool in (db.get_pool(), mirror.get_tee_pool()):
        with pool.connection() as c:
            c.execute(f"DROP TABLE IF EXISTS {table}")


def test_snapshot_releases_stale_secondary_unique_key_before_upsert(unique_key_table):
    with db.get_pool().connection() as c:
        c.execute(
            f"INSERT INTO {unique_key_table} (id, business_key, v) "
            "VALUES ('source-id', 'same-key', 'source')"
        )
    with mirror.get_tee_pool().connection() as c:
        c.execute(
            f"INSERT INTO {unique_key_table} (id, business_key, v) "
            "VALUES ('stale-id', 'same-key', 'target')"
        )

    rep = snapshot.snapshot_table(unique_key_table)

    assert rep["ok"] is True
    with mirror.get_tee_pool().connection() as c:
        assert c.execute(
            f"SELECT id, business_key, v FROM {unique_key_table}"
        ).fetchall() == [("source-id", "same-key", "source")]
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests/test_tee_snapshot.py::test_snapshot_releases_stale_secondary_unique_key_before_upsert -q
```

Expected: FAIL because `rep["ok"]` is `False`, with a duplicate-key error from `_snap_unique_probe_business_key_key`.

- [ ] **Step 3: Strengthen the prune rollback regression before production changes**

Update `test_prune_failure_rolls_back_completed_upsert` so it proves a completed stale delete rolls back. Rename it to `test_failure_after_prune_rolls_back_stale_delete`, seed `('a', 'old')` and `('stale', 'old')` into RDS before the first snapshot, then delete `stale` and update `a` in RDS. Replace its monkeypatch with:

```python
real_prune = snapshot._prune_target

def fail_after_prune(*args, **kwargs):
    real_prune(*args, **kwargs)
    raise RuntimeError("injected failure after prune")

monkeypatch.setattr(snapshot, "_prune_target", fail_after_prune)
```

Assert `rep["ok"] is False`, the injected error is reported, and TEE still contains both pre-transaction rows:

```python
assert _tee_rows(sample_table) == [("a", "old"), ("stale", "old")]
```

- [ ] **Step 4: Run both focused tests before implementation**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest \
  tests/test_tee_snapshot.py::test_snapshot_releases_stale_secondary_unique_key_before_upsert \
  tests/test_tee_snapshot.py::test_failure_after_prune_rolls_back_stale_delete -q
```

Expected: the unique-key test FAILS for the production defect; the rollback test PASSES and guards the reordered transaction.

- [ ] **Step 5: Implement the minimal ordering change**

In `backend/tee_shadow/snapshot.py::_merge_payload`, move this existing call:

```python
_prune_target(dst, table_ident, stage_ident, pk_idents)
```

to immediately after the temporary-table binary COPY completes and before `mutable_cols` and the target `INSERT ... ON CONFLICT`. Add a short comment explaining that stale primary keys must release secondary unique keys before inserts. Do not change `_prune_target`, conflict targets, signatures, or table-specific behavior.

- [ ] **Step 6: Run focused and complete SNAPSHOT tests to verify GREEN**

Run the two-test command from Step 4, then:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests/test_tee_snapshot.py -q
```

Expected: both focused tests pass and the complete file passes with no failures.

- [ ] **Step 7: Commit the atomic bug fix**

```bash
git add backend/tee_shadow/snapshot.py tests/test_tee_snapshot.py
git commit -m "fix(tee): release stale unique keys before snapshot upsert"
```

---

### Task 2: Verify, review, and deploy to TEST

**Files:**
- Verify only: `backend/tee_shadow/snapshot.py`
- Verify only: `tests/test_tee_snapshot.py`
- Existing docs: `docs/superpowers/specs/2026-08-18-tee-snapshot-unique-key-replacement-design.md`

**Interfaces:**
- Consumes: Task 1's unchanged `snapshot_table` API and reordered `_merge_payload` transaction.
- Produces: reviewed commit suitable for merge into `test`; live TEST evidence that SNAPSHOT failures converge to zero.

- [ ] **Step 1: Run the related TEE test slice**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest \
  tests/test_tee_snapshot.py \
  tests/test_tee_sync_scheduler.py \
  tests/test_tee_sync_metrics.py \
  tests/test_phase4_cutover.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run repository verification**

Run the repository's established PostgreSQL-backed full suite from the isolated worktree:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests -q --ignore=tests/test_api.py
```

Expected: no new failures relative to the verified `test` baseline. Also run `git diff --check` and confirm the worktree is clean after commits.

- [ ] **Step 3: Review the branch diff**

Review `test...HEAD` for correctness, scope, FK safety, transaction atomicity, and test quality. Any Critical or Important finding must be fixed through a new RED/GREEN cycle before merge.

- [ ] **Step 4: Merge into local `test` and push**

After verification and review, merge `fix/tee-snapshot-unique-key-20260818` into local `test`, push `test` to `origin`, and monitor the CI workflow until both `deploy CVM (test)` and `deploy runner CVM (test)` succeed. Never push `main`.

- [ ] **Step 5: Verify live TEST convergence**

Confirm the main CVM and runner use the pushed SHA. Read the TEE replication status without printing secrets. Wait for a completed sync tick and require:

```text
snapshot_failures = 0
replicate_errors = 0
replicate_table_failures = 0
tee_healthy = true
```

Run strict verify and require its top-level `ok` and `strict_ok` signals to be true with no unconverged tables or requeue backlog. If any signal is red, stop and diagnose rather than performing cutover.

- [ ] **Step 6: Re-run Phase 4 dry-run only**

Inside the deployed TEST backend container, run:

```bash
cd /app/backend
python -m admin.phase4_cutover
```

Record schema head and drain blockers. Do not run `--apply`, stop writers, change DSNs, change `FEEDLING_DATABASE_SCHEMA`, or open plaintext writes without a separate explicit approval.
