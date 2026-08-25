# Chat Strict Snapshot Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the starvation-prone three-attempt optimistic chat snapshot reload with two optimistic attempts and one per-user serialized fallback that always converges unless the database itself fails.

**Architecture:** Keep the common path unchanged: read a repeatable-read snapshot outside `chat_lock` and install it only if `_chat_cache_generation` is stable. After two conflicts, perform the third and final snapshot while holding the same per-user `chat_lock`, preserving the three-query bound while preventing local cache commits from racing the install boundary.

**Tech Stack:** Python 3.11, `threading.Lock`, psycopg/PostgreSQL repeatable-read snapshots, pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-chat-snapshot-convergence-and-rds-trace-repair-design.md`

## Global Constraints

- Preserve `UserStore.reload_chat_hot_strict() -> list[dict]` and `UserStore.ensure_chat_fresh(*, force: bool = False, target_version: int | None = None) -> bool`.
- Keep at most three calls to `db.chat_load_hot_snapshot_strict(user_id, limit)` per reload.
- Keep the first two snapshot reads outside `chat_lock`.
- The third snapshot read and `_replace_chat_rows_locked()` must execute inside one `chat_lock` critical section.
- Database errors must preserve the last-good cache and keep the existing strict/fail-open contracts.
- Do not change hot-cache size, public Chat behavior, durable event semantics, or lock ordering.
- Do not add trace migration changes; T306 is already in the `origin/test` baseline.
- Target pull requests at `test`, never directly at `main`.

---

### Task 1: Add deterministic starvation and failure regression tests

**Files:**
- Modify: `tests/test_chat_incremental_sync.py`

**Interfaces:**
- Consumes: `_bare_store(rows=(), version=0, limit=256)` and `_row(msg_id, seq, **extra)`.
- Consumes: `UserStore.reload_chat_hot_strict() -> list[dict]`.
- Produces: regression coverage for two optimistic conflicts followed by a locked third snapshot.

- [ ] **Step 1: Add the repeated-conflict convergence test**

Append this test after `test_strict_snapshot_retries_if_local_commit_lands_during_db_read`:

```python
def test_strict_snapshot_serializes_final_attempt_after_repeated_local_commits(
    monkeypatch,
):
    store = _bare_store([_row("old", 1)], version=1)
    calls = []

    def snapshot(_uid, _limit):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            assert store.chat_lock.locked() is False
            store.apply_committed_chat_rows([_row("local-a", 2)])
            return 1, [_row("old", 1)]
        if len(calls) == 2:
            assert store.chat_lock.locked() is False
            store.apply_committed_chat_rows([_row("local-b", 3)])
            return 2, [_row("old", 1), _row("local-a", 2)]
        assert store.chat_lock.locked() is True
        return 3, [
            _row("old", 1),
            _row("local-a", 2),
            _row("local-b", 3),
        ]

    monkeypatch.setattr(
        core_store.db, "chat_load_hot_snapshot_strict", snapshot
    )

    rows = store.reload_chat_hot_strict()

    assert calls == [1, 2, 3]
    assert [row["id"] for row in rows] == ["old", "local-a", "local-b"]
    assert store.chat_version == 3
    assert store.chat_max_seq == 3
    assert set(store._chat_messages_by_id) == {"old", "local-a", "local-b"}
```

- [ ] **Step 2: Add the locked-fallback database-failure test**

```python
def test_strict_snapshot_locked_fallback_failure_preserves_last_good_cache(
    monkeypatch,
):
    store = _bare_store([_row("kept", 1)], version=1)
    calls = []

    def snapshot(_uid, _limit):
        calls.append(len(calls) + 1)
        if len(calls) <= 2:
            store.apply_committed_chat_rows([
                _row(f"local-{len(calls)}", len(calls) + 1)
            ])
            return 1, [_row("kept", 1)]
        assert store.chat_lock.locked() is True
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        core_store.db, "chat_load_hot_snapshot_strict", snapshot
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        store.reload_chat_hot_strict()

    assert calls == [1, 2, 3]
    assert [row["id"] for row in store.chat_messages] == [
        "kept",
        "local-1",
        "local-2",
    ]
    assert store.chat_version == 1
```

- [ ] **Step 3: Run the two new tests and verify RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest \
  tests/test_chat_incremental_sync.py::test_strict_snapshot_serializes_final_attempt_after_repeated_local_commits \
  tests/test_chat_incremental_sync.py::test_strict_snapshot_locked_fallback_failure_preserves_last_good_cache \
  -q
```

Expected: both tests fail at `assert store.chat_lock.locked() is True`, proving that the current third read is still optimistic. The existing source must not pass either test before implementation.

- [ ] **Step 4: Commit the RED tests**

```bash
git add tests/test_chat_incremental_sync.py
git commit -m "test(chat): reproduce strict snapshot starvation"
```

---

### Task 2: Add the serialized third snapshot

**Files:**
- Modify: `backend/core/store.py:528-547`
- Test: `tests/test_chat_incremental_sync.py`

**Interfaces:**
- Consumes: `db.chat_load_hot_snapshot_strict(user_id: str, limit: int) -> tuple[int, list[dict]]`.
- Consumes: `UserStore._replace_chat_rows_locked(rows: list[dict], *, version: int) -> None`.
- Produces: `UserStore.reload_chat_hot_strict() -> list[dict]` with bounded convergence under local generation churn.

- [ ] **Step 1: Reduce optimistic attempts from three to two**

In `reload_chat_hot_strict()`, change:

```python
for _attempt in range(3):
```

to:

```python
for _attempt in range(2):
```

Keep the existing generation capture, lock-free database call, and guarded install unchanged.

- [ ] **Step 2: Replace the starvation exception with one locked snapshot**

Replace:

```python
raise RuntimeError("chat cache changed during strict snapshot reload")
```

with:

```python
with self.chat_lock:
    self._ensure_chat_cache_state_locked()
    version, rows = db.chat_load_hot_snapshot_strict(
        self.user_id, limit
    )
    self._replace_chat_rows_locked(rows, version=version)
    return list(self.chat_messages)
```

Do not catch database exceptions here. If the read fails, `_replace_chat_rows_locked()` is never reached and the existing cache remains intact.

- [ ] **Step 3: Run the focused RED tests and verify GREEN**

Run the exact command from Task 1 Step 3.

Expected: `2 passed`.

- [ ] **Step 4: Run the full chat incremental state-machine file**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests/test_chat_incremental_sync.py -q
```

Expected: all tests pass with a real PostgreSQL-backed test included; no database-backed module may be silently skipped.

- [ ] **Step 5: Commit the implementation**

```bash
git add backend/core/store.py
git commit -m "fix(chat): guarantee strict snapshot convergence"
```

---

### Task 3: Run cross-worker and Chat regression coverage

**Files:**
- Verify: `backend/core/store.py`
- Verify: `tests/test_wake_bus.py`
- Verify: `tests/test_chat_poll_cross_worker_staleness.py`
- Verify: `tests/test_chat_response_finalize_cas.py`

**Interfaces:**
- Consumes: the unchanged `ensure_chat_fresh()` and wake-bus contracts.
- Produces: evidence that lock ordering, multi-worker reconciliation, and finalize paths remain compatible.

- [ ] **Step 1: Run the cross-worker regression slice**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest \
  tests/test_chat_incremental_sync.py \
  tests/test_wake_bus.py \
  tests/test_chat_poll_cross_worker_staleness.py \
  tests/test_chat_response_finalize_cas.py \
  -q
```

Expected: all tests pass and the real PostgreSQL reconciliation test executes.

- [ ] **Step 2: Run syntax and whitespace checks**

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m py_compile backend/core/store.py tests/test_chat_incremental_sync.py
git diff --check
```

Expected: both commands exit 0 with no output from `git diff --check`.

- [ ] **Step 3: Record verification without changing product code**

Update the checkbox state in this plan only after the commands have run. Do not add logging, metrics, refactors, or unrelated cleanup to this repair.
